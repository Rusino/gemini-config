#!/usr/bin/env python3
"""Phase 2 WebParagraph discovery adapter: hierarchical WP vs CanvasKit SkP diff + felt runner"""
import argparse
import base64
import collections
import json
import os
from pathlib import Path
import re
import subprocess
import time

SKILL_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_DIR / "data"

# Test font (Ahem) is exact up to rounding; real fonts get sub-pixel tolerance
TOL = {"test": 0.51, "real": 1.01}
AHEM_OK = re.compile(r"^[\u0000-\u00FF\u2026\u200B-\u200F\u2028\u2029\uFFFC\u00AD]*$")

_SKIP_FRAMES = re.compile(
    r"wrapException|throwExpression|ioore|assertThrow|initializeExceptionWrapper|"
    r"throw_|_err|runCase|Object\.|Closure|_rootRun|zz_harness|"
    r"iae|diagnoseIndexError|diagnoseRangeError|ArgumentError|RangeError|StateError|"
    r"\$throw|throwConcurrent|checkNotNullable|nullCheck|S\.\w+ \("
)

SUITES = {
    "wp": ("chrome-dart2js-webparagraph-ui", "test/webparagraph/zz_diff_test.dart"),
    "skp": ("chrome-dart2js-canvaskit-canvaskit", "test/canvaskit/zz_diff_test.dart"),
}


def _close(a, b, tol):
    # Both sides report "no lines" as a huge negative longestLine with different constants
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a < -1e5 and b < -1e5:
        return True
    if a is None or b is None:
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tol
    return a == b


def err_signature(msg):
    """Exception type + message skeleton + first engine frame, numbers stripped"""
    head, _, stack = msg.partition(" @@ ")
    typ = head.split(":")[0]
    skel = re.sub(r"\d+(\.\d+)?", "N", head)[:120]
    site = ""
    for f in (x.strip() for x in stack.split(" | ")):
        m = re.search(r"at ([\w$.<>]+)", f)
        name = m.group(1) if m else f[:60]
        if _SKIP_FRAMES.search(name) or name in ("Object", "new"):
            continue
        site = name
        break
    return f"{typ}|{skel}|{site}"


def _intervals_from_line_b(line_b):
    return sorted({tuple(r) for r in line_b if r is not None})


def paragraph_invariants(r, case, tol):
    """Domain invariants applied to a single Paragraph result dict"""
    out = []
    n = r.get("n")
    if not n:
        return out
    line_b = r.get("lineB")
    no_trunc = (
        case.get("p", {}).get("maxLines") is None
        and case.get("p", {}).get("ellipsis") is None
    )
    if line_b and no_trunc:
        iv = [x for x in _intervals_from_line_b(line_b) if x != (-1, -1)]
        # Line ranges must be monotonic and non-overlapping (allowing 1-code-unit hard-break gaps)
        prev_end = 0
        ok = True
        for a, b in iv:
            if a < 0 or b > n or a > b or a < prev_end or a - prev_end > 1:
                ok = False
            prev_end = max(prev_end, b)
        if not ok:
            out.append(("inv_line_tiling", 3, f"lineB intervals {iv[:8]} n={n}"))
        for i, rr in enumerate(line_b):
            if rr is not None and rr != [-1, -1] and not (rr[0] <= i <= rr[1]):
                out.append(("inv_lineB_contains", 3, f"offset {i} -> {rr}"))
                break
    word_b = r.get("wordB")
    if word_b:
        for i, rr in enumerate(word_b):
            if rr is not None and not (rr[0] <= i <= rr[1] and 0 <= rr[0] and rr[1] <= n):
                out.append(("inv_wordB_contains", 3, f"offset {i} -> {rr}"))
                break
    w, h = r.get("width"), r.get("height")
    if isinstance(w, (int, float)) and isinstance(h, (int, float)) and w < 900000:
        for b in r.get("allBoxes") or []:
            if b[0] < -tol - 1 or b[2] > w + tol + 1:
                out.append(("inv_box_outside_x", 2, f"allBoxes box {b} width {w}"))
                break
            if b[1] < -tol - 1 or b[3] > h + tol + 1:
                out.append(("inv_box_outside_y", 2, f"allBoxes box {b} height {h}"))
                break
    rf = r.get("reflow")
    if rf:
        lines_inf, lines_max, _, _, ll_min, max_iw, min_iw = rf
        if lines_max > lines_inf:
            out.append((
                "inv_wrap_at_maxIW",
                4,
                f"lines unconstrained={lines_inf} at maxIW({max_iw})={lines_max}",
            ))
        if ll_min > min_iw + tol and case.get("p", {}).get("maxLines") is None:
            out.append((
                "inv_overflow_at_minIW",
                4,
                f"longestLine {ll_min} > minIW {min_iw} at width minIW",
            ))
    lines = r.get("lines")
    if lines is not None and r.get("numLines") is not None and len(lines) != r["numLines"]:
        out.append((
            "inv_numLines",
            2,
            f"numberOfLines {r['numLines']} vs computeLineMetrics {len(lines)}",
        ))
    return out


def _merge_boxes(bs):
    bs = sorted(bs, key=lambda b: (round(b[1]), round(b[3]), b[0]))
    out = []
    for b in bs:
        if (
            out
            and abs(out[-1][1] - b[1]) < 0.5
            and abs(out[-1][3] - b[3]) < 0.5
            and abs(out[-1][2] - b[0]) < 0.5
        ):
            out[-1] = [out[-1][0], out[-1][1], b[2], out[-1][3], out[-1][4]]
        else:
            out.append(list(b))
    return sorted(out)


def _boxes_close(a, b, tol):
    if a is None or b is None:
        return a == b
    a, b = sorted(a), sorted(b)
    if len(a) != len(b):
        # Same union is valid: merge adjacent boxes on same row before comparing
        a, b = _merge_boxes(a), _merge_boxes(b)
        if len(a) != len(b):
            return False
    return all(abs(x[i] - y[i]) <= tol for x, y in zip(a, b) for i in range(4))


def _proj(bs, ych):
    if bs is None:
        return None
    return [b if ych else [b[0], 0, b[2], 0, b[4]] for b in bs]


def case_text(case):
    return "".join(
        o[1] if o[0] == "t" else "\uFFFC"
        for o in case.get("ops", [])
        if o[0] in ("t", "ph")
    )


def compare_wp_vs_skp(case, wp, skp):
    """Hierarchical diff: exceptions -> invariants -> line breaking -> metrics -> queries"""
    mode = case.get("mode", "test")
    tol = TOL[mode]
    text = case_text(case)
    fallback = mode == "test" and not AHEM_OK.match(text)
    f = []
    if wp is None or skp is None:
        return [("missing", 0, f"wp={wp is not None} skp={skp is not None}", None, None)]
    we, se = wp.get("errors", {}), skp.get("errors", {})
    for k, msg in we.items():
        if k not in se:
            f.append(("exc_wp", 5, f"{k}: {msg[:400]}", err_signature(msg), None))
    if "build" in we or "layout" in we:
        return f
    if "build" in se or "layout" in se:
        return [("exc_skp_only", 0, str(se)[:300], None, None)]
    winv = paragraph_invariants(wp, case, tol)
    sinv = {k for k, *_ in paragraph_invariants(skp, case, tol)}
    for k, sev, detail in winv:
        if k not in sinv:
            f.append((k, sev, detail, wp.get("reflow"), skp.get("reflow")))
    if fallback:
        return f
    wl, sl = wp.get("lines") or [], skp.get("lines") or []
    if wp.get("exceed") != skp.get("exceed"):
        f.append(("didExceedMaxLines", 4, "", wp.get("exceed"), skp.get("exceed")))
    if len(wl) != len(sl):
        f.append(("linecount", 4, "", [round(l[5]) for l in wl], [round(l[5]) for l in sl]))
        return f
    wlb, slb = wp.get("lineB"), skp.get("lineB")
    if wlb is not None and slb is not None:
        wiv, siv = _intervals_from_line_b(wlb), _intervals_from_line_b(slb)
        if wiv != siv:
            f.append(("line_ranges", 3, "", wiv[:6], siv[:6]))
            return f
    for i, (a, b) in enumerate(zip(wl, sl)):
        if not _close(a[6], b[6], tol * 2):
            f.append(("lineMetrics_left", 2, f"line {i}", a[6], b[6]))
            break
    ych = mode == "test"
    if not _boxes_close(_proj(wp.get("allBoxes"), ych), _proj(skp.get("allBoxes"), ych), tol * 2):
        f.append(("boxes_all", 3, "", wp.get("allBoxes"), skp.get("allBoxes")))
    return f


def primary_feature(feats, reach_map):
    fs = [
        f
        for f in feats
        if f not in ("F01", "F11", "F23", "F24", "F26", "F25", "F30", "F17", "F16", "F27", "F12")
    ]
    if not fs:
        return "F01"
    return min(fs, key=lambda f: reach_map.get(f, 1.0))


def cmd_diff_results(args):
    """Compare cases.json and results.json and group findings by signature"""
    cases = json.loads(Path(args.cases).read_text())
    res = json.loads(Path(args.results).read_text())
    reach_map = json.loads((DATA_DIR / "reach_web.json").read_text())["R"]
    groups = collections.OrderedDict()
    for c in cases:
        wp = res.get("wp", {}).get(c["id"])
        skp = res.get("skp", {}).get(c["id"])
        for kind, sev, detail, a, b in compare_wp_vs_skp(c, wp, skp):
            sig = (
                f"exc|{a}"
                if kind == "exc_wp"
                else f"{kind}|{c.get('mode')}|{primary_feature(c.get('f', []), reach_map)}"
            )
            g = groups.setdefault(sig, {"sig": sig, "kind": kind, "sev": sev, "cases": []})
            g["cases"].append({"id": c["id"], "detail": detail, "wp": a, "skp": b})

    if args.out_groups:
        Path(args.out_groups).write_text(json.dumps(groups, ensure_ascii=False, indent=2) + "\n")
    for sig, g in sorted(groups.items(), key=lambda kv: (-kv[1]["sev"], kv[0])):
        ex = g["cases"][0]
        print(
            f"S{g['sev']} n={len(g['cases']):4d} {sig[:110]}  e.g. {ex['id']} "
            f"{str(ex['detail'])[:90]}"
        )


def cmd_run_felt(args):
    """Run felt differential harness on a cases JSON file in debug or release mode"""
    web_ui = Path(args.worktree) / "engine/src/flutter/lib/web_ui"
    res_dir = SKILL_DIR / "resources"
    (web_ui / "test/webparagraph/zz_harness.dart").write_text(
        (res_dir / "zz_harness.dart").read_text()
    )
    (web_ui / "test/webparagraph/zz_diff_test.dart").write_text(
        "import 'package:test/bootstrap/browser.dart';\n"
        "import 'zz_harness.dart';\n"
        "void main() {\n"
        "  internalBootstrapBrowserTest(() => zzMain);\n"
        "}\n"
    )
    (web_ui / "test/canvaskit/zz_diff_test.dart").write_text(
        "import 'package:test/bootstrap/browser.dart';\n"
        "import '../webparagraph/zz_harness.dart';\n"
        "void main() {\n"
        "  internalBootstrapBrowserTest(() => zzMain);\n"
        "}\n"
    )
    cases = json.loads(Path(args.cases).read_text())
    b64 = base64.b64encode(json.dumps(cases, ensure_ascii=False).encode("utf-8")).decode()
    (web_ui / "test/webparagraph/zz_cases.dart").write_text(
        "// Generated by discover_wp_diff.py, untracked scratch file\n"
        f"const String kZzCasesB64 = '{b64}';\n"
    )
    env = dict(os.environ)
    if args.release:
        env["FELT_NO_ASSERTS"] = "1"
    else:
        env.pop("FELT_NO_ASSERTS", None)
    cmd = [
        "./dev/felt",
        "test",
        "--suite",
        SUITES["wp"][0],
        "--suite",
        SUITES["skp"][0],
        SUITES["wp"][1],
        SUITES["skp"][1],
    ]
    t0 = time.time()
    p = subprocess.run(
        cmd, cwd=web_ui, env=env, capture_output=True, text=True, timeout=args.timeout
    )
    dt_sec = time.time() - t0
    results = {"wp": {}, "skp": {}}
    for m in re.finditer(r"ZZRES (\{.*\})", p.stdout + p.stderr):
        try:
            r = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        results.setdefault(r["side"], {})[r["id"]] = r
    Path(args.out_results).write_text(json.dumps(results, ensure_ascii=False) + "\n")
    print(
        f"[felt] rc={p.returncode} {dt_sec:.0f}s "
        f"counts={ {k: len(v) for k, v in results.items()} }"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_diff = sub.add_parser("diff-results", help="Diff existing cases.json and results.json")
    p_diff.add_argument("--cases", required=True)
    p_diff.add_argument("--results", required=True)
    p_diff.add_argument("--out-groups")
    p_diff.set_defaults(func=cmd_diff_results)

    p_felt = sub.add_parser("run-felt", help="Execute felt WP vs SkP suites on cases.json")
    p_felt.add_argument("--worktree", required=True, help="Flutter root worktree path")
    p_felt.add_argument("--cases", required=True)
    p_felt.add_argument("--out-results", required=True)
    p_felt.add_argument("--release", action="store_true", help="Set FELT_NO_ASSERTS=1")
    p_felt.add_argument("--timeout", type=int, default=1800)
    p_felt.set_defaults(func=cmd_run_felt)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
