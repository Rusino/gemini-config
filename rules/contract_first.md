---
trigger: model_decision
description: "When starting a new feature, pipeline or behavior change in Skia, SkParagraph or Flutter text code, or writing a new test suite."
---

# Contract First (trial)

This is a trial from Nov 8 2026; real work may need a different order. A small bug fix may skip
it: the test header then names the bug.

Order of work:

1. Contract catalog first. Each contract is one sentence about user-visible behavior, in the terms
   of the public API the clients see (Flutter `dart:ui` names for SkParagraph work, never internal
   method names). Each one names its source (API doc, native platform, pinned bug, text invariant
   or user decision) and links a confirming bug if there is one. Internal guarantees go in a
   separate section. Expected visible differences from the existing engine are listed with reasons
2. The user reviews the catalog (a Google Doc with a comment column is fine)
3. Failing contract tests through the public API, one per contract; the test header names the
   contract ID or the bug. `keeper_trapsmith` can author them
4. Only then code

Tests check properties, not equality with another engine or with native output. Run counts and
other internal structure are not contracts.

Precedent: SkoreText, Nov 8 2026
([contracts doc](https://docs.google.com/document/d/1s4nhwMEv-mHc6IkxVMWU0qxfpYhuiclxnpbhP1Rtrys/edit)):
HarfBuzz-parity tests and run-count asserts had to be rewritten after the fact.
