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

    name: str
    target_os: hosts.Host
    target_arch: hosts.Arch = hosts.Arch.X86_64

    """Additional config data that a builder can specify."""
    extra_config = None

    @property
    def cflags(self) -> List[str]:
        """Returns a list of flags for c compiler."""
        raise NotImplementedError()

    @property
    def cxxflags(self) -> List[str]:
        """Returns a list of flags used for cxx compiler."""
        return self.cflags

    @property
    def ldflags(self) -> List[str]:
        """Returns a list of flags for static linker."""
        raise NotImplementedError()

    def __str__(self) -> str:
        return self.target_os.name

    @property
    def output_suffix(self) -> str:
        """The suffix of output directory name."""
        raise NotImplementedError()

    sysroot: Optional[Path] = None


class _BaseConfig(Config):  # pylint: disable=abstract-method
    """Base configuration."""

    use_lld: bool = True
    is_32_bit: bool = False
    target_os: hosts.Host

    @property
    def cflags(self) -> List[str]:
        cflags: List[str] = [f'-fdebug-prefix-map={paths.ANDROID_DIR}=']
        cflags.extend(f'-B{d}' for d in self.bin_dirs)
        return cflags

    @property
    def ldflags(self) -> List[str]:
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

    @property
    def output_suffix(self) -> str:
        return f'-{self.target_os.value}'


class DarwinConfig(_BaseConfig):
    """Configuration for Darwin targets."""

    target_os: hosts.Host = hosts.Host.Darwin
    use_lld: bool = False

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        # Fails if an API used is newer than what specified in -mmacosx-version-min.
        cflags.append('-Werror=unguarded-availability')
        return cflags


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
    sysroot: Optional[Path] = (paths.GCC_ROOT / 'host' / 'x86_64-linux-glibc2.17-4.8' / 'sysroot')
    gcc_root: Path = (paths.GCC_ROOT / 'host' / 'x86_64-linux-glibc2.17-4.8')
    gcc_triple: str = 'x86_64-linux'
    gcc_ver: str = '4.8.3'

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append(f'--gcc-toolchain={self.gcc_root}')
        return cflags


class WindowsConfig(_GccConfig):
    """Configuration for Windows targets."""

    target_os: hosts.Host = hosts.Host.Windows
    gcc_root: Path = (paths.GCC_ROOT / 'host' / 'x86_64-w64-mingw32-4.8')
    sysroot: Optional[Path] = gcc_root / 'x86_64-w64-mingw32'
    gcc_triple: str = 'x86_64-w64-mingw32'
    gcc_ver: str = '4.8.3'

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('--target=x86_64-pc-windows-gnu')
        cflags.append('-D_LARGEFILE_SOURCE')
        cflags.append('-D_FILE_OFFSET_BITS=64')
        cflags.append('-D_WIN32_WINNT=0x0600')
        cflags.append('-DWINVER=0x0600')
        cflags.append('-D__MSVCRT_VERSION__=0x1400')
        return cflags


class AndroidConfig(_BaseConfig):
    """Config for Android targets."""

    target_os: hosts.Host = hosts.Host.Android

    llvm_triple: str
    ndk_triple: str

    _toolchain_path: Path
    _toolchain_lib: Path

    static: bool = False
    platform: bool = False

    @property
    def sysroot(self) -> Optional[Path]:  # type: ignore
        """Returns sysroot path."""
        platform_or_ndk = 'platform' if self.platform else 'ndk'
        return paths.OUT_DIR / 'sysroots' / platform_or_ndk / self.target_arch.ndk_arch

    @property
    def _toolchain_builtins(self) -> Path:
        """The path with libgcc.a to include in linker search path."""
        return (paths.GCC_ROOT / self._toolchain_path / '..' / 'lib' / 'gcc' /
                self._toolchain_path.name / '4.9.x')

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        ldflags.append(f'-L{self._toolchain_builtins}')
        ldflags.append('-Wl,-z,defs')
        ldflags.append(f'-L{self._toolchain_lib}')
        ldflags.append('-Wl,--gc-sections')
        ldflags.append('-Wl,--build-id=sha1')
        ldflags.append('-pie')
        if self.static:
            ldflags.append('-static')
        if not self.platform:
            libcxx_libs = (paths.NDK_BASE / 'toolchains' / 'llvm' / 'prebuilt'
                           / 'linux-x86_64' / 'sysroot' / 'usr' / 'lib' / self.ndk_triple)
            ldflags.append('-L{}'.format(libcxx_libs / str(self.api_level)))
            ldflags.append('-L{}'.format(libcxx_libs))
        return ldflags

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        toolchain_bin = paths.GCC_ROOT / self._toolchain_path / 'bin'
        api_level = 10000 if self.platform else self.api_level
        cflags.append(f'--target={self.llvm_triple}')
        cflags.append(f'-B{toolchain_bin}')
        cflags.append(f'-D__ANDROID_API__={api_level}')
        cflags.append('-ffunction-sections')
        cflags.append('-fdata-sections')
        return cflags

    @property
    def _libcxx_header_dirs(self) -> List[Path]:
        if self.platform:  # pylint: disable=no-else-return
            # <prebuilts>/include/c++/v1 includes the cxxabi headers
            return [
                paths.CLANG_PREBUILT_LIBCXX_HEADERS,
                paths.BIONIC_HEADERS,
            ]
        else:
            return [
                paths.NDK_LIBCXX_HEADERS,
                paths.NDK_LIBCXXABI_HEADERS,
                paths.NDK_SUPPORT_HEADERS,
            ]

    @property
    def cxxflags(self) -> List[str]:
        cxxflags = super().cxxflags
        # Skip implicit C++ headers and explicitly include C++ header paths.
        cxxflags.append('-nostdinc++')
        cxxflags.extend(f'-isystem {d}' for d in self._libcxx_header_dirs)
        return cxxflags

    @property
    def api_level(self) -> int:
        if self.static or self.platform:
            return 29
        if self.target_arch in [hosts.Arch.ARM, hosts.Arch.I386]:
            return 16
        return 21

    def __str__(self) -> str:
        return (f'{self.target_os.name}-{self.target_arch.name} ' +
                f'(platform={self.platform} static={self.static} {self.extra_config})')

    @property
    def output_suffix(self) -> str:
        suffix = f'-{self.target_arch.value}'
        if not self.platform:
            suffix += '-ndk-cxx'
        return suffix


class AndroidARMConfig(AndroidConfig):
    """Configs for android arm targets."""
    target_arch: hosts.Arch = hosts.Arch.ARM
    llvm_triple: str = 'arm-linux-android'
    ndk_triple: str = 'arm-linux-androideabi'
    _toolchain_path: Path = Path(f'arm/{ndk_triple}-4.9/{ndk_triple}')
    _toolchain_lib: Path = (paths.NDK_BASE / 'toolchains' / f'{ndk_triple}-4.9' /
                            'prebuilt' / 'linux-x86_64' / ndk_triple / 'lib')

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-march=armv7-a')
        return cflags


class AndroidAArch64Config(AndroidConfig):
    """Configs for android arm64 targets."""
    target_arch: hosts.Arch = hosts.Arch.AARCH64
    llvm_triple: str = 'aarch64-linux-android'
    ndk_triple: str = llvm_triple
    _toolchain_path: Path = Path(f'aarch64/{ndk_triple}-4.9/{ndk_triple}')
    _toolchain_lib: Path = (paths.NDK_BASE / 'toolchains' / f'{ndk_triple}-4.9' /
                            'prebuilt' / 'linux-x86_64' / ndk_triple / 'lib64')


class AndroidX64Config(AndroidConfig):
    """Configs for android x86_64 targets."""
    target_arch: hosts.Arch = hosts.Arch.X86_64
    llvm_triple: str = 'x86_64-linux-android'
    ndk_triple: str = llvm_triple
    _toolchain_path: Path = Path(f'x86/{ndk_triple}-4.9/{ndk_triple}')
    _toolchain_lib: Path = (paths.NDK_BASE / 'toolchains' / 'x86_64-4.9' /
                            'prebuilt' / 'linux-x86_64' / ndk_triple / 'lib64')


class AndroidI386Config(AndroidConfig):
    """Configs for android x86 targets."""
    target_arch: hosts.Arch = hosts.Arch.I386
    llvm_triple: str = 'i686-linux-android'
    ndk_triple: str = llvm_triple
    _toolchain_path: Path = Path('x86/x86_64-linux-android-4.9/x86_64-linux-android')
    _toolchain_lib: Path = (paths.NDK_BASE / 'toolchains' / 'x86-4.9' /
                            'prebuilt' / 'linux-x86_64' / ndk_triple / 'lib')

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-m32')
        return cflags

    @property
    def _toolchain_builtins(self) -> Path:
        # The 32-bit libgcc.a is sometimes in a separate subdir
        return super()._toolchain_builtins / '32'


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

def android_configs(platform: bool = True,
                    static: bool = False,
                    extra_config=None) -> List[Config]:
    """Returns a list of configs for android builds."""
    configs = [
        AndroidARMConfig(),
        AndroidAArch64Config(),
        AndroidI386Config(),
        AndroidX64Config(),
    ]
    for config in configs:
        config.static = static
        config.platform = platform
        config.extra_config = extra_config
    # List is not covariant. Explicit convert is required to make it List[Config].
    return list(configs)
