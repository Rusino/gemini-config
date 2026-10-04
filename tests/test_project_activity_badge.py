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
        ("Breaking Chats", 0, 0, 0, "Breaking Chats"),
        ("Breaking Chats", 1, 0, 0, "Breaking Chats · ⟳ 1"),
        ("Breaking Chats · ⟳ 2", 1, 0, 0, "Breaking Chats · ⟳ 1"),
        ("Breaking Chats · ⟳ 1", 0, 0, 1, "Breaking Chats · ● 1"),
        ("Breaking Chats · ● 1", 0, 0, 0, "Breaking Chats"),
        ("WebParagraph", 0, 2, 0, "WebParagraph · ⚠ 2"),
        ("WebParagraph · ⚠ 1", 2, 1, 3, "WebParagraph · ⟳ 2 ⚠ 1 ● 3"),
        ("Skia Gardener · ⟳ 1 · ● 2", 0, 0, 0, "Skia Gardener"),
    ]
    for raw_name, running, blocked, unread, expected in cases:
      with self.subTest(
          raw_name=raw_name, running=running, blocked=blocked, unread=unread
      ):
        formatted = badge.format_project_name(
            raw_name, running, blocked, unread
        )
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
          "c-unread-newer-lmt": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T00:42:09.100Z",
              "annotations": {"lastUserViewTime": "2026-10-04T00:42:09Z"},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-unread-never-viewed": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T00:40:00Z",
              "trajectoryMetadata": {"projectId": "proj-2"},
          },
          "c-read": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T00:42:09Z",
              "annotations": {"lastUserViewTime": "2026-10-04T00:42:09.100Z"},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-archived": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T00:50:00Z",
              "annotations": {"archived": True},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-subagent": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T00:50:00Z",
              "trajectoryMetadata": {
                  "projectId": "proj-1",
                  "parentConversationId": "c-running",
              },
          },
          "c-hidden-tool": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T00:50:00Z",
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

      running, blocked, unread = badge.compute_project_activity(
          summaries, known_projects, folder_to_project
      )
      self.assertEqual(running, {"proj-1": 1, "proj-2": 1})
      self.assertEqual(blocked, {"proj-1": 1})
      self.assertEqual(unread, {"proj-1": 1, "proj-2": 1})

  def test_handed_off_predecessors_excluded_from_unread(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      p1 = {
          "id": "proj-1",
          "name": "Project One",
          "projectResources": {"resources": []},
      }
      with open(os.path.join(tmp_dir, "proj-1.json"), "w") as f:
        json.dump(p1, f)
      known_projects, folder_to_project = badge.load_projects(tmp_dir)

      summaries = {
          "c-start": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T01:08:00Z",
              "annotations": {"title": "[21:07] ▸ Task Chain"},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-step": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T01:09:00Z",
              "annotations": {"title": "[21:07] ✓ Task Chain"},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-closed-start": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T01:09:00Z",
              "annotations": {"title": "[21:07] « Closed Chain"},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-closed-step": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T01:09:00Z",
              "annotations": {"title": "[21:07] ‹✓› Closed Chain"},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-leaf-active": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T01:10:00Z",
              "annotations": {"title": "[21:07] ⦿ Task Chain"},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-single-closed": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T01:10:00Z",
              "annotations": {"title": "[21:07] «» Standalone Closed"},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
      }
      _, _, unread = badge.compute_project_activity(
          summaries, known_projects, folder_to_project
      )
      self.assertEqual(unread, {"proj-1": 2})

  def test_resolve_currently_viewing_cid_and_suppress_unread(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      state_file = os.path.join(tmp_dir, "view_state.json")
      p1 = {
          "id": "proj-1",
          "name": "Project One",
          "projectResources": {"resources": []},
      }
      with open(os.path.join(tmp_dir, "proj-1.json"), "w") as f:
        json.dump(p1, f)
      known_projects, folder_to_project = badge.load_projects(tmp_dir)

      prev_pb = os.path.join(tmp_dir, "c-prev.pbtxt")
      curr_pb = os.path.join(tmp_dir, "c-curr.pbtxt")
      with open(prev_pb, "w") as f:
        f.write("prev")
      with open(curr_pb, "w") as f:
        f.write("curr")
      os.utime(prev_pb, ns=(1000, 1000))
      os.utime(curr_pb, ns=(1000, 2000))

      summaries = {
          "c-prev": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T00:45:00Z",
              "annotations": {"lastUserViewTime": "2026-10-04T00:40:00Z"},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-curr": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T00:46:00Z",
              "annotations": {"lastUserViewTime": "2026-10-04T00:40:00Z"},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
      }

      current_cid, viewed_lmt = badge.resolve_currently_viewing_cid(
          summaries, annotations_dir=tmp_dir, state_file=state_file
      )
      self.assertEqual(current_cid, "c-curr")

      # Even if c-prev.pbtxt is later touched by a background title update,
      # the cached winner for max_luvt remains c-curr
      os.utime(prev_pb, ns=(1000, 9000))
      current_cid2, viewed_lmt2 = badge.resolve_currently_viewing_cid(
          summaries, annotations_dir=tmp_dir, state_file=state_file
      )
      self.assertEqual(current_cid2, "c-curr")

      # c-curr is open on screen so it is not unread; c-prev finished in the
      # background after the switch so it is unread
      _, _, unread = badge.compute_project_activity(
          summaries,
          known_projects,
          folder_to_project,
          currently_viewing_cid=current_cid2,
          viewed_lmt=viewed_lmt2,
      )
      self.assertEqual(unread, {"proj-1": 1})

      # Once the user clicks back onto c-prev, its lastUserViewTime advances
      summaries["c-prev"]["annotations"]["lastUserViewTime"] = (
          "2026-10-04T00:47:00Z"
      )
      current_cid3, viewed_lmt3 = badge.resolve_currently_viewing_cid(
          summaries, annotations_dir=tmp_dir, state_file=state_file
      )
      self.assertEqual(current_cid3, "c-prev")
      _, _, unread_after = badge.compute_project_activity(
          summaries,
          known_projects,
          folder_to_project,
          currently_viewing_cid=current_cid3,
          viewed_lmt=viewed_lmt3,
      )
      self.assertEqual(unread_after, {})

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

      summaries_idle = {
          "cid-1": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T00:50:00Z",
              "trajectoryMetadata": {"projectId": "proj-1"},
          }
      }
      running, blocked, unread = badge.compute_project_activity(
          summaries_idle,
          known_projects,
          folder_to_project,
          force_running_cid="cid-1",
      )
      self.assertEqual(running, {"proj-1": 1})
      self.assertEqual(blocked, {})
      self.assertEqual(unread, {})

      running2, _, _ = badge.compute_project_activity(
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
          tmp_dir, {"proj-1": 2}, {"proj-1": 1}, {"proj-1": 3}
      )
      self.assertEqual(
          changes,
          [("proj-1", "Breaking Chats", "Breaking Chats · ⟳ 2 ⚠ 1 ● 3")],
      )

      with open(pfile, "r") as f:
        updated = json.load(f)
      self.assertEqual(updated["name"], "Breaking Chats · ⟳ 2 ⚠ 1 ● 3")
      self.assertEqual(updated["settings"], {"custom": True})

      changes_noop = badge.apply_project_badges(
          tmp_dir, {"proj-1": 2}, {"proj-1": 1}, {"proj-1": 3}
      )
      self.assertEqual(changes_noop, [])

      cleared = badge.clear_all_badges(tmp_dir)
      self.assertEqual(
          cleared,
          [("proj-1", "Breaking Chats · ⟳ 2 ⚠ 1 ● 3", "Breaking Chats")],
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
