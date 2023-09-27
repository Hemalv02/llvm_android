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
import sys
import textwrap
from typing import List, NamedTuple, Optional, Set, Tuple
import re

import android_version
from base_builders import Builder, LLVMBuilder
import builders
from builder_registry import BuilderRegistry
import configs
import hosts
import paths
import source_manager
import timer
import toolchains
import utils
from version import Version
import win_sdk

def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


def set_default_toolchain(toolchain: toolchains.Toolchain) -> None:
    """Sets the toolchain to use for builders who don't specify a toolchain in constructor."""
    Builder.toolchain = toolchain


def extract_pgo_profile() -> Path:
    pgo_profdata_tar = paths.pgo_profdata_tar()
    if not pgo_profdata_tar:
        raise RuntimeError(f'{pgo_profdata_tar} does not exist')
    utils.extract_tarball(paths.OUT_DIR, pgo_profdata_tar)
    profdata_file = paths.OUT_DIR / paths.pgo_profdata_filename()
    if not profdata_file.exists():
        raise RuntimeError(f'{profdata_file} does not exist')
    return profdata_file


def extract_bolt_profile() -> Path:
    bolt_fdata_tar = paths.bolt_fdata_tar()
    if not bolt_fdata_tar:
        raise RuntimeError(f'{bolt_fdata_tar} does not exist')
    utils.extract_tarball(paths.OUT_DIR, bolt_fdata_tar)
    clang_bolt_fdata_file = paths.OUT_DIR / 'clang.fdata'
    if not clang_bolt_fdata_file.exists():
        raise RuntimeError(f'{clang_bolt_fdata_file} does not exist')
    return clang_bolt_fdata_file


def build_llvm_for_windows(enable_assertions: bool,
                           enable_lto: bool,
                           profdata_file: Optional[Path],
                           build_name: str,
                           build_lldb: bool,
                           swig_builder: Optional[builders.SwigBuilder],
                           full_build: bool,
                           build_simpleperf_readelf: bool):
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
        libcxx_builder = builders.WinLibCxxBuilder(config_list)
        libcxx_builder.enable_assertions = enable_assertions
        libcxx_builder.build()

        # Also build a 32-bit Windows libc++ for use with the platform
        # adb/fastboot.
        libcxx32_builder = builders.WinLibCxxBuilder([configs.MinGWConfig(is_32_bit=True)])
        libcxx32_builder.enable_assertions = enable_assertions
        libcxx32_builder.build()

    lldb_bins: Set[str] = set()

    if full_build:
        libzstd_builder = builders.ZstdBuilder(config_list)
        libzstd_builder.build()
        win_builder.libzstd = libzstd_builder

        libxml2_builder = builders.LibXml2Builder(config_list)
        libxml2_builder.build()
        win_builder.libxml2 = libxml2_builder
        for lib in libxml2_builder.install_libraries:
            lldb_bins.add(lib.name)

        win_builder.build_lldb = build_lldb
        if build_lldb:
            assert swig_builder is not None
            win_builder.libedit = None
            win_builder.swig_executable = swig_builder.install_dir / 'bin' / 'swig'

            xz_builder = builders.XzBuilder(config_list)
            xz_builder.build()
            win_builder.liblzma = xz_builder

            lldb_bins.add('liblldb.dll')

        win_builder.build_name = build_name
        win_builder.svn_revision = android_version.get_svn_revision()
        win_builder.enable_assertions = enable_assertions
        win_builder.lto = enable_lto
        win_builder.build()

    if build_simpleperf_readelf:
        libsimpleperf_readelf_builder = builders.LibSimpleperfReadElfBuilder(config_list)
        if full_build:
            # The libs have been built in win_builder.
            libsimpleperf_readelf_builder.build_readelf_lib(
                win_builder.output_dir / 'lib', libsimpleperf_readelf_builder.install_dir)
        else:
            libsimpleperf_readelf_builder.enable_assertions = enable_assertions
            libsimpleperf_readelf_builder.build()

    return (win_builder, lldb_bins)

def add_header_links(stage: str, host_config: configs.Config):
    # b/251003274 We also need to copy __config_site from a triple-specific
    # location until we have a copy for each target separately.
    llvm_triple = host_config.llvm_triple
    dst = paths.OUT_DIR / f'{stage}-install' / 'include' / 'c++' / 'v1' / '__config_site'
    src = f'../../{llvm_triple}/c++/v1/__config_site'
    dst.unlink(missing_ok=True)
    dst.symlink_to(src)

def add_lib_links(stage: str, host_config: configs.Config):
    # FIXME: b/245395722. When all dependent scripts and .bp rules are changed
    # to use the new lib names and location. These lib links won't be necessary.
    # Libraries in ./stage2-install/lib/clang/*/lib/linux/*-x86_64.* are now
    # built into ./stage2-install/lib/clang/*/lib/x86_64-unknown-linux-gnu/*.*
    # Add symbolic links from linux/*-x86_64.* to ../x86_64-unknown-linux-gnu/*.*
    # b/245614328, stage1-install/lib has the same issue.
    llvm_triple = host_config.llvm_triple
    arch = llvm_triple.split('-')[0]
    srcglob = f'{paths.OUT_DIR}/{stage}-install/lib/clang/*/lib/{llvm_triple}/*.*'
    for file in glob.glob(srcglob):
        dirname = os.path.dirname(file)
        filename = os.path.basename(file)
        suffix = Path(file).suffix
        stem = Path(file).stem
        # If suffix is '.syms', the real suffix is '.a.syms'.
        if suffix == '.syms':
            suffix1 = Path(stem).suffix
            stem = Path(stem).stem
            suffix = suffix1 + suffix

        dst_dir = Path(dirname) / '..' / 'linux'
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / (stem + '-' + arch + suffix)
        src = f'../{llvm_triple}/{filename}'
        dst.unlink(missing_ok=True)
        dst.symlink_to(src)

    if host_config.target_os.is_linux and not host_config.is_32_bit:
        # Add symbolic links from lib/* to lib/x86_64-unknown-linux-gnu/*.  These
        # symlinks are needed for 64-bit and not 32-bit
        srcglob = f'{paths.OUT_DIR}/{stage}-install/lib/{llvm_triple}/*'
        for file in glob.glob(srcglob):
            filename = os.path.basename(file)
            src = f'{llvm_triple}/{filename}'
            dst = paths.OUT_DIR / f'{stage}-install/lib' / filename
            dst.unlink(missing_ok=True)
            dst.symlink_to(src)


def build_runtimes(build_lldb_server: bool,
                   stage: str,
                   host_config: configs.Config,
                   host_32bit_config: configs.Config):
    builders.DeviceSysrootsBuilder().build()
    builders.BuiltinsBuilder().build()
    builders.LibUnwindBuilder().build()
    builders.DeviceLibcxxBuilder().build()
    builders.CompilerRTBuilder().build()
    builders.TsanBuilder().build()
    # Build musl runtimes and 32-bit glibc for Linux
    if hosts.build_host().is_linux:
        add_lib_links(stage, host_config)
        add_lib_links(stage, host_32bit_config)
        add_header_links(stage, host_config)
        builders.MuslHostRuntimeBuilder().build()
    builders.LibOMPBuilder().build()
    if build_lldb_server:
        builders.LldbServerBuilder().build()
    builders.SanitizerMapFileBuilder().build()
    builders.LibSimpleperfReadElfBuilder().build()


def install_wrappers(llvm_install_path: Path, llvm_next=False) -> None:
    wrapper_path = paths.OUT_DIR / 'llvm_android_wrapper'
    wrapper_build_script = paths.TOOLCHAIN_UTILS_DIR / 'compiler_wrapper' / 'build.py'
    # Note: The build script automatically determines the architecture
    # based on the host.
    go_env = dict(os.environ)
    go_env['PATH'] = str(paths.GO_BIN_PATH) + os.pathsep + go_env['PATH']
    go_env['GOROOT'] = str(paths.GO_ROOT)
    utils.check_call([sys.executable, wrapper_build_script,
                      '--config=android',
                      '--use_ccache=false',
                      '--use_llvm_next=' + str(llvm_next).lower(),
                      f'--output_file={wrapper_path}'], env=go_env)

    bisect_path = paths.SCRIPTS_DIR / 'bisect_driver.py'
    clang_tidy_sh_path = paths.SCRIPTS_DIR / 'clang-tidy.sh'
    bin_path = llvm_install_path / 'bin'
    clang_path = bin_path / 'clang'
    clang_real_path = bin_path / 'clang.real'
    clangxx_path = bin_path / 'clang++'
    clangxx_real_path = bin_path / 'clang++.real'
    clang_tidy_path = bin_path / 'clang-tidy'
    clang_tidy_real_path = bin_path / 'clang-tidy.real'

    # Rename clang and clang++ to clang.real and clang++.real.
    # clang and clang-tidy may already be moved by this script if we use a
    # prebuilt clang. So we only move them if clang.real and clang-tidy.real
    # doesn't exist.
    if not clang_real_path.exists():
        clang_path.rename(clang_real_path)
    clang_tidy_real_path = clang_tidy_path.parent / (clang_tidy_path.name + '.real')
    if not clang_tidy_real_path.exists():
        clang_tidy_path.rename(clang_tidy_real_path)
    clang_path.unlink(missing_ok=True)
    clangxx_path.unlink(missing_ok=True)
    clang_tidy_path.unlink(missing_ok=True)
    clangxx_real_path.unlink(missing_ok=True)
    clangxx_real_path.symlink_to('clang.real')

    shutil.copy2(wrapper_path, clang_path)
    shutil.copy2(wrapper_path, clangxx_path)
    shutil.copy2(wrapper_path, clang_tidy_path)
    shutil.copy2(bisect_path, bin_path)
    shutil.copy2(clang_tidy_sh_path, bin_path)

    # point clang-cl to clang.real instead of clang (which is the wrapper)
    clangcl_path = bin_path / 'clang-cl'
    clangcl_path.unlink()
    clangcl_path.symlink_to('clang.real')


def install_license_files(install_dir: Path) -> None:
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
    for license_file in paths.SCRIPTS_DIR.glob('MODULE_LICENSE_*'):
        shutil.copy2(license_file, install_dir)

    # Fetch all the LICENSE.* files under our projects and append them into a
    # single NOTICE file for the resulting prebuilts.
    notices = []
    for project in projects:
        for license_file in (paths.LLVM_PATH / project).glob('LICENSE.*'):
            with license_file.open() as notice_file:
                notices.append(notice_file.read())
    with (install_dir / 'NOTICE').open('w') as notice_file:
        notice_file.write('\n'.join(notices))


def install_winpthreads(bin_dir: Path, lib_dir: Path) -> None:
    """Installs the winpthreads runtime to the Windows bin and lib directory."""
    lib_name = 'libwinpthread-1.dll'
    lib_path = paths.MINGW_ROOT / 'bin' / lib_name

    shutil.copy2(lib_path, lib_dir / lib_name)
    shutil.copy2(lib_path, bin_dir / lib_name)


def remove_static_libraries(static_lib_dir, necessary_libs=None):
    if not necessary_libs:
        necessary_libs = {}
    if os.path.isdir(static_lib_dir):
        lib_files = os.listdir(static_lib_dir)
        for lib_file in lib_files:
            if lib_file.endswith('.a') and lib_file not in necessary_libs:
                static_library = os.path.join(static_lib_dir, lib_file)
                os.remove(static_library)


def bolt_optimize(toolchain_builder: LLVMBuilder, clang_fdata: Path):
    """ Optimize using llvm-bolt. """
    major_version = toolchain_builder.installed_toolchain.version.major_version()
    bin_dir = toolchain_builder.install_dir / 'bin'
    llvm_bolt_bin = bin_dir / 'llvm-bolt'

    clang_bin = bin_dir / ('clang-' + major_version)
    clang_bin_orig = bin_dir / ('clang-' + major_version + '.orig')
    shutil.move(clang_bin, clang_bin_orig)
    args = [
        llvm_bolt_bin, '-data=' + str(clang_fdata), '-o', clang_bin,
        '-reorder-blocks=ext-tsp', '-reorder-functions=hfsort+',
        '-split-functions', '-split-all-cold', '-dyno-stats',
        '-icf=1', '--use-gnu-stack', clang_bin_orig
    ]
    utils.check_call(args)


def bolt_instrument(toolchain_builder: LLVMBuilder):
    """ Instrument binary using llvm-bolt """
    major_version = toolchain_builder.installed_toolchain.version.major_version()
    bin_dir = toolchain_builder.install_dir / 'bin'
    llvm_bolt_bin = bin_dir / 'llvm-bolt'

    clang_bin = bin_dir / ('clang-' + major_version)
    clang_bin_orig = bin_dir / ('clang-' + major_version + '.orig')
    clang_afdo_path = paths.OUT_DIR / 'bolt_collection' / 'clang' / 'clang'
    shutil.move(clang_bin, clang_bin_orig)
    args = [
        llvm_bolt_bin, '-instrument', '--instrumentation-file=' + str(clang_afdo_path),
        '--instrumentation-file-append-pid', '-o', clang_bin,
        clang_bin_orig
    ]
    utils.check_call(args)

    # Need to create the profile output directory for BOLT.
    # TODO: Let BOLT instrumented library to create it on itself.
    os.makedirs(clang_afdo_path, exist_ok=True)


def verify_symlink_exists(link_path: Path, target: Path):
    if not link_path.exists():
        raise RuntimeError(f'{link_path} does not exist')
    if not link_path.is_symlink():
        raise RuntimeError(f'{link_path} exists but is not a symlink')
    if link_path.readlink() != target:
        raise RuntimeError(f'{link_path} points to {link_path.readlink()}, expected {target}')


def verify_file_exists(lib_dir: Path, name: str):
    if not (lib_dir / name).is_file():
        raise RuntimeError(f'Did not find {name} in {lib_dir}')


def package_toolchain(toolchain_builder: LLVMBuilder,
                      necessary_bin_files: Optional[Set[str]]=None,
                      strip=True, with_runtimes=True, create_tar=True, llvm_next=False):
    build_dir = toolchain_builder.install_dir
    host_config = toolchain_builder.config_list[0]
    host = host_config.target_os
    build_name = toolchain_builder.build_name
    version = toolchain_builder.installed_toolchain.version

    package_name = 'clang-' + build_name

    install_dir = paths.get_package_install_path(host, package_name)
    install_host_dir = install_dir.parent

    # Remove any previously installed toolchain so it doesn't pollute the
    # build.
    if install_host_dir.exists():
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
        'clang-scan-deps' + ext,
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
        'llvm-bolt' + ext,
        'llvm-cfi-verify' + ext,
        'llvm-config' + ext,
        'llvm-cov' + ext,
        'llvm-cxxfilt' + ext,
        'llvm-dis' + ext,
        'llvm-dlltool' + ext,
        'llvm-dwarfdump' + ext,
        'llvm-dwp' + ext,
        'llvm-ifs' + ext,
        'llvm-lib' + ext,
        'llvm-link' + ext,
        'llvm-lipo' + ext,
        'llvm-modextract' + ext,
        'llvm-ml' + ext,
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
        'llvm-windres' + ext,
        'merge-fdata' + ext,
        'sancov' + ext,
        'sanstats' + ext,
        'scan-build' + ext,
        'scan-view' + ext,
        'wasm-ld' + ext,
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
            'llvm-bolt' + ext,
            'merge-fdata' + ext,
            'scan-build' + ext,
            'scan-view' + ext,
        }
        necessary_bin_files -= windows_exclude_bin_files

    # scripts that should not be stripped
    script_bins = {
        'git-clang-format',
        'lldb' + script_ext,
        # merge-fdata is built with relocation, strip -S would fail. Treat it as
        # a script and do not strip as a workaround.
        'merge-fdata' + ext,
        'scan-build',
        'scan-view',
    }

    bin_dir = install_dir / 'bin'
    lib_dir = install_dir / 'lib'
    strip_cmd = Builder.toolchain.strip

    for binary in bin_dir.iterdir():
        if binary.is_file():
            if binary.name not in necessary_bin_files:
                binary.unlink()
            elif binary.is_symlink():
                continue
            elif strip and binary.name not in script_bins:
                # Strip all non-global symbols and debug info.
                if host.is_darwin and binary.name == 'clang-' + version.major_version() + ext:
                    # These specific flags prevent Darwin executables from being
                    # stripped of additional global symbols that might be used
                    # by plugins.
                    utils.check_call([strip_cmd, '-S', '-x', binary])
                else:
                    utils.check_call([strip_cmd, binary])

    # FIXME: check that all libs under lib/clang/<version>/ are created.
    for necessary_bin_file in necessary_bin_files:
        if not (bin_dir / necessary_bin_file).is_file():
            raise RuntimeError(f'Did not find {necessary_bin_file} in {bin_dir}')

    necessary_lib_files = set()
    if with_runtimes:
        if not (host.is_windows and win_sdk.is_enabled()):
            necessary_lib_files |= {
                'libc++.a',
                'libc++abi.a',
            }
        if host.is_linux:
            necessary_lib_files |= {
                'libbolt_rt_instr.a',
                'libc++.so',
                'libc++.so.1',
                'libc++abi.so',
                'libc++abi.so.1',
                'libsimpleperf_readelf.a',
            }
        if host.is_darwin:
            necessary_lib_files |= {
                'libc++.dylib',
                'libc++abi.dylib',
                'libsimpleperf_readelf.a',
            }

        if host.is_windows and not win_sdk.is_enabled():
            necessary_lib_files.add('libwinpthread-1' + shlib_ext)
            # For Windows, add other relevant libraries.
            install_winpthreads(bin_dir, lib_dir)

        # Archive libsimpleperf_readelf.a for linux and darwin hosts from stage2 build.
        if host.is_linux:
            builders.LibSimpleperfReadElfBuilder().build_readelf_lib(lib_dir,
                                                                     lib_dir / host_config.llvm_triple)
        elif host.is_darwin:
            builders.LibSimpleperfReadElfBuilder().build_readelf_lib(lib_dir, lib_dir,
                                                                     is_darwin_lib=True)

    # Remove unnecessary static libraries.
    remove_static_libraries(lib_dir, necessary_lib_files)

    if host.is_linux:
        install_wrappers(install_dir, llvm_next)

    # Add libc++[abi].so.1 and libc++[abi].1.dylib symlinks for backwards compatibility. These
    # symlinks point to the unversioned libraries (as opposed to the typical situation where
    # unversioned symlinks point to the versioned libraries).
    if host.is_linux:
        if host_config.is_musl:
            triple32 = 'i686-unknown-linux-musl'
            triple64 = 'x86_64-unknown-linux-musl'
        else:
            triple32 = 'i386-unknown-linux-gnu'
            triple64 = 'x86_64-unknown-linux-gnu'
        for tripleNN in (triple32, triple64):
            (lib_dir / tripleNN / 'libc++.so.1').symlink_to('libc++.so')
            (lib_dir / tripleNN / 'libc++abi.so.1').symlink_to('libc++abi.so')
        (lib_dir / 'libc++.so.1').symlink_to(Path(triple64) / 'libc++.so.1')
        (lib_dir / 'libc++abi.so.1').symlink_to(Path(triple64) / 'libc++abi.so.1')

    # Check necessary lib files exist.
    for necessary_lib_file in necessary_lib_files:
        if necessary_lib_file.startswith('libc++') and (host.is_linux or host.is_windows):
            verify_file_exists(lib_dir, necessary_lib_file)
            if necessary_lib_file.endswith('.a'):
                verify_file_exists(lib_dir / 'i686-w64-windows-gnu', necessary_lib_file)
                verify_file_exists(lib_dir / 'x86_64-w64-windows-gnu', necessary_lib_file)
            if host.is_linux:
                verify_symlink_exists(lib_dir / necessary_lib_file, Path(triple64) / necessary_lib_file)
                verify_file_exists(lib_dir / triple32, necessary_lib_file)
                verify_file_exists(lib_dir / triple64, necessary_lib_file)
        elif necessary_lib_file == 'libsimpleperf_readelf.a' and host.is_linux:
            verify_file_exists(lib_dir / host_config.llvm_triple, necessary_lib_file)
        else:
            verify_file_exists(lib_dir, necessary_lib_file)

    # Next, we copy over stdatomic.h and bits/stdatomic.h from bionic.
    libc_include_path = paths.ANDROID_DIR / 'bionic' / 'libc' / 'include'
    header_path = lib_dir / 'clang' / version.major_version() / 'include'

    shutil.copy2(libc_include_path / 'stdatomic.h', header_path)

    bits_install_path = header_path / 'bits'
    bits_install_path.mkdir(parents=True, exist_ok=True)
    bits_stdatomic_path = libc_include_path / 'bits' / 'stdatomic.h'
    shutil.copy2(bits_stdatomic_path, bits_install_path)

    # Install license files as NOTICE in the toolchain install dir.
    install_license_files(install_dir)

    # Add an AndroidVersion.txt file.
    version_file_path = install_dir / 'AndroidVersion.txt'
    with version_file_path.open('w') as version_file:
        version_file.write(f'{version.long_version()}\n')
        svn_revision = android_version.get_svn_revision()
        version_file.write(f'based on {svn_revision}\n')
        version_file.write('for additional information on LLVM revision and '
                           'cherry-picks, see clang_source_info.md\n')

    clang_source_info_file = paths.OUT_DIR / 'clang_source_info.md'
    manifest = list(paths.DIST_DIR.glob('manifest_*.xml'))

    # get revision from manifest, update clang_source_info.md
    if manifest:
        manifest_path = os.fspath(manifest[0])
        manifest_context = open(manifest_path).read()
        get_scripts_sha = re.findall(r'name="toolchain/llvm_android" revision="(.*)" /',
                                     manifest_context)[0]
    else:
        get_scripts_sha = 'refs/heads/master'
    with open(clang_source_info_file, 'r') as info:
        info_read = info.read()
    with open(clang_source_info_file, 'w') as info:
        info_read = info_read.replace('{{scripts_sha}}', get_scripts_sha)
        info.write(info_read)

    if clang_source_info_file.exists():
        shutil.copy2(clang_source_info_file, install_dir)

    # Add order file scripts to the toolcahin in share_orderfile_dir
    share_orderfile_dir = install_dir / "share/orderfiles"
    share_orderfile_dir.mkdir(parents=True, exist_ok=True)
    for script_file in paths.ORDERFILE_SCRIPTS_DIR.iterdir():
        if script_file.is_file():
            shutil.copy2(script_file, share_orderfile_dir)
    os.remove(share_orderfile_dir / "orderfile_unittest.py")

    # Remove optrecord.py to avoid auto-filed bugs about call to yaml.load_all
    os.remove(install_dir / 'share/opt-viewer/optrecord.py')

    if host.is_linux:

        # Add BUILD.bazel file.
        with (install_dir / 'BUILD.bazel').open('w') as bazel_file:
            bazel_file.write(
                textwrap.dedent("""\
                    package(default_visibility = ["//visibility:public"])

                    filegroup(
                        name = "binaries",
                        srcs = glob([
                            "bin/*",
                            "lib/*",
                        ]),
                    )

                    filegroup(
                        name = "includes",
                        srcs = glob([
                            "lib/clang/*/include/**",
                        ]),
                    )

                    # Special python3 for u-boot.
                    py_runtime(
                        name = "python3",
                        files = glob(
                            ["python3/**"],
                            exclude = [
                                "**/site-packages/**",
                            ],
                        ),
                        interpreter = "python3/bin/python3",
                        python_version = "PY3",
                        visibility = ["//u-boot:__subpackages__"],
                    )
                    """))

        # Create RBE input files.
        with (install_dir / 'bin' / 'remote_toolchain_inputs').open('w') as inputs_file:
            dependencies = ('clang\n'
                            'clang++\n'
                            'clang.real\n'
                            'clang++.real\n'
                            'clang-tidy\n'
                            'clang-tidy.real\n'
                            '../lib/libc++.so\n'
                            'lld\n'
                            'ld64.lld\n'
                            'ld.lld\n'
                            f'../lib/clang/{version.major_version()}/share\n'
                            f'../lib/clang/{version.major_version()}/lib/linux\n'
                            f'../lib/clang/{version.major_version()}/include\n'
                            f'../lib/libxml2.so.{toolchain_builder.libxml2.lib_version}\n'
                           )
            inputs_file.write(dependencies)

    # Package up the resulting trimmed install/ directory.
    if create_tar:
        tag = host.os_tag
        if isinstance(toolchain_builder.config_list[0], configs.LinuxMuslConfig):
            tag = host.os_tag_musl
        tarball_name = package_name + '-' + tag + '.tar.xz'
        package_path = paths.DIST_DIR / tarball_name
        logger().info(f'Packaging {package_path}')
        utils.create_tarball(install_host_dir, [package_name], package_path)


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
                        value, known_components)
                    raise argparse.ArgumentError(self, error)
            setattr(namespace, self.dest, values.split(','))


    # Parses and returns command line arguments.
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '--build-name', default='dev', help='Release name for the package.')

    parser.add_argument(
        '--enable-assertions',
        action='store_true',
        default=False,
        help='Enable assertions (only affects stage2)')

    lto_group = parser.add_mutually_exclusive_group()
    lto_group.add_argument(
        '--lto',
        action='store_true',
        default=False,
        help='Enable LTO (only affects stage2).  This option increases build time.')
    lto_group.add_argument(
        '--no-lto',
        action='store_false',
        default=False,
        dest='lto',
        help='Disable LTO to speed up build (only affects stage2)')

    bolt_group = parser.add_mutually_exclusive_group()
    bolt_group.add_argument(
        '--bolt',
        action='store_true',
        default=False,
        help='Enable BOLT optimization (only affects stage2).  This option increases build time.')
    bolt_group.add_argument(
        '--no-bolt',
        action='store_false',
        default=False,
        dest='bolt',
        help='Disable BOLT optimization to speed up build (only affects stage2)')
    bolt_group.add_argument(
        '--bolt-instrument',
        action='store_true',
        default=False,
        help='Enable BOLT instrumentation (only affects stage2).')

    pgo_group = parser.add_mutually_exclusive_group()
    pgo_group.add_argument(
        '--pgo',
        action='store_true',
        default=False,
        help='Enable PGO (only affects stage2)')
    pgo_group.add_argument(
        '--no-pgo',
        action='store_false',
        default=False,
        dest='pgo',
        help='Disable PGO (only affects stage2)')

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
        '--skip-source-setup',
        action='store_true',
        default=False,
        help='Skip setting up source code, which can be slow on rotational disks. Only use this if \
        no code has changed since previous build.')

    parser.add_argument(
        '--skip-apply-patches',
        action='store_true',
        default=False,
        help='Skip applying local patches. This allows building a vanilla upstream version.')

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

    parser.add_argument(
        '--run-tests-stage1',
        action=argparse.BooleanOptionalAction,
        default=False,
        help='Run tests in stage1, with clang-tools-extra.')

    parser.add_argument(
        '--skip-tests',
        action='store_true',
        default=False,
        help='Skip clang/llvm check tests after stage1 and stage2.')

    build_group = parser.add_mutually_exclusive_group()
    build_group.add_argument(
        '--build',
        nargs='+',
        help='A list of builders to build. All builders not listed will be skipped.')
    build_group.add_argument(
        '--skip',
        nargs='+',
        help='A list of builders to skip. All builders not listed will be built.')

    bootstrap_group = parser.add_mutually_exclusive_group()
    bootstrap_group.add_argument(
        '--bootstrap-build-only',
        default=False,
        action='store_true',
        help='Build the bootstrap compiler and exit.')
    bootstrap_group.add_argument(
        '--bootstrap-use',
        default='',
        help='Use the given bootstrap compiler.'
    )
    bootstrap_group.add_argument(
        '--bootstrap-use-prebuilt',
        action='store_true',
        default=False,
        help='Skip building the bootstrap compiler and use the prebuilt instead.')

    parser.add_argument(
        '--mlgo',
        action='store_true',
        default=False,
        help='Build with MLGO support.')

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
        help='Build TOT LLVM.')

    parser.add_argument('--llvm-rev', help='Fetch specific LLVM revision from upstream instead of \
                        using toolchain/llvm-project (SHA or \'main\')')

    parser.add_argument(
        '--windows-sdk',
        help='Path to a Windows SDK. If set, it will be used instead of MinGW.'
    )

    musl_group = parser.add_mutually_exclusive_group()
    musl_group.add_argument(
        '--musl',
        action='store_true',
        default=False,
        help='Build against musl libc')
    musl_group.add_argument(
        '--no-musl',
        action='store_false',
        default=True,
        dest='musl',
        help="Don't Build against musl libc")

    incremental_group = parser.add_mutually_exclusive_group()
    incremental_group.add_argument(
        '--incremental',
        action='store_true',
        default=False,
        help='Keep paths.OUT_DIR if it exists')
    incremental_group.add_argument(
        '--no-incremental',
        action='store_false',
        default=False,
        dest='incremental',
        help='Delete paths.OUT_DIR if it exists')

    parser.add_argument(
        '--sccache',
        action='store_true',
        default=False,
        help='Use sccache to speed up development builds. (Do not use for release builds)')

    return parser.parse_args()


def main():
    logging.basicConfig(level=logging.DEBUG)
    args = parse_args()

    if paths.OUT_DIR.exists():
        if not args.incremental:
            logger().info(f'Removing {paths.OUT_DIR}')
            utils.clean_out_dir()
        else:
            logger().info(f'Keeping older build in {paths.OUT_DIR}')

    timer.Timer.register_atexit(paths.DIST_DIR / 'build_times.txt')

    if args.skip_build:
        # Skips all builds
        BuilderRegistry.add_filter(lambda name: False)
    elif args.skip:
        BuilderRegistry.add_skips(args.skip)
    elif args.build:
        BuilderRegistry.add_builds(args.build)

    do_bolt = args.bolt and not args.debug and not args.build_instrumented
    do_bolt_instrument = args.bolt_instrument and not args.debug and not args.build_instrumented
    do_runtimes = not args.skip_runtimes
    do_package = not args.skip_package
    do_strip = not args.no_strip
    do_strip_host_package = do_strip and not args.debug and not (args.build_llvm_next)
    build_lldb = 'lldb' not in args.no_build
    mlgo = args.mlgo
    musl = args.musl
    sccache = args.sccache

    host_configs = [configs.host_config(musl)]

    android_version.set_llvm_next(args.build_llvm_next)

    if (do_bolt or do_bolt_instrument) and hosts.build_host().is_darwin:
        raise ValueError("BOLT is not supported for Mach-O binaries. https://github.com/llvm/llvm-project/blob/main/bolt/README.md#input-binary-requirements")

    if mlgo and hosts.build_host().is_darwin:
        raise ValueError("MLGO is not supported for macOS.")

    need_host = hosts.build_host().is_darwin or ('linux' not in args.no_build)
    need_windows_libcxx = hosts.build_host().is_linux and do_runtimes
    need_windows = hosts.build_host().is_linux and ('windows' not in args.no_build)

    logger().info('do_build=%r do_stage1=%r do_stage2=%r do_runtimes=%r do_package=%r need_windows=%r lto=%r bolt=%r musl=%r' %
                  (not args.skip_build, BuilderRegistry.should_build('stage1'), BuilderRegistry.should_build('stage2'),
                  do_runtimes, do_package, need_windows, args.lto, args.bolt, args.musl))

    if paths.get_tensorflow_path() is None:
        if mlgo:
            raise ValueError("MLGO requires tensorflow. Tensorflow not found.")
        else:
            logger().info('Tensorflow not found.')
    else:
        logger().info('Tensorflow found: ' + paths.get_tensorflow_path())

    # Clone sources to be built and apply patches.
    if not args.skip_source_setup:
        source_manager.setup_sources(llvm_rev=args.llvm_rev, skip_apply_patches=args.skip_apply_patches)

    # Build the stage1 Clang for the build host
    instrumented = hosts.build_host().is_linux and args.build_instrumented

    if not args.bootstrap_use_prebuilt and not args.bootstrap_use:
        stage1 = builders.Stage1Builder(host_configs)
        stage1.build_name = 'stage1'
        stage1.svn_revision = android_version.get_svn_revision()
        # Build lldb for lldb-tblgen. It will be used to build lldb-server and windows lldb.
        stage1.build_lldb = build_lldb
        stage1.enable_mlgo = mlgo
        stage1.build_extra_tools = args.run_tests_stage1
        stage1.use_sccache = sccache
        stage1.build()
        if hosts.build_host().is_linux:
            add_header_links('stage1', host_config=configs.host_config(musl))
        # stage1 test is off by default, turned on by --run-tests-stage1,
        # and suppressed by --skip-tests.
        if not args.skip_tests and args.run_tests_stage1:
            stage1.test()
        set_default_toolchain(stage1.installed_toolchain)
    if args.bootstrap_use:
        with timer.Timer(f'extract_bootstrap'):
            utils.extract_tarball(paths.OUT_DIR, args.bootstrap_use)
        set_default_toolchain(toolchains.Toolchain(paths.OUT_DIR / 'stage1-install', paths.OUT_DIR / 'stage1'))
    if args.bootstrap_build_only:
        with timer.Timer(f'package_bootstrap'):
            utils.create_tarball(paths.OUT_DIR, ['stage1', 'stage1-install'], paths.DIST_DIR / 'stage1-install.tar.xz')
        return

    if build_lldb:
        # Swig is needed for both host and windows lldb.
        swig_builder = builders.SwigBuilder(host_configs)
        swig_builder.build()
    else:
        swig_builder = None

    if args.pgo:
        profdata = extract_pgo_profile()
    else:
        profdata = None

    if args.bolt:
        clang_bolt_fdata = extract_bolt_profile()
    else:
        clang_bolt_fdata = None

    if need_host:
        stage2 = builders.Stage2Builder(host_configs)
        stage2.build_name = args.build_name
        stage2.svn_revision = android_version.get_svn_revision()
        stage2.debug_build = args.debug
        stage2.enable_assertions = args.enable_assertions
        stage2.lto = args.lto
        stage2.build_instrumented = instrumented
        stage2.enable_mlgo = mlgo
        stage2.bolt_optimize = args.bolt
        stage2.bolt_instrument = args.bolt_instrument
        stage2.profdata_file = profdata
        stage2.build_32bit_runtimes = hosts.build_host().is_linux

        libzstd_builder = builders.ZstdBuilder(host_configs)
        libzstd_builder.build()
        stage2.libzstd = libzstd_builder

        libxml2_builder = builders.LibXml2Builder(host_configs)
        libxml2_builder.build()
        stage2.libxml2 = libxml2_builder

        stage2.build_lldb = build_lldb
        if build_lldb:
            stage2.swig_executable = swig_builder.install_dir / 'bin' / 'swig'

            xz_builder = builders.XzBuilder(host_configs)
            xz_builder.build()
            stage2.liblzma = xz_builder

            libncurses = builders.LibNcursesBuilder(host_configs)
            libncurses.build()
            stage2.libncurses = libncurses

            libedit_builder = builders.LibEditBuilder(host_configs)
            libedit_builder.libncurses = libncurses
            libedit_builder.build()
            stage2.libedit = libedit_builder

        stage2_tags = []
        # Annotate the version string with build options.
        to_tag = lambda c, tag : ('+' if c else '-') + tag
        stage2_tags.append(to_tag(profdata, 'pgo'))
        stage2_tags.append(to_tag(clang_bolt_fdata, 'bolt'))
        stage2_tags.append(to_tag(stage2.lto, 'lto'))
        stage2_tags.append(to_tag(stage2.enable_mlgo, 'mlgo'))
        if args.build_llvm_next:
            stage2_tags.append('ANDROID_LLVM_NEXT')
        stage2.build_tags = stage2_tags

        stage2.build()

        if do_bolt:
            bolt_optimize(stage2, clang_bolt_fdata)

        if not (stage2.build_instrumented or stage2.debug_build):
            set_default_toolchain(stage2.installed_toolchain)

        Builder.output_toolchain = stage2.installed_toolchain
        if hosts.build_host().is_linux and do_runtimes:
            build_runtimes(build_lldb_server=build_lldb,
                           stage='stage2',
                           host_config=configs.host_config(musl),
                           host_32bit_config=configs.host_32bit_config(musl))

    if need_windows or need_windows_libcxx:
        # Host sysroots are currently setup only for Windows
        builders.HostSysrootsBuilder().build()
        if args.windows_sdk:
            win_sdk.set_path(Path(args.windows_sdk))
        win_builder, win_lldb_bins = build_llvm_for_windows(
            enable_assertions=args.enable_assertions,
            enable_lto=args.lto,
            profdata_file=profdata,
            build_name=args.build_name,
            build_lldb=build_lldb,
            swig_builder=swig_builder,
            full_build=need_windows,
            build_simpleperf_readelf=need_host)

    # stage2 test is on when stage2 is enabled unless --skip-tests or
    # on instrumented builds.
    need_tests = not args.skip_tests and need_host and \
            BuilderRegistry.should_build('stage2') and \
            (not args.build_instrumented)
    if need_tests:
       stage2.test()

    # Instrument with llvm-bolt. Must be the last build step to prevent other
    # build steps generating BOLT profiles.
    if need_host:
        if do_bolt_instrument:
            bolt_instrument(stage2)

    if do_package and need_host:
        package_toolchain(
            stage2,
            strip=do_strip_host_package,
            with_runtimes=do_runtimes,
            create_tar=args.create_tar,
            llvm_next=args.build_llvm_next)

    if do_package and need_windows:
        package_toolchain(
            win_builder,
            necessary_bin_files=win_lldb_bins,
            strip=do_strip,
            with_runtimes=do_runtimes,
            create_tar=args.create_tar)

    return 0


if __name__ == '__main__':
    main()
