# Gemini / Jetski Global Config (`~/.gemini/config`)

Personal global rules, lifecycle hooks, and configuration for Jetski (`~/.gemini/config`), compatible with both Linux and macOS.

## Contents

- `rules/` — Global agent rules:
  - `communication_style.md` — Communication and collaboration preferences (informal address, grammatical gender, critical & objective evaluation without sycophancy, Open Source / Git workflow).
  - `subagent_exploration.md` — Context hygiene (delegating broad codebase exploration to subagents) and reading API definitions before writing calls.
- `hooks.json` and `hooks/` — Lifecycle guard hooks:
  - `context_guard.py` — Monitors conversation context size and turn iteration limits, triggers automatic continuation handoffs (with timezone-aware `[HH:MM]` prefixes via `JETSKI_TIMEZONE`), and enforces the Two-Strike Debug Guard on consecutive command failures.
  - `pre_tool_guard.py` — Prevents blind edits without reading (`view_file`), blocks edits to generated/gitignored build caches, blocks infinite `tail -f` background daemons, verifies that `handoff_summary*.md` exists on disk before allowing `agentapi new-conversation`, auto-enriches `handoff_summary*.md` snapshots, and cleans up orphaned task log processes on handoff.
  - `stop_guard.py` — Ensures source code modifications are verified by a build/test command before the agent finishes its turn, and silences browser completion chimes on automated handoffs.
  - `project_activity_badge.py` — Syncs live active (` · ⟳ N`), blocked (` · ⚠ M`), and unread finished (` · ● K`) conversation counts into `~/.gemini/config/projects/<id>.json` so collapsed project folders in the sidebar show running and newly completed chat activity in real time via `ProjectUpdatesStream`.
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
