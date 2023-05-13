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

import os
from pathlib import Path
import string
import subprocess

import android_version
import hosts
import paths
import utils
import source_manager
import sys

_LLVM_ANDROID_PATH = paths.SCRIPTS_DIR
_PATCH_DIR = _LLVM_ANDROID_PATH / 'patches'
_PATCH_JSON = _PATCH_DIR / 'PATCHES.json'

_SVN_REVISION = (android_version.get_svn_revision_number())


def get_removed_patches(output):
    """
    Parse the list of removed patches from patch_manager.py's output.

    The output is of the form:
    Removed <n> old patches:
    - <patch_path1>: <patch_title1>
    - <patch_path2>: <patch_title2>
    ...
    """

    def _get_file_from_line(line):
        # each line is '- <patch_path>: patch_title\n'
        line = line[2:]
        return line[:line.find(':')]

    marker = ' old patches:\n'
    marker_start = output.find(marker)
    if marker_start == -1:
        return None
    removed = output[marker_start + len(marker):].splitlines()
    rmfiles = [_PATCH_DIR / _get_file_from_line(p) for p in removed if p]
    for rmfile in rmfiles:
        if not rmfile.exists():
            raise RuntimeError(f'Removed file {rmfile} doesn\'t exist')
    return rmfiles


def trim_patches_json():
    """Invoke patch_manager.py with failure_mode=remove_patches."""
    source_dir = paths.TOOLCHAIN_LLVM_PATH
    output = source_manager.apply_patches(source_dir, _SVN_REVISION,
                                          _PATCH_JSON, _PATCH_DIR,
                                          'remove_patches')
    return get_removed_patches(output)


def main():
    if len(sys.argv) > 1:
        print(f'Usage: {sys.argv[0]}')
        print('  Script to remove downstream patches no longer needed for ' +
              'Android LLVM version.')
        return

    # Start a new repo branch before trimming patches.
    os.chdir(_LLVM_ANDROID_PATH)
    branch_name = f'trim-patches-before-{_SVN_REVISION}'
    utils.unchecked_call(['repo', 'abandon', branch_name, '.'])
    utils.check_call(['repo', 'start', branch_name, '.'])

    removed_patches = trim_patches_json()
    if not removed_patches:
        print('No patches to remove')
        return

    # Apply the changes to git and commit.
    utils.check_call(['git', 'add', _PATCH_JSON])
    for patch in removed_patches:
        utils.check_call(['git', 'rm', str(patch)])

    message_lines = [
        f'Remove patch entries older than {_SVN_REVISION}.',
        '',
        'Removed using: python3 trim_patch_data.py',
        'Test: N/A',
    ]
    utils.check_call(['git', 'commit', '-m', '\n'.join(message_lines)])


if __name__ == '__main__':
    main()
