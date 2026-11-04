#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from typing import Any, Iterator

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
    pathlib.Path.home() / "Sources/skia",
)
HASHED_SUBDIRS = (
    "modules/skparagraph",
    "modules/skunicode",
    "modules/skshaper",
)
DEFAULT_BASELINE_CACHE_DIR = pathlib.Path(
    os.environ.get(
        "GOOGLE3_PARAGRAPH_CACHE_DIR",
        str(pathlib.Path.home() / ".cache/google3-test-paragraph"),
    )
)
MAX_BASELINE_CACHE_ENTRIES = 6


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


# Refresh remote-tracking main best-effort so local skew checks catch newly landed commits
def _refresh_local_upstream_ref(repo_dir: pathlib.Path) -> None:
    rem_proc = _run(["git", "remote"], cwd=repo_dir, check=False)
    remotes = {ln.strip() for ln in rem_proc.stdout.splitlines() if ln.strip()}
    for rname in ("upstream", "origin"):
        if rname in remotes:
            try:
                proc = _run(
                    [
                        "git",
                        "fetch",
                        "--quiet",
                        rname,
                        f"+refs/heads/main:refs/remotes/{rname}/main",
                    ],
                    cwd=repo_dir,
                    check=False,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                continue
            if proc.returncode == 0:
                break


# Prefer the newer tip when both upstream/main and origin/main exist locally
def _resolve_default_upstream_ref(repo_dir: pathlib.Path, fallback: str) -> str:
    found: list[tuple[str, str]] = []
    for ref in ("upstream/main", "origin/main"):
        chk = _run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo_dir, check=False)
        if chk.returncode == 0 and chk.stdout.strip():
            found.append((ref, chk.stdout.strip()))
    if not found:
        return fallback
    if len(found) == 2 and found[0][1] != found[1][1]:
        anc = _run(
            ["git", "merge-base", "--is-ancestor", found[0][1], found[1][1]],
            cwd=repo_dir,
            check=False,
        )
        if anc.returncode == 0:
            return found[1][0]
    return found[0][0]


def _resolve_parent_or_self(repo_dir: pathlib.Path, commit_sha: str) -> str:
    chk = _run(["git", "rev-parse", "--verify", f"{commit_sha}^"], cwd=repo_dir, check=False)
    return chk.stdout.strip() if chk.returncode == 0 else commit_sha


# Exclude Skia unit tests under /tests/ because google3 builds only production module targets
def _is_g3_compiled_skia_path(rel_path: str) -> bool:
    if not ar.should_include_file(rel_path):
        return False
    if "/tests/" in f"/{rel_path}" and not rel_path.endswith("BUILD.bazel"):
        return False
    return True


# Detect intervening Skia commits between the candidate's git base and target Skia revision
# that touch the same files or production module directories (silent cherry-pick API drift)
def detect_stack_base_skew(
    repo_dir: pathlib.Path,
    candidate_base_sha: str | None,
    g3_skia_revision: str,
    candidate_files: list[str],
    *,
    target_label: str = "G3 Skia",
) -> dict[str, Any] | None:
    if not candidate_base_sha or candidate_base_sha == g3_skia_revision:
        return None
    diff_proc = _run(
        ["git", "diff", "--name-only", "--no-renames", candidate_base_sha, g3_skia_revision],
        cwd=repo_dir,
        check=False,
    )
    if diff_proc.returncode != 0:
        return None

    g3_diff_files = [
        ln.strip()
        for ln in diff_proc.stdout.splitlines()
        if ln.strip() and _is_g3_compiled_skia_path(ln.strip())
    ]
    if not g3_diff_files:
        return None

    cand_compiled = {f for f in candidate_files if _is_g3_compiled_skia_path(f)}
    touched_modules = {
        sub
        for sub in HASHED_SUBDIRS
        if any(f == sub or f.startswith(f"{sub}/") for f in cand_compiled)
    }
    direct_overlap = sorted(f for f in g3_diff_files if f in cand_compiled)
    module_overlap = sorted(
        f
        for f in g3_diff_files
        if any(
            f.startswith(f"{sub}/src/")
            or f.startswith(f"{sub}/include/")
            or (f.startswith(f"{sub}/") and f.endswith("BUILD.bazel"))
            for sub in touched_modules
        )
    )
    overlapping = sorted(set(direct_overlap) | set(module_overlap))
    if not overlapping:
        return None

    ahead_cnt = 0
    behind_cnt = 0
    cnt_proc = _run(
        [
            "git",
            "rev-list",
            "--left-right",
            "--count",
            f"{candidate_base_sha}...{g3_skia_revision}",
        ],
        cwd=repo_dir,
        check=False,
    )
    if cnt_proc.returncode == 0:
        parts = cnt_proc.stdout.strip().split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            ahead_cnt, behind_cnt = int(parts[0]), int(parts[1])

    if behind_cnt > 0 and ahead_cnt == 0:
        direction = "behind"
        dist_desc = f"{behind_cnt} commit(s) behind"
        remediation = f"rebase candidate onto main / {target_label} revision"
        severity = "warning"
    elif ahead_cnt > 0 and behind_cnt == 0:
        direction = "ahead"
        dist_desc = f"{ahead_cnt} commit(s) ahead of"
        remediation = "sync CitC via 'g4 sync' or wait for Skia autoroll"
        severity = "warning" if direct_overlap else "info"
    else:
        direction = "diverged"
        dist_desc = f"diverged (+{ahead_cnt}/-{behind_cnt} commits) from"
        remediation = "rebase candidate onto main and/or sync CitC via 'g4 sync'"
        severity = "warning"

    sample = ", ".join(f"`{p}`" for p in overlapping[:4])
    if len(overlapping) > 4:
        sample += f" +{len(overlapping) - 4} more"
    warning = (
        f"candidate base `{candidate_base_sha[:10]}` is {dist_desc} "
        f"{target_label} `{g3_skia_revision[:12]}` across {len(overlapping)} overlapping file(s) "
        f"({sample}); {remediation}"
    )
    return {
        "severity": severity,
        "direction": direction,
        "ahead_commits": ahead_cnt,
        "behind_commits": behind_cnt,
        "candidate_base_sha": candidate_base_sha,
        "g3_skia_revision": g3_skia_revision,
        "target_label": target_label,
        "direct_overlap_files": direct_overlap,
        "overlapping_files": overlapping,
        "warning": warning,
    }


# Materialize candidate Skia changes onto g3_skia_revision using 3-way git operations
def stage_candidate_in_worktree(
    ref_spec: str,
    skia_repo: pathlib.Path,
    g3_skia_revision: str,
    stage_wt: pathlib.Path,
    base_ref_override: str | None,
    *,
    allow_stack_base_skew: bool = False,
) -> dict[str, Any]:
    _ensure_commit_present(skia_repo, g3_skia_revision)
    if not ref_spec.startswith("cl:"):
        _refresh_local_upstream_ref(skia_repo)
    if stage_wt.exists():
        _run(["git", "worktree", "remove", "--force", str(stage_wt)], cwd=skia_repo, check=False)
        shutil.rmtree(stage_wt, ignore_errors=True)

    _run(
        ["git", "worktree", "add", "--detach", str(stage_wt), g3_skia_revision],
        cwd=skia_repo,
    )

    dirty_files: list[str] = []
    source_commit_sha: str | None = None
    candidate_base_sha: str | None = None
    default_up = _resolve_default_upstream_ref(skia_repo, g3_skia_revision)
    active_up_ref = default_up

    if ref_spec == "WORKTREE":
        head_sha = _run(["git", "rev-parse", "HEAD"], cwd=skia_repo).stdout.strip()
        source_commit_sha = head_sha
        if base_ref_override:
            base_sha = _run(["git", "rev-parse", base_ref_override], cwd=skia_repo).stdout.strip()
        else:
            mb = _run(
                ["git", "merge-base", "HEAD", default_up],
                cwd=skia_repo,
                check=False,
            )
            base_sha = mb.stdout.strip() if mb.returncode == 0 else head_sha

        candidate_base_sha = base_sha
        if base_sha != head_sha:
            revs = _run(
                ["git", "rev-list", "--reverse", f"{base_sha}..{head_sha}"],
                cwd=skia_repo,
            ).stdout.split()
            if revs:
                candidate_base_sha = _resolve_parent_or_self(skia_repo, revs[0])
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
        if has_fresh_up:
            active_up_ref = upstream_ref
        revs = resolve_cl_cherry_pick_revs(
            stage_wt,
            tip_sha,
            active_up_ref,
            base_ref_override,
        )
        candidate_base_sha = _resolve_parent_or_self(stage_wt, revs[0])
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
                default_up,
                base_ref_override,
            )
        candidate_base_sha = _resolve_parent_or_self(skia_repo, revs[0])
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

    cand_paths = [*changed_or_added, *deleted]
    stack_base_skew = detect_stack_base_skew(
        stage_wt,
        candidate_base_sha,
        g3_skia_revision,
        cand_paths,
    )
    # Also catch stale candidates when CitC has not been synced yet (g3_skia_revision lags main)
    if (
        stack_base_skew is None or stack_base_skew.get("severity") != "warning"
    ) and active_up_ref != g3_skia_revision:
        up_chk = _run(
            ["git", "rev-parse", "--verify", f"{active_up_ref}^{{commit}}"],
            cwd=stage_wt,
            check=False,
        )
        if up_chk.returncode == 0 and up_chk.stdout.strip():
            up_sha = up_chk.stdout.strip()
            # Measure divergence from merge-base(source_commit_sha, up_sha) so commits already
            # landed on main (or parent CLs in A..B / --base-ref) do not conflict with themselves
            up_base_sha = candidate_base_sha
            if source_commit_sha:
                mb_up = _run(
                    ["git", "merge-base", source_commit_sha, up_sha],
                    cwd=stage_wt,
                    check=False,
                )
                if mb_up.returncode == 0 and mb_up.stdout.strip():
                    up_base_sha = mb_up.stdout.strip()
            up_skew = detect_stack_base_skew(
                stage_wt,
                up_base_sha,
                up_sha,
                cand_paths,
                target_label=active_up_ref,
            )
            if up_skew is not None and up_skew.get("direction") in ("behind", "diverged"):
                stack_base_skew = up_skew

    if stack_base_skew is not None:
        tag = (
            "STACK-BASE SKEW WARNING"
            if stack_base_skew.get("severity") == "warning"
            else "STACK-BASE SKEW NOTE"
        )
        print(f"[{tag}] {ref_spec}: {stack_base_skew['warning']}", file=sys.stderr, flush=True)
        if stack_base_skew.get("severity") == "warning" and not allow_stack_base_skew:
            raise RuntimeError(
                f"Stack-base skew for '{ref_spec}': {stack_base_skew['warning']} "
                f"(pass --allow-stack-base-skew to run anyway)"
            )

    return {
        "source_commit_sha": source_commit_sha,
        "candidate_base_sha": candidate_base_sha,
        "stack_base_skew": stack_base_skew,
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


# Serialize CitC client mutations across parallel sessions without unlinking the lock inode
@contextlib.contextmanager
def acquire_citc_lock(
    citc_client: str,
    *,
    wait_sec: float = 0.0,
    lock_dir: pathlib.Path | None = None,
) -> Iterator[pathlib.Path]:
    base_dir = lock_dir or pathlib.Path(tempfile.gettempdir())
    enforce_citc_hygiene(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    safe_client = re.sub(r"[^A-Za-z0-9_.-]", "_", citc_client)
    lock_path = base_dir / f"jetski_citc_{getpass.getuser()}_{safe_client}.lock"
    fh = lock_path.open("a+", encoding="utf-8")
    acquired = False
    deadline = time.monotonic() + max(0.0, wait_sec)
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    fh.seek(0)
                    holder = fh.read().strip() or "unknown holder"
                    raise RuntimeError(
                        f"CitC client '{citc_client}' is locked ({holder}); "
                        f"pass --wait-lock-sec or another --citc-client"
                    ) from exc
                time.sleep(min(0.25, remaining))
        fh.seek(0)
        fh.truncate()
        fh.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "citc_client": citc_client,
                    "started_utc": datetime.now(timezone.utc).isoformat(),
                }
            )
            + "\n"
        )
        fh.flush()
        yield lock_path
    finally:
        if acquired:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def compute_baseline_cache_key(
    citc_base_cl: int,
    g3_skia_rev: str,
    tree_sha: str,
    targets: list[str],
) -> str:
    targets_sha = hashlib.sha256("\n".join(targets).encode("utf-8")).hexdigest()[:12]
    return f"baseline_{citc_base_cl}_{g3_skia_rev[:12]}_{tree_sha}_{targets_sha}"


def _is_cacheable_baseline_leg(attestation: dict[str, Any], summary: dict[str, Any]) -> bool:
    if attestation.get("ref_spec") != "G3_HEAD":
        return False
    if attestation.get("citc_base_cl") is None or attestation.get("citc_pre_dirty_files"):
        return False
    if not attestation.get("citc_restored_clean") or attestation.get("flaky_attempts") != 1:
        return False
    if attestation.get("blaze_rc") not in (0, 3):
        return False
    counts = summary.get("counts") or {}
    if counts.get("failed_to_build", 0) or counts.get("analysis_failure", 0):
        return False
    if "REMOTE_ERROR" in (summary.get("statuses") or {}).values():
        return False
    issues, _ = pg3._evaluate_attestation(
        [("baseline", {"attestation": attestation, "summary": summary})]
    )
    return not issues


def _try_restore_baseline_cache(
    cache_entry_dir: pathlib.Path,
    leg_dir: pathlib.Path,
    *,
    label: str,
    citc_client: str,
    stale_warning: str | None,
) -> bool:
    req_files = ("attestation.json", "summary.json", "bep.json")
    if not all((cache_entry_dir / name).is_file() for name in req_files):
        return False
    try:
        att = json.loads((cache_entry_dir / "attestation.json").read_text(encoding="utf-8"))
        sm = json.loads((cache_entry_dir / "summary.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not _is_cacheable_baseline_leg(att, sm):
        return False
    shutil.copy2(cache_entry_dir / "bep.json", leg_dir / "bep.json")
    if (cache_entry_dir / "blaze.log").is_file():
        shutil.copy2(cache_entry_dir / "blaze.log", leg_dir / "blaze.log")
    sm["bep_path"] = str(leg_dir / "bep.json")
    att = {
        **att,
        "label": label,
        "citc_client": citc_client,
        "targets_stale_warning": stale_warning,
        "baseline_cache_hit": True,
        "baseline_cache_key": cache_entry_dir.name,
    }
    (leg_dir / "attestation.json").write_text(
        json.dumps(att, indent=2) + "\n", encoding="utf-8"
    )
    (leg_dir / "summary.json").write_text(
        json.dumps(sm, indent=2) + "\n", encoding="utf-8"
    )
    return True


def _prune_baseline_cache(
    cache_root: pathlib.Path, max_entries: int = MAX_BASELINE_CACHE_ENTRIES
) -> None:
    if not cache_root.is_dir():
        return
    entries = [
        p
        for p in cache_root.iterdir()
        if p.is_dir() and p.name.startswith("baseline_")
    ]
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for old in entries[max_entries:]:
        shutil.rmtree(old, ignore_errors=True)


def _save_baseline_cache(
    cache_root: pathlib.Path,
    cache_key: str,
    leg_dir: pathlib.Path,
    attestation: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    if not _is_cacheable_baseline_leg(attestation, summary):
        return
    enforce_citc_hygiene(cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    dst_dir = cache_root / cache_key
    tmp_dir = cache_root / f".tmp_{cache_key}_{os.getpid()}"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        for name in ("attestation.json", "summary.json", "bep.json", "targets.txt", "blaze.log"):
            src = leg_dir / name
            if src.is_file():
                shutil.copy2(src, tmp_dir / name)
        if dst_dir.exists():
            shutil.rmtree(dst_dir, ignore_errors=True)
        os.replace(tmp_dir, dst_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    _prune_baseline_cache(cache_root)


# Apply pre-staged candidate files forwarded from macOS over SSH onto google3/third_party/skia/HEAD
def apply_pre_staged_candidate(
    staged_dir: pathlib.Path,
    g3_root: pathlib.Path,
    g3_skia_rev: str,
) -> dict[str, Any]:
    meta = json.loads((staged_dir / "_staged_meta.json").read_text(encoding="utf-8"))
    staged_rev = str(meta.get("g3_skia_revision") or "")
    if staged_rev != g3_skia_rev:
        raise RuntimeError(
            f"G3 Skia revision drifted during staging "
            f"({staged_rev[:12]} -> {g3_skia_rev[:12]})"
        )
    sync_info = ar.sync_skia_tree_to_g3(
        staged_dir / "files",
        g3_root,
        list(meta.get("changed_or_added") or []),
        list(meta.get("deleted") or []),
    )
    return {
        "orig_ref_spec": str(meta["orig_ref_spec"]),
        "source_commit_sha": meta.get("source_commit_sha"),
        "candidate_base_sha": meta.get("candidate_base_sha"),
        "stack_base_skew": meta.get("stack_base_skew"),
        "dirty_files": list(meta.get("dirty_files") or []),
        "sync_info": sync_info,
    }


def resolve_default_ssh_host() -> str:
    env_host = os.environ.get("GOOGLE3_PARAGRAPH_SSH_HOST", "").strip()
    if env_host:
        return env_host
    cfg_file = pathlib.Path.home() / ".config/google3-test-paragraph/ssh_host"
    if cfg_file.is_file():
        saved = cfg_file.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    return f"{getpass.getuser()}-for-jetski.c.googlers.com"


# Reuse one multiplexed SSH control socket for 8h to avoid repeated FIDO2 security key touches
def _ssh_base_cmd(ssh_host: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPath=~/.ssh/cm-%C",
        "-o",
        "ControlPersist=8h",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=20",
        ssh_host,
    ]


def _raise_ssh_error(ssh_host: str, stderr_text: str, rc: int) -> None:
    detail = stderr_text.strip()
    if "agent refused operation" in detail:
        raise RuntimeError(
            f"SSH signing to {ssh_host} timed out waiting for hardware security key touch "
            f"(touch your security key when it blinks; ControlPersist=8h caches the session):\n"
            f"{detail}"
        )
    raise RuntimeError(f"Remote execution on {ssh_host} failed (rc={rc}):\n{detail}")


def _tar_add_bytes(tf: tarfile.TarFile, arcname: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mtime = int(time.time())
    tf.addfile(info, io.BytesIO(data))


# Validate tar member paths so Python 3.9 on macOS extracts safely without filter='data'
def _safe_extract_tar_gz(raw_bytes: bytes, dest_dir: pathlib.Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:gz") as tf:
        for member in tf.getmembers():
            rel = member.name.lstrip("./")
            if not rel:
                continue
            parts = pathlib.PurePosixPath(rel).parts
            if member.name.startswith("/") or ".." in parts:
                raise RuntimeError(f"Unsafe path in remote archive: {member.name}")
            target = dest_dir / rel
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tf.extractfile(member)
                if extracted is not None:
                    target.write_bytes(extracted.read())


def _query_remote_g3_skia_rev(
    ssh_host: str,
    citc_client: str | None,
    create_citc: bool,
) -> str:
    py_code = (
        "import getpass, pathlib, re, subprocess, sys\n"
        "user = getpass.getuser()\n"
        "base = pathlib.Path('/google/src/cloud') / user\n"
        f"cands = {[citc_client] if citc_client else ['skia_g3_ab', 'skia_g3_pilot']!r}\n"
        "root = next((base / n for n in cands if n and (base / n / 'google3').is_dir()), None)\n"
        f"if root is None and {bool(create_citc)!r}:\n"
        f"    t = {(citc_client or 'skia_g3_ab')!r}\n"
        "    subprocess.run(['g4', 'citc', t], check=True, timeout=180)\n"
        "    if (base / t / 'google3').is_dir(): root = base / t\n"
        "if root is None: raise SystemExit('CitC client not found on remote host')\n"
        "txt = (root / 'google3/third_party/skia/HEAD/METADATA').read_text(encoding='utf-8')\n"
        "m = re.search(r'version:\\s*\"([0-9a-f]{40})\"', txt, re.I)\n"
        "if not m: raise SystemExit('Could not parse METADATA git SHA')\n"
        "print(m.group(1).lower())\n"
    )
    proc = subprocess.run(
        [*_ssh_base_cmd(ssh_host), f"python3 -c {shlex.quote(py_code)}"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        _raise_ssh_error(
            ssh_host,
            proc.stderr.decode("utf-8", errors="replace"),
            proc.returncode,
        )
    rev = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()[-1].strip()
    if not re.fullmatch(r"[0-9a-f]{40}", rev):
        raise RuntimeError(f"Unexpected remote METADATA revision output: {rev!r}")
    return rev


# Stage a candidate spec into a directory bundle so pre-flight errors surface before baseline runs
def _stage_candidate_bundle_dir(
    ref_spec: str,
    skia_repo: pathlib.Path,
    g3_skia_rev: str,
    stage_wt: pathlib.Path,
    bundle_dir: pathlib.Path,
    base_ref_override: str | None,
    *,
    allow_stack_base_skew: bool = False,
) -> None:
    try:
        staged = stage_candidate_in_worktree(
            ref_spec,
            skia_repo,
            g3_skia_rev,
            stage_wt,
            base_ref_override,
            allow_stack_base_skew=allow_stack_base_skew,
        )
        bundle_dir.mkdir(parents=True, exist_ok=True)
        files_root = bundle_dir / "files"
        for rel in staged["changed_or_added"]:
            if ar.should_include_file(rel):
                dst = files_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(stage_wt / rel, dst)
        meta = {
            "orig_ref_spec": ref_spec,
            "g3_skia_revision": g3_skia_rev,
            "source_commit_sha": staged["source_commit_sha"],
            "candidate_base_sha": staged["candidate_base_sha"],
            "stack_base_skew": staged["stack_base_skew"],
            "dirty_files": staged["dirty_files"],
            "changed_or_added": staged["changed_or_added"],
            "deleted": staged["deleted"],
        }
        (bundle_dir / "_staged_meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        if stage_wt.exists():
            _run(
                ["git", "worktree", "remove", "--force", str(stage_wt)],
                cwd=skia_repo,
                check=False,
            )
            shutil.rmtree(stage_wt, ignore_errors=True)


def run_via_ssh(
    ssh_host: str,
    args: argparse.Namespace,
    out_dir: pathlib.Path,
    parsed_specs: list[tuple[str, str]],
) -> int:
    local_stage_specs = [
        (lbl, ref)
        for lbl, ref in parsed_specs
        if ref != "G3_HEAD"
        and not ref.startswith(("cl:", "staged:"))
        and not (
            args.resume
            and (out_dir / lbl / "attestation.json").is_file()
            and (out_dir / lbl / "summary.json").is_file()
        )
    ]
    staged_payloads: dict[str, tuple[dict[str, Any], dict[str, bytes]]] = {}
    if local_stage_specs:
        skia_repo = resolve_skia_repo(args.skia_repo)
        g3_skia_rev = _query_remote_g3_skia_rev(ssh_host, args.citc_client, args.create_citc)
        for lbl, ref in local_stage_specs:
            safe_lbl = re.sub(r"[^A-Za-z0-9_.-]", "_", lbl)
            stage_wt = out_dir / f"_mac_stage_{safe_lbl}"
            try:
                staged = stage_candidate_in_worktree(
                    ref,
                    skia_repo,
                    g3_skia_rev,
                    stage_wt,
                    args.base_ref,
                    allow_stack_base_skew=args.allow_stack_base_skew,
                )
                file_blobs: dict[str, bytes] = {}
                for rel in staged["changed_or_added"]:
                    if ar.should_include_file(rel):
                        file_blobs[rel] = (stage_wt / rel).read_bytes()
                meta = {
                    "orig_ref_spec": ref,
                    "g3_skia_revision": g3_skia_rev,
                    "source_commit_sha": staged["source_commit_sha"],
                    "candidate_base_sha": staged["candidate_base_sha"],
                    "stack_base_skew": staged["stack_base_skew"],
                    "dirty_files": staged["dirty_files"],
                    "changed_or_added": staged["changed_or_added"],
                    "deleted": staged["deleted"],
                }
                staged_payloads[lbl] = (meta, file_blobs)
            finally:
                if stage_wt.exists():
                    _run(
                        ["git", "worktree", "remove", "--force", str(stage_wt)],
                        cwd=skia_repo,
                        check=False,
                    )
                    shutil.rmtree(stage_wt, ignore_errors=True)

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tf:
        for script_name in (
            "autoroller_rewrites.py",
            "parse_g3_paragraph_tests.py",
            "run_g3_paragraph_tests.py",
        ):
            src_p = SCRIPT_DIR / script_name
            _tar_add_bytes(tf, f"_runner/scripts/{script_name}", src_p.read_bytes())
        for data_name in ("fast_targets.txt", "slow_targets.txt", "targets_manifest.json"):
            data_p = DATA_DIR / data_name
            if data_p.is_file():
                _tar_add_bytes(tf, f"_runner/data/{data_name}", data_p.read_bytes())
        if args.targets_file is not None:
            _tar_add_bytes(tf, "_runner/custom_targets.txt", args.targets_file.read_bytes())
        if args.resume:
            for lbl, _ in parsed_specs:
                for fn in ("attestation.json", "summary.json", "targets.txt", "blaze.log"):
                    existing = out_dir / lbl / fn
                    if existing.is_file():
                        _tar_add_bytes(tf, f"out/{lbl}/{fn}", existing.read_bytes())
        for lbl, (meta, blobs) in staged_payloads.items():
            safe_lbl = re.sub(r"[^A-Za-z0-9_.-]", "_", lbl)
            _tar_add_bytes(
                tf,
                f"_staged/{safe_lbl}/_staged_meta.json",
                (json.dumps(meta, indent=2) + "\n").encode("utf-8"),
            )
            for rel, raw in blobs.items():
                _tar_add_bytes(tf, f"_staged/{safe_lbl}/files/{rel}", raw)

    safe_out = re.sub(r"[^A-Za-z0-9_.-]", "_", out_dir.name or "g3_paragraph_tests")
    mirror_name = safe_out if "g3" in safe_out.lower() else f"g3_{safe_out}"

    remote_args: list[str] = [
        "--no-ssh",
        "--quiet-stdout",
        "--remote-host-label",
        ssh_host,
        "--tier",
        args.tier,
        "--wait-lock-sec",
        str(args.wait_lock_sec),
        "--flaky-attempts",
        str(args.flaky_attempts),
        "--max-targets-age-days",
        str(args.max_targets_age_days),
    ]
    if args.citc_client:
        remote_args.extend(["--citc-client", args.citc_client])
    if args.create_citc:
        remote_args.append("--create-citc")
    if args.allow_stale_targets:
        remote_args.append("--allow-stale-targets")
    if args.allow_dirty_citc:
        remote_args.append("--allow-dirty-citc")
    if args.allow_stack_base_skew:
        remote_args.append("--allow-stack-base-skew")
    if args.base_ref:
        remote_args.extend(["--base-ref", args.base_ref])
    if args.no_baseline_cache:
        remote_args.append("--no-baseline-cache")
    if args.resume:
        remote_args.append("--resume")

    quoted_flags = " ".join(shlex.quote(a) for a in remote_args)
    spec_tokens: list[str] = []
    for lbl, ref in parsed_specs:
        if lbl in staged_payloads:
            safe_lbl = re.sub(r"[^A-Za-z0-9_.-]", "_", lbl)
            spec_tokens.append(f"{shlex.quote(lbl + '=staged:')}\"$WORK/_staged/{safe_lbl}\"")
        else:
            spec_tokens.append(shlex.quote(f"{lbl}={ref}"))
    extra_tf = ' --targets-file "$WORK/_runner/custom_targets.txt"' if args.targets_file else ""
    joined_specs = " ".join(spec_tokens)

    # Redirect runner output to stderr so stdout carries only the final gzipped result archive
    remote_sh = (
        "set -eu; "
        f"WORK=\"/tmp/g3_remote_{safe_out}_$$\"; "
        f"MIRROR=\"/tmp/{mirror_name}\"; "
        "trap 'rm -rf \"$WORK\" \"$MIRROR.tmp_$$\"' EXIT; "
        "mkdir -p \"$WORK\" \"$WORK/out\"; "
        "tar -xzf - -C \"$WORK\" >&2; "
        "python3 \"$WORK/_runner/scripts/run_g3_paragraph_tests.py\" "
        f"--out \"$WORK/out\" {quoted_flags}{extra_tf} {joined_specs} >&2; "
        "mkdir -p \"$MIRROR.tmp_$$\"; "
        "tar -cf - --exclude='*/bep.json' --exclude='_skia_stage_*' --exclude='_preflight_staged' "
        "-C \"$WORK/out\" . | tar -xf - -C \"$MIRROR.tmp_$$\" >&2; "
        "rm -rf \"$MIRROR\"; "
        "mv \"$MIRROR.tmp_$$\" \"$MIRROR\"; "
        "tar -czf - --exclude='*/bep.json' --exclude='_skia_stage_*' --exclude='_preflight_staged' "
        "-C \"$WORK/out\" ."
    )

    proc = subprocess.run(
        [*_ssh_base_cmd(ssh_host), remote_sh],
        input=tar_buf.getvalue(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        _raise_ssh_error(
            ssh_host,
            proc.stderr.decode("utf-8", errors="replace"),
            proc.returncode,
        )

    _safe_extract_tar_gz(proc.stdout, out_dir)
    compare_path = out_dir / "compare.md"
    if not compare_path.is_file():
        raise RuntimeError(f"Remote run on {ssh_host} did not produce compare.md in {out_dir}")
    if not args.quiet_stdout:
        sys.stdout.write(compare_path.read_text(encoding="utf-8"))
    return 0


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
    use_baseline_cache: bool = True,
    baseline_cache_dir: pathlib.Path = DEFAULT_BASELINE_CACHE_DIR,
    remote_host_label: str | None = None,
    allow_stack_base_skew: bool = False,
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
    effective_ref_spec = ref_spec
    source_commit_sha: str | None = None
    candidate_base_sha: str | None = None
    stack_base_skew: dict[str, Any] | None = None
    skia_dirty: list[str] = []
    restored_clean = True
    cache_eligible = (
        ref_spec == "G3_HEAD"
        and use_baseline_cache
        and citc_base_cl is not None
        and not pre_dirty
        and flaky_attempts == 1
        and stale_warning is None
    )
    cache_key: str | None = None

    try:
        if ref_spec == "G3_HEAD":
            restore_citc_skia_head(g3_root, [], pre_dirty)
            if cache_eligible and citc_base_cl is not None:
                clean_tree_sha = hash_g3_skia_tree(g3_root, [])
                cache_key = compute_baseline_cache_key(
                    citc_base_cl, g3_skia_rev, clean_tree_sha, targets
                )
                if _try_restore_baseline_cache(
                    baseline_cache_dir / cache_key,
                    leg_dir,
                    label=label,
                    citc_client=citc_client,
                    stale_warning=stale_warning,
                ):
                    if remote_host_label:
                        att_p = leg_dir / "attestation.json"
                        att_obj = json.loads(att_p.read_text(encoding="utf-8"))
                        att_obj["remote_ssh_host"] = remote_host_label
                        att_p.write_text(json.dumps(att_obj, indent=2) + "\n", encoding="utf-8")
                    return
        elif ref_spec.startswith("staged:"):
            applied = apply_pre_staged_candidate(
                pathlib.Path(ref_spec[len("staged:") :]),
                g3_root,
                g3_skia_rev,
            )
            effective_ref_spec = applied["orig_ref_spec"]
            source_commit_sha = applied["source_commit_sha"]
            candidate_base_sha = applied["candidate_base_sha"]
            stack_base_skew = applied["stack_base_skew"]
            skia_dirty = applied["dirty_files"]
            sync_info = applied["sync_info"]
        else:
            if skia_repo is None:
                raise RuntimeError(f"skia_repo required for non-G3_HEAD spec '{ref_spec}'")
            staged = stage_candidate_in_worktree(
                ref_spec,
                skia_repo,
                g3_skia_rev,
                stage_wt,
                base_ref_override,
                allow_stack_base_skew=allow_stack_base_skew,
            )
            source_commit_sha = staged["source_commit_sha"]
            candidate_base_sha = staged["candidate_base_sha"]
            stack_base_skew = staged["stack_base_skew"]
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
        "ref_spec": effective_ref_spec,
        "skia_commit_sha": source_commit_sha,
        "candidate_base_sha": candidate_base_sha,
        "stack_base_skew": stack_base_skew,
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
        "baseline_cache_hit": False,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    if remote_host_label:
        attestation["remote_ssh_host"] = remote_host_label

    (leg_dir / "attestation.json").write_text(
        json.dumps(attestation, indent=2) + "\n", encoding="utf-8"
    )
    (leg_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    if cache_eligible and cache_key is not None:
        _save_baseline_cache(baseline_cache_dir, cache_key, leg_dir, attestation, summary)


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
    parser.add_argument(
        "--wait-lock-sec",
        type=float,
        default=0.0,
        help="Seconds to wait for exclusive CitC client lock (default: 0 = fail immediately)",
    )
    parser.add_argument("--skia-repo", type=pathlib.Path, default=None, help="Local Skia git repo")
    parser.add_argument("--base-ref", default=None, help="Override Skia base ref for diffing")
    parser.add_argument("--flaky-attempts", type=int, default=1)
    parser.add_argument("--max-targets-age-days", type=int, default=30)
    parser.add_argument("--allow-stale-targets", action="store_true")
    parser.add_argument("--allow-dirty-citc", action="store_true")
    parser.add_argument(
        "--allow-stack-base-skew",
        action="store_true",
        help="Proceed even when a candidate sits on a stale Skia base with overlapping edits",
    )
    parser.add_argument("--no-baseline-cache", action="store_true")
    parser.add_argument("--baseline-cache-dir", type=pathlib.Path, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--ssh-host",
        default=None,
        help="Remote Linux Cloudtop host when /google/src/cloud is not mounted locally",
    )
    parser.add_argument(
        "--no-ssh",
        action="store_true",
        help="Disable automatic SSH forwarding to Cloudtop when CitC is absent locally",
    )
    parser.add_argument("--quiet-stdout", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--remote-host-label", default=None, help=argparse.SUPPRESS)
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

    local_citc_mount = pathlib.Path("/google/src/cloud") / getpass.getuser()
    if args.ssh_host or (not local_citc_mount.exists() and not args.no_ssh):
        ssh_host = args.ssh_host or resolve_default_ssh_host()
        return run_via_ssh(ssh_host, args, out_dir, parsed_specs)

    cache_dir = (
        args.baseline_cache_dir.expanduser()
        if args.baseline_cache_dir is not None
        else DEFAULT_BASELINE_CACHE_DIR
    )
    enforce_citc_hygiene(cache_dir)

    tier_name, targets, stale_warning = load_tier_targets(
        args.tier,
        args.targets_file,
        args.max_targets_age_days,
        args.allow_stale_targets,
    )
    citc_client, citc_root = resolve_citc_root(args.citc_client, args.create_citc)
    g3_root = citc_root / "google3"
    needs_candidate_staging = any(ref != "G3_HEAD" for _, ref in parsed_specs)
    needs_skia_repo = any(
        ref != "G3_HEAD" and not ref.startswith("staged:") for _, ref in parsed_specs
    )
    skia_repo = resolve_skia_repo(args.skia_repo) if needs_skia_repo else None

    with acquire_citc_lock(citc_client, wait_sec=args.wait_lock_sec):
        pre_dirty = list_citc_dirty_files(g3_root)
        check_citc_dirty_hygiene(
            citc_client,
            pre_dirty,
            args.allow_dirty_citc,
            needs_candidate_staging,
        )

        # Pre-stage all candidate specs before running baseline=G3_HEAD so conflicts, BUILD.bazel
        # drift, or stale stack bases abort in seconds instead of after a 12-minute baseline run
        preflight_dir = out_dir / "_preflight_staged"
        shutil.rmtree(preflight_dir, ignore_errors=True)
        effective_specs = list(parsed_specs)
        try:
            to_prestage = [
                (idx, lbl, ref)
                for idx, (lbl, ref) in enumerate(parsed_specs)
                if ref != "G3_HEAD"
                and not ref.startswith("staged:")
                and not (
                    args.resume
                    and (out_dir / lbl / "attestation.json").exists()
                    and (out_dir / lbl / "summary.json").exists()
                )
            ]
            if to_prestage:
                if skia_repo is None:
                    raise RuntimeError("skia_repo required for candidate pre-flight staging")
                g3_skia_rev = ar.read_g3_skia_revision(g3_root / ar.G3_SKIA_REL / "METADATA")
                for idx, lbl, ref in to_prestage:
                    safe_lbl = re.sub(r"[^A-Za-z0-9_.-]", "_", lbl)
                    bundle_dir = preflight_dir / f"{idx}_{safe_lbl}"
                    stage_wt = out_dir / f"_skia_stage_{idx}_{safe_lbl}"
                    _stage_candidate_bundle_dir(
                        ref,
                        skia_repo,
                        g3_skia_rev,
                        stage_wt,
                        bundle_dir,
                        args.base_ref,
                        allow_stack_base_skew=args.allow_stack_base_skew,
                    )
                    effective_specs[idx] = (lbl, f"staged:{bundle_dir}")

            for lbl, ref in effective_specs:
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
                    use_baseline_cache=not args.no_baseline_cache,
                    baseline_cache_dir=cache_dir,
                    remote_host_label=args.remote_host_label,
                    allow_stack_base_skew=args.allow_stack_base_skew,
                )
        finally:
            shutil.rmtree(preflight_dir, ignore_errors=True)

    runs = [(lbl, pg3.collect_run_data(str(out_dir / lbl), None)) for lbl, _ in parsed_specs]
    md = pg3.format_comparison_markdown(runs)
    compare_path = out_dir / "compare.md"
    compare_path.write_text(md, encoding="utf-8")
    if not args.quiet_stdout:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
