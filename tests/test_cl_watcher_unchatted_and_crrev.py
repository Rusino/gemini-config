#!/usr/bin/env python3
"""Contract tests for crrev/skrev title parsing, unchatted remote discovery, and Start chat."""

import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


def _http_json_resp(payload) -> mock.MagicMock:
  raw = (")]}'\n" + json.dumps(payload)).encode("utf-8")
  resp = mock.MagicMock()
  resp.read.return_value = raw
  resp.__enter__.return_value = resp
  resp.__exit__.return_value = False
  return resp


class ClWatcherUnchattedAndCrrevTest(unittest.TestCase):

  def _make_ls_summary(self, title: str, project_id: str, last_mod: str) -> dict:
    return {
        "annotations": {"title": title, "archived": False},
        "trajectoryMetadata": {
            "projectId": project_id,
            "lastModifiedTime": last_mod,
            "parentConversationId": "",
            "isBattleModeFork": False,
            "sourceMetadata": None,
        },
    }

  def test_re_cl_id_matches_crrev_and_skrev_shortlinks(self):
    summaries = {
        "c-crrev": self._make_ls_summary(
            "[11:53] ⦿ crrev/c/7686597 Linux text gamma & LCD",
            "proj-cr",
            "2026-10-29T15:53:00Z",
        ),
        "c-skrev": self._make_ls_summary(
            "[10:15] ⦿ skrev.com/1402416 SkContext reland",
            "proj-skia",
            "2026-10-29T14:15:00Z",
        ),
        "c-multi": self._make_ls_summary(
            "[12:30] ⦿ crrev/c/8663038 / 8663538 ShapedText split",
            "proj-tc",
            "2026-10-29T16:30:00Z",
        ),
        "c-space": self._make_ls_summary(
            "[14:10] ⦿ cl/1409697 1410416 indent fix",
            "proj-skia",
            "2026-10-29T14:10:00Z",
        ),
        "c-pr-group": self._make_ls_summary(
            "[10:00] ⦿ PR #195122 / #195384 WebParagraph update",
            "proj-wp",
            "2026-10-29T15:00:00Z",
        ),
        "c-bug": self._make_ls_summary(
            "[15:17] ⦿ cl/1404136 b/557269199 RenameFont OOB",
            "proj-skia",
            "2026-10-29T15:17:00Z",
        ),
    }
    proj_names = {
        "proj-cr": "Chromium",
        "proj-skia": "Skia",
        "proj-tc": "TextClusters API",
        "proj-wp": "WebParagraph",
    }
    with mock.patch.object(
        w.cl, "_fetch_ls_summaries_for_audit", return_value=summaries
    ), mock.patch.object(w, "load_project_names", return_value=proj_names):
      discovered = w.discover_active_cl_chats()

    by_key = {d["item_key"]: d for d in discovered}
    self.assertIn("gerrit:7686597", by_key)
    self.assertEqual(by_key["gerrit:7686597"]["conversation_id"], "c-crrev")
    self.assertEqual(
        by_key["gerrit:7686597"].get("preferred_host"),
        "chromium-review.googlesource.com",
    )
    self.assertTrue(by_key["gerrit:7686597"]["single_target"])

    self.assertIn("gerrit:1402416", by_key)
    self.assertEqual(
        by_key["gerrit:1402416"].get("preferred_host"),
        "skia-review.googlesource.com",
    )

    self.assertIn("gerrit:8663038", by_key)
    self.assertFalse(by_key["gerrit:8663038"]["single_target"])
    self.assertIn("gerrit:8663538", by_key)
    self.assertEqual(by_key["gerrit:8663538"]["conversation_id"], "c-multi")
    self.assertEqual(
        by_key["gerrit:8663538"].get("preferred_host"),
        "chromium-review.googlesource.com",
    )
    self.assertFalse(by_key["gerrit:8663538"]["single_target"])

    self.assertIn("gerrit:1409697", by_key)
    self.assertIn("gerrit:1410416", by_key)
    self.assertEqual(by_key["gerrit:1409697"]["conversation_id"], "c-space")
    self.assertEqual(by_key["gerrit:1410416"]["conversation_id"], "c-space")
    self.assertFalse(by_key["gerrit:1409697"]["single_target"])

    self.assertIn("github:195122", by_key)
    self.assertIn("github:195384", by_key)
    self.assertEqual(by_key["github:195122"]["conversation_id"], "c-pr-group")
    self.assertEqual(by_key["github:195384"]["conversation_id"], "c-pr-group")
    self.assertFalse(by_key["github:195122"]["single_target"])

    self.assertIn("gerrit:1404136", by_key)
    self.assertTrue(by_key["gerrit:1404136"]["single_target"])
    self.assertNotIn("piper:557269199", by_key)
    self.assertNotIn("gerrit:557269199", by_key)

    # Roll badge truncation must preserve crrev/c/<id> intact
    calls = []
    with mock.patch.object(
        w.cl, "set_conversation_topic", side_effect=lambda _c, t: calls.append(t)
    ):
      w.update_chat_roll_badge(
          "c-crrev",
          "[11:53] ⦿ crrev/c/7686597 Linux text gamma & LCD rendering",
          "[⏳St · 0/4 rolls]",
      )
    self.assertEqual(len(calls), 1)
    self.assertIn("crrev/c/7686597", calls[0])
    self.assertLessEqual(len(calls[0]), w.TITLE_TOPIC_MAX)

  def test_fetch_gerrit_cl_status_does_not_fall_through_on_transient_timeout(self):
    probed_urls = []

    def fake_urlopen_timeout(req, timeout=8):
      url = req.full_url if hasattr(req, "full_url") else str(req)
      probed_urls.append(url)
      raise TimeoutError("skia-review timed out")

    with mock.patch.object(
        w.urllib.request, "urlopen", side_effect=fake_urlopen_timeout
    ):
      res = w.fetch_gerrit_cl_status("1404136", {}, [], skia_dir="/nonexistent")

    # Transient failure on Skia Gerrit must not fall through to a 2019 Chromium CL
    self.assertFalse(res["ok"])
    self.assertEqual(len(probed_urls), 1)
    self.assertIn("skia-review.googlesource.com", probed_urls[0])

    probed_inc = []

    def fake_urlopen_incomplete(req, timeout=8):
      url = req.full_url if hasattr(req, "full_url") else str(req)
      probed_inc.append(url)
      raise http.client.IncompleteRead(b"part")

    with mock.patch.object(
        w.urllib.request, "urlopen", side_effect=fake_urlopen_incomplete
    ):
      res_inc = w.fetch_gerrit_cl_status("1404136", {}, [], skia_dir="/nonexistent")

    self.assertFalse(res_inc["ok"])
    self.assertEqual(len(probed_inc), 1)

    # Explicit 404 on first host still falls through to the second host
    probed_404 = []
    cr_detail = {
        "status": "NEW",
        "subject": "[canvas] Move TextMetrics shaping state into ShapedText",
        "owner": {"name": "Florin Malita", "email": "fmalita@chromium.org"},
        "current_revision": "rev8",
        "revisions": {"rev8": {"_number": 8}},
        "attention_set": {
            "1": {
                "account": {"email": "jlavrova@google.com"},
                "last_update": "2026-10-29 16:16:10.000000000",
            }
        },
        "reviewers": {"REVIEWER": [{"email": "jlavrova@google.com"}]},
        "messages": [],
    }

    def fake_urlopen_404_then_ok(req, timeout=8):
      url = req.full_url if hasattr(req, "full_url") else str(req)
      probed_404.append(url)
      if "skia-review.googlesource.com" in url:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))
      return _http_json_resp(cr_detail)

    with mock.patch.object(
        w.urllib.request, "urlopen", side_effect=fake_urlopen_404_then_ok
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res_ok = w.fetch_gerrit_cl_status(
          "8663038",
          {},
          [],
          skia_dir="/nonexistent",
          preferred_host="skia-review.googlesource.com",
      )

    self.assertTrue(res_ok["ok"])
    self.assertEqual(res_ok["host"], "chromium-review.googlesource.com")
    self.assertTrue(res_ok["in_my_attention"])

  def test_discover_open_remote_changes_skips_chatted_and_ignored_ids(self):
    skia_changes = [
        {"_number": 580176, "subject": "allow font rasterisation"},
        {"_number": 1408158, "subject": "[skparagraph] Use std::string"},
        {"_number": 1406897, "subject": "[skoretext 1/6] Add CoreText build flag"},
    ]
    cr_changes = [
        {"_number": 8663038, "subject": "[canvas] Move TextMetrics shaping state into ShapedText"},
        {"_number": 8663538, "subject": "[canvas] Move ShapedText into its own files"},
    ]
    gh_authored = {"items": [{"number": 195122, "title": "[web_paragraph] Avoid browser queries"}]}
    gh_involved = {"items": [{"number": 195384, "title": "[web_paragraph] Already chatted"}]}

    def fake_urlopen(req, timeout=10):
      url = req.full_url if hasattr(req, "full_url") else str(req)
      if "skia-review.googlesource.com" in url:
        return _http_json_resp(skia_changes)
      return _http_json_resp(cr_changes)

    def fake_check_output(cmd, **kwargs):
      q_arg = " ".join(cmd)
      if "author:Rusino" in q_arg and "-author:Rusino" not in q_arg:
        return json.dumps(gh_authored)
      return json.dumps(gh_involved)

    proj_names = {
        "p-tc": "TextClusters API",
        "p-skore": "SkoreText",
        "p-wp": "WebParagraph",
        "p-skia": "Skia",
    }
    with mock.patch.object(w.urllib.request, "urlopen", side_effect=fake_urlopen), \
         mock.patch.object(w.subprocess, "check_output", side_effect=fake_check_output):
      remote = w.discover_open_remote_changes(
          already_chatted_keys={"gerrit:1408158", "github:195384"},
          proj_names=proj_names,
          ignored_ids={"580176"},
      )

    by_key = {r["item_key"]: r for r in remote}
    self.assertEqual(
        set(by_key.keys()),
        {"gerrit:1406897", "gerrit:8663038", "gerrit:8663538", "github:195122"},
    )
    self.assertEqual(by_key["gerrit:8663038"]["project_name"], "TextClusters API")
    self.assertEqual(by_key["gerrit:8663038"]["conversation_id"], "")
    self.assertEqual(by_key["gerrit:1406897"]["project_name"], "SkoreText")
    self.assertEqual(by_key["github:195122"]["project_name"], "WebParagraph")

  def test_sync_once_tracks_unchatted_reviews_without_waking_empty_chat(self):
    remote = [
        {
            "item_key": "gerrit:8663038",
            "kind": "gerrit",
            "target_id": "8663038",
            "preferred_host": "chromium-review.googlesource.com",
            "conversation_id": "",
            "title": "",
            "project_id": "p-tc",
            "project_name": "TextClusters API",
            "chat_last_modified": "",
            "single_target": True,
            "chat_busy": False,
        }
    ]
    cl_info = {
        "ok": True,
        "host": "chromium-review.googlesource.com",
        "url": "https://chromium-review.googlesource.com/c/8663038",
        "status": "NEW",
        "phase": "NEEDS_YOUR_ACTION",
        "subject": "[canvas] Move TextMetrics shaping state into ShapedText",
        "owner_name": "Florin Malita",
        "owner_email": "fmalita@chromium.org",
        "role": "reviewer (goog)",
        "is_self": False,
        "is_external": False,
        "is_draft": False,
        "in_development": False,
        "is_watching": False,
        "reviewer_removed": False,
        "patchset": 8,
        "revision": "rev8888888",
        "in_my_attention": True,
        "waiting_on": [],
        "merge_conflict": False,
        "tree_red": False,
        "cr_summary": "0",
        "cq_status": "CQ+1 (Dry Run)",
        "cq_failed": False,
        "last_msg_author": "fmalita@chromium.org",
        "last_msg_date": "2026-10-29 16:16:10.000000000",
        "last_msg_et": "12:16 ET",
        "last_msg_age": "15m ago",
        "last_msg_snippet": "This change is ready for review.",
        "submitted": "",
        "submitted_et": "",
        "submitted_age": "",
        "status_changed_raw": "2026-10-29 16:16:10.000000000",
        "status_changed_et": "12:16 ET",
        "status_changed_age": "15m ago",
        "post_submit": None,
    }
    empty_gmail = {
        "thread_ids": [],
        "inbox_thread_ids": [],
        "total_msgs": 0,
        "inbox_msgs": 0,
        "trash_msgs": 0,
        "unread_msgs": 0,
    }
    with tempfile.TemporaryDirectory() as tmp:
      state_path = os.path.join(tmp, "state.json")
      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(w, "fetch_gardener_snapshot", return_value=({}, [])), \
           mock.patch.object(w, "discover_active_cl_chats", return_value=[]), \
           mock.patch.object(w, "discover_open_remote_changes", return_value=remote), \
           mock.patch.object(w, "fetch_gerrit_cl_status", return_value=cl_info), \
           mock.patch.object(w, "fetch_gmail_status_for_id", return_value=empty_gmail), \
           mock.patch.object(w, "wake_conversation") as mock_wake:
        state = w.sync_once(
            wake_chats=True, update_titles=True, skia_dir="/nonexistent"
        )

    self.assertIn("gerrit:8663038@unchatted", state["items"])
    rec = state["items"]["gerrit:8663038@unchatted"]
    self.assertEqual(rec["conversation_id"], "")
    self.assertTrue(rec["in_my_attention"])
    mock_wake.assert_not_called()

  def test_start_chat_for_item_creates_project_pinned_chat_and_rekeys_state(self):
    initial_state = {
        "items": {
            "gerrit:8663038@unchatted": {
                "item_key": "gerrit:8663038",
                "kind": "gerrit",
                "target_id": "8663038",
                "host": "chromium-review.googlesource.com",
                "url": "https://chromium-review.googlesource.com/c/8663038",
                "subject": "[canvas] Move TextMetrics shaping state into ShapedText",
                "owner_email": "fmalita@chromium.org",
                "is_self": False,
                "conversation_id": "",
                "project_id": "p-tc",
                "project_name": "TextClusters API",
                "composite_key": "gerrit:8663038@unchatted",
            }
        },
        "events": [],
        "last_sync": "12:30:00 ET",
    }
    with tempfile.TemporaryDirectory() as tmp:
      state_path = os.path.join(tmp, "state.json")
      with open(state_path, "w", encoding="utf-8") as f:
        json.dump(initial_state, f)

      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(w, "discover_active_cl_chats", return_value=[]), \
           mock.patch.object(w.cl, "resolve_project_id", return_value="p-tc"), \
           mock.patch.object(
               w.cl, "resolve_handoff_model", return_value={"enum": "MODEL_PRO"}
           ), \
           mock.patch.object(
               w.cl, "start_conversation_exact", return_value="new-cid-1234"
           ) as mock_start:
        res = w.start_chat_for_item("gerrit:8663038")
        with open(state_path, encoding="utf-8") as sf:
          saved = json.load(sf)

    self.assertTrue(res["ok"])
    self.assertEqual(res["conversation_id"], "new-cid-1234")
    self.assertTrue(res["chat_url"].endswith("/c/new-cid-1234"))
    mock_start.assert_called_once()
    call_kwargs = mock_start.call_args.kwargs
    self.assertEqual(call_kwargs["project_id"], "p-tc")
    self.assertIn("crrev/c/8663038", call_kwargs["title"])
    self.assertNotIn("gerrit:8663038@unchatted", saved["items"])
    self.assertIn("gerrit:8663038@new-cid-1234", saved["items"])
    self.assertTrue(saved["items"]["gerrit:8663038@new-cid-1234"]["chat_busy"])

  def test_start_chat_validates_fallback_topics_and_cleans_legacy_key(self):
    for rec in (
        {
            "kind": "gerrit",
            "target_id": "8663038",
            "host": "chromium-review.googlesource.com",
            "subject": "[canvas] Fix bug",
            "project_name": "TextClusters API",
        },
        {
            "kind": "gerrit",
            "target_id": "1408158",
            "host": "skia-review.googlesource.com",
            "subject": "Fix bug",
            "project_name": "Skia",
        },
        {
            "kind": "github",
            "target_id": "195122",
            "subject": "",
            "project_name": "Flight Deck",
        },
    ):
      topic = w._build_start_chat_topic(rec)
      self.assertLessEqual(len(topic), w.TITLE_TOPIC_MAX)
      self.assertEqual(w.cl.validate_chat_topic(topic), topic)

    with mock.patch.object(
        w.urllib.request,
        "urlopen",
        side_effect=[
            http.client.IncompleteRead(b"truncated"),
            _http_json_resp([{"_number": 8663038, "subject": "[canvas] ShapedText"}]),
        ],
    ), mock.patch.object(w.subprocess, "check_output", return_value='{"items":[]}'):
      remote = w.discover_open_remote_changes(
          already_chatted_keys=set(),
          proj_names={"p-tc": "TextClusters API"},
          ignored_ids=set(),
      )
    self.assertEqual([r["item_key"] for r in remote], ["gerrit:8663038"])

    legacy_state = {
        "items": {
            "gerrit:8663038": {
                "item_key": "gerrit:8663038",
                "kind": "gerrit",
                "target_id": "8663038",
                "host": "chromium-review.googlesource.com",
                "url": "https://chromium-review.googlesource.com/c/8663038",
                "subject": "[canvas] Fix bug",
                "is_self": False,
                "conversation_id": "",
                "project_id": "p-tc",
                "project_name": "TextClusters API",
            }
        }
    }
    with tempfile.TemporaryDirectory() as tmp:
      state_path = os.path.join(tmp, "state.json")
      with open(state_path, "w", encoding="utf-8") as f:
        json.dump(legacy_state, f)
      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(w, "discover_active_cl_chats", return_value=[]), \
           mock.patch.object(w.cl, "resolve_project_id", return_value="p-tc"), \
           mock.patch.object(
               w.cl, "resolve_handoff_model", return_value={"enum": "MODEL_PRO"}
           ), \
           mock.patch.object(
               w.cl, "start_conversation_exact", return_value="new-cid-5678"
           ):
        res = w.start_chat_for_item("gerrit:8663038")
        with open(state_path, encoding="utf-8") as sf:
          saved = json.load(sf)

    self.assertTrue(res["ok"])
    self.assertEqual(list(saved["items"].keys()), ["gerrit:8663038@new-cid-5678"])

  def test_dashboard_renders_unchatted_reviewer_cl_after_cq_bot_msg_in_needs_you(self):
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.split("\nboot();", 1)[0] + """
const box = {jetski_box_url: 'https://jetski.corp.google.com/box/host'};
const reviewerAfterBot = {
  item_key: 'gerrit:8663038', kind: 'gerrit', target_id: '8663038', status: 'NEW',
  phase: 'NEEDS_YOUR_ACTION', is_self: false, in_my_attention: true,
  is_draft: false, in_development: false, is_watching: false, conversation_id: '',
  project_name: 'TextClusters API',
  subject: '[canvas] Move TextMetrics shaping state into ShapedText',
  patchset: 8, url: 'https://chromium-review.googlesource.com/c/8663038',
  last_msg_author: 'chromium-scoped@luci-project-accounts.iam.gserviceaccount.com',
  last_msg_age: '15m ago', last_msg_snippet: 'Patch Set 8: This CL has passed the run'
};
const ownAfterBot = Object.assign({}, reviewerAfterBot, {
  item_key: 'gerrit:1408158', target_id: '1408158', is_self: true
});
console.log(JSON.stringify({
  revStatus: statusOf(reviewerAfterBot),
  revLane: laneOf(reviewerAfterBot),
  ownStatus: statusOf(ownAfterBot),
  card: focusCard(reviewerAfterBot, box),
  row: rowHtml(reviewerAfterBot, box),
}));
"""
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=True
    )
    out = json.loads(proc.stdout.strip())
    self.assertEqual(out["revStatus"], "attention")
    self.assertEqual(out["revLane"], "you")
    self.assertEqual(out["ownStatus"], "waiting")
    self.assertIn("Start chat", out["card"])
    self.assertIn("startChat(", out["card"])
    self.assertIn("start chat", out["row"])

  def test_sync_once_links_archived_chat_and_companion_cls_and_prs(self):
    summaries = {
        "c-archived-1409697": {
            "annotations": {
                "title": "[15:11] ⦿ cl/1409697 first-line indent review",
                "archived": True,
            },
            "trajectoryMetadata": {
                "projectId": "proj-skia",
                "lastModifiedTime": "2026-10-29T15:11:00Z",
                "parentConversationId": "",
                "isBattleModeFork": False,
                "sourceMetadata": None,
            },
        },
        "c-active-195122": {
            "annotations": {
                "title": "[10:00] ⦿ PR #195122 Avoid browser queries",
                "archived": False,
            },
            "trajectoryMetadata": {
                "projectId": "proj-wp",
                "lastModifiedTime": "2026-10-29T14:00:00Z",
                "parentConversationId": "",
                "isBattleModeFork": False,
                "sourceMetadata": None,
            },
        },
    }
    proj_names = {
        "proj-skia": "Skia",
        "proj-wp": "WebParagraph",
    }
    remote = [
        {
            "item_key": "gerrit:1409697",
            "kind": "gerrit",
            "target_id": "1409697",
            "preferred_host": "skia-review.googlesource.com",
            "conversation_id": "",
            "title": "",
            "project_id": "proj-skia",
            "project_name": "Skia",
            "chat_last_modified": "",
            "single_target": True,
            "chat_busy": False,
        },
        {
            "item_key": "gerrit:1410416",
            "kind": "gerrit",
            "target_id": "1410416",
            "preferred_host": "skia-review.googlesource.com",
            "conversation_id": "",
            "title": "",
            "project_id": "proj-skia",
            "project_name": "Skia",
            "chat_last_modified": "",
            "single_target": True,
            "chat_busy": False,
        },
        {
            "item_key": "github:195123",
            "kind": "github",
            "target_id": "195123",
            "preferred_host": "",
            "conversation_id": "",
            "title": "",
            "project_id": "proj-wp",
            "project_name": "WebParagraph",
            "chat_last_modified": "",
            "single_target": True,
            "chat_busy": False,
        },
    ]

    def fake_gerrit_status(cl_id, *args, **kwargs):
      is_comp = cl_id == "1410416"
      return {
          "ok": True,
          "host": "skia-review.googlesource.com",
          "url": f"https://skia-review.googlesource.com/c/{cl_id}",
          "status": "NEW",
          "phase": "WAITING_ON_REVIEWERS" if is_comp else "NEEDS_YOUR_ACTION",
          "subject": (
              "[skparagraph] Fix first-line indent edge cases"
              if is_comp
              else "[skparagraph] Add first-line indentation to Paragraph::layout"
          ),
          "owner_name": "Julia Lavrova" if is_comp else "William Selna",
          "owner_email": "jlavrova@google.com" if is_comp else "wselna123@gmail.com",
          "role": "author" if is_comp else "reviewer (goog)",
          "is_self": is_comp,
          "is_external": not is_comp,
          "is_draft": is_comp,
          "in_development": is_comp,
          "is_watching": False,
          "reviewer_removed": False,
          "patchset": 2 if is_comp else 3,
          "revision": "rev2222" if is_comp else "rev3333",
          "in_my_attention": not is_comp,
          "waiting_on": [],
          "merge_conflict": False,
          "tree_red": False,
          "cr_summary": "0",
          "cq_status": "",
          "cq_failed": False,
          "last_msg_author": "jlavrova@google.com" if is_comp else "wselna123@gmail.com",
          "last_msg_date": "2026-10-29 19:07:54.000000000",
          "last_msg_et": "15:07 ET",
          "last_msg_age": "5m ago",
          "last_msg_snippet": "Uploaded patchset 2",
          "submitted": "",
          "submitted_et": "",
          "submitted_age": "",
          "status_changed_raw": "2026-10-29 19:07:54.000000000",
          "status_changed_et": "15:07 ET",
          "status_changed_age": "5m ago",
          "post_submit": None,
      }

    def fake_github_status(pr_id, *args, **kwargs):
      return {
          "ok": True,
          "host": "",
          "url": f"https://github.com/flutter/flutter/pull/{pr_id}",
          "status": "OPEN",
          "phase": "WAITING_ON_REVIEWERS",
          "subject": "[web_paragraph] Avoid browser queries (companion to #195122)",
          "owner_name": "Julia Lavrova",
          "owner_email": "jlavrova@google.com",
          "role": "author",
          "is_self": True,
          "is_external": False,
          "is_draft": True,
          "in_development": True,
          "is_watching": False,
          "reviewer_removed": False,
          "patchset": 1,
          "revision": "rev1111",
          "in_my_attention": False,
          "waiting_on": [],
          "merge_conflict": False,
          "tree_red": False,
          "cr_summary": "",
          "cq_status": "",
          "cq_failed": False,
          "last_msg_author": "jlavrova@google.com",
          "last_msg_date": "2026-10-29 18:00:00.000000000",
          "last_msg_et": "14:00 ET",
          "last_msg_age": "1h ago",
          "last_msg_snippet": "Companion PR",
          "submitted": "",
          "submitted_et": "",
          "submitted_age": "",
          "status_changed_raw": "2026-10-29 18:00:00.000000000",
          "status_changed_et": "14:00 ET",
          "status_changed_age": "1h ago",
          "post_submit": None,
      }

    def fake_related(host, cl_id):
      if cl_id == "1410416":
        return ["1409697"]
      return []

    empty_gmail = {
        "thread_ids": [],
        "inbox_thread_ids": [],
        "total_msgs": 0,
        "inbox_msgs": 0,
        "trash_msgs": 0,
        "unread_msgs": 0,
    }

    with tempfile.TemporaryDirectory() as tmp:
      state_path = os.path.join(tmp, "state.json")
      w._RELATED_CLS_CACHE.clear()
      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(
               w.cl, "_fetch_ls_summaries_for_audit", return_value=summaries
           ), \
           mock.patch.object(w, "load_project_names", return_value=proj_names), \
           mock.patch.object(w, "fetch_gardener_snapshot", return_value=({}, [])), \
           mock.patch.object(w, "discover_open_remote_changes", return_value=remote), \
           mock.patch.object(
               w, "fetch_gerrit_cl_status", side_effect=fake_gerrit_status
           ), \
           mock.patch.object(
               w, "fetch_github_pr_status", side_effect=fake_github_status
           ), \
           mock.patch.object(
               w, "fetch_gerrit_related_cl_ids", side_effect=fake_related
           ), \
           mock.patch.object(
               w, "fetch_gmail_status_for_id", return_value=empty_gmail
           ):
        state = w.sync_once(
            wake_chats=False, update_titles=False, skia_dir="/nonexistent"
        )

    # 1. 1409697 was matched to its archived chat
    self.assertIn("gerrit:1409697@c-archived-1409697", state["items"])
    rec_parent = state["items"]["gerrit:1409697@c-archived-1409697"]
    self.assertEqual(rec_parent["conversation_id"], "c-archived-1409697")
    self.assertTrue(rec_parent["chat_archived"])

    # 2. 1410416 companion was linked to 1409697's archived chat
    self.assertIn("gerrit:1410416@c-archived-1409697", state["items"])
    rec_comp = state["items"]["gerrit:1410416@c-archived-1409697"]
    self.assertEqual(rec_comp["conversation_id"], "c-archived-1409697")
    self.assertEqual(rec_comp["companion_to"], "gerrit:1409697")
    self.assertEqual(rec_comp["companion_to_label"], "CL 1409697")
    self.assertEqual(
        rec_comp["companion_to_url"], "https://skia-review.googlesource.com/c/1409697"
    )
    self.assertFalse(rec_comp["single_target"])
    self.assertEqual(
        rec_parent["companions"],
        [{
            "item_key": "gerrit:1410416",
            "target_id": "1410416",
            "label": "CL 1410416",
            "url": "https://skia-review.googlesource.com/c/1410416",
            "is_wip": True,
        }],
    )

    # 3. 195123 companion PR was linked to 195122's active chat
    self.assertIn("github:195123@c-active-195122", state["items"])
    rec_pr_comp = state["items"]["github:195123@c-active-195122"]
    self.assertEqual(rec_pr_comp["conversation_id"], "c-active-195122")
    self.assertEqual(rec_pr_comp["companion_to"], "github:195122")
    self.assertEqual(rec_pr_comp["companion_to_label"], "PR #195122")
    self.assertEqual(
        rec_pr_comp["companion_to_url"],
        "https://github.com/flutter/flutter/pull/195122",
    )
    self.assertFalse(rec_pr_comp["single_target"])

    # None of them remain unchatted
    self.assertNotIn("gerrit:1409697@unchatted", state["items"])
    self.assertNotIn("gerrit:1410416@unchatted", state["items"])
    self.assertNotIn("github:195123@unchatted", state["items"])

    # 4. start_chat_for_item reuses the linked companion chat
    with tempfile.TemporaryDirectory() as tmp2:
      state_path2 = os.path.join(tmp2, "state.json")
      with open(state_path2, "w", encoding="utf-8") as f:
        json.dump(state, f)
      with mock.patch.object(w, "STATE_FILE", state_path2), \
           mock.patch.object(
               w.cl, "_fetch_ls_summaries_for_audit", return_value=summaries
           ), \
           mock.patch.object(w, "load_project_names", return_value=proj_names), \
           mock.patch.object(
               w, "fetch_gerrit_related_cl_ids", side_effect=fake_related
           ):
        res_comp = w.start_chat_for_item("gerrit:1410416")
        res_arch = w.start_chat_for_item("gerrit:1409697")

    self.assertTrue(res_comp["ok"])
    self.assertTrue(res_comp["already_existed"])
    self.assertEqual(res_comp["conversation_id"], "c-archived-1409697")

    self.assertTrue(res_arch["ok"])
    self.assertTrue(res_arch["already_existed"])
    self.assertEqual(res_arch["conversation_id"], "c-archived-1409697")

  def test_fetch_gerrit_cl_status_wip_and_submit_requirements(self):
    wip_self_payload = {
        "status": "NEW",
        "subject": "[skparagraph] Implement feature",
        "owner": {"email": "jlavrova@google.com", "name": "Julia Lavrova"},
        "current_revision": "sha111",
        "revisions": {"sha111": {"_number": 1}},
        "work_in_progress": True,
        "reviewers": {"REVIEWER": []},
        "labels": {},
        "messages": [],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_http_json_resp(wip_self_payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status("1410001", [], [], skia_dir="/nonexistent")
    self.assertTrue(res["ok"])
    self.assertTrue(res["is_wip"])
    self.assertEqual(res["wip_label"], "WIP")
    self.assertTrue(res["is_draft"])
    self.assertFalse(res["in_development"])
    self.assertEqual(res["missing_reviewers_to_assign"], 1)
    self.assertEqual(res["reviewer_status"], "No reviewers assigned")

    external_payload = {
        "status": "NEW",
        "subject": "[skparagraph] First-line indent",
        "owner": {"email": "wselna123@gmail.com", "name": "William Selna"},
        "current_revision": "sha222",
        "revisions": {"sha222": {"_number": 2}},
        "reviewers": {"REVIEWER": [{"email": "jlavrova@google.com"}]},
        "labels": {
            "Code-Review": {
                "all": [{"email": "jlavrova@google.com", "value": 1}]
            }
        },
        "submit_requirements": [
            {"name": "Review-Enforcement", "status": "UNSATISFIED"}
        ],
        "messages": [],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_http_json_resp(external_payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status("1409697", [], [], skia_dir="/nonexistent")
    self.assertTrue(res["ok"])
    self.assertTrue(res["is_external"])
    self.assertFalse(res["is_wip"])
    self.assertEqual(res["required_approvals"], 2)
    self.assertEqual(res["approved_count"], 1)
    self.assertEqual(res["assigned_reviewers_count"], 1)
    self.assertEqual(res["missing_reviewers_to_assign"], 1)
    self.assertEqual(res["missing_approvals"], 1)
    self.assertEqual(res["cr_summary"], "+1 (1/2)")
    self.assertIn("Needs +1 more reviewer", res["reviewer_status"])

  def test_fetch_github_pr_status_draft_companion_and_two_approvals(self):
    pr_payload = {
        "number": 195123,
        "title": "[web_paragraph] Avoid browser queries",
        "state": "OPEN",
        "isDraft": False,
        "headRefName": "companion-pr-195122",
        "headRefOid": "head1234567890",
        "reviewDecision": "REVIEW_REQUIRED",
        "author": {"login": "wselna"},
        "latestReviews": [{"author": {"login": "jlavrova"}, "state": "APPROVED"}],
        "reviews": [{"author": {"login": "jlavrova"}, "state": "APPROVED"}],
        "reviewRequests": [],
        "comments": [],
        "commits": [],
        "statusCheckRollup": [],
    }
    with mock.patch.object(
        w.subprocess, "check_output", return_value=json.dumps(pr_payload)
    ):
      res = w.fetch_github_pr_status("195123")
    self.assertTrue(res["ok"])
    self.assertEqual(res["required_approvals"], 2)
    self.assertEqual(res["approved_count"], 1)
    self.assertEqual(res["assigned_reviewers_count"], 1)
    self.assertEqual(res["missing_reviewers_to_assign"], 1)
    self.assertEqual(res["missing_approvals"], 1)
    self.assertEqual(res["cr_summary"], "✓ 1/2")

    # Explicit [WIP] in title flags PR as WIP with "WIP" label
    pr_payload["title"] = "[WIP] [web_paragraph] Avoid browser queries"
    with mock.patch.object(
        w.subprocess, "check_output", return_value=json.dumps(pr_payload)
    ):
      res_wip = w.fetch_github_pr_status("195123")
    self.assertTrue(res_wip["is_wip"])
    self.assertEqual(res_wip["wip_label"], "WIP")

    # Native GitHub isDraft flags PR as WIP with "Draft" label
    pr_payload["title"] = "[web_paragraph] Avoid browser queries"
    pr_payload["isDraft"] = True
    with mock.patch.object(
        w.subprocess, "check_output", return_value=json.dumps(pr_payload)
    ):
      res_draft = w.fetch_github_pr_status("195123")
    self.assertTrue(res_draft["is_wip"])
    self.assertEqual(res_draft["wip_label"], "Draft")

  def test_dashboard_renders_wip_companion_and_reviewer_gap_badges(self):
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.split("\nboot();", 1)[0] + """
const box = {jetski_box_url: 'https://jetski.corp.google.com/box/host'};
const parentCl = {
  item_key: 'gerrit:1409697', kind: 'gerrit', target_id: '1409697', status: 'NEW',
  phase: 'WAITING_ON_REVIEWERS', is_self: false, is_external: true, in_my_attention: false,
  is_draft: false, in_development: false, is_watching: true, conversation_id: 'c-archived-1409697',
  project_name: 'Skia',
  subject: '[skparagraph] Add first-line indentation to Paragraph::layout',
  patchset: 3, url: 'https://skia-review.googlesource.com/c/1409697',
  required_approvals: 2, approved_count: 1, assigned_reviewers_count: 1,
  missing_reviewers_to_assign: 1, missing_approvals: 1, cr_summary: '+1 (1/2)',
  companions: [{
    item_key: 'gerrit:1410416', label: 'CL 1410416',
    url: 'https://skia-review.googlesource.com/c/1410416', is_wip: true
  }],
};
const compCl = {
  item_key: 'gerrit:1410416', kind: 'gerrit', target_id: '1410416', status: 'NEW',
  phase: 'WAITING_ON_REVIEWERS', is_self: true, is_external: false, in_my_attention: false,
  is_draft: true, in_development: false, is_watching: false, conversation_id: 'c-archived-1409697',
  project_name: 'Skia',
  subject: '[skparagraph] Fix first-line indent edge cases',
  patchset: 2, url: 'https://skia-review.googlesource.com/c/1410416',
  is_wip: true, wip_label: 'WIP',
  companion_to: 'gerrit:1409697', companion_to_label: 'CL 1409697',
  companion_to_url: 'https://skia-review.googlesource.com/c/1409697',
  required_approvals: 1, approved_count: 0, assigned_reviewers_count: 0,
  missing_reviewers_to_assign: 1, missing_approvals: 1, cr_summary: '0',
};
console.log(JSON.stringify({
  parentRow: rowHtml(parentCl, box),
  parentState: stateHtml(parentCl),
  compRow: rowHtml(compCl, box),
  compState: stateHtml(compCl),
  compCard: focusCard(compCl, box),
}));
"""
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=True
    )
    out = json.loads(proc.stdout.strip())
    self.assertIn("Has companion CL 1410416 (WIP)", out["parentRow"])
    self.assertIn("Needs +1 reviewer (1/2)", out["parentState"])
    self.assertIn('<span class="badge wip">WIP</span>', out["compRow"])
    self.assertIn("Companion → CL 1409697", out["compRow"])
    self.assertIn("No reviewers assigned", out["compState"])
    self.assertIn('<span class="badge wip">WIP</span>', out["compCard"])
    self.assertIn("Companion → CL 1409697", out["compCard"])
    self.assertIn("No reviewers assigned", out["compCard"])


if __name__ == "__main__":
  unittest.main()
