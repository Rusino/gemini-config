---
name: debate
description: >-
  Run structured multi-agent and multi-model debates on architectural design decisions,
  competing root-cause bug hypotheses, or complex trade-offs with adversarial critique,
  per-attack rulings, and deterministic concession synthesis. Trigger when asked to "проведи
  дебаты", "устрой дебаты", "debate this design", or when choosing between non-trivial
  architectural alternatives before writing code.
---

# Structured Multi-Agent / Multi-Model Debate (`debate`)

A structured debate is an adversarial evaluation protocol for high-stakes technical decisions
where multiple candidate approaches exist. Unlike unstructured group chats or naive Best-of-N
evaluations, this skill enforces a strict state machine with load-bearing attack caps,
deterministic quote verification, per-attack rulings, and a tool-less Scribe synthesis.

---

## When to Use vs. When NOT to Use

* **Use for**:
  - High-ambiguity architectural forks (e.g., in-memory LRU caching vs. lock-free flat tables vs.
    WAL persistence)
  - Competing root-cause hypotheses for subtle regressions (e.g., BiDi wrap vs. trailing space
    collapse vs. font fallback metrics)
  - Design review before authoring complex cross-layer CLs or specs
  - Stress-testing trade-offs across orthogonal engineering lenses
* **Do NOT use for**:
  - Simple bug fixes or localized refactors where a unit test or compiler immediately decides truth
  - Routine file editing, typo fixes, or mechanical renames
  - Tasks with clear, single-approach answers (debates cost $O(N \times K)$ turns and minutes)

---

## Interactive Battle Mode (/battle) vs. Structured `debate`

| Situation | Preferred Tool | Quota & Execution Rules |
|---|---|---|
| **Fresh chat (`<= 20` steps, 0 active background tasks/subagents)** with a 2-way root-cause question, minimal C++ patch choice, Tier-2 review/commit phrasing, or depleted primary `Claude` quota | **Interactive Battle Mode (/battle or /bestofn) in Jetski UI** | Server-assigned `SERVING_TYPE_BON` arms draw from a separate experimental quota pool rather than the primary `Claude` / `Gemini` buckets; always click a winner in the UI after comparing both answers |
| **Multi-round adversarial cross-examination**, verbatim `quote_in` verification, deterministic concession ledger, 3+ orthogonal lenses, or **mid-session chats (`> 20` steps / active tasks)** | **Structured `debate` skill (`Mode A` or `Mode B`)** | Uses standard enabled frontier or `flash` models; never pass `-bon-` model IDs directly (`hooks/chat_lifecycle.py` and `scripts/debate_ls_runner.py` block direct `SERVING_TYPE_BON` calls per b/580403511), and never use `advanced.best_of` to save quota (it runs $n + 1$ standard subagents on your own quota) |

### Proactive User Reminder Protocol
- **When a fork arises naturally (without the user typing `"проведи дебаты"`)**: Whenever an open architectural trade-off or `>= 2` competing root-cause hypotheses appear, include `"Провести перекрёстные дебаты моделей (скилл debate)"` as a 1-click option in `ask_question` (and if `steps <= 20` with 0 active tasks, add a 1-line tip mentioning the /battle UI command) so the user never has to remember the command or skill name on her own.
- **When the user explicitly asks for `"проведи дебаты"`**: Run the `debate` skill directly without stalling; if the chat is fresh (`<= 20` steps) and the question is a simple 2-way comparison, include a 1-line note at the end reminding that /battle in the UI is also available for blind `BoN`-pool comparisons.

---

## Two Execution Modes

Both modes share the pure state machine, schemas, quote verifier, and concession ledger in [`scripts/debate_core.py`](scripts/debate_core.py).

| Dimension | Mode A: Native Workflow (`debate_workflow.py`) | Mode B: Cross-Model Runner (`debate_ls_runner.py`) |
|---|---|---|
| **Mechanism** | `run_workflow` tool with stateful `agent()` | Language Server ConnectRPC (`StartCascade`) |
| **Diversity Source** | Orthogonal engineering lenses (`Invariant Purist`, `Systems/Perf Pragmatist`, `Edge-Case Skeptic`) | Cross-provider frontier models (`Claude Opus 5.5`, `Gemini 4 Carbon`, `Claude Fable 5.1`, `GLM 5.2`) |
| **UI Integration** | Renders live phase cards directly in the Jetski chat UI | Runs via command-line, produces clean Markdown packet |
| **Model Selection** | Inherited tier or explicit tier (`flash`, `inherit`, `flash_lite`) | Specific enabled frontier model IDs from `GetAvailableModels` |

---

## Core Invariants

1. **Load-Bearing Attacks Only (`MAX_ATTACKS_PER_PEER = 2`)**:
   Participants are forbidden from raising cosmetic or stylistic nitpicks. Each attack must target
   the load-bearing claim upon which the peer's conclusion depends. If a peer's position has no
   material defect, silence (`attacks: []`) is explicitly valid.
2. **Deterministic Quote Grounding (`quote_in`)**:
   Every attack must quote the attacked claim verbatim. The runner normalizes whitespace, markdown
   formatting, and quotation marks, flagging fabricated quotes as `(unverified quote)`.
3. **Semantic Invariant Gate (`conceded` $\implies$ `revised`)**:
   In the Revise phase, every incoming attack ID must be ruled upon (`conceded`,
   `partially_conceded`, or `held`). Ruling `conceded` strictly forbids leaving the position
   `unchanged`. Ruling `partially_conceded` with `unchanged` requires substantive justification
   explaining why the core conclusion remains unaffected without text edits.
4. **Deterministic Concession Trail**:
   The Scribe does not hallucinate who conceded what. The concession trail is computed
   deterministically from the attack ledger.
5. **Tool-less Scribe Synthesis**:
   The Scribe has no tools, preventing rabbit-hole research during synthesis. Its prompt enforces
   strict separation between the factual record (quoted and attributed) and the Scribe's own
   judgment (`Scribe's call`).

---

## Usage

### Mode A: Native Jetski Workflow

Call the `run_workflow` tool passing the script path and question:

<!-- no-validate -->
```python
run_workflow(
    ScriptPath="/usr/local/google/home/jlavrova/.gemini/config/skills/debate/scripts/debate_workflow.py",
    Description="Debate caching architecture across 3 engineering lenses",
    Args={
        "question": "How should we design thread-safe face caching in FontCollection?",
        "rounds": 1,
        "model": "flash",  # or "inherit"
        "scribe_model": "flash",
        "prior_context": "",
        "output_path": "/tmp/font_cache_debate.md",
    },
)
```
<!-- end-no-validate -->

Custom lenses can be supplied in `Args["lenses"]`:

```json
[
  {"name": "Upstream Invariant Lens", "instruction": "Focus on root-cause font data invariants."},
  {"name": "Hot-Path Perf Lens", "instruction": "Focus on zero-allocation lock-free read paths."},
  {"name": "Cross-Platform Lens", "instruction": "Focus on ICU vs CoreText vs FreeType differences."}
]
```

### Mode B: Cross-Model Language Server CLI

Run the runner directly in the terminal:

<!-- no-validate -->
```bash
# List available models
python3 ~/.gemini/config/skills/debate/scripts/debate_ls_runner.py --list-models

# Run a 2-arm debate between Claude and Gemini with an independent Scribe
python3 ~/.gemini/config/skills/debate/scripts/debate_ls_runner.py \
  --question "Should FontCollection FaceCache use std::shared_mutex or an atomic hazard pointer?" \
  --arms "opus-5.5-high,gemini-4-carbon-high" \
  --scribe "fable-5.1-high" \
  --rounds 1 \
  --out /tmp/debate_packet.md
```
<!-- end-no-validate -->
