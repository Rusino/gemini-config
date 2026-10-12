#!/usr/bin/env python3
"""Triage the google3 Skia autoroll: which check blocks it and whose fault it is.

Must run with cwd inside a google3 CitC workspace (g4 needs it). Uses only
tools that already exist on every cloudtop: g4 and the autorepair CLI.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

AUTOREPAIR = "/google/bin/releases/gemini-agents-auto-repair-cli/autorepair"
ROLL_PATH = "//depot/google3/third_party/skia/HEAD/..."
METADATA = "third_party/skia/METADATA"
SKIA_PREFIXES = ("//third_party/skia/", "//chrome/skia/")
# Mirrors passing_statuses in //chrome/skia/autoroller/autoroller_lib.py so the
# verdict here agrees with what the roller itself would consider "already failing"
PASSING = {"PASSED", "FLAKY", "SKIPPED", "EXECUTION_STATUS_PASSED",
           "EXECUTION_STATUS_FLAKY", "EXECUTION_STATUS_SKIPPED"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def run(cmd, cwd=None):
  p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
  return p.returncode, p.stdout + p.stderr


def g4_changes(path, status, cwd, limit=20):
  rc, out = run(["g4", "changes", "-s", status, "-m", str(limit), path], cwd)
  rows = []
  for line in out.splitlines():
    m = re.match(r"Change (\d+) on (\S+) by (\S+?)@\S+ .*?'(.*)'", line)
    if m:
      rows.append({"cl": int(m.group(1)), "date": m.group(2),
                   "user": m.group(3), "title": m.group(4).strip()})
  return rows


def latest_roll(cwd):
  rolls = [r for r in g4_changes(ROLL_PATH, "pending", cwd)
           if r["user"] == "skia-autoroll"]
  if not rolls:
    return None, 0
  # The roller recreates an identical CL every ~20 min while stuck, so the
  # number of pending twins is the number of failed attempts on this range
  same = [r for r in rolls if r["title"] == rolls[0]["title"]]
  return rolls[0], len(same)


def investigate(cl):
  rc, out = run([AUTOREPAIR, "investigate", f"--cl={cl}"])
  m = re.search(r"Directory: `file://([^`]+)`", out)
  if not m:
    sys.exit(f"autorepair investigate gave no output directory:\n{out[-2000:]}")
  return m.group(1).rstrip("/")


def load(path, default=None):
  try:
    with open(path) as f:
      return json.load(f)
  except (OSError, json.JSONDecodeError):
    return default


def roll_title(cl, cwd):
  rc, out = run(["g4", "describe", "-s", str(cl)], cwd)
  for line in out.splitlines()[1:]:
    if line.strip():
      return line.strip()
  return ""


def checks_from_metadata(cwd):
  """Project sets of each check_tests block in third_party/skia/METADATA.

  The Critique finding that names the TAP run is truncated for the big
  dependents check, so TAP runs are matched to checks by their project list
  """
  checks, cur_id, cur_projects, depth, in_block = [], None, set(), 0, False
  try:
    with open(os.path.join(cwd, METADATA)) as f:
      lines = f.read().splitlines()
  except OSError:
    return checks
  for line in lines:
    s = line.split("#", 1)[0].strip()
    if s.startswith("check_tests") and s.endswith("{"):
      in_block, depth, cur_id, cur_projects = True, 1, None, set()
      continue
    if not in_block:
      continue
    depth += s.count("{") - s.count("}")
    m = re.match(r'(id|project):\s*"([^"]+)"', s)
    if m and m.group(1) == "id":
      cur_id = m.group(2)
    elif m:
      cur_projects.add(m.group(2))
    if depth <= 0:
      in_block = False
      if cur_id:
        checks.append((cur_id, frozenset(cur_projects)))
  return checks


def check_name(projects, checks):
  ps = set(projects)
  best = max(checks, key=lambda c: len(c[1] & ps), default=None)
  return best[0] if best and best[1] & ps else "?"



def head_status(target):
  rc, out = run([AUTOREPAIR, "target-status", f"--target={target}"])
  try:
    out_file = json.loads(out.strip().splitlines()[-1])["outputFile"]
    t = load(out_file, {})["targetStatusResult"]["targets"][0]["headStatus"]
    return {"status": t.get("status", "UNKNOWN").replace("EXECUTION_STATUS_", ""),
            "cl": t.get("changelist"), "sponge": t.get("spongeUrl")}
  except (KeyError, IndexError, ValueError, TypeError):
    return {"status": "UNKNOWN", "cl": None, "sponge": None}


def first_error_line(root, sponge_id):
  tdir = os.path.join(root, "sponge", sponge_id, "targets")
  if not os.path.isdir(tdir):
    return None
  first = None
  for sub in sorted(os.listdir(tdir)):
    try:
      with open(os.path.join(tdir, sub, "log.txt")) as f:
        for line in f:
          line = ANSI.sub("", line).strip()
          if line and first is None:
            first = line
          if "error:" in line or "ERROR" in line or "DISTRIBUTOR(" in line:
            return line
    except OSError:
      continue
  return first


def pending_fixes(target, error_line, cwd):
  pkg = target.split(":")[0].replace("//", "//depot/google3/")
  paths = [pkg + "/..."]
  m = re.match(r"([\w/.+-]+\.(?:cc|h|java|py|go|ts|kt))[:(]", error_line or "")
  if m:
    paths.insert(0, "//depot/google3/" + m.group(1))
  seen, fixes = set(), []
  cutoff = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y/%m/%d")
  for p in paths:
    for r in g4_changes(p, "pending", cwd, limit=10):
      if r["cl"] in seen or r["user"] == "skia-autoroll" or r["date"] < cutoff:
        continue
      seen.add(r["cl"])
      fixes.append(r)
  return fixes


INFRA_STATUSES = {"ABORTED_BY_TOOL", "ABORTED", "CANCELLED", "CANCELLED_BY_USER"}
INFRA_MARKERS = ("DISTRIBUTOR(TIMEOUT)", "DISTRIBUTOR(INFRA", "BuildBundle timed out")
HEAD_UNKNOWN = {"UNKNOWN", "PENDING", "RUNNING", "SCHEDULED", "NOT_RUN", ""}


def classify(target, status, error, runs_failed, latest_run_ids, head):
  if target.startswith(SKIA_PREFIXES):
    return "SKIA_REGRESSION"
  head_known = head["status"] not in HEAD_UNKNOWN
  if head_known and head["status"] not in PASSING:
    return "BROKEN_AT_HEAD"
  if status in INFRA_STATUSES or any(m in (error or "") for m in INFRA_MARKERS):
    return "INFRA"
  if not any(r in latest_run_ids for r in runs_failed):
    return "LIKELY_FLAKY"
  return "POSSIBLE_REGRESSION" if head_known else "UNKNOWN_HEAD"


def main():
  ap = argparse.ArgumentParser(description=__doc__)
  ap.add_argument("--cl", type=int, help="roll CL (default: newest pending roll)")
  ap.add_argument("--workspace", default=os.getcwd(),
                  help="google3 CitC workspace dir for g4 (default: cwd)")
  ap.add_argument("--json", action="store_true", help="machine-readable output")
  args = ap.parse_args()
  cwd = args.workspace

  attempts = 0
  if args.cl:
    roll = {"cl": args.cl, "title": roll_title(args.cl, cwd), "date": ""}
  else:
    roll, attempts = latest_roll(cwd)
    if not roll:
      print("No pending Skia roll CL; nothing to triage.")
      return 0

  d = investigate(roll["cl"])
  root = os.path.dirname(os.path.dirname(d))
  summary = load(os.path.join(d, "summary.json"), {})
  checks = checks_from_metadata(cwd)

  runs, failing = [], {}
  for r in summary.get("tap_presubmit", {}).get("runs", []):
    rs = load(os.path.join(d, "tap_presubmit", str(r["index"]), "summary.json"), r)
    superseded = "cancelled in favor" in (rs.get("setup_error") or "")
    run_info = {"id": rs["id"], "check": check_name(rs.get("projects", []), checks),
                "status": rs.get("status"), "superseded": superseded,
                "failed": len(rs.get("failed_targets", []))}
    runs.append(run_info)
    for ft in rs.get("failed_targets", []):
      e = failing.setdefault(ft["target"], {"status": ft.get("status"),
                                            "sponge": ft.get("sponge_id"),
                                            "runs": []})
      e["runs"].append(rs["id"])

  # Latest run per check: a superseded run only tells us about flakiness
  latest = {}
  for r in runs:
    if not r["superseded"]:
      latest[r["check"]] = r["id"]
  latest_ids = set(latest.values())

  verdicts = []
  for target, e in sorted(failing.items()):
    head = head_status(target)
    error = first_error_line(root, e["sponge"]) if e["sponge"] else None
    kind = classify(target, e["status"], error, e["runs"], latest_ids, head)
    v = {"target": target, "presubmit_status": e["status"], "kind": kind,
         "head": head, "failed_in_latest": any(r in latest_ids for r in e["runs"]),
         "error": error, "fixes": []}
    if kind == "BROKEN_AT_HEAD":
      v["fixes"] = pending_fixes(target, error, cwd)
    verdicts.append(v)

  kinds = {v["kind"] for v in verdicts if v["failed_in_latest"]}
  pending_runs = [r for r in runs if r["status"] in ("PENDING", "RUNNING")]
  if "SKIA_REGRESSION" in kinds:
    overall = "SKIA REGRESSION: a //third_party/skia or //chrome/skia target fails; investigate the roll range"
  elif "POSSIBLE_REGRESSION" in kinds or "UNKNOWN_HEAD" in kinds:
    overall = "DEPENDENT FAILURE NOT BROKEN AT HEAD: could be a Skia-caused regression or a flake; needs a human look"
  elif "BROKEN_AT_HEAD" in kinds:
    overall = "BLOCKED BY DEPENDENTS BROKEN AT HEAD: not Skia's fault; wait for / nudge the fixes, or re-roll with SKIA_DEPENDENTS_ALREADY_FAILING=true"
  elif "INFRA" in kinds:
    overall = "INFRA ONLY: aborted/timed-out dependents that pass at HEAD; the next attempt should clear"
  elif pending_runs:
    overall = "IN PROGRESS: no failures yet, TAP still running"
  elif verdicts:
    overall = "ONLY FLAKES in superseded runs; the latest runs are clean"
  else:
    overall = "GREEN: no failing targets"

  result = {"roll_cl": roll["cl"], "title": roll.get("title"), "attempts": attempts,
            "overall": overall, "runs": runs, "targets": verdicts,
            "autorepair_dir": d}
  if args.json:
    print(json.dumps(result, indent=1))
    return 0

  print(f"# Skia roll triage: cl/{roll['cl']} {roll.get('title', '')}")
  if attempts:
    print(f"Pending identical attempts: {attempts}")
  print(f"\n**Verdict: {overall}**\n")
  print("| Check | TAP run | Status | Failed | Note |\n|---|---|---|---|---|")
  for r in runs:
    note = "superseded" if r["superseded"] else ""
    print(f"| {r['check']} | `{r['id']}` | {r['status']} | {r['failed']} | {note} |")
  if verdicts:
    print("\n| Target | Presubmit | HEAD | Kind |\n|---|---|---|---|")
    for v in verdicts:
      h = v["head"]
      head_txt = h["status"] + (f" @ cl/{h['cl']}" if h["cl"] else "")
      print(f"| `{v['target']}` | {v['presubmit_status']} | {head_txt} | {v['kind']} |")
    for v in verdicts:
      if v["kind"] == "LIKELY_FLAKY":
        continue
      print(f"\n### `{v['target']}` — {v['kind']}")
      if v["error"]:
        print(f"- first log line: `{v['error']}`")
      if v["kind"] == "BROKEN_AT_HEAD":
        if v["fixes"]:
          print("- pending CLs touching it:")
          for f in v["fixes"]:
            print(f"  - cl/{f['cl']} ({f['user']}, {f['date']}): {f['title']}")
        else:
          print("- no pending CL touches it yet")
  print(f"\nDiagnostics: {d}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
