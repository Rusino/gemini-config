#!/usr/bin/env python3
"""Deterministic multi-pass code horror, duplication, dead-code, and invariant auditor."""

import argparse
from collections import defaultdict
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

CPD_BINARY = "/google/bin/releases/gemini-agents-copy-paste/copy_paste_detector"
AUDIT_COMMENTS_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2]
    / "code-comments"
    / "scripts"
    / "audit_comments.py"
)
SHARED_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[3] / "scripts"
if str(SHARED_SCRIPTS_DIR) not in sys.path:
  sys.path.insert(0, str(SHARED_SCRIPTS_DIR))
import finding_ledger

EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".agents",
    ".claude",
    ".gemini",
    ".antigravity",
    ".keeper",
    ".idea",
    ".vscode",
    "venv",
    ".venv",
    "__pycache__",
    "node_modules",
    "out",
    "build",
    "bazel-bin",
    "bazel-out",
    "bazel-testlogs",
    "blaze-bin",
    "blaze-out",
    "blaze-genfiles",
    "blaze-testlogs",
    "prebuilts",
    "third_party",
    "vendor",
    ".pub-cache",
    ".dart_tool",
    "logs",
    "roadmaps",
    "ledgers",
    "projects",
}

SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hpp",
    ".hxx",
    ".m",
    ".mm",
    ".go",
    ".dart",
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".rs",
    ".java",
    ".kt",
}

GENERATED_SUFFIXES = (
    ".pb.go",
    ".pb.cc",
    ".pb.h",
    "_pb2.py",
    "_pb2_grpc.py",
    ".g.dart",
    ".freezed.dart",
    ".mocks.dart",
    ".min.js",
    ".bundle.js",
)

TEST_FILE_RE = re.compile(
    r"(?:^|/)(?:test_[^/]+|[^/]+_test|[^/]+Test|[^/]+Tests|[^/]+\.spec|[^/]+\.test)\.[a-z0-9]+$"
)

LINE_COMMENT_RE = re.compile(r"(//.*$|#.*$)")
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
HEADER_LINE_RE = re.compile(
    r"^\s*(?:#include\b|import\b|from\s+\S+\s+import\b|package\b|using\s+namespace\b|export\s+library\b|part\s+of\b|part\b)"
)
STRING_LITERAL_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
NUMBER_LITERAL_RE = re.compile(r"\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[fFlLuU]*\b")
IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")

KEYWORDS = {
    "if", "else", "elif", "for", "while", "do", "switch", "case", "default",
    "break", "continue", "return", "try", "catch", "finally", "throw", "raise",
    "func", "def", "class", "struct", "enum", "interface", "type", "var", "let",
    "const", "final", "static", "public", "private", "protected", "override",
    "virtual", "inline", "constexpr", "template", "typename", "namespace",
    "true", "false", "null", "nil", "None", "True", "False", "void", "int",
    "bool", "string", "float", "double", "char", "auto", "self", "this", "super",
    "async", "await", "yield", "go", "defer", "select", "range", "in", "is",
    "not", "and", "or", "new", "delete", "sizeof",
}

WARNING_SUPPRESS_RE = re.compile(
    r"(#pragma\s+(?:GCC|clang)\s+diagnostic\s+ignored\b"
    r"|//\s*NOLINT\b"
    r"|//\s*ignore(?:_for_file)?:"
    r"|#\s*noqa\b"
    r"|//\s*eslint-disable\b"
    r"|@SuppressWarnings\b"
    r"|#\[allow\()"
)

VALID_DEBT_RE = re.compile(
    r"(?:TODO|FIXME)\s*\(\s*(?:KEEPER-DEBT|b/\d+|#\d+|https?://|[a-zA-Z0-9_-]+:\s*(?:b/|#|\d+))",
    re.IGNORECASE,
)
ANONYMOUS_DEBT_RE = re.compile(
    r"(?://|#|/\*)\s*(?:TODO|FIXME|HACK|XXX)\b",
    re.IGNORECASE,
)

UNBOUNDED_LOOP_RE = re.compile(
    r"^\s*(?:while\s*\(\s*(?:true|1)\s*\)|for\s*\(\s*;\s*;\s*\)|while\s+True\s*:)"
)
LOOP_GUARD_HINT_RE = re.compile(
    r"\b(?:max_|limit|guard|timeout|deadline|attempts|retries|steps|iterations|fuel|budget|break\b|return\b)",
    re.IGNORECASE,
)

HOT_FUNC_NAME_RE = re.compile(
    r"(?:layout|paint|draw|shape|render|measure|glyph|cluster|run|line|itemize|raster)",
    re.IGNORECASE,
)
HOT_ALLOC_RE = re.compile(
    r"(?:\bnew\s+[A-Z]\w*|\bmalloc\s*\(|\bstd::make_(?:shared|unique)\b|\bCFDataCreate\b|\bSkStringPrintf\b)"
)

HARDCODED_PATH_RE = re.compile(r"(?:/usr/local/google/home/|/Users/[A-Za-z0-9_-]+/)")
DEBUG_LEFTOVER_RE = re.compile(
    r"(?:console\.log\s*\(\s*['\"](?:DUMP|DEBUG|TEMP|TEST)|fmt\.Print(?:ln|f)?\s*\(\s*['\"](?:DUMP|DEBUG|TEMP)|printf\s*\(\s*['\"](?:DUMP|DEBUG|HERE))"
)
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]+")

TEST_SLEEP_RE = re.compile(
    r"(?:\btime\.Sleep\s*\(|\bFuture\.delayed\s*\(|\bstd::this_thread::sleep_for\b|\bsetTimeout\s*\(|\btime\.sleep\s*\()"
)
TEST_MAGIC_PIN_RE = re.compile(
    r"(?:Probability\s*[!=]=\s*\d+|Confidence(?:Score)?\s*[!=]=\s*\d+|sizeof\s*\([^)]+\)\s*==\s*\d{2,})"
)
TEST_NARRATIVE_COMMENT_RE = re.compile(
    r"^\s*//\s*(?:Test\s+that|Verify\s+that|Check\s+that|Ensure\s+that|Assert\s+that)\b",
    re.IGNORECASE,
)
ASSERT_CALL_RE = re.compile(
    r"^\s*(?:REPORTER_ASSERT|SkASSERT|expect|assert|t\.(?:Fatal|Error)|self\.assert)"
)
DEFENSIVE_NULL_RETURN_RE = re.compile(
    r"^\s*if\s*\(\s*!\s*[a-zA-Z_][a-zA-Z0-9_]*\s*\)\s*\{\s*return\s*;\s*\}"
)
API_ENDPOINT_RE = re.compile(r"""['"](/api/[a-zA-Z0-9_/-]+)['"]""")

# Dart / Flutter web_ui & WebParagraph mechanical checks (M4 & M6)
DART_RAW_ASSERT_IN_TEST_RE = re.compile(r"^\s*assert\s*\(")
DART_NON_CI_FONT_IN_TEST_RE = re.compile(
    r"""fontFamily\s*:\s*['"](?:Helvetica(?:\s+Neue)?|Times(?:\s+New\s+Roman)?|Courier(?:\s+New)?|Menlo|Monaco|San\s+Francisco|\.SF\s+[^'"]+|Segoe\s+UI|Consolas|Verdana|Georgia|Tahoma)['"]"""
)
DART_ZERO_COLOR_EQUALITY_RE = re.compile(
    r"(?:==|!=)\s*(?:const\s+)?(?:ui\.)?Color\s*\(\s*0x00000000\s*\)"
)
DART_UN_TREESHAKEN_DEBUG_LOG_RE = re.compile(
    r"^\s*WebParagraphDebug\.(?:log|apiLog)\s*\("
)
DART_NON_CONST_DEBUG_FLAG_RE = re.compile(
    r"^\s*static\s+(?!const\b)(?:final\s+|var\s+)?bool\s+(?:logging|apiLogging)\s*="
)

FUNC_PATTERNS = {
    ".go": re.compile(r"^\s*func\s+(?:\([^)]+\)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    ".py": re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    ".dart": re.compile(
        r"^\s*(?:static\s+)?(?:[A-Za-z0-9_<>,?]+\s+)?([a-z_][A-Za-z0-9_]*)\s*\([^;]*\)\s*(?:async\s*)?\{"
    ),
    ".js": re.compile(
        r"^\s*(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("
        r"|^\s*(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_][A-Za-z0-9_]*)\s*=>"
    ),
    ".ts": re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*[<(]"
    ),
    ".cpp": re.compile(
        r"^\s*(?:[\w:<>~*&]+\s+)+([A-Za-z_][A-Za-z0-9_]*)\s*\([^;]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{"
    ),
}
FUNC_PATTERNS[".cc"] = FUNC_PATTERNS[".cpp"]
FUNC_PATTERNS[".cxx"] = FUNC_PATTERNS[".cpp"]
FUNC_PATTERNS[".c"] = FUNC_PATTERNS[".cpp"]
FUNC_PATTERNS[".h"] = FUNC_PATTERNS[".cpp"]
FUNC_PATTERNS[".hpp"] = FUNC_PATTERNS[".cpp"]
FUNC_PATTERNS[".mm"] = FUNC_PATTERNS[".cpp"]
FUNC_PATTERNS[".mjs"] = FUNC_PATTERNS[".js"]
FUNC_PATTERNS[".tsx"] = FUNC_PATTERNS[".ts"]

IGNORED_FUNC_NAMES = {
    "main", "init", "setup", "teardown", "setUp", "tearDown", "build",
    "dispose", " initState", "didUpdateWidget", "paint", "shouldRepaint",
    "createState", "toString", "hashCode", "operator", "if", "for", "while",
    "switch", "catch", " ServeHTTP", "ServeHTTP", "Error", "String",
}


def is_test_file(rel_path: str) -> bool:
  norm = rel_path.replace("\\", "/")
  if "/test/" in f"/{norm}" or "/tests/" in f"/{norm}" or "/testing/" in f"/{norm}":
    return True
  return bool(TEST_FILE_RE.search(norm))


def is_generated_file(rel_path: str) -> bool:
  return any(rel_path.endswith(suf) for suf in GENERATED_SUFFIXES)


def collect_files(targets, repo_root: pathlib.Path):
  # Canonicalize repo_root so symlinked roots (e.g. macOS /var -> /private/var)
  # match resolved file paths
  repo_root = pathlib.Path(repo_root).resolve()
  files = []
  seen = set()
  for target in targets:
    p = pathlib.Path(target).resolve()
    if p.is_file():
      rel = str(p.relative_to(repo_root)) if p.is_relative_to(repo_root) else str(p)
      if not is_generated_file(rel) and p not in seen:
        seen.add(p)
        files.append((p, rel))
      continue
    if not p.is_dir():
      continue
    for dirpath, dirnames, filenames in os.walk(p):
      dirnames[:] = sorted(
          d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")
      )
      for fname in sorted(filenames):
        ext = pathlib.Path(fname).suffix.lower()
        if ext not in SOURCE_EXTENSIONS:
          continue
        full = (pathlib.Path(dirpath) / fname).resolve()
        if full in seen:
          continue
        rel = (
            str(full.relative_to(repo_root))
            if full.is_relative_to(repo_root)
            else str(full)
        )
        if is_generated_file(rel):
          continue
        seen.add(full)
        files.append((full, rel))
  return files


def normalize_line_for_clone(raw_line: str) -> str:
  line = LINE_COMMENT_RE.sub("", raw_line).strip()
  if not line or HEADER_LINE_RE.match(line):
    return ""
  line = STRING_LITERAL_RE.sub("<STR>", line)
  line = NUMBER_LITERAL_RE.sub("<NUM>", line)

  # Preserve called function/method identifiers (followed by '(') and keywords
  # while normalizing local variable/field identifiers to <ID>
  def replace_ident(match):
    word = match.group(1)
    end_pos = match.end()
    rest = line[end_pos:].lstrip()
    if word in KEYWORDS or word in ("STR", "NUM"):
      return word
    if rest.startswith("(") or rest.startswith("::"):
      return word
    return "<ID>"

  line = IDENT_RE.sub(replace_ident, line)
  line = re.sub(r"\s+", " ", line).strip()
  # Skip trivial single-brace or return-only lines so they don't glue unrelated blocks
  if line in ("{", "}", "};", ");", "return;", "return <ID>;", "break;", "continue;"):
    return ""
  return line


def detect_exact_file_duplicates(file_records):
  by_sha = defaultdict(list)
  for full_path, rel_path, content, lines in file_records:
    if len(lines) < 15:
      continue
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    by_sha[digest].append((rel_path, len(lines)))
  duplicates = []
  for digest, locs in by_sha.items():
    if len(locs) > 1:
      duplicates.append({
          "lines": locs[0][1],
          "locations": [loc[0] for loc in locs],
          "sha256": digest[:12],
      })
  duplicates.sort(key=lambda d: d["lines"] * len(d["locations"]), reverse=True)
  return duplicates


def _strip_comments_for_ext(content: str, ext: str, keep_newlines: bool = False) -> str:
  """Strip comments using syntax appropriate for the file extension."""
  if ext == ".py":
    return re.sub(r"(?m)#.*$", "", content)
  if keep_newlines:
    content = BLOCK_COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), content)
  else:
    content = BLOCK_COMMENT_RE.sub("", content)
  return LINE_COMMENT_RE.sub("", content)


def run_builtin_block_clone_detector(file_records, min_lines=10, min_tokens=50):
  """Cross-platform normalized sliding-window clone detector for when Google3 CPD is absent."""
  window_size = max(6, min_lines // 2)
  # Map window fingerprint -> list of (rel_path, norm_idx, orig_start_line, orig_end_line)
  occ_by_hash = defaultdict(list)
  norm_streams = {}

  for _, rel_path, content, _ in file_records:
    ext = pathlib.Path(rel_path).suffix.lower()
    cleaned = _strip_comments_for_ext(content, ext, keep_newlines=True)
    norm_entries = []
    for line_no, raw in enumerate(cleaned.splitlines(), start=1):
      norm = normalize_line_for_clone(raw)
      if norm:
        tok_count = len(norm.split())
        norm_entries.append((line_no, norm, tok_count))
    norm_streams[rel_path] = norm_entries
    if len(norm_entries) < window_size:
      continue
    for i in range(len(norm_entries) - window_size + 1):
      chunk = "\n".join(norm_entries[j][1] for j in range(i, i + window_size))
      h = hashlib.blake2b(chunk.encode("utf-8"), digest_size=8).hexdigest()
      occ_by_hash[h].append((rel_path, i))

  visited_pairs = set()
  clone_groups = defaultdict(set)

  for h, occs in occ_by_hash.items():
    if len(occs) < 2 or len(occs) > 40:
      continue
    for idx_a in range(len(occs)):
      file_a, pos_a = occs[idx_a]
      stream_a = norm_streams[file_a]
      for idx_b in range(idx_a + 1, len(occs)):
        file_b, pos_b = occs[idx_b]
        if file_a == file_b and abs(pos_a - pos_b) < window_size:
          continue
        pair_key = (file_a, pos_a, file_b, pos_b)
        if pair_key in visited_pairs:
          continue
        stream_b = norm_streams[file_b]
        length = window_size
        max_len = min(len(stream_a) - pos_a, len(stream_b) - pos_b)
        while length < max_len and stream_a[pos_a + length][1] == stream_b[pos_b + length][1]:
          visited_pairs.add((file_a, pos_a + length - window_size + 1, file_b, pos_b + length - window_size + 1))
          length += 1

        span_a_lines = stream_a[pos_a + length - 1][0] - stream_a[pos_a][0] + 1
        span_b_lines = stream_b[pos_b + length - 1][0] - stream_b[pos_b][0] + 1
        tokens = sum(stream_a[pos_a + k][2] for k in range(length))
        if max(span_a_lines, span_b_lines) < min_lines or tokens < min_tokens:
          continue

        sig = hashlib.blake2b(
            "\n".join(stream_a[pos_a + k][1] for k in range(length)).encode("utf-8"),
            digest_size=8,
        ).hexdigest()
        clone_groups[(sig, tokens, max(span_a_lines, span_b_lines))].add(
            (file_a, stream_a[pos_a][0], stream_a[pos_a + length - 1][0])
        )
        clone_groups[(sig, tokens, max(span_a_lines, span_b_lines))].add(
            (file_b, stream_b[pos_b][0], stream_b[pos_b + length - 1][0])
        )

  results = []
  for (_, tokens, span_lines), locs in clone_groups.items():
    sorted_locs = sorted(locs)
    results.append({
        "strategy": "builtin-winnowing",
        "tokens": tokens,
        "lines": span_lines,
        "locations": [f"{f}:L{s}-L{e}" for f, s, e in sorted_locs],
    })
  results.sort(key=lambda r: r["tokens"] * len(r["locations"]), reverse=True)
  return results[:25]


def run_google_cpd(targets, repo_root: pathlib.Path, min_lines=10, min_tokens=60, against=None):
  """Run Google3 copy_paste_detector binary in winnowing, sourcerer, and test modes."""
  if not os.path.isfile(CPD_BINARY) or not os.access(CPD_BINARY, os.X_OK):
    return None

  # Canonicalize repo_root before relativizing resolved target paths
  repo_root = pathlib.Path(repo_root).resolve()
  rel_targets = [
      str(pathlib.Path(t).resolve().relative_to(repo_root))
      if pathlib.Path(t).resolve().is_relative_to(repo_root)
      else str(t)
      for t in targets
  ]
  passes = [
      ("cpd-winnowing", ["--strategy=winnowing"]),
      ("cpd-sourcerer", ["--strategy=sourcerer", "--similarity=0.75"]),
      (
          "cpd-tests",
          [
              "--exclude-default=false",
              "--include=*_test.go,*_test.py,*_test.dart,*Test.cpp,*Test.cc,test_*.py,*.spec.ts,*.test.js",
          ],
      ),
  ]
  if against:
    passes.append(("cpd-against", [f"--against={against}", "--strategy=sourcerer"]))

  findings = []
  seen_loc_sets = set()
  for label, extra_flags in passes:
    cmd = [
        CPD_BINARY,
        "--format=json",
        f"--min-lines={min_lines}",
        f"--min-tokens={min_tokens}",
        "--limit-duplicates=15",
        *extra_flags,
        *rel_targets,
    ]
    try:
      res = subprocess.run(
          cmd, cwd=str(repo_root), capture_output=True, text=True, timeout=45, check=False
      )
      if res.returncode != 0 or not res.stdout.strip():
        continue
      data = json.loads(res.stdout)
      dups = data.get("duplicates") or data.get("clones") or []
      for d in dups:
        locs = []
        for loc in d.get("locations", []):
          f = loc.get("file_path") or loc.get("file") or loc.get("path", "")
          f_path = pathlib.Path(f)
          if (
              not f
              or f_path.suffix.lower() not in SOURCE_EXTENSIONS
              or any(part in EXCLUDE_DIRS for part in f_path.parts)
          ):
            continue
          s = loc.get("start_line") or loc.get("startLine", 0)
          e = loc.get("end_line") or loc.get("endLine", 0)
          locs.append(f"{f}:L{s}-L{e}")
        loc_key = tuple(sorted(locs))
        if len(locs) < 2 or loc_key in seen_loc_sets:
          continue
        seen_loc_sets.add(loc_key)
        findings.append({
            "strategy": label,
            "tokens": d.get("tokens", d.get("token_count", 0)),
            "lines": d.get("lines", d.get("line_count", 0)),
            "locations": locs,
        })
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
      continue
  return findings


def analyze_functions_and_dead_code(file_records, max_func_lines=120, max_file_lines=800):
  """Detect bloated files, giant functions, and uncalled internal functions/helpers."""
  bloated_files = []
  giant_functions = []
  declared_funcs = []
  ident_counts = defaultdict(int)
  test_ident_counts = defaultdict(int)

  for _, rel_path, content, lines in file_records:
    test_flag = is_test_file(rel_path)
    if len(lines) > max_file_lines:
      bloated_files.append({"file": rel_path, "lines": len(lines), "is_test": test_flag})

    ext = pathlib.Path(rel_path).suffix.lower()
    # Count all identifier occurrences per file to avoid quadratic regex scans
    cleaned = _strip_comments_for_ext(content, ext, keep_newlines=False)
    for match in IDENT_RE.finditer(cleaned):
      word = match.group(1)
      ident_counts[word] += 1
      if test_flag:
        test_ident_counts[word] += 1

    func_re = FUNC_PATTERNS.get(ext)
    if not func_re:
      continue

    idx = 0
    total_lines = len(lines)
    while idx < total_lines:
      line = lines[idx]
      m = func_re.match(line)
      if not m:
        idx += 1
        continue
      fname = next((g for g in m.groups() if g), None)
      if not fname or fname in IGNORED_FUNC_NAMES:
        idx += 1
        continue

      start_line = idx + 1
      end_line = start_line
      if ext == ".py":
        base_indent = len(line) - len(line.lstrip())
        scan = idx + 1
        while scan < total_lines:
          sline = lines[scan]
          if sline.strip() and not sline.lstrip().startswith("#"):
            cur_indent = len(sline) - len(sline.lstrip())
            if cur_indent <= base_indent:
              break
            end_line = scan + 1
          scan += 1
      else:
        stripped_head = STRING_LITERAL_RE.sub("", LINE_COMMENT_RE.sub("", line))
        stripped_head = re.sub(r"`[^`]*`", "", stripped_head)
        brace_depth = stripped_head.count("{") - stripped_head.count("}")
        scan = idx + 1
        # Bound scan to prevent runaway on unbalanced braces inside multi-line raw strings
        while scan < total_lines and brace_depth > 0 and (scan - idx) < 3000:
          sline = STRING_LITERAL_RE.sub("", LINE_COMMENT_RE.sub("", lines[scan]))
          sline = re.sub(r"`[^`]*`", "", sline)
          brace_depth += sline.count("{") - sline.count("}")
          end_line = scan + 1
          scan += 1

      func_len = end_line - start_line + 1
      if func_len > max_func_lines:
        giant_functions.append({
            "file": rel_path,
            "name": fname,
            "start": start_line,
            "end": end_line,
            "lines": func_len,
        })

      if not test_flag and not fname.startswith(("Test", "test_", "Benchmark", "DEF_")):
        declared_funcs.append({
            "file": rel_path,
            "name": fname,
            "line": start_line,
            "lines": func_len,
        })
      idx += 1

  dead_functions = []
  test_only_functions = []
  seen_decl_names = defaultdict(list)
  for fn in declared_funcs:
    seen_decl_names[fn["name"]].append(f"{fn['file']}:L{fn['line']}")
    total_refs = ident_counts[fn["name"]]
    test_refs = test_ident_counts[fn["name"]]
    # A count of 1 means the identifier only appears at its own declaration site
    if total_refs <= 1:
      dead_functions.append(fn)
    elif total_refs - test_refs <= 1 and test_refs >= 1:
      test_only_functions.append({**fn, "test_refs": test_refs})

  duplicate_func_names = [
      {"name": name, "locations": locs}
      for name, locs in seen_decl_names.items()
      if len(locs) > 1 and len(name) >= 6
  ]

  bloated_files.sort(key=lambda x: x["lines"], reverse=True)
  giant_functions.sort(key=lambda x: x["lines"], reverse=True)
  dead_functions.sort(key=lambda x: x["lines"], reverse=True)
  return {
      "bloated_files": bloated_files,
      "giant_functions": giant_functions[:30],
      "dead_functions": dead_functions[:30],
      "test_only_functions": test_only_functions[:20],
      "duplicate_func_names": duplicate_func_names[:20],
  }


def scan_horrors_and_invariants(file_records, allow_cyrillic=False):
  """Scan files for KEEPER invariant breaches, anti-slop horrors, and test smells."""
  horrors = defaultdict(list)
  endpoints = defaultdict(set)

  for _, rel_path, content, lines in file_records:
    test_flag = is_test_file(rel_path)
    ext = pathlib.Path(rel_path).suffix.lower()
    in_hot_func = False
    hot_func_brace_depth = 0

    for idx, raw in enumerate(lines, start=1):
      loc = f"{rel_path}:L{idx}"
      stripped = raw.strip()

      for ep_match in API_ENDPOINT_RE.finditer(raw):
        endpoints[ep_match.group(1)].add(rel_path)

      if WARNING_SUPPRESS_RE.search(raw):
        horrors["warning_suppressions"].append((loc, stripped[:100]))

      if ANONYMOUS_DEBT_RE.search(raw) and not VALID_DEBT_RE.search(raw):
        horrors["anonymous_debt"].append((loc, stripped[:100]))

      if UNBOUNDED_LOOP_RE.match(raw):
        lookahead = "\n".join(lines[idx - 1 : min(len(lines), idx + 10)])
        if not LOOP_GUARD_HINT_RE.search(lookahead):
          horrors["unbounded_loops"].append((loc, stripped[:100]))

      if HARDCODED_PATH_RE.search(raw):
        horrors["hardcoded_paths"].append((loc, stripped[:100]))

      if DEBUG_LEFTOVER_RE.search(raw):
        horrors["debug_leftovers"].append((loc, stripped[:100]))

      if not allow_cyrillic and CYRILLIC_RE.search(raw):
        horrors["cyrillic_strings"].append((loc, stripped[:100]))

      if not test_flag and DEFENSIVE_NULL_RETURN_RE.match(raw):
        horrors["defensive_null_returns"].append((loc, stripped[:100]))

      if test_flag:
        if TEST_SLEEP_RE.search(raw):
          horrors["test_sleeps"].append((loc, stripped[:100]))
        if TEST_MAGIC_PIN_RE.search(raw):
          horrors["test_magic_pins"].append((loc, stripped[:100]))
        if TEST_NARRATIVE_COMMENT_RE.match(raw) and idx < len(lines):
          if ASSERT_CALL_RE.match(lines[idx]):
            horrors["test_narrative_headers"].append((loc, stripped[:100]))
        # Enforce Dart test hygiene (expect over assert, CI-available fonts only)
        if ext == ".dart":
          if DART_RAW_ASSERT_IN_TEST_RE.match(raw):
            horrors["dart_test_raw_asserts"].append((loc, stripped[:100]))
          if DART_NON_CI_FONT_IN_TEST_RE.search(raw):
            horrors["dart_test_non_ci_fonts"].append((loc, stripped[:100]))

      # Flag non-idiomatic Dart transparency checks and un-tree-shaken debug logging
      if ext == ".dart" and not test_flag and not stripped.startswith("//"):
        if DART_ZERO_COLOR_EQUALITY_RE.search(raw):
          horrors["dart_zero_color_checks"].append((loc, stripped[:100]))
        if DART_NON_CONST_DEBUG_FLAG_RE.match(raw):
          horrors["dart_un_treeshaken_debug"].append((loc, stripped[:100]))
        if DART_UN_TREESHAKEN_DEBUG_LOG_RE.match(raw):
          prev_window = "\n".join(lines[max(0, idx - 4) : idx])
          if (
              "WebParagraphDebug.logging" not in prev_window
              and "WebParagraphDebug.apiLogging" not in prev_window
              and "assert(" not in prev_window
          ):
            horrors["dart_un_treeshaken_debug"].append((loc, stripped[:100]))

      if ext in (".cc", ".cpp", ".cxx", ".h", ".hpp", ".mm") and not test_flag:
        func_m = FUNC_PATTERNS[".cpp"].match(raw)
        if func_m and HOT_FUNC_NAME_RE.search(func_m.group(1) or ""):
          in_hot_func = True
          hot_func_brace_depth = raw.count("{") - raw.count("}")
        elif in_hot_func:
          hot_func_brace_depth += raw.count("{") - raw.count("}")
          if hot_func_brace_depth <= 0:
            in_hot_func = False
        if in_hot_func and HOT_ALLOC_RE.search(raw) and not raw.lstrip().startswith("//"):
          horrors["hot_path_allocations"].append((loc, stripped[:100]))

  duplicated_endpoints = [
      {"endpoint": ep, "files": sorted(flist)}
      for ep, flist in sorted(endpoints.items())
      if len(flist) > 1
  ]
  if duplicated_endpoints:
    horrors["brain_split_endpoints"] = [
        (ep["endpoint"], ", ".join(ep["files"])) for ep in duplicated_endpoints
    ]

  return horrors


def run_comment_audit(targets, repo_root: pathlib.Path, diff_ref=None):
  if not AUDIT_COMMENTS_SCRIPT.is_file():
    return []
  cmd = [sys.executable, str(AUDIT_COMMENTS_SCRIPT)]
  if diff_ref:
    cmd.extend(["--diff", diff_ref, "--repo", str(repo_root)])
  else:
    cmd.extend(str(t) for t in targets)
  try:
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    return [
        line.strip()
        for line in res.stdout.splitlines()
        if line.startswith(("[ISSUE]", "[UNCOMMENTED"))
    ]
  except (subprocess.TimeoutExpired, OSError):
    return []


def format_markdown_report(report):
  out = []
  summary = report["summary"]
  out.append("# Ruthless Code & Feature Audit (Deterministic Pass)\n")
  out.append(
      f"- **Scanned Files**: `{summary['total_files']}` "
      f"(`{summary['prod_lines']}` prod lines, `{summary['test_lines']}` test lines, "
      f"`{summary['total_lines']}` total)"
  )
  out.append(f"- **Clone Backend**: `{summary['clone_backend']}`\n")

  out.append("## 1. Duplication & Clone Detection\n")
  if report["exact_file_duplicates"]:
    out.append("### 1.1. Exact / Byte-Identical File Duplicates")
    for dup in report["exact_file_duplicates"]:
      locs = ", ".join(f"`{l}`" for l in dup["locations"])
      out.append(f"- **{dup['lines']} lines** (`{dup['sha256']}`): {locs}")
    out.append("")
  else:
    out.append("- Zero exact file duplicates detected.")

  if report["code_clones"]:
    out.append("\n### 1.2. Code Block & Test Clones")
    out.append("| Strategy | Impact (Lines / Tokens) | Locations |")
    out.append("| :--- | :--- | :--- |")
    for c in report["code_clones"][:20]:
      locs = "<br>".join(f"`{l}`" for l in c["locations"][:6])
      out.append(f"| `{c['strategy']}` | `{c['lines']}L` (`{c['tokens']} tok`) | {locs} |")
    out.append("")
  else:
    out.append("- Zero block clones above threshold detected.\n")

  struct = report["structure"]
  out.append("## 2. Structural Bloat, Dead Code & Brain-Split\n")
  if struct["bloated_files"]:
    out.append("### 2.1. Bloated Files")
    for bf in struct["bloated_files"][:15]:
      kind = "test" if bf["is_test"] else "prod"
      out.append(f"- `{bf['file']}` — **{bf['lines']} lines** ({kind})")
    out.append("")

  if struct["giant_functions"]:
    out.append("### 2.2. Monolithic Functions")
    for gf in struct["giant_functions"][:15]:
      out.append(
          f"- `{gf['file']}:L{gf['start']}-L{gf['end']}` (`{gf['name']}`) — **{gf['lines']} lines**"
      )
    out.append("")

  if struct["dead_functions"]:
    out.append("### 2.3. Uncalled Functions / Speculative API (0 external references)")
    for df in struct["dead_functions"][:20]:
      out.append(f"- `{df['file']}:L{df['line']}` (`{df['name']}`, {df['lines']} lines)")
    out.append("")

  if struct["test_only_functions"]:
    out.append("### 2.4. Production Functions Called ONLY From Tests")
    for tf in struct["test_only_functions"][:15]:
      out.append(
          f"- `{tf['file']}:L{tf['line']}` (`{tf['name']}`, {tf['lines']} lines, {tf['test_refs']} test refs)"
      )
    out.append("")

  if struct["duplicate_func_names"]:
    out.append("### 2.5. Duplicate Function Names Across Files (Potential Brain-Split)")
    for dn in struct["duplicate_func_names"][:15]:
      locs = ", ".join(f"`{l}`" for l in dn["locations"])
      out.append(f"- `{dn['name']}`: {locs}")
    out.append("")

  out.append("## 3. Invariant Violations, Anti-Slop & Test Horrors\n")
  horrors = report["horrors"]
  any_horror = False
  labels = [
      ("brain_split_endpoints", "Duplicate API Endpoints Across Files (Brain-Split)"),
      ("warning_suppressions", "Compiler / Linter Warning Suppressions (Invariant 5)"),
      ("unbounded_loops", "Unbounded Loops Without Visible Guard (Invariant 4)"),
      ("hot_path_allocations", "Hot-Path Heap Allocations in Layout/Paint/Shape (Invariant 3)"),
      ("defensive_null_returns", "Defensive `if (!ptr) return;` Instead of Contract Assert (Rule 3)"),
      ("anonymous_debt", "Anonymous / Unstructured TODO/FIXME/HACK (Invariant 6)"),
      ("hardcoded_paths", "Hardcoded Home/Machine Paths"),
      ("debug_leftovers", "Temporary Debug Print Leftovers"),
      ("cyrillic_strings", "Cyrillic Characters in Source/UI"),
      ("test_sleeps", "Sleep-Based Synchronization in Tests"),
      ("test_magic_pins", "Magic Probability / Score / Sizeof Pinning in Tests"),
      ("test_narrative_headers", "Narrative Comment Headers Above Test Assertions (Rule 5)"),
      # Report Dart/WebParagraph test and production hygiene categories
      ("dart_test_raw_asserts", "Raw `assert(...)` in Dart Tests Instead of `expect(...)` (Rule M4)"),
      ("dart_test_non_ci_fonts", "Non-CI Desktop Font Family in Dart Tests (Rule M4)"),
      ("dart_zero_color_checks", "Incomplete Transparent Color Check `Color(0x00000000)` (Rule M6)"),
      ("dart_un_treeshaken_debug", "Un-Tree-Shaken `WebParagraphDebug` Call or Non-`const` Flag (Rule M6)"),
  ]
  for key, title in labels:
    items = horrors.get(key, [])
    if not items:
      continue
    any_horror = True
    out.append(f"### {title} ({len(items)})")
    for loc, snippet in items[:15]:
      out.append(f"- `{loc}`: `{snippet}`")
    if len(items) > 15:
      out.append(f"- *... and {len(items) - 15} more*")
    out.append("")

  if report["comment_issues"]:
    any_horror = True
    out.append(f"### Comment Style & Uncommented Diff Hunks ({len(report['comment_issues'])})")
    for ci in report["comment_issues"][:20]:
      out.append(f"- `{ci}`")
    if len(report["comment_issues"]) > 20:
      out.append(f"- *... and {len(report['comment_issues']) - 20} more*")
    out.append("")

  if not any_horror:
    out.append("- Zero mechanical invariant or comment violations found.\n")

  ledger_md = report.get("refuted_ledger_markdown", "")
  if ledger_md:
    out.append(ledger_md.rstrip())
    out.append("")

  return "\n".join(out)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("paths", nargs="*", default=["."], help="Files or directories to audit")
  parser.add_argument("--repo", default=".", help="Repository root path")
  parser.add_argument("--diff", metavar="REF", help="Git ref to diff against (e.g. HEAD, origin/main)")
  parser.add_argument("--against", metavar="PATHS", help="Reference paths for CPD --against scan")
  parser.add_argument("--min-lines", type=int, default=10, help="Min lines for duplicate blocks")
  parser.add_argument("--min-tokens", type=int, default=50, help="Min tokens for duplicate blocks")
  parser.add_argument("--max-func-lines", type=int, default=120, help="Max lines before flagging function")
  parser.add_argument("--max-file-lines", type=int, default=800, help="Max lines before flagging file")
  parser.add_argument("--allow-cyrillic", action="store_true", help="Do not flag Cyrillic characters")
  parser.add_argument("--force-builtin-cpd", action="store_true", help="Force built-in clone detector")
  parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
  args = parser.parse_args()

  repo_root = pathlib.Path(args.repo).resolve()
  raw_files = collect_files(args.paths, repo_root)
  file_records = []
  prod_lines = 0
  test_lines = 0

  for full_path, rel_path in raw_files:
    try:
      content = full_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
      continue
    lines = content.splitlines()
    if is_test_file(rel_path):
      test_lines += len(lines)
    else:
      prod_lines += len(lines)
    file_records.append((full_path, rel_path, content, lines))

  exact_file_dups = detect_exact_file_duplicates(file_records)

  cpd_results = None
  clone_backend = "builtin-winnowing"
  if not args.force_builtin_cpd:
    cpd_results = run_google_cpd(
        args.paths, repo_root, min_lines=args.min_lines, min_tokens=args.min_tokens, against=args.against
    )
    if cpd_results is not None:
      clone_backend = "google3-copy_paste_detector (winnowing + sourcerer + tests)"

  if cpd_results is None:
    prod_records = [r for r in file_records if not is_test_file(r[1])]
    test_records = [r for r in file_records if is_test_file(r[1])]
    cpd_results = run_builtin_block_clone_detector(
        prod_records, min_lines=args.min_lines, min_tokens=args.min_tokens
    ) + run_builtin_block_clone_detector(
        test_records, min_lines=args.min_lines, min_tokens=args.min_tokens
    )

  structure = analyze_functions_and_dead_code(
      file_records, max_func_lines=args.max_func_lines, max_file_lines=args.max_file_lines
  )
  horrors = scan_horrors_and_invariants(file_records, allow_cyrillic=args.allow_cyrillic)
  comment_Targets = [r[0] for r in file_records] if not args.diff else args.paths
  comment_issues = run_comment_audit(comment_Targets, repo_root, diff_ref=args.diff)

  ledger_files = None if args.paths == ["."] else [r[1] for r in file_records]
  ledger_path, ledger_entries = finding_ledger.query_entries(
      repo=str(repo_root),
      files=ledger_files,
      diff_ref=args.diff,
  )

  report = {
      "summary": {
          "total_files": len(file_records),
          "prod_lines": prod_lines,
          "test_lines": test_lines,
          "total_lines": prod_lines + test_lines,
          "clone_backend": clone_backend,
      },
      "exact_file_duplicates": exact_file_dups,
      "code_clones": cpd_results,
      "structure": structure,
      "horrors": dict(horrors),
      "comment_issues": comment_issues,
      "refuted_ledger": ledger_entries,
      "refuted_ledger_markdown": finding_ledger.format_query_markdown(
          ledger_entries, ledger_path=ledger_path, include_empty_header=True
      ),
  }

  if args.format == "json":
    print(json.dumps(report, indent=2, ensure_ascii=False))
  else:
    print(format_markdown_report(report))


if __name__ == "__main__":
  main()
