---
name: colleague-activity
description: >
  Check live and recent developer activity across Skia and Flutter (commits, Gerrit patchsets,
  PRs, reviews, and devicelab/infra triage). Trigger when asked who is active, who is working
  right now, or to check colleague commits.
---

# Colleague Activity Scanner

Quickly scans real-time developer activity across **Skia** (Gerrit via `gob-curl`)
and **Flutter** (GitHub via `gh api`), filtering out bots and autorollers.

## When to Use

Trigger this skill whenever the user asks:
- "Кто из моих коллег сейчас работает?" / "Кто сейчас активен?" / "Кто на месте?"
- "Посмотри активность в Скии и Флаттере" / "Кто коммитит / пушит?" / "Коллеги"
- "Who is active right now?" / "Check colleague activity" / "/colleague-activity"

## Quick CLI Usage

A global executable symlink `colleagues` is available in `~/.gemini/jetski/bin` and `~/.local/bin`:

```bash
# Scan both Skia and Flutter for the last 12 hours (default timezone: America/New_York)
colleagues

# Custom lookback window (e.g. 3 hours)
colleagues --hours 3

# Filter to Skia Gerrit or Flutter GitHub only
colleagues --skia-only
colleagues --flutter-only

# JSON output for structured inspection
colleagues --json
```

Direct script invocation:
```bash
python3 ~/.gemini/config/skills/colleague-activity/scripts/scan_activity.py --hours 3
```

## How to Present Results to the User

1. **Active Right Now (Last 30–45 min)**:
   - Highlight colleagues who made changes, pushed patchsets, or submitted reviews recently.
   - State times in user local time (**ET first**, with relative age e.g. `20:25 ET (3m ago)`).
   - Show the specific CL or PR subject and action.

2. **Mandatory Titles for PRs, Issues, and CLs (Never Bare Numbers)**:
   - Whenever mentioning, summarizing, or listing PR, Issue, or CL numbers, **always** include their descriptive title or subject next to the number.
   - Examples: `PR #194670 (Add PlatformDispatcher.onAlwaysUse24HourFormatChanged)` or `[Gerrit 1404676](...) ([rust jpeg] Support scaled decoding)`.
   - Never output bare numbers (e.g. `PR #194347` or `CL 1404676`) without their title or domain context.

3. **Domain Grouping (Text / Web / Paragraph vs Core / Engine vs Tooling)**:
   - Group colleagues into:
     - **Text / Web / Paragraph**: `dbebawy`, `mdebbar`, `harryterkelsen`, `YeungKC`,
       `wselna123`, `LongCatIsLooong`, `jlavrova`, etc.
     - **Core Graphics & Impeller**: `alexisdavidc`, `egdaniel`, `michaelludwig`,
       `thomsmit`, `nathanasanchez`, `gaaclarke`, `bdero`, `b-luk`, `zanderso`, etc.
     - **Framework & Tooling**: `justinmc`, `bkonyi`, `loic-sharma`, `jmagman`,
       `chunhtai`, `victorsanni`, `Renzo-Olivares`, `kevmoo`, etc.
     - **Infra & Devicelab**: `smolkaj`, `borenet`, `kentnguyen99`, `davidsegovia`, etc.

4. **Data Sources & Scope**:
   - **Skia Gerrit**: Captures uploaded patchsets, comments, votes, and reviews across `skia` and subprojects (`gob-curl`).
   - **Flutter GitHub**: Captures PR updates, review comments, issue comments, and merged commits (`gh api`).
   - **Buganizer (`b/...`)**: Checked via `/google/bin/releases/issues-cli/issues readonly search --query="..."` when inspecting internal bug activity.
   - **Google Chat**: Checked via `google_chat_google_chat` MCP tool `search_messages` when inspecting discussions in team spaces.
