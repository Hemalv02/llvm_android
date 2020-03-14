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
"""APIs for build configurations."""

from pathlib import Path
from typing import List, Optional

import hosts
import paths

class Config:
    """Base configuration."""

    target_os: hosts.Host

    cflags: List[str]

    ldflags: List[str]

    sysroot: Optional[Path]


class _BaseConfig(Config):  # pylint: disable=abstract-method
    """Base configuration when building for host."""

    use_lld: bool = True
    is_32_bit: bool = False

    @property
    def cflags(self) -> List[str]:  # type: ignore
        cflags: List[str] = [f'-fdebug-prefix-map={paths.ANDROID_DIR}=']
        cflags.extend(f'-B{d}' for d in self.bin_dirs)
        return cflags

    @property
    def ldflags(self) -> List[str]:  # type: ignore
        ldflags: List[str] = []
        for lib_dir in self.lib_dirs:
            ldflags.append(f'-B{lib_dir}')
            ldflags.append(f'-L{lib_dir}')
        if self.use_lld:
            ldflags.append('-fuse-ld=lld')
        return ldflags

    @property
    def bin_dirs(self) -> List[Path]:
        """Paths to binaries used in cflags."""
        return []

    @property
    def lib_dirs(self) -> List[Path]:
        """Paths to libraries used in ldflags."""
        return []


class DarwinConfig(_BaseConfig):
    """Configuration for Darwin targets."""

    target_os: hosts.Host = hosts.Host.Darwin
    use_lld: bool = False

    @property
    def sysroot(self) -> Optional[Path]:  # type: ignore
        return None


class _GccConfig(_BaseConfig):  # pylint: disable=abstract-method
    """Base config to use gcc libs."""

    gcc_root: Path
    gcc_triple: str
    gcc_ver: str

    @property
    def bin_dirs(self) -> List[Path]:
        return [self.gcc_root / self.gcc_triple / 'bin']

    @property
    def lib_dirs(self) -> List[Path]:
        gcc_lib_dir = self.gcc_root / 'lib' / 'gcc' / self.gcc_triple / self.gcc_ver
        if self.is_32_bit:
            gcc_lib_dir = gcc_lib_dir / '32'
            gcc_builtin_dir = self.gcc_root / self.gcc_triple / 'lib32'
        else:
            gcc_builtin_dir = self.gcc_root / self.gcc_triple / 'lib64'
        return [gcc_lib_dir, gcc_builtin_dir]


class LinuxConfig(_GccConfig):
    """Configuration for Linux targets."""

    target_os: hosts.Host = hosts.Host.Linux
    gcc_root: Path = (paths.ANDROID_DIR / 'prebuilts' / 'gcc' / target_os.os_tag /
                      'host' / 'x86_64-linux-glibc2.17-4.8')
    gcc_triple: str = 'x86_64-linux'
    gcc_ver: str = '4.8.3'

    @property
    def sysroot(self) -> Optional[Path]:  # type: ignore
        return (paths.ANDROID_DIR / 'prebuilts' / 'gcc' / self.target_os.os_tag /
                'host' / 'x86_64-linux-glibc2.17-4.8' / 'sysroot')


class WindowsConfig(_GccConfig):
    """Configuration for Windows targets."""

    target_os: hosts.Host = hosts.Host.Windows
    gcc_root: Path = (paths.ANDROID_DIR / 'prebuilts' / 'gcc' / target_os.os_tag /
                      'host' / 'x86_64-w64-mingw32-4.8')
    triple: str = 'x86_64-linux'
    gcc_ver: str = '4.8.3'


def _get_default_host_config() -> Config:
    """Returns the Config matching the current machine."""
    return {
        hosts.Host.Linux: LinuxConfig,
        hosts.Host.Darwin: DarwinConfig,
        hosts.Host.Windows: WindowsConfig
    }[hosts.build_host()]()


_HOST_CONFIG: Config = _get_default_host_config()


def host_config() -> Config:
    """Returns the cached Host matching the current machine."""
    global _HOST_CONFIG  # pylint: disable=global-statement
    return _HOST_CONFIG
