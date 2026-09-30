---
name: keeper-coroner
description: KEEPER Phase 11 escape inquest workflow (adapted for early-stage and local environments).
---
# KEEPER Coroner Workflow

When a severe architectural or functional defect is found (whether it escaped to main or was caught locally in a feature branch), follow the inquest workflow:

1. **Defect Triage (Escape vs. Local):** Identify if the bug bypassed existing invariants (Escape) or if the invariants simply haven't been written yet (Local/New Feature).
2. **Time-Machine Adversarial Falsification (Core Focus):** Reconstruct the exact state where the defect was introduced. Ask: "What failing test or invariant should have been written *before* this code to prevent this bug?" Write that failing test now.
3. **5 Whys (Constitutional Root Cause):** Perform root cause analysis targeting the constitutional/invariant rules. Why didn't the developer or Trapsmith anticipate this edge case?
4. **Inquest Header:** Log the inquest details formally in the commit message.
5. **Atomic Git Triplet:** Produce the test that catches it (from step 2), the fix, and the required invariant update as a single atomic commit.
