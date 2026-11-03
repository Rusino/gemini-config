#!/usr/bin/env python3
"""Auto-commit, push, post-push restart, and Mac clone sync for personal repos."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

from pre_tool_guard import (
    EDIT_TOOLS,
    _active_foreign_file_leases,
    _analyze_git_add_args,
    _extract_cmd_target_dirs,
    _git_head_sha,
    _parallel_guard_paths,
    _parse_git_subcommand_invocations,
    _read_json_file,
    _record_parallel_guard_block,
    _resolve_git_dirs,
    _sync_repo_commits,
    _write_json_atomic,
    get_tool_target_file,
    inspect_unpushed_commit_owners,
)
import pre_tool_guard

DEFAULT_PERSONAL_REPOS = (
    "~/.gemini/config",
    "~/.gemini/jetski/scratch/skia_gardener",
)


# Run external command non-interactively so timeouts or missing binaries never leak tracebacks
def _run_cmd(
    cmd: list[str], timeout: float | None = None, cwd: str | None = None
) -> subprocess.CompletedProcess:
  env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
  try:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
  except (subprocess.TimeoutExpired, OSError) as exc:
    return subprocess.CompletedProcess(cmd, 124, "", str(exc))


def personal_repos() -> list[str]:
  if "JETSKI_AUTO_SHIP_REPOS" in os.environ:
    raw = os.environ["JETSKI_AUTO_SHIP_REPOS"].split(":")
  else:
    raw = list(DEFAULT_PERSONAL_REPOS)
  out: list[str] = []
  for entry in raw:
    clean = entry.strip()
    if clean:
      rp = os.path.realpath(os.path.expanduser(clean))
      if rp not in out:
        out.append(rp)
  return out


# Resolve primary ~/.gemini/config checkout with optional test override
def _config_repo_root() -> str:
  raw = os.environ.get("JETSKI_CONFIG_REPO") or "~/.gemini/config"
  return os.path.realpath(os.path.expanduser(raw.strip()))


# Resolve primary skia_gardener checkout with optional test override
def _gardener_repo_root() -> str:
  raw = (
      os.environ.get("JETSKI_GARDENER_REPO")
      or "~/.gemini/jetski/scratch/skia_gardener"
  )
  return os.path.realpath(os.path.expanduser(raw.strip()))


# Include explicit env overrides alongside personal_repos for worktree discovery
def _configured_base_repos() -> list[str]:
  bases = list(personal_repos())
  for env_key, fn in (
      ("JETSKI_CONFIG_REPO", _config_repo_root),
      ("JETSKI_GARDENER_REPO", _gardener_repo_root),
  ):
    if env_key in os.environ:
      root = fn()
      if os.path.isdir(root) and root not in bases:
        bases.append(root)
  return bases


# Match secondary git worktrees sharing git_common_dir with primary_repo
def _is_linked_worktree_of(path: str, primary_repo: str) -> bool:
  if not path or not primary_repo:
    return False
  rp = os.path.realpath(os.path.expanduser(path.strip()))
  primary_rp = os.path.realpath(os.path.expanduser(primary_repo.strip()))
  if rp == primary_rp or not os.path.isdir(rp) or not os.path.isdir(primary_rp):
    return False
  ginfo = _resolve_git_dirs(rp)
  pinfo = _resolve_git_dirs(primary_rp)
  if not ginfo or not pinfo or ginfo[0] != rp:
    return False
  return ginfo[2] == pinfo[2]


# Treat both primary personal checkouts and their linked worktrees as personal repos
def is_personal_repo(path: str) -> bool:
  if not path:
    return False
  rp = os.path.realpath(os.path.expanduser(path.strip()))
  bases = _configured_base_repos()
  if rp in set(bases):
    return True
  return any(_is_linked_worktree_of(rp, b) for b in bases)


# Primary personal checkouts auto-ship on main; linked worktrees also allow detached HEAD
def is_auto_ship_branch(path: str, branch: str = "") -> bool:
  if not path:
    return False
  rp = os.path.realpath(os.path.expanduser(path.strip()))
  bases = _configured_base_repos()
  br = branch if branch else _repo_branch(rp)
  if rp in set(bases):
    return br == "main"
  if any(_is_linked_worktree_of(rp, b) for b in bases):
    return br in ("main", "HEAD", "")
  return False


def _unquote(value) -> str:
  return str(value or "").strip().strip('"').strip("'")


# Walk transcript and chain state for files explicitly written or staged by this chat
def _collect_strict_touched(
    conv_id: str, transcript_path: str
) -> tuple[set[str], set[str], set[str]]:
  import roadmap

  all_touched: set[str] = set()
  turn_touched: set[str] = set()
  turn_commit_repos: set[str] = set()
  steps: list[dict] = []
  if transcript_path and os.path.isfile(transcript_path):
    try:
      with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
          line = line.strip()
          if not line:
            continue
          try:
            obj = json.loads(line)
          except Exception:
            continue
          if obj.get("step_index") is not None:
            steps.append(obj)
    except Exception:
      pass
  steps.sort(key=lambda s: int(s["step_index"]))

  last_user_idx = -1
  for st in steps:
    if st.get("type") == "USER_INPUT":
      last_user_idx = int(st["step_index"])

  cand_dirs: list[str] = []
  for st in steps:
    if st.get("type") != "PLANNER_RESPONSE":
      continue
    in_turn = int(st["step_index"]) > last_user_idx
    for tc in st.get("tool_calls") or []:
      tname = tc.get("name", "")
      targs = tc.get("args") or {}
      if tname in EDIT_TOOLS:
        tf = _unquote(get_tool_target_file(targs))
        if tf and not roadmap._is_ignored_internal_path(tf):
          rp = os.path.realpath(os.path.expanduser(tf))
          all_touched.add(rp)
          cand_dirs.append(os.path.dirname(rp))
          if in_turn:
            turn_touched.add(rp)
      elif tname == "run_command":
        cmd = str(targs.get("CommandLine", "")).strip()
        if not cmd:
          continue
        for d in _extract_cmd_target_dirs(cmd, []):
          if d and os.path.isdir(d) and not roadmap._is_ignored_internal_path(d):
            cand_dirs.append(os.path.realpath(d))
        # Explicit git add/rm/mv pathspecs cover deletions and renames with no edit tool call
        for subcmd in ("add", "rm", "mv"):
          for sub_args in _parse_git_subcommand_invocations(cmd, subcmd):
            is_broad, paths = _analyze_git_add_args(sub_args)
            if is_broad:
              continue
            for p in paths:
              resolved = ""
              if os.path.isabs(p):
                resolved = os.path.realpath(p)
              else:
                for cd in reversed(cand_dirs):
                  ginfo = _resolve_git_dirs(cd)
                  base = ginfo[0] if ginfo else cd
                  cand_p = os.path.realpath(os.path.join(base, p))
                  if os.path.exists(cand_p) or ginfo:
                    resolved = cand_p
                    break
              if resolved and not roadmap._is_ignored_internal_path(resolved):
                all_touched.add(resolved)
                if in_turn:
                  turn_touched.add(resolved)
        if in_turn and _parse_git_subcommand_invocations(cmd, "commit"):
          for d in _extract_cmd_target_dirs(cmd, []):
            ginfo = _resolve_git_dirs(d)
            if ginfo:
              turn_commit_repos.add(ginfo[0])

  if conv_id:
    try:
      pid = roadmap.project_id_for(conv_id)
      if pid:
        sf = roadmap.state_path(pid, conv_id)
        if os.path.isfile(sf):
          with open(sf, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
          # A state file can have both a manual section and an auto-appended snapshot section
          hdr = "### Files Modified by Tools in This Conversation"
          pos = 0
          while True:
            mod_idx = txt.find(hdr, pos)
            if mod_idx < 0:
              break
            body_start = mod_idx + len(hdr)
            next_h = re.search(r"\n#{1,3}\s+", txt[body_start:])
            mod_end = body_start + next_h.start() if next_h else len(txt)
            # Exclude `]` so Markdown labels like `[file:///path](file:///path)` stop at `]`
            for m in re.findall(r"file://(/[^\s`\"')\]#]+)", txt[mod_idx:mod_end]):
              if not roadmap._is_ignored_internal_path(m):
                all_touched.add(os.path.realpath(m))
            pos = mod_end
    except Exception:
      pass

  return all_touched, turn_touched, turn_commit_repos


def _dirty_repo_rels(top_level: str) -> list[str]:
  import roadmap

  out: list[str] = []
  res = _run_cmd(["git", "-C", top_level, "status", "--porcelain", "-uall"], timeout=2.0)
  if res.returncode != 0:
    return out
  for ln in res.stdout.splitlines():
    if len(ln) <= 3:
      continue
    for part in ln[3:].strip().split(" -> "):
      rel = part.strip().strip('"')
      if not rel:
        continue
      if roadmap._is_ignored_internal_path(os.path.join(top_level, rel)):
        continue
      if rel not in out:
        out.append(rel)
  return out


def _repo_branch(top_level: str) -> str:
  res = _run_cmd(
      ["git", "-C", top_level, "symbolic-ref", "--short", "-q", "HEAD"], timeout=1.0
  )
  return res.stdout.strip() if res.returncode == 0 else ""


# Build per-repo auto-ship entries for dirty owned files and unpushed commits
def plan_ship(
    conv_id: str,
    transcript_path: str = "",
    extra_repos: list[str] | None = None,
) -> list[dict]:
  import roadmap

  tpath = roadmap._find_transcript_path(conv_id, transcript_path)
  all_touched, turn_touched, turn_commit_repos = _collect_strict_touched(
      conv_id, tpath
  )
  base_repos = _configured_base_repos()
  base_common_dirs: set[str] = set()
  for b in base_repos:
    if os.path.isdir(b):
      binfo = _resolve_git_dirs(b)
      if binfo and binfo[0] == b:
        base_common_dirs.add(binfo[2])

  # Discover linked worktrees of personal repos from touched paths or explicit --repo
  explicit_set = {
      os.path.realpath(os.path.expanduser(r.strip()))
      for r in (extra_repos or [])
      if r and r.strip()
  }
  candidate_repos = list(base_repos)
  probe_dirs = (
      {os.path.dirname(p) for p in all_touched}
      | set(turn_commit_repos)
      | explicit_set
  )
  for d in sorted(probe_dirs):
    if not d or not os.path.isdir(d):
      continue
    ginfo = _resolve_git_dirs(d)
    if not ginfo:
      continue
    wt_top, _, wt_common = ginfo
    if wt_common in base_common_dirs and wt_top not in candidate_repos:
      candidate_repos.append(wt_top)

  entries: list[dict] = []
  for repo in candidate_repos:
    if not os.path.isdir(repo):
      continue
    ginfo = _resolve_git_dirs(repo)
    if not ginfo or ginfo[0] != repo:
      continue
    top_level, git_dir, git_common_dir = ginfo
    branch = _repo_branch(top_level)
    # Primary checkouts auto-ship only on main; linked worktrees also allow detached HEAD
    if top_level not in explicit_set and not is_auto_ship_branch(
        top_level, branch
    ):
      continue
    reg = _sync_repo_commits(top_level, git_common_dir)
    foreign_leased = _active_foreign_file_leases(git_dir, conv_id, tpath)
    own_rels = {
        os.path.relpath(p, top_level)
        for p in all_touched
        if p.startswith(top_level + os.sep)
    }
    lease_data = _read_json_file(os.path.join(git_dir, "jetski_file_leases.json"))
    for rel, info in (lease_data.get("files") or {}).items():
      if isinstance(info, dict) and info.get("conv_id") == conv_id:
        own_rels.add(rel)
    own_rels -= set(foreign_leased.keys())
    turn_rels = {
        os.path.relpath(p, top_level)
        for p in turn_touched
        if p.startswith(top_level + os.sep)
    } - set(foreign_leased.keys())

    files = [p for p in _dirty_repo_rels(top_level) if p in own_rels]
    foreign_unpushed, own_unpushed, _ = inspect_unpushed_commit_owners(
        top_level, git_dir, git_common_dir, conv_id, tpath, reg=reg
    )
    if not files and not own_unpushed:
      continue
    touched_now = bool(
        any(p in turn_rels for p in files)
        or (own_unpushed and top_level in turn_commit_repos)
    )
    entries.append({
        "repo": top_level,
        "git_dir": git_dir,
        "git_common_dir": git_common_dir,
        "files": files,
        "own_unpushed": own_unpushed,
        "foreign_unpushed": foreign_unpushed,
        "turn_touched": touched_now,
    })
  return entries


# Map committed paths in personal repos to the local build/service restart commands
def post_push_steps(
    repo: str,
    changed_rels: list[str],
    main_checkout_synced: bool = True,
) -> list[tuple[list[str], str]]:
  rp = os.path.realpath(os.path.expanduser(repo))
  gardener = _gardener_repo_root()
  config = _config_repo_root()
  changed_set = set(changed_rels)
  steps: list[tuple[list[str], str]] = []
  if rp == gardener or _is_linked_worktree_of(rp, gardener):
    # Services execute binaries from primary gardener checkout, so build only when synced
    if rp == gardener or main_checkout_synced:
      root_go = any(
          (r.endswith(".go") and not r.endswith("_test.go") and "/" not in r)
          or r in ("go.mod", "go.sum")
          for r in changed_rels
      )
      if root_go:
        steps.append((["go", "build", "-o", "skia-gardener", "."], gardener))
        steps.append((
            ["systemctl", "--user", "restart", "skia-gardener.service"],
            gardener,
        ))
      overlay_go = any(
          r.startswith("overlay/")
          and r.endswith(".go")
          and not r.endswith("_test.go")
          for r in changed_rels
      )
      if overlay_go:
        steps.append((
            ["go", "build", "-o", "overlay/skia-status-overlay", "./overlay"],
            gardener,
        ))
        steps.append((
            ["systemctl", "--user", "restart", "skia-status-overlay.service"],
            gardener,
        ))
  elif rp == config or _is_linked_worktree_of(rp, config):
    # cl_watcher imports chat_lifecycle at daemon startup, so changes to either need a restart
    if {"scripts/cl_watcher.py", "hooks/chat_lifecycle.py"} & changed_set:
      if rp == config or main_checkout_synced:
        steps.append(
            (["systemctl", "--user", "restart", "cl-flight-deck.service"], config)
        )
    # Sync ~/.gemini/jetski/brain/jetski-closed-filter.js from the shipped tree
    if {"scripts/jetski_closed_filter.js", "scripts/patch_jetski_hub_binary.py"} & changed_set:
      steps.append((["python3", "scripts/patch_jetski_hub_binary.py"], rp))
    # Restart hub daemon so in-memory index.html string table reloads the patched bootstrap shim
    if "scripts/patch_jetski_hub_binary.py" in changed_set:
      steps.append(
          (["systemctl", "--user", "restart", "jetski-hub.service"], rp)
      )
  return steps


def _append_model_trailer(message: str, conv_id: str) -> str:
  msg = message.strip()
  if "Assisted-by:" in msg:
    return msg
  trailer = "Assisted-by: AI"
  try:
    import chat_lifecycle

    trailer = chat_lifecycle.get_model_trailer(conv_id)
  except Exception:
    pass
  return f"{msg}\n\n{trailer}"


# Sync primary personal checkout when shipping from a worktree and run post-push hooks
def _run_post_push(
    repo: str,
    changed_rels: list[str],
    out,
    conv_id: str = "",
    transcript_path: str = "",
    head_sha: str = "",
) -> tuple[bool, list[str]]:
  notes: list[str] = []
  rp = os.path.realpath(os.path.expanduser(repo))
  config = _config_repo_root()
  gardener = _gardener_repo_root()
  main_synced = True
  primary_target = ""
  if _is_linked_worktree_of(rp, config):
    primary_target = config
  elif _is_linked_worktree_of(rp, gardener):
    primary_target = gardener
  if primary_target:
    # Fast-forward primary checkout before building or restarting services wired to it
    sync_res = force_sync_config_clone(
        repo=primary_target, conv_id=conv_id, transcript_path=transcript_path
    )
    sync_status = str(sync_res.get("status") or "unknown")
    prim_head = _git_head_sha(primary_target)
    main_synced = sync_status in ("updated", "up_to_date") and (
        not head_sha or prim_head == head_sha
    )
    if main_synced and sync_status == "updated":
      notes.append(f"synced {primary_target} -> {prim_head[:7]}")
    elif not main_synced:
      if primary_target == config and (
          {"scripts/cl_watcher.py", "hooks/chat_lifecycle.py"} & set(changed_rels)
      ):
        notes.append(
            f"skipped cl-flight-deck.service restart ({config} is {sync_status})"
        )
      elif primary_target == gardener and any(
          (r.endswith(".go") and not r.endswith("_test.go"))
          or r in ("go.mod", "go.sum")
          for r in changed_rels
      ):
        notes.append(
            f"skipped skia-gardener rebuild/restart ({gardener} is {sync_status})"
        )

  for cmd, cwd in post_push_steps(
      repo, changed_rels, main_checkout_synced=main_synced
  ):
    if cmd[:2] == ["systemctl", "--user"] and len(cmd) >= 4:
      unit = cmd[3]
      probe = _run_cmd(["systemctl", "--user", "cat", unit], timeout=3.0)
      if probe.returncode != 0:
        continue
    res = _run_cmd(cmd, timeout=60.0, cwd=cwd)
    if res.returncode != 0:
      out.write(
          f"post-push command failed ({' '.join(cmd)}):\n"
          f"{res.stdout}{res.stderr}\n"
      )
      return False, notes
    notes.append(" ".join(cmd))
  return True, notes


def ship(
    conv_id: str,
    transcript_path: str = "",
    message: str = "",
    out=None,
    run_post_push: bool = True,
    repo_filter: str = "",
) -> int:
  import roadmap

  out = out or sys.stdout
  tpath = roadmap._find_transcript_path(conv_id, transcript_path)
  # Pass explicit --repo into plan_ship so detached linked worktrees are discovered
  target_rp = (
      os.path.realpath(os.path.expanduser(repo_filter)) if repo_filter else ""
  )
  plan = plan_ship(
      conv_id, tpath, extra_repos=[target_rp] if target_rp else None
  )
  if target_rp:
    plan = [e for e in plan if e["repo"] == target_rp]
  if not plan:
    out.write("auto-ship: nothing to commit or push\n")
    return 0

  art_dir = ""
  norm_tpath = tpath.replace("\\", "/") if tpath else ""
  if "/.system_generated/" in norm_tpath:
    # Slice at /.system_generated/ so shallow test transcripts do not collapse to "/"
    art_dir = tpath[: norm_tpath.index("/.system_generated/")]
  override_path, guard_state_path = _parallel_guard_paths(art_dir, conv_id)
  has_override = os.path.exists(override_path)

  for entry in plan:
    repo = entry["repo"]
    git_dir = entry["git_dir"]
    git_common_dir = entry["git_common_dir"]
    files = entry["files"]

    if entry["foreign_unpushed"] and not has_override:
      _record_parallel_guard_block(guard_state_path, tpath)
      out.write(
          f"[FOREIGN-COMMIT PUSH GUARD] Branch in `{repo}` has unpushed "
          f"commit(s) created by another active conversation: "
          f"{', '.join(entry['foreign_unpushed'])}. Refusing auto-push.\n"
      )
      return 1

    base_res = _run_cmd(["git", "-C", repo, "rev-parse", "origin/main"], timeout=2.0)
    base_sha = base_res.stdout.strip() if base_res.returncode == 0 else ""

    if files:
      if not message.strip():
        out.write(f"auto-ship: commit message (-m) required for {repo}\n")
        return 2
      full_msg = _append_model_trailer(message, conv_id)
      # Deleted and renamed-away paths fail `git add` with exit 128; stage them via `git rm`
      present = [f for f in files if os.path.lexists(os.path.join(repo, f))]
      missing = [f for f in files if not os.path.lexists(os.path.join(repo, f))]
      if present:
        add_res = _run_cmd(["git", "-C", repo, "add", "--", *present], timeout=15.0)
        if add_res.returncode != 0:
          out.write(f"git add failed in {repo}:\n{add_res.stdout}{add_res.stderr}\n")
          return add_res.returncode
      if missing:
        rm_res = _run_cmd(
            ["git", "-C", repo, "rm", "--cached", "--ignore-unmatch", "--", *missing],
            timeout=15.0,
        )
        if rm_res.returncode != 0:
          out.write(f"git rm failed in {repo}:\n{rm_res.stdout}{rm_res.stderr}\n")
          return rm_res.returncode

      reg_path = os.path.join(git_common_dir, "jetski_commits.json")
      reg = _sync_repo_commits(repo, git_common_dir)
      head_before = _git_head_sha(repo)
      if head_before:
        reg.setdefault("pending", {})[repo] = {
            "conv_id": conv_id,
            "title": "",
            "head_before": head_before,
            "ts": time.time(),
        }
        _write_json_atomic(reg_path, reg)

      # Allow full githooks/pre-commit test suite to complete under multi-chat CPU load
      commit_res = _run_cmd(
          ["git", "-C", repo, "commit", "-m", full_msg, "--", *files],
          timeout=600.0,
      )
      if commit_res.returncode != 0:
        reg = _read_json_file(reg_path)
        if isinstance(reg.get("pending"), dict):
          reg["pending"].pop(repo, None)
          _write_json_atomic(reg_path, reg)
        out.write(
            f"git commit failed in {repo}:\n"
            f"{commit_res.stdout}{commit_res.stderr}\n"
        )
        return commit_res.returncode
      _sync_repo_commits(repo, git_common_dir)

    # Re-check right before push in case a parallel chat committed during pre-commit
    foreign_now, own_shas, reg = inspect_unpushed_commit_owners(
        repo, git_dir, git_common_dir, conv_id, tpath
    )
    if foreign_now and not has_override:
      _record_parallel_guard_block(guard_state_path, tpath)
      out.write(
          f"[FOREIGN-COMMIT PUSH GUARD] Branch in `{repo}` gained foreign "
          f"unpushed commit(s): {', '.join(foreign_now)}. Refusing auto-push.\n"
      )
      return 1

    # Detached or linked-worktree HEAD must push HEAD:main instead of stale local main
    push_ref = "main" if _repo_branch(repo) == "main" else "HEAD:main"
    push_res = _run_cmd(["git", "-C", repo, "push", "origin", push_ref], timeout=60.0)
    if push_res.returncode != 0:
      out.write(
          f"git push failed in {repo}:\n{push_res.stdout}{push_res.stderr}\n"
      )
      return push_res.returncode

    if own_shas:
      commits_map = (
          reg.get("commits") if isinstance(reg.get("commits"), dict) else {}
      )
      for sha in own_shas:
        if isinstance(commits_map.get(sha), dict):
          commits_map[sha]["pushed_by_owner"] = True
      _write_json_atomic(
          os.path.join(git_common_dir, "jetski_commits.json"), reg
      )

    head_sha = _git_head_sha(repo)
    changed_rels = list(files)
    if base_sha and head_sha:
      diff_res = _run_cmd(
          ["git", "-C", repo, "diff", "--name-only", f"{base_sha}..{head_sha}"],
          timeout=2.0,
      )
      if diff_res.returncode == 0:
        changed_rels = [
            x.strip() for x in diff_res.stdout.splitlines() if x.strip()
        ]

    if run_post_push and changed_rels:
      ok, notes = _run_post_push(
          repo,
          changed_rels,
          out,
          conv_id=conv_id,
          transcript_path=tpath,
          head_sha=head_sha,
      )
      if notes:
        out.write(f"post-push ({repo}): {'; '.join(notes)}\n")
      if not ok:
        return 1

    out.write(f"committed and pushed: {head_sha[:7]} -> origin/main\n")

  return 0


# Measure ahead/behind counts between local HEAD and upstream origin/main
def _ahead_behind_counts(
    target: str, old_sha: str, rem_sha: str
) -> tuple[int, int, bool]:
  if not old_sha or not rem_sha:
    return (0, 0, True)
  if old_sha == rem_sha:
    return (0, 0, False)
  res = _run_cmd(
      ["git", "-C", target, "rev-list", "--left-right", "--count", f"{old_sha}...{rem_sha}"],
      timeout=1.0,
  )
  if res.returncode != 0:
    return (0, 0, True)
  parts = res.stdout.strip().split()
  if len(parts) == 2:
    try:
      return (int(parts[0]), int(parts[1]), False)
    except ValueError:
      pass
  return (0, 0, True)


# Fast-forward clean Mac and Linux ~/.gemini/config clones when behind origin/main
def maybe_sync_mac_clone(
    repo: str = "",
    platform: str = "",
    min_interval_s: int = 300,
    conv_id: str = "",
    transcript_path: str = "",
    fetch_timeout_s: float = 2.0,
) -> dict:
  # Darwin and Linux platforms are supported for personal config sync
  plat = (platform or sys.platform).lower()
  if plat != "darwin" and not plat.startswith("linux"):
    return {"status": "skip_platform"}
  target = os.path.realpath(os.path.expanduser(repo or "~/.gemini/config"))
  if not os.path.isdir(target):
    return {"status": "missing"}
  ginfo = _resolve_git_dirs(target)
  if not ginfo or ginfo[0] != target:
    return {"status": "not_git"}
  _, git_dir, git_common_dir = ginfo
  stamp_path = os.path.join(git_dir, "jetski_mac_sync.json")
  now = time.time()
  stamp = _read_json_file(stamp_path)
  last_ts = float(stamp.get("ts") or 0.0)
  if min_interval_s > 0 and now - last_ts < min_interval_s:
    return {"status": "throttled"}
  # Record attempt timestamp up front so slow or offline networks do not block every turn
  _write_json_atomic(stamp_path, {"ts": now})

  if _repo_branch(target) != "main":
    return {"status": "not_main"}
  st = _run_cmd(
      ["git", "-C", target, "status", "--porcelain", "--untracked-files=no"],
      timeout=1.0,
  )
  is_dirty = st.returncode != 0 or bool(st.stdout.strip())
  # Fetch remote main before evaluating cleanliness and fast-forward status
  fetch = _run_cmd(
      ["git", "-C", target, "fetch", "-q", "origin", "main"],
      timeout=fetch_timeout_s,
  )
  old_sha = _git_head_sha(target)
  rem = _run_cmd(["git", "-C", target, "rev-parse", "origin/main"], timeout=0.8)
  rem_sha = rem.stdout.strip() if rem.returncode == 0 else ""

  # Network or timeout failure during fetch preserves cached behind count with unknown remote
  if fetch.returncode != 0:
    _, behind, _ = _ahead_behind_counts(target, old_sha, rem_sha)
    return {
        "status": "fetch_failed",
        "behind": behind,
        "unknown": True,
        "old": old_sha[:7],
        "remote": rem_sha[:7],
    }

  ahead, behind, unknown = _ahead_behind_counts(target, old_sha, rem_sha)
  # Dirty working tree prevents automated merge; surface behind count to prompt
  if is_dirty:
    return {
        "status": "dirty",
        "behind": behind,
        "ahead": ahead,
        "unknown": unknown,
        "old": old_sha[:7],
        "remote": rem_sha[:7],
    }

  if not rem_sha or rem_sha == old_sha or (behind == 0 and not unknown):
    return {"status": "up_to_date"}

  # Active worktree lease held by another conversation prevents fast-forward merge
  lease_file = os.path.join(git_dir, "jetski_lease.json")
  if os.path.isfile(lease_file):
    lease_data = _read_json_file(lease_file)
    lease_owner = str(lease_data.get("conv_id") or "")
    lease_ts = float(lease_data.get("ts") or 0.0)
    if lease_owner and lease_owner != conv_id:
      still_active, _ = pre_tool_guard._is_conv_still_active_tip(
          lease_owner, conv_id, transcript_path
      )
      if still_active:
        unexpired, _ = pre_tool_guard._is_worktree_lease_unexpired(
            lease_owner, lease_ts, target, transcript_path, now
        )
        if unexpired:
          return {
              "status": "leased",
              "behind": behind,
              "ahead": ahead,
              "unknown": unknown,
              "old": old_sha[:7],
              "remote": rem_sha[:7],
          }

  # Inspect incoming changed files
  incoming_res = _run_cmd(
      ["git", "-C", target, "diff", "--name-only", f"{old_sha}..{rem_sha}"],
      timeout=1.5,
  )
  incoming_files = (
      [x.strip() for x in incoming_res.stdout.splitlines() if x.strip()]
      if incoming_res.returncode == 0
      else []
  )

  # Check active foreign file leases against incoming changed files
  foreign_leases = _active_foreign_file_leases(git_dir, conv_id, transcript_path)
  if any(f in foreign_leases for f in incoming_files):
    return {
        "status": "leased",
        "behind": behind,
        "ahead": ahead,
        "unknown": unknown,
        "old": old_sha[:7],
        "remote": rem_sha[:7],
    }

  # Foreign unpushed commits on the branch block automated merge
  foreign_unpushed, _, _ = inspect_unpushed_commit_owners(
      target, git_dir, git_common_dir, conv_id, transcript_path
  )
  if foreign_unpushed:
    return {
        "status": "diverged",
        "behind": behind,
        "ahead": ahead,
        "unknown": unknown,
        "old": old_sha[:7],
        "remote": rem_sha[:7],
        "foreign_unpushed": foreign_unpushed,
    }

  ff = _run_cmd(
      ["git", "-C", target, "merge", "--ff-only", "-q", "origin/main"],
      timeout=1.5,
  )
  if ff.returncode != 0:
    return {
        "status": "diverged",
        "behind": behind,
        "ahead": ahead,
        "unknown": unknown,
        "old": old_sha[:7],
        "remote": rem_sha[:7],
    }

  # `git merge --ff-only` exits 0 without advancing HEAD when local main is ahead of origin
  new_sha = _git_head_sha(target)
  if not new_sha or new_sha == old_sha:
    return {"status": "up_to_date"}

  return {
      "status": "updated",
      "old": old_sha[:7],
      "new": new_sha[:7],
      "changed_files": incoming_files,
  }


# Bypass the sync throttle for callers that require fresh remote state (e.g. decisions.py)
def force_sync_config_clone(
    repo: str = "",
    platform: str = "",
    fetch_timeout_s: float = 2.0,
    conv_id: str = "",
    transcript_path: str = "",
) -> dict:
  return maybe_sync_mac_clone(
      repo=repo,
      platform=platform,
      min_interval_s=0,
      conv_id=conv_id,
      transcript_path=transcript_path,
      fetch_timeout_s=fetch_timeout_s,
  )


def _cli(argv: list[str]) -> int:
  parser = argparse.ArgumentParser(prog="auto_ship.py")
  sub = parser.add_subparsers(dest="cmd", required=True)

  p_plan = sub.add_parser("plan")
  p_plan.add_argument("conv_id")
  p_plan.add_argument("--transcript", default="")

  p_ship = sub.add_parser("ship")
  p_ship.add_argument("conv_id")
  p_ship.add_argument("-m", "--message", default="")
  p_ship.add_argument("--repo", default="")
  p_ship.add_argument("--transcript", default="")
  p_ship.add_argument("--no-post-push", action="store_true")

  for cmd_name in ("mac-sync", "sync"):
    p_s = sub.add_parser(cmd_name)
    p_s.add_argument("--repo", default="")
    p_s.add_argument("--min-interval", type=int, default=300)
    p_s.add_argument("--force", action="store_true")
    p_s.add_argument("--timeout", type=float, default=2.0)
    p_s.add_argument("--platform", default="")
    p_s.add_argument("--conv-id", default="")
    p_s.add_argument("--transcript", default="")

  ns = parser.parse_args(argv)
  if ns.cmd == "plan":
    print(json.dumps(plan_ship(ns.conv_id, ns.transcript), indent=2))
    return 0
  if ns.cmd == "ship":
    return ship(
        ns.conv_id,
        transcript_path=ns.transcript,
        message=ns.message,
        run_post_push=not ns.no_post_push,
        repo_filter=ns.repo,
    )
  if ns.cmd in ("mac-sync", "sync"):
    min_int = 0 if ns.force else ns.min_interval
    print(
        json.dumps(
            maybe_sync_mac_clone(
                repo=ns.repo,
                platform=ns.platform,
                min_interval_s=min_int,
                conv_id=ns.conv_id,
                transcript_path=ns.transcript,
                fetch_timeout_s=ns.timeout,
            )
        )
    )
    return 0
  return 2


if __name__ == "__main__":
  sys.exit(_cli(sys.argv[1:]))
