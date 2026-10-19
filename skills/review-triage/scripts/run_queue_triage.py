#!/usr/bin/env python3
"""
run_queue_triage.py — Daily Morning Review Queue Triage for jlavrova@google.com.

Queries Gerrit (and other VCS where configured) for pending, non-WIP reviews,
runs review_triage.py on each CL, and generates a structured morning digest.
"""

import argparse
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import urllib.request

TRIAGE_SCRIPT = os.path.expanduser("~/.gemini/config/skills/review-triage/scripts/triage_cl.py")
REVIEWER_EMAIL = "jlavrova@google.com"
GERRIT_HOST = "skia-review.googlesource.com"
IGNORED_CLS_FILE = os.path.expanduser("~/.gemini/config/skills/review-triage/ignored_cls.txt")


def load_ignored_cls():
    # Load user-specified CL numbers to exclude from triage
    ignored = set()
    if os.path.isfile(IGNORED_CLS_FILE):
        with open(IGNORED_CLS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    ignored.add(line)
    return ignored


def parse_gerrit_time(ts_str):
    if not ts_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def fetch_pending_gerrit_reviews():
    """Fetch open reviews where reviewer is jlavrova, excluding self and WIP."""
    # Exclude changes authored by the reviewer to prevent self-review noise
    query = f"is:open+reviewer:{REVIEWER_EMAIL}+-owner:{REVIEWER_EMAIL}+-is:wip"
    url = f"https://{GERRIT_HOST}/changes/?q={query}&n=25&o=DETAILED_ACCOUNTS"
    req = urllib.request.Request(url, headers={"User-Agent": "ReviewTriageQueue"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if raw.startswith(")]}'"):
                raw = raw[4:]
            changes = json.loads(raw)
            # Filter out any self-owned CLs defensively
            return [c for c in changes if c.get("owner", {}).get("email") != REVIEWER_EMAIL]
    except Exception as e:
        print(f"Warning: Failed to fetch Gerrit reviews: {e}", file=sys.stderr)
        return []


def main():
    parser = argparse.ArgumentParser(description="Daily Morning Review Queue Triage")
    parser.add_argument(
        "--max-days",
        type=int,
        default=7,
        help="Only include CLs created within N days (default: 7)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include all pending CLs regardless of creation age"
    )
    args = parser.parse_args()

    ignored_cls = load_ignored_cls()
    raw_changes = fetch_pending_gerrit_reviews()
    now = datetime.now(timezone.utc)

    dated_changes = []
    for c in raw_changes:
        cl_id = str(c.get("_number"))
        if cl_id in ignored_cls:
            continue
        created_dt = parse_gerrit_time(c.get("created"))
        age_days = (now - created_dt).total_seconds() / 86400.0
        if not args.all and age_days > args.max_days:
            continue
        dated_changes.append((created_dt, c))

    # Sort newest first by creation timestamp
    dated_changes.sort(key=lambda x: x[0], reverse=True)
    changes = [x[1] for x in dated_changes]
    if not changes:
        print("## 🌅 Morning Review Queue: Empty 🎉\n")
        print("В очереди нет открытых СЛ, ожидающих твоего ревью. Отличное утро для спокойного кодинга!")
        return

    print(f"## 🌅 Morning Review Queue ({len(changes)} pending CLs)\n")
    print(f"*Очередь собрана на основе Gerrit ({GERRIT_HOST}) для `{REVIEWER_EMAIL}`.*\n")

    triage_results = []
    total_est_minutes = 0

    # 1. Quick pass across all CLs
    for c in changes:
        cl_num = str(c["_number"])
        subj = c.get("subject", "")
        owner = c.get("owner", {}).get("name", "Unknown")

        try:
            cmd = ["python3", TRIAGE_SCRIPT, cl_num, "--json"]
            out = subprocess.check_output(cmd, text=True, timeout=30)
            data = json.loads(out)
            cat = data["analysis"]["category"]
            churn = data["analysis"]["impl_churn"]
            reason = data["analysis"]["reason"]
        except Exception as e:
            cat = "SURGICAL_AUDIT"
            churn = 0
            reason = f"Automated triage error: {e}"

        time_map = {
            "MECHANICAL": 0.5,
            "REASSIGN": 0.5,
            "PUSHBACK": 1.0,
            "SURGICAL_AUDIT": 3.0
        }
        est_min = time_map.get(cat, 3.0)
        total_est_minutes += est_min

        triage_results.append({
            "cl": cl_num,
            "subject": subj,
            "owner": owner,
            "category": cat,
            "churn": churn,
            "est_min": est_min,
            "reason": reason
        })

    # Summary Table
    cat_icons = {
        "MECHANICAL": "🟢 `[MECHANICAL]`",
        "REASSIGN": "🟡 `[REASSIGN]`",
        "SURGICAL_AUDIT": "🔴 `[SURGICAL_AUDIT]`",
        "PUSHBACK": "🟠 `[PUSHBACK]`"
    }

    print("### 📋 Сводка очереди")
    print(f"**Оценочное общее время на ревью всей очереди:** `~{int(total_est_minutes + 0.9)} мин.`\n")
    print("| СЛ | Категория | Логика ядра | Автор | Тема |")
    print("|---|---|---|---|---|")
    for r in triage_results:
        badge = cat_icons.get(r["category"], r["category"])
        cl_link = f"[{r['cl']}](https://{GERRIT_HOST}/c/skia/+/{r['cl']})"
        churn_str = f"`{r['churn']} строк`" if r['churn'] > 0 else "—"
        subj_trunc = r['subject'][:45] + ("..." if len(r['subject']) > 45 else "")
        print(f"| {cl_link} | {badge} | {churn_str} | `{r['owner']}` | {subj_trunc} |")

    print("\n---\n")

    # Detailed cards for each CL
    for r in triage_results:
        cl_num = r["cl"]
        print(f"### Детали по CL {cl_num}")
        try:
            cmd = ["python3", TRIAGE_SCRIPT, cl_num]
            out = subprocess.check_output(cmd, text=True, timeout=30)
            print(out)
        except Exception as e:
            print(f"Ошибка получения деталей для CL {cl_num}: {e}")
        print("\n---\n")

if __name__ == "__main__":
    main()
