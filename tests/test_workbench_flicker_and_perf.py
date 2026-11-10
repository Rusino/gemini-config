#!/usr/bin/env python3
"""Contract tests for Workbench (:8090) slim cached projection, polling, and PEN isolation."""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import unittest
from unittest import mock
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
  sys.path.insert(0, REPO_ROOT)

from workbench import work_store
from workbench import workbench_server


class WorkbenchFlickerAndPerfTest(unittest.TestCase):
  """Verifies payload slimming, generation-safe cache reads, no-op polls, and PEN isolation."""

  def setUp(self) -> None:
    self._tmp = tempfile.TemporaryDirectory()
    self.db_path = os.path.join(self._tmp.name, "workbench.db")
    self.cl_state_path = os.path.join(self._tmp.name, "cl_watcher_state.json")
    self.watcher_state_path = os.path.join(self._tmp.name, "watcher_state.json")
    self.info_path = os.path.join(self._tmp.name, "jetski-workbench.json")
    self.store = work_store.WorkStore(self.db_path)

  def tearDown(self) -> None:
    self._tmp.cleanup()

  # Checks slimming strips multi-KB blobs while keeping crbug SSR merges, rolls, and search parity
  def test_slim_projection_strips_heavy_blobs_and_preserves_ui_contracts(self) -> None:
    now = 1_762_800_000.0
    wid = self.store.create_work(
        "Ellipsis metrics follow-up — Florin Malita",
        work_id="work-slim",
        project_id="skparagraph",
        roadmap_item_key="R22",
        now=now,
    )
    self.store.upsert_artifact(
        "github_issue:flutter/flutter:50168",
        kind="github_issue",
        title="flutter#50168: Ellipsis after hard break",
        url="https://github.com/flutter/flutter/issues/50168",
        state={
            "status": "OPEN",
            "linked_cls": [
                {
                    "target_id": "1414545",
                    "item_key": "gerrit:skia:1414545",
                    "raw_blob": "x" * 8000,
                }
            ],
            "comments": ["c" * 10000],
        },
        now=now,
    )
    self.store.link_artifact(wid, "github_issue:flutter/flutter:50168", role="primary", now=now)

    # Standalone crbug + Buganizer issue verify B1 SSR idempotency and B4 short-alias search
    self.store.upsert_artifact(
        "crbug:40123456",
        kind="crbug",
        title="Fontations glyph bounds mismatch",
        url="https://issues.chromium.org/issues/40123456",
        state={"status": "OPEN"},
        now=now,
    )
    self.store.link_artifact(wid, "crbug:40123456", role="primary", now=now)

    self.store.upsert_artifact(
        "buganizer:580111222",
        kind="buganizer",
        title="Standalone Buganizer triage item",
        url="https://b.corp.google.com/issues/580111222",
        state={"status": "OPEN"},
        now=now,
    )
    self.store.link_artifact(wid, "buganizer:580111222", role="primary", now=now)

    self.store.upsert_artifact(
        "gerrit:skia:1414545",
        kind="gerrit",
        title="1414545: [skparagraph] Ellipsis after hard break",
        url="https://skia-review.googlesource.com/c/skia/+/1414545",
        state={
            "status": "NEW",
            "is_self": True,
            "role": "author",
            "subject": "[skparagraph] Ellipsis after hard break",
            "patchset": 3,
            "in_development": True,
            "assigned_reviewers_count": 0,
            "linked_bugs": [{"kind": "crbug", "id": "40123456"}],
            "commit_message": (
                "Ellipsis fix\n\n"
                "Fixes https://github.com/flutter/flutter/issues/50168\n"
                "Also refs #123456 and b/580955594\n"
                + ("verbose-body " * 800)
            ),
            "triage": {"raw": "t" * 16000},
            "revisions_by_ps": {"1": {"files": ["a.cpp"] * 400}},
            "revision_ps_by_sha": {"deadbeef": 3},
            "files": ["modules/skparagraph/src/ParagraphImpl.cpp"] * 300,
            "post_submit": {
                "rolls": {
                    "Flutter": {"rolled": True, "detail": "r" * 4000},
                    "Android": {"rolled": False, "detail": "p" * 4000},
                    "Chrome": "pending",
                    "Google3": "✓",
                },
                "raw_log": "l" * 8000,
            },
            "entity_state": {
                "status": "dev",
                "lane": "desk",
                "comments": ["huge-comment " * 800],
            },
        },
        now=now,
    )
    self.store.link_artifact(
        wid,
        "gerrit:skia:1414545",
        role="primary",
        link_reason="author",
        link_detail="Matched CL subject token",
        now=now,
    )

    self.store.upsert_artifact(
        "chat_chain:99887766-aaaa-bbbb-cccc-ddeeff001122",
        kind="chat_chain",
        title="SkParagraph ellipsis investigation",
        url="",
        state={"cascade_id": "99887766-aaaa-bbbb-cccc-ddeeff001122"},
        now=now,
    )
    self.store.link_artifact(
        wid, "chat_chain:99887766-aaaa-bbbb-cccc-ddeeff001122", role="channel", now=now
    )

    self.store.upsert_artifact(
        "gmail_thread:th-slim",
        kind="gmail_thread",
        title="Ellipsis metrics follow-up",
        url="https://mail.google.com/mail/u/0/#all/th-slim",
        state={
            "counterparty": "Florin Malita",
            "message_count": 4,
            "thread_text": "full email thread " * 2000,
            "messages": [{"body": "msg " * 1000}],
        },
        now=now,
    )
    self.store.link_artifact(wid, "gmail_thread:th-slim", role="channel", now=now)

    self.store.upsert_artifact(
        "gmail_draft:dr-slim",
        kind="gmail_draft",
        title="Re: Ellipsis metrics follow-up",
        url="https://mail.google.com/mail/u/0/#drafts/dr-slim",
        state={
            "status": "DRAFT",
            "body": "Hi Florin, uploaded https://skia-review.googlesource.com/c/skia/+/1414545 "
            + ("draft-padding " * 800),
            "snippet": "uploaded 1414545 " + ("snip " * 400),
        },
        now=now,
    )
    self.store.link_artifact(wid, "gmail_draft:dr-slim", role="channel", now=now)

    self.store.add_obligation(
        wid,
        description="Reply once roll lands",
        trigger_expr="always",
        now=now,
    )

    with open(self.cl_state_path, "w", encoding="utf-8") as f:
      json.dump({"items": {}, "bugs": {}}, f)
    with open(self.watcher_state_path, "w", encoding="utf-8") as f:
      json.dump(
          {
              "unanswered_tray": [
                  {
                      "artifact_key": "gmail_thread:t-tray-1",
                      "thread_id": "t-tray-1",
                      "sender": "Alice",
                      "subject": "Ping",
                      "url": "https://mail.google.com/mail/u/0/#all/t-tray-1",
                      "last_message_ts": now,
                  }
              ]
          },
          f,
      )

    srv = workbench_server.create_server(
        host="127.0.0.1",
        port=0,
        store=self.store,
        cl_state_path=self.cl_state_path,
        watcher_state_path=self.watcher_state_path,
        info_path=self.info_path,
    )
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
      with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as resp:
        raw_bytes = resp.read()
        payload = json.loads(raw_bytes.decode("utf-8"))
      with urllib.request.urlopen(f"http://127.0.0.1:{port}/?embed=1", timeout=5) as resp:
        html_out = resp.read().decode("utf-8")
    finally:
      srv.shutdown()
      srv.server_close()

    self.assertLess(len(raw_bytes), 14000)
    self.assertEqual([x["thread_id"] for x in payload["unanswered_tray"]], ["t-tray-1"])
    self.assertEqual(len(payload["works"]), 1)
    card = payload["works"][0]
    for needle in (
        "florin malita",
        "flutter#50168",
        "crbug/40123456",
        "b/580111222",
        "skia 1414545",
        "skia-review.googlesource.com/c/skia/+/1414545",
        "99887766-aaaa-bbbb-cccc-ddeeff001122",
    ):
      self.assertIn(needle, card["search_text"])

    arts_by_key = {a["artifact_key"]: a for a in card["artifacts"]}
    self.assertGreaterEqual(len(arts_by_key), 6)
    cl_st = arts_by_key["gerrit:skia:1414545"]["state"]
    for forbidden in (
        "commit_message",
        "triage",
        "revisions_by_ps",
        "revision_ps_by_sha",
        "files",
        "post_submit",
    ):
      self.assertNotIn(forbidden, cl_st)
    self.assertEqual(cl_st.get("assigned_reviewers_count"), 0)
    self.assertEqual(cl_st.get("entity_state"), {"status": "dev", "lane": "desk"})
    # B5: unrolled string status ('pending') must map to False, while '✓' maps to True
    self.assertEqual(
        cl_st.get("rolls"),
        {"Flutter": True, "Android": False, "Chrome": False, "Google3": True},
    )
    for expected_bug in (
        "github_issue:flutter/flutter:50168",
        "github_issue:flutter/flutter:123456",
        "buganizer:580955594",
        "crbug:40123456",
    ):
      self.assertIn(expected_bug, cl_st.get("linked_bug_keys") or [])
    # B1: second-pass _extract_ssr_cl_bug_keys on slimmed state must preserve crbug:<N>
    self.assertIn("crbug:40123456", workbench_server._extract_ssr_cl_bug_keys(cl_st))

    # Also verify why_tooltip and ambiguity_prompt search indexing on slimmed projections
    custom_slim = workbench_server.slim_ui_projection({
        "works": [{
            "work_id": "w-tip",
            "title": "Tip check",
            "obligations": [{
                "obligation_id": "ob-1",
                "description": "Follow up",
                "ambiguity_prompt": "Confirm closing thread after roll",
            }],
            "artifacts": [{
                "artifact_key": "crbug:40999999",
                "kind": "crbug",
                "title": "Chromium issue",
                "url": "https://issues.chromium.org/issues/40999999",
                "role": "primary",
                "why_tooltip": "Matched custom tooltip reason",
                "state": {"status": "OPEN"},
            }],
        }]
    })
    self.assertIn("matched custom tooltip reason", custom_slim["works"][0]["search_text"])
    self.assertIn("confirm closing thread after roll", custom_slim["works"][0]["search_text"])
    self.assertIn("crbug/40999999", custom_slim["works"][0]["search_text"])

    th_st = arts_by_key["gmail_thread:th-slim"]["state"]
    self.assertNotIn("thread_text", th_st)
    self.assertNotIn("messages", th_st)
    self.assertEqual(th_st.get("counterparty"), "Florin Malita")
    self.assertEqual(th_st.get("message_count"), 4)

    dr_st = arts_by_key["gmail_draft:dr-slim"]["state"]
    self.assertNotIn("body", dr_st)
    self.assertNotIn("snippet", dr_st)
    mentioned_joined = " ".join(str(x) for x in (dr_st.get("mentioned_keys") or []))
    self.assertIn("1414545", mentioned_joined)

    ssr_html = html_out.split("<script>", 1)[0]
    self.assertNotIn("verbose-body", html_out)
    self.assertNotIn("full email thread", html_out)
    self.assertNotIn("Not in draft", ssr_html)

  # Checks lock-free cache serving, generation ordering, bg_sync torn-read guard, and materialize
  def test_nonblocking_projection_cache_serves_under_store_lock_and_invalidates(self) -> None:
    now = 1_762_800_100.0
    self.store.create_work("Initial warm work", work_id="work-warm", now=now)
    with open(self.cl_state_path, "w", encoding="utf-8") as f:
      json.dump({"items": {}, "bugs": {}}, f)

    srv = workbench_server.create_server(
        host="127.0.0.1",
        port=0,
        store=self.store,
        cl_state_path=self.cl_state_path,
        watcher_state_path=self.watcher_state_path,
        info_path=self.info_path,
    )
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_store_lock() -> None:
      with self.store._lock:
        lock_acquired.set()
        release_lock.wait(timeout=5.0)

    holder = threading.Thread(target=hold_store_lock, daemon=True)
    try:
      with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as resp:
        warm = json.loads(resp.read().decode("utf-8"))
      self.assertEqual([w["work_id"] for w in warm["works"]], ["work-warm"])

      with open(self.cl_state_path, "w", encoding="utf-8") as f:
        json.dump({"items": {}, "bugs": {}, "seq": 2}, f)

      holder.start()
      self.assertTrue(lock_acquired.wait(timeout=2.0))

      t0 = time.monotonic()
      with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as resp:
        contended_state = json.loads(resp.read().decode("utf-8"))
      with urllib.request.urlopen(f"http://127.0.0.1:{port}/?embed=1", timeout=2) as resp:
        contended_html = resp.read().decode("utf-8")
      elapsed = time.monotonic() - t0

      self.assertLess(elapsed, 0.35, f"HTTP GET blocked on store._lock for {elapsed:.3f}s")
      self.assertEqual([w["work_id"] for w in contended_state["works"]], ["work-warm"])
      self.assertIn("Initial warm work", contended_html)

      release_lock.set()
      holder.join(timeout=2.0)

      self.store.create_work("Post-lock fresh work", work_id="work-fresh", now=now + 10)
      with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as resp:
        fresh_state = json.loads(resp.read().decode("utf-8"))
      self.assertEqual(
          {w["work_id"] for w in fresh_state["works"]}, {"work-warm", "work-fresh"}
      )

      # B3: bg_sync_guard must keep serving warm snapshot when store._lock unlocks mid-sync
      self.assertTrue(callable(getattr(srv, "bg_sync_guard", None)))
      with srv.bg_sync_guard():
        self.store.create_work("Mid-sync unpurged work", work_id="work-torn", now=now + 20)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as resp:
          during_sync = json.loads(resp.read().decode("utf-8"))
        self.assertNotIn("work-torn", {w["work_id"] for w in during_sync["works"]})
      srv.refresh_snapshot()
      with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as resp:
        after_sync = json.loads(resp.read().decode("utf-8"))
      self.assertIn("work-torn", {w["work_id"] for w in after_sync["works"]})

      # B2a/B2b: slow GET HTML encoding must release store._lock and never overwrite a newer write
      real_render = workbench_server.render_workbench_html
      render_entered = threading.Event()
      allow_slow_finish = threading.Event()
      slow_once = {"armed": True}

      def hooked_render(proj: Any, ui_ver: int, **kwargs: Any) -> str:
        if slow_once["armed"] and threading.current_thread().name != "MainThread":
          slow_once["armed"] = False
          # Verify active_store._lock is NOT held by this HTTP thread while rendering HTML
          acquired_other = [False]

          def probe_lock() -> None:
            if self.store._lock.acquire(blocking=False):
              acquired_other[0] = True
              self.store._lock.release()

          pt = threading.Thread(target=probe_lock)
          pt.start()
          pt.join(timeout=1.0)
          if not acquired_other[0]:
            raise RuntimeError("store._lock held during render_workbench_html")
          render_entered.set()
          allow_slow_finish.wait(timeout=3.0)
        return real_render(proj, ui_ver, **kwargs)

      with mock.patch.object(workbench_server, "render_workbench_html", side_effect=hooked_render):
        self.store.create_work("Gen-1 work", work_id="work-gen1", now=now + 30)
        slow_err: list[Exception] = []

        def run_slow_get() -> None:
          try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as r:
              r.read()
          except Exception as exc:  # pragma: no cover - surfaced via slow_err assertion
            slow_err.append(exc)

        gt = threading.Thread(target=run_slow_get, name="SlowHttpGet")
        gt.start()
        self.assertTrue(render_entered.wait(timeout=2.0), f"Slow GET failed: {slow_err}")
        # Advance store & cache to Gen-2 while SlowHttpGet is still paused in HTML encoding
        self.store.create_work("Gen-2 newer work", work_id="work-gen2", now=now + 40)
        srv.refresh_snapshot()
        allow_slow_finish.set()
        gt.join(timeout=3.0)
        self.assertEqual(slow_err, [])

      with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as resp:
        post_race = json.loads(resp.read().decode("utf-8"))
      self.assertIn("work-gen2", {w["work_id"] for w in post_race["works"]})

      # B2c: POST /api/work/draft/materialize must not mutate snapshot_cache["proj"] in place
      with mock.patch.object(
          workbench_server.ingestor, "materialize_reply_draft", return_value={"draft_id": "d-123"}
      ):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/work/draft/materialize",
            data=json.dumps({"work_id": "work-warm"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
          mat_resp = json.loads(resp.read().decode("utf-8"))
      self.assertEqual(mat_resp.get("materialized"), {"draft_id": "d-123"})
      self.assertNotIn("materialized", srv.get_cached_projection())
    finally:
      release_lock.set()
      srv.shutdown()
      srv.server_close()

  # Checks 60s polling skips innerHTML wipes (including '2h 14m ago') and single initial postMessage
  def test_client_60s_poll_skips_noop_dom_wipe_and_rebuilds_on_real_change(self) -> None:
    base_proj: dict[str, Any] = {
        "synced_et": "16:40 ET (just now)",
        "ui_version": 42,
        "counts": {
            "all_active": 2,
            "needs_you": 1,
            "on_desk": 1,
            "waiting": 0,
            "done": 0,
            "obligations_open": 0,
            "unanswered": 0,
        },
        "metrics": {"link_precision": 1.0},
        "unanswered_tray": [],
        "works": [
            {
                "work_id": "w-needs",
                "title": "Urgent review",
                "lane": "needs-you",
                "work_types": ["reviews"],
                "needs_you_types": ["reviews"],
                "activity_at": 5000,
                "activity_et": "14:26 ET (2h 14m ago)",
                "updated_at": 5000,
                "updated_et": "14:26 ET (2h 14m ago)",
                "origin_at": 4000,
                "origin_et": "13:15 ET (3h 25m ago)",
                "reasons": ["Reviewer action required"],
                "obligations": [],
                "checklist": [
                    {
                        "bug_key": "buganizer:580955594",
                        "short_id": "b/580955594",
                        "title": "Paragraph metrics",
                        "stage_label": "In review",
                        "cls": [
                            {
                                "artifact_key": "gerrit:skia:1414545",
                                "label": "Gerrit 1414545 PS3",
                                "updated_et": "14:26 ET (2h 14m ago)",
                            }
                        ],
                    }
                ],
                "artifacts": [],
            },
            {
                "work_id": "w-desk",
                "title": "Prepared local CL",
                "lane": "on-desk",
                "desk_tier": 0,
                "work_types": ["my-cls"],
                "needs_you_types": [],
                "activity_at": 4900,
                "activity_et": "16:30 ET (10m ago)",
                "updated_at": 4900,
                "updated_et": "16:30 ET (10m ago)",
                "origin_at": 3900,
                "origin_et": "16:10 ET (30m ago)",
                "reasons": ["No reviewers assigned"],
                "obligations": [],
                "checklist": [],
                "artifacts": [],
            },
        ],
    }
    html_out = workbench_server.render_workbench_html(base_proj, ui_version=42)
    m = re.search(r"<script>\n(.*?)\n</script>", html_out, re.DOTALL)
    self.assertIsNotNone(m)
    assert m is not None
    script_js = m.group(1)

    lane_chips: list[dict[str, str]] = []
    type_chips: list[dict[str, str]] = []
    for btn_m in re.finditer(r'<button class="wb-chip"([^>]*)>', html_out):
      attr_map = {
          k: html_lib.unescape(v)
          for k, v in re.findall(r'([a-zA-Z0-9_-]+)="([^"]*)"', btn_m.group(1))
      }
      if "data-filter" in attr_map:
        lane_chips.append(attr_map)
      elif "data-type" in attr_map:
        type_chips.append(attr_map)
    self.assertEqual(len(lane_chips), 5)
    self.assertEqual(len(type_chips), 5)

    # Poll 1: synced_et and composite '(2h 15m ago)' suffixes tick; zero domain fields change
    noop_poll_proj = json.loads(json.dumps(base_proj))
    noop_poll_proj["synced_et"] = "16:41 ET (just now)"
    noop_poll_proj["works"][0]["activity_et"] = "14:26 ET (2h 15m ago)"
    noop_poll_proj["works"][0]["updated_et"] = "14:26 ET (2h 15m ago)"
    noop_poll_proj["works"][0]["origin_et"] = "13:15 ET (3h 26m ago)"
    noop_poll_proj["works"][0]["checklist"][0]["cls"][0]["updated_et"] = "14:26 ET (2h 15m ago)"
    noop_poll_proj["works"][1]["activity_et"] = "16:30 ET (11m ago)"
    noop_poll_proj["works"][1]["updated_et"] = "16:30 ET (11m ago)"

    # Poll 2: real domain mutation on the On Desk card
    changed_poll_proj = json.loads(json.dumps(noop_poll_proj))
    changed_poll_proj["synced_et"] = "16:42 ET (just now)"
    changed_poll_proj["works"][1]["reasons"] = ["WIP / Draft updated"]

    harness_arg = json.dumps({
        "script_js": script_js,
        "lane_chips": lane_chips,
        "type_chips": type_chips,
        "noop_poll": noop_poll_proj,
        "changed_poll": changed_poll_proj,
    })

    node_script = r"""
const assert = require('assert');
const cfg = JSON.parse(process.argv[1]);
let cardsInnerWrites = 0;
let chipInnerWrites = 0;
let pollFn = null;
let pollMs = 0;
let nextPollPayload = cfg.noop_poll;
const parentMsgs = [];
let scrollPos = 240;
const scrolledTo = [];

function makeEl(id, extra = {}) {
  const attrs = Object.assign({}, extra.attrs || {});
  const listeners = {};
  let htmlVal = extra.innerHTML || '';
  return Object.assign({
    id,
    value: extra.value || '',
    textContent: extra.textContent || '',
    disabled: false,
    get innerHTML() { return htmlVal; },
    set innerHTML(v) {
      htmlVal = String(v || '');
      if (id === 'wb-cards-container') cardsInnerWrites += 1;
      if (String(id).startsWith('lane-') || String(id).startsWith('type-')) chipInnerWrites += 1;
    },
    getAttribute: (k) => (Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null),
    setAttribute: (k, v) => { attrs[k] = String(v); },
    hasAttribute: (k) => Object.prototype.hasOwnProperty.call(attrs, k),
    addEventListener: (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); },
    dispatch: (ev, obj) => { for (const fn of (listeners[ev] || [])) fn(obj || {}); },
  }, extra.props || {});
}

const els = Object.fromEntries([
  'wb-synced', 'kpi-needs', 'kpi-wait', 'kpi-obs', 'kpi-tray', 'kpi-prec',
  'wb-tray-container', 'wb-cards-container', 'wb-new-btn', 'wb-cancel-new',
  'wb-sync-btn', 'wb-search',
].map((id) => [id, makeEl(id)]));
els['wb-new-form'] = makeEl('wb-new-form', { props: { reset() {} } });
els['wb-toggle-collapse'] = makeEl('wb-toggle-collapse', { attrs: { 'data-action': 'collapse' } });
els['wb-sort-mode'] = makeEl('wb-sort-mode', { value: 'triage' });
els['wb-sort-dir'] = makeEl('wb-sort-dir', { attrs: { 'data-dir': 'desc' }, textContent: '↓ New' });

function chipHtml(attrs) {
  return `${attrs['data-base-label']} <span class="wb-chip-cnt">(${attrs['data-count']})</span>`;
}
const laneChips = cfg.lane_chips.map(
  (attrs) => makeEl('lane-' + attrs['data-filter'], { attrs, innerHTML: chipHtml(attrs) })
);
const typeChips = cfg.type_chips.map(
  (attrs) => makeEl('type-' + attrs['data-type'], { attrs, innerHTML: chipHtml(attrs) })
);
const allChips = laneChips.concat(typeChips);

global.localStorage = { getItem: () => null, setItem: () => {} };
global.window = {
  parent: { postMessage: (msg) => parentMsgs.push(msg) },
  get scrollY() { return scrollPos; },
  scrollTo: (x, y) => { scrollPos = y; scrolledTo.push([x, y]); },
  location: { reload: () => {} },
  addEventListener: () => {},
  getSelection: () => '',
  prompt: () => '',
  alert: () => {},
};
global.setInterval = (fn, ms) => { pollFn = fn; pollMs = ms; return 1; };
global.fetch = async () => ({ ok: true, json: async () => nextPollPayload });
global.document = {
  activeElement: null,
  getElementById: (id) => els[id] || makeEl(id),
  querySelectorAll: (sel) => (sel === '.wb-chip' ? allChips : []),
  createElement: () => {
    let txt = '';
    return {
      set textContent(v) { txt = String(v || ''); },
      get innerHTML() {
        return txt.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      },
    };
  },
  body: { addEventListener: () => {} },
};

eval(cfg.script_js);

;(async () => {
  // B8: initial hydration must emit jetski-workbench-rendered once, only after render() completes
  assert.strictEqual(
    parentMsgs.filter((m) => m && m.type === 'jetski-workbench-rendered').length,
    1,
    'Initial hydration must emit jetski-workbench-rendered exactly once after render()'
  );
  assert.strictEqual(pollMs, 60000, 'Workbench background poll must use a 60000ms interval');
  assert.strictEqual(typeof pollFn, 'function');

  const onDeskChip = laneChips.find((c) => c.getAttribute('data-filter') === 'on-desk');
  onDeskChip.dispatch('click');
  assert(els['wb-cards-container'].innerHTML.includes('data-work-id="w-desk"'));
  assert(!els['wb-cards-container'].innerHTML.includes('data-work-id="w-needs"'));

  const writesBeforeNoop = cardsInnerWrites;
  const chipWritesBeforeNoop = chipInnerWrites;
  parentMsgs.length = 0;

  await pollFn();
  assert.strictEqual(
    cardsInnerWrites,
    writesBeforeNoop,
    'No-op /api/state poll (including composite 2h 15m ago) must skip innerHTML rebuilds'
  );
  assert.strictEqual(
    chipInnerWrites,
    chipWritesBeforeNoop,
    'Unchanged filter counts must not rewrite chip.innerHTML'
  );
  assert.strictEqual(els['wb-synced'].textContent, 'Synced 16:41 ET (just now)');
  assert(
    parentMsgs.some((m) => m && m.type === 'jetski-workbench-rendered'),
    'No-op poll must still heartbeat jetski-workbench-rendered to parent watchdog'
  );

  nextPollPayload = cfg.changed_poll;
  scrollPos = 312;
  scrolledTo.length = 0;
  await pollFn();
  assert.strictEqual(cardsInnerWrites, writesBeforeNoop + 1);
  assert(els['wb-cards-container'].innerHTML.includes('WIP / Draft updated'));
  assert(!els['wb-cards-container'].innerHTML.includes('data-work-id="w-needs"'));
  assert.deepStrictEqual(scrolledTo[scrolledTo.length - 1], [0, 312]);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    res = subprocess.run(
        ["node", "-e", node_script, harness_arg],
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(
        res.returncode,
        0,
        f"Node poll/flicker check failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )

  # Checks PEN URL isolation, 25s load timeout, and mode-switch race/version isolation (B7)
  def test_jetski_closed_filter_isolates_workbench_pen_base_and_load_timeout(self) -> None:
    js_path = os.path.join(REPO_ROOT, "scripts", "jetski_closed_filter.js")
    with open(js_path, "r", encoding="utf-8") as f:
      src = f.read()

    m_timeout = re.search(r"const\s+FLIGHT_DECK_LOAD_TIMEOUT_MS\s*=\s*(\d+)\s*;", src)
    self.assertIsNotNone(m_timeout)
    assert m_timeout is not None
    self.assertGreaterEqual(
        int(m_timeout.group(1)),
        20000,
        "FLIGHT_DECK_LOAD_TIMEOUT_MS must be >= 20000ms to avoid 12s reload loops",
    )

    node_check = r"""
const assert = require('assert');
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');

const elsById = new Map();
function makeDomNode(tag) {
  const attrs = {};
  const children = [];
  return {
    tagName: String(tag || 'div').toUpperCase(),
    id: '',
    className: '',
    textContent: '',
    style: {},
    children,
    parentElement: null,
    nextSibling: null,
    appendChild(ch) {
      ch.parentElement = this;
      if (children.length) children[children.length - 1].nextSibling = ch;
      children.push(ch);
      if (ch.id) elsById.set(ch.id, ch);
      return ch;
    },
    insertBefore(ch) { return this.appendChild(ch); },
    remove() {},
    setAttribute(k, v) {
      attrs[k] = String(v);
      if (k === 'id') elsById.set(String(v), this);
    },
    getAttribute(k) {
      return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null;
    },
    removeAttribute(k) { delete attrs[k]; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k); },
    addEventListener() {},
    removeEventListener() {},
    querySelector(sel) {
      if (sel.startsWith('.')) {
        const cls = sel.slice(1);
        return children.find((c) => String(c.className || '').split(/\s+/).includes(cls)) || null;
      }
      return null;
    },
    querySelectorAll() { return []; },
  };
}

let fetchHandler = async () => ({ ok: false });
const body = makeDomNode('body');
const docEl = makeDomNode('html');
global.localStorage = { getItem: () => null, setItem: () => {} };
global.MutationObserver = class { observe() {} disconnect() {} };
global.document = {
  hidden: false,
  body,
  documentElement: docEl,
  head: makeDomNode('head'),
  createElement: (t) => makeDomNode(t),
  getElementById: (id) => elsById.get(id) || null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {},
  removeEventListener: () => {},
};
global.window = {
  location: {
    hostname: 'cloudtop.corp.goog',
    origin: 'https://cloudtop.corp.goog',
    href: 'https://cloudtop.corp.goog/',
  },
  localStorage: global.localStorage,
  document: global.document,
  addEventListener: () => {},
  removeEventListener: () => {},
  fetch: (url) => fetchHandler(url),
  getComputedStyle: () => ({ display: 'block', visibility: 'visible' }),
};
global.navigator = { clipboard: { writeText: async () => {} } };

eval(src);
const api = global.window.__jetskiFlightDeck;
assert(api, '__jetskiFlightDeck must be registered');

const fdInfo = {
  local_url: 'http://localhost:8089',
  pen_url: 'https://8089-fd-pen.corp.goog',
  port: 8089,
};
const wbInfo = {
  local_url: 'http://localhost:8090',
  pen_url: 'https://8090-wb-pen.corp.goog',
  port: 8090,
};

api.setMode('flight-deck', false);
assert.strictEqual(api.resolveUrl(fdInfo, true), 'https://8089-fd-pen.corp.goog?embed=1');

api.setMode('workbench', false);
assert.strictEqual(api.resolveUrl(wbInfo, true), 'https://8090-wb-pen.corp.goog?embed=1');

const stalledWorkUrl = {
  local_url: 'http://localhost:8090?v=7',
  pen_url: 'http://localhost:8090?v=7',
  port: 8090,
};
assert.strictEqual(
  api.resolveUrl(stalledWorkUrl, true),
  'https://8090-wb-pen.corp.goog?v=7&embed=1'
);

api.setMode('flight-deck', false);
const stalledFdUrl = {
  local_url: 'http://localhost:8089?v=8',
  pen_url: 'http://localhost:8089?v=8',
  port: 8089,
};
assert.strictEqual(
  api.resolveUrl(stalledFdUrl, true),
  'https://8089-fd-pen.corp.goog?v=8&embed=1'
);

;(async () => {
  // B7: stale Flight Deck info fetch resolving after switching to Workbench must not clobber src
  let resolveSlowFd = null;
  fetchHandler = (url) => {
    if (String(url).includes('jetski-flight-deck.json')) {
      return new Promise((resolve) => { resolveSlowFd = resolve; });
    }
    return Promise.resolve({
      ok: true,
      json: async () => ({
        local_url: 'http://localhost:8090?v=200',
        pen_url: 'https://8090-wb-pen.corp.goog?v=200',
        port: 8090,
        ui_version: 200,
      }),
    });
  };

  api.setMode('flight-deck', true);
  assert.strictEqual(typeof resolveSlowFd, 'function');
  const drawer = elsById.get('jetski-flight-deck-drawer');
  assert(drawer, 'Flight Deck drawer must be mounted');
  const iframe = drawer.querySelector('.jetski-flight-deck-iframe');
  assert(iframe, 'Drawer iframe must be mounted');

  // Simulate Flight Deck previously rendering version 100 before mode switch to Workbench (200)
  api.handleMessage({ data: { type: 'jetski-flight-deck-rendered', uiVersion: 100 } });
  let pingMsgs = 0;
  iframe.contentWindow = { postMessage: () => { pingMsgs += 1; } };

  api.setMode('workbench', true);
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(
    iframe.getAttribute('src'),
    'https://8090-wb-pen.corp.goog?v=200&embed=1',
    'Initial Workbench switch must load clean 8090 PEN src without cache-busting &t='
  );

  // Resolve delayed Flight Deck fetch and verify it is ignored while Workbench mode is active
  resolveSlowFd({
    ok: true,
    json: async () => ({
      local_url: 'http://localhost:8089?v=100',
      pen_url: 'https://8089-fd-pen.corp.goog?v=100',
      port: 8089,
      ui_version: 100,
    }),
  });
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(
    iframe.getAttribute('src'),
    'https://8090-wb-pen.corp.goog?v=200&embed=1',
    'Stale Flight Deck info fetch must not overwrite active Workbench iframe src'
  );

  // Re-running ensureFlightDeckUI during cold Workbench load must not send premature 8s ping
  pingMsgs = 0;
  api.ensureFlightDeckUI();
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(
    pingMsgs,
    0,
    'Cold-loading Workbench must not inherit Flight Deck baselineVer=100 and fire an 8s ping probe'
  );
  api.setOpen(false);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    res = subprocess.run(
        ["node", "-e", node_check, js_path],
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(
        res.returncode,
        0,
        f"Node PEN isolation check failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )


if __name__ == "__main__":
  unittest.main()
