#!/usr/bin/env python3
"""Recommend Skia Canary try jobs (Flutter, G3, Chromium, Android) for a Skia diff or Gerrit CL.

Recommendation-only: prints reasons and a ready `git cl try` command, never triggers builds.
Every run is logged to ~/.gemini/config/ledgers/canary_advisor.jsonl so that `stats` can later
measure how often the recommendation was actually followed.

Usage:
  canary_advisor.py advise --cl 1402416            # Gerrit CL (own or under review)
  canary_advisor.py advise --repo ~/skia [--base origin/main]
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
GARDENER_DIR = os.path.expanduser("~/.gemini/jetski/scratch/skia_gardener")
GOOGLE3_DIR = "/google/src/head/depot/google3"
# `git cl try` needs a git checkout as cwd, whatever directory the watcher was started from
SKIA_DIR = os.path.expanduser("~/.gemini/jetski/scratch/skia")
HEALTH_WINDOW_DAYS = 45

# Canaries that must never be triggered: they run but give no verdict. Remove an entry only after
# the bot produces real SUCCESS/FAILURE results again
DISABLED_CANARIES = {
    "Canary-Flutter": "R38: every run since 2026-08 ends in INFRA_FAILURE at the 3h timeout",
}
FLUTTER_FULL_CORPUS = ("python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/"
                       "run_flutter_paragraph_tests.py --full")
G3_PARAGRAPH_PATH_RE = re.compile(r"^modules/(skparagraph|skunicode)/")
G3_PARAGRAPH_CORPUS_SCRIPT = (
    "~/.gemini/config/skills/google3-test-paragraph/scripts/run_g3_paragraph_tests.py"
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
    }


# Cloudtop mounts /google/src/cloud so google3-test-paragraph can run directly in CitC
def has_cloudtop_citc():
    return os.path.isdir("/google/src/cloud")


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
            notes.append(
                "On Cloudtop, run the local google3 A/B corpus (~10-13 min, 1,139 fast targets "
                "including skia_build_test, zero submit hold) instead of Canary-G3 for "
                f"SkParagraph/SkUnicode paths: `{g3_local_cmd}`"
            )
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
        
        if bots_to_run:
            cmd = f"git cl try{issue} -B {BUCKET} " + " ".join(f"-b {b}" for b in bots_to_run)
            print(f"\nAGENT ACTION (execute this command):")
            print(f"  {cmd}")
            if meta["cl"]:
                script_path = os.path.abspath(__file__)
                print(f"\nAGENT WATCH (launch in background after git cl try, "
                      f"WaitMsBeforeAsync=500, NotificationTimeoutSeconds=5400):")
                print(f"  python3 {script_path} watch --cl {meta['cl']} --save-draft")

        # Provide the exact Canary-G3 command to run once a Googler votes CR+1
        if canary_bots and external and "Canary-G3" in canary_bots and not googler_cr_plus_one:
            cmd = f"git cl try{issue} -B {BUCKET} -b Canary-G3"
            print(f"\nCanary-G3 Command (run after Googler Code-Review+1 approval):")
            print(f"  {cmd}")
            
        if canary_bots and not external and not own:
            # Not external, not own -> draft a comment for the author to trigger canaries
            cmd = f"git cl try{issue} -B {BUCKET} " + " ".join(f"-b {b}" for b in canary_bots)
            print(f"\nFor the internal author (draft comment to ask them to run canaries):")
            print(f"  {cmd}")
            
        if canary_bots and own:
            cmd = f"git cl try{issue} -B {BUCKET} " + " ".join(f"-b {b}" for b in canary_bots)
            if not asan_bot:
                print(f"\nCommand (run only after user approval):")
                print(f"  {cmd}")
            else:
                print(f"\nCanary Command (run only after user approval):")
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


def check_google3_somepath(target, touched_files):
    if not os.path.isdir(GOOGLE3_DIR):
        return False, []
    labels = []
    for f in touched_files:
        full = os.path.join(GOOGLE3_DIR, "third_party/skia/HEAD", f)
        if not os.path.exists(full):
            continue
        d = os.path.dirname(f)
        b = os.path.basename(f)
        label = f"//third_party/skia/HEAD/{d}:{b}" if d else f"//third_party/skia/HEAD:{b}"
        labels.append(label)
    if not labels:
        return False, []
    set_expr = " ".join(labels)
    query = f"somepath({target}, set({set_expr}))"
    cmd = ["blaze", "query", query]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=GOOGLE3_DIR)
    if proc.returncode != 0:
        return False, []
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


def triage_canary_g3_failure(cl, ps, bb_build, sw_tasks, touched_files):
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
                f"Canary-G3 failed before Googler CR+1 was present, but chain is now approved; "
                f"re-trigger: git cl try -i {int(cl)} -B {BUCKET} -b Canary-G3"
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

    gardener_verdict = triage_data.get("verdict", "")
    runs = triage_data.get("runs", [])
    targets = triage_data.get("targets", [])

    skia_tests_ran = any(
        r.get("check") == "CheckTestsSkia" and
        r.get("status") not in ("BROKEN", "PENDING", "RUNNING") and not r.get("setup_error")
        for r in runs
    )

    if gardener_verdict in ("PRESUBMIT_CONFIG_BROKEN", "INFRA_ONLY") and not skia_tests_ran:
        return {
            "verdict": "INFRA_DID_NOT_RUN", "g3_cl": g3_cl, "our_part_passed": False,
            "summary": "google3 presubmit broken or infra failure before Skia tests ran",
        }

    target_details = []
    all_unrelated = True
    for t in targets:
        if not t.get("failed_in_latest", True):
            continue
        tgt_name = t.get("target", "")
        kind = t.get("kind", "")
        if kind == "BROKEN_AT_HEAD":
            target_details.append({
                "target": tgt_name, "unrelated": True, "reason": "BROKEN_AT_HEAD",
            })
            continue
        has_path, path_lines = check_google3_somepath(tgt_name, touched_files)
        if not has_path:
            target_details.append({
                "target": tgt_name, "unrelated": True,
                "reason": "somepath empty (no dependency on touched Skia files)",
            })
        else:
            all_unrelated = False
            target_details.append({
                "target": tgt_name, "unrelated": False,
                "reason": f"depends on touched files: {path_lines[0]}",
                "somepath": path_lines[:5],
            })

    our_part_passed = skia_tests_ran and (all_unrelated or not target_details)
    verdict = "UNRELATED_HEAD_BREAKAGE" if our_part_passed else "CL_SUSPECTED_REGRESSION"

    sanitized_draft = ""
    if our_part_passed:
        sanitized_draft = (
            "Canary-G3 failed on unrelated google3 targets that are already broken at HEAD "
            "or do not depend on the files modified in this CL. All Skia tests "
            "(CheckTestsSkia) and dependent targets exercising the touched code passed."
        )

    return {
        "verdict": verdict, "g3_cl": g3_cl, "our_part_passed": our_part_passed,
        "skia_tests_ran": skia_tests_ran, "gardener_verdict": gardener_verdict,
        "targets": target_details, "sanitized_draft": sanitized_draft,
    }


def triage_other_canary_failure(cl, ps, builder, bb_build, sw_tasks, touched_files):
    # Avoid classifying a pre-task merge conflict or startup error as UNRELATED_HEAD_BREAKAGE
    nm = _not_mergeable_verdict(bb_build)
    if nm:
        return {**nm, "builder": builder}
    task = _find_builder_task(cl, ps, builder, bb_build, sw_tasks)
    summary_md = _build_summary_markdown(bb_build)
    if not task and summary_md:
        return {
            "verdict": "INFRA_NO_TASK", "builder": builder, "our_part_passed": False,
            "summary": summary_md,
        }
    tid = task.get("taskId") if task else ""
    stdout = swarming_task_stdout(tid) if tid else ""
    stems = [os.path.splitext(os.path.basename(f))[0] for f in touched_files]
    mentions_cl = any(st in stdout for st in stems) if stems else False

    if not mentions_cl:
        return {
            "verdict": "UNRELATED_HEAD_BREAKAGE", "builder": builder,
            "our_part_passed": True,
            "summary": f"{builder} failed without mentioning modified Skia files",
            "sanitized_draft": (
                f"{builder} failed on unrelated upstream targets outside the files "
                "modified in this CL."
            ),
        }
    return {
        "verdict": "CL_SUSPECTED_REGRESSION", "builder": builder,
        "our_part_passed": False,
        "summary": f"{builder} failure intersects modified Skia files or symbols",
    }


def save_gerrit_patchset_draft(cl, ps, note_text, unresolved=False):
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

    existing_ps_draft = None
    for item in drafts_data.get("/PATCHSET_LEVEL", []):
        existing_ps_draft = item
        break

    if existing_ps_draft:
        old_msg = existing_ps_draft.get("message", "")
        if note_text in old_msg:
            return True
        new_msg = f"{old_msg}\n\n{note_text}".strip()
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


def cmd_triage(args):
    meta = load_cl(args.cl, ps=args.ps)
    cl = meta["cl"]
    ps = meta["patchset"]
    files = parse_diff(meta["diff"])
    touched_files = list(files.keys())

    builds = bb_cl_builds(cl, ps)
    sw_tasks = swarming_tasks_for_cl(cl, ps)

    results = {"cl": cl, "patchset": ps, "builds": {}, "drafts_saved": []}
    drafts_to_save = []

    print(f"# Tryjob Triage: CL {cl} PS {ps} ({meta['subject']})\n")

    asan_builder = next((b for b in builds if "ASAN" in b), None)
    if asan_builder:
        b_info = builds[asan_builder]
        status = b_info.get("status", "UNKNOWN")
        print(f"## ASAN: {asan_builder} [{status}]")
        if status in ("FAILURE", "INFRA_FAILURE"):
            triage_res = triage_asan_failure(
                cl, ps, asan_builder, b_info, sw_tasks, touched_files)
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
            status = b_info.get("status", "UNKNOWN")
            print(f"\n## Canary: {cb} [{status}]")
            if status in ("FAILURE", "INFRA_FAILURE"):
                if cb == "Canary-G3":
                    triage_res = triage_canary_g3_failure(
                        cl, ps, b_info, sw_tasks, touched_files)
                else:
                    triage_res = triage_other_canary_failure(
                        cl, ps, cb, b_info, sw_tasks, touched_files)
                results["builds"][cb] = triage_res
                print(f"- Verdict: {triage_res['verdict']}")
                # Surface summary on pre-roll verdicts (NOT_MERGEABLE, MISSING_APPROVAL, INFRA_*)
                if triage_res.get("summary") and not triage_res.get("sanitized_draft"):
                    print(f"- Summary: {triage_res['summary']}")
                if cb == "Canary-G3" and triage_res.get("g3_cl"):
                    print(f"- Roll CL: http://cl/{triage_res['g3_cl']}")
                    print(f"- Skia Tests Ran: {triage_res.get('skia_tests_ran')}")
                    if triage_res.get("targets"):
                        print("- Failing Targets:")
                        for tgt in triage_res["targets"]:
                            print(f"  * {tgt['target']}: {tgt['reason']}")
                if triage_res.get("sanitized_draft"):
                    print(f"- Sanitized Draft Message: \"{triage_res['sanitized_draft']}\"")
                    if triage_res.get("verdict") == "UNRELATED_HEAD_BREAKAGE":
                        drafts_to_save.append(triage_res["sanitized_draft"])
            else:
                results["builds"][cb] = {"verdict": status}
                print(f"- Verdict: {status}")
    else:
        print("\n## Canaries: No Canary builds found on this patchset")

    if args.save_draft and drafts_to_save:
        combined_draft = "\n\n".join(drafts_to_save)
        ok = save_gerrit_patchset_draft(cl, ps, combined_draft, unresolved=False)
        if ok:
            print(f"\n[GERRIT DRAFT SAVED] Saved sanitized draft to CL {cl} PS {ps}")
            results["drafts_saved"].append(combined_draft)
        else:
            print(f"\n[GERRIT DRAFT FAILED] Could not save draft to CL {cl} PS {ps}")

    if args.json:
        print(f"\n```json\n{json.dumps(results, indent=2)}\n```")
    return results


def cmd_watch(args):
    cl = args.cl
    meta = load_cl(cl, ps=args.ps)
    ps = meta["patchset"]
    poll_sec = args.poll_sec
    timeout_sec = args.timeout_min * 60
    start_time = time.time()

    print(f"# Watching Tryjobs on CL {cl} PS {ps} (poll={poll_sec}s, timeout={args.timeout_min}m)")

    files = parse_diff(meta["diff"])
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
        latest_ps = max(int(meta.get("patchset") or 0), int(meta.get("current_patchset") or 0))
        if latest_ps > ps:
            print(f"\nWAKEUP_REASON: SUPERSEDED (PS {ps} replaced by PS {latest_ps})")
            return 0

        # Retry on the next tick if Buildbucket, Swarming, or Gerrit RPCs fail transiently
        try:
            builds = bb_cl_builds(cl, ps)
            sw_tasks = swarming_tasks_for_cl(cl, ps)

            asan_builder = next((b for b in builds if "ASAN" in b), None)
            if asan_builder:
                b_info = builds[asan_builder]
                status = b_info.get("status")
                if status in ("FAILURE", "INFRA_FAILURE"):
                    touched = list(files.keys())
                    triage_res = triage_asan_failure(
                        cl, ps, asan_builder, b_info, sw_tasks, touched
                    )
                    if triage_res["verdict"] == "INFRA_MISHAP" and not asan_retried:
                        print(f"ASAN infra mishap on {asan_builder}; retrying once")
                        cmd = [
                            "git", "cl", "try", "-i", str(cl), "-B", BUCKET, "-b", asan_builder,
                        ]
                        subprocess.run(cmd, check=False, cwd=SKIA_DIR)
                        asan_retried = True
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
    w.add_argument("--save-draft", action="store_true",
                   help="save sanitized draft to Gerrit on completion if Canary is red")
    w.add_argument("--json", action="store_true", help="output JSON results")
    w.set_defaults(fn=cmd_watch)
    args = p.parse_args()
    # Subcommands like cmd_triage return result dicts for library callers, not process exit codes
    res = args.fn(args)
    return res if isinstance(res, int) else 0


if __name__ == "__main__":
    sys.exit(main())
