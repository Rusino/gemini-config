#!/usr/bin/env python3
"""
triage_store.py — Persistent data store and parser for daily review-triage results.

Provides a structured JSON data contract for code review triage outcomes produced by
review-triage (run_queue_triage.py / triage_cl.py) and per-CL audit agents (wf312_*.md),
enriching Flight Deck CL cards with category badges, review times, summaries, and drafts.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable

TRIAGE_RESULTS_DIR = Path.home() / ".gemini" / "config" / "logs" / "triage_results"

RE_GERRIT_URL_ID = re.compile(r"/c/(?:[^/]+/)?\+/(\d+)")
RE_GITHUB_PR_URL = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)")
RE_VERDICT_BADGE = re.compile(r"\*\*Verdict:\*\*\s*(?:[^\n\[]*)\s*\[([A-Z_ /]+)\]", re.I)
RE_WHY = re.compile(r"\*\*Why:\*\*\s*([^\n]+)", re.I)
RE_PATCHSET_ROW = re.compile(
    r"\|\s*\*\*Patchset\*\*\s*\|\s*`PS(\d+)`\s*\|\s*([^|]+)\s*\|",
    re.I,
)
RE_DRAFT_BLOCK = re.compile(
    r"\*\*Ready-to-post Draft \(English\):\*\*\s*\n+>\s*(.*?)(?=\n\n|\n##|\Z)",
    re.S,
)
RE_TIME_MIN = re.compile(r"about\s*(\d+(?:\.\d+)?)\s*min", re.I)


# Format current local Eastern Time timestamp string for UI display
def now_et_strings() -> tuple[str, str]:
    now_utc = datetime.now(timezone.utc)
    iso_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Convert to ET (-4 during EDT, -5 during EST)
    try:
        from zoneinfo import ZoneInfo
        now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        now_et = now_utc
    et_str = now_et.strftime("%b %d, %H:%M ET")
    return iso_str, et_str


# Derive canonical result filename for a review item
def canonical_filename(kind: str, target_id: str) -> str:
    safe_kind = "github" if kind == "github" else "gerrit"
    clean_id = re.sub(r"[^\w.-]", "_", str(target_id))
    return f"{safe_kind}_{clean_id}.json"


# Ensure storage directory exists and return Path
def ensure_results_dir(override_dir: Path | str | None = None) -> Path:
    p = Path(override_dir) if override_dir else TRIAGE_RESULTS_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


# Atomically persist triage result JSON, preserving deep audit fields over shallow triage
def save_triage_result(data: dict[str, Any], results_dir: Path | str | None = None) -> Path:
    target_dir = ensure_results_dir(results_dir)
    kind = str(data.get("kind") or "gerrit").lower()
    target_id = str(data.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("Missing target_id in triage result")

    filename = canonical_filename(kind, target_id)
    dest_path = target_dir / filename
    existing: dict[str, Any] = {}
    if dest_path.exists():
        try:
            with open(dest_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    # If existing data came from a deep audit on the same or newer patchset, keep audit fields
    new_source = data.get("source", "queue_triage")
    existing_source = existing.get("source")
    existing_ps = existing.get("triaged_patchset") or 0
    new_ps = data.get("triaged_patchset") or 0

    merged = dict(existing)
    if existing_source == "audit" and new_source == "queue_triage" and existing_ps >= new_ps:
        # Preserve audit's verdict, summary, review time, report, and draft
        for k, v in data.items():
            if k not in merged or merged[k] in (None, "", 0):
                merged[k] = v
    else:
        merged.update(data)

    merged["schema_version"] = 1
    merged.setdefault("item_key", f"{kind}:{target_id}")

    tmp_path = target_dir / f"{filename}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    tmp_path.replace(dest_path)
    return dest_path


# Load all stored triage results keyed by item_key and target_id
def load_all_triage_results(
    results_dir: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    target_dir = ensure_results_dir(results_dir)
    results: dict[str, dict[str, Any]] = {}
    for p in target_dir.glob("*.json"):
        if p.name.endswith(".tmp") or ".tmp." in p.name:
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                item = json.load(f)
            ikey = item.get("item_key")
            tid = str(item.get("target_id") or "")
            if ikey:
                results[ikey] = item
            if tid and tid not in results:
                results[tid] = item
        except Exception:
            continue
    return results


# Normalize raw category string to one of the 4 canonical categories
def normalize_verdict(raw: str) -> str:
    s = raw.upper()
    if "MECHANICAL" in s or "WAITING ON OTHERS" in s:
        return "MECHANICAL"
    if "REASSIGN" in s:
        return "REASSIGN"
    if "PUSHBACK" in s:
        return "PUSHBACK"
    return "SURGICAL_AUDIT"


# Parse overview markdown from run_queue_triage.py (e.g. /tmp/triage_1030.md)
def parse_queue_triage_markdown(
    md_text: str,
    source_path: str | None = None,
) -> list[dict[str, Any]]:
    sections = re.split(r"^### Детали по\s+(\S+)", md_text, flags=re.M)
    results: list[dict[str, Any]] = []
    iso_ts, et_ts = now_et_strings()

    for i in range(1, len(sections), 2):
        target_id = sections[i].strip()
        body = sections[i + 1]

        subj_m = re.search(r"^## Review Triage:\s*([^\n]+)", body, re.M)
        subject = subj_m.group(1).strip() if subj_m else ""

        verdict_m = RE_VERDICT_BADGE.search(body)
        raw_verdict = verdict_m.group(1).strip() if verdict_m else "SURGICAL_AUDIT"
        verdict = normalize_verdict(raw_verdict)

        why_m = RE_WHY.search(body)
        reason = why_m.group(1).strip() if why_m else ""

        ps_m = RE_PATCHSET_ROW.search(body)
        triaged_ps = int(ps_m.group(1)) if ps_m else 1
        ps_details = ps_m.group(2).strip() if ps_m else ""

        draft_m = RE_DRAFT_BLOCK.search(body)
        draft = draft_m.group(1).strip() if draft_m else ""

        summary = reason[:90]
        if "Already reviewed by you on this patchset" in ps_details:
            if "Code-Review+1" in ps_details or "Code-Review+2" in ps_details:
                verdict = "MECHANICAL"
                summary = "Already approved on this patchset; no comment needed"

        kind = "gerrit"
        url = ""
        # GitHub PR labels may appear as repo#number or #number
        pr_m = re.search(r"#(\d+)", target_id)
        if pr_m:
            kind = "github"
            target_id = pr_m.group(1)

        url_m = re.search(r"\bhttps://[^\s)>]+/\+/(\d+)", body)
        if url_m:
            url = url_m.group(0)
        else:
            url_gh = RE_GITHUB_PR_URL.search(body)
            if url_gh:
                kind = "github"
                raw_u = url_gh.group(0)
                url = raw_u if raw_u.startswith("https://") else f"https://{raw_u}"
                if not pr_m:
                    target_id = url_gh.group(2)

        results.append({
            "target_id": target_id,
            "item_key": f"{kind}:{target_id}",
            "kind": kind,
            "url": url,
            "subject": subject,
            "verdict": verdict,
            "verdict_label": verdict.replace("_", " "),
            "est_minutes": 3.0 if verdict == "SURGICAL_AUDIT" else 1.0,
            "review_time_label": "~3m" if verdict == "SURGICAL_AUDIT" else "~1m",
            "summary": summary,
            "reason": reason,
            "triaged_patchset": triaged_ps,
            "triaged_at": iso_ts,
            "triaged_at_et": et_ts,
            "source": "queue_triage",
            "draft": draft,
            "report_markdown": f"### Детали по {target_id}\n" + body.strip(),
            "report_path": source_path or "",
        })
    return results


# Extract ready-to-copy draft code block or text from deep audit report
def extract_draft_from_audit(body: str) -> str:
    # 1. Code block after Ready-to-copy draft or Draft
    m = re.search(
        r"\*\*(?:Ready-to-copy draft|Ready-to-post Draft|Draft)[^\n]*\n+"
        r"```[a-zA-Z0-9_-]*\s*\n(.*?)\n```",
        body,
        re.S | re.I,
    )
    if m and m.group(1).strip():
        return m.group(1).strip()
    # 2. Blockquote after Draft heading
    m_bq = re.search(
        r"\*\*(?:Ready-to-copy draft|Ready-to-post Draft|Draft)[^\n]*\n+>\s*(.*?)(?=\n\n|\n##|\Z)",
        body,
        re.S | re.I,
    )
    if m_bq and m_bq.group(1).strip():
        val = m_bq.group(1).strip()
        if "no comment needed" in val.lower():
            return ""
        return val
    # 3. Inline Draft: <text>
    m_in = re.search(r"[-*]?\s*\*\*Draft:\*\*\s*([^\n]+)", body, re.I)
    if m_in and m_in.group(1).strip():
        val = m_in.group(1).strip()
        if "no comment needed" in val.lower():
            return ""
        return val
    # 4. Reply draft heading followed by --- fenced text
    m_fence = re.search(
        r"(?:Reply draft|Draft reply|Suggested reply)[^\n]*:\s*\n+---\s*\n(.*?)\n---",
        body,
        re.S | re.I,
    )
    if m_fence and m_fence.group(1).strip():
        val = m_fence.group(1).strip()
        if "no comment needed" in val.lower():
            return ""
        return val
    return ""


# Parse a single audit section from a wf312_*.md file
def parse_single_audit_text(
    body: str,
    source_path: str | None = None,
) -> dict[str, Any] | None:
    # Target identification
    target_m = re.search(r"\b(?:cl/|CL\s+)(\d+)", body, re.I)
    kind = "gerrit"
    if target_m:
        target_id = target_m.group(1)
    else:
        pr_m = re.search(r"\b(?:PR\s+#|pull/)(\d+)", body, re.I)
        if pr_m:
            target_id = pr_m.group(1)
            kind = "github"
        else:
            return None

    ps_m = re.search(r"\bPS(\d+)\b", body)
    triaged_ps = int(ps_m.group(1)) if ps_m else 1

    cat_m = re.search(r"[-*]?\s*\*\*Category:\*\*\s*([^\n]+)", body, re.I)
    cat_raw = cat_m.group(1).strip() if cat_m else "Surgical audit"
    verdict = normalize_verdict(cat_raw)

    time_m = RE_TIME_MIN.search(body)
    est_min = float(time_m.group(1)) if time_m else 2.0
    if est_min == 0:
        verdict = "MECHANICAL"
    time_label = f"~{int(round(est_min))}m"

    draft = extract_draft_from_audit(body)
    iso_ts, et_ts = now_et_strings()

    # Determine concise one-line summary
    lower_body = body.lower()
    lower_draft = draft.lower()
    if "needs your vote again" in lower_body or "did not copy your cr+1" in lower_body:
        summary = "LGTM pending corpus gate; needs your re-vote"
    elif "lgtm pending corpus gate" in lower_draft or "approve with conditions" in cat_raw.lower():
        summary = "LGTM pending corpus gate"
    elif "waiting on others" in cat_raw.lower():
        summary = "Waiting on others; no comment needed"
    elif not draft or "no comment needed" in lower_draft:
        if "already approved" in lower_body or "+1 still stands" in lower_body:
            summary = "No comment needed (already approved)"
        else:
            summary = "No comment needed"
    elif "pushback" in cat_raw.lower():
        summary = "Needs pushback on missing tests or scope"
    elif "reassign" in cat_raw.lower():
        summary = "Needs reassignment to domain owner"
    else:
        summary = cat_raw[:85]

    return {
        "target_id": target_id,
        "item_key": f"{kind}:{target_id}",
        "kind": kind,
        "verdict": verdict,
        "verdict_label": verdict.replace("_", " "),
        "audit_outcome": cat_raw[:50],
        "est_minutes": est_min,
        "review_time_label": time_label,
        "summary": summary,
        "triaged_patchset": triaged_ps,
        "triaged_at": iso_ts,
        "triaged_at_et": et_ts,
        "source": "audit",
        "draft": draft,
        "report_markdown": body.strip(),
        "report_path": source_path or "",
    }


# Parse deep audit markdown files (e.g. /tmp/wf312_*.md)
def parse_wf_audit_markdown(
    md_text: str,
    source_path: str | None = None,
) -> list[dict[str, Any]]:
    # Handle multi-item files like wf312_followups.md split by **(1), **(2)
    parts = re.split(r"^\*\*\(\d+\)\s+", md_text, flags=re.M)
    items: list[dict[str, Any]] = []
    if len(parts) > 1:
        for p in parts[1:]:
            res = parse_single_audit_text(p, source_path=source_path)
            if res:
                items.append(res)
    else:
        res = parse_single_audit_text(md_text, source_path=source_path)
        if res:
            items.append(res)
    return items


# Ingest latest /tmp/triage_*.md and /tmp/wf*.md files and persist structured JSON
def ingest_tmp_triage_files(
    tmp_dir: Path | str = "/tmp",
    results_dir: Path | str | None = None,
) -> int:
    td = Path(tmp_dir)
    if not td.exists():
        return 0

    count = 0
    # Ingest overview report first
    triage_files = sorted(td.glob("triage_*.md"), key=lambda p: p.stat().st_mtime)
    if triage_files:
        latest_overview = triage_files[-1]
        try:
            content = latest_overview.read_text(encoding="utf-8", errors="replace")
            for res in parse_queue_triage_markdown(content, source_path=str(latest_overview)):
                save_triage_result(res, results_dir=results_dir)
                count += 1
        except Exception:
            pass

    # Ingest deep per-CL audit files second so they override/enrich overview data
    wf_files = sorted(td.glob("wf*.md"), key=lambda p: p.stat().st_mtime)
    for wf in wf_files:
        try:
            content = wf.read_text(encoding="utf-8", errors="replace")
            for res in parse_wf_audit_markdown(content, source_path=str(wf)):
                save_triage_result(res, results_dir=results_dir)
                count += 1
        except Exception:
            continue
    return count


# Save a single triage result directly from run_queue_triage.py
def save_from_triage_cl_output(
    cl_url: str,
    analysis_json: dict[str, Any],
    report_markdown: str,
    results_dir: Path | str | None = None,
) -> Path:
    meta = analysis_json.get("metadata") or {}
    analysis = analysis_json.get("analysis") or {}

    target_id = str(meta.get("cl_id") or meta.get("_number") or meta.get("number") or "")
    kind = "gerrit"
    m_gh = RE_GITHUB_PR_URL.search(cl_url)
    if m_gh:
        kind = "github"
        if not target_id:
            target_id = m_gh.group(2)
    elif not target_id:
        m = RE_GERRIT_URL_ID.search(cl_url)
        target_id = m.group(1) if m else "unknown"

    raw_verdict = str(analysis.get("category") or "SURGICAL_AUDIT")
    verdict = normalize_verdict(raw_verdict)
    reason = str(analysis.get("reason") or "")
    cur_ps = int(meta.get("thread_context", {}).get("current_ps") or meta.get("patchset") or 1)

    # Check for already-reviewed status on current patchset
    tctx = meta.get("thread_context") or {}
    if tctx.get("reviewed_current_ps"):
        verdict = "MECHANICAL"
        summary = "Already reviewed on this patchset; no comment needed"
    else:
        summary = reason[:90]

    time_map = {"SURGICAL_AUDIT": 3.0, "PUSHBACK": 2.0, "REASSIGN": 1.0, "MECHANICAL": 1.0}
    est_min = time_map.get(verdict, 2.0)
    time_label = f"~{int(round(est_min))}m"

    iso_ts, et_ts = now_et_strings()

    data = {
        "target_id": target_id,
        "item_key": f"{kind}:{target_id}",
        "kind": kind,
        "url": cl_url,
        "subject": str(meta.get("subject") or ""),
        "verdict": verdict,
        "verdict_label": verdict.replace("_", " "),
        "est_minutes": est_min,
        "review_time_label": time_label,
        "summary": summary,
        "reason": reason,
        "triaged_patchset": cur_ps,
        "triaged_at": iso_ts,
        "triaged_at_et": et_ts,
        "source": "queue_triage",
        "draft": "",
        "report_markdown": report_markdown,
        "report_path": "",
    }
    return save_triage_result(data, results_dir=results_dir)


# Overlay latest stored triage results onto an iterable of CL/PR item dicts
def overlay_triage_on_items(
    items: Iterable[dict[str, Any]],
    results_dir: Path | str | None = None,
    ingest_tmp: bool = True,
) -> None:
    if ingest_tmp:
        try:
            ingest_tmp_triage_files(results_dir=results_dir)
        except Exception:
            pass

    triage_map = load_all_triage_results(results_dir=results_dir)
    for it in items:
        if not isinstance(it, dict):
            continue
        ikey = str(it.get("item_key") or "")
        tid = str(it.get("target_id") or "")

        tr = triage_map.get(ikey) or triage_map.get(tid)
        if not tr:
            continue

        cur_ps = it.get("patchset")
        tr_ps = tr.get("triaged_patchset")
        is_stale = False
        try:
            if cur_ps is not None and tr_ps is not None and int(cur_ps) > int(tr_ps):
                is_stale = True
        except (ValueError, TypeError):
            is_stale = False

        summary = str(tr.get("summary") or "")
        if "already approved" in summary.lower():
            if not it.get("self_approved"):
                summary = "No comment needed"
            elif int(it.get("missing_approvals") or 0) > 0:
                rem = int(it.get("missing_approvals") or 0)
                summary = (
                    f"You approved; needs +{rem} more approval"
                    f"{'s' if rem > 1 else ''}"
                )

        it["triage"] = {
            **tr,
            "summary": summary,
            "current_patchset": cur_ps,
            "is_stale": is_stale,
        }


# Command line interface for testing and manual ingestion
def main() -> int:
    parser = argparse.ArgumentParser(description="Triage results data store and ingest utility.")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("ingest-tmp", help="Ingest /tmp/triage_*.md and /tmp/wf*.md files")
    p_save = sub.add_parser("save-audit", help="Save a deep audit report")
    p_save.add_argument("--target", required=True, help="CL number or PR number")
    p_save.add_argument("--kind", default="gerrit", choices=["gerrit", "github"])
    p_save.add_argument("--report-file", required=True, help="Path to markdown report")
    p_save.add_argument("--verdict", default=None)
    p_save.add_argument("--summary", default=None)
    p_save.add_argument("--patchset", type=int, default=None)
    p_save.add_argument("--est-minutes", type=float, default=None)

    args = parser.parse_args()
    if args.cmd == "ingest-tmp":
        cnt = ingest_tmp_triage_files()
        print(f"Ingested {cnt} triage/audit items into {TRIAGE_RESULTS_DIR}")
        return 0

    if args.cmd == "save-audit":
        rp = Path(args.report_file)
        if not rp.exists():
            print(f"Error: file not found: {rp}", file=sys.stderr)
            return 1
        body = rp.read_text(encoding="utf-8", errors="replace")
        parsed = parse_single_audit_text(body, source_path=str(rp)) or {}
        parsed["target_id"] = args.target
        parsed["kind"] = args.kind
        parsed["item_key"] = f"{args.kind}:{args.target}"
        if args.verdict:
            parsed["verdict"] = normalize_verdict(args.verdict)
            parsed["verdict_label"] = parsed["verdict"].replace("_", " ")
        if args.summary:
            parsed["summary"] = args.summary
        if args.patchset:
            parsed["triaged_patchset"] = args.patchset
        if args.est_minutes:
            parsed["est_minutes"] = args.est_minutes
            parsed["review_time_label"] = f"~{int(round(args.est_minutes))}m"
        p = save_triage_result(parsed)
        print(f"Saved audit result to {p}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
