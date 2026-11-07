#!/usr/bin/env python3
"""Workbench HTTP server (:8090) and passive background sync daemon."""

from __future__ import annotations

import argparse
import html as html_lib
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.parse

WORKBENCH_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKBENCH_DIR not in sys.path:
  sys.path.insert(0, WORKBENCH_DIR)

import ingestor  # pylint: disable=g-import-not-at-top
import work_store  # pylint: disable=g-import-not-at-top
import workctl  # pylint: disable=g-import-not-at-top

DEFAULT_PORT = 8090
DEFAULT_INFO_FILE = os.path.expanduser(
    "~/.gemini/jetski/brain/jetski-workbench.json"
)
PEN_URL_SH = (
    "/google/src/files/head/depot/google3/security/uberproxy/tools/contrib/pen_url.sh"
)
DEFAULT_WORKBENCH_PEN_URL = (
    "https://b2607f8b048001000013f0f0cac12c3b11f9a000000000000000001.proxy.googlers.com"
)

try:
  _INITIAL_MTIME = int(os.path.getmtime(os.path.abspath(__file__)))
except OSError:
  _INITIAL_MTIME = int(time.time())


def resolve_pen_url(port: int = DEFAULT_PORT) -> str:
  if port == DEFAULT_PORT:
    fallback = DEFAULT_WORKBENCH_PEN_URL
  else:
    fallback = f"http://localhost:{port}"
  try:
    if os.path.isfile(PEN_URL_SH):
      res = subprocess.run(
          [PEN_URL_SH, "--protocol=HTTP", "-p", str(port)],
          capture_output=True,
          text=True,
          timeout=5,
          check=False,
      )
      out = res.stdout.strip()
      if res.returncode == 0 and out.startswith("https://"):
        return out
  except Exception:
    pass
  return fallback


def export_workbench_info(
    port: int = DEFAULT_PORT, info_path: str = DEFAULT_INFO_FILE
) -> dict[str, Any]:
  ver = _INITIAL_MTIME
  ver_q = f"?v={ver}" if ver > 0 else ""
  pen_base = resolve_pen_url(port)
  data = {
      "pen_url": f"{pen_base}{ver_q}",
      "local_url": f"http://localhost:{port}{ver_q}",
      "port": port,
      "ui_version": ver,
  }
  try:
    os.makedirs(os.path.dirname(os.path.abspath(info_path)), exist_ok=True)
    tmp = f"{info_path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
      json.dump(data, f, indent=2)
    os.replace(tmp, info_path)
  except OSError:
    pass
  return data


def _esc(val: Any) -> str:
  return html_lib.escape(str(val or ""), quote=True)


def render_workbench_html(projection: dict[str, Any], ui_version: int) -> str:
  """Renders the standalone/embedded Workbench HTML dashboard."""
  counts = projection.get("counts") or {}
  metrics = projection.get("metrics") or {}
  works = projection.get("works") or []
  tray = projection.get("unanswered_tray") or []
  synced_et = _esc(projection.get("synced_et") or "")
  initial_json = json.dumps(projection, ensure_ascii=False).replace("</", "<\\/")

  cards_ssr: list[str] = []
  for w in works[:40]:
    wid = _esc(w.get("work_id"))
    title = _esc(w.get("title"))
    lane = _esc(w.get("lane"))
    origin = w.get("origin") or {}
    origin_ssr = (
        f'<div class="wb-origin-row"><span class="wb-origin">'
        f'{_esc(origin.get("label"))}</span></div>'
        if origin.get("label")
        else ""
    )
    obs_parts: list[str] = []
    for ob in w.get("obligations") or []:
      if ob.get("status") == "done":
        continue
      prog = _esc(ob.get("progress_summary") or "")
      prog_html = f'<div class="wb-ob-prog">{prog}</div>' if prog else ""
      obs_parts.append(
          f'<div class="wb-ob" data-status="{_esc(ob.get("status"))}">'
          f'<div class="wb-ob-main"><span>🔔 {_esc(ob.get("description"))}</span>'
          f"{prog_html}</div></div>"
      )
    obs_html = "".join(obs_parts)
    chk_items = w.get("checklist") or []
    chk_covered: set[str] = set()
    chk_rows_ssr: list[str] = []
    for item in chk_items:
      chk_covered.add(str(item.get("bug_key") or ""))
      cl_chips: list[str] = []
      for c in item.get("cls") or []:
        chk_covered.add(str(c.get("artifact_key") or ""))
        cl_chips.append(
            f'<div class="wb-cl-chip">'
            f'<a href="{_esc(c.get("url") or "#")}" '
            f'target="_blank" rel="noopener">{_esc(c.get("label"))}</a></div>'
        )
      cls_ssr = (
          f'<div class="wb-chk-cls">{"".join(cl_chips)}</div>' if cl_chips else ""
      )
      outdated_ssr = (
          '<span class="wb-draft-warn">📝 Not in draft yet</span>'
          if item.get("draft_outdated")
          else ""
      )
      chk_rows_ssr.append(
          f'<div class="wb-chk-row" data-stage="{_esc(item.get("stage"))}">'
          f'<div class="wb-chk-bug">'
          f'<a class="wb-bug-id" href="{_esc(item.get("url") or "#")}" '
          f'target="_blank" rel="noopener">🐛 {_esc(item.get("short_id"))}</a>'
          f'<span class="wb-bug-title">{_esc(item.get("title"))}</span></div>'
          f'<div class="wb-chk-status">'
          f'<span class="wb-stage-badge" '
          f'data-tone="{_esc(item.get("stage_tone"))}">'
          f'{_esc(item.get("stage_label"))}</span>{outdated_ssr}</div>'
          f"{cls_ssr}</div>"
      )
    chk_html = (
        f'<div class="wb-checklist">{"".join(chk_rows_ssr)}</div>'
        if chk_rows_ssr
        else ""
    )
    corr_arts_ssr = [
        a
        for a in (w.get("artifacts") or [])
        if a.get("role") != "context_only"
        and a.get("kind") in ("gmail_thread", "gmail_draft", "gchat_thread")
    ]
    main_arts_ssr = [
        a
        for a in (w.get("artifacts") or [])
        if a.get("role") != "context_only"
        and str(a.get("artifact_key") or "") not in chk_covered
        and a.get("kind") not in ("gmail_thread", "gmail_draft", "gchat_thread")
    ]
    mention_arts_ssr = [
        a
        for a in (w.get("artifacts") or [])
        if a.get("role") == "context_only"
        and str(a.get("artifact_key") or "") not in chk_covered
    ]
    corr_pills_ssr = "".join(
        f'<div class="wb-pill" data-kind="{_esc(a.get("kind"))}" '
        f'title="{_esc(a.get("why_tooltip") or "")}">'
        f'{_esc(a.get("icon"))} {_esc(a.get("title"))}'
        + (
            f' <span class="wb-why">· {_esc(a.get("why_badge"))}</span>'
            if a.get("why_badge")
            else ""
        )
        + "</div>"
        for a in corr_arts_ssr
    )
    corr_html = (
        f'<div class="wb-correspondence">{corr_pills_ssr}</div>'
        if corr_arts_ssr
        else ""
    )
    pills_html = "".join(
        f'<div class="wb-pill" data-kind="{_esc(a.get("kind"))}" '
        f'title="{_esc(a.get("why_tooltip") or "")}">'
        f'{_esc(a.get("icon"))} {_esc(a.get("title"))}'
        + (
            f' <span class="wb-why">· {_esc(a.get("why_badge"))}</span>'
            if a.get("why_badge")
            else ""
        )
        + "</div>"
        for a in main_arts_ssr
    )
    mentions_inner_ssr = "".join(
        f'<div class="wb-pill" data-kind="{_esc(a.get("kind"))}" data-role="context_only" '
        f'title="{_esc(a.get("why_tooltip") or "")}">'
        f'{_esc(a.get("icon"))} {_esc(a.get("title"))}'
        + (
            f' <span class="wb-why">· {_esc(a.get("why_badge"))}</span>'
            if a.get("why_badge")
            else ""
        )
        + "</div>"
        for a in mention_arts_ssr
    )
    mentions_ssr = (
        f'<details class="wb-mentions">'
        f"<summary>📎 Упомянуто в чате ({len(mention_arts_ssr)})</summary>"
        f'<div class="wb-mentions-list">{mentions_inner_ssr}</div></details>'
        if mention_arts_ssr
        else ""
    )
    cards_ssr.append(
        f'<article class="wb-card" data-work-id="{wid}" data-lane="{lane}">'
        f'<div class="wb-card-hdr"><div class="wb-hdr-stack">'
        f'<strong class="wb-card-title">{title}</strong>'
        f'<div class="wb-meta-row"><span class="wb-lane-tag">{lane}</span></div>'
        f"{origin_ssr}</div></div>"
        f'<div class="wb-card-body">{corr_html}{obs_html}{chk_html}'
        f"<div class='wb-pills'>{pills_html}</div>{mentions_ssr}</div></article>"
    )
  ssr_body = "\n".join(cards_ssr)
  precision_pct = int(round(float(metrics.get("link_precision", 1.0)) * 100))

  return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Workbench</title>
<style>
  :root {{
    --bg: #fbfbfd;
    --surface: #ffffff;
    --surface-hover: #f5f5f7;
    --border: #e5e5ea;
    --text: #1d1d1f;
    --muted: #86868b;
    --accent: #0071e3;
    --urgent: #ff3b30;
    --ready: #15803d;
    --warn: #b45309;
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.04), 0 8px 28px rgba(0, 0, 0, 0.06);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 12.5px;
    line-height: 1.4;
    -webkit-font-smoothing: antialiased;
  }}
  .wb-top {{
    position: sticky;
    top: 0;
    z-index: 20;
    background: rgba(251, 251, 253, 0.92);
    backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--border);
    padding: 8px 12px;
  }}
  .wb-hdr-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }}
  .wb-brand {{
    display: flex;
    align-items: center;
    gap: 6px;
    font-weight: 700;
    font-size: 13.5px;
    letter-spacing: -0.01em;
    color: var(--text);
  }}
  .wb-sub {{
    color: var(--muted);
    font-size: 11px;
    font-weight: 400;
  }}
  .wb-actions {{
    display: flex;
    align-items: center;
    gap: 6px;
  }}
  .wb-btn {{
    cursor: pointer;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    border-radius: 6px;
    padding: 3px 8px;
    font-size: 11.5px;
    font-weight: 500;
    box-shadow: 0 1px 1px rgba(0, 0, 0, 0.02);
  }}
  .wb-btn:hover {{
    background: var(--surface-hover);
    border-color: rgba(0, 113, 227, 0.45);
  }}
  .wb-btn-primary {{
    background: rgba(0, 113, 227, 0.09);
    border-color: rgba(0, 113, 227, 0.35);
    color: var(--accent);
    font-weight: 600;
  }}
  .wb-kpis {{
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 6px;
    margin-top: 8px;
  }}
  .wb-kpi {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 4px 7px;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
  }}
  .wb-kpi-val {{
    font-size: 14px;
    font-weight: 700;
  }}
  .wb-kpi-lbl {{
    color: var(--muted);
    font-size: 10px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .wb-filters {{
    display: flex;
    align-items: center;
    gap: 5px;
    margin-top: 8px;
    flex-wrap: wrap;
  }}
  .wb-chip {{
    cursor: pointer;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    border-radius: 999px;
    padding: 2px 9px;
    font-size: 11px;
  }}
  .wb-chip[data-active="true"] {{
    background: rgba(0, 113, 227, 0.10);
    color: var(--accent);
    border-color: rgba(0, 113, 227, 0.45);
    font-weight: 600;
  }}
  .wb-search {{
    margin-left: auto;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 6px;
    padding: 3px 8px;
    font-size: 11.5px;
    width: 150px;
  }}
  .wb-main {{
    padding: 10px 12px 28px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }}
  .wb-form {{
    display: none;
    background: var(--surface);
    border: 1px solid rgba(0, 113, 227, 0.35);
    box-shadow: var(--shadow);
    border-radius: 8px;
    padding: 9px;
    gap: 6px;
    flex-direction: column;
  }}
  .wb-form[data-open="true"] {{
    display: flex;
  }}
  .wb-input {{
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 5px;
    padding: 4px 8px;
    font-size: 12px;
    width: 100%;
  }}
  .wb-tray {{
    background: #fffbeb;
    border: 1px solid rgba(180, 83, 9, 0.25);
    border-radius: 8px;
    padding: 7px 10px;
  }}
  .wb-tray-hdr {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-weight: 600;
    color: var(--warn);
    font-size: 11.5px;
    cursor: pointer;
  }}
  .wb-tray-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 5px 0;
    border-top: 1px solid rgba(180, 83, 9, 0.12);
    font-size: 11.5px;
  }}
  .wb-tray-btns {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
  }}
  .wb-btn-dismiss {{
    padding: 3px 6px;
    color: var(--muted);
  }}
  .wb-btn-dismiss:hover {{
    color: var(--urgent);
    border-color: rgba(217, 48, 37, 0.45);
    background: rgba(217, 48, 37, 0.06);
  }}
  .wb-sec-hdr {{
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted);
    margin: 4px 0 2px;
  }}
  #wb-cards-container {{
    display: flex;
    flex-direction: column;
    gap: 8px;
  }}
  .wb-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    box-shadow: var(--shadow);
    border-radius: 10px;
    padding: 9px 10px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    transition: border-color 0.12s ease, background 0.12s ease;
  }}
  .wb-card[data-collapsed="true"] {{
    padding: 7px 10px;
    gap: 0;
  }}
  .wb-card[data-collapsed="true"]:hover {{
    background: var(--surface-hover);
  }}
  .wb-card[data-lane="needs-you"] {{
    border-left: 3px solid var(--urgent);
    background: linear-gradient(90deg, #fff2f1 0%, #ffffff 32px);
  }}
  .wb-card[data-lane="waiting"] {{
    border-left: 3px solid var(--accent);
  }}
  .wb-card[data-lane="done"] {{
    opacity: 0.68;
  }}
  .wb-card-hdr {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
    cursor: pointer;
  }}
  .wb-card-hdr-main {{
    display: flex;
    align-items: flex-start;
    gap: 6px;
    min-width: 0;
    flex: 1;
  }}
  .wb-chevron {{
    color: var(--muted);
    font-size: 12px;
    line-height: 1.45;
    width: 12px;
    flex-shrink: 0;
    user-select: none;
  }}
  .wb-card-hdr:hover .wb-chevron {{
    color: var(--accent);
  }}
  .wb-card-body {{
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding-left: 18px;
  }}
  .wb-card[data-collapsed="true"] .wb-card-body {{
    display: none;
  }}
  .wb-hdr-stack {{
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
    min-width: 0;
    flex: 1;
  }}
  .wb-card-title {{
    font-weight: 600;
    font-size: 13px;
    color: var(--text);
    word-break: break-word;
  }}
  .wb-meta-row {{
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    font-size: 11px;
    color: var(--muted);
  }}
  .wb-origin-row {{
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    max-width: 100%;
  }}
  .wb-origin {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 10.5px;
    font-weight: 500;
    background: #f5f5f7;
    border: 1px solid var(--border);
    color: #3a3a3c;
    text-decoration: none;
    max-width: 100%;
    word-break: break-word;
  }}
  .wb-reasons {{
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    font-size: 11px;
    color: var(--muted);
  }}
  .wb-obs {{
    display: flex;
    flex-direction: column;
    gap: 6px;
  }}
  a.wb-origin:hover {{
    border-color: rgba(0, 113, 227, 0.45);
    color: var(--accent);
    background: #eff6ff;
  }}
  .wb-badge {{
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 10.5px;
    font-weight: 600;
  }}
  .wb-badge-needs {{
    background: #fff2f1;
    color: var(--urgent);
    border: 1px solid rgba(255, 59, 48, 0.22);
  }}
  .wb-badge-wait {{
    background: #eff6ff;
    color: var(--accent);
    border: 1px solid rgba(0, 113, 227, 0.2);
  }}
  .wb-badge-multi {{
    background: #f0fdf4;
    color: var(--ready);
    border: 1px solid rgba(21, 128, 61, 0.2);
  }}
  .wb-ob {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
    padding: 6px 8px;
    border-radius: 6px;
    background: #f8fafc;
    border: 1px solid var(--border);
    font-size: 11.5px;
  }}
  .wb-ob-main {{
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
    min-width: 0;
    flex: 1;
  }}
  .wb-ob[data-status="ready"] {{
    background: #f0fdf4;
    border-color: rgba(21, 128, 61, 0.35);
  }}
  .wb-ob[data-stalled="true"] {{
    background: #fff2f1;
    border-color: rgba(255, 59, 48, 0.35);
  }}
  .wb-ob-prog {{
    font-size: 10.5px;
    font-weight: 600;
    color: var(--accent);
    background: #eff6ff;
    border: 1px solid rgba(0, 113, 227, 0.22);
    padding: 1px 7px;
    border-radius: 999px;
  }}
  .wb-correspondence {{
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
    padding: 6px 8px;
    border-radius: 7px;
    background: #f8fafc;
    border: 1px solid rgba(0, 113, 227, 0.18);
  }}
  .wb-checklist {{
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: #fcfcfd;
    overflow: hidden;
  }}
  .wb-chk-row {{
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
    padding: 7px 9px;
    border-top: 1px solid var(--border);
  }}
  .wb-chk-row:first-child {{
    border-top: none;
  }}
  .wb-chk-bug {{
    display: flex;
    align-items: baseline;
    gap: 6px;
    max-width: 100%;
  }}
  .wb-chk-status {{
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 3px;
    padding-left: 18px;
  }}
  .wb-bug-id {{
    font-weight: 600;
    font-size: 11.5px;
    color: var(--accent);
    text-decoration: none;
    white-space: nowrap;
  }}
  .wb-bug-id:hover {{
    text-decoration: underline;
  }}
  .wb-bug-title {{
    font-size: 11.5px;
    color: var(--text);
    word-break: break-word;
  }}
  .wb-stage-badge {{
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 10.5px;
    font-weight: 600;
    white-space: nowrap;
  }}
  .wb-stage-badge[data-tone="ready"] {{
    background: #dcfce7;
    color: var(--ready);
    border: 1px solid rgba(21, 128, 61, 0.25);
  }}
  .wb-stage-badge[data-tone="ok"] {{
    background: #e0f2fe;
    color: #0369a1;
    border: 1px solid rgba(3, 105, 161, 0.25);
  }}
  .wb-stage-badge[data-tone="wait"] {{
    background: #eff6ff;
    color: var(--accent);
    border: 1px solid rgba(0, 113, 227, 0.22);
  }}
  .wb-stage-badge[data-tone="warn"] {{
    background: #fffbeb;
    color: var(--warn);
    border: 1px solid rgba(180, 83, 9, 0.25);
  }}
  .wb-stage-badge[data-tone="urgent"] {{
    background: #fff2f1;
    color: var(--urgent);
    border: 1px solid rgba(255, 59, 48, 0.25);
  }}
  .wb-stage-badge[data-tone="muted"] {{
    background: #f5f5f7;
    color: var(--muted);
    border: 1px solid var(--border);
  }}
  .wb-draft-warn {{
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 600;
    background: #fffbeb;
    color: var(--warn);
    border: 1px dashed rgba(180, 83, 9, 0.45);
    white-space: nowrap;
  }}
  .wb-chk-cls {{
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
    padding-left: 18px;
    width: 100%;
  }}
  .wb-cl-chip {{
    display: inline-flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 5px;
    padding: 2px 7px;
    border-radius: 5px;
    background: #ffffff;
    border: 1px solid var(--border);
    color: var(--text);
    text-decoration: none;
    font-size: 11px;
    max-width: 100%;
  }}
  .wb-cl-chip:hover {{
    border-color: rgba(0, 113, 227, 0.45);
    background: #eff6ff;
  }}
  .wb-cl-subj {{
    color: #4b5563;
    font-size: 11px;
  }}
  .wb-cl-tag {{
    font-size: 9.5px;
    padding: 0 4px;
    border-radius: 3px;
    font-weight: 600;
  }}
  .wb-pills {{
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
  }}
  .wb-pill {{
    display: inline-flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 4px;
    padding: 3px 8px;
    border-radius: 5px;
    background: #f8fafc;
    border: 1px solid var(--border);
    color: var(--text);
    text-decoration: none;
    font-size: 11px;
    max-width: 100%;
  }}
  .wb-pill:hover {{
    border-color: rgba(0, 113, 227, 0.45);
    background: #eff6ff;
  }}
  .wb-pill[data-role="context_only"] {{
    opacity: 0.78;
    border-style: dashed;
  }}
  .wb-why {{
    color: var(--muted);
    font-size: 10px;
    font-weight: 500;
  }}
  .wb-mentions {{
    margin-top: 2px;
    font-size: 11px;
    color: var(--muted);
  }}
  .wb-mentions summary {{
    cursor: pointer;
    user-select: none;
    font-size: 10.5px;
    color: var(--muted);
    padding: 2px 0;
  }}
  .wb-mentions summary:hover {{
    color: var(--accent);
  }}
  .wb-mentions-list {{
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
    margin-top: 4px;
    padding-left: 8px;
  }}
  .wb-cut {{
    cursor: pointer;
    border: none;
    background: transparent;
    color: var(--muted);
    font-size: 10px;
    padding: 0 2px;
    margin-left: 2px;
  }}
  .wb-cut:hover {{
    color: var(--urgent);
  }}
  .wb-rolls {{
    display: inline-flex;
    gap: 2px;
    margin-left: 3px;
  }}
  .wb-roll-dot {{
    font-size: 9.5px;
    padding: 0 3px;
    border-radius: 3px;
    background: #f1f5f9;
    border: 1px solid #e2e8f0;
    color: var(--muted);
  }}
  .wb-roll-dot[data-rolled="true"] {{
    background: #dcfce7;
    border-color: rgba(21, 128, 61, 0.3);
    color: var(--ready);
  }}
</style>
</head>
<body>
  <header class="wb-top">
    <div class="wb-hdr-row">
      <div class="wb-brand">
        <span>🛠️ Workbench</span>
        <span class="wb-sub" id="wb-synced">Synced {synced_et}</span>
      </div>
      <div class="wb-actions">
        <button class="wb-btn wb-btn-primary" id="wb-new-btn" type="button">+ New Work</button>
        <button class="wb-btn" id="wb-sync-btn" type="button">⟳ Sync</button>
      </div>
    </div>
    <div class="wb-kpis">
      <div class="wb-kpi">
        <div class="wb-kpi-val" id="kpi-needs" style="color:var(--urgent)">{int(counts.get("needs_you", 0))}</div>
        <div class="wb-kpi-lbl">Needs You</div>
      </div>
      <div class="wb-kpi">
        <div class="wb-kpi-val" id="kpi-wait" style="color:var(--accent)">{int(counts.get("waiting", 0))}</div>
        <div class="wb-kpi-lbl">Waiting</div>
      </div>
      <div class="wb-kpi">
        <div class="wb-kpi-val" id="kpi-obs" style="color:var(--ready)">{int(counts.get("obligations_open", 0))}</div>
        <div class="wb-kpi-lbl">Obligations</div>
      </div>
      <div class="wb-kpi">
        <div class="wb-kpi-val" id="kpi-tray" style="color:var(--warn)">{len(tray)}</div>
        <div class="wb-kpi-lbl">Unanswered</div>
      </div>
      <div class="wb-kpi">
        <div class="wb-kpi-val" id="kpi-prec">{precision_pct}%</div>
        <div class="wb-kpi-lbl">14d Precision</div>
      </div>
    </div>
    <div class="wb-filters">
      <button class="wb-chip" data-filter="all" data-active="true" type="button">All</button>
      <button class="wb-chip" data-filter="needs-you" type="button">🚨 Needs You</button>
      <button class="wb-chip" data-filter="multi" type="button">🔗 Multi-channel</button>
      <button class="wb-chip" data-filter="obligations" type="button">🔔 Obligations</button>
      <button class="wb-chip" data-filter="done" type="button">✅ Done</button>
      <button class="wb-btn" id="wb-expand-all" type="button" title="Expand all visible cards">▾ Expand all</button>
      <button class="wb-btn" id="wb-collapse-all" type="button" title="Collapse all visible cards">▸ Collapse all</button>
      <input class="wb-search" id="wb-search" type="search" placeholder="Filter works, CLs, bugs...">
    </div>
  </header>

  <main class="wb-main">
    <form class="wb-form" id="wb-new-form">
      <input class="wb-input" id="wb-new-title" placeholder="Work title (e.g. Renzo Flutter strut metrics follow-up)" required>
      <input class="wb-input" id="wb-new-primary" placeholder="Primary artifacts (comma-separated, e.g. gerrit:1414545, flutter#50168)">
      <input class="wb-input" id="wb-new-channel" placeholder="Channels (comma-separated, e.g. gmail_draft:r2068112471352956917)">
      <input class="wb-input" id="wb-new-oblige" placeholder="Optional obligation (e.g. Reply to Renzo once rolled)">
      <input class="wb-input" id="wb-new-when" placeholder="Trigger (e.g. rolled_into(Flutter, gerrit:skia:1414545) or always)">
      <div class="wb-actions" style="justify-content:flex-end">
        <button class="wb-btn" id="wb-cancel-new" type="button">Cancel</button>
        <button class="wb-btn wb-btn-primary" type="submit">Create Work</button>
      </div>
    </form>

    <section id="wb-tray-container"></section>
    <section id="wb-cards-container">{ssr_body}</section>
  </main>

<script>
(() => {{
  const UI_VERSION = {int(ui_version)};
  const COLLAPSE_STORAGE_KEY = 'wb_collapsed_v1';
  let state = {initial_json};
  let activeFilter = 'all';
  let searchQuery = '';
  let collapsedOverrides = {{}};
  let bulkCollapseMode = null;
  try {{
    const raw = localStorage.getItem(COLLAPSE_STORAGE_KEY);
    if (raw) {{
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') {{
        collapsedOverrides = parsed.overrides || {{}};
        bulkCollapseMode = parsed.bulk ?? null;
      }}
    }}
  }} catch {{}}

  function saveCollapsePrefs() {{
    try {{
      localStorage.setItem(
        COLLAPSE_STORAGE_KEY,
        JSON.stringify({{ overrides: collapsedOverrides, bulk: bulkCollapseMode }})
      );
    }} catch {{}}
  }}

  function isCardCollapsed(w) {{
    const wid = String((w && w.work_id) || '');
    if (wid && Object.prototype.hasOwnProperty.call(collapsedOverrides, wid)) {{
      return Boolean(collapsedOverrides[wid]);
    }}
    if (bulkCollapseMode !== null) {{
      return Boolean(bulkCollapseMode);
    }}
    return w.lane !== 'needs-you';
  }}

  function notifyParentRendered() {{
    try {{
      if (window.parent && window.parent !== window) {{
        const synced = (state && state.synced_et) || '';
        window.parent.postMessage(
          {{ type: 'jetski-flight-deck-rendered', uiVersion: UI_VERSION, synced }},
          '*'
        );
        window.parent.postMessage(
          {{ type: 'jetski-workbench-rendered', uiVersion: UI_VERSION, synced }},
          '*'
        );
      }}
    }} catch {{}}
  }}

  window.addEventListener('message', (ev) => {{
    const d = ev && ev.data;
    if (d && (d.type === 'jetski-flight-deck-ping' || d.type === 'jetski-workbench-ping')) {{
      notifyParentRendered();
    }}
  }});

  function esc(s) {{
    const d = document.createElement('div');
    d.textContent = String(s || '');
    return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }}

  async function postJson(url, body) {{
    const resp = await fetch(url, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(body || {{}}),
    }});
    if (resp.ok) {{
      state = await resp.json();
      render();
    }}
  }}

  function renderRollDots(art) {{
    const st = (art && art.state) || {{}};
    const rolls = st.rolls;
    if (!rolls || typeof rolls !== 'object') return '';
    const map = [
      ['Android', 'A'],
      ['Chrome', 'C'],
      ['Google3', 'G3'],
      ['Flutter', 'F'],
    ];
    const items = [];
    for (const [k, short] of map) {{
      if (k in rolls) {{
        const r = rolls[k];
        const rolled = typeof r === 'boolean' ? r : Boolean(r && r.rolled);
        items.push(`<span class="wb-roll-dot" data-rolled="${{rolled}}" title="${{k}}: ${{rolled ? 'rolled' : 'pending'}}">${{short}}</span>`);
      }}
    }}
    return items.length ? `<span class="wb-rolls">${{items.join('')}}</span>` : '';
  }}

  function render() {{
    const counts = (state && state.counts) || {{}};
    const metrics = (state && state.metrics) || {{}};
    document.getElementById('wb-synced').textContent = 'Synced ' + ((state && state.synced_et) || '');
    document.getElementById('kpi-needs').textContent = String(counts.needs_you || 0);
    document.getElementById('kpi-wait').textContent = String(counts.waiting || 0);
    document.getElementById('kpi-obs').textContent = String(counts.obligations_open || 0);
    document.getElementById('kpi-tray').textContent = String(counts.unanswered || 0);
    const prec = Math.round(((metrics.link_precision ?? 1) * 100));
    document.getElementById('kpi-prec').textContent = `${{prec}}%`;

    const tray = (state && state.unanswered_tray) || [];
    const trayEl = document.getElementById('wb-tray-container');
    if (tray.length === 0) {{
      trayEl.innerHTML = '';
    }} else {{
      const rows = tray.map((t) => {{
        const safeUrl = /^https?:\\/\\//i.test(String(t.url || '')) ? esc(t.url) : '#';
        return `
        <div class="wb-tray-row">
          <div style="min-width:0;flex:1">
            <strong>${{esc(t.sender)}}</strong> ·
            <a href="${{safeUrl}}" target="_blank" rel="noopener"
               style="color:var(--text)">${{esc(t.subject)}}</a>
          </div>
          <div class="wb-tray-btns">
            <button class="wb-btn" data-promote-thread="${{esc(t.artifact_key)}}"
                    data-promote-subj="${{esc(t.subject)}}" type="button">+ Work</button>
            <button class="wb-btn wb-btn-dismiss" data-dismiss-thread="${{esc(t.artifact_key)}}"
                    title="Dismiss thread" aria-label="Dismiss thread" type="button">✕</button>
          </div>
        </div>
      `;
      }}).join('');
      trayEl.innerHTML = `
        <div class="wb-tray">
          <div class="wb-tray-hdr"><span>📨 Unanswered Human Threads (${{tray.length}})</span></div>
          ${{rows}}
        </div>
      `;
    }}

    const works = ((state && state.works) || []).filter((w) => {{
      if (activeFilter === 'needs-you' && w.lane !== 'needs-you') return false;
      if (activeFilter === 'multi' && !w.is_multi_channel) return false;
      if (activeFilter === 'obligations' && !(w.obligations && w.obligations.length > 0)) return false;
      if (activeFilter === 'done' && w.lane !== 'done') return false;
      if (activeFilter !== 'done' && w.lane === 'done') return false;
      if (searchQuery) {{
        const hay = JSON.stringify(w).toLowerCase();
        if (!hay.includes(searchQuery.toLowerCase())) return false;
      }}
      return true;
    }});

    const container = document.getElementById('wb-cards-container');
    if (works.length === 0) {{
      container.innerHTML = '<div style="color:var(--muted);padding:16px 4px">No works match the current filter.</div>';
      return;
    }}

    container.innerHTML = works.map((w) => {{
      const collapsed = isCardCollapsed(w);
      const chevron = collapsed ? '▸' : '▾';
      const openObs = (w.obligations || []).filter((o) => o.status !== 'done');
      const artCount = (w.artifacts || []).length;
      const laneBadge = w.lane === 'needs-you'
        ? '<span class="wb-badge wb-badge-needs">🚨 Needs You</span>'
        : '<span class="wb-badge wb-badge-wait">⏳ Waiting</span>';
      const multiBadge = w.is_multi_channel
        ? '<span class="wb-badge wb-badge-multi">Multi-channel</span>'
        : '<span class="wb-badge" style="color:var(--muted)">Single item</span>';
      const countsBadge = `
        ${{openObs.length ? `<span class="wb-badge" style="color:var(--warn)">🔔 ${{openObs.length}}</span>` : ''}}
        <span class="wb-badge" style="color:var(--muted)">📎 ${{artCount}}</span>
      `;
      // Render clickable root-cause provenance badge on its own vertical row in the header
      let originRowHtml = '';
      if (w.origin && w.origin.label) {{
        const o = w.origin;
        let inner = '';
        if (o.kind === 'chat_chain') {{
          const ocid = String(o.artifact_key || '').split(':').pop();
          inner = `<a class="wb-origin" href="#" data-open-chat="${{esc(ocid)}}">${{esc(o.label)}}</a>`;
        }} else if (o.url) {{
          inner = `<a class="wb-origin" href="${{esc(o.url)}}" target="_blank" rel="noopener">${{esc(o.label)}}</a>`;
        }} else {{
          inner = `<span class="wb-origin">${{esc(o.label)}}</span>`;
        }}
        originRowHtml = `<div class="wb-origin-row">${{inner}}</div>`;
      }}
      const projLabel = (w.project_id && !/^[0-9a-f]{{8}}-[0-9a-f]{{4}}-/i.test(w.project_id))
        ? `<span>📁 ${{esc(w.project_id)}}</span>`
        : '';
      const reasonsHtml = (w.reasons || [])
        .map((r) => `<div class="wb-reason-row">${{esc(r)}}</div>`)
        .join('');

      const obsItemsHtml = openObs.map((o) => {{
        const isDraftOb = String(o.target_artifact_key || '').startsWith('gmail_draft:');
        const waitTag = isDraftOb
          ? '<span style="color:var(--accent);font-weight:600">⏳ Parked draft</span>'
          : '<span style="color:var(--accent);font-weight:600">⏳ Waiting</span>';
        const statusTag = o.status === 'ready'
          ? '<strong style="color:var(--ready)">🔔 READY</strong>'
          : (o.stalled_alert
              ? '<strong style="color:var(--urgent)">⏰ STALLED &gt;7d</strong>'
              : waitTag);
        const progHtml = o.progress_summary
          ? `<div class="wb-ob-prog">${{esc(o.progress_summary)}}</div>`
          : '';
        return `
          <div class="wb-ob" data-status="${{esc(o.status)}}"
               data-stalled="${{Boolean(o.stalled_alert)}}"
               title="Trigger: ${{esc(o.trigger_expr)}}">
            <div class="wb-ob-main">
              <div>${{statusTag}} · ${{esc(o.description)}}</div>
              ${{progHtml}}
            </div>
            <button class="wb-btn" data-ob-done="${{esc(o.obligation_id)}}" type="button">✓</button>
          </div>
        `;
      }}).join('');
      const obsHtml = obsItemsHtml ? `<div class="wb-obs">${{obsItemsHtml}}</div>` : '';

      const chkItems = w.checklist || [];
      const chkCovered = new Set();
      const chkRowsHtml = chkItems.map((item) => {{
        if (item.bug_key) chkCovered.add(item.bug_key);
        const clChips = (item.cls || []).map((c) => {{
          if (c.artifact_key) chkCovered.add(c.artifact_key);
          const wipBadge = c.is_wip
            ? `<span class="wb-cl-tag" style="background:#fffbeb;color:var(--warn)">${{esc(c.wip_label || 'WIP')}}</span>`
            : '';
          const stackBadge = c.is_stacked
            ? '<span class="wb-cl-tag" style="background:#f1f5f9;color:var(--muted)">stacked</span>'
            : '';
          const confBadge = c.merge_conflict
            ? '<span class="wb-cl-tag" style="background:#fff2f1;color:var(--urgent)">⚠️ conflict</span>'
            : '';
          const rawSubj = String(c.subject || '').replace(/^\\d+:\\s*/, '').trim();
          const subjSpan = (rawSubj && rawSubj !== c.artifact_key && rawSubj !== c.label)
            ? `<span class="wb-cl-subj">— ${{esc(rawSubj)}}</span>`
            : '';
          const updSpan = c.updated_et
            ? `<span style="color:var(--muted);font-size:10px">${{esc(c.updated_et)}}</span>`
            : '';
          const cutBtn = !w.is_virtual
            ? `<button class="wb-cut" title="Detach from Work" data-unlink-work="${{esc(w.work_id)}}" data-unlink-key="${{esc(c.artifact_key)}}" type="button">✂</button>`
            : '';
          return `
            <div class="wb-cl-chip">
              <a href="${{esc(c.url || '#')}}" target="_blank" rel="noopener"
                 style="color:var(--accent);text-decoration:none;font-weight:600">${{esc(c.label)}}</a>
              ${{subjSpan}}
              ${{wipBadge}}${{stackBadge}}${{confBadge}}
              ${{renderRollDots({{ state: {{ rolls: c.rolls }} }})}}
              ${{updSpan}}${{cutBtn}}
            </div>
          `;
        }}).join('');
        const clsBlock = clChips ? `<div class="wb-chk-cls">${{clChips}}</div>` : '';
        const outdatedTag = item.draft_outdated
          ? '<span class="wb-draft-warn">📝 Not in draft yet</span>'
          : '';
        return `
          <div class="wb-chk-row" data-stage="${{esc(item.stage)}}">
            <div class="wb-chk-bug">
              <a class="wb-bug-id" href="${{esc(item.url || '#')}}"
                 target="_blank" rel="noopener">🐛 ${{esc(item.short_id)}}</a>
              <span class="wb-bug-title">${{esc(item.title)}}</span>
            </div>
            <div class="wb-chk-status">
              <span class="wb-stage-badge"
                    data-tone="${{esc(item.stage_tone)}}">${{esc(item.stage_label)}}</span>
              ${{outdatedTag}}
            </div>
            ${{clsBlock}}
          </div>
        `;
      }}).join('');
      const checklistHtml = chkRowsHtml ? `<div class="wb-checklist">${{chkRowsHtml}}</div>` : '';

      const corrPills = (w.artifacts || []).filter((a) => {{
        if (a.role === 'context_only') return false;
        return ['gmail_thread', 'gmail_draft', 'gchat_thread'].includes(a.kind);
      }});
      let chatPillCount = 0;
      const visiblePills = (w.artifacts || []).filter((a) => {{
        if (a.role === 'context_only') return false;
        if (chkCovered.has(a.artifact_key)) return false;
        if (['gmail_thread', 'gmail_draft', 'gchat_thread'].includes(a.kind)) return false;
        if (a.kind === 'chat_chain') {{
          const isRawChat = /^Chat\\s+[0-9a-f]{{6,}}$/i.test(String(a.title || ''));
          if (isRawChat && chatPillCount >= 1) return false;
          chatPillCount += 1;
        }}
        return true;
      }});
      const mentionPills = (w.artifacts || []).filter((a) => {{
        if (a.role !== 'context_only') return false;
        if (chkCovered.has(a.artifact_key)) return false;
        return true;
      }});
      const renderPillItem = (a) => {{
        const cutBtn = !w.is_virtual
          ? `<button class="wb-cut" title="Detach from Work (prevent auto-relink)" data-unlink-work="${{esc(w.work_id)}}" data-unlink-key="${{esc(a.artifact_key)}}" type="button">✂</button>`
          : '';
        const isChat = a.kind === 'chat_chain';
        const chatCid = (a.state && a.state.cascade_id) || a.artifact_key.split(':').pop();
        const linkAttrs = isChat
          ? `href="#" data-open-chat="${{esc(chatCid)}}"`
          : `href="${{esc(a.url || '#')}}" target="_blank" rel="noopener"`;
        const stBadge = (a.state && a.state.status) ? `<span style="color:var(--muted);font-size:10px">[${{esc(a.state.status)}}]</span>` : '';
        const whySpan = a.why_badge ? `<span class="wb-why">· ${{esc(a.why_badge)}}</span>` : '';
        const tipAttr = a.why_tooltip ? `title="${{esc(a.why_tooltip)}}"` : '';
        return `
          <div class="wb-pill" data-kind="${{esc(a.kind)}}" data-role="${{esc(a.role)}}" ${{tipAttr}}>
            <a ${{linkAttrs}} style="color:inherit;text-decoration:none">${{esc(a.icon)}} ${{esc(a.title)}}</a>
            ${{whySpan}}
            ${{stBadge}}
            ${{renderRollDots(a)}}
            ${{cutBtn}}
          </div>
        `;
      }};
      const corrHtml = corrPills.length
        ? `<div class="wb-correspondence">${{corrPills.map(renderPillItem).join('')}}</div>`
        : '';
      const pillsHtml = visiblePills.map(renderPillItem).join('');
      const mentionsHtml = mentionPills.length
        ? `<details class="wb-mentions">
             <summary>📎 Упомянуто в чате (${{mentionPills.length}})</summary>
             <div class="wb-mentions-list">${{mentionPills.map(renderPillItem).join('')}}</div>
           </details>`
        : '';

      const cardActions = !w.is_virtual
        ? `<div class="wb-actions">
             <button class="wb-btn" data-add-ob="${{esc(w.work_id)}}" type="button">+ Obligation</button>
             <button class="wb-btn" data-add-link="${{esc(w.work_id)}}" type="button">+ Link</button>
             <button class="wb-btn" data-work-done="${{esc(w.work_id)}}" type="button">✓ Done</button>
           </div>`
        : '';

      return `
        <article class="wb-card" data-work-id="${{esc(w.work_id)}}" data-lane="${{esc(w.lane)}}" data-collapsed="${{collapsed}}">
          <div class="wb-card-hdr" data-toggle-card="${{esc(w.work_id)}}">
            <div class="wb-card-hdr-main">
              <span class="wb-chevron" aria-hidden="true">${{chevron}}</span>
              <div class="wb-hdr-stack">
                <div class="wb-card-title">${{esc(w.title)}}</div>
                <div class="wb-meta-row">
                  ${{laneBadge}} ${{multiBadge}} ${{countsBadge}}
                  ${{projLabel}}
                  ${{w.roadmap_item_key ? `<span>🗺 ${{esc(w.roadmap_item_key)}}</span>` : ''}}
                  <span>${{esc(w.updated_et)}}</span>
                </div>
                ${{originRowHtml}}
              </div>
            </div>
          </div>
          <div class="wb-card-body">
            ${{reasonsHtml ? `<div class="wb-reasons">${{reasonsHtml}}</div>` : ''}}
            ${{corrHtml}}
            ${{obsHtml}}
            ${{checklistHtml}}
            ${{pillsHtml ? `<div class="wb-pills">${{pillsHtml}}</div>` : ''}}
            ${{mentionsHtml}}
            ${{cardActions}}
          </div>
        </article>
      `;
    }}).join('');
  }}

  document.getElementById('wb-new-btn').addEventListener('click', () => {{
    const f = document.getElementById('wb-new-form');
    f.setAttribute('data-open', f.getAttribute('data-open') === 'true' ? 'false' : 'true');
  }});
  document.getElementById('wb-cancel-new').addEventListener('click', () => {{
    document.getElementById('wb-new-form').setAttribute('data-open', 'false');
  }});
  document.getElementById('wb-new-form').addEventListener('submit', (e) => {{
    e.preventDefault();
    const title = document.getElementById('wb-new-title').value.trim();
    const primaries = document.getElementById('wb-new-primary').value.split(',').map((s) => s.trim()).filter(Boolean);
    const channels = document.getElementById('wb-new-channel').value.split(',').map((s) => s.trim()).filter(Boolean);
    const obligation = document.getElementById('wb-new-oblige').value.trim();
    const trigger = document.getElementById('wb-new-when').value.trim() || 'always';
    document.getElementById('wb-new-form').setAttribute('data-open', 'false');
    document.getElementById('wb-new-form').reset();
    void postJson('/api/work/create', {{ title, primaries, channels, obligation, trigger }});
  }});

  document.getElementById('wb-sync-btn').addEventListener('click', () => {{
    void postJson('/api/sync', {{}});
  }});

  for (const chip of document.querySelectorAll('.wb-chip')) {{
    chip.addEventListener('click', () => {{
      for (const c of document.querySelectorAll('.wb-chip')) c.setAttribute('data-active', 'false');
      chip.setAttribute('data-active', 'true');
      activeFilter = chip.getAttribute('data-filter') || 'all';
      render();
    }});
  }}

  document.getElementById('wb-expand-all').addEventListener('click', () => {{
    bulkCollapseMode = false;
    collapsedOverrides = {{}};
    saveCollapsePrefs();
    render();
  }});

  document.getElementById('wb-collapse-all').addEventListener('click', () => {{
    bulkCollapseMode = true;
    collapsedOverrides = {{}};
    saveCollapsePrefs();
    render();
  }});

  document.getElementById('wb-search').addEventListener('input', (e) => {{
    searchQuery = e.target.value || '';
    render();
  }});

  document.body.addEventListener('click', (e) => {{
    const target = e.target.closest('button, a, input');
    if (!target) {{
      const sel = window.getSelection ? String(window.getSelection()) : '';
      if (sel) return;
      const hdr = e.target.closest('[data-toggle-card]');
      if (hdr) {{
        const wid = hdr.getAttribute('data-toggle-card');
        const cardEl = hdr.closest('.wb-card');
        const cur = cardEl ? cardEl.getAttribute('data-collapsed') === 'true' : false;
        collapsedOverrides[wid] = !cur;
        saveCollapsePrefs();
        render();
      }}
      return;
    }}
    if (target.hasAttribute('data-open-chat')) {{
      e.preventDefault();
      const cid = target.getAttribute('data-open-chat');
      if (window.parent && window.parent !== window) {{
        window.parent.postMessage({{ type: 'jetski-flight-deck-open-chat', cascadeId: cid }}, '*');
      }}
      return;
    }}
    if (target.hasAttribute('data-unlink-work')) {{
      void postJson('/api/work/unlink', {{
        work_id: target.getAttribute('data-unlink-work'),
        artifact_key: target.getAttribute('data-unlink-key'),
      }});
      return;
    }}
    if (target.hasAttribute('data-ob-done')) {{
      void postJson('/api/obligation/done', {{
        obligation_id: target.getAttribute('data-ob-done'),
      }});
      return;
    }}
    if (target.hasAttribute('data-work-done')) {{
      void postJson('/api/work/done', {{
        work_id: target.getAttribute('data-work-done'),
      }});
      return;
    }}
    if (target.hasAttribute('data-promote-thread')) {{
      const akey = target.getAttribute('data-promote-thread');
      const subj = target.getAttribute('data-promote-subj') || akey;
      void postJson('/api/work/create', {{
        title: subj,
        primaries: [],
        channels: [akey],
      }});
      return;
    }}
    if (target.hasAttribute('data-dismiss-thread')) {{
      const akey = target.getAttribute('data-dismiss-thread');
      void postJson('/api/tray/dismiss', {{ artifact_key: akey }});
      return;
    }}
    if (target.hasAttribute('data-add-ob')) {{
      const wid = target.getAttribute('data-add-ob');
      const desc = window.prompt('Obligation description (e.g. Reply to Renzo once rolled):');
      if (!desc) return;
      const trig = window.prompt('Trigger (e.g. rolled_into(Flutter, gerrit:skia:1414545) or always):', 'always') || 'always';
      void postJson('/api/obligation/add', {{ work_id: wid, description: desc, trigger_expr: trig }});
      return;
    }}
    if (target.hasAttribute('data-add-link')) {{
      const wid = target.getAttribute('data-add-link');
      const akey = window.prompt('Artifact key (e.g. gerrit:skia:1414016, buganizer:580955594, gmail_thread:...):');
      if (!akey) return;
      void postJson('/api/work/link', {{ work_id: wid, artifact_key: akey, role: 'primary' }});
    }}
  }});

  render();
  notifyParentRendered();
  setInterval(async () => {{
    try {{
      const r = await fetch('/api/state');
      if (r.ok) {{
        state = await r.json();
        render();
        notifyParentRendered();
      }}
    }} catch {{}}
  }}, 30000);
}})();
</script>
</body>
</html>"""


class WorkbenchHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
  allow_reuse_address = True
  daemon_threads = True


def create_server(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    store: work_store.WorkStore | None = None,
    cl_state_path: str = ingestor.DEFAULT_CL_WATCHER_STATE,
    watcher_state_path: str = ingestor.DEFAULT_WATCHER_STATE,
    info_path: str = DEFAULT_INFO_FILE,
) -> WorkbenchHTTPServer:
  """Creates a configured WorkbenchHTTPServer instance."""
  active_store = store or work_store.WorkStore()

  def current_projection() -> dict[str, Any]:
    cl_state = ingestor.load_cl_watcher_snapshot(cl_state_path)
    w_state: dict[str, Any] = {}
    if os.path.isfile(watcher_state_path):
      try:
        with open(watcher_state_path, "r", encoding="utf-8") as f:
          w_state = json.load(f)
      except Exception:
        w_state = {}
    proj = active_store.build_dashboard_projection(
        cl_watcher_state=cl_state,
        unanswered_threads=w_state.get("unanswered_tray") or [],
    )
    proj["ui_version"] = _INITIAL_MTIME
    return proj

  class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt: str, *args: Any) -> None:
      del fmt, args
      return

    def _send_json(self, data: Any, status: int = 200) -> None:
      raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
      self.send_response(status)
      self.send_header("Content-Type", "application/json; charset=utf-8")
      self.send_header("Cache-Control", "no-store")
      self.send_header("Access-Control-Allow-Origin", "*")
      self.send_header("Content-Length", str(len(raw)))
      self.end_headers()
      self.wfile.write(raw)

    def _send_html(self, html_str: str, status: int = 200) -> None:
      raw = html_str.encode("utf-8")
      self.send_response(status)
      self.send_header("Content-Type", "text/html; charset=utf-8")
      self.send_header("Cache-Control", "no-store")
      self.send_header("Content-Length", str(len(raw)))
      self.end_headers()
      self.wfile.write(raw)

    def do_GET(self) -> None:
      parsed = urllib.parse.urlparse(self.path)
      if parsed.path == "/api/state":
        self._send_json(current_projection())
        return
      if parsed.path == "/favicon.ico":
        self.send_response(204)
        self.end_headers()
        return
      if parsed.path in ("/", "/index.html"):
        proj = current_projection()
        self._send_html(render_workbench_html(proj, _INITIAL_MTIME))
        return
      self._send_json({"error": "Not found"}, status=404)

    def do_POST(self) -> None:
      parsed = urllib.parse.urlparse(self.path)
      length = int(self.headers.get("Content-Length") or 0)
      raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
      try:
        body = json.loads(raw_body) if raw_body else {}
      except Exception:
        body = {}

      if parsed.path == "/api/work/create":
        title = str(body.get("title") or "New Work").strip()
        wid = active_store.create_work(
            title,
            project_id=str(body.get("project_id") or ""),
            roadmap_item_key=str(body.get("roadmap_item_key") or ""),
            meta={"title_manual": True},
        )
        for p in body.get("primaries") or []:
          ck = work_store.canonicalize_artifact_key(str(p))
          active_store.upsert_artifact(ck, title=ck, url=workctl._default_url_for_key(ck))
          active_store.link_artifact(wid, ck, role="primary", source="manual")
        for ch in body.get("channels") or []:
          ck = work_store.canonicalize_artifact_key(str(ch))
          active_store.upsert_artifact(ck, title=ck, url=workctl._default_url_for_key(ck))
          active_store.link_artifact(wid, ck, role="channel", source="manual")
        ob_desc = str(body.get("obligation") or "").strip()
        if ob_desc:
          active_store.add_obligation(
              wid,
              description=ob_desc,
              trigger_expr=str(body.get("trigger") or "always"),
          )
        self._send_json(current_projection())
        return

      if parsed.path == "/api/work/link":
        wid = str(body.get("work_id") or "")
        ck = work_store.canonicalize_artifact_key(str(body.get("artifact_key") or ""))
        if wid and ck:
          active_store.upsert_artifact(ck, title=ck, url=workctl._default_url_for_key(ck))
          active_store.link_artifact(
              wid, ck, role=str(body.get("role") or "primary"), source="manual"
          )
        self._send_json(current_projection())
        return

      if parsed.path == "/api/work/unlink":
        wid = str(body.get("work_id") or "")
        ck = str(body.get("artifact_key") or "")
        if wid and ck:
          active_store.unlink_artifact(wid, ck)
        self._send_json(current_projection())
        return

      if parsed.path == "/api/work/done":
        wid = str(body.get("work_id") or "")
        if wid:
          active_store.complete_work(wid, roadmap_cb=workctl._roadmap_writeback)
        self._send_json(current_projection())
        return

      if parsed.path == "/api/obligation/add":
        wid = str(body.get("work_id") or "")
        desc = str(body.get("description") or "").strip()
        if wid and desc:
          active_store.add_obligation(
              wid,
              description=desc,
              trigger_expr=str(body.get("trigger_expr") or "always"),
              target_artifact_key=str(body.get("target_artifact_key") or ""),
          )
        self._send_json(current_projection())
        return

      if parsed.path == "/api/obligation/done":
        ob_id = str(body.get("obligation_id") or "")
        if ob_id:
          active_store.mark_obligation_done(ob_id)
        self._send_json(current_projection())
        return

      if parsed.path == "/api/tray/dismiss":
        akey = str(body.get("artifact_key") or "").strip()
        if akey:
          try:
            active_store.dismiss_tray_thread(akey)
          except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        self._send_json(current_projection())
        return

      if parsed.path == "/api/sync":
        ingestor.run_sync_cycle(
            active_store,
            cl_state_path=cl_state_path,
            watcher_state_path=watcher_state_path,
            include_gmail=True,
        )
        self._send_json(current_projection())
        return

      self._send_json({"error": "Unknown endpoint"}, status=404)

  srv = WorkbenchHTTPServer((host, port), Handler)
  actual_port = srv.server_address[1]
  export_workbench_info(actual_port, info_path=info_path)
  return srv


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description="Workbench HTTP server & passive sync daemon")
  parser.add_argument("--host", default="0.0.0.0")
  parser.add_argument("--port", type=int, default=DEFAULT_PORT)
  parser.add_argument("--interval", type=int, default=45)
  parser.add_argument("--once", action="store_true", help="Run one sync cycle and exit")
  args = parser.parse_args(argv)

  store = work_store.WorkStore()
  if args.once:
    ingestor.run_sync_cycle(store)
    export_workbench_info(args.port)
    return 0

  srv = create_server(host=args.host, port=args.port, store=store)
  stop_event = threading.Event()

  def bg_loop() -> None:
    try:
      ingestor.run_sync_cycle(store)
      export_workbench_info(args.port)
    except Exception:
      pass
    while not stop_event.wait(max(10, args.interval)):
      try:
        ingestor.run_sync_cycle(store)
        export_workbench_info(args.port)
      except Exception:
        pass

  t = threading.Thread(target=bg_loop, daemon=True)
  t.start()
  try:
    srv.serve_forever()
  except KeyboardInterrupt:
    pass
  finally:
    stop_event.set()
    srv.server_close()
  return 0


if __name__ == "__main__":
  sys.exit(main())
