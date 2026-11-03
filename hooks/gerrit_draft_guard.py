#!/usr/bin/env python3
"""gerrit_draft_guard.py — block turns that show a Gerrit review reply without saving a draft.

Enforces the invariant that any prepared English review comment/reply for another author's
Gerrit CL (whether via full review-triage, companion-cl-review, or an incremental
`[CL-WATCHER]` patchset/comment follow-up) is saved as an unpublished Gerrit draft via
`scripts/gerrit_draft.py save` before ending the turn.
"""

from __future__ import annotations

import json
import os
import re
import shlex

USER_REQUEST_RE = re.compile(r"<USER_REQUEST>([\s\S]*?)</USER_REQUEST>")
EXIT_CODE_RE = re.compile(r"Command exited with code (\d+)")
FENCED_OR_QUOTE_BLOCK_RE = re.compile(
    r"```[\s\S]{12,}?```|(?:^>\s*\S[^\n]*\n?){2,}", re.MULTILINE
)
FENCED_MASK_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`")

GERRIT_URL_CL_RE = re.compile(
    r"https?://(?P<host>[a-z0-9.-]*-review\.(?:googlesource|git\.corp\.google)\.com"
    r"|(?:www\.)?(?:crrev|skrev)\.com)/"
    r"(?:c/(?:[^\s+#)]+?/\+/|)|)(?P<cl>\d{5,8})(?:/(?P<ps>\d+))?",
    re.IGNORECASE,
)
WATCHER_GERRIT_KEY_RE = re.compile(
    r"\bgerrit:(\d{5,8})\b(?:[^\n]{0,40}?\bPS\s?(\d+))?", re.IGNORECASE
)
DRAFT_SAVED_TOKEN_RE = re.compile(r"\bGERRIT_DRAFT_SAVED\b[^\n]*?\bcl=(\d{5,8})\b")
DRAFT_SAVED_JSON_RE = re.compile(
    r'"status"\s*:\s*"GERRIT_DRAFT_SAVED"[\s\S]{0,200}?"cl"\s*:\s*(\d{5,8})'
)
REST_PUT_DRAFTS_RE = re.compile(r"changes/(\d{5,8})/revisions/\d+/drafts\b")
AUTH_ERROR_RE = re.compile(
    r"(?:missing_luci_token|git-credential-luci[^\n]*(?:reauth|login|failed)"
    r"|Gerrit HTTP (?:401|403)\b)",
    re.IGNORECASE,
)
AUTH_DISCLOSURE_RE = re.compile(
    r"(?:reauth|git-credential-luci|luci-auth\s+login|авторизац|токен)",
    re.IGNORECASE,
)

USER_DRAFT_OPTOUT_RE = re.compile(
    r"(?:без\s+(?:драфт\w*|черновик\w*|компаньон\w*\s+и\s+драфт\w*)"
    r"|не\s+(?:клади|сохраняй|пиши)\s+в\s+(?:драфт|черновик|gerrit)"
    r"|только\s+(?:текстом\s+)?в\s+чате"
    r"|сух(?:ой|им)\s+прогон\w*"
    r"|\bdry[\s-]+(?:run|mode)\b"
    r"|\b(?:skip|no|without)\s+(?:gerrit\s+)?draft\b"
    r"|\bdo\s+not\s+save\s+(?:a\s+)?(?:gerrit\s+)?draft\b)",
    re.IGNORECASE,
)

PREPARED_REPLY_PROSE_RE = re.compile(
    r"(?:подготовила\s+черновик\s+(?:ответа|комментария|ревью)"
    r"|черновик\s+(?:ответа|комментария)\s*(?:для|на|в|:)"
    r"|текст\s+ответа\s+для\s+Gerrit"
    r"|можешь\s+опубликовать\s+его\s+в\s+Gerrit"
    r"|\bprepared?\s+(?:a\s+|the\s+)?(?:review\s+)?draft\s+(?:reply|comment)\b"
    r"|\bdraft\s+(?:review\s+)?(?:reply|comment)\s*(?:for|on|:))",
    re.IGNORECASE,
)

PENDING_CORPUS_REPLY_RE = re.compile(
    r"(?:UNVERIFIED\s+IN\s+(?:FLUTTER|GOOGLE3)"
    r"|корпус[^\n]{0,80}(?:ещё\s+бежит|выполняется|ждём|не\s+заверш)"
    r"|аппрув\s+пока\s+не\s+сохран"
    r"|no\s+approving\s+draft\s+is\s+saved)",
    re.IGNORECASE,
)

SELF_EMAIL_PREFIXES = ("jlavrova@",)


def _watcher_state_path() -> str:
    env_p = os.environ.get("JETSKI_CL_WATCHER_STATE_FILE", "").strip()
    if env_p:
        return env_p
    return os.path.expanduser("~/.gemini/config/logs/cl_watcher_state.json")


def _lookup_watcher_cl(cl_id: str) -> dict | None:
    path = _watcher_state_path()
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    items = data.get("items")
    if not isinstance(items, dict):
        return None
    exact = items.get(f"gerrit:{cl_id}")
    if isinstance(exact, dict):
        return exact
    prefix = f"gerrit:{cl_id}@"
    for k, val in items.items():
        if k.startswith(prefix) and isinstance(val, dict):
            return val
    return None


def _is_self_owned_cl(cl_id: str, transcript_blobs: list[str]) -> bool:
    rec = _lookup_watcher_cl(cl_id)
    if isinstance(rec, dict):
        if rec.get("is_self") is True:
            return True
        owner = str(rec.get("owner") or rec.get("author") or "").strip().lower()
        if owner.startswith(SELF_EMAIL_PREFIXES):
            return True
        if rec.get("is_self") is False and owner:
            return False

    owner_pat = re.compile(
        rf"(?:CL\s+{re.escape(cl_id)}[^\n]*owner\s+|Author:\s*[^\n<]*<?)"
        r"([A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+)",
        re.IGNORECASE,
    )
    for blob in transcript_blobs:
        if cl_id not in blob:
            continue
        for m in owner_pat.finditer(blob):
            if m.group(1).lower().startswith(SELF_EMAIL_PREFIXES):
                return True
    return False


# Parse `triage_store.py save-review` invocations outside comments/grep
def _parse_save_review_calls(cmd: str) -> list[dict]:
    if "triage_store.py" not in cmd or "save-review" not in cmd:
        return []
    found: list[dict] = []
    for seg in re.split(r"&&|\|\||;|\n", cmd):
        seg = seg.strip()
        if not seg or "triage_store.py" not in seg or "save-review" not in seg:
            continue
        if re.match(r"^(?:grep|egrep|fgrep|rg|echo|cat)\b", seg):
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        try:
            sr_idx = toks.index("save-review")
        except ValueError:
            continue
        rest = toks[sr_idx + 1 :]
        entry = {
            "target": "",
            "kind": "gerrit",
            "kind_explicit": False,
            "patchset": "",
            "outcome": "",
            "corpus": "auto",
            "has_draft_arg": False,
        }
        i = 0
        while i < len(rest):
            t = rest[i]
            nxt = rest[i + 1] if i + 1 < len(rest) else ""
            if t == "--target" and nxt:
                entry["target"] = nxt
                i += 2
                continue
            if t == "--kind" and nxt:
                entry["kind"] = nxt.lower()
                entry["kind_explicit"] = True
                i += 2
                continue
            if t == "--patchset" and nxt:
                entry["patchset"] = nxt
                i += 2
                continue
            if t == "--outcome" and nxt:
                entry["outcome"] = nxt.upper()
                i += 2
                continue
            if t == "--corpus" and nxt:
                entry["corpus"] = nxt.lower()
                i += 2
                continue
            if t in ("--draft", "--draft-file") and nxt.strip():
                entry["has_draft_arg"] = True
                i += 2
                continue
            i += 1
        m_cl = re.search(r"(\d{5,8})", entry["target"])
        if entry["kind"] == "gerrit" and m_cl:
            entry["cl"] = m_cl.group(1)
            found.append(entry)
    return found


def _extract_pending_draft_cmd_cls(cmd: str) -> set[str]:
    cls: set[str] = set()
    for seg in re.split(r"&&|\|\||;|\n", cmd):
        seg = seg.strip()
        if not seg or re.match(r"^(?:grep|egrep|fgrep|rg|echo|cat)\b", seg):
            continue
        if "gerrit_draft.py" in seg and re.search(r"\bsave\b", seg):
            for m in GERRIT_URL_CL_RE.finditer(seg):
                cls.add(m.group("cl"))
            for m in re.finditer(r"--cl(?:=|\s+)(?:gerrit:)?(\d{5,8})\b", seg):
                cls.add(m.group(1))
        elif "PUT" in seg and "/drafts" in seg:
            for m in REST_PUT_DRAFTS_RE.finditer(seg):
                cls.add(m.group(1))
    return cls


def _has_gerrit_anchor(
    cl_id: str,
    kind_explicit: bool,
    transcript_blobs: list[str],
    final_reply: str,
) -> bool:
    # Require positive Gerrit evidence when --kind was omitted so GitHub PRs are ignored
    if kind_explicit:
        return True
    if isinstance(_lookup_watcher_cl(cl_id), dict):
        return True
    for src in [final_reply, *transcript_blobs]:
        if cl_id not in src:
            continue
        if any(m.group("cl") == cl_id for m in GERRIT_URL_CL_RE.finditer(src)):
            return True
        if any(m.group(1) == cl_id for m in WATCHER_GERRIT_KEY_RE.finditer(src)):
            return True
    return False


def gerrit_draft_guard_reason(transcript_path: str) -> str:
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""
    steps: list[dict] = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("step_index") is not None:
                    steps.append(obj)
    except OSError:
        return ""
    if not steps:
        return ""
    steps.sort(key=lambda s: int(s["step_index"]))

    last_user_idx = -1
    last_user_req = ""
    all_user_reqs: list[str] = []
    for st in steps:
        if st.get("type") == "USER_INPUT":
            last_user_idx = int(st["step_index"])
            raw = str(st.get("content") or "")
            m = USER_REQUEST_RE.search(raw)
            last_user_req = m.group(1) if m else raw
            all_user_reqs.append(last_user_req)

    if any(USER_DRAFT_OPTOUT_RE.search(r) for r in all_user_reqs):
        return ""

    saved_cls: set[str] = set()
    auth_failed = False
    save_review_calls: list[dict] = []
    pending_save_cls: list[set[str]] = []
    final_reply = ""
    transcript_blobs: list[str] = [last_user_req]

    for st in steps:
        idx = int(st["step_index"])
        stype = st.get("type")
        if stype == "GENERIC":
            content = str(st.get("content") or "")
            transcript_blobs.append(content[:4000])
            if idx <= last_user_idx:
                continue
            if "\nFile Path: " in content[:200] or content.startswith("Showing lines "):
                continue
            for m in DRAFT_SAVED_TOKEN_RE.finditer(content):
                saved_cls.add(m.group(1))
            for m in DRAFT_SAVED_JSON_RE.finditer(content):
                saved_cls.add(m.group(1))
            if AUTH_ERROR_RE.search(content):
                auth_failed = True
            # Pop on RUNNING background tasks too so later synchronous commands stay aligned
            is_cmd_result = (
                bool(EXIT_CODE_RE.search(content))
                or st.get("status") in ("ERROR", "RUNNING")
                or "Tool is running as a background task" in content
            )
            if pending_save_cls and is_cmd_result:
                cand = pending_save_cls.pop(0)
                m_exit = EXIT_CODE_RE.search(content)
                if (
                    cand
                    and m_exit
                    and int(m_exit.group(1)) == 0
                    and st.get("status") != "ERROR"
                    and "ERROR:" not in content
                ):
                    saved_cls.update(cand)
        elif stype == "PLANNER_RESPONSE" and idx > last_user_idx:
            txt = str(st.get("content") or "")
            if txt.strip():
                final_reply = txt
            for tc in st.get("tool_calls") or []:
                if not isinstance(tc, dict) or tc.get("name") != "run_command":
                    continue
                tc_args = tc.get("args")
                cmd = str(tc_args.get("CommandLine") or "") if isinstance(tc_args, dict) else ""
                save_review_calls.extend(_parse_save_review_calls(cmd))
                pending_save_cls.append(_extract_pending_draft_cmd_cls(cmd))

    if auth_failed and AUTH_DISCLOSURE_RE.search(final_reply):
        return ""

    masked_reply = FENCED_MASK_RE.sub(" ", final_reply)
    has_reply_draft_block = bool(
        PREPARED_REPLY_PROSE_RE.search(masked_reply)
        and FENCED_OR_QUOTE_BLOCK_RE.search(final_reply)
    )

    target_cl = ""
    target_ps = ""
    target_host = ""

    # Signal A: triage_store.py save-review ran for a Gerrit CL in this turn
    for call in reversed(save_review_calls):
        cl_id = call["cl"]
        outcome = call["outcome"]
        if not _has_gerrit_anchor(
            cl_id, bool(call.get("kind_explicit")), transcript_blobs, final_reply
        ):
            continue
        if outcome == "REASSIGN" and not call["has_draft_arg"] and not has_reply_draft_block:
            continue
        # Do not force premature LGTM drafts while local corpus gates are still running
        if (
            outcome == "CLEAN"
            and not call["has_draft_arg"]
            and not has_reply_draft_block
            and (
                call["corpus"] in ("pending", "required")
                or PENDING_CORPUS_REPLY_RE.search(final_reply)
            )
        ):
            continue
        if _is_self_owned_cl(cl_id, transcript_blobs):
            continue
        if cl_id not in saved_cls:
            target_cl = cl_id
            target_ps = call["patchset"]
            break

    # Signal B: Chat-only prepared review reply for an external/other-author Gerrit CL
    if not target_cl and has_reply_draft_block:
        cand_cl = ""
        cand_ps = ""
        for src in (final_reply, last_user_req):
            m_url = GERRIT_URL_CL_RE.search(src)
            if m_url:
                cand_cl = m_url.group("cl")
                cand_ps = m_url.group("ps") or ""
                target_host = m_url.group("host") or ""
                break
            m_wk = WATCHER_GERRIT_KEY_RE.search(src)
            if m_wk:
                cand_cl = m_wk.group(1)
                cand_ps = m_wk.group(2) or ""
                break
        if (
            cand_cl
            and cand_cl not in saved_cls
            and not _is_self_owned_cl(cand_cl, transcript_blobs)
        ):
            rec = _lookup_watcher_cl(cand_cl)
            is_known_review = isinstance(rec, dict) and rec.get("is_self") is False
            is_review_req = bool(
                re.search(
                    r"(?:\[CL-WATCHER\]|review-triage|companion-cl-review|ответь|ревью|триаж)",
                    last_user_req,
                    re.IGNORECASE,
                )
            )
            if is_known_review or is_review_req:
                target_cl = cand_cl
                target_ps = cand_ps

    if not target_cl:
        return ""

    if not target_host:
        for src in (final_reply, last_user_req):
            for m_url in GERRIT_URL_CL_RE.finditer(src):
                if m_url.group("cl") == target_cl:
                    target_host = m_url.group("host") or ""
                    if not target_ps and m_url.group("ps"):
                        target_ps = m_url.group("ps")
                    break
            if target_host:
                break

    host_flag = ""
    if target_host and "skia-review" not in target_host.lower():
        host_flag = f" --host {target_host}"
    ps_flag = f" --ps {target_ps}" if target_ps else ""
    ps_label = f" PS{target_ps}" if target_ps else ""

    return (
        f"[GERRIT DRAFT GUARD] You prepared a review reply for Gerrit CL {target_cl}{ps_label}, "
        "but did not save it as an unpublished Gerrit draft in this turn.\n"
        "Saving an unpublished Gerrit draft is safe and required by default (only publishing "
        "`/review` or resolving threads is forbidden). Run now:\n"
        f"  python3 ~/.gemini/config/scripts/gerrit_draft.py save --cl {target_cl}{ps_flag}"
        f"{host_flag} [--unresolved|--resolved] --message-file <draft_path>\n"
        "(Add `--path <file> --line <N>` for inline comments, or `--in-reply-to <comment_id>` "
        "for thread replies so it targets the parent comment's patchset automatically.)\n"
        "Do NOT repeat or rewrite your earlier review message above — post ONLY a compact "
        "1-2 line postscript confirming the saved clickable draft link "
        f"(`[черновик на Gerrit {target_cl}{ps_label}](https://...)`)."
    )
