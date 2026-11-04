#!/usr/bin/env python3
"""Tests for Flight Deck reviewer ping detection and clipboard message generation."""

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

  def __init__(self, data: dict):
    self._raw = (")]}'\n" + json.dumps(data)).encode("utf-8")

  def read(self, *args, **kwargs):
    return self._raw

  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc_val, exc_tb):
    return False


def _make_gerrit_payload(
    cl_id: str,
    subject: str,
    owner_email: str,
    current_rev: str,
    revisions: dict,
    messages: list,
    attention_email: str = "jlavrova@google.com",
) -> dict:
  return {
      "id": f"skia~{cl_id}",
      "project": "skia",
      "status": "NEW",
      "work_in_progress": False,
      "subject": subject,
      "owner": {"email": owner_email, "name": owner_email.split("@")[0]},
      "current_revision": current_rev,
      "revisions": revisions,
      "attention_set": {
          "1": {"account": {"email": attention_email}},
      },
      "reviewers": {
          "REVIEWER": [
              {"email": "jlavrova@google.com"},
              {"email": "alexisdavidc@google.com"},
          ]
      },
      "labels": {
          "Code-Review": {
              "all": [
                  {"email": "jlavrova@google.com", "value": 1},
                  {"email": "alexisdavidc@google.com", "value": 0},
              ]
          }
      },
      "messages": messages,
  }


class TestReviewerPings(unittest.TestCase):

  def test_gerrit_lost_plus_one_on_rebase_detected_with_inter_patchset_diff_url(self):
    payload = _make_gerrit_payload(
        cl_id="1408436",
        subject="[skparagraph] Do not make invalid cluster ranges in justify",
        owner_email="I@YeungKC.com",
        current_rev="rev3",
        revisions={
            "rev1": {"_number": 1, "kind": "REWORK"},
            "rev2": {"_number": 2, "kind": "REWORK"},
            "rev3": {"_number": 3, "kind": "TRIVIAL_REBASE"},
        },
        messages=[
            {
                "_revision_number": 1,
                "author": {"email": "alexisdavidc@google.com"},
                "message": "Patch Set 1: Code-Review+1",
            },
            {
                "_revision_number": 2,
                "author": {"email": "I@YeungKC.com"},
                "message": (
                    "Uploaded patch set 2.\n\n"
                    "Outdated Votes:\n* Code-Review+1 by alexisdavidc@google.com\n"
                ),
            },
            {
                "_revision_number": 2,
                "author": {"email": "I@YeungKC.com"},
                "message": "Patch Set 2:\n\nRebased on main. No code change.",
            },
            {
                "_revision_number": 2,
                "author": {"email": "jlavrova@google.com"},
                "message": "Patch Set 2: Code-Review+1",
            },
            {
                "_revision_number": 3,
                "author": {"email": "alexisdavidc@google.com"},
                "message": "Patch Set 3: Patch Set 2 was rebased on behalf of I@YeungKC.com",
            },
        ],
    )
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status(
          "1408436", {}, [], preferred_host="skia-review.googlesource.com"
      )

    self.assertTrue(res["ok"])
    pings = res.get("reviewer_pings") or []
    alexis_pings = [p for p in pings if p["reviewer"] == "alexisdavidc@google.com"]
    self.assertEqual(len(alexis_pings), 1)
    ping = alexis_pings[0]
    self.assertEqual(ping["reason"], "lost_on_rebase")
    self.assertEqual(ping["prev_ps"], 1)
    self.assertEqual(ping["current_ps"], 3)
    self.assertTrue(ping["had_approval"])
    self.assertIn("/1..3", ping["diff_url"])
    self.assertFalse(ping["in_attention_set"])

  def test_gerrit_updated_after_approval_and_off_attention_set_detected(self):
    payload = _make_gerrit_payload(
        cl_id="1406297",
        subject="[skparagraph] Leave room for the soft hyphen when justifying",
        owner_email="dbebawy@gmail.com",
        current_rev="rev2",
        revisions={
            "rev1": {"_number": 1, "kind": "REWORK"},
            "rev2": {"_number": 2, "kind": "REWORK"},
        },
        messages=[
            {
                "_revision_number": 1,
                "author": {"email": "alexisdavidc@google.com"},
                "message": "Patch Set 1: Code-Review+1",
            },
            {
                "_revision_number": 2,
                "author": {"email": "dbebawy@gmail.com"},
                "message": (
                    "Uploaded patch set 2.\n\n"
                    "Fold in the ideographic fix and test from 1406377"
                ),
            },
            {
                "_revision_number": 2,
                "author": {"email": "jlavrova@google.com"},
                "message": "Patch Set 2: Code-Review+1",
            },
        ],
    )
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status(
          "1406297", {}, [], preferred_host="skia-review.googlesource.com"
      )

    self.assertTrue(res["ok"])
    pings = res.get("reviewer_pings") or []
    alexis_pings = [p for p in pings if p["reviewer"] == "alexisdavidc@google.com"]
    self.assertEqual(len(alexis_pings), 1)
    ping = alexis_pings[0]
    self.assertEqual(ping["reason"], "updated_after_approval")
    self.assertEqual(ping["prev_ps"], 1)
    self.assertEqual(ping["current_ps"], 2)
    self.assertTrue(ping["had_approval"])
    self.assertIn("/1..2", ping["diff_url"])
    self.assertFalse(ping["in_attention_set"])

  def test_gerrit_needs_second_approval_detected_when_self_already_approved(self):
    payload = _make_gerrit_payload(
        cl_id="1409637",
        subject="Support the Expat 2.8 and 2.9 APIs",
        owner_email="myid.shin@igalia.com",
        current_rev="rev1",
        revisions={
            "rev1": {"_number": 1, "kind": "REWORK"},
        },
        messages=[
            {
                "_revision_number": 1,
                "author": {"email": "jlavrova@google.com"},
                "message": "Patch Set 1: Code-Review+1",
            },
        ],
        attention_email="myid.shin@igalia.com",
    )
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status(
          "1409637", {}, [], preferred_host="skia-review.googlesource.com"
      )

    self.assertTrue(res["ok"])
    pings = res.get("reviewer_pings") or []
    alexis_pings = [p for p in pings if p["reviewer"] == "alexisdavidc@google.com"]
    self.assertEqual(len(alexis_pings), 1)
    ping = alexis_pings[0]
    self.assertEqual(ping["reason"], "needs_second_approval")
    self.assertIsNone(ping["prev_ps"])
    self.assertFalse(ping["had_approval"])
    self.assertEqual(ping["current_ps"], 1)
    self.assertFalse(ping["in_attention_set"])

  def test_gerrit_explicit_minus_two_or_wip_emits_no_reviewer_pings(self):
    payload = {
        "status": "NEW",
        "work_in_progress": True,
        "subject": "WIP: Do something",
        "owner": {"email": "I@YeungKC.com"},
        "current_revision": "rev1",
        "revisions": {"rev1": {"_number": 1}},
        "reviewers": {"REVIEWER": [{"email": "alexisdavidc@google.com"}]},
        "labels": {},
        "messages": [],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status(
          "1400000", {}, [], preferred_host="skia-review.googlesource.com"
      )

    self.assertEqual(res.get("reviewer_pings"), [])

  def test_gerrit_unapproved_external_cl_emits_no_reviewer_pings(self):
    payload = _make_gerrit_payload(
        cl_id="7686597",
        subject="[Extensions] Clean up stale comment",
        owner_email="kschmi@microsoft.com",
        current_rev="rev1",
        revisions={
            "rev1": {"_number": 1, "kind": "REWORK"},
        },
        messages=[],
    )
    payload["labels"]["Code-Review"]["all"] = [
        {"email": "jlavrova@google.com", "value": 0},
    ]
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status(
          "7686597", {}, [], preferred_host="chromium-review.googlesource.com"
      )

    self.assertTrue(res["ok"])
    self.assertEqual(res.get("reviewer_pings"), [])

  def test_js_grouped_reviewer_ping_panel_and_clipboard_message_format(self):
    # Pass DASHBOARD_HTML via stdin to stay below Linux 128 KB MAX_ARG_STRLEN
    js_code = r"""
const assert = require('assert');
const html = require('fs').readFileSync(0, 'utf8');
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
assert(scriptMatch, 'Script block found');
const scriptContent = scriptMatch[1].split('\nboot();')[0];

// Provide DOM mocks
let clipboardText = '';
const mockNav = {
  clipboard: {
    writeText: async (t) => { clipboardText = t; }
  }
};
global.navigator = mockNav;
global.window = {};
global.location = { hash: '' };
global.document = {
  getElementById: (id) => ({ innerHTML: '', textContent: '', style: {} }),
  createElement: () => ({ select: () => {}, style: {} }),
  body: { appendChild: () => {}, removeChild: () => {} },
  execCommand: () => true,
  addEventListener: () => {}
};

eval(scriptContent);

const testItems = [
  {
    item_key: 'gerrit:1408436',
    target_id: '1408436',
    project_name: 'SkParagraph',
    subject: '[skparagraph] Do not make invalid cluster ranges in justify',
    url: 'https://skia-review.googlesource.com/c/skia/+/1408436',
    reviewer_pings: [
      {
        reviewer: 'alexisdavidc@google.com',
        reviewer_name: 'Alexis Cruz-Ayala',
        reason: 'lost_on_rebase',
        prev_ps: 1,
        current_ps: 3,
        had_approval: true,
        in_attention_set: false,
        diff_url: 'https://skia-review.googlesource.com/c/skia/+/1408436/1..3'
      }
    ]
  },
  {
    item_key: 'gerrit:1406297',
    target_id: '1406297',
    project_name: 'SkParagraph',
    subject: '[skparagraph] Leave room for the soft hyphen when justifying',
    url: 'https://skia-review.googlesource.com/c/skia/+/1406297',
    reviewer_pings: [
      {
        reviewer: 'alexisdavidc@google.com',
        reviewer_name: 'Alexis Cruz-Ayala',
        reason: 'updated_after_approval',
        prev_ps: 1,
        current_ps: 2,
        had_approval: true,
        in_attention_set: false,
        diff_url: 'https://skia-review.googlesource.com/c/skia/+/1406297/1..2'
      }
    ]
  },
  {
    item_key: 'gerrit:1409637',
    target_id: '1409637',
    project_name: 'Skia',
    subject: 'Support the Expat 2.8 and 2.9 APIs',
    url: 'https://skia-review.googlesource.com/c/skia/+/1409637',
    reviewer_pings: [
      {
        reviewer: 'alexisdavidc@google.com',
        reviewer_name: 'Alexis Cruz-Ayala',
        reason: 'needs_second_approval',
        prev_ps: null,
        current_ps: 1,
        had_approval: false,
        in_attention_set: false,
        diff_url: 'https://skia-review.googlesource.com/c/skia/+/1409637'
      }
    ]
  },
  {
    item_key: 'github:195384',
    target_id: '195384',
    kind: 'github',
    project_name: 'Flutter',
    subject: 'Map code units without a cluster to a cluster',
    url: 'https://github.com/flutter/flutter/pull/195384',
    reviewer_pings: [
      {
        reviewer: 'mdebbar',
        reviewer_name: 'Mouad Debbar',
        reason: 'updated_after_review',
        prev_ps: null,
        current_ps: 1,
        had_approval: false,
        in_attention_set: false,
        auto_rerequested: false,
        diff_url: 'https://github.com/flutter/flutter/pull/195384/files'
      }
    ]
  }
];

const groups = collectReviewerPingGroups(testItems);
assert.strictEqual(groups.length, 2);
assert.strictEqual(groups[0].reviewer, 'alexisdavidc@google.com');
assert.strictEqual(groups[0].items.length, 3);
assert.strictEqual(groups[1].reviewer, 'mdebbar');
assert.strictEqual(groups[1].items.length, 1);

const msg = buildReviewerPingMessage('alexisdavidc@google.com', testItems);
assert(msg.includes('Lost +1 after rebase'), 'Lost +1 header present: ' + msg);
assert(msg.includes('1408436/1..3'), 'Delta diff 1..3 present: ' + msg);
assert(msg.includes('Updated since your +1'), 'Updated header present: ' + msg);
assert(msg.includes('1406297/1..2'), 'Delta diff 1..2 present: ' + msg);
assert(msg.includes('Waiting for a second +1'), 'Second +1 header present: ' + msg);
assert(msg.includes('Support the Expat 2.8 and 2.9 APIs'), 'Subject present: ' + msg);

// Test exclusion
togglePingItem('alexisdavidc@google.com', 'gerrit:1408436', false);
const filteredMsg = buildReviewerPingMessage('alexisdavidc@google.com', testItems);
assert(!filteredMsg.includes('1408436/1..3'), 'Excluded item dropped from message: ' + filteredMsg);
assert(filteredMsg.includes('1406297/1..2'), 'Remaining item preserved: ' + filteredMsg);

// Test renderPingsSection HTML structure
const rendered = renderPingsSection(testItems);
assert(!rendered.includes('<label class="ping-row'), 'Must not wrap ping-row in label');
assert(rendered.includes('<div class="ping-row'), 'Must wrap ping-row in div');
assert(rendered.includes('class="ping-cl-link"'), 'CL link class present');
assert(rendered.includes('class="ping-subj"'), 'Subject link class present');
assert(rendered.includes('Diff PS1..3 ↗'), 'Diff link present on PS delta');
assert(rendered.includes('Open CL ↗'), 'Open CL link present');
assert(rendered.includes('https://chat.google.com/'), 'Open Google Chat link present');
assert(rendered.includes('class="ping-why"'), 'Context why line present');
assert(rendered.includes('🔕 no re-request'), 'Must show no re-request badge on GitHub');

const inlineRerequested = reviewerPingInlineHtml({
  kind: 'github',
  reviewer_pings: [{
    reviewer: 'mdebbar',
    reason: 'updated_after_review',
    in_attention_set: true,
    auto_rerequested: true
  }]
});
assert(inlineRerequested.includes('🔄 re-requested'), 'Must show re-requested badge on GitHub');

console.log('JS tests passed successfully');
process.exit(0);
"""
    cmd = ["node", "-e", js_code]
    res = subprocess.run(
        cmd, input=w.DASHBOARD_HTML, capture_output=True, text=True, check=False
    )
    self.assertEqual(res.returncode, 0, f"Node output: {res.stdout}\nError: {res.stderr}")

  def test_github_dismissed_approval_and_missing_rerequest_detected(self):
    gh_payload = {
        "number": 195384,
        "title": "Map code units without a cluster to a cluster",
        "body": "Fixes #195000",
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": "ColdPaleLight"},
        "mergedAt": None,
        "headRefOid": "abc1234567890",
        "headRefName": "map-cluster",
        "reviewDecision": "REVIEW_REQUIRED",
        "latestReviews": [
            {
                "author": {"login": "Rusino"},
                "state": "DISMISSED",
                "authorAssociation": "MEMBER",
            },
            {
                "author": {"login": "mdebbar"},
                "state": "COMMENTED",
                "authorAssociation": "MEMBER",
            },
        ],
        "reviews": [
            {
                "author": {"login": "Rusino"},
                "state": "APPROVED",
                "submittedAt": "2026-10-30T10:00:00Z",
            },
            {
                "author": {"login": "mdebbar"},
                "state": "COMMENTED",
                "submittedAt": "2026-10-30T11:00:00Z",
            },
            {
                "author": {"login": "Rusino"},
                "state": "DISMISSED",
                "submittedAt": "2026-10-30T12:00:00Z",
            },
        ],
        "comments": [],
        "commits": [
            {
                "committedDate": "2026-10-30T13:00:00Z",
                "messageHeadline": "Address feedback from Mouad",
            }
        ],
        "statusCheckRollup": [],
        "labels": [],
        "url": "https://github.com/flutter/flutter/pull/195384",
        "mergeable": "MERGEABLE",
        "reviewRequests": [],
        "createdAt": "2026-10-29T10:00:00Z",
        "updatedAt": "2026-10-30T13:05:00Z",
        "closedAt": None,
    }
    with mock.patch.object(
        subprocess, "check_output", return_value=json.dumps(gh_payload)
    ):
      res = w.fetch_github_pr_status("195384")

    self.assertTrue(res["ok"])
    self.assertFalse(res["in_my_attention"])
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")
    pings = res.get("reviewer_pings") or []
    mdebbar_pings = [p for p in pings if p["reviewer"] == "mdebbar"]
    self.assertEqual(len(mdebbar_pings), 1)
    ping = mdebbar_pings[0]
    self.assertEqual(ping["reason"], "updated_after_review")
    self.assertFalse(ping["in_attention_set"])
    self.assertFalse(ping["had_approval"])

  def test_auto_rerequest_github_reviewers_calls_gh_api_once_per_head_sha(self):
    pings = [
        {
            "reviewer": "mdebbar",
            "in_attention_set": False,
            "reason": "updated_after_review",
        }
    ]
    ledger = {}
    with mock.patch.object(
        w, "rerequest_github_reviewers", return_value=True
    ) as mock_req:
      req_users = w.auto_rerequest_github_reviewers(
          "195384", "abc1234", pings, ledger, do_request=True
      )
      self.assertEqual(req_users, ["mdebbar"])
      mock_req.assert_called_once_with("195384", ["mdebbar"])
      self.assertTrue(pings[0]["auto_rerequested"])
      self.assertTrue(pings[0]["in_attention_set"])
      self.assertIn("195384:abc1234:mdebbar", ledger)

    # Second call with the same SHA does not re-request via GitHub API
    with mock.patch.object(
        w, "rerequest_github_reviewers", return_value=True
    ) as mock_req:
      req_users = w.auto_rerequest_github_reviewers(
          "195384", "abc1234", pings, ledger, do_request=True
      )
      self.assertEqual(req_users, [])
      mock_req.assert_not_called()

  def test_rerequest_github_reviewers_runs_gh_api(self):
    with mock.patch.object(
        subprocess,
        "run",
        return_value=mock.Mock(returncode=0, stdout="{}", stderr=""),
    ) as mock_run:
      ok = w.rerequest_github_reviewers("195384", ["mdebbar"])
      self.assertTrue(ok)
      self.assertEqual(mock_run.call_count, 1)
      args = mock_run.call_args[0][0]
      self.assertIn("repos/flutter/flutter/pulls/195384/requested_reviewers", args[2])



if __name__ == "__main__":
  unittest.main()
