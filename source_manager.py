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

import os
import shutil
import string
import subprocess
import sys

import android_version
import hosts
import paths
import utils


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
        '--filesdir_path', str(patch_dir),
        '--src_path', str(source_dir),
        '--use_src_head',
        '--failure_mode', failure_mode
    ]

    return utils.check_output(patch_manager_cmd)


def setup_sources(source_dir):
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
    svn_version = android_version.get_svn_revision()
    # strip the leading 'r' and letter suffix, e.g., r377782b => 377782
    svn_version = svn_version[1:].rstrip(string.ascii_lowercase)

    patch_output = apply_patches(tmp_source_dir, svn_version, patch_json,
                                 patch_dir)
    print(patch_output)

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
