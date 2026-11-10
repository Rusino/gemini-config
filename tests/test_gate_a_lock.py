#!/usr/bin/env python3
"""Contract tests for R139: Gate A contract test SHA-256 hash lock and pre_tool_guard."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

CONFIG_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = str(CONFIG_ROOT / "hooks")
if HOOKS_DIR not in sys.path:
  sys.path.insert(0, HOOKS_DIR)

import gate_a_lock
import pre_tool_guard


class TestGateALock(unittest.TestCase):
  """Verifies SHA-256 Gate A test locking across CLI, subagents, handoffs, and pre_tool_guard."""

  def setUp(self) -> None:
    """Creates an isolated temp workspace, Git repo, app data dir, and ledger per test."""
    super().setUp()
    self._tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self._tmp.cleanup)
    self.root = Path(self._tmp.name)
    self.app_dir = self.root / "appdata"
    (self.app_dir / "brain").mkdir(parents=True)
    (self.app_dir / "annotations").mkdir(parents=True)
    self.ledger = self.root / "ledgers" / "gate_a_locks.json"

    self.repo = self.root / "skia"
    (self.repo / "tests").mkdir(parents=True)
    (self.repo / "src").mkdir(parents=True)
    self._git("init", "-b", "main")
    self._git("config", "user.email", "tester@example.com")
    self._git("config", "user.name", "Tester")

    self.test_file = self.repo / "tests" / "SkParagraphTest.cpp"
    self.prod_file = self.repo / "src" / "ParagraphImpl.cpp"
    self.test_body = (
        "DEF_TEST(Paragraph_GateAContract, reporter) {\n"
        "  REPORTER_ASSERT(reporter, actual == 42);\n"
        "}\n"
    )
    self.prod_body = "int computeLineWidth() { return 0; }\n"
    self.test_file.write_text(self.test_body, encoding="utf-8")
    self.prod_file.write_text(self.prod_body, encoding="utf-8")
    self._git("add", ".")
    self._git("commit", "-m", "Add Gate A failing contract test")

    env_patch = patch.dict(
        os.environ,
        {
            "GATE_A_LOCK_FILE": str(self.ledger),
            "ANTIGRAVITY_APP_DATA_DIR": str(self.app_dir),
            "JETSKI_APP_DATA_DIR": str(self.app_dir),
        },
        clear=False,
    )
    env_patch.start()
    self.addCleanup(env_patch.stop)

  def _git(self, *args: str) -> None:
    """Runs a git command inside self.repo."""
    subprocess.run(
        ["git", "-C", str(self.repo), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

  def _make_chat(
      self,
      cid: str,
      first_prompt: str = "Fix the paragraph bug",
      read_paths: tuple[Path, ...] = (),
      archived: bool = False,
  ) -> tuple[Path, Path]:
    """Creates transcript, artifact dir, and optional archive pbtxt for a mock chat."""
    art_dir = self.app_dir / "brain" / cid
    logs_dir = art_dir / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "scratch").mkdir(parents=True, exist_ok=True)
    tx_path = logs_dir / "transcript.jsonl"
    steps = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "content": first_prompt,
        }
    ]
    for idx, rp in enumerate(read_paths, start=1):
      steps.append({
          "step_index": idx,
          "type": "PLANNER_RESPONSE",
          "tool_calls": [{"name": "read_file", "args": {"Path": str(rp)}}],
      })
    tx_path.write_text(
        "\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8"
    )
    if archived:
      (self.app_dir / "annotations" / f"{cid}.pbtxt").write_text(
          "archived:true\n", encoding="utf-8"
      )
    return art_dir, tx_path

  def _run_cli(self, *argv: str, conv_id: str = "chat-root") -> tuple[int, str, str]:
    """Invokes gate_a_lock.main(argv) capturing exit code, stdout, and stderr."""
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with patch.dict(os.environ, {"CONVERSATION_ID": conv_id}, clear=False):
      with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        rc = gate_a_lock.main(list(argv))
    return rc, out_buf.getvalue(), err_buf.getvalue()

  def _run_guard(
      self,
      tool_name: str,
      tool_args: dict,
      conv_id: str = "chat-root",
      parent_conv_id: str = "",
      transcript_path: Path | None = None,
      artifact_dir: Path | None = None,
  ) -> dict:
    """Runs pre_tool_guard.main() with JSON stdin and parses its JSON decision output."""
    payload = {
        "conversationId": conv_id,
        "parentConversationId": parent_conv_id,
        "transcriptPath": str(transcript_path) if transcript_path else "",
        "artifactDirectoryPath": str(artifact_dir) if artifact_dir else "",
        "workspacePaths": [str(self.repo)],
        "stepIdx": 5,
        "toolCall": {"name": tool_name, "args": tool_args},
    }
    in_buf = io.StringIO(json.dumps(payload))
    out_buf = io.StringIO()
    with patch.object(sys, "stdin", in_buf), contextlib.redirect_stdout(out_buf):
      pre_tool_guard.main()
    raw = out_buf.getvalue().strip()
    return json.loads(raw) if raw else {"decision": "allow"}

  # Rejects missing and empty 0-byte Gate A test files so vacuous hashes cannot be pinned
  def test_rejects_missing_and_empty_gate_a_test_lock(self) -> None:
    self._make_chat("chat-root")
    rc_missing, _, err_missing = self._run_cli("lock", str(self.repo / "tests" / "NoSuch.cpp"))
    self.assertEqual(rc_missing, 2)
    self.assertIn("does not exist", err_missing)

    empty_test = self.repo / "tests" / "EmptyTest.cpp"
    empty_test.write_text("", encoding="utf-8")
    rc_empty, _, err_empty = self._run_cli("lock", str(empty_test))
    self.assertEqual(rc_empty, 2)
    self.assertIn("empty Gate A test file", err_empty)
    self.assertFalse(self.ledger.exists())

  # Pins canonical realpath and SHA-256 through symlinks and verifies status/verify/clear roundtrip
  def test_lock_status_verify_and_clear_roundtrip_canonical_realpath(self) -> None:
    self._make_chat("chat-root")
    symlink_path = self.root / "alias_test.cpp"
    symlink_path.symlink_to(self.test_file)

    rc_lock, out_lock, _ = self._run_cli("lock", str(symlink_path))
    self.assertEqual(rc_lock, 0)
    self.assertIn(str(self.test_file.resolve()), out_lock)

    rc_status, out_status, _ = self._run_cli("status", "--json")
    self.assertEqual(rc_status, 0)
    rows = json.loads(out_status)["locks"]
    self.assertEqual(len(rows), 1)
    self.assertEqual(rows[0]["path"], str(self.test_file.resolve()))
    self.assertEqual(rows[0]["integrity"], "OK")
    self.assertEqual(rows[0]["chain_root"], "chat-root")
    self.assertEqual(rows[0]["worktree"], str(self.repo.resolve()))

    rc_verify, out_verify, _ = self._run_cli("verify")
    self.assertEqual(rc_verify, 0)
    self.assertIn("verified (1 file(s))", out_verify)

    rc_clear, _, _ = self._run_cli("clear")
    self.assertEqual(rc_clear, 0)
    self.assertEqual(gate_a_lock.load_active_locks(), {})

  # Blocks edit_file and write_file on locked Gate A tests with Contract Honesty guidance
  # while permitting edits to unlocked production files
  def test_pre_tool_guard_blocks_edit_file_and_write_file_with_contract_honesty(self) -> None:
    art_dir, tx_path = self._make_chat("chat-root", read_paths=(self.prod_file,))
    self.assertEqual(self._run_cli("lock", str(self.test_file))[0], 0)

    res_edit = self._run_guard(
        "edit_file",
        {
            "Path": str(self.test_file),
            "Target": "actual == 42",
            "Replacement": "actual >= 0",
        },
        transcript_path=tx_path,
        artifact_dir=art_dir,
    )
    self.assertIsNotNone(res_edit)
    assert res_edit is not None
    self.assertEqual(res_edit["decision"], "deny")
    self.assertIn("[GATE-A HASH LOCK]", res_edit["reason"])
    self.assertIn("Contract Honesty", res_edit["reason"])
    self.assertIn("unlock --reason", res_edit["reason"])

    res_write = self._run_guard(
        "write_file",
        {"Path": str(self.test_file), "Content": "// overwritten\n", "Overwrite": True},
        transcript_path=tx_path,
        artifact_dir=art_dir,
    )
    self.assertIsNotNone(res_write)
    assert res_write is not None
    self.assertEqual(res_write["decision"], "deny")
    self.assertIn("Contract Honesty", res_write["reason"])

    res_prod = self._run_guard(
        "edit_file",
        {
            "Path": str(self.prod_file),
            "Target": "return 0;",
            "Replacement": "return 42;",
        },
        transcript_path=tx_path,
        artifact_dir=art_dir,
    )
    self.assertEqual(res_prod["decision"], "allow")

  # Blocks shell and inline-script mutations of locked Gate A paths in run_command
  # while allowing test execution and gate_a_lock.py CLI calls
  def test_pre_tool_guard_blocks_run_command_mutations_and_allows_reads(self) -> None:
    art_dir, tx_path = self._make_chat("chat-root")
    self.assertEqual(self._run_cli("lock", str(self.test_file))[0], 0)

    mutating_cmds = [
        f"sed -i 's/42/0/' {self.test_file}",
        "cd tests && echo '// clobber' > SkParagraphTest.cpp",
        f"cat <<'EOF' > {self.test_file}\n// heredoc overwrite\nEOF",
        f"cp {self.prod_file} {self.test_file}",
        f"git -C {self.repo} checkout HEAD~0 -- tests/SkParagraphTest.cpp",
        "python3 -c \"from pathlib import Path; Path('SkParagraphTest.cpp').write_text('x')\"",
    ]
    for cmd in mutating_cmds:
      res = self._run_guard(
          "run_command",
          {"CommandLine": cmd, "Cwd": str(self.repo), "WaitMsBeforeAsync": 10000},
          transcript_path=tx_path,
          artifact_dir=art_dir,
      )
      self.assertIsNotNone(res, f"Expected denial for command: {cmd}")
      assert res is not None
      self.assertEqual(res["decision"], "deny")
      self.assertIn("[GATE-A HASH LOCK]", res["reason"])

    safe_cmds = [
        f"cat {self.test_file}",
        "python3 ~/.gemini/config/hooks/gate_a_lock.py status",
        "echo running_dm_tests",
    ]
    for cmd in safe_cmds:
      res_ok = self._run_guard(
          "run_command",
          {"CommandLine": cmd, "Cwd": str(self.repo), "WaitMsBeforeAsync": 10000},
          transcript_path=tx_path,
          artifact_dir=art_dir,
      )
      self.assertEqual(res_ok["decision"], "allow", f"Unexpected denial for: {cmd}")

  # Propagates Gate A locks across child subagents and handoff continuations even after
  # the handed-off predecessor piece is archived
  def test_subagent_and_handoff_continuation_inherit_gate_a_lock(self) -> None:
    root_art, root_tx = self._make_chat("chat-root")
    self.assertEqual(self._run_cli("lock", str(self.test_file), conv_id="chat-root")[0], 0)

    # Record invoke_subagent in chat-root transcript so subagent parent resolves from logs too
    root_steps = root_tx.read_text(encoding="utf-8") + json.dumps({
        "step_index": 2,
        "type": "GENERIC",
        "content": 'Launched subagent with "conversationId": "sub-fixer"',
    }) + "\n"
    root_tx.write_text(root_steps, encoding="utf-8")
    sub_art, sub_tx = self._make_chat("sub-fixer", first_prompt="Fix ParagraphImpl.cpp")

    res_sub = self._run_guard(
        "edit_file",
        {
            "Path": str(self.test_file),
            "Target": "actual == 42",
            "Replacement": "actual == 0",
        },
        conv_id="sub-fixer",
        transcript_path=sub_tx,
        artifact_dir=sub_art,
    )
    self.assertIsNotNone(res_sub)
    assert res_sub is not None
    self.assertEqual(res_sub["decision"], "deny")

    # Simulate handoff from chat-root -> chat-piece2: archive chat-root, keep chat-piece2 open
    (root_art / "scratch" / ".context_guard_state.json").write_text(
        json.dumps({
            "owner": "chat-root",
            "handed_off_to": "chat-piece2",
            "handoff_completed": True,
            "handoff_kind": "handoff",
        }),
        encoding="utf-8",
    )
    (self.app_dir / "annotations" / "chat-root.pbtxt").write_text(
        "archived:true\n", encoding="utf-8"
    )
    (self.app_dir / "brain" / "jetski-chains.json").write_text(
        json.dumps({"parentOf": {"chat-piece2": "chat-root"}, "redirects": {}}),
        encoding="utf-8",
    )
    cont_art, cont_tx = self._make_chat(
        "chat-piece2",
        first_prompt="Continuing unfinished work from previous conversation (conversation://chat-root).",
    )

    self.assertEqual(
        gate_a_lock.resolve_chain_root("chat-piece2", transcript_path=str(cont_tx)),
        "chat-root",
    )
    res_cont = self._run_guard(
        "edit_file",
        {
            "Path": str(self.test_file),
            "Target": "actual == 42",
            "Replacement": "actual == 0",
        },
        conv_id="chat-piece2",
        transcript_path=cont_tx,
        artifact_dir=cont_art,
    )
    self.assertIsNotNone(res_cont)
    assert res_cont is not None
    self.assertEqual(res_cont["decision"], "deny")

    # Continuation piece can verify and clear the chain's lock after Gate B passes
    self.assertEqual(self._run_cli("verify", conv_id="chat-piece2")[0], 0)
    self.assertEqual(self._run_cli("clear", conv_id="chat-piece2")[0], 0)
    self.assertEqual(gate_a_lock.load_active_locks(), {})

  # Detects out-of-band on-disk SHA-256 tampering on subsequent edits/commands, refuses clear,
  # spares unrelated chats on their own files, and permits git checkout restoration
  def test_detects_out_of_band_sha256_tamper_and_allows_git_checkout_restore(self) -> None:
    root_art, root_tx = self._make_chat("chat-root", read_paths=(self.prod_file,))
    other_file = self.root / "other_file.cpp"
    other_file.write_text("int x = 1;\n", encoding="utf-8")
    other_art, other_tx = self._make_chat("chat-other", read_paths=(other_file,))

    self.assertEqual(self._run_cli("lock", str(self.test_file), conv_id="chat-root")[0], 0)

    # Tamper with the locked test file directly on disk outside edit_file
    self.test_file.write_text(
        self.test_body.replace("actual == 42", "true"), encoding="utf-8"
    )

    rc_verify, _, err_verify = self._run_cli("verify", conv_id="chat-root")
    self.assertEqual(rc_verify, 1)
    self.assertIn("hash mismatch", err_verify)

    rc_clear, _, err_clear = self._run_cli("clear", conv_id="chat-root")
    self.assertEqual(rc_clear, 1)
    self.assertIn("hash mismatch", err_clear)

    # Owning chain is blocked on subsequent production edits until Gate A test is restored
    res_tamper = self._run_guard(
        "edit_file",
        {
            "Path": str(self.prod_file),
            "Target": "return 0;",
            "Replacement": "return 42;",
        },
        conv_id="chat-root",
        transcript_path=root_tx,
        artifact_dir=root_art,
    )
    self.assertIsNotNone(res_tamper)
    assert res_tamper is not None
    self.assertEqual(res_tamper["decision"], "deny")
    self.assertIn("SHA-256 integrity mismatch", res_tamper["reason"])

    # Unrelated active chat is not blocked from editing its own unlocked file
    res_other = self._run_guard(
        "edit_file",
        {"Path": str(other_file), "Target": "int x = 1;", "Replacement": "int x = 2;"},
        conv_id="chat-other",
        transcript_path=other_tx,
        artifact_dir=other_art,
    )
    self.assertEqual(res_other["decision"], "allow")

    # Restoring the tampered file via git checkout is permitted by pre_tool_guard
    res_restore = self._run_guard(
        "run_command",
        {
            "CommandLine": f"git -C {self.repo} checkout -- tests/SkParagraphTest.cpp",
            "Cwd": str(self.repo),
            "WaitMsBeforeAsync": 10000,
        },
        conv_id="chat-root",
        transcript_path=root_tx,
        artifact_dir=root_art,
    )
    self.assertEqual(res_restore["decision"], "allow")
    self._git("checkout", "--", "tests/SkParagraphTest.cpp")
    self.assertEqual(self._run_cli("verify", conv_id="chat-root")[0], 0)

  # Requires a non-empty --reason for unlock and logs the reason in the audit history
  def test_unlock_requires_non_empty_reason_and_records_audit_trail(self) -> None:
    art_dir, tx_path = self._make_chat("chat-root", read_paths=(self.test_file,))
    self.assertEqual(self._run_cli("lock", str(self.test_file))[0], 0)

    rc_blank, _, err_blank = self._run_cli("unlock", "--reason", "   ")
    self.assertEqual(rc_blank, 2)
    self.assertIn("--reason", err_blank)

    rc_ok, out_ok, _ = self._run_cli(
        "unlock",
        "--reason",
        "Add RTL trailing-space edge case to Gate A suite",
        str(self.test_file),
    )
    self.assertEqual(rc_ok, 0)
    self.assertIn("Unlocked", out_ok)

    ledger_data = json.loads(self.ledger.read_text(encoding="utf-8"))
    self.assertEqual(ledger_data["locks"], {})
    self.assertEqual(ledger_data["history"][-1]["action"], "unlock")
    self.assertIn("RTL trailing-space", ledger_data["history"][-1]["reason"])

    res_edit = self._run_guard(
        "edit_file",
        {
            "Path": str(self.test_file),
            "Target": "actual == 42",
            "Replacement": "actual == 42 && rtl_ok",
        },
        transcript_path=tx_path,
        artifact_dir=art_dir,
    )
    self.assertEqual(res_edit["decision"], "allow")

  # Automatically ignores stale locks once the owning chain is archived or closed via next-task
  def test_archived_and_next_task_closed_chains_auto_release(self) -> None:
    old_art, _ = self._make_chat("chat-old")
    self.assertEqual(self._run_cli("lock", str(self.test_file), conv_id="chat-old")[0], 0)

    # Archive chat-old; future unrelated chat-fresh must not hit a false-positive lock
    (self.app_dir / "annotations" / "chat-old.pbtxt").write_text(
        "archived:true\n", encoding="utf-8"
    )
    fresh_art, fresh_tx = self._make_chat("chat-fresh", read_paths=(self.test_file,))
    self.assertEqual(gate_a_lock.load_active_locks(), {})
    res_fresh = self._run_guard(
        "edit_file",
        {
            "Path": str(self.test_file),
            "Target": "actual == 42",
            "Replacement": "actual == 43",
        },
        conv_id="chat-fresh",
        transcript_path=fresh_tx,
        artifact_dir=fresh_art,
    )
    self.assertEqual(res_fresh["decision"], "allow")

    # Also verify next-task closure releases locks without treating next-task target as same chain
    (self.app_dir / "annotations" / "chat-old.pbtxt").unlink()
    self.assertEqual(self._run_cli("lock", str(self.test_file), conv_id="chat-old")[0], 0)
    (old_art / "scratch" / ".context_guard_state.json").write_text(
        json.dumps({
            "owner": "chat-old",
            "handed_off_to": "chat-next-item",
            "handoff_completed": True,
            "handoff_kind": "next-task",
        }),
        encoding="utf-8",
    )
    self.assertEqual(gate_a_lock.load_active_locks(), {})


if __name__ == "__main__":
  unittest.main()
