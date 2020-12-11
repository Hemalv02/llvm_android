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

import re

patch_level = '2'
_svn_revision = 'r383902b2'
# svn_revision_next will be newer than the official svn_revision in the future.
_svn_revision_next = 'r383902'

def get_svn_revision(build_llvm_next=False):
    if build_llvm_next:
        return _svn_revision_next
    return _svn_revision


# Get the numeric portion of the version number we are working with.
# Strip the leading 'r' and possible letter (and number) suffix,
# e.g., r383902b1 => 383902
def get_svn_revision_number(build_llvm_next=False):
    svn_version = get_svn_revision(build_llvm_next)
    found = re.match(r'r(\d+)([a-z]\d*)?$', svn_version)
    if not found:
        raise RuntimeError(f'Invalid svn revision: {svn_version}')
    return found.group(1)
