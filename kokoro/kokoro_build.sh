#!/bin/bash
set -e

TOP=$(cd $(dirname $0)/../../.. && pwd)
OUT=$TOP/out
DIST=$TOP/dist
python_src=$TOP/toolchain/llvm_android

mkdir "${DIST}"

OUT_DIR="${OUT}" DIST_DIR="${DIST}" $TOP/prebuilts/python/linux-x86/bin/python3 \
$python_src/build.py --lto --pgo --bolt --create-tar --build-name "${KOKORO_BUILD_ID}" \
--no-build=windows

# Kokoro will rsync back everything created by the build. This can take 0
# minutes for our out directory. Clean up these files if our build was
# successful.
rm -rf "${OUT}"
