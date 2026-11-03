#!/usr/bin/env python3
# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for Flight Deck Security merge request detection, gates, UI, and confirm."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


def _run_git(cwd: str, *args: str) -> str:
  res = subprocess.run(
      [
          "git",
          "-C",
          cwd,
          "-c",
          "user.name=Test User",
          "-c",
          "user.email=jlavrova@google.com",
          *args,
      ],
      capture_output=True,
      text=True,
      check=True,
  )
  return res.stdout.strip()


def _build_parent_and_merge_blocks(
    *,
    fixed_by: str = "https://skia-review.googlesource.com/c/skia/+/1404196",
    parent_gerrit: str = (
        "https://skia-review.git.corp.google.com/c/skia/+/1404196, "
        "https://skia-review.git.corp.google.com/c/skia/+/1411716"
    ),
    parent_os: str = "Android, Linux, Windows, ChromeOS",
    m156_os: str = "Android, Linux, Windows, ChromeOS",
    merge_val: str = "Pending",
    auto_cp: str = "Yes",
    assignee: str = "jlavrova@google.com",
    reporter: str = "chrome-release-blintz-continuous-runner@prod.google.com",
    comp_id: str = "1579812",
) -> str:
  blocks = [
      f"Issue ID: 990000100\n"
      f"Component ID: 1457031\n"
      f"Assignee: jlavrova@google.com\n"
      f"Reporter: vmiura@google.com\n"
      f"Status: FIXED\n"
      f"Priority: P1\n"
      f"Issue Type: VULNERABILITY\n"
      f"Title: Potential heap OOB write via integer overflow in SkDistanceFieldGen\n"
      f"Created Time: 2026-10-22T15:56:27.408Z\n"
      f"Modified Time: 2026-11-03T08:16:50.218Z\n"
      f"Gerrit changes: {parent_gerrit}\n"
      f"Severity: S1\n"
      f"Custom Field: Component Tags (1222907)\n"
      f"Custom Field Value: Internals>Skia\n"
      f"Custom Field: OS (1223084)\n"
      f"Custom Field Value: {parent_os}\n"
  ]
  if fixed_by:
    blocks[0] += (
        f"Custom Field: Fixed By Code Changes (1358989)\n"
        f"Custom Field Value: {fixed_by}\n"
    )
  for bid, ms in (("990000156", 156), ("990000157", 157), ("990000158", 158)):
    child_os = m156_os if ms == 156 else parent_os
    blocks.append(
        f"Issue ID: {bid}\n"
        f"Component ID: {comp_id}\n"
        f"Assignee: {assignee}\n"
        f"Reporter: {reporter}\n"
        f"Status: ASSIGNED\n"
        f"Priority: P1\n"
        f"Issue Type: VULNERABILITY\n"
        f"Title: [Merge M{ms}] Potential heap OOB write via integer overflow "
        f"in SkDistanceFieldGen\n"
        f"Created Time: 2026-11-03T08:13:28.708Z\n"
        f"Modified Time: 2026-11-03T08:13:31.945Z\n"
        f"Parent Issue IDs: 990000100\n"
        f"Gerrit changes: https://skia-review.git.corp.google.com/c/skia/+/1404196\n"
        f"Severity: S1\n"
        f"Custom Field: Component Tags (1222907)\n"
        f"Custom Field Value: Programs>PMO>Browser>Release>Merge Request\n"
        f"Custom Field: OS (1223084)\n"
        f"Custom Field Value: {child_os}\n"
        f"Custom Field: Merge (1223087)\n"
        f"Custom Field Value: {merge_val}\n"
        f"Custom Field: Merge Milestone (1340419)\n"
        f"Custom Field Value: {ms}\n"
        f"Custom Field: Auto Cherry Pick (1419876)\n"
        f"Custom Field Value: {auto_cp}\n"
    )
  return "\n---\n\n".join(blocks)


class SecurityMergeRepoHarness:
  """Builds a real local Skia git repository with main and chrome/m156..m158 branches."""

  def __init__(self) -> None:
    self._tmp = tempfile.TemporaryDirectory(prefix="fd_sec_merge_test_")
    self.skia_dir = os.path.join(self._tmp.name, "skia_main")
    os.makedirs(os.path.join(self.skia_dir, "src", "core"), exist_ok=True)
    os.makedirs(os.path.join(self.skia_dir, "tests"), exist_ok=True)
    _run_git(self.skia_dir, "init", "-b", "main")
    self._write(
        "src/core/SkDistanceFieldGen.cpp",
        "int computeSize(int w, int h) {\n  return w * h;\n}\n",
    )
    self._write("tests/DistanceFieldTest.cpp", "// initial test\n")
    _run_git(self.skia_dir, "add", ".")
    _run_git(self.skia_dir, "commit", "-m", "Initial baseline")
    self.base_sha = _run_git(self.skia_dir, "rev-parse", "HEAD")

    for ms in (156, 157, 158):
      _run_git(
          self.skia_dir,
          "update-ref",
          f"refs/remotes/origin/chrome/m{ms}",
          self.base_sha,
      )

    self._write(
        "src/core/SkDistanceFieldGen.cpp",
        "int computeSize(int w, int h) {\n"
        "  if (w <= 0 || h <= 0 || w > 10000) return 0;\n"
        "  return w * h;\n"
        "}\n",
    )
    self._write(
        "tests/DistanceFieldTest.cpp",
        "// initial test\n// boundary overflow check\n",
    )
    _run_git(self.skia_dir, "add", ".")
    _run_git(
        self.skia_dir,
        "commit",
        "-m",
        "Fix integer overflow in SDF generation\n\n"
        "Validate positive dimensions and guard buffer size.\n",
    )
    self.fix_sha = _run_git(self.skia_dir, "rev-parse", "HEAD")
    _run_git(self.skia_dir, "update-ref", "refs/remotes/origin/main", self.fix_sha)

  def _write(self, rel: str, content: str) -> None:
    full = os.path.join(self.skia_dir, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
      f.write(content)

  def add_conflict_on_m156(self) -> None:
    _run_git(self.skia_dir, "checkout", "-q", "-B", "tmp_m156", self.base_sha)
    self._write(
        "src/core/SkDistanceFieldGen.cpp",
        "int computeSize(int w, int h) {\n  return (w + 2) * (h + 2);\n}\n",
    )
    _run_git(self.skia_dir, "add", "src/core/SkDistanceFieldGen.cpp")
    _run_git(self.skia_dir, "commit", "-m", "[M156] Divergent SDF padding")
    tip = _run_git(self.skia_dir, "rev-parse", "HEAD")
    _run_git(self.skia_dir, "update-ref", "refs/remotes/origin/chrome/m156", tip)
    _run_git(self.skia_dir, "checkout", "-q", "main")

  def revert_fix_on_main(self) -> str:
    _run_git(self.skia_dir, "checkout", "-q", "main")
    _run_git(self.skia_dir, "revert", "--no-edit", self.fix_sha)
    rev_sha = _run_git(self.skia_dir, "rev-parse", "HEAD")
    _run_git(self.skia_dir, "update-ref", "refs/remotes/origin/main", rev_sha)
    return rev_sha

  def worktree_count(self) -> int:
    out = _run_git(self.skia_dir, "worktree", "list", "--porcelain")
    return sum(1 for ln in out.splitlines() if ln.startswith("worktree "))

  def cleanup(self) -> None:
    self._tmp.cleanup()


class SecurityMergeRequestTest(unittest.TestCase):
  _shared_repo: SecurityMergeRepoHarness

  @classmethod
  def setUpClass(cls) -> None:
    super().setUpClass()
    cls._shared_repo = SecurityMergeRepoHarness()

  @classmethod
  def tearDownClass(cls) -> None:
    cls._shared_repo.cleanup()
    super().tearDownClass()

  def setUp(self) -> None:
    super().setUp()
    self.repo = self._shared_repo
    # Block live external RPCs and state-file writes even when mutation_check negates guards
    self.enterContext(mock.patch.object(w, "save_watcher_state"))
    self.enterContext(
        mock.patch.object(
            w,
            "call_buganizer_mcp_batch",
            side_effect=AssertionError("Unmocked live Buganizer MCP call in unit test"),
        )
    )
    self.enterContext(
        mock.patch.object(w, "ISSUES_CLI_BIN", "/nonexistent/issues_cli_in_unit_test")
    )
    self.enterContext(
        mock.patch.object(
            w,
            "_gerrit_http_get_json",
            side_effect=AssertionError("Unmocked live Gerrit HTTP call in unit test"),
        )
    )
    self.enterContext(
        mock.patch.object(
            w,
            "_http_get_json_direct",
            side_effect=AssertionError("Unmocked live HTTP call in unit test"),
        )
    )
    # 2026-11-03T16:22:00Z (~4 days after 2026-10-30T23:30:00Z)
    self.now_ts = 1793722920.0
    self.roll_ts = self.now_ts - 3.7 * 86400
    self.canary_ts = self.now_ts - 3.3 * 86400

  def _use_fresh_repo(self) -> None:
    fresh = SecurityMergeRepoHarness()
    self.addCleanup(fresh.cleanup)
    self.repo = fresh

  def _make_bugs_dict(self, **kwargs: str) -> dict[str, dict]:
    parsed = w.parse_buganizer_blocks(_build_parent_and_merge_blocks(**kwargs))
    return {b["bug_key"]: b for b in parsed}

  def _mock_cl_details(
      self, followup_files: list[str] | None = None
  ) -> dict[str, dict]:
    f_files = (
        ["tests/SkRemoteGlyphCacheTest.cpp"]
        if followup_files is None
        else followup_files
    )
    return {
        "1404196": {
            "ok": True,
            "cl_id": "1404196",
            "url": "https://skia-review.googlesource.com/c/skia/+/1404196",
            "host": "skia-review.googlesource.com",
            "project": "skia",
            "repo": "Skia",
            "status": "MERGED",
            "sha": self.repo.fix_sha,
            "short_sha": self.repo.fix_sha[:10],
            "subject": "Fix integer overflow in SDF generation",
            "submitted": "2026-10-29 21:48:13.000000000",
            "files": [
                "src/core/SkDistanceFieldGen.cpp",
                "tests/DistanceFieldTest.cpp",
            ],
        },
        "1411716": {
            "ok": True,
            "cl_id": "1411716",
            "url": "https://skia-review.googlesource.com/c/skia/+/1411716",
            "host": "skia-review.googlesource.com",
            "project": "skia",
            "repo": "Skia",
            "status": "MERGED",
            "sha": "abcd1234567890abcd1234567890abcd12345678",
            "short_sha": "abcd123456",
            "subject": "Avoid pinning FreeType glyph bounds",
            "submitted": "2026-10-30 17:48:13.000000000",
            "files": f_files,
        },
    }

  def _default_canary_info(
      self, *, soak_start_offset_hours: float = 79.2, canary_ver: str = "159.0.8134.0"
  ) -> dict:
    soak_start_ts = self.now_ts - soak_start_offset_hours * 3600.0
    return {
        "rolled": True,
        "roll_sha": "09c8335c1136981c92d9d5a8db5e4d1752bc6328",
        "roll_cl_id": "8695043",
        "roll_ts": self.roll_ts,
        "roll_date_str": "Oct 30",
        "canary_version": canary_ver,
        "canary_ts": soak_start_ts if canary_ver else 0.0,
        "in_canary": True,
    }

  def test_detection_predicates_enforce_all_seven_fields(self) -> None:
    bugs = self._make_bugs_dict()
    self.assertFalse(w.is_security_merge_request(bugs["buganizer:990000100"]))
    for bid, ms in (("990000156", 156), ("990000157", 157), ("990000158", 158)):
      bug = bugs[f"buganizer:{bid}"]
      self.assertTrue(w.is_security_merge_request(bug))
      self.assertEqual(w.extract_merge_milestone(bug["title"]), ms)
      self.assertEqual(bug["parent_issue_id"], "990000100")

    self.assertFalse(
        w.is_security_merge_request(
            self._make_bugs_dict(reporter="someone@google.com")["buganizer:990000156"]
        )
    )
    self.assertFalse(
        w.is_security_merge_request(
            self._make_bugs_dict(merge_val="Approved")["buganizer:990000156"]
        )
    )
    self.assertFalse(
        w.is_security_merge_request(
            self._make_bugs_dict(auto_cp="No")["buganizer:990000156"]
        )
    )
    self.assertFalse(
        w.is_security_merge_request(
            self._make_bugs_dict(assignee="merges@chromium.org")["buganizer:990000156"]
        )
    )
    self.assertFalse(
        w.is_security_merge_request(
            self._make_bugs_dict(comp_id="1457031")["buganizer:990000156"]
        )
    )
    no_parent = dict(bugs["buganizer:990000156"], parent_issue_id="")
    self.assertFalse(w.is_security_merge_request(no_parent))
    bad_title = dict(bugs["buganizer:990000156"], title="Overflow in SkDistanceFieldGen")
    self.assertFalse(w.is_security_merge_request(bad_title))
    closed_bug = dict(bugs["buganizer:990000156"], status="FIXED")
    self.assertFalse(w.is_security_merge_request(closed_bug))

  def _evaluate(
      self,
      bugs: dict[str, dict],
      *,
      followup_files: list[str] | None = None,
      soak_hours: float = 79.2,
  ) -> dict[str, dict]:
    cl_map = self._mock_cl_details(followup_files=followup_files)
    canary = self._default_canary_info(soak_start_offset_hours=soak_hours)
    with (
        mock.patch.object(
            w, "_fetch_merge_cl_detail", side_effect=lambda _h, cid: cl_map.get(cid, {})
        ),
        mock.patch.object(w, "_fetch_skia_roll_and_canary", return_value=canary),
        mock.patch.object(w, "_refresh_skia_merge_refs"),
    ):
      return w.evaluate_security_merge_groups(
          bugs, skia_dir=self.repo.skia_dir, now_ts=self.now_ts
      )

  def test_all_gates_pass_groups_siblings_and_formats_comment(self) -> None:
    groups = self._evaluate(self._make_bugs_dict())

    self.assertEqual(list(groups.keys()), ["990000100"])
    grp = groups["990000100"]
    self.assertTrue(grp["hard_gates_ok"])
    self.assertEqual(grp["milestones"], [156, 157, 158])
    self.assertEqual(grp["button_label"], "Подтвердить merge M156/M157/M158")
    self.assertEqual(grp["blocking_reasons"], [])
    self.assertEqual(grp["os_warning"], "")
    self.assertEqual(self.repo.worktree_count(), 1)

    draft = grp["comment_draft"]
    self.assertIn(
        f"Fix: https://skia-review.googlesource.com/c/skia/+/1404196 "
        f"(Skia {self.repo.fix_sha[:10]}).",
        draft,
    )
    self.assertIn("The test-only follow-up 1411716 does not need a merge.", draft)
    self.assertIn(
        "It rolled into Chromium on Oct 30 and has soaked in Canary for 3 days "
        "without known issues.",
        draft,
    )
    self.assertIn("OS list is correct.", draft)
    self.assertIn("Regression risk is low: ", draft)
    self.assertIn(
        "It cherry-picks cleanly onto Skia chrome/m156, m157, and m158. "
        "Requesting merge approval.",
        draft,
    )

  def test_gate1_empty_fixed_by_code_changes_blocks_button(self) -> None:
    grp = self._evaluate(self._make_bugs_dict(fixed_by=""))["990000100"]
    self.assertFalse(grp["hard_gates_ok"])
    self.assertFalse(grp["gates"]["fixed_by_cls"]["ok"])
    self.assertTrue(
        any("Fixed By Code Changes" in r for r in grp["blocking_reasons"]),
        grp["blocking_reasons"],
    )

  def test_gate2_followup_touching_production_code_blocks_button(self) -> None:
    grp = self._evaluate(
        self._make_bugs_dict(),
        followup_files=["src/core/SkGlyph.cpp", "tests/SkRemoteGlyphCacheTest.cpp"],
    )["990000100"]
    self.assertFalse(grp["hard_gates_ok"])
    self.assertFalse(grp["gates"]["test_only_followups"]["ok"])
    self.assertTrue(
        any("1411716" in r and "SkGlyph.cpp" in r for r in grp["blocking_reasons"]),
        grp["blocking_reasons"],
    )

  def test_gate2_empty_followup_file_list_fails_closed(self) -> None:
    grp = self._evaluate(self._make_bugs_dict(), followup_files=[])["990000100"]
    self.assertFalse(grp["hard_gates_ok"])

  def test_gate3_canary_soak_under_24h_shows_wait_until(self) -> None:
    grp = self._evaluate(self._make_bugs_dict(), soak_hours=10.0)["990000100"]
    self.assertFalse(grp["hard_gates_ok"])
    self.assertFalse(grp["gates"]["canary_soak"]["ok"])
    self.assertTrue(
        any("wait until " in r for r in grp["blocking_reasons"]),
        grp["blocking_reasons"],
    )

  def test_gate4_revert_on_skia_main_blocks_button(self) -> None:
    self._use_fresh_repo()
    rev_sha = self.repo.revert_fix_on_main()
    grp = self._evaluate(self._make_bugs_dict())["990000100"]
    self.assertFalse(grp["hard_gates_ok"])
    self.assertFalse(grp["gates"]["no_revert"]["ok"])
    self.assertTrue(
        any(rev_sha[:10] in r for r in grp["blocking_reasons"]),
        grp["blocking_reasons"],
    )

  def test_gate5_cherry_pick_conflict_lists_conflicting_files_and_cleans_worktree(
      self,
  ) -> None:
    self._use_fresh_repo()
    self.repo.add_conflict_on_m156()
    grp = self._evaluate(self._make_bugs_dict())["990000100"]
    self.assertFalse(grp["hard_gates_ok"])
    self.assertFalse(grp["gates"]["cherry_pick"]["ok"])
    self.assertTrue(
        any(
            "m156" in r and "src/core/SkDistanceFieldGen.cpp" in r
            for r in grp["blocking_reasons"]
        ),
        grp["blocking_reasons"],
    )
    self.assertEqual(self.repo.worktree_count(), 1)

  def test_gate6_os_mismatch_warns_without_blocking_button(self) -> None:
    grp = self._evaluate(self._make_bugs_dict(m156_os="Android, Linux"))["990000100"]
    self.assertTrue(grp["hard_gates_ok"])
    self.assertFalse(grp["gates"]["os_match"]["ok"])
    self.assertIn("M156", grp["os_warning"])
    self.assertNotIn("OS list is correct.", grp["comment_draft"])

  def test_confirm_posts_mcp_comments_runs_safe_cc_and_never_reassigns(self) -> None:
    bugs = self._make_bugs_dict()
    groups = self._evaluate(bugs)

    fake_state = {
        "bugs": bugs,
        "merge_request_groups": groups,
        "confirmed_merge_bugs": {},
    }
    cli_cmds: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
      del args, kwargs
      cli_cmds.append(list(cmd))
      return subprocess.CompletedProcess(
          cmd, 0, stdout="Successfully safely CC-ed jlavrova@google.com", stderr=""
      )

    with (
        mock.patch.object(w, "load_watcher_state", return_value=fake_state),
        mock.patch.object(w, "save_watcher_state"),
        mock.patch.object(
            w,
            "call_buganizer_mcp_batch",
            return_value=[
                "Successfully added comment #2 to issue http://b/990000156",
                "Successfully added comment #2 to issue http://b/990000157",
                "Successfully added comment #2 to issue http://b/990000158",
            ],
        ) as mcp_mock,
        mock.patch("subprocess.run", side_effect=fake_run),
    ):
      res = w.confirm_security_merge_group("990000100")

    self.assertTrue(res["ok"], res)
    self.assertEqual(
        res["urls"],
        [
            "https://issues.chromium.org/issues/990000156",
            "https://issues.chromium.org/issues/990000157",
            "https://issues.chromium.org/issues/990000158",
        ],
    )
    mcp_calls = mcp_mock.call_args[0][0]
    self.assertEqual(len(mcp_calls), 3)
    for (tool_name, args), expected_id in zip(
        mcp_calls, ("990000156", "990000157", "990000158")
    ):
      self.assertEqual(tool_name, "add_buganizer_comment")
      self.assertEqual(args["issueId"], expected_id)
      self.assertIn("Requesting merge approval.", args["commentMarkdown"])

    self.assertEqual(len(cli_cmds), 3)
    for cmd, expected_id in zip(cli_cmds, ("990000156", "990000157", "990000158")):
      self.assertEqual(
          cmd,
          [w.ISSUES_CLI_BIN, "mutate", "update", "safe-cc", expected_id, "jlavrova@google.com"],
      )
      joined = " ".join(cmd)
      self.assertNotIn("safe-reassign", joined)
      self.assertNotIn("bulk-update", joined)
      self.assertNotIn("--assignee", joined)

  def test_dashboard_renders_single_grouped_merge_card_and_hides_button_on_failed_gate(
      self,
  ) -> None:
    js_code = r"""
const assert = require('node:assert');
const fs = require('node:fs');
const html = fs.readFileSync(0, 'utf8');
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
assert(scriptMatch, 'Missing script tag');

const els = {};
function getEl(id) {
  if (!els[id]) els[id] = {innerHTML: '', textContent: '', classList: {add() {}}};
  return els[id];
}
global.document = {
  getElementById: getEl,
  documentElement: {classList: {add() {}}},
  addEventListener() {},
};
global.window = {parent: null, addEventListener() {}, open() {}};
global.location = {search: '', hash: ''};
global.history = {replaceState() {}};
global.localStorage = {getItem() { return null; }, setItem() {}};
global.fetch = async () => ({ok: false});
global.setInterval = () => 0;

let code = scriptMatch[1].replace(/\bboot\(\);/, '');

function runChecks() {
  cachedData = {
    last_sync: '12:00 ET',
    rollers_summary: {},
    items: {},
    bugs: {
      'buganizer:990000156': {
        bug_key: 'buganizer:990000156', id: '990000156', short_id: 'crbug/990000156',
        title: '[Merge M156] Overflow fix', project_name: 'Skia', priority: 'P1',
        severity: 'S1', status: 'ASSIGNED', lane: 'you', needs_you: true,
        is_security_merge_request: true, merge_group_parent_id: '990000100',
        url: 'https://issues.chromium.org/issues/990000156',
      },
      'buganizer:990000157': {
        bug_key: 'buganizer:990000157', id: '990000157', short_id: 'crbug/990000157',
        title: '[Merge M157] Overflow fix', project_name: 'Skia', priority: 'P1',
        severity: 'S1', status: 'ASSIGNED', lane: 'you', needs_you: true,
        is_security_merge_request: true, merge_group_parent_id: '990000100',
        url: 'https://issues.chromium.org/issues/990000157',
      },
    },
    merge_request_groups: {
      '990000100': {
        group_key: 'merge_group:990000100',
        parent_id: '990000100',
        parent_short_id: 'crbug/990000100',
        parent_url: 'https://issues.chromium.org/issues/990000100',
        parent_title: 'Overflow fix',
        project_name: 'Skia',
        priority: 'P1',
        severity: 'S1',
        milestones: [156, 157],
        milestones_label: 'M156/M157',
        button_label: 'Подтвердить merge M156/M157',
        requests: [
          {id: '990000156', short_id: 'crbug/990000156', milestone: 156,
           url: 'https://issues.chromium.org/issues/990000156', already_commented: false},
          {id: '990000157', short_id: 'crbug/990000157', milestone: 157,
           url: 'https://issues.chromium.org/issues/990000157', already_commented: false},
        ],
        all_already_commented: false,
        hard_gates_ok: true,
        blocking_reasons: [],
        os_warning: 'OS mismatch: parent vs M156',
        comment_draft: 'Fix: https://skia-review.googlesource.com/c/skia/+/1404196 ' +
          '(Skia 3d70807b95). Requesting merge approval.',
        gates: {canary_soak: {canary_version: '159.0.8134.0', soak_days: 3}},
      },
    },
  };

  setView('bugs');
  const focusPass = getEl('focus').innerHTML;
  assert.strictEqual(
    (focusPass.match(/Security merge request/g) || []).length,
    1,
    'Must render a single grouped merge card instead of duplicate sibling cards'
  );
  assert(focusPass.includes('Подтвердить merge M156/M157'), 'Must render grouped confirm button');
  assert(focusPass.includes('OS mismatch: parent vs M156'), 'Must surface OS mismatch warning');
  assert(getEl('headline').innerHTML.includes('One thing needs you.'),
    'Sibling merge bugs must count as 1 grouped item: ' + getEl('headline').innerHTML);
  assert(getEl('view-switch').innerHTML.includes('● 1'),
    'Bugs tab badge must count grouped merge card once: ' + getEl('view-switch').innerHTML);

  cachedData.merge_request_groups['990000100'].all_already_commented = true;
  render();
  const focusDone = getEl('focus').innerHTML;
  assert(focusDone.includes('merges@chromium.org'), 'Commented group must prompt manual reassign');
  assert(!focusDone.includes('confirmMerge('), 'Commented group must hide confirmMerge button');

  cachedData.merge_request_groups['990000100'].all_already_commented = false;
  cachedData.merge_request_groups['990000100'].hard_gates_ok = false;
  cachedData.merge_request_groups['990000100'].blocking_reasons = [
    'Canary soak under 24h (8h) — wait until Nov 04, 14:30 ET',
  ];
  render();
  const focusFail = getEl('focus').innerHTML;
  assert(!focusFail.includes('confirmMerge('), 'Failed gate must not render confirmMerge button');
  assert(!focusFail.includes('Подтвердить merge'), 'Failed gate must hide confirm label');
  assert(
    focusFail.includes('wait until Nov 04, 14:30 ET'),
    'Failed gate must show wait until time'
  );
}

eval(code + '\n;(' + String(runChecks) + ')();');
"""
    res = subprocess.run(
        ["node", "-e", js_code],
        input=w.DASHBOARD_HTML,
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(
        res.returncode, 0, f"Node stdout: {res.stdout}\nStderr: {res.stderr}"
    )

  def test_confirm_refuses_failed_gates_and_skips_already_commented_siblings(
      self,
  ) -> None:
    blocked_state = {
        "bugs": {},
        "merge_request_groups": {
            "990000100": {
                "parent_id": "990000100",
                "hard_gates_ok": False,
                "comment_draft": "Fix: ...",
                "requests": [{"id": "990000156", "already_commented": False}],
            }
        },
    }
    with mock.patch.object(w, "load_watcher_state", return_value=blocked_state):
      denied = w.confirm_security_merge_group("990000100")
    self.assertFalse(denied["ok"])

    partial_state = {
        "bugs": {},
        "confirmed_merge_bugs": {"990000156": {"ts": 1.0, "parent_id": "990000100"}},
        "merge_request_groups": {
            "990000100": {
                "parent_id": "990000100",
                "hard_gates_ok": True,
                "comment_draft": "Fix: https://skia-review.googlesource.com/c/skia/+/1404196.",
                "requests": [
                    {
                        "id": "990000156",
                        "url": "https://issues.chromium.org/issues/990000156",
                        "already_commented": True,
                    },
                    {
                        "id": "990000157",
                        "url": "https://issues.chromium.org/issues/990000157",
                        "already_commented": False,
                    },
                ],
            }
        },
    }
    with (
        mock.patch.object(w, "load_watcher_state", return_value=partial_state),
        mock.patch.object(w, "save_watcher_state"),
        mock.patch.object(
            w,
            "call_buganizer_mcp_batch",
            return_value=["Successfully added comment #2"],
        ) as mcp_mock,
        mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout="ok", stderr=""),
        ),
    ):
      retry_res = w.confirm_security_merge_group("990000100")
    self.assertTrue(retry_res["ok"])
    self.assertEqual(retry_res["commented_ids"], ["990000157"])
    self.assertEqual(len(mcp_mock.call_args[0][0]), 1)
    self.assertEqual(mcp_mock.call_args[0][0][0][1]["issueId"], "990000157")

  def test_sync_bugs_fetches_missing_parent_and_skips_auto_triage_chat(self) -> None:
    blocks = _build_parent_and_merge_blocks().split("\n---\n\n")
    parent_block = blocks[0]
    child_blocks = "\n---\n\n".join(blocks[1:])
    cl_map = self._mock_cl_details()
    mcp_queries: list[str] = []

    def fake_mcp(calls, **_kwargs):
      outs: list[str] = []
      for tool, args in calls:
        if tool == "get_bugs_with_external":
          q = args.get("query", "")
          mcp_queries.append(q)
          if "status:open" in q and "-status:open" not in q:
            outs.append(child_blocks)
          elif "id:990000100" in q:
            outs.append(parent_block)
          else:
            outs.append("")
        else:
          outs.append("")
      return outs

    state: dict = {"items": {}, "bugs": {}, "events": []}
    with (
        mock.patch.object(w, "call_buganizer_mcp_batch", side_effect=fake_mcp),
        mock.patch.object(w, "fetch_github_bugs", return_value=[]),
        mock.patch.object(w, "discover_bug_chats", return_value={}),
        mock.patch.object(w, "load_project_names", return_value={}),
        mock.patch.object(
            w, "_fetch_merge_cl_detail", side_effect=lambda _h, cid: cl_map.get(cid, {})
        ),
        mock.patch.object(
            w, "_fetch_skia_roll_and_canary", return_value=self._default_canary_info()
        ),
        mock.patch.object(w, "_refresh_skia_merge_refs"),
        mock.patch.object(w, "_start_chat_for_bug") as start_mock,
        mock.patch.object(w, "ANTIGRAVITY_SDK_CLI", "/nonexistent/sdk_cli"),
    ):
      w.sync_bugs_into_state(
          state, force=True, wake_chats=True, skia_dir=self.repo.skia_dir
      )

    self.assertTrue(any("id:990000100" in q for q in mcp_queries), mcp_queries)
    self.assertIn("990000100", state.get("merge_request_groups", {}))
    start_mock.assert_not_called()


if __name__ == "__main__":
  unittest.main()
