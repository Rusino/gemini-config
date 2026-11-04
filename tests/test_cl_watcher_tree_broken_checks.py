#!/usr/bin/env python3
"""Contract tests for subtracting tree/base-broken checks on Flutter GitHub PRs."""

import json
import os
import sys
import tempfile
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

  def test_github_pr_google_testing_conflict_and_unrelated_customer_testing(self):
    # Reproduce PR #195122: Google testing has g3 merge conflict and customer_testing fails on flame
    head_sha = "19f70f0255d3dc693221c5db2ba36835699fd92a"
    base_sha = "0000base"
    pr_payload = {
        "number": 195122,
        "title": "[web_paragraph] Avoid browser text metrics queries on re-layout",
        "body": "Speeds up WebParagraph layout.",
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": "Rusino"},
        "mergedAt": None,
        "headRefOid": head_sha,
        "headRefName": "relayout-perf",
        "baseRefName": "master",
        "reviewDecision": "APPROVED",
        "latestReviews": [],
        "reviews": [],
        "comments": [],
        "commits": [{"oid": head_sha}],
        "statusCheckRollup": [
            {
                "name": "Google testing",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "detailsUrl": "http://frob/flutter/presubmit?pr=195122",
            },
            {
                "name": "Linux customer_testing",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "detailsUrl": "https://cr-buildbucket.appspot.com/build/8682147800419572945",
            },
        ],
        "labels": [],
        "url": "https://github.com/flutter/flutter/pull/195122",
        "mergeable": "MERGEABLE",
        "reviewRequests": [],
        "createdAt": "2026-11-01T10:00:00Z",
        "updatedAt": "2026-11-04T20:00:00Z",
        "closedAt": None,
        "files": [
            {"path": "engine/src/flutter/lib/web_ui/lib/src/engine/text_fragmenter.dart"},
            {"path": "engine/src/flutter/lib/web_ui/test/ui/text_golden_test.dart"},
        ],
    }
    head_check_runs = {
        "check_runs": [
            {
                "name": "Google testing",
                "conclusion": "failure",
                "details_url": "http://frob/flutter/presubmit?pr=195122",
                "output": {
                    "title": "Merge conflicts detected",
                    "summary": (
                        "This PR cannot be merged cleanly into Google's internal "
                        "repository (`g3`) at HEAD. Please rebase onto `master`."
                    ),
                },
            }
        ]
    }
    bb_search_resp = {
        "builds": [
            {
                "id": "8682147800419572945",
                "builder": {"builder": "Linux customer_testing"},
                "status": "FAILURE",
                "summaryMarkdown": (
                    "Failing tests:\n"
                    "- `/b/s/w/ir/x/w/rc/customer_testing/tests/flame/packages/flame/"
                    "test/components/text_element_component_test.dart`"
                ),
            }
        ]
    }

    def fake_check_output(cmd, **kwargs):
      if cmd[:3] == ["gh", "pr", "view"]:
        return json.dumps(pr_payload)
      target = cmd[2] if len(cmd) > 2 else ""
      if f"commits/{head_sha}/check-runs" in target:
        return json.dumps(head_check_runs)
      if target == "repos/flutter/flutter/pulls/195122":
        return json.dumps({"base": {"sha": base_sha}})
      if f"commits/{base_sha}/check-runs" in target:
        return json.dumps({"check_runs": []})
      raise AssertionError(f"Unexpected command: {cmd}")

    def fake_urlopen(req, timeout=6):
      url = req.full_url if hasattr(req, "full_url") else str(req)
      if "SearchBuilds" in url:
        return _FakeHttpResp(bb_search_resp)
      return _FakeHttpResp({"builds": []})

    w._GH_CHECK_DETAILS_CACHE.clear()
    with mock.patch.object(
        w.subprocess, "check_output", side_effect=fake_check_output
    ), mock.patch.object(w.urllib.request, "urlopen", side_effect=fake_urlopen):
      res = w.fetch_github_pr_status("195122")

    self.assertTrue(res["ok"])
    self.assertTrue(res["cq_failed"])
    self.assertTrue(res["cq_sync_rebase_needed"])
    self.assertEqual(res["cq_status"], "🔴 Rebase needed (Google testing conflict)")
    self.assertEqual(len(res["failed_check_details"]), 2)
    by_name = {d["name"]: d for d in res["failed_check_details"]}
    self.assertEqual(by_name["Google testing"]["kind"], "rebase_conflict")
    self.assertIn("Merge conflicts", by_name["Google testing"]["snippet"])
    self.assertTrue(by_name["Linux customer_testing"]["unrelated"])
    self.assertIn(
        "flame: text_element_component_test.dart",
        by_name["Linux customer_testing"]["snippet"],
    )
    self.assertIn("Rebase PR onto master", res["cq_advice"])

  def test_script_changed_on_disk_validates_mtime_and_syntax(self):
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False
    ) as tf:
      tf.write("x = 1\n")
      path = tf.name
    try:
      base_mtime = os.path.getmtime(path)
      self.assertFalse(w.script_changed_on_disk(path, base_mtime))
      # Newer mtime with valid Python syntax triggers reload
      os.utime(path, (base_mtime + 10.0, base_mtime + 10.0))
      self.assertTrue(w.script_changed_on_disk(path, base_mtime))
      # Newer mtime with broken Python syntax does not trigger reload
      with open(path, "w", encoding="utf-8") as f:
        f.write("def broken(:\n")
      os.utime(path, (base_mtime + 20.0, base_mtime + 20.0))
      self.assertFalse(w.script_changed_on_disk(path, base_mtime))
    finally:
      os.unlink(path)

  def test_reexec_self_closes_server_before_execv(self):
    fake_server = mock.MagicMock()
    with mock.patch.object(w.os, "execv") as mock_execv:
      w.reexec_self(fake_server)
    fake_server.server_close.assert_called_once()
    mock_execv.assert_called_once_with(
        w.sys.executable, [w.sys.executable, *w.sys.argv]
    )


if __name__ == "__main__":
  unittest.main()
