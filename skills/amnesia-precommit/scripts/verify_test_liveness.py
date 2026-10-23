#!/usr/bin/env python3
"""Mechanical test liveness gate: a test that passed must be built, run by name, non-vacuous.

Usage:
  verify_test_liveness.py --repo . --diff origin/main --log /tmp/dm_verbose.log
  verify_test_liveness.py --repo . --test-file tests/FooTest.cpp --test-name Foo_Bar \
      --log /tmp/dm_verbose.log [--explained-warning REGEX ...] [--allow-device-paths]

`--diff REV` checks every test whose definition is added or touched since REV. Each test also
needs a PASS attestation from mutation_check.py for its current file contents. Exit code 1
lists every violation
"""

import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mutation_check  # noqa: E402

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


TEST_FILE_RE = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*$|_test\.\w+$|Test\.\w+$")
# Skia DEF_TEST(Name, ...), gtest TEST(Suite, Name), Python `def test_x`
TEST_DEF_RE = re.compile(
    r"\b(?:DEF_\w*TEST\w*\(\s*(\w+)|TEST(?:_F|_P)?\(\s*(\w+)\s*,\s*(\w+)|def\s+(test_\w+))"
)


def discover_changed_tests(repo: str, rev: str) -> dict[str, list[str]]:
  files = subprocess.run(
      ["git", "-C", repo, "diff", "--name-only", "--diff-filter=AM", rev],
      capture_output=True, text=True, check=True,
  ).stdout.split()
  found: dict[str, list[str]] = {}
  for rel in files:
    if not TEST_FILE_RE.search(rel) or not os.path.exists(os.path.join(repo, rel)):
      continue
    diff = subprocess.run(
        ["git", "-C", repo, "diff", "-U0", rev, "--", rel],
        capture_output=True, text=True, check=True,
    ).stdout
    added = set()
    for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", diff, re.M):
      start, count = int(m.group(1)), int(m.group(2) or 1)
      added.update(range(start, start + count))
    code = open(os.path.join(repo, rel), encoding="utf-8", errors="replace").read()
    for m in TEST_DEF_RE.finditer(code):
      name = m.group(1) or m.group(4) or f"{m.group(2)}.{m.group(3)}"
      offset, body = _test_body(code, m.group(1) or m.group(3) or m.group(4))
      first = code.count("\n", 0, offset) + 1
      last = first + body.count("\n")
      if any(first <= ln <= last for ln in added):
        found.setdefault(rel, []).append(name)
  return found


def _attestation_errors(repo: str, test_file: str, name: str) -> list[str]:
  path = mutation_check.attestation_path(repo, name)
  hint = "run mutation_check.py --test-file {} --test-name {} with a mutation of the covered code"
  if not os.path.exists(path):
    return [f"[NO MUTATION ATTESTATION] `{name}`: " + hint.format(test_file, name)]
  with open(path, encoding="utf-8") as f:
    att = json.load(f)
  if att.get("result") != "PASS":
    return [f"[MUTATION FAIL] `{name}`: last mutation run did not fail the test ({path})"]
  if att.get("test_file") != test_file or att.get("test_file_sha256") != mutation_check.sha256_file(
      os.path.join(repo, test_file)
  ):
    return [f"[STALE MUTATION ATTESTATION] `{name}`: test file changed since {path}; rerun"]
  return []


def check(repo: str, test_file: str, names: list[str], args) -> list[str]:
  errors = []
  test_path = os.path.join(repo, test_file)
  code = open(test_path, encoding="utf-8", errors="replace").read()

  if not _registered(repo, test_file):
    errors.append(
        f"[NOT BUILT] `{os.path.basename(test_file)}` is in no build list "
        f"({', '.join(BUILD_GLOBS)}); it never compiled"
    )

  for name in names:
    short = name.split(".")[-1]
    if not re.search(rf"\b{re.escape(short)}\b", code):
      errors.append(f"[WRONG FILE] test `{name}` is not defined in `{test_file}`")

  if args.log:
    log = open(args.log, encoding="utf-8", errors="replace").read()
    for name in names:
      if not re.search(rf"\b{re.escape(name.split('.')[-1])}\b", log):
        errors.append(
            f"[NOT RUN] `{name}` is absent from `{args.log}`; a count like `N tests done` is "
            "not proof (rerun verbose, e.g. `dm --src tests --match <Name> --verbose`)"
        )
  else:
    errors.append("[NO LOG] pass --log with the verbose runner output of this test run")

  for name in names:
    offset, body = _test_body(code, name.split(".")[-1])
    for issue in _vacuous_loops(body, offset, code):
      errors.append(f"[MAYBE VACUOUS] {test_file}:{issue}")
    if not args.allow_device_paths:
      for rx, what in ((DEVICE_PATH_RE, "device path literal"),
                       (DEVICE_DEFAULT_RE, "SkFontMgr_New_Android(nullptr) reads /system/etc")):
        for m in rx.finditer(body):
          line = code.count("\n", 0, offset + m.start()) + 1
          errors.append(
              f"[HOST-EMPTY ENV] {test_file}:L{line}: {what}; use resources/ fixtures on host"
          )
    errors += _attestation_errors(repo, test_file, name)
  return errors


def log_warning_errors(args) -> list[str]:
  if not args.log:
    return []
  log = open(args.log, encoding="utf-8", errors="replace").read()
  explained = [re.compile(x) for x in args.explained_warning]
  unexplained = sorted({
      ln.strip() for ln in log.splitlines()
      if LOG_WARNING_RE.search(ln) and not any(x.search(ln) for x in explained)
  })
  if not unexplained:
    return []
  shown = "; ".join(f"`{u[:100]}`" for u in unexplained[:5])
  return [
      f"[UNEXPLAINED LOG WARNINGS] {shown}. Root-cause them before filtering; pass "
      "--explained-warning REGEX only once the cause is known and harmless"
  ]


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
  ap.add_argument("--repo", default=".")
  ap.add_argument("--diff", metavar="REV", help="check all tests added or touched since REV")
  ap.add_argument("--test-file", help="path relative to --repo")
  ap.add_argument("--test-name", action="append", default=[])
  ap.add_argument("--log", help="verbose test runner output")
  ap.add_argument("--explained-warning", action="append", default=[])
  ap.add_argument("--allow-device-paths", action="store_true")
  args = ap.parse_args()
  repo = os.path.abspath(args.repo)

  targets: dict[str, list[str]] = {}
  if args.diff:
    targets = discover_changed_tests(repo, args.diff)
  if args.test_file:
    targets.setdefault(args.test_file, []).extend(args.test_name)
  if not args.diff and not any(targets.values()):
    ap.error("no tests to check: pass --diff REV or --test-file with --test-name")
  if not any(targets.values()):
    print(f"[LIVENESS OK] no test definitions added or touched since {args.diff}")
    return 0

  errors = log_warning_errors(args)
  for test_file, names in sorted(targets.items()):
    print(f"[checking] {test_file}: {', '.join(names)}")
    errors += check(repo, test_file, sorted(set(names)), args)
  for e in errors:
    print(e)
  if errors:
    return 1
  print("[LIVENESS OK] built, run by name, non-vacuous, mutation-attested")
  return 0


if __name__ == "__main__":
  sys.exit(main())
