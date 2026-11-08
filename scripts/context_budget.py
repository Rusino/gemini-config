#!/usr/bin/env python3
"""Measures the context cost of agent rules and skills.

Reports always-on rule bytes, conditional rules, weak descriptions, required phrases
missing from the always-on set (or from all rules for scope "any"), and text duplicated across rule and SKILL.md files.

Usage: context_budget.py [--root DIR] [--max-always-on BYTES] [--json]
Exit code 1 when the always-on total exceeds the limit or a required phrase is missing.
"""

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".gemini" / "config"
RULE_DIRS = ("rules", "plugins/keeper/rules")
MUST_KEEP = "scripts/context_budget_must_keep.json"
SHINGLE = 8
# Jetski demotes the largest always-on rules to pointers above this many tokens (docs: rules.md)
JETSKI_RULES_BUDGET_TOKENS = 20000
WEAK_DESC = re.compile(r"^\s*\"?Rule for ", re.I)


def parse_rule(path):
  text = path.read_text(encoding="utf-8")
  trigger, desc = "always_on", ""
  m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
  if m:
    for line in m.group(1).splitlines():
      key, _, val = line.partition(":")
      if key.strip() == "trigger":
        trigger = val.strip()
      elif key.strip() == "description":
        desc = val.strip()
  return {"file": str(path), "bytes": len(text.encode()), "trigger": trigger,
          "description": desc, "text": text}


def shingles(text):
  words = re.findall(r"[a-z0-9_./-]+", text.lower())
  return {" ".join(words[i:i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
  # Our own target, well under the Jetski cap: less always-on text keeps weaker models focused
  ap.add_argument("--max-always-on", type=int, default=20000)
  ap.add_argument("--min-shared", type=int, default=12,
                  help="report file pairs sharing at least this many 8-word shingles")
  ap.add_argument("--json", action="store_true")
  args = ap.parse_args()
  root = args.root

  rules = [parse_rule(p) for d in RULE_DIRS for p in sorted((root / d).glob("*.md"))]
  always = [r for r in rules if r["trigger"] == "always_on"]
  always_bytes = sum(r["bytes"] for r in always)
  weak = [r["file"] for r in rules if not r["description"] or WEAK_DESC.match(r["description"])]

  missing = []
  must_path = root / MUST_KEEP
  if must_path.exists():
    always_text = "\n".join(r["text"] for r in always)
    all_text = "\n".join(r["text"] for r in rules)
    for item in json.loads(must_path.read_text(encoding="utf-8")):
      # "any" items may live in a conditional rule; the rest must be visible every turn
      text = all_text if item.get("scope") == "any" else always_text
      if not re.search(item["regex"], text, re.I | re.S):
        missing.append(item["name"])

  docs = {r["file"]: shingles(r["text"]) for r in rules}
  for p in sorted((root / "skills").glob("*/SKILL.md")):
    docs[str(p)] = shingles(p.read_text(encoding="utf-8"))
  names = sorted(docs)
  dups = []
  for i, a in enumerate(names):
    for b in names[i + 1:]:
      n = len(docs[a] & docs[b])
      if n >= args.min_shared:
        dups.append((n, a, b))
  dups.sort(reverse=True)

  skills_bytes = sum(p.stat().st_size for p in (root / "skills").glob("*/SKILL.md"))
  rel = lambda f: str(Path(f).relative_to(root))
  report = {
      "always_on_bytes": always_bytes,
      "always_on_limit": args.max_always_on,
      "always_on_tokens_estimate": always_bytes // 4,
      "jetski_budget_tokens": JETSKI_RULES_BUDGET_TOKENS,
      "conditional_bytes": sum(r["bytes"] for r in rules if r["trigger"] != "always_on"),
      "skill_md_bytes": skills_bytes,
      "rules": [{"file": rel(r["file"]), "bytes": r["bytes"], "trigger": r["trigger"]}
                for r in rules],
      "weak_descriptions": [rel(f) for f in weak],
      "missing_required": missing,
      "duplicates": [{"shared": n, "a": rel(a), "b": rel(b)} for n, a, b in dups[:25]],
  }
  if args.json:
    print(json.dumps(report, indent=1, ensure_ascii=False))
  else:
    print(f"always-on rules: {always_bytes} B, ~{always_bytes // 4} tokens "
          f"(target {args.max_always_on} B; Jetski cap {JETSKI_RULES_BUDGET_TOKENS} tokens)")
    print(f"conditional rules: {report['conditional_bytes']} B; SKILL.md total: {skills_bytes} B")
    for r in sorted(report["rules"], key=lambda r: -r["bytes"]):
      print(f"  {r['bytes']:6d}  {r['trigger']:14s} {r['file']}")
    print(f"weak descriptions: {report['weak_descriptions'] or 'none'}")
    print(f"missing required phrases: {missing or 'none'}")
    print(f"duplicate pairs (>= {args.min_shared} shared {SHINGLE}-word shingles):")
    for d in report["duplicates"]:
      print(f"  {d['shared']:4d}  {d['a']}  <->  {d['b']}")
  return 1 if always_bytes > args.max_always_on or missing else 0


if __name__ == "__main__":
  sys.exit(main())
