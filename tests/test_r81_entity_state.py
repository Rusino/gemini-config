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
            "gerrit:2004": {
                "item_key": "gerrit:2004",
                "kind": "gerrit",
                "target_id": "2004",
                "status": "NEW",
                "patchset": 2,
                "revision": "rev2004",
                "is_self": False,
                "is_wip": False,
                "self_approved": False,
                "self_cr_vote": 0,
                "in_my_attention": False,
                "phase": "WAITING_ON_OTHERS",
                "cq_status": "CQ+1 (Dry Run)",
                "armed_finalize": {
                    "patchset": 2,
                    "expected_revision": "rev2004",
                    "draft_ids": ["d1"],
                    "armed_at": 1700000000,
                },
                "triage": {
                    "triaged_patchset": 2,
                    "review_outcome": "CLEAN",
                    "verdict": "CLEAN",
                    "corpus_required": False,
                    "corpus_status": "not_required",
                },
            },
            "gerrit:1422517": {
                "item_key": "gerrit:1422517",
                "kind": "gerrit",
                "target_id": "1422517",
                "status": "NEW",
                "patchset": 2,
                "is_self": True,
                "is_wip": False,
                "phase": "WAITING_ON_OTHERS",
                "in_my_attention": False,
                "approved_count": 1,
                "missing_approvals": 0,
                "cq_status": "✓ Checks green",
            },
            "gerrit:1422518": {
                "item_key": "gerrit:1422518",
                "kind": "gerrit",
                "target_id": "1422518",
                "status": "NEW",
                "patchset": 1,
                "is_self": True,
                "is_wip": False,
                "phase": "WAITING_ON_OTHERS",
                "in_my_attention": False,
                "approved_count": 1,
                "missing_approvals": 0,
                "cq_status": "✓ Checks green",
                "stack_parent_ikey": "gerrit:2001",
                "stack_ancestors": ["gerrit:2001"],
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
      needsSelfSubmit: needsSelfSubmit(it),
      isReviewerChange: isReviewerChange(it),
      canRebase: canRebase(it),
      canFinalizeCleanReview: canFinalizeCleanReview(it),
      isArmedFinalize: isArmedFinalize(it),
      finalizeActionInfo: finalizeActionInfo(it),
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

  # Verify approved green self CLs route to Needs You while unapproved zero-missing, CQ+2, WIP, or
  # upstream-blocked CLs stay out, and <GERRIT_ACCOUNT_N> placeholders resolve to human names
  def test_r18_needs_self_submit_and_gerrit_account_placeholder_cleanup(self) -> None:
    now_ts = 1_794_331_000.0
    ready_cl = {
        "item_key": "gerrit:1422517",
        "kind": "gerrit",
        "target_id": "1422517",
        "status": "NEW",
        "phase": "WAITING_ON_OTHERS",
        "is_self": True,
        "is_wip": False,
        "is_draft": False,
        "in_development": False,
        "in_my_attention": False,
        "approved_count": 1,
        "missing_approvals": 0,
        "cq_status": "✓ Checks green",
    }
    self.assertTrue(es.needs_self_submit(ready_cl))
    self.assertEqual(es.status_of(ready_cl, now_ts=now_ts), "attention")
    self.assertEqual(es.lane_of(ready_cl, now_ts=now_ts), "you")
    self.assertEqual(
        es.spine_step_short_state(ready_cl, now_ts=now_ts),
        {"short": "Ready to submit", "icon": "🚀", "tone": "spine-you"},
    )
    self.assertTrue(es.compute_item_entity_state(ready_cl, now_ts=now_ts)["needs_self_submit"])

    for bad_override in [
        {"approved_count": 0, "missing_approvals": 0},
        {"approved_count": 1, "missing_approvals": 1},
        {"cq_status": "CQ+2 (Submitting)"},
        {"cq_status": "CQ+1 · 2 checks running"},
        {"cq_status": "🔄 1 running"},
        {"cq_status": "🏷️ autosubmit"},
        {"cq_status": "🔴 Failed"},
        {"cq_status": "🌲 Tree closed"},
        {"phase": "SUBMITTING"},
        {"phase": "ALERT_CQ_FAILED"},
        {"is_wip": True},
        {"is_draft": True},
        {"in_development": True},
        {"is_companion": True},
        {"merge_conflict": True},
        {"cq_sync_rebase_needed": True},
        {"cq_failed": True},
        {"tree_red": True},
    ]:
      cand = {**ready_cl, **bad_override}
      self.assertFalse(es.needs_self_submit(cand), f"Expected False for {bad_override}")

    stacked_child = {
        **ready_cl,
        "item_key": "gerrit:1422518",
        "stack_parent_ikey": "gerrit:1422510",
        "stack_ancestors": ["gerrit:1422510"],
    }
    unapproved_anc = {
        "gerrit:1422510": {
            "item_key": "gerrit:1422510",
            "status": "NEW",
            "approved_count": 0,
            "missing_approvals": 0,
        }
    }
    self.assertFalse(es.needs_self_submit(stacked_child, by_ikey=unapproved_anc))
    merged_anc = {
        "gerrit:1422510": {
            "item_key": "gerrit:1422510",
            "status": "MERGED",
            "approved_count": 0,
            "missing_approvals": 1,
        }
    }
    self.assertTrue(es.needs_self_submit(stacked_child, by_ikey=merged_anc))

    att_entry = {
        "reason": "Added by <GERRIT_ACCOUNT_5283275> using the hovercard menu",
        "reason_account": {
            "_account_id": 5283275,
            "name": "Miyoung Shin",
            "email": "myid.shin@igalia.com",
        },
    }
    self.assertEqual(
        w._resolve_gerrit_account_placeholders(att_entry["reason"], entry=att_entry),
        "Added by Miyoung Shin using the hovercard menu",
    )
    self.assertEqual(
        w._resolve_gerrit_account_placeholders("Ping <GERRIT_ACCOUNT_9999999>"),
        "Ping account #9999999",
    )

    # Gerrit CR-1 veto, unsatisfied Code-Owners, and GitHub CHANGES_REQUESTED block submit
    veto_payload = {
        "status": "NEW",
        "subject": "[skparagraph] Cap letter spacing",
        "owner": {"email": "jlavrova@google.com", "name": "Julia Lavrova"},
        "current_revision": "sha333",
        "revisions": {"sha333": {"_number": 1}},
        "reviewers": {
            "REVIEWER": [
                {"email": "fmalita@google.com"},
                {"email": "bungeman@google.com"},
            ]
        },
        "labels": {
            "Code-Review": {
                "all": [
                    {"email": "fmalita@google.com", "value": 1},
                    {"email": "bungeman@google.com", "value": -1},
                ]
            }
        },
        "submit_requirements": [{"name": "Code-Review", "status": "UNSATISFIED"}],
        "messages": [],
    }
    owners_payload = {
        **veto_payload,
        "labels": {
            "Code-Review": {"all": [{"email": "fmalita@google.com", "value": 1}]}
        },
        "submit_requirements": [{"name": "Code-Owners", "status": "UNSATISFIED"}],
    }
    autosubmit_gerrit_payload = {
        **veto_payload,
        "labels": {
            "Code-Review": {"all": [{"email": "fmalita@google.com", "value": 1}]},
            "Auto-Submit": {"all": [{"email": "jlavrova@google.com", "value": 1}]},
        },
        "submit_requirements": [{"name": "Code-Review", "status": "SATISFIED"}],
    }
    from unittest import mock

    def _json_resp(obj: dict) -> mock.MagicMock:
      resp = mock.MagicMock()
      resp.read.return_value = (")]}'\n" + json.dumps(obj)).encode("utf-8")
      resp.__enter__.return_value = resp
      return resp

    for bad_gerrit_payload in (veto_payload, owners_payload, autosubmit_gerrit_payload):
      with (
          mock.patch.object(
              w.urllib.request, "urlopen", return_value=_json_resp(bad_gerrit_payload)
          ),
          mock.patch.object(w, "gerrit_mergeable", return_value=True),
      ):
        g_rec = w.fetch_gerrit_cl_status("1422599", [], [], skia_dir="/nonexistent")
      self.assertFalse(es.needs_self_submit(g_rec), f"Expected False for {g_rec}")

    gh_changes_req_payload = {
        "number": 196199,
        "title": "[web] Test PR",
        "state": "OPEN",
        "isDraft": False,
        "headRefName": "fix-branch",
        "headRefOid": "abcdef123456",
        "reviewDecision": "CHANGES_REQUESTED",
        "author": {"login": "jlavrova"},
        "latestReviews": [
            {"author": {"login": "mdebbar"}, "state": "APPROVED", "authorAssociation": "MEMBER"},
            {
                "author": {"login": "yjbanov"},
                "state": "CHANGES_REQUESTED",
                "authorAssociation": "MEMBER",
            },
        ],
        "reviews": [],
        "reviewRequests": [],
        "comments": [],
        "commits": [],
        "labels": [],
        "statusCheckRollup": [{"name": "Linux", "status": "COMPLETED", "conclusion": "SUCCESS"}],
    }
    gh_autosubmit_payload = {
        **gh_changes_req_payload,
        "reviewDecision": "APPROVED",
        "latestReviews": [
            {"author": {"login": "mdebbar"}, "state": "APPROVED", "authorAssociation": "MEMBER"}
        ],
        "labels": [{"name": "autosubmit"}],
    }
    for bad_gh_payload in (gh_changes_req_payload, gh_autosubmit_payload):
      with mock.patch.object(
          w.subprocess, "check_output", return_value=json.dumps(bad_gh_payload)
      ):
        gh_rec = w.fetch_github_pr_status("196199")
      self.assertFalse(es.needs_self_submit(gh_rec), f"Expected False for {gh_rec}")

    # Ancestor open_external_threads / tree_red, bug link stack_idx, and JS composite keys
    anc_threads = {
        "gerrit:1422510": {
            "item_key": "gerrit:1422510",
            "status": "NEW",
            "approved_count": 1,
            "missing_approvals": 0,
            "open_external_threads": 2,
        }
    }
    self.assertFalse(es.needs_self_submit(stacked_child, by_ikey=anc_threads))
    bugs_map = {
        "b:500000001": {
            "bug_key": "b:500000001",
            "status": "ASSIGNED",
            "gerrit_cl_ids": ["1422518"],
        }
    }
    items_composite = {
        "gerrit:1422510@unchatted": {
            "item_key": "gerrit:1422510",
            "kind": "gerrit",
            "target_id": "1422510",
            "status": "NEW",
            "approved_count": 0,
            "missing_approvals": 1,
        },
        "gerrit:1422518@unchatted": {
            **stacked_child,
            "commit_message": "Bug: b/500000001",
        },
    }
    w.link_bugs_and_cls(bugs_map, items_composite)
    subtasks = bugs_map["b:500000001"].get("linked_cls") or []
    self.assertEqual(len(subtasks), 1)
    self.assertFalse(subtasks[0]["in_needs_you"])

    # JS composite ancestor lookup blocks child submit while focusCard keeps reviewer reply snippet
    js_src = _extract_dashboard_script()
    node_probe = f"""
{js_src}
cachedData = {{
  items: {{
    "gerrit:1422510@unchatted": {{
      item_key: "gerrit:1422510", kind: "gerrit", target_id: "1422510",
      status: "NEW", is_cherry_pick: true, cherry_pick_of: "gerrit:1400000",
      approved_count: 0, missing_approvals: 1
    }},
    "gerrit:1400000@unchatted": {{
      item_key: "gerrit:1400000", kind: "gerrit", target_id: "1400000", status: "MERGED"
    }},
    "gerrit:1422518@unchatted": {{
      item_key: "gerrit:1422518", kind: "gerrit", target_id: "1422518",
      status: "NEW", is_self: true, approved_count: 1, missing_approvals: 0,
      cq_status: "✓ Dry Run Passed", stack_ancestors: ["gerrit:1422510"]
    }}
  }}
}};
buildStackContext(visibleItemsFromData(cachedData));
const childBlocked = needsSelfSubmit(cachedData.items["gerrit:1422518@unchatted"]);
const cardHtml = focusCard({{
  item_key: "gerrit:1422517", kind: "gerrit", target_id: "1422517",
  project_name: "Skia", subject: "Cap letter spacing", url: "https://example.com/1422517",
  status: "NEW", patchset: 2, is_self: true, phase: "NEEDS_YOUR_ACTION",
  in_my_attention: true, approved_count: 1, missing_approvals: 0, open_external_threads: 0,
  cq_status: "Idle", last_msg_author: "fmalita@google.com",
  last_msg_snippet: "LGTM modulo comment wording", status_changed_et: "12:00"
}}, cachedData);
console.log(JSON.stringify({{childBlocked, cardHtml}}));
"""
    probe_out = json.loads(
        subprocess.check_output(["node", "-"], input=node_probe, text=True, timeout=10)
    )
    self.assertFalse(probe_out["childBlocked"])
    self.assertIn("Ready to submit", probe_out["cardHtml"])
    self.assertIn("LGTM modulo comment wording", probe_out["cardHtml"])
    self.assertNotIn("checks green", probe_out["cardHtml"])



if __name__ == "__main__":
  unittest.main()
