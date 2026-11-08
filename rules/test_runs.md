---
trigger: model_decision
description: "When running Skia/Flutter/google3 paragraph test orchestrators (skia-test-paragraph, flutter-test-paragraph-engine, webparagraph-corpus, google3-test-paragraph), corpus gates, roll triage, or reading their logs."
---

# Running Tests and Roll Triage

- In Plan Mode, do not run orchestrators on the planning model: call `exit_plan_mode` with a
  1-step plan containing (1) the exact command line, (2) a ban on unrequested bypass flags
  (`--allow-dirty-flutter`, `--allow-dirty-skia`, `--allow-skia-base-mismatch`, `--skip-build`,
  `--skip-cpp`, `--skip-analyze`, `--no-force-test-fonts`), (3) pasting `<out>/compare.md`
  verbatim, including `Паспорт прогона (Run Attestation)` and `Вердикт целостности
  (Integrity Check)`, (4) `Re-plan if:` pre-flight/build fails, Integrity Check is WARNING or
  INVALID, or tests regress
- Roll triage in Plan Mode: `exit_plan_mode` with
  `cd ~/.gemini/jetski/scratch/skia_gardener && go run . -triage-google3`
  (`Re-plan if:` verdict is `SKIA_REGRESSION` or a failing target cites Skia symbols)
- Outside Plan Mode, run orchestrators and read raw logs (`ninja.log`, `dm.log`,
  `*_unittests.log`, `shards/*.log`, `felt.log`, `flutter.log`) in
  `invoke_subagent(TypeName="self", Model="flash")`; it returns the `compare.md` path, exit status
  and failing assertions. Then read `compare.md` yourself and show the attestation table
- Never present a post-fix-only run as proof that a bug is fixed unless it failed before the fix
