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


# The last llvm-svn number and sha found in aosp/upstream-master
LAST_REVISION = 375505
LAST_SHA = '186155b89c2d2a2f62337081e3ca15f676c9434b'

# Length of sha returned by git log --oneline.
MIN_SHA_LENGTH = 11

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('sha', help='aosp/upstream-master SHA to be merged.')
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


def merge_projects(sha, create_new_branch, dry_run):
    if len(sha) < MIN_SHA_LENGTH:
        raise LookupError('sha %s must have at least %d characters.'
                          % (sha, MIN_SHA_LENGTH))
    path = llvm_path()
    if not dry_run:
        sync_upstream_branch(path)
    commits = get_upstream_commits(path)
    if sha[:MIN_SHA_LENGTH] not in commits:
        raise LookupError('sha %s not found in aosp/upstream-master' % sha)
    revision = commits[sha[:MIN_SHA_LENGTH]]
    print('Project llvm-project svn: %d  sha: %s' % (revision, sha))

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
    print('Found %s changes since the last tag llvm-svn.%s'
          % (numChanges, svnNum))
    hasUnknownPatch = False
    for i in range(int(numChanges) - 1, -1, -1):
        changeLog = subprocess.check_output([
            'git', 'show', 'HEAD~' + str(i), '--quiet', '--format=%h%x1f%B%x1e'
        ],
                                            cwd=path,
                                            universal_newlines=True)
        changeLog = changeLog.strip('\n\x1e')
        patchSha, patchRev, cherryPickSha = parse_log(changeLog, commits)
        if patchRev is None:
            if not cherryPickSha:
                print 'To reapply local change ' + patchSha
                reapplyList.append(patchSha)
            else:
                print('Unknown cherry pick, patchSha=%s cherryPickSha=%s'
                      % (patchSha, cherryPickSha))
                hasUnknownPatch = True
        else:
            if patchRev > revision:
                print 'To reapply ' + patchSha + ' ' + str(patchRev)
                reapplyList.append(patchSha)
            else:
                print 'To skip ' + patchSha + ' ' + str(patchRev)

    if hasUnknownPatch:
        print 'Abort, cannot merge with unknown patch!'
        sys.exit(1)

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


def parse_log(raw_log, commits):
    log = raw_log.strip().split('\x1f')
    cherryPickSha = ''
    # Extract revision number from log data.
    foundRevision = 0
    for line in log[1].strip().split('\n'):
        tmp = re.search(r'^llvm-svn: (\d+)$', line)
        if tmp is not None:
            foundRevision = int(tmp.group(1))
        else:
            tmp = re.search(r'\(cherry picked from commit (.+)\)', line)
            if tmp is not None:
                cherryPickSha = tmp.group(1)
    if foundRevision:
        return (log[0], foundRevision, cherryPickSha)
    if cherryPickSha and cherryPickSha[:MIN_SHA_LENGTH] in commits:
        return (log[0], commits[cherryPickSha[:MIN_SHA_LENGTH]], cherryPickSha)
    return (log[0], None, cherryPickSha)


def get_upstream_commits(path):
    subprocess.check_call(['git', 'fetch', 'aosp'], cwd=path)
    lines = subprocess.check_output([
        'git', 'log', '--first-parent', '--oneline',
        LAST_SHA + '..aosp/upstream-master'
    ], cwd=path, universal_newlines=True).strip('\n\x1e')
    lines = lines.strip().split('\n')
    commits = {}
    svnNum = LAST_REVISION + len(lines)
    for line in lines:
        commits[line.split()[0]] = svnNum
        svnNum -= 1
    return commits


def main():
    args = parse_args()
    merge_projects(args.sha, args.create_new_branch, args.dry_run)


if __name__ == '__main__':
    main()
