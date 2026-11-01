#!/usr/bin/env python3
"""Contract tests for self-reviewed CL/PR status, CC reviewer detection, and waiting lanes."""

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


class SelfReviewedStatusTest(unittest.TestCase):

  def test_gerrit_cc_comment_review_detects_self_review_and_routes_to_waiting_on_others(
      self,
  ):
    payload = {
        "status": "NEW",
        "hashtags": ["wip"],
        "subject": "Add README.mds across codebase",
        "owner": {"email": "jmbetancourt@google.com", "name": "John Betancourt"},
        "current_revision": "rev2",
        "revisions": {"rev2": {"_number": 2}},
        "attention_set": {
            "1": {"account": {"email": "jmbetancourt@google.com"}},
        },
        "reviewers": {
            "REVIEWER": [],
            "CC": [{"email": "jlavrova@google.com", "name": "Julia Lavrova"}],
        },
        "labels": {},
        "messages": [
            {
                "_revision_number": 2,
                "author": {"email": "jlavrova@google.com"},
                "date": "2026-10-30 20:17:00.000000000",
                "message": "Patch Set 2:\n\n(25 comments)",
            }
        ],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status("1411816", {}, [], skia_dir="/nonexistent")

    self.assertTrue(res["ok"])
    self.assertFalse(res["is_self"])
    self.assertEqual(res["role"], "reviewer (goog)")
    self.assertEqual(res["self_review_state"], "commented")
    self.assertEqual(res["self_review_ps"], 2)
    self.assertEqual(res["self_comment_count"], 25)
    self.assertEqual(res["assigned_reviewers_count"], 1)
    self.assertEqual(res["missing_reviewers_to_assign"], 0)
    self.assertFalse(res["in_my_attention"])
    self.assertFalse(res["is_watching"])
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")

  def test_github_comment_review_detects_self_review_and_routes_to_waiting_on_others(
      self,
  ):
    gh_json = json.dumps({
        "number": 195500,
        "title": "[web] Optimize line metrics cache",
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": "mdebbar"},
        "mergedAt": None,
        "headRefOid": "feedfacecafe0011",
        "reviews": [
            {
                "author": {"login": "Rusino"},
                "state": "COMMENTED",
                "body": "Left comments on text metrics caching.",
                "submittedAt": "2026-10-28T14:00:00Z",
            }
        ],
        "comments": [],
        "commits": [
            {
                "authoredDate": "2026-10-28T14:30:00Z",
                "committedDate": "2026-10-28T14:30:00Z",
                "authors": [{"login": "mdebbar", "email": "mdebbar@google.com"}],
                "messageHeadline": "Address review comments",
                "oid": "feedfacecafe0012",
            }
        ],
        "statusCheckRollup": [],
        "url": "https://github.com/flutter/flutter/pull/195500",
        "mergeable": "MERGEABLE",
        "reviewRequests": [],
    })
    with mock.patch.object(w.subprocess, "check_output", return_value=gh_json):
      res = w.fetch_github_pr_status("195500")

    self.assertTrue(res["ok"])
    self.assertFalse(res["is_self"])
    self.assertEqual(res["role"], "reviewer")
    self.assertEqual(res["self_review_state"], "commented")
    self.assertEqual(res["self_comment_count"], 1)
    self.assertFalse(res["is_watching"])
    self.assertFalse(res["in_my_attention"])
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")

  def test_dashboard_js_renders_self_review_badges_and_waiting_on_author_state(self):
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.split("\nboot();", 1)[0] + """
const reviewedCl = {
  kind: 'gerrit', target_id: '1411816', status: 'NEW', phase: 'WAITING_ON_OTHERS',
  is_self: false, is_external: false, role: 'reviewer (goog)',
  is_draft: false, in_development: false, is_watching: false,
  owner_name: 'John Betancourt', owner_email: 'jmbetancourt@google.com',
  subject: 'Add README.mds across codebase',
  patchset: 2, url: 'https://skia-review.googlesource.com/c/skia/+/1411816',
  self_review_state: 'commented', self_review_ps: 2, self_comment_count: 25,
  waiting_on: []
};
const updatedAfterReviewCl = Object.assign({}, reviewedCl, {
  patchset: 3
});
const approvedCl = {
  kind: 'gerrit', target_id: '1402416', status: 'NEW', phase: 'WAITING_ON_OTHERS',
  is_self: false, is_external: false, role: 'reviewer (goog)',
  is_draft: false, in_development: false, is_watching: true,
  owner_name: 'Alexis Cruz-Ayala', owner_email: 'alexisdavidc@google.com',
  subject: 'Reland SkGraphics calls',
  patchset: 8, url: 'https://skia-review.googlesource.com/c/skia/+/1402416',
  self_review_state: 'approved', self_review_ps: 6, self_approved: true,
  self_approval_ps: 6, waiting_on: []
};

console.log(JSON.stringify({
  reviewedLane: laneOf(reviewedCl),
  reviewedStatus: statusOf(reviewedCl),
  reviewedBadges: badgesHtml(reviewedCl),
  reviewedState: stateHtml(reviewedCl),
  updatedBadges: badgesHtml(updatedAfterReviewCl),
  approvedLane: laneOf(approvedCl),
  approvedBadges: badgesHtml(approvedCl)
}));
"""
    cmd = ["node", "-e", harness]
    out = subprocess.check_output(cmd, encoding="utf-8")
    data = json.loads(out)

    self.assertEqual(data["reviewedLane"], "waiting")
    self.assertEqual(data["reviewedStatus"], "waiting")
    self.assertIn("badge self-reviewed", data["reviewedBadges"])
    self.assertIn("Reviewed PS2 (25 comments)", data["reviewedBadges"])
    self.assertIn("Waiting on <b>", data["reviewedState"])
    self.assertIn("jmbetancourt", data["reviewedState"])

    self.assertIn("Reviewed PS2 (25 comments) → PS3 uploaded", data["updatedBadges"])
    self.assertIn("1411816/2..3", data["updatedBadges"])

    self.assertEqual(data["approvedLane"], "watching")
    self.assertIn("badge self-approved", data["approvedBadges"])
    self.assertIn("You gave CR+1", data["approvedBadges"])

  def test_gerrit_author_reply_coalesces_messages_and_tracks_resolved_comment_threads(self):
    payload = {
        "status": "NEW",
        "subject": "[skparagraph] Test that ExtendedVisitorInfo::positions can be changed",
        "owner": {"email": "I@YeungKC.com", "name": "KC Yeung"},
        "current_revision": "rev2",
        "revisions": {"rev1": {"_number": 1}, "rev2": {"_number": 2}},
        "attention_set": {
            "8956": {
                "account": {"email": "jlavrova@google.com"},
                "last_update": "2026-11-01 07:16:49.000000000",
                "reason": "Someone else replied on a comment you posted",
            }
        },
        "reviewers": {
            "REVIEWER": [
                {"email": "jlavrova@google.com"},
                {"email": "alexisdavidc@google.com"},
            ],
            "CC": [],
        },
        "labels": {},
        "messages": [
            {
                "_revision_number": 1,
                "author": {"email": "jlavrova@google.com"},
                "date": "2026-11-01 02:55:53.000000000",
                "message": "Patch Set 1:\n\n(1 comment)",
            },
            {
                "_revision_number": 2,
                "author": {"email": "I@YeungKC.com", "name": "KC Yeung"},
                "date": "2026-11-01 07:13:02.000000000",
                "message": "Uploaded patch set 2.",
            },
            {
                "_revision_number": 2,
                "author": {"email": "I@YeungKC.com", "name": "KC Yeung"},
                "date": "2026-11-01 07:13:25.000000000",
                "message": (
                    "Patch Set 2:\n\nDone. I changed the commit message and the"
                    ' test comment. ParagraphImpl::visit() also builds the text blobs, so the text says "not painted or visited yet".'
                ),
            },
            {
                "_revision_number": 1,
                "author": {"email": "I@YeungKC.com", "name": "KC Yeung"},
                "date": "2026-11-01 07:16:49.000000000",
                "message": "Patch Set 1:\n\n(1 comment)",
            },
        ],
        "_comments": {
            "/PATCHSET_LEVEL": [
                {
                    "id": "c1",
                    "patch_set": 1,
                    "updated": "2026-11-01 02:55:53.000000000",
                    "author": {"email": "jlavrova@google.com"},
                    "unresolved": True,
                    "message": "Could you narrow the wording?",
                },
                {
                    "id": "c2",
                    "in_reply_to": "c1",
                    "patch_set": 1,
                    "updated": "2026-11-01 07:16:49.000000000",
                    "author": {"email": "I@YeungKC.com", "name": "KC Yeung"},
                    "unresolved": False,
                    "message": "Done.",
                },
            ]
        },
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status("1412376", {}, [], skia_dir="/nonexistent")

    self.assertTrue(res["ok"])
    self.assertTrue(res["in_my_attention"])
    self.assertEqual(
        res["attention_reason"], "Someone else replied on a comment you posted"
    )
    self.assertEqual(res["self_review_state"], "commented")
    self.assertEqual(res["self_review_ps"], 1)
    self.assertEqual(res["self_comment_count"], 1)
    self.assertTrue(res["author_replied_to_me"])
    self.assertEqual(res["self_threads_total"], 1)
    self.assertEqual(res["self_threads_unresolved"], 0)
    self.assertEqual(res["self_threads_resolved"], 1)
    self.assertEqual(res["last_msg_author"], "I@YeungKC.com")
    self.assertEqual(res["last_msg_author_name"], "KC Yeung")
    self.assertIn("Done. I changed the commit message", res["last_msg_snippet"])
    self.assertIn("Done.", res["author_reply_snippet"])

  def test_dashboard_focus_card_shows_author_reply_badge_full_name_and_missing_cr(self):
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.split("\nboot();", 1)[0] + """
const cl = {
  item_key: 'gerrit:1412376', kind: 'gerrit', target_id: '1412376', status: 'NEW',
  phase: 'NEEDS_YOUR_ACTION', is_self: false, is_external: true, role: 'reviewer (ext)',
  in_my_attention: true, is_draft: false, in_development: false, is_watching: false,
  owner_name: 'KC Yeung', owner_email: 'I@YeungKC.com',
  subject: '[skparagraph] Test that ExtendedVisitorInfo::positions can be changed',
  patchset: 2, url: 'https://skia-review.googlesource.com/c/skia/+/1412376',
  required_approvals: 2, approved_count: 0, assigned_reviewers_count: 2,
  missing_reviewers_to_assign: 0, missing_approvals: 2,
  self_review_state: 'commented', self_review_ps: 1, self_comment_count: 1,
  author_replied_to_me: true, self_threads_total: 1, self_threads_unresolved: 0,
  self_threads_resolved: 1, author_reply_snippet: 'Done.',
  last_msg_author: 'I@YeungKC.com', last_msg_author_name: 'KC Yeung',
  last_msg_snippet: 'Done. I changed the commit message and the test comment.'
};
const box = {jetski_box_url: 'https://jetski.corp.google.com/box/host'};
console.log(JSON.stringify({
  card: focusCard(cl, box),
  badges: badgesHtml(cl)
}));
"""
    cmd = ["node", "-e", harness]
    out = subprocess.check_output(cmd, encoding="utf-8")
    data = json.loads(out)

    card = data["card"]
    badges = data["badges"]

    self.assertIn("KC Yeung", card)
    self.assertNotIn("<b>I</b>", card)
    self.assertIn("Done. I changed the commit message", card)
    self.assertIn("Needs +2 CR (0/2)", card)
    self.assertIn("Reviewed PS1 (1 comment) → PS2 uploaded", badges)
    self.assertIn("Author replied · 1/1 resolved", badges)

  def test_wake_conversation_uses_ls_rpc_and_failed_wake_does_not_promote_reviewed_ps(self):
    with mock.patch.object(
        w.cl, "ls_rpc", return_value={"status": "OK"}
    ) as mock_rpc:
      ok = w.wake_conversation("cid-123", "Wake up")
      self.assertTrue(ok)
      mock_rpc.assert_called_once_with(
          "SendUserCascadeMessage",
          {"cascadeId": "cid-123", "items": [{"text": "Wake up"}], "blocking": False},
      )

    rec = {
        "item_key": "gerrit:1412376",
        "target_id": "1412376",
        "kind": "gerrit",
        "subject": "Test positions",
        "patchset": 2,
        "reviewed_ps": 1,
        "review_target_ps": 1,
        "conversation_id": "cid-rev-1412376",
        "is_self": False,
        "role": "reviewer (ext)",
        "chat_busy": False,
        "url": "https://skia-review.googlesource.com/c/skia/+/1412376",
    }
    state = {
        "items": {"gerrit:1412376@cid-rev-1412376": rec},
        "events": [],
    }
    with mock.patch.object(w, "load_watcher_state", return_value=state), mock.patch.object(
        w, "wake_conversation", return_value=False
    ), mock.patch.object(w, "save_watcher_state"):
      res = w.start_chat_for_item("gerrit:1412376")
      self.assertFalse(res.get("woken", False))
      self.assertFalse(rec.get("chat_busy", False))
      self.assertEqual(rec.get("review_target_ps"), 1)


if __name__ == "__main__":
  unittest.main()
