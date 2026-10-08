---
trigger: always_on
description: "Ensure conversations created via `agentapi new-conversation` are user-visible top-level chats (not hidden tool-sourced ones), and verify it."
---

# Creating User-Visible Chats via `agentapi`

Background: when `agentapi new-conversation` is run from an agent's `run_command`, it inherits the `ANTIGRAVITY_SOURCE_METADATA` env var. The server then records the new chat with `sourceMetadata.tool` set, and such chats do **not** appear in the user's sidebar (observed behavior, undocumented; it may change).

- **Unset the variable for the child process**:
  - Shell: `env -u ANTIGRAVITY_SOURCE_METADATA agentapi new-conversation --title="..." "..."`
  - Python: pass `env={k: v for k, v in os.environ.items() if k != "ANTIGRAVITY_SOURCE_METADATA"}` to `subprocess.run`.
  - Do **not** strip `ANTIGRAVITY_PROJECT_ID` or other `ANTIGRAVITY_*` variables.
  - **Never hardcode `ANTIGRAVITY_*` variables to literal values** yourself. However, you MUST preserve any `ANTIGRAVITY_PROJECT_ID="..."` injections provided by system hooks (e.g. `context_guard.py`), and ALWAYS include `env -u ANTIGRAVITY_SOURCE_METADATA`.
- **Verify every created chat immediately**:
  - Run `agentapi get-conversation-metadata <id>` and check that `"sourceMetadata": null`.
  - Pre-tool hooks may silently rewrite commands, so never assume the unset was applied — check the metadata.
  - **Create and verify in ONE command.** A pre-tool guard may block all further commands right after a continuation chat is created, so a separate verification step can be impossible:
    ```bash
    OUT=$(env -u ANTIGRAVITY_SOURCE_METADATA agentapi new-conversation --title="..." "...") && \
    ID=$(printf '%s' "$OUT" | grep -o '"conversationId": *"[^"]*"' | cut -d'"' -f4) && \
    echo "ID=$ID" && agentapi get-conversation-metadata "$ID" | grep -E '"sourceMetadata"'
    ```
    The output must show `"sourceMetadata": null`; otherwise report `$ID` to the user.
  - **ANTI-PATTERN WARNING**: Never call `agentapi new-conversation` inside `$()` *to extract the ID directly*, as you might accidentally re-execute the command (e.g. `cmd && verify $(cmd | grep)` creates TWO chats). Always capture the full JSON output into a variable (`OUT=$(cmd)`) exactly once, and extract the ID from the variable (`$OUT`).
- **On failed verification**:
  - Stop. Do **not** blindly re-create chats (this produces hidden duplicates that cannot be deleted via `agentapi`).
  - Report the problem and the affected conversation IDs to the user, then fix the creation method first.
- **Before bulk creation**, create one chat, verify it, and only then create the rest.

## Conversation Titles (`--title`) & Lifecycle

- **One project = one task.** Every non-trivial task lives in its own project; all its chats (including handoff continuations) stay inside that project (`ANTIGRAVITY_PROJECT_ID` is inherited automatically by `handoff`). A project is only a grouping — it may point at the same existing checkout as other projects; do not clone or re-sync repositories per task.
- **No Project Prefix**: Never include the project name in a chat title.
- **Style**: Direct and specific (strictly under 50 chars), plain text only (no markdown `**` or HTML tags, no Unicode math fonts).
- **Format**: `[HH:MM] <marker> <Topic>`. The time prefix and the marker are produced **only** by the CLI below — never type them yourself.

### Lifecycle CLI (markers are mechanical)

Markers (`▸ ✓ ⦿ « ‹✓› » «»`) are a side effect of the commands below. They are derived purely from chain structure (who continues whom) plus one "closed" bit. **You must never reason about, choose, or interpret markers** — not when naming, not when continuing, not when closing.

Use only these commands (`python3 ~/.gemini/config/hooks/chat_lifecycle.py ...`):

| Command | When you call it |
|---|---|
| `set-title <id> "<Topic>"` | User asks to name/rename the chat. Pass the bare topic; time and marker are added for you. |
| `handoff <id> [summary_file] [--notes "..."] [--next "..."]` | Context guard alert, or user asks to continue in a new chat. One call: writes/updates the summary, launches a visible continuation in the same project **with the same model as `<id>`'s last turn**, verifies `sourceMetadata: null`, updates chain markers. `--model <id\|name>` only if the user explicitly asks for a different model. |
| `summary-path <id>` | You want to write a detailed summary with `write_to_file` before `handoff` — this returns the canonical path. |
| `finalize <id>` | **Only** when the user explicitly says *"финал"* (or an unambiguous equivalent). Preserves original timestamps, swaps markers. |
| `status [id]` | User asks about the chain / which chats belong to the task / where summaries are. |
| `model [id]` | User asks which model a chat runs on or which model a continuation would get. Read-only. |
| `audit [--fix] [--project <id\|name>]` | Scan top-level chats and chains for title desync (LS RPC vs `.pbtxt` vs SQLite) or broken Scheme Γ markers; `--fix` repairs them. |
| `archive <id...> [--chain]` | Archive specified chats (or their full handoff chain with `--chain`) via LS RPC and `.pbtxt`. |

For moving chats or whole chains between projects (or listing unassigned chats), use `python3 ~/.gemini/config/scripts/move_chats_to_project.py --project "<name_or_id>" [--chain] --restart <id...>` (which waits in the background until all running chats finish their turns before restarting `jetski-hub`) or `--list-unassigned`.

## Backlog & Scope Invariants across Handoff Chains

- **In-project chats: Epic Roadmap via `roadmap.py` only**: A chat inside a Jetski project keeps the Epic Roadmap in the project's shared roadmap through `python3 ~/.gemini/config/hooks/roadmap.py add|set|link|show ...` and **never** edits files under `~/.gemini/config/roadmaps/` directly. The only exception is the chain state file (`roadmap.py state-path <id>`), which is the handoff summary in projects and carries the immediate next step. The checkbox rules below apply to the handoff summaries of outside-project chats.
  - **Backlog requests**: when the user says something like "в бэклог: X" / "add to backlog: X", run `roadmap.py add <id> "X"`, confirm in exactly one line (e.g. `R7 added: X`), then continue the current work.
  - **Spin-offs**: when an item is handed to its own chat, run `roadmap.py link <id> <item ID> <new_chat_id>`.
  - **At handoff**: paste the roadmap block printed by `chat_lifecycle.py handoff` into the final message to the user.
- **Never Shrink Epic Scope**: Handoff summaries must maintain the complete multi-stage roadmap (`Epic Roadmap`) with explicit checkbox states (`[x]` for completed, `[ ]` for pending, `[-]` for abandoned/out-of-scope). An agent is **strictly forbidden** from deleting or dropping uncompleted backlog items from `Next Steps` or `Roadmap` during handoff.
- **Two-Level Plan Bifurcation**: Always separate:
  1. `Epic Roadmap`: Complete checklist of all macro-deliverables across the entire task lifetime. When completing a handoff, copy the previous roadmap forward, updating only the status (`[ ]` -> `[x]`).
  2. `Immediate Next Step`: The concrete, isolated first action for the continuation chat to resume immediately.
- **Continuation Inception Check**: When waking up in a continuation chat, the incoming agent must review the full `Epic Roadmap` from the summary (in projects: `roadmap.py show`), acknowledge the overall task progress, and ensure broader objectives are not abandoned after local deep-dives.

Strict rules:
- **Never** silently prune or drop pending stages from a handoff summary. If an epic had 5 steps and the current chat only addressed step 2, all steps 3–5 MUST remain in the summary as pending `[ ]`.
- **Never** ask the user whether to finalize. No "Пометить разбор как финальный?" prompts. Finalization happens only on the user's explicit word.
- **Never** reopen finalized chats automatically or infer status from the user's wording. If the user continues working in a closed chat, do nothing with the title; the chain is reactivated mechanically by the next `handoff`, or on explicit request (`reopen <id>`).
- **Never** write SQLite queries or Python `sqlite3` snippets to look up or set titles, and **never** read transcripts or hook source code to recall how handoff works — the commands above are the entire interface.
