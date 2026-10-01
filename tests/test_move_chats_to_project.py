#!/usr/bin/env python3
"""Tests for move_chats_to_project.py."""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

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


if __name__ == "__main__":
  unittest.main()
