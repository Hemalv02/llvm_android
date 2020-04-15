#!/usr/bin/env python3
#
# Copyright (C) 2016 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# pylint: disable=not-callable, line-too-long, no-else-return

import argparse
import glob
import logging
from pathlib import Path
import os
import shutil
import string
import sys
import textwrap
from typing import cast, Dict, List, Optional, Set, Sequence

import android_version
import builders
from builder_registry import BuilderRegistry
import configs
import constants
import hosts
import paths
import source_manager
import toolchains
import utils
from version import Version

import mapfile

ORIG_ENV = dict(os.environ)

# Remove GOMA from our environment for building anything from stage2 onwards,
# since it is using a non-GOMA compiler (from stage1) to do the compilation.
USE_GOMA_FOR_STAGE1 = False
if ('USE_GOMA' in ORIG_ENV) and (ORIG_ENV['USE_GOMA'] == 'true'):
    USE_GOMA_FOR_STAGE1 = True
    del ORIG_ENV['USE_GOMA']

BASE_TARGETS = 'X86'
ANDROID_TARGETS = 'AArch64;ARM;BPF;X86'

# TODO (Pirama): Put all the build options in a global so it's easy to refer to
# them instead of plumbing flags through function parameters.
BUILD_LLDB = False
BUILD_LLVM_NEXT = False

def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


def install_file(src, dst):
    """Proxy for shutil.copy2 with logging and dry-run support."""
    logger().info('copy %s %s', src, dst)
    shutil.copy2(src, dst)


def remove(path):
    """Proxy for os.remove with logging."""
    logger().debug('remove %s', path)
    os.remove(path)


def extract_clang_version(clang_install) -> Version:
    version_file = (Path(clang_install) / 'include' / 'clang' / 'Basic' /
                    'Version.inc')
    return Version(version_file)


def pgo_profdata_filename():
    svn_revision = android_version.get_svn_revision(BUILD_LLVM_NEXT)
    base_revision = svn_revision.rstrip(string.ascii_lowercase)
    return '%s.profdata' % base_revision

def pgo_profdata_file(profdata_file):
    profile = utils.android_path('prebuilts', 'clang', 'host', 'linux-x86',
                                 'profiles', profdata_file)
    return profile if os.path.exists(profile) else None


def ndk_base():
    ndk_version = 'r20'
    return utils.android_path('toolchain/prebuilts/ndk', ndk_version)


def android_api(arch: hosts.Arch, platform=False):
    if platform:
        return 29
    elif arch in [hosts.Arch.ARM, hosts.Arch.I386]:
        return 16
    else:
        return 21


def ndk_libcxx_headers():
    return os.path.join(ndk_base(), 'sources', 'cxx-stl', 'llvm-libc++',
                        'include')


def ndk_libcxxabi_headers():
    return os.path.join(ndk_base(), 'sources', 'cxx-stl', 'llvm-libc++abi',
                        'include')


def ndk_toolchain_lib(arch: hosts.Arch, toolchain_root, host_tag):
    toolchain_lib = os.path.join(ndk_base(), 'toolchains', toolchain_root,
                                 'prebuilt', 'linux-x86_64', host_tag)
    if arch in [hosts.Arch.ARM, hosts.Arch.I386]:
        toolchain_lib = os.path.join(toolchain_lib, 'lib')
    else:
        toolchain_lib = os.path.join(toolchain_lib, 'lib64')
    return toolchain_lib


def support_headers():
    return os.path.join(ndk_base(), 'sources', 'android', 'support', 'include')


def clang_prebuilt_base_dir():
    return utils.android_path('prebuilts/clang/host',
                              hosts.build_host().os_tag, constants.CLANG_PREBUILT_VERSION)


def clang_prebuilt_bin_dir():
    return utils.android_path(clang_prebuilt_base_dir(), 'bin')


def clang_resource_dir(version, arch: Optional[hosts.Arch] = None):
    arch_str = arch.value if arch else ''
    return os.path.join('lib64', 'clang', version, 'lib', 'linux', arch_str)


def clang_prebuilt_libcxx_headers():
    return utils.android_path(clang_prebuilt_base_dir(), 'include', 'c++', 'v1')


def libcxx_header_dirs(ndk_cxx):
    if ndk_cxx:
        return [
            ndk_libcxx_headers(),
            ndk_libcxxabi_headers(),
            support_headers()
        ]
    else:
        # <prebuilts>/include/c++/v1 includes the cxxabi headers
        return [
            clang_prebuilt_libcxx_headers(),
            utils.android_path('bionic', 'libc', 'include')
        ]


def cmake_bin_path():
    return utils.android_path('prebuilts/cmake', hosts.build_host().os_tag, 'bin/cmake')


def ninja_bin_path():
    return utils.android_path('prebuilts/ninja', hosts.build_host().os_tag, 'ninja')


def check_create_path(path):
    if not os.path.exists(path):
        os.makedirs(path)


def get_sysroot(arch: hosts.Arch, platform=False):
    sysroots = utils.out_path('sysroots')
    platform_or_ndk = 'platform' if platform else 'ndk'
    return os.path.join(sysroots, platform_or_ndk, arch.ndk_arch)


def debug_prefix_flag():
    return '-fdebug-prefix-map={}='.format(utils.android_path())


def go_bin_dir():
    return utils.android_path('prebuilts/go', hosts.build_host().os_tag, 'bin')


def update_cmake_sysroot_flags(defines, sysroot):
    defines['CMAKE_SYSROOT'] = sysroot
    defines['CMAKE_FIND_ROOT_PATH_MODE_INCLUDE'] = 'ONLY'
    defines['CMAKE_FIND_ROOT_PATH_MODE_LIBRARY'] = 'ONLY'
    defines['CMAKE_FIND_ROOT_PATH_MODE_PACKAGE'] = 'ONLY'
    defines['CMAKE_FIND_ROOT_PATH_MODE_PROGRAM'] = 'NEVER'


def rm_cmake_cache(cacheDir):
    for dirpath, dirs, files in os.walk(cacheDir): # pylint: disable=not-an-iterable
        if 'CMakeCache.txt' in files:
            os.remove(os.path.join(dirpath, 'CMakeCache.txt'))
        if 'CMakeFiles' in dirs:
            utils.rm_tree(os.path.join(dirpath, 'CMakeFiles'))


def invoke_cmake(out_path, defines, env, cmake_path, target=None, install=True):
    flags = ['-G', 'Ninja']

    flags += ['-DCMAKE_MAKE_PROGRAM=' + ninja_bin_path()]

    for key in defines:
        newdef = '-D' + key + '=' + defines[key]
        flags += [newdef]
    flags += [cmake_path]

    check_create_path(out_path)
    # TODO(srhines): Enable this with a flag, because it forces clean builds
    # due to the updated cmake generated files.
    #rm_cmake_cache(out_path)

    if target:
        ninja_target = [target]
    else:
        ninja_target = []

    utils.check_call([cmake_bin_path()] + flags, cwd=out_path, env=env)
    utils.check_call([ninja_bin_path()] + ninja_target, cwd=out_path, env=env)
    if install:
        utils.check_call([ninja_bin_path(), 'install'], cwd=out_path, env=env)


def cross_compile_configs(toolchain, platform=False, static=False):
    configs = [
        (hosts.Arch.ARM, 'arm/arm-linux-androideabi-4.9/arm-linux-androideabi',
         'arm-linux-android', '-march=armv7-a'),
        (hosts.Arch.AARCH64,
         'aarch64/aarch64-linux-android-4.9/aarch64-linux-android',
         'aarch64-linux-android', ''),
        (hosts.Arch.X86_64,
         'x86/x86_64-linux-android-4.9/x86_64-linux-android',
         'x86_64-linux-android', ''),
        (hosts.Arch.I386, 'x86/x86_64-linux-android-4.9/x86_64-linux-android',
         'i686-linux-android', '-m32'),
    ]

    cc = os.path.join(toolchain, 'bin', 'clang')
    cxx = os.path.join(toolchain, 'bin', 'clang++')
    llvm_config = os.path.join(toolchain, 'bin', 'llvm-config')

    for (arch, toolchain_path, llvm_triple, extra_flags) in configs:
        if static:
            api_level = android_api(arch, platform=True)
        else:
            api_level = android_api(arch, platform)
        toolchain_root = utils.android_path('prebuilts/gcc',
                                            hosts.build_host().os_tag)
        toolchain_bin = os.path.join(toolchain_root, toolchain_path, 'bin')
        sysroot = get_sysroot(arch, platform)

        defines = {}
        defines['CMAKE_C_COMPILER'] = cc
        defines['CMAKE_CXX_COMPILER'] = cxx
        defines['LLVM_CONFIG_PATH'] = llvm_config

        # Include the directory with libgcc.a to the linker search path.
        toolchain_builtins = os.path.join(
            toolchain_root, toolchain_path, '..', 'lib', 'gcc',
            os.path.basename(toolchain_path), '4.9.x')
        # The 32-bit libgcc.a is sometimes in a separate subdir
        if arch == hosts.Arch.I386:
            toolchain_builtins = os.path.join(toolchain_builtins, '32')

        if arch == hosts.Arch.ARM:
            toolchain_lib = ndk_toolchain_lib(arch, 'arm-linux-androideabi-4.9',
                                              'arm-linux-androideabi')
        elif arch in [hosts.Arch.I386, hosts.Arch.X86_64]:
            toolchain_lib = ndk_toolchain_lib(arch, arch.ndk_arch + '-4.9',
                                              llvm_triple)
        else:
            toolchain_lib = ndk_toolchain_lib(arch, llvm_triple + '-4.9',
                                              llvm_triple)

        ldflags = [
            '-L' + toolchain_builtins, '-Wl,-z,defs',
            '-L' + toolchain_lib,
            '-fuse-ld=lld',
            '-Wl,--gc-sections',
            '-Wl,--build-id=sha1',
            '-pie',
        ]
        if static:
            ldflags.append('-static')
        if not platform:
            triple = 'arm-linux-androideabi' if arch == hosts.Arch.ARM else llvm_triple
            libcxx_libs = os.path.join(ndk_base(), 'toolchains', 'llvm',
                                       'prebuilt', 'linux-x86_64', 'sysroot',
                                       'usr', 'lib', triple)
            ldflags += ['-L', os.path.join(libcxx_libs, str(api_level))]
            ldflags += ['-L', libcxx_libs]

        defines['CMAKE_EXE_LINKER_FLAGS'] = ' '.join(ldflags)
        defines['CMAKE_SHARED_LINKER_FLAGS'] = ' '.join(ldflags)
        defines['CMAKE_MODULE_LINKER_FLAGS'] = ' '.join(ldflags)
        update_cmake_sysroot_flags(defines, sysroot)

        macro_api_level = 10000 if platform else api_level

        cflags = [
            debug_prefix_flag(),
            '--target=%s' % llvm_triple,
            '-B%s' % toolchain_bin,
            '-D__ANDROID_API__=' + str(macro_api_level),
            '-ffunction-sections',
            '-fdata-sections',
            extra_flags,
        ]
        yield (arch, llvm_triple, defines, cflags)


class AsanMapFileBuilder(builders.Builder):
    name: str = 'asan-mapfile'
    config_list: List[configs.Config] = configs.android_configs()

    @property
    def toolchain(self) -> toolchains.Toolchain:
        return toolchains.get_runtime_toolchain()

    def _build_config(self) -> None:
        arch = self._config.target_arch
        # We can not build asan_test using current CMake building system. Since
        # those files are not used to build AOSP, we just simply touch them so that
        # we can pass the build checks.
        asan_test_path = self.toolchain.path / 'test' / arch.llvm_arch / 'bin'
        asan_test_path.mkdir(parents=True, exist_ok=True)
        asan_test_bin_path = asan_test_path / 'asan_test'
        asan_test_bin_path.touch(exist_ok=True)

        lib_dir = self.toolchain.resource_dir
        self._build_sanitizer_map_file('asan', arch, lib_dir)
        self._build_sanitizer_map_file('ubsan_standalone', arch, lib_dir)

        if arch == hosts.Arch.AARCH64:
            self._build_sanitizer_map_file('hwasan', arch, lib_dir)

    @staticmethod
    def _build_sanitizer_map_file(san: str, arch: hosts.Arch, lib_dir: Path) -> None:
        lib_file = lib_dir / f'libclang_rt.{san}-{arch.llvm_arch}-android.so'
        map_file = lib_dir / f'libclang_rt.{san}-{arch.llvm_arch}-android.map.txt'
        mapfile.create_map_file(lib_file, map_file)


def build_libcxx(toolchain, clang_version):
    for (arch, llvm_triple, libcxx_defines,
         cflags) in cross_compile_configs(toolchain): # pylint: disable=not-an-iterable
        logger().info('Building libcxx for %s', arch.value)
        libcxx_path = utils.out_path('lib', 'libcxx-' + arch.value)

        libcxx_defines['CMAKE_ASM_FLAGS'] = ' '.join(cflags)
        libcxx_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
        libcxx_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)
        libcxx_defines['CMAKE_BUILD_TYPE'] = 'Release'

        libcxx_env = dict(ORIG_ENV)

        libcxx_cmake_path = utils.llvm_path('libcxx')
        rm_cmake_cache(libcxx_path)

        invoke_cmake(
            out_path=libcxx_path,
            defines=libcxx_defines,
            env=libcxx_env,
            cmake_path=libcxx_cmake_path,
            install=False)
        # We need to install libcxx manually.
        install_subdir = clang_resource_dir(clang_version.long_version(),
                                            hosts.Arch.from_triple(llvm_triple))
        libcxx_install = os.path.join(toolchain, install_subdir)

        libcxx_libs = os.path.join(libcxx_path, 'lib')
        check_create_path(libcxx_install)
        for f in os.listdir(libcxx_libs):
            if f.startswith('libc++'):
                shutil.copy2(os.path.join(libcxx_libs, f), libcxx_install)


def build_libcxxabi(toolchain: toolchains.Toolchain, build_arch: hosts.Arch) -> Path:
    # TODO: Refactor cross_compile_configs to support per-arch queries in
    # addition to being a generator.
    for (arch, llvm_triple, defines, cflags) in \
         cross_compile_configs(toolchain.path, platform=True): # pylint: disable=not-an-iterable

        # Build only the requested arch.
        if arch != build_arch:
            continue

        logger().info('Building libcxxabi for %s', arch.value)
        defines['LIBCXXABI_LIBCXX_INCLUDES'] = utils.llvm_path('libcxx', 'include')
        defines['LIBCXXABI_ENABLE_SHARED'] = 'OFF'
        defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
        defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)

        out_path = utils.out_path('lib', 'libcxxabi-' + arch.value)
        if os.path.exists(out_path):
            utils.rm_tree(out_path)

        invoke_cmake(out_path=out_path,
                     defines=defines,
                     env=dict(ORIG_ENV),
                     cmake_path=utils.llvm_path('libcxxabi'),
                     install=False)
        return Path(out_path)
    raise ValueError(f"{build_arch} is not supported.")


def build_llvm_for_windows(enable_assertions,
                           build_name):
    win_builder = WindowsToolchainBuilder()
    if win_builder.install_dir.exists():
        shutil.rmtree(win_builder.install_dir)

    # Build and install libcxxabi and libcxx and use them to build Clang.
    libcxxabi_builder = LibCxxAbiBuilder()
    libcxxabi_builder.enable_assertions = enable_assertions
    libcxxabi_builder.build()

    libcxx_builder = LibCxxBuilder()
    libcxx_builder.enable_assertions = enable_assertions
    libcxx_builder.build()

    win_builder.build_name = build_name
    win_builder.svn_revision = android_version.get_svn_revision(BUILD_LLVM_NEXT)
    win_builder.build_lldb = BUILD_LLDB
    win_builder.enable_assertions = enable_assertions
    win_builder.build()

    return win_builder.install_dir


def host_sysroot():
    if hosts.build_host().is_darwin:
        return ""
    else:
        return utils.android_path('prebuilts/gcc', hosts.build_host().os_tag,
                                  'host/x86_64-linux-glibc2.17-4.8/sysroot')


def host_gcc_toolchain_flags(host: hosts.Host, is_32_bit=False):
    cflags: List[str] = [debug_prefix_flag()]
    ldflags: List[str] = []

    if host.is_darwin:
        return cflags, ldflags

    # GCC toolchain flags for Linux and Windows
    if host.is_linux:
        gccRoot = utils.android_path('prebuilts/gcc', hosts.build_host().os_tag,
                                     'host/x86_64-linux-glibc2.17-4.8')
        gccTriple = 'x86_64-linux'
        gccVersion = '4.8.3'

        # gcc-toolchain is only needed for Linux
        cflags.append(f'--gcc-toolchain={gccRoot}')
    elif host.is_windows:
        gccRoot = utils.android_path('prebuilts/gcc', hosts.build_host().os_tag,
                                     'host/x86_64-w64-mingw32-4.8')
        gccTriple = 'x86_64-w64-mingw32'
        gccVersion = '4.8.3'

    cflags.append(f'-B{gccRoot}/{gccTriple}/bin')

    gccLibDir = f'{gccRoot}/lib/gcc/{gccTriple}/{gccVersion}'
    gccBuiltinDir = f'{gccRoot}/{gccTriple}/lib64'
    if is_32_bit:
        gccLibDir += '/32'
        gccBuiltinDir = gccBuiltinDir.replace('lib64', 'lib32')

    ldflags.extend(('-B' + gccLibDir,
                    '-L' + gccLibDir,
                    '-B' + gccBuiltinDir,
                    '-L' + gccBuiltinDir,
                    '-fuse-ld=lld',
                   ))

    return cflags, ldflags


class Stage1Builder(builders.LLVMBuilder):
    name: str = 'stage1'
    toolchain_name: str = 'prebuilt'
    install_dir: Path = paths.OUT_DIR / 'stage1-install'
    build_llvm_tools: bool = False
    build_all_targets: bool = False
    config_list: List[configs.Config] = [configs.host_config()]

    @property
    def llvm_targets(self) -> Set[str]:
        if self.build_all_targets:
            return set(ANDROID_TARGETS.split(';'))
        else:
            return set(BASE_TARGETS.split(';'))

    @property
    def llvm_projects(self) -> Set[str]:
        proj = {'clang', 'lld', 'libcxxabi', 'libcxx', 'compiler-rt'}
        if self.build_llvm_tools:
            # For lldb-tblgen. It will be used to build lldb-server and
            # windows lldb.
            proj.add('lldb')
        return proj

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        # Point CMake to the libc++.so from the prebuilts.  Install an rpath
        # to prevent linking with the newly-built libc++.so
        ldflags.append(f'-Wl,-rpath,{self.toolchain.lib_dir}')
        return ldflags

    def set_lldb_flags(self, target: hosts.Host, defines: Dict[str, str]) -> None:
        # Disable dependencies because we only need lldb-tblgen to be built.
        defines['LLDB_ENABLE_PYTHON'] = 'OFF'
        defines['LLDB_ENABLE_LIBEDIT'] = 'OFF'

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['CLANG_ENABLE_ARCMT'] = 'OFF'
        defines['CLANG_ENABLE_STATIC_ANALYZER'] = 'OFF'

        if self.build_llvm_tools:
            defines['LLVM_BUILD_TOOLS'] = 'ON'
        else:
            defines['LLVM_BUILD_TOOLS'] = 'OFF'

        # Make libc++.so a symlink to libc++.so.x instead of a linker script that
        # also adds -lc++abi.  Statically link libc++abi to libc++ so it is not
        # necessary to pass -lc++abi explicitly.  This is needed only for Linux.
        if self._config.target_os.is_linux:
            defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'
            defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'

        # Do not build compiler-rt for Darwin.  We don't ship host (or any
        # prebuilt) runtimes for Darwin anyway.  Attempting to build these will
        # fail compilation of lib/builtins/atomic_*.c that only get built for
        # Darwin and fail compilation due to us using the bionic version of
        # stdatomic.h.
        if self._config.target_os.is_darwin:
            defines['LLVM_BUILD_EXTERNAL_COMPILER_RT'] = 'ON'

        # Don't build libfuzzer as part of the first stage build.
        defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'

        return defines

    @property
    def env(self) -> Dict[str, str]:
        env = super().env
        if USE_GOMA_FOR_STAGE1:
            env['USE_GOMA'] = 'true'
        return env


def install_lldb_deps(install_dir: Path, host: hosts.Host):
    lib_dir = install_dir / ('bin' if host.is_windows else 'lib64')
    check_create_path(lib_dir)

    python_prebuilt_dir: Path = paths.get_python_dir(host)
    python_dest_dir: Path = install_dir / 'python3'
    shutil.copytree(python_prebuilt_dir, python_dest_dir, symlinks=True,
                    ignore=shutil.ignore_patterns('*.pyc', '__pycache__', '.git', 'Android.bp'))

    py_lib = paths.get_python_dynamic_lib(host).relative_to(python_prebuilt_dir)
    dest_py_lib = python_dest_dir / py_lib
    py_lib_rel = os.path.relpath(dest_py_lib, lib_dir)
    os.symlink(py_lib_rel, lib_dir / py_lib.name)
    if not host.is_windows:
        libedit_root = BuilderRegistry.get('libedit').install_dir
        shutil.copy2(paths.get_libedit_lib(libedit_root, host), lib_dir)


class Stage2Builder(builders.LLVMBuilder):
    name: str = 'stage2'
    toolchain_name: str = 'stage1'
    install_dir: Path = paths.OUT_DIR / 'stage2-install'
    config_list: List[configs.Config] = [configs.host_config()]
    remove_install_dir: bool = True
    build_lldb: bool = True
    debug_build: bool = False
    build_instrumented: bool = False
    profdata_file: Optional[Path] = None
    lto: bool = True

    @property
    def llvm_targets(self) -> Set[str]:
        return set(ANDROID_TARGETS.split(';'))

    @property
    def llvm_projects(self) -> Set[str]:
        proj = {'clang', 'lld', 'libcxxabi', 'libcxx', 'compiler-rt',
                'clang-tools-extra', 'openmp', 'polly'}
        if self.build_lldb:
            proj.add('lldb')
        return proj

    @property
    def env(self) -> Dict[str, str]:
        env = super().env
        # Point CMake to the libc++ from stage1.  It is possible that once built,
        # the newly-built libc++ may override this because of the rpath pointing to
        # $ORIGIN/../lib64.  That'd be fine because both libraries are built from
        # the same sources.
        env['LD_LIBRARY_PATH'] = str(self.toolchain.lib_dir)
        return env

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        if self.build_instrumented:
            # Building libcxx, libcxxabi with instrumentation causes linker errors
            # because these are built with -nodefaultlibs and prevent libc symbols
            # needed by libclang_rt.profile from being resolved.  Manually adding
            # the libclang_rt.profile to linker flags fixes the issue.
            resource_dir = self.toolchain.resource_dir
            ldflags.append(str(resource_dir / 'libclang_rt.profile-x86_64.a'))
        return ldflags

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        if self.profdata_file:
            cflags.append('-Wno-profile-instr-out-of-date')
            cflags.append('-Wno-profile-instr-unprofiled')
        return cflags

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['SANITIZER_ALLOW_CXXABI'] = 'OFF'
        defines['OPENMP_ENABLE_OMPT_TOOLS'] = 'FALSE'
        defines['LIBOMP_ENABLE_SHARED'] = 'FALSE'
        defines['CLANG_PYTHON_BINDINGS_VERSIONS'] = '3'

        if (self.lto and
                not self._config.target_os.is_darwin and
                not self.build_instrumented and
                not self.debug_build):
            defines['LLVM_ENABLE_LTO'] = 'Thin'

        # Build libFuzzer here to be exported for the host fuzzer builds. libFuzzer
        # is not currently supported on Darwin.
        if self._config.target_os.is_darwin:
            defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'
        else:
            defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'ON'

        if self.debug_build:
            defines['CMAKE_BUILD_TYPE'] = 'Debug'

        if self.build_instrumented:
            defines['LLVM_BUILD_INSTRUMENTED'] = 'ON'

            # llvm-profdata is only needed to finish CMake configuration
            # (tools/clang/utils/perf-training/CMakeLists.txt) and not needed for
            # build
            llvm_profdata = self.toolchain.path / 'bin' / 'llvm-profdata'
            defines['LLVM_PROFDATA'] = str(llvm_profdata)
        elif self.profdata_file:
            defines['LLVM_PROFDATA_FILE'] = str(self.profdata_file)

        # Make libc++.so a symlink to libc++.so.x instead of a linker script that
        # also adds -lc++abi.  Statically link libc++abi to libc++ so it is not
        # necessary to pass -lc++abi explicitly.  This is needed only for Linux.
        if self._config.target_os.is_linux:
            defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'
            defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'

        # Do not build compiler-rt for Darwin.  We don't ship host (or any
        # prebuilt) runtimes for Darwin anyway.  Attempting to build these will
        # fail compilation of lib/builtins/atomic_*.c that only get built for
        # Darwin and fail compilation due to us using the bionic version of
        # stdatomic.h.
        if self._config.target_os.is_darwin:
            defines['LLVM_BUILD_EXTERNAL_COMPILER_RT'] = 'ON'

        if self._config.target_os.is_darwin:
            if utils.is_available_mac_ver('10.11'):
                raise RuntimeError('libcompression can be enabled for macOS 10.11 and above.')
            defines['HAVE_LIBCOMPRESSION'] = '0'
        return defines


class SysrootsBuilder(builders.Builder):
    name: str = 'sysroots'
    config_list: List[configs.Config] = (
        configs.android_configs(platform=True) +
        configs.android_configs(platform=False)
    )

    @property
    def toolchain(self) -> toolchains.Toolchain:
        return toolchains.get_runtime_toolchain()

    def _build_config(self) -> None:
        config: configs.AndroidConfig = cast(configs.AndroidConfig, self._config)
        arch = config.target_arch
        platform = config.platform
        sysroot = config.sysroot
        if sysroot.exists():
            shutil.rmtree(sysroot)
        sysroot.mkdir(parents=True, exist_ok=True)

        base_header_path = paths.NDK_BASE / 'sysroot' / 'usr' / 'include'
        base_lib_path = paths.NDK_BASE / 'platforms' / f'android-{config.api_level}'
        dest_usr = sysroot / 'usr'

        # Copy over usr/include.
        dest_usr_include = dest_usr / 'include'
        shutil.copytree(base_header_path, dest_usr_include, symlinks=True)

        # Copy over usr/include/asm.
        asm_headers = base_header_path / arch.ndk_triple / 'asm'
        dest_usr_include_asm = dest_usr_include / 'asm'
        shutil.copytree(asm_headers, dest_usr_include_asm, symlinks=True)

        # Copy over usr/lib.
        arch_lib_path = base_lib_path / f'arch-{arch.ndk_arch}' / 'usr' / 'lib'
        dest_usr_lib = dest_usr / 'lib'
        shutil.copytree(arch_lib_path, dest_usr_lib, symlinks=True)

        # For only x86_64, we also need to copy over usr/lib64
        if arch == hosts.Arch.X86_64:
            arch_lib64_path = base_lib_path / f'arch-{arch.ndk_arch}' / 'usr' / 'lib64'
            dest_usr_lib64 = dest_usr / 'lib64'
            shutil.copytree(arch_lib64_path, dest_usr_lib64, symlinks=True)

        if platform:
            # Create a stub library for the platform's libc++.
            platform_stubs = paths.OUT_DIR / 'platform_stubs' / arch.ndk_arch
            platform_stubs.mkdir(parents=True, exist_ok=True)
            libdir = dest_usr_lib64 if arch == hosts.Arch.X86_64 else dest_usr_lib
            with (platform_stubs / 'libc++.c').open('w') as f:
                f.write(textwrap.dedent("""\
                    void __cxa_atexit() {}
                    void __cxa_demangle() {}
                    void __cxa_finalize() {}
                    void __dynamic_cast() {}
                    void _ZTIN10__cxxabiv117__class_type_infoE() {}
                    void _ZTIN10__cxxabiv120__si_class_type_infoE() {}
                    void _ZTIN10__cxxabiv121__vmi_class_type_infoE() {}
                    void _ZTISt9type_info() {}
                """))

            utils.check_call([self.toolchain.cc,
                              f'--target={arch.llvm_triple}',
                              '-fuse-ld=lld', '-nostdlib', '-shared',
                              '-Wl,-soname,libc++.so',
                              '-o{}'.format(libdir / 'libc++.so'),
                              str(platform_stubs / 'libc++.c')])

            # For arm64 and x86_64, build static cxxabi library from
            # toolchain/libcxxabi and use it when building runtimes.  This
            # should affect all compiler-rt runtimes that use libcxxabi
            # (e.g. asan, hwasan, scudo, tsan, ubsan, xray).
            if arch not in (hosts.Arch.AARCH64, hosts.Arch.X86_64):
                with (libdir / 'libc++abi.so').open('w') as f:
                    f.write('INPUT(-lc++)')
            else:
                # We can build libcxxabi only after the sysroots are
                # created.  Build it for the current arch and copy it to
                # <libdir>.
                out_dir = build_libcxxabi(self.toolchain, arch)
                out_path = out_dir / 'lib64' / 'libc++abi.a'
                shutil.copy2(out_path, libdir)


class CompilerRTBuilder(builders.LLVMRuntimeBuilder):
    name: str = 'compiler-rt'
    src_dir: Path = paths.LLVM_PATH / 'compiler-rt'
    config_list: List[configs.Config] = (
        configs.android_configs(platform=True) +
        configs.android_configs(platform=False)
    )

    @property
    def install_dir(self) -> Path:
        if self._config.platform:
            return self.toolchain.clang_lib_dir
        # Installs to a temporary dir and copies to runtimes_ndk_cxx manually.
        output_dir = self.output_dir
        return output_dir.parent / (output_dir.name + '-install')

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        arch = self._config.target_arch
        # FIXME: Disable WError build until upstream fixed the compiler-rt
        # personality routine warnings caused by r309226.
        # defines['COMPILER_RT_ENABLE_WERROR'] = 'ON'
        defines['COMPILER_RT_TEST_COMPILER_CFLAGS'] = defines['CMAKE_C_FLAGS']
        defines['COMPILER_RT_TEST_TARGET_TRIPLE'] = arch.llvm_triple
        defines['COMPILER_RT_INCLUDE_TESTS'] = 'OFF'
        defines['SANITIZER_CXX_ABI'] = 'libcxxabi'
        # With CMAKE_SYSTEM_NAME='Android', compiler-rt will be installed to
        # lib/android instead of lib/linux.
        del defines['CMAKE_SYSTEM_NAME']
        libs: List[str] = []
        if arch == 'arm':
            libs += ['-latomic']
        if self._config.api_level < 21:
            libs += ['-landroid_support']
        defines['SANITIZER_COMMON_LINK_LIBS'] = ' '.join(libs)
        if self._config.platform:
            defines['COMPILER_RT_HWASAN_WITH_INTERCEPTORS'] = 'OFF'
        return defines

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-funwind-tables')
        return cflags

    def install_config(self) -> None:
        # Still run `ninja install`.
        super().install_config()

        # Install the fuzzer library to the old {arch}/libFuzzer.a path for
        # backwards compatibility.
        arch = self._config.target_arch
        sarch = 'i686' if arch == hosts.Arch.I386 else arch.value
        static_lib_filename = 'libclang_rt.fuzzer-' + sarch + '-android.a'

        lib_dir = self.install_dir / 'lib' / 'linux'
        arch_dir = lib_dir / arch.value
        arch_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lib_dir / static_lib_filename, arch_dir / 'libFuzzer.a')

        if not self._config.platform:
            dst_dir = self.toolchain.path / 'runtimes_ndk_cxx'
            shutil.copytree(lib_dir, dst_dir, dirs_exist_ok=True)

    def install(self) -> None:
        # Install libfuzzer headers once for all configs.
        header_src = self.src_dir / 'lib' / 'fuzzer'
        header_dst = self.toolchain.path / 'prebuilt_include' / 'llvm' / 'lib' / 'Fuzzer'
        header_dst.mkdir(parents=True, exist_ok=True)
        for f in header_src.iterdir():
            if f.suffix in ('.h', '.def'):
                shutil.copy2(f, header_dst)

        symlink_path = self.toolchain.resource_dir / 'libclang_rt.hwasan_static-aarch64-android.a'
        symlink_path.unlink(missing_ok=True)
        os.symlink('libclang_rt.hwasan-aarch64-android.a', symlink_path)


class CompilerRTHostI386Builder(builders.LLVMRuntimeBuilder):
    name: str = 'compiler-rt-i386-host'
    src_dir: Path = paths.LLVM_PATH / 'compiler-rt'
    config_list: List[configs.Config] = [configs.LinuxConfig(is_32_bit=True)]

    @property
    def install_dir(self) -> Path:
        return self.toolchain.clang_lib_dir

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        # Due to CMake and Clang oddities, we need to explicitly set
        # CMAKE_C_COMPILER_TARGET and use march=i686 in cflags below instead of
        # relying on auto-detection from the Compiler-rt CMake files.
        defines['CMAKE_C_COMPILER_TARGET'] = 'i386-linux-gnu'
        defines['COMPILER_RT_INCLUDE_TESTS'] = 'ON'
        defines['COMPILER_RT_ENABLE_WERROR'] = 'ON'
        defines['SANITIZER_CXX_ABI'] = 'libstdc++'
        return defines

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        # compiler-rt/lib/gwp_asan uses PRIu64 and similar format-specifier macros.
        # Add __STDC_FORMAT_MACROS so their definition gets included from
        # inttypes.h.  This explicit flag is only needed here.  64-bit host runtimes
        # are built in stage1/stage2 and get it from the LLVM CMake configuration.
        # These are defined unconditionaly in bionic and newer glibc
        # (https://sourceware.org/git/gitweb.cgi?p=glibc.git;h=1ef74943ce2f114c78b215af57c2ccc72ccdb0b7)
        cflags.append('-D__STDC_FORMAT_MACROS')
        cflags.append('--target=i386-linux-gnu')
        cflags.append('-march=i686')
        return cflags

    def _build_config(self) -> None:
        # Also remove the "stamps" created for the libcxx included in libfuzzer so
        # CMake runs the configure again (after the cmake caches are deleted).
        stamp_path = self.output_dir / 'lib' / 'fuzzer' / 'libcxx_fuzzer_i386-stamps'
        if stamp_path.exists():
            shutil.rmtree(stamp_path)
        super()._build_config()


class LibOMPBuilder(builders.LLVMRuntimeBuilder):
    name: str = 'libomp'
    src_dir: Path = paths.LLVM_PATH / 'openmp'

    config_list: List[configs.Config] = (
        configs.android_configs(platform=True, extra_config={'is_shared': False}) +
        configs.android_configs(platform=False, extra_config={'is_shared': False}) +
        configs.android_configs(platform=False, extra_config={'is_shared': True})
    )

    @property
    def is_shared(self) -> bool:
        return cast(Dict[str, bool], self._config.extra_config)['is_shared']

    @property
    def output_dir(self) -> Path:
        old_path = super().output_dir
        suffix = '-shared' if self.is_shared else '-static'
        return old_path.parent / (old_path.name + suffix)

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['CMAKE_POSITION_INDEPENDENT_CODE'] = 'ON'
        defines['OPENMP_ENABLE_LIBOMPTARGET'] = 'FALSE'
        defines['OPENMP_ENABLE_OMPT_TOOLS'] = 'FALSE'
        defines['LIBOMP_ENABLE_SHARED'] = 'TRUE' if self.is_shared else 'FALSE'
        # Minimum version for OpenMP's CMake is too low for the CMP0056 policy
        # to be ON by default.
        defines['CMAKE_POLICY_DEFAULT_CMP0056'] = 'NEW'
        return defines

    def install_config(self) -> None:
        # We need to install libomp manually.
        libname = 'libomp.' + ('so' if self.is_shared else 'a')
        src_lib = self.output_dir / 'runtime' / 'src' / libname
        dst_dir = self.install_dir
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_lib, dst_dir / libname)


class LibEditBuilder(builders.AutoconfBuilder):
    name: str = 'libedit'
    src_dir: Path = paths.LIBEDIT_SRC_DIR
    config_list: List[configs.Config] = [configs.host_config()]

    def install(self) -> None:
        super().install()
        if self._config.target_os.is_darwin:
            # Updates LC_ID_DYLIB so that users of libedit won't link with absolute path.
            libedit_path = paths.get_libedit_lib(self.install_dir,
                                                 self._config.target_os)
            cmd = ['install_name_tool',
                   '-id', f'@rpath/{libedit_path.name}',
                   str(libedit_path)]
            utils.check_call(cmd)


class SwigBuilder(builders.AutoconfBuilder):
    name: str = 'swig'
    src_dir: Path = paths.SWIG_SRC_DIR
    config_list: List[configs.Config] = [configs.host_config()]

    @property
    def config_flags(self) -> List[str]:
        flags = super().config_flags
        flags.append('--without-pcre')
        return flags

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        # Point to the libc++.so from the toolchain.
        ldflags.append(f'-Wl,-rpath,{self.toolchain.lib_dir}')
        return ldflags


class LldbServerBuilder(builders.LLVMRuntimeBuilder):
    name: str = 'lldb-server'
    src_dir: Path = paths.LLVM_PATH / 'llvm'
    config_list: List[configs.Config] = configs.android_configs(platform=False, static=True)
    ninja_target: str = 'lldb-server'

    @property
    def cflags(self) -> List[str]:
        cflags: List[str] = super().cflags
        # The build system will add '-stdlib=libc++' automatically. Since we
        # have -nostdinc++ here, -stdlib is useless. Adds a flag to avoid the
        # warnings.
        cflags.append('-Wno-unused-command-line-argument')
        return cflags

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        # lldb depends on support libraries.
        defines['LLVM_ENABLE_PROJECTS'] = 'clang;lldb'
        defines['LLVM_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'llvm-tblgen')
        defines['CLANG_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'clang-tblgen')
        defines['LLDB_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'lldb-tblgen')
        return defines

    def install_config(self) -> None:
        src_path = self.output_dir / 'bin' / 'lldb-server'
        install_dir = self.install_dir
        install_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, install_dir)


class LibCxxAbiBuilder(builders.LLVMRuntimeBuilder):
    name = 'libcxxabi'
    src_dir: Path = paths.LLVM_PATH / 'libcxxabi'
    config_list: List[configs.Config] = [configs.WindowsConfig()]

    @property
    def install_dir(self):
        return paths.OUT_DIR / 'windows-x86-64-install'

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines: Dict[str, str] = super().cmake_defines
        defines['LIBCXXABI_ENABLE_NEW_DELETE_DEFINITIONS'] = 'OFF'
        defines['LIBCXXABI_LIBCXX_INCLUDES'] = str(paths.LLVM_PATH /'libcxx' / 'include')

        # Build only the static library.
        defines['LIBCXXABI_ENABLE_SHARED'] = 'OFF'

        if self.enable_assertions:
            defines['LIBCXXABI_ENABLE_ASSERTIONS'] = 'ON'

        return defines

    @property
    def cflags(self) -> List[str]:
        cflags: List[str] = super().cflags
        # Disable libcxx visibility annotations and enable WIN32 threads.  These
        # are needed because the libcxxabi build happens before libcxx and uses
        # headers directly from the sources.
        cflags.append('-D_LIBCPP_DISABLE_VISIBILITY_ANNOTATIONS')
        cflags.append('-D_LIBCPP_HAS_THREAD_API_WIN32')
        return cflags


class LibCxxBuilder(builders.LLVMRuntimeBuilder):
    name = 'libcxx'
    src_dir: Path = paths.LLVM_PATH / 'libcxx'
    config_list: List[configs.Config] = [configs.WindowsConfig()]

    @property
    def install_dir(self):
        return paths.OUT_DIR / 'windows-x86-64-install'

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines: Dict[str, str] = super().cmake_defines
        defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'
        defines['LIBCXX_CXX_ABI'] = 'libcxxabi'
        defines['LIBCXX_HAS_WIN32_THREAD_API'] = 'ON'

        # Use cxxabi header from the source directory since it gets installed
        # into install_dir only during libcxx's install step.  But use the
        # library from install_dir.
        defines['LIBCXX_CXX_ABI_INCLUDE_PATHS'] = str(paths.LLVM_PATH / 'libcxxabi' / 'include')
        defines['LIBCXX_CXX_ABI_LIBRARY_PATH'] = str(BuilderRegistry.get('libcxxabi').install_dir / 'lib64')

        # Build only the static library.
        defines['LIBCXX_ENABLE_SHARED'] = 'OFF'

        if self.enable_assertions:
            defines['LIBCXX_ENABLE_ASSERTIONS'] = 'ON'

        return defines

    @property
    def cflags(self) -> List[str]:
        cflags: List[str] = super().cflags
        # Disable libcxxabi visibility annotations since we're only building it
        # statically.
        cflags.append('-D_LIBCXXABI_DISABLE_VISIBILITY_ANNOTATIONS')
        return cflags


class WindowsToolchainBuilder(builders.LLVMBuilder):
    name: str = 'windows-x86-64'
    toolchain_name: str = 'stage1'
    config_list: List[configs.Config] = [configs.WindowsConfig()]
    build_lldb: bool = True

    @property
    def install_dir(self) -> Path:
        return paths.OUT_DIR / 'windows-x86-64-install'

    @property
    def llvm_targets(self) -> Set[str]:
        return set(ANDROID_TARGETS.split(';'))

    @property
    def llvm_projects(self) -> Set[str]:
        proj = {'clang', 'clang-tools-extra', 'lld'}
        if self.build_lldb:
            proj.add('lldb')
        return proj

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        # Don't build compiler-rt, libcxx etc. for Windows
        defines['LLVM_BUILD_RUNTIME'] = 'OFF'
        # Build clang-tidy/clang-format for Windows.
        defines['LLVM_TOOL_CLANG_TOOLS_EXTRA_BUILD'] = 'ON'
        defines['LLVM_TOOL_OPENMP_BUILD'] = 'OFF'
        # Don't build tests for Windows.
        defines['LLVM_INCLUDE_TESTS'] = 'OFF'

        defines['LLVM_CONFIG_PATH'] = str(self.toolchain.build_path / 'bin' / 'llvm-config')
        defines['LLVM_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'llvm-tblgen')
        defines['CLANG_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'clang-tblgen')
        if self.build_lldb:
            defines['LLDB_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'lldb-tblgen')
        return defines

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        ldflags.append('-Wl,--dynamicbase')
        ldflags.append('-Wl,--nxcompat')
        # Use static-libgcc to avoid runtime dependence on libgcc_eh.
        ldflags.append('-static-libgcc')
        # pthread is needed by libgcc_eh.
        ldflags.append('-pthread')
        # Add path to libc++, libc++abi.
        libcxx_lib = BuilderRegistry.get('libcxx').install_dir / 'lib64'
        ldflags.append(f'-L{libcxx_lib}')
        ldflags.append('-Wl,--high-entropy-va')
        ldflags.append('-Wl,--Xlink=-Brepro')
        ldflags.append(f'-L{paths.WIN_ZLIB_LIB_PATH}')
        return ldflags

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-DMS_WIN64')
        cflags.append(f'-I{paths.WIN_ZLIB_INCLUDE_PATH}')
        return cflags

    @property
    def cxxflags(self) -> List[str]:
        cxxflags = super().cxxflags

        # Use -fuse-cxa-atexit to allow static TLS destructors.  This is needed for
        # clang-tools-extra/clangd/Context.cpp
        cxxflags.append('-fuse-cxa-atexit')

        # Explicitly add the path to libc++ headers.  We don't need to configure
        # options like visibility annotations, win32 threads etc. because the
        # __generated_config header in the patch captures all the options used when
        # building libc++.
        cxx_headers = BuilderRegistry.get('libcxx').install_dir / 'include' / 'c++' / 'v1'
        cxxflags.append(f'-I{cxx_headers}')

        return cxxflags


def build_runtimes(toolchain, args=None):
    SysrootsBuilder().build()

    CompilerRTBuilder().build()
    # 32-bit host crts are not needed for Darwin
    if hosts.build_host().is_linux:
        CompilerRTHostI386Builder().build()
    LibOMPBuilder().build()
    if BUILD_LLDB:
        LldbServerBuilder().build()
    # Bug: http://b/64037266. `strtod_l` is missing in NDK r15. This will break
    # libcxx build.
    # build_libcxx(toolchain, version)
    AsanMapFileBuilder().build()


def install_wrappers(llvm_install_path):
    wrapper_path = utils.out_path('llvm_android_wrapper')
    wrapper_build_script = utils.android_path('external', 'toolchain-utils',
                                              'compiler_wrapper', 'build.py')
    # Note: The build script automatically determines the architecture
    # based on the host.
    go_env = dict(os.environ)
    go_env['PATH'] = go_bin_dir() + ':' + go_env['PATH']
    utils.check_call([sys.executable, wrapper_build_script,
                      '--config=android',
                      '--use_ccache=false',
                      '--use_llvm_next=' + str(BUILD_LLVM_NEXT).lower(),
                      '--output_file=' + wrapper_path], env=go_env)

    bisect_path = utils.android_path('toolchain', 'llvm_android',
                                     'bisect_driver.py')
    bin_path = os.path.join(llvm_install_path, 'bin')
    clang_path = os.path.join(bin_path, 'clang')
    clangxx_path = os.path.join(bin_path, 'clang++')
    clang_tidy_path = os.path.join(bin_path, 'clang-tidy')

    # Rename clang and clang++ to clang.real and clang++.real.
    # clang and clang-tidy may already be moved by this script if we use a
    # prebuilt clang. So we only move them if clang.real and clang-tidy.real
    # doesn't exist.
    if not os.path.exists(clang_path + '.real'):
        shutil.move(clang_path, clang_path + '.real')
    if not os.path.exists(clang_tidy_path + '.real'):
        shutil.move(clang_tidy_path, clang_tidy_path + '.real')
    utils.remove(clang_path)
    utils.remove(clangxx_path)
    utils.remove(clang_tidy_path)
    utils.remove(clangxx_path + '.real')
    os.symlink('clang.real', clangxx_path + '.real')

    shutil.copy2(wrapper_path, clang_path)
    shutil.copy2(wrapper_path, clangxx_path)
    shutil.copy2(wrapper_path, clang_tidy_path)
    install_file(bisect_path, bin_path)


# Normalize host libraries (libLLVM, libclang, libc++, libc++abi) so that there
# is just one library, whose SONAME entry matches the actual name.
def normalize_llvm_host_libs(install_dir, host: hosts.Host, version):
    if host.is_linux:
        libs = {'libLLVM': 'libLLVM-{version}git.so',
                'libclang': 'libclang.so.{version}git',
                'libclang_cxx': 'libclang_cxx.so.{version}git',
                'libc++': 'libc++.so.{version}',
                'libc++abi': 'libc++abi.so.{version}'
               }
    else:
        libs = {'libc++': 'libc++.{version}.dylib',
                'libc++abi': 'libc++abi.{version}.dylib'
               }

    def getVersions(libname):
        if not libname.startswith('libc++'):
            return version.short_version(), version.major
        else:
            return '1.0', '1'

    libdir = os.path.join(install_dir, 'lib64')
    for libname, libformat in libs.items():
        short_version, major = getVersions(libname)

        soname_lib = os.path.join(libdir, libformat.format(version=major))
        if libname.startswith('libclang'):
            real_lib = soname_lib[:-3]
        else:
            real_lib = os.path.join(libdir, libformat.format(version=short_version))

        if libname not in ('libLLVM',):
            # Rename the library to match its SONAME
            if not os.path.isfile(real_lib):
                raise RuntimeError(real_lib + ' must be a regular file')
            if not os.path.islink(soname_lib):
                raise RuntimeError(soname_lib + ' must be a symlink')

            shutil.move(real_lib, soname_lib)

        # Retain only soname_lib and delete other files for this library.  We
        # still need libc++.so or libc++.dylib symlinks for a subsequent stage1
        # build using these prebuilts (where CMake tries to find C++ atomics
        # support) to succeed.
        libcxx_name = 'libc++.so' if host.is_linux else 'libc++.dylib'
        all_libs = [lib for lib in os.listdir(libdir) if
                    lib != libcxx_name and
                    not lib.endswith('.a') and # skip static host libraries
                    (lib.startswith(libname + '.') or # so libc++abi is ignored
                     lib.startswith(libname + '-'))]

        for lib in all_libs:
            lib = os.path.join(libdir, lib)
            if lib != soname_lib:
                remove(lib)


def install_license_files(install_dir):
    projects = (
        'llvm',
        'compiler-rt',
        'libcxx',
        'libcxxabi',
        'openmp',
        'clang',
        'clang-tools-extra',
        'lld',
    )

    # Get generic MODULE_LICENSE_* files from our android subdirectory.
    llvm_android_path = utils.android_path('toolchain', 'llvm_android')
    license_pattern = os.path.join(llvm_android_path, 'MODULE_LICENSE_*')
    for license_file in glob.glob(license_pattern):
        install_file(license_file, install_dir)

    # Fetch all the LICENSE.* files under our projects and append them into a
    # single NOTICE file for the resulting prebuilts.
    notices = []
    for project in projects:
        license_pattern = utils.llvm_path(project, 'LICENSE.*')
        for license_file in glob.glob(license_pattern):
            with open(license_file) as notice_file:
                notices.append(notice_file.read())
    with open(os.path.join(install_dir, 'NOTICE'), 'w') as notice_file:
        notice_file.write('\n'.join(notices))


def install_winpthreads(bin_dir, lib_dir):
    """Installs the winpthreads runtime to the Windows bin and lib directory."""
    lib_name = 'libwinpthread-1.dll'
    mingw_dir = utils.android_path(
        'prebuilts/gcc/linux-x86/host/x86_64-w64-mingw32-4.8',
        'x86_64-w64-mingw32')
    lib_path = os.path.join(mingw_dir, 'bin', lib_name)

    lib_install = os.path.join(lib_dir, lib_name)
    install_file(lib_path, lib_install)

    bin_install = os.path.join(bin_dir, lib_name)
    install_file(lib_path, bin_install)


def remove_static_libraries(static_lib_dir, necessary_libs=None):
    if not necessary_libs:
        necessary_libs = {}
    if os.path.isdir(static_lib_dir):
        lib_files = os.listdir(static_lib_dir)
        for lib_file in lib_files:
            if lib_file.endswith('.a') and lib_file not in necessary_libs:
                static_library = os.path.join(static_lib_dir, lib_file)
                remove(static_library)


def get_package_install_path(host: hosts.Host, package_name):
    return utils.out_path('install', host.os_tag, package_name)


def package_toolchain(build_dir, build_name, host: hosts.Host, dist_dir, strip=True, create_tar=True):
    package_name = 'clang-' + build_name
    version = extract_clang_version(build_dir)

    install_dir = get_package_install_path(host, package_name)
    install_host_dir = os.path.realpath(os.path.join(install_dir, '../'))

    # Remove any previously installed toolchain so it doesn't pollute the
    # build.
    if os.path.exists(install_host_dir):
        shutil.rmtree(install_host_dir)

    # First copy over the entire set of output objects.
    shutil.copytree(build_dir, install_dir, symlinks=True)

    ext = '.exe' if host.is_windows else ''
    shlib_ext = '.dll' if host.is_windows else '.so' if host.is_linux else '.dylib'

    # Next, we remove unnecessary binaries.
    necessary_bin_files = {
        'clang' + ext,
        'clang++' + ext,
        'clang-' + version.major_version() + ext,
        'clang-check' + ext,
        'clang-cl' + ext,
        'clang-format' + ext,
        'clang-tidy' + ext,
        'dsymutil' + ext,
        'git-clang-format',  # No extension here
        'ld.lld' + ext,
        'ld64.lld' + ext,
        'lld' + ext,
        'lld-link' + ext,
        'llvm-addr2line' + ext,
        'llvm-ar' + ext,
        'llvm-as' + ext,
        'llvm-cfi-verify' + ext,
        'llvm-config' + ext,
        'llvm-cov' + ext,
        'llvm-dis' + ext,
        'llvm-dwarfdump' + ext,
        'llvm-lib' + ext,
        'llvm-link' + ext,
        'llvm-modextract' + ext,
        'llvm-nm' + ext,
        'llvm-objcopy' + ext,
        'llvm-objdump' + ext,
        'llvm-profdata' + ext,
        'llvm-ranlib' + ext,
        'llvm-rc' + ext,
        'llvm-readelf' + ext,
        'llvm-readobj' + ext,
        'llvm-size' + ext,
        'llvm-strings' + ext,
        'llvm-strip' + ext,
        'llvm-symbolizer' + ext,
        'sancov' + ext,
        'sanstats' + ext,
        'scan-build' + ext,
        'scan-view' + ext,
    }

    if BUILD_LLDB:
        necessary_bin_files.update({
            'lldb-argdumper' + ext,
            'lldb' + ext,
        })

    if host.is_windows:
        windows_blacklist_bin_files = {
            'clang-' + version.major_version() + ext,
            'scan-build' + ext,
            'scan-view' + ext,
        }
        necessary_bin_files -= windows_blacklist_bin_files

    if BUILD_LLDB:
        install_lldb_deps(Path(install_dir), host)
        if host.is_windows:
            windows_additional_bin_files = {
                'liblldb' + shlib_ext,
                'python38' + shlib_ext
            }
            necessary_bin_files |= windows_additional_bin_files

    # scripts that should not be stripped
    script_bins = {
        'git-clang-format',
        'scan-build',
        'scan-view',
    }

    bin_dir = os.path.join(install_dir, 'bin')
    lib_dir = os.path.join(install_dir, 'lib64')

    for bin_filename in os.listdir(bin_dir):
        binary = os.path.join(bin_dir, bin_filename)
        if os.path.isfile(binary):
            if bin_filename not in necessary_bin_files:
                remove(binary)
            elif strip and bin_filename not in script_bins:
                utils.check_call(['strip', binary])

    # FIXME: check that all libs under lib64/clang/<version>/ are created.
    for necessary_bin_file in necessary_bin_files:
        if not os.path.isfile(os.path.join(bin_dir, necessary_bin_file)):
            raise RuntimeError('Did not find %s in %s' % (necessary_bin_file, bin_dir))

    necessary_lib_files = {
        'libc++.a',
        'libc++abi.a',
    }

    if host.is_windows:
        necessary_lib_files |= {
            'LLVMgold' + shlib_ext,
            'libwinpthread-1' + shlib_ext,
        }
        # For Windows, add other relevant libraries.
        install_winpthreads(bin_dir, lib_dir)

    # Remove unnecessary static libraries.
    remove_static_libraries(lib_dir, necessary_lib_files)

    if not host.is_windows:
        install_wrappers(install_dir)
        normalize_llvm_host_libs(install_dir, host, version)

    # Check necessary lib files exist.
    for necessary_lib_file in necessary_lib_files:
        if not os.path.isfile(os.path.join(lib_dir, necessary_lib_file)):
            raise RuntimeError('Did not find %s in %s' % (necessary_lib_file, lib_dir))

    # Next, we copy over stdatomic.h and bits/stdatomic.h from bionic.
    libc_include_path = utils.android_path('bionic', 'libc', 'include')
    resdir_top = os.path.join(lib_dir, 'clang')
    header_path = os.path.join(resdir_top, version.long_version(), 'include')

    stdatomic_path = utils.android_path(libc_include_path, 'stdatomic.h')
    install_file(stdatomic_path, header_path)

    bits_install_path = os.path.join(header_path, 'bits')
    if not os.path.isdir(bits_install_path):
        os.mkdir(bits_install_path)
    bits_stdatomic_path = utils.android_path(libc_include_path, 'bits', 'stdatomic.h')
    install_file(bits_stdatomic_path, bits_install_path)


    # Install license files as NOTICE in the toolchain install dir.
    install_license_files(install_dir)

    # Add an AndroidVersion.txt file.
    version_file_path = os.path.join(install_dir, 'AndroidVersion.txt')
    with open(version_file_path, 'w') as version_file:
        version_file.write('{}\n'.format(version.long_version()))
        svn_revision = android_version.get_svn_revision(BUILD_LLVM_NEXT)
        version_file.write('based on {}\n'.format(svn_revision))

    # Create RBE input files.
    if host.is_linux:
        with open(os.path.join(install_dir, 'bin', 'remote_toolchain_inputs'), 'w') as inputs_file:
            dependencies = ('clang\n'
                            'clang++\n'
                            'clang.real\n'
                            'clang++.real\n'
                            '../lib64/libc++.so.1\n'
                            'lld\n'
                            'ld64.lld\n'
                            'ld.lld\n'
                           )
            blacklist_dir = os.path.join('../', 'lib64', 'clang', version.long_version(), 'share\n')
            libs_dir = os.path.join('../', 'lib64', 'clang', version.long_version(), 'lib', 'linux\n')
            dependencies += (blacklist_dir + libs_dir)
            inputs_file.write(dependencies)

    # Package up the resulting trimmed install/ directory.
    if create_tar:
        tarball_name = package_name + '-' + host.os_tag
        package_path = os.path.join(dist_dir, tarball_name) + '.tar.bz2'
        logger().info('Packaging %s', package_path)
        args = ['tar', '-cjC', install_host_dir, '-f', package_path, package_name]
        utils.check_call(args)


def parse_args():
    known_components = ('linux', 'windows', 'lldb')
    known_components_str = ', '.join(known_components)

    # Simple argparse.Action to allow comma-separated values (e.g.
    # --option=val1,val2)
    class CommaSeparatedListAction(argparse.Action):
        def __call__(self, parser, namespace, values, option_string):
            for value in values.split(','):
                if value not in known_components:
                    error = '\'{}\' invalid.  Choose from {}'.format(
                        value, known_platforms)
                    raise argparse.ArgumentError(self, error)
            setattr(namespace, self.dest, values.split(','))


    # Parses and returns command line arguments.
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '-v',
        '--verbose',
        action='count',
        default=0,
        help='Increase log level. Defaults to logging.INFO.')
    parser.add_argument(
        '--build-name', default='dev', help='Release name for the package.')

    parser.add_argument(
        '--enable-assertions',
        action='store_true',
        default=False,
        help='Enable assertions (only affects stage2)')

    parser.add_argument(
        '--no-lto',
        action='store_true',
        default=False,
        help='Disable LTO to speed up build (only affects stage2)')

    parser.add_argument(
        '--debug',
        action='store_true',
        default=False,
        help='Build debuggable Clang and LLVM tools (only affects stage2)')

    parser.add_argument(
        '--build-instrumented',
        action='store_true',
        default=False,
        help='Build LLVM tools with PGO instrumentation')

    # Options to skip build or packaging (can't skip both, or the script does
    # nothing).
    build_package_group = parser.add_mutually_exclusive_group()
    build_package_group.add_argument(
        '--skip-build',
        '-sb',
        action='store_true',
        default=False,
        help='Skip the build, and only do the packaging step')
    build_package_group.add_argument(
        '--skip-package',
        '-sp',
        action='store_true',
        default=False,
        help='Skip the packaging, and only do the build step')

    parser.add_argument(
        '--no-strip',
        action='store_true',
        default=False,
        help='Don\'t strip binaries/libraries')

    build_group = parser.add_mutually_exclusive_group()
    build_group.add_argument(
        '--build',
        nargs='+',
        help='A list of builders to build. All builders not listed will be skipped.')
    build_group.add_argument(
        '--skip',
        nargs='+',
        help='A list of builders to skip. All builders not listed will be built.')

    # skip_runtimes is set to skip recompilation of libraries
    parser.add_argument(
        '--skip-runtimes',
        action='store_true',
        default=False,
        help='Skip the runtime libraries')

    parser.add_argument(
        '--no-build',
        action=CommaSeparatedListAction,
        default=list(),
        help='Don\'t build toolchain components or platforms.  Choices: ' + \
            known_components_str)

    parser.add_argument(
        '--check-pgo-profile',
        action='store_true',
        default=False,
        help='Fail if expected PGO profile doesn\'t exist')

    parser.add_argument(
        '--build-llvm-next',
        action='store_true',
        default=False,
        help='Build next LLVM revision (android_version.py:svn_revision_next)')

    return parser.parse_args()


def main():
    args = parse_args()
    if args.skip_build:
        # Skips all builds
        BuilderRegistry.add_filter(lambda name: False)
    elif args.skip:
        BuilderRegistry.add_skips(args.skip)
    elif args.build:
        BuilderRegistry.add_builds(args.build)
    do_runtimes = not args.skip_runtimes
    do_package = not args.skip_package
    do_strip = not args.no_strip
    do_strip_host_package = do_strip and not args.debug

    # TODO (Pirama): Avoid using global statement
    global BUILD_LLDB, BUILD_LLVM_NEXT
    BUILD_LLDB = 'lldb' not in args.no_build
    BUILD_LLVM_NEXT = args.build_llvm_next

    need_host = hosts.build_host().is_darwin or ('linux' not in args.no_build)
    need_windows = hosts.build_host().is_linux and ('windows' not in args.no_build)

    log_levels = [logging.INFO, logging.DEBUG]
    verbosity = min(args.verbose, len(log_levels) - 1)
    log_level = log_levels[verbosity]
    logging.basicConfig(level=log_level)

    logger().info('do_build=%r do_stage1=%r do_stage2=%r do_runtimes=%r do_package=%r need_windows=%r' %
                  (not args.skip_build, BuilderRegistry.should_build('stage1'), BuilderRegistry.should_build('stage2'),
                  do_runtimes, do_package, need_windows))

    # Clone sources to be built and apply patches.
    source_manager.setup_sources(source_dir=utils.llvm_path(),
                                 build_llvm_next=args.build_llvm_next)

    # Build the stage1 Clang for the build host
    instrumented = hosts.build_host().is_linux and args.build_instrumented

    # Windows libs are built with stage1 toolchain. llvm-config is required.
    stage1_build_llvm_tools = instrumented or \
                              need_windows or \
                              args.debug

    stage1 = Stage1Builder()
    stage1.build_name = args.build_name
    stage1.svn_revision = android_version.get_svn_revision(BUILD_LLVM_NEXT)
    stage1.build_llvm_tools = stage1_build_llvm_tools
    stage1.build_all_targets = args.debug or instrumented
    stage1.build()
    stage1_toolchain = toolchains.get_toolchain_from_builder(stage1)
    toolchains.set_runtime_toolchain(stage1_toolchain)
    stage1_install = str(stage1.install_dir)

    if BUILD_LLDB:
        SwigBuilder().build()
        if BuilderRegistry.should_build('stage2'):
            # libedit is not needed for windows lldb.
            LibEditBuilder().build()

    if need_host:
        profdata_filename = pgo_profdata_filename()
        profdata = pgo_profdata_file(profdata_filename)
        # Do not use PGO profiles if profdata file doesn't exist unless failure
        # is explicitly requested via --check-pgo-profile.
        if profdata is None and args.check_pgo_profile:
            raise RuntimeError('Profdata file does not exist for ' +
                               profdata_filename)

        stage2 = Stage2Builder()
        stage2.build_name = args.build_name
        stage2.svn_revision = android_version.get_svn_revision(BUILD_LLVM_NEXT)
        stage2.build_lldb = BUILD_LLDB
        stage2.debug_build = args.debug
        stage2.enable_assertions = args.enable_assertions
        stage2.lto = not args.no_lto
        stage2.build_instrumented = instrumented
        stage2.profdata_file = Path(profdata) if profdata else None

        # Annotate the version string if there is no profdata.
        if profdata is None:
            stage2.build_name += ', NO PGO PROFILE, '

        stage2.build()
        if not (stage2.build_instrumented or stage2.debug_build):
            stage2_toolchain = toolchains.get_toolchain_from_builder(stage2)
            toolchains.set_runtime_toolchain(stage2_toolchain)
        stage2_install = str(stage2.install_dir)

        if hosts.build_host().is_linux and do_runtimes:
            runtimes_toolchain = stage2_install
            if args.debug or instrumented:
                runtimes_toolchain = stage1_install
            build_runtimes(runtimes_toolchain, args)

    if need_windows:
        windows64_install = build_llvm_for_windows(
            enable_assertions=args.enable_assertions,
            build_name=args.build_name)

    dist_dir = ORIG_ENV.get('DIST_DIR', utils.out_path())
    if do_package and need_host:
        package_toolchain(
            stage2_install,
            args.build_name,
            hosts.build_host(),
            dist_dir,
            strip=do_strip_host_package)

    if do_package and need_windows:
        package_toolchain(
            windows64_install,
            args.build_name,
            hosts.Host.Windows,
            dist_dir,
            strip=do_strip)

    return 0


if __name__ == '__main__':
    main()
