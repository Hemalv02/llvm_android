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
from typing import cast, Dict, Iterator, List, Optional, Set
import contextlib
import os
import re
import shutil
import textwrap
import timer

import base_builders
import configs
import constants
import hosts
import mapfile
import multiprocessing
import paths
import tempfile
import utils

class SanitizerMapFileBuilder(base_builders.Builder):
    name: str = 'sanitizer-mapfile'
    config_list: List[configs.Config] = configs.android_configs()

    def _build_config(self) -> None:
        arch = self._config.target_arch

        lib_dir = self.output_toolchain.clang_lib_dir / 'lib' / 'linux'
        self._build_sanitizer_map_file('asan', arch, lib_dir, 'ASAN')
        self._build_sanitizer_map_file('ubsan_standalone', arch, lib_dir, 'ASAN')
        if super()._is_64bit():
           self._build_sanitizer_map_file('tsan', arch, lib_dir, 'TSAN')

        if arch == hosts.Arch.AARCH64:
            self._build_sanitizer_map_file('hwasan', arch, lib_dir, 'ASAN')

    @staticmethod
    def _build_sanitizer_map_file(san: str, arch: hosts.Arch, lib_dir: Path, section_name: str) -> None:
        lib_file = lib_dir / f'libclang_rt.{san}-{arch.llvm_arch}-android.so'
        map_file = lib_dir / f'libclang_rt.{san}-{arch.llvm_arch}-android.map.txt'
        mapfile.create_map_file(lib_file, map_file, section_name)


class Stage1Builder(base_builders.LLVMBuilder):
    name: str = 'stage1'
    install_dir: Path = paths.OUT_DIR / 'stage1-install'
    build_extra_tools: bool = False

    @property
    def llvm_targets(self) -> Set[str]:
        if self._config.target_os.is_darwin:
            return constants.DARWIN_HOST_TARGETS
        else:
            return constants.HOST_TARGETS | constants.ANDROID_TARGETS

    @property
    def llvm_projects(self) -> Set[str]:
        proj = {'clang', 'lld'}
        # Need tools like clang-pseudo-gen and clang-tidy-confusable-chars-gen
        # for Linux when cross-compiling for Windows.
        proj.add('clang-tools-extra')
        if self.build_lldb:
            proj.add('lldb')
        return proj

    @property
    def llvm_runtime_projects(self) -> Set[str]:
        proj = {'compiler-rt', 'libcxx', 'libcxxabi'}
        if isinstance(self._config, configs.LinuxMuslConfig):
            # libcxx builds against libunwind when building for musl
            proj.add('libunwind')
        return proj

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        if self._config.target_os.is_darwin:
            # On Darwin, -static-libstdc++ isn't supported. So use rpath to find c++ runtime.
            ldflags.append(f'-Wl,-rpath,{self.toolchain.path / "lib"}')
        else:
            # Use -static-libstdc++ to statically link the c++ runtime [1].  This
            # avoids specifying self.toolchain.lib_dirs in rpath to find libc++ at
            # runtime.
            # [1] libc++ in our case, despite the flag saying -static-libstdc++.
            ldflags.append('-static-libstdc++')

        return ldflags

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['CLANG_ENABLE_ARCMT'] = 'OFF'
        if not self.build_extra_tools:
            defines['CLANG_ENABLE_STATIC_ANALYZER'] = 'OFF'

        defines['LLVM_BUILD_TOOLS'] = 'ON'

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

    def test(self) -> None:
        with timer.Timer(f'stage1_test'):
            self._ninja(['check-clang', 'check-llvm', 'check-clang-tools'])
        # stage1 cannot run check-cxx yet


class Stage2Builder(base_builders.LLVMBuilder):
    name: str = 'stage2'
    install_dir: Path = paths.OUT_DIR / 'stage2-install'
    remove_install_dir: bool = True
    debug_build: bool = False
    build_instrumented: bool = False
    bolt_optimize: bool = False
    bolt_instrument: bool = False
    profdata_file: Optional[Path] = None
    lto: bool = False

    @property
    def llvm_targets(self) -> Set[str]:
        return constants.ANDROID_TARGETS

    @property
    def llvm_projects(self) -> Set[str]:
        proj = {'clang', 'lld', 'clang-tools-extra', 'polly', 'bolt'}
        if self.build_lldb:
            proj.add('lldb')
        return proj

    @property
    def llvm_runtime_projects(self) -> Set[str]:
        proj = {'compiler-rt', 'libcxx', 'libcxxabi'}
        if isinstance(self._config, configs.LinuxMuslConfig):
            # libcxx builds against libunwind when building for musl
            proj.add('libunwind')
        return proj

    @property
    def ld_library_path_env_name(self) -> str:
        return 'LD_LIBRARY_PATH' if self._config.target_os.is_linux else 'DYLD_LIBRARY_PATH'

    @property
    def env(self) -> Dict[str, str]:
        env = super().env
        if self._config.target_os.is_linux:
            # Point CMake to the libc++ from stage1.  It is possible that once built,
            # the newly-built libc++ may override this because of the rpath pointing to
            # $ORIGIN/../lib.  That'd be fine because both libraries are built from
            # the same sources.
            # Newer compilers put lib files in lib/x86_64-unknown-linux-gnu.
            # Include the path to the libc++.so.1 in stage2-install,
            # to run unittests/.../*Tests programs.
            env['LD_LIBRARY_PATH'] = (
                    ':'.join([str(item) for item in self.toolchain.lib_dirs])
                    + f':{self.install_dir}/lib')
        return env

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        if self._config.target_os.is_linux:
            if isinstance(self._config, configs.LinuxMuslConfig):
                ldflags.append('-Wl,-rpath,\$ORIGIN/../lib/x86_64-unknown-linux-musl')
            else:
                ldflags.append('-Wl,-rpath,\$ORIGIN/../lib/x86_64-unknown-linux-gnu')
        # '$ORIGIN/../lib' is added by llvm's CMake rules.
        if self.bolt_optimize or self.bolt_instrument:
            ldflags.append('-Wl,-q')
        # TODO: Turn on ICF for Darwin once it can be built with LLD.
        if not self._config.target_os.is_darwin:
            ldflags.append('-Wl,--icf=safe')
        if self.lto and self.enable_mlgo:
            ldflags.append('-Wl,-mllvm,-regalloc-enable-advisor=release')
        return ldflags

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        if self.profdata_file:
            cflags.append('-Wno-profile-instr-out-of-date')
            cflags.append('-Wno-profile-instr-unprofiled')
        if not self.lto and self.enable_mlgo:
            cflags.append('-mllvm -regalloc-enable-advisor=release')
        return cflags

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['CLANG_PYTHON_BINDINGS_VERSIONS'] = '3'

        if (self.lto and
                not self._config.target_os.is_darwin and
                not self.build_instrumented and
                not self.debug_build):
            defines['LLVM_ENABLE_LTO'] = 'Thin'

            # Increase the ThinLTO link jobs limit to improve build speed.
            defines['LLVM_PARALLEL_LINK_JOBS'] = min(int(multiprocessing.cpu_count() / 2), 16)

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

        # Do not build compiler-rt for Darwin.  We don't ship host (or any
        # prebuilt) runtimes for Darwin anyway.  Attempting to build these will
        # fail compilation of lib/builtins/atomic_*.c that only get built for
        # Darwin and fail compilation due to us using the bionic version of
        # stdatomic.h.
        if self._config.target_os.is_darwin:
            defines['LLVM_BUILD_EXTERNAL_COMPILER_RT'] = 'ON'

        return defines

    def _build_config(self) -> None:
        if self._config.target_os.is_darwin:
            # Tablegen binaries (like llvm-min-tblgen, llvm-tblgen) are built and ran before
            # building libc++.dylib. We need someway to help them find libc++.dylib in
            # stage1-install. Because /usr/lib/libc++.1.dylib may be too old to support them.
            # On darwin, System Integrity Protection blocks DYLD_LIBRARY_PATH from taking effect
            # through /bin/bash. And we don't want to change rpath for all binaries built in
            # stage2. So we copy libc++.dylib from stage1-install to stage2 before building stage2.
            # This will help us run tablegen binaries. And it will be overwritten by libc++.dylib
            # built in stage2.
            lib_dir = self.output_dir / 'lib'
            lib_dir.mkdir(parents=True, exist_ok=True)
            libcxx_path = lib_dir / 'libc++.dylib'
            if not libcxx_path.is_file():
                shutil.copy2(self.toolchain.path / 'lib' / 'libc++.dylib', libcxx_path)
        return super()._build_config()

    def install_config(self) -> None:
        super().install_config()
        lldb_wrapper_path = self.install_dir / 'bin' / 'lldb.sh'
        lldb_wrapper_path.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            CURDIR=$(cd $(dirname $0) && pwd)
            export PYTHONHOME="$CURDIR/../python3"
            export {self.ld_library_path_env_name}="$CURDIR/../python3/lib:${self.ld_library_path_env_name}"
            "$CURDIR/lldb" "$@"
        """))
        lldb_wrapper_path.chmod(0o755)

    def test(self) -> None:
        if isinstance(self._config, configs.LinuxMuslConfig):
            # musl cannot run check-cxx yet
            with timer.Timer('stage2_test'):
                self._ninja(['check-clang', 'check-llvm'])
                # TUSchedulerTests.PreambleThrottle is flaky on buildbots for musl build.
                # So disable it.
                self._ninja(['check-clang-tools'],
                            {'GTEST_FILTER': '-TUSchedulerTests.PreambleThrottle'})
        else:
            super().test()


class BuiltinsBuilder(base_builders.LLVMRuntimeBuilder):
    name: str = 'builtins'
    src_dir: Path = paths.LLVM_PATH / 'compiler-rt' / 'lib' / 'builtins'

    # Only target the NDK, not the platform. The NDK copy is sufficient for the
    # platform builders, and both NDK+platform builders use the same toolchain,
    # which can only have a single copy installed into its resource directory.
    @property
    def config_list(self) -> List[configs.Config]:
        result = configs.android_configs(platform=False)
        # There is no NDK for riscv64, use the platform config instead.
        riscv64 = configs.AndroidRiscv64Config()
        riscv64.platform = True
        result.append(riscv64)
        result.append(configs.BaremetalAArch64Config())
        result.append(configs.BaremetalArmv6MConfig())
        result.append(configs.BaremetalArmv8MBaseConfig())
        for fpu in hosts.Armv81MMainFpu:
            result.append(configs.BaremetalArmv81MMainConfig(fpu))
        # For arm32 and x86, build a special version of the builtins library
        # where the symbols are exported, not hidden. This version is needed
        # to continue exporting builtins from libc.so and libm.so.
        for arch in [configs.AndroidARMConfig(), configs.AndroidI386Config()]:
            arch.platform = False
            arch.extra_config = {'is_exported': True}
            result.append(arch)
        result.append(configs.LinuxMuslConfig(hosts.Arch.AARCH64))
        result.append(configs.LinuxMuslConfig(hosts.Arch.ARM))
        result.append(configs.LinuxMuslConfig(hosts.Arch.X86_64))
        return result

    @property
    def is_exported(self) -> bool:
        return self._config.extra_config and self._config.extra_config.get('is_exported', False)

    @property
    def output_dir(self) -> Path:
        old_path = super().output_dir
        suffix = '-exported' if self.is_exported else ''
        return old_path.parent / (old_path.name + suffix)

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        arch = self._config.target_arch
        defines['COMPILER_RT_BUILTINS_HIDE_SYMBOLS'] = \
            'TRUE' if not self.is_exported else 'FALSE'
        # Most builders use COMPILER_RT_DEFAULT_TARGET_TRIPLE, but that cause
        # a problem for non-Android arm.  compiler-rt autodetects arm, armhf
        # and armv6m in compiler-rt/cmake/base-config-ix.cmake, but
        # set_output_name in compiler-rt/cmake/Modules/AddCompilerRT.cmake
        # uses the name libclang_rt.builtins-arm for both arm and armv6m.
        # Use CMAKE_C_COMPILER_TARGET + COMPILER_RT_DEFAULT_TARGET_ONLY
        # instead to only build for armhf.
        defines['CMAKE_C_COMPILER_TARGET'] = self._config.llvm_triple
        defines['CMAKE_CXX_COMPILER_TARGET'] = self._config.llvm_triple
        defines['COMPILER_RT_DEFAULT_TARGET_ONLY'] = 'TRUE'
        # For CMake feature testing, create an archive instead of an executable,
        # because we can't link an executable until builtins have been built.
        defines['CMAKE_TRY_COMPILE_TARGET_TYPE'] = 'STATIC_LIBRARY'
        # Baremetal Armv6-M does not support atomics and the build
        # fails with a static assert if they are included.
        if not isinstance(self._config, configs.BaremetalArmv6MConfig):
            defines['COMPILER_RT_EXCLUDE_ATOMIC_BUILTIN'] = 'OFF'
        defines['COMPILER_RT_OS_DIR'] = self._config.target_os.crt_dir
        return defines

    def install_config(self) -> None:
        # Copy the library into the toolchain resource directory (lib/linux) and
        # runtimes_ndk_cxx.
        arch = self._config.target_arch
        sarch = 'i686' if arch == hosts.Arch.I386 else arch.value
        if isinstance(self._config, configs.LinuxMuslConfig) and arch == hosts.Arch.ARM:
            sarch = 'armhf'
        filename = 'libclang_rt.builtins-' + sarch
        filename += '-android.a' if self._config.target_os.is_android else '.a'
        filename_exported = 'libclang_rt.builtins-' + sarch + '-android-exported.a'
        if isinstance(self._config, configs.BaremetalArmMultilibConfig):
            # For ARM targets, compiler-rt uses the triple to decide which sources to include,
            # however the triple also affects the library suffix (e.g. -armv6m.a vs -arm.a).
            # In order to ensure the correct sources are used, we have to include the subarch in
            # the triple, but we keep the suffix as just 'arm' in the final output to support
            # the commonly used 'arm-none-eabi[hf]' triple.
            src_filename = 'libclang_rt.builtins-' + self._config.llvm_triple.split('-')[0] + '.a'
            src_path = self.output_dir / 'lib' / self._config.target_os.crt_dir / src_filename
            # Copy libs into separate multilib directories to prevent name conflicts.
            out_res_dir = self.output_resource_dir / self._config.multilib_name / 'lib'
            res_dir = self.resource_dir / self._config.multilib_name / 'lib'
        else:
            src_path = self.output_dir / 'lib' / self._config.target_os.crt_dir / filename
            out_res_dir = self.output_resource_dir
            res_dir = self.resource_dir

        out_res_dir.mkdir(parents=True, exist_ok=True)
        if self.is_exported:
            # This special copy exports its symbols and is only intended for use
            # in Bionic's libc.so.
            shutil.copy2(src_path, out_res_dir / filename_exported)
        else:
            shutil.copy2(src_path, out_res_dir / filename)

            # Also install to self.resource_dir, if it's different,
            # for use when building target libraries.
            if res_dir != out_res_dir:
                res_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, res_dir / filename)

            # Make a copy for the NDK.
            if self._config.target_os.is_android:
                dst_dir = self.output_toolchain.path / 'runtimes_ndk_cxx'
                dst_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dst_dir / filename)


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
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        if self._config.platform:
            ldflags.append('-Wl,-z,max-page-size=65536')
        return ldflags

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['COMPILER_RT_BUILD_BUILTINS'] = 'OFF'
        defines['COMPILER_RT_USE_BUILTINS_LIBRARY'] = 'ON'
        # FIXME: Disable WError build until upstream fixed the compiler-rt
        # personality routine warnings caused by r309226.
        # defines['COMPILER_RT_ENABLE_WERROR'] = 'ON'
        defines['COMPILER_RT_TEST_COMPILER_CFLAGS'] = defines['CMAKE_C_FLAGS']
        defines['COMPILER_RT_DEFAULT_TARGET_TRIPLE'] = self._config.llvm_triple
        defines['COMPILER_RT_INCLUDE_TESTS'] = 'OFF'
        defines['SANITIZER_CXX_ABI'] = 'libcxxabi'
        # With CMAKE_SYSTEM_NAME='Android', compiler-rt will be installed to
        # lib/android instead of lib/linux.
        del defines['CMAKE_SYSTEM_NAME']
        libs: List[str] = []
        if self._config.api_level < 21:
            libs += ['-landroid_support']
        # Currently, -rtlib=compiler-rt (even with -unwindlib=libunwind) does
        # not automatically link libunwind.a on Android.
        libs += ['-lunwind']
        defines['SANITIZER_COMMON_LINK_LIBS'] = ' '.join(libs)
        # compiler-rt's CMakeLists.txt file deletes -Wl,-z,defs from
        # CMAKE_SHARED_LINKER_FLAGS when COMPILER_RT_USE_BUILTINS_LIBRARY is
        # set. We want this flag on instead to catch unresolved references
        # early.
        defines['SANITIZER_COMMON_LINK_FLAGS'] = '-Wl,-z,defs'
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

        lib_dir = self.install_dir / 'lib' / 'linux'

        # Install the fuzzer library to the old {arch}/libFuzzer.a path for
        # backwards compatibility.
        arch = self._config.target_arch
        sarch = 'i686' if arch == hosts.Arch.I386 else arch.value
        static_lib_filename = 'libclang_rt.fuzzer-' + sarch + '-android.a'

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

        symlink_path = self.output_resource_dir / 'libclang_rt.hwasan_static-aarch64-android.a'
        symlink_path.unlink(missing_ok=True)
        os.symlink('libclang_rt.hwasan-aarch64-android.a', symlink_path)


class MuslHostRuntimeBuilder(base_builders.LLVMRuntimeBuilder):
    name: str = 'compiler-rt-linux-musl'
    src_dir: Path = paths.LLVM_PATH / 'runtimes'

    config_list: List[configs.Config] = [
            configs.LinuxMuslConfig(hosts.Arch.X86_64),
            configs.LinuxMuslConfig(hosts.Arch.I386),
            configs.LinuxMuslConfig(hosts.Arch.AARCH64),
            configs.LinuxMuslConfig(hosts.Arch.ARM),
    ]

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['LLVM_ENABLE_RUNTIMES'] = 'compiler-rt;libunwind'

        # compiler-rt CMake defines
        # ORC JIT fails to build with MUSL.
        defines['COMPILER_RT_BUILD_ORC'] = 'OFF'

        # libunwind CMake defines
        if self.enable_assertions:
            defines['LIBUNWIND_ENABLE_ASSERTIONS'] = 'TRUE'
        else:
            defines['LIBUNWIND_ENABLE_ASSERTIONS'] = 'FALSE'
        defines['LIBUNWIND_ENABLE_SHARED'] = 'FALSE'
        defines['LIBUNWIND_TARGET_TRIPLE'] = self._config.llvm_triple
        defines['COMPILER_RT_HAS_LIBSTDCXX'] = 'FALSE'
        defines['COMPILER_RT_HAS_LIBCXX'] = 'TRUE'
        defines['SANITIZER_CXX_ABI'] = 'libcxxabi'
        defines['COMPILER_RT_USE_BUILTINS_LIBRARY'] = 'TRUE'

        # Most builders use COMPILER_RT_DEFAULT_TARGET_TRIPLE, but that cause
        # a problem for non-Android arm.  compiler-rt autodetects arm, armhf
        # and armv6m in compiler-rt/cmake/base-config-ix.cmake, but
        # set_output_name in compiler-rt/cmake/Modules/AddCompilerRT.cmake
        # uses the name libclang_rt.builtins-arm for both arm and armv6m.
        # Use CMAKE_C_COMPILER_TARGET + COMPILER_RT_DEFAULT_TARGET_ONLY
        # instead to only build for armhf.
        # CMAKE_CXX_COMPILER_TARGET is also necessary for the libcxx embedded
        # in libclang_rt.fuzzer.
        defines['CMAKE_C_COMPILER_TARGET'] = self._config.llvm_triple
        defines['CMAKE_CXX_COMPILER_TARGET'] = self._config.llvm_triple
        defines['COMPILER_RT_DEFAULT_TARGET_ONLY'] = 'TRUE'
        return defines


    @property
    def cflags(self) -> List[str]:
        # Use the stage2 toolchain's resource-dir where libclang_rt.builtins
        # gets installed.  This is only needed in debug and instrumented builds
        # (where the stage1 toolchain is used to build runtimes) and a no-op
        # elsewhere.
        return super().cflags + ['-resource-dir', f'{self.output_toolchain.clang_lib_dir}']

    @property
    def install_dir(self) -> Path:
        return self.output_resource_dir / self._config.llvm_triple


class LibUnwindBuilder(base_builders.LLVMRuntimeBuilder):
    name: str = 'libunwind'
    src_dir: Path = paths.LLVM_PATH / 'runtimes'

    # Build two copies of the builtins library:
    #  - A copy targeting the NDK with hidden symbols.
    #  - A copy targeting the platform with exported symbols.
    # Bionic's libc.so exports the unwinder, so it needs a copy with exported
    # symbols. Everything else uses the NDK copy.
    @property
    def config_list(self) -> List[configs.Config]:
        result = configs.android_configs(platform=False)
        for arch in configs.android_configs(platform=True):
            arch.extra_config = {'is_exported': True}
            result.append(arch)

        # riscv64 needs a copy with hidden symbols for use while building
        # the runtimes, but doesn't have an NDK sysroot.  Make a copy
        # targeting the platform with hidden symbols.
        riscv64 = configs.AndroidRiscv64Config()
        riscv64.platform = True
        riscv64.extra_config = {'is_exported': False}

        result.append(riscv64)

        return result

    @property
    def is_exported(self) -> bool:
        return self._config.extra_config and self._config.extra_config.get('is_exported', False)

    @property
    def output_dir(self) -> Path:
        old_path = super().output_dir
        suffix = '-exported' if self.is_exported else '-hermetic'
        return old_path.parent / (old_path.name + suffix)

    @property
    def cflags(self) -> List[str]:
        return super().cflags + ['-D_LIBUNWIND_USE_DLADDR=0']

    @property
    def ldflags(self) -> List[str]:
        # Override the default -unwindlib=libunwind. libunwind.a doesn't exist
        # when libunwind is built, and libunwind can't use
        # CMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY because
        # LIBUNWIND_HAS_PTHREAD_LIB must be set to false. Also avoid linking the
        # STL because it too does not exist yet.
        return super().ldflags + ['-unwindlib=none', '-nostdlib++']

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['LLVM_ENABLE_RUNTIMES'] = 'libunwind'
        defines['LIBUNWIND_HIDE_SYMBOLS'] = 'TRUE' if not self.is_exported else 'FALSE'
        defines['LIBUNWIND_ENABLE_SHARED'] = 'FALSE'
        if self.enable_assertions:
            defines['LIBUNWIND_ENABLE_ASSERTIONS'] = 'TRUE'
        else:
            defines['LIBUNWIND_ENABLE_ASSERTIONS'] = 'FALSE'
        # Enable the FrameHeaderCache for the libc.so unwinder only. It can't be
        # enabled generally for Android because it needs the
        # dlpi_adds/dlpi_subs fields, which were only added to Bionic in
        # Android R. See llvm.org/pr46743.
        defines['LIBUNWIND_USE_FRAME_HEADER_CACHE'] = 'TRUE' if self.is_exported else 'FALSE'
        defines['LIBUNWIND_TARGET_TRIPLE'] = self._config.llvm_triple
        return defines

    def install_config(self) -> None:
        # We need to install libunwind manually.
        arch = self._config.target_arch
        src_path = self.output_dir / 'lib' / 'libunwind.a'
        out_res_dir = self.output_resource_dir / arch.value
        out_res_dir.mkdir(parents=True, exist_ok=True)

        if self.is_exported:
            # This special copy exports its symbols and is only intended for use
            # in Bionic's libc.so.
            shutil.copy2(src_path, out_res_dir / 'libunwind-exported.a')
        else:
            shutil.copy2(src_path, out_res_dir / 'libunwind.a')

            # Also install to self.resource_dir, if it's different, for
            # use when building runtimes.
            if self.resource_dir != self.output_resource_dir:
                res_dir = self.resource_dir / arch.value
                res_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, res_dir / 'libunwind.a')

            # Make a copy for the NDK.
            ndk_dir = self.output_toolchain.path / 'runtimes_ndk_cxx' / arch.value
            ndk_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, ndk_dir / 'libunwind.a')


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
        defines['OPENMP_ENABLE_LIBOMPTARGET'] = 'FALSE'
        defines['OPENMP_ENABLE_OMPT_TOOLS'] = 'FALSE'
        defines['LIBOMP_ENABLE_SHARED'] = 'TRUE' if self.is_shared else 'FALSE'
        return defines

    def install_config(self) -> None:
        # We need to install libomp manually.
        libname = 'libomp.' + ('so' if self.is_shared else 'a')
        src_lib = self.output_dir / 'runtime' / 'src' / libname
        dst_dir = self.install_dir
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_lib, dst_dir / libname)

        # install omp.h, omp-tools.h (it's enough to do for just one config).
        if self._config.target_arch == hosts.Arch.AARCH64:
            for header in ['omp.h', 'omp-tools.h']:
                shutil.copy2(self.output_dir / 'runtime' / 'src' / header,
                             self.output_toolchain.clang_builtin_header_dir)


class LibNcursesBuilder(base_builders.AutoconfBuilder, base_builders.LibInfo):
    name: str = 'libncurses'
    src_dir: Path = paths.LIBNCURSES_SRC_DIR

    @property
    def config_flags(self) -> List[str]:
        flags = super().config_flags + [
            '--with-shared',
            '--with-default-terminfo-dir=/usr/share/terminfo',
        ]
        if self._config.target_os.is_darwin:
            flags.append('--disable-mixed-case')
        return flags

    @property
    def _lib_names(self) -> List[str]:
        return ['libncurses', 'libform', 'libpanel']


class LibEditBuilder(base_builders.AutoconfBuilder, base_builders.LibInfo):
    name: str = 'libedit'
    src_dir: Path = paths.LIBEDIT_SRC_DIR
    libncurses: base_builders.LibInfo

    @property
    def ldflags(self) -> List[str]:
        return [
            f'-L{self.libncurses.link_libraries[0].parent}',
        ] + super().ldflags

    @property
    def cflags(self) -> List[str]:
        flags = []
        flags.append('-I' + str(self.libncurses.include_dir))
        flags.append('-I' + str(self.libncurses.include_dir / 'ncurses'))
        return flags + super().cflags


    def build(self) -> None:
        files: List[Path] = []
        super().build()


class SwigBuilder(base_builders.AutoconfBuilder):
    name: str = 'swig'
    src_dir: Path = paths.SWIG_SRC_DIR

    @property
    def config_flags(self) -> List[str]:
        flags = super().config_flags
        flags.append('--without-pcre')
        return flags

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        # Point to the libc++.so from the toolchain.
        for lib_dir in self.toolchain.lib_dirs:
            ldflags.append(f'-Wl,-rpath,{lib_dir}')
        return ldflags


class XzBuilder(base_builders.CMakeBuilder, base_builders.LibInfo):
    name: str = 'liblzma'
    src_dir: Path = paths.XZ_SRC_DIR
    static_lib: bool = True

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        # CMake actually generates a malformed archive command. llvm-ranlib does
        # not accept it, but the Apple ranlib accepts this. Workaround to use
        # the system ranlib until either CMake fixes this or llvm-ranlib also
        # supports this common malformed input.
        # See LIBTOOL(1).
        if self._config.target_os.is_darwin:
            defines['CMAKE_RANLIB'] = '/usr/bin/ranlib'
        return defines


class ZstdBuilder(base_builders.CMakeBuilder, base_builders.LibInfo):
    name: str = 'libzstd'
    src_dir: Path = paths.ZSTD_SRC_DIR / 'build' / 'cmake'
    with_lib_version: bool = False

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['ZSTD_BUILD_PROGRAMS'] = 'OFF'

        # See XzBuilder above for reasoning.
        if self._config.target_os.is_darwin:
            defines['CMAKE_RANLIB'] = '/usr/bin/ranlib'
        return defines

    @property
    def link_libraries(self) -> List[Path]:
        # LLVM requires both dynamic and static libzstd.
        if self._config.target_os.is_windows:
            libs = [self.install_dir / 'bin' / 'libzstd.dll']
        else:
            libs = super().link_libraries
        libs.append(self.install_dir / 'lib' / 'libzstd.a')
        return libs


class LibXml2Builder(base_builders.CMakeBuilder, base_builders.LibInfo):
    name: str = 'libxml2'
    src_dir: Path = paths.LIBXML2_SRC_DIR

    @contextlib.contextmanager
    def _backup_file(self, file_to_backup: Path) -> Iterator[None]:
        backup_file = file_to_backup.parent / (file_to_backup.name + '.bak')
        if file_to_backup.exists():
            file_to_backup.rename(backup_file)
        try:
            yield
        finally:
            if backup_file.exists():
                backup_file.rename(file_to_backup)

    def build(self) -> None:
        # The src dir contains configure files for Android platform. Rename them
        # so that they will not be used during our build.
        # We don't delete them here because the same libxml2 may be used to build
        # Android platform later.
        with self._backup_file(self.src_dir / 'include' / 'libxml' / 'xmlversion.h'):
            with self._backup_file(self.src_dir / 'config.h'):
                super().build()

    @property
    def ldflags(self) -> List[str]:
        if self._config.target_os.is_linux:
            # We do not enable all libxml2 features. Allow undefined symbols in the version script.
            return super().ldflags + ['-Wl,--undefined-version']
        return super().ldflags

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['LIBXML2_WITH_PYTHON'] = 'OFF'
        defines['LIBXML2_WITH_PROGRAMS'] = 'ON'
        defines['LIBXML2_WITH_LZMA'] = 'OFF'
        defines['LIBXML2_WITH_ICONV'] = 'OFF'
        defines['LIBXML2_WITH_ZLIB'] = 'OFF'
        return defines

    @property
    def include_dir(self) -> Path:
        return self.install_dir / 'include' / 'libxml2'

    @property
    def symlinks(self) -> List[Path]:
        if self._config.target_os.is_windows:
            return []
        ext = 'so' if self._config.target_os.is_linux else 'dylib'
        return [self.install_dir / 'lib' / f'libxml2.{ext}']

    @property
    def install_tools(self) -> List[Path]:
        return [self.install_dir / 'bin' / 'xmllint']


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
    def ldflags(self) -> List[str]:
        # Currently, -rtlib=compiler-rt (even with -unwindlib=libunwind) does
        # not automatically link libunwind.a on Android.
        return super().ldflags + ['-lunwind']

    @property
    def _llvm_target(self) -> str:
        return {
            hosts.Arch.ARM: 'ARM',
            hosts.Arch.AARCH64: 'AArch64',
            hosts.Arch.I386: 'X86',
            hosts.Arch.X86_64: 'X86',
            hosts.Arch.RISCV64: 'RISCV',
        }[self._config.target_arch]

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        # lldb depends on support libraries.
        defines['LLVM_ENABLE_PROJECTS'] = 'clang;lldb'
        defines['LLVM_TARGETS_TO_BUILD'] = self._llvm_target
        defines['LLVM_NATIVE_TOOL_DIR'] = str(self.toolchain.build_path / 'bin')
        triple = self._config.llvm_triple
        defines['LLVM_HOST_TRIPLE'] = triple.replace('i686', 'i386')
        defines['LLDB_ENABLE_LUA'] = 'OFF'
        return defines

    def install_config(self) -> None:
        src_path = self.output_dir / 'bin' / 'lldb-server'
        install_dir = self.install_dir
        install_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, install_dir)


class HostSysrootsBuilder(base_builders.Builder):
    name: str = 'host-sysroots'
    config_list: List[configs.Config] = (configs.MinGWConfig(), configs.MinGWConfig(is_32_bit=True))

    def _build_config(self) -> None:
        config = self._config
        sysroot = config.sysroot
        sysroot_lib = sysroot / 'lib'
        if sysroot.exists():
            shutil.rmtree(sysroot)
        sysroot.parent.mkdir(parents=True, exist_ok=True)

        # Copy the sysroot.
        shutil.copytree(config.gcc_root / config.gcc_triple,
                        sysroot, symlinks=True)

        if config.target_arch == hosts.Arch.I386:
            shutil.rmtree(sysroot / 'lib')
            (sysroot / 'lib64').unlink()
            (sysroot / 'lib32').rename(sysroot / 'lib')
        elif config.target_arch == hosts.Arch.X86_64:
            shutil.rmtree(sysroot / 'lib32')

        # Add libgcc* to the sysroot.
        shutil.copytree(config.gcc_lib_dir, sysroot_lib, dirs_exist_ok=True)

        # b/237425904 cleanup: uncomment to remove libstdc++ after toolchain defaults to
        # libc++
        # (sysroot_lib / 'libstdc++.a').unlink()
        # shutil.rmtree(sysroot / 'include' / 'c++' / '4.8.3')


class DeviceSysrootsBuilder(base_builders.Builder):
    name: str = 'device-sysroots'
    config_list: List[configs.Config] = (
        configs.android_configs(platform=True) +
        configs.android_configs(platform=False)
    )

    def _build_config(self) -> None:
        config: configs.AndroidConfig = cast(configs.AndroidConfig, self._config)
        arch = config.target_arch
        sysroot = config.sysroot
        if sysroot.exists():
            shutil.rmtree(sysroot)
        sysroot.mkdir(parents=True, exist_ok=True)

        # Copy the NDK prebuilt's sysroot, but for the platform variant, omit
        # the STL and android_support headers and libraries.
        if arch == hosts.Arch.RISCV64:
            src_sysroot = paths.RISCV64_ANDROID_SYSROOT
        else:
            src_sysroot = paths.NDK_BASE / 'toolchains' / 'llvm' / 'prebuilt' / 'linux-x86_64' / 'sysroot'

        # Copy over usr/include.
        shutil.copytree(src_sysroot / 'usr' / 'include',
                        sysroot / 'usr' / 'include', symlinks=True)

        if arch != hosts.Arch.RISCV64:
            # Remove the STL headers.
            shutil.rmtree(sysroot / 'usr' / 'include' / 'c++')

        # Copy over usr/lib/$TRIPLE.
        src_lib = src_sysroot / 'usr' / 'lib' / config.ndk_sysroot_triple
        dest_lib = sysroot / 'usr' / 'lib' / config.ndk_sysroot_triple
        shutil.copytree(src_lib, dest_lib, symlinks=True)

        # For RISCV64, symlink the 10000 api-dir to 35
        # TODO (http://b/287650094 Remove this hack when we have a risc-v
        # sysroot in the NDK.
        if arch == hosts.Arch.RISCV64:
            (dest_lib / '35').symlink_to('10000')

        # Remove the NDK's libcompiler_rt-extras.  Also remove the NDK libc++,
        # except for the riscv64 sysroot which doesn't have these files.
        (dest_lib / 'libcompiler_rt-extras.a').unlink()
        if arch != hosts.Arch.RISCV64:
            (dest_lib / 'libc++abi.a').unlink()
            (dest_lib / 'libc++_static.a').unlink()
            (dest_lib / 'libc++_shared.so').unlink()
        # Each per-API-level directory has libc++.so and libc++.a.
        for subdir in dest_lib.iterdir():
            if subdir.is_symlink() or not subdir.is_dir():
                continue
            if not re.match(r'\d+$', subdir.name):
                continue
            if arch != hosts.Arch.RISCV64:
                (subdir / 'libc++.a').unlink()
                (subdir / 'libc++.so').unlink()
        # Verify that there aren't any extra copies somewhere else in the
        # directory hierarchy.
        verify_gone = [
            'libc++abi.a',
            'libc++_static.a',
            'libc++_shared.so',
            'libc++.a',
            'libc++.so',
            'libcompiler_rt-extras.a',
            'libunwind.a',
        ]
        for (parent, _, files) in os.walk(sysroot):
            for f in files:
                if f in verify_gone:
                    raise RuntimeError('sysroot file should have been ' +
                                       f'removed: {os.path.join(parent, f)}')


class DeviceLibcxxBuilder(base_builders.LLVMRuntimeBuilder):
    name = 'device-libcxx'
    src_dir: Path = paths.LLVM_PATH / 'runtimes'

    def gen_configs(platform: bool, apex: bool):
        result = configs.android_configs(platform=platform,
            suppress_libcxx_headers=True, extra_config={'apex': apex})
        # The non-APEX system libc++.so needs to be built against a newer API so
        # it uses the unwinder from libc.so. RISC-V uses API 10000 instead
        # currently.
        if platform and not apex:
            for config in result:
                if config.target_arch != hosts.Arch.RISCV64:
                    config.override_api_level = 33
        return result

    config_list: List[configs.Config] = (
        gen_configs(platform=False, apex=False) +
        gen_configs(platform=True, apex=False) +
        gen_configs(platform=True, apex=True)
    )

    @property
    def _is_ndk(self) -> bool:
        return not self._config.platform

    @property
    def _is_apex(self) -> bool:
        return self._config.extra_config['apex']

    @property
    def output_dir(self) -> Path:
        old_path = super().output_dir
        suffix = '-apex' if self._config.extra_config['apex'] else ''
        return old_path.parent / (old_path.name + suffix)

    @property
    def cxxflags(self) -> list[str]:
        base = super().cxxflags
        # Required to prevent dlclose from causing crashes on thread exit.
        # https://github.com/android/ndk/issues/1200
        #
        # This doesn't actually control whether thread_local is used in most of the code
        # base, just whether it is used in the implementation of C++ exception storage
        # for libc++abi.
        if self._config.target_arch is hosts.Arch.RISCV64:
            # But rv64 doesn't support TLS yet (emulated or otherwise).
            # https://github.com/google/android-riscv64/issues/3
            return base
        return base + ['-DHAS_THREAD_LOCAL']

    @property
    def ldflags(self) -> List[str]:
        # Avoid linking the STL because it does not exist yet.
        result = super().ldflags + ['-nostdlib++']

        # For the platform libc++ build, use the unwinder API exported from
        # libc.so. Otherwise, link libunwind.a.
        if self._is_ndk or self._is_apex:
            result.append('-unwindlib=libunwind')
        else:
            result.append('-unwindlib=none')

        return result

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines: Dict[str, str] = super().cmake_defines
        defines['LLVM_ENABLE_RUNTIMES'] ='libcxx;libcxxabi'
        defines['LIBCXXABI_ENABLE_SHARED'] = 'OFF'
        defines['LIBCXXABI_TARGET_TRIPLE'] = self._config.llvm_triple
        if not self._is_ndk:
            defines['LIBCXXABI_NON_DEMANGLING_TERMINATE'] = 'ON'
            defines['LIBCXXABI_STATIC_DEMANGLE_LIBRARY'] = 'ON'

        defines['LIBCXX_ENABLE_SHARED'] = 'ON'
        defines['LIBCXX_TARGET_TRIPLE'] = self._config.llvm_triple
        defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'
        defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'
        defines['LIBCXX_STATICALLY_LINK_ABI_IN_SHARED_LIBRARY'] = 'ON'
        defines['LIBCXX_STATIC_OUTPUT_NAME'] = 'c++_static'
        if self._is_ndk:
            defines['LIBCXX_SHARED_OUTPUT_NAME'] = 'c++_shared'
            defines['LIBCXX_STATICALLY_LINK_ABI_IN_STATIC_LIBRARY'] = 'OFF'
            defines['LIBCXX_ABI_VERSION'] = '1'
            defines['LIBCXX_ABI_NAMESPACE'] = '__ndk1'
        else:
            defines['LIBCXX_STATICALLY_LINK_ABI_IN_STATIC_LIBRARY'] = 'ON'

        # There is a check for ANDROID_NATIVE_API_LEVEL in
        # HandleLLVMOptions.cmake that determines the value of
        # LLVM_FORCE_SMALLFILE_FOR_ANDROID and _FILE_OFFSET_BITS. Maybe it
        # should use a different name for the API level macro in CMake?
        defines['ANDROID_NATIVE_API_LEVEL'] = str(self._config.api_level)

        return defines

    def install_config(self) -> None:
        arch = self._config.target_arch
        sysroot_lib = self._config.sysroot / 'usr' / 'lib'

        # Copy libc++ headers into the NDK+platform sysroot.
        if self._is_ndk or self._is_apex:
            shutil.copytree(self.output_dir / 'include',
                            self._config.sysroot / 'usr' / 'include',
                            dirs_exist_ok=True, symlinks=True)

        # Copy libraries into the NDK sysroot, and generate libc++.{a,so} linker
        # scripts.
        if self._is_ndk:
            for name in ['libc++abi.a', 'libc++_shared.so', 'libc++_static.a']:
                shutil.copy2(self.output_dir / 'lib' / name, sysroot_lib / name)
            with open(sysroot_lib / 'libc++.a', 'w') as out:
                out.write('INPUT(-lc++_static -lc++abi)\n')
            with open(sysroot_lib / 'libc++.so', 'w') as out:
                out.write('INPUT(-lc++_shared)\n')

        # Copy libraries into the platform sysroot. Use the APEX build, which
        # targets a lower API level.
        if self._is_apex:
            for name in ['libc++abi.a', 'libc++.so']:
                shutil.copy2(self.output_dir / 'lib' / name, sysroot_lib / name)

        # Copy the output files to a directory structure for use with (a) Soong
        # and (b) the NDK's checkbuild.py. Offer the experimental library in the
        # NDK but omit it from the platform because we want to discourage
        # platform developers from using unstable APIs.
        if self._is_ndk:
            kind = 'ndk'
            libs = ['libc++abi.a', 'libc++_static.a', 'libc++_shared.so', 'libc++experimental.a']
        else:
            libs = ['libc++abi.a', 'libc++_static.a', 'libc++.so', 'libc++demangle.a']
            if self._is_apex:
                kind = 'apex'
            else:
                kind = 'platform'
        dst_dir = self.output_toolchain.path / 'android_libc++' / kind / arch.value
        dst_lib_dir = dst_dir / 'lib'
        dst_lib_dir.mkdir(parents=True, exist_ok=True)
        for name in libs:
            shutil.copy2(self.output_dir / 'lib' / name, dst_lib_dir)
        dst_inc_dir = dst_dir / 'include' / 'c++' / 'v1'
        dst_inc_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.output_dir / 'include' / 'c++' / 'v1' / '__config_site', dst_inc_dir)


class WinLibCxxBuilder(base_builders.LLVMRuntimeBuilder):
    name = 'win-libcxx'
    src_dir: Path = paths.LLVM_PATH / 'runtimes'

    @property
    def install_dir(self):
        if self._config.target_arch == hosts.Arch.I386:
            return paths.OUT_DIR / 'windows-libcxx-i686-install'
        elif self._config.target_arch == hosts.Arch.X86_64:
            return paths.OUT_DIR / 'windows-libcxx-x86-64-install'
        else:
            raise NotImplementedError()

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines: Dict[str, str] = super().cmake_defines
        defines['LLVM_ENABLE_RUNTIMES'] = 'libcxx;libcxxabi'
        defines['LLVM_ENABLE_PER_TARGET_RUNTIME_DIR'] = 'ON'

        defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'
        defines['LIBCXX_ENABLE_NEW_DELETE_DEFINITIONS'] = 'ON'
        defines['LIBCXXABI_ENABLE_NEW_DELETE_DEFINITIONS'] = 'OFF'
        defines['LIBCXX_CXX_ABI'] = 'libcxxabi'
        defines['LIBCXX_HAS_WIN32_THREAD_API'] = 'ON'
        defines['LIBCXX_TEST_COMPILER_FLAGS'] = defines['CMAKE_CXX_FLAGS']
        defines['LIBCXX_TEST_LINKER_FLAGS'] = defines['CMAKE_EXE_LINKER_FLAGS']
        defines['LIBCXX_TARGET_TRIPLE'] = self._config.llvm_triple
        defines['LIBCXXABI_TARGET_TRIPLE'] = self._config.llvm_triple

        # Build only the static library.
        defines['LIBCXX_ENABLE_SHARED'] = 'OFF'
        defines['LIBCXXABI_ENABLE_SHARED'] = 'OFF'
        defines['LIBCXX_ENABLE_EXPERIMENTAL_LIBRARY'] = 'OFF'

        if self.enable_assertions:
            defines['LIBCXX_ENABLE_ASSERTIONS'] = 'ON'
            defines['LIBCXXABI_ENABLE_ASSERTIONS'] = 'ON'

        return defines

    def install_config(self) -> None:
        super().install_config()

        # The per-target directory uses '-w64-' instead of '-pc-'.
        if self._config.target_arch == hosts.Arch.X86_64:
            triple_dir = 'x86_64-w64-windows-gnu'
        else:
            triple_dir = 'i686-w64-windows-gnu'

        win_install_dir = WindowsToolchainBuilder.install_dir

        if self._config.target_arch == hosts.Arch.X86_64:
            # Copy the x86-64 library and the non-target-specific headers to the sysroot. Clang
            # doesn't automatically find __config_site in a per-triple include directory, so copy
            # that header to the non-specific directory.
            sysroot = self._config.sysroot
            shutil.copy(self.install_dir / 'lib' / triple_dir / 'libc++.a', sysroot / 'lib')
            shutil.copy(self.install_dir / 'lib' / triple_dir / 'libc++abi.a', sysroot / 'lib')
            shutil.copytree(self.install_dir / 'include' / 'c++' / 'v1',
                            sysroot / 'include' / 'c++' / 'v1', dirs_exist_ok=True)
            shutil.copy(self.install_dir / 'include' / triple_dir / 'c++' / 'v1' / '__config_site',
                        sysroot / 'include' / 'c++' / 'v1')

            # Copy the non-target-specific headers into the output Windows toolchain.
            shutil.copytree(self.install_dir / 'include' / 'c++' / 'v1',
                            win_install_dir / 'include' / 'c++' / 'v1',
                            dirs_exist_ok=True)

            # Copy the libraries into the output Windows toolchain.
            # TODO: Maybe we don't need these, because there are per-triple libraries, but I'm not
            # sure who might be using them.
            lib_dir = win_install_dir / 'lib'
            lib_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.install_dir / 'lib' / triple_dir / 'libc++.a', lib_dir)
            shutil.copy(self.install_dir / 'lib' / triple_dir / 'libc++abi.a', lib_dir)

            # Place the x86-64 __config_site header into the non-target-specific include directory.
            # TODO: Maybe we don't need this header either.
            shutil.copy(self.install_dir / 'include' / triple_dir / 'c++' / 'v1' / '__config_site',
                        win_install_dir / 'include' / 'c++' / 'v1')

        # Copy the per-triple libraries and __config_site header to per-triple
        # lib/include directories in both the generated Linux and Windows toolchains.
        for host_dir in [win_install_dir, Stage2Builder.install_dir]:
            lib_dir = host_dir / 'lib' / triple_dir
            lib_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.install_dir / 'lib' / triple_dir / 'libc++.a', lib_dir)
            shutil.copy(self.install_dir / 'lib' / triple_dir / 'libc++abi.a', lib_dir)
            include_dir = host_dir / 'include' / triple_dir / 'c++' / 'v1'
            include_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.install_dir / 'include' / triple_dir / 'c++' / 'v1' / '__config_site', include_dir)


class WindowsToolchainBuilder(base_builders.LLVMBuilder):
    name: str = 'windows-x86-64'
    install_dir: Path = paths.OUT_DIR / 'windows-x86-64-install'
    toolchain_name: str = 'stage1'
    build_lldb: bool = True
    lto: bool = False
    profdata_file: Optional[Path] = None

    @property
    def _is_msvc(self) -> bool:
        return isinstance(self._config, configs.MSVCConfig)

    @property
    def llvm_targets(self) -> Set[str]:
        return constants.ANDROID_TARGETS

    @property
    def llvm_projects(self) -> Set[str]:
        proj = {'clang', 'clang-tools-extra', 'lld', 'polly'}
        if self.build_lldb:
            proj.add('lldb')
        return proj

    @property
    def llvm_runtime_projects(self) -> Set[str]:
        return {}

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
        defines['LLVM_NATIVE_TOOL_DIR'] = str(self.toolchain.build_path / 'bin')
        if self.build_lldb:
            defines['LLDB_PYTHON_RELATIVE_PATH'] = f'lib/python{paths._PYTHON_VER}/site-packages'
            defines['LLDB_PYTHON_EXE_RELATIVE_PATH'] = f'python3'
            defines['LLDB_PYTHON_EXT_SUFFIX'] = '.exe'
        if self.lto:
            defines['LLVM_ENABLE_LTO'] = 'Thin'
        if self.profdata_file:
            defines['LLVM_PROFDATA_FILE'] = str(self.profdata_file)

        defines['CMAKE_CXX_STANDARD'] = '17'

        defines['ZLIB_INCLUDE_DIR'] = str(paths.WIN_ZLIB_INCLUDE_PATH)
        defines['ZLIB_LIBRARY_DEBUG'] = str(paths.WIN_ZLIB_LIB_PATH / 'libz.a')
        defines['ZLIB_LIBRARY_RELEASE'] = str(paths.WIN_ZLIB_LIB_PATH / 'libz.a')

        return defines

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        if not self._is_msvc:
            # Use static-libgcc to avoid runtime dependence on libgcc_eh.
            ldflags.append('-static-libgcc')
            # pthread is needed by libgcc_eh.
            ldflags.append('-pthread')

            libpath_prefix = '-L'
        else:
            libpath_prefix = '/LIBPATH:'

        ldflags.append(libpath_prefix + str(paths.WIN_ZLIB_LIB_PATH))
        return ldflags

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-DLZMA_API_STATIC')
        cflags.append('-DMS_WIN64')
        cflags.append(f'-I{paths.WIN_ZLIB_INCLUDE_PATH}')
        if self.profdata_file:
            cflags.append('-Wno-profile-instr-out-of-date')
            cflags.append('-Wno-profile-instr-unprofiled')
        return cflags

    @property
    def cxxflags(self) -> List[str]:
        cxxflags = super().cxxflags

        # Use -fuse-cxa-atexit to allow static TLS destructors.  This is needed for
        # clang-tools-extra/clangd/Context.cpp
        cxxflags.append('-fuse-cxa-atexit')
        return cxxflags

    def install_config(self) -> None:
        super().install_config()
        lldb_wrapper_path = self.install_dir / 'bin' / 'lldb.cmd'
        lldb_wrapper_path.write_text(textwrap.dedent("""\
            @ECHO OFF
            SET PYTHONHOME=%~dp0..\python3
            SET PATH=%~dp0..\python3;%PATH%
            %~dp0lldb.exe %*
            EXIT /B %ERRORLEVEL%
        """))


class TsanBuilder(base_builders.LLVMRuntimeBuilder):
    name: str = 'tsan'
    src_dir: Path = paths.LLVM_PATH / 'compiler-rt'
    config_list: List[configs.Config] = configs.android_ndk_tsan_configs()

    @property
    def install_dir(self) -> Path:
        # Installs to a temporary dir and copies to runtimes_ndk_cxx manually.
        output_dir = self.output_dir
        return output_dir.parent / (output_dir.name + '-install')

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['COMPILER_RT_BUILD_BUILTINS'] = 'OFF'
        defines['COMPILER_RT_USE_BUILTINS_LIBRARY'] = 'ON'
        defines['COMPILER_RT_SANITIZERS_TO_BUILD'] = 'tsan'
        defines['COMPILER_RT_TEST_COMPILER_CFLAGS'] = defines['CMAKE_C_FLAGS']
        defines['COMPILER_RT_DEFAULT_TARGET_TRIPLE'] = self._config.llvm_triple
        defines['COMPILER_RT_INCLUDE_TESTS'] = 'OFF'
        defines['SANITIZER_CXX_ABI'] = 'libcxxabi'
        # With CMAKE_SYSTEM_NAME='Android', compiler-rt will be installed to
        # lib/android instead of lib/linux.
        del defines['CMAKE_SYSTEM_NAME']
        libs: List[str] = []
        # Currently, -rtlib=compiler-rt (even with -unwindlib=libunwind) does
        # not automatically link libunwind.a on Android.
        libs += ['-lunwind']
        defines['SANITIZER_COMMON_LINK_LIBS'] = ' '.join(libs)
        # compiler-rt's CMakeLists.txt file deletes -Wl,-z,defs from
        # CMAKE_SHARED_LINKER_FLAGS when COMPILER_RT_USE_BUILTINS_LIBRARY is
        # set. We want this flag on instead to catch unresolved references
        # early.
        defines['SANITIZER_COMMON_LINK_FLAGS'] = '-Wl,-z,defs'
        return defines

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-funwind-tables')
        return cflags

    def install_config(self) -> None:
        # Still run `ninja install`.
        super().install_config()

        lib_dir = self.install_dir / 'lib' / 'linux'
        dst_dir = self.output_toolchain.path / 'runtimes_ndk_cxx'
        # CMake builds other libraries (fuzzer, ubsan_standalone) etc.  Only
        # install tsan libraries.
        dst_dir.mkdir(exist_ok=True)
        for tsan_lib in lib_dir.glob('*tsan*'):
            shutil.copy(tsan_lib, dst_dir)


class LibSimpleperfReadElfBuilder(base_builders.LLVMRuntimeBuilder):
    """ Build static llvm libraries for reading ELF files on both devices and hosts. It is used
        by simpleperf.
    """
    name: str = 'libsimpleperf_readelf'
    src_dir: Path = paths.LLVM_PATH / 'llvm'
    config_list = [*configs.android_configs(platform=True),
                   configs.LinuxMuslConfig(hosts.Arch.X86_64),
                   configs.LinuxMuslConfig(hosts.Arch.AARCH64),
                  ]

    @property
    def llvm_libs(self) -> List[str]:
        output = utils.check_output([str(self.toolchain.path / 'bin' / 'llvm-config'),
                                     '--libs', 'object', '--libnames', '--link-static'])
        return output.strip().split()

    ninja_targets: List[str] = llvm_libs
    target_libname: str = 'libsimpleperf_readelf.a'

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        # The build system will add '-stdlib=libc++' automatically. Since we
        # have -nostdinc++ here, -stdlib is useless. Adds a flag to avoid the
        # warnings.
        cflags.append('-Wno-unused-command-line-argument')
        return cflags

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['LLVM_NATIVE_TOOL_DIR'] = str(self.toolchain.build_path / 'bin')
        return defines

    @property
    def install_dir(self) -> Path:
        if self._config.target_os ==  hosts.Host.Windows:
            return self.output_toolchain.path / 'lib' / 'x86_64-w64-windows-gnu'
        if self._config.target_os == hosts.Host.Linux and self._config.is_musl:
            return self.output_resource_dir / self._config.llvm_triple / 'lib'
        return super().install_dir

    def install_config(self) -> None:
        self.build_readelf_lib(self.output_dir / 'lib', self.install_dir)

    def build_readelf_lib(self, llvm_lib_dir: Path, out_dir: Path, is_darwin_lib: bool = False):
        llvm_lib_dir = llvm_lib_dir.absolute()
        out_dir = out_dir.absolute()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / self.target_libname
        out_file.unlink(missing_ok=True)
        if is_darwin_lib:
            utils.check_call([str(self.toolchain.path / 'bin' / 'llvm-libtool-darwin'),
                              '--static', '-o', str(out_file)] + self.llvm_libs, cwd=llvm_lib_dir)
        else:
            with tempfile.TemporaryDirectory(dir=paths.OUT_DIR) as tmp_dirname:
                tmp_dir = Path(tmp_dirname).absolute()
                for name in self.llvm_libs:
                    lib_path = llvm_lib_dir / name
                    assert lib_path.is_file(), f'{lib_path} not found'
                    # The libraries can have object files with the same name. To avoid conflict,
                    # extract each library into a distinct directory.
                    extract_dir = tmp_dir / name[:-2]
                    extract_dir.mkdir()
                    utils.check_call([str(self.toolchain.ar), '-x', str(lib_path)], cwd=extract_dir)
                utils.check_call(f'{self.toolchain.ar} -cqs {out_file} */*', cwd=tmp_dir,
                                 shell=True)
