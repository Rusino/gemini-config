#!/usr/bin/env python3
"""clickable_links_guard.py — block chat replies containing unlinked entity mentions.

Requires every mentioned Gerrit CL, Piper CL, patchset (PS<N>), GitHub PR/issue,
Buganizer/crbug issue, or CI build in stop_guard.py to be formatted as a clickable
[label](https://...) link at least once in the reply and in every table row or list
item, while strictly banning 5-8 digit cl/<N> Gerrit typos and bare xxxxxxxx_xxxxxxxx
comment/draft IDs anywhere in the reply.
"""

from __future__ import annotations

import re

FENCED_CODE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")
HTTP_URL_RE = re.compile(r"https?://[^\s<>)\]]+")
# Allow one level of nested brackets in link labels (e.g. "[skparagraph]" in CL subjects)
MD_LINK_RE = re.compile(r"\[((?:[^\[\]\n]|\[[^\[\]\n]*\])+)\]\(\s*([^\s)]+)\s*\)")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# Exempt literal "[HH:MM] ⦿ ..." chat titles when quoted in prose
LIFECYCLE_TITLE_SPAN_RE = re.compile(
    r"\[\d{1,2}:\d{2}\]\s*(?:‹✓›|«»|⏳|[✓⦿▸«»])[^\n\"'`]{0,60}"
)
ATTESTATION_MARKER_RE = re.compile(
    r"(?:\(Run Attestation\)"
    r"|# Flutter Paragraph Engine Test Comparison"
    r"|# WebParagraph Corpus Report"
    r"|# SkParagraph Test Comparison)"
)
ATTESTATION_ROW_RE = re.compile(
    r"^(?:\|.*\|$|##\s+`[^`]+`\s+vs\s+`[^`]+`$)", re.MULTILINE
)
CLI_OR_PATH_SPAN_RE = re.compile(
    r"^(?:/|~/|\./|\.\./|file://|"
    r"(?:python3?|git|gh|env|cd|go|ninja|bazel|blaze|curl|rg|grep|ls|cat)\s+)"
    r"|\s--[A-Za-z0-9_-]+"
    r"|\.(?:py|sh|js|ts|cpp|cc|h|dart|json|jsonl|md)\b"
)
LIST_ITEM_START_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
HEADING_START_RE = re.compile(r"^\s*#+\s")
TABLE_ROW_START_RE = re.compile(r"^\s*\|")
BARE_GERRIT_CL_SHORTLINK_RE = re.compile(r"^cl/\d{5,8}$", re.IGNORECASE)

GERRIT_HOST_URL_RE = re.compile(
    r"^https?://(?:[a-z0-9.-]*-review\.(?:googlesource|git\.corp\.google)\.com"
    r"|(?:www\.)?(?:crrev|skrev)\.com)/\S+",
    re.IGNORECASE,
)
PIPER_HOST_URL_RE = re.compile(
    r"^https?://(?:cl/|critique\.corp\.google\.com/)\S+",
    re.IGNORECASE,
)

# Entity patterns mapped to (kind, compiled_regex)
ENTITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "gerrit_cl",
        re.compile(
            r"(?<![/\w.-])Gerrit\s+(?:CL\s+)?#?(\d{5,8})\b"
            r"|(?<![/\w.-])(?:crrev(?:\.com)?/c|skrev(?:\.com)?)/(\d{5,8})\b"
            r"|(?<![/\w.-])cl/(\d{5,8})\b",
            re.IGNORECASE,
        ),
    ),
    (
        "gerrit_cl_cs",
        re.compile(r"(?<![/\w.-])(?:CL|\u0421\u041b)\s*#?(\d{5,8})\b"),
    ),
    (
        "piper_cl",
        re.compile(
            r"(?<![/\w.-])(?:CL|\u0421\u041b)\s*#?(\d{9,10})\b"
            r"|(?i:(?<![/\w.-])cl/(\d{9,10})\b)"
        ),
    ),
    (
        "patchset",
        re.compile(
            r"(?<![/\w.-])PS\s?(\d{1,3})\b"
            r"|(?i:(?<![/\w.-])patchset\s+#?(\d{1,3})\b)"
        ),
    ),
    (
        "comment_id",
        re.compile(
            r"(?<![0-9a-fA-F_/.-])\b([0-9a-f]{8}_[0-9a-f]{8})\b(?![0-9a-fA-F_.-])"
        ),
    ),
    (
        "github_pr",
        re.compile(
            r"(?<![/\w.-])PR\s*#(\d{3,7})\b|(?<![/\w.-])PR\s+(\d{3,7})\b",
            re.IGNORECASE,
        ),
    ),
    (
        "github_issue",
        re.compile(
            r"(?<![/\w.-])Issue\s*#(\d{3,7})\b"
            r"|(?<![/\w.-])(?:flutter|skia)#(\d{4,7})\b",
            re.IGNORECASE,
        ),
    ),
    (
        "bug",
        re.compile(
            r"(?<![/\w.-])(?:b|crbug(?:\.com)?)/(\d{5,12})\b", re.IGNORECASE
        ),
    ),
    (
        "build",
        re.compile(r"(?<![/\w.-])build\s+#?(8\d{18})\b", re.IGNORECASE),
    ),
)


def _mask_span(buf: list[str], start: int, end: int) -> None:
  for i in range(start, end):
    if buf[i] != "\n":
      buf[i] = " "


def _url_matches_entity(
    url: str, kind: str, ident: str, *, in_md_link: bool = False
) -> bool:
  u = url.rstrip(".,;:!?)")
  if kind in ("gerrit_cl", "gerrit_cl_cs"):
    # Reject http://cl/<5-8 digits> because cl/ opens Google3 Piper, not Gerrit
    return bool(
        GERRIT_HOST_URL_RE.match(u)
        and re.search(rf"(?<!\d){re.escape(ident)}(?!\d)", u)
    )
  if kind == "piper_cl":
    return bool(
        PIPER_HOST_URL_RE.match(u)
        and re.search(rf"(?<!\d){re.escape(ident)}(?!\d)", u)
    )
  if kind == "patchset":
    if PIPER_HOST_URL_RE.match(u):
      return True
    # Require Gerrit URLs to include the patchset number (/CL/PS or /CL/base..PS)
    return bool(
        GERRIT_HOST_URL_RE.match(u)
        and re.search(
            rf"/\d{{5,8}}/(?:\d+\.\.)?{re.escape(ident)}(?:[/?#]|$)", u
        )
    )
  if kind == "comment_id":
    if not GERRIT_HOST_URL_RE.match(u):
      return False
    # Unpublished drafts lack /comment/<id>, so allow [draft <id>](patchset_url)
    # while rejecting bare hex IDs that merely sit near a base CL URL
    return True if in_md_link else (ident.lower() in u.lower())
  if kind == "github_pr":
    return bool(
        re.match(
            rf"^https?://github\.com/[^/\s)]+/[^/\s)]+/pull/{re.escape(ident)}(?!\d)",
            u,
            re.IGNORECASE,
        )
    )
  if kind == "github_issue":
    return bool(
        re.match(
            r"^https?://(?:github\.com/[^/\s)]+/[^/\s)]+/(?:issues|pull)"
            r"|[a-z0-9.-]*issues\.chromium\.org/(?:u/\d+/)?issues"
            r"|issues\.skia\.org/(?:u/\d+/)?issues"
            r"|[a-z0-9.-]*issuetracker\.google\.com/(?:u/\d+/)?issues"
            rf"|crbug\.com)/{re.escape(ident)}(?!\d)",
            u,
            re.IGNORECASE,
        )
    )
  if kind == "bug":
    return bool(
        re.match(
            r"^https?://(?:(?:b|buganizer|partnerissuetracker)\.corp\.google\.com"
            r"|[a-z0-9.-]*issuetracker\.google\.com"
            r"|[a-z0-9.-]*issues\.chromium\.org"
            r"|issues\.skia\.org|crbug\.com|b)/\S*"
            rf"(?<!\d){re.escape(ident)}(?!\d)",
            u,
            re.IGNORECASE,
        )
    )
  if kind == "build":
    return bool(
        re.match(
            r"^https?://(?:ci\.chromium\.org|milo\.corp\.google\.com)/\S*"
            rf"(?<!\d){re.escape(ident)}(?!\d)",
            u,
            re.IGNORECASE,
        )
    )
  return False


def _segment_markdown_blocks(text: str) -> list[tuple[str, int, int]]:
  """Splits `text` into ('table_row' | 'list_item' | 'prose', start, end) blocks."""
  blocks: list[tuple[str, int, int]] = []
  pos = 0
  cur_kind = ""
  cur_start = 0
  for raw_line in text.splitlines(keepends=True):
    line_start = pos
    line_end = pos + len(raw_line)
    pos = line_end
    stripped = raw_line.strip()
    if not stripped:
      if cur_kind:
        blocks.append((cur_kind, cur_start, line_start))
        cur_kind = ""
      continue
    if TABLE_ROW_START_RE.match(raw_line):
      if cur_kind:
        blocks.append((cur_kind, cur_start, line_start))
        cur_kind = ""
      blocks.append(("table_row", line_start, line_end))
      continue
    if HEADING_START_RE.match(raw_line):
      if cur_kind:
        blocks.append((cur_kind, cur_start, line_start))
        cur_kind = ""
      blocks.append(("prose", line_start, line_end))
      continue
    if LIST_ITEM_START_RE.match(raw_line):
      if cur_kind:
        blocks.append((cur_kind, cur_start, line_start))
      cur_kind = "list_item"
      cur_start = line_start
      continue
    if not cur_kind:
      cur_kind = "prose"
      cur_start = line_start
  if cur_kind:
    blocks.append((cur_kind, cur_start, len(text)))
  return blocks


def _url_matches_patchset_for_cls(
    url: str, ps_ident: str, gerrit_cls: set[str]
) -> bool:
  """Verifies a patchset link matches `ps_ident` and, when present, a CL in `gerrit_cls`."""
  if not _url_matches_entity(url, "patchset", ps_ident, in_md_link=True):
    return False
  if not gerrit_cls or PIPER_HOST_URL_RE.match(url.rstrip(".,;:!?)")):
    return True
  u = url.rstrip(".,;:!?)")
  return any(
      bool(
          re.search(
              rf"/{re.escape(cl_id)}/(?:\d+\.\.)?{re.escape(ps_ident)}(?:[/?#]|$)",
              u,
          )
      )
      for cl_id in gerrit_cls
  )


def find_unlinked_entities(text: str) -> list[str]:
  """Returns entity mentions violating response-level or table/list link invariants."""
  if not text or not text.strip():
    return []

  # Mask fenced code blocks in both URL extraction and entity scanning
  no_fences = list(text)
  for m in FENCED_CODE_RE.finditer(text):
    _mask_span(no_fences, m.start(), m.end())
  clean_base = "".join(no_fences)

  url_spans = [
      (m.start(), m.end(), m.group(0)) for m in HTTP_URL_RE.finditer(clean_base)
  ]
  # Store (link_start, label_start, label_end, link_end, target) for Markdown links
  md_links: list[tuple[int, int, int, int, str]] = []
  scan_buf = list(clean_base)

  if ATTESTATION_MARKER_RE.search(clean_base):
    for m in ATTESTATION_ROW_RE.finditer(clean_base):
      _mask_span(scan_buf, m.start(), m.end())

  for m in MD_LINK_RE.finditer(clean_base):
    label_start, label_end = m.start(1), m.end(1)
    target = m.group(2).strip()
    # Mask conversation:// and file:// links completely (labels are chat titles or filenames)
    if target.startswith(("conversation://", "file://")):
      _mask_span(scan_buf, m.start(), m.end())
    else:
      md_links.append((m.start(), label_start, label_end, m.end(), target))
      # Mask only the (target) tail so entities inside [label] are validated against target
      _mask_span(scan_buf, label_end, m.end())

  for u_start, u_end, _ in url_spans:
    _mask_span(scan_buf, u_start, u_end)

  for m in LIFECYCLE_TITLE_SPAN_RE.finditer("".join(scan_buf)):
    _mask_span(scan_buf, m.start(), m.end())

  # Mask CLI commands and file paths in inline code while keeping bare backticked IDs
  cur_scan = "".join(scan_buf)
  for m in INLINE_CODE_RE.finditer(cur_scan):
    inner = m.group(1).strip()
    if CLI_OR_PATH_SPAN_RE.search(inner):
      _mask_span(scan_buf, m.start(), m.end())

  scan_text = "".join(scan_buf)
  blocks = _segment_markdown_blocks(clean_base)

  def _find_block(pos: int) -> tuple[str, int, int]:
    for b_kind, b_lo, b_hi in blocks:
      if b_lo <= pos < b_hi:
        return b_kind, b_lo, b_hi
    return "prose", 0, len(clean_base)

  # Index Gerrit CL IDs mentioned in each block so `CL 1414017 PS1` checks 1414017's PS1 URL
  block_gerrit_cls: dict[tuple[int, int], set[str]] = {}
  for kind, pat in ENTITY_PATTERNS:
    if kind not in ("gerrit_cl", "gerrit_cl_cs"):
      continue
    for m in pat.finditer(scan_text):
      ident = next((g for g in m.groups() if g is not None), "")
      if ident:
        _, b_lo, b_hi = _find_block(m.start())
        block_gerrit_cls.setdefault((b_lo, b_hi), set()).add(ident)

  missing: list[tuple[int, str]] = []
  for kind, pat in ENTITY_PATTERNS:
    for m in pat.finditer(scan_text):
      ident = next((g for g in m.groups() if g is not None), "")
      if not ident:
        continue
      raw_tok = m.group(0).strip()
      m_start, m_end = m.start(), m.end()

      # 5-8 digit cl/<N> opens Google3 Piper, not Gerrit; ban it everywhere in the reply
      if kind == "gerrit_cl" and BARE_GERRIT_CL_SHORTLINK_RE.match(raw_tok):
        missing.append((m_start, raw_tok))
        continue

      matched_link = False
      for _, l_start, l_end, _, target in md_links:
        if l_start <= m_start and m_end <= l_end:
          if _url_matches_entity(target, kind, ident, in_md_link=True):
            matched_link = True
            break
      if matched_link:
        continue

      # Raw xxxxxxxx_xxxxxxxx comment/draft IDs outside [label](https://...) are banned everywhere
      if kind == "comment_id":
        missing.append((m_start, raw_tok))
        continue

      b_kind, b_lo, b_hi = _find_block(m_start)
      cls_in_block = block_gerrit_cls.get((b_lo, b_hi), set())

      # Check clickable [label](https://...) links within the current table row / list item / block
      for mk_start, _, _, mk_end, target in md_links:
        if mk_start >= b_lo and mk_end <= b_hi:
          if kind == "patchset":
            if _url_matches_patchset_for_cls(target, ident, cls_in_block):
              matched_link = True
              break
          elif _url_matches_entity(target, kind, ident, in_md_link=True):
            matched_link = True
            break

      # Prose paragraphs may refer by name to an entity already linked [label](https://...)
      # at least once anywhere in the response (table rows and list items must link locally)
      if not matched_link and b_kind == "prose":
        for _, _, _, _, target in md_links:
          if kind == "patchset":
            if _url_matches_patchset_for_cls(target, ident, cls_in_block):
              matched_link = True
              break
          elif _url_matches_entity(target, kind, ident, in_md_link=True):
            matched_link = True
            break

      if not matched_link:
        missing.append((m_start, raw_tok))

  missing.sort(key=lambda x: x[0])
  return list(dict.fromkeys(tok for _, tok in missing))


def clickable_links_reason(reply_text: str) -> str:
  """Formats the [CLICKABLE LINKS GUARD] block message if unlinked entities are present"""
  unlinked = find_unlinked_entities(reply_text)
  if not unlinked:
    return ""
  shown = ", ".join(f"`{t}`" for t in unlinked[:8])
  # Jetski keeps the earlier step visible, so append a short postscript instead of duplicating
  header = (
      "[CLICKABLE LINKS GUARD] Your reply contains entity mentions that were never "
      "formatted as a clickable `[label](https://...)` link in this response (or are "
      "unlinked inside a table row / list item, or use banned `cl/<5-8 digits>` / bare "
      f"`xxxxxxxx_xxxxxxxx` comment IDs): {shown}.\n"
      "Do NOT repeat or rewrite your earlier message (it is already rendered above in chat). "
      "Append ONLY a compact 1-3 line links postscript (in the chat language, e.g. `**Links:**`) "
      "with clickable Markdown links (`[label](https://...)`) for those mentions (or a 1-line "
      "note if a token was an abstract placeholder with no concrete URL; if this turn recorded "
      "`decision_guard` acks, retain their triage lines too), e.g.:\n"
  )
  return (
      header
      + "- `[Skia CL 1414016 PS1](https://skia-review.googlesource.com/c/skia/+/1414016/1)` "
      "(never `cl/<5-8 digits>`: `cl/` opens Google3 Piper, not Gerrit)\n"
      "- Published comment: "
      "`[comment](https://skia-review.googlesource.com/c/skia/+/1414016/comment/<id>/)`; "
      "unpublished draft: link to the patchset URL (never output a bare hex draft ID)\n"
      "- `[PR #194980](https://github.com/flutter/flutter/pull/194980)`, "
      "`[flutter#155062](https://github.com/flutter/flutter/issues/155062)`\n"
      "- `[b/580852205](https://b.corp.google.com/issues/580852205)`, "
      "`[crbug/576664769](https://issues.chromium.org/issues/576664769)`\n"
      "- Also link any mentioned try jobs or builds (`ci.chromium.org/b/...`, "
      "`task-scheduler.skia.org/task/...`, or `?tab=checks`)."
  )
