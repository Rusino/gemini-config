#!/usr/bin/env python3
"""Check new Google Alerts mail for real mentions of "Start Doing Graph Theory".

Reads processed alert IDs from state.json next to this script, lists recent alert emails in the
work Gmail via the gmail CLI, extracts direct target URLs from google.com/url redirects, fetches
each page and checks whether it mentions the book. Prints a JSON report to stdout:
{"new_alerts": [{"id", "items": [{"url", "title", "mentions_book", "evidence"}]}]}.
With --mark, adds the reported alert IDs to state.json (side effect); without it, read-only.
"""
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

GMAIL = "/google/bin/releases/gemini-agents-gmail/gmail"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
# Book-specific markers; plain "graph theory" or a bare "Lavrov" (common surname) is a false
# positive
MARKERS = re.compile(r"Start Doing Graph Theory|vertex\.degree|(?:Mikhail|Misha) Lavrov", re.I)


def gmail(*args):
    """Run a read-only gmail CLI command and return stdout."""
    return subprocess.run([GMAIL, "readonly", *args], capture_output=True, text=True,
                          timeout=60).stdout


def alert_items(body):
    """Return (title, direct_url) pairs from an alert email body."""
    items = []
    title_lines = []
    for line in body.splitlines():
        line = line.strip()
        m = re.match(r"<(https://www\.google\.com/url\?[^>]+)>", line)
        if m:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(m.group(1)).query)
            if "url" in q:
                items.append((" ".join(title_lines[:2]), q["url"][0]))
            title_lines = []
        elif line and not line.startswith("==="):
            title_lines.append(line)
        else:
            title_lines = []
    return items


def check(url):
    """Fetch url and return (mentions_book, evidence snippet or error)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        text = urllib.request.urlopen(req, timeout=20).read().decode("utf8", "ignore")
    except Exception as e:  # network errors are reported, not fatal
        return None, "fetch failed: %s" % e
    m = MARKERS.search(text)
    if not m:
        return False, ""
    return True, re.sub(r"\s+", " ", text[max(0, m.start() - 100):m.end() + 100])


def main():
    state = json.load(open(STATE))
    seen = set(state.get("processed_alert_ids", []))
    listing = gmail("search", "from:googlealerts-noreply@google.com newer_than:3d", "--max", "10",
                    "--json")
    ids = [x["id"] for x in json.loads(listing or "[]")]
    report = []
    for mid in ids:
        if mid in seen:
            continue
        entries = []
        for title, url in alert_items(gmail("get", mid)):
            ok, ev = check(url)
            entries.append({"url": url, "title": title, "mentions_book": ok, "evidence": ev})
        report.append({"id": mid, "items": entries})
    if "--mark" in sys.argv:
        state["processed_alert_ids"] = sorted(seen | {r["id"] for r in report})
        json.dump(state, open(STATE, "w"))
    print(json.dumps({"new_alerts": report}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
