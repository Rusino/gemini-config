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
    current_suite = None
    pending_failures = []
    last_progress = None
    pipeline_failures = []
    in_pipeline_failures = False
    timed_out = False
    webparagraph_active = False

    for ln in lines:
        s_ln = ln.strip()
        if not s_ln:
            continue
        if "CanvasKit (Web Paragraph)" in s_ln or "WebParagraph: true" in s_ln:
            webparagraph_active = True
        m = BUNDLE_START_RE.match(s_ln)
        if m:
            current_bundle = m.group("bundle")
            bundles.setdefault(
                current_bundle,
                {"compile_errors": [], "compiled_files": [], "failed": False},
            )
            continue
        if s_ln.startswith("Completed compilation of "):
            current_bundle = None
            continue
        m = COMPILE_ERR_RE.match(s_ln)
        if m and current_bundle:
            bundles[current_bundle]["compile_errors"].append(m.group("file"))
            continue
        m = BUNDLE_FAIL_RE.match(s_ln)
        if m:
            bname = m.group("bundle")
            bundles.setdefault(
                bname,
                {"compile_errors": [], "compiled_files": [], "failed": False},
            )
            bundles[bname]["failed"] = True
            current_bundle = None
            continue
        if current_bundle and s_ln.endswith("_test.dart") and " " not in s_ln:
            if s_ln not in bundles[current_bundle]["compiled_files"]:
                bundles[current_bundle]["compiled_files"].append(s_ln)
            continue
        m = SUITE_START_RE.match(s_ln)
        if m:
            current_bundle = None
            suite_name = m.group("suite")
            current_suite = suite_name
            if suite_name not in suites:
                order.append(suite_name)
            suites[suite_name] = {
                "status": "unfinished",
                "failures": [],
                "executed_files": [],
                "passed": None,
                "skipped": None,
                "failed": None,
            }
            pending_failures = []
            last_progress = None
            continue
        m = SUITE_END_RE.match(s_ln)
        if m:
            name = m.group("suite")
            s = suites.setdefault(
                name,
                {
                    "status": "unfinished",
                    "failures": [],
                    "executed_files": [],
                    "passed": None,
                    "skipped": None,
                    "failed": None,
                },
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
            current_suite = None
            continue
        m = PROGRESS_RE.match(s_ln)
        if m:
            last_progress = m.groupdict()
            rest = m.group("rest").strip()
            if current_suite and current_suite in suites:
                fm = re.match(r"^(?:loading\s+)?(?:\S+/)?([A-Za-z0-9_]+_test\.dart)(?::|\s|$)", rest)
                if fm:
                    fname = fm.group(1)
                    if fname not in suites[current_suite]["executed_files"]:
                        suites[current_suite]["executed_files"].append(fname)
            if rest.endswith("[E]"):
                pending_failures.append(rest[: -len("[E]")].strip())
            continue
        if s_ln.startswith("Pipeline experienced the following failures:"):
            in_pipeline_failures = True
            continue
        if in_pipeline_failures and ln.startswith('  "'):
            pipeline_failures.append(s_ln)
            continue
        if s_ln.startswith("Test pipeline failed."):
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
        "webparagraph_active": webparagraph_active,
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
    all_suites = set()
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
                norm_p = _normalize_suite_path(s.get("path", ""))
                suites[s.get("id")] = norm_p
                if norm_p:
                    all_suites.add(norm_p)
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
        "suites_executed": len(all_suites),
        "shards_total": len(json_files),
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
    executed_suites = set()
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
            fm = re.search(r"(test/[A-Za-z0-9_./-]+_test\.dart)(?::|\s|$)", rest)
            if fm:
                executed_suites.add(fm.group(1))
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
        "suites_executed": len(executed_suites),
        "shards_total": 1,
        "unfinished_shards": [] if final_status in ("pass", "fail") else ["flutter.log"],
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
        n_files = len(s.get("executed_files") or [])
        files_str = f" [{n_files} files]" if n_files else ""
        print(f"  [{s['status'].upper():10s}] {name}{counts}{files_str}")
        for f in s["failures"]:
            print(f"      [E] {f}")


def print_flutter_summary(summary):
    print(f"# flutter test log: {summary['log']}")
    counts = ""
    if summary["passed"] is not None:
        counts = f" (+{summary['passed']} ~{summary['skipped']} -{summary['failed']})"
    wp = "YES" if summary["webparagraph_active"] else "NO"
    n_suites = summary.get("suites_executed")
    suites_str = f", suites={n_suites}" if n_suites is not None else ""
    print(
        f"  [{summary['status'].upper():10s}] packages/flutter{counts} "
        f"(webparagraph_served={wp}{suites_str})"
    )
    for err in summary.get("compile_errors", []):
        print(f"      [COMPILE] {err}")
    for f in summary["failures"]:
        print(f"      [E] {f}")


def _parse_ninja_log(ninja_log_path):
    if not ninja_log_path.exists():
        return None
    text = ninja_log_path.read_text(errors="replace")
    if "no work to do" in text:
        return "0/0 (no-op)"
    steps = re.findall(r"\[(\d+)/(\d+)\]", text)
    if steps:
        last_done, total = steps[-1]
        return f"{last_done}/{total}"
    return "ran"


def collect_run_data(path_str):
    p = pathlib.Path(path_str)
    data = {
        "ref_spec": None,
        "analyze_rc": None,
        "felt_rc": None,
        "flutter_rc": None,
        "ninja_actions": None,
        "attestation": {},
        "felt": None,
        "flutter": None,
    }
    if p.is_dir():
        att_file = p / "attestation.json"
        if att_file.exists():
            try:
                data["attestation"] = json.loads(att_file.read_text())
            except Exception:
                data["attestation"] = {}

        status_file = p / "status.txt"
        if status_file.exists():
            for ln in status_file.read_text().splitlines():
                if ln.startswith("ref="):
                    data["ref_spec"] = ln.split("=", 1)[1].strip()
                elif ln.startswith("analyze="):
                    try:
                        data["analyze_rc"] = int(ln.split("=", 1)[1])
                    except ValueError:
                        data["analyze_rc"] = ln.split("=", 1)[1]
                elif ln.startswith("felt="):
                    try:
                        data["felt_rc"] = int(ln.split("=", 1)[1])
                    except ValueError:
                        pass
                elif ln.startswith("flutter="):
                    try:
                        data["flutter_rc"] = int(ln.split("=", 1)[1])
                    except ValueError:
                        pass

        data["ninja_actions"] = _parse_ninja_log(p / "ninja_web_sdk.log")

        felt_log = p / "felt.log"
        felt_json = p / "felt_summary.json"
        if not felt_json.exists():
            felt_json = p / "summary.json"
        if felt_json.exists():
            data["felt"] = json.loads(felt_json.read_text())
            # Backfill file-level attestation fields when reading summaries produced by older runs
            if felt_log.exists() and "webparagraph_active" not in data["felt"]:
                data["felt"] = summarize_felt(felt_log)
        elif felt_log.exists():
            data["felt"] = summarize_felt(felt_log)

        flutter_json = p / "flutter_summary.json"
        if flutter_json.exists():
            data["flutter"] = json.loads(flutter_json.read_text())
            if "suites_executed" not in data["flutter"]:
                if (p / "flutter_shards").is_dir():
                    data["flutter"] = summarize_flutter_json_shards(p / "flutter_shards")
                elif (p / "flutter.log").exists():
                    data["flutter"] = summarize_flutter(p / "flutter.log")
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


def _evaluate_attestation(runs):
    issues = []
    rows = []
    seen_tree_hashes = {}
    seen_dill_hashes = {}

    for label, run in runs:
        att = run.get("attestation") or {}
        felt = run.get("felt") or {}
        fl = run.get("flutter") or {}

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

        tree_sha = att.get("web_ui_tree_sha256")
        tree_col = f"`{tree_sha}`" if tree_sha else "—"
        if tree_sha:
            if tree_sha in seen_tree_hashes:
                prev_label = seen_tree_hashes[tree_sha]
                issues.append(
                    f"`{label}` and `{prev_label}` have identical `web_ui` source tree hash (`{tree_sha}`)"
                )
            else:
                seen_tree_hashes[tree_sha] = label

        if felt:
            suites = felt.get("suites") or {}
            s_wp = suites.get("chrome-dart2js-webparagraph-ui") or {}
            s_ui = suites.get("chrome-dart2js-webparagraph-ui-text") or {}
            wp_exec = len(s_wp.get("executed_files") or [])
            ui_exec = len(s_ui.get("executed_files") or [])
            wp_exp = att.get("part1_expected_wp_files") or len(
                (felt.get("bundles") or {}).get("dart2js-canvaskit-webparagraph", {}).get("compiled_files")
                or []
            )
            ui_exp = att.get("part1_expected_ui_files") or len(
                (felt.get("bundles") or {}).get("dart2js-canvaskit-ui", {}).get("compiled_files") or []
            )
            n_suites_done = sum(1 for s in suites.values() if s.get("status") in ("pass", "fail"))
            n_suites_unf = sum(1 for s in suites.values() if s.get("status") == "unfinished")
            p1_compile_errs = sum(
                len(b.get("compile_errors") or []) + (1 if b.get("failed") else 0)
                for b in (felt.get("bundles") or {}).values()
            )
            p1_col = f"`{wp_exec}/{wp_exp}` wp + `{ui_exec}/{ui_exp}` ui (`{n_suites_done}/{len(suites)}` suites)"
            if n_suites_unf > 0 or felt.get("unfinished"):
                issues.append(f"`{label}` Part 1 has `{n_suites_unf}` unfinished suite(s)")
            if p1_compile_errs > 0:
                issues.append(f"`{label}` Part 1 has `{p1_compile_errs}` bundle compile error(s)")
            if wp_exp and wp_exec < wp_exp:
                issues.append(f"`{label}` Part 1a executed `{wp_exec}/{wp_exp}` files")
            if ui_exp and ui_exec < ui_exp:
                issues.append(f"`{label}` Part 1b executed `{ui_exec}/{ui_exp}` files")
            if not felt.get("webparagraph_active", True):
                issues.append(f"`{label}` Part 1 log missing `CanvasKit (Web Paragraph)` marker")
        else:
            p1_col = "—"
            p1_compile_errs = 0
            n_suites_unf = 0

        ninja_act = att.get("part2_ninja_actions") or run.get("ninja_actions") or "—"
        dill_sha = att.get("part2_ddc_outline_sha256")
        if dill_sha:
            build_col = f"`{ninja_act}` (`{dill_sha}`)"
            if tree_sha and dill_sha in seen_dill_hashes:
                prev_label, prev_tree = seen_dill_hashes[dill_sha]
                if prev_tree and prev_tree != tree_sha:
                    issues.append(
                        f"`{label}` has a different `web_ui` tree than `{prev_label}` "
                        f"but identical `ddc_outline.dill` hash (`{dill_sha}`)"
                    )
            seen_dill_hashes[dill_sha] = (label, tree_sha)
        else:
            build_col = f"`{ninja_act}`"

        if fl:
            fl_exec = fl.get("suites_executed")
            fl_exp = att.get("part2_expected_chrome_suites")
            shards_total = fl.get("shards_total") or 1
            unf_shards = fl.get("unfinished_shards") or []
            shards_done = shards_total - len(unf_shards)
            fl_compile_errs = len(fl.get("compile_errors") or [])
            if fl_exp:
                p2_col = f"`{fl_exec}/{fl_exp}` suites (`{shards_done}/{shards_total}` shards)"
            else:
                p2_col = f"`{fl_exec}` suites (`{shards_done}/{shards_total}` shards)"
            if unf_shards or fl.get("status") == "unfinished":
                issues.append(f"`{label}` Part 2 has `{len(unf_shards)}` unfinished shard(s)")
            if fl_compile_errs > 0:
                issues.append(f"`{label}` Part 2 has `{fl_compile_errs}` compile/load error(s)")
            if fl_exp and fl_exec is not None and fl_exec < fl_exp:
                issues.append(f"`{label}` Part 2 executed `{fl_exec}/{fl_exp}` expected Chrome suites")
            if not fl.get("webparagraph_active"):
                issues.append(
                    f"`{label}` Part 2 did NOT serve `webparagraph/canvaskit.js` (`webparagraph_active=False`)"
                )
        else:
            p2_col = "—"
            fl_compile_errs = 0
            unf_shards = []

        mode = att.get("corpus_mode")
        if not mode:
            if (fl and (fl.get("suites_executed") or 0) > 50) or (
                felt
                and len(
                    (felt.get("bundles") or {})
                    .get("dart2js-canvaskit-ui", {})
                    .get("compiled_files")
                    or []
                )
                > 10
            ):
                mode = "full"
            else:
                mode = "targeted"
        ftf = att.get("force_test_fonts")
        mode_col = f"`{mode.upper()}`" + ("" if ftf is None else (f" (`testFonts={'on' if ftf else 'off'}`)"))

        wp_p1 = "YES" if (felt and felt.get("webparagraph_active")) else ("—" if not felt else "**NO**")
        wp_p2 = "YES" if (fl and fl.get("webparagraph_active")) else ("—" if not fl else "**NO**")
        wp_col = f"P1:{wp_p1} / P2:{wp_p2}"

        total_compile_errs = p1_compile_errs + fl_compile_errs
        total_unf = n_suites_unf + len(unf_shards)
        err_col = f"`{total_compile_errs}` compile / `{total_unf}` unfinished"

        rows.append(
            f"| `{label}` | {git_col} | {tree_col} | {mode_col} | {p1_col} | {build_col} | {p2_col} | {wp_col} | {err_col} |"
        )

    return issues, rows


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

    issues, att_rows = _evaluate_attestation(runs)
    print("## Паспорт прогона (Run Attestation)\n")
    if issues:
        print(f"**Вердикт целостности (Integrity Check):** `WARNING / INVALID` ({len(issues)} issue(s))\n")
        for iss in issues:
            print(f"- **WARNING:** {iss}")
        print()
    else:
        print("**Вердикт целостности (Integrity Check):** `VALID (все сьюты завершены, WebParagraph активен, обрывов и ошибок компиляции нет)`\n")

    print(
        "| Ref | Git Spec / Commit | `web_ui` Tree SHA | Режим | Part 1 (`felt`) файлы/сьюты | Part 2 Build (`ninja` / `ddc_outline`) | Part 2 (`flutter`) сьюты/шарды | `WebParagraph` | Компиляция / обрывы |"
    )
    print("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
    for r in att_rows:
        print(r)
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
