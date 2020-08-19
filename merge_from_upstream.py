#!/usr/bin/env python3
#
# Copyright (C) 2017 The Android Open Source Project
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

import argparse
import os
import re
import subprocess
import sys

import paths
import utils


def parse_args():
    parser = argparse.ArgumentParser(description="""
        Update toolchain/llvm-project to a selected revision of upstream-master.

        With one of sha and rev, both can be get by using external/toolchain-utils/git_llvm_rev.py.

        The merge of cherry-picked patches (which are in patches/) are delayed
        to when building with build.py.
    """)
    parser.add_argument('--sha', required=True, help='aosp/upstream-master SHA to be merged')
    parser.add_argument('--rev', required=True, help='the svn revision number for update')
    parser.add_argument(
        '--create-new-branch',
        action='store_true',
        default=False,
        help='Create new branch using `repo start` before '
        'merging from upstream.')
    parser.add_argument(
        '--dry-run',
        action='store_true',
        default=False,
        help='Dry run, does not actually commit changes to local workspace.')
    args = parser.parse_args()
    if args.rev.startswith('r'):
        # change r383902 to 383902
        args.rev = args.rev[1:]
    args.rev = int(args.rev)
    return args


def sync_branch(path):
    subprocess.check_call(['repo', 'sync', '.'], cwd=path)

def fetch_upstream(path):
    subprocess.check_call(['git', 'fetch', 'aosp'], cwd=path)

def merge_projects(sha, revision, create_new_branch, dry_run):
    path = paths.TOOLCHAIN_LLVM_PATH
    if not dry_run:
        sync_branch(path)
    fetch_upstream(path)
    print('Project llvm-project svn: %d  sha: %s' % (revision, sha))

    if create_new_branch:
        branch_name = 'merge-upstream-r%d' % revision
        utils.check_call(['repo', 'start', branch_name, '.'],
                     cwd=path,
                     dry_run=dry_run)

    # Merge upstream revision
    utils.check_call([
        'git', 'merge', '--quiet', sha, '-m',
        'Merge %s for LLVM update to %d' % (sha, revision)
    ],
        cwd=path,
        dry_run=dry_run)


def main():
    args = parse_args()
    merge_projects(args.sha, args.rev, args.create_new_branch, args.dry_run)


if __name__ == '__main__':
    main()
