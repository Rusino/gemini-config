---
trigger: always_on
description: "Prevent context pollution and hallucinated APIs by delegating multi-file exploration to subagents, gating batch workflows with advanced.verify, and reading headers before writing code."
---

# Context Hygiene, Batch Verification & API Grounding

- **Delegate Broad Exploration to Subagents (`invoke_subagent`)**:
  - When exploring an unfamiliar subsystem, tracing call graphs across directories, or needing to inspect more than 3–4 files to locate relevant code, **do not** read all those files in the main conversation.
  - Spawn a subagent (`self`) to perform the search in its own isolated context and return a concise summary with exact file paths and line numbers.
- **Gate Batch Agent Workflows with `advanced.verify` (`run_workflow`)**:
  - Whenever orchestrating a batch task via `run_workflow` that spawns multiple agents (`parallel` or `pipeline` with `agent(...)`) to port commits, edit files, triage bugs, or audit modules, always import `from advanced import parallel, verify` and gate each item's output (or the aggregated batch result) with `await verify(result, criteria)` in a clean adversarial context before applying or reporting it.
  - Only pure read-only discovery fan-outs (or quick `call_tool`-only scripts) may omit `verify(...)` by including `# no-verify` in the workflow script.
- **Read Definitions Before Writing Calls**:
  - Never guess class methods, function signatures, struct fields, or CLI flags from memory.
  - Before calling an API that is not already visible in the open file, view its declaration (`.h`, `.hpp`, `.py`, etc.) or run `--help`.
