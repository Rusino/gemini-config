#!/usr/bin/env python3
"""Recommend Skia Canary try jobs (Flutter, G3, Chromium, Android) for a Skia diff or Gerrit CL.

Recommendation-only: prints reasons and a ready `git cl try` command, never triggers builds.
Every run is logged to ~/.gemini/config/ledgers/canary_advisor.jsonl so that `stats` can later
measure how often the recommendation was actually followed.

Usage:
  canary_advisor.py advise --cl 1402416            # Gerrit CL (own or under review)
  canary_advisor.py advise --repo ~/skia [--base origin/main]
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
HEALTH_WINDOW_DAYS = 45

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


def load_cl(cl):
    info = gerrit_json(f"/changes/{cl}?o=DETAILED_ACCOUNTS&o=CURRENT_REVISION")
    rev = next(iter(info["revisions"].values()))
    patch = base64.b64decode(http_get(f"{GERRIT}/changes/{cl}/revisions/current/patch")).decode(
        "utf-8", "replace")
    return {
        "cl": int(cl), "patchset": rev["_number"], "subject": info["subject"],
        "owner": info["owner"].get("email", ""), "diff": patch,
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
    if "Canary-Flutter" in recs and health.get("Canary-Flutter", {}).get("healthy") is False:
        flutter = recs.pop("Canary-Flutter")
        notes.append(
            "Canary-Flutter has produced no SUCCESS/FAILURE verdict in the last "
            f"{HEALTH_WINDOW_DAYS} days ({health['Canary-Flutter']['counts']}); substituting "
            "local `flutter-test-paragraph-engine` + Canary-G3")
        add("Canary-G3", flutter["tier"], "substitute for unhealthy Canary-Flutter")
        recs["LOCAL flutter-test-paragraph-engine"] = {
            "tier": flutter["tier"], "reasons": {r: p for r, p in flutter["reasons"].items()}}
    for c, h in health.items():
        if c in recs and not h["healthy"] and c != "Canary-Flutter":
            notes.append(f"{c} looks unhealthy {h['counts']}: a red result may be infra, "
                         "not the CL")

    if args.strong_only:
        recs = {c: r for c, r in recs.items() if r["tier"] == "STRONG"}

    own = meta["owner"] == ME
    external = not meta["owner"].endswith("@google.com")

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
    if external and canary_bots:
        bots_to_run.extend(canary_bots)

    if bots_to_run or (canary_bots and own):
        issue = f" -i {meta['cl']}" if meta["cl"] else ""
        
        if bots_to_run:
            cmd = f"git cl try{issue} -B {BUCKET} " + " ".join(f"-b {b}" for b in bots_to_run)
            print(f"\nAGENT ACTION (execute this command):")
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
                # If asan_bot is present, it's already in AGENT ACTION, but we need to prompt the user to run canaries too.
                # Actually, wait. For own CL, does the agent run ASAN automatically?
                # "ASAN try job агент запускает САМ (git cl try), не просит автора. Касается и своих CL, и CL на ревью" -> YES.
                # But canaries for own CL still require user approval?
                # "Для внутренних авторов (Googlers) канарейки остаются как сейчас." -> YES.
                print(f"\nCanary Command (run only after user approval):")
                print(f"  {cmd}")
    
    if not args.no_log:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        with open(LEDGER, "a") as fh:
            fh.write(json.dumps({
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "cl": meta["cl"], "patchset": meta["patchset"], "own": own, "external": external,
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
            if e.get("cl") and (any(k.startswith("Canary-") for k in e.get("recommended", {})) or e.get("asan_bot")):
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
            status = 'RAN-by-agent' if ran and e.get("external") else ('RAN' if ran else 'skipped')
            
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
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
