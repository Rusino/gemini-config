#!/usr/bin/env python3
"""Mechanical test liveness gate: a test that passed must be built, run by name, non-vacuous.

Usage:
  verify_test_liveness.py --repo . --test-file tests/FooTest.cpp --test-name Foo_Bar \
      --log /tmp/dm_verbose.log [--explained-warning REGEX ...] [--allow-device-paths]

Exit code 1 lists every violation. The mutation check (one production edit must make the test
fail) cannot be automated generically and stays a manual step in amnesia-precommit/SKILL.md
"""

import argparse
import os
import re
import subprocess
import sys

# Git pathspec `*` crosses `/`, so these match build lists at any depth
BUILD_GLOBS = ["*.gni", "*.gn", "*BUILD", "*BUILD.bazel", "*CMakeLists.txt", "*.cmake", "*.gyp"]

# Loop bounds that come from a runtime collection; iterating zero times passes vacuously
LOOP_BOUND_RE = re.compile(
    r"for\s*\([^;]*;\s*\w+\s*<=?\s*([\w.\->]*?(\w*(?:count|size|length)\w*)\s*\([^)]*\))",
    re.IGNORECASE,
)
RANGE_FOR_RE = re.compile(
    r"for\s*\(\s*(?:const\s+)?(?:auto|[\w:<>]+)\s*&{0,2}\s*\w+\s*:\s*([\w.\->]+)\s*\)"
)
ASSERT_RE = re.compile(
    r"\b(REPORTER_ASSERT|ASSERT_\w+|EXPECT_\w+|SkASSERT_RELEASE|CHECK\w*|expect)\b"
)

# Host has no device config; tests reading these paths silently test an empty environment
DEVICE_PATH_RE = re.compile(r"[\"'](/system/|/vendor/|/product/|/odm/)")
DEVICE_DEFAULT_RE = re.compile(r"SkFontMgr_New_Android\s*\(\s*nullptr")

LOG_WARNING_RE = re.compile(
    r"could not be opened|no such file|not found|cannot open|failed to (?:open|load)|warning",
    re.IGNORECASE,
)


def _registered(repo: str, test_file: str) -> list[str]:
  base = os.path.basename(test_file)
  res = subprocess.run(
      ["git", "-C", repo, "grep", "-l", "-F", base, "--", *BUILD_GLOBS],
      capture_output=True, text=True, check=False,
  )
  return [ln for ln in res.stdout.splitlines() if ln.strip()]


# Restrict loop checks to the named test's body; helpers it calls predate the diff
def _test_body(code: str, name: str) -> tuple[int, str]:
  m = re.search(rf"\b{re.escape(name)}\b[^{{;]*\{{", code)
  if not m:
    return 0, code
  depth, i = 0, m.end() - 1
  while i < len(code):
    depth += {"{": 1, "}": -1}.get(code[i], 0)
    if depth == 0:
      break
    i += 1
  return m.start(), code[m.start(): i + 1]


def _vacuous_loops(code: str, offset: int, full: str) -> list[str]:
  issues = []
  for m in LOOP_BOUND_RE.finditer(code):
    expr, method = m.group(1), m.group(2)
    before = code[: m.start()]
    guarded = any(
        ASSERT_RE.search(ln)
        and method in ln
        and re.search(r">\s*0|>=\s*1|!\s*\w.*empty|\bempty\b", ln)
        for ln in before.splitlines()
    )
    if not guarded:
      line = full.count("\n", 0, offset + m.start()) + 1
      issues.append(f"L{line}: loop bound `{expr}` has no preceding non-empty assert")
  for m in RANGE_FOR_RE.finditer(code):
    coll = m.group(1).split(".")[-1].split("->")[-1]
    # Compile-time tables (`kChars`) can't be empty at runtime
    if re.fullmatch(r"k[A-Z]\w*", coll):
      continue
    before = code[: m.start()]
    guarded = any(
        ASSERT_RE.search(ln) and coll in ln and re.search(r"empty|size|count|>\s*0", ln)
        for ln in before.splitlines()
    )
    if not guarded:
      line = full.count("\n", 0, offset + m.start()) + 1
      issues.append(f"L{line}: range-for over `{m.group(1)}` has no preceding non-empty assert")
  return issues


def check(args) -> list[str]:
  errors = []
  repo = os.path.abspath(args.repo)
  test_path = os.path.join(repo, args.test_file)
  code = open(test_path, encoding="utf-8", errors="replace").read()

  if not _registered(repo, args.test_file):
    errors.append(
        f"[NOT BUILT] `{os.path.basename(args.test_file)}` is in no build list "
        f"({', '.join(BUILD_GLOBS)}); it never compiled"
    )

  for name in args.test_name:
    if not re.search(rf"\b{re.escape(name)}\b", code):
      errors.append(f"[WRONG FILE] test `{name}` is not defined in `{args.test_file}`")

  if args.log:
    log = open(args.log, encoding="utf-8", errors="replace").read()
    for name in args.test_name:
      if not re.search(rf"\b{re.escape(name)}\b", log):
        errors.append(
            f"[NOT RUN] `{name}` is absent from `{args.log}`; a count like `N tests done` is "
            "not proof (rerun verbose, e.g. `dm --src tests --match <Name> --verbose`)"
        )
    explained = [re.compile(x) for x in args.explained_warning]
    unexplained = sorted({
        ln.strip() for ln in log.splitlines()
        if LOG_WARNING_RE.search(ln) and not any(x.search(ln) for x in explained)
    })
    if unexplained:
      shown = "; ".join(f"`{u[:100]}`" for u in unexplained[:5])
      errors.append(
          f"[UNEXPLAINED LOG WARNINGS] {shown}. Root-cause them before filtering; pass "
          "--explained-warning REGEX only once the cause is known and harmless"
      )
  else:
    errors.append("[NO LOG] pass --log with the verbose runner output of this test run")

  for name in args.test_name:
    offset, body = _test_body(code, name)
    for issue in _vacuous_loops(body, offset, code):
      errors.append(f"[MAYBE VACUOUS] {args.test_file}:{issue}")
    if args.allow_device_paths:
      continue
    for rx, what in ((DEVICE_PATH_RE, "device path literal"),
                     (DEVICE_DEFAULT_RE, "SkFontMgr_New_Android(nullptr) reads /system/etc")):
      for m in rx.finditer(body):
        line = code.count("\n", 0, offset + m.start()) + 1
        errors.append(
            f"[HOST-EMPTY ENV] {args.test_file}:L{line}: {what}; use resources/ fixtures on host"
        )
  return errors


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
  ap.add_argument("--repo", default=".")
  ap.add_argument("--test-file", required=True, help="path relative to --repo")
  ap.add_argument("--test-name", action="append", required=True)
  ap.add_argument("--log", help="verbose test runner output")
  ap.add_argument("--explained-warning", action="append", default=[])
  ap.add_argument("--allow-device-paths", action="store_true")
  args = ap.parse_args()
  errors = check(args)
  for e in errors:
    print(e)
  if errors:
    return 1
  print("[LIVENESS OK] built, run by name, non-vacuous. Still required: one mutation must fail it")
  return 0


if __name__ == "__main__":
  sys.exit(main())
