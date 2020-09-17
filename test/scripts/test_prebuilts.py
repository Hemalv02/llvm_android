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
import logging
import pathlib
import sys
import subprocess

sys.path.append(str(
        pathlib.Path(__file__).resolve().parents[2]))

from data import CNSData, PrebuiltCLRecord, SoongCLRecord
import gerrit
import paths
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
    if not (paths.ANDROID_DIR / 'build' / 'soong').exists():
        raise RuntimeError('build/soong does not exist.  ' +\
                           'Execute this script in master-plus-llvm branch.')

    # ensure gcertstatus
    try:
        utils.check_call(['gcertstatus', '-quiet', '-check_remaining=1h'])
    except subprocess.CalledProcessError:
        print('Run prodaccess before executing this script.')
        sys.exit(1)


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

    print(f'CLs ready to test.  {prebuiltCL}\n{soongCL}')


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

    return parser.parse_args()


def main():
    logging.basicConfig(level=logging.INFO)
    do_prechecks()

    args = parse_args()
    prepareCLs(args)


if __name__ == '__main__':
    main()
