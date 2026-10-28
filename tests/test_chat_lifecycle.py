#!/usr/bin/env python3
"""Tests for chat_lifecycle.py."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import mock_open, patch
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))

import chat_lifecycle


class TestChatLifecycle(unittest.TestCase):

  def setUp(self):
    # Handoffs read the project from the environment first and write state
    # files under the roadmaps dir, so neither may leak from the real session
    self.roadmaps_tmp = tempfile.TemporaryDirectory()
    self.env = patch.dict(os.environ, {"JETSKI_ROADMAPS_DIR": self.roadmaps_tmp.name})
    self.env.start()
    os.environ.pop("ANTIGRAVITY_PROJECT_ID", None)
    # Live-dependent checks stream summaries from the LS; tests opt in per case
    self.live = patch.object(chat_lifecycle, "_fetch_live_summaries", return_value={})
    self.live_mock = self.live.start()

  def tearDown(self):
    self.live.stop()
    self.env.stop()
    self.roadmaps_tmp.cleanup()

  def test_clean_base_title_markers(self):
    cases = [
        ("[14:20] ▸ Active Root", "Active Root"),
        ("[14:20] ✓ Completed Step", "Completed Step"),
        ("[14:20] ⦿ Active Task", "Active Task"),
        ("[14:20] « Closed Root", "Closed Root"),
        ("[14:20] ‹✓› Closed Step", "Closed Step"),
        ("[14:20] » Closed End", "Closed End"),
        ("[14:20] «» Single Closed", "Single Closed"),
        ("[14:20 продолжение] Chained Task", "Chained Task"),
        ("продолжение 14:20: Prefix Task", "Prefix Task"),
        ("Trailing Task (продолжение 14:20)", "Trailing Task"),
        ("Plain Simple Title", "Plain Simple Title"),
        ("", ""),
    ]
    for raw, expected in cases:
      with self.subTest(raw=raw):
        self.assertEqual(chat_lifecycle.clean_base_title(raw), expected)

  def test_is_archived_reads_pbtxt(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      ann_dir = os.path.join(tmp_dir, chat_lifecycle.ANNOTATIONS_DIR_NAME)
      os.makedirs(ann_dir)
      for cid, body in (
          ("on", 'title:"[10:00] ⦿ A"  archived:true\n'),
          ("off", 'title:"[10:00] ⦿ B"  archived:false\n'),
          ("plain", 'title:"[10:00] ⦿ C"\n'),
      ):
        with open(os.path.join(ann_dir, f"{cid}.pbtxt"), "w", encoding="utf-8") as f:
          f.write(body)
      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]):
        self.assertTrue(chat_lifecycle.is_archived("on"))
        self.assertFalse(chat_lifecycle.is_archived("off"))
        self.assertFalse(chat_lifecycle.is_archived("plain"))
        self.assertFalse(chat_lifecycle.is_archived("missing"))

  def test_update_conversation_title_disk(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      ann_dir = os.path.join(tmp_dir, "annotations")
      os.makedirs(ann_dir, exist_ok=True)
      cid = "test-conv-123"

      # Pre-create SQLite DB
      db_path = os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME)
      conn = sqlite3.connect(db_path)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)"
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?)", (cid, "Old Title")
        )
      conn.close()

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False):
        ok = chat_lifecycle.update_conversation_title(cid, "[12:00] ⦿ New Title")
        self.assertTrue(ok)

        # Check DB updated
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT title FROM conversation_summaries WHERE conversation_id = ?", (cid,))
        row = cur.fetchone()
        conn.close()
        self.assertEqual(row[0], "[12:00] ⦿ New Title")

        # Check annotation file created/updated
        ann_file = os.path.join(ann_dir, f"{cid}.pbtxt")
        self.assertTrue(os.path.isfile(ann_file))
        with open(ann_file, "r", encoding="utf-8") as f:
          content = f.read()
        self.assertIn('title:"[12:00] ⦿ New Title"', content)

  def test_archive_chain_archives_every_piece_and_unarchive_round_trips(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.write_chain(tmp_dir, [("c1", None), ("c2", "c1"), ("c3", "c2")])
      self.write_titles(tmp_dir, {
          "c1": "[10:00] ⦿ First",
          "c2": "[10:30] ⦿ Second",
          "c3": "[11:00] ⦿ Third",
          "other": "[09:00] ⦿ Other",
      })
      rpc_calls = []

      def fake_ls_rpc(method, body, timeout=20.0):
        rpc_calls.append((method, body))
        return {}

      ann_dir = os.path.join(tmp_dir, chat_lifecycle.ANNOTATIONS_DIR_NAME)
      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "ls_rpc", side_effect=fake_ls_rpc):
        self.assertEqual(chat_lifecycle.archive_chain("c2"), ["c1", "c2", "c3"])
        self.assertEqual(rpc_calls[0][1]["cascadeIds"], ["c1", "c2", "c3"])
        self.assertEqual(rpc_calls[0][1]["annotations"], {"archived": True})
        for cid, title in (("c1", "[10:00] ⦿ First"), ("c3", "[11:00] ⦿ Third")):
          with open(os.path.join(ann_dir, f"{cid}.pbtxt"), encoding="utf-8") as f:
            content = f.read()
          self.assertIn("archived:true", content)
          self.assertIn(f'title:"{title}"', content)
          self.assertTrue(chat_lifecycle.is_archived(cid))
          self.assertEqual(chat_lifecycle.get_conversation_title(cid), title)
        self.assertFalse(chat_lifecycle.is_archived("other"))
        self.assertTrue(
            os.path.isfile(os.path.join(tmp_dir, "brain", chat_lifecycle.CHAIN_INDEX_FILE_NAME))
        )

        res = chat_lifecycle.archive_conversations(["c2"], archived=False)
        self.assertEqual(res["archived_ids"], ["c2"])
        self.assertEqual(rpc_calls[-1][1]["annotations"], {"archived": False})
        with open(os.path.join(ann_dir, "c2.pbtxt"), encoding="utf-8") as f:
          content = f.read()
        self.assertIn("archived:false", content)
        self.assertNotIn("archived:true", content)
        self.assertFalse(chat_lifecycle.is_archived("c2"))
        self.assertTrue(chat_lifecycle.is_archived("c1"))
        self.assertTrue(chat_lifecycle.is_archived("c3"))

  def test_reopen_conversation(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.write_titles(tmp_dir, {
          "c1": "[10:00] «» Closed Solo",
          "c2": "[10:05] ⏳ Cron Watch",
          "c3": "[10:10] ⦿ Already Active",
      })
      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False), \
           patch.object(chat_lifecycle, "get_current_time_str", return_value="12:00"):
        chat_lifecycle.archive_conversations(["c1", "c2", "c3"])

        self.assertTrue(chat_lifecycle.reopen_conversation("c1"))
        self.assertFalse(chat_lifecycle.is_archived("c1"))
        self.assertEqual(chat_lifecycle.get_conversation_title("c1"), "[10:00] ⦿ Closed Solo")

        self.assertTrue(chat_lifecycle.reopen_conversation("c2"))
        self.assertFalse(chat_lifecycle.is_archived("c2"))
        self.assertEqual(chat_lifecycle.get_conversation_title("c2"), "[10:05] ⏳ Cron Watch")

        self.assertTrue(chat_lifecycle.reopen_conversation("c3"))
        self.assertFalse(chat_lifecycle.is_archived("c3"))
        self.assertEqual(chat_lifecycle.get_conversation_title("c3"), "[10:10] ⦿ Already Active")

        self.assertFalse(chat_lifecycle.reopen_conversation("missing"))
        self.assertFalse(chat_lifecycle.is_archived("missing"))

  def test_discover_conversation_chain(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      # Simulate brain directories with transcripts: c1 -> c2 -> c3
      for cid, parent in [("c1", None), ("c2", "c1"), ("c3", "c2")]:
        log_dir = os.path.join(tmp_dir, "brain", cid, ".system_generated", "logs")
        os.makedirs(log_dir, exist_ok=True)
        tr_file = os.path.join(log_dir, "transcript.jsonl")
        content = (
            f'{{"type": "USER_INPUT", "content": "Continuing work from previous conversation (conversation://{parent})..."}}\n'
            if parent else '{"type": "USER_INPUT", "content": "Initial prompt"}\n'
        )
        with open(tr_file, "w", encoding="utf-8") as f:
          f.write(content)

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]):
        chain = chat_lifecycle.discover_conversation_chain("c3")
        self.assertEqual(chain, ["c1", "c2", "c3"])

        # And expanding chain with ancestors
        expanded = chat_lifecycle.expand_chain_with_ancestors(["c2", "c3"])
        self.assertEqual(expanded, ["c1", "c2", "c3"])


  def test_set_conversation_topic(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      db_path = os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME)
      conn = sqlite3.connect(db_path)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)"
        )
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", ("c1", "Old Title"))
      conn.close()

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False), \
           patch.object(chat_lifecycle, "get_current_time_str", return_value="15:30"):

        # 1. Clean topic with active marker
        new_title = chat_lifecycle.set_conversation_topic("c1", "Тест № 2")
        self.assertEqual(new_title, "[15:30] ⦿ Тест № 2")
        self.assertEqual(chat_lifecycle.get_conversation_title("c1"), "[15:30] ⦿ Тест № 2")

        # 2. Strips old markers if provided in input
        new_title2 = chat_lifecycle.set_conversation_topic("c1", "[10:00] » Тест № 2")
        self.assertEqual(new_title2, "[15:30] ⦿ Тест № 2")

  def test_extract_entity_topic(self):
    cases = [
        ("Посмотри на СЛ https://skia-review.git.corp.google.com/c/skia/+/1397896", "cl/1397896"),
        ("Check https://chromium-review.googlesource.com/c/chromium/src/+/5901234", "cl/5901234"),
        ("Look at https://github.com/flutter/engine/pull/55123 please", "PR #55123"),
        ("Issue https://github.com/flutter/flutter/issues/99887", "Issue #99887"),
        ("Fix bug b/345678901 in layout", "b/345678901"),
        ("Inspect crbug.com/1234567", "crbug/1234567"),
        ("Broken in commit a1b2c3d4e5f678", "commit a1b2c3d4"),
        ("Check cl/1397896", "cl/1397896"),
        ("СЛ 1397896 что там происходит", "cl/1397896"),
        ("PR #45678 review", "PR #45678"),
        ('Проведи ревью: @[skia | 1402416:Reland "[SkContext] Forwarding"]', "cl/1402416"),
        ("глянь @[flutter/flutter | 150000:Fix text]", None),
        ("сделай 1402416 тестов", None),
        ("Just a normal conversation without entities", None),
        # Quoted or pasted lifecycle titles must be stripped before entity extraction
        (
            "Вот чат [18:51] ⦿ cl/1405639 Issue #195102 fontconfig запустил агента и ждет",
            None,
        ),
        ('Why is "[18:51] ⦿ cl/1405639 Issue #195102 fontconfig" waiting?', None),
        ("Look at [18:51] ⦿ cl/1405639, please check cl/999999", "cl/999999"),
        ("Compare `[18:51] ⦿ cl/1405639` with cl/888888", "cl/888888"),
    ]
    for prompt, expected in cases:
      with self.subTest(prompt=prompt):
        self.assertEqual(chat_lifecycle.extract_entity_topic(prompt), expected)

  def test_auto_title_conversation(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      db_path = os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME)
      conn = sqlite3.connect(db_path)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)"
        )
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", ("c1", "Untitled"))
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?)", ("c2", "[10:00] ⦿ Custom Topic")
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?)", ("c3", "[10:00] ⦿ Investigation")
        )
      conn.close()

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False), \
           patch.object(chat_lifecycle, "get_current_time_str", return_value="14:15"):

        # 1. Untitled chat with entity prompt gets auto-titled
        res = chat_lifecycle.auto_title_conversation(
            "c1", "Посмотри на СЛ https://skia-review.git.corp.google.com/c/skia/+/1397896"
        )
        self.assertEqual(res, "[14:15] ⦿ cl/1397896")
        self.assertEqual(chat_lifecycle.get_conversation_title("c1"), "[14:15] ⦿ cl/1397896")

        # 2. Already titled chat with a valid multi-word topic is never overwritten
        res2 = chat_lifecycle.auto_title_conversation("c2", "Look at PR #12345")
        self.assertEqual(res2, "")
        self.assertEqual(chat_lifecycle.get_conversation_title("c2"), "[10:00] ⦿ Custom Topic")

        # 3. Chat with generic/single-word title IS overwritten by auto_title
        res_generic = chat_lifecycle.auto_title_conversation(
            "c3", "Look at PR #55123 multi-span BiDi"
        )
        self.assertEqual(res_generic, "[14:15] ⦿ PR #55123 multi-span BiDi")
        self.assertEqual(
            chat_lifecycle.get_conversation_title("c3"),
            "[14:15] ⦿ PR #55123 multi-span BiDi",
        )

        # 4. Prompt without entity does not change title
        res3 = chat_lifecycle.auto_title_conversation("c1", "Just some random question")
        self.assertEqual(res3, "")

  def test_create_handoff(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      db_path = os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME)
      conn = sqlite3.connect(db_path)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, project_id TEXT)"
        )
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)", ("parent-1", "[10:00] ⦿ My Feature", "outside-of-project"))
      conn.close()

      mock_run_res = unittest.mock.MagicMock()
      mock_run_res.returncode = 0
      mock_run_res.stdout = '{"conversationId": "child-2"}'

      mock_meta_res = unittest.mock.MagicMock()
      mock_meta_res.returncode = 0
      mock_meta_res.stdout = '{"response": {"conversationMetadata": {"metadata": {"sourceMetadata": null}}}}'

      def fake_run(cmd, **kwargs):
        if "new-conversation" in cmd:
          return mock_run_res
        if "get-conversation-metadata" in cmd:
          return mock_meta_res
        return unittest.mock.MagicMock(returncode=0, stdout="")

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False), \
           patch.object(chat_lifecycle, "ls_rpc", side_effect=RuntimeError("offline in test")), \
           patch.object(chat_lifecycle, "get_current_time_str", return_value="15:45"), \
           patch("subprocess.run", side_effect=fake_run):

        summary_file = os.path.join(tmp_dir, "brain", "parent-1", "handoff_summary.md")
        res = chat_lifecycle.create_handoff(
            current_conv_id="parent-1",
            summary_file=summary_file,
            next_step_prompt="Do the next check",
        )

        self.assertEqual(res["new_conversation_id"], "child-2")
        self.assertTrue(res["verified_visible"])
        self.assertTrue(os.path.isfile(summary_file))
        # The caller is archived even though child-2 has no transcript.jsonl yet
        self.assertTrue(chat_lifecycle.is_archived("parent-1"))
        self.assertEqual(chat_lifecycle.get_conversation_title("parent-1"), "[10:00] ⦿ My Feature")
        self.assertFalse(chat_lifecycle.is_archived("child-2"))
        self.assertEqual(chat_lifecycle.get_conversation_title("child-2"), "[15:45] ⦿ My Feature")

  def test_create_handoff_multi_step_chain(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      db_path = os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME)
      conn = sqlite3.connect(db_path)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, project_id TEXT)"
        )
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)", ("root-0", "[09:30] ▸ My Feature", "proj-abc"))
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)", ("parent-1", "[10:00] ⦿ My Feature", "proj-abc"))
      conn.close()

      # Only root-0 and parent-1 have transcripts on disk; child-2 will not have one at handoff time
      for cid, parent in [("root-0", None), ("parent-1", "root-0")]:
        log_dir = os.path.join(tmp_dir, "brain", cid, ".system_generated", "logs")
        os.makedirs(log_dir, exist_ok=True)
        tr_file = os.path.join(log_dir, "transcript.jsonl")
        content = (
            f'{{"type": "USER_INPUT", "content": "Continuing work (conversation://{parent})"}}\n'
            if parent else '{"type": "USER_INPUT", "content": "Initial prompt"}\n'
        )
        with open(tr_file, "w", encoding="utf-8") as f:
          f.write(content)

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False), \
           patch.object(chat_lifecycle, "resolve_handoff_model", return_value={"enum": "M1", "id": "m1", "label": "M1", "source": "inherited"}), \
           patch.object(chat_lifecycle, "start_conversation_exact", return_value="child-2"), \
           patch.object(chat_lifecycle, "get_conversation_source_metadata", return_value=(True, None)), \
           patch.object(chat_lifecycle, "get_current_time_str", return_value="15:45"):

        res = chat_lifecycle.create_handoff(
            current_conv_id="parent-1",
            notes="Step 1 done",
            next_step_prompt="Run step 2",
        )
        self.assertEqual(res["new_conversation_id"], "child-2")
        self.assertTrue(res["verified_visible"])
        self.assertEqual(chat_lifecycle.get_conversation_title("root-0"), "[09:30] ▸ My Feature")
        self.assertFalse(chat_lifecycle.is_archived("root-0"))
        self.assertEqual(chat_lifecycle.get_conversation_title("parent-1"), "[10:00] ⦿ My Feature")
        self.assertTrue(chat_lifecycle.is_archived("parent-1"))
        self.assertEqual(chat_lifecycle.get_conversation_title("child-2"), "[15:45] ⦿ My Feature")
        self.assertFalse(chat_lifecycle.is_archived("child-2"))

  def test_get_chain_status(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      db_path = os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME)
      conn = sqlite3.connect(db_path)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)"
        )
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", ("c1", "[10:00] ▸ Step 1"))
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", ("c2", "[10:15] ⦿ Step 2"))
      conn.close()

      # Summary is always written into the *predecessor's* brain dir (c1),
      # so status for the continuation (c2) must look across the whole chain.
      c1_brain = os.path.join(tmp_dir, "brain", "c1")
      os.makedirs(c1_brain)
      summary_path = os.path.join(c1_brain, "handoff_summary_Step_1_c1.md")
      with open(summary_path, "w", encoding="utf-8") as f:
        f.write("# summary\n")
      # Sidecar metadata must not be reported as a summary.
      with open(summary_path + ".metadata.json", "w", encoding="utf-8") as f:
        f.write("{}")

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "ls_rpc", side_effect=RuntimeError("offline in test")), \
           patch.object(chat_lifecycle, "discover_conversation_chain", return_value=["c1", "c2"]):
        status = chat_lifecycle.get_chain_status("c2")
        self.assertEqual(status["current_conversation_id"], "c2")
        self.assertEqual(status["chain_length"], 2)
        self.assertFalse(status["is_archived"])
        self.assertFalse(status["chain"][1]["is_archived"])
        self.assertEqual(status["chain"][0]["conversation_id"], "c1")
        self.assertEqual(status["chain"][1]["conversation_id"], "c2")
        self.assertTrue(status["chain"][1]["is_current"])
        self.assertEqual(status["summary_files"], [summary_path])

        chat_lifecycle.archive_conversations(["c1"])
        status = chat_lifecycle.get_chain_status("c2")
        self.assertTrue(status["chain"][0]["is_archived"])
        self.assertFalse(status["chain"][1]["is_archived"])
        self.assertFalse(status["is_archived"])
        self.assertTrue(chat_lifecycle.get_chain_status("c1")["is_archived"])

  def test_get_handoff_summary_path(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]):
        p = chat_lifecycle.get_handoff_summary_path("c12345678", "Тест № 3")
        self.assertIn("handoff_summary_Тест_3_c1234567.md", p)

  def test_create_handoff_with_notes(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      db_path = os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME)
      conn = sqlite3.connect(db_path)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, project_id TEXT)"
        )
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)", ("parent-notes", "[10:00] ⦿ Parent Task", "outside-of-project"))
      conn.close()

      mock_run_res = unittest.mock.MagicMock(returncode=0, stdout='{"conversationId": "child-notes"}')
      mock_meta_res = unittest.mock.MagicMock(returncode=0, stdout='{"response": {"conversationMetadata": {"metadata": {"sourceMetadata": null}}}}')

      def fake_run(cmd, **kwargs):
        if "new-conversation" in cmd:
          return mock_run_res
        if "get-conversation-metadata" in cmd:
          return mock_meta_res
        return unittest.mock.MagicMock(returncode=0, stdout="")

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False), \
           patch.object(chat_lifecycle, "ls_rpc", side_effect=RuntimeError("offline in test")), \
           patch.object(chat_lifecycle, "get_current_time_str", return_value="15:55"), \
           patch("subprocess.run", side_effect=fake_run):

        res = chat_lifecycle.create_handoff(
            current_conv_id="parent-notes",
            notes="Completed part 1, now starting part 2",
        )
        self.assertEqual(res["new_conversation_id"], "child-notes")
        summary_file = res["summary_file"]
        self.assertTrue(os.path.isfile(summary_file))
        with open(summary_file, "r") as f:
          content = f.read()
        self.assertIn("Completed part 1, now starting part 2", content)
        self.assertTrue(chat_lifecycle.is_archived("parent-notes"))
        self.assertEqual(
            chat_lifecycle.get_conversation_title("parent-notes"), "[10:00] ⦿ Parent Task"
        )
        self.assertEqual(chat_lifecycle.get_conversation_title("child-notes"), "[15:55] ⦿ Parent Task")

  def make_project_db(self, tmp_dir: str, cid: str = "parent-1") -> None:
    conn = sqlite3.connect(os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME))
    with conn:
      conn.execute(
          "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, project_id TEXT)"
      )
      conn.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)", (cid, "[10:00] ⦿ My Feature", "proj-abc"))
    conn.close()

  def handoff_patches(self, tmp_dir: str, start_mock):
    return (
        patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]),
        patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False),
        patch.object(chat_lifecycle, "resolve_handoff_model", return_value={"enum": "M1", "id": "m1", "label": "M1", "source": "inherited"}),
        patch.object(chat_lifecycle, "start_conversation_exact", start_mock),
        patch.object(chat_lifecycle, "get_conversation_source_metadata", return_value=(True, None)),
        patch.object(chat_lifecycle, "get_current_time_str", return_value="15:45"),
    )

  def test_in_project_handoff_moves_state_file_without_roadmap_dump(self):
    import roadmap

    with tempfile.TemporaryDirectory() as tmp_dir:
      self.make_project_db(tmp_dir)
      roadmap.add_item("proj-abc", "parent-1", "Ship it")
      roadmap.set_status("proj-abc", "parent-1", "R1", "doing")
      old_state = roadmap.state_path("proj-abc", "parent-1")
      os.makedirs(os.path.dirname(old_state))
      with open(old_state, "w", encoding="utf-8") as f:
        f.write("# State\nnext: rerun test_foo\n")
      start = unittest.mock.MagicMock(return_value="child-2")
      p = self.handoff_patches(tmp_dir, start)
      with p[0], p[1], p[2], p[3], p[4], p[5]:
        res = chat_lifecycle.create_handoff(current_conv_id="parent-1", next_step_prompt="rerun test_foo")

      new_state = roadmap.state_path("proj-abc", "child-2")
      self.assertEqual(res["summary_file"], new_state)
      self.assertFalse(os.path.exists(old_state))
      with open(new_state, encoding="utf-8") as f:
        self.assertEqual(f.read(), "# State\nnext: rerun test_foo\n")
      self.assertNotIn("state_move_error", res)
      self.assertNotIn("roadmap", res)
      prompt = start.call_args[0][2]
      self.assertIn("roadmap.py show", prompt)
      self.assertIn(os.path.dirname(new_state), prompt)
      self.assertIn("Verify the recorded state", prompt)
      self.assertIn("conversation://parent-1", prompt)
      self.assertEqual(chat_lifecycle.HANDOFF_PARENT_RE.search(prompt).group(1), "parent-1")
      parent_dir = os.path.join(tmp_dir, "brain", "parent-1")
      self.assertEqual(os.listdir(parent_dir), ["scratch"])
      with open(os.path.join(parent_dir, "scratch", ".context_guard_state.json"), encoding="utf-8") as f:
        guard = json.load(f)
      self.assertEqual(guard["handed_off_to"], "child-2")
      self.assertEqual(guard["handoff_kind"], "handoff")
      with open(os.path.join(tmp_dir, "brain", chat_lifecycle.CHAIN_INDEX_FILE_NAME),
                encoding="utf-8") as f:
        index = json.load(f)
      self.assertEqual(index["redirects"]["parent-1"]["to"], "child-2")
      self.assertEqual(index["parentOf"]["child-2"], "parent-1")

      with patch("sys.stdout", new_callable=io.StringIO) as out:
        chat_lifecycle.print_handoff_result(res)
      printed = out.getvalue()
      self.assertNotIn("Ship it", printed)
      self.assertEqual(json.loads(printed)["new_conversation_id"], "child-2")

  def test_in_project_handoff_without_state_file_fails_and_writes_no_stub(self):
    import roadmap

    with tempfile.TemporaryDirectory() as tmp_dir:
      self.make_project_db(tmp_dir)
      start = unittest.mock.MagicMock(return_value="child-2")
      p = self.handoff_patches(tmp_dir, start)
      with p[0], p[1], p[2], p[3], p[4], p[5]:
        with self.assertRaisesRegex(RuntimeError, "state file"):
          chat_lifecycle.create_handoff(current_conv_id="parent-1", next_step_prompt="x")
      start.assert_not_called()
      self.assertFalse(os.path.exists(roadmap.state_path("proj-abc", "parent-1")))
      self.assertFalse(os.path.exists(os.path.join(self.roadmaps_tmp.name, "proj-abc", "roadmap.json")))
      self.assertFalse(os.path.exists(os.path.join(tmp_dir, "brain", "parent-1")))

  def test_in_project_handoff_seeds_state_from_given_summary(self):
    import roadmap

    with tempfile.TemporaryDirectory() as tmp_dir:
      self.make_project_db(tmp_dir)
      summary = os.path.join(tmp_dir, "handoff_summary_x.md")
      with open(summary, "w", encoding="utf-8") as f:
        f.write("# Summary written under the outside flow\n")
      p = self.handoff_patches(tmp_dir, unittest.mock.MagicMock(return_value="child-2"))
      with p[0], p[1], p[2], p[3], p[4], p[5]:
        res = chat_lifecycle.create_handoff(current_conv_id="parent-1", summary_file=summary)
      with open(roadmap.state_path("proj-abc", "child-2"), encoding="utf-8") as f:
        self.assertEqual(f.read(), "# Summary written under the outside flow\n")
      self.assertNotIn("roadmap", res)

  def test_in_project_handoff_prefers_db_project_over_stale_env(self):
    import roadmap

    with tempfile.TemporaryDirectory() as tmp_dir:
      self.make_project_db(tmp_dir)
      old_state = roadmap.state_path("proj-abc", "parent-1")
      os.makedirs(os.path.dirname(old_state))
      with open(old_state, "w", encoding="utf-8") as f:
        f.write("# State after mid-turn move\n")
      start = unittest.mock.MagicMock(return_value="child-2")
      p = self.handoff_patches(tmp_dir, start)
      with patch.dict(os.environ, {"ANTIGRAVITY_PROJECT_ID": "stale-old-project"}), \
           p[0], p[1], p[2], p[3], p[4], p[5]:
        res = chat_lifecycle.create_handoff(current_conv_id="parent-1", next_step_prompt="go")
      self.assertEqual(res["summary_file"], roadmap.state_path("proj-abc", "child-2"))
      self.assertEqual(start.call_args.args[3], "proj-abc")

  def run_rpc_handoff(self, tmp_dir: str, extra_patches=()):
    import roadmap

    sent = []

    def fake_rpc(method, body, timeout=20.0):
      if method == "StartCascade":
        return {"cascadeId": "child-2"}
      if method == "SendUserCascadeMessage":
        sent.append(os.path.isfile(roadmap.state_path("proj-abc", "child-2")))
      return {}

    p = self.handoff_patches(tmp_dir, chat_lifecycle.start_conversation_exact)
    with contextlib.ExitStack() as stack:
      for cm in (*p, patch.object(chat_lifecycle, "ls_rpc", side_effect=fake_rpc), *extra_patches):
        stack.enter_context(cm)
      return sent, chat_lifecycle.create_handoff(current_conv_id="parent-1")

  def write_old_state(self) -> str:
    import roadmap

    old_state = roadmap.state_path("proj-abc", "parent-1")
    os.makedirs(os.path.dirname(old_state), exist_ok=True)
    with open(old_state, "w", encoding="utf-8") as f:
      f.write("# State\n")
    return old_state

  def test_rpc_handoff_moves_state_before_first_message(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.make_project_db(tmp_dir)
      self.write_old_state()
      sent, res = self.run_rpc_handoff(tmp_dir)
      self.assertEqual(sent, [True])
      self.assertEqual(res["new_conversation_id"], "child-2")
      self.assertNotIn("state_move_error", res)

  def test_rpc_handoff_aborts_without_message_when_state_move_fails(self):
    import roadmap

    with tempfile.TemporaryDirectory() as tmp_dir:
      self.make_project_db(tmp_dir)
      old_state = self.write_old_state()
      with open(roadmap.state_path("proj-abc", "child-2"), "w", encoding="utf-8") as f:
        f.write("foreign\n")
      archive = unittest.mock.MagicMock()
      run = unittest.mock.MagicMock()
      with self.assertRaisesRegex(RuntimeError, "no continuation was started"):
        self.run_rpc_handoff(tmp_dir, (
            patch.object(chat_lifecycle, "archive_conversations", archive),
            patch("subprocess.run", run),
        ))
      archive.assert_called_once_with(["child-2"])
      self.assertFalse(any("new-conversation" in c.args[0] for c in run.call_args_list))
      self.assertTrue(os.path.isfile(old_state))

  def test_handoff_notes_are_not_duplicated_on_retry(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.make_project_db(tmp_dir)
      old_state = self.write_old_state()
      p = self.handoff_patches(tmp_dir, unittest.mock.MagicMock(side_effect=RuntimeError("down")))
      with p[0], p[1], p[2], p[3], p[4], p[5], \
           patch("subprocess.run", return_value=unittest.mock.MagicMock(returncode=1, stdout="", stderr="x")):
        for _ in range(2):
          with self.assertRaises(RuntimeError):
            chat_lifecycle.create_handoff(current_conv_id="parent-1", notes="tests green")
      with open(old_state, encoding="utf-8") as f:
        self.assertEqual(f.read().count("tests green"), 1)

  def test_discover_chain_descendants_and_ignore_split_outs(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      # c1 -> c2 -> c3 (handoffs), plus split-1 ("split out of conversation://c1") and sub-1 (SYSTEM_MESSAGE)
      entries = [
          ("c1", '{"type": "USER_INPUT", "step_index": 0, "content": "Initial prompt"}\n'),
          ("c2", '{"type": "USER_INPUT", "step_index": 0, "content": "Continuing work from previous conversation (conversation://c1)"}\n'),
          ("c3", '{"type": "USER_INPUT", "step_index": 0, "content": "Продолжаем работу из предыдущего чата (conversation://c2)"}\n'),
          ("split-1", '{"type": "USER_INPUT", "step_index": 0, "content": "Assigned bug category, split out of conversation://c1"}\n'),
          ("sub-1", '{"type": "SYSTEM_MESSAGE", "step_index": 0, "content": "Subagent"}\n{"type": "GENERIC", "step_index": 2, "content": "USER_INPUT conversation://c1"}\n'),
      ]
      for cid, content in entries:
        log_dir = os.path.join(tmp_dir, "brain", cid, ".system_generated", "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "transcript.jsonl"), "w", encoding="utf-8") as f:
          f.write(content)

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]):
        self.assertEqual(chat_lifecycle.discover_conversation_chain("c1"), ["c1", "c2", "c3"])
        self.assertEqual(chat_lifecycle.discover_conversation_chain("c2"), ["c1", "c2", "c3"])
        self.assertEqual(chat_lifecycle.discover_conversation_chain("c3"), ["c1", "c2", "c3"])
        self.assertEqual(chat_lifecycle.discover_conversation_chain("split-1"), ["split-1"])

  def write_chain(self, tmp_dir: str, links: list[tuple[str, str | None]]) -> None:
    for cid, parent in links:
      log_dir = os.path.join(tmp_dir, "brain", cid, ".system_generated", "logs")
      os.makedirs(log_dir, exist_ok=True)
      with open(os.path.join(log_dir, "transcript.jsonl"), "w", encoding="utf-8") as f:
        if parent:
          f.write(f'{{"type": "USER_INPUT", "step_index": 0, "content": "Continuing work (conversation://{parent})"}}\n')
        else:
          f.write('{"type": "USER_INPUT", "step_index": 0, "content": "Start"}\n')

  def write_titles(self, tmp_dir: str, titles: dict[str, str], project: str = "proj-1") -> None:
    ann_dir = os.path.join(tmp_dir, "annotations")
    os.makedirs(ann_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME))
    with conn:
      conn.execute(
          "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, project_id TEXT)"
      )
      for cid, title in titles.items():
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)", (cid, title, project))
        with open(os.path.join(ann_dir, f"{cid}.pbtxt"), "w", encoding="utf-8") as f:
          f.write(f'title:"{title}"\n')
    conn.close()

  def test_discover_chain_follows_links_across_topics(self):
    # A handoff after finalization reactivates the chain under a new topic;
    # the finalized tail and the topic change must not split it
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.write_chain(tmp_dir, [("c1", None), ("c2", "c1"), ("c3", "c2")])
      self.write_titles(tmp_dir, {
          "c1": "[10:00] « Topic A",
          "c2": "[10:30] » Topic A",
          "c3": "[11:00] ▸ Topic B",
      })
      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]):
        for cid in ("c1", "c2", "c3"):
          self.assertEqual(chat_lifecycle.discover_conversation_chain(cid), ["c1", "c2", "c3"])
        status = chat_lifecycle.get_chain_status("c3")
        self.assertEqual([i["conversation_id"] for i in status["chain"]], ["c1", "c2", "c3"])
        self.assertEqual([i["base_topic"] for i in status["chain"]], ["Topic A", "Topic A", "Topic B"])

  def test_audit_is_clean_on_mixed_topic_chain(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.write_chain(tmp_dir, [("c1", None), ("c2", "c1"), ("c3", "c2")])
      titles = {
          "c1": "[10:00] ⦿ Topic A",
          "c2": "[10:30] ⦿ Topic B",
          "c3": "[11:00] ⦿ Topic C",
      }
      self.write_titles(tmp_dir, titles)
      fake_ls = {
          cid: {
              "summary": t,
              "annotations": {"title": t},
              "trajectoryMetadata": {"projectId": "proj-1", "createdAt": f"2026-10-01T1{i}:00:00Z"},
          }
          for i, (cid, t) in enumerate(titles.items())
      }
      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "ls_rpc", side_effect=RuntimeError("offline in test")), \
           patch.object(chat_lifecycle, "_fetch_ls_summaries_for_audit", return_value=fake_ls):
        report = chat_lifecycle.audit_conversations(fix=False)
        self.assertEqual(report["total_chains"], 1)
        self.assertEqual(report["total_visible_chats"], 3)
        self.assertEqual(
            [(d["conversation_id"], d["issues"], d["title"], d["fixed"])
             for d in report["discrepancies"]],
            [("c1", ["should_be_archived"], titles["c1"], False),
             ("c2", ["should_be_archived"], titles["c2"], False)],
        )
        self.assertFalse(chat_lifecycle.is_archived("c1"))

        chat_lifecycle.archive_conversations(["c1", "c2"])
        report = chat_lifecycle.audit_conversations(fix=False)
        self.assertEqual(report["total_chains"], 1)
        self.assertEqual(report["total_visible_chats"], 1)
        self.assertEqual(report["discrepancies"], [])

  def test_forked_chain_archives_only_handed_off_pieces(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.write_chain(tmp_dir, [("root", None), ("a", "root"), ("b", "root")])
      titles = {
          "root": "[09:00] ⦿ Forked Topic",
          "a": "[10:00] ⦿ Forked Topic",
          "b": "[11:00] ⏳ Forked Topic",
      }
      self.write_titles(tmp_dir, titles)
      fake_ls = {
          cid: {"annotations": {"title": t}, "trajectoryMetadata": {"projectId": "proj-1"}}
          for cid, t in titles.items()
      }
      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "ls_rpc", side_effect=RuntimeError("offline in test")), \
           patch.object(chat_lifecycle, "_fetch_ls_summaries_for_audit", return_value=fake_ls):
        report = chat_lifecycle.audit_conversations(fix=False)
        self.assertEqual(
            [(d["conversation_id"], d["issues"]) for d in report["discrepancies"]],
            [("root", ["should_be_archived"])],
        )
        self.assertEqual(sorted(chat_lifecycle.archive_chain("a")), ["a", "root"])
        self.assertFalse(chat_lifecycle.is_archived("b"))

  def test_handoff_keeps_caller_visible_when_continuation_is_not_verified(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      ctx, launched = self._outside_project_handoff_env(tmp_dir)
      with ctx, patch.object(
          chat_lifecycle, "get_conversation_source_metadata", return_value=(True, {"tool": "x"})
      ):
        res = chat_lifecycle.create_handoff("parent-1", next_step_prompt="go")
        self.assertFalse(res["verified_visible"])
        self.assertFalse(chat_lifecycle.is_archived("parent-1"))

  def test_parse_handoff_args_topic(self):
    cid = "12345678-1234-1234-1234-123456789abc"
    parsed = chat_lifecycle.parse_handoff_args([cid, "--topic", "Next Item", "--next", "go"])
    self.assertEqual(parsed["cid"], cid)
    self.assertEqual(parsed["new_topic"], "Next Item")
    self.assertEqual(parsed["next_step"], "go")
    parsed = chat_lifecycle.parse_handoff_args([cid, "--topic=Other"])
    self.assertEqual(parsed["new_topic"], "Other")

  def test_parse_handoff_args_inline_values_and_positionals(self):
    cid = "12345678-1234-1234-1234-123456789abc"
    parsed = chat_lifecycle.parse_handoff_args([
        cid, "/tmp/sum.md", "--notes=a=b", "--next=step 2", "--model=gemini-x", "--topic=T",
    ])
    self.assertEqual(parsed, {
        "cid": cid, "summary": "/tmp/sum.md", "notes": "a=b", "next_step": "step 2",
        "new_topic": "T", "exact_model": "gemini-x", "force": False, "ignore_live": False,
    })
    for bad in ([cid, "--bogus", "x"], [cid, "/tmp/sum.md", "extra"], [cid, "--topic"]):
      with self.subTest(args=bad), contextlib.redirect_stderr(io.StringIO()):
        with self.assertRaises(SystemExit) as ctx:
          chat_lifecycle.parse_handoff_args(bad)
        self.assertEqual(ctx.exception.code, 2)

  def test_create_handoff_with_topic_retitles_child_and_keeps_summary(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.write_titles(tmp_dir, {"parent-1": "[10:00] ⦿ Topic A"}, project="outside-of-project")
      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]):
        # Written by the old chat via `summary-path <id>`, i.e. under its own topic
        summary_path = chat_lifecycle.get_handoff_summary_path("parent-1")
      with open(summary_path, "w", encoding="utf-8") as f:
        f.write("# real summary\n")

      start_mock = unittest.mock.MagicMock(return_value="child-2")
      with contextlib.ExitStack() as stack:
        for p in self.handoff_patches(tmp_dir, start_mock):
          stack.enter_context(p)
        res = chat_lifecycle.create_handoff(current_conv_id="parent-1", new_topic="Topic B")
        self.assertEqual(res["summary_file"], summary_path)
        with open(summary_path, "r", encoding="utf-8") as f:
          self.assertEqual(f.read(), "# real summary\n")
        self.assertEqual(start_mock.call_args.args[1], "[15:45] ⦿ Topic B")
        self.assertTrue(chat_lifecycle.is_archived("parent-1"))
        self.assertEqual(chat_lifecycle.get_conversation_title("parent-1"), "[10:00] ⦿ Topic A")
        self.assertFalse(chat_lifecycle.is_archived("child-2"))
        self.assertEqual(chat_lifecycle.get_conversation_title("child-2"), "[15:45] ⦿ Topic B")

  def test_archive_conversations_with_chain(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      ann_dir = os.path.join(tmp_dir, "annotations")
      os.makedirs(ann_dir, exist_ok=True)
      with open(os.path.join(ann_dir, "c1.pbtxt"), "w", encoding="utf-8") as f:
        f.write('title:"[10:00] « тест"\n')

      for cid, parent in [("c1", None), ("c2", "c1")]:
        log_dir = os.path.join(tmp_dir, "brain", cid, ".system_generated", "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "transcript.jsonl"), "w", encoding="utf-8") as f:
          if parent:
            f.write(f'{{"type": "USER_INPUT", "step_index": 0, "content": "Continuing work (conversation://{parent})"}}\n')
          else:
            f.write('{"type": "USER_INPUT", "step_index": 0, "content": "Start"}\n')

      rpc_calls = []

      def fake_ls_rpc(method, body, timeout=20.0):
        rpc_calls.append((method, body))
        return {}

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "ls_rpc", side_effect=fake_ls_rpc):
        res = chat_lifecycle.archive_conversations(["c1"], include_chain=True)
        self.assertEqual(res["archived_ids"], ["c1", "c2"])
        self.assertEqual(res["count"], 2)
        self.assertTrue(res["rpc_ok"])
        self.assertTrue(res["disk_ok"])
        self.assertEqual(len(rpc_calls), 1)
        self.assertEqual(rpc_calls[0][0], "UpdateConversationAnnotations")
        self.assertEqual(rpc_calls[0][1]["cascadeIds"], ["c1", "c2"])
        self.assertEqual(rpc_calls[0][1]["annotations"], {"archived": True})

        with open(os.path.join(ann_dir, "c1.pbtxt"), "r", encoding="utf-8") as f:
          self.assertIn("archived:true", f.read())
        with open(os.path.join(ann_dir, "c2.pbtxt"), "r", encoding="utf-8") as f:
          self.assertIn("archived:true", f.read())

  def test_audit_conversations_detects_and_fixes_issues(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.write_chain(tmp_dir, [("c1", None), ("c2", "c1"), ("s1", None), ("s2", None)])
      disk = {
          "c1": "[13:00] ‹✓› Moving Chats",
          "c2": "[13:14] ▸ Moving Chats",
          "s1": "[12:00] » Old Done",
          "s2": "[12:30] ✓ Lonely",
      }
      self.write_titles(tmp_dir, disk)
      ls_titles = dict(disk, c2="[13:08] ▸ ⦿ Moving Chats")
      fake_ls_summaries = {
          cid: {
              "summary": t,
              "annotations": {"title": t},
              "trajectoryMetadata": {
                  "projectId": "proj-1", "createdAt": f"2026-10-01T1{i}:00:00Z"
              },
          }
          for i, (cid, t) in enumerate(ls_titles.items())
      }

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(
               chat_lifecycle, "_fetch_ls_summaries_for_audit", return_value=fake_ls_summaries
           ), \
           patch.object(chat_lifecycle, "ls_rpc", side_effect=RuntimeError("offline in test")), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=True):
        report = chat_lifecycle.audit_conversations(fix=False)
        self.assertEqual(report["total_visible_chats"], 4)
        self.assertEqual(report["total_chains"], 3)
        self.assertEqual(report["issues_count"], 4)
        self.assertEqual(report["fixed_count"], 0)

        by_cid = {d["conversation_id"]: d for d in report["discrepancies"]}
        for cid in ("c1", "s1"):
          self.assertEqual(by_cid[cid]["issues"], ["should_be_archived"])
          self.assertEqual(by_cid[cid]["title"], disk[cid])
          self.assertFalse(by_cid[cid]["fixed"])
        self.assertIn("marker_mismatch", by_cid["c2"]["issues"])
        self.assertEqual(by_cid["c2"]["expected_title"], "[13:14] ⦿ Moving Chats")
        self.assertIn("marker_mismatch", by_cid["s2"]["issues"])
        self.assertEqual(by_cid["s2"]["expected_title"], "[12:30] ⦿ Lonely")
        for cid in disk:
          self.assertFalse(chat_lifecycle.is_archived(cid))

        busy = {"c1": {"backgroundTaskCount": 1}, "s1": {"hasRunningSubagents": True}}
        for live in (None, busy):
          with self.subTest(live=live):
            self.live_mock.return_value = live
            blocked = chat_lifecycle.audit_conversations(fix=True)
            by_cid = {d["conversation_id"]: d for d in blocked["discrepancies"]}
            for cid in ("c1", "s1"):
              self.assertFalse(by_cid[cid]["fixed"])
              self.assertFalse(chat_lifecycle.is_archived(cid))

        self.live_mock.return_value = {}
        fix_report = chat_lifecycle.audit_conversations(fix=True)
        self.assertEqual(fix_report["fixed_count"], 4)
        self.assertTrue(chat_lifecycle.is_archived("c1"))
        self.assertTrue(chat_lifecycle.is_archived("s1"))
        self.assertEqual(chat_lifecycle.get_conversation_title("c1"), disk["c1"])
        self.assertFalse(chat_lifecycle.is_archived("c2"))
        self.assertFalse(chat_lifecycle.is_archived("s2"))
        self.assertEqual(chat_lifecycle.get_conversation_title("c2"), "[13:14] ⦿ Moving Chats")
        self.assertEqual(chat_lifecycle.get_conversation_title("s2"), "[12:30] ⦿ Lonely")

  # --- CLI usage -------------------------------------------------------------

  SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks", "chat_lifecycle.py"))
  PUBLIC_COMMANDS = (
      "status",
      "set-title",
      "auto-title",
      "summary-path",
      "handoff",
      "audit",
      "archive",
      "finalize",
      "reopen",
  )

  def _run_cli(self, *args):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
      try:
        rc = chat_lifecycle.main(list(args))
      except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    res = unittest.mock.MagicMock()
    res.returncode = rc
    res.stdout = out.getvalue()
    res.stderr = err.getvalue()
    return res

  def test_cli_help_lists_public_commands(self):
    for argv in (["--help"], ["-h"], ["help"], []):
      with self.subTest(argv=argv):
        res = self._run_cli(*argv)
        self.assertEqual(res.returncode, 0, res.stderr)
        for cmd in self.PUBLIC_COMMANDS:
          self.assertIn(cmd, res.stdout)
        # The user-only commands must be flagged as such.
        self.assertRegex(res.stdout, r"(?i)finalize.*user-only")

  def test_cli_unknown_command_fails_with_usage(self):
    res = self._run_cli("frobnicate", "c1")
    self.assertEqual(res.returncode, 2)
    self.assertIn("frobnicate", res.stderr)
    self.assertIn("status", res.stderr)

  ALL_COMMANDS = PUBLIC_COMMANDS + ("model", "set")

  def test_cli_subcommand_help_prints_usage(self):
    for cmd in self.ALL_COMMANDS:
      for flag in ("--help", "-h"):
        with self.subTest(cmd=cmd, flag=flag):
          res = self._run_cli(cmd, flag)
          self.assertEqual(res.returncode, 0, res.stderr)
          self.assertIn("Usage:", res.stdout)
          self.assertNotIn("Traceback", res.stderr)

  def test_cli_rejects_invalid_conversation_id_before_roadmap(self):
    id_commands = tuple(c for c in self.ALL_COMMANDS if c != "audit")
    for cmd in id_commands:
      for bad in ("not-a-uuid", "--bogus"):
        with self.subTest(cmd=cmd, bad=bad):
          res = self._run_cli(cmd, bad)
          self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
          self.assertIn(bad, res.stderr)
          self.assertNotIn("Traceback", res.stderr)
          self.assertNotIn("RoadmapError", res.stderr)

  def test_cli_finalize_archives_chain_and_reopen_unarchives(self):
    root = "11111111-1111-1111-1111-111111111111"
    tail = "22222222-2222-2222-2222-222222222222"
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.write_chain(tmp_dir, [(root, None), (tail, root)])
      self.write_titles(tmp_dir, {root: "[10:00] ⦿ Topic", tail: "[10:30] ⦿ Topic"})
      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "ls_rpc", side_effect=RuntimeError("offline in test")), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False):
        res = self._run_cli("finalize", tail)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(chat_lifecycle.is_archived(root))
        self.assertTrue(chat_lifecycle.is_archived(tail))

        res = self._run_cli("reopen", tail)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(chat_lifecycle.is_archived(tail))
        self.assertTrue(chat_lifecycle.is_archived(root))
        self.assertEqual(chat_lifecycle.get_conversation_title(tail), "[10:30] ⦿ Topic")

  def test_cli_requires_conversation_id_without_env(self):
    env = {k: v for k, v in os.environ.items() if k not in ("CONVERSATION_ID", "ANTIGRAVITY_CONVERSATION_ID")}
    with patch.dict(os.environ, env, clear=True):
      for cmd in ("status", "handoff", "summary-path", "finalize", "reopen", "archive"):
        with self.subTest(cmd=cmd):
          res = self._run_cli(cmd)
          self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
          self.assertNotIn("Traceback", res.stderr)

  def test_auto_archive_projects_skips_outside_or_unconfigured_projects(self):
    with (
        patch.object(chat_lifecycle, "load_retention_rules", return_value={"skia-g3-roll-check": 1}),
        patch.object(chat_lifecycle, "resolve_project_id", return_value="proj-roll-1"),
        patch.object(chat_lifecycle, "archive_project_conversations") as mock_arch,
    ):
      with patch.object(chat_lifecycle, "get_conversation_project_id", return_value=""):
        self.assertEqual(chat_lifecycle.auto_archive_projects(target_cid="cid-outside"), {})
        mock_arch.assert_not_called()

      with patch.object(chat_lifecycle, "get_conversation_project_id", return_value="proj-other"):
        self.assertEqual(chat_lifecycle.auto_archive_projects(target_cid="cid-other"), {})
        mock_arch.assert_not_called()

      mock_arch.return_value = {"archived_ids": ["old-1"], "count": 1}
      with patch.object(chat_lifecycle, "get_conversation_project_id", return_value="proj-roll-1"):
        res = chat_lifecycle.auto_archive_projects(target_cid="cid-roll")
        self.assertEqual(res, {"skia-g3-roll-check": {"archived_ids": ["old-1"], "count": 1}})
        mock_arch.assert_called_once_with("proj-roll-1", keep=1)

  def _outside_project_handoff_env(self, tmp_dir: str):
    db_path = os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME)
    conn = sqlite3.connect(db_path)
    with conn:
      conn.execute(
          "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, project_id TEXT)"
      )
      conn.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)", ("parent-1", "[10:00] ⦿ My Feature", "outside-of-project"))
    conn.close()
    launched = []

    def fake_run(cmd, **kwargs):
      if "new-conversation" in cmd:
        launched.append(cmd)
        return unittest.mock.MagicMock(returncode=0, stdout='{"conversationId": "child-%d"}' % len(launched))
      if "get-conversation-metadata" in cmd:
        return unittest.mock.MagicMock(
            returncode=0,
            stdout='{"response": {"conversationMetadata": {"metadata": {"sourceMetadata": null}}}}',
        )
      return unittest.mock.MagicMock(returncode=0, stdout="")

    ctx = contextlib.ExitStack()
    ctx.enter_context(patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]))
    ctx.enter_context(patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False))
    ctx.enter_context(patch.object(chat_lifecycle, "ls_rpc", side_effect=RuntimeError("offline in test")))
    ctx.enter_context(patch.object(chat_lifecycle, "get_current_time_str", return_value="15:45"))
    ctx.enter_context(patch("subprocess.run", side_effect=fake_run))
    return ctx, launched

  def test_handoff_records_continuation_and_refuses_a_second_one(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      ctx, launched = self._outside_project_handoff_env(tmp_dir)
      with ctx:
        res = chat_lifecycle.create_handoff("parent-1", next_step_prompt="go")
        self.assertEqual(res["new_conversation_id"], "child-1")
        state = chat_lifecycle.load_guard_state("parent-1")
        self.assertEqual(state["handed_off_to"], "child-1")
        self.assertTrue(state["handoff_completed"])
        self.assertFalse(state.get("pending_handoff_launch"))

        with self.assertRaises(RuntimeError) as cm:
          chat_lifecycle.create_handoff("parent-1", next_step_prompt="go")
        self.assertIn("child-1", str(cm.exception))
        self.assertEqual(len(launched), 1)
        self.assertEqual(chat_lifecycle.load_guard_state("parent-1")["handed_off_to"], "child-1")

        res2 = chat_lifecycle.create_handoff("parent-1", next_step_prompt="go", force=True)
        self.assertEqual(res2["new_conversation_id"], "child-2")
        self.assertEqual(len(launched), 2)
        self.assertEqual(chat_lifecycle.load_guard_state("parent-1")["handed_off_to"], "child-2")

  def test_cli_handoff_force_flag_is_accepted(self):
    opts = chat_lifecycle.parse_handoff_args(["c1", "--force", "--next", "go"])
    self.assertTrue(opts["force"])
    self.assertEqual(opts["next_step"], "go")
    self.assertFalse(chat_lifecycle.parse_handoff_args(["c1"])["force"])

  def test_print_handoff_result_prints_only_json(self):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
      chat_lifecycle.print_handoff_result({
          "new_conversation_id": "child-1",
          "roadmap": "\n".join(f"R{i} item" for i in range(80)),
      })
    self.assertEqual(json.loads(buf.getvalue()), {"new_conversation_id": "child-1"})

  def test_get_model_trailer(self):
    catalog = {"models": {
        "gemini-3.8-flash-high": {"model": "MODEL_PLACEHOLDER_M50",
                                  "displayName": "Gemini 3.8 Flash (High)"},
        "snowball-subagent": {"model": "MODEL_PLACEHOLDER_M136"},
        "helper": {"model": "MODEL_PLACEHOLDER_M7", "displayName": "research-subagent"},
    }}

    def trailer(enum):
      with patch.object(chat_lifecycle, "get_conversation_model_enum", return_value=enum), \
           patch.object(chat_lifecycle, "get_available_models", return_value=catalog), \
           patch.object(chat_lifecycle, "resolve_handoff_model",
                        side_effect=AssertionError("project default is not the model used")):
        return chat_lifecycle.get_model_trailer("cid-1")

    self.assertEqual(trailer("MODEL_PLACEHOLDER_M50"), "Assisted-by: Gemini 3.8 Flash (High)")
    for internal in ("MODEL_PLACEHOLDER_M136", "MODEL_PLACEHOLDER_M7", "MODEL_UNKNOWN", ""):
      self.assertEqual(trailer(internal), "Assisted-by: AI")

    with patch.object(chat_lifecycle, "get_conversation_model_enum",
                      side_effect=RuntimeError("no LS")):
      self.assertEqual(chat_lifecycle.get_model_trailer("cid-1"), "Assisted-by: AI")

  def test_pbtxt_escapes_backslashes_quotes_and_heals_broken_file(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      ann_dir = os.path.join(tmp_dir, chat_lifecycle.ANNOTATIONS_DIR_NAME)
      os.makedirs(ann_dir, exist_ok=True)
      ann_file = os.path.join(ann_dir, "cid-nl.pbtxt")
      with open(ann_file, "w", encoding="utf-8") as f:
        f.write('title:"old"  last_user_view_time:{seconds:123}\n')

      title = r'[17:34] ⦿ Эксперимент: метрики пустых строк (\n) "test"'
      rpc_calls = []

      def fake_rpc(cid, t):
        rpc_calls.append((cid, t))
        return len(rpc_calls) > 1

      with (
          patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]),
          patch.object(chat_lifecycle, "update_conversation_title_rpc", side_effect=fake_rpc),
      ):
        ok = chat_lifecycle.update_conversation_title("cid-nl", title)
        self.assertTrue(ok)
        self.assertEqual(len(rpc_calls), 2)
        with open(ann_file, "r", encoding="utf-8") as f:
          raw_pbtxt = f.read()
        self.assertEqual(len(raw_pbtxt.splitlines()), 1)
        self.assertIn(r'(\\n)', raw_pbtxt)
        self.assertIn(r'\"test\"', raw_pbtxt)
        self.assertIn("last_user_view_time:{seconds:123}", raw_pbtxt)
        self.assertEqual(chat_lifecycle.get_conversation_title("cid-nl"), title)

  def test_project_model_loading_and_saving(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      cfg_file = os.path.join(tmp_dir, "projects_models.json")
      with (
          patch.object(chat_lifecycle, "DEFAULT_PROJECT_MODELS_FILE", cfg_file),
          patch.dict(chat_lifecycle.DEFAULT_PROJECT_MODELS, {"proj-default": "flash"}),
      ):
        rules = chat_lifecycle.load_project_models()
        self.assertEqual(rules.get("proj-default"), "flash")

        chat_lifecycle.save_project_models({"proj-custom": "pro", "proj-default": "flash_lite"})
        rules2 = chat_lifecycle.load_project_models()
        self.assertEqual(rules2.get("proj-custom"), "pro")
        self.assertEqual(rules2.get("proj-default"), "flash_lite")

  def test_get_project_model_preference(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      pdir = os.path.join(tmp_dir, "projects")
      os.makedirs(pdir, exist_ok=True)
      pfile = os.path.join(pdir, "p1.json")
      with open(pfile, "w", encoding="utf-8") as f:
        json.dump({
            "id": "p1-uuid",
            "name": "Roll Check · ⟳ 1",
            "settings": {"model": "flash"},
        }, f)

      pfile2 = os.path.join(pdir, "p2.json")
      with open(pfile2, "w", encoding="utf-8") as f:
        json.dump({"id": "p2-uuid", "name": "Heavy Engine"}, f)

      with (
          patch.dict(chat_lifecycle.DEFAULT_PROJECT_MODELS, {"Heavy Engine": "pro"}),
          patch.object(chat_lifecycle, "DEFAULT_PROJECT_MODELS_FILE", "/nonexistent"),
      ):
        self.assertEqual(
            chat_lifecycle.get_project_model_preference("p1-uuid", projects_dir=pdir),
            "flash",
        )
        self.assertEqual(
            chat_lifecycle.get_project_model_preference("Roll Check", projects_dir=pdir),
            "flash",
        )
        self.assertEqual(
            chat_lifecycle.get_project_model_preference("p2-uuid", projects_dir=pdir),
            "pro",
        )
        self.assertEqual(
            chat_lifecycle.get_project_model_preference("outside-of-project", projects_dir=pdir),
            "",
        )

  def test_resolve_handoff_model_project_override(self):
    fake_catalog = {
        "models": {
            "m-flash": {"model": "M_FLASH", "displayName": "Flash"},
            "m-pro": {"model": "M_PRO", "displayName": "Pro"},
        },
        "tieredModelIds": {
            "flash": ["m-flash"],
            "pro": ["m-pro"],
        },
    }
    with (
        patch.object(chat_lifecycle, "get_available_models", return_value=fake_catalog),
        patch.object(chat_lifecycle, "get_last_used_model", return_value="M_PRO"),
        patch.object(chat_lifecycle, "get_project_model_preference", return_value="flash"),
    ):
      res = chat_lifecycle.resolve_handoff_model("cid-1", project_id="p-flash")
      self.assertEqual(res["enum"], "M_FLASH")
      self.assertEqual(res["source"], "project:p-flash")

      res_exp = chat_lifecycle.resolve_handoff_model(
          "cid-1", explicit="pro", project_id="p-flash"
      )
      self.assertEqual(res_exp["enum"], "M_PRO")
      self.assertEqual(res_exp["source"], "tier:pro")

    with (
        patch.object(chat_lifecycle, "get_available_models", return_value=fake_catalog),
        patch.object(chat_lifecycle, "get_last_used_model", return_value="M_PRO"),
        patch.object(chat_lifecycle, "get_project_model_preference", return_value=""),
    ):
      res_inh = chat_lifecycle.resolve_handoff_model("cid-1", project_id="p-none")
      self.assertEqual(res_inh["enum"], "M_PRO")
      self.assertEqual(res_inh["source"], "inherited")

  def test_export_chain_index_writes_jetski_chains_json(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.write_chain(tmp_dir, [("c1", None), ("c2", "c1"), ("c3", "c2")])
      self.write_titles(tmp_dir, {
          "c1": "[10:00] ▸ First",
          "c2": "[10:30] ✓ Second",
          "c3": "[11:00] ⦿ Third",
      })
      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False):
        chat_lifecycle.archive_chain("c3")
        idx_path = os.path.join(tmp_dir, "brain", chat_lifecycle.CHAIN_INDEX_FILE_NAME)
        self.assertTrue(os.path.isfile(idx_path))
        with open(idx_path, "r", encoding="utf-8") as f:
          data = json.load(f)
        self.assertEqual(data["parentOf"], {"c2": "c1", "c3": "c2"})

  def test_export_chain_index_merges_guard_redirects(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.write_chain(tmp_dir, [("c1", None)])

      def write_guard(cid, state):
        scratch = os.path.join(tmp_dir, "brain", cid, "scratch")
        os.makedirs(scratch, exist_ok=True)
        with open(os.path.join(scratch, ".context_guard_state.json"), "w",
                  encoding="utf-8") as f:
          json.dump(state, f)

      # c2 has no transcript yet: the redirect alone links it into c1's chain
      write_guard("c1", {"handed_off_to": "c2", "handed_off_at": "2026-10-28T00:00:00+00:00"})
      write_guard("t1", {"handed_off_to": "t2", "handed_off_at": "2026-10-28T00:01:00+00:00",
                         "handoff_kind": "next-task"})
      write_guard("idle", {"gm_count": 3})
      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]):
        data = chat_lifecycle.export_chain_index()
      self.assertEqual(data["parentOf"].get("c2"), "c1")
      self.assertNotIn("t2", data["parentOf"])
      self.assertEqual(data["createdAt"]["c2"], "2026-10-28T00:00:00+00:00")
      self.assertEqual(data["createdAt"]["t2"], "2026-10-28T00:01:00+00:00")
      self.assertEqual(data["redirects"], {
          "c1": {"to": "c2", "at": "2026-10-28T00:00:00+00:00", "kind": "handoff"},
          "t1": {"to": "t2", "at": "2026-10-28T00:01:00+00:00", "kind": "next-task"},
      })

  def test_create_next_task_finalizes_old_chat_claims_roadmap_item_and_starts_root_chat(self):
    import pre_tool_guard
    import roadmap

    with tempfile.TemporaryDirectory() as tmp_dir:
      self.make_project_db(tmp_dir)
      roadmap.add_item("proj-abc", "parent-1", "First task")
      roadmap.set_status("proj-abc", "parent-1", "R1", "done")
      roadmap.add_item("proj-abc", "parent-1", "Implement feature X")
      old_state = self.write_old_state()
      start = unittest.mock.MagicMock(
          side_effect=lambda enum, title, prompt, pid, before_send=None: (
              before_send("child-2") if before_send else None,
              "child-2",
          )[1]
      )
      p = self.handoff_patches(tmp_dir, start)
      with p[0], p[1], p[2], p[3], p[4], p[5]:
        res = chat_lifecycle.create_next_task(
            current_conv_id="parent-1",
            item_id="R2",
            new_topic="R2 Feature X",
            notes="R1 merged cleanly",
        )
        self.assertEqual(res["new_conversation_id"], "child-2")
        self.assertEqual(res["item_id"], "R2")
        self.assertEqual(res["finalized_chain"], ["parent-1"])
        self.assertNotIn("roadmap", res)
        guard = chat_lifecycle.load_guard_state("parent-1")
        self.assertEqual((guard["handed_off_to"], guard["handoff_kind"]), ("child-2", "next-task"))
        self.assertTrue(chat_lifecycle.is_archived("parent-1"))
        self.assertEqual(chat_lifecycle.get_conversation_title("parent-1"), "[10:00] ⦿ My Feature")
        self.assertFalse(chat_lifecycle.is_archived("child-2"))
        self.assertEqual(chat_lifecycle.get_conversation_title("child-2"), "[15:45] ⦿ R2 Feature X")

      # New chat prompt must NOT match HANDOFF_PARENT_RE so it stays an independent root chat
      prompt = start.call_args[0][2]
      self.assertIsNone(chat_lifecycle.HANDOFF_PARENT_RE.search(prompt))
      self.assertIn("split out of conversation://parent-1", prompt)

      # Old state file removed; new state file seeded for R2
      self.assertFalse(os.path.exists(old_state))
      new_state = roadmap.state_path("proj-abc", "child-2")
      self.assertEqual(res["summary_file"], new_state)
      with open(new_state, encoding="utf-8") as f:
        st_txt = f.read()
      self.assertIn("# R2: Implement feature X", st_txt)
      self.assertIn("R1 merged cleanly", st_txt)

      # Roadmap R2 is claimed as doing by child-2 and linked to child-2
      doc = roadmap.load("proj-abc")
      r2 = [it for it in doc["items"] if it["id"] == "R2"][0]
      self.assertEqual(r2["status"], "doing")
      self.assertEqual(r2["owner"], "child-2")
      self.assertEqual(r2["link"], "child-2")

      # Worktree lease check treats parent-1 as inactive/closed immediately
      child_log = os.path.join(
          tmp_dir, "brain", "child-2", ".system_generated", "logs", "transcript.jsonl"
      )
      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]):
        still_active, _ = pre_tool_guard._is_conv_still_active_tip(
            "parent-1", "child-2", child_log
        )
        self.assertFalse(still_active)

  def test_create_next_task_refuses_when_current_chat_still_owns_doing_item(self):
    import roadmap

    with tempfile.TemporaryDirectory() as tmp_dir:
      self.make_project_db(tmp_dir)
      roadmap.add_item("proj-abc", "parent-1", "First task")
      roadmap.set_status("proj-abc", "parent-1", "R1", "doing")
      roadmap.add_item("proj-abc", "parent-1", "Second task")
      start = unittest.mock.MagicMock(return_value="child-2")
      p = self.handoff_patches(tmp_dir, start)
      with p[0], p[1], p[2], p[3], p[4], p[5]:
        with self.assertRaisesRegex(RuntimeError, "still owns 'doing' item.*R1"):
          chat_lifecycle.create_next_task(current_conv_id="parent-1", item_id="R2")
      start.assert_not_called()

  def test_create_next_task_auto_picks_first_todo_and_finalizes_multi_step_chain(self):
    import roadmap

    with tempfile.TemporaryDirectory() as tmp_dir:
      self.write_chain(tmp_dir, [("root-0", None), ("parent-1", "root-0")])
      self.write_titles(tmp_dir, {
          "root-0": "[09:30] ▸ First Task",
          "parent-1": "[10:00] ⦿ First Task",
      }, project="proj-abc")
      roadmap.add_item("proj-abc", "parent-1", "First task")
      roadmap.set_status("proj-abc", "parent-1", "R1", "done")
      roadmap.add_item("proj-abc", "parent-1", "Fix crash in b/345678901")
      start = unittest.mock.MagicMock(
          side_effect=lambda enum, title, prompt, pid, before_send=None: (
              before_send("child-2") if before_send else None,
              "child-2",
          )[1]
      )
      p = self.handoff_patches(tmp_dir, start)
      with p[0], p[1], p[2], p[3], p[4], p[5]:
        res = chat_lifecycle.create_next_task(current_conv_id="parent-1")
        self.assertEqual(res["item_id"], "R2")
        self.assertEqual(res["finalized_chain"], ["root-0", "parent-1"])
        self.assertTrue(chat_lifecycle.is_archived("root-0"))
        self.assertTrue(chat_lifecycle.is_archived("parent-1"))
        self.assertEqual(chat_lifecycle.get_conversation_title("root-0"), "[09:30] ▸ First Task")
        self.assertEqual(chat_lifecycle.get_conversation_title("parent-1"), "[10:00] ⦿ First Task")
        self.assertFalse(chat_lifecycle.is_archived("child-2"))
        self.assertEqual(
            chat_lifecycle.get_conversation_title("child-2"), "[15:45] ⦿ R2 b/345678901"
        )

  def test_parse_next_task_args(self):
    cid = "12345678-1234-1234-1234-123456789abc"
    opts = chat_lifecycle.parse_next_task_args(
        [cid, "R5", "--topic", "R5 Widget", "--notes", "done R4"]
    )
    self.assertEqual(opts["cid"], cid)
    self.assertEqual(opts["item_id"], "R5")
    self.assertEqual(opts["new_topic"], "R5 Widget")
    self.assertEqual(opts["notes"], "done R4")

    opts_no_cid = chat_lifecycle.parse_next_task_args(["R12.2", "--topic=Subtask"])
    self.assertEqual(opts_no_cid["cid"], "")
    self.assertEqual(opts_no_cid["item_id"], "R12.2")
    self.assertEqual(opts_no_cid["new_topic"], "Subtask")

  def test_next_task_cli_recognized_by_guards(self):
    import context_guard
    import pre_tool_guard

    cmd_run = "python3 ~/.gemini/config/hooks/chat_lifecycle.py next-task R2 --topic 'R2 Work'"
    cmd_help = "python3 ~/.gemini/config/hooks/chat_lifecycle.py next-task --help"
    self.assertTrue(pre_tool_guard.is_chat_lifecycle_next_task_launch(cmd_run))
    self.assertTrue(context_guard.is_chat_lifecycle_handoff_command(cmd_run))
    self.assertFalse(pre_tool_guard.is_chat_lifecycle_next_task_launch(cmd_help))
    self.assertFalse(context_guard.is_chat_lifecycle_handoff_command(cmd_help))

  def test_set_conversation_wait_state_and_audit_preserves_waiting_marker(self):
    cid = "12345678-1234-1234-1234-123456789abc"
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.write_chain(tmp_dir, [(cid, None)])
      self.write_titles(tmp_dir, {cid: "[10:15] ⦿ Flight Deck"}, project="proj-1")
      with unittest.mock.patch.object(
          chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]
      ), unittest.mock.patch.object(
          chat_lifecycle, "discover_local_ls_endpoint", return_value=("", "")
      ), unittest.mock.patch.object(
          chat_lifecycle, "ls_rpc", side_effect=RuntimeError("offline in test")
      ), unittest.mock.patch.object(
          chat_lifecycle, "_fetch_ls_summaries_for_audit", return_value=None
      ):
        t_wait = chat_lifecycle.set_conversation_wait_state(cid, waiting=True)
        self.assertEqual(t_wait, "[10:15] ⏳ Flight Deck")
        self.assertTrue(chat_lifecycle.is_waiting_title(t_wait))

        report = chat_lifecycle.audit_conversations(fix=True)
        self.assertEqual(report["total_visible_chats"], 1)
        self.assertEqual(report["issues_count"], 0)
        self.assertEqual(
            chat_lifecycle.get_conversation_title(cid), "[10:15] ⏳ Flight Deck"
        )

        t_toggle = chat_lifecycle.set_conversation_wait_state(cid, waiting=None)
        self.assertEqual(t_toggle, "[10:15] ⦿ Flight Deck")
        self.assertFalse(chat_lifecycle.is_waiting_title(t_toggle))

        # A trailing [⏳St · 1/4 rolls] badge must not be mistaken for a leading ⏳ wait marker
        t_badge = "[10:15] ⦿ cl/1396956 [⏳St · 1/4 rolls]"
        self.assertFalse(chat_lifecycle.is_waiting_title(t_badge))
        chat_lifecycle.set_conversation_wait_state(cid, waiting=True)
        t_updated = chat_lifecycle.set_conversation_topic(
            cid, "cl/1396956 [⏳St · 1/4 rolls]"
        )
        self.assertIn(" ⏳ cl/1396956 [⏳St · 1/4 rolls]", t_updated)

        chat_lifecycle.archive_conversations([cid])
        self.assertEqual(
            chat_lifecycle.set_conversation_wait_state(cid, waiting=False), t_updated
        )
        self.assertEqual(chat_lifecycle.set_conversation_wait_state(cid), t_updated)
        self.assertEqual(chat_lifecycle.get_conversation_title(cid), t_updated)

  def test_create_next_task_sweeps_completed_sibling_chats_in_project(self):
    import roadmap

    with tempfile.TemporaryDirectory() as tmp_dir:
      self.write_chain(tmp_dir, [("c1", None), ("c2", None), ("c3", None), ("c_busy", None)])
      self.write_titles(tmp_dir, {
          "c1": "[10:00] ⦿ R1 First",
          "c2": "[10:05] ⦿ R2 Second",
          "c3": "[10:10] ⦿ R3 Third",
          "c_busy": "[10:15] ⦿ R5 Still working",
      }, project="proj-abc")
      for idx, cid in enumerate(("c1", "c2", "c3"), start=1):
        roadmap.add_item("proj-abc", cid, f"Task {idx}")
        roadmap.set_status("proj-abc", cid, f"R{idx}", "doing")
        st_path = roadmap.state_path("proj-abc", cid)
        os.makedirs(os.path.dirname(st_path), exist_ok=True)
        with open(st_path, "w", encoding="utf-8") as f:
          f.write(f"# R{idx}\n")
        roadmap.set_status("proj-abc", cid, f"R{idx}", "done")

      roadmap.add_item("proj-abc", "c3", "Task 4 Next")
      roadmap.add_item("proj-abc", "c_busy", "Task 5 Busy")
      roadmap.set_status("proj-abc", "c_busy", "R5", "doing")

      start = unittest.mock.MagicMock(
          side_effect=lambda enum, title, prompt, pid, before_send=None: (
              before_send("c4") if before_send else None,
              "c4",
          )[1]
      )
      p = self.handoff_patches(tmp_dir, start)
      with p[0], p[1], p[2], p[3], p[4], p[5]:
        # If c1 is finalized while c_busy is still doing R5 (or c3 holds the baton),
        # sweep_completed_project_chats does NOT sweep a live baton holder
        roadmap.set_status("proj-abc", "c_busy", "R5", "done")
        # Now c_busy holds the baton for R4
        self.assertEqual(roadmap.load("proj-abc").get("baton", {}).get("conv"), "c_busy")
        swept = chat_lifecycle.sweep_completed_project_chats("proj-abc", finalized_cids={"c1"})
        self.assertEqual(sorted(swept), [["c2"], ["c3"]])
        # c_busy (holding baton for R4) stays visible and keeps its baton
        self.assertFalse(chat_lifecycle.is_archived("c_busy"))
        self.assertFalse(chat_lifecycle.is_archived("c1"))
        self.assertEqual(roadmap.load("proj-abc").get("baton", {}).get("conv"), "c_busy")

        # Keep R5 as doing for the next-task sweep check
        roadmap.set_status("proj-abc", "c_busy", "R5", "doing")
        chat_lifecycle.archive_conversations(["c2", "c3"], archived=False)
        res = chat_lifecycle.create_next_task(current_conv_id="c3", item_id="R4")
        self.assertEqual(res["new_conversation_id"], "c4")
        self.assertEqual(res["finalized_chain"], ["c3"])
        # c3 AND finished sibling chats c1, c2 are all archived with titles kept
        for cid, title in (
            ("c1", "[10:00] ⦿ R1 First"),
            ("c2", "[10:05] ⦿ R2 Second"),
            ("c3", "[10:10] ⦿ R3 Third"),
        ):
          self.assertTrue(chat_lifecycle.is_archived(cid))
          self.assertEqual(chat_lifecycle.get_conversation_title(cid), title)
        # Busy sibling chat c_busy (owns doing R5) and new chat c4 stay visible
        self.assertFalse(chat_lifecycle.is_archived("c_busy"))
        self.assertFalse(chat_lifecycle.is_archived("c4"))
        self.assertEqual(
            chat_lifecycle.get_conversation_title("c_busy"), "[10:15] ⦿ R5 Still working"
        )
        self.assertEqual(chat_lifecycle.get_conversation_title("c4"), "[15:45] ⦿ R4 Task 4 Next")
        self.assertEqual(sorted(res["swept_sibling_chains"]), [["c1"], ["c2"]])
        # State files for c1, c2, c3 were cleaned up
        for cid in ("c1", "c2", "c3"):
          self.assertFalse(os.path.exists(roadmap.state_path("proj-abc", cid)))

  def test_list_live_dependents_counts_only_subagents_and_background_tasks(self):
    summaries = {
        "busy": {"status": "CASCADE_RUN_STATUS_RUNNING", "notFullyIdle": True,
                 "backgroundTaskCount": 2, "hasRunningSubagents": True},
        "own-turn": {"status": "CASCADE_RUN_STATUS_RUNNING", "notFullyIdle": True},
        "idle": {"status": "CASCADE_RUN_STATUS_IDLE"},
    }
    self.assertEqual(
        chat_lifecycle.list_live_dependents(["busy", "own-turn", "idle", "absent"], summaries),
        {"busy": ["2 background task(s)", "running subagent(s)"]},
    )

  def test_list_live_dependents_counts_running_turn_of_another_chat(self):
    summaries = {"sib": {"status": "CASCADE_RUN_STATUS_RUNNING"}}
    self.assertEqual(chat_lifecycle.list_live_dependents(["sib"], summaries), {})
    self.assertEqual(
        chat_lifecycle.list_live_dependents(["sib"], summaries, include_running=True),
        {"sib": ["running turn"]},
    )

  def test_handoff_refuses_while_chat_has_live_dependents(self):
    self.live_mock.return_value = {"parent-1": {"backgroundTaskCount": 1}}
    with tempfile.TemporaryDirectory() as tmp_dir:
      ctx, launched = self._outside_project_handoff_env(tmp_dir)
      with ctx:
        with self.assertRaisesRegex(RuntimeError, "1 background task"):
          chat_lifecycle.create_handoff("parent-1", next_step_prompt="go")
        self.assertEqual(launched, [])
        self.assertFalse(chat_lifecycle.load_guard_state("parent-1").get("handed_off_to"))
        res = chat_lifecycle.create_handoff("parent-1", next_step_prompt="go", ignore_live=True)
        self.assertEqual(res["new_conversation_id"], "child-1")

  def test_create_next_task_refuses_while_chat_has_live_dependents(self):
    import roadmap

    self.live_mock.return_value = {"parent-1": {"hasRunningSubagents": True}}
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.make_project_db(tmp_dir)
      roadmap.add_item("proj-abc", "parent-1", "Next task")
      start = unittest.mock.MagicMock(return_value="child-2")
      p = self.handoff_patches(tmp_dir, start)
      with p[0], p[1], p[2], p[3], p[4], p[5]:
        with self.assertRaisesRegex(RuntimeError, "running subagent"):
          chat_lifecycle.create_next_task(current_conv_id="parent-1", item_id="R1")
      start.assert_not_called()

  def test_sweep_skips_chain_whose_leaf_has_live_dependents(self):
    import roadmap

    with tempfile.TemporaryDirectory() as tmp_dir:
      self.write_chain(tmp_dir, [("c1", None), ("c2", None)])
      self.write_titles(tmp_dir, {
          "c1": "[10:00] ⦿ R1 First",
          "c2": "[10:05] ⦿ R2 Second",
      }, project="proj-abc")
      for idx, cid in enumerate(("c1", "c2"), start=1):
        roadmap.add_item("proj-abc", cid, f"Task {idx}")
        roadmap.set_status("proj-abc", cid, f"R{idx}", "doing")
        roadmap.set_status("proj-abc", cid, f"R{idx}", "done")
      self.live_mock.return_value = {"c2": {"backgroundTaskCount": 1}}
      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False):
        swept = chat_lifecycle.sweep_completed_project_chats("proj-abc")
        self.assertEqual(swept, [["c1"]])
        self.assertTrue(chat_lifecycle.is_archived("c1"))
        self.assertFalse(chat_lifecycle.is_archived("c2"))
        self.live_mock.return_value = None
        chat_lifecycle.archive_conversations(["c1"], archived=False)
        self.assertEqual(chat_lifecycle.sweep_completed_project_chats("proj-abc"), [])
        self.assertFalse(chat_lifecycle.is_archived("c1"))

  def test_sweep_skips_waiting_leaf(self):
    import roadmap

    with tempfile.TemporaryDirectory() as tmp_dir:
      self.write_chain(tmp_dir, [("c1", None), ("c2", None)])
      self.write_titles(tmp_dir, {
          "c1": "[10:00] ⦿ R1 First",
          "c2": "[10:05] ⏳ R2 Cron",
      }, project="proj-abc")
      for idx, cid in enumerate(("c1", "c2"), start=1):
        roadmap.add_item("proj-abc", cid, f"Task {idx}")
        roadmap.set_status("proj-abc", cid, f"R{idx}", "doing")
        roadmap.set_status("proj-abc", cid, f"R{idx}", "done")
      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "ls_rpc", side_effect=RuntimeError("offline in test")):
        self.assertEqual(chat_lifecycle.sweep_completed_project_chats("proj-abc"), [["c1"]])
        self.assertTrue(chat_lifecycle.is_archived("c1"))
        self.assertFalse(chat_lifecycle.is_archived("c2"))

  def test_live_state_unknown_blocks_handoff_and_sweep(self):
    self.live_mock.return_value = None
    with tempfile.TemporaryDirectory() as tmp_dir:
      ctx, launched = self._outside_project_handoff_env(tmp_dir)
      with ctx:
        with self.assertRaisesRegex(RuntimeError, "cannot tell"):
          chat_lifecycle.create_handoff("parent-1", next_step_prompt="go")
        self.assertEqual(launched, [])
    self.assertIsNone(chat_lifecycle.list_live_dependents(["c1"]))

  def test_fetch_live_summaries_rejects_fallback_without_jetbox_fields(self):
    self.live.stop()
    try:
      with patch("project_activity_badge.fetch_trajectories", return_value={"c1": {"status": "x"}}):
        self.assertIsNone(chat_lifecycle._fetch_live_summaries())
      jetbox = {"c1": {"status": "x", "hasActivity": False}}
      with patch("project_activity_badge.fetch_trajectories", return_value=jetbox):
        self.assertEqual(chat_lifecycle._fetch_live_summaries(), jetbox)
    finally:
      self.live_mock = self.live.start()

  def test_cli_ignore_live_flag_is_accepted(self):
    self.assertTrue(chat_lifecycle.parse_handoff_args(["c1", "--ignore-live"])["ignore_live"])
    self.assertFalse(chat_lifecycle.parse_handoff_args(["c1"])["ignore_live"])
    self.assertTrue(
        chat_lifecycle.parse_next_task_args(["R2", "--ignore-live"])["ignore_live"]
    )

  def _search_fixture(self):
    hits = [
        {"cascadeId": "c2", "title": "[10:05] ✓ Topic", "snippet": "found in c2",
         "matchSource": "MATCH_SOURCE_USER", "matchedStepIndex": 4,
         "lastModifiedTime": "2026-10-27T15:00:00Z"},
        {"cascadeId": "solo", "title": "[09:00] ⦿ Other", "snippet": "found in solo",
         "matchSource": "MATCH_SOURCE_AGENT", "matchedStepIndex": 7,
         "lastModifiedTime": "2026-10-27T14:00:00Z"},
    ]
    graph = (
        {"c2": "c1", "c3": "c2"},
        {"c1": ["c2"], "c2": ["c3"]},
        {"c1": "2026-10-27T10:00:00Z", "c2": "2026-10-27T11:00:00Z",
         "c3": "2026-10-27T12:00:00Z", "solo": "2026-10-27T09:00:00Z"},
    )
    return hits, graph

  def test_search_conversations_tags_chain_position(self):
    hits, graph = self._search_fixture()
    with patch.object(chat_lifecycle, "ls_rpc", return_value={"results": hits}) as rpc, \
         patch.object(chat_lifecycle, "_scan_transcript_graph", return_value=graph):
      res = chat_lifecycle.search_conversations("found")
    rpc.assert_called_once_with("SearchConversations", {"query": "found"})
    self.assertFalse(res["capped"])
    by_id = {h["cascadeId"]: h for h in res["hits"]}
    self.assertEqual((by_id["c2"]["piece"], by_id["c2"]["chain_length"]), (2, 3))
    self.assertEqual(by_id["c2"]["chain_tail"], "c3")
    self.assertEqual((by_id["solo"]["piece"], by_id["solo"]["chain_length"]), (1, 1))
    self.assertEqual(by_id["solo"]["chain_tail"], "solo")

  def test_search_conversations_reports_cap(self):
    hits, graph = self._search_fixture()
    many = hits + [{"cascadeId": f"x{i}"} for i in range(98)]
    with patch.object(chat_lifecycle, "ls_rpc", return_value={"results": many}), \
         patch.object(chat_lifecycle, "_scan_transcript_graph", return_value=graph):
      res = chat_lifecycle.search_conversations("found")
    self.assertTrue(res["capped"])

  def test_graph_chain_skips_sibling_branches(self):
    graph = (
        {"b": "a", "c1": "b", "c2": "b", "d": "c2"},
        {"a": ["b"], "b": ["c1", "c2"], "c2": ["d"]},
        {"a": "1", "b": "2", "c1": "3", "c2": "4", "d": "5"},
    )
    self.assertEqual(chat_lifecycle._graph_chain("b", graph), ["a", "b", "c2", "d"])
    self.assertEqual(chat_lifecycle._graph_chain("c1", graph), ["a", "b", "c1"])

  def test_search_chain_finds_every_message_hit_newest_piece_first(self):
    _, graph = self._search_fixture()
    steps = {
        "c1": [{"type": "CORTEX_STEP_TYPE_USER_INPUT", "userInput": {"userResponse": "a Probe"}}],
        "c2": [
            {"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
             "plannerResponse": {"thinking": "probe", "modifiedResponse": "probe one"}},
            {"type": "CORTEX_STEP_TYPE_RUN_COMMAND", "runCommand": {"output": "probe"}},
            {"type": "CORTEX_STEP_TYPE_USER_INPUT", "userInput": {"userResponse": "probe two"}},
        ],
        "c3": [],
    }
    rpc = lambda method, body, timeout=20.0: {"steps": steps[body["cascadeId"]]}
    with patch.object(chat_lifecycle, "ls_rpc", side_effect=rpc), \
         patch.object(chat_lifecycle, "_scan_transcript_graph", return_value=graph):
      hits = chat_lifecycle.search_chain("PROBE", "c1")
    self.assertEqual(
        [(h["cascadeId"], h["step"], h["who"]) for h in hits],
        [("c2", 2, "user"), ("c2", 0, "agent"), ("c1", 0, "user")],
    )
    self.assertEqual(hits[0]["chain_length"], 3)

  def test_urlopen_ls_blocks_unmocked_live_ls_in_unittest(self):
    req = chat_lifecycle.urllib.request.Request("http://localhost:5387/")
    with self.assertRaises(chat_lifecycle.urllib.error.URLError):
      chat_lifecycle._urlopen_ls(req, timeout=0.1)

  def test_is_valid_chat_topic_and_validate_chat_topic(self):
    for bad in [
        "",
        "   ",
        "Investigation",
        "Debugging",
        "cl/12345",
        "PR #99",
        "Bug investigation",
        "Code review",
        "Исследование бага",
        "Task",
        "Custom",
        "Untitled",
        "Работа",
        "Чат",
        "CL",
        "cl 99",
        "General work",
    ]:
      self.assertFalse(chat_lifecycle.is_valid_chat_topic(bad), f"Expected '{bad}' rejected")
      with self.assertRaises(chat_lifecycle.InvalidTopicError):
        chat_lifecycle.validate_chat_topic(bad)

    for good in [
        "PR #194980 multi-span BiDi",
        "FontCollection FaceCache thread safety",
        "R2 b/345678901",
        "Тест № 2",
        "Ban single-word and generic chat titles",
        "Audit visible chats",
    ]:
      self.assertTrue(chat_lifecycle.is_valid_chat_topic(good), f"Expected '{good}' accepted")
      self.assertEqual(chat_lifecycle.validate_chat_topic(good), good)

  def test_derive_fallback_topic_from_state_file_and_prompt(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      st_path = os.path.join(tmp_dir, "state.md")
      with open(st_path, "w", encoding="utf-8") as f:
        f.write("# Chain State: Audit visible chats (R27)\n\n## Goal\nTest\n")
      derived = chat_lifecycle.derive_fallback_topic("nonexistent_id", state_file=st_path)
      self.assertEqual(derived, "Audit visible chats")

    prompt = "Посмотри на PR #999 multi-span BiDi layout crash"
    derived_prompt = chat_lifecycle.derive_fallback_topic("nonexistent_id", prompt=prompt)
    self.assertIn("multi-span BiDi layout", derived_prompt)


if __name__ == "__main__":
  unittest.main()

