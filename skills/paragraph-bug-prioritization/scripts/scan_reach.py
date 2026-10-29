#!/usr/bin/env python3
"""Ecosystem feature reach and Phase 3 compound-reach zoom scanner for WebParagraph & SkParagraph"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import time

SKILL_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_DIR / "data"
DEFAULT_WP_PRIO = Path.home() / ".gemini/jetski/scratch/wp_prioritization"

TEST_RE = re.compile(
    r"(_test\.dart$|_test\.cc$|_unittest\.cc$|/test/|/testing/|/integration_test/|"
    r"/test_driver/|/goldens?/|/test_utils?/)"
)
FONT_EXT = r"\.(ttf|otf|woff2?|TTF|OTF)$"
KNOWN_WOFF2_TAGS = (
    "cmap head hhea hmtx maxp name OS/2 post cvt  fpgm glyf loca prep CFF  VORG EBDT EBLC "
    "gasp hdmx kern LTSH PCLT VDMX vhea vmtx BASE GDEF GPOS GSUB EBSC JSTF MATH CBDT CBLC "
    "COLR CPAL SVG  sbix acnt avar bdat bloc bsln cvar fdsc feat fmtx fvar gvar hsty just "
    "lcar mort morx opbd prop trak Zapf Silf Glat Gloc Feat Sill"
).split()


def kendall_tau_b(a, b):
    """Tau-b rank correlation between two score dicts over shared keys"""
    keys = sorted(set(a) & set(b))
    conc = disc = ties_a = ties_b = 0
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            da = a[keys[i]] - a[keys[j]]
            db = b[keys[i]] - b[keys[j]]
            if da == 0 and db == 0:
                continue
            if da == 0:
                ties_a += 1
            elif db == 0:
                ties_b += 1
            elif (da > 0) == (db > 0):
                conc += 1
            else:
                disc += 1
    denom = math.sqrt((conc + disc + ties_a) * (conc + disc + ties_b))
    return round((conc - disc) / denom, 4) if denom else None


def _b128(b, i):
    v = 0
    for _ in range(5):
        c = b[i]
        i += 1
        v = (v << 7) | (c & 0x7F)
        if not (c & 0x80):
            return v, i
    raise ValueError("invalid base128 in WOFF2 header")


def parse_font_tables(b):
    """Extract OpenType table tags from TTF/OTF/TTC/WOFF/WOFF2 bytes"""
    sig = b[:4]
    if sig in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        n = struct.unpack(">H", b[4:6])[0]
        return [b[12 + 16 * k : 16 + 16 * k].decode("latin1") for k in range(n)]
    if sig == b"ttcf":
        off = struct.unpack(">I", b[12:16])[0]
        return parse_font_tables(b[off:])
    if sig == b"wOFF":
        n = struct.unpack(">H", b[12:14])[0]
        return [b[44 + 20 * k : 48 + 20 * k].decode("latin1") for k in range(n)]
    if sig == b"wOF2":
        n = struct.unpack(">H", b[12:14])[0]
        i, out = 48, []
        for _ in range(n):
            fl = b[i]
            i += 1
            idx, ver = fl & 0x3F, (fl >> 6) & 3
            if idx == 63:
                tag = b[i : i + 4].decode("latin1")
                i += 4
            else:
                tag = KNOWN_WOFF2_TAGS[idx]
            _, i = _b128(b, i)
            if (idx in (10, 11) and ver == 0) or (idx not in (10, 11) and ver != 0):
                _, i = _b128(b, i)
            out.append(tag)
        return out
    return None


def cs_once(query, files_only=False):
    """Run one cs query and verify explicit completeness marker in output"""
    cmd = ["cs", "--max_num_results=100000"]
    if files_only:
        cmd.append("-l")
    cmd.append(query)
    p = subprocess.run(cmd, capture_output=True, text=True, cwd="/tmp")
    out = p.stdout + p.stderr
    complete = "These are all the results" in out or "No results found" in out
    if files_only:
        hits = [
            m.group(1)
            for m in (
                re.match(r"/google/src/files/head/depot/google3/(.*)$", line)
                for line in p.stdout.splitlines()
            )
            if m
        ]
    else:
        hits = []
        for line in p.stdout.splitlines():
            m = re.match(r"/google/src/files/head/depot/google3/([^:]*):(\d+):(.*)$", line)
            if m:
                hits.append((m.group(1), int(m.group(2)), m.group(3).strip()[:240]))
    return p.returncode, complete, hits


def _feature_used_in_app(aid, fid, feat_counts, apps_meta, scan_meta, with_locales=True):
    used = feat_counts.get(aid, {}).get(fid, 0) > 0
    if not with_locales:
        return used
    loc = apps_meta[aid].get("locales", {})
    extra = scan_meta.get("extra", {}).get(aid, {})
    if fid == "F19":
        used = used or bool(loc.get("rtl_locales"))
    elif fid == "F20":
        used = used or bool(loc.get("complex_locales"))
    elif fid == "F12":
        used = (
            used
            or bool(loc.get("cjk_locales"))
            or extra.get("F12_cjk_literal_files", 0) > 0
        )
    return used


def cmd_refresh_cached(args):
    """Build data/g3_apps.json, data/reach_web.json, and data/reach_skp.json from 1b and 1c"""
    src = Path(args.source_dir)
    apps_1b = json.loads((src / "1b/apps.json").read_text())
    feat_all = json.loads((src / "1b/features_by_app_all.json").read_text())
    scan_meta = json.loads((src / "1b/features_scan_meta.json").read_text())
    report_1c = json.loads((src / "1c/report.json").read_text())
    fids = [f["id"] for f in json.loads((DATA_DIR / "features.json").read_text())["features"]]

    # Enrich g3_apps with skp_in_reach: all live Flutter apps (including mobile-only GPay)
    g3_apps = {}
    for aid, a in apps_1b.items():
        lw = a.get("liveness_weight") or 0
        tw = a.get("tier_weight") or 1
        web_in_reach = bool(a.get("in_reach"))
        skp_in_reach = lw > 0
        g3_apps[aid] = {
            **a,
            "web_in_reach": web_in_reach,
            "web_weight": tw * lw if web_in_reach else 0,
            "skp_in_reach": skp_in_reach,
            "skp_weight": tw * lw if skp_in_reach else 0,
        }
    (DATA_DIR / "g3_apps.json").write_text(json.dumps(g3_apps, indent=2) + "\n")

    def compute_g3_reach(weight_key, in_reach_key):
        ids = [aid for aid, a in g3_apps.items() if a[in_reach_key]]
        w_sum = sum(g3_apps[aid][weight_key] for aid in ids)
        r_loc = {}
        r_raw = {}
        for fid in fids:
            r_loc[fid] = round(
                sum(
                    g3_apps[aid][weight_key]
                    for aid in ids
                    if _feature_used_in_app(aid, fid, feat_all, g3_apps, scan_meta, True)
                )
                / w_sum,
                4,
            )
            r_raw[fid] = round(
                sum(
                    g3_apps[aid][weight_key]
                    for aid in ids
                    if _feature_used_in_app(aid, fid, feat_all, g3_apps, scan_meta, False)
                )
                / w_sum,
                4,
            )
        return r_loc, r_raw, ids, w_sum

    r_g3_web, r_g3_web_raw, web_ids, w_g3_web = compute_g3_reach("web_weight", "web_in_reach")
    r_g3_skp, r_g3_skp_raw, skp_ids, w_g3_skp = compute_g3_reach("skp_weight", "skp_in_reach")

    r_gh_web = report_1c["R_gh"]
    r_gh_all = report_1c["R_all"]
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    reach_web = {
        "profile": "webparagraph",
        "generated_at": now,
        "formula": "R = (2 * R_g3_with_locales + R_gh_web) / 3",
        "g3_apps_count": len(web_ids),
        "g3_weight_sum": w_g3_web,
        "gh_apps_count": report_1c["web_apps_weighted"]["n"],
        "gh_weight_sum": report_1c["web_apps_weighted"]["weight_sum"],
        "R": {
            fid: round((2 * r_g3_web[fid] + r_gh_web[fid]) / 3, 4)
            for fid in fids
        },
        "R_g3_with_locales": r_g3_web,
        "R_g3_dart_only": r_g3_web_raw,
        "R_gh": r_gh_web,
    }
    reach_skp = {
        "profile": "skparagraph",
        "generated_at": now,
        "formula": "R = (2 * R_g3_all_flutter_with_locales + R_gh_all_flutter) / 3",
        "g3_apps_count": len(skp_ids),
        "g3_weight_sum": w_g3_skp,
        "gh_apps_count": report_1c["all_apps_weighted"]["n"],
        "gh_weight_sum": report_1c["all_apps_weighted"]["weight_sum"],
        "R": {
            fid: round((2 * r_g3_skp[fid] + r_gh_all[fid]) / 3, 4)
            for fid in fids
        },
        "R_g3_with_locales": r_g3_skp,
        "R_g3_dart_only": r_g3_skp_raw,
        "R_gh": r_gh_all,
    }
    (DATA_DIR / "reach_web.json").write_text(json.dumps(reach_web, indent=2) + "\n")
    (DATA_DIR / "reach_skp.json").write_text(json.dumps(reach_skp, indent=2) + "\n")
    tau = kendall_tau_b(reach_web["R"], reach_skp["R"])
    print(
        f"Refreshed cached reach profiles at {now}:\n"
        f"  webparagraph: {len(web_ids)} g3 web apps (w={w_g3_web}) + "
        f"{reach_web['gh_apps_count']} GH web apps (w={reach_web['gh_weight_sum']})\n"
        f"  skparagraph : {len(skp_ids)} g3 Flutter apps incl. mobile (w={w_g3_skp}) + "
        f"{reach_skp['gh_apps_count']} GH Flutter apps (w={reach_skp['gh_weight_sum']})\n"
        f"  Kendall tau-b (reach_web vs reach_skp): {tau}"
    )


def cmd_zoom(args):
    """Phase 3 Compound Reach Zoom-In: measure exact app reach for custom regexes & font tables"""
    g3_apps = json.loads((DATA_DIR / "g3_apps.json").read_text())
    in_reach_key = "web_in_reach" if args.profile == "web" else "skp_in_reach"
    weight_key = "web_weight" if args.profile == "web" else "skp_weight"
    active_apps = {aid: a for aid, a in g3_apps.items() if a.get(in_reach_key)}

    queries = {}
    for qspec in args.query:
        if "=" not in qspec:
            raise SystemExit(f"--query must be NAME=REGEX, got: {qspec}")
        name, rx = qspec.split("=", 1)
        queries[name] = rx

    # Always include CTRL liveness probe so silent cs backend drops are caught and retried
    queries["CTRL"] = r"^(import|part|library|export) "
    if args.check_ttf_fvar:
        queries["FONT_FILES"] = FONT_EXT

    def run_one(item):
        aid, app, qid, rx = item
        root = app["root"]
        if qid == "FONT_FILES":
            q = f"file:^//depot/google3/{root}/.*{rx}"
            files_only = True
        else:
            q = f"case:yes file:^//depot/google3/{root}/{args.file_glob} {rx}"
            files_only = False
        for attempt in range(4):
            rc, complete, hits = cs_once(q, files_only=files_only)
            if rc == 0 and complete:
                break
            time.sleep(2 * (attempt + 1))
        excl = app.get("excluded_subdirs") or []
        if files_only:
            filtered = [p for p in hits if not any(p.startswith(e) for e in excl)]
        else:
            filtered = [
                h
                for h in hits
                if not any(h[0].startswith(e) for e in excl) and not TEST_RE.search(h[0])
            ]
        return aid, qid, rc == 0 and complete, filtered

    jobs = [
        (aid, app, qid, rx)
        for aid, app in active_apps.items()
        for qid, rx in queries.items()
    ]
    with ThreadPoolExecutor(4) as ex:
        rows = list(ex.map(run_one, jobs))

    results = {}
    for aid, qid, ok, hits in rows:
        results.setdefault(aid, {})[qid] = {"complete": ok, "hits": hits}

    # Sequential retry if any query is incomplete or CTRL == 0 on a root with >= 50 Dart files
    for _ in range(3):
        bad = [
            (aid, qid)
            for aid in results
            for qid in queries
            if not results[aid][qid]["complete"]
            or (
                qid == "CTRL"
                and not results[aid]["CTRL"]["hits"]
                and (active_apps[aid].get("dart_files") or 0) >= 50
            )
        ]
        if not bad:
            break
        for aid, qid in bad:
            _, _, ok, hits = run_one((aid, active_apps[aid], qid, queries[qid]))
            results[aid][qid] = {"complete": ok, "hits": hits}
            time.sleep(0.3)

    fvar_by_app = {}
    if args.check_ttf_fvar:
        for aid, d in results.items():
            fvar_files = []
            for rel_path in d.get("FONT_FILES", {}).get("hits", []):
                full_p = Path("/google/src/head/depot/google3") / rel_path
                try:
                    tags = parse_font_tables(full_p.read_bytes())
                    if tags and "fvar" in tags:
                        fvar_files.append(rel_path)
                except (OSError, ValueError, IndexError, struct.error):
                    continue
            fvar_by_app[aid] = fvar_files

    w_total = sum(a[weight_key] for a in active_apps.values())
    user_qids = [q for q in queries if q not in ("CTRL", "FONT_FILES")]
    summary = {
        "profile": args.profile,
        "g3_weight_sum": w_total,
        "queries": {q: queries[q] for q in user_qids},
        "upper_bound_reach_g3": {},
        "apps": {},
    }
    for qid in user_qids:
        w_hit = sum(
            active_apps[aid][weight_key]
            for aid in active_apps
            if results[aid][qid]["hits"]
        )
        summary["upper_bound_reach_g3"][qid] = round(w_hit / w_total, 4) if w_total else 0.0

    # Compound intersection upper bound: app matches ALL user queries (and has fvar if requested)
    def app_matches_all(aid):
        if not all(results[aid][qid]["hits"] for qid in user_qids):
            return False
        if args.check_ttf_fvar and not fvar_by_app.get(aid):
            return False
        return True

    w_all = sum(active_apps[aid][weight_key] for aid in active_apps if app_matches_all(aid))
    summary["upper_bound_reach_g3"]["ALL_INTERSECT"] = (
        round(w_all / w_total, 4) if w_total else 0.0
    )

    for aid, app in sorted(active_apps.items()):
        app_hits = {
            qid: {
                "file_count": len({h[0] for h in results[aid][qid]["hits"]}),
                "sample_hits": results[aid][qid]["hits"][:5],
                "complete": results[aid][qid]["complete"],
            }
            for qid in user_qids
        }
        if any(v["file_count"] > 0 for v in app_hits.values()) or fvar_by_app.get(aid):
            summary["apps"][aid] = {
                "tier": app.get("tier"),
                "weight": app[weight_key],
                "matches_all": app_matches_all(aid),
                "fvar_fonts": fvar_by_app.get(aid, []),
                "queries": app_hits,
            }

    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary["upper_bound_reach_g3"], indent=2))
    print(f"Matched apps ({len(summary['apps'])}): {sorted(summary['apps'].keys())}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ref = sub.add_parser("refresh-cached", help="Rebuild reach_web.json and reach_skp.json")
    p_ref.add_argument("--source-dir", default=str(DEFAULT_WP_PRIO))
    p_ref.set_defaults(func=cmd_refresh_cached)

    p_zoom = sub.add_parser("zoom", help="Phase 3 compound-reach micro-scan in google3")
    p_zoom.add_argument("--profile", choices=("web", "skp"), default="web")
    p_zoom.add_argument("--query", action="append", required=True, help="NAME=REGEX (repeatable)")
    p_zoom.add_argument("--file-glob", default=r".*\.dart$")
    p_zoom.add_argument("--check-ttf-fvar", action="store_true")
    p_zoom.add_argument("--out", help="Optional JSON output path")
    p_zoom.set_defaults(func=cmd_zoom)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
