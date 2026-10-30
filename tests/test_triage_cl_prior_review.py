#!/usr/bin/env python3
"""Contract tests for prior-review detection in review-triage/scripts/triage_cl.py."""

import os
import sys
import unittest

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "skills", "review-triage", "scripts")
    ),
)

import triage_cl  # noqa: E402

ME = {"email": "jlavrova@google.com", "name": "Julia Lavrova"}
AUTHOR = {"email": "author@example.com", "name": "Author"}


def _detail(cur_ps, messages, labels=None):
    revisions = {
        f"sha{n}": {"_number": n, "ref": f"refs/changes/00/1/{n}", "commit": {"parents": []}}
        for n in range(1, cur_ps + 1)
    }
    return {
        "_number": 1,
        "owner": AUTHOR,
        "current_revision": f"sha{cur_ps}",
        "revisions": revisions,
        "messages": messages,
        "labels": labels or {},
    }


class PriorReviewDetectionTest(unittest.TestCase):

    def test_vote_only_message_on_current_patchset_counts_as_reviewed(self):
        detail = _detail(
            7,
            [{"author": ME, "_revision_number": 7, "message": "Patch Set 7: Code-Review+1"}],
            {"Code-Review": {"all": [{**ME, "value": 1}, {**AUTHOR, "value": 0}]}},
        )
        ctx = triage_cl.extract_gerrit_thread_context(detail, {})
        self.assertTrue(ctx["reviewed_current_ps"])
        self.assertEqual(ctx["self_votes"], ["Code-Review+1"])

    def test_review_on_current_patchset_wins_over_older_one(self):
        detail = _detail(
            2,
            [
                {"author": ME, "_revision_number": 1, "message": "Patch Set 1:\n\nOne question"},
                {"author": ME, "_revision_number": 2, "message": "Patch Set 2: Code-Review+1"},
            ],
        )
        ctx = triage_cl.extract_gerrit_thread_context(detail, {})
        self.assertTrue(ctx["reviewed_current_ps"])
        self.assertEqual(ctx["last_reviewed_ps"], 1)

    def test_review_on_older_patchset_only_is_follow_up(self):
        detail = _detail(
            3,
            [{"author": ME, "_revision_number": 2, "message": "Patch Set 2: Code-Review+1"}],
        )
        ctx = triage_cl.extract_gerrit_thread_context(detail, {})
        self.assertFalse(ctx["reviewed_current_ps"])
        self.assertEqual(ctx["last_reviewed_ps"], 2)
        self.assertEqual(ctx["self_votes"], [])

    def test_no_activity_from_me_is_initial_review(self):
        detail = _detail(
            1,
            [{"author": AUTHOR, "_revision_number": 1, "message": "Uploaded patch set 1."}],
        )
        ctx = triage_cl.extract_gerrit_thread_context(detail, {})
        self.assertFalse(ctx["reviewed_current_ps"])
        self.assertIsNone(ctx["last_reviewed_ps"])


if __name__ == "__main__":
    unittest.main()
