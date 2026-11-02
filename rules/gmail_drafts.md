---
trigger: always_on
description: "Gmail drafts: recreate instead of update, HTML body with explicit links, verify links."
---

# Gmail Drafts: Recreate, Explicit Links, Verify

Incident (2026-11-02): three `update_draft` calls on one reply left the user seeing all three
versions in Gmail, and a plain-text `body` turned every URL into an expiring
`https://www.google.com/url?q=...&ust=...` redirect in the visible text (the 2026-10-30 letter
to David Bebawy went out like that).

- **Never edit a draft in place**: to change a draft, `delete_draft` the old one and
  `create_draft` a new one (same `replyToMessageId` for replies). Do not call `update_draft`
- **Write the body as `htmlBody`** with explicit `<a href="https://...">https://...</a>` links
  and commands in `<pre>`; do not rely on a plain `body` for anything that contains a URL
- **Verify after creating**: `get_draft` and check that the visible link text has no
  `google.com/url`
- **The MCP cannot make clean links**: every URL in a draft (`<a href>`, bare URL, `<pre>`,
  `<code>`, even without `https://`) is stored as an expiring `google.com/url?...&ust=...`
  redirect (verified 2026-11-02, the user saw the redirect in the Gmail UI). Before the user
  sends, tell them so and offer either to fix the links in Gmail compose by hand or to drop the
  URLs and keep plain IDs such as `Gerrit 1414296`. Do not fall back to the retired gmail CLI
