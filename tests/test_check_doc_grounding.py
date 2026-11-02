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

  def test_check_rule_skill_hygiene_catches_numbering_and_gate_drift(self) -> None:
    doc = (
        "# Sample Skill\n\n"
        "## 1. Core Principles\n"
        "1. First item\n"
        "2. Second item\n"
        "2. Duplicate second item\n\n"
        "## 4. Workflow\n"
        "- **WebParagraph 11-Rule Gate**:\n"
        "  1. Rule one\n"
        "  2. Rule two\n"
    )
    warnings = cdg.check_rule_skill_hygiene(
        doc.splitlines(), doc, self.repo_dir / "skills" / "s" / "SKILL.md", [self.repo_dir]
    )
    joined = "\n".join(warnings)
    self.assertIn("Non-sequential ordered list item `2.` after `2.`", joined)
    self.assertIn("Non-sequential heading number `4` after `1`", joined)
    self.assertIn("Header claims `11-Rule Gate` but block contains `2` numbered items", joined)

  def test_check_rule_skill_hygiene_catches_retired_skill_and_missing_mcp(self) -> None:
    retired_dir = self.repo_dir / "skills" / "gmail"
    retired_dir.mkdir(parents=True, exist_ok=True)
    rule_text = "NEVER invoke the CLI or follow the `gmail` skill.\n"
    w_ret = cdg.check_rule_skill_hygiene(
        rule_text.splitlines(),
        rule_text,
        self.repo_dir / "plugins" / "google_gmail" / "rules" / "AGENTS.md",
        [self.repo_dir],
    )
    self.assertTrue(any("Retired skill `gmail`" in w for w in w_ret))

    gdir = self.repo_dir / "plugins" / "google_gmail" / "rules"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "AGENTS.md").write_text(rule_text, encoding="utf-8")
    rules_dir = self.repo_dir / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    sub_path = rules_dir / "subagent_exploration.md"
    sub_text = "# Exploration\n- Use `google_chat_google_chat` and `google_docs_google_docs`.\n"
    sub_path.write_text(sub_text, encoding="utf-8")
    w_mcp = cdg.check_rule_skill_hygiene(
        sub_text.splitlines(), sub_text, sub_path, [self.repo_dir]
    )
    self.assertTrue(any("google_gmail_google_gmail" in w for w in w_mcp))

  def test_section8_hygiene_warnings_return_nonzero_exit_code(self) -> None:
    import io
    from unittest import mock

    skill_dir = self.repo_dir / "skills" / "bad-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: bad-skill\ndescription: >\n  Uses bare folded scalar.\n---\n# Bad Skill\n",
        encoding="utf-8",
    )

    out = io.StringIO()
    with (
        mock.patch.object(
            cdg.sys,
            "argv",
            ["check_doc_grounding.py", "--repo", str(self.repo_dir), str(skill_md)],
        ),
        mock.patch("sys.stdout", out),
    ):
      rc = cdg.main()
    self.assertEqual(rc, 1, out.getvalue())

    clean_dir = self.repo_dir / "skills" / "clean-skill"
    clean_dir.mkdir(parents=True, exist_ok=True)
    clean_md = clean_dir / "SKILL.md"
    clean_md.write_text(
        "---\nname: clean-skill\ndescription: >-\n  Clean skill summary.\n---\n# Clean Skill\n",
        encoding="utf-8",
    )
    out_clean = io.StringIO()
    with (
        mock.patch.object(
            cdg.sys,
            "argv",
            ["check_doc_grounding.py", "--repo", str(self.repo_dir), str(clean_md)],
        ),
        mock.patch("sys.stdout", out_clean),
    ):
      rc_clean = cdg.main()
    self.assertEqual(rc_clean, 0, out_clean.getvalue())

  def test_skill_and_reference_line_and_byte_limits(self) -> None:
    # Own skill exceeding 400 lines triggers soft warning
    own_dir = self.repo_dir / "skills" / "own-large"
    own_dir.mkdir(parents=True, exist_ok=True)
    own_md = own_dir / "SKILL.md"
    soft_lines_doc = (
        "---\nname: own-large\ndescription: >-\n  Valid summary.\n---\n"
        + "".join(f"Line {i}\n" for i in range(400))
    )
    own_md.write_text(soft_lines_doc, encoding="utf-8")
    w_soft = cdg.check_rule_skill_hygiene(
        soft_lines_doc.splitlines(), soft_lines_doc, own_md, [self.repo_dir]
    )
    self.assertTrue(any("400" in w for w in w_soft), w_soft)

    # Own skill exceeding 40,000 bytes (even with < 400 lines) triggers soft warning
    soft_bytes_doc = (
        "---\nname: own-large\ndescription: >-\n  Valid summary.\n---\n"
        + ("x" * 250 + "\n") * 165
    )
    own_md.write_text(soft_bytes_doc, encoding="utf-8")
    w_soft_b = cdg.check_rule_skill_hygiene(
        soft_bytes_doc.splitlines(), soft_bytes_doc, own_md, [self.repo_dir]
    )
    self.assertTrue(any("40,000" in w or "40000" in w for w in w_soft_b), w_soft_b)

    # Hard ceiling (> 800 lines or > 46,080 bytes) fires on own and non-exempt external skills
    ext_root = Path(self.temp_dir) / "external_skills"
    ext_other = ext_root / "other-ext"
    ext_other.mkdir(parents=True, exist_ok=True)
    hard_doc = (
        "---\nname: other-ext\ndescription: >-\n  Valid summary.\n---\n"
        + "".join(f"Line {i}\n" for i in range(805))
    )
    (ext_other / "SKILL.md").write_text(hard_doc, encoding="utf-8")
    sym_other = self.repo_dir / "skills" / "other-ext"
    os.symlink(ext_other, sym_other)
    w_hard = cdg.check_rule_skill_hygiene(
        hard_doc.splitlines(), hard_doc, sym_other / "SKILL.md", [self.repo_dir]
    )
    self.assertTrue(any("800" in w or "46,080" in w for w in w_hard), w_hard)

    # External gsheets / gslides symlink is exempt from SKILL.md soft and hard ceilings
    ext_gsheets = ext_root / "gsheets"
    ext_gsheets.mkdir(parents=True, exist_ok=True)
    gsheets_doc = (
        "---\nname: gsheets\ndescription: >-\n  Valid summary.\n---\n"
        + "".join(f"Line {i}\n" for i in range(820))
    )
    (ext_gsheets / "SKILL.md").write_text(gsheets_doc, encoding="utf-8")
    sym_gsheets = self.repo_dir / "skills" / "gsheets"
    os.symlink(ext_gsheets, sym_gsheets)
    w_gsheets = cdg.check_rule_skill_hygiene(
        gsheets_doc.splitlines(), gsheets_doc, sym_gsheets / "SKILL.md", [self.repo_dir]
    )
    self.assertEqual(w_gsheets, [])

    # Reference file limits: <= 500 lines & <= 42,000 bytes (hard ceiling 46,080 bytes)
    ref_skill = self.repo_dir / "skills" / "ref-skill"
    refs_dir = ref_skill / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)
    (refs_dir / "big_topic.md").write_text(
        "".join(f"Ref line {i}\n" for i in range(505)), encoding="utf-8"
    )
    (refs_dir / "huge_bytes.md").write_text(("y" * 250 + "\n") * 190, encoding="utf-8")
    ref_skill_doc = (
        "---\nname: ref-skill\n"
        "description: >-\n  Covers big topic and huge bytes workflows.\n---\n"
        "See `references/big_topic.md` and `references/huge_bytes.md`.\n"
    )
    (ref_skill / "SKILL.md").write_text(ref_skill_doc, encoding="utf-8")
    w_refs = cdg.check_rule_skill_hygiene(
        ref_skill_doc.splitlines(), ref_skill_doc, ref_skill / "SKILL.md", [self.repo_dir]
    )
    joined_refs = "\n".join(w_refs)
    self.assertIn("big_topic.md", joined_refs)
    self.assertIn("500", joined_refs)
    self.assertIn("huge_bytes.md", joined_refs)
    self.assertIn("46,080", joined_refs)

  def test_one_hop_orphan_file_check(self) -> None:
    skill_dir = self.repo_dir / "skills" / "orphan-demo"
    (skill_dir / "references").mkdir(parents=True, exist_ok=True)
    (skill_dir / "scripts" / "__pycache__").mkdir(parents=True, exist_ok=True)

    (skill_dir / "references" / "linked_topic.md").write_text("# Linked\n", encoding="utf-8")
    (skill_dir / "references" / "orphan_topic.md").write_text("# Orphan\n", encoding="utf-8")
    (skill_dir / "references" / ".hidden.md").write_text("# Hidden\n", encoding="utf-8")

    (skill_dir / "scripts" / "used_tool.py").write_text("print(1)\n", encoding="utf-8")
    (skill_dir / "scripts" / "orphan_helper.py").write_text("print(2)\n", encoding="utf-8")
    (skill_dir / "scripts" / "test_used_tool.py").write_text("print(3)\n", encoding="utf-8")
    (skill_dir / "scripts" / "used_tool_test.py").write_text("print(4)\n", encoding="utf-8")
    (skill_dir / "scripts" / "cached.pyc").write_bytes(b"\x00")
    (skill_dir / "scripts" / ".env").write_text("X=1\n", encoding="utf-8")
    (skill_dir / "scripts" / "__pycache__" / "used_tool.cpython-313.pyc").write_bytes(b"\x00")

    doc = (
        "---\nname: orphan-demo\n"
        "description: >-\n  Handles linked and orphan demo topics.\n---\n"
        "Read `references/linked_topic.md` and run `used_tool.py`.\n"
    )
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(doc, encoding="utf-8")

    warnings = cdg.check_rule_skill_hygiene(
        doc.splitlines(), doc, skill_md, [self.repo_dir]
    )
    joined = "\n".join(warnings)
    self.assertIn("orphan_topic.md", joined)
    self.assertIn("orphan_helper.py", joined)
    self.assertNotIn("linked_topic.md", joined)
    self.assertNotIn("used_tool.py", joined)
    self.assertNotIn("test_used_tool.py", joined)
    self.assertNotIn("used_tool_test.py", joined)
    self.assertNotIn("cached.pyc", joined)
    self.assertNotIn(".hidden.md", joined)
    self.assertNotIn(".env", joined)

  def test_yaml_description_hygiene_and_frontmatter_blindness(self) -> None:
    skill_dir = self.repo_dir / "skills" / "desc-check"
    (skill_dir / "references").mkdir(parents=True, exist_ok=True)
    (skill_dir / "references" / "README.md").write_text("# Ignored\n", encoding="utf-8")
    (skill_dir / "references" / "contributing.md").write_text("# Ignored\n", encoding="utf-8")
    (skill_dir / "references" / "skill_bidi.md").write_text("# BiDi\n", encoding="utf-8")

    bad_doc = (
        "---\nname: desc-check\n"
        "description: >\n"
        "  Run `audit_tool.py` with `--intake-id` or `/google/bin/releases/x/cli` to generate\n"
        "  `<stem>_diff.html` for this skill.\n"
        "---\n"
        "See `README.md`, `contributing.md`, and `skill_bidi.md`.\n"
    )
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(bad_doc, encoding="utf-8")
    w_bad = cdg.check_rule_skill_hygiene(
        bad_doc.splitlines(), bad_doc, skill_md, [self.repo_dir]
    )
    joined_bad = "\n".join(w_bad)
    self.assertIn("description: >", joined_bad)
    self.assertIn("<", joined_bad)
    self.assertIn("--intake-id", joined_bad)
    self.assertIn("audit_tool.py", joined_bad)
    self.assertIn("skill_bidi.md", joined_bad)
    self.assertNotIn("README.md", joined_bad)
    self.assertNotIn("contributing.md", joined_bad)

    # Clean description with >-, prose slashes, URLs, artifact names, and topic keyword 'BiDi'
    good_doc = (
        "---\nname: desc-check\n"
        "description: >-\n"
        "  Audits C++/Dart/TS and BiDi layout across docs.google.com/spreadsheets, `.csv`,\n"
        "  `compare.md`, and `attestation.json`.\n"
        "---\n"
        "See `README.md`, `contributing.md`, and `skill_bidi.md`.\n"
    )
    skill_md.write_text(good_doc, encoding="utf-8")
    w_good = cdg.check_rule_skill_hygiene(
        good_doc.splitlines(), good_doc, skill_md, [self.repo_dir]
    )
    self.assertEqual(w_good, [])

  def test_bash_var_command_prefix_and_table_inline_code_pipes(self) -> None:
    skill_dir = self.repo_dir / "skills" / "fmt-check"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    doc = (
        "---\nname: fmt-check\ndescription: >-\n  Valid summary.\n---\n"
        "# Formatting Check\n\n"
        "```bash\n"
        "MY_BIN=/usr/bin/env\n"
        "echo $MY_BIN is set\n"
        "python3 run.py \\\n"
        "  $OUT_DIR --verbose\n"
        "$MY_BIN subcommand --flag\n"
        "```\n\n"
        "```python\n"
        "# Inside python fence, not bash\n"
        "$NOT_BASH something\n"
        "```\n\n"
        "| Command | Notes |\n"
        "|---|---|\n"
        "| `handoff --model <id\\|name>` | Escaped `\\|` is valid |\n"
        "| `foo` | `bar` |\n"
        "| `run --mode a|b` | Unescaped pipe breaks GFM table |\n"
    )
    skill_md.write_text(doc, encoding="utf-8")
    warnings = cdg.check_rule_skill_hygiene(
        doc.splitlines(), doc, skill_md, [self.repo_dir]
    )
    joined = "\n".join(warnings)
    self.assertIn("$MY_BIN", joined)
    self.assertNotIn("$OUT_DIR", joined)
    self.assertNotIn("$NOT_BASH", joined)
    self.assertIn("a|b", joined)
    self.assertNotIn("id\\|name", joined)
    self.assertEqual(len(warnings), 2, warnings)


if __name__ == "__main__":
  unittest.main()
