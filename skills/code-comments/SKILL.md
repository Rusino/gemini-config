---
name: code-comments
description: >-
  Add, rewrite, compress, or remove code comments across files, commits, or git diffs.
  Enforces concise English rationale comments ("why" over "what", timeless invariants
  for fixes and deletions, 1–4 line limit, periods allowed inside multi-sentence blocks
  but never at the very end of a comment or block). Use when the user asks to clean up,
  add, review, or fix comments ("причеши комментарии", "прокомментируй дифф", "убери
  лишние комментарии", "проверь комментарии").
---

# Code Comment Curator (`code-comments`)

Curate code comments so that every remaining or newly added comment earns its place by explaining **why** — never **what** — in clear, compact English.

## 1. Core Formatting & Style Contract

1. **Language**: English only, imperative or declarative prose, zero filler.
2. **Length & Punctuation**:
   - Prefer **1 line** (`// ...`) for straightforward invariants.
   - Allow **2–4 lines** for nuanced domain mechanics or multi-part invariants.
   - **Periods (`.`)**: Allowed *between* sentences inside a multi-line comment block, **strictly forbidden at the very end** of a single-line comment or at the end of the last line of a comment block.
3. **No Slop / No Formatting Noise**:
   - No numbered tutorial steps (`// 1. Setup`, `// 2. Run`).
   - No markdown emphasis (`*word*`, `**word**`) or ASCII dividers (`// ---`, `// ===`).
   - No narrative headers above unit test assertions (`REPORTER_ASSERT`, `expect(...)`).

## 2. Comment Ownership Guardrail (Strict)

- **Only rewrite or delete comments that YOU (the AI) added and explicitly remember adding** during the current task/session.
- **Never silently alter or delete pre-existing comments or user-written comments** — even if they have trailing periods, look verbose, or seem outdated.
- If a pre-existing or unknown comment looks wrong, noisy, or violates style rules, **ask the user first** and show the proposed change before touching it.

## 3. Comment Taxonomy: Add, Rewrite, or Delete

### A. What to ADD (Including 1-Line Non-Trivial Changes)

Even if a change touches **only a single line** (1 line added, modified, or deleted), if it is **non-trivial** (anything beyond a typo, formatting, or obvious dead variable cleanup), **you must add a comment** explaining why:

1. **Design Rationale ("Why this way")**
   Explain why a non-obvious algorithm, ordering, or data layout was chosen over the naive alternative.
   ```cpp
   // Linear scan beats hash lookup for typical run counts (< 8)
   ```

2. **Fixed Bug / Edge-Case Invariant ("Why this line/branch exists or changed")**
   Even for a 1-line condition change (`<` to `<=`, `+ 1`, flag flip, different argument), **never** write a commit-log comment (`// Fixed crash when empty`). State the timeless domain invariant that requires the line to be written this way:
   ```cpp
   // Empty runs still advance caret metrics by the font's ascent and descent.
   // Skipping them collapses line height on blank trailing lines
   ```

3. **Load-Bearing Absence / Negative Invariant ("Why a line/block was deleted or omitted")**
   When `git diff` shows even a single deleted line (or a deliberately omitted check/reset) whose removal is non-trivial, place a comment at the site explaining why it must **not** happen here — without referencing "Removed" or "Previously":
   ```cpp
   // Do not clamp cluster indices here. HarfBuzz normalizes out-of-range
   // clusters upstream, and clamping masks broken UTF-16/UTF-8 mapping tables
   ```

4. **External / API / Hardware Quirks & Explicit Debt**
   Document unintuitive contracts of ICU, HarfBuzz, CanvasKit, Skia, DOM, or `// KEEPER-DEBT: <reason> — remove when <condition>`.

### B. What to REWRITE (AI-Authored Comments Only; Ask for All Others)

| Bad (Verbose / Diff-bound / What-focused) | Good (Timeless "Why", no final period) |
| :--- | :--- |
| `// We need to check if the run is RTL because if it is RTL we reverse the glyph order.` | `// RTL runs arrive in visual order and must be reversed back to logical` |
| `// Fixed bug where trailing spaces caused wrong width calculation.` | `// Trailing whitespace hangs outside visual bounds but still advances layout width` |
| `// Removed cache invalidation here because it was slow and unnecessary.` | `// Layout cache stays valid across paint-only passes; only font changes invalidate it` |

### C. What to DELETE (AI-Authored Comments Only; Ask for All Others)

- **Syntax restatements**: `// Check for null`, `// Loop over lines`, `// Return result`, `// Default constructor`.
- **Stale diff artifacts**: `// Added by PR`, `// New implementation`, commented-out dead code blocks.
- **Test narration**: `// Test that width is 10`, `// Verify empty string`.

---

## 4. Workflow

### Step 1: Determine Scope & Run the Audit Script
- **Diff mode** (default when working on uncommitted changes, a commit, or a branch):
  Inspect `git diff` (or `git show <ref>`) so you see **every added, modified, and deleted line**.
- **File mode** (when the user specifies files/directories):
  Inspect the target files directly.

Run [`audit_comments.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/code-comments/scripts/audit_comments.py) to list mechanical violations and all uncommented diff hunks (including 1-line additions, modifications, and deletions):
```bash
# Audit uncommitted diff against HEAD (or pass --diff origin/master)
python3 ~/.gemini/config/skills/code-comments/scripts/audit_comments.py --diff HEAD --repo <repo_path>

# Audit specific files
python3 ~/.gemini/config/skills/code-comments/scripts/audit_comments.py <file1> <file2>
```

### Step 2: Review Every Hunk & Existing Comment
1. **Every diff hunk (even 1-line additions, modifications, or deletions)**:
   - Is it trivial (typo, formatting, unused import, obvious rename)? If so, skip.
   - Is it **non-trivial** (logic tweak, boundary condition `<` vs `<=`, removed call/reset, added guard, changed parameter)? **Add a 1–4 line comment** explaining the rationale or invariant. If the "why" is not clear from context, ask the user.
2. **Existing comments in scope**:
   - **You remember adding it yourself in this session**: rewrite or delete it directly in the file to match the style contract.
   - **You did NOT add it (or do not remember adding it)**: **DO NOT touch it silently**. List it for the user and ask whether to rewrite/remove it.

### Step 3: Apply Allowed Edits & Verify
1. Apply new comments (and edits to your own AI-added comments) directly in the working tree.
2. Re-run [`audit_comments.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/code-comments/scripts/audit_comments.py) on the diff to confirm your newly added comments have **zero** mechanical violations.
3. Present any questions about pre-existing comments or ambiguous "why"s to the user.
