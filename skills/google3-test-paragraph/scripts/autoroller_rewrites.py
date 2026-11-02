#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

# Mirrors chrome/skia/autoroller/autoroller_lib.py file filters and rewrite rules
G3_SKIA_REL = "third_party/skia/HEAD"

INCLUDE_PATTERNS = [
    r"^[^/]+$",
    r"^bazel/.*$",
    r"^include/.*$",
    r"^modules/.*$",
    r"^platform_tools/android/apps/jetski/.*$",
    r"^platform_tools/android/apps/skottie/.*$",
    r"^platform_tools/android/apps/viewer/.*$",
    r"^rust/.*$",
    r"^src/.*$",
]

EXCLUDE_PATTERNS = [
    r"(^|.*/)\.gitignore$",
    r"(^|.*/)Cargo.toml$",
    r"(^|.*/)OWNERS$",
    r"(^|.*/)README.google$",
    r"(^|.*/)METADATA$",
    r"(^|.*/)BUILD$",
    r"^google3/.*",
    r"^bazel/.+\.bzl",
    r"^bazel/exporter.+",
    r"^bazel/deps.+",
    r"^go_repositories.bzl",
    r"^bazel/external.+",
    r"^bazel/karma.+",
    r"^bazel/rbe/.+",
    r"^bazel/utils/.+",
    r"^example/external_client/.+",
    r"^modules/canvaskit/.*$",
    r"^modules/pathkit/.*$",
    r"^bazel/BUILD.bazel$",
    r"^bazel/device_specific_configs/.*",
    r"^include/third_party/.*",
    r"(^|.*/)tools\.go$",
    r"(^|.*/)go\.mod$",
    r"(^|.*/)go\.sum$",
    r"(^|.*/)MODULE\.bazel$",
    r"(^|.*/)MODULE\.bazel\.lock$",
    r"(^|.*/)WORKSPACE.bazel$",
    r"(^|.*/).bazelrc$",
    r"(^|.*/).bazelversion$",
    r"^rust/bmp/.*$",
]

REWRITE_BAZEL_EXCLUDE_PATTERNS = [
    r"^bazel/exporter.+",
    r"^bazel/external/.+",
    r"^bazel/platform/.+",
    r"^bazel/rbe/.+",
    r"^BUILD.bazel",
    r"^public.bzl",
    r"^src/ports/fontations/.+",
    r"^src/sksl/lex/BUILD.bazel",
]

HARFBUZZ_SELECT = """select({
    "//tools/cc_target_os:wasm": [
        "//third_party/harfbuzz:harfbuzz-subset",
        "//third_party/harfbuzz:harfbuzz_no_google_ucdn",
    ],
    "//third_party/skia/HEAD:use_harfbuzz_ucdn_setting": [
        "//third_party/harfbuzz:harfbuzz-subset",
        "//third_party/harfbuzz:harfbuzz_no_google_ucdn",
    ],
    "//conditions:default": [
        "//third_party/harfbuzz",
        "//third_party/harfbuzz:harfbuzz-subset",
    ],
})"""

REWRITE_BAZEL_REGEX: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r'"//bazel/external/fontconfig.*?"'),
        '"//third_party/skia/HEAD/google3:fontconfig"',
    ),
    (re.compile(r'"@rules_cc//'), '"//third_party/bazel_rules/rules_cc/'),
    (re.compile(r'"//bazel/external/glesv2"'), '"//third_party/GL:EGL_headers"'),
    (re.compile(r'"//bazel/external/egl"'), '"//third_party/GL:GLES2_headers"'),
    (
        re.compile(r'"//bazel:rust_cxx_bridge.bzl"'),
        '"//devtools/rust/cxx:rust_cxx_bridge.bzl"',
    ),
    (re.compile(r'"//(bazel)'), r'"//third_party/skia/HEAD/\1'),
    (re.compile(r'"//(example)'), r'"//third_party/skia/HEAD/\1'),
    (re.compile(r'"//include/third_party/vulkan"'), '"//third_party/vulkan_headers"'),
    (re.compile(r'"@vulkan_headers"'), '"//third_party/vulkan_headers"'),
    (re.compile(r'"//(include)'), r'"//third_party/skia/HEAD/\1'),
    (re.compile(r'"//src/ports/fontations:'), '"//third_party/skia/HEAD/google3:fontations_'),
    (re.compile(r'"//(src)'), r'"//third_party/skia/HEAD/\1'),
    (re.compile(r'"//(modules)'), r'"//third_party/skia/HEAD/\1'),
    (re.compile(r'"//:'), '"//third_party/skia/HEAD:'),
    (
        re.compile(r'"@bazel_skylib//lib:selects.bzl"'),
        '"//third_party/bazel_skylib/lib:selects.bzl"',
    ),
    (re.compile(r"@platforms//os"), "//third_party/bazel_platforms/os"),
    (re.compile(r"@platforms//cpu"), "//third_party/bazel_platforms/cpu"),
    (re.compile(r"@abseil_cpp//absl"), "//third_party/absl"),
    (
        re.compile(r"@bazel_tools//tools/allowlists/function_transition_allowlist"),
        "//tools/allowlists/function_transition_allowlist",
    ),
    (
        re.compile(r"@bazel_skylib//rules:common_settings.bzl"),
        "//third_party/bazel_skylib/rules:common_settings.bzl",
    ),
    (
        re.compile(r"//third_party/bazel_rules/rules_cc/cc"),
        "//third_party/bazel_rules/rules_cc/cc",
    ),
    (re.compile(r"#\s*REPLACE_WHEN_BUILDING_IN_GOOGLE3"), '"SK_BUILD_FOR_GOOGLE3",'),
    (re.compile(r"@skia_user_config//"), "//third_party/skia/HEAD/bazel"),
    (re.compile(r'"@dawn.*?"'), '"//third_party/skia/HEAD/google3:dawn"'),
    (re.compile(r'"@dng_sdk.*?"'), '"//third_party/skia/HEAD/google3:dng_sdk"'),
    (re.compile(r'"@expat.*?"'), '"//third_party/skia/HEAD/google3:expat"'),
    (re.compile(r'"@freetype.*?"'), '"//third_party/skia/HEAD/google3:freetype"'),
    (re.compile(r'\[\s*"@harfbuzz",?\s*\]'), HARFBUZZ_SELECT),
    (re.compile(r'"@icu//:icu_headers"'), '"//third_party/icu:headers"'),
    (
        re.compile(r'"@icu.*?"'),
        '"//third_party/icu:breakiterator",\n"//third_party/icu:common",\n'
        '"//third_party/icu:locale"',
    ),
    (re.compile(r'"@libavif.*?"'), '"//third_party/skia/HEAD/google3:libavif"'),
    (re.compile(r'"@libjpeg_turbo.*?"'), '"//third_party/skia/HEAD/google3:libjpeg_turbo"'),
    (re.compile(r'"@libjxl.*?"'), '"//third_party/skia/HEAD/google3:libjxl"'),
    (re.compile(r'"@libpng*?"'), '"//third_party/skia/HEAD/google3:libpng"'),
    (
        re.compile(r'"@libwebp.*?"'),
        '"//third_party/libwebp:webp_decode",\n'
        '"//third_party/libwebp:webp_demux",\n'
        '"//third_party/libwebp:webp_encode",\n'
        '"//third_party/libwebp:webp_mux"',
    ),
    (
        re.compile(r'"@maven//:androidx_annotation_annotation"'),
        '"//third_party/java/androidx/annotation"',
    ),
    (re.compile(r'"@perfetto.*?"'), '"//third_party/skia/HEAD/google3:perfetto"'),
    (re.compile(r'"@piex.*?"'), '"//third_party/skia/HEAD/google3:piex"'),
    (re.compile(r'"@spirv_cross.*?"'), '"//third_party/skia/HEAD/google3:spirv_cross"'),
    (re.compile(r'"@spirv_tools.*?"'), '"//third_party/skia/HEAD/google3:spirv_tools"'),
    (re.compile(r'"@wuffs.*?"'), '"//third_party/skia/HEAD/google3:wuffs"'),
    (
        re.compile(r'"@vulkanmemoryallocator.*?"'),
        '"//third_party/skia/HEAD/google3:vulkan_memory_allocator"',
    ),
    (
        re.compile(r'"//third_party/vulkanmemoryallocator"'),
        '"//third_party/skia/HEAD/third_party/vulkanmemoryallocator"',
    ),
    (re.compile(r'"@zlib.*?"'), '"//third_party/rust/zlib_rs:zlib"'),
    (re.compile(r'"@crates//:cxx"'), '"//third_party/rust/cxx/v1:cxx"'),
    (re.compile(r'"@crates//:cxx_cc"'), '"//third_party/rust/cxx/v1:cxx_cc"'),
    (re.compile(r'"@crates//:image"'), '"//third_party/rust/image/v0_25:image"'),
    (re.compile(r'"@crates//:moxcms"'), '"//third_party/rust/moxcms/v0_8:moxcms"'),
    (re.compile(r'"@crates//:png"'), '"//third_party/rust/png/v0_18:png"'),
    (re.compile(r'"@rules_rust//'), '"//third_party/bazel_rules/rules_rust/'),
    (re.compile(r'"//(rust)'), r'"//third_party/skia/HEAD/\1'),
    (
        re.compile(r'name = "for_android",\s*default = True,'),
        'name = "for_android",\n    default = False,',
    ),
    (re.compile(r"rust_static_library"), "rust_library"),
]

THIRD_PARTY_INCLUDE_REWRITES: list[tuple[str, str]] = [
    (
        "#include <fontconfig/fontconfig.h>",
        '#include "third_party/fontconfig/fontconfig/fontconfig.h"',
    ),
    ("#include <hb.h>", '#include "third_party/harfbuzz/src/hb.h"'),
    ("#include <hb-subset.h>", '#include "third_party/harfbuzz/src/hb-subset.h"'),
    ("#include <png.h>", '#include "third_party/png/png.h"'),
    ("#include <pngconf.h>", '#include "third_party/png/pngconf.h"'),
    ("#include <zlib.h>", '#include "third_party/rust/zlib_rs/zlib.h"'),
]

_INCLUDE_MATCHERS = [re.compile(p) for p in INCLUDE_PATTERNS]
_EXCLUDE_MATCHERS = [re.compile(p) for p in EXCLUDE_PATTERNS]
_REWRITE_BAZEL_EXCLUDES = [re.compile(p) for p in REWRITE_BAZEL_EXCLUDE_PATTERNS]
_CPP_INCLUDE_RE = re.compile(r'#include\s+"(?!third_party/)([^\n"]+)"([^\n]*)\n')
_CPP_NO_REWRITE_RE = re.compile(
    r'#include "third_party/skia/HEAD/([^\n"]+)"\s*//\s*NO_G3_REWRITE[^\n]*\n'
)
_RUST_INCLUDE_RE = re.compile(r'include!\("(rust|modules)/')
_GIT_ID_BLOCK_RE = re.compile(
    r'identifier\s*\{[^}]*type:\s*"Git"[^}]*version:\s*"([0-9a-f]{40})"',
    re.IGNORECASE | re.DOTALL,
)
_VERSION_RE = re.compile(r'version:\s*"([0-9a-f]{40})"')
_CHECKED_MODULE_PREFIXES = (
    "modules/skparagraph/src/",
    "modules/skparagraph/include/",
    "modules/skunicode/src/",
    "modules/skunicode/include/",
    "modules/skshaper/src/",
    "modules/skshaper/include/",
    "modules/skshaper/utils/",
)


# Match autoroller_lib._GetDiffedFiles file filtering
def should_include_file(git_rel_path: str) -> bool:
    return any(m.match(git_rel_path) for m in _INCLUDE_MATCHERS) and not any(
        m.match(git_rel_path) for m in _EXCLUDE_MATCHERS
    )


def should_rewrite_cpp_file(git_rel_path: str) -> bool:
    return git_rel_path.endswith((".h", ".cpp", ".mm"))


def should_rewrite_rust_file(git_rel_path: str) -> bool:
    return git_rel_path.endswith(".rs")


def should_rewrite_bazel_file(git_rel_path: str) -> bool:
    if not git_rel_path.endswith(("BUILD.bazel", ".bzl")):
        return False
    return not any(m.search(git_rel_path) for m in _REWRITE_BAZEL_EXCLUDES)


# Prefix internal quotes with third_party/skia/HEAD/ and map system headers for layering_check
def rewrite_cpp_content(content: str) -> str:
    out = _CPP_INCLUDE_RE.sub(r'#include "third_party/skia/HEAD/\1"\2\n', content)
    out = _CPP_NO_REWRITE_RE.sub(r'#include "\1"  // NO_G3_REWRITE\n', out)
    for src, dst in THIRD_PARTY_INCLUDE_REWRITES:
        out = out.replace(src, dst)
    return out


def rewrite_rust_content(content: str) -> str:
    return _RUST_INCLUDE_RE.sub(r'include!("third_party/skia/HEAD/\1/', content)


# Transform Skia Bazel targets to google3 labels and sort deps via buildifier
def rewrite_bazel_content(content: str, bazel_rel_path: str) -> str:
    out = (
        "# THIS FILE IS AUTOGENERATED BY go/skia-google3-autoroller\n"
        "# DO NOT EDIT BY HAND.\n" + content
    )
    for find, replace in REWRITE_BAZEL_REGEX:
        out = find.sub(replace, out)
    buildifier = shutil.which("buildifier") or "/usr/bin/buildifier"
    if pathlib.Path(buildifier).exists():
        proc = subprocess.run(
            [buildifier, "-path", bazel_rel_path, "-lint=off", "-mode=fix"],
            input=out,
            check=False,
            capture_output=True,
            encoding="utf-8",
        )
        if proc.stdout:
            return proc.stdout
    return out


# Parse mirrored Skia git SHA from third_party/skia/HEAD/METADATA
def read_g3_skia_revision(metadata_path: pathlib.Path) -> str:
    text = metadata_path.read_text(encoding="utf-8")
    git_match = _GIT_ID_BLOCK_RE.search(text)
    if git_match:
        return git_match.group(1).lower()
    ver_match = _VERSION_RE.search(text)
    if ver_match:
        return ver_match.group(1).lower()
    raise RuntimeError(f"Could not find 40-hex git revision in {metadata_path}")


# Catch GN-only file additions/deletions that forgot to update Skia BUILD.bazel for google3
def check_bazel_filegroup_sync(
    added_files: list[str],
    deleted_files: list[str],
    skia_tree_root: pathlib.Path,
) -> list[str]:
    errors: list[str] = []
    for rel in sorted(added_files):
        if not rel.startswith(_CHECKED_MODULE_PREFIXES) or not rel.endswith((".h", ".cpp", ".mm")):
            continue
        p = skia_tree_root / rel
        bazel_file = p.parent / "BUILD.bazel"
        if not bazel_file.exists():
            continue
        text = bazel_file.read_text(encoding="utf-8")
        if f'"{p.name}"' not in text:
            errors.append(
                f"Added file {rel} is missing from {bazel_file.relative_to(skia_tree_root)}"
            )
    for rel in sorted(deleted_files):
        if not rel.startswith(_CHECKED_MODULE_PREFIXES) or not rel.endswith((".h", ".cpp", ".mm")):
            continue
        p = skia_tree_root / rel
        bazel_file = p.parent / "BUILD.bazel"
        if not bazel_file.exists():
            continue
        text = bazel_file.read_text(encoding="utf-8")
        if f'"{p.name}"' in text:
            errors.append(
                f"Deleted file {rel} is still listed in {bazel_file.relative_to(skia_tree_root)}"
            )
    return errors


# Sync changed/deleted Skia git files into google3/third_party/skia/HEAD and return touched paths
def sync_skia_tree_to_g3(
    skia_tree_root: pathlib.Path,
    g3_root: pathlib.Path,
    changed_or_added: list[str],
    deleted: list[str],
) -> dict[str, list[str]]:
    g3_skia_root = g3_root / G3_SKIA_REL
    written_rel: list[str] = []
    added_new_rel: list[str] = []
    deleted_rel: list[str] = []

    def _write_g3(rel_under_skia: str, data: bytes) -> None:
        dest = g3_skia_root / rel_under_skia
        existed = dest.exists()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        written_rel.append(f"{G3_SKIA_REL}/{rel_under_skia}")
        if not existed:
            added_new_rel.append(f"{G3_SKIA_REL}/{rel_under_skia}")

    for rel in sorted(changed_or_added):
        if not should_include_file(rel):
            continue
        src_path = skia_tree_root / rel
        raw_bytes = src_path.read_bytes()
        _write_g3(rel, raw_bytes)

        if should_rewrite_cpp_file(rel):
            rewritten = rewrite_cpp_content(raw_bytes.decode("utf-8"))
            (g3_skia_root / rel).write_text(rewritten, encoding="utf-8")
        elif should_rewrite_rust_file(rel):
            rewritten = rewrite_rust_content(raw_bytes.decode("utf-8"))
            (g3_skia_root / rel).write_text(rewritten, encoding="utf-8")

        if should_rewrite_bazel_file(rel):
            rewritten_bzl = rewrite_bazel_content(raw_bytes.decode("utf-8"), rel)
            target_rel = rel[: -len(".bazel")] if rel.endswith(".bazel") else rel
            _write_g3(target_rel, rewritten_bzl.encode("utf-8"))

    for rel in sorted(deleted):
        if not should_include_file(rel):
            continue
        candidates = [rel]
        if should_rewrite_bazel_file(rel) and rel.endswith(".bazel"):
            candidates.append(rel[: -len(".bazel")])
        for cand in candidates:
            dest = g3_skia_root / cand
            if dest.exists():
                dest.unlink()
                deleted_rel.append(f"{G3_SKIA_REL}/{cand}")

    return {
        "written": sorted(set(written_rel)),
        "added_new": sorted(set(added_new_rel)),
        "deleted": sorted(set(deleted_rel)),
    }
