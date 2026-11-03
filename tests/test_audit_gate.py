#!/usr/bin/env python3
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

        # Tier 2: real code change -> read-only deep audit on tier="pro", manual-only guardrails
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
        self.assertIn("run_webparagraph_corpus.py", dec_t2["prompt"])
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


if __name__ == "__main__":
    unittest.main()
