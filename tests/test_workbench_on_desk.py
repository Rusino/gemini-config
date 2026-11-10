#!/usr/bin/env python3
"""Contract unit tests for Workbench (:8090) On Desk lane and Focus-bar search placement."""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
  sys.path.insert(0, REPO_ROOT)

from workbench import work_store  # noqa: E402
from workbench import workbench_server  # noqa: E402


def _card(
    wid: str,
    title: str,
    lane: str,
    work_types: list[str],
    activity_at: int,
    origin_at: int,
    reason: str,
    *,
    desk_tier: int = 0,
    needs_you_types: list[str] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
  """Builds a minimal UI projection card fixture for SSR and client-JS filter/peer checks."""
  return {
      "work_id": wid,
      "title": title,
      "lane": lane,
      "desk_tier": desk_tier,
      "work_types": work_types,
      "needs_you_types": list(needs_you_types or []),
      "activity_at": activity_at,
      "origin_at": origin_at,
      "obligations": [],
      "artifacts": list(artifacts or []),
      "checklist": [],
      "reasons": [reason],
  }


class WorkbenchOnDeskTest(unittest.TestCase):
  """Verifies On Desk lane routing, sub-tier ordering, peer stages, and Focus search bar."""

  def setUp(self) -> None:
    self._tmp = tempfile.TemporaryDirectory()
    self.db_path = os.path.join(self._tmp.name, "workbench.db")
    self.store = work_store.WorkStore(self.db_path)

  def tearDown(self) -> None:
    self._tmp.cleanup()

  # Checks prepared self CLs route to on-desk with sub-tiers 0/1/2 while obligations/conflicts hold
  def test_on_desk_classification_subtiers_and_guards(self) -> None:
    now = 1_760_000_000.0

    # Explicit 0-reviewer metadata without in_development + conflicted pre-review CL stay tier 0
    wid_norev = self.store.create_work("CoreText line break fix", work_id="work-norev", now=now)
    self.store.upsert_artifact(
        "gerrit:skia:1411677",
        kind="gerrit",
        title="1411677: [SkoreText] Fix two line break width checks",
        url="https://skia-review.googlesource.com/c/skia/+/1411677",
        state={
            "status": "NEW",
            "is_self": True,
            "role": "author",
            "is_wip": False,
            "assigned_reviewers_count": 0,
            "reviewer_status": "No reviewers assigned",
            "merge_conflict": True,
            "subject": "[SkoreText] Fix two line break width checks",
            "activity_ts": now - 300,
            "entity_state": {"status": "dev", "lane": "desk"},
        },
        now=now,
    )
    self.store.link_artifact(
        wid_norev, "gerrit:skia:1411677", role="primary", source="manual", now=now
    )

    # Multi-CL Work picks min sub-tier (WIP=1 over persisted title-only DO NOT SUBMIT=2)
    # while ignoring context_only and companion CL artifacts
    wid_wip_conf = self.store.create_work("Span background WIP", work_id="work-wip-conf", now=now)
    self.store.upsert_artifact(
        "gerrit:skia:1419003",
        kind="gerrit",
        title="1419003: DO NOT SUBMIT: background height probe",
        url="https://skia-review.googlesource.com/c/skia/+/1419003",
        state={
            "status": "NEW",
            "is_self": True,
            "role": "author",
            "is_wip": True,
            "activity_ts": now - 210,
            "entity_state": {"status": "draft", "lane": "desk"},
        },
        now=now,
    )
    self.store.upsert_artifact(
        "github_pr:flutter/flutter:193774",
        kind="github_pr",
        title="193774: [web_paragraph] Apply StrutStyle to line metrics",
        url="https://github.com/flutter/flutter/pull/193774",
        state={
            "status": "OPEN",
            "is_self": True,
            "role": "author",
            "is_wip": True,
            "is_draft": True,
            "merge_conflict": True,
            "subject": "[web_paragraph] Apply StrutStyle to line metrics",
            "activity_ts": now - 200,
            "entity_state": {"status": "draft", "lane": "desk"},
        },
        now=now,
    )
    self.store.upsert_artifact(
        "gerrit:skia:1499001",
        kind="gerrit",
        title="1499001: Passing context-only 0-reviewer mention",
        state={
            "status": "NEW",
            "is_self": True,
            "role": "author",
            "in_development": True,
            "assigned_reviewers_count": 0,
            "reviewer_status": "No reviewers assigned",
        },
        now=now,
    )
    self.store.upsert_artifact(
        "gerrit:skia:1499002",
        kind="gerrit",
        title="1499002: [Companion WIP] Probe for external review",
        state={
            "status": "NEW",
            "is_self": True,
            "role": "author",
            "is_companion": True,
            "in_development": True,
            "assigned_reviewers_count": 0,
        },
        now=now,
    )
    self.store.link_artifact(wid_wip_conf, "gerrit:skia:1419003", role="primary", now=now)
    self.store.link_artifact(
        wid_wip_conf, "github_pr:flutter/flutter:193774", role="primary", now=now
    )
    self.store.link_artifact(wid_wip_conf, "gerrit:skia:1499001", role="context_only", now=now)
    self.store.link_artifact(wid_wip_conf, "gerrit:skia:1499002", role="channel", now=now)

    # Multi-bug Work with 0-reviewer CL keeps On Desk checklist badge (never "In review")
    wid_bugs = self.store.create_work("Two-bug CoreText fix", work_id="work-bugs-desk", now=now)
    for bnum in ("50000001", "50000002"):
      self.store.upsert_artifact(
          f"buganizer:{bnum}",
          kind="buganizer",
          title=f"b/{bnum}: CoreText metric drift",
          url=f"https://b.corp.google.com/issues/{bnum}",
          state={"status": "OPEN"},
          now=now,
      )
      self.store.link_artifact(
          wid_bugs, f"buganizer:{bnum}", role="primary", source="manual", now=now
      )
    self.store.upsert_artifact(
        "gerrit:skia:1411680",
        kind="gerrit",
        title="1411680: Align CoreText metrics",
        url="https://skia-review.googlesource.com/c/skia/+/1411680",
        state={
            "status": "NEW",
            "is_self": True,
            "role": "author",
            "is_wip": False,
            "in_development": True,
            "assigned_reviewers_count": 0,
            "reviewer_status": "No reviewers assigned",
            "bug_ids": ["50000001", "50000002"],
            "activity_ts": now - 250,
            "entity_state": {"status": "dev", "lane": "desk"},
        },
        now=now,
    )
    self.store.link_artifact(
        wid_bugs, "gerrit:skia:1411680", role="primary", source="manual", now=now
    )

    # Open obligation keeps parked correspondence Work in waiting even when its CL is On Desk
    wid_parked = self.store.create_work("Renzo follow-up", work_id="work-parked", now=now)
    for bnum in ("60000001", "60000002"):
      self.store.upsert_artifact(
          f"buganizer:{bnum}",
          kind="buganizer",
          title=f"b/{bnum}: Span background seam",
          state={"status": "OPEN"},
          now=now,
      )
      self.store.link_artifact(
          wid_parked, f"buganizer:{bnum}", role="primary", source="manual", now=now
      )
    self.store.upsert_artifact(
        "gerrit:skia:1420016",
        kind="gerrit",
        title="1420016: [skparagraph] Unify span background height",
        url="https://skia-review.googlesource.com/c/skia/+/1420016",
        state={
            "status": "NEW",
            "is_self": True,
            "role": "author",
            "is_wip": False,
            "in_development": True,
            "assigned_reviewers_count": 0,
            "reviewer_status": "No reviewers assigned",
            "bug_ids": ["60000001", "60000002"],
            "subject": "[skparagraph] Unify span background height",
            "activity_ts": now - 150,
            "entity_state": {"status": "dev", "lane": "desk"},
        },
        now=now,
    )
    self.store.link_artifact(wid_parked, "gerrit:skia:1420016", role="primary", now=now)
    self.store.add_obligation(
        wid_parked,
        description="Send reply once rolled",
        trigger_expr="rolled_into(Flutter, gerrit:skia:1420016)",
        now=now,
    )

    # Active in-review self CL with merge_conflict stays urgent in needs-you
    wid_conflict = self.store.create_work("Active conflict CL", work_id="work-conflict", now=now)
    self.store.upsert_artifact(
        "gerrit:skia:1404137",
        kind="gerrit",
        title="1404137: [skparagraph] Zero-width cluster spacing",
        url="https://skia-review.googlesource.com/c/skia/+/1404137",
        state={
            "status": "NEW",
            "is_self": True,
            "role": "author",
            "is_wip": False,
            "merge_conflict": True,
            "subject": "[skparagraph] Zero-width cluster spacing",
            "activity_ts": now - 100,
            "entity_state": {"status": "conflict", "lane": "you"},
        },
        now=now,
    )
    self.store.link_artifact(wid_conflict, "gerrit:skia:1404137", role="primary", now=now)

    # Minimal self CL stub without pre-review metadata stays in waiting
    wid_wait = self.store.create_work("In-review CL", work_id="work-in-review", now=now)
    self.store.upsert_artifact(
        "gerrit:skia:1422516",
        kind="gerrit",
        title="1422516: [skparagraph] Derive resolvedFonts",
        url="https://skia-review.googlesource.com/c/skia/+/1422516",
        state={
            "status": "NEW",
            "is_mine": True,
            "activity_ts": now - 90,
            "entity_state": {"lane": "waiting"},
        },
        now=now,
    )
    self.store.link_artifact(wid_wait, "gerrit:skia:1422516", role="primary", now=now)

    cl_state = {
        "items": {
            "gerrit:1415297": {
                "kind": "gerrit",
                "target_id": "1415297",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "is_self": True,
                "role": "author",
                "is_wip": False,
                "in_development": True,
                "merge_conflict": True,
                "subject": "DO NOT SUBMIT: Windows CJK crispness experiment",
                "url": "https://skia-review.googlesource.com/c/skia/+/1415297",
                "activity_ts": now - 10,
                "entity_state": {"status": "dev", "lane": "desk"},
            },
            "gerrit:1419001": {
                "kind": "gerrit",
                "target_id": "1419001",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "is_self": True,
                "role": "author",
                "is_wip": False,
                "in_development": False,
                "subject": "Control: SkParagraph baseline",
                "url": "https://skia-review.googlesource.com/c/skia/+/1419001",
                "activity_ts": now - 20,
                "entity_state": {"status": "waiting", "lane": "waiting"},
            },
            "gerrit:1419002": {
                "kind": "gerrit",
                "target_id": "1419002",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "is_self": True,
                "role": "author",
                "is_wip": False,
                "in_development": True,
                "subject": "[SkoreText] Control: CoreText baseline",
                "url": "https://skia-review.googlesource.com/c/skia/+/1419002",
                "activity_ts": now - 30,
                "entity_state": {"status": "dev", "lane": "desk"},
            },
            "gerrit:1414542": {
                "kind": "gerrit",
                "target_id": "1414542",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "is_self": True,
                "role": "author",
                "is_wip": True,
                "subject": "Prefer matchFamilyStyle in FontCollection",
                "url": "https://skia-review.googlesource.com/c/skia/+/1414542",
                "activity_ts": now - 50,
                "entity_state": {"status": "draft", "lane": "desk"},
            },
        },
        "bugs": {},
    }

    self.store.evaluate_all_obligations(now=now)
    proj = self.store.build_dashboard_projection(cl_watcher_state=cl_state, now=now)
    by_id = {w["work_id"]: w for w in proj["works"]}

    self.assertEqual(by_id["work-norev"]["lane"], "on-desk")
    self.assertEqual(by_id["work-norev"]["desk_tier"], 0)
    self.assertEqual(
        by_id["work-norev"]["reasons"],
        ["No reviewers assigned: Gerrit 1411677 · ⚠️ Merge conflict"],
    )

    self.assertEqual(by_id["work-wip-conf"]["lane"], "on-desk")
    self.assertEqual(by_id["work-wip-conf"]["desk_tier"], 1)
    self.assertEqual(
        by_id["work-wip-conf"]["reasons"],
        [
            "WIP / Draft: PR #193774 · ⚠️ Merge conflict",
            "DO NOT SUBMIT probe: Gerrit 1419003",
        ],
    )

    self.assertEqual(by_id["work-bugs-desk"]["lane"], "on-desk")
    self.assertEqual(by_id["work-bugs-desk"]["desk_tier"], 0)
    self.assertEqual(len(by_id["work-bugs-desk"]["checklist"]), 2)
    for chk_row in by_id["work-bugs-desk"]["checklist"]:
      self.assertEqual(chk_row["stage_tone"], "warn")
      self.assertNotIn("In review", chk_row["stage_label"])

    self.assertEqual(by_id["virt-gerrit:skia:1414542"]["lane"], "on-desk")
    self.assertEqual(by_id["virt-gerrit:skia:1414542"]["desk_tier"], 1)
    self.assertEqual(
        by_id["virt-gerrit:skia:1414542"]["reasons"], ["WIP / Draft: Gerrit 1414542"]
    )

    self.assertEqual(by_id["virt-gerrit:skia:1415297"]["lane"], "on-desk")
    self.assertEqual(by_id["virt-gerrit:skia:1415297"]["desk_tier"], 2)
    self.assertEqual(
        by_id["virt-gerrit:skia:1415297"]["reasons"],
        ["DO NOT SUBMIT probe: Gerrit 1415297 · ⚠️ Merge conflict"],
    )

    for probe_id in ("1419001", "1419002"):
      vkey = f"virt-gerrit:skia:{probe_id}"
      self.assertEqual(by_id[vkey]["lane"], "on-desk")
      self.assertEqual(by_id[vkey]["desk_tier"], 2)
      self.assertEqual(by_id[vkey]["reasons"], [f"DO NOT SUBMIT probe: Gerrit {probe_id}"])

    self.assertEqual(by_id["work-parked"]["lane"], "waiting")
    self.assertTrue(by_id["work-parked"]["reasons"][0].startswith("⏳ Parked:"))
    self.assertNotIn("in review", by_id["work-parked"]["obligations"][0]["progress_summary"])
    self.assertEqual(by_id["work-conflict"]["lane"], "needs-you")
    self.assertEqual(by_id["work-in-review"]["lane"], "waiting")

    # Sub-tier ordering inside on-desk: tier 0 -> tier 1 (newest first) -> tier 2 (newest first)
    desk_ids = [w["work_id"] for w in proj["works"] if w["lane"] == "on-desk"]
    self.assertEqual(
        desk_ids,
        [
            "work-bugs-desk",
            "work-norev",
            "virt-gerrit:skia:1414542",
            "work-wip-conf",
            "virt-gerrit:skia:1415297",
            "virt-gerrit:skia:1419001",
            "virt-gerrit:skia:1419002",
        ],
    )
    counts = proj["counts"]
    self.assertEqual(counts["needs_you"], 1)
    self.assertEqual(counts["on_desk"], 7)
    self.assertEqual(counts["waiting"], 2)
    self.assertEqual(counts["all_active"], 10)
    self.assertEqual(counts["my_cls"], 10)

    # SSR deliverable peer badges on real projection must never stamp On Desk CLs as "In review"
    html_real = workbench_server.render_workbench_html(proj, ui_version=4)
    for wid in ("work-bugs-desk", "virt-gerrit:skia:1419001", "virt-gerrit:skia:1419002"):
      card_m = re.search(
          rf'<article class="wb-card" data-work-id="{re.escape(wid)}".*?</article>',
          html_real,
          re.DOTALL,
      )
      self.assertIsNotNone(card_m)
      assert card_m is not None
      self.assertNotIn(">In review<", card_m.group(0))
      self.assertIn('class="wb-stage-badge re-stage" data-tone="warn"', card_m.group(0))

  # Checks #wb-search flex shrink CSS in Focus bar, peer open/stage parity, and 5x5 filter matrix
  def test_ssr_and_js_on_desk_filter_matrix_and_search_in_focus_bar(self) -> None:
    open_pr_norev_art = {
        "artifact_key": "github_pr:flutter/flutter:194001",
        "kind": "github_pr",
        "role": "primary",
        "title": "194001: [web_paragraph] Tighten line metrics",
        "url": "https://github.com/flutter/flutter/pull/194001",
        "state": {
            "status": "OPEN",
            "is_self": True,
            "is_wip": False,
            "in_development": True,
            "assigned_reviewers_count": 0,
            "subject": "[web_paragraph] Tighten line metrics",
        },
    }
    dns_probe_art = {
        "artifact_key": "gerrit:skia:1415297",
        "kind": "gerrit",
        "role": "primary",
        "title": "1415297: DO NOT SUBMIT: Windows CJK crispness experiment",
        "url": "https://skia-review.googlesource.com/c/skia/+/1415297",
        "state": {
            "status": "NEW",
            "is_self": True,
            "is_wip": False,
            "in_development": True,
            "subject": "DO NOT SUBMIT: Windows CJK crispness experiment",
        },
    }
    ui_proj = {
        "synced_et": "Nov 10, 3:15 PM ET",
        "counts": {
            "all_active": 6,
            "needs_you": 1,
            "on_desk": 3,
            "waiting": 2,
            "done": 1,
            "obligations_open": 1,
            "unanswered": 0,
        },
        "metrics": {"link_precision": 1.0},
        "unanswered_tray": [],
        "works": [
            _card(
                "w_needs_cl",
                "Conflict on self CL",
                "needs-you",
                ["my-cls"],
                5000,
                1000,
                "⚠️ Merge conflict: Skia 1404137",
                needs_you_types=["my-cls"],
            ),
            _card(
                "w_desk_norev",
                "Alpha no-reviewer PR",
                "on-desk",
                ["my-cls"],
                4000,
                1500,
                "No reviewers assigned: PR #194001",
                desk_tier=0,
                artifacts=[open_pr_norev_art],
            ),
            _card(
                "w_desk_wip",
                "Beta WIP draft PR",
                "on-desk",
                ["my-cls", "my-bugs"],
                6000,
                2000,
                "WIP / Draft: flutter#193774",
                desk_tier=1,
            ),
            _card(
                "w_desk_dns",
                "Alpha DO NOT SUBMIT benchmark probe",
                "on-desk",
                ["my-cls"],
                9000,
                2500,
                "DO NOT SUBMIT probe: Skia 1415297",
                desk_tier=2,
                artifacts=[dns_probe_art],
            ),
            _card(
                "w_wait_cl",
                "In-review self CL",
                "waiting",
                ["my-cls"],
                4500,
                1800,
                "Waiting for reviewer",
            ),
            _card(
                "w_wait_rev",
                "External review waiting on author",
                "waiting",
                ["reviews"],
                3500,
                1200,
                "Waiting on author",
            ),
            _card(
                "w_done_cl",
                "Merged Alpha CL",
                "done",
                ["my-cls"],
                2000,
                900,
                "All CLs merged and obligations satisfied",
            ),
        ],
    }

    html_out = workbench_server.render_workbench_html(ui_proj, ui_version=4)

    # Verify #wb-search moved out of Row 1 (.wb-filters) into shorter Row 2 (.wb-type-filters)
    # with shrink-before-wrap flex sizing (hypothetical main size 110px, capped at 165px)
    row1_m = re.search(
        r'<div class="wb-filters">(.*?)</div>\s*<div class="wb-type-filters">',
        html_out,
        re.DOTALL,
    )
    row2_m = re.search(r'<div class="wb-type-filters">(.*?)</div>\s*</header>', html_out, re.DOTALL)
    search_css_m = re.search(r"\.wb-search\s*\{([^}]*)\}", html_out)
    self.assertIsNotNone(row1_m)
    self.assertIsNotNone(row2_m)
    self.assertIsNotNone(search_css_m)
    assert row1_m is not None and row2_m is not None and search_css_m is not None
    self.assertNotIn('id="wb-search"', row1_m.group(1))
    self.assertIn('id="wb-search"', row2_m.group(1))
    self.assertIn("flex: 1 1 110px;", search_css_m.group(1))
    self.assertIn("max-width: 165px;", search_css_m.group(1))

    parsed_lane_chips: list[dict[str, str]] = []
    parsed_type_chips: list[dict[str, str]] = []
    for btn_m in re.finditer(r'<button class="wb-chip"([^>]*)>', html_out):
      attr_map = {
          k: html_lib.unescape(v)
          for k, v in re.findall(r'([a-zA-Z0-9_-]+)="([^"]*)"', btn_m.group(1))
      }
      if "data-filter" in attr_map:
        parsed_lane_chips.append(attr_map)
      elif "data-type" in attr_map:
        parsed_type_chips.append(attr_map)

    self.assertEqual(
        [c["data-filter"] for c in parsed_lane_chips],
        ["all", "needs-you", "on-desk", "waiting", "done"],
    )
    self.assertEqual(
        {c["data-filter"]: int(c["data-count"]) for c in parsed_lane_chips},
        {"all": 6, "needs-you": 1, "on-desk": 3, "waiting": 2, "done": 1},
    )
    self.assertIn(
        '<article class="wb-card" data-work-id="w_desk_norev" data-lane="on-desk" '
        'data-collapsed="true">',
        html_out,
    )
    self.assertIn(
        '<span class="re-status-pill" data-tone="warn"><span class="re-dot"></span>On Desk</span>',
        html_out,
    )

    match = re.search(r"<script>\n(.*?)\n</script>", html_out, re.DOTALL)
    self.assertIsNotNone(match)
    assert match is not None
    script_js = match.group(1)

    node_script = (
        """
const assert = require('assert');
function makeEl(id, extra = {}) {
  const attrs = Object.assign({}, extra.attrs || {});
  const listeners = {};
  return Object.assign({
    id, value: extra.value || '', textContent: extra.textContent || '',
    innerHTML: extra.innerHTML || '', disabled: false,
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
const laneChips = """
        + json.dumps(parsed_lane_chips)
        + """.map((attrs) => makeEl('lane-' + attrs['data-filter'], { attrs }));
const typeChips = """
        + json.dumps(parsed_type_chips)
        + """.map((attrs) => makeEl('type-' + attrs['data-type'], { attrs }));
const allChips = laneChips.concat(typeChips);
global.localStorage = { getItem: () => null, setItem: () => {} };
global.window = { addEventListener: () => {}, getSelection: () => '', prompt: () => '' };
global.setInterval = () => 0;
global.fetch = async () => ({ ok: false });
global.document = {
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

"""
        + script_js
        + """

function visibleIds() {
  return [...els['wb-cards-container'].innerHTML.matchAll(/data-work-id="([^"]+)"/g)]
    .map((m) => m[1]);
}
function cardHtml(wid) {
  const parts = els['wb-cards-container'].innerHTML.split(/(?=<article class="wb-card")/);
  return parts.find((p) => p.includes('data-work-id="' + wid + '"')) || '';
}

const byFilter = Object.fromEntries(laneChips.map((c) => [c.getAttribute('data-filter'), c]));
const byType = Object.fromEntries(typeChips.map((c) => [c.getAttribute('data-type'), c]));

// Default All + My CLs shows all 5 active own CLs ordered Needs You -> On Desk (0,1,2) -> Waiting
byFilter['all'].dispatch('click');
byType['my-cls'].dispatch('click');
assert.deepStrictEqual(visibleIds(), [
  'w_needs_cl', 'w_desk_norev', 'w_desk_wip', 'w_desk_dns', 'w_wait_cl',
]);
assert.strictEqual(byFilter['needs-you'].getAttribute('data-count'), '1');
assert.strictEqual(byFilter['on-desk'].getAttribute('data-count'), '3');
assert.strictEqual(byFilter['waiting'].getAttribute('data-count'), '1');

// Clicking On Desk + All types isolates the 3 prepared CLs in sub-tier order (0 -> 1 -> 2)
// and keeps open non-draft PR + DO NOT SUBMIT peers expanded with warn stage badges
byFilter['on-desk'].dispatch('click');
byType['all'].dispatch('click');
assert.deepStrictEqual(visibleIds(), ['w_desk_norev', 'w_desk_wip', 'w_desk_dns']);
const norevHtml = cardHtml('w_desk_norev');
const dnsHtml = cardHtml('w_desk_dns');
assert(norevHtml.includes('data-tone="warn"><span class="re-dot"></span>On Desk</span>'));
assert(!norevHtml.includes('>In review<'));
assert(norevHtml.includes('>no reviewers<'));
assert(norevHtml.includes('data-peer-open="true"'));
assert(!dnsHtml.includes('>In review<'));
assert(dnsHtml.includes('data-peer-open="true"'));

// Switching to Activity sort orders purely by activity_at descending (9000 -> 6000 -> 4000)
els['wb-sort-mode'].value = 'activity';
els['wb-sort-mode'].dispatch('change', { target: els['wb-sort-mode'] });
assert.deepStrictEqual(visibleIds(), ['w_desk_dns', 'w_desk_wip', 'w_desk_norev']);

// Restore Triage sort and verify every (query, lane, type) slice matches both badge axes
els['wb-sort-mode'].value = 'triage';
els['wb-sort-mode'].dispatch('change', { target: els['wb-sort-mode'] });
for (const q of ['', 'alpha']) {
  els['wb-search'].value = q;
  els['wb-search'].dispatch('input', { target: els['wb-search'] });
  for (const lc of laneChips) {
    lc.dispatch('click');
    for (const tc of typeChips) {
      const advertisedTypeCnt = Number(tc.getAttribute('data-count'));
      tc.dispatch('click');
      assert.strictEqual(visibleIds().length, advertisedTypeCnt);
    }
  }
  for (const tc of typeChips) {
    tc.dispatch('click');
    for (const lc of laneChips) {
      const advertisedLaneCnt = Number(lc.getAttribute('data-count'));
      lc.dispatch('click');
      assert.strictEqual(visibleIds().length, advertisedLaneCnt);
    }
  }
}
"""
    )
    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, check=False
    )
    self.assertEqual(
        res.returncode,
        0,
        f"Node On Desk check failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )


if __name__ == "__main__":
  unittest.main()
