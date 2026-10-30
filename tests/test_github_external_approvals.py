#!/usr/bin/env python3
"""Contract tests for external contributor PRs, CICD status, and triage accuracy."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top
import triage_store as ts  # pylint: disable=g-import-not-at-top


class GithubExternalApprovalsContractTest(unittest.TestCase):

  def test_external_contributor_pr_requires_two_member_approvals_even_when_github_says_approved(
      self,
  ):
    # GitHub sets reviewDecision to APPROVED on 1 approval even when Cocoon needs 2
    pr_payload = {
        "number": 195384,
        "title": "[web] Support text selection bounds",
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": "dbebawy"},
        "headRefOid": "head1234567890",
        "headRefName": "fix-bounds",
        "reviewDecision": "APPROVED",
        "labels": [{"name": "engine"}, {"name": "web"}],
        "latestReviews": [
            {
                "author": {"login": "mdebbar"},
                "state": "APPROVED",
                "authorAssociation": "MEMBER",
            }
        ],
        "reviews": [
            {
                "author": {"login": "mdebbar"},
                "state": "APPROVED",
                "authorAssociation": "MEMBER",
            }
        ],
        "comments": [
            {
                "author": {"login": "dbebawy"},
                "authorAssociation": "CONTRIBUTOR",
                "body": "PTAL",
            }
        ],
        "reviewRequests": [{"login": "jlavrova"}],
        "statusCheckRollup": [],
        "mergeable": "MERGEABLE",
    }

    with mock.patch.object(
        w.subprocess, "check_output", return_value=json.dumps(pr_payload)
    ):
      res = w.fetch_github_pr_status("195384")

    self.assertTrue(res["ok"])
    self.assertEqual(res["required_approvals"], 2)
    self.assertEqual(res["approved_count"], 1)
    self.assertEqual(res["missing_approvals"], 1)
    self.assertEqual(res["cr_summary"], "✓ 1/2")
    self.assertEqual(res["reviewer_status"], "Needs +1 approval (1/2 approved)")

    # Non-member approvals do not increment member approval count
    pr_payload["latestReviews"].append({
        "author": {"login": "random-contributor"},
        "state": "APPROVED",
        "authorAssociation": "CONTRIBUTOR",
    })
    with mock.patch.object(
        w.subprocess, "check_output", return_value=json.dumps(pr_payload)
    ):
      res_contrib = w.fetch_github_pr_status("195384")
    self.assertEqual(res_contrib["approved_count"], 1)
    self.assertEqual(res_contrib["missing_approvals"], 1)

    # 2 member approvals satisfy the requirement and clear missing approvals
    pr_payload["latestReviews"] = [
        {
            "author": {"login": "mdebbar"},
            "state": "APPROVED",
            "authorAssociation": "MEMBER",
        },
        {
            "author": {"login": "Rusino"},
            "state": "APPROVED",
            "authorAssociation": "MEMBER",
        },
    ]
    with mock.patch.object(
        w.subprocess, "check_output", return_value=json.dumps(pr_payload)
    ):
      res_two = w.fetch_github_pr_status("195384")
    self.assertEqual(res_two["approved_count"], 2)
    self.assertEqual(res_two["missing_approvals"], 0)
    self.assertEqual(res_two["reviewer_status"], "Approved")
    self.assertEqual(res_two["cr_summary"], "✓ 2 app")

  def test_awaiting_cicd_label_surfaces_explicit_cq_status(self):
    # External PR awaiting member verification queues on the CICD label check
    pr_payload = {
        "number": 195384,
        "title": "[web] Support text selection bounds",
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": "dbebawy"},
        "headRefOid": "head1234567890",
        "labels": [{"name": "engine"}],
        "statusCheckRollup": [
            {"name": "Awaiting CICD label", "status": "QUEUED", "conclusion": None},
            {"name": "Google testing", "status": "IN_PROGRESS", "conclusion": None},
        ],
        "reviews": [],
        "comments": [],
        "reviewRequests": [],
    }

    with mock.patch.object(
        w.subprocess, "check_output", return_value=json.dumps(pr_payload)
    ):
      res = w.fetch_github_pr_status("195384")
    self.assertTrue(res.get("awaiting_cicd_label"))
    self.assertEqual(res["cq_status"], "🏷️ Needs CICD label")

    # Once a flutter-hackers member adds the CICD label, the alert clears
    pr_payload["labels"].append({"name": "CICD"})
    with mock.patch.object(
        w.subprocess, "check_output", return_value=json.dumps(pr_payload)
    ):
      res_labeled = w.fetch_github_pr_status("195384")
    self.assertFalse(res_labeled.get("awaiting_cicd_label"))

  def test_triage_store_does_not_claim_already_approved_when_unapproved_or_missing_2nd(self):
    # Fenced reply draft extraction from audit report
    audit_text_fenced = (
        "### PR 195384 review\n"
        "Reply draft (inline, under his comment 4403413656):\n"
        "---\n"
        "Thanks for folding them in.\n"
        "---\n"
    )
    draft = ts.extract_draft_from_audit(audit_text_fenced)
    self.assertEqual(draft, "Thanks for folding them in.")

    # Audit without draft or approval claims should not state already approved
    audit_no_draft = (
        "### PR #195384 (flutter/flutter)\n"
        "- **Category:** Surgical audit\n"
        "The change looks reasonable.\n"
    )
    parsed = ts.parse_single_audit_text(audit_no_draft)
    self.assertIsNotNone(parsed)
    self.assertEqual(parsed["summary"], "No comment needed")

    # Overlay triage rewrites already approved if user has not approved
    items = [{
        "item_key": "github:195384",
        "self_approved": False,
        "missing_approvals": 1,
    }]
    mock_triage = {
        "github:195384": {
            "summary": "No comment needed (already approved)",
            "verdict": "APPROVED",
        }
    }
    with mock.patch.object(ts, "load_all_triage_results", return_value=mock_triage):
      ts.overlay_triage_on_items(items, ingest_tmp=False)
    self.assertEqual(items[0]["triage"]["summary"], "No comment needed")

    # If self approved but 2nd approval is still missing, state needs +1
    items[0]["self_approved"] = True
    items[0]["missing_approvals"] = 1
    with mock.patch.object(ts, "load_all_triage_results", return_value=mock_triage):
      ts.overlay_triage_on_items(items, ingest_tmp=False)
    self.assertEqual(items[0]["triage"]["summary"], "You approved; needs +1 more approval")


if __name__ == "__main__":
  unittest.main()
