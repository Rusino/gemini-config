---
name: ruthless-code-audit
description: >-
  End-to-end code review and feature utility audit across repositories, packages, branches, or
  diffs (`ruthless_audit.py`, semantic review, `keeper_skeptic` refutation, and `DELETE / SIMPLIFY
  / KEEP` pruning plan). Trigger when asked to "проведи аудит кода", "беспощадный аудит",
  "проверь на дублирование и ужасы", "оцени полезность функционала", "найди мёртвый код", or
  "ruthless code review / audit".
---

# Ruthless Code & Feature Auditor (`ruthless-code-audit`)

Execute a zero-sycophancy audit (`Stage 1 + Stage 2 + Stage 2.5 Skeptic Refutation + Stage 3`) that exposes code duplication, architectural brain-split, dead/placebo features, invariant breaches, and real bugs—filtering out false positives via a clean-context Verification Skeptic before producing a concrete pruning plan with exact line-count savings.

---

## Stage 1: Deterministic Multi-Pass Scan (`ruthless_audit.py`)

Always start by running [`ruthless_audit.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/ruthless-code-audit/scripts/ruthless_audit.py) on the target directory, file list, or git diff. The script works on both **Linux Jetski Cloud** and **macOS Antigravity**:
- On Linux Jetski Cloud (when `/google` is mounted), it automatically runs `/google/bin/releases/gemini-agents-copy-paste/copy_paste_detector` across **three passes**: `winnowing` (exact/parameterized clones), `sourcerer` (`--similarity=0.75` gapped/reordered clones), and test-only clones (`--exclude-default=false`), plus `--against` when reference directories are supplied.
- On macOS Antigravity (or when `/google` is unavailable), it automatically falls back to its built-in **Normalized Token Winnowing & Sliding-Block Detector** (stripping comments/headers and normalizing literals and local identifiers while preserving called APIs).

### Commands

```bash
# Full repository or package audit
python3 ~/.gemini/config/skills/ruthless-code-audit/scripts/ruthless_audit.py \
  <target_dir_or_files...> --repo <repo_root>

# Diff / PR / CL audit (checks changed files, uncommented hunks, and clones against shared dirs)
python3 ~/.gemini/config/skills/ruthless-code-audit/scripts/ruthless_audit.py \
  <target_dir_or_files...> --repo <repo_root> --diff origin/main --against common,util

# Allow Cyrillic when auditing intentionally localized tools/docs
python3 ~/.gemini/config/skills/ruthless-code-audit/scripts/ruthless_audit.py \
  <target_dir_or_files...> --repo <repo_root> --allow-cyrillic
```

### What Stage 1 Detects Mechanically
1. **Duplication**:
   - 100% byte-identical files (e.g., forgotten copies of scripts/configs across directories).
   - Parameterized and near-duplicate code blocks in production files and test suites.
2. **Structural Bloat & Dead Code**:
   - Bloated files (`> 800` lines) and monolithic functions (`> 120` lines).
   - Uncalled internal functions/methods (`0` external references) and production functions called **only from tests** (dead API surface kept alive by its unit test).
   - Duplicate function names and duplicate `/api/...` endpoints across files (brain-split indicator).
3. **KEEPER Invariants, Anti-Slop & Comment Rules**:
   - Compiler/linter warning suppressions (`#pragma GCC/clang diagnostic ignored`, `NOLINT`, `ignore:`, `noqa`, `eslint-disable`).
   - Unbounded loops (`while (true)`, `for (;;)`) without a visible iteration/timeout guard.
   - Hot-path heap allocations (`new`, `malloc`, `std::make_shared`, `CFDataCreate`, `SkStringPrintf`) inside layout/paint/shape/render functions.
   - Defensive `if (!ptr) { return; }` masking in place of `SkASSERT` / contract assertions.
   - Anonymous `TODO`/`FIXME`/`HACK` without `KEEPER-DEBT` or bug IDs.
   - Hardcoded home paths (`/usr/local/google/home/`, `/Users/`), debug print leftovers, unexpected Cyrillic `[А-Яа-яЁё]`.
   - Test horrors: sleep-based synchronization (`time.Sleep`, `Future.delayed`, `sleep_for`), magic score/probability/sizeof pinning (`Probability != 89`, `sizeof(...) == 544`), and narrative headers above assertions.
   - Mechanical comment violations and uncommented diff hunks via [`audit_comments.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/code-comments/scripts/audit_comments.py).

---

## Stage 2: Deep Semantic, Architectural & Code-Taste Review

Do not stop at the script output. Inspect the flagged hotspots and read the core domain files to evaluate architecture, correctness, and code taste.

### Orchestration in Jetski / Antigravity
- **Persistent Finding Ledger (`finding_ledger.py`)**: Stage 1 (`ruthless_audit.py`) automatically queries `~/.gemini/config/scripts/finding_ledger.py` and outputs any prior refuted findings for the target repo/diff/files. Always include those entries in the prompt of every Stage 2 Hunter subagent so they do not waste tokens re-raising already-refuted issues.
- **For large repositories (> 3,000 lines or > 5 files)**: spawn parallel read-only subagents (`invoke_subagent` with `TypeName="self"` or `TypeName="keeper_inquisitor"`) partitioned by subsystem (e.g., Subagent 1: Backend & Concurrency; Subagent 2: Frontend/UI & Duplication; Subagent 3: Tests & Mutation Harness) so the main context stays clean.
- **Veteran lens (Skia C++ / text / font code)**: run `veteran-review` in lens mode (`~/.gemini/config/skills/veteran-review/SKILL.md`, Steps 1–3) and feed its candidates into the Stage 2.5 packet instead of running its own skeptic pass.
- **Built-in Google3 reference skills** (when `/google/src/files/head/...` is mounted):
  - Consult [`copy-paste-detection`](file:///google/src/files/head/depot/google3/learning/gemini/agents/skills/copy_paste_detection/SKILL.md) for DRY vs. DAMP judgment and `--query-snippet` bug propagation.
  - Consult [`code-taste`](file:///google/src/files/head/depot/google3/learning/gemini/agents/skills/code_taste/SKILL.md) for language-specific structural transformations.
- **Slash command**: remind the user they can also trigger `/review-agent` for an interactive experimental review pass on a CL or branch.

### Semantic Review Checklist
1. **Multi-Generation & Cross-Layer Brain-Split**:
   - Are multiple generations of UI/backends coexisting (e.g., standalone web dashboard + reverse proxy + extension)?
   - Is the same domain rule (e.g., flake classification, team lookup, BiDi level resolution, font fallback) implemented in 2–4 places with diverging formulas?
2. **Code Taste & Abstraction Depth**:
   - **Ousterhout's Deep Modules**: Flag shallow pass-through wrappers, speculative interfaces with a single implementation, and uncalled getters/setters.
   - **Linus's Principle**: Identify `if/else` special-case proliferation or symptomatic test-fitting branches that disappear when the underlying data representation is fixed.
   - **Replace Before Extract**: Before proposing a new helper for duplicated code, check whether a standard library function or an existing project helper already solves it, or whether the repetition is intentional DAMP (e.g., table-driven test inputs).
3. **Production Bug Hunting**:
   - **Concurrency & Data Races**: Slice/map/pointer mutations under `RLock` or after releasing a lock while JSON serialization or background goroutines/threads run.
   - **Cache Poisoning & Memory Leaks**: Caching empty/error results when an RPC/subprocess times out; unbounded global caches (`diffCache`, font registries) holding megabytes of dead blobs forever.
   - **Control-Flow Shadowing**: Missing early `return`/`continue` that causes a specific triage branch to fall through and get overwritten by generic scoring at the bottom of a monolithic function; parameters that are always `false` in production because an upstream filter already excluded those items.
   - **Naive Substring / Regex Traps**: Short tags like `"gl"` matching `"glyph"`/`"google"`, or `"gn"` matching `"align"`/`"diagnostic"`.

---

## Stage 2.5: Skeptic Refutation Pass (`keeper_skeptic`, `Hunt then Refute` + `XFAMILY_REFUTE`)

Before compiling the final report in Stage 3, pass all candidate **`HIGH` / `CRITICAL` production bugs** AND all candidate **`DELETE` / non-trivial `SIMPLIFY` removals** through a single batched clean-context Verification Skeptic (`keeper_skeptic`) following the `XFAMILY_REFUTE` routing, Quota Fallback Ladder, and Three Iron Verification Principles in [`plugins/keeper/agents/keeper_skeptic.md`](file:///usr/local/google/home/jlavrova/.gemini/config/plugins/keeper/agents/keeper_skeptic.md):

```python
invoke_subagent(
    TypeName="keeper_skeptic",
    Role="Batched Audit Verification Skeptic",
    Model="<flash or pro per keeper_skeptic.md XFAMILY_REFUTE rules>",
    Workspace="inherit",
    Prompt="""
You are the Verification Skeptic (`keeper_skeptic`) auditing candidate findings in `<repo_root>`.
Refute each candidate below using real code, callers (`git grep`, `grep -rn`, `code_search`), templates, build files, comments, and unit tests per your Three Iron Verification Principles.

Candidates Under Test (Batch V001):
<paste all candidate HIGH/CRITICAL bugs (B1, B2...) and all candidate DELETE / non-trivial SIMPLIFY items (D1, D2...)>
"""
)
```

Drop all `REFUTED` items before writing Stage 3, and immediately persist every `REFUTED` item (or user design override) via `python3 ~/.gemini/config/scripts/finding_ledger.py add --repo <repo_root> --file <path> --symbol "<func_or_class>" --claim "<claim>" --reason "<evidence>" --scope repo`. Convert any `UNPROVEN` item into an explicit verification question.

---

## Stage 3: Feature & Test Utility Matrix (`DELETE` / `SIMPLIFY` / `KEEP`)

Evaluate every feature, endpoint, heuristic, and test file against a single criterion: **Does it deliver high signal-to-noise value for the core mission, or is it bloat, guesswork, or placebo?**

For full-package or repository audits, write a Markdown artifact in `<appDataDir>/brain/<conversation-id>/code_and_feature_audit.md` (for small diffs, output directly in chat) structured as follows:

1. **Executive Summary & Bloat Score**:
   - Total lines scanned vs. achievable target lines (`−X%` reduction) while *improving* correctness and speed.
   - Mermaid `flowchart TD` diagram illustrating architectural duplication / brain-split across layers.
2. **Matrix 2.1 — Candidates for Complete Removal (`DELETE` / `УДАЛИТЬ`)**:
   - Table with columns: `Feature / Subsystem | Location & Size | Why It Must Go | Line Savings`.
   - Include byte-identical file copies, obsolete UIs, placebo buttons/endpoints (e.g., dry-run endpoints that only format strings or log to stdout), slow external CLI subprocesses that do not affect output, dead branches, and unread struct fields.
3. **Matrix 2.2 — Candidates for Radical Simplification (`SIMPLIFY` / `УПРОСТИТЬ`)**:
   - Table with columns: `Feature / Subsystem | Location | Core Value to Keep | What to Drop / How to Collapse | Line Savings`.
   - Include merging duplicated renderers/switches into table-driven helpers, splitting 1,000-line monoliths, and stripping arbitrary guesswork bonuses.
4. **Section 2.3 — High-Value Core (`KEEP` / `ОСТАВИТЬ`)**:
   - Numbered list of the essential domain algorithms and UI workflows that must be preserved and protected by contract tests.
5. **Section 3 — Critical Production Bugs & Invariant Violations**:
   - Each bug with exact clickable `[file:Lstart-Lend](file:///...)` links, root cause, and concrete fix.
6. **Section 4 — Test Suite & Mutation Harness Audit**:
   - Identify coverage skew (e.g., 90% of mutations targeting one file while core modules have 0), character-pinning of magic numbers (`Probability != 89`, `sizeof == 544`), and copy-pasted test functions that should be collapsed into table-driven tests.
7. **Section 5 — Phased Atomic Cleanup Plan**:
   - 3–4 self-contained stages (`Stage 1: Zero-Risk Pruning & Deduplication`, `Stage 2: Backend Bug Fixes & Dead Code Removal`, `Stage 3: Frontend/UI Compression`, `Stage 4: Test & Mutation Rebalancing`) with expected line delta per stage and verification commands.

> [!IMPORTANT]
> **Stay Read-Only Until Approved**: When asked to perform an audit or review, do **not** modify production files during the audit itself. Present the audit report and phased cleanup plan first, and wait for the user's go-ahead before executing the cleanup stages.
