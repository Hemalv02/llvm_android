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
# pylint: disable=not-callable

import datetime
import logging
import os
import shlex
import shutil
import stat
import subprocess
from typing import List

import constants

THIS_DIR = os.path.realpath(os.path.dirname(__file__))


def remove(path):
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        rm_tree(path)


def rm_tree(treeDir):

    def chmod_and_retry(func, path, _):
        if not os.access(path, os.W_OK):
            os.chmod(path, stat.S_IWUSR)
            return func(path)
        raise IOError("rmtree on %s failed" % path)

    shutil.rmtree(treeDir, onerror=chmod_and_retry)


def android_path(*args):
    return os.path.realpath(os.path.join(THIS_DIR, '../../', *args))


def out_path(*args):
    out_dir = os.environ.get('OUT_DIR', android_path('out'))
    return os.path.realpath(os.path.join(out_dir, *args))


def llvm_path(*args):
    return out_path('llvm-project', *args)


def yes_or_no(prompt, default=True):
    prompt += " (Y/n)" if default else " (y/N)"
    prompt += ": "
    while True:
        reply = str(raw_input(prompt)).lower().strip()
        if len(reply) == 0:
            return default
        elif reply[0] == 'y':
            return True
        elif reply[0] == 'n':
            return False
        else:
            print("Unrecognized reply, try again")


def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


def unchecked_call(cmd, *args, **kwargs):
    """subprocess.call with logging."""
    logger().info('call:%s %s',
                  datetime.datetime.now().strftime("%H:%M:%S"),
                  list2cmdline(cmd))
    subprocess.call(cmd, *args, **kwargs)


def check_call(cmd, *args, **kwargs):
    """subprocess.check_call with logging."""
    logger().info('check_call:%s %s',
                  datetime.datetime.now().strftime("%H:%M:%S"),
                  list2cmdline(cmd))
    subprocess.check_call(cmd, *args, **kwargs)


def check_output(cmd, *args, **kwargs):
    """subprocess.check_output with logging."""
    logger().info('check_output:%s %s',
                  datetime.datetime.now().strftime("%H:%M:%S"),
                  list2cmdline(cmd))
    return subprocess.check_output(cmd, *args, **kwargs, text=True)


def check_call_d(args, stdout=None, stderr=None, cwd=None, dry_run=False):
    if not dry_run:
        return subprocess.check_call(args, stdout=stdout, stderr=stderr,
                                     cwd=cwd)
    else:
        print("Project " + os.path.basename(cwd) + ": " + list2cmdline(args))

def check_output_d(args, stderr=None, cwd=None, dry_run=False):
    if not dry_run:
        return subprocess.check_output(args, stderr=stderr, cwd=cwd,
                                       universal_newlines=True)
    else:
        print("Project " + os.path.basename(cwd) + ": " + list2cmdline(args))


def parse_version(ver: str) -> List[int]:
    """Parses a dot separated version string to int list."""
    return list(int(v) for v in ver.split('.'))


def is_available_mac_ver(ver: str) -> bool:
    """Returns whether a version string is equal to or under MAC_MIN_VERSION."""
    return parse_version(ver) <= parse_version(constants.MAC_MIN_VERSION)


def list2cmdline(args):
    """Joins arguments into a Bourne-shell cmdline.

    Like shlex.join from Python 3.8, but is flexible about the argument type.
    Each argument can be a str, a bytes, or a path-like object. (subprocess.call
    is similarly flexible.)

    Similar to the undocumented subprocess.list2cmdline, but does Bourne-style
    escaping rather than MSVCRT escaping.
    """
    return ' '.join([shlex.quote(os.fsdecode(arg)) for arg in args])
