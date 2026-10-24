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

  def tearDown(self):
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

  def test_is_finalized_title(self):
    self.assertTrue(chat_lifecycle.is_finalized_title("[14:20] «» Single Closed"))
    self.assertTrue(chat_lifecycle.is_finalized_title("[14:20] » End Branch"))
    self.assertTrue(chat_lifecycle.is_finalized_title("[14:20] « Start Branch"))
    self.assertTrue(chat_lifecycle.is_finalized_title("[14:20] ‹✓› Mid Branch"))
    self.assertFalse(chat_lifecycle.is_finalized_title("[14:20] ⦿ Active Task"))
    self.assertFalse(chat_lifecycle.is_finalized_title("[14:20] ▸ Start Active"))
    self.assertFalse(chat_lifecycle.is_finalized_title("Normal Title"))
    self.assertFalse(chat_lifecycle.is_finalized_title(""))

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

  def test_finalize_conversation_chain(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      db_path = os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME)
      conn = sqlite3.connect(db_path)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)"
        )
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", ("c1", "[10:00] ▸ First"))
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", ("c2", "[10:30] ✓ Second"))
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", ("c3", "[11:00] ⦿ Third"))
      conn.close()

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False), \
           patch.object(chat_lifecycle, "get_current_time_str", return_value="12:00"):

        # Finalize chain [c1, c2, c3]: markers change, original timestamps stay
        updated = chat_lifecycle.finalize_conversation_chain("c3", ["c1", "c2", "c3"])
        self.assertEqual(updated, ["c1", "c2", "c3"])

        self.assertEqual(chat_lifecycle.get_conversation_title("c1"), "[10:00] « First")
        self.assertEqual(chat_lifecycle.get_conversation_title("c2"), "[10:30] ‹✓› Second")
        self.assertEqual(chat_lifecycle.get_conversation_title("c3"), "[11:00] » Third")

  def test_finalize_single_conversation(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      db_path = os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME)
      conn = sqlite3.connect(db_path)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)"
        )
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", ("c1", "[10:00] ⦿ Solo Task"))
      conn.close()

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False), \
           patch.object(chat_lifecycle, "get_current_time_str", return_value="12:00"):

        updated = chat_lifecycle.finalize_conversation_chain("c1")
        self.assertEqual(updated, ["c1"])
        self.assertEqual(chat_lifecycle.get_conversation_title("c1"), "[10:00] «» Solo Task")

  def test_reopen_conversation(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      db_path = os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME)
      conn = sqlite3.connect(db_path)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)"
        )
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", ("c1", "[10:00] «» Closed Solo"))
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", ("c2", "[10:00] ⦿ Already Active"))
      conn.close()

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False), \
           patch.object(chat_lifecycle, "get_current_time_str", return_value="12:00"):

        # Reopen finalized conversation
        ok = chat_lifecycle.reopen_conversation("c1")
        self.assertTrue(ok)
        self.assertEqual(chat_lifecycle.get_conversation_title("c1"), "[12:00] ⦿ Closed Solo")

        # Reopen already active conversation (without force) does nothing
        ok = chat_lifecycle.reopen_conversation("c2")
        self.assertFalse(ok)
        self.assertEqual(chat_lifecycle.get_conversation_title("c2"), "[10:00] ⦿ Already Active")

  def test_advance_in_progress_chain(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      db_path = os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME)
      conn = sqlite3.connect(db_path)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)"
        )
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", ("c1", "[10:00] ✓ First"))
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", ("c2", "[10:30] ✓ Second"))
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", ("c3", "Third"))
      conn.close()

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False), \
           patch.object(chat_lifecycle, "get_current_time_str", return_value="12:00"):

        updated = chat_lifecycle.advance_in_progress_chain(["c1", "c2", "c3"])
        self.assertEqual(updated, ["c1", "c2", "c3"])

        # Root gets ▸ and preserves original timestamp
        self.assertEqual(chat_lifecycle.get_conversation_title("c1"), "[10:00] ▸ First")
        # Intermediate gets ✓ and preserves original timestamp
        self.assertEqual(chat_lifecycle.get_conversation_title("c2"), "[10:30] ✓ Second")
        # Active focal point gets ⦿ and uses current time (as it had no timestamp)
        self.assertEqual(chat_lifecycle.get_conversation_title("c3"), "[12:00] ⦿ Third")

  def test_advance_single_conversation(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      db_path = os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME)
      conn = sqlite3.connect(db_path)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)"
        )
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", ("c1", "[10:00] Old Title"))
      conn.close()

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False), \
           patch.object(chat_lifecycle, "get_current_time_str", return_value="12:00"):

        updated = chat_lifecycle.advance_in_progress_chain(["c1"])
        self.assertEqual(updated, ["c1"])
        self.assertEqual(chat_lifecycle.get_conversation_title("c1"), "[12:00] ⦿ Old Title")

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
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", ("c2", "[10:00] ⦿ Custom"))
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

        # 2. Already titled chat (with [HH:MM]) is never overwritten
        res2 = chat_lifecycle.auto_title_conversation("c2", "Look at PR #12345")
        self.assertEqual(res2, "")
        self.assertEqual(chat_lifecycle.get_conversation_title("c2"), "[10:00] ⦿ Custom")

        # 3. Prompt without entity does not change title
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
        # Parent must advance from ⦿ to ▸ even though child-2 has no transcript.jsonl yet
        self.assertEqual(chat_lifecycle.get_conversation_title("parent-1"), "[10:00] ▸ My Feature")
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
        self.assertEqual(chat_lifecycle.get_conversation_title("parent-1"), "[10:00] ✓ My Feature")
        self.assertEqual(chat_lifecycle.get_conversation_title("child-2"), "[15:45] ⦿ My Feature")

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
           patch.object(chat_lifecycle, "discover_conversation_chain", return_value=["c1", "c2"]):
        status = chat_lifecycle.get_chain_status("c2")
        self.assertEqual(status["current_conversation_id"], "c2")
        self.assertEqual(status["chain_length"], 2)
        self.assertFalse(status["is_finalized"])
        self.assertEqual(status["chain"][0]["conversation_id"], "c1")
        self.assertEqual(status["chain"][1]["conversation_id"], "c2")
        self.assertTrue(status["chain"][1]["is_current"])
        self.assertEqual(status["summary_files"], [summary_path])

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
        self.assertEqual(chat_lifecycle.get_conversation_title("parent-notes"), "[10:00] ▸ Parent Task")
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

  def test_in_project_handoff_moves_state_file_and_returns_roadmap(self):
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
      self.assertIn("▸ R1 Ship it [child-2]", res["roadmap"])
      prompt = start.call_args[0][2]
      self.assertIn("roadmap.py show", prompt)
      self.assertIn(os.path.dirname(new_state), prompt)
      self.assertIn("Verify the recorded state", prompt)
      self.assertIn("conversation://parent-1", prompt)
      self.assertEqual(chat_lifecycle.HANDOFF_PARENT_RE.search(prompt).group(1), "parent-1")
      parent_dir = os.path.join(tmp_dir, "brain", "parent-1")
      self.assertEqual(os.listdir(parent_dir), ["scratch"])
      with open(os.path.join(parent_dir, "scratch", ".context_guard_state.json"), encoding="utf-8") as f:
        self.assertEqual(json.load(f)["handed_off_to"], "child-2")

      with patch("sys.stdout", new_callable=io.StringIO) as out:
        chat_lifecycle.print_handoff_result(res)
      printed = out.getvalue()
      head, _, tail = printed.partition("\n{")
      self.assertIn("▸ R1 Ship it", head)
      self.assertEqual(json.loads("{" + tail)["new_conversation_id"], "child-2")

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
      self.assertEqual(res["roadmap"], "Roadmap: empty")

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
          "c1": "[10:00] ▸ Topic A",
          "c2": "[10:30] ✓ Topic B",
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
           patch.object(chat_lifecycle, "_fetch_ls_summaries_for_audit", return_value=fake_ls):
        report = chat_lifecycle.audit_conversations(fix=False)
        self.assertEqual(report["total_chains"], 1)
        self.assertEqual(report["total_visible_chats"], 3)
        self.assertEqual(report["discrepancies"], [])

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
        "new_topic": "T", "exact_model": "gemini-x", "force": False,
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
        self.assertEqual(chat_lifecycle.get_conversation_title("parent-1"), "[10:00] ▸ Topic A")
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
      ann_dir = os.path.join(tmp_dir, "annotations")
      os.makedirs(ann_dir, exist_ok=True)

      # Chain c1 -> c2:
      # c1 has wrong intermediate marker ‹✓› instead of «
      # c2 has correct disk title "[13:14] » Moving Chats", but LS RPC has double marker "[13:08] » ⦿ Moving Chats"
      with open(os.path.join(ann_dir, "c1.pbtxt"), "w", encoding="utf-8") as f:
        f.write('title:"[13:00] ‹✓› Moving Chats"\n')
      with open(os.path.join(ann_dir, "c2.pbtxt"), "w", encoding="utf-8") as f:
        f.write('title:"[13:14] » Moving Chats"\n')

      db_path = os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME)
      conn = sqlite3.connect(db_path)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, project_id TEXT)"
        )
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)", ("c1", "[13:00] ‹✓› Moving Chats", "proj-1"))
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)", ("c2", "[13:14] » Moving Chats", "proj-1"))
      conn.close()

      for cid, parent in [("c1", None), ("c2", "c1")]:
        log_dir = os.path.join(tmp_dir, "brain", cid, ".system_generated", "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "transcript.jsonl"), "w", encoding="utf-8") as f:
          if parent:
            f.write(f'{{"type": "USER_INPUT", "step_index": 0, "content": "Continuing work (conversation://{parent})"}}\n')
          else:
            f.write('{"type": "USER_INPUT", "step_index": 0, "content": "Start"}\n')

      fake_ls_summaries = {
          "c1": {
              "summary": "[13:00] ‹✓› Moving Chats",
              "annotations": {"title": "[13:00] ‹✓› Moving Chats"},
              "trajectoryMetadata": {"projectId": "proj-1", "createdAt": "2026-10-01T17:00:00Z"},
          },
          "c2": {
              "summary": "[13:08] » ⦿ Moving Chats",
              "annotations": {"title": "[13:08] » ⦿ Moving Chats"},
              "trajectoryMetadata": {"projectId": "proj-1", "createdAt": "2026-10-01T17:14:00Z"},
          },
      }

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "_fetch_ls_summaries_for_audit", return_value=fake_ls_summaries), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=True):
        # 1. Dry-run audit (fix=False)
        report = chat_lifecycle.audit_conversations(fix=False)
        self.assertEqual(report["total_visible_chats"], 2)
        self.assertEqual(report["total_chains"], 1)
        self.assertEqual(report["issues_count"], 2)
        self.assertEqual(report["fixed_count"], 0)

        by_cid = {d["conversation_id"]: d for d in report["discrepancies"]}
        self.assertIn("marker_mismatch", by_cid["c1"]["issues"])
        self.assertEqual(by_cid["c1"]["expected_title"], "[13:00] « Moving Chats")
        self.assertIn("storage_desync", by_cid["c2"]["issues"])
        self.assertEqual(by_cid["c2"]["expected_title"], "[13:14] » Moving Chats")

        # 2. Fix mode (fix=True)
        fix_report = chat_lifecycle.audit_conversations(fix=True)
        self.assertEqual(fix_report["fixed_count"], 2)
        self.assertEqual(chat_lifecycle.get_conversation_title("c1"), "[13:00] « Moving Chats")
        self.assertEqual(chat_lifecycle.get_conversation_title("c2"), "[13:14] » Moving Chats")

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

  ALL_COMMANDS = PUBLIC_COMMANDS + ("model", "set", "advance")

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

  def test_print_handoff_result_keeps_json_after_roadmap(self):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
      chat_lifecycle.print_handoff_result({
          "new_conversation_id": "child-1",
          "roadmap": "\n".join(f"R{i} item" for i in range(80)),
      })
    out = buf.getvalue()
    self.assertLess(out.index("R79 item"), out.index('"new_conversation_id": "child-1"'))
    self.assertIn('"new_conversation_id": "child-1"', out[-200:])

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
        chat_lifecycle.finalize_conversation_chain("c3")
        idx_path = os.path.join(tmp_dir, "brain", chat_lifecycle.CHAIN_INDEX_FILE_NAME)
        self.assertTrue(os.path.isfile(idx_path))
        with open(idx_path, "r", encoding="utf-8") as f:
          data = json.load(f)
        self.assertEqual(data["parentOf"], {"c2": "c1", "c3": "c2"})


if __name__ == "__main__":
  unittest.main()

