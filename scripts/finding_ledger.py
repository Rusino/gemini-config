#!/usr/bin/env python3
"""Persistent Finding Ledger for clean-context review and audit subagents.

Stores refuted findings (false positives verified by keeper_skeptic, execution-refuted
trap tests, and explicit user design decisions) per repository or document so subsequent
runs of amnesia-precommit, ruthless-code-audit, review-triage, and amnesia-doc-check
do not waste tokens re-raising the same issues.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


DEFAULT_LEDGER_DIR = Path.home() / ".gemini" / "config" / "ledgers"
CHANGE_ID_RE = re.compile(r"^\s*Change-Id:\s*(I[0-9a-fA-F]{8,40})\s*$", re.MULTILINE)


def get_ledger_dir() -> Path:
  # Allow tests to isolate ledger files via environment override
  env_dir = os.environ.get("JETSKI_LEDGER_DIR")
  base = Path(env_dir) if env_dir else DEFAULT_LEDGER_DIR
  base.mkdir(parents=True, exist_ok=True)
  return base


def _run_git(cwd: Path, args: list[str]) -> str:
  try:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return res.stdout.strip() if res.returncode == 0 else ""
  except Exception:
    return ""


def _sanitize_slug(raw: str) -> str:
  cleaned = raw.strip().rstrip("/")
  if cleaned.endswith(".git"):
    cleaned = cleaned[:-4]
  tail = re.split(r"[:/]", cleaned)[-1]
  slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", tail).strip("-.").lower()
  return slug or "default"


def resolve_repo_root(repo_or_doc: str | Path | None) -> Path | None:
  if not repo_or_doc:
    return None
  cand = Path(str(repo_or_doc)).expanduser()
  if not cand.exists():
    return None
  search_dir = cand if cand.is_dir() else cand.parent
  top = _run_git(search_dir, ["rev-parse", "--show-toplevel"])
  if top:
    return Path(top).resolve()
  return search_dir.resolve() if cand.is_dir() else None


def resolve_ledger_key(repo: str | None = None, doc: str | None = None) -> str:
  # Extract canonical origin slug so separate git worktrees share one repo ledger
  target = repo or doc or "."
  cand = Path(str(target)).expanduser()
  if cand.exists():
    search_dir = cand if cand.is_dir() else cand.parent
    origin = _run_git(search_dir, ["config", "--get", "remote.origin.url"])
    if origin:
      return _sanitize_slug(origin)
    common_dir = _run_git(search_dir, ["rev-parse", "--git-common-dir"])
    if common_dir:
      common_path = (search_dir / common_dir).resolve()
      main_root = common_path.parent if common_path.name == ".git" else common_path
      return _sanitize_slug(main_root.name)
    if cand.is_dir():
      return _sanitize_slug(cand.resolve().name)
    return "docs"
  if repo:
    return _sanitize_slug(repo)
  if doc:
    return "docs"
  return "default"


def get_ledger_path(repo: str | None = None, doc: str | None = None) -> Path:
  key = resolve_ledger_key(repo=repo, doc=doc)
  return get_ledger_dir() / f"{key}.jsonl"


def normalize_target_path(target: str, repo_root: Path | None = None) -> str:
  raw = target.strip()
  if not raw or raw == "*":
    return "*"
  # Strip standard unified-diff prefixes so patch paths match repo-relative paths
  for prefix in ("a/", "b/", "./"):
    if raw.startswith(prefix):
      raw = raw[len(prefix) :]
  cand = Path(raw).expanduser()
  if repo_root and repo_root.exists():
    try:
      abs_cand = cand.resolve() if cand.is_absolute() else (repo_root / cand).resolve()
      rel = abs_cand.relative_to(repo_root.resolve())
      return rel.as_posix()
    except ValueError:
      pass
  if cand.is_absolute() and cand.exists():
    top = resolve_repo_root(cand)
    if top:
      try:
        return cand.resolve().relative_to(top).as_posix()
      except ValueError:
        pass
  return raw.replace("\\", "/")


def compute_file_hash(target_rel: str, repo_root: Path | None = None) -> str:
  if not target_rel or target_rel == "*":
    return ""
  candidates = []
  if repo_root:
    candidates.append(repo_root / target_rel)
  candidates.append(Path(target_rel).expanduser())
  for path in candidates:
    if path.is_file():
      try:
        return hashlib.sha1(path.read_bytes()).hexdigest()[:12]
      except OSError:
        return ""
  return ""


def detect_cl_id(repo_root: Path | None = None, explicit_cl: str | None = None) -> str:
  if explicit_cl:
    return str(explicit_cl).strip()
  if not repo_root or not repo_root.exists():
    return "worktree"
  branch = _run_git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])
  if branch and branch not in ("HEAD", "main", "master"):
    return branch
  # Avoid reading parent HEAD's Change-Id when uncommitted changes sit on top of main
  dirty = bool(_run_git(repo_root, ["status", "--porcelain"]))
  if not dirty:
    msg = _run_git(repo_root, ["log", "-1", "--format=%B", "HEAD"])
    m = CHANGE_ID_RE.search(msg)
    if m:
      return m.group(1)
  return "worktree"


def collect_diff_files(repo_root: Path, diff_ref: str = "HEAD") -> list[str]:
  if not repo_root or not repo_root.exists():
    return []
  files: set[str] = set()
  for cmd in (
      ["diff", "--name-only", diff_ref],
      ["diff", "--name-only", "--cached"],
  ):
    out = _run_git(repo_root, cmd)
    for line in out.splitlines():
      if line.strip():
        files.add(normalize_target_path(line.strip(), repo_root))
  # Fall back to files touched by HEAD commit when working tree is already clean
  if not files and diff_ref == "HEAD":
    out = _run_git(repo_root, ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"])
    for line in out.splitlines():
      if line.strip():
        files.add(normalize_target_path(line.strip(), repo_root))
  return sorted(files)


def load_entries(ledger_path: Path) -> list[dict]:
  if not ledger_path.is_file():
    return []
  entries: list[dict] = []
  for line in ledger_path.read_text(encoding="utf-8", errors="replace").splitlines():
    line = line.strip()
    if not line:
      continue
    try:
      entries.append(json.loads(line))
    except json.JSONDecodeError:
      continue
  return entries


def save_entries(ledger_path: Path, entries: list[dict]) -> None:
  ledger_path.parent.mkdir(parents=True, exist_ok=True)
  lines = [json.dumps(e, ensure_ascii=False) for e in entries]
  content = "\n".join(lines) + ("\n" if lines else "")
  ledger_path.write_text(content, encoding="utf-8")


def _next_id(entries: list[dict]) -> str:
  max_num = 0
  for e in entries:
    eid = str(e.get("id", ""))
    m = re.match(r"^L(\d+)$", eid)
    if m:
      max_num = max(max_num, int(m.group(1)))
  return f"L{max_num + 1:03d}"


def add_entry(
    file_target: str,
    claim: str,
    reason: str,
    repo: str | None = None,
    doc: str | None = None,
    symbol: str = "",
    scope: str = "repo",
    cl: str | None = None,
    category: str = "skeptic-refuted",
) -> dict:
  repo_root = resolve_repo_root(repo or doc or file_target)
  ledger_path = get_ledger_path(repo=repo, doc=doc or file_target)
  norm_file = normalize_target_path(file_target, repo_root)
  file_hash = compute_file_hash(norm_file, repo_root)
  active_cl = detect_cl_id(repo_root, explicit_cl=cl) if scope == "cl" or cl else ""
  now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

  entries = load_entries(ledger_path)
  norm_sym = symbol.strip()
  norm_claim = claim.strip()
  norm_reason = reason.strip()

  # Update in place if the exact (file, symbol, claim) was already recorded
  for existing in entries:
    if (
        existing.get("file") == norm_file
        and existing.get("symbol", "").lower() == norm_sym.lower()
        and existing.get("claim", "").lower() == norm_claim.lower()
    ):
      existing["timestamp"] = now_iso
      existing["reason"] = norm_reason
      existing["scope"] = scope
      existing["cl"] = active_cl
      existing["category"] = category
      if file_hash:
        existing["file_hash"] = file_hash
      save_entries(ledger_path, entries)
      return existing

  record = {
      "id": _next_id(entries),
      "timestamp": now_iso,
      "scope": scope if scope in ("repo", "cl") else "repo",
      "cl": active_cl,
      "file": norm_file,
      "symbol": norm_sym,
      "category": category,
      "claim": norm_claim,
      "reason": norm_reason,
      "file_hash": file_hash,
  }
  entries.append(record)
  save_entries(ledger_path, entries)
  return record


def _paths_match(entry_file: str, query_files: list[str]) -> bool:
  if not query_files or entry_file == "*":
    return True
  for qf in query_files:
    if entry_file == qf:
      return True
    # Match when one path includes a subdirectory prefix of the other
    if entry_file.endswith("/" + qf) or qf.endswith("/" + entry_file):
      return True
  return False


def _normalize_cl_token(raw: str) -> str:
  tok = raw.strip().lower()
  for prefix in ("cl/", "pr/", "cr/"):
    if tok.startswith(prefix):
      tok = tok[len(prefix) :]
  return tok


def _cl_matches(entry_cl: str, active_cl: str) -> bool:
  a = _normalize_cl_token(entry_cl)
  b = _normalize_cl_token(active_cl)
  if not a or not b:
    return False
  if a == b:
    return True
  # Allow abbreviated Gerrit Change-Ids (>= 8 chars starting with 'i') to match full Change-Id
  if len(a) >= 8 and len(b) >= 8 and a.startswith("i") and b.startswith("i"):
    return a.startswith(b) or b.startswith(a)
  return False


def query_entries(
    repo: str | None = None,
    doc: str | None = None,
    files: list[str] | None = None,
    diff_ref: str | None = None,
    cl: str | None = None,
    limit: int = 15,
) -> tuple[Path, list[dict]]:
  repo_root = resolve_repo_root(repo or doc)
  ledger_path = get_ledger_path(repo=repo, doc=doc)
  all_entries = load_entries(ledger_path)
  if not all_entries:
    return ledger_path, []

  explicit_files = [
      normalize_target_path(f, repo_root) for f in (files or []) if f and f.strip()
  ]
  if doc:
    norm_doc = normalize_target_path(doc, repo_root)
    if norm_doc not in explicit_files:
      explicit_files.append(norm_doc)

  has_file_filter = bool(explicit_files or diff_ref)
  if diff_ref and repo_root:
    diff_files = collect_diff_files(repo_root, diff_ref=diff_ref)
    if explicit_files:
      target_files = [df for df in diff_files if _paths_match(df, explicit_files)]
    else:
      target_files = diff_files
  else:
    target_files = explicit_files

  # Return empty when a diff or file filter resolved to 0 touched files
  if has_file_filter and not target_files:
    return ledger_path, []

  active_cl = detect_cl_id(repo_root, explicit_cl=cl)
  matched: list[dict] = []
  for entry in all_entries:
    scope = entry.get("scope", "repo")
    if scope == "cl" and not _cl_matches(entry.get("cl", ""), active_cl):
      continue
    if target_files and not _paths_match(entry.get("file", ""), target_files):
      continue
    entry_copy = dict(entry)
    recorded_hash = entry_copy.get("file_hash", "")
    current_hash = compute_file_hash(entry_copy.get("file", ""), repo_root)
    if recorded_hash and current_hash:
      entry_copy["freshness"] = (
          "file unchanged"
          if recorded_hash == current_hash
          else "FILE MODIFIED SINCE RECORDED — re-verify if touched lines affect this guard"
      )
    else:
      entry_copy["freshness"] = "untracked"
    matched.append(entry_copy)

  # Keep the most recent entries within the prompt token budget
  matched = list(reversed(matched))[: max(1, limit)]
  return ledger_path, matched


def format_query_markdown(
    entries: list[dict],
    ledger_path: Path | None = None,
    include_empty_header: bool = True,
) -> str:
  key_label = ledger_path.stem if ledger_path else "default"
  if not entries:
    if not include_empty_header:
      return ""
    return f"### Persistent Finding Ledger (`{key_label}`)\n- 0 prior refuted findings for target scope.\n"

  lines = [
      f"### Persistent Finding Ledger (`{key_label}` — {len(entries)} Already Refuted)",
      "The following findings were already verified as false positives or intentional trade-offs.",
      "Do **NOT** re-raise them unless the underlying guard/contract at the cited location changed:",
  ]
  for e in entries:
    sym = f" (`{e['symbol']}`)" if e.get("symbol") else ""
    scope_tag = e.get("scope", "repo")
    if scope_tag == "cl" and e.get("cl"):
      scope_tag = f"cl:{e['cl']}"
    fresh = e.get("freshness", "")
    meta = f"{scope_tag}, {fresh}" if fresh and fresh != "untracked" else scope_tag
    lines.append(f"- **[{e.get('id', 'L???')}]** `{e.get('file', '*')}`{sym} *[{meta}]*")
    lines.append(f"  - **Prior Claim**: {e.get('claim', '')}")
    lines.append(f"  - **Why Refuted**: {e.get('reason', '')}")
  lines.append("")
  return "\n".join(lines)


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  target_parent = argparse.ArgumentParser(add_help=False)
  target_parent.add_argument("--repo", help="Repository root path or repo slug")
  target_parent.add_argument("--doc", help="Document path or Google Doc ID")

  sub = parser.add_subparsers(dest="cmd", required=True)

  p_query = sub.add_parser(
      "query", parents=[target_parent], help="Query refuted findings for diff, files, or document"
  )
  p_query.add_argument("--files", nargs="*", help="Specific file paths to filter by")
  p_query.add_argument("--diff", metavar="REF", help="Git ref to diff against (e.g. HEAD)")
  p_query.add_argument("--cl", help="Explicit CL number, Change-Id, or branch name")
  p_query.add_argument("--limit", type=int, default=15, help="Max entries to return (default: 15)")
  p_query.add_argument("--json", action="store_true", help="Output raw JSON list")

  p_add = sub.add_parser(
      "add", parents=[target_parent], help="Record a refuted finding or user design override"
  )
  p_add.add_argument("--file", required=True, help="Target file path, doc path, or '*' for repo")
  p_add.add_argument("--symbol", default="", help="Function, class, method, or doc section")
  p_add.add_argument("--claim", required=True, help="Concise summary of the refuted finding")
  p_add.add_argument("--reason", required=True, help="Exact file:line guard or rationale")
  p_add.add_argument("--scope", choices=("repo", "cl"), default="repo", help="Visibility scope")
  p_add.add_argument("--cl", help="Explicit CL number, Change-Id, or branch name")
  p_add.add_argument(
      "--category",
      default="skeptic-refuted",
      help="Source category (skeptic-refuted, user-override, false-positive-exec, doc-check)",
  )

  p_list = sub.add_parser(
      "list", parents=[target_parent], help="List entries in a ledger or across all ledgers"
  )
  p_list.add_argument("--all-ledgers", action="store_true", help="List all ledger files")

  p_rm = sub.add_parser(
      "remove", parents=[target_parent], help="Remove a specific entry by ID (e.g. L001)"
  )
  p_rm.add_argument("entry_id", help="Entry ID to remove")

  p_clear = sub.add_parser(
      "clear", parents=[target_parent], help="Clear entries for a CL or an entire ledger"
  )
  p_clear.add_argument("--cl", help="Only clear entries matching this CL/Change-Id")

  args = parser.parse_args()

  if args.cmd == "query":
    lpath, matched = query_entries(
        repo=args.repo,
        doc=args.doc,
        files=args.files,
        diff_ref=args.diff,
        cl=args.cl,
        limit=args.limit,
    )
    if args.json:
      print(json.dumps(matched, indent=2, ensure_ascii=False))
    else:
      print(format_query_markdown(matched, ledger_path=lpath, include_empty_header=True), end="")
    return 0

  if args.cmd == "add":
    rec = add_entry(
        file_target=args.file,
        claim=args.claim,
        reason=args.reason,
        repo=args.repo,
        doc=args.doc,
        symbol=args.symbol,
        scope=args.scope,
        cl=args.cl,
        category=args.category,
    )
    lpath = get_ledger_path(repo=args.repo, doc=args.doc or args.file)
    print(f"Recorded [{rec['id']}] in {lpath.name}: {rec['file']} ({rec['scope']})")
    return 0

  lpath = get_ledger_path(repo=args.repo, doc=args.doc)
  entries = load_entries(lpath)

  if args.cmd == "list":
    if args.all_ledgers:
      files = sorted(get_ledger_dir().glob("*.jsonl"))
      for f in files:
        print(f"- {f.stem} ({len(load_entries(f))} entries): {f}")
      if not files:
        print("No ledger files found.")
      return 0
    print(format_query_markdown(entries, ledger_path=lpath, include_empty_header=True), end="")
    return 0

  if args.cmd == "remove":
    kept = [e for e in entries if e.get("id", "").upper() != args.entry_id.upper()]
    if len(kept) == len(entries):
      print(f"Entry {args.entry_id} not found in {lpath.name}", file=sys.stderr)
      return 1
    save_entries(lpath, kept)
    print(f"Removed {args.entry_id} from {lpath.name} ({len(kept)} remaining)")
    return 0

  if args.cmd == "clear":
    if args.cl:
      kept = [e for e in entries if not _cl_matches(e.get("cl", ""), args.cl)]
      save_entries(lpath, kept)
      print(f"Cleared {len(entries) - len(kept)} entry/entries for CL '{args.cl}' in {lpath.name}")
    else:
      if lpath.exists():
        lpath.unlink()
      print(f"Cleared all entries in {lpath.name}")
    return 0

  return 0


if __name__ == "__main__":
  sys.exit(main())
