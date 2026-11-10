#!/usr/bin/env python3
"""Stage 0 parallel context and post-patch file bundler for diffs, CLs, and PRs.

Stages commit/PR metadata, open review threads, persistent finding-ledger
refutations, optional user-constraint slices from transcript.jsonl, unified
diff, and 1-indexed post-patch ('Side B') files + companion .h headers into
<out_dir>/{context.md,diff.patch,files_bundle.md,files/*.txt}, then prints a
compact JSON summary with a shard topology recommendation.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR.parent
REVIEW_TRIAGE_SCRIPTS = CONFIG_DIR / "skills" / "review-triage" / "scripts"
for _p in (SCRIPT_DIR, REVIEW_TRIAGE_SCRIPTS):
  if str(_p) not in sys.path:
    sys.path.insert(0, str(_p))

import finding_ledger
import triage_cl

MAX_INLINE_LINES = 220
WINDOW_PAD_LINES = 40
MAX_EXCERPT_LINES = 400
MAX_FILE_BYTES = 512 * 1024
MAX_LONG_LINE_CHARS = 2000
MAX_BUNDLED_FILES = 40

BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".woff", ".woff2", ".ttf", ".otf", ".pb", ".gz", ".zip",
    ".skp", ".wasm", ".bin", ".jar", ".so", ".dylib", ".dll",
})
GENERATED_SUFFIXES = (
    ".pb.h", ".pb.cc", ".pb.go", ".g.dart", ".freezed.dart", ".min.js", ".min.css",
)
GENERATED_BASENAMES = frozenset({
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "cargo.lock",
    "fieldtrial_testing_config.json", "enums.xml",
})
CPP_IMPL_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".mm"})


def _run_git(cwd: Path, args: list[str], timeout: float = 10.0) -> tuple[int, str]:
  """Runs a read-only git subcommand in cwd and returns (exit_code, stdout)."""
  try:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout
  except (OSError, subprocess.SubprocessError):
    return 1, ""


def _is_skippable_path(rel_path: str) -> str:
  """Returns a skip reason for binary or generated lockfile paths, else empty string."""
  low = rel_path.lower()
  name = os.path.basename(low)
  if name in GENERATED_BASENAMES or low.endswith(GENERATED_SUFFIXES):
    return "generated_or_lockfile"
  ext = os.path.splitext(low)[1]
  if ext in BINARY_SUFFIXES:
    return "binary_extension"
  return ""


def _extract_transcript_constraints(
    transcript_path: Path | None, limit: int = 12
) -> list[str]:
  """Extracts up to limit 1-line USER_INPUT slices (<=240 chars) from transcript.jsonl."""
  if not transcript_path or not transcript_path.is_file():
    return []
  slices: list[str] = []
  try:
    raw_lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
  except OSError:
    return []
  for line in raw_lines:
    if '"USER_INPUT"' not in line:
      continue
    try:
      obj = json.loads(line)
    except json.JSONDecodeError:
      continue
    if obj.get("type") != "USER_INPUT":
      continue
    collapsed = " ".join(str(obj.get("content") or "").split())
    if collapsed:
      slices.append(collapsed[:240])
  return slices[-limit:]


def _parse_side_b_hunk_ranges(
    patch_text: str,
) -> tuple[dict[str, list[tuple[int, int]]], set[str], set[str]]:
  """Parses unified diff into (side_b_ranges, deleted_paths, binary_paths)."""
  ranges: dict[str, list[tuple[int, int]]] = {}
  deleted: set[str] = set()
  binary: set[str] = set()
  cur_a: str | None = None
  cur_b: str | None = None

  for line in patch_text.splitlines():
    m_git = re.match(r"^diff --git a/(.*?) b/(.*)$", line)
    if m_git:
      cur_a, cur_b = m_git.group(1).strip(), m_git.group(2).strip()
      ranges.setdefault(cur_b, [])
      continue
    if line.startswith("Binary files ") and cur_b:
      binary.add(cur_b)
      continue
    if line.startswith("+++ /dev/null"):
      if cur_a:
        deleted.add(cur_a)
      if cur_b:
        ranges.pop(cur_b, None)
      cur_b = None
      continue
    if line.startswith("+++ b/"):
      cur_b = line[6:].strip()
      ranges.setdefault(cur_b, [])
      continue
    if cur_b and line.startswith("@@ "):
      m_hunk = re.search(r"\+(\d+)(?:,(\d+))?", line)
      if m_hunk:
        start = int(m_hunk.group(1))
        count = int(m_hunk.group(2)) if m_hunk.group(2) is not None else 1
        ranges[cur_b].append((start, max(start, start + count - 1)))

  return ranges, deleted, binary


def _merge_line_windows(
    hunks: list[tuple[int, int]], total_lines: int, pad: int = WINDOW_PAD_LINES
) -> list[tuple[int, int]]:
  """Expands Side-B hunk ranges by +-pad lines and merges nearby windows."""
  if total_lines <= 0:
    return []
  if not hunks:
    return [(1, min(total_lines, 160))]
  expanded = sorted(
      (max(1, s - pad), min(total_lines, e + pad)) for s, e in hunks
  )
  merged: list[tuple[int, int]] = [expanded[0]]
  for start, end in expanded[1:]:
    prev_start, prev_end = merged[-1]
    if start <= prev_end + 10:
      merged[-1] = (prev_start, max(prev_end, end))
    else:
      merged.append((start, end))
  return merged


def _companion_headers(rel_path: str) -> list[str]:
  """Returns candidate .h paths (same-dir and ../include/) for C/C++/ObjC++ sources."""
  p = Path(rel_path)
  if p.suffix.lower() not in CPP_IMPL_SUFFIXES:
    return []
  cands = [p.with_suffix(".h").as_posix()]
  # Skia and SkParagraph split public headers under ../include/<Stem>.h
  parts = list(p.parts)
  if "src" in parts:
    idx = len(parts) - 1 - parts[::-1].index("src")
    inc_parts = parts[:idx] + ["include"] + parts[idx + 1 : -1] + [f"{p.stem}.h"]
    inc_path = Path(*inc_parts).as_posix()
    if inc_path not in cands:
      cands.append(inc_path)
  return cands


def _number_lines(raw_text: str) -> str:
  """Formats file contents with 1-based line numbers matching Side-B diff anchors."""
  return "\n".join(
      f"{idx:4d}: {line}" for idx, line in enumerate(raw_text.splitlines(), 1)
  )


def _resolve_repo_root(repo_arg: str) -> Path | None:
  """Resolves repo_arg to its Git top-level directory when available."""
  cand = Path(repo_arg).expanduser()
  if not cand.exists():
    return None
  search_dir = cand if cand.is_dir() else cand.parent
  rc, out = _run_git(search_dir, ["rev-parse", "--show-toplevel"])
  if rc == 0 and out.strip():
    return Path(out.strip()).resolve()
  return None


def _decode_bounded_utf8(raw: bytes) -> str | None:
  """Decodes UTF-8 text when non-binary and within MAX_FILE_BYTES, else None."""
  if b"\x00" in raw[:8192] or len(raw) > MAX_FILE_BYTES:
    return None
  return raw.decode("utf-8", errors="replace")


def _fetch_http_bytes(url: str, max_bytes: int) -> bytes | None:
  """Fetches up to max_bytes from an HTTPS URL, returning None on network/size failure."""
  req = urllib.request.Request(url, headers={"User-Agent": "StageDiffContext/1.0"})
  try:
    with urllib.request.urlopen(req, timeout=12) as resp:
      data = resp.read(max_bytes + 1)
    return data if len(data) <= max_bytes else None
  except (OSError, ValueError, urllib.error.URLError):
    return None


def _fetch_local_file(
    repo_root: Path, rel_path: str, right_rev: str | None
) -> str | None:
  """Reads post-patch file from working tree when right_rev is None, else git show."""
  if right_rev is None:
    disk_path = (repo_root / rel_path).resolve()
    # Reject symlink or '..' escapes outside repo_root when staging untrusted patches
    if repo_root not in disk_path.parents or not disk_path.is_file():
      return None
    try:
      return _decode_bounded_utf8(disk_path.read_bytes())
    except OSError:
      return None
  rc, out = _run_git(repo_root, ["show", f"{right_rev}:{rel_path}"])
  if rc != 0 or len(out.encode("utf-8", errors="replace")) > MAX_FILE_BYTES:
    return None
  return out


def _fetch_gerrit_file(
    host: str, cl_id: str, rev: str, rel_path: str, repo_root: Path | None, rev_sha: str
) -> str | None:
  """Reads Side-B file from local git object if present, else Gerrit REST /content."""
  if repo_root and rev_sha:
    local = _fetch_local_file(repo_root, rel_path, rev_sha)
    if local is not None:
      return local
  quoted = urllib.parse.quote(rel_path, safe="")
  url = f"https://{host}/changes/{cl_id}/revisions/{rev or 'current'}/files/{quoted}/content"
  b64_bytes = _fetch_http_bytes(url, MAX_FILE_BYTES * 2)
  if b64_bytes is None:
    return None
  try:
    return _decode_bounded_utf8(base64.b64decode(b64_bytes))
  except ValueError:
    return None


def _fetch_github_file(
    repo_slug: str, head_sha: str, rel_path: str, repo_root: Path | None
) -> str | None:
  """Reads PR head file from local git object if present, else raw.githubusercontent.com."""
  if repo_root and head_sha:
    local = _fetch_local_file(repo_root, rel_path, head_sha)
    if local is not None:
      return local
  if not repo_slug or not head_sha:
    return None
  quoted = "/".join(urllib.parse.quote(part, safe="") for part in rel_path.split("/"))
  raw = _fetch_http_bytes(
      f"https://raw.githubusercontent.com/{repo_slug}/{head_sha}/{quoted}", MAX_FILE_BYTES
  )
  return _decode_bounded_utf8(raw) if raw is not None else None


def _fetch_github_comments(repo_slug: str, pr_number: str) -> list[str]:
  """Fetches inline review comments on a GitHub PR via gh api when authenticated."""
  if not repo_slug or not pr_number:
    return []
  try:
    out = subprocess.check_output(
        ["gh", "api", f"repos/{repo_slug}/pulls/{pr_number}/comments?per_page=50"],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=8,
    )
    items = json.loads(out)
  except (OSError, ValueError, subprocess.SubprocessError):
    return []
  if not isinstance(items, list):
    return []
  formatted: list[str] = []
  for c in items[-25:]:
    if not isinstance(c, dict):
      continue
    user = ((c.get("user") or {}).get("login")) or "unknown"
    path = c.get("path") or "PR"
    line = c.get("line") or c.get("original_line") or ""
    loc = f"{path}:{line}" if line else path
    body = " ".join(str(c.get("body") or "").split())[:220]
    if body:
      formatted.append(f"- `{loc}` (**@{user}**): \"{body}\"")
  return formatted


def _classify_layer(rel_path: str) -> str:
  """Maps a file path to a coarse architectural layer for cross-layer detection."""
  ext = os.path.splitext(rel_path)[1].lower()
  if ext in {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".mm"}:
    return "cpp"
  if ext == ".dart":
    return "dart"
  if ext == ".py":
    return "python"
  if ext in {".ts", ".tsx", ".js", ".jsx"}:
    return "web"
  return "other"


def _classify_shard_topology(
    changed_files: list[str], insertions: int, deletions: int, status: str
) -> tuple[str, list[list[str]]]:
  """Recommends Stage A / Stage 2 subagent shard topology and <=5-file shard slices."""
  if status.upper() == "ABANDONED" or not changed_files:
    return "0_subagents", []
  code_layers = {
      _classify_layer(p) for p in changed_files if _classify_layer(p) != "other"
  }
  total_delta = insertions + deletions
  shards = [
      changed_files[i : i + 5] for i in range(0, len(changed_files), 5)
  ]
  if len(changed_files) >= 12 or total_delta >= 1000:
    return "large_sharded", shards
  if len(changed_files) > 5 or len(code_layers) > 1 or total_delta >= 400:
    if len(shards) == 1 and len(changed_files) >= 2:
      mid = max(1, len(changed_files) // 2)
      shards = [changed_files[:mid], changed_files[mid:]]
    return "sharded_2_3", shards
  return "single_hunter", shards


def _load_local_source(repo_arg: str, diff_ref: str) -> dict:
  """Resolves local working-tree or branch diff metadata and post-patch file reader."""
  repo_root = _resolve_repo_root(repo_arg)
  if repo_root is None:
    raise ValueError(f"Not inside a Git repository: {repo_arg}")

  right_rev: str | None = None
  diff_mode = f"worktree_vs_{diff_ref}"
  if ".." in diff_ref:
    # Support both two-dot (A..B) and three-dot (A...B) Git diff ranges
    right_rev = re.split(r"\.{2,3}", diff_ref, maxsplit=1)[-1].strip() or "HEAD"
    diff_mode = f"range_{diff_ref}"
    _, patch_text = _run_git(repo_root, ["diff", diff_ref], timeout=20.0)
  else:
    _, patch_text = _run_git(repo_root, ["diff", diff_ref], timeout=20.0)
    # Fall back to committed HEAD patch when working tree is already clean on HEAD
    if not patch_text.strip() and diff_ref == "HEAD":
      _, patch_text = _run_git(
          repo_root, ["show", "--format=", "--patch", "HEAD"], timeout=20.0
      )
      right_rev = "HEAD"
      diff_mode = "head_commit"

  rev_target = right_rev or "HEAD"
  _, commit_msg = _run_git(repo_root, ["log", "-1", "--format=%B", rev_target])
  _, subject = _run_git(repo_root, ["log", "-1", "--format=%s", rev_target])
  _, rev_sha = _run_git(repo_root, ["rev-parse", rev_target])
  _, branch = _run_git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])

  return {
      "mode": "local", "status": "LOCAL", "owner": "local", "patchset": "",
      "target": f"{branch.strip() or 'HEAD'} ({diff_mode})",
      "subject": subject.strip() or "(local working tree)",
      "revision_sha": rev_sha.strip(),
      "commit_msg": commit_msg.strip() or "(no commit message)",
      "patch_text": patch_text, "repo_root": repo_root,
      "ledger_repo": str(repo_root), "ledger_cl": None,
      "open_threads_md": "(local diff — no remote review threads)",
      "open_threads_count": 0,
      "fetch_file": lambda rel: _fetch_local_file(repo_root, rel, right_rev),
  }


def _format_gerrit_threads_md(tctx: dict) -> tuple[str, int]:
  """Formats Gerrit open threads and recent follow-up dialogue for context.md."""
  open_threads = tctx.get("open_threads") or []
  dialogue = tctx.get("dialogue") or []
  parts: list[str] = []
  if open_threads:
    parts.append(f"### Open Unresolved Threads ({len(open_threads)})")
    for t in open_threads:
      tags = " ".join(f"[{tg}]" for tg in (t.get("tags") or []))
      prefix = f"{tags} " if tags else ""
      parts.append(
          f"- {prefix}`{t['location']}` — **{t['root_author']}** "
          f"(PS{t['patch_set']} vs PS{t['current_ps']}): \"{t['root_excerpt']}\""
      )
      if t.get("last_excerpt"):
        parts.append(f"  - Last reply ({t['last_role']}): \"{t['last_excerpt']}\"")
  else:
    parts.append("- 0 unresolved Gerrit comment threads.")

  if dialogue:
    parts.append("\n### Recent Patchset Dialogue")
    parts.append("```text")
    parts.extend(dialogue[-10:])
    parts.append("```")
  return "\n".join(parts), len(open_threads)


def _load_remote_source(target: str, gerrit_host: str, repo_arg: str) -> dict:
  """Resolves a Gerrit CL or GitHub PR into metadata, patch, threads, and fetcher."""
  repo_root = _resolve_repo_root(repo_arg) if repo_arg else None
  raw = target.strip()

  if "github.com" in raw and "/pull/" in raw:
    m_pr = re.search(r"(https://github\.com/([^/]+/[^/]+))/pull/(\d+)", raw)
    if not m_pr:
      raise ValueError(f"Unsupported GitHub PR URL: {raw}")
    repo_url, repo_slug, pr_num = m_pr.group(1), m_pr.group(2), m_pr.group(3)
    pr_data = triage_cl.fetch_github_pr(repo_url, pr_num)
    if "error" in pr_data:
      raise RuntimeError(pr_data["error"])
    head_sha = pr_data.get("head_sha") or ""
    gh_comments = _fetch_github_comments(repo_slug, pr_num)
    threads_md = "\n".join(gh_comments) or "- 0 inline GitHub PR review comments fetched."
    return {
        "mode": "github", "status": "OPEN", "patchset": "1",
        "target": f"{repo_url}/pull/{pr_num}",
        "subject": pr_data.get("subject", f"PR #{pr_num}"),
        "owner": pr_data.get("owner", "Unknown"), "revision_sha": head_sha,
        "commit_msg": pr_data.get("description", ""),
        "patch_text": pr_data.get("patch", ""), "repo_root": repo_root,
        "ledger_repo": str(repo_root) if repo_root else repo_url, "ledger_cl": pr_num,
        "open_threads_md": threads_md, "open_threads_count": len(gh_comments),
        "fetch_file": lambda rel: _fetch_github_file(repo_slug, head_sha, rel, repo_root),
    }

  host_hint = gerrit_host.strip() or None
  cl_id, rev_hint = raw, "current"
  if raw.startswith(("http://", "https://")):
    m_host = re.search(r"https?://([^/]+)", raw)
    if m_host:
      host_hint = m_host.group(1)
    m_cl = re.search(r"/\+/(\d+)(?:/(\d+))?", raw) or re.search(r"/changes/(\d+)", raw)
    if not m_cl:
      raise ValueError(f"Could not parse Gerrit CL number from URL: {raw}")
    cl_id = m_cl.group(1)
    if m_cl.lastindex and m_cl.lastindex >= 2 and m_cl.group(2):
      rev_hint = m_cl.group(2)
  elif not cl_id.isdigit():
    raise ValueError(f"Unsupported remote target '{raw}' (expected CL ID/URL or PR URL)")

  cl_data = triage_cl.probe_gerrit(cl_id, host_hint=host_hint)
  if not cl_data:
    raise RuntimeError(f"Could not fetch Gerrit CL {cl_id} (host_hint={host_hint})")

  detail = cl_data.get("detail") or {}
  host, project = cl_data["host"], detail.get("project") or "skia"
  tctx = cl_data.get("thread_context") or {}
  rev_sha = tctx.get("current_sha") or detail.get("current_revision") or ""
  ps_str = str(cl_data.get("patchset") or tctx.get("current_ps") or "")
  threads_md, open_count = _format_gerrit_threads_md(tctx)

  return {
      "mode": "gerrit", "target": f"https://{host}/c/{project}/+/{cl_id}",
      "status": str(detail.get("status") or "NEW"), "subject": cl_data.get("subject", ""),
      "owner": cl_data.get("owner_email") or cl_data.get("owner", "Unknown"),
      "patchset": ps_str, "revision_sha": rev_sha,
      "commit_msg": cl_data.get("commit_message") or cl_data.get("description", ""),
      "patch_text": cl_data.get("patch", ""), "repo_root": repo_root,
      "ledger_repo": str(repo_root) if repo_root else project, "ledger_cl": cl_id,
      "open_threads_md": threads_md, "open_threads_count": open_count,
      "fetch_file": lambda rel: _fetch_gerrit_file(
          host, cl_id, rev_hint if rev_hint != "current" else ps_str, rel, repo_root, rev_sha
      ),
  }


def _stage_files_bundle(
    out_dir: Path,
    changed_paths: list[str],
    deleted_paths: set[str],
    binary_paths: set[str],
    diff_ranges: dict[str, list[tuple[int, int]]],
    fetch_file: Callable[[str], str | None],
) -> tuple[list[str], list[dict[str, str]]]:
  """Stages 1-indexed Side-B files + companion .h headers and writes files_bundle.md."""
  files_dir = out_dir / "files"
  files_dir.mkdir(parents=True, exist_ok=True)

  skipped: list[dict[str, str]] = []
  primary_set = set(changed_paths)
  candidates: list[str] = []
  seen: set[str] = set()

  for p in sorted(deleted_paths):
    skipped.append({"path": p, "reason": "deleted"})

  for fpath in changed_paths:
    if fpath in binary_paths:
      skipped.append({"path": fpath, "reason": "binary_diff"})
      continue
    reason = _is_skippable_path(fpath)
    if reason:
      skipped.append({"path": fpath, "reason": reason})
      continue
    if fpath not in seen:
      candidates.append(fpath)
      seen.add(fpath)
    for comp_h in _companion_headers(fpath):
      if comp_h not in seen and comp_h not in deleted_paths:
        candidates.append(comp_h)
        seen.add(comp_h)

  if len(candidates) > MAX_BUNDLED_FILES:
    for extra in candidates[MAX_BUNDLED_FILES:]:
      if extra in primary_set:
        skipped.append({"path": extra, "reason": "bundle_cap_exceeded"})
    candidates = candidates[:MAX_BUNDLED_FILES]

  fetched: dict[str, str | None] = {}
  with ThreadPoolExecutor(max_workers=8) as pool:
    futs = {pool.submit(fetch_file, rel): rel for rel in candidates}
    for fut in as_completed(futs):
      fetched[futs[fut]] = fut.result()

  bundle_parts = [
      "# Post-Patch (`Side B`) Files & Companion Headers",
      "Line numbers below are 1-indexed `Side B` anchors.",
  ]
  staged_index: list[str] = []

  for fpath in candidates:
    raw_content = fetched.get(fpath)
    if raw_content is None:
      # Companion header candidates may not exist; only record primary changed files
      if fpath in primary_set:
        skipped.append({"path": fpath, "reason": "oversized_or_unreadable"})
      continue
    raw_lines = raw_content.splitlines()
    if any(len(ln) > MAX_LONG_LINE_CHARS for ln in raw_lines[:50]):
      if fpath in primary_set:
        skipped.append({"path": fpath, "reason": "minified_long_lines"})
      continue

    numbered = _number_lines(raw_content)
    # Sanitize slashes and any non-filename characters so staged files stay flat in files/
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "__", fpath) + ".txt"
    dest = files_dir / safe_name
    dest.write_text(numbered + ("\n" if numbered else ""), encoding="utf-8")

    num_lines = len(raw_lines)
    role_tag = "modified" if fpath in primary_set else "companion header"
    staged_index.append(
        f"- `{fpath}` ({num_lines} lines, {role_tag}) -> `{dest}`"
    )

    n_lines = numbered.splitlines()
    if num_lines <= MAX_INLINE_LINES:
      bundle_parts.append(
          f"\n## FILE: `{fpath}` ({num_lines} lines — complete, {role_tag})\n"
          f"```text\n{numbered}\n```"
      )
      continue

    windows = _merge_line_windows(diff_ranges.get(fpath, []), num_lines)
    chunks: list[str] = []
    used = 0
    if windows and windows[0][0] > 30:
      chunks.append("\n".join(n_lines[:25]) + "\n  ... [header truncated] ...")
      used += 26
    for ws, we in windows:
      slice_lines = n_lines[ws - 1 : we]
      if used + len(slice_lines) > MAX_EXCERPT_LINES:
        remain = max(0, MAX_EXCERPT_LINES - used)
        if remain > 0:
          chunks.append(
              f"  ... [lines {ws}..{ws + remain - 1} of {num_lines}] ...\n"
              + "\n".join(slice_lines[:remain])
          )
        chunks.append(f"  ... [excerpt capped; read full file at {dest}] ...")
        break
      chunks.append(
          f"  ... [lines {ws}..{we} of {num_lines}] ...\n" + "\n".join(slice_lines)
      )
      used += len(slice_lines)

    bundle_parts.append(
        f"\n## FILE: `{fpath}` ({num_lines} lines — `±{WINDOW_PAD_LINES}`-line "
        f"windows inline; full 1-indexed file at `{dest}`)\n"
        f"```text\n{chr(10).join(chunks)}\n```"
    )

  (out_dir / "files_bundle.md").write_text(
      "\n".join(bundle_parts) + "\n", encoding="utf-8"
  )
  return staged_index, skipped


def _render_context_md(
    source: dict,
    out_dir: Path,
    file_stats: list[str],
    insertions: int,
    deletions: int,
    topology: str,
    shards: list[list[str]],
    staged_index: list[str],
    skipped: list[dict[str, str]],
    ledger_md: str,
    constraints: list[str],
) -> str:
  """Formats <out_dir>/context.md for Turn 1 parallel ingestion by audit shards."""
  repo_root = source.get("repo_root")
  repo_note = (
      f"`{repo_root}` (use fast `git -C {repo_root} grep -n` or `scout` for "
      "untouched external callers instead of re-running diff/metadata commands)"
      if repo_root
      else "(remote-only bundle — no local git checkout supplied)"
  )
  shard_lines = [
      f"  - Shard {idx}: {', '.join(f'`{p}`' for p in grp)}"
      for idx, grp in enumerate(shards, 1)
  ] or ["  - (no shards required)"]
  skip_lines = [
      f"- `{item['path']}` ({item['reason']})" for item in skipped
  ] or ["- (none)"]
  constraint_lines = [
      f"{idx}. {c}" for idx, c in enumerate(constraints, 1)
  ] or ["(No transcript provided or 0 USER_INPUT turns found)"]

  ps_suffix = f" (PS {source['patchset']})" if source.get("patchset") else ""
  return f"""# Stage 0 Audit Context — {source['target']}{ps_suffix}

## 1. Metadata & Turn 1 Read Routing
- **Mode / Status**: `{source['mode']}` / `{source['status']}`
- **Subject**: {source['subject']}
- **Owner**: `{source['owner']}`
- **Revision**: `{source.get('revision_sha') or 'working-tree'}`
- **Delta**: `+{insertions} / -{deletions}` across `{len(file_stats)}` file(s)
- **Recommended Shard Topology**: `{topology}`
{chr(10).join(shard_lines)}
- **Local Checkout**: {repo_note}
- **Turn 1 Parallel `read_file` Bundle**:
  - Context & prior refutations: `{out_dir / 'context.md'}`
  - Unified patch: `{out_dir / 'diff.patch'}`
  - Post-patch `Side B` digest + companion `.h` headers: `{out_dir / 'files_bundle.md'}`
  - Full 1-indexed `Side B` files:
{chr(10).join('    ' + s for s in staged_index) if staged_index else '    - (none)'}

## 2. Modified File Inventory
{chr(10).join(file_stats) if file_stats else '- (empty diff)'}

### Skipped Binary / Generated / Deleted Files
{chr(10).join(skip_lines)}

## 3. Commit / PR Description
```text
{source['commit_msg']}
```

## 4. Original User Constraints Slice (`transcript.jsonl` Goal-Drift Check)
{chr(10).join(constraint_lines)}

## 5. Open Review Threads & Patchset Dialogue
{source['open_threads_md']}

## 6. {ledger_md.lstrip('# ').rstrip()}
"""


def stage_context(
    out_dir: Path,
    repo: str = ".",
    diff_ref: str = "HEAD",
    target: str = "",
    gerrit_host: str = "",
    transcript: Path | None = None,
) -> dict:
  """Executes Stage 0 bundling into out_dir and returns the summary dict."""
  t0 = time.monotonic()
  out_dir = out_dir.expanduser().resolve()
  out_dir.mkdir(parents=True, exist_ok=True)

  if target.strip():
    source = _load_remote_source(target, gerrit_host, repo)
  else:
    source = _load_local_source(repo, diff_ref)

  patch_text = source["patch_text"]
  (out_dir / "diff.patch").write_text(patch_text, encoding="utf-8")

  parsed_files = triage_cl.parse_patch(patch_text)
  diff_ranges, deleted_paths, binary_paths = _parse_side_b_hunk_ranges(patch_text)

  changed_paths: list[str] = []
  file_stats: list[str] = []
  insertions = 0
  deletions = 0
  for pf in parsed_files:
    if pf.path == "/COMMIT_MSG":
      continue
    insertions += pf.added
    deletions += pf.deleted
    status_ch = "D" if pf.path in deleted_paths else "M"
    file_stats.append(f"- `{pf.path}` ({status_ch}: +{pf.added} / -{pf.deleted})")
    if pf.path not in deleted_paths and pf.path not in changed_paths:
      changed_paths.append(pf.path)

  all_touched = sorted(set(changed_paths) | deleted_paths)
  ledger_path, ledger_entries = finding_ledger.query_entries(
      repo=source["ledger_repo"],
      files=all_touched or None,
      cl=source["ledger_cl"],
  )
  ledger_md = finding_ledger.format_query_markdown(
      ledger_entries, ledger_path=ledger_path, include_empty_header=True
  )
  constraints = _extract_transcript_constraints(transcript)

  if source["status"].upper() == "ABANDONED":
    staged_index, skipped = [], []
    (out_dir / "files_bundle.md").write_text(
        "# Post-Patch Files Skipped (CL ABANDONED)\n", encoding="utf-8"
    )
  else:
    staged_index, skipped = _stage_files_bundle(
        out_dir, changed_paths, deleted_paths, binary_paths, diff_ranges, source["fetch_file"]
    )

  topology, shards = _classify_shard_topology(
      changed_paths, insertions, deletions, source["status"]
  )
  context_md = _render_context_md(
      source, out_dir, file_stats, insertions, deletions, topology,
      shards, staged_index, skipped, ledger_md, constraints,
  )
  (out_dir / "context.md").write_text(context_md, encoding="utf-8")

  return {
      "mode": source["mode"], "target": source["target"], "status": source["status"],
      "subject": source["subject"], "patchset": source["patchset"],
      "revision_sha": source["revision_sha"],
      "repo_root": str(source["repo_root"]) if source.get("repo_root") else None,
      "files_count": len(all_touched), "bundled_files_count": len(staged_index),
      "skipped_files": skipped, "insertions": insertions, "deletions": deletions,
      "open_threads_count": source["open_threads_count"],
      "prior_refutations_count": len(ledger_entries),
      "user_constraints_count": len(constraints),
      "recommended_topology": topology, "recommended_shards": shards,
      "out_dir": str(out_dir),
      "artifacts": {
          "context_md": str(out_dir / "context.md"),
          "diff_patch": str(out_dir / "diff.patch"),
          "files_bundle_md": str(out_dir / "files_bundle.md"),
          "files_dir": str(out_dir / "files"),
      },
      "elapsed_sec": round(time.monotonic() - t0, 3),
  }


def main(argv: list[str] | None = None) -> int:
  """CLI entry point for Stage 0 diff/CL/PR context bundling."""
  ap = argparse.ArgumentParser(description=__doc__)
  ap.add_argument("--out-dir", required=True, help="Directory to write staged bundle files")
  ap.add_argument("--repo", default=".", help="Local Git repo path for local diffs or Side-B git")
  ap.add_argument("--diff", default="HEAD", help="Git diff ref/range for local diff (default HEAD)")
  ap.add_argument("--target", default="", help="Optional Gerrit CL ID/URL or GitHub PR URL")
  ap.add_argument("--gerrit-host", default="", help="Optional Gerrit host hint for bare CL IDs")
  ap.add_argument("--transcript", default="", help="Optional transcript.jsonl for user constraints")
  args = ap.parse_args(argv)

  tx_path = Path(args.transcript).expanduser() if args.transcript else None
  try:
    summary = stage_context(
        out_dir=Path(args.out_dir), repo=args.repo, diff_ref=args.diff,
        target=args.target, gerrit_host=args.gerrit_host, transcript=tx_path,
    )
  except (OSError, RuntimeError, ValueError) as exc:
    print(json.dumps({"error": str(exc)}), file=sys.stderr)
    return 1

  print(json.dumps(summary, indent=2, ensure_ascii=False))
  return 0


if __name__ == "__main__":
  sys.exit(main())
