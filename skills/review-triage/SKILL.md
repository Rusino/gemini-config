---
name: review-triage
description: >-
  Automated Code Review Triage and Action Engine for Gerrit CLs, Git PRs, and Piper diffs.
  Classifies incoming reviews into 4 actionable categories: (1) Mechanical/Routine (15s),
  (2) Non-domain/Reassign (30s), (3) Core Domain/Surgical Audit (3–5 min), (4) Low Hygiene/Pushback (1 min).
  Automatically isolates the core 5% logic hunks, verifies mechanical churn, drafts professional
  reassignment or pushback messages in English, and pinpoints exact files and line ranges so the reviewer
  maintains their analytical edge without drowning in diffs. Trigger when user asks to triage, review, or
  analyze an incoming CL/PR ("разбери СЛ", "сделай триаж ревью", "оцени СЛ", "посмотри этот ревью",
  "triage this CL/PR", "что делать с этим ревью").
---

# Code Review Triage & Action Engine (`review-triage`)

A fast, zero-fat triage system that protects the reviewer's cognitive focus, prevents loss of code analysis skills, and cuts code review time by up to 80%.

Instead of generating passive summaries that numb critical thinking, this skill classifies incoming CLs into four operational categories, executes mechanical checks automatically, and pinpoints the exact 5% of lines that require human eyes.

---

## The 4 Triage Categories & Decision Protocol

```
                        Incoming CL / PR / Diff
                                   │
               ┌───────────────────┼───────────────────┐
               ▼                   ▼                   ▼
        [Out of Domain?]     [Low Hygiene?]     [Mechanical?]
         (No text/layout)     (Missing tests,     (Rename, format,
               │              massive scope)      revert, bump)
               │                   │                   │
         YELLOW (30s)         ORANGE (1m)         GREEN (15s)
          REASSIGN             PUSHBACK           MECHANICAL
               │                   │                   │
               └───────────────────┼───────────────────┘
                                   │  (None of above)
                                   ▼
                              RED (3–5m)
                           SURGICAL AUDIT
                      (Core 5% logic isolated)
```

### 1. 🟢 `[MECHANICAL / ROUTINE]` (User time: 15–30 seconds)
- **Criteria**: Reverts, pure code formatting (`clang-format`), token renames across callers, version bumps, build config updates, or repetitive boilerplate.
- **Agent Action**:
  1. Verifies that changes follow the uniform pattern across all touched files without rogue modifications.
  2. Produces a 2-line attestation summary.
  3. Prepares a short English approval comment (`LGTM. Verified mechanical churn; no unintended side effects.`).
- **User Action**: Quick glance at the attestation, post approval.

### 2. 🟡 `[REASSIGN / OUT OF DOMAIN]` (User time: 30 seconds)
- **Criteria**: Changes belong entirely to other subsystems (e.g., Vulkan/Metal GPU backends, Graphite core, build/infra, shell embeddings).
- **Agent Action**:
  1. Identifies touched subsystems and scans file history or ownership hints.
  2. Recommends the specific team or primary owner.
  3. Prepares a polite, professional reassignment note in English.
- **User Action**: Copy message, reassign in Gerrit/GitHub, remove self from reviewers.

### 3. 🔴 `[SURGICAL AUDIT / CORE DOMAIN]` (User time: 3–5 minutes)
- **Criteria**: Changes in core text, SkParagraph, WebParagraph, Unicode, font metrics, BiDi, caret geometry, line breaking, or rendering.
- **Agent Action (Noise Stripping)**:
  1. Filters out tests, build files, markdown, and trivial getters/setters.
  2. Pinpoints the **top 1–2 hunks (20–50 lines)** where state mutations or branching algorithms actually change.
  3. Provides exact clickable links: `[File.cpp:L120–L150]`.
  4. Formulates **3–4 domain invariant checks** (Hot-path zero-allocation, boundary/empty handling, BiDi run boundaries, cache invalidation).
- **User Action**: Read **ONLY** the pinpointed 20–50 lines with personal critical eyes. Brain engages fully, analytical edge is sharpened, 95% of reading time is saved.

### 4. 🟠 `[PUSHBACK / HYGIENE REQUIRED]` (User time: 1 minute)
- **Criteria**: 
  - **Architectural Layer Inversion**: Violating layer boundaries in SkParagraph (e.g., attempting to smuggle UTF-8 text/clusters into `SkTextBlob` via `allocRunTextPos` for PDF text extraction; `SkTextBlob` in SkParagraph is strictly an ephemeral drawing cache, while structured document semantics belong at the document/scene layer).
  - Substantial implementation logic churn (>60 lines) with **0 unit tests**.
  - Empty or cryptic description ("fix bug", "update logic") without explaining motivation or edge cases.
  - Giant unstructured diff mixing preparatory refactoring with feature code.
- **Agent Action**:
  1. Flags the exact hygiene or architectural deficit.
  2. Drafts a constructive, objective pushback note in English explaining why safe review is blocked or architectural separation is breached.
- **User Action**: Review draft, post to author, return CL for revision before investing deep review time.

---

## How to Run the Triage CLI

The triage script runs directly on Cloudtop Linux and macOS:

```bash
# Gerrit CL by ID or URL (Skia, Chromium)
python3 ~/.gemini/config/skills/review-triage/scripts/triage_cl.py 1398536
python3 ~/.gemini/config/skills/review-triage/scripts/triage_cl.py https://skia-review.googlesource.com/c/skia/+/1398536

# Google3 Piper CL
python3 ~/.gemini/config/skills/review-triage/scripts/triage_cl.py cl/123456789

# GitHub PR URL
python3 ~/.gemini/config/skills/review-triage/scripts/triage_cl.py https://github.com/flutter/flutter/pull/150000

# Local Git commit / diff / branch
python3 ~/.gemini/config/skills/review-triage/scripts/triage_cl.py HEAD
python3 ~/.gemini/config/skills/review-triage/scripts/triage_cl.py origin/main..HEAD

# Raw diff from stdin or file
git diff | python3 ~/.gemini/config/skills/review-triage/scripts/triage_cl.py
python3 ~/.gemini/config/skills/review-triage/scripts/triage_cl.py --diff-file /path/to/change.patch
```

---

## Review & VCS Guidelines (Strict Rules)
1. **English-Only Review Surface**: All generated review comments, reassignment notes, and pushback messages must be strictly in English. Direct discussion with the user remains in Russian.
2. **Never Auto-Publish or Resolve**: The agent must NEVER publish review comments to Gerrit/GitHub or resolve threads automatically. Drafts are presented in chat for the user to review and post.
3. **No Auto-Submit or CQ Votes**: Never trigger CQ votes (`Commit-Queue+1`/`+2`) or merge actions.
