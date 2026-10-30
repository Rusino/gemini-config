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
    self.assertEqual(rc, 0, printed)
    self.assertIn("Unresolved / Missing Paths (0 missing, 1 verified)", printed)
    self.assertIn("460 chars (keep <= 450 chars", printed)
    self.assertIn("Heading `Shared Heading` duplicates section in `rules/second.md`", printed)

  def test_keeper_invariants_and_test_horrors(self):
    cpp_prod = "\n".join([
        "#pragma GCC diagnostic ignored \"-Wunused-parameter\"",
        "// TODO: fix this later",
        "// TODO(KEEPER-DEBT: Invariant-3): remove fallback after migration",
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


if __name__ == "__main__":
  unittest.main()
