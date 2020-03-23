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
"""Helpers for paths."""

import os
from pathlib import Path

import hosts

ANDROID_DIR = Path(__file__).resolve().parents[2]
OUT_DIR = Path(os.environ.get('OUT_DIR', ANDROID_DIR / 'out')).resolve()
LLVM_PATH = OUT_DIR / 'llvm-project'

CMAKE_BIN_PATH = ANDROID_DIR / 'prebuilts' / 'cmake' / hosts.build_host().os_tag / 'bin' / 'cmake'
NINJA_BIN_PATH = ANDROID_DIR / 'prebuilts' / 'ninja' / hosts.build_host().os_tag / 'ninja'
