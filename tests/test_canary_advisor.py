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
    self.assertIn("Canary-Flutter", out)
    self.assertIn("NativeFonts_FreeType_ASAN_Upload", out)
    # Canary-G3 must NOT be in the immediate AGENT ACTION
    agent_action_line = [
        line for line in out.splitlines() if line.strip().startswith("git cl try")
    ][0]
    self.assertNotIn("Canary-G3", agent_action_line)
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
    self.assertIn("Canary-Flutter", agent_action_line)
    self.assertIn("NativeFonts_FreeType_ASAN_Upload", agent_action_line)
    self.assertNotIn("deferred until a Googler votes Code-Review+1", out)


if __name__ == "__main__":
  unittest.main()
