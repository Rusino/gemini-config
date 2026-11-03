#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import pathlib
import subprocess
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DATA_DIR = SKILL_DIR / "data"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import autoroller_rewrites as ar
import parse_g3_paragraph_tests as pg3
import run_g3_paragraph_tests as rg3

FIND_RDEPS_TARGET = "//prototypes/projects/engprod/lawncare/skills/find_rdeps:find_rdeps_script"
FIND_RDEPS_BIN_REL = (
    "blaze-bin/prototypes/projects/engprod/lawncare/skills/find_rdeps/find_rdeps_script"
)

DEFAULT_SEEDS = (
    "//third_party/skia/HEAD:skparagraph_harfbuzz_skunicode",
    "//third_party/skia/HEAD/modules/skparagraph:skparagraph_harfbuzz_skunicode",
    "//third_party/skia/HEAD/modules/skparagraph/src:srcs",
    "//third_party/skia/HEAD/modules/skparagraph/include:hdrs",
    "//third_party/skia/HEAD:skunicode_icu",
    "//third_party/skia/HEAD/modules/skunicode:skunicode_core",
    "//third_party/skia/HEAD/modules/skunicode:skunicode_icu",
    "//third_party/skia/HEAD/modules/skunicode:skunicode_apple",
    "//third_party/skia/HEAD/modules/skunicode/src:srcs",
    "//third_party/skia/HEAD/modules/skunicode/src:icu_core_srcs",
    "//third_party/skia/HEAD/modules/skunicode/src:icu_bidi_srcs",
    "//third_party/skia/HEAD/modules/skunicode/src:bidi_full_srcs",
    "//third_party/skia/HEAD/modules/skunicode/src:bidi_subset_srcs",
    "//third_party/skia/HEAD/modules/skunicode/src:builtin_srcs",
    "//third_party/skia/HEAD/modules/skunicode/src:runtime_srcs",
    "//third_party/skia/HEAD/modules/skunicode/src:apple_srcs",
    "//third_party/skia/HEAD/modules/skunicode/include:core_hdrs",
    "//third_party/skia/HEAD/modules/skunicode/include:icu_hdrs",
    "//third_party/skia/HEAD/modules/skunicode/include:apple_hdrs",
)

# Skip non-paragraph eternal (3600s) video-storytelling Skottie test and full engine/core trees
EXCLUDED_BRANCHES = frozenset(
    {
        "//googlex/gcam/creative_camera/video_storytelling/video_renderers/"
        "skottie_renderer:skottie_renderer_test",
        "//third_party/flutter_engine:skia",
        "//third_party/skia/HEAD:skia_build_test",
    }
)
# Public Skia build_test gates checked by CheckTestsSkia during autorolls
PINNED_BUILD_TESTS = (
    "//third_party/skia/HEAD:skia_build_test",
    "//third_party/skia/HEAD:skia_build_test_linux",
)
EXCLUDED_TAG_REGEX = "manual|notap|no_presubmit"


def _ensure_find_rdeps_bin(g3_root: pathlib.Path) -> pathlib.Path:
    bin_path = g3_root / FIND_RDEPS_BIN_REL
    if not bin_path.exists():
        subprocess.run(
            ["blaze", "build", FIND_RDEPS_TARGET],
            cwd=str(g3_root),
            check=True,
        )
    return bin_path


def _query_rdeps_batch(
    bin_path: pathlib.Path,
    g3_root: pathlib.Path,
    targets: list[str],
    chunk_size: int = 100,
) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(targets), chunk_size):
        chunk = targets[i : i + chunk_size]
        joined = ",".join(chunk)
        proc = subprocess.run(
            [str(bin_path), f"--target={joined}", "--depth=1", "--local=false"],
            cwd=str(g3_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        for ln in proc.stdout.splitlines():
            s = ln.strip()
            if s.startswith("//"):
                found.add(s)
    return found


def run_depserver_bfs(
    g3_root: pathlib.Path,
    seeds: tuple[str, ...],
    depth: int,
    excluded: frozenset[str],
) -> set[str]:
    bin_path = _ensure_find_rdeps_bin(g3_root)
    frontier: set[str] = set(seeds)
    visited: set[str] = set(seeds)
    discovered: set[str] = set()

    for _ in range(1, depth + 1):
        if not frontier:
            break
        raw_next = _query_rdeps_batch(bin_path, g3_root, sorted(frontier))
        next_frontier: set[str] = set()
        for lbl in raw_next:
            if lbl in excluded or lbl in visited:
                continue
            visited.add(lbl)
            discovered.add(lbl)
            next_frontier.add(lbl)
        frontier = next_frontier
    return discovered


def filter_runnable_tests(
    g3_root: pathlib.Path,
    candidates: set[str],
    chunk_size: int = 100,
) -> tuple[list[str], list[str]]:
    test_rules: set[str] = set()
    cand_list = sorted(candidates)
    for i in range(0, len(cand_list), chunk_size):
        chunk = cand_list[i : i + chunk_size]
        expr = f"set({' '.join(chunk)})"
        proc = subprocess.run(
            ["blaze", "query", "--keep_going", expr, "--output=label_kind"],
            cwd=str(g3_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        for ln in proc.stdout.splitlines():
            if " rule " not in ln:
                continue
            kind, label = ln.split(" rule ", 1)
            k = kind.strip()
            if "test" in k and k != "test_suite":
                test_rules.add(label.strip())

    manual: set[str] = set()
    test_list = sorted(test_rules)
    for i in range(0, len(test_list), chunk_size):
        chunk = test_list[i : i + chunk_size]
        expr = f'attr(tags, "{EXCLUDED_TAG_REGEX}", set({" ".join(chunk)}))'
        proc = subprocess.run(
            ["blaze", "query", "--keep_going", expr],
            cwd=str(g3_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        for ln in proc.stdout.splitlines():
            s = ln.strip()
            if s.startswith("//"):
                manual.add(s)

    runnable = sorted(test_rules - manual)
    return runnable, sorted(manual)


def split_fast_and_slow(
    runnable: list[str],
    existing_slow: set[str],
    bep_durations_ms: dict[str, int],
    slow_threshold_sec: int,
    reset_slow: bool,
) -> tuple[list[str], list[str]]:
    threshold_ms = slow_threshold_sec * 1000
    slow_set: set[str] = set() if reset_slow else set(existing_slow)
    for lbl, dur_ms in bep_durations_ms.items():
        if dur_ms > threshold_ms:
            slow_set.add(lbl)

    runnable_set = set(runnable)
    final_slow = sorted(runnable_set & slow_set)
    final_fast = sorted(runnable_set - set(final_slow))
    return final_fast, final_slow


# Ensure public Skia build_test gates stay in fast tier unless measured slow
def merge_pinned_build_tests(
    fast_list: list[str],
    slow_list: list[str],
    pinned: tuple[str, ...] = PINNED_BUILD_TESTS,
) -> list[str]:
    slow_set = set(slow_list)
    extra = [t for t in pinned if t not in slow_set]
    return sorted(set(fast_list) | set(extra))


def build_manifest_counts(
    fast_len: int,
    slow_len: int,
    runnable_len: int,
    manual_len: int,
    prev_counts: dict[str, int] | None = None,
    pinned_len: int = 0,
) -> dict[str, int]:
    counts = {
        "fast": fast_len,
        "slow": slow_len,
        "pinned_build_tests": pinned_len,
        "total_runnable": fast_len + slow_len,
        "transitive_runnable": runnable_len,
        "manual_excluded": manual_len,
    }
    # Preserve TAP Canary-G3 coverage baseline across target refreshes
    if prev_counts:
        if "canary_covered_transitive" in prev_counts:
            cov_trans = int(prev_counts["canary_covered_transitive"])
        elif "canary_covered_runnable" in prev_counts:
            cov_trans = int(prev_counts["canary_covered_runnable"])
        else:
            cov_trans = -1
        if cov_trans >= 0:
            counts["canary_covered_transitive"] = cov_trans
            counts["canary_covered_runnable"] = cov_trans + pinned_len
            counts["uncovered_runnable"] = max(0, runnable_len - cov_trans)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh cached google3 SkParagraph/SkUnicode test target lists via Depserver"
    )
    parser.add_argument("--citc-client", default=None)
    parser.add_argument("--wait-lock-sec", type=float, default=0.0)
    parser.add_argument("--depth", type=int, default=4)
    parser.add_argument(
        "--bep",
        type=pathlib.Path,
        default=None,
        help="Optional full BEP for slow split",
    )
    parser.add_argument("--slow-threshold-sec", type=int, default=600)
    parser.add_argument("--reset-slow", action="store_true")
    parser.add_argument("--out-dir", type=pathlib.Path, default=DATA_DIR)
    args = parser.parse_args(argv)

    rg3.enforce_citc_hygiene(args.out_dir)
    citc_client, citc_root = rg3.resolve_citc_root(args.citc_client, create_if_missing=False)
    with rg3.acquire_citc_lock(citc_client, wait_sec=args.wait_lock_sec):
        g3_root = citc_root / "google3"
        base_cl = rg3.read_citc_base_cl(citc_root)
        g3_skia_rev = ar.read_g3_skia_revision(g3_root / ar.G3_SKIA_REL / "METADATA")

        discovered = run_depserver_bfs(g3_root, DEFAULT_SEEDS, args.depth, EXCLUDED_BRANCHES)
        runnable, manual = filter_runnable_tests(g3_root, discovered)

    existing_slow = set(pg3._read_targets_file(args.out_dir / "slow_targets.txt"))
    bep_durs: dict[str, int] = {}
    if args.bep and args.bep.exists():
        bep_durs = pg3.parse_bep_file(args.bep).get("durations_ms") or {}

    fast_list, slow_list = split_fast_and_slow(
        runnable,
        existing_slow,
        bep_durs,
        args.slow_threshold_sec,
        args.reset_slow,
    )
    fast_list = merge_pinned_build_tests(fast_list, slow_list)
    pinned_added = len(fast_list) + len(slow_list) - len(runnable)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out_dir / "targets_manifest.json"
    prev_counts: dict[str, int] | None = None
    if manifest_path.exists():
        try:
            prev_counts = json.loads(manifest_path.read_text(encoding="utf-8")).get("counts")
        except (OSError, json.JSONDecodeError):
            prev_counts = None

    (args.out_dir / "fast_targets.txt").write_text("\n".join(fast_list) + "\n", encoding="utf-8")
    (args.out_dir / "slow_targets.txt").write_text("\n".join(slow_list) + "\n", encoding="utf-8")

    manifest = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_cl": base_cl,
        "g3_skia_revision": g3_skia_rev,
        "bfs_depth": args.depth,
        "slow_threshold_sec": args.slow_threshold_sec,
        "seeds": list(DEFAULT_SEEDS),
        "excluded_branches": sorted(EXCLUDED_BRANCHES),
        "pinned_build_tests": list(PINNED_BUILD_TESTS),
        "excluded_tag_regex": EXCLUDED_TAG_REGEX,
        "counts": build_manifest_counts(
            len(fast_list),
            len(slow_list),
            len(runnable),
            len(manual),
            prev_counts,
            pinned_added,
        ),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"Refreshed {args.out_dir}: fast={len(fast_list)}, slow={len(slow_list)}, "
        f"manual_excluded={len(manual)} @ cl/{base_cl}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
