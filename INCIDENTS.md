# Incidents Behind the Rules

Why each rule exists. Not loaded into context; read when a rule looks odd or before deleting one.

| Date | Rule | What happened |
|---|---|---|
| 2026-10-30, 2026-11-02 | `rules/gmail_drafts.md` | The `google_gmail` MCP (`gmailmcp.googleapis.com`) rewrote every URL in drafts into expiring 24h `google.com/url?q=...` redirects; three `update_draft` calls while a reply was open left three draft versions in the Gmail UI |
| 2026-11-02 | `rules/bug_closing_fixed_by.md` | crbug/576664769 was reopened by Chrome Security bots: "Code changes" listed the fix (Skia CL 1404196) and a test-only follow-up (Skia CL 1411716) with "Fixed By Code Changes" empty |
| 2026-11-02 | `rules/bug_reading_authorization.md` | The agent could not read crbug/466929543 (redacted) and asked her to paste it |
| 2026-11-02 | `rules/clickable_links.md` | A reply mentioned a draft on PS1 as a bare `77640dee_0908b885` with no links |
| 2026-11-02 | `rules/communication_style.md` (address) | The agent switched to "вы" while `communication_style.md` was over the rules budget and not injected |
| 2026-11-03 | `rules/security_merge_reassign.md` | `safe-reassign` after `safe-cc` failed with `FAILED_PRECONDITION`: `SafeReassignIssue` checks Collaborators, not CC |
| — | `plugins/keeper/rules/skia_headers_and_font_tests.md` (headers) | Skia CL 1409697 PS4 broke `Build-Ubuntu24.04-Clang-x86_64-Debug-FreeType_ASAN` with `unused parameter 'firstLineIndent'` in `Paragraph.h`; local GCC build was green |
| — | `plugins/keeper/rules/skia_headers_and_font_tests.md` (fonts) | Skia CL 1404196 (`3d70807b95`) pinned FreeType bounds `4071x65527` in `SkRemoteGlyphCache_b576664769`; Linux/Windows trybots passed, Mac14/Mac15 `NativeFonts_Fontations` broke post-submit; fixed in Skia CL 1411716 |
| — | `rules/outgoing_text_verification.md` (CanvasKit suite) | Skia CL 1418376 broke the Flutter roll PR 196144 on `SkParagraph kitchensink` (`LineMetrics.left` 2.5 → 0) and was reverted in Skia CL 1420816 |
| — | `skills/amnesia-precommit/references/commit_message.md` (`Tested:`) | Skia CL 1417397 PS1 kept only a single-test `Test:` line after all gates were green |
| — | `skills/amnesia-precommit/references/commit_message.md` (google3 wording) | A draft on Skia CL 1418836 PS1 blurred the google3 corpus into "Google-internal clients" |
| — | `skills/amnesia-precommit/SKILL.md` (no silent `Not covered:` demotion) | On Skia CL 1418376 PS2–PS6, Stage A caught `B3` visual-vs-logical RTL shift loss and the host agent silently skipped `keeper_skeptic` and wrote `"Not covered: ... for bidi lines"` |
| — | `skills/amnesia-precommit/references/gates.md` (SkParagraph perf gate) | Skia `daf82dad48` (`SkASSERT_RELEASE` in the `SkSpan` ctor, `include/core/`) slowed `LongLayout` by 21.6% and no gate noticed |
| 2026-11-07 | rules budget | `communication_style.md` and `agentapi_visible_chats.md` exceeded the rules token budget and were silently not injected; always-on rules were ~83 KB |
| 2026-11-07 | `hooks/pre_tool_guard.py` `STALE-PATCHSET GUARD` (upload block, commit/worktree warning); `rules/communication_style.md` (latest patchset) | Chat 2a7d959b uploaded 13 SkoreText CLs from worktree `skia_wt_1407718` (branch `ct-single-pass-main`, built on the Nov 6 20:55 UTC patchsets) over the Nov 7 00:15 UTC round (branch `r86-clean-ifdefs`), dropping the build-level source lists and the FontCollection round, e.g. [Skia CL 1406897 PS14](https://skia-review.googlesource.com/c/skia/+/1406897/14) → [PS15](https://skia-review.googlesource.com/c/skia/+/1406897/15); it saw the mismatch (step 234) and called the newer patchset "old" |
