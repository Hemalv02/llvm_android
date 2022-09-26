#!/bin/bash
set -e

TOP=$(cd $(dirname $0)/../../.. && pwd)
OUT=$TOP/out
DIST=$TOP/dist
python_src=$TOP/toolchain/llvm_android
clang_src=${KOKORO_GFILE_DIR}/prod/android-llvm/linux-tot/continuous/${KOKORO_BUILD_NUMBER}
clang_prebuilt=`find $clang_src -name 'clang-*.tar.bz2'`
clang_dir=`dirname $clang_src/$clang_prebuilt`

mkdir "${DIST}"

DIST_DIR="${DIST}" OUT_DIR="${OUT}" $TOP/prebuilts/python/linux-x86/bin/python3 \
python_src/test_compiler.py --build-only --target aosp_x86_64-userdebug --no-clean-built-target \
--module libart ./ --clang-package-path $clang_dir
