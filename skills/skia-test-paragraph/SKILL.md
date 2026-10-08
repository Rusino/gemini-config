---
name: skia-test-paragraph
description: >-
  Run native Skia SkParagraph tests via `dm`, generate per-ref Run Attestation (`attestation.json`),
  and produce comparative Markdown reports with an automated Integrity Check across branches,
  commits, or working tree modifications. Trigger when the user asks to "прогони тесты Skia",
  "протестируй skparagraph", "сравни ветки в Skia", "запусти dm для параграфа", or
  "проверь тесты параграфа в Скии".
---

# Skia SkParagraph Native Testing (`skia-test-paragraph`)

This skill orchestrates running Skia's `dm` test runner for `SkParagraph` tests across one or more git refs, branches, or working tree modifications. It records a tamper-evident **Паспорт прогона (Run Attestation)** (`attestation.json`) per ref, extracts proof of test completeness, and automatically generates comparative Markdown reports (`compare.md`) with an **Integrity Check (Вердикт целостности)**.

## Key Features

1. **Tamper-Evident Run Attestation (`attestation.json`)**:
   - Git ref, 12-char commit SHA, commit subject, and list of dirty files.
   - Pre-build SHA-256 hash of all source files in monitored directories (`modules/skparagraph/`, `modules/skshaper/`, `modules/skunicode/`, `src/`, `tests/`).
   - Build directory path and `args.gn` SHA-256 hash.
   - Ninja return code, action counter (`[N/M]` or `0/0 (no-op)`), and build duration.
   - Test binary path, post-build SHA-256 hash, and mtime.
   - Exact invocation command line and flags (`--src`, `--match`, `--config`, `--nogpu`).
   - Process exit code, timeout flag, and signal termination decoding (`SIGSEGV`, `SIGABRT`, etc.).

2. **Proof of Completeness (`finished_cleanly`)**:
   - Validates that the runner reached final task completion rather than aborting prematurely.
   - Catches mid-run crashes, unhandled `SkASSERT` failures, and truncated logs.

3. **Automated Integrity Check (Вердикт целостности)**:
   Emits `WARNING / INVALID` with specific diagnostic details if any invariant is broken:
   - Abnormal test binary termination (crash by signal, `SkASSERT`, timeout, or unclosed log) or failed ninja compilation.
   - Zero executed tests or drop in executed test count relative to baseline.
   - Identical source tree hashes across differing refs (testing identical code under different names).
   - Differing source tree hashes with identical binary hashes (binary was not rebuilt or built into the wrong output directory).
   - Mismatched run filters (`--match` or `--config`) between baseline and candidate branches.

---

## Scripts

- Orchestrator: [`run_skparagraph_tests.py`](file:///Users/jlavrova/.gemini/config/skills/skia-test-paragraph/scripts/run_skparagraph_tests.py)
- Log parser & comparator: [`parse_dm_logs.py`](file:///Users/jlavrova/.gemini/config/skills/skia-test-paragraph/scripts/parse_dm_logs.py)

---

## Usage

### 1. Compare working tree changes against baseline
```bash
python3 ~/.gemini/config/skills/skia-test-paragraph/scripts/run_skparagraph_tests.py \
  --out /tmp/skia_paragraph_tests \
  baseline=origin/main \
  current=WORKTREE
```

### 2. Compare two branches or commits
```bash
python3 ~/.gemini/config/skills/skia-test-paragraph/scripts/run_skparagraph_tests.py \
  --out /tmp/skia_paragraph_tests \
  baseline=origin/main \
  candidate=my_feature_branch
```

### 3. Run on current working tree only
```bash
python3 ~/.gemini/config/skills/skia-test-paragraph/scripts/run_skparagraph_tests.py \
  --out /tmp/skia_paragraph_tests
```

### Useful Flags
- `--build-dir <dir>` — Build directory relative to Skia repo (default: `out/Debug`).
- `--match <pattern>` — Substring filter passed to `dm` (default: `SkParagraph`).
- `--config <config>` — Rendering config passed to `dm` (e.g. `8888`, `gl`).
- `--nogpu` — Pass `--nogpu` to `dm`.
- `--extra-dm-flags ...` — Forward additional flags directly to `dm`.
- `--skip-build` — Skip running `ninja` prior to test execution.
- `--resume` — Skip execution for any ref whose `attestation.json` and `dm_summary.json` already exist.

---

## Mandatory Reporting Rule

Whenever presenting test results to the user, the agent **must** include both:
1. The primary test outcome table.
2. The complete **Паспорт прогона (Run Attestation)** section including the **Вердикт целостности (Integrity Check)** banner and all table columns without truncation.

If the integrity verdict is `WARNING / INVALID`, do not describe the test run as passing clean; report the exact invariant violations prominently.
