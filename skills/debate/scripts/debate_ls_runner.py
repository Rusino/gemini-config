#!/usr/bin/env python3
"""Mode B: Cross-model Language Server runner for structured multi-model debates.

Spawns hidden conversations pinned to distinct frontier models across provider families
(e.g., Claude, Gemini, GLM, Kimi) via local ConnectRPC and orchestrates debate_core rounds.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOOKS_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", "hooks"))
for d in (SCRIPT_DIR, HOOKS_DIR):
  if d not in sys.path:
    sys.path.insert(0, d)

import chat_lifecycle as cl
import debate_core as dc


def list_catalog_models() -> None:
  """Prints enabled catalog models grouped by provider family."""
  resp = cl.get_available_models()
  models = resp.get("models") or {}
  print(f"{'ID':32s} | {'Display Name':30s} | {'Model Enum'}")
  print("-" * 90)
  for mid, det in sorted(models.items()):
    if det.get("disabled") or not det.get("displayName"):
      continue
    disp = det.get("displayName", "")
    m_enum = det.get("model", "")
    print(f"{mid:32s} | {disp:30s} | {m_enum}")


def resolve_model_or_fail(models: dict, name: str) -> dict:
  """Resolves a model by id, alias, or display name; fails fast with guidance."""
  hit = cl.lookup_model(models, name)
  if not hit or not hit.get("enum"):
    raise ValueError(f"Unknown or disabled model {name!r}. Run with --list-models to see choices.")
  return hit


def detect_provider_family(display_name: str, model_id: str) -> str:
  """Identifies the model provider family (claude, gemini, glm, kimi)."""
  text = f"{display_name} {model_id}".lower()
  if "claude" in text or "opus" in text or "sonnet" in text or "fable" in text:
    return "claude"
  if "gemini" in text:
    return "gemini"
  if "glm" in text:
    return "glm"
  if "kimi" in text:
    return "kimi"
  return "other"


def auto_pick_scribe(models: dict, arm_families: set[str]) -> dict:
  """Picks an enabled frontier model from a family distinct from the debate arms."""
  candidates = [
      "fable-5.1-high",
      "opus-5.5-high",
      "gemini-launch-candidate-high",
      "gemini-4-carbon-high",
      "glm-5.2-fp8-bon-arm",
  ]
  # Try candidate from an unused family first
  for cand in candidates:
    hit = cl.lookup_model(models, cand)
    if hit and not hit.get("disabled"):
      fam = detect_provider_family(hit.get("displayName", ""), hit.get("id", ""))
      if fam not in arm_families:
        return hit
  # Fallback to any enabled frontier candidate
  for cand in candidates:
    hit = cl.lookup_model(models, cand)
    if hit and not hit.get("disabled"):
      return hit
  # Last resort
  return list(models.values())[0]


def wrap_result_schema(schema: dict | None) -> str:
  """Nests result schema inside required object property matching Jetski finish schema."""
  if not schema:
    return ""
  wrapper = {"type": "object", "properties": {"result": schema}, "required": ["result"]}
  return json.dumps(wrapper, separators=(",", ":"))


def finish_cascade_config(wrapped_schema: str, model_enum: str, max_forced: int) -> dict:
  """Constructs turn CascadeConfig requiring finish with schema and forced budget."""
  return {
      "executorConfig": {
          "requireFinishTool": True,
          "maxForcedInvocations": max_forced,
      },
      "plannerConfig": {
          "planModel": model_enum,
          "toolConfig": {
              "finish": {"resultJsonSchemaString": wrapped_schema},
          },
      },
  }


class LsDebateRunner:
  """Orchestrates a structured debate across distinct models via Language Server ConnectRPC."""

  def __init__(self, question: str, arms: list[dict], scribe: dict, rounds: int = 1):
    self.question = question
    self.rounds = max(0, min(dc.MAX_ROUNDS, rounds))
    self.arms = arms
    self.scribe = scribe
    self.participants = [
        dc.Participant(
            name=chr(ord("A") + i),
            model=m.get("displayName") or m.get("id"),
        )
        for i, m in enumerate(arms)
    ]
    self.record = dc.DebateRecord(
        rounds=self.rounds,
        scribe_model=scribe.get("displayName") or scribe.get("id"),
        participants=self.participants,
    )
    self.cascades: dict[str, str] = {}  # participant/scribe name -> cascade_id
    self.turn_counts: dict[str, int] = {}

  def _spawn_cascade(self, target_name: str, model_info: dict, initial_schema: dict | None) -> str:
    """Spawns a hidden cascade for an arm or scribe with finish tool pre-mounted."""
    project_id = os.environ.get("ANTIGRAVITY_PROJECT_ID", "")
    wrapped = wrap_result_schema(initial_schema) if initial_schema else ""
    body: dict = {
        "source": "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT",
        "trajectoryType": "CORTEX_TRAJECTORY_TYPE_CASCADE",
        "sourceMetadata": {
            "hideFromConversationList": True,
        },
        "requestedModel": model_info.get("enum"),
        "customAgentSpec": {
            "builtinAgent": {
                "defaultAgent": {
                    "isGoogle": True,
                    "isInteractive": False,
                    "finishToolSchema": wrapped,
                }
            },
            "cascadeConfig": {
                "plannerConfig": {"planModel": model_info.get("enum")},
            },
        },
    }
    if project_id and project_id != "outside-of-project":
      body["projectEnvConfig"] = {"projectId": project_id, "defaultProjectEnvironment": {}}

    resp = cl.ls_rpc("StartCascade", body)
    cid = resp.get("cascadeId") or (resp.get("response") or {}).get("cascadeId")
    if not cid:
      raise RuntimeError(f"StartCascade failed for {target_name}: {resp}")
    self.cascades[target_name] = cid
    self.turn_counts[target_name] = 0
    cl.ls_rpc(
        "UpdateConversationAnnotations",
        {
            "cascadeIds": [cid],
            "annotations": {"title": f"Debate {target_name} ({model_info.get('id', '')})"},
            "mergeAnnotations": True,
        },
    )
    return cid

  def start_cascades(self) -> None:
    """Spawns hidden conversations for all participant arms."""
    for p, model_info in zip(self.participants, self.arms):
      self._spawn_cascade(p.name, model_info, {"type": "string"})

  def send_turn_and_wait(
      self,
      target_name: str,
      model_info: dict,
      prompt: str,
      schema: dict | None,
      timeout_sec: int = 600,
  ) -> str:
    """Sends a turn with an enforced finish schema and waits until the agent calls finish."""
    cid = self.cascades[target_name]
    self.turn_counts[target_name] = self.turn_counts.get(target_name, 0) + 1
    turn_num = self.turn_counts[target_name]

    # Record from_step before sending message so earlier turns never match
    pre_resp = cl.ls_rpc("GetCascadeTrajectorySteps", {"cascadeId": cid})
    pre_steps = (pre_resp.get("response") or {}).get("steps") or pre_resp.get("steps") or []
    from_step = len(pre_steps)

    wrapped = wrap_result_schema(schema) if schema else ""
    cfg = finish_cascade_config(wrapped, model_info.get("enum"), 3 * turn_num)
    req: dict = {
        "cascadeId": cid,
        "items": [{"text": prompt + "\n\nWhen done, call the finish tool with your result."}],
        "blocking": False,
        "cascadeConfig": cfg,
    }

    cl.ls_rpc("SendUserCascadeMessage", req)

    # Poll for completion with two-fingerprint debounce
    start_time = time.time()
    last_fp = ""
    while time.time() - start_time < timeout_sec:
      time.sleep(2.5)
      try:
        r = cl.ls_rpc("GetCascadeTrajectorySteps", {"cascadeId": cid})
        steps = (r.get("response") or {}).get("steps") or r.get("steps") or []
        done_step = None
        for s in reversed(steps[from_step:]):
          tool_call = (s.get("metadata") or {}).get("toolCall") or {}
          if tool_call.get("name") == "finish":
            done_step = s
            break
        # Wait strictly for the finish tool call enforced by requireFinishTool so
        # intermediate status messages before tool/subagent steps are never captured
        if done_step:
          status = done_step.get("status")
          raw_args = ((done_step.get("metadata") or {}).get("toolCall") or {}).get("argumentsJson")
          if status == "CORTEX_STEP_STATUS_DONE" and raw_args:
            cur_fp = f"{status}:{raw_args}"
            if cur_fp == last_fp:
              parsed_args = json.loads(raw_args)
              res = parsed_args.get("result", parsed_args)
              return json.dumps(res) if not isinstance(res, str) else res
            last_fp = cur_fp
      except Exception:  # lint: allow-broad-except (Language Server RPC errors)
        pass

    raise TimeoutError(f"Turn on {target_name} ({model_info['id']}) timed out after {timeout_sec}s")

  def _run_generation_phase(self) -> None:
    """Executes Phase 0 independent generation across debating arms."""
    print("--- Phase 0: Independent Generation ---")
    with ThreadPoolExecutor(max_workers=len(self.participants)) as pool:
      futs = {
          pool.submit(
              self.send_turn_and_wait,
              p.name,
              self.arms[i],
              dc.build_generate_prompt(self.question),
              {"type": "string"},
          ): p.name
          for i, p in enumerate(self.participants)
      }
      for fut in futs:
        p_name = futs[fut]
        ans = fut.result()
        self.record.base_answers[p_name] = ans
        print(f"Arm {p_name} ({self.arms[ord(p_name)-ord('A')]['id']}) answered.")

  def _run_critique_phase(self, r_num: int) -> tuple[dc.PhaseRecord, list[dc.Attack]]:
    """Collects adversarial critiques for round r_num across all arms."""
    print(f"--- Round {r_num}: Critique ---")
    critiques: dict[str, dc.CritiqueReport] = {}

    def do_critique(p: dc.Participant, idx: int) -> tuple[str, dc.CritiqueReport]:
      peers = [
          (o.name, dc.get_current_answer(self.record, o.name))
          for o in self.participants
          if o.name != p.name
      ]
      peers_dict = dict(peers)
      c_prompt = dc.build_critique_prompt(p.name, r_num, self.rounds, peers)
      c_schema = dc.critique_schema([other_name for other_name, _ in peers])
      raw_str = self.send_turn_and_wait(p.name, self.arms[idx], c_prompt, c_schema)
      parsed, err = dc.parse_critique(raw_str, r_num, p.name, peers_dict)
      if err:
        print(f"Arm {p.name} critique error: {err}; retrying with repair")
        rep_prompt = dc.build_repair_prompt(r_num, "critique", 1, err)
        raw_rep = self.send_turn_and_wait(p.name, self.arms[idx], rep_prompt, c_schema)
        parsed, err2 = dc.parse_critique(raw_rep, r_num, p.name, peers_dict)
        if err2 or parsed is None:
          parsed = dc.CritiqueReport(
              critiques={
                  other_name: dc.PeerCritique(analysis=f"Validation failed: {err2}", attacks=[])
                  for other_name, _ in peers
              }
          )
      return p.name, parsed

    with ThreadPoolExecutor(max_workers=len(self.participants)) as pool:
      futs = {
          pool.submit(do_critique, p, i): p.name for i, p in enumerate(self.participants)
      }
      for fut in futs:
        name, rep = fut.result()
        critiques[name] = rep

    crit_phase = dc.PhaseRecord(kind="critique", round_num=r_num, critiques=critiques)
    all_attacks: list[dc.Attack] = []
    for rep in critiques.values():
      for peer_crit in rep.critiques.values():
        all_attacks.extend(peer_crit.attacks)
    return crit_phase, all_attacks

  def _run_revise_phase(self, r_num: int, all_attacks: list[dc.Attack]) -> dc.PhaseRecord:
    """Collects attack concessions and revised positions for round r_num."""
    print(f"--- Round {r_num}: Revise ({len(all_attacks)} attacks) ---")
    revisions: dict[str, dc.ReviseReport] = {}

    def do_revise(p: dc.Participant, idx: int) -> tuple[str, dc.ReviseReport]:
      attacks_on_p = [a for a in all_attacks if a.target == p.name]
      if not attacks_on_p:
        return p.name, dc.ReviseReport(verdicts={}, status="unchanged", answer="", unchanged=True)
      r_prompt = dc.build_revise_prompt(p.name, r_num, self.rounds, attacks_on_p)
      r_schema = dc.revise_schema(attacks_on_p)
      raw_str = self.send_turn_and_wait(p.name, self.arms[idx], r_prompt, r_schema)
      parsed, err = dc.parse_revise(raw_str, attacks_on_p)
      if err:
        print(f"Arm {p.name} revise error: {err}; retrying with repair")
        rep_prompt = dc.build_repair_prompt(r_num, "revise", 1, err)
        raw_rep = self.send_turn_and_wait(p.name, self.arms[idx], rep_prompt, r_schema)
        parsed, err2 = dc.parse_revise(raw_rep, attacks_on_p)
        if err2 or parsed is None:
          parsed = dc.ReviseReport(
              verdicts={
                  a.id: dc.Verdict(a.id, "held", f"Repair validation failed: {err2}")
                  for a in attacks_on_p
              },
              status="unchanged",
              answer="",
              unchanged=True,
          )
      return p.name, parsed

    with ThreadPoolExecutor(max_workers=len(self.participants)) as pool:
      futs = {
          pool.submit(do_revise, p, i): p.name for i, p in enumerate(self.participants)
      }
      for fut in futs:
        name, rep = fut.result()
        revisions[name] = rep

    return dc.PhaseRecord(kind="revise", round_num=r_num, revisions=revisions)

  def _run_synthesis_phase(self) -> None:
    """Spawns the independent Scribe and records final consensus synthesis."""
    print("--- Phase: Scribe Synthesis ---")
    self._spawn_cascade("scribe", self.scribe, dc.synthesis_schema())
    synth_prompt = dc.build_synthesize_prompt(self.question, self.record)
    raw_synth = self.send_turn_and_wait(
        "scribe", self.scribe, synth_prompt, dc.synthesis_schema()
    )
    synth_rep, s_err = dc.parse_synthesis(raw_synth)
    if s_err or synth_rep is None:
      synth_rep = dc.SynthesisReport(
          verdict=f"Synthesis generated with parsing warning: {s_err}",
          consensus="Review individual arm positions in the packet below.",
          remaining_disagreements="See individual rulings in the scoreboard.",
          scribes_call="Refer to final positions.",
          proposed_decision="Review participant positions and concession trail.",
      )
    self.record.phases.append(
        dc.PhaseRecord(
            kind="synthesize",
            round_num=len([p for p in self.record.phases if p.kind == "critique"]),
            synthesis=synth_rep,
        )
    )
    self.record.status = "done"

  def run_debate(self) -> str:
    """Executes the complete multi-model debate loop."""
    print(f"Starting {len(self.participants)}-arm cross-model debate ({self.rounds} rounds)")
    self.start_cascades()
    self._run_generation_phase()

    for r_num in range(1, self.rounds + 1):
      crit_phase, all_attacks = self._run_critique_phase(r_num)
      self.record.phases.append(crit_phase)
      rev_phase = self._run_revise_phase(r_num, all_attacks)
      self.record.phases.append(rev_phase)
      if dc.all_unchanged(rev_phase, len(self.participants)):
        print(f"All arms unchanged in round {r_num}; stopping early")
        self.record.stopped_early = True
        break

    self._run_synthesis_phase()
    return dc.render_packet(self.record, self.question)

  def cleanup(self) -> None:
    """Deletes temporary hidden arm and scribe cascades."""
    for name, cid in self.cascades.items():
      try:
        cl.ls_rpc("DeleteCascadeTrajectory", {"cascadeId": cid})
      except Exception:  # lint: allow-broad-except (Language Server RPC errors)
        pass


def main() -> None:
  parser = argparse.ArgumentParser(description="Cross-model debate runner via Language Server")
  parser.add_argument("--list-models", action="store_true", help="List enabled models and exit")
  parser.add_argument("--question", type=str, default="", help="Question to debate")
  parser.add_argument(
      "--arms",
      type=str,
      default="opus-5.5-high,gemini-4-carbon-high",
      help="Comma-separated model IDs for the debating arms",
  )
  parser.add_argument(
      "--rounds", type=int, default=1, help="Adversarial critique/revise rounds (0..3)"
  )
  parser.add_argument(
      "--scribe", type=str, default="", help="Model ID for the independent Scribe"
  )
  parser.add_argument(
      "--out", type=str, default="", help="Output path for debate packet Markdown"
  )
  parser.add_argument(
      "--keep-cascades", action="store_true", help="Do not delete hidden cascades on exit"
  )
  args = parser.parse_args()

  if args.list_models:
    list_catalog_models()
    return

  if not args.question.strip():
    parser.error("--question is required")

  resp = cl.get_available_models()
  catalog = resp.get("models") or {}

  arm_ids = [s.strip() for s in args.arms.split(",") if s.strip()]
  if len(arm_ids) < 2:
    parser.error("Debate requires at least 2 arms")

  resolved_arms = [resolve_model_or_fail(catalog, mid) for mid in arm_ids]
  arm_fams = {detect_provider_family(m["displayName"], m["id"]) for m in resolved_arms}

  if args.scribe.strip():
    resolved_scribe = resolve_model_or_fail(catalog, args.scribe.strip())
  else:
    resolved_scribe = auto_pick_scribe(catalog, arm_fams)

  runner = LsDebateRunner(
      question=args.question.strip(),
      arms=resolved_arms,
      scribe=resolved_scribe,
      rounds=args.rounds,
  )

  try:
    packet = runner.run_debate()
    if args.out:
      with open(args.out, "w", encoding="utf-8") as f:
        f.write(packet)
      print(f"Saved debate packet to {args.out}")
    print("\n" + packet)
  finally:
    if not args.keep_cascades:
      runner.cleanup()


if __name__ == "__main__":
  main()
