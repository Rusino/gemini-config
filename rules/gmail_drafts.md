---
trigger: model_decision
description: "When reading, drafting, replying to or sending email (Gmail drafts, messages with links)."
---

# Gmail & Drafts: Use `gmail` Go CLI for Clean Direct Links

Why: the `google_gmail` MCP rewrites URLs into expiring redirects (see `INCIDENTS.md`).

- **Use `gmail` Go CLI, keep `google_gmail` MCP disabled**: Always run
  `/google/bin/releases/gemini-agents-gmail/gmail` (`~/.gemini/config/skills/gmail/SKILL.md`),
  which calls `gmail.googleapis.com` directly over LOAS and preserves clean `https://...` URLs.
  Keep the `google_gmail` plugin disabled
  (`enabled: false`); leave `~/.gemini/jetski/mcp_oauth_tokens.json` on disk only for
  `scripts/cl_watcher.py`
- **Format bodies with `--html` (explicit `<a href>` and `<pre>`)**: Pass `--html` (using
  `--body-file` to a scratch file on `create-draft` / `send`, or `--body "$(cat ...)"` on
  `update-draft`) with explicit `<a href="https://...">...</a>` links and `<pre>` blocks for
  commands; avoid plain text when sending URLs, and avoid `--md` when fenced code follows a list
  item (`blackfriday` collapses unindented fences after `- ...` into inline `<code>` inside `<li>`).
  For replies, pass `--message <MSG_ID>` on `create-draft` / `reply` (adds `ThreadId`,
  `In-Reply-To`, `References`; add `--all --include-history` for reply-all with thread history)
- **Safe draft updates vs. open Compose / Undo Send**: Within the same turn before surfacing a
  draft to the user, `mutate update-draft <DRAFT_ID>` updates it cleanly in place. Once surfaced
  in any reply (including earlier handoff turns), treat the draft as handed over: never call
  `mutate delete-draft` unless the user explicitly asks to delete it (`delete-draft` bypasses Trash
  and permanently deletes a sent message still inside Undo Send, before it reaches `in:sent`), and
  if revising a handed-over draft that may be open in Compose, create a new `mutate create-draft`
  and ask the user to discard the old tab rather than racing with Compose autosave
- **Verify after creating**: Run `readonly get-draft <DRAFT_ID> --json` (never pipe `gmail` CLI
  output to `grep`, `egrep`, or `awk`) and decode the stored HTML body to confirm zero
  `google.com/url` redirects and intact `<a href>` / `<pre>` tags before notifying the user
