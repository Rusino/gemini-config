#!/usr/bin/env python3
"""Tests for move_chats_to_project.py."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))

import chat_lifecycle
import move_chats_to_project


class TestMoveChatsToProject(unittest.TestCase):

  def test_varint_roundtrip(self):
    test_values = [0, 1, 127, 128, 255, 300, 16384, 2097151, 268435455]
    for val in test_values:
      with self.subTest(val=val):
        encoded = move_chats_to_project.encode_varint(val)
        decoded, offset = move_chats_to_project.decode_varint(encoded, 0)
        self.assertEqual(decoded, val)
        self.assertEqual(offset, len(encoded))

  def test_parse_and_serialize_msg(self):
    # Construct a protobuf msg with a varint (fn=1, wt=0) and string (fn=2, wt=2)
    # fn=1, wt=0 -> tag = (1 << 3) | 0 = 8
    # fn=2, wt=2 -> tag = (2 << 3) | 2 = 18
    raw = bytearray()
    raw.extend(move_chats_to_project.encode_varint(8))
    raw.extend(move_chats_to_project.encode_varint(42))
    raw.extend(move_chats_to_project.encode_varint(18))
    string_bytes = b"hello"
    raw.extend(move_chats_to_project.encode_varint(len(string_bytes)))
    raw.extend(string_bytes)

    fields = move_chats_to_project.parse_msg(bytes(raw))
    serialized = move_chats_to_project.serialize_msg(fields)
    self.assertEqual(serialized, bytes(raw))

  def test_update_metadata_project(self):
    proj_id_1 = "project-alpha"
    meta_bytes = move_chats_to_project.update_metadata_project(b"", proj_id_1)
    fields = move_chats_to_project.parse_msg(meta_bytes)
    # Field 18 should be proj_id_1
    field_18 = [val for tag, fn, wt, val in fields if fn == 18]
    self.assertEqual(len(field_18), 1)
    self.assertEqual(field_18[0], proj_id_1.encode("utf-8"))

    # Now update existing project_id
    proj_id_2 = "project-beta"
    updated_meta = move_chats_to_project.update_metadata_project(meta_bytes, proj_id_2)
    fields2 = move_chats_to_project.parse_msg(updated_meta)
    field_18_2 = [val for tag, fn, wt, val in fields2 if fn == 18]
    self.assertEqual(len(field_18_2), 1)
    self.assertEqual(field_18_2[0], proj_id_2.encode("utf-8"))

  def test_update_summary_project(self):
    proj_id = "test-project-xyz"
    summary_bytes = move_chats_to_project.update_summary_project(b"", proj_id)
    # Parse summary fields -> field 17 should contain metadata with field 18
    fields = move_chats_to_project.parse_msg(summary_bytes)
    field_17 = [val for tag, fn, wt, val in fields if fn == 17]
    self.assertEqual(len(field_17), 1)
    meta_fields = move_chats_to_project.parse_msg(field_17[0])
    field_18 = [val for tag, fn, wt, val in meta_fields if fn == 18]
    self.assertEqual(len(field_18), 1)
    self.assertEqual(field_18[0], proj_id.encode("utf-8"))

  def test_update_conversation_db(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      conv_dir = os.path.join(tmp_dir, "conversations")
      os.makedirs(conv_dir, exist_ok=True)
      cid = "conv-abc"
      db_file = os.path.join(conv_dir, f"{cid}.db")

      conn = sqlite3.connect(db_file)
      with conn:
        conn.execute("CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY, data BLOB)")
      conn.close()

      ok = move_chats_to_project.update_conversation_db(tmp_dir, cid, "proj-new")
      self.assertTrue(ok)

      conn = sqlite3.connect(db_file)
      cur = conn.cursor()
      cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'")
      row = cur.fetchone()
      conn.close()

      meta_fields = move_chats_to_project.parse_msg(row[0])
      proj_val = [val for tag, fn, wt, val in meta_fields if fn == 18][0]
      self.assertEqual(proj_val, b"proj-new")

  def test_resolve_project_id_by_name_and_badge(self):
    with tempfile.TemporaryDirectory() as pdir:
      pid_wp = "2f999b18-d1cd-4eb5-9bdd-1c8fe328762e"
      pid_bc = "a29f214d-9fb4-46fd-a8f6-7d60a7e2aaa3"
      with open(os.path.join(pdir, f"{pid_wp}.json"), "w", encoding="utf-8") as f:
        json.dump({"id": pid_wp, "name": "WebParagraph · ⟳ 2 ⚠ 1"}, f)
      with open(os.path.join(pdir, f"{pid_bc}.json"), "w", encoding="utf-8") as f:
        json.dump({"id": pid_bc, "name": "Breaking Chats"}, f)

      self.assertEqual(
          move_chats_to_project.resolve_project_id("webparagraph", projects_dir=pdir),
          pid_wp,
      )
      self.assertEqual(
          move_chats_to_project.resolve_project_id("WebParagraph · ⟳ 1", projects_dir=pdir),
          pid_wp,
      )
      self.assertEqual(
          move_chats_to_project.resolve_project_id("Breaking Chats", projects_dir=pdir),
          pid_bc,
      )
      self.assertEqual(
          move_chats_to_project.resolve_project_id(pid_bc, projects_dir=pdir),
          pid_bc,
      )
      self.assertEqual(
          move_chats_to_project.resolve_project_id("unassigned", projects_dir=pdir),
          "outside-of-project",
      )
      with self.assertRaises(ValueError):
        move_chats_to_project.resolve_project_id("Nonexistent Project", projects_dir=pdir)

  def test_expand_conversations_with_chains(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      for cid, parent in [("c1", None), ("c2", "c1"), ("c3", "c2")]:
        log_dir = os.path.join(tmp_dir, "brain", cid, ".system_generated", "logs")
        os.makedirs(log_dir, exist_ok=True)
        tr_file = os.path.join(log_dir, "transcript.jsonl")
        content = (
            f'{{"type": "USER_INPUT", "step_index": 0, "content": "Continuing work from previous conversation (conversation://{parent})"}}\n'
            if parent
            else '{"type": "USER_INPUT", "step_index": 0, "content": "Initial prompt"}\n'
        )
        with open(tr_file, "w", encoding="utf-8") as f:
          f.write(content)

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]):
        # Passing root ID c1 or intermediate ID c2 expands to the full chain [c1, c2, c3]
        self.assertEqual(
            move_chats_to_project.expand_conversations_with_chains(["c1"]),
            ["c1", "c2", "c3"],
        )
        self.assertEqual(
            move_chats_to_project.expand_conversations_with_chains(["c2", "standalone"]),
            ["c1", "c2", "c3", "standalone"],
        )

  def test_list_unassigned_conversations(self):
    with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as pdir:
      pid_wp = "2f999b18-d1cd-4eb5-9bdd-1c8fe328762e"
      with open(os.path.join(pdir, f"{pid_wp}.json"), "w", encoding="utf-8") as f:
        json.dump({"id": pid_wp, "name": "WebParagraph"}, f)

      db_path = os.path.join(tmp_dir, "conversation_summaries.db")
      conn = sqlite3.connect(db_path)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, project_id TEXT)"
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
            ("u1", "[10:00] ▸ Unassigned Chain", "outside-of-project"),
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
            ("u2", "[10:30] ⦿ Unassigned Chain", "outside-of-project"),
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
            ("a1", "[11:00] «» Assigned Chat", pid_wp),
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
            ("arch1", "[11:15] «» Archived Unassigned", "outside-of-project"),
        )
      conn.close()

      # Mark arch1 as archived in annotations
      ann_dir = os.path.join(tmp_dir, "annotations")
      os.makedirs(ann_dir, exist_ok=True)
      with open(os.path.join(ann_dir, "arch1.pbtxt"), "w", encoding="utf-8") as f:
        f.write('title:"[11:15] «» Archived Unassigned" archived:true\n')

      # Transcripts for u1 -> u2
      for cid, parent in [("u1", None), ("u2", "u1")]:
        log_dir = os.path.join(tmp_dir, "brain", cid, ".system_generated", "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "transcript.jsonl"), "w", encoding="utf-8") as f:
          if parent:
            f.write(
                f'{{"type": "USER_INPUT", "step_index": 0, "content": "Continuing work (conversation://{parent})"}}\n'
            )
          else:
            f.write('{"type": "USER_INPUT", "step_index": 0, "content": "Start"}\n')

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "_fetch_ls_summaries_for_audit", return_value=None):
        items = move_chats_to_project.list_unassigned_conversations(
            app_data_dir=tmp_dir, projects_dir=pdir
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["root_id"], "u1")
        self.assertEqual(items[0]["tail_id"], "u2")
        self.assertEqual(items[0]["chain_ids"], ["u1", "u2"])
        self.assertEqual(items[0]["unassigned_count"], 2)
        report = move_chats_to_project.format_unassigned_report(items)
        self.assertIn("Unassigned conversations: 2 across 1 chain(s)/task(s):", report)
        self.assertIn("Unassigned Chain", report)

  def test_list_unassigned_groups_mixed_topic_chain(self):
    with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as pdir:
      pid_wp = "2f999b18-d1cd-4eb5-9bdd-1c8fe328762e"
      with open(os.path.join(pdir, f"{pid_wp}.json"), "w", encoding="utf-8") as f:
        json.dump({"id": pid_wp, "name": "WebParagraph"}, f)

      conn = sqlite3.connect(os.path.join(tmp_dir, "conversation_summaries.db"))
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, project_id TEXT)"
        )
        for cid, title, pid in [
            ("u1", "[10:00] « Topic A", "outside-of-project"),
            ("u2", "[10:30] » Topic B", "outside-of-project"),
            ("u3", "[11:00] ⦿ Topic C", pid_wp),
        ]:
          conn.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)", (cid, title, pid))
      conn.close()

      for cid, parent in [("u1", None), ("u2", "u1"), ("u3", "u2")]:
        log_dir = os.path.join(tmp_dir, "brain", cid, ".system_generated", "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "transcript.jsonl"), "w", encoding="utf-8") as f:
          if parent:
            f.write(
                f'{{"type": "USER_INPUT", "step_index": 0, "content": "Continuing work (conversation://{parent})"}}\n'
            )
          else:
            f.write('{"type": "USER_INPUT", "step_index": 0, "content": "Start"}\n')

      with patch.object(chat_lifecycle, "find_app_data_dirs", return_value=[tmp_dir]), \
           patch.object(chat_lifecycle, "_fetch_ls_summaries_for_audit", return_value=None):
        items = move_chats_to_project.list_unassigned_conversations(
            app_data_dir=tmp_dir, projects_dir=pdir
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["chain_ids"], ["u1", "u2", "u3"])
        self.assertEqual(items[0]["unassigned_ids"], ["u1", "u2"])
        self.assertEqual(items[0]["assigned_projects_in_chain"], ["WebParagraph"])
        self.assertEqual(items[0]["base_topic"], "Topic C")

  def test_wait_for_idle_ls(self):
    polls = [
        {"c1": {"status": "CASCADE_RUN_STATUS_RUNNING", "summary": "Busy"}},
        {"c1": {"status": "CASCADE_RUN_STATUS_IDLE", "summary": "Busy"}},
        {"c1": {"status": "CASCADE_RUN_STATUS_IDLE", "summary": "Busy"}},
    ]
    with patch.object(
        chat_lifecycle, "_fetch_ls_summaries_for_audit", side_effect=polls
    ):
      ok = move_chats_to_project.wait_for_idle_ls(
          poll_sec=0.01, required_quiet_checks=2, max_wait_sec=1.0
      )
      self.assertTrue(ok)

  def test_update_ls_summaries_project_and_wait_idle_target_flush(self):
    summaries = {
        "moved-chat": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "summary": "Moved",
            "trajectoryMetadata": {"projectId": "old-proj"},
        },
        "other-chat": {
            "status": "CASCADE_RUN_STATUS_RUNNING",
            "summary": "Other",
            "trajectoryMetadata": {"projectId": "other-proj"},
        },
    }
    polls = [
        summaries,
        summaries,
        {
            **summaries,
            "other-chat": {
                "status": "CASCADE_RUN_STATUS_IDLE",
                "summary": "Other",
                "trajectoryMetadata": {"projectId": "other-proj"},
            },
        },
        {
            **summaries,
            "other-chat": {
                "status": "CASCADE_RUN_STATUS_IDLE",
                "summary": "Other",
                "trajectoryMetadata": {"projectId": "other-proj"},
            },
        },
        {
            **summaries,
            "other-chat": {
                "status": "CASCADE_RUN_STATUS_IDLE",
                "summary": "Other",
                "trajectoryMetadata": {"projectId": "other-proj"},
            },
        },
    ]
    rpc_calls = []

    def fake_rpc(method, body, timeout=20.0):
      rpc_calls.append((method, body))
      return {}

    with patch.object(chat_lifecycle, "_fetch_ls_summaries_for_audit", side_effect=polls), \
         patch.object(chat_lifecycle, "ls_rpc", side_effect=fake_rpc):
      ok = move_chats_to_project.wait_for_idle_ls(
          poll_sec=0.01,
          required_quiet_checks=2,
          max_wait_sec=1.0,
          target_project="new-proj",
          target_conv_ids=["moved-chat"],
      )
      self.assertTrue(ok)
      self.assertEqual(len(rpc_calls), 1)
      self.assertEqual(rpc_calls[0][0], "JetboxWriteSummary")
      self.assertEqual(rpc_calls[0][1]["cascadeId"], "moved-chat")
      self.assertEqual(
          rpc_calls[0][1]["summary"]["trajectoryMetadata"]["projectId"],
          "new-proj",
      )

  def test_move_chain_state_files_and_restart_applies_immediate_updates(self):
    import roadmap

    with tempfile.TemporaryDirectory() as tmp_dir, \
         tempfile.TemporaryDirectory() as pdir, \
         tempfile.TemporaryDirectory() as rdir:
      pid_old = "a29f214d-9fb4-46fd-a8f6-7d60a7e2aaa3"
      pid_new = "eb394750-7b65-4e2b-9f16-a169a3caab9f"
      with open(os.path.join(pdir, f"{pid_new}.json"), "w", encoding="utf-8") as f:
        json.dump({"id": pid_new, "name": "Skia Gardener"}, f)

      conv_dir = os.path.join(tmp_dir, "conversations")
      os.makedirs(conv_dir, exist_ok=True)
      cid = "ce0d3216-09d4-40d2-b849-329e5e6351e4"
      conn = sqlite3.connect(os.path.join(conv_dir, f"{cid}.db"))
      with conn:
        conn.execute("CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY, data BLOB)")
      conn.close()

      sum_db = os.path.join(tmp_dir, "conversation_summaries.db")
      conn = sqlite3.connect(sum_db)
      with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, project_id TEXT, raw_summary BLOB)"
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?)",
            (cid, "[12:36] ⦿ Skia Gardener", pid_old, b""),
        )
      conn.close()

      with patch.dict(
          os.environ,
          {
              "ANTIGRAVITY_APP_DATA_DIR": tmp_dir,
              roadmap.ROADMAPS_DIR_ENV: rdir,
          },
      ):
        old_state = roadmap.state_path(pid_old, cid)
        os.makedirs(os.path.dirname(old_state), exist_ok=True)
        with open(old_state, "w", encoding="utf-8") as f:
          f.write("# State\n")

        with patch.object(move_chats_to_project, "schedule_offline_migration") as sched_mock, \
             patch.object(move_chats_to_project, "update_ls_summaries_project", return_value=1) as ls_mock, \
             patch.object(
                 sys,
                 "argv",
                 [
                     "move_chats_to_project.py",
                     "--project",
                     "Skia Gardener",
                     "--projects-dir",
                     pdir,
                     "--restart",
                     cid,
                 ],
             ):
          move_chats_to_project.main()

        sched_mock.assert_called_once()
        ls_mock.assert_called_once_with([cid], pid_new)
        conn = sqlite3.connect(sum_db)
        row = conn.execute(
            "SELECT project_id FROM conversation_summaries WHERE conversation_id = ?",
            (cid,),
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], pid_new)
        self.assertFalse(os.path.exists(old_state))
        self.assertTrue(os.path.isfile(roadmap.state_path(pid_new, cid)))


if __name__ == "__main__":
  unittest.main()
