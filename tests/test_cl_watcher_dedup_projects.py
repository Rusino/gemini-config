#!/usr/bin/env python3
"""Contract tests for deduplicating CL/PR rows and excluding Flight Deck chats."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


class ClWatcherDedupProjectsTest(unittest.TestCase):

  def _make_ls_summary(
      self,
      title: str,
      project_id: str,
      last_mod: str,
      archived: bool = False,
  ) -> dict:
    return {
        "annotations": {"title": title, "archived": archived},
        "trajectoryMetadata": {
            "projectId": project_id,
            "lastModifiedTime": last_mod,
            "parentConversationId": "",
            "isBattleModeFork": False,
            "sourceMetadata": None,
        },
    }

  def test_excludes_flight_deck_project_chats(self):
    summaries = {
        "c-fd-1": self._make_ls_summary(
            "[18:42] ⦿ cl/1402416", "proj-fd", "2026-10-26T22:42:39Z"
        ),
        "c-skia-1": self._make_ls_summary(
            "[12:28] ⦿ cl/1402416 SkContext reland dry review",
            "proj-skia",
            "2026-10-22T14:42:06Z",
        ),
    }
    proj_names = {"proj-fd": "Flight Deck", "proj-skia": "Skia"}
    with mock.patch.object(w.cl, "_fetch_ls_summaries_for_audit", return_value=summaries), \
         mock.patch.object(w, "load_project_names", return_value=proj_names):
      discovered = w.discover_active_cl_chats()

    # Meta-discussions in Flight Deck must never be discovered as tracked CL rows
    self.assertEqual(len(discovered), 1)
    self.assertEqual(discovered[0]["conversation_id"], "c-skia-1")
    self.assertEqual(discovered[0]["project_name"], "Skia")
    self.assertEqual(discovered[0]["item_key"], "gerrit:1402416")

  def test_deduplicates_same_cl_across_domain_and_meta_projects(self):
    summaries = {
        "c-meta": self._make_ls_summary(
            "[19:21] ⦿ Skia CL 1382237 SkGlyphID 32-bit audit",
            "proj-meta",
            "2026-10-26T23:21:33Z",
        ),
        "c-domain": self._make_ls_summary(
            "[19:24] ⦿ Skia CL 1382237 Plan",
            "proj-domain",
            "2026-10-21T23:24:05Z",
        ),
    }
    proj_names = {
        "proj-meta": "Improving Experience",
        "proj-domain": "SkGlyphID to 32 bits",
    }
    with mock.patch.object(w.cl, "_fetch_ls_summaries_for_audit", return_value=summaries), \
         mock.patch.object(w, "load_project_names", return_value=proj_names):
      discovered = w.discover_active_cl_chats()

    # Domain project must win over meta project even when the meta chat is newer
    self.assertEqual(len(discovered), 1)
    self.assertEqual(discovered[0]["conversation_id"], "c-domain")
    self.assertEqual(discovered[0]["project_name"], "SkGlyphID to 32 bits")
    self.assertEqual(discovered[0]["item_key"], "gerrit:1382237")

  def test_keeps_meta_project_cl_when_no_domain_project_chat_exists(self):
    summaries = {
        "c-kb-rules": self._make_ls_summary(
            "[17:50] ⦿ cl/1386096 Skia KB rules audit",
            "proj-meta",
            "2026-10-26T21:50:00Z",
        ),
    }
    proj_names = {"proj-meta": "Improving Experience"}
    with mock.patch.object(w.cl, "_fetch_ls_summaries_for_audit", return_value=summaries), \
         mock.patch.object(w, "load_project_names", return_value=proj_names):
      discovered = w.discover_active_cl_chats()

    # CLs existing only in a meta project are kept under that meta project
    self.assertEqual(len(discovered), 1)
    self.assertEqual(discovered[0]["conversation_id"], "c-kb-rules")
    self.assertEqual(discovered[0]["project_name"], "Improving Experience")
    self.assertEqual(discovered[0]["item_key"], "gerrit:1386096")

  def test_prefers_single_target_then_newest_within_same_project(self):
    summaries = {
        "c-multi": self._make_ls_summary(
            "[19:00] ⦿ cl/1402416 and cl/1402417",
            "proj-skia",
            "2026-10-26T23:00:00Z",
        ),
        "c-single-older": self._make_ls_summary(
            "[12:28] ⦿ cl/1402416 SkContext reland",
            "proj-skia",
            "2026-10-26T16:28:00Z",
        ),
        "c-pr-older": self._make_ls_summary(
            "[12:56] ⦿ Run Flutter PR 193774 Tests",
            "proj-web",
            "2026-10-26T16:56:00Z",
        ),
        "c-pr-newer": self._make_ls_summary(
            "[14:12] ⦿ PR #193774",
            "proj-web",
            "2026-10-26T18:12:00Z",
        ),
    }
    proj_names = {"proj-skia": "Skia", "proj-web": "WebParagraph"}
    with mock.patch.object(w.cl, "_fetch_ls_summaries_for_audit", return_value=summaries), \
         mock.patch.object(w, "load_project_names", return_value=proj_names):
      discovered = w.discover_active_cl_chats()

    by_key = {d["item_key"]: d for d in discovered}
    self.assertEqual(len(by_key), 3)  # gerrit:1402416, gerrit:1402417, github:193774
    # Single target beats multi-target for 1402416
    self.assertEqual(by_key["gerrit:1402416"]["conversation_id"], "c-single-older")
    # Multi-target is retained for 1402417 where no single-target exists
    self.assertEqual(by_key["gerrit:1402417"]["conversation_id"], "c-multi")
    # Newest single-target beats older single-target for 193774
    self.assertEqual(by_key["github:193774"]["conversation_id"], "c-pr-newer")


if __name__ == "__main__":
  unittest.main()
