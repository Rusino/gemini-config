#!/usr/bin/env python3
"""Workbench M0 link churn contracts: idempotent syncs and link_added vs link_changed."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKBENCH_DIR = os.path.join(CONFIG_DIR, "workbench")
if WORKBENCH_DIR not in sys.path:
  sys.path.insert(0, WORKBENCH_DIR)

import ingestor  # pylint: disable=g-import-not-at-top
import work_store  # pylint: disable=g-import-not-at-top


def _link_events(store: work_store.WorkStore, akey: str) -> list[tuple[str, dict]]:
  """Returns (event_type, payload) for every link_* event on akey in insertion order."""
  with store._connect() as conn:  # pylint: disable=protected-access
    rows = conn.execute(
        "SELECT event_type, payload_json FROM events "
        "WHERE artifact_key = ? AND event_type LIKE 'link_%' ORDER BY rowid",
        (akey,),
    ).fetchall()
  return [(str(r["event_type"]), json.loads(r["payload_json"] or "{}")) for r in rows]


def _work_updated_at(store: work_store.WorkStore, wid: str) -> float:
  """Returns works.updated_at for wid."""
  with store._connect() as conn:  # pylint: disable=protected-access
    return float(
        conn.execute("SELECT updated_at FROM works WHERE work_id = ?", (wid,)).fetchone()[0]
    )


class TestWorkbenchLinkChurn(unittest.TestCase):

  def setUp(self) -> None:
    self.tmp_dir = tempfile.TemporaryDirectory()
    self.store = work_store.WorkStore(os.path.join(self.tmp_dir.name, "work_graph.db"))
    self.now = 1794370000.0

  def tearDown(self) -> None:
    self.tmp_dir.cleanup()

  def _ingest_draft(self, did: str, thread_id: str, now: float) -> None:
    """Runs _ingest_single_gmail_draft with gmail CLI and thread provenance stubbed out."""
    draft_json = {
        "id": did,
        "message": {
            "threadId": thread_id,
            "internalDate": str(int((self.now - 600) * 1000)),
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "Re: SkParagraph status"},
                    {"name": "To", "value": "Renzo Olivares <roliv@google.com>"},
                ],
                "body": {
                    "data": base64.urlsafe_b64encode(
                        b"See https://skia-review.googlesource.com/c/skia/+/1404137"
                    ).decode("ascii")
                },
            },
        },
    }
    prov = {"last_message_ts": self.now - 3600, "last_from_me": True, "counterparty": "Renzo"}
    with mock.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["gmail"], 0, stdout=json.dumps(draft_json), stderr=""
        ),
    ), mock.patch.object(ingestor, "_fetch_gmail_thread_provenance", return_value=dict(prov)):
      ingestor._ingest_single_gmail_draft(  # pylint: disable=protected-access
          self.store, {"id": did}, did, {}, {}, now
      )

  # A reply draft on a thread that is already the Work's primary gmail_root must not demote the
  # thread to channel; otherwise the root pass flips it back each ~160s sync (310 events/day)
  def test_draft_ingest_keeps_primary_gmail_root_thread(self) -> None:
    wid = self.store.create_work("Renzo digest", now=self.now)
    t_key = "gmail_thread:1a13c69d14fe1c3d"
    self.store.upsert_artifact(t_key, kind="gmail_thread", title="Digest", now=self.now)
    self.store.link_artifact(
        wid, t_key, role="primary", source="auto", link_reason="gmail_root", now=self.now
    )
    self.store.link_artifact(
        wid, "gmail_draft:r1", role="channel", source="auto", now=self.now
    )
    base_events = len(_link_events(self.store, t_key))

    # First pass may legitimately add the draft's body refs; the second pass must be a no-op
    def sync_pass(ts: float) -> None:
      self._ingest_draft("r1", "1a13c69d14fe1c3d", ts)
      self.store.link_artifact(
          wid, t_key, role="primary", source="auto", link_reason="gmail_root", now=ts + 1
      )

    sync_pass(self.now + 100)
    self.assertEqual(self.store.get_work_artifact_roles(wid).get(t_key), "primary")
    self.assertEqual(_link_events(self.store, t_key)[base_events:], [])
    warm_updated = _work_updated_at(self.store, wid)
    with self.store._connect() as conn:  # pylint: disable=protected-access
      warm_total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    sync_pass(self.now + 200)
    self.assertEqual(self.store.get_work_artifact_roles(wid).get(t_key), "primary")
    with self.store._connect() as conn:  # pylint: disable=protected-access
      new_events = conn.execute(
          "SELECT event_type, artifact_key FROM events ORDER BY rowid LIMIT -1 OFFSET ?",
          (warm_total,),
      ).fetchall()
    self.assertEqual([tuple(r) for r in new_events], [])
    self.assertEqual(_work_updated_at(self.store, wid), warm_updated)

  # A companion PR pinned primary via companion_wip must survive collateral gmail_thread_ref
  # mentions; demoting it lets the companion repair silently restore primary every sync
  def test_collateral_mention_does_not_demote_companion_wip(self) -> None:
    wid = self.store.create_work("Soft hyphens", now=self.now)
    pr_key = "github_pr:flutter/flutter:195377"
    self.store.upsert_artifact(
        pr_key,
        kind="github_pr",
        title="195377: [web_paragraph] Companion fixes and tests for soft hyphens (#195366)",
        state={"is_companion": True, "companion_to": "github_pr:flutter/flutter:195366"},
        now=self.now,
    )
    self.store.link_artifact(
        wid, pr_key, role="primary", source="auto", link_reason="companion_wip", now=self.now
    )
    base_events = len(_link_events(self.store, pr_key))
    base_updated = _work_updated_at(self.store, wid)

    for i in range(2):
      self.store.link_artifact(
          wid, pr_key, role="context_only", source="auto", link_reason="gmail_thread_ref",
          now=self.now + 100 * (i + 1),
      )

    self.assertEqual(self.store.get_work_artifact_roles(wid).get(pr_key), "primary")
    self.assertEqual(_link_events(self.store, pr_key)[base_events:], [])
    self.assertEqual(_work_updated_at(self.store, wid), base_updated)

  # link_added must mean a new (work, artifact) edge; role/reason rewrites of an existing edge are
  # link_changed with the previous role, so churn metrics do not count rewrites as new links
  def test_link_added_vs_link_changed_split(self) -> None:
    wid = self.store.create_work("Split", now=self.now)
    akey = "buganizer:580955594"
    self.store.upsert_artifact(akey, kind="buganizer", title="b", now=self.now)
    self.store.link_artifact(
        wid, akey, role="context_only", source="auto", link_reason="chat_mention", now=self.now
    )
    self.store.link_artifact(
        wid, akey, role="primary", source="manual", link_reason="manual", now=self.now + 1
    )
    events = _link_events(self.store, akey)
    self.assertEqual([e[0] for e in events], ["link_added", "link_changed"])
    self.assertEqual(events[1][1].get("prev_role"), "context_only")
    self.assertEqual(events[1][1].get("role"), "primary")
    metrics = self.store.compute_metrics(days=1, now=self.now + 2)
    self.assertEqual(metrics.get("link_added"), 1)
    self.assertEqual(metrics.get("link_changed"), 1)


if __name__ == "__main__":
  unittest.main()
