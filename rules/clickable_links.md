---
trigger: always_on
description: Every mention of a Gerrit CL, patchset, comment/draft, GitHub PR/issue, bug, or try job in chat replies must be a clickable Markdown link.
---

# Clickable Links in Every Chat Reply

This short rule stays inline even when `communication_style.md` exceeds the rules token budget
(incident 2026-11-02: the agent wrote `"Я положила реплай в черновики CL на PS1 (77640dee_0908b885)"`
with zero links).

In **every** reply to the user, **every** mention of a Gerrit CL, patchset (`PS<N>`), Gerrit
comment or draft, GitHub PR or issue, Buganizer (`b/<N>`) or `crbug` issue, Piper CL, or try job /
CI build must be a clickable Markdown link `[label](https://...)`:

- **Gerrit CL & patchset**: `[Gerrit 1414016 PS1](https://skia-review.googlesource.com/c/skia/+/1414016/1)`,
  `[crrev/c/8663938 PS2](https://chromium-review.googlesource.com/c/chromium/src/+/8663938/2)`.
  Never write `cl/<N>` for a 5–8 digit Gerrit change (`cl/<N>` opens a Google3 Piper CL; use
  `[cl/1008075988](http://cl/1008075988)` only for 9–10 digit Piper CLs).
- **Gerrit comments & drafts**: for published comments, link to
  `https://<host>/c/<project>/+/<CL>/comment/<id>/`; for unpublished drafts (which have no public
  `/comment/<id>` URL until published), link to the patchset
  `[черновик на Gerrit 1414016 PS1](https://skia-review.googlesource.com/c/skia/+/1414016/1)`.
  Bare comment/draft hex IDs (`[0-9a-f]{8}_[0-9a-f]{8}`) without a URL are forbidden.
- **GitHub PRs & issues**: `[PR #194980](https://github.com/flutter/flutter/pull/194980)`,
  `[flutter#155062](https://github.com/flutter/flutter/issues/155062)`.
- **Bugs**: `[b/580852205](https://b.corp.google.com/issues/580852205)`,
  `[crbug/576664769](https://issues.chromium.org/issues/576664769)`.
- **Try jobs & builds**: link bot and check mentions to their build, task, or `?tab=checks` URL
  (e.g. `[Canary-G3](https://ci.chromium.org/b/<build_id>)`,
  `[Test-Ubuntu24.04-...](https://task-scheduler.skia.org/task/<id>)`, or
  `[Canary-G3 (checks)](https://skia-review.googlesource.com/c/skia/+/1414016/1?tab=checks)`).

Link the entity in every paragraph or list item that mentions it (`hooks/stop_guard.py` blocks
turn completion on bare `Gerrit <N>`, `CL <N>`, `PS<N>`, `PR #<N>`, `b/<N>`, or `xxxxxxxx_xxxxxxxx`
mentions without a matching URL nearby).
