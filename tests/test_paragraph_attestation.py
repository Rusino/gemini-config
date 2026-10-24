#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

SKIA_SKILL_DIR = pathlib.Path(__file__).resolve().parent.parent / "skills/skia-test-paragraph/scripts"
sys.path.insert(0, str(SKIA_SKILL_DIR))
import parse_dm_logs

FLUTTER_SKILL_DIR = pathlib.Path(__file__).resolve().parent.parent / "skills/flutter-test-paragraph-engine/scripts"
sys.path.insert(0, str(FLUTTER_SKILL_DIR))
import parse_flutter_paragraph_tests


class TestSkiaDmParserAndAttestation(unittest.TestCase):
    def test_parse_clean_dm_log(self) -> None:
        log_content = """Skipping config nonrendering: Don't understand 'nonrendering'.
0 srcs * 2 sinks + 3 tests == 3 tasks

[1/3] 60MB RAM: unit test SkParagraph_Test1 done
[2/3] 61MB RAM: unit test SkParagraph_Test2 done
[3/3] 62MB RAM: unit test SkParagraph_Test3 done
Finished!
"""
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write(log_content)
            f.flush()
            temp_path = f.name

        summary = parse_dm_logs.summarize_dm_log(temp_path)
        self.assertTrue(summary["finished_cleanly"])
        self.assertEqual(summary["expected_tasks"], 3)
        self.assertEqual(summary["completed_tasks"], 3)
        self.assertEqual(summary["passed"], 3)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(len(summary["failures"]), 0)

    def test_parse_failing_dm_log(self) -> None:
        log_content = """0 srcs * 2 sinks + 2 tests == 2 tasks
FAILURE: tests/FooTest.cpp:10	Expected bar [SkParagraph_Bar]: false

[1/2] 60MB RAM: unit test SkParagraph_Bar done
[2/2] 60MB RAM: unit test SkParagraph_Baz done
Failures:
	tests/FooTest.cpp:10	Expected bar [SkParagraph_Bar]: false
1 failure
"""
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write(log_content)
            f.flush()
            temp_path = f.name

        summary = parse_dm_logs.summarize_dm_log(temp_path)
        self.assertTrue(summary["finished_cleanly"])
        self.assertEqual(summary["completed_tasks"], 2)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertIn("SkParagraph_Bar", summary["failures"])

    def test_parse_aborted_dm_log(self) -> None:
        log_content = """0 srcs * 2 sinks + 5 tests == 5 tasks
[1/5] 60MB RAM: unit test SkParagraph_One done
[2/5] 60MB RAM: unit test SkParagraph_Two done
SkASSERT: ../../src/core/SkBlah.cpp:42: condition failed
"""
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write(log_content)
            f.flush()
            temp_path = f.name

        summary = parse_dm_logs.summarize_dm_log(temp_path)
        self.assertFalse(summary["finished_cleanly"])
        self.assertTrue(summary["abort_detected"])
        self.assertIn("SkASSERT", summary["crash_reason"])

    def test_integrity_check_valid_case(self) -> None:
        runs = [
            (
                "baseline",
                {
                    "attestation": {
                        "ref": "origin/main",
                        "git_sha": "abc123456789",
                        "source_tree_sha256": "tree11111111",
                        "binary_sha256": "bin111111111",
                        "ninja_rc": 0,
                        "ninja_actions": "10/10",
                        "test_cmd": ["dm", "--src", "tests", "--match", "SkParagraph"],
                        "test_rc": 0,
                    },
                    "dm": {
                        "completed_tasks": 150,
                        "expected_tasks": 150,
                        "passed": 150,
                        "failed": 0,
                        "finished_cleanly": True,
                        "failures": [],
                    },
                },
            ),
            (
                "candidate",
                {
                    "attestation": {
                        "ref": "my_fix",
                        "git_sha": "def987654321",
                        "source_tree_sha256": "tree22222222",
                        "binary_sha256": "bin222222222",
                        "ninja_rc": 0,
                        "ninja_actions": "2/2",
                        "test_cmd": ["dm", "--src", "tests", "--match", "SkParagraph"],
                        "test_rc": 0,
                    },
                    "dm": {
                        "completed_tasks": 150,
                        "expected_tasks": 150,
                        "passed": 150,
                        "failed": 0,
                        "finished_cleanly": True,
                        "failures": [],
                    },
                },
            ),
        ]
        issues, rows = parse_dm_logs._evaluate_attestation(runs)
        self.assertEqual(issues, [])
        self.assertEqual(len(rows), 2)

    def test_integrity_check_detects_swapped_source_same_binary(self) -> None:
        runs = [
            (
                "baseline",
                {
                    "attestation": {
                        "ref": "origin/main",
                        "git_sha": "abc123456789",
                        "source_tree_sha256": "tree11111111",
                        "binary_sha256": "same_binary_hash",
                        "ninja_rc": 0,
                        "ninja_actions": "10/10",
                        "test_cmd": ["dm", "--src", "tests", "--match", "SkParagraph"],
                        "test_rc": 0,
                    },
                    "dm": {
                        "completed_tasks": 10,
                        "expected_tasks": 10,
                        "passed": 10,
                        "failed": 0,
                        "finished_cleanly": True,
                    },
                },
            ),
            (
                "candidate",
                {
                    "attestation": {
                        "ref": "my_fix",
                        "git_sha": "def987654321",
                        "source_tree_sha256": "tree22222222",
                        "binary_sha256": "same_binary_hash",
                        "ninja_rc": 0,
                        "ninja_actions": "0/0 (no-op)",
                        "test_cmd": ["dm", "--src", "tests", "--match", "SkParagraph"],
                        "test_rc": 0,
                    },
                    "dm": {
                        "completed_tasks": 10,
                        "expected_tasks": 10,
                        "passed": 10,
                        "failed": 0,
                        "finished_cleanly": True,
                    },
                },
            ),
        ]
        issues, rows = parse_dm_logs._evaluate_attestation(runs)
        self.assertTrue(any("identical test binary hash" in iss for iss in issues))

    def test_integrity_check_detects_filter_mismatch_and_task_drop(self) -> None:
        runs = [
            (
                "baseline",
                {
                    "attestation": {
                        "ref": "origin/main",
                        "git_sha": "abc123456789",
                        "source_tree_sha256": "tree11111111",
                        "binary_sha256": "bin111111111",
                        "ninja_rc": 0,
                        "test_cmd": ["dm", "--src", "tests", "--match", "SkParagraph"],
                        "test_rc": 0,
                    },
                    "dm": {
                        "completed_tasks": 150,
                        "expected_tasks": 150,
                        "passed": 150,
                        "failed": 0,
                        "finished_cleanly": True,
                    },
                },
            ),
            (
                "candidate",
                {
                    "attestation": {
                        "ref": "my_fix",
                        "git_sha": "def987654321",
                        "source_tree_sha256": "tree22222222",
                        "binary_sha256": "bin222222222",
                        "ninja_rc": 0,
                        "test_cmd": ["dm", "--src", "tests", "--match", "SkParagraph_OnlyOne"],
                        "test_rc": 0,
                    },
                    "dm": {
                        "completed_tasks": 1,
                        "expected_tasks": 1,
                        "passed": 1,
                        "failed": 0,
                        "finished_cleanly": True,
                    },
                },
            ),
        ]
        issues, rows = parse_dm_logs._evaluate_attestation(runs)
        self.assertTrue(any("different `--match` filter" in iss for iss in issues))
        self.assertTrue(any("executed fewer tasks" in iss for iss in issues))


class TestFlutterParagraphAttestation(unittest.TestCase):
    def test_parse_gtest_and_dart(self) -> None:
        gtest_content = """[==========] Running 2 tests from 1 test suite.
[----------] Global test environment set-up.
[----------] 2 tests from TxtTest
[ RUN      ] TxtTest.Paragraph
[       OK ] TxtTest.Paragraph (5 ms)
[ RUN      ] TxtTest.FontFallback
[       OK ] TxtTest.FontFallback (4 ms)
[----------] 2 tests from TxtTest (9 ms total)
[----------] Global test environment tear-down
[==========] 2 tests from 1 test suite ran. (9 ms total)
[  PASSED  ] 2 tests.
"""
        dart_content = """00:05 +10 ~0 -0: test/rendering/paragraph_test.dart: simple layout
00:10 +11 ~0 -0: All tests passed!
"""
        with tempfile.NamedTemporaryFile("w+", delete=False) as fg:
            fg.write(gtest_content)
            fg.flush()
            g_path = fg.name

        with tempfile.NamedTemporaryFile("w+", delete=False) as fd:
            fd.write(dart_content)
            fd.flush()
            d_path = fd.name

        g_res = parse_flutter_paragraph_tests.summarize_gtest_log(g_path)
        self.assertTrue(g_res["finished_cleanly"])
        self.assertEqual(g_res["passed"], 2)
        self.assertEqual(g_res["failed"], 0)

        d_res = parse_flutter_paragraph_tests.summarize_dart_log(d_path)
        self.assertTrue(d_res["finished_cleanly"])
        self.assertEqual(d_res["passed"], 11)
        self.assertEqual(d_res["failed"], 0)


if __name__ == "__main__":
    unittest.main()
