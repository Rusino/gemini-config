#!/usr/bin/env python3
# Copyright 2026 Google LLC
"""Unit tests for skills/redline/scripts/redline.py."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills",
    "redline",
    "scripts",
    "redline.py",
)
_SPEC = importlib.util.spec_from_file_location("redline", _SCRIPT_PATH)
redline = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules["redline"] = redline
_SPEC.loader.exec_module(redline)


class RedlineTest(unittest.TestCase):

  def test_coalesces_small_gaps_into_was_now_block(self):
    old = "Прототип на Mokey: `CSUInteractive` −95,7 мс (−5,3%)."
    new = "Прототип на Mokey: медиана `CSUInteractive` −95,7 мс (−5,3%), ожидание `1.ui` убрано."
    html_out, del_w, ins_w, chg = redline.diff_inline_markdown(
        old, new, max_equal_words=4
    )
    self.assertTrue(chg)
    self.assertEqual(html_out.count("<del>"), 1)
    self.assertEqual(html_out.count("<ins>"), 1)
    self.assertIn("<code>CSUInteractive</code>", html_out)
    self.assertIn("<code>1.ui</code>", html_out)
    self.assertGreater(ins_w, del_w)

  def test_preserves_separate_blocks_when_unchanged_gap_exceeds_limit(self):
    old = "Первое старое слово и затем один два три четыре пять неизменных слов и старый хвост."
    new = "Первое новое слово и затем один два три четыре пять неизменных слов и новый хвост."
    html_out, _, _, chg = redline.diff_inline_markdown(
        old, new, max_equal_words=4
    )
    self.assertTrue(chg)
    self.assertEqual(html_out.count("<del>"), 2)
    self.assertEqual(html_out.count("<ins>"), 2)

  def test_inline_bold_with_code_renders_single_strong_tag(self):
    md = "**Шаг 1. Flutter: отложенный `SetupDefaultFontManager`.** Текст."
    toks = redline.tokenize_inline_markdown(md)
    rendered = redline.render_inline_tokens(toks)
    self.assertEqual(
        rendered,
        "<strong>Шаг 1. Flutter: отложенный <code>SetupDefaultFontManager</code>.</strong> Текст.",
    )
    # Unmatched asterisk before bold must not swallow words as italic
    ptr_toks = redline.tokenize_inline_markdown("pointer *p and **bold**")
    self.assertEqual(
        redline.render_inline_tokens(ptr_toks),
        "pointer *p and <strong>bold</strong>",
    )

  def test_renamed_heading_and_wrapped_prose_number_in_blocks(self):
    old = (
        "## Что принимаем\n\n"
        "- Это зависит от открытого вопроса\n"
        "  1. Насколько короче — покажет замер.\n"
    )
    new = (
        "## С чем мы согласны\n\n"
        "- Это зависит от открытого вопроса\n"
        "  1. Насколько короче — покажет замер.\n"
    )
    page, stats = redline.render_redline_html(old, new)
    self.assertIn(
        '<h2 class="doc-block block-mod">'
        "<del>Что принимаем</del> <ins>С чем мы согласны</ins></h2>",
        page,
    )
    self.assertIn(
        "открытого вопроса 1. Насколько короче — покажет замер.</li>",
        page,
    )
    self.assertEqual(stats["changed_blocks"], 1)

  def test_reconstructs_transcript_snapshots_and_picks_last_seen_turn(self):
    with tempfile.TemporaryDirectory() as tmp:
      doc_path = os.path.join(tmp, "draft.md")
      transcript_path = os.path.join(tmp, "transcript_full.jsonl")

      steps = [
          {"step_index": 1, "type": "USER_INPUT", "created_at": "2026-10-22T20:00:00Z"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "created_at": "2026-10-22T20:01:00Z",
              "tool_calls": [
                  {
                      "name": "write_file",
                      "args": {"Path": doc_path, "Content": "# Title\n\nOld text.\n"},
                  }
              ],
          },
          {"step_index": 3, "type": "GENERIC", "status": "DONE", "content": "Created file."},
          {"step_index": 4, "type": "USER_INPUT", "created_at": "2026-10-22T20:05:00Z"},
          {
              "step_index": 5,
              "type": "PLANNER_RESPONSE",
              "created_at": "2026-10-22T20:06:00Z",
              "tool_calls": [
                  {
                      "name": "edit_file",
                      "args": {
                          "Path": doc_path,
                          "Target": "Old text.",
                          "Replacement": "Middle text seen by user.",
                      },
                  }
              ],
          },
          {"step_index": 6, "type": "GENERIC", "status": "DONE", "content": "Edited."},
          {"step_index": 7, "type": "USER_INPUT", "created_at": "2026-10-22T20:10:00Z"},
          {
              "step_index": 8,
              "type": "PLANNER_RESPONSE",
              "created_at": "2026-10-22T20:11:00Z",
              "tool_calls": [
                  {
                      "name": "write_file",
                      "args": {
                          "Path": doc_path,
                          "Content": "# Title\n\nFinal text after review.\n",
                      },
                  }
              ],
          },
          {"step_index": 9, "type": "GENERIC", "status": "DONE", "content": "Created file."},
          {"step_index": 10, "type": "USER_INPUT", "created_at": "2026-10-22T20:12:00Z"},
      ]
      with open(transcript_path, "w", encoding="utf-8") as f:
        for st in steps:
          f.write(json.dumps(st) + "\n")
      with open(doc_path, "w", encoding="utf-8") as f:
        f.write("# Title\n\nFinal text after review.\n")

      snaps = redline.reconstruct_file_snapshots(transcript_path, target_path=doc_path)
      chosen = redline.select_last_seen_snapshot(
          snaps, "# Title\n\nFinal text after review.\n"
      )
      self.assertIsNotNone(chosen)
      self.assertEqual(chosen.content, "# Title\n\nMiddle text seen by user.\n")
      self.assertEqual(chosen.user_step, 7)

  def test_parse_drive_and_gdocs_json_payloads(self):
    drive_json = json.dumps(
        {"fileContent": "# Heading\n\nUses `SkFontMgr\\_Android` and \\~800 ms.\n"}
    )
    self.assertEqual(
        redline.parse_json_doc_payload(drive_json),
        "# Heading\n\nUses `SkFontMgr_Android` and ~800 ms.\n",
    )

    gdoc_json = json.dumps({
        "content": {
            "tabs": [{
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "paragraph": {
                                    "paragraphStyle": {"namedStyleType": "HEADING_2"},
                                    "elements": [{
                                        "textRun": {"content": "Plan\n", "textStyle": {}}
                                    }],
                                }
                            },
                            {
                                "paragraph": {
                                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                    "elements": [
                                        {
                                            "textRun": {
                                                "content": "Call ",
                                                "textStyle": {},
                                            }
                                        },
                                        {
                                            "textRun": {
                                                "content": "scanStyle",
                                                "textStyle": {
                                                    "weightedFontFamily": {
                                                        "fontFamily": "Roboto Mono"
                                                    }
                                                },
                                            }
                                        },
                                    ],
                                }
                            },
                        ]
                    }
                }
            }]
        }
    })
    parsed = redline.parse_json_doc_payload(gdoc_json)
    self.assertEqual(parsed, "## Plan\n\nCall `scanStyle`\n")

  def test_git_fallback_loads_head_or_head_tilde_1(self):
    with tempfile.TemporaryDirectory() as tmp:
      subprocess.run(["git", "-C", tmp, "init", "-q"], check=True)
      subprocess.run(["git", "-C", tmp, "config", "user.email", "t@example.com"], check=True)
      subprocess.run(["git", "-C", tmp, "config", "user.name", "Test"], check=True)
      doc = os.path.join(tmp, "note.md")
      with open(doc, "w", encoding="utf-8") as f:
        f.write("# Note\n\nVersion 1.\n")
      subprocess.run(["git", "-C", tmp, "add", "note.md"], check=True)
      subprocess.run(["git", "-C", tmp, "commit", "-q", "-m", "v1"], check=True)

      with open(doc, "w", encoding="utf-8") as f:
        f.write("# Note\n\nVersion 2.\n")
      subprocess.run(["git", "-C", tmp, "add", "note.md"], check=True)
      subprocess.run(["git", "-C", tmp, "commit", "-q", "-m", "v2"], check=True)

      # Clean working tree -> falls back to HEAD~1 (Version 1)
      base_clean, lbl_clean = redline.load_git_version(doc)
      self.assertEqual(base_clean, "# Note\n\nVersion 1.\n")
      self.assertIn("HEAD~1", lbl_clean)

      # Dirty working tree -> falls back to HEAD (Version 2)
      with open(doc, "w", encoding="utf-8") as f:
        f.write("# Note\n\nVersion 3 dirty.\n")
      base_dirty, lbl_dirty = redline.load_git_version(doc)
      self.assertEqual(base_dirty, "# Note\n\nVersion 2.\n")
      self.assertIn("HEAD:", lbl_dirty)

      # Symlinked parent directory (simulating macOS /var -> /private/var)
      with tempfile.TemporaryDirectory() as link_parent:
        symlink_repo = os.path.join(link_parent, "alias_repo")
        os.symlink(tmp, symlink_repo)
        symlink_doc = os.path.join(symlink_repo, "note.md")
        base_sym, lbl_sym = redline.load_git_version(symlink_doc)
        self.assertEqual(base_sym, "# Note\n\nVersion 2.\n")
        self.assertEqual(lbl_sym, "git HEAD:note.md")

  def test_table_diff_under_unchanged_heading_aligns_deleted_and_inserted_rows(self):
    old = (
        "### Matrix\n\n"
        "| Issue | Description |\n"
        "| --- | --- |\n"
        "| #10 | First row unchanged |\n"
        "| #20 | Removed middle row |\n"
        "| #30 | Third row with old metric 0.5 px |\n"
    )
    new = (
        "### Matrix\n\n"
        "| Issue | Description |\n"
        "| --- | --- |\n"
        "| #10 | First row unchanged |\n"
        "| #30 | Third row with new metric 0.15 px |\n"
        "| #40 | Newly added row at end |\n"
    )
    page, stats = redline.render_redline_html(old, new)
    self.assertEqual(stats["changed_blocks"], 1)
    self.assertIn('<tr class="row-del"><td><del>#20</del></td>', page)
    self.assertIn('<tr class="row-ins"><td><ins>#40</ins></td>', page)
    self.assertIn(
        "<td>#30</td><td>Third row with <del>old metric 0.5</del>"
        " <ins>new metric 0.15</ins> px</td>",
        page,
    )

  def test_move_detection_pairs_relocated_paragraph_with_inline_diff_and_stub(self):
    old = (
        "## Раздел А\n\n"
        "Первый абзац остаётся на месте без изменений.\n\n"
        "## Раздел Б\n\n"
        "Этот важный абзац про кэширование глифов в CoreText переедет наверх с правкой 10 мс.\n"
    )
    new = (
        "## Раздел А\n\n"
        "Этот важный абзац про кэширование глифов в CoreText переедет наверх с правкой 25 мс.\n\n"
        "Первый абзац остаётся на месте без изменений.\n\n"
        "## Раздел Б\n"
    )
    page, stats = redline.render_redline_html(old, new)
    self.assertEqual(stats["moved_blocks"], 1)
    self.assertIn("↕ Перенесено из «Раздел Б»", page)
    self.assertIn("↪ Перенесено в «Раздел А»", page)
    self.assertIn("<del>10</del> <ins>25</ins> мс.", page)

  def test_heavy_rewrite_uses_stacked_was_now_and_sbs_auto_mode(self):
    old = (
        "Старая архитектура использовала синхронный вызов парсера таблиц шрифтов "
        "при каждом создании процесса приложения на старте системы.\n"
    )
    new = (
        "В новой схеме потоки рендеринга запрашивают дескрипторы шрифтов лениво "
        "через системный кэш без блокировки главного цикла.\n"
    )
    # Pair as 1-to-1 paragraph replacement where >55% of words changed
    page, _ = redline.render_redline_html(old, new)
    self.assertIn('class="rewrite-stack"', page)
    self.assertIn('<span class="rewrite-tag">Было</span>', page)
    self.assertIn('<span class="rewrite-tag">Стало</span>', page)
    self.assertIn('<body data-mode="sbs">', page)


if __name__ == "__main__":
  unittest.main()
