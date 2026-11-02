#!/usr/bin/env python3
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "../skills/skia-canary-advisor/scripts",
        )
    ),
)
import canary_advisor


class TestCanaryAdvisor(unittest.TestCase):

  def test_load_cl_googler_cr_plus_one_detection(self):
    fake_diff_b64 = "ZGlmZiAtLWdpdCBhL2Zvby5oIGIvZm9vLmgK"
    tests = [
        ("no_labels", {}, False),
        (
            "external_cr_plus_one",
            {
                "Code-Review": {
                    "all": [{"value": 1, "email": "external@author.com"}]
                }
            },
            False,
        ),
        (
            "googler_cr_zero",
            {
                "Code-Review": {
                    "all": [{"value": 0, "email": "googler@google.com"}]
                }
            },
            False,
        ),
        (
            "googler_cr_minus_one",
            {
                "Code-Review": {
                    "all": [{"value": -1, "email": "googler@google.com"}]
                }
            },
            False,
        ),
        (
            "googler_cr_plus_one",
            {
                "Code-Review": {
                    "all": [
                        {"value": 1, "email": "googler@google.com"},
                        {"value": 0, "email": "other@google.com"},
                    ]
                }
            },
            True,
        ),
    ]

    for name, labels, expected in tests:
      with self.subTest(name=name):
        fake_info = {
            "subject": "Test change",
            "owner": {"email": "author@example.com"},
            "revisions": {"rev1": {"_number": 1}},
            "labels": labels,
        }
        with patch.object(canary_advisor, "gerrit_json", return_value=fake_info):
          with patch.object(canary_advisor, "http_get", return_value=fake_diff_b64):
            meta = canary_advisor.load_cl("12345")
            self.assertEqual(meta["googler_cr_plus_one"], expected)

  def test_external_author_defers_canary_g3_without_googler_cr_plus_one(self):
    diff_text = (
        "diff --git a/modules/skparagraph/src/ParagraphImpl.cpp"
        " b/modules/skparagraph/src/ParagraphImpl.cpp\n"
        "--- a/modules/skparagraph/src/ParagraphImpl.cpp\n"
        "+++ b/modules/skparagraph/src/ParagraphImpl.cpp\n"
        "@@ -1,2 +1,3 @@\n"
        "+// some change\n"
    )
    fake_meta = {
        "cl": 1408157,
        "patchset": 1,
        "subject": "SkParagraph change",
        "owner": "external@author.com",
        "diff": diff_text,
        "googler_cr_plus_one": False,
    }

    args = type(
        "Args",
        (),
        {
            "cl": "1408157",
            "repo": None,
            "base": "origin/main",
            "max_symbols": 6,
            "no_search": True,
            "no_health": True,
            "no_log": True,
            "strong_only": False,
        },
    )()

    buf = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=fake_meta):
      with patch("sys.stdout", buf):
        canary_advisor.advise(args)

    out = buf.getvalue()
    self.assertIn("AGENT ACTION (execute this command):", out)
    self.assertIn("Canary-Flutter is disabled", out)
    self.assertIn("NativeFonts_FreeType_ASAN_Upload", out)
    # Canary-G3 must NOT be in the immediate AGENT ACTION
    agent_action_line = [
        line for line in out.splitlines() if line.strip().startswith("git cl try")
    ][0]
    self.assertNotIn("Canary-G3", agent_action_line)
    self.assertNotIn("Canary-Flutter", agent_action_line)
    self.assertIn("Canary-G3 requires a Googler author or Googler Code-Review+1", out)
    self.assertIn("Canary-G3 Command (run after Googler Code-Review+1 approval):", out)
    self.assertIn("follow submit-hold protocol", out)

  def test_external_author_includes_canary_g3_when_googler_cr_plus_one_granted(self):
    diff_text = (
        "diff --git a/modules/skparagraph/src/ParagraphImpl.cpp"
        " b/modules/skparagraph/src/ParagraphImpl.cpp\n"
        "--- a/modules/skparagraph/src/ParagraphImpl.cpp\n"
        "+++ b/modules/skparagraph/src/ParagraphImpl.cpp\n"
        "@@ -1,2 +1,3 @@\n"
        "+// some change\n"
    )
    fake_meta = {
        "cl": 1408157,
        "patchset": 2,
        "subject": "SkParagraph change",
        "owner": "external@author.com",
        "diff": diff_text,
        "googler_cr_plus_one": True,
    }

    args = type(
        "Args",
        (),
        {
            "cl": "1408157",
            "repo": None,
            "base": "origin/main",
            "max_symbols": 6,
            "no_search": True,
            "no_health": True,
            "no_log": True,
            "strong_only": False,
        },
    )()

    buf = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=fake_meta):
      with patch("sys.stdout", buf):
        canary_advisor.advise(args)

    out = buf.getvalue()
    agent_action_line = [
        line for line in out.splitlines() if line.strip().startswith("git cl try")
    ][0]
    self.assertIn("Canary-G3", agent_action_line)
    self.assertNotIn("Canary-Flutter", agent_action_line)
    self.assertIn("run_flutter_paragraph_tests.py --full", out)
    self.assertIn("NativeFonts_FreeType_ASAN_Upload", agent_action_line)
    self.assertNotIn("deferred until a Googler votes Code-Review+1", out)

  def test_font_backed_test_adds_mac_native_fonts_trybot(self):
    diff_text = (
        "diff --git a/tests/SkRemoteGlyphCacheTest.cpp b/tests/SkRemoteGlyphCacheTest.cpp\n"
        "--- a/tests/SkRemoteGlyphCacheTest.cpp\n"
        "+++ b/tests/SkRemoteGlyphCacheTest.cpp\n"
        "@@ -10,2 +10,4 @@\n"
        "+DEF_TEST(SkRemoteGlyphCache_Test, reporter) {\n"
        "+    auto mgr = ToolUtils::TestFontMgr();\n"
        "+}\n"
    )
    fake_meta = {
        "cl": 1404196,
        "patchset": 1,
        "subject": "Add font test",
        "owner": "googler@google.com",
        "diff": diff_text,
        "googler_cr_plus_one": True,
    }
    args = type(
        "Args",
        (),
        {
            "cl": "1404196",
            "repo": None,
            "base": "origin/main",
            "max_symbols": 6,
            "no_search": True,
            "no_health": True,
            "no_log": True,
            "strong_only": False,
        },
    )()

    buf = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=fake_meta):
      with patch("sys.stdout", buf):
        canary_advisor.advise(args)

    out = buf.getvalue()
    self.assertIn("Font-Backend Trybot [ALWAYS_RUN]", out)
    self.assertIn(
        "Test-Mac14-Clang-MacMini8.1-CPU-AVX2-x86_64-Debug-All-NativeFonts_Fontations",
        out,
    )
    agent_action_line = [
        line for line in out.splitlines() if line.strip().startswith("git cl try")
    ][0]
    self.assertIn(
        "Test-Mac14-Clang-MacMini8.1-CPU-AVX2-x86_64-Debug-All-NativeFonts_Fontations",
        agent_action_line,
    )

  def test_http_get_authenticates_gerrit_requests_via_luci_token(self):
    seen_reqs = []

    class FakeResp:
      def __enter__(self):
        return self

      def __exit__(self, *args):
        return False

      def read(self):
        return b")]}'\n{}"

    def fake_urlopen(req, timeout=20):
      seen_reqs.append(req)
      return FakeResp()

    fake_proc = type("Proc", (), {"stdout": "username=git\npassword=tok123\n"})()
    canary_advisor._LUCI_TOKEN_CACHE = ("", 0.0)
    with patch("subprocess.run", return_value=fake_proc):
      with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        body = canary_advisor.http_get(
            "https://skia-review.googlesource.com/changes/1412380"
        )
    self.assertEqual(body, ")]}'\n{}")
    self.assertEqual(
        seen_reqs[0].full_url,
        "https://skia-review.googlesource.com/a/changes/1412380",
    )
    self.assertEqual(seen_reqs[0].get_header("Authorization"), "Bearer tok123")

  def test_cmd_watch_survives_transient_http_429_and_skips_poll_diff(self):
    import urllib.error

    initial_meta = {
        "cl": 1412380,
        "patchset": 3,
        "current_patchset": 3,
        "subject": "Keep glyph offsets only if needed",
        "owner": "googler@google.com",
        "diff": "diff --git a/src/foo.cpp b/src/foo.cpp\n+// x\n",
        "googler_cr_plus_one": True,
    }
    err_429 = urllib.error.HTTPError(
        "https://skia-review.googlesource.com/changes/1412380",
        429,
        "Too Many Requests",
        {},
        None,
    )
    calls = []

    def fake_load_cl(cl, ps=None, include_diff=True):
      calls.append((cl, ps, include_diff))
      if len(calls) == 1:
        return initial_meta
      if len(calls) == 2:
        raise err_429
      return {**initial_meta, "diff": ""}

    args = type(
        "Args",
        (),
        {
            "cl": "1412380",
            "ps": None,
            "poll_sec": 1,
            "timeout_min": 5,
            "save_draft": False,
            "json": False,
        },
    )()
    fake_builds = {"Canary-G3": {"status": "SUCCESS"}}
    buf = io.StringIO()
    with patch.object(canary_advisor, "load_cl", side_effect=fake_load_cl):
      with patch.object(canary_advisor, "bb_cl_builds", return_value=fake_builds):
        with patch.object(canary_advisor, "swarming_tasks_for_cl", return_value=[]):
          with patch.object(canary_advisor, "resolve_canary_g3_holds", return_value=[]):
            with patch.object(canary_advisor, "cmd_triage", return_value={}):
              with patch("time.sleep"):
                with patch("sys.stdout", buf):
                  rc = canary_advisor.cmd_watch(args)
    self.assertEqual(rc, 0)
    self.assertEqual(
        calls,
        [
            ("1412380", None, True),
            ("1412380", None, False),
            ("1412380", None, False),
        ],
    )
    self.assertIn("ALL_TRYJOBS_COMPLETE", buf.getvalue())

  def test_find_unresolved_canary_g3_hold_leaves_safety_filters(self):
    hold_text = (
        "Thanks, looks good.\n\n"
        "The change looks good to me (Code-Review+1). I leave this comment unresolved on purpose:\n"
        "it blocks the submit until the Canary-G3 try job passes. Skia starts it only after a\n"
        "Googler votes Code-Review+1, so I vote first and hold\nthe submit with this comment."
    )
    comments_by_path = {
        "/PATCHSET_LEVEL": [
            # Valid own unresolved hold comment with line-wrapped marker
            {
                "id": "valid_hold",
                "patch_set": 2,
                "unresolved": True,
                "updated": "2026-11-01 01:00:00.000000000",
                "author": {"email": "jlavrova@google.com"},
                "message": hold_text,
            },
            # Already resolved hold thread
            {
                "id": "resolved_root",
                "patch_set": 2,
                "unresolved": True,
                "updated": "2026-11-01 01:05:00.000000000",
                "author": {"email": "jlavrova@google.com"},
                "message": hold_text,
            },
            {
                "id": "resolved_reply",
                "patch_set": 2,
                "in_reply_to": "resolved_root",
                "unresolved": False,
                "updated": "2026-11-01 02:00:00.000000000",
                "author": {"email": "jlavrova@google.com"},
                "message": "Done",
            },
            # Other Googler's hold thread must never be touched
            {
                "id": "other_googler_hold",
                "patch_set": 2,
                "unresolved": True,
                "updated": "2026-11-01 01:10:00.000000000",
                "author": {"email": "colleague@google.com"},
                "message": hold_text,
            },
            # Author replied with an unanswered question on the hold thread
            {
                "id": "hold_with_human_reply_root",
                "patch_set": 2,
                "unresolved": True,
                "updated": "2026-11-01 01:15:00.000000000",
                "author": {"email": "jlavrova@google.com"},
                "message": hold_text,
            },
            {
                "id": "hold_with_human_reply_leaf",
                "patch_set": 2,
                "in_reply_to": "hold_with_human_reply_root",
                "unresolved": True,
                "updated": "2026-11-01 02:15:00.000000000",
                "author": {"email": "author@external.org"},
                "message": "Should I also guard the fallback path?",
            },
            # Substantive /PATCHSET_LEVEL review request from jlavrova without hold marker
            {
                "id": "substantive_ps_comment",
                "patch_set": 2,
                "unresolved": True,
                "updated": "2026-11-01 01:20:00.000000000",
                "author": {"email": "jlavrova@google.com"},
                "message": "Could you narrow the contract in the commit message before Canary-G3?",
            },
            # Negated phrase must not match
            {
                "id": "negated_hold",
                "patch_set": 2,
                "unresolved": True,
                "updated": "2026-11-01 01:25:00.000000000",
                "author": {"email": "jlavrova@google.com"},
                "message": "Please do not hold the submit for Canary-G3; fix the test first.",
            },
        ],
        # Inline file comment must never be auto-resolved even if it repeats the hold phrase
        "modules/skparagraph/src/Run.h": [
            {
                "id": "inline_comment",
                "patch_set": 2,
                "line": 42,
                "unresolved": True,
                "updated": "2026-11-01 01:30:00.000000000",
                "author": {"email": "jlavrova@google.com"},
                "message": hold_text,
            }
        ],
    }
    leaves = canary_advisor.find_unresolved_canary_g3_hold_leaves(comments_by_path)
    self.assertEqual(
        leaves,
        [{"id": "valid_hold", "patch_set": 2, "path": "/PATCHSET_LEVEL"}],
    )

  def test_resolve_canary_g3_holds_cross_ps_and_stack_guards(self):
    hold_msg = (
        "The change looks good to me (Code-Review+1). I leave this comment unresolved on purpose: "
        "so I vote first and hold the submit with this comment until Canary-G3 passes."
    )
    related = {
        "changes": [
            # Abandoned companion descendant must be ignored
            {
                "_change_number": 1411683,
                "_revision_number": 1,
                "status": "ABANDONED",
                "commit": {"commit": "comp1", "parents": [{"commit": "c84"}]},
            },
            {
                "_change_number": 1412384,
                "_revision_number": 3,
                "status": "NEW",
                "commit": {"commit": "c84", "parents": [{"commit": "c83"}]},
            },
            {
                "_change_number": 1412383,
                "_revision_number": 3,
                "status": "NEW",
                "commit": {"commit": "c83", "parents": [{"commit": "c82"}]},
            },
            {
                "_change_number": 1412382,
                "_revision_number": 3,
                "status": "NEW",
                "commit": {"commit": "c82", "parents": [{"commit": "c81"}]},
            },
            {
                "_change_number": 1412381,
                "_revision_number": 3,
                "status": "NEW",
                "commit": {"commit": "c81", "parents": [{"commit": "c80"}]},
            },
            {
                "_change_number": 1412380,
                "_revision_number": 3,
                "status": "NEW",
                "commit": {"commit": "c80", "parents": [{"commit": "base_merged"}]},
            },
        ]
    }
    cl_details = {
        # 1412380: cur_ps == 3 (tested), own Canary-G3 == SUCCESS, PS2 hold unresolved -> resolve
        1412380: (3, [{"email": "jlavrova@google.com", "value": 1}]),
        # 1412381: cur_ps == 3 (tested), own Canary-G3 == STARTED, PS2 hold unresolved -> resolve
        1412381: (3, [{"email": "jlavrova@google.com", "value": 1}]),
        # 1412382: moved to untested PS4 -> MUST skip
        1412382: (4, [{"email": "jlavrova@google.com", "value": 1}]),
        # 1412383: cur_ps == 3, but jlavrova CR+1 was reset -> MUST skip
        1412383: (3, [{"email": "colleague@google.com", "value": 1}]),
        # 1412384: top CL, cur_ps == 3, Canary-G3 == SUCCESS, PS2 hold unresolved -> resolve
        1412384: (3, [{"email": "jlavrova@google.com", "value": 1}]),
    }

    def fake_gerrit_json(path):
      if path == "/changes/1412384/revisions/3/related":
        return related
      for c, (cur_ps, votes) in cl_details.items():
        if path.startswith(f"/changes/{c}?"):
          return {
              "status": "NEW",
              "current_revision": f"sha_{c}_{cur_ps}",
              "revisions": {f"sha_{c}_{cur_ps}": {"_number": cur_ps}},
              "labels": {"Code-Review": {"all": votes}},
          }
        if path == f"/changes/{c}/comments":
          return {
              "/PATCHSET_LEVEL": [{
                  "id": f"hold_{c}_ps2",
                  "patch_set": 2,
                  "unresolved": True,
                  "updated": "2026-11-01 02:00:00.000000000",
                  "author": {"email": "jlavrova@google.com"},
                  "message": hold_msg,
              }]
          }
      raise AssertionError(f"unexpected gerrit_json path: {path}")

    def fake_bb_cl_builds(c, ps):
      if int(c) in (1412380, 1412384):
        return {"Canary-G3": {"status": "SUCCESS"}}
      return {"Canary-G3": {"status": "STARTED"}}

    posted = []

    class FakeResp:
      status = 200

      def __enter__(self):
        return self

      def __exit__(self, *args):
        return False

      def read(self):
        return b")]}'\n{}"

    def fake_urlopen(req, timeout=20):
      posted.append((req.full_url, req.get_method(), json.loads(req.data.decode("utf-8"))))
      return FakeResp()

    with patch.object(canary_advisor, "gerrit_json", side_effect=fake_gerrit_json):
      with patch.object(canary_advisor, "bb_cl_builds", side_effect=fake_bb_cl_builds):
        with patch.object(canary_advisor, "_gerrit_luci_token", return_value="tok"):
          with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = canary_advisor.resolve_canary_g3_holds(1412384, 3)

    resolved_cls = [r["cl"] for r in res if r.get("resolved")]
    self.assertEqual(resolved_cls, [1412380, 1412381, 1412384])

    # Every reply MUST target revisions/2/review (the PS2 hold comment's patchset), never PS3
    self.assertEqual(
        [u for u, _, _ in posted],
        [
            "https://skia-review.googlesource.com/a/changes/1412380/revisions/2/review",
            "https://skia-review.googlesource.com/a/changes/1412381/revisions/2/review",
            "https://skia-review.googlesource.com/a/changes/1412384/revisions/2/review",
        ],
    )
    for _, method, body in posted:
      self.assertEqual(method, "POST")
      self.assertEqual(body["drafts"], "KEEP")
      self.assertTrue(body["omit_duplicate_comments"])
      self.assertNotIn("labels", body)
      c_in = body["comments"]["/PATCHSET_LEVEL"][0]
      self.assertFalse(c_in["unresolved"])
      self.assertEqual(c_in["patch_set"], 2)

    # 1412380 & 1412384 had direct Canary-G3 SUCCESS; 1412381 cites stack tip 1412384/3
    self.assertEqual(
        posted[0][2]["comments"]["/PATCHSET_LEVEL"][0]["message"],
        "Canary-G3 passed; resolving submit hold.",
    )
    self.assertIn(
        "https://skia-review.googlesource.com/c/skia/+/1412384/3",
        posted[1][2]["comments"]["/PATCHSET_LEVEL"][0]["message"],
    )

    skipped = {r["cl"]: r["skip_reason"] for r in res if not r.get("resolved")}
    self.assertEqual(skipped[1412382], "current_ps_4_differs_from_tested_ps_3")
    self.assertEqual(skipped[1412383], "missing_own_cr_plus_one")

  def test_resolve_canary_g3_holds_skips_ancestor_whose_own_g3_failed_tap(self):
    related = {
        "changes": [
            {
                "_change_number": 2002,
                "_revision_number": 1,
                "status": "NEW",
                "commit": {"commit": "c2", "parents": [{"commit": "c1"}]},
            },
            {
                "_change_number": 2001,
                "_revision_number": 1,
                "status": "NEW",
                "commit": {"commit": "c1", "parents": [{"commit": "base"}]},
            },
        ]
    }
    hold_msg = (
        "The change looks good to me (Code-Review+1). I vote first and hold the submit "
        "until Canary-G3 is green."
    )

    def fake_gerrit_json(path):
      if path == "/changes/2002/revisions/1/related":
        return related
      for c in (2001, 2002):
        if path.startswith(f"/changes/{c}?"):
          return {
              "status": "NEW",
              "current_revision": f"sha_{c}",
              "revisions": {f"sha_{c}": {"_number": 1}},
              "labels": {"Code-Review": {"all": [{"email": "jlavrova@google.com", "value": 1}]}},
          }
        if path == f"/changes/{c}/comments":
          return {
              "/PATCHSET_LEVEL": [{
                  "id": f"h_{c}",
                  "patch_set": 1,
                  "unresolved": True,
                  "updated": "2026-11-01 02:00:00.000000000",
                  "author": {"email": "jlavrova@google.com"},
                  "message": hold_msg,
              }]
          }
      raise AssertionError(path)

    def fake_bb_cl_builds(c, ps):
      if int(c) == 2002:
        return {"Canary-G3": {"status": "SUCCESS"}}
      return {"Canary-G3": {"status": "FAILURE"}}

    with patch.object(canary_advisor, "gerrit_json", side_effect=fake_gerrit_json):
      with patch.object(canary_advisor, "bb_cl_builds", side_effect=fake_bb_cl_builds):
        with patch.object(canary_advisor, "swarming_tasks_for_cl", return_value=[{"taskId": "t1"}]):
          # Real google3 roll CL failed TAP -> must NOT resolve ancestor hold
          with patch.object(
              canary_advisor,
              "swarming_task_stdout",
              return_value="Canary roll [ http://cl/999999 ] has status failed",
          ):
            res = canary_advisor.resolve_canary_g3_holds(2002, 1, dry_run=True)
          by_cl = {r["cl"]: r for r in res}
          self.assertEqual(by_cl[2001]["skip_reason"], "ancestor_own_canary_g3_failed")
          self.assertTrue(by_cl[2002]["would_resolve"])

          # Pre-roll 900s queue timeout (no roll CL created) -> superseded by stack tip SUCCESS
          with patch.object(
              canary_advisor,
              "swarming_task_stdout",
              return_value="Waiting for Canary roll to start\nProcess timed out after 900 seconds",
          ):
            res_q = canary_advisor.resolve_canary_g3_holds(2002, 1, dry_run=True)
          by_cl_q = {r["cl"]: r for r in res_q}
          self.assertTrue(by_cl_q[2001]["would_resolve"])

  def test_resolve_canary_g3_holds_refuses_when_top_canary_g3_not_success(self):
    with patch.object(
        canary_advisor, "bb_cl_builds", return_value={"Canary-G3": {"status": "FAILURE"}}
    ):
      with patch.object(
          canary_advisor, "gerrit_json", side_effect=AssertionError("unexpected Gerrit call")
      ):
        with patch.object(
            canary_advisor,
            "post_gerrit_hold_resolution",
            side_effect=AssertionError("unexpected Gerrit POST"),
        ):
          res = canary_advisor.resolve_canary_g3_holds(999999, 1)
    self.assertEqual(res, [])

  def test_cmd_watch_honors_no_resolve_hold_and_asan_failure(self):
    meta = {
        "cl": 1412384,
        "patchset": 3,
        "current_patchset": 3,
        "subject": "Derive first-line spacing offset",
        "owner": "external@author.com",
        "diff": "diff --git a/modules/skparagraph/src/Run.cpp b/modules/skparagraph/src/Run.cpp\n",
        "googler_cr_plus_one": True,
    }
    args_opt_out = type(
        "Args",
        (),
        {
            "cl": "1412384",
            "ps": "3",
            "poll_sec": 1,
            "timeout_min": 5,
            "save_draft": False,
            "no_resolve_hold": True,
            "json": False,
        },
    )()
    with patch.object(canary_advisor, "load_cl", return_value=meta):
      with patch.object(
          canary_advisor, "bb_cl_builds", return_value={"Canary-G3": {"status": "SUCCESS"}}
      ):
        with patch.object(canary_advisor, "swarming_tasks_for_cl", return_value=[]):
          with patch.object(canary_advisor, "resolve_canary_g3_holds") as mock_resolve:
            with patch.object(canary_advisor, "cmd_triage", return_value={}):
              with patch("sys.stdout", io.StringIO()):
                canary_advisor.cmd_watch(args_opt_out)
    mock_resolve.assert_not_called()
    self.assertFalse(args_opt_out.resolve_hold)

  def test_main_triage_returns_zero_exit_code_not_dict(self):
    with patch.object(sys, "argv", ["canary_advisor.py", "triage", "--cl", "1412384", "--ps", "3"]):
      with patch.object(canary_advisor, "cmd_triage", return_value={"cl": 1412384}):
        self.assertEqual(canary_advisor.main(), 0)

  def test_g3_change_approved_requests_detailed_labels_and_handles_str_args(self):
    seen_paths = []

    def fake_gerrit_json(path):
      seen_paths.append(path)
      has_detailed = "DETAILED_LABELS" in path
      if "/changes/1409696/" in path:
        cr = (
            {"all": [{"value": 1, "email": "jlavrova@google.com"}]}
            if has_detailed
            else {"approved": {"_account_id": 8956}}
        )
        return {
            "owner": {"email": "wselna123@gmail.com"},
            "revisions": {},
            "labels": {"Code-Review": cr},
        }
      if "/changes/1409999/" in path:
        return {
            "owner": {"email": "googler@google.com"},
            "revisions": {
                "sha4": {
                    "ref": "refs/changes/99/1409999/4",
                    "commit": {"author": {"email": "googler@google.com"}},
                }
            },
            "labels": {},
        }
      raise AssertionError(f"unexpected path: {path}")

    with patch.object(canary_advisor, "gerrit_json", side_effect=fake_gerrit_json):
      self.assertTrue(canary_advisor.g3_change_approved("1409696", "4"))
      self.assertTrue(canary_advisor.g3_change_approved("1409999", "4"))

    self.assertTrue(all("o=DETAILED_LABELS" in p for p in seen_paths))
    self.assertTrue(all("o=DETAILED_ACCOUNTS" in p for p in seen_paths))

  def test_g3_chain_single_cl_no_related_and_triage_missing_approval(self):
    missing_stdout = (
        "Failed to run g3 try: changes must be either owned and authored by Googlers "
        "or approved (CR+1) by a Googler"
    )
    sw_tasks = [{"name": "Canary-G3", "taskId": "task_1409696"}]

    # Case 1: single external CL with empty related changes and no Googler CR+1 on PS4
    def fake_unapproved_gerrit(path):
      if path == "/changes/1409696/revisions/4/related":
        return {"changes": []}
      if path.startswith("/changes/1409696/detail?"):
        self.assertIn("o=DETAILED_LABELS", path)
        return {
            "owner": {"email": "wselna123@gmail.com"},
            "revisions": {
                "sha4": {
                    "_number": 4,
                    "ref": "refs/changes/96/1409696/4",
                    "commit": {"author": {"email": "wselna123@gmail.com"}},
                }
            },
            "labels": {
                "Code-Review": {
                    "all": [
                        {"value": 0, "email": "jlavrova@google.com"},
                        {"value": 0, "email": "alexisdavidc@google.com"},
                    ]
                }
            },
        }
      raise AssertionError(f"unexpected path: {path}")

    with patch.object(canary_advisor, "gerrit_json", side_effect=fake_unapproved_gerrit):
      self.assertEqual(canary_advisor.g3_chain("1409696", "4"), [(1409696, 4)])
      self.assertEqual(canary_advisor.unapproved_g3_chain("1409696", "4"), [(1409696, 4)])
      with patch.object(canary_advisor, "swarming_task_stdout", return_value=missing_stdout):
        res = canary_advisor.triage_canary_g3_failure(
            "1409696", "4", {"status": "FAILURE"}, sw_tasks, []
        )
    self.assertEqual(res["verdict"], "MISSING_APPROVAL")
    self.assertEqual(res["missing"], [(1409696, 4)])
    self.assertFalse(res.get("now_approved", False))
    self.assertIn("missing on: 1409696/4", res["summary"])

    # Case 2: Canary-G3 failed before CR+1, then Googler CR+1 was added before triage ran
    def fake_now_approved_gerrit(path):
      if path == "/changes/1409696/revisions/4/related":
        return {"changes": []}
      if path.startswith("/changes/1409696/detail?"):
        self.assertIn("o=DETAILED_LABELS", path)
        return {
            "owner": {"email": "wselna123@gmail.com"},
            "revisions": {},
            "labels": {
                "Code-Review": {"all": [{"value": 1, "email": "jlavrova@google.com"}]}
            },
        }
      raise AssertionError(f"unexpected path: {path}")

    with patch.object(canary_advisor, "gerrit_json", side_effect=fake_now_approved_gerrit):
      with patch.object(canary_advisor, "swarming_task_stdout", return_value=missing_stdout):
        res_now = canary_advisor.triage_canary_g3_failure(
            1409696, 4, {"status": "FAILURE"}, sw_tasks, []
        )
    self.assertEqual(res_now["verdict"], "MISSING_APPROVAL")
    self.assertEqual(res_now["missing"], [])
    self.assertTrue(res_now["now_approved"])
    self.assertNotIn("none found", res_now["summary"])
    self.assertIn("git cl try -i 1409696", res_now["summary"])

  def test_triage_reads_build_summary_markdown_not_mergeable_before_stale_swarming_task(self):
    # Real fixture from CL 1409696 PS 4 build 8666232682941943121: an earlier Oct 30 Canary-G3
    # Swarming task failed with MissingApproval, then after Googler CR+1 a Nov 2 Canary-G3 build
    # failed in 8.8s with INFRA_FAILURE and summaryMarkdown "CL is not mergeable" (no new task)
    bb_g3_not_mergeable = {
        "id": "8666232682941943121",
        "builder": {"project": "skia", "bucket": "skia.primary", "builder": "Canary-G3"},
        "createTime": "2026-11-02T15:31:32.485687809Z",
        "startTime": "2026-11-02T15:31:35.267904411Z",
        "endTime": "2026-11-02T15:31:44.024725222Z",
        "status": "INFRA_FAILURE",
        "summaryMarkdown": "Failed to start Job: CL is not mergeable (issue 1409696 patchset 4)",
    }
    stale_sw_tasks = [{
        "taskId": "7ba5f448af76d610",
        "name": "Canary-G3",
        "createdTs": "2026-10-30T14:07:07.439462Z",
        "completedTs": "2026-10-30T14:07:45.343393Z",
        "state": "COMPLETED",
    }]
    stale_missing_stdout = (
        "Failed to run g3 try: changes must be either owned and authored by Googlers "
        "or approved (CR+1) by a Googler"
    )
    meta = {
        "cl": 1409696,
        "patchset": 4,
        "subject": "[skparagraph] Add Paragraph::paintLines for painting a range of lines",
        "owner": "wselna123@gmail.com",
        "diff": (
            "diff --git a/modules/skparagraph/tests/SkParagraphTest.cpp "
            "b/modules/skparagraph/tests/SkParagraphTest.cpp\n+// test\n"
        ),
        "googler_cr_plus_one": True,
    }
    args = type("Args", (), {"cl": "1409696", "ps": "4", "save_draft": False, "json": False})()
    buf = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=meta):
      with patch.object(
          canary_advisor, "bb_cl_builds", return_value={"Canary-G3": bb_g3_not_mergeable}
      ):
        with patch.object(canary_advisor, "swarming_tasks_for_cl", return_value=stale_sw_tasks):
          with patch.object(
              canary_advisor, "swarming_task_stdout", return_value=stale_missing_stdout
          ):
            with patch.object(canary_advisor, "unapproved_g3_chain", return_value=[]):
              with patch("sys.stdout", buf):
                triage_out = canary_advisor.cmd_triage(args)

    g3_res = triage_out["builds"]["Canary-G3"]
    self.assertEqual(g3_res["verdict"], "NOT_MERGEABLE")
    self.assertFalse(g3_res["our_part_passed"])
    self.assertIn("CL is not mergeable", g3_res["summary"])
    self.assertIn("rebase", g3_res["summary"])
    rendered = buf.getvalue()
    self.assertIn("- Verdict: NOT_MERGEABLE", rendered)
    self.assertIn("- Summary: Failed to start Job: CL is not mergeable", rendered)
    self.assertNotIn("Roll CL: http://cl/", rendered)
    self.assertNotIn("chain is now approved", rendered)

    # Also verify snake_case summary_markdown and generic pre-task failure with stale task
    res_snake = canary_advisor.triage_canary_g3_failure(
        1409696,
        4,
        {"status": "INFRA_FAILURE", "summary_markdown": "Failed to start Job: CL is not mergeable"},
        stale_sw_tasks,
        [],
    )
    self.assertEqual(res_snake["verdict"], "NOT_MERGEABLE")

    bb_other_startup_err = {
        "id": "8666232682941943999",
        "createTime": "2026-11-02T15:31:32.485687809Z",
        "status": "INFRA_FAILURE",
        "summaryMarkdown": "Failed to start Job: git fetch timeout",
    }
    with patch.object(canary_advisor, "swarming_tasks_for_cl", return_value=stale_sw_tasks):
      res_stale = canary_advisor.triage_canary_g3_failure(
          1409696, 4, bb_other_startup_err, stale_sw_tasks, []
      )
    self.assertEqual(res_stale["verdict"], "INFRA_NO_TASK")
    self.assertIn("git fetch timeout", res_stale["summary"])


if __name__ == "__main__":
  unittest.main()
