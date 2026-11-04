#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import re
import signal
import sys
from typing import Any

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
GTEST_TIMING_SUFFIX_RE = re.compile(r"\s+\(\d+\s*ms\)$")

PASSING_STATUSES = frozenset({"PASSED", "FLAKY"})
FAILING_STATUSES = frozenset(
    {
        "FAILED",
        "TIMEOUT",
        "FAILED_TO_BUILD",
        "ANALYSIS_FAILURE",
        "REMOTE_ERROR",
        "INCOMPLETE",
    }
)
SKIPPED_STATUSES = frozenset({"SKIPPED", "NO_STATUS", "SKIPPED_NON_HOST"})


def _read_clean_lines(path: pathlib.Path) -> list[str]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    cleaned = ANSI_RE.sub("", raw)
    return [ln.rstrip() for ln in re.split(r"[\r\n]", cleaned)]


def _clean_detail(text: str, max_len: int = 220) -> str:
    flat = " ".join(text.replace("`", "'").split())
    return flat[:max_len]


def _read_targets_file(path: pathlib.Path | None) -> list[str]:
    if path is None or not path.exists():
        return []
    out: list[str] = []
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


# Parse Blaze Build Event Protocol JSONL into per-target statuses and build metrics
def parse_bep_file(
    bep_path: pathlib.Path,
    expected_targets: list[str] | None = None,
    blaze_log_path: pathlib.Path | None = None,
) -> dict[str, Any]:
    invocation_id: str | None = None
    start_ms: int | None = None
    finish_ms: int | None = None
    bep_exit_code: int | None = None
    bep_exit_name: str | None = None
    saw_build_finished = False
    actions_executed: int | None = None
    cpp_compile_actions: int = 0

    configured_targets: dict[str, dict[str, Any]] = {}
    aborted_targets: dict[str, dict[str, Any]] = {}
    aborted_patterns: dict[str, dict[str, Any]] = {}
    target_completed: dict[str, tuple[bool, str]] = {}
    test_summaries: dict[str, str] = {}
    test_durations_ms: dict[str, int] = {}
    test_details: dict[str, str] = {}

    if bep_path.exists():
        with bep_path.open("r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev_id = ev.get("id") or {}

                if "started" in ev_id:
                    st = ev.get("started") or {}
                    invocation_id = st.get("uuid") or invocation_id
                    if "startTimeMillis" in st:
                        start_ms = int(st["startTimeMillis"])
                elif "buildFinished" in ev_id:
                    fin = ev.get("finished") or {}
                    saw_build_finished = True
                    if "finishTimeMillis" in fin:
                        finish_ms = int(fin["finishTimeMillis"])
                    ec = fin.get("exitCode") or {}
                    if "code" in ec:
                        bep_exit_code = int(ec["code"])
                    bep_exit_name = ec.get("name")
                elif "buildMetrics" in ev_id:
                    act_sum = (ev.get("buildMetrics") or {}).get("actionSummary") or {}
                    if "actionsExecuted" in act_sum:
                        actions_executed = int(act_sum["actionsExecuted"])
                    for item in act_sum.get("actionData") or []:
                        if item.get("mnemonic") == "CppCompile":
                            cpp_compile_actions += int(item.get("actionsExecuted", 0))
                elif "pattern" in ev_id and "aborted" in ev:
                    for pat in ev_id["pattern"].get("pattern") or []:
                        if pat.startswith("//"):
                            aborted_patterns[pat] = ev["aborted"]
                elif "targetConfigured" in ev_id and "aspect" not in ev_id["targetConfigured"]:
                    lbl = ev_id["targetConfigured"].get("label", "")
                    if not lbl:
                        continue
                    if "aborted" in ev:
                        aborted_targets[lbl] = ev["aborted"]
                    else:
                        configured_targets[lbl] = ev.get("configured") or {}
                elif "targetCompleted" in ev_id and "aspect" not in ev_id["targetCompleted"]:
                    lbl = ev_id["targetCompleted"].get("label", "")
                    if not lbl:
                        continue
                    comp = ev.get("completed") or {}
                    succ = bool(comp.get("success", False))
                    fdet = comp.get("failureDetail")
                    msg = ""
                    if isinstance(fdet, dict):
                        msg = str(fdet.get("message", ""))
                    elif fdet is not None:
                        msg = str(fdet)
                    target_completed[lbl] = (succ, msg)
                elif "testResult" in ev_id:
                    lbl = ev_id["testResult"].get("label", "")
                    if not lbl:
                        continue
                    res = ev.get("testResult") or {}
                    dur = int(res.get("testAttemptDurationMillis", 0) or 0)
                    if dur:
                        test_durations_ms[lbl] = max(test_durations_ms.get(lbl, 0), dur)
                    st_name = res.get("status", "")
                    if st_name and st_name != "PASSED":
                        det = str(res.get("statusDetails", "")).strip()
                        warns = [str(w).strip() for w in (res.get("warning") or []) if w]
                        combined = "; ".join(x for x in [det, *warns] if x)
                        if combined:
                            test_details[lbl] = _clean_detail(combined)
                elif "testSummary" in ev_id:
                    lbl = ev_id["testSummary"].get("label", "")
                    if not lbl:
                        continue
                    ts = ev.get("testSummary") or {}
                    test_summaries[lbl] = str(ts.get("overallStatus", "UNKNOWN"))
                    dur = 0
                    if "totalRunDurationMillis" in ts:
                        dur = int(ts["totalRunDurationMillis"])
                    elif "lastStopTimeMillis" in ts and "firstStartTimeMillis" in ts:
                        dur = int(ts["lastStopTimeMillis"]) - int(ts["firstStartTimeMillis"])
                    if dur:
                        test_durations_ms[lbl] = max(test_durations_ms.get(lbl, 0), dur)

    universe = (
        list(dict.fromkeys(expected_targets))
        if expected_targets
        else sorted(
            set(configured_targets)
            | set(aborted_targets)
            | set(aborted_patterns)
            | set(test_summaries)
        )
    )

    statuses: dict[str, str] = {}
    details: dict[str, str] = {}
    counts = {
        "total": len(universe),
        "passed": 0,
        "failed": 0,
        "flaky": 0,
        "timeout": 0,
        "failed_to_build": 0,
        "analysis_failure": 0,
        "skipped": 0,
        "incomplete": 0,
    }

    for lbl in universe:
        if lbl in test_summaries:
            st = test_summaries[lbl]
            if st == "PASSED":
                statuses[lbl] = "PASSED"
                counts["passed"] += 1
            elif st == "FLAKY":
                statuses[lbl] = "FLAKY"
                counts["flaky"] += 1
            elif st == "FAILED":
                statuses[lbl] = "FAILED"
                counts["failed"] += 1
            elif st == "TIMEOUT":
                statuses[lbl] = "TIMEOUT"
                counts["timeout"] += 1
            elif st == "FAILED_TO_BUILD":
                statuses[lbl] = "FAILED_TO_BUILD"
                counts["failed_to_build"] += 1
            elif st in ("SKIPPED", "NO_STATUS"):
                statuses[lbl] = st
                counts["skipped"] += 1
            elif "FAIL" in st or "ERROR" in st:
                statuses[lbl] = "REMOTE_ERROR" if "REMOTE" in st else "FAILED"
                counts["failed"] += 1
            else:
                statuses[lbl] = "SKIPPED"
                counts["skipped"] += 1
            if lbl in test_details:
                details[lbl] = test_details[lbl]
        elif lbl in aborted_targets:
            ab = aborted_targets[lbl]
            reason = str(ab.get("reason", "ANALYSIS_FAILURE"))
            desc = _clean_detail(str(ab.get("description", "")))
            if reason == "SKIPPED":
                statuses[lbl] = "SKIPPED"
                counts["skipped"] += 1
            else:
                statuses[lbl] = "ANALYSIS_FAILURE"
                counts["analysis_failure"] += 1
            if desc:
                details[lbl] = desc
        elif lbl in target_completed:
            succ, msg = target_completed[lbl]
            if not succ:
                statuses[lbl] = "FAILED_TO_BUILD"
                counts["failed_to_build"] += 1
                if msg:
                    details[lbl] = _clean_detail(msg)
            else:
                kind = str((configured_targets.get(lbl) or {}).get("targetKind", ""))
                statuses[lbl] = "SKIPPED_NON_HOST"
                counts["skipped"] += 1
                if kind:
                    details[lbl] = f"Built ({kind}), no host test runner"
        elif lbl in aborted_patterns:
            ab = aborted_patterns[lbl]
            reason = str(ab.get("reason", "LOADING_FAILURE"))
            desc = _clean_detail(str(ab.get("description", "")))
            desc_lower = desc.lower()
            # Targets added/removed across CitC base CLs emit LOADING_FAILURE with no such
            # target/package under --keep_going; treat syntax/Starlark errors as ANALYSIS_FAILURE
            is_missing_at_cl = reason == "SKIPPED" or (
                reason == "LOADING_FAILURE"
                and ("no such target" in desc_lower or "no such package" in desc_lower)
            )
            if is_missing_at_cl:
                statuses[lbl] = "SKIPPED"
                counts["skipped"] += 1
            else:
                statuses[lbl] = "ANALYSIS_FAILURE"
                counts["analysis_failure"] += 1
            if desc:
                details[lbl] = desc
        else:
            statuses[lbl] = "INCOMPLETE"
            counts["incomplete"] += 1
            details[lbl] = "No completion event in BEP"

    abort_detected = False
    crash_reason: str | None = None
    if blaze_log_path and blaze_log_path.exists():
        log_lines = _read_clean_lines(blaze_log_path)
        failing_lbls = [k for k, v in statuses.items() if v in FAILING_STATUSES]
        short_to_lbls: dict[str, list[str]] = {}
        for lbl in failing_lbls:
            short_to_lbls.setdefault(lbl.split(":")[-1], []).append(lbl)

        for ln in log_lines:
            s = ln.strip()
            if not s:
                continue
            if "SkASSERT" in s or "Fatal error" in s or "Assertion failed:" in s:
                abort_detected = True
                crash_reason = s
            if s.startswith("[  FAILED  ] ") and not s.endswith(("test.", "tests.")):
                case_name = GTEST_TIMING_SUFFIX_RE.sub("", s[len("[  FAILED  ] ") :].strip())
                for lbl in failing_lbls:
                    if lbl in s:
                        details[lbl] = f"{case_name} ({details.get(lbl, '')})".strip(" ()")
            elif any(
                tok in s
                for tok in (
                    "ERROR:",
                    "FAILED:",
                    "wrapped_clang failed",
                    "layering_check",
                    "undeclared inclusion",
                )
            ):
                for lbl in failing_lbls:
                    short_t = lbl.split(":")[-1]
                    if lbl in s or (len(short_to_lbls.get(short_t, [])) == 1 and short_t in s):
                        if not details.get(lbl) or details.get(lbl) == "Exit 1":
                            details[lbl] = s[:220]

    wall_s: float | None = None
    if start_ms is not None and finish_ms is not None and finish_ms >= start_ms:
        wall_s = round((finish_ms - start_ms) / 1000.0, 1)

    executed_tests = (
        counts["passed"] + counts["failed"] + counts["flaky"] + counts["timeout"]
    )
    # Exit codes 0 (all pass), 1 (build failure with --keep_going), 3 (tests failed), 4 (no tests)
    infra_crash = bep_exit_code in (36, 37, 38)
    finished_cleanly = (
        saw_build_finished
        and not infra_crash
        and counts["incomplete"] == 0
        and executed_tests > 0
    )

    return {
        "bep_path": str(bep_path),
        "invocation_id": invocation_id,
        "sponge_url": f"http://sponge2/{invocation_id}" if invocation_id else None,
        "wall_duration_s": wall_s,
        "bep_exit_code": bep_exit_code,
        "bep_exit_name": bep_exit_name,
        "saw_build_finished": saw_build_finished,
        "actions_executed": actions_executed,
        "cpp_compile_actions": cpp_compile_actions,
        "counts": counts,
        "executed_tests": executed_tests,
        "finished_cleanly": finished_cleanly,
        "abort_detected": abort_detected,
        "crash_reason": crash_reason,
        "statuses": statuses,
        "details": details,
        "durations_ms": test_durations_ms,
    }


def collect_run_data(spec_path: str, fallback_targets_file: pathlib.Path | None) -> dict[str, Any]:
    p = pathlib.Path(spec_path)
    att: dict[str, Any] = {}
    summary: dict[str, Any] | None = None

    if p.is_dir():
        att_path = p / "attestation.json"
        if att_path.exists():
            att = json.loads(att_path.read_text(encoding="utf-8"))
        sum_path = p / "summary.json"
        if sum_path.exists():
            summary = json.loads(sum_path.read_text(encoding="utf-8"))
        if summary is None:
            t_file = p / "targets.txt"
            targets = _read_targets_file(t_file if t_file.exists() else fallback_targets_file)
            log_p = p / "blaze.log"
            summary = parse_bep_file(
                p / "bep.json",
                expected_targets=targets or None,
                blaze_log_path=log_p if log_p.exists() else None,
            )
    else:
        targets = _read_targets_file(fallback_targets_file)
        summary = parse_bep_file(p, expected_targets=targets or None)

    return {"attestation": att, "summary": summary}


BUILD_FAIL_STATUSES = frozenset({"FAILED_TO_BUILD", "ANALYSIS_FAILURE"})


def _evaluate_attestation(runs: list[tuple[str, dict[str, Any]]]) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    rows: list[str] = []
    seen_tree_hashes: dict[str, str] = {}
    base_tier: str | None = None
    base_total: int | None = None
    base_g3_sha: str | None = None
    base_cl: Any = None
    base_statuses: dict[str, str] = {}
    base_executed: int = 0

    for idx, (label, run) in enumerate(runs):
        att = run.get("attestation") or {}
        sm = run.get("summary") or {}
        counts = sm.get("counts") or {}
        g3_sha = att.get("g3_skia_revision")

        ref_spec = att.get("ref_spec") or label
        skia_sha = att.get("skia_commit_sha")
        cand_base = att.get("candidate_base_sha")
        dirty_cnt = len(att.get("skia_dirty_files") or [])
        spec_col = f"`{ref_spec}`"
        if skia_sha:
            spec_col += f" (`{skia_sha[:10]}`"
            if dirty_cnt:
                spec_col += f" + `{dirty_cnt} dirty`"
            if cand_base and g3_sha and cand_base != g3_sha:
                spec_col += f", base `{cand_base[:10]}`"
            spec_col += ")"
        elif cand_base and g3_sha and cand_base != g3_sha:
            spec_col += f" (base `{cand_base[:10]}`)"

        citc_client = att.get("citc_client") or "—"
        citc_cl = att.get("citc_base_cl")
        citc_col = f"`{citc_client}`" + (f" @ `cl/{citc_cl}`" if citc_cl else "")

        pre_dirty = att.get("citc_pre_dirty_files") or []
        if pre_dirty:
            issues.append(
                f"`{label}` started on dirty CitC client `{citc_client}` "
                f"(`{len(pre_dirty)}` pre-existing file(s))"
            )
        if att.get("citc_restored_clean") is False:
            issues.append(f"`{label}` failed to restore CitC client `{citc_client}` to clean state")

        g3_col = f"`{g3_sha[:12]}`" if g3_sha else "—"
        if idx == 0:
            base_g3_sha = g3_sha
            base_cl = citc_cl
            base_statuses = dict(sm.get("statuses") or {})
            base_executed = int(sm.get("executed_tests", 0) or 0)
        else:
            if base_g3_sha and g3_sha and g3_sha != base_g3_sha:
                issues.append(
                    f"`{label}` G3 Skia revision (`{g3_sha[:12]}`) differs from "
                    f"baseline (`{base_g3_sha[:12]}`)"
                )
            if base_cl and citc_cl and citc_cl != base_cl:
                issues.append(
                    f"`{label}` CitC base CL (`{citc_cl}`) differs from baseline (`{base_cl}`)"
                )

        tree_sha = att.get("source_tree_sha256")
        tree_col = f"`{tree_sha}`" if tree_sha else "—"
        if tree_sha:
            if tree_sha in seen_tree_hashes:
                prev = seen_tree_hashes[tree_sha]
                issues.append(
                    f"`{label}` and `{prev}` have identical G3 Skia tree SHA (`{tree_sha}`)"
                )
            else:
                seen_tree_hashes[tree_sha] = label

        rewritten_cnt = att.get("rewritten_file_count")
        deleted_cnt = att.get("deleted_file_count", 0)
        if rewritten_cnt is not None:
            rw_col = f"`{rewritten_cnt}w`" + (f" / `{deleted_cnt}d`" if deleted_cnt else "")
        else:
            rw_col = "—"

        act = sm.get("actions_executed")
        cpp_act = sm.get("cpp_compile_actions", 0)
        wall_s = sm.get("wall_duration_s") or att.get("wall_duration_s")
        inv_id = sm.get("invocation_id")
        act_parts: list[str] = []
        if att.get("baseline_cache_hit"):
            act_parts.append("`cached`")
        if act is not None:
            act_parts.append(f"`{act}` act (`{cpp_act}` C++)")
        if wall_s is not None:
            act_parts.append(f"{wall_s}s")
        if inv_id:
            act_parts.append(f"[sponge](http://sponge2/{inv_id})")
        blaze_col = " · ".join(act_parts) if act_parts else "—"

        tier = att.get("tier") or "custom"
        total_t = counts.get("total", 0)
        tier_col = f"`{tier}` (`{total_t}`)"
        if idx == 0:
            base_tier = tier
            base_total = total_t
        else:
            if tier != base_tier or total_t != base_total:
                issues.append(
                    f"`{label}` tier/targets (`{tier}:{total_t}`) differ from "
                    f"baseline (`{base_tier}:{base_total}`)"
                )

        if att.get("targets_stale_warning"):
            issues.append(f"`{label}` target list stale: {att['targets_stale_warning']}")

        p_cnt = counts.get("passed", 0)
        f_cnt = (
            counts.get("failed", 0)
            + counts.get("timeout", 0)
            + counts.get("failed_to_build", 0)
            + counts.get("analysis_failure", 0)
        )
        fl_cnt = counts.get("flaky", 0)
        tests_col = f"`+{p_cnt} -{f_cnt} ~{fl_cnt}`"

        cand_exec = int(sm.get("executed_tests", 0) or 0)
        if cand_exec == 0:
            issues.append(f"`{label}` executed 0 tests")

        # Flag silent cherry-pick skew and candidate build-failure drops so uncompiled targets
        # never masquerade as a clean VALID corpus run
        if idx > 0:
            cand_statuses = sm.get("statuses") or {}
            new_build_fails = [
                t_lbl
                for t_lbl, c_st in sorted(cand_statuses.items())
                if c_st in BUILD_FAIL_STATUSES
                and base_statuses.get(t_lbl) not in BUILD_FAIL_STATUSES
            ]
            skew = att.get("stack_base_skew")
            if isinstance(skew, dict) and skew.get("warning"):
                if skew.get("severity", "warning") == "warning" or new_build_fails:
                    issues.append(f"`{label}` stack-base skew: {skew['warning']}")
            elif isinstance(skew, str) and skew:
                issues.append(f"`{label}` stack-base skew: {skew}")
            if new_build_fails:
                sample_lbl = new_build_fails[0]
                issues.append(
                    f"`{label}` failed to build/analyze `{len(new_build_fails)}` target(s) "
                    f"that built on baseline (`executed_tests` `{cand_exec}` vs "
                    f"`{base_executed}`, e.g. `{sample_lbl}`)"
                )

        status_parts: list[str] = []
        rc = att.get("blaze_rc")
        if rc is None:
            rc = sm.get("bep_exit_code")
        fin = bool(sm.get("finished_cleanly", False))
        if rc == 0 and fin:
            status_parts.append("PASS")
        elif rc is not None:
            status_parts.append(f"EXIT={rc}")
            if rc < 0 or rc >= 128:
                signum = abs(rc) if rc < 0 else rc - 128
                sname = (
                    f"SIG{signal.Signals(signum).name}"
                    if signum in signal.Signals._value2member_map_
                    else f"SIG_{signum}"
                )
                status_parts.append(sname)
                issues.append(f"`{label}` blaze crashed with `{sname}` (rc={rc})")

        if sm.get("abort_detected"):
            status_parts.append("SkASSERT")
            issues.append(f"`{label}` triggered assertion: {sm.get('crash_reason')}")

        if not fin:
            status_parts.append("UNFINISHED")
            inc = counts.get("incomplete", 0)
            issues.append(
                f"`{label}` did not finish cleanly (saw_build_finished="
                f"{sm.get('saw_build_finished')}, incomplete={inc}, exit={rc})"
            )

        status_col = f"`{' / '.join(status_parts)}`" if status_parts else "`—`"
        rows.append(
            f"| `{label}` | {spec_col} | {citc_col} | {g3_col} | {tree_col} | "
            f"{rw_col} | {blaze_col} | {tier_col} | {tests_col} | {status_col} |"
        )

    return issues, rows


def _classify_vs_baseline(
    base_statuses: dict[str, str],
    cand_statuses: dict[str, str],
) -> dict[str, list[tuple[str, str, str]]]:
    all_lbls = sorted(set(base_statuses) | set(cand_statuses))
    regressions: list[tuple[str, str, str]] = []
    flips_to_pass: list[tuple[str, str, str]] = []
    flaky_shifts: list[tuple[str, str, str]] = []
    pre_existing_fails: list[tuple[str, str, str]] = []

    for lbl in all_lbls:
        b_st = base_statuses.get(lbl, "MISSING")
        c_st = cand_statuses.get(lbl, "MISSING")
        if b_st in PASSING_STATUSES and c_st in FAILING_STATUSES:
            regressions.append((lbl, b_st, c_st))
        elif b_st in PASSING_STATUSES and c_st in SKIPPED_STATUSES:
            regressions.append((lbl, b_st, c_st))
        elif b_st in FAILING_STATUSES and c_st in PASSING_STATUSES:
            flips_to_pass.append((lbl, b_st, c_st))
        elif b_st in FAILING_STATUSES and c_st in FAILING_STATUSES:
            pre_existing_fails.append((lbl, b_st, c_st))
        elif b_st in PASSING_STATUSES and c_st in PASSING_STATUSES and b_st != c_st:
            flaky_shifts.append((lbl, b_st, c_st))

    return {
        "regressions": regressions,
        "flips_to_pass": flips_to_pass,
        "flaky_shifts": flaky_shifts,
        "pre_existing_fails": pre_existing_fails,
    }


def format_comparison_markdown(runs: list[tuple[str, dict[str, Any]]]) -> str:
    lines: list[str] = []
    base_label, base_run = runs[0]
    base_sm = base_run.get("summary") or {}
    base_statuses: dict[str, str] = base_sm.get("statuses") or {}
    base_details: dict[str, str] = base_sm.get("details") or {}

    lines.append(f"# Google3 Paragraph Corpus Comparison (baseline = `{base_label}`)\n")
    lines.append(
        "| Ref | Tier | Passed | Failed | Flaky | Timeout | Build/Analysis Fail | "
        "Skipped | Regressions | Flips to Pass |"
    )
    lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")

    diffs_by_label: dict[str, dict[str, list[tuple[str, str, str]]]] = {}
    for idx, (label, run) in enumerate(runs):
        sm = run.get("summary") or {}
        att = run.get("attestation") or {}
        c = sm.get("counts") or {}
        tier = att.get("tier") or "custom"
        ba_fail = c.get("failed_to_build", 0) + c.get("analysis_failure", 0)

        if idx == 0:
            reg_col = "*(baseline)*"
            flip_col = "*(baseline)*"
        else:
            diff = _classify_vs_baseline(base_statuses, sm.get("statuses") or {})
            diffs_by_label[label] = diff
            regs = diff["regressions"]
            flips = diff["flips_to_pass"]
            reg_col = f"**{len(regs)}**" if regs else "0"
            flip_col = f"**+{len(flips)}**" if flips else "0"

        lines.append(
            f"| `{label}` | `{tier}` | `{c.get('passed', 0)}` | `{c.get('failed', 0)}` | "
            f"`{c.get('flaky', 0)}` | `{c.get('timeout', 0)}` | `{ba_fail}` | "
            f"`{c.get('skipped', 0)}` | {reg_col} | {flip_col} |"
        )
    lines.append("")

    issues, att_rows = _evaluate_attestation(runs)
    lines.append("## Паспорт прогона (Run Attestation)\n")
    if issues:
        lines.append(
            f"**Вердикт целостности (Integrity Check):** `WARNING / INVALID` "
            f"({len(issues)} issue(s))\n"
        )
        for iss in issues:
            lines.append(f"- **WARNING:** {iss}")
        lines.append("")
    else:
        lines.append(
            "**Вердикт целостности (Integrity Check):** "
            "`VALID (BEP завершён штатно, CitC восстановлен, подмены исходников нет)`\n"
        )

    lines.append(
        "| Ref | Spec / Skia Base | CitC Client @ Base CL | G3 Skia SHA | "
        "G3 Skia Tree SHA | Rewrites | Blaze Actions / Wall | Tier (Targets) | "
        "Тесты (+pass -fail ~flaky) | Status / Exit |"
    )
    lines.append(
        "| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |"
    )
    lines.extend(att_rows)
    lines.append("")

    for label, run in runs[1:]:
        sm = run.get("summary") or {}
        cand_details: dict[str, str] = sm.get("details") or {}
        diff = diffs_by_label[label]
        regs = diff["regressions"]
        flips = diff["flips_to_pass"]
        pre_fails = diff["pre_existing_fails"]
        flaky_shifts = diff["flaky_shifts"]

        lines.append(f"## `{label}` vs `{base_label}`\n")
        lines.append(
            f"**Выполнено тестов: {sm.get('executed_tests', 0)} | "
            f"Новых падений (Regressions): {len(regs)} | "
            f"Флипов FAIL→PASS: {len(flips)} | "
            f"Pre-existing падений: {len(pre_fails)}**\n"
        )

        if regs:
            lines.append("### Regressions (PASS/FLAKY on baseline -> FAIL here)\n")
            for lbl, b_st, c_st in regs:
                det = cand_details.get(lbl, "")
                suffix = f" — `{det}`" if det else ""
                lines.append(f"- `{lbl}` (`{b_st}` -> `{c_st}`){suffix}")
            lines.append("")

        if flips:
            lines.append(
                "### Newly Passing / Baseline Flake Flips "
                "(FAIL on baseline -> PASS/FLAKY here)\n"
            )
            for lbl, b_st, c_st in flips:
                det = base_details.get(lbl, "")
                suffix = f" — baseline reason: `{det}`" if det else ""
                lines.append(f"- `{lbl}` (`{b_st}` -> `{c_st}`){suffix}")
            lines.append("")

        if flaky_shifts:
            lines.append("### Flaky Status Shifts (`PASSED` <-> `FLAKY`)\n")
            for lbl, b_st, c_st in flaky_shifts:
                lines.append(f"- `{lbl}` (`{b_st}` -> `{c_st}`)")
            lines.append("")

        if pre_fails:
            lines.append("### Pre-existing Failures (failing on both baseline and candidate)\n")
            for lbl, b_st, c_st in pre_fails:
                det = cand_details.get(lbl) or base_details.get(lbl) or ""
                suffix = f" — `{det}`" if det else ""
                lines.append(f"- `{lbl}` (`{b_st}` -> `{c_st}`){suffix}")
            lines.append("")

    if len(runs) == 1:
        single_fails = sorted(
            lbl for lbl, st in base_statuses.items() if st in FAILING_STATUSES
        )
        if single_fails:
            lines.append(f"## Failing Targets (`{base_label}`: {len(single_fails)})\n")
            for lbl in single_fails:
                st = base_statuses[lbl]
                det = base_details.get(lbl, "")
                suffix = f" — `{det}`" if det else ""
                lines.append(f"- `{lbl}` (`{st}`){suffix}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse google3 BEP JSONL test logs and emit compare.md"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_cmp = sub.add_parser("compare", help="Compare one or more runs (label=dir_or_bep)")
    p_cmp.add_argument("--targets-file", type=pathlib.Path, default=None)
    p_cmp.add_argument("--out", type=pathlib.Path, default=None)
    p_cmp.add_argument("runs", nargs="+", help="label=dir_or_bep_json")

    args = parser.parse_args(argv)
    if args.cmd == "compare":
        parsed_runs: list[tuple[str, dict[str, Any]]] = []
        for spec in args.runs:
            if "=" not in spec:
                parser.error(f"Expected label=path, got: {spec}")
            lbl, path_str = spec.split("=", 1)
            parsed_runs.append((lbl, collect_run_data(path_str, args.targets_file)))
        md = format_comparison_markdown(parsed_runs)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(md, encoding="utf-8")
        sys.stdout.write(md)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
