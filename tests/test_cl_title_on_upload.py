#!/usr/bin/env python3
"""Contract tests for the hook that puts a new CL or PR into the chat title."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks")
)

import cl_title_on_upload as hook  # pylint: disable=g-import-not-at-top

GERRIT_OUT = (
    "remote: SUCCESS\nremote:   "
    "https://skia-review.googlesource.com/c/skia/+/1404196 Fix overflow [NEW]\n"
)
GITHUB_OUT = "https://github.com/flutter/flutter/pull/195200\n"


class TopicWithChangeTest(unittest.TestCase):

  def test_bug_chat_gets_cl_and_keeps_bug(self):
    topic = hook.topic_with_change(
        "[16:04] ⦿ b/576664769 SkDistanceFieldGen overflow", GERRIT_OUT
    )
    self.assertTrue(topic.startswith("Gerrit 1404196 b/576664769"))
    self.assertLessEqual(len(topic), hook.MAX_TOPIC_LEN)

  def test_chromium_gerrit_upload_uses_crrev(self):
    cr_out = (
        "remote:   https://chromium-review.googlesource.com/c/chromium/src/+/8663938"
        " Extract ShapedText [NEW]\n"
    )
    topic = hook.topic_with_change("[16:04] ⦿ crbug/466929543 font order", cr_out)
    self.assertTrue(topic.startswith("crrev/c/8663938 crbug/466929543"))
    self.assertLessEqual(len(topic), hook.MAX_TOPIC_LEN)
    dart_out = (
        "remote:   https://dart-review.googlesource.com/c/sdk/+/395123"
        " Fix UTF-16 range [NEW]\n"
    )
    dart_topic = hook.topic_with_change("[16:04] ⦿ Dart SDK UTF-16 range", dart_out)
    self.assertTrue(dart_topic.startswith("Gerrit 395123 Dart SDK UTF-16"))
    self.assertLessEqual(len(dart_topic), hook.MAX_TOPIC_LEN)

  def test_issue_chat_gets_pr(self):
    topic = hook.topic_with_change("[14:16] ⦿ Issue #195102", GITHUB_OUT)
    self.assertEqual(topic, "PR #195200 Issue #195102")

  def test_review_chat_keeps_reviewed_cl(self):
    self.assertEqual(
        hook.topic_with_change("[10:51] ⦿ Gerrit 1403456 computeCodeUnitFlags", GERRIT_OUT),
        "",
    )
    self.assertEqual(
        hook.topic_with_change("[10:51] ⦿ cl/1403456 computeCodeUnitFlags", GERRIT_OUT),
        "",
    )

  def test_no_url_leaves_title(self):
    self.assertEqual(hook.topic_with_change("[14:16] ⦿ Issue #195102", "error"), "")


if __name__ == "__main__":
  unittest.main()
