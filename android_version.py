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

_llvm_next = False
_version_read = False

_patch_level = '4'
_svn_revision = 'r399163'
# svn_revision_next will be newer than the official svn_revision in the future.
_svn_revision_next = 'r400163'

def set_llvm_next(llvm_next: bool):
    if _version_read:
        raise RuntimeError('set_llvm_next() after earlier read of versions')
    global _llvm_next
    _llvm_next = llvm_next

def get_svn_revision():
    _version_read = True
    if _llvm_next:
        return _svn_revision_next
    return _svn_revision

def get_patch_level():
    _version_read = True
    if _llvm_next:
        return None
    return _patch_level
