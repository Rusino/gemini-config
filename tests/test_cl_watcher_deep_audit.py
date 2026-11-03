#!/usr/bin/env python3
"""Contract tests for R26.3 and R26.4 deep audit gate, UI, and daily-chat migration."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import audit_gate  # pylint: disable=g-import-not-at-top
import cl_watcher as w  # pylint: disable=g-import-not-at-top
import triage_store as ts  # pylint: disable=g-import-not-at-top


def _gerrit_json_resp(payload: object) -> io.BytesIO:
    raw = ")]}'\n" + json.dumps(payload)
    buf = io.BytesIO(raw.encode("utf-8"))
    buf.status = 200  # type: ignore[attr-defined]
    buf.__enter__ = lambda self=buf: self  # type: ignore[attr-defined]
    buf.__exit__ = lambda *args: False  # type: ignore[attr-defined]
    return buf


class DeepAuditWatcherContractTests(unittest.TestCase):

    def _eval_js(self, js_code: str) -> dict:
        script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        harness = script_body.split("\nboot();", 1)[0] + "\n" + js_code
        proc = subprocess.run(
            ["node", "-"], input=harness, capture_output=True, text=True, check=True
        )
        return json.loads(proc.stdout.strip())

    def test_fetch_gerrit_cl_status_extracts_revisions_by_ps_and_external_threads(self):
        detail_payload = {
            "project": "skia",
            "status": "NEW",
            "subject": "[skparagraph] Adjust cluster bounds",
            "owner": {"email": "author@example.com", "name": "Author"},
            "current_revision": "sha_ps2_abcdef",
            "revisions": {
                "sha_ps1_111111": {
                    "_number": 1,
                    "kind": "REWORK",
                    "commit": {"parents": [{"commit": "parent_old_111"}]},
                },
                "sha_ps2_abcdef": {
                    "_number": 2,
                    "kind": "TRIVIAL_REBASE",
                    "commit": {
                        "parents": [{"commit": "parent_new_222"}],
                        "message": "[skparagraph] Adjust cluster bounds\n",
                    },
                    "files": {"modules/skparagraph/src/Run.cpp": {}},
                },
            },
            "attention_set": {
                "1": {
                    "account": {"email": "jlavrova@google.com"},
                    "reason": "Added as reviewer",
                }
            },
            "reviewers": {"REVIEWER": [{"email": "jlavrova@google.com"}]},
            "labels": {"Code-Review": {"all": []}, "Commit-Queue": {"all": []}},
            "messages": [
                {
                    "_revision_number": 2,
                    "author": {"email": "colleague@google.com", "name": "Colleague"},
                    "date": "2026-11-03 15:00:00.000000000",
                    "message": "Patch Set 2:\n\n(1 comment)",
                }
            ],
            "_comments": {
                "modules/skparagraph/src/Run.cpp": [
                    {
                        "id": "c1",
                        "line": 42,
                        "patch_set": 2,
                        "updated": "2026-11-03 15:00:00.000000000",
                        "unresolved": True,
                        "author": {"email": "colleague@google.com"},
                        "message": "Should cluster end exclude trailing spaces?",
                    }
                ]
            },
        }
        with (
            mock.patch.object(
                w.urllib.request, "urlopen", return_value=_gerrit_json_resp(detail_payload)
            ),
            mock.patch.object(w, "gerrit_mergeable", return_value=True),
        ):
            res = w.fetch_gerrit_cl_status("1414017", {}, [], skia_dir="/nonexistent")

        self.assertTrue(res["ok"])
        self.assertEqual(
            res["revisions_by_ps"],
            {
                1: {"sha": "sha_ps1_111111", "kind": "REWORK", "parent": "parent_old_111"},
                2: {
                    "sha": "sha_ps2_abcdef",
                    "kind": "TRIVIAL_REBASE",
                    "parent": "parent_new_222",
                },
            },
        )
        self.assertEqual(res["open_external_threads"], 1)

    def test_sync_once_tier0_rebase_updates_triage_without_waking_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "cl_watcher_state.json")
            triage_dir = os.path.join(tmp, "triage_results")
            ts.save_review_verdict(
                target_id="1409697",
                kind="gerrit",
                patchset=5,
                outcome="CLEAN",
                summary="Cluster owner invariants verified",
                draft="LGTM.",
                corpus="passed",
                revision="sha_ps5_1111111",
                files=["modules/skparagraph/src/ParagraphImpl.cpp"],
                verify_attestation=False,
                results_dir=triage_dir,
            )
            seed_state = {
                "items": {
                    "gerrit:1409697@cid-1409697": {
                        "item_key": "gerrit:1409697",
                        "kind": "gerrit",
                        "target_id": "1409697",
                        "conversation_id": "cid-1409697",
                        "project_id": "proj-skp",
                        "project_name": "SkParagraph",
                        "status": "NEW",
                        "patchset": 5,
                        "revision": "sha_ps5_1111111",
                        "reviewed_ps": 5,
                        "review_target_ps": 5,
                        "role": "reviewer (ext)",
                        "is_self": False,
                        "in_my_attention": False,
                        "chat_busy": False,
                        "last_msg_date": "2026-11-03 12:00:00.000000000",
                    }
                },
                "review_claims": {"gerrit:1409697:5": {"cid": "cid-1409697", "ts": 1.0}},
                "events": [],
            }
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(seed_state, f)

            chatted = [
                {
                    "item_key": "gerrit:1409697",
                    "kind": "gerrit",
                    "target_id": "1409697",
                    "preferred_host": "skia-review.googlesource.com",
                    "conversation_id": "cid-1409697",
                    "title": "[10:00] ⦿ Gerrit 1409697 firstLineIndent",
                    "project_id": "proj-skp",
                    "project_name": "SkParagraph",
                    "chat_last_modified": "2026-11-03T12:05:00Z",
                    "single_target": True,
                    "chat_busy": False,
                }
            ]
            ps6_info = {
                "ok": True,
                "host": "skia-review.googlesource.com",
                "project": "skia",
                "url": "https://skia-review.googlesource.com/c/skia/+/1409697",
                "status": "NEW",
                "phase": "NEEDS_YOUR_ACTION",
                "subject": "[skparagraph] Add firstLineIndent",
                "owner_name": "Author",
                "owner_email": "author@example.com",
                "role": "reviewer (ext)",
                "is_self": False,
                "is_external": True,
                "is_draft": False,
                "is_wip": False,
                "in_development": False,
                "is_watching": False,
                "reviewer_removed": False,
                "self_cr_vote": 0,
                "attention_reason": "Vote got outdated and was removed: Code-Review+1",
                "patchset": 6,
                "revision": "sha_ps6_2222222",
                "revisions_by_ps": {
                    5: {"sha": "sha_ps5_1111111", "kind": "REWORK", "parent": "p5"},
                    6: {"sha": "sha_ps6_2222222", "kind": "TRIVIAL_REBASE", "parent": "p6"},
                },
                "files": ["modules/skparagraph/src/ParagraphImpl.cpp"],
                "in_my_attention": True,
                "last_msg_author": "author@example.com",
                "last_msg_date": "2026-11-03 16:00:00.000000000",
                "last_msg_snippet": "Uploaded patch set 6: Patch Set 5 was rebased.",
                "open_external_threads": 0,
            }
            empty_gmail = {
                "thread_ids": [],
                "inbox_thread_ids": [],
                "total_msgs": 0,
                "inbox_msgs": 0,
                "trash_msgs": 0,
                "unread_msgs": 0,
            }
            with (
                mock.patch.object(w, "STATE_FILE", state_path),
                mock.patch.object(w, "fetch_gardener_snapshot", return_value=({}, [])),
                mock.patch.object(w, "discover_active_cl_chats", return_value=chatted),
                mock.patch.object(w, "discover_open_remote_changes", return_value=[]),
                mock.patch.object(w, "fetch_gerrit_cl_status", return_value=ps6_info),
                mock.patch.object(w, "fetch_gmail_status_for_id", return_value=empty_gmail),
                mock.patch.object(w, "wake_conversation") as mock_wake,
            ):
                state = w.sync_once(wake_chats=True, update_titles=False, skia_dir="/nonexistent")

            mock_wake.assert_not_called()
            saved_tr = ts.load_triage_result("gerrit", "1409697", results_dir=triage_dir)
            self.assertIsNotNone(saved_tr)
            assert saved_tr is not None
            self.assertEqual(saved_tr["triaged_patchset"], 6)
            self.assertEqual(saved_tr["delta_kind"], "rebase_only")
            self.assertEqual(saved_tr["audit_state"], "done")
            self.assertEqual(saved_tr["corpus_status"], "pending")
            rec = state["items"]["gerrit:1409697@cid-1409697"]
            self.assertEqual(rec["reviewed_ps"], 6)

    def test_sync_once_blocked_auth_records_paused_state_and_recovers_after_gcert(self):
        remote = [
            {
                "item_key": "gerrit:1414017",
                "kind": "gerrit",
                "target_id": "1414017",
                "preferred_host": "skia-review.googlesource.com",
                "conversation_id": "",
                "title": "",
                "project_id": "proj-skp",
                "project_name": "SkParagraph",
                "chat_last_modified": "",
                "single_target": True,
                "chat_busy": False,
            }
        ]
        cl_info = {
            "ok": True,
            "host": "skia-review.googlesource.com",
            "project": "skia",
            "url": "https://skia-review.googlesource.com/c/skia/+/1414017",
            "status": "NEW",
            "phase": "NEEDS_YOUR_ACTION",
            "subject": "[skparagraph] Shift trailing space of RTL run",
            "owner_name": "David",
            "owner_email": "david@example.com",
            "role": "reviewer (ext)",
            "is_self": False,
            "is_external": True,
            "is_draft": False,
            "is_wip": False,
            "in_development": False,
            "is_watching": False,
            "reviewer_removed": False,
            "patchset": 1,
            "revision": "abc123456789",
            "files": ["modules/skparagraph/include/Paragraph.h"],
            "in_my_attention": True,
            "last_msg_author": "david@example.com",
            "last_msg_date": "2026-11-03 16:00:00.000000000",
            "last_msg_snippet": "Uploaded patch set 1.",
        }
        empty_gmail = {
            "thread_ids": [],
            "inbox_thread_ids": [],
            "total_msgs": 0,
            "inbox_msgs": 0,
            "trash_msgs": 0,
            "unread_msgs": 0,
        }

        def fake_start(items, ikey, matched_key, rec, prompt):
            w._rekey_state_item_chat(
                items, ikey, matched_key, rec, "cid-new-1", chat_archived=False
            )
            rec["chat_busy"] = True
            rec["review_target_ps"] = 1
            return {"ok": True, "started": True, "conversation_id": "cid-new-1"}

        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "cl_watcher_state.json")
            triage_dir = os.path.join(tmp, "triage_results")
            with (
                mock.patch.object(w, "STATE_FILE", state_path),
                mock.patch.object(w, "fetch_gardener_snapshot", return_value=({}, [])),
                mock.patch.object(w, "discover_active_cl_chats", return_value=[]),
                mock.patch.object(w, "discover_open_remote_changes", return_value=remote),
                mock.patch.object(w, "fetch_gerrit_cl_status", return_value=cl_info),
                mock.patch.object(w, "fetch_gmail_status_for_id", return_value=empty_gmail),
                mock.patch.object(
                    audit_gate,
                    "check_audit_preflight",
                    return_value={"ok": False, "reason": "LOAS expired (run gcert)"},
                ),
                mock.patch.object(
                    w, "_start_pinned_chat_for_record", side_effect=fake_start
                ) as mock_start,
            ):
                st1 = w.sync_once(wake_chats=True, update_titles=False, skia_dir="/nonexistent")

            mock_start.assert_not_called()
            self.assertNotIn("gerrit:1414017:1", st1["review_claims"])
            tr_blocked = ts.load_triage_result("gerrit", "1414017", results_dir=triage_dir)
            self.assertIsNotNone(tr_blocked)
            assert tr_blocked is not None
            self.assertEqual(tr_blocked["audit_state"], "blocked_auth")

            with (
                mock.patch.object(w, "STATE_FILE", state_path),
                mock.patch.object(w, "fetch_gardener_snapshot", return_value=({}, [])),
                mock.patch.object(w, "discover_active_cl_chats", return_value=[]),
                mock.patch.object(w, "discover_open_remote_changes", return_value=remote),
                mock.patch.object(w, "fetch_gerrit_cl_status", return_value=cl_info),
                mock.patch.object(w, "fetch_gmail_status_for_id", return_value=empty_gmail),
                mock.patch.object(
                    audit_gate,
                    "check_audit_preflight",
                    return_value={"ok": True, "reason": ""},
                ),
                mock.patch.object(
                    w, "_start_pinned_chat_for_record", side_effect=fake_start
                ) as mock_start2,
            ):
                st2 = w.sync_once(wake_chats=True, update_titles=False, skia_dir="/nonexistent")

            mock_start2.assert_called_once()
            self.assertIn("[FLIGHT-DECK AUDIT · Tier 2]", mock_start2.call_args[0][4])
            self.assertIn("-Wshorten-64-to-32", mock_start2.call_args[0][4])
            self.assertIn("gerrit:1414017:1", st2["review_claims"])
            tr_running = ts.load_triage_result("gerrit", "1414017", results_dir=triage_dir)
            assert tr_running is not None
            self.assertEqual(tr_running["audit_state"], "running")

    def test_sync_once_chatted_reviewer_wakeup_respects_concurrency_and_ping_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "cl_watcher_state.json")
            triage_dir = os.path.join(tmp, "triage_results")
            ts.save_review_verdict(
                target_id="1409637",
                kind="gerrit",
                patchset=1,
                outcome="CLEAN",
                summary="Looks good",
                draft="LGTM.",
                corpus="not_required",
                revision="sha_ps1_9999999",
                verify_attestation=False,
                results_dir=triage_dir,
            )
            seed_state = {
                "items": {
                    "gerrit:1409637@cid-1409637": {
                        "item_key": "gerrit:1409637",
                        "kind": "gerrit",
                        "target_id": "1409637",
                        "conversation_id": "cid-1409637",
                        "project_id": "proj-skia",
                        "project_name": "Skia",
                        "status": "NEW",
                        "patchset": 1,
                        "revision": "sha_ps1_9999999",
                        "reviewed_ps": 1,
                        "review_target_ps": 1,
                        "role": "reviewer (ext)",
                        "is_self": False,
                        "in_my_attention": True,
                        "chat_busy": False,
                        "last_msg_date": "2026-11-03 10:00:00.000000000",
                    },
                    "gerrit:2001@cid-b1": {
                        "conversation_id": "cid-b1",
                        "role": "reviewer (goog)",
                        "is_self": False,
                        "chat_busy": True,
                    },
                    "gerrit:2002@cid-b2": {
                        "conversation_id": "cid-b2",
                        "role": "reviewer (goog)",
                        "is_self": False,
                        "chat_busy": True,
                    },
                },
                "review_claims": {"gerrit:1409637:1": {"cid": "cid-1409637", "ts": 1.0}},
                "events": [],
            }
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(seed_state, f)

            chatted = [
                {
                    "item_key": "gerrit:1409637",
                    "kind": "gerrit",
                    "target_id": "1409637",
                    "preferred_host": "skia-review.googlesource.com",
                    "conversation_id": "cid-1409637",
                    "title": "[10:00] ⦿ Gerrit 1409637 fix",
                    "project_id": "proj-skia",
                    "project_name": "Skia",
                    "chat_last_modified": "2026-11-03T10:05:00Z",
                    "single_target": True,
                    "chat_busy": False,
                }
            ]
            ping_info = {
                "ok": True,
                "host": "skia-review.googlesource.com",
                "project": "skia",
                "url": "https://skia-review.googlesource.com/c/skia/+/1409637",
                "status": "NEW",
                "phase": "NEEDS_YOUR_ACTION",
                "subject": "Fix SkFont bounds",
                "owner_name": "Ext",
                "owner_email": "ext@example.com",
                "role": "reviewer (ext)",
                "is_self": False,
                "is_external": True,
                "is_draft": False,
                "is_wip": False,
                "in_development": False,
                "is_watching": False,
                "reviewer_removed": False,
                "patchset": 1,
                "revision": "sha_ps1_9999999",
                "files": ["src/core/SkFont.cpp"],
                "in_my_attention": True,
                "last_msg_author": "ext@example.com",
                "last_msg_date": "2026-11-03 11:00:00.000000000",
                "last_msg_snippet": "PTAL, thanks!",
                "open_external_threads": 0,
            }
            empty_gmail = {
                "thread_ids": [],
                "inbox_thread_ids": [],
                "total_msgs": 0,
                "inbox_msgs": 0,
                "trash_msgs": 0,
                "unread_msgs": 0,
            }
            # Polite nudge with 0 open external threads must not wake the chat even when idle
            seed_state_idle = dict(seed_state)
            seed_state_idle["items"] = {
                "gerrit:1409637@cid-1409637": seed_state["items"][
                    "gerrit:1409637@cid-1409637"
                ]
            }
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(seed_state_idle, f)

            with (
                mock.patch.object(w, "STATE_FILE", state_path),
                mock.patch.object(w, "fetch_gardener_snapshot", return_value=({}, [])),
                mock.patch.object(w, "discover_active_cl_chats", return_value=chatted),
                mock.patch.object(w, "discover_open_remote_changes", return_value=[]),
                mock.patch.object(w, "fetch_gerrit_cl_status", return_value=ping_info),
                mock.patch.object(w, "fetch_gmail_status_for_id", return_value=empty_gmail),
                mock.patch.object(
                    audit_gate, "check_audit_preflight", return_value={"ok": True, "reason": ""}
                ),
                mock.patch.object(w, "wake_conversation", return_value=True) as mock_wake,
            ):
                w.sync_once(wake_chats=True, update_titles=False, skia_dir="/nonexistent")
            mock_wake.assert_not_called()

            # Code change PS2 with 2 busy review chats is throttled, then wakes when free
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(seed_state, f)
            ps2_code_info = {
                **ping_info,
                "patchset": 2,
                "revision": "sha_ps2_8888888",
                "revisions_by_ps": {
                    1: {"sha": "sha_ps1_9999999", "kind": "REWORK", "parent": "p1"},
                    2: {"sha": "sha_ps2_8888888", "kind": "REWORK", "parent": "p1"},
                },
                "last_msg_date": "2026-11-03 12:30:00.000000000",
                "last_msg_snippet": "Uploaded patch set 2.",
            }
            with (
                mock.patch.object(w, "STATE_FILE", state_path),
                mock.patch.object(w, "fetch_gardener_snapshot", return_value=({}, [])),
                mock.patch.object(w, "discover_active_cl_chats", return_value=chatted),
                mock.patch.object(w, "discover_open_remote_changes", return_value=[]),
                mock.patch.object(w, "fetch_gerrit_cl_status", return_value=ps2_code_info),
                mock.patch.object(w, "fetch_gmail_status_for_id", return_value=empty_gmail),
                mock.patch.object(
                    audit_gate, "check_audit_preflight", return_value={"ok": True, "reason": ""}
                ),
                mock.patch.object(w, "wake_conversation", return_value=True) as mock_wake2,
            ):
                st_throttled = w.sync_once(
                    wake_chats=True, update_titles=False, skia_dir="/nonexistent"
                )
            mock_wake2.assert_not_called()
            self.assertNotIn("gerrit:1409637:2", st_throttled["review_claims"])

            # Next poll has 0 busy review chats (2001/2002 dropped) -> PS2 audit wakes immediately
            with (
                mock.patch.object(w, "STATE_FILE", state_path),
                mock.patch.object(w, "fetch_gardener_snapshot", return_value=({}, [])),
                mock.patch.object(w, "discover_active_cl_chats", return_value=chatted),
                mock.patch.object(w, "discover_open_remote_changes", return_value=[]),
                mock.patch.object(w, "fetch_gerrit_cl_status", return_value=ps2_code_info),
                mock.patch.object(w, "fetch_gmail_status_for_id", return_value=empty_gmail),
                mock.patch.object(
                    audit_gate, "check_audit_preflight", return_value={"ok": True, "reason": ""}
                ),
                mock.patch.object(w, "wake_conversation", return_value=True) as mock_wake3,
            ):
                st_woken = w.sync_once(
                    wake_chats=True, update_titles=False, skia_dir="/nonexistent"
                )
            mock_wake3.assert_called_once()
            self.assertIn("[FLIGHT-DECK AUDIT · Tier 2]", mock_wake3.call_args[0][1])
            self.assertIn("gerrit:1409637:2", st_woken["review_claims"])

    def test_save_gerrit_draft_endpoint_dedups_patchset_level_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "cl_watcher_state.json")
            triage_dir = os.path.join(tmp, "triage_results")
            ts.save_review_verdict(
                target_id="1414016",
                kind="gerrit",
                patchset=2,
                outcome="NITS",
                summary="Unused parameter warning in Paragraph.h",
                draft="Please leave the unused parameter unnamed in Paragraph.h.",
                corpus="not_required",
                revision="abcd123456",
                verify_attestation=False,
                results_dir=triage_dir,
            )
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "items": {
                            "gerrit:1414016@cid-1": {
                                "item_key": "gerrit:1414016",
                                "kind": "gerrit",
                                "target_id": "1414016",
                                "host": "skia-review.googlesource.com",
                                "patchset": 2,
                            }
                        }
                    },
                    f,
                )

            calls: list[tuple[str, str, dict]] = []

            def fake_urlopen(req, timeout=15):
                method = req.get_method()
                url = req.full_url
                body = json.loads(req.data.decode("utf-8")) if req.data else {}
                calls.append((method, url, body))
                if method == "GET":
                    return _gerrit_json_resp(
                        {
                            "/PATCHSET_LEVEL": [
                                {
                                    "id": "draft_existing_42",
                                    "path": "/PATCHSET_LEVEL",
                                    "message": "Old stale draft",
                                    "unresolved": True,
                                }
                            ]
                        }
                    )
                return _gerrit_json_resp(
                    {
                        "id": "draft_existing_42",
                        "path": "/PATCHSET_LEVEL",
                        "message": body.get("message", ""),
                    }
                )

            with (
                mock.patch.object(w, "STATE_FILE", state_path),
                mock.patch.object(w.canary_advisor, "_gerrit_luci_token", return_value="tok-123"),
                mock.patch.object(w.urllib.request, "urlopen", side_effect=fake_urlopen),
            ):
                res = w.save_gerrit_draft_for_item("gerrit:1414016")

            self.assertTrue(res["ok"])
            self.assertTrue(res["updated_existing"])
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][0], "GET")
            self.assertIn("/a/changes/1414016/revisions/2/drafts", calls[0][1])
            self.assertEqual(calls[1][0], "PUT")
            self.assertTrue(calls[1][1].endswith("/drafts/draft_existing_42"))
            self.assertEqual(
                calls[1][2]["message"],
                "Please leave the unused parameter unnamed in Paragraph.h.",
            )
            self.assertTrue(calls[1][2]["unresolved"])

    def test_flight_deck_ui_renders_auditing_blocked_auth_core_hunks_and_save_draft(self):
        js = """
if (typeof document === 'undefined') {
  globalThis.document = {
    _html: '',
    getElementById(id) {
      if (id === 'triage-modal-root') {
        const self = this;
        return { set innerHTML(v) { self._html = v; }, get innerHTML() { return self._html; } };
      }
      return null;
    }
  };
}
const baseItem = {
  item_key: 'gerrit:1414017',
  target_id: '1414017',
  kind: 'gerrit',
  host: 'skia-review.googlesource.com',
  role: 'reviewer (ext)',
  status: 'NEW',
  phase: 'NEEDS_YOUR_ACTION',
  patchset: 2,
  reviewed_ps: 1,
  chat_busy: true,
  url: 'https://skia-review.googlesource.com/c/skia/+/1414017',
  subject: '[skparagraph] Shift trailing space',
  project_name: 'SkParagraph',
  owner_name: 'David',
  owner_email: 'david@example.com',
  is_external: true,
};

const auditingItem = Object.assign({}, baseItem, {
  triage: {
    verdict: 'SURGICAL_AUDIT',
    audit_state: 'running',
    audit_target_patchset: 2,
    triaged_patchset: 1,
    summary: 'Previous PS1 summary',
  }
});

const crashedIdleItem = Object.assign({}, auditingItem, {
  chat_busy: false,
});

const blockedAuthItem = Object.assign({}, baseItem, {
  chat_busy: false,
  triage: {
    verdict: 'SURGICAL_AUDIT',
    audit_state: 'blocked_auth',
    audit_target_patchset: 2,
    audit_error: 'LOAS expired (run gcert)',
    triaged_patchset: 1,
  }
});

const richDoneItem = Object.assign({}, baseItem, {
  chat_busy: false,
  reviewed_ps: 2,
  triage: {
    verdict: 'NITS',
    review_outcome: 'NITS',
    audit_state: 'done',
    delta_kind: 'rebase_only',
    triaged_patchset: 2,
    triaged_at_et: 'Nov 03, 12:15 ET',
    summary: 'Unnamed unused parameter needed in Paragraph.h',
    draft: 'Please rename parameter in Paragraph.h.',
    report_markdown:
      'See Gerrit 1409697 PS5 and ' +
      'https://skia-review.googlesource.com/c/skia/+/1414017/2 for b/580882306.',
    core_hunks: [
      {file: 'modules/skparagraph/src/Run.cpp', lines: '180-195', note: 'RTL shift calculation'}
    ],
    open_thread_verdicts: [
      {author: 'Alexis', loc: 'Run.cpp:184', verdict: 'valid', note: 'Guard empty cluster range'}
    ]
  }
});

const ghDoneItem = Object.assign({}, richDoneItem, {
  item_key: 'github:195366',
  target_id: '195366',
  kind: 'github',
  url: 'https://github.com/flutter/flutter/pull/195366',
});

cachedData = {items: {'gerrit:1414017': richDoneItem, 'github:195366': ghDoneItem}};
currentTriageModalTab = 'report';
renderTriageModal(richDoneItem);
const modalReportHtml = document._html;

console.log(JSON.stringify({
  auditingCard: focusCard(auditingItem, {jetski_box_url: ''}),
  crashedIdleCard: focusCard(crashedIdleItem, {jetski_box_url: ''}),
  blockedCard: focusCard(blockedAuthItem, {jetski_box_url: ''}),
  richCard: focusCard(richDoneItem, {jetski_box_url: ''}),
  ghCard: focusCard(ghDoneItem, {jetski_box_url: ''}),
  modalReportHtml,
}));
"""
        out = self._eval_js(js)
        self.assertIn("Auditing PS2", out["auditingCard"])
        self.assertNotIn("Auditing PS2", out["crashedIdleCard"])
        self.assertIn("Audit paused: run gcert", out["blockedCard"])
        self.assertIn("rebase-only", out["richCard"])
        self.assertIn(
            "https://skia-review.googlesource.com/c/skia/+/1414017/2/"
            "modules/skparagraph/src/Run.cpp#180",
            out["richCard"],
        )
        self.assertIn("Save to Gerrit Draft", out["richCard"])
        self.assertNotIn("Save to Gerrit Draft", out["ghCard"])
        self.assertIn("Run.cpp:184", out["modalReportHtml"])
        self.assertIn("Guard empty cluster range", out["modalReportHtml"])
        self.assertIn('href="https://b.corp.google.com/issues/580882306"', out["modalReportHtml"])
        self.assertIn(
            'href="https://skia-review.googlesource.com/c/1409697/5"',
            out["modalReportHtml"],
        )

    def test_all_repo_github_pr_discovery_and_repo_slug_fetch(self):
        queries_seen: list[str] = []

        def fake_check_output(cmd, **kwargs):
            if cmd[:4] == ["gh", "api", "-X", "GET"] and "search/issues" in cmd:
                q_arg = next((a[2:] for a in cmd if a.startswith("q=")), "")
                queries_seen.append(q_arg)
                if "author:Rusino" in q_arg and "-author:Rusino" not in q_arg:
                    return json.dumps(
                        {
                            "items": [
                                {
                                    "number": 42,
                                    "title": "Sync TS paragraph clusters",
                                    "html_url": "https://github.com/Rusino/paragraph/pull/42",
                                }
                            ]
                        }
                    )
                return json.dumps({"items": []})
            if cmd[:3] == ["gh", "pr", "view"]:
                self.assertNotIn("closingIssuesReferences", ",".join(cmd))
                repo_idx = cmd.index("--repo")
                self.assertEqual(cmd[repo_idx + 1], "Rusino/paragraph")
                return json.dumps(
                    {
                        "number": 42,
                        "title": "Sync TS paragraph clusters",
                        "body": "Fixes #12",
                        "state": "OPEN",
                        "isDraft": False,
                        "author": {"login": "Rusino"},
                        "headRefOid": "deadbeef1234",
                        "headRefName": "sync-ts",
                        "baseRefName": "main",
                        "reviewDecision": "",
                        "latestReviews": [],
                        "reviews": [],
                        "comments": [],
                        "commits": [],
                        "statusCheckRollup": [],
                        "labels": [],
                        "url": "https://github.com/Rusino/paragraph/pull/42",
                        "mergeable": "MERGEABLE",
                        "reviewRequests": [{"login": "colleague"}],
                        "createdAt": "2026-11-03T14:00:00Z",
                        "updatedAt": "2026-11-03T15:00:00Z",
                        "closedAt": None,
                        "files": [{"path": "src/Paragraph.ts"}],
                    }
                )
            raise AssertionError(f"Unexpected command: {cmd}")

        with (
            mock.patch.object(w, "_gerrit_http_get_json", return_value=[]),
            mock.patch.object(w.subprocess, "check_output", side_effect=fake_check_output),
        ):
            remote = w.discover_open_remote_changes(
                set(), proj_names={"p1": "WebParagraph"}, ignored_ids=set()
            )
            self.assertTrue(any(q == "is:open is:pr author:Rusino" for q in queries_seen))
            self.assertTrue(
                any(q == "is:open is:pr involves:Rusino -author:Rusino" for q in queries_seen)
            )
            self.assertEqual(len(remote), 1)
            self.assertEqual(remote[0]["repo_slug"], "Rusino/paragraph")

            pr_status = w.fetch_github_pr_status("42", repo_slug=remote[0]["repo_slug"])
            self.assertTrue(pr_status["ok"])
            self.assertEqual(pr_status["repo_slug"], "Rusino/paragraph")
            self.assertEqual(pr_status["host"], "github.com/Rusino/paragraph")
            self.assertEqual(pr_status["url"], "https://github.com/Rusino/paragraph/pull/42")

    def test_github_issue_mentions_own_latest_comment_suppression_and_ack_persistence(self):
        def fake_check_output(cmd, **kwargs):
            if cmd[:3] == ["gh", "issue", "list"]:
                return "[]"
            if cmd[:4] == ["gh", "api", "-X", "GET"] and "search/issues" in cmd:
                return json.dumps(
                    {
                        "items": [
                            {
                                "number": 101,
                                "title": "Answered mention issue",
                                "html_url": "https://github.com/flutter/flutter/issues/101",
                                "updated_at": "2026-11-03T15:00:00Z",
                                "assignees": [],
                            },
                            {
                                "number": 102,
                                "title": "Unanswered mention issue",
                                "html_url": "https://github.com/flutter/flutter/issues/102",
                                "updated_at": "2026-11-03T16:00:00Z",
                                "assignees": [],
                            },
                        ]
                    }
                )
            if cmd[:2] == ["gh", "api"] and any("notifications" in a for a in cmd):
                return "[]"
            if cmd[:3] == ["gh", "api", "graphql"]:
                num_arg = next((a for a in cmd if a.startswith("num=")), "")
                if num_arg == "num=101":
                    return json.dumps(
                        {
                            "data": {
                                "repository": {
                                    "issue": {
                                        "number": 101,
                                        "title": "Answered mention issue",
                                        "url": "https://github.com/flutter/flutter/issues/101",
                                        "state": "OPEN",
                                        "createdAt": "2026-11-01T10:00:00Z",
                                        "updatedAt": "2026-11-03T15:00:00Z",
                                        "lastEditedAt": None,
                                        "editor": None,
                                        "author": {"login": "reporter1"},
                                        "body": "cc @Rusino please check",
                                        "assignees": {"nodes": []},
                                        "timelineItems": {
                                            "nodes": [
                                                {
                                                    "__typename": "IssueComment",
                                                    "author": {"login": "Rusino"},
                                                    "body": "Fixed in main.",
                                                    "createdAt": "2026-11-03T14:00:00Z",
                                                    "updatedAt": "2026-11-03T14:00:00Z",
                                                },
                                                {
                                                    "__typename": "IssueComment",
                                                    "author": {"login": "fluttergithubbot"},
                                                    "body": "Automated label update",
                                                    "createdAt": "2026-11-03T15:00:00Z",
                                                    "updatedAt": "2026-11-03T15:00:00Z",
                                                },
                                            ]
                                        },
                                    }
                                }
                            }
                        }
                    )
                if num_arg == "num=102":
                    return json.dumps(
                        {
                            "data": {
                                "repository": {
                                    "issue": {
                                        "number": 102,
                                        "title": "Unanswered mention issue",
                                        "url": "https://github.com/flutter/flutter/issues/102",
                                        "state": "OPEN",
                                        "createdAt": "2026-11-01T10:00:00Z",
                                        "updatedAt": "2026-11-03T16:00:00Z",
                                        "lastEditedAt": None,
                                        "editor": None,
                                        "author": {"login": "reporter2"},
                                        "body": "Initial report",
                                        "assignees": {"nodes": []},
                                        "timelineItems": {
                                            "nodes": [
                                                {
                                                    "__typename": "IssueComment",
                                                    "author": {"login": "Rusino"},
                                                    "body": "Can you share a repro?",
                                                    "createdAt": "2026-11-03T14:00:00Z",
                                                    "updatedAt": "2026-11-03T14:00:00Z",
                                                },
                                                {
                                                    "__typename": "IssueComment",
                                                    "author": {"login": "reporter2"},
                                                    "body": "@Rusino attached minimal reproducer!",
                                                    "createdAt": "2026-11-03T16:00:00Z",
                                                    "updatedAt": "2026-11-03T16:00:00Z",
                                                },
                                            ]
                                        },
                                    }
                                }
                            }
                        }
                    )
            raise AssertionError(f"Unexpected command: {cmd}")

        def fake_run(cmd, *args, **kwargs):
            return w.subprocess.CompletedProcess(cmd, 0, fake_check_output(cmd), "")

        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "cl_watcher_state.json")
            state = {"items": {}, "bugs": {}, "events": []}
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f)

            with (
                mock.patch.object(w, "STATE_FILE", state_path),
                mock.patch.object(w.subprocess, "run", side_effect=fake_run),
                mock.patch.object(w.subprocess, "check_output", side_effect=fake_check_output),
                mock.patch.object(w, "call_buganizer_mcp_batch", return_value=["", ""]),
                mock.patch.object(w, "fetch_buganizer_fallback", return_value=[]),
                mock.patch.object(w, "discover_bug_chats", return_value={}),
                mock.patch.object(w, "_start_chat_for_bug") as mock_start_bug,
            ):
                w.sync_bugs_into_state(
                    state, force=True, wake_chats=True, proj_names={"p1": "SkParagraph"}
                )
                w.save_watcher_state(state)

                # Answered issue #101 is suppressed; unanswered #102 surfaces without auto-chat
                self.assertNotIn("gh_issue:flutter/flutter#101", state["bugs"])
                self.assertIn("gh_issue:flutter/flutter#102", state["bugs"])
                b102 = state["bugs"]["gh_issue:flutter/flutter#102"]
                self.assertTrue(b102["needs_you"])
                self.assertEqual(b102["lane"], "you")
                self.assertIn("Mentioned", b102["why_needs_you"])
                self.assertIn("reporter2", b102["why_needs_you"])
                mock_start_bug.assert_not_called()

                # Acking #102 hides it from Needs you and persists across subsequent syncs
                ack_res = w.ack_bug_item("gh_issue:flutter/flutter#102")
                self.assertTrue(ack_res["ok"])
                reloaded = w.load_watcher_state()
                w.sync_bugs_into_state(
                    reloaded, force=True, wake_chats=True, proj_names={"p1": "SkParagraph"}
                )
                self.assertNotIn("gh_issue:flutter/flutter#102", reloaded["bugs"])


if __name__ == "__main__":
    unittest.main()
