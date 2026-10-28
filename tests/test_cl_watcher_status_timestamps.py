#!/usr/bin/env python3
"""Contract tests for CL/PR status change timestamps in Flight Deck."""

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


class _FakeGerritResponse:

  def __init__(self, payload: dict):
    self._raw = (")]}'\n" + json.dumps(payload)).encode("utf-8")

  def read(self) -> bytes:
    return self._raw

  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc, tb):
    return False


class StatusTimestampsContractTest(unittest.TestCase):

  def test_parse_gerrit_or_iso_time_handles_piper_slash_timestamp(self):
    dt = w.parse_gerrit_or_iso_time("2026/10/26 12:00:00")
    self.assertIsNotNone(dt)
    et_fmt, age = w.format_et_and_age("2026/10/26 12:00:00")
    self.assertIn("ET", et_fmt)
    self.assertTrue(age.endswith("ago") or age == "just now")

  def test_fetch_piper_cl_status_extracts_status_changed_timestamps(self):
    raw_out = (
        "Change 1009999999 by jlavrova@jlavrova:ws on 2026/10/26 12:00:00 *pending*\n\n"
        "\tUpdate SkParagraph tests\n\n"
        "Affected files ...\n"
    )
    with mock.patch.object(w.subprocess, "check_output", return_value=raw_out):
      res = w.fetch_piper_cl_status("1009999999")
    self.assertEqual(res["status_changed_raw"], "2026/10/26 12:00:00")
    self.assertIn("ET", res["status_changed_et"])
    self.assertTrue(res["status_changed_age"])

  def test_fetch_gerrit_cl_status_extracts_status_changed_timestamps(self):
    # Attention set last_update must be preferred for needs_your_action
    payload = {
        "status": "NEW",
        "work_in_progress": False,
        "subject": "[skparagraph] Cluster metrics fix",
        "owner": {"email": "jlavrova@google.com", "name": "Julia Lavrova"},
        "current_revision": "rev1",
        "revisions": {"rev1": {"_number": 1, "created": "2026-10-26 10:00:00.000000000"}},
        "attention_set": {
            "1": {
                "account": {"email": "jlavrova@google.com"},
                "last_update": "2026-10-26 14:30:00.000000000",
            }
        },
        "reviewers": {"REVIEWER": [{"email": "egdaniel@google.com"}]},
        "labels": {},
        "messages": [
            {
                "_revision_number": 1,
                "author": {"email": "egdaniel@google.com"},
                "date": "2026-10-26 14:25:00.000000000",
                "message": "Patch Set 1: LGTM",
            }
        ],
        "updated": "2026-10-26 14:30:00.000000000",
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status("1409999", {}, [], skia_dir="/nonexistent")
    self.assertTrue(res["in_my_attention"])
    self.assertEqual(res["status_changed_raw"], "2026-10-26 14:30:00.000000000")
    self.assertIn("ET", res["status_changed_et"])
    self.assertTrue(res["status_changed_age"])

  def test_fetch_github_pr_status_extracts_status_changed_timestamps(self):
    gh_json = json.dumps({
        "number": 195120,
        "title": "[web] Lazy word segmentation",
        "state": "CLOSED",
        "isDraft": False,
        "author": {"login": "Rusino"},
        "mergedAt": None,
        "closedAt": "2026-10-26T19:45:00Z",
        "createdAt": "2026-10-26T18:00:00Z",
        "updatedAt": "2026-10-26T19:45:10Z",
        "headRefOid": "1122334455667788",
        "reviews": [],
        "comments": [],
        "statusCheckRollup": [],
        "url": "https://github.com/flutter/flutter/pull/195120",
        "mergeable": "MERGEABLE",
        "reviewRequests": [],
    })
    with mock.patch.object(w.subprocess, "check_output", return_value=gh_json):
      res = w.fetch_github_pr_status("195120")
    self.assertEqual(res["status_changed_raw"], "2026-10-26T19:45:10Z")
    self.assertIn("ET", res["status_changed_et"])

  def test_dashboard_js_functions_render_status_timestamps(self):
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.split("\nboot();", 1)[0] + """
const closedPr = {
  kind: 'github', status: 'CLOSED', phase: 'ABANDONED',
  is_draft: false, in_development: false, patchset: 1, url: 'https://example.com/6',
  status_changed_et: '15:45 ET', status_changed_age: '1h ago'
};
const devCl = {
  kind: 'gerrit', status: 'NEW', phase: 'WAITING_ON_OTHERS',
  is_draft: false, in_development: true, is_watching: false,
  patchset: 3, url: 'https://example.com/2', cr_summary: '0', cq_status: '✓ Dry Run Passed',
  status_changed_et: '16:00 ET', status_changed_age: '45m ago'
};
const reviewCl = {
  kind: 'gerrit', status: 'NEW', phase: 'WAITING_ON_OTHERS',
  is_draft: false, in_development: false, is_watching: false,
  patchset: 2, url: 'https://example.com/4', cr_summary: '+1', cq_status: 'Idle',
  status_changed_et: '16:15 ET', status_changed_age: '30m ago'
};
const mergedCl = {
  kind: 'gerrit', status: 'MERGED', phase: 'READY_TO_CLOSE',
  revision: '1234567890abcdef', patchset: 4, url: 'https://example.com/5',
  status_changed_et: '14:00 ET', status_changed_age: '2h ago',
  submitted_et: '14:00 ET', submitted_age: '2h ago',
  post_submit: {
    all_passed: true,
    status_changed_et: '14:20 ET', status_changed_age: '1h 40m ago'
  }
};
const rollingCl = Object.assign({}, mergedCl, {
  phase: 'POST_SUBMIT_ROLLING', status_changed_et: '', status_changed_age: '',
  submitted_ts: Date.now() / 1000 - 7200,
  post_submit: {all_passed: false, rolls: {}}
});
console.log(JSON.stringify({
  closedState: stateHtml(closedPr),
  closedTime: rowTime(closedPr),
  devTime: rowTime(devCl),
  reviewTime: rowTime(reviewCl),
  mergedState: stateHtml(mergedCl),
  mergedTime: rowTime(mergedCl),
  rollingTime: rowTime(rollingCl),
  rollingLane: laneOf(rollingCl),
}));
"""
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=True
    )
    out = json.loads(proc.stdout.strip())
    self.assertEqual(out["closedState"], "Closed")
    self.assertEqual(out["closedTime"], "15:45 ET (1h ago)")
    self.assertEqual(out["devTime"], "16:00 ET (45m ago)")
    self.assertEqual(out["reviewTime"], "16:15 ET (30m ago)")
    self.assertEqual(out["mergedState"], "Shipped to all 4 products")
    self.assertEqual(out["mergedTime"], "14:20 ET (1h 40m ago)")
    self.assertEqual(out["rollingTime"], "14:00 ET (2h ago)")
    self.assertEqual(out["rollingLane"], "landing")

if __name__ == "__main__":
  unittest.main()
