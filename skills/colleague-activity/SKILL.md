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
- "Посмотри активность в Скии и Флаттере" / "Кто коммитит / пушит?"
- "Who is active right now?" / "Check colleague activity"

## Quick CLI Usage

```bash
# Scan both Skia and Flutter for the last 12 hours (default timezone: America/New_York)
python3 ~/.gemini/config/skills/colleague-activity/scripts/scan_activity.py

# Custom lookback window (e.g. 6 hours)
python3 ~/.gemini/config/skills/colleague-activity/scripts/scan_activity.py --hours 6

# JSON output for structured inspection
python3 ~/.gemini/config/skills/colleague-activity/scripts/scan_activity.py --json
```

## How to Present Results to the User

1. **Active Right Now (Last 30–45 min)**:
   - Highlight colleagues who made changes, pushed patchsets, or submitted reviews recently.
   - State times in user local time (**ET first**, with relative age e.g. `20:25 ET (3m ago)`).
   - Show the specific CL or PR subject and action.

2. **Domain Grouping (Text / Web / Paragraph vs Core / Engine vs Tooling)**:
   - Group colleagues into:
     - **Text / Web / Paragraph**: `dbebawy`, `mdebbar`, `harryterkelsen`, `YeungKC`,
       `wselna123`, `LongCatIsLooong`, `jlavrova`, etc.
     - **Core Graphics & Impeller**: `alexisdavidc`, `egdaniel`, `michaelludwig`,
       `thomsmit`, `nathanasanchez`, `gaaclarke`, `bdero`, `b-luk`, `zanderso`, etc.
     - **Framework & Tooling**: `justinmc`, `bkonyi`, `loic-sharma`, `jmagman`,
       `chunhtai`, `victorsanni`, `Renzo-Olivares`, `kevmoo`, etc.
     - **Infra & Devicelab**: `smolkaj`, `borenet`, `kentnguyen99`, `davidsegovia`, etc.

3. **Disclosures & Limitations**:
   - Gerrit captures uploaded patchsets, comments, and reviews; local unpushed commits
     are invisible.
   - GitHub tracks PR updates, review comments, issue comments, and merged commits.
