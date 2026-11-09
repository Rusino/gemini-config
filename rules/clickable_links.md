---
trigger: always_on
description: "Every CL, PR, bug, comment and CI build mention in chat must be a clickable link."
---

# Clickable Links

In every reply, format every mentioned Gerrit CL, patchset (`PS<N>`), Gerrit comment or draft,
GitHub PR or issue, Buganizer or crbug issue, Piper CL, or try job / CI build as a
`[label](https://...)` link at least once in the reply and in every table row or list item that
mentions it (`stop_guard` blocks unlinked entities; subsequent prose paragraphs may refer to an
already-linked entity by name, except comment/draft IDs which must always be linked):

- Gerrit: `[Skia CL 1414016 PS1](https://skia-review.googlesource.com/c/skia/+/1414016/1)`,
  `[Chromium CL 8663938](https://chromium-review.googlesource.com/c/chromium/src/+/8663938)`.
  Never write `cl/<N>` for a 5–8 digit Gerrit change anywhere in the reply: `cl/<N>` opens a
  Piper CL (`[cl/1008075988](http://cl/1008075988)` only for 9–10 digit Piper CLs)
- Comments: `https://<host>/c/<project>/+/<CL>/comment/<id>/`; unpublished drafts link to the
  patchset. Never write a bare `xxxxxxxx_xxxxxxxx` ID anywhere in the reply
- `[PR #194980](https://github.com/flutter/flutter/pull/194980)`,
  `[b/580852205](https://b.corp.google.com/issues/580852205)`,
  `[crbug/576664769](https://issues.chromium.org/issues/576664769)`
- Builds: `[Canary-G3](https://ci.chromium.org/b/<id>)` or the CL `?tab=checks` URL
- This overrides the Jetski "write internal shortlinks bare" instruction
