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

import constants
import hosts

ANDROID_DIR: Path = Path(__file__).resolve().parents[2]
OUT_DIR: Path = Path(os.environ.get('OUT_DIR', ANDROID_DIR / 'out')).resolve()
LLVM_PATH: Path = OUT_DIR / 'llvm-project'
PREBUILTS_DIR: Path = ANDROID_DIR / 'prebuilts'
EXTERNAL_DIR: Path = ANDROID_DIR / 'external'

CLANG_PREBUILT_DIR: Path = (PREBUILTS_DIR / 'clang' / 'host' / hosts.build_host().os_tag
                            / constants.CLANG_PREBUILT_VERSION)
CLANG_PREBUILT_LIBCXX_HEADERS: Path = CLANG_PREBUILT_DIR / 'include' / 'c++' / 'v1'
BIONIC_HEADERS: Path = ANDROID_DIR / 'bionic' / 'libc' / 'include'

CMAKE_BIN_PATH: Path = PREBUILTS_DIR / 'cmake' / hosts.build_host().os_tag / 'bin' / 'cmake'
NINJA_BIN_PATH: Path = PREBUILTS_DIR / 'ninja' / hosts.build_host().os_tag / 'ninja'

LIBEDIT_SRC_DIR: Path = EXTERNAL_DIR / 'libedit'
SWIG_SRC_DIR: Path = EXTERNAL_DIR / 'swig'

NDK_BASE: Path = ANDROID_DIR / 'toolchain' / 'prebuilts' /'ndk' / constants.NDK_VERSION
NDK_LIBCXX_HEADERS: Path = NDK_BASE / 'sources' / 'cxx-stl' / 'llvm-libc++'/ 'include'
NDK_LIBCXXABI_HEADERS: Path = NDK_BASE / 'sources' / 'cxx-stl' / 'llvm-libc++abi' / 'include'
NDK_SUPPORT_HEADERS: Path = NDK_BASE / 'sources' / 'android' / 'support' / 'include'

GCC_ROOT: Path = PREBUILTS_DIR / 'gcc' / hosts.build_host().os_tag

_WIN_ZLIB_PATH: Path = (PREBUILTS_DIR / 'clang' / 'host' / 'windows-x86' /
                        'toolchain-prebuilts' / 'zlib')
WIN_ZLIB_INCLUDE_PATH: Path = _WIN_ZLIB_PATH / 'include'
WIN_ZLIB_LIB_PATH: Path = _WIN_ZLIB_PATH / 'lib'

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

def get_libedit_include_dir(libedit_root: Path) -> Path:
    """Returns the path to libedit include for a host."""
    return libedit_root / 'include'

def get_libedit_lib(libedit_root: Path, host: hosts.Host) -> Path:
    """Returns the path to libedit lib for a host."""
    if host.is_darwin:
        return libedit_root / 'lib' / 'libedit.dylib'
    if host.is_linux:
        return libedit_root / 'lib' / 'libedit.so.0'
    raise NotImplementedError(f"Unsupported host {host.name}")
