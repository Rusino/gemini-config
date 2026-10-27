#!/usr/bin/env python3
"""Unit tests for jetski_closed_filter.js and patch_jetski_hub_binary.py."""

import glob
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(CONFIG_DIR, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import patch_jetski_hub_binary as patcher  # noqa: E402


def find_node_binary() -> str:
  path_node = shutil.which("node")
  if path_node:
    return path_node
  for root in ("/var/tmp", "/tmp"):
    pattern = (
        f"{root}/sar.server.*/server_impl.runfiles/"
        "google3/third_party/jetski/cmd/hub/server/node_basic"
    )
    candidates = sorted(glob.glob(pattern))
    if candidates:
      return candidates[0]
  mac_electron = "/Applications/Antigravity.app/Contents/MacOS/Antigravity"
  if os.path.exists(mac_electron):
    return mac_electron
  raise RuntimeError("No node or node_basic binary found for JS filter tests")


class TestJetskiClosedFilter(unittest.TestCase):

  def test_no_cyrillic_in_scripts(self) -> None:
    for filename in ("jetski_closed_filter.js", "patch_jetski_hub_binary.py"):
      path = os.path.join(SCRIPTS_DIR, filename)
      with open(path, encoding="utf-8") as f:
        content = f.read()
      cyrillic = re.findall(r"[А-Яа-яЁё]+", content)
      self.assertEqual(
          cyrillic,
          [],
          f"Expected English-only strings in {filename}, found: {cyrillic}",
      )

  def test_js_filter_behavior_and_english_ui(self) -> None:
    node_bin = find_node_binary()
    js_path = os.path.join(SCRIPTS_DIR, "jetski_closed_filter.js")

    harness = r"""
const fs = require('fs');
const assert = require('assert');

const scriptPath = process.argv[1];
const code = fs.readFileSync(scriptPath, 'utf8');

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.id = '';
    this.className = '';
    this.type = '';
    this.textContent = '';
    this._innerHTML = '';
    this.style = {};
    this.attributes = new Map();
    this.children = [];
    this.parentElement = null;
    this.listeners = new Map();
  }
  get innerHTML() {
    return this._innerHTML;
  }
  set innerHTML(val) {
    this._innerHTML = String(val);
    if (global.__recordMutation) {
      global.__recordMutation(this);
    }
  }
  setAttribute(k, v) {
    this.attributes.set(k, String(v));
    if (k === 'id') this.id = String(v);
    if (k === 'class') this.className = String(v);
    if (global.__recordMutation) {
      global.__recordMutation(this);
    }
  }
  getAttribute(k) {
    if (k === 'id' && this.id) return this.id;
    return this.attributes.has(k) ? this.attributes.get(k) : null;
  }
  hasAttribute(k) {
    return this.attributes.has(k);
  }
  removeAttribute(k) {
    this.attributes.delete(k);
  }
  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    if (global.__recordMutation) {
      global.__recordMutation(this);
    }
    return child;
  }
  insertBefore(child, ref) {
    child.parentElement = this;
    const idx = this.children.indexOf(ref);
    if (idx === -1) this.children.push(child);
    else this.children.splice(idx, 0, child);
    if (global.__recordMutation) {
      global.__recordMutation(this);
    }
    return child;
  }
  remove() {
    if (!this.parentElement) return;
    const idx = this.parentElement.children.indexOf(this);
    if (idx !== -1) this.parentElement.children.splice(idx, 1);
    this.parentElement = null;
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  dispatchEvent(type, eventInit = {}) {
    let defaultPrevented = false;
    let propagationStopped = false;
    const evt = {
      type,
      button: 0,
      shiftKey: false,
      ctrlKey: false,
      metaKey: false,
      ...eventInit,
      preventDefault() { defaultPrevented = true; },
      stopPropagation() { propagationStopped = true; },
    };
    for (const fn of (this.listeners.get(type) || [])) {
      fn(evt);
    }
    return { defaultPrevented, propagationStopped };
  }
  getBoundingClientRect() {
    return { left: 24, top: 40, bottom: 62, right: 140, width: 116, height: 22 };
  }
  focus() {}
  closest(selector) {
    let cur = this;
    while (cur) {
      if (selector.startsWith('#') && cur.id === selector.slice(1)) return cur;
      if (selector.startsWith('.') && cur.className === selector.slice(1)) return cur;
      cur = cur.parentElement;
    }
    return null;
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  querySelectorAll(selector) {
    const out = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (matches(child, selector)) out.push(child);
        visit(child);
      }
    };
    visit(this);
    return out;
  }
}

function matches(el, selector) {
  if (selector.includes(',')) {
    return selector.split(',').some((part) => matches(el, part.trim()));
  }
  if (selector === 'span') return el.tagName === 'SPAN';
  if (selector === 'span.truncate') return el.tagName === 'SPAN' && el.className === 'truncate';
  if (selector === 'a[aria-label]') return el.tagName === 'A' && el.hasAttribute('aria-label');
  if (selector === 'button[aria-label="Display Options"]') {
    return el.tagName === 'BUTTON' && el.getAttribute('aria-label') === 'Display Options';
  }
  // Match sidebar project header and card selectors used by ensureProjectRoadmapBadges
  if (selector === 'button[data-testid="project-header"]') {
    return el.tagName === 'BUTTON' && el.getAttribute('data-testid') === 'project-header';
  }
  if (selector === 'button[data-project-card]') {
    return el.tagName === 'BUTTON' && el.hasAttribute('data-project-card');
  }
  const bucketMatch = /^button\[data-bucket="([^"]+)"\]$/.exec(selector);
  if (bucketMatch) {
    return el.tagName === 'BUTTON' && el.getAttribute('data-bucket') === bucketMatch[1];
  }
  if (selector === 'script[src*="jetski-closed-filter.js"]') {
    return el.tagName === 'SCRIPT' && (el.getAttribute('src') || '').includes('jetski-closed-filter.js');
  }
  if (selector === '[data-testid="titlebar-more-actions"]') {
    return el.getAttribute('data-testid') === 'titlebar-more-actions';
  }
  // Match OverviewPanelHost containers when resolving active pane conversation IDs
  if (selector === '[data-testid="overview-panel-host"]') {
    return el.getAttribute('data-testid') === 'overview-panel-host';
  }
  if (selector === '[data-testid="conversation-pin-button"]') {
    return el.getAttribute('data-testid') === 'conversation-pin-button';
  }
  if (selector === '[data-testid="conversation-archive-button"]') {
    return el.getAttribute('data-testid') === 'conversation-archive-button';
  }
  if (selector === '[data-testid="conversation-row-sidebar"][data-cascade-id]') {
    return (
      el.getAttribute('data-testid') === 'conversation-row-sidebar' &&
      el.hasAttribute('data-cascade-id')
    );
  }
  if (selector === 'div[data-sidebar-row-id]') {
    return el.tagName === 'DIV' && el.hasAttribute('data-sidebar-row-id');
  }
  if (selector === 'div[data-sidebar-section-drop-id]:not([data-sidebar-row-id])') {
    return (
      el.tagName === 'DIV' &&
      el.hasAttribute('data-sidebar-section-drop-id') &&
      !el.hasAttribute('data-sidebar-row-id')
    );
  }
  if (selector.startsWith('.')) {
    return el.className === selector.slice(1);
  }
  if (selector.startsWith('#')) {
    return el.id === selector.slice(1);
  }
  return false;
}

const docEl = new FakeElement('html');
const headEl = new FakeElement('head');
const bodyEl = new FakeElement('body');
docEl.appendChild(headEl);
docEl.appendChild(bodyEl);

const actionRow = new FakeElement('div');
const displayOptionsBtn = new FakeElement('button');
displayOptionsBtn.setAttribute('aria-label', 'Display Options');
actionRow.appendChild(displayOptionsBtn);
bodyEl.appendChild(actionRow);

const emptyProjContainer = new FakeElement('div');
emptyProjContainer.setAttribute('data-sidebar-section-drop-id', 'proj-closed-only');
const emptyProjSpan = new FakeElement('span');
emptyProjSpan.textContent = 'No conversations yet';
emptyProjContainer.appendChild(emptyProjSpan);
bodyEl.appendChild(emptyProjContainer);

// Project header rows for proj-1 (3 open of 8) and proj-closed-only (0 open of 10)
const proj1HeaderSection = new FakeElement('div');
proj1HeaderSection.setAttribute('data-sidebar-section-drop-id', 'proj-1');
const proj1HeaderBtn = new FakeElement('button');
proj1HeaderBtn.setAttribute('data-testid', 'project-header');
proj1HeaderBtn.setAttribute('data-project-card', '');
const proj1TitleWrap = new FakeElement('div');
const proj1TitleSpan = new FakeElement('span');
proj1TitleSpan.className = 'truncate';
proj1TitleSpan.textContent = 'Breaking Chats';
proj1TitleWrap.appendChild(proj1TitleSpan);
proj1HeaderBtn.appendChild(proj1TitleWrap);
proj1HeaderSection.appendChild(proj1HeaderBtn);
bodyEl.appendChild(proj1HeaderSection);

const projClosedHeaderSection = new FakeElement('div');
projClosedHeaderSection.setAttribute('data-sidebar-section-drop-id', 'proj-closed-only');
const projClosedHeaderBtn = new FakeElement('button');
projClosedHeaderBtn.setAttribute('data-testid', 'project-header');
const projClosedTitleSpan = new FakeElement('span');
projClosedTitleSpan.className = 'truncate';
projClosedTitleSpan.textContent = 'Finished Project';
projClosedHeaderBtn.appendChild(projClosedTitleSpan);
projClosedHeaderSection.appendChild(projClosedHeaderBtn);
bodyEl.appendChild(projClosedHeaderSection);

// Titlebar with more-actions button for the visible tail of a chain ('open-current')
const titlebarToolbar = new FakeElement('div');
const moreActionsBtn = new FakeElement('button');
moreActionsBtn.setAttribute('data-testid', 'titlebar-more-actions');
moreActionsBtn.__reactFiber$titlebar = {
  memoizedProps: {},
  return: {
    memoizedProps: { cascadeId: 'open-current' },
  },
};
titlebarToolbar.appendChild(moreActionsBtn);
bodyEl.appendChild(titlebarToolbar);

// Sidebar row with hover action bar for 'solo-active'
const sidebarRow = new FakeElement('div');
sidebarRow.setAttribute('data-testid', 'conversation-row-sidebar');
sidebarRow.setAttribute('data-cascade-id', 'solo-active');
const rowHoverBar = new FakeElement('div');
const pinBtn = new FakeElement('button');
pinBtn.setAttribute('data-testid', 'conversation-pin-button');
rowHoverBar.appendChild(pinBtn);
sidebarRow.appendChild(rowHoverBar);
bodyEl.appendChild(sidebarRow);

function makeSummary(title, projectId, extraMeta = {}, archived = false, extraSummary = {}) {
  return {
    summary: title,
    annotations: { archived },
    trajectoryMetadata: {
      projectId,
      ...extraMeta,
    },
    ...extraSummary,
  };
}

// Finished chats and handed-off chain pieces are archived; visible chats carry ⦿ or ⏳ only
const initialSummaries = {
  'solo-active': makeSummary('[10:00] ⦿ Active solo task', 'proj-1'),
  'agent-working': makeSummary('[10:02] ⦿ Busy agent task', 'proj-1', {}, false, {
    status: 'CASCADE_RUN_STATUS_RUNNING',
  }),
  'cron-waiting': makeSummary('[10:04] ⏳ Flight Deck cron', 'proj-1', {}, false, {
    status: 'CASCADE_RUN_STATUS_IDLE',
    notFullyIdle: true,
  }),
  'open-root': makeSummary('[09:00] ⦿ Open chain topic', 'proj-1', {}, true),
  'open-step': makeSummary('[09:30] ⦿ Open chain topic', 'proj-1', {}, true),
  'open-current': makeSummary('[10:05] ⦿ Open chain topic', 'proj-1'),
  'closed-solo': makeSummary('[08:00] ⦿ Finished bugfix', 'proj-1', {}, true),
  'quoted-title': makeSummary('«Quoted» topic without lifecycle prefix', 'proj-1'),
  'closed-root': makeSummary('[10:15] ⦿ Closed chain topic', 'proj-closed-only', {}, true),
  'closed-tail': makeSummary('[11:00] ⏳ Closed chain topic', 'proj-closed-only', {}, true),
  'ext-waiting': makeSummary('[11:30] ⏳ Waiting on CQ', 'proj-closed-only'),
  'native-hidden': makeSummary('[12:00] ⦿ Background subagent', 'proj-1', {
    sourceMetadata: { hideFromConversationList: true },
  }),
};

const storeState = {
  trajectorySummaries: {
    summaries: { ...initialSummaries },
  },
  // Seed an open Overview Panel entry to verify syncOverviewPanelClosed flips it to false
  auxPaneV2: {
    overviewPanelOpen: {
      'open-root': true,
    },
  },
};

const subscribers = [];
const reduxStore = {
  getState() {
    return storeState;
  },
  dispatch(action) {
    // Mirror auxPaneV2Slice.setOverviewPanelOpen reducer in the test harness
    if (action?.type === 'auxPaneV2/setOverviewPanelOpen') {
      const { cascadeId, open } = action.payload || {};
      // Record per-conversation open state when cascadeId is present
      if (cascadeId) {
        storeState.auxPaneV2.overviewPanelOpen = {
          ...(storeState.auxPaneV2.overviewPanelOpen || {}),
          [cascadeId]: open,
        };
      }
      return action;
    }
    // Mirror auxPaneV2Slice.restorePersistedPanes reducer in the test harness
    if (action?.type === 'auxPaneV2/restorePersistedPanes') {
      // Merge persisted overviewPanelOpen map under existing in-memory choices
      if (action.payload?.overviewPanelOpen) {
        storeState.auxPaneV2.overviewPanelOpen = {
          ...(action.payload.overviewPanelOpen || {}),
          ...(storeState.auxPaneV2.overviewPanelOpen || {}),
        };
      }
      return action;
    }
    if (action?.type === 'trajectorySummaries/applyStreamBatch') {
      const next = { ...storeState.trajectorySummaries.summaries };
      for (const [id, s] of (action.payload?.updates || [])) {
        next[id] = s;
      }
      storeState.trajectorySummaries = { summaries: next };
      for (const sub of subscribers) sub();
    }
    return action;
  },
  subscribe(fn) {
    subscribers.push(fn);
    return () => {};
  },
};

displayOptionsBtn.__reactFiber$test = {
  memoizedProps: { store: reduxStore },
};

const storage = new Map();
let observerCallback = null;
let rafCallbacks = [];
const rpcRequests = [];

global.window = {
  innerWidth: 1280,
  __APP_CONFIG__: { csrfToken: 'test-csrf-token' },
  localStorage: {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
  },
  fetch: async (url, opts) => {
    if (String(url).includes('GetCascadeTrajectorySteps')) {
      const body = JSON.parse(opts.body);
      rpcRequests.push({ url: String(url), body, headers: opts.headers });
      const stepsByCid = {
        'open-root': [
          {
            type: 'CORTEX_STEP_TYPE_USER_INPUT',
            userInput: { userResponse: 'Find the <b>probe</b> word' },
          },
          { type: 'CORTEX_STEP_TYPE_RUN_COMMAND', runCommand: { output: 'probe in tool output' } },
        ],
        'open-step': [
          {
            type: 'CORTEX_STEP_TYPE_PLANNER_RESPONSE',
            plannerResponse: {
              thinking: 'probe in thinking',
              response: 'raw',
              modifiedResponse: 'Intro\n\nA PROBE line\n\nOutro',
            },
          },
        ],
        'open-current': [],
      };
      return { ok: true, json: async () => ({ steps: stepsByCid[body.cascadeId] || [] }) };
    }
    if (String(url).includes('jetski-chains.json')) {
      return {
        ok: true,
        json: async () => ({
          parentOf: {
            'open-step': 'open-root',
            'open-current': 'open-step',
          },
          createdAt: {
            'open-root': '2026-10-21T09:00:00Z',
            'open-step': '2026-10-21T09:30:00Z',
            'open-current': '2026-10-21T10:05:00Z',
          },
        }),
      };
    }
    // Stub jetski-roadmaps.json with both an open-roadmap project and a zero-open project
    if (String(url).includes('jetski-roadmaps.json')) {
      return {
        ok: true,
        json: async () => ({
          version: 1,
          projects: {
            'proj-1': {
              doing: 1,
              todo: 2,
              done: 5,
              dropped: 1,
              open: 3,
              total: 8,
              openItems: [
                {
                  id: 'R6',
                  status: 'doing',
                  text: 'Fix <tag> & badge',
                  depth: 0,
                  owner: 'a26d9ce2',
                },
                { id: 'R7', status: 'todo', text: 'Next task item', depth: 0, owner: '' },
                { id: 'R7.1', status: 'todo', text: 'Subtask item', depth: 1, owner: '' },
              ],
            },
            'proj-closed-only': {
              doing: 0,
              todo: 0,
              done: 10,
              dropped: 0,
              open: 0,
              total: 10,
              openItems: [],
            },
          },
        }),
      };
    }
    if (String(url).includes('UpdateConversationAnnotations')) {
      rpcRequests.push({
        url,
        headers: opts?.headers || {},
        body: JSON.parse(opts?.body || '{}'),
      });
      return { ok: true, status: 200 };
    }
    return { ok: false, status: 404 };
  },
};
global.document = {
  documentElement: docEl,
  head: headEl,
  body: bodyEl,
  getElementById(id) {
    return docEl.querySelector(`#${id}`);
  },
  createElement(tag) {
    return new FakeElement(tag);
  },
  querySelector(sel) {
    return docEl.querySelector(sel);
  },
  querySelectorAll(sel) {
    return docEl.querySelectorAll(sel);
  },
};
global.MutationObserver = class {
  constructor(cb) {
    observerCallback = cb;
  }
  observe() {}
  disconnect() {}
};
global.requestAnimationFrame = (cb) => {
  rafCallbacks.push(cb);
  return rafCallbacks.length;
};

eval(code);

const filterBar = bodyEl.querySelector('.jetski-attention-filter-bar');
assert(filterBar, '3-button filter bar must be injected below the sidebar header row');
assert.strictEqual(bodyEl.children[1], filterBar, 'Filter bar must sit directly below actionRow');
const bucketBtns = filterBar.querySelectorAll('.jetski-closed-chats-filter-btn');
assert.strictEqual(bucketBtns.length, 3, 'Filter bar must render 3 state buttons');

const btnNeedsMe = filterBar.querySelector('button[data-bucket="needs-me"]');
const btnWorking = filterBar.querySelector('button[data-bucket="working"]');
const btnWaiting = filterBar.querySelector('button[data-bucket="waiting"]');
assert.deepStrictEqual(
  bucketBtns.map((b) => b.getAttribute('data-bucket')),
  ['needs-me', 'working', 'waiting']
);

const isHidden = (id) => Boolean(
  storeState.trajectorySummaries.summaries[id].trajectoryMetadata?.sourceMetadata?.hideFromConversationList
);
const assertHidden = (ids, expected) => {
  for (const id of ids) assert.strictEqual(isHidden(id), expected, id);
};
const ARCHIVED = ['open-root', 'open-step', 'closed-solo', 'closed-root', 'closed-tail'];
// Archived chats are hidden by the native sidebar; the filter must neither count nor touch them
const assertArchivedUntouched = () => {
  for (const id of ARCHIVED) {
    const s = storeState.trajectorySummaries.summaries[id];
    assert.strictEqual(s.annotations.archived, true, id);
    assert.strictEqual(s.trajectoryMetadata.sourceMetadata, undefined, id);
  }
};

// Default visible buckets: ['needs-me', 'working']
// Counts across 6 eligible chats; the 5 archived chats are never counted
// - needs-me (3): solo-active, open-current, quoted-title
// - working (1): agent-working
// - waiting (2): cron-waiting (⏳ with status IDLE & notFullyIdle=true stays in waiting!),
//   ext-waiting
assert.strictEqual(btnNeedsMe.getAttribute('aria-label'), '🚨 Needs Me: 3 (shown)');
assert.strictEqual(btnNeedsMe.getAttribute('data-active'), 'true');
assert.strictEqual(btnWorking.getAttribute('aria-label'), '🤖 Working: 1 (shown)');
assert.strictEqual(btnWorking.getAttribute('data-active'), 'true');
assert.strictEqual(btnWaiting.getAttribute('aria-label'), '⏳ Waiting: 2 (hidden)');
assert.strictEqual(btnWaiting.getAttribute('data-active'), 'false');
assert.strictEqual(emptyProjSpan.textContent, 'No matching chats (1 hidden)');

assertHidden(['cron-waiting', 'ext-waiting'], true);
assertHidden(['solo-active', 'agent-working', 'open-current', 'quoted-title'], false);
assertArchivedUntouched();

// Overview Panel stays closed in Redux for active chats and resists open/restore actions
assert.strictEqual(storeState.auxPaneV2.overviewPanelOpen['open-root'], false);
assert.strictEqual(storeState.auxPaneV2.overviewPanelOpen['open-current'], false);
reduxStore.dispatch({
  type: 'auxPaneV2/setOverviewPanelOpen',
  payload: { cascadeId: 'solo-active', open: true },
});
assert.strictEqual(storeState.auxPaneV2.overviewPanelOpen['solo-active'], false);
reduxStore.dispatch({
  type: 'auxPaneV2/restorePersistedPanes',
  payload: { overviewPanelOpen: { 'restored-chat': true } },
});
assert.strictEqual(storeState.auxPaneV2.overviewPanelOpen['restored-chat'], false);
const injectedStyle = global.document.getElementById('jetski-closed-chats-filter-style');
assert(injectedStyle.textContent.includes('[data-testid="overview-panel-host-gutter"]'));
assert(injectedStyle.textContent.includes('--overview-panel-reserve: 0px !important;'));

// Hovering each button displays a dedicated tooltip explaining that button's state
btnWaiting.dispatchEvent('mouseenter');
const bubble = global.document.getElementById('jetski-closed-chats-filter-bubble');
assert(bubble, 'Hover bubble must be created');
assert.strictEqual(bubble.getAttribute('data-visible'), 'true');
assert(bubble.innerHTML.includes('⏳ Waiting (Cron / External)'));
assert(bubble.innerHTML.includes('[HIDDEN · 2]'));
assert(bubble.innerHTML.includes('Background cron/timer chats or chats waiting on external events'));
assert(bubble.innerHTML.includes('All 3 chat states'));
assert(bubble.innerHTML.includes('or all 3 if solo'));
assert(bubble.innerHTML.includes('4 of 6 (2 hidden)'));
assert(!/[А-Яа-яЁё]/.test(bubble.innerHTML), 'Bubble HTML must not contain Cyrillic');

btnNeedsMe.dispatchEvent('mouseenter');
assert(bubble.innerHTML.includes('🚨 Needs Me (⦿)'));
assert(bubble.innerHTML.includes('[SHOWN · 3]'));

// Verify MutationObserver ignores mutations inside bubble and filter bar
rafCallbacks = [];
observerCallback([{ target: bubble }, { target: btnWaiting }, { target: filterBar }]);
assert.strictEqual(rafCallbacks.length, 0, 'Bubble/button mutations must not schedule sync');

// Left-click on ⏳ Waiting button toggles it ON independently (combining 🚨 + 🤖 + ⏳)
const pdRes = btnWaiting.dispatchEvent('pointerdown', { button: 0 });
assert.strictEqual(pdRes.defaultPrevented, true);
assert.strictEqual(pdRes.propagationStopped, true);
btnWaiting.dispatchEvent('mousedown', { button: 0 });
btnWaiting.dispatchEvent('click', { button: 0 });

assert.strictEqual(btnWaiting.getAttribute('data-active'), 'true');
assert.strictEqual(btnWaiting.getAttribute('aria-label'), '⏳ Waiting: 2 (shown)');
assertHidden(['cron-waiting', 'ext-waiting'], false);
assertHidden(['solo-active', 'agent-working', 'open-current', 'quoted-title'], false);
assert.strictEqual(emptyProjSpan.textContent, 'No conversations yet');
assert(bubble.innerHTML.includes('6 of 6 (0 hidden)'));
assertHidden(['native-hidden'], true);
assertArchivedUntouched();

// Right-click (contextmenu) on ⏳ Waiting solos ONLY the waiting bucket
btnWaiting.dispatchEvent('contextmenu', { button: 2 });
assert.strictEqual(btnWaiting.getAttribute('data-active'), 'true');
assert.strictEqual(btnNeedsMe.getAttribute('data-active'), 'false');
assert.strictEqual(btnWorking.getAttribute('data-active'), 'false');
assertHidden(['cron-waiting', 'ext-waiting'], false);
assertHidden(['solo-active', 'agent-working', 'open-current', 'quoted-title'], true);
assertArchivedUntouched();

// Right-click again on already-soloed ⏳ Waiting turns all 3 buckets ON
btnWaiting.dispatchEvent('contextmenu', { button: 2 });
for (const b of [btnNeedsMe, btnWorking, btnWaiting]) {
  assert.strictEqual(b.getAttribute('data-active'), 'true');
}
assertHidden(['solo-active', 'agent-working', 'open-current', 'quoted-title'], false);

// Turn ⏳ Waiting back OFF via left-click (after advancing Date.now past 300ms debounce)
const origNowSync = Date.now;
Date.now = () => origNowSync() + 500;
btnWaiting.dispatchEvent('pointerdown', { button: 0 });
Date.now = origNowSync;
assert.strictEqual(btnWaiting.getAttribute('data-active'), 'false');
assertHidden(['cron-waiting', 'ext-waiting'], true);

// Incoming stream batch while ⏳ Waiting is OFF hides the new ⏳ chat; an archived one is
// neither counted nor flagged
reduxStore.dispatch({
  type: 'trajectorySummaries/applyStreamBatch',
  payload: {
    updates: [
      ['streamed-waiting', makeSummary('[12:30] ⏳ Newly waiting chat', 'proj-1')],
      ['streamed-archived', makeSummary('[12:31] ⦿ Newly archived chat', 'proj-1', {}, true)],
    ],
    deletes: [],
  },
});
assertHidden(['streamed-waiting'], true);
assertHidden(['streamed-archived'], false);
while (rafCallbacks.length > 0) rafCallbacks.shift()();
assert.strictEqual(btnWaiting.getAttribute('aria-label'), '⏳ Waiting: 3 (hidden)');
assert.strictEqual(btnNeedsMe.getAttribute('aria-label'), '🚨 Needs Me: 3 (shown)');

// Re-evaluating the script cleanly replaces the bar via __jetskiClosedChatsFilterCleanup;
// bucket sets saved before 'steps' and 'closed' were retired load without them
const reloadWith = (saved) => {
  storage.set('jetski_visible_buckets_v1', JSON.stringify(saved));
  eval(code);
  const bars = bodyEl.querySelectorAll('.jetski-attention-filter-bar');
  assert.strictEqual(bars.length, 1, 'Reloading script must not duplicate filter bar');
  return bars[0].querySelectorAll('.jetski-closed-chats-filter-btn')
    .map((b) => [b.getAttribute('data-bucket'), b.getAttribute('data-active')]);
};
assert.deepStrictEqual(
  reloadWith(['steps', 'closed', 'needs-me']),
  [['needs-me', 'true'], ['working', 'false'], ['waiting', 'false']]
);
// A set of retired buckets alone falls back to the defaults instead of an empty sidebar
assert.deepStrictEqual(
  reloadWith(['closed']),
  [['needs-me', 'true'], ['working', 'true'], ['waiting', 'false']]
);
assert.deepStrictEqual(
  reloadWith([]),
  [['needs-me', 'false'], ['working', 'false'], ['waiting', 'false']]
);
assert.deepStrictEqual(
  reloadWith(['steps', 'needs-me', 'working']),
  [['needs-me', 'true'], ['working', 'true'], ['waiting', 'false']]
);
assert.strictEqual(
  storage.get('jetski_visible_buckets_v1'),
  JSON.stringify(['steps', 'needs-me', 'working']),
  'Loading must not rewrite storage before the user toggles a bucket'
);
// Buttons captured above belong to the bar removed by the reload cleanup
const liveBucketBtn = (k) =>
  bodyEl.querySelector('.jetski-attention-filter-bar').querySelector(`button[data-bucket="${k}"]`);

(async () => {
  const titlebarWaitBtn = titlebarToolbar.querySelector('.jetski-titlebar-wait-btn');
  const titlebarBtn = titlebarToolbar.querySelector('.jetski-titlebar-archive-btn');
  assert(titlebarWaitBtn, 'Titlebar Wait button must be injected after Search, before Archive');
  assert(titlebarBtn, 'Titlebar Archive button must be injected before titlebar-more-actions');
  assert.strictEqual(titlebarToolbar.children[1], titlebarWaitBtn);
  assert.strictEqual(titlebarToolbar.children[2], titlebarBtn);
  assert.strictEqual(titlebarBtn.getAttribute('data-testid'), 'jetski-titlebar-archive-btn');
  assert.strictEqual(titlebarBtn.getAttribute('aria-label'), 'Archive chat');
  assert.strictEqual(titlebarBtn.getAttribute('data-archived'), 'false');
  assert.strictEqual(titlebarWaitBtn.style.display, '');

  const rowWaitBtn = rowHoverBar.querySelector('.jetski-row-wait-btn');
  const rowBtn = rowHoverBar.querySelector('.jetski-row-archive-btn');
  assert(rowWaitBtn, 'Sidebar row Wait button must be injected before Archive button');
  assert(rowBtn, 'Sidebar row Archive button must be injected before pin button');
  assert.strictEqual(rowHoverBar.children[0], rowWaitBtn);
  assert.strictEqual(rowHoverBar.children[1], rowBtn);
  assert.strictEqual(rowBtn.getAttribute('data-testid'), 'jetski-row-archive-btn');
  assert.strictEqual(rowBtn.getAttribute('aria-label'), 'Archive chat');
  assert.strictEqual(rowBtn.getAttribute('data-archived'), 'false');
  assert.strictEqual(rowWaitBtn.style.display, '');

  // Mutations inside injected action buttons must not schedule a sync loop
  rafCallbacks = [];
  observerCallback([{ target: titlebarBtn }, { target: rowBtn }]);
  assert.strictEqual(rafCallbacks.length, 0);

  const advanceClock = () => {
    const prev = Date.now;
    const shifted = prev() + 500;
    Date.now = () => shifted;
  };
  const summaryOf = (id) => storeState.trajectorySummaries.summaries[id];

  // Clicking the sidebar row Wait button on 'solo-active' swaps its marker between ⦿ and ⏳
  rpcRequests.length = 0;
  rowWaitBtn.dispatchEvent('pointerdown', { button: 0 });
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(summaryOf('solo-active').summary, '[10:00] ⏳ Active solo task');
  assert.strictEqual(rowWaitBtn.getAttribute('data-waiting'), 'true');
  assert.strictEqual(rpcRequests.length, 1);
  assert.deepStrictEqual(rpcRequests[0].body, {
    cascadeIds: ['solo-active'],
    annotations: { title: '[10:00] ⏳ Active solo task' },
    mergeAnnotations: true,
  });

  // Clicking the sidebar row Archive button archives exactly that chat, keeping its title
  rpcRequests.length = 0;
  rowBtn.dispatchEvent('pointerdown', { button: 0 });
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(summaryOf('solo-active').annotations.archived, true);
  assert.strictEqual(summaryOf('solo-active').summary, '[10:00] ⏳ Active solo task');
  assert.strictEqual(rpcRequests.length, 1);
  assert.strictEqual(
    rpcRequests[0].url,
    '/exa.language_server_pb.LanguageServerService/UpdateConversationAnnotations'
  );
  assert.deepStrictEqual(rpcRequests[0].body, {
    cascadeIds: ['solo-active'],
    annotations: { archived: true },
    mergeAnnotations: true,
  });
  assert.strictEqual(rpcRequests[0].headers['x-codeium-csrf-token'], 'test-csrf-token');
  assert.strictEqual(rowBtn.getAttribute('aria-label'), 'Unarchive chat');
  assert.strictEqual(rowBtn.getAttribute('data-archived'), 'true');
  assert.strictEqual(rowWaitBtn.style.display, 'none');
  assert.strictEqual(liveBucketBtn('waiting').getAttribute('aria-label'), '⏳ Waiting: 3 (hidden)');
  assertHidden(['solo-active'], false);

  // Unarchive sends archived:false and brings the chat back into its bucket
  rpcRequests.length = 0;
  advanceClock();
  rowBtn.dispatchEvent('pointerdown', { button: 0 });
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(summaryOf('solo-active').annotations.archived, false);
  assert.strictEqual(rpcRequests.length, 1);
  assert.deepStrictEqual(rpcRequests[0].body, {
    cascadeIds: ['solo-active'],
    annotations: { archived: false },
    mergeAnnotations: true,
  });
  assert.strictEqual(rpcRequests[0].headers['x-codeium-csrf-token'], 'test-csrf-token');
  assert.strictEqual(rowBtn.getAttribute('aria-label'), 'Archive chat');
  assert.strictEqual(rowBtn.getAttribute('data-archived'), 'false');
  assert.strictEqual(rowWaitBtn.style.display, '');
  assert.strictEqual(liveBucketBtn('waiting').getAttribute('aria-label'), '⏳ Waiting: 4 (hidden)');
  assertHidden(['solo-active'], true);

  // The titlebar Archive button on a chain tail archives only that chat: no chain walk, no
  // retitling of the already archived pieces
  rpcRequests.length = 0;
  advanceClock();
  titlebarBtn.dispatchEvent('pointerdown', { button: 0 });
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(rpcRequests.length, 1);
  assert.deepStrictEqual(rpcRequests[0].body, {
    cascadeIds: ['open-current'],
    annotations: { archived: true },
    mergeAnnotations: true,
  });
  assert.strictEqual(summaryOf('open-current').annotations.archived, true);
  assert.strictEqual(summaryOf('open-current').summary, '[10:05] ⦿ Open chain topic');
  assert.strictEqual(summaryOf('open-root').summary, '[09:00] ⦿ Open chain topic');
  assert.strictEqual(summaryOf('open-step').summary, '[09:30] ⦿ Open chain topic');
  assert.strictEqual(titlebarBtn.getAttribute('aria-label'), 'Unarchive chat');
  assert.strictEqual(titlebarBtn.getAttribute('data-archived'), 'true');
  assert.strictEqual(titlebarWaitBtn.style.display, 'none');
  assert.strictEqual(liveBucketBtn('needs-me').getAttribute('aria-label'), '🚨 Needs Me: 1 (shown)');

  rpcRequests.length = 0;
  advanceClock();
  titlebarBtn.dispatchEvent('pointerdown', { button: 0 });
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(rpcRequests.length, 1);
  assert.deepStrictEqual(rpcRequests[0].body, {
    cascadeIds: ['open-current'],
    annotations: { archived: false },
    mergeAnnotations: true,
  });
  assert.strictEqual(summaryOf('open-current').annotations.archived, false);
  assert.strictEqual(titlebarBtn.getAttribute('aria-label'), 'Archive chat');
  assert.strictEqual(titlebarWaitBtn.style.display, '');
  assert.strictEqual(liveBucketBtn('needs-me').getAttribute('aria-label'), '🚨 Needs Me: 2 (shown)');

  // Verify live ⏳ row indicator when an open ⦿ chat waits on a background task or subagent
  const rowTitleSpan = new FakeElement('span');
  rowTitleSpan.className = 'truncate';
  rowTitleSpan.textContent = '[18:51] ⦿ cl/1405639 Issue #195102 fontconfig';
  sidebarRow.insertBefore(rowTitleSpan, rowHoverBar);

  reduxStore.dispatch({
    type: 'trajectorySummaries/applyStreamBatch',
    payload: {
      updates: [
        [
          'solo-active',
          makeSummary('[18:51] ⦿ cl/1405639 Issue #195102 fontconfig', 'proj-1', {}, false, {
            status: 'CASCADE_RUN_STATUS_IDLE',
            notFullyIdle: true,
          }),
        ],
      ],
      deletes: [],
    },
  });
  while (rafCallbacks.length > 0) rafCallbacks.shift()();

  assert.strictEqual(sidebarRow.getAttribute('data-jetski-bg-waiting'), 'true');
  assert.strictEqual(rowTitleSpan.textContent, '[18:51] ⏳ cl/1405639 Issue #195102 fontconfig');
  assert.strictEqual(
    rowTitleSpan.getAttribute('data-jetski-orig-title'),
    '[18:51] ⦿ cl/1405639 Issue #195102 fontconfig'
  );
  assert.strictEqual(
    storeState.trajectorySummaries.summaries['solo-active'].summary,
    '[18:51] ⦿ cl/1405639 Issue #195102 fontconfig'
  );
  const bgWaitBadge = sidebarRow.querySelector('.jetski-row-bg-wait-indicator');
  assert(bgWaitBadge, 'Right-side ⏳ badge must be rendered while waiting on background work');
  assert.strictEqual(bgWaitBadge.textContent, '⏳');

  // Once background work finishes (notFullyIdle: false), restore ⦿ and remove the ⏳ badge
  reduxStore.dispatch({
    type: 'trajectorySummaries/applyStreamBatch',
    payload: {
      updates: [
        [
          'solo-active',
          makeSummary('[18:51] ⦿ cl/1405639 Issue #195102 fontconfig', 'proj-1', {}, false, {
            status: 'CASCADE_RUN_STATUS_IDLE',
            notFullyIdle: false,
          }),
        ],
      ],
      deletes: [],
    },
  });
  while (rafCallbacks.length > 0) rafCallbacks.shift()();

  assert.strictEqual(sidebarRow.hasAttribute('data-jetski-bg-waiting'), false);
  assert.strictEqual(rowTitleSpan.textContent, '[18:51] ⦿ cl/1405639 Issue #195102 fontconfig');
  assert.strictEqual(rowTitleSpan.hasAttribute('data-jetski-orig-title'), false);
  assert.strictEqual(sidebarRow.querySelector('.jetski-row-bg-wait-indicator'), null);

  // Verify project header roadmap badge for proj-1 (▸1 ☐2) and hidden badge for 0 open
  const proj1Badge = proj1HeaderBtn.querySelector('.jetski-project-roadmap-badge');
  assert(proj1Badge, 'Project with open roadmap items must render a roadmap badge');
  assert.strictEqual(proj1Badge.textContent, '▸1 ☐2');
  assert.strictEqual(proj1Badge.getAttribute('data-has-doing'), 'true');
  assert.strictEqual(proj1Badge.getAttribute('data-doing'), '1');
  assert.strictEqual(proj1Badge.getAttribute('data-todo'), '2');
  assert.strictEqual(proj1Badge.getAttribute('data-open'), '3');
  assert.strictEqual(proj1Badge.getAttribute('data-total'), '8');
  const expectedSummary = '3 open of 8 (5 done · 1 doing · 2 todo · 1 dropped)';
  assert(
    proj1Badge.getAttribute('title').includes(expectedSummary),
    'Badge title must include open/total summary and breakdown'
  );
  assert(
    proj1Badge.getAttribute('title').includes('▸ R6 Fix <tag> & badge [a26d9ce2]'),
    'Badge title must list open doing items with owner'
  );
  assert.strictEqual(
    projClosedHeaderBtn.querySelector('.jetski-project-roadmap-badge'),
    null,
    'Project with 0 open roadmap items must not render a badge'
  );

  proj1Badge.dispatchEvent('mouseenter');
  const roadmapBubble = document.getElementById('jetski-closed-chats-filter-bubble');
  assert.strictEqual(roadmapBubble.getAttribute('data-visible'), 'true');
  assert(roadmapBubble.innerHTML.includes('[3 open of 8]'));
  assert(
    roadmapBubble.innerHTML.includes('<div class="jcfb-section-title">Open roadmap items</div>'),
    'Section title must not repeat leaf open count above hierarchical openItems list'
  );
  assert(roadmapBubble.innerHTML.includes('Fix &lt;tag&gt; &amp; badge [a26d9ce2]'));
  assert(roadmapBubble.innerHTML.includes('R7.1'));
  proj1Badge.dispatchEvent('mouseleave');
  assert.strictEqual(roadmapBubble.getAttribute('data-visible'), 'false');

  const titlebarSearchBtn = titlebarToolbar.querySelector('.jetski-chat-search-btn');
  assert(titlebarSearchBtn, 'Titlebar must carry the chain search button');
  assert(!bodyEl.querySelector('.jetski-attention-filter-bar .jetski-chat-search-btn'));

  assert.deepStrictEqual(
    await window.__jetskiChatSearch.chainOf('open-step'),
    ['open-root', 'open-step', 'open-current']
  );
  rpcRequests.length = 0;
  const resultsEl = new FakeElement('div');
  const stepCache = new Map();
  const chain = ['open-root', 'open-step', 'open-current'];
  await window.__jetskiChatSearch.runChatSearch('probe', resultsEl, chain, stepCache);
  assert.deepStrictEqual(
    rpcRequests.map((r) => r.body.cascadeId),
    ['open-current', 'open-step', 'open-root']
  );
  assert.strictEqual(rpcRequests[0].headers['x-codeium-csrf-token'], 'test-csrf-token');
  const html = resultsEl.innerHTML;
  assert(html.indexOf('piece 2/3') < html.indexOf('piece 1/3'), 'Newest piece goes first');
  assert(!html.includes('piece 3/3'), 'Pieces without hits are skipped');
  assert(html.includes('A <mark>PROBE</mark> line') && !html.includes('Intro'), 'Paragraph only');
  assert(html.includes('Find the &lt;b&gt;<mark>probe</mark>'), 'Text must be escaped');
  assert(!html.includes('tool output') && !html.includes('thinking'), 'Only messages are searched');
  rpcRequests.length = 0;
  await window.__jetskiChatSearch.runChatSearch('line', resultsEl, chain, stepCache);
  assert.strictEqual(rpcRequests.length, 0, 'Steps are cached for the open panel');
  await window.__jetskiChatSearch.runChatSearch('  ', resultsEl, chain, stepCache);
  assert.strictEqual(resultsEl.innerHTML, '');

  console.log('ALL_JS_ASSERTIONS_PASSED');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    env = os.environ.copy()
    env["ELECTRON_RUN_AS_NODE"] = "1"
    res = subprocess.run(
        [node_bin, "-e", harness, js_path],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(
        res.returncode,
        0,
        f"JS harness failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )
    self.assertIn("ALL_JS_ASSERTIONS_PASSED", res.stdout)

  def test_patch_binary_and_idempotency(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
      bin_path = os.path.join(tmpdir, "jetski-hub-server")
      shim_content = (
          patcher.SHIM_PREFIX
          + (b" " * 660)
          + patcher.REPLACEMENT_SUFFIX
      )
      shim_len = len(shim_content)

      prefix = b"\x00" * 64
      shim_offset = len(prefix)

      # Construct code section after shim:
      # test %cl, %cl (84 c9); je +0x1d (74 1d); mov %rbx, %rcx (48 89 d9);
      # lea disp32(%rip), %rdi (48 8d 3d <disp32>); mov $shim_len, %esi (be <u32>)
      code_prefix = prefix + shim_content + (b"\x90" * 32)
      je_code = b"\x84\xc9\x74\x1d\x48\x89\xd9"
      lea_pos = len(code_prefix) + len(je_code)
      rip_after_lea = lea_pos + 7
      disp32 = shim_offset - rip_after_lea
      lea_instr = b"\x48\x8d\x3d" + struct.pack("<i", disp32)
      mov_esi = b"\xbe" + struct.pack("<I", shim_len)

      synthetic_bin = code_prefix + je_code + lea_instr + mov_esi + (b"\xc3" * 16)
      with open(bin_path, "wb") as f:
        f.write(synthetic_bin)

      # First patch
      self.assertTrue(patcher.patch_binary(bin_path))
      with open(bin_path, "rb") as f:
        patched_data = f.read()

      self.assertEqual(len(patched_data), len(synthetic_bin))
      self.assertIn(patcher.PATCH_MARKER, patched_data)
      self.assertIn(b"?v=\"+Date.now()+\"&csrf=", patched_data)
      # Conditional jump `74 1d` replaced with `90 90`
      je_pos = len(code_prefix) + 2
      self.assertEqual(patched_data[je_pos : je_pos + 2], b"\x90\x90")

      # Second patch (idempotent)
      self.assertTrue(patcher.patch_binary(bin_path))
      with open(bin_path, "rb") as f:
        patched_again = f.read()
      self.assertEqual(patched_data, patched_again)

  def test_extract_sar_supports_var_tmp(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
      var_tmp_root = os.path.join(tmpdir, "var", "tmp")
      fake_sar_dir = os.path.join(var_tmp_root, "sar.server.123.abc")
      fake_bin = os.path.join(fake_sar_dir, patcher.SAR_BIN_RELPATH)
      os.makedirs(os.path.dirname(fake_bin), exist_ok=True)
      with open(fake_bin, "wb") as f:
        f.write(b"ELF")

      launcher = os.path.join(tmpdir, "fake_launcher.sh")
      with open(launcher, "w", encoding="utf-8") as f:
        f.write(
            "#!/bin/sh\n"
            f'echo "Extraction directory [random_tmpdir=0]: {fake_sar_dir}."\n'
        )
      os.chmod(launcher, 0o755)

      orig_launcher = patcher.SAR_LAUNCHER
      orig_roots = patcher.SAR_SEARCH_ROOTS
      orig_env = os.environ.get("SAR_EXTRACT_DIR")
      try:
        patcher.SAR_LAUNCHER = launcher
        patcher.SAR_SEARCH_ROOTS = (var_tmp_root,)
        os.environ.pop("SAR_EXTRACT_DIR", None)
        found = patcher.extract_sar_and_find_binaries()
        self.assertEqual(found, [fake_bin])
      finally:
        patcher.SAR_LAUNCHER = orig_launcher
        patcher.SAR_SEARCH_ROOTS = orig_roots
        if orig_env is None:
          os.environ.pop("SAR_EXTRACT_DIR", None)
        else:
          os.environ["SAR_EXTRACT_DIR"] = orig_env

  def test_extract_sar_retries_when_binfs_swaps_launcher_hash(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
      uid = os.getuid()
      old_dir = os.path.join(tmpdir, f"sar.server.{uid}.a16e06")
      new_dir = os.path.join(tmpdir, f"sar.server.{uid}.5368aa")
      old_bin = os.path.join(old_dir, patcher.SAR_BIN_RELPATH)
      new_bin = os.path.join(new_dir, patcher.SAR_BIN_RELPATH)
      for p in (old_bin, new_bin):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
          f.write(b"ELF")

      launcher = os.path.join(tmpdir, "server")
      launcher_v2 = os.path.join(tmpdir, "server_v2")
      state_file = os.path.join(tmpdir, "called_once")
      # Avoid sed -i (incompatible between GNU sed and macOS BSD sed) by swapping
      # the launcher script via cp on first run
      with open(launcher_v2, "w", encoding="utf-8") as f:
        f.write(
            "#!/bin/sh\n"
            '# SHARDIR="${SAR_EXTRACT_DIR}/${STUB}.5368aa"\n'
            f'echo "Extraction directory: {new_dir}"\n'
        )
      os.chmod(launcher_v2, 0o755)
      with open(launcher, "w", encoding="utf-8") as f:
        f.write(
            "#!/bin/sh\n"
            '# SHARDIR="${SAR_EXTRACT_DIR}/${STUB}.a16e06"\n'
            f'if [ ! -f "{state_file}" ]; then\n'
            f'  touch "{state_file}"\n'
            f'  echo "Extraction directory: {old_dir}"\n'
            f'  cp "{launcher_v2}" "{launcher}"\n'
            "else\n"
            f'  echo "Extraction directory: {new_dir}"\n'
            "fi\n"
        )
      os.chmod(launcher, 0o755)

      orig_launcher = patcher.SAR_LAUNCHER
      orig_roots = patcher.SAR_SEARCH_ROOTS
      orig_env = os.environ.get("SAR_EXTRACT_DIR")
      try:
        patcher.SAR_LAUNCHER = launcher
        patcher.SAR_SEARCH_ROOTS = (tmpdir,)
        os.environ.pop("SAR_EXTRACT_DIR", None)
        found = patcher.extract_sar_and_find_binaries()
        self.assertEqual(found, [old_bin, new_bin])
      finally:
        patcher.SAR_LAUNCHER = orig_launcher
        patcher.SAR_SEARCH_ROOTS = orig_roots
        if orig_env is None:
          os.environ.pop("SAR_EXTRACT_DIR", None)
        else:
          os.environ["SAR_EXTRACT_DIR"] = orig_env


if __name__ == "__main__":
  unittest.main()
