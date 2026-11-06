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
    obs_html = "".join(
        f'<div class="wb-ob" data-eval="{_esc(ob.get("last_eval"))}">'
        f'<span>🔔 {_esc(ob.get("description"))}</span>'
        f'<code class="wb-trig">{_esc(ob.get("trigger_expr"))}</code>'
        f"</div>"
        for ob in (w.get("obligations") or [])
        if ob.get("status") != "done"
    )
    pills_html = "".join(
        f'<span class="wb-pill" data-kind="{_esc(a.get("kind"))}">'
        f'{_esc(a.get("icon"))} {_esc(a.get("title"))}</span>'
        for a in (w.get("artifacts") or [])
    )
    cards_ssr.append(
        f'<article class="wb-card" data-work-id="{wid}" data-lane="{lane}">'
        f'<div class="wb-card-hdr"><span class="wb-lane-tag">{lane}</span>'
        f'<strong class="wb-card-title">{title}</strong></div>'
        f"{obs_html}<div class='wb-pills'>{pills_html}</div></article>"
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
    --bg: #111318;
    --surface: #181b22;
    --surface-hover: #1f242d;
    --border: rgba(255, 255, 255, 0.09);
    --text: #e8eaed;
    --muted: #9aa0a6;
    --accent: #8ab4f8;
    --urgent: #f87171;
    --ready: #34d399;
    --warn: #fbbf24;
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
  }}
  .wb-top {{
    position: sticky;
    top: 0;
    z-index: 20;
    background: rgba(17, 19, 24, 0.96);
    backdrop-filter: blur(8px);
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
  }}
  .wb-btn:hover {{
    background: var(--surface-hover);
    border-color: rgba(138, 180, 248, 0.45);
  }}
  .wb-btn-primary {{
    background: rgba(138, 180, 248, 0.16);
    border-color: rgba(138, 180, 248, 0.45);
    color: var(--accent);
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
    background: transparent;
    color: var(--muted);
    border-radius: 999px;
    padding: 2px 9px;
    font-size: 11px;
  }}
  .wb-chip[data-active="true"] {{
    background: rgba(138, 180, 248, 0.18);
    color: var(--accent);
    border-color: rgba(138, 180, 248, 0.5);
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
    border: 1px solid rgba(138, 180, 248, 0.4);
    border-radius: 8px;
    padding: 9px;
    gap: 6px;
    flex-direction: column;
  }}
  .wb-form[data-open="true"] {{
    display: flex;
  }}
  .wb-input {{
    background: #101217;
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 5px;
    padding: 4px 8px;
    font-size: 12px;
    width: 100%;
  }}
  .wb-tray {{
    background: rgba(251, 191, 36, 0.06);
    border: 1px solid rgba(251, 191, 36, 0.28);
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
    border-top: 1px solid rgba(255, 255, 255, 0.06);
    font-size: 11.5px;
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
  .wb-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 9px 10px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }}
  .wb-card[data-lane="needs-you"] {{
    border-left: 3px solid var(--urgent);
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
  .wb-badge {{
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 10.5px;
    font-weight: 600;
  }}
  .wb-badge-needs {{
    background: rgba(248, 113, 113, 0.16);
    color: var(--urgent);
  }}
  .wb-badge-wait {{
    background: rgba(138, 180, 248, 0.14);
    color: var(--accent);
  }}
  .wb-badge-multi {{
    background: rgba(52, 211, 153, 0.14);
    color: var(--ready);
  }}
  .wb-ob {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    padding: 5px 8px;
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.08);
    font-size: 11.5px;
  }}
  .wb-ob[data-status="ready"] {{
    background: rgba(52, 211, 153, 0.11);
    border-color: rgba(52, 211, 153, 0.4);
  }}
  .wb-ob[data-stalled="true"] {{
    background: rgba(248, 113, 113, 0.12);
    border-color: rgba(248, 113, 113, 0.4);
  }}
  .wb-trig {{
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 10px;
    color: var(--muted);
    background: rgba(0, 0, 0, 0.25);
    padding: 1px 5px;
    border-radius: 4px;
  }}
  .wb-pills {{
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
  }}
  .wb-pill {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 7px;
    border-radius: 5px;
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.09);
    color: var(--text);
    text-decoration: none;
    font-size: 11px;
    max-width: 100%;
  }}
  .wb-pill:hover {{
    border-color: rgba(138, 180, 248, 0.5);
    background: rgba(138, 180, 248, 0.1);
  }}
  .wb-pill[data-role="context_only"] {{
    opacity: 0.6;
    border-style: dashed;
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
    background: rgba(255, 255, 255, 0.08);
    color: var(--muted);
  }}
  .wb-roll-dot[data-rolled="true"] {{
    background: rgba(52, 211, 153, 0.2);
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
  let state = {initial_json};
  let activeFilter = 'all';
  let searchQuery = '';

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
    return d.innerHTML;
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
      const rows = tray.map((t) => `
        <div class="wb-tray-row">
          <div style="min-width:0;flex:1">
            <strong>${{esc(t.sender)}}</strong> ·
            <a href="${{esc(t.url)}}" target="_blank" style="color:var(--text)">${{esc(t.subject)}}</a>
          </div>
          <button class="wb-btn" data-promote-thread="${{esc(t.artifact_key)}}" data-promote-subj="${{esc(t.subject)}}" type="button">+ Work</button>
        </div>
      `).join('');
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
      const laneBadge = w.lane === 'needs-you'
        ? '<span class="wb-badge wb-badge-needs">🚨 Needs You</span>'
        : '<span class="wb-badge wb-badge-wait">⏳ Waiting</span>';
      const multiBadge = w.is_multi_channel
        ? '<span class="wb-badge wb-badge-multi">Multi-channel</span>'
        : '<span class="wb-badge" style="color:var(--muted)">Single item</span>';
      const reasonsHtml = (w.reasons || []).map((r) => `<span>${{esc(r)}}</span>`).join(' · ');

      const obsHtml = (w.obligations || []).filter((o) => o.status !== 'done').map((o) => {{
        const statusTag = o.status === 'ready'
          ? '<strong style="color:var(--ready)">READY</strong>'
          : (o.stalled_alert ? '<strong style="color:var(--urgent)">STALLED &gt;7d</strong>' : `<span style="color:var(--warn)">eval:${{esc(o.last_eval)}}</span>`);
        return `
          <div class="wb-ob" data-status="${{esc(o.status)}}" data-stalled="${{Boolean(o.stalled_alert)}}">
            <div style="min-width:0;flex:1">
              🔔 ${{ statusTag }} · ${{esc(o.description)}}
              <code class="wb-trig">${{esc(o.trigger_expr)}}</code>
            </div>
            <button class="wb-btn" data-ob-done="${{esc(o.obligation_id)}}" type="button">✓</button>
          </div>
        `;
      }}).join('');

      const pillsHtml = (w.artifacts || []).map((a) => {{
        const cutBtn = !w.is_virtual
          ? `<button class="wb-cut" title="Detach from Work (prevent auto-relink)" data-unlink-work="${{esc(w.work_id)}}" data-unlink-key="${{esc(a.artifact_key)}}" type="button">✂</button>`
          : '';
        const isChat = a.kind === 'chat_chain';
        const chatCid = (a.state && a.state.cascade_id) || a.artifact_key.split(':').pop();
        const linkAttrs = isChat
          ? `href="#" data-open-chat="${{esc(chatCid)}}"`
          : `href="${{esc(a.url || '#')}}" target="_blank" rel="noopener"`;
        const stBadge = (a.state && a.state.status) ? `<span style="color:var(--muted);font-size:10px">[${{esc(a.state.status)}}]</span>` : '';
        return `
          <span class="wb-pill" data-kind="${{esc(a.kind)}}" data-role="${{esc(a.role)}}">
            <a ${{linkAttrs}} style="color:inherit;text-decoration:none">${{esc(a.icon)}} ${{esc(a.title)}}</a>
            ${{stBadge}}
            ${{renderRollDots(a)}}
            ${{cutBtn}}
          </span>
        `;
      }}).join('');

      const cardActions = !w.is_virtual
        ? `<div class="wb-actions">
             <button class="wb-btn" data-add-ob="${{esc(w.work_id)}}" type="button">+ Obligation</button>
             <button class="wb-btn" data-add-link="${{esc(w.work_id)}}" type="button">+ Link</button>
             <button class="wb-btn" data-work-done="${{esc(w.work_id)}}" type="button">✓ Done</button>
           </div>`
        : '';

      return `
        <article class="wb-card" data-work-id="${{esc(w.work_id)}}" data-lane="${{esc(w.lane)}}">
          <div class="wb-card-hdr">
            <div>
              <div class="wb-card-title">${{esc(w.title)}}</div>
              <div class="wb-meta-row">
                ${{laneBadge}} ${{multiBadge}}
                ${{w.project_id ? `<span>📁 ${{esc(w.project_id)}}</span>` : ''}}
                ${{w.roadmap_item_key ? `<span>🗺 ${{esc(w.roadmap_item_key)}}</span>` : ''}}
                <span>${{esc(w.updated_et)}}</span>
              </div>
            </div>
            ${{cardActions}}
          </div>
          ${{reasonsHtml ? `<div style="font-size:11px;color:var(--muted)">${{reasonsHtml}}</div>` : ''}}
          ${{obsHtml}}
          <div class="wb-pills">${{pillsHtml}}</div>
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

  document.getElementById('wb-search').addEventListener('input', (e) => {{
    searchQuery = e.target.value || '';
    render();
  }});

  document.body.addEventListener('click', (e) => {{
    const target = e.target.closest('button, a');
    if (!target) return;
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

      if parsed.path == "/api/sync":
        ingestor.run_sync_cycle(
            active_store,
            cl_state_path=cl_state_path,
            watcher_state_path=watcher_state_path,
            include_gmail=False,
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
  ingestor.run_sync_cycle(store)
  if args.once:
    export_workbench_info(args.port)
    return 0

  stop_event = threading.Event()

  def bg_loop() -> None:
    while not stop_event.wait(max(10, args.interval)):
      try:
        ingestor.run_sync_cycle(store)
        export_workbench_info(args.port)
      except Exception:
        pass

  t = threading.Thread(target=bg_loop, daemon=True)
  t.start()
  srv = create_server(host=args.host, port=args.port, store=store)
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
