#!/usr/bin/env python3
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "../skills/skia-canary-advisor/scripts",
        )
    ),
)
import canary_advisor


class TestCanaryAdvisor(unittest.TestCase):

  def test_load_cl_googler_cr_plus_one_detection(self):
    fake_diff_b64 = "ZGlmZiAtLWdpdCBhL2Zvby5oIGIvZm9vLmgK"
    tests = [
        ("no_labels", {}, False),
        (
            "external_cr_plus_one",
            {
                "Code-Review": {
                    "all": [{"value": 1, "email": "external@author.com"}]
                }
            },
            False,
        ),
        (
            "googler_cr_zero",
            {
                "Code-Review": {
                    "all": [{"value": 0, "email": "googler@google.com"}]
                }
            },
            False,
        ),
        (
            "googler_cr_minus_one",
            {
                "Code-Review": {
                    "all": [{"value": -1, "email": "googler@google.com"}]
                }
            },
            False,
        ),
        (
            "googler_cr_plus_one",
            {
                "Code-Review": {
                    "all": [
                        {"value": 1, "email": "googler@google.com"},
                        {"value": 0, "email": "other@google.com"},
                    ]
                }
            },
            True,
        ),
    ]

    for name, labels, expected in tests:
      with self.subTest(name=name):
        fake_info = {
            "subject": "Test change",
            "owner": {"email": "author@example.com"},
            "revisions": {"rev1": {"_number": 1}},
            "labels": labels,
        }
        with patch.object(canary_advisor, "gerrit_json", return_value=fake_info):
          with patch.object(canary_advisor, "http_get", return_value=fake_diff_b64):
            meta = canary_advisor.load_cl("12345")
            self.assertEqual(meta["googler_cr_plus_one"], expected)

  def test_external_author_defers_canary_g3_without_googler_cr_plus_one(self):
    diff_text = (
        "diff --git a/modules/skparagraph/src/ParagraphImpl.cpp"
        " b/modules/skparagraph/src/ParagraphImpl.cpp\n"
        "--- a/modules/skparagraph/src/ParagraphImpl.cpp\n"
        "+++ b/modules/skparagraph/src/ParagraphImpl.cpp\n"
        "@@ -1,2 +1,3 @@\n"
        "+// some change\n"
    )
    fake_meta = {
        "cl": 1408157,
        "patchset": 1,
        "subject": "SkParagraph change",
        "owner": "external@author.com",
        "diff": diff_text,
        "googler_cr_plus_one": False,
    }

    args = type(
        "Args",
        (),
        {
            "cl": "1408157",
            "repo": None,
            "base": "origin/main",
            "max_symbols": 6,
            "no_search": True,
            "no_health": True,
            "no_log": True,
            "strong_only": False,
        },
    )()

    buf = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=fake_meta):
      with patch("sys.stdout", buf):
        canary_advisor.advise(args)

    out = buf.getvalue()
    self.assertIn("AGENT ACTION (execute this command):", out)
    self.assertIn("Canary-Flutter is disabled", out)
    self.assertIn("NativeFonts_FreeType_ASAN_Upload", out)
    # Canary-G3 must NOT be in the immediate AGENT ACTION
    agent_action_line = [
        line for line in out.splitlines() if line.strip().startswith("git cl try")
    ][0]
    self.assertNotIn("Canary-G3", agent_action_line)
    self.assertNotIn("Canary-Flutter", agent_action_line)
    self.assertIn("Canary-G3 requires a Googler author or Googler Code-Review+1", out)
    self.assertIn("Canary-G3 Command (run after Googler Code-Review+1 approval):", out)
    self.assertIn("follow submit-hold protocol", out)

  def test_external_author_includes_canary_g3_when_googler_cr_plus_one_granted(self):
    diff_text = (
        "diff --git a/modules/skparagraph/src/ParagraphImpl.cpp"
        " b/modules/skparagraph/src/ParagraphImpl.cpp\n"
        "--- a/modules/skparagraph/src/ParagraphImpl.cpp\n"
        "+++ b/modules/skparagraph/src/ParagraphImpl.cpp\n"
        "@@ -1,2 +1,3 @@\n"
        "+// some change\n"
    )
    fake_meta = {
        "cl": 1408157,
        "patchset": 2,
        "subject": "SkParagraph change",
        "owner": "external@author.com",
        "diff": diff_text,
        "googler_cr_plus_one": True,
    }

    args = type(
        "Args",
        (),
        {
            "cl": "1408157",
            "repo": None,
            "base": "origin/main",
            "max_symbols": 6,
            "no_search": True,
            "no_health": True,
            "no_log": True,
            "strong_only": False,
        },
    )()

    buf = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=fake_meta):
      with patch("sys.stdout", buf):
        canary_advisor.advise(args)

    out = buf.getvalue()
    agent_action_line = [
        line for line in out.splitlines() if line.strip().startswith("git cl try")
    ][0]
    self.assertIn("Canary-G3", agent_action_line)
    self.assertNotIn("Canary-Flutter", agent_action_line)
    self.assertIn("run_flutter_paragraph_tests.py --full", out)
    self.assertIn("NativeFonts_FreeType_ASAN_Upload", agent_action_line)
    self.assertNotIn("deferred until a Googler votes Code-Review+1", out)

  def test_font_backed_test_adds_mac_native_fonts_trybot(self):
    diff_text = (
        "diff --git a/tests/SkRemoteGlyphCacheTest.cpp b/tests/SkRemoteGlyphCacheTest.cpp\n"
        "--- a/tests/SkRemoteGlyphCacheTest.cpp\n"
        "+++ b/tests/SkRemoteGlyphCacheTest.cpp\n"
        "@@ -10,2 +10,4 @@\n"
        "+DEF_TEST(SkRemoteGlyphCache_Test, reporter) {\n"
        "+    auto mgr = ToolUtils::TestFontMgr();\n"
        "+}\n"
    )
    fake_meta = {
        "cl": 1404196,
        "patchset": 1,
        "subject": "Add font test",
        "owner": "googler@google.com",
        "diff": diff_text,
        "googler_cr_plus_one": True,
    }
    args = type(
        "Args",
        (),
        {
            "cl": "1404196",
            "repo": None,
            "base": "origin/main",
            "max_symbols": 6,
            "no_search": True,
            "no_health": True,
            "no_log": True,
            "strong_only": False,
        },
    )()

    buf = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=fake_meta):
      with patch("sys.stdout", buf):
        canary_advisor.advise(args)

    out = buf.getvalue()
    self.assertIn("Font-Backend Trybot [ALWAYS_RUN]", out)
    self.assertIn(
        "Test-Mac14-Clang-MacMini8.1-CPU-AVX2-x86_64-Debug-All-NativeFonts_Fontations",
        out,
    )
    agent_action_line = [
        line for line in out.splitlines() if line.strip().startswith("git cl try")
    ][0]
    self.assertIn(
        "Test-Mac14-Clang-MacMini8.1-CPU-AVX2-x86_64-Debug-All-NativeFonts_Fontations",
        agent_action_line,
    )

  def test_http_get_authenticates_gerrit_requests_via_luci_token(self):
    seen_reqs = []

    class FakeResp:
      def __enter__(self):
        return self

      def __exit__(self, *args):
        return False

      def read(self):
        return b")]}'\n{}"

    def fake_urlopen(req, timeout=20):
      seen_reqs.append(req)
      return FakeResp()

    fake_proc = type("Proc", (), {"stdout": "username=git\npassword=tok123\n"})()
    canary_advisor._LUCI_TOKEN_CACHE = ("", 0.0)
    with patch("subprocess.run", return_value=fake_proc):
      with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        body = canary_advisor.http_get(
            "https://skia-review.googlesource.com/changes/1412380"
        )
    self.assertEqual(body, ")]}'\n{}")
    self.assertEqual(
        seen_reqs[0].full_url,
        "https://skia-review.googlesource.com/a/changes/1412380",
    )
    self.assertEqual(seen_reqs[0].get_header("Authorization"), "Bearer tok123")

  def test_cmd_watch_survives_transient_http_429_and_skips_poll_diff(self):
    import urllib.error

    initial_meta = {
        "cl": 1412380,
        "patchset": 3,
        "current_patchset": 3,
        "subject": "Keep glyph offsets only if needed",
        "owner": "googler@google.com",
        "diff": "diff --git a/src/foo.cpp b/src/foo.cpp\n+// x\n",
        "googler_cr_plus_one": True,
    }
    err_429 = urllib.error.HTTPError(
        "https://skia-review.googlesource.com/changes/1412380",
        429,
        "Too Many Requests",
        {},
        None,
    )
    calls = []

    def fake_load_cl(cl, ps=None, include_diff=True):
      calls.append((cl, ps, include_diff))
      if len(calls) == 1:
        return initial_meta
      if len(calls) == 2:
        raise err_429
      return {**initial_meta, "diff": ""}

    args = type(
        "Args",
        (),
        {
            "cl": "1412380",
            "ps": None,
            "poll_sec": 1,
            "timeout_min": 5,
            "save_draft": False,
            "json": False,
        },
    )()
    fake_builds = {"Canary-G3": {"status": "SUCCESS"}}
    buf = io.StringIO()
    with patch.object(canary_advisor, "load_cl", side_effect=fake_load_cl):
      with patch.object(canary_advisor, "bb_cl_builds", return_value=fake_builds):
        with patch.object(canary_advisor, "swarming_tasks_for_cl", return_value=[]):
          with patch.object(canary_advisor, "cmd_triage", return_value={}):
            with patch("time.sleep"):
              with patch("sys.stdout", buf):
                rc = canary_advisor.cmd_watch(args)
    self.assertEqual(rc, 0)
    self.assertEqual(
        calls,
        [
            ("1412380", None, True),
            ("1412380", None, False),
            ("1412380", None, False),
        ],
    )
    self.assertIn("ALL_TRYJOBS_COMPLETE", buf.getvalue())


if __name__ == "__main__":
  unittest.main()
