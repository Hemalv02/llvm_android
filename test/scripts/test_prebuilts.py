#!/usr/bin/env python3
#
# Copyright (C) 2020 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# pylint: disable=not-callable
"""Test Clang prebuilts on Android"""

from typing import NamedTuple, Tuple
import argparse
import inspect
import logging
import os
import pathlib
import sys
import subprocess

sys.path.append(str(
        pathlib.Path(__file__).resolve().parents[2]))

import gerrit
import paths
import utils


class ArgParser(argparse.ArgumentParser):

    def __init__(self):
        super(ArgParser, self).__init__(
            description=inspect.getdoc(sys.modules[__name__]))

        self.add_argument(
            'build', metavar='BUILD', help='Toolchain build number to test.')


class ToolchainBuild(NamedTuple):
    build_id: str
    branch: str


def get_toolchain_build(build) -> ToolchainBuild:
    toolchain_branches = ('aosp-llvm-toolchain', 'aosp-llvm-toolchain-testing')
    output = utils.check_output(['/google/data/ro/projects/android/ab', 'get',
                                 '--raw', # prevent color text
                                 '--bid', build,
                                 '--target', 'linux'])
    # Example output is:
    #   aosp-llvm-toolchain linux 6732143 complete True
    branch, _, _, complete, success = output.split()
    is_testable = branch in toolchain_branches and complete == 'complete' and \
                  success == 'True'
    if not is_testable:
        raise RuntimeError(f'Build {build} is not testable.  '
                           f'Build info is {output}')
    return ToolchainBuild(build, branch)


def prepare_ab_test_topic(
    toolchain_build: ToolchainBuild
) -> Tuple['gerrit.GerritChange', 'gerrit.GerritChange']:
    is_test_prebuilt = toolchain_build.branch == 'aosp-llvm-toolchain-testing'

    # Get prebuilts and soong switchover change
    prebuilt_cl = gerrit.get_prebuilt_change(toolchain_build.build_id)

    switchover_cl = gerrit.get_soong_change(toolchain_build.build_id,
                                            prebuilt_cl.clang_info())

    # If the prebuilts are not merged, the two changes must be set to the same
    # topic.  If the prebuilt CL already have a topic set, prefer that.
    if not prebuilt_cl.is_merged():
        topic = prebuilt_cl.topic()
        if topic is None:
            tag = 'testing-prebuilt' if is_test_prebuilt else 'prebuilt'
            topic = f'clang-{tag}-{toolchain_build.build_id}'
            prebuilt_cl.set_topic(topic)
        switchover_cl.set_topic(topic)

    return (prebuilt_cl, switchover_cl)


def do_prechecks():
    # ensure build/soong is present.
    if not (paths.ANDROID_DIR / 'build' / 'soong').exists():
        raise RuntimeError('build/soong does not exist.  ' +\
                           'Execute this script in master-plus-llvm branch.')

    # ensure gcertstatus
    try:
        utils.check_call(
            ['gcertstatus', '-quiet', '-check_remaining=1h'])
    except subprocess.CalledProcessError:
        print('Run prodaccess before executing this script.')
        sys.exit(1)


def main():
    logging.basicConfig(level=logging.INFO)
    do_prechecks()

    args = ArgParser().parse_args()

    build = get_toolchain_build(args.build)
    cls = prepare_ab_test_topic(build)
    print(f'Cls to test: {cls[0].change_number()}, {cls[1].change_number()}')


if __name__ == '__main__':
    main()
