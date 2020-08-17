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
from typing import cast, List, Optional, Set

import android_version
from base_builders import Builder, LLVMBuilder
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
BUILD_LLVM_NEXT = False

def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


def set_default_toolchain(toolchain: toolchains.Toolchain) -> None:
    """Sets the toolchain to use for builders who don't specify a toolchain in constructor."""
    Builder.toolchain = toolchain


def build_llvm_for_windows(enable_assertions: bool,
                           build_name: str,
                           build_lldb: bool,
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

    win_builder.build_lldb = build_lldb
    lldb_bins: Optional[Set[str]] = None
    if build_lldb:
        assert swig_builder is not None
        win_builder.libedit = None
        win_builder.swig_executable = swig_builder.install_dir / 'bin' / 'swig'

        xz_builder = builders.XzBuilder(config_list)
        xz_builder.build()
        win_builder.liblzma = xz_builder

        libxml2_builder = builders.LibXml2Builder(config_list)
        libxml2_builder.build()
        win_builder.libxml2 = libxml2_builder
        lldb_bins = {
            'liblldb.dll',
            'python38.dll',
            libxml2_builder.install_library.name,
        }

    win_builder.build_name = build_name
    win_builder.svn_revision = android_version.get_svn_revision(BUILD_LLVM_NEXT)
    win_builder.enable_assertions = enable_assertions
    win_builder.build()

    return (win_builder, lldb_bins)


def build_runtimes(build_lldb_server: bool):
    builders.SysrootsBuilder().build()

    builders.PlatformLibcxxAbiBuilder().build()
    builders.CompilerRTBuilder().build()
    # 32-bit host crts are not needed for Darwin
    if hosts.build_host().is_linux:
        builders.CompilerRTHostI386Builder().build()
    builders.LibOMPBuilder().build()
    if build_lldb_server:
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
    go_env['PATH'] = str(paths.GO_BIN_PATH) + os.pathsep + go_env['PATH']
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
    shutil.copy2(bisect_path, bin_path)


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
                os.remove(lib)


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
        shutil.copy2(license_file, install_dir)

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
    shutil.copy2(lib_path, lib_install)

    bin_install = os.path.join(bin_dir, lib_name)
    shutil.copy2(lib_path, bin_install)


def remove_static_libraries(static_lib_dir, necessary_libs=None):
    if not necessary_libs:
        necessary_libs = {}
    if os.path.isdir(static_lib_dir):
        lib_files = os.listdir(static_lib_dir)
        for lib_file in lib_files:
            if lib_file.endswith('.a') and lib_file not in necessary_libs:
                static_library = os.path.join(static_lib_dir, lib_file)
                os.remove(static_library)


def package_toolchain(toolchain_builder: LLVMBuilder,
                      necessary_bin_files: Optional[Set[str]]=None,
                      strip=True, create_tar=True):
    dist_dir = Path(ORIG_ENV.get('DIST_DIR', paths.OUT_DIR))
    build_dir = toolchain_builder.install_dir
    host = toolchain_builder.config_list[0].target_os
    build_name = toolchain_builder.build_name
    version = toolchain_builder.installed_toolchain.version

    package_name = 'clang-' + build_name

    install_dir = paths.get_package_install_path(host, package_name)
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

    if not necessary_bin_files:
        necessary_bin_files = set()

    # Next, we remove unnecessary binaries.
    necessary_bin_files |= {
        'clang' + ext,
        'clang++' + ext,
        'clang-' + version.major_version() + ext,
        'clang-check' + ext,
        'clang-cl' + ext,
        'clang-format' + ext,
        'clang-tidy' + ext,
        'clangd' + ext,
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

    if toolchain_builder.build_lldb:
        necessary_bin_files.update({
            'lldb-argdumper' + ext,
            'lldb' + ext,
            'lldb' + script_ext,
        })

    if host.is_windows:
        windows_exclude_bin_files = {
            'clang-' + version.major_version() + ext,
            'clangd' + ext,
            'scan-build' + ext,
            'scan-view' + ext,
        }
        necessary_bin_files -= windows_exclude_bin_files

    # scripts that should not be stripped
    script_bins = {
        'git-clang-format',
        'scan-build',
        'scan-view',
        'lldb' + script_ext,
    }

    bin_dir = os.path.join(install_dir, 'bin')
    lib_dir = os.path.join(install_dir, 'lib64')
    strip_cmd = Builder.toolchain.strip

    for bin_filename in os.listdir(bin_dir):
        binary = os.path.join(bin_dir, bin_filename)
        if os.path.isfile(binary):
            if bin_filename not in necessary_bin_files:
                os.remove(binary)
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
    shutil.copy2(stdatomic_path, header_path)

    bits_install_path = os.path.join(header_path, 'bits')
    if not os.path.isdir(bits_install_path):
        os.mkdir(bits_install_path)
    bits_stdatomic_path = utils.android_path(libc_include_path, 'bits', 'stdatomic.h')
    shutil.copy2(bits_stdatomic_path, bits_install_path)


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
        '--create-tar',
        action='store_true',
        default=False,
        help='Create a tar archive of the toolchains')

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
    build_lldb = 'lldb' not in args.no_build

    # TODO (Pirama): Avoid using global statement
    global BUILD_LLVM_NEXT
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
    stage1.build_lldb = build_lldb
    stage1.build_android_targets = args.debug or instrumented
    stage1.use_goma_for_stage1 = USE_GOMA_FOR_STAGE1
    stage1.build()
    set_default_toolchain(stage1.installed_toolchain)

    if build_lldb:
        # Swig is needed for both host and windows lldb.
        swig_builder = builders.SwigBuilder()
        swig_builder.build()
    else:
        swig_builder = None

    if need_host:
        profdata_filename = paths.pgo_profdata_filename(BUILD_LLVM_NEXT)
        profdata = paths.pgo_profdata_file(profdata_filename)

        stage2 = builders.Stage2Builder()
        stage2.build_name = args.build_name
        stage2.svn_revision = android_version.get_svn_revision(BUILD_LLVM_NEXT)
        stage2.debug_build = args.debug
        stage2.enable_assertions = args.enable_assertions
        stage2.lto = not args.no_lto
        stage2.build_instrumented = instrumented
        stage2.profdata_file = profdata if profdata else None

        stage2.build_lldb = build_lldb
        if build_lldb:
            stage2.swig_executable = swig_builder.install_dir / 'bin' / 'swig'

            xz_builder = builders.XzBuilder()
            xz_builder.build()
            stage2.liblzma = xz_builder

            libxml2_builder = builders.LibXml2Builder()
            libxml2_builder.build()
            stage2.libxml2 = libxml2_builder

            libedit_builder = builders.LibEditBuilder()
            libedit_builder.build()
            stage2.libedit = libedit_builder

        stage2_tags = []
        # Annotate the version string if there is no profdata.
        if profdata is None:
            stage2_tags.append('NO PGO PROFILE')
        # Annotate the version string if this is an llvm-next build.
        if BUILD_LLVM_NEXT:
            stage2_tags.append('ANDROID_LLVM_NEXT')
        stage2.build_tags = stage2_tags

        stage2.build()
        if not (stage2.build_instrumented or stage2.debug_build):
            set_default_toolchain(stage2.installed_toolchain)

        Builder.output_toolchain = stage2.installed_toolchain
        if hosts.build_host().is_linux and do_runtimes:
            build_runtimes(build_lldb_server=build_lldb)

    if need_windows:
        if args.windows_sdk:
            win_sdk.set_path(Path(args.windows_sdk))
        win_builder, win_lldb_bins = build_llvm_for_windows(
            enable_assertions=args.enable_assertions,
            build_name=args.build_name,
            build_lldb=build_lldb,
            swig_builder=swig_builder)

    if do_package and need_host:
        package_toolchain(
            stage2,
            strip=do_strip_host_package,
            create_tar=args.create_tar)

    if do_package and need_windows:
        package_toolchain(
            win_builder,
            necessary_bin_files=win_lldb_bins,
            strip=do_strip,
            create_tar=args.create_tar)

    return 0


if __name__ == '__main__':
    main()
