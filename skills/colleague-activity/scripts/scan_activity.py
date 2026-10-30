#!/usr/bin/env python3
"""Unified developer activity scanner across Skia Gerrit and Flutter GitHub."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import re
import subprocess
import sys
import urllib.request
from zoneinfo import ZoneInfo

BOT_MARKERS = (
    "gserviceaccount", "autoroll", "copybara", "skia-recreate", "[bot]",
    "exitgate", "fluttergithubbot", "engine-flutter-autoroll", "dependabot",
    "flutter-dashboard", "auto-submit", "roller",
)


def is_bot(name: str) -> bool:
    low = name.lower()
    return any(marker in low for marker in BOT_MARKERS)


def parse_utc_timestamp(val: str) -> datetime:
    cleaned = val.replace("T", " ").replace("Z", "")[:19]
    return datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def format_et(dt: datetime, tz: ZoneInfo) -> str:
    local_dt = dt.astimezone(tz)
    return local_dt.strftime("%H:%M ET")


def format_relative(dt: datetime, now_utc: datetime) -> str:
    diff_sec = max(0, int((now_utc - dt).total_seconds()))
    if diff_sec < 60:
        return "just now"
    diff_min = diff_sec // 60
    if diff_min < 60:
        return f"{diff_min}m ago"
    diff_hours = diff_min // 60
    rem_min = diff_min % 60
    return f"{diff_hours}h {rem_min}m ago" if rem_min else f"{diff_hours}h ago"


def fetch_gerrit_json(url: str) -> list:
    try:
        raw = subprocess.check_output(["gob-curl", "-s", url]).decode("utf-8")
    except Exception:
        req = urllib.request.Request(url, headers={"User-Agent": "colleague-activity-scan"})
        raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
    if raw.startswith(")]}'"):
        raw = raw[4:]
    return json.loads(raw)


def fetch_github_json(path: str, paginate: bool = True) -> list:
    cmd = ["gh", "api"] + (["--paginate"] if paginate else []) + [path]
    raw = subprocess.check_output(cmd).decode("utf-8")
    decoder = json.JSONDecoder()
    items = []
    idx = 0
    while idx < len(raw):
        while idx < len(raw) and raw[idx].isspace():
            idx += 1
        if idx >= len(raw):
            break
        obj, end_idx = decoder.raw_decode(raw, idx)
        items.extend(obj if isinstance(obj, list) else [obj])
        idx = end_idx
    return items


def scan_skia_gerrit(hours: int, now_utc: datetime) -> list:
    cutoff = now_utc - timedelta(hours=hours)
    url = (
        f"https://skia-review.googlesource.com/a/changes/?q=-age:{hours}h&n=150"
        "&o=DETAILED_ACCOUNTS&o=MESSAGES&o=ALL_REVISIONS"
    )
    events = []
    for change in fetch_gerrit_json(url):
        subj = change.get("subject", "")[:55]
        num = change.get("_number")
        proj = change.get("project", "")
        wip = " [WIP]" if change.get("work_in_progress") else ""
        for msg in change.get("messages", []):
            author_info = msg.get("author", {})
            author = author_info.get("email", author_info.get("name", "bot"))
            if is_bot(author):
                continue
            ts = parse_utc_timestamp(msg.get("date", ""))
            if ts < cutoff:
                continue
            text = msg.get("message", "").replace("\n", " ")[:60]
            events.append({
                "ts": ts,
                "actor": author,
                "repo": f"skia/{proj}" if proj else "skia",
                "kind": "review",
                "detail": f"{proj} {num}{wip} ({subj}): {text}",
                "url": f"https://skia-review.googlesource.com/c/{proj}/+/{num}",
            })
        for rinfo in change.get("revisions", {}).values():
            uploader = rinfo.get("uploader", {}).get("email", "bot")
            if is_bot(uploader):
                continue
            ts = parse_utc_timestamp(rinfo.get("created", ""))
            if ts < cutoff:
                continue
            ps_num = rinfo.get("_number")
            events.append({
                "ts": ts,
                "actor": uploader,
                "repo": f"skia/{proj}" if proj else "skia",
                "kind": "upload",
                "detail": f"PS{ps_num} -> {proj} {num}{wip}: {subj}",
                "url": f"https://skia-review.googlesource.com/c/{proj}/+/{num}",
            })
    return events


def scan_flutter_github(repo: str, hours: int, now_utc: datetime) -> list:
    cutoff = now_utc - timedelta(hours=hours)
    since_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    events = []

    # 1. Issues & PR comments
    try:
        path = (
            f"repos/{repo}/issues/comments?sort=updated&direction=desc"
            f"&since={since_iso}&per_page=100"
        )
        for comment in fetch_github_json(path, paginate=True):
            user = comment.get("user", {}).get("login", "")
            if is_bot(user):
                continue
            ts = parse_utc_timestamp(comment.get("created_at", ""))
            if ts < cutoff:
                continue
            issue_url = comment.get("issue_url", "")
            issue_num = issue_url.rsplit("/", 1)[-1]
            body = comment.get("body", "").replace("\n", " ")[:55]
            events.append({
                "ts": ts,
                "actor": user,
                "repo": repo,
                "kind": "comment",
                "detail": f"comment #{issue_num}: {body}",
                "url": f"https://github.com/{repo}/issues/{issue_num}",
            })
    except Exception:
        pass

    # 2. PR Review comments
    try:
        path = (
            f"repos/{repo}/pulls/comments?sort=updated&direction=desc"
            f"&since={since_iso}&per_page=100"
        )
        for comment in fetch_github_json(path, paginate=True):
            user = comment.get("user", {}).get("login", "")
            if is_bot(user):
                continue
            ts = parse_utc_timestamp(comment.get("created_at", ""))
            if ts < cutoff:
                continue
            pr_url = comment.get("pull_request_url", "")
            pr_num = pr_url.rsplit("/", 1)[-1]
            body = comment.get("body", "").replace("\n", " ")[:55]
            events.append({
                "ts": ts,
                "actor": user,
                "repo": repo,
                "kind": "pr_review",
                "detail": f"review PR #{pr_num}: {body}",
                "url": f"https://github.com/{repo}/pull/{pr_num}",
            })
    except Exception:
        pass

    # 3. Pull request activity (author pushes, reviews, merges)
    try:
        prs = []
        for page in (1, 2, 3):
            url = (
                f"repos/{repo}/pulls?state=all&sort=updated&direction=desc"
                f"&per_page=100&page={page}"
            )
            sub = fetch_github_json(url, paginate=False)
            prs.extend(sub)
            if not sub or parse_utc_timestamp(sub[-1]["updated_at"]) < cutoff:
                break
        for pr in prs:
            user = pr.get("user", {}).get("login", "")
            if is_bot(user):
                continue
            ts = parse_utc_timestamp(pr.get("updated_at", ""))
            if ts < cutoff:
                continue
            num = pr.get("number")
            state = pr.get("state", "")
            draft = " draft" if pr.get("draft") else ""
            title = pr.get("title", "")[:55]
            events.append({
                "ts": ts,
                "actor": user,
                "repo": repo,
                "kind": "pr_update",
                "detail": f"PR #{num} [{state}{draft}]: {title}",
                "url": f"https://github.com/{repo}/pull/{num}",
            })
    except Exception:
        pass

    # 4. Merged commits
    try:
        path = f"repos/{repo}/commits?since={since_iso}&per_page=100"
        for commit_obj in fetch_github_json(path, paginate=True):
            author_info = commit_obj.get("author") or {}
            actor = (
                author_info.get("login")
                or commit_obj.get("commit", {}).get("author", {}).get("email", "")
            )
            if is_bot(actor):
                continue
            c_date = commit_obj.get("commit", {}).get("committer", {}).get("date", "")
            ts = parse_utc_timestamp(c_date)
            if ts < cutoff:
                continue
            msg = commit_obj.get("commit", {}).get("message", "").splitlines()[0][:60]
            events.append({
                "ts": ts,
                "actor": actor,
                "repo": repo,
                "kind": "commit",
                "detail": f"merged: {msg}",
                "url": commit_obj.get("html_url", ""),
            })
    except Exception:
        pass

    return events


def categorize_actor(actor: str, details_sample: str) -> str:
    actor_low = actor.lower()
    sample_low = details_sample.lower()
    text_keywords = ("paragraph", "text", "bidi", "font", "shaping", "canvas", "web", "unicode")
    if any(k in sample_low for k in text_keywords) or actor_low in (
        "dbebawy", "mdebbar", "harryterkelsen", "wselna123", "yeungkc",
        "longcatislooong", "jlavrova"
    ):
        return "Text / Web / Paragraph"
    infra_keywords = ("gold", "buildbot", "k8s", "infra", "autoroll", "roller", "pinpoint", "cbb")
    if any(k in sample_low for k in infra_keywords) or actor_low in (
        "smolkaj", "borenet", "kentnguyen99", "davidsegovia", "gmarcoesau"
    ):
        return "Infra / Tooling CI"
    tool_keywords = ("tool", "flutter_tools", "a11y", "ios", "android", "mac", "cmake", "gradle")
    if any(k in sample_low for k in tool_keywords) or actor_low in (
        "bkonyi", "loic-sharma", "jmagman", "justinmc", "vashworth", "victorsanni", "renzo-olivares"
    ):
        return "Framework & Tooling"
    return "Core Graphics & Engine"


def run_scan(hours: int, tz_name: str, skia_only: bool, flutter_only: bool) -> dict:
    now_utc = datetime.now(timezone.utc)
    tz = ZoneInfo(tz_name)
    all_events = []

    tasks = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        if not flutter_only:
            tasks.append(pool.submit(scan_skia_gerrit, hours, now_utc))
        if not skia_only:
            tasks.append(pool.submit(scan_flutter_github, "flutter/flutter", hours, now_utc))
            tasks.append(pool.submit(scan_flutter_github, "flutter/packages", hours, now_utc))
        for task in tasks:
            try:
                all_events.extend(task.result())
            except Exception as exc:
                print(f"Warning: subtask failed: {exc}", file=sys.stderr)

    # Aggregate by actor
    actors = {}
    for ev in all_events:
        act = ev["actor"]
        data = actors.setdefault(act, {
            "first_ts": ev["ts"],
            "last_ts": ev["ts"],
            "events_count": 0,
            "samples": [],
            "repos": set(),
        })
        data["events_count"] += 1
        data["first_ts"] = min(data["first_ts"], ev["ts"])
        data["last_ts"] = max(data["last_ts"], ev["ts"])
        data["repos"].add(ev["repo"])
        if len(data["samples"]) < 5:
            data["samples"].append(ev)

    sorted_actors = sorted(actors.items(), key=lambda kv: kv[1]["last_ts"], reverse=True)
    recent_threshold = now_utc - timedelta(minutes=45)

    active_now = []
    recent_work = []
    for actor, info in sorted_actors:
        item = {
            "actor": actor,
            "last_ts": info["last_ts"],
            "first_ts": info["first_ts"],
            "formatted_time": format_et(info["last_ts"], tz),
            "relative": format_relative(info["last_ts"], now_utc),
            "count": info["events_count"],
            "repos": list(info["repos"]),
            "samples": [s["detail"] for s in info["samples"]],
            "category": categorize_actor(actor, " ".join(s["detail"] for s in info["samples"])),
        }
        if info["last_ts"] >= recent_threshold:
            active_now.append(item)
        else:
            recent_work.append(item)

    return {
        "now_utc": now_utc.isoformat(),
        "now_formatted": format_et(now_utc, tz),
        "tz": tz_name,
        "active_now": active_now,
        "recent_work": recent_work,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Scan active colleagues across Skia and Flutter"
    )
    parser.add_argument(
        "--hours", type=int, default=12, help="Lookback window in hours (default 12)"
    )
    parser.add_argument(
        "--tz", type=str, default="America/New_York",
        help="Timezone for output (default America/New_York)"
    )
    parser.add_argument("--skia-only", action="store_true", help="Scan only Skia Gerrit")
    parser.add_argument("--flutter-only", action="store_true", help="Scan only Flutter GitHub")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    result = run_scan(args.hours, args.tz, args.skia_only, args.flutter_only)
    if args.json:
        # Convert datetime objects to string
        for group in (result["active_now"], result["recent_work"]):
            for item in group:
                item["last_ts"] = item["last_ts"].isoformat()
                item["first_ts"] = item["first_ts"].isoformat()
        print(json.dumps(result, indent=2))
        return

    print(f"\nDeveloper Activity Report — {result['now_formatted']} (Last {args.hours}h)")
    print("=" * 80)

    print(f"\n### Active Right Now (Last 45 minutes, {len(result['active_now'])} people)\n")
    if not result["active_now"]:
        print("  (No activity detected in the last 45 minutes)")
    for item in result["active_now"]:
        header = f"* {item['actor']} — {item['formatted_time']} ({item['relative']})"
        print(f"{header} [{item['category']}]")
        for sample in item["samples"][:3]:
            print(f"    - {sample}")

    # Group earlier activity by category
    categories = {}
    for item in result["recent_work"]:
        categories.setdefault(item["category"], []).append(item)

    print(f"\n### Earlier Today ({len(result['recent_work'])} people)\n")
    for cat, members in sorted(categories.items()):
        print(f"\n#### {cat} ({len(members)} people)")
        for item in members:
            print(f"* {item['actor']} (until {item['formatted_time']}, {item['relative']})")
            if item["samples"]:
                print(f"    - {item['samples'][0]}")


if __name__ == "__main__":
    main()
