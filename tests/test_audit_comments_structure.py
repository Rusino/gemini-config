#!/usr/bin/env python3
"""Tests for the C++/Dart structure checks in audit_comments.py."""

import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "skills", "code-comments", "scripts"))
if SCRIPT_DIR not in sys.path:
  sys.path.insert(0, SCRIPT_DIR)

import audit_comments  # pylint: disable=g-import-not-at-top

WRAPPED_HEADER = """\
// Copyright 2026 Google LLC
#ifndef Map_DEFINED
#define Map_DEFINED

#include "include/core/SkTypes.h"

#if defined(SK_PARAGRAPH_NATIVE_CORETEXT)
#include <CoreFoundation/CoreFoundation.h>

// Maps offsets
class Map {
public:
    Map();
    explicit Map(int n);
    ~Map();
    Map& operator=(const Map&) = default;
    void reset(int n);
    // Converts an index
    size_t toUtf16Index(size_t utf8Index) const;
    // Calls back per code point
    template <typename OnCodepoint>
    void visit(OnCodepoint&& f);
    int size() const override;
    // Statements in inline bodies are not declarations
    int inlined() const {
        int local(3);
        return local;
    }
};

template <typename OnCodepoint>
void Map::visit(OnCodepoint&& f) {
    f(1);
}

#endif  // defined(SK_PARAGRAPH_NATIVE_CORETEXT)

#endif  // Map_DEFINED
"""

BUILD_SELECTED_HEADER = """\
// Copyright 2026 Google LLC
#ifndef Map_DEFINED
#define Map_DEFINED

#include "include/core/SkTypes.h"

#if defined(SK_DEBUG)
#define MAP_TRACE 1
#endif

// Maps offsets
class Map {};

#endif  // Map_DEFINED
"""


def _ids(issues):
  return sorted((span, msg.split(":", 1)[0]) for span, msg in issues)


def _all(text):
  lines = text.splitlines()
  return lines, set(range(1, len(lines) + 1))


class AuditCppStructureTest(unittest.TestCase):

  # Whole-file SK_ wrappers fire once on a new file, with every finding in the same pass
  def test_wrapped_header_findings(self):
    lines, added = _all(WRAPPED_HEADER)
    issues = audit_comments.audit_cpp_structure("src/Map.h", lines, added, True)
    self.assertEqual(_ids(issues), [
        ("L1", "WHOLE_FILE_IFDEF"),
        ("L17", "FUNC_NO_CONTRACT"),
        ("L31", "TEMPLATE_NO_WHY"),
    ])
    self.assertIn("'reset'", dict(issues)["L17"])

  # A file that existed before keeps its #if layout; only new files are checked
  def test_whole_file_ifdef_only_for_new_files(self):
    lines, added = _all(WRAPPED_HEADER)
    issues = audit_comments.audit_cpp_structure("src/Map.h", lines, added, False)
    self.assertNotIn("WHOLE_FILE_IFDEF", str(issues))

  # A local #if block that does not wrap the rest of the file is a call-site choice
  def test_partial_ifdef_is_not_whole_file(self):
    lines, added = _all(BUILD_SELECTED_HEADER)
    self.assertEqual(audit_comments.audit_cpp_structure("src/Map.h", lines, added, True), [])
    cpp = "#include \"Map.h\"\n\n#if defined(SK_X)\nint a = 1;\n#endif\n\nint b = 2;\n"
    lines, added = _all(cpp)
    self.assertEqual(audit_comments.audit_cpp_structure("src/Map.cpp", lines, added, True), [])

  # Wrapped .cpp files without an include guard fire too
  def test_wrapped_cpp_fires(self):
    cpp = "// Copyright\n#include \"Map.h\"\n\n#if defined(SK_X)\n\nint a = 1;\n\n#endif  // SK_X\n"
    lines, added = _all(cpp)
    issues = audit_comments.audit_cpp_structure("src/Map.cpp", lines, added, True)
    self.assertEqual(_ids(issues), [("L1", "WHOLE_FILE_IFDEF")])

  # Debug-only and legacy-API staging files are wrapped for another reason than backend choice;
  # a backend macro mixed into the same condition still fires
  def test_debug_and_legacy_wrappers_are_not_backend_selection(self):
    for cond, fires in (("#ifdef SK_DEBUG", False), ("#if defined(SK_RELEASE)", False),
                        ("#if defined(SK_SUPPORT_LEGACY_FOO)", False),
                        ("#if defined(SK_DEBUG) && defined(SK_PARAGRAPH_NATIVE_CORETEXT)", True)):
      cpp = "#include \"Map.h\"\n\n" + cond + "\nint a = 1;\n#endif\n"
      lines, added = _all(cpp)
      issues = audit_comments.audit_cpp_structure("src/Map.cpp", lines, added, True)
      self.assertEqual("WHOLE_FILE_IFDEF" in str(issues), fires, cond)

  # Only added lines count, so untouched declarations in an edited header stay silent
  def test_only_added_lines_are_checked(self):
    lines, _ = _all(WRAPPED_HEADER)
    issues = audit_comments.audit_cpp_structure("src/Map.h", lines, {19}, False)
    self.assertEqual(issues, [])

  # Doxygen blocks whose */ closes on a prose line still count as the contract comment, and
  # code examples inside a block comment are not declarations
  def test_block_comments_count_as_contracts(self):
    h = ("class Map {\npublic:\n    /** Resets the map.\n        Keeps capacity. */\n"
         "    void reset(int n);\n    /** Example:\n        void onDraw(SkCanvas* c) {\n"
         "        }\n    */\n    // Size\n    int size() const;\n};\n")
    lines, added = _all(h)
    self.assertEqual(audit_comments.audit_cpp_structure("src/Map.h", lines, added, True), [])

  # A one-line template declaration is reported once, as a template
  def test_one_line_template_reported_once(self):
    lines, added = _all("class Map {\npublic:\n    template <class T> T* make();\n};\n")
    issues = audit_comments.audit_cpp_structure("src/Map.h", lines, added, True)
    self.assertEqual(_ids(issues), [("L3", "TEMPLATE_NO_WHY")])

  # Test macros need a header comment in C++ and Dart test files; .cpp skips FUNC_NO_CONTRACT
  def test_test_headers(self):
    cpp = ("#include \"tests/Test.h\"\n\nDEF_TEST(A, r) {\n}\n\n"
           "// Checks the B edge\nDEF_TEST(B, r) {\n}\nUNIX_ONLY_TEST(C, r) {\n}\n")
    lines, added = _all(cpp)
    issues = audit_comments.audit_cpp_structure("tests/ATest.cpp", lines, added, True)
    self.assertEqual(_ids(issues), [("L3", "TEST_NO_HEADER"), ("L9", "TEST_NO_HEADER")])
    dart = ("void main() {\n  test('a', () {});\n  // Checks b.\n"
            "  testWidgets('b', (t) async {});\n}\n")
    lines, added = _all(dart)
    issues = audit_comments.audit_cpp_structure("test/a_test.dart", lines, added, True)
    self.assertEqual(_ids(issues), [("L2", "TEST_NO_HEADER")])
    issues = audit_comments.audit_cpp_structure("lib/a.dart", lines, added, True)
    self.assertEqual(issues, [])


class AuditCommentsCliTest(unittest.TestCase):

  def setUp(self):
    self._tmp = tempfile.TemporaryDirectory()
    self.repo = self._tmp.name
    subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
    self.git("config", "user.email", "t@example.com")
    self.git("config", "user.name", "Test")
    self.write("README", "x\n")
    self.git("commit", "-q", "-m", "base")

  def tearDown(self):
    self._tmp.cleanup()

  def git(self, *args):
    return subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True,
                          text=True).stdout.strip()

  def write(self, rel, text):
    path = os.path.join(self.repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
      f.write(text)
    self.git("add", rel)

  def run_audit(self, *args):
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, "audit_comments.py"), "--repo", self.repo,
         "--no-cl-hygiene", *args],
        capture_output=True, text=True, check=False,
    )

  # The precommit path (--diff HEAD on a staged new file) blocks on the structure checks
  def test_diff_head_blocks_on_structure_findings(self):
    self.write("src/Map.h", WRAPPED_HEADER)
    res = self.run_audit("--diff", "HEAD")
    self.assertEqual(res.returncode, 1, res.stdout)
    self.assertIn("[ISSUE] src/Map.h:L1: WHOLE_FILE_IFDEF", res.stdout)
    self.assertIn("[ISSUE] src/Map.h:L31: TEMPLATE_NO_WHY", res.stdout)

  # --commit reads a past commit without checkout, even after the tree moved on
  def test_commit_mode_reads_blobs_from_revision(self):
    self.write("src/Map.h", WRAPPED_HEADER)
    self.git("commit", "-q", "-m", "add map")
    sha = self.git("rev-parse", "HEAD")
    self.write("src/Map.h", BUILD_SELECTED_HEADER)
    self.git("commit", "-q", "-m", "build-level selection")
    res = self.run_audit("--commit", sha)
    self.assertEqual(res.returncode, 1, res.stdout)
    self.assertIn("src/Map.h:L17: FUNC_NO_CONTRACT", res.stdout)
    self.assertIn("src/Map.h:L1: WHOLE_FILE_IFDEF", res.stdout)
    res = self.run_audit("--commit", "HEAD")
    self.assertNotIn("WHOLE_FILE_IFDEF", res.stdout)


if __name__ == "__main__":
  unittest.main()
