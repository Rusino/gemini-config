#!/usr/bin/env python3
"""Phase 2 SkParagraph discovery adapter: oracle-free domain invariants + issue/TODO mapper"""
import argparse
import collections
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_wp_diff as wp_diff  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_DIR / "data"


def skp_metamorphic_invariants(r, case, tol=0.51):
    """Evaluate oracle-free invariants from text_domain_invariants.md on one SkParagraph output"""
    out = list(wp_diff.paragraph_invariants(r, case, tol))
    errs = r.get("errors", {})
    for phase, msg in errs.items():
        out.append(("exc_skp", 5, f"{phase}: {msg[:300]}"))
    if "build" in errs or "layout" in errs:
        return out

    # Inverted character box check: left must never exceed right
    cu_boxes = r.get("cuBoxes")
    lines = r.get("lines") or []
    if cu_boxes and lines:
        for idx, boxes in enumerate(cu_boxes):
            if not boxes:
                continue
            b = boxes[0]
            if b[0] > b[2] + 0.01:
                out.append(("inv_inverted_box", 3, f"char {idx} box {b}"))
                break
    return out


def cmd_check_invariants(args):
    """Run oracle-free domain invariant checks over a cases.json + results.json pair"""
    cases = json.loads(Path(args.cases).read_text())
    raw = json.loads(Path(args.results).read_text())
    side_res = raw.get(args.side, raw)
    groups = collections.OrderedDict()
    for c in cases:
        r = side_res.get(c["id"])
        if r is None:
            continue
        tol = wp_diff.TOL.get(c.get("mode", "test"), 0.51)
        for kind, sev, detail in skp_metamorphic_invariants(r, c, tol):
            sig = f"{kind}|{c.get('mode', 'test')}"
            g = groups.setdefault(sig, {"sig": sig, "kind": kind, "sev": sev, "cases": []})
            g["cases"].append({"id": c["id"], "detail": detail})

    if args.out_groups:
        Path(args.out_groups).write_text(json.dumps(groups, indent=2) + "\n")
    for sig, g in sorted(groups.items(), key=lambda kv: (-kv[1]["sev"], kv[0])):
        ex = g["cases"][0]
        print(f"S{g['sev']} n={len(g['cases']):4d} {sig:40s} e.g. {ex['id']} {ex['detail'][:100]}")


def cmd_scan_skia_todos(args):
    """Scan modules/skparagraph/ in a Skia or Flutter checkout for TODO/SkASSERT bug markers"""
    root = Path(args.skparagraph_dir)
    features = json.loads((DATA_DIR / "features.json").read_text())["features"]
    cpp_patterns = [
        (f["id"], re.compile(f["cpp_regex"])) for f in features if f.get("cpp_regex")
    ]
    todo_re = re.compile(r"//\s*(TODO|FIXME|BUG)\b(.*)$")
    items = []
    for p in sorted(root.rglob("*")):
        if p.suffix not in (".cpp", ".cc", ".h") or "tests" in p.parts:
            continue
        lines = p.read_text(errors="replace").splitlines()
        for lineno, line in enumerate(lines, 1):
            m = todo_re.search(line)
            if not m:
                continue
            window = "\n".join(lines[max(0, lineno - 15) : min(len(lines), lineno + 15)])
            matched_fids = [fid for fid, rx in cpp_patterns if rx.search(window)]
            if not matched_fids:
                matched_fids = ["F01"]
            rel = p.relative_to(root)
            items.append({
                "id": f"TODO-{rel.stem}-{lineno}",
                "what": f"{rel}:{lineno}: {m.group(0).strip()}",
                "features": matched_fids,
                "reach_feature": matched_fids[0],
                "S": 2,
                "P": 0.3,
                "known": f"{rel}:{lineno}",
            })
    if args.out:
        Path(args.out).write_text(json.dumps(items, indent=2) + "\n")
    print(f"Scanned {root}: found {len(items)} TODO/FIXME/BUG sites mapped to F01..F30")


def cmd_fetch_gh_issues(args):
    """Fetch open flutter/flutter text/typography issues (non-web-only) and map to F01..F30"""
    features = json.loads((DATA_DIR / "features.json").read_text())["features"]
    dart_patterns = [
        (f["id"], re.compile(f["regex"])) for f in features if f.get("regex")
    ]
    cmd = [
        "gh",
        "issue",
        "list",
        "--repo",
        "flutter/flutter",
        "--state",
        "open",
        "--label",
        args.label,
        "--limit",
        str(args.limit),
        "--json",
        "number,title,labels,body",
    ]
    raw = json.loads(subprocess.check_output(cmd, text=True))
    out = []
    for iss in raw:
        lbls = {l["name"] for l in iss.get("labels", [])}
        non_web = {
            "platform-android",
            "platform-ios",
            "platform-macos",
            "platform-windows",
            "platform-linux",
        }
        if args.exclude_web_only and "platform-web" in lbls and not (lbls & non_web):
            continue
        blob = f"{iss.get('title', '')}\n{iss.get('body', '')}"
        fids = [fid for fid, rx in dart_patterns if rx.search(blob)] or ["F01"]
        is_crash = any(w in blob.lower() for w in ("crash", "segfault", "sigabrt", "exception"))
        out.append({
            "id": f"GH-{iss['number']}",
            "what": iss["title"][:120],
            "features": fids,
            "reach_feature": fids[0],
            "S": 5 if is_crash else 3,
            "severity_debug": 5 if is_crash else 3,
            "severity_release": 5 if is_crash else 3,
            "release_verified": False,
            "P": 0.3,
            "known": f"#{iss['number']}",
        })
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=2) + "\n")
    print(f"Fetched {len(out)} open flutter/flutter issues for label '{args.label}'")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_inv = sub.add_parser("check-invariants", help="Check oracle-free paragraph invariants")
    p_inv.add_argument("--cases", required=True)
    p_inv.add_argument("--results", required=True)
    p_inv.add_argument("--side", default="skp")
    p_inv.add_argument("--out-groups")
    p_inv.set_defaults(func=cmd_check_invariants)

    p_todo = sub.add_parser("scan-todos", help="Scan modules/skparagraph C++ sources for TODOs")
    p_todo.add_argument("--skparagraph-dir", required=True)
    p_todo.add_argument("--out")
    p_todo.set_defaults(func=cmd_scan_skia_todos)

    p_gh = sub.add_parser("fetch-issues", help="Fetch & map open flutter/flutter text issues")
    p_gh.add_argument("--label", default="a: typography")
    p_gh.add_argument("--limit", type=int, default=100)
    p_gh.add_argument(
        "--exclude-web-only",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    p_gh.add_argument("--out")
    p_gh.set_defaults(func=cmd_fetch_gh_issues)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
