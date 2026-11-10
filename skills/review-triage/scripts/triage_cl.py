#!/usr/bin/env python3
"""
review_triage.py — Automated Code Review Triage and Action Engine.

Classifies incoming code reviews into 4 actionable categories:
  1. MECHANICAL: Pure routine, formatting, rename, boilerplate, revert, version bump.
  2. REASSIGN: Changes outside user domain / wrong reviewer.
  3. SURGICAL_AUDIT: Critical core domain logic; isolates the 5% core mutation lines.
  4. PUSHBACK: Low hygiene (missing tests, poor description, mixed toxic scope).

Outputs structured Markdown with exact line numbers, invariant checklists,
and ready-to-copy review drafts in English.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import heapq
import json
import os
import re
import subprocess
import sys
from pathlib import Path
import urllib.request
import urllib.error

SHARED_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(SHARED_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS_DIR))
import finding_ledger
import triage_store

USER_DOMAIN_KEYWORDS = [
    "text", "paragraph", "skparagraph", "webparagraph", "unicode",
    "font", "shaping", "bidi", "layout", "typography", "strike",
    "slug", "glyph", "run", "textblob", "line_metrics", "caret",
    "selection", "hit_test", "web_ui/lib/src/engine/text"
]

NON_DOMAIN_HINTS = [
    "gpu/ganesh/vk", "gpu/ganesh/mtl", "gpu/ganesh/gl", "gpu/graphite/dawn",
    "gpu/graphite/mtl", "gpu/graphite/vk", "bazel", "infra", "gn", "build",
    "experimental/rust", "tools/perf", "tools/viewer", "shell/platform/android",
    "shell/platform/darwin", "shell/platform/windows"
]

# Restrict corpus gating to production C++/Dart/TS directories (excluding tests/slides)
SKIA_CORPUS_PATH_RE = re.compile(
    r"(?:^|/)modules/(?:skparagraph|skunicode|skshaper)/(?:src|include)/"
)
WEBPARAGRAPH_CORPUS_PATH_RE = re.compile(
    r"(?:^|/)(?:engine/src/flutter/)?lib/web_ui/lib/"
)
CODE_CORPUS_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".mm", ".dart", ".ts"
}


# Scan hunk diff lines to distinguish executable code changes from comment-only edits
def hunk_has_executable_code_changes(hunk):
    """True if hunk has added/deleted non-comment code lines."""
    in_block = False
    for raw_line in hunk.lines:
        if not raw_line or raw_line[0] not in ("+", "-") or raw_line.startswith(("+++", "---")):
            continue
        line = raw_line[1:].strip()
        while line:
            if in_block:
                end_idx = line.find("*/")
                if end_idx < 0:
                    line = ""
                    break
                in_block = False
                line = line[end_idx + 2:].strip()
            else:
                if line.startswith("//"):
                    line = ""
                    break
                start_idx = line.find("/*")
                line_comment_idx = line.find("//")
                if 0 <= line_comment_idx < (start_idx if start_idx >= 0 else len(line)):
                    if line[:line_comment_idx].strip():
                        return True
                    line = ""
                    break
                if start_idx == 0:
                    end_idx = line.find("*/", 2)
                    if end_idx < 0:
                        in_block = True
                        line = ""
                        break
                    line = line[end_idx + 2:].strip()
                elif start_idx > 0:
                    return True
                else:
                    return True
    return False


# Flag reviews that touch SkParagraph/SkUnicode/SkShaper or WebParagraph executable code
def detect_required_review_corpus(files):
    """Detect if changed files touch SkParagraph/SkUnicode/SkShaper or WebParagraph executable code."""
    skia_files = []
    wp_files = []
    for f in files:
        norm = f.path.strip().replace("\\", "/")
        _, ext = os.path.splitext(norm)
        if ext.lower() not in CODE_CORPUS_EXTENSIONS or f.is_test:
            continue
        if not any(hunk_has_executable_code_changes(h) for h in f.hunks):
            continue
        if SKIA_CORPUS_PATH_RE.search(norm):
            skia_files.append(norm)
        elif WEBPARAGRAPH_CORPUS_PATH_RE.search(norm):
            wp_files.append(norm)
    if skia_files:
        return {
            "required": True,
            "domain": "SkParagraph/SkUnicode/SkShaper",
            "script": "run_flutter_paragraph_tests.py",
            "command": (
                "python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/"
                "scripts/run_flutter_paragraph_tests.py"
            ),
            "files": skia_files,
        }
    if wp_files:
        return {
            "required": True,
            "domain": "WebParagraph (web_ui)",
            "script": "run_webparagraph_corpus.py",
            "command": (
                "python3 ~/.gemini/config/skills/webparagraph-corpus/"
                "scripts/run_webparagraph_corpus.py"
            ),
            "files": wp_files,
        }
    return {"required": False, "domain": "", "script": "", "command": "", "files": []}

REVIEWER_EMAILS = {"jlavrova@google.com", "jlavrova@gmail.com"}
REVIEWER_NAMES = {"Julia Lavrova"}


def is_self_author(author_obj):
    if not isinstance(author_obj, dict):
        return False
    email = (author_obj.get("email") or "").strip().lower()
    self_emails = {e.lower() for e in REVIEWER_EMAILS}
    return email in self_emails or author_obj.get("name") in REVIEWER_NAMES


def _is_same_account(a, b):
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    aid = a.get("_account_id")
    bid = b.get("_account_id")
    if aid is not None and bid is not None and aid == bid:
        return True
    ae = (a.get("email") or "").strip().lower()
    be = (b.get("email") or "").strip().lower()
    return bool(ae and ae == be)


def _role_label(author_obj, owner_obj):
    if is_self_author(author_obj):
        return "you"
    if _is_same_account(author_obj, owner_obj):
        return "author"
    return "reviewer"


def _parse_gerrit_ts(ts_str):
    if not ts_str or not isinstance(ts_str, str):
        return None
    s = ts_str.strip()
    try:
        # Trim Gerrit 9-digit nanoseconds down to 6-digit microseconds for fromisoformat
        if len(s) >= 26 and s[19] == ".":
            frac = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\.(\d+)", s)
            if frac:
                us = (frac.group(3) + "000000")[:6]
                return datetime.fromisoformat(
                    f"{frac.group(1)}T{frac.group(2)}.{us}+00:00"
                )
        clean = s.replace("Z", "+00:00")
        if len(clean) == 19 and clean[10] == " ":
            return datetime.strptime(clean, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(clean)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _short_excerpt(text, limit=140):
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


# Reconstruct Gerrit /comments threads via in_reply_to and return unresolved threads with tags
def extract_open_threads(detail, inline_comments, cur_ps):
    if not isinstance(inline_comments, dict):
        return []
    owner_obj = detail.get("owner") or {}
    rev_created = {}
    for r in (detail.get("revisions") or {}).values():
        if not isinstance(r, dict):
            continue
        num = int(r.get("_number") or 0)
        ts = _parse_gerrit_ts(r.get("created"))
        if num > 0 and ts is not None:
            rev_created[num] = ts

    by_id = {}
    children = {}
    anon_idx = 0
    for path, clist in inline_comments.items():
        for raw in clist or []:
            if not isinstance(raw, dict):
                continue
            cid = str(raw.get("id") or "")
            if not cid:
                anon_idx += 1
                cid = f"_anon_{anon_idx}"
            by_id[cid] = (path, raw)

    for cid, (_, c) in by_id.items():
        parent_id = str(c.get("in_reply_to") or "")
        if parent_id and parent_id in by_id and parent_id != cid:
            children.setdefault(parent_id, []).append(cid)

    roots = [
        cid for cid, (_, c) in by_id.items()
        if not c.get("in_reply_to")
        or str(c.get("in_reply_to")) not in by_id
        or str(c.get("in_reply_to")) == cid
    ]

    open_threads = []
    for root_id in roots:
        root_path, root_c = by_id[root_id]
        heap = [(str(root_c.get("updated") or ""), root_id)]
        thread = []
        visited = set()
        while heap and len(visited) <= len(by_id):
            _, cid = heapq.heappop(heap)
            if cid in visited:
                continue
            visited.add(cid)
            thread.append(by_id[cid])
            for ch_id in children.get(cid, ()):
                if ch_id not in visited:
                    ch_c = by_id[ch_id][1]
                    heapq.heappush(heap, (str(ch_c.get("updated") or ""), ch_id))
        if not thread:
            continue
        _, leaf = thread[-1]
        if not bool(leaf.get("unresolved", False)):
            continue

        if root_path == "/PATCHSET_LEVEL":
            loc = "patchset"
        else:
            line_no = root_c.get("line")
            if not line_no and isinstance(root_c.get("range"), dict):
                line_no = root_c["range"].get("end_line") or root_c["range"].get("start_line")
            loc = f"{root_path}:{line_no}" if line_no else root_path

        root_ps = int(root_c.get("patch_set") or 0)
        root_ts = _parse_gerrit_ts(root_c.get("updated"))
        newest_ps_at_post = root_ps
        if root_ts is not None and rev_created:
            existing = [ps_n for ps_n, c_ts in rev_created.items() if c_ts <= root_ts]
            if existing:
                newest_ps_at_post = max(newest_ps_at_post, max(existing))
        # Flag drafts/comments posted onto an older patchset after a newer patchset already existed
        stale = bool(root_ps > 0 and newest_ps_at_post > root_ps)

        root_author_obj = root_c.get("author") or {}
        leaf_author_obj = leaf.get("author") or {}
        root_role = _role_label(root_author_obj, owner_obj)
        last_role = _role_label(leaf_author_obj, owner_obj)
        reply_count = len(thread) - 1
        author_replied = bool(reply_count > 0 and last_role == "author")
        tags = []
        if stale:
            tags.append("STALE")
        if author_replied:
            tags.append("AUTHOR_REPLIED")

        open_threads.append({
            "id": root_id,
            "path": root_path,
            "line": root_c.get("line"),
            "location": loc,
            "root_author": root_author_obj.get("name") or root_author_obj.get("email") or "Unknown",
            "root_email": root_author_obj.get("email") or "",
            "root_role": root_role,
            "patch_set": root_ps,
            "current_ps": cur_ps,
            "newest_ps_at_post": newest_ps_at_post,
            "reply_count": reply_count,
            "last_author": leaf_author_obj.get("name") or leaf_author_obj.get("email") or "Unknown",
            "last_email": leaf_author_obj.get("email") or "",
            "last_role": last_role,
            "stale": stale,
            "author_replied": author_replied,
            "tags": tags,
            "updated": str(root_c.get("updated") or ""),
            "root_excerpt": _short_excerpt(root_c.get("message", "")),
            "last_excerpt": _short_excerpt(leaf.get("message", "")) if reply_count > 0 else "",
        })

    open_threads.sort(key=lambda item: (item["updated"], item["location"], item["id"]))
    return open_threads


def extract_gerrit_thread_context(detail, inline_comments):
    """Extract patchset revision history, last self-reviewed patchset, and review dialogue."""
    revs = detail.get("revisions") or {}
    cur_sha = detail.get("current_revision", "")
    cur_rev = revs.get(cur_sha) or {}
    cur_ps = cur_rev.get("_number") or 1
    cur_ref = cur_rev.get("ref", "")
    parents = [p.get("commit", "") for p in (cur_rev.get("commit") or {}).get("parents", [])]
    cur_parent = parents[0] if parents else ""

    ps_map = {}
    for sha, r in revs.items():
        num = r.get("_number")
        if num:
            ps_map[num] = {"sha": sha, "ref": r.get("ref", "")}

    owner_obj = detail.get("owner") or {}
    last_reviewed_ps = None
    # A review on the current patchset (often a bare "Code-Review+1" message) means the CL is
    # already reviewed, not an initial review or a follow-up on an older patchset
    reviewed_current_ps = False
    companion_urls = []
    dialogue = []

    # Collect human review messages across patchsets to preserve reviewer/author agreements
    for m in detail.get("messages") or []:
        tag = m.get("tag") or ""
        if tag.startswith("autogenerated:"):
            continue
        author = m.get("author") or {}
        ps = m.get("_revision_number") or 0
        msg = (m.get("message") or "").strip()
        # Skip empty Gerrit envelope headers whose body lives in /comments
        if not msg or re.match(r"^Patch Set \d+:\s*(\(\d+ comments?\))?$", msg):
            continue
        if is_self_author(author) and ps and ps < cur_ps:
            last_reviewed_ps = max(last_reviewed_ps or 0, ps)
        if is_self_author(author) and ps == cur_ps:
            reviewed_current_ps = True
        for u in re.findall(r"https://[^\s)>]+/\+/\d+", msg):
            if str(detail.get("_number", "")) not in u and u not in companion_urls:
                companion_urls.append(u)
        role = _role_label(author, owner_obj).upper()
        who = author.get("name", "Unknown")
        dialogue.append((m.get("date", ""), f"[PS{ps} {role} ({who})] {msg}"))

    # Include patchset-level and file-level comments so prior requests and replies are visible
    if isinstance(inline_comments, dict):
        for path, clist in inline_comments.items():
            for c in clist or []:
                author = c.get("author") or {}
                ps = c.get("patch_set") or 0
                msg = (c.get("message") or "").strip()
                if not msg:
                    continue
                if is_self_author(author) and ps and ps < cur_ps:
                    last_reviewed_ps = max(last_reviewed_ps or 0, ps)
                if is_self_author(author) and ps == cur_ps:
                    reviewed_current_ps = True
                for u in re.findall(r"https://[^\s)>]+/\+/\d+", msg):
                    if str(detail.get("_number", "")) not in u and u not in companion_urls:
                        companion_urls.append(u)
                role = _role_label(author, owner_obj).upper()
                who = author.get("name", "Unknown")
                if path == "/PATCHSET_LEVEL":
                    loc = "patchset"
                else:
                    line_no = c.get("line")
                    loc = f"{path}:{line_no}" if line_no else path
                dialogue.append((c.get("updated", ""), f"[PS{ps} {role} ({who}) @ {loc}] {msg}"))

    dialogue.sort(key=lambda x: x[0])
    prev_info = ps_map.get(last_reviewed_ps) or {}
    # Gerrit label "all" lists the votes that stand on the current patchset; a sticky vote
    # carried over by a trivial rebase shows here even without a message on the new patchset
    self_votes = []
    for label, info in (detail.get("labels") or {}).items():
        for v in (info or {}).get("all") or []:
            if is_self_author(v) and v.get("value"):
                self_votes.append(f"{label}{v['value']:+d}")
    open_threads = extract_open_threads(detail, inline_comments, cur_ps)
    return {
        "current_ps": cur_ps,
        "current_sha": cur_sha,
        "current_ref": cur_ref,
        "current_parent": cur_parent,
        "last_reviewed_ps": last_reviewed_ps,
        "last_reviewed_sha": prev_info.get("sha", ""),
        "last_reviewed_ref": prev_info.get("ref", ""),
        "reviewed_current_ps": reviewed_current_ps,
        "self_votes": self_votes,
        "companion_urls": companion_urls,
        "dialogue": [item[1] for item in dialogue[-12:]],
        "open_threads": open_threads,
    }


def probe_gerrit(cl_id, host_hint=None):
    """Probe Gerrit hosts for change metadata, commit message, review thread, and patch."""
    default_hosts = ["skia-review.googlesource.com", "chromium-review.googlesource.com"]
    hosts = [host_hint] if host_hint else default_hosts
    for host in hosts:
        if not host:
            continue
        try:
            # Request full commit message, all revisions, and messages to support follow-up diffs
            opts = (
                "o=CURRENT_REVISION&o=CURRENT_COMMIT"
                "&o=ALL_REVISIONS&o=MESSAGES&o=DETAILED_ACCOUNTS&o=DETAILED_LABELS"
            )
            url = f"https://{host}/changes/{cl_id}/detail?{opts}"
            req = urllib.request.Request(url, headers={"User-Agent": "ReviewTriageAgent"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                if raw.startswith(")]}'"):
                    raw = raw[4:]
                detail = json.loads(raw)

            inline_comments = {}
            try:
                c_url = f"https://{host}/changes/{cl_id}/comments"
                c_req = urllib.request.Request(c_url, headers={"User-Agent": "ReviewTriageAgent"})
                with urllib.request.urlopen(c_req, timeout=8) as c_resp:
                    c_raw = c_resp.read().decode("utf-8", errors="replace")
                    if c_raw.startswith(")]}'"):
                        c_raw = c_raw[4:]
                    inline_comments = json.loads(c_raw)
            except Exception:
                pass

            patch_url = f"https://{host}/changes/{cl_id}/revisions/current/patch"
            patch_req = urllib.request.Request(
                patch_url, headers={"User-Agent": "ReviewTriageAgent"}
            )
            with urllib.request.urlopen(patch_req, timeout=12) as patch_resp:
                b64 = patch_resp.read()
                patch_text = base64.b64decode(b64).decode("utf-8", errors="replace")

            cur_sha = detail.get("current_revision", "")
            cur_rev = (detail.get("revisions") or {}).get(cur_sha) or {}
            commit_msg = ((cur_rev.get("commit") or {}).get("message") or "").strip()
            thread_ctx = extract_gerrit_thread_context(detail, inline_comments)

            return {
                "source": f"Gerrit ({host})",
                "id": str(cl_id),
                "host": host,
                "subject": detail.get("subject", ""),
                "owner": detail.get("owner", {}).get("name", "Unknown"),
                "owner_email": detail.get("owner", {}).get("email", ""),
                "description": commit_msg or detail.get("subject", ""),
                "commit_message": commit_msg,
                "patch": patch_text,
                "patchset": cur_rev.get("_number"),
                "thread_context": thread_ctx,
                "detail": detail,
            }
        except Exception:
            continue
    return None

def fetch_google3_cl(cl_number):
    """Fetch Piper CL metadata and diff via g4."""
    cl_str = str(cl_number).replace("cl/", "")
    try:
        describe_out = subprocess.check_output(
            ["g4", "describe", "-s", cl_str],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15
        )
    except Exception as e:
        return {"error": f"Failed to run g4 describe: {e}"}

    try:
        diff_out = subprocess.check_output(
            ["g4", "describe", cl_str],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=25
        )
    except Exception as e:
        diff_out = f"Failed to run g4 diff: {e}"

    lines = describe_out.splitlines()
    owner = "Unknown"
    desc_lines = []
    in_desc = False
    for line in lines:
        if line.startswith(f"Change {cl_str} by "):
            parts = line.split()
            if len(parts) >= 4:
                owner = parts[3].split("@")[0]
            in_desc = True
            continue
        if line.startswith("Affected files ..."):
            break
        if in_desc:
            desc_lines.append(line)

    full_desc = "\n".join(desc_lines).strip()
    subject = desc_lines[0].strip() if desc_lines else f"CL {cl_str}"

    return {
        "source": "Google3 Piper",
        "project": "google3",
        "id": cl_str,
        "subject": subject,
        "owner": owner,
        "owner_email": f"{owner}@google.com",
        "description": full_desc,
        "patch": diff_out
    }

def fetch_github_pr(repo_url, pr_number):
    """Fetch GitHub PR metadata and patch via gh CLI or .diff URL."""
    clean_repo = repo_url.rstrip("/")
    diff_url = f"{clean_repo}/pull/{pr_number}.diff"
    req = urllib.request.Request(diff_url, headers={"User-Agent": "ReviewTriageAgent"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            patch_text = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return {"error": f"Failed to fetch GitHub PR diff: {e}"}

    subject = f"PR #{pr_number}"
    owner = "Unknown"
    description = subject
    head_sha = ""
    m_repo = re.search(r"github\.com/([^/]+/[^/]+)$", clean_repo)
    if m_repo:
        repo_slug = m_repo.group(1)
        try:
            meta_raw = subprocess.check_output(
                ["gh", "api", f"repos/{repo_slug}/pulls/{pr_number}"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
            )
            meta = json.loads(meta_raw)
            subject = meta.get("title") or subject
            owner = (meta.get("user") or {}).get("login") or owner
            # Record PR head SHA so new commits reset corpus gate even when patchset stays 1
            head_sha = str((meta.get("head") or {}).get("sha") or "")
            body = (meta.get("body") or "").strip()
            description = f"{subject}\n\n{body}".strip() if body else subject
        except Exception:
            pass

    if owner == "Unknown":
        for line in patch_text.splitlines()[:30]:
            if line.startswith("Subject:"):
                subject = line.replace("Subject:", "").strip()
            elif line.startswith("From:"):
                owner = line.replace("From:", "").strip()

    return {
        "source": f"GitHub ({clean_repo})",
        "repo_url": clean_repo,
        "pr_number": str(pr_number),
        "id": str(pr_number),
        "head_sha": head_sha,
        "subject": subject,
        "owner": owner,
        "owner_email": "",
        "description": description,
        "patch": patch_text
    }

class DiffHunk:
    def __init__(self, old_start, old_len, new_start, new_len, header):
        self.old_start = old_start
        self.old_len = old_len
        self.new_start = new_start
        self.new_len = new_len
        self.header = header
        self.lines = []
        self.added = 0
        self.deleted = 0
        self.complexity_score = 0

class ChangedFile:
    def __init__(self, path):
        self.path = path
        self.old_path = path
        self.hunks = []
        self.added = 0
        self.deleted = 0
        self.is_new_file = False
        self.is_deleted_file = False
        self.is_renamed = False
        self.is_build = bool(
            re.search(r"(BUILD|\.gn|\.bazel|\.bzl|CMakeLists|DEPS)", path)
        )
        # Exclude build lists like gn/tests.gni so they never count as unit test source files
        self.is_test = bool(
            re.search(r"(test|Test|\btests/|_test\.dart|_test\.cpp|_gm\.cpp)", path, re.I)
        ) and not self.is_build
        self.is_bench = (
            bool(
                re.search(
                    r"(?:^|/)(?:bench|benchmarks)/|(?:_bench|Bench|Benchmark)\.\w+$", path
                )
            )
            and not self.is_build
            and not self.is_test
        )
        self.is_doc = bool(re.search(r"(\.md|\.rst|\.txt|OWNERS|METADATA)", path))
        self.is_header = bool(re.search(r"\.(h|hh|hpp|hxx)$", path)) and not self.is_build
        self.is_impl = (
            bool(re.search(r"\.(c|cc|cpp|cxx|mm|dart|py|java|ts|js|go)$", path))
            and not self.is_test
            and not self.is_build
        )
        self.domain_affinity = any(k in path.lower() for k in USER_DOMAIN_KEYWORDS)

def changed_line_range(hunk):
    # Hunk headers include context lines, so anchor on the first and last new-side changed lines
    line_no = hunk.new_start
    first = last = None
    for line in hunk.lines:
        if line.startswith("+"):
            first = first or line_no
            last = line_no
            line_no += 1
        elif line.startswith("-"):
            # A pure deletion sits between new-side lines; point at the line that follows it
            first = first or line_no
            last = last or line_no
        elif not line.startswith("\\"):
            line_no += 1
    if first is None:
        return hunk.new_start, hunk.new_start + hunk.new_len
    return first, max(first, last)


def parse_patch(patch_text):
    """Parse unified git diff into ChangedFile objects with hunks and file lifecycle flags."""
    files = []
    current_file = None
    current_hunk = None
    pending_old_path = ""
    has_explicit_header = False

    for line in patch_text.splitlines():
        # Match file diff headers across Git, Piper, and SVN/patch formats
        m_git = re.search(r"^diff --git a/(.*?) b/(.*)$", line)
        m_piper = re.search(r"^==== //" + r"depot/google3/(.*?)#", line)
        m_idx = re.search(r"^Index: (.*)$", line)

        if m_git:
            has_explicit_header = True
            old_p = m_git.group(1).strip()
            filepath = m_git.group(2).strip()
            current_file = ChangedFile(filepath)
            current_file.old_path = old_p
            current_file.is_renamed = old_p != filepath
            files.append(current_file)
            current_hunk = None
            pending_old_path = ""
            continue
        elif m_piper:
            has_explicit_header = True
            filepath = m_piper.group(1).strip()
            current_file = ChangedFile(filepath)
            files.append(current_file)
            current_hunk = None
            pending_old_path = ""
            continue
        elif m_idx:
            has_explicit_header = True
            filepath = m_idx.group(1).strip()
            current_file = ChangedFile(filepath)
            files.append(current_file)
            current_hunk = None
            pending_old_path = ""
            continue

        # Reset hunk state at file boundaries in headerless unified diffs (diff -u)
        if not has_explicit_header and line.startswith("--- "):
            cand_old = line[4:].split("\t", 1)[0].strip()
            if (
                cand_old == "/dev/null"
                or cand_old.startswith("a/")
                or "/" in cand_old
                or re.search(r"\.[A-Za-z0-9_]+$", cand_old)
            ):
                current_file = None
                current_hunk = None

        # Capture file-level git mode and rename metadata before hunk headers start
        if current_hunk is None:
            if line.startswith("new file mode"):
                if current_file:
                    current_file.is_new_file = True
                continue
            if line.startswith("deleted file mode"):
                if current_file:
                    current_file.is_deleted_file = True
                continue
            if line.startswith("rename from "):
                if current_file:
                    current_file.old_path = line[len("rename from ") :].strip()
                    current_file.is_renamed = True
                continue
            if line.startswith("rename to "):
                if current_file:
                    current_file.is_renamed = True
                continue
            if line.startswith("--- "):
                raw_old = line[4:].split("\t", 1)[0].strip()
                if raw_old == "/dev/null":
                    if current_file:
                        current_file.is_new_file = True
                    pending_old_path = "/dev/null"
                else:
                    pending_old_path = raw_old[2:] if raw_old.startswith("a/") else raw_old
                continue
            # Keep deleted files attached to their real path instead of creating a /dev/null entry
            if line.startswith("+++ "):
                raw_new = line[4:].split("\t", 1)[0].strip()
                if raw_new == "/dev/null":
                    if current_file:
                        current_file.is_deleted_file = True
                    elif pending_old_path and pending_old_path != "/dev/null":
                        current_file = ChangedFile(pending_old_path)
                        current_file.is_deleted_file = True
                        files.append(current_file)
                elif not has_explicit_header or current_file is None:
                    filepath = raw_new[2:] if raw_new.startswith("b/") else raw_new
                    current_file = ChangedFile(filepath)
                    if pending_old_path == "/dev/null":
                        current_file.is_new_file = True
                    elif pending_old_path:
                        current_file.old_path = pending_old_path
                        current_file.is_renamed = pending_old_path != filepath
                    files.append(current_file)
                continue

        # Match hunk header: @@ -old_start,old_len +new_start,new_len @@ header
        if line.startswith("@@ "):
            m_hunk = re.search(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)", line)
            if m_hunk and current_file:
                old_start = int(m_hunk.group(1))
                old_len = int(m_hunk.group(2)) if m_hunk.group(2) is not None else 1
                new_start = int(m_hunk.group(3))
                new_len = int(m_hunk.group(4)) if m_hunk.group(4) is not None else 1
                header = m_hunk.group(5).strip()
                if old_start == 0 and old_len == 0:
                    current_file.is_new_file = True
                elif new_start == 0 and new_len == 0:
                    current_file.is_deleted_file = True
                current_hunk = DiffHunk(old_start, old_len, new_start, new_len, header)
                current_file.hunks.append(current_hunk)
            continue

        if current_hunk:
            current_hunk.lines.append(line)
            if line.startswith("+") and not line.startswith("+++"):
                current_hunk.added += 1
                current_file.added += 1
                content = line[1:].strip()
                # Branching & control flow
                if re.search(r"\b(if|else if|for|while|switch|case|return)\b", content):
                    current_hunk.complexity_score += 4
                # State mutations
                if re.search(r"(=|\+=|-=|\*=|reset|clear|insert|erase|emplace|push_back)", content):
                    current_hunk.complexity_score += 2
                # Allocations & heavy types
                if re.search(
                    r"\b(new|malloc|make_unique|make_shared|std::vector|std::string)\b",
                    content,
                ):
                    current_hunk.complexity_score += 5
                # Invariants & checks
                if re.search(r"\b(SkASSERT|assert|CHECK|DCHECK)\b", content):
                    current_hunk.complexity_score += 2
            elif line.startswith("-") and not line.startswith("---"):
                current_hunk.deleted += 1
                current_file.deleted += 1
                current_hunk.complexity_score += 1

    return files


C_BUILD_SOURCE_EXTS = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".m", ".mm"
}
INCLUDE_GUARD_SUFFIX_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:_H|_H_|_HPP|_HPP_|_HXX_|_DEFINED|_INCLUDED)$"
)
SKIA_TEST_MACRO_RE = re.compile(
    r"\b(DEF_(?:CONDITIONAL_)?TEST|UNIX_ONLY_TEST"
    r"|DEF_GANESH_TEST(?:_[A-Z0-9_]+)?|DEF_GRAPHITE_TEST(?:_[A-Z0-9_]+)?)"
    r"\s*\(\s*([A-Za-z_]\w*)"
)
GTEST_MACRO_RE = re.compile(
    r"\b(TEST|TEST_F|TEST_P|TYPED_TEST(?:_P)?)\s*\(\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)"
)
DART_TS_TEST_RE = re.compile(
    r"\b(test|testWidgets)\s*(?:\.\s*(skip))?\s*\(\s*r?(['\"`])((?:\\.|(?!\3).)+)\3"
)
MARKER_COMMENT_RE = re.compile(
    r"\b(TODO|FIXME|XXX|HACK|SAFETY|INVARIANT|KEEPER-DEBT)\b", re.I
)
LICENSE_COMMENT_RE = re.compile(
    r"\b(copyright|governed by a bsd-style license|all rights reserved|spdx-license)\b",
    re.I,
)
SPLIT_GLUE_RE = re.compile(
    r"^(?:#\s*include\b|#\s*pragma\s+once\b|import\s+|export\s+|part\s+|using\s+namespace\b"
    r"|namespace(?:\s+[A-Za-z_][\w:]*)?\s*\{?$|(?:\}\s*)?$"
    r"|(?:class|struct|enum(?:\s+class)?)\s+[A-Za-z_]\w*\s*;$)"
)
FUNC_DEF_SKIP_NAMES = {
    "if", "for", "while", "switch", "catch", "return", "sizeof", "alignof",
    "decltype", "static_assert", "DEF_TEST", "DEF_CONDITIONAL_TEST", "UNIX_ONLY_TEST",
    "TEST", "TEST_F", "TEST_P", "TYPED_TEST", "TYPED_TEST_P",
    "test", "testWidgets", "group", "describe",
}


# Collect raw added or deleted lines for a single ChangedFile without diff prefixes
def _collect_file_side_lines(changed_file: ChangedFile, prefix: str) -> list[str]:
    """Return raw lines for prefix ('+' or '-') across all hunks in changed_file."""
    skip_hdr = "+++" if prefix == "+" else "---"
    out: list[str] = []
    for h in changed_file.hunks:
        for raw in h.lines:
            if raw.startswith(prefix) and not raw.startswith(skip_hdr):
                out.append(raw[1:])
    return out


# Scan one line outside #if 0 blocks into active code text and extracted comment chunks
def _scan_line_code_and_comments(raw: str, in_block: bool) -> tuple[str, list[str], bool]:
    """Split raw line into (active_code, comment_chunks, next_in_block) respecting quotes."""
    i = 0
    n = len(raw)
    code_buf: list[str] = []
    comment_chunks: list[str] = []
    in_str = ""
    while i < n:
        if in_block:
            end_idx = raw.find("*/", i)
            if end_idx < 0:
                comment_chunks.append(raw[i:])
                break
            comment_chunks.append(raw[i:end_idx])
            in_block = False
            i = end_idx + 2
            continue
        ch = raw[i]
        if in_str:
            code_buf.append(ch)
            if ch == "\\" and i + 1 < n:
                code_buf.append(raw[i + 1])
                i += 2
                continue
            if ch == in_str:
                in_str = ""
            i += 1
            continue
        if ch in ("'", '"'):
            in_str = ch
            code_buf.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and raw[i + 1] == "/":
            comment_chunks.append(raw[i + 2 :])
            break
        if ch == "/" and i + 1 < n and raw[i + 1] == "*":
            in_block = True
            i += 2
            continue
        code_buf.append(ch)
        i += 1
    return "".join(code_buf), comment_chunks, in_block


# Strip C/C++/Dart comments and #if 0 dead blocks while preserving TODO/invariant markers
def _strip_comments_and_extract_markers(raw_lines: list[str]) -> tuple[list[str], list[str]]:
    """Strip comments and #if 0 bodies from raw_lines; return (code_lines, marker_comments)."""
    code_lines: list[str] = []
    markers: list[str] = []
    in_block = False
    if0_depth = 0
    nested_if_in_if0 = 0

    for raw in raw_lines:
        stripped = raw.strip()
        # Blank #if 0 bodies so disabled blocks never inflate active test or symbol counts
        if not in_block and stripped.startswith("#"):
            if if0_depth == 0 and re.match(r"^#\s*if\s+0\b", stripped):
                if0_depth = 1
                nested_if_in_if0 = 0
                code_lines.append("#if 0")
                continue
            if if0_depth > 0:
                if re.match(r"^#\s*if(?:n?def)?\b", stripped):
                    nested_if_in_if0 += 1
                elif re.match(r"^#\s*endif\b", stripped):
                    if nested_if_in_if0 > 0:
                        nested_if_in_if0 -= 1
                    else:
                        if0_depth = 0
                elif re.match(r"^#\s*el(?:se|if)\b", stripped) and nested_if_in_if0 == 0:
                    if0_depth = 0
                code_lines.append("")
                continue

        code_line, comment_chunks, in_block = _scan_line_code_and_comments(raw, in_block)
        # Record TODO, SAFETY, and invariant comments while skipping standard license headers
        for chunk in comment_chunks:
            norm_c = " ".join(chunk.strip().lstrip("*").split())
            if (
                norm_c
                and MARKER_COMMENT_RE.search(norm_c)
                and not LICENSE_COMMENT_RE.search(norm_c)
            ):
                markers.append(norm_c)
        code_lines.append("" if if0_depth > 0 else code_line)

    return code_lines, markers


# Strip comments and #if 0 dead blocks per hunk so unclosed #if 0 never bleeds across hunks
def _strip_file_hunks(
    changed_file: ChangedFile, prefix: str
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Return (raw_lines, code_lines, hunk_code_blobs, markers) for prefix ('+' or '-') side."""
    skip_hdr = "+++" if prefix == "+" else "---"
    raw_all: list[str] = []
    code_all: list[str] = []
    hunk_blobs: list[str] = []
    markers_all: list[str] = []
    for h in changed_file.hunks:
        side_raw = [
            ln[1:] for ln in h.lines if ln.startswith(prefix) and not ln.startswith(skip_hdr)
        ]
        if not side_raw:
            continue
        raw_all.extend(side_raw)
        h_code, h_markers = _strip_comments_and_extract_markers(side_raw)
        code_all.extend(h_code)
        hunk_blobs.append("\n".join(h_code))
        markers_all.extend(h_markers)
    return raw_all, code_all, hunk_blobs, markers_all


# Identify header include-guard macro names so new split headers do not trigger guard alarms
def _detect_header_guard_macros(raw_lines: list[str], is_header: bool) -> set[str]:
    """Return include-guard macro names declared via #ifndef X / #define X in headers."""
    if not is_header:
        return set()
    guards: set[str] = set()
    non_empty = [ln.strip() for ln in raw_lines if ln.strip() and not ln.strip().startswith("//")]
    for idx in range(len(non_empty) - 1):
        m_ifndef = re.match(r"^#\s*ifndef\s+([A-Za-z_]\w*)\b", non_empty[idx])
        if not m_ifndef:
            continue
        macro = m_ifndef.group(1)
        m_def = re.match(rf"^#\s*define\s+{re.escape(macro)}\b", non_empty[idx + 1])
        if m_def or INCLUDE_GUARD_SUFFIX_RE.match(macro):
            guards.add(macro)
    return guards


# Normalize substantive code lines for cross-file relocation matching during file splits
def _extract_substantive_reloc_lines(
    code_lines: list[str], header_guards: set[str]
) -> tuple[Counter[str], set[str]]:
    """Return (substantive_line_counts, prototype_stems) for relocation ratio scoring."""
    bag: Counter[str] = Counter()
    proto_stems: set[str] = set()
    for raw in code_lines:
        norm = " ".join(raw.strip().split())
        if not norm or norm in ("{", "}", "};", ");", "}; // namespace", "#if 0"):
            continue
        if SPLIT_GLUE_RE.match(norm):
            continue
        # Ignore header include-guard directives when measuring relocated logic lines
        m_guard = re.match(r"^#\s*(?:ifndef|define)\s+([A-Za-z_]\w*)\s*$", norm)
        if m_guard and (
            m_guard.group(1) in header_guards or INCLUDE_GUARD_SUFFIX_RE.match(m_guard.group(1))
        ):
            continue
        if re.match(r"^#\s*endif\b", norm):
            continue
        # Normalize linkage qualifiers that routinely change when extracting helpers across files
        canon = re.sub(r"^(?:static|inline)\s+", "", norm)
        bag[canon] += 1
        # Track function signature stems so new header declarations match moved definitions
        if canon.endswith("{") or canon.endswith(";"):
            stem = canon[:-1].strip()
            if "(" in stem and ")" in stem and not stem.startswith(("return ", "if ", "for ")):
                unqual = re.sub(r"\b[A-Za-z_]\w*::", "", stem)
                proto_stems.add(unqual)
    return bag, proto_stems


# Parse Skia, GoogleTest, and Dart/TS test definitions from comment-stripped hunk code
def _extract_hunk_side_tests(filepath: str, joined: str) -> list[dict]:
    """Return normalized test metadata dicts parsed from one hunk's active code text."""
    found: list[dict] = []
    # Match Skia DEF_TEST, DEF_GANESH_TEST*, DEF_GRAPHITE_TEST*, and UNIX_ONLY_TEST
    for m_sk in SKIA_TEST_MACRO_RE.finditer(joined):
        macro, name = m_sk.group(1), m_sk.group(2)
        if macro.startswith(("DEF_GANESH_TEST", "DEF_GRAPHITE_TEST")) and macro.endswith("_SUITE"):
            continue
        is_dis = name.startswith("DISABLED_")
        clean = name[len("DISABLED_") :] if is_dis else name
        found.append({
            "kind": "skia", "macro": macro, "suite": "", "name": name,
            "clean_name": clean, "disabled": is_dis,
            "label": f"{macro}({name})", "file": filepath,
        })

    # Match GoogleTest / Chromium TEST, TEST_F, TEST_P, and TYPED_TEST*
    for m_gt in GTEST_MACRO_RE.finditer(joined):
        macro, suite, name = m_gt.group(1), m_gt.group(2), m_gt.group(3)
        is_dis = suite.startswith("DISABLED_") or name.startswith("DISABLED_")
        clean_suite = suite[len("DISABLED_") :] if suite.startswith("DISABLED_") else suite
        clean_name = name[len("DISABLED_") :] if name.startswith("DISABLED_") else name
        found.append({
            "kind": "gtest", "macro": macro, "suite": clean_suite, "name": name,
            "clean_name": clean_name, "disabled": is_dis,
            "label": f"{macro}({suite}, {name})", "file": filepath,
        })

    # Match Dart/Flutter and TypeScript test(...) and testWidgets(...)
    if filepath.endswith((".dart", ".ts", ".tsx", ".js")):
        dart_matches = list(DART_TS_TEST_RE.finditer(joined))
        for idx, m_dt in enumerate(dart_matches):
            macro = m_dt.group(1)
            dot_skip = bool(m_dt.group(2))
            desc_name = " ".join(m_dt.group(4).split())
            end_pos = (
                dart_matches[idx + 1].start() if idx + 1 < len(dart_matches) else len(joined)
            )
            tail_window = joined[m_dt.end() : end_pos]
            kw_skip = bool(re.search(r",\s*skip\s*:\s*(?!false\b)\S", tail_window))
            found.append({
                "kind": "dart_ts", "macro": macro, "suite": "", "name": desc_name,
                "clean_name": desc_name, "disabled": dot_skip or kw_skip,
                "label": f"{macro}('{desc_name}')", "file": filepath,
            })
    return found


# Extract Skia, GoogleTest/Chromium, and Dart/TS unit tests from pre-stripped hunk blobs
def _extract_tests_from_hunks(
    code_files: list[ChangedFile], side_cache: dict, prefix: str
) -> list[dict]:
    """Extract test declarations from pre-stripped '+' or '-' hunk blobs across code_files."""
    found: list[dict] = []
    for f in code_files:
        for hunk_blob in side_cache[id(f)][prefix][2]:
            found.extend(_extract_hunk_side_tests(f.path, hunk_blob))
    return found


# Match one base test against remaining patched tests, detecting suite renames or disabling
def _match_patched_test(
    bt: dict, remaining_patched: list[dict], renamed_suites: list[str]
) -> tuple[dict | None, bool]:
    """Pop and return (matched_patched_test, became_disabled_or_narrowed) for bt."""
    # Prefer an exact kind/suite/name/enabled match before suite-rename fallbacks
    for idx, pt in enumerate(remaining_patched):
        if (
            pt["kind"] == bt["kind"]
            and pt["clean_name"] == bt["clean_name"]
            and pt["suite"] == bt["suite"]
            and pt["disabled"] == bt["disabled"]
        ):
            matched = remaining_patched.pop(idx)
            narrowed = bt["kind"] == "skia" and bt["macro"] != matched["macro"]
            return matched, narrowed

    # Allow GoogleTest fixture renames (e.g. RenderTextTest -> RenderTextElideTest) on splits
    if bt["kind"] == "gtest":
        for idx, pt in enumerate(remaining_patched):
            if (
                pt["kind"] == "gtest"
                and pt["clean_name"] == bt["clean_name"]
                and pt["disabled"] == bt["disabled"]
            ):
                matched = remaining_patched.pop(idx)
                renamed_suites.append(f"{bt['label']} -> {matched['label']}")
                return matched, False

    # Detect enabled base tests that reappeared with DISABLED_ or skip: true
    for idx, pt in enumerate(remaining_patched):
        if (
            pt["kind"] == bt["kind"]
            and pt["clean_name"] == bt["clean_name"]
            and not bt["disabled"]
            and pt["disabled"]
        ):
            return remaining_patched.pop(idx), True
    return None, False


# Compare base vs patched test inventories for omissions and silent DISABLED_/skip conversions
def _compare_test_parity(base_tests: list[dict], patched_tests: list[dict]) -> dict:
    """Verify exact test parity between removed ('-') and added ('+') test definitions."""
    remaining_patched = list(patched_tests)
    dropped: list[str] = []
    newly_disabled: list[str] = []
    renamed_suites: list[str] = []

    for bt in base_tests:
        pt, became_disabled = _match_patched_test(bt, remaining_patched, renamed_suites)
        if pt is None:
            dropped.append(f"{bt['label']} ({bt['file']})")
        elif became_disabled:
            newly_disabled.append(f"{bt['label']} -> {pt['label']} ({pt['file']})")

    added = [f"{pt['label']} ({pt['file']})" for pt in remaining_patched]
    return {
        "base_count": len(base_tests),
        "patched_count": len(patched_tests),
        "dropped_tests": dropped,
        "newly_disabled_tests": newly_disabled,
        "added_tests": added,
        "renamed_suites": renamed_suites,
        "ok": not dropped and not newly_disabled and len(base_tests) == len(patched_tests),
    }


# Extract preprocessor guards, defined symbol counts, and TODO/invariant comments per side
def _extract_guards_symbols_and_todos(
    code_files: list[ChangedFile], side_cache: dict, prefix: str
) -> tuple[Counter[str], Counter[str], dict[str, str], Counter[str]]:
    """Return (pp_guard_counts, symbol_counts, symbol_labels, marker_counts) for diff side."""
    guards: Counter[str] = Counter()
    sym_counts: Counter[str] = Counter()
    sym_labels: dict[str, str] = {}
    todos: Counter[str] = Counter()

    type_def_re = re.compile(
        r"\b(?:(?:abstract|base|final|sealed|export)\s+)*"
        r"(?:class|struct|interface|mixin|enum(?:\s+class)?)\s+"
        r"(?:[A-Z0-9_]+_EXPORT\s+|SK_API\s+)?([A-Za-z_]\w*)\b[^;{}]*\{"
    )
    func_def_re = re.compile(
        r"(?:^|[\n};])\s*(?:template\s*<[^>]*>\s*)?"
        r"(?:(?:static|inline|constexpr|virtual|explicit|extern|export|async|function"
        r"|SK_API|[A-Z0-9_]+_EXPORT)\s+)*"
        r"(?:[A-Za-z_~][\w:<>,\s*&?]*?\s+)?([A-Za-z_~]\w*(?:::[A-Za-z_~]\w*)*)"
        r"\s*\([^;{}]*\)\s*(?::\s*[^;{}]+?)?"
        r"\s*(?:const\b|noexcept\b|override\b|final\b|async\*?|sync\*?|\s)*\{"
    )

    for f in code_files:
        raw_lines, code_lines, hunk_blobs, file_markers = side_cache[id(f)][prefix]
        if not raw_lines:
            continue
        hdr_guards = _detect_header_guard_macros(raw_lines, f.is_header)
        for mc in file_markers:
            todos[mc] += 1

        # Scan comment-stripped active lines so /* #ifdef ... */ comments never count as guards
        for code_ln in code_lines:
            s = code_ln.strip()
            if s.startswith("#"):
                m_pp = re.match(r"^#\s*(if|ifdef|ifndef|elif)\b(.*)$", s)
                if m_pp:
                    kind, cond = m_pp.group(1), " ".join(m_pp.group(2).split())
                    cond = re.sub(r"\bdefined\s*\(\s*([A-Za-z_]\w*)\s*\)", r"defined(\1)", cond)
                    # Skip header include-guard macros while preserving feature/platform guards
                    if kind == "ifndef" and (
                        cond in hdr_guards or INCLUDE_GUARD_SUFFIX_RE.match(cond)
                    ):
                        continue
                    guards[f"#{kind} {cond}".strip()] += 1
                continue
            for m_bf in re.finditer(r"\bBUILDFLAG\s*\(\s*([A-Za-z0-9_]+)\s*\)", code_ln):
                guards[f"BUILDFLAG({m_bf.group(1)})"] += 1

        # Extract class/struct/enum and function/method definitions per hunk to track overloads
        for hunk_blob in hunk_blobs:
            for m_t in type_def_re.finditer(hunk_blob):
                tname = m_t.group(1)
                tkey = f"type:{tname}"
                sym_counts[tkey] += 1
                sym_labels.setdefault(tkey, f"type {tname} ({f.path})")
            for m_fn in func_def_re.finditer(hunk_blob):
                qual_name = m_fn.group(1)
                short_name = qual_name.split("::")[-1]
                if short_name in FUNC_DEF_SKIP_NAMES or short_name.startswith("DEF_"):
                    continue
                sym_counts[short_name] += 1
                sym_labels.setdefault(short_name, f"{qual_name}() ({f.path})")

    return guards, sym_counts, sym_labels, todos


# Match a quoted build-file path token against a repository-relative C/C++ file path
def _build_token_matches_file(token: str, build_file_path: str, target_path: str) -> bool:
    """Return True if token in build_file_path refers to target_path."""
    raw = token.strip().strip("\"',")
    if not raw:
        return False
    norm_target = target_path.strip().replace("\\", "/")
    # Repo-root //path/to/file.cc labels must match norm_target from root
    if raw.startswith("//"):
        rooted = raw[2:].lstrip(":").replace(":", "/")
        return rooted == norm_target
    # Skia .gni variables ($_src, $_include, $_modules, $_tests) expand under module/repo dirs
    had_gn_var = bool(re.match(r"^\$_[a-zA-Z0-9_]+/", raw))
    clean = re.sub(r"^\$_[a-zA-Z0-9_]+/", "", raw).lstrip("/:")
    build_dir = os.path.dirname(build_file_path.replace("\\", "/"))
    if had_gn_var:
        if clean != norm_target and not norm_target.endswith("/" + clean):
            return False
        return build_dir in ("", "gn", "bazel") or norm_target.startswith(f"{build_dir}/")
    # Relative BUILD.gn / BUILD.bazel entries resolve directly against build_file_path's directory
    resolved = f"{build_dir}/{clean}" if build_dir else clean
    if resolved == norm_target:
        return True
    return build_dir in ("gn", "bazel") and (
        clean == norm_target or norm_target.endswith("/" + clean)
    )


# Extract C/C++/ObjC++ source tokens added or deleted inside a build file
def _extract_build_side_tokens(build_file: ChangedFile, prefix: str) -> list[str]:
    """Return quoted path strings with C/C++/ObjC++ extensions on prefix side of build_file."""
    tokens: list[str] = []
    for raw in _collect_file_side_lines(build_file, prefix):
        line = raw.split("#", 1)[0]
        for m_q in re.finditer(r"['\"]([^'\"]+)['\"]", line):
            cand = m_q.group(1).strip()
            _, ext = os.path.splitext(cand)
            if ext.lower() in C_BUILD_SOURCE_EXTS:
                tokens.append(cand)
    return tokens


# Check net basename-level symmetry between Skia GN (*.gni / BUILD.gn) and Bazel source lists
def _append_skia_gn_bazel_basename_drift(
    gn_plus: list[tuple[str, str]],
    bz_plus: list[tuple[str, str]],
    gn_minus: list[tuple[str, str]],
    bz_minus: list[tuple[str, str]],
    sync_issues: list[str],
) -> None:
    """Append net GN vs Bazel basename drift messages while ignoring intra-file re-sorts."""
    gn_plus_c = Counter(os.path.basename(tok) for _, tok in gn_plus)
    gn_minus_c = Counter(os.path.basename(tok) for _, tok in gn_minus)
    bz_plus_c = Counter(os.path.basename(tok) for _, tok in bz_plus)
    bz_minus_c = Counter(os.path.basename(tok) for _, tok in bz_minus)
    gn_add_names = set((gn_plus_c - gn_minus_c).keys())
    bz_add_names = set((bz_plus_c - bz_minus_c).keys())
    for extra_gn in sorted(gn_add_names - bz_add_names):
        if not any(extra_gn in msg for msg in sync_issues):
            sync_issues.append(
                f"{extra_gn} added in GN (*.gni / BUILD.gn) without matching Bazel entry"
            )
    for extra_bz in sorted(bz_add_names - gn_add_names):
        if not any(extra_bz in msg for msg in sync_issues):
            sync_issues.append(
                f"{extra_bz} added in Bazel (BUILD.bazel / public.bzl) without GN entry"
            )
    gn_del_names = set((gn_minus_c - gn_plus_c).keys())
    bz_del_names = set((bz_minus_c - bz_plus_c).keys())
    for extra_gn_d in sorted(gn_del_names - bz_del_names):
        if not any(extra_gn_d in msg for msg in sync_issues):
            sync_issues.append(
                f"{extra_gn_d} removed in GN (*.gni / BUILD.gn) without matching Bazel removal"
            )
    for extra_bz_d in sorted(bz_del_names - gn_del_names):
        if not any(extra_bz_d in msg for msg in sync_issues):
            sync_issues.append(
                f"{extra_bz_d} removed in Bazel (BUILD.bazel / public.bzl) without GN removal"
            )


# Audit GN (*.gni, BUILD.gn) and Bazel (BUILD.bazel, public.bzl) source-list symmetry
def _audit_build_graph_parity(cl_data: dict, files: list[ChangedFile]) -> dict:
    """Verify new/moved/deleted C++ files are symmetrically updated in GN and Bazel."""
    new_c_files = [
        f.path for f in files
        if not f.is_build and not f.is_doc
        and os.path.splitext(f.path)[1].lower() in C_BUILD_SOURCE_EXTS
        and (f.is_new_file or f.is_renamed)
    ]
    deleted_c_files = [
        (f.old_path if f.is_renamed and not f.is_deleted_file else f.path)
        for f in files
        if not f.is_build and not f.is_doc
        and os.path.splitext(f.path)[1].lower() in C_BUILD_SOURCE_EXTS
        and (f.is_deleted_file or (f.is_renamed and f.old_path and f.old_path != f.path))
    ]

    build_files = [f for f in files if f.is_build]
    gn_files = [f for f in build_files if f.path.endswith((".gn", ".gni"))]
    bazel_files = [
        f for f in build_files
        if os.path.basename(f.path) in ("BUILD", "BUILD.bazel")
        or f.path.endswith((".bazel", ".bzl"))
    ]

    # Detect Skia dual GN + Bazel build graph without misclassifying Chromium checkouts
    host_low = (cl_data.get("host") or "").lower()
    proj_low = str((cl_data.get("detail") or {}).get("project", "")).lower()
    is_chromium = "chromium" in host_low or proj_low.startswith("chromium")
    is_skia = not is_chromium and (
        "skia" in host_low
        or proj_low == "skia"
        or any(
            f.path.startswith(("gn/", "modules/sk", "src/gpu/ganesh/", "src/gpu/graphite/"))
            or f.path.startswith(("include/core/Sk", "src/core/Sk"))
            or os.path.basename(f.path) in ("BUILD.bazel", "public.bzl")
            for f in files
        )
    )

    gn_plus = [(bf.path, tok) for bf in gn_files for tok in _extract_build_side_tokens(bf, "+")]
    gn_minus = [(bf.path, tok) for bf in gn_files for tok in _extract_build_side_tokens(bf, "-")]
    bz_plus = [(bf.path, tok) for bf in bazel_files for tok in _extract_build_side_tokens(bf, "+")]
    bz_minus = [(bf.path, tok) for bf in bazel_files for tok in _extract_build_side_tokens(bf, "-")]
    all_plus = [(bf.path, tok) for bf in build_files for tok in _extract_build_side_tokens(bf, "+")]
    all_minus = [
        (bf.path, tok) for bf in build_files for tok in _extract_build_side_tokens(bf, "-")
    ]

    unregistered_new: list[str] = []
    stale_deleted: list[str] = []
    sync_issues: list[str] = []

    # Verify every newly added or renamed-to C/C++/ObjC++ file is registered symmetrically
    for nf in new_c_files:
        in_any = any(_build_token_matches_file(tok, bp, nf) for bp, tok in all_plus)
        in_gn = any(_build_token_matches_file(tok, bp, nf) for bp, tok in gn_plus)
        in_bz = any(_build_token_matches_file(tok, bp, nf) for bp, tok in bz_plus)
        if not in_any:
            unregistered_new.append(nf)
        elif is_skia and in_gn != in_bz:
            missing = "BUILD.bazel / public.bzl" if in_gn else "GN (*.gni / BUILD.gn)"
            present = "GN (*.gni / BUILD.gn)" if in_gn else "BUILD.bazel / public.bzl"
            sync_issues.append(f"{nf} added in {present} without {missing}")

    # Verify every deleted or renamed-from C/C++/ObjC++ file is cleaned out of build lists
    for df in deleted_c_files:
        in_any_minus = any(_build_token_matches_file(tok, bp, df) for bp, tok in all_minus)
        in_gn_minus = any(_build_token_matches_file(tok, bp, df) for bp, tok in gn_minus)
        in_bz_minus = any(_build_token_matches_file(tok, bp, df) for bp, tok in bz_minus)
        still_added = any(_build_token_matches_file(tok, bp, df) for bp, tok in all_plus)
        if not in_any_minus or still_added:
            stale_deleted.append(df)
        elif is_skia and in_gn_minus != in_bz_minus:
            left_in = "BUILD.bazel / public.bzl" if in_gn_minus else "GN (*.gni / BUILD.gn)"
            rem_from = "GN (*.gni / BUILD.gn)" if in_gn_minus else "BUILD.bazel / public.bzl"
            sync_issues.append(f"{df} removed from {rem_from} but left in {left_in}")

    if is_skia and (gn_plus or bz_plus or gn_minus or bz_minus):
        _append_skia_gn_bazel_basename_drift(gn_plus, bz_plus, gn_minus, bz_minus, sync_issues)

    return {
        "is_skia": is_skia,
        "unregistered_new_files": unregistered_new,
        "stale_deleted_files": stale_deleted,
        "build_sync_issues": sync_issues,
        "ok": not unregistered_new and not stale_deleted and not sync_issues,
    }


# Measure cross-file substantive line relocation and matched header/test-macro glue
def _measure_cross_file_relocation(
    code_files: list[ChangedFile], side_cache: dict
) -> dict:
    """Compute cross-file moved line counts, ratios, unmatched branch lines, and side tests."""
    leaving_total: Counter[str] = Counter()
    entering_total: Counter[str] = Counter()
    moved_proto_stems: set[str] = set()
    entering_proto_map: list[tuple[str, set[str]]] = []
    total_sub_del = 0
    total_sub_add = 0

    for f in code_files:
        raw_minus, code_minus, _, _ = side_cache[id(f)]["-"]
        raw_plus, code_plus, _, _ = side_cache[id(f)]["+"]
        bag_minus, stems_minus = _extract_substantive_reloc_lines(
            code_minus, _detect_header_guard_macros(raw_minus, f.is_header)
        )
        bag_plus, _ = _extract_substantive_reloc_lines(
            code_plus, _detect_header_guard_macros(raw_plus, f.is_header)
        )
        total_sub_del += sum(bag_minus.values())
        total_sub_add += sum(bag_plus.values())
        leaving_total += bag_minus - bag_plus
        net_enter = bag_plus - bag_minus
        entering_total += net_enter
        moved_proto_stems.update(stems_minus)
        # Collect added header declarations so extracted prototypes credit their moved definitions
        if f.is_header:
            for line_k, cnt in net_enter.items():
                if line_k.endswith(";") and cnt > 0:
                    unqual = re.sub(r"\b[A-Za-z_]\w*::", "", line_k[:-1].strip())
                    entering_proto_map.append((line_k, {unqual}))

    cross_matched = leaving_total & entering_total
    moved_lines = sum(cross_matched.values())
    unmatched_enter = entering_total - cross_matched

    # Credit header prototype declarations that match functions moved out of implementation files
    matched_header_decls = 0
    for line_k, stems in entering_proto_map:
        if unmatched_enter.get(line_k, 0) > 0 and (stems & moved_proto_stems):
            matched_header_decls += 1
            unmatched_enter[line_k] -= 1
            if unmatched_enter[line_k] <= 0:
                del unmatched_enter[line_k]

    # Credit test-macro header renames (e.g. TEST_F fixture split) as relocated test glue
    base_tests = _extract_tests_from_hunks(code_files, side_cache, "-")
    patched_tests = _extract_tests_from_hunks(code_files, side_cache, "+")
    renamed_test_headers = 0
    if base_tests and patched_tests:
        for line_k in list(unmatched_enter.keys()):
            if (
                GTEST_MACRO_RE.search(line_k)
                or SKIA_TEST_MACRO_RE.search(line_k)
                or DART_TS_TEST_RE.search(line_k)
            ):
                renamed_test_headers += unmatched_enter.pop(line_k)

    unmatched_add_lines = sum(unmatched_enter.values())
    unmatched_branch_lines = sum(
        cnt
        for ln, cnt in unmatched_enter.items()
        if re.search(r"\b(if|else\s+if|for|while|switch)\b", ln)
    )
    effective_relocated_add = moved_lines + matched_header_decls + renamed_test_headers
    return {
        "moved_lines": moved_lines,
        "effective_relocated_add": effective_relocated_add,
        "total_sub_add": total_sub_add,
        "total_sub_del": total_sub_del,
        "add_ratio": effective_relocated_add / max(1, total_sub_add),
        "del_ratio": (moved_lines + renamed_test_headers) / max(1, total_sub_del),
        "unmatched_add_lines": unmatched_add_lines,
        "unmatched_branch_lines": unmatched_branch_lines,
        "base_tests": base_tests,
        "patched_tests": patched_tests,
    }


# Compare base vs patched symbols, preprocessor/BUILDFLAG guards, and TODO/invariant comments
def _audit_symbols_guards_and_todos(
    code_files: list[ChangedFile], side_cache: dict
) -> tuple[list[str], list[str], list[str]]:
    """Return (dropped_symbols, dropped_guards, dropped_todos) across code_files."""
    base_guards, base_syms, base_labels, base_todos = _extract_guards_symbols_and_todos(
        code_files, side_cache, "-"
    )
    patch_guards, patch_syms, _, patch_todos = _extract_guards_symbols_and_todos(
        code_files, side_cache, "+"
    )

    dropped_symbols: list[str] = []
    for sym_key in sorted(base_syms.keys()):
        cnt_b = base_syms[sym_key]
        cnt_p = patch_syms.get(sym_key, 0)
        if cnt_p < cnt_b:
            lbl = base_labels[sym_key]
            dropped_symbols.append(
                lbl if cnt_b == 1 else f"{lbl} (base: {cnt_b}, patched: {cnt_p})"
            )

    dropped_guards: list[str] = []
    for g_expr, cnt_b in sorted(base_guards.items()):
        cnt_p = patch_guards.get(g_expr, 0)
        if cnt_p < cnt_b:
            dropped_guards.append(f"{g_expr} (base: {cnt_b}, patched: {cnt_p})")
    # Flag newly introduced #if 0 dead blocks on the patched side even when base had none
    if patch_guards.get("#if 0", 0) > base_guards.get("#if 0", 0):
        dropped_guards.append("#if 0 (newly disabled block on patched side)")

    dropped_todos = [
        td_text for td_text, cnt_b in sorted(base_todos.items())
        if patch_todos.get(td_text, 0) < cnt_b
    ]
    return dropped_symbols, dropped_guards, dropped_todos


# Detect pure file/test split or relocation CLs and run 3-axis deterministic parity verification
def evaluate_split_refactor(
    cl_data: dict, files: list[ChangedFile], is_revert: bool
) -> dict:
    """Detect split_refactor CLs and audit test, symbol/guard/TODO, and build parity."""
    code_files = [f for f in files if not f.is_build and not f.is_doc]
    empty_res = {
        "is_split_refactor": False, "ok": True, "moved_lines": 0,
        "add_relocated_ratio": 0.0, "del_relocated_ratio": 0.0,
        "tests": {
            "base_count": 0, "patched_count": 0, "dropped_tests": [],
            "newly_disabled_tests": [], "added_tests": [], "renamed_suites": [], "ok": True,
        },
        "dropped_symbols": [], "dropped_guards": [], "dropped_todos": [],
        "build": {
            "is_skia": False, "unregistered_new_files": [], "stale_deleted_files": [],
            "build_sync_issues": [], "ok": True,
        },
        "issues": [],
    }
    if is_revert or not code_files:
        return empty_res
    has_rename = any(f.is_renamed for f in code_files)
    if len(code_files) < 2 and not has_rename:
        return empty_res

    # Pre-strip all code hunks once per side so relocation, test, and guard passes share one scan
    side_cache = {
        id(f): {"-": _strip_file_hunks(f, "-"), "+": _strip_file_hunks(f, "+")}
        for f in code_files
    }
    reloc = _measure_cross_file_relocation(code_files, side_cache)
    subj_desc = f"{cl_data.get('subject', '')}\n{cl_data.get('description', '')}"
    split_intent = bool(
        re.search(
            r"\b(split|extract|move|relocat\w*|break\s+up|decompose|factor\s+out|separate)\b",
            subj_desc,
            re.I,
        )
    )
    has_file_churn = any(f.is_new_file or f.is_deleted_file or f.is_renamed for f in code_files)
    min_moved = 5 if (has_file_churn or split_intent) else 8
    is_pure_rename = (
        has_rename
        and reloc["total_sub_add"] <= 2
        and reloc["total_sub_del"] <= 2
        and reloc["unmatched_branch_lines"] == 0
    )
    # Bound unmatched branches and new lines so mixed feature CLs never bypass test gates
    is_split = is_pure_rename or (
        reloc["effective_relocated_add"] >= min_moved
        and reloc["moved_lines"] >= 4
        and reloc["total_sub_add"] > 0
        and reloc["total_sub_del"] > 0
        and reloc["add_ratio"] >= 0.80
        and reloc["del_ratio"] >= 0.65
        and reloc["unmatched_branch_lines"] <= 2
        and reloc["unmatched_add_lines"] <= 12
    )
    if not is_split:
        return empty_res

    # Run deterministic 3-axis parity checks across tests, symbols/guards/TODOs, and build files
    test_parity = _compare_test_parity(reloc["base_tests"], reloc["patched_tests"])
    dropped_symbols, dropped_guards, dropped_todos = _audit_symbols_guards_and_todos(
        code_files, side_cache
    )
    build_parity = _audit_build_graph_parity(cl_data, files)

    issues = (
        [f"Dropped test: {dt}" for dt in test_parity["dropped_tests"]]
        + [f"Test silently disabled: {nd}" for nd in test_parity["newly_disabled_tests"]]
        + [f"Unexpected added test in split: {at}" for at in test_parity["added_tests"]]
        + [f"Dropped preprocessor guard: {dg}" for dg in dropped_guards]
        + [f"Dropped symbol definition: {ds}" for ds in dropped_symbols]
        + [f"Dropped TODO/invariant comment: {td}" for td in dropped_todos]
        + [
            f"New source/header missing from build files: {unf}"
            for unf in build_parity["unregistered_new_files"]
        ]
        + [
            f"Deleted source/header still referenced or unremoved in build files: {sdf}"
            for sdf in build_parity["stale_deleted_files"]
        ]
        + [f"Build graph desync: {bsi}" for bsi in build_parity["build_sync_issues"]]
    )

    add_ratio_out = 1.0 if is_pure_rename else round(reloc["add_ratio"], 3)
    del_ratio_out = 1.0 if is_pure_rename else round(reloc["del_ratio"], 3)
    return {
        "is_split_refactor": True,
        "ok": len(issues) == 0,
        "moved_lines": reloc["moved_lines"],
        "add_relocated_ratio": add_ratio_out,
        "del_relocated_ratio": del_ratio_out,
        "tests": test_parity,
        "dropped_symbols": dropped_symbols,
        "dropped_guards": dropped_guards,
        "dropped_todos": dropped_todos,
        "build": build_parity,
        "issues": issues,
    }


PERF_TEXT_INTENT_RE = re.compile(
    r"\b(perf|performance|optimi[sz]\w*|speed\s*up|faster|fast[- ]path|slow[- ]path"
    r"|latency|throughput|zero[- ]alloc|avoid\s+(?:copy|copies|alloc\w*|recomput\w*|redundant)"
    r"|memoiz\w*|coalesc\w*|prealloc\w*|microbenchmark\w*|benchmark\w*"
    r"|txt_benchmarks|nanobench|skpbench|crossbench|pinpoint|frame\s*time)\b",
    re.I,
)
BENCH_METRIC_NUMBER_RE = re.compile(
    r"(?:[-+]?\d+(?:\.\d+)?\s*(?:%|ms|us|µs|ns|fps|x\s+faster|x\s+speedup))", re.I
)
PERF_TRAILER_LINE_RE = re.compile(
    r"^\s*(?:Tested|Test|Bug|Fixed|Fixes|Change-Id|Reviewed-on|Reviewed-by"
    r"|Commit-Queue|Auto-Submit|Assisted-by|Cq-Include-Trybots)\s*:",
    re.I,
)
CACHE_BUGFIX_GUARD_RE = re.compile(
    r"\b(fix\w*|bug\w*|stale|invalidat\w*|corrupt\w*|crash\w*|leak\w*|uaf|asan|regress\w*)\b",
    re.I,
)
ABLATION_EVIDENCE_RE = re.compile(
    r"\b(ablat\w*|a\s*\+\s*b|isolated\s+contribution|per[- ]mechanism"
    r"|without\s+(?:the\s+)?(?:cache|fast[- ]path|rewrite)"
    r"|(?:baseline|base)\b[\s\S]{0,240}?\b(?:only|alone|variant\s*[ab12])\b"
    r"|\bcombined\b[\s\S]{0,120}?\bonly\b|\bonly\b[\s\S]{0,120}?\bcombined\b)\b",
    re.I,
)
BENCH_NAME_TOKEN_RE = re.compile(
    r"\b(txt_benchmarks|nanobench|skpbench|Crossbench|browser-startup|FirstPaint"
    r"|ReloadButton\.FCP|NonEmptyPaint3|Speedometer\d*|MotionMark"
    r"|[A-Za-z0-9_]*(?:Bench|Benchmark)[A-Za-z0-9_]*)\b"
)


# Classify distinct optimization mechanisms present in an incoming performance diff
def _detect_perf_mechanisms(
    joined_plus: str,
    joined_minus: str,
    code_targets: list[ChangedFile],
    has_cache_code: bool,
) -> list[str]:
    """Return mechanism tags detected across cache, fast-path, loop, container, and task axes."""
    mechanisms: list[str] = []
    if has_cache_code:
        mechanisms.append("caching_or_memoization")
    # Exclude cache-hit early returns so a single cache lookup never double-counts as fast-path
    early_ret_matches = [
        m.group(0)
        for m in re.finditer(r"\bif\s*\([^\n;{]{1,120}\)\s*\{?\s*return\b", joined_plus)
    ]
    has_non_cache_early_return = any(
        not has_cache_code
        or not re.search(r"\b(cache|memo\w*|hit|find|lookup)\b", stmt, re.I)
        for stmt in early_ret_matches
    )
    if has_non_cache_early_return or re.search(
        r"(?:FastPath|\bfast_path\b|\bFeatureList::IsEnabled\b|\bBASE_FEATURE\b)",
        joined_plus,
    ):
        mechanisms.append("fast_path_or_early_return")
    if (
        re.search(r"\b(for|while)\s*\(", joined_plus)
        and re.search(r"\b(for|while)\s*\(", joined_minus)
    ) or re.search(r"\bstd::(?:ranges::|lower_bound|binary_search)\b", joined_plus):
        mechanisms.append("loop_or_algorithm_rewrite")
    if re.search(
        r"(?:\.\s*reserve\s*\(|\b(?:SkSTArray|STArray|AutoSTMalloc|AutoSTArray|InlinedVector"
        r"|FixedFlatMap|std::string_view)\b)",
        joined_plus,
    ):
        mechanisms.append("allocation_or_container_tuning")
    if re.search(
        r"\b(SingleThreadTaskRunner|PostTask|TaskPriority|BEST_EFFORT|USER_BLOCKING"
        r"|BarrierClosure)\b",
        joined_plus,
    ):
        mechanisms.append("async_or_task_scheduling")
    if len(code_targets) >= 3 and sum(f.added + f.deleted for f in code_targets) >= 100:
        mechanisms.append("bundled_refactor_or_cleanup")
    return mechanisms


# Evaluate incoming external performance CLs for multi-mechanism bundling and guard alignment
def evaluate_perf_ablation_gate(
    cl_data: dict, files: list[ChangedFile], is_revert: bool, is_split_refactor: bool
) -> dict:
    """Flag incoming external perf CLs that bundle 2+ mechanisms without ablation or guard proof."""
    default_gate = {
        "is_perf_cl": False, "triggered": False, "multi_mechanism_unablated": False,
        "mechanisms": [], "has_ablation_data": False, "has_benchmark_numbers": False,
        "cited_benchmarks": [], "code_guards": [], "warnings": [],
    }
    if is_revert or is_split_refactor:
        return default_gate
    owner_name = str(cl_data.get("owner") or "").strip()
    owner_email = str(cl_data.get("owner_email") or "").strip()
    if is_self_author({"name": owner_name, "email": owner_email}) or owner_name.lower() == "rusino":
        return default_gate
    code_targets = [f for f in files if (f.is_impl or f.is_header) and not f.is_bench]
    if not code_targets:
        return default_gate

    subj_desc = f"{cl_data.get('subject', '')}\n{cl_data.get('description', '')}"
    # Strip Tested:/Bug: trailers before checking intent so non-regression gates do not misfire
    intent_prose = "\n".join(
        ln for ln in subj_desc.splitlines() if not PERF_TRAILER_LINE_RE.match(ln)
    )
    touches_bench = any(f.is_bench for f in files)

    added_code: list[str] = []
    deleted_code: list[str] = []
    code_guards: list[str] = []
    for f in code_targets:
        _, c_plus, _, _ = _strip_file_hunks(f, "+")
        _, c_minus, _, _ = _strip_file_hunks(f, "-")
        added_code.extend(c_plus)
        deleted_code.extend(c_minus)
        for raw_ln in c_plus:
            s = " ".join(raw_ln.strip().split())
            m_if = re.match(r"^(if\s*\([^;{]{3,90}\))", s)
            if m_if and m_if.group(1) not in code_guards:
                code_guards.append(m_if.group(1))
            m_pp = re.match(r"^(#\s*if(?:n?def)?\s+.+)$", s)
            if m_pp and m_pp.group(1) not in code_guards:
                code_guards.append(m_pp.group(1))

    joined_plus = "\n".join(added_code)
    joined_minus = "\n".join(deleted_code)
    has_cache_code = bool(
        re.search(
            r"\b(Cache|LRUCache|THashMap|THashSet|flat_hash_map|unordered_map|DenseMap"
            r"|SupportsUserData|fCached\w*|cached_\w*|\w+_cache_)\b",
            joined_plus,
        )
    )
    is_cache_perf_intent = (
        has_cache_code
        and bool(re.search(r"\bcach(?:e|ing)\b", intent_prose, re.I))
        and not CACHE_BUGFIX_GUARD_RE.search(intent_prose)
    )
    if (
        not PERF_TEXT_INTENT_RE.search(intent_prose)
        and not is_cache_perf_intent
        and not touches_bench
    ):
        return default_gate

    mechanisms = _detect_perf_mechanisms(joined_plus, joined_minus, code_targets, has_cache_code)
    cited_benchmarks: list[str] = []
    for m_bn in BENCH_NAME_TOKEN_RE.finditer(subj_desc):
        if m_bn.group(1) not in cited_benchmarks:
            cited_benchmarks.append(m_bn.group(1))

    has_numbers = bool(BENCH_METRIC_NUMBER_RE.search(subj_desc))
    has_ablation = bool(ABLATION_EVIDENCE_RE.search(subj_desc))
    multi_unablated = len(mechanisms) >= 2 and not has_ablation

    # Build actionable reviewer warnings for unablated bundling, missing numbers, or guard checks
    warnings: list[str] = []
    if multi_unablated:
        mech_list = ", ".join(mechanisms)
        warnings.append(
            f"Bundles {len(mechanisms)} distinct mechanisms ({mech_list}) without isolated "
            "ablation data (baseline vs A vs B vs A+B); require per-mechanism numbers or split CLs."
        )
    if not has_numbers:
        warnings.append(
            "Claims a performance optimization without quantitative benchmark results in the "
            "commit description."
        )
    if cited_benchmarks and code_guards:
        warnings.append(
            f"Verify cited benchmark(s) ({', '.join(cited_benchmarks[:4])}) actually hit the "
            f"modified runtime guard condition(s) ({'; '.join(code_guards[:3])})."
        )
    elif code_guards:
        warnings.append(
            "Verify benchmark inputs actually exercise the modified guard condition(s): "
            f"{'; '.join(code_guards[:3])}."
        )

    return {
        "is_perf_cl": True,
        "triggered": bool(multi_unablated or not has_numbers),
        "multi_mechanism_unablated": multi_unablated,
        "mechanisms": mechanisms,
        "has_ablation_data": has_ablation,
        "has_benchmark_numbers": has_numbers,
        "cited_benchmarks": cited_benchmarks[:6],
        "code_guards": code_guards[:6],
        "warnings": warnings,
    }

def analyze_review(cl_data, files):
    """Analyze changed files and metadata to classify review and pinpoint core logic."""
    total_added = sum(f.added for f in files)
    total_deleted = sum(f.deleted for f in files)
    total_files = len(files)

    impl_files = [f for f in files if f.is_impl]
    test_files = [f for f in files if f.is_test]
    header_files = [f for f in files if f.is_header]
    doc_files = [f for f in files if f.is_doc]
    build_files = [f for f in files if f.is_build]

    impl_churn = sum(f.added + f.deleted for f in impl_files)
    test_churn = sum(f.added + f.deleted for f in test_files)

    domain_files = [f for f in files if f.domain_affinity]
    subject = cl_data.get("subject", "").strip()
    desc = cl_data.get("description", "").strip()

    in_domain = len(domain_files) > 0 or any(k in subject.lower() for k in USER_DOMAIN_KEYWORDS)
    is_revert = bool(re.search(r'^revert\b', subject, re.I))

    # Reassign check runs before split/perf profiles so out-of-domain CLs stay 30s reassignments
    is_reassign = False
    if not in_domain and len(domain_files) == 0:
        non_domain_matches = [
            f for f in files if any(nd in f.path.lower() for nd in NON_DOMAIN_HINTS)
        ]
        if len(non_domain_matches) == total_files or (len(impl_files) > 0 and not in_domain):
            is_reassign = True

    # Run deterministic split/refactor parity and external-perf ablation checks on in-domain CLs
    split_parity = evaluate_split_refactor(cl_data, files, is_revert or is_reassign)
    is_split_refactor = bool(split_parity.get("is_split_refactor"))
    ablation_gate = evaluate_perf_ablation_gate(
        cl_data, files, is_revert or is_reassign, is_split_refactor
    )
    review_profile = "split_refactor" if is_split_refactor else "standard"

    # Hygiene flags (suppress false missing-test/scope alarms on splits and out-of-domain CLs)
    is_poor_description = (
        len(desc) < 35 or bool(re.match(r"^(fix|update|clean|refactor|fix bug)\.?$", desc, re.I))
    )
    is_missing_tests = (
        not is_split_refactor
        and not is_reassign
        and impl_churn > 75
        and len(test_files) == 0
        and not is_revert
        and not any(f.is_doc for f in files)
    )
    is_toxic_scope = (
        not is_split_refactor
        and not is_reassign
        and total_files > 15
        and impl_churn > 350
        and (len(test_files) == 0 or is_poor_description)
    )

    # Mechanical flags (never auto-approve split_refactor or external perf CLs as blind MECHANICAL)
    all_hunks = [h for f in files for h in f.hunks]
    avg_complexity = (
        (sum(h.complexity_score for h in all_hunks) / len(all_hunks)) if all_hunks else 0
    )
    is_pure_mechanical = False

    if is_revert:
        is_pure_mechanical = True
    elif not is_split_refactor and not ablation_gate["is_perf_cl"]:
        if total_files > 3 and avg_complexity < 2.5 and impl_churn > 0:
            is_pure_mechanical = True
        elif total_files >= 1 and impl_churn == 0 and (
            len(doc_files) + len(build_files) == total_files
        ):
            is_pure_mechanical = True
        elif re.search(r"\b(roll|bump|lsc|format|clang-format|typo)\b", subject, re.I):
            is_pure_mechanical = True

    # Domain-specific architectural analysis
    arch_violations = []
    sound_patterns = []

    patch_text = cl_data.get("patch", "")
    file_paths = [f.path for f in files]
    touches_skparagraph = any("modules/skparagraph" in p for p in file_paths)

    if touches_skparagraph:
        desc_full = (subject + "\n" + desc).lower()
        # SkParagraph is an abstract 2D layout engine with zero knowledge of concrete
        # document backends (like PDF). It does not support PDF or backend-specific text scraping.
        backend_keywords = ["pdf", "skpdf", "actualtext", "svg canvas", "tounicode"]
        matched_backends = [kw for kw in backend_keywords if re.search(r'\b' + re.escape(kw) + r'\b', desc_full)]

        if matched_backends or "allocRunTextPos" in patch_text:
            reason_detail = (
                f"coupling with {', '.join(matched_backends)}" if matched_backends 
                else "smuggling text/clusters into drawing primitives (allocRunTextPos)"
            )
            arch_violations.append(
                f"Backend Coupling in SkParagraph ({reason_detail}): SkParagraph is an abstract 2D text layout "
                f"engine with zero coupling to concrete document backends (like PDF). It does not know about or "
                f"support PDF. Document-level semantics, tagging (/ActualText), and structure belong at the "
                f"document/application layer using the Paragraph object directly, not by contorting SkParagraph "
                f"internals or contaminating its ephemeral canvas drawing cache."
            )

        # Sound domain patterns in SkParagraph
        if any("OneLineShaper.cpp" in p for p in file_paths) and ("kControl" in patch_text or "CR LF" in subject):
            sound_patterns.append("Sound Shaper optimization: Bypasses redundant font fallback for control/whitespace clusters (aligns with Flutter).")
        if any("ParagraphCache.cpp" in p for p in file_paths) and "fUnresolvedGlyphs" in patch_text:
            sound_patterns.append("Sound Cache fix: Restores unresolved glyph/codepoint diagnostic metrics on ParagraphCache hits.")

    # Triage decision logic
    if is_revert:
        category = "MECHANICAL"
        reason = "Clean revert/rollback of a previous commit (usually triggered by test failures or tree breakages)."
    elif arch_violations:
        category = "PUSHBACK"
        reason = f"Architectural layer inversion: {arch_violations[0]}"
    elif is_toxic_scope or (is_missing_tests and impl_churn >= 60):
        category = "PUSHBACK"
        reason = f"High implementation churn ({impl_churn} lines) without test coverage or mixed unstructured scope."
    elif is_reassign:
        category = "REASSIGN"
        reason = "All touched files belong to subsystems outside text, layout, typography, and SkParagraph."
    elif is_pure_mechanical:
        category = "MECHANICAL"
        reason = "Diff is a mechanical refactoring, code formatting, rename, or build configuration update."
    elif is_split_refactor:
        category = "SURGICAL_AUDIT"
        if split_parity["ok"]:
            pct = int(round(split_parity["add_relocated_ratio"] * 100))
            reason = (
                f"Pure split/refactor CL ({split_parity['moved_lines']} relocated lines, "
                f"{pct}% add parity): deterministic test/guard/build parity passed; "
                "verify semantic equivalence without flagging moved legacy code."
            )
        else:
            issue_cnt = len(split_parity["issues"])
            reason = (
                f"Split/refactor parity alert ({issue_cnt} issue(s) across "
                f"{split_parity['moved_lines']} relocated lines): {split_parity['issues'][0]}"
            )
    else:
        category = "SURGICAL_AUDIT"
        reason = "Core domain logic modification requiring surgical manual inspection of critical mutation points."
        if ablation_gate["multi_mechanism_unablated"]:
            mech_cnt = len(ablation_gate["mechanisms"])
            reason = (
                f"External perf CL bundling {mech_cnt} mechanisms without isolated ablation "
                "(baseline vs A vs B vs A+B); verify per-mechanism wins and guard conditions."
            )
        elif ablation_gate["triggered"]:
            reason = (
                "External perf CL missing quantitative benchmark evidence; verify measured win "
                "and runtime guard conditions."
            )

    # Rank hunks for surgical audit
    ranked_hunks = []
    for f in impl_files:
        for h in f.hunks:
            ranked_hunks.append((h.complexity_score, f, h))
    # If no impl hunks, check header hunks
    if not ranked_hunks:
        for f in header_files:
            for h in f.hunks:
                ranked_hunks.append((h.complexity_score, f, h))

    ranked_hunks.sort(key=lambda x: x[0], reverse=True)
    top_hunks = ranked_hunks[:3]
    # Attach corpus requirement metadata so stop_guard can block unverified approvals
    corpus_gate = detect_required_review_corpus(files)

    return {
        "category": category,
        "reason": reason,
        "review_profile": review_profile,
        "split_parity": split_parity,
        "ablation_gate": ablation_gate,
        "total_files": total_files,
        "total_added": total_added,
        "total_deleted": total_deleted,
        "impl_files_count": len(impl_files),
        "test_files_count": len(test_files),
        "impl_churn": impl_churn,
        "test_churn": test_churn,
        "in_domain": in_domain,
        "is_revert": is_revert,
        "is_poor_description": is_poor_description,
        "is_missing_tests": is_missing_tests,
        "arch_violations": arch_violations,
        "sound_patterns": sound_patterns,
        "top_hunks": top_hunks,
        "corpus_gate": corpus_gate,
    }

def _is_active_ldap(email: str) -> bool:
    ldap = email.split("@", 1)[0].strip().lower()
    if not ldap or not os.path.exists("/usr/bin/id"):
        return True
    try:
        res = subprocess.run(
            ["id", "-u", ldap],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return True


def suggest_reviewer(files):
    """Suggest active alternative reviewers based on path hints."""
    path_map = {
        "gpu/ganesh/vk": ("Vulkan Backend Team", ["sunnyps@google.com", "egdaniel@google.com"]),
        "gpu/ganesh/mtl": ("Metal Backend Team", ["michaelludwig@google.com", "egdaniel@google.com"]),
        "gpu/graphite": ("Graphite Team", ["robertphillips@google.com", "michaelludwig@google.com"]),
        "bazel": ("Infra / Build Team", ["borenet@google.com"]),
        "gn": ("Build Team", ["borenet@google.com"]),
        "flutter/engine/shell": ("Flutter Engine Team", ["cbracken@google.com"]),
        "flutter/lib/web_ui": ("Flutter Web Team", ["yjbanov@google.com", "mdebbar@google.com"]),
    }
    for f in files:
        for k, (team, candidates) in path_map.items():
            if k in f.path.lower():
                active = [c for c in candidates if _is_active_ldap(c)]
                return team, (", ".join(active) if active else "relevant subsystem owner")
    return "Subsystem Owner", "relevant subsystem owner"

# Render deterministic 3-axis parity report block for split_refactor CLs
def _render_split_parity_section(split_parity: dict) -> list[str]:
    """Build Markdown lines for the Split / Refactor 100% Parity Audit section."""
    if not split_parity or not split_parity.get("is_split_refactor"):
        return []
    lines: list[str] = ["### 🧩 Split / Refactor 100% Parity Audit (`split_refactor`)"]
    add_pct = int(round(split_parity["add_relocated_ratio"] * 100))
    del_pct = int(round(split_parity["del_relocated_ratio"] * 100))
    status = (
        "✅ **PASS (100% deterministic hunk parity)**"
        if split_parity["ok"]
        else f"🚨 **FAIL ({len(split_parity['issues'])} parity defect(s) detected)**"
    )
    lines.append(
        f"- **Status:** {status} · "
        + (
            "pure file rename (`100%` path/build parity)"
            if split_parity["moved_lines"] == 0 and add_pct == 100
            else (
                f"`{split_parity['moved_lines']}` relocated substantive lines "
                f"(`{add_pct}%` `+` / `{del_pct}%` `-` match)"
            )
        )
    )

    tp = split_parity["tests"]
    t_badge = "✅" if tp["ok"] else "🚨"
    lines.append(
        f"- **1. Test Counter Parity:** {t_badge} base `{tp['base_count']}` vs "
        f"patched `{tp['patched_count']}`"
    )
    for dt in tp["dropped_tests"]:
        lines.append(f"  - 🚨 Dropped test: `{dt}`")
    for nd in tp["newly_disabled_tests"]:
        lines.append(f"  - 🚨 Silently disabled / narrowed test: `{nd}`")
    for at in tp["added_tests"]:
        lines.append(f"  - 🚨 Unexpected new test mixed into split: `{at}`")
    for rs in tp["renamed_suites"]:
        lines.append(f"  - ℹ️ Suite renamed: `{rs}`")

    sym_ok = (
        not split_parity["dropped_guards"]
        and not split_parity["dropped_symbols"]
        and not split_parity["dropped_todos"]
    )
    s_badge = "✅" if sym_ok else "🚨"
    lines.append(f"- **2. Symbol, `#if`/`BUILDFLAG` Guard & `TODO` Parity:** {s_badge}")
    for dg in split_parity["dropped_guards"]:
        lines.append(f"  - 🚨 Dropped/disabled preprocessor guard: `{dg}`")
    for ds in split_parity["dropped_symbols"]:
        lines.append(f"  - 🚨 Dropped symbol definition: `{ds}`")
    for td in split_parity["dropped_todos"]:
        lines.append(f"  - 🚨 Dropped `TODO`/invariant comment: `{td}`")

    bp = split_parity["build"]
    b_badge = "✅" if bp["ok"] else "🚨"
    b_scope = "Skia GN + Bazel" if bp["is_skia"] else "Build Lists"
    lines.append(f"- **3. Build Graph Parity ({b_scope}):** {b_badge}")
    for unf in bp["unregistered_new_files"]:
        lines.append(f"  - 🚨 Unregistered new/moved file: `{unf}`")
    for sdf in bp["stale_deleted_files"]:
        lines.append(f"  - 🚨 Deleted/moved-from file not cleaned from build lists: `{sdf}`")
    for bsi in bp["build_sync_issues"]:
        lines.append(f"  - 🚨 GN vs Bazel desync: `{bsi}`")

    lines.append(
        "> **Audit Directive (`split_refactor`):** Verify 100% token/semantic equivalence, "
        "`#include`/header-guard hygiene, and build registration across split files. Do **NOT** "
        "generate false-positive bug reports on unchanged relocated code.\n"
    )
    return lines


# Render deterministic Ablation Gate report block for incoming external performance CLs
def _render_ablation_gate_section(ablation_gate: dict) -> list[str]:
    """Build Markdown lines for the Incoming Perf Ablation Gate section."""
    if not ablation_gate or not ablation_gate.get("is_perf_cl"):
        return []
    lines: list[str] = ["### 🧪 Incoming Perf Ablation Gate"]
    badge = (
        "🚨 **TRIGGERED (Require Per-Mechanism Ablation / Benchmark Proof)**"
        if ablation_gate["triggered"]
        else "ℹ️ **External Perf CL — Verify Benchmark & Guard Alignment**"
    )
    mechs = ", ".join(f"`{m}`" for m in ablation_gate["mechanisms"]) or "`single_mechanism`"
    num_str = "✅ Present" if ablation_gate["has_benchmark_numbers"] else "❌ Missing"
    abl_str = "✅ Present" if ablation_gate["has_ablation_data"] else "❌ Missing"
    lines.append(f"- **Gate Status:** {badge}")
    lines.append(
        f"- **Mechanisms (`{len(ablation_gate['mechanisms'])}`):** {mechs} · "
        f"**Numbers:** {num_str} · **Ablation (`baseline` / `A` / `B` / `A+B`):** {abl_str}"
    )
    if ablation_gate["cited_benchmarks"]:
        benches = ", ".join(f"`{b}`" for b in ablation_gate["cited_benchmarks"])
        lines.append(f"- **Cited Benchmarks:** {benches}")
    if ablation_gate["code_guards"]:
        guards = "; ".join(f"`{g}`" for g in ablation_gate["code_guards"][:4])
        lines.append(f"- **Modified Runtime Guards:** {guards}")
    for w in ablation_gate["warnings"]:
        lines.append(f"  - ⚠️ {w}")
    lines.append("")
    return lines


def generate_report(cl_data, analysis, files):
    """Generate Markdown report with concrete actions and drafts."""
    cat = analysis["category"]
    cat_badges = {
        "MECHANICAL": "🟢 `[MECHANICAL / ROUTINE]`",
        "REASSIGN": "🟡 `[REASSIGN / OUT OF DOMAIN]`",
        "SURGICAL_AUDIT": "🔴 `[SURGICAL AUDIT / CORE DOMAIN]`",
        "PUSHBACK": "🟠 `[PUSHBACK / HYGIENE REQUIRED]`"
    }

    md = []
    title = cl_data.get("subject", "CL " + cl_data.get("id", ""))
    md.append(f"## Review Triage: {title}\n")
    md.append(f"**Verdict:** {cat_badges.get(cat, cat)}  \n")
    md.append(f"**Why:** {analysis['reason']}\n")

    # Metrics Summary Table
    md.append("### 📊 Churn & Profile")
    md.append("| Metric | Value | Detail |")
    md.append("|---|---|---|")
    md.append(f"| **Author** | `{cl_data.get('owner', 'Unknown')}` | {cl_data.get('owner_email', '')} |")
    md.append(f"| **Scope** | `{analysis['total_files']} file(s)` | `+{analysis['total_added']} / -{analysis['total_deleted']}` lines |")
    md.append(f"| **Implementation Churn** | `{analysis['impl_churn']} lines` across {analysis['impl_files_count']} impl file(s) |")
    review_profile = analysis.get("review_profile") or "standard"
    split_parity = analysis.get("split_parity") or {}
    ablation_gate = analysis.get("ablation_gate") or {}
    if review_profile == "split_refactor":
        prof_val = "🧩 `split_refactor`"
        prof_det = (
            "100% parity audit passed"
            if split_parity.get("ok")
            else f"🚨 {len(split_parity.get('issues') or [])} parity defect(s)"
        )
    elif ablation_gate.get("is_perf_cl"):
        prof_val = "🧪 `standard` + `Ablation Gate`"
        prof_det = (
            "🚨 Multi-mechanism / unmeasured perf CL"
            if ablation_gate.get("triggered")
            else "External perf CL (verify guard coverage)"
        )
    else:
        prof_val = "`standard`"
        prof_det = "Core 5% mutation audit"
    md.append(f"| **Review Profile** | {prof_val} | {prof_det} |")
    if analysis["test_files_count"] > 0:
        test_icon = "✅"
    elif review_profile == "split_refactor":
        test_icon = "➖ (Relocation)"
    else:
        test_icon = "❌ (Missing!)"
    md.append(
        f"| **Tests Present** | {test_icon} | {analysis['test_files_count']} test file(s), "
        f"`{analysis['test_churn']} lines` |"
    )
    domain_str = "🎯 In-Domain (Text/Paragraph)" if analysis["in_domain"] else "🌐 Out-of-Domain"
    md.append(f"| **Domain Affinity** | {domain_str} | Evaluated against text, paragraph & layout |")
    tctx = cl_data.get("thread_context") or {}
    if tctx.get("current_ps"):
        last_ps = tctx.get("last_reviewed_ps")
        votes = ", ".join(f"`{v}`" for v in tctx.get("self_votes") or [])
        if tctx.get("reviewed_current_ps"):
            ps_detail = "**Already reviewed by you on this patchset**" + (
                f" (your votes: {votes})" if votes else ""
            )
        elif last_ps:
            ps_detail = (
                f"Follow-up since your review on **PS{last_ps}** "
                f"(`{tctx['last_reviewed_sha'][:10]}`)"
            )
            if votes:
                ps_detail += f"; your votes carried over: {votes}"
        else:
            ps_detail = "Initial review (no prior comments or votes from you)"
        md.append(f"| **Patchset** | `PS{tctx['current_ps']}` | {ps_detail} |")
    # Emit machine-readable corpus gate marker for stop_guard.py on non-revert CLs
    corpus_gate = analysis.get("corpus_gate") or {}
    if corpus_gate.get("required") and not analysis.get("is_revert"):
        md.append(
            f"| **Flutter Corpus Gate** | 🚨 **REQUIRED (`{corpus_gate['script']}`)** | "
            f"Touches `{corpus_gate['domain']}` (`{os.path.basename(corpus_gate['files'][0])}`) |"
        )
    md.append("")

    # Surface prominent corpus gate instructions before the follow-up thread
    if corpus_gate.get("required") and not analysis.get("is_revert"):
        md.append("### 🚨 Mandatory Flutter Corpus Gate (`[REVIEW CORPUS GUARD]`)")
        md.append(
            f"> **Hard Gate:** This CL modifies executable production code in "
            f"**{corpus_gate['domain']}** "
            f"({', '.join(f'`{p}`' for p in corpus_gate['files'][:4])}).  \n"
            f"> Approving (`LGTM`, `Case 1`, `Case 2`, `Case 3`) is **mechanically blocked "
            f"by `stop_guard.py`** until you check out the author's exact patchset and run "
            f"`{corpus_gate['command']}` with exit code 0 (unless the user explicitly opted "
            f"out of corpus verification).\n"
        )

    # Surface deterministic split/refactor parity and external-perf ablation blocks
    md.extend(_render_split_parity_section(split_parity))
    md.extend(_render_ablation_gate_section(ablation_gate))

    open_threads = tctx.get("open_threads") or []
    if open_threads:
        md.append(f"### 🧵 Open threads ({len(open_threads)} unresolved)")
        if any(t.get("root_role") != "you" for t in open_threads):
            md.append(
                "> **Per-Thread Verdict Required:** Give each unresolved thread from another "
                "reviewer an explicit verdict (`valid`, `already addressed in PS<n>` with "
                "`file:line`, or `question for the user`)."
            )
        for t in open_threads:
            tags = " ".join(f"`[{tg}]`" for tg in (t.get("tags") or []))
            tag_prefix = f"{tags} " if tags else ""
            email_str = f" (`{t['root_email']}`)" if t.get("root_email") else ""
            ps_str = f"`PS{t['patch_set']}` vs current `PS{t['current_ps']}`"
            if t.get("stale"):
                ps_str += f" *(posted when `PS{t['newest_ps_at_post']}` existed)*"
            if t.get("reply_count", 0) > 0:
                last_str = f"last reply: **{t['last_role']}** (`{t['last_author']}`)"
            else:
                last_str = f"no replies (last: **{t['last_role']}**)"
            md.append(
                f"- {tag_prefix}`{t['location']}` — **{t['root_author']}**{email_str} "
                f"on {ps_str} · {last_str}"
            )
            md.append(f"  - Root: \"{t['root_excerpt']}\"")
            if t.get("reply_count", 0) > 0 and t.get("last_excerpt"):
                md.append(f"  - Last reply (`{t['last_role']}`): \"{t['last_excerpt']}\"")
        md.append("")

    if tctx.get("last_reviewed_ps") or tctx.get("dialogue") or tctx.get("reviewed_current_ps"):
        md.append("### 🔄 Follow-Up Patchset & Prior Review Thread")
        if tctx.get("reviewed_current_ps"):
            # The code under review has not changed since the user's own review, so a
            # delta audit would re-review code she already approved or commented on
            md.append(
                f"> **Already Reviewed (`PS{tctx['current_ps']}`):** You already reviewed the "
                f"current patchset. Do not re-audit the code; only check messages posted after "
                f"your review and what still blocks submit (votes, CQ, unresolved threads)."
            )
        elif tctx.get("last_reviewed_ps"):
            prev_ps = tctx["last_reviewed_ps"]
            cur_ps = tctx["current_ps"]
            prev_sha = tctx["last_reviewed_sha"][:10]
            cur_sha = tctx["current_sha"][:10]
            repo_host = (cl_data.get("host") or "").replace("-review.", ".")
            proj = (cl_data.get("detail") or {}).get("project", "skia")
            md.append(
                f"> **Follow-Up Mode (`PS{prev_ps}` → `PS{cur_ps}`):** You already reviewed "
                f"`PS{prev_ps}` (`{prev_sha}`). Audit the delta `git diff {prev_sha} {cur_sha}` "
                f"and verify that your prior points were addressed. **Never** contradict your "
                f"own earlier requests."
            )
            if repo_host and tctx.get("last_reviewed_ref") and tctx.get("current_ref"):
                md.append(
                    f"> Fetch delta: `git -c http.extraheader= -c http.cookiefile= fetch "
                    f"https://{repo_host}/{proj} {tctx['last_reviewed_ref']} "
                    f"{tctx['current_ref']} && git diff {prev_sha} {cur_sha}`"
                )
        if tctx.get("companion_urls"):
            urls_str = ", ".join(f"<{u}>" for u in tctx["companion_urls"])
            md.append(
                f"> **Companion CL Fold-In:** Prior thread references {urls_str}. "
                f"Do **NOT** ask the author to split out or revert code that you asked them "
                f"to fold in from your companion CL."
            )
        if tctx.get("dialogue"):
            md.append("\n**Recent Review Thread:**")
            md.append("```text")
            for entry in tctx["dialogue"]:
                md.append(entry)
            md.append("```")
        md.append("")

    commit_msg = (cl_data.get("commit_message") or cl_data.get("description") or "").strip()
    if commit_msg and "\n" in commit_msg:
        md.append("### 📜 Commit Message (Current Patchset)")
        md.append(
            "*Read before drafting comments — never ask the author to document behavior "
            "that is already explained below.*"
        )
        md.append("```text")
        md.append(commit_msg)
        md.append("```\n")

    # Actionable Deliverable per Category
    md.append("---")
    md.append("### 🎯 Concrete Next Actions\n")

    if (
        cl_data.get("id")
        and "skia" in (cl_data.get("host") or "")
        and cat in ("SURGICAL_AUDIT", "PUSHBACK")
    ):
        advisor_py = "~/.gemini/config/skills/skia-canary-advisor/scripts/canary_advisor.py"
        md.append(
            f"> **🛡️ Mandatory ASAN & Canary Gate (`[ASAN/CANARY GUARD]`):**\n"
            f"> Run `python3 {advisor_py} advise --cl {cl_data['id']}`\n"
            "> and execute the printed `AGENT ACTION` command"
            " (`canary_advisor.py try ...`, which must end with `TRIGGER: VERIFIED`)"
            " to auto-trigger ASAN and"
            " external canaries.\n"
        )

    if cat == "MECHANICAL":
        md.append("> **Your Action (15–30 seconds):** Confirm this is a safe mechanical update/revert and approve.")
        md.append("\n**Agent Verification Summary:**")
        if analysis["is_revert"]:
            md.append("- This is a rollback CL. Reason from author: revert to restore tree / tests.")
        else:
            md.append(f"- Scanned {analysis['total_files']} files. No complex control flow branching or hidden logic found.")
            md.append("- Changes follow uniform refactor, formatting, or config bump.")
        md.append("\n**Ready-to-post Review Comment (English):**")
        md.append("```markdown")
        if analysis["is_revert"]:
            md.append("LGTM for rollback.")
        else:
            md.append("LGTM. Verified mechanical churn; no unintended side effects.")
        md.append("```")

    elif cat == "REASSIGN":
        team, reviewer = suggest_reviewer(files)
        md.append(f"> **Your Action (30 seconds):** Reassign to `{reviewer}` ({team}) and remove yourself.")
        md.append("\n**Ready-to-post Reassignment Note (English):**")
        md.append("```markdown")
        author_name = cl_data.get('owner', '').split()[0] if cl_data.get('owner') else "Author"
        md.append(f"Hi {author_name}, this change is outside my primary focus area (text/layout). Reassigning to @{reviewer.split('@')[0]} ({team}) for review.")
        md.append("```")

    elif cat == "SURGICAL_AUDIT":
        md.append("> **Your Action (3–5 minutes):** Inspect ONLY the pinpointed core mutation lines below with your own eyes. Ignore the remaining files.")
        if analysis.get("sound_patterns"):
            md.append("\n> **💡 Expert Domain Assessment:** " + " ".join(analysis["sound_patterns"]) + " **(Sound & safe to approve)**")
        md.append("\n#### 🔬 Pinpointed Core Logic (The 5% That Matters)")
        
        if analysis["top_hunks"]:
            host = cl_data.get("host")
            cl_id = cl_data.get("id")
            project = cl_data.get("detail", {}).get("project", "skia") if isinstance(cl_data.get("detail"), dict) else "skia"
            for idx, (score, f, h) in enumerate(analysis["top_hunks"], 1):
                start_line, end_line = changed_line_range(h)
                func_hint = f" in `{h.header}`" if h.header else ""
                if host and cl_id:
                    # Without the patchset segment Gerrit treats the first path component as one
                    ps = cl_data.get("patchset") or "current"
                    code_url = f"https://{host}/c/{project}/+/{cl_id}/{ps}/{f.path}#{start_line}"
                elif cl_data.get("repo_url") and cl_data.get("pr_number"):
                    code_url = f"{cl_data['repo_url']}/pull/{cl_data['pr_number']}/files"
                else:
                    code_url = f"file://{os.path.abspath(f.path)}#L{start_line}-L{end_line}"
                md.append(f"**{idx}. [{os.path.basename(f.path)}:{start_line}–{end_line}]({code_url})**{func_hint} *(Complexity Score: {score})*")
                md.append("```cpp")
                sample_lines = [l for l in h.lines if l.startswith("+") or l.startswith("-")][:12]
                md.append("\n".join(sample_lines))
                if len(h.lines) > 12:
                    md.append("// ... (truncated for focus)")
                md.append("```\n")
        else:
            md.append("No concentrated logic hunk found; inspect the implementation files listed in the profile.")

        if review_profile == "split_refactor":
            md.append("#### 🧩 Split / Refactor Parity Checklist to Verify:")
            md.append(
                "1. **Omission & Test Parity:** Confirm 0 test cases, helper functions/classes, "
                "`#if` / `BUILDFLAG(...)` guards, or `TODO`/invariant comments dropped."
            )
            md.append(
                "2. **Semantic & Lifetime Equivalence:** Confirm moved bodies preserve assertion "
                "fidelity, initialization/destruction order, and anonymous-namespace scoping "
                "without filing bugs on unchanged legacy lines."
            )
            md.append(
                "3. **Build Graph & Header Hygiene:** Confirm `gn/*.gni`, `BUILD.gn`, "
                "`BUILD.bazel`, and `public.bzl` match symmetrically and new headers are "
                "self-contained."
            )
        else:
            md.append("#### 🛡️ Invariants Checklist to Verify in These Lines:")
            md.append(
                "1. **Zero-Allocation Hot Path:** Are any dynamic memory allocations (`new`, "
                "`malloc`, `std::string` copies, vector resizes) introduced on the layout or "
                "paint path?"
            )
            md.append(
                "2. **Boundary & Fail-Fast Honesty:** What happens if the text is empty, length "
                "is 0, or dimensions are zero / NaN?"
            )
            md.append(
                "3. **BiDi & Directionality:** Does this logic hold across RTL/LTR runs and "
                "surrogate pair boundaries?"
            )
            md.append(
                "4. **Cache & State Invalidation:** If styling or font metrics change, is cached "
                "geometry properly invalidated?"
            )

    elif cat == "PUSHBACK":
        md.append("> **Your Action (1 minute):** Send the constructive pushback note below to request proper scoping or tests before deep review.")
        md.append("\n**Identified Hygiene Issues:**")
        if analysis.get("arch_violations"):
            for v in analysis["arch_violations"]:
                md.append(f"- **Architectural Layer Inversion:** {v}")
        if analysis["is_missing_tests"] and not analysis.get("arch_violations"):
            md.append(f"- Substantial logic change (`{analysis['impl_churn']}` lines of implementation) with **0 unit tests**.")
        if analysis["is_poor_description"]:
            md.append("- Description lacks context, rationale, or invariant guarantees.")
        if analysis["total_files"] > 15:
            md.append(f"- High diff volume ({analysis['total_files']} files) mixing refactoring with logic.")
            
        md.append("\n**Ready-to-post Pushback Draft (English):**")
        md.append("```markdown")
        if analysis.get("arch_violations"):
            md.append("Thanks for the patch, but we cannot accept this change into SkParagraph.\n\n"
                      "SkParagraph is an abstract 2D text layout engine with zero knowledge of, or coupling to, specific document backends like PDF. It does not support or integrate with PDF:\n"
                      "1. Architectural separation: SkParagraph lays out text into glyph runs and draws them to an abstract SkCanvas. The SkTextBlob built in Run::copyTo is strictly an internal, ephemeral display cache for rasterization (via allocRunPos). It was never meant to be a semantic transport container for downstream document backends.\n"
                      "2. Structured document export: In systems using SkParagraph (such as Flutter or Chromium), semantic structure, selectable text, and PDF features like /ActualText or ToUnicode are handled at the document/scene/accessibility layer using the Paragraph object directly (where complete UTF-8 strings, cluster indices, and text spans are natively preserved).\n"
                      "3. Hot-path overhead: Populating allocRunTextPos introduces cluster scanning loops, UTF-8 buffer copies, and significant memory bloat to every cached TextBlob across all normal screen painting paths just to accommodate PDF text scraping.\n\n"
                      "If PDF requires ActualText or document structure, that belongs at the document/canvas integration layer, not by modifying SkParagraph internals.")
        elif analysis["is_missing_tests"]:
            md.append(f"Hi! Could you please add unit test coverage asserting this behavior before review? With {analysis['impl_churn']} lines of implementation changes, we need automated regression tests covering the expected invariants and boundary cases.")
        else:
            md.append("Hi! Could you please split this change into two separate CLs? One for the preparatory refactoring/cleanup, and a second focused CL with the actual behavioral change and unit tests. Right now the semantic changes are obscured by the diff size, making safe review difficult.")
        md.append("```")

    ledger_md = analysis.get("refuted_ledger_markdown", "")
    if ledger_md:
        md.append("\n---\n")
        md.append(ledger_md.rstrip())

    return "\n".join(md)

def main():
    parser = argparse.ArgumentParser(description="Automated Code Review Triage and Action Engine")
    parser.add_argument("target", nargs="?", default="", help="Gerrit URL / CL ID, Piper CL (cl/123456), GitHub PR URL, or local git ref")
    parser.add_argument("--gerrit-host", default="skia-review.googlesource.com", help="Default Gerrit host")
    parser.add_argument("--diff-file", help="Path to raw diff or patch file")
    parser.add_argument("--json", action="store_true", help="Output raw JSON analysis")
    args = parser.parse_args()

    target = args.target.strip()
    cl_data = {}
    patch_text = ""

    if args.diff_file:
        with open(args.diff_file, "r", encoding="utf-8", errors="replace") as f:
            patch_text = f.read()
        cl_data = {
            "source": "Local File",
            "id": os.path.basename(args.diff_file),
            "subject": f"Patch from {args.diff_file}",
            "owner": "Local",
            "description": "Local diff",
            "patch": patch_text
        }
    elif target.startswith("http://") or target.startswith("https://"):
        if "googlesource.com" in target:
            host_m = re.search(r'https?://([^/]+)', target)
            cl_m = re.search(r'/\+/(\d+)', target)
            host = host_m.group(1) if host_m else args.gerrit_host
            cl_id = cl_m.group(1) if cl_m else ""
            if not cl_id:
                cl_m = re.search(r'/changes/(\d+)', target)
                cl_id = cl_m.group(1) if cl_m else ""
            cl_data = probe_gerrit(cl_id, host)
            if not cl_data:
                print(f"Error: Could not retrieve Gerrit CL {cl_id} from {host}", file=sys.stderr)
                sys.exit(1)
            patch_text = cl_data.get("patch", "")
        elif "github.com" in target and "/pull/" in target:
            m = re.search(r'(https://github\.com/[^/]+/[^/]+)/pull/(\d+)', target)
            if m:
                repo_url, pr_num = m.group(1), m.group(2)
                cl_data = fetch_github_pr(repo_url, pr_num)
                patch_text = cl_data.get("patch", "")
    elif target.startswith("cl/"):
        cl_data = fetch_google3_cl(target)
        patch_text = cl_data.get("patch", "")
    elif target.isdigit():
        # Check Gerrit first
        cl_data = probe_gerrit(target, args.gerrit_host)
        if not cl_data:
            # Fall back to Piper
            cl_data = fetch_google3_cl(target)
        if cl_data:
            patch_text = cl_data.get("patch", "")
    elif not target and not sys.stdin.isatty():
        patch_text = sys.stdin.read()
        cl_data = {
            "source": "Stdin",
            "id": "stdin",
            "subject": "Diff from stdin",
            "owner": "Local",
            "description": "Stdin diff",
            "patch": patch_text
        }
    else:
        git_target = target if target else "HEAD"
        try:
            patch_text = subprocess.check_output(
                ["git", "show", git_target],
                stderr=subprocess.STDOUT,
                text=True,
                timeout=15
            )
            subj = subprocess.check_output(
                ["git", "log", "-1", "--format=%s", git_target],
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5
            ).strip()
            owner = subprocess.check_output(
                ["git", "log", "-1", "--format=%an", git_target],
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5
            ).strip()
            cl_data = {
                "source": "Local Git",
                "id": git_target,
                "subject": subj,
                "owner": owner,
                "description": subj,
                "patch": patch_text
            }
        except Exception as e:
            print(f"Error resolving target '{target}': {e}", file=sys.stderr)
            sys.exit(1)

    if not patch_text or patch_text.startswith("Error"):
        print(f"Failed to obtain patch: {patch_text}", file=sys.stderr)
        sys.exit(1)

    files = parse_patch(patch_text)
    analysis = analyze_review(cl_data, files)

    detail_dict = cl_data.get("detail") if isinstance(cl_data.get("detail"), dict) else {}
    repo_hint = (
        detail_dict.get("project")
        or cl_data.get("project")
        or cl_data.get("repo_url")
        or "."
    )
    touched_paths = [f.path for f in files if getattr(f, "path", "")]
    ledger_path, ledger_entries = finding_ledger.query_entries(
        repo=repo_hint,
        files=touched_paths or None,
        cl=str(cl_data.get("id", "")),
    )
    analysis["refuted_ledger"] = ledger_entries
    analysis["refuted_ledger_markdown"] = finding_ledger.format_query_markdown(
        ledger_entries, ledger_path=ledger_path, include_empty_header=True
    )

    out_payload = {
        "metadata": {k: v for k, v in cl_data.items() if k != "patch"},
        "analysis": {k: v for k, v in analysis.items() if k != "top_hunks"},
    }
    report_md = generate_report(cl_data, analysis, files)

    # Persist hunk-level corpus gate requirement for Flight Deck before save-review runs
    cl_url = ""
    if cl_data.get("repo_url") and cl_data.get("pr_number"):
        cl_url = f"{cl_data['repo_url']}/pull/{cl_data['pr_number']}"
    elif cl_data.get("host") and cl_data.get("id"):
        proj = detail_dict.get("project", "skia")
        cl_url = f"https://{cl_data['host']}/c/{proj}/+/{cl_data['id']}"
    if cl_url:
        try:
            triage_store.save_from_triage_cl_output(cl_url, out_payload, report_md)
        except (OSError, ValueError, TypeError):
            pass

    if args.json:
        print(json.dumps(out_payload, indent=2))
    else:
        print(report_md)

if __name__ == "__main__":
    main()
