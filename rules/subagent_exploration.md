---
trigger: always_on
description: "Context hygiene: when to delegate to subagents, which model, and grounding API calls."
---

# Context Hygiene and Delegation

- Size the task: under 3 tool calls → do it yourself; 3–15 calls (exploring a subsystem, call
  graph over more than 3–4 files, clean-context audit) → one subagent; over 15 calls or many
  items → `run_workflow` or several subagents
- Models: `Model="flash"` for mechanical lookups, test/build runners, log triage and routine
  `keeper_skeptic` batches; `Model="inherit"` for semantic research, root-cause work and
  `keeper_inquisitor`. Never `Model="pro"` (hook-enforced)
- Give a subagent at most 150 KB or 10 files per turn; never dump whole `references/`, `g3doc/`
  or module trees into one subagent
- Running test orchestrators, roll triage, or reading their logs: see `test_runs.md`
- Never defer to a broken local pattern just because existing code or a comment codifies it;
  judge against `cpp_systems_invariants.md`, `text_domain_invariants.md`, `code_style.md`
- Read the declaration (`.h`, `.py`, `--help`) before calling any API not already open
- MCP server names are `google_chat_google_chat`, `google_docs_google_docs`,
  `google_drive_google_drive` (plugin rules saying `chat_google_chat` etc. are stale and
  read-only); Gmail goes through the `gmail` CLI, see `gmail_drafts.md`
- Post-mortem only when code refutes her explicit decision or a defect escapes past her approval
  (revert, post-submit failure, reopened bug, blocking human review): 3 lines
  `Решение -> Что опровергло (file:line / гейт / инцидент) -> Правило перехвата`; if the same
  class repeats, offer an `ask_question` to codify the invariant. No turn-by-turn praise or blame;
  never speculate about her IDE actions from prompt length ("ты не читала код"); fix your own
  pre-commit catches silently
