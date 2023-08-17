#!/usr/bin/env python3
#
# Copyright (C) 2023 The Android Open Source Project
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
# Sample Usage:
# $ python3 -m unittest orderfile_unittest.py
#
# For more verbose test information:
# $ python3 -m unittest -v orderfile_unittest.py

import os
import unittest
import subprocess

import orderfile_utils as utils

class TestCreateOrderfile(unittest.TestCase):

    def setUp(self):
        top = utils.android_build_top()
        THIS_DIR = os.getcwd()
        self.create_script = top+"/toolchain/llvm_android/orderfiles/scripts/create_orderfile.py"
        self.validate_script = top+"/toolchain/llvm_android/orderfiles/scripts/validate_orderfile.py"
        self.profile_file = top+"/toolchain/llvm_android/orderfiles/test/example.prof"
        self.mapping_file = top+"/toolchain/llvm_android/orderfiles/test/example-mapping.txt"
        self.order_file = top+"/toolchain/llvm_android/orderfiles/test/example.orderfile"
        self.denylist_file = top+"/toolchain/llvm_android/orderfiles/test/denylist.txt"
        self.output_file = THIS_DIR+"/default.orderfile"
        self.temp_file = THIS_DIR+"/temp.orderfile"

    # Test if the script creates an orderfile
    def test_create_orderfile_normal(self):
        utils.check_call(["python3", self.create_script,
                            "--profile-file", self.profile_file,
                            "--mapping-file", self.mapping_file])
        self.assertTrue(os.path.isfile(self.output_file))

        # Clean up at the end
        os.remove(self.output_file)

    # Test if no mapping/profile file isn't passed then the script errors
    def test_create_orderfile_missing_mapping_argument(self):
        with self.assertRaises(subprocess.CalledProcessError) as context:
            utils.check_error(["python3", self.create_script,
                                "--profile-file", self.profile_file])

        # Check error output that flag is required
        last_line = context.exception.output.split("\n")[-2]
        self.assertEqual(last_line,
                        "create_orderfile: error: the following arguments are required: --mapping-file")

    # Test if the script creates an orderfile named temp.orderfile not default.orderfile
    def test_create_orderfile_output_name(self):
        utils.check_call(["python3", self.create_script,
                            "--profile-file", self.profile_file,
                            "--mapping-file", self.mapping_file,
                            "--output", "temp.orderfile"])
        self.assertTrue(os.path.isfile(self.temp_file))
        self.assertFalse(os.path.isfile(self.output_file))

        # Clean up at the end
        os.remove(self.temp_file)

    # Test if the script creates an orderfile by adding the leftover mapping symbols at the end of the orderfile
    def test_create_orderfile_leftover(self):
        utils.check_call(["python3", self.create_script,
                            "--profile-file", self.profile_file,
                            "--mapping-file", self.mapping_file])
        utils.check_call(["python3", self.create_script,
                            "--profile-file", self.profile_file,
                            "--mapping-file", self.mapping_file,
                            "--leftover",
                            "--output", "temp.orderfile"])
        self.assertTrue(os.path.isfile(self.temp_file))
        self.assertTrue(os.path.isfile(self.output_file))

        first  = []
        second = []
        with open(self.output_file, "r") as f:
            for line in f:
                first.append(line.strip())

        with open(self.temp_file, "r") as f:
            for line in f:
                second.append(line.strip())

        # Leftover flag will make the second orderfile either have the same
        # number of symbols or more than the first orderfile
        self.assertGreaterEqual(len(second), len(first))

        # Both orderfiles should have the same first few symbols
        for i in range(len(first)):
            self.assertEqual(first[i], second[i])

        # Clean up at the end
        os.remove(self.temp_file)
        os.remove(self.output_file)

    # Test if the script creates an orderfile without part based on both formats
    def test_create_orderfile_denylist(self):
        # Test with CSV format
        utils.check_call(["python3", self.create_script,
                            "--profile-file", self.profile_file,
                            "--mapping-file", self.mapping_file,
                            "--denylist", "_Z4partPiii"])
        self.assertTrue(os.path.isfile(self.output_file))

        with open(self.output_file, "r") as f:
            for line in f:
                line = line.strip()
                self.assertNotEqual(line, "_Z4partPiii")

        # Clean up at the end
        os.remove(self.output_file)

        # Test with file format
        utils.check_call(["python3", self.create_script,
                            "--profile-file", self.profile_file,
                            "--mapping-file", self.mapping_file,
                            "--denylist", "@"+self.denylist_file])

        self.assertTrue(os.path.isfile(self.output_file))

        with open(self.output_file, "r") as f:
            for line in f:
                line = line.strip()
                self.assertNotEqual(line, "_Z4partPiii")

        # Clean up at the end
        os.remove(self.output_file)

    # Test if the script creates an orderfile until the last symbol
    def test_create_orderfile_last_symbol(self):
        # Test an example where main is the last symbol
        utils.check_call(["python3", self.create_script,
                            "--profile-file", self.profile_file,
                            "--mapping-file", self.mapping_file,
                            "--last-symbol", "main"])
        self.assertTrue(os.path.isfile(self.output_file))

        # Only main symbols should be in the file
        output = utils.check_output(["python3", self.validate_script,
                                    "--order-file", self.output_file,
                                    "--allowlist", "_GLOBAL__sub_I_main.cpp,main",
                                    "--denylist", "_Z5mergePiiii,_Z9mergeSortPiii,_Z4partPiii,_Z9quickSortPiii"])
        self.assertTrue(output, "Order file is valid")

        # Clean up at the end
        os.remove(self.output_file)

        # Test last-symbol has higher priority over leftover
        utils.check_call(["python3", self.create_script,
                            "--profile-file", self.profile_file,
                            "--mapping-file", self.mapping_file,
                            "--last-symbol", "main",
                            "--leftover"])
        self.assertTrue(os.path.isfile(self.output_file))

        # Only main symbols should be in the file because leftover was ignored
        output = utils.check_output(["python3", self.validate_script,
                                    "--order-file", self.output_file,
                                    "--allowlist", "_GLOBAL__sub_I_main.cpp,main",
                                    "--denylist", "_Z5mergePiiii,_Z9mergeSortPiii,_Z4partPiii,_Z9quickSortPiii"])
        self.assertTrue(output, "Order file is valid")

        # Clean up at the end
        os.remove(self.output_file)

class TestValidateOrderfile(unittest.TestCase):

    def setUp(self):
        top = utils.android_build_top()
        THIS_DIR = os.getcwd()
        self.validate_script = top+"/toolchain/llvm_android/orderfiles/scripts/validate_orderfile.py"
        self.order_file = top+"/toolchain/llvm_android/orderfiles/test/example.orderfile"
        self.denylist_file = top+"/toolchain/llvm_android/orderfiles/test/denylist.txt"
        self.partial_file = top+"/toolchain/llvm_android/orderfiles/test/partial.txt"
        self.partialb_file = top+"/toolchain/llvm_android/orderfiles/test/partial_bad.txt"
        self.allowlistv_file = top+"/toolchain/llvm_android/orderfiles/test/allowlistv.txt"
        self.output_file = THIS_DIR+"/default.orderfile"

    # Test the validate script works correctly
    def test_validate_orderfile_normal(self):
        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.order_file])
        self.assertTrue(output, "Order file is valid")

    # Test errors in vaidate script like bad type mismatch or no orderfile passed
    def test_validate_orderfile_argument_errors(self):
        with self.assertRaises(subprocess.CalledProcessError) as context:
            utils.check_error(["python3", self.validate_script])

        # Check error output that flag is required
        last_line = context.exception.output.split("\n")[-2]
        self.assertEqual(last_line,
                        "validate_orderfile: error: the following arguments are required: --order-file")

    # Test if the validate script checks partial order based on both formats
    def test_validate_orderfile_partial_flag(self):
        # Test a correct partial order in CSV format
        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.order_file,
                                        "--partial", "_Z9mergeSortPiii,_Z5mergePiiii"])

        self.assertTrue(output, "Order file is valid")

        # Test a correct partial order in file format
        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.order_file,
                                        "--partial", "@"+self.partial_file])
        self.assertTrue(output, "Order file is valid")

        # Test a partial order with only one symbol (We allow this case)
        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.order_file,
                                        "--partial", "_Z9mergeSortPiii"])
        self.assertTrue(output, "Order file is valid")

        # Test a partial order with one symbol not in orderfile
        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.order_file,
                                        "--partial", "_Z9mergeSortPiii,temp"])
        self.assertTrue(output, "Order file is valid")

        # Test a bad partial order in CSV format
        with self.assertRaises(subprocess.CalledProcessError) as context:
            utils.check_error(["python3", self.validate_script,
                                "--order-file", self.order_file,
                                "--partial", "_Z5mergePiiii,_Z9mergeSortPiii"])

        # Check the last non-empty to see if gives a RuntimeError
        # and has a message saying _Z5mergePiiii must be before _Z9mergeSortPiii in orderfile
        last_line = context.exception.output.split("\n")[-2]
        self.assertEqual(last_line,
                        "RuntimeError: _Z5mergePiiii must be before _Z9mergeSortPiii in orderfile")

        # Test a bad partial order in file format
        with self.assertRaises(subprocess.CalledProcessError) as context:
            utils.check_error(["python3", self.validate_script,
                                "--order-file", self.order_file,
                                "--partial", "@"+self.partialb_file])

        # Check the last non-empty to see if gives a RuntimeError
        # and has a message saying _Z5mergePiiii must be before _Z9mergeSortPiii in orderfile
        last_line = context.exception.output.split("\n")[-2]
        self.assertEqual(last_line,
                        "RuntimeError: _Z5mergePiiii must be before _Z9mergeSortPiii in orderfile")

    # Test if the validate script checks if symbols are present in orderfile based on both format
    def test_validate_orderfile_allowlist_flag(self):
        # Test a correct allowlist in CSV format
        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.order_file,
                                        "--allowlist", "main"])
        self.assertTrue(output, "Order file is valid")

        # Test a correct allowlist in file format
        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.order_file,
                                        "--allowlist", "@"+self.allowlistv_file])
        self.assertTrue(output, "Order file is valid")

        # Test a bad allowlist in CSV format
        with self.assertRaises(subprocess.CalledProcessError) as context:
            utils.check_error(["python3", self.validate_script,
                                "--order-file", self.order_file,
                                "--allowlist", "_Z4partPiii"])

        # Check the last non-empty to see if gives a RuntimeError
        # and has a message saying symbols in allow-list are not in orderfile
        last_line = context.exception.output.split("\n")[-2]
        self.assertEqual(last_line,
                        "RuntimeError: Some symbols in allow-list are not in the orderfile")

        # Test a bad allowlist in file format
        with self.assertRaises(subprocess.CalledProcessError) as context:
            utils.check_error(["python3", self.validate_script,
                                "--order-file", self.order_file,
                                "--allowlist", "@"+self.denylist_file])

        # Check the last non-empty to see if gives a RuntimeError
        # and has a message saying symbols in allow-list are not in orderfile
        last_line = context.exception.output.split("\n")[-2]
        self.assertEqual(last_line,
                        "RuntimeError: Some symbols in allow-list are not in the orderfile")

    # Test if the validate script checks if symbols are not present in orderfile based on both format
    def test_validate_orderfile_denylist_flag(self):
        # Test a correct denylist in CSV format
        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.order_file,
                                        "--denylist", "_Z4partPiii"])
        self.assertTrue(output, "Order file is valid")

        # Test a correct denylist in file format
        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.order_file,
                                        "--denylist", "@"+self.denylist_file])
        self.assertTrue(output, "Order file is valid")

        # Test a bad denylist in CSV format
        with self.assertRaises(subprocess.CalledProcessError) as context:
            utils.check_error(["python3", self.validate_script,
                                "--order-file", self.order_file,
                                "--denylist", "main"])

        # Check the last non-empty to see if gives a RuntimeError
        # and has a message saying "main" should not be in orderfile
        last_line = context.exception.output.split("\n")[-2]
        self.assertEqual(last_line,
                        "RuntimeError: Orderfile should not contain main")

        # Test a bad denylist in file format
        with self.assertRaises(subprocess.CalledProcessError) as context:
            utils.check_error(["python3", self.validate_script,
                                "--order-file", self.order_file,
                                "--denylist", "@"+self.allowlistv_file])

        # Check the last non-empty to see if gives a RuntimeError
        # and has a message saying "main" should not be in orderfile
        last_line = context.exception.output.split("\n")[-2]
        self.assertEqual(last_line,
                        "RuntimeError: Orderfile should not contain main")

    # Test if the validate script checks if there are a minimum number of symbols
    def test_validate_orderfile_min_flag(self):
        # Test a correct minimum number of symbols
        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.order_file,
                                        "--min", "3"])
        self.assertTrue(output, "Order file is valid")

        # Test a bad minimum number of symbols
        with self.assertRaises(subprocess.CalledProcessError) as context:
            utils.check_error(["python3", self.validate_script,
                                "--order-file", self.order_file,
                                "--min", "10"])

        # Check the last non-empty to see if gives a RuntimeError
        # and has a message saying it needs at least 10 symbols
        last_line = context.exception.output.split("\n")[-2]
        self.assertEqual(last_line,
                        "RuntimeError: The orderfile has 5 symbols but it needs at least 10 symbols")

        # Test a bad minimum number of symbols
        with self.assertRaises(subprocess.CalledProcessError) as context:
            utils.check_error(["python3", self.validate_script,
                                "--order-file", self.order_file,
                                "--min", "three"])

        # Check error output that flag has invalid type
        last_line = context.exception.output.split("\n")[-2]
        self.assertEqual(last_line,
                        "validate_orderfile: error: argument --min: invalid int value: 'three'")

    # Test if the validate script gives priority to denylist flag over other flags
    def test_validate_orderfile_denylist_priority(self):
        # Test the denylist has more priority over allowlist and should not give error
        # here because the symbol is not in the orderfile
        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.order_file,
                                        "--allowlist", "_Z4partPiii",
                                        "--denylist", "_Z4partPiii"])
        self.assertTrue(output, "Order file is valid")

        # Test the denylist has more priority over allowlist and should give error
        # here because the symbol is in the orderfile
        with self.assertRaises(subprocess.CalledProcessError) as context:
            utils.check_error(["python3", self.validate_script,
                                "--order-file", self.order_file,
                                "--allowlist", "_Z5mergePiiii",
                                "--denylist", "_Z5mergePiiii"])

        # Check the last non-empty to see if gives a RuntimeError
        # and has a message saying _Z5mergePiiii should not be in orderfile
        last_line = context.exception.output.split("\n")[-2]
        self.assertEqual(last_line,
                        "RuntimeError: Orderfile should not contain _Z5mergePiiii")

if __name__ == '__main__':
    unittest.main()
