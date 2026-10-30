---
name: review-triage
description: >-
  Automated Code Review Triage and Action Engine for Gerrit CLs, Git PRs, and Piper diffs
  (4 triage categories, core 5% hunk isolation, `keeper_skeptic` refutation + `finding_ledger.py`,
  and default `companion-cl-review` WIP CL execution unless opted out with "без компаньона" /
  "только текстом"). Trigger when the user asks to "разбери СЛ", "сделай триаж ревью", "оцени СЛ",
  "посмотри этот ревью", "triage this CL/PR", or "что делать с этим ревью".
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

## Mandatory Ruthless Audit & Skeptic Refutation Gate for Surviving CLs

Triage output alone is never the final word. Every `🔴 SURGICAL AUDIT / CORE DOMAIN` CL (and any `🟠 PUSHBACK` CL where specific technical defects are cited alongside hygiene feedback; `🟢 MECHANICAL` and `🟡 REASSIGN` skip heavy subagent audits) must go through [`ruthless-code-audit`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/ruthless-code-audit/SKILL.md) and its **Stage 2.5 Skeptic Refutation Pass (`keeper_skeptic`)** before review drafts are presented:
0. **Load Follow-Up Patchset Context & Commit Message (Pass to Any Auditor Subagent)**:
   - Always inspect the `### 🔄 Follow-Up Patchset & Prior Review Thread` and `### 📜 Commit Message` sections emitted by `triage_cl.py` (and include them verbatim in the prompt of any subagent auditing the CL).
   - **Follow-up mode (`PS > 1` with prior comments from `YOU`)**: Fetch both `last_reviewed_ref` and `current_ref`, inspect `git diff <last_reviewed_sha> <current_sha>`, and verify whether the author addressed the user's prior comments. **Never** contradict the user's earlier requests or ask to split out/revert code folded in from a companion WIP CL.
   - **Commit message grounding**: Read the full commit message before drafting comments; never ask the author to document something already explained in the commit message.
1. Fetch the current patchset into a separate worktree per [`companion-cl-review` Phase 1](file:///usr/local/google/home/jlavrova/.gemini/config/skills/companion-cl-review/SKILL.md). To inspect baseline versions of modified files without switching branches, use `git cat-file -p <base_sha>:<path>`.
2. Run Stage 1 (`ruthless_audit.py ... --diff <parent>`) and Stage 2 (semantic review of the pinpointed hunks against the full file, passing prior refuted findings from `finding_ledger.py`). For Skia C++ / text / font diffs, include `veteran-review` **lens mode** (`~/.gemini/config/skills/veteran-review/SKILL.md`, Steps 1–3) in the same Stage 2.5 packet.
3. **Run Stage 2.5 (`keeper_skeptic` Batched Refutation Pass)**:
   - Whenever Stage 2 raises any `HIGH` / `CRITICAL` candidate defect or architectural pushback, spawn **one batched `keeper_skeptic` subagent** following [`plugins/keeper/agents/keeper_skeptic.md`](file:///usr/local/google/home/jlavrova/.gemini/config/plugins/keeper/agents/keeper_skeptic.md) (`XFAMILY_REFUTE` routing, Quota Fallback Ladder, and `finding_ledger.py add`).
   - **`REFUTED`**: Drop **completely** (never rephrase as a "polite question" or doc nit) and persist via `finding_ledger.py add`. If all candidates are `REFUTED`, the draft is a clean `LGTM`.
   - **`CONFIRMED`**: Proceed to **Default Companion WIP CL Execution (`companion-cl-review`)** below.
   - **`UNPROVEN`**: Convert into a polite, specific **Question for the CL Author** in English.

---

## Default Companion WIP CL Execution (`companion-cl-review`)

For every targeted single-CL review in `🔴 SURGICAL AUDIT / CORE DOMAIN` (and `🟠 PUSHBACK` when providing failing reproducer tests as Case 4; skip worktree builds for pure no-description/unsplit-refactor pushbacks) that remains after triage and audit, **always follow [`companion-cl-review`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/companion-cl-review/SKILL.md) by default** (unless the user explicitly opts out with *"без компаньона"*, *"только текстом"*, *"without companion CL"*, or during unattended `run_queue_triage.py` sweeps):

1. Evaluate the CL against the **5-Case Decision Matrix** and execute Phases 1–5 in [`companion-cl-review/SKILL.md`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/companion-cl-review/SKILL.md) (including **Self-Review Mode** for the user's own CLs and **Dry Mode** when asked for *"dry"* / *"сухой прогон"*).
2. **Review message tone**: draft in the collaborative `companion-cl-review` Phase 5 style by default; render in `veteran-review` voice mode only when requested (or offer it as an option for Case 4 pushback).
3. **Pin External Source Links with `;drc=<commit_sha>` (`R19`)**: Whenever a drafted review comment cites a line-numbered `source.chromium.org` or Gitiles URL (`;l=<line>`), always include `;drc=<full_commit_sha>` so line numbers never drift after upstream commits.

---

## Skia Canary & ASAN Recommendation (Others' Skia CLs)

For surviving Skia CLs in `🔴 SURGICAL AUDIT` or `🟠 PUSHBACK`, run
[`skia-canary-advisor`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/skia-canary-advisor/SKILL.md)
(`canary_advisor.py advise --cl <N>`). Run ASAN automatically.

- **External Authors**: Trigger non-G3 canaries (`Canary-Flutter`, `Canary-Chromium`,
  `Canary-Android`) automatically. For `Canary-G3`, it requires a Googler author or Googler
  `Code-Review+1` (`g3_canary.go:134`). Follow the **External Author Canary-G3 Submit-Hold Protocol**:
  when approving, draft the `/PATCHSET_LEVEL` comment with `unresolved: True` holding the submit
  until Canary-G3 passes. Run Canary-G3 immediately after the user publishes CR+1. If Canary-G3 fails,
  follow the triage procedure in `skia-canary-advisor`.
- **Internal Authors**: Suggest recommended canaries in chat; do not ask them in Gerrit comments.
- **Background Watcher & Wakeup Protocol**: Immediately after triggering `git cl try`, launch
  `python3 ~/.gemini/config/skills/skia-canary-advisor/scripts/canary_advisor.py watch --cl <N> --save-draft`
  in the background (`WaitMsBeforeAsync=500`, `NotificationTimeoutSeconds=5400`).
  - **On Red Canary (`UNRELATED_HEAD_BREAKAGE`)**: `watch --save-draft` automatically saves a
    sanitized Gerrit `/PATCHSET_LEVEL` draft (only when Canary is red; never when green) and wakes
    the chat.
  - **On Red ASAN**: Never touch external/others' CLs directly. Create or update the `%wip`
    companion CL per `companion-cl-review` (`PS1` = reproducer test, `PS2` = fix), update the
    parent CL's `/PATCHSET_LEVEL` draft (`unresolved: true`) with the ASAN trace and companion link,
    and notify the user.



## Report Contract: Execution Ledger & Finding Statuses

Every review report (chat summary or artifact) must make skipped work visible. The Stop hook
(`~/.gemini/config/hooks/stop_guard.py`, `[REVIEW DISCIPLINE GUARD]`) blocks finishing when a
named skill's `SKILL.md` was not read, when a report with a `Case 1-5` verdict lacks an
`Execution Ledger` heading, or when a drafted testharness test has no `assert_*` call.

1. **`## Execution Ledger` comes first**: one row per step of this skill and of
   `companion-cl-review` (Phase 0 threads, triage script, Stage 1/2 audit, Stage 2.5 skeptic,
   checkout, Gate A build/run, classification, draft) with `done` / `skipped` and the reason.
   A skipped step is never described as done anywhere else in the report or the chat reply.
2. **Every finding carries exactly one status**: `CONFIRMED` (entry -> `path:line` -> bad
   outcome, plus the `keeper_skeptic` verdict), `REFUTED` (dropped from drafts, recorded in the
   ledger), `UNPROVEN` (becomes a question for the author), or `UNVERIFIED` (no refutation pass
   ran). Hypotheses handed over by the user or a parent chat start as `UNVERIFIED`; the word
   "confirmed" without a skeptic verdict is forbidden.
3. **Drafted tests must be able to fail**: each test asserts the contract (pixel/`getImageData`
   comparison or reftest for drawing behavior, `assert_*` for return values). A test that only
   calls the API is not a reproducer.

---

## Review & VCS Guidelines (Strict Rules)
1. **English-Only Review Surface**: All generated review comments, reassignment notes, and pushback messages must be strictly in English. Direct discussion with the user remains in Russian.
2. **Never Auto-Publish or Resolve (Always Save Gerrit Drafts on the Parent CL)**: The agent must NEVER publish review comments to Gerrit/GitHub or resolve threads automatically. For targeted external Gerrit CL reviews (outside Dry Mode and Self-Review Mode), always save the prepared English review comment (including the companion WIP CL link and patchset guide when a companion CL is uploaded) as a `/PATCHSET_LEVEL` draft directly on the parent CL's current patchset (`PUT https://<host>/a/changes/<CL>/revisions/<PS>/drafts`, updating any existing `/PATCHSET_LEVEL` draft on `<PS>` rather than duplicating), and also present the draft in chat for the user to review and publish.
3. **Gerrit Drafts & Thread Follow-ups**:
   - Save the prepared review comment on the parent Gerrit CL via `PUT https://<host>/a/changes/<CL>/revisions/<PS>/drafts` using the OAuth token from `printf "protocol=https\nhost=%s\n\n" "<host>" | git-credential-luci get` and Python `json.dumps({"path": "/PATCHSET_LEVEL", "message": msg, "unresolved": unresolved})` (`unresolved: True` when there are defects/questions, `False` for clean approvals), including the full review explanation and companion WIP CL link/patchset guide. If `git-credential-luci` requires reauth, warn the user and still output the full draft in chat.
   - A follow-up after a new patchset is saved as a standalone `/PATCHSET_LEVEL` draft on the CURRENT patchset (no `in_reply_to`), containing only new text.
   - Use `in_reply_to` only for inline (`file:line`) replies or when the user explicitly asks to answer inside a thread; never include or re-quote already-published text in a draft.
   - Never resolve the old thread unless the user asks; if asked, resolve via a minimal "Done" reply in that thread.
4. **No Auto-Submit or CQ Votes**: Never trigger CQ votes (`Commit-Queue+1`/`+2`) or merge actions.
5. **Respect `PRESUBMIT.py` Include & Forward-Declaration Sorting**: In Skia, `PRESUBMIT.py` runs
   `python3 tools/rewrite_includes.py --dry-run` on all modified files and fails CQ
   (`Housekeeper-OnDemand-Presubmit`) if existing `#include` lines or forward declarations
   (`class Foo;`) in a touched header/source file are not sorted alphabetically. Never flag
   alphabetical `#include` or forward-declaration reordering in a modified file as "unrelated
   churn".
6. **No Unmeasured Test-Runtime or Header Doc-Comment Bikeshedding**: Never question unit-test
   loop counts (e.g., `65536` iterations) as slow on Debug bots without inspecting the mock/helper
   implementation and measuring actual `dm` runtime, and never hold up approval asking to add doc
   comments to headers whose existing methods have no doc comments when the contract is already
   stated in the commit message.
7. **External Author Canary-G3 Submit-Hold Protocol**: When approving an external author's Skia CL
   that requires `Canary-G3` (`g3_canary.go:134`), do not draft a bare approval. Prepare the
   `/PATCHSET_LEVEL` draft with `unresolved: True` containing the standard submit-hold text to prevent
   premature submit while Canary-G3 (~55 min) runs:
   > The change looks good to me (Code-Review+1). I leave this comment unresolved on purpose: it blocks
   > the submit until the Canary-G3 try job passes. Canary-G3 builds and tests the google3 clients of
   > Skia with this change. Skia starts it for an external author only after a Googler votes
   > Code-Review+1, so I vote first and hold the submit with this comment. I will resolve it when
   > Canary-G3 is green, or report the failure here. Please do not resolve or override it before that.
   After the user publishes CR+1 with this hold comment, immediately trigger Canary-G3 (`git cl try
   -i <CL> -B skia/skia.primary -b Canary-G3`). If red, follow the Canary-G3 triage procedure in
   [`skia-canary-advisor`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/skia-canary-advisor/SKILL.md).
8. **Daily Queue Summary Shows Open Issues Only**: In `run_queue_triage.py` summaries, never list or
   mention GitHub issues that are already closed (including ones closed during the window or still
   receiving bot activity); report only open issues and the user's own open PRs.
9. **Flight Deck Triage Persistence (`triage_store.py`)**: When running daily queue triage
   (`run_queue_triage.py`) or per-CL audit workflows (`wf*.md`), save the structured audit report
   for Flight Deck via:
   `python3 ~/.gemini/config/scripts/triage_store.py save-audit --target <ID> --report-file <md>`
   (or run `triage_store.py ingest-tmp` to ingest all `/tmp/wf*.md` and `/tmp/triage_*.md` files).
   This displays category badges, estimated review times, one-line summaries, and 1-click
   ready-to-copy drafts next to each CL/PR in Flight Deck.
