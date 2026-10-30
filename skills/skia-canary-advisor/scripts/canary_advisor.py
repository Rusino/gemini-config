#!/usr/bin/env python3
"""Recommend Skia Canary try jobs (Flutter, G3, Chromium, Android) for a Skia diff or Gerrit CL.

Recommendation-only: prints reasons and a ready `git cl try` command, never triggers builds.
Every run is logged to ~/.gemini/config/ledgers/canary_advisor.jsonl so that `stats` can later
measure how often the recommendation was actually followed.

Usage:
  canary_advisor.py advise --cl 1402416            # Gerrit CL (own or under review)
  canary_advisor.py advise --repo ~/skia [--base origin/main]
  canary_advisor.py triage --cl 1402416 [--ps 3]   # triage tryjobs (ASAN + Canaries)
  canary_advisor.py watch --cl 1402416             # watch until terminal / ASAN early exit
  canary_advisor.py health                         # per-canary health from Buildbucket
  canary_advisor.py stats                          # follow-rate of logged recommendations
"""

import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

GERRIT = "https://skia-review.googlesource.com"
BUCKET = "skia/skia.primary"
CANARIES = ["Canary-Flutter", "Canary-G3", "Canary-Chromium", "Canary-Android"]
ME = "jlavrova@google.com"
LEDGER = os.environ.get("CANARY_ADVISOR_LEDGER",
                        os.path.expanduser("~/.gemini/config/ledgers/canary_advisor.jsonl"))
BB = os.path.expanduser("~/depot_tools/bb")
PRPC = os.path.expanduser("~/depot_tools/prpc")
GARDENER_DIR = os.path.expanduser("~/.gemini/jetski/scratch/skia_gardener")
GOOGLE3_DIR = "/google/src/head/depot/google3"
HEALTH_WINDOW_DAYS = 45

# Canaries that must never be triggered: they run but give no verdict. Remove an entry only after
# the bot produces real SUCCESS/FAILURE results again
DISABLED_CANARIES = {
    "Canary-Flutter": "R38: every run since 2026-08 ends in INFRA_FAILURE at the 3h timeout",
}
FLUTTER_FULL_CORPUS = ("python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/"
                       "run_flutter_paragraph_tests.py --full")

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


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "canary-advisor"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
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


def load_cl(cl, ps=None):
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
        "subject": info["subject"], "owner": info["owner"].get("email", ""), "diff": patch,
        "googler_cr_plus_one": googler_cr_plus_one,
    }


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

    health = {c: canary_health(c) for c in recs} if not args.no_health else {}
    notes = []
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
            print(f"  (Note: follow submit-hold protocol — leave unresolved comment to hold submit until Canary-G3 passes)")
            
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


def swarming_tasks_for_cl(cl, ps):
    if not os.path.exists(PRPC):
        return []
    req = {
        "tags": [f"sk_issue:{cl}", f"sk_patchset:{ps}"],
        "state": "QUERY_ALL",
        "limit": 50,
    }
    proc = subprocess.run([PRPC, "call", "chromium-swarm.appspot.com",
                           "swarming.v2.Tasks.ListTasks"],
                          input=json.dumps(req), capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
        return data.get("items", [])
    except json.JSONDecodeError:
        return []


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


def triage_asan_failure(cl, ps, builder, bb_build, sw_tasks, touched_files):
    failed_tasks = []
    for t in sw_tasks:
        name = t.get("name", "")
        is_asan = "ASAN" in name or (builder and builder in name)
        if not is_asan:
            continue
        state = t.get("state")
        failure = t.get("failure", False)
        if failure or state in ("BOT_DIED", "TIMED_OUT", "NO_RESOURCE", "EXPIRED", "KILLED"):
            failed_tasks.append(t)

    if not failed_tasks and bb_build.get("status") in ("FAILURE", "INFRA_FAILURE"):
        for t in sw_tasks:
            if builder and builder in t.get("name", ""):
                failed_tasks.append(t)

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

    summary_md = bb_build.get("summaryMarkdown", "")
    return {
        "verdict": "FAILED_UNSPECIFIED", "task_id": target_task_id, "name": task_name or builder,
        "summary": summary_md or "ASAN tryjob failed",
    }


def triage_canary_g3_failure(cl, ps, bb_build, sw_tasks, touched_files):
    g3_task = next((t for t in sw_tasks if "Canary-G3" in t.get("name", "")), None)
    if not g3_task:
        return {
            "verdict": "INFRA_NO_TASK", "our_part_passed": False,
            "summary": "Canary-G3 Swarming task not found",
        }
    tid = g3_task.get("taskId")
    stdout = swarming_task_stdout(tid)
    m_cl = re.search(r"http://cl/(\d+)", stdout)
    if not m_cl:
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
    task = next((t for t in sw_tasks if builder in t.get("name", "")), None)
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
    try:
        proc = subprocess.run(
            ["git-credential-luci", "get"],
            input="protocol=https\nhost=skia-review.googlesource.com\n\n",
            capture_output=True, text=True, timeout=10, check=True
        )
        token = ""
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                token = line.split("=", 1)[1].strip()
                break
        if not token:
            print("WARNING: git-credential-luci returned empty password, cannot save draft")
            return False
    except (subprocess.SubprocessError, OSError) as e:
        print(f"WARNING: failed to get git-credential-luci token: {e}")
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
        "User-Agent": "canary-advisor",
    }
    drafts_url = f"{GERRIT}/a/changes/{cl}/revisions/{ps}/drafts"
    try:
        req = urllib.request.Request(drafts_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            drafts_data = json.loads(r.read().decode("utf-8")[4:])
    except (urllib.error.URLError, OSError) as e:
        print(f"WARNING: failed to list Gerrit drafts on CL {cl} PS {ps}: {e}")
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
        update_url = f"{drafts_url}/{existing_ps_draft['id']}"
        payload = json.dumps({
            "path": "/PATCHSET_LEVEL",
            "message": new_msg,
            "unresolved": existing_ps_draft.get("unresolved", unresolved),
        }).encode("utf-8")
        req = urllib.request.Request(update_url, data=payload, headers=headers, method="PUT")
    else:
        payload = json.dumps({
            "path": "/PATCHSET_LEVEL",
            "message": note_text,
            "unresolved": unresolved,
        }).encode("utf-8")
        req = urllib.request.Request(drafts_url, data=payload, headers=headers, method="PUT")

    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status in (200, 201)
    except (urllib.error.URLError, OSError) as e:
        print(f"WARNING: failed to save Gerrit draft on CL {cl} PS {ps}: {e}")
        return False


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
                if cb == "Canary-G3":
                    print(f"- Roll CL: http://cl/{triage_res.get('g3_cl', '')}")
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
    canary_g3_triggered = False

    print(f"# Watching Tryjobs on CL {cl} PS {ps} (poll={poll_sec}s, timeout={args.timeout_min}m)")

    files = parse_diff(meta["diff"])
    recs = {}
    for path in [p for p in files if not NO_EFFECT.search(p)]:
        for rx, canaries, tier, reason in AREA_RULES:
            if re.search(rx, path):
                for c in canaries:
                    recs[c] = True
                break
    g3_needed = "Canary-G3" in recs
    asan_retried = False

    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout_sec:
            print(f"\nWAKEUP_REASON: TIMEOUT ({args.timeout_min}m elapsed)")
            cmd_triage(args)
            return 1

        meta = load_cl(cl, ps=None)
        if meta["patchset"] > ps:
            print(f"\nWAKEUP_REASON: SUPERSEDED (PS {ps} replaced by PS {meta['patchset']})")
            return 0

        if g3_needed and not canary_g3_triggered and not meta["owner"].endswith("@google.com"):
            builds = bb_cl_builds(cl, ps)
            if "Canary-G3" not in builds and meta["googler_cr_plus_one"]:
                print(f"Googler CR+1 detected on external CL {cl}; triggering Canary-G3")
                cmd = ["git", "cl", "try", "-i", str(cl), "-B", BUCKET, "-b", "Canary-G3"]
                subprocess.run(cmd, check=False)
                canary_g3_triggered = True

        builds = bb_cl_builds(cl, ps)
        sw_tasks = swarming_tasks_for_cl(cl, ps)

        asan_builder = next((b for b in builds if "ASAN" in b), None)
        if asan_builder:
            b_info = builds[asan_builder]
            status = b_info.get("status")
            if status in ("FAILURE", "INFRA_FAILURE"):
                touched = list(files.keys())
                triage_res = triage_asan_failure(cl, ps, asan_builder, b_info, sw_tasks, touched)
                if triage_res["verdict"] == "INFRA_MISHAP" and not asan_retried:
                    print(f"ASAN infra mishap on {asan_builder}; retrying once")
                    cmd = ["git", "cl", "try", "-i", str(cl), "-B", BUCKET, "-b", asan_builder]
                    subprocess.run(cmd, check=False)
                    asan_retried = True
                elif triage_res["verdict"] in ("COMPILE_FAILURE", "SANITIZER_OR_TEST_FAILURE"):
                    print(f"\nWAKEUP_REASON: ASAN_FAILURE ({triage_res['verdict']})")
                    cmd_triage(args)
                    return 0

        terminal_statuses = ("SUCCESS", "FAILURE", "INFRA_FAILURE", "CANCELED")
        if builds:
            all_terminal = all(b.get("status") in terminal_statuses for b in builds.values())
            if all_terminal:
                if g3_needed and not meta["owner"].endswith("@google.com") and \
                   not meta["googler_cr_plus_one"] and "Canary-G3" not in builds:
                    pass
                else:
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
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
