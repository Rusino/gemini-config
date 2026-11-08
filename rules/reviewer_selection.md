---
trigger: always_on
description: "Verify a reviewer, owner or assignee is still active before proposing them."
---

# Reviewer Selection

Before proposing any `@google.com` LDAP as reviewer, owner, assignee or CC, check
`id -u <ldap> >/dev/null 2>&1` (exit 0 = active) and recent activity via `colleague-activity`;
never rely on `git log` / `git blame` alone. The hooks keep the list of departed members.
