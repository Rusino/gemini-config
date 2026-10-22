#!/usr/bin/env python3
# Copyright 2026 Google LLC
"""Generate a rendered-Markdown HTML redline (was -> now diff) between two document versions.

Supports:
1. Two explicit files or git/transcript specs:
     redline.py old.md new.md -o diff.html
     redline.py HEAD~1:doc.md HEAD:doc.md -o diff.html
2. Single file (default base = last version the user saw in transcript, or git HEAD/HEAD~1):
     redline.py doc.md -o diff.html
     redline.py doc.md --chat <conv_id> --base-turn -1 -o diff.html
3. Google Doc / Drive JSON or transcript snapshots:
     redline.py --gdoc <doc_id> new.md --chat <conv_id> -o diff.html
4. Listing available transcript snapshots for a file or Google Doc:
     redline.py --list-snapshots doc.md [--chat <conv_id>]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import difflib
import html
import json
import os
import re
import subprocess
import sys
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 1. Google Drive / Google Docs JSON & Escape Normalization
# ---------------------------------------------------------------------------

_DRIVE_ESCAPE_RE = re.compile(r"\\([_~<>#*+\-\[\]()!|`.])")


def unescape_drive_markdown(text: str) -> str:
  """Remove backslash escapes added by Google Drive's markdown exporter."""
  return _DRIVE_ESCAPE_RE.sub(r"\1", text)


def _extract_gdoc_paragraph_md(
    para: dict[str, Any], lists_meta: dict[str, Any]
) -> str:
  style_type = (
      para.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
  )
  prefix = ""
  if style_type.startswith("HEADING_"):
    try:
      level = int(style_type.split("_")[1])
      prefix = "#" * max(1, min(6, level)) + " "
    except (IndexError, ValueError):
      pass
  elif "bullet" in para:
    bullet = para["bullet"]
    nesting = max(0, int(bullet.get("nestingLevel", 0)))
    list_id = bullet.get("listId", "")
    levels = (
        lists_meta.get(list_id, {})
        .get("listProperties", {})
        .get("nestingLevels", [])
    )
    glyph = levels[min(nesting, len(levels) - 1)] if levels else {}
    is_ordered = "glyphType" in glyph and glyph.get("glyphType") not in (
        "GLYPH_TYPE_UNSPECIFIED",
        "NONE",
    )
    marker = "1. " if is_ordered else "- "
    prefix = ("  " * nesting) + marker

  parts: list[str] = []
  for el in para.get("elements", []):
    tr = el.get("textRun")
    if not tr:
      continue
    raw = tr.get("content", "")
    if not raw:
      continue
    # Strip trailing newline (handled at paragraph level)
    trailing_nl = raw.endswith("\n")
    content = raw[:-1] if trailing_nl else raw
    if not content:
      continue
    ts = tr.get("textStyle", {})
    font_family = ts.get("weightedFontFamily", {}).get("fontFamily", "")
    is_code = any(
        mono in font_family
        for mono in ("Mono", "Courier", "Consolas", "Menlo", "Inconsolata")
    )
    is_bold = bool(ts.get("bold"))
    is_italic = bool(ts.get("italic"))
    link_url = ts.get("link", {}).get("url")

    # Preserve leading/trailing spaces outside inline markers
    lead = len(content) - len(content.lstrip(" "))
    trail = len(content) - len(content.rstrip(" "))
    core = content.strip(" ")
    if core:
      if is_code:
        core = f"`{core}`"
      if is_bold:
        core = f"**{core}**"
      if is_italic:
        core = f"*{core}*"
      if link_url:
        core = f"[{core}]({link_url})"
      content = (" " * lead) + core + (" " * trail)
    parts.append(content)

  line = "".join(parts).rstrip()
  if not line:
    return ""
  return prefix + line


def parse_json_doc_payload(raw_json: str) -> Optional[str]:
  """If raw_json is a Drive read_file_content or Docs read_doc JSON, return Markdown."""
  stripped = raw_json.lstrip()
  if not stripped.startswith("{"):
    return None
  try:
    obj = json.loads(stripped)
  except json.JSONDecodeError:
    return None
  if not isinstance(obj, dict):
    return None

  # Case 1: google_drive_google_drive read_file_content -> {"fileContent": "..."}
  if isinstance(obj.get("fileContent"), str):
    return unescape_drive_markdown(obj["fileContent"])

  # Case 2: google_docs_google_docs read_doc -> {"content": {"tabs": [...]}} or {"body": ...}
  doc = obj.get("content") if isinstance(obj.get("content"), dict) else obj
  body_content: list[dict[str, Any]] = []
  lists_meta: dict[str, Any] = {}
  if isinstance(doc.get("tabs"), list) and doc["tabs"]:
    doc_tab = doc["tabs"][0].get("documentTab", {})
    body_content = doc_tab.get("body", {}).get("content", [])
    lists_meta = doc_tab.get("lists", {})
  elif isinstance(doc.get("body"), dict):
    body_content = doc["body"].get("content", [])
    lists_meta = doc.get("lists", {})

  if not body_content:
    return None

  out_lines: list[str] = []
  for el in body_content:
    if "paragraph" in el:
      md_line = _extract_gdoc_paragraph_md(el["paragraph"], lists_meta)
      out_lines.append(md_line)
    elif "table" in el:
      rows = el["table"].get("tableRows", [])
      for r_idx, row in enumerate(rows):
        cells: list[str] = []
        for cell in row.get("tableCells", []):
          cell_paras = [
              _extract_gdoc_paragraph_md(c_el["paragraph"], lists_meta)
              for c_el in cell.get("content", [])
              if "paragraph" in c_el
          ]
          cells.append(" ".join(p for p in cell_paras if p).strip())
        out_lines.append("| " + " | ".join(cells) + " |")
        if r_idx == 0 and cells:
          out_lines.append("| " + " | ".join("---" for _ in cells) + " |")
      out_lines.append("")

  # Collapse 3+ consecutive empty lines into a single blank line between blocks
  normalized: list[str] = []
  for ln in out_lines:
    if not ln and (not normalized or not normalized[-1]):
      continue
    normalized.append(ln)
    if not ln.startswith(("- ", "1. ", "|")):
      normalized.append("")
  text = "\n".join(normalized).strip() + "\n"
  return text


# ---------------------------------------------------------------------------
# 2. Transcript & Git Snapshot Resolution
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
  kind: str
  step_index: int
  user_step: int
  turn_number: int
  timestamp: str
  content: str
  label: str


def _unquote_arg(val: Any) -> Any:
  if isinstance(val, str) and len(val) >= 2 and val.startswith('"') and val.endswith('"'):
    try:
      return json.loads(val)
    except json.JSONDecodeError:
      return val[1:-1]
  return val


def _parse_json_arg(val: Any) -> Any:
  val = _unquote_arg(val)
  if isinstance(val, str):
    try:
      return json.loads(val)
    except json.JSONDecodeError:
      return val
  return val


def find_transcript_path(
    target_path: Optional[str] = None,
    chat_id: Optional[str] = None,
    explicit_transcript: Optional[str] = None,
) -> Optional[str]:
  """Locate transcript_full.jsonl (or transcript.jsonl) for the given chat or file."""
  if explicit_transcript and os.path.isfile(explicit_transcript):
    return explicit_transcript

  brain_roots = [
      os.path.expanduser("~/.gemini/jetski/brain"),
      os.path.expanduser("~/.gemini/antigravity/brain"),
  ]
  candidates: list[str] = []
  if chat_id:
    for br in brain_roots:
      if os.path.isdir(os.path.join(br, chat_id)):
        candidates.append(chat_id)
      elif os.path.isdir(br):
        for entry in sorted(os.listdir(br)):
          if entry.startswith(chat_id):
            candidates.append(entry)

  if target_path:
    abs_target = os.path.abspath(target_path)
    m = re.search(r"/brain/([0-9a-fA-F\-]{36})/", abs_target)
    if m:
      candidates.append(m.group(1))

  env_cid = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
  if env_cid:
    candidates.append(env_cid)

  for cid in candidates:
    for br in brain_roots:
      log_dir = os.path.join(br, cid, ".system_generated", "logs")
      for fname in ("transcript_full.jsonl", "transcript.jsonl"):
        p = os.path.join(log_dir, fname)
        if os.path.isfile(p):
          return p
  return None


def _paths_match(candidate: str, target: str) -> bool:
  if not candidate or not target:
    return False
  if candidate == target:
    return True
  if os.path.abspath(candidate) == os.path.abspath(target):
    return True
  if os.path.realpath(candidate) == os.path.realpath(target):
    return True
  return False


def _extract_read_file_output(generic_content: str) -> Optional[str]:
  # Support both Jetski read_file ("Showing lines 1-N of N") and Antigravity view_file
  m = re.search(r"Showing lines 1-(\d+) of (\d+)\n(.*)", generic_content, re.S)
  if m and m.group(1) == m.group(2):
    body = m.group(3)
  else:
    m_view = re.search(
        r"Total Lines: (\d+)\n(?:Total Bytes: \d+\n)?Showing lines 1 to (\d+)\n"
        r"The following code has been modified[^\n]*\n(.*)",
        generic_content,
        re.S,
    )
    if not m_view or m_view.group(1) != m_view.group(2):
      return None
    body = m_view.group(3)

  raw_lines = body.splitlines()
  no_nl = False
  cleaned: list[str] = []
  for rl in raw_lines:
    if rl == "(no newline at end of file)":
      no_nl = True
      continue
    cm = re.match(r"^\d+: (.*)", rl)
    cleaned.append(cm.group(1) if cm else rl)
  text = "\n".join(cleaned)
  if not no_nl:
    text += "\n"
  return text


def _extract_mcp_payload(generic_content: str) -> Optional[str]:
  m = re.search(r"The output was large and was saved to: file://(/[^\s]+)", generic_content)
  if m and os.path.isfile(m.group(1)):
    try:
      with open(m.group(1), encoding="utf-8") as f:
        return f.read()
    except OSError:
      return None
  # Otherwise strip "Created At: ...\nCompleted At: ...\n" header
  lines = generic_content.splitlines()
  start = 0
  while start < len(lines) and (
      lines[start].startswith("Created At:")
      or lines[start].startswith("Completed At:")
  ):
    start += 1
  return "\n".join(lines[start:])


def reconstruct_file_snapshots(
    transcript_path: str,
    target_path: Optional[str] = None,
    gdoc_id: Optional[str] = None,
) -> list[Snapshot]:
  """Replay write_file, edit_file, read_file, and MCP Doc calls from a transcript."""
  with open(transcript_path, encoding="utf-8") as f:
    steps = [json.loads(ln) for ln in f if ln.strip()]

  current: Optional[str] = None
  snapshots: list[Snapshot] = []
  last_user_step = 0
  turn_number = 0
  pending_pre_read_edits: list[tuple[int, int, int, str, str, str]] = []

  for idx, st in enumerate(steps):
    sidx = int(st.get("step_index", idx))
    ts = st.get("created_at", "")
    if st.get("type") == "USER_INPUT":
      turn_number += 1
      last_user_step = sidx
      if current is not None:
        snapshots.append(
            Snapshot(
                kind="turn_boundary",
                step_index=sidx,
                user_step=last_user_step,
                turn_number=turn_number,
                timestamp=ts,
                content=current,
                label=f"turn {turn_number} (step {sidx})",
            )
        )

    tool_calls = st.get("tool_calls", [])
    for j, tc in enumerate(tool_calls):
      name = tc.get("name", "")
      args = tc.get("args", {})
      gen_step = (
          steps[idx + 1 + j]
          if (idx + 1 + j < len(steps) and steps[idx + 1 + j].get("type") == "GENERIC")
          else None
      )
      if gen_step and gen_step.get("status") == "ERROR":
        continue
      gen_content = gen_step.get("content", "") if gen_step else ""

      if target_path and name in (
          "write_file",
          "write_to_file",
          "edit_file",
          "replace_file_content",
          "multi_replace_file_content",
          "read_file",
          "view_file",
      ):
        p = str(
            _unquote_arg(
                args.get("Path")
                or args.get("TargetFile")
                or args.get("AbsolutePath")
                or ""
            )
        )
        if not _paths_match(p, target_path):
          continue

        if name in ("write_file", "write_to_file"):
          raw_c = args.get("Content") if "Content" in args else args.get("CodeContent", "")
          c = str(_unquote_arg(raw_c))
          append = str(_unquote_arg(args.get("Append", "false"))).lower() == "true"
          current = (current or "") + c if append else c
          pending_pre_read_edits.clear()
          snapshots.append(
              Snapshot(
                  kind=name,
                  step_index=sidx,
                  user_step=last_user_step,
                  turn_number=turn_number,
                  timestamp=ts,
                  content=current,
                  label=f"{name} at step {sidx} (turn {turn_number})",
              )
          )
        elif name in ("edit_file", "replace_file_content", "multi_replace_file_content"):
          if name == "multi_replace_file_content":
            chunks = _parse_json_arg(args.get("ReplacementChunks", []))
            pairs = [
                (
                    str(ch.get("TargetContent", "")),
                    str(ch.get("ReplacementContent", "")),
                )
                for ch in (chunks if isinstance(chunks, list) else [])
                if isinstance(ch, dict)
            ]
          else:
            t_raw = args.get("Target") if "Target" in args else args.get("TargetContent", "")
            r_raw = (
                args.get("Replacement")
                if "Replacement" in args
                else args.get("ReplacementContent", "")
            )
            pairs = [(str(_unquote_arg(t_raw)), str(_unquote_arg(r_raw)))]

          for t, r in pairs:
            if current is not None:
              if t and t in current:
                current = current.replace(t, r)
                snapshots.append(
                    Snapshot(
                        kind=name,
                        step_index=sidx,
                        user_step=last_user_step,
                        turn_number=turn_number,
                        timestamp=ts,
                        content=current,
                        label=f"{name} at step {sidx} (turn {turn_number})",
                    )
                )
            else:
              pending_pre_read_edits.append(
                  (sidx, last_user_step, turn_number, ts, t, r)
              )
        elif name in ("read_file", "view_file") and gen_content:
          extracted = _extract_read_file_output(gen_content)
          if extracted is not None:
            # If there were edit_file calls before the first full read_file,
            # reconstruct the earlier snapshots by reversing those edits
            if current is None and pending_pre_read_edits:
              rev_text = extracted
              reconstructed_early: list[Snapshot] = []
              for esidx, eustep, eturn, ets, etarget, erepl in reversed(
                  pending_pre_read_edits
              ):
                reconstructed_early.append(
                    Snapshot(
                        kind="edit_file",
                        step_index=esidx,
                        user_step=eustep,
                        turn_number=eturn,
                        timestamp=ets,
                        content=rev_text,
                        label=f"edit_file at step {esidx} (turn {eturn})",
                    )
                )
                if erepl and erepl in rev_text:
                  rev_text = rev_text.replace(erepl, etarget)
              first_edit = pending_pre_read_edits[0]
              snapshots.append(
                  Snapshot(
                      kind="pre_edit_base",
                      step_index=first_edit[0] - 1,
                      user_step=first_edit[1],
                      turn_number=first_edit[2],
                      timestamp=first_edit[3],
                      content=rev_text,
                      label=f"pre-edit state before step {first_edit[0]}",
                  )
              )
              snapshots.extend(reversed(reconstructed_early))
              pending_pre_read_edits.clear()

            current = extracted
            snapshots.append(
                Snapshot(
                    kind=name,
                    step_index=sidx,
                    user_step=last_user_step,
                    turn_number=turn_number,
                    timestamp=ts,
                    content=current,
                    label=f"{name} at step {sidx} (turn {turn_number})",
                )
            )

      elif gdoc_id and name == "call_mcp_tool":
        mcp_args = _parse_json_arg(args.get("Arguments", {}))
        if not isinstance(mcp_args, dict):
          continue
        tool_name = str(_unquote_arg(args.get("ToolName", "")))
        fid = mcp_args.get("fileId") or mcp_args.get("documentId") or ""

        if tool_name in ("create_file", "update_file") and isinstance(
            mcp_args.get("textContent"), str
        ):
          # Match either explicit fileId or the created file's returned id
          returned_id = ""
          if gen_content:
            m_id = re.search(r'"id"\s*:\s*"([^"]+)"', gen_content)
            if m_id:
              returned_id = m_id.group(1)
          if gdoc_id in (fid, returned_id, "any"):
            current = mcp_args["textContent"]
            snapshots.append(
                Snapshot(
                    kind=tool_name,
                    step_index=sidx,
                    user_step=last_user_step,
                    turn_number=turn_number,
                    timestamp=ts,
                    content=current,
                    label=f"{tool_name} at step {sidx} (turn {turn_number})",
                )
            )
        elif tool_name in ("read_file_content", "read_doc") and (
            fid == gdoc_id or gdoc_id == "any"
        ):
          payload = _extract_mcp_payload(gen_content)
          if payload:
            md = parse_json_doc_payload(payload)
            if md:
              current = md
              snapshots.append(
                  Snapshot(
                      kind=tool_name,
                      step_index=sidx,
                      user_step=last_user_step,
                      turn_number=turn_number,
                      timestamp=ts,
                      content=current,
                      label=f"{tool_name} at step {sidx} (turn {turn_number})",
                  )
              )

  return snapshots


def select_last_seen_snapshot(
    snapshots: list[Snapshot],
    new_content: str,
    base_step: Optional[int] = None,
    base_turn: Optional[int] = None,
) -> Optional[Snapshot]:
  """Select the default base snapshot ('last version the user saw')."""
  if not snapshots:
    return None

  if base_step is not None:
    # Latest snapshot at or before base_step
    at_or_before = [s for s in snapshots if s.step_index <= base_step]
    return at_or_before[-1] if at_or_before else snapshots[0]

  turn_snaps = [s for s in snapshots if s.kind == "turn_boundary"]
  if base_turn is not None:
    if base_turn < 0:
      # Deduplicate consecutive identical turn boundaries when indexing backwards
      distinct_turns: list[Snapshot] = []
      for ts in turn_snaps:
        if not distinct_turns or distinct_turns[-1].content != ts.content:
          distinct_turns.append(ts)
      # Exclude trailing snapshot if it is already identical to new_content
      if distinct_turns and distinct_turns[-1].content == new_content:
        distinct_turns.pop()
      idx = len(distinct_turns) + base_turn
      if 0 <= idx < len(distinct_turns):
        return distinct_turns[idx]
      return distinct_turns[0] if distinct_turns else snapshots[0]
    else:
      matching = [s for s in turn_snaps if s.turn_number <= base_turn]
      return matching[-1] if matching else snapshots[0]

  # Default: most recent turn_boundary snapshot whose content differs from new_content.
  # Why turn_boundary? Because a turn_boundary is recorded at a USER_INPUT step,
  # which means the file was in that exact state when the user sent their message
  # (i.e. the user had just seen that version at the end of the previous turn)
  for ts in reversed(turn_snaps):
    if ts.content.strip() != new_content.strip():
      return ts

  # Fallback: most recent non-turn snapshot that differs from new_content
  for s in reversed(snapshots):
    if s.content.strip() != new_content.strip():
      return s

  return snapshots[-1]


def load_git_version(file_path: str, rev: Optional[str] = None) -> tuple[str, str]:
  """Load a version of file_path from git (default: HEAD if dirty, else HEAD~1)."""
  abs_path = os.path.abspath(file_path)
  cwd = os.path.dirname(abs_path) or "."
  top = subprocess.run(
      ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
      capture_output=True,
      text=True,
      check=False,
  )
  if top.returncode != 0:
    raise ValueError(f"{file_path} is not inside a git repository")
  repo_root = top.stdout.strip()
  rel_path = os.path.relpath(abs_path, repo_root)

  chosen_rev = rev
  if not chosen_rev:
    # Check if working tree differs from HEAD
    head_proc = subprocess.run(
        ["git", "-C", repo_root, "show", f"HEAD:{rel_path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    current_disk = ""
    if os.path.isfile(abs_path):
      with open(abs_path, encoding="utf-8") as f:
        current_disk = f.read()
    if head_proc.returncode == 0 and head_proc.stdout.strip() != current_disk.strip():
      chosen_rev = "HEAD"
    else:
      chosen_rev = "HEAD~1"

  proc = subprocess.run(
      ["git", "-C", repo_root, "show", f"{chosen_rev}:{rel_path}"],
      capture_output=True,
      text=True,
      check=False,
  )
  if proc.returncode != 0:
    raise ValueError(
        f"Could not read git revision {chosen_rev}:{rel_path}: {proc.stderr.strip()}"
    )
  return proc.stdout, f"git {chosen_rev}:{rel_path}"


def read_text_or_doc_json(path: str, unescape_drive: bool = False) -> str:
  with open(path, encoding="utf-8") as f:
    raw = f.read()
  parsed = parse_json_doc_payload(raw)
  if parsed is not None:
    return parsed
  return unescape_drive_markdown(raw) if unescape_drive else raw


# ---------------------------------------------------------------------------
# 3. Inline Markdown Tokenizer + Coalesced Word Diff
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InlineStyle:
  bold: bool = False
  italic: bool = False
  code: bool = False
  link: Optional[str] = None


@dataclass(frozen=True)
class InlineToken:
  text: str
  style: InlineStyle = field(default_factory=InlineStyle)

  @property
  def is_space(self) -> bool:
    return not self.text.strip()


_INLINE_SPAN_RE = re.compile(
    r"(`[^`\n]+`"
    r"|\[[^\]\n]+\]\([^)\n]+\)"
    r"|\*\*\*[^*\n]+\*\*\*"
    r"|\*\*[^*\n]+\*\*"
    r"|\*[^*\s](?:[^*\n]*[^*\s])?\*)"
)

_WORD_SPLIT_RE = re.compile(r"(\s+|[^\s]+)")


def tokenize_inline_markdown(
    text: str, base_style: InlineStyle = InlineStyle()
) -> list[InlineToken]:
  """Parse inline markdown (`code`, **bold**, *italic*, [link](url)) into tokens."""
  tokens: list[InlineToken] = []
  pos = 0
  for m in _INLINE_SPAN_RE.finditer(text):
    if m.start() > pos:
      plain = text[pos : m.start()]
      for w in _WORD_SPLIT_RE.findall(plain):
        tokens.append(InlineToken(w, base_style))
    span = m.group(0)
    if span.startswith("`") and span.endswith("`"):
      # Keep an inline code span as a single atomic token so backticks never split
      inner = span[1:-1]
      tokens.append(
          InlineToken(
              inner,
              InlineStyle(
                  bold=base_style.bold,
                  italic=base_style.italic,
                  code=True,
                  link=base_style.link,
              ),
          )
      )
    elif span.startswith("[") and "](" in span and span.endswith(")"):
      bracket_end = span.index("](")
      label = span[1:bracket_end]
      url = span[bracket_end + 2 : -1]
      sub_style = InlineStyle(
          bold=base_style.bold,
          italic=base_style.italic,
          code=base_style.code,
          link=url,
      )
      tokens.extend(tokenize_inline_markdown(label, sub_style))
    elif span.startswith("***") and span.endswith("***") and len(span) > 6:
      inner = span[3:-3]
      sub_style = InlineStyle(
          bold=True, italic=True, code=base_style.code, link=base_style.link
      )
      tokens.extend(tokenize_inline_markdown(inner, sub_style))
    elif span.startswith("**") and span.endswith("**") and len(span) > 4:
      inner = span[2:-2]
      sub_style = InlineStyle(
          bold=True,
          italic=base_style.italic,
          code=base_style.code,
          link=base_style.link,
      )
      tokens.extend(tokenize_inline_markdown(inner, sub_style))
    elif span.startswith("*") and span.endswith("*") and len(span) > 2:
      inner = span[1:-1]
      sub_style = InlineStyle(
          bold=base_style.bold,
          italic=True,
          code=base_style.code,
          link=base_style.link,
      )
      tokens.extend(tokenize_inline_markdown(inner, sub_style))
    pos = m.end()

  if pos < len(text):
    for w in _WORD_SPLIT_RE.findall(text[pos:]):
      tokens.append(InlineToken(w, base_style))

  return tokens


def render_inline_tokens(tokens: list[InlineToken]) -> str:
  """Render a slice of InlineTokens to valid HTML with minimal tag churn."""
  if not tokens:
    return ""
  out: list[str] = []
  idx = 0
  n = len(tokens)
  while idx < n:
    st = tokens[idx].style
    outer_key = (st.bold, st.italic, st.link)
    j = idx
    inner_parts: list[str] = []
    while j < n:
      st_j = tokens[j].style
      if (st_j.bold, st_j.italic, st_j.link) != outer_key:
        break
      if st_j.code:
        inner_parts.append(f"<code>{html.escape(tokens[j].text)}</code>")
      else:
        inner_parts.append(html.escape(tokens[j].text))
      j += 1
    chunk = "".join(inner_parts)
    if st.italic:
      chunk = f"<em>{chunk}</em>"
    if st.bold:
      chunk = f"<strong>{chunk}</strong>"
    if st.link:
      chunk = f'<a href="{html.escape(st.link, quote=True)}">{chunk}</a>'
    out.append(chunk)
    idx = j
  return "".join(out)


def coalesce_opcodes(
    ops: list[tuple[str, int, int, int, int]],
    tokens_a: list[InlineToken],
    tokens_b: list[InlineToken],
    max_equal_words: int = 4,
) -> list[tuple[str, int, int, int, int]]:
  """Coalesce adjacent edits separated by <= max_equal_words unchanged words."""
  merged: list[tuple[str, int, int, int, int]] = []
  for op in ops:
    tag, i1, i2, j1, j2 = op
    # Treat pure-whitespace equal segments between edits as part of the edit
    if (
        merged
        and tag != "equal"
        and len(merged) >= 2
        and merged[-1][0] == "equal"
        and merged[-2][0] != "equal"
    ):
      eq = merged[-1]
      seg_a = tokens_a[eq[1] : eq[2]]
      has_newline = any("\n" in t.text for t in seg_a)
      word_count = sum(1 for t in seg_a if not t.is_space)
      if not has_newline and word_count <= max_equal_words:
        prev = merged[-2]
        merged = merged[:-2]
        merged.append(("replace", prev[1], i2, prev[3], j2))
        continue

    if merged and tag != "equal" and merged[-1][0] != "equal":
      prev = merged.pop()
      merged.append(("replace", prev[1], i2, prev[3], j2))
      continue

    merged.append(op)
  return merged


def diff_inline_markdown(
    old_md: str,
    new_md: str,
    max_equal_words: int = 4,
) -> tuple[str, int, int, bool]:
  """Return (html_str, deleted_words, inserted_words, changed) for two inline strings."""
  tok_a = tokenize_inline_markdown(old_md)
  tok_b = tokenize_inline_markdown(new_md)

  if tok_a == tok_b:
    return render_inline_tokens(tok_b), 0, 0, False

  raw_ops = difflib.SequenceMatcher(None, tok_a, tok_b, autojunk=False).get_opcodes()
  ops = coalesce_opcodes(raw_ops, tok_a, tok_b, max_equal_words=max_equal_words)

  parts: list[str] = []
  del_words = 0
  ins_words = 0

  for tag, i1, i2, j1, j2 in ops:
    if tag == "equal":
      parts.append(render_inline_tokens(tok_a[i1:i2]))
      continue

    slice_a = tok_a[i1:i2]
    slice_b = tok_b[j1:j2]

    # Trim shared leading/trailing whitespace out of <del>/<ins> so spaces sit outside tags
    while slice_a and slice_b and slice_a[0].is_space and slice_b[0].is_space:
      parts.append(html.escape(slice_a[0].text))
      slice_a = slice_a[1:]
      slice_b = slice_b[1:]

    trailing_ws: list[str] = []
    while slice_a and slice_b and slice_a[-1].is_space and slice_b[-1].is_space:
      trailing_ws.append(html.escape(slice_a[-1].text))
      slice_a = slice_a[:-1]
      slice_b = slice_b[:-1]

    has_a = any(not t.is_space for t in slice_a)
    has_b = any(not t.is_space for t in slice_b)

    if has_a:
      del_words += sum(1 for t in slice_a if not t.is_space)
      parts.append(f"<del>{render_inline_tokens(slice_a)}</del>")
    if has_b:
      ins_words += sum(1 for t in slice_b if not t.is_space)
      if has_a:
        parts.append(" ")
      parts.append(f"<ins>{render_inline_tokens(slice_b)}</ins>")
    elif not has_a and slice_b:
      parts.append(render_inline_tokens(slice_b))

    if trailing_ws:
      parts.extend(reversed(trailing_ws))

  return "".join(parts), del_words, ins_words, (del_words > 0 or ins_words > 0)


# ---------------------------------------------------------------------------
# 4. Structural Markdown Block Parser & Block-Level Diff
# ---------------------------------------------------------------------------

@dataclass
class DocBlock:
  kind: str  # 'h', 'p', 'li', 'alert', 'blockquote', 'code', 'table', 'hr'
  text: str = ""
  level: int = 0  # heading level (1-6) or list indent level (0, 1, 2...)
  list_type: str = ""  # 'ul' or 'ol'
  list_num: Optional[int] = None
  alert_type: str = ""  # NOTE, TIP, IMPORTANT, WARNING, CAUTION
  lang: str = ""
  rows: list[list[str]] = field(default_factory=list)

  def norm_words(self) -> list[str]:
    if self.kind == "table":
      return [w.lower() for row in self.rows for cell in row for w in cell.split()]
    return [w.lower() for w in re.findall(r"\w+", self.text)]


_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.*)$")
_UL_RE = re.compile(r"^(\s*)[-*]\s+(.*)$")
_OL_RE = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
_ALERT_RE = re.compile(r"^>\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*$", re.I)
_BQ_RE = re.compile(r"^>\s?(.*)$")
_HR_RE = re.compile(r"^\s*(?:---+|\*\*\*+|___+)\s*$")
_FENCE_RE = re.compile(r"^(\s*)(```+|~~~+)(.*)$")


def parse_markdown_blocks(md_text: str) -> list[DocBlock]:
  """Parse a Markdown document into structural blocks, unwrapping soft line wraps."""
  lines = md_text.splitlines()
  blocks: list[DocBlock] = []
  i = 0
  n = len(lines)

  while i < n:
    ln = lines[i]
    if not ln.strip():
      i += 1
      continue

    # 1. Fenced code block
    m_fence = _FENCE_RE.match(ln)
    if m_fence:
      fence_marker = m_fence.group(2)
      lang = m_fence.group(3).strip()
      i += 1
      code_lines: list[str] = []
      while i < n and not lines[i].lstrip().startswith(fence_marker):
        code_lines.append(lines[i])
        i += 1
      if i < n:
        i += 1
      blocks.append(DocBlock(kind="code", text="\n".join(code_lines), lang=lang))
      continue

    # 2. Horizontal rule
    if _HR_RE.match(ln):
      blocks.append(DocBlock(kind="hr"))
      i += 1
      continue

    # 3. Heading
    m_h = _HEADING_RE.match(ln)
    if m_h:
      blocks.append(
          DocBlock(kind="h", level=len(m_h.group(1)), text=m_h.group(2).strip())
      )
      i += 1
      continue

    # 4. GitHub Alert or Blockquote
    m_alert = _ALERT_RE.match(ln.strip())
    if m_alert or _BQ_RE.match(ln.lstrip()):
      alert_kind = m_alert.group(1).upper() if m_alert else ""
      if m_alert:
        i += 1
      bq_lines: list[str] = []
      while i < n and _BQ_RE.match(lines[i].lstrip()):
        sub_m = _BQ_RE.match(lines[i].lstrip())
        sub_txt = sub_m.group(1).strip() if sub_m else ""
        if sub_txt:
          bq_lines.append(sub_txt)
        i += 1
      blocks.append(
          DocBlock(
              kind="alert" if alert_kind else "blockquote",
              alert_type=alert_kind,
              text=" ".join(bq_lines),
          )
      )
      continue

    # 5. Table
    if ln.lstrip().startswith("|") and ln.rstrip().endswith("|"):
      table_rows: list[list[str]] = []
      while i < n and lines[i].lstrip().startswith("|"):
        raw_row = lines[i].strip().strip("|")
        cells = [c.strip() for c in raw_row.split("|")]
        # Skip separator row (|---|---|)
        if not all(re.match(r"^:?-{2,}:?$", c) for c in cells if c):
          table_rows.append(cells)
        i += 1
      if table_rows:
        blocks.append(DocBlock(kind="table", rows=table_rows))
      continue

    # 6. List item (unordered or ordered)
    m_ul = _UL_RE.match(ln)
    m_ol = _OL_RE.match(ln)
    if m_ul or m_ol:
      indent_spaces = len(m_ul.group(1) if m_ul else m_ol.group(1))
      indent_level = indent_spaces // 2
      ltype = "ul" if m_ul else "ol"
      lnum = int(m_ol.group(2)) if m_ol else None
      item_parts = [(m_ul.group(2) if m_ul else m_ol.group(3)).strip()]
      i += 1
      # Collect soft-wrapped continuation lines belonging to this list item
      while i < n:
        nxt = lines[i]
        if not nxt.strip():
          break
        prev_ended_sentence = bool(
            item_parts and re.search(r"[.:;?!)\]`*]$", item_parts[-1])
        )
        is_new_ol = bool(_OL_RE.match(nxt)) and (
            prev_ended_sentence or ltype == "ol"
        )
        if (
            _HEADING_RE.match(nxt)
            or _UL_RE.match(nxt)
            or is_new_ol
            or _BQ_RE.match(nxt.lstrip())
            or _FENCE_RE.match(nxt)
            or _HR_RE.match(nxt)
            or (nxt.lstrip().startswith("|") and nxt.rstrip().endswith("|"))
        ):
          break
        # A non-indented line starting with bold Step/Шаг starts a new paragraph
        if not nxt.startswith(" ") and re.match(r"^\*\*(Step|Шаг)\b", nxt):
          break
        if nxt.startswith(" ") or indent_spaces == 0:
          item_parts.append(nxt.strip())
          i += 1
        else:
          break
      blocks.append(
          DocBlock(
              kind="li",
              level=indent_level,
              list_type=ltype,
              list_num=lnum,
              text=" ".join(item_parts),
          )
      )
      continue

    # 7. Standard Paragraph (unwrap soft-wrapped continuation lines)
    p_parts = [ln.strip()]
    i += 1
    while i < n:
      nxt = lines[i]
      if not nxt.strip():
        break
      prev_ended_sentence = bool(
          p_parts and re.search(r"[.:;?!)\]`*]$", p_parts[-1])
      )
      is_new_ol = bool(_OL_RE.match(nxt)) and prev_ended_sentence
      if (
          _HEADING_RE.match(nxt)
          or _UL_RE.match(nxt)
          or is_new_ol
          or _BQ_RE.match(nxt.lstrip())
          or _FENCE_RE.match(nxt)
          or _HR_RE.match(nxt)
          or (nxt.lstrip().startswith("|") and nxt.rstrip().endswith("|"))
      ):
        break
      if re.match(r"^\*\*(Step|Шаг)\b", nxt.strip()):
        break
      p_parts.append(nxt.strip())
      i += 1
    blocks.append(DocBlock(kind="p", text=" ".join(p_parts)))

  return blocks


def _block_similarity(a: DocBlock, b: DocBlock) -> float:
  if a.kind == "h" and b.kind == "h" and a.level == b.level:
    # Same-level headings in a replace block represent a renamed section heading
    wa, wb = a.norm_words(), b.norm_words()
    ratio = (
        difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()
        if (wa and wb)
        else 0.0
    )
    return max(0.35, ratio)
  if a.kind != b.kind:
    # Allow pairing a paragraph and a list item only when their text strongly overlaps
    if {a.kind, b.kind} != {"p", "li"}:
      return 0.0
    penalty = 0.15
  else:
    penalty = 0.0
  wa = a.norm_words()
  wb = b.norm_words()
  if not wa and not wb:
    return 1.0
  if not wa or not wb:
    return 0.0
  short_len = min(len(wa), len(wb))
  if short_len >= 2 and wa[:short_len] == wb[:short_len]:
    return max(0.35, difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio() - penalty)
  ratio = difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()
  return max(0.0, ratio - penalty)


@dataclass
class RenderedBlock:
  kind: str
  html: str
  changed: bool
  state: str  # 'equal', 'modified', 'deleted', 'inserted'
  level: int = 0
  list_type: str = ""
  list_num: Optional[int] = None
  del_words: int = 0
  ins_words: int = 0


def _render_single_block(
    blk: DocBlock, state: str, max_equal_words: int = 4
) -> RenderedBlock:
  """Render an unchanged, purely deleted, or purely inserted block."""
  changed = state != "equal"
  wrap_tag = "del" if state == "deleted" else ("ins" if state == "inserted" else "")
  cls_suffix = (
      " block-del"
      if state == "deleted"
      else (" block-ins" if state == "inserted" else " block-eq")
  )

  def wrap(inner: str) -> str:
    return f"<{wrap_tag}>{inner}</{wrap_tag}>" if wrap_tag else inner

  tokens = tokenize_inline_markdown(blk.text)
  word_cnt = sum(1 for t in tokens if not t.is_space)
  del_w = word_cnt if state == "deleted" else 0
  ins_w = word_cnt if state == "inserted" else 0
  inner_html = wrap(render_inline_tokens(tokens))

  if blk.kind == "h":
    lvl = max(1, min(6, blk.level))
    h = f'<h{lvl} class="doc-block{cls_suffix}">{inner_html}</h{lvl}>'
  elif blk.kind == "p":
    h = f'<p class="doc-block{cls_suffix}">{inner_html}</p>'
  elif blk.kind == "li":
    val_attr = f' value="{blk.list_num}"' if blk.list_type == "ol" and blk.list_num else ""
    indent_cls = f" indent-{blk.level}" if blk.level > 0 else ""
    h = f'<li class="doc-block{cls_suffix}{indent_cls}"{val_attr}>{inner_html}</li>'
  elif blk.kind == "alert":
    ak = blk.alert_type.lower() or "note"
    h = (
        f'<div class="doc-block alert alert-{ak}{cls_suffix}">'
        f'<div class="alert-title">{html.escape(blk.alert_type or "NOTE")}</div>'
        f"<div>{inner_html}</div></div>"
    )
  elif blk.kind == "blockquote":
    h = f'<blockquote class="doc-block{cls_suffix}">{inner_html}</blockquote>'
  elif blk.kind == "code":
    code_escaped = wrap(html.escape(blk.text))
    h = f'<pre class="doc-block{cls_suffix}"><code>{code_escaped}</code></pre>'
  elif blk.kind == "table":
    del_w = sum(len(c.split()) for r in blk.rows for c in r) if state == "deleted" else 0
    ins_w = sum(len(c.split()) for r in blk.rows for c in r) if state == "inserted" else 0
    rows_html: list[str] = []
    for r_idx, row in enumerate(blk.rows):
      cell_tag = "th" if r_idx == 0 else "td"
      cells_h = "".join(
          f"<{cell_tag}>{wrap(render_inline_tokens(tokenize_inline_markdown(c)))}</{cell_tag}>"
          for c in row
      )
      rows_html.append(f"<tr>{cells_h}</tr>")
    h = f'<table class="doc-block{cls_suffix}">{"".join(rows_html)}</table>'
  else:
    h = f'<hr class="doc-block{cls_suffix}">'

  return RenderedBlock(
      kind=blk.kind,
      html=h,
      changed=changed,
      state=state,
      level=blk.level,
      list_type=blk.list_type,
      list_num=blk.list_num,
      del_words=del_w,
      ins_words=ins_w,
  )


def _row_similarity(r_old: list[str], r_new: list[str]) -> float:
  if not r_old or not r_new:
    return 0.0
  # Rows sharing the same non-empty first column (e.g. issue ID or key) belong to the same entry
  k_old = r_old[0].strip()
  k_new = r_new[0].strip()
  if k_old and k_old == k_new:
    return 0.95
  w_old = [w.lower() for c in r_old for w in re.findall(r"\w+", c)]
  w_new = [w.lower() for c in r_new for w in re.findall(r"\w+", c)]
  if not w_old or not w_new:
    return 0.0
  return difflib.SequenceMatcher(None, w_old, w_new, autojunk=False).ratio()


def _render_paired_block(
    old_blk: DocBlock, new_blk: DocBlock, max_equal_words: int = 4
) -> RenderedBlock:
  """Render a modified pair of blocks with inline coalesced word diff."""
  if old_blk.kind == "table" and new_blk.kind == "table":
    del_w = 0
    ins_w = 0
    any_chg = False
    rows_html: list[str] = []

    def render_row_pair(
        r_old: list[str], r_new: list[str], cell_tag: str
    ) -> None:
      nonlocal del_w, ins_w, any_chg
      max_cols = max(len(r_old), len(r_new))
      cells_h: list[str] = []
      for c_idx in range(max_cols):
        c_o = r_old[c_idx] if c_idx < len(r_old) else ""
        c_n = r_new[c_idx] if c_idx < len(r_new) else ""
        ch, dw, iw, chg = diff_inline_markdown(c_o, c_n, max_equal_words)
        del_w += dw
        ins_w += iw
        any_chg = any_chg or chg
        cells_h.append(f"<{cell_tag}>{ch}</{cell_tag}>")
      rows_html.append(f"<tr>{''.join(cells_h)}</tr>")

    def render_del_row(r_old: list[str], cell_tag: str) -> None:
      nonlocal del_w, any_chg
      any_chg = True
      del_w += sum(len(c.split()) for c in r_old)
      cells_h = [
          f"<{cell_tag}><del>"
          f"{render_inline_tokens(tokenize_inline_markdown(c))}"
          f"</del></{cell_tag}>"
          for c in r_old
      ]
      rows_html.append(f'<tr class="row-del">{"".join(cells_h)}</tr>')

    def render_ins_row(r_new: list[str], cell_tag: str) -> None:
      nonlocal ins_w, any_chg
      any_chg = True
      ins_w += sum(len(c.split()) for c in r_new)
      cells_h = [
          f"<{cell_tag}><ins>"
          f"{render_inline_tokens(tokenize_inline_markdown(c))}"
          f"</ins></{cell_tag}>"
          for c in r_new
      ]
      rows_html.append(f'<tr class="row-ins">{"".join(cells_h)}</tr>')

    if old_blk.rows and new_blk.rows:
      render_row_pair(old_blk.rows[0], new_blk.rows[0], "th")
    elif old_blk.rows:
      render_del_row(old_blk.rows[0], "th")
    elif new_blk.rows:
      render_ins_row(new_blk.rows[0], "th")

    # Align data rows by similarity so deleted/inserted rows do not shift adjacent rows
    data_o = old_blk.rows[1:]
    data_n = new_blk.rows[1:]
    m_len, n_len = len(data_o), len(data_n)
    dp = [[0.0] * (n_len + 1) for _ in range(m_len + 1)]
    choice = [[""] * (n_len + 1) for _ in range(m_len + 1)]
    for oi in range(m_len - 1, -1, -1):
      for nj in range(n_len - 1, -1, -1):
        best = dp[oi + 1][nj]
        best_c = "skip_o"
        if dp[oi][nj + 1] > best:
          best = dp[oi][nj + 1]
          best_c = "skip_n"
        sim = _row_similarity(data_o[oi], data_n[nj])
        if sim >= 0.35:
          cand = sim + dp[oi + 1][nj + 1]
          if cand >= best:
            best = cand
            best_c = "pair"
        dp[oi][nj] = best
        choice[oi][nj] = best_c

    oi, nj = 0, 0
    while oi < m_len and nj < n_len:
      c = choice[oi][nj]
      if c == "pair":
        render_row_pair(data_o[oi], data_n[nj], "td")
        oi += 1
        nj += 1
      elif c == "skip_o":
        render_del_row(data_o[oi], "td")
        oi += 1
      else:
        render_ins_row(data_n[nj], "td")
        nj += 1
    while oi < m_len:
      render_del_row(data_o[oi], "td")
      oi += 1
    while nj < n_len:
      render_ins_row(data_n[nj], "td")
      nj += 1

    cls_suffix = " block-mod" if any_chg else " block-eq"
    return RenderedBlock(
        kind="table",
        html=f'<table class="doc-block{cls_suffix}">{"".join(rows_html)}</table>',
        changed=any_chg,
        state="modified" if any_chg else "equal",
        del_words=del_w,
        ins_words=ins_w,
    )

  if old_blk.kind == "code" and new_blk.kind == "code":
    # Line-by-line diff inside <pre><code>
    old_lines = old_blk.text.splitlines()
    new_lines = new_blk.text.splitlines()
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    code_parts: list[str] = []
    del_w = 0
    ins_w = 0
    any_chg = False
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
      if tag == "equal":
        for ln in old_lines[i1:i2]:
          code_parts.append(html.escape(ln))
      else:
        any_chg = True
        for ln in old_lines[i1:i2]:
          del_w += len(ln.split())
          code_parts.append(f"<del>{html.escape(ln)}</del>")
        for ln in new_lines[j1:j2]:
          ins_w += len(ln.split())
          code_parts.append(f"<ins>{html.escape(ln)}</ins>")
    cls_suffix = " block-mod" if any_chg else " block-eq"
    return RenderedBlock(
        kind="code",
        html=f'<pre class="doc-block{cls_suffix}"><code>' + "\n".join(code_parts) + "</code></pre>",
        changed=any_chg,
        state="modified" if any_chg else "equal",
        del_words=del_w,
        ins_words=ins_w,
    )

  diff_html, del_w, ins_w, chg = diff_inline_markdown(
      old_blk.text, new_blk.text, max_equal_words=max_equal_words
  )
  # Check if structural marker changed (e.g. bullet became paragraph or vice versa)
  struct_note = ""
  if old_blk.kind != new_blk.kind:
    chg = True
    if old_blk.kind == "li" and new_blk.kind == "p":
      struct_note = '<span class="struct-badge" title="Was list item">was •</span> '
    elif old_blk.kind == "p" and new_blk.kind == "li":
      struct_note = '<span class="struct-badge" title="Was paragraph">was ¶</span> '

  cls_suffix = " block-mod" if chg else " block-eq"
  state = "modified" if chg else "equal"
  inner = struct_note + diff_html

  if new_blk.kind == "h":
    lvl = max(1, min(6, new_blk.level))
    h = f'<h{lvl} class="doc-block{cls_suffix}">{inner}</h{lvl}>'
  elif new_blk.kind == "li":
    val_attr = (
        f' value="{new_blk.list_num}"'
        if new_blk.list_type == "ol" and new_blk.list_num
        else ""
    )
    indent_cls = f" indent-{new_blk.level}" if new_blk.level > 0 else ""
    h = f'<li class="doc-block{cls_suffix}{indent_cls}"{val_attr}>{inner}</li>'
  elif new_blk.kind == "alert":
    ak = new_blk.alert_type.lower() or "note"
    if old_blk.kind == "alert" and old_blk.alert_type != new_blk.alert_type:
      chg = True
      cls_suffix = " block-mod"
      state = "modified"
      alert_hdr = (
          f"<del>{html.escape(old_blk.alert_type or 'NOTE')}</del> "
          f"<ins>{html.escape(new_blk.alert_type or 'NOTE')}</ins>"
      )
    else:
      alert_hdr = html.escape(new_blk.alert_type or "NOTE")
    h = (
        f'<div class="doc-block alert alert-{ak}{cls_suffix}">'
        f'<div class="alert-title">{alert_hdr}</div>'
        f"<div>{inner}</div></div>"
    )
  elif new_blk.kind == "blockquote":
    h = f'<blockquote class="doc-block{cls_suffix}">{inner}</blockquote>'
  else:
    h = f'<p class="doc-block{cls_suffix}">{inner}</p>'

  return RenderedBlock(
      kind=new_blk.kind,
      html=h,
      changed=chg,
      state=state,
      level=new_blk.level,
      list_type=new_blk.list_type,
      list_num=new_blk.list_num,
      del_words=del_w,
      ins_words=ins_w,
  )


def diff_document_blocks(
    old_blocks: list[DocBlock],
    new_blocks: list[DocBlock],
    max_equal_words: int = 4,
) -> list[RenderedBlock]:
  """Align document blocks between old and new and diff paired blocks in place."""
  old_keys = [
      (b.kind, b.level, b.alert_type, b.text, tuple(tuple(r) for r in b.rows))
      for b in old_blocks
  ]
  new_keys = [
      (b.kind, b.level, b.alert_type, b.text, tuple(tuple(r) for r in b.rows))
      for b in new_blocks
  ]
  sm = difflib.SequenceMatcher(None, old_keys, new_keys, autojunk=False)

  rendered: list[RenderedBlock] = []
  for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag == "equal":
      for blk in new_blocks[j1:j2]:
        rendered.append(_render_single_block(blk, "equal", max_equal_words))
    elif tag == "delete":
      for blk in old_blocks[i1:i2]:
        rendered.append(_render_single_block(blk, "deleted", max_equal_words))
    elif tag == "insert":
      for blk in new_blocks[j1:j2]:
        rendered.append(_render_single_block(blk, "inserted", max_equal_words))
    elif tag == "replace":
      # Greedily pair monotonic sub-blocks by similarity >= 0.22
      slice_o = old_blocks[i1:i2]
      slice_n = new_blocks[j1:j2]
      pairs: list[tuple[int, int]] = []
      # Dynamic programming for monotonic maximum-similarity block pairing
      m_len, n_len = len(slice_o), len(slice_n)
      dp = [[0.0] * (n_len + 1) for _ in range(m_len + 1)]
      choice = [[""] * (n_len + 1) for _ in range(m_len + 1)]
      threshold = 0.22
      for oi in range(m_len - 1, -1, -1):
        for nj in range(n_len - 1, -1, -1):
          best = dp[oi + 1][nj]
          best_c = "skip_o"
          if dp[oi][nj + 1] > best:
            best = dp[oi][nj + 1]
            best_c = "skip_n"
          sim = _block_similarity(slice_o[oi], slice_n[nj])
          # Also pair if slice_o and slice_n are 1-to-1 of the exact same kind
          if (m_len == 1 and n_len == 1 and slice_o[0].kind == slice_n[0].kind) or sim >= threshold:
            cand = max(sim, 0.25) + dp[oi + 1][nj + 1]
            if cand >= best:
              best = cand
              best_c = "pair"
          dp[oi][nj] = best
          choice[oi][nj] = best_c

      oi, nj = 0, 0
      while oi < m_len and nj < n_len:
        c = choice[oi][nj]
        if c == "pair":
          pairs.append((oi, nj))
          oi += 1
          nj += 1
        elif c == "skip_o":
          oi += 1
        else:
          nj += 1

      prev_o, prev_n = 0, 0
      for po, pn in pairs:
        for k in range(prev_o, po):
          rendered.append(_render_single_block(slice_o[k], "deleted", max_equal_words))
        for k in range(prev_n, pn):
          rendered.append(_render_single_block(slice_n[k], "inserted", max_equal_words))
        rendered.append(
            _render_paired_block(slice_o[po], slice_n[pn], max_equal_words)
        )
        prev_o = po + 1
        prev_n = pn + 1
      for k in range(prev_o, m_len):
        rendered.append(_render_single_block(slice_o[k], "deleted", max_equal_words))
      for k in range(prev_n, n_len):
        rendered.append(_render_single_block(slice_n[k], "inserted", max_equal_words))

  return rendered


def assemble_html_blocks(blocks: list[RenderedBlock]) -> str:
  """Wrap consecutive <li> RenderedBlocks into <ul>/<ol> containers."""
  out: list[str] = []
  open_list_type = ""
  open_list_changed = False
  buf: list[str] = []

  def flush_list() -> None:
    nonlocal open_list_type, open_list_changed, buf
    if open_list_type and buf:
      cls = "doc-list has-changes" if open_list_changed else "doc-list block-eq"
      out.append(f'<{open_list_type} class="{cls}">\n' + "\n".join(buf) + f"\n</{open_list_type}>")
    open_list_type = ""
    open_list_changed = False
    buf = []

  for b in blocks:
    if b.kind == "li":
      ltype = b.list_type or "ul"
      if open_list_type and open_list_type != ltype:
        flush_list()
      open_list_type = ltype
      open_list_changed = open_list_changed or b.changed
      buf.append(b.html)
    else:
      flush_list()
      out.append(b.html)
  flush_list()
  return "\n".join(out)


# ---------------------------------------------------------------------------
# 5. Full HTML Document Generation
# ---------------------------------------------------------------------------

def render_redline_html(
    old_text: str,
    new_text: str,
    title: str = "Document Redline",
    base_label: str = "previous version",
    new_label: str = "current version",
    max_equal_words: int = 4,
) -> tuple[str, dict[str, int]]:
  """Generate the standalone HTML redline page and return (html_string, stats)."""
  old_blocks = parse_markdown_blocks(old_text)
  new_blocks = parse_markdown_blocks(new_text)
  rendered_blocks = diff_document_blocks(
      old_blocks, new_blocks, max_equal_words=max_equal_words
  )
  body_html = assemble_html_blocks(rendered_blocks)

  old_words = len(old_text.split())
  new_words = len(new_text.split())
  delta_words = new_words - old_words
  pct = (delta_words / old_words * 100.0) if old_words else 0.0
  del_words = sum(b.del_words for b in rendered_blocks)
  ins_words = sum(b.ins_words for b in rendered_blocks)
  changed_blocks = sum(1 for b in rendered_blocks if b.changed)
  total_blocks = len(rendered_blocks)

  delta_sign = "+" if delta_words > 0 else ""
  stats = {
      "old_words": old_words,
      "new_words": new_words,
      "delta_words": delta_words,
      "del_words": del_words,
      "ins_words": ins_words,
      "changed_blocks": changed_blocks,
      "total_blocks": total_blocks,
  }

  page = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{
    --ins-fg: #137333;
    --ins-bg: #e6f4ea;
    --ins-border: #a8dab5;
    --del-fg: #a50e0e;
    --del-bg: #fce8e6;
    --del-border: #f6aea9;
    --text-fg: #1f2328;
    --muted-fg: #59636e;
    --surface-bg: #f6f8fa;
    --border-col: #d1d9e0;
  }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: var(--text-fg);
    max-width: 920px;
    margin: 1.5rem auto 3rem;
    padding: 0 1.25rem;
    line-height: 1.6;
  }}
  ins {{
    color: var(--ins-fg);
    background: var(--ins-bg);
    text-decoration: none;
    padding: 0.05em 0.2em;
    border-radius: 3px;
  }}
  del {{
    color: var(--del-fg);
    background: var(--del-bg);
    text-decoration: line-through;
    padding: 0.05em 0.2em;
    border-radius: 3px;
  }}
  .legend {{
    position: sticky;
    top: 0;
    z-index: 10;
    background: rgba(255, 255, 255, 0.96);
    backdrop-filter: blur(6px);
    border: 1px solid var(--border-col);
    border-radius: 8px;
    padding: 0.65rem 1rem;
    margin-bottom: 1.5rem;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.04);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.6rem;
    font-size: 0.9rem;
  }}
  .legend-meta {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem 0.9rem;
  }}
  .stats-pill {{
    color: var(--muted-fg);
    font-size: 0.84rem;
    background: var(--surface-bg);
    padding: 0.15rem 0.55rem;
    border-radius: 999px;
    border: 1px solid var(--border-col);
  }}
  .mode-bar {{
    display: inline-flex;
    gap: 0.25rem;
    background: var(--surface-bg);
    padding: 0.2rem;
    border-radius: 6px;
    border: 1px solid var(--border-col);
  }}
  .mode-btn {{
    border: none;
    background: transparent;
    color: var(--muted-fg);
    font-size: 0.8rem;
    font-weight: 500;
    padding: 0.22rem 0.6rem;
    border-radius: 4px;
    cursor: pointer;
  }}
  .mode-btn.active {{
    background: #fff;
    color: var(--text-fg);
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
  }}
  h1, h2, h3, h4, h5, h6 {{
    margin-top: 1.5em;
    margin-bottom: 0.5em;
    line-height: 1.3;
  }}
  h1 {{ font-size: 1.65rem; border-bottom: 1px solid var(--border-col); padding-bottom: 0.3rem; }}
  h2 {{ font-size: 1.32rem; border-bottom: 1px solid #eaeef2; padding-bottom: 0.22rem; }}
  h3 {{ font-size: 1.12rem; }}
  p {{ margin: 0.65em 0; }}
  ul, ol {{
    margin: 0.55em 0;
    padding-left: 1.6rem;
  }}
  li {{ margin: 0.3em 0; }}
  li.indent-1 {{ margin-left: 1.4rem; list-style-type: circle; }}
  li.indent-2 {{ margin-left: 2.8rem; list-style-type: square; }}
  code {{
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    font-size: 0.88em;
    background: rgba(175, 184, 193, 0.2);
    padding: 0.12em 0.32em;
    border-radius: 4px;
  }}
  ins code {{ background: rgba(19, 115, 51, 0.12); }}
  del code {{ background: rgba(165, 14, 14, 0.12); }}
  pre {{
    background: var(--surface-bg);
    border: 1px solid var(--border-col);
    border-radius: 6px;
    padding: 0.8rem 1rem;
    overflow-x: auto;
    line-height: 1.45;
  }}
  pre code {{ background: none; padding: 0; }}
  blockquote, .alert {{
    margin: 0.8em 0;
    padding: 0.6rem 1rem;
    border-left: 4px solid var(--border-col);
    background: var(--surface-bg);
    border-radius: 0 6px 6px 0;
  }}
  .alert-note {{ border-left-color: #0969da; background: #ddf4ff44; }}
  .alert-important {{ border-left-color: #8250df; background: #fbefff44; }}
  .alert-warning {{ border-left-color: #9a6700; background: #fff8c544; }}
  .alert-caution {{ border-left-color: #cf222e; background: #ffebe944; }}
  .alert-tip {{ border-left-color: #1a7f37; background: #dafbe144; }}
  .alert-title {{
    font-weight: 600;
    font-size: 0.82rem;
    letter-spacing: 0.03em;
    margin-bottom: 0.2rem;
    color: var(--muted-fg);
  }}
  table {{
    border-collapse: collapse;
    width: 100%;
    margin: 0.9em 0;
    font-size: 0.92rem;
  }}
  th, td {{
    border: 1px solid var(--border-col);
    padding: 0.45rem 0.7rem;
    text-align: left;
    vertical-align: top;
  }}
  th {{ background: var(--surface-bg); font-weight: 600; }}
  .struct-badge {{
    display: inline-block;
    font-size: 0.75rem;
    color: var(--muted-fg);
    background: var(--surface-bg);
    border: 1px solid var(--border-col);
    border-radius: 4px;
    padding: 0 0.35rem;
    margin-right: 0.25rem;
  }}
  /* View modes */
  body[data-mode="new"] del,
  body[data-mode="new"] .block-del,
  body[data-mode="new"] .row-del {{ display: none !important; }}
  body[data-mode="new"] ins {{ background: transparent; color: inherit; padding: 0; }}
  body[data-mode="old"] ins,
  body[data-mode="old"] .block-ins,
  body[data-mode="old"] .row-ins {{ display: none !important; }}
  body[data-mode="old"] del {{
    background: transparent;
    color: inherit;
    text-decoration: none;
    padding: 0;
  }}
  body[data-mode="changes"] .block-eq:not(h1):not(h2):not(h3) {{
    opacity: 0.35;
    transition: opacity 0.15s;
  }}
  body[data-mode="changes"] .block-eq:hover {{ opacity: 1; }}
</style>
</head>
<body data-mode="redline">
<div class="legend">
  <div class="legend-meta">
    <span>
      <b>Redline:</b> <ins>добавлено (+{ins_words})</ins>
      · <del>удалено (−{del_words})</del>
    </span>
    <span class="stats-pill"
          title="Base: {html.escape(base_label)} → New: {html.escape(new_label)}">
      {old_words} → {new_words} слов ({delta_sign}{delta_words}, {delta_sign}{pct:.1f}%)
      · изменено блоков: {changed_blocks}/{total_blocks}
    </span>
    <span class="stats-pill">База: {html.escape(base_label)}</span>
  </div>
  <div class="mode-bar" role="group" aria-label="View mode">
    <button class="mode-btn active" onclick="setMode('redline', this)">Было → Стало</button>
    <button class="mode-btn" onclick="setMode('changes', this)">Фокус на правках</button>
    <button class="mode-btn" onclick="setMode('new', this)">Чистый итог</button>
    <button class="mode-btn" onclick="setMode('old', this)">База</button>
  </div>
</div>
<main id="redline-root">
{body_html}
</main>
<script>
function setMode(mode, btn) {{
  document.body.setAttribute('data-mode', mode);
  for (const el of document.querySelectorAll('.mode-btn')) {{
    el.classList.toggle('active', el === btn);
  }}
}}
</script>
</body>
</html>
"""
  return page, stats


# ---------------------------------------------------------------------------
# 6. CLI Entrypoint
# ---------------------------------------------------------------------------

def _resolve_source_spec(
    spec: str,
    chat_id: Optional[str] = None,
    transcript_path: Optional[str] = None,
    unescape_drive: bool = False,
) -> tuple[str, str, str]:
  """Resolve a source spec ('path.md', 'HEAD:path.md', '@step:348:path.md')."""
  if spec.startswith("@step:") or spec.startswith("@turn:"):
    parts = spec.split(":", 2)
    if len(parts) < 3:
      raise ValueError(
          f"Invalid transcript spec {spec!r}; expected @step:<N>:<path> or @turn:<N>:<path>"
      )
    kind, num_s, target = parts[0], parts[1], parts[2]
    num = int(num_s)
    t_path = find_transcript_path(target, chat_id, transcript_path)
    if not t_path:
      raise ValueError(f"Could not find transcript for {target}")
    snaps = reconstruct_file_snapshots(t_path, target_path=target)
    chosen = (
        select_last_seen_snapshot(snaps, "", base_step=num)
        if kind == "@step"
        else select_last_seen_snapshot(snaps, "", base_turn=num)
    )
    if not chosen:
      raise ValueError(f"No matching snapshot in {t_path} for {spec}")
    return chosen.content, f"{os.path.basename(target)} ({chosen.label})", target

  if ":" in spec and not os.path.exists(spec):
    rev, path = spec.split(":", 1)
    txt, lbl = load_git_version(path, rev=rev)
    return txt, lbl, path

  return (
      read_text_or_doc_json(spec, unescape_drive=unescape_drive),
      os.path.basename(spec),
      spec,
  )


def main(argv: Optional[list[str]] = None) -> int:
  parser = argparse.ArgumentParser(
      description="Generate rendered-Markdown HTML redline between two document versions."
  )
  parser.add_argument(
      "sources",
      nargs="*",
      help="One file (compares last-seen base vs file) or two sources (old new).",
  )
  parser.add_argument(
      "-o",
      "--output",
      help="Output HTML file path (default: <new_stem>_diff.html).",
  )
  parser.add_argument(
      "--chat",
      help="Conversation ID (or prefix) to read transcript snapshots from.",
  )
  parser.add_argument(
      "--transcript",
      help="Explicit path to transcript_full.jsonl or transcript.jsonl.",
  )
  parser.add_argument(
      "--base-step",
      type=int,
      help="Explicit transcript step_index to use as the base version.",
  )
  parser.add_argument(
      "--base-turn",
      type=int,
      help="Turn index for the base version (e.g. -1 for last seen turn).",
  )
  parser.add_argument(
      "--git",
      nargs="?",
      const="",
      default=None,
      help="Use git revision as base (default: HEAD if dirty, else HEAD~1).",
  )
  parser.add_argument(
      "--gdoc",
      help="Google Doc fileId to reconstruct from transcript MCP calls.",
  )
  parser.add_argument(
      "--coalesce-words",
      type=int,
      default=4,
      help="Max unchanged words between edits to coalesce into one was->now block (default: 4).",
  )
  parser.add_argument(
      "--title",
      help="Custom HTML document title.",
  )
  parser.add_argument(
      "--base-label",
      help="Custom description of the base version shown in the legend.",
  )
  parser.add_argument(
      "--unescape-drive",
      action="store_true",
      help="Unescape Google Drive markdown backslash escapes.",
  )
  parser.add_argument(
      "--list-snapshots",
      action="store_true",
      help="List available snapshots in the transcript and exit.",
  )
  args = parser.parse_args(argv)
  if not args.sources and args.git and os.path.exists(args.git):
    args.sources = [args.git]
    args.git = ""

  if args.list_snapshots:
    target = args.sources[0] if args.sources else None
    t_path = find_transcript_path(target, args.chat, args.transcript)
    if not t_path:
      print("No transcript found.", file=sys.stderr)
      return 1
    snaps = reconstruct_file_snapshots(
        t_path,
        target_path=None if args.gdoc else target,
        gdoc_id=args.gdoc,
    )
    if not snaps:
      print(f"No snapshots found in {t_path}.", file=sys.stderr)
      return 1
    for idx, s in enumerate(snaps):
      words = len(s.content.split())
      print(
          f"[{idx:2d}] {s.kind:15s} step={s.step_index:4d} turn={s.turn_number:2d} "
          f"user_step={s.user_step:4d} ts={s.timestamp} words={words} ({s.label})"
      )
    return 0

  # Resolve (old_text, base_label) and (new_text, new_label)
  if len(args.sources) == 2:
    old_text, auto_base_label, _ = _resolve_source_spec(
        args.sources[0], args.chat, args.transcript, args.unescape_drive
    )
    new_text, new_label, new_path = _resolve_source_spec(
        args.sources[1], args.chat, args.transcript, args.unescape_drive
    )
    default_out = os.path.splitext(new_path)[0] + "_diff.html"
  elif len(args.sources) == 1:
    target = args.sources[0]
    new_text = read_text_or_doc_json(target, unescape_drive=args.unescape_drive)
    new_label = os.path.basename(target)
    default_out = os.path.splitext(target)[0] + "_diff.html"

    old_text = None
    auto_base_label = ""

    if args.git is not None:
      old_text, auto_base_label = load_git_version(
          target, rev=args.git if args.git else None
      )
    else:
      t_path = find_transcript_path(target, args.chat, args.transcript)
      if t_path:
        snaps = reconstruct_file_snapshots(
            t_path,
            target_path=None if args.gdoc else target,
            gdoc_id=args.gdoc,
        )
        chosen = select_last_seen_snapshot(
            snaps,
            new_text,
            base_step=args.base_step,
            base_turn=args.base_turn,
        )
        if chosen:
          old_text = chosen.content
          auto_base_label = f"последняя просмотренная версия ({chosen.label})"

      if old_text is None:
        # Fallback to git if inside a repository
        try:
          old_text, auto_base_label = load_git_version(target)
        except ValueError as exc:
          print(
              f"Error: No earlier transcript snapshot or git version found for {target}: {exc}",
              file=sys.stderr,
          )
          return 1
  elif args.gdoc and not args.sources:
    t_path = find_transcript_path(None, args.chat, args.transcript)
    if not t_path:
      print("Error: --gdoc requires --chat or --transcript.", file=sys.stderr)
      return 1
    snaps = reconstruct_file_snapshots(t_path, gdoc_id=args.gdoc)
    if len(snaps) < 2:
      print(
          f"Error: Found fewer than 2 snapshots for Google Doc {args.gdoc}.",
          file=sys.stderr,
      )
      return 1
    new_snap = snaps[-1]
    new_text = new_snap.content
    new_label = f"gdoc:{args.gdoc} ({new_snap.label})"
    chosen = select_last_seen_snapshot(
        snaps[:-1], new_text, base_step=args.base_step, base_turn=args.base_turn
    )
    if not chosen:
      chosen = snaps[0]
    old_text = chosen.content
    auto_base_label = f"gdoc:{args.gdoc} ({chosen.label})"
    default_out = f"gdoc_{args.gdoc[:12]}_diff.html"
  else:
    parser.print_help(sys.stderr)
    return 2

  out_path = args.output or default_out
  title = args.title or f"{new_label} — изменения"
  base_label = args.base_label or auto_base_label

  html_out, stats = render_redline_html(
      old_text,
      new_text,
      title=title,
      base_label=base_label,
      new_label=new_label,
      max_equal_words=args.coalesce_words,
  )

  os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
  with open(out_path, "w", encoding="utf-8") as f:
    f.write(html_out)

  sign = "+" if stats["delta_words"] > 0 else ""
  print(
      f"Wrote {out_path}: {stats['old_words']} -> {stats['new_words']} words "
      f"({sign}{stats['delta_words']}), +{stats['ins_words']}/-{stats['del_words']} changed words "
      f"across {stats['changed_blocks']}/{stats['total_blocks']} blocks (base: {base_label})"
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())
