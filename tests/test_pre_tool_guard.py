#!/usr/bin/env python3
"""Tests for pre_tool_guard.py."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))

import pre_tool_guard

HOOK = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks", "pre_tool_guard.py"))


class TestPreToolGuard(unittest.TestCase):

  def test_is_tracked_source_file(self):
    self.assertTrue(pre_tool_guard.is_tracked_source_file("/workspace/project/main.py"))
    self.assertTrue(pre_tool_guard.is_tracked_source_file("/workspace/project/source.cc"))
    self.assertTrue(pre_tool_guard.is_tracked_source_file("/workspace/project/header.h"))
    self.assertTrue(pre_tool_guard.is_tracked_source_file("/workspace/project/BUILD.bazel"))
    self.assertTrue(pre_tool_guard.is_tracked_source_file("/workspace/project/CMakeLists.txt"))
    self.assertFalse(pre_tool_guard.is_tracked_source_file("/workspace/project/README.md"))
    self.assertFalse(pre_tool_guard.is_tracked_source_file("/workspace/project/notes.txt"))
    self.assertFalse(pre_tool_guard.is_tracked_source_file("/tmp/scratch.py"))
    self.assertFalse(
        pre_tool_guard.is_tracked_source_file(
            os.path.expanduser("~/.gemini/config/hooks/chat_lifecycle.py")
        )
    )

  def test_read_before_edit_guard_unseen_file(self):
    fake_project_dir = "/workspace/dummy_project"
    fake_src = os.path.join(fake_project_dir, "feature.py")
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans:
      # Empty transcript: file was never read
      f_trans.write("")
      f_trans.flush()

      data = {
          "conversationId": "c1",
          "transcriptPath": f_trans.name,
          "toolCall": {
              "name": "replace_file_content",
              "args": {"TargetFile": fake_src},
          },
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch("os.path.exists", side_effect=lambda p: p in (fake_src, f_trans.name)):
        pre_tool_guard.main()
        out = json.loads(mock_out.getvalue())
        self.assertEqual(out.get("decision"), "deny")
        self.assertIn("READ-BEFORE-EDIT", out.get("reason", ""))

  def test_read_before_edit_guard_seen_file(self):
    fake_project_dir = "/workspace/dummy_project"
    fake_src = os.path.join(fake_project_dir, "feature.py")
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans:
      view_step = {
          "step_index": 1,
          "type": "PLANNER_RESPONSE",
          "tool_calls": [
              {"name": "view_file", "args": {"AbsolutePath": fake_src}}
          ],
      }
      f_trans.write(json.dumps(view_step) + "\n")
      f_trans.flush()

      data = {
          "conversationId": "c1",
          "transcriptPath": f_trans.name,
          "toolCall": {
              "name": "replace_file_content",
              "args": {"TargetFile": fake_src},
          },
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch("os.path.exists", side_effect=lambda p: p in (fake_src, f_trans.name)):
        pre_tool_guard.main()
        out = json.loads(mock_out.getvalue())
        self.assertEqual(out.get("decision"), "allow")

  def test_agentapi_subcommand_guard(self):
    bad_commands = [
        "agentapi start 'hello'",
        "agentapi start-conversation 'hello'",
        "agentapi create 'hello'",
        "agentapi conversation 'hello'",
    ]
    for cmd in bad_commands:
      with self.subTest(cmd=cmd):
        data = {
            "conversationId": "c1",
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": cmd},
            },
        }
        with patch("sys.stdin", io.StringIO(json.dumps(data))), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
          pre_tool_guard.main()
          out = json.loads(mock_out.getvalue())
          self.assertEqual(out.get("decision"), "deny")
          self.assertIn("does not exist", out.get("reason", ""))

  def test_agentapi_multiple_new_conversation_guard(self):
    cmd = 'OUT=$(agentapi new-conversation "prompt") && agentapi new-conversation "prompt2"'
    data = {
        "conversationId": "c1",
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": cmd},
        },
    }
    with patch("sys.stdin", io.StringIO(json.dumps(data))), \
         patch("sys.stdout", new_callable=io.StringIO) as mock_out:
      pre_tool_guard.main()
      out = json.loads(mock_out.getvalue())
      self.assertEqual(out.get("decision"), "deny")
      self.assertIn("multiple times", out.get("reason", ""))

  def test_agentapi_auto_rewrite(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      summary_file = os.path.join(tmp_dir, "handoff_summary_123.md")
      with open(summary_file, "w") as f:
        f.write("# Summary\n")

      cmd = f'agentapi new-conversation "Read {summary_file}"'
      data = {
          "conversationId": "c1",
          "artifactDirectoryPath": tmp_dir,
          "toolCall": {
              "name": "run_command",
              "args": {"CommandLine": cmd},
          },
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out:
        pre_tool_guard.main()
        out = json.loads(mock_out.getvalue())
        self.assertEqual(out.get("decision"), "allow")
        self.assertIn("overwrite", out)
        rewritten = out["overwrite"]["CommandLine"]
        self.assertIn("env -u ANTIGRAVITY_SOURCE_METADATA", rewritten)
        self.assertIn("--title=", rewritten)
        self.assertIn("--model=pro", rewritten)

  def run_guard(self, data: dict) -> dict:
    with patch("sys.stdin", io.StringIO(json.dumps(data))), \
         patch("sys.stdout", new_callable=io.StringIO) as mock_out:
      pre_tool_guard.main()
    return json.loads(mock_out.getvalue())

  def test_internal_error_fails_open_with_valid_json(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      marker = os.path.join(tmp_dir, "marker.json")
      with patch.object(pre_tool_guard, "roadmap_edit_denial", side_effect=OSError("boom")) as guard, \
           patch.dict(os.environ, {"JETSKI_PRE_TOOL_GUARD_MARKER": marker}), \
           patch("sys.stdin", io.StringIO(json.dumps({"toolCall": {"name": "write_to_file", "args": {}}}))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch("sys.stderr", new_callable=io.StringIO) as mock_err:
        pre_tool_guard._guarded_main()
      guard.assert_called_once()
      self.assertEqual(json.loads(mock_out.getvalue()), {"decision": "allow"})
      self.assertIn("OSError: boom", mock_err.getvalue())
      with open(marker) as f:
        self.assertEqual(json.load(f)["error"], "OSError: boom")

  def test_unwritable_marker_still_fails_open(self):
    with patch.object(pre_tool_guard, "roadmap_edit_denial", side_effect=OSError("boom")), \
         patch.dict(os.environ, {"JETSKI_PRE_TOOL_GUARD_MARKER": "/dev/null/logs/marker.json"}), \
         patch("sys.stdin", io.StringIO(json.dumps({"toolCall": {"name": "write_to_file", "args": {}}}))), \
         patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
         patch("sys.stderr", new_callable=io.StringIO) as mock_err:
      pre_tool_guard._guarded_main()
    self.assertEqual(json.loads(mock_out.getvalue()), {"decision": "allow"})
    self.assertIn("OSError: boom", mock_err.getvalue())

  def test_entry_point_runs_the_guards(self):
    # Runs the file the way hooks.json does (path + shebang, cwd = config root):
    # tests that call main() cannot see a dead entry point, which fails open and
    # still prints a valid allow
    conv = f"smoke-{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory() as tmp_dir:
      marker = os.path.join(tmp_dir, "marker.json")
      cases = [
          (os.path.join(tmp_dir, "p1", "roadmap.json"), "deny"),
          (os.path.join(tmp_dir, "p1", "state", f"{conv}.md"), "allow"),
      ]
      for target, expected in cases:
        with self.subTest(expected=expected):
          proc = subprocess.run(
              [HOOK],
              input=json.dumps({
                  "conversationId": conv,
                  "toolCall": {"name": "write_to_file", "args": {"TargetFile": target}},
              }),
              capture_output=True,
              text=True,
              timeout=10,
              cwd=os.path.dirname(os.path.dirname(HOOK)),
              env={**os.environ, "JETSKI_ROADMAPS_DIR": tmp_dir, "JETSKI_PRE_TOOL_GUARD_MARKER": marker},
          )
          self.assertEqual((proc.returncode, proc.stderr), (0, ""))
          self.assertEqual(json.loads(proc.stdout).get("decision"), expected)
      self.assertFalse(os.path.exists(marker))

  def test_roadmap_files_are_edited_only_via_cli(self):
    with tempfile.TemporaryDirectory() as tmp_dir, \
         patch.dict(os.environ, {"JETSKI_ROADMAPS_DIR": tmp_dir}):
      cases = [
          ("c1", "", "p1/roadmap.json", "deny"),
          ("c1", "", "p1/journal.jsonl", "deny"),
          ("c1", "", "p1/state/c2.md", "deny"),
          ("c1", "", "p1/state/../roadmap.json", "deny"),
          ("sub", "c1", "p1/state/c1.md", "deny"),
          ("c1", "", "p1/state/c1.md", "allow"),
      ]
      for conv, parent, rel, expected in cases:
        for tool in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
          with self.subTest(conv=conv, rel=rel, tool=tool):
            out = self.run_guard({
                "conversationId": conv,
                "parentConversationId": parent,
                "toolCall": {"name": tool, "args": {"TargetFile": os.path.join(tmp_dir, rel)}},
            })
            self.assertEqual(out.get("decision"), expected)
            if expected == "deny":
              self.assertIn("roadmap.py", out.get("reason", ""))

  def test_in_project_handoff_requires_and_refreshes_state_file(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      state_file = os.path.join(tmp_dir, "roadmaps", "p1", "state", "c1.md")
      artifact_dir = os.path.join(tmp_dir, "brain", "c1")
      pre_tool_guard_state = os.path.join(artifact_dir, "scratch", ".context_guard_state.json")
      os.makedirs(os.path.dirname(pre_tool_guard_state))
      with open(pre_tool_guard_state, "w") as f:
        json.dump({
            "pending_handoff_launch": True,
            "pending_in_project": True,
            "pending_handoff_file": state_file,
        }, f)
      data = {
          "conversationId": "c1",
          "artifactDirectoryPath": artifact_dir,
          "toolCall": {
              "name": "run_command",
              "args": {"CommandLine": 'python3 ~/.gemini/config/hooks/chat_lifecycle.py handoff c1 --next "go"'},
          },
      }
      out = self.run_guard(data)
      self.assertEqual(out.get("decision"), "deny")
      self.assertIn(state_file, out["reason"])

      os.makedirs(os.path.dirname(state_file))
      with open(state_file, "w") as f:
        f.write("# State\nnext: go\n")
      transcript = os.path.join(artifact_dir, ".system_generated", "logs", "transcript.jsonl")
      os.makedirs(os.path.dirname(transcript), exist_ok=True)
      with open(transcript, "w") as f:
        for i, step in enumerate((
            {"type": "PLANNER_RESPONSE", "content": "Option 1 or Option 2?"},
            {
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>\nLet us use Option 2.\n</USER_REQUEST>",
            },
        )):
          f.write(json.dumps({"step_index": i, **step}) + "\n")
      data["transcriptPath"] = transcript
      for _ in range(2):
        self.assertEqual(self.run_guard(data).get("decision"), "allow")
      with open(state_file) as f:
        content = f.read()
      self.assertTrue(content.startswith("# State\nnext: go\n"))
      self.assertEqual(content.count(pre_tool_guard.AUTO_SNAPSHOT_HEADER), 1)
      self.assertIn("Option 1 or Option 2?", content)
      self.assertIn("Let us use Option 2.", content)
      self.assertNotIn("<USER_REQUEST>", content)
      self.assertFalse(os.path.exists(os.path.join(artifact_dir, "handoff_summary.md")))

  def test_repeated_handoff_is_denied_once_state_records_the_continuation(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      artifact_dir = os.path.join(tmp_dir, "brain", "c1")
      transcript = os.path.join(artifact_dir, ".system_generated", "logs", "transcript.jsonl")
      os.makedirs(os.path.dirname(transcript))
      with open(transcript, "w") as f:
        for i, step in enumerate((
            {"type": "USER_INPUT", "content": "go"},
            {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {
                "CommandLine": "python3 ~/.gemini/config/hooks/chat_lifecycle.py handoff c1 --next 'x'"}}]},
            {"type": "GENERIC", "status": "DONE",
             "content": "The command exited with code 0.\nOutput:\n<truncated 53 lines>\n▸ R18 tail"},
        )):
          f.write(json.dumps({"step_index": i, **step}) + "\n")
      os.makedirs(os.path.join(artifact_dir, "scratch"))
      with open(os.path.join(artifact_dir, "scratch", ".context_guard_state.json"), "w") as f:
        json.dump({"pending_handoff_launch": False, "handoff_completed": True, "handed_off_to": "child-9"}, f)
      data = {
          "conversationId": "c1",
          "artifactDirectoryPath": artifact_dir,
          "transcriptPath": transcript,
          "toolCall": {
              "name": "run_command",
              "args": {"CommandLine": 'python3 ~/.gemini/config/hooks/chat_lifecycle.py handoff c1 --next "go"'},
          },
      }
      out = self.run_guard(data)
      self.assertEqual(out.get("decision"), "deny")
      self.assertIn("child-9", out["reason"])
      data["toolCall"] = {"name": "write_to_file", "args": {"TargetFile": os.path.join(tmp_dir, "state.md")}}
      out = self.run_guard(data)
      self.assertEqual(out.get("decision"), "deny")
      self.assertIn("child-9", out["reason"])

  def test_batch_workflow_requires_advanced_verify(self):
    unverified_script = (
        'res = await parallel(\n'
        '    items, lambda x: agent(f"Port {x}", {"type": "string"})\n'
        ')\n'
        "print(res)\n"
    )
    out = self.run_guard({
        "conversationId": "c1",
        "toolCall": {
            "name": "run_workflow",
            "args": {"Script": unverified_script},
        },
    })
    self.assertEqual(out.get("decision"), "deny")
    self.assertIn("BATCH-VERIFY GUARD", out.get("reason", ""))

    verified_script = (
        "from advanced import parallel, verify\n"
        'res = await parallel(\n'
        '    items, lambda x: agent(f"Port {x}", {"type": "string"})\n'
        ')\n'
        'verdict = await verify(res, "All items ported and tested")\n'
    )
    out_ok = self.run_guard({
        "conversationId": "c1",
        "toolCall": {
            "name": "run_workflow",
            "args": {"Script": verified_script},
        },
    })
    self.assertEqual(out_ok.get("decision"), "allow")

    opt_out_script = "# no-verify\n" + unverified_script
    out_opt = self.run_guard({
        "conversationId": "c1",
        "toolCall": {
            "name": "run_workflow",
            "args": {"Script": opt_out_script},
        },
    })
    self.assertEqual(out_opt.get("decision"), "allow")

    tool_only_script = (
        'res = await parallel(\n'
        '    files, lambda f: call_tool("read_file", Path=f)\n'
        ')\n'
    )
    out_tool = self.run_guard({
        "conversationId": "c1",
        "toolCall": {
            "name": "run_workflow",
            "args": {"Script": tool_only_script},
        },
    })
    self.assertEqual(out_tool.get("decision"), "allow")

    # Prompt string literals mentioning `parallel(` or `verify(` must not
    # trigger false positives or mask missing `verify()` calls
    single_agent_mentioning_parallel = (
        'res = await agent("Check parallel(items, fn) usage", {"type": "string"})\n'
    )
    self.assertEqual(
        self.run_guard({
            "conversationId": "c1",
            "toolCall": {
                "name": "run_workflow",
                "args": {"Script": single_agent_mentioning_parallel},
            },
        }).get("decision"),
        "allow",
    )

    batch_mentioning_verify_in_prompt = (
        'res = await parallel(\n'
        '    items, lambda x: agent("Run verify(x) #1", {"type": "string"})\n'
        ')\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py") as f_script:
      f_script.write(batch_mentioning_verify_in_prompt)
      f_script.flush()
      out_path = self.run_guard({
          "conversationId": "c1",
          "toolCall": {
              "name": "run_workflow",
              "args": {"ScriptPath": f_script.name},
          },
      })
      self.assertEqual(out_path.get("decision"), "deny")

  def test_jetski_web_tool_names_read_before_edit(self):
    fake_src = "/workspace/dummy_project/Paragraph.cpp"
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans:
      f_trans.write("")
      f_trans.flush()
      data = {
          "conversationId": "c1",
          "transcriptPath": f_trans.name,
          "toolCall": {
              "name": "edit_file",
              "args": {"Path": fake_src},
          },
      }
      with patch("os.path.exists", side_effect=lambda p: p in (fake_src, f_trans.name)):
        out = self.run_guard(data)
        self.assertEqual(out.get("decision"), "deny")
        self.assertIn("READ-BEFORE-EDIT", out.get("reason", ""))

      read_step = {
          "step_index": 1,
          "type": "PLANNER_RESPONSE",
          "tool_calls": [{"name": "read_file", "args": {"Path": fake_src}}],
      }
      f_trans.write(json.dumps(read_step) + "\n")
      f_trans.flush()
      with patch("os.path.exists", side_effect=lambda p: p in (fake_src, f_trans.name)):
        out_ok = self.run_guard(data)
        self.assertEqual(out_ok.get("decision"), "allow")

  def test_code_check_guard_requires_precommit_audits_after_source_edit(self):
    fake_src = "/workspace/skia/modules/skparagraph/src/ParagraphImpl.cpp"
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans:
      steps = [
          {
              "step_index": 1,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{"name": "edit_file", "args": {"Path": fake_src}}],
          },
      ]
      for s in steps:
        f_trans.write(json.dumps(s) + "\n")
      f_trans.flush()

      for cmd in (
          "git commit -m 'fix'",
          "git -c commit.gpgsign=false commit -m 'fix'",
          "git cl upload",
          "gh pr create --fill",
      ):
        out = self.run_guard({
            "conversationId": "c1",
            "transcriptPath": f_trans.name,
            "toolCall": {"name": "run_command", "args": {"CommandLine": cmd}},
        })
        self.assertEqual(out.get("decision"), "deny")
        self.assertIn("CODE-CHECK GUARD", out.get("reason", ""))

      # Inspection commands mentioning the script names do not unlock commit
      f_trans.write(
          json.dumps({
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {"CommandLine": "git grep audit_comments.py && ls ruthless_audit.py"},
              }],
          })
          + "\n"
      )
      f_trans.flush()
      self.assertEqual(
          self.run_guard({
              "conversationId": "c1",
              "transcriptPath": f_trans.name,
              "toolCall": {"name": "run_command", "args": {"CommandLine": "git commit -m 'fix'"}},
          }).get("decision"),
          "deny",
      )

      # Running only audit_comments.py is not enough without ruthless_audit.py
      f_trans.write(
          json.dumps({
              "step_index": 3,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {"CommandLine": "python3 audit_comments.py --diff HEAD --repo ."},
              }],
          })
          + "\n"
      )
      f_trans.flush()
      self.assertEqual(
          self.run_guard({
              "conversationId": "c1",
              "transcriptPath": f_trans.name,
              "toolCall": {"name": "run_command", "args": {"CommandLine": "git commit -m 'fix'"}},
          }).get("decision"),
          "deny",
      )

      # Running ruthless_audit.py completes the Phase 1 check and allows commit
      f_trans.write(
          json.dumps({
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {"CommandLine": "python3 ruthless_audit.py . --repo . --diff HEAD"},
              }],
          })
          + "\n"
      )
      f_trans.flush()
      self.assertEqual(
          self.run_guard({
              "conversationId": "c1",
              "transcriptPath": f_trans.name,
              "toolCall": {"name": "run_command", "args": {"CommandLine": "git commit -m 'fix'"}},
          }).get("decision"),
          "allow",
      )

      # Subsequent source edit resets the audit state
      f_trans.write(
          json.dumps({
              "step_index": 5,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{"name": "edit_file", "args": {"Path": fake_src}}],
          })
          + "\n"
      )
      f_trans.flush()
      self.assertEqual(
          self.run_guard({
              "conversationId": "c1",
              "transcriptPath": f_trans.name,
              "toolCall": {"name": "run_command", "args": {"CommandLine": "git commit -m 'fix'"}},
          }).get("decision"),
          "deny",
      )

  def test_doc_check_guard_requires_grounding_before_update_doc(self):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans:
      f_trans.write(
          json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "Update the doc"})
          + "\n"
      )
      f_trans.flush()

      update_call = {
          "conversationId": "c1",
          "transcriptPath": f_trans.name,
          "toolCall": {
              "name": "call_mcp_tool",
              "args": {"ServerName": "google_docs", "ToolName": "update_doc", "Arguments": {}},
          },
      }
      out = self.run_guard(update_call)
      self.assertEqual(out.get("decision"), "deny")
      self.assertIn("DOC-CHECK GUARD", out.get("reason", ""))

      # Running check_doc_grounding.py unlocks update_doc across confirmation turns
      f_trans.write(
          json.dumps({
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {"CommandLine": "python3 check_doc_grounding.py draft.md"},
              }],
          })
          + "\n"
          + json.dumps({"step_index": 3, "type": "USER_INPUT", "content": "Looks good, publish it"})
          + "\n"
      )
      f_trans.flush()
      self.assertEqual(self.run_guard(update_call).get("decision"), "allow")

      # After update_doc executes and a new turn starts, a fresh grounding check is required
      f_trans.write(
          json.dumps({
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "call_mcp_tool",
                  "args": {"ServerName": "google_docs", "ToolName": "update_doc", "Arguments": {}},
              }],
          })
          + "\n"
          + json.dumps({"step_index": 5, "type": "USER_INPUT", "content": "Now edit section 4"})
          + "\n"
      )
      f_trans.flush()
      self.assertEqual(self.run_guard(update_call).get("decision"), "deny")

  # Verify cross-project scope guard blocks foreign exclusive roots until user confirms override
  def test_cross_project_scope_guard_blocks_foreign_edits_and_commits(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      pdir = os.path.join(tmp_dir, "projects")
      rdir = os.path.join(tmp_dir, "roadmaps")
      artifact_dir = os.path.join(tmp_dir, "brain", "c1")
      os.makedirs(pdir)
      os.makedirs(os.path.join(artifact_dir, "scratch"))

      ws_hack = os.path.join(tmp_dir, "hackathon_submission_2026")
      ws_ts = os.path.join(tmp_dir, "webparagraph-ts")
      ws_shared = os.path.join(tmp_dir, "skia")

      with open(os.path.join(pdir, "proj-a.json"), "w") as f:
        json.dump({
            "id": "proj-a",
            "name": "Chrome AI Week H2 2026",
            "projectResources": {"resources": [
                {"folderUri": f"file://{ws_hack}"},
                {"folderUri": f"file://{ws_shared}"},
            ]},
        }, f)
      with open(os.path.join(pdir, "proj-b.json"), "w") as f:
        json.dump({
            "id": "proj-b",
            "name": "Typescript WebParagraph",
            "projectResources": {"resources": [{"folderUri": f"file://{ws_shared}"}]},
        }, f)
      sdir_b = os.path.join(rdir, "proj-b", "state")
      os.makedirs(sdir_b)
      with open(os.path.join(sdir_b, "aa88014c.md"), "w") as f:
        f.write(f"# [12:04] ⦿ TS Paragraph Editor\nWorking directory: `{ws_ts}`\n")

      transcript = os.path.join(artifact_dir, "transcript.jsonl")
      with open(transcript, "w") as f:
        f.write(json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "Fix editor border"}) + "\n")

      import context_guard
      with patch.dict(os.environ, {"JETSKI_PROJECTS_DIR": pdir, "JETSKI_ROADMAPS_DIR": rdir}), \
           patch.object(context_guard, "get_conversation_db_info", return_value=("[11:00] ⦿ Hackathon", "proj-a")), \
           patch.object(context_guard, "summary_db_paths", return_value=[]):
        # Editing a file inside proj-b's exclusive root is denied
        out_edit = self.run_guard({
            "conversationId": "c1",
            "transcriptPath": transcript,
            "artifactDirectoryPath": artifact_dir,
            "toolCall": {
                "name": "write_file",
                "args": {"Path": os.path.join(ws_ts, "demo", "index.html")},
            },
        })
        self.assertEqual(out_edit.get("decision"), "deny")
        self.assertIn("CROSS-PROJECT SCOPE GUARD", out_edit.get("reason", ""))
        self.assertIn("Typescript WebParagraph", out_edit.get("reason", ""))
        self.assertIn("conversation://aa88014c", out_edit.get("reason", ""))

        # Committing inside proj-b's exclusive root is also denied
        out_commit = self.run_guard({
            "conversationId": "c1",
            "transcriptPath": transcript,
            "artifactDirectoryPath": artifact_dir,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": f"cd {ws_ts} && git commit -m 'fix'"},
            },
        })
        self.assertEqual(out_commit.get("decision"), "deny")
        self.assertIn("CROSS-PROJECT SCOPE GUARD", out_commit.get("reason", ""))

        # Editing own workspace or a shared multi-project repo is allowed
        self.assertEqual(
            self.run_guard({
                "conversationId": "c1",
                "transcriptPath": transcript,
                "artifactDirectoryPath": artifact_dir,
                "toolCall": {
                    "name": "write_file",
                    "args": {"Path": os.path.join(ws_hack, "one_pager.md")},
                },
            }).get("decision"),
            "allow",
        )
        self.assertEqual(
            self.run_guard({
                "conversationId": "c1",
                "transcriptPath": transcript,
                "artifactDirectoryPath": artifact_dir,
                "toolCall": {
                    "name": "write_file",
                    "args": {"Path": os.path.join(ws_shared, "README.md")},
                },
            }).get("decision"),
            "allow",
        )

        # Self-override in the same turn (before a new USER_INPUT) is blocked
        override_file = os.path.join(artifact_dir, "scratch", ".cross_project_override.json")
        out_cheat = self.run_guard({
            "conversationId": "c1",
            "transcriptPath": transcript,
            "artifactDirectoryPath": artifact_dir,
            "toolCall": {
                "name": "write_file",
                "args": {"Path": override_file},
            },
        })
        self.assertEqual(out_cheat.get("decision"), "deny")
        self.assertIn("same turn", out_cheat.get("reason", ""))

        # After user confirms in a new turn, creating the override and editing is allowed
        with open(transcript, "a") as f:
          f.write(json.dumps({"step_index": 2, "type": "USER_INPUT", "content": "Execute it here"}) + "\n")
        self.assertEqual(
            self.run_guard({
                "conversationId": "c1",
                "transcriptPath": transcript,
                "artifactDirectoryPath": artifact_dir,
                "toolCall": {
                    "name": "write_file",
                    "args": {"Path": override_file},
                },
            }).get("decision"),
            "allow",
        )
        with open(override_file, "w") as f:
          f.write("{}")
        self.assertEqual(
            self.run_guard({
                "conversationId": "c1",
                "transcriptPath": transcript,
                "artifactDirectoryPath": artifact_dir,
                "toolCall": {
                    "name": "write_file",
                    "args": {"Path": os.path.join(ws_ts, "demo", "index.html")},
                },
            }).get("decision"),
            "allow",
        )


if __name__ == "__main__":
  unittest.main()
