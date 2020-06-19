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

patch_level = '3'
_svn_revision = 'r383902c'
# svn_revision_next will be newer than the official svn_revision in the future.
_svn_revision_next = 'r391452'

def get_svn_revision(build_llvm_next=False):
    if build_llvm_next:
        return _svn_revision_next
    return _svn_revision
