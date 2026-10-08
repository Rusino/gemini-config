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
PARSER_SCRIPT = SCRIPT_DIR / "parse_flutter_paragraph_tests.py"

BATCH_1_FILES = [
    "packages/flutter/test/rendering/paragraph_test.dart",
    "packages/flutter/test/rendering/paragraph_intrinsics_test.dart",
    "packages/flutter/test/painting/text_painter_test.dart",
    "packages/flutter/test/painting/text_painter_rtl_test.dart",
    "packages/flutter/test/painting/text_span_test.dart",
    "packages/flutter/test/painting/text_style_test.dart",
    "packages/flutter/test/widgets/rich_text_test.dart",
    "packages/flutter/test/widgets/text_test.dart",
]

BATCH_2_FILES = [
    "packages/flutter/test/widgets/editable_text_test.dart",
    "packages/flutter/test/widgets/editable_text_styles_test.dart",
    "packages/flutter/test/material/selectable_text_test.dart",
    "packages/flutter/test/widgets/text_selection_test.dart",
]

BATCH_3_FILES = [
    "packages/flutter/test/material/text_field_test.dart",
    "packages/flutter/test/cupertino/text_field_test.dart",
    "packages/flutter/test/services/text_boundary_test.dart",
    "packages/flutter/test/services/text_editing_delta_test.dart",
]

SKIA_MONITORED_DIRS = [
    "modules/skparagraph",
    "modules/skshaper",
    "modules/skunicode",
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


def compute_skia_source_tree_sha256(skia_repo: pathlib.Path) -> tuple[str, int]:
    h = hashlib.sha256()
    file_count = 0
    for subdir in SKIA_MONITORED_DIRS:
        root = skia_repo / subdir
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix in (".cpp", ".h", ".mm", ".m", ".gn", ".gni", ".inc"):
                rel = str(p.relative_to(skia_repo))
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


def checkout_skia_ref(skia_repo: pathlib.Path, ref: str, worktree_patch: Optional[pathlib.Path]) -> None:
    for subdir in SKIA_MONITORED_DIRS:
        if (skia_repo / subdir).exists():
            subprocess.run(
                ["git", "-C", str(skia_repo), "checkout", "--", subdir],
                capture_output=True,
                check=False,
            )

    if ref == "WORKTREE":
        if worktree_patch and worktree_patch.exists() and worktree_patch.stat().st_size > 0:
            subprocess.run(
                ["git", "-C", str(skia_repo), "apply", str(worktree_patch)],
                check=True,
            )
    else:
        subprocess.run(
            ["git", "-C", str(skia_repo), "checkout", ref],
            check=True,
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Build and run Flutter engine text tests with Run Attestation.")
    ap.add_argument(
        "targets",
        nargs="*",
        default=["current=WORKTREE"],
        help="List of label=ref pairs for Skia submodule (default: current=WORKTREE)",
    )
    ap.add_argument(
        "--repo",
        default="/Users/jlavrova/Sources/flutter",
        help="Path to Flutter framework repository root",
    )
    ap.add_argument(
        "--engine-src",
        default=None,
        help="Path to Flutter engine src (defaults to <repo>/engine/src)",
    )
    ap.add_argument(
        "--build-dir",
        default="out/host_debug_unopt_arm64",
        help="Engine build output directory relative to engine src",
    )
    ap.add_argument(
        "--out",
        default="/tmp/flutter_paragraph_tests",
        help="Output directory for test logs and report",
    )
    ap.add_argument(
        "--batches",
        default="all",
        help="Batches to run: 'all', or comma-separated numbers '1,2,3' (default: all)",
    )
    ap.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip ninja compilation step",
    )
    ap.add_argument(
        "--skip-cpp",
        action="store_true",
        help="Skip C++ smoke tests (txt_unittests, ui_unittests)",
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=1200,
        help="Timeout in seconds per test batch (default: 1200)",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="Skip ref if attestation and summary JSON already exist",
    )
    args = ap.parse_args()

    repo = pathlib.Path(args.repo).resolve()
    engine_src = pathlib.Path(args.engine_src or (repo / "engine/src")).resolve()
    skia_repo = (engine_src / "flutter/third_party/skia").resolve()
    build_dir = (engine_src / args.build_dir).resolve()
    flutter_tester_bin = build_dir / "flutter_tester"
    gn_args_file = build_dir / "args.gn"

    out_dir = pathlib.Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    driver_log = out_dir / "driver.log"

    worktree_patch = out_dir / "worktree_skia.patch"
    diff_res = subprocess.run(
        ["git", "-C", str(skia_repo), "diff", "HEAD"],
        capture_output=True,
        check=True,
    )
    worktree_patch.write_bytes(diff_res.stdout)

    def cleanup() -> None:
        log("=== Cleaning up Skia submodule state ===", driver_log)
        try:
            checkout_skia_ref(skia_repo, "WORKTREE", worktree_patch)
        except Exception as e:
            log(f"WARNING: restoring Skia submodule failed: {e}", driver_log)

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
            sum_path = ref_dir / "summary.json"

            if args.resume and att_path.exists() and sum_path.exists():
                log(f"[{label}] Skipping (--resume: {att_path.name} exists)", driver_log)
                compare_args.append(f"{label}={ref_dir}")
                continue

            log(f"=== [{label}] Switching Skia submodule to {ref} ===", driver_log)
            checkout_skia_ref(skia_repo, ref, worktree_patch)

            attestation: dict[str, Any] = {
                "label": label,
                "ref": ref,
            }
            attestation.update(compute_git_metadata(skia_repo, ref))
            source_tree_sha, source_count = compute_skia_source_tree_sha256(skia_repo)
            attestation["source_tree_sha256"] = source_tree_sha
            attestation["source_files_count"] = source_count
            attestation["build_dir"] = str(build_dir)
            attestation["gn_args_sha256"] = file_sha256_short(gn_args_file)
            attestation["batches_run"] = args.batches

            # Compilation with ninja
            ninja_log = ref_dir / "ninja.log"
            ninja_rc = 0
            ninja_dt = 0
            if not args.skip_build:
                log(f"[{label}] Compiling engine targets with ninja...", driver_log)
                t_ninja0 = time.time()
                with open(ninja_log, "w") as lf:
                    res = subprocess.run(
                        [
                            "ninja",
                            "-C",
                            str(build_dir),
                            "flutter_tester",
                            "sky_engine",
                            "txt_unittests",
                            "ui_unittests",
                        ],
                        cwd=str(engine_src),
                        stdout=lf,
                        stderr=subprocess.STDOUT,
                    )
                    ninja_rc = res.returncode
                ninja_dt = int(time.time() - t_ninja0)
                log(f"[{label}] ninja exit={ninja_rc} ({ninja_dt}s)", driver_log)

            attestation["ninja_rc"] = ninja_rc
            attestation["ninja_actions"] = parse_ninja_actions(ninja_log)
            attestation["ninja_duration_s"] = ninja_dt

            # Verify flutter_tester binary artifact
            attestation["binary_path"] = str(flutter_tester_bin)
            attestation["binary_sha256"] = file_sha256_short(flutter_tester_bin)
            if flutter_tester_bin.exists():
                mtime_ts = flutter_tester_bin.stat().st_mtime
                attestation["binary_mtime"] = datetime.datetime.fromtimestamp(
                    mtime_ts, datetime.timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                attestation["binary_mtime"] = None

            # Execution state
            summary: dict[str, Any] = {
                "label": label,
                "ref": ref,
                "cpp_passed": 0,
                "cpp_failed": 0,
                "batch1_passed": 0,
                "batch1_failed": 0,
                "batch2_passed": 0,
                "batch2_failed": 0,
                "batch3_passed": 0,
                "batch3_failed": 0,
                "failures": [],
                "finished_cleanly": True,
                "abort_detected": False,
                "crash_reason": None,
            }
            worst_test_rc = 0
            test_commands: list[str] = []

            # 1. C++ Smoke tests
            if not args.skip_cpp:
                for cpp_target in ("txt_unittests", "ui_unittests"):
                    cpp_bin = build_dir / cpp_target
                    cpp_log = ref_dir / f"{cpp_target}.log"
                    cmd = [str(cpp_bin)]
                    test_commands.append(" ".join(cmd))
                    log(f"[{label}] Running C++ {cpp_target}...", driver_log)
                    with open(cpp_log, "w") as lf:
                        res = subprocess.run(
                            cmd,
                            cwd=str(engine_src),
                            stdout=lf,
                            stderr=subprocess.STDOUT,
                            timeout=300,
                        )
                        if res.returncode != 0 and worst_test_rc == 0:
                            worst_test_rc = res.returncode
                    from parse_flutter_paragraph_tests import summarize_gtest_log
                    cpp_res = summarize_gtest_log(cpp_log)
                    summary["cpp_passed"] += cpp_res["passed"]
                    summary["cpp_failed"] += cpp_res["failed"]
                    summary["failures"].extend(cpp_res["failures"])
                    if not cpp_res["finished_cleanly"]:
                        summary["finished_cleanly"] = False
                    if cpp_res["abort_detected"]:
                        summary["abort_detected"] = True
                        summary["crash_reason"] = cpp_res["crash_reason"]

            # 2. Dart framework test batches
            from parse_flutter_paragraph_tests import summarize_dart_log

            batch_configs = [
                ("batch1", BATCH_1_FILES, args.batches in ("all", "1") or "1" in args.batches.split(",")),
                ("batch2", BATCH_2_FILES, args.batches in ("all", "2") or "2" in args.batches.split(",")),
                ("batch3", BATCH_3_FILES, args.batches in ("all", "3") or "3" in args.batches.split(",")),
            ]

            flutter_bin = repo / "bin/flutter"
            engine_flags = [
                f"--local-engine={build_dir.name}",
                f"--local-engine-host={build_dir.name}",
                f"--local-engine-src-path={engine_src}",
            ]

            for b_name, b_files, should_run in batch_configs:
                if not should_run:
                    continue
                b_log = ref_dir / f"{b_name}.log"
                cmd = [str(flutter_bin), "test", *engine_flags, *b_files]
                test_commands.append(" ".join(cmd))
                log(f"[{label}] Running Dart {b_name} ({len(b_files)} files)...", driver_log)
                with open(b_log, "w") as lf:
                    res = subprocess.run(
                        cmd,
                        cwd=str(repo),
                        stdout=lf,
                        stderr=subprocess.STDOUT,
                        timeout=args.timeout,
                    )
                    if res.returncode != 0 and worst_test_rc == 0:
                        worst_test_rc = res.returncode
                b_res = summarize_dart_log(b_log)
                summary[f"{b_name}_passed"] = b_res["passed"]
                summary[f"{b_name}_failed"] = b_res["failed"]
                summary["failures"].extend(b_res["failures"])
                if not b_res["finished_cleanly"]:
                    summary["finished_cleanly"] = False
                if b_res["abort_detected"]:
                    summary["abort_detected"] = True
                    summary["crash_reason"] = b_res["crash_reason"]

            summary["total_passed"] = (
                summary["cpp_passed"]
                + summary["batch1_passed"]
                + summary["batch2_passed"]
                + summary["batch3_passed"]
            )
            summary["total_failed"] = (
                summary["cpp_failed"]
                + summary["batch1_failed"]
                + summary["batch2_failed"]
                + summary["batch3_failed"]
            )

            attestation["test_cmd"] = test_commands
            attestation["test_rc"] = worst_test_rc
            sig_name: Optional[str] = None
            if worst_test_rc < 0 or worst_test_rc >= 128:
                signum = abs(worst_test_rc) if worst_test_rc < 0 else worst_test_rc - 128
                sig_name = f"SIG{signal.Signals(signum).name}" if signum in signal.Signals._value2member_map_ else f"SIG_{signum}"
            attestation["signal"] = sig_name
            attestation["timestamp_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            att_path.write_text(json.dumps(attestation, indent=2))
            sum_path.write_text(json.dumps(summary, indent=2))

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
