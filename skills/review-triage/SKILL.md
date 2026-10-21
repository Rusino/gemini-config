---
name: review-triage
description: >-
  Automated Code Review Triage and Action Engine for Gerrit CLs, Git PRs, and Piper diffs.
  Classifies incoming reviews into 4 actionable categories: (1) Mechanical/Routine (15s),
  (2) Non-domain/Reassign (30s), (3) Core Domain/Surgical Audit (3–5 min), (4) Low Hygiene/Pushback (1 min).
  Automatically isolates the core 5% logic hunks, verifies mechanical churn, runs a two-stage
  Hunt then Refute pass (keeper_skeptic with XFAMILY_REFUTE auto-routing) backed by the Persistent
  Finding Ledger (`finding_ledger.py`) to eliminate and cache false-positive review comments across
  patchsets, and by default executes the `companion-cl-review` protocol (checking out the author's
  exact patchset without rebasing, running adversarial contract tests, classifying into the 5-Case
  Decision Matrix, and uploading a silent stacked WIP companion CL with PS1=tests and PS2=fix when
  warranted, unless the user explicitly opts out with "без компаньона" / "только текстом"). Trigger when user asks to triage, review, or analyze an incoming CL/PR ("разбери СЛ",
  "сделай триаж ревью", "оцени СЛ", "посмотри этот ревью", "triage this CL/PR",
  "что делать с этим ревью").
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
  2. Verifies that any candidate `@google.com` owner/reviewer is currently an active Googler (`id -u <ldap> >/dev/null 2>&1` on Linux) and is **not** a departed Skia team member (`bungeman`, `kjlubick`, `brianosman`, `jvanverth`, `jsimmons`, `gregdaniel`, `nifong`).
  3. Recommends the specific team or active primary owner.
  4. Prepares a polite, professional reassignment note in English.
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

## Mandatory Ruthless Audit & Skeptic Refutation Gate for Every Surviving CL

Triage output alone is never the final word. Every CL that stays in the queue after triage (any
category except a clear 🟡 REASSIGN) must also go through the
`ruthless-code-audit` (`~/.gemini/config/skills/ruthless-code-audit/SKILL.md`)
skill and its **Stage 2.5 Skeptic Refutation Pass (`keeper_skeptic`)** before review drafts are presented:
1. Fetch the current patchset into a separate worktree (never switch branches in an existing
   checkout): `git fetch https://skia.googlesource.com/skia refs/changes/<NN>/<CL>/<PS>`. If the
   stored credentials are rejected, fetch anonymously with
   `git -c credential.helper= -c http.cookiefile=/dev/null fetch ...`.
2. Run Stage 1 (`ruthless_audit.py ... --diff <parent>`) and Stage 2 (semantic review of the
   pinpointed hunks against the full file, not just the diff). Both `triage_cl.py` and
   `ruthless_audit.py` automatically query `~/.gemini/config/scripts/finding_ledger.py` for the
   target repo/CL/files — pass any prior refuted findings into the Stage 2 auditor so repeat
   patchsets never re-raise already-refuted claims.
   - **Bungeman lens (Skia C++ / text / font diffs)**: as part of Stage 2, run `bungeman-review`
     in **lens mode** (`~/.gemini/config/skills/bungeman-review/SKILL.md`, Steps 1–3: precedent
     search + Layer 1/2 invariants). Its candidates join the same Stage 2.5 packet; confirmed ones
     become the edge cases for `companion-cl-review` contract tests. Unattended queue sweeps
     (`run_queue_triage.py`) run only the precedent search.
3. **Run Stage 2.5 (`keeper_skeptic` Batched Refutation Pass)**:
   - Whenever Stage 2 raises any `HIGH` / `CRITICAL` candidate defect or architectural pushback, spawn **one batched `keeper_skeptic` subagent** in a clean context to **REFUTE** the candidates using actual callers (`git grep`, `grep -rn`, `code_search`), comments, and tests.
   - **Automatic Model Routing (`XFAMILY_REFUTE`)**: Use `Model="flash"` for routine local checks; automatically escalate to `Model="pro"` if any candidate involves concurrency/lifetimes, BiDi/UTF-8/UTF-16/grapheme index math, `SkParagraph` line metrics, dropped historical guards (`git log -S`), or cross-backend `SkParagraph` ↔ `WebParagraph` symmetry.
   - **Quota Fallback**: On `pro` quota exhaustion, auto-downgrade to `Model="flash"` (`[Verified via Flash fallback (Pro quota exhausted)]`); if all models fail, mark the draft `[UNVERIFIED: Quota Exhausted]` and alert the user.
   - **Apply the Three Skeptic Verdicts to Review Drafts & Persist Refutations**:
     - **`REFUTED` (`path:line` guard, caller `SkASSERT`, or documented intent)**: Drop silently—never embarrass the human reviewer with a false positive on Gerrit/GitHub. Record every `REFUTED` item (and any review comment the user later dismisses in chat) in the Persistent Finding Ledger via `python3 ~/.gemini/config/scripts/finding_ledger.py add --repo <repo_or_project> --file <path> --symbol "<sym>" --claim "<claim>" --reason "<evidence>" [--scope repo|cl --cl <cl_id>]`.
     - **`CONFIRMED` (`entry -> path:line -> bad outcome`)**: Proceed to the **Default Companion WIP CL Execution (`companion-cl-review`)** stage below; drop any LGTM draft that a confirmed defect contradicts.
     - **`UNPROVEN` (trace hits an external contract or unverified product intent)**: Convert into a polite, specific **Question for the CL Author** in English (stating what was traced and what remains open) rather than asserting a bug.

---

## Default Companion WIP CL Execution (`companion-cl-review`)

For every targeted single-CL review in `🔴 SURGICAL AUDIT / CORE DOMAIN` (or `🟠 PUSHBACK`) that remains after triage and audit, **always follow the `companion-cl-review` (`~/.gemini/config/skills/companion-cl-review/SKILL.md`) skill by default** (unless the user explicitly opts out with *"без компаньона"*, *"только текстом"*, *"without companion CL"*, or when running an unattended batch queue sweep via `run_queue_triage.py`, which stops at triage + static audit until the user selects a specific CL):

1. Evaluate the CL against the **5-Case Decision Matrix** in `~/.gemini/config/skills/companion-cl-review/SKILL.md`:
   - **Case 1 (Ideal CL)** or **Case 2 (Near-Ideal / Minor Nits)**: Do **not** create a companion WIP CL; prepare a concise `LGTM` or inline nit comments directly.
   - **Case 3 (No Failing Tests / Coverage or Cleanup Only)**: Upload a **1-patchset WIP CL (`PS1` only)** if the extra edge-case tests or structural cleanup exceed 15–20 lines (otherwise use inline comments).
   - **Case 4 (Fundamental Flaw / Unfixable Without Rewrite)**: Upload a **1-patchset WIP CL (`PS1` = Failing Reproducer Tests ONLY)** and draft a constructive pushback message.
   - **Case 5 (Edge-Case Defects + Clean Fix)**: Upload a **2-patchset WIP CL** stacked directly on the author's exact patchset SHA without rebasing (`PS1` = failing contract tests with timeless comments; `PS2` = production fix + passing tests, uploaded via `git push origin HEAD:refs/for/main%wip` with zero reviewers) and draft the collaborative English review comment linking `PS1` and `PS2`.
2. If the CL is the user's own, follow **Self-Review Mode** in `companion-cl-review` instead: no companion CL, the same patchsets go directly into the user's CL.
3. **Review message tone**: draft in the collaborative `companion-cl-review` Phase 5 style by default. Render it in Ben Wagner's voice (`bungeman-review` voice mode) only when the user asks for Ben; for Case 4 pushback, offer the voice-mode variant as an option.

---

## Review & VCS Guidelines (Strict Rules)
1. **English-Only Review Surface**: All generated review comments, reassignment notes, and pushback messages must be strictly in English. Direct discussion with the user remains in Russian.
2. **Never Auto-Publish or Resolve**: The agent must NEVER publish review comments to Gerrit/GitHub or resolve threads automatically. Drafts are presented in chat for the user to review and post.
3. **No Auto-Submit or CQ Votes**: Never trigger CQ votes (`Commit-Queue+1`/`+2`) or merge actions.
