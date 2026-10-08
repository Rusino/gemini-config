#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import re
import signal
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PROGRESS_RE = re.compile(
    r"^(?P<time>\d{2}:\d{2})\s+\+(?P<passed>\d+)(?:\s+~(?P<skipped>\d+))?(?:\s+-(?P<failed>\d+))?:\s+(?P<rest>.*)$"
)
GTEST_PASSED_RE = re.compile(r"\[\s*PASSED\s*\]\s+(\d+)\s+tests?")
GTEST_FAILED_RE = re.compile(r"\[\s*FAILED\s*\]\s+(\d+)\s+tests?")


def read_lines(path: pathlib.Path | str) -> list[str]:
    with open(path, "r", errors="replace") as f:
        data = f.read()
    data = ANSI_RE.sub("", data)
    return [ln.rstrip() for ln in re.split(r"[\r\n]", data)]


def summarize_dart_log(path: pathlib.Path | str) -> dict[str, Any]:
    lines = read_lines(path)
    failures: list[str] = []
    seen_failures: Set[str] = set()
    last_progress: Optional[dict[str, str]] = None
    final_status = "unfinished"
    abort_or_crash = False
    crash_reason: Optional[str] = None

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if "SkASSERT" in s or "Fatal error" in s or "Assertion failed:" in s:
            abort_or_crash = True
            crash_reason = s

        m = PROGRESS_RE.match(s)
        if m:
            last_progress = m.groupdict()
            rest = m.group("rest").strip()
            if rest in ("All tests passed!", "Some tests failed."):
                final_status = "pass" if rest.startswith("All") else "fail"
                continue
            if rest.endswith("[E]"):
                t_name = rest[: -len("[E]")].strip()
                if t_name not in seen_failures:
                    seen_failures.add(t_name)
                    failures.append(t_name)

    passed = int(last_progress["passed"]) if last_progress else 0
    skipped = int(last_progress["skipped"] or 0) if last_progress else 0
    failed = int(last_progress["failed"] or 0) if last_progress else len(failures)

    finished_cleanly = (not abort_or_crash) and (final_status in ("pass", "fail"))

    return {
        "log": str(path),
        "status": final_status,
        "passed": passed,
        "skipped": skipped,
        "failed": failed,
        "failures": failures,
        "finished_cleanly": finished_cleanly,
        "crash_reason": crash_reason,
        "abort_detected": abort_or_crash,
    }


def summarize_gtest_log(path: pathlib.Path | str) -> dict[str, Any]:
    lines = read_lines(path)
    passed = 0
    failed = 0
    failures: list[str] = []
    abort_or_crash = False
    crash_reason: Optional[str] = None
    saw_passed_line = False

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if "SkASSERT" in s or "Fatal error" in s or "Assertion failed:" in s:
            abort_or_crash = True
            crash_reason = s

        m_p = GTEST_PASSED_RE.search(s)
        if m_p:
            passed = int(m_p.group(1))
            saw_passed_line = True
            continue

        m_f = GTEST_FAILED_RE.search(s)
        if m_f:
            failed = int(m_f.group(1))
            continue

        if s.startswith("[  FAILED  ] ") and not s.endswith("test.") and not s.endswith("tests."):
            name = s[len("[  FAILED  ] ") :].strip()
            if name:
                failures.append(name)

    finished_cleanly = (not abort_or_crash) and saw_passed_line

    return {
        "log": str(path),
        "status": "pass" if (failed == 0 and finished_cleanly) else "fail",
        "passed": passed,
        "failed": failed,
        "failures": failures,
        "finished_cleanly": finished_cleanly,
        "crash_reason": crash_reason,
        "abort_detected": abort_or_crash,
    }


def _evaluate_attestation(runs: list[tuple[str, dict[str, Any]]]) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    rows: list[str] = []
    seen_tree_hashes: dict[str, str] = {}
    seen_binary_hashes: dict[str, tuple[str, Optional[str]]] = {}
    base_batches: Optional[str] = None
    base_executed: Optional[int] = None

    for idx, (label, run) in enumerate(runs):
        att = run.get("attestation") or {}
        summary = run.get("summary") or {}

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

        # Ninja actions and duration
        ninja_rc = att.get("ninja_rc")
        ninja_act = att.get("ninja_actions") or "—"
        ninja_dt = att.get("ninja_duration_s")
        ninja_part = f"`{ninja_act}`"
        if ninja_dt is not None:
            ninja_part += f" ({ninja_dt}s)"
        if ninja_rc not in (None, 0):
            issues.append(f"`{label}` ninja build failed with exit code `{ninja_rc}`")

        # Binary verification (flutter_tester)
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
                        f"but identical `flutter_tester` hash (`{bin_sha}`)"
                    )
            seen_binary_hashes[bin_sha] = (label, tree_sha)
        else:
            bin_col = "—"

        batches_run = att.get("batches_run") or "all"
        if idx == 0:
            base_batches = batches_run
        else:
            if batches_run != base_batches:
                issues.append(
                    f"`{label}` ran batches (`{batches_run}`) differing from baseline (`{base_batches}`)"
                )

        batch_col = f"`{batches_run}`"

        # Overall execution counts and proof of completeness
        total_passed = summary.get("total_passed", 0)
        total_failed = summary.get("total_failed", 0)
        total_executed = total_passed + total_failed
        finished_cleanly = summary.get("finished_cleanly", False)
        tests_col = f"`+{total_passed} -{total_failed}`"

        if idx == 0:
            base_executed = total_executed
        else:
            if base_executed and total_executed < base_executed:
                issues.append(
                    f"`{label}` executed fewer tests (`{total_executed}`) than baseline (`{base_executed}`)"
                )

        if total_executed == 0:
            issues.append(f"`{label}` executed 0 tests")

        status_parts: list[str] = []
        test_rc = att.get("test_rc", 0)
        sig = att.get("signal")
        if test_rc == 0 and finished_cleanly:
            status_parts.append("PASS")
        elif test_rc != 0:
            status_parts.append(f"EXIT={test_rc}")

        if sig:
            status_parts.append(f"SIGNAL {sig}")
            issues.append(f"`{label}` test process crashed by signal `{sig}` (exit code {test_rc})")
        elif test_rc is not None and (test_rc < 0 or test_rc >= 128):
            signum = abs(test_rc) if test_rc < 0 else test_rc - 128
            sig_name = f"SIG{signal.Signals(signum).name}" if signum in signal.Signals._value2member_map_ else f"SIG_{signum}"
            status_parts.append(sig_name)
            issues.append(f"`{label}` test process crashed by signal `{sig_name}` (exit code {test_rc})")

        if summary.get("abort_detected"):
            status_parts.append("SkASSERT")
            issues.append(f"`{label}` triggered SkASSERT/fatal assertion: {summary.get('crash_reason')}")

        if not finished_cleanly:
            status_parts.append("UNFINISHED")
            issues.append(f"`{label}` test suite did not finish cleanly (incomplete run or premature abort)")

        status_col = f"`{' / '.join(status_parts)}`"

        rows.append(
            f"| `{label}` | {git_col} | {tree_col} | {bin_col} | {ninja_part} | {batch_col} | {tests_col} | {status_col} |"
        )

    return issues, rows


def collect_run_data(path_str: str) -> dict[str, Any]:
    p = pathlib.Path(path_str)
    data: dict[str, Any] = {
        "attestation": None,
        "summary": None,
        "ref_spec": None,
    }
    if p.is_dir():
        att_path = p / "attestation.json"
        if att_path.exists():
            try:
                data["attestation"] = json.loads(att_path.read_text())
            except Exception:
                pass
        sum_path = p / "summary.json"
        if sum_path.exists():
            try:
                data["summary"] = json.loads(sum_path.read_text())
            except Exception:
                pass
    return data


def compare_runs(args: list[str]) -> None:
    runs: list[tuple[str, dict[str, Any]]] = []
    for a in args:
        label, path = a.split("=", 1)
        runs.append((label, collect_run_data(path)))

    base_label, base = runs[0]
    base_fails: Set[str] = set((base.get("summary") or {}).get("failures") or [])

    print(f"# Flutter Paragraph Engine Test Comparison (baseline = `{base_label}`)\n")
    print("| Ref | C++ Smoke Tests | Batch 1 (Layout) | Batch 2 (Editing) | Batch 3 (Fields) | Regressions | Fixed vs baseline |")
    print("| :--- | :---: | :---: | :---: | :---: | :---: | :---: |")

    for idx, (label, run) in enumerate(runs):
        sm = run.get("summary") or {}
        cpp_cnt = f"`+{sm.get('cpp_passed', 0)} -{sm.get('cpp_failed', 0)}`"
        b1_cnt = f"`+{sm.get('batch1_passed', 0)} -{sm.get('batch1_failed', 0)}`"
        b2_cnt = f"`+{sm.get('batch2_passed', 0)} -{sm.get('batch2_failed', 0)}`"
        b3_cnt = f"`+{sm.get('batch3_passed', 0)} -{sm.get('batch3_failed', 0)}`"

        fails: Set[str] = set(sm.get("failures") or [])
        if idx == 0:
            reg_str = "*(baseline)*"
            fix_str = "*(baseline)*"
        else:
            regs = fails - base_fails
            fixes = base_fails - fails
            reg_str = f"**{len(regs)}**" if regs else "0"
            fix_str = f"**+{len(fixes)}**" if fixes else "0"

        print(f"| `{label}` | {cpp_cnt} | {b1_cnt} | {b2_cnt} | {b3_cnt} | {reg_str} | {fix_str} |")
    print()

    issues, att_rows = _evaluate_attestation(runs)
    print("## Паспорт прогона (Run Attestation)\n")
    if issues:
        print(f"**Вердикт целостности (Integrity Check):** `WARNING / INVALID` ({len(issues)} issue(s))\n")
        for iss in issues:
            print(f"- **WARNING:** {iss}")
        print()
    else:
        print("**Вердикт целостности (Integrity Check):** `VALID (все пакеты тестов завершены штатно, бинарники проверены, подмены исходников нет)`\n")

    print(
        "| Ref | Git Spec / Commit | Source Tree SHA | Binary (`flutter_tester`) SHA / mtime | Ninja Build | Батчи | Тесты (+pass -fail) | Status / Signal |"
    )
    print("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |")
    for r in att_rows:
        print(r)
    print()

    for label, run in runs[1:]:
        sm = run.get("summary") or {}
        fails = set(sm.get("failures") or [])
        regressions = sorted(fails - base_fails)
        fixes = sorted(base_fails - fails)

        print(f"## `{label}` vs `{base_label}`\n")
        print(
            f"**Выполнено тестов: {sm.get('total_passed', 0) + sm.get('total_failed', 0)} | "
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
        print("Usage: parse_flutter_paragraph_tests.py compare <baseline>=<dir> <cand>=<dir> [...]")
        return 2

    cmd = argv[1]
    if cmd == "compare":
        compare_runs(argv[2:])
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main(argv=sys.argv))
