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
    self.assertTrue(topic.startswith("cl/1404196 b/576664769"))
    self.assertLessEqual(len(topic), hook.MAX_TOPIC_LEN)

  def test_issue_chat_gets_pr(self):
    topic = hook.topic_with_change("[14:16] ⦿ Issue #195102", GITHUB_OUT)
    self.assertEqual(topic, "PR #195200 Issue #195102")

  def test_review_chat_keeps_reviewed_cl(self):
    self.assertEqual(
        hook.topic_with_change("[10:51] ⦿ cl/1403456 computeCodeUnitFlags", GERRIT_OUT),
        "",
    )

  def test_no_url_leaves_title(self):
    self.assertEqual(hook.topic_with_change("[14:16] ⦿ Issue #195102", "error"), "")


if __name__ == "__main__":
  unittest.main()
