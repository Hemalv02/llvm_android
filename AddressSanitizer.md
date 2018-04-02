AddressSanitizer on Android
============================

AddressSanitizer is a fast compiler-based tool for detecting memory
bugs in native code. It is comparable to Valgrind (Memcheck tool),
but, unlike it, ASan

* detects overflows on stack and global objects
* does not detect uninitialized reads and memory leaks
* is much faster (2-3x slowdown compared to Valgrind’s 20-100x)
* has less memory overhead

This document describes how to build and run parts of Android platform
with AddressSanitizer.

If you are looking to build a standalone (i.e. SDK/NDK) application
with AddressSanitizer, see these docs
instead([link](https://github.com/google/sanitizers/wiki/AddressSanitizerOnAndroid),
[link](https://github.com/google/sanitizers/wiki/AddressSanitizerOnAndroidO)).

## SANITIZE_TARGET

To build the entire platform with AddressSanitizer, run the following
commands in the same build tree.

    make -j42
    make SANITIZE_TARGET=address -j42

In this mode, userdata.img contains extra libraries and must be
flashed to the device as well. Use the following command line:

    fastboot flash userdata && fastboot flashall

### How does it work

AddressSanitizer checks are inserted into the binary at compilation
time and can not be turned off at runtime. There is also a requirement
that if a library is instrumented (built with ASan), then the main
executable has to be instrumented as well. It works fine the other way
around: instrumented executable can load both instrumented and
non-instrumented libraries.

In the context of whole-system sanitization this means that a device
needs to carry two sets of system libraries to accomodate both
instrumented and non-instrumented executables. With `SANITIZE_TARGET`,
regular libraries are installed under `/system/lib`, and ASan-ified
libraries - under `/data/asan/system/lib`.

ASan-ified executables refer to the loader as
`/system/bin/linker_asan` in `PT_INTERP` field. This is used as a
signal that the executable is fine with, and prefers ASan-ified shared
libraries; such executables have `/data/asan/system/lib` prepended to
their default library search path.

Due to limitations of Android build system we can not build two copies
of each library at once (Soong is expected to fix this). The second
invocation of make builds ASan-ified binaries (overriding regular
binaries from the first invocation), and ASan-ified libraries
(installed in `/data`).

Build system clobbers intermediate object directories when
`SANITIZE_TARGET` value has changed. This forces a rebuild of all
targets while preserving installed binaries under `/system/lib`.

Some targets can not be built with ASan:

* Statically linked executables.
* `LOCAL_CLANG:=false` targets
* `LOCAL_SANITIZE:=never` targets

Executables like this are skipped in the `SANITIZE_TARGET` build, and
the version from the first make invocation is left in `/system/bin`.

## Symbolization

Initially, ASan reports contain references to offsets in binaries and
shared libraries. To obtain source file and line information, filter
the report through either `stack` tool, or the
`external/compiler-rt/lib/asan/scripts/symbolize.py` script.

AddressSanitizer will attempt to symbolize reports online if it finds
`llvm-symbolizer` in `$PATH` (ex. `/system/bin`). Replace device
binaries with copies from the `symbols` directory for good results, or
simply use offline symbolization.

## ASAN_OPTIONS

AddressSanitizer behavior can be changed with a large number of
runtime flags. Most of them are documented
[here](https://github.com/google/sanitizers/wiki/AddressSanitizerFlags#run-time-flags)
and
[here](https://github.com/google/sanitizers/wiki/SanitizerCommonFlags).

Flags can be passed to standalone binaries by setting an environment
variable:

    adb shell ASAN_OPTIONS=verbosity=2 /system/bin/ping

In `SANITIZE_TARGET=address` build, default system environment is set
up to read flags from `/system/asan.options`. Changing that file will
affect all future processes.

To set flags for individual processes, create and/or edit
`/system/asan.options.%b`, where `%b` stands for the process name as
seen in `adb shell ps`.
