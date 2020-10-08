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
"""Manage test metadata stored in CSV files"""

from typing import Callable, Generic, List, Optional, NamedTuple, TypeVar, Iterable
import csv
import io
import subprocess

import utils
import test_paths

GFS_GROUP = 'android-llvm-toolchain'
FILEUTIL_CMD_PREFIX = ['fileutil', '-gfs_user', GFS_GROUP]


def _read_cns_file(filename: str) -> str:
    """Read from CNS using fileutil cat."""
    return utils.check_output(FILEUTIL_CMD_PREFIX + ['cat', filename])


def _write_cns_file(filename: str, contents: str) -> None:
    """Write to CNS using `fileutil cp /dev/stdin <filename>`."""
    utils.check_call(
        FILEUTIL_CMD_PREFIX + ['cp', '-f', '/dev/stdin', filename],
        input=contents,
        stderr=subprocess.DEVNULL)


class PrebuiltCLRecord(NamedTuple):
    """CSV Record for a CL that uploads a Linux prebuilt."""
    revision: str
    version: str
    build_number: str
    cl_number: str
    is_llvm_next: str


class SoongCLRecord(NamedTuple):
    """CSV Record for a build/soong CL that switches Clang revision/version."""
    revision: str
    version: str
    cl_number: str


class ForrestPendingRecord(NamedTuple):
    """CSV Record for an in-progress forrest invocation."""
    prebuilt_build_number: str
    invocation_id: str
    tag: str
    branch: str
    target: str


class ForrestRecord(NamedTuple):
    """CSV Record for a completed forrest invocation."""
    prebuilt_build_number: str
    invocation_id: str
    tag: str
    branch: str
    target: str
    build_number: str
    result: str


ForrestRecordType = TypeVar('ForrestRecordType', ForrestPendingRecord,
                            ForrestRecord)
RecordType = TypeVar('RecordType', PrebuiltCLRecord, SoongCLRecord,
                     ForrestPendingRecord, ForrestRecord)


class CSVTable(Generic[RecordType]):
    """Generic class to bookkeep CSV records stored in CNS."""

    makeRow: Callable[[Iterable[str]], RecordType]

    def __init__(self, csvfile):
        self.csvfile: str = csvfile
        self.records: List[RecordType] = []
        file_contents = _read_cns_file(self.csvfile)
        reader = csv.reader(file_contents.splitlines())
        self.header = next(reader)
        for row in reader:
            self.records.append(self.makeRow(row))

    def add(self, record: RecordType, writeBack: bool = True) -> None:
        """Add a record and optionally write immediately to CNS.

        writeBack: CSV file in CNS is updated if this parameter is True.  Set to
        False if updates are batched for a writeback at the end.  NOTE: Data is
        written only if CSVTable.write() is called.
        """
        self.records.append(record)
        if writeBack:
            self.write()

    def remove(self, record: RecordType, writeBack: bool = True) -> None:
        """Remove a record and optionally write immediately to CNS.

        writeBack: CSV file in CNS is updated if this parameter is True.  Set to
        False if updates are batched for a writeback at the end.  NOTE: Data is
        written only if CSVTable.write() is called.
        """
        if record not in self.records:
            raise RuntimeError(f'Cannot remove non-existent record {record}')
        self.records.remove(record)
        if writeBack:
            self.write()

    def write(self) -> None:
        """Write records back to CSV file."""
        output = io.StringIO()
        writer = csv.writer(output, lineterminator='\n')
        writer.writerow(self.header)
        writer.writerows(self.records)

        _write_cns_file(self.csvfile, output.getvalue())

    def get(self, filter_fn: Callable[[RecordType], bool]) -> List[RecordType]:
        """Return records that match a filter."""
        return [r for r in self.records if filter_fn(r)]

    def getOne(self, filter_fn: Callable[[RecordType],
                                         bool]) -> Optional[RecordType]:
        """Return zero or one record that matches a filter.

        Raise an exception if there's more than one match.
        """
        records = self.get(filter_fn)
        if len(records) == 0:
            return None
        if len(records) > 1:
            raise RuntimeError('Expected unique match but found many.')
        return records[0]


class PrebuiltsTable(CSVTable[PrebuiltCLRecord]):
    """CSV table for bookkeeping prebuilt CLs."""

    makeRow = PrebuiltCLRecord._make

    @staticmethod
    def buildNumberCompareFn(
            build_number: str) -> Callable[[PrebuiltCLRecord], bool]:
        """Return a lambda comparing build_number of a PrebuiltCLRecord."""
        return lambda that: that.build_number == build_number

    def addPrebuilt(self, record: PrebuiltCLRecord) -> None:
        """Add a PrebuiltCL record and write back to CSV file."""
        if self.get(self.buildNumberCompareFn(record.build_number)):
            raise RuntimeError(f'Build {record.build_number} already exists')
        self.add(record)

    def getPrebuilt(self, build_number: str,
                    cl_number: Optional[str]) -> Optional[PrebuiltCLRecord]:
        """Get a PrebuiltCL record with build_number.

        If optional parameter cl_number is provided, raise an exception if the
        record's cl_number is different.
        """
        row = self.getOne(self.buildNumberCompareFn(build_number))
        if row and cl_number and row.cl_number != cl_number:
            raise RuntimeError(
                f'CL mismatch for build {build_number}. ' +
                f'User Input: {cl_number}. Data: {row.cl_number}')
        return row


class SoongCLTable(CSVTable[SoongCLRecord]):
    """CSV table for bookkeeping build/soong switchover CLs."""
    makeRow = SoongCLRecord._make

    @staticmethod
    def clangInfoCompareFn(revision,
                           version) -> Callable[[SoongCLRecord], bool]:
        """Return a lambda comparing revision and version of a SoongCLRecord."""
        return lambda that: that.revision == revision and \
                            that.version == version

    def addCL(self, record: SoongCLRecord) -> None:
        """Add a CL record and write back to CSV file."""
        filterFn = self.clangInfoCompareFn(record.revision, record.version)
        if self.get(filterFn):
            raise RuntimeError(f'Soong CL for {record} already exists')
        self.add(record)

    def getCL(self, revision: str, version: str,
              cl_number: Optional[str]) -> Optional[SoongCLRecord]:
        """Get a SoongCL record with matching clang version and revision.

        If optional parameter cl_number is provided, raise an exception if the
        record's cl_number is different.
        """
        row = self.getOne(self.clangInfoCompareFn(revision, version))
        if row and cl_number and cl_number != row.cl_number:
            raise RuntimeError(
                f'CL mismatch for clang {revision} {version}. ' +
                f'User Input: {cl_number}.  Data: {row.cl_number}')
        return row


class ForrestTableBase(CSVTable[ForrestRecordType]):
    """Base CSV table for pending and completed Forrest invocations."""

    def addInvocation(self,
                      record: ForrestRecordType,
                      writeBack=True) -> None:
        """Add invocation to CSV Table and optionally write back."""
        if self.get(lambda that: that.invocation_id == record.invocation_id):
            raise RuntimeError(f'Invocation {record} already exists')
        self.add(record, writeBack)

    def find(self, prebuilt_build_number, tag, branch,
             target) -> Optional[ForrestRecordType]:
        """Find Forrest invocation based on (prebuilt, tag, branch, target)."""
        return self.getOne(lambda that:
            that.prebuilt_build_number == prebuilt_build_number and \
            that.branch == branch and \
            that.target == target and \
            that.tag == tag)

    def findByInvocation(self, invocation_id) -> Optional[ForrestRecordType]:
        """Find Forrest invocation based on invocation_id."""
        return self.getOne(lambda that: that.invocation_id == invocation_id)


class ForrestPendingTable(ForrestTableBase[ForrestPendingRecord]):
    """CSV table for bookkeeping pending Forrest invocations."""
    makeRow = ForrestPendingRecord._make

class ForrestTable(ForrestTableBase[ForrestRecord]):
    """CSV table for bookkeeping completed Forrest invocations."""
    makeRow = ForrestRecord._make

class CNSData():
    """Wrapper for CSV Data stored in CNS."""
    Prebuilts: PrebuiltsTable
    SoongCLs: SoongCLTable
    ForrestPending: ForrestPendingTable
    Forrest: ForrestTable

    @staticmethod
    def loadCNSData() -> None:
        """Load CSV data from CNS."""
        cns_path = test_paths.cns_path()
        CNSData.Prebuilts = PrebuiltsTable(
            f'{cns_path}/{test_paths.PREBUILT_CSV}')
        CNSData.SoongCLs = SoongCLTable(f'{cns_path}/{test_paths.SOONG_CSV}')
        CNSData.ForrestPending = ForrestPendingTable(
            f'{cns_path}/{test_paths.FORREST_PENDING_CSV}')
        CNSData.Forrest = ForrestTable(f'{cns_path}/{test_paths.FORREST_CSV}')
