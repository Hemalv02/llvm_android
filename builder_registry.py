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
"""A class to manage existing builders, so that they are discoverable."""

import logging
from typing import Callable, Dict, List, Optional


def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


class BuilderRegistry:
    """A class to manage existing builders, so that they are discoverable."""
    _builders: Dict[str, object] = dict()

    # A lambda to decide whether we should build or skip a builder."""
    _should_build: Callable[[str], bool] = lambda name: True

    @classmethod
    def set_build_filters(cls, builds: Optional[List[str]], skips: Optional[List[str]]) -> None:
        """Sets a list of targets to skip, or a list of targets to build."""
        if skips:
            skip_set = set(skips)
            cls._should_build = lambda name: name not in skip_set
        elif builds:
            build_set = set(builds)
            cls._should_build = lambda name: name in build_set
        else:
            # build all
            cls._should_build = lambda name: True

    @classmethod
    def register_and_build(cls, function):
        """A decorator to wrap a build() method for a Builder."""
        def wrapper(builder, *args, **kwargs) -> None:
            name = builder.name
            cls._builders[name] = builder
            if cls._should_build(name):
                logger().info("Building %s.", name)
                function(builder, *args, **kwargs)
            else:
                logger().info("Skipping %s.", name)
        return wrapper

    @classmethod
    def get(cls, name: str):
        """Gets the instance of a builder."""
        return cls._builders[name]
