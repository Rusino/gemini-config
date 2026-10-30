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

import argparse
import base64
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
    return (
        author_obj.get("email") in REVIEWER_EMAILS
        or author_obj.get("name") in REVIEWER_NAMES
    )


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

    owner_email = (detail.get("owner") or {}).get("email", "")
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
        role = (
            "YOU"
            if is_self_author(author)
            else ("AUTHOR" if author.get("email") == owner_email else "REVIEWER")
        )
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
                role = (
                    "YOU"
                    if is_self_author(author)
                    else ("AUTHOR" if author.get("email") == owner_email else "REVIEWER")
                )
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
        self.hunks = []
        self.added = 0
        self.deleted = 0
        self.is_test = bool(re.search(r'(test|Test|\btests/|_test\.dart|_test\.cpp|_gm\.cpp)', path, re.I))
        self.is_build = bool(re.search(r'(BUILD|\.gn|\.bazel|\.bzl|CMakeLists|DEPS)', path))
        self.is_doc = bool(re.search(r'(\.md|\.rst|\.txt|OWNERS|METADATA)', path))
        self.is_header = bool(re.search(r'\.(h|hpp|hxx)$', path))
        self.is_impl = bool(re.search(r'\.(c|cc|cpp|cxx|dart|py|java|ts|js|go)$', path)) and not self.is_test
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
    """Parse unified git diff into ChangedFile objects with hunks."""
    files = []
    current_file = None
    current_hunk = None

    for line in patch_text.splitlines():
        # Match file diff headers
        m_git = re.search(r'^diff --git a/(.*?) b/(.*)$', line)
        m_piper = re.search(r'^==== //depot/google3/(.*?)#', line)
        m_idx = re.search(r'^Index: (.*)$', line)

        if m_git:
            filepath = m_git.group(2).strip()
            current_file = ChangedFile(filepath)
            files.append(current_file)
            current_hunk = None
            continue
        elif m_piper:
            filepath = m_piper.group(1).strip()
            current_file = ChangedFile(filepath)
            files.append(current_file)
            current_hunk = None
            continue
        elif m_idx:
            filepath = m_idx.group(1).strip()
            current_file = ChangedFile(filepath)
            files.append(current_file)
            current_hunk = None
            continue

        # Fallback for diff output without git header (e.g. +++ b/path)
        if line.startswith("+++ b/") or line.startswith("+++ "):
            filepath = line.replace("+++ b/", "").replace("+++ ", "").strip()
            if not current_file or current_file.path != filepath:
                current_file = ChangedFile(filepath)
                files.append(current_file)
                current_hunk = None
            continue

        # Match hunk header: @@ -old_start,old_len +new_start,new_len @@ header
        if line.startswith("@@ "):
            m_hunk = re.search(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)', line)
            if m_hunk and current_file:
                old_start = int(m_hunk.group(1))
                old_len = int(m_hunk.group(2)) if m_hunk.group(2) else 1
                new_start = int(m_hunk.group(3))
                new_len = int(m_hunk.group(4)) if m_hunk.group(4) else 1
                header = m_hunk.group(5).strip()
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
                if re.search(r'\b(if|else if|for|while|switch|case|return)\b', content):
                    current_hunk.complexity_score += 4
                # State mutations
                if re.search(r'(=|\+=|-=|\*=|reset|clear|insert|erase|emplace|push_back)', content):
                    current_hunk.complexity_score += 2
                # Allocations & heavy types
                if re.search(r'\b(new|malloc|make_unique|make_shared|std::vector|std::string)\b', content):
                    current_hunk.complexity_score += 5
                # Invariants & checks
                if re.search(r'\b(SkASSERT|assert|CHECK|DCHECK)\b', content):
                    current_hunk.complexity_score += 2
            elif line.startswith("-") and not line.startswith("---"):
                current_hunk.deleted += 1
                current_file.deleted += 1
                current_hunk.complexity_score += 1

    return files

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

    # Hygiene flags
    is_poor_description = len(desc) < 35 or re.match(r'^(fix|update|clean|refactor|fix bug)\.?$', desc, re.I)
    is_missing_tests = (impl_churn > 75 and len(test_files) == 0 and not is_revert and not any(f.is_doc for f in files))
    is_toxic_scope = (total_files > 15 and impl_churn > 350 and (len(test_files) == 0 or is_poor_description))

    # Mechanical flags
    all_hunks = [h for f in files for h in f.hunks]
    avg_complexity = (sum(h.complexity_score for h in all_hunks) / len(all_hunks)) if all_hunks else 0
    is_pure_mechanical = False

    if is_revert:
        is_pure_mechanical = True
    elif total_files > 3 and avg_complexity < 2.5 and impl_churn > 0:
        is_pure_mechanical = True
    elif total_files >= 1 and impl_churn == 0 and (len(doc_files) + len(build_files) == total_files):
        is_pure_mechanical = True
    elif re.search(r'\b(roll|bump|lsc|format|clang-format|typo)\b', subject, re.I):
        is_pure_mechanical = True

    # Reassign check
    is_reassign = False
    if not in_domain and len(domain_files) == 0:
        non_domain_matches = [f for f in files if any(nd in f.path.lower() for nd in NON_DOMAIN_HINTS)]
        if len(non_domain_matches) == total_files or (len(impl_files) > 0 and not in_domain):
            is_reassign = True

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
    else:
        category = "SURGICAL_AUDIT"
        reason = "Core domain logic modification requiring surgical manual inspection of critical mutation points."

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
    test_icon = "✅" if analysis["test_files_count"] > 0 else "❌ (Missing!)"
    md.append(f"| **Tests Present** | {test_icon} | {analysis['test_files_count']} test file(s), `{analysis['test_churn']} lines` |")
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

    if cl_data.get("id") and "skia" in (cl_data.get("host") or "") and cat in ("SURGICAL_AUDIT", "PUSHBACK"):
        md.append(f"> **🛡️ Mandatory ASAN & Canary Gate (`[ASAN/CANARY GUARD]`):**\n"
                  f"> Run `python3 ~/.gemini/config/skills/skia-canary-advisor/scripts/canary_advisor.py advise --cl {cl_data['id']}`\n"
                  f"> and execute the printed `AGENT ACTION` command (`git cl try ...`) to auto-trigger ASAN and external canaries.\n")

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

        md.append("#### 🛡️ Invariants Checklist to Verify in These Lines:")
        md.append("1. **Zero-Allocation Hot Path:** Are any dynamic memory allocations (`new`, `malloc`, `std::string` copies, vector resizes) introduced on the layout or paint path?")
        md.append("2. **Boundary & Fail-Fast Honesty:** What happens if the text is empty, length is 0, or dimensions are zero / NaN?")
        md.append("3. **BiDi & Directionality:** Does this logic hold across RTL/LTR runs and surrogate pair boundaries?")
        md.append("4. **Cache & State Invalidation:** If styling or font metrics change, is cached geometry properly invalidated?")

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

    if args.json:
        out = {
            "metadata": {k: v for k, v in cl_data.items() if k != "patch"},
            "analysis": {k: v for k, v in analysis.items() if k != "top_hunks"}
        }
        print(json.dumps(out, indent=2))
    else:
        print(generate_report(cl_data, analysis, files))

if __name__ == "__main__":
    main()
