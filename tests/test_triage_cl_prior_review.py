#!/usr/bin/env python3
"""Contract tests for prior-review and open-thread detection in review-triage."""

import os
import sys
import unittest

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "skills", "review-triage", "scripts")
    ),
)

import run_queue_triage
import triage_cl

ME = {"email": "jlavrova@google.com", "name": "Julia Lavrova"}
AUTHOR = {"email": "author@example.com", "name": "Author"}
REVIEWER_ALEXIS = {"email": "alexisdavidc@google.com", "name": "Alexis Cruz-Ayala"}


def _detail(cur_ps, messages, labels=None, revisions=None, owner=None):
    revs = revisions or {
        f"sha{n}": {"_number": n, "ref": f"refs/changes/00/1/{n}", "commit": {"parents": []}}
        for n in range(1, cur_ps + 1)
    }
    return {
        "_number": 1,
        "owner": owner or AUTHOR,
        "current_revision": f"sha{cur_ps}",
        "revisions": revs,
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


class OpenThreadsContractTest(unittest.TestCase):

    def test_cl_1408158_stale_old_patchset_and_author_replied(self):
        owner_kc = {"email": "i@yeungkc.com", "name": "KC Yeung"}
        author_reply_acct = {"email": "I@YeungKC.com", "name": "KC Yeung"}
        revisions = {
            "sha1": {
                "_number": 1,
                "created": "2026-10-28 14:00:00.000000000",
                "ref": "refs/changes/58/1408158/1",
                "commit": {"parents": []},
            },
            "sha2": {
                "_number": 2,
                "created": "2026-10-28 17:43:00.000000000",
                "ref": "refs/changes/58/1408158/2",
                "commit": {"parents": []},
            },
            "sha3": {
                "_number": 3,
                "created": "2026-10-29 17:25:03.000000000",
                "ref": "refs/changes/58/1408158/3",
                "commit": {"parents": []},
            },
            "sha4": {
                "_number": 4,
                "created": "2026-10-30 13:36:16.000000000",
                "ref": "refs/changes/58/1408158/4",
                "commit": {"parents": []},
            },
        }
        messages = [
            {
                "author": ME,
                "_revision_number": 4,
                "date": f"2026-10-30 18:{i:02d}:00.000000000",
                "message": f"Follow-up discussion item {i}",
            }
            for i in range(15)
        ]
        detail = _detail(4, messages, revisions=revisions, owner=owner_kc)
        detail["_number"] = 1408158
        inline_comments = {
            "/PATCHSET_LEVEL": [
                {
                    "id": "4636fc85_9006d0dd",
                    "author": REVIEWER_ALEXIS,
                    "patch_set": 1,
                    "unresolved": True,
                    "updated": "2026-10-28 17:24:25.000000000",
                    "message": "Add a null/length check in the C-string addText overloads.",
                },
                {
                    "id": "ffb909d3_5c9bbc63",
                    "in_reply_to": "4636fc85_9006d0dd",
                    "author": author_reply_acct,
                    "patch_set": 1,
                    "unresolved": False,
                    "updated": "2026-10-28 17:43:26.000000000",
                    "message": "Done.",
                },
            ],
            "modules/skparagraph/src/ParagraphBuilderImpl.cpp": [
                {
                    "id": "a16c65bd_79de480b",
                    "author": REVIEWER_ALEXIS,
                    "patch_set": 1,
                    "line": 106,
                    "unresolved": True,
                    "updated": "2026-10-30 17:20:37.000000000",
                    "message": (
                        "Unlike `SkString::append` (which safely no-ops on `nullptr` or "
                        "`len == 0`), `std::string::append(const char*)` has UB on nullptr."
                    ),
                },
                {
                    "id": "59be34ea_2a221780",
                    "in_reply_to": "a16c65bd_79de480b",
                    "author": author_reply_acct,
                    "patch_set": 1,
                    "line": 106,
                    "unresolved": True,
                    "updated": "2026-10-31 03:07:38.000000000",
                    "message": (
                        "Done in PS2 and later. Both overloads already check for nullptr, "
                        "and the length overload also skips len == 0."
                    ),
                },
            ],
        }

        ctx = triage_cl.extract_gerrit_thread_context(detail, inline_comments)
        self.assertEqual(len(ctx["open_threads"]), 1)
        t = ctx["open_threads"][0]
        self.assertEqual(t["root_author"], "Alexis Cruz-Ayala")
        self.assertEqual(t["root_email"], "alexisdavidc@google.com")
        self.assertEqual(t["root_role"], "reviewer")
        self.assertEqual(
            t["location"], "modules/skparagraph/src/ParagraphBuilderImpl.cpp:106"
        )
        self.assertEqual(t["patch_set"], 1)
        self.assertEqual(t["current_ps"], 4)
        self.assertEqual(t["newest_ps_at_post"], 4)
        self.assertEqual(t["last_role"], "author")
        self.assertTrue(t["stale"])
        self.assertTrue(t["author_replied"])
        self.assertEqual(t["tags"], ["STALE", "AUTHOR_REPLIED"])
        self.assertIn("SkString::append", t["root_excerpt"])
        self.assertIn("Done in PS2 and later", t["last_excerpt"])

        patch_text = (
            "diff --git a/modules/skparagraph/src/ParagraphBuilderImpl.cpp "
            "b/modules/skparagraph/src/ParagraphBuilderImpl.cpp\n"
            "--- a/modules/skparagraph/src/ParagraphBuilderImpl.cpp\n"
            "+++ b/modules/skparagraph/src/ParagraphBuilderImpl.cpp\n"
            "@@ -98,3 +98,6 @@ void ParagraphBuilderImpl::addText(const char* text) {\n"
            "+    if (text) {\n"
            "+        fUtf8.append(text);\n"
            "+    }\n"
        )
        cl_data = {
            "id": "1408158",
            "host": "skia-review.googlesource.com",
            "subject": "[skparagraph] Use std::string for the builder text",
            "owner": "KC Yeung",
            "owner_email": "I@YeungKC.com",
            "description": "Use std::string in ParagraphBuilderImpl to avoid quadratic copies.",
            "patch": patch_text,
            "patchset": 4,
            "thread_context": ctx,
            "detail": detail,
        }
        files = triage_cl.parse_patch(patch_text)
        analysis = triage_cl.analyze_review(cl_data, files)
        report = triage_cl.generate_report(cl_data, analysis, files)
        self.assertIn("Open threads", report)
        self.assertIn("STALE", report)
        self.assertIn("AUTHOR_REPLIED", report)
        self.assertIn("modules/skparagraph/src/ParagraphBuilderImpl.cpp:106", report)
        self.assertIn("Alexis Cruz-Ayala", report)
        self.assertIn("PS1", report)
        self.assertIn("PS4", report)
        self.assertIn("author", report)
        self.assertIn("Done in PS2 and later", report)

        # Daily queue helper surfaces external reviewer unresolved threads for agent verdict
        ext = run_queue_triage.external_open_threads({"metadata": {"thread_context": ctx}})
        self.assertEqual(len(ext), 1)
        cell = run_queue_triage.format_open_threads_badge(ctx["open_threads"])
        self.assertIn("STALE", cell)
        self.assertIn("AUTHOR_REPLIED", cell)

    def test_non_stale_thread_and_last_replier_roles(self):
        revisions = {
            "sha1": {
                "_number": 1,
                "created": "2026-10-28 14:00:00.000000000",
                "ref": "refs/changes/00/2/1",
                "commit": {"parents": []},
            },
            "sha2": {
                "_number": 2,
                "created": "2026-10-29 10:00:00.000000000",
                "ref": "refs/changes/00/2/2",
                "commit": {"parents": []},
            },
        }
        detail = _detail(2, [], revisions=revisions)
        inline_comments = {
            "src/a.cpp": [
                {
                    "id": "r1",
                    "author": REVIEWER_ALEXIS,
                    "patch_set": 1,
                    "line": 12,
                    "unresolved": True,
                    "updated": "2026-10-28 15:00:00.000000000",
                    "message": "Posted while PS1 was newest; no replies yet.",
                },
            ],
            "src/b.cpp": [
                {
                    "id": "r2",
                    "author": REVIEWER_ALEXIS,
                    "patch_set": 2,
                    "line": 44,
                    "unresolved": True,
                    "updated": "2026-10-29 11:00:00.000000000",
                    "message": "Question on PS2.",
                },
                {
                    "id": "r2_reply",
                    "in_reply_to": "r2",
                    "author": ME,
                    "patch_set": 2,
                    "line": 44,
                    "unresolved": True,
                    "updated": "2026-10-29 11:15:00.000000000",
                    "message": "I agree with Alexis here.",
                },
            ],
        }
        ctx = triage_cl.extract_gerrit_thread_context(detail, inline_comments)
        by_loc = {t["location"]: t for t in ctx["open_threads"]}
        self.assertEqual(set(by_loc.keys()), {"src/a.cpp:12", "src/b.cpp:44"})

        t_a = by_loc["src/a.cpp:12"]
        self.assertFalse(t_a["stale"])
        self.assertFalse(t_a["author_replied"])
        self.assertEqual(t_a["tags"], [])
        self.assertEqual(t_a["last_role"], "reviewer")
        self.assertEqual(t_a["reply_count"], 0)

        t_b = by_loc["src/b.cpp:44"]
        self.assertFalse(t_b["stale"])
        self.assertFalse(t_b["author_replied"])
        self.assertEqual(t_b["last_role"], "you")
        self.assertEqual(t_b["reply_count"], 1)


if __name__ == "__main__":
    unittest.main()

