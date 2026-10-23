#!/usr/bin/env python3
"""Mutation gate: a test counts as live only if it passes as-is and fails with one mutation.

Usage:
  mutation_check.py --repo . --file src/ports/SkFontMgr_android.cpp \
      --find 'entry->fStyle = apply_xml_style(fontFile, scannedStyle);' \
      --replace 'entry->fStyle = SkFontStyle::BoldItalic();' \
      --build 'third_party/ninja/ninja -C out/Static dm' \
      --test 'out/Static/dm --src tests --match FontMgrAndroid_LazyParity_FreeType' \
      --test-file tests/FontMgrAndroidParserTest.cpp \
      --test-name FontMgrAndroid_LazyParity_FreeType

Sequence: build+test clean (must pass) -> mutate, build (must compile), test (must fail) ->
restore (always, also on error/Ctrl-C/SIGTERM), check bytes and `git diff -- <file>` match the
pre-run state -> rebuild+test (must pass). Choosing the mutation is the agent's judgement; the
execution is mechanical and is recorded per test name in an attestation under
`$(git rev-parse --git-path jetski/mutation_attestations)`, which verify_test_liveness.py
requires. Exit 0 only for PASS
"""

import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile


class GateError(Exception):
  pass


def attestation_dir(repo: str) -> str:
  rel = subprocess.run(
      ["git", "-C", repo, "rev-parse", "--git-path", "jetski/mutation_attestations"],
      capture_output=True, text=True, check=True,
  ).stdout.strip()
  return rel if os.path.isabs(rel) else os.path.join(repo, rel)


def attestation_path(repo: str, test_name: str) -> str:
  return os.path.join(attestation_dir(repo), re.sub(r"[^\w.\-]", "_", test_name) + ".json")


def sha256_file(path: str) -> str:
  with open(path, "rb") as f:
    return hashlib.sha256(f.read()).hexdigest()


def _sh(cmd: str, repo: str, timeout: int) -> tuple[int, str]:
  try:
    res = subprocess.run(
        cmd, shell=True, cwd=repo, capture_output=True, text=True, timeout=timeout, check=False
    )
  except subprocess.TimeoutExpired:
    return 124, f"timeout after {timeout}s"
  return res.returncode, (res.stdout + res.stderr)


def _tail(out: str, n: int = 8) -> str:
  lines = [ln for ln in out.strip().splitlines() if ln.strip()]
  return "\n".join("    " + ln for ln in lines[-n:])


def _git_diff(repo: str, rel: str) -> str:
  return subprocess.run(
      ["git", "-C", repo, "diff", "--", rel], capture_output=True, text=True, check=True
  ).stdout


def _build_and_test(args, phase: str) -> tuple[int, str]:
  rc, out = _sh(args.build, args.repo, args.timeout)
  if rc != 0:
    raise GateError(f"[{phase}] build failed (rc={rc}):\n{_tail(out)}")
  rc, out = _sh(args.test, args.repo, args.timeout)
  print(f"[{phase}] test rc={rc}")
  if args.verbose:
    print(_tail(out, 20))
  return rc, out


def _raise_on_sigterm(signum, frame):
  raise KeyboardInterrupt(f"signal {signum}")


def _write_attestations(args, record: dict) -> None:
  os.makedirs(attestation_dir(args.repo), exist_ok=True)
  for name in args.test_name:
    path = attestation_path(args.repo, name)
    with open(path, "w", encoding="utf-8") as f:
      json.dump(dict(record, test_name=name), f, indent=2)
    print(f"[attestation] {path}")


def run(args) -> int:
  args.repo = os.path.abspath(args.repo)
  path = os.path.join(args.repo, args.file)
  test_path = os.path.join(args.repo, args.test_file)
  original = open(path, "rb").read()
  diff_before = _git_diff(args.repo, args.file)
  find, replace = args.find.encode(), args.replace.encode()
  if original.count(find) != 1:
    raise GateError(f"--find must occur exactly once in {args.file}, found {original.count(find)}")
  if find == replace:
    raise GateError("--replace equals --find; that is not a mutation")
  mutated = original.replace(find, replace, 1)
  mutation_diff = "".join(difflib.unified_diff(
      original.decode(errors="replace").splitlines(True),
      mutated.decode(errors="replace").splitlines(True),
      f"a/{args.file}", f"b/{args.file}", n=1,
  ))

  # Copy outside the repo survives SIGKILL so the source can be recovered by hand
  fd, backup = tempfile.mkstemp(prefix="mutation_backup_", suffix=os.path.basename(path))
  os.close(fd)
  shutil.copy2(path, backup)
  print(f"[backup] {backup}")

  clean_rc, _ = _build_and_test(args, "clean")
  if clean_rc != 0:
    raise GateError("[clean] test fails without the mutation; fix it before the mutation gate")

  prev_term = signal.signal(signal.SIGTERM, _raise_on_sigterm)
  mutated_rc, mutated_out = None, ""
  try:
    with open(path, "wb") as f:
      f.write(mutated)
    rc, out = _sh(args.build, args.repo, args.timeout)
    if rc != 0:
      raise GateError(f"[mutated] mutation does not compile; pick a compiling one:\n{_tail(out)}")
    mutated_rc, mutated_out = _sh(args.test, args.repo, args.timeout)
    print(f"[mutated] test rc={mutated_rc}")
    print(_tail(mutated_out, 12 if mutated_rc else 4))
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

  restored_rc, restored_out = _build_and_test(args, "restored")
  passed = mutated_rc != 0 and restored_rc == 0
  _write_attestations(args, {
      "result": "PASS" if passed else "FAIL",
      "test_file": args.test_file,
      "test_file_sha256": sha256_file(test_path),
      "prod_file": args.file,
      "prod_sha256_before": hashlib.sha256(original).hexdigest(),
      "prod_sha256_after": sha256_file(path),
      "mutation_diff": mutation_diff,
      "build_cmd": args.build,
      "test_cmd": args.test,
      "mutated_rc": mutated_rc,
      "restored_rc": restored_rc,
      "mutated_log_tail": _tail(mutated_out, 30),
      "restored_log_tail": _tail(restored_out, 10),
      "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
  })
  if restored_rc != 0:
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
  ap.add_argument("--test-file", required=True, help="file defining the test, relative to --repo")
  ap.add_argument("--test-name", action="append", required=True)
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
