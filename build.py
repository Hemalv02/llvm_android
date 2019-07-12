#!/usr/bin/env python
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
# pylint: disable=not-callable, relative-import, line-too-long, no-else-return

import argparse
import datetime
import glob
import logging
import os
import shutil
import string
import subprocess
import textwrap
import utils

import android_version
from version import Version

import mapfile

ORIG_ENV = dict(os.environ)

# Remove GOMA from our environment for building anything from stage2 onwards,
# since it is using a non-GOMA compiler (from stage1) to do the compilation.
USE_GOMA_FOR_STAGE1 = False
if ('USE_GOMA' in ORIG_ENV) and (ORIG_ENV['USE_GOMA'] == 'true'):
    USE_GOMA_FOR_STAGE1 = True
    del ORIG_ENV['USE_GOMA']

STAGE2_TARGETS = 'AArch64;ARM;BPF;X86'


def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


def check_call(cmd, *args, **kwargs):
    """subprocess.check_call with logging."""
    logger().info('check_call:%s %s',
                  datetime.datetime.now().strftime("%H:%M:%S"),
                  subprocess.list2cmdline(cmd))
    subprocess.check_call(cmd, *args, **kwargs)


def check_output(cmd, *args, **kwargs):
    """subprocess.check_output with logging."""
    logger().info('check_output:%s %s',
                  datetime.datetime.now().strftime("%H:%M:%S"),
                  subprocess.list2cmdline(cmd))
    return subprocess.check_output(cmd, *args, **kwargs)


def install_file(src, dst):
    """Proxy for shutil.copy2 with logging and dry-run support."""
    logger().info('copy %s %s', src, dst)
    shutil.copy2(src, dst)


def remove(path):
    """Proxy for os.remove with logging."""
    logger().debug('remove %s', path)
    os.remove(path)


def extract_clang_version(clang_install):
    version_file = os.path.join(clang_install, 'include', 'clang', 'Basic',
                                'Version.inc')
    return Version(version_file)


def extract_clang_long_version(clang_install):
    return extract_clang_version(clang_install).long_version()


def pgo_profdata_filename():
    base_revision = android_version.svn_revision.rstrip(string.ascii_lowercase)
    return '%s.profdata' % base_revision

def pgo_profdata_file(profdata_file):
    profile = utils.android_path('prebuilts', 'clang', 'host', 'linux-x86',
                                 'profiles', profdata_file)
    return profile if os.path.exists(profile) else None


def ndk_base():
    ndk_version = 'r20'
    return utils.android_path('toolchain/prebuilts/ndk', ndk_version)


def android_api(arch, platform=False):
    if platform:
        return 29
    elif arch in ['arm', 'i386', 'x86']:
        return 16
    else:
        return 21


def ndk_path(arch, platform=False):
    platform_level = 'android-' + str(android_api(arch, platform))
    return os.path.join(ndk_base(), 'platforms', platform_level)


def ndk_libcxx_headers():
    return os.path.join(ndk_base(), 'sources', 'cxx-stl', 'llvm-libc++',
                        'include')


def ndk_libcxxabi_headers():
    return os.path.join(ndk_base(), 'sources', 'cxx-stl', 'llvm-libc++abi',
                        'include')


def ndk_toolchain_lib(arch, toolchain_root, host_tag):
    toolchain_lib = os.path.join(ndk_base(), 'toolchains', toolchain_root,
                                 'prebuilt', 'linux-x86_64', host_tag)
    if arch in ['arm', 'i386']:
        toolchain_lib = os.path.join(toolchain_lib, 'lib')
    else:
        toolchain_lib = os.path.join(toolchain_lib, 'lib64')
    return toolchain_lib


def support_headers():
    return os.path.join(ndk_base(), 'sources', 'android', 'support', 'include')


# This is the baseline stable version of Clang to start our stage-1 build.
def clang_prebuilt_version():
    return 'clang-r353983d'


def clang_prebuilt_base_dir():
    return utils.android_path('prebuilts/clang/host',
                              utils.build_os_type(), clang_prebuilt_version())


def clang_prebuilt_bin_dir():
    return utils.android_path(clang_prebuilt_base_dir(), 'bin')


def clang_prebuilt_lib_dir():
    return utils.android_path(clang_prebuilt_base_dir(), 'lib64')


def arch_from_triple(triple):
    arch = triple.split('-')[0]
    if arch == 'i686':
        arch = 'i386'
    return arch


def clang_resource_dir(version, arch):
    return os.path.join('lib64', 'clang', version, 'lib', 'linux', arch)


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


def cmake_prebuilt_bin_dir():
    return utils.android_path('prebuilts/cmake', utils.build_os_type(), 'bin')


def cmake_bin_path():
    return os.path.join(cmake_prebuilt_bin_dir(), 'cmake')


def ninja_bin_path():
    return os.path.join(cmake_prebuilt_bin_dir(), 'ninja')


def check_create_path(path):
    if not os.path.exists(path):
        os.makedirs(path)


def get_sysroot(arch, platform=False):
    sysroots = utils.out_path('sysroots')
    platform_or_ndk = 'platform' if platform else 'ndk'
    return os.path.join(sysroots, platform_or_ndk, arch)


def debug_prefix_flag():
    return '-fdebug-prefix-map={}='.format(utils.android_path())


def create_sysroots():
    # Construct the sysroots from scratch, since symlinks can't nest within
    # the right places (without altering source prebuilts).
    configs = [
        ('arm', 'arm-linux-androideabi'),
        ('arm64', 'aarch64-linux-android'),
        ('x86_64', 'x86_64-linux-android'),
        ('x86', 'i686-linux-android'),
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
            arch_lib_path = os.path.join(base_lib_path, 'arch-' + arch,
                                         'usr', 'lib')
            dest_usr_lib = os.path.join(dest_usr, 'lib')
            shutil.copytree(arch_lib_path, dest_usr_lib, symlinks=True)

            # For only x86_64, we also need to copy over usr/lib64
            if arch == 'x86_64':
                arch_lib64_path = os.path.join(base_lib_path, 'arch-' + arch,
                                               'usr', 'lib64')
                dest_usr_lib64 = os.path.join(dest_usr, 'lib64')
                shutil.copytree(arch_lib64_path, dest_usr_lib64, symlinks=True)

            if platform:
                # Create a stub library for the platform's libc++.
                platform_stubs = utils.out_path('platform_stubs', arch)
                check_create_path(platform_stubs)
                libdir = dest_usr_lib64 if arch == 'x86_64' else dest_usr_lib
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
                check_call([utils.out_path('stage2-install', 'bin', 'clang'),
                            '--target=' + target,
                            '-fuse-ld=lld', '-nostdlib', '-shared',
                            '-Wl,-soname,libc++.so',
                            '-o', os.path.join(libdir, 'libc++.so'),
                            os.path.join(platform_stubs, 'libc++.c')])

                # For arm64 and x86_64, build static cxxabi library from
                # toolchain/libcxxabi and use it when building runtimes.  This
                # should affect all compiler-rt runtimes that use libcxxabi
                # (e.g. asan, hwasan, scudo, tsan, ubsan, xray).
                if arch not in ('arm64', 'x86_64'):
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

    # http://b/111885871 - Disable building xray because of MacOS issues.
    defines['COMPILER_RT_BUILD_XRAY'] = 'OFF'
    return defines


def invoke_cmake(out_path, defines, env, cmake_path, target=None, install=True):
    flags = ['-G', 'Ninja']

    # Specify CMAKE_PREFIX_PATH so 'cmake -G Ninja ...' can find the ninja
    # executable.
    flags += ['-DCMAKE_PREFIX_PATH=' + cmake_prebuilt_bin_dir()]

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

    check_call([cmake_bin_path()] + flags, cwd=out_path, env=env)
    check_call([ninja_bin_path()] + ninja_target, cwd=out_path, env=env)
    if install:
        check_call([ninja_bin_path(), 'install'], cwd=out_path, env=env)


def cross_compile_configs(stage2_install, platform=False):
    configs = [
        ('arm', 'arm', 'arm/arm-linux-androideabi-4.9/arm-linux-androideabi',
         'arm-linux-android', '-march=armv7-a'),
        ('aarch64', 'arm64',
         'aarch64/aarch64-linux-android-4.9/aarch64-linux-android',
         'aarch64-linux-android', ''),
        ('x86_64', 'x86_64',
         'x86/x86_64-linux-android-4.9/x86_64-linux-android',
         'x86_64-linux-android', ''),
        ('i386', 'x86', 'x86/x86_64-linux-android-4.9/x86_64-linux-android',
         'i686-linux-android', '-m32'),
    ]

    cc = os.path.join(stage2_install, 'bin', 'clang')
    cxx = os.path.join(stage2_install, 'bin', 'clang++')
    llvm_config = os.path.join(stage2_install, 'bin', 'llvm-config')

    for (arch, ndk_arch, toolchain_path, llvm_triple, extra_flags) in configs:
        toolchain_root = utils.android_path('prebuilts/gcc',
                                            utils.build_os_type())
        toolchain_bin = os.path.join(toolchain_root, toolchain_path, 'bin')
        sysroot = get_sysroot(ndk_arch, platform)

        defines = {}
        defines['CMAKE_C_COMPILER'] = cc
        defines['CMAKE_CXX_COMPILER'] = cxx
        defines['LLVM_CONFIG_PATH'] = llvm_config

        # Include the directory with libgcc.a to the linker search path.
        toolchain_builtins = os.path.join(
            toolchain_root, toolchain_path, '..', 'lib', 'gcc',
            os.path.basename(toolchain_path), '4.9.x')
        # The 32-bit libgcc.a is sometimes in a separate subdir
        if arch == 'i386':
            toolchain_builtins = os.path.join(toolchain_builtins, '32')

        if ndk_arch == 'arm':
            toolchain_lib = ndk_toolchain_lib(arch, 'arm-linux-androideabi-4.9',
                                              'arm-linux-androideabi')
        elif ndk_arch == 'x86' or ndk_arch == 'x86_64':
            toolchain_lib = ndk_toolchain_lib(arch, ndk_arch + '-4.9',
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
        ]
        if not platform:
            triple = 'arm-linux-androideabi' if ndk_arch == 'arm' else llvm_triple
            libcxx_libs = os.path.join(ndk_base(), 'toolchains', 'llvm',
                                       'prebuilt', 'linux-x86_64', 'sysroot',
                                       'usr', 'lib', triple)
            ldflags += ['-L', os.path.join(libcxx_libs, str(android_api(arch)))]
            ldflags += ['-L', libcxx_libs]

        defines['CMAKE_EXE_LINKER_FLAGS'] = ' '.join(ldflags)
        defines['CMAKE_SHARED_LINKER_FLAGS'] = ' '.join(ldflags)
        defines['CMAKE_MODULE_LINKER_FLAGS'] = ' '.join(ldflags)
        update_cmake_sysroot_flags(defines, sysroot)

        cflags = [
            debug_prefix_flag(),
            '--target=%s' % llvm_triple,
            '-B%s' % toolchain_bin,
            '-D__ANDROID_API__=' + str(android_api(arch, platform=platform)),
            '-ffunction-sections',
            '-fdata-sections',
            extra_flags,
        ]
        yield (arch, llvm_triple, defines, cflags)


def build_asan_test(stage2_install):
    # We can not build asan_test using current CMake building system. Since
    # those files are not used to build AOSP, we just simply touch them so that
    # we can pass the build checks.
    for arch in ('aarch64', 'arm', 'i686'):
        asan_test_path = os.path.join(stage2_install, 'test', arch, 'bin')
        check_create_path(asan_test_path)
        asan_test_bin_path = os.path.join(asan_test_path, 'asan_test')
        open(asan_test_bin_path, 'w+').close()

def build_sanitizer_map_file(san, arch, lib_dir):
    lib_file = os.path.join(lib_dir, 'libclang_rt.{}-{}-android.so'.format(san, arch))
    map_file = os.path.join(lib_dir, 'libclang_rt.{}-{}-android.map.txt'.format(san, arch))
    mapfile.create_map_file(lib_file, map_file)

def build_sanitizer_map_files(stage2_install, clang_version):
    lib_dir = os.path.join(stage2_install,
                           clang_resource_dir(clang_version.long_version(), ''))
    for arch in ('aarch64', 'arm', 'i686', 'x86_64'):
        build_sanitizer_map_file('asan', arch, lib_dir)
    build_sanitizer_map_file('hwasan', 'aarch64', lib_dir)

def create_hwasan_symlink(stage2_install, clang_version):
    lib_dir = os.path.join(stage2_install,
                           clang_resource_dir(clang_version.long_version(), ''))
    symlink_path = lib_dir + 'libclang_rt.hwasan_static-aarch64-android.a'
    utils.remove(symlink_path)
    os.symlink('libclang_rt.hwasan-aarch64-android.a', symlink_path)

def build_libcxx(stage2_install, clang_version):
    for (arch, llvm_triple, libcxx_defines,
         cflags) in cross_compile_configs(stage2_install): # pylint: disable=not-an-iterable
        logger().info('Building libcxx for %s', arch)
        libcxx_path = utils.out_path('lib', 'libcxx-' + arch)

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
                                            arch_from_triple(llvm_triple))
        libcxx_install = os.path.join(stage2_install, install_subdir)

        libcxx_libs = os.path.join(libcxx_path, 'lib')
        check_create_path(libcxx_install)
        for f in os.listdir(libcxx_libs):
            if f.startswith('libc++'):
                shutil.copy2(os.path.join(libcxx_libs, f), libcxx_install)


def build_crts(stage2_install, clang_version, ndk_cxx=False):
    llvm_config = os.path.join(stage2_install, 'bin', 'llvm-config')
    # Now build compiler-rt for each arch
    for (arch, llvm_triple, crt_defines,
         cflags) in cross_compile_configs(stage2_install, platform=(not ndk_cxx)): # pylint: disable=not-an-iterable
        logger().info('Building compiler-rt for %s', arch)
        crt_path = utils.out_path('lib', 'clangrt-' + arch)
        crt_install = os.path.join(stage2_install, 'lib64', 'clang',
                                   clang_version.long_version())
        if ndk_cxx:
            crt_path += '-ndk-cxx'
            crt_install = crt_path + '-install'

        crt_defines['ANDROID'] = '1'
        crt_defines['LLVM_CONFIG_PATH'] = llvm_config
        crt_defines['COMPILER_RT_INCLUDE_TESTS'] = 'ON'
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
            dst_dir = os.path.join(stage2_install, 'runtimes_ndk_cxx')
            check_create_path(dst_dir)
            for f in os.listdir(src_dir):
                shutil.copy2(os.path.join(src_dir, f), os.path.join(dst_dir, f))


def build_libfuzzers(stage2_install, clang_version, ndk_cxx=False):
    llvm_config = os.path.join(stage2_install, 'bin', 'llvm-config')

    for (arch, llvm_triple, libfuzzer_defines, cflags) in cross_compile_configs( # pylint: disable=not-an-iterable
            stage2_install, platform=(not ndk_cxx)):
        logger().info('Building libfuzzer for %s (ndk_cxx? %s)', arch, ndk_cxx)

        libfuzzer_path = utils.out_path('lib', 'libfuzzer-' + arch)
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
        sarch = arch
        if sarch == 'i386':
            sarch = 'i686'
        static_lib_filename = 'libclang_rt.fuzzer-' + sarch + '-android.a'
        static_lib = os.path.join(libfuzzer_path, 'lib', 'linux', static_lib_filename)
        triple_arch = arch_from_triple(llvm_triple)

        # Install the fuzzer library to the old {arch}/libFuzzer.a path for
        # backwards compatibility.
        if ndk_cxx:
            lib_subdir = os.path.join('runtimes_ndk_cxx', triple_arch)
        else:
            lib_subdir = clang_resource_dir(clang_version.long_version(),
                                            triple_arch)
        lib_dir = os.path.join(stage2_install, lib_subdir)

        check_create_path(lib_dir)
        shutil.copy2(static_lib, os.path.join(lib_dir, 'libFuzzer.a'))

        # Now install under the libclang_rt.fuzzer[...] name as well.
        if ndk_cxx:
            #  1. Under runtimes_ndk_cxx
            dst_dir = os.path.join(stage2_install, 'runtimes_ndk_cxx')
            check_create_path(dst_dir)
            shutil.copy2(static_lib, os.path.join(dst_dir, static_lib_filename))
        else:
            #  2. Under lib64.
            libfuzzer_install = os.path.join(stage2_install, 'lib64', 'clang',
                                             clang_version.long_version())
            libfuzzer_install = os.path.join(libfuzzer_install, "lib", "linux")
            check_create_path(libfuzzer_install)
            shutil.copy2(static_lib, os.path.join(libfuzzer_install, static_lib_filename))

    # Install libfuzzer headers.
    header_src = utils.llvm_path('compiler-rt', 'lib', 'fuzzer')
    header_dst = os.path.join(stage2_install, 'prebuilt_include', 'llvm', 'lib',
                              'Fuzzer')
    check_create_path(header_dst)
    for f in os.listdir(header_src):
        if f.endswith('.h') or f.endswith('.def'):
            shutil.copy2(os.path.join(header_src, f), header_dst)


def build_libcxxabi(stage2_install, build_arch):
    # Normalize arm64/aarch64
    if build_arch == 'arm64':
        build_arch = 'aarch64'

    # TODO: Refactor cross_compile_configs to support per-arch queries in
    # addition to being a generator.
    for (arch, llvm_triple, defines, cflags) in \
         cross_compile_configs(stage2_install, platform=True): # pylint: disable=not-an-iterable

        # Build only the requested arch.
        if arch != build_arch:
            continue

        logger().info('Building libcxxabi for %s', arch)
        defines['LIBCXXABI_LIBCXX_INCLUDES'] = utils.llvm_path('libcxx', 'include')
        defines['LIBCXXABI_ENABLE_SHARED'] = 'OFF'
        defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
        defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)

        out_path = utils.out_path('lib', 'libcxxabi-' + arch)
        if os.path.exists(out_path):
            utils.rm_tree(out_path)

        invoke_cmake(out_path=out_path,
                     defines=defines,
                     env=dict(ORIG_ENV),
                     cmake_path=utils.llvm_path('libcxxabi'),
                     install=False)
        return out_path


def build_libomp(stage2_install, clang_version, ndk_cxx=False, is_shared=False):

    for (arch, llvm_triple, libomp_defines, cflags) in cross_compile_configs( # pylint: disable=not-an-iterable
            stage2_install, platform=(not ndk_cxx)):

        logger().info('Building libomp for %s (ndk_cxx? %s)', arch, ndk_cxx)
        # Skip implicit C++ headers and explicitly include C++ header paths.
        cflags.append('-nostdinc++')
        cflags.extend('-isystem ' + d for d in libcxx_header_dirs(ndk_cxx))

        cflags.append('-fPIC')

        libomp_path = utils.out_path('lib', 'libomp-' + arch)
        if ndk_cxx:
            libomp_path += '-ndk-cxx'
        libomp_path += '-' + ('shared' if is_shared else 'static')

        libomp_defines['ANDROID'] = '1'
        libomp_defines['CMAKE_BUILD_TYPE'] = 'Release'
        libomp_defines['CMAKE_ASM_FLAGS'] = ' '.join(cflags)
        libomp_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
        libomp_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)
        libomp_defines['OPENMP_ENABLE_LIBOMPTARGET'] = 'FALSE'
        libomp_defines['LIBOMP_ENABLE_SHARED'] = 'TRUE' if is_shared else 'FALSE'

        # Minimum version for OpenMP's CMake is too low for the CMP0056 policy
        # to be ON by default.
        libomp_defines['CMAKE_POLICY_DEFAULT_CMP0056'] = 'NEW'

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
        triple_arch = arch_from_triple(llvm_triple)
        if ndk_cxx:
            dst_subdir = os.path.join('runtimes_ndk_cxx', triple_arch)
        else:
            dst_subdir = clang_resource_dir(clang_version.long_version(),
                                            triple_arch)
        dst_dir = os.path.join(stage2_install, dst_subdir)

        check_create_path(dst_dir)
        shutil.copy2(src_lib, os.path.join(dst_dir, libname))


def build_crts_host_i686(stage2_install, clang_version):
    logger().info('Building compiler-rt for host-i686')

    llvm_config = os.path.join(stage2_install, 'bin', 'llvm-config')

    crt_install = os.path.join(stage2_install, 'lib64', 'clang',
                               clang_version.long_version())
    crt_cmake_path = utils.llvm_path('compiler-rt')

    cflags, ldflags = host_gcc_toolchain_flags(utils.build_os_type(), is_32_bit=True)

    crt_defines = base_cmake_defines()
    crt_defines['CMAKE_C_COMPILER'] = os.path.join(stage2_install, 'bin',
                                                   'clang')
    crt_defines['CMAKE_CXX_COMPILER'] = os.path.join(stage2_install, 'bin',
                                                     'clang++')

    # Skip building runtimes for i386
    crt_defines['COMPILER_RT_DEFAULT_TARGET_ONLY'] = 'ON'

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

    crt_defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'

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
        android_version.svn_revision + ') '
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


def build_libs_for_windows(libname,
                           toolchain_dir,
                           enable_assertions,
                           install_dir):
    cflags, ldflags = host_gcc_toolchain_flags('windows-x86')

    cflags.extend(windows_cflags())

    cmake_defines = dict()
    cmake_defines['CMAKE_SYSTEM_NAME'] = 'Windows'
    cmake_defines['CMAKE_C_COMPILER'] = os.path.join(
        toolchain_dir, 'bin', 'clang')
    cmake_defines['CMAKE_CXX_COMPILER'] = os.path.join(
        toolchain_dir, 'bin', 'clang++')
    cmake_defines['LLVM_CONFIG_PATH'] = os.path.join(
        toolchain_dir, 'bin', 'llvm-config')
    # To prevent cmake from checking libstdcxx version.
    cmake_defines['LLVM_ENABLE_LIBCXX'] = 'ON'

    windows_sysroot = utils.android_path('prebuilts', 'gcc', 'linux-x86', 'host',
                                         'x86_64-w64-mingw32-4.8',
                                         'x86_64-w64-mingw32')
    update_cmake_sysroot_flags(cmake_defines, windows_sysroot)

    # Build only the static library.
    cmake_defines[libname.upper() + '_ENABLE_SHARED'] = 'OFF'

    if enable_assertions:
        cmake_defines[libname.upper() + '_ENABLE_ASSERTIONS'] = 'ON'

    if libname == 'libcxx':
        cmake_defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'
        cmake_defines['LIBCXX_CXX_ABI'] = 'libcxxabi'
        cmake_defines['LIBCXX_HAS_WIN32_THREAD_API'] = 'ON'

        # Use cxxabi header from the source directory since it gets installed
        # into install_dir only during libcxx's install step.  But use the
        # library from install_dir.
        cmake_defines['LIBCXX_CXX_ABI_INCLUDE_PATHS'] = utils.llvm_path('libcxxabi', 'include')
        cmake_defines['LIBCXX_CXX_ABI_LIBRARY_PATH'] = os.path.join(install_dir, 'lib64')

        # Disable libcxxabi visibility annotations since we're only building it
        # statically.
        cflags.append('-D_LIBCXXABI_DISABLE_VISIBILITY_ANNOTATIONS')

    elif libname == 'libcxxabi':
        cmake_defines['LIBCXXABI_ENABLE_NEW_DELETE_DEFINITIONS'] = 'OFF'
        cmake_defines['LIBCXXABI_LIBCXX_INCLUDES'] = utils.llvm_path('libcxx', 'include')

        # Disable libcxx visibility annotations and enable WIN32 threads.  These
        # are needed because the libcxxabi build happens before libcxx and uses
        # headers directly from the sources.
        cflags.append('-D_LIBCPP_DISABLE_VISIBILITY_ANNOTATIONS')
        cflags.append('-D_LIBCPP_HAS_THREAD_API_WIN32')

    cmake_defines['CMAKE_INSTALL_PREFIX'] = install_dir
    cmake_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
    cmake_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)
    cmake_defines['CMAKE_EXE_LINKER_FLAGS'] = ' '.join(ldflags)
    cmake_defines['CMAKE_SHARED_LINKER_FLAGS'] = ' '.join(ldflags)
    cmake_defines['CMAKE_MODULE_LINKER_FLAGS'] = ' '.join(ldflags)

    out_path = utils.out_path('lib', 'windows-' + libname)
    if os.path.exists(out_path):
        utils.rm_tree(out_path)

    invoke_cmake(out_path=out_path,
                 defines=cmake_defines,
                 env=dict(ORIG_ENV),
                 cmake_path=utils.llvm_path(libname),
                 install=True)


def build_llvm_for_windows(stage1_install,
                           targets,
                           enable_assertions,
                           build_dir,
                           install_dir,
                           build_name):

    # Build and install libcxxabi and libcxx and use them to build Clang.
    build_libs_for_windows('libcxxabi',
                           stage1_install,
                           enable_assertions,
                           install_dir)

    build_libs_for_windows('libcxx',
                           stage1_install,
                           enable_assertions,
                           install_dir)

    # Write a NATIVE.cmake in windows_path that contains the compilers used
    # to build native tools such as llvm-tblgen and llvm-config.  This is
    # used below via the CMake variable CROSS_TOOLCHAIN_FLAGS_NATIVE.
    cc = os.path.join(stage1_install, 'bin', 'clang')
    cxx = os.path.join(stage1_install, 'bin', 'clang++')
    check_create_path(build_dir)
    native_cmake_file_path = os.path.join(build_dir, 'NATIVE.cmake')
    native_cmake_text = ('set(CMAKE_C_COMPILER {cc})\n'
                         'set(CMAKE_CXX_COMPILER {cxx})\n'
                         'set(LLVM_ENABLE_PROJECTS "clang")\n').format(
                             cc=cc, cxx=cxx)

    with open(native_cmake_file_path, 'w') as native_cmake_file:
        native_cmake_file.write(native_cmake_text)

    # Extra cmake defines to use while building for Windows
    windows_extra_defines = dict()
    windows_extra_defines['CMAKE_C_COMPILER'] = cc
    windows_extra_defines['CMAKE_CXX_COMPILER'] = cxx
    windows_extra_defines['CMAKE_SYSTEM_NAME'] = 'Windows'
    # Don't build compiler-rt, libcxx etc. for Windows
    windows_extra_defines['LLVM_BUILD_RUNTIME'] = 'OFF'
    # Build clang-tidy/clang-format for Windows.
    windows_extra_defines['LLVM_TOOL_CLANG_TOOLS_EXTRA_BUILD'] = 'ON'
    windows_extra_defines['LLVM_TOOL_OPENMP_BUILD'] = 'OFF'
    # Don't build tests for Windows.
    windows_extra_defines['LLVM_INCLUDE_TESTS'] = 'OFF'
    # Use libc++ for Windows.
    windows_extra_defines['LLVM_ENABLE_LIBCXX'] = 'ON'

    windows_extra_defines['LLVM_ENABLE_PROJECTS'] = 'clang;clang-tools-extra;lld'

    windows_sysroot = utils.android_path('prebuilts', 'gcc', 'linux-x86',
                                         'host', 'x86_64-w64-mingw32-4.8',
                                         'x86_64-w64-mingw32')
    update_cmake_sysroot_flags(windows_extra_defines, windows_sysroot)

    # Set CMake path, toolchain file for native compilation (to build tablegen
    # etc).  Also disable libfuzzer build during native compilation.
    windows_extra_defines['CROSS_TOOLCHAIN_FLAGS_NATIVE'] = \
        '-DCMAKE_PREFIX_PATH=' + cmake_prebuilt_bin_dir() + ';' + \
        '-DCOMPILER_RT_BUILD_LIBFUZZER=OFF;'+ \
        '-DCMAKE_TOOLCHAIN_FILE=' + native_cmake_file_path + ';' + \
        '-DLLVM_ENABLE_LIBCXX=ON;' + \
        '-DCMAKE_BUILD_WITH_INSTALL_RPATH=TRUE;' + \
        '-DCMAKE_INSTALL_RPATH=' + os.path.join(stage1_install, 'lib64')

    if enable_assertions:
        windows_extra_defines['LLVM_ENABLE_ASSERTIONS'] = 'ON'

    cflags, ldflags = host_gcc_toolchain_flags('windows-x86')
    cflags.extend(windows_cflags())
    cxxflags = list(cflags)

    # Use -fuse-cxa-atexit to allow static TLS destructors.  This is needed for
    # clang-tools-extra/clangd/Context.cpp
    cxxflags.append('-fuse-cxa-atexit')

    # Explicitly add the path to libc++ headers.  We don't need to configure
    # options like visibility annotations, win32 threads etc. because the
    # __generated_config header in the patch captures all the options used when
    # building libc++.
    cxxflags.extend(('-I', os.path.join(install_dir, 'include', 'c++', 'v1')))

    ldflags.extend((
        '-Wl,--dynamicbase',
        '-Wl,--nxcompat',
        # Use ucrt to find locale functions needed by libc++.
        '-lucrt', '-lucrtbase',
        # Use static-libgcc to avoid runtime dependence on libgcc_eh.
        '-static-libgcc',
        # pthread is needed by libgcc_eh
        '-lpthread',
        # Add path to libc++, libc++abi.
        '-L', os.path.join(install_dir, 'lib64')))

    ldflags.append('-Wl,--high-entropy-va')

    # Include zlib's header and library path
    zlib_path = utils.android_path('prebuilts', 'clang', 'host', 'windows-x86',
                                   'toolchain-prebuilts', 'zlib')
    zlib_inc = os.path.join(zlib_path, 'include')
    zlib_lib = os.path.join(zlib_path, 'lib')

    cflags.extend(['-I', zlib_inc])
    cxxflags.extend(['-I', zlib_inc])
    ldflags.extend(['-L', zlib_lib])

    windows_extra_defines['CMAKE_ASM_FLAGS'] = ' '.join(cflags)
    windows_extra_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
    windows_extra_defines['CMAKE_CXX_FLAGS'] = ' '.join(cxxflags)
    windows_extra_defines['CMAKE_EXE_LINKER_FLAGS'] = ' '.join(ldflags)
    windows_extra_defines['CMAKE_SHARED_LINKER_FLAGS'] = ' '.join(ldflags)
    windows_extra_defines['CMAKE_MODULE_LINKER_FLAGS'] = ' '.join(ldflags)

    windows_extra_env = dict()

    build_llvm(
        targets=targets,
        build_dir=build_dir,
        install_dir=install_dir,
        build_name=build_name,
        extra_defines=windows_extra_defines,
        extra_env=windows_extra_env)


def host_sysroot():
    if utils.host_is_darwin():
        return ""
    else:
        return utils.android_path('prebuilts/gcc', utils.build_os_type(),
                                  'host/x86_64-linux-glibc2.17-4.8/sysroot')


def host_gcc_toolchain_flags(host_os, is_32_bit=False):
    def formatFlags(flags, **values):
        flagsStr = ' '.join(flags)
        flagsStr = flagsStr.format(**values)
        return flagsStr.split(' ')

    cflags = [debug_prefix_flag()]
    ldflags = []

    if host_os == 'darwin-x86':
        xcrun_command = ['xcrun', '--show-sdk-path']
        macSdkRoot = (check_output(xcrun_command)).strip()
        # We were using 10.8, but we need 10.9 to use ~type_info() from libcxx.
        macMinVersion = '10.9'

        cflags.extend(('-isysroot {macSdkRoot}',
                       '-mmacosx-version-min={macMinVersion}',
                       '-DMACOSX_DEPLOYMENT_TARGET={macMinVersion}',
                      ))
        ldflags.extend(('-isysroot {macSdkRoot}',
                        '-Wl,-syslibroot,{macSdkRoot}',
                        '-mmacosx-version-min={macMinVersion}',
                       ))

        cflags = formatFlags(cflags, macSdkRoot=macSdkRoot,
                             macMinVersion=macMinVersion)
        ldflags = formatFlags(ldflags, macSdkRoot=macSdkRoot,
                              macMinVersion=macMinVersion)
        return cflags, ldflags

    # GCC toolchain flags for Linux and Windows
    if host_os == 'linux-x86':
        gccRoot = utils.android_path('prebuilts/gcc', utils.build_os_type(),
                                     'host/x86_64-linux-glibc2.17-4.8')
        gccTriple = 'x86_64-linux'
        gccVersion = '4.8.3'

        # gcc-toolchain is only needed for Linux
        cflags.append('--gcc-toolchain={gccRoot}')
    elif host_os == 'windows-x86':
        gccRoot = utils.android_path('prebuilts/gcc', utils.build_os_type(),
                                     'host/x86_64-w64-mingw32-4.8')
        gccTriple = 'x86_64-w64-mingw32'
        gccVersion = '4.8.3'

    cflags.append('-B{gccRoot}/{gccTriple}/bin')

    gccLibDir = '{gccRoot}/lib/gcc/{gccTriple}/{gccVersion}'
    gccBuiltinDir = '{gccRoot}/{gccTriple}/lib64'
    if is_32_bit:
        gccLibDir += '/32'
        gccBuiltinDir = gccBuiltinDir.replace('lib64', 'lib32')

    ldflags.extend(('-B' + gccLibDir,
                    '-L' + gccLibDir,
                    '-B' + gccBuiltinDir,
                    '-L' + gccBuiltinDir,
                    '-fuse-ld=lld',
                   ))

    cflags = formatFlags(cflags, gccRoot=gccRoot, gccTriple=gccTriple,
                         gccVersion=gccVersion)
    ldflags = formatFlags(ldflags, gccRoot=gccRoot, gccTriple=gccTriple,
                          gccVersion=gccVersion)
    return cflags, ldflags


def get_shared_extra_defines():
    extra_defines = dict()
    extra_defines['LLVM_BUILD_RUNTIME'] = 'ON'
    extra_defines['LLVM_ENABLE_PROJECTS'] = 'clang;lld;libcxxabi;libcxx;compiler-rt'
    return extra_defines


def build_stage1(stage1_install, build_name, build_llvm_tools=False):
    # Build/install the stage 1 toolchain
    cflags, ldflags = host_gcc_toolchain_flags(utils.build_os_type())

    stage1_path = utils.out_path('stage1')
    stage1_targets = 'X86'

    stage1_extra_defines = get_shared_extra_defines()
    stage1_extra_defines['CLANG_ENABLE_ARCMT'] = 'OFF'
    stage1_extra_defines['CLANG_ENABLE_STATIC_ANALYZER'] = 'OFF'
    stage1_extra_defines['CMAKE_C_COMPILER'] = os.path.join(
        clang_prebuilt_bin_dir(), 'clang')
    stage1_extra_defines['CMAKE_CXX_COMPILER'] = os.path.join(
        clang_prebuilt_bin_dir(), 'clang++')

    update_cmake_sysroot_flags(stage1_extra_defines, host_sysroot())

    if not utils.host_is_darwin():
        stage1_extra_defines['LLVM_ENABLE_LLD'] = 'ON'

    if build_llvm_tools:
        stage1_extra_defines['LLVM_BUILD_TOOLS'] = 'ON'
    else:
        stage1_extra_defines['LLVM_BUILD_TOOLS'] = 'OFF'

    # Have clang use libc++, ...
    stage1_extra_defines['LLVM_ENABLE_LIBCXX'] = 'ON'

    # ... and point CMake to the libc++.so from the prebuilts.  Install an rpath
    # to prevent linking with the newly-built libc++.so
    ldflags.append('-L' + clang_prebuilt_lib_dir())
    ldflags.append('-Wl,-rpath,' + clang_prebuilt_lib_dir())

    # Make libc++.so a symlink to libc++.so.x instead of a linker script that
    # also adds -lc++abi.  Statically link libc++abi to libc++ so it is not
    # necessary to pass -lc++abi explicitly.  This is needed only for Linux.
    if utils.host_is_linux():
        stage1_extra_defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'
        stage1_extra_defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'

    # Do not build compiler-rt for Darwin.  We don't ship host (or any
    # prebuilt) runtimes for Darwin anyway.  Attempting to build these will
    # fail compilation of lib/builtins/atomic_*.c that only get built for
    # Darwin and fail compilation due to us using the bionic version of
    # stdatomic.h.
    if utils.host_is_darwin():
        stage1_extra_defines['LLVM_BUILD_EXTERNAL_COMPILER_RT'] = 'ON'

    # Don't build libfuzzer as part of the first stage build.
    stage1_extra_defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'

    # Set the compiler and linker flags
    stage1_extra_defines['CMAKE_ASM_FLAGS'] = ' '.join(cflags)
    stage1_extra_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
    stage1_extra_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)

    stage1_extra_defines['CMAKE_EXE_LINKER_FLAGS'] = ' '.join(ldflags)
    stage1_extra_defines['CMAKE_SHARED_LINKER_FLAGS'] = ' '.join(ldflags)
    stage1_extra_defines['CMAKE_MODULE_LINKER_FLAGS'] = ' '.join(ldflags)

    stage1_extra_env = dict()
    if USE_GOMA_FOR_STAGE1:
        stage1_extra_env['USE_GOMA'] = 'true'

    build_llvm(
        targets=stage1_targets,
        build_dir=stage1_path,
        install_dir=stage1_install,
        build_name=build_name,
        extra_defines=stage1_extra_defines,
        extra_env=stage1_extra_env)


def build_stage2(stage1_install,
                 stage2_install,
                 stage2_targets,
                 build_name,
                 enable_assertions=False,
                 debug_build=False,
                 no_lto=False,
                 build_instrumented=False,
                 profdata_file=None):
    cflags, ldflags = host_gcc_toolchain_flags(utils.build_os_type())

    # Build/install the stage2 toolchain
    stage2_cc = os.path.join(stage1_install, 'bin', 'clang')
    stage2_cxx = os.path.join(stage1_install, 'bin', 'clang++')
    stage2_path = utils.out_path('stage2')

    stage2_extra_defines = get_shared_extra_defines()
    stage2_extra_defines['LLVM_ENABLE_PROJECTS'] += ';clang-tools-extra;openmp'
    stage2_extra_defines['CMAKE_C_COMPILER'] = stage2_cc
    stage2_extra_defines['CMAKE_CXX_COMPILER'] = stage2_cxx
    stage2_extra_defines['LLVM_ENABLE_LIBCXX'] = 'ON'
    stage2_extra_defines['SANITIZER_ALLOW_CXXABI'] = 'OFF'
    stage2_extra_defines['LIBOMP_ENABLE_SHARED'] = 'FALSE'

    update_cmake_sysroot_flags(stage2_extra_defines, host_sysroot())

    if not utils.host_is_darwin():
        stage2_extra_defines['LLVM_ENABLE_LLD'] = 'ON'

        # lld, lto and pgo instrumentation doesn't work together
        # http://b/79419131
        if not build_instrumented and not no_lto:
            stage2_extra_defines['LLVM_ENABLE_LTO'] = 'Thin'

    # Build libFuzzer here to be exported for the host fuzzer builds. libFuzzer
    # is not currently supported on Darwin.
    if utils.host_is_darwin():
        stage2_extra_defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'
    else:
        stage2_extra_defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'ON'

    if enable_assertions:
        stage2_extra_defines['LLVM_ENABLE_ASSERTIONS'] = 'ON'

    if debug_build:
        stage2_extra_defines['CMAKE_BUILD_TYPE'] = 'Debug'

    if build_instrumented:
        stage2_extra_defines['LLVM_BUILD_INSTRUMENTED'] = 'ON'

        # llvm-profdata is only needed to finish CMake configuration
        # (tools/clang/utils/perf-training/CMakeLists.txt) and not needed for
        # build
        llvm_profdata = os.path.join(stage1_install, 'bin', 'llvm-profdata')
        stage2_extra_defines['LLVM_PROFDATA'] = llvm_profdata

        # Building libcxx, libcxxabi with instrumentation causes linker errors
        # because these are built with -nodefaultlibs and prevent libc symbols
        # needed by libclang_rt.profile from being resolved.  Manually adding
        # the libclang_rt.profile to linker flags fixes the issue.
        version = extract_clang_long_version(stage1_install)
        resource_dir = clang_resource_dir(version, '')
        ldflags.append(os.path.join(stage1_install, resource_dir,
                                    'libclang_rt.profile-x86_64.a'))

    if profdata_file:
        if build_instrumented:
            raise RuntimeError(
                'Cannot simultaneously instrument and use profiles')

        stage2_extra_defines['LLVM_PROFDATA_FILE'] = profdata_file
        cflags.append('-Wno-profile-instr-out-of-date')
        cflags.append('-Wno-profile-instr-unprofiled')

    # Make libc++.so a symlink to libc++.so.x instead of a linker script that
    # also adds -lc++abi.  Statically link libc++abi to libc++ so it is not
    # necessary to pass -lc++abi explicitly.  This is needed only for Linux.
    if utils.host_is_linux():
        stage2_extra_defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'
        stage2_extra_defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'

    # Do not build compiler-rt for Darwin.  We don't ship host (or any
    # prebuilt) runtimes for Darwin anyway.  Attempting to build these will
    # fail compilation of lib/builtins/atomic_*.c that only get built for
    # Darwin and fail compilation due to us using the bionic version of
    # stdatomic.h.
    if utils.host_is_darwin():
        stage2_extra_defines['LLVM_BUILD_EXTERNAL_COMPILER_RT'] = 'ON'

    # Point CMake to the libc++ from stage1.  It is possible that once built,
    # the newly-built libc++ may override this because of the rpath pointing to
    # $ORIGIN/../lib64.  That'd be fine because both libraries are built from
    # the same sources.
    ldflags.append('-L' + os.path.join(stage1_install, 'lib64'))
    stage2_extra_env = dict()
    stage2_extra_env['LD_LIBRARY_PATH'] = os.path.join(stage1_install, 'lib64')

    # Set the compiler and linker flags
    stage2_extra_defines['CMAKE_ASM_FLAGS'] = ' '.join(cflags)
    stage2_extra_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
    stage2_extra_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)

    stage2_extra_defines['CMAKE_EXE_LINKER_FLAGS'] = ' '.join(ldflags)
    stage2_extra_defines['CMAKE_SHARED_LINKER_FLAGS'] = ' '.join(ldflags)
    stage2_extra_defines['CMAKE_MODULE_LINKER_FLAGS'] = ' '.join(ldflags)

    build_llvm(
        targets=stage2_targets,
        build_dir=stage2_path,
        install_dir=stage2_install,
        build_name=build_name,
        extra_defines=stage2_extra_defines,
        extra_env=stage2_extra_env)


def build_runtimes(stage2_install):
    create_sysroots()
    version = extract_clang_version(stage2_install)
    build_crts(stage2_install, version)
    build_crts(stage2_install, version, ndk_cxx=True)
    # 32-bit host crts are not needed for Darwin
    if utils.host_is_linux():
        build_crts_host_i686(stage2_install, version)
    build_libfuzzers(stage2_install, version)
    build_libfuzzers(stage2_install, version, ndk_cxx=True)
    build_libomp(stage2_install, version)
    build_libomp(stage2_install, version, ndk_cxx=True)
    build_libomp(stage2_install, version, ndk_cxx=True, is_shared=True)
    # Bug: http://b/64037266. `strtod_l` is missing in NDK r15. This will break
    # libcxx build.
    # build_libcxx(stage2_install, version)
    build_asan_test(stage2_install)
    build_sanitizer_map_files(stage2_install, version)
    create_hwasan_symlink(stage2_install, version)

def install_wrappers(llvm_install_path):
    wrapper_path = utils.android_path('toolchain', 'llvm_android',
                                      'compiler_wrapper.py')
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
def normalize_llvm_host_libs(install_dir, host, version):
    if host == 'linux-x86':
        libs = {'libLLVM': 'libLLVM-{version}svn.so',
                'libclang': 'libclang.so.{version}svn',
                'libclang_cxx': 'libclang_cxx.so.{version}svn',
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
        libcxx_name = 'libc++.so' if host == 'linux-x86' else 'libc++.dylib'
        all_libs = [lib for lib in os.listdir(libdir) if
                    lib != libcxx_name and
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


def remove_static_libraries(static_lib_dir):
    if os.path.isdir(static_lib_dir):
        lib_files = os.listdir(static_lib_dir)
        for lib_file in lib_files:
            if lib_file.endswith('.a'):
                static_library = os.path.join(static_lib_dir, lib_file)
                remove(static_library)


def package_toolchain(build_dir, build_name, host, dist_dir, strip=True):
    is_windows = host == 'windows-x86-64'
    is_linux = host == 'linux-x86'
    package_name = 'clang-' + build_name
    install_host_dir = utils.out_path('install', host)
    install_dir = os.path.join(install_host_dir, package_name)
    version = extract_clang_version(build_dir)

    # Remove any previously installed toolchain so it doesn't pollute the
    # build.
    if os.path.exists(install_host_dir):
        shutil.rmtree(install_host_dir)

    # First copy over the entire set of output objects.
    shutil.copytree(build_dir, install_dir, symlinks=True)

    ext = '.exe' if is_windows else ''
    shlib_ext = '.dll' if is_windows else '.so' if is_linux else '.dylib'

    # Next, we remove unnecessary binaries.
    necessary_bin_files = [
        'clang' + ext,
        'clang++' + ext,
        'clang-' + version.major_version() + ext,
        'clang-check' + ext,
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
        'llvm-link' + ext,
        'llvm-modextract' + ext,
        'llvm-nm' + ext,
        'llvm-objcopy' + ext,
        'llvm-objdump' + ext,
        'llvm-profdata' + ext,
        'llvm-ranlib' + ext,
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
    ]

    windows_bin_blacklist = [
        'clang-' + version.major_version() + ext,
        'scan-build' + ext,
        'scan-view' + ext,
    ]

    # scripts that should not be stripped
    script_bins = [
        'git-clang-format',
        'scan-build',
        'scan-view',
    ]

    bin_dir = os.path.join(install_dir, 'bin')
    lib_dir = os.path.join(install_dir, 'lib64')

    bin_files = os.listdir(bin_dir)
    for bin_filename in bin_files:
        binary = os.path.join(bin_dir, bin_filename)
        if os.path.isfile(binary):
            if bin_filename not in necessary_bin_files:
                remove(binary)
            elif strip:
                if bin_filename not in script_bins:
                    check_call(['strip', binary])

    # FIXME: check that all libs under lib64/clang/<version>/ are created.
    for necessary_bin_file in necessary_bin_files:
        if is_windows and necessary_bin_file in windows_bin_blacklist:
            continue
        if not os.path.isfile(os.path.join(bin_dir, necessary_bin_file)):
            raise RuntimeError('Did not find %s in %s' % (necessary_bin_file, bin_dir))

    # Next, we remove unnecessary static libraries.
    remove_static_libraries(lib_dir)

    # For Windows, add other relevant libraries.
    if is_windows:
        install_winpthreads(bin_dir, lib_dir)

    if not is_windows:
        install_wrappers(install_dir)
        normalize_llvm_host_libs(install_dir, host, version)

    # Check necessary Windows lib files exist.
    windows_necessary_lib_files = [
        'LLVMgold' + shlib_ext,
        'libwinpthread-1' + shlib_ext,
    ]

    for necessary_lib_file in windows_necessary_lib_files:
        if is_windows and not os.path.isfile(os.path.join(lib_dir, necessary_lib_file)):
            raise RuntimeError('Did not find %s under lib64' % necessary_lib_file)

    # Next, we copy over stdatomic.h from bionic.
    stdatomic_path = utils.android_path('bionic', 'libc', 'include',
                                        'stdatomic.h')
    resdir_top = os.path.join(lib_dir, 'clang')
    header_path = os.path.join(resdir_top, version.long_version(), 'include')
    install_file(stdatomic_path, header_path)

    # Install license files as NOTICE in the toolchain install dir.
    install_license_files(install_dir)

    # Add an AndroidVersion.txt file.
    version_file_path = os.path.join(install_dir, 'AndroidVersion.txt')
    with open(version_file_path, 'w') as version_file:
        version_file.write('{}\n'.format(version.long_version()))
        version_file.write('based on {}\n'.format(android_version.svn_revision))

    # Package up the resulting trimmed install/ directory.
    tarball_name = package_name + '-' + host
    package_path = os.path.join(dist_dir, tarball_name) + '.tar.bz2'
    logger().info('Packaging %s', package_path)
    args = ['tar', '-cjC', install_host_dir, '-f', package_path, package_name]
    check_call(args)


def parse_args():
    known_platforms = ('linux', 'windows')
    known_platforms_str = ', '.join(known_platforms)

    # Simple argparse.Action to allow comma-separated values (e.g.
    # --option=val1,val2)
    class CommaSeparatedListAction(argparse.Action):
        def __call__(self, parser, namespace, values, option_string):
            for value in values.split(','):
                if value not in known_platforms:
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

    parser.add_argument(
        '--no-build',
        action=CommaSeparatedListAction,
        default=list(),
        help='Don\'t build toolchain for specified platforms.  Choices: ' + \
            known_platforms_str)

    parser.add_argument(
        '--check-pgo-profile',
        action='store_true',
        default=False,
        help='Fail if expected PGO profile doesn\'t exist')

    return parser.parse_args()


def main():
    args = parse_args()
    do_build = not args.skip_build
    do_package = not args.skip_package
    do_strip = not args.no_strip
    do_strip_host_package = do_strip and not args.debug

    need_host = utils.host_is_darwin() or ('linux' not in args.no_build)
    need_windows = utils.host_is_linux() and \
        ('windows' not in args.no_build)

    log_levels = [logging.INFO, logging.DEBUG]
    verbosity = min(args.verbose, len(log_levels) - 1)
    log_level = log_levels[verbosity]
    logging.basicConfig(level=log_level)

    stage1_install = utils.out_path('stage1-install')
    stage2_install = utils.out_path('stage2-install')
    windows64_install = utils.out_path('windows-x86-64-install')

    # Build the stage1 Clang for the build host
    instrumented = utils.host_is_linux() and args.build_instrumented
    # Windows libs are built with stage1 toolchain. llvm-config is required.
    stage1_build_llvm_tools = instrumented or (do_build and need_windows)
    build_stage1(stage1_install, args.build_name,
                 build_llvm_tools=stage1_build_llvm_tools)

    if do_build and need_host:
        if os.path.exists(stage2_install):
            utils.rm_tree(stage2_install)

        profdata_filename = pgo_profdata_filename()
        profdata = pgo_profdata_file(profdata_filename)
        # Do not use PGO profiles if profdata file doesn't exist unless failure
        # is explicitly requested via --check-pgo-profile.
        if profdata is None and args.check_pgo_profile:
            raise RuntimeError('Profdata file does not exist for ' +
                               profdata_filename)

        build_stage2(stage1_install, stage2_install, STAGE2_TARGETS,
                     args.build_name, args.enable_assertions,
                     args.debug, args.no_lto, instrumented, profdata)

        if utils.host_is_linux():
            build_runtimes(stage2_install)

    if do_build and need_windows:
        if os.path.exists(windows64_install):
            utils.rm_tree(windows64_install)

        windows64_path = utils.out_path('windows-x86-64')
        build_llvm_for_windows(
            stage1_install=stage1_install,
            targets=STAGE2_TARGETS,
            enable_assertions=args.enable_assertions,
            build_dir=windows64_path,
            install_dir=windows64_install,
            build_name=args.build_name)

    dist_dir = ORIG_ENV.get('DIST_DIR', utils.out_path())
    if do_package and need_host:
        package_toolchain(
            stage2_install,
            args.build_name,
            utils.build_os_type(),
            dist_dir,
            strip=do_strip_host_package)

    if do_package and need_windows:
        package_toolchain(
            windows64_install,
            args.build_name,
            'windows-x86-64',
            dist_dir,
            strip=do_strip)

    return 0


if __name__ == '__main__':
    main()
