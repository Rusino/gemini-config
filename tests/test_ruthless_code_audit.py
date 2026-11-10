#!/usr/bin/env python3
"""Unit tests for the ruthless-code-audit skill script."""

import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "skills"
    / "ruthless-code-audit"
    / "scripts"
    / "ruthless_audit.py"
)

spec = importlib.util.spec_from_file_location("ruthless_audit", SCRIPT_PATH)
ruthless_audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ruthless_audit)


class TestRuthlessCodeAudit(unittest.TestCase):

  def setUp(self):
    self.tmp_dir = tempfile.TemporaryDirectory()
    self.root = pathlib.Path(self.tmp_dir.name)

  def tearDown(self):
    self.tmp_dir.cleanup()

  def _write(self, rel_path: str, content: str) -> pathlib.Path:
    full = self.root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full

  def test_exact_file_duplicates_and_builtin_block_clones(self):
    shared_body = "\n".join([
        "package sample",
        "",
        "func computeScore(items []int, weight int) int {",
        "  total := 0",
        "  for idx, val := range items {",
        "    if val > 10 {",
        "      total += val * weight + idx",
        "    } else if val > 5 {",
        "      total += val + weight",
        "    } else {",
        "      total -= 1",
        "    }",
        "  }",
        "  return total",
        "}",
        "",
        "func extraHelper(a int) int {",
        "  return a * 2",
        "}",
    ])
    self._write("pkg_a/dup1.go", shared_body)
    self._write("pkg_b/dup2.go", shared_body)

    # Parameterized clone with renamed local variables and different literals
    clone_c = "\n".join([
        "package sample",
        "",
        "func evaluateMetric(values []int, factor int) int {",
        "  acc := 0",
        "  for i, v := range values {",
        "    if v > 99 {",
        "      acc += v * factor + i",
        "    } else if v > 42 {",
        "      acc += v + factor",
        "    } else {",
        "      acc -= 3",
        "    }",
        "  }",
        "  return acc",
        "}",
    ])
    self._write("pkg_c/clone.go", clone_c)

    raw_files = ruthless_audit.collect_files([self.root], self.root)
    records = [
        (p, rel, p.read_text(encoding="utf-8"), p.read_text(encoding="utf-8").splitlines())
        for p, rel in raw_files
    ]

    exact = ruthless_audit.detect_exact_file_duplicates(records)
    self.assertEqual(len(exact), 1)
    self.assertEqual(sorted(exact[0]["locations"]), ["pkg_a/dup1.go", "pkg_b/dup2.go"])

    clones = ruthless_audit.run_builtin_block_clone_detector(records, min_lines=8, min_tokens=25)
    self.assertTrue(len(clones) >= 1)
    all_clone_locs = " ".join(loc for c in clones for loc in c["locations"])
    self.assertIn("pkg_c/clone.go", all_clone_locs)

    # Symlinked scan root (simulating macOS /var -> /private/var)
    with tempfile.TemporaryDirectory() as link_parent:
      symlink_root = pathlib.Path(link_parent) / "alias_root"
      symlink_root.symlink_to(self.root)
      sym_files = ruthless_audit.collect_files([symlink_root], symlink_root)
      self.assertEqual(
          sorted(rel for _, rel in sym_files),
          ["pkg_a/dup1.go", "pkg_b/dup2.go", "pkg_c/clone.go"],
      )

  def test_dead_and_test_only_functions(self):
    prod_code = "\n".join([
        "package engine",
        "",
        "func ActiveEntry() int {",
        "  return usedHelper(4)",
        "}",
        "",
        "func usedHelper(x int) int {",
        "  return x + 1",
        "}",
        "",
        "func completelyDeadHelper(x int) int {",
        "  return x * 99",
        "}",
        "",
        "func onlyCalledInTestHelper(x int) int {",
        "  return x - 1",
        "}",
    ])
    test_code = "\n".join([
        "package engine",
        "",
        "func TestEngine(t *testing.T) {",
        "  _ = ActiveEntry()",
        "  _ = onlyCalledInTestHelper(10)",
        "}",
    ])
    self._write("engine.go", prod_code)
    self._write("engine_test.go", test_code)

    raw_files = ruthless_audit.collect_files([self.root], self.root)
    records = [
        (p, rel, p.read_text(encoding="utf-8"), p.read_text(encoding="utf-8").splitlines())
        for p, rel in raw_files
    ]
    struct = ruthless_audit.analyze_functions_and_dead_code(records, max_func_lines=50, max_file_lines=500)
    dead_names = [fn["name"] for fn in struct["dead_functions"]]
    test_only_names = [fn["name"] for fn in struct["test_only_functions"]]

    self.assertIn("completelyDeadHelper", dead_names)
    self.assertNotIn("usedHelper", dead_names)
    self.assertIn("onlyCalledInTestHelper", test_only_names)

  def test_python_hash_comments_do_not_hide_callers_or_glob_strings(self):
    py_code = "\n".join([
        "def caller_fn(root):",
        "  # C-style /* inside python comment",
        "  files = root.glob('**/*.py')",
        "  return helper_after_glob(files)",
        "",
        "def helper_after_glob(items):",
        "  # Closing */ inside python comment",
        "  return list(items)",
        "",
        "# def commented_out_call(): return dead_py_fn()",
        "def dead_py_fn():",
        "  return 42",
    ])
    self._write("mod.py", py_code)
    raw_files = ruthless_audit.collect_files([self.root], self.root)
    records = [
        (p, rel, p.read_text(encoding="utf-8"), p.read_text(encoding="utf-8").splitlines())
        for p, rel in raw_files
    ]
    struct = ruthless_audit.analyze_functions_and_dead_code(
        records, max_func_lines=50, max_file_lines=500
    )
    dead_names = [fn["name"] for fn in struct["dead_functions"]]
    self.assertNotIn("helper_after_glob", dead_names)
    self.assertIn("dead_py_fn", dead_names)

  def test_check_doc_grounding_bare_paths_and_rule_hygiene(self):
    grounding_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "skills"
        / "amnesia-doc-check"
        / "scripts"
        / "check_doc_grounding.py"
    )
    gspec = importlib.util.spec_from_file_location("check_doc_grounding", grounding_path)
    cdg = importlib.util.module_from_spec(gspec)
    gspec.loader.exec_module(cdg)

    self._write("rules/first.md", "# Shared Heading\nBody 1\n")
    self._write("rules/second.md", "# Shared Heading\nBody 2\n")
    skill_doc = self._write(
        "skills/demo-skill/SKILL.md",
        "---\nname: demo-skill\ndescription: " + ("x" * 460) + "\n---\n"
        "See `skills/demo-skill/SKILL.md` and bare arithmetic 125/480 or C++/Dart/TS.\n",
    )
    out = io.StringIO()
    with (
        unittest.mock.patch.object(
            sys,
            "argv",
            [
                "check_doc_grounding.py",
                "--repo",
                str(self.root),
                str(skill_doc),
                str(self.root / "rules/first.md"),
            ],
        ),
        unittest.mock.patch("sys.stdout", out),
    ):
      rc = cdg.main()
    printed = out.getvalue()
    # Section [8] hygiene warnings increment total_issues so main() returns exit code 1
    self.assertEqual(rc, 1, printed)
    self.assertIn("Unresolved / Missing Paths (0 missing, 1 verified)", printed)
    self.assertIn("460 chars (keep <= 450 chars", printed)
    self.assertIn("Heading `Shared Heading` duplicates section in `rules/second.md`", printed)

  def test_keeper_invariants_and_test_horrors(self):
    cpp_prod = "\n".join([
        "#pragma GCC diagnostic ignored \"-Wunused-parameter\"",
        "// TODO: fix this later",
        "// TODO(KEEPER-DEBT: Invariant-3): remove fallback after migration",
        "// TODO(jlavrova): unify UTF-8/UTF-16 index mapping",
        "void layoutParagraph(Node* ptr) {",
        "  if (!ptr) { return; }",
        "  auto* buf = new FooBuffer();",
        "  while (true) {",
        "    doWork(buf);",
        "  }",
        "  const char* p = \"/usr/local/google/home/jlavrova/secret\";",
        "  const char* ru = \"Привет\";",
        "  const char* ep = \"/api/autorollers\";",
        "}",
    ])
    go_proxy = "\n".join([
        "package main",
        "func proxy() {",
        "  fmt.Println(\"DEBUG: temporary trace\")",
        "  _ = \"/api/autorollers\"",
        "}",
    ])
    cpp_test = "\n".join([
        "void TestParagraph(skiatest::Reporter* r) {",
        "  std::this_thread::sleep_for(std::chrono::milliseconds(50));",
        "  if (Probability != 89) {}",
        "  // Test that width is positive",
        "  REPORTER_ASSERT(r, width > 0);",
        "}",
        "DEF_TEST(FontPinBad, r) {",
        "  sk_sp<SkFontMgr> mgr = ToolUtils::TestFontMgr();",
        "  REPORTER_ASSERT(r, glyph.width() == 4071 && glyph.height() == 65527);",
        "}",
        "DEF_TEST(FontPinGuarded, r) {",
        "  sk_sp<SkFontMgr> mgr = SkFontMgr_New_FreeType(nullptr);",
        "  REPORTER_ASSERT(r, glyph.width() == 4071 && glyph.height() == 65527);",
        "}",
    ])
    self._write("src/layout.cpp", cpp_prod)
    self._write("overlay/proxy.go", go_proxy)
    self._write("tests/layout_test.cpp", cpp_test)

    raw_files = ruthless_audit.collect_files([self.root], self.root)
    records = [
        (p, rel, p.read_text(encoding="utf-8"), p.read_text(encoding="utf-8").splitlines())
        for p, rel in raw_files
    ]
    horrors = ruthless_audit.scan_horrors_and_invariants(records, allow_cyrillic=False)

    self.assertEqual(len(horrors["warning_suppressions"]), 1)
    self.assertEqual(len(horrors["anonymous_debt"]), 1)
    self.assertEqual(len(horrors["defensive_null_returns"]), 1)
    self.assertEqual(len(horrors["hot_path_allocations"]), 1)
    self.assertEqual(len(horrors["unbounded_loops"]), 1)
    self.assertEqual(len(horrors["hardcoded_paths"]), 1)
    self.assertEqual(len(horrors["cyrillic_strings"]), 1)
    self.assertEqual(len(horrors["debug_leftovers"]), 1)
    self.assertEqual(len(horrors["brain_split_endpoints"]), 1)
    self.assertEqual(len(horrors["test_sleeps"]), 1)
    self.assertEqual(len(horrors["test_magic_pins"]), 1)
    self.assertEqual(len(horrors["test_narrative_headers"]), 1)
    self.assertEqual(len(horrors["test_font_metric_pins"]), 1)

  def test_cli_json_and_markdown_execution(self):
    self._write("a.py", "def unused_fn():\n  return 1\n")
    res = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            str(self.root),
            "--repo",
            str(self.root),
            "--force-builtin-cpd",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(res.stdout)
    self.assertEqual(payload["summary"]["total_files"], 1)
    self.assertEqual(payload["summary"]["clone_backend"], "builtin-winnowing")

  # Verifies cross-language unused explicit import detection (Python, TS/JS, Dart) and safe skips
  def test_cross_language_unused_imports_detection(self):
    py_file = "\n".join([
        "from __future__ import annotations",
        "import os",
        "import sys  # keep: side-effect",
        "from typing import TYPE_CHECKING, Optional",
        "if TYPE_CHECKING:",
        "  from pkg.models import UsedModel, UnusedModel",
        "",
        "def run(val: UsedModel) -> int:",
        "  try:",
        "    import optional_dep",
        "  except ImportError:",
        "    pass",
        "  return 1",
    ])
    compat_py = "\n".join([
        "import sys",
        "if sys.version_info >= (3, 11):",
        "  import tomllib",
        "else:",
        "  import tomli as tomllib",
        "PLATFORM = sys.platform",
    ])
    ts_file = "\n".join([
        "import React from 'react';",
        (
            "import DefaultUnused, { usedFn, unusedFn as aliasUnused,"
            " type UsedT, type UnusedT } from './m';"
        ),
        "import * as usedNs from './ns';",
        "import * as unusedNs from './dead_ns';",
        "import { $, user$ } from './signals';",
        "import './side_effect_only.css';",
        "export function Widget(x: UsedT): number {",
        "  const endpoint = 'https://example.com/' + usedFn(usedNs.val, x) + $(user$.val);",
        "  return endpoint.length;",
        "}",
    ])
    ts_semiless = "\n".join([
        "import { helper } from './helper'",
        "import './styles.css'",
        "import DeadDefault from './dead'",
        "export function run(): number {",
        "  return helper()",
        "}",
        "export { PublicType } from './types'",
    ])
    dart_file = "\n".join([
        "import 'dart:math' as math;",
        "import 'dart:developer' as unusedDev;",
        "import 'package:ui/ui.dart'",
        "    show",
        "        Offset,",
        "        RRect,",
        "        RSTransform,",
        "        Radius,",
        "        Rect,",
        "        Shader,",
        "        Shadow,",
        "        Size,",
        "        Tangent,",
        "        UnusedTen;",
        "class Box {",
        "  Object all(RRect a, RSTransform b, Radius c, Rect d, Shader e, Shadow f, Tangent g) =>",
        "      Offset(math.max(0.0, 1.0), 0.0);",
        "}",
    ])
    dart_barrel = "export 'src/a.dart';\nimport 'dart:ui' as ui;\n"
    dart_part_owner = "library owner;\nimport 'dart:math' as math;\npart 'owner_part.dart';\nclass Owner {}\n"

    self._write("pkg/__init__.py", "from .sub import ReExported\n")
    self._write("app.py", py_file)
    self._write("compat.py", compat_py)
    self._write("ui/widget.tsx", ts_file)
    self._write("ui/semiless.ts", ts_semiless)
    self._write("lib/box.dart", dart_file)
    self._write("lib/barrel.dart", dart_barrel)
    self._write("lib/owner.dart", dart_part_owner)

    raw_files = ruthless_audit.collect_files([self.root], self.root)
    records = [
        (p, rel, p.read_text(encoding="utf-8"), p.read_text(encoding="utf-8").splitlines())
        for p, rel in raw_files
    ]
    horrors = ruthless_audit.scan_horrors_and_invariants(records, allow_cyrillic=False)
    unused = {f"{loc}:{msg}" for loc, msg in horrors["unused_imports"]}

    self.assertIn("app.py:L2:unused import `os`", unused)
    self.assertIn("app.py:L4:unused import `Optional`", unused)
    self.assertIn("app.py:L6:unused import `UnusedModel`", unused)
    self.assertNotIn("app.py:L3:unused import `sys`", unused)
    self.assertNotIn("app.py:L4:unused import `TYPE_CHECKING`", unused)
    self.assertNotIn("app.py:L6:unused import `UsedModel`", unused)
    self.assertIn("compat.py:L3:unused import `tomllib`", unused)
    self.assertIn("compat.py:L5:unused import `tomllib`", unused)

    self.assertIn("ui/widget.tsx:L2:unused import `DefaultUnused`", unused)
    self.assertIn("ui/widget.tsx:L2:unused import `aliasUnused`", unused)
    self.assertIn("ui/widget.tsx:L2:unused import `UnusedT`", unused)
    self.assertIn("ui/widget.tsx:L4:unused import `unusedNs`", unused)
    for used_sym in ("`React`", "`usedFn`", "`UsedT`", "`usedNs`", "`$`", "`user$`"):
      self.assertFalse(any(used_sym in item for item in unused), used_sym)
    self.assertIn("ui/semiless.ts:L3:unused import `DeadDefault`", unused)
    self.assertNotIn("ui/semiless.ts:L1:unused import `helper`", unused)

    self.assertIn("lib/box.dart:L2:unused import `unusedDev`", unused)
    self.assertIn("lib/box.dart:L3:unused import `Size`", unused)
    self.assertIn("lib/box.dart:L3:unused import `UnusedTen`", unused)
    self.assertFalse(any("barrel.dart" in item for item in unused))
    self.assertFalse(any("owner.dart" in item for item in unused))
    self.assertFalse(any("__init__.py" in item for item in unused))

  # Verifies fuseable consecutive loops vs phase-dependent reduction/early-return loops
  def test_consecutive_loops_and_post_early_exit_tautologies(self):
    cpp_code = "\n".join([
        "void processRuns( Paragraph* para, Node* ptr ) {",
        "  if (!ptr) {",
        "    return;",
        "  }",
        "  int x = 1;",
        "  if (ptr != nullptr) {",
        "    ptr->touch(x);",
        "  }",
        "  for (auto& run : fRuns) {",
        "    run.resetOffsets();",
        "  }",
        "  // Comment between two fuseable passes",
        "  for (auto& run : fRuns) {",
        "    run.computeMetrics();",
        "  }",
        "  float maxAscent = 0.0f;",
        "  for (const auto& run : fBaselineRuns) {",
        "    maxAscent = minVal(maxAscent, run.ascent());",
        "  }",
        "  for (auto& run : fBaselineRuns) {",
        "    run.setBaseline(maxAscent);",
        "  }",
        "  for (auto& run : fScaledRuns) {",
        "    this->fTotal += run.width();",
        "  }",
        "  for (auto& run : fScaledRuns) {",
        "    run.scale(this->fTotal);",
        "  }",
        "  if (fItems.empty()) {",
        "    return;",
        "  }",
        "  pruneInPlace(fItems);",
        "  if (fItems.empty()) {",
        "    return;",
        "  }",
        "#if defined(SK_DEBUG)",
        "  if (!para) {",
        "    return;",
        "  }",
        "#endif",
        "  if (!para) {",
        "    recover();",
        "  }",
        "  if (strict)",
        "    if (!optPtr) return;",
        "  if (!optPtr) {",
        "    fallback();",
        "  }",
        "}",
    ])
    py_code = "\n".join([
        "def handle(items, rows, mut_list):",
        "  if not items:",
        "    return 0",
        "  total = 0",
        "  if items:",
        "    total += 1",
        "  for row in items:",
        "    row.prepare()",
        "  for row in items:",
        "    row.commit()",
        "  counts = {}",
        "  for r in rows:",
        "    counts[r.key] = counts.get(r.key, 0) + 1",
        "  for r in rows:",
        "    r.is_dup = counts[r.key] > 1",
        "  if not mut_list:",
        "    return 0",
        "  del mut_list[:1]",
        "  if not mut_list:",
        "    return 0",
        "  return total",
    ])
    cjs_code = "function f(x) {\n  if (!x) return 0;\n  if (x) { return 1; }\n  return 2;\n}\n"
    self._write("src/runs.cpp", cpp_code)
    self._write("src/handler.py", py_code)
    self._write("src/mod.cjs", cjs_code)

    raw_files = ruthless_audit.collect_files([self.root], self.root)
    records = [
        (p, rel, p.read_text(encoding="utf-8"), p.read_text(encoding="utf-8").splitlines())
        for p, rel in raw_files
    ]
    horrors = ruthless_audit.scan_horrors_and_invariants(records, allow_cyrillic=False)

    loop_locs = [loc for loc, _ in horrors["consecutive_collection_loops"]]
    self.assertEqual(loop_locs, ["src/handler.py:L9", "src/runs.cpp:L13"])

    taut_items = {f"{loc}:{msg}" for loc, msg in horrors["post_early_exit_tautologies"]}
    self.assertEqual(
        taut_items,
        {
            "src/runs.cpp:L6:tautological guard on `ptr` after early exit at L2",
            "src/handler.py:L5:tautological guard on `items` after early exit at L2",
            "src/mod.cjs:L3:tautological guard on `x` after early exit at L2",
        },
    )

  # Verifies R138 4 simplification checks and Zero-Content-Loss anchors across skill docs
  def test_r138_simplification_checks_documented_without_content_loss(self):
    cfg_root = pathlib.Path(__file__).resolve().parents[1]
    hunter_md = (
        cfg_root / "skills" / "amnesia-precommit" / "references" / "hunter_prompt.md"
    ).read_text(encoding="utf-8")
    precommit_md = (
        cfg_root / "skills" / "amnesia-precommit" / "SKILL.md"
    ).read_text(encoding="utf-8")
    ruthless_md = (
        cfg_root / "skills" / "ruthless-code-audit" / "SKILL.md"
    ).read_text(encoding="utf-8")

    for doc_name, txt in (("hunter_prompt.md", hunter_md), ("ruthless/SKILL.md", ruthless_md)):
      self.assertIn("Zero-Base Necessity", txt, doc_name)
      self.assertIn("Deletion Dominance", txt, doc_name)
      self.assertIn("Structural Diff", txt, doc_name)
      self.assertIn("No Round-Trip Property Plumbing", txt, doc_name)
      self.assertIn("Prototype-Scar Elimination", txt, doc_name)
      self.assertIn("COMPACTION: YES", txt, doc_name)
      self.assertIn("COMPACTION: NO", txt, doc_name)

    self.assertIn("Deletion Dominance", precommit_md)
    self.assertIn("stage_diff_context.py", precommit_md)
    self.assertIn("stage_diff_context.py", ruthless_md)
    for m_rule in ("M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9"):
      self.assertIn(m_rule, hunter_md)


if __name__ == "__main__":
  unittest.main()
