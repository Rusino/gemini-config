---
trigger: always_on
description: "Ensure conversations created via `agentapi new-conversation` are user-visible top-level chats (not hidden tool-sourced ones), and verify it."
---

# Creating User-Visible Chats via `agentapi`

Background: when `agentapi new-conversation` is run from an agent's `run_command`, it inherits the `ANTIGRAVITY_SOURCE_METADATA` env var. The server then records the new chat with `sourceMetadata.tool` set, and such chats do **not** appear in the user's sidebar (observed behavior, undocumented; it may change).

- **Unset the variable for the child process**:
  - Shell: `env -u ANTIGRAVITY_SOURCE_METADATA agentapi new-conversation --title="..." "..."`
  - Python: pass `env={k: v for k, v in os.environ.items() if k != "ANTIGRAVITY_SOURCE_METADATA"}` to `subprocess.run`.
  - Do **not** strip `ANTIGRAVITY_PROJECT_ID` or other `ANTIGRAVITY_*` variables.
- **Verify every created chat immediately**:
  - Run `agentapi get-conversation-metadata <id>` and check that `"sourceMetadata": null`.
  - Pre-tool hooks may silently rewrite commands, so never assume the unset was applied — check the metadata.
- **On failed verification**:
  - Stop. Do **not** blindly re-create chats (this produces hidden duplicates that cannot be deleted via `agentapi`).
  - Report the problem and the affected conversation IDs to the user, then fix the creation method first.
- **Before bulk creation**, create one chat, verify it, and only then create the rest.
