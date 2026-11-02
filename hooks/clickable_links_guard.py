#!/usr/bin/env python3
"""clickable_links_guard.py — block chat replies containing bare unlinked entity IDs.

Checks the final planner reply in stop_guard.py for bare mentions of Gerrit CLs,
Piper CLs, patchsets (PS<N>), Gerrit comment/draft hex IDs (xxxxxxxx_xxxxxxxx),
GitHub PRs/issues, Buganizer/crbug issues, and Buildbucket build IDs that lack a
clickable URL for that object nearby.
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
BLOCK_BOUNDARY_RE = re.compile(r"\n\s*\n|\n\s*(?:[-*+]|\d+\.)\s|\n#")

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

NEARBY_CHAR_WINDOW = 220


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


def find_unlinked_entities(text: str) -> list[str]:
  """Returns bare entity tokens in `text` that have no matching URL nearby"""
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
  md_links: list[tuple[int, int, str]] = []
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
      md_links.append((label_start, label_end, target))
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

  # Precompute paragraph/bullet block boundaries so nearby URLs never cross bullets or paragraphs
  boundaries = [0]
  for m in BLOCK_BOUNDARY_RE.finditer(clean_base):
    boundaries.append(m.start())
    boundaries.append(m.end())
  boundaries.append(len(clean_base))

  def _block_bounds(pos: int) -> tuple[int, int]:
    lo, hi = 0, len(clean_base)
    for b in boundaries:
      if b <= pos:
        lo = b
      elif b > pos:
        hi = b
        break
    return lo, hi

  missing: list[tuple[int, str]] = []
  for kind, pat in ENTITY_PATTERNS:
    for m in pat.finditer(scan_text):
      ident = next((g for g in m.groups() if g is not None), "")
      if not ident:
        continue
      m_start, m_end = m.start(), m.end()
      matched_link = False

      for l_start, l_end, target in md_links:
        if l_start <= m_start and m_end <= l_end:
          if _url_matches_entity(target, kind, ident, in_md_link=True):
            matched_link = True
            break
      if matched_link:
        continue

      b_lo, b_hi = _block_bounds(m_start)
      win_lo = max(b_lo, m_start - NEARBY_CHAR_WINDOW)
      win_hi = min(b_hi, m_end + NEARBY_CHAR_WINDOW)
      for u_start, u_end, u_str in url_spans:
        if u_end >= win_lo and u_start <= win_hi:
          if _url_matches_entity(u_str, kind, ident):
            matched_link = True
            break
      if not matched_link:
        missing.append((m_start, m.group(0).strip()))

  missing.sort(key=lambda x: x[0])
  return list(dict.fromkeys(tok for _, tok in missing))


def clickable_links_reason(reply_text: str) -> str:
  """Formats the [CLICKABLE LINKS GUARD] block message if bare entities are present"""
  unlinked = find_unlinked_entities(reply_text)
  if not unlinked:
    return ""
  shown = ", ".join(f"`{t}`" for t in unlinked[:8])
  return (
      "[CLICKABLE LINKS GUARD] Your reply contains unlinked entity or draft/patchset "
      f"mentions without a matching URL nearby: {shown}.\n"
      "Rewrite your reply so EVERY mention of a Gerrit CL, patchset (`PS<N>`), "
      "comment/draft (`xxxxxxxx_xxxxxxxx`), GitHub PR/issue, Buganizer/`crbug` issue, "
      "or try job / build is a clickable Markdown link (`[label](https://...)`), e.g.:\n"
      "- `[Gerrit 1414016 PS1](https://skia-review.googlesource.com/c/skia/+/1414016/1)` "
      "(never `http://cl/<5-8 digits>`: `cl/` opens Google3 Piper, not Gerrit)\n"
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
