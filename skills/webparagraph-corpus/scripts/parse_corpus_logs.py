#!/usr/bin/env python3
"""Parse `felt test` (Part 1) and `flutter test` (Part 2) logs and compare runs against a baseline.

Usage:
  parse_corpus_logs.py summarize-felt <felt.log> [--json OUT.json]
  parse_corpus_logs.py summarize-flutter <flutter.log_or_dir> [--json OUT.json]
  parse_corpus_logs.py compare <baseline_label>=<run_dir> <label>=<run_dir> [...]
"""

import json
import pathlib
import re
import sys

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PROGRESS_RE = re.compile(
    r"^(?P<time>\d{2}:\d{2})\s+\+(?P<passed>\d+)(?:\s+~(?P<skipped>\d+))?(?:\s+-(?P<failed>\d+))?:\s+(?P<rest>.*)$"
)
SUITE_START_RE = re.compile(r"^\[(?P<suite>[A-Za-z0-9_.-]+)\] Running\.\.\.\s*$")
SUITE_END_RE = re.compile(
    r"^\[(?P<suite>[A-Za-z0-9_.-]+)\] (?P<status>All tests passed!|Some tests failed\.)\s*$"
)
BUNDLE_START_RE = re.compile(r"^Compiling test bundle (?P<bundle>[A-Za-z0-9_.-]+)\.\.\.\s*$")
BUNDLE_FAIL_RE = re.compile(r"^Failed to compile (?P<bundle>[A-Za-z0-9_.-]+)\.\s*$")
COMPILE_ERR_RE = re.compile(r"^ERROR: Failed to compile test (?P<file>\S+)\.")


def read_lines(path):
    with open(path, "r", errors="replace") as f:
        data = f.read()
    data = ANSI_RE.sub("", data)
    return [ln.rstrip() for ln in re.split(r"[\r\n]", data)]


def summarize_felt(path):
    lines = read_lines(path)
    suites = {}
    bundles = {}
    order = []
    current_bundle = None
    pending_failures = []
    last_progress = None
    pipeline_failures = []
    in_pipeline_failures = False
    timed_out = False

    for ln in lines:
        if not ln.strip():
            continue
        m = BUNDLE_START_RE.match(ln)
        if m:
            current_bundle = m.group("bundle")
            bundles.setdefault(current_bundle, {"compile_errors": [], "failed": False})
            continue
        m = COMPILE_ERR_RE.match(ln)
        if m and current_bundle:
            bundles[current_bundle]["compile_errors"].append(m.group("file"))
            continue
        m = BUNDLE_FAIL_RE.match(ln)
        if m:
            bundles.setdefault(m.group("bundle"), {"compile_errors": [], "failed": False})
            bundles[m.group("bundle")]["failed"] = True
            continue
        m = SUITE_START_RE.match(ln)
        if m:
            suite_name = m.group("suite")
            if suite_name not in suites:
                order.append(suite_name)
            suites[suite_name] = {
                "status": "unfinished",
                "failures": [],
                "passed": None,
                "skipped": None,
                "failed": None,
            }
            pending_failures = []
            last_progress = None
            continue
        m = SUITE_END_RE.match(ln)
        if m:
            name = m.group("suite")
            s = suites.setdefault(
                name,
                {"status": "unfinished", "failures": [], "passed": None, "skipped": None, "failed": None},
            )
            if name not in order:
                order.append(name)
            s["status"] = "pass" if m.group("status").startswith("All") else "fail"
            seen = set()
            s["failures"] = [x for x in pending_failures if not (x in seen or seen.add(x))]
            if last_progress:
                s["passed"] = int(last_progress["passed"])
                s["skipped"] = int(last_progress["skipped"] or 0)
                s["failed"] = int(last_progress["failed"] or 0)
            pending_failures = []
            last_progress = None
            continue
        m = PROGRESS_RE.match(ln)
        if m:
            last_progress = m.groupdict()
            rest = m.group("rest").strip()
            if rest.endswith("[E]"):
                pending_failures.append(rest[: -len("[E]")].strip())
            continue
        if ln.startswith("Pipeline experienced the following failures:"):
            in_pipeline_failures = True
            continue
        if in_pipeline_failures and ln.startswith('  "'):
            pipeline_failures.append(ln.strip())
            continue
        if ln.startswith("Test pipeline failed."):
            in_pipeline_failures = False
            continue

    for s in suites.values():
        if s["status"] == "unfinished":
            timed_out = True
            seen = set()
            s["failures"] = [x for x in pending_failures if not (x in seen or seen.add(x))]

    return {
        "kind": "felt",
        "log": str(path),
        "suites": {name: suites[name] for name in order},
        "bundles": bundles,
        "pipeline_failures": pipeline_failures,
        "unfinished": timed_out,
    }


def _normalize_suite_path(raw_path):
    if not raw_path:
        return ""
    idx = raw_path.find("test/")
    if idx != -1:
        return raw_path[idx:]
    return raw_path


def summarize_flutter_json_shards(shards_dir, log_paths=None):
    shards_dir = pathlib.Path(shards_dir)
    json_files = sorted(shards_dir.glob("shard_*.json"))
    passed = 0
    skipped = 0
    failed = 0
    failures = []
    seen_failures = set()
    canvaskit_served = []
    compile_errors = []
    unfinished_shards = []

    for jf in json_files:
        suites = {}
        tests = {}
        shard_done = False
        for raw_line in jf.read_text(errors="replace").splitlines():
            raw_line = raw_line.strip()
            if not raw_line.startswith("{"):
                continue
            try:
                ev = json.loads(raw_line)
            except Exception:
                continue
            etype = ev.get("type")
            if etype == "suite":
                s = ev.get("suite", {})
                suites[s.get("id")] = _normalize_suite_path(s.get("path", ""))
            elif etype == "testStart":
                t = ev.get("test", {})
                tests[t.get("id")] = {
                    "name": t.get("name", ""),
                    "suiteID": t.get("suiteID"),
                }
            elif etype == "testDone":
                tid = ev.get("testID")
                tinfo = tests.get(tid, {})
                tname = tinfo.get("name", "")
                spath = suites.get(tinfo.get("suiteID"), "")
                hidden = ev.get("hidden", False)
                is_skipped = ev.get("skipped", False)
                result = ev.get("result", "")

                if hidden:
                    if result in ("failure", "error"):
                        failed += 1
                        key = f"{spath}: {tname}" if spath else tname
                        if key not in seen_failures:
                            seen_failures.add(key)
                            failures.append(key)
                    continue

                if is_skipped:
                    skipped += 1
                elif result == "success":
                    passed += 1
                else:
                    failed += 1
                    key = f"{spath}: {tname}" if spath else tname
                    if key not in seen_failures:
                        seen_failures.add(key)
                        failures.append(key)
            elif etype == "done":
                shard_done = True

        if not shard_done:
            unfinished_shards.append(jf.name)

    if log_paths is None:
        log_paths = sorted(shards_dir.glob("shard_*.log"))
    for lp in log_paths:
        if not lp.exists():
            continue
        for ln in read_lines(lp):
            s = ln.strip()
            if "[CanvasKit served]:" in s:
                item = s.split("[CanvasKit served]:", 1)[1].strip()
                if item not in canvaskit_served:
                    canvaskit_served.append(item)
            elif s.startswith("Failed to load ") or s.startswith("Failed to compile"):
                if s not in compile_errors:
                    compile_errors.append(s)

    if unfinished_shards:
        status = "unfinished"
    elif failed > 0 or compile_errors:
        status = "fail"
    else:
        status = "pass"

    webparagraph_active = any("webparagraph/" in x for x in canvaskit_served)
    return {
        "kind": "flutter",
        "log": str(shards_dir),
        "status": status,
        "passed": passed,
        "skipped": skipped,
        "failed": failed,
        "failures": failures,
        "compile_errors": compile_errors,
        "unfinished_shards": unfinished_shards,
        "canvaskit_served": canvaskit_served,
        "webparagraph_active": webparagraph_active,
    }


def summarize_flutter(path):
    p = pathlib.Path(path)
    if p.is_dir():
        return summarize_flutter_json_shards(p)
    shards_dir = p.parent / "flutter_shards"
    if shards_dir.is_dir() and list(shards_dir.glob("shard_*.json")):
        return summarize_flutter_json_shards(shards_dir)

    lines = read_lines(path)
    canvaskit_served = []
    failures = []
    seen_failures = set()
    last_progress = None
    final_status = "unfinished"
    compile_errors = []

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if "[CanvasKit served]:" in s:
            item = s.split("[CanvasKit served]:", 1)[1].strip()
            if item not in canvaskit_served:
                canvaskit_served.append(item)
            continue
        if s.startswith("Failed to load ") or s.startswith("Error: "):
            if s not in compile_errors:
                compile_errors.append(s)
        m = PROGRESS_RE.match(s)
        if m:
            last_progress = m.groupdict()
            rest = m.group("rest").strip()
            if rest in ("All tests passed!", "Some tests failed."):
                final_status = "pass" if rest.startswith("All") else "fail"
                continue
            if rest.endswith("[E]"):
                test_name = rest[: -len("[E]")].strip()
                if test_name not in seen_failures:
                    seen_failures.add(test_name)
                    failures.append(test_name)

    passed = int(last_progress["passed"]) if last_progress else None
    skipped = int(last_progress["skipped"] or 0) if last_progress else None
    failed = int(last_progress["failed"] or 0) if last_progress else None
    webparagraph_active = any("webparagraph/" in x for x in canvaskit_served)

    return {
        "kind": "flutter",
        "log": str(path),
        "status": final_status,
        "passed": passed,
        "skipped": skipped,
        "failed": failed,
        "failures": failures,
        "compile_errors": compile_errors,
        "canvaskit_served": canvaskit_served,
        "webparagraph_active": webparagraph_active,
    }


def print_felt_summary(summary):
    print(f"# felt log: {summary['log']}")
    bad_bundles = {
        b: info for b, info in summary["bundles"].items() if info["failed"] or info["compile_errors"]
    }
    print(f"bundles compiled: {len(summary['bundles'])}, failed: {len(bad_bundles)}")
    for b, info in bad_bundles.items():
        print(f"  BUNDLE FAIL {b}: {len(info['compile_errors'])} file(s)")
        for f in info["compile_errors"]:
            print(f"    - {f}")
    n_pass = sum(1 for s in summary["suites"].values() if s["status"] == "pass")
    n_fail = sum(1 for s in summary["suites"].values() if s["status"] == "fail")
    n_unf = sum(1 for s in summary["suites"].values() if s["status"] == "unfinished")
    print(f"suites: {len(summary['suites'])} (pass {n_pass}, fail {n_fail}, unfinished {n_unf})")
    for name, s in summary["suites"].items():
        counts = ""
        if s["passed"] is not None:
            counts = f" (+{s['passed']} ~{s['skipped']} -{s['failed']})"
        print(f"  [{s['status'].upper():10s}] {name}{counts}")
        for f in s["failures"]:
            print(f"      [E] {f}")


def print_flutter_summary(summary):
    print(f"# flutter test log: {summary['log']}")
    counts = ""
    if summary["passed"] is not None:
        counts = f" (+{summary['passed']} ~{summary['skipped']} -{summary['failed']})"
    wp = "YES" if summary["webparagraph_active"] else "NO"
    print(f"  [{summary['status'].upper():10s}] packages/flutter{counts} (webparagraph_served={wp})")
    for err in summary.get("compile_errors", []):
        print(f"      [COMPILE] {err}")
    for f in summary["failures"]:
        print(f"      [E] {f}")


def collect_run_data(path_str):
    p = pathlib.Path(path_str)
    data = {
        "analyze_rc": None,
        "felt": None,
        "flutter": None,
    }
    if p.is_dir():
        status_file = p / "status.txt"
        if status_file.exists():
            for ln in status_file.read_text().splitlines():
                if ln.startswith("analyze="):
                    try:
                        data["analyze_rc"] = int(ln.split("=", 1)[1])
                    except ValueError:
                        data["analyze_rc"] = ln.split("=", 1)[1]
        felt_json = p / "felt_summary.json"
        if not felt_json.exists():
            felt_json = p / "summary.json"
        if felt_json.exists():
            data["felt"] = json.loads(felt_json.read_text())
        elif (p / "felt.log").exists():
            data["felt"] = summarize_felt(p / "felt.log")

        flutter_json = p / "flutter_summary.json"
        if flutter_json.exists():
            data["flutter"] = json.loads(flutter_json.read_text())
        elif (p / "flutter_shards").is_dir():
            data["flutter"] = summarize_flutter_json_shards(p / "flutter_shards")
        elif (p / "flutter.log").exists():
            data["flutter"] = summarize_flutter(p / "flutter.log")
    elif p.suffix == ".json":
        obj = json.loads(p.read_text())
        if obj.get("kind") == "flutter":
            data["flutter"] = obj
        else:
            data["felt"] = obj
    elif p.exists():
        text = p.read_text(errors="replace")
        if "Compiling test bundle" in text or "] Running..." in text:
            data["felt"] = summarize_felt(p)
        else:
            data["flutter"] = summarize_flutter(p)
    return data


def felt_failure_set(felt):
    if not felt:
        return set()
    out = set()
    for suite, s in felt.get("suites", {}).items():
        for f in s.get("failures", []):
            out.add((suite, f))
        if s.get("status") == "fail" and not s.get("failures"):
            out.add((suite, "<suite failed without [E] lines>"))
        if s.get("status") == "unfinished":
            out.add((suite, "<suite unfinished>"))
    for b, info in felt.get("bundles", {}).items():
        if info.get("failed"):
            out.add((f"bundle:{b}", "<compile failed>"))
        for f in info.get("compile_errors", []):
            out.add((f"bundle:{b}", f"compile error: {f}"))
    return out


def flutter_failure_set(fl):
    if not fl:
        return set()
    out = set()
    for f in fl.get("failures", []):
        out.add(("packages/flutter", f))
    if fl.get("status") == "fail" and not fl.get("failures"):
        out.add(("packages/flutter", "<failed without [E] lines>"))
    if fl.get("status") == "unfinished":
        for sh in fl.get("unfinished_shards", ["<unfinished>"]):
            out.add(("packages/flutter", f"<unfinished:{sh}>"))
    return out


def run_totals(run):
    passed = 0
    skipped = 0
    failed = 0
    felt = run.get("felt") or {}
    for s in felt.get("suites", {}).values():
        passed += s.get("passed") or 0
        skipped += s.get("skipped") or 0
        failed += s.get("failed") or 0
    fl = run.get("flutter") or {}
    passed += fl.get("passed") or 0
    skipped += fl.get("skipped") or 0
    failed += fl.get("failed") or 0
    return passed, skipped, failed


def format_counts(s):
    if not s or s.get("passed") is None:
        return "—"
    return f"`+{s['passed']} ~{s['skipped']} -{s['failed']}`"


def compare_runs(args):
    runs = []
    for a in args:
        label, path = a.split("=", 1)
        runs.append((label, collect_run_data(path)))

    base_label, base = runs[0]
    base_fails = felt_failure_set(base["felt"]) | flutter_failure_set(base["flutter"])

    print(f"# WebParagraph Corpus Report (baseline = `{base_label}`)\n")
    print("| Ref | `dart analyze` | Part 1a (`webparagraph/*`) | Part 1b (`test/ui/*`) | Part 2 (`packages/flutter`) | Regressions | Fixed vs baseline |")
    print("| :--- | :---: | :---: | :---: | :---: | :---: | :---: |")

    for idx, (label, run) in enumerate(runs):
        an = "—"
        if run["analyze_rc"] is not None:
            an = "PASS" if run["analyze_rc"] == 0 else f"FAIL ({run['analyze_rc']})"
        felt_suites = (run["felt"] or {}).get("suites", {})
        s_wp = felt_suites.get("chrome-dart2js-webparagraph-ui")
        s_ui = felt_suites.get("chrome-dart2js-webparagraph-ui-text")
        fl = run["flutter"]
        fails = felt_failure_set(run["felt"]) | flutter_failure_set(fl)
        if idx == 0:
            reg_str = "*(baseline)*"
            fix_str = "*(baseline)*"
        else:
            regs = fails - base_fails
            fixes = base_fails - fails
            reg_str = f"**{len(regs)}**" if regs else "0"
            fix_str = f"**+{len(fixes)}**" if fixes else "0"
        print(
            f"| `{label}` | {an} | {format_counts(s_wp)} | {format_counts(s_ui)} | {format_counts(fl)} | {reg_str} | {fix_str} |"
        )
    print()

    for label, run in runs[1:]:
        fails = felt_failure_set(run["felt"]) | flutter_failure_set(run["flutter"])
        regressions = sorted(fails - base_fails)
        fixes = sorted(base_fails - fails)
        passed, skipped, failed_cnt = run_totals(run)
        total_executed = passed + failed_cnt
        total_all = passed + skipped + failed_cnt

        print(f"## `{label}` vs `{base_label}`\n")
        print(
            f"**Пропущено тестов: {total_all} (выполнено: {total_executed}, skipped: {skipped}) | "
            f"Было фейлов: {len(base_fails)} | Починено: {len(fixes)} | "
            f"Поломано: {len(regressions)} | Стало фейлов: {len(fails)}**\n"
        )
        if regressions:
            print("### Regressions (fail here, pass on baseline)\n")
            for suite, f in regressions:
                print(f"- `{suite}` :: `{f}`")
            print()
        if fixes:
            print("### Fixed vs baseline (fail on baseline, pass here)\n")
            for suite, f in fixes:
                print(f"- `{suite}` :: `{f}`")
            print()

    print(f"## Baseline Failures (`{base_label}`: {len(base_fails)})\n")
    for suite, f in sorted(base_fails):
        print(f"- `{suite}` :: `{f}`")


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "summarize-felt":
        summary = summarize_felt(argv[2])
        print_felt_summary(summary)
        if "--json" in argv:
            out = argv[argv.index("--json") + 1]
            pathlib.Path(out).write_text(json.dumps(summary, indent=2))
        return 0
    if cmd == "summarize-flutter":
        summary = summarize_flutter(argv[2])
        print_flutter_summary(summary)
        if "--json" in argv:
            out = argv[argv.index("--json") + 1]
            pathlib.Path(out).write_text(json.dumps(summary, indent=2))
        return 0
    if cmd == "compare":
        compare_runs(argv[2:])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
