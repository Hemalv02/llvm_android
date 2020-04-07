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
import textwrap
from typing import Dict, List, Optional, Set

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


def create_sysroots():
    # Construct the sysroots from scratch, since symlinks can't nest within
    # the right places (without altering source prebuilts).
    configs = [
        (hosts.Arch.ARM, 'arm-linux-androideabi'),
        (hosts.Arch.AARCH64, 'aarch64-linux-android'),
        (hosts.Arch.X86_64, 'x86_64-linux-android'),
        (hosts.Arch.I386, 'i686-linux-android'),
    ]

    # TODO(srhines): We destroy and recreate the sysroots each time, but this
    # could check for differences and only replace files if needed.
    sysroots_out = utils.out_path('sysroots')
    if os.path.exists(sysroots_out):
        shutil.rmtree(sysroots_out)
    check_create_path(sysroots_out)

    base_header_path = os.path.join(ndk_base(), 'sysroot', 'usr', 'include')
    for (arch, target) in configs:
        # Also create sysroots for each of platform and the NDK.
        for platform_or_ndk in ['platform', 'ndk']:
            platform = platform_or_ndk == 'platform'
            base_lib_path = \
                utils.android_path(ndk_base(), 'platforms',
                                   'android-' + str(android_api(arch, platform)))
            dest_usr = os.path.join(get_sysroot(arch, platform), 'usr')

            # Copy over usr/include.
            dest_usr_include = os.path.join(dest_usr, 'include')
            shutil.copytree(base_header_path, dest_usr_include, symlinks=True)

            # Copy over usr/include/asm.
            asm_headers = os.path.join(base_header_path, target, 'asm')
            dest_usr_include_asm = os.path.join(dest_usr_include, 'asm')
            shutil.copytree(asm_headers, dest_usr_include_asm, symlinks=True)

            # Copy over usr/lib.
            arch_lib_path = os.path.join(base_lib_path, 'arch-' + arch.ndk_arch,
                                         'usr', 'lib')
            dest_usr_lib = os.path.join(dest_usr, 'lib')
            shutil.copytree(arch_lib_path, dest_usr_lib, symlinks=True)

            # For only x86_64, we also need to copy over usr/lib64
            if arch == hosts.Arch.X86_64:
                arch_lib64_path = os.path.join(base_lib_path, 'arch-' + arch.ndk_arch,
                                               'usr', 'lib64')
                dest_usr_lib64 = os.path.join(dest_usr, 'lib64')
                shutil.copytree(arch_lib64_path, dest_usr_lib64, symlinks=True)

            if platform:
                # Create a stub library for the platform's libc++.
                platform_stubs = utils.out_path('platform_stubs', arch.ndk_arch)
                check_create_path(platform_stubs)
                libdir = dest_usr_lib64 if arch == hosts.Arch.X86_64 else dest_usr_lib
                with open(os.path.join(platform_stubs, 'libc++.c'), 'w') as f:
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
                utils.check_call([utils.out_path('stage2-install', 'bin', 'clang'),
                                  '--target=' + target,
                                  '-fuse-ld=lld', '-nostdlib', '-shared',
                                  '-Wl,-soname,libc++.so',
                                  '-o', os.path.join(libdir, 'libc++.so'),
                                  os.path.join(platform_stubs, 'libc++.c')])

                # For arm64 and x86_64, build static cxxabi library from
                # toolchain/libcxxabi and use it when building runtimes.  This
                # should affect all compiler-rt runtimes that use libcxxabi
                # (e.g. asan, hwasan, scudo, tsan, ubsan, xray).
                if arch not in (hosts.Arch.AARCH64, hosts.Arch.X86_64):
                    with open(os.path.join(libdir, 'libc++abi.so'), 'w') as f:
                        f.write('INPUT(-lc++)')
                else:
                    # We can build libcxxabi only after the sysroots are
                    # created.  Build it for the current arch and copy it to
                    # <libdir>.
                    out_dir = build_libcxxabi(utils.out_path('stage2-install'), arch)
                    out_path = utils.out_path(out_dir, 'lib64', 'libc++abi.a')
                    shutil.copy2(out_path, os.path.join(libdir))


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


# Base cmake options such as build type that are common across all invocations
def base_cmake_defines():
    defines = {}

    defines['CMAKE_BUILD_TYPE'] = 'Release'
    defines['LLVM_ENABLE_ASSERTIONS'] = 'OFF'
    # https://github.com/android-ndk/ndk/issues/574 - Don't depend on libtinfo.
    defines['LLVM_ENABLE_TERMINFO'] = 'OFF'
    defines['LLVM_ENABLE_THREADS'] = 'ON'
    defines['LLVM_USE_NEWPM'] = 'ON'
    defines['LLVM_LIBDIR_SUFFIX'] = '64'
    defines['LLVM_VERSION_PATCH'] = android_version.patch_level
    defines['CLANG_VERSION_PATCHLEVEL'] = android_version.patch_level
    defines['CLANG_REPOSITORY_STRING'] = 'https://android.googlesource.com/toolchain/llvm-project'
    defines['BUG_REPORT_URL'] = 'https://github.com/android-ndk/ndk/issues'

    if hosts.build_host().is_darwin:
        # This will be used to set -mmacosx-version-min. And helps to choose SDK.
        # To specify a SDK, set CMAKE_OSX_SYSROOT or SDKROOT environment variable.
        defines['CMAKE_OSX_DEPLOYMENT_TARGET'] = constants.MAC_MIN_VERSION

    # http://b/111885871 - Disable building xray because of MacOS issues.
    defines['COMPILER_RT_BUILD_XRAY'] = 'OFF'
    return defines


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


def build_asan_test(toolchain):
    # We can not build asan_test using current CMake building system. Since
    # those files are not used to build AOSP, we just simply touch them so that
    # we can pass the build checks.
    for arch in ('aarch64', 'arm', 'i686'):
        asan_test_path = os.path.join(toolchain, 'test', arch, 'bin')
        check_create_path(asan_test_path)
        asan_test_bin_path = os.path.join(asan_test_path, 'asan_test')
        open(asan_test_bin_path, 'w+').close()

def build_sanitizer_map_file(san, arch, lib_dir):
    lib_file = os.path.join(lib_dir, 'libclang_rt.{}-{}-android.so'.format(san, arch))
    map_file = os.path.join(lib_dir, 'libclang_rt.{}-{}-android.map.txt'.format(san, arch))
    mapfile.create_map_file(lib_file, map_file)

def build_sanitizer_map_files(toolchain, clang_version):
    lib_dir = os.path.join(toolchain,
                           clang_resource_dir(clang_version.long_version()))
    for arch in ('aarch64', 'arm', 'i686', 'x86_64'):
        build_sanitizer_map_file('asan', arch, lib_dir)
        build_sanitizer_map_file('ubsan_standalone', arch, lib_dir)
    build_sanitizer_map_file('hwasan', 'aarch64', lib_dir)

def create_hwasan_symlink(toolchain, clang_version):
    lib_dir = os.path.join(toolchain,
                           clang_resource_dir(clang_version.long_version()))
    symlink_path = lib_dir + 'libclang_rt.hwasan_static-aarch64-android.a'
    utils.remove(symlink_path)
    os.symlink('libclang_rt.hwasan-aarch64-android.a', symlink_path)

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


def build_crts(toolchain, clang_version, ndk_cxx=False):
    llvm_config = os.path.join(toolchain, 'bin', 'llvm-config')
    # Now build compiler-rt for each arch
    for (arch, llvm_triple, crt_defines,
         cflags) in cross_compile_configs(toolchain, platform=(not ndk_cxx)): # pylint: disable=not-an-iterable
        logger().info('Building compiler-rt for %s', arch.value)
        crt_path = utils.out_path('lib', 'clangrt-' + arch.value)
        crt_install = os.path.join(toolchain, 'lib64', 'clang',
                                   clang_version.long_version())
        if ndk_cxx:
            crt_path += '-ndk-cxx'
            crt_install = crt_path + '-install'

        crt_defines['ANDROID'] = '1'
        crt_defines['LLVM_CONFIG_PATH'] = llvm_config
        # FIXME: Disable WError build until upstream fixed the compiler-rt
        # personality routine warnings caused by r309226.
        # crt_defines['COMPILER_RT_ENABLE_WERROR'] = 'ON'

        # Skip implicit C++ headers and explicitly include C++ header paths.
        cflags.append('-nostdinc++')
        cflags.extend('-isystem ' + d for d in libcxx_header_dirs(ndk_cxx))

        cflags.append('-funwind-tables')

        crt_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
        crt_defines['CMAKE_ASM_FLAGS'] = ' '.join(cflags)
        crt_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)
        crt_defines['COMPILER_RT_TEST_COMPILER_CFLAGS'] = ' '.join(cflags)
        crt_defines['COMPILER_RT_TEST_TARGET_TRIPLE'] = llvm_triple
        crt_defines['COMPILER_RT_INCLUDE_TESTS'] = 'OFF'
        crt_defines['CMAKE_INSTALL_PREFIX'] = crt_install

        # Build libfuzzer separately.
        crt_defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'

        crt_defines['SANITIZER_CXX_ABI'] = 'libcxxabi'
        libs = []
        if arch == 'arm':
            libs += ['-latomic']
        if android_api(arch, platform=(not ndk_cxx)) < 21:
            libs += ['-landroid_support']
        crt_defines['SANITIZER_COMMON_LINK_LIBS'] = ' '.join(libs)
        if not ndk_cxx:
            crt_defines['COMPILER_RT_HWASAN_WITH_INTERCEPTORS'] = 'OFF'

        crt_defines.update(base_cmake_defines())

        crt_env = dict(ORIG_ENV)

        crt_cmake_path = utils.llvm_path('compiler-rt')
        rm_cmake_cache(crt_path)
        invoke_cmake(
            out_path=crt_path,
            defines=crt_defines,
            env=crt_env,
            cmake_path=crt_cmake_path)

        if ndk_cxx:
            src_dir = os.path.join(crt_install, 'lib', 'linux')
            dst_dir = os.path.join(toolchain, 'runtimes_ndk_cxx')
            check_create_path(dst_dir)
            for f in os.listdir(src_dir):
                shutil.copy2(os.path.join(src_dir, f), os.path.join(dst_dir, f))


def build_libfuzzers(toolchain, clang_version, ndk_cxx=False):
    llvm_config = os.path.join(toolchain, 'bin', 'llvm-config')

    for (arch, llvm_triple, libfuzzer_defines, cflags) in cross_compile_configs( # pylint: disable=not-an-iterable
            toolchain, platform=(not ndk_cxx)):
        logger().info('Building libfuzzer for %s (ndk_cxx? %s)', arch.value, ndk_cxx)

        libfuzzer_path = utils.out_path('lib', 'libfuzzer-' + arch.value)
        if ndk_cxx:
            libfuzzer_path += '-ndk-cxx'

        libfuzzer_defines['ANDROID'] = '1'
        libfuzzer_defines['LLVM_CONFIG_PATH'] = llvm_config

        # Skip implicit C++ headers and explicitly include C++ header paths.
        cflags.append('-nostdinc++')
        cflags.extend('-isystem ' + d for d in libcxx_header_dirs(ndk_cxx))

        libfuzzer_defines['CMAKE_ASM_FLAGS'] = ' '.join(cflags)
        libfuzzer_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
        libfuzzer_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)

        # lib/Fuzzer/CMakeLists.txt does not call cmake_minimum_required() to
        # set a minimum version.  Explicitly request a policy that'll pass
        # CMAKE_*_LINKER_FLAGS to the trycompile() step.
        libfuzzer_defines['CMAKE_POLICY_DEFAULT_CMP0056'] = 'NEW'

        libfuzzer_cmake_path = utils.llvm_path('compiler-rt')
        libfuzzer_env = dict(ORIG_ENV)
        rm_cmake_cache(libfuzzer_path)
        invoke_cmake(
            out_path=libfuzzer_path,
            defines=libfuzzer_defines,
            env=libfuzzer_env,
            cmake_path=libfuzzer_cmake_path,
            target='fuzzer',
            install=False)
        # We need to install libfuzzer manually.
        if arch == hosts.Arch.I386:
            sarch = 'i686'
        else:
            sarch = arch.value
        static_lib_filename = 'libclang_rt.fuzzer-' + sarch + '-android.a'
        static_lib = os.path.join(libfuzzer_path, 'lib', 'linux', static_lib_filename)
        triple_arch: Arch = hosts.Arch.from_triple(llvm_triple)

        # Install the fuzzer library to the old {arch}/libFuzzer.a path for
        # backwards compatibility.
        if ndk_cxx:
            lib_subdir = os.path.join('runtimes_ndk_cxx', triple_arch.value)
        else:
            lib_subdir = clang_resource_dir(clang_version.long_version(),
                                            triple_arch)
        lib_dir = os.path.join(toolchain, lib_subdir)

        check_create_path(lib_dir)
        shutil.copy2(static_lib, os.path.join(lib_dir, 'libFuzzer.a'))

        # Now install under the libclang_rt.fuzzer[...] name as well.
        if ndk_cxx:
            #  1. Under runtimes_ndk_cxx
            dst_dir = os.path.join(toolchain, 'runtimes_ndk_cxx')
            check_create_path(dst_dir)
            shutil.copy2(static_lib, os.path.join(dst_dir, static_lib_filename))
        else:
            #  2. Under lib64.
            libfuzzer_install = os.path.join(toolchain, 'lib64', 'clang',
                                             clang_version.long_version())
            libfuzzer_install = os.path.join(libfuzzer_install, "lib", "linux")
            check_create_path(libfuzzer_install)
            shutil.copy2(static_lib, os.path.join(libfuzzer_install, static_lib_filename))

    # Install libfuzzer headers.
    header_src = utils.llvm_path('compiler-rt', 'lib', 'fuzzer')
    header_dst = os.path.join(toolchain, 'prebuilt_include', 'llvm', 'lib',
                              'Fuzzer')
    check_create_path(header_dst)
    for f in os.listdir(header_src):
        if f.endswith('.h') or f.endswith('.def'):
            shutil.copy2(os.path.join(header_src, f), header_dst)


def build_libcxxabi(toolchain, build_arch: hosts.Arch):
    # TODO: Refactor cross_compile_configs to support per-arch queries in
    # addition to being a generator.
    for (arch, llvm_triple, defines, cflags) in \
         cross_compile_configs(toolchain, platform=True): # pylint: disable=not-an-iterable

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
        return out_path


def build_libomp(toolchain, clang_version, ndk_cxx=False, is_shared=False):

    for (arch, llvm_triple, libomp_defines, cflags) in cross_compile_configs( # pylint: disable=not-an-iterable
            toolchain, platform=(not ndk_cxx)):

        logger().info('Building libomp for %s (ndk_cxx? %s)', arch.value, ndk_cxx)
        # Skip implicit C++ headers and explicitly include C++ header paths.
        cflags.append('-nostdinc++')
        cflags.extend('-isystem ' + d for d in libcxx_header_dirs(ndk_cxx))

        cflags.append('-fPIC')

        libomp_path = utils.out_path('lib', 'libomp-' + arch.value)
        if ndk_cxx:
            libomp_path += '-ndk-cxx'
        libomp_path += '-' + ('shared' if is_shared else 'static')

        libomp_defines['ANDROID'] = '1'
        libomp_defines['CMAKE_BUILD_TYPE'] = 'Release'
        libomp_defines['CMAKE_ASM_FLAGS'] = ' '.join(cflags)
        libomp_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
        libomp_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)
        libomp_defines['OPENMP_ENABLE_LIBOMPTARGET'] = 'FALSE'
        libomp_defines['OPENMP_ENABLE_OMPT_TOOLS'] = 'FALSE'
        libomp_defines['LIBOMP_ENABLE_SHARED'] = 'TRUE' if is_shared else 'FALSE'

        # Minimum version for OpenMP's CMake is too low for the CMP0056 policy
        # to be ON by default.
        libomp_defines['CMAKE_POLICY_DEFAULT_CMP0056'] = 'NEW'

        libomp_defines.update(base_cmake_defines())

        libomp_cmake_path = utils.llvm_path('openmp')
        libomp_env = dict(ORIG_ENV)
        rm_cmake_cache(libomp_path)
        invoke_cmake(
            out_path=libomp_path,
            defines=libomp_defines,
            env=libomp_env,
            cmake_path=libomp_cmake_path,
            install=False)

        # We need to install libomp manually.
        libname = 'libomp.' + ('so' if is_shared else 'a')
        src_lib = os.path.join(libomp_path, 'runtime', 'src', libname)
        triple_arch = hosts.Arch.from_triple(llvm_triple)
        if ndk_cxx:
            dst_subdir = os.path.join('runtimes_ndk_cxx', triple_arch.value)
        else:
            dst_subdir = clang_resource_dir(clang_version.long_version(),
                                            triple_arch)
        dst_dir = os.path.join(toolchain, dst_subdir)

        check_create_path(dst_dir)
        shutil.copy2(src_lib, os.path.join(dst_dir, libname))


def build_lldb_server(toolchain, clang_version, ndk_cxx=False):
    llvm_config = os.path.join(toolchain, 'bin', 'llvm-config')
    for (arch, llvm_triple, lldb_defines,
         cflags) in cross_compile_configs(toolchain, platform=(not ndk_cxx),
                                          static=True): # pylint: disable=not-an-iterable

        logger().info('Building lldb for %s (ndk_cxx? %s)', arch.value, ndk_cxx)
        # Skip implicit C++ headers and explicitly include C++ header paths.
        cflags.append('-nostdinc++')
        cflags.extend('-isystem ' + d for d in libcxx_header_dirs(ndk_cxx))
        # The build system will add '-stdlib=libc++' automatically. Since we
        # have -nostdinc++ here, -stdlib is useless. Adds a flag to avoid the
        # warnings.
        cflags.append('-Wno-unused-command-line-argument')

        lldb_path = utils.out_path('lib', 'lldb-server-' + arch.value)
        if ndk_cxx:
            lldb_path += '-ndk-cxx'

        lldb_defines['ANDROID'] = '1'
        lldb_defines['LLVM_CONFIG_PATH'] = llvm_config

        lldb_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
        lldb_defines['CMAKE_ASM_FLAGS'] = ' '.join(cflags)
        lldb_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)

        lldb_defines.update(base_cmake_defines())

        # lldb depends on support libraries.
        lldb_defines['LLVM_ENABLE_PROJECTS'] = 'clang;lldb'

        lldb_defines['LLVM_ENABLE_LIBCXX'] = 'ON'
        lldb_defines['CMAKE_CROSSCOMPILING'] = 'True'
        lldb_defines['LLVM_TABLEGEN'] = os.path.join(toolchain, 'bin', 'llvm-tblgen')
        lldb_defines['CLANG_TABLEGEN'] = os.path.join(toolchain, '..', 'stage2', 'bin', 'clang-tblgen')
        lldb_defines['LLDB_TABLEGEN'] = os.path.join(toolchain, '..', 'stage2', 'bin', 'lldb-tblgen')
        lldb_defines['LLVM_DEFAULT_TARGET_TRIPLE'] = llvm_triple
        lldb_defines['LLVM_HOST_TRIPLE'] = llvm_triple
        lldb_defines['LLVM_TARGET_ARCH'] = arch.value

        lldb_defines['CMAKE_SYSTEM_NAME'] = 'Android'
        # Inhibit all of CMake's own NDK handling code.
        lldb_defines['CMAKE_SYSTEM_VERSION'] = '1'

        lldb_env = dict(ORIG_ENV)

        lldb_cmake_path = utils.llvm_path('llvm')
        invoke_cmake(
            out_path=lldb_path,
            defines=lldb_defines,
            env=lldb_env,
            cmake_path=lldb_cmake_path,
            target='lldb-server',
            install=False)

        # We need to install manually.
        libname = 'lldb-server'
        src_lib = os.path.join(lldb_path, 'bin', libname)
        triple_arch = hosts.Arch.from_triple(llvm_triple)
        if ndk_cxx:
            dst_subdir = os.path.join('runtimes_ndk_cxx', triple_arch.value)
        else:
            dst_subdir = clang_resource_dir(clang_version.long_version(),
                                            triple_arch)
        dst_dir = os.path.join(toolchain, dst_subdir)

        check_create_path(dst_dir)
        shutil.copy2(src_lib, os.path.join(dst_dir, libname))


def build_crts_host_i686(toolchain, clang_version):
    logger().info('Building compiler-rt for host-i686')

    llvm_config = os.path.join(toolchain, 'bin', 'llvm-config')

    crt_install = os.path.join(toolchain, 'lib64', 'clang',
                               clang_version.long_version())
    crt_cmake_path = utils.llvm_path('compiler-rt')

    cflags, ldflags = host_gcc_toolchain_flags(hosts.build_host(), is_32_bit=True)

    crt_defines = base_cmake_defines()
    crt_defines['CMAKE_C_COMPILER'] = os.path.join(toolchain, 'bin',
                                                   'clang')
    crt_defines['CMAKE_CXX_COMPILER'] = os.path.join(toolchain, 'bin',
                                                     'clang++')

    # compiler-rt/lib/gwp_asan uses PRIu64 and similar format-specifier macros.
    # Add __STDC_FORMAT_MACROS so their definition gets included from
    # inttypes.h.  This explicit flag is only needed here.  64-bit host runtimes
    # are built in stage1/stage2 and get it from the LLVM CMake configuration.
    # These are defined unconditionaly in bionic and newer glibc
    # (https://sourceware.org/git/gitweb.cgi?p=glibc.git;h=1ef74943ce2f114c78b215af57c2ccc72ccdb0b7)
    cflags.append('-D__STDC_FORMAT_MACROS')

    # Due to CMake and Clang oddities, we need to explicitly set
    # CMAKE_C_COMPILER_TARGET and use march=i686 in cflags below instead of
    # relying on auto-detection from the Compiler-rt CMake files.
    crt_defines['CMAKE_C_COMPILER_TARGET'] = 'i386-linux-gnu'

    crt_defines['CMAKE_SYSROOT'] = host_sysroot()

    cflags.append('--target=i386-linux-gnu')
    cflags.append('-march=i686')

    crt_defines['LLVM_CONFIG_PATH'] = llvm_config
    crt_defines['COMPILER_RT_INCLUDE_TESTS'] = 'ON'
    crt_defines['COMPILER_RT_ENABLE_WERROR'] = 'ON'
    crt_defines['CMAKE_INSTALL_PREFIX'] = crt_install
    crt_defines['SANITIZER_CXX_ABI'] = 'libstdc++'

    # Set the compiler and linker flags
    crt_defines['CMAKE_ASM_FLAGS'] = ' '.join(cflags)
    crt_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
    crt_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)

    crt_defines['CMAKE_EXE_LINKER_FLAGS'] = ' '.join(ldflags)
    crt_defines['CMAKE_SHARED_LINKER_FLAGS'] = ' '.join(ldflags)
    crt_defines['CMAKE_MODULE_LINKER_FLAGS'] = ' '.join(ldflags)

    crt_env = dict(ORIG_ENV)

    crt_path = utils.out_path('lib', 'clangrt-i386-host')
    rm_cmake_cache(crt_path)

    # Also remove the "stamps" created for the libcxx included in libfuzzer so
    # CMake runs the configure again (after the cmake caches are deleted in the
    # line above).
    utils.remove(os.path.join(crt_path, 'lib', 'fuzzer', 'libcxx_fuzzer_i386-stamps'))

    invoke_cmake(
        out_path=crt_path,
        defines=crt_defines,
        env=crt_env,
        cmake_path=crt_cmake_path)


def build_llvm(targets,
               build_dir,
               install_dir,
               build_name,
               extra_defines=None,
               extra_env=None):
    cmake_defines = base_cmake_defines()
    cmake_defines['CMAKE_INSTALL_PREFIX'] = install_dir
    cmake_defines['LLVM_TARGETS_TO_BUILD'] = targets
    cmake_defines['LLVM_BUILD_LLVM_DYLIB'] = 'ON'
    cmake_defines['CLANG_VENDOR'] = 'Android (' + build_name + ' based on ' + \
        android_version.get_svn_revision(BUILD_LLVM_NEXT) + ') '
    cmake_defines['LLVM_BINUTILS_INCDIR'] = utils.android_path(
        'toolchain/binutils/binutils-2.27/include')

    if extra_defines is not None:
        cmake_defines.update(extra_defines)

    env = dict(ORIG_ENV)
    if extra_env is not None:
        env.update(extra_env)

    invoke_cmake(
        out_path=build_dir,
        defines=cmake_defines,
        env=env,
        cmake_path=utils.llvm_path('llvm'))


def windows_cflags():
    cflags = ['--target=x86_64-pc-windows-gnu', '-D_LARGEFILE_SOURCE',
              '-D_FILE_OFFSET_BITS=64', '-D_WIN32_WINNT=0x0600', '-DWINVER=0x0600',
              '-D__MSVCRT_VERSION__=0x1400']

    return cflags


def build_llvm_for_windows(enable_assertions,
                           build_name):
    if WindowsToolchainBuilder.install_dir.exists():
        shutil.rmtree(WindowsToolchainBuilder.install_dir)

    # Build and install libcxxabi and libcxx and use them to build Clang.
    libcxxabi_builder = LibCxxAbiBuilder()
    libcxxabi_builder.enable_assertions = enable_assertions
    libcxxabi_builder.build()

    libcxx_builder = LibCxxBuilder()
    libcxx_builder.enable_assertions = enable_assertions
    libcxx_builder.build()

    win_builder = WindowsToolchainBuilder()
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


def get_shared_extra_defines():
    extra_defines = dict()
    extra_defines['LLVM_BUILD_RUNTIME'] = 'ON'
    extra_defines['LLVM_ENABLE_PROJECTS'] = 'clang;lld;libcxxabi;libcxx;compiler-rt'
    return extra_defines


class Stage1Builder(builders.LLVMBuilder):
    name: str = 'stage1'
    toolchain_name: str = 'prebuilt'
    install_dir: Path = paths.OUT_DIR / 'stage1-install'
    build_llvm_tools: bool = False
    build_all_targets: bool = False
    config: configs.Config = configs.host_config()

    @property
    def llvm_targets(self) -> Set[str]:
        if self.build_all_targets:
            return set(ANDROID_TARGETS.split(';'))
        else:
            return set(BASE_TARGETS.split(';'))

    @property
    def llvm_projects(self) -> Set[str]:
        return {'clang', 'lld', 'libcxxabi', 'libcxx', 'compiler-rt'}

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        # Point CMake to the libc++.so from the prebuilts.  Install an rpath
        # to prevent linking with the newly-built libc++.so
        ldflags.append(f'-Wl,-rpath,{self.toolchain.lib_dir}')
        return ldflags

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
        if self.target_os.is_linux:
            defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'
            defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'

        # Do not build compiler-rt for Darwin.  We don't ship host (or any
        # prebuilt) runtimes for Darwin anyway.  Attempting to build these will
        # fail compilation of lib/builtins/atomic_*.c that only get built for
        # Darwin and fail compilation due to us using the bionic version of
        # stdatomic.h.
        if self.target_os.is_darwin:
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

    @classmethod
    def built_toolchain(cls) -> toolchains.Toolchain:
        """The built (or skipped) toolchain."""
        return toolchains.build_toolchain_for_path(cls.install_dir)


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
    if host.is_linux:
        shutil.copy2(paths.get_libedit_lib(host), lib_dir)


class Stage2Builder(builders.LLVMBuilder):
    name: str = 'stage2'
    toolchain_name: str = 'stage1'
    install_dir: Path = paths.OUT_DIR / 'stage2-install'
    config: configs.Config = configs.host_config()
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
            resource_dir = self.toolchain.get_resource_dir()
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
        defines['LLVM_ENABLE_LIBCXX'] = 'ON'
        defines['SANITIZER_ALLOW_CXXABI'] = 'OFF'
        defines['OPENMP_ENABLE_OMPT_TOOLS'] = 'FALSE'
        defines['LIBOMP_ENABLE_SHARED'] = 'FALSE'

        if (self.lto and
                not self.target_os.is_darwin and
                not self.debug_build):
            defines['LLVM_ENABLE_LTO'] = 'Thin'

        # Build libFuzzer here to be exported for the host fuzzer builds. libFuzzer
        # is not currently supported on Darwin.
        if self.target_os.is_darwin:
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
        if self.target_os.is_linux:
            defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'
            defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'

        # Do not build compiler-rt for Darwin.  We don't ship host (or any
        # prebuilt) runtimes for Darwin anyway.  Attempting to build these will
        # fail compilation of lib/builtins/atomic_*.c that only get built for
        # Darwin and fail compilation due to us using the bionic version of
        # stdatomic.h.
        if self.target_os.is_darwin:
            defines['LLVM_BUILD_EXTERNAL_COMPILER_RT'] = 'ON'

        return defines


class LibCxxBaseBuilder(builders.CMakeBuilder):
    toolchain_name: str = 'stage1'
    config: configs.Config = configs.WindowsConfig()
    install_dir: Path = paths.OUT_DIR / 'windows-x86-64-install'
    remove_cmake_cache: bool = True
    enable_assertions: bool = False

    @property
    def output_path(self) -> Path:
        return paths.OUT_DIR / 'lib' / (f'{self.config.target_os.value}-{self.name}')

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines: Dict[str, str] = super().cmake_defines
        defines['LLVM_CONFIG_PATH'] = str(self.toolchain.path /
                                          'bin' / 'llvm-config')

        # To prevent cmake from checking libstdcxx version.
        defines['LLVM_ENABLE_LIBCXX'] = 'ON'

        # Build only the static library.
        defines[self.name.upper() + '_ENABLE_SHARED'] = 'OFF'

        if self.enable_assertions:
            defines[self.name.upper() + '_ENABLE_ASSERTIONS'] = 'ON'
        return defines


class LibCxxAbiBuilder(LibCxxBaseBuilder):
    name = 'libcxxabi'
    src_dir: Path = paths.LLVM_PATH / 'libcxxabi'
    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines: Dict[str, str] = super().cmake_defines
        defines['LIBCXXABI_ENABLE_NEW_DELETE_DEFINITIONS'] = 'OFF'
        defines['LIBCXXABI_LIBCXX_INCLUDES'] = str(paths.LLVM_PATH /'libcxx' / 'include')
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


class LibCxxBuilder(LibCxxBaseBuilder):
    name = 'libcxx'
    src_dir: Path = paths.LLVM_PATH / 'libcxx'
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
    install_dir: Path = paths.OUT_DIR / 'windows-x86-64-install'
    toolchain_name: str = 'stage1'
    config: configs.Config = configs.WindowsConfig()
    build_lldb: bool = True

    @property
    def llvm_targets(self) -> Set[str]:
        return set(ANDROID_TARGETS.split(';'))

    @property
    def llvm_projects(self) -> Set[str]:
        proj = {'clang', 'clang-tools-extra', 'lld'}
        if self.build_lldb:
            proj.add('lldb')
        return proj

    def _create_native_cmake_file(self) -> Path:
        # Write a NATIVE.cmake in windows_path that contains the compilers used
        # to build native tools such as llvm-tblgen and llvm-config.  This is
        # used below via the CMake variable CROSS_TOOLCHAIN_FLAGS_NATIVE.
        native_projects = 'clang' if not self.build_lldb else 'clang;lldb'
        native_cmake_text: List[str] = [
            f'set(CMAKE_C_COMPILER {self.toolchain.cc})',
            f'set(CMAKE_CXX_COMPILER {self.toolchain.cxx})',
            f'set(LLVM_ENABLE_PROJECTS "{native_projects}" CACHE STRING "" FORCE)',
        ]
        if self.build_lldb:
            native_cmake_text.extend([
                'set(LLDB_ENABLE_PYTHON "OFF" CACHE STRING "" FORCE)',
                'set(LLDB_ENABLE_CURSES "OFF" CACHE STRING "" FORCE)',
                'set(LLDB_ENABLE_LIBEDIT "OFF" CACHE STRING "" FORCE)',
                # TODO: Remove the following on or after r380035.
                'set(LLDB_DISABLE_PYTHON "ON" CACHE STRING "" FORCE)',
                'set(LLDB_DISABLE_CURSES "ON" CACHE STRING "" FORCE)',
                'set(LLDB_DISABLE_LIBEDIT "ON" CACHE STRING "" FORCE)'])

        self.output_path.mkdir(parents=True, exist_ok=True)

        native_cmake_file_path = self.output_path / 'NATIVE.cmake'
        with native_cmake_file_path.open('w') as native_cmake_file:
            native_cmake_file.write('\n'.join(native_cmake_text))

        return native_cmake_file_path

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

        # Set CMake path, toolchain file for native compilation (to build tablegen
        # etc).  Also disable libfuzzer build during native compilation.
        native_flags = [
            f'-DCMAKE_MAKE_PROGRAM={paths.NINJA_BIN_PATH}',
            '-DCOMPILER_RT_BUILD_LIBFUZZER=OFF',
            f'-DCMAKE_TOOLCHAIN_FILE={self._create_native_cmake_file()}',
            '-DLLVM_ENABLE_LIBCXX=ON',
            '-DCMAKE_BUILD_WITH_INSTALL_RPATH=TRUE',
            f'-DCMAKE_INSTALL_RPATH={self.toolchain.lib_dir}',
        ]
        defines['CROSS_TOOLCHAIN_FLAGS_NATIVE'] = ';'.join(native_flags)
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
    if args is not None and args.skip_sysroots:
        logger().info('Skip libcxxabi and other sysroot libraries')
    else:
        create_sysroots()
    version = extract_clang_version(toolchain)
    if args is not None and args.skip_compiler_rt:
        logger().info('Skip compiler-rt')
    else:
        build_crts(toolchain, version)
        build_crts(toolchain, version, ndk_cxx=True)
        # 32-bit host crts are not needed for Darwin
        if hosts.build_host().is_linux:
            build_crts_host_i686(toolchain, version)
    if args is not None and args.skip_libfuzzers:
        logger().info('Skip libfuzzers')
    else:
        build_libfuzzers(toolchain, version)
        build_libfuzzers(toolchain, version, ndk_cxx=True)
    if args is not None and args.skip_libomp:
        logger().info('Skip libomp')
    else:
        build_libomp(toolchain, version)
        build_libomp(toolchain, version, ndk_cxx=True)
        build_libomp(toolchain, version, ndk_cxx=True, is_shared=True)
    if BUILD_LLDB:
        build_lldb_server(toolchain, version, ndk_cxx=True)
    else:
        logger().info('Skip lldb server')
    # Bug: http://b/64037266. `strtod_l` is missing in NDK r15. This will break
    # libcxx build.
    # build_libcxx(toolchain, version)
    if args is not None and args.skip_asan:
        logger().info('Skip asan test, map, symlink')
    else:
        build_asan_test(toolchain)
        build_sanitizer_map_files(toolchain, version)
        create_hwasan_symlink(toolchain, version)

def install_wrappers(llvm_install_path):
    def _instantiate_wrapper(wrapper_path):
        wrapper_template = utils.android_path('toolchain', 'llvm_android',
                                              'compiler_wrapper.py.in')

        append_flags = '\'-Wno-error\'' if BUILD_LLVM_NEXT else ''
        with open(wrapper_template, 'r') as infile:
            wrapper_txt = string.Template(infile.read()).substitute(
                {'append_flags': append_flags})

        with open(wrapper_path, 'w') as outfile:
            outfile.write(wrapper_txt)
        # Also preserve permission bits etc.
        shutil.copystat(wrapper_template, wrapper_path)

    wrapper_path = utils.out_path('compiler_wrapper.py')
    _instantiate_wrapper(wrapper_path)

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

    # skip_stage1 is set to quickly reproduce stage2 failures
    parser.add_argument(
        '--skip-stage1',
        action='store_true',
        default=False,
        help='Skip the stage1 build')

    # skip_stage2 is set to quickly reproduce runtime failures
    parser.add_argument(
        '--skip-stage2',
        action='store_true',
        default=False,
        help='Skip the stage2 build')

    # skip_runtimes is set to skip recompilation of libraries
    parser.add_argument(
        '--skip-runtimes',
        action='store_true',
        default=False,
        help='Skip the runtime libraries')

    # Finer controls to skip only some runtime libraries
    parser.add_argument(
        '--skip-sysroots',
        action='store_true',
        default=False,
        help='Skip the sysroot libraries')
    parser.add_argument(
        '--skip-compiler-rt',
        action='store_true',
        default=False,
        help='Skip the compiler-rt libraries, including libcxxabi')
    parser.add_argument(
        '--skip-libfuzzers',
        action='store_true',
        default=False,
        help='Skip the libfuzzer libraries')
    parser.add_argument(
        '--skip-libomp',
        action='store_true',
        default=False,
        help='Skip the libomp libraries')
    parser.add_argument(
        '--skip-asan',
        action='store_true',
        default=False,
        help='Skip the sanitizer libraries')

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
    do_build = not args.skip_build
    do_stage1 = do_build and not args.skip_stage1
    do_stage2 = do_build and not args.skip_stage2
    do_runtimes = not args.skip_runtimes
    do_package = not args.skip_package
    do_strip = not args.no_strip
    do_strip_host_package = do_strip and not args.debug

    # TODO (Pirama): Avoid using global statement
    global BUILD_LLDB, BUILD_LLVM_NEXT
    BUILD_LLDB = 'lldb' not in args.no_build
    BUILD_LLVM_NEXT = args.build_llvm_next

    need_host = hosts.build_host().is_darwin or ('linux' not in args.no_build)
    need_windows = hosts.build_host().is_linux and \
        ('windows' not in args.no_build)

    log_levels = [logging.INFO, logging.DEBUG]
    verbosity = min(args.verbose, len(log_levels) - 1)
    log_level = log_levels[verbosity]
    logging.basicConfig(level=log_level)

    logger().info('do_build=%r do_stage1=%r do_stage2=%r do_runtimes=%r do_package=%r need_windows=%r' %
                  (do_build, do_stage1, do_stage2, do_runtimes, do_package, need_windows))

    # Clone sources to be built and apply patches.
    source_manager.setup_sources(source_dir=utils.llvm_path(),
                                 build_llvm_next=args.build_llvm_next)

    # Build the stage1 Clang for the build host
    instrumented = hosts.build_host().is_linux and args.build_instrumented

    # Windows libs are built with stage1 toolchain. llvm-config is required.
    stage1_build_llvm_tools = instrumented or \
                              (do_build and need_windows) or \
                              args.debug

    stage1 = Stage1Builder()
    stage1.build_name = args.build_name
    stage1.svn_revision = android_version.get_svn_revision(BUILD_LLVM_NEXT)
    stage1.build_llvm_tools = stage1_build_llvm_tools
    stage1.build_all_targets = args.debug or instrumented
    if do_stage1:
        stage1.build()
    stage1_install = str(stage1.install_dir)

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
        if do_stage2:
            stage2.build()
        stage2_install = str(stage2.install_dir)

        if hosts.build_host().is_linux and do_runtimes:
            runtimes_toolchain = stage2_install
            if args.debug or instrumented:
                runtimes_toolchain = stage1_install
            build_runtimes(runtimes_toolchain, args)

    if need_windows and do_build:
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
