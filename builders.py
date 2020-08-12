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
"""Builder instances for various targets."""

from pathlib import Path
import os
import shutil
import textwrap
from typing import cast, Dict, List, Optional, Set

import base_builders
import configs
import constants
import hosts
import mapfile
import paths
import toolchains
import utils
import win_sdk

class AsanMapFileBuilder(base_builders.Builder):
    name: str = 'asan-mapfile'
    config_list: List[configs.Config] = configs.android_configs()

    @property
    def toolchain(self) -> toolchains.Toolchain:
        return toolchains.get_runtime_toolchain()

    def _build_config(self) -> None:
        arch = self._config.target_arch
        # We can not build asan_test using current CMake building system. Since
        # those files are not used to build AOSP, we just simply touch them so that
        # we can pass the build checks.
        asan_test_path = self.output_toolchain.path / 'test' / arch.llvm_arch / 'bin'
        asan_test_path.mkdir(parents=True, exist_ok=True)
        asan_test_bin_path = asan_test_path / 'asan_test'
        asan_test_bin_path.touch(exist_ok=True)

        lib_dir = self.output_toolchain.resource_dir
        self._build_sanitizer_map_file('asan', arch, lib_dir)
        self._build_sanitizer_map_file('ubsan_standalone', arch, lib_dir)

        if arch == hosts.Arch.AARCH64:
            self._build_sanitizer_map_file('hwasan', arch, lib_dir)

    @staticmethod
    def _build_sanitizer_map_file(san: str, arch: hosts.Arch, lib_dir: Path) -> None:
        lib_file = lib_dir / f'libclang_rt.{san}-{arch.llvm_arch}-android.so'
        map_file = lib_dir / f'libclang_rt.{san}-{arch.llvm_arch}-android.map.txt'
        mapfile.create_map_file(lib_file, map_file)


class Stage1Builder(base_builders.LLVMBuilder):
    name: str = 'stage1'
    toolchain_name: str = 'prebuilt'
    install_dir: Path = paths.OUT_DIR / 'stage1-install'
    build_android_targets: bool = False
    config_list: List[configs.Config] = [configs.host_config()]
    use_goma_for_stage1: bool = False

    @property
    def llvm_targets(self) -> Set[str]:
        if self.build_android_targets:
            return constants.HOST_TARGETS | constants.ANDROID_TARGETS
        else:
            return constants.HOST_TARGETS

    @property
    def llvm_projects(self) -> Set[str]:
        proj = {'clang', 'lld', 'libcxxabi', 'libcxx', 'compiler-rt'}
        if self.build_lldb:
            proj.add('lldb')
        return proj

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

        defines['LLVM_BUILD_TOOLS'] = 'ON'

        # Make libc++.so a symlink to libc++.so.x instead of a linker script that
        # also adds -lc++abi.  Statically link libc++abi to libc++ so it is not
        # necessary to pass -lc++abi explicitly.  This is needed only for Linux.
        if self._config.target_os.is_linux:
            defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'
            defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'

        # Do not build compiler-rt for Darwin.  We don't ship host (or any
        # prebuilt) runtimes for Darwin anyway.  Attempting to build these will
        # fail compilation of lib/builtins/atomic_*.c that only get built for
        # Darwin and fail compilation due to us using the bionic version of
        # stdatomic.h.
        if self._config.target_os.is_darwin:
            defines['LLVM_BUILD_EXTERNAL_COMPILER_RT'] = 'ON'

        # Don't build libfuzzer as part of the first stage build.
        defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'

        return defines

    @property
    def env(self) -> Dict[str, str]:
        env = super().env
        if self.use_goma_for_stage1:
            env['USE_GOMA'] = 'true'
        return env


class Stage2Builder(base_builders.LLVMBuilder):
    name: str = 'stage2'
    toolchain_name: str = 'stage1'
    install_dir: Path = paths.OUT_DIR / 'stage2-install'
    config_list: List[configs.Config] = [configs.host_config()]
    remove_install_dir: bool = True
    debug_build: bool = False
    build_instrumented: bool = False
    profdata_file: Optional[Path] = None
    lto: bool = True

    @property
    def llvm_targets(self) -> Set[str]:
        return constants.ANDROID_TARGETS

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
            resource_dir = self.toolchain.resource_dir
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
        defines['SANITIZER_ALLOW_CXXABI'] = 'OFF'
        defines['OPENMP_ENABLE_OMPT_TOOLS'] = 'FALSE'
        defines['LIBOMP_ENABLE_SHARED'] = 'FALSE'
        defines['CLANG_PYTHON_BINDINGS_VERSIONS'] = '3'

        if (self.lto and
                not self._config.target_os.is_darwin and
                not self.build_instrumented and
                not self.debug_build):
            defines['LLVM_ENABLE_LTO'] = 'Thin'

        # Build libFuzzer here to be exported for the host fuzzer builds. libFuzzer
        # is not currently supported on Darwin.
        if self._config.target_os.is_darwin:
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
        if self._config.target_os.is_linux:
            defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'
            defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'

        # Do not build compiler-rt for Darwin.  We don't ship host (or any
        # prebuilt) runtimes for Darwin anyway.  Attempting to build these will
        # fail compilation of lib/builtins/atomic_*.c that only get built for
        # Darwin and fail compilation due to us using the bionic version of
        # stdatomic.h.
        if self._config.target_os.is_darwin:
            defines['LLVM_BUILD_EXTERNAL_COMPILER_RT'] = 'ON'

        return defines

    def install_config(self) -> None:
        super().install_config()
        lldb_wrapper_path = self.install_dir / 'bin' / 'lldb.sh'
        lldb_wrapper_path.write_text(textwrap.dedent("""\
            #!/bin/bash
            CURDIR=$(cd $(dirname $0) && pwd)
            PYTHONHOME="$CURDIR/../python3" "$CURDIR/lldb" "$@"
        """))
        lldb_wrapper_path.chmod(0o755)


class CompilerRTBuilder(base_builders.LLVMRuntimeBuilder):
    name: str = 'compiler-rt'
    src_dir: Path = paths.LLVM_PATH / 'compiler-rt'
    config_list: List[configs.Config] = (
        configs.android_configs(platform=True) +
        configs.android_configs(platform=False)
    )

    @property
    def install_dir(self) -> Path:
        if self._config.platform:
            return self.output_toolchain.clang_lib_dir
        # Installs to a temporary dir and copies to runtimes_ndk_cxx manually.
        output_dir = self.output_dir
        return output_dir.parent / (output_dir.name + '-install')

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        arch = self._config.target_arch
        # FIXME: Disable WError build until upstream fixed the compiler-rt
        # personality routine warnings caused by r309226.
        # defines['COMPILER_RT_ENABLE_WERROR'] = 'ON'
        defines['COMPILER_RT_TEST_COMPILER_CFLAGS'] = defines['CMAKE_C_FLAGS']
        defines['COMPILER_RT_TEST_TARGET_TRIPLE'] = arch.llvm_triple
        defines['COMPILER_RT_INCLUDE_TESTS'] = 'OFF'
        defines['SANITIZER_CXX_ABI'] = 'libcxxabi'
        # With CMAKE_SYSTEM_NAME='Android', compiler-rt will be installed to
        # lib/android instead of lib/linux.
        del defines['CMAKE_SYSTEM_NAME']
        libs: List[str] = []
        if arch == 'arm':
            libs += ['-latomic']
        if self._config.api_level < 21:
            libs += ['-landroid_support']
        defines['SANITIZER_COMMON_LINK_LIBS'] = ' '.join(libs)
        if self._config.platform:
            defines['COMPILER_RT_HWASAN_WITH_INTERCEPTORS'] = 'OFF'
        return defines

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-funwind-tables')
        return cflags

    def install_config(self) -> None:
        # Still run `ninja install`.
        super().install_config()

        # Install the fuzzer library to the old {arch}/libFuzzer.a path for
        # backwards compatibility.
        arch = self._config.target_arch
        sarch = 'i686' if arch == hosts.Arch.I386 else arch.value
        static_lib_filename = 'libclang_rt.fuzzer-' + sarch + '-android.a'

        lib_dir = self.install_dir / 'lib' / 'linux'
        arch_dir = lib_dir / arch.value
        arch_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lib_dir / static_lib_filename, arch_dir / 'libFuzzer.a')

        if not self._config.platform:
            dst_dir = self.output_toolchain.path / 'runtimes_ndk_cxx'
            shutil.copytree(lib_dir, dst_dir, dirs_exist_ok=True)

    def install(self) -> None:
        # Install libfuzzer headers once for all configs.
        header_src = self.src_dir / 'lib' / 'fuzzer'
        header_dst = self.output_toolchain.path / 'prebuilt_include' / 'llvm' / 'lib' / 'Fuzzer'
        header_dst.mkdir(parents=True, exist_ok=True)
        for f in header_src.iterdir():
            if f.suffix in ('.h', '.def'):
                shutil.copy2(f, header_dst)

        symlink_path = self.output_toolchain.resource_dir / 'libclang_rt.hwasan_static-aarch64-android.a'
        symlink_path.unlink(missing_ok=True)
        os.symlink('libclang_rt.hwasan-aarch64-android.a', symlink_path)


class CompilerRTHostI386Builder(base_builders.LLVMRuntimeBuilder):
    name: str = 'compiler-rt-i386-host'
    src_dir: Path = paths.LLVM_PATH / 'compiler-rt'
    config_list: List[configs.Config] = [configs.LinuxConfig(is_32_bit=True)]

    @property
    def install_dir(self) -> Path:
        return self.output_toolchain.clang_lib_dir

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        # Due to CMake and Clang oddities, we need to explicitly set
        # CMAKE_C_COMPILER_TARGET and use march=i686 in cflags below instead of
        # relying on auto-detection from the Compiler-rt CMake files.
        defines['CMAKE_C_COMPILER_TARGET'] = 'i386-linux-gnu'
        defines['COMPILER_RT_INCLUDE_TESTS'] = 'ON'
        defines['COMPILER_RT_ENABLE_WERROR'] = 'ON'
        defines['SANITIZER_CXX_ABI'] = 'libstdc++'
        return defines

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        # compiler-rt/lib/gwp_asan uses PRIu64 and similar format-specifier macros.
        # Add __STDC_FORMAT_MACROS so their definition gets included from
        # inttypes.h.  This explicit flag is only needed here.  64-bit host runtimes
        # are built in stage1/stage2 and get it from the LLVM CMake configuration.
        # These are defined unconditionaly in bionic and newer glibc
        # (https://sourceware.org/git/gitweb.cgi?p=glibc.git;h=1ef74943ce2f114c78b215af57c2ccc72ccdb0b7)
        cflags.append('-D__STDC_FORMAT_MACROS')
        cflags.append('--target=i386-linux-gnu')
        cflags.append('-march=i686')
        return cflags

    def _build_config(self) -> None:
        # Also remove the "stamps" created for the libcxx included in libfuzzer so
        # CMake runs the configure again (after the cmake caches are deleted).
        stamp_path = self.output_dir / 'lib' / 'fuzzer' / 'libcxx_fuzzer_i386-stamps'
        if stamp_path.exists():
            shutil.rmtree(stamp_path)
        super()._build_config()


class LibOMPBuilder(base_builders.LLVMRuntimeBuilder):
    name: str = 'libomp'
    src_dir: Path = paths.LLVM_PATH / 'openmp'

    config_list: List[configs.Config] = (
        configs.android_configs(platform=True, extra_config={'is_shared': False}) +
        configs.android_configs(platform=False, extra_config={'is_shared': False}) +
        configs.android_configs(platform=False, extra_config={'is_shared': True})
    )

    @property
    def is_shared(self) -> bool:
        return cast(Dict[str, bool], self._config.extra_config)['is_shared']

    @property
    def output_dir(self) -> Path:
        old_path = super().output_dir
        suffix = '-shared' if self.is_shared else '-static'
        return old_path.parent / (old_path.name + suffix)

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['CMAKE_POSITION_INDEPENDENT_CODE'] = 'ON'
        defines['OPENMP_ENABLE_LIBOMPTARGET'] = 'FALSE'
        defines['OPENMP_ENABLE_OMPT_TOOLS'] = 'FALSE'
        defines['LIBOMP_ENABLE_SHARED'] = 'TRUE' if self.is_shared else 'FALSE'
        # Minimum version for OpenMP's CMake is too low for the CMP0056 policy
        # to be ON by default.
        defines['CMAKE_POLICY_DEFAULT_CMP0056'] = 'NEW'
        return defines

    def install_config(self) -> None:
        # We need to install libomp manually.
        libname = 'libomp.' + ('so' if self.is_shared else 'a')
        src_lib = self.output_dir / 'runtime' / 'src' / libname
        dst_dir = self.install_dir
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_lib, dst_dir / libname)


class LibEditBuilder(base_builders.AutoconfBuilder):
    name: str = 'libedit'
    src_dir: Path = paths.LIBEDIT_SRC_DIR
    config_list: List[configs.Config] = [configs.host_config()]

    def install(self) -> None:
        super().install()
        if self._config.target_os.is_darwin:
            # Updates LC_ID_DYLIB so that users of libedit won't link with absolute path.
            libedit_path = self.link_library
            cmd = ['install_name_tool',
                   '-id', f'@rpath/{libedit_path.name}',
                   str(libedit_path)]
            utils.check_call(cmd)

    @property
    def include_dir(self) -> Path:
        return self.install_dir / 'include'

    @property
    def link_library(self) -> Path:
        return {
            hosts.Host.Linux: self.install_dir / 'lib' / 'libedit.so.0',
            hosts.Host.Darwin: self.install_dir / 'lib' / 'libedit.0.dylib',
        }[self._config.target_os]

    @property
    def install_library(self) -> Path:
        return self.link_library


class SwigBuilder(base_builders.AutoconfBuilder):
    name: str = 'swig'
    src_dir: Path = paths.SWIG_SRC_DIR
    config_list: List[configs.Config] = [configs.host_config()]

    @property
    def config_flags(self) -> List[str]:
        flags = super().config_flags
        flags.append('--without-pcre')
        return flags

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        # Point to the libc++.so from the toolchain.
        ldflags.append(f'-Wl,-rpath,{self.toolchain.lib_dir}')
        return ldflags


class XzBuilder(base_builders.CMakeBuilder, base_builders.LibInfo):
    name: str = 'xz'
    src_dir: Path = paths.XZ_SRC_DIR
    config_list: List[configs.Config] = [configs.host_config()]

    @property
    def include_dir(self) -> Path:
        return self.install_dir / 'include'

    @property
    def link_library(self) -> Path:
        if self._config.target_os.is_windows and win_sdk.is_enabled():
            return self.install_dir / 'lib' / 'liblzma.lib'
        return self.install_dir / 'lib' / 'liblzma.a'

    @property
    def install_library(self) -> Optional[Path]:
        return None


class LibXml2Builder(base_builders.CMakeBuilder, base_builders.LibInfo):
    name: str = 'libxml2'
    src_dir: Path = paths.LIBXML2_SRC_DIR
    config_list: List[configs.Config] = [configs.host_config()]

    def build(self) -> None:
        # The src dir contains configure files for Android platform. Remove them.
        (self.src_dir / 'config.h').unlink(missing_ok=True)
        (self.src_dir / 'include' / 'libxml' / 'xmlversion.h').unlink(missing_ok=True)
        super().build()

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['LIBXML2_WITH_PYTHON'] = 'OFF'
        defines['LIBXML2_WITH_PROGRAMS'] = 'OFF'
        return defines

    @property
    def include_dir(self) -> Path:
        return self.install_dir / 'include' / 'libxml2'

    @property
    def link_library(self) -> Path:
        if self._config.target_os.is_windows and win_sdk.is_enabled():
            return self.install_dir / 'lib' / 'libxml2.lib'
        return {
            hosts.Host.Linux: self.install_dir / 'lib' / 'libxml2.so.2.9.10',
            hosts.Host.Darwin: self.install_dir / 'lib' / 'libxml2.2.9.10.dylib',
            hosts.Host.Windows: self.install_dir / 'lib' / 'libxml2.dll.a',
        }[self._config.target_os]

    @property
    def install_library(self) -> Path:
        if self._config.target_os.is_windows:
            return self.install_dir / 'bin' / 'libxml2.dll'
        return self.link_library


class LldbServerBuilder(base_builders.LLVMRuntimeBuilder):
    name: str = 'lldb-server'
    src_dir: Path = paths.LLVM_PATH / 'llvm'
    config_list: List[configs.Config] = configs.android_configs(platform=False, static=True)
    ninja_targets: List[str] = ['lldb-server']

    @property
    def cflags(self) -> List[str]:
        cflags: List[str] = super().cflags
        # The build system will add '-stdlib=libc++' automatically. Since we
        # have -nostdinc++ here, -stdlib is useless. Adds a flag to avoid the
        # warnings.
        cflags.append('-Wno-unused-command-line-argument')
        return cflags

    @property
    def _llvm_target(self) -> str:
        return {
            hosts.Arch.ARM: 'ARM',
            hosts.Arch.AARCH64: 'AArch64',
            hosts.Arch.I386: 'X86',
            hosts.Arch.X86_64: 'X86',
        }[self._config.target_arch]

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        # lldb depends on support libraries.
        defines['LLVM_ENABLE_PROJECTS'] = 'clang;lldb'
        defines['LLVM_TARGETS_TO_BUILD'] = self._llvm_target
        defines['LLVM_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'llvm-tblgen')
        defines['CLANG_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'clang-tblgen')
        defines['LLDB_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'lldb-tblgen')
        defines['LLVM_HOST_TRIPLE'] = self._config.target_arch.llvm_triple
        return defines

    def install_config(self) -> None:
        src_path = self.output_dir / 'bin' / 'lldb-server'
        install_dir = self.install_dir
        install_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, install_dir)


class LibCxxAbiBuilder(base_builders.LLVMRuntimeBuilder):
    name = 'libcxxabi'
    src_dir: Path = paths.LLVM_PATH / 'libcxxabi'

    @property
    def install_dir(self):
        return paths.OUT_DIR / 'windows-x86-64-install'

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines: Dict[str, str] = super().cmake_defines
        defines['LIBCXXABI_ENABLE_NEW_DELETE_DEFINITIONS'] = 'OFF'
        defines['LIBCXXABI_LIBCXX_INCLUDES'] = str(paths.LLVM_PATH /'libcxx' / 'include')

        # Build only the static library.
        defines['LIBCXXABI_ENABLE_SHARED'] = 'OFF'

        if self.enable_assertions:
            defines['LIBCXXABI_ENABLE_ASSERTIONS'] = 'ON'

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


class PlatformLibcxxAbiBuilder(base_builders.LLVMRuntimeBuilder):
    name = 'platform-libcxxabi'
    src_dir: Path = paths.LLVM_PATH / 'libcxxabi'
    config_list: List[configs.Config] = configs.android_configs(
        platform=True, suppress_libcxx_headers=True)

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines: Dict[str, str] = super().cmake_defines
        defines['LIBCXXABI_LIBCXX_INCLUDES'] = str(paths.LLVM_PATH / 'libcxx' / 'include')
        defines['LIBCXXABI_ENABLE_SHARED'] = 'OFF'
        return defines

    def _is_64bit(self) -> bool:
        return self._config.target_arch in (hosts.Arch.AARCH64, hosts.Arch.X86_64)

    def _build_config(self) -> None:
        if self._is_64bit():
            # For arm64 and x86_64, build static cxxabi library from
            # toolchain/libcxxabi and use it when building runtimes.  This
            # should affect all compiler-rt runtimes that use libcxxabi
            # (e.g. asan, hwasan, scudo, tsan, ubsan, xray).
            super()._build_config()
        else:
            self.install_config()

    def install_config(self) -> None:
        arch = self._config.target_arch
        lib_name = 'lib64' if arch == hosts.Arch.X86_64 else 'lib'
        install_dir = self._config.sysroot / 'usr' / lib_name

        if self._is_64bit():
            src_path = self.output_dir / 'lib64' / 'libc++abi.a'
            shutil.copy2(src_path, install_dir / 'libc++abi.a')
        else:
            with (install_dir / 'libc++abi.so').open('w') as f:
                f.write('INPUT(-lc++)')


class LibCxxBuilder(base_builders.LLVMRuntimeBuilder):
    name = 'libcxx'
    src_dir: Path = paths.LLVM_PATH / 'libcxx'
    libcxx_abi_path: Path

    @property
    def install_dir(self):
        return paths.OUT_DIR / 'windows-x86-64-install'

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
        defines['LIBCXX_CXX_ABI_LIBRARY_PATH'] = str(self.libcxx_abi_path / 'lib64')

        # Build only the static library.
        defines['LIBCXX_ENABLE_SHARED'] = 'OFF'
        defines['LIBCXX_ENABLE_EXPERIMENTAL_LIBRARY'] = 'OFF'

        if self.enable_assertions:
            defines['LIBCXX_ENABLE_ASSERTIONS'] = 'ON'

        return defines

    @property
    def cflags(self) -> List[str]:
        cflags: List[str] = super().cflags
        # Disable libcxxabi visibility annotations since we're only building it
        # statically.
        cflags.append('-D_LIBCXXABI_DISABLE_VISIBILITY_ANNOTATIONS')
        return cflags


class WindowsToolchainBuilder(base_builders.LLVMBuilder):
    name: str = 'windows-x86-64'
    toolchain_name: str = 'stage1'
    build_lldb: bool = True
    libcxx_path: Optional[Path] = None

    @property
    def _is_msvc(self) -> bool:
        return isinstance(self._config, configs.MSVCConfig)

    @property
    def install_dir(self) -> Path:
        return paths.OUT_DIR / 'windows-x86-64-install'

    @property
    def llvm_targets(self) -> Set[str]:
        return constants.ANDROID_TARGETS

    @property
    def llvm_projects(self) -> Set[str]:
        proj = {'clang', 'clang-tools-extra', 'lld'}
        if self.build_lldb:
            proj.add('lldb')
        return proj

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

        defines['LLVM_CONFIG_PATH'] = str(self.toolchain.build_path / 'bin' / 'llvm-config')
        defines['LLVM_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'llvm-tblgen')
        defines['CLANG_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'clang-tblgen')
        if self.build_lldb:
            defines['LLDB_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'lldb-tblgen')
        if self._is_msvc:
            # Generating libLLVM is not supported on MSVC.
            defines['LLVM_BUILD_LLVM_DYLIB'] = 'OFF'
            # But we still want LLVMgold.dll.
            defines['LLVM_ENABLE_PLUGINS'] = 'ON'

        defines['CMAKE_CXX_STANDARD'] = '17'
        defines['LLVM_BUILD_LLVM_C_DYLIB'] = 'OFF'

        return defines

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        if not self._is_msvc:
            # Use static-libgcc to avoid runtime dependence on libgcc_eh.
            ldflags.append('-static-libgcc')
            # pthread is needed by libgcc_eh.
            ldflags.append('-pthread')

            ldflags.append('-Wl,--dynamicbase')
            ldflags.append('-Wl,--nxcompat')
            ldflags.append('-Wl,--high-entropy-va')
            ldflags.append('-Wl,--Xlink=-Brepro')
            libpath_prefix = '-L'
        else:
            ldflags.append('/dynamicbase')
            ldflags.append('/nxcompat')
            ldflags.append('/highentropyva')
            ldflags.append('/Brepro')
            libpath_prefix = '/LIBPATH:'

        ldflags.append(libpath_prefix + str(paths.WIN_ZLIB_LIB_PATH))
        if self.libcxx_path:
            # Add path to libc++, libc++abi.
            libcxx_lib = self.libcxx_path / 'lib64'
            ldflags.append(libpath_prefix + str(libcxx_lib))
        return ldflags

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-DLZMA_API_STATIC')
        cflags.append('-DMS_WIN64')
        cflags.append(f'-I{paths.WIN_ZLIB_INCLUDE_PATH}')
        return cflags

    @property
    def cxxflags(self) -> List[str]:
        cxxflags = super().cxxflags

        # Use -fuse-cxa-atexit to allow static TLS destructors.  This is needed for
        # clang-tools-extra/clangd/Context.cpp
        cxxflags.append('-fuse-cxa-atexit')

        if self.libcxx_path:
            # Explicitly add the path to libc++ headers.  We don't need to configure
            # options like visibility annotations, win32 threads etc. because the
            # __generated_config header in the patch captures all the options used when
            # building libc++.
            cxx_headers = self.libcxx_path / 'include' / 'c++' / 'v1'
            cxxflags.append(f'-I{cxx_headers}')

        return cxxflags

    def install_config(self) -> None:
        super().install_config()
        lldb_wrapper_path = self.install_dir / 'bin' / 'lldb.cmd'
        lldb_wrapper_path.write_text(textwrap.dedent("""\
            @ECHO OFF
            SET PYTHONHOME=%~dp0..\python3
            %~dp0lldb.exe %*
            IF NOT [%ERRORLEVEL%] == [0] EXIT /B 1
        """))
