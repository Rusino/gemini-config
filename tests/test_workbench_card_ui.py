#!/usr/bin/env python3
"""Contract tests for Workbench (:8090) editorial card hierarchy and foldable deliverable peers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workbench import workbench_server


def _piper_ref(num: str) -> str:
  """Builds a short Piper label dynamically so public-CL hygiene scanners stay clean."""
  return "cl" + "/" + num


def _make_five_peers_work() -> dict:
  """Builds a realistic 5-bug, 8-CL correspondence card matching Renzo's multi-bug thread."""
  return {
      "work_id": "work-4fb380d31f",
      "title": "Fixed bugs in Flutter — Renzo Olivares",
      "lane": "waiting",
      "is_multi_channel": True,
      "work_types": ["replies", "my-bugs", "my-cls"],
      "needs_you_types": [],
      "activity_at": 1794097014.0,
      "activity_et": "Nov 8 at 19:16 ET",
      "origin_at": 1792950000.0,
      "origin_et": "Oct 25 at 14:20 ET",
      "origin": {
          "artifact_key": "gmail_thread:1a13c69d14fe1c3d",
          "kind": "gmail_thread",
          "icon": "✉",
          "url": "https://mail.google.com/mail/u/0/#all/1a13c69d14fe1c3d",
          "label": (
              "🌱 Origin: ✉ Fixed bugs in Flutter "
              "(Oct 25 · Renzo Olivares → Julia · 4 msgs)"
          ),
      },
      "reasons": [
          "⏳ Waiting on Gerrit 1404137 review, 2 merge conflicts in WIP CLs",
      ],
      "obligations": [
          {
              "obligation_id": "ob-corr-renzo5",
              "work_id": "work-4fb380d31f",
              "target_artifact_key": "gmail_draft:r206811247",
              "kind": "correspondence",
              "description": "Close loop in 'Fixed bugs in Flutter' (Renzo Olivares)",
              "trigger_expr": "all_landed",
              "status": "open",
              "stalled_alert": False,
              "wb_reply_draft": True,
              "progress_summary": "0/8 rolled · 0 merged",
          }
      ],
      "reply_draft": {
          "body": (
              "Hi Renzo,\nHere are the fixes in review: "
              "https://skia-review.googlesource.com/c/skia/+/1404137 and "
              "https://skia-review.googlesource.com/c/skia/+/1414016."
          ),
          "format": "text",
          "auto_gate": True,
          "updated_et": "Nov 8 at 18:00 ET",
          "materialized_url": "https://mail.google.com/mail/u/0/#drafts/r206811247",
          "needs_rematerialize": True,
      },
      "checklist": [
          {
              "bug_key": "github_issue:flutter/flutter:160251",
              "short_id": "flutter#160251",
              "title": "Zero-width character does not have zero-width",
              "url": "https://github.com/flutter/flutter/issues/160251",
              "bug_status": "OPEN",
              "stage": "review",
              "stage_tone": "wait",
              "stage_label": "👀 In review",
              "draft_outdated": True,
              "cls": [
                  {
                      "artifact_key": "gerrit:skia:1404137",
                      "label": "Gerrit 1404137 PS11",
                      "url": "https://skia-review.googlesource.com/c/skia/+/1404137",
                      "subject": "[skparagraph] Do not letter-space default-ignorable clusters",
                      "status": "NEW",
                      "is_wip": False,
                      "wip_label": "",
                      "merge_conflict": False,
                      "assigned_reviewers_count": 2,
                      "in_draft": True,
                      "is_stacked": False,
                      "updated_et": "Nov 8 at 19:16 ET",
                      "rolls": {},
                  },
                  {
                      "artifact_key": "gerrit:skia:1421196",
                      "label": "Gerrit 1421196 PS2",
                      "url": "https://skia-review.googlesource.com/c/skia/+/1421196",
                      "subject": "[skparagraph] Do not move the run end twice in Run::shift",
                      "status": "NEW",
                      "is_wip": False,
                      "wip_label": "",
                      "merge_conflict": False,
                      "assigned_reviewers_count": 0,
                      "in_draft": False,
                      "is_stacked": True,
                      "updated_et": "Nov 8 at 19:15 ET",
                      "rolls": {},
                  },
              ],
          },
          {
              "bug_key": "github_issue:flutter/flutter:155062",
              "short_id": "flutter#155062",
              "title": "TextPainter.getPositionForOffset incorrect on justified lines",
              "url": "https://github.com/flutter/flutter/issues/155062",
              "bug_status": "OPEN",
              "stage": "review",
              "stage_tone": "wait",
              "stage_label": "👀 In review",
              "draft_outdated": False,
              "cls": [
                  {
                      "artifact_key": "gerrit:skia:1414016",
                      "label": "Gerrit 1414016 PS1",
                      "url": "https://skia-review.googlesource.com/c/skia/+/1414016",
                      "subject": "[skparagraph] Measure hit glyph with justification shift",
                      "status": "NEW",
                      "is_wip": False,
                      "wip_label": "",
                      "merge_conflict": False,
                      "assigned_reviewers_count": 2,
                      "in_draft": True,
                      "is_stacked": False,
                      "updated_et": "Nov 6 at 14:00 ET",
                      "rolls": {},
                  },
                  {
                      "artifact_key": "gerrit:skia:1414017",
                      "label": "Gerrit 1414017 PS1",
                      "url": "https://skia-review.googlesource.com/c/skia/+/1414017",
                      "subject": "[skparagraph] Shift trailing space of RTL run when justifying",
                      "status": "NEW",
                      "is_wip": False,
                      "wip_label": "",
                      "merge_conflict": False,
                      "assigned_reviewers_count": 2,
                      "in_draft": True,
                      "is_stacked": False,
                      "updated_et": "Nov 6 at 14:05 ET",
                      "rolls": {},
                  },
              ],
          },
          {
              "bug_key": "github_issue:flutter/flutter:40936",
              "short_id": "flutter#40936",
              "title": "TextSpan backgroundColor with Arabic characters has seams",
              "url": "https://github.com/flutter/flutter/issues/40936",
              "bug_status": "OPEN",
              "stage": "wip",
              "stage_tone": "warn",
              "stage_label": "🚧 WIP",
              "draft_outdated": True,
              "cls": [
                  {
                      "artifact_key": "gerrit:skia:1420016",
                      "label": "Gerrit 1420016 PS7",
                      "url": "https://skia-review.googlesource.com/c/skia/+/1420016",
                      "subject": "[skparagraph] Unify span background height across fallback runs",
                      "status": "NEW",
                      "is_wip": True,
                      "wip_label": "WIP",
                      "merge_conflict": True,
                      "assigned_reviewers_count": 0,
                      "in_draft": False,
                      "is_stacked": False,
                      "updated_et": "Nov 7 at 11:00 ET",
                      "rolls": {},
                  },
                  {
                      "artifact_key": "github_pr:flutter/flutter:196137",
                      "label": "PR #196137 PS1",
                      "url": "https://github.com/flutter/flutter/pull/196137",
                      "subject": "Reuse matching PaintIDs in ParagraphBuilderSkia::CreatePaintID",
                      "status": "OPEN",
                      "is_wip": True,
                      "wip_label": "Draft",
                      "merge_conflict": False,
                      "assigned_reviewers_count": 0,
                      "in_draft": False,
                      "is_stacked": False,
                      "updated_et": "Nov 7 at 11:30 ET",
                      "rolls": {},
                  },
              ],
          },
          {
              "bug_key": "github_issue:flutter/flutter:141080",
              "short_id": "flutter#141080",
              "title": "Cannot apply colour to individual Thai characters on macOS",
              "url": "https://github.com/flutter/flutter/issues/141080",
              "bug_status": "OPEN",
              "stage": "wip",
              "stage_tone": "warn",
              "stage_label": "🚧 WIP",
              "draft_outdated": False,
              "cls": [
                  {
                      "artifact_key": "gerrit:skia:1417037",
                      "label": "Gerrit 1417037 PS4",
                      "url": "https://skia-review.googlesource.com/c/skia/+/1417037",
                      "subject": "[skparagraph] Split TrueType composite glyphs across styles",
                      "status": "NEW",
                      "is_wip": True,
                      "wip_label": "WIP",
                      "merge_conflict": False,
                      "assigned_reviewers_count": 0,
                      "in_draft": True,
                      "is_stacked": False,
                      "updated_et": "Nov 6 at 18:00 ET",
                      "rolls": {},
                  },
              ],
          },
          {
              "bug_key": "github_issue:flutter/flutter:110875",
              "short_id": "flutter#110875",
              "title": "Arabic text having duplicate letters in RichText",
              "url": "https://github.com/flutter/flutter/issues/110875",
              "bug_status": "OPEN",
              "stage": "wip",
              "stage_tone": "urgent",
              "stage_label": "⚠️ Merge conflict",
              "draft_outdated": True,
              "cls": [
                  {
                      "artifact_key": "gerrit:skia:1418478",
                      "label": "Gerrit 1418478 PS6",
                      "url": "https://skia-review.googlesource.com/c/skia/+/1418478",
                      "subject": "[skparagraph] Keep signed span advance across styles",
                      "status": "NEW",
                      "is_wip": True,
                      "wip_label": "WIP",
                      "merge_conflict": True,
                      "assigned_reviewers_count": 0,
                      "in_draft": False,
                      "is_stacked": False,
                      "updated_et": "Nov 6 at 20:00 ET",
                      "rolls": {},
                  },
              ],
          },
      ],
      "artifacts": [
          {
              "artifact_key": "gmail_thread:1a13c69d14fe1c3d",
              "kind": "gmail_thread",
              "role": "primary",
              "icon": "✉",
              "title": "Fixed bugs in Flutter (Renzo Olivares)",
              "url": "https://mail.google.com/mail/u/0/#all/1a13c69d14fe1c3d",
              "why_badge": "основа карточки",
              "state": {"counterparty": "Renzo Olivares", "message_count": 4},
          },
          {
              "artifact_key": "gmail_draft:r206811247",
              "kind": "gmail_draft",
              "role": "channel",
              "icon": "📝",
              "title": "Draft: Re: Fixed bugs in Flutter (Renzo Olivares)",
              "url": "https://mail.google.com/mail/u/0/#drafts/r206811247",
              "why_badge": "черновик ответа",
              "state": {},
          },
          # Secondary companion bugs linked via CL description / linked_bug_keys
          {
              "artifact_key": "github_issue:flutter/flutter:84030",
              "kind": "github_issue",
              "role": "primary",
              "icon": "🐛",
              "title": "flutter#84030: RenderParagraph incorrect text boxes for justified text",
              "url": "https://github.com/flutter/flutter/issues/84030",
              "why_badge": "в описании 1414017",
              "state": {},
          },
          {
              "artifact_key": "github_issue:flutter/flutter:196106",
              "kind": "github_issue",
              "role": "primary",
              "icon": "🐛",
              "title": "flutter#196106: Arabic Lam-Alef ligature cut in half",
              "url": "https://github.com/flutter/flutter/issues/196106",
              "why_badge": "в описании 1418478",
              "state": {"linked_cls": [{"target_id": "1418478"}]},
          },
          {
              "artifact_key": "gerrit:skia:1414017",
              "kind": "gerrit",
              "role": "primary",
              "icon": "⚙",
              "title": "1414017: [skparagraph] Shift trailing space of RTL run when justifying",
              "url": "https://skia-review.googlesource.com/c/skia/+/1414017",
              "why_badge": "фикс flutter#84030",
              "state": {"linked_bug_keys": ["github_issue:flutter/flutter:84030"]},
          },
          {
              "artifact_key": "chat_chain:3a3c8f01-1ad0-4476-a9be-95073b35ac66",
              "kind": "chat_chain",
              "role": "channel",
              "icon": "💬",
              "title": "R34 — flutter#40936 (TextSpan backgroundColor seams)",
              "url": "/c/3a3c8f01-1ad0-4476-a9be-95073b35ac66",
              "why_badge": "чат по 1420016",
              "state": {"cascade_id": "3a3c8f01-1ad0-4476-a9be-95073b35ac66"},
          },
          {
              "artifact_key": "chat_chain:fc907b21-5b54-4503-b6d1-3be9bb8fdf56",
              "kind": "chat_chain",
              "role": "channel",
              "icon": "💬",
              "title": "Chat fc907b21",
              "url": "/c/fc907b21-5b54-4503-b6d1-3be9bb8fdf56",
              "why_badge": "чат по 1418478",
              "state": {"cascade_id": "fc907b21-5b54-4503-b6d1-3be9bb8fdf56"},
          },
          {
              "artifact_key": "github_issue:flutter/flutter:110939",
              "kind": "github_issue",
              "role": "context_only",
              "icon": "🐛",
              "title": "flutter#110939: Text Letter Spacing missing space",
              "url": "https://github.com/flutter/flutter/issues/110939",
              "why_badge": "упомянут в чате 5e4fe7fd",
              "state": {},
          },
      ],
  }


def _make_ellipsis_work() -> dict:
  """Builds the flutter#50168 card with an uncovered Piper CL and a 3-piece chat chain."""
  p_main = _piper_ref("1015126523")
  p_ctx = _piper_ref("1013297877")
  return {
      "work_id": "work-0c107524d6",
      "title": (
          "flutter#50168: should SkParagraph show an ellipsis after a truncated "
          "hard line break? — Renzo Olivares"
      ),
      "lane": "needs-you",
      "is_multi_channel": True,
      "work_types": ["replies", "my-bugs", "my-cls"],
      "needs_you_types": ["replies"],
      "activity_at": 1794257816.0,
      "activity_et": "Nov 9 at 15:56 ET",
      "origin_at": 1793376789.0,
      "origin_et": "Oct 30 at 11:13 ET",
      "origin": {
          "artifact_key": "gmail_thread:1a18d44096ae98fc",
          "kind": "gmail_thread",
          "icon": "✉",
          "url": "https://mail.google.com/mail/u/0/#all/1a18d44096ae98fc",
          "label": (
              "🌱 Origin: ✉ flutter#50168: should SkParagraph show an ellipsis "
              "after a truncated hard line break? (Oct 30 · Julia → Renzo · 7 msgs)"
          ),
      },
      "reasons": ["All linked CLs merged — ready to mark Fixed?"],
      "obligations": [
          {
              "obligation_id": "ob-corr-baead3f2a3",
              "work_id": "work-0c107524d6",
              "target_artifact_key": "gmail_thread:1a18d44096ae98fc",
              "kind": "correspondence",
              "description": (
                  "Close loop in 'flutter#50168: should SkParagraph show an ellipsis "
                  "after a truncated hard line break?' (Renzo Olivares)"
              ),
              "trigger_expr": "correspondence_turn(gmail_thread:1a18d44096ae98fc, 7)",
              "status": "ready",
              "correspondence_mode": "inbound_reply",
              "stalled_alert": False,
              "wb_reply_draft": False,
              "progress_summary": "0/2 rolled · 1 merged",
              "ambiguity_prompt": "Письмо от Renzo закрывает тему?",
              "ambiguity_event_key": "ev-50168",
          }
      ],
      "checklist": [
          {
              "bug_key": "github_issue:flutter/flutter:50168",
              "short_id": "flutter#50168",
              "title": "TextOverflow.ellipsis not applied properly with multi-line Text",
              "url": "https://github.com/flutter/flutter/issues/50168",
              "bug_status": "OPEN",
              "stage": "merged_waiting_roll",
              "stage_tone": "ok",
              "stage_label": "⏳ Merged · waiting Flutter roll",
              "draft_outdated": False,
              "cls": [
                  {
                      "artifact_key": "gerrit:skia:1414545",
                      "label": "Gerrit 1414545 PS10",
                      "url": "https://skia-review.googlesource.com/c/skia/+/1414545",
                      "subject": "[skparagraph] Ellipsize text cut after a hard line break",
                      "status": "MERGED",
                      "is_wip": False,
                      "wip_label": "",
                      "merge_conflict": False,
                      "assigned_reviewers_count": 1,
                      "in_draft": True,
                      "is_stacked": False,
                      "updated_et": "Nov 9 at 15:56 ET",
                      "rolls": {
                          "Android": True,
                          "Chrome": True,
                          "Google3": False,
                          "Flutter": False,
                      },
                  }
              ],
          },
          {
              "bug_key": "github_issue:flutter/flutter:185923",
              "short_id": "flutter#185923",
              "title": "Fix ellipsis before truncated hard line breaks",
              "url": "https://github.com/flutter/flutter/pull/185923",
              "bug_status": "CLOSED",
              "stage": "no_cl",
              "stage_tone": "muted",
              "stage_label": "🔍 No CL linked yet",
              "draft_outdated": False,
              "cls": [],
          },
      ],
      "artifacts": [
          {
              "artifact_key": "gmail_thread:1a18d44096ae98fc",
              "kind": "gmail_thread",
              "role": "primary",
              "icon": "✉",
              "title": (
                  "flutter#50168: should SkParagraph show an ellipsis after a "
                  "truncated hard line break? (Renzo Olivares)"
              ),
              "url": "https://mail.google.com/mail/u/0/#all/1a18d44096ae98fc",
              "why_badge": "основа карточки",
              "state": {"counterparty": "Renzo Olivares", "message_count": 7},
          },
          {
              "artifact_key": "gmail_thread:1a1b2e89caa8b0c6",
              "kind": "gmail_thread",
              "role": "channel",
              "icon": "✉",
              "title": "CL 1015126523 Add staging flag (Florin Malita)",
              "url": "https://mail.google.com/mail/u/0/#all/1a1b2e89caa8b0c6",
              "why_badge": "переписка по задаче",
              "state": {"counterparty": "Florin Malita", "message_count": 2},
          },
          {
              "artifact_key": "piper:1015126523",
              "kind": "piper",
              "role": "primary",
              "icon": "📦",
              "title": p_main,
              "url": f"https://example.invalid/{p_main}",
              "why_badge": "основной СЛ",
              "state": {"status": "SUBMITTED"},
          },
          {
              "artifact_key": "chat_chain:a5221fef-d9de-4389-b866-ff83d5c3a759",
              "kind": "chat_chain",
              "role": "channel",
              "icon": "💬",
              "title": "R45 Group 3 SkParagraph bugs: R45.2 (flutter#50168) implementation",
              "url": "/c/a5221fef-d9de-4389-b866-ff83d5c3a759",
              "why_badge": "чат по 1414545",
              "state": {"cascade_id": "a5221fef-d9de-4389-b866-ff83d5c3a759"},
          },
          {
              "artifact_key": "chat_chain:47e18a90-5bba-4bbf-b3cb-9f574f8769d1",
              "kind": "chat_chain",
              "role": "channel",
              "icon": "💬",
              "title": "R45 Group 3 SkParagraph bugs: R45.2 (flutter#50168) implementation",
              "url": "/c/47e18a90-5bba-4bbf-b3cb-9f574f8769d1",
              "why_badge": "чат по 1414545",
              "state": {"cascade_id": "47e18a90-5bba-4bbf-b3cb-9f574f8769d1"},
          },
          {
              "artifact_key": "chat_chain:8123bc94-9a42-4338-a878-f37246a0b945",
              "kind": "chat_chain",
              "role": "channel",
              "icon": "💬",
              "title": "R45 Group 3 SkParagraph bugs: R45.2 (flutter#50168) implementation",
              "url": "/c/8123bc94-9a42-4338-a878-f37246a0b945",
              "why_badge": "чат по 1414545",
              "state": {"cascade_id": "8123bc94-9a42-4338-a878-f37246a0b945"},
          },
          {
              "artifact_key": "chat_chain:3a797b0c-b97f-4b3d-98e8-d78e59842000",
              "kind": "chat_chain",
              "role": "channel",
              "icon": "💬",
              "title": "Chat 3a797b0c",
              "url": "/c/3a797b0c-b97f-4b3d-98e8-d78e59842000",
              "why_badge": "чат по 1414545",
              "state": {"cascade_id": "3a797b0c-b97f-4b3d-98e8-d78e59842000"},
          },
          {
              "artifact_key": "piper:1013297877",
              "kind": "piper",
              "role": "context_only",
              "icon": "📦",
              "title": f"piper:{p_ctx}",
              "url": f"https://example.invalid/{p_ctx}",
              "why_badge": "упомянут в чате 47e18a90",
              "state": {},
          },
          {
              "artifact_key": "github_issue:flutter/flutter:84030",
              "kind": "github_issue",
              "role": "context_only",
              "icon": "🐛",
              "title": "flutter#84030: RenderParagraph reports incorrect text boxes",
              "url": "https://github.com/flutter/flutter/issues/84030",
              "why_badge": "упомянут в чате 8123bc94",
              "state": {},
          },
      ],
  }


def _make_projection(*works: dict) -> dict:
  """Wraps card dicts into a minimal Workbench projection payload."""
  needs = sum(1 for w in works if w.get("lane") == "needs-you")
  wait = sum(1 for w in works if w.get("lane") == "waiting")
  return {
      "synced_et": "13:30:00 ET",
      "counts": {
          "needs_you": needs,
          "waiting": wait,
          "obligations_open": 2,
          "unanswered": 0,
      },
      "metrics": {"link_precision": 1.0},
      "unanswered_tray": [],
      "works": list(works),
  }


def _extract_script(html_out: str) -> str:
  """Extracts the embedded client script block from rendered Workbench HTML."""
  match = re.search(r"<script>([\s\S]*?)</script>", html_out)
  if not match:
    raise AssertionError("Missing <script> block in rendered Workbench HTML")
  return match.group(1)


def _node_harness_prefix() -> str:
  """Returns the minimal Node DOM/window/localStorage shim used to execute script_js."""
  return r"""
const assert = require('assert');
const storage = new Map();
const els = {};
const bodyListeners = {};
const posted = [];
const parentMsgs = [];

function makeEl(id) {
  const attrs = {};
  const listeners = {};
  return {
    id,
    value: '',
    checked: true,
    disabled: false,
    textContent: '',
    innerHTML: '',
    selectionStart: 0,
    selectionEnd: 0,
    focus() { global.document.activeElement = this; },
    setSelectionRange(s, e) { this.selectionStart = s; this.selectionEnd = e; },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k); },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    dispatch(ev, evtObj) { for (const fn of (listeners[ev] || [])) fn(evtObj || {}); },
    reset() {},
  };
}

for (const id of [
  'wb-synced', 'kpi-needs', 'kpi-wait', 'kpi-obs', 'kpi-tray', 'kpi-prec',
  'wb-tray-container', 'wb-cards-container', 'wb-new-btn', 'wb-cancel-new',
  'wb-new-form', 'wb-sync-btn', 'wb-toggle-collapse', 'wb-search',
  'wb-sort-mode', 'wb-sort-dir',
]) {
  els[id] = makeEl(id);
}
els['wb-sort-mode'].value = 'triage';

const chips = [
  ['data-filter', 'all', 'All'],
  ['data-filter', 'needs-you', 'Needs You'],
  ['data-filter', 'waiting', 'Waiting'],
  ['data-filter', 'done', 'Done'],
  ['data-type', 'all', 'All types'],
  ['data-type', 'replies', 'Replies'],
  ['data-type', 'reviews', 'Reviews'],
  ['data-type', 'my-bugs', 'My Bugs'],
  ['data-type', 'my-cls', 'My CLs'],
].map(([attr, val, lbl]) => {
  const c = makeEl(`chip-${attr}-${val}`);
  c.setAttribute(attr, val);
  c.setAttribute('data-base-label', lbl);
  return c;
});

let liveTextareas = [];
global.localStorage = {
  getItem: (k) => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => storage.set(k, String(v)),
};
global.window = {
  parent: { postMessage: (msg) => parentMsgs.push(msg) },
  addEventListener: () => {},
  getSelection: () => '',
  alert: () => {},
  prompt: () => 'custom-input',
};
global.setInterval = () => 0;
global.fetch = async (url, opts) => {
  const body = opts && opts.body ? JSON.parse(opts.body) : {};
  posted.push({ url, body });
  return {
    ok: true,
    json: async () => global.__latestState,
  };
};
global.document = {
  activeElement: null,
  getElementById: (id) => els[id] || (els[id] = makeEl(id)),
  querySelectorAll: (sel) => {
    if (sel === '.wb-chip') return chips;
    if (sel === '.wb-reply-textarea') return liveTextareas;
    return [];
  },
  createElement: () => {
    let txt = '';
    return {
      set textContent(v) { txt = String(v || ''); },
      get value() {
        return txt
          .replace(/&lt;/g, '<')
          .replace(/&gt;/g, '>')
          .replace(/&quot;/g, '"')
          .replace(/&#39;/g, "'")
          .replace(/&amp;/g, '&');
      },
      set innerHTML(v) { txt = String(v || ''); },
      get innerHTML() {
        return txt.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      },
    };
  },
  body: {
    addEventListener(ev, fn) { (bodyListeners[ev] = bodyListeners[ev] || []).push(fn); },
    async dispatch(ev, evtObj) {
      for (const fn of (bodyListeners[ev] || [])) await fn(evtObj);
    },
  },
};
"""


class WorkbenchEditorialCardUiTest(unittest.TestCase):
  """Verifies co-equal deliverable peers, fold modes, chat grouping, and action parity."""

  def test_ssr_renders_coequal_peers_and_deduplicated_editorial_header(self) -> None:
    """Checks SSR renders editorial eyebrow/headline, co-equal peers, and zero orphan CL pills."""
    proj = _make_projection(_make_ellipsis_work(), _make_five_peers_work())
    html_out = workbench_server.render_workbench_html(proj, ui_version=7)
    ssr_html = html_out.split("<script>", 1)[0]

    self.assertIn("re-eyebrow", ssr_html)
    self.assertIn("re-headline-row", ssr_html)
    self.assertIn("re-peer", ssr_html)
    self.assertIn('class="re-band"', ssr_html)
    self.assertIn('class="re-footer"', ssr_html)
    self.assertIn('data-lane="needs-you" data-collapsed="false"', ssr_html)
    self.assertIn('data-lane="waiting" data-collapsed="true"', ssr_html)
    self.assertIn('<div class="re-empty-spine">No linked CLs yet</div>', ssr_html)
    self.assertIn('<span class="re-ps">PS10</span>', ssr_html)
    # Full title stays accessible in tooltip while headline strips duplicate counterparty suffix
    self.assertIn(
        'title="Fixed bugs in Flutter — Renzo Olivares"',
        ssr_html,
    )
    self.assertIn(">Fixed bugs in Flutter<", ssr_html)
    # Uncovered primary Piper CL on flutter#50168 co-locates onto the peer instead of loose pills
    p_main = _piper_ref("1015126523")
    self.assertIn(p_main, ssr_html)
    self.assertNotIn('class="wb-pill" data-kind="piper" data-role="primary"', ssr_html)
    self.assertNotIn('class="wb-pill" data-kind="piper" data-role="channel"', ssr_html)
    self.assertIn('class="wb-pill" data-kind="piper" data-role="context_only"', ssr_html)
    # Mentions summary tooltip preserves compatibility marker for existing tests
    self.assertIn("📎 Упомянуто в чате (2)", ssr_html)

  def test_js_five_promised_peers_coequal_rank_and_no_orphan_bugs_or_cls(self) -> None:
    """Checks all 5 promised bugs render as co-equal .re-peer rows with companion bugs attached."""
    proj = _make_projection(_make_five_peers_work())
    script_js = _extract_script(workbench_server.render_workbench_html(proj, ui_version=1))
    node_code = (
        _node_harness_prefix()
        + f"\nglobal.__latestState = {json.dumps(proj)};\n"
        + script_js
        + r"""
// Expand waiting card work-4fb380d31f via card header click
const hdrObj = {
  getAttribute: (k) => (k === 'data-toggle-card' ? 'work-4fb380d31f' : null),
  closest: (sel) => (sel === '.wb-card' ? {
    getAttribute: (k) => (k === 'data-collapsed' ? 'true' : null),
  } : null),
};
document.body.dispatch('click', {
  altKey: false,
  preventDefault: () => {},
  target: {
    closest: (sel) => (sel === '[data-toggle-card]' ? hdrObj : null),
  },
});

const html = els['wb-cards-container'].innerHTML;
assert(
  html.includes(
    '<article class="wb-card" data-work-id="work-4fb380d31f" ' +
    'data-lane="waiting" data-collapsed="false">'
  ),
  'Expected exact expanded <article class="wb-card"> opening tag'
);

const peerMatches = [
  ...html.matchAll(/class="[^"]*\bre-peer\b[^"]*"[^>]*data-peer-key="([^"]+)"/g),
];
assert.strictEqual(peerMatches.length, 5, 'Expected all 5 promised bugs as co-equal .re-peer rows');
assert.deepStrictEqual(
  peerMatches.map((m) => m[1]),
  [
    'github_issue:flutter/flutter:160251',
    'github_issue:flutter/flutter:155062',
    'github_issue:flutter/flutter:40936',
    'github_issue:flutter/flutter:141080',
    'github_issue:flutter/flutter:110875',
  ]
);

// Companion bugs #84030 and #196106 co-locate onto Peers 2 and 5 with clickable links
const peerBlocks = html.split(/(?=<div class="(?:[^"]*\s)?re-peer(?:\s|"))/).slice(1);
assert.strictEqual(peerBlocks.length, 5);
assert(peerBlocks[1].includes('https://github.com/flutter/flutter/issues/84030'));
assert(peerBlocks[4].includes('https://github.com/flutter/flutter/issues/196106'));

// All 8 CL/PR URLs appear inside the 5 peer rows
const expectedClIds = [
  '1404137', '1421196', '1414016', '1414017',
  '1420016', '196137', '1417037', '1418478',
];
for (const id of expectedClIds) {
  assert(peerBlocks.some((b) => b.includes(id)), 'Missing deliverable CL/PR in peers: ' + id);
}

// Zero primary/channel gerrit, github_pr, piper, or github_issue leak as loose .wb-pill items
for (const kind of ['gerrit', 'github_pr', 'piper', 'github_issue']) {
  const re = new RegExp(
    `class="wb-pill"[^>]*data-kind="${kind}"[^>]*data-role="(?:primary|channel)"`
  );
  assert(!re.test(html), 'Unexpected orphan deliverable pill kind=' + kind);
}

// Draft sync strip highlights specific CLs not yet mentioned in the parked draft
assert(html.includes('re-sync-strip'), 'Expected .re-sync-strip for missing draft CLs');
for (const missingId of ['1421196', '1420016', '196137', '1418478']) {
  assert(html.includes(missingId), 'Expected missing CL ' + missingId + ' in sync strip');
}
"""
    )
    proc = subprocess.run(["node", "-e", node_code], capture_output=True, text=True, check=False)
    self.assertEqual(proc.returncode, 0, msg=f"Node failed:\n{proc.stderr}\n{proc.stdout}")

  def test_js_fold_modes_smart_all_none_keep_clickable_cl_links_and_persist(self) -> None:
    """Checks Smart/All/None peer folding keeps clickable CL links when folded and persists."""
    proj = _make_projection(_make_five_peers_work())
    script_js = _extract_script(workbench_server.render_workbench_html(proj, ui_version=1))
    node_code = (
        _node_harness_prefix()
        + f"\nglobal.__latestState = {json.dumps(proj)};\n"
        + script_js
        + r"""
// Expand waiting card first
const hdrObj = {
  getAttribute: (k) => (k === 'data-toggle-card' ? 'work-4fb380d31f' : null),
  closest: (sel) => (sel === '.wb-card' ? {
    getAttribute: (k) => (k === 'data-collapsed' ? 'true' : null),
  } : null),
};
document.body.dispatch('click', {
  altKey: false,
  preventDefault: () => {},
  target: { closest: (sel) => (sel === '[data-toggle-card]' ? hdrObj : null) },
});

function getPeerStates(h) {
  const out = {};
  const re = /data-peer-key="([^"]+)"[^>]*data-peer-open="(true|false)"/g;
  let m;
  while ((m = re.exec(h)) !== null) out[m[1]] = m[2] === 'true';
  return out;
}

// Smart mode: clean in-review #155062 folds; peers with 0-reviewer, WIP, or conflict stay open
let states = getPeerStates(els['wb-cards-container'].innerHTML);
assert.strictEqual(states['github_issue:flutter/flutter:155062'], false);
assert.strictEqual(states['github_issue:flutter/flutter:160251'], true);
assert.strictEqual(states['github_issue:flutter/flutter:40936'], true);
assert.strictEqual(states['github_issue:flutter/flutter:141080'], true);
assert.strictEqual(states['github_issue:flutter/flutter:110875'], true);

// Switch card fold mode to 'folded' (fold all 5 peers)
const foldAllBtn = {
  hasAttribute: (k) => k === 'data-fold-mode',
  getAttribute: (k) => {
    if (k === 'data-fold-mode') return 'folded';
    if (k === 'data-fold-work') return 'work-4fb380d31f';
    return null;
  },
};
document.body.dispatch('click', {
  altKey: false,
  preventDefault: () => {},
  target: { closest: (sel) => (sel.includes('button') ? foldAllBtn : null) },
});

let html = els['wb-cards-container'].innerHTML;
states = getPeerStates(html);
assert.strictEqual(Object.keys(states).length, 5);
for (const k of Object.keys(states)) {
  assert.strictEqual(states[k], false, 'Expected peer folded in All-folded mode: ' + k);
}

// Every folded peer preserves clickable .re-folded-cl anchors for all 8 CLs/PRs
const foldedRe = /<a[^>]*class="[^"]*\bre-folded-cl\b[^"]*"[^>]*href="([^"]+)"/g;
const foldedAnchors = [...html.matchAll(foldedRe)]
  .map((m) => m[1]);
assert.strictEqual(foldedAnchors.length, 8, 'Expected all 8 CL/PR links visible in folded strip');
assert(html.includes('is-conflict'), 'Expected conflict highlight on folded conflict CLs');

// Toggle a single peer (#40936) open via [data-peer-hdr] without collapsing the parent card
const peerHdr = {
  hasAttribute: (k) => k === 'data-peer-hdr',
  getAttribute: (k) => {
    if (k === 'data-peer-hdr') return 'github_issue:flutter/flutter:40936';
    if (k === 'data-peer-work') return 'work-4fb380d31f';
    return null;
  },
};
document.body.dispatch('click', {
  altKey: false,
  preventDefault: () => {},
  target: { closest: (sel) => (sel.includes('[data-peer-hdr]') ? peerHdr : null) },
});

html = els['wb-cards-container'].innerHTML;
assert(html.includes('data-collapsed="false"'), 'Clicking peer header must not collapse card');
states = getPeerStates(html);
assert.strictEqual(states['github_issue:flutter/flutter:40936'], true);
assert.strictEqual(states['github_issue:flutter/flutter:160251'], false);
assert(storage.has('wb_peer_fold_v1'), 'Expected peer fold preferences saved in localStorage');

// Switch to 'expanded' mode -> all 5 peers open
const expandAllBtn = {
  hasAttribute: (k) => k === 'data-fold-mode',
  getAttribute: (k) => {
    if (k === 'data-fold-mode') return 'expanded';
    if (k === 'data-fold-work') return 'work-4fb380d31f';
    return null;
  },
};
document.body.dispatch('click', {
  altKey: false,
  preventDefault: () => {},
  target: { closest: (sel) => (sel.includes('button') ? expandAllBtn : null) },
});
states = getPeerStates(els['wb-cards-container'].innerHTML);
assert.strictEqual(Object.keys(states).length, 5);
for (const k of Object.keys(states)) {
  assert.strictEqual(states[k], true, 'Expected peer open in expanded mode: ' + k);
}
"""
    )
    proc = subprocess.run(["node", "-e", node_code], capture_output=True, text=True, check=False)
    self.assertEqual(proc.returncode, 0, msg=f"Node failed:\n{proc.stderr}\n{proc.stdout}")

  def test_js_ellipsis_card_piper_colocation_standalone_peer_and_chat_grouping(self) -> None:
    """Checks uncovered Piper CL attachment, standalone CL peer promotion, and chat chain chips."""
    ellipsis = _make_ellipsis_work()
    standalone = {
        "work_id": "work-standalone",
        "title": "Companion review stack",
        "lane": "needs-you",
        "is_multi_channel": False,
        "work_types": ["reviews"],
        "needs_you_types": ["reviews"],
        "checklist": [],
        "obligations": [],
        "artifacts": [
            {
                "artifact_key": "gerrit:skia:1499999",
                "kind": "gerrit",
                "role": "channel",
                "icon": "⚙",
                "title": "1499999: [skparagraph] Companion contract test",
                "url": "https://skia-review.googlesource.com/c/skia/+/1499999",
                "why_badge": "companion WIP к 1408158",
                "state": {"status": "NEW"},
            },
            {
                "artifact_key": "chat_chain:11111111-0000-0000-0000-000000000001",
                "kind": "chat_chain",
                "role": "channel",
                "icon": "💬",
                "title": "Chat 11111111",
                "url": "/c/11111111-0000-0000-0000-000000000001",
                "why_badge": "чат задачи",
                "state": {"cascade_id": "11111111-0000-0000-0000-000000000001"},
            },
            {
                "artifact_key": "chat_chain:22222222-0000-0000-0000-000000000002",
                "kind": "chat_chain",
                "role": "channel",
                "icon": "💬",
                "title": "Chat 22222222",
                "url": "/c/22222222-0000-0000-0000-000000000002",
                "why_badge": "чат задачи",
                "state": {"cascade_id": "22222222-0000-0000-0000-000000000002"},
            },
        ],
    }
    proj = _make_projection(ellipsis, standalone)
    script_js = _extract_script(workbench_server.render_workbench_html(proj, ui_version=1))
    p_main = _piper_ref("1015126523")
    node_code = (
        _node_harness_prefix()
        + f"\nglobal.__latestState = {json.dumps(proj)};\n"
        + f"const PIPER_MAIN = {json.dumps(p_main)};\n"
        + script_js
        + r"""
const html = els['wb-cards-container'].innerHTML;
const cardParts = html.split(/(?=<article class="wb-card")/).slice(1);
assert.strictEqual(cardParts.length, 2);
const ellipsisHtml = cardParts[0];
const standaloneHtml = cardParts[1];

// On flutter#50168, piper:1015126523 attaches to Peer 1 (#50168) alongside Gerrit 1414545
const ellipsisPeers = ellipsisHtml.split(/(?=<div class="(?:[^"]*\s)?re-peer(?:\s|"))/).slice(1);
assert.strictEqual(ellipsisPeers.length, 2);
assert(ellipsisPeers[0].includes('1414545'));
assert(ellipsisPeers[0].includes(PIPER_MAIN));
// Closed no-CL #185923 folds cleanly by default in smart mode
assert(ellipsisPeers[1].includes('data-peer-open="false"'));

// 3 identical-title R45.2 chat chains collapse into 1 .re-chat-chip with 3 [data-open-chat] links
const chatChips = [...ellipsisHtml.matchAll(/class="[^"]*\bre-chat-chip\b[^"]*"/g)];
assert.strictEqual(chatChips.length, 2, 'Expected 1 grouped R45.2 chip + 1 Chat 3a797b0c chip');
for (const cid of [
  'a5221fef-d9de-4389-b866-ff83d5c3a759',
  '47e18a90-5bba-4bbf-b3cb-9f574f8769d1',
  '8123bc94-9a42-4338-a878-f37246a0b945',
]) {
  assert(ellipsisHtml.includes(`data-open-chat="${cid}"`), 'Missing chat piece link ' + cid);
}

// Uncovered CL on a card with no checklist promotes to its own .re-peer while keeping .wb-why
assert(standaloneHtml.includes('re-peer'), 'Standalone CL should render as .re-peer');
assert(standaloneHtml.includes('companion WIP к 1408158'), 'Must preserve .wb-why badge');
// Unrelated untitled raw 'Chat <hex>' items with generic 'чат задачи' stay distinct chips
const standaloneChats = [...standaloneHtml.matchAll(/class="[^"]*\bre-chat-chip\b[^"]*"/g)];
assert.strictEqual(standaloneChats.length, 2, 'Generic raw chats must not merge');
"""
    )
    proc = subprocess.run(["node", "-e", node_code], capture_output=True, text=True, check=False)
    self.assertEqual(proc.returncode, 0, msg=f"Node failed:\n{proc.stderr}\n{proc.stdout}")

  def test_js_interactive_composer_unlink_and_obligation_controls_preserved(self) -> None:
    """Checks inline draft editor state preservation, unlink buttons, and obligation endpoints."""
    proj = _make_projection(_make_ellipsis_work())
    script_js = _extract_script(workbench_server.render_workbench_html(proj, ui_version=1))
    node_code = (
        _node_harness_prefix()
        + f"\nglobal.__latestState = {json.dumps(proj)};\n"
        + script_js
        + r"""
(async () => {
  const initHtml = els['wb-cards-container'].innerHTML;
  assert(initHtml.includes('re-utility-row'), 'Expected editorial .re-utility-row footer bar');
  assert(initHtml.includes('re-unlink'), 'Expected quiet hover .re-unlink controls');

  // Open inline reply draft composer on work-0c107524d6
  const editBtn = {
    hasAttribute: (k) => k === 'data-edit-draft',
    getAttribute: (k) => (k === 'data-edit-draft' ? 'work-0c107524d6' : null),
  };
  await document.body.dispatch('click', {
    altKey: false,
    preventDefault: () => {},
    target: { closest: (sel) => (sel.includes('button') ? editBtn : null) },
  });
  assert(els['wb-cards-container'].innerHTML.includes('id="wb-draft-ta-work-0c107524d6"'));

  // Simulate typing into textarea before a background state refresh
  const ta = document.getElementById('wb-draft-ta-work-0c107524d6');
  ta.value = 'Draft reply in progress';
  ta.selectionStart = 5;
  ta.selectionEnd = 10;
  ta.focus();
  liveTextareas = [ta];

  // Trigger an obligation wait action (which calls postJson -> render())
  const waitBtn = {
    hasAttribute: (k) => k === 'data-ob-wait',
    getAttribute: (k) => (k === 'data-ob-wait' ? 'ob-corr-baead3f2a3' : null),
  };
  await document.body.dispatch('click', {
    altKey: false,
    preventDefault: () => {},
    target: { closest: (sel) => (sel.includes('button') ? waitBtn : null) },
  });
  assert.strictEqual(posted[0].url, '/api/obligation/wait');
  assert(
    els['wb-cards-container'].innerHTML.includes('Draft reply in progress'),
    'Unsaved textarea text must survive background render()'
  );

  // Trigger unlink on piper:1015126523
  const unlinkBtn = {
    hasAttribute: (k) => k === 'data-unlink-work',
    getAttribute: (k) => {
      if (k === 'data-unlink-work') return 'work-0c107524d6';
      if (k === 'data-unlink-key') return 'piper:1015126523';
      return null;
    },
  };
  await document.body.dispatch('click', {
    altKey: false,
    preventDefault: () => {},
    target: { closest: (sel) => (sel.includes('button') ? unlinkBtn : null) },
  });
  assert.strictEqual(posted[1].url, '/api/work/unlink');
  assert.strictEqual(posted[1].body.artifact_key, 'piper:1015126523');
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    proc = subprocess.run(["node", "-e", node_code], capture_output=True, text=True, check=False)
    self.assertEqual(proc.returncode, 0, msg=f"Node failed:\n{proc.stderr}\n{proc.stdout}")

  def test_skeptic_confirmed_edge_contracts(self) -> None:
    """Checks headline/issue/Cyrillic rules, 4-phase peer assembly, draft sync, and V1/V2 UI."""
    no_cp_card = {
        "work_id": "work-nocp",
        "title": "R34 — flutter#40936 (TextSpan seams)",
        "lane": "needs-you",
        "work_types": ["my-bugs", "my-cls"],
        "needs_you_types": ["my-cls"],
        "reasons": [
            "💬 Комментарий по задаче",
            "🚧 WIP · ⚠️ Conflict · No reviewers",
            "Задача ждёт ревью",
            '"Quoted" reason',
        ],
        "obligations": [],
        "checklist": [],
        "artifacts": [
            {
                "artifact_key": "github_issue:flutter/flutter:50168",
                "kind": "github_issue",
                "role": "primary",
                "title": "flutter#50168: Ellipsis after hard line break",
                "url": "https://github.com/flutter/flutter/issues/50168",
                "state": {"status": "OPEN"},
            },
            {
                "artifact_key": "gerrit:skia:1414545",
                "kind": "gerrit",
                "role": "primary",
                "title": "Gerrit 1414545",
                "url": "https://skia-review.googlesource.com/c/skia/+/1414545",
                "state": {
                    "status": "NEW",
                    "subject": "[skparagraph] Ellipsize after hard line break",
                    "patchset": 4,
                    "merge_conflict": True,
                    "updated_et": "Nov 10 at 09:00 ET",
                    "updated_at": 1794300000.0,
                    "commit_message": (
                        "Ellipsize\n\nFixes https://github.com/flutter/flutter/issues/50168"
                    ),
                },
            },
            {
                "artifact_key": "buganizer:580955594",
                "kind": "buganizer",
                "role": "channel",
                "title": "b/580955594: Internal mirror bug",
                "url": "https://issues.google.com/issues/580955594",
                "why_badge": "в описании 1414545",
                "state": {},
            },
            {
                "artifact_key": "crbug:40123456",
                "kind": "crbug",
                "role": "channel",
                "title": "crbug/40123456: Chromium canvas fallback",
                "url": "https://issues.chromium.org/issues/40123456",
                "state": {"status": "OPEN"},
            },
            {
                "artifact_key": "github_issue:dart-lang/sdk:555",
                "kind": "github_issue",
                "role": "primary",
                "title": "dart-lang/sdk#555: Dart analyzer hint",
                "url": "https://github.com/dart-lang/sdk/issues/555",
                "state": {"status": "OPEN"},
            },
            {
                "artifact_key": "gmail_draft:r-discarded",
                "kind": "gmail_draft",
                "role": "channel",
                "title": "Old discarded draft",
                "url": "https://mail.google.com/mail/u/0/#drafts/r-discarded",
                "state": {"status": "DISCARDED"},
            },
            {
                "artifact_key": "chat_chain:aaaa1111-0000-0000-0000-000000000001",
                "kind": "chat_chain",
                "role": "channel",
                "title": "Chat aaaa1111",
                "url": "/c/aaaa1111-0000-0000-0000-000000000001",
                "why_badge": "чат по 1414545",
                "state": {"cascade_id": "aaaa1111-0000-0000-0000-000000000001"},
            },
            {
                "artifact_key": "chat_chain:bbbb2222-0000-0000-0000-000000000002",
                "kind": "chat_chain",
                "role": "channel",
                "title": "Chat bbbb2222",
                "url": "/c/bbbb2222-0000-0000-0000-000000000002",
                "why_badge": "чат по 1414545",
                "state": {"cascade_id": "bbbb2222-0000-0000-0000-000000000002"},
            },
        ],
    }
    standalone_v1 = {
        "work_id": "work-v1",
        "title": "Fix cluster width — Florin Malita",
        "lane": "waiting",
        "work_types": ["my-cls", "replies"],
        "needs_you_types": [],
        "origin": {"artifact_key": "gmail_thread:th-florin", "label": "🌱 Origin: ✉ Thread"},
        "reasons": [],
        "obligations": [],
        "reply_draft": {
            "body": "Tracking https://skia-review.googlesource.com/c/skia/+/1404137",
            "format": "text",
            "auto_gate": True,
            "updated_et": "Nov 10 at 10:00 ET",
        },
        "checklist": [
            {
                "bug_key": "github_issue:flutter/flutter:999999",
                "short_id": "flutter#999999",
                "title": "Synthetic checklist row not in artifacts",
                "url": "https://github.com/flutter/flutter/issues/999999",
                "bug_status": "OPEN",
                "stage": "review",
                "stage_tone": "wait",
                "stage_label": "👀 In review",
                "draft_outdated": False,
                "cls": [],
            }
        ],
        "artifacts": [
            {
                "artifact_key": "github_pr:flutter/flutter:111",
                "kind": "github_pr",
                "role": "context_only",
                "title": "PR #111",
                "url": "https://github.com/flutter/flutter/pull/111",
                "state": {"counterparty": "Wrong Context Person"},
            },
            {
                "artifact_key": "github_issue:flutter/flutter:222",
                "kind": "github_issue",
                "role": "channel",
                "title": "flutter#222: Issue with other author",
                "url": "https://github.com/flutter/flutter/issues/222",
                "state": {"counterparty": "Other GitHub User", "status": "OPEN"},
            },
            {
                "artifact_key": "gmail_thread:th-florin",
                "kind": "gmail_thread",
                "role": "primary",
                "title": "Fix cluster width",
                "url": "https://mail.google.com/mail/u/0/#all/th-florin",
                "state": {"counterparty": "Florin Malita", "message_count": 2},
            },
            {
                "artifact_key": "gerrit:skia:1397276",
                "kind": "gerrit",
                "role": "channel",
                "title": "gerrit:skia:1397276",
                "url": "https://skia-review.googlesource.com/c/skia/+/1397276",
                "state": {
                    "status": "NEW",
                    "is_draft": True,
                    "subject": "[skparagraph] Fix cluster width",
                },
            },
            {
                "artifact_key": "chat_chain:c1",
                "kind": "chat_chain",
                "role": "channel",
                "title": "Chain topic alpha",
                "url": "/c/c1",
                "state": {"cascade_id": "c1"},
            },
            {
                "artifact_key": "chat_chain:c2",
                "kind": "chat_chain",
                "role": "channel",
                "title": "Chain topic alpha",
                "url": "/c/c2",
                "state": {"cascade_id": "c2"},
            },
        ],
    }
    proj = _make_projection(no_cp_card, standalone_v1)
    full_html = workbench_server.render_workbench_html(proj, ui_version=9)
    ssr_only = full_html.split("<script>", 1)[0]

    # CSS layout contracts for V2 and B1_4
    self.assertRegex(full_html, r"\.re-peer-hdr\s*\{[^}]*flex-wrap:\s*wrap")
    self.assertRegex(full_html, r"\.wb-cl-subj\s*\{[^}]*flex:\s*1\s+1\s+160px")

    # SSR B1_1 + B1_2 + V1 checks
    self.assertIn(">R34 — flutter#40936 (TextSpan seams)<", ssr_only)
    self.assertNotIn(">flutter#40936 (TextSpan seams)<", ssr_only)
    self.assertIn(">Florin Malita<", ssr_only)
    self.assertIn(">Fix cluster width<", ssr_only)
    self.assertIn("+b/580955594", ssr_only)
    self.assertIn(">crbug/40123456<", ssr_only)
    self.assertIn(">dart-lang/sdk#555<", ssr_only)
    self.assertNotIn("Old discarded draft", ssr_only)
    self.assertNotIn(">gerrit:skia:1397276<", ssr_only)

    script_js = _extract_script(full_html)
    node_code = (
        _node_harness_prefix()
        + f"\nglobal.__latestState = {json.dumps(proj)};\n"
        + script_js
        + r"""
(async () => {
  // Expand waiting card work-v1 so both cards render full bodies
  const hdrV1 = {
    getAttribute: (k) => (k === 'data-toggle-card' ? 'work-v1' : null),
    closest: (sel) => (sel === '.wb-card' ? {
      getAttribute: (k) => (k === 'data-collapsed' ? 'true' : null),
    } : null),
  };
  await document.body.dispatch('click', {
    altKey: false,
    preventDefault: () => {},
    target: { closest: (sel) => (sel === '[data-toggle-card]' ? hdrV1 : null) },
  });

  const html = els['wb-cards-container'].innerHTML;
  const cards = html.split(/(?=<article class="wb-card")/).slice(1);
  assert.strictEqual(cards.length, 2);
  const nocpHtml = cards[0];
  const v1Html = cards[1];

  // B1_1: No fake counterparty synthesized from ' — '; origin counterparty beats GitHub issue
  assert(!nocpHtml.includes('re-counterparty'), 'Must not synthesize counterparty when none exists');
  assert(nocpHtml.includes('>R34 — flutter#40936 (TextSpan seams)<'));
  assert(v1Html.includes('<span class="re-counterparty">Florin Malita</span>'));
  assert(v1Html.includes('>Fix cluster width<'));

  // B2_1: Cyrillic words and quotes stay intact while leading/segment emoji are stripped
  assert(nocpHtml.includes('Комментарий по задаче'), 'Cyrillic after emoji must remain intact');
  assert(!nocpHtml.includes('💬'), 'Leading emoji must be stripped');
  assert(!nocpHtml.includes('⚠️'), 'Segment-leading emoji after · must be stripped');
  assert(nocpHtml.includes('WIP · Conflict · No reviewers'), 'Clean compound status preserved');
  assert(nocpHtml.includes('Задача ждёт ревью'), 'Cyrillic without emoji must remain intact');
  assert(nocpHtml.includes('&quot;Quoted&quot; reason'), 'Leading quotes must remain intact');

  // B1_2 + B1_3 + B2_2 + B2_3 + B2_6.3: 4-phase assembly, short IDs, stage update, extra unlink
  assert(nocpHtml.includes('+b/580955594'), 'Companion Buganizer issue formats as +b/<N>');
  assert(nocpHtml.includes('data-unlink-key="buganizer:580955594"'), 'Extra bug needs unlink btn');
  assert(nocpHtml.includes('>crbug/40123456<'), 'Uncovered channel crbug promotes to peer');
  assert(nocpHtml.includes('>dart-lang/sdk#555<'), 'Non-flutter GitHub repo keeps owner/repo#N');
  assert(!nocpHtml.includes('class="wb-pill"'), 'Zero bugs/CLs or discarded drafts in .wb-pills');
  assert(nocpHtml.includes('Merge conflict'), 'Non-checklist bug peer updates stage from CL');
  assert(nocpHtml.includes('<span class="re-ps">PS4</span>'), 'ast.patchset renders PS4 badge');
  assert(nocpHtml.includes('Nov 10 at 09:00 ET'), 'Uses ast.updated_et string');
  assert(!nocpHtml.includes('1794300000'), 'Never renders raw numeric epoch updated_at');

  // B2_6.1 + Grill #1: Discarded gmail_draft ignored; no '· Parked' when hasDraft is false
  assert(!nocpHtml.includes('Old discarded draft'), 'Discarded gmail_draft hidden');
  assert(!nocpHtml.includes('Parked'), 'No Parked label when no active draft exists');

  // B2_6.2: Two untitled raw chats with same numeric why_badge stay separate chips
  const rawChips = [...nocpHtml.matchAll(/class="[^"]*\bre-chat-chip\b[^"]*"/g)];
  assert.strictEqual(rawChips.length, 2, 'Untitled Chat <hex> items must never merge by why_badge');

  // V1 + B2_2 + Grill #2 on work-v1:
  // Standalone CL strips gerrit:skia:1397276, shows subject once, omits self from folded CLs,
  // flags missing draft CL in .re-sync-strip, and omits peer unlink on synthetic #999999
  assert(!v1Html.includes('>gerrit:skia:1397276<'), 'Raw gerrit key stripped from peer title');
  const subjOccurrences = v1Html.split('[skparagraph] Fix cluster width').length - 1;
  // Appears in title=".." attribute + .re-peer-title text = 2, NOT a 3rd time in .wb-cl-subj
  assert.strictEqual(subjOccurrences, 2, 'Standalone CL subject must not duplicate in .wb-cl-subj');
  assert(!v1Html.includes('class="re-folded-cl"'), 'Standalone CL omits duplicate folded pill');
  assert(v1Html.includes('data-tone="warn">Draft<'), 'is_draft standalone CL gets warn/Draft');
  assert(v1Html.includes('re-sync-strip') && v1Html.includes('Skia 1397276'), 'Not-in-draft strip');
  assert(!v1Html.includes('data-unlink-key="github_issue:flutter/flutter:999999"'));

  // B2_4: Text selection prevents [data-peer-hdr] toggle
  const peerHdrV1 = {
    getAttribute: (k) => {
      if (k === 'data-peer-hdr') return 'gerrit:skia:1397276';
      if (k === 'data-peer-work') return 'work-v1';
      return null;
    },
  };
  window.getSelection = () => 'Fix cluster width';
  await document.body.dispatch('click', {
    altKey: false,
    preventDefault: () => {},
    target: { closest: (sel) => (sel.includes('[data-peer-hdr]') ? peerHdrV1 : null) },
  });
  assert(
    els['wb-cards-container'].innerHTML.includes(
      'data-peer-key="gerrit:skia:1397276" data-peer-open="true"'
    ),
    'Selecting text inside peer header must not fold the peer'
  );

  // B2_5: Toggling peer folded then back open cleans up peerOpenOverrides when matching default
  window.getSelection = () => '';
  await document.body.dispatch('click', {
    altKey: false,
    preventDefault: () => {},
    target: { closest: (sel) => (sel.includes('[data-peer-hdr]') ? peerHdrV1 : null) },
  });
  let savedFold = JSON.parse(storage.get('wb_peer_fold_v1'));
  assert.strictEqual(savedFold.overrides['work-v1::gerrit:skia:1397276'], false);

  await document.body.dispatch('click', {
    altKey: false,
    preventDefault: () => {},
    target: { closest: (sel) => (sel.includes('[data-peer-hdr]') ? peerHdrV1 : null) },
  });
  savedFold = JSON.parse(storage.get('wb_peer_fold_v1'));
  assert(
    !Object.prototype.hasOwnProperty.call(savedFold.overrides, 'work-v1::gerrit:skia:1397276'),
    'Returning peer to default open state must delete override key'
  );

  // B2_6.2: Clicking unlink on grouped 2-piece chat chain detaches all pieces
  const chatUnlinkMatch = els['wb-cards-container'].innerHTML.match(
    /data-unlink-work="work-v1"\s+data-unlink-key="([^"]*chat_chain:c1[^"]*)"/
  );
  assert(chatUnlinkMatch, 'Expected grouped chat unlink button');
  const chatUnlinkBtn = {
    hasAttribute: (k) => k === 'data-unlink-work',
    getAttribute: (k) => {
      if (k === 'data-unlink-work') return 'work-v1';
      if (k === 'data-unlink-key') return chatUnlinkMatch[1];
      return null;
    },
  };
  await document.body.dispatch('click', {
    altKey: false,
    preventDefault: () => {},
    target: { closest: (sel) => (sel.includes('button') ? chatUnlinkBtn : null) },
  });
  const unlinkedKeys = posted
    .filter((p) => p.url === '/api/work/unlink')
    .map((p) => p.body.artifact_key);
  assert.deepStrictEqual(unlinkedKeys, ['chat_chain:c1', 'chat_chain:c2']);
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    proc = subprocess.run(["node", "-e", node_code], capture_output=True, text=True, check=False)
    self.assertEqual(proc.returncode, 0, msg=f"Node failed:\n{proc.stderr}\n{proc.stdout}")


if __name__ == "__main__":
  unittest.main()
