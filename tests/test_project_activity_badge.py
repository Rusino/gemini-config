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

  def test_archived_chats_excluded_from_unread(self):
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
              "annotations": {"title": "[21:07] ▸ Task Chain", "archived": True},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-step": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T01:09:00Z",
              "annotations": {"title": "[21:07] ✓ Task Chain", "archived": True},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-closed-start": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T01:09:00Z",
              "annotations": {"title": "[21:07] « Closed Chain", "archived": True},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-closed-step": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T01:09:00Z",
              "annotations": {"title": "[21:07] ‹✓› Closed Chain", "archived": True},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-closed-end": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T01:09:00Z",
              "annotations": {"title": "[21:07] » Closed Chain", "archived": True},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-single-closed": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T01:10:00Z",
              "annotations": {"title": "[21:07] «» Standalone Closed", "archived": True},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-leaf-active": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T01:10:00Z",
              "annotations": {"title": "[21:07] ⦿ Task Chain"},
              "trajectoryMetadata": {"projectId": "proj-1"},
          },
          "c-unmarked-quoted": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-04T01:10:00Z",
              "annotations": {"title": "«Quoted» topic without time prefix"},
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

  # Verifies self-healing of `+x` bits stripped by `.tmp` -> `mv` hook overwrites
  def test_ensure_hooks_executable_restores_dropped_exec_bit(self):
    with tempfile.TemporaryDirectory() as tmp:
      hook_py = os.path.join(tmp, "context_guard.py")
      with open(hook_py, "w") as f:
        f.write("#!/usr/bin/env python3\n")
      os.chmod(hook_py, 0o644)
      self.assertFalse(os.access(hook_py, os.X_OK))
      restored = badge.ensure_hooks_executable(tmp)
      self.assertEqual(restored, ["context_guard.py"])
      self.assertTrue(os.access(hook_py, os.X_OK))
      self.assertEqual(badge.ensure_hooks_executable(tmp), [])

  def test_waiting_cron_chat_not_counted_as_running_when_idle_between_ticks(self):
    known_projects = {"p1": ("/tmp/p1.json", {"id": "p1", "name": "Flight Deck"})}
    summaries = {
        "c-cron": {
            "summary": "[10:00] ⏳ Flight Deck",
            "status": "CASCADE_RUN_STATUS_IDLE",
            "notFullyIdle": True,
            "lastModifiedTime": "2026-10-26T20:00:00Z",
            "annotations": {"lastUserViewTime": "2026-10-26T10:00:00Z"},
            "trajectoryMetadata": {"projectId": "p1"},
        },
        "c-active": {
            "summary": "[10:05] ⦿ Active Subagent Chat",
            "status": "CASCADE_RUN_STATUS_IDLE",
            "notFullyIdle": True,
            "trajectoryMetadata": {"projectId": "p1"},
        },
    }
    running, blocked, unread = badge.compute_project_activity(
        summaries, known_projects, []
    )
    self.assertEqual(running["p1"], 1)
    self.assertEqual(blocked.get("p1", 0), 0)
    self.assertEqual(unread.get("p1", 0), 0)

    summaries["c-cron"]["status"] = "CASCADE_RUN_STATUS_RUNNING"
    running2, _, _ = badge.compute_project_activity(
        summaries, known_projects, []
    )
    self.assertEqual(running2["p1"], 2)

  def test_urlopen_ls_blocks_unmocked_live_ls_in_unittest(self):
    req = badge.urllib.request.Request("http://localhost:5387/")
    with self.assertRaises(badge.urllib.error.URLError):
      badge._urlopen_ls(req, timeout=0.1)

  def test_trailing_waiting_step_overrides_idle_and_running_with_detail(self):
    known_projects = {"p1": ("/tmp/p1.json", {"id": "p1", "name": "Breaking Chats"})}
    summaries = {
        "c-perm-idle": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "latestExecutorTerminationReason": (
                "EXECUTOR_TERMINATION_REASON_USER_CANCELED"
            ),
            "trajectoryMetadata": {"projectId": "p1"},
        },
        "c-ask-parallel-running": {
            "status": 2,
            "notFullyIdle": True,
            "summary": "[14:00] ⏳ Blocked Behind Timer Marker",
            "trajectoryMetadata": {"projectId": "p1"},
        },
        "c-elicitation-v2": {
            "status": 1,
            "trajectoryMetadata": {"projectId": "p1"},
        },
        "c-toolcall-fallback": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "trajectoryMetadata": {"projectId": "p1"},
        },
    }
    trajectories = {
        "c-perm-idle": {
            "trajectory": {
                "steps": [
                    {"type": "CORTEX_STEP_TYPE_USER_INPUT", "status": 3},
                    {
                        "type": "CORTEX_STEP_TYPE_RUN_COMMAND",
                        "status": "CORTEX_STEP_STATUS_WAITING",
                        "requestedInteraction": {
                            "permission": {
                                "resource": {
                                    "action": "execute",
                                    "target": "git push origin main",
                                }
                            }
                        },
                    },
                ]
            }
        },
        "c-ask-parallel-running": {
            "trajectory": {
                "steps": [
                    {"type": 14, "status": 3},
                    {
                        "type": 138,
                        "status": 9,
                        "requestedInteraction": {
                            "interaction": {
                                "case": "askQuestion",
                                "value": {
                                    "questions": [{"question": "Commit + push?"}]
                                },
                            }
                        },
                    },
                    {"type": 132, "status": 3},
                ]
            }
        },
        "c-elicitation-v2": {
            "trajectory": {
                "steps": [
                    {"type": 14, "status": 3},
                    {
                        "type": 132,
                        "status": "CORTEX_STEP_STATUS_WAITING",
                        "requestedInteraction": {
                            "interaction": {
                                "case": "elicitation",
                                "value": {"message": "2FA code"},
                            }
                        },
                    },
                ]
            }
        },
        "c-toolcall-fallback": {
            "trajectory": {
                "steps": [
                    {"type": 14, "status": 3},
                    {
                        "type": "CORTEX_STEP_TYPE_RUN_COMMAND",
                        "status": 9,
                        "metadata": {
                            "toolCall": {
                                "name": "run_command",
                                "argumentsJson": json.dumps(
                                    {"toolSummary": "Deploy hub patch"}
                                ),
                            }
                        },
                    },
                ]
            }
        },
    }
    running, blocked, unread = badge.compute_project_activity(
        summaries, known_projects, [], trajectories=trajectories
    )
    self.assertEqual(running, {})
    self.assertEqual(blocked, {"p1": 4})
    self.assertEqual(unread, {})

    b1, d1 = badge.resolve_conversation_activity(
        "c-perm-idle", summaries["c-perm-idle"], summaries, trajectories=trajectories
    )
    self.assertEqual((b1, d1), ("blocked", "Permission required for execute: git push origin main"))

    b2, d2 = badge.resolve_conversation_activity(
        "c-ask-parallel-running",
        summaries["c-ask-parallel-running"],
        summaries,
        trajectories=trajectories,
    )
    self.assertEqual((b2, d2), ("blocked", "Awaiting answer: Commit + push?"))

    b3, d3 = badge.resolve_conversation_activity(
        "c-elicitation-v2",
        summaries["c-elicitation-v2"],
        summaries,
        trajectories=trajectories,
    )
    self.assertEqual((b3, d3), ("blocked", "Input required: 2FA code"))

    b4, d4 = badge.resolve_conversation_activity(
        "c-toolcall-fallback",
        summaries["c-toolcall-fallback"],
        summaries,
        trajectories=trajectories,
    )
    self.assertEqual((b4, d4), ("blocked", "Permission required for Deploy hub patch"))

  def test_blocked_subagent_and_battle_arm_bubble_up_ignoring_kills(self):
    known_projects = {"p1": ("/tmp/p1.json", {"id": "p1", "name": "Breaking Chats"})}
    summaries = {
        "c-parent-blocked-child": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "notFullyIdle": True,
            "hasRunningSubagents": True,
            "trajectoryMetadata": {"projectId": "p1"},
        },
        "sub-grandparent": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "notFullyIdle": True,
            "summary": "Orchestrator",
            "trajectoryMetadata": {"parentConversationId": "c-parent-blocked-child"},
        },
        "sub-grandchild": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "summary": "DB Debugger",
            "waitingSteps": [{
                "stepIndex": 4,
                "step": {
                    "type": "CORTEX_STEP_TYPE_RUN_COMMAND",
                    "requestedInteraction": {
                        "permission": {"actionDescription": "drop table"}
                    },
                },
            }],
            "trajectoryMetadata": {"parentConversationId": "sub-grandparent"},
        },
        "c-killed-child-parent": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "lastModifiedTime": "2026-10-04T00:50:00Z",
            "annotations": {"lastUserViewTime": "2026-10-04T00:55:00Z"},
            "trajectoryMetadata": {"projectId": "p1"},
        },
        "sub-killed-flag": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "killed": True,
            "waitingSteps": [{"stepIndex": 1}],
            "trajectoryMetadata": {"parentConversationId": "c-killed-child-parent"},
        },
        "sub-killed-tool": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "waitingSteps": [{"stepIndex": 2}],
            "trajectoryMetadata": {"parentConversationId": "c-killed-child-parent"},
        },
        "c-battle": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "notFullyIdle": True,
            "battleModeInfo": {
                "endType": 0,
                "childrenConversationIds": ["arm-elim", "arm-live"],
                "eliminated": [True, False],
            },
            "trajectoryMetadata": {"projectId": "p1"},
        },
        "arm-elim": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "waitingSteps": [{"stepIndex": 1}],
            "trajectoryMetadata": {"isBattleModeFork": True},
        },
        "arm-live": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "waitingSteps": [{
                "stepIndex": 2,
                "step": {
                    "type": 138,
                    "askQuestion": {"questions": [{"question": "Pick API?"}]},
                },
            }],
            "trajectoryMetadata": {"isBattleModeFork": True},
        },
    }
    trajectories = {
        "c-killed-child-parent": {
            "trajectory": {
                "steps": [
                    {"type": 14, "status": 3},
                    {
                        "type": 127,
                        "status": 3,
                        "invokeSubagent": {
                            "subagents": [{"role": "Worker"}],
                            "results": [{"conversationId": "sub-killed-tool"}],
                        },
                    },
                    {
                        "type": 15,
                        "status": 3,
                        "plannerResponse": {
                            "toolCalls": [{
                                "name": "manage_subagents",
                                "argumentsJson": json.dumps({
                                    "Action": "kill",
                                    "ConversationIds": ["sub-killed-tool"],
                                }),
                            }]
                        },
                    },
                    {"type": 15, "status": 3, "plannerResponse": {"response": "Done"}},
                ]
            }
        }
    }
    running, blocked, unread = badge.compute_project_activity(
        summaries, known_projects, [], trajectories=trajectories
    )
    self.assertEqual(running, {})
    self.assertEqual(blocked, {"p1": 2})
    self.assertEqual(unread, {})

    b_gc, d_gc = badge.resolve_conversation_activity(
        "c-parent-blocked-child",
        summaries["c-parent-blocked-child"],
        summaries,
        trajectories=trajectories,
    )
    self.assertEqual((b_gc, d_gc), ("blocked", "[DB Debugger] Permission required for drop table"))

    b_arm, d_arm = badge.resolve_conversation_activity(
        "c-battle", summaries["c-battle"], summaries, trajectories=trajectories
    )
    self.assertEqual((b_arm, d_arm), ("blocked", "[Battle arm 2] Awaiting answer: Pick API?"))

    now = badge._parse_iso_timestamp("2026-10-29T18:30:00Z")
    with self.subTest("idle_ongoing_battle_awaiting_vote"):
      battle_ready_sums = {
          "c-battle-ready": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "notFullyIdle": False,
              "lastModifiedTime": "2026-10-29T18:29:59Z",
              "annotations": {"lastUserViewTime": "2026-10-29T18:20:00Z"},
              "battleModeInfo": {
                  "endType": 0,
                  "childrenConversationIds": ["arm-r1", "arm-r2"],
              },
              "trajectoryMetadata": {"projectId": "p1"},
          },
          "arm-r1": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-29T18:29:59Z",
              "trajectoryMetadata": {"isBattleModeFork": True},
          },
          "arm-r2": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-29T18:29:59Z",
              "trajectoryMetadata": {"isBattleModeFork": True},
          },
      }
      battle_ready_trajs = {
          "c-battle-ready": {
              "trajectory": {
                  "steps": [
                      {"type": 14, "status": 3},
                      {
                          "type": 127,
                          "status": 3,
                          "invokeSubagent": {"results": [{"conversationId": "sub-done"}]},
                      },
                      {"type": 101, "status": 3, "systemMessage": {"message": "sender=sub-done"}},
                      {"type": 15, "status": 3, "plannerResponse": {"response": "Ready"}},
                  ]
              }
          }
      }
      self.assertEqual(
          badge.resolve_conversation_activity(
              "c-battle-ready",
              battle_ready_sums["c-battle-ready"],
              battle_ready_sums,
              trajectories=battle_ready_trajs,
              now_ts=now,
          ),
          ("unread", ""),
      )

    with self.subTest("prior_turn_unindexed_child_and_created_time_fallback"):
      prior_child_sums = {
          "c-multi": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "lastModifiedTime": "2026-10-29T18:29:59Z",
              "annotations": {"lastUserViewTime": "2026-10-29T18:20:00Z"},
              "trajectoryMetadata": {"projectId": "p1"},
          },
          "sub-turn1": {
              "status": "CASCADE_RUN_STATUS_IDLE",
              "createdTime": "2026-10-29T18:29:55Z",
              "trajectoryMetadata": {"parentConversationId": "c-multi"},
          },
      }
      prior_child_trajs = {
          "c-multi": {
              "trajectory": {
                  "steps": [
                      {"type": 14, "status": 3},
                      {"type": 15, "status": 3, "plannerResponse": {"response": "Turn 2 done"}},
                  ]
              }
          }
      }
      self.assertEqual(
          badge.resolve_conversation_activity(
              "c-multi",
              prior_child_sums["c-multi"],
              prior_child_sums,
              trajectories=prior_child_trajs,
              now_ts=now,
          ),
          ("unread", ""),
      )

  def test_coordinator_synthesis_required_after_subagent_reports(self):
    known_projects = {"p1": ("/tmp/p1.json", {"id": "p1", "name": "Breaking Chats"})}
    summaries = {
        "c-coord": {
            "summary": "[14:10] ⏳ Coordinator Waiting",
            "status": "CASCADE_RUN_STATUS_IDLE",
            "notFullyIdle": False,
            "lastModifiedTime": "2026-10-29T18:00:10Z",
            "annotations": {"lastUserViewTime": "2026-10-29T18:00:00Z"},
            "trajectoryMetadata": {"projectId": "p1"},
        },
        "sub-1": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "notFullyIdle": False,
            "lastModifiedTime": "2026-10-29T18:00:09Z",
            "trajectoryMetadata": {"parentConversationId": "c-coord"},
        },
    }
    steps: list[dict] = [
        {"type": "CORTEX_STEP_TYPE_USER_INPUT", "status": "CORTEX_STEP_STATUS_DONE"},
        {
            "type": "CORTEX_STEP_TYPE_INVOKE_SUBAGENT",
            "status": "CORTEX_STEP_STATUS_DONE",
            "invokeSubagent": {
                "subagents": [{"role": "Auditor"}],
                "results": [{"conversationId": "sub-1"}],
            },
        },
        {
            "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
            "status": "CORTEX_STEP_STATUS_DONE",
            "plannerResponse": {"response": "Launched Auditor in background."},
        },
    ]
    trajectories = {"c-coord": {"trajectory": {"steps": steps}}}

    b0, d0 = badge.resolve_conversation_activity(
        "c-coord", summaries["c-coord"], summaries, trajectories=trajectories
    )
    self.assertEqual((b0, d0), ("running", "Waiting for subagent reports"))

    # Subagent report arrives via IdleSubagentGuardStopHook systemMessage, still unsynthesized
    steps.append({
        "type": "CORTEX_STEP_TYPE_SYSTEM_MESSAGE",
        "status": "CORTEX_STEP_STATUS_DONE",
        "systemMessage": {"message": "Subagent sub-1 has gone idle with final output."},
    })
    # Intermediate tool-calling planner step does NOT count as final synthesis
    steps.append({
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": "CORTEX_STEP_STATUS_DONE",
        "plannerResponse": {
            "response": "Checking file...",
            "toolCalls": [{"name": "read_file", "argumentsJson": "{}"}],
        },
    })
    b1, d1 = badge.resolve_conversation_activity(
        "c-coord", summaries["c-coord"], summaries, trajectories=trajectories
    )
    self.assertEqual((b1, d1), ("running", "Synthesizing subagent findings"))

    # Coordinator produces final DONE PLANNER_RESPONSE followed by transparent CHECKPOINT
    summaries["c-coord"]["summary"] = "[14:10] ⦿ Coordinator Ready"
    steps.append({
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": "CORTEX_STEP_STATUS_DONE",
        "plannerResponse": {"response": "All findings verified and fixed."},
    })
    steps.append({"type": "CORTEX_STEP_TYPE_CHECKPOINT", "status": "CORTEX_STEP_STATUS_DONE"})
    steps.append({
        "type": "CORTEX_STEP_TYPE_SUGGESTED_RESPONSES",
        "status": "CORTEX_STEP_STATUS_DONE",
    })
    running, blocked, unread = badge.compute_project_activity(
        summaries, known_projects, [], trajectories=trajectories
    )
    self.assertEqual(running, {})
    self.assertEqual(blocked, {})
    self.assertEqual(unread, {"p1": 1})

    # If execution aborts with ERROR before synthesis, do not stay stuck in running
    steps.pop()
    steps.pop()
    steps.pop()
    summaries["c-coord"]["latestExecutorTerminationReason"] = (
        "EXECUTOR_TERMINATION_REASON_ERROR"
    )
    b_err, _ = badge.resolve_conversation_activity(
        "c-coord", summaries["c-coord"], summaries, trajectories=trajectories
    )
    self.assertEqual(b_err, "unread")

  def test_eval_cache_and_truncated_trajectory_tail_fetch(self):
    known_projects = {"p1": ("/tmp/p1.json", {"id": "p1", "name": "Breaking Chats"})}
    summaries = {
        "c-1": {
            "stepCount": 3,
            "lastModifiedTime": "2026-10-29T18:10:00Z",
            "status": "CASCADE_RUN_STATUS_IDLE",
            "trajectoryMetadata": {"projectId": "p1"},
        }
    }
    calls: list[str] = []

    def fake_fetcher(cid: str):
      calls.append(cid)
      return {
          "trajectory": {
              "steps": [
                  {"type": 14, "status": 3},
                  {
                      "type": 21,
                      "status": 9,
                      "requestedInteraction": {
                          "permission": {"actionDescription": "rm -rf /tmp/x"}
                      },
                  },
              ]
          }
      }

    eval_cache: dict = {}
    now = badge._parse_iso_timestamp("2026-10-29T18:10:05Z")
    _, blocked1, _ = badge.compute_project_activity(
        summaries,
        known_projects,
        [],
        traj_fetcher=fake_fetcher,
        eval_cache=eval_cache,
        now_ts=now,
    )
    self.assertEqual(blocked1, {"p1": 1})
    self.assertEqual(calls, ["c-1"])

    # Unchanged summary hits eval_cache with zero additional RPCs
    _, blocked2, _ = badge.compute_project_activity(
        summaries,
        known_projects,
        [],
        traj_fetcher=fake_fetcher,
        eval_cache=eval_cache,
        now_ts=now + 1.5,
    )
    self.assertEqual(blocked2, {"p1": 1})
    self.assertEqual(calls, ["c-1"])

    # Verify fetch_single_trajectory appends multi-page truncated tail steps
    responses = [
        json.dumps({
            "status": "CASCADE_RUN_STATUS_IDLE",
            "numTotalSteps": 5,
            "trajectory": {"steps": [{"type": 14, "status": 3}]},
        }).encode("utf-8"),
        json.dumps({
            "steps": [
                {"type": 15, "status": 3, "plannerResponse": {"toolCalls": [{"name": "view"}]}},
                {"type": 16, "status": 3},
            ]
        }).encode("utf-8"),
        json.dumps({
            "steps": [
                {"type": 15, "status": 3, "plannerResponse": {"response": "ok"}},
                {"type": 23, "status": 3},
            ]
        }).encode("utf-8"),
    ]

    class FakeHttpResp(io.BytesIO):
      def __enter__(self):
        return self

      def __exit__(self, *args):
        self.close()

    with self.subTest("multipage_tail_fetch"):
      with mock.patch(
          "urllib.request.urlopen",
          side_effect=[FakeHttpResp(b) for b in responses],
      ):
        merged = badge.fetch_single_trajectory("c-1", "localhost:5387", "tok")
        self.assertIsNotNone(merged)
        self.assertEqual(len(merged["trajectory"]["steps"]), 5)

    with self.subTest("eval_cache_invalidates_on_interrupted_and_snake_term"):
      int_sums = {
          "c-int": {
              "stepCount": 2,
              "lastModifiedTime": "2026-10-29T18:10:00Z",
              "status": "CASCADE_RUN_STATUS_IDLE",
              "interrupted": False,
              "annotations": {"lastUserViewTime": "2026-10-29T18:00:00Z"},
              "trajectoryMetadata": {"projectId": "p1"},
          }
      }
      int_cache: dict = {}

      def running_tail_fetcher(_cid: str):
        return {
            "trajectory": {
                "steps": [
                    {"type": 14, "status": 3},
                    {
                        "type": 15,
                        "status": 3,
                        "plannerResponse": {"toolCalls": [{"name": "read_file"}]},
                    },
                ]
            }
        }

      run_a, _, unread_a = badge.compute_project_activity(
          int_sums,
          known_projects,
          [],
          traj_fetcher=running_tail_fetcher,
          eval_cache=int_cache,
          now_ts=now,
      )
      self.assertEqual(run_a, {"p1": 1})
      self.assertEqual(unread_a, {})

      int_sums["c-int"]["interrupted"] = True
      run_b, _, unread_b = badge.compute_project_activity(
          int_sums,
          known_projects,
          [],
          traj_fetcher=running_tail_fetcher,
          eval_cache=int_cache,
          now_ts=now + 1.0,
      )
      self.assertEqual(run_b, {})
      self.assertEqual(unread_b, {"p1": 1})


if __name__ == "__main__":
  unittest.main()
