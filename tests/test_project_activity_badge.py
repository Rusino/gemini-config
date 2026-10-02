#!/usr/bin/env python3
"""Tests for project_activity_badge.py."""

import io
import json
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks"))
)

import project_activity_badge as badge


class TestProjectActivityBadge(unittest.TestCase):

  def test_strip_and_format_idempotence(self):
    cases = [
        ("Breaking Chats", 0, 0, "Breaking Chats"),
        ("Breaking Chats", 1, 0, "Breaking Chats · ⟳ 1"),
        ("Breaking Chats · ⟳ 2", 1, 0, "Breaking Chats · ⟳ 1"),
        ("Breaking Chats · ⟳ 1", 0, 0, "Breaking Chats"),
        ("WebParagraph", 0, 2, "WebParagraph · ⚠ 2"),
        ("WebParagraph · ⚠ 1", 2, 1, "WebParagraph · ⟳ 2 ⚠ 1"),
        ("Skia Gardener · ⟳ 1 · ⟳ 3", 0, 0, "Skia Gardener"),
    ]
    for raw_name, running, blocked, expected in cases:
      with self.subTest(raw_name=raw_name, running=running, blocked=blocked):
        formatted = badge.format_project_name(raw_name, running, blocked)
        self.assertEqual(formatted, expected)
        self.assertEqual(
            badge.strip_activity_badge(formatted),
            badge.strip_activity_badge(raw_name),
        )

  def test_compute_project_activity_filters_and_fallbacks(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      p1 = {
          "id": "proj-1",
          "name": "Project One",
          "projectResources": {
              "resources": [{
                  "gitFolder": {"folderUri": "file:///workspace/repo"}
              }]
          },
      }
      p2 = {
          "id": "proj-2",
          "name": "Project Two",
          "projectResources": {
              "resources": [{
                  "gitFolder": {"folderUri": "file:///workspace/repo/nested"}
              }]
          },
      }
      for p in (p1, p2):
        with open(os.path.join(tmp_dir, f"{p['id']}.json"), "w") as f:
          json.dump(p, f)

      known_projects, folder_to_project = badge.load_projects(tmp_dir)

      summaries = {
          "c-running": {
              "status": "CASCADE_RUN_STATUS_RUNNING",
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-blocked": {
              "status": "CASCADE_RUN_STATUS_RUNNING",
              "waitingSteps": [{"stepIndex": 3}],
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-archived": {
              "status": "CASCADE_RUN_STATUS_RUNNING",
              "annotations": {"archived": True},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-subagent": {
              "status": "CASCADE_RUN_STATUS_RUNNING",
              "trajectoryMetadata": {
                  "projectId": "proj-1",
                  "parentConversationId": "c-running",
              },
          },
          "c-hidden-tool": {
              "status": "CASCADE_RUN_STATUS_RUNNING",
              "trajectoryMetadata": {
                  "projectId": "proj-1",
                  "sourceMetadata": {"tool": "agentapi"},
              },
          },
          "c-ws-fallback-nested": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "notFullyIdle": True,
              "workspaces": [{
                  "workspaceFolderAbsoluteUri": (
                      "file:///workspace/repo/nested/sub"
                  )
              }],
          },
      }

      running, blocked = badge.compute_project_activity(
          summaries, known_projects, folder_to_project
      )
      self.assertEqual(running, {"proj-1": 1, "proj-2": 1})
      self.assertEqual(blocked, {"proj-1": 1})

  def test_force_running_cid_for_new_or_starting_chat(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      p1 = {
          "id": "proj-1",
          "name": "Project One",
          "projectResources": {
              "resources": [{"gitFolder": {"folderUri": "file:///ws/one"}}]
          },
      }
      with open(os.path.join(tmp_dir, "proj-1.json"), "w") as f:
        json.dump(p1, f)

      known_projects, folder_to_project = badge.load_projects(tmp_dir)

      # Case 1: Chat is in summaries as IDLE during PreInvocation
      summaries_idle = {
          "cid-1": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "trajectoryMetadata": {"projectId": "proj-1"},
          }
      }
      running, blocked = badge.compute_project_activity(
          summaries_idle,
          known_projects,
          folder_to_project,
          force_running_cid="cid-1",
      )
      self.assertEqual(running, {"proj-1": 1})
      self.assertEqual(blocked, {})

      # Case 2: Brand-new chat not in summaries yet, matched via workspace
      running2, _ = badge.compute_project_activity(
          {},
          known_projects,
          folder_to_project,
          force_running_cid="cid-new",
          force_running_workspaces=["/ws/one/subdir"],
      )
      self.assertEqual(running2, {"proj-1": 1})

  def test_apply_and_clear_project_badges(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      pfile = os.path.join(tmp_dir, "proj-1.json")
      original = {
          "id": "proj-1",
          "name": "Breaking Chats",
          "projectResources": {"resources": []},
          "settings": {"custom": True},
          "isWorkspaceOnly": False,
      }
      with open(pfile, "w") as f:
        json.dump(original, f)

      changes = badge.apply_project_badges(
          tmp_dir, {"proj-1": 2}, {"proj-1": 1}
      )
      self.assertEqual(
          changes,
          [("proj-1", "Breaking Chats", "Breaking Chats · ⟳ 2 ⚠ 1")],
      )

      with open(pfile, "r") as f:
        updated = json.load(f)
      self.assertEqual(updated["name"], "Breaking Chats · ⟳ 2 ⚠ 1")
      self.assertEqual(updated["settings"], {"custom": True})

      # Second call with same counts should be a no-op (zero writes)
      changes_noop = badge.apply_project_badges(
          tmp_dir, {"proj-1": 2}, {"proj-1": 1}
      )
      self.assertEqual(changes_noop, [])

      # Clearing restores exact original name
      cleared = badge.clear_all_badges(tmp_dir)
      self.assertEqual(
          cleared,
          [("proj-1", "Breaking Chats · ⟳ 2 ⚠ 1", "Breaking Chats")],
      )
      with open(pfile, "r") as f:
        final = json.load(f)
      self.assertEqual(final["name"], "Breaking Chats")

  def test_handle_hook_invocation_outputs_empty_json_and_spawns(self):
    fake_stdin = io.StringIO(
        json.dumps({
            "conversationId": "conv-123",
            "workspacePaths": ["/ws/a"],
        })
    )
    fake_stdout = io.StringIO()
    with (
        mock.patch.object(sys, "stdin", fake_stdin),
        mock.patch.object(sys, "stdout", fake_stdout),
        mock.patch.object(badge, "spawn_detached_trigger") as mock_spawn,
    ):
      badge.handle_hook_invocation("PreInvocation", "/tmp/projects")
      self.assertEqual(fake_stdout.getvalue(), "{}\n")
      mock_spawn.assert_called_once_with(
          event="PreInvocation",
          cid="conv-123",
          project_id=mock.ANY,
          workspace_paths=["/ws/a"],
          projects_dir="/tmp/projects",
      )

  def test_fetch_jetbox_summaries_burst_parses_connect_frames(self):
    pkt = json.dumps({
        "updates": {
            "ae64a06d": {
                "status": "CASCADE_RUN_STATUS_IDLE",
                "notFullyIdle": True,
                "trajectoryMetadata": {"projectId": "webparagraph"},
            }
        }
    }).encode("utf-8")
    stream_bytes = struct.pack(">BI", 0, len(pkt)) + pkt + struct.pack(">BI", 2, 2) + b"{}"

    class FakeResp(io.BytesIO):
      def __enter__(self):
        return self

      def __exit__(self, *args):
        self.close()

    with mock.patch("urllib.request.urlopen", return_value=FakeResp(stream_bytes)):
      res = badge._fetch_jetbox_summaries_burst("localhost:5387", "tok")
      self.assertEqual(
          res,
          {
              "ae64a06d": {
                  "status": "CASCADE_RUN_STATUS_IDLE",
                  "notFullyIdle": True,
                  "trajectoryMetadata": {"projectId": "webparagraph"},
              }
          },
      )


if __name__ == "__main__":
  unittest.main()
