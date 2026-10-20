---
trigger: always_on
description: Mandatory fact-checking and referential grounding for all outgoing documents and technical texts.
---

# Outgoing Document & Technical Text Verification

Code has a compiler; prose documents do not. Never publish, hand off, or present as final any external document or substantive technical text without verifying its factual grounding against actual source files and repository state.

## 1. Two-Tier Verification by Scope

1. **Substantive Documents & Technical Writeups -> Run `amnesia-doc-check` (Script + Clean-Context Subagent)**:
   - **Applies to**:
     - Any standalone document or presentation: Google Docs, design docs, architecture proposals, postmortems, audit/benchmark reports, slide decks, `README.md`, `SKILL.md`, or agent rule files.
     - Any multi-paragraph outgoing technical text (detailed CL/PR descriptions, Buganizer / GitHub issue writeups, design review comments, or team chat posts) that presents multi-step arguments/tables or cites file paths, skill/target names, code symbols, CLI flags, commit SHAs, or subsystem mechanics.
   - **Required Action**: Before updating a Google Doc, creating a shareable deliverable, or presenting the final draft to the user to send externally, execute the `amnesia-doc-check` skill:
     1. Run `python3 ~/.gemini/config/skills/amnesia-doc-check/scripts/check_doc_grounding.py --repo <repo> <draft>` to catch leftover placeholders (`[find link]`, `TBD`), intra-document path drift, missing paths, and paraphrased skill/directory names.
     2. Spawn a single clean-context subagent (with zero chat history bias) to verify both **external grounding** (every path, symbol, CLI flag, and `file:line` mechanism claim) and **internal content self-consistency** (cross-section contradictions between proposals and caveats, terminology/naming drift across sections, summary-vs-table arithmetic, and problem-to-solution alignment).

2. **Short Replies & Trivial Commit Messages -> Inline Grounding (0 Subagents)**:
   - **Applies to**: Short 1–3 sentence replies, brief inline review comments, or small single-hunk commit messages where all referenced lines are already open in the current turn's context.
   - **Required Action**: Do not spawn a subagent for trivial replies, but **never** type a file path, directory name, skill/target name, class/function identifier, CLI flag, or commit SHA from memory or by paraphrasing section headings. Every identifier in outgoing text must match the repository character-for-character.

## 2. Hard Prohibitions in Outgoing Text

- **No Paraphrased Identifiers**: Never reconstruct file paths or skill/target names from prose headings (e.g., turning `skia-style-check` into `skia-code-style-audit` or `agents/core_skills.json` into `_agents/core_skills.json`).
- **No Unbacked Mechanism Claims**: Never state that a parser, build system, or runtime behaves a certain way without first reading the implementation (`file:line`).
- **Zero Leftover Scaffolding**: Never leave generation placeholders (`[find link]`, `[insert ...]`, `TBD`, `TODO`, `(AI evaluation...)`, or empty markdown links) in any text delivered to the user.
