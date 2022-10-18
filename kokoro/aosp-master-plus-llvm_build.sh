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
clang_prebuilt=`find ${KOKORO_GFILE_DIR} -name 'clang-*.tar.bz2'`

mkdir "${DIST}"

# TODO: Disabled for debugging
# DIST_DIR="${DIST}" OUT_DIR="${OUT}" $TOP/prebuilts/python/linux-x86/bin/python3 \
# python_src/test_compiler.py --build-only --target aosp_x86_64-userdebug --no-clean-built-target \
# --module libart ./ --clang-package-path $clang_dir

ls -a $clang_prebuilt
