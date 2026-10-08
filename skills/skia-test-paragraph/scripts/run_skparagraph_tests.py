#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
PARSER_SCRIPT = SCRIPT_DIR / "parse_dm_logs.py"

SOURCE_HASH_DIRS = [
    "modules/skparagraph",
    "modules/skshaper",
    "modules/skunicode",
    "src",
    "tests",
]


def log(msg: str, driver_log: Optional[pathlib.Path] = None) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"{ts} {msg}"
    print(line, flush=True)
    if driver_log:
        with open(driver_log, "a") as f:
            f.write(line + "\n")


def file_sha256_short(path: pathlib.Path | str) -> Optional[str]:
    p = pathlib.Path(path)
    if not p.exists() or not p.is_file():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()[:12]


def compute_source_tree_sha256(repo: pathlib.Path) -> tuple[str, int]:
    h = hashlib.sha256()
    file_count = 0
    for subdir in SOURCE_HASH_DIRS:
        root = repo / subdir
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix in (".cpp", ".h", ".mm", ".m", ".gn", ".gni", ".inc"):
                rel = str(p.relative_to(repo))
                h.update(rel.encode("utf-8") + b"\0")
                h.update(p.read_bytes())
                h.update(b"\0")
                file_count += 1
    return h.hexdigest()[:12], file_count


def compute_git_metadata(repo: pathlib.Path, ref: str) -> dict[str, Any]:
    commit_target = "HEAD" if ref == "WORKTREE" else ref
    git_sha = (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short=12", commit_target],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    git_subject = (
        subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%s", commit_target],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    dirty_files: list[str] = []
    if ref == "WORKTREE":
        diff_names = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        dirty_files = [ln.strip() for ln in diff_names if ln.strip()]

    return {
        "ref": ref,
        "git_sha": git_sha,
        "git_subject": git_subject,
        "dirty_files": dirty_files,
    }


def parse_ninja_actions(ninja_log_path: pathlib.Path) -> str:
    if not ninja_log_path.exists():
        return "—"
    text = ninja_log_path.read_text(errors="replace")
    if "no work to do" in text:
        return "0/0 (no-op)"
    steps = re.findall(r"\[(\d+)/(\d+)\]", text)
    if steps:
        last_done, total = steps[-1]
        return f"{last_done}/{total}"
    return "ran"


def checkout_ref(repo: pathlib.Path, ref: str, worktree_patch: Optional[pathlib.Path]) -> None:
    # Reset dirty state in monitored source subtrees before switching refs
    for subdir in SOURCE_HASH_DIRS:
        if (repo / subdir).exists():
            subprocess.run(
                ["git", "-C", str(repo), "checkout", "--", subdir],
                capture_output=True,
                check=False,
            )

    if ref == "WORKTREE":
        if worktree_patch and worktree_patch.exists() and worktree_patch.stat().st_size > 0:
            subprocess.run(
                ["git", "-C", str(repo), "apply", str(worktree_patch)],
                check=True,
            )
    else:
        subprocess.run(
            ["git", "-C", str(repo), "checkout", ref],
            check=True,
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Run and attest SkParagraph tests across refs.")
    ap.add_argument(
        "targets",
        nargs="*",
        default=["current=WORKTREE"],
        help="List of label=ref pairs (default: current=WORKTREE)",
    )
    ap.add_argument(
        "--repo",
        default="/Users/jlavrova/Sources/skia",
        help="Path to Skia repository root",
    )
    ap.add_argument(
        "--out",
        default="/tmp/skia_paragraph_tests",
        help="Output directory for logs and compare.md",
    )
    ap.add_argument(
        "--build-dir",
        default="out/Debug",
        help="Build directory relative to repo (default: out/Debug)",
    )
    ap.add_argument(
        "--match",
        default="SkParagraph",
        help="Test match filter passed to dm (default: SkParagraph)",
    )
    ap.add_argument(
        "--config",
        default=None,
        help="Optional config passed to dm (e.g. 8888)",
    )
    ap.add_argument(
        "--src",
        default="tests",
        help="Source passed to dm (default: tests)",
    )
    ap.add_argument(
        "--nogpu",
        action="store_true",
        help="Pass --nogpu to dm",
    )
    ap.add_argument(
        "--extra-dm-flags",
        nargs="*",
        default=[],
        help="Extra command-line flags forwarded to dm",
    )
    ap.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip ninja compilation step",
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Timeout in seconds for dm run (default: 600)",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="Skip ref if attestation and summary JSON already exist in output dir",
    )
    args = ap.parse_args()

    repo = pathlib.Path(args.repo).resolve()
    out_dir = pathlib.Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    driver_log = out_dir / "driver.log"

    build_dir = (repo / args.build_dir).resolve()
    dm_bin = build_dir / "dm"
    gn_args_file = build_dir / "args.gn"

    # Save initial working tree diff for restoration
    worktree_patch = out_dir / "worktree.patch"
    diff_res = subprocess.run(
        ["git", "-C", str(repo), "diff", "HEAD"],
        capture_output=True,
        check=True,
    )
    worktree_patch.write_bytes(diff_res.stdout)

    initial_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    def cleanup() -> None:
        log("=== Cleaning up working tree state ===", driver_log)
        try:
            checkout_ref(repo, "WORKTREE", worktree_patch)
        except Exception as e:
            log(f"WARNING: restoring worktree failed: {e}", driver_log)

    compare_args: list[str] = []

    try:
        for pair in args.targets:
            if "=" in pair:
                label, ref = pair.split("=", 1)
            else:
                label, ref = pair, pair

            ref_dir = out_dir / label
            ref_dir.mkdir(parents=True, exist_ok=True)
            att_path = ref_dir / "attestation.json"
            dm_sum_json = ref_dir / "dm_summary.json"

            if args.resume and att_path.exists() and dm_sum_json.exists():
                log(f"[{label}] Skipping (--resume: {att_path.name} exists)", driver_log)
                compare_args.append(f"{label}={ref_dir}")
                continue

            log(f"=== [{label}] Switching to {ref} ===", driver_log)
            checkout_ref(repo, ref, worktree_patch)

            # 1. Attestation: git metadata & disk source tree hash before build
            attestation: dict[str, Any] = {
                "label": label,
                "ref": ref,
            }
            attestation.update(compute_git_metadata(repo, ref))
            source_tree_sha, source_files_count = compute_source_tree_sha256(repo)
            attestation["source_tree_sha256"] = source_tree_sha
            attestation["source_files_count"] = source_files_count
            attestation["build_dir"] = str(build_dir)
            attestation["gn_args_sha256"] = file_sha256_short(gn_args_file)

            # 2. Compilation with ninja
            ninja_log = ref_dir / "ninja.log"
            ninja_rc = 0
            ninja_dt = 0
            if not args.skip_build:
                log(f"[{label}] Compiling dm with ninja in {build_dir.name}...", driver_log)
                t_ninja0 = time.time()
                with open(ninja_log, "w") as lf:
                    res = subprocess.run(
                        ["ninja", "-C", str(build_dir), "dm"],
                        cwd=str(repo),
                        stdout=lf,
                        stderr=subprocess.STDOUT,
                    )
                    ninja_rc = res.returncode
                ninja_dt = int(time.time() - t_ninja0)
                log(f"[{label}] ninja exit={ninja_rc} ({ninja_dt}s)", driver_log)

            attestation["ninja_rc"] = ninja_rc
            attestation["ninja_actions"] = parse_ninja_actions(ninja_log)
            attestation["ninja_duration_s"] = ninja_dt

            # 3. Test binary artifact verification
            attestation["binary_path"] = str(dm_bin)
            attestation["binary_sha256"] = file_sha256_short(dm_bin)
            if dm_bin.exists():
                mtime_ts = dm_bin.stat().st_mtime
                attestation["binary_mtime"] = datetime.datetime.fromtimestamp(
                    mtime_ts, datetime.timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                attestation["binary_mtime"] = None

            # 4. Construct test command line
            test_cmd = [str(dm_bin), "--src", args.src, "--match", args.match]
            if args.config:
                test_cmd.extend(["--config", args.config])
            if args.nogpu:
                test_cmd.append("--nogpu")
            test_cmd.extend(args.extra_dm_flags)
            attestation["test_cmd"] = test_cmd

            # 5. Execute dm
            dm_log = ref_dir / "dm.log"
            log(f"[{label}] Running: {' '.join(test_cmd)}", driver_log)
            t_test0 = time.time()
            test_rc = 0
            timed_out = False
            with open(dm_log, "w") as lf:
                try:
                    res = subprocess.run(
                        test_cmd,
                        cwd=str(repo),
                        stdout=lf,
                        stderr=subprocess.STDOUT,
                        timeout=args.timeout,
                    )
                    test_rc = res.returncode
                except subprocess.TimeoutExpired:
                    test_rc = 124
                    timed_out = True

            test_dt = int(time.time() - t_test0)
            log(f"[{label}] dm finished exit={test_rc} ({test_dt}s)", driver_log)

            attestation["test_rc"] = test_rc
            attestation["test_duration_s"] = test_dt
            attestation["timed_out"] = timed_out

            sig_name: Optional[str] = None
            if test_rc < 0 or test_rc >= 128:
                signum = abs(test_rc) if test_rc < 0 else test_rc - 128
                sig_name = f"SIG{signal.Signals(signum).name}" if signum in signal.Signals._value2member_map_ else f"SIG_{signum}"
            attestation["signal"] = sig_name
            attestation["timestamp_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            att_path.write_text(json.dumps(attestation, indent=2))

            # 6. Parse log and write summary
            dm_sum_txt = ref_dir / "dm_summary.txt"
            with open(dm_sum_txt, "w") as sf:
                subprocess.run(
                    [
                        sys.executable,
                        str(PARSER_SCRIPT),
                        "summarize",
                        str(dm_log),
                        "--json",
                        str(dm_sum_json),
                    ],
                    stdout=sf,
                    stderr=subprocess.STDOUT,
                    check=True,
                )

            compare_args.append(f"{label}={ref_dir}")

        compare_md = out_dir / "compare.md"
        with open(compare_md, "w") as cf:
            subprocess.run(
                [sys.executable, str(PARSER_SCRIPT), "compare", *compare_args],
                stdout=cf,
                stderr=subprocess.STDOUT,
                check=True,
            )
        log(f"=== Report written to {compare_md} ===", driver_log)
        print("\n" + compare_md.read_text())

    finally:
        cleanup()

    return 0


if __name__ == "__main__":
    sys.exit(main())
