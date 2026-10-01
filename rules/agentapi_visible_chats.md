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
  - **Never set `ANTIGRAVITY_*` variables to literal values** (e.g. `ANTIGRAVITY_PROJECT_ID=...`). The only allowed change is `env -u ANTIGRAVITY_SOURCE_METADATA`; everything else is inherited as-is.
- **Verify every created chat immediately**:
  - Run `agentapi get-conversation-metadata <id>` and check that `"sourceMetadata": null`.
  - Pre-tool hooks may silently rewrite commands, so never assume the unset was applied — check the metadata.
  - **Create and verify in ONE command.** A pre-tool guard may block all further commands right after a continuation chat is created, so a separate verification step can be impossible:
    ```bash
    OUT=$(env -u ANTIGRAVITY_SOURCE_METADATA agentapi new-conversation --title="..." "...") && \
    ID=$(printf '%s' "$OUT" | grep -o '"conversationId": *"[^"]*"' | cut -d'"' -f4) && \
    echo "ID=$ID" && agentapi get-conversation-metadata "$ID" | grep -E '"sourceMetadata"'
    ```
    The output must show `"sourceMetadata": null`; otherwise report `$ID` to the user.
- **On failed verification**:
  - Stop. Do **not** blindly re-create chats (this produces hidden duplicates that cannot be deleted via `agentapi`).
  - Report the problem and the affected conversation IDs to the user, then fix the creation method first.
- **Before bulk creation**, create one chat, verify it, and only then create the rest.

## Conversation Titles (`--title`)

- **No Project Prefix**: Never include the project name.
- **Continuations**: Prefix only with time: `[HH:MM] <Original Topic>`. Do not add words like "continuation" or part numbers.
- **Style**: Direct and specific (under 50 chars), no filler prefixes (`Task:`, `Fix:`).
