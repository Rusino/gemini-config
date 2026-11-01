#!/usr/bin/env python3
"""Tests for decision_guard.py (test-contract change detector)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))

import decision_guard as dg

CPP_BASE = """#include "tests/Test.h"

DEF_TEST(Trak_Replaces, r) {
    float w = width(17);
    REPORTER_ASSERT(r, w == 1.5f);
}

DEF_TEST(Kerning_Pairs, r) {
    REPORTER_ASSERT(r, kern("AV") < 0);
}
"""

CARD = """Решение: D1 trak x letterSpacing
Было → Стало: STACK → REPLACE
Критерий-победитель: Cupertino values give HIG width
Критерий-проигравший: match native Apple
Основание: измерено: ct_trak.c
Не измерено: native with letterSpacing != 0
Что обнажается: fallback fonts keep STACK
Противоречит: none
"""


def _git(root, *args):
  return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True)


class RepoCase(unittest.TestCase):

  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.root = os.path.realpath(self.tmp.name)
    _git(self.root, "init", "-q")
    _git(self.root, "config", "user.email", "t@example.com")
    _git(self.root, "config", "user.name", "t")
    self.write("modules/skparagraph/tests/TrakTest.cpp", CPP_BASE)
    self.write("modules/skparagraph/src/Shaper.cpp", "int x = 1;\n")
    _git(self.root, "add", "-A")
    _git(self.root, "commit", "-qm", "base")
    self.ledger = os.path.join(self.root, ".ledger.jsonl")
    os.environ["DECISION_GUARD_LEDGER"] = self.ledger

  def tearDown(self):
    os.environ.pop("DECISION_GUARD_LEDGER", None)
    self.tmp.cleanup()

  def write(self, rel, text, stage=False):
    path = os.path.join(self.root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
      f.write(text)
    if stage:
      _git(self.root, "add", "--", rel)

  def kinds(self, include_worktree=False):
    return sorted((h["test"], h["kind"]) for h in dg.contract_delta(self.root, include_worktree))


class TestContractDelta(RepoCase):

  def test_new_test_fires(self):
    self.write("modules/skparagraph/tests/TrakTest.cpp",
               CPP_BASE + "\nDEF_TEST(Trak_Fallback, r) {\n    REPORTER_ASSERT(r, ok());\n}\n",
               stage=True)
    self.assertEqual(self.kinds(), [("Trak_Fallback", "new")])

  def test_changed_expected_value_fires(self):
    self.write("modules/skparagraph/tests/TrakTest.cpp",
               CPP_BASE.replace("w == 1.5f", "w == 2.5f"), stage=True)
    self.assertEqual(self.kinds(), [("Trak_Replaces", "asserts")])

  def test_assert_added_inside_existing_test_fires(self):
    self.write("modules/skparagraph/tests/TrakTest.cpp",
               CPP_BASE.replace("w == 1.5f);",
                                "w == 1.5f);\n    REPORTER_ASSERT(r, cupertino() < w);"),
               stage=True)
    self.assertEqual(self.kinds(), [("Trak_Replaces", "asserts")])

  def test_reflow_only_is_silent(self):
    self.write("modules/skparagraph/tests/TrakTest.cpp",
               CPP_BASE.replace("REPORTER_ASSERT(r, w == 1.5f);",
                                "REPORTER_ASSERT(r,\n                    w == 1.5f);"),
               stage=True)
    self.assertEqual(self.kinds(), [])

  def test_rename_with_same_body_is_rename(self):
    self.write("modules/skparagraph/tests/TrakTest.cpp",
               CPP_BASE.replace("Trak_Replaces", "Trak_Stacks"), stage=True)
    self.assertEqual(self.kinds(), [("Trak_Stacks", "renamed")])

  def test_removed_test_fires(self):
    body = CPP_BASE.split("DEF_TEST(Kerning_Pairs")[0]
    self.write("modules/skparagraph/tests/TrakTest.cpp", body, stage=True)
    self.assertEqual(self.kinds(), [("Kerning_Pairs", "removed")])

  def test_value_outside_assert_fires(self):
    self.write("modules/skparagraph/tests/TrakTest.cpp",
               CPP_BASE.replace("width(17)", "width(36)"), stage=True)
    self.assertEqual(self.kinds(), [("Trak_Replaces", "values")])

  def test_identical_move_between_files_is_silent(self):
    head, kern = CPP_BASE.split("DEF_TEST(Kerning_Pairs")
    self.write("modules/skparagraph/tests/TrakTest.cpp", head, stage=True)
    self.write("modules/skparagraph/tests/KernTest.cpp",
               '#include "tests/Test.h"\n\nDEF_TEST(Kerning_Pairs' + kern, stage=True)
    self.assertEqual(self.kinds(), [])

  def test_non_test_source_is_silent(self):
    self.write("modules/skparagraph/src/Shaper.cpp", "int x = 2;\n", stage=True)
    self.assertEqual(self.kinds(), [])

  def test_unstaged_change_counts_only_with_worktree(self):
    self.write("modules/skparagraph/tests/TrakTest.cpp",
               CPP_BASE.replace("w == 1.5f", "w == 2.5f"))
    self.assertEqual(self.kinds(), [])
    self.assertEqual(self.kinds(include_worktree=True), [("Trak_Replaces", "asserts")])

  def test_rename_with_changed_assert_is_not_a_rename(self):
    self.write("modules/skparagraph/tests/TrakTest.cpp",
               CPP_BASE.replace("Trak_Replaces", "Trak_Stacks").replace("w == 1.5f", "w == 2.5f"),
               stage=True)
    self.assertEqual(self.kinds(), [("Trak_Replaces", "removed"), ("Trak_Stacks", "new")])

  def test_parser_edge_cases(self):
    py = ("class A:\n  def testRun(self):\n    assert x == 1\n"
          "class B:\n  def testRun(self):\n    assert x == 2\n")
    self.assertEqual(sorted(dg.parse_tests("t/test_a.py", py)), ["testRun", "testRun#2"])
    dart = "void main() {\n  test(\n    'wraps',\n    () {\n      expect(w, 10);\n    });\n}\n"
    self.assertEqual(list(dg.parse_tests("t/a_test.dart", dart)), ["wraps"])
    body = dg.parse_tests("tests/A.cpp",
                          'DEF_TEST(U, r) {\n  auto u = "https://x/7";\n'
                          '  REPORTER_ASSERT(r, s == ")" && n == 3);\n}\n')["U"]
    asserts, rest = dg.split_asserts(body)
    self.assertEqual(list(asserts), ['REPORTER_ASSERT(r,s==")"&&n==3)'])
    self.assertIn("https://x/7", rest)

  def test_helper_after_test_is_not_part_of_it(self):
    self.write("modules/skparagraph/tests/TrakTest.cpp",
               CPP_BASE + "\nstatic int helper() {\n    SkASSERT(true);\n    return 1;\n}\n",
               stage=True)
    self.assertEqual(self.kinds(), [])

  def test_dart_and_python_tests_are_parsed(self):
    self.write("lib/web_ui/test/text_test.dart",
               "void main() {\n  test('wraps', () {\n    expect(w, 10);\n  });\n}\n")
    self.write("tools/test_shaper.py", "def test_wrap():\n  assert w == 10\n")
    _git(self.root, "add", "-A")
    _git(self.root, "commit", "-qm", "more")
    self.write("lib/web_ui/test/text_test.dart",
               "void main() {\n  test('wraps', () {\n    expect(w, 12);\n  });\n}\n", stage=True)
    self.write("tools/test_shaper.py", "def test_wrap():\n  assert w == 12\n", stage=True)
    self.assertEqual(self.kinds(), [("test_wrap", "asserts"), ("wraps", "asserts")])


class TestGate(RepoCase):

  def change(self):
    self.write("modules/skparagraph/tests/TrakTest.cpp",
               CPP_BASE.replace("w == 1.5f", "w == 2.5f"), stage=True)
    return dg.contract_delta(self.root, False)

  def transcript(self, steps):
    path = os.path.join(self.root, "transcript.jsonl")
    with open(path, "w", encoding="utf-8") as f:
      for s in steps:
        f.write(json.dumps(s) + "\n")
    return path

  def test_unacked_change_is_denied_with_hits_listed(self):
    hits = self.change()
    reason = dg.commit_denial(self.root, hits, "conv", "conv", "")
    self.assertIn("[DECISION GUARD]", reason)
    self.assertIn("Trak_Replaces", reason)
    self.assertIn(dg.hits_hash(hits), reason)

  def test_non_policy_ack_allows_commit(self):
    hits = self.change()
    dg.record_ack(self.root, dg.hits_hash(hits), "conv", "conv",
                  {"Trak_Replaces": ("bugfix", "wrong expected width")}, None, hits)
    self.assertEqual(dg.commit_denial(self.root, hits, "conv", "conv", ""), "")

  def test_ack_must_cover_every_hit(self):
    hits = self.change()
    with self.assertRaises(ValueError):
      dg.record_ack(self.root, dg.hits_hash(hits), "conv", "conv", {}, None, hits)

  def test_policy_ack_requires_complete_card(self):
    hits = self.change()
    with self.assertRaises(ValueError):
      dg.record_ack(self.root, dg.hits_hash(hits), "conv", "conv",
                    {"Trak_Replaces": ("policy", "flip")}, "Решение: x\n", hits)

  def test_policy_ack_waits_for_user_reply(self):
    hits = self.change()
    dg.record_ack(self.root, dg.hits_hash(hits), "conv", "conv",
                  {"Trak_Replaces": ("policy", "flip")}, CARD, hits)
    ack_ts = dg.find_ack(self.root, dg.hits_hash(hits))["ts"]
    before = self.transcript([{"step_index": 1, "type": "USER_INPUT",
                               "created_at": "2000-01-01T00:00:00Z"}])
    self.assertIn("waits for the user", dg.commit_denial(self.root, hits, "conv", "conv", before))
    after = self.transcript([{"step_index": 9, "type": "USER_INPUT",
                              "created_at": "2999-01-01T00:00:00Z"}])
    self.assertGreater("2999", ack_ts[:4])
    self.assertEqual(dg.commit_denial(self.root, hits, "conv", "conv", after), "")

  def test_turn_visibility_requires_test_names_in_reply(self):
    hits = self.change()
    dg.record_ack(self.root, dg.hits_hash(hits), "sub", "root",
                  {"Trak_Replaces": ("bugfix", "wrong width")}, None, hits)
    reason = dg.visibility_reason("root", "2000-01-01T00:00:00Z", "Done, all green.")
    self.assertIn("Trak_Replaces", reason)
    self.assertEqual(
        dg.visibility_reason("root", "2000-01-01T00:00:00Z",
                             "Contract: Trak_Replaces asserts, bugfix: wrong width"), "")
    self.assertEqual(dg.visibility_reason("root", "2999-01-01T00:00:00Z", "x"), "")

  def test_policy_visibility_requires_card_in_reply(self):
    hits = self.change()
    dg.record_ack(self.root, dg.hits_hash(hits), "conv", "conv",
                  {"Trak_Replaces": ("policy", "flip")}, CARD, hits)
    reason = dg.visibility_reason("conv", "2000-01-01T00:00:00Z", "Trak_Replaces changed")
    self.assertIn("card", reason)
    self.assertEqual(dg.visibility_reason("conv", "2000-01-01T00:00:00Z",
                                          "Trak_Replaces\n" + CARD), "")


class TestHookWiring(RepoCase):

  def test_pre_tool_guard_blocks_commit_and_skips_inspection(self):
    import pre_tool_guard
    self.write("modules/skparagraph/tests/TrakTest.cpp",
               CPP_BASE.replace("w == 1.5f", "w == 2.5f"), stage=True)
    deny = pre_tool_guard.decision_contract_denial(
        f"git -C {self.root} commit -m x", [self.root], "conv", "", "")
    self.assertIn("[DECISION GUARD]", deny)
    self.assertEqual(pre_tool_guard.decision_contract_denial(
        f"git -C {self.root} log --grep commit", [self.root], "conv", "", ""), "")

  def test_pre_tool_guard_all_flag_sees_unstaged_change(self):
    import pre_tool_guard
    self.write("modules/skparagraph/tests/TrakTest.cpp",
               CPP_BASE.replace("w == 1.5f", "w == 2.5f"))
    self.assertEqual(pre_tool_guard.decision_contract_denial(
        f"git -C {self.root} commit -m x", [self.root], "conv", "", ""), "")
    self.assertIn("[DECISION GUARD]", pre_tool_guard.decision_contract_denial(
        f"git -C {self.root} commit -am x", [self.root], "conv", "", ""))

  def test_pathspec_commit_sees_worktree_and_hint_carries_flag(self):
    import pre_tool_guard
    self.write("modules/skparagraph/tests/TrakTest.cpp",
               CPP_BASE.replace("w == 1.5f", "w == 2.5f"))
    deny = pre_tool_guard.decision_contract_denial(
        f"git -C {self.root} commit -qm 'x y' modules/skparagraph/tests/TrakTest.cpp",
        [self.root], "conv", "", "")
    self.assertIn("--worktree", deny)
    self.assertEqual(pre_tool_guard._commit_scope("git commit -qm 'x y'"), (False, None))
    self.assertEqual(pre_tool_guard._commit_scope("git commit --amend --no-edit"), (False, None))
    self.assertEqual(pre_tool_guard._commit_scope("git commit -am x"), (True, None))
    self.assertEqual(pre_tool_guard._commit_scope("git commit -m x -- a.cpp"), (True, ["a.cpp"]))

  def test_pathspec_commit_ignores_dirty_tests_outside_pathspec(self):
    import pre_tool_guard
    self.write("modules/skparagraph/tests/TrakTest.cpp",
               CPP_BASE.replace("w == 1.5f", "w == 2.5f"))
    self.write("modules/skparagraph/src/Shaper.cpp", "int x = 2;\n")
    self.assertEqual(pre_tool_guard.decision_contract_denial(
        f"git -C {self.root} commit -m x -- modules/skparagraph/src/Shaper.cpp",
        [self.root], "conv", "", ""), "")

  def test_help_commit_graph_and_trailing_cd_are_handled(self):
    import pre_tool_guard
    self.write("modules/skparagraph/tests/TrakTest.cpp",
               CPP_BASE.replace("w == 1.5f", "w == 2.5f"), stage=True)
    for cmd in (f"git -C {self.root} commit --help", f"git -C {self.root} commit-graph write"):
      self.assertEqual(pre_tool_guard.decision_contract_denial(cmd, [self.root], "c", "", ""), "")
    self.assertIn("[DECISION GUARD]", pre_tool_guard.decision_contract_denial(
        f"git -C {self.root} commit -m x && cd /tmp", ["/tmp"], "c", "", ""))

  def test_stop_guard_reports_last_reply_and_user_time(self):
    import stop_guard
    path = os.path.join(self.root, "t.jsonl")
    with open(path, "w", encoding="utf-8") as f:
      for s in ({"step_index": 1, "type": "USER_INPUT", "created_at": "2026-11-01T19:00:00Z",
                 "content": "go"},
                {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Trak_Replaces: bugfix"}):
        f.write(json.dumps(s) + "\n")
    info = stop_guard.analyze_current_turn(path)
    self.assertEqual(info["last_user_ts"], "2026-11-01T19:00:00Z")
    self.assertEqual(info["last_planner_text"], "Trak_Replaces: bugfix")


if __name__ == "__main__":
  unittest.main()
