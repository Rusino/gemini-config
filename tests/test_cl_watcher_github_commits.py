#!/usr/bin/env python3
"""Contract tests for GitHub PR commit timeline in cl_watcher.py."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


class GitHubPrCommitTimelineTest(unittest.TestCase):

  def test_author_commit_after_gemini_review_clears_needs_your_action(self):
    gh_json = json.dumps({
        "number": 194980,
        "title": "[web_paragraph] Fix multi-span BiDi span order and region direction",
        "state": "OPEN",
        "author": {"login": "Rusino"},
        "mergedAt": None,
        "headRefOid": "84b63c2112680f558ac00bab69e5cc461d7496f1",
        "reviews": [
            {
                "author": {"login": "gemini-code-assist"},
                "state": "COMMENTED",
                "body": "## Code Review\n\nMutating paragraph.spans in-place is risky.",
                "submittedAt": "2026-10-21T18:20:36Z",
            }
        ],
        "comments": [],
        "commits": [
            {
                "authoredDate": "2026-10-21T18:15:53Z",
                "committedDate": "2026-10-22T20:42:21Z",
                "authors": [{"login": "Rusino", "email": "jlavrova@google.com"}],
                "messageHeadline": (
                    "[web_paragraph] Avoid mutating paragraph.spans in-place"
                ),
                "oid": "354ca12554bb118dba594bf29c3b04501952a9c2",
            },
            {
                "authoredDate": "2026-10-23T21:56:31Z",
                "committedDate": "2026-10-23T21:56:31Z",
                "authors": [{"login": "Rusino", "email": "Rusino@users.noreply.github.com"}],
                "messageHeadline": (
                    "Merge branch 'master' into webparagraph_multi_span_bidi"
                ),
                "oid": "84b63c2112680f558ac00bab69e5cc461d7496f1",
            },
        ],
        "statusCheckRollup": [{"name": "Linux web_tests", "conclusion": "SUCCESS"}],
        "url": "https://github.com/flutter/flutter/pull/194980",
        "mergeable": "MERGEABLE",
        "reviewRequests": [],
    })
    with mock.patch.object(
        w.subprocess, "check_output", return_value=gh_json
    ) as mock_cmd:
      res = w.fetch_github_pr_status("194980")

    self.assertIn("commits", mock_cmd.call_args[0][0][-1])
    self.assertTrue(res["ok"])
    self.assertEqual(res["last_msg_author"], "Rusino")
    self.assertEqual(res["last_msg_date"], "2026-10-23T21:56:31Z")
    self.assertFalse(res["in_my_attention"])
    self.assertEqual(res["phase"], "WAITING_ON_OTHERS")

  def test_reviewer_comment_after_latest_commit_keeps_needs_your_action(self):
    gh_json = json.dumps({
        "number": 194980,
        "title": "[web_paragraph] Fix multi-span BiDi span order and region direction",
        "state": "OPEN",
        "author": {"login": "Rusino"},
        "mergedAt": None,
        "headRefOid": "84b63c2112680f558ac00bab69e5cc461d7496f1",
        "reviews": [
            {
                "author": {"login": "mdebbar"},
                "state": "CHANGES_REQUESTED",
                "body": "Please add a test for mixed RTL/LTR placeholders.",
                "submittedAt": "2026-10-24T12:00:00Z",
            }
        ],
        "comments": [],
        "commits": [
            {
                "authoredDate": "2026-10-23T21:56:31Z",
                "committedDate": "2026-10-23T21:56:31Z",
                "authors": [{"login": "Rusino", "email": "jlavrova@google.com"}],
                "messageHeadline": "Merge branch 'master'",
                "oid": "84b63c2112680f558ac00bab69e5cc461d7496f1",
            }
        ],
        "statusCheckRollup": [{"name": "Linux web_tests", "conclusion": "SUCCESS"}],
        "url": "https://github.com/flutter/flutter/pull/194980",
        "mergeable": "MERGEABLE",
        "reviewRequests": [],
    })
    with mock.patch.object(w.subprocess, "check_output", return_value=gh_json):
      res = w.fetch_github_pr_status("194980")

    self.assertTrue(res["ok"])
    self.assertEqual(res["last_msg_author"], "mdebbar")
    self.assertTrue(res["in_my_attention"])
    self.assertEqual(res["phase"], "NEEDS_YOUR_ACTION")


if __name__ == "__main__":
  unittest.main()
