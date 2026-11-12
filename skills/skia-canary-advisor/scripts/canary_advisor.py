#!/usr/bin/env python3
"""Recommend Skia Canary try jobs (Flutter, G3, Chromium, Android) for a Skia diff or Gerrit CL.

`advise` only recommends and prints commands; `try` is the one subcommand that triggers builds.
Every run is logged to ~/.gemini/config/ledgers/canary_advisor.jsonl so that `stats` can later
measure how often the recommendation was actually followed.

Usage:
  canary_advisor.py advise --cl 1402416            # Gerrit CL (own or under review)
  canary_advisor.py advise --repo ~/skia [--base origin/main]
  canary_advisor.py try --cl 1402416 -b <builder>  # git cl try + Buildbucket check (TRIGGER:)
  canary_advisor.py triage --cl 1402416 [--ps 3]   # triage tryjobs (ASAN + Canaries)
  canary_advisor.py watch --cl 1402416             # watch tryjobs + triage on completion
  canary_advisor.py health                         # per-canary health from Buildbucket
  canary_advisor.py stats                          # follow-rate of logged recommendations
"""

import argparse
import base64
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GERRIT = "https://skia-review.googlesource.com"
BUCKET = "skia/skia.primary"
CANARIES = ["Canary-Flutter", "Canary-G3", "Canary-Chromium", "Canary-Android"]
ME = "jlavrova@google.com"
LEDGER = os.environ.get("CANARY_ADVISOR_LEDGER",
                        os.path.expanduser("~/.gemini/config/ledgers/canary_advisor.jsonl"))
LUCI_TOKEN_TTL_S = 240

# depot_tools lives in ~/depot_tools on Linux and in ~/Sources/depot_tools on the Mac
def _depot_tool(name):
  for root in ("~/depot_tools", "~/Sources/depot_tools"):
    path = os.path.expanduser(os.path.join(root, name))
    if os.path.exists(path):
      return path
  return shutil.which(name) or os.path.expanduser(os.path.join("~/depot_tools", name))


BB = _depot_tool("bb")
PRPC = _depot_tool("prpc")
LUCI_AUTH = _depot_tool("luci-auth")
GARDENER_DIR = os.path.expanduser("~/.gemini/jetski/scratch/skia_gardener")
GOOGLE3_DIR = "/google/src/head/depot/google3"
# `git cl try` needs a git checkout as cwd, whatever directory the watcher was started from.
# The Mac has no Jetski scratch clone, only ~/Sources/skia
SKIA_DIR = next((p for p in (os.path.expanduser("~/.gemini/jetski/scratch/skia"),
                             os.path.expanduser("~/Sources/skia")) if os.path.isdir(p)),
                os.path.expanduser("~/.gemini/jetski/scratch/skia"))
HEALTH_WINDOW_DAYS = 45

# Canaries that must never be triggered: they run but give no verdict. Remove an entry only after
# the bot produces real SUCCESS/FAILURE results again
# Canary-Flutter left this list on 2026-11-12 (R58): buildbot CL 1409959 (deployed 2026-11-03)
# taught the autoroller that GitHub "skipped" checks are finished, so runs give verdicts again
DISABLED_CANARIES = {}
FLUTTER_FULL_CORPUS = ("python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/"
                       "run_flutter_paragraph_tests.py --full")
# Canary-Flutter runs flutter/try + flutter-gold but not the ~2400 text tests the local corpus
# compares A/B, so these paths keep the local gate even when the canary is healthy
FLUTTER_PARAGRAPH_PATH_RE = re.compile(r"^modules/(skparagraph|skunicode|skshaper)/(src|include)/")
G3_PARAGRAPH_PATH_RE = re.compile(r"^modules/(skparagraph|skunicode)/")
G3_PARAGRAPH_CORPUS_SCRIPT = (
    "~/.gemini/config/skills/google3-test-paragraph/scripts/run_g3_paragraph_tests.py"
)
# Same scope as SKIA_PERF_PATH_RE in hooks/pre_tool_guard.py. That hook only sees our own edits,
# so reviews of other authors' CLs never ran txt_benchmarks (Gerrit 1418836 PS1 perf CL)
PERF_GATE_PATH_RE = re.compile(
    r"^modules/(?:skparagraph|skunicode|skshaper)/(?:src|include)/"
    r"|^(?:include/core|include/private/base|src/base|src/core)/Sk[^/]*$"
)
PERF_GATE_CODE_RE = re.compile(r"\.(?:c|cc|cpp|h|hpp|inc)$")
PERF_GATE_SCRIPT = (
    "~/.gemini/config/skills/flutter-engine-perf-compare/scripts/skia_patch_ab.py"
)

# Client-side files that define SK_* macros; a removed guard is only dangerous if a client still
# defines it. google3 has no single config file, so it is delegated to the agent via code_search
CLIENT_DEFINE_FILES = {
    "Canary-Chromium": [
        "https://chromium.googlesource.com/chromium/src/+/refs/heads/main/"
        "skia/config/SkUserConfig.h",
        "https://chromium.googlesource.com/chromium/src/+/refs/heads/main/skia/BUILD.gn",
    ],
    "Canary-Android": [
        "https://android.googlesource.com/platform/external/skia/+/refs/heads/main/"
        "include/config/SkUserConfigManual.h",
        "https://android.googlesource.com/platform/external/skia/+/refs/heads/main/Android.bp",
    ],
    "Canary-Flutter": [
        "https://raw.githubusercontent.com/flutter/flutter/master/engine/src/flutter/skia/BUILD.gn",
    ],
}

# Paths with no effect on any client build or behavior
NO_EFFECT = re.compile(
    r"^(tests|gm|bench|docs|site|tools|infra|relnotes|experimental|example|demos\.skia\.org|"
    r"resources|specs|fuzz|dm)/|\.md$|^RELEASE_NOTES|OWNERS|^modules/[^/]+/(tests|gm|slides)/")

# (path regex, canaries, tier, reason). Consumers were checked against client build files:
# Chromium's skia/BUILD.gn does not build skparagraph/skunicode; Android.bp compiles skparagraph
# only into skia_gm_srcs; google3 has many direct skparagraph users (YouTube, Impress, Flutter)
AREA_RULES = [
    (r"^include/private/chromium/|^src/text/gpu/SkChromeRemoteGlyphCache",
     ["Canary-Chromium"], "STRONG",
     "Chromium-specific OOP-R glyph cache and private headers"),
    (r"^modules/(skparagraph|skunicode)/(src|include)/", ["Canary-Flutter", "Canary-G3"], "STRONG",
     "text layout behavior: Flutter and google3 are the SkParagraph/SkUnicode consumers"),
    (r"^modules/skshaper/(src|include)/", ["Canary-Flutter", "Canary-G3"], "STRONG",
     "shaping behavior: consumed by SkParagraph clients"),
    (r"^src/ports/SkFontMgr_android", ["Canary-Android"], "STRONG",
     "Android font manager is only exercised by Android"),
    (r"^src/ports/(SkFontMgr_fontconfig|SkFontConfigInterface|SkFontMgr_FontConfigInterface)",
     ["Canary-Chromium", "Canary-G3"], "CONSIDER", "fontconfig font manager"),
    (r"^src/ports/(SkFontMgr_mac|SkTypeface_mac|SkScalerContext_mac)",
     ["Canary-Chromium", "Canary-Flutter"], "CONSIDER", "CoreText port"),
    (r"^src/ports/(SkFontMgr_win|SkTypeface_win|SkScalerContext_win|SkFontMgr_indirect)",
     ["Canary-Chromium", "Canary-Flutter"], "CONSIDER", "DirectWrite port"),
    (r"^(src/ports/.*[Ff]ontations|rust/)", ["Canary-Chromium", "Canary-G3"], "CONSIDER",
     "Fontations backend"),
    (r"(^|/)BUILD\.bazel$|\.bzl$", ["Canary-G3"], "STRONG", "bazel files are consumed by google3"),
    (r"^gn/.*\.gni$|(^|/)BUILD\.gn$", ["Canary-Chromium", "Canary-Android", "Canary-Flutter"],
     "STRONG", "gn source lists feed Chromium, Flutter and Android (gn_to_bp)"),
    (r"^(src|include)/", ["Canary-G3", "Canary-Chromium"], "CONSIDER",
     "core behavior change; only worth it if rendering output may change"),
]

KEYWORDS = {
    "if", "for", "while", "switch", "return", "sizeof", "alignof", "decltype", "static_assert",
    "SkASSERT", "SkDEBUGCODE", "SK_API", "SK_SPI", "operator", "template", "defined", "noexcept",
    "explicit", "const", "constexpr", "override", "final", "virtual", "static", "inline", "void",
    "int", "bool", "float", "size_t", "SkScalar", "char", "auto", "using", "typename", "class",
    "struct", "enum", "namespace", "delete", "default", "new",
}


_LUCI_TOKEN_CACHE = ("", 0.0)


# Authenticate Gerrit calls via git-credential-luci to avoid anonymous IP HTTP 429 limits
def _gerrit_luci_token(force_refresh=False):
    global _LUCI_TOKEN_CACHE
    tok, ts = _LUCI_TOKEN_CACHE
    # Refresh cached OAuth tokens every 4 minutes before Gerrit /a/ rejects them with HTTP 400
    if not force_refresh and tok and (time.time() - ts) < LUCI_TOKEN_TTL_S:
        return tok
    try:
        proc = subprocess.run(
            ["git-credential-luci", "get"],
            input="protocol=https\nhost=skia-review.googlesource.com\n\n",
            capture_output=True, text=True, timeout=10, check=True,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                tok = line.split("=", 1)[1].strip()
                if tok:
                    _LUCI_TOKEN_CACHE = (tok, time.time())
                    return tok
    except (subprocess.SubprocessError, OSError):
        pass
    # Drop stale cached token when git-credential-luci fails so callers fall back cleanly
    _LUCI_TOKEN_CACHE = ("", 0.0)
    return ""


# Execute an authenticated Gerrit /a/ request and retry once with a fresh token on HTTP 400/401/403
def _gerrit_authed_urlopen(url, *, data=None, method=None, headers=None, timeout=20):
    global _LUCI_TOKEN_CACHE
    tok = _gerrit_luci_token()
    if not tok:
        return None, "missing_luci_token"
    base_headers = {"User-Agent": "canary-advisor", **(headers or {})}
    req = urllib.request.Request(
        url,
        data=data,
        headers={**base_headers, "Authorization": f"Bearer {tok}"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (getattr(r, "status", 200), r.read()), ""
    except urllib.error.HTTPError as e:
        # Refresh cached LUCI OAuth token once when Gerrit /a/ rejects a stale Bearer token
        if e.code in (400, 401, 403):
            _LUCI_TOKEN_CACHE = ("", 0.0)
            fresh = _gerrit_luci_token(force_refresh=True)
            if fresh and fresh != tok:
                retry_req = urllib.request.Request(
                    url,
                    data=data,
                    headers={**base_headers, "Authorization": f"Bearer {fresh}"},
                    method=method,
                )
                try:
                    with urllib.request.urlopen(retry_req, timeout=timeout) as r:
                        return (getattr(r, "status", 200), r.read()), ""
                except (urllib.error.URLError, OSError) as e2:
                    return None, str(e2)
        return None, str(e)
    except (urllib.error.URLError, OSError) as e:
        return None, str(e)


def http_get(url, timeout=20):
    global _LUCI_TOKEN_CACHE
    orig_url = url
    headers = {"User-Agent": "canary-advisor"}
    # Route Gerrit reads through /a/ when a LUCI token is available
    is_gerrit = orig_url.startswith(GERRIT + "/") and not orig_url.startswith(GERRIT + "/a/")
    tok = _gerrit_luci_token() if is_gerrit else ""
    if tok:
        url = GERRIT + "/a/" + orig_url[len(GERRIT) + 1:]
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # Gerrit /a/ returns HTTP 400/401/403 when an OAuth token expires mid-watch
        if not (tok and e.code in (400, 401, 403)):
            raise
        _LUCI_TOKEN_CACHE = ("", 0.0)
        fresh = _gerrit_luci_token(force_refresh=True)
        # Retry authenticated /a/ once when git-credential-luci mints a distinct fresh token
        if fresh and fresh != tok:
            retry_req = urllib.request.Request(
                GERRIT + "/a/" + orig_url[len(GERRIT) + 1:],
                headers={"User-Agent": "canary-advisor", "Authorization": f"Bearer {fresh}"},
            )
            try:
                with urllib.request.urlopen(retry_req, timeout=timeout) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e2:
                if e2.code not in (400, 401, 403):
                    raise
        # Public Skia Gerrit changes remain readable anonymously if LUCI auth stays unavailable
        anon_req = urllib.request.Request(orig_url, headers={"User-Agent": "canary-advisor"})
        with urllib.request.urlopen(anon_req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")


def gerrit_json(path):
    return json.loads(http_get(GERRIT + path)[4:])


def gitiles_text(url):
    if "googlesource.com" in url:
        return base64.b64decode(http_get(url + "?format=TEXT")).decode("utf-8", "replace")
    return http_get(url)


def parse_diff(text):
    """Returns {path: {"added": [...], "removed": [...], "status": "A"|"D"|"M"}}."""
    files, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if m:
            cur = files.setdefault(m.group(2), {"added": [], "removed": [], "status": "M"})
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur["status"] = "A"
        elif line.startswith("deleted file mode"):
            cur["status"] = "D"
        elif line.startswith("+") and not line.startswith("+++"):
            cur["added"].append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            cur["removed"].append(line[1:])
    return files


def decl_names(lines):
    names = set()
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith(("//", "*", "/*", "#include")):
            continue
        for m in re.finditer(r"\b(?:class|struct|enum(?:\s+class)?)\s+(?:SK_API\s+)?([A-Za-z_]\w*)",
                             s):
            names.add(m.group(1))
        for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", s):
            names.add(m.group(1))
    return {n for n in names if n not in KEYWORDS and len(n) > 2 and not n.isupper()}



def choose_asan_bot(effective_paths):
    asan_bots = set()
    for path in effective_paths:
        if path.endswith(('.md', '.dart', '.ts', '.txt', '.py', '.gn', '.gni', '.bzl', '.json')) or "BUILD" in path or "OWNERS" in path:
            continue
            
        p = path.lower()
        if re.search(r"^(modules/(skparagraph|skunicode|skshaper)|src/ports|tests?/(paragraph|shaper|unicode|text|font))", p) or "/text/" in p or "font" in p:
             asan_bots.add("Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-NativeFonts_FreeType_ASAN_Upload")
        elif "metal" in p and ("graphite" in p or "ganesh" in p or "gpu" in p):
             asan_bots.add("Test-Mac15-Clang-MacMini16.10-GPU-AppleM4-arm64-Debug-All-ASAN_Graphite_Native_Metal")
        elif "graphite" in p or "ganesh" in p or "gpu" in p or "vulkan" in p:
             asan_bots.add("Test-Ubuntu24.04-Clang-NUC11TZi5-GPU-IntelIrisXe-x86_64-Debug-All-DDL3_ASAN_Vulkan")
        elif re.search(r"^(src|include|tests|bench)/", p) and p.endswith(('.h', '.cpp', '.c', '.cc', '.m', '.mm')):
             asan_bots.add("Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-ASAN")
             
    if "Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-NativeFonts_FreeType_ASAN_Upload" in asan_bots:
        return "Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-NativeFonts_FreeType_ASAN_Upload", "Text/Fonts/Ports/Perf diff"
    if "Test-Ubuntu24.04-Clang-NUC11TZi5-GPU-IntelIrisXe-x86_64-Debug-All-DDL3_ASAN_Vulkan" in asan_bots:
        return "Test-Ubuntu24.04-Clang-NUC11TZi5-GPU-IntelIrisXe-x86_64-Debug-All-DDL3_ASAN_Vulkan", "Graphite/Ganesh/Vulkan GPU diff"
    if "Test-Mac15-Clang-MacMini16.10-GPU-AppleM4-arm64-Debug-All-ASAN_Graphite_Native_Metal" in asan_bots:
         return "Test-Mac15-Clang-MacMini16.10-GPU-AppleM4-arm64-Debug-All-ASAN_Graphite_Native_Metal", "Metal GPU diff"
    if "Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-ASAN" in asan_bots:
        return "Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-ASAN", "General CPU C++ diff"
    return None, None

MAC_NATIVE_FONTS_BOT = (
    "Test-Mac14-Clang-MacMini8.1-CPU-AVX2-x86_64-Debug-All-NativeFonts_Fontations"
)
FONT_TEST_HINT_RE = re.compile(
    r"\b(?:TestFontMgr|DefaultFontMgr|makeFromData|makeFromStream|makeFromFile|"
    r"MakeFromData|MakeFromFile|MakeFromName|CreateTestTypeface|DefaultTypeface|"
    r"SampleUserTypeface|SkRemoteGlyphCache|SkScalerContext)\b"
)

def needs_mac_native_fonts_bot(files):
    for path, data in files.items():
        p = path.lower()
        if not (p.startswith("tests/") or "/tests/" in p) or not p.endswith((".cpp", ".cc", ".h")):
            continue
        added_text = " ".join(data.get("added", []))
        if FONT_TEST_HINT_RE.search(added_text):
            return (
                MAC_NATIVE_FONTS_BOT,
                "Font-backed cross-platform test diff (CoreText NativeFonts check)",
            )
    return None, None

def is_public_header(path):
    return bool(re.match(r"^(include/|modules/[^/]+/include/)", path)) and path.endswith(".h")


def removed_defines(files):
    out = set()
    for f in files.values():
        added = " ".join(f["added"])
        for ln in f["removed"]:
            for d in re.findall(r"\b(SK_(?:SUPPORT_LEGACY|LEGACY|DISABLE|ENABLE|USE)_\w+)", ln):
                if d not in added:
                    out.add(d)
    return sorted(out)


def api_changes(files):
    """(symbol, header stem) pairs for public declarations that were removed or rewritten.

    The header stem (e.g. SkCodec) is paired with the symbol because bare method names such as
    Register produce hundreds of unrelated hits in client code search"""
    removed, changed = set(), set()
    for path, f in files.items():
        if not is_public_header(path):
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        r, a = decl_names(f["removed"]), decl_names(f["added"])
        removed |= {(n, stem) for n in r - a}
        changed |= {(n, stem) for n in r & a}
    return sorted(removed), sorted(changed - removed)


def flutter_usage(symbol, stem):
    """Hits in flutter/flutter engine via GitHub code search, or None if search is unavailable."""
    terms = f'"{symbol}"' if symbol == stem else f'"{symbol}" "{stem}"'
    q = f"{terms} repo:flutter/flutter path:engine/src/flutter"
    try:
        out = subprocess.run(["gh", "api", "-X", "GET", "search/code", "-f", f"q={q}",
                              "--jq", ".total_count"], capture_output=True, text=True, timeout=30)
        return int(out.stdout.strip()) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def define_users(define):
    users = {}
    for canary, urls in CLIENT_DEFINE_FILES.items():
        for url in urls:
            try:
                if re.search(r"\b" + re.escape(define) + r"\b", gitiles_text(url)):
                    users.setdefault(canary, []).append(url.rsplit("/", 1)[-1])
            except OSError:
                users.setdefault(canary, []).append("UNREACHABLE " + url)
    return users


def bb_builds(builder, n=25, cl=None):
    cmd = [BB, "ls", "-n", str(n), "-json", "-fields", "status,create_time"]
    if cl:
        cmd += ["-cl", cl]
    cmd.append(f"{BUCKET}/{builder}")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=90, cwd="/tmp")
    return [json.loads(l) for l in out.stdout.splitlines() if l.startswith("{")]


def canary_health(builder):
    """A canary is healthy if it produced a real verdict (SUCCESS/FAILURE) recently.

    INFRA_FAILURE-only history (e.g. 3h timeouts waiting for target CI) gives no signal."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=HEALTH_WINDOW_DAYS)
    builds = bb_builds(builder, n=60)
    recent = [b for b in builds
              if datetime.datetime.fromisoformat(b["createTime"][:19] + "+00:00") >= cutoff]
    verdicts = [b for b in recent if b["status"] in ("SUCCESS", "FAILURE")]
    counts = {}
    for b in recent:
        counts[b["status"]] = counts.get(b["status"], 0) + 1
    return {"healthy": bool(verdicts), "counts": counts}


def load_cl(cl, ps=None, include_diff=True):
    url = f"/changes/{cl}?o=DETAILED_ACCOUNTS&o=ALL_REVISIONS&o=DETAILED_LABELS"
    info = gerrit_json(url)
    cur_rev = None
    if "current_revision_number" in info:
        cur_num = info["current_revision_number"]
        cur_rev = next(
            (r for r in info["revisions"].values()
             if r.get("_number") == cur_num or r.get("ref", "").endswith(f"/{cur_num}")),
            None,
        )
    if not cur_rev and "current_revision" in info:
        cur_rev = info["revisions"].get(info["current_revision"])
    if not cur_rev:
        cur_rev = next(iter(info["revisions"].values()))
    target_rev = cur_rev
    if ps is not None:
        for rev in info.get("revisions", {}).values():
            if rev.get("_number") == int(ps):
                target_rev = rev
                break
    ps_num = target_rev["_number"]
    # Watch polls skip the patch download after the initial file-list parse
    patch = ""
    if include_diff:
        patch_url = f"{GERRIT}/changes/{cl}/revisions/{ps_num}/patch"
        patch = base64.b64decode(http_get(patch_url)).decode("utf-8", "replace")
    cr_label = info.get("labels", {}).get("Code-Review", {})
    # Canary-G3 requires either a Googler author or Googler CR+1 (g3_canary.go:134)
    googler_cr_plus_one = any(
        vote.get("value", 0) >= 1 and vote.get("email", "").endswith("@google.com")
        for vote in cr_label.get("all", [])
    )
    return {
        "cl": int(cl), "patchset": ps_num, "current_patchset": cur_rev["_number"],
        "status": info.get("status", "NEW"),
        "subject": info["subject"], "owner": info["owner"].get("email", ""), "diff": patch,
        "googler_cr_plus_one": googler_cr_plus_one,
        "kinds": {r.get("_number"): r.get("kind", "") for r in info.get("revisions", {}).values()},
    }


# A description-only patchset (Gerrit kind NO_CODE_CHANGE) has the same tree as its parent
# patchset, so tryjobs from the last patchset with code still describe it
def code_patchset(meta, ps):
    kinds = meta.get("kinds") or {}
    while ps > 1 and kinds.get(ps) == "NO_CODE_CHANGE":
        ps -= 1
    return ps


TESTED_HEADER_RE = re.compile(r"^(?:Tested|Test):", re.MULTILINE)


# The CL description must list every green trybot under Tested:, since corpora and trybots
# often finish after upload (Gerrit 1417397 PS1 kept only a single-test Test: line)
def tested_block_gaps(cl, ps, green_builders):
    msg = gerrit_json(f"/changes/{cl}/revisions/{ps}/commit").get("message", "")
    if not TESTED_HEADER_RE.search(msg):
        return ["no Tested: block"]
    return [b for b in sorted(green_builders) if b not in msg]


# Local CitC mount on Linux Cloudtop, or corp gMac forwarding over SSH via corp-ssh-helper
def has_cloudtop_citc():
    if os.path.isdir("/google/src/cloud"):
        return True
    if os.environ.get("GOOGLE3_PARAGRAPH_SSH_HOST", "").strip():
        return True
    return sys.platform == "darwin" and os.path.exists("/usr/local/bin/corp-ssh-helper")


def advise(args):
    if args.cl:
        meta = load_cl(args.cl)
    else:
        base = subprocess.run(["git", "-C", args.repo, "merge-base", args.base, "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        diff = subprocess.run(["git", "-C", args.repo, "diff", base], capture_output=True,
                              text=True, check=True).stdout
        meta = {"cl": None, "patchset": None, "subject": "(local diff)", "owner": ME, "diff": diff}

    files = parse_diff(meta["diff"])
    recs = {}

    # Reasons are grouped so that one rule matching many files prints a single line
    def add(canary, tier, reason, path=None):
        r = recs.setdefault(canary, {"tier": tier, "reasons": {}})
        if tier == "STRONG":
            r["tier"] = "STRONG"
        paths = r["reasons"].setdefault(reason, [])
        if path and path not in paths:
            paths.append(path)

    effective = [p for p in files if not NO_EFFECT.search(p)]
    for path in effective:
        for rx, canaries, tier, reason in AREA_RULES:
            if re.search(rx, path):
                for c in canaries:
                    add(c, tier, reason, path)
                break
        if files[path]["status"] in ("A", "D") and re.match(r"^(src|include|modules)/", path):
            for c in ("Canary-G3", "Canary-Android", "Canary-Chromium"):
                add(c, "STRONG", "file added/removed, client source lists must follow", path)

    agent_todo = []
    for d in removed_defines(files):
        for canary, where in define_users(d).items():
            add(canary, "STRONG", f"removed guard {d} is still defined in {', '.join(where)}")
        agent_todo.append(f"code_search `\\b{d}\\b -f:third_party/skia/` -> Canary-G3 if any hit")

    removed, changed = api_changes(files)
    api = removed + changed
    if api:
        sample = api[:args.max_symbols]
        for i, (sym, stem) in enumerate(sample):
            if not args.no_search:
                if i:
                    time.sleep(6)  # GitHub code search allows 10 requests/min
                hits = flutter_usage(sym, stem)
                if hits:
                    add("Canary-Flutter", "STRONG",
                        f"public `{stem}::{sym}` changed, {hits} Flutter engine files mention it "
                        "(a local engine build also catches the compile break)")
            plus = "" if sym == stem else f" +{stem}"
            agent_todo.append(f"code_search `\\b{sym}\\b{plus} -f:third_party/skia/` "
                              "-> Canary-G3 if hit")
        if any(p.startswith("include/") for p in files if is_public_header(p)):
            names = ", ".join(f"{st}::{sy}" for sy, st in sample[:5])
            add("Canary-Chromium", "STRONG",
                f"core public API changed ({names}); Chromium is the largest core API user and "
                "its usage cannot be searched from here")

    notes = []
    # LOCAL gates print as AGENT ACTION lines: as a NOTE they were read as advice and skipped
    # (Gerrit 1400176 review got an LGTM draft without the google3 A/B run)
    local_actions = []
    # Route SkParagraph/SkUnicode paths on Cloudtop to the local CitC A/B corpus instead of G3 try
    if "Canary-G3" in recs and has_cloudtop_citc():
        g3_rec = recs["Canary-G3"]
        para_reasons = {}
        other_reasons = {}
        for reason, paths in g3_rec["reasons"].items():
            if not paths:
                other_reasons[reason] = []
                continue
            p_paths = [p for p in paths if G3_PARAGRAPH_PATH_RE.search(p)]
            o_paths = [p for p in paths if not G3_PARAGRAPH_PATH_RE.search(p)]
            if p_paths:
                para_reasons[reason] = p_paths
            if o_paths:
                other_reasons[reason] = o_paths
        if para_reasons:
            recs["LOCAL google3-test-paragraph"] = {
                "tier": "STRONG",
                "reasons": para_reasons,
            }
            if meta["cl"]:
                g3_local_cmd = (
                    f"python3 {G3_PARAGRAPH_CORPUS_SCRIPT} --out /tmp/g3_cl_{meta['cl']} "
                    f"--create-citc baseline=G3_HEAD cand=cl:{meta['cl']}/{meta['patchset']}"
                )
            else:
                g3_local_cmd = (
                    f"python3 {G3_PARAGRAPH_CORPUS_SCRIPT} --out /tmp/g3_local "
                    f"--create-citc --skia-repo {args.repo} baseline=G3_HEAD current=WORKTREE"
                )
            # The advisor runs right before CR+1 or upload, where the slow tier is mandatory
            g3_local_cmd = g3_local_cmd.replace("--create-citc", "--create-citc --tier all", 1)
            notes.append(
                "On Cloudtop, the local google3 A/B corpus replaces Canary-G3 for "
                "SkParagraph/SkUnicode paths (see AGENT ACTION below)"
            )
            local_actions.append(("google3-test-paragraph", g3_local_cmd))
            if other_reasons:
                other_strong = any(
                    files.get(p, {}).get("status") in ("A", "D")
                    or any(
                        re.search(rx, p) and tier == "STRONG" and "Canary-G3" in cs
                        for rx, cs, tier, _ in AREA_RULES
                    )
                    for o_paths in other_reasons.values()
                    for p in o_paths
                ) or any(not o_paths for o_paths in other_reasons.values())
                recs["Canary-G3"] = {
                    "tier": "STRONG" if other_strong else "CONSIDER",
                    "reasons": other_reasons,
                }
            else:
                recs.pop("Canary-G3")

    perf_paths = [p for p in files if PERF_GATE_PATH_RE.search(p) and PERF_GATE_CODE_RE.search(p)]
    if perf_paths:
        recs["LOCAL flutter-engine-perf-compare (txt_benchmarks A/B)"] = {
            "tier": "STRONG",
            "reasons": {"SkParagraph layout speed: correctness corpora do not measure it":
                        perf_paths},
        }
        if meta["cl"]:
            # skia_patch_ab.py diffs a checkout against --base, so the CL needs its own worktree
            wt = f"/tmp/skia_audit_{meta['cl']}"
            perf_cmd = (
                f"(worktree {wt} detached at the patchset) python3 {PERF_GATE_SCRIPT} "
                f"--skia-repo {wt} --base HEAD~1 --out /tmp/skia_patch_ab_{meta['cl']}"
            )
        else:
            perf_cmd = (f"python3 {PERF_GATE_SCRIPT} --skia-repo {args.repo} "
                        "--out /tmp/skia_patch_ab")
        local_actions.append(("flutter-engine-perf-compare", perf_cmd))

    health = (
        {c: canary_health(c) for c in recs if c.startswith("Canary-")}
        if not args.no_health
        else {}
    )
    flutter_disabled = "Canary-Flutter" in DISABLED_CANARIES
    if "Canary-Flutter" in recs and (
            flutter_disabled or health.get("Canary-Flutter", {}).get("healthy") is False):
        flutter = recs.pop("Canary-Flutter")
        if flutter_disabled:
            why = f"Canary-Flutter is disabled ({DISABLED_CANARIES['Canary-Flutter']})"
        else:
            why = ("Canary-Flutter has produced no SUCCESS/FAILURE verdict in the last "
                   f"{HEALTH_WINDOW_DAYS} days ({health['Canary-Flutter']['counts']})")
        notes.append(f"{why}; never trigger it. Run the full local Flutter corpus instead: "
                     f"`{FLUTTER_FULL_CORPUS}`")
        recs["LOCAL flutter-test-paragraph-engine --full"] = {
            "tier": flutter["tier"], "reasons": {r: p for r, p in flutter["reasons"].items()}}
        if flutter["tier"] == "STRONG":
            local_actions.append(("flutter-test-paragraph-engine", FLUTTER_FULL_CORPUS))
    elif "Canary-Flutter" in recs:
        para_paths = [p for p in effective if FLUTTER_PARAGRAPH_PATH_RE.search(p)]
        if para_paths:
            recs["LOCAL flutter-test-paragraph-engine --full"] = {
                "tier": "STRONG",
                "reasons": {"Flutter text corpus A/B: Canary-Flutter does not compare it":
                            para_paths}}
            local_actions.append(("flutter-test-paragraph-engine", FLUTTER_FULL_CORPUS))
    for c, h in health.items():
        if c in recs and not h["healthy"] and c != "Canary-Flutter":
            notes.append(f"{c} looks unhealthy {h['counts']}: a red result may be infra, "
                         "not the CL")

    if args.strong_only:
        recs = {c: r for c, r in recs.items() if r["tier"] == "STRONG"}

    own = meta["owner"] == ME
    external = not meta["owner"].endswith("@google.com")
    googler_cr_plus_one = meta.get("googler_cr_plus_one", False)

    asan_bot, asan_reason = choose_asan_bot(files.keys())
    mac_font_bot, mac_font_reason = needs_mac_native_fonts_bot(files)

    print(f"# Canary advice: {meta['subject']}")
    if meta["cl"]:
        print(f"CL {GERRIT}/c/skia/+/{meta['cl']}/{meta['patchset']}  owner={meta['owner']}"
              f"  ({'own' if own else 'review'}{', external' if external else ''})")
    print(f"files: {len(files)}, client-relevant: {len(effective)}")
    if not recs:
        print("\nNo canary recommended: the diff has no client-visible surface.")
    for c in sorted(recs, key=lambda k: (recs[k]["tier"] != "STRONG", k)):
        print(f"\n## {c} [{recs[c]['tier']}]")
        for reason, paths in recs[c]["reasons"].items():
            shown = ", ".join(paths[:3]) + (f" +{len(paths) - 3} more" if len(paths) > 3 else "")
            print(f"- {reason}" + (f" ({shown})" if paths else ""))
            
    if asan_bot:
        print(f"\n## ASAN [ALWAYS_RUN]")
        print(f"- {asan_reason} -> {asan_bot}")

    if mac_font_bot:
        print(f"\n## Font-Backend Trybot [ALWAYS_RUN]")
        print(f"- {mac_font_reason} -> {mac_font_bot}")

    if external and "Canary-G3" in recs and not googler_cr_plus_one:
        notes.append(
            "Canary-G3 requires a Googler author or Googler Code-Review+1 (g3_canary.go:134); "
            "deferred until a Googler votes Code-Review+1")

    for n in notes:
        print(f"\nNOTE: {n}")
    if agent_todo:
        print("\nAGENT TODO (google3 usage, run before finalizing):")
        for t in agent_todo:
            print(f"- {t}")

    bots_to_run = []
    canary_bots = [c for c in recs if c.startswith("Canary-")]
    
    # Policy: agent ALWAYS triggers ASAN
    if asan_bot:
        bots_to_run.append(asan_bot)

    # Policy: agent ALWAYS triggers Mac NativeFonts trybot for font-backed tests
    if mac_font_bot and mac_font_bot not in bots_to_run:
        bots_to_run.append(mac_font_bot)
        
    # Policy: agent triggers Canary for external authors automatically
    # Canary-G3 requires Googler author or Googler CR+1 approval (g3_canary.go:134)
    if external and canary_bots:
        for b in canary_bots:
            if b == "Canary-G3" and not googler_cr_plus_one:
                continue
            bots_to_run.append(b)

    if bots_to_run or (canary_bots and own) or (canary_bots and external):
        issue = f" -i {meta['cl']}" if meta["cl"] else ""
        # Wrap git cl in luci-auth context so depot_tools uses 7-day LOAS2 instead of 20h CorpSSO
        try_prefix = f"luci-auth context -- git cl try{issue} -B {BUCKET}"

        if bots_to_run:
            cmd = f"{try_prefix} " + " ".join(f"-b {b}" for b in bots_to_run)
            if meta["cl"]:
                # The `try` subcommand prints TRIGGER: VERIFIED only once Buildbucket lists the
                # builds, so a failed `git cl try` can no longer pass for a running bot
                cmd = (f"python3 {os.path.abspath(__file__)} try --cl {meta['cl']} "
                       + " ".join(f"-b {b}" for b in bots_to_run))
            print(f"\nAGENT ACTION (execute this command):")
            print(f"  {cmd}")
            if meta["cl"]:
                script_path = os.path.abspath(__file__)
                print(f"\nAGENT WATCH (launch in background after TRIGGER: VERIFIED, "
                      f"WaitMsBeforeAsync=500, NotificationTimeoutSeconds=5400):")
                print(f"  python3 {script_path} watch --cl {meta['cl']} --save-draft")

        # Provide the exact Canary-G3 command to run once a Googler votes CR+1
        if canary_bots and external and "Canary-G3" in canary_bots and not googler_cr_plus_one:
            cmd = f"{try_prefix} -b Canary-G3"
            print(f"\nCanary-G3 Command (run after Googler Code-Review+1 approval):")
            print(f"  {cmd}")

        if canary_bots and not external and not own:
            # Not external, not own -> draft a comment for the author to trigger canaries
            cmd = f"{try_prefix} " + " ".join(f"-b {b}" for b in canary_bots)
            print(f"\nFor the internal author (draft comment to ask them to run canaries):")
            print(f"  {cmd}")

        if canary_bots and own:
            cmd = f"{try_prefix} " + " ".join(f"-b {b}" for b in canary_bots)
            if not asan_bot:
                print(f"\nCommand (run only after user approval):")
                print(f"  {cmd}")
            else:
                print(f"\nCanary Command (run only after user approval):")
                print(f"  {cmd}")
            if meta["cl"]:
                script_path = os.path.abspath(__file__)
                print("  # Watch running canaries and save drafts if unrelated targets fail:")
                print(f"  python3 {script_path} watch --cl {meta['cl']} --save-draft")
    
    for name, cmd in local_actions:
        print(f"\nAGENT ACTION (LOCAL gate {name}: run in background now; no LGTM, CLEAN verdict, "
              "Code-Review+1 or upload until it finishes with 0 regressions):")
        print(f"  {cmd}")

    if not args.no_log:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        with open(LEDGER, "a") as fh:
            fh.write(json.dumps({
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "cl": meta["cl"], "patchset": meta["patchset"], "own": own, "external": external,
                "googler_cr_plus_one": googler_cr_plus_one,
                "recommended": {c: r["tier"] for c, r in recs.items()},
                "asan_bot": asan_bot,
                "mac_font_bot": mac_font_bot,
            }) + "\n")


def cmd_health(_args):
    for c in CANARIES:
        h = canary_health(c)
        print(f"{c:16} {'OK  ' if h['healthy'] else 'DEAD'} {h['counts']}")


def cmd_stats(_args):
    if not os.path.exists(LEDGER):
        print("no ledger yet")
        return
    latest = {}
    with open(LEDGER) as fh:
        for line in fh:
            e = json.loads(line)
            has_canary_or_asan = any(
                k.startswith("Canary-") for k in e.get("recommended", {})
            ) or e.get("asan_bot")
            if e.get("cl") and has_canary_or_asan:
                latest[e["cl"]] = e
    followed = total = 0
    for cl, e in sorted(latest.items()):
        cur = gerrit_json(f"/changes/{cl}?o=CURRENT_REVISION")
        cur_ps = next(iter(cur["revisions"].values()))["_number"]
        
        # Check canaries
        for canary in (k for k in e.get("recommended", {}) if k.startswith("Canary-")):
            ran = any(bb_builds(canary, n=1, cl=f"{GERRIT}/c/skia/+/{cl}/{ps}")
                      for ps in range(e["patchset"], cur_ps + 1))
            total += 1
            followed += ran
            
            # Note RAN-by-agent if external
            if ran and e.get("external"):
                status = 'RAN-by-agent'
            elif ran:
                status = 'RAN'
            # External CLs cannot run G3 canary until approved by a Googler
            elif canary == "Canary-G3" and e.get("external") and not e.get("googler_cr_plus_one"):
                status = 'waiting-CR+1'
            else:
                status = 'skipped'
            
            print(f"{cl} {'own' if e.get('own') else 'rev'} {canary:16} "
                  f"{e['recommended'][canary]:8} {status}")
                  
        # Check ASAN
        asan_bot = e.get("asan_bot")
        if asan_bot:
            ran = any(bb_builds(asan_bot, n=1, cl=f"{GERRIT}/c/skia/+/{cl}/{ps}")
                      for ps in range(e["patchset"], cur_ps + 1))
            status = 'RAN-by-agent' if ran else 'skipped'
            print(f"{cl} {'own' if e.get('own') else 'rev'} {asan_bot[:25]+'...':25} "
                  f"{'ALWAYS':8} {status}")
                  
    if total:
        print(f"\nfollow rate: {followed}/{total} = {100 * followed // total}%")


def bb_cl_builds(cl, ps):
    cmd = [BB, "ls", "-n", "50", "-json",
           "-fields", "id,builder,status,create_time,start_time,end_time,summary_markdown",
           "-cl", f"{GERRIT}/c/skia/+/{cl}/{ps}"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=90, cwd="/tmp")
    builds = {}
    for line in out.stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            b = json.loads(line)
            builder = b.get("builder", {}).get("builder")
            if not builder:
                continue
            cur = builds.get(builder)
            if not cur or b.get("createTime", "") > cur.get("createTime", ""):
                builds[builder] = b
        except json.JSONDecodeError:
            continue
    return builds


def same_tree_builds(cl, meta, ps):
    """Collect newest Buildbucket builds per builder across all same-tree patchsets code_ps..ps.

    Canaries are often triggered on an intermediate NO_CODE_CHANGE patchset (e.g. PS3) before
    a final Tested: description update (PS4); scanning the full range keeps every tryjob and
    tags each build with `_patchset` so Swarming tasks are looked up on the patchset that ran it.
    """
    ps = int(ps)
    code_ps = int(code_patchset(meta, ps))
    merged = {}
    source_ps = set()
    for cand_ps in range(code_ps, ps + 1):
        ps_builds = bb_cl_builds(cl, cand_ps)
        if ps_builds:
            source_ps.add(cand_ps)
        for builder, b in ps_builds.items():
            annotated = {**b, "_patchset": cand_ps}
            cur = merged.get(builder)
            if not cur or (
                annotated.get("createTime", ""),
                cand_ps,
            ) >= (
                cur.get("createTime", ""),
                cur.get("_patchset", cand_ps),
            ):
                merged[builder] = annotated
    return merged, code_ps, sorted(source_ps)


def _tasks_for_ps(cl, ps, cache):
    """Return Swarming tasks for (cl, ps), memoizing per patchset inside cache."""
    ps = int(ps)
    if ps not in cache:
        cache[ps] = swarming_tasks_for_cl(cl, ps)
    return cache[ps]


def swarming_tasks_for_cl(cl, ps, name=None):
    if not os.path.exists(PRPC):
        return []
    tags = [f"sk_issue:{cl}", f"sk_patchset:{ps}"]
    if name:
        tags.append(f"sk_name:{name}")
    req = {
        "tags": tags,
        "state": "QUERY_ALL",
        "limit": 50,
    }
    proc = subprocess.run([PRPC, "call", "chromium-swarm.appspot.com",
                           "swarming.v2.Tasks.ListTasks"],
                          input=json.dumps(req), capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        # Swarming hides task lists from anonymous callers; the LUCI refresh token expires
        if "anonymous:anonymous" in proc.stderr + proc.stdout:
            sys.exit("ERROR: LUCI is not logged in, Swarming tasks are hidden. "
                     "Run `luci-auth login` and retry")
        return []
    try:
        data = json.loads(proc.stdout)
        return data.get("items", [])
    except json.JSONDecodeError:
        return []


def g3_change_approved(change, patchset):
    # Same rule as IsTryChangeSafe in google3 chrome/skia/autoroller/try_service.py
    change, patchset = int(change), int(patchset)
    detail = gerrit_json(
        f"/changes/{change}/detail?o=ALL_REVISIONS&o=ALL_COMMITS"
        "&o=DETAILED_LABELS&o=DETAILED_ACCOUNTS"
    )
    if detail.get("owner", {}).get("email", "").endswith("@google.com"):
        ref = "refs/changes/%02d/%d/%d" % (change % 100, change, patchset)
        rev = next((r for r in detail.get("revisions", {}).values() if r.get("ref") == ref), {})
        if rev.get("commit", {}).get("author", {}).get("email", "").endswith("@google.com"):
            return True
    votes = detail.get("labels", {}).get("Code-Review", {}).get("all", [])
    return any(v.get("value", 0) == 1 and v.get("email", "").endswith("@google.com")
               for v in votes)


def g3_chain(cl, ps):
    """Returns ordered (change, patchset) chain cherry-picked by Canary-G3 for cl/ps."""
    resp = gerrit_json(f"/changes/{int(cl)}/revisions/{int(ps)}/related")
    related = (resp.get("changes") if isinstance(resp, dict) else None) or []
    parent_of, by_commit = {}, {}
    for c in related:
        if c.get("status") in ("MERGED", "ABANDONED"):
            continue
        key = (int(c["_change_number"]), int(c["_revision_number"]))
        by_commit[c["commit"]["commit"]] = key
        if c["commit"].get("parents"):
            parent_of[key] = c["commit"]["parents"][0]["commit"]
    chain = [(int(cl), int(ps))]
    visited = {chain[0]}
    # The related list is finite and each step moves to a distinct unvisited parent commit
    while len(chain) <= len(related):
        nxt = by_commit.get(parent_of.get(chain[0]))
        if not nxt or nxt in visited:
            break
        visited.add(nxt)
        chain.insert(0, nxt)
    return chain


def unapproved_g3_chain(cl, ps):
    """Returns (change, patchset) pairs that block Canary-G3 for cl/ps.

    The google3 try service cherry-picks every unmerged parent of the change too
    (GetChangesToCherryPick), and each of them needs its own Googler approval
    """
    return [(c, p) for c, p in g3_chain(cl, ps) if not g3_change_approved(c, p)]


def swarming_task_stdout(task_id):
    if not os.path.exists(PRPC) or not task_id:
        return ""
    proc = subprocess.run([PRPC, "call", "chromium-swarm.appspot.com",
                           "swarming.v2.Tasks.GetStdout"],
                          input=json.dumps({"taskId": task_id}),
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        return ""
    try:
        data = json.loads(proc.stdout)
        out_b64 = data.get("output", "")
        return base64.b64decode(out_b64).decode("utf-8", "replace")
    except (json.JSONDecodeError, ValueError):
        return ""


def swarming_task_result(task_id):
    if not os.path.exists(PRPC) or not task_id:
        return {}
    proc = subprocess.run([PRPC, "call", "chromium-swarm.appspot.com",
                           "swarming.v2.Tasks.GetResult"],
                          input=json.dumps({"taskId": task_id}),
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def fetch_logdog_step(task_id, run_id, step):
    run_ids = []
    if run_id:
        run_ids.append(run_id)
    if task_id and len(task_id) > 1:
        # Swarming run IDs end with 1 or 2 for task shards
        run_ids.extend([task_id[:-1] + "1", task_id[:-1] + "2"])
    for rid in run_ids:
        url = f"https://logs.chromium.org/logs/skia/{rid}/+/steps/{step}/0/stdout?format=raw"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "canary-advisor"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError):
            continue
    return ""


def _google3_skia_target_label(g3_skia_root, rel_path):
    """Map a Skia repo-relative path to its nearest enclosing Blaze label in google3 HEAD."""
    pkg_dir = os.path.dirname(rel_path)
    while pkg_dir and not os.path.isfile(os.path.join(g3_skia_root, pkg_dir, "BUILD")):
        pkg_dir = os.path.dirname(pkg_dir)
    if not pkg_dir and not os.path.isfile(os.path.join(g3_skia_root, "BUILD")):
        return None
    pkg_prefix = f"//third_party/skia/HEAD/{pkg_dir}" if pkg_dir else "//third_party/skia/HEAD"
    full = os.path.join(g3_skia_root, rel_path)
    # New files added by the CL or BUILD/BUILD.bazel edits map to all rules in their package
    if not os.path.exists(full) or os.path.basename(rel_path) in ("BUILD", "BUILD.bazel"):
        return f"{pkg_prefix}:all"
    target_rel = os.path.relpath(rel_path, pkg_dir) if pkg_dir else rel_path
    return f"{pkg_prefix}:{target_rel}"


G3_IGNORED_FILE_RE = re.compile(r"(^|/)BUILD\.gn$|\.gni$")


def check_google3_somepath(target, touched_files):
    """Query Blaze `somepath` from `target` to `touched_files`; return (has_path_or_none, lines)."""
    g3_skia_root = os.path.join(GOOGLE3_DIR, "third_party/skia/HEAD")
    if not touched_files or not os.path.isdir(g3_skia_root):
        return None, []
    # Skip Skia unit tests/docs and GN-only files absent from google3 Blaze packages (CL 1421816)
    g3_files = [
        f for f in touched_files
        if not NO_EFFECT.search(f) and not G3_IGNORED_FILE_RE.search(f)
    ]
    if not g3_files:
        return False, []
    labels = []
    for f in g3_files:
        lbl = _google3_skia_target_label(g3_skia_root, f)
        if not lbl:
            return None, []
        if lbl not in labels:
            labels.append(lbl)
    set_expr = " ".join(labels)
    query = f"somepath({target}, set({set_expr}))"
    proc = subprocess.run(
        ["blaze", "query", query], capture_output=True, text=True, timeout=60, cwd=GOOGLE3_DIR
    )
    # Return None on Blaze errors so callers fail closed instead of mistaking errors for empty paths
    if proc.returncode != 0:
        return None, []
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    return bool(lines), lines


# Normalize Buildbucket JSON camelCase (summaryMarkdown) and snake_case (summary_markdown)
def _build_summary_markdown(bb_build):
    if not isinstance(bb_build, dict):
        return ""
    return (bb_build.get("summaryMarkdown") or bb_build.get("summary_markdown") or "").strip()


# Detect Task Scheduler pre-task merge conflicts before inspecting older Swarming tasks
def _not_mergeable_verdict(bb_build):
    summary_md = _build_summary_markdown(bb_build)
    low = summary_md.lower()
    if "not mergeable" in low:
        first_line = summary_md.splitlines()[0].strip()
        return {
            "verdict": "NOT_MERGEABLE",
            "our_part_passed": False,
            "summary": f"{first_line}; author must rebase onto main",
        }
    # Stacked parent CLs landing on main break gclient.py sync --patch-ref before Swarming runs
    if "failed to start job: syncing" in low and "gclient.py sync" in low:
        return {
            "verdict": "NOT_MERGEABLE",
            "our_part_passed": False,
            "summary": (
                "Task Scheduler gclient.py sync failed applying patchset "
                "(merged stack ancestors or rebase conflict); rebase onto main"
            ),
        }
    return None


# Reject Swarming tasks from earlier tryjobs created >60s before bb_build was requested
def _task_predates_build(task, bb_build):
    if not isinstance(task, dict) or not isinstance(bb_build, dict):
        return False
    bb_created = (bb_build.get("createTime") or bb_build.get("create_time") or "").strip()
    # Deduplicated tasks still get a fresh createdTs during bb_build even when completedTs is older
    task_created = (task.get("createdTs") or task.get("completedTs") or "").strip()
    if len(bb_created) < 19 or len(task_created) < 19:
        return False
    try:
        bb_dt = datetime.datetime.fromisoformat(bb_created[:19])
        task_dt = datetime.datetime.fromisoformat(task_created[:19])
    except ValueError:
        return False
    return (bb_dt - task_dt).total_seconds() > 60


# Match a Swarming task for builder while ignoring stale tasks from older tryjobs on the same PS
def _find_builder_task(cl, ps, builder, bb_build, sw_tasks):
    task = next(
        (t for t in sw_tasks
         if builder in t.get("name", "") and not _task_predates_build(t, bb_build)),
        None,
    )
    if not task:
        # The shared per-CL query is capped, so a CQ dry run can push this builder out of it
        task = next(
            (t for t in swarming_tasks_for_cl(cl, ps, name=builder)
             if not _task_predates_build(t, bb_build)),
            None,
        )
    return task


def triage_asan_failure(cl, ps, builder, bb_build, sw_tasks, touched_files):
    # Task Scheduler aborts unmergeable CLs before creating any ASAN Swarming task
    nm = _not_mergeable_verdict(bb_build)
    if nm:
        return {**nm, "name": builder}
    failed_tasks = []
    for t in sw_tasks:
        # Ignore failed Swarming tasks from earlier tryjobs on the same patchset
        if _task_predates_build(t, bb_build):
            continue
        name = t.get("name", "")
        is_asan = "ASAN" in name or (builder and builder in name)
        if not is_asan:
            continue
        state = t.get("state")
        failure = t.get("failure", False)
        if failure or state in ("BOT_DIED", "TIMED_OUT", "NO_RESOURCE", "EXPIRED", "KILLED"):
            failed_tasks.append(t)

    if not failed_tasks and bb_build.get("status") in ("FAILURE", "INFRA_FAILURE") and builder:
        failed_tasks = [
            t for t in sw_tasks
            if builder in t.get("name", "") and not _task_predates_build(t, bb_build)
        ]
        if not failed_tasks:
            # Look past the capped per-CL query: a CQ dry run creates more tasks than it returns
            failed_tasks = [
                t for t in swarming_tasks_for_cl(cl, ps, name=builder)
                if not _task_predates_build(t, bb_build)
            ]

    for t in failed_tasks:
        state = t.get("state")
        if state in ("BOT_DIED", "TIMED_OUT", "NO_RESOURCE", "CANCELED", "EXPIRED", "KILLED"):
            return {
                "verdict": "INFRA_MISHAP", "task_id": t.get("taskId"), "name": t.get("name"),
                "summary": f"Swarming task {t.get('taskId')} failed with state {state}",
            }

    extracted_error = []
    failing_tests = []
    task_name = ""
    target_task_id = ""

    for t in failed_tasks:
        tid = t.get("taskId")
        target_task_id = tid
        task_name = t.get("name", "")
        res = swarming_task_result(tid)
        rid = res.get("runId", "")
        stdout = swarming_task_stdout(tid)

        if "Build-" in task_name or "ninja" in stdout:
            ninja_log = fetch_logdog_step(tid, rid, "ninja")
            log_text = ninja_log or stdout
            for line in log_text.splitlines():
                if any(k in line for k in ("FAILED:", ": error:", ": fatal error:",
                                           "undefined reference to")):
                    extracted_error.append(line.strip())
            if extracted_error:
                return {
                    "verdict": "COMPILE_FAILURE", "task_id": tid, "name": task_name,
                    "errors": extracted_error[:10],
                    "summary": f"ASAN compilation failed in {task_name}",
                }

        steps_to_check = ["symbolized_dm", "dm", "symbolize_stack_trace", "test_skia"]
        step_log = ""
        for s in steps_to_check:
            step_log = fetch_logdog_step(tid, rid, s)
            if step_log:
                break
        test_text = step_log or stdout
        in_asan_block = False
        asan_lines = []
        for line in test_text.splitlines():
            if "ERROR: AddressSanitizer" in line or "ERROR: LeakSanitizer" in line or \
               "ERROR: UndefinedBehaviorSanitizer" in line:
                in_asan_block = True
            if in_asan_block:
                asan_lines.append(line)
                if len(asan_lines) > 25:
                    in_asan_block = False
            m_fail = re.search(r"^\s*Failure in (?:test )?(\w+):", line)
            if m_fail:
                failing_tests.append(m_fail.group(1))

        if asan_lines or failing_tests:
            touches_cl = any(os.path.basename(f) in "\n".join(asan_lines) for f in touched_files)
            return {
                "verdict": "SANITIZER_OR_TEST_FAILURE", "task_id": tid, "name": task_name,
                "sanitizer_stack": asan_lines[:25], "failing_tests": failing_tests,
                "intersects_cl": touches_cl,
                "summary": f"Sanitizer or test failure in {task_name}",
            }

    summary_md = _build_summary_markdown(bb_build)
    return {
        "verdict": "FAILED_UNSPECIFIED", "task_id": target_task_id, "name": task_name or builder,
        "summary": summary_md or "ASAN tryjob failed",
    }


# Internal corporate URLs, shortlinks, hostnames, and target paths forbidden on public Gerrit
PUBLIC_DRAFT_LEAK_RE = re.compile(
    r"(/google/" r"src/|/google_" r"src/|/usr/local/" r"google/home/|//depot/" r"google3\b"
    r"|\b(?:[a-zA-Z0-9-]+\.)*corp\.(?:google\.com|goog)\b"
    r"|\bgoogleplex\b|\bgoto\.google\.com\b"
    r"|(?<![a-zA-Z0-9_./-])(?:https?://)?(?:cl|cr|ag)/\d+\b"
    r"|(?<![a-zA-Z0-9_./-])(?:https?://)?b/\d{5,}\b"
    r"|(?<![a-zA-Z0-9_./-])(?:https?://)?go/[a-z0-9][a-z0-9_-]{2,}\b"
    r"|\bI\d{15,}\b|(?:^|\s)//[a-zA-Z0-9_.-]+/)"
)

SKIA_SYMBOL_RE = re.compile(
    r"\b(?:Sk[A-Z][A-Za-z0-9_]*|Gr[A-Z][A-Za-z0-9_]*|skgpu::|skia::|android::uirenderer::)\b"
)
CHROMIUM_ROLL_URL_RE = re.compile(
    r"https://chromium-review\.googlesource\.com/c/(?:chromium/src/\+/)?(\d+)"
)
ANDROID_ROLL_URL_RE = re.compile(
    r"https://googleplex-android-review\.googlesource\.com/c/"
    r"(?:platform/external/skia/\+/)?(\d+)"
)
FLUTTER_ROLL_URL_RE = re.compile(r"https://github\.com/flutter/flutter/pull/(\d+)")
GITHUB_API = "https://api.github.com"
GITHUB_OK_CONCLUSIONS = ("success", "skipped", "neutral")
TREEHUGGER_SUMMARY_RE = re.compile(
    r"^## Presubmit (?:finished|running):\s*([^\n]+)", re.MULTILINE
)
TREEHUGGER_PASSED_COUNT_RE = re.compile(r"(\d+)\s+passed")
TREEHUGGER_TEST_FAIL_RE = re.compile(
    r"FAIL:\s*Tests failed for \[([^\]]+)\]"
    r"\(https://android-build\.googleplex\.com/builds/tests/view\?invocationId=(I\d+)\)"
    r"(?:\s+on\s+([^\s.\n]+))?(?:\.\s*Caused by\s+([A-Z0-9_]+))?"
)
TREEHUGGER_BUILD_FAIL_RE = re.compile(
    r"FAIL:\s*Failed to build \[([^\]]+)\]"
    r"\(https://android-build\.googleplex\.com/builds/(?:pending|submitted)/"
    r"([A-Za-z0-9_]+)/([^/]+)/latest/view/logs%2Fbuild_error\.log\)"
)
ANDROID_COMPILER_ERROR_RE = re.compile(
    r"^(?:external/[^\s:]+|[A-Za-z0-9_./-]+\.(?:h|hpp|cpp|cc|c|rs)):\d+(?::\d+)?:\s*"
    r"(?:fatal )?error:\s*([^\n]+)",
    re.MULTILINE,
)
ANDROID_MISSING_SYMBOL_RE = re.compile(
    r'CANNOT LINK EXECUTABLE[^\n]*?:\s*cannot locate symbol "([^"]+)"'
)
ANDROID_MANGLED_SKIA_RE = re.compile(
    r"Sk[A-Z]|Gr[A-Z]|\d+(?:skgpu|skia|skparagraph|skottie|skshaper|skunicode|skcms|uirenderer)"
)


def find_public_draft_leak(text):
    """Return the first corporate/internal substring in `text`, or empty string when clean."""
    m = PUBLIC_DRAFT_LEAK_RE.search(text or "")
    return m.group(0).strip() if m else ""


def triage_canary_g3_failure(cl, ps, bb_build, sw_tasks, touched_files):
    """Triage a red Canary-G3 build via skia_gardener and blaze somepath; return verdict dict."""
    # Check Buildbucket summary_markdown first so merge conflicts never fall back to old tasks
    nm = _not_mergeable_verdict(bb_build)
    if nm:
        return nm
    summary_md = _build_summary_markdown(bb_build)
    g3_task = _find_builder_task(cl, ps, "Canary-G3", bb_build, sw_tasks)
    if not g3_task:
        return {
            "verdict": "INFRA_NO_TASK", "our_part_passed": False,
            "summary": summary_md or "Canary-G3 Swarming task not found",
        }
    tid = g3_task.get("taskId")
    stdout = swarming_task_stdout(tid)
    if "changes must be either owned and authored by Googlers" in stdout:
        missing = unapproved_g3_chain(cl, ps)
        if missing:
            listed = ", ".join(f"{c}/{p}" for c, p in missing)
            summary = (
                "google3 try service wants a Googler CR+1 on every unmerged change in the "
                f"chain; missing on: {listed}"
            )
        else:
            # Googler CR+1 landed after this failed tryjob ran, so Canary-G3 can be re-run now
            summary = (
                "Canary-G3 failed before Googler CR+1 was present, but chain is now approved; "
                f"re-trigger: luci-auth context -- git cl try -i {int(cl)} "
                f"-B {BUCKET} -b Canary-G3"
            )
        return {
            "verdict": "MISSING_APPROVAL", "our_part_passed": False, "task_id": tid,
            "missing": missing, "now_approved": not missing,
            "summary": summary,
        }
    m_cl = re.search(r"http://cl/(\d+)", stdout)
    if not m_cl:
        # The CL does not apply to the Skia hash pinned in google3 (google3 lags upstream main)
        if "merge conflict" in stdout:
            return {
                "verdict": "MERGE_CONFLICT_WITH_G3_SKIA", "our_part_passed": False,
                "summary": "The CL does not apply to the Skia hash in google3; "
                           "retry after the google3 Skia roll passes the CL base",
            }
        return {
            "verdict": "INFRA_NO_ROLL_CL", "our_part_passed": False,
            "summary": "Canary-G3 did not produce a google3 roll CL",
        }
    g3_cl = m_cl.group(1)
    if not os.path.isdir(GARDENER_DIR):
        return {
            "verdict": "CANNOT_TRIAGE", "g3_cl": g3_cl, "our_part_passed": False,
            "summary": f"skia_gardener not found at {GARDENER_DIR}",
        }
    proc = subprocess.run(["go", "run", ".", "-triage-google3", "-cl", g3_cl, "-json"],
                          capture_output=True, text=True, timeout=180, cwd=GARDENER_DIR)
    if proc.returncode != 0:
        return {
            "verdict": "GARDENER_ERROR", "g3_cl": g3_cl, "our_part_passed": False,
            "summary": f"skia_gardener failed: {proc.stderr[:200]}",
        }
    try:
        triage_data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "verdict": "GARDENER_PARSE_ERROR", "g3_cl": g3_cl, "our_part_passed": False,
            "summary": "Failed to parse skia_gardener JSON output",
        }

    # skia_gardener -json emits Go field names (Verdict/Runs/Targets); lowercase reads
    # silently saw no runs and reported every G3 failure as a CL regression
    gardener_verdict = triage_data.get("Verdict", triage_data.get("verdict", ""))
    runs = triage_data.get("Runs", triage_data.get("runs", [])) or []
    targets = triage_data.get("Targets", triage_data.get("targets", [])) or []

    live_runs = [r for r in runs if not r.get("superseded")]

    def _check_passed(check_id):
        matched = [r for r in live_runs if r.get("check") == check_id]
        return bool(matched) and all(
            r.get("status") in ("PASSED", "OK", "SUCCESS")
            and int(r.get("failed") or 0) == 0
            and not r.get("setup_error")
            for r in matched
        )

    def _check_settled(check_id):
        matched = [r for r in live_runs if r.get("check") == check_id]
        return bool(matched) and all(
            not r.get("setup_error")
            and (
                (r.get("status") in ("PASSED", "OK", "SUCCESS") and int(r.get("failed") or 0) == 0)
                or (r.get("status") in ("FAILED", "WARNING") and int(r.get("failed") or 0) > 0)
            )
            for r in matched
        )

    skia_tests_passed = _check_passed("CheckTestsSkia")
    all_skia_tap_passed = (
        skia_tests_passed
        and _check_passed("CheckSkiaDependents")
        and _check_passed("CheckTestsSkiaDependents")
    )
    all_skia_tap_settled = (
        skia_tests_passed
        and _check_settled("CheckSkiaDependents")
        and _check_settled("CheckTestsSkiaDependents")
    )
    any_broken_setup = any(
        r.get("status") == "BROKEN" or bool(r.get("setup_error")) for r in live_runs
    )

    if gardener_verdict in ("PRESUBMIT_CONFIG_BROKEN", "INFRA_ONLY") and not skia_tests_passed:
        return {
            "verdict": "INFRA_DID_NOT_RUN", "g3_cl": g3_cl, "our_part_passed": False,
            "summary": "google3 presubmit broken or infra failure before Skia tests passed",
        }

    target_details = []
    all_unrelated = True
    for t in targets:
        if not t.get("failed_in_latest", True):
            continue
        tgt_name = t.get("target", "")
        kind = t.get("kind", "")
        if kind in ("BROKEN_AT_HEAD", "INFRA", "LIKELY_FLAKY"):
            target_details.append({
                "target": tgt_name, "unrelated": True, "reason": kind,
            })
            continue
        has_path, path_lines = check_google3_somepath(tgt_name, touched_files)
        if has_path is False:
            target_details.append({
                "target": tgt_name, "unrelated": True,
                "reason": "somepath empty (no dependency on touched Skia files)",
            })
        elif has_path is None:
            all_unrelated = False
            target_details.append({
                "target": tgt_name, "unrelated": False,
                "reason": "somepath query inconclusive (cannot prove target unrelated at HEAD)",
            })
        else:
            all_unrelated = False
            target_details.append({
                "target": tgt_name, "unrelated": False,
                "reason": f"depends on touched files: {path_lines[0]}",
                "somepath": path_lines[:5],
            })

    # Included non-TAP presubmits (e.g. YouTube iOS buildtest) stay RUNNING after TAP exits;
    # require all three Skia TAP checks to settle with unrelated failures, or all three to pass
    our_part_passed = not any_broken_setup and (
        (all_skia_tap_settled and bool(target_details) and all_unrelated)
        or (all_skia_tap_passed and not target_details)
    )
    verdict = "UNRELATED_HEAD_BREAKAGE" if our_part_passed else "CL_SUSPECTED_REGRESSION"

    sanitized_draft = ""
    if our_part_passed:
        if not target_details:
            sanitized_draft = (
                "Canary-G3 hit a wrapper timeout/infrastructure error after all Skia unit tests "
                "(CheckTestsSkia) and dependent targets passed with 0 failures."
            )
        else:
            sanitized_draft = (
                "Canary-G3 failed only on unrelated downstream targets (already broken at HEAD, "
                "infrastructure errors, or targets with no build dependency on the files modified "
                "in this CL); CheckTestsSkia and all dependent targets exercising the touched "
                "code passed."
            )

    return {
        "verdict": verdict, "g3_cl": g3_cl, "our_part_passed": our_part_passed,
        "skia_tests_ran": skia_tests_passed, "gardener_verdict": gardener_verdict,
        "targets": target_details, "sanitized_draft": sanitized_draft,
    }


def _prpc_json_post(url, payload, timeout=15):
    """POST JSON to a public LUCI pRPC endpoint and strip the optional XSSI prefix."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": "canary-advisor",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(1024 * 1024).decode("utf-8", "replace").strip()
    if raw.startswith(")]}'"):
        raw = raw[4:].lstrip()
    return json.loads(raw) if raw else {}


def _is_bot_update_patch_failure(summary):
    """Return True when a Chromium trybot failed applying DEPS in bot_update due to GoB lag."""
    low = (summary or "").lower()
    return (
        "patch failure" in low
        or "patch_failure" in low
        or "patch error log attached to bot_update" in low
    )


def _extract_chromium_summary_failed_tests(summary):
    """Extract markdown bullet items of failing tests from Buildbucket summaryMarkdown."""
    tests = []
    for line in (summary or "").splitlines():
        s = line.strip()
        if s.startswith(("- ", "* ")):
            item = s[2:].strip()
            if item and not item.startswith(("...", "http")) and item not in tests:
                tests.append(item)
    return tests


def _extract_chromium_non_skia_compile_sig(summary, stems):
    """Return a non-empty target signature only when a Chromium compile failure excludes Skia."""
    low = (summary or "").lower()
    if "step _compile" not in low:
        return ""
    if "skia" in low or SKIA_SYMBOL_RE.search(summary or ""):
        return ""
    if any(st and st.lower() in low for st in stems):
        return ""
    in_fence = False
    code_lines = []
    for raw in (summary or "").splitlines():
        s = raw.strip()
        if s.startswith("```"):
            if in_fence:
                break
            in_fence = True
            continue
        if in_fence and s and not s.startswith("#####"):
            code_lines.append(s)
    return code_lines[0] if code_lines else ""


def _chromium_tests_fail_on_sibling_cl(cr_cl, builder_name, failed_build_id, failed_tests):
    """Check whether `failed_tests` also failed recently on another non-autoroll Chromium CL."""
    if not failed_tests or not builder_name:
        return 0
    short_builder = builder_name.removeprefix("chromium/try/")
    payload = {
        "predicate": {
            "builder": {"project": "chromium", "bucket": "try", "builder": short_builder},
            "status": "FAILURE",
        },
        "pageSize": 20,
        "mask": {"fields": "id,status,summaryMarkdown,input.gerritChanges,tags"},
    }
    try:
        resp = _prpc_json_post(
            "https://cr-buildbucket.appspot.com/prpc/buildbucket.v2.Builds/SearchBuilds", payload
        )
    except (urllib.error.URLError, OSError, ValueError):
        return 0
    for sb in resp.get("builds") or []:
        if sb.get("status") != "FAILURE" or not sb.get("id") or sb.get("id") == failed_build_id:
            continue
        changes = (sb.get("input") or {}).get("gerritChanges") or []
        if not changes:
            continue
        try:
            sib_cl = int(str(changes[0].get("change", "0")).strip('"'))
        except ValueError:
            continue
        if sib_cl <= 0 or sib_cl == int(cr_cl):
            continue
        if any(
            t.get("key") == "cq_cl_owner" and "autoroll" in (t.get("value") or "").lower()
            for t in (sb.get("tags") or [])
        ):
            continue
        sib_tests = set(_extract_chromium_summary_failed_tests(_build_summary_markdown(sb)))
        if sib_tests and all(ft in sib_tests for ft in failed_tests):
            return sib_cl
    return 0


def _classify_chromium_failed_build(cr_cl, b_name, fb, has_independent_infra_peer, stems):
    """Classify one un-cleared Chromium roll build as (is_unrelated, reason_str)."""
    b_id = fb.get("id", "")
    status = fb.get("status", "")
    if status in ("SCHEDULED", "STARTED", "CANCELED"):
        return False, f"{b_name} {status} before completing"
    summary = _build_summary_markdown(fb)
    if _is_bot_update_patch_failure(summary):
        return True, f"{b_name} hit bot_update patch failure"
    # Only clear an early-aborted shard when an independent peer hit bot_update/INFRA_FAILURE
    if (
        status == "FAILURE"
        and has_independent_infra_peer
        and "did a shard fail early?" in summary.lower()
        and "skia" not in summary.lower()
        and not SKIA_SYMBOL_RE.search(summary)
        and not _extract_chromium_summary_failed_tests(summary)
    ):
        return True, f"{b_name} aborted early after sibling infra failure"

    detail = {}
    if b_id:
        try:
            detail = _prpc_json_post(
                "https://cr-buildbucket.appspot.com/prpc/buildbucket.v2.Builds/GetBuild",
                {"id": b_id, "mask": {"fields": "status,summaryMarkdown,steps"}},
            )
        except (urllib.error.URLError, OSError, ValueError):
            detail = {}

    detail_sum = _build_summary_markdown(detail) or summary
    steps = detail.get("steps") or []
    failed_steps = [
        st.get("name", "")
        for st in steps
        if st.get("status") in ("FAILURE", "INFRA_FAILURE")
        and "|" not in st.get("name", "")
        and st.get("name") not in ("lookup_builder_gn_args", "TRYJOB FAILURE")
    ]
    compile_failed = any(
        st.get("name", "").lower().startswith("compile")
        and st.get("status") in ("FAILURE", "INFRA_FAILURE")
        for st in steps
    ) or "step _compile" in detail_sum.lower()

    if "bot_update" in failed_steps or _is_bot_update_patch_failure(detail_sum):
        return True, f"{b_name} hit bot_update patch failure"
    if status == "INFRA_FAILURE" and not compile_failed:
        return True, f"{b_name} hit Buildbucket INFRA_FAILURE"
    if compile_failed:
        # Consumer compile errors in cc/blink omit "skia" in truncated summaries; require trunk
        # compile (without patch) to have failed as well before clearing a compile step
        wo_patch_failed = any(
            st.get("name", "").lower().startswith("compile")
            and "without patch" in st.get("name", "").lower()
            and st.get("status") in ("FAILURE", "INFRA_FAILURE")
            for st in steps
        )
        non_skia_sig = _extract_chromium_non_skia_compile_sig(detail_sum, stems)
        if wo_patch_failed and non_skia_sig:
            return True, f"{b_name} failed compiling unrelated target broken without patch"
        return False, f"{b_name} failed in compile"

    combined_low = (detail_sum + "\n" + "\n".join(failed_steps)).lower()
    touches_stems = any(st and st.lower() in combined_low for st in stems)
    # Browser/WebSocket disconnects can stem from GPU/renderer crashes; only pure ADB transport
    # disconnects without crash/Skia tokens clear without cross-CL reproduction
    adb_disconnect = ("deviceunreachableerror", "adbshellcommandfailederror")
    crash_tokens = ("skia", "crash", "minidump", "sigsegv", "sigabrt", "gpuprocess", "renderer")
    if (
        any(k in combined_low for k in adb_disconnect)
        and not touches_stems
        and not any(tok in combined_low for tok in crash_tokens)
        and not SKIA_SYMBOL_RE.search(detail_sum)
    ):
        return True, f"{b_name} hit an ADB device disconnect"

    failed_tests = _extract_chromium_summary_failed_tests(detail_sum)
    if failed_tests and not touches_stems:
        sib_cl = _chromium_tests_fail_on_sibling_cl(cr_cl, b_name, b_id, failed_tests)
        if sib_cl:
            return (
                True,
                f"{b_name} failed on pre-existing Chromium flake "
                f"(also failing on https://crrev.com/c/{sib_cl})",
            )

    step_desc = ", ".join(failed_steps[:2]) or "test failure"
    return False, f"{b_name} failed ({step_desc})"


def _fetch_chromium_roll_builds(cr_cl):
    """Fetch Buildbucket tryjobs on the newest populated patchset of Chromium roll `cr_cl`."""
    # Check newest patchsets first so PS3 retries are never shadowed by earlier PS2 builds
    for cand_ps in (3, 2, 1):
        try:
            resp = _prpc_json_post(
                "https://cr-buildbucket.appspot.com/prpc/buildbucket.v2.Builds/SearchBuilds",
                {
                    "predicate": {
                        "gerritChanges": [{
                            "host": "chromium-review.googlesource.com",
                            "project": "chromium/src",
                            "change": cr_cl,
                            "patchset": cand_ps,
                        }]
                    },
                    "pageSize": 200,
                    "mask": {"fields": "id,status,summaryMarkdown,builder,createTime"},
                },
            )
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if resp.get("builds"):
            return resp["builds"], cand_ps
    return [], 2


def _partition_chromium_roll_builds(roll_builds):
    """Split Chromium roll builds into (passed_compile_builders, unpassed_latest_by_builder)."""
    passed_all = {
        (b.get("builder") or {}).get("builder")
        for b in roll_builds
        if b.get("status") == "SUCCESS" and (b.get("builder") or {}).get("builder")
    }
    # Exclude non-compiling presubmit/analysis bots so C++ compilation is proven before passing
    passed_compile = {
        name for name in passed_all
        if "presubmit" not in name.lower() and "tricium" not in name.lower()
    }
    unpassed = {}
    for b in roll_builds:
        b_name = (b.get("builder") or {}).get("builder") or ""
        if not b_name or b_name in passed_all:
            continue
        if b.get("status") != "SUCCESS":
            cur = unpassed.get(b_name)
            if not cur or b.get("createTime", "") >= cur.get("createTime", ""):
                unpassed[b_name] = b
    return passed_compile, unpassed


def triage_canary_chromium_failure(cl, ps, bb_build, sw_tasks, touched_files):
    """Verify a red Canary-Chromium build by inspecting CQ trybots on its Chromium roll CL."""
    nm = _not_mergeable_verdict(bb_build)
    if nm:
        return {**nm, "builder": "Canary-Chromium"}
    summary_md = _build_summary_markdown(bb_build)
    task = _find_builder_task(cl, ps, "Canary-Chromium", bb_build, sw_tasks)
    if not task:
        return {
            "verdict": "INFRA_NO_TASK", "builder": "Canary-Chromium", "our_part_passed": False,
            "summary": summary_md or "Canary-Chromium Swarming task not found",
        }
    stdout = swarming_task_stdout(task.get("taskId"))
    m_cr = CHROMIUM_ROLL_URL_RE.search(stdout)
    if not m_cr:
        if "merge conflict" in stdout.lower() or "failed to cherry-pick" in stdout.lower():
            return {
                "verdict": "MERGE_CONFLICT_WITH_CHROMIUM_SKIA",
                "builder": "Canary-Chromium",
                "our_part_passed": False,
                "summary": "The CL does not apply cleanly to Chromium's pinned Skia revision",
            }
        return {
            "verdict": "INFRA_NO_ROLL_CL", "builder": "Canary-Chromium", "our_part_passed": False,
            "summary": "Canary-Chromium did not produce a Chromium roll CL",
        }

    cr_cl = int(m_cr.group(1))
    roll_builds, roll_ps = _fetch_chromium_roll_builds(cr_cl)
    if not roll_builds:
        return {
            "verdict": "CANNOT_TRIAGE", "builder": "Canary-Chromium", "cr_cl": cr_cl,
            "our_part_passed": False,
            "summary": f"No Chromium trybot builds found on https://crrev.com/c/{cr_cl}",
        }

    passed_compile, unpassed = _partition_chromium_roll_builds(roll_builds)
    if not passed_compile:
        return {
            "verdict": "CL_SUSPECTED_REGRESSION", "builder": "Canary-Chromium",
            "cr_cl": cr_cl, "roll_ps": roll_ps, "our_part_passed": False,
            "summary": f"0 compiling Chromium CQ trybots passed on https://crrev.com/c/{cr_cl}/{roll_ps}",
        }
    if not unpassed:
        draft = (
            f"Canary-Chromium exited before CQ completion on roll "
            f"https://crrev.com/c/{cr_cl}/{roll_ps}, where all {len(passed_compile)} "
            f"compiling Chromium CQ trybots passed."
        )
        return {
            "verdict": "UNRELATED_HEAD_BREAKAGE", "builder": "Canary-Chromium",
            "cr_cl": cr_cl, "roll_ps": roll_ps, "passed_count": len(passed_compile),
            "our_part_passed": True, "sanitized_draft": draft,
        }

    stems = [os.path.splitext(os.path.basename(f))[0] for f in touched_files]
    unrelated_reasons, suspect_reasons = [], []
    for b_name in sorted(unpassed):
        has_infra_peer = any(
            other != b_name and (
                peer.get("status") == "INFRA_FAILURE"
                or _is_bot_update_patch_failure(_build_summary_markdown(peer))
            )
            for other, peer in unpassed.items()
        )
        ok, why = _classify_chromium_failed_build(
            cr_cl, b_name, unpassed[b_name], has_infra_peer, stems
        )
        (unrelated_reasons if ok else suspect_reasons).append(why)

    our_part_passed = not suspect_reasons
    verdict = "UNRELATED_HEAD_BREAKAGE" if our_part_passed else "CL_SUSPECTED_REGRESSION"
    sanitized_draft = (
        f"Canary-Chromium roll (https://crrev.com/c/{cr_cl}/{roll_ps}) passed on "
        f"{len(passed_compile)} Chromium CQ trybots; remaining failure(s) were unrelated "
        f"to this CL ({'; '.join(unrelated_reasons)})."
        if our_part_passed else ""
    )
    return {
        "verdict": verdict, "builder": "Canary-Chromium", "cr_cl": cr_cl, "roll_ps": roll_ps,
        "passed_count": len(passed_compile), "our_part_passed": our_part_passed,
        "summary": "; ".join(suspect_reasons or unrelated_reasons),
        "sanitized_draft": sanitized_draft,
    }


def _sso_fetch_json(url, timeout=20):
    """Fetch an internal Gerrit JSON payload via sso_client, returning None when unavailable."""
    sso = shutil.which("sso_client")
    if not sso:
        return None
    try:
        proc = subprocess.run(
            [sso, "--location", f"--url={url}"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    raw = proc.stdout.strip()
    if raw.startswith(")]}'"):
        raw = raw[4:].lstrip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _android_build_api_get(path_and_query, timeout=15):
    """Read an AnTS / Android Build v3 path using a luci-auth androidbuild.internal token."""
    scope = "https://www.googleapis.com/auth/androidbuild.internal"
    try:
        tok_proc = subprocess.run(
            [LUCI_AUTH, "token", "-scopes", scope],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    tok = (tok_proc.stdout or "").strip()
    if tok_proc.returncode != 0 or not tok:
        return ""
    url = "https://androidbuildinternal.googleapis.com/" + path_and_query.lstrip("/")
    req = urllib.request.Request(
        url, headers={"User-Agent": "canary-advisor", "Authorization": f"Bearer {tok}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(512 * 1024).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return ""


def _is_skia_related_android_text(text, stems=()):
    """Return True if an Android log or test record mentions Skia, HWUI, Minikin, or stems."""
    low = (text or "").lower()
    keywords = (
        "external/skia", "libskia", "libhwui", "hwui", "renderengine", "skparagraph",
        "skottie", "skshaper", "skunicode", "skcms", "ctsuirendering", "ctsgraphics",
        "ctstext", "minikin", "typeface", "staticlayout", "pdfium", "surfaceflinger", "skia",
    )
    if any(kw in low for kw in keywords):
        return True
    if any(st and st.lower() in low for st in stems):
        return True
    return bool(SKIA_SYMBOL_RE.search(text or ""))


def _is_skia_related_android_test_failure(tr, stems=()):
    """Evaluate one AnTS testResult record for Skia/HWUI involvement (handling linker errors)."""
    ident = tr.get("testIdentifier") or {}
    dbg = tr.get("debugInfo") or {}
    err_msg = dbg.get("errorMessage") or ""
    m_sym = ANDROID_MISSING_SYMBOL_RE.search(err_msg)
    if m_sym:
        sym = m_sym.group(1)
        return (
            _is_skia_related_android_text(sym, stems)
            or bool(ANDROID_MANGLED_SKIA_RE.search(sym))
        )
    combined = "\n".join([
        ident.get("module") or "",
        ident.get("testClass") or "",
        ident.get("method") or "",
        err_msg,
        dbg.get("trace") or "",
    ])
    return _is_skia_related_android_text(combined, stems)


def _parse_treehugger_verdict(messages):
    """Extract the newest latest-revision TreeHugger verdict from Gerrit `messages`."""
    rev_nums = [int(m.get("_revision_number") or 0) for m in messages if m.get("_revision_number")]
    latest_rev = max(rev_nums) if rev_nums else 0
    passed_count = ""
    test_fails, build_fails = [], []
    retry_passed = False
    has_unparsed_fail = False

    for msg in reversed(messages):
        msg_rev = int(msg.get("_revision_number") or 0)
        if latest_rev and msg_rev and msg_rev != latest_rev:
            continue
        tag = msg.get("tag") or ""
        body = msg.get("message") or ""
        if "TreeHugger" not in tag and "FAIL:" not in body and "Presubmit-Verified" not in body:
            continue
        m_sum = TREEHUGGER_SUMMARY_RE.search(body)
        if m_sum and not passed_count:
            m_cnt = TREEHUGGER_PASSED_COUNT_RE.search(m_sum.group(1))
            if m_cnt:
                passed_count = m_cnt.group(1)
        no_vote_removal = "Removed Presubmit-Verified" not in body
        is_explicit_fail = (
            "Verify-Fail" in tag
            or "TreeHugger:Verify-Fail" in body
            or (no_vote_removal and ("Presubmit-Verified-1" in body or "Presubmit-Verified-2" in body))
            or bool(re.search(r"^## Presubmit finished:[^\n]*\bfailed\b", body, re.I | re.M))
            or bool(re.search(r"^FAIL:", body, re.MULTILINE))
        )
        is_pass = not is_explicit_fail and (
            "Verify-Pass" in tag
            or "TreeHugger:Verify-Pass" in body
            or (no_vote_removal and ("Presubmit-Verified+1" in body or "Presubmit-Verified+2" in body))
            or ("TreeHugger" in tag and "Presubmit finished" in body and "failed" not in body.lower())
        )
        if is_pass:
            retry_passed = True
            break
        if is_explicit_fail:
            for suite, inv_id, dev, caused_by in TREEHUGGER_TEST_FAIL_RE.findall(body):
                test_fails.append((suite.strip(), inv_id.strip(), dev.strip(), caused_by.strip()))
            for label, build_id, target in TREEHUGGER_BUILD_FAIL_RE.findall(body):
                build_fails.append((label.strip(), build_id.strip(), target.strip()))
            total_fail_lines = len(re.findall(r"^FAIL:", body, re.MULTILINE))
            has_unparsed_fail = (
                len(test_fails) + len(build_fails) < total_fail_lines
                or (not test_fails and not build_fails)
            )
            # Never walk backward past a newer Verify-Fail into an older Verify-Pass
            break
    return retry_passed, passed_count, build_fails, test_fails, has_unparsed_fail


def _fetch_android_build_error_log(build_id, target):
    """Fetch `build_error.log` text via `/url` redirect or JSON `signedUrl` payload."""
    enc_bid = urllib.parse.quote(build_id, safe="")
    enc_tgt = urllib.parse.quote(target, safe="")
    raw = _android_build_api_get(
        f"/android/internal/build/v3/builds/{enc_bid}/{enc_tgt}/"
        "attempts/latest/artifacts/logs%2Fbuild_error.log/url"
    )
    if raw.lstrip().startswith("{"):
        try:
            signed_url = (json.loads(raw) or {}).get("signedUrl") or ""
        except json.JSONDecodeError:
            signed_url = ""
        if signed_url.startswith("https://"):
            req = urllib.request.Request(signed_url, headers={"User-Agent": "canary-advisor"})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return resp.read(512 * 1024).decode("utf-8", "replace")
            except (urllib.error.URLError, OSError):
                return ""
    return raw


def _android_build_fails_unrelated(ag_cl, build_fails, stems):
    """Verify every Android build failure in `build_fails` is outside Skia/HWUI/consumers."""
    for label, build_id, target in build_fails:
        log_str = _fetch_android_build_error_log(build_id, target)
        if (
            not log_str
            or not ANDROID_COMPILER_ERROR_RE.search(log_str)
            or _is_skia_related_android_text(log_str, stems)
        ):
            return {
                "verdict": "CL_SUSPECTED_REGRESSION", "builder": "Canary-Android",
                "ag_cl": ag_cl, "our_part_passed": False,
                "summary": f"Android build failure on {label} (ag/{ag_cl})",
            }
    return {
        "verdict": "UNRELATED_HEAD_BREAKAGE", "builder": "Canary-Android", "ag_cl": ag_cl,
        "our_part_passed": True,
        "summary": f"Unrelated non-Skia Android build failure on ag/{ag_cl}",
        "sanitized_draft": (
            "Canary-Android failed to build an unrelated non-Skia platform target; "
            "external/skia compiled with 0 Skia errors."
        ),
    }


def _fetch_ants_failed_tests(inv_id, max_pages=5):
    """Fetch paginated AnTS failed/error test results; return (complete_ok, results)."""
    results = []
    page_token = ""
    enc_inv = urllib.parse.quote(inv_id, safe="")
    for _ in range(max_pages):
        tok_arg = f"&pageToken={urllib.parse.quote(page_token, safe='')}" if page_token else ""
        raw = _android_build_api_get(
            f"/android/internal/build/v3/tests?invocationId={enc_inv}"
            f"&testStatuses=testError&testStatuses=fail&maxResults=50{tok_arg}"
        )
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return False, []
        results.extend(payload.get("testResults") or [])
        page_token = (payload.get("nextPageToken") or "").strip()
        if not page_token:
            return bool(results), results
    # Fail closed if AnTS still has uninspected pages past max_pages
    return False, []


def _android_test_fails_unrelated(ag_cl, test_fails, stems, passed_count, count_note):
    """Verify every TreeHugger test failure is pure infra or has 0 Skia/HWUI/consumer frames."""
    internal_notes = []
    saw_ants_verified = False
    for suite, inv_id, _dev, caused_by in test_fails:
        if "INFRA_ERROR" in caused_by:
            internal_notes.append(f"{suite} ({caused_by})")
            continue
        ok, test_results = _fetch_ants_failed_tests(inv_id)
        if not ok or any(_is_skia_related_android_test_failure(tr, stems) for tr in test_results):
            return {
                "verdict": "CL_SUSPECTED_REGRESSION", "builder": "Canary-Android", "ag_cl": ag_cl,
                "our_part_passed": False,
                "summary": f"Android test failure in {suite} ({inv_id}) on ag/{ag_cl}",
            }
        saw_ants_verified = True
        internal_notes.append(f"{suite} (0 Skia/HWUI frames)")
    if not saw_ants_verified and int(passed_count or 0) <= 0:
        return {
            "verdict": "CL_SUSPECTED_REGRESSION", "builder": "Canary-Android", "ag_cl": ag_cl,
            "our_part_passed": False,
            "summary": f"0 passing Android presubmit targets recorded on ag/{ag_cl}",
        }
    return {
        "verdict": "UNRELATED_HEAD_BREAKAGE", "builder": "Canary-Android", "ag_cl": ag_cl,
        "our_part_passed": True,
        "summary": f"Unrelated Android failure(s) on ag/{ag_cl}: {', '.join(internal_notes)}",
        "sanitized_draft": (
            f"Canary-Android built Skia cleanly ({count_note}) and failed only in unrelated "
            f"non-Skia platform tests/infrastructure with 0 Skia or HWUI failures."
        ),
    }


def triage_canary_android_failure(cl, ps, bb_build, sw_tasks, touched_files):
    """Verify a red Canary-Android build via internal TreeHugger messages and AnTS results."""
    nm = _not_mergeable_verdict(bb_build)
    if nm:
        return {**nm, "builder": "Canary-Android"}
    summary_md = _build_summary_markdown(bb_build)
    task = _find_builder_task(cl, ps, "Canary-Android", bb_build, sw_tasks)
    if not task:
        return {
            "verdict": "INFRA_NO_TASK", "builder": "Canary-Android", "our_part_passed": False,
            "summary": summary_md or "Canary-Android Swarming task not found",
        }
    stdout = swarming_task_stdout(task.get("taskId"))
    m_ag = ANDROID_ROLL_URL_RE.search(stdout)
    if not m_ag:
        if "merge conflict" in stdout.lower() or "failed to cherry-pick" in stdout.lower():
            return {
                "verdict": "MERGE_CONFLICT_WITH_ANDROID_SKIA",
                "builder": "Canary-Android",
                "our_part_passed": False,
                "summary": "The CL does not apply cleanly to Android external/skia",
            }
        return {
            "verdict": "INFRA_NO_ROLL_CL", "builder": "Canary-Android", "our_part_passed": False,
            "summary": "Canary-Android did not produce an Android roll CL",
        }

    ag_cl = int(m_ag.group(1))
    change = _sso_fetch_json(
        f"https://googleplex-android-review.git.corp.google.com/changes/{ag_cl}"
        "?o=MESSAGES&o=LABELS"
    )
    if not isinstance(change, dict):
        return {
            "verdict": "CANNOT_TRIAGE", "builder": "Canary-Android", "ag_cl": ag_cl,
            "our_part_passed": False,
            "summary": f"Could not fetch Android roll CL ag/{ag_cl} via sso_client",
        }

    stems = [os.path.splitext(os.path.basename(f))[0] for f in touched_files]
    retry_passed, passed_count, build_fails, test_fails, unparsed = _parse_treehugger_verdict(
        change.get("messages") or []
    )
    count_note = (
        f"{passed_count} targets passed, 0 build failures" if passed_count else "0 build failures"
    )
    if retry_passed:
        return {
            "verdict": "UNRELATED_HEAD_BREAKAGE", "builder": "Canary-Android", "ag_cl": ag_cl,
            "our_part_passed": True, "summary": f"TreeHugger Verify-Pass on retry ({count_note})",
            "sanitized_draft": (
                f"Canary-Android exited on an initial presubmit flake/infrastructure error, and "
                f"Android presubmit passed on automatic retry ({count_note})."
            ),
        }
    if unparsed:
        return {
            "verdict": "CL_SUSPECTED_REGRESSION", "builder": "Canary-Android", "ag_cl": ag_cl,
            "our_part_passed": False,
            "summary": f"Unparsed TreeHugger presubmit failure on ag/{ag_cl}",
        }
    if not build_fails and not test_fails:
        return {
            "verdict": "CANNOT_TRIAGE", "builder": "Canary-Android", "ag_cl": ag_cl,
            "our_part_passed": False,
            "summary": f"No finalized TreeHugger verdict found on ag/{ag_cl}",
        }
    if build_fails:
        return _android_build_fails_unrelated(ag_cl, build_fails, stems)
    return _android_test_fails_unrelated(ag_cl, test_fails, stems, passed_count, count_note)


def _github_json(path):
    """GET a GitHub REST `path` as JSON.

    Tries `gh api` first (authenticated, higher rate limit), then anonymous urllib against
    api.github.com. Returns the decoded JSON, or None when both fail.
    """
    try:
        out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return json.loads(out.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    req = urllib.request.Request(f"{GITHUB_API}/{path}", headers={
        "User-Agent": "canary-advisor", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _flutter_pr_check_state(pr):
    """Return (head_sha, check_runs, statuses) for flutter/flutter PR `pr`, or None on any fetch
    failure. Walks every check-run page: a roll PR has ~150 check runs, over one 100-item page
    """
    sha = ((_github_json(f"repos/flutter/flutter/pulls/{pr}") or {}).get("head") or {}).get("sha")
    if not sha:
        return None
    runs = []
    for page in range(1, 21):
        data = _github_json(f"repos/flutter/flutter/commits/{sha}/check-runs"
                            f"?per_page=100&page={page}")
        if not isinstance(data, dict):
            return None
        batch = data.get("check_runs") or []
        runs.extend(batch)
        if not batch or len(runs) >= int(data.get("total_count") or 0):
            break
    status = _github_json(f"repos/flutter/flutter/commits/{sha}/status?per_page=100")
    if not isinstance(status, dict):
        return None
    return sha, runs, status.get("statuses") or []


def triage_canary_flutter_failure(cl, ps, bb_build, sw_tasks, _touched_files):
    """Verify a red Canary-Flutter build against the GitHub checks of its flutter/flutter roll PR.

    Returns UNRELATED_HEAD_BREAKAGE only when every check run and commit status except
    `tree-status` finished green and Dashboard Checks, Presubmit (if present) and flutter-gold
    are `success`; dry-run rolls wait for no named check (autoroll codereview/roll.go), so a
    closed tree alone turns the canary red
    """
    nm = _not_mergeable_verdict(bb_build)
    if nm:
        return {**nm, "builder": "Canary-Flutter"}
    base = {"builder": "Canary-Flutter", "our_part_passed": False}
    task = _find_builder_task(cl, ps, "Canary-Flutter", bb_build, sw_tasks)
    if not task:
        return {**base, "verdict": "INFRA_NO_TASK",
                "summary": _build_summary_markdown(bb_build) or
                "Canary-Flutter Swarming task not found"}
    stdout = swarming_task_stdout(task.get("taskId"))
    prs = FLUTTER_ROLL_URL_RE.findall(stdout)
    if not prs:
        return {**base, "verdict": "INFRA_NO_ROLL_CL",
                "summary": ("Canary roll could not be created" if
                            "Canary roll could not be created" in stdout else
                            "Canary-Flutter did not produce a flutter/flutter roll PR")}
    # A retried roll prints several PR URLs; the newest one carries the verdict
    pr = int(prs[-1])
    pr_url = f"https://github.com/flutter/flutter/pull/{pr}"
    base.update({"pr": pr, "pr_url": pr_url})
    state = _flutter_pr_check_state(pr)
    if state is None:
        return {**base, "verdict": "CANNOT_TRIAGE",
                "summary": f"Could not read GitHub checks of {pr_url}"}
    sha, runs, statuses = state
    contexts = {s.get("context"): s.get("state") for s in statuses}
    pending = ([r.get("name") for r in runs if r.get("status") != "completed"] +
               [c for c, st in contexts.items() if c != "tree-status" and st == "pending"])
    if pending:
        return {**base, "verdict": "CANNOT_TRIAGE", "head_sha": sha,
                "summary": f"{len(pending)} checks still running on {pr_url}: "
                           + ", ".join(pending[:5])}
    # flutter-dashboard marks Presubmit `action_required` while it auto-retries LUCI builds and
    # resets it to success if they pass; until then it counts as red (fail closed)
    bad = ([f"{r.get('name')}: {r.get('conclusion')}" for r in runs
            if r.get("conclusion") not in GITHUB_OK_CONCLUSIONS] +
           [f"{c}: {st}" for c, st in contexts.items() if c != "tree-status" and st != "success"])
    if bad:
        return {**base, "verdict": "CL_SUSPECTED_REGRESSION", "head_sha": sha, "failed": bad,
                "summary": f"{len(bad)} red checks on {pr_url}: " + ", ".join(bad[:10])}
    by_name = {r.get("name"): r.get("conclusion") for r in runs}
    gates = ["Dashboard Checks"] + (["Presubmit"] if "Presubmit" in by_name else [])
    missing = [g for g in gates if by_name.get(g) != "success"]
    if contexts.get("flutter-gold") != "success":
        missing.append("flutter-gold")
    if missing:
        return {**base, "verdict": "CANNOT_TRIAGE", "head_sha": sha,
                "summary": f"Required gates not success on {pr_url}: " + ", ".join(missing)}
    if contexts.get("tree-status") in ("failure", "error"):
        cause = "the Flutter tree was closed (tree-status: failure)"
    else:
        cause = "the autoroller reported an error after all checks passed"
    return {
        **base, "verdict": "UNRELATED_HEAD_BREAKAGE", "our_part_passed": True, "head_sha": sha,
        "summary": f"All {len(runs)} check runs green on {pr_url}; red because {cause}",
        "sanitized_draft": (
            f"Canary-Flutter went red only because {cause}; its roll PR {pr_url} passed all "
            f"{len(runs)} check runs, including {', '.join(gates)} and flutter-gold."
        ),
    }


def triage_other_canary_failure(cl, ps, builder, bb_build, sw_tasks, _touched_files):
    """Fail-closed fallback for unknown canary builders whose roll target has no verifier."""
    nm = _not_mergeable_verdict(bb_build)
    if nm:
        return {**nm, "builder": builder}
    task = _find_builder_task(cl, ps, builder, bb_build, sw_tasks)
    summary_md = _build_summary_markdown(bb_build)
    if not task:
        return {
            "verdict": "INFRA_NO_TASK", "builder": builder, "our_part_passed": False,
            "summary": summary_md or f"{builder} Swarming task not found",
        }
    # Never mark a canary UNRELATED_HEAD_BREAKAGE from Swarming stdout alone: canary.go only
    # prints the roll URL and panic line, never downstream compiler/test diagnostics
    return {
        "verdict": "CANNOT_TRIAGE", "builder": builder, "our_part_passed": False,
        "summary": (
            f"{builder} failed (task {task.get('taskId')}); inspect the downstream roll CL "
            "before saving a Gerrit draft"
        ),
    }


# Extracts the dm "Failures:" block (file:line, test name, failed expression) from task stdout
def dm_failure_lines(stdout, limit=20):
    lines = stdout.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() != "Failures:":
            continue
        block = [ln]
        for nxt in lines[i + 1:i + 1 + limit]:
            block.append(nxt.rstrip())
            if re.match(r"^\d+ failures?$", nxt.strip()):
                break
        return block
    return []


def _merge_patchset_draft_notes(old_msg, note_text):
    """Merge per-canary paragraphs into `old_msg`, replacing stale notes for the same canary."""
    paras = [p.strip() for p in (old_msg or "").split("\n\n") if p.strip()]
    for note in [n.strip() for n in (note_text or "").split("\n\n") if n.strip()]:
        if note in paras:
            continue
        m_prefix = re.match(r"^(Canary-[A-Za-z0-9_-]+)\b", note)
        replaced = False
        if m_prefix:
            prefix_re = re.compile(rf"^{re.escape(m_prefix.group(1))}\b")
            for idx, existing in enumerate(paras):
                if prefix_re.match(existing):
                    paras[idx] = note
                    replaced = True
                    break
        if not replaced:
            paras.append(note)
    return "\n\n".join(paras)


def _is_top_level_ps_draft(item):
    """Return True if a Gerrit draft item is a standalone /PATCHSET_LEVEL comment (not a reply)."""
    return (
        not item.get("in_reply_to")
        and not item.get("line")
        and (item.get("side") or "REVISION") == "REVISION"
    )


def save_gerrit_patchset_draft(cl, ps, note_text, unresolved=False):
    """Save or update an unpublished /PATCHSET_LEVEL draft on Gerrit after public leak checks."""
    leak = find_public_draft_leak(note_text)
    if leak:
        print(
            f"ERROR: refusing to save Gerrit draft on CL {cl} PS {ps}: "
            f"internal leak detected ({leak!r})"
        )
        return False
    json_headers = {"Content-Type": "application/json; charset=UTF-8"}
    drafts_url = f"{GERRIT}/a/changes/{cl}/revisions/{ps}/drafts"
    resp_pair, err = _gerrit_authed_urlopen(drafts_url, headers=json_headers, timeout=15)
    if resp_pair is None:
        print(f"WARNING: failed to list Gerrit drafts on CL {cl} PS {ps}: {err}")
        return False
    try:
        raw = resp_pair[1].decode("utf-8", "replace")
        drafts_data = json.loads(raw[4:] if raw.startswith(")]}'") else raw)
    except json.JSONDecodeError as e:
        print(f"WARNING: failed to parse Gerrit drafts on CL {cl} PS {ps}: {e}")
        return False

    existing_ps_draft = next(
        (item for item in (drafts_data.get("/PATCHSET_LEVEL") or []) if _is_top_level_ps_draft(item)),
        None,
    )

    if existing_ps_draft:
        old_msg = existing_ps_draft.get("message", "")
        new_msg = _merge_patchset_draft_notes(old_msg, note_text)
        if new_msg == old_msg.strip():
            return True
        target_url = f"{drafts_url}/{existing_ps_draft['id']}"
        payload = json.dumps({
            "path": "/PATCHSET_LEVEL",
            "message": new_msg,
            "unresolved": existing_ps_draft.get("unresolved", unresolved),
        }).encode("utf-8")
    else:
        target_url = drafts_url
        payload = json.dumps({
            "path": "/PATCHSET_LEVEL",
            "message": note_text,
            "unresolved": unresolved,
        }).encode("utf-8")

    put_pair, put_err = _gerrit_authed_urlopen(
        target_url, data=payload, method="PUT", headers=json_headers, timeout=15
    )
    if put_pair is None:
        print(f"WARNING: failed to save Gerrit draft on CL {cl} PS {ps}: {put_err}")
        return False
    return put_pair[0] in (200, 201)


def has_gerrit_canary_drafts(cl, ps, canary_names):
    """Return True if CL `cl` PS `ps` already has a /PATCHSET_LEVEL draft naming every canary."""
    if not canary_names:
        return True
    drafts_url = f"{GERRIT}/a/changes/{cl}/revisions/{ps}/drafts"
    resp_pair, _err = _gerrit_authed_urlopen(
        drafts_url, headers={"Content-Type": "application/json; charset=UTF-8"}, timeout=15
    )
    if resp_pair is None:
        return False
    try:
        raw = resp_pair[1].decode("utf-8", "replace")
        drafts_data = json.loads(raw[4:] if raw.startswith(")]}'") else raw)
    except json.JSONDecodeError:
        return False
    combined = "\n".join(
        (item.get("message") or "")
        for item in (drafts_data.get("/PATCHSET_LEVEL") or [])
        if _is_top_level_ps_draft(item)
    )
    return all(cb in combined for cb in canary_names)


def cmd_triage(args):
    """Triage ASAN, Canary, and platform trybots across same-tree patchsets and save drafts."""
    meta = load_cl(args.cl, ps=args.ps)
    cl = meta["cl"]
    ps = int(meta["patchset"])
    cur_ps = int(meta.get("current_patchset") or ps)
    files = parse_diff(meta["diff"])
    touched_files = list(files.keys())

    code_ps = int(code_patchset(meta, ps))
    # Standalone /PATCHSET_LEVEL drafts belong on current_patchset when only commit messages
    # changed after `ps`, and same_tree_builds scans through `draft_ps` to keep newer retries
    draft_ps = cur_ps if cur_ps > ps and int(code_patchset(meta, cur_ps)) == code_ps else ps
    builds, code_ps, source_ps = same_tree_builds(cl, meta, draft_ps)
    sw_cache = {}

    results = {
        "cl": cl, "patchset": ps, "draft_patchset": draft_ps, "builds": {}, "drafts_saved": [],
    }
    drafts_to_save = []
    red_unrelated_canaries = []
    manual_proof_canaries = []

    print(f"# Tryjob Triage: CL {cl} PS {ps} ({meta['subject']})\n")
    other_ps = [p for p in source_ps if p != ps]
    if code_ps != ps and other_ps:
        ps_list = ", ".join(f"PS {p}" for p in source_ps)
        print(f"Note: PS {ps} changes only the commit message (NO_CODE_CHANGE); "
              f"using same-tree tryjobs from {ps_list}\n")

    asan_builder = next((b for b in builds if "ASAN" in b), None)
    if asan_builder:
        b_info = builds[asan_builder]
        b_ps = int(b_info.get("_patchset") or ps)
        status = b_info.get("status", "UNKNOWN")
        print(f"## ASAN: {asan_builder} [{status}]")
        if status in ("FAILURE", "INFRA_FAILURE"):
            sw_tasks = _tasks_for_ps(cl, b_ps, sw_cache)
            triage_res = triage_asan_failure(
                cl, b_ps, asan_builder, b_info, sw_tasks, touched_files)
            results["builds"][asan_builder] = triage_res
            print(f"- Verdict: {triage_res['verdict']}")
            print(f"- Summary: {triage_res['summary']}")
            if triage_res.get("errors"):
                print("```")
                for err in triage_res["errors"]:
                    print(err)
                print("```")
            if triage_res.get("sanitizer_stack"):
                print("```")
                for ln in triage_res["sanitizer_stack"]:
                    print(ln)
                print("```")
        else:
            results["builds"][asan_builder] = {"verdict": status}
            print(f"- Verdict: {status}")
    else:
        print("## ASAN: No ASAN build found on this patchset")

    canary_builders = [b for b in builds if b.startswith("Canary-")]
    if canary_builders:
        for cb in canary_builders:
            b_info = builds[cb]
            b_ps = int(b_info.get("_patchset") or ps)
            status = b_info.get("status", "UNKNOWN")
            print(f"\n## Canary: {cb} [{status}]")
            if status in ("FAILURE", "INFRA_FAILURE"):
                sw_tasks = _tasks_for_ps(cl, b_ps, sw_cache)
                if cb == "Canary-G3":
                    triage_res = triage_canary_g3_failure(
                        cl, b_ps, b_info, sw_tasks, touched_files)
                elif cb == "Canary-Chromium":
                    triage_res = triage_canary_chromium_failure(
                        cl, b_ps, b_info, sw_tasks, touched_files)
                elif cb == "Canary-Android":
                    triage_res = triage_canary_android_failure(
                        cl, b_ps, b_info, sw_tasks, touched_files)
                elif cb == "Canary-Flutter":
                    triage_res = triage_canary_flutter_failure(
                        cl, b_ps, b_info, sw_tasks, touched_files)
                else:
                    triage_res = triage_other_canary_failure(
                        cl, b_ps, cb, b_info, sw_tasks, touched_files)
                results["builds"][cb] = triage_res
                print(f"- Verdict: {triage_res['verdict']}")
                if triage_res.get("summary") and not triage_res.get("sanitized_draft"):
                    print(f"- Summary: {triage_res['summary']}")
                if cb == "Canary-G3" and triage_res.get("g3_cl"):
                    print(f"- Roll CL: http://cl/{triage_res['g3_cl']}")
                    print(f"- Skia Tests Ran: {triage_res.get('skia_tests_ran')}")
                    if triage_res.get("targets"):
                        print("- Failing Targets:")
                        for tgt in triage_res["targets"]:
                            print(f"  * {tgt['target']}: {tgt['reason']}")
                elif cb == "Canary-Chromium" and triage_res.get("cr_cl"):
                    roll_ps = triage_res.get("roll_ps") or 1
                    print(f"- Roll CL: https://crrev.com/c/{triage_res['cr_cl']}/{roll_ps}")
                elif cb == "Canary-Android" and triage_res.get("ag_cl"):
                    print(f"- Roll CL: http://ag/{triage_res['ag_cl']}")
                    if triage_res.get("summary") and triage_res.get("sanitized_draft"):
                        print(f"- Internal Triage: {triage_res['summary']}")
                elif cb == "Canary-Flutter" and triage_res.get("pr_url"):
                    print(f"- Roll PR: {triage_res['pr_url']}")
                    if triage_res.get("summary") and triage_res.get("sanitized_draft"):
                        print(f"- Triage: {triage_res['summary']}")
                if triage_res.get("sanitized_draft"):
                    print(f"- Sanitized Draft Message: \"{triage_res['sanitized_draft']}\"")
                    if triage_res.get("verdict") == "UNRELATED_HEAD_BREAKAGE":
                        drafts_to_save.append(triage_res["sanitized_draft"])
                        red_unrelated_canaries.append(cb)
                elif triage_res.get("verdict") not in ("NOT_MERGEABLE", "MISSING_APPROVAL"):
                    manual_proof_canaries.append(cb)
            else:
                results["builds"][cb] = {"verdict": status}
                print(f"- Verdict: {status}")
    else:
        print("\n## Canaries: No Canary builds found on this patchset")

    # Font-backend and other non-ASAN trybots are triggered by `advise` too; a red Mac
    # NativeFonts_Fontations job went unnoticed on Gerrit 1400176 PS2 when only ASAN was checked
    touches_skparagraph = any(f.startswith("modules/skparagraph/") for f in touched_files)
    other_builders = [b for b in builds if "ASAN" not in b and not b.startswith("Canary-")]
    for ob in other_builders:
        b_info = builds[ob]
        b_ps = int(b_info.get("_patchset") or ps)
        status = b_info.get("status", "UNKNOWN")
        print(f"\n## Trybot: {ob} [{status}]")
        # Android NativeFonts bots omit --paragraph_fonts extra_fonts (dm_flags.go:1813), so
        # ResourceFontCollection tests guarded by SKIP_IF_FONTS_NOT_FOUND return early without
        # running (Gerrit 1416501 PS1 on Android_NativeFonts_AndroidNDKFonts)
        skip_note = (
            "Android NativeFonts bots do not mount extra_fonts or pass "
            "--paragraph_fonts extra_fonts (infra/bots/gen_tasks_logic/dm_flags.go:1813, "
            "gen_tasks_logic.go:1737); SkParagraph ResourceFontCollection tests guarded "
            "by SKIP_IF_FONTS_NOT_FOUND return early without running on this bot."
            if ("Android" in ob and "NativeFonts" in ob and touches_skparagraph)
            else ""
        )
        if status not in ("FAILURE", "INFRA_FAILURE"):
            res = {"verdict": status}
            if skip_note:
                res["note"] = skip_note
            results["builds"][ob] = res
            print(f"- Verdict: {status}")
            if skip_note:
                print(f"- Note: {skip_note}")
            continue
        # The stem heuristic in triage_other_canary_failure saw empty stdout here and called a
        # CL-caused dm failure UNRELATED_HEAD_BREAKAGE, so read the dm step log directly instead
        triage_res = _not_mergeable_verdict(b_info) or {}
        failures = []
        if not triage_res:
            sw_tasks = _tasks_for_ps(cl, b_ps, sw_cache)
            task = _find_builder_task(cl, b_ps, ob, b_info, sw_tasks)
            tid = task.get("taskId", "") if task else ""
            rid = swarming_task_result(tid).get("runId", "") if tid else ""
            for step in ("dm", "symbolized_dm", "test_skia"):
                failures = dm_failure_lines(fetch_logdog_step(tid, rid, step)) if tid else []
                if failures:
                    break
            if not failures and tid:
                failures = dm_failure_lines(swarming_task_stdout(tid))
            triage_res = {
                "verdict": "TEST_FAILURE" if failures else "FAILED_UNSPECIFIED",
                "task_id": tid,
                "summary": (f"dm failures in {ob}" if failures else
                            f"{ob} failed; no dm Failures block found (task {tid or 'not found'})"),
            }
        if failures:
            triage_res["failures"] = failures
        if skip_note:
            triage_res["note"] = skip_note
        results["builds"][ob] = triage_res
        print(f"- Verdict: {triage_res['verdict']}")
        print(f"- Summary: {triage_res['summary']}")
        if skip_note:
            print(f"- Note: {skip_note}")
        if failures:
            print("```")
            print("\n".join(failures))
            print("```")

    draft_saved_ok = False
    if args.save_draft and drafts_to_save:
        combined_draft = "\n\n".join(drafts_to_save)
        draft_saved_ok = save_gerrit_patchset_draft(cl, draft_ps, combined_draft, unresolved=False)
        if draft_saved_ok:
            print(f"\n[GERRIT DRAFT SAVED] Saved sanitized draft to CL {cl} PS {draft_ps}")
            results["drafts_saved"].append(combined_draft)
        else:
            print(f"\n[GERRIT DRAFT FAILED] Could not save draft to CL {cl} PS {draft_ps}")

    # Canary triage checks unrelated breakage against the roll; trybot triage cannot, so stays red
    if not results["builds"]:
        print("\nTRYJOBS: NOT GREEN (no tryjobs found)")
    else:
        red = [b for b, r in results["builds"].items()
               if r.get("verdict") != "SUCCESS"
               and not (b.startswith("Canary-") and r.get("verdict") == "UNRELATED_HEAD_BREAKAGE")]
        print("\nTRYJOBS: " + (f"NOT GREEN ({', '.join(sorted(red))})" if red else "ALL GREEN"))
        green = [b for b, r in results["builds"].items() if r.get("verdict") == "SUCCESS"]
        is_open = meta.get("status", "NEW") == "NEW"
        if not red and is_open:
            gaps = tested_block_gaps(cl, draft_ps, green)
            print("TESTED BLOCK: " + (f"STALE (missing: {', '.join(gaps)})" if gaps else "OK"))
            if gaps:
                re_save_hint = (
                    f", and then re-run `python3 {os.path.abspath(__file__)} triage --cl {cl} "
                    "--save-draft` so the red-canary /PATCHSET_LEVEL draft lands on the new PS"
                    if red_unrelated_canaries
                    else ""
                )
                print("AGENT ACTION (update the CL description before the final summary): add "
                      "the trybots and the latest local runs (suites, test counts, OS/arch, "
                      "build type, compiler, font/Unicode backend) plus Not tested: to the "
                      "Tested: block, then `git commit --amend` and `luci-auth context -- git cl "
                      "upload -f --commit-description=+` (a NO_CODE_CHANGE patchset keeps these "
                      f"tryjob results){re_save_hint}")
        if is_open and red_unrelated_canaries:
            has_draft = draft_saved_ok or has_gerrit_canary_drafts(
                cl, draft_ps, red_unrelated_canaries
            )
            if has_draft:
                print(f"CANARY DRAFT: OK (present on PS {draft_ps})")
            else:
                missing_str = ", ".join(red_unrelated_canaries)
                print(f"CANARY DRAFT: MISSING on PS {draft_ps} ({missing_str})")
                print(f"AGENT ACTION (save sanitized red-canary draft on current PS {draft_ps}): "
                      f"python3 {os.path.abspath(__file__)} triage --cl {cl} --save-draft")
        if is_open and manual_proof_canaries:
            listed = ", ".join(manual_proof_canaries)
            print(f"AGENT NOTE ({listed}): if manual stack-trace / roll-CL triage proves our part "
                  f"passed and the failure is unrelated to this CL, you MUST save a sanitized "
                  f"English /PATCHSET_LEVEL Gerrit draft on the final current patchset via "
                  f"`python3 ~/.gemini/config/scripts/gerrit_draft.py save --cl {cl} --resolved "
                  f"...` explaining what passed for our part and why the canary failed.")

    if args.json:
        print(f"\n```json\n{json.dumps(results, indent=2)}\n```")
    return results


def git_cl_try(cl, builders):
    """Run `git cl try` for builders; returns (exit code, combined output)."""
    git_cl = ["git", "cl", "try", "-i", str(cl), "-B", BUCKET]
    for b in builders:
        git_cl += ["-b", b]
    # luci-auth context gives depot_tools the 7-day LOAS2 credential on Cloudtop. The Mac has no
    # luci-auth login, and there git cl falls back to its own credentials
    attempts = [[LUCI_AUTH, "context", "--"] + git_cl, git_cl]
    for cmd in attempts:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=SKIA_DIR,
                              timeout=300)
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0 and "Not logged in" in out and cmd is attempts[0]:
            continue
        return proc.returncode, out
    return proc.returncode, out


def cmd_try(args):
    """Trigger tryjobs and report success only after Buildbucket lists every one of them.

    Incident (Gerrit 1407718 PS18, 2026-11-07): `git cl try` exited 1 with "Unable to determine
    default branch" from a worktree branch without upstream, only depot_tools warnings were shown,
    and the agent reported ASAN as running while no build existed for an hour.
    """
    meta = load_cl(args.cl, ps=args.ps, include_diff=False)
    ps = meta["patchset"]
    # Buildbucket createTime is UTC ISO-8601; a minute of slack covers clock skew
    started = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S")
    rc, out = git_cl_try(args.cl, args.builders)
    if rc != 0:
        lines = [l for l in out.splitlines()
                 if l.strip() and not l.startswith(("|", "+=", "!!!", "---"))]
        print("\n".join(lines[-25:]))
        print(f"\nTRIGGER: FAILED (git cl try exit {rc}; cwd {SKIA_DIR})")
        return 1
    deadline = time.time() + args.verify_sec
    while True:
        builds = bb_cl_builds(args.cl, ps)
        missing = [b for b in args.builders
                   if b not in builds or builds[b].get("createTime", "") < started]
        if not missing:
            for b in args.builders:
                print(f"{b}: {builds[b].get('status')} "
                      f"(https://cr-buildbucket.appspot.com/build/{builds[b].get('id')})")
            print(f"\nTRIGGER: VERIFIED (CL {args.cl} PS {ps}: {len(args.builders)} build(s))")
            return 0
        if time.time() > deadline:
            print(f"\nTRIGGER: FAILED (git cl try exit 0, but Buildbucket has no new build for "
                  f"{', '.join(missing)} on CL {args.cl} PS {ps} after {args.verify_sec}s)")
            return 1
        time.sleep(10)


def cmd_watch(args):
    """Poll Buildbucket and Gerrit across same-tree patchsets until tryjobs finish, then triage."""
    cl = args.cl
    init_meta = load_cl(cl, ps=args.ps)
    ps = init_meta["patchset"]
    poll_sec = args.poll_sec
    timeout_sec = args.timeout_min * 60
    start_time = time.time()

    print(f"# Watching Tryjobs on CL {cl} PS {ps} (poll={poll_sec}s, timeout={args.timeout_min}m)")

    files = parse_diff(init_meta["diff"])
    asan_retried = False

    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout_sec:
            print(f"\nWAKEUP_REASON: TIMEOUT ({args.timeout_min}m elapsed)")
            cmd_triage(args)
            return 1

        # Keep watching across transient Gerrit rate-limit or network errors
        try:
            meta = load_cl(cl, ps=None, include_diff=False)
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"WARNING: transient Gerrit poll error on CL {cl}: {e}; retrying in {poll_sec}s")
            time.sleep(poll_sec)
            continue
        # Exit cleanly once the watched CL merges or is abandoned
        cl_status = str(meta.get("status") or "NEW").upper()
        if cl_status in ("MERGED", "ABANDONED"):
            print(f"\nWAKEUP_REASON: CL_{cl_status}")
            return 0
        poll_meta = meta if meta.get("kinds") else init_meta
        latest_ps = max(int(meta.get("patchset") or 0), int(meta.get("current_patchset") or 0))
        if latest_ps > ps:
            # Keep watching across NO_CODE_CHANGE description updates when --ps was not pinned
            if (
                not args.ps
                and poll_meta.get("kinds")
                and int(code_patchset(poll_meta, latest_ps)) == int(code_patchset(poll_meta, ps))
            ):
                print(f"Note: PS {latest_ps} is NO_CODE_CHANGE; advancing watch PS {ps} -> {latest_ps}")
                ps = latest_ps
                init_meta = {**init_meta, "kinds": poll_meta["kinds"], "current_patchset": ps}
            else:
                print(f"\nWAKEUP_REASON: SUPERSEDED (PS {ps} replaced by PS {latest_ps})")
                return 0

        # Retry on the next tick if Buildbucket, Swarming, or Gerrit RPCs fail transiently
        try:
            builds, _code_ps, _source_ps = same_tree_builds(cl, poll_meta, ps)
            sw_cache = {}

            asan_builder = next((b for b in builds if "ASAN" in b), None)
            if asan_builder:
                b_info = builds[asan_builder]
                b_ps = int(b_info.get("_patchset") or ps)
                status = b_info.get("status")
                if status in ("FAILURE", "INFRA_FAILURE"):
                    touched = list(files.keys())
                    sw_tasks = _tasks_for_ps(cl, b_ps, sw_cache)
                    triage_res = triage_asan_failure(
                        cl, b_ps, asan_builder, b_info, sw_tasks, touched
                    )
                    if triage_res["verdict"] == "INFRA_MISHAP" and not asan_retried:
                        print(f"ASAN infra mishap on {asan_builder}; retrying once")
                        rc, detail = git_cl_try(cl, [asan_builder])
                        if rc != 0:
                            print(f"ERROR: ASAN retry failed (exit {rc}): {detail.strip()}")
                            print(f"\nWAKEUP_REASON: ASAN_RETRY_FAILED (exit {rc})")
                            cmd_triage(args)
                            return 1
                        # Re-poll so terminal check sees the newly scheduled retry build
                        asan_retried = True
                        time.sleep(poll_sec)
                        continue
                    elif triage_res["verdict"] in (
                        "COMPILE_FAILURE", "SANITIZER_OR_TEST_FAILURE", "NOT_MERGEABLE"
                    ):
                        print(f"\nWAKEUP_REASON: ASAN_FAILURE ({triage_res['verdict']})")
                        cmd_triage(args)
                        return 0
        except (
            urllib.error.URLError,
            OSError,
            subprocess.SubprocessError,
            ValueError,
        ) as e:
            print(f"WARNING: transient poll error on CL {cl}: {e}; retrying in {poll_sec}s")
            time.sleep(poll_sec)
            continue

        terminal_statuses = ("SUCCESS", "FAILURE", "INFRA_FAILURE", "CANCELED")
        if builds:
            all_terminal = all(b.get("status") in terminal_statuses for b in builds.values())
            if all_terminal:
                print("\nWAKEUP_REASON: ALL_TRYJOBS_COMPLETE")
                cmd_triage(args)
                return 0
        # A failed trigger leaves nothing to wait for; without this the watcher sat out its whole
        # 90-minute timeout on Gerrit 1407718 PS18
        elif elapsed > args.no_builds_min * 60:
            print(f"\nWAKEUP_REASON: NO_TRYJOBS (none on PS {ps} after {args.no_builds_min}m; "
                  "the trigger failed, re-run `try` and read its TRIGGER: line)")
            return 1

        time.sleep(poll_sec)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("advise")
    a.add_argument("--cl", help="Skia Gerrit change number")
    a.add_argument("--repo", default=".", help="local Skia checkout (when --cl is not given)")
    a.add_argument("--base", default="origin/main")
    a.add_argument("--max-symbols", type=int, default=6)
    a.add_argument("--no-search", action="store_true", help="skip GitHub code search")
    a.add_argument("--no-health", action="store_true", help="skip Buildbucket health check")
    a.add_argument("--no-log", action="store_true", help="do not append to the ledger")
    a.add_argument("--strong-only", action="store_true")
    a.set_defaults(fn=advise)
    sub.add_parser("health").set_defaults(fn=cmd_health)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    t = sub.add_parser("triage")
    t.add_argument("--cl", required=True, help="Skia Gerrit change number")
    t.add_argument("--ps", help="Patchset number (defaults to current)")
    t.add_argument("--save-draft", action="store_true",
                   help="save sanitized draft to Gerrit if Canary is red due to unrelated failures")
    t.add_argument("--json", action="store_true", help="output JSON results")
    t.set_defaults(fn=cmd_triage)
    w = sub.add_parser("watch")
    w.add_argument("--cl", required=True, help="Skia Gerrit change number")
    w.add_argument("--ps", help="Patchset number (defaults to current)")
    w.add_argument("--poll-sec", type=int, default=60, help="Poll interval in seconds")
    w.add_argument("--timeout-min", type=int, default=90, help="Maximum watch time in minutes")
    w.add_argument("--no-builds-min", type=int, default=10,
                   help="exit with NO_TRYJOBS when the patchset has no build after this long")
    w.add_argument("--save-draft", action="store_true",
                   help="save sanitized draft to Gerrit on completion if Canary is red")
    w.add_argument("--json", action="store_true", help="output JSON results")
    w.set_defaults(fn=cmd_watch)
    tr = sub.add_parser("try", help="trigger tryjobs and verify them in Buildbucket")
    tr.add_argument("--cl", required=True, help="Skia Gerrit change number")
    tr.add_argument("--ps", help="Patchset number (defaults to current)")
    tr.add_argument("-b", "--builder", dest="builders", action="append", required=True)
    tr.add_argument("--verify-sec", type=int, default=120,
                    help="how long to wait for Buildbucket to list the new builds")
    tr.set_defaults(fn=cmd_try)
    args = p.parse_args()
    # Subcommands like cmd_triage return result dicts for library callers, not process exit codes
    res = args.fn(args)
    return res if isinstance(res, int) else 0


if __name__ == "__main__":
    sys.exit(main())
