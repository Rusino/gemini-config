---
description: 12 fundamental invariants for text editing, layout, font metrics, BiDi, and caret geometry.
trigger: model_decision
---
# Text Domain Invariants (Dungeons 12 Invariants)

1. **Dual-contract Typography:** Text layout respects both the physical boundaries (pixels/points) and logical boundaries (characters/words).
2. **Fallback:** Robust font fallback mechanisms must be in place and tested. Never rely on a single font covering all glyphs.
3. **Cluster Atomicity:** Grapheme clusters are atomic. Cursor movement, selection, and deletion MUST operate on whole clusters, not individual code points.
4. **Soft-wrap Singularity:** Wrapping points are singular decisions in a layout pass and must consistently recalculate downstream offsets.
5. **Continuous Physical Selection:** Selection geometries must form a continuous physical shape without gaps across lines.
6. **Reverse Topological Mutation:** Mutations that alter document structure (e.g. deletions spanning lines) must process from bottom to top (or end to start) to preserve early offsets.
7. **BiDi Inversion:** Bidirectional text flow requires strict adherence to logical vs visual index mappings.
8. **Metrics vs Ink:** Bounding boxes must distinguish between logical metrics (advances) and physical ink (overhangs).
9. **Zero-delta Phantom Law:** Invisible characters or zero-width joiners must not affect cursor positioning distances unless part of a valid cluster.
10. **Input Hygiene:** All text input is untrusted and must be sanitized (e.g. standardizing line endings to `\n`, stripping invalid UTF-8) at the boundary.
11. **Presentation Purity:** Text models contain pure data. Presentation attributes (color, font weight) belong strictly to the presentation layer, not the text buffer.
12. **Reversible Editing:** Every mutation operation MUST be fully reversible (support strict Undo/Redo without loss of fidelity).
