import argparse
import json
import os
import re
import subprocess
import sys

def g4_changes(path, status, cwd, limit=1):
  rc, out = subprocess.getstatusoutput(f"g4 changes -m {limit} -s {status} {path}")
  if rc != 0:
    return []
  res = []
  for line in out.strip().splitlines():
    m = re.match(r"Change (\d+) on (\d+/\d+/\d+) by ([^\s@]+)[@\s].*?'(.*?)'", line)
    if m:
      res.append({"cl": int(m.group(1)), "date": m.group(2),
                  "user": m.group(3), "title": m.group(4)})
  return res

def latest_roll(cwd):
  rolls = [r for r in g4_changes("//depot/google3/third_party/skia/HEAD", "pending", cwd, limit=100)
           if r["user"] == "skia-autoroll"]
  if not rolls:
    return None, 0
  same = [r for r in rolls if r["title"] == rolls[0]["title"]]
  return rolls[0], len(same)

def roll_title(cl, cwd):
  rc, out = subprocess.getstatusoutput(f"g4 describe -s {cl}")
  if rc != 0:
    return ""
  lines = out.strip().splitlines()
  if len(lines) > 1:
    return lines[1].strip()
  return ""

OVERALL_TEXT = {
    "SKIA_REGRESSION": "SKIA REGRESSION: a //third_party/skia or //chrome/skia target fails; investigate the roll range",
    "DEPENDENT_NOT_BROKEN_AT_HEAD": "DEPENDENT FAILURE NOT BROKEN AT HEAD: could be a Skia-caused regression or a flake; needs a human look",
    "BLOCKED_BY_HEAD": "BLOCKED BY DEPENDENTS BROKEN AT HEAD: not Skia's fault; wait for / nudge the fixes, or re-roll with SKIA_DEPENDENTS_ALREADY_FAILING=true",
    "INFRA_ONLY": "INFRA ONLY: aborted/timed-out dependents that pass at HEAD; the next attempt should clear",
    "IN_PROGRESS": "IN PROGRESS: no failures yet, TAP still running",
    "FLAKES_ONLY": "ONLY FLAKES in superseded runs; the latest runs are clean",
    "GREEN": "GREEN: no failing targets",
}

def main():
  ap = argparse.ArgumentParser(description="Triage google3 Skia roll")
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

  # Run the go triage
  gardener_path = "/usr/local/google/home/jlavrova/.gemini/jetski/scratch/skia_gardener"
  # Use go run so we don't have to build
  cmd = ["go", "run", ".", f"-triage-google3={roll['cl']}"]
  
  try:
      out = subprocess.check_output(cmd, cwd=gardener_path, stderr=subprocess.STDOUT)
  except subprocess.CalledProcessError as e:
      sys.exit(f"Go triage failed:\n{e.output.decode('utf-8')}")

  try:
      result_go = json.loads(out)
  except ValueError:
      sys.exit(f"Failed to parse Go triage output:\n{out.decode('utf-8')}")

  # result_go matches Google3RollTriage struct in go
  verdict = result_go.get("Verdict", "")
  overall = OVERALL_TEXT.get(verdict, verdict)

  # Adapt format for --json output
  runs = []
  for r in result_go.get("Runs", []):
      runs.append({
          "id": r.get("id"),
          "check": r.get("check"),
          "status": r.get("status"),
          "superseded": r.get("superseded", False),
          "failed": r.get("failed", 0)
      })

  verdicts = []
  for v in result_go.get("Targets", []):
      head = {
          "status": v.get("head_status", "UNKNOWN"),
          "cl": 0,
      }
      fixes = []
      for f in v.get("fixes", []):
          fixes.append({
              "cl": f.get("number"),
              "user": f.get("owner", ""),
              "title": f.get("subject", ""),
              "date": f.get("updated", "")
          })
          
      verdicts.append({
          "target": v.get("target"),
          "presubmit_status": v.get("presubmit_status"),
          "kind": v.get("kind"),
          "head": head,
          "failed_in_latest": v.get("failed_in_latest", False),
          "error": v.get("first_error"),
          "fixes": fixes
      })

  result = {
      "roll_cl": roll["cl"], 
      "title": roll.get("title", ""), 
      "attempts": attempts,
      "overall": overall, 
      "runs": runs, 
      "targets": verdicts,
      "autorepair_dir": result_go.get("DiagDir", "")
  }
  
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
      head_txt = h["status"] + (f" @ cl/{h['cl']}" if h.get("cl") else "")
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
            print(f"  - cl/{f['cl']} ({f['user']}): {f['title']}")
        else:
          print("- no pending CL touches it yet")
  print(f"\nDiagnostics: {result.get('autorepair_dir', '')}")
  return 0

if __name__ == "__main__":
  sys.exit(main())
