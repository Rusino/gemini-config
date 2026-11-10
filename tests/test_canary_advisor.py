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
        "diff --git a/modules/skshaper/src/SkShaper.cpp"
        " b/modules/skshaper/src/SkShaper.cpp\n"
        "--- a/modules/skshaper/src/SkShaper.cpp\n"
        "+++ b/modules/skshaper/src/SkShaper.cpp\n"
        "@@ -1,2 +1,3 @@\n"
        "+// some change\n"
    )
    fake_meta = {
        "cl": 1408157,
        "patchset": 1,
        "subject": "SkShaper change",
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
      with patch.object(canary_advisor, "has_cloudtop_citc", return_value=True):
        with patch("sys.stdout", buf):
          canary_advisor.advise(args)

    out = buf.getvalue()
    self.assertIn("AGENT ACTION (execute this command):", out)
    self.assertIn("Canary-Flutter is disabled", out)
    self.assertIn("NativeFonts_FreeType_ASAN_Upload", out)
    # Canary-G3 must NOT be in the immediate AGENT ACTION
    agent_action_line = [
        line
        for line in out.splitlines()
        if "canary_advisor.py try --cl" in line
    ][0]
    self.assertNotIn("Canary-G3", agent_action_line)
    self.assertNotIn("Canary-Flutter", agent_action_line)
    self.assertIn("Canary-G3 requires a Googler author or Googler Code-Review+1", out)
    self.assertIn("Canary-G3 Command (run after Googler Code-Review+1 approval):", out)
    self.assertIn(
        "luci-auth context -- git cl try -i 1408157 -B skia/skia.primary -b Canary-G3",
        out,
    )
    self.assertNotIn("submit-hold", out)

  def test_external_author_includes_canary_g3_when_googler_cr_plus_one_granted(self):
    diff_text = (
        "diff --git a/modules/skshaper/src/SkShaper.cpp"
        " b/modules/skshaper/src/SkShaper.cpp\n"
        "--- a/modules/skshaper/src/SkShaper.cpp\n"
        "+++ b/modules/skshaper/src/SkShaper.cpp\n"
        "@@ -1,2 +1,3 @@\n"
        "+// some change\n"
    )
    fake_meta = {
        "cl": 1408157,
        "patchset": 2,
        "subject": "SkShaper change",
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
      with patch.object(canary_advisor, "has_cloudtop_citc", return_value=True):
        with patch("sys.stdout", buf):
          canary_advisor.advise(args)

    out = buf.getvalue()
    agent_action_line = [
        line
        for line in out.splitlines()
        if "canary_advisor.py try --cl" in line
    ][0]
    self.assertIn("Canary-G3", agent_action_line)
    self.assertNotIn("Canary-Flutter", agent_action_line)
    self.assertIn("run_flutter_paragraph_tests.py --full", out)
    self.assertIn("NativeFonts_FreeType_ASAN_Upload", agent_action_line)
    self.assertNotIn("deferred until a Googler votes Code-Review+1", out)

  def test_skparagraph_routes_to_local_google3_corpus_on_cloudtop_and_falls_back_off_cloudtop(self):
    para_only_diff = (
        "diff --git a/modules/skparagraph/src/ParagraphImpl.cpp"
        " b/modules/skparagraph/src/ParagraphImpl.cpp\n"
        "--- a/modules/skparagraph/src/ParagraphImpl.cpp\n"
        "+++ b/modules/skparagraph/src/ParagraphImpl.cpp\n"
        "@@ -1,2 +1,3 @@\n"
        "+// paragraph change\n"
    )
    fake_meta = {
        "cl": 1409697,
        "patchset": 5,
        "subject": "SkParagraph change",
        "owner": "external@author.com",
        "diff": para_only_diff,
        "googler_cr_plus_one": False,
    }
    args = type(
        "Args",
        (),
        {
            "cl": "1409697",
            "repo": None,
            "base": "origin/main",
            "max_symbols": 6,
            "no_search": True,
            "no_health": True,
            "no_log": True,
            "strong_only": False,
        },
    )()

    # On Cloudtop: Canary-G3 is replaced by LOCAL google3-test-paragraph with zero deferral/hold
    buf_cloud = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=fake_meta):
      with patch.object(canary_advisor, "has_cloudtop_citc", return_value=True):
        with patch("sys.stdout", buf_cloud):
          canary_advisor.advise(args)
    out_cloud = buf_cloud.getvalue()
    self.assertIn("## LOCAL google3-test-paragraph [STRONG]", out_cloud)
    self.assertNotIn("## Canary-G3", out_cloud)
    self.assertIn("run_g3_paragraph_tests.py", out_cloud)
    self.assertIn("cand=cl:1409697/5", out_cloud)
    self.assertNotIn("Canary-G3 requires a Googler author", out_cloud)

    # Off Cloudtop (e.g. macOS): falls back to Canary-G3
    buf_mac = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=fake_meta):
      with patch.object(canary_advisor, "has_cloudtop_citc", return_value=False):
        with patch("sys.stdout", buf_mac):
          canary_advisor.advise(args)
    out_mac = buf_mac.getvalue()
    self.assertIn("## Canary-G3 [STRONG]", out_mac)
    self.assertNotIn("LOCAL google3-test-paragraph", out_mac)

    # Mixed diff (skparagraph STRONG + core src/ CONSIDER): keeps Canary-G3 as CONSIDER only
    mixed_diff = para_only_diff + (
        "diff --git a/src/core/SkPaint.cpp b/src/core/SkPaint.cpp\n"
        "--- a/src/core/SkPaint.cpp\n"
        "+++ b/src/core/SkPaint.cpp\n"
        "@@ -1,2 +1,3 @@\n"
        "+// core change\n"
    )
    buf_mix = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value={**fake_meta, "diff": mixed_diff}):
      with patch.object(canary_advisor, "has_cloudtop_citc", return_value=True):
        with patch("sys.stdout", buf_mix):
          canary_advisor.advise(args)
    out_mix = buf_mix.getvalue()
    self.assertIn("## LOCAL google3-test-paragraph [STRONG]", out_mix)
    self.assertIn("## Canary-G3 [CONSIDER]", out_mix)

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
        line
        for line in out.splitlines()
        if "canary_advisor.py try --cl" in line
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
    self.assertIn("luci-auth context -- git cl try -i 1409696", res_now["summary"])

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

  def test_http_get_invalidates_expired_luci_token_on_400_and_retries(self):
    # Gerrit /a/ returns HTTP 400/401 when a cached git-credential-luci Bearer token expires
    # mid-watch (as happened on CL 1414016); http_get must drop cache and retry cleanly
    canary_advisor._LUCI_TOKEN_CACHE = ("expired-tok", 9999999999.0)
    seen_urls = []

    class _FakeResp:

      def __init__(self, text):
        self._text = text

      def read(self):
        return self._text.encode("utf-8")

      def __enter__(self):
        return self

      def __exit__(self, exc_type, exc, tb):
        return False

    def fake_urlopen(req, timeout=20):
      seen_urls.append((req.full_url, req.headers.get("Authorization")))
      if req.headers.get("Authorization") == "Bearer expired-tok":
        raise canary_advisor.urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)
      return _FakeResp(")]}'\n{\"status\":\"NEW\"}")

    with patch.object(
        canary_advisor.subprocess,
        "run",
        return_value=type("P", (), {"stdout": "password=fresh-tok\n"})(),
    ):
      with patch.object(canary_advisor.urllib.request, "urlopen", side_effect=fake_urlopen):
        body = canary_advisor.http_get(f"{canary_advisor.GERRIT}/changes/1414016/detail")

    self.assertIn('"status":"NEW"', body)
    self.assertEqual(len(seen_urls), 2)
    self.assertEqual(seen_urls[0][1], "Bearer expired-tok")
    self.assertEqual(seen_urls[1][1], "Bearer fresh-tok")

  def test_not_mergeable_verdict_detects_gclient_sync_patch_ref_conflict(self):
    # Real fixture from CL 1412381 PS 3: stacked parent CLs landed on main, so Task Scheduler's
    # gclient.py sync --patch-ref failed cherry-picking ancestor commits before any Swarming task
    bb_sync_conflict = {
        "id": "8666280000000000001",
        "createTime": "2026-11-02T13:01:20.000000Z",
        "status": "INFRA_FAILURE",
        "summaryMarkdown": (
            "Failed to start Job: syncing to "
            "skia@cf49321de4047a459c44ab3de2b842c47f7f5868:refs/changes/81/1412381/3: "
            "Command exited with exit status 1: vpython3 -u "
            "/mnt/task-scheduler/depot_tools/gclient.py sync "
            "--revision skia@cf49321de4047a459c44ab3de2b842c47f7f5868 "
            "--patch-ref skia@cf49321de4047a459c44ab3de2b842c47f7f5868:refs/changes/81/1412381/3"
        ),
    }
    nm = canary_advisor._not_mergeable_verdict(bb_sync_conflict)
    self.assertIsNotNone(nm)
    self.assertEqual(nm["verdict"], "NOT_MERGEABLE")
    self.assertFalse(nm["our_part_passed"])
    self.assertIn("gclient.py sync", nm["summary"])
    self.assertIn("rebase", nm["summary"])

    asan_res = canary_advisor.triage_asan_failure(
        1412381, 3, "Test-Ubuntu24.04-Clang-ASAN", bb_sync_conflict, [], []
    )
    self.assertEqual(asan_res["verdict"], "NOT_MERGEABLE")
    g3_res = canary_advisor.triage_canary_g3_failure(
        1412381, 3, bb_sync_conflict, [], []
    )
    self.assertEqual(g3_res["verdict"], "NOT_MERGEABLE")

  def test_luci_token_ttl_and_authed_writes_refresh_expired_token_on_400(self):
    # Cached tokens older than 4 minutes must refresh before Google OAuth expires them
    canary_advisor._LUCI_TOKEN_CACHE = ("stale-5m-tok", canary_advisor.time.time() - 300)
    with patch.object(
        canary_advisor.subprocess,
        "run",
        return_value=type("P", (), {"stdout": "password=refreshed-tok\n"})(),
    ):
      self.assertEqual(canary_advisor._gerrit_luci_token(), "refreshed-tok")

    # save_gerrit_patchset_draft must retry on HTTP 400
    class _FakeResp:

      def __init__(self, status=200, text=")]}'\n{}"):
        self.status = status
        self._text = text

      def read(self):
        return self._text.encode("utf-8")

      def __enter__(self):
        return self

      def __exit__(self, exc_type, exc, tb):
        return False

    seen_draft_auth = []

    def fake_draft_urlopen(req, timeout=15):
      auth = req.headers.get("Authorization")
      seen_draft_auth.append((req.get_method(), auth))
      if auth == "Bearer expired-draft-tok":
        raise canary_advisor.urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {}, None
        )
      return _FakeResp(200, ")]}'\n{}")

    canary_advisor._LUCI_TOKEN_CACHE = ("expired-draft-tok", canary_advisor.time.time())
    with patch.object(
        canary_advisor.subprocess,
        "run",
        return_value=type("P", (), {"stdout": "password=fresh-draft-tok\n"})(),
    ):
      with patch.object(
          canary_advisor.urllib.request, "urlopen", side_effect=fake_draft_urlopen
      ):
        saved = canary_advisor.save_gerrit_patchset_draft(
            1414017, 1, "Canary-G3 failed on unrelated targets.", unresolved=False
        )
    self.assertTrue(saved)
    self.assertEqual(seen_draft_auth[0], ("GET", "Bearer expired-draft-tok"))
    self.assertEqual(seen_draft_auth[1], ("GET", "Bearer fresh-draft-tok"))
    self.assertEqual(seen_draft_auth[2], ("PUT", "Bearer fresh-draft-tok"))

  def test_cmd_watch_exits_cleanly_when_cl_abandoned(self):
    init_meta = {
        "cl": 1414017,
        "patchset": 1,
        "current_patchset": 1,
        "status": "NEW",
        "subject": "Test",
        "owner": "ext@gmail.com",
        "diff": "diff --git a/modules/skparagraph/src/Run.cpp b/modules/skparagraph/src/Run.cpp\n",
        "googler_cr_plus_one": False,
    }
    abandoned_meta = {**init_meta, "status": "ABANDONED"}
    args = type(
        "Args",
        (),
        {
            "cl": "1414017",
            "ps": "1",
            "poll_sec": 0,
            "timeout_min": 5,
            "save_draft": False,
            "json": False,
        },
    )()
    buf = io.StringIO()
    with patch.object(canary_advisor, "load_cl", side_effect=[init_meta, abandoned_meta]):
      with patch("sys.stdout", buf):
        rc = canary_advisor.cmd_watch(args)
    self.assertEqual(rc, 0)
    self.assertIn("WAKEUP_REASON: CL_ABANDONED", buf.getvalue())

  def test_cmd_watch_survives_transient_rpc_errors_in_poll_loop(self):
    meta = {
        "cl": 1414017,
        "patchset": 1,
        "current_patchset": 1,
        "status": "NEW",
        "subject": "Test",
        "owner": "ext@gmail.com",
        "diff": "diff --git a/modules/skparagraph/src/Run.cpp b/modules/skparagraph/src/Run.cpp\n",
        "googler_cr_plus_one": True,
    }
    args = type(
        "Args",
        (),
        {
            "cl": "1414017",
            "ps": "1",
            "poll_sec": 0,
            "timeout_min": 5,
            "save_draft": False,
            "json": False,
        },
    )()
    g3_ok = {"Canary-G3": {"id": "86662085", "status": "SUCCESS"}}
    with patch.object(canary_advisor, "load_cl", return_value=meta):
      with patch.object(
          canary_advisor,
          "bb_cl_builds",
          side_effect=[canary_advisor.urllib.error.URLError("transient 503"), g3_ok],
      ):
        with patch.object(
            canary_advisor,
            "swarming_tasks_for_cl",
            return_value=[],
        ):
          with patch.object(canary_advisor, "cmd_triage", return_value={}) as mock_triage:
            with patch.object(canary_advisor.time, "sleep"):
              with patch("sys.stdout", io.StringIO()):
                rc = canary_advisor.cmd_watch(args)
    self.assertEqual(rc, 0)
    mock_triage.assert_called_once()

  def test_cmd_watch_asan_retry_uses_luci_auth_and_checks_exit_code(self):
    meta = {
        "cl": 1409658,
        "patchset": 2,
        "current_patchset": 2,
        "status": "NEW",
        "subject": "Fix paragraph",
        "owner": "jlavrova@google.com",
        "diff": "diff --git a/modules/skparagraph/src/Run.cpp b/modules/skparagraph/src/Run.cpp\n",
        "googler_cr_plus_one": True,
    }
    args = type(
        "Args",
        (),
        {
            "cl": "1409658",
            "ps": "2",
            "poll_sec": 1,
            "timeout_min": 5,
            "save_draft": False,
            "json": False,
        },
    )()
    asan_bot = "Test-Ubuntu24.04-Clang-NUC11TZi5-CPU-AVX2-x86_64-Debug-All-ASAN"
    infra_builds = {asan_bot: {"id": "8001", "status": "INFRA_FAILURE"}}
    ok_builds = {asan_bot: {"id": "8002", "status": "SUCCESS"}}
    mishap_verdict = {"verdict": "INFRA_MISHAP", "our_part_passed": False, "summary": "bot died"}

    ok_proc = type("Proc", (), {"returncode": 0, "stdout": "Scheduled\n", "stderr": ""})()
    buf_ok = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=meta), patch.object(
        canary_advisor, "bb_cl_builds", side_effect=[infra_builds, ok_builds]
    ) as mock_bb, patch.object(
        canary_advisor, "swarming_tasks_for_cl", return_value=[]
    ), patch.object(
        canary_advisor, "triage_asan_failure", return_value=mishap_verdict
    ), patch.object(
        canary_advisor.subprocess, "run", return_value=ok_proc
    ) as mock_run, patch.object(
        canary_advisor, "cmd_triage", return_value={}
    ), patch.object(
        canary_advisor.time, "sleep"
    ), patch("sys.stdout", buf_ok):
      rc_ok = canary_advisor.cmd_watch(args)

    self.assertEqual(rc_ok, 0)
    self.assertEqual(mock_bb.call_count, 2)
    ran_cmd = mock_run.call_args[0][0]
    self.assertEqual(
        ran_cmd[:6],
        [canary_advisor.LUCI_AUTH, "context", "--", "git", "cl", "try"],
    )
    self.assertIn("ALL_TRYJOBS_COMPLETE", buf_ok.getvalue())

    fail_proc = type(
        "Proc", (), {"returncode": 1, "stdout": "", "stderr": "SSOAuthenticator: exit status 1\n"}
    )()
    buf_err = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=meta), patch.object(
        canary_advisor, "bb_cl_builds", return_value=infra_builds
    ), patch.object(
        canary_advisor, "swarming_tasks_for_cl", return_value=[]
    ), patch.object(
        canary_advisor, "triage_asan_failure", return_value=mishap_verdict
    ), patch.object(
        canary_advisor.subprocess, "run", return_value=fail_proc
    ), patch.object(
        canary_advisor, "cmd_triage", return_value={}
    ) as mock_err_triage, patch.object(
        canary_advisor.time, "sleep"
    ), patch("sys.stdout", buf_err):
      rc_err = canary_advisor.cmd_watch(args)

    self.assertEqual(rc_err, 1)
    mock_err_triage.assert_called_once()
    err_out = buf_err.getvalue()
    self.assertIn("ASAN_RETRY_FAILED", err_out)
    self.assertIn("SSOAuthenticator: exit status 1", err_out)
    self.assertNotIn("ALL_TRYJOBS_COMPLETE", err_out)

  def test_cmd_triage_reports_font_trybot_dm_failures_and_tryjobs_summary(self):
    meta = {
        "cl": 1400176,
        "patchset": 2,
        "current_patchset": 2,
        "status": "NEW",
        "subject": "Prefer matchFamilyStyle in FontCollection",
        "owner": "cwei97388@gmail.com",
        "diff": (
            "diff --git a/modules/skparagraph/src/FontCollection.cpp "
            "b/modules/skparagraph/src/FontCollection.cpp\n"
        ),
        "googler_cr_plus_one": False,
    }
    args = type("Args", (), {"cl": "1400176", "ps": "2", "save_draft": False, "json": False})()
    asan_bot = "Test-Ubuntu24.04-Clang-NUC11TZi5-CPU-AVX2-x86_64-Debug-All-ASAN"
    mac_bot = "Test-Mac14-Clang-MacMini8.1-CPU-AVX2-x86_64-Debug-All-NativeFonts_Fontations"
    dm_log = (
        "start unit test SkParagraph_FallbackCacheAcrossParagraphs\n"
        "Failures:\n"
        "\tmodules/skparagraph/tests/SkParagraphTest.cpp:12189\t"
        "otherFontMgr->fFamilyStyleMatches <= 1 [SkParagraph_FallbackCacheAcrossParagraphs]\n"
        "1 failure\n"
    )

    # Red Mac font trybot alongside green ASAN prints dm failure block and NOT GREEN
    buf_red = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=meta), patch.object(
        canary_advisor,
        "bb_cl_builds",
        return_value={
            asan_bot: {"id": "9001", "status": "SUCCESS"},
            mac_bot: {"id": "9002", "status": "FAILURE"},
        },
    ), patch.object(
        canary_advisor,
        "swarming_tasks_for_cl",
        return_value=[{"name": mac_bot, "taskId": "task9002"}],
    ), patch.object(
        canary_advisor, "swarming_task_result", return_value={"runId": "run9002"}
    ), patch.object(
        canary_advisor, "fetch_logdog_step", return_value=dm_log
    ), patch("sys.stdout", buf_red):
      res_red = canary_advisor.cmd_triage(args)

    out_red = buf_red.getvalue()
    self.assertEqual(res_red["builds"][mac_bot]["verdict"], "TEST_FAILURE")
    self.assertIn("SkParagraph_FallbackCacheAcrossParagraphs", out_red)
    self.assertIn(f"TRYJOBS: NOT GREEN ({mac_bot})", out_red)

    # Empty build map must not report ALL GREEN
    buf_empty = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=meta), patch.object(
        canary_advisor, "bb_cl_builds", return_value={}
    ), patch.object(canary_advisor, "swarming_tasks_for_cl", return_value=[]), patch(
        "sys.stdout", buf_empty
    ):
      canary_advisor.cmd_triage(args)
    self.assertIn("TRYJOBS: NOT GREEN (no tryjobs found)", buf_empty.getvalue())

    # Green ASAN + green Mac font trybot prints TRYJOBS: ALL GREEN
    buf_ok = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=meta), patch.object(
        canary_advisor,
        "bb_cl_builds",
        return_value={
            asan_bot: {"id": "9001", "status": "SUCCESS"},
            mac_bot: {"id": "9003", "status": "SUCCESS"},
        },
    ), patch.object(canary_advisor, "swarming_tasks_for_cl", return_value=[]), patch(
        "sys.stdout", buf_ok
    ):
      res_ok = canary_advisor.cmd_triage(args)
    self.assertIn("TRYJOBS: ALL GREEN", buf_ok.getvalue())
    self.assertNotIn("note", res_ok["builds"][mac_bot])

    # Green Android NativeFonts trybot on an SkParagraph CL warns that extra_fonts /
    # --paragraph_fonts extra_fonts are omitted (dm_flags.go:1813) and SKIP_IF_FONTS_NOT_FOUND
    # tests return early without running
    android_bot = (
        "Test-Android-Clang-GalaxyS24-CPU-Adreno750-arm64-Release-All-"
        "Android_NativeFonts_AndroidNDKFonts"
    )
    buf_android = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=meta), patch.object(
        canary_advisor,
        "bb_cl_builds",
        return_value={
            asan_bot: {"id": "9001", "status": "SUCCESS"},
            android_bot: {"id": "8666039676250874145", "status": "SUCCESS"},
        },
    ), patch.object(canary_advisor, "swarming_tasks_for_cl", return_value=[]), patch(
        "sys.stdout", buf_android
    ):
      res_android = canary_advisor.cmd_triage(args)
    out_android = buf_android.getvalue()
    self.assertEqual(res_android["builds"][android_bot]["verdict"], "SUCCESS")
    self.assertIn("note", res_android["builds"][android_bot])
    self.assertIn("--paragraph_fonts extra_fonts", res_android["builds"][android_bot]["note"])
    self.assertIn("SKIP_IF_FONTS_NOT_FOUND", res_android["builds"][android_bot]["note"])
    self.assertIn("- Note: Android NativeFonts bots do not mount extra_fonts", out_android)
    self.assertIn("dm_flags.go:1813", out_android)

    # Non-SkParagraph CLs do not emit the SkParagraph SKIP_IF_FONTS_NOT_FOUND note
    non_para_meta = {
        **meta,
        "diff": "diff --git a/src/core/SkPaint.cpp b/src/core/SkPaint.cpp\n+// x\n",
    }
    buf_non_para = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=non_para_meta), patch.object(
        canary_advisor,
        "bb_cl_builds",
        return_value={android_bot: {"id": "8666039676250874145", "status": "SUCCESS"}},
    ), patch.object(canary_advisor, "swarming_tasks_for_cl", return_value=[]), patch(
        "sys.stdout", buf_non_para
    ):
      res_non_para = canary_advisor.cmd_triage(args)
    self.assertNotIn("note", res_non_para["builds"][android_bot])
    self.assertNotIn("SKIP_IF_FONTS_NOT_FOUND", buf_non_para.getvalue())

  def test_has_cloudtop_citc_detects_local_mount_or_corp_mac_ssh(self):
    with patch("os.path.isdir", return_value=True):
      self.assertTrue(canary_advisor.has_cloudtop_citc())
    with patch("os.path.isdir", return_value=False), patch.dict(
        "os.environ", {"GOOGLE3_PARAGRAPH_SSH_HOST": "my-cloudtop.c.googlers.com"}, clear=False
    ):
      self.assertTrue(canary_advisor.has_cloudtop_citc())
    with patch("os.path.isdir", return_value=False), patch.dict(
        "os.environ", {"GOOGLE3_PARAGRAPH_SSH_HOST": ""}, clear=False
    ), patch("sys.platform", "darwin"), patch("os.path.exists", return_value=True):
      self.assertTrue(canary_advisor.has_cloudtop_citc())
    with patch("os.path.isdir", return_value=False), patch.dict(
        "os.environ", {"GOOGLE3_PARAGRAPH_SSH_HOST": ""}, clear=False
    ), patch("sys.platform", "darwin"), patch("os.path.exists", return_value=False):
      self.assertFalse(canary_advisor.has_cloudtop_citc())

  def test_code_patchset_skips_description_only_patchsets(self):
    meta = {"kinds": {1: "REWORK", 2: "NO_CODE_CHANGE", 3: "NO_CODE_CHANGE", 4: "TRIVIAL_REBASE"}}
    self.assertEqual(canary_advisor.code_patchset(meta, 3), 1)
    self.assertEqual(canary_advisor.code_patchset(meta, 4), 4)
    self.assertEqual(canary_advisor.code_patchset(meta, 1), 1)

  def test_tested_block_gaps_requires_tested_block_with_green_trybots(self):
    asan = "Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-ASAN"
    cases = [
        ("Fix.\n\nChange-Id: I1\n", ["no Tested: block"]),
        ("Fix.\n\nTest: dm --match Foo\n\nChange-Id: I1\n", [asan]),
        (f"Fix.\n\nTested:\n- dm on Linux\n- {asan}: green\n\nChange-Id: I1\n", []),
    ]
    for msg, want in cases:
      with self.subTest(msg=msg), patch.object(
          canary_advisor, "gerrit_json", return_value={"message": msg}
      ):
        self.assertEqual(canary_advisor.tested_block_gaps(1, 1, [asan]), want)

  # Checks R135 Canary-G3 triage on PascalCase skia_gardener output with settled Skia TAP checks
  # and fail-closed rejection when a TAP check fails without diagnosed targets
  def test_triage_canary_g3_verifies_pascal_case_runs_and_settled_tap_checks(self):
    sw_tasks = [{"name": "Canary-G3", "taskId": "task_g3_1421656"}]
    stdout = "Canary roll [ http://cl/1017394648 ] failed\n"
    gardener_ok = {
        "Verdict": "DEPENDENT_HEAD_UNKNOWN",
        "Runs": [
            {"id": "r1", "check": "CheckTestsSkia", "status": "PASSED", "failed": 0},
            {"id": "r2", "check": "CheckSkiaDependents", "status": "PASSED", "failed": 0},
            {"id": "r3", "check": "CheckTestsSkiaDependents", "status": "FAILED", "failed": 2},
            {"id": "", "check": "Presubmit", "status": "RUNNING", "failed": 0},
        ],
        "Targets": [
            {"target": "//cloud/ml/translate:handler_test", "kind": "INFRA",
             "failed_in_latest": True},
            {"target": "//video/youtube/scotty:handler_test", "kind": "UNKNOWN",
             "failed_in_latest": True},
        ],
    }
    ok_proc = type("P", (), {"returncode": 0, "stdout": json.dumps(gardener_ok), "stderr": ""})()
    with patch("os.path.isdir", return_value=True), patch.object(
        canary_advisor, "swarming_task_stdout", return_value=stdout
    ), patch.object(
        canary_advisor.subprocess, "run", return_value=ok_proc
    ), patch.object(
        canary_advisor, "check_google3_somepath", return_value=(False, [])
    ):
      res = canary_advisor.triage_canary_g3_failure(
          1421656, 3, {"status": "INFRA_FAILURE"}, sw_tasks, ["src/core/SkScalerContext.cpp"]
      )
    self.assertEqual(res["verdict"], "UNRELATED_HEAD_BREAKAGE")
    self.assertTrue(res["our_part_passed"])
    self.assertEqual(canary_advisor.find_public_draft_leak(res["sanitized_draft"]), "")
    self.assertIn("CheckTestsSkia", res["sanitized_draft"])

    # Undiagnosed FAILED CheckSkiaDependents (failed == 0, empty Targets) must fail closed
    gardener_undiagnosed = {
        "Verdict": "UNKNOWN",
        "Runs": [
            {"id": "r1", "check": "CheckTestsSkia", "status": "PASSED", "failed": 0},
            {"id": "r2", "check": "CheckSkiaDependents", "status": "FAILED", "failed": 0},
            {"id": "r3", "check": "CheckTestsSkiaDependents", "status": "PASSED", "failed": 0},
        ],
        "Targets": [],
    }
    bad_proc = type(
        "P", (), {"returncode": 0, "stdout": json.dumps(gardener_undiagnosed), "stderr": ""}
    )()
    with patch("os.path.isdir", return_value=True), patch.object(
        canary_advisor, "swarming_task_stdout", return_value=stdout
    ), patch.object(canary_advisor.subprocess, "run", return_value=bad_proc):
      bad_res = canary_advisor.triage_canary_g3_failure(
          1421656, 3, {"status": "FAILURE"}, sw_tasks, ["src/core/SkScalerContext.cpp"]
      )
    self.assertEqual(bad_res["verdict"], "CL_SUSPECTED_REGRESSION")
    self.assertFalse(bad_res["our_part_passed"])
    self.assertEqual(bad_res["sanitized_draft"], "")

  # Checks R135 Canary-Chromium roll verification: bot_update GoB lag after green CQ bots clears,
  # whereas a Skia compile error or unproven test failure stays CL_SUSPECTED_REGRESSION
  def test_triage_canary_chromium_proves_cq_roll_passed_and_fails_closed_on_skia_regression(self):
    sw_tasks = [{"name": "Canary-Chromium", "taskId": "task_cr_1"}]
    stdout = "Created roll https://chromium-review.googlesource.com/c/chromium/src/+/8774115\n"
    search_ok = {
        "builds": [
            {"id": "101", "status": "SUCCESS", "builder": {"builder": "linux-rel"}},
            {"id": "102", "status": "SUCCESS", "builder": {"builder": "mac-rel"}},
            {
                "id": "103",
                "status": "INFRA_FAILURE",
                "builder": {"builder": "ios-simulator"},
                "summaryMarkdown": "patch error log attached to bot_update",
            },
        ]
    }
    with patch.object(canary_advisor, "swarming_task_stdout", return_value=stdout), patch.object(
        canary_advisor, "_prpc_json_post", side_effect=[{"builds": []}, search_ok]
    ):
      res = canary_advisor.triage_canary_chromium_failure(
          1421656, 3, {"status": "FAILURE"}, sw_tasks, ["src/core/SkScalerContext.cpp"]
      )
    self.assertEqual(res["verdict"], "UNRELATED_HEAD_BREAKAGE")
    self.assertTrue(res["our_part_passed"])
    self.assertIn("https://crrev.com/c/8774115/2", res["sanitized_draft"])
    self.assertIn("passed on 2 Chromium CQ trybots", res["sanitized_draft"])
    self.assertEqual(canary_advisor.find_public_draft_leak(res["sanitized_draft"]), "")

    # Skia compile failure on a Chromium roll trybot must fail closed as CL_SUSPECTED_REGRESSION
    search_skia_err = {
        "builds": [
            {"id": "201", "status": "SUCCESS", "builder": {"builder": "ios-simulator"}},
            {
                "id": "202",
                "status": "FAILURE",
                "builder": {"builder": "linux-rel"},
                "summaryMarkdown": "step _compile failed:\n```\n../../third_party/skia/src/core/"
                "SkScalerContext.cpp:99: error: undeclared identifier\n```",
            },
        ]
    }
    detail_skia_err = {
        "status": "FAILURE",
        "summaryMarkdown": search_skia_err["builds"][1]["summaryMarkdown"],
        "steps": [{"name": "compile (with patch)", "status": "FAILURE"}],
    }
    with patch.object(canary_advisor, "swarming_task_stdout", return_value=stdout), patch.object(
        canary_advisor, "_prpc_json_post", side_effect=[search_skia_err, detail_skia_err]
    ):
      bad = canary_advisor.triage_canary_chromium_failure(
          1421656, 3, {"status": "FAILURE"}, sw_tasks, ["src/core/SkScalerContext.cpp"]
      )
    self.assertEqual(bad["verdict"], "CL_SUSPECTED_REGRESSION")
    self.assertFalse(bad["our_part_passed"])
    self.assertEqual(bad["sanitized_draft"], "")

  # Checks R135 Canary-Android verification across TreeHugger retry, non-Skia AnTS failures, and
  # HWUI/Skia stack failures while ensuring zero internal identifiers leak into drafts
  def test_triage_canary_android_proves_treehugger_or_ants_and_strips_internal_leaks(self):
    sw_tasks = [{"name": "Canary-Android", "taskId": "task_ag_1"}]
    stdout = (
        "Roll https://googleplex-android-review.googlesource.com/c/"
        "platform/external/skia/+/43653565 failed\n"
    )
    retry_change = {
        "messages": [
            {
                "tag": "autogenerated:TreeHugger:Verify-Fail",
                "message": (
                    "## Presubmit finished: 119 passed, 1 failed\n"
                    "FAIL: Tests failed for [pts-bot](https://android-build.googleplex.com/"
                    "builds/tests/view?invocationId=I8570000105396069325) on menari_lte. "
                    "Caused by UNKNOWN_INFRA_ERROR"
                ),
            },
            {
                "tag": "autogenerated:TreeHugger:Verify-Pass",
                "message": "Patch Set 1: Presubmit-Verified+1\n\n## Presubmit finished: 121 passed",
            },
        ]
    }
    with patch.object(canary_advisor, "swarming_task_stdout", return_value=stdout), patch.object(
        canary_advisor, "_sso_fetch_json", return_value=retry_change
    ):
      res_retry = canary_advisor.triage_canary_android_failure(
          1421816, 4, {"status": "FAILURE"}, sw_tasks, ["src/ports/SkFontHost_FreeType.cpp"]
      )
    self.assertEqual(res_retry["verdict"], "UNRELATED_HEAD_BREAKAGE")
    self.assertTrue(res_retry["our_part_passed"])
    self.assertIn("121 targets passed, 0 build failures", res_retry["sanitized_draft"])
    self.assertEqual(canary_advisor.find_public_draft_leak(res_retry["sanitized_draft"]), "")
    self.assertNotIn("menari_lte", res_retry["sanitized_draft"])
    self.assertNotIn("43653565", res_retry["sanitized_draft"])

    # Non-Skia AnTS failure passes, whereas HWUI/Skia frame fails closed as CL_SUSPECTED_REGRESSION
    fail_change = {"messages": [retry_change["messages"][0].copy()]}
    fail_change["messages"][0]["message"] = (
        "## Presubmit finished: 120 passed, 1 failed\n"
        "FAIL: Tests failed for [CtsNetTestCases](https://android-build.googleplex.com/"
        "builds/tests/view?invocationId=I8570000105396069999) on pixel_fold"
    )
    non_skia_ants = json.dumps({
        "testResults": [{
            "testIdentifier": {"module": "CtsNetTestCases", "testClass": " ConnectivityTest"},
            "debugInfo": {"errorMessage": "SocketTimeoutException", "trace": "android.net.Foo"},
        }]
    })
    hwui_ants = json.dumps({
        "testResults": [{
            "testIdentifier": {"module": "CtsUiRenderingTestCases", "testClass": "CanvasTest"},
            "debugInfo": {"errorMessage": "crash", "trace": "android::uirenderer::SkiaPipeline"},
        }]
    })
    with patch.object(canary_advisor, "swarming_task_stdout", return_value=stdout), patch.object(
        canary_advisor, "_sso_fetch_json", return_value=fail_change
    ), patch.object(canary_advisor, "_android_build_api_get", return_value=non_skia_ants):
      res_ants_ok = canary_advisor.triage_canary_android_failure(
          1421816, 4, {"status": "FAILURE"}, sw_tasks, ["src/ports/SkFontHost_FreeType.cpp"]
      )
    self.assertEqual(res_ants_ok["verdict"], "UNRELATED_HEAD_BREAKAGE")
    self.assertEqual(canary_advisor.find_public_draft_leak(res_ants_ok["sanitized_draft"]), "")

    with patch.object(canary_advisor, "swarming_task_stdout", return_value=stdout), patch.object(
        canary_advisor, "_sso_fetch_json", return_value=fail_change
    ), patch.object(canary_advisor, "_android_build_api_get", return_value=hwui_ants):
      res_hwui = canary_advisor.triage_canary_android_failure(
          1421816, 4, {"status": "FAILURE"}, sw_tasks, ["src/ports/SkFontHost_FreeType.cpp"]
      )
    self.assertEqual(res_hwui["verdict"], "CL_SUSPECTED_REGRESSION")
    self.assertFalse(res_hwui["our_part_passed"])

  # Checks R135 public Gerrit leak blocker and in-place per-canary draft paragraph replacement
  def test_save_gerrit_patchset_draft_blocks_corporate_leaks_and_replaces_same_canary_note(self):
    leaking_inputs = [
        "Canary-G3 failed on http://cl/1017355544.",
        "Canary-Android failed on ag/43653565.",
        "Canary-Chromium failed on " + "cr" + "/8774115.",
        "Tracked in b/493545344.",
        "See go/skia-g3-policy for details.",
        "Host https://android-build.googleplex.com/builds failed.",
        "Target //video/youtube/scotty:handler_test failed.",
        "Invocation I8570000105396069325 timed out.",
    ]
    with patch.object(canary_advisor, "_gerrit_authed_urlopen") as mock_http:
      for bad_text in leaking_inputs:
        with self.subTest(bad_text=bad_text), patch("sys.stdout", io.StringIO()):
          self.assertFalse(canary_advisor.save_gerrit_patchset_draft(1421656, 4, bad_text))
      mock_http.assert_not_called()

    merged = canary_advisor._merge_patchset_draft_notes(
        "Reviewer note.\n\nCanary-Chromium roll (https://crrev.com/c/1/1) failed.",
        "Canary-Chromium roll (https://crrev.com/c/1/2) passed on 48 CQ trybots.\n\n"
        "Canary-G3 failed only on unrelated downstream targets.",
    )
    self.assertEqual(
        merged,
        "Reviewer note.\n\n"
        "Canary-Chromium roll (https://crrev.com/c/1/2) passed on 48 CQ trybots.\n\n"
        "Canary-G3 failed only on unrelated downstream targets.",
    )

  # Checks R135 NO_CODE_CHANGE multi-patchset aggregation (PS2+PS3 -> PS4) and saving the red-canary
  # /PATCHSET_LEVEL draft onto current_patchset (PS4) so it never lands stale on PS3
  def test_cmd_triage_aggregates_no_code_change_patchsets_and_saves_draft_on_current_ps(self):
    asan = "Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-ASAN"
    meta_ps3 = {
        "cl": 1421656,
        "patchset": 3,
        "current_patchset": 4,
        "status": "NEW",
        "subject": "[SkScalerContext] Harden mask-filtered getImage",
        "owner": "jlavrova@google.com",
        "diff": "diff --git a/src/core/SkScalerContext.cpp b/src/core/SkScalerContext.cpp\n+// x\n",
        "googler_cr_plus_one": True,
        "kinds": {1: "REWORK", 2: "REWORK", 3: "NO_CODE_CHANGE", 4: "NO_CODE_CHANGE"},
    }
    ps_builds = {
        2: {asan: {"id": "802", "status": "SUCCESS", "createTime": "2026-11-09T20:00:00Z"}},
        3: {"Canary-Chromium": {"id": "803", "status": "FAILURE",
                                "createTime": "2026-11-09T21:00:00Z"}},
        4: {},
    }
    cr_verdict = {
        "verdict": "UNRELATED_HEAD_BREAKAGE",
        "builder": "Canary-Chromium",
        "cr_cl": 8774115,
        "roll_ps": 2,
        "our_part_passed": True,
        "sanitized_draft": (
            "Canary-Chromium roll (https://crrev.com/c/8774115/2) passed on 48 Chromium CQ trybots."
        ),
    }

    # Without --save-draft and no existing draft on PS 4 -> CANARY DRAFT: MISSING on PS 4
    buf_missing = io.StringIO()
    args_no_save = type(
        "Args", (), {"cl": "1421656", "ps": "4", "save_draft": False, "json": False}
    )()
    with patch.object(
        canary_advisor, "load_cl", return_value={**meta_ps3, "patchset": 4}
    ), patch.object(
        canary_advisor, "bb_cl_builds", side_effect=lambda _cl, p: ps_builds[int(p)]
    ), patch.object(
        canary_advisor, "swarming_tasks_for_cl", return_value=[]
    ), patch.object(
        canary_advisor, "triage_canary_chromium_failure", return_value=cr_verdict
    ), patch.object(
        canary_advisor, "tested_block_gaps", return_value=[]
    ), patch.object(
        canary_advisor, "has_gerrit_canary_drafts", return_value=False
    ), patch("sys.stdout", buf_missing):
      canary_advisor.cmd_triage(args_no_save)
    out_missing = buf_missing.getvalue()
    self.assertIn("TRYJOBS: ALL GREEN", out_missing)
    self.assertIn("CANARY DRAFT: MISSING on PS 4 (Canary-Chromium)", out_missing)

    # With --save-draft while inspecting PS 3 -> still saves onto current_patchset PS 4!
    saved_calls = []
    buf_save = io.StringIO()
    args_save = type("Args", (), {"cl": "1421656", "ps": "3", "save_draft": True, "json": False})()
    with patch.object(canary_advisor, "load_cl", return_value=meta_ps3), patch.object(
        canary_advisor, "bb_cl_builds", side_effect=lambda _cl, p: ps_builds[int(p)]
    ), patch.object(
        canary_advisor, "swarming_tasks_for_cl", return_value=[]
    ), patch.object(
        canary_advisor, "triage_canary_chromium_failure", return_value=cr_verdict
    ), patch.object(
        canary_advisor, "tested_block_gaps", return_value=[]
    ), patch.object(
        canary_advisor,
        "save_gerrit_patchset_draft",
        side_effect=lambda cl, ps, msg, unresolved=False: saved_calls.append((cl, ps, msg)) or True,
    ), patch("sys.stdout", buf_save):
      res_save = canary_advisor.cmd_triage(args_save)
    out_save = buf_save.getvalue()
    self.assertEqual(res_save["draft_patchset"], 4)
    self.assertEqual(len(saved_calls), 1)
    self.assertEqual(saved_calls[0][1], 4)
    self.assertIn("CANARY DRAFT: OK (present on PS 4)", out_save)
    self.assertIn("TESTED BLOCK: OK", out_save)

  # Checks R135 cmd_watch across NO_CODE_CHANGE patchsets: watching PS4 waits for a canary running
  # on PS3 instead of exiting early after seeing only PS2 ASAN
  def test_cmd_watch_polls_across_same_tree_no_code_change_patchsets(self):
    asan = "Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-ASAN"
    meta = {
        "cl": 1421656,
        "patchset": 4,
        "current_patchset": 4,
        "status": "NEW",
        "subject": "Test",
        "owner": "jlavrova@google.com",
        "diff": "diff --git a/src/core/SkScalerContext.cpp b/src/core/SkScalerContext.cpp\n+// x\n",
        "googler_cr_plus_one": True,
        "kinds": {1: "REWORK", 2: "REWORK", 3: "NO_CODE_CHANGE", 4: "NO_CODE_CHANGE"},
    }
    tick = {"n": 0}

    def fake_bb(_cl, ps):
      ps = int(ps)
      if ps == 2:
        return {asan: {"id": "1", "status": "SUCCESS", "createTime": "2026-11-09T20:00:00Z"}}
      if ps == 3:
        st = "STARTED" if tick["n"] == 0 else "FAILURE"
        tick["n"] += 1
        return {"Canary-Chromium": {"id": "2", "status": st, "createTime": "2026-11-09T21:00:00Z"}}
      return {}

    args = type(
        "Args",
        (),
        {
            "cl": "1421656",
            "ps": "4",
            "poll_sec": 1,
            "timeout_min": 5,
            "no_builds_min": 10,
            "save_draft": True,
            "json": False,
        },
    )()
    buf = io.StringIO()
    with patch.object(canary_advisor, "load_cl", return_value=meta), patch.object(
        canary_advisor, "bb_cl_builds", side_effect=fake_bb
    ), patch.object(canary_advisor, "cmd_triage", return_value={}) as mock_triage, patch.object(
        canary_advisor.time, "sleep"
    ) as mock_sleep, patch("sys.stdout", buf):
      rc = canary_advisor.cmd_watch(args)
    self.assertEqual(rc, 0)
    self.assertEqual(mock_sleep.call_count, 1)
    mock_triage.assert_called_once()
    self.assertIn("ALL_TRYJOBS_COMPLETE", buf.getvalue())

  # Checks R135 B1/B2 fail-closed Chromium roll gates: PS3-first lookup, presubmit-only/CANCELED/
  # STARTED rejection, caller compile without-patch check, browser crash, and mutual shard aborts
  def test_triage_canary_chromium_fails_closed_on_presubmit_only_unpassed_and_caller_compile(self):
    sw_tasks = [{"name": "Canary-Chromium", "taskId": "task_cr_b1"}]
    stdout = "Created roll https://chromium-review.googlesource.com/c/chromium/src/+/8774115\n"

    # PS3 must be queried before PS2, and presubmit-only + CANCELED C++ bot must fail closed
    seen_ps = []

    def fake_ps_order(_url, payload, timeout=15):
      ps = payload["predicate"]["gerritChanges"][0]["patchset"]
      seen_ps.append(ps)
      if ps == 3:
        return {
            "builds": [
                {"id": "1", "status": "SUCCESS", "builder": {"builder": "chromium_presubmit"}},
                {"id": "2", "status": "CANCELED", "builder": {"builder": "linux-rel"}},
            ]
        }
      return {"builds": [{"id": "3", "status": "SUCCESS", "builder": {"builder": "linux-rel"}}]}

    with patch.object(canary_advisor, "swarming_task_stdout", return_value=stdout), patch.object(
        canary_advisor, "_prpc_json_post", side_effect=fake_ps_order
    ):
      res_presub = canary_advisor.triage_canary_chromium_failure(
          1421656, 3, {"status": "FAILURE"}, sw_tasks, ["src/core/SkScalerContext.cpp"]
      )
    self.assertEqual(seen_ps[0], 3)
    self.assertEqual(res_presub["verdict"], "CL_SUSPECTED_REGRESSION")
    self.assertFalse(res_presub["our_part_passed"])

    # STARTED trybot alongside green linux-rel must fail closed
    search_started = {
        "builds": [
            {"id": "10", "status": "SUCCESS", "builder": {"builder": "linux-rel"}},
            {"id": "11", "status": "STARTED", "builder": {"builder": "win-rel"}},
        ]
    }
    with patch.object(canary_advisor, "swarming_task_stdout", return_value=stdout), patch.object(
        canary_advisor, "_prpc_json_post", return_value=search_started
    ):
      res_started = canary_advisor.triage_canary_chromium_failure(
          1421656, 3, {"status": "FAILURE"}, sw_tasks, ["src/core/SkScalerContext.cpp"]
      )
    self.assertFalse(res_started["our_part_passed"])

    # Caller compile error in cc/paint without "skia" in snippet fails closed when compile
    # (without patch) passed, and clears only when compile (without patch) also failed
    cc_sum = "step _compile failed:\n```\n../../cc/paint/paint_op_buffer.cc:44: error: no match\n```"
    search_cc = {
        "builds": [
            {"id": "20", "status": "SUCCESS", "builder": {"builder": "mac-rel"}},
            {"id": "21", "status": "FAILURE", "builder": {"builder": "linux-rel"},
             "summaryMarkdown": cc_sum},
        ]
    }
    detail_wo_ok = {
        "status": "FAILURE",
        "summaryMarkdown": cc_sum,
        "steps": [
            {"name": "compile (with patch)", "status": "FAILURE"},
            {"name": "compile (without patch)", "status": "SUCCESS"},
        ],
    }
    detail_wo_fail = {
        "status": "FAILURE",
        "summaryMarkdown": cc_sum,
        "steps": [
            {"name": "compile (with patch)", "status": "FAILURE"},
            {"name": "compile (without patch)", "status": "FAILURE"},
        ],
    }
    with patch.object(canary_advisor, "swarming_task_stdout", return_value=stdout), patch.object(
        canary_advisor, "_prpc_json_post", side_effect=[search_cc, detail_wo_ok]
    ):
      self.assertFalse(
          canary_advisor.triage_canary_chromium_failure(
              1421656, 3, {"status": "FAILURE"}, sw_tasks, ["src/core/SkScalerContext.cpp"]
          )["our_part_passed"]
      )
    with patch.object(canary_advisor, "swarming_task_stdout", return_value=stdout), patch.object(
        canary_advisor, "_prpc_json_post", side_effect=[search_cc, detail_wo_fail]
    ):
      self.assertTrue(
          canary_advisor.triage_canary_chromium_failure(
              1421656, 3, {"status": "FAILURE"}, sw_tasks, ["src/core/SkScalerContext.cpp"]
          )["our_part_passed"]
      )

    # BrowserConnectionGoneException and two mutual early-shard crashes must both fail closed
    search_browser_crash = {
        "builds": [
            {"id": "30", "status": "SUCCESS", "builder": {"builder": "mac-rel"}},
            {"id": "31", "status": "FAILURE", "builder": {"builder": "linux-rel"},
             "summaryMarkdown": "BrowserConnectionGoneException during telemetry_gpu_integration"},
        ]
    }
    detail_browser = {
        "status": "FAILURE",
        "summaryMarkdown": "BrowserConnectionGoneException during telemetry_gpu_integration",
        "steps": [{"name": "pixel_skia_gold_validating_test", "status": "FAILURE"}],
    }
    with patch.object(canary_advisor, "swarming_task_stdout", return_value=stdout), patch.object(
        canary_advisor, "_prpc_json_post", side_effect=[search_browser_crash, detail_browser]
    ):
      self.assertFalse(
          canary_advisor.triage_canary_chromium_failure(
              1421656, 3, {"status": "FAILURE"}, sw_tasks, ["src/core/SkScalerContext.cpp"]
          )["our_part_passed"]
      )

    search_mutual_abort = {
        "builds": [
            {"id": "40", "status": "SUCCESS", "builder": {"builder": "win-rel"}},
            {"id": "41", "status": "FAILURE", "builder": {"builder": "linux-rel"},
             "summaryMarkdown": "Suite failed: did a shard fail early?"},
            {"id": "42", "status": "FAILURE", "builder": {"builder": "mac-rel"},
             "summaryMarkdown": "Suite failed: did a shard fail early?"},
        ]
    }
    with patch.object(canary_advisor, "swarming_task_stdout", return_value=stdout), patch.object(
        canary_advisor, "_prpc_json_post", return_value=search_mutual_abort
    ):
      self.assertFalse(
          canary_advisor.triage_canary_chromium_failure(
              1421656, 3, {"status": "FAILURE"}, sw_tasks, ["src/core/SkScalerContext.cpp"]
          )["our_part_passed"]
      )

  # Checks R135 B3 Android gates: newest revision verdict precedence, unparsed FAIL lines,
  # Minikin/CtsText/PDFium consumer detection, AnTS pagination, and signedUrl build_error.log
  def test_triage_canary_android_fails_closed_on_latest_verdict_unparsed_fail_and_consumers(self):
    sw_tasks = [{"name": "Canary-Android", "taskId": "task_ag_b3"}]
    stdout = (
        "Roll https://googleplex-android-review.googlesource.com/c/"
        "platform/external/skia/+/43653565 failed\n"
    )

    # Latest message is Verify-Fail with unparsed Soong error + Removed Presubmit-Verified+1 after
    # an older Verify-Pass: must NOT walk backward into the older Verify-Pass
    soong_fail_change = {
        "messages": [
            {
                "_revision_number": 1,
                "tag": "autogenerated:TreeHugger:Verify-Pass",
                "message": "Patch Set 1: Presubmit-Verified+1\n\n## Presubmit finished: 100 passed",
            },
            {
                "_revision_number": 2,
                "tag": "autogenerated:TreeHugger:Verify-Fail",
                "message": (
                    "Patch Set 2: Presubmit-Verified-1 (Removed Presubmit-Verified+1)\n\n"
                    "## Presubmit finished: 0 passed, 1 failed\n"
                    "FAIL: Soong blueprint hook failed in external/skia/Android.bp"
                ),
            },
        ]
    }
    with patch.object(canary_advisor, "swarming_task_stdout", return_value=stdout), patch.object(
        canary_advisor, "_sso_fetch_json", return_value=soong_fail_change
    ):
      self.assertFalse(
          canary_advisor.triage_canary_android_failure(
              1421816, 4, {"status": "FAILURE"}, sw_tasks, ["src/ports/SkFontMgr_android.cpp"]
          )["our_part_passed"]
      )

    # Unparsed FAIL alongside INFRA_ERROR test failure must also fail closed
    mixed_unparsed = {
        "messages": [{
            "_revision_number": 2,
            "tag": "autogenerated:TreeHugger:Verify-Fail",
            "message": (
                "## Presubmit finished: 118 passed, 2 failed\n"
                "FAIL: Tests failed for [pts-bot](https://android-build.googleplex.com/"
                "builds/tests/view?invocationId=I8570000105396069325) on dev. "
                "Caused by UNKNOWN_INFRA_ERROR\n"
                "FAIL: Hook check_api_lint failed"
            ),
        }]
    }
    with patch.object(canary_advisor, "swarming_task_stdout", return_value=stdout), patch.object(
        canary_advisor, "_sso_fetch_json", return_value=mixed_unparsed
    ):
      self.assertFalse(
          canary_advisor.triage_canary_android_failure(
              1421816, 4, {"status": "FAILURE"}, sw_tasks, ["src/ports/SkFontMgr_android.cpp"]
          )["our_part_passed"]
      )

    # Minikin / CtsText failure on page 2 of AnTS pagination must be caught and fail closed
    ants_change = {
        "messages": [{
            "_revision_number": 1,
            "tag": "autogenerated:TreeHugger:Verify-Fail",
            "message": (
                "## Presubmit finished: 110 passed, 1 failed\n"
                "FAIL: Tests failed for [CtsTestSuite](https://android-build.googleplex.com/"
                "builds/tests/view?invocationId=I8570000105396069777) on pixel"
            ),
        }]
    }
    page1 = json.dumps({
        "testResults": [{
            "testIdentifier": {"module": "CtsNetTestCases", "testClass": "NetTest"},
            "debugInfo": {"errorMessage": "timeout", "trace": "android.net.Socket"},
        }],
        "nextPageToken": "tok_p2",
    })
    page2_minikin = json.dumps({
        "testResults": [{
            "testIdentifier": {"module": "CtsTextTestCases", "testClass": "StaticLayoutTest"},
            "debugInfo": {"errorMessage": "mismatch", "trace": "minikin::Layout::doLayout"},
        }],
    })
    with patch.object(canary_advisor, "swarming_task_stdout", return_value=stdout), patch.object(
        canary_advisor, "_sso_fetch_json", return_value=ants_change
    ), patch.object(canary_advisor, "_android_build_api_get", side_effect=[page1, page2_minikin]):
      self.assertFalse(
          canary_advisor.triage_canary_android_failure(
              1421816, 4, {"status": "FAILURE"}, sw_tasks, ["src/ports/SkFontMgr_android.cpp"]
          )["our_part_passed"]
      )

    # Build failure returning {"signedUrl": ...} downloads the signed log and classifies cleanly
    build_change = {
        "messages": [{
            "_revision_number": 1,
            "tag": "autogenerated:TreeHugger:Verify-Fail",
            "message": (
                "## Presubmit finished: 90 passed, 1 failed\n"
                "FAIL: Failed to build [cf_x86_64](https://android-build.googleplex.com/builds/"
                "submitted/P12345/cf_x86_64-userdebug/latest/view/logs%2Fbuild_error.log)"
            ),
        }]
    }

    class _SignedResp:
      def __enter__(self):
        return self

      def __exit__(self, *args):
        return False

      def read(self, _n=-1):
        return b"frameworks/base/WindowManager.cpp:12:3: error: undeclared identifier 'wm'\n"

    with patch.object(canary_advisor, "swarming_task_stdout", return_value=stdout), patch.object(
        canary_advisor, "_sso_fetch_json", return_value=build_change
    ), patch.object(
        canary_advisor,
        "_android_build_api_get",
        return_value=json.dumps({"signedUrl": "https://storage.googleapis.com/ants/err.log"}),
    ), patch.object(canary_advisor.urllib.request, "urlopen", return_value=_SignedResp()):
      res_signed = canary_advisor.triage_canary_android_failure(
          1421816, 4, {"status": "FAILURE"}, sw_tasks, ["src/ports/SkFontMgr_android.cpp"]
      )
    self.assertTrue(res_signed["our_part_passed"])
    self.assertEqual(res_signed["verdict"], "UNRELATED_HEAD_BREAKAGE")

  # Checks R135 B4 Canary-G3 fail-closed somepath when Blaze or GOOGLE3_DIR is unavailable, GN/test
  # filtering on mixed diffs (CL 1421816), and requiring every CheckTestsSkia run to pass
  def test_triage_canary_g3_fails_closed_when_somepath_inconclusive_or_duplicate_run_failed(self):
    with patch("os.path.isdir", return_value=False):
      status, lines = canary_advisor.check_google3_somepath(
          "//video/youtube:test", ["src/core/SkScalerContext.cpp"]
      )
    self.assertIsNone(status)
    self.assertEqual(lines, [])

    # GN-only and tests/ files must not pollute Blaze target labels (CL 1421816)
    seen_queries = []

    def fake_blaze(cmd, **kwargs):
      seen_queries.append(cmd[2])
      return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch("os.path.isdir", return_value=True), patch(
        "os.path.isfile",
        side_effect=lambda p: p.endswith("src/ports/BUILD") or p.endswith("HEAD/BUILD"),
    ), patch("os.path.exists", return_value=True), patch.object(
        canary_advisor.subprocess, "run", side_effect=fake_blaze
    ):
      st_gn, _ = canary_advisor.check_google3_somepath(
          "//video/youtube:test",
          [
              "BUILD.gn",
              "src/ports/SkFontHost_FreeType_common.cpp",
              "tests/FontHost_FreeTypeTest.cpp",
          ],
      )
    self.assertIs(st_gn, False)
    want_q = (
        "somepath(//video/youtube:test, "
        "set(//third_party/skia/HEAD/src/ports:SkFontHost_FreeType_common.cpp))"
    )
    self.assertEqual(seen_queries, [want_q])

    sw_tasks = [{"name": "Canary-G3", "taskId": "task_g3_b4"}]
    stdout = "Canary roll [ http://cl/1017394648 ] failed\n"
    gardener_unknown_tgt = {
        "Verdict": "DEPENDENT_HEAD_UNKNOWN",
        "Runs": [
            {"id": "r1", "check": "CheckTestsSkia", "status": "PASSED", "failed": 0},
            {"id": "r2", "check": "CheckSkiaDependents", "status": "PASSED", "failed": 0},
            {"id": "r3", "check": "CheckTestsSkiaDependents", "status": "FAILED", "failed": 1},
        ],
        "Targets": [
            {"target": "//video/youtube:handler_test", "kind": "UNKNOWN", "failed_in_latest": True}
        ],
    }
    proc_ok = type(
        "P", (), {"returncode": 0, "stdout": json.dumps(gardener_unknown_tgt), "stderr": ""}
    )()
    with patch("os.path.isdir", return_value=True), patch.object(
        canary_advisor, "swarming_task_stdout", return_value=stdout
    ), patch.object(
        canary_advisor.subprocess, "run", return_value=proc_ok
    ), patch.object(canary_advisor, "check_google3_somepath", return_value=(None, [])):
      res_inconc = canary_advisor.triage_canary_g3_failure(
          1421656, 3, {"status": "FAILURE"}, sw_tasks, ["src/core/SkScalerContext.cpp"]
      )
    self.assertEqual(res_inconc["verdict"], "CL_SUSPECTED_REGRESSION")
    self.assertFalse(res_inconc["our_part_passed"])

    # Split CheckTestsSkia runs (one PASSED, one FAILED, neither superseded) must fail closed
    gardener_split = {
        "Verdict": "UNKNOWN",
        "Runs": [
            {"id": "r1", "check": "CheckTestsSkia", "status": "PASSED", "failed": 0},
            {"id": "r2", "check": "CheckTestsSkia", "status": "FAILED", "failed": 1},
            {"id": "r3", "check": "CheckSkiaDependents", "status": "PASSED", "failed": 0},
            {"id": "r4", "check": "CheckTestsSkiaDependents", "status": "PASSED", "failed": 0},
        ],
        "Targets": [],
    }
    proc_split = type(
        "P", (), {"returncode": 0, "stdout": json.dumps(gardener_split), "stderr": ""}
    )()
    with patch("os.path.isdir", return_value=True), patch.object(
        canary_advisor, "swarming_task_stdout", return_value=stdout
    ), patch.object(canary_advisor.subprocess, "run", return_value=proc_split):
      res_split = canary_advisor.triage_canary_g3_failure(
          1421656, 3, {"status": "FAILURE"}, sw_tasks, ["src/core/SkScalerContext.cpp"]
      )
    self.assertFalse(res_split["our_part_passed"])

  # Checks R135 B5: thread-reply drafts (in_reply_to) are never overwritten or counted as top-level
  # canary drafts, and all *.corp.google.com / *.corp.goog domains are blocked by leak check
  def test_patchset_draft_ignores_thread_replies_and_blocks_all_corp_domains(self):
    for corp_leak in (
        "Bug https://b.corp.google.com/issues/580852205 reproduced.",
        "Mirror https://skia-review.git.corp.google.com/c/skia/+/1421656 failed.",
        "Dashboard https://autoroll-internal.corp.goog/r/skia failed.",
    ):
      self.assertNotEqual(canary_advisor.find_public_draft_leak(corp_leak), "", msg=corp_leak)

    reply_only_drafts = json.dumps({
        "/PATCHSET_LEVEL": [{
            "id": "reply_draft_99",
            "in_reply_to": "reviewer_comment_1",
            "message": "Done — Canary-Chromium was unrelated.",
            "unresolved": False,
        }]
    }).encode("utf-8")
    calls = []

    def fake_authed(url, data=None, method=None, headers=None, timeout=20):
      calls.append((method or ("POST" if data else "GET"), url, data))
      if method == "PUT":
        return (201, b")]}'\n{\"id\":\"new_top_draft\"}"), None
      return (200, b")]}'\n" + reply_only_drafts), None

    with patch.object(canary_advisor, "_gerrit_authed_urlopen", side_effect=fake_authed):
      self.assertFalse(canary_advisor.has_gerrit_canary_drafts(1421656, 4, ["Canary-Chromium"]))
      ok = canary_advisor.save_gerrit_patchset_draft(
          1421656, 4, "Canary-Chromium roll (https://crrev.com/c/8774115/2) passed on 49 trybots."
      )
    self.assertTrue(ok)
    put_calls = [c for c in calls if c[0] == "PUT"]
    self.assertEqual(len(put_calls), 1)
    # Must create a fresh top-level draft at .../drafts, NOT overwrite .../drafts/reply_draft_99
    self.assertTrue(put_calls[0][1].endswith("/revisions/4/drafts"), msg=put_calls[0][1])

  # Checks R135 B6 & Grill 1: cmd_watch advances across NO_CODE_CHANGE patchsets without SUPERSEDED,
  # cmd_triage --ps 3 includes PS4 builds on the same tree, and advise prints watch --save-draft
  def test_cmd_watch_and_triage_follow_no_code_change_patchsets_and_advise_prints_watch(self):
    meta_ps3 = {
        "cl": 1421656,
        "patchset": 3,
        "current_patchset": 3,
        "status": "NEW",
        "subject": "Test",
        "owner": "jlavrova@google.com",
        "diff": "diff --git a/src/core/SkScalerContext.cpp b/src/core/SkScalerContext.cpp\n+// x\n",
        "googler_cr_plus_one": True,
        "kinds": {1: "REWORK", 2: "REWORK", 3: "NO_CODE_CHANGE"},
    }
    meta_ps4 = {
        **meta_ps3,
        "patchset": 4,
        "current_patchset": 4,
        "kinds": {1: "REWORK", 2: "REWORK", 3: "NO_CODE_CHANGE", 4: "NO_CODE_CHANGE"},
    }
    watch_args = type(
        "Args",
        (),
        {
            "cl": "1421656",
            "ps": None,
            "poll_sec": 1,
            "timeout_min": 5,
            "no_builds_min": 10,
            "save_draft": True,
            "json": False,
        },
    )()
    buf_watch = io.StringIO()
    with patch.object(canary_advisor, "load_cl", side_effect=[meta_ps3, meta_ps4]), patch.object(
        canary_advisor,
        "bb_cl_builds",
        side_effect=lambda _cl, p: (
            {"Canary-Chromium": {"id": "9", "status": "SUCCESS"}} if int(p) == 3 else {}
        ),
    ), patch.object(canary_advisor, "cmd_triage", return_value={}) as mock_triage, patch(
        "sys.stdout", buf_watch
    ):
      rc = canary_advisor.cmd_watch(watch_args)
    self.assertEqual(rc, 0)
    self.assertNotIn("SUPERSEDED", buf_watch.getvalue())
    self.assertIn("ALL_TRYJOBS_COMPLETE", buf_watch.getvalue())
    mock_triage.assert_called_once()

    # cmd_triage --ps 3 when current_patchset is PS 4 (NO_CODE_CHANGE) must scan PS 4 builds too
    triage_args = type("Args", (), {"cl": "1421656", "ps": "3", "save_draft": False, "json": False})()
    with patch.object(
        canary_advisor, "load_cl", return_value={**meta_ps4, "patchset": 3}
    ), patch.object(
        canary_advisor,
        "bb_cl_builds",
        side_effect=lambda _cl, p: (
            {"Canary-G3": {"id": "77", "status": "SUCCESS", "createTime": "2026-11-09T22:00:00Z"}}
            if int(p) == 4
            else {}
        ),
    ), patch.object(canary_advisor, "tested_block_gaps", return_value=[]), patch(
        "sys.stdout", io.StringIO()
    ):
      res_triage = canary_advisor.cmd_triage(triage_args)
    self.assertIn("Canary-G3", res_triage["builds"])

    # advise on own CL prints watch --cl <CL> --save-draft under Canary Command
    adv_args = type(
        "Args",
        (),
        {
            "cl": "1421656",
            "repo": None,
            "base": "origin/main",
            "max_symbols": 6,
            "no_search": True,
            "no_health": True,
            "no_log": True,
            "strong_only": False,
        },
    )()
    buf_adv = io.StringIO()
    with patch.object(
        canary_advisor, "load_cl", return_value={**meta_ps3, "owner": canary_advisor.ME}
    ), patch("sys.stdout", buf_adv):
      canary_advisor.advise(adv_args)
    adv_out = buf_adv.getvalue()
    canary_section = adv_out.split("Canary Command (run only after user approval):")[-1]
    self.assertIn("watch --cl 1421656 --save-draft", canary_section)


if __name__ == "__main__":
  unittest.main()
