#!/bin/bash
set -e

SCRIPT_PATH=$(realpath "$0")
SCRIPT_DIR=$(dirname ${SCRIPT_PATH})
BASE_DIR=$(dirname ${SCRIPT_DIR})/../..
WORK_DIR=/tmpfs/src/git/

docker build -t llvm-ubuntu-dev --quiet ${SCRIPT_DIR}
docker run -it --rm -v ${BASE_DIR}:${WORK_DIR} --workdir ${WORK_DIR} llvm-ubuntu-dev /bin/bash

