#!/usr/bin/env python3
"""Resource and structure lints for Skia C++ text code (SkoreText CoreText pipeline first).

Blocking rules catch defect classes found in the SkoreText audit (2026-11-07):
  cf-nocopy-null   CF*NoCopy(..., kCFAllocatorNull) without a `LIFETIME:` comment
                   (borrowed bytes outliving their owner, finding X1)
  unbounded-cache  hash/map containers in singletons or function statics without SkLRUCache
                   or a `BOUNDED:` comment (finding B3)
  utf-decode-owner SkUTF decoding outside the one owner file of a module (finding A1)

Advisory rules point a human reviewer at readability problems; they never fail the run,
because hard length or loop caps get gamed by splitting code into context-struct helpers:
  same-range-loops     >= 3 `for` loops over the same bound in one function
  long-no-contract     function longer than --long-func lines without a header comment

With --diff REF only findings on lines added since REF are reported (function-level advisories:
functions that contain an added line). Exit code 1 when a blocking finding is reported.
"""

import argparse
import fnmatch
import json
import pathlib
import re
import subprocess
import sys

CXX_EXTS = {".cpp", ".cc", ".h", ".hpp", ".mm"}
NOCOPY_RE = re.compile(r"\b(CF\w*NoCopy)\s*\(")
CONTAINER_RE = re.compile(
    r"\b(?:skia_private::)?(THashMap|THashSet|std::unordered_map|std::unordered_set|std::map|"
    r"std::set)\s*<")
SINGLETON_RE = re.compile(r"\bInstance\s*\(\s*\)")
STATIC_LOCAL_RE = re.compile(r"^\s+static\s+(?!constexpr|const\b)")
UTF_RE = re.compile(r"\bSkUTF::(NextUTF8WithReplacement|NextUTF8|NextUTF16|ToUTF16|ToUTF8)\b")
FOR_BOUND_RE = re.compile(r"\bfor\s*\([^;]*;\s*[\w.\->\[\]]+\s*(<=?|!=|>=?)\s*([^;]+);")
FUNC_START_RE = re.compile(r"^(?!\s)(?!(?:if|for|while|switch|return|namespace|class|struct|enum|"
                           r"using|typedef|template|#|//|/\*|\}))[^;{}]*\([^;]*$|"
                           r"^(?!\s)(?!(?:if|for|while|switch|return|namespace|class|struct|enum|"
                           r"using|typedef|#|//|/\*|\}))[^;{}]*\)[^;{}]*\{\s*$")


def strip_line(line):
    line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
    line = re.sub(r"'(?:\\.|[^'\\])*'", "''", line)
    return line.split("//", 1)[0]


def has_marker(lines, idx, marker, above=3):
    lo = max(0, idx - above)
    return any(marker in lines[i] for i in range(lo, idx + 1))


def find_functions(lines):
    """Return (name, start, body_open, end) for top-level function definitions (0-based)."""
    funcs = []
    i, n = 0, len(lines)
    while i < n:
        if not FUNC_START_RE.match(lines[i]):
            i += 1
            continue
        # The body opens on this line or within the next few lines, before any ';'
        j, opened = i, -1
        while j < n and j < i + 8:
            s = strip_line(lines[j])
            if ";" in s and "{" not in s:
                break
            if "{" in s:
                opened = j
                break
            j += 1
        if opened < 0:
            i += 1
            continue
        depth, k = 0, opened
        while k < n:
            s = strip_line(lines[k])
            depth += s.count("{") - s.count("}")
            if depth <= 0:
                break
            k += 1
        m = re.search(r"([~\w:]+)\s*\(", lines[i])
        funcs.append((m.group(1) if m else "?", i, opened, min(k, n - 1)))
        i = k + 1
    return funcs


def lint_file(rel, lines, args):
    out = []
    in_utf_scope = any(fnmatch.fnmatch(rel, g) for g in args.utf_scope)
    is_utf_owner = any(fnmatch.fnmatch(rel, g) for g in args.utf_owner)
    has_singleton = any(SINGLETON_RE.search(strip_line(l)) and "static" in l for l in lines)

    for idx, raw in enumerate(lines):
        s = strip_line(raw)
        m = NOCOPY_RE.search(s)
        if m:
            stmt = " ".join(strip_line(l) for l in lines[idx:idx + 8])
            stmt = stmt.split(";", 1)[0]
            if "kCFAllocatorNull" in stmt and not has_marker(lines, idx, "LIFETIME:"):
                out.append(("BLOCK", "cf-nocopy-null", idx,
                            f"{m.group(1)}(..., kCFAllocatorNull) borrows bytes; write who owns "
                            "them and why they outlive every CF object, as a `LIFETIME:` comment"))
        cm = CONTAINER_RE.search(s)
        if cm and "SkLRUCache" not in s and not has_marker(lines, idx, "BOUNDED:"):
            is_static_local = STATIC_LOCAL_RE.match(s) is not None
            is_member = has_singleton and re.search(r"\bf[A-Z]\w*\s*;", s) is not None
            if is_static_local or is_member:
                where = "function static" if is_static_local else "singleton member"
                out.append(("BLOCK", "unbounded-cache", idx,
                            f"{cm.group(1)} as {where} has no bound or purge hook; use "
                            "SkLRUCache or add a `BOUNDED:` comment that states the bound"))
        if in_utf_scope and not is_utf_owner:
            um = UTF_RE.search(s)
            if um:
                out.append(("BLOCK", "utf-decode-owner", idx,
                            f"SkUTF::{um.group(1)} outside the UTF owner "
                            f"({', '.join(args.utf_owner)}); take indices or UTF-16 from it"))

    for name, start, opened, end in find_functions(lines):
        bounds = {}
        for idx in range(opened, end + 1):
            fm = FOR_BOUND_RE.search(strip_line(lines[idx]))
            if fm:
                key = re.sub(r"\s+", "", fm.group(2))
                bounds.setdefault(key, []).append(idx)
        for key, where in bounds.items():
            if len(where) >= args.same_range:
                out.append(("WARN", "same-range-loops", where[0],
                            f"{name}: {len(where)} loops over `{key}` (lines "
                            f"{', '.join(str(w + 1) for w in where)}); if they are steps of one "
                            "computation, name each step as a stage function with a short contract; "
                            "fuse only steps that read the same data in the same direction",
                            (start, end)))
        length = end - start + 1
        if length > args.long_func:
            prev = lines[start - 1].strip() if start > 0 else ""
            if not (prev.startswith("//") or prev.endswith("*/")):
                out.append(("WARN", "long-no-contract", start,
                            f"{name}: {length} lines without a header comment; state inputs, "
                            "outputs and stage order in 2-4 lines, or split along real data "
                            "dependencies", (start, end)))
    return out


def added_lines(repo, ref, rel):
    res = subprocess.run(["git", "-C", str(repo), "diff", "-U0", ref, "--", rel],
                         capture_output=True, text=True, check=False)
    added = set()
    for line in res.stdout.splitlines():
        m = re.match(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)
        if m:
            first, count = int(m.group(1)), int(m.group(2) or "1")
            added.update(range(first - 1, first - 1 + count))
    return added


def collect(repo, paths):
    files = []
    for p in paths:
        path = (repo / p).resolve()
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(f for f in sorted(path.rglob("*")) if f.suffix in CXX_EXTS)
    return [f for f in files if f.suffix in CXX_EXTS]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=["modules/skparagraph/src"])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--diff", metavar="REF", help="report only lines added since REF")
    ap.add_argument("--utf-scope", action="append",
                    help="glob of files where SkUTF decoding is owned by one file "
                         "(default modules/skparagraph/src/CoreText*)")
    ap.add_argument("--utf-owner", action="append",
                    help="glob of the owner file (default */CoreTextIndexMap.cpp)")
    ap.add_argument("--same-range", type=int, default=3)
    ap.add_argument("--long-func", type=int, default=80)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args()
    args.utf_scope = args.utf_scope or ["modules/skparagraph/src/CoreText*"]
    args.utf_owner = args.utf_owner or ["*/CoreTextIndexMap.cpp"]

    repo = pathlib.Path(args.repo).resolve()
    report = []
    for f in collect(repo, args.paths):
        rel = str(f.relative_to(repo))
        lines = f.read_text(errors="replace").splitlines()
        findings = lint_file(rel, lines, args)
        if args.diff:
            added = added_lines(repo, args.diff, rel)
            kept = []
            for fd in findings:
                span = fd[4] if len(fd) > 4 else (fd[2], fd[2])
                if fd[0] == "WARN" and len(fd) > 4:
                    if any(span[0] <= a <= span[1] for a in added):
                        kept.append(fd)
                elif fd[2] in added:
                    kept.append(fd)
            findings = kept
        for fd in findings:
            report.append({"level": fd[0], "rule": fd[1], "file": rel, "line": fd[2] + 1,
                           "message": fd[3]})

    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        for r in report:
            print(f"{r['level']} {r['rule']} {r['file']}:{r['line']}: {r['message']}")
        blocks = sum(r["level"] == "BLOCK" for r in report)
        print(f"\nresource_lints: {blocks} blocking, {len(report) - blocks} advisory")
    return 1 if any(r["level"] == "BLOCK" for r in report) else 0


if __name__ == "__main__":
    sys.exit(main())
