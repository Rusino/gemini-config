#!/usr/bin/env python3
"""Deterministic Phase 1 & commit-message razor for amnesia-precommit.

Runs audit_comments.py --diff HEAD, ruthless_audit.py --diff HEAD, and optionally
audit_comments.py --commit-msg on HEAD, printing a single summary verdict.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
AUDIT_COMMENTS = os.path.join(
    CONFIG_DIR, "skills", "code-comments", "scripts", "audit_comments.py"
)
RUTHLESS_AUDIT = os.path.join(
    CONFIG_DIR, "skills", "ruthless-code-audit", "scripts", "ruthless_audit.py"
)


def main(argv: list[str] | None = None) -> int:
  ap = argparse.ArgumentParser(description=__doc__)
  ap.add_argument("--repo", default=".", help="Repository path (default: .)")
  ap.add_argument("--diff", default="HEAD", help="Diff base (default: HEAD)")
  ap.add_argument(
      "--check-commit-msg",
      action="store_true",
      help="Also audit git log -1 --format=%%B HEAD via audit_comments.py --commit-msg",
  )
  args = ap.parse_args(argv)
  repo = os.path.abspath(args.repo)

  # Run both Phase 1 deterministic auditors and collect exit codes
  c1 = subprocess.run(
      [
          sys.executable,
          AUDIT_COMMENTS,
          "--diff",
          args.diff,
          "--repo",
          repo,
          "--check-cl-hygiene",
      ],
      cwd=repo,
      check=False,
  )
  c2 = subprocess.run(
      [sys.executable, RUTHLESS_AUDIT, repo, "--repo", repo, "--diff", args.diff],
      cwd=repo,
      check=False,
  )
  c3_code = 0
  if args.check_commit_msg:
    msg = subprocess.run(
        ["git", "-C", repo, "log", "-1", "--format=%B", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
      tf.write(msg)
      tf_path = tf.name
    try:
      c3_code = subprocess.run(
          [sys.executable, AUDIT_COMMENTS, "--commit-msg", tf_path],
          cwd=repo,
          check=False,
      ).returncode
    finally:
      if os.path.exists(tf_path):
        os.unlink(tf_path)

  ok = c1.returncode == 0 and c2.returncode == 0 and c3_code == 0
  verdict = "PASS" if ok else "FAIL"
  print(
      f"[PRECOMMIT-RAZOR] Verdict: {verdict}"
      f" (audit_comments={c1.returncode}, ruthless_audit={c2.returncode},"
      f" commit_msg={c3_code})"
  )
  return 0 if ok else 1


if __name__ == "__main__":
  sys.exit(main())
