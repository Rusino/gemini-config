#!/usr/bin/env python3
# no-verify
"""Mode A: Native Jetski multi-agent debate workflow.

Orchestrates multi-turn agents with per-turn finish JSON schemas and adversarial lenses
using Jetski's run_workflow primitives (phase, agent, parallel, log).
"""

from __future__ import annotations

import os
import sys

# run_workflow executes scripts without setting __file__, whereas test imports set __file__
SCRIPT_DIR = (
    os.path.dirname(os.path.abspath(__file__))
    if "__file__" in globals()
    else os.path.expanduser("~/.gemini/config/skills/debate/scripts")
)
if SCRIPT_DIR not in sys.path:
  sys.path.insert(0, SCRIPT_DIR)

import debate_core as dc


async def main() -> None:
  # Injected run_workflow globals
  workflow_args = globals().get("args", {})
  wf_log = globals().get("log", print)
  wf_phase = globals().get("phase", lambda _: None)
  wf_agent = globals().get("agent")
  wf_parallel = globals().get("parallel")

  question = str(workflow_args.get("question", "")).strip()
  if not question:
    raise ValueError("debate_workflow requires non-empty 'question' in args")

  rounds = int(workflow_args.get("rounds", 1))
  rounds = max(0, min(dc.MAX_ROUNDS, rounds))

  model = str(workflow_args.get("model", "inherit")).strip() or "inherit"
  scribe_model = str(workflow_args.get("scribe_model", "inherit")).strip() or "inherit"
  prior_context = str(workflow_args.get("prior_context", "")).strip()
  output_path = str(workflow_args.get("output_path", "")).strip()

  custom_lenses = workflow_args.get("lenses")
  if isinstance(custom_lenses, list) and custom_lenses:
    raw_lenses = [
        (str(item.get("name", f"Lens {i+1}")), str(item.get("instruction", "")))
        for i, item in enumerate(custom_lenses)
        if isinstance(item, dict)
    ]
  else:
    raw_lenses = dc.DEFAULT_LENSES

  participants = [
      dc.Participant(
          name=chr(ord("A") + i),
          model=f"{lens_name} [{model}]",
          lens_instruction=lens_desc,
      )
      for i, (lens_name, lens_desc) in enumerate(raw_lenses)
  ]

  record = dc.DebateRecord(
      rounds=rounds,
      scribe_model=f"Scribe [{scribe_model}]",
      participants=participants,
  )

  wf_log(f"Starting {len(participants)}-arm debate with {rounds} round(s)")

  # Phase 0: Independent generation
  wf_phase("Phase 0: Independent Generation")
  arm_agents = {}
  for p in participants:
    arm_agents[p.name] = wf_agent(
        prompt=None,
        role=f"Arm {p.name}: {p.model}",
        model=model,
    )

  async def run_gen(p: dc.Participant) -> tuple[str, str]:
    prompt = dc.build_generate_prompt(
        question,
        lens_name=p.model,
        lens_desc=p.lens_instruction,
        prior_context=prior_context,
    )
    ans = await arm_agents[p.name](prompt, {"type": "string"})
    return p.name, str(ans).strip()

  gen_results = await wf_parallel(participants, run_gen)
  for p_name, ans in gen_results:
    record.base_answers[p_name] = ans
    wf_log(f"Arm {p_name} produced base answer ({len(ans.split())} words)")

  # Adversarial critique / revise loop
  for r_num in range(1, rounds + 1):
    wf_phase(f"Round {r_num}: Critique")

    async def run_critique(p: dc.Participant) -> tuple[str, dc.CritiqueReport]:
      peers = [
          (other.name, dc.get_current_answer(record, other.name))
          for other in participants
          if other.name != p.name
      ]
      peers_dict = dict(peers)
      c_prompt = dc.build_critique_prompt(p.name, r_num, rounds, peers)
      c_schema = dc.critique_schema([other_name for other_name, _ in peers])

      raw_report = await arm_agents[p.name](c_prompt, c_schema)
      parsed, err = dc.parse_critique(raw_report, r_num, p.name, peers_dict)
      if err:
        wf_log(f"Arm {p.name} critique validation error: {err}; requesting repair")
        repair_prompt = dc.build_repair_prompt(r_num, "critique", 1, err)
        raw_repair = await arm_agents[p.name](repair_prompt, c_schema)
        parsed, err2 = dc.parse_critique(raw_repair, r_num, p.name, peers_dict)
        if err2:
          wf_log(f"Arm {p.name} repair failed: {err2}; falling back to empty critique")
          parsed = dc.CritiqueReport(
              critiques={
                  other_name: dc.PeerCritique(
                      analysis=f"Validation failed: {err2}",
                      attacks=[],
                  )
                  for other_name, _ in peers
              }
          )
      return p.name, parsed

    crit_results = await wf_parallel(participants, run_critique)
    crit_phase = dc.PhaseRecord(
        kind="critique",
        round_num=r_num,
        critiques=dict(crit_results),
    )
    record.phases.append(crit_phase)

    all_attacks: list[dc.Attack] = []
    for rep in crit_phase.critiques.values():
      for peer_crit in rep.critiques.values():
        all_attacks.extend(peer_crit.attacks)

    wf_log(f"Round {r_num} critiques submitted: {len(all_attacks)} attack(s) raised")

    wf_phase(f"Round {r_num}: Revise")

    async def run_revise(p: dc.Participant) -> tuple[str, dc.ReviseReport]:
      attacks_on_p = [a for a in all_attacks if a.target == p.name]
      if not attacks_on_p:
        wf_log(f"Arm {p.name} received 0 attacks; skipping revise turn")
        return p.name, dc.ReviseReport(
            verdicts={},
            status="unchanged",
            answer="",
            unchanged=True,
        )

      r_prompt = dc.build_revise_prompt(p.name, r_num, rounds, attacks_on_p)
      r_schema = dc.revise_schema(attacks_on_p)
      raw_rev = await arm_agents[p.name](r_prompt, r_schema)
      parsed, err = dc.parse_revise(raw_rev, attacks_on_p)
      if err:
        wf_log(f"Arm {p.name} revise validation error: {err}; requesting repair")
        repair_prompt = dc.build_repair_prompt(r_num, "revise", 1, err)
        raw_repair = await arm_agents[p.name](repair_prompt, r_schema)
        parsed, err2 = dc.parse_revise(raw_repair, attacks_on_p)
        if err2:
          wf_log(f"Arm {p.name} revise repair failed: {err2}; keeping prior answer")
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

    rev_results = await wf_parallel(participants, run_revise)
    rev_phase = dc.PhaseRecord(
        kind="revise",
        round_num=r_num,
        revisions=dict(rev_results),
    )
    record.phases.append(rev_phase)

    if dc.all_unchanged(rev_phase, len(participants)):
      wf_log(f"All arms unchanged in round {r_num}; stopping early")
      record.stopped_early = True
      break

  # Teardown multi-turn arm conversations before synthesis
  del arm_agents

  # Scribe Synthesis
  wf_phase("Synthesis: Independent Scribe")
  synth_prompt = dc.build_synthesize_prompt(
      question,
      record,
      prior_context=prior_context,
  )
  scribe = wf_agent(
      prompt=None,
      role=f"Debate Scribe [{scribe_model}]",
      model=scribe_model,
  )
  raw_synth = await scribe(synth_prompt, dc.synthesis_schema())
  synth_rep, s_err = dc.parse_synthesis(raw_synth)
  if s_err:
    wf_log(f"Scribe synthesis parsing error: {s_err}; fallback synthesis generated")
    synth_rep = dc.SynthesisReport(
        verdict=f"Synthesis generated with parsing warning: {s_err}",
        consensus="Review individual arm positions in the packet below.",
        remaining_disagreements="See individual rulings in the scoreboard.",
        scribes_call="Refer to final positions.",
        proposed_decision="Review participant positions and concession trail.",
    )

  record.phases.append(
      dc.PhaseRecord(
          kind="synthesize",
          round_num=len([p for p in record.phases if p.kind == "critique"]),
          synthesis=synth_rep,
      )
  )
  record.status = "done"

  packet = dc.render_packet(record, question)
  if output_path:
    try:
      with open(output_path, "w", encoding="utf-8") as f:
        f.write(packet)
      wf_log(f"Saved debate packet to {output_path}")
    except OSError as e:
      wf_log(f"Warning: could not write to {output_path}: {e}")

  print(packet)
  wf_log("Debate complete")


# Only invoke main() inside Jetski's run_workflow runtime where agent() is injected
if callable(globals().get("agent")):
  await main()
