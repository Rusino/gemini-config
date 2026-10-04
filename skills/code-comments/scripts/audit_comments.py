#!/usr/bin/env python3
"""Audit code comments in files or git diffs for mechanical rules and uncommented hunks."""

import argparse
import pathlib
import re
import subprocess
import sys

COMMENT_RE = re.compile(r"^(\s*)//(?!/|!)\s?(.*)$")
TUTORIAL_RE = re.compile(r"^\d+[\.\)]\s+")
MARKDOWN_RE = re.compile(r"(\*\*[^*]+\*\*|\*[^*\s][^*]*\*|`[^`]+`|--\s)")
DIVIDER_RE = re.compile(r"^[-=_*#]{3,}\s*$")
DIFF_SPEAK_RE = re.compile(
    r"\b(removed|deleted|fixed bug|previously|used to|changed from)\b",
    re.IGNORECASE,
)


def extract_comment_blocks(lines, line_filter=None):
  """Group consecutive single-line // comments (excluding /// and //!) into blocks."""
  blocks = []
  current = []
  for idx, raw in enumerate(lines, start=1):
    m = COMMENT_RE.match(raw)
    if m:
      if current and current[-1][0] != idx - 1:
        if line_filter is None or any(ln in line_filter for ln, _, _ in current):
          blocks.append(current)
        current = []
      current.append((idx, m.group(1), m.group(2).rstrip()))
    else:
      if current:
        if line_filter is None or any(ln in line_filter for ln, _, _ in current):
          blocks.append(current)
        current = []
  if current:
    if line_filter is None or any(ln in line_filter for ln, _, _ in current):
      blocks.append(current)
  return blocks


def audit_blocks(blocks, max_lines=4):
  issues = []
  for block in blocks:
    start_ln = block[0][0]
    end_ln = block[-1][0]
    span = f"L{start_ln}" if start_ln == end_ln else f"L{start_ln}-L{end_ln}"
    last_text = block[-1][2].strip()

    if len(block) > max_lines:
      issues.append((span, f"block is {len(block)} lines (max {max_lines})"))

    if last_text.endswith(".") and not last_text.endswith("..."):
      issues.append((span, f"trailing period at end of comment block: '{last_text}'"))

    for ln, _, text in block:
      stripped = text.strip()
      if DIVIDER_RE.match(stripped):
        issues.append((f"L{ln}", f"ASCII divider noise: '{stripped}'"))
      if TUTORIAL_RE.match(stripped):
        issues.append((f"L{ln}", f"numbered tutorial marker: '{stripped}'"))
      if MARKDOWN_RE.search(stripped):
        issues.append((f"L{ln}", f"markdown/formatting noise: '{stripped}'"))
      if DIFF_SPEAK_RE.search(stripped):
        issues.append((f"L{ln}", f"changelog/diff phrasing (reframe as invariant): '{stripped}'"))
  return issues


def parse_git_diff(repo_path, diff_ref):
  cmd = ["git", "-C", str(repo_path), "diff", "--unified=0", diff_ref]
  res = subprocess.run(cmd, capture_output=True, text=True, check=False)
  if res.returncode != 0:
    sys.stderr.write(res.stderr)
    sys.exit(res.returncode)

  files_added_lines = {}
  uncommented_hunks = []
  cur_file = None
  cur_line = 0
  hunk_start = 0
  hunk_added = []
  hunk_deleted = []

  def flush_hunk():
    nonlocal hunk_added, hunk_deleted
    if not cur_file or (not hunk_added and not hunk_deleted):
      return
    has_new_comment = any(COMMENT_RE.match(l) for l in hunk_added)
    added_code = [l.strip() for l in hunk_added if l.strip() and not COMMENT_RE.match(l)]
    deleted_code = [l.strip() for l in hunk_deleted if l.strip() and not COMMENT_RE.match(l)]
    if (added_code or deleted_code) and not has_new_comment:
      uncommented_hunks.append((cur_file, hunk_start, added_code, deleted_code))
    hunk_added = []
    hunk_deleted = []

  for raw in res.stdout.splitlines():
    if raw.startswith("+++ b/"):
      flush_hunk()
      cur_file = raw[6:]
      files_added_lines.setdefault(cur_file, set())
    elif raw.startswith("@@ "):
      flush_hunk()
      m = re.search(r"\+(\d+)", raw)
      cur_line = int(m.group(1)) if m else 1
      hunk_start = cur_line
    elif cur_file:
      if raw.startswith("+") and not raw.startswith("+++"):
        files_added_lines[cur_file].add(cur_line)
        hunk_added.append(raw[1:])
        cur_line += 1
      elif raw.startswith("-") and not raw.startswith("---"):
        hunk_deleted.append(raw[1:])

  flush_hunk()
  return files_added_lines, uncommented_hunks


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("files", nargs="*", help="Files to audit")
  parser.add_argument("--diff", metavar="REF", help="Git ref to diff against (e.g., HEAD, origin/master)")
  parser.add_argument("--repo", default=".", help="Repository path for --diff")
  parser.add_argument("--max-lines", type=int, default=4, help="Max lines per comment block")
  args = parser.parse_args()

  total_issues = 0
  repo = pathlib.Path(args.repo).resolve()

  if args.diff:
    added_map, uncommented_hunks = parse_git_diff(repo, args.diff)
    for rel_path, added_lines in sorted(added_map.items()):
      full_path = repo / rel_path
      if not full_path.is_file() or not added_lines:
        continue
      try:
        lines = full_path.read_text(encoding="utf-8").splitlines()
      except UnicodeDecodeError:
        continue
      blocks = extract_comment_blocks(lines, line_filter=added_lines)
      issues = audit_blocks(blocks, max_lines=args.max_lines)
      for span, msg in issues:
        print(f"[ISSUE] {rel_path}:{span}: {msg}")
        total_issues += 1

    if uncommented_hunks:
      print("\n=== Uncommented Diff Hunks (Add a comment if the change is non-trivial, even 1 line) ===")
      for rel_path, near_line, added_code, deleted_code in uncommented_hunks:
        parts = []
        if deleted_code:
          del_preview = " | ".join(deleted_code[:2])
          if len(deleted_code) > 2:
            del_preview += f" (+{len(deleted_code) - 2} lines)"
          parts.append(f"- [{del_preview}]")
        if added_code:
          add_preview = " | ".join(added_code[:2])
          if len(added_code) > 2:
            add_preview += f" (+{len(added_code) - 2} lines)"
          parts.append(f"+ [{add_preview}]")
        print(f"[UNCOMMENTED {rel_path}:L{near_line}] {' -> '.join(parts)}")

  for file_arg in args.files:
    p = pathlib.Path(file_arg)
    if not p.is_file():
      continue
    lines = p.read_text(encoding="utf-8").splitlines()
    blocks = extract_comment_blocks(lines)
    issues = audit_blocks(blocks, max_lines=args.max_lines)
    for span, msg in issues:
      print(f"[ISSUE] {p}:{span}: {msg}")
      total_issues += 1

  if total_issues == 0:
    print("\nOK: 0 mechanical comment violations found.")
  else:
    print(f"\nFound {total_issues} mechanical comment violation(s).")
    sys.exit(1)


if __name__ == "__main__":
  main()
