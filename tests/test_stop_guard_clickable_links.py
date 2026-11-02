#!/usr/bin/env python3
"""Contract tests for clickable entity links enforcement in stop_guard.py."""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks"))
)

import stop_guard


class TestStopGuardClickableLinks(unittest.TestCase):

  def setUp(self):
    self._patch_dossier = patch("stop_guard.spawn_background_dossier_update")
    self._patch_autoship = patch(
        "stop_guard.auto_ship_guard_reason", return_value=("", False)
    )
    self._patch_dossier.start()
    self._patch_autoship.start()

  def tearDown(self):
    self._patch_autoship.stop()
    self._patch_dossier.stop()

  def _run_reply(self, tmp_dir: str, reply_text: str, user_idx: int = 1) -> dict:
    transcript = os.path.join(tmp_dir, "transcript.jsonl")
    steps = [
        {"step_index": user_idx, "type": "USER_INPUT", "content": "Статус ревью"},
        {
            "step_index": user_idx + 1,
            "type": "PLANNER_RESPONSE",
            "content": reply_text,
        },
    ]
    with open(transcript, "w", encoding="utf-8") as f:
      for s in steps:
        f.write(json.dumps(s, ensure_ascii=False) + "\n")
    data = {
        "conversationId": "c-links",
        "transcriptPath": transcript,
        "artifactDirectoryPath": tmp_dir,
        "fullyIdle": True,
        "terminationReason": "NO_TOOL_CALL",
    }
    with (
        patch("sys.stdin", io.StringIO(json.dumps(data))),
        patch("sys.stdout", new_callable=io.StringIO) as mock_out,
        patch.object(stop_guard, "play_stop_sound"),
    ):
      stop_guard.main()
    return json.loads(mock_out.getvalue())

  def test_bare_entity_mentions_block_stop_once_with_hint(self):
    bad_replies = [
        "Я положила реплай в черновики CL на PS1 (`77640dee_0908b885`).",
        "Проверила Gerrit 1414016, все тесты прошли.",
        "В CL 1414016 всё чисто.",
        "Загрузила фикс в cl/1414016.",
        "Нужен ребейз для PR #194980.",
        "Это воспроизводится в Issue #155062 и flutter#155062.",
        "Закрыла баг b/580852205 и crbug/576664769.",
        # Wrong Piper shortlink URL for a 7-digit Gerrit CL must still fail
        "Ссылка: [cl/1414016](http://cl/1414016).",
        # Second CL unlinked next to a linked first CL must still fail
        (
            "В [Gerrit 1414016](https://skia-review.googlesource.com/c/skia/+/1414016)"
            " ок, а в Gerrit 1414017 нужен фикс."
        ),
        # PS mention in a separate unlinked paragraph far from top link must fail
        (
            "Ревью [Gerrit 1414016](https://skia-review.googlesource.com/c/skia/+/1414016).\n\n"
            "Я положила черновик на PS1 (`77640dee_0908b885`)."
        ),
        # Bare PS1 or bare draft hex ID in the SAME paragraph as a base CL URL must still fail
        (
            "В [Gerrit 1414016](https://skia-review.googlesource.com/c/skia/+/1414016) "
            "положила черновик на PS1 (`77640dee_0908b885`)."
        ),
        # Lowercase pr #N must also be blocked when unlinked
        "Нужен ребейз для pr #194980.",
    ]
    for i, reply in enumerate(bad_replies):
      with self.subTest(reply=reply), tempfile.TemporaryDirectory() as tmp_dir:
        out = self._run_reply(tmp_dir, reply, user_idx=10 * (i + 1))
        self.assertEqual(out.get("decision"), "continue", f"Expected block for: {reply}")
        self.assertIn("[CLICKABLE LINKS GUARD]", out.get("reason", ""))
        # Second stop in the same user turn must not loop forever
        out_second = self._run_reply(tmp_dir, reply, user_idx=10 * (i + 1))
        self.assertEqual(out_second, {})

  def test_linked_entity_mentions_and_exempt_contexts_allow_stop(self):
    good_replies = [
        (
            "Положила реплай в черновики на "
            "[Gerrit 1414016 PS1](https://skia-review.googlesource.com/c/skia/+/1414016/1) "
            "([черновик `77640dee_0908b885`]"
            "(https://skia-review.googlesource.com/c/skia/+/1414016/1))."
        ),
        (
            "**CL:** [Gerrit 1414016 PS1 — [skparagraph] Measure the hit glyph]"
            "(https://skia-review.googlesource.com/c/skia/+/1414016/1)"
        ),
        (
            "Опубликованный тред: "
            "[`77640dee_0908b885`]"
            "(https://skia-review.googlesource.com/c/skia/+/1414016/comment/77640dee_0908b885/)."
        ),
        (
            "Исправлено в [PR #194980](https://github.com/flutter/flutter/pull/194980) "
            "для [flutter#155062](https://github.com/flutter/flutter/issues/155062)."
        ),
        (
            "Закрыла [b/580852205](https://b.corp.google.com/issues/580852205) и "
            "[crbug/576664769](https://issues.chromium.org/issues/576664769)."
        ),
        "Piper ролл: [cl/1008075988](http://cl/1008075988).",
        # Conversation handoff / redirect links carry chat titles, not unlinked entities
        (
            "Продолжаем в [👉 [16:48] ⦿ Gerrit 1414016 hit glyph]"
            "(conversation://55006e03-15e4-4762-8c30-454dfeefdcf8) и "
            "[Gerrit 1414017](conversation://bb4e75ac-5396-4d7c-a095-19bd161047d3)."
        ),
        # Fenced code blocks and CLI invocations in inline code are exempt
        (
            "Запусти `python3 ~/.gemini/config/hooks/chat_lifecycle.py set-title c1 "
            '"Gerrit 1414016 hit glyph"`:\n'
            "```gitcommit\nBug: b/580852205\nReviewed-on: CL 1414016 PS1\n```"
        ),
        # Verbatim compare.md Run Attestation tables with `PS1` variant labels are exempt
        (
            "Результаты прогона на [Gerrit 1414016 PS1]"
            "(https://skia-review.googlesource.com/c/skia/+/1414016/1):\n\n"
            "## Паспорт прогона (Run Attestation)\n\n"
            "| Ref | Commit |\n| :--- | :--- |\n| `PS1` | `8675bb8` |\n\n"
            "## `PS1` vs `base`\n"
        ),
    ]
    for i, reply in enumerate(good_replies):
      with self.subTest(reply=reply), tempfile.TemporaryDirectory() as tmp_dir:
        out = self._run_reply(tmp_dir, reply, user_idx=100 * (i + 1))
        self.assertEqual(out, {}, f"Unexpected block ({out}) for: {reply}")


if __name__ == "__main__":
  unittest.main()
