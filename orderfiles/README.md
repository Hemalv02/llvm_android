Android Order Files
====================

For the latest version of this doc, please make sure to visit:
[Android Order Files](https://android.googlesource.com/toolchain/llvm_android/+/refs/heads/main/orderfiles/README.md)

Getting started with Order files
----------------------------------
Order files are text files containing symbols representing functions names.
Linker (lld) uses order files to layout functions in a specific order.
These ordered binaries in Android will reduce page faults and improve a program's launch time due to the efficient loading of symbols during program’s cold-start.
Order files have two stages: generating and loading.
We provide the steps below for generating and loading. In addition, we have an example (dex2oat) that we used for experimenting.

Generate Order files
----------------------------------
1. Add the orderfile property in its Android.bp to enable instrumentation. The `cflags` is used to pass the mapping flag and optimization flag (`-O1` or higher) to generate the mapping file. If no optimization flag is passed, it will not generate any mapping file. There is an example below:
```
orderfile: {
    instrumentation: true,
    load_order_file: false,
    order_file_path: "",
    cflags: [
        "-O1",
        "-mllvm",
        "-orderfile-write-mapping=<filename>-mapping.txt",
    ],
}
```

2. Add `__llvm_profile_initialize_file` and `__llvm_orderfile_dump` functions and make sure they are called at the end of the code (End of `main` or `atexit`). You can also rename the output file using `__llvmprofile_set_filename` function.
```
extern "C" void __llvm_profile_initialize_file(void);
extern "C" int __llvm_orderfile_dump(void);
extern "C" void __llvm_profile_set_filename(const char *Name);

int main () {
    ...
    __llvm_profile_set_filename("<filename>.output");
    __llvm_profile_initialize_file();
    __llvm_orderfile_dump();
}
```

3. Run the binary on either a device or cuttlefish to create `<filename>.output.order` then pull it from it.

4. Convert the profraw file into hexadecimal format
```
# Convert to hexadecimal format on Linux, Mac, and ChromeOS
hexdump -C <filename>.output.order > <filename>.prof

# Convert to hexadecimal format on Windows
certutil -f -encodeHex <filename>.output.order <filename>.prof
```

5. Use [toolchain/llvm_android/orderfiles/scripts/create_orderfile.py](https://android.googlesource.com/toolchain/llvm_android/+/refs/heads/main/orderfiles/scripts/create_orderfile.py) to create an order file based on the profile and mapping files
```
python3 create-orderfile.py --profile-file <filename>.prof --mapping-file <filename>-mapping.txt --output <filename>.orderfile
```

6. (Optional) We also provide [toolchain/llvm_android/orderfiles/scripts/validate_orderfile.py](https://android.googlesource.com/toolchain/llvm_android/+/refs/heads/main/orderfiles/scripts/validate_orderfile.py) to validate the order file based on your criteria
```
python3 validate-orderfile.py --order-file <filename>.orderfile
```

Load Order file
----------------------------------
1. Make sure your order file (`<filename>.orderfile`) is saved in toolchain/pgo-profiles/orderfiles. We use this folder to find the order files in the build system.

2. Just change the orderfile property in Android.bp and it will automatically load the order file to layout the symbols
```
orderfile: {
    instrumentation: true,
    load_order_file: true,
    order_file_path: "<filename>.orderfile",
}
```

Example (Dex2oat)
----------------------------------
Dex2oat is an ART application used to verify the byte-code of an Android application and used to create an optimized native binary. We ran the build steps on dex2oat and provided the exact steps.

1. Create an orderfile property in [art/dex2oat/Android.bp](https://android.googlesource.com/platform/art/+/refs/heads/main/dex2oat/Android.bp). You should put it in the `art_cc_binary` for dex2oat. Since all of ART including dex2oat is already compiled with optimizations enabled e.g.,`-O2`, we do not need to pass an extra optimization flag to `cflags`.
```
orderfile: {
    instrumentation: true,
    load_order_file: false,
    order_file_path: "",
    cflags: [
        # Profilings increase frame sizes and ART requires specific sizes.
        # The below flag ignores the frame sizes defined.
        "-Wno-frame-larger-than=",
        # ART sometimes does a fast exit and we want to avoid it to get
        # correct symbols so we use the below macro
        "-DART_PGO_INSTRUMENTATION",
        "-mllvm",
        "-orderfile-write-mapping=dex2oat-mapping.txt",
    ],
}
```

2. Add the llvm order files functions to [art/dex2oat/dex2oat.cc](https://android.googlesource.com/platform/art/+/refs/heads/main/dex2oat/dex2oat.cc). We only want to build this for Android so we encapsulate the order file functions within the macro that is only defined for non-host builds.
```
#if defined(ART_PGO_INSTRUMENTATION)
  extern "C" void __llvm_profile_initialize_file(void);
  extern "C" int __llvm_orderfile_dump(void);
  extern "C" void __llvm_profile_set_filename(const char *Name);
#endif

int main(int argc, char** argv) {
  int result = static_cast<int>(art::Dex2oat(argc, argv));
  if (!art::kIsDebugBuild && !art::kIsPGOInstrumentation && !art::kRunningOnMemoryTool) {
    art::FastExit(result);
  }

  #if defined(ART_PGO_INSTRUMENTATION)
    __llvm_profile_set_filename("dex2oat.output");
    __llvm_profile_initialize_file();
    __llvm_orderfile_dump();
  #endif
  return result;
}
```

3. Build the ART test system by following the steps in [art/test/README.chroot.md](https://android.googlesource.com/platform/art/+/refs/heads/main/test/README.chroot.md). These steps are specifically for running ART tests on a device.

4. Run the dex2oat specific tests on the device and pull the output file and convert it to hexadecimal format
```
adb pull /data/local/art-test-chroot/dex2oat.output.order .

# Convert to hexadecimal format on Linux, Mac, and ChromeOS
hexdump -C dex2oat.output.order > dex2oat.prof

# Convert to hexadecimal format on Windows
certutil -f -encodeHex dex2oat.output.order dex2oat.prof
```

5. Create the order file from both the profile file and mapping file
```
python3 create-orderfile.py --profile-file dex2oat.prof --mapping-file dex2oat-mapping.txt --output toolchain/pgo-profiles/orderfiles/dex2oat.orderfile
```

6. Change the order file property in [art/dex2oat/Android.bp](https://android.googlesource.com/platform/art/+/refs/heads/main/dex2oat/Android.bp).
```
orderfile: {
    instrumentation: true,
    load_order_file: true,
    order_file_path: "dex2oat.orderfile",
}
```