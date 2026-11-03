#!/usr/bin/env python3
"""Contract tests for R71 Flight Deck GitHub PR fetch, triage ingest, and wakeup throttling."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if SCRIPTS_DIR not in sys.path:
  sys.path.insert(0, SCRIPTS_DIR)

import cl_watcher as w  # pylint: disable=g-import-not-at-top
import triage_store as ts  # pylint: disable=g-import-not-at-top

# Exact set of fields accepted by gh 2.46.0 `gh pr view --json`
GH_2_46_SUPPORTED_PR_JSON_FIELDS = {
    "additions",
    "assignees",
    "author",
    "autoMergeRequest",
    "baseRefName",
    "body",
    "changedFiles",
    "closed",
    "closedAt",
    "comments",
    "commits",
    "createdAt",
    "deletions",
    "files",
    "headRefName",
    "headRefOid",
    "headRepository",
    "headRepositoryOwner",
    "id",
    "isCrossRepository",
    "isDraft",
    "labels",
    "latestReviews",
    "maintainerCanModify",
    "mergeCommit",
    "mergeStateStatus",
    "mergeable",
    "mergedAt",
    "mergedBy",
    "milestone",
    "number",
    "potentialMergeCommit",
    "projectCards",
    "projectItems",
    "reactionGroups",
    "reviewDecision",
    "reviewRequests",
    "reviews",
    "state",
    "statusCheckRollup",
    "title",
    "updatedAt",
    "url",
}


class R71GitHubPrFetchTest(unittest.TestCase):

  def test_fetch_github_pr_status_omits_unsupported_closing_issues_field(self):
    captured_cmds: list[list[str]] = []

    def fake_check_output(cmd, **kwargs):
      del kwargs
      captured_cmds.append(list(cmd))
      return json.dumps({
          "number": 195366,
          "title": "[web_paragraph] Render soft hyphens",
          "body": "Fixes #180001\n\nFollow-up for soft hyphen layout.",
          "state": "OPEN",
          "isDraft": False,
          "author": {"login": "dbebawy"},
          "headRefOid": "11db23963ceac1e412b732b3121f007ff76920ea",
          "headRefName": "soft-hyphens",
          "baseRefName": "master",
          "reviewDecision": "REVIEW_REQUIRED",
          "reviews": [],
          "latestReviews": [],
          "comments": [],
          "commits": [{
              "oid": "11db23963ceac1e412b732b3121f007ff76920ea",
              "committedDate": "2026-11-02T19:00:00Z",
              "messageHeadline": "[web_paragraph] Render soft hyphens",
              "authors": [{"login": "dbebawy"}],
          }],
          "statusCheckRollup": [],
          "labels": [],
          "mergeable": "MERGEABLE",
          "reviewRequests": [{"login": "Rusino"}],
          "closingIssuesReferences": [
              {"number": 180002, "repository": {"nameWithOwner": "flutter/flutter"}}
          ],
          "createdAt": "2026-10-25T12:00:00Z",
          "updatedAt": "2026-11-02T19:13:00Z",
          "files": [{
              "path": "engine/src/flutter/lib/web_ui/lib/src/engine/web_paragraph/layout.dart"
          }],
      })

    with mock.patch.object(w.subprocess, "check_output", side_effect=fake_check_output):
      res = w.fetch_github_pr_status("195366")

    self.assertTrue(res["ok"])
    self.assertEqual(len(captured_cmds), 1)
    cmd = captured_cmds[0]
    json_idx = cmd.index("--json") + 1
    requested_fields = set(cmd[json_idx].split(","))
    self.assertNotIn("closingIssuesReferences", requested_fields)
    self.assertTrue(
        requested_fields.issubset(GH_2_46_SUPPORTED_PR_JSON_FIELDS),
        f"Unsupported gh 2.46 fields: {requested_fields - GH_2_46_SUPPORTED_PR_JSON_FIELDS}",
    )
    self.assertEqual(
        res["linked_bug_keys"],
        ["gh_issue:flutter/flutter#180002", "gh_issue:flutter/flutter#180001"],
    )

  def test_fetch_github_pr_status_supports_custom_repo_slug(self):
    captured_cmds: list[list[str]] = []

    def fake_check_output(cmd, **kwargs):
      del kwargs
      captured_cmds.append(list(cmd))
      return json.dumps({
          "number": 42,
          "title": "Sync soft hyphen layout",
          "body": "Closes #17",
          "state": "OPEN",
          "isDraft": False,
          "author": {"login": "Rusino"},
          "headRefOid": "abcdef1234567890abcdef1234567890abcdef12",
          "headRefName": "sync-hyphen",
          "baseRefName": "main",
          "reviews": [],
          "comments": [],
          "commits": [],
          "statusCheckRollup": [{"name": "unit-tests", "conclusion": "FAILURE"}],
          "labels": [],
          "mergeable": "MERGEABLE",
          "reviewRequests": [{"login": "reviewer1"}],
          "createdAt": "2026-11-03T10:00:00Z",
          "updatedAt": "2026-11-03T10:05:00Z",
          "files": [],
      })

    with mock.patch.object(w.subprocess, "check_output", side_effect=fake_check_output), \
         mock.patch.object(w, "partition_github_failed_checks") as mock_flutter_partition:
      res = w.fetch_github_pr_status("42", repo_slug="Rusino/paragraph")

    self.assertTrue(res["ok"])
    mock_flutter_partition.assert_not_called()
    cmd = captured_cmds[0]
    repo_idx = cmd.index("--repo") + 1
    self.assertEqual(cmd[repo_idx], "Rusino/paragraph")
    self.assertEqual(res["host"], "github.com/Rusino/paragraph")
    self.assertEqual(res["url"], "https://github.com/Rusino/paragraph/pull/42")
    self.assertEqual(
        res["commit_url"],
        "https://github.com/Rusino/paragraph/commit/abcdef1234567890abcdef1234567890abcdef12",
    )
    self.assertEqual(res["linked_bug_keys"], ["gh_issue:Rusino/paragraph#17"])
    self.assertEqual(res["own_failed_checks"], ["unit-tests"])


class R71TriageStoreIngestTest(unittest.TestCase):

  def setUp(self):
    self.test_dir = tempfile.mkdtemp(prefix="r71_triage_")
    self.tmp_dir = tempfile.mkdtemp(prefix="r71_tmp_")

  def tearDown(self):
    shutil.rmtree(self.test_dir, ignore_errors=True)
    shutil.rmtree(self.tmp_dir, ignore_errors=True)

  def test_parse_single_audit_text_supports_gerrit_links_urls_and_revision(self):
    md_1409697 = (
        "## Follow-up: [Gerrit 1409697 PS6]"
        '(https://skia-review.googlesource.com/c/skia/+/1409697/6), "[skparagraph] Indent"\n\n'
        "**Category:** 🟢 rebase only. **Review time:** about 3 minutes.\n\n"
        "- You commented on [PS5](https://skia-review.googlesource.com/c/skia/+/1409697/5)"
        " (`11d7fba59d`).\n"
        "- The author uploaded [PS6](https://skia-review.googlesource.com/c/skia/+/1409697/6)"
        " (`6e8dd008c2`).\n\n"
        "### Draft (post only after the google3 A/B comes back clean)\n"
        "```\n"
        "Thanks for the rebase. LGTM pending google3 checks.\n"
        "```\n"
    )
    parsed = ts.parse_single_audit_text(md_1409697, source_path="/tmp/wf516_1409697.md")
    self.assertIsNotNone(parsed)
    self.assertEqual(parsed["kind"], "gerrit")
    self.assertEqual(parsed["target_id"], "1409697")
    self.assertEqual(parsed["triaged_patchset"], 6)
    self.assertEqual(parsed["triaged_revision"], "6e8dd008c2")
    self.assertIn("Thanks for the rebase", parsed["draft"])

    # Parent CL SHA on the same line must not pollute child CL's triaged_revision
    md_1412383 = (
        "## [Gerrit 1412383 PS4](https://skia-review.googlesource.com/c/skia/+/1412383/4):"
        " follow-up\n\n"
        "**Category:** `[FOLLOW-UP / REBASE-ONLY]`. Your CR+1 carried over correctly.\n"
        "**Review time:** about 2 minutes.\n"
        "- **[PS4](https://skia-review.googlesource.com/c/skia/+/1412383/4)** moves onto "
        "[Gerrit 1412382 PS4](https://skia-review.googlesource.com/c/skia/+/1412382/4)"
        " (`90b9c516fd`).\n"
    )
    parsed_383 = ts.parse_single_audit_text(md_1412383, source_path="/tmp/wf516_1412383.md")
    self.assertIsNotNone(parsed_383)
    self.assertEqual(parsed_383["target_id"], "1412383")
    self.assertEqual(parsed_383["triaged_patchset"], 4)
    self.assertNotEqual(parsed_383.get("triaged_revision", ""), "90b9c516fd")

    # Bare Gerrit /c/<project>/+/<id> URL without Gerrit/CL prefix
    md_url_only = (
        "## Follow-up on https://chromium-review.googlesource.com/c/chromium/src/+/8663938/2\n"
        "**Category:** Mechanical\n"
        "**Review time:** about 1 min\n"
        "Uploaded PS2 (`deadbeef99`).\n"
    )
    parsed_url = ts.parse_single_audit_text(md_url_only)
    self.assertIsNotNone(parsed_url)
    self.assertEqual(parsed_url["kind"], "gerrit")
    self.assertEqual(parsed_url["target_id"], "8663938")
    self.assertEqual(parsed_url["triaged_patchset"], 2)
    self.assertEqual(parsed_url["triaged_revision"], "deadbeef99")

    # Plain Gerrit <N> header without a /c/.../+/<N> URL must also resolve via Gerrit\s+(\d+)
    md_gerrit_plain = (
        "## Follow-up: Gerrit 1409697 PS6\n"
        "**Category:** Mechanical\n"
        "Uploaded PS6 (`6e8dd008c2`).\n"
    )
    parsed_plain = ts.parse_single_audit_text(md_gerrit_plain)
    self.assertIsNotNone(parsed_plain)
    self.assertEqual(parsed_plain["kind"], "gerrit")
    self.assertEqual(parsed_plain["target_id"], "1409697")
    self.assertEqual(parsed_plain["triaged_patchset"], 6)
    self.assertEqual(parsed_plain["triaged_revision"], "6e8dd008c2")

  def test_github_pr_audit_with_skia_cl_mention_and_force_push_rev_drift(self):
    # Seed older review record on PR #195366 at PS1 (old head 88b5e70c41)
    old_wf = os.path.join(self.tmp_dir, "wf289_195366.md")
    with open(old_wf, "w", encoding="utf-8") as f:
      f.write(
          "## PR #195366 follow-up: [web_paragraph] Render soft hyphens\n"
          "**Category:** Waiting on others\n"
          "- **New head:** `88b5e70c41`\n"
          "- Related Skia CL 1406081.\n"
      )
    os.utime(old_wf, (1793282760, 1793282760))

    with open(old_wf, encoding="utf-8") as f:
      old_report_md = f.read()

    # Simulate pre-existing review record whose triaged_revision was poisoned by queue_triage
    ts.save_triage_result(
        {
            "target_id": "195366",
            "item_key": "github:195366",
            "kind": "github",
            "verdict": "CLEAN",
            "verdict_label": "🟢 Clean (LGTM)",
            "review_outcome": "CLEAN",
            "summary": "Draft PR stacked on #195384 and #195122; waiting on author",
            "triaged_patchset": 1,
            "triaged_at": "2026-10-31T23:41:17Z",
            "source": "review",
            "triaged_revision": "11db23963ceac1e412b732b3121f007ff76920ea",
            "report_markdown": old_report_md,
            "report_path": old_wf,
        },
        results_dir=self.test_dir,
    )

    # Newer wf516_195366.md at PS1 after force-push to 11db23963c, mentioning Skia CL 1406081
    new_wf = os.path.join(self.tmp_dir, "wf516_195366.md")
    with open(new_wf, "w", encoding="utf-8") as f:
      f.write(
          "## [PR #195366](https://github.com/flutter/flutter/pull/195366)"
          " [web_paragraph] Render soft hyphens: follow-up\n\n"
          "**Category:** 🔴 SURGICAL AUDIT / CORE DOMAIN\n"
          "**Review time:** about 5 minutes.\n"
          "- New head `11db23963c` was committed Nov 2 and force-pushed.\n"
          "- [Skia CL 1406081](https://skia-review.googlesource.com/c/skia/+/1406081) is noted.\n\n"
          "### Draft (only post once the corpus gate passes)\n"
          "```\n"
          "LGTM pending corpus gate (run_webparagraph_corpus.py).\n"
          "```\n"
      )
    os.utime(new_wf, (1793718616, 1793718616))

    ts.ingest_tmp_triage_files(tmp_dir=self.tmp_dir, results_dir=self.test_dir)
    loaded = ts.load_triage_result("github", "195366", results_dir=self.test_dir)
    self.assertIsNotNone(loaded)
    self.assertEqual(loaded["kind"], "github")
    self.assertEqual(loaded["target_id"], "195366")
    self.assertEqual(loaded["triaged_revision"], "11db23963c")
    self.assertEqual(loaded["source"], "audit")
    self.assertEqual(loaded["summary"], "LGTM pending corpus gate")
    self.assertIn("run_webparagraph_corpus.py", loaded["draft"])
    # Must not have misrouted PR #195366 into gerrit_1406081.json
    self.assertIsNone(ts.load_triage_result("gerrit", "1406081", results_dir=self.test_dir))

    # A subsequent newer save-review on a later SHA must NOT be clobbered by older /tmp/wf*.md files
    ts.save_review_verdict(
        target_id="195366",
        kind="github",
        patchset=1,
        outcome="CLEAN",
        summary="Corpus passed on newest commit",
        revision="ffeeddccbb11223344556677889900aabbccddee",
        corpus="not_required",
        results_dir=self.test_dir,
    )
    ts.ingest_tmp_triage_files(tmp_dir=self.tmp_dir, results_dir=self.test_dir)
    after_reingest = ts.load_triage_result("github", "195366", results_dir=self.test_dir)
    self.assertEqual(after_reingest["source"], "review")
    self.assertEqual(
        after_reingest["triaged_revision"], "ffeeddccbb11223344556677889900aabbccddee"
    )
    self.assertEqual(after_reingest["summary"], "Corpus passed on newest commit")


class R71ReviewerWakeupConcurrencyGateTest(unittest.TestCase):

  def setUp(self):
    super().setUp()
    w._RELATED_CLS_CACHE.clear()
    w._RELATED_STACK_ORDER_CACHE.clear()
    w._RELATED_RAW_CHANGES_CACHE.clear()

  def _empty_gmail(self) -> dict:
    return {
        "thread_ids": [],
        "inbox_thread_ids": [],
        "total_msgs": 0,
        "inbox_msgs": 0,
        "trash_msgs": 0,
        "unread_msgs": 0,
    }

  def test_sync_once_throttles_reviewer_stack_wakeups_and_advances_across_polls(self):
    host = "skia-review.googlesource.com"
    stack_ids = ["1412381", "1412382", "1412383", "1412384", "1412385", "1412386"]
    for cid_num in stack_ids:
      w._RELATED_STACK_ORDER_CACHE[f"{host}:{cid_num}"] = list(stack_ids)

    chatted = []
    prev_items = {}
    for idx, cl_num in enumerate(stack_ids):
      ikey = f"gerrit:{cl_num}"
      conv_id = f"cid-{cl_num}"
      chatted.append({
          "item_key": ikey,
          "kind": "gerrit",
          "target_id": cl_num,
          "preferred_host": host,
          "conversation_id": conv_id,
          "title": f"[10:0{idx}] ⦿ Gerrit {cl_num} cluster refactor",
          "project_id": "proj-skia",
          "project_name": "Skia",
          "chat_last_modified": f"2026-11-03T14:0{idx}:00Z",
          "single_target": True,
          "chat_busy": False,
          "chat_archived": False,
      })
      prev_items[f"{ikey}@{conv_id}"] = {
          "item_key": ikey,
          "kind": "gerrit",
          "target_id": cl_num,
          "conversation_id": conv_id,
          "status": "NEW",
          "phase": "WAITING_ON_OTHERS",
          "patchset": 2,
          "reviewed_ps": 2,
          "review_target_ps": 2,
          "is_self": False,
          "role": "reviewer (goog)",
          "is_watching": False,
          "in_my_attention": False,
          "chat_busy": False,
          "last_msg_author": "jlavrova@google.com",
          "last_msg_date": "2026-11-02 15:00:00.000000000",
      }

    def fake_status(cl_id, *args, **kwargs):
      del args, kwargs
      idx = stack_ids.index(str(cl_id))
      return {
          "ok": True,
          "host": host,
          "url": f"https://{host}/c/skia/+/{cl_id}",
          "status": "NEW",
          "phase": "NEEDS_YOUR_ACTION",
          "subject": f"[skparagraph] Stack part {idx + 1}",
          "owner_name": "KC Yeung",
          "owner_email": "yeung@example.com",
          "role": "reviewer (goog)",
          "is_self": False,
          "is_external": True,
          "is_draft": False,
          "is_wip": False,
          "in_development": False,
          "is_watching": False,
          "reviewer_removed": False,
          "patchset": 3,
          "revision": f"rev300000{idx}",
          "in_my_attention": True,
          "last_msg_author": "yeung@example.com",
          "last_msg_date": "2026-11-03 16:00:00.000000000",
          "last_msg_snippet": "Uploaded patch set 3.",
          "created": f"2026-11-01 10:0{idx}:00.000000000",
      }

    with tempfile.TemporaryDirectory() as tmp:
      state_path = os.path.join(tmp, "state.json")
      with open(state_path, "w", encoding="utf-8") as f:
        json.dump({"items": prev_items, "events": [], "review_claims": {}}, f)

      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(w, "fetch_gardener_snapshot", return_value=({}, [])), \
           mock.patch.object(w, "discover_active_cl_chats", return_value=chatted), \
           mock.patch.object(w, "discover_open_remote_changes", return_value=[]), \
           mock.patch.object(w, "fetch_gerrit_cl_status", side_effect=fake_status), \
           mock.patch.object(w, "fetch_gerrit_related_cl_ids", return_value=[]), \
           mock.patch.object(w, "fetch_gerrit_stack_order_ids", return_value=list(stack_ids)), \
           mock.patch.object(w, "fetch_gmail_status_for_id", return_value=self._empty_gmail()), \
           mock.patch.object(w, "overlay_live_chat_busy"), \
           mock.patch.object(
               w.audit_gate,
               "check_audit_preflight",
               return_value={"ok": True, "reason": ""},
           ), \
           mock.patch.object(ts, "overlay_triage_on_items"), \
           mock.patch.object(ts, "load_triage_result", return_value=None), \
           mock.patch.object(w, "wake_conversation", return_value=True) as mock_wake:
        # Poll 1 wakes ONLY stack root 1412381
        state1 = w.sync_once(wake_chats=True, skia_dir="/nonexistent", sync_bugs=False)
        self.assertEqual(mock_wake.call_count, 1)
        self.assertEqual(mock_wake.call_args[0][0], "cid-1412381")
        self.assertIn("gerrit:1412381:3", state1["review_claims"])
        for child_id in stack_ids[1:]:
          self.assertNotIn(f"gerrit:{child_id}:3", state1["review_claims"])
          child_rec = state1["items"][f"gerrit:{child_id}@cid-{child_id}"]
          self.assertFalse(child_rec["chat_busy"])
          self.assertEqual(child_rec["reviewed_ps"], 2)

        # Poll 2 while 1412381 is still busy: no child wakes
        mock_wake.reset_mock()
        chatted[0]["chat_busy"] = True
        state2 = w.sync_once(wake_chats=True, skia_dir="/nonexistent", sync_bugs=False)
        self.assertEqual(mock_wake.call_count, 0)
        self.assertNotIn("gerrit:1412382:3", state2["review_claims"])

      # Poll 3 after 1412381 finishes PS3 review: child 1412382 wakes next
      chatted[0]["chat_busy"] = False
      with open(state_path, encoding="utf-8") as f:
        saved2 = json.load(f)
      saved2["items"]["gerrit:1412381@cid-1412381"]["chat_woken_ts"] = 0.0
      saved2["items"]["gerrit:1412381@cid-1412381"]["chat_busy"] = False
      saved2["items"]["gerrit:1412381@cid-1412381"]["reviewed_ps"] = 3
      with open(state_path, "w", encoding="utf-8") as f:
        json.dump(saved2, f)

      def fake_load_triage(kind, tid):
        del kind
        if str(tid) == "1412381":
          return {"patchset": 3, "triaged_patchset": 3, "verdict": "CLEAN"}
        return None

      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(w, "fetch_gardener_snapshot", return_value=({}, [])), \
           mock.patch.object(w, "discover_active_cl_chats", return_value=chatted), \
           mock.patch.object(w, "discover_open_remote_changes", return_value=[]), \
           mock.patch.object(w, "fetch_gerrit_cl_status", side_effect=fake_status), \
           mock.patch.object(w, "fetch_gerrit_related_cl_ids", return_value=[]), \
           mock.patch.object(w, "fetch_gerrit_stack_order_ids", return_value=list(stack_ids)), \
           mock.patch.object(w, "fetch_gmail_status_for_id", return_value=self._empty_gmail()), \
           mock.patch.object(w, "overlay_live_chat_busy"), \
           mock.patch.object(
               w.audit_gate,
               "check_audit_preflight",
               return_value={"ok": True, "reason": ""},
           ), \
           mock.patch.object(ts, "overlay_triage_on_items"), \
           mock.patch.object(ts, "load_triage_result", side_effect=fake_load_triage), \
           mock.patch.object(w, "wake_conversation", return_value=True) as mock_wake3:
        state3 = w.sync_once(wake_chats=True, skia_dir="/nonexistent", sync_bugs=False)
        self.assertEqual(mock_wake3.call_count, 1)
        self.assertEqual(mock_wake3.call_args[0][0], "cid-1412382")
        self.assertIn("gerrit:1412382:3", state3["review_claims"])
        self.assertNotIn("gerrit:1412383:3", state3["review_claims"])

  def test_same_ps_comment_claim_key_and_unthrottled_critical_alerts(self):
    host = "skia-review.googlesource.com"
    chatted = [
        {
            "item_key": "gerrit:2001",
            "kind": "gerrit",
            "target_id": "2001",
            "preferred_host": host,
            "conversation_id": "cid-rev-2001",
            "title": "[10:00] ⦿ Gerrit 2001 comment reply",
            "project_id": "proj-skia",
            "project_name": "Skia",
            "chat_last_modified": "2026-11-03T15:00:00Z",
            "single_target": True,
            "chat_busy": False,
            "chat_archived": False,
        },
        {
            "item_key": "gerrit:2002",
            "kind": "gerrit",
            "target_id": "2002",
            "preferred_host": host,
            "conversation_id": "cid-rev-2002",
            "title": "[10:01] ⦿ Gerrit 2002 second review",
            "project_id": "proj-skia",
            "project_name": "Skia",
            "chat_last_modified": "2026-11-03T14:59:00Z",
            "single_target": True,
            "chat_busy": False,
            "chat_archived": False,
        },
        {
            "item_key": "gerrit:3001",
            "kind": "gerrit",
            "target_id": "3001",
            "preferred_host": host,
            "conversation_id": "cid-self-3001",
            "title": "[10:02] ⦿ Gerrit 3001 own CL",
            "project_id": "proj-skia",
            "project_name": "Skia",
            "chat_last_modified": "2026-11-03T14:58:00Z",
            "single_target": True,
            "chat_busy": False,
            "chat_archived": False,
        },
    ]
    prev_items = {
        "gerrit:2001@cid-rev-2001": {
            "item_key": "gerrit:2001",
            "kind": "gerrit",
            "target_id": "2001",
            "conversation_id": "cid-rev-2001",
            "status": "NEW",
            "phase": "WAITING_ON_OTHERS",
            "patchset": 6,
            "reviewed_ps": 6,
            "review_target_ps": 6,
            "is_self": False,
            "role": "reviewer (goog)",
            "is_watching": False,
            "chat_busy": False,
            "last_msg_author": "jlavrova@google.com",
            "last_msg_date": "2026-11-03 12:00:00.000000000",
        },
        "gerrit:2002@cid-rev-2002": {
            "item_key": "gerrit:2002",
            "kind": "gerrit",
            "target_id": "2002",
            "conversation_id": "cid-rev-2002",
            "status": "NEW",
            "phase": "WAITING_ON_OTHERS",
            "patchset": 1,
            "reviewed_ps": 1,
            "review_target_ps": 1,
            "is_self": False,
            "role": "reviewer (goog)",
            "is_watching": False,
            "chat_busy": False,
            "last_msg_author": "jlavrova@google.com",
            "last_msg_date": "2026-11-03 12:00:00.000000000",
        },
        "gerrit:3001@cid-self-3001": {
            "item_key": "gerrit:3001",
            "kind": "gerrit",
            "target_id": "3001",
            "conversation_id": "cid-self-3001",
            "status": "NEW",
            "phase": "WAITING_ON_OTHERS",
            "patchset": 4,
            "is_self": True,
            "role": "author",
            "chat_busy": False,
            "last_msg_author": "jlavrova@google.com",
            "last_msg_date": "2026-11-03 12:00:00.000000000",
        },
    }

    new_msg_ts = "2026-11-03 16:15:00.000000000"

    def fake_status(cl_id, *args, **kwargs):
      del args, kwargs
      if str(cl_id) == "2001":
        return {
            "ok": True,
            "host": host,
            "url": f"https://{host}/c/skia/+/2001",
            "status": "NEW",
            "phase": "NEEDS_YOUR_ACTION",
            "subject": "First review CL",
            "owner_name": "Alice",
            "owner_email": "alice@example.com",
            "role": "reviewer (goog)",
            "is_self": False,
            "is_external": True,
            "is_draft": False,
            "is_wip": False,
            "in_development": False,
            "is_watching": False,
            "patchset": 6,
            "revision": "6666666666",
            "in_my_attention": True,
            "last_msg_author": "alice@example.com",
            "last_msg_date": new_msg_ts,
            "last_msg_snippet": "Addressed your question on PS6.",
            "created": "2026-11-01 09:00:00.000000000",
        }
      if str(cl_id) == "2002":
        return {
            "ok": True,
            "host": host,
            "url": f"https://{host}/c/skia/+/2002",
            "status": "NEW",
            "phase": "NEEDS_YOUR_ACTION",
            "subject": "Second review CL",
            "owner_name": "Bob",
            "owner_email": "bob@example.com",
            "role": "reviewer (goog)",
            "is_self": False,
            "is_external": True,
            "is_draft": False,
            "is_wip": False,
            "in_development": False,
            "is_watching": False,
            "patchset": 2,
            "revision": "2222222222",
            "in_my_attention": True,
            "last_msg_author": "bob@example.com",
            "last_msg_date": new_msg_ts,
            "last_msg_snippet": "Uploaded patch set 2.",
            "created": "2026-11-01 09:30:00.000000000",
        }
      return {
          "ok": True,
          "host": host,
          "url": f"https://{host}/c/skia/+/3001",
          "status": "NEW",
          "phase": "ALERT_CQ_FAILED",
          "subject": "Own CL with CQ failure",
          "owner_name": "Julia",
          "owner_email": "jlavrova@google.com",
          "role": "author",
          "is_self": True,
          "is_external": False,
          "is_draft": False,
          "is_wip": False,
          "in_development": False,
          "is_watching": False,
          "patchset": 4,
          "revision": "4444444444",
          "in_my_attention": True,
          "cq_failed": True,
          "last_msg_author": "skia-commit-bot@chromium.org",
          "last_msg_date": new_msg_ts,
          "last_msg_snippet": "CQ failed on ASAN.",
          "created": "2026-11-01 10:00:00.000000000",
      }

    with tempfile.TemporaryDirectory() as tmp:
      state_path = os.path.join(tmp, "state.json")
      with open(state_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "items": prev_items,
                "events": [],
                "review_claims": {"gerrit:2001:6": {"cid": "cid-rev-2001", "ts": 1.0}},
            },
            f,
        )

      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(w, "fetch_gardener_snapshot", return_value=({}, [])), \
           mock.patch.object(w, "discover_active_cl_chats", return_value=chatted), \
           mock.patch.object(w, "discover_open_remote_changes", return_value=[]), \
           mock.patch.object(w, "fetch_gerrit_cl_status", side_effect=fake_status), \
           mock.patch.object(w, "fetch_gerrit_related_cl_ids", return_value=[]), \
           mock.patch.object(
               w, "fetch_gerrit_stack_order_ids", side_effect=lambda _h, cid: [str(cid)]
           ), \
           mock.patch.object(w, "fetch_gmail_status_for_id", return_value=self._empty_gmail()), \
           mock.patch.object(w, "send_desktop_alert"), \
           mock.patch.object(w, "overlay_live_chat_busy"), \
           mock.patch.object(
               w.audit_gate,
               "check_audit_preflight",
               return_value={"ok": True, "reason": ""},
           ), \
           mock.patch.object(ts, "overlay_triage_on_items"), \
           mock.patch.object(w, "wake_conversation", return_value=True) as mock_wake:
        state = w.sync_once(wake_chats=True, skia_dir="/nonexistent", sync_bugs=False)

    woken_cids = [call.args[0] for call in mock_wake.call_args_list]
    # With 0 busy chats initially, 2001 wakes on same-PS comment; 2002 hits per-poll cap (<1);
    # 3001 critical CQ alert remains unthrottled
    self.assertEqual(woken_cids, ["cid-rev-2001", "cid-self-3001"])
    self.assertIn(f"gerrit:2001:6:{new_msg_ts}", state["review_claims"])
    self.assertNotIn("gerrit:2002:2", state["review_claims"])

    # With 2 busy reviewer chats already running, busy_review_chats < 2 blocks 2002 even at count 0
    busy_seed_items = {
        "gerrit:2002@cid-rev-2002": dict(prev_items["gerrit:2002@cid-rev-2002"]),
        "gerrit:2998@cid-busy-a": {
            "item_key": "gerrit:2998",
            "kind": "gerrit",
            "target_id": "2998",
            "conversation_id": "cid-busy-a",
            "status": "NEW",
            "is_self": False,
            "role": "reviewer (goog)",
            "is_watching": False,
            "chat_busy": True,
        },
        "gerrit:2999@cid-busy-b": {
            "item_key": "gerrit:2999",
            "kind": "gerrit",
            "target_id": "2999",
            "conversation_id": "cid-busy-b",
            "status": "NEW",
            "is_self": False,
            "role": "reviewer (goog)",
            "is_watching": False,
            "chat_busy": True,
        },
    }
    with tempfile.TemporaryDirectory() as tmp2:
      state_path2 = os.path.join(tmp2, "state.json")
      with open(state_path2, "w", encoding="utf-8") as f:
        json.dump({"items": busy_seed_items, "events": [], "review_claims": {}}, f)
      with mock.patch.object(w, "STATE_FILE", state_path2), \
           mock.patch.object(w, "fetch_gardener_snapshot", return_value=({}, [])), \
           mock.patch.object(w, "discover_active_cl_chats", return_value=[chatted[1]]), \
           mock.patch.object(w, "discover_open_remote_changes", return_value=[]), \
           mock.patch.object(w, "fetch_gerrit_cl_status", side_effect=fake_status), \
           mock.patch.object(w, "fetch_gerrit_related_cl_ids", return_value=[]), \
           mock.patch.object(
               w, "fetch_gerrit_stack_order_ids", side_effect=lambda _h, cid: [str(cid)]
           ), \
           mock.patch.object(w, "fetch_gmail_status_for_id", return_value=self._empty_gmail()), \
           mock.patch.object(w, "overlay_live_chat_busy"), \
           mock.patch.object(
               w.audit_gate,
               "check_audit_preflight",
               return_value={"ok": True, "reason": ""},
           ), \
           mock.patch.object(ts, "overlay_triage_on_items"), \
           mock.patch.object(w, "wake_conversation", return_value=True) as mock_wake_busy:
        state_busy = w.sync_once(wake_chats=True, skia_dir="/nonexistent", sync_bugs=False)
      self.assertEqual(mock_wake_busy.call_count, 0)
      self.assertNotIn("gerrit:2002:2", state_busy["review_claims"])


if __name__ == "__main__":
  unittest.main()
