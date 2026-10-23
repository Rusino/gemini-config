#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import random
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
    "packages/flutter/test/material/dropdown_menu_test.dart",
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


def set_gn_unicode_backend(gn_args_file: pathlib.Path, mode: str) -> None:
    if mode == "keep" or not gn_args_file.exists():
        return
    text = gn_args_file.read_text(errors="replace")
    lines = text.splitlines()
    out_lines: list[str] = []
    saw_icu = False
    saw_apple = False
    want_apple = mode == "apple"
    want_icu = mode == "icu"

    for ln in lines:
        if re.match(r"^\s*skia_use_icu\s*=", ln):
            out_lines.append(f"skia_use_icu = {'true' if want_icu else 'false'}")
            saw_icu = True
        elif re.match(r"^\s*skia_use_apple_unicode\s*=", ln):
            if want_apple:
                out_lines.append("skia_use_apple_unicode = true")
            saw_apple = True
        else:
            out_lines.append(ln)

    if not saw_icu:
        out_lines.append(f"skia_use_icu = {'true' if want_icu else 'false'}")
    if want_apple and not saw_apple:
        out_lines.append("skia_use_apple_unicode = true")

    new_text = "\n".join(out_lines) + "\n"
    if new_text != text:
        gn_args_file.write_text(new_text)


def verify_flutter_apple_glue(engine_src: pathlib.Path) -> list[str]:
    missing: list[str] = []
    checks = [
        (
            engine_src / "flutter/skia/modules/skunicode/BUILD.gn",
            "skia_use_apple_unicode",
        ),
        (
            engine_src / "flutter/txt/BUILD.gn",
            "skia_use_apple_unicode",
        ),
        (
            engine_src / "flutter/txt/src/skia/paragraph_builder_skia.cc",
            "SK_UNICODE_APPLE_IMPLEMENTATION",
        ),
    ]
    for path, needle in checks:
        if not path.exists() or needle not in path.read_text(errors="replace"):
            missing.append(str(path))
    return missing


def find_ninja(engine_src: pathlib.Path) -> str:
    candidates = [
        shutil.which("ninja"),
        engine_src / "flutter/third_party/ninja/ninja",
        pathlib.Path.home() / ".gemini/jetski/scratch/depot_tools/ninja",
    ]
    for c in candidates:
        if c and pathlib.Path(c).exists():
            return str(c)
    return "ninja"


def default_flutter_repo() -> str:
    candidates = [
        pathlib.Path.home() / "Sources/flutter",
        pathlib.Path.home() / ".gemini/jetski/scratch/flutter",
        pathlib.Path("/usr/local/google/home/jlavrova/.gemini/jetski/scratch/flutter"),
        pathlib.Path("/Users/jlavrova/Sources/flutter"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return str(candidates[0])


def default_external_skia_repo() -> str:
    candidates = [
        pathlib.Path.home() / "Sources/skia",
        pathlib.Path.home() / ".gemini/jetski/scratch/skia",
        pathlib.Path("/usr/local/google/home/jlavrova/.gemini/jetski/scratch/skia"),
        pathlib.Path("/usr/local/google/home/jlavrova/skia"),
        pathlib.Path("/Users/jlavrova/Sources/skia"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return str(candidates[0])


def resolve_build_dir(engine_src: pathlib.Path, requested: Optional[str]) -> pathlib.Path:
    if requested:
        return (engine_src / requested).resolve()
    if sys.platform == "darwin":
        candidates = ["out/host_debug_unopt_arm64", "out/host_debug_unopt"]
    else:
        candidates = ["out/host_debug_unopt", "out/host_debug_unopt_x64", "out/host_debug_unopt_arm64"]
    for rel in candidates:
        p = engine_src / rel
        if p.exists():
            return p.resolve()
    return (engine_src / candidates[0]).resolve()


def detect_unicode_backend(build_dir: pathlib.Path, requested_mode: str = "keep") -> dict[str, Any]:
    gn_args_file = build_dir / "args.gn"
    gn_apple = False
    gn_icu = True
    if gn_args_file.exists():
        gn_text = gn_args_file.read_text(errors="replace")
        if re.search(r"^\s*skia_use_apple_unicode\s*=\s*true\b", gn_text, re.M):
            gn_apple = True
        if re.search(r"^\s*skia_use_icu\s*=\s*false\b", gn_text, re.M):
            gn_icu = False

    txt_bin = build_dir / "exe.unstripped/txt_unittests"
    if not txt_bin.exists():
        txt_bin = build_dir / "txt_unittests"
    tester_bin = build_dir / "exe.unstripped/flutter_tester"
    if not tester_bin.exists():
        tester_bin = build_dir / "flutter_tester"
    has_apple_sym = False
    has_icu_sym = False
    if txt_bin.exists():
        nm_res = subprocess.run(
            ["nm", str(txt_bin)], capture_output=True, text=True, check=False
        )
        has_apple_sym = "_ZN10SkUnicodes5Apple4MakeEv" in nm_res.stdout
        has_icu_sym = "_ZN10SkUnicodes3ICU4MakeEv" in nm_res.stdout
    elif tester_bin.exists():
        nm_res = subprocess.run(
            ["nm", "-u", str(tester_bin)], capture_output=True, text=True, check=False
        )
        has_apple_sym = "CFStringTokenizerCreate" in nm_res.stdout
        has_icu_sym = bool(re.search(r"\b_?ubrk_open\b|\b_?u_strToUTF8\b", nm_res.stdout))

    if gn_apple and has_apple_sym and not has_icu_sym:
        backend = "Apple (CFString/CoreText)"
    elif not gn_apple and has_icu_sym and not has_apple_sym:
        backend = "ICU"
    else:
        backend = f"MISMATCH(gn_apple={gn_apple},gn_icu={gn_icu},sym_apple={has_apple_sym},sym_icu={has_icu_sym})"

    return {
        "requested_unicode": requested_mode,
        "unicode_backend": backend,
        "gn_skia_use_apple_unicode": gn_apple,
        "gn_skia_use_icu": gn_icu,
        "sym_apple_make": has_apple_sym,
        "sym_icu_make": has_icu_sym,
    }


def checkout_skia_ref(
    skia_repo: pathlib.Path,
    ref: str,
    worktree_patch: Optional[pathlib.Path],
    external_skia_repo: Optional[pathlib.Path] = None,
    base_head: Optional[str] = None,
) -> dict[str, Any]:
    target_head = base_head or "HEAD"
    if base_head:
        subprocess.run(
            ["git", "-C", str(skia_repo), "checkout", "-f", base_head],
            capture_output=True,
            check=False,
        )
    existing_dirs = [d for d in SKIA_MONITORED_DIRS if (skia_repo / d).exists()]
    if existing_dirs:
        subprocess.run(
            ["git", "-C", str(skia_repo), "checkout", target_head, "--", *existing_dirs],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["git", "-C", str(skia_repo), "clean", "-fd", "--", *existing_dirs],
            capture_output=True,
            check=False,
        )

    if ref == "WORKTREE":
        if worktree_patch and worktree_patch.exists() and worktree_patch.stat().st_size > 0:
            subprocess.run(
                ["git", "-C", str(skia_repo), "apply", str(worktree_patch)],
                check=True,
            )
        return compute_git_metadata(skia_repo, ref)

    if ref.startswith("ext:") and external_skia_repo and external_skia_repo.exists():
        ext_ref = ref[len("ext:") :]
        subprocess.run(
            ["git", "-C", str(skia_repo), "fetch", str(external_skia_repo), ext_ref],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(skia_repo), "checkout", "FETCH_HEAD", "--", *SKIA_MONITORED_DIRS],
            check=True,
        )
        meta = compute_git_metadata(external_skia_repo, ext_ref)
        meta["ref"] = ref
        return meta

    subprocess.run(
        ["git", "-C", str(skia_repo), "checkout", ref],
        check=True,
    )
    return compute_git_metadata(skia_repo, ref)


# Collect and deterministically shuffle all framework *_test.dart files for load-balanced sharding
def collect_all_framework_tests(repo: pathlib.Path) -> list[str]:
    test_dir = repo / "packages/flutter/test"
    # Exclude root-level helper files directly under packages/flutter/test
    all_tests = sorted(
        str(p.relative_to(repo))
        for p in test_dir.rglob("*_test.dart")
        if p.parent != test_dir
    )
    rng = random.Random(0)
    rng.shuffle(all_tests)
    return all_tests


# Run all framework test files across parallel flutter test shards with isolated kernel caches
def run_sharded_framework_tests(
    repo: pathlib.Path,
    flutter_bin: pathlib.Path,
    engine_flags: list[str],
    flutter_env: dict[str, str],
    ref_dir: pathlib.Path,
    label: str,
    test_files: list[str],
    num_shards: int,
    concurrency: int,
    timeout: int,
    driver_log: pathlib.Path,
) -> tuple[int, pathlib.Path, list[str]]:
    shards_dir = ref_dir / "flutter_shards"
    if shards_dir.exists():
        shutil.rmtree(shards_dir)
    shards_dir.mkdir(parents=True, exist_ok=True)

    # Warm up unit_test_assets once before spawning parallel shards with --no-test-assets
    t_warm = time.time()
    warmup_log = shards_dir / "warmup.log"
    warmup_cmd = [
        str(flutter_bin),
        "test",
        *engine_flags,
        "packages/flutter/test/rendering/paragraph_intrinsics_test.dart",
    ]
    with open(warmup_log, "w") as wf:
        subprocess.run(
            warmup_cmd,
            cwd=str(repo),
            env=flutter_env,
            stdout=wf,
            stderr=subprocess.STDOUT,
            timeout=300,
        )
    log(f"[{label}] Warmed up unit_test_assets ({int(time.time() - t_warm)}s)", driver_log)

    actual_shards = min(num_shards, len(test_files))
    shards = [test_files[i::actual_shards] for i in range(actual_shards)]
    log(
        f"[{label}] Launching {actual_shards} shards x -j {concurrency} "
        f"({len(test_files)} test files total)...",
        driver_log,
    )

    t0 = time.time()
    procs = []
    cmds: list[str] = []
    for idx, shard_files in enumerate(shards):
        s_json = shards_dir / f"shard_{idx:02d}.json"
        s_log = shards_dir / f"shard_{idx:02d}.log"
        # Distinct --dart-define per shard isolates each shard's kernel path in build/test_cache
        cmd = [
            str(flutter_bin),
            "test",
            "--no-pub",
            "--no-test-assets",
            f"--dart-define=FLUTTER_TEST_SHARD={idx}",
            "-j",
            str(concurrency),
            f"--file-reporter=json:{s_json}",
            *engine_flags,
            *shard_files,
        ]
        cmds.append(" ".join(cmd[:10]) + f" ... ({len(shard_files)} files)")
        lf = open(s_log, "w")
        p = subprocess.Popen(
            cmd,
            cwd=str(repo),
            env=flutter_env,
            stdout=lf,
            stderr=subprocess.STDOUT,
        )
        procs.append((idx, p, lf, time.time()))

    deadline = t0 + timeout
    worst_rc = 0
    remaining = list(procs)
    # Poll active shard processes until completion or overall wall-clock deadline
    while remaining:
        now = time.time()
        if now >= deadline:
            for idx, p, lf, _ in remaining:
                p.kill()
                lf.close()
                log(f"[{label}] Shard {idx:02d} TIMED OUT after {timeout}s", driver_log)
            worst_rc = 124
            break
        still_running = []
        for item in remaining:
            idx, p, lf, st = item
            rc = p.poll()
            if rc is None:
                still_running.append(item)
            else:
                lf.close()
                if rc != 0 and worst_rc == 0:
                    worst_rc = rc
                elapsed = int(time.time() - st)
                log(
                    f"[{label}] Shard {idx:02d}/{actual_shards} finished exit={rc} ({elapsed}s)",
                    driver_log,
                )
        remaining = still_running
        if remaining:
            time.sleep(1.0)

    dt = int(time.time() - t0)
    log(f"[{label}] Full sharded flutter test exit={worst_rc} ({dt}s)", driver_log)
    return worst_rc, shards_dir, cmds


def main() -> int:
    ap = argparse.ArgumentParser(description="Build and run Flutter engine text tests with Run Attestation.")
    ap.add_argument(
        "targets",
        nargs="*",
        default=["current=WORKTREE"],
        help="List of label=ref[@icu|@apple] pairs for Skia submodule (default: current=WORKTREE). Use ext:<ref> to sync modules from --external-skia.",
    )
    ap.add_argument(
        "--unicode",
        choices=["icu", "apple", "keep"],
        default="icu",
        help="Default SkUnicode backend when target does not specify @icu or @apple (default: icu; use 'apple' for opt-in Apple Unicode replacement on macOS, or 'keep' to leave args.gn untouched)",
    )
    ap.add_argument(
        "--external-skia",
        default=default_external_skia_repo(),
        help="Path to standalone Skia repository for ext:<ref> targets (auto-detected across macOS and Jetski Cloud)",
    )
    ap.add_argument(
        "--repo",
        default=default_flutter_repo(),
        help="Path to Flutter framework repository root (auto-detected across macOS and Jetski Cloud)",
    )
    ap.add_argument(
        "--engine-src",
        default=None,
        help="Path to Flutter engine src (defaults to <repo>/engine/src)",
    )
    ap.add_argument(
        "--build-dir",
        default=None,
        help="Engine build output directory relative to engine src (auto-detected: out/host_debug_unopt_arm64 on macOS, out/host_debug_unopt on Linux)",
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
        "--full",
        action="store_true",
        help=(
            "Run all packages/flutter/test/**/*_test.dart files sharded across parallel workers "
            "via local flutter_tester"
        ),
    )
    ap.add_argument(
        "--flutter-shards",
        type=int,
        default=8,
        help="Number of parallel `flutter test` shard processes in `--full` mode (default: 8)",
    )
    ap.add_argument(
        "--flutter-concurrency",
        type=int,
        default=4,
        help=(
            "Number of concurrent flutter_tester processes (`-j`) per shard in `--full` mode "
            "(default: 4)"
        ),
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
        help="Timeout in seconds per test batch or full sharded run (default: 1200)",
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
    ext_skia_repo = pathlib.Path(args.external_skia).resolve() if args.external_skia else None
    build_dir = resolve_build_dir(engine_src, args.build_dir)
    ninja_bin = find_ninja(engine_src)
    flutter_tester_bin = build_dir / "flutter_tester"
    gn_args_file = build_dir / "args.gn"
    orig_gn_args = gn_args_file.read_text(errors="replace") if gn_args_file.exists() else None
    orig_branch_or_sha = subprocess.run(
        ["git", "-C", str(skia_repo), "symbolic-ref", "--short", "-q", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip() or subprocess.run(
        ["git", "-C", str(skia_repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

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

    test_cache_dir = repo / "build/test_cache/build"
    orig_cache_dills = (
        set(test_cache_dir.glob("*.cache.dill*")) if test_cache_dir.exists() else set()
    )

    def cleanup() -> None:
        log("=== Cleaning up Skia submodule and args.gn state ===", driver_log)
        try:
            checkout_skia_ref(skia_repo, "WORKTREE", worktree_patch, ext_skia_repo, orig_branch_or_sha)
        except Exception as e:
            log(f"WARNING: restoring Skia submodule failed: {e}", driver_log)
        try:
            if orig_gn_args is not None and gn_args_file.exists():
                if gn_args_file.read_text(errors="replace") != orig_gn_args:
                    gn_args_file.write_text(orig_gn_args)
        except Exception as e:
            log(f"WARNING: restoring args.gn failed: {e}", driver_log)
        if args.full and test_cache_dir.exists():
            for p in test_cache_dir.glob("*.cache.dill*"):
                if p not in orig_cache_dills:
                    try:
                        p.unlink()
                    except OSError:
                        pass

    compare_args: list[str] = []

    try:
        for pair in args.targets:
            if "=" in pair:
                label, raw_ref = pair.split("=", 1)
            else:
                label, raw_ref = pair, pair

            target_unicode = args.unicode
            ref = raw_ref
            if "@" in raw_ref:
                maybe_ref, maybe_uni = raw_ref.rsplit("@", 1)
                if maybe_uni in ("icu", "apple", "keep"):
                    ref = maybe_ref
                    target_unicode = maybe_uni

            if target_unicode == "apple" and sys.platform != "darwin":
                raise RuntimeError(
                    f"[{label}] Apple Unicode backend (--unicode apple / @apple) is only supported on macOS."
                )

            ref_dir = out_dir / label
            ref_dir.mkdir(parents=True, exist_ok=True)
            att_path = ref_dir / "attestation.json"
            sum_path = ref_dir / "summary.json"

            if args.resume and att_path.exists() and sum_path.exists():
                log(f"[{label}] Skipping (--resume: {att_path.name} exists)", driver_log)
                compare_args.append(f"{label}={ref_dir}")
                continue

            log(
                f"=== [{label}] Switching Skia submodule to {ref} (unicode={target_unicode}) ===",
                driver_log,
            )
            git_meta = checkout_skia_ref(
                skia_repo, ref, worktree_patch, ext_skia_repo, orig_branch_or_sha
            )
            set_gn_unicode_backend(gn_args_file, target_unicode)

            missing_glue: list[str] = []
            if target_unicode == "apple":
                missing_glue = verify_flutter_apple_glue(engine_src)
                if missing_glue:
                    log(
                        f"WARNING: [{label}] Missing Apple Unicode glue in Flutter engine: {missing_glue}",
                        driver_log,
                    )

            attestation: dict[str, Any] = {
                "label": label,
                "ref": raw_ref,
                "missing_apple_glue": missing_glue,
            }
            attestation.update(git_meta)
            attestation["ref"] = raw_ref
            source_tree_sha, source_count = compute_skia_source_tree_sha256(skia_repo)
            attestation["source_tree_sha256"] = source_tree_sha
            attestation["source_files_count"] = source_count
            attestation["build_dir"] = str(build_dir)
            attestation["gn_args_sha256"] = file_sha256_short(gn_args_file)
            attestation["batches_run"] = "full" if args.full else args.batches

            # Compilation with ninja
            ninja_log = ref_dir / "ninja.log"
            ninja_rc = 0
            ninja_dt = 0
            if not args.skip_build:
                log(f"[{label}] Compiling engine targets with ninja...", driver_log)
                t_ninja0 = time.time()
                # Share RBE/Siso build cache across worktrees regardless of absolute path
                ninja_env = {**os.environ, "DEPOT_TOOLS_USE_VIRTUAL_BUILD_PATH": "1"}
                with open(ninja_log, "w") as lf:
                    res = subprocess.run(
                        [
                            ninja_bin,
                            "-C",
                            str(build_dir),
                            "flutter_tester",
                            "sky_engine",
                            "txt_unittests",
                            "ui_unittests",
                        ],
                        cwd=str(engine_src),
                        env=ninja_env,
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

            attestation.update(detect_unicode_backend(build_dir, target_unicode))

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
                "full_passed": 0,
                "full_failed": 0,
                "full_suites_executed": 0,
                "full_shards_total": 0,
                "unfinished_shards": [],
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

            # 2. Dart framework test batches or full sharded suite
            from parse_flutter_paragraph_tests import summarize_dart_json_shards, summarize_dart_log

            flutter_bin = repo / "bin/flutter"
            engine_flags = [
                f"--local-engine={build_dir.name}",
                f"--local-engine-host={build_dir.name}",
                f"--local-engine-src-path={engine_src}",
            ]
            flutter_env = os.environ.copy()
            dart_sdk_stamp = repo / "bin/cache/engine-dart-sdk.stamp"
            if "FLUTTER_PREBUILT_ENGINE_VERSION" not in flutter_env and dart_sdk_stamp.exists():
                stamp_val = dart_sdk_stamp.read_text(errors="replace").strip()
                if stamp_val:
                    flutter_env["FLUTTER_PREBUILT_ENGINE_VERSION"] = stamp_val

            if args.full:
                all_fw_tests = collect_all_framework_tests(repo)
                attestation["full_expected_files"] = len(all_fw_tests)
                rc, shards_dir, shard_cmds = run_sharded_framework_tests(
                    repo=repo,
                    flutter_bin=flutter_bin,
                    engine_flags=engine_flags,
                    flutter_env=flutter_env,
                    ref_dir=ref_dir,
                    label=label,
                    test_files=all_fw_tests,
                    num_shards=args.flutter_shards,
                    concurrency=args.flutter_concurrency,
                    timeout=args.timeout,
                    driver_log=driver_log,
                )
                test_commands.extend(shard_cmds)
                if rc != 0 and worst_test_rc == 0:
                    worst_test_rc = rc
                f_res = summarize_dart_json_shards(shards_dir)
                summary["full_passed"] = f_res["passed"]
                summary["full_failed"] = f_res["failed"]
                summary["full_suites_executed"] = f_res["suites_executed"]
                summary["full_shards_total"] = f_res["shards_total"]
                summary["unfinished_shards"] = f_res["unfinished_shards"]
                summary["failures"].extend(f_res["failures"])
                if not f_res["finished_cleanly"]:
                    summary["finished_cleanly"] = False
                if f_res["abort_detected"]:
                    summary["abort_detected"] = True
                    summary["crash_reason"] = f_res["crash_reason"]
            else:
                batch_parts = args.batches.split(",")
                batch_configs = [
                    ("batch1", BATCH_1_FILES, args.batches in ("all", "1") or "1" in batch_parts),
                    ("batch2", BATCH_2_FILES, args.batches in ("all", "2") or "2" in batch_parts),
                    ("batch3", BATCH_3_FILES, args.batches in ("all", "3") or "3" in batch_parts),
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
                            env=flutter_env,
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
                + summary["full_passed"]
            )
            summary["total_failed"] = (
                summary["cpp_failed"]
                + summary["batch1_failed"]
                + summary["batch2_failed"]
                + summary["batch3_failed"]
                + summary["full_failed"]
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
