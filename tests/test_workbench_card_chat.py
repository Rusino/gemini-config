#!/usr/bin/env python3
"""Contract unit tests for Workbench (:8090) card chat launcher and standalone chat open (R23)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if CONFIG_DIR not in sys.path:
  sys.path.insert(0, CONFIG_DIR)
WORKBENCH_DIR = os.path.join(CONFIG_DIR, "workbench")
if WORKBENCH_DIR not in sys.path:
  sys.path.insert(0, WORKBENCH_DIR)
HOOKS_DIR = os.path.join(CONFIG_DIR, "hooks")
if HOOKS_DIR not in sys.path:
  sys.path.insert(0, HOOKS_DIR)

import chat_lifecycle as cl  # pylint: disable=g-import-not-at-top
import ingestor  # pylint: disable=g-import-not-at-top
import work_store  # pylint: disable=g-import-not-at-top
import workbench_server  # pylint: disable=g-import-not-at-top
import workctl  # pylint: disable=g-import-not-at-top


def _seed_projects_dir(pdir: str) -> dict[str, str]:
  """Writes a realistic projects directory including archived SkoreText and config My Work."""
  os.makedirs(pdir, exist_ok=True)
  specs = [
      ("e41d6d4e-d449-4f9b-8284-ac843651c5fb", "Reviews of e-mails, meetings and such", False),
      ("100deebd-72df-4dbe-a33a-aae6712f994e", "SkoreText", True),
      ("61f270de-563d-45a4-b9eb-1c0bccf80cc9", "My Work · ⟳ 2 ● 1", False),
      ("8acb67c4-2cb7-43ca-ba04-dc547b3c4788", "SkParagraph · ⟳ 1", False),
      ("2f999b18-d1cd-4eb5-9bdd-1c8fe328762e", "WebParagraph", False),
      ("42497f18-ff9d-4fd5-9e8b-e666147873c9", "TextClusters API", False),
      ("1a021a3a-e0f3-4f85-ab21-42cfba6cf4d4", "Chromium", False),
      ("9b93560e-8b7f-4b1c-9dfd-8290e4884541", "Skia", False),
      ("9571f9cb-06ce-4b6b-9a5d-9aa1ab55c52a", "Lazy Android", False),
      ("33333333-4444-5555-6666-777777777777", "Text Editor", False),
  ]
  out: dict[str, str] = {}
  for pid, name, archived in specs:
    with open(os.path.join(pdir, f"{pid}.json"), "w", encoding="utf-8") as f:
      json.dump({"id": pid, "name": name, "archived": archived, "conversations": []}, f)
    out[cl.strip_activity_badge(name)] = pid
  return out


class TestWorkbenchCardChatLauncher(unittest.TestCase):

  def setUp(self) -> None:
    self.tmp_dir = tempfile.TemporaryDirectory()
    self.db_path = os.path.join(self.tmp_dir.name, "work_graph.db")
    self.cl_state_path = os.path.join(self.tmp_dir.name, "cl_watcher_state.json")
    self.watcher_state_path = os.path.join(self.tmp_dir.name, "watcher_state.json")
    self.info_path = os.path.join(self.tmp_dir.name, "jetski-workbench.json")
    self.projects_dir = os.path.join(self.tmp_dir.name, "projects")
    self.open_req_file = os.path.join(self.tmp_dir.name, "jetski-open-chat.json")
    self.annotations_dir = os.path.join(self.tmp_dir.name, "annotations")
    os.makedirs(self.annotations_dir, exist_ok=True)
    self.proj_ids = _seed_projects_dir(self.projects_dir)
    # Keep projection classification hermetic so tests never scan ~/.gemini chat files
    self._cls_patcher = mock.patch.object(
        work_store,
        "get_local_chat_classification",
        return_value=(set(), set(), set()),
    )
    self._cls_patcher.start()

  def tearDown(self) -> None:
    self._cls_patcher.stop()
    self.tmp_dir.cleanup()

  # Verifies A2 routing: docs -> Reviews of e-mails, code -> active repo, never archived/config
  def test_project_routing_for_doc_and_code_cards(self) -> None:
    doc_card = {
        "work_id": "work-a0a20efbeb",
        "title": "Notes - Skore/Flutter Sync — Renzo Olivares",
        "project_id": "",
        "artifacts": [
            {
                "artifact_key": "gdoc:1e4GtnB9hkFctLGpprQ_QRkHzVaPXqQF2ZbE-lNZwbk0",
                "kind": "gdoc",
                "role": "primary",
                "title": "Notes - Skore/Flutter Sync",
            },
            {
                "artifact_key": "gdoc_thread:1e4GtnB9hkFctLGpprQ_QRkHzVaPXqQF2ZbE-lNZwbk0:doco.6Xf",
                "kind": "gdoc_thread",
                "role": "channel",
                "title": "Notes - Skore/Flutter Sync (Renzo Olivares)",
            },
        ],
    }
    pid, pname = ingestor.resolve_work_chat_project(doc_card, projects_dir=self.projects_dir)
    self.assertEqual(pid, self.proj_ids["Reviews of e-mails, meetings and such"])
    self.assertEqual(pname, "Reviews of e-mails, meetings and such")

    # Archived SkoreText or config My Work stamps on cards must never be selected
    archived_stamp_doc = {**doc_card, "project_id": "SkoreText"}
    pid_arch, _ = ingestor.resolve_work_chat_project(
        archived_stamp_doc, projects_dir=self.projects_dir
    )
    self.assertEqual(pid_arch, self.proj_ids["Reviews of e-mails, meetings and such"])

    cfg_stamp_doc = {**doc_card, "project_id": "My Work"}
    pid_cfg, _ = ingestor.resolve_work_chat_project(cfg_stamp_doc, projects_dir=self.projects_dir)
    self.assertEqual(pid_cfg, self.proj_ids["Reviews of e-mails, meetings and such"])

    skore_cl_card = {
        "work_id": "work-skore-cl",
        "title": "[SkoreText] Add CoreTextLineShaper",
        "project_id": "100deebd-72df-4dbe-a33a-aae6712f994e",
        "artifacts": [{
            "artifact_key": "gerrit:skia:1382196",
            "kind": "gerrit",
            "role": "primary",
            "title": "[SkoreText] Add CoreTextLineShaper",
        }],
    }
    pid_skore_cl, pname_skore_cl = ingestor.resolve_work_chat_project(
        skore_cl_card, projects_dir=self.projects_dir
    )
    self.assertEqual(pid_skore_cl, self.proj_ids["Skia"])
    self.assertEqual(pname_skore_cl, "Skia")

    skp_card = {
        "work_id": "work-skp",
        "title": "[skparagraph] Measure hit glyph with justification shift",
        "project_id": "",
        "artifacts": [{
            "artifact_key": "gerrit:skia:1414016",
            "kind": "gerrit",
            "role": "primary",
            "title": "[skparagraph] Measure hit glyph with justification shift",
        }],
    }
    self.assertEqual(
        ingestor.resolve_work_chat_project(skp_card, projects_dir=self.projects_dir)[0],
        self.proj_ids["SkParagraph"],
    )

    wp_card = {
        "work_id": "work-wp",
        "title": "Fix bidi cluster mapping in WebParagraph",
        "project_id": "",
        "artifacts": [{
            "artifact_key": "github_pr:flutter/flutter:194335",
            "kind": "github_pr",
            "role": "primary",
            "title": "Fix bidi cluster mapping in WebParagraph",
        }],
    }
    self.assertEqual(
        ingestor.resolve_work_chat_project(wp_card, projects_dir=self.projects_dir)[0],
        self.proj_ids["WebParagraph"],
    )

    tc_card = {
        "work_id": "work-tc",
        "title": "[canvas] Expose getTextClusters on TextMetrics",
        "project_id": "",
        "artifacts": [{
            "artifact_key": "gerrit:chromium:8746922",
            "kind": "gerrit",
            "role": "primary",
            "title": "[canvas] Expose getTextClusters on TextMetrics",
        }],
    }
    self.assertEqual(
        ingestor.resolve_work_chat_project(tc_card, projects_dir=self.projects_dir)[0],
        self.proj_ids["TextClusters API"],
    )

    # Any active non-archived non-config code project in projects_dir (e.g. Text Editor) is honored
    te_card = {
        "work_id": "work-te",
        "title": "Caret affinity at line wrap",
        "project_id": "Text Editor",
        "artifacts": [{
            "artifact_key": "buganizer:580852205",
            "kind": "buganizer",
            "role": "primary",
            "title": "Caret affinity at line wrap",
        }],
    }
    pid_te, pname_te = ingestor.resolve_work_chat_project(te_card, projects_dir=self.projects_dir)
    self.assertEqual(pid_te, self.proj_ids["Text Editor"])
    self.assertEqual(pname_te, "Text Editor")

  # Verifies prompt includes GDoc + primary CLs and topics handle non-Flutter/bare/numbered IDs
  def test_topic_and_prompt_edge_cases_for_multi_artifact_and_non_flutter_cards(self) -> None:
    mixed_card = {
        "work_id": "work-mixed",
        "title": "[skparagraph] Measure hit glyph",
        "artifacts": [
            {
                "artifact_key": "gerrit:skia:1414016",
                "kind": "gerrit",
                "role": "primary",
                "title": "Gerrit 1414016",
                "url": "https://skia-review.googlesource.com/c/skia/+/1414016",
            },
            {
                "artifact_key": "gdoc:1e4GtnB9hkFctLGpprQ_QRkHzVaPXqQF2ZbE-lNZwbk0",
                "kind": "gdoc",
                "role": "channel",
                "title": "Sync Notes",
                "url": (
                    "https://docs.google.com/document/d/"
                    "1e4GtnB9hkFctLGpprQ_QRkHzVaPXqQF2ZbE-lNZwbk0/edit"
                ),
            },
        ],
    }
    mixed_prompt = ingestor._build_work_chat_prompt(mixed_card)
    self.assertIn("includeComments: true", mixed_prompt)
    self.assertIn("1414016", mixed_prompt)

    dart_issue_topic = ingestor._build_work_chat_topic({
        "title": "Analyzer crash on generic extension",
        "artifacts": [{
            "artifact_key": "github_issue:dart-lang/sdk:55123",
            "kind": "github_issue",
            "role": "primary",
        }],
    })
    self.assertNotIn("flutter#", dart_issue_topic)
    self.assertIn("55123", dart_issue_topic)

    bare_bug_topic = ingestor._build_work_chat_topic({
        "title": "flutter#187181",
        "artifacts": [{
            "artifact_key": "github_issue:flutter/flutter:187181",
            "kind": "github_issue",
            "role": "primary",
        }],
    })
    self.assertNotEqual(bare_bug_topic, "flutter#187181")
    self.assertGreaterEqual(len(bare_bug_topic.split()), 2)

    numbered_cr_topic = ingestor._build_work_chat_topic({
        "title": "Chromium CL 8663938: Fix blink layout",
        "artifacts": [{
            "artifact_key": "gerrit:chromium:8663938",
            "kind": "gerrit",
            "role": "primary",
        }],
    })
    self.assertEqual(numbered_cr_topic.count("8663938"), 1)

  # Verifies launcher for gdoc, archived Works, and repeated virtual-card calls (idempotent reuse)
  def test_start_chat_for_gdoc_work_and_virtual_card_links_channel(self) -> None:
    now = 1794344400.0
    store = work_store.WorkStore(self.db_path)
    wid = store.create_work("Notes - Skore/Flutter Sync — Renzo Olivares", now=now)
    doc_id = "1e4GtnB9hkFctLGpprQ_QRkHzVaPXqQF2ZbE-lNZwbk0"
    doc_key = f"gdoc:{doc_id}"
    th_key = f"gdoc_thread:{doc_id}:doco.6XfN31uIG9Ifs1ZiF_NBWg"
    store.upsert_artifact(
        doc_key,
        kind="gdoc",
        title="Notes - Skore/Flutter Sync",
        url=f"https://docs.google.com/document/d/{doc_id}/edit",
        state={"doc_title": "Notes - Skore/Flutter Sync"},
        now=now,
    )
    store.upsert_artifact(
        th_key,
        kind="gdoc_thread",
        title="Notes - Skore/Flutter Sync (Renzo Olivares)",
        url=f"https://docs.google.com/document/d/{doc_id}/edit?disco=doco.6XfN31uIG9Ifs1ZiF_NBWg",
        state={
            "doc_id": doc_id,
            "disco_id": "doco.6XfN31uIG9Ifs1ZiF_NBWg",
            "counterparty": "Renzo Olivares",
            "last_unquoted_text": "Renzo Olivares | --no-harfbuzz @jlavrova@google.com",
            "is_active_correspondence": True,
            "unanswered_human": True,
        },
        now=now,
    )
    store.link_artifact(wid, doc_key, role="primary", source="auto", now=now)
    store.link_artifact(wid, th_key, role="channel", source="auto", now=now)
    store.add_obligation(
        wid,
        description="Follow up / resolve comment in 'Notes - Skore/Flutter Sync' (Renzo Olivares)",
        trigger_expr=f"correspondence_turn({th_key}, 7)",
        target_artifact_key=th_key,
        now=now,
    )

    spawned: list[dict[str, str]] = []

    def fake_start_exact(
        model_enum: str, title: str, prompt: str, project_id: str, before_send=None
    ) -> str:
      cid = f"11112222-3333-4444-8888-00000000000{len(spawned) + 1}"
      if before_send:
        before_send(cid)
      spawned.append({
          "cid": cid,
          "model_enum": model_enum,
          "title": title,
          "prompt": prompt,
          "project_id": project_id,
      })
      return cid

    with (
        mock.patch.object(cl, "resolve_handoff_model", return_value={"enum": "MODEL_TEST_PRO"}),
        mock.patch.object(cl, "start_conversation_exact", side_effect=fake_start_exact),
        mock.patch.object(cl, "is_archived", return_value=False),
    ):
      res1 = ingestor.start_chat_for_work(
          store,
          wid,
          cl_state_path=self.cl_state_path,
          projects_dir=self.projects_dir,
          now=now + 10,
      )
      self.assertTrue(res1["ok"])
      self.assertTrue(res1["started"])
      self.assertFalse(res1["already_existed"])
      self.assertEqual(res1["project_id"], self.proj_ids["Reviews of e-mails, meetings and such"])
      self.assertEqual(len(spawned), 1)
      call0 = spawned[0]
      self.assertLess(len(call0["title"]), 50)
      self.assertTrue(cl.validate_chat_topic(cl.clean_base_title(call0["title"])))
      self.assertFalse(work_store.is_config_text(call0["title"]))
      self.assertFalse(work_store.is_config_text(call0["prompt"]))
      self.assertIn(doc_id, call0["prompt"])
      self.assertIn("read_file_content", call0["prompt"])
      self.assertIn("includeComments", call0["prompt"])
      self.assertIn("--no-harfbuzz", call0["prompt"])

      # Verify manual channel link is visible on the projected card immediately
      proj = store.build_dashboard_projection()
      card_after = next(w for w in proj["works"] if w["work_id"] == wid)
      chat_keys = [
          a["artifact_key"] for a in card_after["artifacts"] if a["kind"] == "chat_chain"
      ]
      self.assertIn(f"chat_chain:{call0['cid']}", chat_keys)

      # Second call with force_new=False reuses the existing active chat idempotently
      res_dup = ingestor.start_chat_for_work(
          store,
          wid,
          cl_state_path=self.cl_state_path,
          projects_dir=self.projects_dir,
          force_new=False,
          now=now + 20,
      )
      self.assertTrue(res_dup["already_existed"])
      self.assertEqual(res_dup["conversation_id"], call0["cid"])
      self.assertEqual(len(spawned), 1)

      # Explicit force_new=True starts an additional chat on the card
      res_force = ingestor.start_chat_for_work(
          store,
          wid,
          cl_state_path=self.cl_state_path,
          projects_dir=self.projects_dir,
          force_new=True,
          now=now + 30,
      )
      self.assertTrue(res_force["started"])
      self.assertEqual(len(spawned), 2)

      # Virtual CL card materializes into a persisted Work and reuses cleanly on repeated virt- call
      with open(self.cl_state_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "items": {
                    "1414016": {
                        "kind": "gerrit",
                        "target_id": "1414016",
                        "host": "skia-review.googlesource.com",
                        "status": "NEW",
                        "subject": "[skparagraph] Measure hit glyph with justification shift",
                        "url": "https://skia-review.googlesource.com/c/skia/+/1414016",
                    }
                }
            },
            f,
        )
      res_virt = ingestor.start_chat_for_work(
          store,
          "virt-gerrit:skia:1414016",
          cl_state_path=self.cl_state_path,
          projects_dir=self.projects_dir,
          now=now + 40,
      )
      self.assertTrue(res_virt["ok"])
      self.assertTrue(res_virt["work_id"].startswith("work-"))
      self.assertEqual(res_virt["project_id"], self.proj_ids["SkParagraph"])
      roles = store.get_work_artifact_roles(res_virt["work_id"])
      self.assertEqual(roles.get("gerrit:skia:1414016"), "primary")
      self.assertEqual(roles.get(f"chat_chain:{res_virt['conversation_id']}"), "channel")

      res_virt_repeat = ingestor.start_chat_for_work(
          store,
          "virt-gerrit:skia:1414016",
          cl_state_path=self.cl_state_path,
          projects_dir=self.projects_dir,
          force_new=False,
          now=now + 45,
      )
      self.assertTrue(res_virt_repeat["already_existed"])
      self.assertEqual(res_virt_repeat["work_id"], res_virt["work_id"])
      self.assertEqual(res_virt_repeat["conversation_id"], res_virt["conversation_id"])

      # Archived/completed Work fallback retains stored artifacts for reuse and prompt building
      wid_arch = store.create_work("Archived Sync Notes", now=now + 50)
      arch_doc_id = "1ArchivedDocId9999999999999"
      store.upsert_artifact(
          f"gdoc:{arch_doc_id}",
          kind="gdoc",
          title="Archived Sync Notes",
          url=f"https://docs.google.com/document/d/{arch_doc_id}/edit",
          now=now + 50,
      )
      store.link_artifact(wid_arch, f"gdoc:{arch_doc_id}", role="primary", source="manual")
      store.complete_work(wid_arch, now=now + 55)
      with store._lock, store._connect() as conn:
        conn.execute("UPDATE works SET status = 'archived' WHERE work_id = ?", (wid_arch,))
      res_arch = ingestor.start_chat_for_work(
          store,
          wid_arch,
          cl_state_path=self.cl_state_path,
          projects_dir=self.projects_dir,
          now=now + 60,
      )
      self.assertTrue(res_arch["started"])
      self.assertIn(arch_doc_id, spawned[-1]["prompt"])
      self.assertIn("includeComments: true", spawned[-1]["prompt"])

  # Verifies read-only gdoc chats attach unambiguously and never hijack mixed CL/multi-doc chats
  def test_readonly_gdoc_transcript_attaches_without_hijacking_cl_or_multidoc_chats(self) -> None:
    now = 1794344500.0
    store = work_store.WorkStore(self.db_path)
    doc_id = "1e4GtnB9hkFctLGpprQ_QRkHzVaPXqQF2ZbE-lNZwbk0"
    doc_key = f"gdoc:{doc_id}"
    wid = store.create_work("Notes - Skore/Flutter Sync — Renzo Olivares", now=now)
    store.upsert_artifact(
        doc_key,
        kind="gdoc",
        title="Notes - Skore/Flutter Sync",
        url=f"https://docs.google.com/document/d/{doc_id}/edit",
        now=now,
    )
    store.link_artifact(wid, doc_key, role="primary", source="auto", now=now)

    w_cl = store.create_work("Review Gerrit 1414016", now=now)
    store.upsert_artifact(
        "gerrit:skia:1414016",
        kind="gerrit",
        title="[skparagraph] Measure hit glyph",
        url="https://skia-review.googlesource.com/c/skia/+/1414016",
        now=now,
    )
    store.link_artifact(w_cl, "gerrit:skia:1414016", role="primary", source="auto", now=now)

    def _write_transcript(cid: str, steps: list[dict]) -> str:
      tpath = os.path.join(
          self.tmp_dir.name, "brain", cid, ".system_generated", "logs", "transcript.jsonl"
      )
      os.makedirs(os.path.dirname(tpath), exist_ok=True)
      with open(tpath, "w", encoding="utf-8") as f:
        for st in steps:
          f.write(json.dumps(st, ensure_ascii=False) + "\n")
      return tpath

    def _write_readonly_transcript(cid: str, target_doc_id: str) -> str:
      return _write_transcript(
          cid,
          [
              {
                  "step_index": 0,
                  "type": "USER_INPUT",
                  "created_at": "2026-11-10T21:30:00Z",
                  "content": (
                      f"Прочитай https://docs.google.com/document/d/{target_doc_id}/edit "
                      "и выпиши сегодняшние задачи."
                  ),
              },
              {
                  "step_index": 1,
                  "type": "PLANNER_RESPONSE",
                  "created_at": "2026-11-10T21:30:05Z",
                  "content": "",
                  "tool_calls": [{
                      "name": "call_mcp_tool",
                      "args": {
                          "ServerName": "google_drive_google_drive",
                          "ToolName": "read_file_content",
                          "Arguments": {"fileId": target_doc_id, "includeComments": True},
                      },
                  }],
              },
          ],
      )

    cursors: dict = {}
    cid_existing = "aaaa1111-2222-3333-4444-555566667777"
    tpath_existing = _write_readonly_transcript(cid_existing, doc_id)
    ingestor.ingest_transcript_file(
        store,
        cid_existing,
        cid_existing,
        tpath_existing,
        cursors,
        chat_title="Notes - Skore/Flutter Sync",
        project_id="Reviews of e-mails, meetings and such",
        now=now + 10,
    )
    self.assertEqual(
        store.get_work_artifact_roles(wid).get(f"chat_chain:{cid_existing}"),
        "channel",
    )

    # Unknown gdoc in a read-only session must NOT mint a new Work card
    cid_unknown = "bbbb1111-2222-3333-4444-555566667777"
    tpath_unknown = _write_readonly_transcript(
        cid_unknown, "9999999999999999999999_unknown_doc"
    )
    ingestor.ingest_transcript_file(
        store,
        cid_unknown,
        cid_unknown,
        tpath_unknown,
        cursors,
        chat_title="Random reference doc",
        project_id="Reviews of e-mails, meetings and such",
        now=now + 20,
    )
    self.assertEqual(len(store.list_works()), 2)
    self.assertIsNone(store.find_work_by_artifact(f"chat_chain:{cid_unknown}"))

    # Mixed CL + spec GDoc read-only Turn 1 must NOT hijack onto wid, so Turn 2 binds to w_cl
    cid_mixed = "eeee1111-2222-3333-4444-555566667777"
    mixed_steps = [{
        "step_index": 0,
        "type": "USER_INPUT",
        "created_at": "2026-11-10T21:40:00Z",
        "content": (
            "Проверь https://skia-review.googlesource.com/c/skia/+/1414016 "
            f"по спеке https://docs.google.com/document/d/{doc_id}/edit"
        ),
    }]
    tpath_mixed = _write_transcript(cid_mixed, mixed_steps)
    ingestor.ingest_transcript_file(
        store,
        cid_mixed,
        cid_mixed,
        tpath_mixed,
        cursors,
        chat_title="[21:40] ⦿ Gerrit 1414016 hit glyph review",
        project_id="SkParagraph",
        now=now + 30,
    )
    self.assertIsNone(store.find_work_by_artifact(f"chat_chain:{cid_mixed}"))

    mixed_steps.append({
        "step_index": 1,
        "type": "PLANNER_RESPONSE",
        "created_at": "2026-11-10T21:41:00Z",
        "content": "",
        "tool_calls": [{
            "name": "run_command",
            "args": {
                "CommandLine": (
                    "python3 ~/.gemini/config/scripts/gerrit_draft.py save "
                    "--cl 1414016 --message 'LGTM'"
                )
            },
        }],
    })
    _write_transcript(cid_mixed, mixed_steps)
    ingestor.ingest_transcript_file(
        store,
        cid_mixed,
        cid_mixed,
        tpath_mixed,
        cursors,
        chat_title="[21:40] ⦿ Gerrit 1414016 hit glyph review",
        project_id="SkParagraph",
        now=now + 40,
    )
    self.assertEqual(store.find_work_by_artifact(f"chat_chain:{cid_mixed}"), w_cl)

    # Read-only 2-doc comparison chat (one owned, one unowned) must NOT attach to wid
    cid_twodoc = "ffff1111-2222-3333-4444-555566667777"
    tpath_twodoc = _write_transcript(
        cid_twodoc,
        [{
            "step_index": 0,
            "type": "USER_INPUT",
            "created_at": "2026-11-10T21:45:00Z",
            "content": (
                f"Сравни https://docs.google.com/document/d/{doc_id}/edit "
                "и https://docs.google.com/document/d/2UnownedDocId888888888888/edit"
            ),
        }],
    )
    ingestor.ingest_transcript_file(
        store,
        cid_twodoc,
        cid_twodoc,
        tpath_twodoc,
        cursors,
        chat_title="Compare two sync docs",
        project_id="Reviews of e-mails, meetings and such",
        now=now + 50,
    )
    self.assertIsNone(store.find_work_by_artifact(f"chat_chain:{cid_twodoc}"))

  # Verifies conv_id traversal rejection and finite bounded timeout in open_chat_in_jetski
  def test_open_chat_validation_and_timeout_bounds(self) -> None:
    bad_res = ingestor.open_chat_in_jetski(
        "../outside",
        timeout_sec=0.0,
        req_file=self.open_req_file,
        annotations_dir=self.annotations_dir,
    )
    self.assertFalse(bad_res["ok"])
    self.assertFalse(os.path.exists(self.open_req_file))

    done_evt = threading.Event()

    def _call_inf() -> None:
      ingestor.open_chat_in_jetski(
          "11112222-3333-4444-8888-999900001111",
          timeout_sec=float("inf"),
          req_file=self.open_req_file,
          annotations_dir=self.annotations_dir,
      )
      done_evt.set()

    worker = threading.Thread(target=_call_inf, daemon=True)
    worker.start()
    self.assertTrue(done_evt.wait(timeout=0.35), "open_chat_in_jetski hung on infinite timeout_sec")

  # Verifies /api/chat/open, /c/<cid> redirect, workctl stdout, and embedded + standalone UI JS
  def test_http_endpoints_and_ui_chat_launcher_embedded_and_standalone(self) -> None:
    now = 1794344600.0
    store = work_store.WorkStore(self.db_path)
    wid = store.create_work("Notes - Skore/Flutter Sync — Renzo Olivares", now=now)
    doc_id = "1e4GtnB9hkFctLGpprQ_QRkHzVaPXqQF2ZbE-lNZwbk0"
    store.upsert_artifact(
        f"gdoc:{doc_id}",
        kind="gdoc",
        title="Notes - Skore/Flutter Sync",
        url=f"https://docs.google.com/document/d/{doc_id}/edit",
        now=now,
    )
    store.link_artifact(wid, f"gdoc:{doc_id}", role="primary", source="auto", now=now)

    with open(self.cl_state_path, "w", encoding="utf-8") as f:
      json.dump(
          {
              "items": {
                  "1414016": {
                      "kind": "gerrit",
                      "target_id": "1414016",
                      "host": "skia-review.googlesource.com",
                      "status": "NEW",
                      "subject": "[skparagraph] Measure hit glyph",
                      "url": "https://skia-review.googlesource.com/c/skia/+/1414016",
                  }
              }
          },
          f,
      )

    proj_initial = store.build_dashboard_projection(
        cl_watcher_state=ingestor.load_cl_watcher_snapshot(self.cl_state_path)
    )
    html_str = workbench_server.render_workbench_html(proj_initial, 12345)
    self.assertIn(f'data-start-chat="{wid}"', html_str)
    self.assertIn('data-start-chat="virt-gerrit:skia:1414016"', html_str)

    fake_cid = "cccc1111-2222-3333-4444-555566667777"
    with (
        mock.patch.object(cl, "resolve_handoff_model", return_value={"enum": "MODEL_TEST_PRO"}),
        mock.patch.object(cl, "start_conversation_exact", return_value=fake_cid),
        mock.patch.object(cl, "is_archived", return_value=False),
        mock.patch.object(ingestor, "DEFAULT_PROJECTS_DIR", self.projects_dir),
        mock.patch.object(ingestor, "OPEN_CHAT_REQ_FILE", self.open_req_file),
        mock.patch.object(ingestor, "ANNOTATIONS_DIR", self.annotations_dir),
    ):
      srv = workbench_server.create_server(
          host="127.0.0.1",
          port=0,
          store=store,
          cl_state_path=self.cl_state_path,
          watcher_state_path=self.watcher_state_path,
          info_path=self.info_path,
      )
      port = srv.server_address[1]
      th = threading.Thread(target=srv.serve_forever, daemon=True)
      th.start()
      try:
        def _post(path: str, payload: dict) -> tuple[int, dict]:
          req = urllib.request.Request(
              f"http://127.0.0.1:{port}{path}",
              data=json.dumps(payload).encode("utf-8"),
              headers={"Content-Type": "application/json"},
              method="POST",
          )
          try:
            with urllib.request.urlopen(req, timeout=5) as resp:
              return resp.status, json.loads(resp.read().decode("utf-8"))
          except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

        # Reject invalid conversation_id traversal and malformed timeout_sec with HTTP 400
        st_bad_cid, _ = _post("/api/chat/open", {"conversation_id": "../etc/passwd"})
        self.assertEqual(st_bad_cid, 400)
        st_bad_to, _ = _post(
            "/api/chat/open", {"conversation_id": fake_cid, "timeout_sec": "not-a-float"}
        )
        self.assertEqual(st_bad_to, 400)

        # GET /c/<cid> redirects to Jetski Web box URL so standalone hrefs never 404
        class _NoRedirect(urllib.request.HTTPRedirectHandler):

          def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

        opener = urllib.request.build_opener(_NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as redir_ctx:
          opener.open(f"http://127.0.0.1:{port}/c/{fake_cid}", timeout=5)
        self.assertEqual(redir_ctx.exception.code, 302)
        self.assertTrue(
            str(redir_ctx.exception.headers.get("Location") or "").endswith(f"/c/{fake_cid}")
        )

        # Timeout=0 without .pbtxt touch -> reused_tab=False + writes jetski-open-chat.json
        st_ok0, open_no_ack = _post(
            "/api/chat/open", {"conversation_id": fake_cid, "timeout_sec": 0.0}
        )
        self.assertEqual(st_ok0, 200)
        self.assertTrue(open_no_ack["ok"])
        self.assertFalse(open_no_ack["reused_tab"])
        self.assertTrue(open_no_ack["chat_url"].endswith(f"/c/{fake_cid}"))
        with open(self.open_req_file, "r", encoding="utf-8") as rf:
          req_json = json.load(rf)
        self.assertEqual(req_json["cascadeId"], fake_cid)

        # Simulated Jetski tab .pbtxt ack -> reused_tab=True
        ann_path = os.path.join(self.annotations_dir, f"{fake_cid}.pbtxt")
        with open(ann_path, "w", encoding="utf-8") as af:
          af.write('title:"initial"\n')

        def _touch_ack() -> None:
          time.sleep(0.08)
          with open(ann_path, "w", encoding="utf-8") as af2:
            af2.write('title:"acked"\n')

        threading.Thread(target=_touch_ack, daemon=True).start()
        _, open_acked = _post(
            "/api/chat/open", {"conversation_id": fake_cid, "timeout_sec": 0.6}
        )
        self.assertTrue(open_acked["ok"])
        self.assertTrue(open_acked["reused_tab"])

        # POST /api/work/start_chat creates & links chat_chain:<fake_cid>
        _, started_resp = _post("/api/work/start_chat", {"work_id": wid})
        self.assertEqual(started_resp["started_chat"]["conversation_id"], fake_cid)
        w_after = next(w for w in started_resp["works"] if w["work_id"] == wid)
        self.assertTrue(
            any(a["artifact_key"] == f"chat_chain:{fake_cid}" for a in w_after["artifacts"])
        )

        # CLI workctl start-chat prints valid JSON reusing existing chat cleanly
        cli_buf = io.StringIO()
        with contextlib.redirect_stdout(cli_buf):
          rc = workctl.main([
              "--db",
              self.db_path,
              "start-chat",
              wid,
              "--cl-state",
              self.cl_state_path,
              "--json",
          ])
        self.assertEqual(rc, 0)
        cli_json = json.loads(cli_buf.getvalue())
        self.assertEqual(cli_json["conversation_id"], fake_cid)
        self.assertTrue(cli_json["already_existed"])
      finally:
        srv.shutdown()
        srv.server_close()

    # Verify client JS standalone + embedded sidecar + virtual-card state migration in Node
    script_start = html_str.rindex("<script>") + len("<script>")
    script_end = html_str.rindex("</script>")
    app_js = html_str[script_start:script_end]
    node_code = (
        """
const assert = require('assert');
const els = {};
function makeEl(id) {
  const attrs = {};
  return {
    id,
    innerHTML: '',
    textContent: '',
    value: '',
    checked: true,
    disabled: false,
    dataset: {},
    style: {},
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
    reset() {},
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    addEventListener(ev, fn) { this['_on_' + ev] = fn; },
    dispatch(ev, eObj) { return this['_on_' + ev] ? this['_on_' + ev](eObj) : undefined; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
  };
}
[
  'wb-synced', 'kpi-needs', 'kpi-wait', 'kpi-obs', 'kpi-tray', 'kpi-prec',
  'wb-tray-container', 'wb-cards-container', 'wb-lanes', 'wb-types', 'wb-search',
  'wb-new-btn', 'wb-sync-btn',
].forEach((id) => { els[id] = makeEl(id); });
const bodyEl = makeEl('body');
const openedWindows = [];
const assignedLocations = [];
const parentMessages = [];
const fetchCalls = [];
let nextOpenReused = false;
let nextOpenFail = false;
let blockPopup = false;
const storage = new Map();
storage.set(
  'wb_collapsed_v2',
  JSON.stringify({ overrides: { 'virt-gerrit:skia:1414016': false } })
);
storage.set(
  'wb_peer_fold_v1',
  JSON.stringify({
    modes: { 'virt-gerrit:skia:1414016': 'expanded' },
    overrides: { 'virt-gerrit:skia:1414016::p1': true },
  })
);
global.localStorage = {
  getItem: (k) => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => storage.set(k, String(v)),
};
global.document = {
  body: bodyEl,
  activeElement: null,
  getElementById: (id) => els[id] || (els[id] = makeEl(id)),
  querySelectorAll: () => [],
  createElement: () => {
    let txt = '';
    return {
      set textContent(v) { txt = String(v || ''); },
      get innerHTML() {
        return txt.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      },
    };
  },
};
const selfWin = {
  open: (u, name) => {
    if (blockPopup) return null;
    const rec = { url: u, name };
    openedWindows.push(rec);
    return rec;
  },
  location: { assign: (u) => assignedLocations.push(u) },
  postMessage: () => {},
  addEventListener: () => {},
  getSelection: () => '',
  alert: (msg) => { throw new Error('Unexpected window.alert: ' + msg); },
};
selfWin.parent = selfWin;
global.window = selfWin;
global.setInterval = () => 1;
global.setTimeout = (fn) => { fn(); return 1; };
global.fetch = async (url, opts) => {
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  fetchCalls.push({ url, body });
  if (url === '/api/chat/open') {
    if (nextOpenFail) {
      return { ok: false, status: 500, json: async () => ({ error: 'boom' }) };
    }
    return {
      ok: true,
      json: async () => ({
        ok: true,
        conversation_id: body.conversation_id,
        chat_url: 'https://jetski.corp.google.com/box/host/c/' + body.conversation_id,
        reused_tab: nextOpenReused,
      }),
    };
  }
  if (url === '/api/work/start_chat') {
    const isVirt = String(body.work_id).startsWith('virt-');
    const resolvedWid = isVirt ? 'work-mat1414016' : body.work_id;
    const newCid = isVirt
      ? 'eeee2222-3333-4444-8888-555566667777'
      : 'dddd1111-2222-3333-4444-555566667777';
    return {
      ok: true,
      json: async () => ({
        counts: { needs_you: 1, on_desk: 1, waiting: 0, obligations_open: 0, unanswered: 0 },
        metrics: { link_precision: 1 },
        unanswered_tray: [],
        works: [{
          work_id: resolvedWid,
          title: isVirt ? '[skparagraph] Measure hit glyph' : 'Notes - Skore/Flutter Sync',
          lane: isVirt ? 'on-desk' : 'needs-you',
          is_virtual: false,
          reasons: [],
          obligations: [],
          checklist: [],
          artifacts: [{
            artifact_key: 'chat_chain:' + newCid,
            kind: 'chat_chain',
            role: 'channel',
            title: 'Chat',
            url: '/c/' + newCid,
            state: { cascade_id: newCid, root_cid: newCid },
          }],
        }],
        started_chat: {
          ok: true,
          work_id: resolvedWid,
          conversation_id: newCid,
          chat_url: 'https://jetski.corp.google.com/box/host/c/' + newCid,
        },
      }),
    };
  }
  return { ok: true, json: async () => ({ works: [] }) };
};
"""
        + app_js
        + f"""
(async () => {{
  const cardsHtml0 = els['wb-cards-container'].innerHTML;
  assert(cardsHtml0.includes('data-start-chat="{wid}"'), 'Missing + Chat on gdoc card');
  assert(
    cardsHtml0.includes('data-start-chat="virt-gerrit:skia:1414016"'),
    'Missing + Chat on virtual CL card'
  );

  // 1. Standalone + Chat with reused_tab=false -> opens window('jetski-chat')
  nextOpenReused = false;
  const startBtn = {{
    disabled: false,
    textContent: '+ Chat',
    hasAttribute: (k) => k === 'data-start-chat',
    getAttribute: (k) => (k === 'data-start-chat' ? '{wid}' : null),
  }};
  await document.body.dispatch('click', {{
    altKey: false,
    preventDefault: () => {{}},
    target: {{ closest: (sel) => (sel.includes('button') ? startBtn : null) }},
  }});
  assert.strictEqual(openedWindows.length, 1);
  assert.strictEqual(openedWindows[0].name, 'jetski-chat');
  assert(openedWindows[0].url.endsWith('/c/dddd1111-2222-3333-4444-555566667777'));

  // 2. Popup-blocked window.open(null) falls back to window.location.assign
  blockPopup = true;
  nextOpenFail = true;
  const openLinkFail = {{
    textContent: 'Chat',
    hasAttribute: (k) => k === 'data-open-chat',
    getAttribute: (k) => (
      k === 'data-open-chat' ? 'dddd1111-2222-3333-4444-555566667777' : null
    ),
  }};
  await document.body.dispatch('click', {{
    altKey: false,
    preventDefault: () => {{}},
    target: {{ closest: (sel) => (sel.includes('a') ? openLinkFail : null) }},
  }});
  assert.strictEqual(
    assignedLocations.length, 1, 'Expected location.assign fallback on null popup'
  );
  assert(
    assignedLocations[0].startsWith('http'),
    'Fallback URL must be absolute Jetski Box URL, got: ' + assignedLocations[0]
  );
  blockPopup = false;
  nextOpenFail = false;

  // 3. Virtual card promotion migrates expanded collapse & peer fold state to work-mat1414016
  nextOpenReused = true;
  const virtBtn = {{
    disabled: false,
    textContent: '+ Chat',
    hasAttribute: (k) => k === 'data-start-chat',
    getAttribute: (k) => (k === 'data-start-chat' ? 'virt-gerrit:skia:1414016' : null),
  }};
  await document.body.dispatch('click', {{
    altKey: false,
    preventDefault: () => {{}},
    target: {{ closest: (sel) => (sel.includes('button') ? virtBtn : null) }},
  }});
  const matHtml = els['wb-cards-container'].innerHTML;
  assert(
    matHtml.includes('data-work-id="work-mat1414016"')
      && matHtml.includes('data-collapsed="false"'),
    'Materialized virtual card must stay expanded after + Chat'
  );
  const savedCol = JSON.parse(storage.get('wb_collapsed_v2') || '{{}}');
  assert.strictEqual(savedCol.overrides['work-mat1414016'], false);
  assert.strictEqual(savedCol.overrides['virt-gerrit:skia:1414016'], undefined);
  const savedFold = JSON.parse(storage.get('wb_peer_fold_v1') || '{{}}');
  assert.strictEqual(savedFold.modes['work-mat1414016'], 'expanded');
  assert.strictEqual(savedFold.overrides['work-mat1414016::p1'], true);
  assert.strictEqual(savedFold.modes['virt-gerrit:skia:1414016'], undefined);

  // 4. Embedded sidecar mode (window.parent !== window) posts message and sends timeout_sec=0
  selfWin.parent = {{ postMessage: (msg) => parentMessages.push(msg) }};
  fetchCalls.length = 0;
  const openLinkEmb = {{
    textContent: 'Chat',
    hasAttribute: (k) => k === 'data-open-chat',
    getAttribute: (k) => (
      k === 'data-open-chat' ? 'eeee2222-3333-4444-8888-555566667777' : null
    ),
  }};
  await document.body.dispatch('click', {{
    altKey: false,
    preventDefault: () => {{}},
    target: {{ closest: (sel) => (sel.includes('a') ? openLinkEmb : null) }},
  }});
  assert.strictEqual(parentMessages.length, 1);
  assert.strictEqual(parentMessages[0].type, 'jetski-flight-deck-open-chat');
  assert.strictEqual(parentMessages[0].cascadeId, 'eeee2222-3333-4444-8888-555566667777');
  assert(
    fetchCalls.some((c) => c.url === '/api/chat/open' && c.body.timeout_sec === 0),
    'Embedded sidecar must pass timeout_sec=0'
  );
  assert.strictEqual(openedWindows.length, 1, 'Embedded sidecar must never open popup');
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""
    )
    proc = subprocess.run(["node", "-e", node_code], capture_output=True, text=True, check=False)
    self.assertEqual(proc.returncode, 0, msg=f"Node failed:\n{proc.stderr}\n{proc.stdout}")


if __name__ == "__main__":
  unittest.main()
