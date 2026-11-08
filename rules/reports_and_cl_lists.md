---
trigger: model_decision
description: "When writing reports, summaries, design notes or perf writeups for people, or listing/summarizing CLs and PRs (chat tables, Flight Deck)."
---

# Reports and CL/PR Lists

## Reports for people
- Never use commit hashes, roadmap IDs (`R12`, `R47.1`) or internal variant names (`c0`, `g1`,
  `wpnew`); name the change in words ("WebParagraph with FillTextRuns")
- Keep an identifier only where reproducibility needs it (methodology, attestation) and explain it

## Listing CLs and PRs
For every CL or PR row include:
1. Last status change: date, ET time and age (`Oct 26 at 14:15 ET (2h ago)`)
2. `WIP` / `Draft` badge for Gerrit `work_in_progress`, `WIP` / `Draft` / `DO NOT SUBMIT` subject
   prefix, or GitHub `isDraft`
3. Companion review CLs (`companion-cl-review`) only as an attribute of the parent row
   (`Has companion [Skia CL <M> PS<K>](<url>) (WIP)`), never as their own rows. Give a reverse link
   only when she names the companion CL directly
4. Missing reviewers or approvals: `No reviewers assigned`, `Needs 2 reviewers`,
   `CR +1 (1/2) · Needs +1 CR`
