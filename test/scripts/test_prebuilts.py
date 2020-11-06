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
# pylint: disable=invalid-name
"""Test Clang prebuilts on Android"""

from typing import List, NamedTuple, Set, Tuple
import argparse
import inspect
import logging
import pathlib
import sys
import yaml

sys.path.append(str(pathlib.Path(__file__).resolve().parents[2]))

from data import CNSData, PrebuiltCLRecord, SoongCLRecord, WorkNodeRecord
import forrest
import gerrit
import test_paths
import utils


class TestConfig(NamedTuple):
    branch_private: str  # use branch property instead
    target: str
    groups: List[str]
    tests: List[str]

    def __str__(self):
        return f'{self.branch}:{self.target}'

    @property
    def branch(self):
        if self.branch_private == 'RELEASE_BRANCH':
            return test_paths.release_branch_name()
        return self.branch_private


def _load_configs() -> List[TestConfig]:
    with open(test_paths.CONFIGS_YAML) as infile:
        configs = yaml.load(infile, Loader=yaml.FullLoader)
    result = []
    for branch, targets in configs.items():
        for target, target_config in targets.items():
            if target_config:
                # groups and tests can be empty.
                groups = target_config.get('groups', '').split()
                tests = target_config.get('tests', list())
            else:
                groups, tests = list(), list()
            result.append(
                TestConfig(
                    branch_private=branch,
                    target=target,
                    groups=groups,
                    tests=tests))

    return result


def _find_groups(all_configs: List[TestConfig]) -> Set[str]:
    groups = set()
    for config in all_configs:
        groups.update(config.groups)
    return groups


TEST_CONFIGS = _load_configs()
TEST_GROUPS = _find_groups(TEST_CONFIGS)


class ToolchainBuild(NamedTuple):
    """Record of a toolchain build."""
    build_number: str
    branch: str


def get_toolchain_build(build) -> ToolchainBuild:
    """Return ToolchainBuild record for a build."""
    toolchain_branches = ('aosp-llvm-toolchain', 'aosp-llvm-toolchain-testing')
    output = utils.check_output([
        '/google/data/ro/projects/android/ab',
        'get',
        '--raw',  # prevent color text
        f'--bid={build}',
        '--target=linux'
    ])
    # Example output is:
    #   aosp-llvm-toolchain linux 6732143 complete True
    branch, _, _, complete, success = output.split()
    is_testable = branch in toolchain_branches and complete == 'complete' and \
                  success == 'True'
    if not is_testable:
        raise RuntimeError(f'Build {build} is not testable.  '
                           f'Build info is {output}')
    return ToolchainBuild(build, branch)


def do_prechecks():
    # ensure build/soong is present.
    # TODO(pirama) build/soong is only necessary if we're uploading a new CL.
    # Consider moving this deeper.
    if not (test_paths.ANDROID_DIR / 'build' / 'soong').exists():
        raise RuntimeError('build/soong does not exist.  ' +\
                           'Execute this script in master-plus-llvm branch.')

    utils.check_gcertstatus()


def prepareCLs(args):
    """Prepare CLs for testing.

    Upload new CLs to gerrit if matching CLs not found in CNS data.
    """
    build = get_toolchain_build(args.build)

    prebuiltRow = CNSData.Prebuilts.getPrebuilt(args.build, args.prebuilt_cl)
    if prebuiltRow:
        prebuiltCL = gerrit.PrebuiltCL.getExistingCL(prebuiltRow.cl_number)
        if not prebuiltCL.equals(prebuiltRow):
            raise RuntimeError('Mismatch between CSV Data and Gerrit CL. \n' +
                               f'  {prebuiltRow}\n  {prebuiltCL}')
    else:
        # Prebuilt record not found.  Create record (from args.prebuilt_cl or
        # new prebuilts) and update records.
        if args.prebuilt_cl:
            prebuiltCL = gerrit.PrebuiltCL.getExistingCL(args.prebuilt_cl)
            if prebuiltCL.build_number != args.build:
                raise RuntimeError(
                    f'Input CL {args.cl_number} does not correspond to build {args.build}'
                )
        else:
            prebuiltCL = gerrit.PrebuiltCL.getNewCL(build.build_number,
                                                    build.branch)
        is_llvm_next = build.branch == 'aosp-llvm-toolchain-testing'
        prebuiltRow = PrebuiltCLRecord(
            revision=prebuiltCL.revision,
            version=prebuiltCL.version,
            build_number=prebuiltCL.build_number,
            cl_number=prebuiltCL.cl_number,
            is_llvm_next=str(is_llvm_next))
        CNSData.Prebuilts.addPrebuilt(prebuiltRow)

    soongRow = CNSData.SoongCLs.getCL(prebuiltRow.revision, prebuiltRow.version,
                                      args.soong_cl)
    if soongRow:
        soongCL = gerrit.SoongCL.getExistingCL(soongRow.cl_number)
        if not soongCL.equals(soongRow):
            raise RuntimeError('Mismatch between CSV Data and Gerrit CL. \n' +
                               f'  {soongRow}\n  {soongCL}')
    else:
        # Soong CL record not found.  Create record (from args.soong_cl or new
        # switchover change) and update records.
        if args.soong_cl:
            soongCL = gerrit.SoongCL.getExistingCL(args.soong_cl)
        else:
            soongCL = gerrit.SoongCL.getNewCL(prebuiltCL.revision,
                                              prebuiltCL.version)
        if soongCL.revision != prebuiltCL.revision or \
           soongCL.version != prebuiltCL.version:
            raise RuntimeError('Clang version mismatch: \n' +
                               f'  {soongCL}\n  {prebuiltCL}')
        soongRow = SoongCLRecord(
            version=soongCL.version,
            revision=soongCL.revision,
            cl_number=soongCL.cl_number)
        CNSData.SoongCLs.addCL(soongRow)

    cls = [soongCL]
    if not prebuiltCL.merged:
        cls.append(prebuiltCL)
    return cls


def invokeForrestRuns(cls, args):
    """Submit builds/tests to Forrest for provided CLs and args."""
    build, tag = args.build, args.tag

    cl_numbers = [cl.cl_number for cl in cls]

    to_run = set(args.groups) if args.groups else set()

    def _should_run(test_groups):
        if not to_run:
            # if args.groups is empty, run all tests (note: some tests may not
            # be part of any group.)
            return True
        # Run test if it is a part of a group specified in args.groups
        return any(g in to_run for g in test_groups)

    for config in TEST_CONFIGS:
        if not _should_run(config.groups):
            logging.info(f'Skipping disabled config {config}')
            continue
        branch = config.branch
        target = config.target
        tests = config.tests
        if CNSData.PendingWorkNodes.find(build, tag, branch, target) or \
           CNSData.CompletedWorkNodes.find(build, tag, branch, target):
            # Skip if this was previously scheduled.
            logging.info(f'Skipping already-submitted config {config}.')
            continue
        invocation_id = forrest.invokeForrestRun(branch, target, cl_numbers,
                                                 tests, args.tag)
        logging.info(f'Submitted {config} to forrest: {invocation_id}')
        record = WorkNodeRecord(
            prebuilt_build_number=build,
            invocation_id=invocation_id,
            tag=tag,
            branch=branch,
            target=target)
        CNSData.PendingWorkNodes.addInvocation(record)


def parse_args():
    parser = argparse.ArgumentParser(
        description=inspect.getdoc(sys.modules[__name__]))

    parser.add_argument('--build', help='Toolchain build number (from go/ab/).')
    parser.add_argument(
        '--prebuilt_cl',
        help='[Optional] Prebuilts CL (to prebuilts/clang/host/linux-x86)')
    parser.add_argument(
        '--soong_cl',
        help='[Optional] build/soong/ CL to switch compiler version')
    parser.add_argument(
        '--prepare-only',
        action='store_true',
        help='Prepare/validate CLs.  Don\'t initiate tests')

    parser.add_argument(
        '--tag',
        help=('Tag to group Forrest invocations for this test ' +
              '(and avoid duplicate submissions).'))

    parser.add_argument(
        '--groups',
        metavar='GROUP',
        choices=TEST_GROUPS,
        nargs='+',
        action='extend',
        help=f'Run tests from specified groups.  Choices: {TEST_GROUPS}')

    parser.add_argument(
        '--verbose', '-v', action='store_true', help='Print verbose output')

    args = parser.parse_args()
    if not args.prepare_only and not args.tag:
        raise RuntimeError('Provide a --tag argument for Forrest invocations' +
                           ' or use --prepare-only to only prepare Gerrit CLs.')

    return args


def main():
    args = parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level)
    do_prechecks()

    CNSData.loadCNSData()
    cls = prepareCLs(args)
    if args.prepare_only:
        return

    invokeForrestRuns(cls, args)


if __name__ == '__main__':
    main()
