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
- **Identifiers**: When a chat targets a specific PR, CL, bug, issue, or commit, include its short ID in `<Topic>` (e.g. `PR #12345`, `cl/123456`, `b/123456`, 7-char SHA).
- **Format**: `[HH:MM] <marker> <Topic>`. The time prefix and the marker are produced **only** by the CLI below — never type them yourself.

### Lifecycle CLI (markers are mechanical)

Markers (`▸ ✓ ⦿ « ‹✓› » «»`) are a side effect of the commands below. They are derived purely from chain structure (who continues whom) plus one "closed" bit. **You must never reason about, choose, or interpret markers** — not when naming, not when continuing, not when closing.

Use only these commands (`python3 ~/.gemini/config/hooks/chat_lifecycle.py ...`):

| Command | When you call it |
|---|---|
| `set-title <id> "<Topic>"` | User asks to name/rename the chat, or on the first turn once you know the bug/CL/PR/commit topic to refine the auto-detected ID (or replace Jetski's generic auto-generated title like `"Bug Report Analysis"`). Pass the bare topic (e.g. `"b/575506671 SkFontDescriptor serialize"`); time and marker are added for you. |
| `auto-title <id> "<prompt>"` | Detect PR/CL/bug/commit in prompt and set title if the chat is currently untitled (invoked automatically by `context_guard.py` on turn start). |
| `handoff <id> [summary_file] [--notes "..."] [--next "..."] [--topic "<Topic>"] [--force]` | Context guard alert, or user asks to continue in a new chat. One call: writes/updates the summary, launches a visible continuation in the same project **with the same model as `<id>`'s last turn**, verifies `sourceMetadata: null`, updates chain markers. `--topic` titles the continuation with the next item's topic (the chain is defined by handoff links, so topics may differ per step); without it the topic is inherited. `--model <id\|name>` only if the user explicitly asks for a different model. A second handoff from the same chat is refused unless `--force` (only when the user explicitly asked for another continuation). |
| `summary-path <id>` | You want to write a detailed summary with `write_to_file` before `handoff` — this returns the canonical path. |
| `finalize <id>` | **Only** when the user explicitly says *"финал"* (or an unambiguous equivalent). Preserves original timestamps, swaps markers. |
| `status [id]` | User asks about the chain / which chats belong to the task / where summaries are. |
| `model [id] [--trailer]` | User asks which model a chat runs on or which model a continuation would get (or outputs git `Assisted-by: ...` trailer with `--trailer`). Read-only. |
| `project-model [<p>] [<m>]` | View or configure project-specific preferred model (saved to `~/.gemini/config/projects_models.json`, e.g. `skia-g3-roll-check` -> `flash`). |
| `audit [--fix] [--project <id\|name>]` | Scan top-level chats and chains for title desync (LS RPC vs `.pbtxt` vs SQLite) or broken Scheme Γ markers; `--fix` repairs them. |
| `archive [<id...>] [--chain] [--project <p>] [--keep <N>]` | Archive specified chats (or their full handoff chain with `--chain`) via LS RPC and `.pbtxt`. With `--project`, archives older active chats in project `<p>`, keeping the `<N>` newest (default 1). |
| `auto-archive [--hook]` | Prune older chats across projects configured with a retention policy (default: keep 1 newest for `skia-g3-roll-check`). Runs automatically via `Stop` and `SessionStart` hooks. |

For moving chats or whole chains between projects (or listing unassigned chats), use `python3 ~/.gemini/config/scripts/move_chats_to_project.py --project "<name_or_id>" [--chain] --restart <id...>` (which waits in the background until all running chats finish their turns before restarting `jetski-hub`) or `--list-unassigned`.

## Backlog & Scope Invariants across Handoff Chains

- **In-project chats: Epic Roadmap via `roadmap.py` only**: A chat inside a Jetski project keeps the Epic Roadmap in the project's shared roadmap through `python3 ~/.gemini/config/hooks/roadmap.py add|set|link|show|edit|reorder|move ...` and **never** edits files under `~/.gemini/config/roadmaps/` directly. The only exception is the chain state file (`roadmap.py state-path <id>`), which is the handoff summary in projects and carries the immediate next step. The checkbox rules below apply to the handoff summaries of outside-project chats.
  - **Backlog requests**: when the user says something like "в бэклог: X" / "add to backlog: X", run `roadmap.py add <id> "X"`, confirm in one line for a single item (`R7 added: X`) or as a bulleted Markdown list separated by blank lines when adding multiple items (never consecutive lines with single newlines, which Markdown collapses into one paragraph), then continue the current work.
  - **Spin-offs**: when an item is handed to its own chat, run `roadmap.py link <id> <item ID> <new_chat_id>`.
  - **At handoff**: paste the roadmap block printed by `chat_lifecycle.py handoff` into the final message to the user.
- **Parallel chats in one project share resources, so claim before touching**:
  - **Claim before touching.** Before editing a deliverable or running build/test in a working copy, run `roadmap.py show`. If the item you need is `doing` in another live chat, do not start: tell the user which chat owns it and wait. Otherwise claim it first with `roadmap.py set <id> <item> doing`.
  - **One working copy, one writer (`[WORKTREE LEASE GUARD]`).** Code worktrees have a per-worktree lease (`<git_dir>/jetski_lease.json`, 30m TTL, auto-released when the owner chat is finalized `✓` or handed off) enforced by `hooks/pre_tool_guard.py` on file edits and mutating git, build, or test commands (`checkout`, `rebase`, `commit`, `ninja`, `siso`, `felt test`, `flutter test`, `gn gen`). If a worktree is leased by another active chat, create a separate linked worktree via `git worktree add` instead of sharing `out/` or branches.
  - **Explicit pathspec staging & commits in shared checkouts (`[SHARED-FILE LEASE GUARD]` & `[SHARED-WORKTREE COMMIT GUARD]`).** In shared config checkouts (`~/.gemini/config`), `hooks/pre_tool_guard.py` tracks per-file leases (`<git_dir>/jetski_file_leases.json`), blocks concurrent edits to a file leased by another active chat, blocks `git add .` / `git add -A` / `git commit -a` when foreign dirty files exist, and blocks pathspec-less `git commit` when foreign files are staged in the Git index. Always stage and commit only the files touched in your chat using explicit pathspecs (`git add -- <files> && git commit -m "..." -- <files>`).
  - **Push only your own commits (`[FOREIGN-COMMIT PUSH GUARD]` & `[GIT SYNC NOTICE]`).** `git push` sends every commit on the branch, not just yours. `hooks/pre_tool_guard.py` records commit ownership in `<git_common_dir>/jetski_commits.json` and blocks `git push` if `@{u}..HEAD` contains unpushed commits created by another active chat; `hooks/context_guard.py` injects `[GIT SYNC NOTICE]` at turn start when a chat's earlier local commit was pushed to upstream by another conversation while idle. Only after the user explicitly confirms in a subsequent turn may you write `<artifact_dir>/scratch/.parallel_chat_override.json` to bypass a parallel-chat guard.
  - **One deliverable, one home.** A document under a roadmap item lives at exactly one path, recorded in the item text; other chats read it, they do not copy or fork it.
- **Wrong-Chat / Cross-Project & Sibling Scope Guard (`hooks/context_guard.py` + `hooks/pre_tool_guard.py`)**:
  - **Turn-start soft stop**: At `invocation_num == 0`, `hooks/context_guard.py` injects `[SCOPE GUARD: WRONG-CHAT / WRONG-PROJECT RADAR]` listing the current chat's project, workspace(s), active sibling chats in the same project (holding `doing` items or active within 2h), and other active projects and their open `⦿` chats. If a new user request clearly targets another project, workspace, or chat topic, **do not** start investigating, reading foreign project files, or running commands — reply immediately warning the user which project or chat owns that topic (`[👉 <Title>](conversation://<target_conv_id>)`) and offer to forward the message via `agentapi send-message <target_conv_id> "<user message>"` or execute it here.
  - **Hard cross-project write & commit guard**: `pre_tool_guard.py` blocks file edits (`EDIT_TOOLS`) and `git commit` / `git cl upload` / `gh pr create` inside any workspace directory exclusively associated with another Jetski project (`[CROSS-PROJECT SCOPE GUARD]`). Only after the user explicitly confirms in a subsequent turn that she wants the cross-project edit performed in the current chat may you write `<artifact_dir>/scratch/.cross_project_override.json` to unlock it.
- **Never Shrink Epic Scope**: Handoff summaries must maintain the complete multi-stage roadmap (`Epic Roadmap`) with explicit checkbox states (`[x]` for completed, `[ ]` for pending, `[-]` for abandoned/out-of-scope). An agent is **strictly forbidden** from deleting or dropping uncompleted backlog items from `Next Steps` or `Roadmap` during handoff.
- **Two-Level Plan Bifurcation**: Always separate:
  1. `Epic Roadmap`: Complete checklist of all macro-deliverables across the entire task lifetime. When completing a handoff, copy the previous roadmap forward, updating only the status (`[ ]` -> `[x]`).
  2. `Immediate Next Step`: The concrete, isolated first action for the continuation chat to resume immediately.
- **No Handoff Anchoring (Separate Proven Facts from Unverified Hypotheses)**:
  - Never record an unverified user or agent idea or last-minute compromise in a handoff summary or chain state file as a settled decision (`"Decided to do X"`). Explicitly label untested proposals as **`[UNVERIFIED HYPOTHESIS]`** so the continuation chat stress-tests them and checks alternatives before writing code.
  - **Preserve Rejected Alternatives**: Briefly record rejected approaches and the exact counterexample or invariant that killed them (`Rejected: <approach> because <reason>`) in the handoff summary so continuation chats do not re-propose or re-accept dead ends.
- **Continuation Inception Check**: When waking up in a continuation chat, the incoming agent must review the full `Epic Roadmap` from the summary (in projects: `roadmap.py show`), verify that any pending design in `Immediate Next Step` was actually proven against the codebase rather than assumed at handoff boundary, acknowledge the overall task progress, and ensure broader objectives are not abandoned after local deep-dives.

Strict rules:
- **Never** silently prune or drop pending stages from a handoff summary. If an epic had 5 steps and the current chat only addressed step 2, all steps 3–5 MUST remain in the summary as pending `[ ]`.
- **Never** ask the user whether to finalize. No "Пометить разбор как финальный?" prompts. Finalization happens only on the user's explicit word.
- **Never** reopen finalized chats automatically or infer status from the user's wording. If the user continues working in a closed chat, do nothing with the title; the chain is reactivated mechanically by the next `handoff`, or on explicit request (`reopen <id>`).
- **Never** write SQLite queries or Python `sqlite3` snippets to look up or set titles, and **never** read transcripts or hook source code to recall how handoff works — the commands above are the entire interface.
