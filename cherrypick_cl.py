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

from __future__ import annotations
import argparse
import collections
import copy
import dataclasses
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
import urllib.request

from android_version import get_svn_revision_number
from merge_from_upstream import fetch_upstream, sha_to_revision
import paths
import source_manager
from utils import check_call, check_output


def parse_args():
    parser = argparse.ArgumentParser(description="Cherry pick upstream LLVM patches.",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--sha', nargs='+', help='sha of patches to cherry pick')
    parser.add_argument('--diff', help='Cherry pick from a differential revision e.g., D149486')
    parser.add_argument(
        '--start-version', default='llvm',
        help="""svn revision to start applying patches. 'llvm' can also be used.""")
    parser.add_argument('--verify-merge', action='store_true',
                        help='check if patches can be applied cleanly')
    parser.add_argument('--create-cl', action='store_true', help='create a CL')
    parser.add_argument('--bug', help='bug to reference in CLs created (if any)')
    parser.add_argument('--reason', help='issue/reason to mention in CL subject line')
    parser.add_argument('--verbose', help='Enable logging')
    args = parser.parse_args()
    return args


def parse_start_version(start_version: str) -> int:
    if start_version == 'llvm':
        return int(get_svn_revision_number())
    m = re.match(r'r?(\d+)', start_version)
    assert m, f'invalid start_version: {start_version}'
    return int(m.group(1))


@dataclass
class PatchItem:
    metadata: Dict[str, Any]
        # info: Optional[List[str]]
        # title: str
    platforms: List[str]
    rel_patch_path: str
    version_range: Dict[str, Optional[int]]
        # from: Optional[int]
        # until: Optional[int]

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> PatchItem:
        return PatchItem(
            metadata=d['metadata'],
            platforms=d['platforms'],
            rel_patch_path=d['rel_patch_path'],
            version_range=d['version_range'])

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self, dict_factory=collections.OrderedDict)

    @property
    def is_local_patch(self) -> bool:
        return not self.rel_patch_path.startswith('cherry/')

    @property
    def sha(self) -> str:
        m = re.match(r'cherry/(.+)\.patch', self.rel_patch_path)
        assert m, self.rel_patch_path
        return m.group(1)

    @property
    def phab_link(self) -> str:
        m = next(re.match(r'Differential Revision: (.+)', line)
            for line in open(f'patches/{self.rel_patch_path}'))

        assert m, f'No phabricator link found in: {self.rel_patch_path}'
        return m.group(1)

    @property
    def end_version(self) -> Optional[int]:
        return self.version_range.get('until', None)

    @property
    def start_version(self) -> Optional[int]:
        return self.version_range.get('from', None)

    @property
    def sort_key(item: PatchItem) -> Tuple:
        # Keep local patches at the end of the list, and don't change the
        # relative order between two local patches.
        if item.is_local_patch:
            return (True,)

        # Just before local patches, include patches with no end_version. Sort
        # them by start_version.
        if item.end_version is None:
            return (False, math.inf, item.start_version)

        # At the front of the list, sort upstream patches by ascending order of
        # end_version. Don't reorder patches with the same end_version.
        return (False, item.end_version)

    def __lt__(self, other: PatchItem) -> bool:
        """Used to sort patches in PatchList"""
        return self.sort_key < other.sort_key


class PatchList(list):
    """ a list of PatchItem """

    JSON_FILE_PATH = paths.SCRIPTS_DIR / 'patches' / 'PATCHES.json'

    @classmethod
    def load_from_file(cls) -> PatchList:
        with open(cls.JSON_FILE_PATH, 'r') as fh:
            array = json.load(fh)
        return PatchList(PatchItem.from_dict(d) for d in array)

    def save_to_file(self):
        array = [patch.to_dict() for patch in self]
        with open(self.JSON_FILE_PATH, 'w') as fh:
            json.dump(array, fh, indent=4, separators=(',', ': '), sort_keys=True)
            fh.write('\n')


def generate_patch_files(sha_list: List[str], start_version: int) -> PatchList:
    """ generate upstream cherry-pick patch files """
    upstream_dir = paths.TOOLCHAIN_LLVM_PATH
    fetch_upstream()
    result = PatchList()
    for sha in sha_list:
        if len(sha) < 40:
            sha = get_full_sha(upstream_dir, sha)
        file_path = paths.SCRIPTS_DIR / 'patches' / 'cherry' / f'{sha}.patch'
        assert not file_path.exists(), f'{file_path} already exists'
        with open(file_path, 'w') as fh:
            check_call(f'git format-patch -1 {sha} --stdout',
                       stdout=fh, shell=True, cwd=upstream_dir)

        commit_subject = check_output(
            f'git log -n1 --format=%s {sha}', shell=True, cwd=upstream_dir)
        info: Optional[List[str]] = []
        title = '[UPSTREAM] ' + commit_subject.strip()
        rel_patch_path = f'cherry/{sha}.patch'
        end_version = sha_to_revision(sha)
        metadata = { 'info': info, 'title': title }
        platforms = ['android']
        version_range: Dict[str, Optional[int]] = {
            'from': start_version,
            'until': end_version,
        }
        result.append(PatchItem(metadata, platforms, rel_patch_path, version_range))
    return result


def get_full_sha(upstream_dir: Path, short_sha: str) -> str:
    return check_output(['git', 'rev-parse', short_sha], cwd=upstream_dir).strip()


def create_cl(new_patches: PatchList, reason: str, bug: Optional[str], cherry: bool):
    file_list = [
        str(paths.SCRIPTS_DIR / 'patches' / p.rel_patch_path) for p in new_patches
    ]

    file_list += ['patches/PATCHES.json']
    check_call(['git', 'add'] + file_list)

    subject = f'[patches] Cherry pick CLS for: {reason}'
    commit_lines = [subject, '']
    script = os.path.basename(sys.argv[0])
    argv_deepcopy = copy.deepcopy(sys.argv[1:])
    for i in range(len(argv_deepcopy)):
        element = argv_deepcopy[i]
        if element.startswith('--reason'):
            del argv_deepcopy[i]
            if element == '--reason':
              del argv_deepcopy[i]
            break

    args = ' '.join(argv_deepcopy)
    auto_msg = f'This change is generated automatically by the script:\n  {script} {args}'
    commit_lines = [auto_msg, '']
    if bug:
        if bug.isnumeric():
            commit_lines += [f'Bug: http://b/{bug}', '']
        else:
            commit_lines += [f'Bug: {bug}', '']
    for patch in new_patches:
        if cherry: # Add SHA and title for each cherry-pick.
            sha = patch.sha[:11]
            subject = patch.metadata['title']
            if subject.startswith('[UPSTREAM] '):
                subject = subject[len('[UPSTREAM] '):]
            commit_line = sha + ' ' + subject
        else: # Add link to differential revision.
            commit_line = patch.phab_link

        commit_lines.append(commit_line)

    commit_lines += ['', 'Test: N/A']
    check_call(['git', 'commit', '-m', '\n'.join(commit_lines)])

def get_title_from_phab(url) -> str:
    webpage = urllib.request.urlopen(url).read()
    title = str(webpage).split('<title>')[1].split('</title>')[0]
    actual_title = ' '.join(title.split(' ')[1:])
    return actual_title

def create_patch(diff, start_version) -> PatchList:
    assert diff.startswith('D'), f'Invalid Differential Revision {diff}'

    phab_url=f'https://reviews.llvm.org/{diff}'
    patch_url_req = urllib.request.Request(f'https://reviews.llvm.org/{diff}?download=true',
                                        method="HEAD")
    patch_url = urllib.request.urlopen(patch_url_req).url

    # TODO: Add commit body and author details as well.
    title=get_title_from_phab(phab_url)
    assert title, f'Title not found for {diff}'
    print(f'Creating a patch for {title}')
    file_name=f'{diff}.patch'
    abs_file_name= paths.SCRIPTS_DIR / 'patches' / file_name
    # Download the file from `patch_url` and save in `abs_file_name`:
    urllib.request.urlretrieve(patch_url, abs_file_name)
    # Add link to Differential Revision at the beginning of the file
    patch_first_line=f'Differential Revision: {patch_url}'
    with open(abs_file_name, 'r+') as f:
        content = f.read()
        f.seek(0, 0)
        f.write(patch_first_line + '\n\n' + content)

    # Extend the PATCHES.json
    result = PatchList()
    info: Optional[List[str]] = []
    rel_patch_path = f'{file_name}'
    end_version = None
    metadata = { 'info': info, 'title': title }
    platforms = ['android']
    version_range: Dict[str, Optional[int]] = {
       'from': start_version,
       'until': end_version,
    }
    result.append(PatchItem(metadata, platforms, rel_patch_path, version_range))
    return result

def main():
    args = parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level)
    patch_list = PatchList.load_from_file()

    assert not (bool(args.sha) and bool(args.diff)), (
        'Only one of cherry-pick or patch supported.'
    )
    if args.diff:
        start_version = parse_start_version(args.start_version)
        new_patches = create_patch(args.diff, start_version)
        patch_list.extend(new_patches)
    elif args.sha:
        start_version = parse_start_version(args.start_version)
        new_patches = generate_patch_files(args.sha, start_version)
        patch_list.extend(new_patches)

    patch_list.sort()
    patch_list.save_to_file()

    if args.verify_merge:
        print('Verifying merge...')
        source_manager.setup_sources()
    if args.create_cl:
        if not args.reason:
            print('error: --create-cl requires --reason')
            exit(1)
        cherry = True if args.sha else False
        create_cl(new_patches, args.reason, args.bug, cherry)


if __name__ == '__main__':
    main()
