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

  # A chat of any project may write its own chain state file even though the
  # roadmaps dir sits inside another project's exclusive config root
  def test_cross_project_guard_allows_own_chain_state_file_in_foreign_config_root(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      pdir = os.path.join(tmp_dir, "projects")
      cfg = os.path.join(tmp_dir, "config")
      rdir = os.path.join(cfg, "roadmaps")
      artifact_dir = os.path.join(tmp_dir, "brain", "c1")
      os.makedirs(pdir)
      os.makedirs(os.path.join(artifact_dir, "scratch"))
      os.makedirs(os.path.join(rdir, "proj-a", "state"))
      os.makedirs(os.path.join(rdir, "proj-b", "state"))
      with open(os.path.join(pdir, "proj-a.json"), "w") as f:
        json.dump({"id": "proj-a", "name": "Chrome AI Week H2 2026"}, f)
      with open(os.path.join(pdir, "proj-b.json"), "w") as f:
        json.dump({
            "id": "proj-b",
            "name": "Improving Experience",
            "projectResources": {"resources": [{"folderUri": f"file://{cfg}"}]},
        }, f)
      transcript = os.path.join(artifact_dir, "transcript.jsonl")
      with open(transcript, "w") as f:
        f.write(json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "x"}) + "\n")

      import context_guard
      with patch.dict(os.environ, {"JETSKI_PROJECTS_DIR": pdir, "JETSKI_ROADMAPS_DIR": rdir}), \
           patch.object(context_guard, "get_conversation_db_info",
                        return_value=("[11:00] ⦿ AI Week", "proj-a")), \
           patch.object(context_guard, "summary_db_paths", return_value=[]):
        def decide(path):
          return self.run_guard({
              "conversationId": "c1",
              "transcriptPath": transcript,
              "artifactDirectoryPath": artifact_dir,
              "toolCall": {"name": "write_file", "args": {"Path": path}},
          })

        self.assertEqual(
            decide(os.path.join(rdir, "proj-a", "state", "c1.md")).get("decision"), "allow"
        )
        for path in (
            os.path.join(rdir, "proj-b", "state", "c1.md"),
            os.path.join(cfg, "rules", "x.md"),
        ):
          out = decide(path)
          self.assertEqual(out.get("decision"), "deny", msg=path)
          self.assertIn("CROSS-PROJECT SCOPE GUARD", out.get("reason", ""))

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

        # Relative build dirs resolve against the preceding `cd`, not the hook's own cwd,
        # even when that cwd is the foreign root and the command runs a script from it
        prev_cwd = os.getcwd()
        os.makedirs(ws_ts, exist_ok=True)
        os.chdir(ws_ts)
        try:
          for tail in ("", f"; python3 {ws_ts}/skills/audit.py --repo . --diff HEAD~1"):
            out_rel = self.run_guard({
                "conversationId": "c1",
                "transcriptPath": transcript,
                "artifactDirectoryPath": artifact_dir,
                "workspacePaths": [ws_ts],
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": (
                        f"cd {ws_hack} && python3 - <<'EOF'\nprint(1)\nEOF\n"
                        "third_party/ninja/ninja -C out/Static dm && git add -- a.cpp && "
                        "git commit --amend --no-edit -q" + tail
                    )},
                },
            })
            self.assertNotIn("CROSS-PROJECT", out_rel.get("reason", ""))
          out_rel_cd = self.run_guard({
              "conversationId": "c1",
              "transcriptPath": transcript,
              "artifactDirectoryPath": artifact_dir,
              "toolCall": {
                  "name": "run_command",
                  "args": {"CommandLine": f"cd {ws_hack} && cd ../webparagraph-ts && git commit -m x"},
              },
          })
          self.assertEqual(out_rel_cd.get("decision"), "deny")
        finally:
          os.chdir(prev_cwd)

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

  def test_corpus_check_guard_requires_flutter_corpus_only_for_executable_code_edits(self):
    skia_src = "/workspace/skia/modules/skparagraph/src/ParagraphImpl.cpp"
    wp_src = "/workspace/flutter/engine/src/flutter/lib/web_ui/lib/src/engine/text_fragmenter.dart"

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans:
      # 1. Comment-only edit in SkParagraph + Phase 1 audits -> git cl upload & git push allowed without corpus
      steps = [
          {
              "step_index": 1,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "edit_file",
                  "args": {
                      "Path": skia_src,
                      "Target": "// Old comment\nfRuns.reset();",
                      "Replacement": "// Updated timeless comment explaining why\nfRuns.reset();",
                  },
              }],
          },
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "python3 audit_comments.py --diff HEAD --repo . && "
                          "python3 ruthless_audit.py . --repo . --diff HEAD"
                      )
                  },
              }],
          },
      ]
      for s in steps:
        f_trans.write(json.dumps(s) + "\n")
      f_trans.flush()

      for cmd in ("git cl upload", "git push origin main", "gh pr create --fill"):
        out = self.run_guard({
            "conversationId": "c1",
            "transcriptPath": f_trans.name,
            "toolCall": {"name": "run_command", "args": {"CommandLine": cmd}},
        })
        self.assertEqual(out.get("decision"), "allow", msg=f"Failed for {cmd}: {out}")

      # 2. Executable code edit in SkParagraph -> git commit is allowed after Phase 1 audits,
      #    but git cl upload / git push / gh pr create are blocked by CORPUS-CHECK GUARD
      f_trans.write(
          json.dumps({
              "step_index": 3,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "edit_file",
                  "args": {
                      "Path": skia_src,
                      "Target": "fRuns.reset();",
                      "Replacement": "fRuns.reset();\nfMaxIntrinsicWidth = 0;",
                  },
              }],
          })
          + "\n"
          + json.dumps({
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "python3 audit_comments.py --diff HEAD --repo . && "
                          "python3 ruthless_audit.py . --repo . --diff HEAD"
                      )
                  },
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

      for cmd in ("git cl upload", "git push origin head", "gh pr create --fill"):
        out_deny = self.run_guard({
            "conversationId": "c1",
            "transcriptPath": f_trans.name,
            "toolCall": {"name": "run_command", "args": {"CommandLine": cmd}},
        })
        self.assertEqual(out_deny.get("decision"), "deny")
        self.assertIn("CORPUS-CHECK GUARD", out_deny.get("reason", ""))
        self.assertIn("run_flutter_paragraph_tests.py", out_deny.get("reason", ""))

      # 3. Running run_flutter_paragraph_tests.py unlocks git cl upload / git push
      f_trans.write(
          json.dumps({
              "step_index": 5,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": "python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py --out /tmp/out"
                  },
              }],
          })
          + "\n"
      )
      f_trans.flush()
      self.assertEqual(
          self.run_guard({
              "conversationId": "c1",
              "transcriptPath": f_trans.name,
              "toolCall": {"name": "run_command", "args": {"CommandLine": "git cl upload"}},
          }).get("decision"),
          "allow",
      )

      # 4. Subsequent comment-only edit after corpus run only requires Phase 1 audits, NOT re-running corpus
      f_trans.write(
          json.dumps({
              "step_index": 6,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "edit_file",
                  "args": {
                      "Path": skia_src,
                      "Target": "// Updated timeless comment explaining why",
                      "Replacement": "// Polished comment before upload",
                  },
              }],
          })
          + "\n"
          + json.dumps({
              "step_index": 7,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "python3 audit_comments.py --diff HEAD --repo . && "
                          "python3 ruthless_audit.py . --repo . --diff HEAD"
                      )
                  },
              }],
          })
          + "\n"
      )
      f_trans.flush()
      self.assertEqual(
          self.run_guard({
              "conversationId": "c1",
              "transcriptPath": f_trans.name,
              "toolCall": {"name": "run_command", "args": {"CommandLine": "git cl upload"}},
          }).get("decision"),
          "allow",
      )

      # 5. Executable code edit in WebParagraph requires run_webparagraph_corpus.py before git push / gh pr create
      f_trans.write(
          json.dumps({
              "step_index": 8,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "edit_file",
                  "args": {
                      "Path": wp_src,
                      "Target": "return start;",
                      "Replacement": "return start + 1;",
                  },
              }],
          })
          + "\n"
          + json.dumps({
              "step_index": 9,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "python3 audit_comments.py --diff HEAD --repo . && "
                          "python3 ruthless_audit.py . --repo . --diff HEAD"
                      )
                  },
              }],
          })
          + "\n"
      )
      f_trans.flush()
      out_wp = self.run_guard({
          "conversationId": "c1",
          "transcriptPath": f_trans.name,
          "toolCall": {"name": "run_command", "args": {"CommandLine": "git push origin my-branch"}},
      })
      self.assertEqual(out_wp.get("decision"), "deny")
      self.assertIn("CORPUS-CHECK GUARD", out_wp.get("reason", ""))
      self.assertIn("run_webparagraph_corpus.py", out_wp.get("reason", ""))

      f_trans.write(
          json.dumps({
              "step_index": 10,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": "python3 ~/.gemini/config/skills/webparagraph-corpus/scripts/run_webparagraph_corpus.py --out /tmp/wp"
                  },
              }],
          })
          + "\n"
      )
      f_trans.flush()
      self.assertEqual(
          self.run_guard({
              "conversationId": "c1",
              "transcriptPath": f_trans.name,
              "toolCall": {"name": "run_command", "args": {"CommandLine": "git push origin my-branch"}},
          }).get("decision"),
          "allow",
      )

  def test_compound_command_inspection_prefix_does_not_bypass_mutation_guards(self):
    import pre_tool_guard
    self.assertEqual(
        pre_tool_guard._non_inspection_cmd_segments("grep -rn foo . && echo ok"),
        "",
    )
    self.assertEqual(
        pre_tool_guard._non_inspection_cmd_segments("grep -rn 'git commit' . && git commit -m 'x'"),
        "git commit -m 'x'",
    )
    self.assertTrue(
        pre_tool_guard.is_git_commit_or_upload_command(
            "grep -rn 'git commit' . && git commit -m 'x'"
        )
    )
    self.assertTrue(
        pre_tool_guard.is_git_upload_or_push_command(
            "echo checking && git push origin main"
        )
    )

  # Verify per-worktree lease blocks a second active chat on the same worktree
  def test_worktree_lease_guard_blocks_concurrent_chat_on_same_code_worktree(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      repo = os.path.join(tmp_dir, "flutter_perf")
      wt2 = os.path.join(tmp_dir, "flutter_wt2")
      subprocess.run(["git", "init", "-b", "main", repo], check=True, capture_output=True)
      subprocess.run(["git", "-C", repo, "config", "user.email", "t@example.com"], check=True)
      subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)
      with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("init\n")
      subprocess.run(["git", "-C", repo, "add", "README.md"], check=True)
      subprocess.run(["git", "-C", repo, "commit", "-m", "init"], check=True, capture_output=True)
      subprocess.run(
          ["git", "-C", repo, "worktree", "add", "-b", "wt2-branch", wt2],
          check=True,
          capture_output=True,
      )

      import context_guard
      titles = {
          "c1": ("[11:04] ⦿ R16 C++ perf loop", "proj-1"),
          "c2": ("[20:00] ⦿ PR #195122 WebParagraph", "proj-1"),
      }
      with patch.object(
          context_guard,
          "get_conversation_db_info",
          side_effect=lambda cid, _h="": titles.get(cid, ("", "proj-1")),
      ):
        # Chat c1 claims `repo` via ninja build command
        self.assertEqual(
            self.run_guard({
                "conversationId": "c1",
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": f"ninja -C {repo}/out/Release"},
                },
            }).get("decision"),
            "allow",
        )

        # Chat c2 attempting to build or edit in `repo` is blocked by WORKTREE LEASE GUARD
        out_build = self.run_guard({
            "conversationId": "c2",
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": f"cd {repo} && felt test"},
            },
        })
        self.assertEqual(out_build.get("decision"), "deny")
        self.assertIn("WORKTREE LEASE GUARD", out_build.get("reason", ""))
        self.assertIn("conversation://c1", out_build.get("reason", ""))

        out_edit = self.run_guard({
            "conversationId": "c2",
            "toolCall": {
                "name": "write_file",
                "args": {"Path": os.path.join(repo, "notes.md")},
            },
        })
        self.assertEqual(out_edit.get("decision"), "deny")
        self.assertIn("WORKTREE LEASE GUARD", out_edit.get("reason", ""))

        # Chat c2 can freely work inside its separate linked worktree `wt2`
        self.assertEqual(
            self.run_guard({
                "conversationId": "c2",
                "toolCall": {
                    "name": "write_file",
                    "args": {"Path": os.path.join(wt2, "notes.md")},
                },
            }).get("decision"),
            "allow",
        )

        # Even if jetski_lease.json is older than 30m, a fresh background task log
        # or subagent transcript for c1 keeps the lease alive
        import time

        git_dir = os.path.join(repo, ".git")
        with open(os.path.join(git_dir, "jetski_lease.json"), "w") as f:
          json.dump(
              {
                  "conv_id": "c1",
                  "title": titles["c1"][0],
                  "ts": time.time() - 3600,
                  "worktree": repo,
              },
              f,
          )
        c1_tasks = os.path.join(tmp_dir, "brain", "c1", ".system_generated", "tasks")
        c2_logs = os.path.join(tmp_dir, "brain", "c2", ".system_generated", "logs")
        os.makedirs(c1_tasks, exist_ok=True)
        os.makedirs(c2_logs, exist_ok=True)
        with open(os.path.join(c1_tasks, "task-99.log"), "w") as f:
          f.write("running long test suite...\n")
        trans_c2 = os.path.join(c2_logs, "transcript.jsonl")
        with open(trans_c2, "w") as f:
          f.write("{}\n")

        out_still_running = self.run_guard({
            "conversationId": "c2",
            "transcriptPath": trans_c2,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": f"cd {repo} && felt test"},
            },
        })
        self.assertEqual(out_still_running.get("decision"), "deny")
        self.assertIn("WORKTREE LEASE GUARD", out_still_running.get("reason", ""))

        # Once c1 is archived (finished or handed off), c2 takes over `repo` without an override
        with patch.object(
            context_guard, "_is_archived_in_pbtxt", side_effect=lambda cid, _h="": cid == "c1"
        ):
          self.assertEqual(
              self.run_guard({
                  "conversationId": "c2",
                  "transcriptPath": trans_c2,
                  "toolCall": {
                      "name": "run_command",
                      "args": {"CommandLine": f"cd {repo} && felt test"},
                  },
              }).get("decision"),
              "allow",
          )

  # Verify shared-config repo allows editing distinct files but blocks colliding edits
  def test_shared_config_repo_per_file_lease_and_commit_guard(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      repo = os.path.join(tmp_dir, "shared_cfg")
      subprocess.run(["git", "init", "-b", "main", repo], check=True, capture_output=True)
      subprocess.run(["git", "-C", repo, "config", "user.email", "t@example.com"], check=True)
      subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)
      file_a = os.path.join(repo, "skill_a.md")
      file_b = os.path.join(repo, "skill_b.md")
      with open(file_a, "w") as f:
        f.write("a1\n")
      with open(file_b, "w") as f:
        f.write("b1\n")
      subprocess.run(["git", "-C", repo, "add", "skill_a.md", "skill_b.md"], check=True)
      subprocess.run(["git", "-C", repo, "commit", "-m", "init"], check=True, capture_output=True)

      trans_c1 = os.path.join(tmp_dir, "c1.jsonl")
      trans_c2 = os.path.join(tmp_dir, "c2.jsonl")
      with open(trans_c1, "w") as f:
        f.write(
            json.dumps({
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"name": "write_file", "args": {"Path": file_a}}],
            })
            + "\n"
        )
      with open(trans_c2, "w") as f:
        f.write(
            json.dumps({
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"name": "write_file", "args": {"Path": file_b}}],
            })
            + "\n"
        )

      import context_guard
      titles = {
          "c1": ("[10:00] ⦿ Skill A", "proj-1"),
          "c2": ("[10:05] ⦿ Skill B", "proj-1"),
      }
      with patch.dict(os.environ, {"JETSKI_SHARED_CONFIG_REPO": repo}), \
           patch.object(
               context_guard,
               "get_conversation_db_info",
               side_effect=lambda cid, _h="": titles.get(cid, ("", "proj-1")),
           ):
        # c1 edits skill_a.md and c2 edits skill_b.md in the same tree -> both allowed
        self.assertEqual(
            self.run_guard({
                "conversationId": "c1",
                "transcriptPath": trans_c1,
                "toolCall": {"name": "write_file", "args": {"Path": file_a}},
            }).get("decision"),
            "allow",
        )
        self.assertEqual(
            self.run_guard({
                "conversationId": "c2",
                "transcriptPath": trans_c2,
                "toolCall": {"name": "write_file", "args": {"Path": file_b}},
            }).get("decision"),
            "allow",
        )

        # c2 cannot overwrite c1's leased skill_a.md
        out_collide = self.run_guard({
            "conversationId": "c2",
            "transcriptPath": trans_c2,
            "toolCall": {"name": "write_file", "args": {"Path": file_a}},
        })
        self.assertEqual(out_collide.get("decision"), "deny")
        self.assertIn("SHARED-FILE LEASE GUARD", out_collide.get("reason", ""))

        # Modify both files on disk so git status shows both dirty
        with open(file_a, "w") as f:
          f.write("a2\n")
        with open(file_b, "w") as f:
          f.write("b2\n")

        # c2 running `git add .` or `git commit -a` is blocked because skill_a.md is dirty
        for bad_cmd in (
            f"git -C {repo} add .",
            f"git -C {repo} add -A",
            f"git -C {repo} commit -a -m 'sweep'",
        ):
          out_broad = self.run_guard({
              "conversationId": "c2",
              "transcriptPath": trans_c2,
              "toolCall": {"name": "run_command", "args": {"CommandLine": bad_cmd}},
          })
          self.assertEqual(out_broad.get("decision"), "deny", msg=bad_cmd)
          self.assertIn("SHARED-WORKTREE COMMIT GUARD", out_broad.get("reason", ""))
          self.assertIn("skill_a.md", out_broad.get("reason", ""))

        # If c1 stages skill_a.md in .git/index, c2 cannot run pathspec-less `git commit -m ...`
        subprocess.run(["git", "-C", repo, "add", "skill_a.md"], check=True)
        out_index = self.run_guard({
            "conversationId": "c2",
            "transcriptPath": trans_c2,
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": (
                        f"git -C {repo} add skill_b.md && git -C {repo} commit -m 'b'"
                    )
                },
            },
        })
        self.assertEqual(out_index.get("decision"), "deny")
        self.assertIn("SHARED-WORKTREE COMMIT GUARD", out_index.get("reason", ""))

        # Explicit pathspec `git commit -m 'b' -- skill_b.md` is allowed
        out_explicit = self.run_guard({
            "conversationId": "c2",
            "transcriptPath": trans_c2,
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": (
                        f"git -C {repo} add skill_b.md && "
                        f"git -C {repo} commit -m 'b' -- skill_b.md"
                    )
                },
            },
        })
        self.assertEqual(out_explicit.get("decision"), "allow")

  # Verify foreign-commit push guard blocks pushing another chat's unpushed commit
  def test_foreign_commit_push_guard_blocks_unpushed_sibling_commit(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      origin = os.path.join(tmp_dir, "origin.git")
      repo = os.path.join(tmp_dir, "shared_cfg")
      subprocess.run(
          ["git", "init", "--bare", "-b", "main", origin],
          check=True,
          capture_output=True,
      )
      subprocess.run(["git", "init", "-b", "main", repo], check=True, capture_output=True)
      subprocess.run(["git", "-C", repo, "config", "user.email", "t@example.com"], check=True)
      subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)
      subprocess.run(["git", "-C", repo, "remote", "add", "origin", origin], check=True)

      file_a = os.path.join(repo, "a.md")
      file_b = os.path.join(repo, "b.md")
      with open(file_a, "w") as f:
        f.write("init\n")
      with open(file_b, "w") as f:
        f.write("init\n")
      subprocess.run(["git", "-C", repo, "add", "a.md", "b.md"], check=True)
      subprocess.run(["git", "-C", repo, "commit", "-m", "init"], check=True, capture_output=True)
      subprocess.run(
          ["git", "-C", repo, "push", "-u", "origin", "main"],
          check=True,
          capture_output=True,
      )

      trans_c1 = os.path.join(tmp_dir, "c1.jsonl")
      trans_c2 = os.path.join(tmp_dir, "c2.jsonl")
      art_c2 = os.path.join(tmp_dir, "brain", "c2")
      os.makedirs(os.path.join(art_c2, "scratch"))
      with open(trans_c1, "w") as f:
        f.write(
            json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "commit a"})
            + "\n"
        )
      with open(trans_c2, "w") as f:
        f.write(
            json.dumps({
                "step_index": 1,
                "type": "USER_INPUT",
                "content": "commit and push b",
            })
            + "\n"
        )

      import context_guard
      titles = {
          "c1": ("[11:04] ⦿ R16 perf skill", "proj-1"),
          "c2": ("[10:08] ⦿ Skills inventory", "proj-1"),
      }
      with patch.dict(os.environ, {"JETSKI_SHARED_CONFIG_REPO": repo}), \
           patch.object(
               context_guard,
               "get_conversation_db_info",
               side_effect=lambda cid, _h="": titles.get(cid, ("", "proj-1")),
           ):
        # c1 edits and commits a.md locally without pushing
        self.assertEqual(
            self.run_guard({
                "conversationId": "c1",
                "transcriptPath": trans_c1,
                "toolCall": {"name": "write_file", "args": {"Path": file_a}},
            }).get("decision"),
            "allow",
        )
        with open(file_a, "w") as f:
          f.write("c1 change\n")
        self.assertEqual(
            self.run_guard({
                "conversationId": "c1",
                "transcriptPath": trans_c1,
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": f"git -C {repo} commit -m 'Add perf skill' -- a.md"},
                },
            }).get("decision"),
            "allow",
        )
        subprocess.run(
            ["git", "-C", repo, "commit", "-m", "Add perf skill", "--", "a.md"],
            check=True,
            capture_output=True,
        )

        # c2 attempts `git push origin main` -> denied because c1's commit is on main..origin/main
        out_push = self.run_guard({
            "conversationId": "c2",
            "transcriptPath": trans_c2,
            "artifactDirectoryPath": art_c2,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": f"git -C {repo} push origin main"},
            },
        })
        self.assertEqual(out_push.get("decision"), "deny")
        self.assertIn("FOREIGN-COMMIT PUSH GUARD", out_push.get("reason", ""))
        self.assertIn("Add perf skill", out_push.get("reason", ""))
        self.assertIn("conversation://c1", out_push.get("reason", ""))
        # c1 receives a turn-start GIT SYNC NOTICE once its commit lands on origin/main
        subprocess.run(
            ["git", "-C", repo, "push", "origin", "main"],
            check=True,
            capture_output=True,
        )
        notice = context_guard.build_git_sync_notice("c1", [repo])
        self.assertIn("GIT SYNC NOTICE", notice)
        self.assertIn("Add perf skill", notice)
        # Second call does not repeat the already-notified commit
        self.assertEqual(context_guard.build_git_sync_notice("c1", [repo]), "")

  # Verify runaway source-file reading sprees in the main chat are blocked until a subagent is used
  def test_exploration_circuit_breaker_blocks_runaway_source_reads(self):
    # Read targets are never opened, but they must sit outside /tmp/: Linux temp dirs
    # live there and is_agent_internal_file exempts it, while macOS uses /var/folders
    src_root = os.path.join(os.sep, "breaker_test_src")
    with tempfile.TemporaryDirectory() as tmp_dir:
      trans = os.path.join(tmp_dir, "transcript.jsonl")
      steps = [{"step_index": 0, "type": "USER_INPUT", "content": "explain bug"}]
      for i in range(15):
        steps.append({
            "step_index": i + 1,
            "type": "PLANNER_RESPONSE",
            "tool_calls": [{
                "name": "read_file",
                "args": {"Path": os.path.join(src_root, f"src_{i % 5}.cpp")},
            }],
        })
      with open(trans, "w", encoding="utf-8") as f:
        for s in steps:
          f.write(json.dumps(s) + "\n")

      # 16th source read in the main chat across 5 files is denied
      out = self.run_guard({
          "conversationId": "c1",
          "transcriptPath": trans,
          "toolCall": {
              "name": "read_file",
              "args": {"Path": os.path.join(src_root, "src_0.cpp")},
          },
      })
      self.assertEqual(out.get("decision"), "deny")
      self.assertIn("EXPLORATION CIRCUIT BREAKER", out.get("reason", ""))

      # Subagent conversations are allowed to read freely
      out_sub = self.run_guard({
          "conversationId": "sub1",
          "parentConversationId": "c1",
          "transcriptPath": trans,
          "toolCall": {
              "name": "read_file",
              "args": {"Path": os.path.join(src_root, "src_0.cpp")},
          },
      })
      self.assertEqual(out_sub.get("decision"), "allow")


if __name__ == "__main__":
  unittest.main()
