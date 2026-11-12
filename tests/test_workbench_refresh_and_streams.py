#!/usr/bin/env python3
"""Contract tests for Workbench R30 follow-ups: multi-source adopt hints and single-card refresh."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.request

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKBENCH_DIR = os.path.join(CONFIG_DIR, "workbench")
SCRIPTS_DIR = os.path.join(CONFIG_DIR, "scripts")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (CONFIG_DIR, WORKBENCH_DIR, SCRIPTS_DIR, TESTS_DIR):
  if _p not in sys.path:
    sys.path.insert(0, _p)

import ingestor  # pylint: disable=g-import-not-at-top
import test_workbench_partial_dom as partial_dom  # pylint: disable=g-import-not-at-top
import work_store  # pylint: disable=g-import-not-at-top
from workbench import workbench_server  # pylint: disable=g-import-not-at-top

NOW = 1_760_000_000.0
DAY = 86400.0
BUG_A = "buganizer:41132531"
BUG_B = "buganizer:40189181"
CL_A = "gerrit:skia:1414016"
CL_B = "gerrit:skia:1414017"
CL_COMP = "gerrit:skia:1414999"
MAIL_HUMAN = "gmail_thread:1a1b5428d6cf6533"
MAIL_GH_NOTIF = "gmail_thread:1a1c733cd7dda7b6"


def _cards_by_id(
    store: work_store.WorkStore, cl_state: dict | None = None, now: float = NOW
) -> dict[str, dict]:
  """Projects dashboard cards indexed by work_id."""
  proj = store.build_dashboard_projection(
      cl_watcher_state=cl_state or {"items": {}, "bugs": {}}, now=now
  )
  return {w["work_id"]: w for w in proj["works"]}


def _hint_kinds(card: dict) -> list[str]:
  """Extracts ordered hint kind strings from a projected card."""
  return [h["kind"] for h in card.get("hints") or []]


class MultiSourceAdoptHintContractTest(unittest.TestCase):
  """Verifies adopt banner only fires on active multi-source works, not routine single-bug items."""

  def setUp(self) -> None:
    self._tmp = tempfile.TemporaryDirectory()
    self.store = work_store.WorkStore(os.path.join(self._tmp.name, "workbench.db"))

  def tearDown(self) -> None:
    self._tmp.cleanup()

  def _upsert(self, key: str, kind: str, title: str, state: dict) -> None:
    self.store.upsert_artifact(key, kind=kind, title=title, state=state, now=NOW)

  # Routine 1-bug + 1-CL work with multiple chat handoff pieces and a companion probe stays quiet
  def test_single_bug_and_cl_with_chats_and_companion_has_no_adopt_banner(self) -> None:
    self._upsert(BUG_A, "buganizer", "Bug A", {"status": "ASSIGNED", "updated_ts": NOW})
    self._upsert(
        CL_A,
        "gerrit",
        "Fix A",
        {"status": "NEW", "is_self": True, "activity_ts": NOW, "linked_bug_keys": [BUG_A]},
    )
    self._upsert(
        CL_COMP,
        "gerrit",
        "[companion] probe",
        {"status": "NEW", "is_companion": True, "companion_to": CL_A, "activity_ts": NOW},
    )
    wid = self.store.create_work("Single bug fix", work_id="work-single", now=NOW)
    for k in (BUG_A, CL_A, CL_COMP):
      self.store.link_artifact(wid, k, role="primary", now=NOW)
    for idx in range(3):
      ck = f"chat_chain:chat-{idx}"
      self._upsert(ck, "chat_chain", f"Chat {idx}", {"cascade_id": f"chat-{idx}"})
      self.store.link_artifact(wid, ck, role="channel", now=NOW)
    self.assertEqual(_hint_kinds(_cards_by_id(self.store, now=NOW + DAY)[wid]), [])

  # Pure single-family stacks (3 own CLs with no bugs or correspondence) do not show adopt banner
  def test_three_cls_without_second_source_family_has_no_adopt_banner(self) -> None:
    wid = self.store.create_work("Pure CL stack", work_id="work-stack", now=NOW)
    for num in ("1001", "1002", "1003"):
      ck = f"gerrit:skia:{num}"
      self._upsert(
          ck, "gerrit", f"CL {num}", {"status": "NEW", "is_self": True, "activity_ts": NOW}
      )
      self.store.link_artifact(wid, ck, role="primary", now=NOW)
    self.assertEqual(_hint_kinds(_cards_by_id(self.store, now=NOW + DAY)[wid]), [])

  # GitHub notification mirror email does not count as independent human correspondence for adopt
  def test_github_pr_issue_and_notif_mirror_email_has_no_adopt_banner(self) -> None:
    pr = "github_pr:flutter/flutter:196314"
    iss = "github_issue:flutter/flutter:196308"
    self._upsert(pr, "github_pr", "PR", {"status": "OPEN", "is_self": True, "activity_ts": NOW})
    self._upsert(iss, "github_issue", "Issue", {"status": "OPEN", "updated_ts": NOW})
    self._upsert(
        MAIL_GH_NOTIF,
        "gmail_thread",
        "[flutter/flutter] PR",
        {"last_message_ts": NOW, "last_from_me": True, "is_github_pr_thread": True},
    )
    wid = self.store.create_work("GH mirror", work_id="work-gh", now=NOW)
    for k in (pr, iss, MAIL_GH_NOTIF):
      self.store.link_artifact(wid, k, role="primary", now=NOW)
    self.assertEqual(_hint_kinds(_cards_by_id(self.store, now=NOW + DAY)[wid]), [])

  # Finished works (all bugs closed and all CLs landed) never ask to be adopted
  def test_finished_work_never_shows_adopt_banner(self) -> None:
    self._upsert(BUG_A, "buganizer", "Bug A", {"status": "FIXED", "updated_ts": NOW})
    self._upsert(
        CL_A, "gerrit", "Fix A", {"status": "MERGED", "is_self": True, "activity_ts": NOW}
    )
    self._upsert(
        MAIL_HUMAN, "gmail_thread", "Thread", {"last_message_ts": NOW, "last_from_me": True}
    )
    wid = self.store.create_work("Done work", work_id="work-fin", now=NOW)
    for k in (BUG_A, CL_A, MAIL_HUMAN):
      self.store.link_artifact(wid, k, role="primary", now=NOW)
    self.assertEqual(_hint_kinds(_cards_by_id(self.store, now=NOW + DAY)[wid]), [])

  # Human correspondence + 2 bugs (3 substantive items across 2 families) triggers adopt banner
  def test_human_thread_plus_two_bugs_triggers_adopt_banner(self) -> None:
    self._upsert(BUG_A, "buganizer", "Bug A", {"status": "ASSIGNED", "updated_ts": NOW})
    self._upsert(BUG_B, "buganizer", "Bug B", {"status": "ASSIGNED", "updated_ts": NOW})
    self._upsert(
        MAIL_HUMAN,
        "gmail_thread",
        "Fixed bugs in Flutter",
        {"last_message_ts": NOW, "last_from_me": True},
    )
    wid = self.store.create_work("Renzo bugs", work_id="work-renzo", now=NOW)
    for k in (BUG_A, BUG_B, MAIL_HUMAN):
      self.store.link_artifact(wid, k, role="primary", now=NOW)
    self.assertEqual(_hint_kinds(_cards_by_id(self.store, now=NOW + DAY)[wid]), ["adopt"])

  # Unhydrated Gmail stubs, gh_target_key mirrors, and same-doc gdoc_thread do not trigger adopt
  def test_unhydrated_stub_gh_target_and_gdoc_thread_do_not_false_trigger_adopt(self) -> None:
    self._upsert(BUG_A, "buganizer", "Bug A", {"status": "ASSIGNED", "updated_ts": NOW})
    self._upsert(
        CL_A, "gerrit", "Fix A", {"status": "NEW", "is_self": True, "activity_ts": NOW}
    )
    stub_mail = "gmail_thread:unhydrated001"
    self._upsert(stub_mail, "gmail_thread", stub_mail, {})
    w_stub = self.store.create_work("Stub mail", work_id="work-stub-mail", now=NOW)
    for k in (BUG_A, CL_A, stub_mail):
      self.store.link_artifact(w_stub, k, role="primary", now=NOW)

    gh_mail = "gmail_thread:ghtarget002"
    self._upsert(
        gh_mail,
        "gmail_thread",
        "Re: [flutter/flutter] Issue",
        {"last_message_ts": NOW, "last_from_me": False, "gh_target_key": "github_issue:a/b:1"},
    )
    w_gh = self.store.create_work("GH target mail", work_id="work-gh-target", now=NOW)
    for k in (BUG_A, CL_A, gh_mail):
      self.store.link_artifact(w_gh, k, role="primary", now=NOW)

    doc_key = "gdoc:1AbCdEfGh"
    doc_thr = "gdoc_thread:1AbCdEfGh:disco99"
    self._upsert(doc_key, "gdoc", "Design doc", {"updated_ts": NOW})
    self._upsert(
        doc_thr,
        "gdoc_thread",
        "Comment on Design doc",
        {"last_message_ts": NOW, "last_from_me": True},
    )
    w_doc = self.store.create_work("Doc and CL", work_id="work-doc-cl", now=NOW)
    for k in (CL_A, doc_key, doc_thr):
      self.store.link_artifact(w_doc, k, role="primary", now=NOW)

    cards = _cards_by_id(self.store, now=NOW + DAY)
    self.assertEqual(_hint_kinds(cards[w_stub]), [])
    self.assertEqual(_hint_kinds(cards[w_gh]), [])
    self.assertEqual(_hint_kinds(cards[w_doc]), [])


class AcceptAndRefreshWorkCardContractTest(unittest.TestCase):
  """Verifies virtual card adoption and single-card live tracker refresh outcomes."""

  def setUp(self) -> None:
    self._tmp = tempfile.TemporaryDirectory()
    self.db_path = os.path.join(self._tmp.name, "workbench.db")
    self.cl_state_path = os.path.join(self._tmp.name, "cl_watcher_state.json")
    self.watcher_state_path = os.path.join(self._tmp.name, "watcher_state.json")
    self.store = work_store.WorkStore(self.db_path)

  def tearDown(self) -> None:
    self._tmp.cleanup()

  def _write_cl_state(self, payload: dict) -> None:
    with open(self.cl_state_path, "w", encoding="utf-8") as f:
      json.dump(payload, f)

  # Accepting a virtual single-bug card materializes it as owner=user without starting a chat
  def test_accept_virtual_card_materializes_user_work(self) -> None:
    self._write_cl_state({
        "items": {},
        "bugs": {
            BUG_A: {
                "id": "41132531",
                "bug_key": BUG_A,
                "source": "buganizer",
                "status": "ASSIGNED",
                "assignee": "jlavrova@google.com",
                "title": "PDF image size",
                "url": "https://b.corp.google.com/issues/41132531",
                "modified_ts": NOW,
            }
        },
    })
    wid = ingestor.accept_work_card(
        self.store, f"virt-{BUG_A}", cl_state_path=self.cl_state_path, now=NOW
    )
    self.assertFalse(wid.startswith("virt-"))
    cl_state = ingestor.load_cl_watcher_snapshot(self.cl_state_path)
    cards = _cards_by_id(self.store, cl_state=cl_state, now=NOW)
    self.assertIn(wid, cards)
    self.assertNotIn(f"virt-{BUG_A}", cards)
    self.assertEqual(cards[wid]["owner"], "user")

  # Refreshing a virtual bug reassigned to Florin drops it from cl_watcher_state and returns reason
  def test_refresh_virtual_bug_reassigned_to_colleague_removes_card_with_reason(self) -> None:
    self._write_cl_state({
        "items": {},
        "bugs": {
            BUG_A: {
                "id": "41132531",
                "bug_key": BUG_A,
                "source": "buganizer",
                "status": "ASSIGNED",
                "assignee": "jlavrova@google.com",
                "title": "Chrome PDF large image",
                "url": "https://b.corp.google.com/issues/41132531",
                "last_human_comment_author": "scroggo@google.com",
                "last_human_comment_at": "2024-02-20T20:06:00Z",
                "modified_ts": NOW - 60,
            },
            BUG_B: {
                "id": "40189181",
                "bug_key": BUG_B,
                "source": "buganizer",
                "status": "ASSIGNED",
                "assignee": "jlavrova@google.com",
                "title": "Unrelated neighbor bug",
                "url": "https://b.corp.google.com/issues/40189181",
                "modified_ts": NOW - 120,
            },
        },
    })
    fresh_bug = {
        "id": "41132531",
        "bug_key": BUG_A,
        "source": "buganizer",
        "status": "ASSIGNED",
        "assignee": "fmalita@google.com",
        "title": "Chrome PDF large image",
        "url": "https://b.corp.google.com/issues/41132531",
        "modified_ts": NOW,
    }
    with mock.patch.object(
        ingestor, "_fetch_live_buganizer_records", return_value={BUG_A: fresh_bug}
    ) as mock_bz:
      res = ingestor.refresh_work_card(
          self.store,
          f"virt-{BUG_A}",
          cl_state_path=self.cl_state_path,
          watcher_state_path=self.watcher_state_path,
          now=NOW,
      )
    mock_bz.assert_called_once_with(["41132531"])
    self.assertIsNone(res["card"])
    self.assertIn("fmalita@google.com", res["removed_reason"])
    saved_cl = ingestor.load_cl_watcher_snapshot(self.cl_state_path)
    self.assertNotIn(BUG_A, saved_cl["bugs"])
    self.assertIn(BUG_B, saved_cl["bugs"])

  # Refreshing a persisted work whose Gerrit CL moved out of attention updates its lane to waiting
  def test_refresh_persisted_work_updates_gerrit_lane_and_reports_lane_move(self) -> None:
    raw_item_key = "gerrit:1414016@unchatted"
    old_item = {
        "kind": "gerrit",
        "target_id": "1414016",
        "host": "skia-review.googlesource.com",
        "status": "NEW",
        "subject": "Fix paragraph bounds",
        "url": "https://skia-review.googlesource.com/c/skia/+/1414016",
        "is_self": True,
        "in_attention_set": True,
        "attention_reason": "Reviewer replied",
        "entity_state": {"lane": "you", "action_required": True, "reason": "Reviewer replied"},
        "updated_ts": NOW - 300,
    }
    self._write_cl_state({"items": {raw_item_key: old_item}, "bugs": {}})
    self.store.upsert_artifact(
        CL_A, kind="gerrit", title="1414016: Fix paragraph bounds", state=old_item, now=NOW - 300
    )
    wid = self.store.create_work("Paragraph bounds", work_id="work-cl", owner="user", now=NOW - 300)
    self.store.link_artifact(wid, CL_A, role="primary", source="manual", now=NOW - 300)

    fresh_cl = {
        **old_item,
        "in_attention_set": False,
        "attention_reason": "Waiting on reviewer",
        "has_reviewers": True,
        "reviewers": ["bungeman@google.com"],
        "entity_state": {
            "lane": "waiting",
            "action_required": False,
            "reason": "Waiting on reviewer",
        },
        "updated_ts": NOW,
    }
    with mock.patch.object(
        ingestor, "_fetch_live_gerrit_records", return_value={CL_A: fresh_cl}
    ) as mock_ger:
      res = ingestor.refresh_work_card(
          self.store,
          wid,
          cl_state_path=self.cl_state_path,
          watcher_state_path=self.watcher_state_path,
          now=NOW,
      )
    mock_ger.assert_called_once_with([CL_A])
    self.assertIsNotNone(res["card"])
    self.assertEqual(res["previous_lane"], "needs-you")
    self.assertEqual(res["card"]["lane"], "waiting")
    self.assertIn("Waiting", res["lane_moved_reason"])

  # Refreshing a Gmail card within the 10-min cache TTL still polls live Gmail and clears tray
  def test_refresh_gmail_thread_bypasses_ttl_cache_and_updates_watcher_state(self) -> None:
    tid = MAIL_HUMAN.split(":", 1)[1]
    stale_prov = {
        "schema_ver": 6,
        "thread_id": tid,
        "origin_ts": NOW - 600,
        "root_subject": "Fixed bugs in Flutter",
        "origin_from": "Renzo",
        "started_by_me": False,
        "sent_by_me": True,
        "last_from_me": False,
        "last_message_ts": NOW - 60,
        "counterparty": "Renzo",
        "counterparty_reply_ts": NOW - 60,
        "message_count": 1,
        "non_bot_human_count": 1,
        "in_inbox": True,
        "me_addressed": True,
        "recipient_count": 1,
        "unanswered_human": True,
        "fetched_at": NOW - 30,
    }
    self._write_cl_state({"items": {}, "bugs": {}})
    with open(self.watcher_state_path, "w", encoding="utf-8") as f:
      json.dump(
          {
              "gmail_threads": {tid: stale_prov},
              "unanswered_tray": [
                  {
                      "artifact_key": MAIL_HUMAN,
                      "sender": "Renzo",
                      "subject": "Fixed bugs in Flutter",
                      "url": f"https://mail.google.com/mail/u/0/#all/{tid}",
                  }
              ],
          },
          f,
      )
    self.store.upsert_artifact(
        MAIL_HUMAN,
        kind="gmail_thread",
        title="Fixed bugs in Flutter",
        url=f"https://mail.google.com/mail/u/0/#all/{tid}",
        state=stale_prov,
        now=NOW - 30,
    )
    self.store.upsert_artifact(
        BUG_A,
        kind="buganizer",
        title="Bug A",
        state={"status": "ASSIGNED", "updated_ts": NOW - 30},
        now=NOW - 30,
    )
    wid = self.store.create_work("Renzo thread", work_id="work-mail", owner="user", now=NOW - 30)
    self.store.link_artifact(wid, MAIL_HUMAN, role="channel", source="manual", now=NOW - 30)
    self.store.link_artifact(wid, BUG_A, role="primary", source="manual", now=NOW - 30)
    self.store.reconcile_correspondence_obligations(wid, now=NOW - 30)

    live_thread_json = json.dumps({
        "messages": [
            {
                "id": "m1",
                "internalDate": str(int((NOW - 600) * 1000)),
                "labelIds": ["INBOX"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Renzo <renzo@google.com>"},
                        {"name": "To", "value": "jlavrova@google.com"},
                        {"name": "Subject", "value": "Fixed bugs in Flutter"},
                    ]
                },
                "snippet": "Could you check these bugs?",
            },
            {
                "id": "m2",
                "internalDate": str(int(NOW * 1000)),
                "labelIds": ["SENT"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Julia Lavrova <jlavrova@google.com>"},
                        {"name": "To", "value": "Renzo <renzo@google.com>"},
                        {"name": "Subject", "value": "Re: Fixed bugs in Flutter"},
                    ]
                },
                "snippet": "Reassigned PDF bugs to Florin.",
            },
        ]
    })
    fake_proc = mock.Mock(returncode=0, stdout=live_thread_json, stderr="")
    with mock.patch.object(ingestor, "_fetch_live_buganizer_records", return_value={}):
      with mock.patch.object(ingestor.subprocess, "run", return_value=fake_proc) as mock_run:
        res = ingestor.refresh_work_card(
            self.store,
            wid,
            cl_state_path=self.cl_state_path,
            watcher_state_path=self.watcher_state_path,
            now=NOW,
        )
    mock_run.assert_called_once()
    art_st = (self.store.get_artifact(MAIL_HUMAN) or {}).get("state") or {}
    self.assertTrue(art_st.get("last_from_me"))
    self.assertFalse(art_st.get("unanswered_human"))
    with open(self.watcher_state_path, "r", encoding="utf-8") as f:
      saved_w = json.load(f)
    self.assertTrue((saved_w.get("gmail_threads") or {}).get(tid, {}).get("last_from_me"))
    self.assertNotIn("gmail_threads_cache", saved_w)
    self.assertEqual(saved_w.get("unanswered_tray"), [])
    self.assertEqual(res["projection"]["unanswered_tray"], [])

  # Accepting a nonexistent persisted work_id raises ValueError instead of silently succeeding
  def test_accept_unknown_persisted_work_raises_value_error(self) -> None:
    self._write_cl_state({"items": {}, "bugs": {}})
    with self.assertRaises(ValueError):
      ingestor.accept_work_card(
          self.store, "work-does-not-exist", cl_state_path=self.cl_state_path, now=NOW
      )

  # Sent drafts, closed GH mirror emails, and concurrent watcher_state writes reconcile cleanly
  def test_refresh_gmail_draft_and_gh_mirror_thread_and_concurrent_watcher_state(self) -> None:
    pr_key = "github_pr:flutter/flutter:196314"
    tid = MAIL_GH_NOTIF.split(":", 1)[1]
    draft_key = "gmail_draft:r987654"
    self._write_cl_state({"items": {}, "bugs": {}})
    with open(self.watcher_state_path, "w", encoding="utf-8") as f:
      json.dump(
          {"gmail_threads": {}, "active_draft_ids": ["r987654"], "transcript_cursors": {"c0": 1}},
          f,
      )
    self.store.upsert_artifact(
        pr_key, "github_pr", "196314: Fix BiDi", state={"status": "OPEN"}, now=NOW - 60
    )
    self.store.upsert_artifact(
        MAIL_GH_NOTIF,
        "gmail_thread",
        "[flutter/flutter] Fix BiDi",
        state={
            "schema_ver": 6,
            "thread_id": tid,
            "last_from_me": False,
            "last_message_ts": NOW - 60,
            "gh_target_key": pr_key,
            "is_github_pr_thread": True,
            "unanswered_human": True,
        },
        now=NOW - 60,
    )
    self.store.upsert_artifact(
        draft_key, "gmail_draft", "Draft reply", state={"status": "OPEN"}, now=NOW - 60
    )
    wid = self.store.create_work("GH + draft", work_id="work-gh-draft", owner="user", now=NOW - 60)
    self.store.link_artifact(wid, pr_key, role="primary", source="manual", now=NOW - 60)
    self.store.link_artifact(wid, MAIL_GH_NOTIF, role="channel", source="manual", now=NOW - 60)
    self.store.link_artifact(wid, draft_key, role="channel", source="manual", now=NOW - 60)

    gh_node = {
        "__typename": "PullRequest",
        "number": 196314,
        "title": "Fix BiDi",
        "state": "MERGED",
        "locked": False,
        "closedAt": "2026-11-11T20:00:00Z",
        "mergedAt": "2026-11-11T20:00:00Z",
        "updatedAt": "2026-11-11T20:00:00Z",
        "url": "https://github.com/flutter/flutter/pull/196314",
        "comments": {"nodes": []},
        "reviews": {"nodes": []},
    }
    fresh_prov = {
        "schema_ver": 6,
        "thread_id": tid,
        "root_subject": "[flutter/flutter] Fix BiDi",
        "last_from_me": False,
        "last_message_ts": NOW - 50,
        "is_automated": False,
        "fetched_at": NOW,
    }

    def fake_fetch_prov(req_tid: str, cache: dict, cur_now: float, force_refresh: bool = False):
      del cur_now, force_refresh
      # Simulate concurrent run_sync_cycle advancing transcript_cursors while RPC is in flight
      with open(self.watcher_state_path, "r", encoding="utf-8") as rf:
        cur_disk = json.load(rf)
      cur_disk["transcript_cursors"] = {"c0": 1, "conv-concurrent": 99}
      with open(self.watcher_state_path, "w", encoding="utf-8") as wf:
        json.dump(cur_disk, wf)
      cache[req_tid] = dict(fresh_prov)
      return dict(fresh_prov)

    pr_info = {
        "kind": "github",
        "target_id": "196314",
        "host": "github.com/flutter/flutter",
        "status": "MERGED",
        "phase": "Merged",
        "subject": "Fix BiDi",
        "url": "https://github.com/flutter/flutter/pull/196314",
    }
    cw = ingestor._load_cl_watcher_module()  # pylint: disable=protected-access
    with mock.patch.object(ingestor, "_fetch_live_github_nodes", return_value={pr_key: gh_node}):
      with mock.patch.object(cw, "fetch_github_pr_status", return_value=pr_info):
        with mock.patch.object(
            ingestor, "_fetch_gmail_thread_provenance", side_effect=fake_fetch_prov
        ):
          with mock.patch.object(ingestor, "_gmail_draft_still_exists", return_value=False):
            ingestor.refresh_work_card(
                self.store,
                wid,
                cl_state_path=self.cl_state_path,
                watcher_state_path=self.watcher_state_path,
                now=NOW,
            )

    d_st = (self.store.get_artifact(draft_key) or {}).get("state") or {}
    self.assertEqual(d_st.get("status"), "SENT")
    t_st = (self.store.get_artifact(MAIL_GH_NOTIF) or {}).get("state") or {}
    self.assertTrue(t_st.get("gh_thread_closed"))
    self.assertFalse(t_st.get("unanswered_human"))
    with open(self.watcher_state_path, "r", encoding="utf-8") as f:
      saved_w = json.load(f)
    self.assertEqual(saved_w.get("transcript_cursors"), {"c0": 1, "conv-concurrent": 99})
    self.assertNotIn("r987654", saved_w.get("active_draft_ids") or [])

  # Buganizer refresh handles DUPLICATE batch-get, comments, collaborators, and redacted titles
  def test_refresh_buganizer_duplicate_collaborator_redacted_and_comments(self) -> None:
    cw = ingestor._load_cl_watcher_module()  # pylint: disable=protected-access
    batch_stdout = (
        "- Issue 40189181: Old PDF bug [DUPLICATE of 41132531] (Priority: P2, Assignee: none)\n"
    )
    rendered = (
        "Issue ID: 40189181\n"
        "Comment #1 by colleague@google.com at 2026-11-11 18:00:00:\n"
        "Please take another look.\n"
    )
    fake_cli = mock.Mock(returncode=0, stdout=batch_stdout, stderr="")
    with mock.patch.object(
        cw, "call_buganizer_mcp_batch", side_effect=[[""], [rendered]]
    ):
      with mock.patch.object(ingestor.os.path, "isfile", return_value=True):
        with mock.patch.object(ingestor.subprocess, "run", return_value=fake_cli):
          fetched = ingestor._fetch_live_buganizer_records(  # pylint: disable=protected-access
              ["40189181"]
          )
    self.assertIn(BUG_B, fetched)
    self.assertTrue(fetched[BUG_B]["status"].startswith("DUPLICATE"))
    self.assertEqual(fetched[BUG_B]["last_human_comment_author"], "colleague@google.com")

    # Collaborator bug + P1 bug with active fix CL + redacted title preserving SQLite real title
    collab_key = "buganizer:40708190"
    redacted_key = "buganizer:41311608"
    self._write_cl_state({
        "items": {
            "gerrit:1414016@unchatted": {
                "kind": "gerrit",
                "target_id": "1414016",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "phase": "Review",
                "is_self": True,
                "is_wip": False,
                "subject": "Fix P1 bug",
                "bug_ids": ["41132531"],
            }
        },
        "bugs": {
            collab_key: {
                "id": "40708190",
                "bug_key": collab_key,
                "source": "buganizer",
                "status": "ASSIGNED",
                "priority": "P2",
                "assignee": "other@google.com",
                "is_collaborator": True,
                "title": "Collaborator bug",
                "modified_ts": NOW - 60,
            },
            BUG_A: {
                "id": "41132531",
                "bug_key": BUG_A,
                "source": "buganizer",
                "status": "ASSIGNED",
                "priority": "P1",
                "assignee": "jlavrova@google.com",
                "seen_self_assigned": True,
                "acked_ts": NOW - 10,
                "title": "P1 bug with CL",
                "modified_ts": NOW - 60,
            },
        },
    })
    self.store.upsert_artifact(
        redacted_key,
        "buganizer",
        "b/41311608: Real unredacted customer bug title",
        state={"status": "ASSIGNED", "bug_title": "Real unredacted customer bug title"},
        now=NOW - 60,
    )
    wid = self.store.create_work("Bug trio", work_id="work-bugs", owner="user", now=NOW - 60)
    for k in (collab_key, BUG_A, redacted_key):
      self.store.link_artifact(wid, k, role="primary", source="manual", now=NOW - 60)

    live_map = {
        collab_key: {
            "id": "40708190",
            "bug_key": collab_key,
            "source": "buganizer",
            "status": "ASSIGNED",
            "priority": "P2",
            "assignee": "other@google.com",
            "title": "Collaborator bug",
            "modified_ts": NOW,
        },
        BUG_A: {
            "id": "41132531",
            "bug_key": BUG_A,
            "source": "buganizer",
            "status": "ASSIGNED",
            "priority": "P1",
            "assignee": "jlavrova@google.com",
            "title": "P1 bug with CL",
            "modified_ts": NOW,
        },
        redacted_key: {
            "id": "41311608",
            "bug_key": redacted_key,
            "source": "buganizer",
            "status": "ASSIGNED",
            "priority": "P2",
            "assignee": "jlavrova@google.com",
            "title": "",
            "title_redacted": True,
            "modified_ts": NOW,
        },
    }
    with mock.patch.object(ingestor, "_fetch_live_buganizer_records", return_value=live_map):
      ingestor.refresh_work_card(
          self.store,
          wid,
          cl_state_path=self.cl_state_path,
          watcher_state_path=self.watcher_state_path,
          now=NOW,
      )
    saved_cl = ingestor.load_cl_watcher_snapshot(self.cl_state_path)
    self.assertIn(collab_key, saved_cl["bugs"])
    self.assertNotIn("without a CL", str(saved_cl["bugs"][BUG_A].get("why_needs_you") or ""))
    red_art = self.store.get_artifact(redacted_key) or {}
    self.assertIn("Real unredacted customer bug title", str(red_art.get("title") or ""))
    self.assertEqual(
        (red_art.get("state") or {}).get("bug_title"), "Real unredacted customer bug title"
    )

  # Raw Gerrit responses (no entity_state, absent from watcher) and GitHub Issue/PR refresh lanes
  def test_refresh_gerrit_without_watcher_entry_and_github_issue_pr_updates_lanes(self) -> None:
    gh_iss = "github_issue:flutter/flutter:196308"
    gh_bkey = "gh_issue:flutter/flutter#196308"
    gh_pr = "github_pr:flutter/flutter:196314"
    self._write_cl_state({
        "items": {},
        "bugs": {
            gh_bkey: {
                "bug_key": gh_bkey,
                "source": "github",
                "id": "196308",
                "tracker_flavor": "github",
                "short_id": "flutter#196308",
                "status": "OPEN",
                "assignee": "jlavrova",
                "title": "WebParagraph line height",
                "url": "https://github.com/flutter/flutter/issues/196308",
                "modified_ts": NOW - 100,
            }
        },
    })
    self.store.upsert_artifact(
        CL_A,
        "gerrit",
        "1414016: Aged out CL",
        state={
            "kind": "gerrit",
            "target_id": "1414016",
            "status": "NEW",
            "phase": "Comments",
            "is_self": True,
            "in_my_attention": True,
            "entity_state": {"lane": "you", "needs_my_reply": True},
        },
        now=NOW - 100,
    )
    self.store.upsert_artifact(
        gh_pr,
        "github_pr",
        "196314: WebParagraph PR",
        state={"kind": "github", "target_id": "196314", "status": "OPEN", "is_self": True},
        now=NOW - 100,
    )
    wid = self.store.create_work("Solo Gerrit", work_id="work-solo-cl", owner="user", now=NOW - 100)
    self.store.link_artifact(wid, CL_A, role="primary", source="manual", now=NOW - 100)

    # Real fetch_gerrit_cl_status returns raw fields without entity_state
    raw_gerrit_live = {
        "kind": "gerrit",
        "target_id": "1414016",
        "host": "skia-review.googlesource.com",
        "status": "NEW",
        "phase": "Review",
        "is_self": True,
        "is_wip": False,
        "has_reviewers": True,
        "in_my_attention": False,
        "cq_failed": False,
        "subject": "Aged out CL",
        "url": "https://skia-review.googlesource.com/c/skia/+/1414016",
    }
    with mock.patch.object(
        ingestor, "_fetch_live_gerrit_records", return_value={CL_A: raw_gerrit_live}
    ):
      res_cl = ingestor.refresh_work_card(
          self.store,
          wid,
          cl_state_path=self.cl_state_path,
          watcher_state_path=self.watcher_state_path,
          now=NOW,
      )
    self.assertEqual(res_cl["previous_lane"], "needs-you")
    self.assertEqual(res_cl["card"]["lane"], "waiting")

    # Closed GitHub Issue removes stale OPEN entry from cl_watcher_state["bugs"]
    closed_issue_node = {
        "__typename": "Issue",
        "number": 196308,
        "title": "WebParagraph line height",
        "state": "CLOSED",
        "stateReason": "COMPLETED",
        "locked": False,
        "closedAt": "2026-11-11T21:00:00Z",
        "updatedAt": "2026-11-11T21:00:00Z",
        "url": "https://github.com/flutter/flutter/issues/196308",
        "assignees": {"nodes": [{"login": "jlavrova"}]},
        "comments": {"nodes": []},
    }
    with mock.patch.object(
        ingestor, "_fetch_live_github_nodes", return_value={gh_iss: closed_issue_node}
    ):
      res_iss = ingestor.refresh_work_card(
          self.store,
          f"virt-{gh_iss}",
          cl_state_path=self.cl_state_path,
          watcher_state_path=self.watcher_state_path,
          now=NOW,
      )
    self.assertIsNone(res_iss["card"])
    self.assertIn("CLOSED", res_iss["removed_reason"])
    saved_cl = ingestor.load_cl_watcher_snapshot(self.cl_state_path)
    self.assertNotIn(gh_bkey, saved_cl["bugs"])

    # Open GitHub PR with failing checks refreshes via fetch_github_pr_status -> needs-you
    w_pr = self.store.create_work("GH PR", work_id="work-gh-pr", owner="user", now=NOW - 100)
    self.store.link_artifact(w_pr, gh_pr, role="primary", source="manual", now=NOW - 100)
    live_pr_status = {
        "kind": "github",
        "target_id": "196314",
        "host": "github.com/flutter/flutter",
        "status": "OPEN",
        # Same phase fetch_github_pr_status sets on own PRs with failing checks
        "phase": "ALERT_CQ_FAILED",
        "is_self": True,
        "is_wip": False,
        "cq_failed": True,
        "in_my_attention": True,
        "subject": "WebParagraph PR",
        "url": "https://github.com/flutter/flutter/pull/196314",
    }
    cw = ingestor._load_cl_watcher_module()  # pylint: disable=protected-access
    with mock.patch.object(ingestor, "_fetch_live_github_nodes", return_value={}):
      with mock.patch.object(cw, "fetch_github_pr_status", return_value=live_pr_status):
        res_pr = ingestor.refresh_work_card(
            self.store,
            w_pr,
            cl_state_path=self.cl_state_path,
            watcher_state_path=self.watcher_state_path,
            now=NOW,
        )
    self.assertEqual(res_pr["card"]["lane"], "needs-you")


def _post_json(port: int, path: str, payload: dict) -> dict:
  """Sends a JSON POST request to a local test Workbench HTTP server."""
  req = urllib.request.Request(
      f"http://127.0.0.1:{port}{path}",
      data=json.dumps(payload).encode("utf-8"),
      headers={"Content-Type": "application/json"},
      method="POST",
  )
  with urllib.request.urlopen(req, timeout=5) as resp:
    return json.loads(resp.read().decode("utf-8"))


def _get_json(port: int, path: str) -> dict:
  """Sends a JSON GET request to a local test Workbench HTTP server."""
  with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
    return json.loads(resp.read().decode("utf-8"))


class WorkbenchStreamsAndSingleRefreshUiTest(unittest.TestCase):
  """Verifies HTTP routes and client DOM behavior for open streams and per-card live refresh."""

  # HTTP /api/work/accept promotes virtual cards and /api/work/refresh updates cached /api/state
  def test_http_accept_virtual_card_and_refresh_endpoint(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      store = work_store.WorkStore(os.path.join(tmp, "wb.db"))
      cl_state_path = os.path.join(tmp, "cl_watcher_state.json")
      watcher_state_path = os.path.join(tmp, "watcher_state.json")
      info_path = os.path.join(tmp, "info.json")
      with open(cl_state_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "items": {},
                "bugs": {
                    BUG_A: {
                        "id": "41132531",
                        "bug_key": BUG_A,
                        "source": "buganizer",
                        "status": "ASSIGNED",
                        "assignee": "jlavrova@google.com",
                        "title": "PDF image bug",
                        "url": "https://b.corp.google.com/issues/41132531",
                        "modified_ts": NOW,
                    },
                    BUG_B: {
                        "id": "40189181",
                        "bug_key": BUG_B,
                        "source": "buganizer",
                        "status": "ASSIGNED",
                        "assignee": "jlavrova@google.com",
                        "title": "Reassignable PDF bug",
                        "url": "https://b.corp.google.com/issues/40189181",
                        "modified_ts": NOW - 60,
                    },
                },
            },
            f,
        )
      srv = workbench_server.create_server(
          port=0,
          store=store,
          cl_state_path=cl_state_path,
          watcher_state_path=watcher_state_path,
          info_path=info_path,
      )
      srv.refresh_snapshot()
      th = threading.Thread(target=srv.serve_forever, daemon=True)
      th.start()
      try:
        port = srv.server_address[1]
        accepted = _post_json(port, "/api/work/accept", {"work_id": f"virt-{BUG_A}"})
        acc_wid = accepted.get("accepted_work_id")
        self.assertTrue(acc_wid and not acc_wid.startswith("virt-"))
        by_id = {w["work_id"]: w for w in accepted["works"]}
        self.assertEqual(by_id[acc_wid]["owner"], "user")

        fresh_b = {
            "id": "40189181",
            "bug_key": BUG_B,
            "source": "buganizer",
            "status": "ASSIGNED",
            "assignee": "fmalita@google.com",
            "title": "Reassignable PDF bug",
            "url": "https://b.corp.google.com/issues/40189181",
            "modified_ts": NOW,
        }
        with mock.patch.object(
            ingestor, "_fetch_live_buganizer_records", return_value={BUG_B: fresh_b}
        ):
          refreshed = _post_json(port, "/api/work/refresh", {"work_id": f"virt-{BUG_B}"})
        self.assertTrue(refreshed["ok"])
        self.assertIsNone(refreshed["card"])
        self.assertIn("fmalita@google.com", refreshed["removed_reason"])
        state_after = _get_json(port, "/api/state")
        after_ids = {w["work_id"] for w in state_after["works"]}
        self.assertNotIn(f"virt-{BUG_B}", after_ids)
        self.assertIn(acc_wid, after_ids)
      finally:
        srv.shutdown()
        srv.server_close()

  # Triage renders Мои работы above open Вне моих работ without Черновики fold, with virtual Принять
  def test_ui_open_streams_and_virtual_card_accept_button(self) -> None:
    w_user = partial_dom._make_work("work-u", lane="waiting", owner="user", title="Owned work")
    w_draft_ny = partial_dom._make_work(
        "work-dn", lane="needs-you", owner="draft", title="Urgent unowned"
    )
    w_draft_wt = partial_dom._make_work(
        "work-dw", lane="waiting", owner="draft", title="Waiting unowned", activity_at=1762800100.0
    )
    w_virt = partial_dom._make_work(
        "virt-buganizer:41132531",
        lane="needs-you",
        owner="",
        title="Virtual bug card",
        activity_at=1762800150.0,
    )
    w_virt.pop("owner", None)
    w_virt["is_virtual"] = True

    promoted = partial_dom._make_work(
        "work-promoted", lane="needs-you", owner="user", title="Virtual bug card"
    )
    proj1 = partial_dom._make_projection([w_user, w_draft_ny, w_draft_wt, w_virt])
    proj2 = partial_dom._make_projection([promoted, w_user, w_draft_ny, w_draft_wt])
    proj2["accepted_work_id"] = "work-promoted"

    res = partial_dom._run_workbench_js(
        proj1,
        f"""
      const p2 = {json.dumps(proj2)};
      const container = document.getElementById('wb-cards-container');
      const fullTxt = container.textContent;
      assert(!fullTxt.includes('Черновики'), 'Collapsed Черновики fold must be gone');
      assert.strictEqual(container.querySelector('[data-drafts-toggle]'), null);
      assert.strictEqual(container.querySelector('.wb-drafts-body'), null);

      const iMine = fullTxt.indexOf('Мои работы (1)');
      const iOther = fullTxt.indexOf('Вне моих работ (3)');
      assert(iMine >= 0 && iOther > iMine, 'Мои работы must render above open Вне моих работ');

      const articles = Array.from(container.children).filter(el => el.tagName === 'ARTICLE');
      assert.strictEqual(articles.length, 4, 'All 4 cards must be open direct children');
      assert.strictEqual(articles[0].getAttribute('data-work-id'), 'work-u');
      assert.strictEqual(articles[0].querySelector('[data-work-accept]'), null);

      for (const wid of ['work-dn', 'virt-buganizer:41132531', 'work-dw']) {{
        const card = container.querySelector(`[data-work-id="${{wid}}"]`);
        assert(card, `Card ${{wid}} must be mounted`);
        assert(card.querySelector('[data-work-accept]'), `Unowned ${{wid}} needs Принять`);
        assert(card.querySelector('[data-work-refresh]'), `Card ${{wid}} needs ↻ refresh button`);
      }}

      // Collapse virtual card, click Принять, and verify collapse state carries to work-promoted
      const virtHdr = container.querySelector('[data-toggle-card="virt-buganizer:41132531"]');
      await clickElement(virtHdr);
      assert.strictEqual(
        container.querySelector('[data-work-id="virt-buganizer:41132531"]')
          .getAttribute('data-collapsed'),
        'true'
      );

      const posted = [];
      global.fetch = async (url, opts) => {{
        posted.push({{url, body: JSON.parse(opts.body)}});
        return {{ok: true, json: async () => p2}};
      }};
      const virtAccept = container.querySelector(
        '[data-work-accept="virt-buganizer:41132531"]'
      );
      await clickElement(virtAccept);
      assert.strictEqual(posted.length, 1);
      assert.strictEqual(posted[0].url, '/api/work/accept');
      assert.strictEqual(posted[0].body.work_id, 'virt-buganizer:41132531');
      const promCard = container.querySelector('[data-work-id="work-promoted"]');
      assert(promCard, 'Promoted card must render in Мои работы');
      assert.strictEqual(promCard.getAttribute('data-collapsed'), 'true');
      console.log(JSON.stringify({{ok: true}}));
    """,
    )
    self.assertTrue(res["ok"])

  # Single-card ↻ updates only that <article> in place without re-sorting neighbors
  def test_ui_single_card_refresh_morphs_in_place_without_jumping_siblings(self) -> None:
    w1 = partial_dom._make_work("work-1", lane="needs-you", title="Top card", activity_at=300.0)
    w2 = partial_dom._make_work(
        "work-2", lane="needs-you", title="Middle card", activity_at=200.0, ps=1
    )
    w3 = partial_dom._make_work(
        "work-3", lane="waiting", title="Bottom card", activity_at=100.0
    )
    proj = partial_dom._make_projection([w1, w2, w3])
    w2_fresh = copy.deepcopy(w2)
    w2_fresh["lane"] = "waiting"
    w2_fresh["activity_at"] = 999.0
    w2_fresh["updated_at"] = 999.0
    w2_fresh["reasons"] = ["Waiting on reviewer"]
    w2_fresh["artifacts"][1]["title"] = "Skia 141002 PS2: Middle card"
    w2_fresh["artifacts"][1]["state"]["patchset"] = 2
    refresh_resp = {
        "ok": True,
        "work_id": "work-2",
        "resolved_work_id": "work-2",
        "previous_lane": "needs-you",
        "card": w2_fresh,
        "removed_reason": "",
        "lane_moved_reason": "Перемещено в Waiting",
        "counts": {"needs_you": 1, "waiting": 2, "obligations_open": 0, "unanswered": 0},
        "metrics": {"link_precision": 0.98},
        "synced_et": "15:12 ET (just now)",
    }

    res = partial_dom._run_workbench_js(
        proj,
        f"""
      const refreshPayload = {json.dumps(refresh_resp)};
      const container = document.getElementById('wb-cards-container');
      const c1Before = container.querySelector('[data-work-id="work-1"]');
      const c2Before = container.querySelector('[data-work-id="work-2"]');
      const c3Before = container.querySelector('[data-work-id="work-3"]');
      const btn1 = c1Before.querySelector('[data-work-refresh="work-1"]');
      const btn2 = c2Before.querySelector('[data-work-refresh="work-2"]');
      const btn3 = c3Before.querySelector('[data-work-refresh="work-3"]');
      assert(btn1 && btn2 && btn3);

      let resolveFetch;
      const posted = [];
      global.fetch = (url, opts) => {{
        posted.push({{url, body: JSON.parse(opts.body)}});
        return new Promise(r => {{ resolveFetch = r; }});
      }};

      const clickPromise = clickElement(btn2);
      assert.strictEqual(btn2.disabled, true, 'Clicked ↻ button must disable while in flight');
      assert.strictEqual(btn1.disabled, false, 'Neighbor ↻ buttons must stay enabled');
      assert.strictEqual(btn3.disabled, false);

      resetDomStats();
      resolveFetch({{ok: true, json: async () => refreshPayload}});
      await clickPromise;

      assert.strictEqual(posted.length, 1);
      assert.strictEqual(posted[0].url, '/api/work/refresh');
      assert.strictEqual(posted[0].body.work_id, 'work-2');
      assert.strictEqual(btn2.disabled, false);

      const articles = Array.from(container.children).filter(el => el.tagName === 'ARTICLE');
      assert.strictEqual(articles.length, 3);
      assert.strictEqual(articles[0], c1Before, 'Top neighbor must keep slot 0 and DOM identity');
      assert.strictEqual(articles[1], c2Before, 'Refreshed card must stay in slot 1 in place');
      assert.strictEqual(
        articles[2], c3Before, 'Bottom neighbor must keep slot 2 and DOM identity'
      );
      assert.strictEqual(c2Before.getAttribute('data-lane'), 'waiting');
      assert(c2Before.textContent.includes('PS2'));
      console.log(JSON.stringify({{
        innerHTMLSets: domStats.innerHTMLSets,
        removals: domStats.removals,
      }}));
    """,
    )
    self.assertEqual(res["innerHTMLSets"], 0)
    self.assertEqual(res["removals"], 0)

  # Refreshing a card that disappears leaves a 1-line anchored stub with ✕ so neighbors do not jump
  def test_ui_single_card_refresh_leaves_anchored_stub_with_dismiss(self) -> None:
    w1 = partial_dom._make_work("work-1", lane="needs-you", title="First urgent", activity_at=300.0)
    w2 = partial_dom._make_work(
        "work-2", lane="needs-you", title="PDF image bug", activity_at=200.0
    )
    w3 = partial_dom._make_work("work-3", lane="needs-you", title="Third urgent", activity_at=100.0)
    proj = partial_dom._make_projection([w1, w2, w3])
    proj_after_poll = partial_dom._make_projection([w1, w3], synced_et="15:13 ET (just now)")
    refresh_resp = {
        "ok": True,
        "work_id": "work-2",
        "resolved_work_id": "work-2",
        "previous_lane": "needs-you",
        "card": None,
        "removed_reason": "Переведён на fmalita@google.com",
        "lane_moved_reason": "",
        "counts": {"needs_you": 2, "waiting": 0, "obligations_open": 0, "unanswered": 0},
        "metrics": {"link_precision": 0.98},
        "synced_et": "15:12 ET (just now)",
    }

    res = partial_dom._run_workbench_js(
        proj,
        f"""
      const refreshPayload = {json.dumps(refresh_resp)};
      const pollPayload = {json.dumps(proj_after_poll)};
      const container = document.getElementById('wb-cards-container');
      const c1Before = container.querySelector('[data-work-id="work-1"]');
      const c3Before = container.querySelector('[data-work-id="work-3"]');
      const btn2 = container.querySelector('[data-work-refresh="work-2"]');
      assert(btn2);

      global.fetch = async () => ({{ok: true, json: async () => refreshPayload}});
      await clickElement(btn2);

      let articles = Array.from(container.children).filter(el => el.tagName === 'ARTICLE');
      assert.strictEqual(articles.length, 3, 'Stub must hold slot 1 so card 3 does not jump up');
      assert.strictEqual(articles[0], c1Before);
      assert.strictEqual(articles[2], c3Before);
      const stub = articles[1];
      assert.strictEqual(stub.getAttribute('data-work-id'), 'work-2');
      assert.strictEqual(stub.getAttribute('data-refresh-stub'), 'true');
      assert(stub.textContent.includes('Переведён на fmalita@google.com'));

      // Quiet 60s background poll must keep the anchored stub until she dismisses it
      global.fetch = async () => ({{ok: true, json: async () => pollPayload}});
      await env.getPollFn()();
      articles = Array.from(container.children).filter(el => el.tagName === 'ARTICLE');
      assert.strictEqual(articles.length, 3);
      assert.strictEqual(articles[1].getAttribute('data-refresh-stub'), 'true');

      // Clicking ✕ dismisses only the 1-line stub while neighbors keep DOM identity
      const dismissBtn = stub.querySelector('[data-dismiss-stub="work-2"]');
      assert(dismissBtn);
      await clickElement(dismissBtn);
      articles = Array.from(container.children).filter(el => el.tagName === 'ARTICLE');
      assert.strictEqual(articles.length, 2);
      assert.strictEqual(articles[0], c1Before);
      assert.strictEqual(articles[1], c3Before);
      console.log(JSON.stringify({{ok: true}}));
    """,
    )
    self.assertTrue(res["ok"])

  # Concurrent ↻ refreshes, tab epoch switches, tray updates, search stubs, and group counts hold
  def test_ui_concurrent_refresh_epoch_unanswered_tray_search_and_header_counts(self) -> None:
    w_mine = partial_dom._make_work(
        "work-m", lane="needs-you", owner="user", title="Mine card", activity_at=400.0, ps=1
    )
    w_u1 = partial_dom._make_work(
        "work-u1", lane="needs-you", owner="draft", title="Unowned first", activity_at=300.0, ps=1
    )
    w_u2 = partial_dom._make_work(
        "work-u2", lane="needs-you", owner="draft", title="PDF Florin bug", activity_at=200.0
    )
    proj = partial_dom._make_projection([w_mine, w_u1, w_u2])
    proj["unanswered_tray"] = [{
        "artifact_key": MAIL_HUMAN,
        "sender": "Renzo",
        "subject": "Fixed bugs",
        "age_et": "5m ago",
        "url": "https://mail.google.com/mail/u/0/#all/abc",
    }]
    proj["counts"]["unanswered"] = 1

    u1_fresh = copy.deepcopy(w_u1)
    u1_fresh["artifacts"][1]["title"] = "Skia 141002 PS2: Unowned first"
    u1_fresh["artifacts"][1]["state"]["patchset"] = 2
    resp_u1 = {
        "ok": True,
        "work_id": "work-u1",
        "resolved_work_id": "work-u1",
        "previous_lane": "needs-you",
        "card": u1_fresh,
        "removed_reason": "",
        "lane_moved_reason": "",
        "counts": {"needs_you": 3, "waiting": 0, "obligations_open": 0, "unanswered": 0},
        "unanswered_tray": [],
        "metrics": {"link_precision": 0.98},
        "synced_et": "15:20 ET",
    }
    resp_u2 = {
        "ok": True,
        "work_id": "work-u2",
        "resolved_work_id": "work-u2",
        "previous_lane": "needs-you",
        "card": None,
        "removed_reason": "Переведён на fmalita@google.com",
        "lane_moved_reason": "",
        "counts": {"needs_you": 2, "waiting": 0, "obligations_open": 0, "unanswered": 0},
        "unanswered_tray": [],
        "metrics": {"link_precision": 0.98},
        "synced_et": "15:21 ET",
    }
    resp_mine_done = {
        "ok": True,
        "work_id": "work-m",
        "resolved_work_id": "work-m",
        "previous_lane": "needs-you",
        "card": None,
        "removed_reason": "СЛ замержен",
        "lane_moved_reason": "",
        "counts": {"needs_you": 1, "waiting": 0, "obligations_open": 0, "unanswered": 0},
        "unanswered_tray": [],
        "metrics": {"link_precision": 0.98},
        "synced_et": "15:22 ET",
    }

    res = partial_dom._run_workbench_js(
        proj,
        f"""
      const rU1 = {json.dumps(resp_u1)};
      const rU2 = {json.dumps(resp_u2)};
      const rMine = {json.dumps(resp_mine_done)};
      const container = document.getElementById('wb-cards-container');
      const trayEl = document.getElementById('wb-tray-container');
      assert(trayEl.textContent.includes('Unanswered Human Threads'));

      const resolvers = {{}};
      global.fetch = (_url, opts) => {{
        const wid = JSON.parse(opts.body).work_id;
        return new Promise(r => {{ resolvers[wid] = r; }});
      }};

      // Start two concurrent ↻ refreshes; resolve u1 (PS2) first, then u2 (stub)
      const p1 = clickElement(container.querySelector('[data-work-refresh="work-u1"]'));
      const p2 = clickElement(container.querySelector('[data-work-refresh="work-u2"]'));
      resolvers['work-u1']({{ok: true, json: async () => rU1}});
      await p1;
      assert(!trayEl.textContent.includes('Unanswered Human Threads'), 'Tray must clear');

      resolvers['work-u2']({{ok: true, json: async () => rU2}});
      await p2;

      // Second completion must NOT revert u1 PS2 via stale pre-await prevWorks snapshot
      const u1After = container.querySelector('[data-work-id="work-u1"]');
      assert(u1After && u1After.textContent.includes('PS2'), 'Concurrent PS2 update must survive');
      const unownedHdr = container.querySelector('[data-unit-key="wb-sec:unowned-hdr"]');
      assert(unownedHdr.textContent.includes('Вне моих работ (1)'), 'Header excludes stub count');

      // Zero-hit search must hide unrelated stubs and show #wb-empty
      const searchEl = document.getElementById('wb-search');
      searchEl.value = 'zzz-no-such-item';
      await searchEl.dispatch('input', {{ target: searchEl }});
      assert(container.querySelector('#wb-empty'), 'Zero-hit search must render #wb-empty');
      searchEl.value = '';
      await searchEl.dispatch('input', {{ target: searchEl }});
      assert(
        container.querySelector('[data-refresh-stub="true"]'), 'Clearing search restores stub'
      );

      // Switching lane chip mid-flight must NOT plant a bogus stub into the newly selected tab
      const pMine = clickElement(container.querySelector('[data-work-refresh="work-m"]'));
      const waitingChip = document.querySelector('button[data-filter="waiting"]');
      // Chip listeners bind on the button itself rather than the delegated body handler
      await waitingChip.dispatch('click', {{ target: waitingChip }});
      resolvers['work-m']({{ok: true, json: async () => rMine}});
      await pMine;
      assert.strictEqual(
        container.querySelector('[data-refresh-stub="true"]'),
        null,
        'In-flight refresh from prior tab must not plant stub into newly selected tab'
      );
      console.log(JSON.stringify({{ok: true}}));
    """,
    )
    self.assertTrue(res["ok"])


if __name__ == "__main__":
  unittest.main()
