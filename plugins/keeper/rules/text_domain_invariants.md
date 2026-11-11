---
description: 9 fundamental invariants for text layout, font fallback, grapheme clusters, BiDi, font metrics, and caret geometry.
trigger: model_decision
---
# Text Domain Invariants

1. **Dual-contract Typography:** Text layout respects both the physical boundaries (pixels/points)
   and logical boundaries (characters/words).
2. **Fallback:** Robust font fallback mechanisms must be in place and tested. Never rely on a single
   font covering all glyphs.
3. **Cluster Atomicity:** Grapheme clusters are atomic. Cursor movement, hit-testing, selection, and
   deletion MUST operate on whole clusters, not individual code points.
4. **Soft-wrap Singularity:** Wrapping points are singular decisions in a layout pass and must
   consistently recalculate downstream offsets.
5. **Continuous Physical Selection:** Selection geometries must form a continuous physical shape
   without gaps across lines.
6. **BiDi Inversion:** Bidirectional text flow requires strict adherence to logical vs visual index
   mappings.
7. **Metrics vs Ink:** Bounding boxes must distinguish between logical metrics (advances) and
   physical ink (overhangs).
8. **Zero-delta Phantom Law:** Invisible characters or zero-width joiners must not affect cursor
   positioning distances unless part of a valid cluster.
9. **Input Hygiene:** All text input is untrusted and must be sanitized (e.g. standardizing line
   endings to `\n`, stripping invalid UTF-8) at the boundary.
