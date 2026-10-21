---
trigger: always_on
description: Mandatory zero-trust verification for all incoming and outgoing documents, technical texts, and code changes/snippets.
---

# Incoming & Outgoing Document and Code Verification

Code has a compiler; prose documents do not, and incoming specs or code snippets often contain stale paths, broken contracts, or unverified assumptions. Apply zero-trust verification across all four boundaries (Incoming/Outgoing × Documents/Code), while keeping normal conversational chat lightweight.

## 1. Documents & Technical Prose (`amnesia-doc-check`)

1. **Outgoing Documents & Substantive Technical Writeups -> Full `amnesia-doc-check` (Script + Two-Stage Clean-Context Pipeline)**:
   - **Applies to**:
     - Any standalone document or presentation: Google Docs (`update_doc`, `create_file`), design docs, architecture proposals, postmortems, audit/benchmark reports, slide decks, `README.md`, `SKILL.md`, or agent rule files.
     - Any multi-paragraph outgoing technical text (detailed CL/PR descriptions, Buganizer / GitHub issue writeups, design review comments, or team chat posts) that presents multi-step arguments/tables or cites file paths, skill/target names, code symbols, CLI flags, commit SHAs, or subsystem mechanics.
   - **Required Action**: Before updating a Google Doc, creating a shareable deliverable, or presenting the final draft to the user to send externally, execute the `amnesia-doc-check` skill (`~/.gemini/config/skills/amnesia-doc-check/SKILL.md`):
     1. Run `python3 ~/.gemini/config/skills/amnesia-doc-check/scripts/check_doc_grounding.py --repo <repo> <draft>` to catch leftover placeholders (`[find link]`, `TBD`), intra-document path drift, missing paths, and paraphrased skill/directory names.
     2. Spawn a clean-context Hunter subagent (with zero chat history bias) to verify both **external grounding** (every path, symbol, CLI flag, and `file:line` mechanism claim) and **internal content self-consistency** (cross-section contradictions between proposals and caveats, terminology/naming drift across sections, summary-vs-table arithmetic, and problem-to-solution alignment), followed by a batched `keeper_skeptic` refutation pass (`Hunt then Refute` with `XFAMILY_REFUTE` auto-routing) whenever non-trivial mechanism or contradiction findings are raised.

2. **Incoming Documents & Specs -> Verify Before Trusting (`amnesia-doc-check` on Read)**:
   - **Applies to**: Any Google Doc (`read_file_content`, `read_doc`), local `.md` design doc/spec, bug report, or multi-paragraph technical text that the user asks you to read, review, analyze, or implement from.
   - **Required Action**: Never trust an incoming document's technical claims or paths at face value:
     1. Run `check_doc_grounding.py` against the document text (when a target repository applies) and verify its cited paths, symbols, CLI flags, `file:line` mechanism claims, and internal self-consistency against actual source files (using the clean-context Hunter + Skeptic pipeline for substantive docs/specs).
     2. Surface a concise grounding verdict at the top of your response (`Grounding OK` or the exact factual/logical discrepancies against the codebase) before answering or implementing from the document.

3. **Short Replies & Trivial Commit Messages -> Inline Grounding (0 Subagents)**:
   - **Applies to**: Short 1–3 sentence chat replies, brief inline review comments, or small single-hunk commit messages where all referenced lines are already open in the current turn's context.
   - **Required Action**: Do not spawn a subagent for trivial replies, but **never** type a file path, directory name, skill/target name, class/function identifier, CLI flag, or commit SHA from memory or by paraphrasing section headings. Every identifier must match the repository character-for-character.

## 2. Incoming & Outgoing Code (`amnesia-precommit` & KEEPER Invariants)

1. **Outgoing Repository Code Changes -> Run `amnesia-precommit`**:
   - **Applies to**: Any source code modifications in a repository before committing (`git commit`), uploading (`git cl upload`, `gh pr create`), or reporting the implementation as complete.
   - **Required Action**: Execute the `amnesia-precommit` workflow (`~/.gemini/config/skills/amnesia-precommit/SKILL.md`):
     1. Run `python3 ~/.gemini/config/skills/code-comments/scripts/audit_comments.py --diff HEAD --repo <repo>` and `python3 ~/.gemini/config/skills/ruthless-code-audit/scripts/ruthless_audit.py <repo> --repo <repo> --diff HEAD` (plus `SKIP_GCE_AUTH_FOR_GIT=1 git cl presubmit --force` if `PRESUBMIT.py` exists).
     2. For non-trivial diffs, run the two-stage **Hunt then Refute** clean-context pipeline: spawn a Hunter subagent (`keeper_inquisitor` or `self`) to shrink drive-by changes, verify dropped guards via `git log -S`, check pattern completeness across all callers/backends, and enforce KEEPER invariants (`cpp_systems_invariants.md`, `text_domain_invariants.md`, `code_style.md`), then challenge any candidate `HIGH`/`CRITICAL` defects or non-trivial code removals with a single batched `keeper_skeptic` refutation pass (`Model="flash"` by default, escalating to `Model="pro"` for deep invariants, with automatic `pro` → `flash` → Trapsmith TDD fallback on quota exhaustion).

2. **Outgoing Code Snippets in Chat (>5 Lines) -> Inline Invariant Check (0 Subagents)**:
   - **Applies to**: Any non-trivial code block or patch (>5 lines) proposed in chat.
   - **Required Action**: Read the actual header/definition before calling any API, and verify the snippet against `cpp_systems_invariants.md`, `text_domain_invariants.md`, and `code_style.md` (no trailing periods in non-Dart comments, `<= 100` cols, zero-allocation hot paths, loop guards, `SkASSERT` over defensive `if (!ptr) return;`).

3. **Incoming Code (CLs, PRs, Commits, Patches, or User-Pasted Snippets >5 Lines) -> Zero-Trust Audit on Read**:
   - **Applies to**: Any external CL/PR, commit, branch, diff, or code snippet (>5 lines) the user asks you to read, explain, review, or integrate.
   - **Required Action**:
     1. For incoming CLs/PRs or local diffs, run `audit_comments.py` and `ruthless_audit.py --diff` (and use `review-triage` (`~/.gemini/config/skills/review-triage/SKILL.md`) for full CL/PR reviews).
     2. Audit the incoming code against `cpp_systems_invariants.md`, `text_domain_invariants.md`, and `code_style.md` (checking for hot-path allocations, missing loop guards, UTF-8/UTF-16/BiDi index mismatches, dropped guards, or missing sibling backend updates), and highlight any defects immediately alongside your explanation or review.

## 3. Hard Prohibitions

- **No Paraphrased Identifiers**: Never reconstruct file paths or skill/target names from prose headings (e.g., turning `skia-style-check` into `skia-code-style-audit` or `agents/core_skills.json` into `_agents/core_skills.json`).
- **No Unbacked Mechanism Claims**: Never state that a parser, build system, or runtime behaves a certain way without first reading the implementation (`file:line`).
- **Zero Leftover Scaffolding**: Never leave generation placeholders (`[find link]`, `[insert ...]`, `TBD`, `TODO`, `(AI evaluation...)`, or empty markdown links) in any text delivered to the user.
