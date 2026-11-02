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
  `google.com/url`. The API may still show rewritten `href`s; in that case ask the user to hover
  the link in Gmail before sending
