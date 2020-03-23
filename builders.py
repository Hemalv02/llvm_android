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
"""Builders for different targets."""

from pathlib import Path
import os
from typing import Dict, List, Optional

import android_version
import configs
import constants
import hosts
import paths
import toolchains
import utils

ORIG_ENV = dict(os.environ)

class Builder:  # pylint: disable=too-few-public-methods
    """Base builder type."""
    name: str = ""

    def build(self) -> None:
        """Builds the target."""
        raise NotImplementedError


class CMakeBuilder(Builder):
    """Builder for cmake targets."""
    toolchain: toolchains.Toolchain
    config: configs.Config
    src_dir: Path
    remove_cmake_cache: bool = False
    ninja_target: Optional[str] = None
    install: bool = True
    install_dir: Path

    @property
    def target_os(self) -> hosts.Host:
        return self.config.target_os

    @property
    def output_path(self) -> Path:
        """The path for intermediate results."""
        return paths.OUT_DIR / self.name

    @property
    def cmake_defines(self) -> Dict[str, str]:
        """CMake defines."""
        cflags = self.config.cflags + self.additional_cflags
        ldflags = self.config.ldflags + self.additional_ldflags
        cflags_str = ' '.join(cflags)
        ldflags_str = ' '.join(ldflags)
        defines: Dict[str, str] = {
            'CMAKE_C_COMPILER': str(self.toolchain.cc),
            'CMAKE_CXX_COMPILER': str(self.toolchain.cxx),

            'CMAKE_ASM_FLAGS':  cflags_str,
            'CMAKE_C_FLAGS': cflags_str,
            'CMAKE_CXX_FLAGS': cflags_str,

            'CMAKE_EXE_LINKER_FLAGS': ldflags_str,
            'CMAKE_SHARED_LINKER_FLAGS': ldflags_str,
            'CMAKE_MODULE_LINKER_FLAGS': ldflags_str,

            'CMAKE_BUILD_TYPE': 'Release',
            'CMAKE_INSTALL_PREFIX': str(self.install_dir),

            'CMAKE_MAKE_PROGRAM': str(paths.NINJA_BIN_PATH),

            'CMAKE_FIND_ROOT_PATH_MODE_INCLUDE': 'ONLY',
            'CMAKE_FIND_ROOT_PATH_MODE_LIBRARY': 'ONLY',
            'CMAKE_FIND_ROOT_PATH_MODE_PACKAGE': 'ONLY',
            'CMAKE_FIND_ROOT_PATH_MODE_PROGRAM': 'NEVER',
        }
        if self.config.sysroot:
            defines['CMAKE_SYSROOT'] = str(self.config.sysroot)
        return defines

    @property
    def additional_cflags(self) -> List[str]:
        """Additional cflags to use."""
        return []

    @property
    def additional_ldflags(self) -> List[str]:
        """Additional ldflags to use."""
        return []

    @property
    def env(self) -> Dict[str, str]:
        """Environment variables used when building."""
        return ORIG_ENV

    @staticmethod
    def _rm_cmake_cache(cache_dir: Path):
        for dirpath, dirs, files in os.walk(cache_dir):
            if 'CMakeCache.txt' in files:
                os.remove(os.path.join(dirpath, 'CMakeCache.txt'))
            if 'CMakeFiles' in dirs:
                utils.rm_tree(os.path.join(dirpath, 'CMakeFiles'))

    def build(self) -> None:
        if self.remove_cmake_cache:
            self._rm_cmake_cache(self.output_path)

        cmake_cmd: List[str] = [str(paths.CMAKE_BIN_PATH), '-G', 'Ninja']

        cmake_cmd.extend(f'-D{key}={val}' for key, val in self.cmake_defines.items())
        cmake_cmd.append(str(self.src_dir))

        os.makedirs(self.output_path, exist_ok=True)

        utils.check_call(cmake_cmd, cwd=self.output_path, env=self.env)

        ninja_cmd: List[str] = [str(paths.NINJA_BIN_PATH)]
        if self.ninja_target:
            ninja_cmd.append(self.ninja_target)
        utils.check_call(ninja_cmd, cwd=self.output_path, env=self.env)

        if self.install:
            utils.check_call([paths.NINJA_BIN_PATH, 'install'],
                             cwd=self.output_path, env=self.env)


class LLVMBuilder(CMakeBuilder):
    """Builder for LLVM project."""

    src_dir: Path = paths.LLVM_PATH / 'llvm'
    build_name: str
    svn_revision: bool
    llvm_projects: List[str]
    llvm_targets: List[str]

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines

        defines['LLVM_ENABLE_PROJECTS'] = ';'.join(self.llvm_projects)

        defines['LLVM_ENABLE_ASSERTIONS'] = 'OFF'
        # https://github.com/android-ndk/ndk/issues/574 - Don't depend on libtinfo.
        defines['LLVM_ENABLE_TERMINFO'] = 'OFF'
        defines['LLVM_ENABLE_THREADS'] = 'ON'
        defines['LLVM_USE_NEWPM'] = 'ON'
        defines['LLVM_LIBDIR_SUFFIX'] = '64'
        defines['LLVM_VERSION_PATCH'] = android_version.patch_level
        defines['CLANG_VERSION_PATCHLEVEL'] = android_version.patch_level
        defines['CLANG_REPOSITORY_STRING'] = (
            'https://android.googlesource.com/toolchain/llvm-project')
        defines['BUG_REPORT_URL'] = 'https://github.com/android-ndk/ndk/issues'

        if self.target_os.is_darwin:
            # This will be used to set -mmacosx-version-min. And helps to choose SDK.
            # To specify a SDK, set CMAKE_OSX_SYSROOT or SDKROOT environment variable.
            defines['CMAKE_OSX_DEPLOYMENT_TARGET'] = constants.MAC_MIN_VERSION

        # http://b/111885871 - Disable building xray because of MacOS issues.
        defines['COMPILER_RT_BUILD_XRAY'] = 'OFF'

        defines['LLVM_TARGETS_TO_BUILD'] = ';'.join(self.llvm_targets)
        defines['LLVM_BUILD_LLVM_DYLIB'] = 'ON'

        defines['CLANG_VENDOR'] = 'Android ({} based on {})'.format(
            self.build_name,
            self.svn_revision)

        defines['LLVM_BINUTILS_INCDIR'] = str(paths.ANDROID_DIR / 'toolchain' /
                                              'binutils' / 'binutils-2.27' / 'include')

        defines['LLVM_ENABLE_LIBCXX'] = 'ON'
        return defines
