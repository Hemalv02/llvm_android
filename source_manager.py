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

"""
Package to manage LLVM sources when building a toolchain.
"""

import logging
from pathlib import Path
import os
import re
import shutil
import string
import subprocess
import sys

import android_version
import hosts
import paths
import utils


def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


def apply_patches(source_dir, svn_version, patch_json, patch_dir,
                  failure_mode='fail'):
    """Apply patches in $patch_dir/$patch_json to $source_dir.

    Invokes external/toolchain-utils/llvm_tools/patch_manager.py to apply the
    patches.
    """

    patch_manager_cmd = [
        sys.executable,
        str(paths.TOOLCHAIN_UTILS_DIR / 'llvm_tools' / 'patch_manager.py'),
        '--svn_version', str(svn_version),
        '--patch_metadata_file', str(patch_json),
        '--src_path', str(source_dir),
        '--failure_mode', failure_mode
    ]

    return utils.check_output(patch_manager_cmd)


def write_source_info(source_dir: str, patch_output: str) -> None:
    url_prefix = 'https://android.googlesource.com/toolchain/llvm_android/+/' +\
        '{{scripts_sha}}'

    def _get_subject(patch_file):
        with open(patch_file) as pf:
            contents = pf.read()
        matches = re.search('Subject: (.*)\n', contents)
        if not matches:
            # Patch doesn't have a subject line.  Fallback to the file's
            # basename.
            return patch_file.name

        subject = matches.groups()[0]
        trim_str = '[PATCH] '
        if subject.startswith(trim_str):
            subject = subject[len(trim_str):]
        return subject


    def _format_patch_line(patch):
        if re.match('([0-9a-f]+)(_v[0-9]+)?\.patch$', patch):
            url_suffix = '/patches/cherry/' + patch
            link_text = _get_subject(paths.SCRIPTS_DIR / 'patches' / 'cherry' / patch)
        else:
            url_suffix = '/patches/' + patch
            link_text = patch
        return f'- [{link_text}]({url_prefix}{url_suffix})'

    output = []
    base_revision = android_version.get_git_sha()
    github_url = 'https://github.com/llvm/llvm-project/commits/' + base_revision
    output.append(f'Base revision: [{base_revision}]({github_url})')
    output.append('')

    patches = patch_output.strip().splitlines()
    patches_iter = iter(patches)
    assert next(patches_iter) == 'The following patches applied successfully:'
    while True:
        patch = next(patches_iter, None)
        # We may optionally have an empty line followed by patches that were not
        # applicable.
        if patch == '':
            assert next(
                patches_iter) == 'The following patches were not applicable:'
            break
        elif patch is None:
            break
        assert patch.endswith('.patch')
        output.append(_format_patch_line(patch))

    with open(paths.OUT_DIR / 'clang_source_info.md', 'w') as outfile:
        outfile.write('\n'.join(output))


def setup_sources(source_dir, skip_apply_patches=False):
    """Setup toolchain sources into source_dir.

    Copy toolchain/llvm-project into source_dir.
    Apply patches per the specification in
    toolchain/llvm_android/patches/PATCHES.json.  The function overwrites
    source_dir only if necessary to avoid recompiles during incremental builds.
    """

    copy_from = paths.TOOLCHAIN_LLVM_PATH

    # Copy llvm source tree to a temporary directory.
    tmp_source_dir = source_dir.parent / (source_dir.name + '.tmp')
    if os.path.exists(tmp_source_dir):
        shutil.rmtree(tmp_source_dir)

    # mkdir parent of tmp_source_dir if necessary - so we can call 'cp' below.
    tmp_source_parent = os.path.dirname(tmp_source_dir)
    if not os.path.exists(tmp_source_parent):
        os.makedirs(tmp_source_parent)

    # Use 'cp' instead of shutil.copytree.  The latter uses copystat and retains
    # timestamps from the source.  We instead use rsync below to only update
    # changed files into source_dir.  Using 'cp' will ensure all changed files
    # get a newer timestamp than files in $source_dir.
    # Note: Darwin builds don't copy symlinks with -r.  Use -R instead.
    reflink = '--reflink=auto' if hosts.build_host().is_linux else '-c'
    try:
      cmd = ['cp', '-Rf', reflink, copy_from, tmp_source_dir]
      subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
      # Fallback to normal copy.
      cmd = ['cp', '-Rf', copy_from, tmp_source_dir]
      subprocess.check_call(cmd)

    # patch source tree
    patch_dir = paths.SCRIPTS_DIR / 'patches'
    patch_json = os.path.join(patch_dir, 'PATCHES.json')
    svn_version = android_version.get_svn_revision_number()

    if not skip_apply_patches:
      patch_output = apply_patches(tmp_source_dir, svn_version, patch_json,
                                   patch_dir)
      logger().info(patch_output)
      write_source_info(tmp_source_dir, patch_output)

    # Copy tmp_source_dir to source_dir if they are different.  This avoids
    # invalidating prior build outputs.
    if not os.path.exists(source_dir):
        os.rename(tmp_source_dir, source_dir)
    else:
        # Without a trailing '/' in $SRC, rsync copies $SRC to
        # $DST/BASENAME($SRC) instead of $DST.
        tmp_source_dir_str = str(tmp_source_dir) + '/'

        # rsync to update only changed files.  Use '-c' to use checksums to find
        # if files have changed instead of only modification time and size -
        # which could have inconsistencies.  Use '--delete' to ensure files not
        # in tmp_source_dir are deleted from $source_dir.
        subprocess.check_call(['rsync', '-r', '--delete', '--links', '-c',
                               tmp_source_dir_str, source_dir])

        shutil.rmtree(tmp_source_dir)
    remote, url = try_set_git_remote(source_dir)
    logger().info(f'git remote url: remote: {remote} url: {url}')


def try_set_git_remote(source_dir):
    AOSP_URL = 'https://android.googlesource.com/toolchain/llvm-project'

    def get_git_remote_url(remote=None):
        try:
            if not remote:
                remote = utils.check_output([
                    'git', 'rev-parse', '--abbrev-ref', '--symbolic-full-name',
                    '@{upstream}'
                ]).strip()
                remote = remote.split('/')[0]
            url = utils.check_output(['git', 'remote', 'get-url',
                                      remote]).strip()
            return (remote, url)
        except:
            return (remote, None)

    git_dir = source_dir / '.git'
    if not git_dir.is_dir():
        return (None, None)

    with utils.chdir_context(git_dir):
        remote, url = get_git_remote_url()
        if url != AOSP_URL:
            if not remote:
                remote = 'origin'
            try:
                if Path('config').is_symlink():
                    link = utils.check_output(['readlink', 'config']).strip()
                    utils.check_call(['rm', '-f', 'config'])
                    utils.check_call(['cp', link, 'config'])
                if remote:
                    utils.check_call(
                        ['git', 'remote', 'set-url', remote, AOSP_URL])
                else:
                    utils.check_call(['git', 'remote', 'add', remote, AOSP_URL])
                remote, url = get_git_remote_url(remote)
            except:
                pass
    return (remote, url)
