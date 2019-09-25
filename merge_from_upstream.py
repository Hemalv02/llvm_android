#!/usr/bin/env python
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

from utils import *


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'revision', help='Revision number of llvm source.', type=int)
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
    return parser.parse_args()


def sync_upstream_branch(path):
    subprocess.check_call(['repo', 'sync', '.'], cwd=path)


def merge_projects(revision, create_new_branch, dry_run):
    if not dry_run:
        sync_upstream_branch(path)
    path = llvm_path()
    sha = get_commit_hash(revision, path)
    if sha is None:
        raise LookupError('found no sha for %s.' % (revision))
    print('Project llvm-project git hash: %s' % sha)

    if create_new_branch:
        branch_name = 'merge-upstream-r%s' % revision
        check_call_d(['repo', 'start', branch_name, '.'],
                     cwd=path,
                     dry_run=dry_run)

    # Get the info since the last tag, the format is
    #   llvm-svn.[svn]-[number of changes since tag]-[sha of the current commit]
    desc = subprocess.check_output(
        ['git', 'describe', '--tags', '--long', '--match', 'llvm-svn.[0-9]*'],
        cwd=path,
        universal_newlines=True).strip()
    _, svnNum, numChanges, _ = desc.split('-')

    # Check changes since the previous merge point
    reapplyList = []
    for i in range(int(numChanges) - 1, -1, -1):
        changeLog = subprocess.check_output([
            'git', 'show', 'HEAD~' + str(i), '--quiet', '--format=%h%x1f%B%x1e'
        ],
                                            cwd=path,
                                            universal_newlines=True)
        changeLog = changeLog.strip('\n\x1e')
        patchSha, patchRev = parse_log(changeLog)
        if patchRev is None or patchRev > revision:
            reapplyList.append(patchSha)

    # Reset to previous branch point, if necessary
    if int(numChanges) > 0:
        check_output_d([
            'git', 'revert', '--no-commit', '--no-merges',
            'llvm-' + svnNum + '...HEAD'
        ],
                       cwd=path,
                       dry_run=dry_run)
        check_output_d(
            ['git', 'commit', '-m revert to previous base llvm-' + svnNum],
            cwd=path,
            dry_run=dry_run)

    # Merge upstream revision
    check_call_d([
        'git', 'merge', '--quiet', sha, '-m',
        'Merge %s for LLVM update to %d' % (sha, revision)
    ],
                 cwd=path,
                 dry_run=dry_run)

    # Tag the merge point
    check_call_d(['git', 'tag', '-f', 'llvm-svn.' + str(revision)],
                 cwd=path,
                 dry_run=dry_run)

    # Reapply
    FNULL = open(os.devnull, 'w')
    for sha in reapplyList:
        subprocess.check_call(['git', '--no-pager', 'show', sha, '--quiet'],
                              cwd=path)

        # Check whether applying this change will cause conflict
        ret_code = subprocess.call(
            ['git', 'cherry-pick', '--no-commit', '--no-ff', sha],
            cwd=path,
            stdout=FNULL,
            stderr=FNULL)
        subprocess.check_call(['git', 'reset', '--hard'],
                              cwd=path,
                              stdout=FNULL,
                              stderr=FNULL)

        if ret_code != 0:
            print 'Change cannot merge cleanly, please manual merge if needed'
            print
            keep_going = yes_or_no('Continue?', default=False)
            if not keep_going:
                sys.exit(1)
            continue

        # Change can apply cleanly...
        reapply = yes_or_no('Reapply change?', default=True)
        if reapply:
            check_call_d(['git', 'cherry-pick', sha],
                         cwd=path,
                         stdout=FNULL,
                         stderr=FNULL,
                         dry_run=dry_run)
            # Now change the commit Change-Id.
            check_call_d(['git', 'commit', '--amend'],
                         cwd=path,
                         dry_run=dry_run)
        else:
            print 'Skipping ' + sha

        print


def get_commit_hash(revision, path):
    # Get sha and commit message body for each log.
    subprocess.check_call(['git', 'fetch', 'aosp'], cwd=path)
    p = subprocess.Popen(
        ['git', 'log', 'aosp/upstream-master', '--format=%h%x1f%B%x1e'],
        stdout=subprocess.PIPE,
        cwd=path)
    (log, _) = p.communicate()
    if p.returncode != 0:
        raise LookupError('git log for path: %s failed!' % path)

    # Log will be in reversed order.
    log = log.strip('\n\x1e').split('\x1e')

    for commit_msg in log:
        (sha, cur_revision) = parse_log(commit_msg)
        if cur_revision == revision:
            return sha
    raise LookupError('could not find an upstream sha corresponding to %s' %
                      revision)


def parse_log(raw_log):
    log = raw_log.strip().split('\x1f')
    # Extract revision number from log data.
    revision_string = log[1].strip().split('\n')[-1]
    revision = re.search(r'llvm-svn: (\d+)', revision_string)
    if revision is None:
        return (log[0], None)
    return (log[0], int(revision.group(1)))


def main():
    args = parse_args()
    merge_projects(args.revision, args.create_new_branch, args.dry_run)


if __name__ == '__main__':
    main()
