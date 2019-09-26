#!/usr/bin/env python
#
# Copyright (C) 2019 The Android Open Source Project
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
# pylint: disable=not-callable, relative-import, line-too-long, no-else-return

import argparse
import fileinput
import os.path as path
import re
import subprocess
import xml.etree.ElementTree as ET


def green_print(to_print):
    print("\033[92m" + to_print + "\033[0m")


def is_clang_project(element):
    return element.tag == "project" and \
           element.get("path") == "prebuilts-master/clang/host/linux-x86"


class KernelToolchainUpdater():

    def __init__(self):
        self.parse_args()
        self.get_clang_versions()
        self.get_clang_sha()
        self.kernel_dir = path.join(self.kernel_tree, "private", "msm-google")
        self.repo_dir = path.join(self.kernel_tree, ".repo", "manifests")
        self.topic = path.basename(self.kernel_tree) + "_" + self.clang_revision

        self.resync_tree()

        self.update_sha()
        self.commit_sha()
        self.push_manifest_change()

        self.resync_tree()

        self.update_kernel_toolchain()
        self.commit_kernel_toolchain()
        self.push_kernel_change()

    def parse_args(self):
        parser = argparse.ArgumentParser()
        # TODO: some validation of the command line args would be nice.
        parser.add_argument(
            "kernel_tree",
            help="Source directory of the repo checkout of the kernel.")
        parser.add_argument(
            "clang_bin", help="Path to the new clang binary in AOSP.")
        parser.add_argument(
            "bug_number",
            help="The bug number to be included in the commit message.")
        parser.add_argument(
            "-d", action="store_true", help="Dry run and debug.")
        parser.add_argument(
            "-n", action="store_true", help="No push (but do local changes)")
        args = parser.parse_args()
        self.bug_number = args.bug_number
        self.clang_bin = args.clang_bin
        self.kernel_tree = args.kernel_tree
        self.dry_run = args.d
        self.no_push = args.n

    def get_clang_versions(self):
        output = subprocess.check_output([self.clang_bin, "--version"])
        self.clang_revision = re.search("based on (r[0-9]+[a-z]?)",
                                        output).groups()[0]
        self.clang_version = re.search("clang version ([0-9\.]+)",
                                       output).groups()[0]

    def get_clang_sha(self):
        clang_dir = path.dirname(self.clang_bin)
        output = subprocess.check_output(
            ["git", "--no-pager", "log", "-n", "1"], cwd=clang_dir)
        self.clang_sha = re.search("commit ([0-9a-z]+)", output).groups()[0]

    def update_sha(self):
        green_print("Updating SHA")
        xml_path = path.join(self.repo_dir, "default.xml")
        if self.dry_run:
            print("Updating %s to use\n%s." % (xml_path, self.clang_sha))
            return
        # It would be great to just use the builtin XML serializer/deserializer
        # in a sane way; unfortunately this will end up reformatting
        # indentation and reordering element attributes.
        for line in fileinput.input(xml_path, inplace=True):
            try:
                element = ET.fromstring(line)
                if is_clang_project(element):
                    line = re.sub("revision=\"[0-9a-z]+\"",
                                  "revision=\"%s\"" % self.clang_sha, line)
            except ET.ParseError:
                pass
            finally:
                print(line),

    def commit_sha(self):
        green_print("Committing SHA")
        message = """
Update Clang to %s based on %s

Bug: %s
""".strip() % (self.clang_version, self.clang_revision, self.bug_number)
        command = "git commit -asm"
        if self.dry_run:
            print(command + " \"" + message + "\"")
            return
        subprocess.check_output(
            command.split(" ") + [message], cwd=self.repo_dir)

    def push_manifest_change(self):
        green_print("Pushing manifest change")
        output = subprocess.check_output(["repo", "--no-pager", "info"],
                                         cwd=self.repo_dir)
        repo_branch = output.split("\n")[0].split(" ")[2]
        command = "git push origin HEAD:refs/for/%s -o topic=%s" % (repo_branch,
                                                                    self.topic)
        if self.dry_run or self.no_push:
            print(command)
            return
        subprocess.check_output(command.split(" "), cwd=self.repo_dir)

    def resync_tree(self):
        green_print("Syncing kernel tree")
        command = "repo sync -c --no-tags -q -n -j 71"
        if self.dry_run:
            print(command)
            return
        subprocess.check_output(command.split(" "), cwd=self.repo_dir)

    def update_kernel_toolchain(self):
        green_print("Updating kernel toolchain")
        config_path = path.join(self.kernel_dir, "build.config.common")
        if self.dry_run:
            print("Updating %s to use %s." % (config_path, self.clang_revision))
            return
        for line in fileinput.input(config_path, inplace=True):
            line = re.sub("clang-r[0-9a-z]+", "clang-" + self.clang_revision,
                          line)
            print(line),

    def commit_kernel_toolchain(self):
        green_print("Committing kernel toolchain")
        message = """
ANDROID: clang: update to %s

Bug: %s
""".strip() % (self.clang_version, self.bug_number)
        command = "git commit -asm"
        if self.dry_run:
            print(command + " \"" + message + "\"")
            return
        # TODO: it might be nice to `git checkout -b <topic>` before committing.
        subprocess.check_output(
            command.split(" ") + [message], cwd=self.kernel_dir)

    def push_kernel_change(self):
        green_print("Pushing kernel change")
        xml_path = path.join(self.repo_dir, "default.xml")
        remote = subprocess.check_output(["git", "--no-pager", "remote"],
                                         cwd=self.kernel_dir).strip()
        for project in ET.parse(xml_path).iter("project"):
            if (project.get("path") == "private/msm-google"):
                command = "git push %s HEAD:refs/for/%s -o topic=%s" % (
                    remote, project.get("revision"), self.topic)
                if self.dry_run or self.no_push:
                    print(command)
                    return
                subprocess.check_output(command.split(" "), cwd=self.kernel_dir)
                break


if __name__ == "__main__":
    KernelToolchainUpdater()
