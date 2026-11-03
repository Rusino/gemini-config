---
trigger: always_on
description: >-
  Reassign bot-filed Chrome [Merge Mxx] requests via safe_add_collaborator and
  safe_reassign_issue after one batch confirmation
---

# Reassigning Chrome Security Merge Requests to `merges@chromium.org`

Chrome's release bot (`chrome-release-blintz-continuous-runner@prod.google.com`) files `[Merge Mxx]`
child issues in component 1579812 (`Programs>PMO>Browser>Release>Merge Request`) with custom field
1223087 (`Merge`) set to `Pending`. Calling `issues mutate update safe-reassign` (or
`issues mutate bulk-update --assignee`, which invokes the same `SafeReassignIssue` RPC per issue)
after `safe-cc` fails with `FAILED_PRECONDITION` (`removing the current assignee could cause them
to lose access`), because `SafeReassignIssue` checks `Collaborators` (or `Verifier`), not `CC`.

The user gave standing authorization (2026-11-03) to execute the sanctioned Buganizer
collaborator-then-reassign write protocol **after one explicit user confirmation per batch**
(`ask_question` in chat or the Flight Deck confirm button; all Buganizer mutations are write
actions and never run autonomously):

- **Strict scope**: Only issues where `Reporter` is
  `chrome-release-blintz-continuous-runner@prod.google.com`, `Component ID` is 1579812, `Title`
  starts with `[Merge M<NN>]`, `Parent Issue ID` is `jlavrova`'s bug, `Merge` (field 1223087) is
  `Pending`, and `Assignee` is `jlavrova@google.com` or `jlavrova@chromium.org`.
- **Mandatory pre-gates**: All 6 merge gates must pass first (`Fixed By Code Changes` (field
  1358989) on the parent bug is non-empty; every other CL on the parent touches only `tests/`;
  >= 24h Canary soak; no revert on `main`; clean trial `git cherry-pick -x` onto `chrome/m<NN>`;
  `OS` (field 1223084) checked against the parent).
- **Sequential write & verification order**: Run `python3 ~/.gemini/config/scripts/bug_actions.py reassign_merge <ID>`
- **Fallback (never bypass `SafeReassignIssue`)**: If `safe_reassign_issue` fails for any issue IDs
  (for example, if `SafeReassignIssue` rejects `merges@chromium.org` on edit-access validation),
  report the exact error and open/return a **single** Buganizer bulk-query URL
  `https://issues.chromium.org/issues?q=id:(<ID1>%20OR%20<ID2>...)` so the user reassigns all
  remaining issues in one Web UI bulk edit instead of N separate tabs.
