#!/usr/bin/env python3
"""Tests for chat_lifecycle.py."""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))

import chat_lifecycle


class TestChatLifecycle(unittest.TestCase):

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

  def test_create_handoff(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      db_path = os.path.join(tmp_dir, chat_lifecycle.SUMMARY_DB_NAME)
      conn = sqlite3.connect(db_path)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, project_id TEXT)"
        )
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)", ("parent-1", "[10:00] ⦿ My Feature", "proj-abc"))
      conn.close()

      mock_run_res = unittest.mock.MagicMock()
      mock_run_res.returncode = 0
      mock_run_res.stdout = '{"conversationId": "child-2"}'

      mock_meta_res = unittest.mock.MagicMock()
      mock_meta_res.returncode = 0
      mock_meta_res.stdout = '{"sourceMetadata": null}'

      def fake_run(cmd, **kwargs):
        if "new-conversation" in cmd:
          return mock_run_res
        if "get-conversation-metadata" in cmd:
          return mock_meta_res
        return unittest.mock.MagicMock(returncode=0, stdout="")

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False), \
           patch.object(chat_lifecycle, "get_current_time_str", return_value="15:45"), \
           patch("subprocess.run", side_effect=fake_run) as mock_subproc:

        summary_file = os.path.join(tmp_dir, "brain", "parent-1", "handoff_summary.md")
        res = chat_lifecycle.create_handoff(
            current_conv_id="parent-1",
            summary_file=summary_file,
            next_step_prompt="Do the next check",
        )

        self.assertEqual(res["new_conversation_id"], "child-2")
        self.assertTrue(res["verified_visible"])
        self.assertTrue(os.path.isfile(summary_file))

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
        conn.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)", ("parent-notes", "[10:00] ⦿ Parent Task", "proj-1"))
      conn.close()

      mock_run_res = unittest.mock.MagicMock(returncode=0, stdout='{"conversationId": "child-notes"}')
      mock_meta_res = unittest.mock.MagicMock(returncode=0, stdout='{"sourceMetadata": null}')

      def fake_run(cmd, **kwargs):
        if "new-conversation" in cmd:
          return mock_run_res
        if "get-conversation-metadata" in cmd:
          return mock_meta_res
        return unittest.mock.MagicMock(returncode=0, stdout="")

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "update_conversation_title_rpc", return_value=False), \
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

  # --- CLI usage -------------------------------------------------------------

  SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks", "chat_lifecycle.py"))
  PUBLIC_COMMANDS = ("status", "set-title", "summary-path", "handoff", "finalize", "reopen")

  def _run_cli(self, *args):
    import subprocess
    return subprocess.run(
        [sys.executable, self.SCRIPT, *args], capture_output=True, text=True, check=False
    )

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


if __name__ == "__main__":
  unittest.main()

