#
# Copyright (C) 2020 The Android Open Source Project
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
"""Helpers for paths."""

import os
from pathlib import Path
import string
from typing import Optional

import android_version
import constants
import hosts

SCRIPTS_DIR: Path = Path(__file__).resolve().parent
ANDROID_DIR: Path = SCRIPTS_DIR.parents[1]
OUT_DIR: Path = Path(os.environ.get('OUT_DIR', ANDROID_DIR / 'out')).resolve()
SYSROOTS: Path = OUT_DIR / 'sysroots'
LLVM_PATH: Path = OUT_DIR / 'llvm-project'
PREBUILTS_DIR: Path = ANDROID_DIR / 'prebuilts'
EXTERNAL_DIR: Path = ANDROID_DIR / 'external'
TOOLCHAIN_DIR: Path = ANDROID_DIR / 'toolchain'
TOOLCHAIN_UTILS_DIR: Path = EXTERNAL_DIR / 'toolchain-utils'
TOOLCHAIN_LLVM_PATH: Path = TOOLCHAIN_DIR / 'llvm-project'

CLANG_PREBUILT_DIR: Path = (PREBUILTS_DIR / 'clang' / 'host' / hosts.build_host().os_tag
                            / constants.CLANG_PREBUILT_VERSION)
CLANG_PREBUILT_LIBCXX_HEADERS: Path = CLANG_PREBUILT_DIR / 'include' / 'c++' / 'v1'
BIONIC_HEADERS: Path = ANDROID_DIR / 'bionic' / 'libc' / 'include'

GO_BIN_PATH: Path = PREBUILTS_DIR / 'go' / hosts.build_host().os_tag / 'bin'
CMAKE_BIN_PATH: Path = PREBUILTS_DIR / 'cmake' / hosts.build_host().os_tag / 'bin' / 'cmake'
NINJA_BIN_PATH: Path = PREBUILTS_DIR / 'build-tools' / hosts.build_host().os_tag / 'bin' / 'ninja'

LIBNCURSES_SRC_DIR: Path = EXTERNAL_DIR / 'libncurses'
LIBEDIT_SRC_DIR: Path = EXTERNAL_DIR / 'libedit'
SWIG_SRC_DIR: Path = EXTERNAL_DIR / 'swig'
XZ_SRC_DIR: Path = TOOLCHAIN_DIR / 'xz'
LIBXML2_SRC_DIR: Path = EXTERNAL_DIR / 'libxml2'

NDK_BASE: Path = TOOLCHAIN_DIR / 'prebuilts' /'ndk' / constants.NDK_VERSION
NDK_LIBCXX_HEADERS: Path = NDK_BASE / 'sources' / 'cxx-stl' / 'llvm-libc++'/ 'include'
NDK_LIBCXXABI_HEADERS: Path = NDK_BASE / 'sources' / 'cxx-stl' / 'llvm-libc++abi' / 'include'
NDK_SUPPORT_HEADERS: Path = NDK_BASE / 'sources' / 'android' / 'support' / 'include'

GCC_ROOT: Path = PREBUILTS_DIR / 'gcc' / hosts.build_host().os_tag
MINGW_ROOT: Path = PREBUILTS_DIR / 'gcc' / 'linux-x86' / 'host' / 'x86_64-w64-mingw32-4.8'

_WIN_ZLIB_PATH: Path = (PREBUILTS_DIR / 'clang' / 'host' / 'windows-x86' /
                        'toolchain-prebuilts' / 'zlib')
WIN_ZLIB_INCLUDE_PATH: Path = _WIN_ZLIB_PATH / 'include'
WIN_ZLIB_LIB_PATH: Path = _WIN_ZLIB_PATH / 'lib'

KYTHE_RUN_EXTRACTOR = (PREBUILTS_DIR / 'build-tools' / hosts.build_host().os_tag / 'bin' /
                       'runextractor')
KYTHE_CXX_EXTRACTOR = (PREBUILTS_DIR / 'clang-tools' / hosts.build_host().os_tag / 'bin' /
                       'cxx_extractor')
KYTHE_OUTPUT_DIR = OUT_DIR / 'kythe-files'


def pgo_profdata_filename() -> str:
    svn_revision = android_version.get_svn_revision_number()
    return f'r{svn_revision}.profdata'


def pgo_profdata_tarname() -> str:
    return pgo_profdata_filename() + '.tar.bz2'


def pgo_profdata_tar() -> Optional[Path]:
    profile = (PREBUILTS_DIR / 'clang' / 'host' / 'linux-x86' / 'profiles' /
               pgo_profdata_tarname())
    return profile if profile.exists() else None


def get_package_install_path(host: hosts.Host, package_name) -> Path:
    return OUT_DIR / 'install' / host.os_tag / package_name

def get_python_dir(host: hosts.Host) -> Path:
    """Returns the path to python for a host."""
    return PREBUILTS_DIR / 'python' / host.os_tag

def get_python_executable(host: hosts.Host) -> Path:
    """Returns the path to python executable for a host."""
    python_root = get_python_dir(host)
    return {
        hosts.Host.Linux: python_root / 'bin' / 'python3.8',
        hosts.Host.Darwin: python_root / 'bin' / 'python3.8',
        hosts.Host.Windows: python_root / 'python.exe',
    }[host]

def get_python_include_dir(host: hosts.Host) -> Path:
    """Returns the path to python include dir for a host."""
    python_root = get_python_dir(host)
    return {
        hosts.Host.Linux: python_root / 'include' / 'python3.8',
        hosts.Host.Darwin: python_root / 'include' / 'python3.8',
        hosts.Host.Windows: python_root / 'include',
    }[host]

def get_python_lib(host: hosts.Host) -> Path:
    """Returns the path to python lib for a host."""
    python_root = get_python_dir(host)
    return {
        hosts.Host.Linux: python_root / 'lib' / 'libpython3.8.so',
        hosts.Host.Darwin: python_root / 'lib' / 'libpython3.8.dylib',
        hosts.Host.Windows: python_root / 'libs' / 'python38.lib',
    }[host]

def get_python_dynamic_lib(host: hosts.Host) -> Path:
    """Returns the path to python runtime dynamic lib for a host."""
    python_root = get_python_dir(host)
    return {
        hosts.Host.Linux: python_root / 'lib' / 'libpython3.8.so.1.0',
        hosts.Host.Darwin: python_root / 'lib' / 'libpython3.8.dylib',
        hosts.Host.Windows: python_root / 'python38.dll',
    }[host]
