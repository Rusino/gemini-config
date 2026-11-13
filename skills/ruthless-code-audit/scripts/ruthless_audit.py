#!/usr/bin/env python3
"""Deterministic multi-pass code horror, duplication, dead-code, and invariant auditor."""

# Enable PEP 604 union type annotations on macOS Xcode Python 3.9
from __future__ import annotations

import argparse
import ast
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
import stage_diff_context

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

# Accept KEEPER-DEBT, issue/URL links, or Google/Skia TODO(owner) markers with a named owner
VALID_DEBT_RE = re.compile(
    r"(?:TODO|FIXME)\s*\(\s*(?:KEEPER-DEBT|b/\d+|#\d+|https?://"
    r"|[a-zA-Z0-9_-]+(?:\s*\)|\s*:\s*(?:b/|#|\d+)))",
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

# Font-backed test inspection: pinning font metrics against hardcoded numbers breaks cross-platform
FONT_MGR_RE = re.compile(
    r"\b(?:TestFontMgr|DefaultFontMgr|makeFromData|makeFromStream|makeFromFile|"
    r"MakeFromData|MakeFromFile|MakeFromName|CreateTestTypeface|DefaultTypeface|"
    r"SampleUserTypeface)\b"
)
FONT_BACKEND_GUARD_RE = re.compile(
    r"\b(?:SkFontMgr_New_(?:FreeType|Fontations|CoreText|DirectWrite|GDI|Android|FontConfig)|"
    r"SkTypeface_Make_Fontations|SK_BUILD_FOR_(?:MAC|WIN|IOS|ANDROID)|ToolUtils::TestFontMgrIs)\b"
)
_FONT_METRIC_EXPR = (
    r"(?:\b\w*(?:glyph|Glyph|mx|metrics|Metrics|bounds|Bounds|advance|Advance)\w*\s*(?:\.|->)\s*"
    r"(?:width|height|left|top|right|bottom|fAscent|fDescent|fLeading|fTop|fBottom|"
    r"fXMin|fXMax|fAvgCharWidth|fMaxCharWidth|fCapHeight|fXHeight|"
    r"fUnderlineThickness|fUnderlinePosition)\b"
    r"|\.(?:getBounds|getWidthsBounds|getWidths|getPos|getMetrics|measureText)\b)"
)
_NON_ZERO_NUM = r"[-+]?(?:[1-9]\d*(?:\.\d+)?|0\.\d+)f?\b"
FONT_METRIC_PIN_RE = re.compile(
    r"\b(?:REPORTER_ASSERT|SkASSERT|ASSERT_\w+|EXPECT_\w+)\s*\([^;]*?"
    rf"(?:{_FONT_METRIC_EXPR}[^;]*?"
    rf"(?:==\s*{_NON_ZERO_NUM}|SkScalarNearlyEqual\s*\([^;]*,\s*{_NON_ZERO_NUM})"
    rf"|{_NON_ZERO_NUM}\s*==\s*{_FONT_METRIC_EXPR})"
)
CPP_TEST_DEF_RE = re.compile(
    r"\b(?:DEF_\w*TEST\w*\(\s*(\w+)|TEST(?:_F|_P)?\(\s*(\w+)\s*,\s*(\w+))[^{]*\{"
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


_PY_TOKEN_OR_COMMENT_RE = re.compile(
    r'(?m)"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\''
    r'|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|(#.*$)'
)
_SCRIPT_TOKEN_OR_COMMENT_RE = re.compile(
    r'(?m)"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|`(?:\\.|[^`\\])*`'
    r"|(/\*[\s\S]*?\*/)|(//.*$)"
)
_CFAM_TOKEN_OR_COMMENT_RE = re.compile(
    r'(?m)"(?:\\.|[^"\\\n])*"|(?<![0-9A-Za-z_])\'(?:\\.|[^\'\\\n]){1,4}\''
    r"|(/\*[\s\S]*?\*/)|(//.*$)"
)


def _strip_comments_for_ext(content: str, ext: str, keep_newlines: bool = False) -> str:
  """Strip comments outside string literals while optionally preserving line numbers; pure."""
  if ext == ".py":
    # Preserve `#` inside string literals while stripping end-of-line comments per line
    return _PY_TOKEN_OR_COMMENT_RE.sub(lambda m: "" if m.group(1) else m.group(0), content)

  def _repl_c_style(m: re.Match[str]) -> str:
    if m.group(1):
      return "\n" * m.group(1).count("\n") if keep_newlines else ""
    if m.group(2):
      return ""
    return m.group(0)

  # Keep `//` and `/*` inside URL strings/templates intact without breaking C++ digit separators
  pat = (
      _SCRIPT_TOKEN_OR_COMMENT_RE
      if ext in (".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".dart")
      else _CFAM_TOKEN_OR_COMMENT_RE
  )
  return pat.sub(_repl_c_style, content)


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


_JS_IDENT_RE = re.compile(r"(?<![A-Za-z0-9_$])([A-Za-z_$][A-Za-z0-9_$]*)(?![A-Za-z0-9_$])")


def _collect_top_if_imports(if_node: ast.If, out: list[ast.Import | ast.ImportFrom]) -> None:
  """Collect Import/ImportFrom nodes across both `body` and `orelse` of top-level `if` guards."""
  for branch in (if_node.body, if_node.orelse):
    for sub in branch:
      if isinstance(sub, (ast.Import, ast.ImportFrom)):
        out.append(sub)
      elif isinstance(sub, ast.If):
        _collect_top_if_imports(sub, out)


def _scan_unused_py_imports(
    rel_path: str, content: str, lines: list[str], cleaned_py: list[str]
) -> list[tuple[str, str]]:
  """Return (loc, snippet) pairs for unused top-level Python imports; pure, no side effects."""
  # Package __init__.py modules re-export symbols for callers without local references
  if pathlib.Path(rel_path).name == "__init__.py":
    return []
  try:
    tree = ast.parse(content)
  except SyntaxError:
    return []

  # Restrict to module-level imports (including `if/elif/else` version or TYPE_CHECKING guards)
  # so import-smoke statements inside test methods and try/except ImportError probes stay valid
  nodes: list[ast.Import | ast.ImportFrom] = []
  for stmt in tree.body:
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
      nodes.append(stmt)
    elif isinstance(stmt, ast.If):
      _collect_top_if_imports(stmt, nodes)

  import_lines: set[int] = set()
  for node in nodes:
    end_ln = getattr(node, "end_lineno", None) or node.lineno
    import_lines.update(range(node.lineno, end_ln + 1))

  used_idents: set[str] = set()
  for ln_no, cl in enumerate(cleaned_py, start=1):
    if ln_no not in import_lines:
      used_idents.update(IDENT_RE.findall(cl))

  ignore_re = re.compile(r"#.*\b(?:noqa|pylint:|pytype:|type:\s*ignore|side-effect|keep)\b", re.I)
  out: list[tuple[str, str]] = []
  for node in nodes:
    # Compiler directives in __future__ alter parser behavior rather than binding runtime symbols
    if isinstance(node, ast.ImportFrom) and node.module == "__future__":
      continue
    end_ln = getattr(node, "end_lineno", None) or node.lineno
    raw_span = "\n".join(lines[node.lineno - 1 : end_ln])
    # Honor explicit suppression comments on side-effect imports that register hooks
    if ignore_re.search(raw_span):
      continue
    for alias in node.names:
      if alias.name == "*":
        continue
      bound = alias.asname or alias.name.split(".")[0]
      if bound not in used_idents:
        out.append((f"{rel_path}:L{node.lineno}", f"unused import `{bound}`"))
  return out


def _parse_ts_import_bindings(clause: str, is_jsx: bool) -> list[str]:
  """Extract local bound identifiers from a TS/JS import clause; pure, no side effects."""
  clause = re.sub(r"^type\s+", "", clause.strip())
  bindings: list[str] = []
  # Namespace imports (`* as ns`) bind one local module object
  ns_m = re.search(r"\*\s+as\s+([A-Za-z_$][\w$]*)", clause)
  if ns_m:
    bindings.append(ns_m.group(1))
  # Named specifiers (`{ a, b as c, type T }`) bind the alias target when `as` is present
  brace_m = re.search(r"\{([^}]*)\}", clause)
  if brace_m:
    for part in brace_m.group(1).split(","):
      tok = re.sub(r"^type\s+", "", part.strip())
      if not tok:
        continue
      as_m = re.search(r"\bas\s+([A-Za-z_$][\w$]*)$", tok)
      cand = as_m.group(1) if as_m else tok
      if re.match(r"^[A-Za-z_$][\w$]*$", cand):
        bindings.append(cand)
  # Classic JSX transforms lower `<Tag />` to `React.createElement` without a source token
  rem = re.sub(r"\{[^}]*\}|\*\s+as\s+[A-Za-z_$][\w$]*", "", clause).strip(" ,")
  if re.match(r"^[A-Za-z_$][\w$]*$", rem) and not (is_jsx and rem == "React"):
    bindings.append(rem)
  return bindings


def _scan_unused_ts_or_dart_imports(
    rel_path: str, ext: str, content: str, lines: list[str], cleaned: list[str]
) -> list[tuple[str, str]]:
  """Return (loc, snippet) pairs for unused explicit TS/JS or Dart imports; pure."""
  fname = pathlib.Path(rel_path).name
  is_ts = ext in (".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx")
  # Skip TS barrel entrypoints and ambient `.d.ts` files that import symbols for re-export
  if is_ts and (fname.startswith("index.") or rel_path.endswith(".d.ts")):
    return []
  if ext == ".dart":
    # Skip Dart libraries with `part '...'` (whose parts share library imports) and pure barrels
    if re.search(r"""(?m)^\s*part\s+['"]""", content):
      return []
    if "export '" in content or 'export "' in content:
      if not re.search(r"\b(?:class|enum|mixin|extension|void\s+main)\b", content):
        return []

  spans: list[tuple[int, int, list[str]]] = []
  idx, total = 0, len(cleaned)
  next_stmt_re = re.compile(
      r"^\s*(?:import|export|const|let|var|function|class|interface|enum)\b"
  )
  while idx < total:
    line = cleaned[idx]
    # Match static ES imports; short-circuit semicolon-less bare `import './x.css'` first
    if is_ts and re.match(r"^\s*import\b(?!\s*\()", line):
      if re.match(r"""^\s*import\s+['"][^'"]+['"]\s*;?\s*$""", line):
        idx += 1
        continue
      end = idx
      while end + 1 < total and end - idx < 80 and ";" not in cleaned[end]:
        if re.search(r"""\bfrom\s+['"][^'"]+['"]""", cleaned[end]):
          break
        if next_stmt_re.match(cleaned[end + 1]):
          break
        end += 1
      raw_span = "\n".join(lines[idx : end + 1])
      stmt = " ".join(cleaned[idx : end + 1])
      m_from = re.match(r"""^\s*import\s+(.+?)\s+from\s+['"][^'"]+['"]""", stmt)
      if m_from and not re.search(r"eslint-disable|@ts-", raw_span):
        bnds = _parse_ts_import_bindings(m_from.group(1), ext in (".tsx", ".jsx"))
        spans.append((idx + 1, end + 1, bnds))
      idx = end + 1
      continue
    # Unqualified Dart imports bring an implicit library scope in; check explicit `as` and `show`
    if ext == ".dart" and re.match(r"""^\s*import\s+['"]""", line):
      end = idx
      while end + 1 < total and end - idx < 80 and ";" not in cleaned[end]:
        end += 1
      raw_span = "\n".join(lines[idx : end + 1])
      stmt = " ".join(cleaned[idx : end + 1])
      if "ignore:" not in raw_span:
        bnds = []
        as_m = re.search(r"\bas\s+([A-Za-z_]\w*)\b", stmt)
        if as_m:
          bnds.append(as_m.group(1))
        show_m = re.search(r"\bshow\s+([^;]+)", stmt)
        if show_m:
          shown = re.split(r"\bhide\b", show_m.group(1))[0]
          bnds.extend(
              t.strip() for t in shown.split(",") if re.match(r"^[A-Za-z_]\w*$", t.strip())
          )
        if bnds:
          spans.append((idx + 1, end + 1, bnds))
      idx = end + 1
      continue
    idx += 1

  if not spans:
    return []
  import_lines = {ln for s, e, _ in spans for ln in range(s, e + 1)}
  tok_re = _JS_IDENT_RE if is_ts else IDENT_RE
  used_idents: set[str] = set()
  for ln_no, cl in enumerate(cleaned, start=1):
    if ln_no not in import_lines:
      used_idents.update(tok_re.findall(cl))
  return [
      (f"{rel_path}:L{s}", f"unused import `{b}`")
      for s, _, bnds in spans
      for b in bnds
      if b not in used_idents
  ]


def _extract_loop_header(line: str, ext: str) -> tuple[str, str] | None:
  """Extract (loop_var, collection_key) from a `for` header line, or None; pure."""
  if ext == ".py":
    m = re.match(r"^\s*for\s+([A-Za-z_]\w*)\s+in\s+([A-Za-z_][\w.]*)\s*:\s*$", line)
    return (m.group(1), m.group(2)) if m else None
  # Match Dart/TS `in`/`of`, C++ range-for `:`, and canonical 0-based index loops
  for pat in (
      (
          r"^\s*for\s*\(\s*(?:final|var|const|let|[A-Za-z_][\w<>?]*)\s+"
          r"([A-Za-z_]\w*)\s+(?:in|of)\s+([A-Za-z_][\w.\->]*)\s*\)\s*\{\s*$"
      ),
      r"^\s*for\s*\([^:;]*?\b([A-Za-z_]\w*)\s*:\s*([A-Za-z_][\w.\->]*)\s*\)\s*\{\s*$",
      (
          r"^\s*for\s*\([^;]*?\b([A-Za-z_]\w*)\s*=\s*0\s*;\s*\1\s*<\s*"
          r"([A-Za-z_][\w.\->]*?)(?:\.(?:size|count|length)\(\)|\.length)?\s*;"
          r"\s*(?:\+\+\1|\1\+\+)\s*\)\s*\{\s*$"
      ),
  ):
    m = re.match(pat, line)
    if m:
      return (m.group(1), m.group(2))
  return None


def _find_loop_end(cleaned_lines: list[str], start: int, ext: str) -> int | None:
  """Return 0-based inclusive end line of loop at `start` (bounded <= 60 lines), or None."""
  total = len(cleaned_lines)
  # Cap loop window at 60 lines so multi-screen outer passes are left to semantic review
  if ext == ".py":
    base_ind = len(cleaned_lines[start]) - len(cleaned_lines[start].lstrip())
    end = start
    for k in range(start + 1, min(total, start + 61)):
      if cleaned_lines[k].strip():
        if len(cleaned_lines[k]) - len(cleaned_lines[k].lstrip()) <= base_ind:
          return end if end > start else None
        end = k
    return end if end > start and total - start <= 61 else None
  depth = cleaned_lines[start].count("{") - cleaned_lines[start].count("}")
  for k in range(start + 1, min(total, start + 61)):
    s = STRING_LITERAL_RE.sub("", cleaned_lines[k])
    depth += s.count("{") - s.count("}")
    if depth == 0:
      return k
  return None


_COLL_MUTATOR_RE = re.compile(
    r"\.\s*(?:push_back|emplace_back|pop_back|insert|erase|clear|resize|sort|reverse|"
    r"add|remove|removeAt|append|pop|update|extend|set|delete|setdefault)\s*\("
)
_EARLY_CTRL_RE = re.compile(r"\b(?:return|break|goto|throw|raise)\b")
_LHS_WRITE_RE = re.compile(
    r"\b([A-Za-z_]\w*)\s*(?:\[[^\]\n]+\]\s*)?(?:=(?!=)|[+\-*/|&]=|\+\+|--)"
    r"|(?:\+\+|--|&)\s*\b([A-Za-z_]\w*)\b"
)


def _scan_consecutive_loops(
    rel_path: str, ext: str, cleaned_lines: list[str]
) -> list[tuple[str, str]]:
  """Return (loc, snippet) pairs for fuseable back-to-back loops over one collection; pure."""
  out: list[tuple[str, str]] = []
  total = len(cleaned_lines)
  for idx in range(total):
    hdr1 = _extract_loop_header(cleaned_lines[idx], ext)
    if not hdr1:
      continue
    end1 = _find_loop_end(cleaned_lines, idx, ext)
    if end1 is None:
      continue
    nxt = end1 + 1
    while nxt < total and not cleaned_lines[nxt].strip():
      nxt += 1
    if nxt >= total:
      continue
    ind1 = len(cleaned_lines[idx]) - len(cleaned_lines[idx].lstrip())
    ind2 = len(cleaned_lines[nxt]) - len(cleaned_lines[nxt].lstrip())
    hdr2 = _extract_loop_header(cleaned_lines[nxt], ext)
    # Require identical block indentation and identical collection expression
    if not hdr2 or ind1 != ind2 or hdr1[1] != hdr2[1]:
      continue
    end2 = _find_loop_end(cleaned_lines, nxt, ext)
    if end2 is None:
      continue
    body1 = "\n".join(cleaned_lines[idx + 1 : end1 + (1 if ext == ".py" else 0)])
    body2 = "\n".join(cleaned_lines[nxt + 1 : end2 + (1 if ext == ".py" else 0)])
    # Skip when Pass 1 exits early or mutates container cardinality/order before Pass 2
    if _EARLY_CTRL_RE.search(body1) or _COLL_MUTATOR_RE.search(body1):
      continue
    coll_root = re.split(r"[.\->]", hdr1[1])[0]
    skip_syms = {hdr1[0], coll_root, "self", "this"} | KEYWORDS
    written = {
        (m.group(1) or m.group(2))
        for m in _LHS_WRITE_RE.finditer(body1)
        if (m.group(1) or m.group(2)) not in skip_syms
    }
    # Preserve two-phase reduction/histogram passes where Pass 1 writes state read by Pass 2
    if written & set(IDENT_RE.findall(body2)):
      continue
    out.append((
        f"{rel_path}:L{nxt + 1}",
        f"consecutive loop over `{hdr1[1]}` (prev at L{idx + 1})",
    ))
  return out


def _canonicalize_guard_cond(cond: str) -> tuple[str, bool] | None:
  """Map simple null/bool/empty guard `cond` to (ident, truthy_polarity), or None; pure."""
  c = cond.strip()
  while c.startswith("(") and c.endswith(")") and "(" not in c[1:-1]:
    c = c[1:-1].strip()
  # Normalize negated null/empty checks vs truthy checks onto `(ident, truthy_polarity)`
  false_pats = (
      r"^!\s*([A-Za-z_]\w*)$",
      r"^not\s+([A-Za-z_]\w*)$",
      r"^([A-Za-z_]\w*)\s*==\s*(?:nullptr|null|NULL|None)$",
      r"^(?:nullptr|null|NULL|None)\s*==\s*([A-Za-z_]\w*)$",
      r"^([A-Za-z_]\w*)\s+is\s+None$",
      r"^([A-Za-z_]\w*)\.(?:empty\(\)|isEmpty)$",
      r"^len\(\s*([A-Za-z_]\w*)\s*\)\s*==\s*0$",
  )
  true_pats = (
      r"^([A-Za-z_]\w*)$",
      r"^([A-Za-z_]\w*)\s*!=\s*(?:nullptr|null|NULL|None)$",
      r"^(?:nullptr|null|NULL|None)\s*!=\s*([A-Za-z_]\w*)$",
      r"^([A-Za-z_]\w*)\s+is\s+not\s+None$",
      r"^!\s*([A-Za-z_]\w*)\.empty\(\)$",
      r"^([A-Za-z_]\w*)\.isNotEmpty$",
      r"^len\(\s*([A-Za-z_]\w*)\s*\)\s*(?:>\s*0|!=\s*0)$",
  )
  for pat in false_pats:
    m = re.match(pat, c)
    if m and m.group(1) not in KEYWORDS:
      return (m.group(1), False)
  for pat in true_pats:
    m = re.match(pat, c)
    if m and m.group(1) not in KEYWORDS:
      return (m.group(1), True)
  return None


def _match_early_exit_guard(
    cleaned_lines: list[str], idx: int, ext: str
) -> tuple[str, bool, int, int] | None:
  """Match unconditional `if (cond) return/continue` at `idx`; return (ident, pol, end_idx, ind)."""
  line = cleaned_lines[idx]
  ind = len(line) - len(line.lstrip())
  total = len(cleaned_lines)
  if ext == ".py":
    m1 = re.match(r"^\s*if\s+([^:]+):\s*(?:return(?:\s+.*)?|continue)\s*$", line)
    if m1:
      end_i = idx
      cond_raw = m1.group(1)
    else:
      m2 = re.match(r"^\s*if\s+([^:]+):\s*$", line)
      if not m2 or idx + 1 >= total:
        return None
      nxt = idx + 1
      while nxt < total and not cleaned_lines[nxt].strip():
        nxt += 1
      if nxt >= total:
        return None
      body_ln = cleaned_lines[nxt]
      if len(body_ln) - len(body_ln.lstrip()) <= ind:
        return None
      if not re.match(r"^\s*(?:return(?:\s+.*)?|continue)\s*$", body_ln):
        return None
      end_i = nxt
      cond_raw = m2.group(1)
    # Exclude `if/elif/else` chains where later siblings do not execute on fallthrough
    after = end_i + 1
    while after < total and not cleaned_lines[after].strip():
      after += 1
    if after < total and re.match(r"^\s*(?:else\s*:|elif\b)", cleaned_lines[after]):
      if len(cleaned_lines[after]) - len(cleaned_lines[after].lstrip()) == ind:
        return None
    can = _canonicalize_guard_cond(cond_raw)
    return (can[0], can[1], end_i, ind) if can else None

  # Exclude guards nested directly inside an unbraced outer `if (...)` / `else` / `for (...)`
  prev = idx - 1
  while prev >= 0 and not cleaned_lines[prev].strip():
    prev -= 1
  if prev >= 0:
    p_str = cleaned_lines[prev].rstrip()
    if re.match(r"^\s*(?:if|for|while)\s*\(.*\)\s*$|^\s*else\s*$", p_str):
      return None

  # Match single-line `if (cond) return;` and 3-line braced `if (cond) { return; }`
  m1 = re.match(
      r"^\s*if\s*\(([^()]+(?:\(\))?[^()]*)\)\s*"
      r"(?:\{\s*(?:return(?:\s+[^;{}]*)?|continue)\s*;\s*\}"
      r"|(?:return(?:\s+[^;{}]*)?|continue)\s*;)\s*$",
      line,
  )
  if m1:
    after = idx + 1
    while after < total and not cleaned_lines[after].strip():
      after += 1
    if after < total and re.match(r"^\s*else\b", cleaned_lines[after]):
      return None
    can = _canonicalize_guard_cond(m1.group(1))
    return (can[0], can[1], idx, ind) if can else None
  m2 = re.match(r"^\s*if\s*\(([^()]+(?:\(\))?[^()]*)\)\s*\{\s*$", line)
  if not m2 or idx + 2 >= total:
    return None
  if not re.match(r"^\s*(?:return(?:\s+[^;{}]*)?|continue)\s*;\s*$", cleaned_lines[idx + 1]):
    return None
  if not re.match(r"^\s*\}\s*$", cleaned_lines[idx + 2]):
    return None
  after = idx + 3
  while after < total and not cleaned_lines[after].strip():
    after += 1
  if after < total and re.match(r"^\s*else\b", cleaned_lines[after]):
    return None
  can = _canonicalize_guard_cond(m2.group(1))
  return (can[0], can[1], idx + 2, ind) if can else None


def _scan_post_early_exit_guards(
    rel_path: str, ext: str, cleaned_lines: list[str]
) -> list[tuple[str, str]]:
  """Return (loc, snippet) pairs for tautological/dead checks right below early exit; pure."""
  out: list[tuple[str, str]] = []
  total = len(cleaned_lines)
  for idx in range(total):
    guard = _match_early_exit_guard(cleaned_lines, idx, ext)
    if not guard:
      continue
    ident, exit_pol, end_idx, ind = guard
    # Member fields (`fFoo`, `_foo`, `m_foo`) can be mutated indirectly by helper calls
    is_member = bool(re.match(r"^(?:f[A-Z]|_[a-z]|m_)\w*$", ident))
    esc = re.escape(ident)
    mut_re = re.compile(
        rf"\b{esc}\s*(?:\[[^\]\n]*\]\s*)?(?:=(?!=)|[+\-*/|&]=|\+\+|--|\.\w+\()"
        rf"|(?:\+\+|--|&)\s*\b{esc}\b"
        rf"|\bdel\s+[^\n;]*\b{esc}\b"
        rf"|\b(?!if\b|elif\b|while\b|switch\b|return\b|len\b|sizeof\b)\w+\s*\([^;\n]*\b{esc}\b"
    )
    depth = 0
    for j in range(end_idx + 1, min(total, end_idx + 13)):
      ln = cleaned_lines[j]
      if not ln.strip():
        continue
      cur_ind = len(ln) - len(ln.lstrip())
      if ext == ".py":
        if cur_ind < ind:
          break
        if cur_ind > ind:
          continue
        m_if = re.match(r"^\s*if\s+([^:]+):", ln)
      else:
        s_ln = STRING_LITERAL_RE.sub("", ln)
        # Stop at preprocessor conditionals (`#if/#else/#endif`), block ends, or switch/goto labels
        if re.match(r"^\s*#\s*(?:if|ifdef|ifndef|elif|else|endif)\b", s_ln):
          break
        is_block_end = s_ln.lstrip().startswith("}") or re.match(
            r"^\s*(?:case\b|default\s*:|[A-Za-z_]\w*\s*:(?!:))", s_ln
        )
        if depth == 0 and (is_block_end or cur_ind < ind):
          break
        if depth == 0:
          m_if = re.match(r"^\s*if\s*\(([^()]+(?:\(\))?[^()]*)\)", s_ln)
        else:
          m_if = None
        depth += s_ln.count("{") - s_ln.count("}")
        if depth < 0:
          break
        if m_if is None and depth > 0:
          if mut_re.search(s_ln) or (is_member and re.search(r"\b\w+\s*\(", s_ln)):
            break
          continue

      # Matching polarity after exit is unreachable dead code; opposite polarity is tautological
      if m_if:
        can2 = _canonicalize_guard_cond(m_if.group(1))
        if can2 and can2[0] == ident:
          kind = "dead branch" if can2[1] == exit_pol else "tautological guard"
          out.append((
              f"{rel_path}:L{j + 1}",
              f"{kind} on `{ident}` after early exit at L{idx + 1}",
          ))
          break
      # Stop once the guarded symbol may be reassigned, passed to a call, or mutated in place
      if mut_re.search(ln) or (is_member and re.search(r"\b\w+\s*\(", ln)):
        break
  return out


_CPP_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]\s*$')
_CPP_PREPROC_RE = re.compile(r"^\s*#\s*(\w+)(.*)$")
_CPP_ACCESS_RE = re.compile(r"(?m)^\s*(?:public|private|protected)\s*:")
_CPP_STR_OR_CHAR_RE = re.compile(
    r'"(?:\\.|[^"\\\n])*"|(?<![0-9A-Za-z_])\'(?:\\.|[^\'\\\n]){1,4}\''
)
_CPP_TYPE_DECL_RE = re.compile(
    r"\b(?:class|struct)\s+(?:[A-Z_][A-Z0-9_]*\s+)*([A-Za-z_]\w*)"
    r"\s*(?:final\b)?\s*(?::\s*([^{;]+))?$"
)
_CPP_FN_BODY_TAIL_RE = re.compile(
    r"(?:\)|\}|\]\]|&&?|->\s*[\w:<>~*&\s,]+"
    r"|\b(?:const|noexcept|volatile|final|[A-Z_][A-Z0-9_]*))\s*$"
)


def _extract_cpp_includes(
    comment_stripped: str, allow_header_guard: bool
) -> tuple[set[str], str] | None:
  """Extract `#include` targets and mask literals; return None on macros or `#if` guards."""
  includes: set[str] = set()
  out_lines: list[str] = []
  saw_pragma = False
  saw_content = False
  guard_ident: str | None = None
  guard_defined = False
  guard_open = False
  guard_closed = False
  for line in comment_stripped.splitlines():
    inc_m = _CPP_INCLUDE_RE.match(line)
    if inc_m:
      if guard_closed or (guard_open and not guard_defined):
        return None
      saw_content = True
      includes.add(inc_m.group(1).strip())
      out_lines.append("")
      continue
    pp_m = _CPP_PREPROC_RE.match(line)
    if pp_m:
      kw, rest = pp_m.group(1), pp_m.group(2).strip()
      if allow_header_guard and kw == "pragma" and rest == "once" and guard_ident is None:
        saw_pragma = True
        out_lines.append("")
        continue
      # Accept at most one outer `#ifndef X` / `#define X` / `#endif` include guard in `.h`
      if (
          allow_header_guard
          and kw == "ifndef"
          and not saw_pragma
          and not saw_content
          and guard_ident is None
          and re.match(r"^\w+$", rest)
      ):
        guard_ident, guard_open = rest, True
        out_lines.append("")
        continue
      if (
          allow_header_guard
          and kw == "define"
          and guard_open
          and not guard_defined
          and rest == guard_ident
      ):
        guard_defined = True
        out_lines.append("")
        continue
      if allow_header_guard and kw == "endif" and guard_open and guard_defined:
        guard_open, guard_closed = False, True
        out_lines.append("")
        continue
      # Conditional compilation (`#if`/`#ifdef`) or macros make `.cpp` non-trivial to inline
      return None
    masked = _CPP_STR_OR_CHAR_RE.sub('""', line)
    if masked.strip():
      if guard_closed or (guard_open and not guard_defined):
        return None
      saw_content = True
    out_lines.append(masked)
  return None if guard_open else (includes, "\n".join(out_lines))


def _find_matching_brace(text: str, open_idx: int) -> int | None:
  """Return index of `}` matching `text[open_idx] == '{'`, or None if unbalanced; pure."""
  depth = 1
  for j in range(open_idx + 1, len(text)):
    ch = text[j]
    if ch == "{":
      depth += 1
    elif ch == "}":
      depth -= 1
      if depth == 0:
        return j
  return None


def _split_sig_and_init(raw_head: str) -> tuple[str, str] | None:
  """Split C++ declaration header before `{` into `(before_params, post_params)`; pure."""
  first_open = raw_head.find("(")
  if first_open < 0:
    return None
  before = raw_head[:first_open]
  # Advance past `operator()` empty parens when followed by the call operator's parameter list
  pos = first_open
  while pos < len(raw_head) and raw_head[pos] == "(":
    depth = 0
    close_idx = -1
    for j in range(pos, len(raw_head)):
      if raw_head[j] == "(":
        depth += 1
      elif raw_head[j] == ")":
        depth -= 1
        if depth == 0:
          close_idx = j
          break
    if close_idx < 0:
      return None
    nxt = close_idx + 1
    while nxt < len(raw_head) and raw_head[nxt].isspace():
      nxt += 1
    if re.search(r"\boperator\s*$", before) and pos == first_open and nxt < len(raw_head):
      if raw_head[nxt] == "(":
        before = raw_head[:nxt]
        pos = nxt
        continue
    return (before, raw_head[close_idx + 1 :])
  return None


def _count_method_body_lines(init_seg: str, body_seg: str) -> int | None:
  """Count ctor-init + non-brace statement lines, or return None on function-local `static`."""
  if re.search(r"\bstatic\b", body_seg):
    return None
  colon_m = re.search(r"(?<!:):(?!:)", init_seg)
  init_lines = (
      sum(1 for ln in init_seg[colon_m.start() :].splitlines() if ln.strip()) if colon_m else 0
  )
  stmt_lines = sum(
      1 for ln in body_seg.splitlines() if ln.strip() and ln.strip() not in ("{", "}", "};")
  )
  return init_lines + stmt_lines


def _inspect_private_header(hdr_masked: str) -> set[str] | None:
  """Return non-virtual class/struct names in `hdr_masked` if inline bodies are <= 10 lines."""
  if re.search(r"\b(?:virtual|override|template)\b", hdr_masked):
    return None
  text = _CPP_ACCESS_RE.sub("", hdr_masked)
  hdr_classes: set[str] = set()
  scope_stack: list[str] = []
  paren_depth, stmt_start, i, n = 0, 0, 0, len(text)
  while i < n:
    ch = text[i]
    if ch == "(":
      paren_depth += 1
    elif ch == ")":
      paren_depth = max(0, paren_depth - 1)
    elif paren_depth == 0 and ch == ";":
      stmt_start = i + 1
    elif paren_depth == 0 and ch == "{":
      raw_head = text[stmt_start:i]
      head = " ".join(raw_head.split())
      if re.match(r"^namespace\s+[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*$", head):
        scope_stack.append("ns")
        stmt_start = i + 1
      elif re.search(r"\benum\b", head) and "(" not in head:
        scope_stack.append("enum")
        stmt_start = i + 1
      elif re.search(r"\b(?:class|struct|union)\b", head) and "(" not in head:
        m = _CPP_TYPE_DECL_RE.search(head)
        if not m or "union" in head.split():
          return None
        # Reject inherited bases other than SkNoncopyable because base vtables need a key function
        if m.group(2):
          bases = [
              re.sub(r"^\b(?:public|private|protected)\b\s*", "", b.strip())
              for b in m.group(2).split(",")
          ]
          if any(b != "SkNoncopyable" for b in bases):
            return None
        hdr_classes.add(m.group(1))
        scope_stack.append("type")
        stmt_start = i + 1
      elif "(" in head and _CPP_FN_BODY_TAIL_RE.search(raw_head):
        sig_init = _split_sig_and_init(raw_head)
        end_b = _find_matching_brace(text, i)
        if not sig_init or end_b is None:
          return None
        cnt = _count_method_body_lines(sig_init[1], text[i + 1 : end_b])
        if cnt is None or cnt > 10:
          return None
        i, stmt_start = end_b, end_b + 1
      else:
        # Skip value/member brace-initializers (`int fX{0};`, `: fArr{1}`) keeping `stmt_start`
        end_b = _find_matching_brace(text, i)
        if end_b is None:
          return None
        i = end_b
    elif paren_depth == 0 and ch == "}":
      if not scope_stack:
        return None
      scope_stack.pop()
      stmt_start = i + 1
    i += 1
  return hdr_classes if (hdr_classes and not scope_stack) else None


def _is_stub_cpp_body(cpp_masked: str, hdr_classes: set[str]) -> bool:
  """Return True when `.cpp` holds only <= 10-line out-of-line methods of `hdr_classes`."""
  if re.search(r"\b(?:virtual|override|template)\b", cpp_masked):
    return False
  scope_stack: list[str] = []
  paren_depth, stmt_start, i, n = 0, 0, 0, len(cpp_masked)
  while i < n:
    ch = cpp_masked[i]
    if ch == "(":
      paren_depth += 1
    elif ch == ")":
      paren_depth = max(0, paren_depth - 1)
    elif paren_depth == 0 and ch == ";":
      stmt = " ".join(cpp_masked[stmt_start:i].split())
      stmt_start = i + 1
      if not stmt:
        i += 1
        continue
      # Allow out-of-line `= default`/`= delete` members while rejecting globals/static data
      m_def = re.search(
          r"\b([A-Za-z_]\w*)::(?:~\1|[A-Za-z_]\w*|operator\b[^(]*)\s*\([^;]*\)\s*"
          r"(?:const\s*)?(?:&&?\s*)?(?:noexcept\s*)?=\s*(?:default|delete)$",
          stmt,
      )
      if not m_def or m_def.group(1) not in hdr_classes or re.search(r"\bstatic\b", stmt):
        return False
    elif paren_depth == 0 and ch == "{":
      raw_head = cpp_masked[stmt_start:i]
      head = " ".join(raw_head.split())
      if re.match(r"^namespace\s+[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*$", head):
        scope_stack.append("ns")
        stmt_start = i + 1
      elif "(" in head and re.search(r"(?<!:):(?!:)", raw_head) and not _CPP_FN_BODY_TAIL_RE.search(
          raw_head
      ):
        # Skip brace member-initializers inside ctor initializer lists before the body `{`
        end_b = _find_matching_brace(cpp_masked, i)
        if end_b is None:
          return False
        i = end_b
      else:
        if re.search(r"\b(?:static|extern|namespace|class|struct|enum|union)\b", head):
          return False
        if not _CPP_FN_BODY_TAIL_RE.search(raw_head):
          return False
        sig_init = _split_sig_and_init(raw_head)
        end_b = _find_matching_brace(cpp_masked, i)
        if not sig_init or end_b is None:
          return False
        # Require `ClassName::Method` before `(` so free helpers taking `ClassName` are excluded
        cls_m = re.search(
            r"\b([A-Za-z_]\w*)::(?:~\1|[A-Za-z_]\w*|operator\b.*)$", sig_init[0].strip()
        )
        if not cls_m or cls_m.group(1) not in hdr_classes:
          return False
        cnt = _count_method_body_lines(sig_init[1], cpp_masked[i + 1 : end_b])
        if cnt is None or cnt > 10:
          return False
        i, stmt_start = end_b, end_b + 1
    elif paren_depth == 0 and ch == "}":
      if not scope_stack:
        return False
      scope_stack.pop()
      stmt_start = i + 1
    i += 1
  return not scope_stack and not cpp_masked[stmt_start:].strip()


def _check_stub_cpp_file(
    full_path: pathlib.Path, rel_path: str, cpp_content: str
) -> tuple[str, str] | None:
  """Flag private `src/*.cpp` files whose `<= 10`-line methods belong in-class in paired `.h`."""
  norm_rel = rel_path.replace("\\", "/")
  rel_parts = pathlib.PurePosixPath(norm_rel).parts
  dir_parts = rel_parts[:-1]
  if "src" not in dir_parts or "include" in dir_parts or "public" in dir_parts:
    return None
  hdr_path = next(
      (
          full_path.with_suffix(suf)
          for suf in (".h", ".hpp")
          if full_path.with_suffix(suf).is_file()
      ),
      None,
  )
  if hdr_path is None:
    return None
  hdr_rel = str(pathlib.PurePosixPath(norm_rel).with_suffix(hdr_path.suffix))
  try:
    hdr_raw = hdr_path.read_text(encoding="utf-8", errors="replace")
  except OSError:
    return None
  cpp_parsed = _extract_cpp_includes(
      _strip_comments_for_ext(cpp_content, ".cpp", keep_newlines=True), allow_header_guard=False
  )
  hdr_parsed = _extract_cpp_includes(
      _strip_comments_for_ext(hdr_raw, ".h", keep_newlines=True), allow_header_guard=True
  )
  if not cpp_parsed or not hdr_parsed:
    return None
  cpp_incs, cpp_masked = cpp_parsed
  hdr_incs, hdr_masked = hdr_parsed
  is_self = lambda inc: inc in (hdr_rel, hdr_path.name) or hdr_rel.endswith("/" + inc)
  if not any(is_self(inc) for inc in cpp_incs):
    return None
  # Keep `.cpp` files that isolate extra `#include` dependencies behind forward declarations
  repo_root = full_path.parents[len(rel_parts) - 1]
  resolve_inc = lambda base_dir, inc: (
      (repo_root / inc).resolve() if (repo_root / inc).is_file() else (base_dir / inc).resolve()
  )
  hdr_resolved = {resolve_inc(hdr_path.parent, h) for h in hdr_incs}
  for inc in cpp_incs:
    if not is_self(inc) and inc not in hdr_incs:
      if resolve_inc(full_path.parent, inc) not in hdr_resolved:
        return None
  hdr_classes = _inspect_private_header(hdr_masked)
  if not hdr_classes or not _is_stub_cpp_body(cpp_masked, hdr_classes):
    return None
  return (
      f"{rel_path}:L1",
      f"[STUB_CPP] move <= 10-line methods in-class into `{hdr_rel}` and delete stub `.cpp`",
  )


def scan_horrors_and_invariants(file_records, allow_cyrillic=False):
  """Scan files for KEEPER invariant breaches, anti-slop horrors, and test smells."""
  horrors = defaultdict(list)
  endpoints = defaultdict(set)

  for full_path, rel_path, content, lines in file_records:
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

    if test_flag and ext in (".cc", ".cpp", ".cxx", ".mm") and FONT_MGR_RE.search(content):
      for m in CPP_TEST_DEF_RE.finditer(content):
        depth, i = 0, m.end() - 1
        while i < len(content):
          depth += {"{": 1, "}": -1}.get(content[i], 0)
          if depth == 0:
            break
          i += 1
        body = content[m.start():i+1]
        if not FONT_MGR_RE.search(body) or FONT_BACKEND_GUARD_RE.search(body):
          continue
        for pm in FONT_METRIC_PIN_RE.finditer(body):
          ln = content.count("\n", 0, m.start() + pm.start()) + 1
          horrors["test_font_metric_pins"].append(
              (f"{rel_path}:L{ln}", " ".join(pm.group(0).split())[:100])
          )

    # Cross-language simplification & prototype-scar checks (R138)
    if ext in (
        ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx", ".m", ".mm",
        ".dart", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".py",
    ):
      cleaned_for_cf = _strip_comments_for_ext(content, ext, keep_newlines=True).splitlines()
      if ext == ".py":
        horrors["unused_imports"].extend(
            _scan_unused_py_imports(rel_path, content, lines, cleaned_for_cf)
        )
      elif ext in (".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".dart"):
        horrors["unused_imports"].extend(
            _scan_unused_ts_or_dart_imports(rel_path, ext, content, lines, cleaned_for_cf)
        )
      if not test_flag:
        horrors["consecutive_collection_loops"].extend(
            _scan_consecutive_loops(rel_path, ext, cleaned_for_cf)
        )
        if ext in (".cc", ".cpp", ".cxx"):
          stub_hit = _check_stub_cpp_file(full_path, rel_path, content)
          if stub_hit:
            horrors["stub_cpp_files"].append(stub_hit)
      horrors["post_early_exit_tautologies"].extend(
          _scan_post_early_exit_guards(rel_path, ext, cleaned_for_cf)
      )

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


def scan_diff_doc_drift(repo_root: pathlib.Path, diff_ref: str | None) -> list[tuple[str, str]]:
  """Checks git diff against diff_ref for stale governing-doc references (R4 Check B)."""
  if not diff_ref:
    return []
  try:
    src = stage_diff_context._load_local_source(str(repo_root), diff_ref)
  except (OSError, ValueError):
    return []
  patch_text = src.get("patch_text") or ""
  if not patch_text.strip():
    return []
  _, deleted_paths, _ = stage_diff_context._parse_side_b_hunk_ranges(patch_text)
  changed_paths = [
      pf.path
      for pf in stage_diff_context.triage_cl.parse_patch(patch_text)
      if pf.path != "/COMMIT_MSG" and pf.path not in deleted_paths
  ]
  drift = stage_diff_context.compute_governing_doc_drift(
      repo_root,
      changed_paths,
      deleted_paths,
      patch_text,
      src.get("commit_msg") or "",
  )
  return [
      (
          f"{dc['doc_path']}:L{dc['doc_line']}",
          f"[DOC_DRIFT:{dc['kind']}] `{dc['token']}` removed in `{dc['source_file']}`",
      )
      for dc in (drift.get("drift_candidates") or [])
  ]


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
      ("test_font_metric_pins", "Cross-Platform Font Metric Pinning in Tests (Invariant 10)"),
      # Report Dart/WebParagraph test and production hygiene categories
      ("dart_test_raw_asserts", "Raw `assert(...)` in Dart Tests Instead of `expect(...)` (Rule M4)"),
      ("dart_test_non_ci_fonts", "Non-CI Desktop Font Family in Dart Tests (Rule M4)"),
      ("dart_zero_color_checks", "Incomplete Transparent Color Check `Color(0x00000000)` (Rule M6)"),
      ("dart_un_treeshaken_debug", "Un-Tree-Shaken `WebParagraphDebug` Call or Non-`const` Flag (Rule M6)"),
      # Surface R138 cross-language prototype-scar and control-flow simplification categories
      ("unused_imports", "Orphaned / Unused Explicit Imports (Prototype-Scar Elimination)"),
      ("consecutive_collection_loops", "Fuseable Consecutive Same-Collection Loops"),
      ("post_early_exit_tautologies", "Tautological or Dead Re-Checks Below Early Return/Continue"),
      (
          "stub_cpp_files",
          "Private `src/` Stub `.cpp` Files Belonging In-Class in `.h` (`[STUB_CPP]`)",
      ),
      (
          "doc_code_drift",
          "Stale Governing Doc References After Code Diff (`[DOC_DRIFT]`, R4)",
      ),
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
  if args.diff:
    doc_drift_hits = scan_diff_doc_drift(repo_root, args.diff)
    if doc_drift_hits:
      horrors["doc_code_drift"].extend(doc_drift_hits)
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
