---
trigger: always_on
description: "Zero-trust verification of incoming and outgoing documents and code."
---

# Verify What Comes In and What Goes Out

| Boundary | Action |
|---|---|
| Outgoing doc or substantive writeup (Google Doc, design doc, report, slides, README, SKILL.md, rule file, multi-paragraph CL/PR/bug text with tables or code citations) | `skills/amnesia-doc-check` before sharing (hook-enforced for `update_doc`) |
| Incoming doc, spec, or bug report to read, review, analyze or implement from | `check_doc_grounding.py` (+ Hunter and `keeper_skeptic` for substantive specs); put `Grounding OK` or the exact discrepancies at the top of the reply |
| Outgoing code (commit, upload, push, "done") | `skills/amnesia-precommit`; its Phase 4 lists the corpus gates per path |
| Incoming external CL/PR, branch, diff, commit or snippet over 5 lines she asks to read, explain or integrate | `audit_comments.py` + `ruthless_audit.py --diff`, check against the invariants |
| Full CL/PR review | `review-triage` + `companion-cl-review` by default ("без компаньона" / "только текстом" opts out) |

- Short replies: type every path, symbol, flag, skill name and SHA character-for-character from a
  source open in this turn, never from memory or a heading
- Code snippets over 5 lines in chat: read the real declaration first; check against
  `cpp_systems_invariants.md`, `text_domain_invariants.md`, `code_style.md`
- Edited a document she already saw (more than 1 line or 15 words): run `skills/redline` without
  being asked and link `<stem>_diff.html`
- Google Docs: after `create_file` / `copy_file`, and whenever a Docs/Drive link goes into mail,
  Chat, a Buganizer comment or a Gerrit CL, run `get_file_permissions` and give the Alphabet
  audience commenter access (`share_file`, `permissionId="10969126254235447854"`, or the ID from
  `get_audience_recommendations` if it differs). Named mail/Chat recipients also get writer
  access. The `DOC-SHARE` hook checks only mail and Chat
- Touching `modules/{skparagraph,skunicode}/`: also run the Flutter web CanvasKit suite
  `felt test --suite chrome-dart2js-canvaskit-canvaskit` on baseline and patched Skia. The upload
  hook does not check this one
- Bugs: new side-bug on `master` or external issue → `paragraph-bug-prioritization` intake first;
  every fix starts with a failing contract test (run `mutation_check.py` and
  `verify_test_liveness.py`; the `TEST-LIVENESS` hook blocks upload until they pass); never
  present a post-fix-only run as proof the bug is fixed unless it failed before the fix, and say
  explicitly when an auxiliary fuzzer or corpus run only checks the new code for regressions
- Source of truth (code vs. docs): executable code and active configs (`infra/`, `BUILD`, CI/CQ
  verifiers) win over `.md` docs for what a system actually does (`what is`); header/API contracts
  and tests define required behavior (`what should be`). When code and docs disagree, name both
  values with exact `file:line` (`stale doc` vs `code bug`)

Never: paraphrase identifiers; claim how a parser, build, CI/CQ or runtime works without
`file:line`; make a negative claim ("project has no X check") from docs or one file (e.g.
`PRESUBMIT.py`) without a repo-wide search (`git grep -i`, including `infra/`); leave placeholders
(`TBD`, `TODO`, `[insert ...]`, empty links) in delivered text.
