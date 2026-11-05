#!/usr/bin/env python3
"""Contract: a co-reviewer reply after my COMMENTED review keeps the PR off Needs You."""

import datetime
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


def _ts(minutes_ago: int) -> str:
  t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
  return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _payload(last_review_login: str, last_review_state: str) -> dict:
  return {
      "number": 188906,
      "title": "[Impeller] Render COLR/CPAL color text as vector paths",
      "state": "OPEN",
      "isDraft": False,
      "author": {"login": "AnasQasem"},
      "headRefOid": "head1234567890",
      "headRefName": "colr",
      "reviewDecision": "CHANGES_REQUESTED",
      "labels": [{"name": "engine"}],
      "commits": [{"oid": "c1", "committedDate": _ts(300), "messageHeadline": "COLR"}],
      "reviews": [
          {"author": {"login": "Rusino"}, "state": "COMMENTED", "submittedAt": _ts(120)},
          {
              "author": {"login": last_review_login},
              "state": last_review_state,
              "submittedAt": _ts(60),
          },
      ],
      "latestReviews": [],
      "comments": [],
      "reviewRequests": [],
      "statusCheckRollup": [],
      "mergeable": "MERGEABLE",
  }


class GithubCoReviewerReplyContractTest(unittest.TestCase):

  def _fetch(self, payload: dict) -> dict:
    with mock.patch.object(w.subprocess, "check_output", return_value=json.dumps(payload)):
      return w.fetch_github_pr_status("188906")

  def test_coreviewer_changes_requested_after_my_comment_is_not_my_turn(self):
    res = self._fetch(_payload("gaaclarke", "CHANGES_REQUESTED"))
    self.assertTrue(res["ok"])
    self.assertFalse(res["in_my_attention"])

  def test_author_reply_after_my_comment_is_my_turn(self):
    res = self._fetch(_payload("AnasQasem", "COMMENTED"))
    self.assertTrue(res["ok"])
    self.assertTrue(res["in_my_attention"])


if __name__ == "__main__":
  unittest.main()
