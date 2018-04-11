#!/usr/bin/env python
#
# Copyright (C) 2018 The Android Open Source Project
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

"""Update the prebuilt lldb from the build server."""

import argparse
import inspect
import logging
import os
import shutil
import subprocess
import utils
import sys

def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


def unchecked_call(cmd, *args, **kwargs):
    """subprocess.call with logging."""
    logger().info('unchecked_call: %s', subprocess.list2cmdline(cmd))
    return subprocess.call(cmd, *args, **kwargs)


def check_call(cmd, *args, **kwargs):
    """subprocess.check_call with logging."""
    logger().info('check_call: %s', subprocess.list2cmdline(cmd))
    subprocess.check_call(cmd, *args, **kwargs)


class ArgParser(argparse.ArgumentParser):
    def __init__(self):
        super(ArgParser, self).__init__(
            description=inspect.getdoc(sys.modules[__name__]))

        self.add_argument(
            'build', metavar='BUILD',
            help='Build number to pull from the build server.')

        self.add_argument(
            '-b', '--bug', type=int,
            help='Bug to reference in commit message.')

        self.add_argument(
            '--use-current-branch', action='store_true',
            help='Do not repo start a new branch for the update.')

        self.add_argument(
            '--skip-fetch',
            '-sf',
            action='store_true',
            default=False,
            help='Skip the fetch, and only do the extraction step')

        self.add_argument(
            '--skip-cleanup',
            '-sc',
            action='store_true',
            default=False,
            help='Skip the cleanup, and leave intermediate files')


def fetch_artifact(branch, target, build, pattern):
    fetch_artifact_path = '/google/data/ro/projects/android/fetch_artifact'
    cmd = [fetch_artifact_path, '--branch={}'.format(branch),
           '--target={}'.format(target), '--bid={}'.format(build), pattern]
    check_call(cmd)


def get_lldb_package(target, build_number):
    return 'lldb-{}-{}.zip'.format(target[1], build_number)


def get_android_package(build_number):
    return 'lldb-android-{}.zip'.format(build_number)


def get_manifest(build_number):
    return 'manifest_{}.xml'.format(build_number)


def extract_package(package, install_dir):
    cmd = ['unzip', package, '-d', install_dir]
    check_call(cmd)


def update_lldb(target, build_number, use_current_branch, download_dir, bug):
    prebuilt_dir = utils.android_path('prebuilts/clang/host', target[1] + '-x86')
    os.chdir(prebuilt_dir)
    install_subdir = os.path.join(prebuilt_dir, 'lldb')

    if not use_current_branch:
        branch_name = 'update-lldb-{}'.format(build_number)
        unchecked_call(
            ['repo', 'abandon', branch_name, '.'])
        check_call(
            ['repo', 'start', branch_name, '.'])

    package = os.path.join(download_dir, get_lldb_package(target, build_number))
    if os.path.isdir(install_subdir):
        shutil.rmtree(install_subdir)
    os.makedirs(install_subdir)
    extract_package(package, install_subdir)

    android_package = os.path.join(download_dir,
            get_android_package(build_number))
    manifest = os.path.join(download_dir, get_manifest(build_number))

    shutil.copy(manifest, install_subdir)

    check_call(['git', 'add', install_subdir])

    # If there is no difference with the new files, we are already done.
    diff = unchecked_call(['git', 'diff', '--cached', '--quiet'])
    if diff == 0:
        logger().info('Bypassed commit with no diff')
        return

    message_lines = [
        'Update prebuilt LLDB to build {}.'.format(build_number),
    ]
    if bug is not None:
        message_lines.append('')
        message_lines.append('Bug: http://b/{}'.format(bug))
    message_lines.append('Test: N/A')
    message = '\n'.join(message_lines)
    check_call(['git', 'commit', '-m', message])


def fetch(targets, build_number):
    branch = 'git_lldb-master-dev'
    fetch_artifact(branch, targets[0][0], build_number,
            get_android_package(build_number))
    for target in targets:
        fetch_artifact(branch, target[0], build_number,
                get_manifest(build_number))
        fetch_artifact(branch, target[0], build_number, get_lldb_package(target,
            build_number))


def main():
    args = ArgParser().parse_args()
    logging.basicConfig(level=logging.INFO)

    do_fetch = not args.skip_fetch
    do_cleanup = not args.skip_cleanup

    download_dir = os.path.realpath('.download')
    if do_fetch:
        if os.path.isdir(download_dir):
            shutil.rmtree(download_dir)
        os.makedirs(download_dir)

    os.chdir(download_dir)

    targets = [['lldb', 'linux']]
    try:
        if do_fetch:
            fetch(targets, args.build)

        for target in targets:
            update_lldb(target, args.build, args.use_current_branch,
                         download_dir, args.bug)
    finally:
        if do_cleanup:
            shutil.rmtree(download_dir)

    return 0


if __name__ == '__main__':
    main()
