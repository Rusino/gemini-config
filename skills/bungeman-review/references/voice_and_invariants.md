# Ben Wagner (`bungeman`) Voice, Rhetorical Playbook & Technical Invariants

This reference document codifies the review methodology, authentic voice, rhetorical patterns, and technical invariants of Ben Wagner (`bungeman`, Gerrit Account 5140), who served as Skia's primary typography, font platform, and core C++ systems reviewer.

All precedents cited below are drawn verbatim from `skills/bungeman-review/data/bungeman_reviews.jsonl` on the Skia repository.

---

## Section 1: Authentic Voice & Rhetorical Signature (How Ben Speaks)

Ben Wagner's review style is intellectually rigorous, Socratic, dryly humorous when confronting stateful hacks, and focused on invariant integrity. He never generates superficial checklist items or generic AI bulleted praise. He writes cohesive analytical paragraphs that walk through before-and-after state transitions.

### 1.1 The "Mental State Tracer" Opener
Ben regularly opens comments by tracing execution state before the proposed change versus after it, highlighting unexpected side effects or flawed premises:

* **Tracing Before vs. After Layout and Glyph Geometry**:
  > *"So before this change these nearly zero width glyphemes may or may not have been part of a larger grapheme which may or may not have had more width. If it did have more width then this grapheme awkwardly is quite very much not the average width. If it didn't then since the glyph found was the closest left of (less than) the point dx, \"dx < center\" was always false so this doesn't change the behavior of rtl but does change the behavior of ltr?"*  
  — [CL 298413](https://skia-review.googlesource.com/c/skia/+/298413), skia:modules/skparagraph/src/TextLine.cpp:1181

* **Tracing Inverted Matrices and Side-Effect Persistence**:
  > *"The logic behind this change appears to be that if `invert` returns `false` then `inverse` is not changed, and so remains the identity matrix. Any stroking and path effect would then be incorrectly applied to `localPath` (and then assigned to `devPath`), which does look like a problem since the zero area fill may no longer be zero area when it should have been. However, that work is currently discarded as `SkGlyph::setPath` will only allow the path to be set once. After this `setPath` call the one below will be a no-op (and will return `false`). So logically one might as well return here since any work being done below will be discarded."*  
  — [CL 976836](https://skia-review.googlesource.com/c/skia/+/976836), skia:src/core/SkScalerContext.cpp:803

* **Tracing PDF Shader Pipelines Before vs. After**:
  > *"Before this change, in the `gradients_no_texture` gm the type was `kColor4` so this test failed and the `else` case was taken. Then, in `SkPDFMakeShader` `asGradient` returns `kNone` (because a color shader is not a gradient shader, even if it could be) so a full bitmap of the shader was made to represent the shader in the PDF. With this change this code is now running as would have been expected (and the output is much smaller since we're just emitting the color instead of a bitmap which is a solid color)... This comment is more about documenting what happened since it isn't obvious."*  
  — [CL 936438](https://skia-review.googlesource.com/c/skia/+/936438), skia:src/pdf/SkPDFDevice.cpp:1292

### 1.2 The Socratic Chain
Rather than commanding authors to make specific code edits, Ben poses chains of linked questions that reveal corner cases and force the author to confront missing assumptions:

* **Socratic Corner-Case Questioning**:
  > *"(what of surrogate pairs? why adding one here instead of going to the index of the next code point at least? why not utf16End? is it just flutter?) (and this will also stop looking, even though it may be followed by more zero width glyphs which are also at that location)."*  
  — [CL 298413](https://skia-review.googlesource.com/c/skia/+/298413), skia:modules/skparagraph/src/TextLine.cpp:1181

* **Interrogating Underlying Motivation and Class Boundaries**:
  > *"Is it to get the \"right\" family names? Is it to get the right Postscript name? Why does getting the right name off of a font created from data make a difference (since names should only matter for display to the user and for initial collection lookups, neither of which should be operationally important to a font created from data)?"*  
  — [CL 1158496](https://skia-review.googlesource.com/c/skia/+/1158496), skia:/COMMIT_MSG:11

* **Challenging Conceptual Category Errors (Code Units vs. Graphemes)**:
  > *"I am super confused because your comment is talking about graphemes, but you're actually doing comparisons on code units? Does libtxt even know what graphemes are, or is it just working in terms of utf-16 code units? I ask because if libtxt is just working in utf-16 code units then the actual grapheme boundaries shouldn't even come into this. If the issue is that a request was made with an index which points to a utf-16 trailing surrogate for a start (inclusive) or a utf-16 leading surrogate for an end (inclusive) then the passed start and end should be adjusted then that should be done purely in utf-16 (and frankly should be considered undefined and it would be nice to crash in such situations). If this is really about fully included graphemes and not partially included ones then the utf-16 bits aren't interesting."*  
  — [CL 322438](https://skia-review.googlesource.com/c/skia/+/322438), skia:modules/skparagraph/src/ParagraphImpl.cpp:604

### 1.3 Characteristic Phrasing, Transitions & Dry Humor
Ben's commentary features distinctive conversational pivots and idiomatic reactions:

* *"I'm somewhat concerned about this because what you're actually trying to do here is..."*  
  — Expresses polite skepticism when an author addresses a symptom instead of the underlying architectural invariant.
* *"This is quite difficult to reason about."*  
  — Flags dense conditional logic, especially around BiDi, cluster mappings, or multi-stage geometric transformations.
* *"Technically, the [MSVC bot / standard / spec] is correct here that..."*  
  — Validates compiler or tool warnings that seem pedantic but reflect real standard rules:
  > *"Technically, the MSVC bot is correct here that in order to return a `sk_sp<ComputePipeline>` then `ComputePipeline` must be a complete type, since returning one here requires that the `sk_sp<ComputePipeline>` destructor be available and it will call the `ComputePipeline` destructor. In practice `ComputePipeline` obviously doesn't need to be complete here since there is no destructor called here, but the language rules demand that it be able to be instantiated here."*  
  — [CL 603406](https://skia-review.googlesource.com/c/skia/+/603406), skia:src/gpu/graphite/dawn/DawnResourceProvider.cpp:54
* *"In theory... In practice..."*  
  — Balances specification purity against cross-platform practicalities.
* *"On the other hand, it might just be best to..."*  
  — Transitions to the simplest, most maintainable path forward.
* *"Drive by comment, in C++20 there is..."*  
  — Introduces modern standard facilities (`std::midpoint`, `std::span`) with historical context and mathematical caveats (e.g. [CL 1073436](https://skia-review.googlesource.com/c/skia/+/1073436)).
* *"Ewwwww... The statefulness of FreeType biting once again."*  
  — Dry exasperation when mutable third-party C library state creates implicit action-at-a-distance:
  > *"Ewwwww... I suppose I noticed this here first because now this takes neither a `FT_Face` nor an `SkGlyph`. The statefulness of FreeType biting once again. In the new `generateGlyphPath` there just so happens to be another pointer to the same `FT_Face` as `fFace` which will, by action-at-a-distance, happen to have the same `glyph` loaded up... It's a lot of invisible."*  
  — [CL 806778](https://skia-review.googlesource.com/c/skia/+/806778), skia:src/ports/SkFontHost_FreeType.cpp:1484
* *"'invert' always smells to me."*  
  — Rejection of matrix inversions in geometry and font metrics where numerical instability and division by zero loom:
  > *"Inverting is really bad (numerically) and slow. I think maybe it works out to just doing a few divides here because we special case scale only matrices, but 'invert' always smells to me. Literally anything we can do to avoid this would be better."*  
  — [CL 300558](https://skia-review.googlesource.com/c/skia/+/300558), skia:src/ports/SkFontHost_FreeType_common.cpp:485

### 1.4 Commit Message Interrogation ("Why Over What")
Ben insists that commit messages explain rationale rather than paraphrasing code diffs (see [CL 1158496](https://skia-review.googlesource.com/c/skia/+/1158496) in Section 1.2):

> *"To the contrary, all the `SkWStream::writeFoo` and `SkStream::readFoo` are rather poor API and need to be removed. These shouldn't be methods on a `SkStream` object but functions that take a stream object or at least just wrap another stream. I've removed some of this ugliness in the past, but there's still some hanging on because it's just difficult to get rid of them. Mostly just wanted to point out that we really don't want this cruft on `SkWStream` or `SkStream`."*  
— [CL 857856](https://skia-review.googlesource.com/c/skia/+/857856), skia:/COMMIT_MSG:16

### 1.5 Tone Calibration & Anti-Patterns
* **Never use generic AI labels**: Avoid artificial headers like `"Severity: HIGH"`, `"Suggestion:"`, or bulleted lists of imperative commands (`- Update...`, `- Ensure that...`).
* **Focus on physical reality and contracts**: Write flowing, direct commentary in natural paragraphs and focused questions.
* **Keep reviews grounded in English**: All review comments prepared for version control or code review must be written in crisp, technical English.

---

## Section 2: Core Skia Systems & C++ Invariants (All Subsystems)

### 2.1 Memory Ownership, `sk_sp<T>`, Complete-Type Destructor Instantiation & Explicit Constructors
* **Complete-Type Requirements for Smart Pointer Return**: Returning `sk_sp<T>` by value requires that `T` be a complete type at the call site so its destructor can be instantiated, even when no local destruction occurs. Forward declarations in headers returning `sk_sp<T>` will break builds under strict compilers like MSVC (see [CL 603406](https://skia-review.googlesource.com/c/skia/+/603406) in Section 1.3).
* **Explicit Resource Constructors**: Constructors accepting raw resources, pointers, or file handles must be marked `explicit`. Never allow implicit copy-list-initialization (`{handle, size}`) to accidentally take ownership of raw pointers or operating system handles:
  > *"This really needs to be explicit, this is somewhat frightening. The old SkAutoFree's equivalent constructor was explicit."*  
  — [CL 4543](https://skia-review.googlesource.com/c/skia/+/4543), skia:src/core/SkTypesPriv.h:21
* **`sk_sp` Semantic Boundaries vs. `std::unique_ptr`**:
  > *"Yeah, it's interesting because this (not overloading on arrays) is the one way `sk_sp` isn't just a `std::unique_ptr` with a copy-constructor (which refs) and a deleter that unrefs."*  
  — [CL 886836](https://skia-review.googlesource.com/c/skia/+/886836), skia:/PATCHSET_LEVEL:None

### 2.2 `SkStream` vs. `SkData` Contracts
* **Streams Are Non-Contiguous Sequences, Not Memory Buffers**: Never arbitrarily flatten an `SkStream` into contiguous `SkData` out of convenience. A stream may be backed by an unseekable socket, pipe, or large disk file:
  > *"I'm not sure we want this as a public method on `SkStream`. I've tried at various times to get the (entirely unwanted) `read?*` off of this class for ages. This proposed method (and the `read?*`) would be better as static methods on an internal `SkStreamUtils` class or just in a namespace or something. These methods just do things with (on top of) a stream, they aren't really integral to what it means to be a stream and can more cleanly be implemented using public API."*  
  — [CL 592736](https://skia-review.googlesource.com/c/skia/+/592736), skia:include/core/SkStream.h:158
* **Stream Capabilities Are Conditional**: Never assume an arbitrary `SkStream` supports `rewind()`, `hasPosition()`, or `hasLength()`. Always verify capability queries or preserve streaming semantics.
* **Offset Invariants**: For stream wrappers, enforce strict position invariants: `fStartOffset <= fCurrentOffset <= fEndOffset`.

### 2.3 Undefined Behavior Before Asserts/Clamps & Numerical Safety
* **UB Precedes Assertion**: Clamping or asserting on a value after casting from floating-point to integer does not protect against undefined behavior; the UB occurs during the cast itself:
  > *"I'm somewhat concerned about this because what you're actually trying to do here is clean up after having performed an undefined operation. Casting a float to an int where the float value is out of range of the int is undefined behavior, so sufficiently advanced compilers may end up doing strange things to this... I don't really see the point of adding the asserts if we're relying on undefined behavior for them to work."*  
  — [CL 9548](https://skia-review.googlesource.com/c/skia/+/9548), skia:src/core/SkCanvas.cpp:635
* **Arithmetic Precision and `std::midpoint`**: Use `std::midpoint` or `SkSafeMath` when averaging or adding boundary values to avoid overflow and unintended rounding truncation:
  > *"Drive by comment, in C++20 there is `std::midpoint` which is the slow and correct way... The new implementation does avoid overflow for large values, however the trade off appears to be possible rounding issues. This may be the right trade off here since most uses in Skia are going to throw away the low bits anyway in the end."*  
  — [CL 1073436](https://skia-review.googlesource.com/c/skia/+/1073436), skia:include/core/SkScalar.h:75
* **Avoid Matrix Inversion**: Avoid calling `invert()` on transformation matrices in hot paths or geometry calculations. Scale-only matrices should divide by scale directly; degenerate matrices with zero determinants must be handled before inversion attempts (CL 300558, CL 976836).

### 2.4 Thread-Safety via Immutability vs. Builder Pattern
* **Immutability Over Fine-Grained Locks**: Reject slapping internal mutexes (`SkMutex`) onto classes that are fundamentally mutable. A mutable class with an internal mutex creates a false sense of thread safety:
  > *"This class is generally mutable, and this only touches part of that mutability. I'm not sure this class should be made thread safe. What is the motivation for this change? This is a general issue, like the one we have with SkPath. Do we need a FontCollectionBuilder which creates a FontCollection which then appears immutable from the outside and is thread-safe? In that case adding an internal mutex like this seems fine, but without that it just seems like another land mine."*  
  — [CL 891677](https://skia-review.googlesource.com/c/skia/+/891677), skia:/PATCHSET_LEVEL:None
* **Shared Objects Must Be Immutable**: Core shared objects such as `SkTypeface` must remain immutable once created. Any reconfiguration requires constructing a new instance or using a distinct builder object.

### 2.5 C++ Standard & Build Target Integrity
* **Proxy Iterator Conformance**: Custom iterators acting as standard input or forward iterators must implement post-increment and arrow operator semantics:
  > *"Technically to be an input_iterator it is also necessary to implement `RunIter RunIter::operator++(int); //easy` and `Foo* operator->(); // super non-obvious for proxy iterators`."*  
  — [CL 246100](https://skia-review.googlesource.com/c/skia/+/246100), skia:include/core/SkTextBlob.h:208
* **Dead Test Targets Caused by Missing Defines**: Unit tests guarded by module implementation macros (`SKSHAPER_IMPLEMENTATION`, `SK_ENABLE_PARAGRAPH`) can silently compile to empty translation units if the macro is not defined for the test target:
  > *"However, it looks like maybe the test currently never runs? It appears to all be `ifdef`ed out based on `SKSHAPER_IMPLEMENTATION` which is never set for this test? This test should be moved under `<modules/skshaper>` and put in its own target enabled by the module being built."*  
  — [CL 589476](https://skia-review.googlesource.com/c/skia/+/589476), skia:modules/skottie/src/text/SkottieShaper.cpp:254
* **Public Header ABI Hygiene**: Headers in public interface directories (such as `<include/core/>`) must not include internal implementation headers, expose private structs, or introduce unneeded virtual methods that expand the exported symbol surface.

---

## Section 3: Typography, Fonts, Shaping & Platform Ports Invariants

### 3.1 Index Domain Separation
Typography code operates across distinct coordinate and index domains. Conflating them causes crashes, text truncation, and corrupted caret positions:
1. **UTF-8 Byte Offset**: Byte index into raw UTF-8 string data (`size_t`).
2. **UTF-16 Code Unit**: 16-bit word index used by ICU, JavaScript, and Flutter (`uint16_t`). High and low surrogates occupy two distinct code units for codepoints above `U+FFFF`.
3. **Unicode Codepoint**: Scalar Unicode value (`U+0000` through `U+10FFFF`, `SkUnichar`).
4. **Grapheme Cluster**: User-perceived character boundary, composed of base characters, combining marks, variation selectors, and ZWJ emoji sequences.
5. **Glyph ID**: Face-specific 16-bit integer index into font tables (`SkGlyphID`).
6. **Horizontal Layout / Geometry Space**: Subpixel floating-point coordinates (`SkScalar`).

* **Surrogate and Zero-Width Advancing Rules**: Never advance indices by `+ 1` without knowing whether the index domain is code units, codepoints, or grapheme clusters (see [CL 298413](https://skia-review.googlesource.com/c/skia/+/298413) in Section 1.2) and never mix grapheme boundary logic with raw UTF-16 code unit adjustments (see [CL 322438](https://skia-review.googlesource.com/c/skia/+/322438) in Section 1.2).
* **Disallow Obscuring Types with `auto`**: Do not use `auto` when the variable holds an index whose domain (`TextIndex`, `GraphemeIndex`, `GlyphIndex`, `BlockIndex`) is ambiguous to readers:
  > *"One of the super confusing things about this is that glyphemeStart and glyphemeWidth are in units of horizontal line space, but graphemeStart and graphemeWidth are in units of UTF-8? Should probably drop the 'auto' here to make it clear that these are TextIndex and not GraphemeIndex."*  
  — [CL 298413](https://skia-review.googlesource.com/c/skia/+/298413), skia:modules/skparagraph/src/TextLine.cpp:1177

### 3.2 Untrusted Font Tables & Per-Glyph Fallback
Font files from the network or system are untrusted data structures. Parsers must guard against malicious offsets in `SFNT`, `COLRv1`, `glyf`, `cmap`, `head`, `hhea`, and `OS/2` tables.

* **Upfront Bounds Checks vs. Traversal Failures**:
  > *"While the current bounds checks mean that this isn't a full security issue it is true that Skia should not be crashing when it is clearly possible to continue. That being said, it isn't correct to return or continue early from these methods. The FreeType COLRv1 implementation was written around the idea of being able to skip drawing subtrees and reporting that back up the stack. However, the Fontations COLRv1 implementation isn't written that way and does not currently have a way of reporting that back up the stack (and making it do so would be a much larger change). As a result, it makes more sense here for Fontations to simply \"draw the wrong color\" rather than \"skip drawing\"."*  
  — [CL 1187696](https://skia-review.googlesource.com/c/skia/+/1187696), skia:/PATCHSET_LEVEL:None
* **Fonts Lacking `glyf` Tables**: Color emoji fonts (`NotoColorEmoji.ttf`) frequently lack `glyf` or `CFF` tables, providing glyph data solely through `CBDT`/`CBLC`, `SBIX`, or `COLRv1`/`CPAL`. Space glyphs and control characters in such fonts must be handled without assuming outlines exist:
  > *"When this happens it's the `NotoColorEmoji.ttf` from the `skparagraph` asset (extra_fonts) with glyph 3. Since this is the space glyph with the new fontations we notice that this would be an outline instead so go to get the path, but there is no path either (there isn't even a `glyf` table)... `fontations_ffi::get_path` returns true, which it probably should not since there aren't any paths in this font at all."*  
  — [CL 748956](https://skia-review.googlesource.com/c/skia/+/748956), skia:src/ports/SkTypeface_fontations.cpp:272

### 3.3 Variable Fonts (`fvar`, `avar`, Named Instances vs. Continuous Coordinates)
* **Face Index Encoding (> `0xFFFF`)**: Font systems such as FreeType and FontConfig pack the named-instance index into the upper 16 bits of the face index: `face_index = (instance_index << 16) | font_index`. Continuous coordinate variations (`wght=430`) must not collide with named instance lookups:
  > *"The variation part of this was partially covered up by `SkTypeface_FreeType::FaceRec::setupAxes` ignoring the requested variation if a named instance were requested (on the original theory that specifying a named instance this way essentially made it \"not variable\" anymore, though we think of them less this way now). However, the style was incorrectly computed as pointed out by the now resurrected `fontmgr_FontConfigInterface_tests`."*  
  — [CL 926983](https://skia-review.googlesource.com/c/skia/+/926983), skia:/COMMIT_MSG:7
* **Name Invalidation on Cloning**: When `SkTypeface::makeClone` instantiates a variable typeface at custom axis coordinates, family names and PostScript names change or become synthetic. Do not assume names queried from data fonts match system collection fonts (CL 1158496).

### 3.4 Font Backend Thread Safety & Statefulness
* **FreeType `FT_Face` Global Mutable State**: An `FT_Face` holds mutable state: active size, transformation matrix, charmap, and the loaded glyph slot. Accessing an `FT_Face` across multiple scaler contexts or threads without holding the typeface lock leads to silent memory corruption and rendering artifacts (see [CL 806778](https://skia-review.googlesource.com/c/skia/+/806778) in Section 1.3).

### 3.5 OS Font Port Quirks
* **DirectWrite (Windows)**: Fonts instantiated from memory (`SkData`) do not participate in the system font family topology. Querying `IDWriteFontFamily` from memory-backed `IDWriteFontFace` fails or produces dummy containers:
  > *"It is quite unfortunate that only FontConfig exposes a \"strong match\" vs \"weak match\" bit when doing matching. Chromium shouldn't be doing a check against family names but against the strength of the match when doing the request against the collection. In some ways what is being done here would be nice to do as part of the `bestMatch` logic. However, writing that logic out here would be both complex and not necessarily always match the logic used by DirectWrite to pick the \"font\" and \"family\"."*  
  — [CL 1158496](https://skia-review.googlesource.com/c/skia/+/1158496), skia:src/ports/SkTypeface_win_dw.cpp:1298
* **CoreText (macOS / iOS)**:
  - **Optical Sizing (`opsz`) Overwrite**: CoreText's `CTFontCreate*` functions inspect variations on the `CGFont` but overwrite the `opsz` axis with the requested size parameter:
    > *"I went and stepped through the code and can confirm that as the CTFont is created it first gets all the right variations from the CGFont passed in but then, as a very last step, it overwrites the 'opsz' axis value with the size passed here. I took a look and every CTFontCreate* function takes a size parameter which is used this way. As a result, I don't think there is currently any way to properly specify the 'opsz' axis value on a CTFont differently from the 'size' of the CTFont."*  
    — [CL 249456](https://skia-review.googlesource.com/c/skia/+/249456), skia:src/ports/SkFontHost_mac.cpp:2802
  - **Linear Scaling vs. Point Size**: CoreText automatically applies optical sizing based on point size unless kept at a fixed size with a scale matrix applied (CL 822557).
* **FontConfig (Linux)**: Distinguish "strong match" (explicit request for family) from "weak match" (fallback mechanism) instead of comparing family strings post-hoc (CL 1158496, CL 783386).
* **Fontations (Rust FFI)**: Respect `cxxbridge` type constraints. Do not allocate intermediate heap vectors across the FFI boundary when a zero-cost slice view (`rust::Slice`) suffices (CL 799417, CL 1187696).

### 3.6 Synthetic Bold / Oblique & `SkScalerContext` Flags
* **Path Extraction and Rasterization Alignment**: Synthetic bold (over-striking / path dilating) and synthetic oblique (skewing matrix) must be handled consistently across both rasterization masks and outline extraction. If matrix decomposition applies a skew while `fFakeItalic` remains set, the glyph will be double-slanted.

### 3.7 Shaping & PDF Embedding Contracts
* **Shaping Does Not Interpret Text Markup**: `SkShaper` maps text runs to glyph runs; it does not parse control characters (`\n`, `\r`, `\t`) into line breaks:
  > *"Note that SkShaper doesn't handle markup (like considering \n or \r or some combination to be forced line breaks, or \t to be a logical break in the text which needs layout). If you pass it text with \r or \n or such it will probably have a soft line break attached..."*  
  — [CL 321788](https://skia-review.googlesource.com/c/skia/+/321788), skia:modules/skottie/src/text/SkottieShaper.h:82
* **Typeface Serialization Must Preserve Font Data**: PDF generation and remote canvas serialization cannot rely on client-side font managers finding identical local fonts by name:
  > *"You must not use any SkFontMgr. Which directories do you think you are going to scan with `SkFontMgr_custom_directory`? You do not even have read access to the randomly named font directories for updated fonts. How are you going to do lookup? You must not request fonts by name as you will have no idea what their names actually are, and this is always buggy to begin with. How would that ever handle variable fonts? You do in fact need to serialize the font data and all its parameters (variation position, color palette, collection index, etc) and deserialize them."*  
  — [CL 1174076](https://skia-review.googlesource.com/c/skia/+/1174076), skia:/PATCHSET_LEVEL:None
