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
from typing import cast, List, Optional

import android_version
import base_builders
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
import win_sdk

ORIG_ENV = dict(os.environ)

# Remove GOMA from our environment for building anything from stage2 onwards,
# since it is using a non-GOMA compiler (from stage1) to do the compilation.
USE_GOMA_FOR_STAGE1 = False
if ('USE_GOMA' in ORIG_ENV) and (ORIG_ENV['USE_GOMA'] == 'true'):
    USE_GOMA_FOR_STAGE1 = True
    del ORIG_ENV['USE_GOMA']

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

    flags += ['-DCMAKE_MAKE_PROGRAM=' + str(paths.NINJA_BIN_PATH)]

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

    utils.check_call([paths.CMAKE_BIN_PATH] + flags, cwd=out_path, env=env)
    utils.check_call([paths.NINJA_BIN_PATH] + ninja_target, cwd=out_path, env=env)
    if install:
        utils.check_call([paths.NINJA_BIN_PATH, 'install'], cwd=out_path, env=env)


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


class SysrootsBuilder(base_builders.Builder):
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


def build_llvm_for_windows(enable_assertions: bool,
                           build_name: str,
                           swig_builder: Optional[builders.SwigBuilder]):
    config_list: List[configs.Config]
    if win_sdk.is_enabled():
        config_list = [configs.MSVCConfig()]
    else:
        config_list = [configs.MinGWConfig()]

    win_builder = builders.WindowsToolchainBuilder(config_list)
    if win_builder.install_dir.exists():
        shutil.rmtree(win_builder.install_dir)

    if not win_sdk.is_enabled():
        # Build and install libcxxabi and libcxx and use them to build Clang.
        libcxx_builder = builders.LibCxxBuilder(config_list)
        libcxxabi_builder = builders.LibCxxAbiBuilder(config_list)
        libcxxabi_builder.enable_assertions = enable_assertions
        libcxxabi_builder.build()

        libcxx_builder.libcxx_abi_path = libcxxabi_builder.install_dir
        libcxx_builder.enable_assertions = enable_assertions
        libcxx_builder.build()
        win_builder.libcxx_path = libcxx_builder.install_dir

    win_builder.build_lldb = BUILD_LLDB
    lldb_deps_to_install: List[Path] = []
    if BUILD_LLDB:
        assert swig_builder is not None
        win_builder.libedit = None
        win_builder.swig_executable = swig_builder.install_dir / 'bin' / 'swig'

        xz_builder = builders.XzBuilder(config_list)
        xz_builder.build()
        win_builder.liblzma = xz_builder

        libxml2_builder = builders.LibXml2Builder(config_list)
        libxml2_builder.build()
        win_builder.libxml2 = libxml2_builder
        lldb_deps_to_install.append(libxml2_builder.install_library)

    win_builder.build_name = build_name
    win_builder.svn_revision = android_version.get_svn_revision(BUILD_LLVM_NEXT)
    win_builder.enable_assertions = enable_assertions
    win_builder.build()

    return (win_builder.install_dir, lldb_deps_to_install)


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


def install_lldb_deps(install_dir: Path, host: hosts.Host, lldb_deps: List[Path]):
    lib_dir = install_dir / ('bin' if host.is_windows else 'lib64')
    check_create_path(lib_dir)

    python_prebuilt_dir: Path = paths.get_python_dir(host)
    python_dest_dir: Path = install_dir / 'python3'
    shutil.copytree(python_prebuilt_dir, python_dest_dir, symlinks=True,
                    ignore=shutil.ignore_patterns('*.pyc', '__pycache__', 'Android.bp',
                                                  '.git', '.gitignore'))

    py_lib = paths.get_python_dynamic_lib(host).relative_to(python_prebuilt_dir)
    dest_py_lib = python_dest_dir / py_lib
    py_lib_rel = os.path.relpath(dest_py_lib, lib_dir)
    os.symlink(py_lib_rel, lib_dir / py_lib.name)

    for lldb_dep in lldb_deps:
        shutil.copy(lldb_dep, lib_dir)


def build_runtimes(toolchain, args=None):
    SysrootsBuilder().build()

    builders.CompilerRTBuilder().build()
    # 32-bit host crts are not needed for Darwin
    if hosts.build_host().is_linux:
        builders.CompilerRTHostI386Builder().build()
    builders.LibOMPBuilder().build()
    if BUILD_LLDB:
        builders.LldbServerBuilder().build()
    # Bug: http://b/64037266. `strtod_l` is missing in NDK r15. This will break
    # libcxx build.
    # build_libcxx(toolchain, version)
    builders.AsanMapFileBuilder().build()


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


def package_toolchain(build_dir, build_name, host: hosts.Host, dist_dir,
                      lldb_deps_to_install: List[Path], strip=True, create_tar=True):
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
    script_ext = '.cmd' if host.is_windows else '.sh'
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
            'lldb' + script_ext,
        })

    if host.is_windows:
        windows_exclude_bin_files = {
            'clang-' + version.major_version() + ext,
            'scan-build' + ext,
            'scan-view' + ext,
        }
        necessary_bin_files -= windows_exclude_bin_files

    if BUILD_LLDB:
        install_lldb_deps(Path(install_dir), host, lldb_deps_to_install)
        if host.is_windows:
            necessary_bin_files |= {
                'liblldb' + shlib_ext,
                'python38' + shlib_ext
            }
            necessary_bin_files |= set(path.name for path in lldb_deps_to_install)

    # scripts that should not be stripped
    script_bins = {
        'git-clang-format',
        'scan-build',
        'scan-view',
        'lldb' + script_ext,
    }

    bin_dir = os.path.join(install_dir, 'bin')
    lib_dir = os.path.join(install_dir, 'lib64')
    strip_cmd = toolchains.get_runtime_toolchain().strip

    for bin_filename in os.listdir(bin_dir):
        binary = os.path.join(bin_dir, bin_filename)
        if os.path.isfile(binary):
            if bin_filename not in necessary_bin_files:
                remove(binary)
            elif strip and bin_filename not in script_bins:
                # Strip all non-global symbols and debug info.
                # These specific flags prevent Darwin executables from being
                # stripped of additional global symbols that might be used
                # by plugins.
                utils.check_call([strip_cmd, '-S', '-x', binary])

    # FIXME: check that all libs under lib64/clang/<version>/ are created.
    for necessary_bin_file in necessary_bin_files:
        if not os.path.isfile(os.path.join(bin_dir, necessary_bin_file)):
            raise RuntimeError('Did not find %s in %s' % (necessary_bin_file, bin_dir))

    necessary_lib_files = set()
    if not (host.is_windows and win_sdk.is_enabled()):
        necessary_lib_files |= {
            'libc++.a',
            'libc++abi.a',
        }

    if host.is_windows:
        necessary_lib_files.add('LLVMgold' + shlib_ext)

    if host.is_windows and not win_sdk.is_enabled():
        necessary_lib_files.add('libwinpthread-1' + shlib_ext)
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
                            'clang-tidy\n'
                            'clang-tidy.real\n'
                            '../lib64/libc++.so.1\n'
                            'lld\n'
                            'ld64.lld\n'
                            'ld.lld\n'
                           )
            exclude_dir = os.path.join('../', 'lib64', 'clang', version.long_version(), 'share\n')
            libs_dir = os.path.join('../', 'lib64', 'clang', version.long_version(), 'lib', 'linux\n')
            dependencies += (exclude_dir + libs_dir)
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
        '--build-llvm-next',
        action='store_true',
        default=False,
        help='Build next LLVM revision (android_version.py:svn_revision_next)')

    parser.add_argument(
        '--windows-sdk',
        help='Path to a Windows SDK. If set, it will be used instead of MinGW.'
    )

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

    stage1 = builders.Stage1Builder()
    stage1.build_name = args.build_name
    stage1.svn_revision = android_version.get_svn_revision(BUILD_LLVM_NEXT)
    # Build lldb for lldb-tblgen. It will be used to build lldb-server and windows lldb.
    stage1.build_lldb = BUILD_LLDB
    stage1.build_android_targets = args.debug or instrumented
    stage1.use_goma_for_stage1 = USE_GOMA_FOR_STAGE1
    stage1.build()
    stage1_toolchain = toolchains.get_toolchain_from_builder(stage1)
    toolchains.set_runtime_toolchain(stage1_toolchain)
    stage1_install = str(stage1.install_dir)

    if BUILD_LLDB:
        # Swig is needed for both host and windows lldb.
        swig_builder = builders.SwigBuilder()
        swig_builder.build()
    else:
        swig_builder = None

    if need_host:
        profdata_filename = pgo_profdata_filename()
        profdata = pgo_profdata_file(profdata_filename)

        stage2 = builders.Stage2Builder()
        stage2.build_name = args.build_name
        stage2.svn_revision = android_version.get_svn_revision(BUILD_LLVM_NEXT)
        stage2.debug_build = args.debug
        stage2.enable_assertions = args.enable_assertions
        stage2.lto = not args.no_lto
        stage2.build_instrumented = instrumented
        stage2.profdata_file = Path(profdata) if profdata else None

        stage2.build_lldb = BUILD_LLDB
        if BUILD_LLDB:
            stage2.swig_executable = swig_builder.install_dir / 'bin' / 'swig'

            xz_builder = builders.XzBuilder()
            xz_builder.build()
            stage2.liblzma = xz_builder

            libxml2_builder = builders.LibXml2Builder()
            libxml2_builder.build()
            stage2.libxml2_path = libxml2_builder

            libedit_builder = builders.LibEditBuilder()
            libedit_builder.build()
            stage2.libedit = libedit_builder

            lldb_deps: List[Path] = [
                libxml2_builder.install_library,
                libedit_builder.install_library,
            ]

        # Annotate the version string if there is no profdata.
        if profdata is None:
            stage2.build_name += ', NO PGO PROFILE'
        # Annotate the version string if this is an llvm-next build.
        if BUILD_LLVM_NEXT:
            stage2.build_name += ', ANDROID_LLVM_NEXT'

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
        if args.windows_sdk:
            win_sdk.set_path(Path(args.windows_sdk))
        windows64_install, win_lldb_deps = build_llvm_for_windows(
            enable_assertions=args.enable_assertions,
            build_name=args.build_name,
            swig_builder=swig_builder)

    dist_dir = ORIG_ENV.get('DIST_DIR', utils.out_path())
    if do_package and need_host:
        package_toolchain(
            stage2_install,
            args.build_name,
            hosts.build_host(),
            dist_dir,
            lldb_deps,
            strip=do_strip_host_package)

    if do_package and need_windows:
        package_toolchain(
            windows64_install,
            args.build_name,
            hosts.Host.Windows,
            dist_dir,
            win_lldb_deps,
            strip=do_strip)

    return 0


if __name__ == '__main__':
    main()
