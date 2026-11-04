#!/usr/bin/env python3
"""Unit and JS-parity tests for R81 Phase 2 (`scripts/entity_state.py`)."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS_DIR = os.path.expanduser("~/.gemini/config/scripts")
if SCRIPTS_DIR not in sys.path:
  sys.path.insert(0, SCRIPTS_DIR)

import cl_watcher as w  # pylint: disable=g-import-not-at-top
import entity_state as es  # pylint: disable=g-import-not-at-top


def _extract_dashboard_script() -> str:
  script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
  return script_body.split("\nboot();", 1)[0]


class EntityStateTest(unittest.TestCase):

  def test_short_actor_collapses_bots_and_prefers_names(self) -> None:
    self.assertEqual(es.short_actor("skia-commit-bot@skcq-be.iam.gserviceaccount.com"), "SkCQ")
    self.assertEqual(es.short_actor("bot"), "SkCQ")
    self.assertEqual(
        es.short_actor(
            "alice@google.com",
            {"last_msg_author": "alice@google.com", "last_msg_author_name": "Alice Smith"},
        ),
        "Alice Smith",
    )
    self.assertEqual(es.short_actor("ab@example.com"), "ab@example.com")
    self.assertEqual(es.short_actor("bob@example.com"), "bob")

  def test_visible_items_from_data_hides_attached_companions_and_cherry_picks(self) -> None:
    data = {
        "items": {
            "gerrit:100": {"item_key": "gerrit:100", "status": "NEW"},
            "gerrit:101": {
                "item_key": "gerrit:101",
                "status": "NEW",
                "companion_to": "gerrit:100",
            },
            "gerrit:102": {
                "item_key": "gerrit:102",
                "status": "NEW",
                "companion_to": "gerrit:999",
            },
            "gerrit:103": {
                "item_key": "gerrit:103",
                "status": "NEW",
                "is_cherry_pick": True,
                "cherry_pick_of": "gerrit:100",
            },
            "gerrit:104": {
                "item_key": "gerrit:104",
                "status": "ABANDONED",
                "companion_to": "gerrit:999",
            },
        }
    }
    vis_keys = [es.item_key_of(it) for it in es.visible_items_from_data(data)]
    self.assertEqual(vis_keys, ["gerrit:100", "gerrit:102"])

  def test_status_and_lane_transitions(self) -> None:
    now_ts = 1_700_000_000.0
    self.assertEqual(
        es.status_of({"status": "NEW", "is_draft": True, "in_my_attention": True}, now_ts=now_ts),
        "draft",
    )
    self.assertEqual(
        es.lane_of({"status": "NEW", "is_draft": True, "in_my_attention": True}, now_ts=now_ts),
        "desk",
    )
    self.assertEqual(
        es.status_of(
            {"status": "NEW", "is_self": True, "merge_conflict": True},
            now_ts=now_ts,
        ),
        "conflict",
    )
    self.assertEqual(
        es.lane_of(
            {"status": "NEW", "is_self": True, "merge_conflict": True},
            now_ts=now_ts,
        ),
        "you",
    )
    self.assertEqual(
        es.status_of(
            {
                "status": "NEW",
                "is_self": True,
                "in_my_attention": True,
                "last_msg_author": "bot@skcq-be.iam.gserviceaccount.com",
            },
            now_ts=now_ts,
        ),
        "waiting",
    )
    self.assertEqual(
        es.status_of(
            {
                "status": "NEW",
                "is_self": True,
                "in_my_attention": True,
                "attention_reason": "SkCQ dry run succeeded.",
                "last_msg_author": "jlavrova@google.com",
                "missing_approvals": 1,
                "open_external_threads": 0,
            },
            now_ts=now_ts,
        ),
        "waiting",
    )
    self.assertEqual(
        es.status_of(
            {"status": "MERGED", "submitted_ts": now_ts - 3600},
            now_ts=now_ts,
        ),
        "merged",
    )
    self.assertEqual(
        es.status_of(
            {"status": "MERGED", "submitted_ts": now_ts - 5 * 86400},
            now_ts=now_ts,
        ),
        "old",
    )
    self.assertEqual(
        es.lane_of(
            {
                "status": "MERGED",
                "submitted_ts": now_ts - 5 * 86400,
                "has_active_merges": True,
            },
            now_ts=now_ts,
        ),
        "landing",
    )
    self.assertTrue(
        es.can_rebase(
            {
                "item_key": "github:195122",
                "status": "OPEN",
                "is_self": True,
                "merge_conflict": False,
                "cq_sync_rebase_needed": True,
            }
        )
    )
    self.assertIn(
        "diagnose_checks",
        es.available_actions_of(
            {
                "item_key": "github:195999",
                "status": "OPEN",
                "is_self": True,
                "phase": "ALERT_CQ_FAILED",
                "cq_failed": True,
                "merge_conflict": False,
                "cq_sync_rebase_needed": False,
            }
        ),
    )

  def test_triage_gates_and_finalize_clean_review(self) -> None:
    clean_item = {
        "item_key": "gerrit:1400001",
        "kind": "gerrit",
        "status": "NEW",
        "patchset": 2,
        "triage": {
            "triaged_patchset": 2,
            "review_outcome": "CLEAN",
            "corpus_required": True,
            "corpus_status": "passed",
            "corpus_tested_patchset": 2,
            "g3_corpus_required": True,
            "g3_corpus_status": "pending",
        },
    }
    self.assertFalse(es.can_finalize_clean_review(clean_item))
    self.assertEqual(es.combined_gate_state(clean_item)["status"], "pending")
    self.assertEqual(es.combined_gate_state(clean_item)["activeLabel"], "google3 A/B")

    clean_item["triage"]["g3_corpus_status"] = "passed"
    clean_item["triage"]["g3_corpus_tested_patchset"] = 2
    self.assertTrue(es.can_finalize_clean_review(clean_item))
    self.assertEqual(es.combined_gate_state(clean_item)["status"], "passed")
    self.assertEqual(
        es.combined_gate_state(clean_item)["passedLabel"],
        "Flutter + google3 A/B",
    )

  def test_save_watcher_state_enriches_items_and_bugs(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
      state_path = os.path.join(tmpdir, "state.json")
      with mock.patch.object(w, "STATE_FILE", state_path):
        state = {
            "items": {
                "gerrit:1410001": {
                    "item_key": "gerrit:1410001",
                    "kind": "gerrit",
                    "target_id": "1410001",
                    "status": "NEW",
                    "in_my_attention": True,
                    "role": "reviewer",
                }
            },
            "bugs": {
                "buganizer:500": {
                    "id": "500",
                    "lane": "you",
                    "needs_you": True,
                    "chat_busy": False,
                }
            },
        }
        w.save_watcher_state(state)
        loaded = w.load_watcher_state()
        it_es = loaded["items"]["gerrit:1410001"].get("entity_state")
        self.assertIsInstance(it_es, dict)
        self.assertEqual(it_es["status"], "attention")
        self.assertEqual(it_es["lane"], "you")
        self.assertIn("start_code_review", it_es["available_actions"])

        bug_es = loaded["bugs"]["buganizer:500"].get("entity_state")
        self.assertIsInstance(bug_es, dict)
        self.assertEqual(bug_es["lane"], "you")
        self.assertTrue(bug_es["actionable_now"])

    # B3: Approved merge-request bug (is_merge_request_bug=True, is_security_merge_request=False)
    # still exposes security_merge_manual_cherry_pick when lane == "you"
    approved_merge_bug = {
        "id": "501",
        "lane": "you",
        "needs_you": True,
        "is_merge_request_bug": True,
        "is_security_merge_request": False,
        "merge_action_phase": "manual_cherry_pick",
    }
    self.assertIn(
        "security_merge_manual_cherry_pick",
        es.compute_bug_entity_state(approved_merge_bug)["available_actions"],
    )

  def test_js_parity_and_freshness_signature(self) -> None:
    now_ms = 1_762_272_000_000
    now_ts = now_ms / 1000.0
    state = {
        "items": {
            "gerrit:2001": {
                "item_key": "gerrit:2001",
                "kind": "gerrit",
                "target_id": "2001",
                "status": "NEW",
                "patchset": 3,
                "in_my_attention": True,
                "role": "reviewer",
                "missing_reviewers_to_assign": 1,
                "triage": {
                    "triaged_patchset": 3,
                    "review_outcome": "CLEAN",
                    "corpus_required": True,
                    "corpus_status": "pending",
                    "corpus_script": "run_flutter_paragraph_tests.py",
                },
            },
            "gerrit:2002": {
                "item_key": "gerrit:2002",
                "kind": "gerrit",
                "target_id": "2002",
                "status": "NEW",
                "patchset": 1,
                "stack_parent_ikey": "gerrit:2001",
                "stack_ancestors": ["gerrit:2001"],
                "sponsor_action": "submit_cq",
                "missing_approvals": 1,
            },
            "github:3001": {
                "item_key": "github:3001",
                "kind": "github",
                "target_id": "3001",
                "status": "OPEN",
                "patchset": 4,
                "head_sha": "abcdef1234567",
                "chat_busy": True,
                "triage": {
                    "triaged_patchset": 1,
                    "triaged_revision": "abcdef1",
                    "review_outcome": "NITS",
                    "corpus_required": True,
                    "corpus_status": "passed",
                    "corpus_tested_sha": "abcdef1234567",
                    "corpus_script": "run_webparagraph_corpus.py",
                },
            },
            "gerrit:2003": {
                "item_key": "gerrit:2003",
                "kind": "gerrit",
                "target_id": "2003",
                "status": "NEW",
                "is_self": True,
                "merge_conflict": True,
                "companions": [
                    {"id": "2099", "status": "NEW", "ready_to_abandon": True}
                ],
            },
        },
        "bugs": {},
    }
    es.enrich_entity_states(state, now_ts=now_ts)

    # Also test a copy with entity_state stripped to verify 1:1 parity against JS fallback
    stripped_state = copy.deepcopy(state)
    for it in stripped_state["items"].values():
      it.pop("entity_state", None)

    script = _extract_dashboard_script()
    harness = f"""
const DateNowOrig = Date.now;
Date.now = () => {now_ms};
{script}
const enriched = {json.dumps(state)};
const stripped = {json.dumps(stripped_state)};

function collect(data) {{
  cachedData = data;
  const vis = visibleItemsFromData(data);
  buildStackContext(vis);
  const out = {{}};
  for (const [k, it] of Object.entries(data.items)) {{
    out[k] = {{
      fresh: hasFreshEntityState(it, data),
      status: statusOf(it),
      lane: laneOf(it),
      needsMyReply: needsMyReply(it),
      isAlert: isAlert(it),
      isPostSubmitAlert: isPostSubmitAlert(it),
      needsSponsorAction: needsSponsorAction(it),
      isReviewerChange: isReviewerChange(it),
      canRebase: canRebase(it),
      canFinalizeCleanReview: canFinalizeCleanReview(it),
      isTriageStale: isTriageStale(it),
      effectiveCorpusStatus: effectiveCorpusStatus(it),
      effectiveG3CorpusStatus: effectiveG3CorpusStatus(it),
      combinedGateState: combinedGateState(it),
      triageBadgeInfo: triageBadgeInfo(it),
      spine: spineStepShortState(it),
      blockers: upstreamBlockersOf(it, data).map(b => ({{
        ancestor_key: itemKeyOf(b.ancestor),
        reasons: b.reasons,
      }})),
    }};
  }}
  return out;
}}

const resEnriched = collect(enriched);
const resStripped = collect(stripped);

// Mutate parent CL in JS without updating entity_state and verify child invalidates cache
cachedData = enriched;
buildStackContext(visibleItemsFromData(enriched));
enriched.items['gerrit:2001'].merge_conflict = true;
const childFreshAfterParentMutation = hasFreshEntityState(enriched.items['gerrit:2002'], enriched);
const childBlockersAfterMutation = upstreamBlockersOf(enriched.items['gerrit:2002'], enriched)
  .map(b => b.reasons);

// B1.1 & B1.2: Mutate is_companion, conversation_id, or stack_root_ikey and verify invalidation + parity
const compItem = {{item_key: 'gerrit:2099', kind: 'gerrit', status: 'NEW', is_companion: true}};
const compParity = isCompanion(compItem) === true && isReviewerChange(compItem) === false;
enriched.items['gerrit:2003'].conversation_id = 'c-new-conv';
const freshAfterConvMutation = hasFreshEntityState(enriched.items['gerrit:2003'], enriched);

// B2.2: Verify JS fallback needsSponsorAction checks cachedData.items when cachedStackByIkey is empty
cachedStackByIkey = new Map();
cachedData = stripped;
stripped.items['gerrit:2001'].missing_approvals = 1;
const sponsorWithUnapprovedAncInCachedData = needsSponsorAction(stripped.items['gerrit:2002']);

console.log(JSON.stringify({{
  resEnriched,
  resStripped,
  childFreshAfterParentMutation,
  childBlockersAfterMutation,
  compParity,
  freshAfterConvMutation,
  sponsorWithUnapprovedAncInCachedData,
}}));
"""
    proc = subprocess.run(
        ["node", "-"],
        input=harness,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    for k, enriched_vals in payload["resEnriched"].items():
      self.assertTrue(enriched_vals["fresh"], f"Expected fresh entity_state for {k}")
      stripped_vals = dict(payload["resStripped"][k])
      stripped_vals["fresh"] = True
      self.assertEqual(enriched_vals, stripped_vals, f"Parity mismatch on {k}")

    self.assertFalse(payload["childFreshAfterParentMutation"])
    self.assertIn("conflict", payload["childBlockersAfterMutation"][0])
    self.assertTrue(payload["compParity"])
    self.assertFalse(payload["freshAfterConvMutation"])
    self.assertFalse(payload["sponsorWithUnapprovedAncInCachedData"])


if __name__ == "__main__":
  unittest.main()
