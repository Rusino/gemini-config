#!/usr/bin/env python3
"""Mutation gate: a test counts as live only if it passes as-is and fails with one mutation.

Usage:
  mutation_check.py --repo . --file src/ports/SkFontMgr_android.cpp \
      --find 'entry->fStyle = apply_xml_style(fontFile, scannedStyle);' \
      --replace 'entry->fStyle = SkFontStyle::BoldItalic();' \
      --build 'third_party/ninja/ninja -C out/Static dm' \
      --test 'out/Static/dm --src tests --match FontMgrAndroid_LazyParity_FreeType'

Sequence: build+test clean (must pass) -> mutate, build (must compile), test (must fail) ->
restore (always, also on error/Ctrl-C/SIGTERM), check bytes and `git diff -- <file>` match the
pre-run state -> rebuild+test (must pass). Exit 0 only for PASS
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile


class GateError(Exception):
  pass


def _sh(cmd: str, repo: str, timeout: int) -> tuple[int, str]:
  try:
    res = subprocess.run(
        cmd, shell=True, cwd=repo, capture_output=True, text=True, timeout=timeout, check=False
    )
  except subprocess.TimeoutExpired:
    return 124, f"timeout after {timeout}s"
  return res.returncode, (res.stdout + res.stderr)


def _tail(out: str, n: int = 8) -> str:
  return "\n".join("    " + ln for ln in out.strip().splitlines()[-n:])


def _git_diff(repo: str, rel: str) -> str:
  return subprocess.run(
      ["git", "-C", repo, "diff", "--", rel], capture_output=True, text=True, check=True
  ).stdout


def _build_and_test(args, phase: str) -> int:
  rc, out = _sh(args.build, args.repo, args.timeout)
  if rc != 0:
    raise GateError(f"[{phase}] build failed (rc={rc}):\n{_tail(out)}")
  rc, out = _sh(args.test, args.repo, args.timeout)
  print(f"[{phase}] test rc={rc}")
  if args.verbose:
    print(_tail(out, 20))
  return rc


def _raise_on_sigterm(signum, frame):
  raise KeyboardInterrupt(f"signal {signum}")


def run(args) -> int:
  args.repo = os.path.abspath(args.repo)
  path = os.path.join(args.repo, args.file)
  original = open(path, "rb").read()
  diff_before = _git_diff(args.repo, args.file)
  find, replace = args.find.encode(), args.replace.encode()
  if original.count(find) != 1:
    raise GateError(f"--find must occur exactly once in {args.file}, found {original.count(find)}")
  if find == replace:
    raise GateError("--replace equals --find; that is not a mutation")

  # Copy outside the repo survives SIGKILL so the source can be recovered by hand
  fd, backup = tempfile.mkstemp(prefix="mutation_backup_", suffix=os.path.basename(path))
  os.close(fd)
  shutil.copy2(path, backup)
  print(f"[backup] {backup}")

  if _build_and_test(args, "clean") != 0:
    raise GateError("[clean] test fails without the mutation; fix it before the mutation gate")

  prev_term = signal.signal(signal.SIGTERM, _raise_on_sigterm)
  mutated_rc = None
  try:
    with open(path, "wb") as f:
      f.write(original.replace(find, replace, 1))
    rc, out = _sh(args.build, args.repo, args.timeout)
    if rc != 0:
      raise GateError(f"[mutated] mutation does not compile; pick a compiling one:\n{_tail(out)}")
    mutated_rc, out = _sh(args.test, args.repo, args.timeout)
    print(f"[mutated] test rc={mutated_rc}")
    print(_tail(out, 12 if mutated_rc else 4))
  finally:
    shutil.copyfile(backup, path)
    # A restored old mtime predates the mutated object file, so ninja/make would skip the
    # rebuild and keep the mutated binary; a fresh mtime forces it
    os.utime(path, None)
    signal.signal(signal.SIGTERM, prev_term)
    restored_ok = open(path, "rb").read() == original
    diff_ok = _git_diff(args.repo, args.file) == diff_before
    print(f"[restore] bytes identical={restored_ok} git diff unchanged={diff_ok}")
    if not (restored_ok and diff_ok):
      print(f"[restore] FAILED, recover manually from {backup}")
      raise SystemExit(2)
  os.remove(backup)

  if _build_and_test(args, "restored") != 0:
    raise GateError("[restored] test fails after restoring the source; the tree is not clean")
  if mutated_rc == 0:
    print("FAIL: the test passed with the mutation, so it does not check the mutated behavior")
    return 1
  print("PASS: test fails with the mutation and passes without it")
  return 0


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
  ap.add_argument("--repo", default=".")
  ap.add_argument("--file", required=True, help="production file, relative to --repo")
  ap.add_argument("--find", required=True, help="literal text, must occur exactly once")
  ap.add_argument("--replace", required=True)
  ap.add_argument("--build", required=True, help="shell command run in --repo")
  ap.add_argument("--test", required=True, help="shell command running the test by name")
  ap.add_argument("--timeout", type=int, default=1800)
  ap.add_argument("--verbose", action="store_true")
  args = ap.parse_args()
  try:
    return run(args)
  except GateError as e:
    print(f"FAIL: {e}")
    return 1


if __name__ == "__main__":
  sys.exit(main())
