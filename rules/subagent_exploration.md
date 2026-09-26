---
trigger: always_on
description: "Prevent context pollution and hallucinated APIs by delegating multi-file exploration to subagents and reading headers before writing code."
---

# Context Hygiene & API Verification

- **Delegate Broad Exploration to Subagents (`invoke_subagent`)**:
  - When exploring an unfamiliar subsystem, tracing call graphs across directories, or needing to inspect more than 3–4 files to locate relevant code, **do not** read all those files in the main conversation.
  - Spawn a subagent (`self`) to perform the search in its own isolated context and return a concise summary with exact file paths and line numbers.
- **Read Definitions Before Writing Calls**:
  - Never guess class methods, function signatures, struct fields, or CLI flags from memory.
  - Before calling an API that is not already visible in the open file, view its declaration (`.h`, `.hpp`, `.py`, etc.) or run `--help`.
