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

// Titlebar with more-actions button for an open chain chat ('open-current')
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

function makeSummary(title, projectId, extraMeta = {}, archived = false) {
  return {
    summary: title,
    annotations: { archived },
    trajectoryMetadata: {
      projectId,
      ...extraMeta,
    },
  };
}

// Scheme Γ: open chains read ▸ root, ✓ steps, ⦿ current; finalize rewrites them to «, ‹✓›, » or «» solo
const initialSummaries = {
  'solo-active': makeSummary('[10:00] ⦿ Active solo task', 'proj-1'),
  'open-root': makeSummary('[09:00] ▸ Open chain topic', 'proj-1'),
  'open-step': makeSummary('[09:30] ✓ Open chain topic', 'proj-1'),
  'open-current': makeSummary('[10:05] ⦿ Open chain topic', 'proj-1'),
  'closed-solo': makeSummary('[08:00] «» Finished bugfix', 'proj-1'),
  'closed-pair-root': makeSummary('[07:00] « Short closed chain', 'proj-1'),
  'closed-pair-tail': makeSummary('[07:30] » Short closed chain', 'proj-1'),
  'quoted-title': makeSummary('«Quoted» topic without lifecycle prefix', 'proj-1'),
  'closed-root': makeSummary('[10:15] « Closed chain topic', 'proj-closed-only'),
  'closed-step': makeSummary('[10:30] ‹✓› Closed chain topic', 'proj-closed-only'),
  'closed-tail': makeSummary('[11:00] » Closed chain topic', 'proj-closed-only'),
  'native-hidden': makeSummary('[12:00] ✓ Background subagent', 'proj-1', {
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

const btn = actionRow.querySelector('.jetski-closed-chats-filter-btn');
assert(btn, 'Filter toggle button must be injected before Display Options');
assert.strictEqual(actionRow.children[0], btn);

const isHidden = (id) => Boolean(
  storeState.trajectorySummaries.summaries[id].trajectoryMetadata?.sourceMetadata?.hideFromConversationList
);
const assertHidden = (ids, expected) => {
  for (const id of ids) assert.strictEqual(isHidden(id), expected, id);
};
const CLOSED_SCOPE_HIDDEN = [
  'closed-solo', 'closed-pair-root', 'closed-pair-tail',
  'closed-root', 'closed-step', 'closed-tail',
];

// Initial state: filterEnabled=true, filterScope='closed'
// Hidden: closed-solo («»), closed-pair-* (« »), closed-root/step/tail (« ‹✓› ») => 6
// Visible: solo-active (⦿), open-root (▸), open-step (✓), open-current (⦿), quoted-title => 5 (3 active)
// Total eligible: 11 (native-hidden is excluded)
assert.strictEqual(btn.getAttribute('aria-label'), '3 active · 6 closed');
assert.strictEqual(btn.getAttribute('data-active'), 'true');
assert.strictEqual(emptyProjSpan.textContent, 'No active chats (3 hidden)');

assertHidden(CLOSED_SCOPE_HIDDEN, true);
assertHidden(['solo-active', 'open-root', 'open-step', 'open-current', 'quoted-title'], false);
assert.strictEqual(storeState.trajectorySummaries.summaries['closed-solo'].annotations.archived, false);

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

// Hover bubble in Filter ON (closed) state
btn.dispatchEvent('mouseenter');
const bubble = global.document.getElementById('jetski-closed-chats-filter-bubble');
assert(bubble, 'Hover bubble must be created');
assert.strictEqual(bubble.getAttribute('data-visible'), 'true');
assert(bubble.innerHTML.includes('Filter ON: closed chats hidden'));
assert(bubble.innerHTML.includes('Active (⦿, no marker):'));
assert(bubble.innerHTML.includes('Open chain roots (▸):'));
assert(bubble.innerHTML.includes('Handed-off steps (✓):'));
assert(bubble.innerHTML.includes('Finalized chains («, ‹✓›, », «»):'));
assert(bubble.innerHTML.includes('6 of 11'));
assert(!/[А-Яа-яЁё]/.test(bubble.innerHTML), 'Bubble HTML must not contain Cyrillic');

// Verify MutationObserver ignores mutations inside bubble and button
rafCallbacks = [];
observerCallback([{ target: bubble }, { target: btn }]);
assert.strictEqual(rafCallbacks.length, 0, 'Bubble/button mutations must not schedule sync');

// Left-click via pointerdown + mousedown + click sequence must toggle ONCE (to Filter OFF)
const pdRes = btn.dispatchEvent('pointerdown', { button: 0 });
assert.strictEqual(pdRes.defaultPrevented, true);
assert.strictEqual(pdRes.propagationStopped, true);
btn.dispatchEvent('mousedown', { button: 0 });
btn.dispatchEvent('click', { button: 0 });

assert.strictEqual(btn.getAttribute('data-active'), 'false');
assert.strictEqual(btn.getAttribute('aria-label'), 'All (3 active · 6 closed)');
assert.strictEqual(emptyProjSpan.textContent, 'No conversations yet');
assert(bubble.innerHTML.includes('Filter OFF: showing all chats'));
assert(bubble.innerHTML.includes('Would be hidden (count on button):'));

assertHidden(CLOSED_SCOPE_HIDDEN, false);
// Natively hidden conversation must remain hidden even when filter is turned OFF
assertHidden(['native-hidden'], true);

// Right-click (contextmenu) switches scope to 'active-only' and enables filter
btn.dispatchEvent('contextmenu', { button: 2 });
assert.strictEqual(btn.getAttribute('data-active'), 'true');
// In 'active-only' scope, open-root (▸) and open-step (✓) are also hidden => 8 hidden of 11
assert.strictEqual(btn.getAttribute('aria-label'), '3 active · 8 hidden');
assert(bubble.innerHTML.includes('Filter ON: active chats only'));
assert(bubble.innerHTML.includes('● <b>Active only</b>'));
assert(bubble.innerHTML.includes('8 of 11'));
assertHidden([...CLOSED_SCOPE_HIDDEN, 'open-root', 'open-step'], true);
assertHidden(['solo-active', 'open-current', 'quoted-title'], false);

btn.dispatchEvent('contextmenu', { button: 2 });
assert.strictEqual(btn.getAttribute('aria-label'), '3 active · 6 closed');
assertHidden(['open-root', 'open-step'], false);

// Incoming stream batch while filter is ON automatically hides newly arrived closed chat
reduxStore.dispatch({
  type: 'trajectorySummaries/applyStreamBatch',
  payload: {
    updates: [
      ['streamed-closed', makeSummary('[12:30] «» Newly finalized chat', 'proj-1')],
    ],
    deletes: [],
  },
});
assertHidden(['streamed-closed'], true);

// Re-evaluating the script cleanly replaces the button via __jetskiClosedChatsFilterCleanup
eval(code);
const btnsAfterReload = actionRow.querySelectorAll('.jetski-closed-chats-filter-btn');
assert.strictEqual(btnsAfterReload.length, 1, 'Reloading script must not duplicate button');

(async () => {
  const titlebarBtn = titlebarToolbar.querySelector('.jetski-titlebar-close-reopen-btn');
  assert(titlebarBtn, 'Titlebar Close/Reopen button must be injected before titlebar-more-actions');
  assert.strictEqual(titlebarToolbar.children[0], titlebarBtn);
  assert.strictEqual(titlebarBtn.getAttribute('aria-label'), 'Close chat');
  assert.strictEqual(titlebarBtn.getAttribute('data-finalized'), 'false');

  const rowBtn = rowHoverBar.querySelector('.jetski-row-close-reopen-btn');
  assert(rowBtn, 'Sidebar row Close/Reopen button must be injected before pin button');
  assert.strictEqual(rowHoverBar.children[0], rowBtn);
  assert.strictEqual(rowBtn.getAttribute('aria-label'), 'Close chat');
  assert.strictEqual(rowBtn.getAttribute('data-finalized'), 'false');

  // Clicking the sidebar row Close button on a solo chat swaps its marker to «»
  rpcRequests.length = 0;
  rowBtn.dispatchEvent('pointerdown', { button: 0 });
  await new Promise((r) => setTimeout(r, 10));

  assert.strictEqual(
    storeState.trajectorySummaries.summaries['solo-active'].summary,
    '[10:00] «» Active solo task'
  );
  assert.strictEqual(rowBtn.getAttribute('aria-label'), 'Reopen chat');
  assert.strictEqual(rowBtn.getAttribute('data-finalized'), 'true');
  assert.strictEqual(rpcRequests.length, 1);
  assert.deepStrictEqual(rpcRequests[0].body, {
    cascadeIds: ['solo-active'],
    annotations: { title: '[10:00] «» Active solo task' },
    mergeAnnotations: true,
  });
  assert.strictEqual(rpcRequests[0].headers['x-codeium-csrf-token'], 'test-csrf-token');

  // Clicking the titlebar Close button on a chain member closes the entire chain [open-root, open-step, open-current]
  rpcRequests.length = 0;
  titlebarBtn.dispatchEvent('pointerdown', { button: 0 });
  await new Promise((r) => setTimeout(r, 10));

  assert.strictEqual(
    storeState.trajectorySummaries.summaries['open-root'].summary,
    '[09:00] « Open chain topic'
  );
  assert.strictEqual(
    storeState.trajectorySummaries.summaries['open-step'].summary,
    '[09:30] ‹✓› Open chain topic'
  );
  assert.strictEqual(
    storeState.trajectorySummaries.summaries['open-current'].summary,
    '[10:05] » Open chain topic'
  );
  assert.strictEqual(titlebarBtn.getAttribute('aria-label'), 'Reopen chat');
  assert.strictEqual(titlebarBtn.getAttribute('data-finalized'), 'true');
  assert.strictEqual(rpcRequests.length, 3);

  // Clicking the titlebar Reopen button reopens the entire chain back to [▸, ✓, ⦿]
  rpcRequests.length = 0;
  // Wait > 300ms debounce or trigger via fresh click after advancing Date.now
  const origNow = Date.now;
  Date.now = () => origNow() + 500;
  titlebarBtn.dispatchEvent('pointerdown', { button: 0 });
  await new Promise((r) => setTimeout(r, 10));
  Date.now = origNow;

  assert.strictEqual(
    storeState.trajectorySummaries.summaries['open-root'].summary,
    '[09:00] ▸ Open chain topic'
  );
  assert.strictEqual(
    storeState.trajectorySummaries.summaries['open-step'].summary,
    '[09:30] ✓ Open chain topic'
  );
  assert.strictEqual(
    storeState.trajectorySummaries.summaries['open-current'].summary,
    '[10:05] ⦿ Open chain topic'
  );
  assert.strictEqual(titlebarBtn.getAttribute('aria-label'), 'Close chat');
  assert.strictEqual(titlebarBtn.getAttribute('data-finalized'), 'false');
  assert.strictEqual(rpcRequests.length, 3);

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
