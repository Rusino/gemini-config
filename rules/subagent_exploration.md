---
trigger: always_on
description: "Prevent context pollution and hallucinated APIs by delegating multi-file exploration to subagents, gating batch workflows with advanced.verify, and reading headers before writing code."
---

# Context Hygiene, Batch Verification & API Grounding

- **Task Sizing & Subagent Model Routing (`invoke_subagent`)**:
  - **`< 3` tool calls** (targeted file read, single `code_search` / `git grep`): Execute directly in the main conversation (0 subagents).
  - **`3–15` tool calls** (subsystem exploration, tracing call graphs across >3–4 files, or clean-context Hunter/Skeptic audit): Delegate to an isolated subagent (`invoke_subagent`). Default `research-google` and routine `keeper_skeptic` batches to `Model="flash"`; escalate `keeper_skeptic` to `Model="pro"` for deep concurrency, BiDi/UTF-8/UTF-16/grapheme math, dropped historical guards (`git log -S`), or cross-backend symmetry (`XFAMILY_REFUTE`).
  - **`> 15` tool calls or multi-item fan-outs**: Use `run_workflow` (`parallel` / `pipeline`) or partitioned subagents so the main context stays clean.
- **Gate Batch Agent Workflows with `advanced.verify` (`run_workflow`)**:
  - Whenever orchestrating a batch task via `run_workflow` that spawns multiple agents (`parallel` or `pipeline` with `agent(...)`) to port commits, edit files, triage bugs, or audit modules, always import `from advanced import parallel, verify` and gate each item's output (or the aggregated batch result) with `await verify(result, criteria)` in a clean adversarial context before applying or reporting it.
  - Only pure read-only discovery fan-outs (or quick `call_tool`-only scripts) may omit `verify(...)` by including `# no-verify` in the workflow script.
- **Read Definitions Before Writing Calls**:
  - Never guess class methods, function signatures, struct fields, or CLI flags from memory.
  - Before calling an API that is not already visible in the open file, view its declaration (`.h`, `.hpp`, `.py`, etc.) or run `--help`.
  - For Workspace MCP plugins (`google_chat`, `google_docs`, `google_drive`), the registered `ServerName` and schema directory under `~/.gemini/jetski/mcp/` are **`google_chat_google_chat`**, **`google_docs_google_docs`**, and **`google_drive_google_drive`** (overriding the legacy `chat_google_chat` / `docs_google_docs` / `drive_google_drive` strings in the plugin symlinks; also ignore the pre-loaded `gdrive` CLI skill).
