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
        if line.strip().startswith("luci-auth context -- git cl try")
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
        if line.strip().startswith("luci-auth context -- git cl try")
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
        if line.strip().startswith("luci-auth context -- git cl try")
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


if __name__ == "__main__":
  unittest.main()
