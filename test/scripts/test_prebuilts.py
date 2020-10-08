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

from typing import NamedTuple
import argparse
import inspect
import json
import logging
import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).resolve().parents[2]))

from data import CNSData, PrebuiltCLRecord, SoongCLRecord, ForrestPendingRecord
import forrest
import gerrit
import test_paths
import utils


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
    CNSData.loadCNSData()
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

    all_configs = json.load(open(test_paths.CONFIGS_JSON))
    cl_numbers = [cl.cl_number for cl in cls]

    for config in all_configs:
        branch = config['branch']
        target = config['target']
        tests = config['tests']
        if CNSData.ForrestPending.find(build, tag, branch, target) or \
           CNSData.Forrest.find(build, tag, branch, target):
            # Skip if this was previously scheduled.
            print(f'Skipping already-submitted config {config}.')
            continue
        invocation_id = forrest.invokeForrestRun(branch, target, cl_numbers,
                                                 tests, args.tag)
        record = ForrestPendingRecord(
            prebuilt_build_number=build,
            invocation_id=invocation_id,
            tag=tag,
            branch=branch,
            target=target)
        CNSData.ForrestPending.addInvocation(record)


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

    args = parser.parse_args()
    if not args.prepare_only and not args.tag:
        raise RuntimeError('Provide a --tag argument for Forrest invocations' +
                           ' or use --prepare-only to only prepare Gerrit CLs.')

    return args


def main():
    logging.basicConfig(level=logging.INFO)
    args = parse_args()
    do_prechecks()

    cls = prepareCLs(args)
    if args.prepare_only:
        return

    invokeForrestRuns(cls, args)


if __name__ == '__main__':
    main()
