---
name: amnesia-doc-check
description: >-
  Clean-context fact-checking, internal self-consistency, and referential grounding pipeline
  for outgoing documents, design docs, slide decks, and substantive technical texts (CL/PR
  descriptions, bug reports, review summaries). Combines (1) a zero-token deterministic pass
  (`check_doc_grounding.py`) for leftover LLM placeholders, intra-document path drift,
  nonexistent paths, and wrong skill/symbol names, with (2) a single amnesiac subagent pass
  (zero chat history bias) that checks internal logical/numeric/terminological consistency
  across sections AND verifies every path, symbol, CLI flag, and `file:line` mechanism claim
  against actual source code. Trigger before sharing any document or substantive technical
  writeup externally, or when the user asks to "проверь документ", "проверь док на лажу",
  "проверь на непротиворечивость", "проверь текст перед отправкой", "сделай амнезия-проверку
  дока", or "run amnesia doc check".
---

# Amnesia Document & Outgoing Text Fact-Checker (`amnesia-doc-check`)

Code has a compiler and test suite; prose documents do not. When an agent drafts or edits a document at the end of a long conversation, it routinely carries over paraphrased file/skill names, unverified assumptions about how an external subsystem works, internal contradictions between early proposals and late caveats, or leftover generation scaffolding (`[find link]`, `TBD`).

This skill acts as a **compiler + clean-room auditor for technical prose** before any document or substantive technical text leaves the session.

---

## Scope Calibration (When to Run Full vs. Inline)

1. **Full Pipeline (Phase 1 Script + Phase 2 Amnesiac Subagent)**:
   - Any standalone document or presentation: Google Docs, design docs, architecture proposals, postmortems, audit/benchmark reports, slide decks, `README.md`, `SKILL.md`, or rules files (even if the document is pure prose/strategy with no code links — internal self-consistency still applies).
   - Any multi-paragraph external technical message (detailed CL/PR description, Buganizer / GitHub issue analysis, design review comment, or team chat post) that makes multi-step arguments, presents numbers/tables, or cites file paths, code symbols, CLI flags, commit SHAs, or subsystem mechanics.
2. **Lightweight Inline Check (Phase 1 + Direct Tool Check, 0 Subagents)**:
   - Short chat or review replies (1–3 sentences) and trivial 1-hunk commit messages where every referenced file and line is already open in the current turn's context.

---

## Core Invariants

1. **Zero-Token First Pass**: Always run [`check_doc_grounding.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/amnesia-doc-check/scripts/check_doc_grounding.py) first to catch leftover placeholders, intra-document path conflicts, nonexistent repo paths, and wrong directory prefixes before spawning a subagent.
2. **Single Amnesiac Subagent**: Spawn **at most one** clean-context subagent (`research-google` or `self`). Pass **only** the raw text of the document, the target repository path(s) (if any), and the Phase 1 script output—never pass the main conversation's reasoning or compromises.
3. **Internal Self-Consistency First**: A document must not contradict itself across sections, tables, or bullet lists—whether in logic (recommending X in Section 2 while admitting X is broken in Section 5), terminology (calling the same entity by different names in different sections), or arithmetic (summary numbers disagreeing with tables).
4. **`file:line` Proof Over Plausibility**: Any claim that *"System X works by doing Y"* or *"Config A requires format B"* is treated as false unless backed by an exact `file:line` in actual source code.
5. **No Silent Approximations**: Paraphrased identifiers (e.g., writing `skia-code-style-audit` when the directory is `skia-style-check`, or `_agents/core_skills.json` when the file is `agents/core_skills.json`) are treated as hard compilation errors.

---

## Workflow

### Phase 1: Deterministic Grounding Razor (0 Subagents)

Run the deterministic grounding script against the draft file (markdown, plain text, or a saved Google Drive `read_file_content` JSON file) and all relevant local repositories:

```bash
python3 ~/.gemini/config/skills/amnesia-doc-check/scripts/check_doc_grounding.py \
  --repo <path/to/repo> \
  <path/to/draft_or_drive_output>
```

If the text only exists in memory (e.g., a drafted CL description or message), pipe it via stdin or write it to `<artifact_dir>/scratch/draft_to_check.md` (never write scratch files into CitC or a repo tree).

Immediately fix any deterministic failures that are obvious typos before or alongside Phase 2:
- Leftover LLM markers (`[find link]`, `[insert ...]`, `(AI evaluation...)`, `TBD`, `TODO`, empty markdown links).
- Wrong directory prefixes where `check_doc_grounding.py` identifies the existing same-basename path in the repo.

---

### Phase 2: The Amnesiac Fact-Check Inquisitor (1 Subagent)

Invoke a single clean-context subagent with zero prior conversation history:

```python
invoke_subagent(
    TypeName="research-google",  # Use "self" if local shell commands in non-g3 git repos are needed
    Role="Amnesiac Document Fact-Checker",
    Workspace="inherit",
    Prompt="""
You are a hostile, clean-context technical fact-checker evaluating a document/text with zero prior chat history.
Assume the author may have hallucinated file paths, paraphrased symbol/skill names from memory, or invented plausible-sounding architectural rules without reading the source code.

Target Document / Text:
<path to draft file or inline text>

Target Repositories / Codebases:
<explicit paths to local git repos and/or google3 depot paths>

Deterministic Pre-Check Output:
<paste output of check_doc_grounding.py>

Instructions:
Verify the document across four strict axes using actual tool calls (`read_file`, `code_search`, or read-only `git`/`ls` commands):

1. **Referential Grounding (Paths, Names, Flags, SHAs, Bugs/CLs)**:
   - Verify every file path, directory, skill name, class/function/method name, CLI flag, build target, and commit/CL/bug ID mentioned in the text.
   - Distinguish between paths explicitly proposed as *new* vs. paths claimed to exist *today*. Every path or identifier claimed to exist today must match the repository character-for-character.

2. **Mechanism & Contract Claims (`file:line` Verification)**:
   - Extract every technical claim about how a tool, parser, build system, or runtime behaves (e.g., "X only discovers Y", "entries[].path must point to a parent directory", "all tests live in tests/").
   - Locate the actual implementation (`file:line`) in the codebase. Flag any claim that contradicts the implementation or omits a critical branch/exception.

3. **Internal Content Self-Consistency (Logic, Terminology, & Numbers)**:
   - **Proposals vs. Caveats**: Check whether the architecture, defaults, or action items proposed in one section rely on components, paths, or assumptions that another section of the same document admits are broken, missing, external, or unsupported.
   - **Mutually Contradictory Rules/Requirements**: Check whether two principles or requirements in the document conflict with each other (e.g., demanding concise DRY files in one bullet while mandating copy-pasted environment boilerplate in another).
   - **Terminology & Taxonomy Drift**: Check whether the document invents one set of names, paths, or categories in early sections/tables and silently shifts to different names or omits them in later sections.
   - **Quantitative & Tabular Arithmetic**: Verify that counts, totals, percentages, and deltas stated in executive summaries or prose match the detailed tables, lists, and data rows (e.g., "3 reasons" followed by 4 bullets, or summary percentages not matching raw numbers).
   - **Problem ↔ Solution & Lifecycle Alignment**: Verify that every stated problem in the motivation is actually addressed by the proposal, and that invariants are placed in a layer whose lifecycle can enforce them (e.g., never placing `always_on` guardrails inside an on-demand component).

4. **Engineering Common Sense & Pragmatism (The "Senior TL Reality Check")**:
   - **Proportionality & Occam's Razor (Over-Engineering Check)**: Does the document propose a multi-tier architecture, new config schemas, or heavy process for a problem that can be solved in 5 lines using an existing mechanism (e.g., `inherits` instead of a parallel registry)? Always check if a 10x simpler solution exists.
   - **Day-2 Blast Radius & Single Source of Truth**: What happens the day after this lands to an ordinary developer who runs `git pull` or to CI? Does the proposal duplicate an existing list/config/source of truth, guaranteeing silent drift within weeks?
   - **Naked Numbers & Hypothesis-as-Fact**: Flag any performance/memory/quality numbers cited without baseline, denominator, input corpus, and platform/environment. Flag hypotheses or unmeasured guesses phrased as proven causal facts.
   - **"Exists Today" vs. "Proposed" Boundary**: Can an outside reader immediately tell which files, behaviors, or APIs already exist in the codebase today vs. which ones are being proposed?

5. **Operational Hygiene, Tone, & Residual Slop**:
   - **No Ephemeral / Local Path Leaks**: Ensure no personal home paths (`/usr/local/google/home/...`), `/tmp/...`, personal CitC paths (`/google/src/cloud/...`), `localhost` URLs, or internal `conversation://` links leak into external documents.
   - **Active LDAPs & Ownership**: Verify that any `@google.com` LDAPs cited as owners, reviewers, or contacts are active employees (`id -u <ldap>`) and actually own the relevant area.
   - **Zero AI-Slop & Prompt Leakage**: Strip leftover generation placeholders (`[find link]`, `TBD`), meta-commentary from the prompt, and pompous LLM corporate filler (`holistic`, `synergy`, `paradigm shift`, `delve`, `testament to`).

6. **Pre-Review Grill (Skeptical Reviewer's First Question)**:
   - Identify the single hardest or most awkward question a skeptical domain owner/TL will ask in their first review comment on this document (*"Why didn't you just do X?"*, *"What happens when Y occurs?"*) that is not yet answered in the text.

Output Format (Strict):
### 1. Factual & Referential Errors (Must Fix)
For each error:
- `Location`: <Section / Line>
- `Claimed in Text`: <exact quote>
- `Actual Reality (`file:line` or repo state)`: <exact path, symbol, or code behavior>
- `Fix`: <exact replacement text>

### 2. Unverified / Overstated Mechanism Claims
Claims that lack source-code evidence or overgeneralize (with the exact `file:line` showing the real behavior).

### 3. Internal Contradictions, Common-Sense & Architectural Flaws
Conflicts between sections, over-engineered proposals where a trivial fix exists, naked numbers without baselines, or proposals that will break on Day 2.

### 4. Pre-Review Grill (Unanswered Reviewer Questions)
At most 1–2 tough questions a skeptical reviewer will immediately ask that the document fails to address. Write `None` if the document already anticipates and answers them.

### 5. Verified Grounding Summary
Brief count of verified paths, symbols, and mechanism claims.
"""
)
```

---

### Phase 3: Remediation & Verification Summary

1. **Apply All Fact-Check Fixes & Resolve Pre-Review Grill**:
   - If the user owns/authors the draft:
     - Apply every `[Must Fix]` correction and resolve internal contradictions before presenting or publishing the text.
     - For any **Pre-Review Grill** question: if the answer is already determinable from the codebase or conversation context, **silently add a concise 1-sentence explanation** to the document to preempt the reviewer's question. Ask the user **only** if it exposes a genuine unresolved product/design decision that cannot be answered from code.
     - Re-run `check_doc_grounding.py` to confirm `0` unresolved placeholders or accidental broken paths.
   - If the user asked to review someone else's document: present the categorized findings directly in chat with clickable `[filename](file:///path#L123)` links to the proving source lines.

2. **Report Concise Audit Telemetry in Chat**:
   - **Grounding Status**: `<N> paths/names verified, <M> factual/referential errors caught`
   - **Mechanism Claims**: `<K> verified against file:line, <J> refuted/corrected`
   - **Contradictions / Placeholders**: Summary of any cross-section conflicts or stripped LLM scaffolding.
