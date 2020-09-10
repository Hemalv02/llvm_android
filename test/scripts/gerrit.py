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
"""Upload and query CLs on Gerrit"""

from typing import Optional, Tuple
import contextlib
import json
import os
import urllib.parse

import paths
import utils

AOSP_GERRIT_ENDPOINT = 'https://android-review.googlesource.com'


def gerrit_query_change(query: str):
    """Return JSON output of gerrit changes that match 'query'.
    (https://gerrit-review.googlesource.com/Documentation/rest-api-changes.html#list-changes)
    """
    quoted = urllib.parse.quote(query)
    output = utils.check_output(['gob-curl', '--request', 'GET',
                                 f'{AOSP_GERRIT_ENDPOINT}/changes/?q={quoted}'])
    return json.loads(output[5:])


def gerrit_set_property(cl_number: str, prop: str, payload: str):
    """Return JSON output after updating 'prop' property of gerrit CL
    'cl_number'.  'prop' can be any gerrit PUT endpoint
    (https://gerrit-review.googlesource.com/Documentation/rest-api-changes.html).
    """
    output = utils.check_output(['gob-curl', '--request', 'PUT',
                                 '-H', 'Content-Type: application/json; charset=UTF-8',
                                 '--data', payload,
                                 f'{AOSP_GERRIT_ENDPOINT}/changes/{cl_number}/{prop}',])
    return json.loads(output[5:])


class GerritChange():
    """Base class for a gerrit change that is uniquely identified by a gerrit
    change query.
    """

    def __init__(self, project: str):
        self.project = project
        self._ensure_exists()

    def __str__(self):
        raise NotImplementedError

    def _gerrit_query(self) -> str:
        # gerrit query that uniquely identifies the change.
        raise NotImplementedError

    def _gerrit_base_query(self) -> str:
        # Generic parts of the gerrit search query.
        return (f'project:platform/{self.project} ' +
                '(status: open OR status: merged)')

    def _update_gerrit_info(self) -> None:
        json_output = gerrit_query_change(self._gerrit_query())
        if len(json_output) > 1:
            raise RuntimeError('Found more than one CL.\n' +
                               json.dumps(json_output, indent=4))
        if len(json_output) == 0:
            self.gerrit_info = None
        else:
            self.gerrit_info = json_output[0]

    def upload_change(self) -> None:
        """Create and upload a change to gerrit."""
        raise NotImplementedError

    def exists(self) -> bool:
        """Does this change exist on gerrit?"""
        return self.gerrit_info is not None

    def _ensure_exists(self) -> None:
        """Upload change to gerrit if not present."""
        self._update_gerrit_info()
        if self.exists():
            return

        self.upload_change()
        self._update_gerrit_info()
        if not self.exists():
            raise RuntimeError(f'Upload of {self} succeeded ' +
                               'but unable to find it in Gerrit')

    def topic(self) -> Optional[str]:
        """Return topic of this change (or None)."""
        if self.gerrit_info and 'topic' in self.gerrit_info:
            return self.gerrit_info['topic']
        return None

    def change_number(self) -> Optional[str]:
        """Return the CL number of this change (or None)."""
        if self.gerrit_info:
            return self.gerrit_info['_number']
        return None

    def is_merged(self) -> bool:
        """Return if this topic is merged"""
        if self.gerrit_info:
            return self.gerrit_info['status'] == 'MERGED'
        return False

    def set_topic(self, topic: str) -> None:
        change_number = self.change_number()
        if not change_number:
            raise RuntimeError(f'{self} doesn\'t exist on gerrit.')
        if self.topic() == topic:
            return
        payload = json.dumps({'topic': topic})
        output = gerrit_set_property(change_number, 'topic', payload)
        if output != topic:
            raise RuntimeError(f'Set topic to {topic} for "{self}" failed')


class ClangPrebuiltChange(GerritChange):
    """Change updating clang prebuilts"""
    PROJECT = 'prebuilts/clang/host/linux-x86'

    def __init__(self, build: str):
        self.build = build
        super().__init__(self.PROJECT)

    def __str__(self):
        return f'Clang prebuilt from build {self.build}'

    def _gerrit_query(self):
        # commit message added by update-prebuilts.py has the string
        # "from build <build>."
        return self._gerrit_base_query() + f' "from build {self.build}."'

    def upload_change(self):
        if self.gerrit_info:
            return

        utils.check_call([str(paths.LLVM_ANDROID_DIR / 'update-prebuilts.py'),
                          '-br', 'aosp-llvm-toolchain-testing',
                          '--overwrite',
                          '--host', 'linux-x86',
                          '--repo-upload',
                          self.build,
                          ])

    def clang_info(self) -> Tuple[str, str]:
        if not self.gerrit_info:
            raise RuntimeError(f'{self} doesn\'t exist on gerrit.')
        # Extract svn revision from subject, whose format is
        # 'Update prebuilt Clang to r123456 (x.y.z).'
        subject = self.gerrit_info['subject'].split()
        revision = subject[4]
        clang_version = subject[5][1:-2]
        return (revision, clang_version)


class SoongSwitchoverChange(GerritChange):
    """Change updating clang version used in soong"""

    def __init__(self, clang_build: str, clang_info: Tuple[str, str]):
        self.clang_revision, self.clang_version = clang_info
        self.clang_build = clang_build
        super().__init__('build/soong')

    def __str__(self):
        return f'Soong switchover to build {self.clang_build} ' +\
               f'({self.clang_revision})'

    def _gerrit_query(self):
        # Look for clang_build in commit message since we might test different
        # prebuilts for the same clang_revision.
        return (super()._gerrit_base_query() +
                f' "Switch to clang {self.clang_revision} ' +
                f'({self.clang_build})."')

    def _switch_clang_version(self, soong_filepath) -> None:
        """Set ClangDefaultVersion and ClangDefaultShortVersion in
        soong_filepath
        """
        def rewrite(line):
            # Rewrite clang info in go initialization code of the form
            # ClangDefaultVersion           = "clang-r399163"
            # ClangDefaultShortVersion      = "11.0.4"
            replace = None
            if 'ClangDefaultVersion' in line and '=' in line:
                replace = 'clang-' + self.clang_revision
            elif 'ClangDefaultShortVersion' in line and '=' in line:
                replace = self.clang_version
            if replace:
                prefix, _, post = line.split('"')
                return f'{prefix}"{replace}"{post}'
            return line

        with open(soong_filepath) as soong_file:
            contents = soong_file.readlines()
        contents = list(map(rewrite, contents))
        with open(soong_filepath, 'w') as soong_file:
            soong_file.write(''.join(contents))

    def upload_change(self):
        if self.gerrit_info:
            return

        branch = f'clang-prebuilt-{self.clang_build}'
        # Add clang_build in commit message since we might test different
        # prebuilts for the same clang_revision.
        message = (f'[DO NOT SUBMIT] Switch to clang {self.clang_revision} ' +
                   f'({self.clang_build}).\n\n' +
                   'For testing\n' +
                   'Test: N/A\n')

        @contextlib.contextmanager
        def chdir_context(directory):
            prev_dir = os.getcwd()
            try:
                os.chdir(directory)
                yield
            finally:
                os.chdir(prev_dir)

        # Create change:
        #   - repo start
        #   - update clang version in soong
        #   - git commit
        with chdir_context(paths.ANDROID_DIR / self.project):
            utils.unchecked_call(['repo', 'abandon', branch, '.'])
            utils.check_call(['repo', 'start', branch, '.'])

            soong_filepath = 'cc/config/global.go'
            self._switch_clang_version(soong_filepath)
            utils.check_call(['git', 'add', soong_filepath])
            utils.check_call(['git', 'commit', '-m', message])

            utils.check_call(['repo', 'upload', '.',
                              '--current-branch',
                              '--yes', # Answer yes to all safe prompts
                              '--wip', # work in progress
                              '--label=Code-Review-2', # code-review -2
                              ])


def get_soong_change(build: str,
                     clang_info: Tuple[str, str]) -> SoongSwitchoverChange:
    return SoongSwitchoverChange(build, clang_info)


def get_prebuilt_change(build: str) -> ClangPrebuiltChange:
    return ClangPrebuiltChange(build)
