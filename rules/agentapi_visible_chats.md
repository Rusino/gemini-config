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
- **Format**: `[HH:MM] <marker> <Topic>` — strictly under 50 chars, plain text only, no project prefix, include short PR/CL/bug/commit ID when applicable. The time prefix and marker (`⦿` needs the user, `⏳` waiting) are mechanical side effects of `chat_lifecycle.py` — **never type or choose markers yourself**. Finished chats and handed-off chain pieces carry no special marker: they are archived (hidden from the sidebar, still found by search and opened by `conversation://`).
- **Concrete 2–6 word topics required (< 50 chars)**: `<Topic>` must consist of 2–6 specific
  words naming the concrete subsystem/component/bug/CL/PR and problem or action (e.g.
  `PR #194980 multi-span BiDi`, `FontCollection FaceCache thread safety`,
  `R27 Ban generic chat titles`).
  - **Strictly forbidden**: single-word titles (`Investigation`, `Debugging`, `Perf`, `Skia`),
    bare entity IDs without a subject (`cl/1405639`, `PR #12345`), and generic filler combinations
    (`Bug investigation`, `Code review`, `Fix issue`, `Task continuation`, `Исследование бага`).
  - `set-title`, `handoff`, and `next-task` validate `<Topic>` via `validate_chat_topic` and raise
    an error on generic/single-word topics (`handoff` derives a fallback from the chain state file
    or roadmap if needed). `context_guard` and `stop_guard` enforce titling on the first turn
    with `[TITLE GUARD]`.

Use only these commands (`python3 ~/.gemini/config/hooks/chat_lifecycle.py ...`):

| Command | When you call it |
|---|---|
| `set-title <id> "<Topic>"` | User asks to name/rename the chat, or on the first turn once you know the bug/CL/PR/commit topic to refine the auto-detected ID or replace a generic title. Pass bare `<Topic>`. |
| `auto-title <id> "<prompt>"` | Detect PR/CL/bug/commit in prompt and set title if untitled (invoked automatically by `context_guard.py`). |
| `handoff <id> [summary_file] [--notes "..."] [--next "..."] [--topic "<Topic>"] [--force]` | Context guard alert, or user asks to continue in a new chat. Writes/updates summary, launches visible continuation in the same project on `<id>`'s last model, verifies `sourceMetadata: null`, archives `<id>`. Refuses while `<id>` has running subagents or background tasks (timers included), since their reports would land in a hidden chat: wait for them or kill them; `--ignore-live` only after recording them in the state file. `--model <id\|name>` only on explicit user request; `--force` only when user explicitly asked for a second continuation. |
| `next-task [<id>] [<R_ID>] [--topic "<Topic>"] [--notes "..."] [--next "..."] [--force]` | After finishing a task (`roadmap.py set <id> <R_prev> done`), when `[NEXT-TASK BATON: YOURS ...]` is granted and the user confirms starting `<R_ID>` in a fresh chat: archives `<id>`'s chain and all completed sibling chats in the project (releasing worktree leases), launches an independent root chat (`⦿`) in the same project, claims `<R_ID>` as `doing`, links `<R_ID> → <new_id>`, and seeds `state/<new_id>.md`. |
| `summary-path <id>` | Returns the canonical summary path before writing with `write_file`. |
| `finalize <id>` | **Only** when the user explicitly says *"финал"* (or unambiguous equivalent): archives the chain; also archives completed sibling chats in the same project that own no open roadmap items. |
| `search "<query>" [--chain <id>]` | Before answering from memory about earlier work: `--chain` finds every user message / agent reply with the query across all pieces of `<id>`'s chain (hidden pieces included); without it, LS search over all chats (one hit per chat, symbols like `✓` never match). |
| `status [id]` | Inspect chain structure and summary paths. |
| `model [id] [--trailer]` | Inspect chat model or print `Assisted-by: ...` git trailer (`--trailer`). |
| `project-model [<p>] [<m>]` | View or set project preferred model in `~/.gemini/config/projects_models.json`. |
| `audit [--fix] [--project <id\|name>]` | Scan/repair title desync and broken markers; visible handed-off pieces and old `«…»`-closed chats are reported as `should_be_archived` (`--fix` archives them unless live work runs in them). |
| `archive [<id...>] [--chain] [--project <p>] [--keep <N>]` | Archive chats/chains via LS RPC and `.pbtxt`, or prune project `<p>` keeping `<N>` newest. |
| `auto-archive [--hook]` | Prune older chats across projects with retention policies. |

To move chats/chains between projects, run `python3 ~/.gemini/config/scripts/move_chats_to_project.py --project "<name_or_id>" [--chain] --restart <id...>` or `--list-unassigned`.

## Backlog & Scope Invariants across Handoff Chains

- **In-project chats: Epic Roadmap via `roadmap.py` only**: Manage the project roadmap exclusively via `python3 ~/.gemini/config/hooks/roadmap.py add|set|link|show|edit|reorder|move ...` (never edit `~/.gemini/config/roadmaps/` files directly, except the chain state file from `roadmap.py state-path <id>`).
  - **Backlog requests ("в бэклог: X")**: run `roadmap.py add <id> "X"`, confirm on one line (or bulleted list separated by blank lines for multiple items), then continue current work.
  - **Finishing a task & proposing the next roadmap item (`next-task`, Last-Chat Baton)**:
    - Once the current roadmap item is completed and marked `done` (`roadmap.py set <id> <R_prev> done`), obey the `[NEXT-TASK BATON: ...]` and `[VCS BATON: ...]` lines printed by `roadmap.py`:
      - **`[VCS BATON: PENDING ...]`**: Call `ask_question` with options `(Recommended) Commit + push`, `Commit only`, `Not now`. Stage and commit ONLY `own_dirty` files using explicit pathspecs (`git add -- <files> && git commit -m "..." -- <files>`), never `foreign_dirty` files. For Gerrit repos, `Commit + push` runs `amnesia-precommit` + mandatory corpus gate (`run_flutter_paragraph_tests.py` / `run_webparagraph_corpus.py`) + `git cl upload`, never setting CQ (`CQ+1`/`CQ+2`) or `Auto-Submit+1`. `stop_guard` blocks turn end if `done` was marked in the current turn while VCS baton is `PENDING` without calling `ask_question`.
      - **`[VCS BATON: CLEAN ...]`**: Report the already-pushed commit SHA and remote (`<sha> → origin/main`) or uploaded Gerrit CL (`cl/<N> PS <M>`) on one line without asking.
      - **`[NEXT-TASK BATON: HOLD ...]`** (other items are still `doing` in parallel chats, or another chat already holds the proposal baton) or **`[NEXT-TASK BATON: ALL DONE ...]`**: report the completed task and **stop without proposing `next-task`**.
      - **`[NEXT-TASK BATON: YOURS ...]`** (`0 doing` in the project): only this last finishing chat proposes `<R_next>`. For a **top-level item (`R2`, `R3`)** or whenever context is **`>= 30%`**, propose opening `<R_next>` in a fresh chat (`chat_lifecycle.py next-task <id> <R_next> --topic "<Topic>"`), which archives `<id>` and any completed sibling worker chats. For a small **sub-item (`R1.1`, `R1.2`)** with context **`< 30%`**, offer to either do it right here or open a new chat via `next-task`.
  - **Spin-offs**: run `roadmap.py link <id> <item ID> <new_chat_id>`.
  - **At handoff / `next-task`**: paste the roadmap block printed by `chat_lifecycle.py handoff` / `next-task` into the final message alongside `[👉 <Title>](conversation://<new_conversation_id>)`.
- **Parallel chats in one project share resources — claim before touching**:
  - Run `roadmap.py show` before editing or building; if an item is `doing` in another live chat, report the owner and wait, otherwise claim it with `roadmap.py set <id> <item> doing`.
  - **Worktree & shared-checkout guards (`hooks/pre_tool_guard.py`)**: If a code worktree is leased by another active chat (`[WORKTREE LEASE GUARD]`), create a separate linked worktree (`git worktree add`). In shared checkouts (`~/.gemini/config`), stage and commit only your own files with explicit pathspecs (`git add -- <files> && git commit -m "..." -- <files>`), and never `git push` when `@{u}..HEAD` contains another active chat's commits (`[FOREIGN-COMMIT PUSH GUARD]`). One deliverable document lives at one canonical path.
- **Wrong-Chat / Cross-Project Scope Guard**:
  - If `[SCOPE GUARD: WRONG-CHAT / WRONG-PROJECT RADAR]` shows a new user request belongs to another project or foreign workspace (never a sibling chat in the same project), **do not** start investigating or running commands — warn the user with `[👉 <Title>](conversation://<target_conv_id>)` and offer `agentapi send-message <target_conv_id> "<user message>"`. `pre_tool_guard.py` blocks cross-project edits/commits unless the user explicitly confirms in a subsequent turn (`.cross_project_override.json` / `.parallel_chat_override.json`).
- **Handoff Integrity (`Epic Roadmap` + `Immediate Next Step`)**:
  - Never drop or shrink pending `[ ]` roadmap items during handoff.
  - Label untested ideas as **`[UNVERIFIED HYPOTHESIS]`** (never `"Decided to do X"`) and record rejected approaches (`Rejected: <approach> because <reason>`).
  - On continuation start, run `roadmap.py show`, verify the state file against `git status`/tests, and check that any proposed design was proven in code.
  - Never ask whether to finalize, never auto-unarchive archived chats, and never query SQLite or hook sources directly for lifecycle state.
