#!/usr/bin/env python3
"""Post-review finalization pipeline for clean Gerrit CLs (Variant A).

Separates reversible draft staging from the user's explicit one-click approval:
  1. `stage-thread-drafts`: stages unpublished "Done" (unresolved=false) Gerrit drafts
     on the user's own addressed prior-patchset threads using in-place dedup.
  2. `preview`: read-only dry-run verifying revision SHA, corpus gates, ASAN status,
     drafts across all patchsets, own unresolved threads, companion WIP CL state,
     and deferred post-CR+1 external canaries.
  3. `execute`: re-verifies gates against live Gerrit (TOCTOU guard), stages any
     selected own-thread "Done" drafts, publishes only `draft_ids_to_publish` with
     `PUBLISH_ALL_REVISIONS` + `Code-Review+1` in one atomic Gerrit transaction,
     idempotently abandons open self-owned companion WIP CLs with `notify=NONE`,
     triggers deferred `Canary-G3` if needed, and updates `cl_watcher_state.json`.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Sequence
import urllib.error
import urllib.request

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
  sys.path.insert(0, SCRIPTS_DIR)

CANARY_SCRIPTS_DIR = os.path.normpath(
    os.path.join(SCRIPTS_DIR, "..", "skills", "skia-canary-advisor", "scripts")
)
if CANARY_SCRIPTS_DIR not in sys.path and os.path.isdir(CANARY_SCRIPTS_DIR):
  sys.path.insert(0, CANARY_SCRIPTS_DIR)

import canary_advisor  # pylint: disable=g-import-not-at-top
import triage_store  # pylint: disable=g-import-not-at-top

DEFAULT_GERRIT_HOST = "skia-review.googlesource.com"
SELF_EMAILS = frozenset({"jlavrova@google.com"})
WATCHER_STATE_FILE = triage_store.WATCHER_STATE_FILE
SKIA_CHECKOUT_DIR = canary_advisor.SKIA_DIR
CANARY_ADVISOR_SCRIPT = os.path.join(CANARY_SCRIPTS_DIR, "canary_advisor.py")

G3_PARAGRAPH_EXEC_RE = re.compile(
    r"(?:^|/)modules/(?:skparagraph|skunicode)/(?:(?:src|include)/|BUILD\.bazel$)"
)
G3_REPORT_TITLE = "# Google3 Paragraph Corpus Comparison"
G3_VALID_RE = re.compile(r"Integrity Check\):\*\*\s*`VALID")
G3_REGRESSIONS_RE = re.compile(r"Новых падений \(Regressions\):\s*(\d+)")
REBASE_CUE_RE = re.compile(r"\brebase\b", re.IGNORECASE)

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_TTL_SEC = 240.0


# Fetch LUCI OAuth Bearer token for Gerrit /a/ endpoints with 4-minute caching
def _get_luci_bearer_token(host: str, force_refresh: bool = False) -> str:
  cached = _TOKEN_CACHE.get(host)
  now = time.time()
  if not force_refresh and cached and cached[0] and (now - cached[1]) < _TOKEN_TTL_SEC:
    return cached[0]
  try:
    proc = subprocess.run(
        ["git-credential-luci", "get"],
        input=f"protocol=https\nhost={host}\n\n",
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    for line in proc.stdout.splitlines():
      if line.startswith("password="):
        tok = line.split("=", 1)[1].strip()
        if tok:
          _TOKEN_CACHE[host] = (tok, now)
          return tok
  except (OSError, subprocess.SubprocessError):
    pass
  _TOKEN_CACHE.pop(host, None)
  return ""


# Execute an authenticated Gerrit /a/ REST request and strip the XSSI prefix
def gerrit_api_call(
    host: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 15,
) -> Any:
  rel = path if path.startswith("/") else f"/{path}"
  tok = _get_luci_bearer_token(host)
  if not tok:
    raise RuntimeError(
        f"Missing Gerrit OAuth token for {host}; run `git-credential-luci login`"
    )
  url = f"https://{host}/a{rel}"
  data = json.dumps(payload).encode("utf-8") if payload is not None else None
  base_headers = {"User-Agent": "flight-deck-finalize/1.0"}
  if data is not None:
    base_headers["Content-Type"] = "application/json; charset=UTF-8"

  def _send(bearer: str) -> str:
    req = urllib.request.Request(
        url,
        data=data,
        headers={**base_headers, "Authorization": f"Bearer {bearer}"},
        method=method.upper(),
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
      return resp.read().decode("utf-8", errors="replace")

  try:
    raw = _send(tok)
  except urllib.error.HTTPError as err:
    # Retry once with a freshly minted token if the cached Bearer token expired
    if err.code in (400, 401, 403):
      fresh = _get_luci_bearer_token(host, force_refresh=True)
      if fresh and fresh != tok:
        raw = _send(fresh)
      else:
        detail = err.read().decode("utf-8", errors="replace")[:240]
        raise RuntimeError(f"Gerrit HTTP {err.code} on {method} {rel}: {detail}") from err
    elif err.code == 409 and rel.endswith("/abandon"):
      return {"status": "ABANDONED", "already_abandoned": True}
    else:
      detail = err.read().decode("utf-8", errors="replace")[:240]
      raise RuntimeError(f"Gerrit HTTP {err.code} on {method} {rel}: {detail}") from err

  text = raw.strip()
  if not text:
    return {}
  if text.startswith(")]}'"):
    text = text[4:].lstrip()
  return json.loads(text) if text else {}


# Extract current patchset number, commit SHA, and changed file paths from Gerrit ChangeInfo
def _extract_current_revision(info: dict[str, Any]) -> tuple[int, str, list[str]]:
  revs = info.get("revisions") or {}
  cur_sha = str(info.get("current_revision") or "").strip()
  cur_num = int(info.get("current_revision_number") or 0)
  cur_obj: dict[str, Any] = {}
  if cur_sha and isinstance(revs.get(cur_sha), dict):
    cur_obj = revs[cur_sha]
  elif cur_num and isinstance(revs, dict):
    for sha, rdata in revs.items():
      if isinstance(rdata, dict) and int(rdata.get("_number") or 0) == cur_num:
        cur_sha = str(sha)
        cur_obj = rdata
        break
  if not cur_num and cur_obj:
    cur_num = int(cur_obj.get("_number") or 1)
  files_dict = cur_obj.get("files") if isinstance(cur_obj, dict) else {}
  files = [
      str(p)
      for p in (files_dict or {}).keys()
      if str(p) and str(p) != "/COMMIT_MSG"
  ]
  return max(1, cur_num), cur_sha, files


# Reconstruct comment threads ordered chronologically and tag self-authored roots
def _extract_threads(comments_by_path: dict[str, Any]) -> list[dict[str, Any]]:
  if not isinstance(comments_by_path, dict):
    return []
  all_comments: list[dict[str, Any]] = []
  parent_of: dict[str, str] = {}
  for path, clist in comments_by_path.items():
    if not isinstance(clist, list):
      continue
    for item in clist:
      if not isinstance(item, dict) or not item.get("id"):
        continue
      cid = str(item["id"])
      copy_item = dict(item)
      copy_item["_path"] = str(path)
      all_comments.append(copy_item)
      if item.get("in_reply_to"):
        parent_of[cid] = str(item["in_reply_to"])

  def _root_id(cid: str) -> str:
    seen: set[str] = set()
    cur = cid
    for _ in range(64):
      if cur not in parent_of or cur in seen:
        break
      seen.add(cur)
      cur = parent_of[cur]
    return cur

  grouped: dict[str, list[dict[str, Any]]] = {}
  for c in all_comments:
    grouped.setdefault(_root_id(str(c["id"])), []).append(c)

  threads: list[dict[str, Any]] = []
  for root_id, items in grouped.items():
    items.sort(
        key=lambda x: (str(x.get("updated") or ""), int(x.get("patch_set") or 0))
    )
    root = items[0]
    leaf = items[-1]
    root_email = str(((root.get("author") or {}).get("email")) or "").lower()
    leaf_email = str(((leaf.get("author") or {}).get("email")) or "").lower()
    root_msg = str(root.get("message") or "").strip()
    threads.append({
        "root_id": root_id,
        "leaf_id": str(leaf.get("id") or root_id),
        "patchset": int(leaf.get("patch_set") or root.get("patch_set") or 1),
        "root_patchset": int(root.get("patch_set") or 1),
        "path": str(leaf.get("_path") or root.get("_path") or "/PATCHSET_LEVEL"),
        "line": leaf.get("line") if leaf.get("line") else root.get("line"),
        "unresolved": bool(leaf.get("unresolved", False)),
        "is_own_root": root_email in SELF_EMAILS,
        "has_self_comment": any(
            str(((c.get("author") or {}).get("email")) or "").lower() in SELF_EMAILS
            for c in items
        ),
        "root_author": root_email,
        "last_author": leaf_email,
        "root_message": root_msg,
        "root_snippet": " ".join(root_msg.split())[:140],
        "is_rebase_request": bool(REBASE_CUE_RE.search(root_msg)),
        "comment_ids": {str(c.get("id") or "") for c in items if c.get("id")},
    })
  threads.sort(key=lambda t: (int(t["patchset"]), str(t["path"]), str(t["root_id"])))
  return threads


# Upsert a Gerrit draft in place on revisions/{ps}/drafts to avoid stacking duplicates
def _upsert_gerrit_draft(
    host: str,
    cl_id: str,
    patchset: int,
    *,
    path: str,
    message: str,
    unresolved: bool,
    line: int | None = None,
    in_reply_to: str | None = None,
    api_fn: Callable[..., Any] = gerrit_api_call,
) -> dict[str, Any]:
  drafts_path = f"/changes/{cl_id}/revisions/{int(patchset)}/drafts"
  existing_by_path = api_fn(host, "GET", drafts_path) or {}
  target_key = (int(line) if line else None, str(in_reply_to) if in_reply_to else None)
  existing_id = ""
  if isinstance(existing_by_path, dict):
    for d in existing_by_path.get(path) or ():
      if not isinstance(d, dict):
        continue
      d_key = (
          int(d["line"]) if d.get("line") else None,
          str(d["in_reply_to"]) if d.get("in_reply_to") else None,
      )
      if d_key == target_key and d.get("id"):
        existing_id = str(d["id"])
        break

  payload: dict[str, Any] = {
      "path": path,
      "message": message,
      "unresolved": bool(unresolved),
  }
  if line:
    payload["line"] = int(line)
  if in_reply_to:
    payload["in_reply_to"] = str(in_reply_to)

  put_path = f"{drafts_path}/{existing_id}" if existing_id else drafts_path
  saved = api_fn(host, "PUT", put_path, payload) or {}
  draft_id = str(saved.get("id") or existing_id)
  return {
      "draft_id": draft_id,
      "patchset": int(patchset),
      "path": path,
      "line": int(line) if line else None,
      "in_reply_to": str(in_reply_to) if in_reply_to else None,
      "message": message,
      "unresolved": bool(unresolved),
  }


# Stage unpublished "Done" (unresolved=false) drafts on own addressed prior-PS threads
def stage_own_thread_drafts(
    target_id: str,
    *,
    host: str = DEFAULT_GERRIT_HOST,
    thread_ids: Sequence[str] | None = None,
    auto_rebase: bool = False,
    all_own_prior: bool = False,
    api_fn: Callable[..., Any] = gerrit_api_call,
) -> list[dict[str, Any]]:
  cl_id = str(target_id).strip()
  info = api_fn(
      host,
      "GET",
      f"/changes/{cl_id}?o=DETAILED_ACCOUNTS&o=ALL_REVISIONS&o=DETAILED_LABELS",
  )
  cur_ps, _, _ = _extract_current_revision(info)
  mergeable = info.get("mergeable")
  comments = api_fn(host, "GET", f"/changes/{cl_id}/comments") or {}
  threads = _extract_threads(comments)
  wanted_ids = {str(x).strip() for x in (thread_ids or ()) if str(x).strip()}

  staged: list[dict[str, Any]] = []
  for th in threads:
    if not th["unresolved"] or not th["is_own_root"]:
      continue
    if int(th["patchset"]) >= cur_ps:
      continue
    matched_explicit = bool(wanted_ids & (th["comment_ids"] | {th["root_id"], th["leaf_id"]}))
    matched_rebase = (
        auto_rebase and th["is_rebase_request"] and mergeable is not False
    )
    if not (matched_explicit or matched_rebase or all_own_prior):
      continue
    saved = _upsert_gerrit_draft(
        host,
        cl_id,
        int(th["patchset"]),
        path=str(th["path"]),
        line=th["line"],
        in_reply_to=str(th["leaf_id"]),
        message="Done",
        unresolved=False,
        api_fn=api_fn,
    )
    saved["root_id"] = th["root_id"]
    saved["root_snippet"] = th["root_snippet"]
    staged.append(saved)
  return staged


# Verify Google3 Paragraph A/B compare.md attestation for SkParagraph/SkUnicode CLs on Cloudtop
def _evaluate_g3_corpus_gate(
    cl_id: str,
    cur_ps: int,
    cur_sha: str,
    files: Sequence[str],
    subject: str,
    triage: dict[str, Any],
    search_dirs: Sequence[Path | str],
    has_citc: bool,
) -> dict[str, Any]:
  if not has_citc or re.search(r"^revert\b", subject.strip(), re.I):
    return {"required": False, "status": "not_required", "compare_md": ""}
  exec_files = [
      f
      for f in files
      if not triage_store.TEST_FILE_PATH_RE.search(f) and G3_PARAGRAPH_EXEC_RE.search(f)
  ]
  if not exec_files:
    return {"required": False, "status": "not_required", "compare_md": ""}

  g3_status = str(triage.get("g3_corpus_status") or "").lower()
  g3_tested_ps = int(
      triage.get("g3_corpus_tested_patchset") or triage.get("triaged_patchset") or 0
  )
  triaged_rev = str(triage.get("triaged_revision") or "").strip()
  # A manual waiver (e.g. a flake proven on baseline) outranks the failed compare.md it
  # explains, but only on the patchset and revision it was recorded for
  if (
      g3_status == "waived"
      and g3_tested_ps == cur_ps
      and (not triaged_rev or triage_store._sha_matches(cur_sha, triaged_rev))
  ):
    return {"required": True, "status": "waived", "compare_md": ""}

  cand_paths: list[Path] = []
  explicit = str(triage.get("g3_corpus_compare_md") or "").strip()
  if explicit:
    cand_paths.append(Path(explicit))
  seen: set[Path] = set()
  dir_candidates: list[tuple[float, Path]] = []
  for raw_d in search_dirs:
    d = Path(raw_d)
    if not d.is_dir():
      continue
    for p in (d / "compare.md", *d.glob("g3*/compare.md"), *d.glob("*g3*/compare.md")):
      try:
        rp = p.resolve()
        if rp not in seen and rp.is_file():
          seen.add(rp)
          dir_candidates.append((rp.stat().st_mtime, rp))
      except OSError:
        continue
  dir_candidates.sort(key=lambda x: x[0], reverse=True)
  cand_paths.extend(p for _, p in dir_candidates)

  cl_ps_token = f"cl:{cl_id}/{cur_ps}".lower()
  short_sha = cur_sha[:7].lower() if len(cur_sha) >= 7 else ""
  for p in cand_paths:
    if not p.is_file():
      continue
    try:
      text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
      continue
    if G3_REPORT_TITLE not in text:
      continue
    low = text.lower()
    if cl_ps_token not in low and (not short_sha or short_sha not in low):
      continue
    valid = bool(G3_VALID_RE.search(text))
    regs = [int(n) for n in G3_REGRESSIONS_RE.findall(text)]
    if valid and regs and all(n == 0 for n in regs):
      return {"required": True, "status": "passed", "compare_md": str(p)}
    return {"required": True, "status": "failed", "compare_md": str(p)}

  # Trust persisted g3_corpus_status on the exact patchset if /tmp compare.md was cleaned up
  if g3_status == "passed" and g3_tested_ps == cur_ps:
    return {"required": True, "status": "passed", "compare_md": explicit}
  return {"required": True, "status": "pending", "compare_md": ""}


# Check Buildbucket status for mandatory ASAN trybot on Skia C++ changes
def _evaluate_asan_gate(
    host: str,
    cl_id: str,
    cur_ps: int,
    files: Sequence[str],
    bb_fn: Callable[[str, int], dict[str, Any]],
) -> dict[str, Any]:
  if host != DEFAULT_GERRIT_HOST:
    return {"required": False, "status": "not_required", "bot": "", "build_id": ""}
  eff_files = [p for p in files if not canary_advisor.NO_EFFECT.search(p)]
  asan_bot, _ = canary_advisor.choose_asan_bot(files)
  if not asan_bot and not eff_files:
    return {"required": False, "status": "not_required", "bot": "", "build_id": ""}
  if not asan_bot:
    return {"required": False, "status": "not_required", "bot": "", "build_id": ""}

  builds = bb_fn(cl_id, cur_ps) or {}
  b_info = builds.get(asan_bot) or {}
  b_status = str(b_info.get("status") or "").upper()
  b_id = str(b_info.get("id") or "")
  if b_status == "SUCCESS":
    return {"required": True, "status": "passed", "bot": asan_bot, "build_id": b_id}
  if b_status in ("SCHEDULED", "STARTED"):
    return {"required": True, "status": "running", "bot": asan_bot, "build_id": b_id}
  if b_status:
    return {
        "required": True,
        "status": "failed",
        "bot": asan_bot,
        "build_id": b_id,
        "raw_status": b_status,
    }
  return {"required": True, "status": "missing", "bot": asan_bot, "build_id": ""}


# Compute external-author canaries that were deferred until Googler CR+1 (Canary-G3)
def _compute_deferred_canaries(
    host: str,
    owner_email: str,
    files: Sequence[str],
    builds: dict[str, Any],
    has_citc: bool,
) -> list[str]:
  if host != DEFAULT_GERRIT_HOST or owner_email.lower().endswith("@google.com"):
    return []
  effective = [p for p in files if not canary_advisor.NO_EFFECT.search(p)]
  g3_paths: list[str] = []
  for path in effective:
    for rx, canaries, _, _ in canary_advisor.AREA_RULES:
      if re.search(rx, path):
        if "Canary-G3" in canaries:
          if not (has_citc and canary_advisor.G3_PARAGRAPH_PATH_RE.search(path)):
            g3_paths.append(path)
        break
  if not g3_paths:
    return []
  existing = builds.get("Canary-G3") or {}
  if str(existing.get("status") or "").upper() in ("SUCCESS", "STARTED", "SCHEDULED"):
    return []
  return ["Canary-G3"]


# Walk backwards from cur_ps across message/rebase-only revisions that keep CQ dry-run equivalence
def compute_equivalent_patchsets(info: dict[str, Any], cur_ps: int) -> list[int]:
  kind_by_ps: dict[int, str] = {}
  revs = info.get("revisions") or {}
  if isinstance(revs, dict):
    for r in revs.values():
      if isinstance(r, dict) and r.get("_number"):
        kind_by_ps[int(r["_number"])] = str(r.get("kind") or "").upper()
  revs_by_ps = info.get("revisions_by_ps") or {}
  if isinstance(revs_by_ps, dict):
    for k, r in revs_by_ps.items():
      try:
        ps_k = int(k)
      except (ValueError, TypeError):
        continue
      if isinstance(r, dict) and ps_k not in kind_by_ps:
        kind_by_ps[ps_k] = str(r.get("kind") or "").upper()

  eq_ps: set[int] = {max(1, int(cur_ps or 1))}
  ps = max(1, int(cur_ps or 1))
  while ps > 1:
    if kind_by_ps.get(ps) in ("NO_CHANGE", "NO_CODE_CHANGE", "TRIVIAL_REBASE"):
      eq_ps.add(ps - 1)
      ps -= 1
    else:
      break
  return sorted(eq_ps)


AUTHORS_EMAIL_PATTERN_RE = re.compile(r" <([^>]+)>")
BEHDAD_EXEMPT_AUTHORS = frozenset({
    "behdad.esfahbod@gmail.com",
    "behdad",
    "behdad esfahbod",
})


def _evaluate_authors_gate(
    host: str,
    info: dict[str, Any],
    files: Sequence[str],
    authors_text_override: str | None = None,
) -> dict[str, Any]:
  """Verify external Gerrit author presence in AUTHORS before CR+1 finalization."""
  owner_obj = info.get("owner") or {}
  owner_email = str(owner_obj.get("email") or "").strip()
  owner_name = str(owner_obj.get("name") or "").strip()
  cur_sha = str(info.get("current_revision") or "")
  cur_rev = ((info.get("revisions") or {}).get(cur_sha)) or {}
  commit_author = ((cur_rev.get("commit") or {}).get("author")) or {}
  commit_email = str(commit_author.get("email") or "").strip()
  eff_email = commit_email or owner_email
  low_email = eff_email.lower()
  low_name = owner_name.lower()
  is_ext = (
      bool(low_email and "@" in low_email)
      and not low_email.endswith(("@google.com", "@chromium.org", ".gserviceaccount.com"))
      and not low_email.startswith(("skia-autoroll@", "chromium-autoroll@", "autoroll"))
  )
  if not is_ext or not any(h in str(host or "").lower() for h in ("skia-review", "chromium-review")):
    return {"required": False, "status": "not_required", "email": eff_email}
  if low_email in BEHDAD_EXEMPT_AUTHORS or low_name in BEHDAD_EXEMPT_AUTHORS:
    return {"required": False, "status": "exempt", "email": eff_email}
  if "AUTHORS" in (files or ()):
    return {"required": True, "status": "passed", "source": "cl_files", "email": eff_email}
  if authors_text_override is not None:
    body = authors_text_override
  else:
    authors_path = os.path.join(SKIA_CHECKOUT_DIR, "AUTHORS")
    if not os.path.isfile(authors_path):
      return {"required": False, "status": "skipped_no_file", "email": eff_email}
    try:
      with open(authors_path, "r", encoding="utf-8", errors="replace") as fh:
        body = fh.read()
    except OSError:
      return {"required": False, "status": "skipped_no_file", "email": eff_email}
  for raw_line in body.splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
      continue
    m = AUTHORS_EMAIL_PATTERN_RE.search(line)
    if not m:
      continue
    pat = m.group(1).strip().replace(".", r"\.").replace("*", ".*")
    try:
      if re.match(rf"^{pat}$", eff_email, re.IGNORECASE):
        return {
            "required": True,
            "status": "passed",
            "source": "repo_authors",
            "matched_entry": line,
            "email": eff_email,
        }
    except re.error:
      continue
  return {"required": True, "status": "missing", "email": eff_email}


# Evaluate SkCQ and LUCI CV dry-run status across equivalent patchsets of cur_ps
def evaluate_cq_dry_run_gate(
    info: dict[str, Any], cur_ps: int | None = None
) -> dict[str, Any]:
  eff_ps = int(cur_ps) if cur_ps is not None else _extract_current_revision(info)[0]
  eq_list = compute_equivalent_patchsets(info, eff_ps)
  eq_set = set(eq_list)

  cq_all = ((info.get("labels") or {}).get("Commit-Queue") or {}).get("all") or []
  max_cq = max(
      (
          int(a.get("value") or 0)
          for a in cq_all
          if isinstance(a, dict) and a.get("value")
      ),
      default=0,
  )
  if max_cq >= 2:
    return {
        "required": True,
        "status": "passed",
        "detail": "CQ+2 active",
        "equivalent_patchsets": eq_list,
    }
  if max_cq == 1:
    return {
        "required": True,
        "status": "running",
        "detail": "CQ+1 (Dry Run) running",
        "equivalent_patchsets": eq_list,
    }

  messages = info.get("messages") or []
  for m in reversed(messages):
    if not isinstance(m, dict):
      continue
    m_ps = int(m.get("_revision_number") or 0)
    if m_ps not in eq_set:
      continue
    txt = str(m.get("message") or "")
    tag = str(m.get("tag") or "")
    is_fail = (
        "Removing from SkCQ because verifiers have failed:" in txt
        or "This CL failed the SkCQ" in txt
        or "Failed builds:" in txt
        or "Tryjobs failed" in txt
        or (
            tag.startswith("autogenerated:cq:")
            and (
                "This CL has failed the run" in txt
                or "Failed Tryjobs:" in txt
                or "Dry run: This CL failed" in txt
            )
        )
    )
    if is_fail:
      return {
          "required": True,
          "status": "failed",
          "detail": " ".join(txt.split())[:180],
          "equivalent_patchsets": eq_list,
      }
    if "passed the SkCQ dry run" in txt or "passed the CQ dry run" in txt:
      ps_note = f" (on equivalent PS{m_ps})" if m_ps != eff_ps else ""
      return {
          "required": True,
          "status": "passed",
          "detail": f"Dry Run passed{ps_note}",
          "equivalent_patchsets": eq_list,
      }

  return {
      "required": True,
      "status": "not_run",
      "detail": "Not run on current patchset",
      "equivalent_patchsets": eq_list,
  }


# Lookup parent and companion records in cl_watcher_state.json
def _load_watcher_context(
    cl_id: str, state_path: Path | str | None
) -> tuple[dict[str, Any], list[str]]:
  if not state_path:
    return {}, []
  sp = Path(state_path)
  if not sp.is_file():
    return {}, []
  try:
    state = json.loads(sp.read_text(encoding="utf-8", errors="replace"))
  except Exception:
    return {}, []
  items = (state.get("items") or {}) if isinstance(state, dict) else {}
  parent_rec: dict[str, Any] = {}
  comp_ids: list[str] = []
  parent_ikey = f"gerrit:{cl_id}"
  for rec in items.values():
    if not isinstance(rec, dict):
      continue
    if rec.get("item_key") == parent_ikey and not parent_rec:
      parent_rec = rec
      for c in rec.get("companions") or ():
        if isinstance(c, dict) and c.get("target_id"):
          cid = str(c["target_id"]).strip()
          if cid and cid not in comp_ids:
            comp_ids.append(cid)
    elif rec.get("companion_to") == parent_ikey and rec.get("target_id"):
      cid = str(rec["target_id"]).strip()
      if cid and cid not in comp_ids:
        comp_ids.append(cid)
  return parent_rec, comp_ids


# Build a complete, side-effect-free dry-run preview of post-review finalization
def build_finalize_preview(
    target_id: str,
    *,
    host: str = DEFAULT_GERRIT_HOST,
    results_dir: Path | str | None = None,
    state_path: Path | str | None = None,
    search_dirs: Sequence[Path | str] | None = None,
    has_citc: bool | None = None,
    api_fn: Callable[..., Any] = gerrit_api_call,
    bb_fn: Callable[[str, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
  cl_id = str(target_id).strip()
  eff_state_path = (
      state_path
      if state_path is not None
      else (WATCHER_STATE_FILE if results_dir is None else None)
  )
  eff_search_dirs: list[Path | str] = (
      list(search_dirs) if search_dirs is not None else [Path("/tmp")]
  )
  eff_citc = canary_advisor.has_cloudtop_citc() if has_citc is None else bool(has_citc)
  eff_bb_fn = bb_fn or canary_advisor.bb_cl_builds

  triage = triage_store.load_triage_result("gerrit", cl_id, results_dir=results_dir) or {}
  parent_rec, state_comp_ids = _load_watcher_context(cl_id, eff_state_path)
  if not host and parent_rec.get("host"):
    host = str(parent_rec["host"])

  info = api_fn(
      host,
      "GET",
      f"/changes/{cl_id}?o=DETAILED_ACCOUNTS&o=ALL_REVISIONS&o=CURRENT_FILES"
      "&o=DETAILED_LABELS&o=SUBMIT_REQUIREMENTS&o=MESSAGES",
  )
  cur_ps, cur_sha, files = _extract_current_revision(info)
  if not files and isinstance(parent_rec.get("files"), list):
    files = [str(p) for p in parent_rec["files"] if str(p)]
  status = str(info.get("status") or "NEW").upper()
  is_wip = bool(info.get("work_in_progress", False))
  mergeable = info.get("mergeable")
  subject = str(info.get("subject") or triage.get("subject") or "")
  owner_email = str(((info.get("owner") or {}).get("email")) or "")

  cr_all = ((info.get("labels") or {}).get("Code-Review") or {}).get("all") or []
  self_vote = 0
  for v in cr_all:
    if isinstance(v, dict) and str(v.get("email") or "").lower() in SELF_EMAILS:
      self_vote = int(v.get("value") or 0)

  blockers: list[str] = []
  if status != "NEW":
    blockers.append(f"CL status is {status} (expected NEW)")
  if is_wip:
    blockers.append("CL is marked Work-In-Progress")
  if self_vote > 0:
    blockers.append(f"Already voted Code-Review+{self_vote} on PS{cur_ps}")

  outcome = str(triage.get("review_outcome") or triage.get("verdict") or "").upper()
  if outcome != "CLEAN":
    blockers.append(
        f"Review outcome in triage_store is {outcome or 'missing'} (expected CLEAN)"
    )

  triaged_ps = int(triage.get("triaged_patchset") or 0)
  triaged_rev = str(triage.get("triaged_revision") or "").strip()
  if triaged_ps and triaged_ps != cur_ps:
    blockers.append(
        f"Patchset drifted since review: triaged PS{triaged_ps}, current is PS{cur_ps}"
    )
  if triaged_rev and cur_sha and not triage_store._sha_matches(cur_sha, triaged_rev):
    blockers.append(
        f"Revision SHA drifted: triaged {triaged_rev[:10]}, current is {cur_sha[:10]}"
    )

  # Evaluate Flutter/WebParagraph corpus gate
  fg = triage_store.detect_corpus_from_files(files, subject)
  corpus_req = bool(triage.get("corpus_required")) or bool(fg.get("required"))
  corpus_script = str(triage.get("corpus_script") or fg.get("script") or "")
  c_status = str(triage.get("corpus_status") or "").lower()
  if not corpus_req:
    c_status = "not_required"
  else:
    tested_ps = int(triage.get("corpus_tested_patchset") or triaged_ps or 0)
    if c_status == "passed" and tested_ps != cur_ps:
      c_status = "pending"
    if c_status not in ("passed", "waived"):
      ev = triage_store.find_matching_compare_md(
          eff_search_dirs,
          corpus_script or "run_flutter_paragraph_tests.py",
          cl_id,
          cur_ps,
          patchset_sha=cur_sha,
      )
      if ev and ev.get("status") == "passed":
        c_status = "passed"
    if c_status not in ("passed", "waived"):
      cname = triage_store.corpus_display_name(corpus_script)
      blockers.append(f"{cname} gate is {c_status or 'pending'} on PS{cur_ps}")

  # Evaluate Google3 Paragraph A/B corpus gate on Cloudtop
  g3_gate = _evaluate_g3_corpus_gate(
      cl_id, cur_ps, cur_sha, files, subject, triage, eff_search_dirs, eff_citc
  )
  if g3_gate["required"] and g3_gate["status"] not in ("passed", "waived"):
    blockers.append(
        f"Google3 Paragraph A/B corpus is {g3_gate['status']} on PS{cur_ps}"
    )

  # Evaluate Buildbucket ASAN gate (cache builds for Canary-G3 check below)
  cached_builds: dict[str, Any] = {}

  def _memo_bb(cid: str, ps_num: int) -> dict[str, Any]:
    if not cached_builds:
      cached_builds.update(eff_bb_fn(cid, ps_num) or {})
    return cached_builds

  asan_gate = _evaluate_asan_gate(host, cl_id, cur_ps, files, _memo_bb)
  if asan_gate["required"] and asan_gate["status"] != "passed":
    bot_name = asan_gate.get("bot") or "ASAN"
    st_desc = asan_gate.get("raw_status") or asan_gate["status"]
    blockers.append(f"ASAN gate not green on PS{cur_ps}: {bot_name} ({st_desc})")

  comments_raw = api_fn(host, "GET", f"/changes/{cl_id}/comments") or {}
  threads = _extract_threads(comments_raw)
  own_threads_by_leaf = {
      t["leaf_id"]: t for t in threads if t["unresolved"] and t["is_own_root"]
  }

  drafts_raw = api_fn(host, "GET", f"/changes/{cl_id}/drafts") or {}
  explicit_draft_ids = set(triage.get("draft_ids") or ())
  drafts_list: list[dict[str, Any]] = []
  covered_leaf_ids: set[str] = set()
  has_cur_ps_draft = False

  if isinstance(drafts_raw, dict):
    for path, d_items in drafts_raw.items():
      if not isinstance(d_items, list):
        continue
      for d in d_items:
        if not isinstance(d, dict) or not d.get("id"):
          continue
        did = str(d["id"])
        dps = int(d.get("patch_set") or cur_ps)
        d_unres = bool(d.get("unresolved", False))
        in_rep = str(d.get("in_reply_to") or "")
        msg = str(d.get("message") or "")
        if in_rep:
          covered_leaf_ids.add(in_rep)
        if dps == cur_ps:
          has_cur_ps_draft = True

        is_own_thread_done = (
            dps < cur_ps
            and not d_unres
            and in_rep in own_threads_by_leaf
        )
        selected = False
        warning = ""
        if did in explicit_draft_ids:
          selected = True
        elif dps == cur_ps and not d_unres:
          selected = True
        elif is_own_thread_done:
          selected = True
        else:
          warning = (
              f"Unexpected draft on PS{dps}"
              + (" (unresolved=true)" if d_unres else "")
              + " — unchecked by default"
          )

        drafts_list.append({
            "id": did,
            "patchset": dps,
            "path": str(path),
            "line": d.get("line"),
            "in_reply_to": in_rep or None,
            "unresolved": d_unres,
            "message": msg,
            "snippet": " ".join(msg.split())[:180],
            "selected_by_default": selected,
            "warning": warning,
        })
  drafts_list.sort(key=lambda x: (-int(x["patchset"]), str(x["path"]), str(x["id"])))

  triage_draft_fallback = ""
  if not has_cur_ps_draft and str(triage.get("draft") or "").strip():
    triage_draft_fallback = str(triage["draft"]).strip()

  resolved_hint_ids = set(triage.get("resolved_thread_ids") or ())
  own_unresolved: list[dict[str, Any]] = []
  other_unresolved: list[dict[str, Any]] = []
  for th in threads:
    if not th["unresolved"]:
      continue
    if th["is_own_root"] and int(th["patchset"]) < cur_ps:
      if th["leaf_id"] in covered_leaf_ids:
        continue
      # Default-check own prior-PS threads on a CLEAN follow-up unless merge conflict persists
      can_close = not (th["is_rebase_request"] and mergeable is False)
      default_sel = can_close and (
          outcome == "CLEAN"
          or bool(resolved_hint_ids & (th["comment_ids"] | {th["root_id"], th["leaf_id"]}))
          or th["is_rebase_request"]
      )
      own_unresolved.append({
          "root_id": th["root_id"],
          "leaf_id": th["leaf_id"],
          "patchset": th["patchset"],
          "path": th["path"],
          "line": th["line"],
          "root_snippet": th["root_snippet"],
          "is_rebase_request": th["is_rebase_request"],
          "selected_by_default": default_sel,
      })
    elif not th["is_own_root"]:
      other_unresolved.append({
          "root_id": th["root_id"],
          "patchset": th["patchset"],
          "path": th["path"],
          "root_author": th["root_author"],
          "root_snippet": th["root_snippet"],
      })

  # Inspect companion WIP CLs in live Gerrit
  comp_ids: list[str] = []
  tr_comp = str(triage.get("companion_id") or "").strip()
  if tr_comp:
    comp_ids.append(tr_comp)
  for sc in state_comp_ids:
    if sc not in comp_ids:
      comp_ids.append(sc)

  companions: list[dict[str, Any]] = []
  for cid in comp_ids:
    if not cid.isdigit():
      continue
    try:
      c_info = api_fn(host, "GET", f"/changes/{cid}?o=DETAILED_ACCOUNTS") or {}
    except Exception as exc:
      companions.append({
          "id": cid,
          "url": f"https://{host}/c/skia/+/{cid}",
          "status": "ERROR",
          "action": "skip_error",
          "label": f"Lookup failed ({exc})",
          "selected_by_default": False,
      })
      continue
    c_status_val = str(c_info.get("status") or "").upper()
    c_owner = str(((c_info.get("owner") or {}).get("email")) or "").lower()
    c_subj = str(c_info.get("subject") or "")
    c_url = f"https://{host}/c/skia/+/{cid}"
    if c_status_val in ("ABANDONED", "MERGED"):
      companions.append({
          "id": cid,
          "url": c_url,
          "status": c_status_val,
          "subject": c_subj,
          "action": "noop_already_closed",
          "label": f"Already {c_status_val.lower()} (no-op)",
          "selected_by_default": False,
      })
    elif c_owner in SELF_EMAILS and c_status_val == "NEW":
      companions.append({
          "id": cid,
          "url": c_url,
          "status": c_status_val,
          "subject": c_subj,
          "action": "abandon",
          "label": "Abandon companion WIP CL (notify=NONE)",
          "selected_by_default": True,
      })
    else:
      companions.append({
          "id": cid,
          "url": c_url,
          "status": c_status_val,
          "subject": c_subj,
          "action": "skip_not_owned",
          "label": f"Owned by {c_owner or 'other'} — never auto-abandon",
          "selected_by_default": False,
      })

  deferred_canaries = _compute_deferred_canaries(
      host,
      owner_email,
      files,
      _memo_bb(cl_id, cur_ps) if host == DEFAULT_GERRIT_HOST else {},
      eff_citc,
  )
  authors_gate = _evaluate_authors_gate(host, info, files)
  if authors_gate["required"] and authors_gate["status"] == "missing":
    blockers.append(
        f"External author {authors_gate['email']} is missing from AUTHORS "
        "(verify CLA + add to AUTHORS before CR+1)"
    )
  cq_gate = evaluate_cq_dry_run_gate(info, cur_ps=cur_ps)
  raw_armed = parent_rec.get("armed_finalize")
  armed_finalize = None
  if (
      isinstance(raw_armed, dict)
      and status == "NEW"
      and not is_wip
      and self_vote <= 0
      and int(raw_armed.get("patchset") or 0) in cq_gate["equivalent_patchsets"]
      and (
          not raw_armed.get("expected_revision")
          or not cur_sha
          or triage_store._sha_matches(
              cur_sha, str(raw_armed.get("expected_revision") or "")
          )
      )
  ):
    armed_finalize = dict(raw_armed)

  return {
      "ok": True,
      "ready": len(blockers) == 0,
      "blockers": blockers,
      "target_id": cl_id,
      "host": host,
      "url": f"https://{host}/c/skia/+/{cl_id}/{cur_ps}",
      "subject": subject,
      "owner_email": owner_email,
      "is_external": not owner_email.lower().endswith("@google.com"),
      "patchset": cur_ps,
      "revision": cur_sha,
      "review_outcome": outcome,
      "cq_dry_run_passed": cq_gate["status"] == "passed",
      "armed_finalize": armed_finalize,
      "gates": {
          "flutter_corpus": {"required": corpus_req, "status": c_status},
          "g3_corpus": g3_gate,
          "asan": asan_gate,
          "authors": authors_gate,
          "cq_dry_run": cq_gate,
      },
      "drafts": drafts_list,
      "triage_draft_fallback": triage_draft_fallback,
      "own_unresolved_threads": own_unresolved,
      "other_unresolved_threads": other_unresolved,
      "companions": companions,
      "deferred_canaries": deferred_canaries,
  }


# Default command runner for post-CR+1 `git cl try` and detached `canary_advisor.py watch`
def _default_cmd_runner(
    cmd: Sequence[str], background: bool = False
) -> tuple[int, str]:
  cwd = SKIA_CHECKOUT_DIR if os.path.isdir(SKIA_CHECKOUT_DIR) else "/tmp"
  if background:
    subprocess.Popen(
        list(cmd),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return 0, "started_background"
  proc = subprocess.run(
      list(cmd),
      cwd=cwd,
      stdin=subprocess.DEVNULL,
      capture_output=True,
      text=True,
      timeout=90,
      check=False,
  )
  return proc.returncode, (proc.stdout + "\n" + proc.stderr).strip()


# Re-enrich precomputed entity_state dicts before writing cl_watcher_state.json
def _write_watcher_state_atomic(sp: Path, state: dict[str, Any]) -> None:
  try:
    import entity_state as _es  # pylint: disable=g-import-not-at-top

    _es.enrich_entity_states(state)
  except Exception:
    pass
  tmp_p = sp.parent / f"{sp.name}.tmp.{os.getpid()}"
  with open(tmp_p, "w", encoding="utf-8") as fh:
    json.dump(state, fh, indent=2, ensure_ascii=False)
  tmp_p.replace(sp)


# Synchronize cl_watcher_state.json immediately so Flight Deck cards update without poll lag
def _update_watcher_state_after_finalize(
    state_path: Path | str | None,
    *,
    cl_id: str,
    cur_ps: int,
    resolved_count: int,
    abandoned_comp_ids: Sequence[str],
    triggered_canaries: Sequence[str],
) -> None:
  if not state_path:
    return
  sp = Path(state_path)
  if not sp.is_file():
    return
  try:
    state = json.loads(sp.read_text(encoding="utf-8", errors="replace"))
  except Exception:
    return
  if not isinstance(state, dict):
    return

  items = state.get("items") or {}
  parent_ikey = f"gerrit:{cl_id}"
  abandoned_ikeys = {f"gerrit:{cid}" for cid in abandoned_comp_ids}
  iso_now, et_now = triage_store.now_et_strings()

  for rec in items.values():
    if not isinstance(rec, dict):
      continue
    if rec.get("item_key") == parent_ikey:
      rec.pop("armed_finalize", None)
      rec.pop("armed_finalize_error", None)
      rec["self_cr_vote"] = 1
      rec["self_approved"] = True
      rec["self_approval_ps"] = cur_ps
      rec["self_review_state"] = "approved"
      rec["self_review_ps"] = cur_ps
      prev_app = int(rec.get("approved_count") or 0)
      req_app = max(1, int(rec.get("required_approvals") or 1))
      new_app = min(req_app, prev_app + 1)
      rec["approved_count"] = new_app
      rec["missing_approvals"] = max(0, req_app - new_app)
      if rec["missing_approvals"] > 0:
        rem = rec["missing_approvals"]
        rec["reviewer_status"] = f"Needs +{rem} CR ({new_app}/{req_app} approved)"
      else:
        rec["reviewer_status"] = f"Approved ({new_app}/{req_app})"

      unres_before = int(rec.get("self_threads_unresolved") or 0)
      closed_now = min(unres_before, int(resolved_count))
      rec["self_threads_unresolved"] = max(0, unres_before - closed_now)
      rec["self_threads_resolved"] = int(rec.get("self_threads_resolved") or 0) + closed_now

      if abandoned_ikeys and isinstance(rec.get("companions"), list):
        rec["companions"] = [
            c
            for c in rec["companions"]
            if isinstance(c, dict) and c.get("item_key") not in abandoned_ikeys
        ]

      # Keep attention only when external CL still needs a 2nd Googler assigned by us
      if rec.get("is_external") and int(rec.get("missing_reviewers_to_assign") or 0) > 0:
        rec["sponsor_action"] = "assign_second_reviewer"
        rec["sponsor_action_reason"] = "Add 2nd Googler reviewer"
        rec["in_my_attention"] = True
        rec["phase"] = "NEEDS_YOUR_ACTION"
      else:
        rec["in_my_attention"] = False
        rec["phase"] = "WAITING_ON_OTHERS"
      rec["status_changed_raw"] = iso_now
      rec["status_changed_et"] = et_now
      rec["status_changed_age"] = "just now"
    elif rec.get("item_key") in abandoned_ikeys:
      rec["status"] = "ABANDONED"
      rec["phase"] = "ABANDONED"
      rec["in_my_attention"] = False
      rec["ready_to_abandon"] = False
      rec["auto_abandon_eligible"] = False
      rec["companion_state"] = "abandoned"

  events = state.setdefault("events", [])
  parts = [f"CR+1 & published review on gerrit:{cl_id} (PS{cur_ps})"]
  if resolved_count:
    parts.append(f"closed {resolved_count} own thread(s)")
  if abandoned_comp_ids:
    parts.append(f"abandoned companion {', '.join(abandoned_comp_ids)}")
  if triggered_canaries:
    parts.append(f"triggered {', '.join(triggered_canaries)}")
  events.insert(
      0,
      {
          "id": f"ev-{int(time.time() * 1000)}-{cl_id}",
          "time": et_now,
          "item_key": parent_ikey,
          "target_id": cl_id,
          "conversation_id": "",
          "project_name": "Flight Deck",
          "critical": False,
          "message": "[FLIGHT-DECK] ✅ " + "; ".join(parts),
      },
  )
  state["events"] = events[:50]
  _write_watcher_state_atomic(sp, state)


# Persist armed finalize state or dry-run-only state into cl_watcher_state.json
def _update_watcher_state_after_arm(
    state_path: Path | str | None,
    *,
    cl_id: str,
    cur_ps: int,
    armed_record: dict[str, Any] | None,
    event_message: str,
) -> None:
  if not state_path:
    return
  sp = Path(state_path)
  if not sp.is_file():
    return
  try:
    state = json.loads(sp.read_text(encoding="utf-8", errors="replace"))
  except Exception:
    return
  if not isinstance(state, dict):
    return

  items = state.get("items") or {}
  parent_ikey = f"gerrit:{cl_id}"
  iso_now, et_now = triage_store.now_et_strings()

  for rec in items.values():
    if not isinstance(rec, dict) or rec.get("item_key") != parent_ikey:
      continue
    # Park armed items in WAITING_ON_OTHERS during CQ Dry Run; keep cq_only items in place
    rec["self_triggered_dry_run_ps"] = cur_ps
    rec["cq_status"] = "CQ+1 (Dry Run)"
    rec["cq_failed"] = False
    rec.pop("armed_finalize_error", None)
    if armed_record is not None:
      rec["armed_finalize"] = dict(armed_record)
      rec["in_my_attention"] = False
      rec["phase"] = "WAITING_ON_OTHERS"
    else:
      rec.pop("armed_finalize", None)
    rec["status_changed_raw"] = iso_now
    rec["status_changed_et"] = et_now
    rec["status_changed_age"] = "just now"

  events = state.setdefault("events", [])
  events.insert(
      0,
      {
          "id": f"ev-{int(time.time() * 1000)}-{cl_id}",
          "time": et_now,
          "item_key": parent_ikey,
          "target_id": cl_id,
          "conversation_id": "",
          "project_name": "Flight Deck",
          "critical": False,
          "message": event_message,
      },
  )
  state["events"] = events[:50]
  _write_watcher_state_atomic(sp, state)


# Execute atomic post-review approval, draft publication, companion abandon, and canary launch
def execute_finalize_review(
    target_id: str,
    *,
    host: str = DEFAULT_GERRIT_HOST,
    expected_revision: str = "",
    draft_ids: Sequence[str] | None = None,
    resolve_thread_leaf_ids: Sequence[str] | None = None,
    abandon_companion_ids: Sequence[str] | None = None,
    trigger_canaries: Sequence[str] | None = None,
    stage_fallback_draft: bool = True,
    require_cq_dry_run: bool = False,
    results_dir: Path | str | None = None,
    state_path: Path | str | None = None,
    search_dirs: Sequence[Path | str] | None = None,
    has_citc: bool | None = None,
    api_fn: Callable[..., Any] = gerrit_api_call,
    bb_fn: Callable[[str, int], dict[str, Any]] | None = None,
    cmd_runner: Callable[[Sequence[str], bool], tuple[int, str]] = _default_cmd_runner,
) -> dict[str, Any]:
  cl_id = str(target_id).strip()
  eff_state_path = (
      state_path
      if state_path is not None
      else (WATCHER_STATE_FILE if results_dir is None else None)
  )
  preview = build_finalize_preview(
      cl_id,
      host=host,
      results_dir=results_dir,
      state_path=eff_state_path,
      search_dirs=search_dirs,
      has_citc=has_citc,
      api_fn=api_fn,
      bb_fn=bb_fn,
  )
  if not preview.get("ready"):
    return {
        "ok": False,
        "error": "; ".join(preview.get("blockers") or ["Pre-flight gate failed"]),
        "preview": preview,
    }

  cur_ps = int(preview["patchset"])
  cur_sha = str(preview["revision"] or "")
  eff_host = str(preview["host"] or host)
  if expected_revision and cur_sha and not triage_store._sha_matches(
      cur_sha, expected_revision
  ):
    return {
        "ok": False,
        "error": (
            f"Revision changed since preview: expected {expected_revision[:10]}, "
            f"found {cur_sha[:10]}"
        ),
        "preview": preview,
    }
  if require_cq_dry_run and not preview.get("cq_dry_run_passed"):
    cq_st = str(((preview.get("gates") or {}).get("cq_dry_run") or {}).get("status") or "not_run")
    return {
        "ok": False,
        "error": f"CQ Dry Run has not passed on PS{cur_ps} (status: {cq_st})",
        "preview": preview,
    }

  # Determine exact whitelist of existing drafts to publish
  if draft_ids is None:
    publish_ids = [
        str(d["id"]) for d in preview["drafts"] if d.get("selected_by_default")
    ]
  else:
    allowed_existing = {str(d["id"]) for d in preview["drafts"]}
    publish_ids = [
        str(x).strip()
        for x in draft_ids
        if str(x).strip() and str(x).strip() in allowed_existing
    ]

  # Stage fallback LGTM draft from triage_store if Gerrit had no current-PS draft yet
  if (
      stage_fallback_draft
      and not publish_ids
      and str(preview.get("triage_draft_fallback") or "").strip()
  ):
    staged_lgtm = _upsert_gerrit_draft(
        eff_host,
        cl_id,
        cur_ps,
        path="/PATCHSET_LEVEL",
        message=str(preview["triage_draft_fallback"]).strip(),
        unresolved=False,
        api_fn=api_fn,
    )
    if staged_lgtm.get("draft_id"):
      publish_ids.append(str(staged_lgtm["draft_id"]))

  # Stage "Done" drafts on selected own prior-patchset unresolved threads
  own_map = {str(t["leaf_id"]): t for t in preview["own_unresolved_threads"]}
  if resolve_thread_leaf_ids is None:
    chosen_leaves = [
        str(t["leaf_id"])
        for t in preview["own_unresolved_threads"]
        if t.get("selected_by_default")
    ]
  else:
    chosen_leaves = [
        str(x).strip()
        for x in resolve_thread_leaf_ids
        if str(x).strip() in own_map
    ]

  resolved_threads_count = 0
  for leaf_id in chosen_leaves:
    th = own_map[leaf_id]
    saved_done = _upsert_gerrit_draft(
        eff_host,
        cl_id,
        int(th["patchset"]),
        path=str(th["path"]),
        line=th.get("line"),
        in_reply_to=leaf_id,
        message="Done",
        unresolved=False,
        api_fn=api_fn,
    )
    did = str(saved_done.get("draft_id") or "")
    if did and did not in publish_ids:
      publish_ids.append(did)
    resolved_threads_count += 1

  # Count pre-staged own-thread Done drafts included in publish_ids
  for d in preview["drafts"]:
    if (
        str(d["id"]) in publish_ids
        and int(d["patchset"]) < cur_ps
        and not d.get("unresolved")
        and d.get("in_reply_to")
    ):
      resolved_threads_count += 1

  review_payload: dict[str, Any] = {
      "labels": {"Code-Review": 1},
      "omit_duplicate_comments": True,
  }
  if publish_ids:
    review_payload["drafts"] = "PUBLISH_ALL_REVISIONS"
    review_payload["draft_ids_to_publish"] = publish_ids
  else:
    review_payload["drafts"] = "KEEP"

  api_fn(
      eff_host,
      "POST",
      f"/changes/{cl_id}/revisions/{cur_ps}/review",
      review_payload,
  )

  # Idempotently abandon selected open self-owned companion WIP CLs with notify=NONE
  comp_map = {str(c["id"]): c for c in preview["companions"]}
  if abandon_companion_ids is None:
    chosen_comps = [
        str(c["id"])
        for c in preview["companions"]
        if c.get("action") == "abandon" and c.get("selected_by_default")
    ]
  else:
    chosen_comps = [
        str(x).strip()
        for x in abandon_companion_ids
        if str(x).strip() in comp_map
        and comp_map[str(x).strip()].get("action") == "abandon"
    ]

  abandoned_ids: list[str] = []
  companion_errors: list[str] = []
  for comp_id in chosen_comps:
    try:
      api_fn(
          eff_host,
          "POST",
          f"/changes/{comp_id}/abandon",
          {
              "message": f"Abandoning companion WIP CL (approved CL {cl_id} PS{cur_ps}).",
              "notify": "NONE",
          },
      )
      abandoned_ids.append(comp_id)
    except Exception as exc:
      companion_errors.append(f"{comp_id}: {exc}")

  # Trigger deferred post-CR+1 external canaries (Canary-G3) and background watcher
  allowed_canaries = set(preview.get("deferred_canaries") or ())
  if trigger_canaries is None:
    chosen_canaries = list(preview.get("deferred_canaries") or ())
  else:
    chosen_canaries = [
        str(c).strip() for c in trigger_canaries if str(c).strip() in allowed_canaries
    ]

  triggered_canaries: list[str] = []
  canary_errors: list[str] = []
  if chosen_canaries:
    luci_auth_bin = getattr(canary_advisor, "LUCI_AUTH", None) or (
        canary_advisor._depot_tool("luci-auth")
    )
    try_cmd = [
        luci_auth_bin,
        "context",
        "--",
        "git",
        "cl",
        "try",
        "-i",
        cl_id,
        "-B",
        canary_advisor.BUCKET,
    ]
    for c_bot in chosen_canaries:
      try_cmd.extend(["-b", c_bot])
    rc, out = cmd_runner(try_cmd, False)
    if rc == 0:
      triggered_canaries.extend(chosen_canaries)
      if os.path.isfile(CANARY_ADVISOR_SCRIPT):
        watch_cmd = [
            sys.executable,
            CANARY_ADVISOR_SCRIPT,
            "watch",
            "--cl",
            cl_id,
            "--save-draft",
        ]
        cmd_runner(watch_cmd, True)
    else:
      canary_errors.append(f"git cl try exited {rc}: {out[:200]}")

  _update_watcher_state_after_finalize(
      eff_state_path,
      cl_id=cl_id,
      cur_ps=cur_ps,
      resolved_count=resolved_threads_count,
      abandoned_comp_ids=abandoned_ids,
      triggered_canaries=triggered_canaries,
  )

  return {
      "ok": True,
      "mode": "published",
      "armed": False,
      "target_id": cl_id,
      "patchset": cur_ps,
      "revision": cur_sha,
      "voted_cr": 1,
      "published_draft_ids": publish_ids,
      "resolved_threads_count": resolved_threads_count,
      "abandoned_companion_ids": abandoned_ids,
      "companion_errors": companion_errors,
      "triggered_canaries": triggered_canaries,
      "canary_errors": canary_errors,
      "url": preview["url"],
  }


# Trigger quiet CQ+1 dry run and arm auto-CR+1 publication when the dry run passes
def arm_finalize_review(
    target_id: str,
    *,
    host: str = DEFAULT_GERRIT_HOST,
    expected_revision: str = "",
    draft_ids: Sequence[str] | None = None,
    resolve_thread_leaf_ids: Sequence[str] | None = None,
    abandon_companion_ids: Sequence[str] | None = None,
    trigger_canaries: Sequence[str] | None = None,
    stage_fallback_draft: bool = True,
    results_dir: Path | str | None = None,
    state_path: Path | str | None = None,
    search_dirs: Sequence[Path | str] | None = None,
    has_citc: bool | None = None,
    api_fn: Callable[..., Any] = gerrit_api_call,
    bb_fn: Callable[[str, int], dict[str, Any]] | None = None,
    cmd_runner: Callable[[Sequence[str], bool], tuple[int, str]] = _default_cmd_runner,
) -> dict[str, Any]:
  cl_id = str(target_id).strip()
  eff_state_path = (
      state_path
      if state_path is not None
      else (WATCHER_STATE_FILE if results_dir is None else None)
  )
  preview = build_finalize_preview(
      cl_id,
      host=host,
      results_dir=results_dir,
      state_path=eff_state_path,
      search_dirs=search_dirs,
      has_citc=has_citc,
      api_fn=api_fn,
      bb_fn=bb_fn,
  )
  if not preview.get("ready"):
    return {
        "ok": False,
        "error": "; ".join(preview.get("blockers") or ["Pre-flight gate failed"]),
        "preview": preview,
    }

  cur_ps = int(preview["patchset"])
  cur_sha = str(preview["revision"] or "")
  eff_host = str(preview["host"] or host)
  if expected_revision and cur_sha and not triage_store._sha_matches(
      cur_sha, expected_revision
  ):
    return {
        "ok": False,
        "error": (
            f"Revision changed since preview: expected {expected_revision[:10]}, "
            f"found {cur_sha[:10]}"
        ),
        "preview": preview,
    }

  # Publish immediately when CQ Dry Run already passed on cur_ps or an equivalent patchset
  if preview.get("cq_dry_run_passed"):
    return execute_finalize_review(
        cl_id,
        host=eff_host,
        expected_revision=cur_sha,
        draft_ids=draft_ids,
        resolve_thread_leaf_ids=resolve_thread_leaf_ids,
        abandon_companion_ids=abandon_companion_ids,
        trigger_canaries=trigger_canaries,
        stage_fallback_draft=stage_fallback_draft,
        results_dir=results_dir,
        state_path=eff_state_path,
        search_dirs=search_dirs,
        has_citc=has_citc,
        api_fn=api_fn,
        bb_fn=bb_fn,
        cmd_runner=cmd_runner,
    )

  if draft_ids is None:
    chosen_drafts = [
        str(d["id"]) for d in preview["drafts"] if d.get("selected_by_default")
    ]
  else:
    allowed_existing = {str(d["id"]) for d in preview["drafts"]}
    chosen_drafts = [
        str(x).strip()
        for x in draft_ids
        if str(x).strip() and str(x).strip() in allowed_existing
    ]

  own_map = {str(t["leaf_id"]): t for t in preview["own_unresolved_threads"]}
  if resolve_thread_leaf_ids is None:
    chosen_leaves = [
        str(t["leaf_id"])
        for t in preview["own_unresolved_threads"]
        if t.get("selected_by_default")
    ]
  else:
    chosen_leaves = [
        str(x).strip()
        for x in resolve_thread_leaf_ids
        if str(x).strip() in own_map
    ]

  comp_map = {str(c["id"]): c for c in preview["companions"]}
  if abandon_companion_ids is None:
    chosen_comps = [
        str(c["id"])
        for c in preview["companions"]
        if c.get("action") == "abandon" and c.get("selected_by_default")
    ]
  else:
    chosen_comps = [
        str(x).strip()
        for x in abandon_companion_ids
        if str(x).strip() in comp_map
        and comp_map[str(x).strip()].get("action") == "abandon"
    ]

  allowed_canaries = set(preview.get("deferred_canaries") or ())
  if trigger_canaries is None:
    chosen_canaries = list(preview.get("deferred_canaries") or ())
  else:
    chosen_canaries = [
        str(c).strip() for c in trigger_canaries if str(c).strip() in allowed_canaries
    ]

  cq_status_now = str(
      ((preview.get("gates") or {}).get("cq_dry_run") or {}).get("status") or "not_run"
  )
  triggered_cq = False
  if cq_status_now != "running":
    api_fn(
        eff_host,
        "POST",
        f"/changes/{cl_id}/revisions/{cur_ps}/review",
        {
            "labels": {"Commit-Queue": 1},
            "tag": "autogenerated:cq:dry-run",
            "notify": "NONE",
            "ignore_automatic_attention_set_rules": True,
        },
    )
    triggered_cq = True

  iso_now, et_now = triage_store.now_et_strings()
  armed_record = {
      "armed_at": iso_now,
      "armed_at_et": et_now,
      "armed_ts": time.time(),
      "patchset": cur_ps,
      "expected_revision": cur_sha,
      "host": eff_host,
      "draft_ids": chosen_drafts,
      "resolve_thread_leaf_ids": chosen_leaves,
      "abandon_companion_ids": chosen_comps,
      "trigger_canaries": chosen_canaries,
      "stage_fallback_draft": bool(stage_fallback_draft),
  }
  ev_msg = (
      f"[FLIGHT-DECK] 🧪 Started CQ Dry Run & armed auto CR+1 on gerrit:{cl_id} (PS{cur_ps})"
      if triggered_cq
      else (
          f"[FLIGHT-DECK] ⏳ Armed auto CR+1 after running CQ Dry Run "
          f"on gerrit:{cl_id} (PS{cur_ps})"
      )
  )
  _update_watcher_state_after_arm(
      eff_state_path,
      cl_id=cl_id,
      cur_ps=cur_ps,
      armed_record=armed_record,
      event_message=ev_msg,
  )
  return {
      "ok": True,
      "mode": "armed",
      "armed": True,
      "triggered_cq_dry_run": triggered_cq,
      "target_id": cl_id,
      "patchset": cur_ps,
      "revision": cur_sha,
      "armed_finalize": armed_record,
      "url": preview["url"],
  }


# Trigger quiet Commit-Queue+1 dry run without arming auto-CR+1
def trigger_cq_dry_run_only(
    target_id: str,
    *,
    host: str = DEFAULT_GERRIT_HOST,
    expected_revision: str = "",
    results_dir: Path | str | None = None,
    state_path: Path | str | None = None,
    api_fn: Callable[..., Any] = gerrit_api_call,
) -> dict[str, Any]:
  cl_id = str(target_id).strip()
  eff_state_path = (
      state_path
      if state_path is not None
      else (WATCHER_STATE_FILE if results_dir is None else None)
  )
  parent_rec, _ = _load_watcher_context(cl_id, eff_state_path)
  eff_host = str(host or parent_rec.get("host") or DEFAULT_GERRIT_HOST)
  info = api_fn(
      eff_host,
      "GET",
      f"/changes/{cl_id}?o=DETAILED_ACCOUNTS&o=ALL_REVISIONS&o=CURRENT_FILES"
      "&o=DETAILED_LABELS&o=SUBMIT_REQUIREMENTS&o=MESSAGES",
  )
  cur_ps, cur_sha, _ = _extract_current_revision(info)
  status = str(info.get("status") or "NEW").upper()
  if status != "NEW" or bool(info.get("work_in_progress", False)):
    return {
        "ok": False,
        "error": f"Cannot run CQ Dry Run on {status} / WIP CL {cl_id}",
    }
  if expected_revision and cur_sha and not triage_store._sha_matches(
      cur_sha, expected_revision
  ):
    return {
        "ok": False,
        "error": (
            f"Revision changed since preview: expected {expected_revision[:10]}, "
            f"found {cur_sha[:10]}"
        ),
    }

  cq_gate = evaluate_cq_dry_run_gate(info, cur_ps=cur_ps)
  triggered_cq = False
  if cq_gate["status"] != "running":
    api_fn(
        eff_host,
        "POST",
        f"/changes/{cl_id}/revisions/{cur_ps}/review",
        {
            "labels": {"Commit-Queue": 1},
            "tag": "autogenerated:cq:dry-run",
            "notify": "NONE",
            "ignore_automatic_attention_set_rules": True,
        },
    )
    triggered_cq = True

  _update_watcher_state_after_arm(
      eff_state_path,
      cl_id=cl_id,
      cur_ps=cur_ps,
      armed_record=None,
      event_message=(
          f"[FLIGHT-DECK] 🧪 Triggered CQ Dry Run (no auto CR+1) on gerrit:{cl_id} (PS{cur_ps})"
      ),
  )
  return {
      "ok": True,
      "mode": "dry_run_only",
      "armed": False,
      "triggered_cq_dry_run": triggered_cq,
      "target_id": cl_id,
      "patchset": cur_ps,
      "revision": cur_sha,
      "url": f"https://{eff_host}/c/skia/+/{cl_id}/{cur_ps}",
  }


# Cancel an armed auto-CR+1 on target_id and optionally remove own Commit-Queue+1 vote
def disarm_finalize_review(
    target_id: str,
    *,
    host: str = DEFAULT_GERRIT_HOST,
    remove_cq_vote: bool = False,
    results_dir: Path | str | None = None,
    state_path: Path | str | None = None,
    api_fn: Callable[..., Any] = gerrit_api_call,
) -> dict[str, Any]:
  cl_id = str(target_id).strip()
  eff_state_path = (
      state_path
      if state_path is not None
      else (WATCHER_STATE_FILE if results_dir is None else None)
  )
  parent_rec, _ = _load_watcher_context(cl_id, eff_state_path)
  armed = parent_rec.get("armed_finalize") if isinstance(parent_rec, dict) else None
  eff_host = str(
      (armed or {}).get("host")
      or host
      or (parent_rec or {}).get("host")
      or DEFAULT_GERRIT_HOST
  )
  cur_ps = int(
      (armed or {}).get("patchset") or (parent_rec or {}).get("patchset") or 1
  )
  if remove_cq_vote:
    api_fn(
        eff_host,
        "POST",
        f"/changes/{cl_id}/revisions/{cur_ps}/review",
        {
            "labels": {"Commit-Queue": 0},
            "notify": "NONE",
            "ignore_automatic_attention_set_rules": True,
        },
    )

  if eff_state_path:
    sp = Path(eff_state_path)
    if sp.is_file():
      try:
        state = json.loads(sp.read_text(encoding="utf-8", errors="replace"))
      except Exception:
        state = None
      if isinstance(state, dict):
        items = state.get("items") or {}
        parent_ikey = f"gerrit:{cl_id}"
        iso_now, et_now = triage_store.now_et_strings()
        for rec in items.values():
          if not isinstance(rec, dict) or rec.get("item_key") != parent_ikey:
            continue
          rec.pop("armed_finalize", None)
          if remove_cq_vote and rec.get("cq_status") == "CQ+1 (Dry Run)":
            rec["cq_status"] = "Idle"
          if (
              rec.get("status") == "NEW"
              and not rec.get("self_approved")
              and int(rec.get("self_cr_vote") or 0) <= 0
              and rec.get("phase") != "ALERT_CQ_FAILED"
          ):
            rec["in_my_attention"] = True
            rec["phase"] = "NEEDS_YOUR_ACTION"
          rec["status_changed_raw"] = iso_now
          rec["status_changed_et"] = et_now
          rec["status_changed_age"] = "just now"
        events = state.setdefault("events", [])
        events.insert(
            0,
            {
                "id": f"ev-{int(time.time() * 1000)}-{cl_id}",
                "time": et_now,
                "item_key": parent_ikey,
                "target_id": cl_id,
                "conversation_id": "",
                "project_name": "Flight Deck",
                "critical": False,
                "message": (
                    f"[FLIGHT-DECK] 🛑 Canceled armed auto CR+1 on gerrit:{cl_id} (PS{cur_ps})"
                ),
            },
        )
        state["events"] = events[:50]
        _write_watcher_state_atomic(sp, state)

  return {
      "ok": True,
      "mode": "disarmed",
      "armed": False,
      "removed_cq_vote": bool(remove_cq_vote),
      "target_id": cl_id,
      "patchset": cur_ps,
      "url": f"https://{eff_host}/c/skia/+/{cl_id}/{cur_ps}",
  }


def main(argv: Sequence[str] | None = None) -> int:
  parser = argparse.ArgumentParser(
      description="Stage, preview, arm, and execute post-review Gerrit CL finalization."
  )
  sub = parser.add_subparsers(dest="cmd")

  p_stage = sub.add_parser(
      "stage-thread-drafts",
      help="Stage unpublished 'Done' drafts on own addressed prior-PS threads",
  )
  p_stage.add_argument("--cl", "--target", dest="target", required=True)
  p_stage.add_argument("--host", default=DEFAULT_GERRIT_HOST)
  p_stage.add_argument("--thread-id", action="append", default=[])
  p_stage.add_argument("--auto-rebase", action="store_true")
  p_stage.add_argument("--all-own-prior", action="store_true")

  p_prev = sub.add_parser(
      "preview", help="Read-only dry-run preview of post-review finalization"
  )
  p_prev.add_argument("--cl", "--target", dest="target", required=True)
  p_prev.add_argument("--host", default=DEFAULT_GERRIT_HOST)

  p_exec = sub.add_parser(
      "execute",
      help="Publish verified drafts + Code-Review+1 + companion cleanup atomically",
  )
  p_exec.add_argument("--cl", "--target", dest="target", required=True)
  p_exec.add_argument("--host", default=DEFAULT_GERRIT_HOST)
  p_exec.add_argument("--expected-revision", default="")
  p_exec.add_argument("--draft-id", action="append", default=None)
  p_exec.add_argument("--resolve-thread-leaf", action="append", default=None)
  p_exec.add_argument("--abandon-companion", action="append", default=None)
  p_exec.add_argument("--trigger-canary", action="append", default=None)
  p_exec.add_argument("--require-cq-dry-run", action="store_true")

  p_arm = sub.add_parser(
      "arm",
      help="Trigger quiet CQ Dry Run and arm auto CR+1 when dry run passes",
  )
  p_arm.add_argument("--cl", "--target", dest="target", required=True)
  p_arm.add_argument("--host", default=DEFAULT_GERRIT_HOST)
  p_arm.add_argument("--expected-revision", default="")
  p_arm.add_argument("--draft-id", action="append", default=None)
  p_arm.add_argument("--resolve-thread-leaf", action="append", default=None)
  p_arm.add_argument("--abandon-companion", action="append", default=None)
  p_arm.add_argument("--trigger-canary", action="append", default=None)

  p_disarm = sub.add_parser(
      "disarm",
      help="Cancel armed auto CR+1 on a Gerrit CL",
  )
  p_disarm.add_argument("--cl", "--target", dest="target", required=True)
  p_disarm.add_argument("--host", default=DEFAULT_GERRIT_HOST)
  p_disarm.add_argument("--remove-cq-vote", action="store_true")

  args = parser.parse_args(argv)
  if args.cmd == "stage-thread-drafts":
    res = stage_own_thread_drafts(
        args.target,
        host=args.host,
        thread_ids=args.thread_id,
        auto_rebase=args.auto_rebase,
        all_own_prior=args.all_own_prior,
    )
    print(json.dumps({"ok": True, "staged": res}, indent=2, ensure_ascii=False))
    return 0

  if args.cmd == "preview":
    res = build_finalize_preview(args.target, host=args.host)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("ready") else 2

  if args.cmd == "execute":
    res = execute_finalize_review(
        args.target,
        host=args.host,
        expected_revision=args.expected_revision,
        draft_ids=args.draft_id,
        resolve_thread_leaf_ids=args.resolve_thread_leaf,
        abandon_companion_ids=args.abandon_companion,
        trigger_canaries=args.trigger_canary,
        require_cq_dry_run=args.require_cq_dry_run,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("ok") else 1

  if args.cmd == "arm":
    res = arm_finalize_review(
        args.target,
        host=args.host,
        expected_revision=args.expected_revision,
        draft_ids=args.draft_id,
        resolve_thread_leaf_ids=args.resolve_thread_leaf,
        abandon_companion_ids=args.abandon_companion,
        trigger_canaries=args.trigger_canary,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("ok") else 1

  if args.cmd == "disarm":
    res = disarm_finalize_review(
        args.target,
        host=args.host,
        remove_cq_vote=args.remove_cq_vote,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("ok") else 1

  parser.print_help()
  return 0


if __name__ == "__main__":
  sys.exit(main())
