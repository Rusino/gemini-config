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


# Classify a newly discovered or externally filed bug against the active Epic score cutoff
def format_intake_verdict(ranked, intake_id, cutoff_score):
    target = next((r for r in ranked if r["id"] == intake_id), None)
    if not target:
        return ""
    # Block roadmap placement until release severity and compound zoom reach are verified
    if target["gates"]:
        g_str = ", ".join(f"[{g}]" for g in target["gates"])
        status = f"BLOCKED_BY_GATES ({g_str})"
        action = "Resolve gates (--intake-release-verified / scan_reach.py zoom) before roadmap placement"
    # High-pain bugs enter the active top Epic directly at their sorted rank
    elif target["score_nl"] >= cutoff_score:
        status = "ACTIVE_TOP_EPIC"
        action = (
            f"Insert into active Epic (R42) at rank #{target['rank']} "
            f"(score_nl={target['score_nl']:.2f} >= cutoff {cutoff_score:.2f})"
        )
    # Low-pain bugs are recorded as frozen so they do not divert bandwidth from the active Epic
    else:
        status = "FROZEN_BACKLOG"
        action = (
            f"Add to roadmap.py as [FROZEN, rank {target['rank']} as {target['id']}, "
            f"score {target['score_nl']:.2f} < cutoff {cutoff_score:.2f}]"
        )
    return (
        f"## Single-Bug Intake Verdict: {target['id']}\n\n"
        f"- **Verdict**: `{status}`\n"
        f"- **Rank**: `#{target['rank']} / {len(ranked)}`\n"
        f"- **Score**: `score_nl = {target['score_nl']:.2f}` "
        f"(`S{target['S']}={target['S_nl']}` * `R={target['R']:.3g}` ({target['R_basis']}) * "
        f"`P={target['P']:.2g}`)\n"
        f"- **Action**: {action}\n\n"
    )


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
    # Single-bug intake flags evaluate a new bug against the pool in one pass without editing JSON
    ap.add_argument(
        "--cutoff-score",
        type=float,
        default=1.09,
        help="Minimum score_nl required to enter the active top Epic (default: 1.09 for R42)",
    )
    ap.add_argument("--intake-id", help="Evaluate a single new/filed bug ID against the pool")
    ap.add_argument("--intake-what", default="", help="One-line summary of the intake bug")
    ap.add_argument(
        "--intake-S",
        type=int,
        choices=(0, 1, 2, 3, 4, 5),
        help="Release-mode severity (0..5) of the intake bug",
    )
    ap.add_argument(
        "--intake-features",
        help="Comma-separated feature IDs (e.g. F04,F26) for the intake bug",
    )
    ap.add_argument(
        "--intake-P",
        type=float,
        default=1.0,
        help="Conditional trigger probability (1.0, 0.3, 0.05, 0.01)",
    )
    ap.add_argument(
        "--intake-zoom-reach",
        type=float,
        help="Measured Phase 3 zoom reach in [0, 1] if already computed via scan_reach.py zoom",
    )
    ap.add_argument(
        "--intake-zoom-basis",
        default="",
        help="Measured Phase 3 zoom basis description",
    )
    ap.add_argument(
        "--intake-release-verified",
        action="store_true",
        help="Mark S5 release crash as verified with assertions disabled",
    )
    ap.add_argument("--intake-known", default="", help="Roadmap or issue reference")
    ap.add_argument("--intake-repro", default="", help="Minimal reproducer snippet")
    ap.add_argument(
        "--save-intake",
        action="store_true",
        help="Persist the intake bug entry into --candidates JSON",
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

    # Inject or replace the intake bug in-memory and optionally persist it to the candidate file
    if args.intake_id:
        if args.intake_S is None:
            ap.error("--intake-S is required when --intake-id is set")
        f_list = (
            [f.strip() for f in args.intake_features.split(",") if f.strip()]
            if args.intake_features
            else []
        )
        entry = {
            "id": args.intake_id,
            "what": args.intake_what,
            "S": args.intake_S,
            "features": f_list,
            "reach_feature": f_list[0] if len(f_list) == 1 else None,
            "P": args.intake_P,
            "release_verified": args.intake_release_verified,
            "known": args.intake_known,
            "repro": args.intake_repro,
        }
        # Record measured Phase 3 compound reach when provided so [NEEDS_ZOOM_SCAN] is satisfied
        if args.intake_zoom_reach is not None:
            entry["zoom_reach"] = args.intake_zoom_reach
            entry["zoom_P"] = args.intake_P
            entry["zoom_basis"] = args.intake_zoom_basis or "measured (Phase 3 zoom)"
        candidates = [c for c in candidates if c.get("id") != args.intake_id] + [entry]
        # Only mutate the canonical candidate database when explicitly requested via --save-intake
        if args.save_intake:
            Path(args.candidates).write_text(
                json.dumps(candidates, indent=2, ensure_ascii=False) + "\n"
            )

    ranked = score_candidates(candidates, reach_map, ignore_zoom=args.ignore_zoom)

    if args.out_json:
        Path(args.out_json).write_text(json.dumps(ranked, indent=2, ensure_ascii=False) + "\n")
    # Prepend the single-bug intake summary above the full table when --intake-id is used
    verdict_md = (
        format_intake_verdict(ranked, args.intake_id, args.cutoff_score)
        if args.intake_id
        else ""
    )
    md = verdict_md + format_markdown(ranked, args.profile)
    if args.out_md:
        Path(args.out_md).write_text(md)
    print(md)


if __name__ == "__main__":
    main()
