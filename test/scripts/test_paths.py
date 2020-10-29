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
"""Helpers for paths used in test scripts."""

from pathlib import Path

TEST_SCRIPTS_DIR: Path = Path(__file__).resolve().parent
LLVM_ANDROID_DIR: Path = TEST_SCRIPTS_DIR.parents[1]
ANDROID_DIR: Path = TEST_SCRIPTS_DIR.parents[3]
CONFIGS_YAML: Path = TEST_SCRIPTS_DIR / 'test_configs.yaml'
CLUSTER_INFO_YAML: Path = TEST_SCRIPTS_DIR / 'cluster_info.yaml'

FORREST: Path = Path('/google/data/ro/teams/android-test/tools/forrest')
CNS_KEY_FILE: Path = Path(
    '/google/data/ro/teams/android-llvm/tests/cns_key_file.txt')
GCL_KEY_FILE: Path = Path(
    '/google/data/ro/teams/android-llvm/tests/gcl_key_file.txt')

SOONG_CSV: str = 'soong_cls.csv'
PREBUILT_CSV: str = 'prebuilt_cls.csv'
FORREST_PENDING_CSV: str = 'forrest_pending.csv'
FORREST_CSV: str = 'forrest.csv'


def _read_key_file(key_file: Path) -> str:
    with open(key_file) as infile:
        return infile.read().strip()


def cns_path() -> str:
    """Read path to CNS testdata from CNS_KEY_FILE."""
    return _read_key_file(CNS_KEY_FILE)


def gcl_path() -> str:
    """Read path to testbench GCLs from GCL_KEY_FILE."""
    return _read_key_file(GCL_KEY_FILE)
