#!/bin/bash
set -e

getent passwd $(id -un) > /tmp/passwd.docker
getent group $(id -gn) > /tmp/group.docker

SCRIPT_PATH=$(realpath "$0")
SCRIPT_DIR=$(dirname ${SCRIPT_PATH})
BASE_DIR=$(dirname ${SCRIPT_DIR})/../..
WORK_DIR=/tmpfs/src/git/

docker build -t llvm-ubuntu-dev --quiet ${SCRIPT_DIR}
docker run -it \
  --rm \
  --user $(id -u):$(id -g) \
  --volume /tmp/passwd.docker:/etc/passwd:ro \
  --volume /tmp/group.docker:/etc/group:ro \
  --volume ${BASE_DIR}:${WORK_DIR} \
  --workdir ${WORK_DIR} \
  llvm-ubuntu-dev \
  /bin/bash

