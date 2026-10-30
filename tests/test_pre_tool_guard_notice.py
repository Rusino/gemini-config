#!/usr/bin/env python3
"""Tests for pre_tool_guard_notice.py"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HOOKS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks"))
sys.path.insert(0, HOOKS_DIR)

import pre_tool_guard
import pre_tool_guard_notice


class TestPreToolGuardNotice(unittest.TestCase):

  def setUp(self):
    self.tmp = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.tmp)
    self.marker = os.path.join(self.tmp, "marker.json")
    env = patch.dict(os.environ, {
        "JETSKI_PRE_TOOL_GUARD_MARKER": self.marker,
        "JETSKI_ROADMAPS_DIR": os.path.join(self.tmp, "roadmaps"),
        "ANTIGRAVITY_CONVERSATION_ID": "c1",
    })
    env.start()
    self.addCleanup(env.stop)
    app_dirs = patch.object(
        pre_tool_guard_notice, "APP_DATA_DIR_CANDIDATES", (self.tmp,), create=True
    )
    app_dirs.start()
    self.addCleanup(app_dirs.stop)
    self.chat = {"conversationId": "c1", "artifactDirectoryPath": os.path.join(self.tmp, "brain")}

  def write_marker(self, ts):
    with open(self.marker, "w") as f:
      json.dump({"ts": ts, "conv": "c1", "error": "OSError: boom", "where": "x.py:1 in f"}, f)

  def notices(self, out):
    return [step["ephemeralMessage"] for step in out.get("injectSteps", [])]

  def run_hook(self, path, payload):
    return subprocess.run(
        [path], input=json.dumps(payload), capture_output=True, text=True,
        timeout=10, cwd=os.path.dirname(os.path.dirname(path)))

  def test_new_failures_are_noticed_once_per_interval(self):
    notice = pre_tool_guard_notice.notice
    later = 1002 + pre_tool_guard_notice.NOTICE_INTERVAL_S
    self.write_marker(999)
    self.assertEqual(notice(self.chat, 1000), {})
    self.assertEqual(notice(self.chat, 1001), {})
    self.write_marker(1001.5)
    [msg] = self.notices(notice(self.chat, 1002))
    self.assertIn("OSError: boom (x.py:1 in f)", msg)
    self.assertIn("in this chat", msg)
    self.write_marker(1003)
    self.assertEqual(notice(self.chat, 1004), {})
    self.write_marker(later + 1)
    self.assertEqual(len(self.notices(notice(self.chat, later + 2))), 1)

  def test_subagents_and_battle_mode_are_not_noticed(self):
    for extra in ({"parentConversationId": "p1"}, {"isBattleMode": True}):
      with self.subTest(extra=extra):
        chat = {**self.chat, **extra}
        pre_tool_guard_notice.notice(chat, 1000)
        self.write_marker(1001)
        self.assertEqual(pre_tool_guard_notice.notice(chat, 1002), {})

  def test_macos_subagents_without_parent_field_are_not_noticed(self):
    import sqlite3

    parent_id = "0687185b-9c27-41f8-9a0a-bfec291f51f8"
    sub_db_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    sub_tx_id = "b8d01615-02b4-46ed-93ee-3774ec3a473f"
    top_id = "ac507d76-cae2-4090-a7b1-01321a6ddd00"
    app_dir = os.path.join(self.tmp, "antigravity")
    app_dir2 = os.path.join(self.tmp, "jetski")
    os.makedirs(app_dir, exist_ok=True)
    os.makedirs(app_dir2, exist_ok=True)

    sum_db = os.path.join(app_dir, "conversation_summaries.db")
    conn = sqlite3.connect(sum_db)
    conn.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id TEXT PRIMARY KEY, parent_conversation_id TEXT)"
    )
    conn.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?)", (sub_db_id, parent_id)
    )
    conn.execute(
        "INSERT INTO conversation_summaries VALUES (?, '')", (top_id,)
    )
    conn.commit()
    conn.close()

    sum_db2 = os.path.join(app_dir2, "conversation_summaries.db")
    conn2 = sqlite3.connect(sum_db2)
    conn2.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id TEXT PRIMARY KEY, parent_conversation_id TEXT)"
    )
    conn2.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?)", (top_id, parent_id)
    )
    conn2.commit()
    conn2.close()

    tx_path = os.path.join(
        app_dir, "brain", sub_tx_id, ".system_generated", "logs", "transcript.jsonl"
    )
    os.makedirs(os.path.dirname(tx_path), exist_ok=True)
    with open(tx_path, "w", encoding="utf-8") as f:
      f.write(json.dumps({
          "step_index": 0,
          "source": "SYSTEM",
          "type": "SYSTEM_MESSAGE",
          "content": (
              f"<SYSTEM_MESSAGE>\n[Message] timestamp=2026-10-29T23:33:42Z "
              f"sender={parent_id} priority=MESSAGE_PRIORITY_HIGH content=Run\n"
              "</SYSTEM_MESSAGE>"
          ),
      }) + "\n")

    with patch.object(
        pre_tool_guard_notice, "APP_DATA_DIR_CANDIDATES", (app_dir, app_dir2), create=True
    ):
      for chat in (
          {
              "conversationId": sub_db_id,
              "artifactDirectoryPath": os.path.join(app_dir, "brain", sub_db_id),
          },
          {
              "conversationId": sub_tx_id,
              "transcriptPath": tx_path,
              "artifactDirectoryPath": os.path.join(app_dir, "brain", sub_tx_id),
          },
      ):
        with self.subTest(conv=chat["conversationId"]):
          pre_tool_guard_notice.notice(chat, 1000)
          self.write_marker(1001)
          self.assertEqual(pre_tool_guard_notice.notice(chat, 1002), {})

      top_chat = {
          "conversationId": top_id,
          "artifactDirectoryPath": os.path.join(app_dir, "brain", top_id),
      }
      real_connect = sqlite3.connect
      with patch("sqlite3.connect", wraps=real_connect) as spy_healthy:
        self.write_marker(999)
        self.assertEqual(pre_tool_guard_notice.notice(top_chat, 1000), {})
        self.assertEqual(pre_tool_guard_notice.notice(top_chat, 1001), {})
        self.assertEqual(spy_healthy.call_count, 0)
      self.write_marker(1002)
      with patch("sqlite3.connect", wraps=real_connect) as spy_active:
        self.assertEqual(len(self.notices(pre_tool_guard_notice.notice(top_chat, 1003))), 1)
        self.assertEqual(spy_active.call_count, 1)

  def run_chain(self, sibling, edit):
    # A copy keeps the injected fault away from the live hooks; both hooks run
    # by path, the way hooks.json runs them
    root = tempfile.mkdtemp(dir=self.tmp)
    hooks = shutil.copytree(
        HOOKS_DIR, os.path.join(root, "hooks"), ignore=shutil.ignore_patterns("__pycache__"))
    path = os.path.join(hooks, sibling)
    with open(path) as f:
      src = f.read()
    with open(path, "w") as f:
      f.write(edit(src))
    chat = {**self.chat, "artifactDirectoryPath": os.path.join(root, "brain")}
    notice = os.path.join(hooks, "pre_tool_guard_notice.py")
    first = self.run_hook(notice, chat)
    self.assertEqual((first.returncode, first.stderr, json.loads(first.stdout)), (0, "", {}))
    guard = self.run_hook(os.path.join(hooks, "pre_tool_guard.py"), {
        **chat, "toolCall": {"name": "write_to_file", "args": {"TargetFile": "/tmp/x"}}})
    second = self.run_hook(notice, chat)
    self.assertEqual((second.returncode, second.stderr), (0, ""))
    return guard, self.notices(json.loads(second.stdout))

  def test_guard_failure_reaches_the_next_model_call(self):
    guard, notices = self.run_chain(
        "roadmap.py", lambda src: src + "\n\ndef roadmaps_root():\n  raise OSError('injected')\n")
    self.assertEqual((guard.returncode, json.loads(guard.stdout)), (0, {"decision": "allow"}))
    self.assertIn("OSError: injected", guard.stderr)
    [msg] = notices
    self.assertIn("OSError: injected (roadmap.py:", msg)
    self.assertIn("in this chat", msg)

  def test_broken_sibling_import_fails_open_and_is_noticed(self):
    for sibling in ("roadmap.py", "context_guard.py"):
      with self.subTest(sibling=sibling):
        guard, notices = self.run_chain(sibling, lambda src: src + "\nraise ImportError('injected')\n")
        self.assertEqual((guard.returncode, json.loads(guard.stdout)), (0, {"decision": "allow"}))
        [msg] = notices
        self.assertIn(f"ImportError: injected ({sibling}:", msg)

  def test_hooks_json_runs_the_notice_before_every_model_call(self):
    with open(os.path.join(HOOKS_DIR, "..", "hooks.json")) as f:
      specs = json.load(f).values()
    commands = [
        h["command"] for s in specs if s.get("enabled", True) for h in s.get("PreInvocation", [])]
    self.assertIn("python3 ~/.gemini/config/hooks/pre_tool_guard_notice.py", commands)
    self.assertTrue(os.access(os.path.join(HOOKS_DIR, "pre_tool_guard_notice.py"), os.X_OK))

  def test_marker_path_matches_the_guard(self):
    self.assertEqual(
        pre_tool_guard_notice.FAILURE_MARKER_DEFAULT, pre_tool_guard.FAILURE_MARKER_DEFAULT)


if __name__ == "__main__":
  unittest.main()
