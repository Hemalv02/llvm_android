#!/bin/bash
set -e

function cleanup {
  # Kokoro will rsync back everything created by the build. This can take up to 10
  # minutes for our out directory. Clean up these files at the end.
  rm -rf "${OUT}"
}

trap cleanup EXIT

TOP=$(cd $(dirname $0)/../../.. && pwd)
OUT=$TOP/out
DIST=$TOP/dist
python_src=$TOP/toolchain/llvm_android

mkdir "${DIST}"

if [ $LLVM_BUILD_TYPE == "linux-TOT" ]; then
  OUT_DIR="${OUT}" DIST_DIR="${DIST}" $TOP/prebuilts/python/linux-x86/bin/python3 \
  $python_src/build.py --build-llvm-tot --create-tar --build-name "${KOKORO_BUILD_NUMBER}" \
  --no-build=windows
elif [ $LLVM_BUILD_TYPE == "linux-master" ]; then
  OUT_DIR="${OUT}" DIST_DIR="${DIST}" $TOP/prebuilts/python/linux-x86/bin/python3 \
  $python_src/build.py --lto --pgo --bolt --create-tar --build-name "${KOKORO_BUILD_NUMBER}" \
  --no-build=windows
elif [ $LLVM_BUILD_TYPE == "darwin-master" ]; then
  OUT_DIR="${OUT}" DIST_DIR="${DIST}" $TOP/prebuilts/python/darwin-x86/bin/python3 \
  $python_src/build.py --lto --pgo --create-tar --build-name "${KOKORO_BUILD_NUMBER}"
elif [ $LLVM_BUILD_TYPE == "windows-master" ]; then
  OUT_DIR="${OUT}" DIST_DIR="${DIST}" $TOP/prebuilts/python/linux-x86/bin/python3 \
  $python_src/build.py --create-tar --build-name "${KOKORO_BUILD_NUMBER}" \
  --no-build=linux
else
  echo "Error: requires LLVM_BUILD_TYPE"
fi

