#!/usr/bin/env python3
"""Canonical Gerrit unpublished draft manager (`list` and `save` with in-place dedup).

Enforces three Gerrit invariants:
1. Never publishes (`/review`) or sets votes (`labels`); writes strictly go to
   `PUT /a/changes/<CL>/revisions/<PS>/drafts[/<id>]`.
2. Deduplicates in place by `(path, line, in_reply_to, side)` (b/580882306) so Gerrit UI
   never accumulates hidden duplicate drafts that publish all at once on `Reply`.
3. Resolves `--in-reply-to` parent comments via `GET /a/changes/<CL>/comments` and forces
   the draft onto the parent comment's `patch_set` (preventing Gerrit HTTP 400
   `Comment replies must be submitted on the same patchset as the parent comment`), while
   resolving standalone drafts to `current_revision` when `--ps` is omitted.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOST = "skia-review.googlesource.com"
HOST_ALIASES = {
    "skia": "skia-review.googlesource.com",
    "skrev": "skia-review.googlesource.com",
    "chromium": "chromium-review.googlesource.com",
    "crrev": "chromium-review.googlesource.com",
    "dart": "dart-review.googlesource.com",
    "angle": "angle-review.googlesource.com",
}
LUCI_TOKEN_TTL_S = 240.0
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

# Restrict mutations to Gerrit draft endpoints so publishing (/review) is structurally impossible
ALLOWED_PUT_API_RE = re.compile(r"^changes/[^/?#]+/revisions/\d+/drafts(?:/[^/?#]+)?$")
GERRIT_URL_RE = re.compile(
    r"^https?://(?P<host>[a-z0-9.-]+)/"
    r"(?:c/(?:(?P<project>[^\s+#]+?)/\+/|)|)(?P<cl>\d{4,9})(?:/(?P<ps>\d+))?(?:[/?#].*)?$",
    re.IGNORECASE,
)


class GerritDraftError(RuntimeError):
    """Raised on invalid Gerrit draft parameters, auth errors, or API rejections."""


def normalize_host(host: str | None) -> str:
    raw = (host or "").strip().lower()
    if not raw:
        return DEFAULT_HOST
    raw = re.sub(r"^https?://", "", raw).rstrip("/")
    if raw in HOST_ALIASES:
        return HOST_ALIASES[raw]
    if raw in ("crrev.com", "www.crrev.com"):
        return "chromium-review.googlesource.com"
    if raw in ("skrev.com", "www.skrev.com"):
        return "skia-review.googlesource.com"
    return raw


# Extract (host, cl_id, optional_ps) from URLs, Flight Deck keys (`gerrit:N`), or bare IDs
def parse_cl_target(
    cl_arg: str | int,
    host: str | None = None,
    ps: int | str | None = None,
) -> tuple[str, str, int | None]:
    raw = str(cl_arg or "").strip()
    if raw.lower().startswith("gerrit:"):
        raw = raw.split(":", 1)[1].split("@", 1)[0].strip()

    parsed_host = normalize_host(host) if host else ""
    url_ps: int | None = None
    m = GERRIT_URL_RE.match(raw)
    if m:
        if not parsed_host:
            parsed_host = normalize_host(m.group("host"))
        cl_id = m.group("cl")
        if m.group("ps"):
            url_ps = int(m.group("ps"))
    else:
        cl_id = raw.strip("/")
        if not re.fullmatch(r"(?:[A-Za-z0-9_./-]+~)?\d{4,9}", cl_id):
            raise GerritDraftError(f"Invalid Gerrit CL identifier or URL: {cl_arg!r}")

    eff_host = parsed_host or DEFAULT_HOST
    eff_ps: int | None
    if ps is None or str(ps).strip() == "" or str(ps).strip().lower() == "current":
        eff_ps = None if (ps is not None and str(ps).strip().lower() == "current") else url_ps
    else:
        eff_ps = int(ps)
        if eff_ps <= 0:
            raise GerritDraftError(f"Patchset must be positive, got {ps!r}")
    return eff_host, cl_id, eff_ps


# Mint and cache OAuth tokens per host via git-credential-luci before 5-minute Gerrit /a/ expiry
def get_luci_token(host: str, force_refresh: bool = False) -> str:
    norm_host = normalize_host(host)
    cached = _TOKEN_CACHE.get(norm_host)
    if not force_refresh and cached and cached[0] and (time.time() - cached[1]) < LUCI_TOKEN_TTL_S:
        return cached[0]
    try:
        proc = subprocess.run(
            ["git-credential-luci", "get"],
            input=f"protocol=https\nhost={norm_host}\n\n",
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                tok = line.split("=", 1)[1].strip()
                if tok:
                    _TOKEN_CACHE[norm_host] = (tok, time.time())
                    return tok
    except (subprocess.SubprocessError, OSError):
        pass
    _TOKEN_CACHE.pop(norm_host, None)
    return ""


def _decode_gerrit_json(raw_bytes: bytes):
    text = raw_bytes.decode("utf-8", "replace")
    if text.startswith(")]}'"):
        text = text[4:].lstrip("\r\n")
    if not text.strip():
        return None
    return json.loads(text)


# Execute authenticated Gerrit /a/ calls while forbidding any endpoint outside GET or PUT /drafts
def gerrit_request(
    host: str,
    method: str,
    api_path: str,
    payload: dict | None = None,
    timeout: int = 20,
):
    upper_method = method.upper()
    clean_path = api_path.lstrip("/")
    if upper_method == "GET":
        if payload is not None:
            raise GerritDraftError("GET requests must not include a JSON body")
    elif upper_method == "PUT":
        if not ALLOWED_PUT_API_RE.match(clean_path):
            raise GerritDraftError(
                f"Refusing non-draft Gerrit write: PUT /{clean_path}"
            )
    else:
        raise GerritDraftError(
            f"Refusing HTTP {upper_method} /{clean_path}: "
            "gerrit_draft.py never publishes or deletes"
        )

    norm_host = normalize_host(host)
    tok = get_luci_token(norm_host)
    if not tok:
        raise GerritDraftError(
            f"missing_luci_token: git-credential-luci returned no token for {norm_host} "
            "(run `git-credential-luci login` or `luci-auth login` if reauth is required)"
        )

    url = f"https://{norm_host}/a/{clean_path}"
    body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
    base_headers = {"User-Agent": "jetski-gerrit-draft"}
    if body_bytes is not None:
        base_headers["Content-Type"] = "application/json; charset=UTF-8"

    def _send(bearer: str):
        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers={**base_headers, "Authorization": f"Bearer {bearer}"},
            method=upper_method,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _decode_gerrit_json(resp.read())

    try:
        return _send(tok)
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", "replace").strip()
        except OSError:
            pass
        # Retry once on 400/401/403 in case a cached LUCI token expired mid-session
        if exc.code in (400, 401, 403):
            fresh = get_luci_token(norm_host, force_refresh=True)
            if fresh and fresh != tok:
                try:
                    return _send(fresh)
                except urllib.error.HTTPError as exc2:
                    try:
                        err_body = exc2.read().decode("utf-8", "replace").strip() or err_body
                    except OSError:
                        pass
                    raise GerritDraftError(
                        f"Gerrit HTTP {exc2.code} on {upper_method} /a/{clean_path}: "
                        f"{err_body or exc2.reason}"
                    ) from exc2
                except (urllib.error.URLError, OSError) as exc2:
                    raise GerritDraftError(f"Gerrit network error: {exc2}") from exc2
        raise GerritDraftError(
            f"Gerrit HTTP {exc.code} on {upper_method} /a/{clean_path}: {err_body or exc.reason}"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise GerritDraftError(f"Gerrit network error on {norm_host}: {exc}") from exc


def fetch_change_info(host: str, cl_id: str) -> dict:
    enc_cl = urllib.parse.quote(str(cl_id), safe="~")
    info = gerrit_request(
        host,
        "GET",
        f"changes/{enc_cl}?o=CURRENT_REVISION&o=ALL_REVISIONS&o=DETAILED_ACCOUNTS",
    )
    if not isinstance(info, dict):
        raise GerritDraftError(f"Unexpected Gerrit change metadata for CL {cl_id}")
    revs = info.get("revisions") or {}
    valid_ps = sorted(
        int(r["_number"]) for r in revs.values() if isinstance(r, dict) and "_number" in r
    )
    cur_sha = info.get("current_revision") or ""
    if cur_sha and isinstance(revs.get(cur_sha), dict) and "_number" in revs[cur_sha]:
        current_ps = int(revs[cur_sha]["_number"])
    elif valid_ps:
        current_ps = valid_ps[-1]
    else:
        current_ps = 1
    return {
        "cl_number": int(info.get("_number") or str(cl_id).split("~")[-1]),
        "project": str(info.get("project") or ""),
        "subject": str(info.get("subject") or ""),
        "status": str(info.get("status") or ""),
        "owner_email": str((info.get("owner") or {}).get("email") or ""),
        "current_ps": current_ps,
        "valid_patchsets": set(valid_ps),
    }


def _patchset_url(host: str, project: str, cl_number: int, ps: int) -> str:
    norm_host = normalize_host(host)
    if project:
        return f"https://{norm_host}/c/{project}/+/{cl_number}/{ps}"
    return f"https://{norm_host}/c/{cl_number}/{ps}"


# Locate parent comment metadata so thread replies land on the parent's exact patchset and path
def resolve_parent_comment(host: str, cl_id: str, in_reply_to: str) -> dict:
    enc_cl = urllib.parse.quote(str(cl_id), safe="~")
    comments = gerrit_request(host, "GET", f"changes/{enc_cl}/comments") or {}
    for c_path, items in comments.items():
        for item in items or []:
            if item.get("id") == in_reply_to:
                if "patch_set" not in item:
                    raise GerritDraftError(
                        f"Parent comment {in_reply_to} has no patch_set field on CL {cl_id}"
                    )
                return {
                    "id": in_reply_to,
                    "patch_set": int(item["patch_set"]),
                    "path": c_path,
                    "line": item.get("line"),
                    "side": item.get("side"),
                    "range": item.get("range"),
                    "unresolved": bool(item.get("unresolved", False)),
                }

    # Explain clearly if the caller passed one of their own unpublished draft IDs to --in-reply-to
    drafts = gerrit_request(host, "GET", f"changes/{enc_cl}/drafts") or {}
    for d_path, items in drafts.items():
        for item in items or []:
            if item.get("id") == in_reply_to:
                d_ps = item.get("patch_set", "?")
                raise GerritDraftError(
                    f"{in_reply_to} on {d_path} (PS{d_ps}) is an unpublished draft, not a "
                    "published comment; omit --in-reply-to to update an existing draft in place"
                )
    raise GerritDraftError(f"Parent comment {in_reply_to!r} not found on CL {cl_id}")


def list_drafts(
    cl: str | int,
    *,
    host: str | None = None,
    ps: int | str | None = None,
) -> dict:
    eff_host, cl_id, eff_ps = parse_cl_target(cl, host=host, ps=ps)
    meta = fetch_change_info(eff_host, cl_id)
    enc_cl = urllib.parse.quote(str(meta["cl_number"]), safe="")
    if eff_ps is not None:
        raw = gerrit_request(eff_host, "GET", f"changes/{enc_cl}/revisions/{eff_ps}/drafts") or {}
        default_ps = eff_ps
    else:
        raw = gerrit_request(eff_host, "GET", f"changes/{enc_cl}/drafts") or {}
        default_ps = meta["current_ps"]

    entries: list[dict] = []
    for path, items in sorted(raw.items()):
        for item in items or []:
            d_ps = int(item.get("patch_set") or default_ps)
            reply_to = item.get("in_reply_to")
            # Standalone drafts on an older patchset are easy to miss or publish stale
            stale_ps = bool(not reply_to and d_ps < meta["current_ps"])
            entries.append({
                "id": item.get("id", ""),
                "patch_set": d_ps,
                "path": path,
                "line": item.get("line"),
                "side": item.get("side") or "REVISION",
                "in_reply_to": reply_to,
                "unresolved": bool(item.get("unresolved", False)),
                "updated": item.get("updated", ""),
                "message": item.get("message", ""),
                "stale_ps": stale_ps,
                "url": _patchset_url(eff_host, meta["project"], meta["cl_number"], d_ps),
            })
    entries.sort(key=lambda e: (e["patch_set"], e["path"], e["line"] or 0, e["id"]))
    return {
        "host": eff_host,
        "cl": meta["cl_number"],
        "project": meta["project"],
        "subject": meta["subject"],
        "owner_email": meta["owner_email"],
        "current_ps": meta["current_ps"],
        "queried_ps": eff_ps,
        "drafts": entries,
    }


def save_draft(
    cl: str | int,
    *,
    message: str,
    host: str | None = None,
    ps: int | str | None = None,
    path: str | None = None,
    line: int | None = None,
    side: str | None = None,
    in_reply_to: str | None = None,
    unresolved: bool | None = None,
    append: bool = False,
    err=None,
) -> dict:
    err_stream = err if err is not None else sys.stderr
    clean_msg = (message or "").strip()
    if not clean_msg:
        raise GerritDraftError("Refusing to save an empty Gerrit draft message")

    eff_host, cl_id, requested_ps = parse_cl_target(cl, host=host, ps=ps)
    meta = fetch_change_info(eff_host, cl_id)
    cl_num = meta["cl_number"]
    enc_cl = urllib.parse.quote(str(cl_num), safe="")

    eff_range = None
    if in_reply_to:
        parent = resolve_parent_comment(eff_host, str(cl_num), in_reply_to.strip())
        target_ps = parent["patch_set"]
        if requested_ps is not None and requested_ps != target_ps:
            err_stream.write(
                f"WARNING: Gerrit requires in_reply_to drafts on the parent comment's patchset; "
                f"overriding requested PS{requested_ps} -> PS{target_ps}\n"
            )
        if path and path != "/PATCHSET_LEVEL" and path != parent["path"]:
            raise GerritDraftError(
                f"--path {path!r} does not match parent comment path {parent['path']!r}"
            )
        if path == "/PATCHSET_LEVEL" and parent["path"] != "/PATCHSET_LEVEL":
            raise GerritDraftError(
                f"--path '/PATCHSET_LEVEL' conflicts with parent comment path {parent['path']!r}"
            )
        eff_path = parent["path"]
        # Reject --line if parent comment is file/patchset-level or on a different line
        if line is not None and (
            parent["line"] is None or int(line) != int(parent["line"])
        ):
            raise GerritDraftError(
                f"--line {line} does not match parent comment line {parent['line']}"
            )
        eff_line = int(line) if line is not None else parent["line"]
        eff_side = side or parent["side"]
        eff_range = parent["range"]
        default_unresolved = parent["unresolved"]
    else:
        if requested_ps is None:
            target_ps = meta["current_ps"]
        else:
            target_ps = requested_ps
            if meta["valid_patchsets"] and target_ps not in meta["valid_patchsets"]:
                valid_str = ", ".join(f"PS{n}" for n in sorted(meta["valid_patchsets"]))
                raise GerritDraftError(
                    f"PS{target_ps} does not exist on CL {cl_num} (valid: {valid_str})"
                )
            if target_ps < meta["current_ps"]:
                err_stream.write(
                    f"WARNING: saving draft on older PS{target_ps} while current patchset on "
                    f"CL {cl_num} is PS{meta['current_ps']}\n"
                )
        eff_path = path or "/PATCHSET_LEVEL"
        if eff_path == "/PATCHSET_LEVEL" and line is not None:
            raise GerritDraftError("--line cannot be combined with /PATCHSET_LEVEL")
        eff_line = int(line) if line is not None else None
        if eff_line is not None and eff_line <= 0:
            raise GerritDraftError(f"--line must be positive, got {eff_line}")
        eff_side = side
        default_unresolved = False

    drafts_api = f"changes/{enc_cl}/revisions/{target_ps}/drafts"
    drafts_on_ps = gerrit_request(eff_host, "GET", drafts_api) or {}
    norm_side = eff_side or "REVISION"
    dedup_key = (eff_line or None, (in_reply_to.strip() if in_reply_to else None), norm_side)

    existing = None
    for item in drafts_on_ps.get(eff_path, []) or []:
        item_key = (
            item.get("line") or None,
            item.get("in_reply_to") or None,
            item.get("side") or "REVISION",
        )
        if item_key == dedup_key:
            existing = item
            break

    if existing is not None:
        old_msg = (existing.get("message") or "").strip()
        if append:
            final_unresolved = (
                bool(existing.get("unresolved", default_unresolved))
                if unresolved is None
                else bool(unresolved)
            )
            # Match whole blank-line-delimited blocks so substrings ("LGTM" in "Needs LGTM") append
            old_blocks = [blk.strip() for blk in old_msg.split("\n\n") if blk.strip()]
            same_unres = bool(existing.get("unresolved", False)) == final_unresolved
            if clean_msg in old_blocks and same_unres:
                return _build_save_result(
                    eff_host, meta, target_ps, "unchanged", existing, eff_path, eff_line
                )
            final_msg = f"{old_msg}\n\n{clean_msg}".strip() if old_msg else clean_msg
        else:
            final_unresolved = default_unresolved if unresolved is None else bool(unresolved)
            if old_msg == clean_msg and bool(existing.get("unresolved", False)) == final_unresolved:
                return _build_save_result(
                    eff_host, meta, target_ps, "unchanged", existing, eff_path, eff_line
                )
            final_msg = clean_msg
    else:
        final_unresolved = default_unresolved if unresolved is None else bool(unresolved)
        final_msg = clean_msg

    payload: dict = {
        "path": eff_path,
        "message": final_msg,
        "unresolved": final_unresolved,
    }
    if eff_line is not None:
        payload["line"] = eff_line
    if eff_side:
        payload["side"] = eff_side
    if eff_range:
        payload["range"] = eff_range
    if in_reply_to:
        payload["in_reply_to"] = in_reply_to.strip()

    if existing is not None:
        saved = gerrit_request(eff_host, "PUT", f"{drafts_api}/{existing['id']}", payload)
        action = "updated"
    else:
        saved = gerrit_request(eff_host, "PUT", drafts_api, payload)
        action = "created"

    return _build_save_result(
        eff_host, meta, target_ps, action, saved or payload, eff_path, eff_line
    )


def _build_save_result(
    host: str,
    meta: dict,
    ps: int,
    action: str,
    draft_obj: dict,
    path: str,
    line: int | None,
) -> dict:
    return {
        "status": "GERRIT_DRAFT_SAVED",
        "host": host,
        "cl": meta["cl_number"],
        "project": meta["project"],
        "ps": ps,
        "action": action,
        "id": str(draft_obj.get("id") or ""),
        "path": path,
        "line": line,
        "in_reply_to": draft_obj.get("in_reply_to"),
        "unresolved": bool(draft_obj.get("unresolved", False)),
        "url": _patchset_url(host, meta["project"], meta["cl_number"], ps),
    }


def _read_message_arg(args: argparse.Namespace) -> str:
    if args.message_file:
        if args.message_file == "-":
            return sys.stdin.read()
        with open(args.message_file, "r", encoding="utf-8") as f:
            return f.read()
    if args.message is not None:
        return args.message
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise GerritDraftError("Provide --message or --message-file <path|->")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "List or save unpublished Gerrit review drafts with in-place dedup "
            "(never publishes or votes)."
        )
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List unpublished drafts on a Gerrit CL")
    p_list.add_argument("--cl", required=True, help="CL number, gerrit:<N>, or full Gerrit URL")
    p_list.add_argument("--host", default=None, help="Gerrit host or alias (default: skia)")
    p_list.add_argument(
        "--ps",
        default=None,
        help="Optional patchset number or 'current' (omit to inspect all patchsets)",
    )
    p_list.add_argument("--json", action="store_true", help="Emit JSON output")

    p_save = sub.add_parser(
        "save",
        help="Create or update an unpublished Gerrit draft in place (never publishes)",
    )
    p_save.add_argument("--cl", required=True, help="CL number, gerrit:<N>, or full Gerrit URL")
    p_save.add_argument("--host", default=None, help="Gerrit host or alias (default: skia)")
    p_save.add_argument(
        "--ps",
        default=None,
        help="Target patchset (defaults to latest PS, or parent comment's PS for --in-reply-to)",
    )
    p_save.add_argument(
        "--path",
        default=None,
        help="File path for inline drafts, or /PATCHSET_LEVEL (default when not replying)",
    )
    p_save.add_argument("--line", type=int, default=None, help="1-based line number on --path")
    p_save.add_argument(
        "--side", choices=["REVISION", "PARENT"], default=None, help="Diff side for inline drafts"
    )
    p_save.add_argument(
        "--in-reply-to",
        default=None,
        help="Published Gerrit comment ID to reply to (uses parent comment's patchset)",
    )
    res_group = p_save.add_mutually_exclusive_group()
    res_group.add_argument(
        "--unresolved",
        dest="unresolved",
        action="store_true",
        default=None,
        help="Mark draft as unresolved (for action items, defects, or open questions)",
    )
    res_group.add_argument(
        "--resolved",
        dest="unresolved",
        action="store_false",
        help="Mark draft as resolved / non-blocking (for clean approvals or acks)",
    )
    p_save.add_argument(
        "--append",
        action="store_true",
        help="Append to an existing matching draft separated by a blank line instead of replacing",
    )
    p_save.add_argument("--message", default=None, help="Draft message text")
    p_save.add_argument(
        "--message-file", default=None, help="Path to UTF-8 draft message file ('-' for stdin)"
    )
    p_save.add_argument("--json", action="store_true", help="Emit JSON output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        if args.cmd == "list":
            res = list_drafts(args.cl, host=args.host, ps=args.ps)
            if args.json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
                return 0
            drafts = res["drafts"]
            print(
                f"CL {res['cl']} (current PS{res['current_ps']}, owner {res['owner_email']}): "
                f"{len(drafts)} unpublished draft(s)"
            )
            for d in drafts:
                loc = f"{d['path']}:{d['line']}" if d["line"] else d["path"]
                stale = f" [STALE < current PS{res['current_ps']}]" if d["stale_ps"] else ""
                reply = f" in_reply_to={d['in_reply_to']}" if d["in_reply_to"] else ""
                preview = " ".join(d["message"].split())[:120]
                print(
                    f"  PS{d['patch_set']}{stale} | {loc}{reply} | "
                    f"unresolved={str(d['unresolved']).lower()} | id={d['id']} | {preview}"
                )
            return 0

        msg = _read_message_arg(args)
        saved = save_draft(
            args.cl,
            message=msg,
            host=args.host,
            ps=args.ps,
            path=args.path,
            line=args.line,
            side=args.side,
            in_reply_to=args.in_reply_to,
            unresolved=args.unresolved,
            append=args.append,
        )
        if args.json:
            print(json.dumps(saved, indent=2, ensure_ascii=False))
        else:
            line_tok = saved["line"] if saved["line"] is not None else "-"
            print(
                f"GERRIT_DRAFT_SAVED host={saved['host']} cl={saved['cl']} ps={saved['ps']} "
                f"action={saved['action']} id={saved['id']} path={saved['path']} "
                f"line={line_tok} unresolved={str(saved['unresolved']).lower()} "
                f"url={saved['url']}"
            )
        return 0
    except GerritDraftError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
