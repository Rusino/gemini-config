#!/usr/bin/env python3
"""Offline fixture contract tests for GitHub issue mention/reply filtering in run_queue_triage."""

from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "skills", "review-triage", "scripts")
    ),
)

import run_queue_triage

FIXED_NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenDateTime(datetime):

    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz else FIXED_NOW.replace(tzinfo=None)


def _search_item(number, title, updated_at, assignees=None, user="reporter"):
    url = f"https://github.com/flutter/flutter/issues/{number}"
    return {
        "number": number,
        "title": title,
        "html_url": url,
        "updated_at": updated_at,
        "created_at": "2024-06-25T00:40:09Z",
        "user": {"login": user},
        "assignees": [{"login": a} for a in (assignees or [])],
    }


def _graphql_issue(
    number,
    title,
    updated_at,
    author="reporter",
    body="",
    state="OPEN",
    created_at="2024-06-25T00:40:09Z",
    assignees=None,
    timeline_nodes=None,
    last_edited_at=None,
    editor=None,
):
    return {
        "data": {
            "repository": {
                "issue": {
                    "number": number,
                    "title": title,
                    "url": f"https://github.com/flutter/flutter/issues/{number}",
                    "state": state,
                    "createdAt": created_at,
                    "updatedAt": updated_at,
                    "lastEditedAt": last_edited_at,
                    "editor": {"login": editor} if editor else None,
                    "author": {"login": author} if author else None,
                    "body": body,
                    "assignees": {"nodes": [{"login": a} for a in (assignees or [])]},
                    "timelineItems": {"nodes": timeline_nodes or []},
                }
            }
        }
    }


def _comment_node(login, body, created_at, updated_at=None):
    return {
        "__typename": "IssueComment",
        "author": {"login": login} if login else None,
        "body": body,
        "createdAt": created_at,
        "updatedAt": updated_at or created_at,
    }


def _make_fake_gh(search_items, graphql_by_key, notifications=None):
    notifs = notifications or []

    def _fake_check_output(cmd, *args, **kwargs):
        if cmd[:4] == ["gh", "api", "-X", "GET"] and "search/issues" in cmd:
            return json.dumps({"items": search_items})
        if len(cmd) >= 3 and cmd[:2] == ["gh", "api"] and cmd[2].startswith("notifications?"):
            return json.dumps(notifs)
        if cmd[:3] == ["gh", "api", "graphql"]:
            owner = ""
            name = ""
            num = ""
            for i, tok in enumerate(cmd):
                if tok in ("-f", "-F") and i + 1 < len(cmd):
                    kv = cmd[i + 1]
                    if kv.startswith("owner="):
                        owner = kv.split("=", 1)[1]
                    elif kv.startswith("name="):
                        name = kv.split("=", 1)[1]
                    elif kv.startswith("num="):
                        num = kv.split("=", 1)[1]
            payload = graphql_by_key[(f"{owner}/{name}", str(num))]
            return json.dumps(payload)
        if len(cmd) == 3 and cmd[:2] == ["gh", "api"] and "/issues/" in cmd[2]:
            slug_num = cmd[2].split("/repos/", 1)[1].split("/issues/")
            issue = graphql_by_key[(slug_num[0], slug_num[1])]["data"]["repository"]["issue"]
            return json.dumps({"state": issue["state"].lower()})
        raise AssertionError(f"Unexpected subprocess command: {cmd!r}")

    return _fake_check_output


class QueueTriageIssueMentionsTest(unittest.TestCase):

    def test_nov3_own_comment_last_issues_150744_and_161201_are_hidden(self):
        search_items = [
            _search_item(
                150744,
                "Superscript text doesn't begin with the right character",
                "2026-11-03T01:58:47Z",
                user="matthew-carroll",
            ),
            _search_item(
                161201,
                "Variation selector mapping broken in SkParagraph",
                "2026-11-03T01:47:17Z",
                user="shiro-white-10",
            ),
        ]
        graphql_map = {
            ("flutter/flutter", "150744"): _graphql_issue(
                150744,
                "Superscript text doesn't begin with the right character",
                "2026-11-03T01:58:47Z",
                author="matthew-carroll",
                body="Superscript text fails on macOS.",
                timeline_nodes=[
                    _comment_node(
                        "huycozy",
                        "Additional tests on Android and iOS.",
                        "2024-06-25T09:26:51Z",
                    ),
                    _comment_node(
                        "Rusino",
                        "This is working as intended and is not a bug in Flutter.",
                        "2026-11-03T01:58:47Z",
                    ),
                ],
            ),
            ("flutter/flutter", "161201"): _graphql_issue(
                161201,
                "Variation selector mapping broken in SkParagraph",
                "2026-11-03T01:47:17Z",
                author="shiro-white-10",
                body="Reproducible on latest stable.",
                created_at="2025-01-08T10:00:00Z",
                timeline_nodes=[
                    _comment_node(
                        "shiro-white-10",
                        "We are hoping for a resolution as soon as possible.",
                        "2025-03-27T00:37:39Z",
                    ),
                    _comment_node(
                        "Rusino",
                        "This issue was fixed in Skia by Gerrit 1193898.",
                        "2026-11-03T01:47:17Z",
                    ),
                ],
            ),
        }

        with (
            mock.patch.object(run_queue_triage, "datetime", _FrozenDateTime),
            mock.patch.object(
                subprocess, "check_output", side_effect=_make_fake_gh(search_items, graphql_map)
            ),
        ):
            issues = run_queue_triage.fetch_github_issue_updates(max_days=7, include_all=False)

        self.assertEqual(issues, [])

    def test_reply_after_own_comment_is_shown_with_replier_and_snippet(self):
        search_items = [
            _search_item(
                150744,
                "Superscript text doesn't begin with the right character",
                "2026-11-03T04:15:00Z",
                user="matthew-carroll",
            )
        ]
        graphql_map = {
            ("flutter/flutter", "150744"): _graphql_issue(
                150744,
                "Superscript text doesn't begin with the right character",
                "2026-11-03T04:15:00Z",
                author="matthew-carroll",
                body="Original report.",
                timeline_nodes=[
                    _comment_node(
                        "Rusino",
                        "This is working as intended.",
                        "2026-11-03T01:58:47Z",
                    ),
                    _comment_node(
                        "matthew-carroll",
                        (
                            "> This is working as intended.\n\n"
                            "Thanks! What if the font lacks `sups` | `subs` glyphs on iOS?"
                        ),
                        "2026-11-03T04:15:00Z",
                    ),
                ],
            )
        }

        with (
            mock.patch.object(run_queue_triage, "datetime", _FrozenDateTime),
            mock.patch.object(
                subprocess, "check_output", side_effect=_make_fake_gh(search_items, graphql_map)
            ),
        ):
            issues = run_queue_triage.fetch_github_issue_updates(max_days=7, include_all=False)

        self.assertEqual(len(issues), 1)
        iss = issues[0]
        self.assertEqual(iss["number"], "150744")
        self.assertEqual(iss["role"], "Mentioned")
        self.assertEqual(iss["replier"], "matthew-carroll")
        self.assertIn("What if the font lacks", iss["snippet"])
        self.assertNotIn("working as intended", iss["snippet"])
        self.assertIn("`matthew-carroll`", iss["latest_note"])
        self.assertIn("What if the font lacks", iss["latest_note"])

        rendered = run_queue_triage.format_github_issues_section(issues, max_days=7)
        self.assertIn("[flutter/flutter#150744]", rendered)
        self.assertIn("`matthew-carroll`", rendered)
        self.assertIn("What if the font lacks", rendered)
        self.assertIn(r"\|", rendered)

    def test_mention_with_no_own_comment_is_shown(self):
        search_items = [
            _search_item(
                195500,
                "Line height mismatch with half leading",
                "2026-11-03T08:30:00Z",
                user="Henry-Hiles",
            ),
            _search_item(
                195501,
                "RTL caret jump at ZWJ boundary",
                "2026-11-03T09:00:00Z",
                user="dnfield",
            ),
        ]
        graphql_map = {
            ("flutter/flutter", "195500"): _graphql_issue(
                195500,
                "Line height mismatch with half leading",
                "2026-11-03T08:30:00Z",
                author="Henry-Hiles",
                body="Steps to reproduce...",
                created_at="2026-11-02T20:00:00Z",
                timeline_nodes=[
                    _comment_node(
                        "gaaclarke",
                        "@Rusino can you verify this please?",
                        "2026-11-03T08:30:00Z",
                    )
                ],
            ),
            ("flutter/flutter", "195501"): _graphql_issue(
                195501,
                "RTL caret jump at ZWJ boundary",
                "2026-11-03T09:00:00Z",
                author="dnfield",
                body="cc @Rusino — seeing caret jump to offset 0 on RTL emoji.",
                created_at="2026-11-03T09:00:00Z",
            ),
        }

        with (
            mock.patch.object(run_queue_triage, "datetime", _FrozenDateTime),
            mock.patch.object(
                subprocess, "check_output", side_effect=_make_fake_gh(search_items, graphql_map)
            ),
        ):
            issues = run_queue_triage.fetch_github_issue_updates(max_days=7, include_all=False)

        by_num = {iss["number"]: iss for iss in issues}
        self.assertEqual(set(by_num.keys()), {"195500", "195501"})
        self.assertEqual(by_num["195500"]["replier"], "gaaclarke")
        self.assertEqual(by_num["195500"]["snippet"], "@Rusino can you verify this please?")
        self.assertEqual(by_num["195501"]["replier"], "dnfield")
        self.assertIn("seeing caret jump", by_num["195501"]["snippet"])

    def test_assigned_issue_hidden_when_own_comment_is_latest_unless_new_event(self):
        search_items = [
            _search_item(
                195102,
                "fontFamilyFallback emoji monochrome on Linux",
                "2026-11-01T20:06:24Z",
                assignees=["Rusino"],
                user="Henry-Hiles",
            ),
            _search_item(
                195200,
                "Paragraph crash after reopen",
                "2026-11-03T10:00:00Z",
                assignees=["Rusino"],
                user="Henry-Hiles",
            ),
        ]
        graphql_map = {
            ("flutter/flutter", "195102"): _graphql_issue(
                195102,
                "fontFamilyFallback emoji monochrome on Linux",
                "2026-11-01T20:06:24Z",
                author="Henry-Hiles",
                body="Emoji renders monochrome.",
                created_at="2026-10-22T20:04:48Z",
                assignees=["Rusino"],
                timeline_nodes=[
                    _comment_node(
                        "gaaclarke",
                        "@Rusino can you verify this please?",
                        "2026-11-01T18:11:20Z",
                    ),
                    {
                        "__typename": "AssignedEvent",
                        "actor": {"login": "gaaclarke"},
                        "assignee": {"login": "Rusino"},
                        "createdAt": "2026-11-01T18:11:25Z",
                    },
                    _comment_node(
                        "Rusino",
                        "Fix with a test: Gerrit 1405639.",
                        "2026-11-01T20:06:24Z",
                    ),
                ],
            ),
            ("flutter/flutter", "195200"): _graphql_issue(
                195200,
                "Paragraph crash after reopen",
                "2026-11-03T10:00:00Z",
                author="Henry-Hiles",
                body="Crashes on empty span.",
                created_at="2026-10-30T10:00:00Z",
                assignees=["Rusino"],
                timeline_nodes=[
                    _comment_node(
                        "Rusino",
                        "Fixed on master.",
                        "2026-11-02T10:00:00Z",
                    ),
                    {
                        "__typename": "ReopenedEvent",
                        "actor": {"login": "Henry-Hiles"},
                        "createdAt": "2026-11-03T10:00:00Z",
                    },
                ],
            ),
            ("flutter/flutter", "194296"): _graphql_issue(
                194296,
                "Closed issue with bot lock notification",
                "2026-11-02T21:14:49Z",
                author="aaguueeroo",
                body="Closed.",
                state="CLOSED",
                created_at="2026-10-13T13:15:44Z",
                assignees=["Rusino"],
            ),
        }
        notifications = [
            {
                "reason": "mention",
                "updated_at": "2026-11-02T21:15:14Z",
                "repository": {"full_name": "flutter/flutter"},
                "subject": {
                    "type": "Issue",
                    "title": "Closed issue with bot lock notification",
                    "url": "https://api.github.com/repos/flutter/flutter/issues/194296",
                },
            }
        ]

        with (
            mock.patch.object(run_queue_triage, "datetime", _FrozenDateTime),
            mock.patch.object(
                subprocess,
                "check_output",
                side_effect=_make_fake_gh(search_items, graphql_map, notifications=notifications),
            ),
        ):
            issues = run_queue_triage.fetch_github_issue_updates(max_days=7, include_all=False)

        self.assertEqual([iss["number"] for iss in issues], ["195200"])
        self.assertEqual(issues[0]["role"], "Assigned")
        self.assertEqual(issues[0]["replier"], "Henry-Hiles")
        self.assertEqual(issues[0]["latest_note"], "Reopened by `Henry-Hiles`")

    def test_reopen_days_after_external_comment_and_own_body_edit_after_old_comment(self):
        search_items = [
            _search_item(
                196001,
                "Reopened days after old comment",
                "2026-11-03T11:00:00Z",
                assignees=["Rusino"],
                user="reporter",
            ),
            _search_item(
                196002,
                "Umbrella issue edited by Rusino after old comment",
                "2026-11-03T09:00:00Z",
                assignees=["Rusino"],
                user="mdebbar",
            ),
        ]
        graphql_map = {
            ("flutter/flutter", "196001"): _graphql_issue(
                196001,
                "Reopened days after old comment",
                "2026-11-03T11:00:00Z",
                author="reporter",
                body="Initial report.",
                created_at="2026-10-20T10:00:00Z",
                assignees=["Rusino"],
                timeline_nodes=[
                    _comment_node(
                        "Rusino",
                        "Closing as fixed.",
                        "2026-10-25T10:00:00Z",
                    ),
                    _comment_node(
                        "alice",
                        "Thanks for fixing!",
                        "2026-10-30T10:00:00Z",
                    ),
                    {
                        "__typename": "ReopenedEvent",
                        "actor": {"login": "bob"},
                        "createdAt": "2026-11-03T11:00:00Z",
                    },
                ],
            ),
            ("flutter/flutter", "196002"): _graphql_issue(
                196002,
                "Umbrella issue edited by Rusino after old comment",
                "2026-11-03T09:00:00Z",
                author="mdebbar",
                body="Checklist updated.",
                created_at="2026-07-01T10:00:00Z",
                assignees=["Rusino"],
                last_edited_at="2026-11-03T09:00:00Z",
                editor="Rusino",
                timeline_nodes=[
                    _comment_node(
                        "alice",
                        "Old comment from August.",
                        "2026-08-01T10:00:00Z",
                    ),
                ],
            ),
        }

        with (
            mock.patch.object(run_queue_triage, "datetime", _FrozenDateTime),
            mock.patch.object(
                subprocess, "check_output", side_effect=_make_fake_gh(search_items, graphql_map)
            ),
        ):
            issues = run_queue_triage.fetch_github_issue_updates(max_days=7, include_all=False)

        self.assertEqual([iss["number"] for iss in issues], ["196001"])
        self.assertEqual(issues[0]["replier"], "bob")
        self.assertEqual(issues[0]["latest_note"], "Reopened by `bob`")
        self.assertEqual(issues[0]["updated_et"], "2026-11-03 06:00 ET")


if __name__ == "__main__":
    unittest.main()
