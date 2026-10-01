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
  - **Never hardcode `ANTIGRAVITY_*` variables to literal values** yourself. However, you MUST preserve any `ANTIGRAVITY_PROJECT_ID="..."` injections provided by system hooks (e.g. `context_guard.py`), and ALWAYS include `env -u ANTIGRAVITY_SOURCE_METADATA`.
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
  - **ANTI-PATTERN WARNING**: Never call `agentapi new-conversation` inside `$()` *to extract the ID directly*, as you might accidentally re-execute the command (e.g. `cmd && verify $(cmd | grep)` creates TWO chats). Always capture the full JSON output into a variable (`OUT=$(cmd)`) exactly once, and extract the ID from the variable (`$OUT`).
- **On failed verification**:
  - Stop. Do **not** blindly re-create chats (this produces hidden duplicates that cannot be deleted via `agentapi`).
  - Report the problem and the affected conversation IDs to the user, then fix the creation method first.
- **Before bulk creation**, create one chat, verify it, and only then create the rest.

## Conversation Titles (`--title`) & Lifecycle Markers

- **No Project Prefix**: Never include the project name.
- **Style**: Direct and specific (strictly under 50 chars), plain text only (no markdown `**` or HTML tags, no Unicode math fonts).
- **Time Prefix**: Every continuation or tracked step starts with local time: `[HH:MM]`.

### Lifecycle Markers (Scheme Γ)

1. **In-Progress Investigation (active branch)**:
   - **First chat (start of branch)**: `[HH:MM] ▸ <Topic>` (or starts simply as topic/timestamped).
   - **Closed intermediate step**: `[HH:MM] ✓ <Topic>` (completed previous turn/step while work continues).
   - **Current active chat (in progress)**: `[HH:MM] ⦿ <Topic>` (attracts attention, bold focal point).
   - **Helper**: `python3 ~/.gemini/config/hooks/chat_lifecycle.py advance <new_conversation_id>` (automatically discovers parent chats via transcripts, setting root to `▸`, intermediates to `✓`, and active to `⦿`).

2. **Finalized / Closed Investigation (when completed / user says "финал")**:
   - **First chat (closed start)**: `[HH:MM] « <Topic>` (symmetric start bracket).
   - **Intermediate chats (archived steps)**: `[HH:MM] ‹✓› <Topic>` (completed intermediate archive steps).
   - **Last chat (closed final)**: `[HH:MM] » <Topic>` (symmetric end bracket).
   - **Single chat task (closed without continuations)**: `[HH:MM] «» <Topic>`.
   - **Helper**: `python3 ~/.gemini/config/hooks/chat_lifecycle.py finalize <conversation_id>` (automatically discovers chain ancestors and updates markers).

3. **Reopening Finalized Chats (возобновление работы)**:
   - Если работа возобновляется в ранее закрытом чате (`«»` или `»`), **по умолчанию откатывать статус в активный рабочий (`⦿`)**:
     - Одиночный чат: `[HH:MM] «» <Topic>` → `[HH:MM] ⦿ <Topic>`.
     - Последний или промежуточный шаг в цепочке: вернуть в `[HH:MM] ⦿ <Topic>`.
   - **Исключение**: не менять статус только если пользователь явно указал, что это просто вопрос/справка и менять статус не нужно (например, *"это просто вопрос"*, *"не меняй статус"*).
   - При повторном завершении работы процедура стандартная: спросить пользователя или закрыть по слову *"финал"*.

4. **Heuristics & Triggering Finalization**:
   - If the task is heuristically complete (all tests pass, bug investigated, final summary provided), ask the user: *"Пометить разбор как финальный?"*.
   - When the user confirms or explicitly says *"финал"*, *"готово"* or similar, update the titles across the chain or current conversation in the DB/title metadata.
