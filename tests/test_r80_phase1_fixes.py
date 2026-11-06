#!/usr/bin/env python3
"""Unit tests for R80 Phase 1 fixes in cl_watcher.py, bug_actions.py, and prepare-commit-msg."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts"
)
if SCRIPTS_DIR not in sys.path:
  sys.path.insert(0, SCRIPTS_DIR)

import bug_actions  # pylint: disable=g-import-not-at-top
import cl_watcher as w  # pylint: disable=g-import-not-at-top


class R80Phase1FixesTest(unittest.TestCase):

  def test_dashboard_html_has_no_unrequested_search_box(self):
    self.assertNotIn('id="search-box"', w.DASHBOARD_HTML)
    self.assertNotIn("updateSearchQuery", w.DASHBOARD_HTML)

  def test_locks_are_reentrant(self):
    self.assertEqual(type(w._SYNC_LOCK).__name__, "RLock")
    self.assertEqual(type(w._STATE_IO_LOCK).__name__, "RLock")

  def test_merge_concurrent_state_mutations_preserves_ack_reassign_and_rekey(self):
    baseline = {
        "items": {
            "gerrit:1408158@unchatted": {
                "item_key": "gerrit:1408158",
                "composite_key": "gerrit:1408158@unchatted",
                "conversation_id": "",
                "status": "NEW",
                "phase": "NEEDS_YOUR_ACTION",
                "self_approved": False,
                "chat_busy": False,
            },
            "gerrit:1410000@c1": {
                "item_key": "gerrit:1410000",
                "composite_key": "gerrit:1410000@c1",
                "conversation_id": "c1",
                "status": "NEW",
                "phase": "WAITING_ON_REVIEWERS",
                "self_approved": False,
            },
        },
        "bugs": {
            "buganizer:5001": {
                "id": "5001",
                "bug_key": "buganizer:5001",
                "status": "ASSIGNED",
                "assignee": "jlavrova@google.com",
                "is_collaborator": True,
                "acked_ts": 100.0,
                "modified_ts": 200.0,
                "last_human_comment_at": "2026-11-04 10:00:00 UTC",
                "last_human_comment_by": "reviewer@google.com",
            },
            "buganizer:5002": {
                "id": "5002",
                "bug_key": "buganizer:5002",
                "title": "[Merge M144] Fix OOB",
                "component_id": "1579812",
                "reporter": "chrome-release-blintz-continuous-runner@prod.google.com",
                "parent_issue_id": "5000",
                "merge_status": "Pending",
                "status": "ASSIGNED",
                "assignee": "jlavrova@google.com",
                "is_collaborator": False,
            },
        },
        "bug_acks": {"buganizer:5001": 100.0},
        "confirmed_merge_bugs": {},
    }

    # Concurrent mutations written to disk while sync_once was polling
    disk_during_sync = {
        "items": {
            "gerrit:1408158@new-cid": {
                "item_key": "gerrit:1408158",
                "composite_key": "gerrit:1408158@new-cid",
                "conversation_id": "new-cid",
                "title": "Gerrit 1408158 test",
                "status": "NEW",
                "phase": "NEEDS_YOUR_ACTION",
                "self_approved": False,
                "chat_busy": True,
            },
            "gerrit:1410000@c1": {
                "item_key": "gerrit:1410000",
                "composite_key": "gerrit:1410000@c1",
                "conversation_id": "c1",
                "status": "ABANDONED",
                "phase": "ABANDONED",
                "self_approved": True,
            },
        },
        "bugs": {
            "buganizer:5001": {
                "id": "5001",
                "bug_key": "buganizer:5001",
                "status": "ASSIGNED",
                "assignee": "jlavrova@google.com",
                "is_collaborator": True,
                "acked_ts": 9999999999.0,
                "modified_ts": 200.0,
            },
            "buganizer:5002": {
                "id": "5002",
                "bug_key": "buganizer:5002",
                "title": "[Merge M144] Fix OOB",
                "component_id": "1579812",
                "reporter": "chrome-release-blintz-continuous-runner@prod.google.com",
                "parent_issue_id": "5000",
                "merge_status": "Pending",
                "status": "ASSIGNED",
                "assignee": "merges@chromium.org",
                "is_collaborator": True,
            },
        },
        "bug_acks": {"buganizer:5001": 9999999999.0},
        "confirmed_merge_bugs": {"5000": True},
    }

    # New state computed by sync_once from stale baseline
    new_state = json.loads(json.dumps(baseline))

    with tempfile.TemporaryDirectory() as tmp:
      state_path = os.path.join(tmp, "state.json")
      with mock.patch.object(w, "STATE_FILE", state_path):
        w.save_watcher_state(disk_during_sync)
        w.merge_concurrent_state_mutations(new_state, baseline)

    self.assertEqual(new_state["bug_acks"]["buganizer:5001"], 9999999999.0)
    self.assertEqual(
        new_state["bugs"]["buganizer:5001"]["acked_ts"], 9999999999.0
    )
    self.assertEqual(
        new_state["bugs"]["buganizer:5002"]["assignee"], "merges@chromium.org"
    )
    self.assertTrue(new_state["bugs"]["buganizer:5002"]["is_collaborator"])
    self.assertTrue(new_state["confirmed_merge_bugs"].get("5000"))
    self.assertNotIn("gerrit:1408158@unchatted", new_state["items"])
    self.assertIn("gerrit:1408158@new-cid", new_state["items"])
    self.assertTrue(new_state["items"]["gerrit:1408158@new-cid"]["chat_busy"])
    self.assertEqual(
        new_state["items"]["gerrit:1410000@c1"]["status"], "ABANDONED"
    )
    self.assertTrue(new_state["items"]["gerrit:1410000@c1"]["self_approved"])

  def test_fetch_gerrit_cl_status_skia_404_does_not_fall_back_to_chromium(self):
    probed = []

    def fake_urlopen_404(req, timeout=8):
      url = req.full_url if hasattr(req, "full_url") else str(req)
      probed.append(url)
      raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))

    with mock.patch.object(
        w.urllib.request, "urlopen", side_effect=fake_urlopen_404
    ):
      res = w.fetch_gerrit_cl_status(
          "1404136",
          {},
          [],
          skia_dir="/nonexistent",
          preferred_host="skia-review.googlesource.com",
      )

    self.assertFalse(res["ok"])
    self.assertEqual(len(probed), 1)
    self.assertIn("skia-review.googlesource.com", probed[0])

  def test_link_bugs_and_cls_and_classify_bug_status_created_ts_fallback(self):
    bugs = {
        "buganizer:4001": {
            "id": "4001",
            "bug_key": "buganizer:4001",
            "status": "ASSIGNED",
            "assignee": "jlavrova@google.com",
            "is_collaborator": False,
            "last_human_comment_at": "2026-11-01 10:00:00 UTC",
            "last_human_comment_author": "reporter@google.com",
            "gerrit_cl_ids": ["1414016"],
        }
    }
    items = {
        "gerrit:1414016@c1": {
            "item_key": "gerrit:1414016",
            "kind": "gerrit",
            "target_id": "1414016",
            "status": "NEW",
            "is_self": True,
            "is_wip": False,
            "is_draft": False,
            "created": "2026-11-02 12:00:00.000000000",
            "activity_ts": 1793620800.0,
        }
    }
    w.link_bugs_and_cls(bugs, items)
    linked = bugs["buganizer:4001"]["linked_cls"]
    self.assertEqual(len(linked), 1)
    self.assertGreater(linked[0]["created_ts"], 0.0)

    # Even if created_ts is cleared to 0, activity_ts fallback acknowledges the earlier comment
    linked[0]["created_ts"] = 0.0
    w.classify_bug_status(bugs["buganizer:4001"])
    self.assertNotEqual(bugs["buganizer:4001"].get("lane"), "you")

  def test_reassign_security_merge_batch_tolerates_phase3_readback_timeout(self):
    bugs = {
        "buganizer:6001": {
            "id": "6001",
            "bug_key": "buganizer:6001",
            "title": "[Merge M144] Security fix",
            "component_id": "1579812",
            "reporter": "chrome-release-blintz-continuous-runner@prod.google.com",
            "parent_issue_id": "6000",
            "merge_status": "Pending",
            "status": "ASSIGNED",
            "assignee": "jlavrova@google.com",
            "is_collaborator": False,
        }
    }
    # Phase 1 succeeds, Phase 2 succeeds, Phase 3 read-back returns empty strings on timeout
    side_effects = [
        ["ok comment", "ok collab"],
        ["ok reassigned"],
        ["", ""],
    ]
    with mock.patch.object(
        w, "call_buganizer_mcp_batch", side_effect=side_effects
    ):
      res = w.reassign_security_merge_batch(bugs, ["6001"])

    self.assertTrue(res["ok"])
    self.assertEqual(res["reassigned_ids"], ["6001"])
    self.assertEqual(res["failed_ids"], [])
    self.assertEqual(
        bugs["buganizer:6001"]["assignee"], "merges@chromium.org"
    )
    self.assertTrue(bugs["buganizer:6001"]["is_collaborator"])

  def test_bug_actions_scope_and_validation(self):
    # 1. close_fixed rejects empty or non-Gerrit URL
    bad_close = bug_actions.close_bug_with_fixed_by("576664769", "")
    self.assertFalse(bad_close["ok"])
    bad_url = bug_actions.close_bug_with_fixed_by(
        "576664769", "https://example.com/123"
    )
    self.assertFalse(bad_url["ok"])

    # Valid close_fixed
    with mock.patch.object(
        w,
        "call_buganizer_mcp_batch",
        return_value=[
            "ok field",
            "ok status",
            "Issue 576664769 status: FIXED https://skia-review.googlesource.com/c/skia/+/1404196",
        ],
    ):
      ok_close = bug_actions.close_bug_with_fixed_by(
          "576664769", "https://skia-review.googlesource.com/c/skia/+/1404196"
      )
    self.assertTrue(ok_close["ok"])
    self.assertEqual(ok_close["status"], "FIXED")

    # 2. render enforces authorization scope and wraps in <untrusted_bug_content>
    with mock.patch.object(
        w, "call_buganizer_mcp_batch", return_value=["assignee: someone@else.com"]
    ):
      denied = bug_actions.render_issue_with_external_auth(
          "999999", state={"bugs": {}}
      )
    self.assertFalse(denied["ok"])
    self.assertIn("not in Flight Deck state", denied["error"])

    with mock.patch.object(
        w, "call_buganizer_mcp_batch", return_value=["Public bug description"]
    ):
      allowed = bug_actions.render_issue_with_external_auth(
          "466929543",
          state={"bugs": {"buganizer:466929543": {"id": "466929543"}}},
      )
    self.assertTrue(allowed["ok"])
    self.assertIn("<untrusted_bug_content", allowed["content"])
    self.assertIn("Public bug description", allowed["content"])

    # 3. reassign_merge blocks when merge_request_groups gates failed
    fake_state = {
        "bugs": {
            "buganizer:7001": {
                "id": "7001",
                "parent_issue_id": "7000",
            }
        },
        "merge_request_groups": {
            "7000": {
                "all_gates_passed": False,
                "failed_gates": ["24h Canary soak pending"],
            }
        },
    }
    with mock.patch.object(w, "load_watcher_state", return_value=fake_state):
      blocked = bug_actions.reassign_security_merge("7001")
    self.assertFalse(blocked["ok"])
    self.assertIn("Pre-gates not satisfied", blocked["error"])

  def test_prepare_commit_msg_hook_inserts_assisted_by_before_change_id(self):
    root = os.path.abspath(os.path.join(SCRIPTS_DIR, ".."))
    hook_path = os.path.join(root, "githooks", "prepare-commit-msg")
    with tempfile.TemporaryDirectory() as tmp:
      msg_file = os.path.join(tmp, "COMMIT_EDITMSG")
      with open(msg_file, "w", encoding="utf-8") as f:
        f.write(
            "Fix paragraph layout edge case\n\nChange-Id: I1234567890abcdef\n"
        )
      subprocess.run([hook_path, msg_file, "message"], cwd=tmp, check=True)
      with open(msg_file, "r", encoding="utf-8") as f:
        out = f.read()
    self.assertIn("Assisted-by:", out)
    self.assertLess(out.index("Assisted-by:"), out.index("Change-Id:"))

  def test_single_latest_assisted_by_across_hooks_and_guards(self):
    import io  # pylint: disable=g-import-not-at-top
    root = os.path.abspath(os.path.join(SCRIPTS_DIR, ".."))
    hooks_dir = os.path.join(root, "hooks")
    comments_dir = os.path.join(root, "skills", "code-comments", "scripts")
    for p in (hooks_dir, comments_dir):
      if p not in sys.path:
        sys.path.insert(0, p)
    import audit_comments  # pylint: disable=g-import-not-at-top
    import auto_ship  # pylint: disable=g-import-not-at-top
    import chat_lifecycle  # pylint: disable=g-import-not-at-top
    import pre_tool_guard  # pylint: disable=g-import-not-at-top

    hook_path = os.path.join(root, "githooks", "prepare-commit-msg")
    stacked = (
        "[SkoreText] Fix edge case\n\n"
        "Body quotes old lines:\n"
        "  Assisted-by: Quoted Example 1\n"
        "  Assisted-by: Quoted Example 2\n\n"
        "Perf: +1.2%\n"
        "Assisted-by: Claude Fable 5.1 (Max)\n"
        "Assisted-by: Gemini 4 Argon (High)\n"
        "Assisted-by: Claude Opus 5.5 (Medium)\n"
        "Change-Id: I1234567890abcdef1234567890abcdef12345678\n"
        "# ------------------------ >8 ------------------------\n"
        " Assisted-by: Diff Context Line\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
      msg_file = os.path.join(tmp, "COMMIT_EDITMSG")
      with open(msg_file, "w", encoding="utf-8") as f:
        f.write(stacked)
      env = dict(os.environ, CONVERSATION_ID="", ANTIGRAVITY_CONVERSATION_ID="")
      subprocess.run([hook_path, msg_file, "commit"], cwd=tmp, env=env, check=True)
      with open(msg_file, "r", encoding="utf-8") as f:
        out = f.read()

      # Also verify active model replacement via a shim chat_lifecycle.py in a snapshot copy
      snap_hooks = os.path.join(tmp, "hooks")
      snap_githooks = os.path.join(tmp, "githooks")
      os.makedirs(snap_hooks)
      os.makedirs(snap_githooks)
      with open(os.path.join(snap_hooks, "chat_lifecycle.py"), "w", encoding="utf-8") as f:
        f.write('print("Assisted-by: Gemini 4 Carbon (High)")\n')
      snap_hook = os.path.join(snap_githooks, "prepare-commit-msg")
      with open(hook_path, "r", encoding="utf-8") as src, open(
          snap_hook, "w", encoding="utf-8"
      ) as dst:
        dst.write(src.read())
      os.chmod(snap_hook, 0o755)
      msg_active = os.path.join(tmp, "COMMIT_ACTIVE")
      with open(msg_active, "w", encoding="utf-8") as f:
        f.write(stacked)
      subprocess.run([snap_hook, msg_active, "commit"], cwd=tmp, env=env, check=True)
      with open(msg_active, "r", encoding="utf-8") as f:
        out_active = f.read()

    head_part = out.split("# ------------------------ >8 ------------------------")[0]
    ab_lines = [l for l in head_part.splitlines() if l.startswith("Assisted-by:")]
    self.assertEqual(ab_lines, ["Assisted-by: Claude Opus 5.5 (Medium)"])
    self.assertIn("  Assisted-by: Quoted Example 1", head_part)
    self.assertIn("Perf: +1.2%\nAssisted-by: Claude Opus 5.5 (Medium)\nChange-Id:", head_part)
    active_head = out_active.split("# ------------------------ >8 ------------------------")[0]
    self.assertEqual(
        [l for l in active_head.splitlines() if l.startswith("Assisted-by:")],
        ["Assisted-by: Gemini 4 Carbon (High)"],
    )

    issues = audit_comments.audit_commit_message(stacked)
    self.assertTrue(any("multiple Assisted-by trailers" in m for _, m in issues))
    self.assertEqual(audit_comments.audit_commit_message(out), [])

    with mock.patch.object(
        chat_lifecycle, "get_model_trailer", return_value="Assisted-by: Gemini 4 Carbon (High)"
    ):
      replaced = auto_ship._append_model_trailer(
          "Fix bug\n\nAssisted-by: Old 1\nAssisted-by: Old 2\nChange-Id: Iabc", "cid-1"
      )
    self.assertEqual(
        [l for l in replaced.splitlines() if l.startswith("Assisted-by:")],
        ["Assisted-by: Gemini 4 Carbon (High)"],
    )
    self.assertLess(replaced.index("Assisted-by:"), replaced.index("Change-Id:"))

    def run_guard_cmd(command_line: str) -> str:
      payload = json.dumps({
          "toolCall": {"name": "run_command", "args": {"CommandLine": command_line}}
      })
      buf = io.StringIO()
      with mock.patch.object(sys, "stdin", io.StringIO(payload)), mock.patch.object(
          sys, "stdout", buf
      ):
        pre_tool_guard.main()
      return buf.getvalue().strip()

    for bad_cmd in (
        'git -C /tmp/r commit -m "Subj" -m "Assisted-by: A" -m "Assisted-by: B"',
        'gh pr edit 42 --body "Body\n\nAssisted-by: A\nAssisted-by: B"',
    ):
      raw_out = run_guard_cmd(bad_cmd)
      self.assertIn("[ASSISTED-BY GUARD]", raw_out)
      self.assertEqual(json.loads(raw_out)["decision"], "deny")

    # Indented prose examples and non-commit heredoc test fixtures must not self-block
    for ok_cmd in (
        'git commit -m "Subj\n\n  Assisted-by: Q1\n  Assisted-by: Q2\n\nAssisted-by: B"',
        "cat <<'EOF' > /tmp/fixture.txt\ngit commit\nAssisted-by: A\nAssisted-by: B\nEOF",
    ):
      self.assertNotIn("[ASSISTED-BY GUARD]", run_guard_cmd(ok_cmd))


if __name__ == "__main__":
  unittest.main()
