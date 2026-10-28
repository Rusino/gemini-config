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

  def test_gerrit_self_wip_cl_marks_is_draft_and_clears_attention(self):
    payload = {
        "status": "NEW",
        "work_in_progress": True,
        "subject": "[skparagraph] WIP: Prototype cluster cache",
        "owner": {"email": "jlavrova@google.com", "name": "Julia Lavrova"},
        "current_revision": "rev1",
        "revisions": {"rev1": {"_number": 5}},
        "attention_set": {"1": {"account": {"email": "jlavrova@google.com"}}},
        "reviewers": {"REVIEWER": [{"email": "egdaniel@google.com"}]},
        "labels": {},
        "messages": [],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status("1405600", {}, [], skia_dir="/nonexistent")

    self.assertTrue(res["ok"])
    self.assertTrue(res["is_self"])
    self.assertTrue(res["is_draft"])
    self.assertFalse(res["in_development"])
    self.assertFalse(res["is_watching"])
    self.assertFalse(res["in_my_attention"])
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")

  def test_gerrit_colleague_wip_cl_with_self_as_reviewer_waits_without_draft_bucket(
      self,
  ):
    payload = {
        "status": "NEW",
        "work_in_progress": True,
        "subject": "[skparagraph] WIP: Refactor text line metrics",
        "owner": {"email": "jiameil@google.com", "name": "Jiamei"},
        "current_revision": "rev1",
        "revisions": {"rev1": {"_number": 2}},
        "attention_set": {"1": {"account": {"email": "jlavrova@google.com"}}},
        "reviewers": {"REVIEWER": [{"email": "jlavrova@google.com"}]},
        "labels": {},
        "messages": [],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status("1386097", {}, [], skia_dir="/nonexistent")

    self.assertTrue(res["ok"])
    self.assertFalse(res["is_self"])
    self.assertFalse(res["is_draft"])
    self.assertFalse(res["in_development"])
    self.assertTrue(res["is_watching"])
    self.assertEqual(res["role"], "reviewer (goog)")
    self.assertFalse(res["in_my_attention"])
    self.assertEqual(res["waiting_on"], [])
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")

  def test_gerrit_colleague_cl_already_reviewed_by_self_routes_to_watching_not_waiting(
      self,
  ):
    payload = {
        "status": "NEW",
        "work_in_progress": False,
        "subject": 'Reland "[SkContext] Forwarding SkGraphics calls to SkContext"',
        "owner": {"email": "alexisdavidc@google.com", "name": "Alexis Cruz-Ayala"},
        "current_revision": "rev8",
        "revisions": {"rev8": {"_number": 8}},
        "attention_set": {
            "1": {"account": {"email": "michaelludwig@google.com"}},
            "2": {"account": {"email": "alexisdavidc@google.com"}},
        },
        "reviewers": {
            "REVIEWER": [
                {"email": "alexisdavidc@google.com"},
                {"email": "jlavrova@google.com"},
                {"email": "michaelludwig@google.com"},
            ]
        },
        "labels": {
            "Code-Review": {
                "all": [
                    {"email": "jlavrova@google.com", "value": 1},
                ]
            }
        },
        "messages": [],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status("1402416", {}, [], skia_dir="/nonexistent")

    self.assertTrue(res["ok"])
    self.assertFalse(res["is_self"])
    self.assertFalse(res["is_draft"])
    self.assertFalse(res["in_development"])
    self.assertFalse(res["in_my_attention"])
    self.assertTrue(res["is_watching"])
    self.assertEqual(res["role"], "reviewer (goog)")
    self.assertEqual(res["waiting_on"], [])
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")

  def test_gerrit_reviewer_cl_with_self_code_review_plus_one_clears_attention_even_in_attention_set(
      self,
  ):
    payload = {
        "status": "NEW",
        "work_in_progress": False,
        "subject": "[skparagraph] Keep line breaking of long words linear",
        "owner": {"email": "I@YeungKC.com", "name": "Keyuan Yeung"},
        "current_revision": "rev2",
        "revisions": {"rev2": {"_number": 2}},
        "attention_set": {
            "8956": {
                "account": {"email": "jlavrova@google.com"},
                "reason": "SkCQ dry run succeeded.",
            }
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
            },
            "Commit-Queue": {
                "all": [
                    {"email": "jlavrova@google.com", "value": 0},
                ]
            },
        },
        "messages": [
            {
                "_revision_number": 2,
                "author": {
                    "email": "skcq-be@skia-corp.google.com.iam.gserviceaccount.com"
                },
                "date": "2026-10-27 15:54:43.000000000",
                "message": "Patch Set 2: Dry run: This CL passed the SkCQ dry run.",
            }
        ],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status("1407016", {}, [], skia_dir="/nonexistent")

    self.assertTrue(res["ok"])
    self.assertFalse(res["is_self"])
    self.assertTrue(res["is_external"])
    self.assertFalse(res["in_my_attention"])
    self.assertTrue(res["is_watching"])
    self.assertEqual(res["role"], "reviewer (ext)")
    self.assertEqual(res["waiting_on"], [])
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")

  def test_gerrit_reviewer_cl_with_failed_cq_still_needs_your_action_not_cq_alert(
      self,
  ):
    payload = {
        "status": "NEW",
        "work_in_progress": False,
        "subject": "[skparagraph] Add cluster range helper",
        "owner": {"email": "alexisdavidc@google.com", "name": "Alexis Cruz-Ayala"},
        "current_revision": "rev3",
        "revisions": {"rev3": {"_number": 3}},
        "attention_set": {
            "8956": {"account": {"email": "jlavrova@google.com"}},
        },
        "reviewers": {
            "REVIEWER": [
                {"email": "jlavrova@google.com"},
            ]
        },
        "labels": {},
        "messages": [
            {
                "_revision_number": 3,
                "author": {
                    "email": "skcq-be@skia-corp.google.com.iam.gserviceaccount.com"
                },
                "date": "2026-10-27 16:00:00.000000000",
                "message": "Patch Set 3: This CL failed the SkCQ",
            }
        ],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status("1408001", {}, [], skia_dir="/nonexistent")

    self.assertFalse(res["is_self"])
    self.assertTrue(res["cq_failed"])
    self.assertTrue(res["in_my_attention"])
    self.assertFalse(res["is_watching"])
    self.assertEqual(res["phase"], "NEEDS_YOUR_ACTION")

  def test_gerrit_non_participant_cl_marks_is_watching_not_draft_or_dev(self):
    payload = {
        "status": "NEW",
        "work_in_progress": True,
        "subject": "[agentic-review] Add starter Skia knowledge base rules",
        "owner": {"email": "jiameil@google.com", "name": "Jiamei"},
        "current_revision": "rev1",
        "revisions": {"rev1": {"_number": 5}},
        "attention_set": {},
        "reviewers": {"REVIEWER": [{"email": "egdaniel@google.com"}]},
        "labels": {},
        "messages": [
            {
                "_revision_number": 5,
                "author": {
                    "email": "skcq-be@skia-corp.google.com.iam.gserviceaccount.com"
                },
                "date": "2026-10-21 22:49:56.000000000",
                "message": "Patch Set 5: This CL failed the SkCQ",
            }
        ],
    }
    with mock.patch.object(
        w.urllib.request, "urlopen", return_value=_FakeGerritResponse(payload)
    ), mock.patch.object(w, "gerrit_mergeable", return_value=True):
      res = w.fetch_gerrit_cl_status("1386096", {}, [], skia_dir="/nonexistent")

    self.assertTrue(res["ok"])
    self.assertFalse(res["is_self"])
    self.assertFalse(res["is_draft"])
    self.assertFalse(res["in_development"])
    self.assertTrue(res["is_watching"])
    self.assertEqual(res["role"], "watching")
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

  def test_piper_cl_distinguishes_in_development_watching_and_in_review(self):
    no_rev_out = (
        "Change 1009999999 by jlavrova@jlavrova:ws on 2026/10/26 12:00:00\n\n"
        "\tUpdate SkParagraph tests\n\n"
        "Affected files ...\n"
    )
    with mock.patch.object(w.subprocess, "check_output", return_value=no_rev_out):
      res_dev = w.fetch_piper_cl_status("1009999999")
    self.assertFalse(res_dev["is_draft"])
    self.assertTrue(res_dev["in_development"])
    self.assertFalse(res_dev["is_watching"])

    watching_out = (
        "Change 1002307625 by bwils@bwils:ws on 2026/10/19 06:55:40\n\n"
        "\tRemove missing project from skia check_tests\n\n"
        "\tR=egdaniel\n\n"
        "Affected files ...\n"
    )
    with mock.patch.object(w.subprocess, "check_output", return_value=watching_out):
      res_watch = w.fetch_piper_cl_status("1002307625")
    self.assertFalse(res_watch["is_draft"])
    self.assertFalse(res_watch["in_development"])
    self.assertTrue(res_watch["is_watching"])
    self.assertEqual(res_watch["role"], "watching")

    reviewer_out = (
        "Change 1002307626 by bwils@bwils:ws on 2026/10/19 06:55:40\n\n"
        "\tUpdate skia check_tests\n\n"
        "\tR=jlavrova\n\n"
        "Affected files ...\n"
    )
    with mock.patch.object(w.subprocess, "check_output", return_value=reviewer_out):
      res_rev = w.fetch_piper_cl_status("1002307626")
    self.assertFalse(res_rev["is_draft"])
    self.assertFalse(res_rev["in_development"])
    self.assertTrue(res_rev["is_watching"])
    self.assertEqual(res_rev["role"], "reviewer (goog)")

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
    self.assertFalse(res_wip["is_watching"])

  def test_github_non_participant_pr_marks_is_watching(self):
    gh_json = json.dumps({
        "number": 195300,
        "title": "[web] WIP: Refactor engine canvas setup",
        "state": "OPEN",
        "isDraft": True,
        "author": {"login": "mdebbar"},
        "mergedAt": None,
        "headRefOid": "1234567890abcdef",
        "reviews": [],
        "comments": [],
        "statusCheckRollup": [{"name": "Linux test", "conclusion": "FAILURE"}],
        "url": "https://github.com/flutter/flutter/pull/195300",
        "mergeable": "MERGEABLE",
        "reviewRequests": [],
    })
    with mock.patch.object(w.subprocess, "check_output", return_value=gh_json):
      res = w.fetch_github_pr_status("195300")

    self.assertFalse(res["is_self"])
    self.assertFalse(res["is_draft"])
    self.assertFalse(res["in_development"])
    self.assertTrue(res["is_watching"])
    self.assertEqual(res["role"], "watching")
    self.assertFalse(res["in_my_attention"])
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")

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
    self.assertFalse(res["is_watching"])
    self.assertTrue(res["in_my_attention"])
    self.assertEqual(res["phase"], "NEEDS_YOUR_ACTION")

  def test_github_reviewer_pr_already_approved_clears_attention_and_watches(self):
    gh_json = json.dumps({
        "number": 195400,
        "title": "[web_paragraph] Fix cluster indexing",
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": "mdebbar"},
        "mergedAt": None,
        "headRefOid": "1234567890abcdef",
        "reviews": [
            {
                "author": {"login": "Rusino"},
                "state": "APPROVED",
                "body": "LGTM",
                "submittedAt": "2026-10-27T15:00:00Z",
            }
        ],
        "comments": [
            {
                "author": {"login": "mdebbar"},
                "body": "Thanks! Waiting on second reviewer.",
                "createdAt": "2026-10-27T15:10:00Z",
            }
        ],
        "statusCheckRollup": [],
        "url": "https://github.com/flutter/flutter/pull/195400",
        "mergeable": "MERGEABLE",
        "reviewRequests": [{"login": "eyebrowsoffire"}],
    })
    with mock.patch.object(w.subprocess, "check_output", return_value=gh_json):
      res = w.fetch_github_pr_status("195400")

    self.assertFalse(res["is_self"])
    self.assertEqual(res["role"], "reviewer")
    self.assertFalse(res["in_my_attention"])
    self.assertTrue(res["is_watching"])
    self.assertEqual(res["waiting_on"], [])
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")

  def test_dashboard_js_functions_render_draft_development_review_and_cq_precedence(self):
    self.assertNotIn("In review / development", w.DASHBOARD_HTML)
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.split("\nboot();", 1)[0] + """
const draftPr = {
  kind: 'github', status: 'OPEN', phase: 'WAITING_ON_OTHERS',
  is_draft: true, in_development: false, is_watching: false, tree_red: true,
  patchset: 1, url: 'https://example.com/1', cr_summary: '0 rev', cq_status: '🌲 Tree is red'
};
const devCl = {
  kind: 'gerrit', status: 'NEW', phase: 'WAITING_ON_OTHERS',
  is_draft: false, in_development: true, is_watching: false,
  patchset: 3, url: 'https://example.com/2', cr_summary: '0', cq_status: '✓ Dry Run Passed'
};
const devCqRunningCl = {
  kind: 'gerrit', status: 'NEW', phase: 'WAITING_ON_OTHERS',
  is_draft: false, in_development: true, is_watching: false,
  patchset: 3, url: 'https://example.com/3', cr_summary: '0', cq_status: 'CQ+1 (Dry Run)'
};
const watchingCl = {
  kind: 'gerrit', target_id: '1386096', status: 'NEW', phase: 'WAITING_ON_OTHERS',
  is_self: false, is_external: false, role: 'watching', is_draft: false, in_development: false,
  is_watching: true, merge_conflict: true, owner_email: 'jiameil@google.com',
  subject: '[agentic-review] Add starter Skia knowledge base rules',
  patchset: 5, url: 'https://example.com/1386096', cr_summary: '0', cq_status: 'CQ+1 (Dry Run)'
};
const reviewedColleagueCl = {
  kind: 'gerrit', target_id: '1402416', status: 'NEW', phase: 'WAITING_ON_OTHERS',
  is_self: false, is_external: false, role: 'reviewer (goog)', is_watching: true,
  in_my_attention: false, is_draft: false, in_development: false,
  owner_email: 'alexisdavidc@google.com',
  subject: 'Reland "[SkContext] Forwarding SkGraphics calls to SkContext"',
  patchset: 8, url: 'https://example.com/1402416', cr_summary: '+1', cq_status: '✓ Dry Run Passed'
};
const externalCl = {
  kind: 'gerrit', target_id: '1407016', is_self: false, is_external: true,
  owner_name: 'KC Yeung', owner_email: 'I@YeungKC.com'
};
const reviewCl = {
  kind: 'gerrit', status: 'NEW', phase: 'WAITING_ON_OTHERS',
  is_draft: false, in_development: false, is_watching: false,
  patchset: 2, url: 'https://example.com/4', cr_summary: '+1', cq_status: 'Idle'
};
const waitingOnCl = Object.assign({}, reviewCl, {
  waiting_on: [
    {id: 'alexisdavidc@google.com', active: true}, {id: 'gone@google.com', active: false}
  ]
});
const treeRedPr = {
  kind: 'github', status: 'OPEN', phase: 'WAITING_ON_OTHERS',
  is_draft: false, in_development: false, is_watching: false, tree_red: true,
  patchset: 1, url: 'https://example.com/7', cr_summary: '1 rev', cq_status: '🌲 Tree is red'
};
const ownConflictCl = Object.assign({}, reviewCl, {is_self: true, merge_conflict: true});
const alertCl = {
  kind: 'gerrit', status: 'MERGED', phase: 'ALERT_ROLL_OR_STATUS', is_self: false,
  post_submit: {has_alert: true, all_passed: false}
};
const abandonedCl = {
  kind: 'gerrit', status: 'ABANDONED', phase: 'ABANDONED',
  is_draft: false, in_development: false, patchset: 1, url: 'https://example.com/5'
};
const closedPr = {
  kind: 'github', status: 'CLOSED', phase: 'ABANDONED',
  is_draft: false, in_development: false, patchset: 1, url: 'https://example.com/6'
};
const box = {jetski_box_url: 'https://jetski.corp.google.com/box/host'};
const ownCqFail = {
  kind: 'gerrit', status: 'NEW', phase: 'ALERT_CQ_FAILED', is_self: true, cq_failed: true,
  post_submit: null, url: 'https://example.com/9', patchset: 2,
  status_changed_et: '16:00 ET', status_changed_age: '5m ago'
};
const busyAttentionCl = {
  kind: 'gerrit', target_id: '1408436', status: 'NEW', phase: 'NEEDS_YOUR_ACTION',
  in_my_attention: true, is_self: false, chat_busy: true, conversation_id: 'a2e9e5f4',
  project_name: 'SkParagraph', subject: '[skparagraph] Avoid indexing empty fUTF16IndexForUTF8Index',
  patchset: 1, url: 'https://example.com/1408436', last_msg_author: 'author@example.com',
  last_msg_age: '15m ago', last_msg_snippet: 'Uploaded patch set 1.'
};
const piperCl = {kind: 'piper', status: 'NEW', phase: 'WAITING_ON_OTHERS'};
console.log(JSON.stringify({
  draftStatus: statusOf(draftPr),
  devStatus: statusOf(devCl),
  devCqStatus: statusOf(devCqRunningCl),
  watchingStatus: statusOf(watchingCl),
  reviewedColleagueStatus: statusOf(reviewedColleagueCl),
  reviewStatus: statusOf(reviewCl),
  lanes: [draftPr, devCl, devCqRunningCl, watchingCl, reviewedColleagueCl, reviewCl, treeRedPr,
    ownConflictCl, alertCl, abandonedCl, closedPr].map(laneOf),
  draftState: stateHtml(draftPr),
  devState: stateHtml(devCl),
  watchingState: stateHtml(watchingCl),
  reviewState: stateHtml(reviewCl),
  waitingOnState: stateHtml(waitingOnCl),
  treeRedState: stateHtml(treeRedPr),
  abandonedState: stateHtml(abandonedCl),
  closedState: stateHtml(closedPr),
  colleagueAuthor: authorLabel(reviewedColleagueCl),
  externalAuthor: authorLabel(externalCl),
  cleanedSubject: cleanSubject(watchingCl.subject),
  chat: chatUrl({conversation_id: '9db15645-df20'}, box),
  chatWithoutBox: chatUrl({conversation_id: '9db15645-df20'}, {}),
  task: shortTask(
    'Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-NativeFonts_FreeType_ASAN_Upload'),
  headlineZero: headlineHtml(0, 'ALL'),
  headlineZeroOneBusy: headlineHtml(0, 'ALL', 1),
  headlineZeroTwoBusyInProj: headlineHtml(0, 'SkParagraph', 2),
  headlineOneAndOneBusy: headlineHtml(1, 'ALL', 1),
  headlineOneInProject: headlineHtml(1, 'Skia'),
  headlineTwo: headlineHtml(2, 'ALL'),
  ownCqFailCard: focusCard(ownCqFail, box),
  busyCard: focusCard(busyAttentionCl, box),
  piperState: stateHtml(piperCl),
}));
"""
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=True
    )
    out = json.loads(proc.stdout.strip())
    self.assertEqual(out["draftStatus"], "draft")
    self.assertEqual(out["devStatus"], "dev")
    self.assertEqual(out["devCqStatus"], "cq")
    self.assertEqual(out["watchingStatus"], "watching")
    self.assertEqual(out["reviewedColleagueStatus"], "watching")
    self.assertEqual(out["reviewStatus"], "waiting")
    self.assertEqual(
        out["lanes"],
        ["desk", "desk", "landing", "watching", "watching", "waiting", "waiting",
         "you", "you", "done", "done"],
    )
    self.assertTrue(out["draftState"].startswith("Draft · "))
    self.assertTrue(out["devState"].startswith("No reviewers yet · "))
    self.assertIn("CR 0", out["watchingState"])
    self.assertIn("In review · CR +1", out["reviewState"])
    self.assertIn("Waiting on <b>", out["waitingOnState"])
    self.assertIn("alexisdavidc", out["waitingOnState"])
    self.assertIn("(left Google)", out["waitingOnState"])
    self.assertIn("CR +1", out["waitingOnState"])
    self.assertIn("is red; landing waits for it", out["treeRedState"])
    self.assertEqual(out["abandonedState"], "Abandoned")
    self.assertEqual(out["closedState"], "Closed")
    self.assertEqual(out["colleagueAuthor"], "alexisdavidc")
    self.assertEqual(out["externalAuthor"], "KC Yeung (external)")
    self.assertEqual(out["cleanedSubject"], "Add starter Skia knowledge base rules")
    self.assertEqual(out["chat"], "https://jetski.corp.google.com/box/host/c/9db15645-df20")
    self.assertEqual(out["chatWithoutBox"], "")
    self.assertEqual(out["task"], "NativeFonts FreeType ASAN · Debug · Ubuntu 24.04")
    self.assertIn("Nothing needs you.", out["headlineZero"])
    self.assertEqual(
        out["headlineZeroOneBusy"],
        'Nothing needs you right now. <span class="soft">One chat is working.</span>',
    )
    self.assertEqual(
        out["headlineZeroTwoBusyInProj"],
        'Nothing in SkParagraph needs you right now. '
        '<span class="soft">Two chats are working.</span>',
    )
    self.assertEqual(
        out["headlineOneAndOneBusy"],
        'One thing needs you. <span class="soft">One chat is working.</span>',
    )
    self.assertEqual(out["headlineOneInProject"], "One thing in Skia needs you.")
    self.assertEqual(out["headlineTwo"], "Two things need you.")
    self.assertIn("The commit queue failed · 16:00 ET (5m ago)", out["ownCqFailCard"])
    self.assertIn("https://example.com/9?tab=checks", out["ownCqFailCard"])
    self.assertNotIn("Post-submit alert", out["ownCqFailCard"])
    self.assertNotIn("status.skia.org", out["ownCqFailCard"])
    self.assertIn("Chat is working", out["busyCard"])
    self.assertIn("Open chat", out["busyCard"])
    self.assertNotIn("Working in chat", out["busyCard"])
    self.assertEqual(out["piperState"], "In review")

  def test_dashboard_has_no_manual_chat_actions_or_browser_alerts(self):
    for removed in (
        "conversation://", "/api/wake", "/api/finalize", "/api/archive_gmail",
        "Notification", "AudioContext", "Ping",
    ):
      self.assertNotIn(removed, w.DASHBOARD_HTML, removed)
    for path in ("/api/wake", "/api/finalize", "/api/archive_gmail"):
      handler = w.FlightDeckHandler.__new__(w.FlightDeckHandler)
      handler.path = path
      sent = {}
      handler._send_json = lambda payload, code=200, sent=sent: sent.update(code=code)
      handler.do_POST()
      self.assertEqual(sent["code"], 404, path)


if __name__ == "__main__":
  unittest.main()
