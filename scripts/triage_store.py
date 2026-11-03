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
from typing import Any, Iterable, Sequence

TRIAGE_RESULTS_DIR = Path.home() / ".gemini" / "config" / "logs" / "triage_results"
WATCHER_STATE_FILE = Path.home() / ".gemini" / "config" / "logs" / "cl_watcher_state.json"

# Production directories whose changes require Flutter or WebParagraph corpus verification
SKIA_CORPUS_PATH_RE = re.compile(
    r"(?:^|/)modules/(?:skparagraph|skunicode|skshaper)/(?:src|include)/"
)
WEBPARAGRAPH_CORPUS_PATH_RE = re.compile(
    r"(?:^|/)(?:engine/src/flutter/)?lib/web_ui/lib/"
)
CODE_CORPUS_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".mm", ".dart", ".ts"
}
TEST_FILE_PATH_RE = re.compile(
    r"(test|Test|\btests/|_test\.dart|_test\.cpp|_gm\.cpp)"
)
TRIAGE_CORPUS_REQUIRED_RE = re.compile(
    r"REQUIRED\s*\(`(run_flutter_paragraph_tests\.py|run_webparagraph_corpus\.py)`\)"
    r"|"
    r'"required":\s*true[\s\S]{0,160}?"script":\s*"'
    r'(run_flutter_paragraph_tests\.py|run_webparagraph_corpus\.py)"'
)
VALID_INTEGRITY_RE = re.compile(
    r"(?:Integrity Check|Вердикт целостности)[^`\n]*`VALID\b", re.IGNORECASE
)
INVALID_INTEGRITY_RE = re.compile(
    r"(?:Integrity Check|Вердикт целостности)[^`\n]*`(?:WARNING|INVALID)\b",
    re.IGNORECASE,
)
REGRESSIONS_COUNT_RE = re.compile(r"Поломано:\s*(\d+)")
ATTESTATION_SHA_CELL_RE = re.compile(r"\(`([0-9a-f]{7,40})`\)")

# Review outcomes that allow or encourage CR+1 and must block when corpus has not passed
GATED_LGTM_OUTCOMES = {"CLEAN", "NITS", "COMPANION_TESTS"}
PATCHSET_SCOPED_REVIEW_KEYS = (
    "verdict_label",
    "review_outcome",
    "review_outcome_label",
    "review_eyebrow_label",
    "verdict_tone",
    "companion_id",
    "draft",
    "audit_outcome",
    "report_path",
    "report_markdown",
    "corpus_tested_patchset",
    "corpus_tested_sha",
    "corpus_compare_md",
    # Reset Google3 corpus path, addressed thread IDs, and staged draft IDs when a new PS lands
    "g3_corpus_compare_md",
    "resolved_thread_ids",
    "draft_ids",
    "triaged_revision",
    # Clear deep-audit hunks, thread verdicts, and in-flight status when a new PS arrives
    "delta_kind",
    "core_hunks",
    "open_thread_verdicts",
    "audit_state",
    "audit_target_patchset",
    "audit_target_revision",
    "audit_error",
)

# Match multi-segment Gerrit project paths such as /c/chromium/src/+/8663938
RE_GERRIT_URL_ID = re.compile(r"/c/(?:[^\s/)]+/)*\+/(\d+)")
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


# Format UTC epoch or current wall-clock time as ISO-8601 and Eastern Time display strings
def now_et_strings(epoch_sec: float | None = None) -> tuple[str, str]:
    now_utc = (
        datetime.fromtimestamp(epoch_sec, tz=timezone.utc)
        if epoch_sec is not None
        else datetime.now(timezone.utc)
    )
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


# Load a single triage result by kind and target_id
def load_triage_result(
    kind: str,
    target_id: str,
    results_dir: Path | str | None = None,
) -> dict[str, Any] | None:
    fn = canonical_filename(kind, target_id)
    target_path = ensure_results_dir(results_dir) / fn
    if not target_path.is_file():
        return None
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


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

    # Source priority ensures deep audits and finalized reviews override shallow queue triage
    source_priority = {"queue_triage": 0, "audit": 1, "review": 2}
    new_source = data.get("source", "queue_triage")
    existing_source = existing.get("source", "queue_triage")
    existing_ps = int(existing.get("triaged_patchset") or 0)
    new_ps = int(data.get("triaged_patchset") or 0)

    merged = dict(existing)
    p_exist = source_priority.get(str(existing_source), 0)
    p_new = source_priority.get(str(new_source), 0)

    # Reset patchset-scoped review and corpus state on newer patchset or new PR commit SHA
    exist_rev = str(existing.get("triaged_revision") or "").strip().lower()
    if kind == "github" and existing.get("report_markdown"):
        # Recover report SHA if queue_triage backfilled a newer SHA onto an old report
        rep_rev = extract_audit_revision(
            str(existing["report_markdown"]), kind, target_id, existing_ps
        )
        if len(rep_rev) >= 7 and (not exist_rev or not _sha_matches(exist_rev, rep_rev)):
            exist_rev = rep_rev
    new_rev = str(data.get("triaged_revision") or "").strip().lower()
    exist_at = str(existing.get("triaged_at") or "").strip()
    new_at = str(data.get("triaged_at") or "").strip()
    # Ignore older /tmp/wf*.md files so historical audits never roll back a newer review SHA
    not_older_than_existing = not (exist_at and new_at and new_at < exist_at)
    rev_drifted = (
        new_ps == existing_ps > 0
        and not_older_than_existing
        and len(exist_rev) >= 7
        and len(new_rev) >= 7
        and not _sha_matches(exist_rev, new_rev)
    )
    if new_ps > existing_ps > 0 or rev_drifted:
        for k in PATCHSET_SCOPED_REVIEW_KEYS:
            merged.pop(k, None)
        if existing.get("corpus_required") and "corpus_required" not in data:
            merged["corpus_required"] = True
            merged["corpus_script"] = existing.get("corpus_script", "")
            merged["corpus_domain"] = existing.get("corpus_domain", "")
            merged["corpus_gate_authoritative"] = False
            merged["corpus_status"] = "pending"
        merged.update(data)
        if not merged.get("verdict_label"):
            merged["verdict_label"] = str(merged.get("verdict") or "SURGICAL_AUDIT").replace(
                "_", " "
            )
    elif p_exist > p_new and existing_ps >= new_ps:
        same_rev = not exist_rev or not new_rev or _sha_matches(exist_rev, new_rev)
        # Preserve higher priority verdict, summary, review time, report, and draft
        for k, v in data.items():
            # Never backfill a shallow GitHub PR SHA onto an older review record
            if k == "triaged_revision" and (kind == "github" or existing_ps != new_ps):
                continue
            # Do not mix audit markdown from a different patchset or commit SHA
            if k in ("audit_outcome", "report_path", "report_markdown") and (
                existing_ps != new_ps or not same_rev
            ):
                continue
            if k not in merged or merged[k] in (None, "", 0):
                merged[k] = v
        # Refresh deep audit report metadata when a matching-PS audit lands alongside a review
        if existing_ps == new_ps and new_source == "audit" and same_rev:
            cur_rpath = str(merged.get("report_path") or "")
            new_rpath = str(data.get("report_path") or "")
            if new_rpath and (
                not cur_rpath
                or cur_rpath.startswith("/tmp/triage_")
                or cur_rpath != new_rpath
            ):
                merged["report_path"] = new_rpath
                if data.get("report_markdown"):
                    merged["report_markdown"] = data["report_markdown"]
                if data.get("audit_outcome"):
                    merged["audit_outcome"] = data["audit_outcome"]
        # Propagate authoritative hunk-level corpus requirement onto same-PS review records
        if existing_ps == new_ps and data.get("corpus_gate_authoritative"):
            merged["corpus_required"] = bool(data.get("corpus_required"))
            merged["corpus_script"] = str(data.get("corpus_script") or "")
            merged["corpus_domain"] = str(data.get("corpus_domain") or "")
            merged["corpus_gate_authoritative"] = True
            merged["corpus_gate_evaluated_ps"] = new_ps
            if data.get("corpus_required") and merged.get("corpus_status") in (
                None,
                "",
                "not_required",
            ):
                merged["corpus_status"] = "pending"
                if merged.get("review_outcome"):
                    lbls = format_review_labels(
                        str(merged["review_outcome"]),
                        new_ps,
                        True,
                        "pending",
                        merged["corpus_script"],
                    )
                    merged.update(lbls)
    else:
        # Preserve existing revision when same-patchset update omits triaged_revision
        if not new_rev and exist_rev and existing_ps == new_ps:
            data = {**data, "triaged_revision": existing["triaged_revision"]}
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
    if "SURGICAL" not in s and (
        "MECHANICAL" in s
        or "WAITING ON OTHERS" in s
        or "REBASE ONLY" in s
        or "REBASE-ONLY" in s
    ):
        return "MECHANICAL"
    if "REASSIGN" in s:
        return "REASSIGN"
    if "PUSHBACK" in s:
        return "PUSHBACK"
    return "SURGICAL_AUDIT"


# Canonical post-review outcomes mapped from companion-cl-review (Cases 1-5) and review-triage
REVIEW_OUTCOME_META: dict[str, dict[str, str]] = {
    "CLEAN": {
        "badge": "🟢 Clean (LGTM)",
        "eyebrow": "🟢 Review PS{ps}: Clean (LGTM)",
    },
    "NITS": {
        "badge": "🔵 Minor nits",
        "eyebrow": "🔵 Review PS{ps}: Minor nits",
    },
    "COMPANION_TESTS": {
        "badge": "🟣 Logic OK + test companion",
        "eyebrow": "🟣 Review PS{ps}: Logic OK + test companion",
    },
    "COMPANION_FIX": {
        "badge": "🟠 Defects + companion fix",
        "eyebrow": "🟠 Review PS{ps}: Defects + companion fix",
    },
    "PUSHBACK": {
        "badge": "🔴 Do not land / Pushback",
        "eyebrow": "🔴 Review PS{ps}: Do not land / Pushback",
    },
    "REASSIGN": {
        "badge": "⚪ Out of domain",
        "eyebrow": "⚪ Review PS{ps}: Out of domain",
    },
}


# Normalize raw review outcome string to one of the canonical outcomes
def normalize_review_outcome(raw: str) -> str:
    s = raw.strip().upper().replace("-", "_").replace(" ", "_")
    if s in ("CLEAN", "LGTM", "CASE_1", "CASE1", "IDEAL"):
        return "CLEAN"
    if s in ("NITS", "MINOR_NITS", "CASE_2", "CASE2", "NEAR_IDEAL"):
        return "NITS"
    if s in ("COMPANION_TESTS", "TESTS", "CASE_3", "CASE3"):
        return "COMPANION_TESTS"
    if s in ("PUSHBACK", "DO_NOT_LAND", "CASE_4", "CASE4", "CAT_4", "FUNDAMENTAL_FLAW"):
        return "PUSHBACK"
    if s in ("COMPANION_FIX", "COMPANION", "FIX", "DEFECTS", "CASE_5", "CASE5"):
        return "COMPANION_FIX"
    if s in ("REASSIGN", "OUT_OF_DOMAIN", "CAT_2"):
        return "REASSIGN"
    raise ValueError(f"Unknown review outcome: {raw}")


# Truncates summary to max_len on a word boundary with an ellipsis
def truncate_summary(text: str, max_len: int = 180) -> str:
    clean = " ".join(str(text or "").strip().split())
    if len(clean) <= max_len:
        return clean
    cut = clean[: max_len - 1]
    sp = cut.rfind(" ")
    if sp >= max_len // 2:
        cut = cut[:sp]
    cut = cut.rstrip(" ,;:-")
    return f"{cut}…"


# Normalize CLI or API corpus gate option to canonical corpus status
def normalize_corpus_arg(raw: str | None) -> str:
    s = str(raw or "auto").strip().lower().replace("-", "_")
    if s in ("", "auto"):
        return "auto"
    if s in ("none", "not_required", "false", "no"):
        return "not_required"
    if s in ("required", "pending"):
        return "pending"
    if s == "passed":
        return "passed"
    if s == "failed":
        return "failed"
    if s == "waived":
        return "waived"
    raise ValueError(f"Unknown corpus gate status: {raw}")


# Map corpus runner script filename to Flight Deck pill display label
def corpus_display_name(corpus_script: str) -> str:
    if corpus_script == "run_webparagraph_corpus.py":
        return "WebParagraph corpus"
    return "Flutter corpus"


# Format Flight Deck eyebrow, badge, and tone so gated LGTM outcomes never show green early
def format_review_labels(
    outcome: str,
    patchset: int,
    corpus_required: bool,
    corpus_status: str,
    corpus_script: str = "",
) -> dict[str, str]:
    meta = REVIEW_OUTCOME_META[outcome]
    safe_ps = int(patchset) if patchset else 1
    cname = corpus_display_name(corpus_script)
    st = str(corpus_status or "").lower()
    if outcome in GATED_LGTM_OUTCOMES and corpus_required:
        if st in ("", "pending", "required"):
            lbl = f"⏳ LGTM blocked: {cname} not run"
            return {
                "verdict_label": lbl,
                "review_outcome_label": lbl,
                "review_eyebrow_label": lbl,
                "verdict_tone": "corpus_blocked",
            }
        if st == "failed":
            lbl = f"🔴 LGTM blocked: {cname} failed"
            return {
                "verdict_label": lbl,
                "review_outcome_label": lbl,
                "review_eyebrow_label": lbl,
                "verdict_tone": "corpus_failed",
            }
        if st == "waived":
            short_map = {
                "CLEAN": "Clean",
                "NITS": "Minor nits",
                "COMPANION_TESTS": "Logic OK + tests",
            }
            short = short_map.get(outcome, outcome)
            return {
                "verdict_label": f"⚠️ {short} ({cname} waived)",
                "review_outcome_label": f"⚠️ {short} ({cname} waived)",
                "review_eyebrow_label": f"⚠️ Review PS{safe_ps}: {short} ({cname} waived)",
                "verdict_tone": "corpus_blocked",
            }
    return {
        "verdict_label": meta["badge"],
        "review_outcome_label": meta["badge"],
        "review_eyebrow_label": meta["eyebrow"].format(ps=safe_ps),
        "verdict_tone": outcome.lower(),
    }


# Detect mandatory corpus gate requirement from changed file paths and CL/PR subject
def detect_corpus_from_files(
    files: Iterable[str] | None,
    subject: str = "",
) -> dict[str, Any]:
    if re.search(r"^revert\b", str(subject or "").strip(), re.I):
        return {"required": False, "script": "", "domain": "", "files": []}
    skia_files: list[str] = []
    wp_files: list[str] = []
    for raw_f in files or ():
        norm = str(raw_f or "").strip().replace("\\", "/")
        if not norm or TEST_FILE_PATH_RE.search(norm):
            continue
        _, ext = os.path.splitext(norm)
        if ext.lower() not in CODE_CORPUS_EXTENSIONS:
            continue
        if SKIA_CORPUS_PATH_RE.search(norm):
            skia_files.append(norm)
        elif WEBPARAGRAPH_CORPUS_PATH_RE.search(norm):
            wp_files.append(norm)
    if skia_files:
        return {
            "required": True,
            "script": "run_flutter_paragraph_tests.py",
            "domain": "SkParagraph/SkUnicode/SkShaper",
            "files": skia_files,
        }
    if wp_files:
        return {
            "required": True,
            "script": "run_webparagraph_corpus.py",
            "domain": "WebParagraph (web_ui)",
            "files": wp_files,
        }
    return {"required": False, "script": "", "domain": "", "files": []}


# Parse corpus gate markers emitted by triage_cl.py or audit markdown
def detect_corpus_from_report_markdown(md_text: str) -> dict[str, Any] | None:
    if not md_text:
        return None
    m = TRIAGE_CORPUS_REQUIRED_RE.search(md_text)
    if m:
        script = m.group(1) or m.group(2)
        domain = (
            "WebParagraph (web_ui)"
            if script == "run_webparagraph_corpus.py"
            else "SkParagraph/SkUnicode/SkShaper"
        )
        return {
            "required": True,
            "script": script,
            "domain": domain,
            "authoritative": True,
        }
    # A full triage_cl.py report without the REQUIRED marker proved no executable corpus churn
    if "## Review Triage:" in md_text and "### 📊 Churn & Profile" in md_text:
        return {
            "required": False,
            "script": "",
            "domain": "",
            "authoritative": True,
        }
    low = md_text.lower()
    if "pending corpus gate" in low or "lgtm blocked:" in low:
        script = (
            "run_webparagraph_corpus.py"
            if "webparagraph" in low or "lib/web_ui/lib/" in low
            else "run_flutter_paragraph_tests.py"
        )
        domain = (
            "WebParagraph (web_ui)"
            if script == "run_webparagraph_corpus.py"
            else "SkParagraph/SkUnicode/SkShaper"
        )
        return {
            "required": True,
            "script": script,
            "domain": domain,
            "authoritative": False,
        }
    return None


# Match abbreviated (>= 7 hex) and full 40-char commit SHAs across Gerrit, GitHub, and compare.md
def _sha_matches(a: str, b: str) -> bool:
    sa = str(a or "").strip().lower()
    sb = str(b or "").strip().lower()
    if len(sa) < 7 or len(sb) < 7:
        return False
    if not re.fullmatch(r"[0-9a-f]+", sa) or not re.fullmatch(r"[0-9a-f]+", sb):
        return False
    return sa.startswith(sb) or sb.startswith(sa)


# Inspect compare.md and sibling attestation.json files to verify Integrity Check and patchset SHA
def evaluate_corpus_compare_md(
    compare_md_path: Path | str,
    expected_script: str = "",
    patchset_sha: str = "",
    min_timestamp_utc: str = "",
) -> dict[str, Any]:
    p = Path(compare_md_path)
    if not p.is_file():
        return {"valid": False, "status": "missing", "reason": "file_not_found"}
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"valid": False, "status": "missing", "reason": "unreadable"}

    runner_script = ""
    if "Flutter Paragraph Engine Test Comparison" in content:
        runner_script = "run_flutter_paragraph_tests.py"
    elif "WebParagraph Corpus Report" in content:
        runner_script = "run_webparagraph_corpus.py"
    else:
        return {
            "valid": False,
            "status": "wrong_runner",
            "matched_script": False,
            "reason": "wrong_runner",
        }

    if expected_script and runner_script != expected_script:
        return {
            "valid": False,
            "status": "wrong_runner",
            "matched_script": False,
            "runner_script": runner_script,
            "reason": "script_mismatch",
        }

    integrity_ok = bool(
        VALID_INTEGRITY_RE.search(content) and not INVALID_INTEGRITY_RE.search(content)
    )
    regressions = 0
    for m_reg in REGRESSIONS_COUNT_RE.finditer(content):
        regressions = max(regressions, int(m_reg.group(1)))
    # Parse top Markdown summary table candidate rows as a second check on Regressions column
    in_top_table = False
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("| Ref |") and "Regressions" in s:
            in_top_table = True
            continue
        if in_top_table:
            if not s.startswith("|"):
                break
            if ":---" in s or "*(baseline)*" in s:
                continue
            cols = [c.strip() for c in s.strip("|").split("|")]
            if len(cols) >= 3:
                reg_cell = re.sub(r"[*`\s]", "", cols[-2])
                if reg_cell.isdigit():
                    regressions = max(regressions, int(reg_cell))

    cand_shas: list[str] = []
    has_dirty_worktree = False
    latest_ts = ""
    att_files = sorted(p.parent.glob("*/attestation.json"))
    non_base_atts: list[dict[str, Any]] = []
    all_atts: list[dict[str, Any]] = []
    for af in att_files:
        try:
            att = json.loads(af.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(att, dict):
            continue
        all_atts.append(att)
        if str(att.get("label") or "").lower() != "baseline":
            non_base_atts.append(att)

    target_atts = non_base_atts or all_atts
    for att in target_atts:
        gsha = str(att.get("git_sha") or "").strip().lower()
        if len(gsha) >= 7:
            cand_shas.append(gsha)
        raw_ref = str(att.get("ref") or "").strip()
        if raw_ref.startswith("ext:"):
            ext_sha = raw_ref[4:].strip().lower()
            if len(ext_sha) >= 7:
                cand_shas.append(ext_sha)
        if raw_ref.upper() == "WORKTREE" or bool(att.get("dirty_files")):
            has_dirty_worktree = True
        ts_val = str(att.get("timestamp_utc") or "").strip()
        if ts_val and ts_val > latest_ts:
            latest_ts = ts_val

    if not cand_shas:
        shas_in_md = [m.group(1).lower() for m in ATTESTATION_SHA_CELL_RE.finditer(content)]
        cand_shas = shas_in_md[1:] if len(shas_in_md) > 1 else shas_in_md
        if "`WORKTREE`" in content:
            has_dirty_worktree = True

    ps_sha = str(patchset_sha or "").strip().lower()
    sha_matched = any(_sha_matches(ps_sha, c) for c in cand_shas) if ps_sha else False
    matches_patchset = True
    if ps_sha and cand_shas and not has_dirty_worktree and not sha_matched:
        matches_patchset = False
    if min_timestamp_utc and latest_ts and latest_ts < min_timestamp_utc and not sha_matched:
        matches_patchset = False

    if not matches_patchset:
        return {
            "valid": False,
            "status": "stale_patchset",
            "matched_script": True,
            "matches_patchset": False,
            "sha_matched": False,
            "runner_script": runner_script,
            "tested_sha": cand_shas[0] if cand_shas else "",
            "timestamp_utc": latest_ts,
            "compare_md": str(p),
        }

    passed = integrity_ok and regressions == 0
    return {
        "valid": passed,
        "status": "passed" if passed else "failed",
        "matched_script": True,
        "matches_patchset": True,
        "sha_matched": sha_matched,
        "has_dirty_worktree": has_dirty_worktree,
        "integrity_ok": integrity_ok,
        "regressions": regressions,
        "runner_script": runner_script,
        "tested_sha": cand_shas[0] if cand_shas else "",
        "timestamp_utc": latest_ts,
        "compare_md": str(p),
    }


# Locate the newest compare.md in search_dirs whose Run Attestation matches target and patchset
def find_matching_compare_md(
    search_dirs: Sequence[Path | str],
    expected_script: str,
    target_id: str,
    patchset: int,
    patchset_sha: str = "",
    min_timestamp_utc: str = "",
    require_sha_match: bool = False,
) -> dict[str, Any] | None:
    tid = str(target_id or "").strip()
    tid_re = re.compile(rf"(?:^|[^0-9]){re.escape(tid)}(?:[^0-9]|$)") if tid else None
    ps_re = re.compile(rf"(?:^|[^a-z0-9])ps0*{int(patchset)}(?:[^0-9]|$)", re.I)

    candidates: list[tuple[float, Path]] = []
    seen: set[Path] = set()
    for raw_d in search_dirs:
        d = Path(raw_d)
        if not d.is_dir():
            continue
        for p in (d / "compare.md", *d.glob("*/compare.md")):
            try:
                rp = p.resolve()
                if rp in seen or not p.is_file():
                    continue
                seen.add(rp)
                candidates.append((p.stat().st_mtime, p))
            except Exception:
                continue

    candidates.sort(key=lambda x: x[0], reverse=True)
    for _, p in candidates:
        ev = evaluate_corpus_compare_md(
            p,
            expected_script=expected_script,
            patchset_sha=patchset_sha,
            min_timestamp_utc=min_timestamp_utc,
        )
        if not ev.get("matched_script") or not ev.get("matches_patchset"):
            continue
        if ev.get("sha_matched"):
            return ev
        if require_sha_match:
            continue
        dir_str = str(p.parent)
        if tid_re and tid_re.search(dir_str):
            if patchset_sha and not ev.get("has_dirty_worktree"):
                continue
            # Dirty WORKTREE runs without SHA match cannot satisfy PS > 1 unless tagged for PS
            if int(patchset) > 1 and not ps_re.search(dir_str) and not min_timestamp_utc:
                continue
            return ev
    return None


# Read cached files, subject, and patchset SHA from cl_watcher_state.json when CLI omits them
def _lookup_watcher_item_meta(
    kind: str,
    target_id: str,
    patchset: int,
    state_path: Path | str | None,
) -> dict[str, Any]:
    if not state_path:
        return {}
    sp = Path(state_path)
    if not sp.is_file():
        return {}
    try:
        state = json.loads(sp.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    items = (state.get("items") or {}) if isinstance(state, dict) else {}
    for it in items.values():
        if not isinstance(it, dict):
            continue
        if str(it.get("kind") or "gerrit") != kind:
            continue
        if str(it.get("target_id") or "") != str(target_id):
            continue
        rev = ""
        rev_map = it.get("revision_ps_by_sha") or {}
        for sha, ps_num in rev_map.items():
            if int(ps_num or 0) == int(patchset):
                rev = str(sha)
                break
        if not rev and int(it.get("patchset") or 0) == int(patchset):
            rev = str(it.get("head_sha") or it.get("revision") or "")
        return {
            "files": list(it.get("files") or ()),
            "subject": str(it.get("subject") or ""),
            "revision": rev,
        }
    return {}


# Fall back to 2-arg load_triage_result when unit tests mock it with positional args only
def _load_triage_compat(
    kind: str, tid: str, results_dir: Path | str | None
) -> dict[str, Any] | None:
    if results_dir is None:
        return load_triage_result(kind, tid)
    try:
        return load_triage_result(kind, tid, results_dir=results_dir)
    except TypeError:
        return load_triage_result(kind, tid)


# Normalize CLI or dict core hunk entries ("file:lines:note") for Flight Deck links
def _normalize_core_hunks(
    raw_hunks: Sequence[dict[str, Any] | str] | None,
) -> list[dict[str, str]]:
    hunks: list[dict[str, str]] = []
    for item in raw_hunks or ():
        if isinstance(item, dict):
            fpath = str(item.get("file") or "").strip()
            lines = str(item.get("lines") or "").strip()
            note = str(item.get("note") or "").strip()
            if fpath:
                hunks.append({"file": fpath, "lines": lines, "note": note})
        elif isinstance(item, str) and item.strip():
            parts = item.strip().split(":", 2)
            fpath = parts[0].strip()
            lines = parts[1].strip() if len(parts) >= 2 else ""
            note = parts[2].strip() if len(parts) >= 3 else ""
            if fpath:
                hunks.append({"file": fpath, "lines": lines, "note": note})
    return hunks


# Normalize CLI or dict open-thread verdicts ("author|loc|verdict|note")
def _normalize_thread_verdicts(
    raw_verdicts: Sequence[dict[str, Any] | str] | None,
) -> list[dict[str, str]]:
    verdicts: list[dict[str, str]] = []
    for item in raw_verdicts or ():
        if isinstance(item, dict):
            verdicts.append({
                "author": str(item.get("author") or "").strip(),
                "loc": str(item.get("loc") or "").strip(),
                "verdict": str(item.get("verdict") or "").strip(),
                "note": str(item.get("note") or "").strip(),
            })
        elif isinstance(item, str) and item.strip():
            parts = [p.strip() for p in item.strip().split("|", 3)]
            verdicts.append({
                "author": parts[0] if len(parts) >= 1 else "",
                "loc": parts[1] if len(parts) >= 2 else "",
                "verdict": parts[2] if len(parts) >= 3 else "",
                "note": parts[3] if len(parts) >= 4 else "",
            })
    return verdicts


# Persist completed code review verdict and findings for Flight Deck display
def save_review_verdict(
    target_id: str,
    kind: str = "gerrit",
    patchset: int = 1,
    outcome: str = "CLEAN",
    summary: str = "",
    draft: str = "",
    report_markdown: str = "",
    companion_id: str = "",
    corpus: str = "auto",
    compare_md: Path | str | None = None,
    g3_compare_md: Path | str | None = None,
    resolved_thread_ids: Sequence[str] | None = None,
    draft_ids: Sequence[str] | None = None,
    revision: str = "",
    delta_kind: str = "",
    core_hunks: Sequence[dict[str, Any] | str] | None = None,
    open_thread_verdicts: Sequence[dict[str, Any] | str] | None = None,
    audit_state: str = "done",
    files: Iterable[str] | None = None,
    verify_attestation: bool = False,
    compare_search_dirs: Sequence[Path | str] | None = None,
    state_path: Path | str | None = None,
    results_dir: Path | str | None = None,
) -> Path:
    normalized_outcome = normalize_review_outcome(outcome)
    corpus_arg = normalize_corpus_arg(corpus)
    iso_ts, et_ts = now_et_strings()
    safe_ps = int(patchset) if patchset else 1

    existing = _load_triage_compat(kind, str(target_id), results_dir) or {}
    eff_state_path = (
        state_path
        if state_path is not None
        else (WATCHER_STATE_FILE if results_dir is None else None)
    )
    watcher_meta = _lookup_watcher_item_meta(kind, str(target_id), safe_ps, eff_state_path)

    eff_rev = str(revision or "").strip()
    if not eff_rev and int(existing.get("triaged_patchset") or 0) == safe_ps:
        eff_rev = str(existing.get("triaged_revision") or "").strip()
    if not eff_rev:
        eff_rev = str(watcher_meta.get("revision") or "").strip()

    corpus_req = False
    corpus_script = ""
    corpus_domain = ""
    corpus_auth = False

    rep_gate = detect_corpus_from_report_markdown(report_markdown)
    if rep_gate is not None:
        corpus_req = bool(rep_gate["required"])
        corpus_script = str(rep_gate.get("script") or "")
        corpus_domain = str(rep_gate.get("domain") or "")
        corpus_auth = bool(rep_gate.get("authoritative"))
    elif files is not None:
        fg = detect_corpus_from_files(files, str(existing.get("subject") or ""))
        corpus_req = bool(fg["required"])
        corpus_script = str(fg["script"])
        corpus_domain = str(fg["domain"])
        corpus_auth = True
    elif int(existing.get("triaged_patchset") or 0) == safe_ps and "corpus_required" in existing:
        corpus_req = bool(existing.get("corpus_required"))
        corpus_script = str(existing.get("corpus_script") or "")
        corpus_domain = str(existing.get("corpus_domain") or "")
        corpus_auth = bool(existing.get("corpus_gate_authoritative"))
    elif watcher_meta.get("files"):
        fg = detect_corpus_from_files(watcher_meta["files"], watcher_meta.get("subject", ""))
        corpus_req = bool(fg["required"])
        corpus_script = str(fg["script"])
        corpus_domain = str(fg["domain"])
    elif existing.get("corpus_required"):
        corpus_req = True
        corpus_script = str(existing.get("corpus_script") or "")
        corpus_domain = str(existing.get("corpus_domain") or "")

    eff_search_dirs: list[Path | str] = (
        list(compare_search_dirs)
        if compare_search_dirs is not None
        else ([Path("/tmp")] if results_dir is None else [])
    )

    tested_ps = 0
    tested_sha = ""
    matched_compare_md = ""
    if corpus_arg == "not_required":
        corpus_req = False
        corpus_status = "not_required"
        corpus_auth = True
    elif corpus_arg in ("pending", "failed", "waived"):
        corpus_req = True
        corpus_script = corpus_script or "run_flutter_paragraph_tests.py"
        corpus_status = corpus_arg
    elif corpus_arg == "passed":
        corpus_req = True
        corpus_script = corpus_script or "run_flutter_paragraph_tests.py"
        if compare_md:
            ev = evaluate_corpus_compare_md(
                compare_md, expected_script=corpus_script, patchset_sha=eff_rev
            )
            if ev.get("status") == "passed":
                corpus_status = "passed"
                tested_ps = safe_ps
                tested_sha = str(ev.get("tested_sha") or "")
                matched_compare_md = str(compare_md)
            elif ev.get("status") == "failed":
                corpus_status = "failed"
            else:
                corpus_status = "pending"
        elif verify_attestation:
            ev = find_matching_compare_md(
                eff_search_dirs, corpus_script, str(target_id), safe_ps, patchset_sha=eff_rev
            )
            if ev and ev.get("status") == "passed":
                corpus_status = "passed"
                tested_ps = safe_ps
                tested_sha = str(ev.get("tested_sha") or "")
                matched_compare_md = str(ev.get("compare_md") or "")
            elif ev and ev.get("status") == "failed":
                corpus_status = "failed"
            else:
                corpus_status = "pending"
                print(
                    f"Warning: --corpus passed requested without valid compare.md Run "
                    f"Attestation for PS{safe_ps}; recording corpus_status=pending.",
                    file=sys.stderr,
                )
        else:
            corpus_status = "passed"
            tested_ps = safe_ps
    else:
        # Auto mode derives corpus status from requirement and compare.md Run Attestation
        if not corpus_req and not compare_md:
            corpus_status = "not_required"
        else:
            corpus_req = True
            corpus_script = corpus_script or "run_flutter_paragraph_tests.py"
            ev = (
                evaluate_corpus_compare_md(
                    compare_md, expected_script=corpus_script, patchset_sha=eff_rev
                )
                if compare_md
                else find_matching_compare_md(
                    eff_search_dirs,
                    corpus_script,
                    str(target_id),
                    safe_ps,
                    patchset_sha=eff_rev,
                )
            )
            if ev and ev.get("status") == "passed":
                corpus_status = "passed"
                tested_ps = safe_ps
                tested_sha = str(ev.get("tested_sha") or "")
                matched_compare_md = str(ev.get("compare_md") or compare_md or "")
            elif ev and ev.get("status") == "failed":
                corpus_status = "failed"
            else:
                corpus_status = "pending"

    labels = format_review_labels(
        normalized_outcome, safe_ps, corpus_req, corpus_status, corpus_script
    )
    # Truncate on a word boundary so card summaries never cut off mid-word
    clean_summary = truncate_summary(summary, 180)
    data = {
        "target_id": str(target_id),
        "item_key": f"{kind}:{target_id}",
        "kind": kind,
        "verdict": normalized_outcome,
        **labels,
        "review_outcome": normalized_outcome,
        "corpus_required": corpus_req,
        "corpus_status": corpus_status,
        "corpus_script": corpus_script,
        "corpus_domain": corpus_domain,
        "corpus_gate_authoritative": corpus_auth,
        "corpus_gate_evaluated_ps": safe_ps if corpus_auth else 0,
        "corpus_tested_patchset": tested_ps,
        "corpus_tested_sha": tested_sha,
        "corpus_compare_md": matched_compare_md,
        "g3_corpus_compare_md": str(g3_compare_md or "").strip(),
        "resolved_thread_ids": [
            str(x).strip() for x in (resolved_thread_ids or ()) if str(x).strip()
        ],
        "draft_ids": [str(x).strip() for x in (draft_ids or ()) if str(x).strip()],
        "triaged_revision": eff_rev,
        "est_minutes": 0.0,
        "review_time_label": "",
        "summary": clean_summary,
        "triaged_patchset": safe_ps,
        "triaged_at": iso_ts,
        "triaged_at_et": et_ts,
        "source": "review",
        "companion_id": str(companion_id or "").strip(),
        "draft": draft.strip(),
        "report_markdown": report_markdown.strip(),
        "delta_kind": str(delta_kind or "").strip(),
        "core_hunks": _normalize_core_hunks(core_hunks),
        "open_thread_verdicts": _normalize_thread_verdicts(open_thread_verdicts),
        "audit_state": str(audit_state or "done").strip().lower(),
        "audit_target_patchset": safe_ps,
        "audit_target_revision": eff_rev,
        "audit_error": "",
    }
    return save_triage_result(data, results_dir=results_dir)


# Persist zero-token Tier 0 rebase_only or commit_msg_only outcome without waking an LLM chat
def record_tier0_delta_verdict(
    target_id: str,
    kind: str = "gerrit",
    patchset: int = 1,
    revision: str = "",
    delta_kind: str = "rebase_only",
    summary: str = "",
    draft: str = "",
    files: Iterable[str] | None = None,
    results_dir: Path | str | None = None,
) -> Path:
    existing = _load_triage_compat(kind, str(target_id), results_dir) or {}
    prev_outcome = str(existing.get("review_outcome") or existing.get("verdict") or "CLEAN")
    try:
        norm_outcome = normalize_review_outcome(prev_outcome)
    except ValueError:
        norm_outcome = "CLEAN"

    # Commit-message-only edits keep identical tree and base, so explicit corpus state stays valid
    corpus_mode = "auto"
    prev_corpus = str(existing.get("corpus_status") or "").lower()
    if delta_kind == "commit_msg_only" and prev_corpus in ("passed", "failed", "waived"):
        corpus_mode = prev_corpus

    return save_review_verdict(
        target_id=target_id,
        kind=kind,
        patchset=patchset,
        outcome=norm_outcome,
        summary=summary or str(existing.get("summary") or ""),
        draft=draft,
        report_markdown=str(existing.get("report_markdown") or ""),
        companion_id=str(existing.get("companion_id") or ""),
        corpus=corpus_mode,
        revision=revision,
        delta_kind=delta_kind,
        core_hunks=existing.get("core_hunks") or [],
        open_thread_verdicts=existing.get("open_thread_verdicts") or [],
        audit_state="done",
        files=files,
        verify_attestation=False,
        results_dir=results_dir,
    )


# Record live audit progress or preflight auth block without overwriting prior patchset verdict
def record_audit_status(
    target_id: str,
    kind: str = "gerrit",
    patchset: int = 1,
    revision: str = "",
    audit_state: str = "running",
    audit_error: str = "",
    results_dir: Path | str | None = None,
) -> Path:
    target_dir = ensure_results_dir(results_dir)
    s_kind = str(kind or "gerrit").lower()
    tid = str(target_id or "").strip()
    dest_path = target_dir / canonical_filename(s_kind, tid)
    existing = _load_triage_compat(s_kind, tid, results_dir) or {
        "target_id": tid,
        "item_key": f"{s_kind}:{tid}",
        "kind": s_kind,
        "verdict": "SURGICAL_AUDIT",
        "verdict_label": "SURGICAL AUDIT",
        "triaged_patchset": 0,
        "summary": "",
        "source": "queue_triage",
        "schema_version": 1,
    }
    updated = dict(existing)
    updated["audit_state"] = str(audit_state or "running").strip().lower()
    updated["audit_target_patchset"] = int(patchset or 1)
    updated["audit_target_revision"] = str(revision or "").strip()
    updated["audit_error"] = str(audit_error or "").strip()
    tmp_path = target_dir / f"{dest_path.name}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(updated, f, indent=2, ensure_ascii=False)
    tmp_path.replace(dest_path)
    return dest_path


# Parse overview markdown from run_queue_triage.py (e.g. /tmp/triage_1030.md)
def parse_queue_triage_markdown(
    md_text: str,
    source_path: str | None = None,
) -> list[dict[str, Any]]:
    sections = re.split(r"^### Детали по\s+(\S+)", md_text, flags=re.M)
    results: list[dict[str, Any]] = []
    file_mtime: float | None = None
    if source_path and os.path.isfile(source_path):
        try:
            file_mtime = os.path.getmtime(source_path)
        except OSError:
            file_mtime = None
    iso_ts, et_ts = now_et_strings(file_mtime)

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

        summary = truncate_summary(reason, 180)
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

        # Match parent target_id URL first so inline companion links never hijack card URLs
        url_m = None
        if kind != "github":
            parent_url_m = (
                re.search(rf"\bhttps://[^\s)>]+/\+/{re.escape(target_id)}(?:/\d+)?\b", body)
                if target_id.isdigit()
                else None
            )
            url_m = parent_url_m or re.search(r"\bhttps://[^\s)>]+/\+/(\d+)", body)
        if url_m:
            url = url_m.group(0)
        else:
            parent_gh_m = (
                re.search(
                    rf"\bhttps://github\.com/[^\s/]+/[^\s/]+/pull/{re.escape(target_id)}\b",
                    body,
                )
                if target_id.isdigit()
                else None
            )
            url_gh = parent_gh_m or RE_GITHUB_PR_URL.search(body)
            if url_gh:
                kind = "github"
                raw_u = url_gh.group(0)
                url = raw_u if raw_u.startswith("https://") else f"https://{raw_u}"
                if not pr_m and not parent_gh_m:
                    target_id = url_gh.group(2)

        rep_gate = detect_corpus_from_report_markdown(body)
        corpus_req = bool(rep_gate and rep_gate["required"])
        corpus_script = str((rep_gate or {}).get("script") or "")
        corpus_domain = str((rep_gate or {}).get("domain") or "")
        corpus_auth = bool((rep_gate or {}).get("authoritative"))

        results.append({
            "target_id": target_id,
            "item_key": f"{kind}:{target_id}",
            "kind": kind,
            "url": url,
            "subject": subject,
            "verdict": verdict,
            "verdict_label": verdict.replace("_", " "),
            "corpus_required": corpus_req,
            "corpus_status": "pending" if corpus_req else "not_required",
            "corpus_script": corpus_script,
            "corpus_domain": corpus_domain,
            "corpus_gate_authoritative": corpus_auth,
            "corpus_gate_evaluated_ps": triaged_ps if corpus_auth else 0,
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
    heading_pat = (
        r"(?:^#{1,4}\s+|\*\*)(?:Ready-to-copy(?:\s+review)?\s+draft|"
        r"Ready-to-post\s+Draft|Reply\s+draft|Draft)\b[^\n]*\n+"
    )
    # Code block immediately following a Markdown or bold Draft heading
    m = re.search(
        heading_pat + r"```[a-zA-Z0-9_-]*\s*\n(.*?)\n```",
        body,
        re.S | re.I | re.M,
    )
    if m and m.group(1).strip():
        return m.group(1).strip()
    # Blockquote immediately following a Draft heading
    m_bq = re.search(
        heading_pat + r">\s*(.*?)(?=\n\n|\n##|\Z)",
        body,
        re.S | re.I | re.M,
    )
    if m_bq and m_bq.group(1).strip():
        val = m_bq.group(1).strip()
        if "no comment needed" in val.lower():
            return ""
        return val
    # Inline Draft: <text>
    m_in = re.search(r"[-*]?\s*\*\*Draft:\*\*\s*([^\n]+)", body, re.I)
    if m_in and m_in.group(1).strip():
        val = m_in.group(1).strip()
        if "no comment needed" in val.lower():
            return ""
        return val
    # Reply draft heading followed by --- fenced text
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


# Extract triaged commit or PR head SHA for the target patchset from audit markdown
def extract_audit_revision(
    body: str,
    kind: str,
    target_id: str,
    triaged_ps: int,
) -> str:
    if not body:
        return ""
    if kind == "gerrit" and triaged_ps > 0:
        # Match [PS6](.../+/1409697/6) (`6e8dd008c2`) or [PS6] (`6e8dd008c2`) for this CL only
        link_ps_re = re.compile(
            rf"\[([^\]]*\bPS{triaged_ps}\b[^\]]*)\](?:\(([^)\n]*)\))?[^(\n]*\(\s*"
            r"`?([0-9a-f]{7,40})`?(?:[,)\s])"
        )
        for m in link_ps_re.finditer(body):
            lbl, url, sha = m.group(1), m.group(2) or "", m.group(3)
            ids_in_ref = re.findall(r"\b(?:Gerrit|CL|cl/)\s*(\d+)", lbl, re.I)
            url_m = RE_GERRIT_URL_ID.search(url)
            if url_m:
                ids_in_ref.append(url_m.group(1))
            if target_id and any(cid != target_id for cid in ids_in_ref):
                continue
            return sha.lower()

        plain_ps_re = re.compile(
            rf"(?<!\[)\bPS{triaged_ps}\b(?:\*\*|\s)*\(\s*`?([0-9a-f]{{7,40}})`?(?:[,)\s])"
        )
        for m in plain_ps_re.finditer(body):
            prefix = body[max(0, m.start() - 32) : m.start()]
            other_ids = re.findall(r"\b(?:Gerrit|CL|cl/)\s*(\d+)", prefix, re.I)
            if target_id and any(cid != target_id for cid in other_ids):
                continue
            return m.group(1).lower()

        inline_ps_m = re.search(rf"\bPS{triaged_ps}\s+`([0-9a-f]{{7,40}})`", body)
        if inline_ps_m:
            return inline_ps_m.group(1).lower()

    if kind == "github":
        for pat in (
            r"\bnew\s+head[:*\s]+`?([0-9a-f]{7,40})`?\b",
            r"\bhead\s+`([0-9a-f]{7,40})`",
            r"\bthe\s+head\s+is(?:\s+still|\s+now)?\s+`?([0-9a-f]{7,40})`?\b",
        ):
            hm = re.search(pat, body, re.I)
            if hm:
                return hm.group(1).lower()
    else:
        hm = re.search(r"\bhead\s+`([0-9a-f]{7,40})`", body, re.I)
        if hm:
            return hm.group(1).lower()
    return ""


# Parse a single audit section from a wf*.md file
def parse_single_audit_text(
    body: str,
    source_path: str | None = None,
) -> dict[str, Any] | None:
    # Pick whichever target reference (Gerrit CL/URL vs GitHub PR) appears first in the report
    gerrit_candidates: list[tuple[int, str]] = []
    m_named = re.search(r"\b(?:cl/|CL\s+|Gerrit\s+)(\d+)", body, re.I)
    if m_named:
        gerrit_candidates.append((m_named.start(), m_named.group(1)))
    m_url = RE_GERRIT_URL_ID.search(body)
    if m_url:
        gerrit_candidates.append((m_url.start(), m_url.group(1)))
    gerrit_hit = min(gerrit_candidates, key=lambda x: x[0]) if gerrit_candidates else None

    pr_m = re.search(r"\b(?:PR\s+#|pull/)(\d+)", body, re.I)
    gh_hit = (pr_m.start(), pr_m.group(1)) if pr_m else None

    if gerrit_hit and (not gh_hit or gerrit_hit[0] <= gh_hit[0]):
        kind = "gerrit"
        target_id = gerrit_hit[1]
    elif gh_hit:
        kind = "github"
        target_id = gh_hit[1]
    else:
        return None

    triaged_ps = 1
    if kind == "gerrit":
        # Prefer explicit patchset attached to target_id before generic PS<N> mentions
        ps_target = re.search(
            rf"(?:\b(?:Gerrit|CL|cl/)\s*{re.escape(target_id)}\s+PS(\d+)\b|"
            rf"/\+/{re.escape(target_id)}/(\d+)\b)",
            body,
            re.I,
        )
        if ps_target:
            triaged_ps = int(ps_target.group(1) or ps_target.group(2))
        else:
            ps_m = re.search(r"\bPS(\d+)\b", body)
            if ps_m:
                triaged_ps = int(ps_m.group(1))
    else:
        ps_m = re.search(r"\bPS(\d+)\b", body)
        if ps_m:
            triaged_ps = int(ps_m.group(1))

    triaged_rev = extract_audit_revision(body, kind, target_id, triaged_ps)

    cat_m = re.search(r"[-*]?\s*\*\*Category:\*\*\s*([^\n]+)", body, re.I)
    cat_raw = cat_m.group(1).strip() if cat_m else "Surgical audit"
    verdict = normalize_verdict(cat_raw)

    time_m = RE_TIME_MIN.search(body)
    est_min = float(time_m.group(1)) if time_m else 2.0
    if est_min == 0:
        verdict = "MECHANICAL"
    time_label = f"~{int(round(est_min))}m"

    draft = extract_draft_from_audit(body)
    file_mtime: float | None = None
    if source_path and os.path.isfile(source_path):
        try:
            file_mtime = os.path.getmtime(source_path)
        except OSError:
            file_mtime = None
    iso_ts, et_ts = now_et_strings(file_mtime)

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
        if (
            "already approved" in lower_body
            or "+1 still stands" in lower_body
            or "cr+1 carried over" in lower_body
            or "cr+1 was copied over" in lower_body
        ):
            summary = "No comment needed (already approved)"
        else:
            summary = "No comment needed"
    elif "pushback" in cat_raw.lower():
        summary = "Needs pushback on missing tests or scope"
    elif "reassign" in cat_raw.lower():
        summary = "Needs reassignment to domain owner"
    else:
        summary = truncate_summary(cat_raw, 180)

    # Infer structured review outcome and badges from audit text if present
    review_outcome = ""
    if verdict == "PUSHBACK" or "pushback" in cat_raw.lower():
        review_outcome = "PUSHBACK"
    elif verdict == "REASSIGN" or "reassign" in cat_raw.lower():
        review_outcome = "REASSIGN"
    elif (
        verdict == "MECHANICAL"
        or "lgtm" in summary.lower()
        or "no comment needed" in summary.lower()
        or "approve with conditions" in cat_raw.lower()
    ):
        review_outcome = "CLEAN"

    rep_gate = detect_corpus_from_report_markdown(f"{body}\n{draft}")
    corpus_req = bool(rep_gate and rep_gate["required"])
    corpus_script = str((rep_gate or {}).get("script") or "")
    corpus_domain = str((rep_gate or {}).get("domain") or "")
    corpus_status = "pending" if corpus_req else "not_required"

    if review_outcome:
        labels = format_review_labels(
            review_outcome, triaged_ps, corpus_req, corpus_status, corpus_script
        )
    else:
        labels = {
            "verdict_label": verdict.replace("_", " "),
            "review_outcome_label": "",
            "review_eyebrow_label": "",
            "verdict_tone": verdict.lower(),
        }

    result: dict[str, Any] = {
        "target_id": target_id,
        "item_key": f"{kind}:{target_id}",
        "kind": kind,
        "verdict": verdict,
        **labels,
        "audit_outcome": cat_raw[:50],
        "review_outcome": review_outcome,
        "corpus_required": corpus_req,
        "corpus_status": corpus_status,
        "corpus_script": corpus_script,
        "corpus_domain": corpus_domain,
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
    if triaged_rev:
        result["triaged_revision"] = triaged_rev
    return result


# Parse deep audit markdown files (e.g. /tmp/wf516_*.md)
def parse_wf_audit_markdown(
    md_text: str,
    source_path: str | None = None,
) -> list[dict[str, Any]]:
    # Handle multi-item files split by **(1), **(2) or multiple ## CL/PR headings
    parts = re.split(r"^\*\*\(\d+\)\s+", md_text, flags=re.M)
    items: list[dict[str, Any]] = []
    if len(parts) > 1:
        for p in parts[1:]:
            res = parse_single_audit_text(p, source_path=source_path)
            if res:
                items.append(res)
        return items

    h2_starts = [
        m.start()
        for m in re.finditer(
            r"^##\s+[^\n]*(?:\bGerrit\s+\d+|\bCL\s+\d+|\bcl/\d+|/\+/\d+|\bPR\s+#\d+|pull/\d+)",
            md_text,
            flags=re.M | re.I,
        )
    ]
    if len(h2_starts) >= 2:
        for idx, start in enumerate(h2_starts):
            end = h2_starts[idx + 1] if idx + 1 < len(h2_starts) else len(md_text)
            res = parse_single_audit_text(md_text[start:end], source_path=source_path)
            if res:
                items.append(res)
        return items

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

    # Keep only the newest per-item wf*.md report so stale files never oscillate head SHAs
    wf_files = sorted(td.glob("wf*.md"), key=lambda p: p.stat().st_mtime)
    latest_wf_by_key: dict[str, dict[str, Any]] = {}
    for wf in wf_files:
        try:
            content = wf.read_text(encoding="utf-8", errors="replace")
            for res in parse_wf_audit_markdown(content, source_path=str(wf)):
                ikey = str(res.get("item_key") or f"{res.get('kind')}:{res.get('target_id')}")
                latest_wf_by_key[ikey] = res
        except Exception:
            continue
    for res in latest_wf_by_key.values():
        save_triage_result(res, results_dir=results_dir)
        count += 1
    return count


# Save a single triage result directly from run_queue_triage.py or triage_cl.py
def save_from_triage_cl_output(
    cl_url: str,
    analysis_json: dict[str, Any],
    report_markdown: str,
    results_dir: Path | str | None = None,
) -> Path:
    meta = analysis_json.get("metadata") or {}
    analysis = analysis_json.get("analysis") or {}

    target_id = str(
        meta.get("id") or meta.get("cl_id") or meta.get("_number") or meta.get("number") or ""
    )
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
    tctx = meta.get("thread_context") or {}
    cur_ps = int(tctx.get("current_ps") or meta.get("patchset") or 1)
    cur_rev = str(tctx.get("current_sha") or meta.get("head_sha") or "").strip()

    # Check for already-reviewed status on current patchset
    if tctx.get("reviewed_current_ps"):
        verdict = "MECHANICAL"
        summary = "Already reviewed on this patchset; no comment needed"
    else:
        summary = reason[:90]

    cg = analysis.get("corpus_gate")
    if isinstance(cg, dict) and "required" in cg:
        corpus_req = bool(cg.get("required")) and not bool(analysis.get("is_revert"))
        corpus_script = str(cg.get("script") or "") if corpus_req else ""
        corpus_domain = str(cg.get("domain") or "") if corpus_req else ""
        corpus_auth = True
    else:
        rep_gate = detect_corpus_from_report_markdown(report_markdown)
        corpus_req = bool(rep_gate and rep_gate["required"])
        corpus_script = str((rep_gate or {}).get("script") or "")
        corpus_domain = str((rep_gate or {}).get("domain") or "")
        corpus_auth = bool((rep_gate or {}).get("authoritative"))

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
        "corpus_required": corpus_req,
        "corpus_status": "pending" if corpus_req else "not_required",
        "corpus_script": corpus_script,
        "corpus_domain": corpus_domain,
        "corpus_gate_authoritative": corpus_auth,
        "corpus_gate_evaluated_ps": cur_ps if corpus_auth else 0,
        "triaged_revision": cur_rev,
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

        # GitHub PRs keep patchset=1 before reviews exist, so head_sha drift marks triage stale
        cur_rev = str(it.get("head_sha") or it.get("revision") or "").strip().lower()
        tr_rev = str(tr.get("triaged_revision") or "").strip().lower()
        if len(cur_rev) >= 7 and len(tr_rev) >= 7 and not _sha_matches(cur_rev, tr_rev):
            is_stale = True

        # Propagate reviewed patchset to item when review outcome is recorded
        if tr.get("review_outcome") and tr_ps is not None:
            try:
                it["reviewed_ps"] = max(int(it.get("reviewed_ps") or 0), int(tr_ps))
            except (ValueError, TypeError):
                pass

        it_corpus = detect_corpus_from_files(
            it.get("files") or (), str(it.get("subject") or tr.get("subject") or "")
        )
        corpus_req = bool(tr.get("corpus_required"))
        corpus_script = str(tr.get("corpus_script") or "")
        corpus_domain = str(tr.get("corpus_domain") or "")
        if is_stale or not tr.get("corpus_gate_authoritative"):
            if it_corpus["required"]:
                corpus_req = True
                corpus_script = corpus_script or str(it_corpus["script"])
                corpus_domain = corpus_domain or str(it_corpus["domain"])

        c_status = str(tr.get("corpus_status") or "").lower()
        if not corpus_req:
            c_status = "not_required"
        elif is_stale:
            # Uploading a new patchset or pushing a new commit resets corpus gate to pending
            c_status = "pending"
        else:
            if c_status in ("", "not_required", "required"):
                c_status = "pending"
            tested_ps = int(tr.get("corpus_tested_patchset") or tr_ps or 0)
            eff_ps = int(cur_ps or tr_ps or 1)
            if c_status == "passed" and tested_ps != eff_ps:
                c_status = "pending"
            # Upgrade pending to passed in production when compare.md attests this patchset
            if c_status == "pending" and results_dir is None and len(cur_rev) >= 7:
                ev = find_matching_compare_md(
                    [Path("/tmp")],
                    corpus_script or "run_flutter_paragraph_tests.py",
                    tid,
                    eff_ps,
                    patchset_sha=cur_rev,
                )
                if ev and ev.get("status") == "passed":
                    c_status = "passed"

        label_updates: dict[str, Any] = {}
        if tr.get("review_outcome") and not is_stale:
            label_updates = format_review_labels(
                str(tr["review_outcome"]),
                int(tr_ps or 1),
                corpus_req,
                c_status,
                corpus_script,
            )

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
            **label_updates,
            "corpus_required": corpus_req,
            "corpus_status": c_status,
            "corpus_script": corpus_script,
            "corpus_domain": corpus_domain,
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

    # Subcommand to persist completed review outcome for Flight Deck display
    p_rev = sub.add_parser("save-review", help="Save a completed code review verdict")
    p_rev.add_argument("--target", required=True, help="CL number or PR number")
    p_rev.add_argument("--kind", default="gerrit", choices=["gerrit", "github"])
    p_rev.add_argument("--patchset", type=int, required=True, help="Reviewed patchset")
    p_rev.add_argument("--outcome", required=True, help="CLEAN, NITS, COMPANION_FIX, etc.")
    p_rev.add_argument("--summary", required=True, help="Concise 1-line findings summary")
    p_rev.add_argument("--draft", default="", help="Ready-to-post draft string")
    p_rev.add_argument("--draft-file", default="", help="File path with ready-to-post draft")
    p_rev.add_argument("--report-file", default="", help="Path to markdown report file")
    p_rev.add_argument("--companion", default="", help="Companion CL or PR ID")
    p_rev.add_argument(
        "--corpus",
        default="auto",
        choices=["auto", "not_required", "required", "pending", "passed", "failed", "waived"],
        help="Corpus gate status (auto derives from triage_cl.py and compare.md attestation)",
    )
    p_rev.add_argument(
        "--compare-md",
        default="",
        help="Path to compare.md Run Attestation report for the reviewed patchset",
    )
    p_rev.add_argument(
        "--g3-compare-md",
        default="",
        help="Path to google3 A/B compare.md report for the reviewed patchset",
    )
    p_rev.add_argument(
        "--resolved-thread-id",
        action="append",
        default=[],
        help="Own prior-patchset comment/thread ID verified as addressed (repeatable)",
    )
    p_rev.add_argument(
        "--draft-id",
        action="append",
        default=[],
        help="Saved Gerrit draft ID intended for atomic publish (repeatable)",
    )
    p_rev.add_argument(
        "--revision",
        default="",
        help="Commit or head SHA of the reviewed patchset",
    )
    p_rev.add_argument(
        "--delta-kind",
        default="",
        help=(
            "Delta classification (initial, code_change, rebase_only, "
            "commit_msg_only, thread_only)"
        ),
    )
    p_rev.add_argument(
        "--core-hunk",
        action="append",
        default=[],
        help="Repeatable core hunk pointer formatted as file:lines:note",
    )
    p_rev.add_argument(
        "--thread-verdict",
        action="append",
        default=[],
        help="Repeatable open-thread verdict formatted as author|loc|verdict|note",
    )
    p_rev.add_argument(
        "--audit-state",
        default="done",
        choices=["queued", "running", "done", "blocked_auth", "failed"],
        help="Current audit execution state",
    )

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
            parsed["summary"] = truncate_summary(args.summary, 180)
        if args.patchset:
            parsed["triaged_patchset"] = args.patchset
        if args.est_minutes:
            parsed["est_minutes"] = args.est_minutes
            parsed["review_time_label"] = f"~{int(round(args.est_minutes))}m"
        p = save_triage_result(parsed)
        print(f"Saved audit result to {p}")
        return 0

    # Execute save-review command reading drafts and reports from flags or files
    if args.cmd == "save-review":
        draft_text = args.draft or ""
        if args.draft_file:
            df = Path(args.draft_file)
            if df.exists():
                draft_text = df.read_text(encoding="utf-8", errors="replace")
        report_text = ""
        if args.report_file:
            rf = Path(args.report_file)
            if rf.exists():
                report_text = rf.read_text(encoding="utf-8", errors="replace")
        p = save_review_verdict(
            target_id=args.target,
            kind=args.kind,
            patchset=args.patchset,
            outcome=args.outcome,
            summary=args.summary,
            draft=draft_text,
            report_markdown=report_text,
            companion_id=args.companion,
            corpus=args.corpus,
            compare_md=args.compare_md or None,
            g3_compare_md=args.g3_compare_md or None,
            resolved_thread_ids=args.resolved_thread_id,
            draft_ids=args.draft_id,
            revision=args.revision,
            delta_kind=args.delta_kind,
            core_hunks=args.core_hunk,
            open_thread_verdicts=args.thread_verdict,
            audit_state=args.audit_state,
            verify_attestation=True,
        )
        print(f"Saved review verdict to {p}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
