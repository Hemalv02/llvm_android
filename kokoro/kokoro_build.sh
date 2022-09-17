#!/bin/bash
set -e

top=$(cd $(dirname $0)/../../.. && pwd)
OUT=$top/out
python_src=$top/toolchain/llvm_android

OUT_DIR="${OUT}" $top/prebuilts/python/linux-x86/bin/python3 \
$python_src/build.py --lto --pgo --bolt --create-tar --build-name "${KOKORO_BUILD_ID}" \
--no-build=windows
