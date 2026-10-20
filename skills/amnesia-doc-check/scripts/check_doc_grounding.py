#!/usr/bin/env python3
"""Deterministic pre-check for outgoing technical documents and messages.

Scans markdown/text or Google Drive JSON dumps for:
1. Leftover LLM scaffolding, local-machine/ephemeral path leaks (/tmp, /usr/local/google/home,
   CitC /google/src/cloud, localhost, conversation://), and LLM-slop giveaway vocabulary
2. Intra-document consistency issues (conflicting paths for the same filename within
   the same doc, broken #heading anchors)
3. Non-existent file/directory paths (both backticked and bare prose paths) against
   local repositories, with same-basename suggestions
4. Unresolved skill names (e.g. kebab-case names in skill lists/audits that do not
   match any SKILL.md directory in the repository)
5. Invalid git commit SHAs and inactive/nonexistent @google.com LDAPs cited in the text
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


PLACEHOLDER_PATTERNS = [
    (
        re.compile(r"\[(?:find|insert|add|todo|tbd|missing|placeholder)[^\]]*\]", re.I),
        "Leftover bracket placeholder",
    ),
    (
        re.compile(r"\((?:AI evaluation|note to self|todo|tbd|insert )[^)]*\)", re.I),
        "Leftover parenthetical LLM note",
    ),
    (
        re.compile(r"\[[^\]]+\]\(\s*(?:#|https?://example\.com[^)]*)?\s*\)"),
        "Empty or dummy markdown link",
    ),
    (
        re.compile(r"\b(?:TBD|FIXME|TODO)\b"),
        "Unresolved TODO/TBD/FIXME marker",
    ),
    (
        re.compile(r"Placeholder for [a-zA-Z0-9_-]+"),
        "Template placeholder text",
    ),
    (
        re.compile(
            r"(?:/usr/local/google/home/[\w.-]+|/Users/[\w.-]+|/google/src/cloud/[\w./-]+"
            r"|/tmp/[\w./-]+|https?://(?:localhost|127\.0\.0\.1)[:/\w.-]*|conversation://[\w-]+)"
        ),
        "Local-machine / ephemeral path or private link leak",
    ),
    (
        re.compile(
            r"\b(?:delve|delving|tapestry|testament to|holistic|synergy|synergistic"
            r"|paradigm shift|game-changer|it is important to note|it's worth noting)\b",
            re.I,
        ),
        "LLM-slop giveaway phrase",
    ),
]

LDAP_RE = re.compile(r"\b([a-z][a-z0-9_.-]{1,30})@google\.com\b", re.I)

KNOWN_EXTS = (
    "json|md|cpp|cc|c|h|hpp|py|gn|gni|bazel|bzl|dart|ts|js|go|sh|yaml|yml|toml|txt"
)
FILE_EXT_RE = re.compile(rf"^[\w./~-]+?\.(?:{KNOWN_EXTS})$")
PATH_TOKEN_RE = re.compile(r"`([^`\n]{2,120})`")
FILE_LINK_RE = re.compile(r"file://(/[^\s)#`\"']+)")
BARE_PATH_RE = re.compile(
    rf"(?<![\w:/.])((?:/|~|\.\.?/)?[\w.-]+(?:/[\w.*<>-]+)+/?|[\w.-]+\.(?:{KNOWN_EXTS}))(?![\w/])"
)
SKILL_BULLET_RE = re.compile(
    r"^\s*[-*]\s+(?:\*\*|`)+([a-z0-9]+(?:-[a-z0-9]+)+)(?:\*\*|`)+"
)
HEX_SHA_RE = re.compile(r"`([0-9a-f]{7,40})`")
HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
ANCHOR_LINK_RE = re.compile(r"\[[^\]]+\]\(#([a-zA-Z0-9_-]+)\)")


def slugify_heading(title: str) -> str:
    cleaned = re.sub(r"[*`_~]", "", title).strip().lower()
    cleaned = re.sub(r"[^\w\s-]", "", cleaned)
    return re.sub(r"[\s]+", "-", cleaned)

PROSE_SLASH_WORDS = {
    "and", "or", "yes", "no", "true", "false", "read", "write", "input", "output",
    "public", "private", "local", "remote", "client", "server", "before", "after",
    "open", "close", "enable", "disable", "enabled", "disabled", "start", "stop",
    "up", "down", "in", "out", "on", "off", "ci", "cd", "ui", "ux", "io", "os",
    "cpu", "gpu", "pr", "cl", "cls", "prs", "ai", "ml", "llm", "llms", "api",
    "abi", "ide", "cli", "sdk", "jdk", "ndk", "rtl", "ltr", "cr", "lf", "crlf",
    "utf", "ascii", "icu", "skia", "flutter", "chrome", "chromium", "jetski",
    "antigravity", "compile", "test", "build", "run", "edit", "view", "add",
    "remove", "delete", "create", "update", "pass", "fail", "ok", "bad", "good",
    "file", "files", "folder", "folders", "dir", "dirs", "directory", "directories",
    "path", "paths", "skill", "skills", "rule", "rules", "agent", "agents",
    "error", "errors", "warning", "warnings", "verification", "handling",
    "setup", "config", "configs", "status", "logs", "step", "steps", "commit",
    "commits", "script", "scripts",
}

SKIP_WALK_DIRS = {
    ".git", "out", "build", "node_modules", "__pycache__", ".dart_tool", "bazel-out",
    "bazel-bin", "bazel-testlogs", "bazel-skia", "third_party",
}


def load_text(path_str: str) -> str:
    if path_str == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path_str).read_text(encoding="utf-8", errors="replace")

    stripped = raw.strip()
    if stripped.startswith("{") and '"fileContent"' in stripped:
        try:
            data = json.loads(stripped)
            if isinstance(data, dict) and isinstance(data.get("fileContent"), str):
                return data["fileContent"]
        except json.JSONDecodeError:
            pass
    return raw


def clean_google_docs_escapes(text: str) -> str:
    return text.replace(r"\_", "_").replace(r"\[", "[").replace(r"\]", "]")


def index_repo_files(
    repo_paths: list[Path],
) -> tuple[set[str], dict[str, list[str]], set[str]]:
    rel_set: set[str] = set()
    by_basename: dict[str, list[str]] = {}
    skill_names: set[str] = set()

    def register_rel(rel_path: str) -> None:
        rel_set.add(rel_path)
        parts = rel_path.split("/")
        for i in range(1, len(parts)):
            rel_set.add("/".join(parts[:i]))
        base = os.path.basename(rel_path)
        if base:
            bucket = by_basename.setdefault(base, [])
            if rel_path not in bucket:
                bucket.append(rel_path)
        if base == "SKILL.md" and len(parts) >= 2:
            skill_names.add(parts[-2])

    for repo in repo_paths:
        if not repo.is_dir():
            continue
        try:
            out = subprocess.check_output(
                ["git", "-C", str(repo), "ls-files"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            for line in out.splitlines():
                line = line.strip()
                if line:
                    register_rel(line)
        except Exception:
            pass

        # Also walk shallow/non-third_party trees (including DEPS checkouts like agents/shared)
        for root, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in SKIP_WALK_DIRS]
            rel_root = os.path.relpath(root, repo)
            if rel_root == ".":
                rel_root = ""
            for d in dirs:
                rel_d = f"{rel_root}/{d}" if rel_root else d
                rel_set.add(rel_d)
                bucket = by_basename.setdefault(d, [])
                if rel_d not in bucket:
                    bucket.append(rel_d)
            for f in files:
                rel_f = f"{rel_root}/{f}" if rel_root else f
                register_rel(rel_f)

    return rel_set, by_basename, skill_names


def looks_like_path(token: str) -> bool:
    if " " in token or token.startswith(("http://", "https://", "mailto:", "conversation://")):
        return False
    if token.startswith("-") or "::" in token or "(" in token or "=" in token:
        return False
    if FILE_EXT_RE.match(token):
        return True
    if "/" in token:
        stripped = token.strip("/")
        if not stripped:
            return False
        segments = [s for s in stripped.split("/") if s and s != "..."]
        if not segments or all(s.isdigit() for s in segments):
            return False
        # Skip shortlinks (cl/123, b/123, go/foo) and bare domain/org/repo references
        if segments[0].lower() in ("cl", "b", "go", "crbug.com", "skbug.com"):
            return False
        if segments[0].lower() == "github.com" and len(segments) <= 3:
            return False
        # Single-segment trailing-slash directory like `agents/` or `.agents/`
        if len(segments) == 1 and token.endswith("/"):
            seg = segments[0]
            return seg.startswith((".", "_")) or seg in (
                "agents", "skills", "rules", "scripts", "references", "assets",
                "src", "include", "tests", "modules", "tools", "infra", "gn", "out",
            )
        # Filter out ordinary English slash pairs like `read/write` or `Error/Verification`
        if len(segments) == 2 and not token.endswith("/"):
            s0, s1 = segments[0].lower(), segments[1].lower()
            repo_roots = (
                "agents", "_agents", ".agents", "src", "include",
                "tools", "tests", "modules", "infra", "gn",
            )
            if (s0 in PROSE_SLASH_WORDS or s1 in PROSE_SLASH_WORDS) and s0 not in repo_roots:
                return False
            if segments[0][0].isupper() and segments[1][0].isupper():
                return False
        return True
    return False


def normalize_candidate_path(token: str) -> str:
    cleaned = token.strip().rstrip(".,:;)")
    cleaned = re.sub(r"^github\.com/[^/]+/[^/]+/(?:blob|tree)/[^/]+/", "", cleaned)
    cleaned = re.sub(r"(?::\d+(?:-\d+)?|#L\d+(?:-L?\d+)?)$", "", cleaned)
    cleaned = re.sub(r"/\.\.\.$", "", cleaned)
    cleaned = re.sub(r"/\*[^/]*$", "", cleaned)
    if cleaned.endswith("-"):
        return ""
    return cleaned.rstrip("/")


def path_exists_anywhere(cand: str, repos: list[Path], repo_rel_set: set[str]) -> bool:
    if not cand:
        return True
    expanded = os.path.expanduser(cand)
    if os.path.exists(expanded):
        return True
    if cand.startswith("/google3/") or cand.startswith("google3/"):
        g3_rel = cand.lstrip("/")
        if (Path("/google/src/files/head/depot") / g3_rel).exists():
            return True
    if cand.startswith("//"):
        bazel_rel = cand.lstrip("/").split(":")[0]
        if bazel_rel in repo_rel_set or any((r / bazel_rel).exists() for r in repos):
            return True
    rel = cand.lstrip("./")
    if rel in repo_rel_set:
        return True
    return any((r / rel).exists() for r in repos)


def check_commit_sha(sha: str, repos: list[Path]) -> bool:
    for repo in repos:
        try:
            res = subprocess.run(
                ["git", "-C", str(repo), "cat-file", "-t", sha],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            if res.returncode == 0:
                return True
        except Exception:
            pass
    return False


def check_active_ldap(ldap: str) -> bool:
    try:
        res = subprocess.run(
            ["id", "-u", ldap.lower()],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return res.returncode == 0
    except Exception:
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic document grounding checker.")
    parser.add_argument("files", nargs="+", help="Document file(s) to check ('-' for stdin)")
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repository root(s) to resolve relative paths and commit SHAs against",
    )
    args = parser.parse_args()

    repos = [Path(os.path.expanduser(r)).resolve() for r in args.repo]
    if not repos:
        try:
            top = subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if top:
                repos.append(Path(top))
        except Exception:
            pass

    repo_rel_set, by_basename, skill_names = index_repo_files(repos)
    total_issues = 0

    for file_arg in args.files:
        raw_text = load_text(file_arg)
        text = clean_google_docs_escapes(raw_text)
        lines = text.splitlines()

        print(f"=== Grounding Audit: {file_arg} ===")
        if repos:
            print(f"Repositories checked: {', '.join(str(r) for r in repos)}")

        placeholders: list[tuple[int, str, str]] = []
        for idx, line in enumerate(lines, start=1):
            for regex, label in PLACEHOLDER_PATTERNS:
                for m in regex.finditer(line):
                    placeholders.append((idx, label, m.group(0)))

        seen_paths: set[str] = set()
        doc_paths_by_base: dict[str, list[tuple[int, str]]] = {}
        missing_paths: list[tuple[int, str, list[str]]] = []
        verified_paths: list[str] = []

        heading_slugs: set[str] = set()
        for line in lines:
            hm = HEADING_RE.match(line)
            if hm:
                heading_slugs.add(slugify_heading(hm.group(1)))

        broken_anchors: list[tuple[int, str]] = []
        for idx, line in enumerate(lines, start=1):
            for am in ANCHOR_LINK_RE.finditer(line):
                anchor = am.group(1).lower()
                if heading_slugs and anchor not in heading_slugs:
                    broken_anchors.append((idx, am.group(1)))

        for idx, line in enumerate(lines, start=1):
            # Ignore URLs before scanning bare paths
            line_no_urls = re.sub(r"https?://\S+", "", line)
            candidates: list[str] = []
            for m in FILE_LINK_RE.finditer(line):
                candidates.append(m.group(1))
            for m in PATH_TOKEN_RE.finditer(line):
                tok = m.group(1).strip()
                if looks_like_path(tok):
                    candidates.append(tok)
            for m in BARE_PATH_RE.finditer(line_no_urls):
                tok = m.group(1).strip()
                if looks_like_path(tok):
                    candidates.append(tok)

            for raw_cand in candidates:
                cand = normalize_candidate_path(raw_cand)
                if not cand or cand in seen_paths:
                    continue
                if "<" in cand or ">" in cand or "$" in cand or "*" in cand:
                    continue
                seen_paths.add(cand)
                if "/" in cand:
                    base = os.path.basename(cand)
                    if base and base not in ("SKILL.md", "AGENTS.md", "GEMINI.md", "README.md"):
                        doc_paths_by_base.setdefault(base, []).append((idx, cand))
                if path_exists_anywhere(cand, repos, repo_rel_set):
                    verified_paths.append(cand)
                else:
                    base = os.path.basename(cand)
                    suggestions = by_basename.get(base, [])[:4] if base else []
                    missing_paths.append((idx, raw_cand, suggestions))

        intra_doc_path_conflicts: list[tuple[str, list[tuple[int, str]]]] = [
            (base, entries)
            for base, entries in sorted(doc_paths_by_base.items())
            if len({c.lstrip("./") for _, c in entries}) > 1
        ]

        unresolved_skills: list[tuple[int, str]] = []
        if skill_names:
            seen_skills: set[str] = set()
            for idx, line in enumerate(lines, start=1):
                m = SKILL_BULLET_RE.match(line)
                if m:
                    sname = m.group(1)
                    if sname not in seen_skills and sname not in skill_names:
                        seen_skills.add(sname)
                        unresolved_skills.append((idx, sname))

        invalid_shas: list[tuple[int, str]] = []
        if repos:
            seen_shas: set[str] = set()
            for idx, line in enumerate(lines, start=1):
                for m in HEX_SHA_RE.finditer(line):
                    sha = m.group(1)
                    if sha in seen_shas:
                        continue
                    seen_shas.add(sha)
                    if not check_commit_sha(sha, repos):
                        invalid_shas.append((idx, sha))

        invalid_ldaps: list[tuple[int, str]] = []
        seen_ldaps: set[str] = set()
        for idx, line in enumerate(lines, start=1):
            for m in LDAP_RE.finditer(line):
                ldap = m.group(1).lower()
                if ldap in seen_ldaps or ldap in ("user", "username", "author", "ldap", "example"):
                    continue
                seen_ldaps.add(ldap)
                if not check_active_ldap(ldap):
                    invalid_ldaps.append((idx, ldap))

        print(f"\n[1] Leftover Placeholders / Scaffolding ({len(placeholders)}):")
        if placeholders:
            for line_no, label, snippet in placeholders:
                print(f"  - Line {line_no}: [{label}] {snippet!r}")
            total_issues += len(placeholders)
        else:
            print("  None")

        intra_count = len(intra_doc_path_conflicts) + len(broken_anchors)
        print(f"\n[2] Intra-Document Consistency Warnings ({intra_count}):")
        if intra_doc_path_conflicts or broken_anchors:
            for base, entries in intra_doc_path_conflicts:
                locs = ", ".join(f"`{c}` (L{ln})" for ln, c in entries)
                print(f"  - Multiple distinct paths for `{base}` within the same doc: {locs}")
            for line_no, anchor in broken_anchors:
                print(f"  - Line {line_no}: Broken internal heading anchor `#{anchor}`")
            total_issues += intra_count
        else:
            print("  None")

        print(
            f"\n[3] Unresolved / Missing Paths ({len(missing_paths)} missing, "
            f"{len(verified_paths)} verified):"
        )
        if missing_paths:
            for line_no, raw_cand, suggestions in missing_paths:
                hint = (
                    f" -> Existing same-name path(s): {', '.join(suggestions)}"
                    if suggestions
                    else " -> Not found in repo (verify if proposed new path vs. hallucination)"
                )
                print(f"  - Line {line_no}: `{raw_cand}`{hint}")
            total_issues += len(missing_paths)
        else:
            print("  None (all cited paths exist)")

        if skill_names:
            print(
                f"\n[4] Unresolved Bullet Skill Names ({len(unresolved_skills)} missing; "
                f"known repo skills: {', '.join(sorted(skill_names))}):"
            )
            if unresolved_skills:
                for line_no, sname in unresolved_skills:
                    print(f"  - Line {line_no}: `{sname}` not found in repo skills")
                total_issues += len(unresolved_skills)
            else:
                print("  None")

        if repos:
            print(f"\n[5] Unresolved Git Commit SHAs ({len(invalid_shas)}):")
            if invalid_shas:
                for line_no, sha in invalid_shas:
                    print(f"  - Line {line_no}: `{sha}` not found in git history")
                total_issues += len(invalid_shas)
            else:
                print("  None")

        print(f"\n[6] Inactive or Nonexistent @google.com LDAPs ({len(invalid_ldaps)}):")
        if invalid_ldaps:
            for line_no, ldap in invalid_ldaps:
                print(f"  - Line {line_no}: `{ldap}@google.com` (no active user found via `id -u`)")
            total_issues += len(invalid_ldaps)
        else:
            print("  None")

    return 1 if total_issues > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
