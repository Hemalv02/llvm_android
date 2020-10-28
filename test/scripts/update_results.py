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
"""Monitor forrest runs and update status of completed runs."""

try:
    import apiclient.discovery
    import apiclient.http
    from oauth2client import client as oauth2_client
except ImportError:
    missingImportString = """
  Missing necessary libraries. Try doing the following:
  $ sudo apt-get install python-pip3
  $ sudo pip3 install --upgrade google-api-python-client
  $ sudo pip3 install --upgrade oauth2client
"""
    raise ImportError(missingImportString)

from typing import Tuple
import logging
import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).resolve().parents[2]))

from data import CNSData, ForrestRecord
import utils

ANDROID_BUILD_API_SCOPE = (
    'https://www.googleapis.com/auth/androidbuild.internal')
ANDROID_BUILD_API_NAME = 'androidbuildinternal'
ANDROID_BUILD_API_VERSION = 'v2beta1'
TRADEFED_KEY_FILE = '/google/data/ro/teams/tradefed/configs/tradefed.json'


class AndroidBuildClient():
    """Helper class to query the Android build API."""

    def __init__(self):
        creds = oauth2_client.GoogleCredentials.from_stream(TRADEFED_KEY_FILE)
        self.creds = creds.create_scoped([ANDROID_BUILD_API_SCOPE])

        self.client = apiclient.discovery.build(
            ANDROID_BUILD_API_NAME,
            ANDROID_BUILD_API_VERSION,
            credentials=creds,
            discoveryServiceUrl=apiclient.discovery.DISCOVERY_URI)

    def get_build(self, forrest_invocation_id) -> Tuple[str, str]:
        """Return the build ID and target for a Forrest invocation_id."""
        request = self.client.worknode().list(
            workExecutorTypes='pendingChangeBuild',
            workPlanId=forrest_invocation_id)

        response = request.execute()
        # TODO(pirama) validate response
        worknode = response['workNodes'][0]
        if 'workOutput' not in worknode:
            return ('PENDING', 'PENDING')
        buildOutput = worknode['workOutput']['buildOutput']
        return (buildOutput['buildId'], buildOutput['target'])

    def get_build_status(self, buildId, target):
        """Return the status of a target in a build."""
        request = self.client.build().get(buildId=buildId, target=target)
        # TODO(pirama) validate response
        response = request.execute()
        status = response['buildAttemptStatus']
        if not isinstance(response['successful'], bool):
            raise RuntimeError('response[\'successful\'] is not a boolean')
        complete = (status == 'complete') and response['successful']
        return 'completed' if complete else 'failed'


def main():
    logging.basicConfig(level=logging.INFO)
    utils.check_gcertstatus()

    if len(sys.argv) > 1:
        print(f'{sys.argv[0]} doesn\'t accept any arguments')
        sys.exit(1)

    CNSData.loadCNSData()
    build_client = AndroidBuildClient()

    invocations = [r.invocation_id for r in CNSData.ForrestPending.records]
    for inv in invocations:
        pending = CNSData.ForrestPending.findByInvocation(inv)
        # Remove the pending record if it already exists in CNSData.Forrest or
        # if its invocation has finished execution.
        complete = CNSData.Forrest.findByInvocation(inv)
        if not complete:
            buildId, target = build_client.get_build(inv)
            if buildId == 'PENDING':
                continue # Build is not finished yet.

            result = build_client.get_build_status(buildId, target)
            record = ForrestRecord(
                prebuilt_build_number=pending.prebuilt_build_number,
                invocation_id=inv,
                tag=pending.tag,
                branch=pending.branch,
                target=pending.target,
                build_number=buildId,
                result=result)
            CNSData.Forrest.addInvocation(record, writeBack=False)
        CNSData.ForrestPending.remove(pending, writeBack=False)

    CNSData.Forrest.write()
    CNSData.ForrestPending.write()


if __name__ == '__main__':
    main()
