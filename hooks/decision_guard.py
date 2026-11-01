#!/usr/bin/env python3
"""decision_guard.py — catch behavior-contract changes before they are committed.

A behavior decision (whoever made it: the user or the agent) leaves a trace in test contracts:
a test is removed, renamed, added, or its asserts / expected values change. The commit gate in
pre_tool_guard.py lists such changes and blocks the commit until every one is triaged with
`ack`. Triage lines must reach the user in the final reply (stop_guard.py). A `policy` change
also needs a decision card and waits for a user reply before the commit goes through.

Usage:
  decision_guard.py scan --repo <root> [--worktree]
  decision_guard.py ack --repo <root> --hash <H> --conv <id> --root <id>
      --triage "<Test>=<class>: <why>" [--triage ...] [--card <file>] [--worktree]

Classes: bugfix, refactor, rename, format, gating, feature, prune, policy.
Replay on SkoreText history: brain/44b1483f-c993-4d53-9d1a-57939eb1470a/scratch/
d1_detector_replay.md (detector T2 without topic keywords).
"""

import argparse
import collections
import datetime
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys

CLASSES = ("bugfix", "refactor", "rename", "format", "gating", "feature", "prune", "policy")
CARD_FIELDS = (
    "Решение:",
    "Было → Стало:",
    "Критерий-победитель:",
    "Критерий-проигравший:",
    "Основание:",
    "Не измерено:",
    "Что обнажается:",
    "Противоречит:",
)
# The reply must carry these card lines so the user sees what wins and what is unproven
CARD_REPLY_MARKERS = ("Критерий-проигравший", "Не измерено")
DEFAULT_LEDGER = os.path.expanduser("~/.gemini/config/ledgers/decision_guard.jsonl")

CODE_EXTS = (".cpp", ".cc", ".mm", ".c", ".h", ".dart", ".py", ".ts", ".js")
TEST_NAME_RE = re.compile(
    r"(^test_.*\.py$|_test\.(?:dart|py|cc|cpp)$|Tests?\.(?:cpp|cc|mm)$|\.(?:test|spec)\.(?:ts|js)$)"
)
TEST_DIRS = {"tests", "test", "unittests"}
NON_TEST_DIRS = {"testdata", "resources", "fixtures", "golden", "goldens"}

CPP_HEADER_RE = re.compile(
    r"^\s*(DEF_\w*TEST\w*|UNIX_ONLY_TEST|TEST|TEST_F|TEST_P|TYPED_TEST|TYPED_TEST_P)"
    r"\s*\(\s*(\w+)(?:\s*,\s*(\w+))?"
)
STR_HEADER_RE = re.compile(r"^\s*(?:test|testWidgets|it)\s*\(\s*(['\"])(.+?)\1")
PY_HEADER_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(")
# dart format / prettier put the test description on the line after `test(`
STR_HEADER_JOIN_RE = re.compile(r"\b(test|testWidgets|it)\s*\(\s*\n\s*")
ASSERT_CALL_RE = re.compile(
    r"\b(REPORTER_ASSERT\w*|REPORTER_EXPECT\w*|SkASSERT\w*|EXPECT_\w+|ASSERT_\w+|CHECK\w*"
    r"|DCHECK\w*|expect|expectLater"
    r"|self\.assert\w+|assert\w*)\s*\("
)
PY_ASSERT_RE = re.compile(r"^\s*assert\b(.*)$", re.MULTILINE)
NUMBER_RE = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?[fF]?(?![\w.])")


def ledger_path() -> str:
  return os.environ.get("DECISION_GUARD_LEDGER") or DEFAULT_LEDGER


def is_test_file(path: str) -> bool:
  if not path.endswith(CODE_EXTS):
    return False
  parts = path.split("/")
  if any(p in NON_TEST_DIRS for p in parts[:-1]):
    return False
  return bool(TEST_NAME_RE.search(parts[-1])) or any(p in TEST_DIRS for p in parts[:-1])


STRING_RE = r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'"
PY_COMMENT_RE = re.compile(rf"({STRING_RE})|#[^\n]*")
C_COMMENT_RE = re.compile(rf"({STRING_RE})|//[^\n]*|/\*.*?\*/", re.DOTALL)


def _strip_comments(text: str, path: str) -> str:
  # Strings are matched first and kept, so "#fff" or "https://x" survive
  pattern = PY_COMMENT_RE if path.endswith(".py") else C_COMMENT_RE
  return pattern.sub(lambda m: m.group(1) or "", text)


def _header_name(line: str, path: str) -> str:
  if path.endswith(".py"):
    m = PY_HEADER_RE.match(line)
    return m.group(1) if m else ""
  if path.endswith((".dart", ".ts", ".js")):
    m = STR_HEADER_RE.match(line)
    return m.group(2) if m else ""
  m = CPP_HEADER_RE.match(line)
  if not m:
    return ""
  # gtest names are Suite.Case; Skia DEF_*TEST macros take (Name, reporter, ...)
  if m.group(1) in ("TEST", "TEST_F", "TEST_P") and m.group(3):
    return f"{m.group(2)}.{m.group(3)}"
  return m.group(2)


def parse_tests(path: str, text: str) -> dict:
  """Map test name -> body text (header line excluded, comments stripped)."""
  tests: dict = {}
  name, body = "", []
  text = _strip_comments(text, path)
  if path.endswith((".dart", ".ts", ".js")):
    text = STR_HEADER_JOIN_RE.sub(r"\1(", text)

  def store():
    # Same-named tests in different classes or groups must not overwrite each other
    key, n = name, 2
    while key in tests:
      key, n = f"{name}#{n}", n + 1
    tests[key] = "\n".join(body)

  for line in text.splitlines():
    found = _header_name(line, path)
    if found:
      if name:
        store()
      name, body = found, []
    elif name:
      body.append(line)
  if name:
    store()
  return tests


def _balanced_call(text: str, open_idx: int) -> int:
  depth, quote, i = 0, "", open_idx
  # Each step advances i, so the loop ends by the text length; parens in strings do not count
  while i < len(text):
    c = text[i]
    if quote:
      if c == "\\":
        i += 1
      elif c == quote:
        quote = ""
    elif c in "\"'":
      quote = c
    elif c == "(":
      depth += 1
    elif c == ")":
      depth -= 1
      if depth == 0:
        return i + 1
    i += 1
  return len(text)


def split_asserts(body: str) -> tuple:
  """Return (Counter of whitespace-free assert calls, body with the asserts removed)."""
  found = collections.Counter()
  rest, pos = [], 0
  for m in ASSERT_CALL_RE.finditer(body):
    if m.start() < pos:
      continue
    end = _balanced_call(body, m.end() - 1)
    found[re.sub(r"\s+", "", body[m.start():end])] += 1
    rest.append(body[pos:m.start()])
    pos = end
  rest.append(body[pos:])
  remaining = "".join(rest)
  for m in PY_ASSERT_RE.finditer(remaining):
    found["assert" + re.sub(r"\s+", "", m.group(1))] += 1
  return found, PY_ASSERT_RE.sub("", remaining)


def _norm(body: str) -> str:
  return re.sub(r"\s+", "", body)


def _git(root: str, *args: str) -> str:
  res = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                       timeout=10, check=False)
  return res.stdout if res.returncode == 0 else ""


def _changed_files(root: str, include_worktree: bool, paths=None) -> list:
  """(old_path, new_path) pairs of test files that differ from HEAD; '' marks absence."""
  outs = [_git(root, "diff", "--cached", "--name-status", "-M", "HEAD")]
  if include_worktree:
    outs.append(_git(root, "diff", "--name-status", "-M", "HEAD"))
  pairs: dict = {}
  for out in outs:
    for line in out.splitlines():
      cols = line.split("\t")
      st = cols[0][:1]
      if st == "R" and len(cols) == 3:
        old, new = cols[1], cols[2]
      elif st == "A" and len(cols) == 2:
        old, new = "", cols[1]
      elif st == "D" and len(cols) == 2:
        old, new = cols[1], ""
      elif len(cols) == 2:
        old = new = cols[1]
      else:
        continue
      if paths is not None and not any(_under(x, paths) for x in (old, new) if x):
        continue
      if is_test_file(old or new) or is_test_file(new or old):
        pairs[(old, new)] = True
  return list(pairs)


def _under(path: str, pathspecs) -> bool:
  return any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/") for p in pathspecs)


def _after_text(root: str, path: str, include_worktree: bool) -> str:
  full = os.path.join(root, path)
  if include_worktree and os.path.exists(full):
    with open(full, encoding="utf-8", errors="replace") as f:
      return f.read()
  return _git(root, "show", f":{path}")


def _same_contract(a: str, b: str) -> bool:
  a_asserts, a_rest = split_asserts(a)
  b_asserts, b_rest = split_asserts(b)
  return a_asserts == b_asserts and (collections.Counter(NUMBER_RE.findall(a_rest))
                                     == collections.Counter(NUMBER_RE.findall(b_rest)))


# Fuzzy rename matching is quadratic; past this many pairs only exact bodies pair up
MAX_FUZZY_PAIRS = 400


def contract_delta(root: str, include_worktree: bool, paths=None) -> list:
  """Test-contract changes between HEAD and what the commit would record.

  `paths` (repo-relative) limits a pathspec commit to the files it records.
  """
  before: dict = {}
  after: dict = {}
  for old, new in _changed_files(root, include_worktree, paths):
    if old:
      for name, body in parse_tests(old, _git(root, "show", f"HEAD:{old}")).items():
        before[(new or old, name)] = body
    if new:
      for name, body in parse_tests(new, _after_text(root, new, include_worktree)).items():
        after[(new, name)] = body
  hits = []
  removed = [k for k in before if k not in after]
  added = [k for k in after if k not in before]
  for key in [k for k in before if k in after]:
    old_a, old_rest = split_asserts(before[key])
    new_a, new_rest = split_asserts(after[key])
    if old_a != new_a:
      plus, minus = new_a - old_a, old_a - new_a
      sample = next(iter(plus or minus))
      hits.append({"file": key[0], "test": key[1], "kind": "asserts",
                   "detail": f"+{sum(plus.values())}/-{sum(minus.values())} {sample[:80]}"})
    elif (collections.Counter(NUMBER_RE.findall(old_rest))
          != collections.Counter(NUMBER_RE.findall(new_rest))):
      hits.append({"file": key[0], "test": key[1], "kind": "values", "detail": ""})
  # A body that reappears unchanged under the same name in another file is a pure move;
  # a rename pairs only when asserts and numbers stay the same, so it never hides a contract edit
  fuzzy = len(removed) * len(added) <= MAX_FUZZY_PAIRS
  for r in list(removed):
    for a in list(added):
      same_body = _norm(before[r]) == _norm(after[a])
      similar = (fuzzy and not same_body and r[0] == a[0]
                 and _same_contract(before[r], after[a])
                 and difflib.SequenceMatcher(
                     None, _norm(before[r]), _norm(after[a])).ratio() >= 0.9)
      if same_body or similar:
        removed.remove(r)
        added.remove(a)
        if r[1] != a[1] or not same_body:
          hits.append({"file": a[0], "test": a[1], "kind": "renamed",
                       "detail": f"from {r[1]}" + ("" if same_body else ", body edited")})
        break
  hits += [{"file": f, "test": n, "kind": "removed", "detail": ""} for f, n in removed]
  hits += [{"file": f, "test": n, "kind": "new", "detail": ""} for f, n in added]
  return sorted(hits, key=lambda h: (h["file"], h["test"], h["kind"]))


def hits_hash(hits: list) -> str:
  blob = json.dumps([[h["file"], h["test"], h["kind"], h["detail"]] for h in hits])
  return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _now() -> str:
  return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(value: str):
  try:
    v = str(value).replace("Z", "+00:00")
    # Python < 3.11 accepts at most 6 fractional digits
    v = re.sub(r"(\.\d{6})\d+", r"\1", v)
    dt = datetime.datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
  except ValueError:
    return None


def _read_ledger() -> list:
  path = ledger_path()
  if not os.path.exists(path):
    return []
  entries = []
  with open(path, encoding="utf-8") as f:
    for line in f:
      try:
        entries.append(json.loads(line))
      except json.JSONDecodeError:
        continue
  return entries


def find_ack(root: str, h: str):
  matches = [e for e in _read_ledger()
             if e.get("repo") == os.path.realpath(root) and e.get("hash") == h]
  return matches[-1] if matches else None


def card_problems(card: str) -> list:
  problems = []
  for field in CARD_FIELDS:
    m = re.search(rf"^{re.escape(field)}[ \t]*(\S.*)$", card or "", re.MULTILINE)
    if not m:
      problems.append(field)
  return problems


def record_ack(root, h, conv, root_conv, triage: dict, card, hits) -> dict:
  """Store a triage of every hit; a policy class needs a complete decision card."""
  names = {x["test"] for x in hits}
  missing = sorted(names - set(triage))
  if missing:
    raise ValueError(f"triage missing for: {', '.join(missing)}")
  bad = sorted(c for c, _ in triage.values() if c not in CLASSES)
  if bad:
    raise ValueError(f"unknown class {bad}; use one of {', '.join(CLASSES)}")
  has_policy = any(c == "policy" for c, _ in triage.values())
  if has_policy and card_problems(card or ""):
    raise ValueError("policy needs a decision card with: " + ", ".join(card_problems(card or "")))
  entry = {"ts": _now(), "repo": os.path.realpath(root), "hash": h, "conv": conv,
           "root": root_conv or conv, "policy": has_policy, "card": card or "",
           "triage": {k: list(v) for k, v in triage.items()},
           "hits": hits}
  os.makedirs(os.path.dirname(ledger_path()) or ".", exist_ok=True)
  with open(ledger_path(), "a", encoding="utf-8") as f:
    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
  return entry


def _user_replied_after(transcript_path: str, ts: str) -> bool:
  limit = _parse_ts(ts)
  if not transcript_path or not os.path.exists(transcript_path) or not limit:
    return False
  with open(transcript_path, encoding="utf-8") as f:
    for line in f:
      try:
        step = json.loads(line)
      except json.JSONDecodeError:
        continue
      if step.get("type") != "USER_INPUT":
        continue
      at = _parse_ts(step.get("created_at", ""))
      if at and at > limit:
        return True
  return False


def format_hits(hits: list) -> str:
  return "\n".join(f"- {h['file']}: {h['test']} {h['kind']} {h['detail']}".rstrip()
                   for h in hits)


def commit_denial(root, hits, conv, root_conv, root_transcript, include_worktree=False,
                  paths=None) -> str:
  if not hits:
    return ""
  h = hits_hash(hits)
  ack = find_ack(root, h)
  script = "python3 ~/.gemini/config/hooks/decision_guard.py"
  if not ack:
    return (
        "[DECISION GUARD] This commit changes behavior contracts (tests). A test change can "
        "silently flip an earlier decision, whether the user or the agent started it. Triage "
        f"every line, then run:\n`{script} ack --repo {root} --hash {h} --conv {conv} "
        f"--root {root_conv or conv}{' --worktree' if include_worktree else ''}"
        f"{''.join(' --path ' + p for p in paths or [])} "
        "--triage \"<Test>=<class>: <why>\" ...`\n"
        f"Classes: {', '.join(CLASSES)}. Use `policy` when expected behavior changes (not a "
        "bug fix toward an already agreed behavior); policy also needs `--card <file>` with "
        f"the fields {' / '.join(f[:-1] for f in CARD_FIELDS)} and waits for the user's reply. "
        "List every triage line in your final reply.\nChanges:\n" + format_hits(hits)
    )
  if ack.get("policy") and not _user_replied_after(root_transcript, ack["ts"]):
    return (
        "[DECISION GUARD] A policy change waits for the user: show the decision card from the "
        f"ack ({h}) in your reply, ask the user, and commit only after the user answers. A "
        "subagent stops here and reports the card to its parent."
    )
  return ""


def visibility_reason(root_conv: str, since_ts: str, final_text: str) -> str:
  """Reason to block the stop when acks of this turn are missing from the reply."""
  since = _parse_ts(since_ts)
  missing, need_card = [], False
  for e in _read_ledger():
    at = _parse_ts(e.get("ts", ""))
    if e.get("root") != root_conv or not at or (since and at <= since):
      continue
    missing += [t for t in e.get("triage", {}) if t not in final_text]
    need_card = need_card or bool(e.get("policy"))
  card_missing = need_card and not all(m in final_text for m in CARD_REPLY_MARKERS)
  if not missing and not card_missing:
    return ""
  parts = ["[DECISION GUARD] This turn triaged behavior-contract changes, but the reply does "
           "not show them to the user."]
  if missing:
    parts.append("Add one line per test (name, change, class, why): "
                 + ", ".join(sorted(set(missing))) + ".")
  if card_missing:
    parts.append("A policy change was acked: paste its decision card into the reply.")
  return " ".join(parts)


def _parse_triage(items: list) -> dict:
  triage = {}
  for item in items or []:
    m = re.match(r"^\s*(.+?)\s*=\s*(\w+)\s*:\s*(.+?)\s*$", item)
    if not m:
      raise ValueError(f"bad --triage {item!r}; expected \"<Test>=<class>: <why>\"")
    triage[m.group(1)] = (m.group(2), m.group(3))
  return triage


def main(argv=None) -> int:
  p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
  sub = p.add_subparsers(dest="cmd", required=True)
  s = sub.add_parser("scan")
  s.add_argument("--repo", required=True)
  s.add_argument("--worktree", action="store_true")
  s.add_argument("--path", action="append", default=None)
  a = sub.add_parser("ack")
  a.add_argument("--repo", required=True)
  a.add_argument("--hash", required=True)
  a.add_argument("--conv", required=True)
  a.add_argument("--root", default="")
  a.add_argument("--triage", action="append", default=[])
  a.add_argument("--card", default="")
  a.add_argument("--worktree", action="store_true")
  a.add_argument("--path", action="append", default=None)
  args = p.parse_args(argv)
  hits = contract_delta(args.repo, args.worktree, args.path)
  if args.cmd == "scan":
    print(f"hash {hits_hash(hits)}\n{format_hits(hits) or 'no contract changes'}")
    return 0
  # Re-scan so an ack never covers a diff that changed after the gate listed it
  if hits_hash(hits) != args.hash:
    print(f"hash mismatch: the change now hashes to {hits_hash(hits)}; re-run scan",
          file=sys.stderr)
    return 2
  card = ""
  if args.card:
    with open(os.path.expanduser(args.card), encoding="utf-8") as f:
      card = f.read()
  try:
    entry = record_ack(args.repo, args.hash, args.conv, args.root, _parse_triage(args.triage),
                       card, hits)
  except ValueError as e:
    print(str(e), file=sys.stderr)
    return 2
  for h in hits:
    cls, why = entry["triage"][h["test"]]
    print(f"{h['test']}: {h['kind']} {h['detail']} -> {cls}: {why}".replace("  ", " "))
  return 0


if __name__ == "__main__":
  sys.exit(main())
