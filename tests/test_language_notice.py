#!/usr/bin/env python3
"""Tests for the reply-language reminder in context_guard."""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))

import context_guard


class TestLanguageNotice(unittest.TestCase):

  def test_user_messages(self):
    for text, lang in (("Вроде сделала", "ru"), ("ну почему опять по-английски?", "ru"),
                       ("глянь\nTraceback (most recent call last): KeyError 'x' " * 3, "ru"),
                       ("Please answer in English", "other"), ("ok", "other")):
      self.assertEqual(context_guard.detect_user_language(text), lang, text)

  def test_wakeups_and_auto_prompts_keep_the_recorded_language(self):
    for text in ("", "<SYSTEM_MESSAGE>\n[Message] task-25 finished\n</SYSTEM_MESSAGE>",
                 "Continuing unfinished work from previous conversation (conversation://x).",
                 "Starting R48 in a fresh chat.\n\nThis prompt is auto-generated and does not set "
                 "the chat language: talk to the user in Russian."):
      self.assertEqual(context_guard.detect_user_language(text), "", text)

  def test_notice_for_russian_and_fresh_continuations_only(self):
    self.assertIn("[LANGUAGE]", context_guard.build_language_notice("ru"))
    self.assertIn("[LANGUAGE]", context_guard.build_language_notice(""))
    self.assertEqual(context_guard.build_language_notice("other"), "")


if __name__ == "__main__":
  unittest.main()
