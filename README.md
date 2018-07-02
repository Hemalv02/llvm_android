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

If building on Linux, pass the `--no-build-windows` to `build.py` to skip
building Clang for Windows.

More Information
----------------

We have a public mailing list that you can subscribe to:
[android-llvm@googlegroups.com](https://groups.google.com/forum/#!forum/android-llvm)

