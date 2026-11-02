#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DATA_DIR = SKILL_DIR / "data"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import autoroller_rewrites as ar
import parse_g3_paragraph_tests as pg3

SKIA_GERRIT_GIT_URL = "https://skia.googlesource.com/skia.git"
SKIA_GERRIT_API_URL = "https://skia-review.googlesource.com/changes"
CITC_CHANGE_RE = re.compile(r"\bchange:\s*(\d+)\b")
FORBIDDEN_OUT_PREFIXES = (
    "/google/src/cloud/",
    "/Volumes/google/src/cloud/",
    "/google_src/",
)
DEFAULT_SKIA_REPOS = (
    pathlib.Path.home() / ".gemini/jetski/scratch/skia",
    pathlib.Path.home() / ".gemini/jetski/scratch/flutter/engine/src/flutter/third_party/skia",
)
HASHED_SUBDIRS = (
    "modules/skparagraph",
    "modules/skunicode",
    "modules/skshaper",
)


def _run(
    cmd: list[str],
    *,
    cwd: pathlib.Path | None = None,
    check: bool = True,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# Refuse output paths inside world-readable CitC storage
def enforce_citc_hygiene(out_dir: pathlib.Path) -> None:
    for candidate in (str(out_dir), str(out_dir.resolve())):
        if candidate.startswith(FORBIDDEN_OUT_PREFIXES):
            raise RuntimeError(
                f"Refusing --out inside CitC ({candidate}); use /tmp or scratch outside CitC"
            )


def resolve_skia_repo(explicit: pathlib.Path | None) -> pathlib.Path:
    if explicit is not None:
        if not (explicit / ".git").exists():
            raise RuntimeError(f"--skia-repo is not a git checkout: {explicit}")
        return explicit.resolve()
    for p in DEFAULT_SKIA_REPOS:
        if p.exists() and (p / ".git").exists():
            return p.resolve()
    raise RuntimeError("Could not auto-detect local Skia git repo; pass --skia-repo")


def resolve_citc_root(client_name: str | None, create_if_missing: bool) -> tuple[str, pathlib.Path]:
    user = getpass.getuser()
    cloud_base = pathlib.Path("/google/src/cloud") / user
    if not cloud_base.exists():
        raise RuntimeError(f"CitC mount {cloud_base} not found (google3 corpus requires Cloudtop)")

    candidates = [client_name] if client_name else ["skia_g3_ab", "skia_g3_pilot"]
    for name in candidates:
        if not name:
            continue
        root = cloud_base / name
        if (root / "google3").is_dir():
            return name, root

    target_name = client_name or "skia_g3_ab"
    if create_if_missing:
        _run(["g4", "citc", target_name], check=True, timeout=180)
        root = cloud_base / target_name
        if (root / "google3").is_dir():
            return target_name, root
    raise RuntimeError(
        f"CitC client not found ({candidates}); pass --citc-client or --create-citc"
    )


def read_citc_base_cl(citc_root: pathlib.Path) -> int | None:
    ws_ascii = citc_root / ".citc/srcfs_workspace.ascii"
    if ws_ascii.exists():
        m = CITC_CHANGE_RE.search(ws_ascii.read_text(encoding="utf-8", errors="replace"))
        if m:
            return int(m.group(1))
    return None


def list_citc_dirty_files(g3_root: pathlib.Path) -> list[str]:
    proc = _run(["g4", "status"], cwd=g3_root, check=False, timeout=60)
    combined = f"{proc.stdout}\n{proc.stderr}"
    dirty: list[str] = []
    for raw in combined.splitlines():
        s = raw.strip()
        if not s or "not opened on this client" in s or s.startswith("Default change:"):
            continue
        dirty.append(s)
    return dirty


def load_tier_targets(
    tier: str,
    custom_file: pathlib.Path | None,
    max_age_days: int,
    allow_stale: bool,
) -> tuple[str, list[str], str | None]:
    if custom_file is not None:
        targets = pg3._read_targets_file(custom_file)
        if not targets:
            raise RuntimeError(f"Custom --targets-file is empty: {custom_file}")
        return "custom", targets, None

    manifest_path = DATA_DIR / "targets_manifest.json"
    stale_warning: str | None = None
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        gen_str = manifest.get("generated_at", "")
        if gen_str:
            gen_dt = datetime.fromisoformat(gen_str.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - gen_dt).days
            if age_days > max_age_days:
                msg = (
                    f"targets_manifest.json is {age_days}d old (> {max_age_days}d); "
                    f"run refresh_g3_targets.py"
                )
                if not allow_stale:
                    raise RuntimeError(f"{msg} (or pass --allow-stale-targets)")
                stale_warning = msg

    fast = pg3._read_targets_file(DATA_DIR / "fast_targets.txt")
    slow = pg3._read_targets_file(DATA_DIR / "slow_targets.txt")
    if tier == "fast":
        selected = fast
    elif tier == "slow":
        selected = slow
    elif tier == "all":
        selected = list(dict.fromkeys([*fast, *slow]))
    else:
        raise ValueError(f"Unsupported tier: {tier}")
    if not selected:
        raise RuntimeError(f"No targets found for tier '{tier}' in {DATA_DIR}")
    return tier, selected, stale_warning


# Compute deterministic SHA-256 across paragraph/unicode/shaper and any touched Skia files
def hash_g3_skia_tree(g3_root: pathlib.Path, extra_rel_paths: list[str]) -> str:
    skia_head = g3_root / ar.G3_SKIA_REL
    files_to_hash: set[pathlib.Path] = {skia_head / "METADATA"}
    for sub in HASHED_SUBDIRS:
        sub_dir = skia_head / sub
        if sub_dir.exists():
            for p in sub_dir.rglob("*"):
                if p.is_file():
                    files_to_hash.add(p)
    for rel in extra_rel_paths:
        p = g3_root / rel
        if p.is_file():
            files_to_hash.add(p)

    h = hashlib.sha256()
    for p in sorted(files_to_hash):
        rel = p.relative_to(g3_root).as_posix()
        h.update(rel.encode("utf-8") + b"\0")
        h.update(p.read_bytes() + b"\0")
    for rel in sorted(extra_rel_paths):
        p = g3_root / rel
        if not p.exists():
            h.update(f"DELETED:{rel}\0".encode("utf-8"))
    return h.hexdigest()[:16]


def _ensure_commit_present(skia_repo: pathlib.Path, commit_sha: str) -> None:
    chk = _run(
        ["git", "cat-file", "-e", f"{commit_sha}^{{commit}}"],
        cwd=skia_repo,
        check=False,
    )
    if chk.returncode != 0:
        _run(["git", "fetch", SKIA_GERRIT_GIT_URL, commit_sha], cwd=skia_repo, timeout=300)


def _resolve_gerrit_patch_ref(cl_spec: str) -> tuple[int, int, str]:
    body = cl_spec[len("cl:") :]
    if "/" in body:
        c_str, ps_str = body.split("/", 1)
        change, ps = int(c_str), int(ps_str)
    else:
        change = int(body)
        url = f"{SKIA_GERRIT_API_URL}/{change}?o=CURRENT_REVISION"
        with urllib.request.urlopen(url, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        payload = json.loads(raw.lstrip(")]}'\n"))
        cur_rev = payload["current_revision"]
        ps = int(payload["revisions"][cur_rev]["_number"])
    ref = f"refs/changes/{change % 100:02d}/{change}/{ps}"
    return change, ps, ref


# Cherry-pick candidate commits onto g3_skia_revision and surface conflict stderr
def _cherry_pick_onto_stage(
    stage_wt: pathlib.Path,
    revs: list[str],
    g3_skia_revision: str,
) -> None:
    proc = _run(["git", "cherry-pick", "--no-commit", *revs], cwd=stage_wt, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            f"git cherry-pick onto G3 Skia base {g3_skia_revision[:12]} failed "
            f"(sync CitC via 'g4 sync' if base lags):\n{detail}"
        )


# Isolate unmerged CL commits relative to upstream main so post-roll main commits are excluded
def resolve_cl_cherry_pick_revs(
    repo_dir: pathlib.Path,
    tip_sha: str,
    upstream_ref: str = "origin/main",
    base_ref_override: str | None = None,
) -> list[str]:
    if base_ref_override:
        b_sha = _run(["git", "rev-parse", base_ref_override], cwd=repo_dir).stdout.strip()
        revs = _run(["git", "rev-list", "--reverse", f"{b_sha}..{tip_sha}"], cwd=repo_dir)
        out = revs.stdout.split()
        return out if out else [tip_sha]
    mb = _run(["git", "merge-base", tip_sha, upstream_ref], cwd=repo_dir, check=False)
    if mb.returncode == 0 and mb.stdout.strip() != tip_sha:
        revs = _run(
            ["git", "rev-list", "--reverse", f"{mb.stdout.strip()}..{tip_sha}"],
            cwd=repo_dir,
        ).stdout.split()
        if revs:
            return revs
    return [tip_sha]


# Materialize candidate Skia changes onto g3_skia_revision using 3-way git operations
def stage_candidate_in_worktree(
    ref_spec: str,
    skia_repo: pathlib.Path,
    g3_skia_revision: str,
    stage_wt: pathlib.Path,
    base_ref_override: str | None,
) -> dict[str, Any]:
    _ensure_commit_present(skia_repo, g3_skia_revision)
    if stage_wt.exists():
        _run(["git", "worktree", "remove", "--force", str(stage_wt)], cwd=skia_repo, check=False)
        shutil.rmtree(stage_wt, ignore_errors=True)

    _run(
        ["git", "worktree", "add", "--detach", str(stage_wt), g3_skia_revision],
        cwd=skia_repo,
    )

    dirty_files: list[str] = []
    source_commit_sha: str | None = None

    if ref_spec == "WORKTREE":
        head_sha = _run(["git", "rev-parse", "HEAD"], cwd=skia_repo).stdout.strip()
        source_commit_sha = head_sha
        if base_ref_override:
            base_sha = _run(["git", "rev-parse", base_ref_override], cwd=skia_repo).stdout.strip()
        else:
            mb = _run(
                ["git", "merge-base", "HEAD", "origin/main"],
                cwd=skia_repo,
                check=False,
            )
            base_sha = mb.stdout.strip() if mb.returncode == 0 else head_sha

        if base_sha != head_sha:
            revs = _run(
                ["git", "rev-list", "--reverse", f"{base_sha}..{head_sha}"],
                cwd=skia_repo,
            ).stdout.split()
            if revs:
                _cherry_pick_onto_stage(stage_wt, revs, g3_skia_revision)

        wt_diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD"],
            cwd=str(skia_repo),
            check=True,
            capture_output=True,
        ).stdout
        if wt_diff:
            proc = subprocess.run(
                ["git", "apply", "--3way", "--whitespace=nowarn"],
                cwd=str(stage_wt),
                input=wt_diff,
                capture_output=True,
            )
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="replace")
                raise RuntimeError(f"git apply --3way onto {g3_skia_revision} failed:\n{err}")

        untracked = _run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=skia_repo,
        ).stdout.splitlines()
        for rel in untracked:
            rel_s = rel.strip()
            if rel_s and ar.should_include_file(rel_s):
                dst = stage_wt / rel_s
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(skia_repo / rel_s, dst)

        porcelain = _run(["git", "status", "--porcelain"], cwd=skia_repo).stdout.splitlines()
        dirty_files = [ln[3:].strip() for ln in porcelain if ln.strip()]
    elif ref_spec.startswith("cl:"):
        _, _, patch_ref = _resolve_gerrit_patch_ref(ref_spec)
        upstream_ref = "refs/remotes/g3_upstream/main"
        _run(
            ["git", "fetch", SKIA_GERRIT_GIT_URL, f"+refs/heads/main:{upstream_ref}"],
            cwd=stage_wt,
            check=False,
            timeout=300,
        )
        _run(["git", "fetch", SKIA_GERRIT_GIT_URL, patch_ref], cwd=stage_wt, timeout=300)
        tip_sha = _run(["git", "rev-parse", "FETCH_HEAD"], cwd=stage_wt).stdout.strip()
        source_commit_sha = tip_sha
        has_fresh_up = (
            _run(["git", "rev-parse", "--verify", upstream_ref], cwd=stage_wt, check=False)
            .returncode
            == 0
        )
        revs = resolve_cl_cherry_pick_revs(
            stage_wt,
            tip_sha,
            upstream_ref if has_fresh_up else "origin/main",
            base_ref_override,
        )
        _cherry_pick_onto_stage(stage_wt, revs, g3_skia_revision)
    else:
        if ".." in ref_spec:
            revs = _run(["git", "rev-list", "--reverse", ref_spec], cwd=skia_repo).stdout.split()
            if not revs:
                raise RuntimeError(f"Empty commit range: {ref_spec}")
            source_commit_sha = revs[-1]
        else:
            tip_sha = _run(
                ["git", "rev-parse", f"{ref_spec}^{{commit}}"],
                cwd=skia_repo,
            ).stdout.strip()
            source_commit_sha = tip_sha
            revs = resolve_cl_cherry_pick_revs(
                skia_repo,
                tip_sha,
                "origin/main",
                base_ref_override,
            )
        _cherry_pick_onto_stage(stage_wt, revs, g3_skia_revision)

    _run(["git", "add", "-A"], cwd=stage_wt)
    ns_lines = _run(
        ["git", "diff", "--cached", "--name-status", "--no-renames", g3_skia_revision],
        cwd=stage_wt,
    ).stdout.splitlines()

    changed_or_added: list[str] = []
    added_only: list[str] = []
    deleted: list[str] = []
    for ln in ns_lines:
        if not ln.strip():
            continue
        status_tok, rel_p = ln.split("\t", 1)
        st = status_tok.strip()
        rel_clean = rel_p.strip()
        if st == "D":
            deleted.append(rel_clean)
        else:
            changed_or_added.append(rel_clean)
            if st == "A":
                added_only.append(rel_clean)

    if not changed_or_added and not deleted:
        raise RuntimeError(
            f"Candidate '{ref_spec}' produced 0 file changes relative to {g3_skia_revision[:12]}"
        )

    sync_errs = ar.check_bazel_filegroup_sync(added_only, deleted, stage_wt)
    if sync_errs:
        raise RuntimeError(
            "Skia BUILD.bazel out of sync with added/deleted files:\n  - "
            + "\n  - ".join(sync_errs)
        )

    return {
        "source_commit_sha": source_commit_sha,
        "dirty_files": dirty_files,
        "changed_or_added": changed_or_added,
        "deleted": deleted,
    }


def check_citc_dirty_hygiene(
    citc_client: str,
    pre_dirty: list[str],
    allow_dirty_citc: bool,
    needs_candidate_staging: bool,
) -> None:
    if not pre_dirty:
        return
    # Never overwrite uncommitted user edits inside third_party/skia/HEAD even with bypass flag
    skia_dirty = [d for d in pre_dirty if ar.G3_SKIA_REL in d]
    if skia_dirty and needs_candidate_staging:
        raise RuntimeError(
            f"CitC client '{citc_client}' has modified files inside {ar.G3_SKIA_REL}; "
            f"revert them before staging candidates:\n  " + "\n  ".join(skia_dirty[:10])
        )
    if not allow_dirty_citc:
        raise RuntimeError(
            f"CitC client '{citc_client}' has {len(pre_dirty)} open/modified file(s); "
            f"revert them or pass --allow-dirty-citc:\n  " + "\n  ".join(pre_dirty[:10])
        )


def restore_citc_skia_head(
    g3_root: pathlib.Path,
    added_new_rel: list[str],
    pre_dirty: list[str],
) -> bool:
    for rel in added_new_rel:
        p = g3_root / rel
        if p.exists():
            p.unlink()
    if not any(ar.G3_SKIA_REL in d for d in pre_dirty):
        _run(
            ["g4", "revert", "//depot/google3/third_party/skia/HEAD/..."],
            cwd=g3_root,
            check=False,
            timeout=180,
        )
    post_dirty = list_citc_dirty_files(g3_root)
    return set(post_dirty) <= set(pre_dirty)


def run_single_leg(
    label: str,
    ref_spec: str,
    *,
    out_dir: pathlib.Path,
    citc_client: str,
    citc_root: pathlib.Path,
    skia_repo: pathlib.Path | None,
    tier: str,
    targets: list[str],
    stale_warning: str | None,
    flaky_attempts: int,
    base_ref_override: str | None,
    pre_dirty: list[str],
) -> None:
    leg_dir = out_dir / label
    leg_dir.mkdir(parents=True, exist_ok=True)
    targets_path = leg_dir / "targets.txt"
    targets_path.write_text("\n".join(targets) + "\n", encoding="utf-8")

    g3_root = citc_root / "google3"
    g3_skia_rev = ar.read_g3_skia_revision(g3_root / ar.G3_SKIA_REL / "METADATA")
    citc_base_cl = read_citc_base_cl(citc_root)

    stage_wt = out_dir / f"_skia_stage_{label}"
    sync_info: dict[str, list[str]] = {"written": [], "added_new": [], "deleted": []}
    source_commit_sha: str | None = None
    skia_dirty: list[str] = []
    restored_clean = True

    try:
        if ref_spec == "G3_HEAD":
            restore_citc_skia_head(g3_root, [], pre_dirty)
        else:
            if skia_repo is None:
                raise RuntimeError(f"skia_repo required for non-G3_HEAD spec '{ref_spec}'")
            staged = stage_candidate_in_worktree(
                ref_spec,
                skia_repo,
                g3_skia_rev,
                stage_wt,
                base_ref_override,
            )
            source_commit_sha = staged["source_commit_sha"]
            skia_dirty = staged["dirty_files"]
            sync_info = ar.sync_skia_tree_to_g3(
                stage_wt,
                g3_root,
                staged["changed_or_added"],
                staged["deleted"],
            )

        touched_all = [*sync_info["written"], *sync_info["deleted"]]
        tree_sha = hash_g3_skia_tree(g3_root, touched_all)

        bep_path = leg_dir / "bep.json"
        log_path = leg_dir / "blaze.log"
        blaze_cmd = [
            "blaze",
            "--host_jvm_args=-Xmx32G",
            "test",
            "--symlink_prefix=/",
            "--keep_going",
            "--test_output=errors",
            "--config=minimize_memory",
            f"--build_event_json_file={bep_path}",
            f"--target_pattern_file={targets_path}",
        ]
        if flaky_attempts > 1:
            blaze_cmd.append(f"--flaky_test_attempts={flaky_attempts}")

        t0 = time.monotonic()
        with log_path.open("w", encoding="utf-8") as log_f:
            proc = subprocess.run(
                blaze_cmd,
                cwd=str(g3_root),
                stdout=log_f,
                stderr=subprocess.STDOUT,
                check=False,
            )
        wall_s = round(time.monotonic() - t0, 1)
    finally:
        if skia_repo is not None and stage_wt.exists():
            _run(
                ["git", "worktree", "remove", "--force", str(stage_wt)],
                cwd=skia_repo,
                check=False,
            )
            shutil.rmtree(stage_wt, ignore_errors=True)
        if ref_spec != "G3_HEAD":
            restored_clean = restore_citc_skia_head(
                g3_root,
                sync_info["added_new"],
                pre_dirty,
            )

    summary = pg3.parse_bep_file(bep_path, expected_targets=targets, blaze_log_path=log_path)
    if summary.get("wall_duration_s") is None:
        summary["wall_duration_s"] = wall_s

    attestation = {
        "label": label,
        "ref_spec": ref_spec,
        "skia_commit_sha": source_commit_sha,
        "skia_dirty_files": skia_dirty,
        "citc_client": citc_client,
        "citc_base_cl": citc_base_cl,
        "citc_pre_dirty_files": pre_dirty,
        "citc_restored_clean": restored_clean,
        "g3_skia_revision": g3_skia_rev,
        "source_tree_sha256": tree_sha,
        "rewritten_files": sync_info["written"],
        "deleted_files": sync_info["deleted"],
        "rewritten_file_count": len(sync_info["written"]),
        "deleted_file_count": len(sync_info["deleted"]),
        "tier": tier,
        "expected_targets": len(targets),
        "targets_stale_warning": stale_warning,
        "flaky_attempts": flaky_attempts,
        "blaze_cmd": blaze_cmd,
        "blaze_rc": proc.returncode,
        "wall_duration_s": wall_s,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

    (leg_dir / "attestation.json").write_text(
        json.dumps(attestation, indent=2) + "\n", encoding="utf-8"
    )
    (leg_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run google3 SkParagraph/SkUnicode A/B tests in CitC and emit compare.md"
    )
    parser.add_argument("--out", type=pathlib.Path, required=True, help="Output dir outside CitC")
    parser.add_argument("--tier", choices=("fast", "slow", "all"), default="fast")
    parser.add_argument("--targets-file", type=pathlib.Path, default=None)
    parser.add_argument(
        "--citc-client",
        default=None,
        help="CitC client name (default: skia_g3_ab)",
    )
    parser.add_argument("--create-citc", action="store_true", help="Create CitC client if missing")
    parser.add_argument("--skia-repo", type=pathlib.Path, default=None, help="Local Skia git repo")
    parser.add_argument("--base-ref", default=None, help="Override Skia base ref for diffing")
    parser.add_argument("--flaky-attempts", type=int, default=1)
    parser.add_argument("--max-targets-age-days", type=int, default=30)
    parser.add_argument("--allow-stale-targets", action="store_true")
    parser.add_argument("--allow-dirty-citc", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "specs",
        nargs="*",
        help="Run specs label=ref (default: baseline=G3_HEAD current=WORKTREE)",
    )

    args = parser.parse_args(argv)
    out_dir = args.out.expanduser()
    enforce_citc_hygiene(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_specs = args.specs or ["baseline=G3_HEAD", "current=WORKTREE"]
    parsed_specs: list[tuple[str, str]] = []
    for item in raw_specs:
        if "=" not in item:
            parser.error(f"Expected label=ref_spec, got: {item}")
        lbl, ref = item.split("=", 1)
        parsed_specs.append((lbl.strip(), ref.strip()))

    tier_name, targets, stale_warning = load_tier_targets(
        args.tier,
        args.targets_file,
        args.max_targets_age_days,
        args.allow_stale_targets,
    )
    citc_client, citc_root = resolve_citc_root(args.citc_client, args.create_citc)
    g3_root = citc_root / "google3"
    pre_dirty = list_citc_dirty_files(g3_root)
    needs_skia_repo = any(ref != "G3_HEAD" for _, ref in parsed_specs)
    check_citc_dirty_hygiene(
        citc_client,
        pre_dirty,
        args.allow_dirty_citc,
        needs_skia_repo,
    )
    skia_repo = resolve_skia_repo(args.skia_repo) if needs_skia_repo else None

    for lbl, ref in parsed_specs:
        leg_dir = out_dir / lbl
        if (
            args.resume
            and (leg_dir / "attestation.json").exists()
            and (leg_dir / "summary.json").exists()
        ):
            continue
        run_single_leg(
            lbl,
            ref,
            out_dir=out_dir,
            citc_client=citc_client,
            citc_root=citc_root,
            skia_repo=skia_repo,
            tier=tier_name,
            targets=targets,
            stale_warning=stale_warning,
            flaky_attempts=args.flaky_attempts,
            base_ref_override=args.base_ref,
            pre_dirty=pre_dirty,
        )

    runs = [(lbl, pg3.collect_run_data(str(out_dir / lbl), None)) for lbl, _ in parsed_specs]
    md = pg3.format_comparison_markdown(runs)
    compare_path = out_dir / "compare.md"
    compare_path.write_text(md, encoding="utf-8")
    sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
