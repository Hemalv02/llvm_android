#!/bin/bash
set -e

# Set up system dependencies
sudo apt update
sudo apt install -y bison rsync

TOP=$(cd $(dirname $0)/../../.. && pwd)
OUT=$TOP/out
DIST=$TOP/dist
python_src=$TOP/toolchain/llvm_android

mkdir "${DIST}"

if [ $LLVM_BUILD_TYPE == "TOT" ]; then
   OUT_DIR="${OUT}" DIST_DIR="${DIST}" $TOP/prebuilts/python/linux-x86/bin/python3 \
   $python_src/build.py --build-llvm-tot --create-tar --build-name "${KOKORO_BUILD_ID}" \
   --no-build=windows
elif [ $LLVM_BUILD_TYPE == "AOSP" ]; then
   OUT_DIR="${OUT}" DIST_DIR="${DIST}" $TOP/prebuilts/python/linux-x86/bin/python3 \
   $python_src/build.py --lto --pgo --bolt --create-tar --build-name "${KOKORO_BUILD_ID}" \
   --no-build=windows
else
   echo "Error: requires LLVM_BUILD_TYPE"
fi

# Kokoro will rsync back everything created by the build. This can take 0
# minutes for our out directory. Clean up these files if our build was
# successful.
rm -rf "${OUT}"
