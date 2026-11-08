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
# Scope disclaimers in commit messages must never hide diff-induced regressions or vanish in chat
SCOPE_DISCLAIMER_RE = re.compile(
    r"^(?:[-*]\s+)?(?:Not covered|Known limitations?|Non-goals?|Caveats?)\s*:",
    re.IGNORECASE,
)
COMMIT_SECTION_OR_TRAILER_RE = re.compile(
    r"^(?:Tested|Not tested|Not measured|Not profiled|Bug|Fixed|Fixes|Issue|"
    r"Assisted-by|Signed-off-by|Co-authored-by|Reviewed-by|Reviewed-on|Commit-Queue)\s*:",
    re.IGNORECASE,
)


CPP_SUFFIXES = (".h", ".hpp", ".cpp", ".cc", ".mm")
PP_COND_OPEN_RE = re.compile(r"^\s*#\s*if(?:n?def)?\b")
PP_ENDIF_RE = re.compile(r"^\s*#\s*endif\b")
SK_COND_RE = re.compile(r"^\s*#\s*if(?:def)?\b.*\bSK_\w+")
TEMPLATE_RE = re.compile(r"^\s*template\s*<(?!\s*>)")
TEMPLATE_ANY_RE = re.compile(r"^\s*template\s*<")
DECL_QUALIFIER_RE = re.compile(
    r"^(?:(?:static|virtual|inline|constexpr|explicit|friend|extern|SK_API|\[\[nodiscard\]\])\s+)*"
)
# Return type tokens, then an unqualified name, then one-line params; ctors have no return type
FUNC_DECL_RE = re.compile(
    r"^(?P<type>[A-Za-z_][\w:<>,\s*&]*?[\w>*&])\s+[*&]*(?P<name>[A-Za-z_]\w*)\s*\("
    r"[^;{}=]*\)\s*(?P<tail>(?:const|noexcept|override|final|\s)*)(?:=\s*0\s*)?(?:;|\{)\s*$"
)
DECL_NON_TYPE_WORDS = {"return", "else", "delete", "new", "throw", "case", "goto", "using",
                       "typedef", "co_return", "co_yield"}
SCOPE_OPENER_RE = re.compile(
    r"^\s*(?:template\s*<.*>\s*)?(?:class|struct|union|namespace)\b|^\s*extern\s+\"C\""
)
CPP_TEST_RE = re.compile(
    r"^\s*(?:DEF_TEST|UNIX_ONLY_TEST|DEF_GANESH_TEST\w*|DEF_GRAPHITE_TEST\w*"
    r"|TEST|TEST_F|TEST_P)\s*\("
)
DART_TEST_RE = re.compile(r"^\s*(?:test|testWidgets)\s*\(")
WHOLE_FILE_IFDEF_MSG = (
    "WHOLE_FILE_IFDEF: file is wrapped in a whole-file '{cond}'; platform/backend files are "
    "selected by the build (own gni/Bazel source list + `sources +=` under the gn flag, like "
    "skunicode/skshaper), not by a whole-file #if"
)
TEMPLATE_NO_WHY_MSG = (
    "TEMPLATE_NO_WHY: template with no comment above; a template needs a why-comment, and a "
    "callback template a measured cost or a precedent over a plain function parameter "
    "(std::function or function pointer; code_style.md)"
)
FUNC_NO_CONTRACT_MSG = (
    "FUNC_NO_CONTRACT: new function '{name}' has no contract comment above it (what it does, "
    "inputs, outputs, side effects; code_style.md)"
)
TEST_NO_HEADER_MSG = (
    "TEST_NO_HEADER: new test has no 1-2 line header comment above it (which contract it checks "
    "and why that edge matters; code_style.md)"
)


def _comment_line_indices(lines):
  """Return the 0-based indices of // lines and of every line of a /* ... */ block.

  Doxygen blocks often close `*/` on a prose line with no leading `*`, so a line-local regex
  cannot tell them from code.
  """
  out = set()
  in_block = False
  for idx, raw in enumerate(lines):
    s = raw.strip()
    if in_block:
      out.add(idx)
      in_block = "*/" not in s
    elif s.startswith("//"):
      out.add(idx)
    elif s.startswith("/*"):
      out.add(idx)
      in_block = "*/" not in s[2:]
  return out


def _has_comment_above(comment_lines, idx):
  """Return True when the 0-based line idx has a comment line directly above (no blank gap)."""
  return idx - 1 in comment_lines


def _skip_template_header(lines, idx):
  """Return the 0-based index of the `template <...>` line heading idx, or idx itself."""
  if idx > 0 and TEMPLATE_ANY_RE.match(lines[idx - 1]):
    return idx - 1
  return idx


def _whole_file_ifdef(lines):
  """Return the SK_ condition wrapping all code of the file, or None.

  Ignores license comments, the include guard, #include/#import and #pragma once around it.
  """
  sig = []
  in_block_comment = False
  for idx, raw in enumerate(lines):
    s = raw.strip()
    if in_block_comment:
      in_block_comment = "*/" not in s
      continue
    if not s or s.startswith("//"):
      continue
    if s.startswith("/*"):
      in_block_comment = "*/" not in s
      continue
    sig.append((idx, s))
  # Strip the include guard so its #endif does not count as code after the wrapper
  if (len(sig) >= 3 and re.match(r"#\s*ifndef\s+(\w+)", sig[0][1])
      and re.match(r"#\s*define\s+" + re.escape(sig[0][1].split()[-1]) + r"\b", sig[1][1])
      and PP_ENDIF_RE.match(sig[-1][1])):
    sig = sig[2:-1]
  # Only includes may come before the wrapper; any code there means the #if is local
  pos = 0
  while pos < len(sig) and re.match(r"#\s*(?:include|import|pragma\s+once)\b", sig[pos][1]):
    pos += 1
  if pos >= len(sig) or not SK_COND_RE.match(sig[pos][1]):
    return None
  # The wrapper's matching #endif must be the last code line for the #if to cover the file
  depth = 0
  for j in range(pos, len(sig)):
    if PP_COND_OPEN_RE.match(sig[j][1]):
      depth += 1
    elif PP_ENDIF_RE.match(sig[j][1]):
      depth -= 1
      if depth == 0:
        return sig[pos][1] if j == len(sig) - 1 else None
  return None


def audit_cpp_structure(rel_path, lines, added_lines, is_new_file):
  """Run the WHOLE_FILE_IFDEF, TEMPLATE_NO_WHY, FUNC_NO_CONTRACT and TEST_NO_HEADER checks.

  lines is the full new file content, added_lines the 1-based added line numbers and
  is_new_file whether the diff creates the file. Returns (span, message) issues; .dart test files
  get only TEST_NO_HEADER.
  """
  issues = []
  is_cpp = rel_path.endswith(CPP_SUFFIXES)
  is_dart_test = rel_path.endswith("_test.dart")
  if not (is_cpp or is_dart_test):
    return issues
  test_re = CPP_TEST_RE if is_cpp else DART_TEST_RE
  comment_lines = _comment_line_indices(lines)
  for ln in sorted(added_lines):
    idx = ln - 1
    if (0 <= idx < len(lines) and test_re.match(lines[idx])
        and not _has_comment_above(comment_lines, idx)):
      issues.append((f"L{ln}", TEST_NO_HEADER_MSG))
  if not is_cpp:
    return issues

  # Existing files keep their layout; the rule covers files this change creates
  if is_new_file:
    cond = _whole_file_ifdef(lines)
    if cond:
      issues.append(("L1", WHOLE_FILE_IFDEF_MSG.format(cond=cond)))

  for ln in sorted(added_lines):
    idx = ln - 1
    if 0 <= idx < len(lines) and TEMPLATE_RE.match(lines[idx]):
      if not _has_comment_above(comment_lines, idx):
        issues.append((f"L{ln}", TEMPLATE_NO_WHY_MSG))

  if not rel_path.endswith((".h", ".hpp")):
    return issues
  # Track brace scopes so statements inside inline bodies never count as declarations
  scope_stack = []
  prev_code = ""
  for idx, raw in enumerate(lines):
    code = raw.split("//", 1)[0]
    ln = idx + 1
    if idx in comment_lines:
      continue
    at_decl_scope = all(kind == "scope" for kind in scope_stack)
    # A one-line `template <...> T f();` is already reported as TEMPLATE_NO_WHY
    if at_decl_scope and ln in added_lines and not TEMPLATE_ANY_RE.match(code):
      name = _function_decl_name(code)
      if name:
        head = _skip_template_header(lines, idx)
        if not _has_comment_above(comment_lines, head):
          issues.append((f"L{ln}", FUNC_NO_CONTRACT_MSG.format(name=name)))
    for ch in code:
      if ch == "{":
        # A brace alone on its line belongs to the class/namespace header above it
        opener = code if code.strip() != "{" else prev_code
        scope_stack.append("scope" if SCOPE_OPENER_RE.match(opener) else "body")
      elif ch == "}" and scope_stack:
        scope_stack.pop()
    if code.strip():
      prev_code = code
  return issues


def _function_decl_name(code):
  """Return the function name when code is a one-line new-function declaration, else None.

  Skips ctors/dtors (no return type), operators, override, `= default` and `= delete`.
  """
  s = DECL_QUALIFIER_RE.sub("", code.strip())
  if "operator" in s or "= default" in s or "= delete" in s or s.startswith("#"):
    return None
  m = FUNC_DECL_RE.match(s)
  if not m or "override" in m.group("tail"):
    return None
  first_word = m.group("type").split()[0].split("<")[0]
  if first_word in DECL_NON_TYPE_WORDS or m.group("name") in DECL_NON_TYPE_WORDS:
    return None
  return m.group("name")


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
  assisted_by_lines = []
  for idx, raw in enumerate(msg_text.splitlines(), start=1):
    # Stop before Git's verbose commit diff scissors so diff context lines are never audited
    if re.match(r"^# -+ >8 -+\s*$", raw):
      break
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
      continue
    # Match column-0 trailers only so indented prose quotations in the body do not false-positive
    if re.match(r"^Assisted-by\s*:", raw, re.IGNORECASE):
      assisted_by_lines.append(idx)
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
  # Commits carry only the latest model trailer so amends never accumulate history
  if len(assisted_by_lines) > 1:
    issues.append((
        f"L{assisted_by_lines[1]}",
        f"multiple Assisted-by trailers ({len(assisted_by_lines)} found; "
        "keep only one — the latest model)",
    ))
  return issues


def extract_commit_disclaimers(msg_text):
  # Collect multi-line paragraph disclaimers (e.g. 'Not covered: ...') until the next blank/trailer
  disclaimers = []
  lines = msg_text.splitlines()
  idx = 0
  while idx < len(lines):
    stripped = lines[idx].strip()
    if SCOPE_DISCLAIMER_RE.match(stripped):
      start_ln = idx + 1
      para = [stripped]
      idx += 1
      while idx < len(lines):
        nxt = lines[idx].strip()
        if not nxt or nxt.startswith("#") or CHANGE_ID_PREFIX_RE.match(nxt):
          break
        if SCOPE_DISCLAIMER_RE.match(nxt) or COMMIT_SECTION_OR_TRAILER_RE.match(nxt):
          break
        para.append(nxt)
        idx += 1
      end_ln = idx
      span = f"L{start_ln}" if start_ln == end_ln else f"L{start_ln}-L{end_ln}"
      disclaimers.append((span, " ".join(para)))
    else:
      idx += 1
  return disclaimers


def get_unpushed_head_message(repo_path):
  # Check if HEAD is ahead of upstream or main; if so, audit HEAD's commit message
  is_ahead = False
  for base_ref in ("@{u}", "upstream/main", "origin/main", "origin/master"):
    res = subprocess.run(
        ["git", "-C", str(repo_path), "rev-list", "--count", f"{base_ref}..HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode == 0:
      is_ahead = int(res.stdout.strip() or "0") > 0
      break
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
  # Count local structs/classes and helper lambdas alongside branches when checking body density
  body_landmark_re = re.compile(
      r"^\s*(?:(?:if|else\s+if|while|for|switch|case)\b|"
      r"(?:struct|class)\s+\w+\s*\{|"
      r"auto\s+\w+\s*=\s*\[[^\]]*\]\s*\()"
  )
  test_file_re = re.compile(r"(?:^|/)(?:test|tests|testing)/|(?:\.|_)(?:test|unittest|tests)\.")

  # Flag multi-branch stretches inside a commented method body that went >=20 lines (or >=4
  # control-flow/helper landmarks in >=14 lines) without a targeted rationale comment
  def flush_sparse_stretch(start_rel: int, code_count: int, landmarks: list[str]):
    if (code_count >= 20 and len(landmarks) >= 2) or (code_count >= 14 and len(landmarks) >= 4):
      tag = (
          f"[SPARSE_BODY: {len(landmarks)} uncommented branches/helpers "
          f"across {code_count} code lines]"
      )
      uncommented_hunks.append((cur_file, hunk_start + start_rel, [tag] + landmarks, []))

  def flush_hunk():
    nonlocal hunk_added, hunk_deleted
    if not cur_file or (not hunk_added and not hunk_deleted):
      return
    def is_comment(line_s: str) -> bool:
      if COMMENT_RE.match(line_s):
        return True
      return bool(cur_file and cur_file.endswith(".py") and line_s.lstrip().startswith("#"))

    has_new_comment = any(is_comment(l) for l in hunk_added)
    added_code = [l.strip() for l in hunk_added if l.strip() and not is_comment(l)]
    deleted_code = [l.strip() for l in hunk_deleted if l.strip() and not is_comment(l)]
    if (added_code or deleted_code) and not has_new_comment:
      uncommented_hunks.append((cur_file, hunk_start, added_code, deleted_code))
    elif has_new_comment and not test_file_re.search(cur_file):
      # A method-header comment describes only the method contract and does not substitute for
      # targeted comments on internal control-flow branches inside the body
      first_brace_idx = next(
          (i for i, l in enumerate(hunk_added) if not is_comment(l) and "{" in l),
          None,
      )
      if first_brace_idx is not None:
        body_lines = hunk_added[first_brace_idx + 1 :]
        body_branches = [
            l.strip() for l in body_lines if not is_comment(l) and branch_re.match(l)
        ]
        has_body_comment = any(is_comment(l) for l in body_lines)
        if len(body_branches) >= 2 and not has_body_comment:
          tag = f"[HEADER_ONLY: {len(body_branches)} uncommented branches inside body]"
          uncommented_hunks.append((
              cur_file,
              hunk_start + first_brace_idx + 1,
              [tag] + body_branches,
              [],
          ))
        elif has_body_comment:
          # One comment inside a large method body must not silence long uncommented stretches of
          # loops, branches, local structs, or helper lambdas elsewhere in the same hunk
          stretch_code = 0
          stretch_landmarks = []
          stretch_start = first_brace_idx + 1
          for rel_idx, line_s in enumerate(body_lines, start=first_brace_idx + 1):
            if is_comment(line_s):
              flush_sparse_stretch(stretch_start, stretch_code, stretch_landmarks)
              stretch_code = 0
              stretch_landmarks = []
              stretch_start = rel_idx + 1
            elif line_s.strip():
              if stretch_code == 0:
                stretch_start = rel_idx
              stretch_code += 1
              if body_landmark_re.match(line_s):
                stretch_landmarks.append(line_s.strip())
          flush_sparse_stretch(stretch_start, stretch_code, stretch_landmarks)
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


def new_files_in_diff(repo_path, diff_ref):
  """Return the set of paths the diff creates (git --diff-filter=A)."""
  res = subprocess.run(
      ["git", "-C", str(repo_path), "diff", "--name-only", "--diff-filter=A", diff_ref],
      capture_output=True,
      text=True,
      check=False,
  )
  return set(res.stdout.split()) if res.returncode == 0 else set()


def read_diff_side_lines(repo_path, rel_path, rev):
  """Return the new-side lines of rel_path: the working tree file, or the blob at rev.

  Returns None for missing, deleted or non-UTF-8 files.
  """
  if rev is None:
    full_path = pathlib.Path(repo_path) / rel_path
    if not full_path.is_file():
      return None
    try:
      return full_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
      return None
  res = subprocess.run(
      ["git", "-C", str(repo_path), "show", f"{rev}:{rel_path}"],
      capture_output=True,
      check=False,
  )
  if res.returncode != 0:
    return None
  try:
    return res.stdout.decode("utf-8").splitlines()
  except UnicodeDecodeError:
    return None


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("files", nargs="*", help="Files to audit")
  parser.add_argument("--diff", metavar="REF", help="Git ref to diff against (e.g., HEAD, origin/master)")
  parser.add_argument(
      "--commit",
      metavar="REV",
      help="Audit one commit (diff REV^!, contents and message read from REV, no checkout)",
  )
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

  disclaimers = []
  if args.commit_msg:
    # Accept both regular files and process-substitution FIFOs like <(git log -1 --format=%B HEAD)
    msg_path = pathlib.Path(args.commit_msg)
    if not msg_path.exists():
      print(f"[ISSUE] {args.commit_msg}:L1: commit-msg path does not exist")
      total_issues += 1
    else:
      try:
        msg_text = msg_path.read_text(encoding="utf-8")
      except OSError as exc:
        print(f"[ISSUE] {args.commit_msg}:L1: failed to read commit-msg ({exc})")
        total_issues += 1
        msg_text = ""
      if msg_text:
        for span, msg in audit_commit_message(msg_text):
          print(f"[ISSUE] {args.commit_msg}:{span}: {msg}")
          total_issues += 1
        for span, text in extract_commit_disclaimers(msg_text):
          disclaimers.append((args.commit_msg, span, text))

  # --commit audits a past commit read-only, so blobs come from REV instead of the working tree
  content_rev = args.commit
  if args.commit:
    args.diff = f"{args.commit}^!"

  if args.diff:
    added_map, uncommented_hunks = parse_git_diff(repo, args.diff)
    new_files = new_files_in_diff(repo, args.diff)
    for rel_path, added_lines in sorted(added_map.items()):
      if not added_lines:
        continue
      lines = read_diff_side_lines(repo, rel_path, content_rev)
      if lines is None:
        continue
      full_path = repo / rel_path
      blocks = [] if full_path.suffix == ".py" else extract_comment_blocks(lines, line_filter=added_lines)
      issues = audit_blocks(
          blocks,
          max_lines=args.max_lines,
          require_period=full_path.suffix == ".dart",
          check_cl_hygiene=check_cl_hygiene,
      )
      if check_cl_hygiene:
        issues.extend(audit_added_code_leaks(lines, added_lines))
      issues.extend(audit_cpp_structure(rel_path, lines, added_lines, rel_path in new_files))
      for span, msg in issues:
        print(f"[ISSUE] {rel_path}:{span}: {msg}")
        total_issues += 1

    if check_cl_hygiene and not args.commit_msg:
      if args.commit:
        head_msg = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%B", args.commit],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
      else:
        head_msg = get_unpushed_head_message(repo)
      if head_msg:
        for span, msg in audit_commit_message(head_msg):
          print(f"[ISSUE] HEAD commit-msg:{span}: {msg}")
          total_issues += 1
        for span, text in extract_commit_disclaimers(head_msg):
          disclaimers.append(("HEAD commit-msg", span, text))

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
    blocks = [] if p.suffix == ".py" else extract_comment_blocks(lines)
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

  if disclaimers:
    print(
        "\n=== Commit-Message Scope Disclaimers "
        "(Verify Pre-Existing Only & Repeat in Final User Summary) ==="
    )
    for src, span, text in disclaimers:
      print(f"[DISCLAIMER {src}:{span}] {text}")

  if total_issues == 0:
    print("\nOK: 0 mechanical comment violations found.")
  else:
    print(f"\nFound {total_issues} mechanical comment violation(s).")
    sys.exit(1)


if __name__ == "__main__":
  main()
