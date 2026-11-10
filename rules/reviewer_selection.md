---
trigger: model_decision
description: "When selecting, suggesting, or assigning any @google.com LDAP or person as a CL/PR reviewer, bug owner, assignee, or CC."
---

# Reviewer Selection

Before proposing any `@google.com` LDAP as reviewer, owner, assignee or CC, check
`id -u <ldap> >/dev/null 2>&1` (exit 0 = active) and recent activity via `colleague-activity`;
never rely on `git log` / `git blame` alone. The hooks keep the list of departed members.
