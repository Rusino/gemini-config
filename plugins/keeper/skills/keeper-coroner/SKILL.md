---
name: keeper-coroner
description: KEEPER Phase 11 escape inquest workflow.
---
# KEEPER Coroner Workflow

When a defect escapes into production or main branch, follow the Phase 11 escape inquest:
1. **Escape Inquest:** Identify precisely how the defect bypassed existing invariants.
2. **Inquest Header:** Log the inquest details formally.
3. **5 Whys (Physical/Pipeline/Constitutional):** Perform root cause analysis targeting the physical domain, the pipeline/automation, and the constitutional/invariant rules.
4. **Time-Machine Adversarial Falsification:** Reconstruct the exact state where the defect was introduced and falsify it against new invariants.
5. **Atomic Git Triplet:** Produce the test that catches it, the fix, and the invariant update as a single atomic commit.
