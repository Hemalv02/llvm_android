Android Clang/LLVM Toolchain
============================

For the latest version of this doc, please make sure to visit:
[Android Clang/LLVM Toolchain Readme Doc](https://android.googlesource.com/toolchain/llvm_android/+/master/README.md)

You can also visit the
[Android Clang/LLVM Prebuilts Readme Doc](https://android.googlesource.com/platform/prebuilts/clang/host/linux-x86/+/master/README.md)
for more information about our prebuilt toolchains (and what versions they are based upon).

Build Instructions
------------------

```
$ mkdir llvm-toolchain && cd llvm-toolchain
$ repo init -u https://android.googlesource.com/platform/manifest -b llvm-toolchain
$ repo sync -c
$ python toolchain/llvm_android/build.py
```

If building on Linux, pass `--no-build windows` to `build.py` to skip
building Clang for Windows.

If you have an additional llvm tree built and present in your `$PATH`, then
`build.py` might fail during the Windows build of libcxxabi with the error
`'libstdc++ version must be at least 4.8.'`. The solution is to remove that
path from your `$PATH` before invoking `build.py`.

More Information
----------------

We have a public mailing list that you can subscribe to:
[android-llvm@googlegroups.com](https://groups.google.com/forum/#!forum/android-llvm)

