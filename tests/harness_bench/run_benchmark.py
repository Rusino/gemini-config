#!/usr/bin/env python3
"""Deterministic Frozen Bugs + Safe Decoys (TPR - FPR) harness benchmark runner.

Evaluates paired positive bug-escape fixtures from INCIDENTS.md ('frozen_bug')
and clean post-fix counterparts ('safe_decoy') across ruthless_audit,
audit_comments, and finding_ledger, computing True Positive Rate (TPR),
False Positive Rate (FPR), and net discovery score (TPR - FPR).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

BENCH_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BENCH_DIR.parents[1]
SCRIPTS_DIR = CONFIG_DIR / "scripts"
AUDIT_COMMENTS_DIR = CONFIG_DIR / "skills" / "code-comments" / "scripts"
RUTHLESS_AUDIT_DIR = CONFIG_DIR / "skills" / "ruthless-code-audit" / "scripts"

for _p in (SCRIPTS_DIR, AUDIT_COMMENTS_DIR, RUTHLESS_AUDIT_DIR):
  if str(_p) not in sys.path:
    sys.path.insert(0, str(_p))

import audit_comments
import finding_ledger
import ruthless_audit

DEFAULT_CASES_PATH = BENCH_DIR / "cases.json"
STRUCTURAL_TAGS = frozenset({
    "WHOLE_FILE_IFDEF",
    "TEMPLATE_NO_WHY",
    "FUNC_NO_CONTRACT",
    "TEST_NO_HEADER",
})


def _eval_ruthless_audit(case: dict) -> list[str]:
  """Runs scan_horrors_and_invariants on in-memory case files and returns hit category keys."""
  files_map: dict[str, str] = case.get("files") or {}
  with tempfile.TemporaryDirectory(prefix="harness_ruthless_") as tmp:
    root = Path(tmp)
    records = []
    for rel_path, content in sorted(files_map.items()):
      full_path = root / rel_path
      full_path.parent.mkdir(parents=True, exist_ok=True)
      full_path.write_text(content, encoding="utf-8")
      records.append((full_path, rel_path, content, content.splitlines()))
    horrors = ruthless_audit.scan_horrors_and_invariants(records)
  return sorted(key for key, items in horrors.items() if items)


def _eval_commit_msg(case: dict) -> list[str]:
  """Audits a commit message fixture for hygiene issues and scope disclaimers."""
  msg = str(case.get("commit_msg") or "")
  cats: set[str] = set()
  if audit_comments.audit_commit_message(msg):
    cats.add("commit_issue")
  if audit_comments.extract_commit_disclaimers(msg):
    cats.add("commit_disclaimer")
  return sorted(cats)


def _eval_audit_comments_cpp(case: dict) -> list[str]:
  """Audits C++/header/test fixtures for structural and comment block violations."""
  files_map: dict[str, str] = case.get("files") or {}
  is_new_file = bool(case.get("is_new_file", False))
  cats: set[str] = set()
  for rel_path, content in sorted(files_map.items()):
    lines = content.splitlines()
    added_lines = set(range(1, len(lines) + 1))
    issues = list(
        audit_comments.audit_cpp_structure(rel_path, lines, added_lines, is_new_file)
    )
    blocks = audit_comments.extract_comment_blocks(lines, line_filter=added_lines)
    issues.extend(
        audit_comments.audit_blocks(
            blocks,
            require_period=rel_path.endswith(".dart"),
            check_cl_hygiene=True,
        )
    )
    for _, msg in issues:
      prefix = msg.split(":", 1)[0].strip()
      cats.add(prefix if prefix in STRUCTURAL_TAGS else "comment_block_issue")
  return sorted(cats)


def _eval_finding_ledger(case: dict) -> list[str]:
  """Evaluates whether a candidate finding survives or is suppressed by finding_ledger."""
  entries = case.get("ledger_entries") or []
  cand = case.get("candidate_finding") or {}
  with tempfile.TemporaryDirectory(prefix="harness_ledger_") as tmp:
    old_env = os.environ.get("JETSKI_LEDGER_DIR")
    os.environ["JETSKI_LEDGER_DIR"] = tmp
    try:
      for item in entries:
        finding_ledger.add_entry(
            file_target=item["file"],
            symbol=item.get("symbol", ""),
            claim=item["claim"],
            reason=item.get("reason", "refuted in benchmark"),
            repo="harness_bench_repo",
            scope=item.get("scope", "repo"),
        )
      _, matched = finding_ledger.query_entries(
          repo="harness_bench_repo",
          files=[cand.get("file", "*")],
      )
    finally:
      if old_env is None:
        os.environ.pop("JETSKI_LEDGER_DIR", None)
      else:
        os.environ["JETSKI_LEDGER_DIR"] = old_env

  cand_sym = str(cand.get("symbol") or "").strip().lower()
  cand_claim = str(cand.get("claim") or "").strip().lower()
  for m in matched:
    if (
        str(m.get("symbol") or "").strip().lower() == cand_sym
        and str(m.get("claim") or "").strip().lower() == cand_claim
    ):
      return []
  return ["active_unrefuted_finding"]


EVALUATORS = {
    "ruthless_audit": _eval_ruthless_audit,
    "commit_msg": _eval_commit_msg,
    "audit_comments_cpp": _eval_audit_comments_cpp,
    "finding_ledger": _eval_finding_ledger,
}


def evaluate_case(case: dict) -> dict:
  """Evaluates a single frozen_bug or safe_decoy case and returns its verdict dict."""
  evaluator_name = str(case.get("evaluator") or "")
  if evaluator_name not in EVALUATORS:
    raise ValueError(f"Unknown evaluator '{evaluator_name}' in case {case.get('id')}")
  detected = EVALUATORS[evaluator_name](case)
  expected = sorted(str(x) for x in (case.get("expected_categories") or []))
  kind = str(case.get("kind") or "")
  if kind == "frozen_bug":
    passed = bool(expected) and set(expected).issubset(set(detected))
  elif kind == "safe_decoy":
    passed = len(detected) == 0
  else:
    raise ValueError(f"Unknown case kind '{kind}' in case {case.get('id')}")
  return {
      "id": case.get("id"),
      "kind": kind,
      "evaluator": evaluator_name,
      "expected_categories": expected,
      "detected_categories": detected,
      "passed": passed,
  }


def run_benchmark(cases_path: Path = DEFAULT_CASES_PATH) -> dict:
  """Runs all cases in cases_path and computes TPR, FPR, and net_score (TPR - FPR)."""
  payload = json.loads(cases_path.read_text(encoding="utf-8"))
  cases = payload.get("cases") or []
  results = [evaluate_case(c) for c in cases]

  bug_results = [r for r in results if r["kind"] == "frozen_bug"]
  decoy_results = [r for r in results if r["kind"] == "safe_decoy"]
  if not bug_results or not decoy_results:
    raise ValueError("Benchmark requires at least one frozen_bug and one safe_decoy case")

  tp = sum(1 for r in bug_results if r["passed"])
  fp = sum(1 for r in decoy_results if not r["passed"])
  tpr = round(tp / len(bug_results), 4)
  fpr = round(fp / len(decoy_results), 4)
  net_score = round(tpr - fpr, 4)

  return {
      "benchmark": payload.get("benchmark", "gemini-config-harness-tpr-fpr-v1"),
      "total_cases": len(results),
      "frozen_bugs": len(bug_results),
      "safe_decoys": len(decoy_results),
      "true_positives": tp,
      "false_positives": fp,
      "tpr": tpr,
      "fpr": fpr,
      "net_score": net_score,
      "results": results,
  }


def main(argv: list[str] | None = None) -> int:
  """CLI entry point for running the Frozen Bugs + Safe Decoys benchmark."""
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
      "--cases",
      type=Path,
      default=DEFAULT_CASES_PATH,
      help="Path to benchmark cases.json",
  )
  parser.add_argument("--json", action="store_true", help="Print JSON summary")
  parser.add_argument(
      "--min-net-score",
      type=float,
      default=1.0,
      help="Minimum required (TPR - FPR) score before failing (default: 1.0)",
  )
  args = parser.parse_args(argv)

  summary = run_benchmark(args.cases)
  if args.json:
    print(json.dumps(summary, indent=2))
  else:
    print(
        f"Harness Benchmark ({summary['benchmark']}): "
        f"TPR={summary['tpr']:.2f} ({summary['true_positives']}/{summary['frozen_bugs']}), "
        f"FPR={summary['fpr']:.2f} ({summary['false_positives']}/{summary['safe_decoys']}), "
        f"TPR - FPR = {summary['net_score']:.2f}"
    )
    for r in summary["results"]:
      status = "PASS" if r["passed"] else "FAIL"
      print(
          f"  [{status}] {r['id']} ({r['kind']}, {r['evaluator']}): "
          f"expected={r['expected_categories']} detected={r['detected_categories']}"
      )

  return 0 if summary["net_score"] >= args.min_net_score else 1


if __name__ == "__main__":
  sys.exit(main())
