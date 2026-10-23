---
trigger: always_on
description: "Ensure conversations created via `agentapi new-conversation` are user-visible top-level chats (not hidden tool-sourced ones), and verify it."
---

# Creating User-Visible Chats via `agentapi`

When `agentapi new-conversation` runs from `run_command`, it inherits `ANTIGRAVITY_SOURCE_METADATA`, causing the server to mark `sourceMetadata.tool` and hide the chat from the sidebar. Always unset `ANTIGRAVITY_SOURCE_METADATA` (while preserving `ANTIGRAVITY_PROJECT_ID`), capture the output once into `OUT=$(...)`, and verify `"sourceMetadata": null` in the same command:

```bash
OUT=$(env -u ANTIGRAVITY_SOURCE_METADATA agentapi new-conversation --title="..." "...") && \
ID=$(printf '%s' "$OUT" | grep -o '"conversationId": *"[^"]*"' | cut -d'"' -f4) && \
echo "ID=$ID" && agentapi get-conversation-metadata "$ID" | grep -E '"sourceMetadata"'
```

- Never call `agentapi new-conversation` inside `$()` to extract the ID directly (avoid double execution).
- On failed verification (`sourceMetadata != null`), stop and report `$ID` — do not blindly re-create duplicates. Before bulk creation, create and verify one chat first.

## Conversation Titles (`--title`) & Lifecycle

- **One project = one task.** Every non-trivial task lives in its own project; all its chats (including handoff continuations) stay inside that project (`ANTIGRAVITY_PROJECT_ID` is inherited automatically by `handoff`). A project is only a grouping — do not clone or re-sync repositories per task.
- **Format**: `[HH:MM] <marker> <Topic>` — strictly under 50 chars, plain text only, no project prefix, include short PR/CL/bug/commit ID when applicable. The time prefix and marker (`▸ ✓ ⦿ « ‹✓› » «»`) are mechanical side effects of `chat_lifecycle.py` — **never type or choose markers yourself**.

Use only these commands (`python3 ~/.gemini/config/hooks/chat_lifecycle.py ...`):

| Command | When you call it |
|---|---|
| `set-title <id> "<Topic>"` | User asks to name/rename the chat, or on the first turn once you know the bug/CL/PR/commit topic to refine the auto-detected ID or replace a generic title. Pass bare `<Topic>`. |
| `auto-title <id> "<prompt>"` | Detect PR/CL/bug/commit in prompt and set title if untitled (invoked automatically by `context_guard.py`). |
| `handoff <id> [summary_file] [--notes "..."] [--next "..."] [--topic "<Topic>"] [--force]` | Context guard alert, or user asks to continue in a new chat. Writes/updates summary, launches visible continuation in the same project on `<id>`'s last model, verifies `sourceMetadata: null`, updates chain markers. `--model <id\|name>` only on explicit user request; `--force` only when user explicitly asked for a second continuation. |
| `summary-path <id>` | Returns the canonical summary path before writing with `write_file`. |
| `finalize <id>` | **Only** when the user explicitly says *"финал"* (or unambiguous equivalent). |
| `status [id]` | Inspect chain structure and summary paths. |
| `model [id] [--trailer]` | Inspect chat model or print `Assisted-by: ...` git trailer (`--trailer`). |
| `project-model [<p>] [<m>]` | View or set project preferred model in `~/.gemini/config/projects_models.json`. |
| `audit [--fix] [--project <id\|name>]` | Scan/repair title desync or broken Scheme Γ markers. |
| `archive [<id...>] [--chain] [--project <p>] [--keep <N>]` | Archive chats/chains via LS RPC and `.pbtxt`, or prune project `<p>` keeping `<N>` newest. |
| `auto-archive [--hook]` | Prune older chats across projects with retention policies. |

To move chats/chains between projects, run `python3 ~/.gemini/config/scripts/move_chats_to_project.py --project "<name_or_id>" [--chain] --restart <id...>` or `--list-unassigned`.

## Backlog & Scope Invariants across Handoff Chains

- **In-project chats: Epic Roadmap via `roadmap.py` only**: Manage the project roadmap exclusively via `python3 ~/.gemini/config/hooks/roadmap.py add|set|link|show|edit|reorder|move ...` (never edit `~/.gemini/config/roadmaps/` files directly, except the chain state file from `roadmap.py state-path <id>`).
  - **Backlog requests ("в бэклог: X")**: run `roadmap.py add <id> "X"`, confirm on one line (or bulleted list separated by blank lines for multiple items), then continue current work.
  - **Spin-offs**: run `roadmap.py link <id> <item ID> <new_chat_id>`.
  - **At handoff**: paste the roadmap block printed by `chat_lifecycle.py handoff` into the final message.
- **Parallel chats in one project share resources — claim before touching**:
  - Run `roadmap.py show` before editing or building; if an item is `doing` in another live chat, report the owner and wait, otherwise claim it with `roadmap.py set <id> <item> doing`.
  - **Worktree & shared-checkout guards (`hooks/pre_tool_guard.py`)**: If a code worktree is leased by another active chat (`[WORKTREE LEASE GUARD]`), create a separate linked worktree (`git worktree add`). In shared checkouts (`~/.gemini/config`), stage and commit only your own files with explicit pathspecs (`git add -- <files> && git commit -m "..." -- <files>`), and never `git push` when `@{u}..HEAD` contains another active chat's commits (`[FOREIGN-COMMIT PUSH GUARD]`). One deliverable document lives at one canonical path.
- **Wrong-Chat / Cross-Project Scope Guard**:
  - If `[SCOPE GUARD: WRONG-CHAT / WRONG-PROJECT RADAR]` shows a new user request belongs to another project, workspace, or active sibling chat, **do not** start investigating or running commands — warn the user with `[👉 <Title>](conversation://<target_conv_id>)` and offer `agentapi send-message <target_conv_id> "<user message>"`. `pre_tool_guard.py` blocks cross-project edits/commits unless the user explicitly confirms in a subsequent turn (`.cross_project_override.json` / `.parallel_chat_override.json`).
- **Handoff Integrity (`Epic Roadmap` + `Immediate Next Step`)**:
  - Never drop or shrink pending `[ ]` roadmap items during handoff.
  - Label untested ideas as **`[UNVERIFIED HYPOTHESIS]`** (never `"Decided to do X"`) and record rejected approaches (`Rejected: <approach> because <reason>`).
  - On continuation start, run `roadmap.py show`, verify the state file against `git status`/tests, and check that any proposed design was proven in code.
  - Never ask whether to finalize, never auto-reopen finalized chats, and never query SQLite or hook sources directly for lifecycle state.
