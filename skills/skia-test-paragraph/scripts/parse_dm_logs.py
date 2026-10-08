#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import re
import signal
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TASK_HEADER_RE = re.compile(
    r"^(?P<srcs>\d+)\s+srcs\s*\*\s*(?P<sinks>\d+)\s+sinks\s*\+\s*(?P<tests>\d+)\s+tests\s*==\s*(?P<tasks>\d+)\s+tasks"
)
PROGRESS_RE = re.compile(
    r"^\[(?P<done>\d+)/(?P<total>\d+)\]"
)
UNIT_TEST_DONE_RE = re.compile(
    r"^\s*unit\s+test\s+(?P<name>\S+)\s+done\s*$"
)
FAILURE_SUMMARY_COUNT_RE = re.compile(
    r"^(?P<count>\d+)\s+failures?\s*$"
)
FAILURE_HEADER_RE = re.compile(
    r"^FAILURE:\s*(?P<file>\S+?):(?P<line>\d+)\t(?P<msg>.*)\s*$"
)


def read_lines(path: pathlib.Path | str) -> list[str]:
    with open(path, "r", errors="replace") as f:
        data = f.read()
    data = ANSI_RE.sub("", data)
    return [ln.rstrip() for ln in re.split(r"[\r\n]", data)]


def summarize_dm_log(path: pathlib.Path | str) -> dict[str, Any]:
    lines = read_lines(path)
    expected_tasks: Optional[int] = None
    expected_tests: Optional[int] = None
    last_done_task: Optional[int] = None
    total_progress_tasks: Optional[int] = None
    completed_tests: list[str] = []
    failures: list[dict[str, str]] = []
    raw_failure_lines: list[str] = []
    in_failures_block = False
    reported_failure_count: Optional[int] = None
    finished_marker = False
    abort_or_crash_detected = False
    crash_reason: Optional[str] = None

    for ln in lines:
        stripped = ln.strip()
        if not stripped:
            continue

        if "SkASSERT" in stripped or "Fatal error" in stripped or "Assertion failed:" in stripped:
            abort_or_crash_detected = True
            crash_reason = stripped

        m_head = TASK_HEADER_RE.match(stripped)
        if m_head:
            expected_tests = int(m_head.group("tests"))
            expected_tasks = int(m_head.group("tasks"))
            continue

        m_prog = PROGRESS_RE.match(stripped)
        if m_prog:
            last_done_task = int(m_prog.group("done"))
            total_progress_tasks = int(m_prog.group("total"))
            continue

        m_unit = UNIT_TEST_DONE_RE.match(ln)
        if m_unit:
            completed_tests.append(m_unit.group("name"))
            continue

        if stripped == "Finished!":
            finished_marker = True
            continue

        m_fail_head = FAILURE_HEADER_RE.match(stripped)
        if m_fail_head:
            failures.append({
                "file": m_fail_head.group("file"),
                "line": m_fail_head.group("line"),
                "message": m_fail_head.group("msg").strip(),
            })
            continue

        if stripped == "Failures:":
            in_failures_block = True
            continue

        m_fc = FAILURE_SUMMARY_COUNT_RE.match(stripped)
        if m_fc:
            reported_failure_count = int(m_fc.group("count"))
            in_failures_block = False
            continue

        if in_failures_block and ln.startswith("\t"):
            raw_failure_lines.append(stripped)

    # Cross-reference parsed failures with the raw trailing block
    unique_failures: list[str] = []
    seen_f: Set[str] = set()
    for f in failures:
        msg = f["message"]
        # Extract bracketed test name if present, e.g. [SkParagraph_Flutter_123065]
        m_name = re.search(r"\[([A-Za-z0-9_]+)\]", msg)
        test_name = m_name.group(1) if m_name else msg
        if test_name not in seen_f:
            seen_f.add(test_name)
            unique_failures.append(test_name)

    total_tasks = expected_tasks if expected_tasks is not None else total_progress_tasks
    tasks_done = last_done_task if last_done_task is not None else len(completed_tests)
    if expected_tasks == 0:
        finished_cleanly = True
    else:
        # Require task completion and explicit summary marker without unhandled asserts
        has_summary = reported_failure_count is not None or finished_marker
        completed_all = (total_tasks is not None and tasks_done == total_tasks)
        finished_cleanly = (
            not abort_or_crash_detected
            and completed_all
            and has_summary
        )

    failed_count = reported_failure_count if reported_failure_count is not None else len(unique_failures)
    total_exec = expected_tests if expected_tests is not None else tasks_done
    passed_count = max(0, total_exec - failed_count) if total_exec is not None else None

    return {
        "kind": "dm",
        "log": str(path),
        "expected_tests": expected_tests,
        "expected_tasks": total_tasks,
        "completed_tasks": tasks_done,
        "passed": passed_count,
        "failed": failed_count,
        "failures": unique_failures,
        "failure_details": failures,
        "finished_cleanly": finished_cleanly,
        "crash_reason": crash_reason,
        "abort_detected": abort_or_crash_detected,
    }


def print_dm_summary(summary: dict[str, Any]) -> None:
    print(f"# dm log: {summary['log']}")
    clean = "YES" if summary["finished_cleanly"] else "NO (ABNORMAL / CRASH / INCOMPLETE)"
    print(f"clean exit: {clean}")
    print(
        f"tasks: {summary['completed_tasks']}/{summary['expected_tasks']} | "
        f"tests: passed={summary['passed']}, failed={summary['failed']}"
    )
    if summary["crash_reason"]:
        print(f"crash: {summary['crash_reason']}")
    for f in summary["failures"]:
        print(f"  [FAIL] {f}")


def _evaluate_attestation(runs: list[tuple[str, dict[str, Any]]]) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    rows: list[str] = []
    seen_tree_hashes: dict[str, str] = {}
    seen_binary_hashes: dict[str, tuple[str, Optional[str]]] = {}
    base_match: Optional[str] = None
    base_config: Optional[str] = None
    base_executed: Optional[int] = None

    for idx, (label, run) in enumerate(runs):
        att = run.get("attestation") or {}
        dm = run.get("dm") or {}

        ref_spec = att.get("ref") or run.get("ref_spec") or "—"
        git_sha = att.get("git_sha")
        dirty_files = att.get("dirty_files") or []
        if git_sha:
            git_col = f"`{ref_spec}` (`{git_sha}`"
            if dirty_files:
                git_col += f" + `{len(dirty_files)} dirty`"
            git_col += ")"
        else:
            git_col = f"`{ref_spec}`"

        tree_sha = att.get("source_tree_sha256")
        tree_col = f"`{tree_sha}`" if tree_sha else "—"
        if tree_sha:
            if tree_sha in seen_tree_hashes:
                prev_label = seen_tree_hashes[tree_sha]
                issues.append(
                    f"`{label}` and `{prev_label}` have identical source tree hash (`{tree_sha}`)"
                )
            else:
                seen_tree_hashes[tree_sha] = label

        # Ninja actions and binary SHA-256
        ninja_rc = att.get("ninja_rc")
        ninja_act = att.get("ninja_actions") or "—"
        ninja_dt = att.get("ninja_duration_s")
        ninja_part = f"`{ninja_act}`"
        if ninja_dt is not None:
            ninja_part += f" ({ninja_dt}s)"
        if ninja_rc not in (None, 0):
            issues.append(f"`{label}` ninja build failed with exit code `{ninja_rc}`")

        bin_sha = att.get("binary_sha256")
        bin_mtime = att.get("binary_mtime")
        if bin_sha:
            bin_col = f"`{bin_sha}`"
            if bin_mtime:
                bin_col += f" ({bin_mtime})"
            if tree_sha and bin_sha in seen_binary_hashes:
                prev_label, prev_tree = seen_binary_hashes[bin_sha]
                if prev_tree and prev_tree != tree_sha:
                    issues.append(
                        f"`{label}` has a different source tree than `{prev_label}` "
                        f"but identical test binary hash (`{bin_sha}`)"
                    )
            seen_binary_hashes[bin_sha] = (label, tree_sha)
        else:
            bin_col = "—"

        # Flags: --match and --config
        t_cmd = att.get("test_cmd") or []
        m_val: Optional[str] = None
        c_val: Optional[str] = None
        for i, arg in enumerate(t_cmd):
            if arg in ("--match", "-m") and i + 1 < len(t_cmd):
                m_val = t_cmd[i + 1]
            elif arg == "--config" and i + 1 < len(t_cmd):
                c_val = t_cmd[i + 1]

        if idx == 0:
            base_match = m_val
            base_config = c_val
        else:
            if m_val != base_match:
                issues.append(
                    f"`{label}` has different `--match` filter (`{m_val}`) than baseline (`{base_match}`)"
                )
            if c_val != base_config:
                issues.append(
                    f"`{label}` has different `--config` filter (`{c_val}`) than baseline (`{base_config}`)"
                )

        flag_str = f"`-m {m_val or 'all'}`"
        if c_val:
            flag_str += f" / `{c_val}`"

        # Test execution status and signal checking
        test_rc = att.get("test_rc")
        sig = att.get("signal")
        finished_cleanly = dm.get("finished_cleanly", False)
        tasks_done = dm.get("completed_tasks", 0)
        tasks_exp = dm.get("expected_tasks", 0)
        tasks_col = f"`{tasks_done}/{tasks_exp}`"

        if idx == 0:
            base_executed = tasks_done
        else:
            if base_executed and tasks_done < base_executed:
                issues.append(
                    f"`{label}` executed fewer tasks (`{tasks_done}`) than baseline (`{base_executed}`)"
                )

        if tasks_done == 0:
            issues.append(f"`{label}` executed 0 tests/tasks")

        status_parts: list[str] = []
        if test_rc == 0 and finished_cleanly:
            status_parts.append("PASS")
        elif test_rc != 0:
            status_parts.append(f"EXIT={test_rc}")

        if sig:
            status_parts.append(f"SIGNAL {sig}")
            issues.append(f"`{label}` test binary crashed by signal `{sig}` (exit code {test_rc})")
        elif test_rc is not None and (test_rc < 0 or test_rc >= 128):
            signum = abs(test_rc) if test_rc < 0 else test_rc - 128
            sig_name = f"SIG{signal.Signals(signum).name}" if signum in signal.Signals._value2member_map_ else f"SIG_{signum}"
            status_parts.append(sig_name)
            issues.append(f"`{label}` test binary crashed by signal `{sig_name}` (exit code {test_rc})")

        if dm.get("abort_detected"):
            status_parts.append("SkASSERT")
            issues.append(f"`{label}` triggered SkASSERT/fatal assertion: {dm.get('crash_reason')}")

        if not finished_cleanly:
            status_parts.append("UNFINISHED")
            issues.append(f"`{label}` test runner did not finish cleanly (incomplete run or mid-run abort)")

        status_col = f"`{' / '.join(status_parts)}`"

        rows.append(
            f"| `{label}` | {git_col} | {tree_col} | {bin_col} | {ninja_part} | {flag_str} | {tasks_col} | {status_col} |"
        )

    return issues, rows


def collect_run_data(path_str: str) -> dict[str, Any]:
    p = pathlib.Path(path_str)
    data: dict[str, Any] = {
        "attestation": None,
        "dm": None,
        "ref_spec": None,
    }
    if p.is_dir():
        att_path = p / "attestation.json"
        if att_path.exists():
            try:
                data["attestation"] = json.loads(att_path.read_text())
            except Exception:
                pass
        sum_path = p / "dm_summary.json"
        if sum_path.exists():
            try:
                data["dm"] = json.loads(sum_path.read_text())
            except Exception:
                pass
        elif (p / "dm.log").exists():
            data["dm"] = summarize_dm_log(p / "dm.log")
    elif p.suffix == ".json":
        data["dm"] = json.loads(p.read_text())
    elif p.exists():
        data["dm"] = summarize_dm_log(p)
    return data


def compare_runs(args: list[str]) -> None:
    runs: list[tuple[str, dict[str, Any]]] = []
    for a in args:
        label, path = a.split("=", 1)
        runs.append((label, collect_run_data(path)))

    base_label, base = runs[0]
    base_fails: Set[str] = set((base.get("dm") or {}).get("failures") or [])

    print(f"# SkParagraph Test Comparison Report (baseline = `{base_label}`)\n")
    print("| Ref | Tasks Completed | Passed | Failed | Regressions | Fixed vs baseline |")
    print("| :--- | :---: | :---: | :---: | :---: | :---: |")

    for idx, (label, run) in enumerate(runs):
        dm = run.get("dm") or {}
        tasks = f"`{dm.get('completed_tasks', 0)}/{dm.get('expected_tasks', 0)}`"
        p_cnt = f"`{dm.get('passed', 0)}`"
        f_cnt = f"`{dm.get('failed', 0)}`"
        fails: Set[str] = set(dm.get("failures") or [])
        if idx == 0:
            reg_str = "*(baseline)*"
            fix_str = "*(baseline)*"
        else:
            regs = fails - base_fails
            fixes = base_fails - fails
            reg_str = f"**{len(regs)}**" if regs else "0"
            fix_str = f"**+{len(fixes)}**" if fixes else "0"
        print(f"| `{label}` | {tasks} | {p_cnt} | {f_cnt} | {reg_str} | {fix_str} |")
    print()

    issues, att_rows = _evaluate_attestation(runs)
    print("## Паспорт прогона (Run Attestation)\n")
    if issues:
        print(f"**Вердикт целостности (Integrity Check):** `WARNING / INVALID` ({len(issues)} issue(s))\n")
        for iss in issues:
            print(f"- **WARNING:** {iss}")
        print()
    else:
        print("**Вердикт целостности (Integrity Check):** `VALID (все тесты завершены штатно, бинарники валидированы, обрывов и подмены исходников нет)`\n")

    print(
        "| Ref | Git Spec / Commit | Source Tree SHA | Binary (`dm`) SHA / mtime | Ninja Build | Filters (`--match` / `--config`) | Tasks Executed | Status / Signal |"
    )
    print("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |")
    for r in att_rows:
        print(r)
    print()

    for label, run in runs[1:]:
        dm = run.get("dm") or {}
        fails = set(dm.get("failures") or [])
        regressions = sorted(fails - base_fails)
        fixes = sorted(base_fails - fails)

        print(f"## `{label}` vs `{base_label}`\n")
        print(
            f"**Выполнено тестов: {dm.get('completed_tasks', 0)} | "
            f"Было фейлов: {len(base_fails)} | Починено: {len(fixes)} | "
            f"Поломано: {len(regressions)} | Стало фейлов: {len(fails)}**\n"
        )
        if regressions:
            print("### Regressions (fail here, pass on baseline)\n")
            for f in regressions:
                print(f"- `{f}`")
            print()
        if fixes:
            print("### Fixed vs baseline (fail on baseline, pass here)\n")
            for f in fixes:
                print(f"- `{f}`")
            print()

    if base_fails:
        print(f"## Baseline Failures (`{base_label}`: {len(base_fails)})\n")
        for f in sorted(base_fails):
            print(f"- `{f}`")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(
            "Usage:\n"
            "  parse_dm_logs.py summarize <dm.log> [--json OUT.json]\n"
            "  parse_dm_logs.py compare <baseline_label>=<run_dir> <label>=<run_dir> [...]"
        )
        return 2

    cmd = argv[1]
    if cmd == "summarize":
        log_path = argv[2]
        summary = summarize_dm_log(log_path)
        print_dm_summary(summary)
        if "--json" in argv:
            out_idx = argv.index("--json") + 1
            if out_idx < len(argv):
                pathlib.Path(argv[out_idx]).write_text(json.dumps(summary, indent=2))
        return 0

    if cmd == "compare":
        compare_runs(argv[2:])
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
