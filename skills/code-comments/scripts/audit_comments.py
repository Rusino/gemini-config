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
# Corporate paths, domains, and review shortlinks that must never leak into public open-source CLs
ADDED_LINE_LEAK_RE = re.compile(
    r"(/google/src/|/google_src/|/usr/local/google/home/|//depot/google3\b"
    r"|\b(?:goto|moma|critique|sponge2?|plx|yaqs|source)\.corp\.google\.com\b"
    r"|\bgoto\.google\.com\b"
    r"|(?<![a-zA-Z0-9_./-])(?:cl|cr)/\d{5,}\b"
    r"|\bLocal Hold Only\b)"
)
# Lookbehind avoids false-positiving on Go package paths like src/go/parser
COMMENT_LEAK_RE = re.compile(
    r"((?<![a-zA-Z0-9_./-])b/\d{5,}\b"
    r"|(?<![a-zA-Z0-9_./-])(?:https?://)?go/[a-z0-9][a-z0-9_-]{2,}\b)"
)
# Gerrit rejects truncated Change-Ids; enforce full 'I' + 40 lowercase hex SHA-1 format
CHANGE_ID_PREFIX_RE = re.compile(r"^Change-Id:\s*(.*)$")
VALID_CHANGE_ID_RE = re.compile(r"^I[0-9a-f]{40}$")


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


def audit_blocks(blocks, max_lines=4, require_period=False, check_cl_hygiene=False):
  issues = []
  for block in blocks:
    start_ln = block[0][0]
    end_ln = block[-1][0]
    span = f"L{start_ln}" if start_ln == end_ln else f"L{start_ln}-L{end_ln}"
    last_text = block[-1][2].strip()

    if len(block) > max_lines:
      issues.append((span, f"block is {len(block)} lines (max {max_lines})"))

    # Effective Dart demands a terminal period (an ellipsis counts); every other language forbids it
    if require_period and last_text and not last_text.endswith("."):
      issues.append((span, f"missing trailing period (Effective Dart): '{last_text}'"))
    elif not require_period and last_text.endswith(".") and not last_text.endswith("..."):
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
      if check_cl_hygiene:
        for m_leak in ADDED_LINE_LEAK_RE.finditer(stripped):
          issues.append((f"L{ln}", f"public CL hygiene leak ('{m_leak.group(0)}'): '{stripped}'"))
        for m_comm in COMMENT_LEAK_RE.finditer(stripped):
          tok = m_comm.group(0)
          issues.append((
              f"L{ln}",
              f"public CL comment leak ('{tok}' — use public issue ID/URL): '{stripped}'",
          ))
  return issues


def audit_added_code_leaks(lines, added_lines):
  # Non-comment lines (e.g. string literals or /// doc comments) can still leak internal paths/URLs
  issues = []
  for ln in sorted(added_lines):
    if ln < 1 or ln > len(lines):
      continue
    raw = lines[ln - 1]
    if COMMENT_RE.match(raw):
      continue
    for m_leak in ADDED_LINE_LEAK_RE.finditer(raw):
      issues.append((f"L{ln}", f"public CL hygiene leak in code/literal ('{m_leak.group(0)}')"))
    if re.match(r"^\s*///", raw):
      for m_comm in COMMENT_LEAK_RE.finditer(raw):
        tok = m_comm.group(0)
        issues.append((f"L{ln}", f"public CL doc-comment leak ('{tok}' — use public ID/URL)"))
  return issues


def audit_commit_message(msg_text):
  issues = []
  for idx, raw in enumerate(msg_text.splitlines(), start=1):
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
      continue
    m_cid = CHANGE_ID_PREFIX_RE.match(stripped)
    if m_cid:
      cid_val = m_cid.group(1).strip()
      if not VALID_CHANGE_ID_RE.match(cid_val):
        issues.append((
            f"L{idx}",
            f"malformed Gerrit Change-Id (must be 'I' + 40 lowercase hex chars): '{stripped}'",
        ))
      continue
    for m_leak in ADDED_LINE_LEAK_RE.finditer(stripped):
      tok = m_leak.group(0)
      issues.append((
          f"L{idx}",
          f"public CL hygiene leak in commit message ('{tok}'): '{stripped}'",
      ))
    for m_comm in COMMENT_LEAK_RE.finditer(stripped):
      token = m_comm.group(0)
      # Public Skia/Chromium trackers allow Bug: b/<id> trailers when the component is public
      is_bug_trailer = re.match(r"^(?:Bug|Fixed|Fixes|Issue):\s*", stripped, re.IGNORECASE)
      if token.startswith("b/") and is_bug_trailer:
        continue
      issues.append((
          f"L{idx}",
          f"public CL commit-message leak ('{token}' — avoid go/ or prose b/ IDs): '{stripped}'",
      ))
  return issues


def get_unpushed_head_message(repo_path):
  # Check if HEAD is ahead of upstream; if so, audit HEAD's commit message
  res = subprocess.run(
      ["git", "-C", str(repo_path), "rev-list", "--count", "@{u}..HEAD"],
      capture_output=True,
      text=True,
      check=False,
  )
  is_ahead = res.returncode == 0 and int(res.stdout.strip() or "0") > 0
  if not is_ahead:
    return None
  msg = subprocess.run(
      ["git", "-C", str(repo_path), "log", "-1", "--format=%B", "HEAD"],
      capture_output=True,
      text=True,
      check=False,
  )
  return msg.stdout if msg.returncode == 0 else None


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

  branch_re = re.compile(r"^\s*(?:if|else\s+if|while|for|switch|case)\b")
  test_file_re = re.compile(r"(?:^|/)(?:test|tests|testing)/|(?:\.|_)(?:test|unittest|tests)\.")

  def flush_hunk():
    nonlocal hunk_added, hunk_deleted
    if not cur_file or (not hunk_added and not hunk_deleted):
      return
    has_new_comment = any(COMMENT_RE.match(l) for l in hunk_added)
    added_code = [l.strip() for l in hunk_added if l.strip() and not COMMENT_RE.match(l)]
    deleted_code = [l.strip() for l in hunk_deleted if l.strip() and not COMMENT_RE.match(l)]
    if (added_code or deleted_code) and not has_new_comment:
      uncommented_hunks.append((cur_file, hunk_start, added_code, deleted_code))
    elif has_new_comment and not test_file_re.search(cur_file):
      # A method-header comment describes only the method contract and does not substitute for
      # targeted comments on internal control-flow branches inside the body
      first_brace_idx = next(
          (i for i, l in enumerate(hunk_added) if not COMMENT_RE.match(l) and "{" in l),
          None,
      )
      if first_brace_idx is not None:
        body_lines = hunk_added[first_brace_idx + 1 :]
        body_branches = [
            l.strip() for l in body_lines if not COMMENT_RE.match(l) and branch_re.match(l)
        ]
        has_body_comment = any(COMMENT_RE.match(l) for l in body_lines)
        if len(body_branches) >= 2 and not has_body_comment:
          tag = f"[HEADER_ONLY: {len(body_branches)} uncommented branches inside body]"
          uncommented_hunks.append((
              cur_file,
              hunk_start + first_brace_idx + 1,
              [tag] + body_branches,
              [],
          ))
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
  parser.add_argument(
      "--check-cl-hygiene",
      action="store_true",
      help="Enforce public Gerrit/PR hygiene (ban internal links/paths and malformed Change-Id)",
  )
  parser.add_argument(
      "--no-cl-hygiene",
      action="store_true",
      help="Disable public CL hygiene checks",
  )
  parser.add_argument(
      "--commit-msg",
      metavar="FILE",
      help="Commit message file to audit for public CL hygiene and valid 40-hex Change-Id",
  )
  args = parser.parse_args()

  total_issues = 0
  repo = pathlib.Path(args.repo).resolve()
  # Auto-enable public CL hygiene for all repositories outside internal ~/.gemini config
  check_cl_hygiene = args.check_cl_hygiene or (
      not args.no_cl_hygiene and ".gemini" not in repo.parts
  )

  if args.commit_msg:
    msg_path = pathlib.Path(args.commit_msg)
    if msg_path.is_file():
      for span, msg in audit_commit_message(msg_path.read_text(encoding="utf-8")):
        print(f"[ISSUE] {args.commit_msg}:{span}: {msg}")
        total_issues += 1

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
      issues = audit_blocks(
          blocks,
          max_lines=args.max_lines,
          require_period=full_path.suffix == ".dart",
          check_cl_hygiene=check_cl_hygiene,
      )
      if check_cl_hygiene:
        issues.extend(audit_added_code_leaks(lines, added_lines))
      for span, msg in issues:
        print(f"[ISSUE] {rel_path}:{span}: {msg}")
        total_issues += 1

    if check_cl_hygiene and not args.commit_msg:
      head_msg = get_unpushed_head_message(repo)
      if head_msg:
        for span, msg in audit_commit_message(head_msg):
          print(f"[ISSUE] HEAD commit-msg:{span}: {msg}")
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
    file_hygiene = args.check_cl_hygiene or (
        not args.no_cl_hygiene and ".gemini" not in p.resolve().parts
    )
    lines = p.read_text(encoding="utf-8").splitlines()
    blocks = extract_comment_blocks(lines)
    issues = audit_blocks(
        blocks,
        max_lines=args.max_lines,
        require_period=p.suffix == ".dart",
        check_cl_hygiene=file_hygiene,
    )
    if file_hygiene:
      issues.extend(audit_added_code_leaks(lines, range(1, len(lines) + 1)))
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
