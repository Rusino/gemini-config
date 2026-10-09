# Gemini / Jetski Global Config (`~/.gemini/config`)

Personal global rules, lifecycle hooks, and configuration for Jetski (`~/.gemini/config`), compatible with both Linux and macOS.

## Contents

- `rules/` — Global agent rules:
  - `communication_style.md` — Communication and collaboration preferences (informal address, grammatical gender, critical & objective evaluation without sycophancy, Open Source / Git workflow).
  - `subagent_exploration.md` — Context hygiene (delegating broad codebase exploration to subagents) and reading API definitions before writing calls.
- `hooks.json` and `hooks/` — Lifecycle guard hooks:
  - `context_guard.py` — Monitors conversation context size and turn iteration limits, triggers automatic continuation handoffs (with timezone-aware `[HH:MM]` prefixes via `JETSKI_TIMEZONE`), and enforces the Two-Strike Debug Guard on consecutive command failures.
  - `pre_tool_guard.py` — Prevents blind edits without reading (`view_file`), blocks edits to generated/gitignored build caches, blocks infinite `tail -f` background daemons, verifies that `handoff_summary*.md` exists on disk before allowing `agentapi new-conversation`, auto-enriches `handoff_summary*.md` snapshots, and cleans up orphaned task log processes on handoff.
  - `pre_tool_guard_notice.py` — When `pre_tool_guard.py` fails open (one of its guards raised and the call was allowed unguarded), it leaves `logs/pre_tool_guard_failure.json`; this PreInvocation hook shows that failure to the agent as an ephemeral message, at most once per chat every 15 minutes.
  - `stop_guard.py` — Ensures source code modifications are verified by a build/test command before the agent finishes its turn, and silences browser completion chimes on automated handoffs.
  - `project_activity_badge.py` — Syncs live active (` · ⟳ N`), blocked (` · ⚠ M`), and unread finished (` · ● K`) conversation counts into `~/.gemini/config/projects/<id>.json` so collapsed project folders in the sidebar show running and newly completed chat activity in real time via `ProjectUpdatesStream`.
- `scripts/` — Helper utilities and Jetski Hub sidebar customizations:
  - `move_chats_to_project.py` — Moves conversations (or entire handoff chains) between projects and lists unassigned top-level conversations.
  - `jetski_closed_filter.js` — Client-side sidebar filter toggle injected next to `Display Options` that hides conversations in Redux without archiving them: *Closed hidden* hides finalized chains (`«`, `‹✓›`, `»`, `«»`) and handed-off steps of open chains (`✓`); *Active only* also hides open chain roots (`▸`), leaving only `⦿` and unmarked chats.
  - `patch_jetski_hub_binary.py` — `ExecStartPre` helper for `jetski-hub.service` that syncs `jetski_closed_filter.js` into `/static/artifacts/jetski-closed-filter.js` and patches the extracted `jetski-hub-server` binary so `index.html` loads the filter script automatically.
- `githooks/pre-commit` — Runs `tests/run_all_tests.py` on the staged snapshot and aborts the commit if it fails. Git does not version `core.hooksPath`, so enable it once per clone (see Setup).
- `documentation/` — Detailed architecture and design docs:
  - [`long_context_guards_guide.md`](./documentation/long_context_guards_guide.md) — *Long-Context Error & Hallucination Prevention System for Jetski* (including Mermaid architecture diagram).

> **Note:** `config.json` and `projects/` are excluded via `.gitignore` because they contain machine-specific paths (`/usr/local/...` vs `/Users/...`), remote-control hostnames, and runtime state mutated during execution.

## Setup on macOS (or Another Machine)

If `~/.gemini/config` does not exist yet:

```bash
mkdir -p ~/.gemini
git clone git@github.com:Rusino/gemini-config.git ~/.gemini/config
```

If `~/.gemini/config` was already created by Jetski on first launch (e.g., it already contains a local `config.json`):

```bash
cd ~/.gemini/config
git init
git remote add origin git@github.com:Rusino/gemini-config.git
git fetch origin
git checkout -f main
```

In both cases enable the versioned pre-commit hook:

```bash
git -C ~/.gemini/config config core.hooksPath githooks
```

## Syncing Changes

Push updates from any machine:

```bash
git -C ~/.gemini/config add -A
git -C ~/.gemini/config commit -m "Update config"
git -C ~/.gemini/config push
```

Pull updates on another machine:

```bash
git -C ~/.gemini/config pull --rebase
```
