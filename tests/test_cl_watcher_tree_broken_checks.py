#!/usr/bin/env python3
"""Contract tests for subtracting tree/base-broken checks on Flutter GitHub PRs."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


class _FakeHttpResp:

  def __init__(self, data: dict):
    self._raw = json.dumps(data).encode("utf-8")

  def read(self) -> bytes:
    return self._raw

  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc, tb):
    return False


class GitHubTreeBrokenChecksTest(unittest.TestCase):

  def setUp(self):
    super().setUp()
    if hasattr(w, "_FLUTTER_TREE_CACHE"):
      w._FLUTTER_TREE_CACHE.update({"tasks": set(), "expires_at": 0.0})
    if hasattr(w, "_FLUTTER_BASE_CACHE"):
      w._FLUTTER_BASE_CACHE.clear()

  def test_github_pr_ignores_checks_broken_on_tree_or_base_commit(self):
    pr_payload = {
        "number": 193774,
        "title": (
            "[web_paragraph] Apply StrutStyle to line metrics and placeholders"
        ),
        "state": "OPEN",
        "isDraft": True,
        "author": {"login": "Rusino"},
        "mergedAt": None,
        "headRefOid": "97329b1881f827e30eeb8eeea04aa62134d5b4e1",
        "reviews": [],
        "comments": [],
        "commits": [],
        "statusCheckRollup": [
            {
                "name": "Tree_analyze",
                "conclusion": "FAILURE",
                "status": "COMPLETED",
            },
            {"context": "tree-status", "state": "FAILURE"},
            {"name": "Dashboard Checks", "conclusion": "", "status": "QUEUED"},
        ],
        "url": "https://github.com/flutter/flutter/pull/193774",
        "mergeable": "MERGEABLE",
        "reviewRequests": [],
    }
    master_runs = {
        "check_runs": [
            {"id": 10, "name": "Tree_analyze", "conclusion": "failure"},
        ]
    }
    build_status = {
        "buildStatus": "failure",
        "failingTasks": ["Linux analyze", "Windows tool_tests_general"],
    }

    def fake_check_output(cmd, **kwargs):
      if cmd[:3] == ["gh", "pr", "view"]:
        return json.dumps(pr_payload)
      if "commits/master/check-runs" in cmd[-1]:
        return json.dumps(master_runs)
      raise AssertionError(f"Unexpected command: {cmd}")

    with mock.patch.object(
        w.subprocess, "check_output", side_effect=fake_check_output
    ), mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeHttpResp(build_status)
    ):
      res = w.fetch_github_pr_status("193774")

    self.assertTrue(res["ok"])
    self.assertTrue(res["is_draft"])
    self.assertFalse(res["cq_failed"])
    self.assertEqual(res["own_failed_checks"], [])
    self.assertEqual(res["tree_failed_checks"], ["Tree_analyze"])
    self.assertEqual(res["cq_status"], "🌲 Tree broken (Tree_analyze)")
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")
    self.assertFalse(res["in_my_attention"])

  def test_github_pr_expands_dashboard_checks_against_tree_and_base_failures(
      self,
  ):
    head_sha = "9dfa5370104254826bddb53753d7aae9c5f5d979"
    base_sha = "e6221ce7f105207ed1eb15eca3d111237067b6d0"
    pr_payload = {
        "number": 195367,
        "title": "[web_paragraph] Refactor placeholder metrics",
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": "Rusino"},
        "mergedAt": None,
        "headRefOid": head_sha,
        "reviews": [],
        "comments": [],
        "commits": [],
        "statusCheckRollup": [
            {
                "name": "Dashboard Checks",
                "conclusion": "ACTION_REQUIRED",
                "status": "COMPLETED",
            },
            {
                "name": "Tree_analyze",
                "conclusion": "FAILURE",
                "status": "COMPLETED",
            },
        ],
        "url": "https://github.com/flutter/flutter/pull/195367",
        "mergeable": "MERGEABLE",
        "reviewRequests": [],
    }
    # Master already recovered for Tree_analyze, but base_sha still has it failing
    master_runs = {
        "check_runs": [
            {"id": 20, "name": "Tree_analyze", "conclusion": "success"}
        ]
    }
    base_runs = {
        "check_runs": [
            {"id": 11, "name": "Tree_analyze", "conclusion": "failure"}
        ]
    }
    head_dashboard_runs = {
        "check_runs": [{
            "id": 99,
            "name": "Dashboard Checks",
            "conclusion": "action_required",
            "output": {
                "text": (
                    "Failed presubmit jobs:\n"
                    "- `Linux analyze`\n"
                    "- `Windows tool_tests_general`"
                )
            },
        }]
    }
    build_status = {
        "buildStatus": "failure",
        "failingTasks": ["Linux analyze", "Windows tool_tests_general"],
    }

    def fake_check_output(cmd, **kwargs):
      if cmd[:3] == ["gh", "pr", "view"]:
        return json.dumps(pr_payload)
      target = cmd[-1]
      if "commits/master/check-runs" in target:
        return json.dumps(master_runs)
      if f"commits/{head_sha}/check-runs" in target:
        return json.dumps(head_dashboard_runs)
      if target == "repos/flutter/flutter/pulls/195367":
        return json.dumps({"base": {"sha": base_sha}})
      if f"commits/{base_sha}/check-runs" in target:
        return json.dumps(base_runs)
      raise AssertionError(f"Unexpected command: {cmd}")

    with mock.patch.object(
        w.subprocess, "check_output", side_effect=fake_check_output
    ), mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeHttpResp(build_status)
    ):
      res = w.fetch_github_pr_status("195367")

    self.assertTrue(res["ok"])
    self.assertFalse(res["cq_failed"])
    self.assertEqual(res["own_failed_checks"], [])
    self.assertEqual(
        res["tree_failed_checks"],
        ["Linux analyze", "Windows tool_tests_general", "Tree_analyze"],
    )
    self.assertEqual(res["cq_status"], "🌲 Tree broken (3 checks)")
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")

    # Add a PR-specific failure inside Dashboard Checks and verify it raises CQ alert
    head_dashboard_runs["check_runs"][0]["output"]["text"] += (
        "\n- `Linux web_engine_framework_tests`"
    )
    with mock.patch.object(
        w.subprocess, "check_output", side_effect=fake_check_output
    ), mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeHttpResp(build_status)
    ):
      res_own = w.fetch_github_pr_status("195367")

    self.assertTrue(res_own["cq_failed"])
    self.assertEqual(
        res_own["own_failed_checks"], ["Linux web_engine_framework_tests"]
    )
    self.assertEqual(res_own["cq_status"], "🔴 1 check(s) failed")
    self.assertEqual(res_own["phase"], "ALERT_CQ_FAILED")
    self.assertTrue(res_own["in_my_attention"])


if __name__ == "__main__":
  unittest.main()
