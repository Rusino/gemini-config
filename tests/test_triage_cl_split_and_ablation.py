#!/usr/bin/env python3
"""Contract tests for split_refactor 100% parity mode and incoming perf Ablation Gate."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "skills", "review-triage", "scripts")
    ),
)
sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")),
)

import triage_cl
import triage_store


def _run_triage(patch: str, subject: str, description: str = "", **extra) -> tuple[dict, str]:
    """Parse patch and return (analysis_dict, markdown_report) for unit assertions."""
    cl_data = {
        "source": "Gerrit (skia-review.googlesource.com)",
        "host": "skia-review.googlesource.com",
        "id": "1500001",
        "patchset": 1,
        "subject": subject,
        "owner": "External Dev",
        "owner_email": "dev@chromium.org",
        "description": description or subject,
        "commit_message": description or subject,
        "patch": patch,
        "detail": {"project": "skia"},
    }
    cl_data.update(extra)
    files = triage_cl.parse_patch(patch)
    analysis = triage_cl.analyze_review(cl_data, files)
    report = triage_cl.generate_report(cl_data, analysis, files)
    return analysis, report


class SplitRefactorParityContractTest(unittest.TestCase):

    # Verify clean Skia impl split enters split_refactor without false missing-test pushback
    def test_clean_skia_impl_split_suppresses_missing_tests_and_verifies_parity(self):
        moved_lines = [
            f"    fLines[{i}].markDirty(width, height);"
            for i in range(42)
        ]
        minus_block = "\n".join(f"-{ln}" for ln in moved_lines)
        plus_block = "\n".join(f"+{ln}" for ln in moved_lines)
        patch = (
            "diff --git a/modules/skparagraph/src/ParagraphImpl.cpp "
            "b/modules/skparagraph/src/ParagraphImpl.cpp\n"
            "--- a/modules/skparagraph/src/ParagraphImpl.cpp\n"
            "+++ b/modules/skparagraph/src/ParagraphImpl.cpp\n"
            "@@ -100,46 +100,2 @@\n"
            "-void ParagraphImpl::relayoutLines(SkScalar width, SkScalar height) {\n"
            "-#if defined(SK_BUILD_FOR_WIN)\n"
            "-    // TODO(skia:1234): keep DirectWrite line metrics monotonic\n"
            f"{minus_block}\n"
            "-#endif\n"
            "-}\n"
            "diff --git a/modules/skparagraph/src/ParagraphRelayout.h "
            "b/modules/skparagraph/src/ParagraphRelayout.h\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/modules/skparagraph/src/ParagraphRelayout.h\n"
            "@@ -0,0 +1,6 @@\n"
            "+#ifndef ParagraphRelayout_DEFINED\n"
            "+#define ParagraphRelayout_DEFINED\n"
            "+#include \"modules/skparagraph/src/ParagraphImpl.h\"\n"
            "+#endif\n"
            "diff --git a/modules/skparagraph/src/ParagraphRelayout.cpp "
            "b/modules/skparagraph/src/ParagraphRelayout.cpp\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/modules/skparagraph/src/ParagraphRelayout.cpp\n"
            "@@ -0,0 +1,47 @@\n"
            "+#include \"modules/skparagraph/src/ParagraphRelayout.h\"\n"
            "+void ParagraphImpl::relayoutLines(SkScalar width, SkScalar height) {\n"
            "+#if defined(SK_BUILD_FOR_WIN)\n"
            "+    // TODO(skia:1234): keep DirectWrite line metrics monotonic\n"
            f"{plus_block}\n"
            "+#endif\n"
            "+}\n"
            "diff --git a/modules/skparagraph/skparagraph.gni "
            "b/modules/skparagraph/skparagraph.gni\n"
            "--- a/modules/skparagraph/skparagraph.gni\n"
            "+++ b/modules/skparagraph/skparagraph.gni\n"
            "@@ -20,2 +20,4 @@\n"
            "+  \"$_src/ParagraphRelayout.cpp\",\n"
            "+  \"$_src/ParagraphRelayout.h\",\n"
            "diff --git a/modules/skparagraph/BUILD.bazel "
            "b/modules/skparagraph/BUILD.bazel\n"
            "--- a/modules/skparagraph/BUILD.bazel\n"
            "+++ b/modules/skparagraph/BUILD.bazel\n"
            "@@ -30,2 +30,4 @@\n"
            "+    \"src/ParagraphRelayout.cpp\",\n"
            "+    \"src/ParagraphRelayout.h\",\n"
        )
        analysis, report = _run_triage(
            patch,
            "[SkParagraph] Split ParagraphRelayout out of ParagraphImpl",
            "Pure file split moving relayoutLines into ParagraphRelayout.cpp.",
        )
        self.assertEqual(analysis["review_profile"], "split_refactor")
        self.assertEqual(analysis["category"], "SURGICAL_AUDIT")
        self.assertFalse(analysis["is_missing_tests"])
        self.assertTrue(analysis["split_parity"]["is_split_refactor"])
        self.assertTrue(analysis["split_parity"]["ok"])
        self.assertIn("Split / Refactor 100% Parity Audit", report)

        with tempfile.TemporaryDirectory() as tmpdir:
            payload = {
                "metadata": {"id": "1500001", "subject": "Split ParagraphRelayout"},
                "analysis": {k: v for k, v in analysis.items() if k != "top_hunks"},
            }
            json.dumps(payload)
            saved = triage_store.save_from_triage_cl_output(
                "https://skia-review.googlesource.com/c/skia/+/1500001",
                payload,
                report,
                results_dir=tmpdir,
            )
            self.assertTrue(saved.exists())

    # Catch dropped Skia DEF_TEST and silent DISABLED_ or UNIX_ONLY_TEST narrowing on suite splits
    def test_skia_test_split_catches_dropped_and_disabled_tests(self):
        patch = (
            "diff --git a/modules/skparagraph/tests/SkParagraphTest.cpp "
            "b/modules/skparagraph/tests/SkParagraphTest.cpp\n"
            "--- a/modules/skparagraph/tests/SkParagraphTest.cpp\n"
            "+++ b/modules/skparagraph/tests/SkParagraphTest.cpp\n"
            "@@ -10,18 +10,1 @@\n"
            "-DEF_TEST(SkParagraph_LTRCaret, reporter) {\n"
            "-    REPORTER_ASSERT(reporter, checkCaret(0) == 0);\n"
            "-    REPORTER_ASSERT(reporter, checkCaret(1) == 1);\n"
            "-}\n"
            "-DEF_TEST(SkParagraph_RTLCaret, reporter) {\n"
            "-    REPORTER_ASSERT(reporter, checkRtlCaret(0) == 4);\n"
            "-    REPORTER_ASSERT(reporter, checkRtlCaret(1) == 3);\n"
            "-}\n"
            "-DEF_GANESH_TEST_FOR_RENDERING_CONTEXTS(SkParagraph_GpuSlug, r, ctxInfo) {\n"
            "-    REPORTER_ASSERT(r, ctxInfo.directContext() != nullptr);\n"
            "-}\n"
            "-DEF_TEST(SkParagraph_DroppedEdge, reporter) {\n"
            "-    REPORTER_ASSERT(reporter, checkDroppedEdge());\n"
            "-}\n"
            "diff --git a/modules/skparagraph/tests/SkParagraphCaretTest.cpp "
            "b/modules/skparagraph/tests/SkParagraphCaretTest.cpp\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/modules/skparagraph/tests/SkParagraphCaretTest.cpp\n"
            "@@ -0,0 +1,14 @@\n"
            "+UNIX_ONLY_TEST(SkParagraph_LTRCaret, reporter) {\n"
            "+    REPORTER_ASSERT(reporter, checkCaret(0) == 0);\n"
            "+    REPORTER_ASSERT(reporter, checkCaret(1) == 1);\n"
            "+}\n"
            "+DEF_TEST(DISABLED_SkParagraph_RTLCaret, reporter) {\n"
            "+    REPORTER_ASSERT(reporter, checkRtlCaret(0) == 4);\n"
            "+    REPORTER_ASSERT(reporter, checkRtlCaret(1) == 3);\n"
            "+}\n"
            "+DEF_GANESH_TEST_FOR_RENDERING_CONTEXTS(SkParagraph_GpuSlug, r, ctxInfo) {\n"
            "+    REPORTER_ASSERT(r, ctxInfo.directContext() != nullptr);\n"
            "+}\n"
            "diff --git a/modules/skparagraph/skparagraph.gni "
            "b/modules/skparagraph/skparagraph.gni\n"
            "--- a/modules/skparagraph/skparagraph.gni\n"
            "+++ b/modules/skparagraph/skparagraph.gni\n"
            "@@ -50,1 +50,2 @@\n"
            "+  \"$_tests/SkParagraphCaretTest.cpp\",\n"
            "diff --git a/modules/skparagraph/tests/BUILD.bazel "
            "b/modules/skparagraph/tests/BUILD.bazel\n"
            "--- a/modules/skparagraph/tests/BUILD.bazel\n"
            "+++ b/modules/skparagraph/tests/BUILD.bazel\n"
            "@@ -10,1 +10,2 @@\n"
            "+    \"SkParagraphCaretTest.cpp\",\n"
        )
        analysis, report = _run_triage(patch, "Split caret tests out of SkParagraphTest")
        sp = analysis["split_parity"]
        self.assertTrue(sp["is_split_refactor"])
        self.assertFalse(sp["ok"])
        self.assertEqual(sp["tests"]["base_count"], 4)
        self.assertEqual(sp["tests"]["patched_count"], 3)
        self.assertTrue(any("SkParagraph_DroppedEdge" in d for d in sp["tests"]["dropped_tests"]))
        self.assertEqual(len(sp["tests"]["newly_disabled_tests"]), 2)
        self.assertIn("SkParagraph_DroppedEdge", report)

    # Catch dropped #if / BUILDFLAG guards, dropped TODOs, and Skia gn/*.gni vs BUILD.bazel desync
    def test_split_catches_dropped_if_guard_todo_and_gni_bazel_desync(self):
        patch = (
            "diff --git a/modules/skunicode/src/SkUnicode_icu.cpp "
            "b/modules/skunicode/src/SkUnicode_icu.cpp\n"
            "--- a/modules/skunicode/src/SkUnicode_icu.cpp\n"
            "+++ b/modules/skunicode/src/SkUnicode_icu.cpp\n"
            "@@ -20,14 +20,1 @@\n"
            "-bool extractBidiRegions(const char* utf8, int bytes) {\n"
            "-#if defined(SK_UNICODE_ICU_IMPLEMENTATION)\n"
            "-    // TODO(icu): preserve trailing isolate level across paragraph split\n"
            "-    if (bytes <= 0) {\n"
            "-        return false;\n"
            "-    }\n"
            "-    int level = computeBaseLevel(utf8, bytes);\n"
            "-    return level >= 0;\n"
            "-#endif\n"
            "-}\n"
            "diff --git a/modules/skunicode/src/SkUnicode_icu_bidi.cpp "
            "b/modules/skunicode/src/SkUnicode_icu_bidi.cpp\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/modules/skunicode/src/SkUnicode_icu_bidi.cpp\n"
            "@@ -0,0 +1,9 @@\n"
            "+bool extractBidiRegions(const char* utf8, int bytes) {\n"
            "+    if (bytes <= 0) {\n"
            "+        return false;\n"
            "+    }\n"
            "+    int level = computeBaseLevel(utf8, bytes);\n"
            "+    return level >= 0;\n"
            "+}\n"
            "diff --git a/gn/skunicode.gni b/gn/skunicode.gni\n"
            "--- a/gn/skunicode.gni\n"
            "+++ b/gn/skunicode.gni\n"
            "@@ -10,1 +10,2 @@\n"
            "+  \"$_modules/skunicode/src/SkUnicode_icu_bidi.cpp\",\n"
        )
        analysis, report = _run_triage(
            patch, "Extract SkUnicode_icu_bidi.cpp from SkUnicode_icu.cpp"
        )
        sp = analysis["split_parity"]
        self.assertTrue(sp["is_split_refactor"])
        self.assertFalse(sp["ok"])
        self.assertTrue(
            any("SK_UNICODE_ICU_IMPLEMENTATION" in g for g in sp["dropped_guards"])
        )
        self.assertTrue(any("TODO(icu)" in t for t in sp["dropped_todos"]))
        self.assertTrue(
            any("BUILD.bazel" in b for b in sp["build"]["build_sync_issues"])
        )
        self.assertIn("GN vs Bazel desync", report)

    # Verify Chromium BUILD.gn + GoogleTest and Flutter Dart testWidgets split parity support
    def test_chromium_gtest_and_flutter_dart_split_parity(self):
        cr_patch = (
            "diff --git a/ui/gfx/render_text_unittest.cc b/ui/gfx/render_text_unittest.cc\n"
            "--- a/ui/gfx/render_text_unittest.cc\n"
            "+++ b/ui/gfx/render_text_unittest.cc\n"
            "@@ -10,12 +10,1 @@\n"
            "-TEST_F(RenderTextTest, ElideLongRtlRun) {\n"
            "-#if BUILDFLAG(IS_WIN)\n"
            "-  RenderTextHarfBuzz text;\n"
            "-  text.SetDisplayRect(gfx::Rect(0, 0, 40, 20));\n"
            "-  EXPECT_FALSE(text.GetDisplayText().empty());\n"
            "-#endif\n"
            "-}\n"
            "diff --git a/ui/gfx/render_text_elide_unittest.cc "
            "b/ui/gfx/render_text_elide_unittest.cc\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/ui/gfx/render_text_elide_unittest.cc\n"
            "@@ -0,0 +1,8 @@\n"
            "+TEST_F(RenderTextElideTest, ElideLongRtlRun) {\n"
            "+#if BUILDFLAG(IS_WIN)\n"
            "+  RenderTextHarfBuzz text;\n"
            "+  text.SetDisplayRect(gfx::Rect(0, 0, 40, 20));\n"
            "+  EXPECT_FALSE(text.GetDisplayText().empty());\n"
            "+#endif\n"
            "+}\n"
            "diff --git a/ui/gfx/BUILD.gn b/ui/gfx/BUILD.gn\n"
            "--- a/ui/gfx/BUILD.gn\n"
            "+++ b/ui/gfx/BUILD.gn\n"
            "@@ -100,1 +100,2 @@\n"
            "+  \"render_text_elide_unittest.cc\",\n"
        )
        cr_analysis, _ = _run_triage(
            cr_patch,
            "Split render_text_elide_unittest.cc out of render_text_unittest.cc",
            host="chromium-review.googlesource.com",
            detail={"project": "chromium/src"},
        )
        self.assertTrue(cr_analysis["split_parity"]["is_split_refactor"])
        self.assertTrue(cr_analysis["split_parity"]["ok"])
        self.assertFalse(cr_analysis["split_parity"]["build"]["is_skia"])

        dart_patch = (
            "diff --git a/packages/flutter/test/painting/text_painter_test.dart "
            "b/packages/flutter/test/painting/text_painter_test.dart\n"
            "--- a/packages/flutter/test/painting/text_painter_test.dart\n"
            "+++ b/packages/flutter/test/painting/text_painter_test.dart\n"
            "@@ -10,9 +10,1 @@\n"
            "-  testWidgets('caret offset with strut halfLeading', (WidgetTester tester) async {\n"
            "-    final TextPainter painter = TextPainter(textDirection: TextDirection.ltr);\n"
            "-    painter.layout(maxWidth: 120.0);\n"
            "-    expect(painter.width, lessThanOrEqualTo(120.0));\n"
            "-    painter.dispose();\n"
            "-  });\n"
            "diff --git a/packages/flutter/test/painting/text_painter_strut_test.dart "
            "b/packages/flutter/test/painting/text_painter_strut_test.dart\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/packages/flutter/test/painting/text_painter_strut_test.dart\n"
            "@@ -0,0 +1,7 @@\n"
            "+  testWidgets('caret offset with strut halfLeading', (WidgetTester tester) async {\n"
            "+    final TextPainter painter = TextPainter(textDirection: TextDirection.ltr);\n"
            "+    painter.layout(maxWidth: 120.0);\n"
            "+    expect(painter.width, lessThanOrEqualTo(120.0));\n"
            "+    painter.dispose();\n"
            "+  }, skip: true);\n"
        )
        dart_analysis, _ = _run_triage(
            dart_patch,
            "Split strut tests out of text_painter_test.dart",
            source="GitHub (https://github.com/flutter/flutter)",
            host="",
            detail={},
        )
        self.assertTrue(dart_analysis["split_parity"]["is_split_refactor"])
        self.assertFalse(dart_analysis["split_parity"]["ok"])
        self.assertEqual(len(dart_analysis["split_parity"]["tests"]["newly_disabled_tests"]), 1)


class IncomingPerfAblationGateContractTest(unittest.TestCase):

    # Flag external perf CL bundling cache + fast-path + loop rewrite without isolated ablation
    def test_triggers_on_external_multi_mechanism_perf_cl_without_ablation(self):
        patch = (
            "diff --git a/modules/skparagraph/src/OneLineShaper.cpp "
            "b/modules/skparagraph/src/OneLineShaper.cpp\n"
            "--- a/modules/skparagraph/src/OneLineShaper.cpp\n"
            "+++ b/modules/skparagraph/src/OneLineShaper.cpp\n"
            "@@ -50,6 +50,15 @@\n"
            "-    for (size_t i = 0; i < fClusters.size(); ++i) {\n"
            "-        shapeCluster(fClusters[i]);\n"
            "-    }\n"
            "+    if (fClusters.size() < 64 && fAsciiFastPath) {\n"
            "+        return shapeAsciiFastPath();\n"
            "+    }\n"
            "+    skia_private::THashMap<uint32_t, ShapedRun> runCache;\n"
            "+    for (const auto& cluster : fClusters) {\n"
            "+        shapeWithCache(cluster, &runCache);\n"
            "+    }\n"
        )
        analysis, report = _run_triage(
            patch,
            "[SkParagraph] Optimize OneLineShaper with ASCII fast-path and run cache",
            "Speeds up txt_benchmarks ParagraphLayoutBench by 14.2% on Linux.",
        )
        gate = analysis["ablation_gate"]
        self.assertTrue(gate["is_perf_cl"])
        self.assertTrue(gate["triggered"])
        self.assertTrue(gate["multi_mechanism_unablated"])
        self.assertGreaterEqual(len(gate["mechanisms"]), 3)
        self.assertIn("txt_benchmarks", gate["cited_benchmarks"])
        self.assertIn("ParagraphLayoutBench", gate["cited_benchmarks"])
        self.assertTrue(any("fAsciiFastPath" in g for g in gate["code_guards"]))
        self.assertIn("Incoming Perf Ablation Gate", report)

    # Clear multi-mechanism warning when description reports baseline vs A vs B vs A+B ablation
    def test_ablation_evidence_clears_bundling_trigger_and_skips_bugfix_trailers(self):
        patch = (
            "diff --git a/modules/skparagraph/src/OneLineShaper.cpp "
            "b/modules/skparagraph/src/OneLineShaper.cpp\n"
            "--- a/modules/skparagraph/src/OneLineShaper.cpp\n"
            "+++ b/modules/skparagraph/src/OneLineShaper.cpp\n"
            "@@ -50,4 +50,9 @@\n"
            "-    for (size_t i = 0; i < n; ++i) { run(i); }\n"
            "+    if (n == 0) return;\n"
            "+    THashMap<int, int> memo;\n"
            "+    for (size_t i = 0; i < n; ++i) { runMemo(i, &memo); }\n"
        )
        desc = (
            "Optimize OneLineShaper cluster memoization.\n\n"
            "Ablation on txt_benchmarks:\n"
            "- baseline: 120 us\n"
            "- variant A only (early return): 114 us\n"
            "- variant B only (memo): 96 us\n"
            "- A+B combined: 91 us (-24.1%)"
        )
        analysis, _ = _run_triage(patch, "Optimize OneLineShaper cluster memoization", desc)
        gate = analysis["ablation_gate"]
        self.assertTrue(gate["is_perf_cl"])
        self.assertTrue(gate["has_ablation_data"])
        self.assertFalse(gate["multi_mechanism_unablated"])
        self.assertFalse(gate["triggered"])

        # Non-perf bugfix mentioning txt_benchmarks in Tested: or fixing stale cache stays quiet
        bugfix_analysis, _ = _run_triage(
            patch,
            "Fix stale cache invalidation on font fallback",
            "Fix stale cache entry.\n\nTested: dm --match SkParagraph, txt_benchmarks (neutral)",
        )
        self.assertFalse(bugfix_analysis["ablation_gate"]["is_perf_cl"])

        # Self-authored CLs use perf-optimization-loop and skip incoming external Ablation Gate
        self_analysis, _ = _run_triage(
            patch,
            "Optimize OneLineShaper",
            "Speed up shaping.",
            owner="Julia Lavrova",
            owner_email="jlavrova@google.com",
        )
        self.assertFalse(self_analysis["ablation_gate"]["is_perf_cl"])

    # Avoid double-counting cache-hit early return or flagging benchmark-harness-only CLs
    def test_single_cache_hit_return_and_bench_only_cl_avoid_false_ablation_triggers(self):
        cache_patch = (
            "diff --git a/modules/skparagraph/src/OneLineShaper.cpp "
            "b/modules/skparagraph/src/OneLineShaper.cpp\n"
            "--- a/modules/skparagraph/src/OneLineShaper.cpp\n"
            "+++ b/modules/skparagraph/src/OneLineShaper.cpp\n"
            "@@ -50,2 +50,6 @@\n"
            "+    if (const ShapedRun* hit = fRunCache.find(runKey)) {\n"
            "+        return *hit;\n"
            "+    }\n"
            "+    skia_private::THashMap<uint32_t, ShapedRun> localCache;\n"
        )
        cache_analysis, _ = _run_triage(
            cache_patch,
            "[SkParagraph] Cache shaped runs in OneLineShaper",
            "Speeds up txt_benchmarks ParagraphLayoutBench by 18.4% on Linux.",
        )
        gate = cache_analysis["ablation_gate"]
        self.assertTrue(gate["is_perf_cl"])
        self.assertEqual(gate["mechanisms"], ["caching_or_memoization"])
        self.assertFalse(gate["multi_mechanism_unablated"])
        self.assertFalse(gate["triggered"])

        bench_only_patch = (
            "diff --git a/bench/ParagraphBench.cpp b/bench/ParagraphBench.cpp\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/bench/ParagraphBench.cpp\n"
            "@@ -0,0 +1,9 @@\n"
            "+void runBenchLoop(int loops) {\n"
            "+    if (loops <= 0) return;\n"
            "+    skia_private::THashMap<int, int> warm;\n"
            "+    for (int i = 0; i < loops; ++i) { warm.set(i, i); }\n"
            "+}\n"
        )
        bench_analysis, _ = _run_triage(
            bench_only_patch,
            "Add ParagraphBench warmup microbenchmark",
            "Adds a new benchmark harness under bench/.",
        )
        self.assertFalse(bench_analysis["ablation_gate"]["is_perf_cl"])
        self.assertFalse(bench_analysis["ablation_gate"]["triggered"])


class SplitRefactorEdgeTrapTest(unittest.TestCase):

    # Parse headerless multi-file unified diffs and isolate unclosed #if 0 per hunk
    def test_headerless_unified_diff_and_cross_hunk_if0_isolation(self):
        moved = "\n".join(f"    layoutLine({i});" for i in range(12))
        minus_moved = "\n".join(f"-{ln}" for ln in moved.splitlines())
        plus_moved = "\n".join(f"+{ln}" for ln in moved.splitlines())
        patch = (
            "--- a/modules/skparagraph/src/ParagraphImpl.cpp\t2026-11-10\n"
            "+++ b/modules/skparagraph/src/ParagraphImpl.cpp\t2026-11-10\n"
            "@@ -5,3 +5,1 @@\n"
            "-#if 0\n"
            "-    legacyDebugDump();\n"
            "@@ -80,16 +78,1 @@\n"
            "-void relayoutMovedHelper() {\n"
            "-#if defined(SK_BUILD_FOR_MAC)\n"
            f"{minus_moved}\n"
            "-#endif\n"
            "-}\n"
            "--- /dev/null\t2026-11-10\n"
            "+++ b/modules/skparagraph/src/ParagraphUtil.h\t2026-11-10\n"
            "@@ -0,0 +1,4 @@\n"
            "+#ifndef SkParagraphUtil_DEFINED\n"
            "+void relayoutMovedHelper();\n"
            "+#endif\n"
            "--- /dev/null\t2026-11-10\n"
            "+++ b/modules/skparagraph/src/ParagraphUtil.cpp\t2026-11-10\n"
            "@@ -0,0 +1,14 @@\n"
            "+void relayoutMovedHelper() {\n"
            f"{plus_moved}\n"
            "+}\n"
            "--- a/modules/skparagraph/skparagraph.gni\t2026-11-10\n"
            "+++ b/modules/skparagraph/skparagraph.gni\t2026-11-10\n"
            "@@ -10,1 +10,3 @@\n"
            "+  \"$_src/ParagraphUtil.h\",\n"
            "+  \"$_src/ParagraphUtil.cpp\",\n"
            "--- a/modules/skparagraph/BUILD.bazel\t2026-11-10\n"
            "+++ b/modules/skparagraph/BUILD.bazel\t2026-11-10\n"
            "@@ -10,1 +10,3 @@\n"
            "+    \"src/ParagraphUtil.h\",\n"
            "+    \"src/ParagraphUtil.cpp\",\n"
        )
        files = triage_cl.parse_patch(patch)
        self.assertEqual(len(files), 5)
        analysis, _ = _run_triage(patch, "Split ParagraphUtil out of ParagraphImpl")
        sp = analysis["split_parity"]
        self.assertTrue(sp["is_split_refactor"])
        self.assertTrue(any("SK_BUILD_FOR_MAC" in g for g in sp["dropped_guards"]))
        self.assertFalse(any("SkParagraphUtil_DEFINED" in g for g in sp["dropped_guards"]))

    # Reject mixed refactor + untested feature branches from split_refactor and keep REASSIGN clean
    def test_mixed_refactor_plus_untested_feature_branches_rejected_from_split_mode(self):
        moved = [f"    fRuns[{i}].resetAdvance();" for i in range(55)]
        minus_b = "\n".join(f"-{ln}" for ln in moved)
        plus_b = "\n".join(f"+{ln}" for ln in moved)
        new_feature = "\n".join([
            "+    if (fNewEllipsisFallback) {",
            "+        for (int k = 0; k < count; ++k) {",
            "+            switch (clusters[k].kind()) { case 1: adjust(k); break; }",
            "+        }",
            "+    }",
        ])
        patch = (
            "diff --git a/modules/skparagraph/src/ParagraphImpl.cpp "
            "b/modules/skparagraph/src/ParagraphImpl.cpp\n"
            "--- a/modules/skparagraph/src/ParagraphImpl.cpp\n"
            "+++ b/modules/skparagraph/src/ParagraphImpl.cpp\n"
            "@@ -10,56 +10,1 @@\n"
            f"{minus_b}\n"
            "diff --git a/modules/skparagraph/src/ParagraphRuns.cpp "
            "b/modules/skparagraph/src/ParagraphRuns.cpp\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/modules/skparagraph/src/ParagraphRuns.cpp\n"
            "@@ -0,0 +1,63 @@\n"
            f"{plus_b}\n"
            f"{new_feature}\n"
        )
        analysis, _ = _run_triage(
            patch,
            "Split ParagraphRuns and add new ellipsis fallback mode",
            "Moves run reset loop and adds new ellipsis fallback branch.",
        )
        self.assertFalse(analysis["split_parity"]["is_split_refactor"])
        self.assertEqual(analysis["review_profile"], "standard")
        self.assertTrue(analysis["is_missing_tests"])
        self.assertEqual(analysis["category"], "PUSHBACK")

        # Pure git file rename reports 100% parity rather than 0% match
        rename_patch = (
            "diff --git a/modules/skparagraph/src/OldName.cpp "
            "b/modules/skparagraph/src/NewName.cpp\n"
            "similarity index 100%\n"
            "rename from modules/skparagraph/src/OldName.cpp\n"
            "rename to modules/skparagraph/src/NewName.cpp\n"
            "diff --git a/modules/skparagraph/skparagraph.gni "
            "b/modules/skparagraph/skparagraph.gni\n"
            "--- a/modules/skparagraph/skparagraph.gni\n"
            "+++ b/modules/skparagraph/skparagraph.gni\n"
            "@@ -1,1 +1,1 @@\n"
            "-  \"$_src/OldName.cpp\",\n"
            "+  \"$_src/NewName.cpp\",\n"
            "diff --git a/modules/skparagraph/BUILD.bazel "
            "b/modules/skparagraph/BUILD.bazel\n"
            "--- a/modules/skparagraph/BUILD.bazel\n"
            "+++ b/modules/skparagraph/BUILD.bazel\n"
            "@@ -1,1 +1,1 @@\n"
            "-    \"src/OldName.cpp\",\n"
            "+    \"src/NewName.cpp\",\n"
        )
        ren_analysis, ren_report = _run_triage(rename_patch, "Rename OldName.cpp to NewName.cpp")
        self.assertTrue(ren_analysis["split_parity"]["is_split_refactor"])
        self.assertEqual(ren_analysis["split_parity"]["add_relocated_ratio"], 1.0)
        self.assertNotIn("0%` `+`", ren_report)

        # Out-of-domain Vulkan GPU split stays REASSIGN with standard profile and no parity block
        vk_patch = patch.replace(
            "modules/skparagraph/src/ParagraphImpl.cpp", "src/gpu/ganesh/vk/VkGpu.cpp"
        ).replace("modules/skparagraph/src/ParagraphRuns.cpp", "src/gpu/ganesh/vk/VkOps.cpp")
        vk_analysis, vk_report = _run_triage(vk_patch[:vk_patch.rfind("+    if (")], "Split VkOps")
        self.assertEqual(vk_analysis["category"], "REASSIGN")
        self.assertEqual(vk_analysis["review_profile"], "standard")
        self.assertNotIn("Split / Refactor 100% Parity Audit", vk_report)

    # Catch >500-char Dart skip, raw test strings, added_tests, dropped overloads, and Bazel resort
    def test_long_dart_skip_raw_names_overloads_added_tests_and_bazel_resort(self):
        long_body = "\n".join(
            f"    expect(painter.getOffsetForCaret(TextPosition(offset: {i}), Rect.zero).dx, {i});"
            for i in range(10)
        )
        minus_body = "\n".join(f"-{ln}" for ln in long_body.splitlines())
        plus_body = "\n".join(f"+{ln}" for ln in long_body.splitlines())
        dart_patch = (
            "diff --git a/packages/flutter/test/painting/a_test.dart "
            "b/packages/flutter/test/painting/a_test.dart\n"
            "--- a/packages/flutter/test/painting/a_test.dart\n"
            "+++ b/packages/flutter/test/painting/a_test.dart\n"
            "@@ -1,12 +1,1 @@\n"
            "-  testWidgets(r'raw caret test > 500 chars', (WidgetTester tester) async {\n"
            f"{minus_body}\n"
            "-  });\n"
            "diff --git a/packages/flutter/test/painting/b_test.dart "
            "b/packages/flutter/test/painting/b_test.dart\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/packages/flutter/test/painting/b_test.dart\n"
            "@@ -0,0 +1,15 @@\n"
            "+  testWidgets(r'raw caret test > 500 chars', (WidgetTester tester) async {\n"
            f"{plus_body}\n"
            "+  }, skip: 'migrating');\n"
            "+  test('extra unannounced test', () {\n"
            "+    expect(1, 1);\n"
            "+  });\n"
        )
        d_analysis, d_report = _run_triage(
            dart_patch,
            "Split raw caret test into b_test.dart",
            source="GitHub (https://github.com/flutter/flutter)",
            host="",
            detail={},
        )
        dsp = d_analysis["split_parity"]
        self.assertTrue(dsp["is_split_refactor"])
        self.assertFalse(dsp["ok"])
        self.assertEqual(len(dsp["tests"]["newly_disabled_tests"]), 1)
        self.assertEqual(len(dsp["tests"]["added_tests"]), 1)
        self.assertIn("extra unannounced test", d_report)

        # Overload drop + Ganesh->DEF_TEST macro narrowing + intra-file BUILD.bazel resort
        sk_body = "\n".join(f"    builder.pushByte({i});" for i in range(8))
        sk_minus = "\n".join(f"-{ln}" for ln in sk_body.splitlines())
        sk_plus = "\n".join(f"+{ln}" for ln in sk_body.splitlines())
        sk_patch = (
            "diff --git a/modules/skparagraph/tests/SkParagraphTest.cpp "
            "b/modules/skparagraph/tests/SkParagraphTest.cpp\n"
            "--- a/modules/skparagraph/tests/SkParagraphTest.cpp\n"
            "+++ b/modules/skparagraph/tests/SkParagraphTest.cpp\n"
            "@@ -1,22 +1,1 @@\n"
            "-void addText(const char* utf8) {\n"
            f"{sk_minus}\n"
            "-}\n"
            "-void addText(const char* utf8, size_t len) {\n"
            "-    if (len > 0) { sink(utf8, len); }\n"
            "-}\n"
            "-DEF_GANESH_TEST_FOR_RENDERING_CONTEXTS(SkParagraph_GpuAtlas, r, ctx) {\n"
            "-    REPORTER_ASSERT(r, ctx.directContext() != nullptr);\n"
            "-}\n"
            "diff --git a/modules/skparagraph/tests/SkParagraphAtlasTest.cpp "
            "b/modules/skparagraph/tests/SkParagraphAtlasTest.cpp\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/modules/skparagraph/tests/SkParagraphAtlasTest.cpp\n"
            "@@ -0,0 +1,14 @@\n"
            "+void addText(const char* utf8) {\n"
            f"{sk_plus}\n"
            "+}\n"
            "+DEF_TEST(SkParagraph_GpuAtlas, r) {\n"
            "+    REPORTER_ASSERT(r, true);\n"
            "+}\n"
            "diff --git a/modules/skparagraph/skparagraph.gni "
            "b/modules/skparagraph/skparagraph.gni\n"
            "--- a/modules/skparagraph/skparagraph.gni\n"
            "+++ b/modules/skparagraph/skparagraph.gni\n"
            "@@ -10,1 +10,2 @@\n"
            "+  \"$_tests/SkParagraphAtlasTest.cpp\",\n"
            "diff --git a/modules/skparagraph/tests/BUILD.bazel "
            "b/modules/skparagraph/tests/BUILD.bazel\n"
            "--- a/modules/skparagraph/tests/BUILD.bazel\n"
            "+++ b/modules/skparagraph/tests/BUILD.bazel\n"
            "@@ -10,3 +10,4 @@\n"
            "-    \"SkParagraphExistingTest.cpp\",\n"
            "+    \"SkParagraphAtlasTest.cpp\",\n"
            "+    \"SkParagraphExistingTest.cpp\",\n"
        )
        sk_analysis, _ = _run_triage(sk_patch, "Split SkParagraphAtlasTest out of SkParagraphTest")
        sk_sp = sk_analysis["split_parity"]
        self.assertTrue(sk_sp["is_split_refactor"])
        self.assertTrue(any("addText" in s for s in sk_sp["dropped_symbols"]))
        self.assertEqual(len(sk_sp["tests"]["newly_disabled_tests"]), 1)
        self.assertEqual(sk_sp["build"]["build_sync_issues"], [])


if __name__ == "__main__":
    unittest.main()
