#!/usr/bin/env python3
"""Contract tests for self-reviewed CL/PR status, CC reviewer detection, and waiting lanes."""

import json
import os
import re
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
                "authoredDate": "2026-10-28T13:30:00Z",
                "committedDate": "2026-10-28T13:30:00Z",
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
  is_draft: false, in_development: false, is_watching: false,
  owner_name: 'Alexis Cruz-Ayala', owner_email: 'alexisdavidc@google.com',
  subject: 'Reland SkGraphics calls',
  patchset: 8, url: 'https://skia-review.googlesource.com/c/skia/+/1402416',
  self_review_state: 'approved', self_review_ps: 6, self_approved: true,
  self_approval_ps: 6, waiting_on: []
};
const lostApprovalCl = {
  kind: 'gerrit', target_id: '1409999', status: 'NEW', phase: 'WAITING_ON_OTHERS',
  is_self: false, is_external: true, role: 'reviewer (ext)',
  is_draft: false, in_development: false, is_watching: false,
  owner_name: 'External Dev', owner_email: 'dev@example.com',
  subject: 'Fix paragraph metrics',
  patchset: 5, url: 'https://skia-review.googlesource.com/c/skia/+/1409999',
  self_review_state: 'approved', self_review_ps: 3, self_approved: false,
  self_approval_ps: 3, waiting_on: []
};

console.log(JSON.stringify({
  reviewedLane: laneOf(reviewedCl),
  reviewedStatus: statusOf(reviewedCl),
  reviewedBadges: badgesHtml(reviewedCl),
  reviewedState: stateHtml(reviewedCl),
  updatedBadges: badgesHtml(updatedAfterReviewCl),
  approvedLane: laneOf(approvedCl),
  approvedBadges: badgesHtml(approvedCl),
  lostApprovalLane: laneOf(lostApprovalCl),
  lostApprovalBadges: badgesHtml(lostApprovalCl)
}));
"""
    # Feed DASHBOARD_HTML harness via stdin to stay below Linux 128 KB MAX_ARG_STRLEN
    out = subprocess.check_output(["node", "-"], input=harness, encoding="utf-8")
    data = json.loads(out)

    self.assertEqual(data["reviewedLane"], "waiting")
    self.assertEqual(data["reviewedStatus"], "waiting")
    self.assertIn("badge self-reviewed", data["reviewedBadges"])
    self.assertIn("Reviewed PS2 (25 comments)", data["reviewedBadges"])
    self.assertIn("Waiting on <b>", data["reviewedState"])
    self.assertIn("jmbetancourt", data["reviewedState"])

    self.assertIn("Reviewed PS2 (25 comments) → PS3 uploaded", data["updatedBadges"])
    self.assertIn("1411816/2..3", data["updatedBadges"])

    self.assertEqual(data["approvedLane"], "waiting")
    self.assertIn("badge self-approved", data["approvedBadges"])
    self.assertIn("You gave CR+1 (PS6)", data["approvedBadges"])

    self.assertEqual(data["lostApprovalLane"], "waiting")
    self.assertNotIn("badge self-approved", data["lostApprovalBadges"])
    self.assertIn("Gave CR+1 on PS3 → PS5 uploaded", data["lostApprovalBadges"])
    self.assertIn("1409999/3..5", data["lostApprovalBadges"])

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
    # Feed DASHBOARD_HTML harness via stdin to stay below Linux 128 KB MAX_ARG_STRLEN
    out = subprocess.check_output(["node", "-"], input=harness, encoding="utf-8")
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

  def test_extract_gerrit_last_message_skips_code_owners_hashtags_and_copied_votes(self):
    messages = [
        {
            "tag": "autogenerated:gerrit:setHashtag",
            "author": {"email": "dbebawy@gmail.com", "name": "David H Bebawy"},
            "date": "2026-11-02 18:53:23.000000000",
            "message": "Hashtag added: skparagraph",
        },
        {
            "tag": "autogenerated:gerrit:code-owners:addReviewer",
            "author": {"email": "dbebawy@gmail.com", "name": "David H Bebawy"},
            "date": "2026-11-02 18:53:23.000000000",
            "message": (
                "jlavrova@google.com, who was added as reviewer owns the following files:\n"
                "* `modules/skparagraph/src/TextLine.cpp`\n"
            ),
        },
    ]
    author, name, date, text = w._extract_gerrit_last_message(messages)
    self.assertEqual(author, "dbebawy@gmail.com")
    self.assertEqual(name, "David H Bebawy")
    self.assertEqual(text, "")

    messages_with_rebase = [
        {
            "author": {"email": "author@example.com", "name": "Author"},
            "date": "2026-11-02 10:00:00.000000000",
            "message": "Patch Set 2: Fixed the indent issue",
        },
        {
            "author": {"email": "author@example.com", "name": "Author"},
            "date": "2026-11-02 11:00:00.000000000",
            "message": (
                "Patch Set 4: Patch Set 3 was rebased\n\n"
                "Copied Votes:\n"
                "* Code-Review+1 by reviewer@google.com (copy condition*: ...)\n"
            ),
        },
    ]
    author, name, date, text = w._extract_gerrit_last_message(messages_with_rebase)
    self.assertEqual(author, "author@example.com")
    self.assertEqual(text, "Patch Set 2: Fixed the indent issue")

  def test_approved_external_cls_route_sponsor_actions_to_needs_you_and_rest_to_waiting(self):
    # Gerrit 1414016: external author, 1/2 CR+1 from self, no 2nd Googler reviewer assigned
    payload_1414016 = {
        "status": "NEW",
        "subject": "[skparagraph] Measure the hit glyph with its own justification shift",
        "owner": {"email": "dbebawy@gmail.com", "name": "David H Bebawy"},
        "current_revision": "rev1",
        "revisions": {"rev1": {"_number": 1}},
        "attention_set": {},
        "reviewers": {
            "REVIEWER": [{"email": "jlavrova@google.com", "name": "Julia Lavrova"}],
            "CC": [],
        },
        "labels": {
            "Code-Review": {"all": [{"email": "jlavrova@google.com", "value": 1}]},
            "Commit-Queue": {"all": []},
        },
        "submit_requirements": [
            {"name": "Review-Enforcement", "status": "UNSATISFIED"},
            {"name": "Code-Review", "status": "SATISFIED"},
            {"name": "No-Unresolved-Comments", "status": "NOT_APPLICABLE"},
            {"name": "Code-Owners", "status": "SATISFIED"},
        ],
        "messages": [
            {
                "_revision_number": 1,
                "author": {"email": "jlavrova@google.com", "name": "Julia Lavrova"},
                "date": "2026-11-02 20:20:00.000000000",
                "message": "Patch Set 1: Code-Review+1",
            }
        ],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload_1414016)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res_missing_rev = w.fetch_gerrit_cl_status("1414016", {}, [], skia_dir="/nonexistent")

    self.assertTrue(res_missing_rev["ok"])
    self.assertFalse(res_missing_rev["is_watching"])
    self.assertFalse(res_missing_rev["in_my_attention"])
    self.assertTrue(res_missing_rev["self_approved"])
    self.assertEqual(res_missing_rev["self_review_state"], "approved")
    self.assertEqual(res_missing_rev["missing_reviewers_to_assign"], 1)
    self.assertEqual(res_missing_rev["sponsor_action"], "assign_second_reviewer")
    self.assertIn("2nd Googler reviewer", res_missing_rev["sponsor_action_reason"])

    # Gerrit 1412381: external Skia CL, 2/2 Googler CR+1, all submit_requirements satisfied, idle CQ
    payload_1412381 = {
        "status": "NEW",
        "subject": "[skparagraph] Keep run baseline aligned",
        "owner": {"email": "ext@example.com", "name": "External Author"},
        "current_revision": "rev4",
        "revisions": {"rev4": {"_number": 4}},
        "attention_set": {},
        "reviewers": {
            "REVIEWER": [
                {"email": "jlavrova@google.com"},
                {"email": "alexisdavidc@google.com"},
            ],
            "CC": [],
        },
        "labels": {
            "Code-Review": {
                "all": [
                    {"email": "jlavrova@google.com", "value": 1},
                    {"email": "alexisdavidc@google.com", "value": 1},
                ]
            },
            "Commit-Queue": {"all": []},
        },
        "submit_requirements": [
            {"name": "Review-Enforcement", "status": "SATISFIED"},
            {"name": "Code-Review", "status": "SATISFIED"},
            {"name": "No-Unresolved-Comments", "status": "NOT_APPLICABLE"},
            {"name": "Code-Owners", "status": "SATISFIED"},
        ],
        "messages": [
            {
                "_revision_number": 4,
                "author": {"email": "jlavrova@google.com"},
                "date": "2026-11-02 18:00:00.000000000",
                "message": "Patch Set 4: Code-Review+1",
            },
            {
                "_revision_number": 4,
                "author": {"email": "alexisdavidc@google.com"},
                "date": "2026-11-02 19:00:00.000000000",
                "message": "Patch Set 4: Code-Review+1",
            },
        ],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload_1412381)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res_ready_cq = w.fetch_gerrit_cl_status("1412381", {}, [], skia_dir="/nonexistent")

    self.assertFalse(res_ready_cq["is_watching"])
    self.assertFalse(res_ready_cq["in_my_attention"])
    self.assertEqual(res_ready_cq["sponsor_action"], "submit_cq")
    self.assertIn("CQ+2", res_ready_cq["sponsor_action_reason"])

    # Gerrit 1409697: 2/2 Googler CR+1 and all submit_requirements satisfied, but merge conflict
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload_1412381)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=False):
      res_cq_conflict = w.fetch_gerrit_cl_status("1409697", {}, [], skia_dir="/nonexistent")

    self.assertFalse(res_cq_conflict["is_watching"])
    self.assertEqual(res_cq_conflict["sponsor_action"], "submit_cq_conflict")
    self.assertIn("rebase", res_cq_conflict["sponsor_action_reason"])

    # Gerrit 1409696: 1/2 CR+1, 2nd Googler assigned, No-Unresolved-Comments UNSATISFIED -> waiting
    payload_1409696 = dict(payload_1412381)
    payload_1409696["labels"] = {
        "Code-Review": {
            "all": [
                {"email": "jlavrova@google.com", "value": 1},
                {"email": "alexisdavidc@google.com", "value": 0},
            ]
        },
        "Commit-Queue": {"all": []},
    }
    payload_1409696["submit_requirements"] = [
        {"name": "Review-Enforcement", "status": "UNSATISFIED"},
        {"name": "Code-Review", "status": "SATISFIED"},
        {"name": "No-Unresolved-Comments", "status": "UNSATISFIED"},
    ]
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload_1409696)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=False), mock.patch.object(
        w, "google_account_active", return_value=True
    ):
      res_waiting_2nd = w.fetch_gerrit_cl_status("1409696", {}, [], skia_dir="/nonexistent")

    self.assertFalse(res_waiting_2nd["is_watching"])
    self.assertFalse(res_waiting_2nd["in_my_attention"])
    self.assertEqual(res_waiting_2nd["sponsor_action"], "")

    # crrev/c/7686597: CR+1 on PS2 lost on PS4, then self commented on PS4 without +1
    payload_7686597 = {
        "status": "NEW",
        "subject": "Fontations font fallback",
        "owner": {"email": "jftucker@gmail.com", "name": "James Tucker"},
        "current_revision": "rev4",
        "revisions": {"rev2": {"_number": 2}, "rev4": {"_number": 4}},
        "attention_set": {"1": {"account": {"email": "jftucker@gmail.com"}}},
        "reviewers": {
            "REVIEWER": [
                {"email": "jlavrova@google.com"},
                {"email": "alexisdavidc@google.com"},
            ],
            "CC": [],
        },
        "labels": {"Code-Review": {"all": [{"email": "jlavrova@google.com", "value": 0}]}},
        "messages": [
            {
                "_revision_number": 2,
                "author": {"email": "jlavrova@google.com"},
                "date": "2026-10-15 14:44:05.000000000",
                "message": "Patch Set 2: Code-Review+1",
            },
            {
                "_revision_number": 4,
                "author": {"email": "jlavrova@google.com"},
                "date": "2026-10-27 16:28:34.000000000",
                "message": "Patch Set 4:\n\n(4 comments)",
            },
        ],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload_7686597)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True), mock.patch.object(
        w, "fetch_gerrit_comments", return_value={}
    ):
      res_7686597 = w.fetch_gerrit_cl_status(
          "7686597", {}, [], skia_dir="/nonexistent", preferred_host=w.GERRIT_CHROMIUM_HOST
      )

    self.assertFalse(res_7686597["self_approved"])
    self.assertEqual(res_7686597["self_review_state"], "commented")
    self.assertEqual(res_7686597["self_review_ps"], 4)
    self.assertFalse(res_7686597["is_watching"])

    # Verify JS lanes, focusCard rendering for 1414016 & 1412381, and collapsed stack sibling pill
    res_missing_rev["item_key"] = "gerrit:1414016"
    res_missing_rev["kind"] = "gerrit"
    res_missing_rev["target_id"] = "1414016"
    res_missing_rev["project_name"] = "SkParagraph"
    res_missing_rev["conversation_id"] = "55006e03-15e4-4762-8c30-454dfeefdcf8"
    res_missing_rev["reviewed_ps"] = 1
    res_missing_rev["triage"] = {
        "verdict": "CLEAN",
        "verdict_label": "CLEAN",
        "review_outcome": "CLEAN",
        "review_eyebrow_label": "🟢 Review PS1: Clean (LGTM)",
        "triaged_patchset": 1,
        "summary": "Clean justification hit-test fix.",
    }
    res_ready_cq["item_key"] = "gerrit:1412381"
    res_ready_cq["kind"] = "gerrit"
    res_ready_cq["target_id"] = "1412381"
    res_ready_cq["project_name"] = "SkParagraph"
    res_waiting_2nd["item_key"] = "gerrit:1409696"
    res_waiting_2nd["kind"] = "gerrit"
    res_waiting_2nd["target_id"] = "1409696"
    res_waiting_2nd["project_name"] = "SkParagraph"
    res_waiting_2nd["stack_ancestors"] = ["gerrit:1414016"]
    res_waiting_2nd["stack_ikeys"] = ["gerrit:1414016", "gerrit:1409696"]
    res_missing_rev["stack_ikeys"] = ["gerrit:1414016", "gerrit:1409696"]
    res_waiting_2nd["triage"] = {
        "verdict": "CLEAN",
        "review_outcome": "CLEAN",
        "review_eyebrow_label": "🟢 Review PS4: Clean (LGTM)",
        "triaged_patchset": 4,
    }

    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = (
        script_body.split("\nboot();", 1)[0]
        + "\nconst c1414016 = "
        + json.dumps(res_missing_rev)
        + ";\nconst c1412381 = "
        + json.dumps(res_ready_cq)
        + ";\nconst c1409696 = "
        + json.dumps(res_waiting_2nd)
        + ";\nconst box = {jetski_box_url: 'https://jetski.corp.google.com/box/host', items: {}};\n"
        + "buildStackContext([c1414016, c1409696]);\n"
        + "console.log(JSON.stringify({\n"
        + "  lane1414016: laneOf(c1414016),\n"
        + "  lane1412381: laneOf(c1412381),\n"
        + "  lane1409696: laneOf(c1409696),\n"
        + "  card1414016: focusCard(c1414016, box),\n"
        + "  card1412381: focusCard(c1412381, box),\n"
        + "  state1409696: stateHtml(c1409696),\n"
        + "  focusStack: renderFocusSection([c1414016], [c1414016, c1409696], box)\n"
        + "}));\n"
    )
    # Feed DASHBOARD_HTML via stdin so script bytes stay independent of Linux MAX_ARG_STRLEN
    out = json.loads(
        subprocess.check_output(["node", "-"], input=harness, encoding="utf-8")
    )
    self.assertEqual(out["lane1414016"], "you")
    self.assertEqual(out["lane1412381"], "you")
    self.assertEqual(out["lane1409696"], "waiting")
    self.assertIn("🟢 Review PS1: Clean (LGTM)", out["card1414016"])
    self.assertIn("https://skia-review.googlesource.com/c/1414016", out["card1414016"])
    self.assertIn("Sponsor action", out["card1414016"])
    self.assertIn("2nd Googler reviewer", out["card1414016"])
    self.assertIn("Open review chat", out["card1414016"])
    self.assertIn("CQ+2", out["card1412381"])
    self.assertIn("Conflict", out["state1409696"])
    self.assertIn("🟢 Review PS4: Clean (LGTM)", out["focusStack"])

  def test_github_approved_pr_and_gerrit_item_chat_title_prefix(self):
    gh_json = json.dumps({
        "number": 195600,
        "title": "[web] Paragraph hit-test",
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": "mdebbar"},
        "mergedAt": None,
        "headRefOid": "1122334455667788",
        "reviews": [
            {
                "author": {"login": "Rusino"},
                "state": "APPROVED",
                "body": "LGTM",
                "submittedAt": "2026-11-02T15:00:00Z",
            }
        ],
        "comments": [],
        "commits": [],
        "statusCheckRollup": [],
        "url": "https://github.com/flutter/flutter/pull/195600",
        "mergeable": "MERGEABLE",
        "reviewRequests": [],
    })
    with mock.patch.object(w.subprocess, "check_output", return_value=gh_json):
      gh_res = w.fetch_github_pr_status("195600")
    self.assertEqual(gh_res["self_review_state"], "approved")
    self.assertFalse(gh_res["is_watching"])

    skia_title = w._build_start_chat_topic({
        "kind": "gerrit",
        "host": w.GERRIT_SKIA_HOST,
        "target_id": "1414016",
        "subject": "[skparagraph] Measure the hit glyph with its own justification shift",
    })
    self.assertTrue(skia_title.startswith("Gerrit 1414016 "), skia_title)
    self.assertNotIn("cl/1414016", skia_title)

    piper_title = w._build_start_chat_topic({
        "kind": "piper",
        "target_id": "1008075988",
        "subject": "Roll Skia into google3",
    })
    self.assertTrue(piper_title.startswith("cl/1008075988 "), piper_title)

  # Verifies 1-click Skia 2nd reviewer actions, stack sibling boost, spine, and /api/add_reviewer
  def test_assign_second_reviewer_primary_actions_stack_header_and_api(self):
    base_payload = {
        "status": "NEW",
        "subject": "[skparagraph] Measure the hit glyph with its own justification shift",
        "owner": {"email": "dbebawy@gmail.com", "name": "David H Bebawy"},
        "current_revision": "rev1",
        "revisions": {
            "rev1": {
                "_number": 1,
                "files": {"modules/skparagraph/src/TextLine.cpp": {}},
            }
        },
        "attention_set": {},
        "reviewers": {
            "REVIEWER": [
                {"email": "jlavrova@google.com", "name": "Julia Lavrova"},
                {"email": "I@YeungKC.com", "name": "KC Yeung"},
            ],
            "CC": [],
        },
        "labels": {
            "Code-Review": {
                "all": [
                    {"email": "jlavrova@google.com", "value": 1},
                    {"email": "I@YeungKC.com", "value": 1},
                ]
            },
            "Commit-Queue": {"all": []},
        },
        "submit_requirements": [
            {"name": "Review-Enforcement", "status": "UNSATISFIED"},
            {"name": "Code-Review", "status": "SATISFIED"},
            {"name": "Code-Owners", "status": "SATISFIED"},
        ],
        "messages": [
            {
                "_revision_number": 1,
                "author": {"email": "jlavrova@google.com", "name": "Julia Lavrova"},
                "date": "2026-11-02 20:20:00.000000000",
                "message": "Patch Set 1: Code-Review+1",
            }
        ],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(base_payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True), mock.patch.object(
        w, "google_account_active", return_value=True
    ):
      res_text = w.fetch_gerrit_cl_status("1414016", {}, [], skia_dir="/nonexistent")

    self.assertEqual(res_text["approved_count"], 1)
    self.assertEqual(res_text["missing_approvals"], 1)
    self.assertEqual(res_text["missing_reviewers_to_assign"], 1)
    self.assertEqual(res_text["sponsor_action"], "assign_second_reviewer")
    self.assertEqual(
        [c["short"] for c in res_text["suggested_second_reviewers"]],
        ["Alexis", "Florin"],
    )

    ck_payload = json.loads(json.dumps(base_payload))
    ck_payload["revisions"]["rev1"]["files"] = {"modules/canvaskit/paragraph.js": {}}
    ck_payload["reviewer_updates"] = [
        {"state": "REMOVED", "reviewer": {"email": "fmalita@google.com"}}
    ]
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(ck_payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True), mock.patch.object(
        w, "google_account_active", return_value=True
    ):
      res_removed = w.fetch_gerrit_cl_status("1404496", {}, [], skia_dir="/nonexistent")
    self.assertEqual(
        [c["email"] for c in res_removed["suggested_second_reviewers"]],
        ["alexisdavidc@google.com"],
    )

    ck_payload["reviewer_updates"] = []
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(ck_payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True), mock.patch.object(
        w, "google_account_active", return_value=True
    ):
      res_ck = w.fetch_gerrit_cl_status("1404496", {}, [], skia_dir="/nonexistent")
      res_cr = w.fetch_gerrit_cl_status(
          "7686597",
          {},
          [],
          skia_dir="/nonexistent",
          preferred_host=w.GERRIT_CHROMIUM_HOST,
      )
    self.assertEqual(
        [c["short"] for c in res_ck["suggested_second_reviewers"]],
        ["Florin", "Alexis"],
    )
    self.assertEqual(res_cr["suggested_second_reviewers"], [])

    c1 = dict(res_text)
    c1.update({
        "item_key": "gerrit:1414016",
        "kind": "gerrit",
        "target_id": "1414016",
        "project_name": "SkParagraph",
        "stack_ikeys": ["gerrit:1414016", "gerrit:1414017"],
    })
    c2 = dict(res_text)
    c2.update({
        "item_key": "gerrit:1414017",
        "kind": "gerrit",
        "target_id": "1414017",
        "url": "https://skia-review.googlesource.com/c/1414017",
        "subject": "[skparagraph] Shift the trailing space of an RTL run",
        "project_name": "SkParagraph",
        "stack_ancestors": ["gerrit:1414016"],
        "stack_ikeys": ["gerrit:1414016", "gerrit:1414017"],
    })
    c_sib_parent = dict(res_text)
    c_sib_parent.update({
        "item_key": "gerrit:1409658",
        "kind": "gerrit",
        "target_id": "1409658",
        "project_name": "SkParagraph",
        "stack_ikeys": ["gerrit:1409658", "gerrit:1409696"],
    })
    c_sib_child = {
        "item_key": "gerrit:1409696",
        "kind": "gerrit",
        "target_id": "1409696",
        "status": "NEW",
        "phase": "WAITING_ON_OTHERS",
        "is_self": False,
        "is_external": True,
        "self_approved": True,
        "self_review_state": "approved",
        "sponsor_action": "",
        "reviewer_pings": [{"reviewer": "fmalita@google.com", "active": True}],
        "stack_ancestors": ["gerrit:1409658"],
        "stack_ikeys": ["gerrit:1409658", "gerrit:1409696"],
    }

    sib_approved_payload = json.loads(json.dumps(base_payload))
    sib_approved_payload["reviewers"]["REVIEWER"].append(
        {"email": "fmalita@google.com", "name": "Florin Malita"}
    )
    sib_approved_payload["labels"]["Code-Review"]["all"].append(
        {"email": "fmalita@google.com", "value": 1}
    )
    sib_approved_payload["submit_requirements"] = [
        {"name": "Review-Enforcement", "status": "SATISFIED"},
        {"name": "Code-Review", "status": "SATISFIED"},
    ]
    with mock.patch.object(
        w.urllib.request,
        "urlopen",
        return_value=_FakeGerritResponse(sib_approved_payload),
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True), mock.patch.object(
        w, "google_account_active", return_value=True
    ):
      c_sib_approved = w.fetch_gerrit_cl_status("1409696", {}, [], skia_dir="/nonexistent")
    c_sib_approved.update({
        "item_key": "gerrit:1409696",
        "kind": "gerrit",
        "target_id": "1409696",
        "stack_ancestors": ["gerrit:1409658"],
        "stack_ikeys": ["gerrit:1409658", "gerrit:1409696"],
    })

    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = (
        script_body.split("\nboot();", 1)[0]
        + "\nconst c1 = "
        + json.dumps(c1)
        + ";\nconst c2 = "
        + json.dumps(c2)
        + ";\nconst p1 = "
        + json.dumps(c_sib_parent)
        + ";\nconst p2 = "
        + json.dumps(c_sib_child)
        + ";\nconst p2App = "
        + json.dumps(c_sib_approved)
        + ";\nconst box = {jetski_box_url: 'https://jetski.corp.google.com/box/host', items: {}};\n"
        + "buildStackContext([c1, c2]);\n"
        + "const card1 = focusCard(c1, box);\n"
        + "const stackHtml = renderFocusSection([c1, c2], [c1, c2], box);\n"
        + "buildStackContext([p1, p2]);\n"
        + "const sibCard = focusCard(p1, box);\n"
        + "buildStackContext([p1, p2App]);\n"
        + "const sibApprovedCard = focusCard(p1, box);\n"
        + "console.log(JSON.stringify({card1, stackHtml, sibCard, sibApprovedCard}));\n"
    )
    ui = json.loads(
        subprocess.check_output(["node", "-"], input=harness, encoding="utf-8")
    )
    self.assertIn("👤 + Alexis (2nd reviewer)", ui["card1"])
    self.assertIn("+ Florin", ui["card1"])
    self.assertIn(
        'class="btn secondary" href="https://skia-review.googlesource.com/c/1414016"',
        ui["card1"],
    )
    self.assertIn("2 approved · need 2nd reviewer", ui["stackHtml"])
    self.assertNotIn("2 need you, 2 approved", ui["stackHtml"])
    self.assertIn("CR+1 · +2nd rev", ui["stackHtml"])
    self.assertNotIn("✓ CR+1", ui["stackHtml"])
    self.assertIn("👤 + Alexis to all 2 CLs", ui["stackHtml"])
    self.assertIn("+ Florin to all 2 CLs", ui["stackHtml"])
    self.assertIn("👤 + Florin (2nd reviewer)", ui["sibCard"])
    self.assertIn("👤 + Florin (2nd reviewer)", ui["sibApprovedCard"])

    seen_reqs: list[tuple[str, dict]] = []

    def _fake_urlopen(req, timeout=10):
      del timeout
      seen_reqs.append((req.full_url, json.loads(req.data.decode("utf-8"))))
      return _FakeGerritResponse({"reviewers": [{"email": "alexisdavidc@google.com"}]})

    fake_state = {
        "items": {
            "gerrit:1414016@cid1": dict(c1, reviewer_pings=[], waiting_on=[]),
            "gerrit:1414017@cid2": dict(c2, reviewer_pings=[], waiting_on=[]),
        },
        "events": [],
    }
    with mock.patch.object(
        w, "_get_gerrit_bearer_token", return_value="tok-123"
    ), mock.patch.object(w.urllib.request, "urlopen", side_effect=_fake_urlopen):
      api_res = w.assign_second_reviewer_items(
          ["gerrit:1414016", "gerrit:1414017"],
          "alexisdavidc@google.com",
          state=fake_state,
      )

    self.assertTrue(api_res["ok"])
    self.assertEqual(api_res["assigned"], ["gerrit:1414016", "gerrit:1414017"])
    self.assertEqual(
        seen_reqs,
        [
            (
                "https://skia-review.googlesource.com/a/changes/1414016/reviewers",
                {"reviewer": "alexisdavidc@google.com"},
            ),
            (
                "https://skia-review.googlesource.com/a/changes/1414017/reviewers",
                {"reviewer": "alexisdavidc@google.com"},
            ),
        ],
    )
    for k in ("gerrit:1414016@cid1", "gerrit:1414017@cid2"):
      rec = fake_state["items"][k]
      self.assertEqual(rec["missing_reviewers_to_assign"], 0)
      self.assertEqual(rec["sponsor_action"], "")
      self.assertEqual(rec["suggested_second_reviewers"], [])
      self.assertEqual(rec["reviewer_pings"][0]["reviewer"], "alexisdavidc@google.com")
      self.assertEqual(rec["reviewer_pings"][0]["reason"], "needs_second_approval")

    err_state = {"items": {"gerrit:1414016@cid1": dict(c1)}, "events": []}
    with mock.patch.object(
        w, "_get_gerrit_bearer_token", return_value="tok-123"
    ), mock.patch.object(
        w.urllib.request,
        "urlopen",
        return_value=_FakeGerritResponse({"error": "Account not found"}),
    ):
      err_res = w.assign_second_reviewer_items(
          ["gerrit:1414016"], "alexisdavidc@google.com", state=err_state
      )
    self.assertFalse(err_res["ok"])
    self.assertEqual(
        err_state["items"]["gerrit:1414016@cid1"]["sponsor_action"],
        "assign_second_reviewer",
    )

    disk_state = {
        "items": {"gerrit:1414016@cid1": dict(c1, reviewer_pings=[], waiting_on=[])},
        "events": [],
    }
    saved_snapshots: list[dict] = []

    def _concurrent_urlopen(req, timeout=10):
      del req, timeout
      disk_state["items"]["gerrit:9999999@bg"] = {"item_key": "gerrit:9999999"}
      return _FakeGerritResponse({"reviewers": [{"email": "alexisdavidc@google.com"}]})

    with mock.patch.object(
        w, "load_watcher_state", side_effect=lambda: json.loads(json.dumps(disk_state))
    ), mock.patch.object(
        w, "save_watcher_state", side_effect=saved_snapshots.append
    ), mock.patch.object(
        w, "_get_gerrit_bearer_token", return_value="tok-123"
    ), mock.patch.object(
        w.urllib.request, "urlopen", side_effect=_concurrent_urlopen
    ):
      race_res = w.assign_second_reviewer_items(
          ["gerrit:1414016"], "alexisdavidc@google.com"
      )
    self.assertTrue(race_res["ok"])
    self.assertIn("gerrit:9999999@bg", saved_snapshots[-1]["items"])
    self.assertEqual(
        saved_snapshots[-1]["items"]["gerrit:1414016@cid1"]["sponsor_action"], ""
    )

  # Verifies pre-CR+1 external Skia cards show neutral 'After your CR+1' hint without 1-click buttons
  def test_pre_approval_external_cl_shows_neutral_after_cr1_named_hint(self):
    pre_cr_payload = {
        "status": "NEW",
        "subject": "[skparagraph] Move the trailing spaces of a line to its visual end",
        "owner": {"email": "dbebawy@gmail.com", "name": "David H Bebawy"},
        "current_revision": "rev1",
        "revisions": {
            "rev1": {
                "_number": 1,
                "files": {"modules/skparagraph/src/TextLine.cpp": {}},
            }
        },
        "attention_set": {
            "1001": {
                "account": {"email": "jlavrova@google.com"},
                "reason": "Reviewer was added",
            }
        },
        "reviewers": {
            "REVIEWER": [{"email": "jlavrova@google.com", "name": "Julia Lavrova"}],
            "CC": [],
        },
        "labels": {"Code-Review": {"all": []}, "Commit-Queue": {"all": []}},
        "submit_requirements": [
            {"name": "Review-Enforcement", "status": "UNSATISFIED"},
            {"name": "Code-Review", "status": "UNSATISFIED"},
        ],
        "messages": [],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(pre_cr_payload)
    ), mock.patch.object(
        w, "gerrit_mergeable", return_value=True
    ), mock.patch.object(
        w, "google_account_active", return_value=True
    ):
      res_skia = w.fetch_gerrit_cl_status("1414416", {}, [], skia_dir="/nonexistent")
      res_cr = w.fetch_gerrit_cl_status(
          "7686597",
          {},
          [],
          skia_dir="/nonexistent",
          preferred_host=w.GERRIT_CHROMIUM_HOST,
      )

    self.assertEqual(res_skia["self_cr_vote"], 0)
    self.assertEqual(res_skia["missing_reviewers_to_assign"], 1)
    self.assertEqual(res_skia["sponsor_action"], "")
    self.assertEqual(
        [c["short"] for c in res_skia["suggested_second_reviewers"]],
        ["Alexis", "Florin"],
    )
    self.assertEqual(res_cr["sponsor_action"], "")
    self.assertEqual(res_cr["suggested_second_reviewers"], [])

    m = re.search(r"<script>([\s\S]*?)</script>", w.DASHBOARD_HTML)
    self.assertIsNotNone(m)
    script = re.sub(r"\bboot\(\);\s*$", "", m.group(1))
    skia_item = dict(res_skia, item_key="gerrit:1414416", kind="gerrit", target_id="1414416")
    cr_item = dict(res_cr, item_key="gerrit:7686597", kind="gerrit", target_id="7686597")
    harness = (
        "global.document = {getElementById: () => ({textContent:'', innerHTML:''}), addEventListener: () => {},"
        " documentElement: {classList: {add: () => {}}}};\n"
        "global.window = {parent: null, addEventListener: () => {}};\n"
        "global.location = {search: '', hash: ''};\n"
        "global.localStorage = {getItem: () => null, setItem: () => {}};\n"
        "global.fetch = async () => ({json: async () => ({})});\n"
        "global.setInterval = () => {};\n"
        + script
        + f"\nconst skiaIt = {json.dumps(skia_item)};\n"
        + f"const crIt = {json.dumps(cr_item)};\n"
        + "const sibFlorin = Object.assign({}, skiaIt, {item_key: 'gerrit:1414017',"
        + " assigned_reviewers: ['fmalita@google.com'], missing_reviewers_to_assign: 0});\n"
        + "const skiaStacked = Object.assign({}, skiaIt,"
        + " {_stackMembers: [sibFlorin, skiaIt]});\n"
        + "const ownOneOfTwo = {item_key: 'github:195999', kind: 'github',"
        + " target_id: '195999', status: 'OPEN', is_self: true, is_external: false,"
        + " required_approvals: 2, approved_count: 0, assigned_reviewers_count: 1,"
        + " missing_reviewers_to_assign: 1, missing_approvals: 2};\n"
        + "const unassExt = Object.assign({}, skiaIt,"
        + " {assigned_reviewers_count: 0, missing_reviewers_to_assign: 2});\n"
        + "console.log(JSON.stringify({\n"
        + "  skiaCard: focusCard(skiaIt, {jetski_box_url: ''}),\n"
        + "  skiaState: stateHtml(skiaIt),\n"
        + "  stackedGap: reviewerGapHtml(skiaStacked),\n"
        + "  crGap: reviewerGapHtml(crIt),\n"
        + "  ownGap: reviewerGapHtml(ownOneOfTwo),\n"
        + "  unassGap: reviewerGapHtml(unassExt),\n"
        + "}));\n"
    )
    try:
        proc = subprocess.run(
            ["node", "-"], input=harness, capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as e:
        print("NODE STDERR:", e.stderr)
        raise e
    ui = json.loads(proc.stdout.strip())
    expected_hint = '<span class="soft">After your CR+1: +1 Googler (Alexis / Florin)</span>'
    self.assertIn(expected_hint, ui["skiaCard"])
    self.assertIn(expected_hint, ui["skiaState"])
    self.assertNotIn("Needs +1 reviewer", ui["skiaCard"])
    self.assertNotIn("assignReviewer(", ui["skiaCard"])
    self.assertIn('class="btn primary"', ui["skiaCard"])
    self.assertIn("After your CR+1: +1 Googler (Alexis / Florin)", ui["stackedGap"])
    self.assertEqual(
        ui["crGap"], '<span class="soft">After your CR+1: +1 Googler (1/2)</span>'
    )
    self.assertEqual(
        ui["ownGap"], '<span class="amber">Needs +1 reviewer (1/2)</span>'
    )
    self.assertEqual(ui["unassGap"], '<span class="amber">Needs 2 reviewers</span>')


if __name__ == "__main__":
  unittest.main()

