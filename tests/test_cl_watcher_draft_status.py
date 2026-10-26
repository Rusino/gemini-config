#!/usr/bin/env python3
"""Contract tests for draft/WIP and no-reviewer (in-development) CL/PR detection in Flight Deck."""

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


class DraftAndDevelopmentStatusTest(unittest.TestCase):

  def test_gerrit_wip_cl_marks_is_draft_and_clears_attention(self):
    payload = {
        "status": "NEW",
        "work_in_progress": True,
        "subject": "[agentic-review] Add starter Skia knowledge base rules",
        "owner": {"email": "jiameil@google.com", "name": "Jiamei"},
        "current_revision": "rev1",
        "revisions": {"rev1": {"_number": 5}},
        "attention_set": {"1": {"account": {"email": "jlavrova@google.com"}}},
        "reviewers": {"REVIEWER": [{"email": "jlavrova@google.com"}]},
        "labels": {},
        "messages": [],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status("1386096", {}, [], skia_dir="/nonexistent")

    self.assertTrue(res["ok"])
    self.assertTrue(res["is_draft"])
    self.assertFalse(res["in_development"])
    self.assertFalse(res["in_my_attention"])
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")

  def test_gerrit_self_cl_without_other_reviewers_marks_in_development(self):
    payload = {
        "status": "NEW",
        "work_in_progress": False,
        "subject": "[fontconfig] Keep alias order in matchStyle",
        "owner": {"email": "jlavrova@google.com", "name": "Julia Lavrova"},
        "current_revision": "rev3",
        "revisions": {"rev3": {"_number": 3}},
        "attention_set": {"1": {"account": {"email": "jlavrova@google.com"}}},
        "reviewers": {
            "REVIEWER": [
                {"email": "jlavrova@google.com"},
                {"email": "skcq-be@skia-corp.google.com.iam.gserviceaccount.com"},
            ]
        },
        "labels": {},
        "messages": [
            {
                "_revision_number": 3,
                "author": {
                    "email": "skcq-be@skia-corp.google.com.iam.gserviceaccount.com"
                },
                "date": "2026-10-26 15:00:00.000000000",
                "message": "Patch Set 3: Dry run: This CL passed the SkCQ dry run.",
            }
        ],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status("1405639", {}, [], skia_dir="/nonexistent")

    self.assertTrue(res["ok"])
    self.assertFalse(res["is_draft"])
    self.assertTrue(res["in_development"])
    self.assertFalse(res["in_my_attention"])
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")

  def test_gerrit_cl_with_external_reviewer_is_in_review(self):
    payload = {
        "status": "NEW",
        "work_in_progress": False,
        "subject": "[skparagraph] Fix cluster metrics",
        "owner": {"email": "jlavrova@google.com", "name": "Julia Lavrova"},
        "current_revision": "rev2",
        "revisions": {"rev2": {"_number": 2}},
        "attention_set": {"1": {"account": {"email": "jlavrova@google.com"}}},
        "reviewers": {
            "REVIEWER": [
                {"email": "jlavrova@google.com"},
                {"email": "egdaniel@google.com"},
            ]
        },
        "labels": {},
        "messages": [
            {
                "_revision_number": 2,
                "author": {"email": "egdaniel@google.com"},
                "date": "2026-10-26 15:10:00.000000000",
                "message": "Patch Set 2: LGTM with nit",
            }
        ],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status("1409999", {}, [], skia_dir="/nonexistent")

    self.assertTrue(res["ok"])
    self.assertFalse(res["is_draft"])
    self.assertFalse(res["in_development"])
    self.assertTrue(res["in_my_attention"])
    self.assertEqual(res["phase"], "NEEDS_YOUR_ACTION")

  def test_github_draft_pr_marks_is_draft(self):
    gh_json = json.dumps({
        "number": 195120,
        "title": "[web] Make word segmentation lazy in CanvasKit Client ICU",
        "state": "OPEN",
        "isDraft": True,
        "author": {"login": "Rusino"},
        "mergedAt": None,
        "headRefOid": "1122334455667788",
        "reviews": [
            {
                "author": {"login": "gemini-code-assist"},
                "state": "COMMENTED",
                "body": "Automated review",
                "submittedAt": "2026-10-26T18:00:00Z",
            }
        ],
        "comments": [],
        "statusCheckRollup": [{"name": "tree-status", "conclusion": "FAILURE"}],
        "url": "https://github.com/flutter/flutter/pull/195120",
        "mergeable": "MERGEABLE",
        "reviewRequests": [],
    })
    with mock.patch.object(
        w.subprocess, "check_output", return_value=gh_json
    ) as mock_cmd:
      res = w.fetch_github_pr_status("195120")

    self.assertIn("isDraft", mock_cmd.call_args[0][0][-1])
    self.assertTrue(res["ok"])
    self.assertTrue(res["is_draft"])
    self.assertFalse(res["in_development"])
    self.assertFalse(res["in_my_attention"])
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")

  def test_github_open_pr_without_human_reviewers_marks_in_development(self):
    gh_json = json.dumps({
        "number": 195122,
        "title": "[web_paragraph] Avoid browser text metrics queries on re-layout",
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": "Rusino"},
        "mergedAt": None,
        "headRefOid": "aabbccddeeff0011",
        "reviews": [
            {
                "author": {"login": "gemini-code-assist"},
                "state": "COMMENTED",
                "body": "Automated review",
                "submittedAt": "2026-10-26T18:00:00Z",
            },
            {
                "author": {"login": "Rusino"},
                "state": "COMMENTED",
                "body": "Self note",
                "submittedAt": "2026-10-26T18:05:00Z",
            },
        ],
        "comments": [],
        "statusCheckRollup": [],
        "url": "https://github.com/flutter/flutter/pull/195122",
        "mergeable": "MERGEABLE",
        "reviewRequests": [],
    })
    with mock.patch.object(w.subprocess, "check_output", return_value=gh_json):
      res = w.fetch_github_pr_status("195122")

    self.assertTrue(res["ok"])
    self.assertFalse(res["is_draft"])
    self.assertTrue(res["in_development"])
    self.assertFalse(res["in_my_attention"])
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")

  def test_piper_cl_distinguishes_in_development_and_in_review(self):
    no_rev_out = (
        "Change 1009999999 by jlavrova@jlavrova:ws on 2026/10/26 12:00:00\n\n"
        "\tUpdate SkParagraph tests\n\n"
        "Affected files ...\n"
    )
    with mock.patch.object(w.subprocess, "check_output", return_value=no_rev_out):
      res_dev = w.fetch_piper_cl_status("1009999999")
    self.assertFalse(res_dev["is_draft"])
    self.assertTrue(res_dev["in_development"])

    with_rev_out = (
        "Change 1002307625 by bwils@bwils:ws on 2026/10/19 06:55:40\n\n"
        "\tRemove missing project from skia check_tests\n\n"
        "\tR=egdaniel\n\n"
        "Affected files ...\n"
    )
    with mock.patch.object(w.subprocess, "check_output", return_value=with_rev_out):
      res_rev = w.fetch_piper_cl_status("1002307625")
    self.assertFalse(res_rev["is_draft"])
    self.assertFalse(res_rev["in_development"])

    tag_wip_out = (
        "Change 1008888888 by jlavrova@jlavrova:ws on 2026/10/26 12:00:00\n\n"
        "\t[skparagraph] WIP: Prototype cluster cache\n\n"
        "\tR=egdaniel\n\n"
        "Affected files ...\n"
    )
    with mock.patch.object(w.subprocess, "check_output", return_value=tag_wip_out):
      res_wip = w.fetch_piper_cl_status("1008888888")
    self.assertTrue(res_wip["is_draft"])
    self.assertFalse(res_wip["in_development"])

  def test_github_pr_with_human_timeline_comment_is_in_review_and_needs_action(self):
    gh_json = json.dumps({
        "number": 195200,
        "title": "[web_paragraph] Add line metrics test",
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": "Rusino"},
        "mergedAt": None,
        "headRefOid": "1234567890abcdef",
        "reviews": [],
        "comments": [
            {
                "author": {"login": "mdebbar"},
                "body": "Could we also test RTL here?",
                "createdAt": "2026-10-26T19:00:00Z",
            }
        ],
        "statusCheckRollup": [],
        "url": "https://github.com/flutter/flutter/pull/195200",
        "mergeable": "MERGEABLE",
        "reviewRequests": [],
    })
    with mock.patch.object(w.subprocess, "check_output", return_value=gh_json):
      res = w.fetch_github_pr_status("195200")

    self.assertFalse(res["is_draft"])
    self.assertFalse(res["in_development"])
    self.assertTrue(res["in_my_attention"])
    self.assertEqual(res["phase"], "NEEDS_YOUR_ACTION")

  def test_dashboard_js_functions_render_draft_development_review_and_cq_precedence(self):
    self.assertNotIn("In review / development", w.DASHBOARD_HTML)
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.split("initTheme();", 1)[0] + """
const draftPr = {
  kind: 'github', status: 'OPEN', phase: 'WAITING_ON_OTHERS',
  is_draft: true, in_development: false, tree_red: true,
  patchset: 1, url: 'https://example.com/1', cr_summary: '0 rev', cq_status: '🌲 Tree is red'
};
const devCl = {
  kind: 'gerrit', status: 'NEW', phase: 'WAITING_ON_OTHERS',
  is_draft: false, in_development: true,
  patchset: 3, url: 'https://example.com/2', cr_summary: '0', cq_status: '✓ Dry Run Passed'
};
const devCqRunningCl = {
  kind: 'gerrit', status: 'NEW', phase: 'WAITING_ON_OTHERS',
  is_draft: false, in_development: true,
  patchset: 3, url: 'https://example.com/3', cr_summary: '0', cq_status: 'CQ+1 (Dry Run)'
};
const reviewCl = {
  kind: 'gerrit', status: 'NEW', phase: 'WAITING_ON_OTHERS',
  is_draft: false, in_development: false,
  patchset: 2, url: 'https://example.com/4', cr_summary: '+1', cq_status: 'Idle'
};
const abandonedCl = {
  kind: 'gerrit', status: 'ABANDONED', phase: 'ABANDONED',
  is_draft: false, in_development: false, patchset: 1, url: 'https://example.com/5'
};
const closedPr = {
  kind: 'github', status: 'CLOSED', phase: 'ABANDONED',
  is_draft: false, in_development: false, patchset: 1, url: 'https://example.com/6'
};
const gmailAllTrash = {
  target_id: '101',
  gmail: {
    thread_ids: ['t1'], inbox_thread_ids: [],
    total_msgs: 5, inbox_msgs: 0, trash_msgs: 5,
    threads: [{url: 'https://mail/1', source: 'Gerrit', msgs: 5}]
  }
};
const gmailMixedTrash = {
  target_id: '102',
  gmail: {
    thread_ids: ['t2'], inbox_thread_ids: [],
    total_msgs: 5, inbox_msgs: 0, trash_msgs: 3,
    threads: [{url: 'https://mail/2', source: 'Gerrit', msgs: 5}]
  }
};
const gmailAllArchived = {
  target_id: '103',
  gmail: {
    thread_ids: ['t3'], inbox_thread_ids: [],
    total_msgs: 4, inbox_msgs: 0, trash_msgs: 0,
    threads: [{url: 'https://mail/3', source: 'Gerrit', msgs: 4}]
  }
};
console.log(JSON.stringify({
  draftRolls: formatRollsCell(draftPr),
  devRolls: formatRollsCell(devCl),
  reviewRolls: formatRollsCell(reviewCl),
  abandonedRolls: formatRollsCell(abandonedCl),
  closedRolls: formatRollsCell(closedPr),
  draftCore: statusCoreHtml(draftPr),
  devCore: statusCoreHtml(devCl),
  reviewCore: statusCoreHtml(reviewCl),
  draftStatus: statusOf(draftPr),
  devStatus: statusOf(devCl),
  devCqStatus: statusOf(devCqRunningCl),
  reviewStatus: statusOf(reviewCl),
  gmailAllTrashHtml: formatGmailCell(gmailAllTrash),
  gmailMixedTrashHtml: formatGmailCell(gmailMixedTrash),
  gmailAllArchivedHtml: formatGmailCell(gmailAllArchived),
  chipZeroInactive: chipHtml('seg', 'status', 'waiting', '⏸ Waiting on others', 0, false),
  chipZeroActive: chipHtml('seg', 'status', 'attention', '⚡ Needs you', 0, true),
  chipNonZero: chipHtml('seg', 'status', 'waiting', '⏸ Waiting on others', 2, false),
  visibleWhenAllZero: STATUSES
    .filter(st => st.alwaysShow || 0 > 0 || 'attention' === st.id)
    .map(st => st.id),
  visibleWhenDraftSelectedAtZero: STATUSES
    .filter(st => st.alwaysShow || 0 > 0 || 'draft' === st.id)
    .map(st => st.id),
  statuses: STATUSES,
}));
"""
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=True
    )
    out = json.loads(proc.stdout.strip())
    self.assertEqual(out["draftRolls"], '<span class="muted">In draft</span>')
    self.assertEqual(out["devRolls"], '<span class="muted">In development</span>')
    self.assertEqual(out["reviewRolls"], '<span class="muted">In review</span>')
    self.assertEqual(out["abandonedRolls"], '<span class="muted">—</span>')
    self.assertEqual(out["closedRolls"], '<span class="muted">—</span>')
    self.assertIn("📝", out["draftCore"])
    self.assertIn("<code>DRAFT</code>", out["draftCore"])
    self.assertIn("🛠️", out["devCore"])
    self.assertIn("In development — no reviewers yet", out["devCore"])
    self.assertIn("In review (CR: +1", out["reviewCore"])
    self.assertEqual(out["draftStatus"], "draft")
    self.assertEqual(out["devStatus"], "dev")
    self.assertEqual(out["devCqStatus"], "cq")
    self.assertEqual(out["reviewStatus"], "waiting")
    self.assertIn("(all 5 in <code>TRASH</code>)", out["gmailAllTrashHtml"])
    self.assertIn(
        "(5 msgs: 3 in <code>TRASH</code>, 2 archived)", out["gmailMixedTrashHtml"]
    )
    self.assertIn("(all 4 archived)", out["gmailAllArchivedHtml"])
    self.assertIn(" disabled", out["chipZeroInactive"])
    self.assertIn(" disabled", out["chipZeroActive"])
    self.assertNotIn(" disabled", out["chipNonZero"])
    self.assertEqual(
        out["visibleWhenAllZero"],
        ["attention", "cq", "waiting", "dev", "merged"],
    )
    self.assertEqual(
        out["visibleWhenDraftSelectedAtZero"],
        ["attention", "cq", "waiting", "dev", "draft", "merged"],
    )
    status_ById = {s["id"]: s for s in out["statuses"]}
    self.assertIn("dev", status_ById)
    self.assertIn("draft", status_ById)
    for core_id in ("attention", "dev", "cq", "waiting", "merged"):
      self.assertTrue(status_ById[core_id].get("alwaysShow"), core_id)
    for tail_id in ("draft", "conflict", "tree", "old", "abandoned"):
      self.assertFalse(status_ById[tail_id].get("alwaysShow"), tail_id)


if __name__ == "__main__":
  unittest.main()
