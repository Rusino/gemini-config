#!/usr/bin/env python3
"""Contract tests for topological review queue ordering and parent readiness gates."""

import os
import sys
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(TESTS_DIR, "..", "scripts")
TRIAGE_SCRIPTS_DIR = os.path.join(
    TESTS_DIR, "..", "skills", "review-triage", "scripts"
)
CANARY_SCRIPTS_DIR = os.path.join(
    TESTS_DIR, "..", "skills", "skia-canary-advisor", "scripts"
)

for d in (SCRIPTS_DIR, TRIAGE_SCRIPTS_DIR, CANARY_SCRIPTS_DIR):
  if d not in sys.path:
    sys.path.insert(0, d)

import canary_advisor  # pylint: disable=g-import-not-at-top
import cl_watcher as w  # pylint: disable=g-import-not-at-top
import run_queue_triage as rqt  # pylint: disable=g-import-not-at-top
import triage_store  # pylint: disable=g-import-not-at-top


class ReviewQueueOrderAndReadinessTest(unittest.TestCase):

  def setUp(self):
    super().setUp()
    w._RELATED_CLS_CACHE.clear()
    w._RELATED_STACK_ORDER_CACHE.clear()

  def test_gerrit_stack_ordering_and_fifo(self):
    host = "skia-review.googlesource.com"
    # Stack: 1412376 (root) -> 1412377 (child) -> 1412378 (grandchild)
    w._RELATED_STACK_ORDER_CACHE[f"{host}:1412376"] = [
        "1412376",
        "1412377",
        "1412378",
    ]
    w._RELATED_STACK_ORDER_CACHE[f"{host}:1412377"] = [
        "1412376",
        "1412377",
        "1412378",
    ]
    w._RELATED_STACK_ORDER_CACHE[f"{host}:1412378"] = [
        "1412376",
        "1412377",
        "1412378",
    ]

    # Present changes in reverse order alongside an older independent CL
    items = [
        {
            "item_key": "gerrit:1412378",
            "kind": "gerrit",
            "target_id": "1412378",
            "preferred_host": host,
        },
        {
            "item_key": "gerrit:1412377",
            "kind": "gerrit",
            "target_id": "1412377",
            "preferred_host": host,
        },
        {
            "item_key": "gerrit:1412376",
            "kind": "gerrit",
            "target_id": "1412376",
            "preferred_host": host,
        },
        {
            "item_key": "gerrit:1400000",
            "kind": "gerrit",
            "target_id": "1400000",
            "preferred_host": host,
        },
    ]
    cache = {
        "gerrit:1412378": {"created": "2026-10-31 12:00:00.000000000", "patchset": 1},
        "gerrit:1412377": {"created": "2026-10-31 11:30:00.000000000", "patchset": 1},
        "gerrit:1412376": {"created": "2026-10-31 11:00:00.000000000", "patchset": 1},
        "gerrit:1400000": {"created": "2026-10-31 09:00:00.000000000", "patchset": 1},
    }

    ordered = w.order_and_link_stack_changes(items, cache)
    tids = [d["target_id"] for d in ordered]

    # 1400000 is depth 0 and older; 1412376 is depth 0; 1412377 is depth 1; 1412378 is depth 2
    self.assertEqual(tids, ["1400000", "1412376", "1412377", "1412378"])

    d76 = next(d for d in ordered if d["target_id"] == "1412376")
    d77 = next(d for d in ordered if d["target_id"] == "1412377")
    d78 = next(d for d in ordered if d["target_id"] == "1412378")

    self.assertEqual(d76["stack_depth"], 0)
    self.assertEqual(d76["stack_ancestors"], [])
    self.assertEqual(d76["stack_parent_ikey"], "")

    self.assertEqual(d77["stack_depth"], 1)
    self.assertEqual(d77["stack_ancestors"], ["gerrit:1412376"])
    self.assertEqual(d77["stack_parent_ikey"], "gerrit:1412376")

    self.assertEqual(d78["stack_depth"], 2)
    self.assertEqual(d78["stack_ancestors"], ["gerrit:1412376", "gerrit:1412377"])
    self.assertEqual(d78["stack_parent_ikey"], "gerrit:1412377")

  def test_parent_readiness_barrier_blocks_descendants_until_triage_or_review(self):
    host = "skia-review.googlesource.com"
    w._RELATED_STACK_ORDER_CACHE[f"{host}:1412376"] = [
        "1412376",
        "1412377",
        "1412378",
    ]
    w._RELATED_STACK_ORDER_CACHE[f"{host}:1412377"] = [
        "1412376",
        "1412377",
        "1412378",
    ]
    w._RELATED_STACK_ORDER_CACHE[f"{host}:1412378"] = [
        "1412376",
        "1412377",
        "1412378",
    ]

    items = [
        {
            "item_key": "gerrit:1412376",
            "kind": "gerrit",
            "target_id": "1412376",
            "preferred_host": host,
            "role": "Reviewer",
        },
        {
            "item_key": "gerrit:1412377",
            "kind": "gerrit",
            "target_id": "1412377",
            "preferred_host": host,
            "role": "Reviewer",
        },
        {
            "item_key": "gerrit:1412378",
            "kind": "gerrit",
            "target_id": "1412378",
            "preferred_host": host,
            "role": "Reviewer",
        },
    ]
    cache = {
        "gerrit:1412376": {"status": "NEW", "patchset": 1, "in_my_attention": True},
        "gerrit:1412377": {"status": "NEW", "patchset": 1, "in_my_attention": True},
        "gerrit:1412378": {"status": "NEW", "patchset": 1, "in_my_attention": True},
    }

    w.order_and_link_stack_changes(items, cache)
    by_ikey = {d["item_key"]: d for d in items}

    # Root CL has no unready ancestors
    self.assertFalse(
        w._has_unready_open_stack_parent(
            "gerrit:1412376", by_ikey, cache, {}, {}
        )
    )

    # Descendants are blocked while root is untriaged
    with mock.patch.object(triage_store, "load_triage_result", return_value=None):
      self.assertTrue(
          w._has_unready_open_stack_parent(
              "gerrit:1412377", by_ikey, cache, {}, {}
          )
      )
      self.assertTrue(
          w._has_unready_open_stack_parent(
              "gerrit:1412378", by_ikey, cache, {}, {}
          )
      )

    # When root has a saved triage verdict for PS1, 1412377 is unblocked
    def fake_load_triage(kind, tid):
      if str(tid) == "1412376":
        return {"patchset": 1, "verdict": "Clean"}
      return None

    with mock.patch.object(
        triage_store, "load_triage_result", side_effect=fake_load_triage
    ):
      self.assertFalse(
          w._has_unready_open_stack_parent(
              "gerrit:1412377", by_ikey, cache, {}, {}
          )
      )
      # 1412378 is still blocked because 1412377 is not yet triaged
      self.assertTrue(
          w._has_unready_open_stack_parent(
              "gerrit:1412378", by_ikey, cache, {}, {}
          )
      )

    # When 1412376's chat is busy running review, 1412377 is blocked again
    new_items_busy = {
        "gerrit:1412376": {
            "item_key": "gerrit:1412376",
            "chat_busy": True,
            "status": "NEW",
            "patchset": 1,
        }
    }
    with mock.patch.object(
        triage_store, "load_triage_result", side_effect=fake_load_triage
    ):
      self.assertTrue(
          w._has_unready_open_stack_parent(
              "gerrit:1412377", by_ikey, cache, new_items_busy, {}
          )
      )

    # When 1412376 is MERGED, 1412377 is unblocked regardless of triage_store
    cache_merged = {
        "gerrit:1412376": {"status": "MERGED", "patchset": 1},
        "gerrit:1412377": {"status": "NEW", "patchset": 1},
        "gerrit:1412378": {"status": "NEW", "patchset": 1},
    }
    with mock.patch.object(triage_store, "load_triage_result", return_value=None):
      self.assertFalse(
          w._has_unready_open_stack_parent(
              "gerrit:1412377", by_ikey, cache_merged, {}, {}
          )
      )

  def test_github_stacked_prs_ordering_and_depth(self):
    items = [
        {
            "item_key": "github:303",
            "kind": "github",
            "target_id": "303",
            "head_ref": "branch-c",
            "base_ref": "branch-b",
            "created": "2026-10-31T12:00:00Z",
        },
        {
            "item_key": "github:301",
            "kind": "github",
            "target_id": "301",
            "head_ref": "branch-a",
            "base_ref": "master",
            "created": "2026-10-31T10:00:00Z",
        },
        {
            "item_key": "github:302",
            "kind": "github",
            "target_id": "302",
            "head_ref": "branch-b",
            "base_ref": "branch-a",
            "created": "2026-10-31T11:00:00Z",
        },
    ]
    cache = {d["item_key"]: d for d in items}

    ordered = w.order_and_link_stack_changes(items, cache)
    tids = [d["target_id"] for d in ordered]

    self.assertEqual(tids, ["301", "302", "303"])
    self.assertEqual(ordered[0]["stack_depth"], 0)
    self.assertEqual(ordered[1]["stack_depth"], 1)
    self.assertEqual(ordered[1]["stack_parent_ikey"], "github:301")
    self.assertEqual(ordered[2]["stack_depth"], 2)
    self.assertEqual(ordered[2]["stack_parent_ikey"], "github:302")
    self.assertEqual(ordered[2]["stack_ancestors"], ["github:301", "github:302"])

  def test_run_queue_triage_topological_sort(self):
    dated_changes = [
        (
            1700000002,
            {
                "_number": 202,
                "_gerrit_host": "skia-review.googlesource.com",
                "current_revision": "sha2",
                "revisions": {
                    "sha2": {
                        "commit": {
                            "commit": "sha2",
                            "parents": [{"commit": "sha1"}],
                        }
                    }
                },
            },
        ),
        (
            1700000001,
            {
                "_number": 201,
                "_gerrit_host": "skia-review.googlesource.com",
                "current_revision": "sha1",
                "revisions": {
                    "sha1": {
                        "commit": {
                            "commit": "sha1",
                            "parents": [{"commit": "sha0"}],
                        }
                    }
                },
            },
        ),
    ]

    rqt.sort_gerrit_changes_topological_fifo(dated_changes)
    result_nums = [c[1]["_number"] for c in dated_changes]
    self.assertEqual(result_nums, [201, 202])

  def test_canary_advisor_waits_for_parent_cr_plus_one(self):
    args = mock.MagicMock()
    args.cl = 1412377
    args.ps = 1
    args.poll_sec = 1
    args.timeout_min = 1

    diff_text = (
        "diff --git a/modules/skparagraph/include/Paragraph.h "
        "b/modules/skparagraph/include/Paragraph.h\n"
        "--- a/modules/skparagraph/include/Paragraph.h\n"
        "+++ b/modules/skparagraph/include/Paragraph.h\n"
        "@@ -1 +1 @@\n"
    )
    meta = {
        "owner": "external@author.com",
        "googler_cr_plus_one": True,
        "is_wip": False,
        "subject": "Child feature",
        "patchset": 1,
        "diff": diff_text,
    }
    missing_cycle = [[("1412376", 1)], []]

    def fake_missing(cl, ps):
      return missing_cycle.pop(0) if missing_cycle else []

    sleep_count = 0

    def fake_sleep(sec):
      nonlocal sleep_count
      sleep_count += 1
      if sleep_count > 4:
        raise KeyboardInterrupt

    with (
        mock.patch.object(canary_advisor, "load_cl", return_value=meta),
        mock.patch.object(
            canary_advisor, "unapproved_g3_chain", side_effect=fake_missing
        ),
        mock.patch.object(canary_advisor, "bb_cl_builds", return_value={}),
        mock.patch.object(
            canary_advisor, "swarming_tasks_for_cl", return_value=[]
        ),
        mock.patch.object(canary_advisor.subprocess, "run") as mock_run,
        mock.patch.object(canary_advisor.time, "sleep", side_effect=fake_sleep),
    ):
      try:
        canary_advisor.cmd_watch(args)
      except KeyboardInterrupt:
        pass

      # Canary-G3 must have been triggered once the parent approval cleared
      mock_run.assert_called_with(
          [
              "git",
              "cl",
              "try",
              "-i",
              "1412377",
              "-B",
              canary_advisor.BUCKET,
              "-b",
              "Canary-G3",
          ],
          check=False,
          cwd=canary_advisor.SKIA_DIR,
      )


if __name__ == "__main__":
  unittest.main()
