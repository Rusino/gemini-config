#!/usr/bin/env python3
"""Tests for amnesia-precommit/scripts/verify_test_liveness.py on the CL 1404316 incident."""

import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "skills", "amnesia-precommit", "scripts",
    "verify_test_liveness.py",
))

VACUOUS_PARITY = """
DEF_TEST(FontMgrAndroid_LazyParity_FreeType, reporter) {
    sk_sp<SkFontMgr> eager = SkFontMgr_New_Android(nullptr, Scanner(), kEager);
    sk_sp<SkFontMgr> lazy = SkFontMgr_New_Android(nullptr, Scanner(), kLazy);
    REPORTER_ASSERT(reporter, eager->countFamilies() == lazy->countFamilies());
    for (int i = 0; i < eager->countFamilies(); ++i) {
        REPORTER_ASSERT(reporter, SameStyles(eager.get(), lazy.get(), i));
    }
}
"""

LIVE_PARITY = """
DEF_TEST(FontMgrAndroid_LazyParity_FreeType, reporter) {
    SkFontMgr_Android_CustomFonts custom = ResourceFonts("fonts/fonts.xml");
    sk_sp<SkFontMgr> eager = SkFontMgr_New_Android(&custom, Scanner(), kEager);
    sk_sp<SkFontMgr> lazy = SkFontMgr_New_Android(&custom, Scanner(), kLazy);
    REPORTER_ASSERT(reporter, eager->countFamilies() > 0);
    for (int i = 0; i < eager->countFamilies(); ++i) {
        REPORTER_ASSERT(reporter, SameStyles(eager.get(), lazy.get(), i));
    }
}
"""

INCIDENT_LOG = "\n".join(
    ["'/system/etc/fonts.xml' could not be opened."] * 8
    + ["0 srcs * 0 sinks + 8 tests == 8 tasks", "Finished!"]
)

LIVE_LOG = (
    "start unit test FontMgrAndroid_LazyParity_FreeType\n"
    "start unit test FontScanner_FreeType_ScanStyleMatchesMakeFromStream\n"
    "0 srcs * 0 sinks + 2 tests == 2 tasks\nFinished!\n"
)


class TestVerifyTestLiveness(unittest.TestCase):

  def make_repo(self, tmp, parity_body, register_scanner_test):
    os.makedirs(os.path.join(tmp, "tests"))
    os.makedirs(os.path.join(tmp, "gn"))
    gni = ['"$_tests/FontMgrAndroidParserTest.cpp",']
    if register_scanner_test:
      gni.append('"$_tests/FontScannerStyleTest.cpp",')
    with open(os.path.join(tmp, "gn", "tests.gni"), "w") as f:
      f.write("tests_sources = [\n" + "\n".join(gni) + "\n]\n")
    with open(os.path.join(tmp, "tests", "FontMgrAndroidParserTest.cpp"), "w") as f:
      f.write(parity_body)
    with open(os.path.join(tmp, "tests", "FontScannerStyleTest.cpp"), "w") as f:
      f.write("DEF_TEST(FontScanner_FreeType_ScanStyleMatchesMakeFromStream, r) {\n"
              "    REPORTER_ASSERT(r, true);\n}\n")
    subprocess.run(["git", "init", "-q", tmp], check=True)
    subprocess.run(["git", "-C", tmp, "add", "-A"], check=True)

  def run_script(self, tmp, test_file, name, log_text):
    log = os.path.join(tmp, "dm.log")
    with open(log, "w") as f:
      f.write(log_text)
    return subprocess.run(
        [sys.executable, SCRIPT, "--repo", tmp, "--test-file", test_file,
         "--test-name", name, "--log", log],
        capture_output=True, text=True, check=False,
    )

  def test_incident_is_caught(self):
    with tempfile.TemporaryDirectory() as tmp:
      self.make_repo(tmp, VACUOUS_PARITY, register_scanner_test=False)
      parity = self.run_script(
          tmp, "tests/FontMgrAndroidParserTest.cpp", "FontMgrAndroid_LazyParity_FreeType",
          INCIDENT_LOG,
      )
      self.assertEqual(parity.returncode, 1)
      for tag in ("[NOT RUN]", "[UNEXPLAINED LOG WARNINGS]", "[MAYBE VACUOUS]",
                  "[HOST-EMPTY ENV]"):
        self.assertIn(tag, parity.stdout)
      scanner = self.run_script(
          tmp, "tests/FontScannerStyleTest.cpp",
          "FontScanner_FreeType_ScanStyleMatchesMakeFromStream", INCIDENT_LOG,
      )
      self.assertEqual(scanner.returncode, 1)
      self.assertIn("[NOT BUILT]", scanner.stdout)
      self.assertIn("[NOT RUN]", scanner.stdout)

  def test_fixed_version_passes(self):
    with tempfile.TemporaryDirectory() as tmp:
      self.make_repo(tmp, LIVE_PARITY, register_scanner_test=True)
      for test_file, name in (
          ("tests/FontMgrAndroidParserTest.cpp", "FontMgrAndroid_LazyParity_FreeType"),
          ("tests/FontScannerStyleTest.cpp",
           "FontScanner_FreeType_ScanStyleMatchesMakeFromStream"),
      ):
        res = self.run_script(tmp, test_file, name, LIVE_LOG)
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("mutation", res.stdout)


if __name__ == "__main__":
  unittest.main()
