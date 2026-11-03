#!/usr/bin/env python3
"""Contract tests for R79: GitHub involves:Rusino scope, repo-aware projects, and PR stacks."""

import json
import os
import sys
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
sys.path.insert(0, os.path.join(REPO_ROOT, "hooks"))

import cl_watcher as w


class R79GitHubScopeAndStacksTests(unittest.TestCase):

  def test_discover_excludes_stale_external_involves_but_keeps_review_requested(self):
    queries_seen: list[str] = []

    def fake_check_output(cmd, **kwargs):
      q_arg = next((a[2:] for a in cmd if a.startswith("q=")), "")
      queries_seen.append(q_arg)
      if "author:Rusino" in q_arg and "-author:Rusino" not in q_arg:
        return json.dumps({
            "items": [{
                "number": 42,
                "title": "Sync TS paragraph clusters",
                "html_url": "https://github.com/Rusino/paragraph/pull/42",
            }]
        })
      if "review-requested:Rusino" in q_arg:
        return json.dumps({
            "items": [{
                "number": 5001,
                "title": "External HarfBuzz shaping PR requesting Rusino",
                "html_url": "https://github.com/harfbuzz/harfbuzz/pull/5001",
            }]
        })
      if "involves:Rusino" in q_arg:
        return json.dumps({
            "items": [
                {
                    "number": 195366,
                    "title": "[web_paragraph] Render soft hyphens",
                    "html_url": "https://github.com/flutter/flutter/pull/195366",
                },
                {
                    "number": 3106,
                    "title": "Improved text shaping",
                    "html_url": "https://github.com/airbnb/lottie-web/pull/3106",
                    "assignees": [],
                    "requested_reviewers": [],
                },
            ]
        })
      return json.dumps({"items": []})

    proj_names = {
        "p-wp": "WebParagraph",
        "p-sp": "SkParagraph",
        "p-skia": "Skia",
    }
    with (
        mock.patch.object(w, "_gerrit_http_get_json", return_value=[]),
        mock.patch.object(w.subprocess, "check_output", side_effect=fake_check_output),
    ):
      remote = w.discover_open_remote_changes(
          set(), proj_names=proj_names, ignored_ids=set()
      )

    self.assertTrue(any("review-requested:Rusino -author:Rusino" in q for q in queries_seen))
    involves_qs = [q for q in queries_seen if "involves:Rusino" in q]
    self.assertEqual(len(involves_qs), 1)
    self.assertIn("org:flutter", involves_qs[0])
    self.assertIn("user:Rusino", involves_qs[0])

    by_key = {r["item_key"]: r for r in remote}
    self.assertNotIn("github:3106", by_key)
    self.assertIn("github:42", by_key)
    self.assertIn("github:5001", by_key)
    self.assertIn("github:195366", by_key)
    self.assertEqual(by_key["github:5001"]["repo_slug"], "harfbuzz/harfbuzz")
    self.assertEqual(by_key["github:5001"]["project_name"], "Unassigned")
    self.assertEqual(by_key["github:195366"]["project_name"], "WebParagraph")

  def test_fetch_github_pr_status_demotes_stale_external_comment_from_reviewer(self):
    def make_pr_payload(repo_url, req_logins, review_date="2024-09-16T15:41:29Z"):
      return {
          "number": 3106,
          "title": "Improved text shaping",
          "body": "",
          "state": "OPEN",
          "isDraft": False,
          "author": {"login": "b-wils"},
          "mergedAt": None,
          "headRefOid": "deadbeef1234567890",
          "headRefName": "master",
          "baseRefName": "master",
          "reviewDecision": "",
          "latestReviews": [],
          "reviews": [{
              "author": {"login": "Rusino"},
              "state": "COMMENTED",
              "body": "Looks interesting",
              "submittedAt": review_date,
          }],
          "comments": [{
              "author": {"login": "zanderisrael"},
              "body": "Any update?",
              "createdAt": "2025-03-17T08:41:05Z",
          }],
          "commits": [],
          "statusCheckRollup": [],
          "labels": [],
          "url": repo_url,
          "mergeable": "MERGEABLE",
          "reviewRequests": [{"login": u} for u in req_logins],
          "createdAt": "2024-09-01T00:00:00Z",
          "updatedAt": "2025-03-17T08:41:07Z",
          "closedAt": None,
          "files": [{"path": "player/js/elements/TextElement.js"}],
      }

    stale_ext = make_pr_payload("https://github.com/airbnb/lottie-web/pull/3106", [])
    with mock.patch.object(w.subprocess, "check_output", return_value=json.dumps(stale_ext)):
      st_stale = w.fetch_github_pr_status("3106", repo_slug="airbnb/lottie-web")
    self.assertTrue(st_stale["ok"])
    self.assertEqual(st_stale["role"], "watching")
    self.assertFalse(st_stale["in_my_attention"])
    self.assertEqual(st_stale["phase"], "WAITING_ON_OTHERS")
    self.assertTrue(st_stale["is_watching"])

    active_ext = make_pr_payload(
        "https://github.com/airbnb/lottie-web/pull/3106", ["Rusino"]
    )
    with mock.patch.object(w.subprocess, "check_output", return_value=json.dumps(active_ext)):
      st_req = w.fetch_github_pr_status("3106", repo_slug="airbnb/lottie-web")
    self.assertEqual(st_req["role"], "reviewer")
    self.assertTrue(st_req["in_my_attention"])
    self.assertEqual(st_req["phase"], "NEEDS_YOUR_ACTION")

    flutter_pr = make_pr_payload(
        "https://github.com/flutter/flutter/pull/188906", []
    )
    with (
        mock.patch.object(w.subprocess, "check_output", return_value=json.dumps(flutter_pr)),
        mock.patch.object(w, "partition_github_failed_checks", return_value=([], [])),
    ):
      st_fl = w.fetch_github_pr_status("188906", repo_slug="flutter/flutter")
    self.assertEqual(st_fl["role"], "reviewer")
    self.assertTrue(st_fl["in_my_attention"])
    self.assertEqual(st_fl["phase"], "NEEDS_YOUR_ACTION")

  def test_infer_project_for_change_and_bug_are_repo_aware(self):
    proj_names = {
        "p-wp": "WebParagraph",
        "p-sp": "SkParagraph",
        "p-skia": "Skia",
        "p-tc": "TextClusters API",
    }
    self.assertEqual(
        w.infer_project_for_change(
            "github",
            "github.com/airbnb/lottie-web",
            "Improved text shaping",
            proj_names,
            repo_slug="airbnb/lottie-web",
        ),
        ("", "Unassigned"),
    )
    self.assertEqual(
        w.infer_project_for_change(
            "github",
            "github.com/flutter/flutter",
            "[web_paragraph] Render soft hyphens",
            proj_names,
            repo_slug="flutter/flutter",
        ),
        ("p-wp", "WebParagraph"),
    )
    self.assertEqual(
        w.infer_project_for_change(
            "github",
            "github.com/Rusino/paragraph",
            "Sync TS paragraph clusters",
            proj_names,
            repo_slug="Rusino/paragraph",
        ),
        ("p-wp", "WebParagraph"),
    )
    self.assertEqual(
        w.infer_project_for_change(
            "github",
            "github.com/flutter/engine",
            "[skparagraph] Fix strut metrics",
            proj_names,
            repo_slug="flutter/engine",
        ),
        ("p-sp", "SkParagraph"),
    )

    ext_bug = {
        "bug_key": "gh_issue:airbnb/lottie-web#3100",
        "source": "github",
        "url": "https://github.com/airbnb/lottie-web/issues/3100",
        "title": "SVG mask clipping issue",
        "labels": [],
    }
    self.assertEqual(w.infer_project_for_bug(ext_bug, proj_names), ("", "Unassigned"))

    fl_bug = {
        "bug_key": "gh_issue:flutter/flutter#160000",
        "source": "github",
        "url": "https://github.com/flutter/flutter/issues/160000",
        "title": "Text layout overflow in Row",
        "labels": [],
    }
    self.assertEqual(w.infer_project_for_bug(fl_bug, proj_names), ("p-sp", "SkParagraph"))

  def test_stack_graph_isolates_repos_and_ignores_trunk_or_self_head_ref(self):
    items = [
        {
            "item_key": "github:3106",
            "kind": "github",
            "target_id": "3106",
            "repo_slug": "airbnb/lottie-web",
            "head_ref": "master",
            "base_ref": "master",
            "created": "2024-09-01T00:00:00Z",
        },
        {
            "item_key": "github:199999",
            "kind": "github",
            "target_id": "199999",
            "repo_slug": "flutter/flutter",
            "head_ref": "master",
            "base_ref": "master",
            "created": "2026-10-01T00:00:00Z",
        },
        {
            "item_key": "github:195122",
            "kind": "github",
            "target_id": "195122",
            "repo_slug": "flutter/flutter",
            "head_ref": "webparagraph_layout_perf",
            "base_ref": "master",
            "created": "2026-11-01T00:00:00Z",
        },
        {
            "item_key": "github:77",
            "kind": "github",
            "target_id": "77",
            "repo_slug": "other/repo",
            "head_ref": "shared-branch",
            "base_ref": "main",
            "created": "2026-11-01T01:00:00Z",
        },
        {
            "item_key": "github:195500",
            "kind": "github",
            "target_id": "195500",
            "repo_slug": "flutter/flutter",
            "head_ref": "child-branch",
            "base_ref": "shared-branch",
            "created": "2026-11-01T02:00:00Z",
        },
        {
            "item_key": "github:195601",
            "kind": "github",
            "target_id": "195601",
            "repo_slug": "flutter/flutter",
            "head_ref": "wp-part1",
            "base_ref": "master",
            "created": "2026-11-02T01:00:00Z",
        },
        {
            "item_key": "github:195602",
            "kind": "github",
            "target_id": "195602",
            "repo_slug": "flutter/flutter",
            "head_ref": "wp-part2",
            "base_ref": "wp-part1",
            "created": "2026-11-02T02:00:00Z",
        },
        {
            "item_key": "github:195701",
            "kind": "github",
            "target_id": "195701",
            "repo_slug": "flutter/flutter",
            "head_ref": "flutter-3.32-candidate.0",
            "base_ref": "flutter-3.32-candidate.0",
            "created": "2026-11-02T03:00:00Z",
        },
        {
            "item_key": "github:195702",
            "kind": "github",
            "target_id": "195702",
            "repo_slug": "flutter/flutter",
            "head_ref": "cp-wp-fix",
            "base_ref": "flutter-3.32-candidate.0",
            "created": "2026-11-02T04:00:00Z",
        },
    ]
    cache = {d["item_key"]: d for d in items}
    ordered = w.order_and_link_stack_changes(items, cache)
    by_key = {d["item_key"]: d for d in ordered}

    for solo in (
        "github:3106",
        "github:199999",
        "github:195122",
        "github:77",
        "github:195500",
        "github:195701",
        "github:195702",
    ):
      self.assertEqual(by_key[solo]["stack_size"], 1, msg=solo)
      self.assertEqual(by_key[solo]["stack_root_ikey"], "", msg=solo)
      self.assertEqual(by_key[solo]["stack_ancestors"], [], msg=solo)

    self.assertEqual(by_key["github:195601"]["stack_root_ikey"], "github:195601")
    self.assertEqual(by_key["github:195601"]["stack_size"], 2)
    self.assertEqual(by_key["github:195602"]["stack_parent_ikey"], "github:195601")
    self.assertEqual(by_key["github:195602"]["stack_root_ikey"], "github:195601")
    self.assertEqual(by_key["github:195602"]["stack_size"], 2)


if __name__ == "__main__":
  unittest.main()
