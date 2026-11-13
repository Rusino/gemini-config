#!/usr/bin/env python3
"""Contract tests for R137: Stage 0 context bundler, scout subagent, and COMPACTION footers."""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

CONFIG_ROOT = Path(__file__).resolve().parent.parent
for _sub in ("hooks", "scripts", "skills/review-triage/scripts"):
  _p = str(CONFIG_ROOT / _sub)
  if _p not in sys.path:
    sys.path.insert(0, _p)

import finding_ledger
import pre_tool_guard
import stage_diff_context


class TestR137Stage0ScoutCompaction(unittest.TestCase):
  """Verifies Stage 0 diff/CL/PR bundling, scout Flash guard, and COMPACTION contracts."""

  def _git(self, cwd: Path, *args: str) -> None:
    """Runs a git command in a temporary test repository."""
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

  # Verifies local dirty diffs bundle Side-B files, companion ../include headers, ledger
  # refutations, and transcript constraints while skipping binary/generated/deleted files
  def test_stage_local_worktree_diff_with_companions_ledger_and_skips(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      repo = root / "repo"
      repo.mkdir()
      self._git(repo, "init", "-b", "main")
      self._git(repo, "config", "user.email", "test@example.com")
      self._git(repo, "config", "user.name", "Tester")

      inc_dir = repo / "modules" / "skparagraph" / "include"
      src_dir = repo / "modules" / "skparagraph" / "src"
      inc_dir.mkdir(parents=True)
      src_dir.mkdir(parents=True)

      (inc_dir / "Paragraph.h").write_text(
          "class Paragraph {\npublic:\n  void layout(float w);\n};\n",
          encoding="utf-8",
      )
      long_lines = [f"int line_{i} = {i};" for i in range(1, 261)]
      (src_dir / "Paragraph.cpp").write_text(
          "\n".join(long_lines) + "\n", encoding="utf-8"
      )
      (repo / "old.txt").write_text("obsolete\n", encoding="utf-8")
      (repo / "package-lock.json").write_text('{"lock": 1}\n', encoding="utf-8")
      (repo / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01")
      self._git(repo, "add", ".")
      self._git(repo, "commit", "-m", "Initial baseline")

      # Modify line 140 of Paragraph.cpp, delete old.txt, touch lockfile + binary
      long_lines[139] = "int line_140 = 9999; // updated"
      (src_dir / "Paragraph.cpp").write_text(
          "\n".join(long_lines) + "\n", encoding="utf-8"
      )
      (repo / "old.txt").unlink()
      (repo / "package-lock.json").write_text('{"lock": 2}\n', encoding="utf-8")
      (repo / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x02")

      ledger_dir = root / "ledgers"
      tx_path = root / "transcript.jsonl"
      tx_path.write_text(
          json.dumps({"type": "USER_INPUT", "content": "Keep ABI frozen\nNo extra deps"})
          + "\n",
          encoding="utf-8",
      )
      out_dir = root / "stage0"

      with patch.dict(os.environ, {"JETSKI_LEDGER_DIR": str(ledger_dir)}):
        finding_ledger.add_entry(
            file_target="modules/skparagraph/src/Paragraph.cpp",
            symbol="Paragraph::layout",
            claim="Missing null check",
            reason="Caller guarantees non-null via SkASSERT at Builder:42",
            repo=str(repo),
        )
        summary = stage_diff_context.stage_context(
            out_dir=out_dir,
            repo=str(repo),
            diff_ref="HEAD",
            transcript=tx_path,
        )

      self.assertEqual(summary["mode"], "local")
      self.assertEqual(summary["prior_refutations_count"], 1)
      self.assertEqual(summary["user_constraints_count"], 1)
      self.assertEqual(summary["recommended_topology"], "single_hunter")
      skipped_map = {s["path"]: s["reason"] for s in summary["skipped_files"]}
      self.assertEqual(skipped_map.get("old.txt"), "deleted")
      self.assertEqual(skipped_map.get("package-lock.json"), "generated_or_lockfile")
      self.assertEqual(skipped_map.get("icon.png"), "binary_diff")

      cpp_staged = out_dir / "files" / "modules__skparagraph__src__Paragraph.cpp.txt"
      hdr_staged = out_dir / "files" / "modules__skparagraph__include__Paragraph.h.txt"
      self.assertTrue(cpp_staged.is_file())
      self.assertTrue(hdr_staged.is_file())
      self.assertIn(" 140: int line_140 = 9999;", cpp_staged.read_text(encoding="utf-8"))

      bundle_md = (out_dir / "files_bundle.md").read_text(encoding="utf-8")
      self.assertIn("Paragraph.h` (4 lines — complete, companion header)", bundle_md)
      self.assertIn("windows inline; full 1-indexed file at", bundle_md)

      ctx_md = (out_dir / "context.md").read_text(encoding="utf-8")
      self.assertIn("Keep ABI frozen No extra deps", ctx_md)
      self.assertIn("Missing null check", ctx_md)

  # Verifies R143 History-RAG ranks co-modified production files while filtering tests, docs,
  # mechanical renames, and mega-commits
  def test_history_rag_comodified_siblings_ranks_and_filters(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      repo = Path(tmp) / "repo"
      repo.mkdir()
      self._git(repo, "init", "-b", "main")
      self._git(repo, "config", "user.email", "test@example.com")
      self._git(repo, "config", "user.name", "Tester")

      src = repo / "src"
      tests = repo / "tests"
      docs = repo / "docs"
      src.mkdir()
      tests.mkdir()
      docs.mkdir()

      for name in ("layout.cpp", "run.cpp", "caret.cpp", "paint.cpp"):
        (src / name).write_text("int v = 0;\n", encoding="utf-8")
      (tests / "layout_test.cpp").write_text("int t = 0;\n", encoding="utf-8")
      (docs / "guide.md").write_text("# Guide\n", encoding="utf-8")
      self._git(repo, "add", ".")
      self._git(repo, "commit", "-m", "Initial")

      # Commit 1 & 2: layout.cpp co-modified with run.cpp, test, and doc
      for idx in (1, 2):
        (src / "layout.cpp").write_text(f"int v = {idx};\n", encoding="utf-8")
        (src / "run.cpp").write_text(f"int v = {idx};\n", encoding="utf-8")
        (tests / "layout_test.cpp").write_text(f"int t = {idx};\n", encoding="utf-8")
        (docs / "guide.md").write_text(f"# Guide {idx}\n", encoding="utf-8")
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-m", f"Co-mod run {idx}")

      # Commit 3: layout.cpp co-modified with caret.cpp once
      (src / "layout.cpp").write_text("int v = 3;\n", encoding="utf-8")
      (src / "caret.cpp").write_text("int v = 3;\n", encoding="utf-8")
      self._git(repo, "add", ".")
      self._git(repo, "commit", "-m", "Co-mod caret")

      # Commit 4: mega-commit touching >12 files (must be filtered out)
      (src / "layout.cpp").write_text("int v = 4;\n", encoding="utf-8")
      (src / "paint.cpp").write_text("int v = 4;\n", encoding="utf-8")
      for i in range(13):
        (src / f"bulk_{i}.cpp").write_text(f"int b = {i};\n", encoding="utf-8")
      self._git(repo, "add", ".")
      self._git(repo, "commit", "-m", "Mega tree-wide formatting")

      # Dirty worktree modifying only src/layout.cpp
      (src / "layout.cpp").write_text("int v = 5;\n", encoding="utf-8")
      out_dir = Path(tmp) / "stage0"
      with patch.dict(os.environ, {"JETSKI_LEDGER_DIR": str(Path(tmp) / "ledgers")}):
        summary = stage_diff_context.stage_context(
            out_dir=out_dir, repo=str(repo), diff_ref="HEAD"
        )

      siblings = summary["history_rag_siblings"]
      self.assertEqual(
          [s["path"] for s in siblings],
          ["src/run.cpp", "src/caret.cpp"],
      )
      self.assertEqual(siblings[0]["co_commits"], 2)
      self.assertEqual(siblings[0]["paired_with"], ["src/layout.cpp"])
      self.assertEqual(siblings[1]["co_commits"], 1)
      ctx_md = (out_dir / "context.md").read_text(encoding="utf-8")
      self.assertIn("History-RAG Co-Modified Production Siblings", ctx_md)
      self.assertIn("`src/run.cpp` (co-modified in 2 recent commit(s)", ctx_md)
      self.assertNotIn("layout_test.cpp", ctx_md)
      self.assertNotIn("guide.md", ctx_md)
      self.assertNotIn("paint.cpp", ctx_md)

  # Verifies clean HEAD fallback stages the top commit when working tree has no unstaged changes
  def test_stage_local_clean_head_fallback(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      repo = Path(tmp) / "repo"
      repo.mkdir()
      self._git(repo, "init", "-b", "main")
      self._git(repo, "config", "user.email", "test@example.com")
      self._git(repo, "config", "user.name", "Tester")
      (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
      self._git(repo, "add", ".")
      self._git(repo, "commit", "-m", "Base")
      (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
      self._git(repo, "add", ".")
      self._git(repo, "commit", "-m", "Top commit message")

      out_dir = Path(tmp) / "out"
      with patch.dict(os.environ, {"JETSKI_LEDGER_DIR": str(Path(tmp) / "ledgers")}):
        summary = stage_diff_context.stage_context(out_dir=out_dir, repo=str(repo))

      self.assertIn("head_commit", summary["target"])
      self.assertEqual(summary["files_count"], 1)
      self.assertIn("   1: x = 2", (out_dir / "files" / "a.py.txt").read_text("utf-8"))

      # Three-dot git range (HEAD~1...HEAD) must resolve right_rev='HEAD', not '.HEAD'
      out_dots = Path(tmp) / "out_dots"
      with patch.dict(os.environ, {"JETSKI_LEDGER_DIR": str(Path(tmp) / "ledgers")}):
        dots_sum = stage_diff_context.stage_context(
            out_dir=out_dots, repo=str(repo), diff_ref="HEAD~1...HEAD"
        )
      self.assertEqual(dots_sum["bundled_files_count"], 1)
      self.assertIn("   1: x = 2", (out_dots / "files" / "a.py.txt").read_text("utf-8"))

  # Verifies Gerrit CL staging includes open threads, Side-B REST files, and ABANDONED short-circuit
  def test_stage_remote_gerrit_active_and_abandoned(self) -> None:
    patch_txt = (
        "diff --git a/src/foo.cpp b/src/foo.cpp\n"
        "--- a/src/foo.cpp\n"
        "+++ b/src/foo.cpp\n"
        "@@ -1,1 +1,2 @@\n"
        " int a = 1;\n"
        "+int b = 2;\n"
        "diff --git a/lib/bar.dart b/lib/bar.dart\n"
        "--- a/lib/bar.dart\n"
        "+++ b/lib/bar.dart\n"
        "@@ -1,1 +1,2 @@\n"
        " final x = 1;\n"
        "+final y = 2;\n"
    )
    fake_cl = {
        "host": "skia-review.googlesource.com",
        "subject": "[SkParagraph] Cross-layer test",
        "owner_email": "dev@google.com",
        "commit_message": "[SkParagraph] Cross-layer test\n\nBody.",
        "patch": patch_txt,
        "patchset": 3,
        "detail": {"status": "NEW", "project": "skia", "current_revision": "abc1234"},
        "thread_context": {
            "current_ps": 3,
            "current_sha": "abc1234",
            "open_threads": [{
                "location": "src/foo.cpp:2",
                "root_author": "Reviewer",
                "patch_set": 2,
                "current_ps": 3,
                "tags": ["AUTHOR_REPLIED"],
                "root_excerpt": "Check overflow",
                "last_role": "author",
                "last_excerpt": "Fixed in PS3",
            }],
            "dialogue": ["[PS2 REVIEWER] Check overflow"],
        },
    }

    def fake_urlopen(req, timeout=12):
      url = req.full_url
      resp = MagicMock()
      resp.__enter__.return_value = resp
      if "src%2Ffoo.cpp" in url:
        resp.read.return_value = base64.b64encode(b"int a = 1;\nint b = 2;\n")
      elif "lib%2Fbar.dart" in url:
        resp.read.return_value = base64.b64encode(b"final x = 1;\nfinal y = 2;\n")
      else:
        raise OSError("404")
      return resp

    with tempfile.TemporaryDirectory() as tmp:
      out_active = Path(tmp) / "gerrit_active"
      with (
          patch.dict(os.environ, {"JETSKI_LEDGER_DIR": str(Path(tmp) / "ledgers")}),
          patch("triage_cl.probe_gerrit", return_value=fake_cl),
          patch("urllib.request.urlopen", side_effect=fake_urlopen),
      ):
        summary = stage_diff_context.stage_context(
            out_dir=out_active, repo="", target="1414016"
        )
      self.assertEqual(summary["mode"], "gerrit")
      self.assertEqual(summary["open_threads_count"], 1)
      # Cross-layer C++ + Dart triggers sharded_2_3 even with 2 files
      self.assertEqual(summary["recommended_topology"], "sharded_2_3")
      self.assertEqual(len(summary["recommended_shards"]), 2)
      ctx = (out_active / "context.md").read_text(encoding="utf-8")
      self.assertIn("Check overflow", ctx)
      self.assertIn("Fixed in PS3", ctx)

      abandoned_cl = {
          **fake_cl,
          "detail": {**fake_cl["detail"], "status": "ABANDONED"},
      }
      out_aban = Path(tmp) / "gerrit_abandoned"
      with (
          patch.dict(os.environ, {"JETSKI_LEDGER_DIR": str(Path(tmp) / "ledgers")}),
          patch("triage_cl.probe_gerrit", return_value=abandoned_cl),
      ):
        ab_sum = stage_diff_context.stage_context(
            out_dir=out_aban, repo="", target="1414016"
        )
      self.assertEqual(ab_sum["status"], "ABANDONED")
      self.assertEqual(ab_sum["recommended_topology"], "0_subagents")
      self.assertEqual(ab_sum["bundled_files_count"], 0)

  # Verifies scout agent definition and pre_tool_guard Flash-only enforcement
  def test_scout_agent_definition_and_flash_routing_guard(self) -> None:
    scout_path = CONFIG_ROOT / "agents" / "scout.md"
    self.assertTrue(scout_path.is_file())
    scout_txt = scout_path.read_text(encoding="utf-8")
    for required in (
        "name: scout",
        "mainAgent: false",
        "subagent: true",
        "model: flash",
        "inheritCustomizations: false",
        "inheritMcp: false",
        "at most 3 tool turns",
        "file:line",
    ):
      self.assertIn(required, scout_txt)

    def call_guard(args: dict) -> dict:
      payload = {
          "conversationId": "c-scout",
          "toolCall": {"name": "invoke_subagent", "args": args},
      }
      with (
          patch("sys.stdin", io.StringIO(json.dumps(payload))),
          patch("sys.stdout", new_callable=io.StringIO) as out,
      ):
        pre_tool_guard.main()
      return json.loads(out.getvalue())

    for bad_model in (None, "inherit", "pro"):
      args = {"TypeName": "scout", "Role": "Caller Lookup", "Prompt": "Find callers"}
      if bad_model is not None:
        args["Model"] = bad_model
      res = call_guard(args)
      self.assertEqual(res.get("decision"), "deny", f"Expected deny for {bad_model}")
      self.assertIn("[SUBAGENT-MODEL GUARD]", res.get("reason", ""))

    for ok_model in ("flash", "flash_lite"):
      res = call_guard({
          "TypeName": "scout",
          "Model": ok_model,
          "Role": "Caller Lookup",
          "Prompt": "Find callers",
      })
      self.assertEqual(res.get("decision"), "allow", f"Unexpected deny: {res}")

  # Verifies COMPACTION: YES/NO footer and re-sharding requirement across all 6 audit prompts/docs
  def test_compaction_footer_documented_across_agents_and_skills(self) -> None:
    required_files = [
        CONFIG_ROOT / "plugins" / "keeper" / "agents" / "keeper_inquisitor.md",
        CONFIG_ROOT / "plugins" / "keeper" / "agents" / "keeper_skeptic.md",
        CONFIG_ROOT / "skills" / "amnesia-precommit" / "SKILL.md",
        CONFIG_ROOT / "skills" / "amnesia-precommit" / "references" / "hunter_prompt.md",
        CONFIG_ROOT / "skills" / "review-triage" / "SKILL.md",
        CONFIG_ROOT / "skills" / "ruthless-code-audit" / "SKILL.md",
    ]
    for path in required_files:
      txt = path.read_text(encoding="utf-8")
      self.assertIn("COMPACTION: YES", txt, f"Missing COMPACTION: YES in {path}")
      self.assertIn("COMPACTION: NO", txt, f"Missing COMPACTION: NO in {path}")
      self.assertIn("<CONTEXT_SUMMARY>", txt, f"Missing <CONTEXT_SUMMARY> in {path}")

  # Verifies R4 Part (3) walk-up governing doc discovery, directive extraction (Check A),
  # stale symbol/flag/path drift detection (Check B), private-helper filtering, and NO_DOC_SYNC
  def test_governing_doc_drift_discovery_directives_and_no_doc_sync(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      repo = Path(tmp) / "repo"
      repo.mkdir()
      self._git(repo, "init", "-b", "main")
      self._git(repo, "config", "user.email", "test@example.com")
      self._git(repo, "config", "user.name", "Tester")

      (repo / "AGENTS.md").write_text(
          "# Root Rules\n- NEVER bypass pre-commit checks.\n", encoding="utf-8"
      )
      mod_dir = repo / "skills" / "demo"
      mod_dir.mkdir(parents=True)
      (mod_dir / "SKILL.md").write_text(
          "# Demo Skill\n"
          "MUST validate inputs before running.\n"
          "Invoke `run_legacy_check` with `--legacy-flag` or `skills/demo/old_helper.py`.\n"
          "<!-- no-validate -->\n"
          "Ignored stale `run_legacy_check` and `--legacy-flag` block.\n"
          "<!-- /no-validate -->\n",
          encoding="utf-8",
      )
      (mod_dir / "runner.py").write_text(
          "def _private_helper():\n"
          "  return 1\n\n"
          "def run_legacy_check():\n"
          "  return '--legacy-flag'\n",
          encoding="utf-8",
      )
      (mod_dir / "old_helper.py").write_text("x = 1\n", encoding="utf-8")
      self._git(repo, "add", ".")
      self._git(repo, "commit", "-m", "Initial baseline")

      # Rename run_legacy_check -> run_modern_check, drop --legacy-flag, drop _private_helper,
      # and delete old_helper.py without updating SKILL.md
      (mod_dir / "runner.py").write_text(
          "def _new_private_helper():\n"
          "  return 2\n\n"
          "def run_modern_check():\n"
          "  return '--modern-flag'\n",
          encoding="utf-8",
      )
      (mod_dir / "old_helper.py").unlink()

      out_dir = Path(tmp) / "stage0"
      with patch.dict(os.environ, {"JETSKI_LEDGER_DIR": str(Path(tmp) / "ledgers")}):
        summary = stage_diff_context.stage_context(
            out_dir=out_dir, repo=str(repo), diff_ref="HEAD"
        )

      gov_paths = [g["doc_path"] for g in summary["governing_docs"]]
      self.assertIn("skills/demo/SKILL.md", gov_paths)
      self.assertIn("AGENTS.md", gov_paths)
      skill_gov = next(
          g for g in summary["governing_docs"] if g["doc_path"] == "skills/demo/SKILL.md"
      )
      self.assertFalse(skill_gov["co_updated"])
      self.assertTrue(any("MUST validate inputs" in d for d in skill_gov["directives"]))

      drifts = summary["doc_code_drift"]
      drift_tokens = {(d["kind"], d["token"], d["doc_line"]) for d in drifts}
      self.assertIn(("deleted_path", "skills/demo/old_helper.py", 3), drift_tokens)
      self.assertIn(("removed_cli_flag", "--legacy-flag", 3), drift_tokens)
      self.assertIn(("removed_or_renamed_symbol", "run_legacy_check", 3), drift_tokens)
      # Private helper and <!-- no-validate --> lines must never be flagged
      self.assertFalse(any(d["token"] == "_private_helper" for d in drifts))
      self.assertFalse(any(d["doc_line"] == 5 for d in drifts))

      ctx_md = (out_dir / "context.md").read_text(encoding="utf-8")
      self.assertIn("Governing Docs & Doc <-> Code Drift Check (R4)", ctx_md)
      self.assertIn("[DRIFT `removed_cli_flag`]", ctx_md)

      # Verify NO_DOC_SYNC=<reason> suppresses drift candidates
      self._git(repo, "add", "-A")
      self._git(
          repo,
          "commit",
          "-m",
          "Migrate runner\n\nNO_DOC_SYNC=doc update staged in follow-up CL",
      )
      out_skip = Path(tmp) / "stage0_skip"
      with patch.dict(os.environ, {"JETSKI_LEDGER_DIR": str(Path(tmp) / "ledgers")}):
        skip_sum = stage_diff_context.stage_context(
            out_dir=out_skip, repo=str(repo), diff_ref="HEAD~1..HEAD"
        )
      self.assertEqual(
          skip_sum["no_doc_sync_reason"], "doc update staged in follow-up CL"
      )
      self.assertEqual(skip_sum["doc_code_drift"], [])


if __name__ == "__main__":
  unittest.main()
