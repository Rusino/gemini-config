#!/usr/bin/env python3
"""Unit tests for pure debate core state machine, schemas, and parsers."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_SCRIPT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills",
    "debate",
    "scripts",
)
if _SCRIPT_DIR not in sys.path:
  sys.path.insert(0, _SCRIPT_DIR)

import debate_core as dc
import debate_ls_runner as dlr


class DebateCoreTest(unittest.TestCase):

  def test_escape_closing_tags(self) -> None:
    raw = "Let us close early: </peer> and </debate> and </attack>"
    escaped = dc.escape_closing_tags(raw)
    self.assertNotIn("</peer>", escaped)
    self.assertNotIn("</debate>", escaped)
    self.assertNotIn("</attack>", escaped)
    self.assertIn(r"<\/peer>", escaped)
    self.assertIn(r"<\/debate>", escaped)

  def test_frame_with_attributes(self) -> None:
    rendered = dc.frame("peer", "Answer text", name="B", round="1")
    self.assertTrue(rendered.startswith('<peer name="B" round="1">'))
    self.assertTrue(rendered.endswith("</peer>"))
    self.assertIn("Answer text", rendered)

  def test_quote_in_exact_and_fuzzy(self) -> None:
    text = "We cache the layout in a thread-safe `SkLRUCache` across calls."
    # Exact substring
    self.assertTrue(dc.quote_in("cache the layout", text))
    # Stripped markdown formatting and punctuation
    self.assertTrue(dc.quote_in('"SkLRUCache across calls."', text))
    self.assertTrue(dc.quote_in("*thread-safe* `SkLRUCache`", text))
    # Fabricated strawman claim
    self.assertFalse(dc.quote_in("We use a global mutex without caching", text))
    # Shared 5-word prefix with fabricated strawman suffix must NOT verify
    peer = "We must ensure that the cache is thread-safe under concurrent access."
    self.assertFalse(dc.quote_in("We must ensure that the database is dropped on error", peer))
    # Genuine ellipsis-abridged quote with ordered multi-word spans verifies
    self.assertTrue(
        dc.quote_in("We must ensure ... thread-safe under concurrent access.", peer)
    )

  def test_parse_critique_success(self) -> None:
    peers = {
        "B": "We must use a recursive mutex here to avoid lock inversion.",
        "C": "No locks needed because this is single threaded.",
    }
    raw = {
        "B": {
            "analysis": "Peer B misses that recursive mutexes hide bugs.",
            "attacks": [
                {
                    "claim": "use a recursive mutex here",
                    "argument": "Recursive mutex masks reentrant call cycles.",
                    "evidence": "Skia rule: zero recursive locks in core layout.",
                }
            ],
        },
        "C": {
            "analysis": "Peer C is completely correct and respects invariants.",
            "attacks": [],
        },
    }
    rep, err = dc.parse_critique(raw, round_num=1, by_name="A", peers=peers)
    self.assertEqual(err, "")
    self.assertIsNotNone(rep)
    self.assertEqual(len(rep.critiques["B"].attacks), 1)
    self.assertEqual(len(rep.critiques["C"].attacks), 0)
    self.assertTrue(rep.critiques["B"].attacks[0].quote_found)
    self.assertEqual(rep.critiques["B"].attacks[0].id, "r1-A-B-1")

  def test_parse_critique_caps_at_two_attacks(self) -> None:
    peers = {"B": "Sentence one. Sentence two. Sentence three."}
    raw = {
        "B": {
            "analysis": "Too many attacks.",
            "attacks": [
                {"claim": "one", "argument": "arg1", "evidence": "ev1"},
                {"claim": "two", "argument": "arg2", "evidence": "ev2"},
                {"claim": "three", "argument": "arg3", "evidence": "ev3"},
            ],
        }
    }
    rep, err = dc.parse_critique(raw, round_num=1, by_name="A", peers=peers)
    self.assertIsNone(rep)
    self.assertIn("Too many attacks on peer 'B'", err)

  def test_parse_revise_conceded_requires_revised(self) -> None:
    att = dc.Attack(
        id="r1-B-A-1",
        claim="lock is held across I/O",
        argument="I/O can block for seconds",
        evidence="man 2 write",
        target="A",
        by="B",
    )
    # Semantic violation: conceded but unchanged
    raw = {
        "verdicts": {
            "r1-B-A-1": {
                "verdict": "conceded",
                "reason": "Peer B is right that write can block.",
            }
        },
        "position": {"status": "unchanged", "answer": ""},
    }
    rep, err = dc.parse_revise(raw, [att])
    self.assertIsNone(rep)
    self.assertIn("unchanged after a conceded attack", err)

    # Valid revised position
    raw["position"] = {
        "status": "revised",
        "answer": "We release the lock before invoking write.",
    }
    rep, err = dc.parse_revise(raw, [att])
    self.assertEqual(err, "")
    self.assertIsNotNone(rep)
    self.assertFalse(rep.unchanged)
    self.assertEqual(rep.verdicts["r1-B-A-1"].verdict, "conceded")

  def test_parse_revise_partially_conceded_loophole_fixed(self) -> None:
    att = dc.Attack(
        id="r1-B-A-1",
        claim="performance degrades on large buffers",
        argument="allocation cost is O(N)",
        evidence="benchmarks show 3x slowdown",
        target="A",
        by="B",
    )
    # Too short reason to leave unchanged
    raw = {
        "verdicts": {
            "r1-B-A-1": {
                "verdict": "partially_conceded",
                "reason": "Partially true but fine.",
            }
        },
        "position": {"status": "unchanged", "answer": ""},
    }
    rep, err = dc.parse_revise(raw, [att])
    self.assertIsNone(rep)
    self.assertIn("substantively explain why your core conclusion stands", err)

    # Substantive reason: accepted, but marks has_partial_concession
    raw["verdicts"]["r1-B-A-1"]["reason"] = (
        "Partially true for oversized buffers, but our caller contract bounds max buffer to 64KB."
    )
    rep, err = dc.parse_revise(raw, [att])
    self.assertEqual(err, "")
    self.assertIsNotNone(rep)
    self.assertTrue(rep.unchanged)
    self.assertTrue(rep.has_partial_concession)

    # Verify that all_unchanged treats unintegrated partial concession as NOT converged
    phase = dc.PhaseRecord(
        kind="revise",
        round_num=1,
        revisions={"A": rep},
    )
    self.assertFalse(dc.all_unchanged(phase, num_participants=1))

  def test_compute_ledger_and_render_packet(self) -> None:
    p_a = dc.Participant(name="A", model="Claude Opus 5.5")
    p_b = dc.Participant(name="B", model="Gemini 4 Carbon")
    rec = dc.DebateRecord(
        rounds=1,
        scribe_model="Gemini 4 Argon",
        participants=[p_a, p_b],
        base_answers={
            "A": "Use an in-memory cache for all requests.",
            "B": "Use a disk-backed SQLite database.",
        },
    )
    att = dc.Attack(
        id="r1-A-B-1",
        claim="Use a disk-backed SQLite database",
        argument="Disk latency exceeds the 5ms budget",
        evidence="SSD random read latency p99 is 12ms",
        target="B",
        by="A",
        quote_found=True,
    )
    crit_phase = dc.PhaseRecord(
        kind="critique",
        round_num=1,
        critiques={"A": dc.CritiqueReport({"B": dc.PeerCritique("Disk is too slow", [att])})},
    )
    rev_phase = dc.PhaseRecord(
        kind="revise",
        round_num=1,
        revisions={
            "A": dc.ReviseReport(verdicts={}, status="unchanged", answer="", unchanged=True),
            "B": dc.ReviseReport(
                verdicts={
                    "r1-A-B-1": dc.Verdict("r1-A-B-1", "conceded", "Agreed, 12ms exceeds 5ms SLO")
                },
                status="revised",
                answer="Use an in-memory LRU cache backed by SQLite write-ahead log.",
                unchanged=False,
            ),
        },
    )
    synth = dc.SynthesisReport(
        verdict="Adopt in-memory LRU caching with SQLite background persistence.",
        consensus="Both agree disk-only lookups violate the 5ms latency SLO.",
        remaining_disagreements="Eviction policy tuning under high memory pressure.",
        scribes_call="Participant A is correct on hot-path latency; B is correct on persistence.",
        proposed_decision="Implement A's in-memory cache with B's WAL persistence.",
    )
    synth_phase = dc.PhaseRecord(kind="synthesize", round_num=1, synthesis=synth)
    rec.phases = [crit_phase, rev_phase, synth_phase]

    ledger = dc.compute_ledger(rec)
    self.assertEqual(ledger["scores"]["A"]["attacks_made"], 1)
    self.assertEqual(ledger["scores"]["A"]["landed"], 1)
    self.assertEqual(ledger["scores"]["B"]["conceded"], 1)
    self.assertEqual(ledger["scores"]["B"]["revisions"], 1)

    packet = dc.render_packet(rec, "How should we design the lookup cache?")
    self.assertIn("## Verdict", packet)
    self.assertIn("## Proposed decision", packet)
    self.assertIn("## Scoreboard & Concession trail", packet)
    self.assertIn("**B** conceded to **A**", packet)
    self.assertIn("### B (Gemini 4 Carbon)", packet)
    self.assertIn("backed by SQLite write-ahead log", packet)

  def test_ls_runner_ignores_interim_text_until_finish(self) -> None:
    runner = dlr.LsDebateRunner(
        question="Q?",
        arms=[{"id": "m1", "displayName": "M1", "enum": "E1"}],
        scribe={"id": "s1", "displayName": "S1", "enum": "ES"},
    )
    runner.cascades["A"] = "cid-a"
    interim = {
        "status": "CORTEX_STEP_STATUS_DONE",
        "plannerResponse": {"response": "Starting subagent inspection..."},
    }
    finished = {
        "status": "CORTEX_STEP_STATUS_DONE",
        "metadata": {
            "toolCall": {
                "name": "finish",
                "argumentsJson": '{"result": "Final grounded design."}',
            }
        },
    }
    responses = [
        {"steps": []},
        {"steps": [interim]},
        {"steps": [interim]},
        {"steps": [interim, finished]},
        {"steps": [interim, finished]},
    ]

    def fake_rpc(method: str, body: dict) -> dict:
      del body
      if method == "GetCascadeTrajectorySteps":
        return responses.pop(0)
      return {}

    with mock.patch.object(dlr.cl, "ls_rpc", side_effect=fake_rpc), mock.patch.object(
        dlr.time, "sleep"
    ):
      out = runner.send_turn_and_wait(
          "A", {"id": "m1", "enum": "E1"}, "P", {"type": "string"}, timeout_sec=0.05
      )
    self.assertEqual(out, "Final grounded design.")

  def test_ls_runner_repair_failure_falls_back_without_none(self) -> None:
    runner = dlr.LsDebateRunner(
        question="Q?",
        arms=[
            {"id": "m1", "displayName": "M1", "enum": "E1"},
            {"id": "m2", "displayName": "M2", "enum": "E2"},
        ],
        scribe={"id": "s1", "displayName": "S1", "enum": "ES"},
    )
    runner.record.base_answers = {"A": "Ans A", "B": "Ans B"}
    with mock.patch.object(runner, "send_turn_and_wait", return_value="not valid json"):
      crit_phase, _ = runner._run_critique_phase(1)
      att = dc.Attack("r1-A-B-1", "Ans B", "bad", "ev", "B", "A")
      rev_phase = runner._run_revise_phase(1, [att])
    self.assertIsNotNone(crit_phase.critiques["A"])
    self.assertIsNotNone(rev_phase.revisions["B"])
    self.assertTrue(dc.all_unchanged(rev_phase, 2))

  # Verifies hidden debate cascades get a valid [HH:MM] lifecycle title so context_guard
  # never injects [TITLE GUARD] shell instructions into non-interactive arms
  def test_ls_runner_sets_valid_lifecycle_title_on_hidden_cascade(self) -> None:
    runner = dlr.LsDebateRunner(
        question="Q?",
        arms=[{"id": "gemini-4-carbon-high", "displayName": "Gemini 4 Carbon", "enum": "E1"}],
        scribe={"id": "s1", "displayName": "S1", "enum": "ES"},
    )
    calls: list[tuple[str, dict]] = []

    def fake_rpc(method: str, body: dict) -> dict:
      calls.append((method, body))
      if method == "StartCascade":
        return {"cascadeId": "cid-b"}
      return {}

    with mock.patch.object(dlr.cl, "ls_rpc", side_effect=fake_rpc):
      cid = runner._spawn_cascade(
          "B", {"id": "gemini-4-carbon-high", "enum": "E1"}, {"type": "string"}
      )
    self.assertEqual(cid, "cid-b")
    annot_calls = [b for m, b in calls if m == "UpdateConversationAnnotations"]
    self.assertEqual(len(annot_calls), 1)
    title = annot_calls[0]["annotations"]["title"]
    self.assertTrue(dlr.cl.has_lifecycle_title_prefix(title), msg=title)
    self.assertTrue(dlr.cl.is_valid_chat_topic(dlr.cl.clean_base_title(title)), msg=title)

  # Verifies send_turn_and_wait fails fast with diagnostics instead of hanging until timeout
  # when a hidden cascade stops IDLE with an error step and never calls finish
  def test_ls_runner_fails_fast_when_cascade_goes_idle_with_error(self) -> None:
    runner = dlr.LsDebateRunner(
        question="Q?",
        arms=[{"id": "m1", "displayName": "Gemini 4 Carbon", "enum": "E1"}],
        scribe={"id": "s1", "displayName": "S1", "enum": "ES"},
    )
    runner.cascades["A"] = "cid-a0"
    spawned = iter(["cid-a1", "cid-a2", "cid-a3"])
    err_step = {
        "type": "CORTEX_STEP_TYPE_ERROR_MESSAGE",
        "status": "CORTEX_STEP_STATUS_ERROR",
        "errorMessage": {"message": "HTTP 400 Invalid signature in thinking block"},
    }
    seen_initial: set[str] = set()
    injected_transient = False

    def fake_rpc(method: str, body: dict) -> dict:
      nonlocal injected_transient
      if method == "StartCascade":
        return {"cascadeId": next(spawned)}
      if method == "GetCascadeTrajectory":
        if not injected_transient:
          injected_transient = True
          raise RuntimeError("LS HTTP 503 transient")
        return {"status": "CASCADE_RUN_STATUS_IDLE"}
      if method == "GetCascadeTrajectorySteps":
        cid = body["cascadeId"]
        if cid not in seen_initial and cid == "cid-a0":
          seen_initial.add(cid)
          return {"steps": []}
        return {"steps": [err_step]}
      return {}

    with mock.patch.object(dlr.cl, "ls_rpc", side_effect=fake_rpc), mock.patch.object(
        dlr.time, "sleep"
    ):
      with self.assertRaises(RuntimeError) as ctx:
        runner.send_turn_and_wait(
            "A",
            {"id": "m1", "displayName": "Gemini 4 Carbon", "enum": "E1"},
            "Prompt",
            {"type": "string"},
            timeout_sec=0.3,
        )
    self.assertIn("IDLE", str(ctx.exception))
    self.assertIn("Invalid signature", str(ctx.exception))

  # Verifies Claude/Fable arms spawn a fresh cascade seeded with prior_context on Turn 2+
  # (including validation repair retries) and forbid intermediate tool calls before finish
  def test_ls_runner_claude_turn2_uses_fresh_seeded_cascade_and_single_step(self) -> None:
    claude_arm = {"id": "fable-5.2-high", "displayName": "Claude Fable 5.2", "enum": "EC"}
    gemini_arm = {"id": "gemini-4-carbon-high", "displayName": "Gemini 4 Carbon", "enum": "EG"}
    runner = dlr.LsDebateRunner(
        question="Must preserve zero allocations on hot path?",
        arms=[claude_arm, gemini_arm],
        scribe={"id": "s1", "displayName": "S1", "enum": "ES"},
    )
    runner.cascades = {"A": "cid-a1", "B": "cid-b1"}
    runner.turn_counts = {"A": 1, "B": 1}
    runner.record.base_answers = {
        "A": "Claude base position on lookup tables.",
        "B": "Gemini base position on lookup tables.",
    }

    spawn_seq = iter(["cid-a2", "cid-a3"])
    deleted_cids: list[str] = []
    sent_messages: list[tuple[str, str]] = []
    valid_a_crit = '{"B": {"analysis": "Solid.", "attacks": []}}'
    valid_b_crit = '{"A": {"analysis": "Clean.", "attacks": []}}'
    payload_by_cid = {
        "cid-a2": "broken non-json draft",
        "cid-a3": valid_a_crit,
        "cid-b1": valid_b_crit,
    }
    polled_pre: set[str] = set()

    def fake_rpc(method: str, body: dict) -> dict:
      if method == "DeleteCascadeTrajectory":
        deleted_cids.append(body["cascadeId"])
        return {}
      if method == "StartCascade":
        return {"cascadeId": next(spawn_seq)}
      if method == "SendUserCascadeMessage":
        sent_messages.append((body["cascadeId"], body["items"][0]["text"]))
        return {}
      if method == "GetCascadeTrajectorySteps":
        cid = body["cascadeId"]
        if cid in ("cid-a1", "cid-b1") and cid not in polled_pre:
          polled_pre.add(cid)
          return {"steps": []}
        res_val = payload_by_cid.get(cid, valid_a_crit)
        return {
            "steps": [{
                "status": "CORTEX_STEP_STATUS_DONE",
                "metadata": {
                    "toolCall": {
                        "name": "finish",
                        "argumentsJson": dlr.json.dumps({"result": res_val}),
                    }
                },
            }]
        }
      return {}

    with mock.patch.object(dlr.cl, "ls_rpc", side_effect=fake_rpc), mock.patch.object(
        dlr.time, "sleep"
    ):
      crit_phase, _ = runner._run_critique_phase(1)

    self.assertEqual(deleted_cids, ["cid-a1", "cid-a2"])
    self.assertNotIn("cid-b1", deleted_cids)
    msg_by_cid = dict(sent_messages)
    self.assertIn("cid-a2", msg_by_cid)
    self.assertIn("Must preserve zero allocations on hot path?", msg_by_cid["cid-a2"])
    self.assertIn("Claude base position on lookup tables.", msg_by_cid["cid-a2"])
    self.assertNotIn("You may use read-only inspection tools", msg_by_cid["cid-a2"])
    self.assertIn("ONLY tool call", msg_by_cid["cid-a2"])
    self.assertIn("cid-a3", msg_by_cid)
    self.assertIn("Must preserve zero allocations on hot path?", msg_by_cid["cid-a3"])
    self.assertIn("Claude base position on lookup tables.", msg_by_cid["cid-a3"])
    self.assertIn("broken non-json draft", msg_by_cid["cid-a3"])
    self.assertIn("You may use read-only inspection tools", msg_by_cid["cid-b1"])
    self.assertEqual(crit_phase.critiques["A"].critiques["B"].analysis, "Solid.")


if __name__ == "__main__":
  unittest.main()
