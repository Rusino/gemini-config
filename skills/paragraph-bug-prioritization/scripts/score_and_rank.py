#!/usr/bin/env python3
"""Phase 3 nonlinear S x R x P scorer with [NEEDS_ZOOM_SCAN] and release-severity gates"""
import argparse
import json
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_DIR / "data"

# Nonlinear severity weights: crash (S5=30) >> broken layout (S4=10) >> wrong caret/box (S3=4)
S_NL = {0: 0, 1: 1, 2: 2, 3: 4, 4: 10, 5: 30}
VALID_P = (1.0, 0.3, 0.05, 0.01)


def load_candidates(path):
    p = Path(path)
    text = p.read_text().strip()
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def score_candidates(candidates, reach_map, ignore_zoom=False):
    ranked = []
    for c in candidates:
        cid = c["id"]
        what = c.get("what") or c.get("signature", "")
        s_rel = int(c.get("S", c.get("severity_release", 0)))
        s_dbg = int(c.get("severity_debug", s_rel))
        fids = c.get("features") or ([c["reach_feature"]] if c.get("reach_feature") else [])

        # Use Phase 3 measured zoom_reach if available and not ignored; else look up feature reach
        zoom_r = None if ignore_zoom else c.get("zoom_reach")
        if zoom_r is not None:
            r_val = float(zoom_r)
            p_val = float(c.get("zoom_P", c.get("P", c.get("trigger_band", 1.0))))
            r_basis = c.get("zoom_basis") or c.get("R_basis") or "measured (Phase 3 zoom)"
            has_zoom = True
        else:
            rf = c.get("reach_feature")
            if rf and rf in reach_map:
                r_val = round(float(reach_map[rf]), 2)
                r_basis = c.get("R_basis") or rf
            elif fids:
                r_val = round(min(float(reach_map.get(f, 1.0)) for f in fids), 2)
                r_basis = c.get("R_basis") or "+".join(fids)
            else:
                r_val = float(c.get("R", 0.0))
                r_basis = c.get("R_basis") or "manual"
            p_val = float(c.get("P", c.get("trigger_band", 1.0)))
            has_zoom = bool(str(r_basis).startswith("measured:"))

        s_weight = S_NL[s_rel]
        score_lin = round(s_rel * r_val * p_val, 3)
        score_nl = round(s_weight * r_val * p_val, 2)

        gates = []
        if s_rel == 5 and not c.get("release_verified", False):
            gates.append("UNVERIFIED_RELEASE_SEVERITY")
        if not has_zoom and r_val >= 0.50 and p_val <= 0.05:
            gates.append("NEEDS_ZOOM_SCAN")

        ranked.append({
            "id": cid,
            "what": what,
            "S": s_rel,
            "S_debug": s_dbg,
            "S_nl": s_weight,
            "R": r_val,
            "P": p_val,
            "R_basis": r_basis,
            "score": score_lin,
            "score_nl": score_nl,
            "known": c.get("known") or c.get("known_item", ""),
            "gates": gates,
            "repro": c.get("repro", ""),
        })

    ranked.sort(key=lambda x: (-x["score_nl"], -x["score"], x["id"]))
    for idx, row in enumerate(ranked, 1):
        row["rank"] = idx
        # Only enforce NEEDS_ZOOM_SCAN as a blocking gate if the item lands in the top 10
        if idx > 10 and "NEEDS_ZOOM_SCAN" in row["gates"]:
            row["gates"] = [g for g in row["gates"] if g != "NEEDS_ZOOM_SCAN"]
    return ranked


def format_markdown(ranked, profile):
    lines = [
        f"# Prioritized Paragraph Bugs ({profile})",
        "",
        "Formula: `score_nl = S_nl[S_release] * R * P` "
        "(`S_nl = {0:0, 1:1, 2:2, 3:4, 4:10, 5:30}`)",
        "",
        "| Rank | ID | S (rel) | R | P | score_nl | score_lin | R_basis | Gates | Known | "
        "Summary |",
        "|---:|---|---:|---:|---:|---:|---:|---|---|---|---|",
    ]
    for r in ranked:
        g_str = ", ".join(f"`[{g}]`" for g in r["gates"]) if r["gates"] else "OK"
        lines.append(
            f"| {r['rank']} | **{r['id']}** | S{r['S']} ({r['S_nl']}) | {r['R']:.3g} | "
            f"{r['P']:.2g} | **{r['score_nl']:.2f}** | {r['score']:.3f} | "
            f"{r['R_basis']} | {g_str} | {r['known'][:28]} | {r['what'][:65]} |"
        )
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--candidates",
        default=str(DATA_DIR / "wp_candidates.json"),
        help="Path to candidate bugs JSON or JSONL",
    )
    ap.add_argument("--profile", choices=("web", "skp"), default="web")
    ap.add_argument("--reach-file", help="Override reach profile JSON")
    ap.add_argument(
        "--ignore-zoom",
        action="store_true",
        help="Ignore Phase 3 zoom_reach overrides to test [NEEDS_ZOOM_SCAN] gate detection",
    )
    ap.add_argument("--out-json", help="Write ranked JSON to path")
    ap.add_argument("--out-md", help="Write ranked Markdown table to path")
    args = ap.parse_args()

    reach_path = (
        Path(args.reach_file)
        if args.reach_file
        else DATA_DIR / (f"reach_{args.profile}.json")
    )
    reach_map = json.loads(reach_path.read_text())["R"]
    candidates = load_candidates(args.candidates)
    ranked = score_candidates(candidates, reach_map, ignore_zoom=args.ignore_zoom)

    if args.out_json:
        Path(args.out_json).write_text(json.dumps(ranked, indent=2, ensure_ascii=False) + "\n")
    md = format_markdown(ranked, args.profile)
    if args.out_md:
        Path(args.out_md).write_text(md)
    print(md)


if __name__ == "__main__":
    main()
