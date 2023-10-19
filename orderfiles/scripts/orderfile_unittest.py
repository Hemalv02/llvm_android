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

import merge_orderfile
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
                        "RuntimeError: `_Z5mergePiiii` must be before `_Z9mergeSortPiii` in orderfile")

        # Test a bad partial order in file format
        with self.assertRaises(subprocess.CalledProcessError) as context:
            utils.check_error(["python3", self.validate_script,
                                "--order-file", self.order_file,
                                "--partial", "@"+self.partialb_file])

        # Check the last non-empty to see if gives a RuntimeError
        # and has a message saying _Z5mergePiiii must be before _Z9mergeSortPiii in orderfile
        last_line = context.exception.output.split("\n")[-2]
        self.assertEqual(last_line,
                        "RuntimeError: `_Z5mergePiiii` must be before `_Z9mergeSortPiii` in orderfile")

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

class TestMergeOrderfile(unittest.TestCase):

    def setUp(self):
        top = utils.android_build_top()
        THIS_DIR = os.path.realpath(os.path.dirname(__file__))
        self.validate_script = top+"/toolchain/llvm_android/orderfiles//scripts/validate_orderfile.py"
        self.merge_script = top+"/toolchain/llvm_android/orderfiles/scripts/merge_orderfile.py"
        self.output_file = THIS_DIR+"/merged-normal.orderfile"
        self.merge_test_folder = top+"/toolchain/llvm_android/orderfiles/test/merge-test"
        self.file = top+"/toolchain/llvm_android/orderfiles/test/merge-test/merge.txt"

    # Test if the order files are merged correctly
    def test_merge_orderfile_normal(self):
        # Test a folder input
        utils.check_call(["python3", self.merge_script,
                            "--order-files", f"^{self.merge_test_folder}",
                            f"--output={self.output_file}"])
        self.assertTrue(os.path.isfile(self.output_file))

        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.output_file,
                                        "--partial", "main,b,c,d,a,e,f"])
        self.assertTrue(output, "Order file is valid")

        # Clean up at the end
        os.remove(self.output_file)

        # Test the file format with different weights
        utils.check_call(["python3", self.merge_script,
                            "--order-files", f"@{self.file}",
                            f"--output={self.output_file}"])
        self.assertTrue(os.path.isfile(self.output_file))

        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.output_file,
                                        "--partial", "main,b,c,d,e,f,a"])
        self.assertTrue(output, "Order file is valid")

        # Clean up at the end
        os.remove(self.output_file)

        # Test with CSV format
        lst = ["1.orderfile", "2.orderfile"]
        lst = [self.merge_test_folder + "/" + orderfile for orderfile in lst]
        param = ",".join(lst)
        utils.check_call(["python3", self.merge_script,
                            "--order-files", param,
                            f"--output={self.output_file}"])
        self.assertTrue(os.path.isfile(self.output_file))

        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.output_file,
                                        "--partial", "main,b,c,d,e,f"])
        self.assertTrue(output, "Order file is valid")

        # Clean up at the end
        os.remove(self.output_file)

    # Test if the simple graph functions work correctly
    def test_graph_simple_function(self):
        graph = merge_orderfile.Graph()

        # Add 0-9 to the graph
        prev = None
        for i in range(10):
            name = str(i)

            graph.addVertex(name)
            self.assertTrue(graph.checkVertex(name))

            if prev != None:
                graph.addEdge(prev, name)
                self.assertTrue(graph.checkEdgeWeight(prev, name, 1))

            prev = name

        # Numbers were never added so they should be no edge to or from them
        # and they should not be a vertex
        self.assertFalse(graph.checkVertex("100"))
        self.assertFalse(graph.checkEdge("0", "10"))
        self.assertFalse(graph.checkEdge("100", "10"))
        self.assertFalse(graph.checkEdge("0", "100"))

        # As we add an edge for two vertices, the edge weight increases
        graph.addVertex("11")
        graph.addVertex("12")
        self.assertTrue(graph.checkVertex("11"))
        self.assertTrue(graph.checkVertex("12"))
        for i in range(5):
            graph.addEdge("11","12")
            self.assertTrue(graph.checkEdgeWeight("11","12",i+1))

        # Edge got deleted so it should not be in the graph anymore
        graph.removeEdgeCompletely("11", "12")
        self.assertFalse(graph.checkEdge("11", "12"))

        # Check if you add many successors for a vertex then getOutEdges
        # will return the correct number of successor and has the edge weight
        for i in range(20, 26):
            graph.addVertex(str(i))
            self.assertTrue(graph.checkVertex(str(i)))

        for j in range(21, 26):
            graph.addEdge("20", str(j))
            self.assertTrue(graph.checkEdge("20",str(j)))

        out_edges = graph.getOutEdges("20")
        self.assertTrue(len(out_edges) == 5)
        out_vertices = [x[0].name for x in out_edges]
        out_weights = [x[1] for x in out_edges]
        for j in range(21, 26):
            self.assertTrue(str(j) in out_vertices)
            index = out_vertices.index(str(j))
            self.assertTrue(out_weights[index] == 1)

        # Check if you add many predecessors for a vertex then getOutEdges
        # will return the correct number of predecessor and has the edge weight
        for i in range(30, 33):
            graph.addVertex(str(i))
            self.assertTrue(graph.checkVertex(str(i)))

        for j in range(31, 33):
            graph.addEdge(str(j), "30")
            self.assertTrue(graph.checkEdge(str(j), "30"))

        in_edges = graph.getInEdges("30")
        self.assertTrue(len(in_edges) == 2)
        in_vertices = [x[0].name for x in in_edges]
        in_weights = [x[1] for x in in_edges]
        for j in range(31, 33):
            self.assertTrue(str(j) in in_vertices)
            index = in_vertices.index(str(j))
            self.assertTrue(in_weights[index] == 1)

        # Check if the roots are correct
        roots = graph.getRoots()
        self.assertTrue(len(roots) == 6)
        for v in roots:
            if v in ["0", "11", "12", "20", "31", "32"]:
                continue

            self.assertTrue(False)

        # Check if the endings (0 out-edges) are corrects
        endings = graph.getRoots(True)
        self.assertTrue(len(endings) == 9)
        for v in endings:
            if v in ["9", "11", "12", "21", "22", "23", "24", "25", "30"]:
                continue

            self.assertTrue(False)

    # Test if the graphs correctly removes cycles
    def test_graph_remove_cycles(self):
        simple_cycle = merge_orderfile.Graph()
        long_cycle = merge_orderfile.Graph()
        many_cycles = merge_orderfile.Graph()

        ############## Example 1 ###############
        # Add vertices to make sure no cycles
        simple_cycle.addVertex("a")
        simple_cycle.addVertex("b")
        simple_cycle.addVertex("c")
        simple_cycle.addVertex("d")

        simple_cycle.addEdge("a","b")
        simple_cycle.addEdge("a","b")
        simple_cycle.addEdge("a","c")
        simple_cycle.addEdge("b","c")
        simple_cycle.addEdge("b","d")
        simple_cycle.addEdge("c","d")
        self.assertTrue(len(simple_cycle.getCycles()) == 0)

        # Added a cycle (b,c)
        simple_cycle.addEdge("c","b")
        self.assertTrue(len(simple_cycle.getCycles()) == 1)

        # Since b has a higher in-edge weights than c, the edge c->b
        # gets removed
        self.assertTrue(simple_cycle.checkEdgeWeight("a","b",2))
        self.assertTrue(simple_cycle.checkEdgeWeight("a","c",1))

        merge_orderfile.removeCycles(simple_cycle)
        self.assertTrue(len(simple_cycle.getCycles()) == 0)
        self.assertFalse(simple_cycle.checkEdge("c","b"))

        ############## Example 2 ###############
        # Add vertices to make sure no cycles
        long_cycle.addVertex("a")
        long_cycle.addVertex("b")
        long_cycle.addVertex("c")
        long_cycle.addVertex("d")
        long_cycle.addVertex("e")
        long_cycle.addVertex("f")
        long_cycle.addVertex("g")

        long_cycle.addEdge("a","b")
        long_cycle.addEdge("a","c")
        long_cycle.addEdge("c","e")
        long_cycle.addEdge("c","f")
        long_cycle.addEdge("b","e")
        long_cycle.addEdge("b","g")
        long_cycle.addEdge("a","d")
        long_cycle.addEdge("d","e")
        long_cycle.addEdge("e","f")
        long_cycle.addEdge("f","g")
        self.assertTrue(len(long_cycle.getCycles()) == 0)

        # Added a cycle (d, e, f, g)
        long_cycle.addEdge("g","d")
        self.assertTrue(len(long_cycle.getCycles()) == 1)

        # Since e has a higher in-edge weights than the other vertices in the cycle,
        # the edge d->e gets removed.
        # d's in-edges excluding cycle edges are (a->d): 1
        # e's in-edges excluding cycle edges are (b->e) and (c->e): 2
        # f's in-edges excluding cycle edges are (c->f): 1
        # g's in-edges excluding cycle edges are (b->g): 1
        merge_orderfile.removeCycles(long_cycle)
        self.assertTrue(len(long_cycle.getCycles()) == 0)
        self.assertFalse(long_cycle.checkEdge("d","e"))

        ############## Example 3 ###############
        # Add vertices to make sure no cycles
        many_cycles.addVertex("a")
        many_cycles.addVertex("b")
        many_cycles.addVertex("c")
        many_cycles.addVertex("d")
        many_cycles.addVertex("e")
        many_cycles.addVertex("f")
        many_cycles.addVertex("g")

        many_cycles.addEdge("a","b")
        many_cycles.addEdge("a","b")
        many_cycles.addEdge("a","b")
        many_cycles.addEdge("a","d")
        many_cycles.addEdge("b","c")
        many_cycles.addEdge("c","d")
        many_cycles.addEdge("d","e")
        many_cycles.addEdge("e","f")
        many_cycles.addEdge("f","g")
        self.assertTrue(len(many_cycles.getCycles()) == 0)

        # Added cycles (b,c,d), (d, e, f), (b,c,d,e,f,g)
        many_cycles.addEdge("f","d")
        many_cycles.addEdge("g","b")
        many_cycles.addEdge("d","b")

        self.assertTrue(len(many_cycles.getCycles()) == 3)

        # Since in_edges(b) > in_edges(d) > the other vertices in the cycle,
        # the edge g->b gets removed in the big cycle, d->b from the first small cycle,
        # and edge f->d from the second small cycle
        # b's in-edges excluding cycle edges is (a->d) with weight 3: 1
        # c's in-edges excluding cycle edges: 0
        # d's in-edges excluding cycle edges is (a->d) with weight 1: 1
        # e's in-edges excluding cycle edges: 0
        # f's in-edges excluding cycle edges: 0
        # g's in-edges excluding cycle edges: 0
        merge_orderfile.removeCycles(many_cycles)
        self.assertTrue(len(many_cycles.getCycles()) == 0)
        self.assertFalse(many_cycles.checkEdge("g","b"))
        self.assertFalse(many_cycles.checkEdge("d","b"))
        self.assertFalse(many_cycles.checkEdge("f","d"))

    # Test if the graphs correctly orders based on our algorithm.
    # Assume we have no cycles because the script will remove cycles
    # before creating the order file.
    def test_graph_order(self):
        linear_graph = merge_orderfile.Graph()
        merge_to_postdominator = merge_orderfile.Graph()
        fernando_example = merge_orderfile.Graph()

        ############## Example 1 ###############
        # You only have a simple order file that have one successor
        # along the way.
        linear_graph.addVertex("a")
        linear_graph.addVertex("b")
        linear_graph.addVertex("c")
        linear_graph.addVertex("d")

        linear_graph.addEdge("a","b")
        linear_graph.addEdge("b","c")
        linear_graph.addEdge("c","d")

        linear_graph.printOrder(self.output_file)
        self.assertTrue(os.path.isfile(self.output_file))

        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.output_file,
                                        "--partial", "a,b,c,d"])
        self.assertTrue(output, "Order file is valid")

        linear_graph.exportGraph("example1.dot")
        self.assertTrue(os.path.isfile("example1.dot"))

        # Clean up at the end
        os.remove(self.output_file)
        os.remove("example1.dot")
        os.remove("example1.dot.pdf")

        ############## Example 2 ###############
        order1 = ["a","b"]
        order2 = ["a","b","e","h"]
        order3 = ["a","b","e","h"]
        order4 = ["a","b","e"]
        order5 = ["a","b","c","d","h"]
        order6 = ["a","b","c"]
        order7 = ["a","b","f","g","h"]

        merge_orderfile.addSymbolsToGraph(merge_to_postdominator, order1)
        merge_orderfile.addSymbolsToGraph(merge_to_postdominator, order2)
        merge_orderfile.addSymbolsToGraph(merge_to_postdominator, order3)
        merge_orderfile.addSymbolsToGraph(merge_to_postdominator, order4)
        merge_orderfile.addSymbolsToGraph(merge_to_postdominator, order5)
        merge_orderfile.addSymbolsToGraph(merge_to_postdominator, order6)
        merge_orderfile.addSymbolsToGraph(merge_to_postdominator, order7)

        merge_to_postdominator.printOrder(self.output_file)
        self.assertTrue(os.path.isfile(self.output_file))

        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.output_file,
                                        "--partial", "a,b,e,h,c,d,f,g"])
        self.assertTrue(output, "Order file is valid")

        merge_to_postdominator.exportGraph("example2.dot")
        self.assertTrue(os.path.isfile("example2.dot"))

        # Clean up at the end
        os.remove(self.output_file)
        os.remove("example2.dot")
        os.remove("example2.dot.pdf")

        ############## Example 3 ###############
        order1 = ["main","a","b","c","d"]
        order2 = ["main","a","b","c","e","f"]
        order3 = ["main","f"]
        order4 = ["main","a","b","c","i"]
        order5 = ["main","g", "i", "c"]
        order6 = ["main","g", "i", "j"]
        order7 = ["main","h", "i"]
        order8 = ["main","a","b","c","e","f"]

        merge_orderfile.addSymbolsToGraph(fernando_example, order1)
        merge_orderfile.addSymbolsToGraph(fernando_example, order2)
        merge_orderfile.addSymbolsToGraph(fernando_example, order3)
        merge_orderfile.addSymbolsToGraph(fernando_example, order4)
        merge_orderfile.addSymbolsToGraph(fernando_example, order5)
        merge_orderfile.addSymbolsToGraph(fernando_example, order6)
        merge_orderfile.addSymbolsToGraph(fernando_example, order7)
        merge_orderfile.addSymbolsToGraph(fernando_example, order8)

        fernando_example.printOrder(self.output_file)
        self.assertTrue(os.path.isfile(self.output_file))

        output = utils.check_output(["python3", self.validate_script,
                                        "--order-file", self.output_file,
                                        "--partial", "main,a,b,c,e,f,d,i,j,g,h"])
        self.assertTrue(output, "Order file is valid")

        fernando_example.exportGraph("example3.dot")
        self.assertTrue(os.path.isfile("example3.dot"))

        # Clean up at the end
        os.remove(self.output_file)
        os.remove("example3.dot")
        os.remove("example3.dot.pdf")

if __name__ == '__main__':
    unittest.main()
