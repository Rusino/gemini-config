---
trigger: always_on
description: When closing a crbug issue, fill "Fixed By Code Changes" with only the fixing CLs
---

# Closing Bugs: Always Fill "Fixed By Code Changes"

Chrome Security bots (`chrome-security-blintz-continuous-runner@prod.google.com`) reopen a Fixed
bug when the "Code changes" list holds more than one CL and custom field `1358989`
"Fixed By Code Changes" is empty. Incident: crbug/576664769 was reopened on 2026-11-02 because it
listed both the fix (Gerrit 1404196) and a test-only follow-up (Gerrit 1411716).

The user gave standing authorization (2026-11-02): whenever you propose to close a bug (Flight
Deck "All linked CLs merged — ready to mark Fixed?", `[BUG-WATCHER]` prompts, or your own
suggestion), the proposal includes the "Fixed By Code Changes" value. When the user agrees to
close, set the field and the status together, without asking separately.

- **Value**: only CLs needed to consider the bug fixed. Leave out tests-only, logging, cleanup,
  and bot-fix follow-ups. List every CL if the fix needs several; the multi-value input format of
  `set_issue_custom_field` is not verified yet, so read the field back after setting it.
- **Third-party repos (Skia, Dawn, v8)**: list the original commit on the repo HEAD as a Gerrit URL
  (`https://skia-review.googlesource.com/c/skia/+/<N>`), never the Chromium roll or cherry-picks.
  Check the roll landed: Chromium `DEPS` `skia_revision` must contain the fix commit.
- **No Gerrit change rolls into Chromium**: value `NA`.
- **Scope**: field lives under component `1363614` (Chromium), so it applies to crbug issues,
  mainly Issue Type `VULNERABILITY`. Skip it on bugs where the field does not exist.
- **How** (Buganizer MCP over LOAS, read back to confirm):
  ```python
  import sys; sys.path.insert(0, "/usr/local/google/home/jlavrova/.gemini/config/scripts")
  from cl_watcher import call_buganizer_mcp_batch as call
  print(call([
      ("set_issue_custom_field", {"issueId": "<ID>", "customFieldId": "1358989",
                                  "customFieldValue": "<gerrit url>"}),
      ("update_issue_status", {"issueId": "<ID>", "status": "FIXED"}),
      ("get_bugs_with_external", {"query": "id:<ID>", "maxResults": 1, "verbose": True}),
  ]))
  ```
