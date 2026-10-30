#!/usr/bin/env python3
"""Unit tests for check_doc_grounding.py resolution, normalization, and false positives."""

from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "amnesia-doc-check"
    / "scripts"
    / "check_doc_grounding.py"
)
spec = importlib.util.spec_from_file_location("check_doc_grounding", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
cdg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cdg)


class TestCheckDocGrounding(unittest.TestCase):

  def setUp(self) -> None:
    self.temp_dir = tempfile.mkdtemp(prefix="cdg_test_")
    self.repo_dir = Path(self.temp_dir) / "repo"
    self.repo_dir.mkdir(parents=True)

  def tearDown(self) -> None:
    shutil.rmtree(self.temp_dir, ignore_errors=True)

  def test_canonicalize_for_conflict_same_file_relative_and_absolute(self) -> None:
    target = self.repo_dir / "plugins" / "keeper" / "keeper_skeptic.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Test\n", encoding="utf-8")

    rel_cand = "plugins/keeper/keeper_skeptic.md"
    abs_cand = str(target.resolve())

    canon_rel = cdg.canonicalize_for_conflict(rel_cand, [self.repo_dir], [])
    canon_abs = cdg.canonicalize_for_conflict(abs_cand, [self.repo_dir], [])

    # Both must canonicalize to the exact same repo-relative identity
    self.assertEqual(canon_rel, "plugins/keeper/keeper_skeptic.md")
    self.assertEqual(canon_abs, "plugins/keeper/keeper_skeptic.md")

  def test_canonicalize_for_conflict_doc_relative_references(self) -> None:
    skill_dir = self.repo_dir / "skills" / "sample"
    ref_file = skill_dir / "references" / "guide.md"
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_text("# Guide\n", encoding="utf-8")

    doc_dirs = [skill_dir]
    rel_cand = "references/guide.md"
    abs_cand = str(ref_file.resolve())

    canon_rel = cdg.canonicalize_for_conflict(rel_cand, [self.repo_dir], doc_dirs)
    canon_abs = cdg.canonicalize_for_conflict(abs_cand, [self.repo_dir], doc_dirs)

    self.assertEqual(canon_rel, "skills/sample/references/guide.md")
    self.assertEqual(canon_abs, "skills/sample/references/guide.md")

  def test_canonicalize_for_conflict_preserves_genuinely_different_paths(self) -> None:
    file_a = self.repo_dir / "dir_a" / "item.txt"
    file_b = self.repo_dir / "dir_b" / "item.txt"
    file_a.parent.mkdir(parents=True, exist_ok=True)
    file_b.parent.mkdir(parents=True, exist_ok=True)
    file_a.write_text("A", encoding="utf-8")
    file_b.write_text("B", encoding="utf-8")

    canon_a = cdg.canonicalize_for_conflict("dir_a/item.txt", [self.repo_dir], [])
    canon_b = cdg.canonicalize_for_conflict("dir_b/item.txt", [self.repo_dir], [])

    self.assertNotEqual(canon_a, canon_b)
    self.assertEqual(canon_a, "dir_a/item.txt")
    self.assertEqual(canon_b, "dir_b/item.txt")

  def test_bare_path_regex_ignores_shell_variable_prefixes(self) -> None:
    lines = [
      "python3 $S/opt_ledger.py --repo <repo> path",
      "python3 ${VAR}/scripts/audit.py check",
      "Set S=~/.gemini/config/scripts",
    ]
    extracted: list[str] = []
    for line in lines:
      line_no_urls = cdg.re.sub(
          r"(?:https?|file)://\S+|`[^`\n]*`|\$(?:\{[^}\s]+\}|[A-Za-z_]\w*)(?:/[^\s`'\")]+)*",
          " ",
          line,
      )
      for m in cdg.BARE_PATH_RE.finditer(line_no_urls):
        tok = m.group(1).strip()
        if cdg.looks_like_path(tok):
          extracted.append(tok)

    # Shell variable paths must not have S/opt_ledger.py or scripts/audit.py extracted
    self.assertNotIn("S/opt_ledger.py", extracted)
    self.assertNotIn("scripts/audit.py", extracted)
    self.assertNotIn("opt_ledger.py", extracted)
    self.assertNotIn("audit.py", extracted)

  def test_path_exists_anywhere_with_doc_dirs(self) -> None:
    skill_a = self.repo_dir / "skills" / "skill_a"
    skill_b = self.repo_dir / "skills" / "skill_b"
    skill_a.mkdir(parents=True, exist_ok=True)
    skill_b.mkdir(parents=True, exist_ok=True)
    (skill_b / "SKILL.md").write_text("# Skill B\n", encoding="utf-8")
    (skill_a / "notes.md").write_text("# Notes\n", encoding="utf-8")
    (skill_a / "references").mkdir(parents=True, exist_ok=True)
    (skill_a / "references" / "helper.md").write_text("# Helper\n", encoding="utf-8")

    doc_dirs = [skill_a, skill_a.parent]
    repo_rel_set: set[str] = set()

    # Relative to doc directory
    self.assertTrue(
        cdg.path_exists_anywhere("notes.md", [self.repo_dir], repo_rel_set, doc_dirs=doc_dirs)
    )
    self.assertTrue(
        cdg.path_exists_anywhere(
            "references/helper.md", [self.repo_dir], repo_rel_set, doc_dirs=doc_dirs
        )
    )
    # Inside references directory resolved for bare filename
    self.assertTrue(
        cdg.path_exists_anywhere(
            "helper.md", [self.repo_dir], repo_rel_set, doc_dirs=doc_dirs
        )
    )
    # Sibling skill relative to doc_dirs parent (skills/)
    self.assertTrue(
        cdg.path_exists_anywhere(
            "skill_b/SKILL.md", [self.repo_dir], repo_rel_set, doc_dirs=doc_dirs
        )
    )


if __name__ == "__main__":
  unittest.main()
