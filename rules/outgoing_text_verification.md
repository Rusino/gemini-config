---
trigger: always_on
description: Mandatory zero-trust verification for all incoming and outgoing documents, technical texts, and code changes/snippets.
---

# Incoming & Outgoing Document and Code Verification

Code has a compiler; prose documents do not, and incoming specs or code snippets often contain stale paths, broken contracts, or unverified assumptions. Apply zero-trust verification across all four boundaries (Incoming/Outgoing × Documents/Code), while keeping normal conversational chat lightweight.

## 1. Documents & Technical Prose (`amnesia-doc-check`)

1. **Outgoing Documents & Substantive Technical Writeups -> Full `amnesia-doc-check`**:
   - **Applies to**: Standalone docs/presentations (`update_doc`, `create_file`, design docs, postmortems, audit/benchmark reports, slide decks, `README.md`, `SKILL.md`, rule files) and multi-paragraph outgoing technical texts (CL/PR descriptions, Buganizer/GitHub writeups, design review comments, team chat posts) with tables or code/path/symbol/CLI citations.
   - **Required Action**: Before sharing externally or updating a Google Doc, read and execute `~/.gemini/config/skills/amnesia-doc-check/SKILL.md` (`check_doc_grounding.py` + clean-context Hunter + batched `keeper_skeptic` refutation + `finding_ledger.py`).

2. **Incoming Documents & Specs -> Verify Before Trusting (`amnesia-doc-check` on Read)**:
   - **Applies to**: Any Google Doc (`read_file_content`, `read_doc`), local `.md` design doc/spec, bug report, or multi-paragraph technical text the user asks you to read, review, analyze, or implement from.
   - **Required Action**: Run `check_doc_grounding.py` and verify cited paths, symbols, flags, `file:line` claims, and self-consistency against actual source files (using Hunter + `keeper_skeptic` for substantive specs). Surface a concise grounding verdict (`Grounding OK` or exact discrepancies) at the top of your response before implementing.

3. **Short Replies & Trivial Commit Messages -> Inline Grounding (0 Subagents)**:
   - **Applies to**: Short 1–3 sentence chat replies, brief inline review comments, or small single-hunk commit messages where all referenced lines are already open in the current turn's context.
   - **Required Action**: Never type a file path, directory name, skill/target name, class/function identifier, CLI flag, or commit SHA from memory or by paraphrasing headings; match repository identifiers character-for-character.

4. **Automatic Redline on Document Updates (`redline`)**:
   - **Applies to**: Any edit, shortening, translation, or restructuring (>1 line or >15 words) of an existing document/artifact/spec that the user has already seen in earlier turns.
   - **Required Action**: Automatically run `python3 ~/.gemini/config/skills/redline/scripts/redline.py` against the last version the user saw, verify `<stem>_diff.html` via headless browser screenshot (`~/.gemini/config/skills/redline/SKILL.md`), and link `<stem>_diff.html` alongside the updated document without waiting to be asked.

## 2. Incoming & Outgoing Code (`amnesia-precommit`, Corpus Gate & KEEPER Invariants)

1. **Outgoing Repository Code Changes -> Run `amnesia-precommit` & Pre-Upload Flutter Corpus**:
   - **Applies to**: Any source code modifications in a repository before committing (`git commit`), uploading/pushing (`git cl upload`, `gh pr create`, `git push`), or reporting implementation complete.
   - **Required Action**: Read and execute `~/.gemini/config/skills/amnesia-precommit/SKILL.md`:
     1. Run `audit_comments.py --diff HEAD --repo <repo>` and `ruthless_audit.py <repo> --repo <repo> --diff HEAD` (plus `SKIP_GCE_AUTH_FOR_GIT=1 git cl presubmit --force` if `PRESUBMIT.py` exists).
     2. For non-trivial diffs, run the clean-context Hunter (`keeper_inquisitor`) + batched `keeper_skeptic` refutation pass (`plugins/keeper/agents/keeper_skeptic.md`) + `finding_ledger.py`.
     3. **Pre-Upload / Pre-Push Flutter Corpus Gate (`CORPUS-CHECK GUARD` in `pre_tool_guard.py`)**: Before `git cl upload`, `gh pr create`, or `git push` with executable production code changes:
        - Touching `modules/{skparagraph,skunicode,skshaper}/{src,include}/` -> run `flutter-test-paragraph-engine` (`run_flutter_paragraph_tests.py`).
        - Touching `lib/web_ui/lib/` -> run `webparagraph-corpus` (`run_webparagraph_corpus.py`, Part 1 `felt` + Part 2 `packages/flutter`).

2. **Bug Triage & Fixes -> Failing-Then-Passing Contract Test (and On-Demand `dual-version-bug-slide`)**:
   - **Applies to**: Any bug report or regression in `SkParagraph`, `SkUnicode`, `SkShaper`, `WebParagraph`, or `Skia`.
   - **Required Action**: Author a failing-then-passing contract unit test before fixing production code; when the user asks for a visual before/after comparison or slide, run `~/.gemini/config/skills/dual-version-bug-slide/SKILL.md`.

3. **Outgoing Code Snippets in Chat (>5 Lines) -> Inline Invariant Check (0 Subagents)**:
   - **Applies to**: Any non-trivial code block or patch (>5 lines) proposed in chat.
   - **Required Action**: Read the actual header/definition before calling any API, and verify against `cpp_systems_invariants.md`, `text_domain_invariants.md`, and `code_style.md` (no trailing periods in non-Dart comments, `<= 100` cols, zero-allocation hot paths, loop guards, `SkASSERT` over defensive `if (!ptr) return;`).

4. **Incoming Code (CLs, PRs, Commits, Patches, or User-Pasted Snippets >5 Lines) -> Zero-Trust Audit on Read**:
   - **Applies to**: Any external CL/PR, commit, branch, diff, or code snippet (>5 lines) the user asks you to read, explain, review, or integrate.
   - **Required Action**: Run `audit_comments.py` and `ruthless_audit.py --diff` (and use `review-triage` + `companion-cl-review` by default for full CL/PR reviews unless the user opts out with "без компаньона" / "только текстом"), and audit against `cpp_systems_invariants.md`, `text_domain_invariants.md`, and `code_style.md`.

## 3. Hard Prohibitions

- **No Paraphrased Identifiers**: Never reconstruct file paths or skill/target names from prose headings.
- **No Unbacked Mechanism Claims**: Never state how a parser, build system, or runtime works without reading `file:line`.
- **Zero Leftover Scaffolding**: Never leave generation placeholders (`[find link]`, `[insert ...]`, `TBD`, `TODO`, `(AI evaluation...)`, or empty markdown links) in any delivered text.
