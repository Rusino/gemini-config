#!/usr/bin/env python3
"""Contract tests for R28/R28.2 external Canary-G3 automation in cl_watcher.py."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts"
)
CANARY_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "skills",
    "skia-canary-advisor",
    "scripts",
)
for p in (CANARY_DIR, SCRIPTS_DIR):
  if p not in sys.path:
    sys.path.insert(0, p)

import canary_advisor  # pylint: disable=g-import-not-at-top
import cl_watcher as w  # pylint: disable=g-import-not-at-top


HOLD_TEXT = (
    "The change looks good to me (Code-Review+1). I leave this comment unresolved "
    "on purpose: it blocks the submit until the Canary-G3 try job passes. Canary-G3 "
    "builds and tests the google3 clients of Skia with this change. Skia starts it "
    "for an external author only after a Googler votes Code-Review+1, so I vote "
    "first and hold the submit with this comment."
)


class _FakeHttpResp:

  def __init__(self, payload: str, status: int = 200):
    self._payload = payload
    self.status = status

  def read(self) -> bytes:
    return self._payload.encode("utf-8")

  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc, tb):
    return False


class ClWatcherCanaryG3Test(unittest.TestCase):

  def test_gerrit_reads_fallback_to_authenticated_endpoint_on_http_429(self):
    seen = []

    def fake_urlopen(req, timeout=8):
      seen.append((req.full_url, req.headers.get("Authorization")))
      if "/a/" not in req.full_url:
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {}, None
        )
      return _FakeHttpResp(
          ")]}'\n{\"/PATCHSET_LEVEL\": [{\"id\": \"c1\", \"patch_set\": 1,"
          " \"unresolved\": true, \"author\": {\"email\": \"jlavrova@google.com\"},"
          f" \"message\": \"{HOLD_TEXT}\"}}]" + "}"
      )

    with patch.object(
        canary_advisor, "_gerrit_luci_token", return_value="luci-tok-123"
    ):
      with patch.object(
          w.urllib.request, "urlopen", side_effect=fake_urlopen
      ):
        comments = w.fetch_gerrit_comments(w.GERRIT_SKIA_HOST, "1414017")

    self.assertIn("/PATCHSET_LEVEL", comments)
    self.assertEqual(len(seen), 2)
    self.assertNotIn("/a/", seen[0][0])
    self.assertIn("/a/changes/1414017/comments", seen[1][0])
    self.assertEqual(seen[1][1], "Bearer luci-tok-123")

  def test_fetch_gerrit_cl_status_detects_own_canary_g3_submit_hold_only(self):
    def _detail_payload(comments_dict):
      return {
          "status": "NEW",
          "project": "skia",
          "subject": "[skparagraph] Shift trailing space",
          "owner": {"email": "dbebawy@gmail.com", "name": "David"},
          "current_revision": "sha1",
          "revisions": {
              "sha1": {
                  "_number": 1,
                  "commit": {"message": "Subject\n", "parents": []},
                  "files": {"modules/skparagraph/src/TextLine.cpp": {}},
              }
          },
          "labels": {
              "Code-Review": {
                  "all": [{"email": "jlavrova@google.com", "value": 1}]
              },
              "Commit-Queue": {"all": []},
          },
          "reviewers": {
              "REVIEWER": [{"email": "jlavrova@google.com"}],
              "CC": [],
          },
          "submit_requirements": [
              {"name": "No-Unresolved-Comments", "status": "UNSATISFIED"}
          ],
          "messages": [
              {
                  "_revision_number": 1,
                  "author": {"email": "jlavrova@google.com"},
                  "message": "Patch Set 1: Code-Review+1\n\n(1 comment)",
                  "date": "2026-11-02 22:00:00.000000000",
              }
          ],
          "_comments": comments_dict,
      }

    hold_comments = {
        "/PATCHSET_LEVEL": [{
            "id": "068f541c_51cd2ab8",
            "patch_set": 1,
            "unresolved": True,
            "updated": "2026-11-02 22:00:00.000000000",
            "author": {"email": "jlavrova@google.com"},
            "message": HOLD_TEXT,
        }]
    }
    substantive_comments = {
        "/PATCHSET_LEVEL": [{
            "id": "deadbeef_00000001",
            "patch_set": 1,
            "unresolved": True,
            "updated": "2026-11-02 22:00:00.000000000",
            "author": {"email": "jlavrova@google.com"},
            "message": "Please guard against empty runs before indexing.",
        }]
    }

    with patch.object(w, "gerrit_mergeable", return_value=True):
      with patch.object(
          w.urllib.request,
          "urlopen",
          return_value=_FakeHttpResp(
              ")]}'\n" + w.json.dumps(_detail_payload(hold_comments))
          ),
      ):
        st_hold = w.fetch_gerrit_cl_status(
            "1414017", {}, [], preferred_host=w.GERRIT_SKIA_HOST
        )
      with patch.object(
          w.urllib.request,
          "urlopen",
          return_value=_FakeHttpResp(
              ")]}'\n" + w.json.dumps(_detail_payload(substantive_comments))
          ),
      ):
        st_sub = w.fetch_gerrit_cl_status(
            "1414017", {}, [], preferred_host=w.GERRIT_SKIA_HOST
        )

    self.assertTrue(st_hold["has_own_canary_g3_hold"])
    self.assertFalse(st_sub["has_own_canary_g3_hold"])

  def test_sync_external_canary_g3_triggers_once_and_auto_resolves_hold_on_success(self):
    rec = {
        "kind": "gerrit",
        "host": w.GERRIT_SKIA_HOST,
        "item_key": "gerrit:1414016",
        "target_id": "1414016",
        "status": "NEW",
        "patchset": 1,
        "subject": "Fix justification run boundary",
        "owner_email": "dbebawy@gmail.com",
        "is_self": False,
        "is_companion": False,
        "is_wip": False,
        "reviewer_removed": False,
        "self_cr_vote": 1,
        "has_own_canary_g3_hold": True,
        "conversation_id": "conv-1414016",
        "project_name": "Reviews",
        "files": ["modules/skparagraph/src/TextLine.cpp"],
    }
    items = {"gerrit:1414016@conv-1414016": rec}
    state = {"events": [], "canary_g3_ledger": {}}
    woken = []

    with tempfile.TemporaryDirectory() as tmp:
      claims_file = os.path.join(tmp, "claims.json")
      with patch.dict(os.environ, {"CANARY_G3_CLAIMS_FILE": claims_file}):
        with patch.object(
            w, "wake_conversation", side_effect=lambda c, m: woken.append((c, m)) or True
        ):
          # Tick 1 (t=1000): triggers Canary-G3 once
          with patch.object(
              canary_advisor,
              "trigger_canary_g3_once",
              return_value={"ok": True, "triggered": True, "claim": {"status": "triggered"}},
          ) as mock_trig:
            w.sync_external_canary_g3(
                items, state, skia_dir="/tmp/skia", now_ts=1000.0
            )
          self.assertEqual(mock_trig.call_count, 1)
          self.assertEqual(len(state["events"]), 1)
          self.assertIn("Canary-G3", state["events"][0]["message"])

          # Tick 2 (t=1020, <60s): throttled, neither triggers nor polls Buildbucket
          with patch.object(
              canary_advisor, "trigger_canary_g3_once"
          ) as mock_trig2:
            with patch.object(canary_advisor, "bb_cl_builds") as mock_bb_early:
              w.sync_external_canary_g3(
                  items, state, skia_dir="/tmp/skia", now_ts=1020.0
              )
          mock_trig2.assert_not_called()
          mock_bb_early.assert_not_called()

          # Tick 3 (t=1070, >=60s): Canary-G3 + ASAN are SUCCESS -> resolves own hold & wakes chat
          builds_ok = {
              "Canary-G3": {"id": "8666214344502815105", "status": "SUCCESS"},
              "Test-Ubuntu24.04-Clang-ASAN": {"id": "8666210000", "status": "SUCCESS"},
          }
          with patch.object(canary_advisor, "bb_cl_builds", return_value=builds_ok):
            with patch.object(
                canary_advisor,
                "resolve_canary_g3_holds",
                return_value=[{
                    "cl": 1414016,
                    "tested_ps": 1,
                    "current_ps": 1,
                    "resolved": True,
                    "resolved_leaf_ids": ["hold_leaf_1"],
                    "message": "Canary-G3 passed; resolving submit hold.",
                }],
            ) as mock_resolve:
              w.sync_external_canary_g3(
                  items, state, skia_dir="/tmp/skia", now_ts=1070.0
              )
          mock_resolve.assert_called_once_with(1414016, 1)
          self.assertEqual(len(woken), 1)
          self.assertEqual(woken[0][0], "conv-1414016")
          self.assertIn(
              "https://skia-review.googlesource.com/c/skia/+/1414016/1",
              woken[0][1],
          )
          self.assertIn(
              "https://ci.chromium.org/b/8666214344502815105", woken[0][1]
          )
          self.assertTrue(state["canary_g3_ledger"]["1414016:1"]["settled"])

          # Tick 4 (t=1200): already settled -> zero Buildbucket calls, zero duplicate wakeups
          with patch.object(canary_advisor, "bb_cl_builds") as mock_bb_after:
            w.sync_external_canary_g3(
                items, state, skia_dir="/tmp/skia", now_ts=1200.0
            )
          mock_bb_after.assert_not_called()
          self.assertEqual(len(woken), 1)

  def test_sync_external_canary_g3_adopts_failure_keeps_hold_and_reports_once(self):
    rec = {
        "kind": "gerrit",
        "host": w.GERRIT_SKIA_HOST,
        "item_key": "gerrit:1414017",
        "target_id": "1414017",
        "status": "NEW",
        "patchset": 1,
        "subject": "[skparagraph] Shift the trailing space of an RTL run when justifying",
        "owner_email": "dbebawy@gmail.com",
        "is_self": False,
        "is_companion": False,
        "is_wip": False,
        "reviewer_removed": False,
        "self_cr_vote": 1,
        "has_own_canary_g3_hold": True,
        "conversation_id": "conv-1414017",
        "project_name": "Reviews",
        "files": ["modules/skparagraph/src/TextLine.cpp"],
    }
    items = {"gerrit:1414017@conv-1414017": rec}
    state = {"events": [], "canary_g3_ledger": {}}
    woken = []
    failed_builds = {
        "Canary-G3": {"id": "8666208547771532737", "status": "FAILURE"},
        "Test-Ubuntu24.04-Clang-ASAN": {"id": "8666200000", "status": "SUCCESS"},
    }

    with patch.object(
        w, "wake_conversation", side_effect=lambda c, m: woken.append((c, m)) or True
    ):
      with patch.object(
          canary_advisor,
          "trigger_canary_g3_once",
          return_value={
              "ok": True,
              "triggered": False,
              "adopted": True,
              "build": failed_builds["Canary-G3"],
              "claim": {"status": "adopted"},
          },
      ):
        with patch.object(canary_advisor, "bb_cl_builds", return_value=failed_builds):
          with patch.object(canary_advisor, "swarming_tasks_for_cl", return_value=[]):
            with patch.object(
                canary_advisor,
                "triage_canary_g3_failure",
                return_value={
                    "verdict": "CL_SUSPECTED_REGRESSION",
                    "g3_cl": "1012433408",
                    "our_part_passed": False,
                },
            ):
              with patch.object(canary_advisor, "resolve_canary_g3_holds") as mock_resolve:
                w.sync_external_canary_g3(
                    items, state, skia_dir="/tmp/skia", now_ts=2000.0
                )

    mock_resolve.assert_not_called()
    self.assertEqual(len(woken), 1)
    self.assertIn("CL_SUSPECTED_REGRESSION", woken[0][1])
    self.assertIn("http://cl/1012433408", woken[0][1])
    self.assertIn(
        "https://skia-review.googlesource.com/c/skia/+/1414017/1", woken[0][1]
    )
    self.assertTrue(state["canary_g3_ledger"]["1414017:1"]["settled"])

  def test_sync_external_canary_g3_ignores_internal_or_without_hold_comment(self):
    items = {
        "gerrit:100@c1": {
            "kind": "gerrit",
            "host": w.GERRIT_SKIA_HOST,
            "item_key": "gerrit:100",
            "target_id": "100",
            "status": "NEW",
            "patchset": 1,
            "owner_email": "colleague@google.com",
            "self_cr_vote": 1,
            "has_own_canary_g3_hold": True,
        },
        "gerrit:101@c2": {
            "kind": "gerrit",
            "host": w.GERRIT_SKIA_HOST,
            "item_key": "gerrit:101",
            "target_id": "101",
            "status": "NEW",
            "patchset": 1,
            "owner_email": "external@gmail.com",
            "self_cr_vote": 1,
            "has_own_canary_g3_hold": False,
        },
    }
    state = {"events": [], "canary_g3_ledger": {}}
    with patch.object(canary_advisor, "trigger_canary_g3_once") as mock_trig:
      w.sync_external_canary_g3(
          items, state, skia_dir="/tmp/skia", now_ts=3000.0
      )
    mock_trig.assert_not_called()

  def test_sync_external_canary_g3_isolates_transient_rpc_error_per_cl(self):
    items = {
        "gerrit:1414016@c1": {
            "kind": "gerrit",
            "host": w.GERRIT_SKIA_HOST,
            "item_key": "gerrit:1414016",
            "target_id": "1414016",
            "status": "NEW",
            "patchset": 1,
            "subject": "First CL",
            "owner_email": "ext@gmail.com",
            "self_cr_vote": 1,
            "has_own_canary_g3_hold": True,
            "conversation_id": "c1",
            "project_name": "Reviews",
        },
        "gerrit:1414017@c2": {
            "kind": "gerrit",
            "host": w.GERRIT_SKIA_HOST,
            "item_key": "gerrit:1414017",
            "target_id": "1414017",
            "status": "NEW",
            "patchset": 1,
            "subject": "Second CL",
            "owner_email": "ext@gmail.com",
            "self_cr_vote": 1,
            "has_own_canary_g3_hold": True,
            "conversation_id": "c2",
            "project_name": "Reviews",
        },
    }
    state = {"events": [], "canary_g3_ledger": {}}

    def fake_trig(cl_num, ps, **_kwargs):
      if cl_num == 1414016:
        raise canary_advisor.urllib.error.URLError("transient gerrit 502")
      return {"ok": True, "triggered": True, "claim": {"status": "triggered"}}

    with patch.object(canary_advisor, "trigger_canary_g3_once", side_effect=fake_trig):
      w.sync_external_canary_g3(
          items, state, skia_dir="/tmp/skia", now_ts=4000.0
      )
    self.assertTrue(
        state["canary_g3_ledger"]["1414017:1"]["triggered_or_claimed"]
    )
    self.assertFalse(
        state["canary_g3_ledger"]["1414016:1"]["triggered_or_claimed"]
    )
    self.assertEqual(state["canary_g3_ledger"]["1414016:1"]["last_poll_ts"], 0.0)


if __name__ == "__main__":
  unittest.main()
