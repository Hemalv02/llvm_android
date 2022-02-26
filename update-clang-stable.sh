#!/bin/bash

if [ $# -ne 1 ]; then
  echo "Usage: update-clang-stable.sh <clang-rXYZ>"
  exit 1
fi

PREBUILT=$1
cd ../../prebuilts/clang/host/linux-x86

rm -rf clang-stable/*
mkdir clang-stable/bin
mkdir clang-stable/lib64
mkdir clang-stable/share

for bin in clang-format git-clang-format; do
  cp $PREBUILT/bin/$bin clang-stable/bin
done

cp -d $PREBUILT/lib64/libclang.* clang-stable/lib64
cp -d $PREBUILT/lib64/libc++.so* clang-stable/lib64
cp -rd $PREBUILT/lib64/python3 clang-stable/lib64/python3
cp -rd $PREBUILT/share/clang clang-stable/share

cd clang-stable/lib64 && ln -s libclang.so.* libclang.so && cd ../..

echo "All contents in clang-stable are copies of $PREBUILT." > clang-stable/README.md
