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
  candidates = sorted(
      glob.glob(
          "/tmp/sar.server.*/server_impl.runfiles/google3/third_party/jetski/cmd/hub/server/node_basic"
      )
  )
  if candidates:
    return candidates[0]
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
  if (selector === 'span') return el.tagName === 'SPAN';
  if (selector === 'span.truncate') return el.tagName === 'SPAN' && el.className === 'truncate';
  if (selector === 'a[aria-label]') return el.tagName === 'A' && el.hasAttribute('aria-label');
  if (selector === 'button[aria-label="Display Options"]') {
    return el.tagName === 'BUTTON' && el.getAttribute('aria-label') === 'Display Options';
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

const initialSummaries = {
  'open-solo': makeSummary('[10:00] ⦿ Active solo task', 'proj-1'),
  'open-head': makeSummary('[10:05] ▸ Open chain topic', 'proj-1'),
  'open-link': makeSummary('[09:50] « Open chain topic', 'proj-1'),
  'closed-solo': makeSummary('[08:00] ✓ Finished bugfix', 'proj-1'),
  'closed-head': makeSummary('[11:00] ‹✓› Closed chain topic', 'proj-closed-only'),
  'closed-mid': makeSummary('[10:30] «» Closed chain topic', 'proj-closed-only'),
  'closed-root': makeSummary('[10:15] » Closed chain topic', 'proj-closed-only'),
  'native-hidden': makeSummary('[12:00] ✓ Background subagent', 'proj-1', {
    sourceMetadata: { hideFromConversationList: true },
  }),
};

const storeState = {
  trajectorySummaries: {
    summaries: { ...initialSummaries },
  },
};

const subscribers = [];
const reduxStore = {
  getState() {
    return storeState;
  },
  dispatch(action) {
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

global.window = {
  innerWidth: 1280,
  localStorage: {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
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

// Initial state: filterEnabled=true, filterScope='closed'
// Closed chats in 'closed' scope: closed-solo (✓), closed-head (‹✓›), closed-mid («»), closed-root (») => 4
// Open chats: open-solo (⦿), open-head (▸), open-link («) => 3
// Total eligible: 7 (native-hidden is excluded)
assert.strictEqual(btn.getAttribute('aria-label'), 'Closed hidden: 4');
assert.strictEqual(btn.getAttribute('data-active'), 'true');
assert.strictEqual(emptyProjSpan.textContent, 'No active chats (3 hidden)');

const s1 = storeState.trajectorySummaries.summaries;
assert.strictEqual(s1['closed-solo'].trajectoryMetadata.sourceMetadata.hideFromConversationList, true);
assert.strictEqual(s1['closed-head'].trajectoryMetadata.sourceMetadata.hideFromConversationList, true);
assert.strictEqual(s1['closed-mid'].trajectoryMetadata.sourceMetadata.hideFromConversationList, true);
assert.strictEqual(s1['closed-root'].trajectoryMetadata.sourceMetadata.hideFromConversationList, true);
assert.strictEqual(Boolean(s1['open-link'].trajectoryMetadata?.sourceMetadata?.hideFromConversationList), false);
assert.strictEqual(s1['closed-solo'].annotations.archived, false);

// Hover bubble in Filter ON (closed) state
btn.dispatchEvent('mouseenter');
const bubble = global.document.getElementById('jetski-closed-chats-filter-bubble');
assert(bubble, 'Hover bubble must be created');
assert.strictEqual(bubble.getAttribute('data-visible'), 'true');
assert(bubble.innerHTML.includes('Filter ON: closed chats hidden'));
assert(bubble.innerHTML.includes('Active / open chats (▸, ⦿):'));
assert(bubble.innerHTML.includes('Closed final chats (✓, ‹✓›):'));
assert(bubble.innerHTML.includes('Closed chain links («, «», »):'));
assert(bubble.innerHTML.includes('4 of 7'));
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
assert.strictEqual(btn.getAttribute('aria-label'), 'All chats (4 closed)');
assert.strictEqual(emptyProjSpan.textContent, 'No conversations yet');
assert(bubble.innerHTML.includes('Filter OFF: showing all chats'));
assert(bubble.innerHTML.includes('Would be hidden (count on button):'));

const s2 = storeState.trajectorySummaries.summaries;
assert.strictEqual(s2['closed-solo'].trajectoryMetadata.sourceMetadata.hideFromConversationList, false);
// Natively hidden conversation must remain hidden even when filter is turned OFF
assert.strictEqual(s2['native-hidden'].trajectoryMetadata.sourceMetadata.hideFromConversationList, true);

// Right-click (contextmenu) switches scope to 'active-only' and enables filter
btn.dispatchEvent('contextmenu', { button: 2 });
assert.strictEqual(btn.getAttribute('data-active'), 'true');
// In 'active-only' scope, open-link («) is also hidden => 5 hidden of 7
assert.strictEqual(btn.getAttribute('aria-label'), 'Active only · 5 hidden');
assert(bubble.innerHTML.includes('Filter ON: active chats only'));
assert(bubble.innerHTML.includes('Handoff chain links («, «», ») — all:'));
assert(bubble.innerHTML.includes('5 of 7'));
assert.strictEqual(
  storeState.trajectorySummaries.summaries['open-link'].trajectoryMetadata.sourceMetadata.hideFromConversationList,
  true
);

// Incoming stream batch while filter is ON automatically hides newly arrived closed chat
reduxStore.dispatch({
  type: 'trajectorySummaries/applyStreamBatch',
  payload: {
    updates: [
      ['streamed-closed', makeSummary('[12:30] ✓ Newly finalized chat', 'proj-1')],
    ],
    deletes: [],
  },
});
assert.strictEqual(
  storeState.trajectorySummaries.summaries['streamed-closed'].trajectoryMetadata.sourceMetadata.hideFromConversationList,
  true
);

// Re-evaluating the script cleanly replaces the button via __jetskiClosedChatsFilterCleanup
eval(code);
const btnsAfterReload = actionRow.querySelectorAll('.jetski-closed-chats-filter-btn');
assert.strictEqual(btnsAfterReload.length, 1, 'Reloading script must not duplicate button');

console.log('ALL_JS_ASSERTIONS_PASSED');
"""
    res = subprocess.run(
        [node_bin, "-e", harness, js_path],
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


if __name__ == "__main__":
  unittest.main()
