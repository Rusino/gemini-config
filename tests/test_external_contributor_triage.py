#!/usr/bin/env python3
"""Contract tests for external contributor AUTHORS verification and Option 1 queue heads-up."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
REVIEW_TRIAGE_SCRIPTS_DIR = os.path.join(
    REPO_ROOT, "skills", "review-triage", "scripts"
)
for p in (SCRIPTS_DIR, REVIEW_TRIAGE_SCRIPTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import cl_watcher as w  # pylint: disable=g-import-not-at-top
import finalize_review as fr  # pylint: disable=g-import-not-at-top
import run_queue_triage as rqt  # pylint: disable=g-import-not-at-top
import triage_cl as tc  # pylint: disable=g-import-not-at-top


SAMPLE_AUTHORS = """# Names should be added to this file with this pattern:
# Name or Organization <email address>

Google Inc. <*@google.com>
The Chromium Authors <*@chromium.org>
KC Yeung <I@YeungKC.com>
Acme Corp <*@acme-corp.org>
"""


class ExternalContributorTriageTest(unittest.TestCase):
    """Verify AUTHORS matching, exemptions, and queue-ack visibility preservation."""

    def test_is_external_gerrit_email_filters_internal_and_autoroll(self) -> None:
        # Internal Google/Chromium/service accounts and autorollers are not external humans
        self.assertFalse(tc.is_external_gerrit_email("jlavrova@google.com"))
        self.assertFalse(tc.is_external_gerrit_email("dev@chromium.org"))
        self.assertFalse(
            tc.is_external_gerrit_email("bot@skia-public.iam.gserviceaccount.com")
        )
        self.assertFalse(
            tc.is_external_gerrit_email("skia-autoroll@skia-public.iam.gserviceaccount.com")
        )
        self.assertTrue(tc.is_external_gerrit_email("contributor@example.com"))
        self.assertTrue(tc.is_external_gerrit_email("I@YeungKC.com"))
        self.assertTrue(tc.is_external_gerrit_email("behdad.esfahbod@gmail.com"))

    def test_match_email_in_authors_supports_exact_and_wildcard_entries(self) -> None:
        # Mirrors SkCQ AuthorsVerifier wildcard and case-insensitive matching
        exact = tc.match_email_in_authors_text("i@yeungkc.com", SAMPLE_AUTHORS)
        self.assertEqual(exact, "KC Yeung <I@YeungKC.com>", f"got {exact!r}")
        wildcard = tc.match_email_in_authors_text("alice@acme-corp.org", SAMPLE_AUTHORS)
        self.assertEqual(wildcard, "Acme Corp <*@acme-corp.org>", f"got {wildcard!r}")
        missing = tc.match_email_in_authors_text("newdev@external.io", SAMPLE_AUTHORS)
        self.assertEqual(missing, "", f"got {missing!r}")

    def test_evaluate_external_authors_missing_builds_both_templates(self) -> None:
        # Missing external contributor gets both AUTHORS request and Option 1 queue heads-up
        cl_data = {
            "host": "skia-review.googlesource.com",
            "owner": "New Contributor",
            "owner_email": "newdev@external.io",
            "detail": {},
        }
        res = tc.evaluate_external_authors_check(
            cl_data, files=[], authors_text_override=SAMPLE_AUTHORS
        )
        self.assertTrue(res["applicable"])
        self.assertFalse(res["verified"])
        self.assertFalse(res["exempt_from_messages"])
        self.assertFalse(res["exempt_from_queue_heads_up"])
        self.assertIn("newdev@external.io", res["authors_request_comment"])
        self.assertIn("https://skia.org/docs/dev/contrib/", res["authors_request_comment"])
        self.assertIn(tc.QUEUE_HEADS_UP_TEXT, res["queue_heads_up_comment"])
        self.assertIn(res["authors_request_comment"], res["queue_heads_up_comment"])
        rendered = "\n".join(tc._render_external_authors_section(res))
        self.assertIn("MISSING (`newdev@external.io` not in `AUTHORS` or CL diff)", rendered)
        self.assertIn("Queue Heads-Up (Option 1 — Ask User First)", rendered)

    def test_evaluate_external_authors_added_in_cl_diff_marks_verified(self) -> None:
        # Adding the contributor's email to AUTHORS in the CL diff satisfies the check
        f_authors = tc.ChangedFile("AUTHORS")
        hunk = tc.DiffHunk(10, 1, 10, 2, "@@ -10,1 +10,2 @@")
        hunk.lines = ["+New Contributor <newdev@external.io>"]
        f_authors.hunks.append(hunk)
        cl_data = {
            "host": "skia-review.googlesource.com",
            "owner": "New Contributor",
            "owner_email": "newdev@external.io",
            "detail": {},
        }
        res = tc.evaluate_external_authors_check(
            cl_data, files=[f_authors], authors_text_override=SAMPLE_AUTHORS
        )
        self.assertTrue(res["verified"])
        self.assertTrue(res["added_in_cl"])
        self.assertEqual(res["authors_request_comment"], "", f"got {res['authors_request_comment']!r}")
        self.assertEqual(
            res["queue_heads_up_comment"],
            tc.QUEUE_HEADS_UP_TEXT,
            f"got {res['queue_heads_up_comment']!r}",
        )

    def test_kc_yeung_and_behdad_esfahbod_exemptions(self) -> None:
        # KC Yeung is exempt from slow-queue heads-up; Behdad is exempt from all boilerplate
        kc_cl = {
            "host": "skia-review.googlesource.com",
            "owner": "KC Yeung",
            "owner_email": "I@YeungKC.com",
            "detail": {},
        }
        kc_res = tc.evaluate_external_authors_check(
            kc_cl, files=[], authors_text_override=SAMPLE_AUTHORS
        )
        self.assertTrue(kc_res["verified"])
        self.assertTrue(kc_res["exempt_from_queue_heads_up"])
        self.assertFalse(kc_res["exempt_from_messages"])
        self.assertEqual(kc_res["queue_heads_up_comment"], "")

        behdad_cl = {
            "host": "skia-review.googlesource.com",
            "owner": "Behdad Esfahbod",
            "owner_email": "behdad.esfahbod@gmail.com",
            "detail": {},
        }
        behdad_res = tc.evaluate_external_authors_check(
            behdad_cl, files=[], authors_text_override=""
        )
        self.assertTrue(behdad_res["exempt_from_messages"])
        self.assertTrue(behdad_res["exempt_from_queue_heads_up"])
        self.assertEqual(behdad_res["authors_request_comment"], "")
        self.assertEqual(behdad_res["queue_heads_up_comment"], "")
        rendered = "\n".join(tc._render_external_authors_section(behdad_res))
        self.assertIn("Exempt (`Behdad Esfahbod`)", rendered)

    def test_queue_heads_up_preserves_awaiting_reviewer_and_needs_your_action(self) -> None:
        # Posting the queue heads-up must never mark a Gerrit CL or GitHub PR as already reviewed
        gerrit_change = {
            "owner": {"email": "ext@example.com", "name": "Ext Dev"},
            "current_revision": "sha2",
            "revisions": {
                "sha2": {
                    "_number": 2,
                    "created": "2026-11-13 10:00:00.000000000",
                    "commit": {"parents": [{"commit": "sha1"}]},
                }
            },
            "messages": [
                {
                    "_revision_number": 2,
                    "date": "2026-11-13 10:00:00.000000000",
                    "tag": "autogenerated:gerrit:newPatchSet",
                    "author": {"email": "ext@example.com", "name": "Ext Dev"},
                    "message": "Uploaded patch set 2.",
                },
                {
                    "_revision_number": 2,
                    "date": "2026-11-13 11:00:00.000000000",
                    "author": {"email": "jlavrova@google.com", "name": "Julia Lavrova"},
                    "message": f"Patch Set 2:\n\n{tc.QUEUE_HEADS_UP_TEXT}",
                },
            ],
        }
        self.assertTrue(rqt.is_awaiting_reviewer(gerrit_change))
        tctx = tc.extract_gerrit_thread_context(gerrit_change, {})
        self.assertFalse(tctx["reviewed_current_ps"])
        state, rev, cnt = w._parse_gerrit_self_review(gerrit_change["messages"], 0, 2)
        self.assertEqual(state, "", f"got state={state!r}, rev={rev}, cnt={cnt}")
        self.assertEqual(cnt, 0, f"got cnt={cnt}")

        gh_detail = {
            "author": {"login": "external-user"},
            "createdAt": "2026-11-13T10:00:00Z",
            "commits": [{"committedDate": "2026-11-13T10:00:00Z"}],
            "reviews": [],
            "comments": [
                {
                    "author": {"login": "Rusino"},
                    "createdAt": "2026-11-13T11:00:00Z",
                    "body": rqt.QUEUE_HEADS_UP_TEXT,
                }
            ],
        }
        self.assertTrue(rqt.is_github_pr_awaiting_reviewer(gh_detail))

    def test_cl_watcher_github_is_external_distinguishes_googler_vs_external(self) -> None:
        # GitHub PRs from MEMBER authors have is_external=False; non-members/awaiting-CICD get True
        def fake_gh_json(owner_login: str, assoc: str) -> str:
            import json
            return json.dumps({
                "number": 195000,
                "title": "[web_paragraph] Fix cluster range",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "author": {"login": owner_login},
                "reviewDecision": "",
                "reviewRequests": [{"login": "Rusino"}],
                "reviews": [],
                "comments": [
                    {
                        "author": {"login": owner_login},
                        "authorAssociation": assoc,
                        "createdAt": "2026-11-13T10:30:00Z",
                        "body": "PTAL",
                    },
                    {
                        "author": {"login": "Rusino"},
                        "createdAt": "2026-11-13T11:00:00Z",
                        "body": w.QUEUE_HEADS_UP_TEXT,
                    },
                ],
                "commits": [{"committedDate": "2026-11-13T10:00:00Z"}],
                "files": [
                    {"path": "engine/src/flutter/lib/web_ui/lib/src/engine/text/paragraph.dart"}
                ],
                "statusCheckRollup": [],
            })

        with mock.patch("subprocess.check_output", return_value=fake_gh_json("mdebbar", "MEMBER")):
            googler_pr = w.fetch_github_pr_status("flutter/flutter", 195000)
        assert googler_pr is not None
        self.assertFalse(googler_pr["is_external"])
        self.assertEqual(
            googler_pr["self_review_state"], "", f"got {googler_pr['self_review_state']!r}"
        )

        with mock.patch(
            "subprocess.check_output", return_value=fake_gh_json("external-dev", "CONTRIBUTOR")
        ):
            ext_pr = w.fetch_github_pr_status("flutter/flutter", 195000)
        assert ext_pr is not None
        self.assertTrue(ext_pr["is_external"])
        self.assertEqual(ext_pr["self_review_state"], "", f"got {ext_pr['self_review_state']!r}")

    def test_finalize_review_authors_gate_blocks_missing_and_allows_verified_or_exempt(self) -> None:
        # finalize_review blocks CR+1 when an external author is missing from AUTHORS
        info_ext = {
            "owner": {"email": "newdev@external.io", "name": "New Dev"},
            "current_revision": "sha1",
            "revisions": {"sha1": {"commit": {"author": {"email": "newdev@external.io"}}}},
        }
        missing_gate = fr._evaluate_authors_gate(
            "skia-review.googlesource.com",
            info_ext,
            ["src/core/SkCanvas.cpp"],
            authors_text_override=SAMPLE_AUTHORS,
        )
        self.assertTrue(missing_gate["required"])
        self.assertEqual(missing_gate["status"], "missing", f"got {missing_gate}")

        in_cl_gate = fr._evaluate_authors_gate(
            "skia-review.googlesource.com",
            info_ext,
            ["AUTHORS", "src/core/SkCanvas.cpp"],
            authors_text_override=SAMPLE_AUTHORS,
        )
        self.assertTrue(in_cl_gate["required"])
        self.assertEqual(in_cl_gate["status"], "passed", f"got {in_cl_gate}")
        self.assertEqual(in_cl_gate["source"], "cl_files", f"got {in_cl_gate}")

        info_wildcard = {
            "owner": {"email": "bob@acme-corp.org", "name": "Bob"},
            "current_revision": "sha1",
            "revisions": {"sha1": {"commit": {"author": {"email": "bob@acme-corp.org"}}}},
        }
        repo_gate = fr._evaluate_authors_gate(
            "skia-review.googlesource.com",
            info_wildcard,
            ["src/core/SkCanvas.cpp"],
            authors_text_override=SAMPLE_AUTHORS,
        )
        self.assertTrue(repo_gate["required"])
        self.assertEqual(repo_gate["status"], "passed", f"got {repo_gate}")
        self.assertEqual(repo_gate["source"], "repo_authors", f"got {repo_gate}")

        info_behdad = {
            "owner": {"email": "behdad.esfahbod@gmail.com", "name": "Behdad Esfahbod"},
            "current_revision": "sha1",
            "revisions": {"sha1": {"commit": {"author": {"email": "behdad.esfahbod@gmail.com"}}}},
        }
        behdad_gate = fr._evaluate_authors_gate(
            "skia-review.googlesource.com",
            info_behdad,
            ["src/core/SkCanvas.cpp"],
            authors_text_override="",
        )
        self.assertFalse(behdad_gate["required"])
        self.assertEqual(behdad_gate["status"], "exempt", f"got {behdad_gate}")


if __name__ == "__main__":
    unittest.main()
