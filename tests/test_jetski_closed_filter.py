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
    this._textContent = '';
    this._innerHTML = '';
    this.style = {};
    this.attributes = new Map();
    this.children = [];
    this.parentElement = null;
    this.listeners = new Map();
  }
  get firstChild() {
    return this.children[0] || null;
  }
  get lastChild() {
    return this.children[this.children.length - 1] || null;
  }
  get textContent() {
    if (this.children.length > 0) {
      return this._textContent + this.children.map((c) => c.textContent).join('');
    }
    return this._textContent;
  }
  set textContent(val) {
    this._textContent = String(val);
    for (const c of this.children) c.parentElement = null;
    this.children = [];
    if (global.__recordMutation) {
      global.__recordMutation(this);
    }
  }
  get innerHTML() {
    return this._innerHTML;
  }
  set innerHTML(val) {
    this._innerHTML = String(val);
    if (!val) {
      for (const c of this.children) c.parentElement = null;
      this.children = [];
    }
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
    // Detach from any previous parent so moving an existing node never duplicates it
    if (child.parentElement) child.remove();
    child.parentElement = this;
    this.children.push(child);
    if (global.__recordMutation) {
      global.__recordMutation(this);
    }
    return child;
  }
  insertBefore(child, ref) {
    // Detach from any previous parent so re-inserting an existing node relocates it cleanly
    if (child === ref) return child;
    if (child.parentElement) child.remove();
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
      target: this,
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
    if (
      !propagationStopped &&
      global.document &&
      typeof global.document.dispatchEvent === 'function'
    ) {
      global.document.dispatchEvent(evt);
    }
    return { defaultPrevented, propagationStopped };
  }
  getBoundingClientRect() {
    return { left: 24, top: 40, bottom: 62, right: 140, width: 116, height: 22 };
  }
  scrollIntoView(opts) {
    this._lastScrolledIntoView = opts || true;
  }
  focus() {}
  closest(selector) {
    let cur = this;
    while (cur) {
      if (matches(cur, selector)) return cur;
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
  if (selector === '[data-testid="conversation-row-sidebar"][data-cascade-id]') {
    return (
      el.getAttribute('data-testid') === 'conversation-row-sidebar' &&
      el.hasAttribute('data-cascade-id')
    );
  }
  if (selector === '[data-testid="conversation-view"][data-cascade-id]') {
    return el.getAttribute('data-testid') === 'conversation-view' && el.hasAttribute('data-cascade-id');
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
  const tagAttrMatch = /^([a-zA-Z0-9]+)\[([-a-zA-Z0-9_]+)="([^"]+)"\]$/.exec(selector);
  if (tagAttrMatch) {
    return (
      el.tagName === tagAttrMatch[1].toUpperCase() &&
      el.getAttribute(tagAttrMatch[2]) === tagAttrMatch[3]
    );
  }
  const doubleAttrMatch =
    /^\[([-a-zA-Z0-9_]+)="([^"]+)"\]\[([-a-zA-Z0-9_]+)="([^"]+)"\]$/.exec(selector);
  if (doubleAttrMatch) {
    return (
      el.getAttribute(doubleAttrMatch[1]) === doubleAttrMatch[2] &&
      el.getAttribute(doubleAttrMatch[3]) === doubleAttrMatch[4]
    );
  }
  const attrEqMatch = /^\[([-a-zA-Z0-9_]+)="([^"]+)"\]$/.exec(selector);
  if (attrEqMatch) {
    return el.getAttribute(attrEqMatch[1]) === attrEqMatch[2];
  }
  const attrHasMatch = /^\[([-a-zA-Z0-9_]+)\]$/.exec(selector);
  if (attrHasMatch) {
    return el.hasAttribute(attrHasMatch[1]);
  }
  if (selector.startsWith('.')) {
    // Support compound class selectors in fake DOM queries
    const classes = selector
      .split('.')
      .filter(Boolean)
      .map((cls) => cls.replace(/\\(.)/g, '$1'));
    const elClasses = el.className ? el.className.split(/\s+/) : [];
    return classes.every((cls) => elClasses.includes(cls));
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

// Active conversation view for 'open-step' (middle piece of chain ['open-root', 'open-step', 'open-current'])
const convoViewEl = new FakeElement('div');
convoViewEl.setAttribute('data-testid', 'conversation-view');
convoViewEl.setAttribute('data-cascade-id', 'open-step');

const convoRailSlot = new FakeElement('div');
convoRailSlot.className = 'pointer-events-none absolute inset-y-0 left-0 z-10 w-7';
const convoNavEl = new FakeElement('nav');
convoNavEl.setAttribute('data-testid', 'conversation-navigator');
const convoNavInner = new FakeElement('div');
convoNavInner.className = 'relative flex min-h-0 flex-col';
const convoNavList = new FakeElement('ol');
convoNavInner.appendChild(convoNavList);
convoNavEl.appendChild(convoNavInner);
convoRailSlot.appendChild(convoNavEl);
convoViewEl.appendChild(convoRailSlot);

const convoFooterEl = new FakeElement('div');
convoFooterEl.className = 'relative w-full px-4 pb-4';
const convoFooterInner = new FakeElement('div');
convoFooterInner.className = 'flex flex-col w-full gap-2 min-h-0';
const convoInputBox = new FakeElement('div');
convoInputBox.setAttribute('data-testid', 'agent-input-box');
const convoContentEditable = new FakeElement('div');
convoContentEditable.setAttribute('contenteditable', 'true');
const convoSendBtn = new FakeElement('button');
convoSendBtn.setAttribute('data-testid', 'send-button');
convoInputBox.appendChild(convoContentEditable);
convoInputBox.appendChild(convoSendBtn);
convoFooterInner.appendChild(convoInputBox);
convoFooterEl.appendChild(convoFooterInner);
convoViewEl.appendChild(convoFooterEl);
bodyEl.appendChild(convoViewEl);

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
  multiConvoLayout: {
    rootNode: {
      type: 'split',
      id: 'split-1',
      direction: 'horizontal',
      children: [
        { type: 'pane', id: 'pane-1', cascadeId: 'solo-active' },
        { type: 'pane', id: 'pane-2', cascadeId: 'open-step' },
      ],
      weights: [50, 50],
    },
    focusedPaneId: 'pane-2',
  },
  conversation: {
    cascadeId: 'open-step',
  },
  sideBar: {
    isOpen: true,
  },
};

const subscribers = [];
const reduxStore = {
  getState() {
    return storeState;
  },
  dispatch(action) {
    if (action?.type === 'sideBar/setIsOpenInternal') {
      storeState.sideBar = { isOpen: Boolean(action.payload) };
      return action;
    }
    // Mirror multiConvoLayout/resetPane in test harness
    if (action?.type === 'multiConvoLayout/resetPane') {
      const { paneId, cascadeId } = action.payload || {};
      const updatePane = (node) => {
        if (!node) return node;
        if (node.type === 'pane' && node.id === paneId) {
          return { ...node, cascadeId };
        }
        if (node.type === 'split' && Array.isArray(node.children)) {
          return { ...node, children: node.children.map(updatePane) };
        }
        return node;
      };
      if (storeState.multiConvoLayout?.rootNode) {
        storeState.multiConvoLayout.rootNode = updatePane(storeState.multiConvoLayout.rootNode);
      }
      return action;
    }
    if (action?.type === 'multiConvoLayout/setFocusedPane') {
      if (storeState.multiConvoLayout) {
        storeState.multiConvoLayout.focusedPaneId = action.payload;
      }
      return action;
    }
    if (action?.type === 'conversation/setCascadeId') {
      if (storeState.conversation) {
        storeState.conversation.cascadeId = action.payload?.cascadeId;
      }
      return action;
    }
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
      // Stands in for osConversationNotificationsMiddleware, which runs after the reducer
      if (fakeNotificationMiddleware) fakeNotificationMiddleware(action);
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
// Handoff redirects served in jetski-chains.json; empty until the auto-switch checks
let chainRedirects = {};
let fakeNotificationMiddleware = null;
// Chime files that reached the network, i.e. were not dropped by the sound gate
const soundFetches = [];
let observerCallback = null;
let rafCallbacks = [];
const rpcRequests = [];

global.PopStateEvent = class {
  constructor(type) {
    this.type = type;
  }
};

global.window = {
  innerWidth: 1280,
  location: { pathname: '/c/open-step', search: '' },
  history: {
    state: { __TSR_index: 2, __TSR_key: 'k2', key: 'k2' },
    _lastPushed: null,
    _lastState: null,
    pushState(state, title, url) {
      this.state = state;
      this._lastState = state;
      this._lastPushed = url;
      const qIdx = String(url).indexOf('?');
      if (qIdx >= 0) {
        global.window.location.pathname = url.slice(0, qIdx);
        global.window.location.search = url.slice(qIdx);
      } else {
        global.window.location.pathname = url;
        global.window.location.search = '';
      }
    },
  },
  dispatchEvent(evt) {},
  __APP_CONFIG__: { csrfToken: 'test-csrf-token' },
  localStorage: {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
  },
  fetch: async (url, opts) => {
    if (String(url).includes('SearchConversations')) {
      const body = JSON.parse(opts.body);
      rpcRequests.push({ url: String(url), body, headers: opts.headers });
      return {
        ok: true,
        json: async () => ({
          results: [
            {
              cascadeId: 'open-step',
              title: '[10:00] ⦿ Open Step Chat',
              snippet: 'Found 1410476 in test step',
              archived: true,
              matchedStepIndex: 1,
            },
          ],
        }),
      };
    }
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
          redirects: chainRedirects,
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
    if (String(url).endsWith('.mp3')) {
      soundFetches.push(String(url));
      return { ok: true, status: 200 };
    }
    return { ok: false, status: 404 };
  },
};
const docListeners = new Map();
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
  addEventListener(type, fn) {
    if (!docListeners.has(type)) docListeners.set(type, []);
    docListeners.get(type).push(fn);
  },
  removeEventListener(type, fn) {
    if (!docListeners.has(type)) return;
    const arr = docListeners.get(type);
    const idx = arr.indexOf(fn);
    if (idx !== -1) arr.splice(idx, 1);
  },
  dispatchEvent(evt) {
    const type = typeof evt === 'string' ? evt : evt?.type;
    const eventObj = typeof evt === 'string' ? { type: evt, target: docEl } : evt;
    for (const fn of (docListeners.get(type) || [])) {
      fn(eventObj);
    }
    return eventObj;
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

const filterBar = actionRow.querySelector('.jetski-attention-filter-bar');
assert(filterBar, '3-button filter bar must be injected inside actionRow');
assert.strictEqual(
  actionRow.children[0],
  filterBar,
  'Filter bar must sit inside actionRow before displayOptionsBtn'
);
assert.strictEqual(
  actionRow.children[1],
  displayOptionsBtn,
  'displayOptionsBtn must follow filterBar'
);
const bucketBtns = filterBar.querySelectorAll('.jetski-closed-chats-filter-btn');
assert.strictEqual(bucketBtns.length, 3, 'Filter bar must render 3 state buttons');

const btnNeedsMe = filterBar.querySelector('button[data-bucket="needs-me"]');
const btnWorking = filterBar.querySelector('button[data-bucket="working"]');
const btnWaiting = filterBar.querySelector('button[data-bucket="waiting"]');
assert.deepStrictEqual(
  bucketBtns.map((b) => b.getAttribute('data-bucket')),
  ['needs-me', 'working', 'waiting']
);
assert.strictEqual(btnNeedsMe.innerHTML, '<span class="jetski-bucket-icon">🚨</span>');
assert(
  !btnNeedsMe.querySelector('.jetski-filter-badge'),
  'Filter button must not carry count badge'
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

// Hovering each button displays a dedicated compact tooltip explaining that button's state
btnWaiting.dispatchEvent('mouseenter');
const bubble = global.document.getElementById('jetski-closed-chats-filter-bubble');
assert(bubble, 'Hover bubble must be created');
assert.strictEqual(bubble.getAttribute('data-visible'), 'true');
assert.strictEqual(bubble.getAttribute('data-mode'), 'filter');
assert.strictEqual(bubble.style.top, '8px', 'Compact filter pill must position above button');
assert(bubble.className.includes('surface-glass'));
assert(bubble.innerHTML.includes('⏳ Waiting (Cron / External) · 2 hidden'));
assert(!bubble.innerHTML.includes('All 3 chat states'), 'Must not render multi-state section');
assert(!bubble.innerHTML.includes('Right-click'), 'Must not render controls footer');
assert(
  !injectedStyle.textContent.includes('--popover'),
  'Must use surface-glass/card tokens instead of nonexistent --popover'
);
assert(!/[А-Яа-яЁё]/.test(bubble.innerHTML), 'Bubble HTML must not contain Cyrillic');

btnNeedsMe.dispatchEvent('mouseenter');
assert(bubble.innerHTML.includes('🚨 Needs Me (⦿) · 3 shown'));

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
assert(bubble.innerHTML.includes('⏳ Waiting (Cron / External) · 2 shown'));
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
  const titlebarContextBadge = titlebarToolbar.querySelector('.jetski-titlebar-context-badge');
  const titlebarSearchElem = titlebarToolbar.querySelector('.jetski-chat-search-btn');
  const titlebarLocateBtn = titlebarToolbar.querySelector('.jetski-chat-locate-btn');
  const titlebarBtn = titlebarToolbar.querySelector('.jetski-titlebar-archive-btn');
  assert(titlebarContextBadge, 'Titlebar context badge must be injected leftmost');
  assert(titlebarLocateBtn, 'Titlebar Locate button must be injected after Search');
  assert(
    !titlebarToolbar.querySelector('.jetski-titlebar-wait-btn'),
    'Titlebar must not contain Wait button'
  );
  assert(titlebarBtn, 'Titlebar Archive button must be injected before titlebar-more-actions');
  assert.strictEqual(titlebarLocateBtn.textContent, '🎯');
  assert.strictEqual(titlebarLocateBtn.getAttribute('data-testid'), 'jetski-chat-locate-btn');
  assert.strictEqual(titlebarLocateBtn.getAttribute('data-cascade-id'), 'open-current');
  assert.strictEqual(titlebarToolbar.children[0], titlebarContextBadge);
  assert.strictEqual(titlebarToolbar.children[1], titlebarSearchElem);
  assert.strictEqual(titlebarToolbar.children[2], titlebarLocateBtn);
  assert.strictEqual(titlebarToolbar.children[3], titlebarBtn);
  assert.strictEqual(titlebarToolbar.children[4], moreActionsBtn);
  assert.strictEqual(titlebarBtn.textContent, '🗄');
  assert.strictEqual(titlebarBtn.getAttribute('data-testid'), 'jetski-titlebar-archive-btn');
  assert.strictEqual(titlebarBtn.getAttribute('aria-label'), 'Archive chat');
  assert.strictEqual(titlebarBtn.getAttribute('data-archived'), 'false');

  assert(!rowHoverBar.querySelector('.jetski-row-wait-btn'), 'Sidebar row must not contain Wait button');
  assert(!rowHoverBar.querySelector('.jetski-row-archive-btn'), 'Rows keep only native Archive');

  // Mutations inside injected action buttons must not schedule a sync loop
  rafCallbacks = [];
  observerCallback([
    { target: titlebarLocateBtn },
    { target: titlebarBtn },
  ]);
  assert.strictEqual(rafCallbacks.length, 0);

  const advanceClock = () => {
    const prev = Date.now;
    const shifted = prev() + 500;
    Date.now = () => shifted;
  };
  const summaryOf = (id) => storeState.trajectorySummaries.summaries[id];

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
  assert.strictEqual(
    rpcRequests[0].url,
    '/exa.language_server_pb.LanguageServerService/UpdateConversationAnnotations'
  );
  assert.strictEqual(rpcRequests[0].headers['x-codeium-csrf-token'], 'test-csrf-token');
  assert.strictEqual(summaryOf('open-current').annotations.archived, true);
  assert.strictEqual(summaryOf('open-current').summary, '[10:05] ⦿ Open chain topic');
  assert.strictEqual(summaryOf('open-root').summary, '[09:00] ⦿ Open chain topic');
  assert.strictEqual(summaryOf('open-step').summary, '[09:30] ⦿ Open chain topic');
  assert.strictEqual(titlebarBtn.getAttribute('aria-label'), 'Unarchive chat');
  assert.strictEqual(titlebarBtn.getAttribute('data-archived'), 'true');
  assert.strictEqual(titlebarBtn.textContent, '⦿');
  assert.strictEqual(liveBucketBtn('needs-me').getAttribute('aria-label'), '🚨 Needs Me: 2 (shown)');

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
  assert.strictEqual(titlebarBtn.textContent, '🗄');
  assert.strictEqual(liveBucketBtn('needs-me').getAttribute('aria-label'), '🚨 Needs Me: 3 (shown)');

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
  assert.strictEqual(roadmapBubble.getAttribute('data-mode'), 'roadmap');
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

  // Hovering titlebar icon buttons renders compact surface-glass pill bubbles
  titlebarSearchBtn.dispatchEvent('mouseenter');
  assert.strictEqual(roadmapBubble.getAttribute('data-visible'), 'true');
  assert.strictEqual(roadmapBubble.getAttribute('data-mode'), 'filter');
  assert(
    roadmapBubble.innerHTML.includes('🔍 Search this chat and its hidden pieces'),
    'Search button hover must display the compact search pill bubble'
  );
  titlebarSearchBtn.dispatchEvent('mouseleave');
  assert.strictEqual(roadmapBubble.getAttribute('data-visible'), 'false');

  titlebarLocateBtn.dispatchEvent('mouseenter');
  assert.strictEqual(roadmapBubble.getAttribute('data-visible'), 'true');
  assert.strictEqual(roadmapBubble.getAttribute('data-mode'), 'filter');
  assert(
    roadmapBubble.innerHTML.includes('🎯 Locate this chat in sidebar'),
    'Locate button hover must display the compact locate pill bubble'
  );
  titlebarLocateBtn.dispatchEvent('mouseleave');
  assert.strictEqual(roadmapBubble.getAttribute('data-visible'), 'false');

  assert.strictEqual(titlebarBtn.getAttribute('title'), null);
  titlebarBtn.dispatchEvent('mouseenter');
  assert.strictEqual(roadmapBubble.getAttribute('data-visible'), 'true');
  assert.strictEqual(roadmapBubble.getAttribute('data-mode'), 'filter');
  assert(
    roadmapBubble.innerHTML.includes('🗄 Archive chat'),
    'Archive button hover must display the compact archive pill bubble'
  );
  titlebarBtn.dispatchEvent('mouseleave');
  assert.strictEqual(roadmapBubble.getAttribute('data-visible'), 'false');

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

  // Verify global search covers archived chats and chain pieces
  rpcRequests.length = 0;
  const globalResultsEl = new FakeElement('div');
  await window.__jetskiChatSearch.runGlobalSearch('1410476', globalResultsEl);
  assert.strictEqual(rpcRequests.length, 1);
  assert(rpcRequests[0].url.includes('SearchConversations'));
  assert.strictEqual(rpcRequests[0].body.query, '1410476');
  assert(globalResultsEl.innerHTML.includes('open-step'));
  assert(globalResultsEl.innerHTML.includes('piece 2/3'));
  assert(globalResultsEl.innerHTML.includes('🗄 archived'));
  assert(globalResultsEl.innerHTML.includes('<mark>1410476</mark>'));

  // Verify sidebar search button opens global search panel
  const sidebarSearchBtn = new FakeElement('button');
  sidebarSearchBtn.setAttribute('data-testid', 'sidebar-search-button');
  bodyEl.appendChild(sidebarSearchBtn);
  sidebarSearchBtn.dispatchEvent('pointerdown', { button: 0 });
  await new Promise((r) => setTimeout(r, 10));
  const searchPanel = document.getElementById('jetski-chat-search-panel');
  assert(searchPanel, 'Clicking sidebar search button must mount search panel');
  const modeBar = searchPanel.querySelector('.jcs-mode-bar');
  assert(modeBar, 'Search panel must carry mode toggle bar');
  const globalModeBtn = modeBar.querySelector('[data-mode="global"]');
  assert(globalModeBtn, 'Mode bar must carry global search button');
  assert.strictEqual(globalModeBtn.getAttribute('data-active'), 'true');
  searchPanel.remove();
  sidebarSearchBtn.remove();

  // 1. Verify chain navigator stars on 'open-step' (middle piece: 2 before, 1 after)
  const starsBefore = convoNavEl.querySelector('[data-testid="jetski-chain-stars-before"]');
  assert(starsBefore, 'Stars before group must be mounted in convo navigator');
  assert.strictEqual(starsBefore.children.length, 2, 'Two pieces up to open-step');
  assert.strictEqual(starsBefore.children[0].textContent, '✦');
  assert.strictEqual(starsBefore.children[0].getAttribute('data-active'), 'false');
  assert.strictEqual(starsBefore.children[1].textContent, '★');
  assert.strictEqual(starsBefore.children[1].getAttribute('data-active'), 'true');

  // Verify DOM star button instances are preserved across sync passes when chain is unchanged
  const firstRootStarBtn = starsBefore.children[0];
  await window.__jetskiChainNav.syncConversationChainNavigators();
  assert.strictEqual(
    starsBefore.children[0],
    firstRootStarBtn,
    'Unchanged chain must preserve star button DOM instances'
  );

  // Verify star click in split-view URL mode preserves other panes and sets focused params
  global.window.location.pathname = '/c/solo-active+open-step';
  global.window.location.search = '?focused=open-step&section=proj-1';
  starsBefore.children[0].dispatchEvent('pointerdown', { button: 0 });
  starsBefore.children[0].dispatchEvent('click', { button: 0 });
  assert.strictEqual(
    global.window.history._lastPushed,
    '/c/solo-active+open-root?focused=open-root&section=proj-1'
  );
  assert.strictEqual(global.window.history._lastState?.__TSR_index, 3);
  assert.strictEqual(global.window.history._lastState?.__TSR_key, 'k2');
  assert.strictEqual(
    storeState.multiConvoLayout.rootNode.children[1].cascadeId,
    'open-root'
  );
  assert.strictEqual(storeState.multiConvoLayout.focusedPaneId, 'pane-2');
  assert.strictEqual(storeState.conversation.cascadeId, 'open-root');

  // Reset URL to single-pane for jump-to-tail test
  global.window.location.pathname = '/c/open-step';
  global.window.location.search = '';

  const starsAfter = convoNavEl.querySelector('[data-testid="jetski-chain-stars-after"]');
  assert(starsAfter, 'Stars after group must be mounted in convo navigator');
  assert.strictEqual(starsAfter.children.length, 1, 'One descendant piece after open-step');
  assert.strictEqual(starsAfter.children[0].textContent, '✦');
  assert.strictEqual(starsAfter.children[0].getAttribute('data-active'), 'false');

  // 2. Verify non-tail input lock on 'open-step'
  assert.strictEqual(convoInputBox.style.display, 'none', 'Input box must be hidden on non-tail piece');
  assert.strictEqual(convoContentEditable.getAttribute('contenteditable'), 'false');
  assert.strictEqual(convoSendBtn.getAttribute('disabled'), 'true');

  const archiveBanner = convoFooterInner.querySelector('[data-testid="jetski-archive-piece-banner"]');
  assert(archiveBanner, 'Archive banner must be displayed on non-tail piece');
  assert(archiveBanner.textContent.includes('Archived piece #2 of 3'));
  const jumpBtn = archiveBanner.querySelector('[data-testid="jetski-jump-to-tail-btn"]');
  assert(jumpBtn, 'Jump to tail button must be present in archive banner');

  // Verify jumpBtn preserves its exact DOM element identity across sync passes
  await window.__jetskiChainNav.syncConversationChainNavigators();
  assert.strictEqual(
    archiveBanner.querySelector('[data-testid="jetski-jump-to-tail-btn"]'),
    jumpBtn,
    'Unchanged chain must preserve jump-to-tail button DOM instance'
  );

  // 3. Click jump-to-tail and verify navigation target and optimistic banner removal
  window.history._lastPushed = null;
  advanceClock();
  jumpBtn.dispatchEvent('pointerdown', { button: 0 });
  jumpBtn.dispatchEvent('click');
  assert.strictEqual(window.history._lastPushed, '/c/open-current');
  assert(
    !convoFooterInner.querySelector('[data-testid="jetski-archive-piece-banner"]'),
    'Archive banner must be optimistically removed immediately on jump click'
  );

  // 3b. Verify layout where ActiveInputArea is absent and nativeArchivedBanner is present
  convoInputBox.remove();
  const nativeArchivedBanner = new FakeElement('div');
  nativeArchivedBanner.className = 'min-h-[80px] rounded-2xl bg-muted';
  convoFooterInner.appendChild(nativeArchivedBanner);
  convoViewEl.setAttribute('data-cascade-id', 'open-root');
  await window.__jetskiChainNav.syncConversationChainNavigators();

  assert.strictEqual(
    nativeArchivedBanner.style.display,
    'none',
    'Native archived banner must be hidden on non-tail archived piece'
  );
  const rootArchiveBanner = convoFooterInner.querySelector(
    '[data-testid="jetski-archive-piece-banner"]'
  );
  assert(
    rootArchiveBanner,
    'Archive banner must mount in footer container of native archived banner'
  );
  assert.strictEqual(rootArchiveBanner.parentElement, convoFooterInner);
  assert(rootArchiveBanner.textContent.includes('Archived piece #1 of 3'));
  const rootJumpBtn = rootArchiveBanner.querySelector('[data-testid="jetski-jump-to-tail-btn"]');
  assert(rootJumpBtn, 'Jump to tail button must be present on archived piece #1');
  assert.strictEqual(rootJumpBtn.getAttribute('data-source-id'), 'open-root');
  assert.strictEqual(rootJumpBtn.getAttribute('data-tail-id'), 'open-current');
  window.history._lastPushed = null;
  advanceClock();
  rootJumpBtn.dispatchEvent('pointerdown', { button: 0 });
  rootJumpBtn.dispatchEvent('click');
  assert.strictEqual(window.history._lastPushed, '/c/open-current');

  // Restore convoInputBox and remove nativeArchivedBanner for step 4
  nativeArchivedBanner.remove();
  convoFooterInner.appendChild(convoInputBox);

  // 4. Switch conversation-view rapidly from non-tail to tail and ensure stale sync is dropped
  convoViewEl.setAttribute('data-cascade-id', 'open-step');
  const staleSync = window.__jetskiChainNav.syncConversationChainNavigators();
  convoViewEl.setAttribute('data-cascade-id', 'open-current');
  const freshSync = window.__jetskiChainNav.syncConversationChainNavigators();
  await Promise.all([staleSync, freshSync]);

  const tailStarsBefore = convoNavEl.querySelector('[data-testid="jetski-chain-stars-before"]');
  assert.strictEqual(tailStarsBefore.children.length, 3, 'All 3 pieces before/at tail');
  assert.strictEqual(tailStarsBefore.children[2].textContent, '★');
  assert.strictEqual(tailStarsBefore.children[2].getAttribute('data-active'), 'true');

  const tailStarsAfter = convoNavEl.querySelector('[data-testid="jetski-chain-stars-after"]');
  assert(!tailStarsAfter || tailStarsAfter.children.length === 0, 'No stars after tail piece');

  // Input must be restored/unlocked on tail piece
  assert.strictEqual(convoInputBox.style.display, '', 'Input box must be restored on tail piece');
  assert.strictEqual(convoContentEditable.getAttribute('contenteditable'), 'true');
  assert.strictEqual(convoSendBtn.hasAttribute('disabled'), false);
  assert(!convoFooterInner.querySelector('[data-testid="jetski-archive-piece-banner"]'));

  // 5. Fallback path when conversation-navigator is unmounted (< 2 turns in ConversationNavigatorRail)
  convoNavEl.remove();
  await window.__jetskiChainNav.syncConversationChainNavigators();
  const fallbackStars = convoRailSlot.querySelector('.jetski-chain-stars-fallback');
  assert(fallbackStars, 'Fallback stars must mount in rail slot when nav is unmounted');
  assert.strictEqual(fallbackStars.children.length, 3, 'All 3 pieces in fallback');
  assert.strictEqual(fallbackStars.children[2].textContent, '★');
  assert.strictEqual(fallbackStars.children[2].getAttribute('data-active'), 'true');
  assert.strictEqual(fallbackStars.style.position, 'absolute');
  assert.strictEqual(fallbackStars.style.left, '0');
  assert.strictEqual(fallbackStars.style.width, '28px');
  assert.strictEqual(fallbackStars.style.top, '50%');
  assert.strictEqual(fallbackStars.style.transform, 'translateY(-50%)');

  // A fresh handoff redirect moves a background split pane off the just-archived chat onto
  // its successor without taking focus from the pane the user works in
  {
    const savedPath = global.window.location.pathname;
    const savedSearch = global.window.location.search;
    const savedLayout = JSON.parse(JSON.stringify(storeState.multiConvoLayout));
    const savedConvo = storeState.conversation.cascadeId;
    const origNow = Date.now;
    // Step past the 5s chain index cache so the new redirects are fetched
    let shiftedNow = origNow() + 6000;
    Date.now = () => shiftedNow;
    const isoAgo = (ms) => new Date(shiftedNow - ms).toISOString();

    global.window.location.pathname = '/c/solo-active+open-step';
    global.window.location.search = '?focused=solo-active';
    storeState.multiConvoLayout.rootNode.children[0].cascadeId = 'solo-active';
    storeState.multiConvoLayout.rootNode.children[1].cascadeId = 'open-step';
    storeState.multiConvoLayout.focusedPaneId = 'pane-1';
    storeState.conversation.cascadeId = 'solo-active';
    chainRedirects = {
      'open-step': { to: 'open-current', at: isoAgo(1000), kind: 'handoff' },
      'open-root': { to: 'open-step', at: isoAgo(1000), kind: 'handoff' },
      'closed-root': { to: 'closed-tail', at: isoAgo(120000), kind: 'handoff' },
    };
    convoViewEl.setAttribute('data-cascade-id', 'open-step');
    window.history._lastPushed = null;
    await window.__jetskiChainNav.syncConversationChainNavigators();
    assert.strictEqual(
      window.history._lastPushed,
      '/c/solo-active+open-current?focused=solo-active'
    );
    assert.strictEqual(storeState.multiConvoLayout.rootNode.children[1].cascadeId, 'open-current');
    assert.strictEqual(storeState.multiConvoLayout.rootNode.children[0].cascadeId, 'solo-active');
    assert.strictEqual(storeState.multiConvoLayout.focusedPaneId, 'pane-1');
    assert.strictEqual(storeState.conversation.cascadeId, 'solo-active');

    // Going back to the archived piece does not bounce forward a second time
    convoViewEl.setAttribute('data-cascade-id', 'open-step');
    window.history._lastPushed = null;
    await window.__jetskiChainNav.syncConversationChainNavigators();
    assert.strictEqual(window.history._lastPushed, null);

    // open-root was opened by a star click, so its redirect is ignored
    convoViewEl.setAttribute('data-cascade-id', 'open-root');
    await window.__jetskiChainNav.syncConversationChainNavigators();
    assert.strictEqual(window.history._lastPushed, null);

    // A redirect older than a minute is history, not a live handoff
    convoViewEl.setAttribute('data-cascade-id', 'closed-root');
    await window.__jetskiChainNav.syncConversationChainNavigators();
    assert.strictEqual(window.history._lastPushed, null);

    chainRedirects = {};
    Date.now = origNow;
    convoViewEl.setAttribute('data-cascade-id', 'open-current');
    global.window.location.pathname = savedPath;
    global.window.location.search = savedSearch;
    storeState.multiConvoLayout = savedLayout;
    storeState.conversation.cascadeId = savedConvo;
  }

  // The archived predecessor ending its handoff turn stays silent; a visible chat that ends
  // its turn, alone or in the same batch, still chimes
  {
    const prevSummaries = { ...storeState.trajectorySummaries.summaries };
    fakeNotificationMiddleware = (action) => {
      for (const [id, s] of action.payload.updates) {
        if (prevSummaries[id]?.notFullyIdle && !s.notFullyIdle) {
          void window.fetch('/antigravityCascadeDone.mp3');
        }
      }
    };
    const busy = (id) => ({ ...prevSummaries[id], notFullyIdle: true });
    const idle = (id) => ({ ...prevSummaries[id], notFullyIdle: false });
    const endTurns = (ids) => {
      for (const id of ids) prevSummaries[id] = busy(id);
      storeState.trajectorySummaries = { summaries: { ...prevSummaries } };
      reduxStore.dispatch({
        type: 'trajectorySummaries/applyStreamBatch',
        payload: { updates: ids.map((id) => [id, idle(id)]), deletes: [] },
      });
    };

    endTurns(['open-step']);
    assert.deepStrictEqual(soundFetches, []);
    endTurns(['solo-active']);
    assert.deepStrictEqual(soundFetches, ['/antigravityCascadeDone.mp3']);
    // A mixed batch is not muted: the real chime of solo-active must play, and the 2s quiet
    // window in playAgentSound folds both requests into one sound
    endTurns(['open-step', 'solo-active']);
    assert.strictEqual(soundFetches.length, 3);
    assert.strictEqual(window.__jetskiAgentSoundMuted, 0);

    fakeNotificationMiddleware = null;
    storeState.trajectorySummaries = {
      summaries: Object.fromEntries(
        Object.entries(prevSummaries).map(([id, s]) => [id, { ...s, notFullyIdle: false }])
      ),
    };
  }

  // Verify locateConversationInSidebar resolves chain tail, opens sidebar, unhides bucket,
  // expands collapsed sections/pagination, scrolls virtualizer, and pulses the DOM row
  const vlistEl = new FakeElement('div');
  vlistEl.setAttribute('data-testid', 'conversation-list-sidebar');
  const targetSidebarRow = new FakeElement('div');
  targetSidebarRow.setAttribute('data-testid', 'conversation-row-sidebar');
  targetSidebarRow.setAttribute('data-cascade-id', 'open-current');

  const toggledSectionHeaders = [];
  const toggledCollapsedSections = [];
  const toggledPaginations = [];
  let lastVirtualScroll = null;

  const vlistProps = {
    items: [
      {
        type: 'section-header',
        id: 'main-section-header',
        collapseId: 'main-section-header',
        isCollapsed: true,
      },
      {
        type: 'row',
        id: 'pinned:open-current',
        cascadeId: 'open-current',
        groupId: 'pinned',
      },
      {
        type: 'header',
        id: 'proj-1',
        isCollapsed: true,
      },
    ],
    onToggleSectionHeader(id) {
      toggledSectionHeaders.push(id);
      vlistProps.items[0].isCollapsed = false;
    },
    onToggleCollapseSection(id) {
      toggledCollapsedSections.push(id);
      vlistProps.items[2].isCollapsed = false;
      vlistProps.items.push({
        type: 'show-more',
        id: 'show-more:proj-1',
        groupId: 'proj-1',
      });
    },
    onTogglePagination(id) {
      toggledPaginations.push(id);
      vlistProps.items = vlistProps.items.filter((it) => it.type !== 'show-more');
      vlistProps.items.push({
        type: 'row',
        id: 'proj-1:open-current',
        cascadeId: 'open-current',
        groupId: 'proj-1',
      });
    },
  };

  vlistEl.__reactFiber$vlist = {
    memoizedProps: {},
    return: {
      memoizedProps: vlistProps,
      memoizedState: {
        memoizedState: {
          scrollToIndex(idx, opts) {
            lastVirtualScroll = { idx, opts };
            if (!targetSidebarRow.parentElement) {
              vlistEl.appendChild(targetSidebarRow);
            }
          },
        },
        next: null,
      },
    },
  };
  bodyEl.appendChild(vlistEl);

  // Close sidebar and hide needs-me bucket before locating archived piece 'open-step'
  storeState.sideBar = { isOpen: false };
  if (liveBucketBtn('needs-me').getAttribute('data-active') === 'true') {
    advanceClock();
    liveBucketBtn('needs-me').dispatchEvent('pointerdown', { button: 0 });
    liveBucketBtn('needs-me').dispatchEvent('click');
  }
  assert.strictEqual(liveBucketBtn('needs-me').getAttribute('data-active'), 'false');

  await window.__jetskiLocateInSidebar('open-step');

  assert.strictEqual(storeState.sideBar.isOpen, true, 'Sidebar must be opened');
  assert.strictEqual(
    liveBucketBtn('needs-me').getAttribute('data-active'),
    'true',
    'Filtered bucket must be unhidden'
  );
  assert.deepStrictEqual(toggledSectionHeaders, ['main-section-header']);
  assert.deepStrictEqual(toggledCollapsedSections, ['proj-1']);
  assert.deepStrictEqual(toggledPaginations, ['proj-1']);
  assert.deepStrictEqual(
    lastVirtualScroll,
    { idx: 3, opts: { align: 'center' } },
    'Virtualizer must scroll to the project group row rather than the pinned row'
  );
  assert.strictEqual(targetSidebarRow.getAttribute('data-jetski-located-row'), 'true');
  assert.deepStrictEqual(targetSidebarRow._lastScrolledIntoView, {
    block: 'center',
    behavior: 'smooth',
  });

  // Verify collapse-first "Collapse All / Expand All" projects button in sidebar filter bar
  const collapseBtn = actionRow.querySelector('.jetski-collapse-projects-btn');
  assert(collapseBtn, 'Collapse projects button must be mounted inside actionRow filter bar');
  assert.strictEqual(
    collapseBtn.getAttribute('data-testid'),
    'jetski-collapse-projects-btn'
  );

  // Set up 3 projects in vlistProps: proj-1 (open), proj-2 (open), proj-3 (collapsed)
  vlistProps.items.push(
    { type: 'header', id: 'proj-2', isCollapsed: false },
    { type: 'header', id: 'proj-3', isCollapsed: true }
  );
  const toggledByCollapseAll = [];
  vlistProps.onToggleCollapseSection = (id) => {
    toggledByCollapseAll.push(id);
    const it = vlistProps.items.find(
      (x) => x && x.type === 'header' && (x.id === id || x.id === 'header-' + id)
    );
    if (it) it.isCollapsed = !it.isCollapsed;
  };

  advanceClock();
  for (const sub of subscribers) sub();
  while (rafCallbacks.length > 0) rafCallbacks.shift()();

  assert.strictEqual(collapseBtn.getAttribute('data-action'), 'collapse');
  assert.strictEqual(collapseBtn.textContent, '⇈');
  assert.strictEqual(
    collapseBtn.getAttribute('aria-label'),
    '⇈ Collapse all projects (2/3 open)'
  );

  // First click: collapses all open projects (only proj-1 and proj-2 need toggling)
  toggledByCollapseAll.length = 0;
  advanceClock();
  collapseBtn.dispatchEvent('pointerdown', { button: 0 });
  collapseBtn.dispatchEvent('click');

  assert.deepStrictEqual(toggledByCollapseAll.sort(), ['proj-1', 'proj-2']);
  assert.strictEqual(collapseBtn.getAttribute('data-action'), 'expand');
  assert.strictEqual(collapseBtn.textContent, '⇊');
  assert.strictEqual(
    collapseBtn.getAttribute('aria-label'),
    '⇊ Expand all projects (3 collapsed)'
  );
  const allCollapsed = vlistProps.items
    .filter((x) => x && x.type === 'header')
    .every((x) => x.isCollapsed);
  assert.strictEqual(allCollapsed, true, 'All project headers must be collapsed');

  // Second click: expands all projects (all 3 need toggling)
  toggledByCollapseAll.length = 0;
  advanceClock();
  collapseBtn.dispatchEvent('pointerdown', { button: 0 });
  collapseBtn.dispatchEvent('click');

  assert.deepStrictEqual(toggledByCollapseAll.sort(), ['proj-1', 'proj-2', 'proj-3']);
  assert.strictEqual(collapseBtn.getAttribute('data-action'), 'collapse');
  assert.strictEqual(collapseBtn.textContent, '⇈');
  assert.strictEqual(
    collapseBtn.getAttribute('aria-label'),
    '⇈ Collapse all projects (3/3 open)'
  );
  const allExpanded = vlistProps.items
    .filter((x) => x && x.type === 'header')
    .every((x) => !x.isCollapsed);
  assert.strictEqual(allExpanded, true, 'All project headers must be expanded');

  // Verify Needs-Reaction inbox widget mounted right before vlistEl and shows only unseen chats
  const inboxEl = bodyEl.querySelector('.jetski-needs-reaction-inbox');
  assert(inboxEl, 'Needs-Reaction inbox widget must be mounted');
  assert.strictEqual(
    bodyEl.children.indexOf(inboxEl),
    bodyEl.children.indexOf(vlistEl) - 1,
    'Inbox widget must be positioned immediately before conversation-list-sidebar'
  );
  const styleEl = global.document.getElementById('jetski-closed-chats-filter-style');
  assert(styleEl, 'Filter style must be injected');
  assert(
    styleEl.textContent.includes('[data-testid="new-conversation-button"]'),
    'Style must hide new-conversation-button'
  );
  assert(
    styleEl.textContent.includes('div:has(> .jetski-needs-reaction-inbox)'),
    'Style must enforce flex column on inbox parent to prevent bottom overflow'
  );
  assert(
    styleEl.textContent.includes('border-radius: 0;'),
    'Style must format inbox as flat architectural hairline section'
  );
  // Clear running agent from earlier sidebar-row tests so inbox starts in a clean 0-item state
  reduxStore.dispatch({
    type: 'trajectorySummaries/applyStreamBatch',
    payload: {
      updates: [
        [
          'agent-working',
          makeSummary('[10:02] ⦿ Busy agent task', 'proj-1', { archived: true }, false, {
            status: 'CASCADE_RUN_STATUS_IDLE',
          }),
        ],
      ],
      deletes: [],
    },
  });
  while (rafCallbacks.length > 0) rafCallbacks.shift()();

  // With all 3 tabs empty, the inbox stays visible and shows an all-clear line
  assert.strictEqual(inboxEl.getAttribute('data-empty'), 'true');
  assert.strictEqual(inboxEl.style.display, '');
  assert.strictEqual(
    inboxEl.querySelector('.jetski-inbox-empty-row').textContent,
    '✓ All caught up: nothing needs you'
  );
  assert.strictEqual(inboxEl.querySelectorAll('.jetski-inbox-row').length, 0);
  assert.strictEqual(inboxEl.getAttribute('data-collapsed'), 'false');

  // Verify the 3 tabs in headerLeft
  const tabNeedsMe = inboxEl.querySelector('.jetski-inbox-tab-needs-me');
  const tabWorking = inboxEl.querySelector('.jetski-inbox-tab-working');
  const tabStale = inboxEl.querySelector('.jetski-inbox-tab-stale');
  assert(tabNeedsMe, 'Needs Me tab button must exist');
  assert(tabWorking, 'Working tab button must exist');
  assert(tabStale, 'Stale tab button must exist');
  assert.strictEqual(tabNeedsMe.querySelector('.jetski-inbox-tab-label').textContent, 'Needs Me');
  assert.strictEqual(tabWorking.querySelector('.jetski-inbox-tab-label').textContent, 'Working');
  assert.strictEqual(tabStale.querySelector('.jetski-inbox-tab-label').textContent, 'Stale');
  assert.strictEqual(tabNeedsMe.getAttribute('data-active'), 'true');
  assert.strictEqual(tabWorking.getAttribute('data-active'), 'false');
  assert.strictEqual(tabStale.getAttribute('data-active'), 'false');

  // Seed store projects map with activity badges and add unseen vs already-viewed chats
  storeState.projects = {
    projects: {
      'proj-1': { name: 'Breaking Chats · ⟳ 1 ⚠ 2' },
      'proj-archived': { id: 'proj-archived', name: 'Old Hackathon', archived: true },
    },
  };
  // The Workspaces sidebar keeps project names only in the sidebarProjects array
  storeState.sidebarProjects = {
    enabled: true,
    projects: [{ id: 'proj-urgent', name: 'Skia Text · ● 1', hideWhenEmpty: false }],
  };
  reduxStore.dispatch({
    type: 'trajectorySummaries/applyStreamBatch',
    payload: {
      updates: [
        [
          'urgent-question',
          makeSummary('[14:10] ⦿ Ask user question', 'proj-urgent', {}, false, {
            waitingSteps: [{ stepIndex: 3 }],
            lastModifiedTime: '2026-10-28T14:10:00Z',
          }),
        ],
        [
          'urgent-unread',
          makeSummary('[14:05] ⦿ Finished unread analysis', 'proj-1', {}, false, {
            // Protobuf Timestamp object with bigint seconds as stored in Redux
            lastModifiedTime: { seconds: 1793196300n, nanos: 0 },
            annotations: {
              archived: false,
              lastUserViewTime: { seconds: 1793192400n, nanos: 0 },
            },
          }),
        ],
        [
          'never-viewed',
          makeSummary('[14:01] ⦿ Never opened root chat', 'proj-1', {}, false, {
            lastModifiedTime: '2026-10-28T14:01:00Z',
          }),
        ],
        [
          'solo-active',
          makeSummary('[18:51] ⦿ Issue #195102 fontconfig', 'proj-1', {}, false, {
            lastModifiedTime: '2026-10-28T12:00:00Z',
            annotations: {
              archived: false,
              lastUserViewTime: '2026-10-28T12:05:00Z',
            },
          }),
        ],
        [
          'open-current',
          makeSummary('[10:05] ⦿ Open chain topic', 'proj-1', {}, false, {
            lastModifiedTime: '2026-10-28T14:20:00Z',
            annotations: {
              archived: false,
              lastUserViewTime: '2026-10-28T14:25:00Z',
            },
          }),
        ],
        [
          'agent-running',
          makeSummary('[14:25] ⦿ Running agent task', 'proj-1', {}, false, {
            status: 'CASCADE_RUN_STATUS_RUNNING',
            lastModifiedTime: '2026-10-28T14:25:00Z',
          }),
        ],
        [
          'agent-bg-wait',
          makeSummary('[14:20] ⦿ Waiting on background runner', 'proj-1', {}, false, {
            status: 'CASCADE_RUN_STATUS_IDLE',
            notFullyIdle: true,
            lastModifiedTime: '2026-10-28T14:20:00Z',
          }),
        ],
        [
          'stale-task',
          makeSummary('[10:00] ⦿ Forgotten ancient task', 'proj-1', {}, false, {
            status: 'CASCADE_RUN_STATUS_IDLE',
            lastModifiedTime: new Date(Date.now() - 10 * 24 * 3600 * 1000).toISOString(),
          }),
        ],
        [
          'tool-stale',
          makeSummary(
            '[10:00] ⦿ Hidden CLI chat',
            'proj-1',
            { sourceMetadata: { tool: 'agentapi' } },
            false,
            {
              status: 'CASCADE_RUN_STATUS_IDLE',
              lastModifiedTime: new Date(Date.now() - 12 * 24 * 3600 * 1000).toISOString(),
            }
          ),
        ],
        [
          'legacy-closed-stale',
          makeSummary('[10:00] «» Handed off legacy chat', 'proj-1', {}, false, {
            status: 'CASCADE_RUN_STATUS_IDLE',
            lastModifiedTime: new Date(Date.now() - 12 * 24 * 3600 * 1000).toISOString(),
          }),
        ],
        [
          'archived-proj-stale',
          makeSummary('[10:00] ⦿ Hackathon demo', 'proj-archived', {}, false, {
            status: 'CASCADE_RUN_STATUS_IDLE',
            lastModifiedTime: new Date(Date.now() - 12 * 24 * 3600 * 1000).toISOString(),
          }),
        ],
      ],
      deletes: [],
    },
  });
  while (rafCallbacks.length > 0) rafCallbacks.shift()();

  // open-current and solo-active (already viewed) are excluded;
  // urgent-question, urgent-unread, and never-viewed appear in needs-me;
  // agent-running and agent-bg-wait appear in working;
  // stale-task appears in stale
  assert.strictEqual(inboxEl.getAttribute('data-empty'), 'false');
  assert(!inboxEl.querySelector('.jetski-inbox-empty-row'));
  assert.strictEqual(inboxEl.style.display, '');
  assert.strictEqual(inboxEl.getAttribute('data-active-tab'), 'needs-me');
  assert.strictEqual(inboxEl.getAttribute('data-urgent'), 'true');
  assert.strictEqual(inboxEl.getAttribute('data-urgent-count'), '1');
    assert.strictEqual(inboxEl.getAttribute('data-count'), '3');

  // Check badges across all 3 tab buttons
  assert.strictEqual(tabNeedsMe.querySelector('.jetski-inbox-count-badge').textContent, '3');
  assert.strictEqual(tabWorking.querySelector('.jetski-inbox-count-badge').textContent, '2');
  assert.strictEqual(tabStale.querySelector('.jetski-inbox-count-badge').textContent, '1');

  const urgentPill = tabNeedsMe.querySelector('.jetski-inbox-urgent-pill');
  assert(urgentPill, 'Urgent pill must be rendered when urgentCount > 0');
  assert.strictEqual(urgentPill.style.display, '');
  assert.strictEqual(urgentPill.textContent, '· 1!');

  const inboxRows = inboxEl.querySelectorAll('.jetski-inbox-row');
  assert.strictEqual(inboxRows.length, 3);
  assert.deepStrictEqual(
    inboxRows.map((r) => [r.getAttribute('data-cascade-id'), r.getAttribute('data-tier')]),
    [
      ['urgent-question', 'urgent'],
      ['urgent-unread', 'unseen'],
      ['never-viewed', 'unseen'],
    ]
  );

  // Project badge strips trailing activity suffixes (' · ● 1' and ' · ⟳ 1 ⚠ 2')
  assert.strictEqual(
    inboxRows[0].querySelector('.jetski-inbox-project').textContent,
    'Skia Text'
  );
  assert.strictEqual(
    inboxRows[0].querySelector('.jetski-inbox-title').textContent,
    'Ask user question'
  );
  assert.strictEqual(
    inboxRows[0].querySelector('.jetski-inbox-time').textContent,
    '14:10'
  );
  assert.strictEqual(
    inboxRows[1].querySelector('.jetski-inbox-project').textContent,
    'Breaking Chats'
  );

  // Rows in Needs Me tab have ✓ (seen) and 🗄 (archive), but no Locate or Wait buttons
  assert(inboxRows[0].querySelector('.jetski-inbox-seen-btn'));
  assert(inboxRows[0].querySelector('.jetski-inbox-archive-btn'));
  assert(!inboxRows[0].querySelector('.jetski-inbox-locate-btn'));
  assert(!inboxRows[0].querySelector('.jetski-inbox-wait-btn'));

  // Mutations inside the inbox must not schedule a sync loop
  rafCallbacks = [];
  observerCallback([{ target: inboxEl }, { target: inboxRows[0] }]);
  assert.strictEqual(rafCallbacks.length, 0, 'Inbox mutations must not trigger observer sync');

  // Clicking an inbox row navigates to that conversation, but does NOT dismiss it
  window.history._lastPushed = null;
  advanceClock();
  inboxRows[0].dispatchEvent('pointerdown', { button: 0 });
  inboxRows[0].dispatchEvent('click', { button: 0 });
  assert.strictEqual(window.history._lastPushed, '/c/urgent-question');
  assert(window.__jetskiManualVisitedCids.has('urgent-question'));
  assert.strictEqual(
    inboxEl.getAttribute('data-count'),
    '3',
    'Clicking row must navigate without dismissing from Needs Me'
  );
  assert.strictEqual(inboxEl.getAttribute('data-urgent-count'), '1');

  // Clicking the inline checkmark (seen) button on urgent-question dismisses it
  advanceClock();
  const seenBtn = inboxRows[0].querySelector('.jetski-inbox-seen-btn');
  seenBtn.dispatchEvent('pointerdown', { button: 0 });
  seenBtn.dispatchEvent('click', { button: 0 });
  assert.strictEqual(inboxEl.getAttribute('data-count'), '2');
  assert.strictEqual(inboxEl.getAttribute('data-urgent-count'), '0');
  assert.strictEqual(inboxEl.getAttribute('data-urgent'), 'false');
  assert.strictEqual(tabNeedsMe.querySelector('.jetski-inbox-count-badge').textContent, '2');
  assert.strictEqual(urgentPill.style.display, 'none');

  // Focusing or clicking inside the prompt input box dismisses the active conversation
  const agentInputBox = new FakeElement('div');
  agentInputBox.setAttribute('data-testid', 'agent-input-box');
  agentInputBox.setAttribute('data-cascade-id', 'urgent-unread');
  bodyEl.appendChild(agentInputBox);
  advanceClock();
  agentInputBox.dispatchEvent('pointerdown', { button: 0 });
  assert.strictEqual(
    inboxEl.getAttribute('data-count'),
    '1',
    'Input box pointerdown must dismiss conversation from Needs Me'
  );
  assert.strictEqual(tabNeedsMe.querySelector('.jetski-inbox-count-badge').textContent, '1');
  const remainingRows = inboxEl.querySelectorAll('.jetski-inbox-row');
  assert.strictEqual(remainingRows.length, 1);
  assert.strictEqual(remainingRows[0].getAttribute('data-cascade-id'), 'never-viewed');

  // Dismissing the last remaining conversation keeps Needs Me active with an empty row
  agentInputBox.setAttribute('data-cascade-id', 'never-viewed');
  advanceClock();
  agentInputBox.dispatchEvent('pointerdown', { button: 0 });
  assert.strictEqual(
    inboxEl.getAttribute('data-active-tab'),
    'needs-me',
    'Needs Me must stay active even when count reaches 0'
  );
  assert.strictEqual(tabNeedsMe.getAttribute('data-active'), 'true');
  assert.strictEqual(tabWorking.getAttribute('data-active'), 'false');
  assert.strictEqual(inboxEl.getAttribute('data-count'), '0');
  assert.strictEqual(tabNeedsMe.querySelector('.jetski-inbox-count-badge').textContent, '0');
  assert.strictEqual(tabWorking.querySelector('.jetski-inbox-count-badge').textContent, '2');
  assert.strictEqual(
    inboxEl.querySelector('.jetski-inbox-empty-row').textContent,
    '✓ All caught up: nothing needs you'
  );

  // Switch to Working tab
  advanceClock();
  tabWorking.dispatchEvent('pointerdown', { button: 0 });
  tabWorking.dispatchEvent('click', { button: 0 });
  assert.strictEqual(inboxEl.getAttribute('data-active-tab'), 'working');
  assert.strictEqual(tabWorking.getAttribute('data-active'), 'true');
  assert.strictEqual(tabNeedsMe.getAttribute('data-active'), 'false');
  assert.strictEqual(storage.get('jetski_inbox_tab_v1'), 'working');
  assert.strictEqual(inboxEl.getAttribute('data-count'), '2');

  const workingRows = inboxEl.querySelectorAll('.jetski-inbox-row');
  assert.strictEqual(workingRows.length, 2);
  assert.strictEqual(workingRows[0].getAttribute('data-cascade-id'), 'agent-running');
  assert.strictEqual(workingRows[0].querySelector('.jetski-inbox-tier-icon').textContent, '🤖');
  assert.strictEqual(workingRows[1].getAttribute('data-cascade-id'), 'agent-bg-wait');
  assert.strictEqual(workingRows[1].querySelector('.jetski-inbox-tier-icon').textContent, '⏳');
  // Working rows have 0 action buttons
  assert.strictEqual(workingRows[0].querySelector('.jetski-inbox-actions').children.length, 0);
  assert.strictEqual(workingRows[1].querySelector('.jetski-inbox-actions').children.length, 0);

  // Switch to Stale tab
  advanceClock();
  tabStale.dispatchEvent('pointerdown', { button: 0 });
  tabStale.dispatchEvent('click', { button: 0 });
  assert.strictEqual(inboxEl.getAttribute('data-active-tab'), 'stale');
  assert.strictEqual(storage.get('jetski_inbox_tab_v1'), 'stale');
  assert.strictEqual(inboxEl.getAttribute('data-count'), '1');

  const staleRows = inboxEl.querySelectorAll('.jetski-inbox-row');
  assert.strictEqual(staleRows.length, 1);
  assert.strictEqual(staleRows[0].getAttribute('data-cascade-id'), 'stale-task');
  assert.strictEqual(staleRows[0].querySelector('.jetski-inbox-tier-icon').textContent, '🧹');
  assert.strictEqual(staleRows[0].querySelector('.jetski-inbox-time').textContent, '10d');
  // Stale rows have 1 action button: archive
  assert.strictEqual(staleRows[0].querySelector('.jetski-inbox-actions').children.length, 1);
  const staleArchiveBtn = staleRows[0].querySelector('.jetski-inbox-archive-btn');
  assert(staleArchiveBtn, 'Stale row must have archive button');

  // Archiving stale-task empties the Stale tab and auto-falls back to Needs Me
  rpcRequests.length = 0;
  advanceClock();
  staleArchiveBtn.dispatchEvent('pointerdown', { button: 0 });
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(summaryOf('stale-task').annotations.archived, true);
  assert.strictEqual(tabStale.querySelector('.jetski-inbox-count-badge').textContent, '0');
  assert.strictEqual(
    inboxEl.getAttribute('data-active-tab'),
    'needs-me',
    'Emptied non-Needs Me tab must return to Needs Me'
  );
  assert.strictEqual(inboxEl.getAttribute('data-count'), '0');

  // Clicking the inbox header toggles collapsed state and persists to storage
  const inboxHeader = inboxEl.querySelector('.jetski-inbox-header');
  advanceClock();
  inboxHeader.dispatchEvent('pointerdown', { button: 0 });
  assert.strictEqual(inboxEl.getAttribute('data-collapsed'), 'true');
  assert.strictEqual(storage.get('jetski_inbox_collapsed_v1'), 'true');
  assert.strictEqual(inboxEl.querySelector('.jetski-inbox-toggle-btn').textContent, '▸');

  // Clicking any tab button while collapsed auto-expands the inbox
  advanceClock();
  tabWorking.dispatchEvent('pointerdown', { button: 0 });
  tabWorking.dispatchEvent('click', { button: 0 });
  assert.strictEqual(inboxEl.getAttribute('data-collapsed'), 'false');
  assert.strictEqual(storage.get('jetski_inbox_collapsed_v1'), 'false');
  assert.strictEqual(inboxEl.querySelector('.jetski-inbox-toggle-btn').textContent, '▾');
  assert.strictEqual(inboxEl.getAttribute('data-active-tab'), 'working');

  // Clear remaining items across working and needs-me
  reduxStore.dispatch({
    type: 'trajectorySummaries/applyStreamBatch',
    payload: {
      updates: [
        [
          'never-viewed',
          makeSummary('[14:01] ⦿ Never opened root chat', 'proj-1', {}, true, {
            lastModifiedTime: '2026-10-28T14:01:00Z',
          }),
        ],
        [
          'agent-running',
          makeSummary('[14:25] ⦿ Running agent task', 'proj-1', {}, false, {
            status: 'CASCADE_RUN_STATUS_IDLE',
            notFullyIdle: false,
            annotations: { archived: false, lastUserViewTime: '2026-10-28T14:26:00Z' },
          }),
        ],
        [
          'agent-bg-wait',
          makeSummary('[14:20] ⦿ Waiting on background runner', 'proj-1', {}, false, {
            status: 'CASCADE_RUN_STATUS_IDLE',
            notFullyIdle: false,
            annotations: { archived: false, lastUserViewTime: '2026-10-28T14:21:00Z' },
          }),
        ],
      ],
      deletes: [],
    },
  });
  while (rafCallbacks.length > 0) rafCallbacks.shift()();
  assert.strictEqual(inboxEl.getAttribute('data-empty'), 'true');
  assert.strictEqual(inboxEl.style.display, '');
  assert.strictEqual(inboxEl.getAttribute('data-active-tab'), 'needs-me');
  assert.strictEqual(
    inboxEl.querySelector('.jetski-inbox-empty-row').textContent,
    '✓ All caught up: nothing needs you'
  );

  // Cleanup must clear located-row attribute, remove locate buttons, and remove the inbox widget
  window.__jetskiClosedChatsFilterCleanup();
  assert.strictEqual(targetSidebarRow.hasAttribute('data-jetski-located-row'), false);
  assert(!titlebarToolbar.querySelector('.jetski-titlebar-context-badge'));
  assert(!titlebarToolbar.querySelector('.jetski-chat-locate-btn'));
  // Verify the Needs-Reaction inbox widget is removed from the DOM on cleanup
  assert(!bodyEl.querySelector('.jetski-needs-reaction-inbox'));

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

  def test_hidden_model_picker_enrichment(self) -> None:
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
    this.style = {};
    this.attributes = new Map();
    this.children = [];
    this.parentElement = null;
    this.listeners = new Map();
    this._textContent = '';
    this._innerHTML = '';
  }
  get textContent() { return this._textContent; }
  set textContent(v) { this._textContent = String(v); }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(v) { this._innerHTML = String(v); }
  setAttribute(k, v) {
    this.attributes.set(k, String(v));
    if (k === 'id') this.id = String(v);
    if (k === 'class') this.className = String(v);
  }
  getAttribute(k) { return this.attributes.has(k) ? this.attributes.get(k) : null; }
  hasAttribute(k) { return this.attributes.has(k); }
  removeAttribute(k) { this.attributes.delete(k); }
  appendChild(c) { c.parentElement = this; this.children.push(c); return c; }
  insertBefore(c, ref) { c.parentElement = this; this.children.push(c); return c; }
  remove() {
    if (!this.parentElement) return;
    const i = this.parentElement.children.indexOf(this);
    if (i !== -1) this.parentElement.children.splice(i, 1);
    this.parentElement = null;
  }
  addEventListener(t, fn) {
    if (!this.listeners.has(t)) this.listeners.set(t, []);
    this.listeners.get(t).push(fn);
  }
  closest() { return null; }
  querySelector(s) { return this.querySelectorAll(s)[0] || null; }
  querySelectorAll(s) {
    if (typeof s === 'string' && s.includes(',')) {
      return s.split(',').flatMap((p) => this.querySelectorAll(p.trim()));
    }
    const out = [];
    const visit = (n) => {
      for (const c of n.children) {
        if (s === 'button[aria-label="Display Options"]' &&
            c.tagName === 'BUTTON' &&
            c.getAttribute('aria-label') === 'Display Options') {
          out.push(c);
        } else if (s.startsWith('#') && c.id === s.slice(1)) {
          out.push(c);
        }
        visit(c);
      }
    };
    visit(this);
    return out;
  }
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

// Freeze clock before fixture evaluation to prevent resetTime expiration
const NOW_ISO = '2026-10-29T18:30:00Z';
Date.now = () => Date.parse(NOW_ISO);

const protoMarker = { __isBufProto: true };
function protoObj(typeName, fields) {
  return Object.assign(Object.create(protoMarker), { $typeName: typeName, ...fields });
}

function makeNativeConfig(label, enumVal, modelId, remaining = 0.95) {
  return protoObj('exa.codeium_common_pb.ClientModelConfig', {
    label,
    modelOrAlias: protoObj('exa.codeium_common_pb.ModelOrAlias', {
      choice: { case: 'model', value: enumVal },
    }),
    creditMultiplier: 0,
    pricingType: 0,
    disabled: false,
    supportsImages: true,
    supportsLegacy: false,
    isPremium: false,
    betaWarningMessage: '',
    isBeta: false,
    provider: 0,
    isRecommended: true,
    allowedTiers: [1, 2],
    description: '',
    quotaInfo: protoObj('exa.codeium_common_pb.QuotaInfo', {
      remainingFraction: remaining,
      resetTime: protoObj('google.protobuf.Timestamp', {
        seconds: 1793447031n,
        nanos: 0,
      }),
    }),
    tagTitle: '',
    tagDescription: '',
    supportedMimeTypes: { 'image/png': true },
    supportsThoughtCirculation: false,
    modelUrl: '',
    modelId,
    contextWindowOptions: [],
  });
}

function makeServerUserStatus(proRemaining = 0.989) {
  const nativeConfigs = [
    makeNativeConfig('Gemini Pro Next', 1260, 'auto', proRemaining),
    makeNativeConfig('Gemini 3.5 Flash Lite', 1198, 'gemini-3.5-flash-lite', 0.998),
    makeNativeConfig('Claude Opus 5.5 (Max)', 1371, 'opus-5.5-max', 0.56),
    makeNativeConfig('Claude Opus 5.5 (High)', 1370, 'opus-5.5-high', 0.56),
  ];
  return protoObj('exa.codeium_common_pb.UserStatus', {
    cascadeModelConfigData: protoObj('exa.codeium_common_pb.CascadeModelConfigData', {
      clientModelConfigs: nativeConfigs,
      clientModelSorts: [
        protoObj('exa.codeium_common_pb.ClientModelSort', {
          name: 'Recommended',
          groups: [
            protoObj('exa.codeium_common_pb.ClientModelGroup', {
              groupName: '',
              modelLabels: nativeConfigs.map((c) => c.label),
            }),
          ],
        }),
      ],
      battleModeModelConfigs: [],
      battleModeModelSorts: [],
    }),
  });
}

const availableModelsPayload = {
  response: {
    models: {
      'auto': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M260',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.989, resetTime: '2026-10-30T11:43:51Z' },
      },
      'opus-5.5-high': {
        displayName: 'Claude Opus 5.5 (High)',
        model: 'MODEL_PLACEHOLDER_M370',
        apiProvider: 'API_PROVIDER_ANTHROPIC_VERTEX',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.56, resetTime: '2026-10-30T12:00:00Z' },
      },
      'glm-5.2-fp8-bon-arm': {
        displayName: 'GLM 5.2',
        model: 'MODEL_PLACEHOLDER_M252',
        apiProvider: 'API_PROVIDER_OPENAI_VERTEX',
        supportsImages: false,
        supportedMimeTypes: { 'text/plain': true },
        quotaInfo: { remainingFraction: 1, resetTime: '2026-10-30T12:30:00Z' },
      },
      'kimi-k3-maas-bon-arm': {
        displayName: 'Kimi K3',
        model: 'MODEL_PLACEHOLDER_M284',
        apiProvider: 'API_PROVIDER_OPENAI_VERTEX',
        supportsImages: false,
        quotaInfo: { remainingFraction: 0.85, resetTime: '2026-10-30T12:30:00Z' },
      },
      'sonnet-5.5-bon-le': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M377',
        apiProvider: 'API_PROVIDER_ANTHROPIC_VERTEX',
        supportsImages: true,
        supportedMimeTypes: { 'image/png': true },
        quotaInfo: { remainingFraction: 0.72, resetTime: '2026-10-30T12:30:03Z' },
      },
      'sonnet-5.5-high-bon-le': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M420',
        apiProvider: 'API_PROVIDER_ANTHROPIC_VERTEX',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.72, resetTime: '2026-10-30T12:30:03Z' },
      },
      'opus-5-bon-le': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M324',
        apiProvider: 'API_PROVIDER_ANTHROPIC_VERTEX',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.91, resetTime: '2026-10-30T12:30:03Z' },
      },
      'opus-5.5-max-bon-le': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M368',
        apiProvider: 'API_PROVIDER_ANTHROPIC_VERTEX',
        supportsImages: true,
        quotaInfo: { remainingFraction: 1, resetTime: '2026-10-30T12:30:03Z' },
      },
      'bon-experiment-arm-226': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M226',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.989 },
      },
      'gemini-2.5-flash': {
        displayName: 'Gemini 3.5 Flash Lite',
        model: 'MODEL_GOOGLE_GEMINI_2_5_FLASH',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.998, resetTime: '2026-10-30T14:00:00Z' },
      },
      'deepseek-r2-bon-arm': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M490',
        apiProvider: 'API_PROVIDER_OPENAI_VERTEX',
        supportsImages: false,
        quotaInfo: { remainingFraction: 0.5, resetTime: '2026-10-30T15:00:00Z' },
      },
      // Junk / FIM / image / latency-trap / duplicate-alias entries that MUST be filtered out
      'tab_flash_lite': {
        model: 'MODEL_GOOGLE_GEMINI_INTERNAL_TAB_FLASH_LITE',
        apiProvider: 'API_PROVIDER_GOOGLE_EVERGREEN',
      },
      'tab_flash_lite_preview': {
        model: 'MODEL_PLACEHOLDER_M19',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
      },
      'chat_20706': {
        model: 'MODEL_CHAT_20706',
        apiProvider: 'API_PROVIDER_INTERNAL',
      },
      'gemini-3.1-flash-image': {
        displayName: 'Gemini 3.1 Flash Image',
        model: 'MODEL_PLACEHOLDER_M21',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
      },
      'models/proactive-observer-v10': {
        model: 'MODEL_PLACEHOLDER_M150',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
      },
      'le-01-experiment-arm': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M80',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
      },
      'bon-experiment-arm-232': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M232',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
      },
      'bon-experiment-arm-233': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M233',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
      },
      'bon-experiment-arm-234': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M234',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
      },
      'bon-experiment-arm-235': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M235',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
      },
      'fable-5.1-dropdown': {
        displayName: 'Claude Fable 5.1 (High)',
        model: 'MODEL_PLACEHOLDER_M388',
        apiProvider: 'API_PROVIDER_ANTHROPIC_VERTEX',
      },
      'gemini-4-carbon': {
        displayName: 'Gemini 4 Carbon (High)',
        model: 'MODEL_PLACEHOLDER_M439',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
      },
      'gemini-launch-candidate': {
        displayName: 'Gemini 4 Argon (High)',
        model: 'MODEL_PLACEHOLDER_M393',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
      },
      'gemini-opt-out': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M94',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
      },
      'gemini-3.8-flash-tiered': {
        model: 'MODEL_PLACEHOLDER_M322',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
      },
      'unknown-enum-model': {
        displayName: 'Mystery Enum',
        model: 'MODEL_COMPLETELY_UNMAPPED_XYZ',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
      },
    },
    tabModelIds: ['chat_20706', 'tab_flash_lite_preview', 'tab_flash_lite'],
    imageGenerationModelIds: ['gemini-3.1-flash-image'],
    audioTranscriptionModelIds: ['models/proactive-observer-v10'],
    deprecatedModelIds: { 'le-01-experiment-arm': { oldModelEnum: 'MODEL_PLACEHOLDER_M80' } },
  },
};

const storeState = {
  trajectorySummaries: { summaries: {} },
  userStatus: makeServerUserStatus(0.989),
};

let userStatusDispatches = 0;
const subscribers = [];
const reduxStore = {
  getState() { return storeState; },
  dispatch(action) {
    if (action?.type === 'userStatus/setUserStatus') {
      userStatusDispatches++;
      storeState.userStatus = action.payload;
      for (const fn of subscribers) fn();
    }
    return action;
  },
  subscribe(fn) {
    subscribers.push(fn);
    return () => {};
  },
};
displayOptionsBtn.__reactFiber$test = { memoizedProps: { store: reduxStore } };

let availableModelsFetches = 0;
global.window = {
  innerWidth: 1280,
  location: { pathname: '/', search: '' },
  __APP_CONFIG__: { csrfToken: 'csrf-models-test' },
  localStorage: { getItem: () => null, setItem: () => {} },
  fetch: async (url, opts) => {
    if (String(url).includes('GetAvailableModels')) {
      availableModelsFetches++;
      assert.strictEqual(opts?.method, 'POST');
      assert.strictEqual(opts?.headers?.['x-codeium-csrf-token'], 'csrf-models-test');
      return { ok: true, json: async () => availableModelsPayload };
    }
    return { ok: false, status: 404 };
  },
};
global.document = {
  documentElement: docEl,
  head: headEl,
  body: bodyEl,
  getElementById(id) { return docEl.querySelector(`#${id}`); },
  createElement(t) { return new FakeElement(t); },
  querySelector(s) { return docEl.querySelector(s); },
  querySelectorAll(s) { return docEl.querySelectorAll(s); },
  addEventListener() {},
  removeEventListener() {},
};
global.MutationObserver = class { observe() {} disconnect() {} };
global.requestAnimationFrame = () => 1;

(async () => {
  eval(code);
  await new Promise((r) => setImmediate(r));

  assert.strictEqual(availableModelsFetches, 1, 'Must fetch GetAvailableModels on initial hydrate');
  assert.strictEqual(
    userStatusDispatches,
    1,
    'Must dispatch enriched userStatus once after hydrate'
  );

  const data = storeState.userStatus.cascadeModelConfigData;
  const configs = data.clientModelConfigs;
  const labels = data.clientModelSorts[0].groups[0].modelLabels;

  // Auto is placed first, followed immediately by strongest models (Opus 5.5)
  assert.strictEqual(labels[0], 'Auto');
  assert.deepStrictEqual(labels.slice(0, 4), [
    'Auto',
    'Claude Opus 5.5 BoN (Max)',
    'Claude Opus 5.5 (High)',
    'Claude Opus 5.5 (Max)',
  ]);

  // Every label in clientModelConfigs and modelLabels must be unique
  assert.strictEqual(new Set(configs.map((c) => c.label)).size, configs.length);
  assert.strictEqual(new Set(labels).size, labels.length);
  assert.strictEqual(labels.length, configs.length);

  const byModelId = new Map(configs.map((c) => [c.modelId, c]));

  // Verify native models are enriched with live quota percentage badges
  const nativeAuto = byModelId.get('auto');
  assert(nativeAuto, 'auto must be present in configs');
  assert.strictEqual(nativeAuto.tagTitle, '99%');
  assert(nativeAuto.tagDescription.includes('99% quota left'));
  assert(nativeAuto.tagDescription.includes('auto'));

  assert.strictEqual(byModelId.get('gemini-3.5-flash-lite')?.tagTitle, '100%');
  assert.strictEqual(byModelId.get('opus-5.5-max')?.tagTitle, '56%');
  assert.strictEqual(byModelId.get('opus-5.5-high')?.tagTitle, '56%');

  const glm = byModelId.get('glm-5.2-fp8-bon-arm');
  assert(glm, 'GLM 5.2 must be unlocked');
  assert.strictEqual(glm.label, 'GLM 5.2');
  assert.strictEqual(glm.tagTitle, '3P · 100%');
  assert(glm.tagDescription.includes('100% quota left'));
  assert(glm.tagDescription.includes('glm-5.2-fp8-bon-arm'));
  assert.strictEqual(glm.$typeName, 'exa.codeium_common_pb.ClientModelConfig');
  assert.strictEqual(glm.__isBufProto, true, 'Must preserve ClientModelConfig prototype');
  assert.deepStrictEqual(glm.modelOrAlias.choice, { case: 'model', value: 1252 });
  assert.strictEqual(glm.quotaInfo.$typeName, 'exa.codeium_common_pb.QuotaInfo');
  assert.strictEqual(glm.quotaInfo.remainingFraction, 1);
  assert.strictEqual(typeof glm.quotaInfo.resetTime.seconds, 'bigint');
  assert.strictEqual(glm.quotaInfo.resetTime.$typeName, 'google.protobuf.Timestamp');

  assert.strictEqual(byModelId.get('kimi-k3-maas-bon-arm')?.label, 'Kimi K3');
  assert.strictEqual(byModelId.get('kimi-k3-maas-bon-arm')?.tagTitle, '3P · 85%');
  assert.strictEqual(byModelId.get('sonnet-5.5-bon-le')?.label, 'Claude Sonnet 5.5 (Max)');
  assert.strictEqual(byModelId.get('sonnet-5.5-bon-le')?.tagTitle, 'BoN · 72%');
  assert.strictEqual(byModelId.get('sonnet-5.5-high-bon-le')?.label, 'Claude Sonnet 5.5 (High)');
  assert.strictEqual(byModelId.get('sonnet-5.5-high-bon-le')?.tagTitle, 'BoN · 72%');
  assert.strictEqual(byModelId.get('opus-5-bon-le')?.label, 'Claude Opus 5 (High)');
  assert.strictEqual(byModelId.get('opus-5-bon-le')?.tagTitle, 'BoN · 91%');
  assert.strictEqual(byModelId.get('opus-5.5-max-bon-le')?.label, 'Claude Opus 5.5 BoN (Max)');
  assert.strictEqual(byModelId.get('opus-5.5-max-bon-le')?.tagTitle, 'BoN · 100%');
  assert.strictEqual(byModelId.get('bon-experiment-arm-226')?.label, 'Gemini 4 Carbon-G');
  assert.strictEqual(byModelId.get('bon-experiment-arm-226')?.tagTitle, 'Exp · 99%');
  assert.strictEqual(byModelId.get('bon-experiment-arm-226')?.quotaInfo?.resetTime, undefined);

  const flash25 = byModelId.get('gemini-2.5-flash');
  assert(flash25, 'Gemini 2.5 Flash must be unlocked with named enum 312');
  assert.strictEqual(flash25.label, 'Gemini 2.5 Flash');
  assert.strictEqual(flash25.tagTitle, '2.5 · 100%');
  assert.deepStrictEqual(flash25.modelOrAlias.choice, { case: 'model', value: 312 });

  const future = byModelId.get('deepseek-r2-bon-arm');
  assert(future, 'Unknown future hidden model must be unlocked via fallback');
  assert.notStrictEqual(future.label, 'Gemini Pro Next');
  assert(/deepseek/i.test(future.label), `Expected humanized fallback label, got: ${future.label}`);
  assert.strictEqual(future.tagTitle, 'Hidden · 50%');
  assert.deepStrictEqual(future.modelOrAlias.choice, { case: 'model', value: 1490 });

  // Verify junk, FIM, image, deprecated, duplicate-alias, and artificial-latency arms are excluded
  for (const blocked of [
    'tab_flash_lite',
    'tab_flash_lite_preview',
    'chat_20706',
    'gemini-3.1-flash-image',
    'models/proactive-observer-v10',
    'le-01-experiment-arm',
    'bon-experiment-arm-232',
    'bon-experiment-arm-233',
    'bon-experiment-arm-234',
    'bon-experiment-arm-235',
    'fable-5.1-dropdown',
    'gemini-4-carbon',
    'gemini-launch-candidate',
    'gemini-opt-out',
    'gemini-3.8-flash-tiered',
    'unknown-enum-model',
  ]) {
    assert(!byModelId.has(blocked), `Blocked model ${blocked} must not appear in picker`);
  }

  // Subsequent server UserStatus poll must synchronously enrich payload without extra dispatches
  const dispatchesBeforePoll = userStatusDispatches;
  reduxStore.dispatch({
    type: 'userStatus/setUserStatus',
    payload: makeServerUserStatus(0.777),
  });
  assert.strictEqual(userStatusDispatches, dispatchesBeforePoll + 1);
  const afterPollConfigs = storeState.userStatus.cascadeModelConfigData.clientModelConfigs;
  assert.strictEqual(afterPollConfigs[0].quotaInfo.remainingFraction, 0.777);
  assert.strictEqual(afterPollConfigs[0].tagTitle, '78%');
  assert(afterPollConfigs[0].tagDescription.includes('78% quota left'));
  assert(
    afterPollConfigs.some((c) => c.modelId === 'glm-5.2-fp8-bon-arm'),
    'Synchronous enrichment on poll must keep unlocked models without flicker'
  );

  // Re-injection idempotence: re-running the script must not duplicate unlocked entries
  const countBeforeReinject = afterPollConfigs.length;
  eval(code);
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(
    storeState.userStatus.cascadeModelConfigData.clientModelConfigs.length,
    countBeforeReinject,
    'Re-injection must not duplicate unlocked models'
  );

  // Cleanup must restore un-enriched server UserStatus cleanly
  window.__jetskiClosedChatsFilterCleanup();
  const restoredConfigs = storeState.userStatus.cascadeModelConfigData.clientModelConfigs;
  const restoredLabels =
    storeState.userStatus.cascadeModelConfigData.clientModelSorts[0].groups[0].modelLabels;
  assert.strictEqual(restoredConfigs.length, 4, 'Cleanup must restore original 4 native models');
  assert.strictEqual(restoredConfigs[0].quotaInfo.remainingFraction, 0.777);
  assert.strictEqual(restoredConfigs[0].tagTitle, '');
  assert.strictEqual(restoredConfigs[0].tagDescription, '');
  assert.deepStrictEqual(restoredLabels, [
    'Gemini Pro Next',
    'Gemini 3.5 Flash Lite',
    'Claude Opus 5.5 (Max)',
    'Claude Opus 5.5 (High)',
  ]);

  // Verify OneQuota tag is preserved as-is
  const oqConfig = makeNativeConfig('Gemini 3.1 Pro', 1333, 'gemini-3.1-pro', 0);
  oqConfig.tagTitle = 'OneQuota';
  oqConfig.tagDescription = 'Managed outside AICode quota';
  const oqStatus = protoObj('exa.codeium_common_pb.UserStatus', {
    cascadeModelConfigData: protoObj('exa.codeium_common_pb.CascadeModelConfigData', {
      clientModelConfigs: [oqConfig],
      clientModelSorts: [],
    }),
  });
  reduxStore.dispatch({ type: 'userStatus/setUserStatus', payload: oqStatus });
  const oqEnriched = storeState.userStatus.cascadeModelConfigData.clientModelConfigs[0];
  assert.strictEqual(oqEnriched.tagTitle, 'OneQuota');
  assert.strictEqual(oqEnriched.tagDescription, 'Managed outside AICode quota');
  window.__jetskiClosedChatsFilterCleanup();

  console.log('MODEL_PICKER_ENRICHMENT_PASSED');
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
        f"Model picker harness failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )
    self.assertIn("MODEL_PICKER_ENRICHMENT_PASSED", res.stdout)

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

  def test_context_volume_badge_thresholds_paging_and_tooltip(self) -> None:
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
    this._textContent = '';
    this._innerHTML = '';
    this.style = {};
    this.attributes = new Map();
    this.children = [];
    this.parentElement = null;
    this.listeners = new Map();
  }
  get textContent() {
    if (this.children.length > 0) {
      return this._textContent + this.children.map((c) => c.textContent).join('');
    }
    return this._textContent;
  }
  set textContent(val) {
    this._textContent = String(val);
    for (const c of this.children) c.parentElement = null;
    this.children = [];
  }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(val) { this._innerHTML = String(val); }
  setAttribute(k, v) {
    this.attributes.set(k, String(v));
    if (k === 'id') this.id = String(v);
    if (k === 'class') this.className = String(v);
  }
  getAttribute(k) {
    if (k === 'id' && this.id) return this.id;
    return this.attributes.has(k) ? this.attributes.get(k) : null;
  }
  hasAttribute(k) { return this.attributes.has(k); }
  removeAttribute(k) { this.attributes.delete(k); }
  appendChild(child) {
    if (child.parentElement) child.remove();
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  insertBefore(child, ref) {
    if (child === ref) return child;
    if (child.parentElement) child.remove();
    child.parentElement = this;
    const idx = this.children.indexOf(ref);
    if (idx === -1) this.children.push(child);
    else this.children.splice(idx, 0, child);
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
    const evt = {
      type,
      target: this,
      button: 0,
      shiftKey: false,
      ctrlKey: false,
      metaKey: false,
      ...eventInit,
      preventDefault() {},
      stopPropagation() {},
    };
    for (const fn of (this.listeners.get(type) || [])) fn(evt);
  }
  getBoundingClientRect() {
    return { left: 920, top: 12, bottom: 32, right: 1040, width: 120, height: 20 };
  }
  closest(selector) {
    let cur = this;
    while (cur) {
      if (matches(cur, selector)) return cur;
      cur = cur.parentElement;
    }
    return null;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
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
  if (selector === 'button[aria-label="Display Options"]') {
    return el.tagName === 'BUTTON' && el.getAttribute('aria-label') === 'Display Options';
  }
  const attrEqMatch = /^\[([-a-zA-Z0-9_]+)="([^"]+)"\]$/.exec(selector);
  if (attrEqMatch) return el.getAttribute(attrEqMatch[1]) === attrEqMatch[2];
  if (selector.startsWith('.')) {
    const classes = selector.split('.').filter(Boolean);
    const elClasses = el.className ? el.className.split(/\s+/) : [];
    return classes.every((cls) => elClasses.includes(cls));
  }
  if (selector.startsWith('#')) return el.id === selector.slice(1);
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

const titlebarActive = new FakeElement('div');
const moreActiveBtn = new FakeElement('button');
moreActiveBtn.setAttribute('data-testid', 'titlebar-more-actions');
moreActiveBtn.__reactFiber$t = { return: { memoizedProps: { cascadeId: 'chat-active' } } };
titlebarActive.appendChild(moreActiveBtn);
bodyEl.appendChild(titlebarActive);

const titlebarEmpty = new FakeElement('div');
const moreEmptyBtn = new FakeElement('button');
moreEmptyBtn.setAttribute('data-testid', 'titlebar-more-actions');
moreEmptyBtn.__reactFiber$t = { return: { memoizedProps: { cascadeId: 'chat-empty' } } };
titlebarEmpty.appendChild(moreEmptyBtn);
bodyEl.appendChild(titlebarEmpty);

let fakeNow = 1000000;
Date.now = () => fakeNow;

const storeState = {
  trajectorySummaries: {
    summaries: {
      'chat-active': {
        summary: '[12:00] ⦿ Context badge test',
        status: 'CASCADE_RUN_STATUS_IDLE',
        notFullyIdle: false,
        stepCount: 4,
        lastModifiedTime: '2026-10-29T16:00:00Z',
        annotations: { title: '[12:00] ⦿ Context badge test', archived: false },
      },
      'chat-empty': {
        summary: '[12:01] ⦿ Brand new chat',
        status: 'CASCADE_RUN_STATUS_IDLE',
        notFullyIdle: false,
        stepCount: 0,
        lastModifiedTime: '2026-10-29T16:01:00Z',
        annotations: { title: '[12:01] ⦿ Brand new chat', archived: false },
      },
    },
  },
  conversation: { cascadeId: 'chat-active' },
};

const subscribers = [];
const reduxStore = {
  getState: () => storeState,
  dispatch(action) {
    if (action?.type === 'trajectorySummaries/applyStreamBatch') {
      const next = { ...storeState.trajectorySummaries.summaries };
      for (const [id, s] of (action.payload?.updates || [])) next[id] = s;
      storeState.trajectorySummaries = { summaries: next };
    }
    for (const sub of subscribers) sub();
    return action;
  },
  subscribe(fn) {
    subscribers.push(fn);
    return () => {};
  },
};
displayOptionsBtn.__reactFiber$test = { memoizedProps: { store: reduxStore } };

function makeEntry(used, max, ckpt, breakdown, modelOpts) {
  const csm = {
    contextWindowMetadata: {
      estimatedTokensUsed: used,
      maxContextTokens: max,
      ...(breakdown ? { tokenBreakdown: { groups: breakdown, totalTokens: used } } : {}),
    },
  };
  if (ckpt !== undefined) csm.checkpointIndex = ckpt;
  return {
    chatModel: {
      chatStartMetadata: csm,
      ...(modelOpts || {}),
    },
  };
}

let activeGmList = [
  makeEntry(32000, 256000, -1),
  makeEntry(85000, 256000, -1, [
    { name: 'System Prompt', type: 'TOKEN_TYPE_SYSTEM_PROMPT', numTokens: 40000 },
    { name: 'Tools', type: 'TOKEN_TYPE_TOOLS', numTokens: 11000 },
    { name: 'Chat Messages', type: 'TOKEN_TYPE_CHAT_MESSAGES', numTokens: 34000 },
  ]),
  // Trailing entry with explicit null estimatedTokensUsed (S2) and empty metadata (B2)
  {
    chatModel: {
      chatStartMetadata: {
        contextWindowMetadata: { estimatedTokensUsed: null, maxContextTokens: 256000 },
      },
    },
  },
  { chatModel: { chatStartMetadata: {} } },
];

let failNextActiveFetch = true;
const gmCalls = [];
let observerCallback = null;
let rafCallbacks = [];

global.window = {
  innerWidth: 1280,
  location: { pathname: '/c/chat-active', search: '' },
  __APP_CONFIG__: { csrfToken: 'ctx-csrf' },
  localStorage: { getItem: () => null, setItem: () => {} },
  fetch: async (url, opts) => {
    if (String(url).includes('GetCascadeTrajectoryGeneratorMetadata')) {
      const body = JSON.parse(opts.body);
      gmCalls.push({ url: String(url), headers: opts.headers, body });
      if (body.cascadeId === 'chat-active' && failNextActiveFetch) {
        failNextActiveFetch = false;
        return { ok: false, status: 503 };
      }
      if (body.cascadeId === 'chat-second') {
        return {
          ok: true,
          json: async () => ({ generatorMetadata: [makeEntry(200000, 256000, -1)] }),
        };
      }
      if (body.cascadeId === 'chat-empty') {
        return { ok: true, json: async () => ({ generatorMetadata: [] }) };
      }
      const off = Number(body.generatorMetadataOffset) || 0;
      return {
        ok: true,
        json: async () => ({ generatorMetadata: activeGmList.slice(off) }),
      };
    }
    if (String(url).includes('jetski-chains.json')) {
      return { ok: true, json: async () => ({ parentOf: {}, createdAt: {}, redirects: {} }) };
    }
    if (String(url).includes('jetski-roadmaps.json')) {
      return { ok: true, json: async () => ({ version: 1, projects: {} }) };
    }
    return { ok: false, status: 404 };
  },
};

global.document = {
  documentElement: docEl,
  head: headEl,
  body: bodyEl,
  getElementById: (id) => docEl.querySelector(`#${id}`),
  createElement: (tag) => new FakeElement(tag),
  querySelector: (sel) => docEl.querySelector(sel),
  querySelectorAll: (sel) => docEl.querySelectorAll(sel),
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent() {},
};
global.MutationObserver = class {
  constructor(cb) { observerCallback = cb; }
  observe() {}
  disconnect() {}
};
global.requestAnimationFrame = (cb) => {
  rafCallbacks.push(cb);
  return rafCallbacks.length;
};

let nextTimerId = 1;
const pendingTimers = new Map();
global.setTimeout = (fn, ms) => {
  const id = nextTimerId++;
  pendingTimers.set(id, { fn, due: fakeNow + (Number(ms) || 0) });
  return id;
};
global.clearTimeout = (id) => {
  pendingTimers.delete(id);
};

const flushRaf = () => {
  while (rafCallbacks.length > 0) rafCallbacks.shift()();
};

const flushAsync = async () => {
  for (let round = 0; round < 4; round++) {
    flushRaf();
    for (let i = 0; i < 6; i++) await Promise.resolve();
  }
  flushRaf();
};

const advanceFakeTime = async (ms) => {
  fakeNow += ms;
  for (const [id, t] of [...pendingTimers.entries()]) {
    if (t.due <= fakeNow) {
      pendingTimers.delete(id);
      t.fn();
    }
  }
  await flushAsync();
};

eval(code);

(async () => {
  await flushAsync();

  const activeBadge = titlebarActive.querySelector('.jetski-titlebar-context-badge');
  const emptyBadge = titlebarEmpty.querySelector('.jetski-titlebar-context-badge');
  assert(activeBadge, 'Active titlebar must contain context badge');
  assert(emptyBadge, 'Empty titlebar must mount hidden context badge placeholder');
  assert.strictEqual(emptyBadge.style.display, 'none');
  assert.strictEqual(emptyBadge.getAttribute('data-empty'), 'true');

  // B4a: Initial transient 503 hides badge on mount, then recovers on next UI sync after 1200ms
  assert.strictEqual(activeBadge.style.display, 'none');
  gmCalls.length = 0;
  fakeNow += 2000;
  reduxStore.dispatch({ type: 'NOOP' });
  await flushAsync();
  assert.strictEqual(gmCalls.length, 1, 'Transient 503 must retry on later UI sync');
  const firstActiveCall = gmCalls[0];
  assert.deepStrictEqual(firstActiveCall.body, {
    cascadeId: 'chat-active',
    generatorMetadataOffset: 0,
    includeMessages: false,
  });
  assert.strictEqual(firstActiveCall.headers['x-codeium-csrf-token'], 'ctx-csrf');

  // S2: Explicit null estimatedTokensUsed is skipped so valid 85k entry is rendered
  assert.strictEqual(activeBadge.style.display, '');
  assert.strictEqual(activeBadge.getAttribute('data-empty'), 'false');
  assert.strictEqual(activeBadge.getAttribute('data-level'), 'ok');
  assert.strictEqual(activeBadge.getAttribute('data-compacted'), 'false');
  assert.strictEqual(activeBadge.textContent, '85k / 256k · 33%');

  // Idle syncs with unchanged summary signature must not re-fetch
  gmCalls.length = 0;
  fakeNow += 10000;
  reduxStore.dispatch({ type: 'NOOP' });
  await flushAsync();
  assert.strictEqual(gmCalls.length, 0, 'Unchanged idle chat must not poll LS');

  // Mutations on the badge itself must be ignored by MutationObserver
  rafCallbacks = [];
  observerCallback([{ target: activeBadge }]);
  assert.strictEqual(rafCallbacks.length, 0);

  const bumpActiveSummary = (stepCount, status = 'CASCADE_RUN_STATUS_IDLE') => {
    reduxStore.dispatch({
      type: 'trajectorySummaries/applyStreamBatch',
      payload: {
        updates: [[
          'chat-active',
          {
            ...storeState.trajectorySummaries.summaries['chat-active'],
            stepCount,
            status,
            lastModifiedTime: `2026-10-29T16:00:${String(stepCount).padStart(2, '0')}Z`,
          },
        ]],
        deletes: [],
      },
    });
    flushRaf();
  };

  // B2: Update while trailing entries lack metadata anchors offset at last valid index (1 -> 1 RPC)
  gmCalls.length = 0;
  fakeNow += 2000;
  bumpActiveSummary(4, 'CASCADE_RUN_STATUS_IDLE');
  await flushAsync();
  assert.strictEqual(gmCalls.length, 1, 'Trailing empty metadata must stay paged in 1 RPC');
  assert.strictEqual(gmCalls[0].body.generatorMetadataOffset, 1);

  // Near-threshold 59.96% (153500 / 256000) must stay ok and display 59%, never 60%
  gmCalls.length = 0;
  activeGmList.push(makeEntry(153500, 256000, -1));
  fakeNow += 2000;
  bumpActiveSummary(5);
  await flushAsync();
  assert.strictEqual(gmCalls.length, 1);
  assert.strictEqual(gmCalls[0].body.generatorMetadataOffset, 1);
  assert.strictEqual(activeBadge.getAttribute('data-level'), 'ok');
  assert.strictEqual(activeBadge.textContent, '154k / 256k · 59%');

  // Burst updates within CONTEXT_MIN_FETCH_INTERVAL_MS (1200ms) coalesce into 1 timer fetch
  gmCalls.length = 0;
  activeGmList.push(makeEntry(155000, 256000, -1));
  fakeNow += 300;
  bumpActiveSummary(6);
  await flushAsync();
  activeGmList.push(makeEntry(158720, 256000, -1));
  fakeNow += 200;
  bumpActiveSummary(7);
  await flushAsync();
  assert.strictEqual(gmCalls.length, 0, 'Rapid burst within 1200ms must defer to timer');
  assert.strictEqual(pendingTimers.size, 1);

  // Advancing past 1200ms fires the single coalesced paged fetch and enters soft (>= 60%)
  await advanceFakeTime(1000);
  assert.strictEqual(gmCalls.length, 1);
  assert.strictEqual(gmCalls[0].body.generatorMetadataOffset, 4);
  assert.strictEqual(activeBadge.getAttribute('data-level'), 'soft');
  assert.strictEqual(activeBadge.textContent, '159k / 256k · 62%');

  // Turn-start threshold (68% >= 65%)
  gmCalls.length = 0;
  activeGmList.push(makeEntry(174080, 256000, -1));
  fakeNow += 2000;
  bumpActiveSummary(8);
  await flushAsync();
  assert.strictEqual(activeBadge.getAttribute('data-level'), 'turn-start');
  assert.strictEqual(activeBadge.textContent, '174k / 256k · 68%');

  // Running chat polls after 4000ms even during silent thinking with zero DOM/Redux events
  gmCalls.length = 0;
  fakeNow += 2000;
  bumpActiveSummary(9, 'CASCADE_RUN_STATUS_RUNNING');
  await flushAsync();
  assert.strictEqual(gmCalls.length, 1);
  assert.strictEqual(pendingTimers.size, 1, 'Open running chat must arm 4000ms refresh timer');
  gmCalls.length = 0;
  activeGmList.push(makeEntry(212480, 256000, -1));
  await advanceFakeTime(4000);
  assert.strictEqual(gmCalls.length, 1, 'Running chat must refresh after 4000ms');
  assert.strictEqual(activeBadge.getAttribute('data-level'), 'hard');
  assert.strictEqual(activeBadge.textContent, '212k / 256k · 83%');

  // B1: Fast completion (< 1200ms) while 4s poll timer is armed must keep final count
  gmCalls.length = 0;
  activeGmList.push(makeEntry(220000, 256000, -1));
  fakeNow += 500;
  bumpActiveSummary(10, 'CASCADE_RUN_STATUS_IDLE');
  await flushAsync();
  await advanceFakeTime(1200);
  assert.strictEqual(gmCalls.length, 1, 'End-of-turn transition within 1200ms must still fetch');
  assert.strictEqual(activeBadge.textContent, '220k / 256k · 86%');

  // Rewound trajectory + proto3 omitted checkpointIndex=0 (compacted at 40% -> hard)
  gmCalls.length = 0;
  activeGmList = [
    makeEntry(102400, 256000, undefined, [
      { name: 'System Prompt', type: 'TOKEN_TYPE_SYSTEM_PROMPT', numTokens: 40000 },
      { name: 'Tools', type: 'TOKEN_TYPE_TOOLS', numTokens: 12400 },
      { name: 'Chat Messages', type: 'TOKEN_TYPE_CHAT_MESSAGES', numTokens: 50000 },
    ]),
  ];
  fakeNow += 2000;
  bumpActiveSummary(2);
  await flushAsync();
  assert.deepStrictEqual(
    gmCalls.map((c) => c.body.generatorMetadataOffset),
    [9, 0],
    'Empty paged slice after rewind must fall back to offset 0'
  );
  assert.strictEqual(activeBadge.getAttribute('data-level'), 'hard');
  assert.strictEqual(activeBadge.getAttribute('data-compacted'), 'true');
  assert.strictEqual(activeBadge.textContent, '⟳ 102k / 256k · 40%');

  // Hover tooltip shows exact breakdown, free tokens, compaction, and thresholds
  activeBadge.dispatchEvent('mouseenter');
  const bubble = global.document.getElementById('jetski-closed-chats-filter-bubble');
  assert(bubble, 'Shared hover bubble must exist');
  assert.strictEqual(bubble.getAttribute('data-visible'), 'true');
  assert.strictEqual(bubble.getAttribute('data-mode'), 'context');
  assert(bubble.innerHTML.includes('102,400 / 256,000 tokens'));
  assert(bubble.innerHTML.includes('153,600 free'));
  assert(bubble.innerHTML.includes('System Prompt'));
  assert(bubble.innerHTML.includes('40,000'));
  assert(bubble.innerHTML.includes('Tools'));
  assert(bubble.innerHTML.includes('12,400'));
  assert(bubble.innerHTML.includes('Chat Messages'));
  assert(bubble.innerHTML.includes('50,000'));
  assert(bubble.innerHTML.includes('Compacted'));
  assert(bubble.innerHTML.includes('Soft 60%'));
  assert(bubble.innerHTML.includes('Turn-start 65%'));
  assert(bubble.innerHTML.includes('Hard 80%'));
  assert(!/[А-Яа-яЁё]/.test(bubble.innerHTML), 'Tooltip must be English-only');

  // B3: In-place cascadeId switch while hovered refreshes open tooltip to the new conversation
  moreEmptyBtn.__reactFiber$t.return.memoizedProps.cascadeId = 'chat-second';
  reduxStore.dispatch({ type: 'NOOP' });
  await flushAsync();
  moreActiveBtn.__reactFiber$t.return.memoizedProps.cascadeId = 'chat-second';
  reduxStore.dispatch({ type: 'NOOP' });
  await flushAsync();
  assert(
    bubble.innerHTML.includes('200,000 / 256,000 tokens'),
    'Hovered tooltip must track cascadeId switch'
  );
  // B4b: Bounded cache evicts oldest idle entry past CONTEXT_CACHE_MAX_ENTRIES (64)
  for (let i = 0; i < 66; i++) {
    moreEmptyBtn.__reactFiber$t.return.memoizedProps.cascadeId = `chat-lru-${i}`;
    reduxStore.dispatch({ type: 'NOOP' });
    await flushAsync();
  }
  gmCalls.length = 0;
  moreEmptyBtn.__reactFiber$t.return.memoizedProps.cascadeId = 'chat-lru-0';
  reduxStore.dispatch({ type: 'NOOP' });
  await flushAsync();
  assert.strictEqual(gmCalls.length, 1, 'Evicted oldest entry must re-fetch on revisit');

  moreActiveBtn.__reactFiber$t.return.memoizedProps.cascadeId = 'chat-active';
  moreEmptyBtn.__reactFiber$t.return.memoizedProps.cascadeId = 'chat-empty';
  reduxStore.dispatch({ type: 'NOOP' });
  await flushAsync();

  activeBadge.dispatchEvent('mouseleave');
  assert.strictEqual(bubble.getAttribute('data-visible'), 'false');

  // Clicking the badge forces an immediate refresh even within throttle interval
  gmCalls.length = 0;
  activeGmList.push(makeEntry(110000, 256000, 1));
  fakeNow += 400;
  activeBadge.dispatchEvent('pointerdown', { button: 0 });
  await flushAsync();
  assert.strictEqual(gmCalls.length, 1);
  assert.strictEqual(activeBadge.textContent, '⟳ 110k / 256k · 43%');

  // R71.3: Gemini Pro Orchestrator planning turn surfaces router arm and underlying Barium-B
  gmCalls.length = 0;
  activeGmList.push(
    makeEntry(112000, 256000, 1, undefined, {
      model: 'MODEL_PLACEHOLDER_M415',
      modelDisplayName: 'Gemini Pro Orchestrator',
      responseModel:
        'gemini-pro-coordinator-router-plan|barium-b-jetski/tfhub/prod/ml-gemini-experimental/Gemini-5-M-IT-G1085-Rev27-Barium-B-EEVEE/1',
    })
  );
  fakeNow += 2000;
  bumpActiveSummary(11);
  await flushAsync();
  assert.strictEqual(activeBadge.textContent, '⟳ Orchestrator (Barium-B) · 112k / 256k · 44%');
  assert.strictEqual(activeBadge.getAttribute('data-actual-model'), 'Orchestrator (Barium-B)');

  // Tooltip displays Last turn and Backend checkpoint details
  activeBadge.dispatchEvent('mouseenter');
  assert(bubble.innerHTML.includes('Gemini Pro Orchestrator (M415)'));
  assert(bubble.innerHTML.includes('Barium-B'));
  assert(bubble.innerHTML.includes('Gemini-5-M-IT-G1085-Rev27'));
  assert(bubble.innerHTML.includes('gemini-pro-coordinator-router-plan'));
  activeBadge.dispatchEvent('mouseleave');

  // R71.3: Execution turn (M422 Flash router) surfaces Skimaki backend
  gmCalls.length = 0;
  activeGmList.push(
    makeEntry(115000, 256000, 1, undefined, {
      model: 'MODEL_PLACEHOLDER_M422',
      modelDisplayName: 'Gemini 3.8 Flash (router)',
      responseModel:
        'gemini-3.8-flash-router|skimaki-agy-fast/tfhub/prod/ml-gemini-bip/Gemini-4.1-S-IT-G1038-Rev25-Topo-Skimaki-EEVEE/2',
    })
  );
  fakeNow += 2000;
  bumpActiveSummary(12);
  await flushAsync();
  assert.strictEqual(activeBadge.textContent, '⟳ Flash 3.8 (Skimaki) · 115k / 256k · 45%');
  assert.strictEqual(activeBadge.getAttribute('data-actual-model'), 'Flash 3.8 (Skimaki)');

  // R71.3: Masked BoN model unmasks in pill and tooltip shows Next turn selected model
  storeState.modelSelection = {
    byConversation: { 'chat-active': { model: 1226 } },
  };
  gmCalls.length = 0;
  activeGmList.push(
    makeEntry(118000, 256000, 1, undefined, {
      model: 'MODEL_PLACEHOLDER_M380',
      modelDisplayName: 'Gemini Pro Next',
      responseModel: 'opus-5.5-high-bon-le|claude-opus-5-5@default',
    })
  );
  fakeNow += 2000;
  bumpActiveSummary(13);
  await flushAsync();
  assert.strictEqual(activeBadge.textContent, '⟳ Opus 5.5 BoN (High) · 118k / 256k · 46%');
  assert.strictEqual(activeBadge.getAttribute('data-actual-model'), 'Opus 5.5 BoN (High)');

  activeBadge.dispatchEvent('mouseenter');
  assert(bubble.innerHTML.includes('Claude Opus 5.5 BoN (High) (M380)'));
  assert(bubble.innerHTML.includes('opus-5.5-high-bon-le · claude-opus-5-5'));
  assert(bubble.innerHTML.includes('Next turn'));
  assert(bubble.innerHTML.includes('Gemini 4 Carbon-G'));
  activeBadge.dispatchEvent('mouseleave');

  // Manual pin surfaces "· Pinned" marker next to selected next turn model
  window.__jetskiManualPickedModels.set('chat-active', {
    model: 1226,
    atStep: 0,
    updatedAt: fakeNow,
  });
  activeBadge.dispatchEvent('mouseenter');
  assert(bubble.innerHTML.includes('Gemini 4 Carbon-G · Pinned'));
  activeBadge.dispatchEvent('mouseleave');

  // Cleanup cancels any pending throttle timer and removes all context badges
  fakeNow += 100;
  bumpActiveSummary(3);
  await flushAsync();
  assert.strictEqual(pendingTimers.size, 1);
  window.__jetskiClosedChatsFilterCleanup();
  assert.strictEqual(pendingTimers.size, 0, 'Cleanup must cancel pending throttle timers');
  assert(!titlebarActive.querySelector('.jetski-titlebar-context-badge'));
  assert(!titlebarEmpty.querySelector('.jetski-titlebar-context-badge'));

  console.log('CONTEXT_BADGE_ASSERTIONS_PASSED');
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
        f"Context badge JS harness failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )
    self.assertIn("CONTEXT_BADGE_ASSERTIONS_PASSED", res.stdout)

  def test_r22_blocked_steps_subagent_tree_and_coordinator_synthesis(self) -> None:
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
    this._textContent = '';
    this._innerHTML = '';
    this.style = {};
    this.attributes = new Map();
    this.children = [];
    this.parentElement = null;
    this.listeners = new Map();
  }
  get textContent() {
    if (this.children.length > 0) {
      return this._textContent + this.children.map((c) => c.textContent).join('');
    }
    return this._textContent;
  }
  set textContent(val) {
    this._textContent = String(val);
    for (const c of this.children) c.parentElement = null;
    this.children = [];
  }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(val) { this._innerHTML = String(val); }
  setAttribute(k, v) {
    this.attributes.set(k, String(v));
    if (k === 'id') this.id = String(v);
    if (k === 'class') this.className = String(v);
  }
  getAttribute(k) {
    if (k === 'id' && this.id) return this.id;
    return this.attributes.has(k) ? this.attributes.get(k) : null;
  }
  hasAttribute(k) { return this.attributes.has(k); }
  removeAttribute(k) { this.attributes.delete(k); }
  appendChild(child) {
    if (child.parentElement) child.remove();
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  insertBefore(child, ref) {
    if (child === ref) return child;
    if (child.parentElement) child.remove();
    child.parentElement = this;
    const idx = this.children.indexOf(ref);
    if (idx === -1) this.children.push(child);
    else this.children.splice(idx, 0, child);
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
    const evt = {
      type,
      target: this,
      button: 0,
      shiftKey: false,
      ctrlKey: false,
      metaKey: false,
      ...eventInit,
      preventDefault() {},
      stopPropagation() {},
    };
    for (const fn of (this.listeners.get(type) || [])) fn(evt);
  }
  getBoundingClientRect() {
    return { left: 24, top: 40, bottom: 62, right: 140, width: 116, height: 22 };
  }
  closest(selector) {
    let cur = this;
    while (cur) {
      if (matches(cur, selector)) return cur;
      cur = cur.parentElement;
    }
    return null;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
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
  const bucketMatch = /^button\[data-bucket="([^"]+)"\]$/.exec(selector);
  if (bucketMatch) {
    return el.tagName === 'BUTTON' && el.getAttribute('data-bucket') === bucketMatch[1];
  }
  if (selector === '[data-testid="conversation-row-sidebar"][data-cascade-id]') {
    return (
      el.getAttribute('data-testid') === 'conversation-row-sidebar' &&
      el.hasAttribute('data-cascade-id')
    );
  }
  const doubleAttrMatch =
    /^\[([-a-zA-Z0-9_]+)="([^"]+)"\]\[([-a-zA-Z0-9_]+)="([^"]+)"\]$/.exec(selector);
  if (doubleAttrMatch) {
    return (
      el.getAttribute(doubleAttrMatch[1]) === doubleAttrMatch[2] &&
      el.getAttribute(doubleAttrMatch[3]) === doubleAttrMatch[4]
    );
  }
  const attrEqMatch = /^\[([-a-zA-Z0-9_]+)="([^"]+)"\]$/.exec(selector);
  if (attrEqMatch) return el.getAttribute(attrEqMatch[1]) === attrEqMatch[2];
  if (selector.startsWith('.')) {
    const classes = selector.split('.').filter(Boolean);
    const elClasses = el.className ? el.className.split(/\s+/) : [];
    return classes.every((cls) => elClasses.includes(cls));
  }
  if (selector.startsWith('#')) return el.id === selector.slice(1);
  return false;
}

const docEl = new FakeElement('html');
const headEl = new FakeElement('head');
const bodyEl = new FakeElement('body');
docEl.appendChild(headEl);
docEl.appendChild(bodyEl);

const sidebarWrap = new FakeElement('div');
const actionRow = new FakeElement('div');
const displayOptionsBtn = new FakeElement('button');
displayOptionsBtn.setAttribute('aria-label', 'Display Options');
actionRow.appendChild(displayOptionsBtn);
sidebarWrap.appendChild(actionRow);

const sidebarListEl = new FakeElement('div');
sidebarListEl.setAttribute('data-testid', 'conversation-list-sidebar');
sidebarWrap.appendChild(sidebarListEl);
bodyEl.appendChild(sidebarWrap);

function makeSidebarRow(cascadeId, title) {
  const row = new FakeElement('div');
  row.setAttribute('data-testid', 'conversation-row-sidebar');
  row.setAttribute('data-cascade-id', cascadeId);
  const titleSpan = new FakeElement('span');
  titleSpan.className = 'truncate';
  titleSpan.textContent = title;
  const hoverBar = new FakeElement('div');
  const pinBtn = new FakeElement('button');
  pinBtn.setAttribute('data-testid', 'conversation-pin-button');
  hoverBar.appendChild(pinBtn);
  row.appendChild(titleSpan);
  row.appendChild(hoverBar);
  sidebarListEl.appendChild(row);
  return { row, titleSpan };
}

const { row: rowPermRunning, titleSpan: spanPermRunning } = makeSidebarRow(
  'chat-perm-running',
  '[14:00] ⦿ Clean scratch'
);
const { row: rowCoord, titleSpan: spanCoord } = makeSidebarRow(
  'chat-coord',
  '[14:20] ⦿ Multi-agent review'
);

const NOW_ISO = '2026-10-29T18:30:00Z';
let fakeNow = Date.parse(NOW_ISO);
Date.now = () => fakeNow;

const trajectoriesByCid = {
  'chat-wait-ask': {
    numTotalSteps: 3,
    trajectory: {
      steps: [
        { type: 14, status: 3 },
        {
          type: 138,
          status: 9,
          requestedInteraction: {
            interaction: {
              case: 'askQuestion',
              value: {
                questions: [{ question: 'Which branch should I audit?' }],
              },
            },
          },
        },
        // Parallel DONE VIEW_FILE step after WAITING must not mask the blocked step
        { type: 16, status: 3 },
      ],
    },
  },
  'chat-sub-parent': {
    numTotalSteps: 2,
    trajectory: {
      steps: [
        { type: 14, status: 3 },
        {
          type: 127,
          status: 3,
          invokeSubagent: {
            subagents: [{ role: 'Security Auditor' }],
            results: [{ conversationId: 'sub-traj-blocked' }],
          },
        },
      ],
    },
  },
  'sub-traj-blocked': {
    numTotalSteps: 2,
    trajectory: {
      steps: [
        { type: 14, status: 3 },
        {
          type: 21,
          status: 9,
          requestedInteraction: {
            interaction: {
              case: 'permission',
              value: {
                resource: { action: 'edit', target: '/etc/hosts' },
              },
            },
          },
        },
      ],
    },
  },
  'chat-coord': {
    numTotalSteps: 4,
    trajectory: {
      steps: [
        { type: 14, status: 3 },
        {
          type: 15,
          status: 3,
          plannerResponse: {
            toolCalls: [{ name: 'invoke_subagent', argumentsJson: '{}' }],
          },
        },
        {
          type: 127,
          status: 3,
          invokeSubagent: {
            subagents: [{ role: 'Code Reviewer' }],
            results: [{ conversationId: 'sub-worker-1' }],
          },
        },
      ],
    },
  },
  'chat-battle-ready': {
    numTotalSteps: 2,
    trajectory: {
      steps: [
        { type: 14, status: 3 },
        { type: 15, status: 3, plannerResponse: { response: 'Ready for vote' } },
      ],
    },
  },
};

const stepTailsByCid = {
  'chat-coord': [{ type: 23, status: 3 }],
};

const storeState = {
  trajectorySummaries: {
    summaries: {
      'chat-perm-running': {
        summary: '[14:00] ⦿ Clean scratch',
        // Numeric RUNNING (2) + notFullyIdle=true must still yield to waitingSteps
        status: 2,
        notFullyIdle: true,
        stepCount: 3,
        lastModifiedTime: '2026-10-29T18:29:50Z',
        annotations: { title: '[14:00] ⦿ Clean scratch', archived: false },
        waitingSteps: [
          {
            stepIndex: 2,
            step: {
              status: 9,
              type: 21,
              requestedInteraction: {
                interaction: {
                  case: 'permission',
                  value: {
                    resource: { action: 'execute', target: 'rm -rf /tmp/scratch' },
                  },
                },
              },
            },
          },
        ],
      },
      'chat-wait-ask': {
        summary: '[14:05] ⏳ Daily digest',
        // ⏳ waiting chat with generic summary waitingStep refined via GetCascadeTrajectory
        status: 1,
        notFullyIdle: true,
        stepCount: 3,
        lastModifiedTime: '2026-10-29T18:29:40Z',
        annotations: { title: '[14:05] ⏳ Daily digest', archived: false },
        waitingSteps: [{ stepIndex: 1, step: { status: 9, type: 138 } }],
      },
      'chat-elicitation': {
        summary: '[14:08] ⦿ OAuth setup',
        status: 1,
        notFullyIdle: false,
        stepCount: 2,
        lastModifiedTime: '2026-10-29T18:29:30Z',
        annotations: { title: '[14:08] ⦿ OAuth setup', archived: false },
        waitingSteps: [
          {
            stepIndex: 1,
            step: {
              status: 'CORTEX_STEP_STATUS_WAITING',
              requested_interaction: {
                elicitation: { message: 'Enter API token' },
              },
            },
          },
        ],
      },
      'chat-sub-parent': {
        summary: '[14:10] ⦿ Audit pipeline',
        status: 1,
        notFullyIdle: false,
        stepCount: 2,
        lastModifiedTime: '2026-10-29T18:20:00Z',
        // Already viewed by user, yet blocked active child must surface in Needs Me
        annotations: {
          title: '[14:10] ⦿ Audit pipeline',
          archived: false,
          lastUserViewTime: '2026-10-29T18:25:00Z',
        },
      },
      'sub-killed-flag': {
        summary: 'Dead Helper',
        status: 1,
        killed: true,
        lastModifiedTime: '2026-10-29T18:29:00Z',
        trajectoryMetadata: {
          parentConversationId: 'chat-sub-parent',
          sourceMetadata: { hideFromConversationList: true },
        },
        waitingSteps: [
          {
            stepIndex: 1,
            step: {
              status: 9,
              requestedInteraction: {
                permission: { actionDescription: 'ignored' },
              },
            },
          },
        ],
      },
      'sub-traj-blocked': {
        summary: 'Raw Child Title',
        status: 1,
        notFullyIdle: false,
        stepCount: 2,
        lastModifiedTime: '2026-10-29T18:29:45Z',
        waitingSteps: [{ stepIndex: 1, step: { status: 9, type: 21 } }],
        trajectoryMetadata: {
          parentConversationId: 'chat-sub-parent',
          sourceMetadata: { hideFromConversationList: true },
        },
      },
      'chat-battle': {
        summary: '[14:15] ⦿ Arenabakeoff',
        status: 1,
        notFullyIdle: false,
        stepCount: 2,
        lastModifiedTime: '2026-10-29T18:21:00Z',
        annotations: {
          title: '[14:15] ⦿ Arenabakeoff',
          archived: false,
          lastUserViewTime: '2026-10-29T18:26:00Z',
        },
        battleModeInfo: {
          endType: 0,
          childrenConversationIds: ['arm-1', 'arm-2'],
          eliminated: [false, true],
        },
      },
      'arm-1': {
        summary: 'Arm 1',
        status: 1,
        isBattleModeFork: true,
        trajectoryMetadata: { isBattleModeFork: true },
        waitingSteps: [
          {
            stepIndex: 3,
            step: {
              status: 9,
              requestedInteraction: {
                permission: { actionDescription: 'git push' },
              },
            },
          },
        ],
      },
      'arm-2': {
        summary: 'Arm 2',
        status: 1,
        isBattleModeFork: true,
        trajectoryMetadata: { isBattleModeFork: true },
        waitingSteps: [
          {
            stepIndex: 1,
            step: {
              status: 9,
              requestedInteraction: {
                permission: { actionDescription: 'eliminated' },
              },
            },
          },
        ],
      },
      'chat-coord': {
        summary: '[14:20] ⦿ Multi-agent review',
        status: 1,
        notFullyIdle: false,
        hasRunningSubagents: false,
        stepCount: 4,
        lastModifiedTime: '2026-10-29T18:29:55Z',
        annotations: { title: '[14:20] ⦿ Multi-agent review', archived: false },
      },
      'sub-worker-1': {
        summary: 'Code Reviewer',
        status: 1,
        notFullyIdle: false,
        stepCount: 3,
        lastModifiedTime: '2026-10-29T18:29:58Z',
        trajectoryMetadata: {
          parentConversationId: 'chat-coord',
          sourceMetadata: { hideFromConversationList: true },
        },
      },
      'chat-num-running': {
        summary: '[14:22] ⦿ Numeric busy agent',
        status: 2,
        notFullyIdle: true,
        stepCount: 2,
        lastModifiedTime: '2026-10-29T18:29:59Z',
        annotations: { title: '[14:22] ⦿ Numeric busy agent', archived: false },
      },
      'chat-battle-ready': {
        summary: '[14:18] ⦿ Battle awaiting vote',
        status: 1,
        notFullyIdle: false,
        stepCount: 2,
        lastModifiedTime: '2026-10-29T18:29:56Z',
        annotations: {
          title: '[14:18] ⦿ Battle awaiting vote',
          archived: false,
          lastUserViewTime: '2026-10-29T18:20:00Z',
        },
        battleModeInfo: {
          endType: 0,
          childrenConversationIds: ['arm-ready-1', 'arm-ready-2'],
        },
      },
      'arm-ready-1': {
        summary: 'Ready Arm 1',
        status: 1,
        lastModifiedTime: '2026-10-29T18:29:56Z',
        isBattleModeFork: true,
        trajectoryMetadata: { isBattleModeFork: true },
      },
      'arm-ready-2': {
        summary: 'Ready Arm 2',
        status: 1,
        lastModifiedTime: '2026-10-29T18:29:56Z',
        isBattleModeFork: true,
        trajectoryMetadata: { isBattleModeFork: true },
      },
    },
  },
  conversation: { cascadeId: 'chat-coord' },
  sideBar: { isOpen: true },
};

const subscribers = [];
const reduxStore = {
  getState: () => storeState,
  dispatch(action) {
    if (action?.type === 'trajectorySummaries/applyStreamBatch') {
      const next = { ...storeState.trajectorySummaries.summaries };
      for (const [id, s] of (action.payload?.updates || [])) next[id] = s;
      storeState.trajectorySummaries = { summaries: next };
    }
    for (const sub of subscribers) sub();
    return action;
  },
  subscribe(fn) {
    subscribers.push(fn);
    return () => {};
  },
};
displayOptionsBtn.__reactFiber$test = { memoizedProps: { store: reduxStore } };

const storage = new Map();
let rafCallbacks = [];

global.window = {
  innerWidth: 1280,
  location: { pathname: '/c/chat-coord', search: '' },
  __APP_CONFIG__: { csrfToken: 'r22-csrf' },
  localStorage: {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
  },
  fetch: async (url, opts) => {
    const u = String(url);
    const body = opts?.body ? JSON.parse(opts.body) : {};
    if (u.includes('/GetCascadeTrajectorySteps')) {
      const tail = stepTailsByCid[body.cascadeId] || [];
      const initLen =
        trajectoriesByCid[body.cascadeId]?.trajectory?.steps?.length || 0;
      const start = Math.max(0, (body.stepOffset || 0) - initLen);
      const pageSlice = tail.slice(start, start + 2).map((s) => ({ ...s }));
      return { ok: true, json: async () => ({ steps: pageSlice }) };
    }
    if (u.includes('/GetCascadeTrajectory')) {
      const src = trajectoriesByCid[body.cascadeId];
      if (!src) return { ok: true, json: async () => ({ trajectory: { steps: [] } }) };
      const cloned = JSON.parse(JSON.stringify(src));
      return { ok: true, json: async () => cloned };
    }
    if (u.includes('jetski-chains.json')) {
      return { ok: true, json: async () => ({ parentOf: {}, createdAt: {}, redirects: {} }) };
    }
    if (u.includes('jetski-roadmaps.json')) {
      return { ok: true, json: async () => ({ version: 1, projects: {} }) };
    }
    return { ok: false, status: 404 };
  },
};

global.document = {
  documentElement: docEl,
  head: headEl,
  body: bodyEl,
  getElementById: (id) => docEl.querySelector(`#${id}`),
  createElement: (tag) => new FakeElement(tag),
  querySelector: (sel) => docEl.querySelector(sel),
  querySelectorAll: (sel) => docEl.querySelectorAll(sel),
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent() {},
};
global.MutationObserver = class {
  observe() {}
  disconnect() {}
};
global.requestAnimationFrame = (cb) => {
  rafCallbacks.push(cb);
  return rafCallbacks.length;
};
let nextTimerId = 1;
const pendingTimers = new Map();
global.setTimeout = (fn, ms) => {
  const id = nextTimerId++;
  pendingTimers.set(id, { fn, due: fakeNow + (Number(ms) || 0) });
  return id;
};
global.clearTimeout = (id) => {
  pendingTimers.delete(id);
};

const flushRaf = () => {
  while (rafCallbacks.length > 0) rafCallbacks.shift()();
};
const flushAsync = async () => {
  for (let round = 0; round < 5; round++) {
    flushRaf();
    for (let i = 0; i < 8; i++) await Promise.resolve();
  }
  flushRaf();
};

eval(code);

(async () => {
  await flushAsync();

  const filterBar = actionRow.querySelector('.jetski-attention-filter-bar');
  const btnNeedsMe = filterBar.querySelector('button[data-bucket="needs-me"]');
  const btnWorking = filterBar.querySelector('button[data-bucket="working"]');
  const btnWaiting = filterBar.querySelector('button[data-bucket="waiting"]');

  // Eligible top-level chats (8):
  // - needs-me (6): 5 blocked + 1 unread finished battle awaiting vote (chat-battle-ready)
  // - working (2): chat-coord (waiting for subagent report), chat-num-running (status: 2)
  // - waiting (0): chat-wait-ask promoted out of waiting into needs-me because blocked
  assert.strictEqual(btnNeedsMe.getAttribute('aria-label'), '🚨 Needs Me: 6 (shown)');
  assert.strictEqual(btnWorking.getAttribute('aria-label'), '🤖 Working: 2 (shown)');
  assert.strictEqual(btnWaiting.getAttribute('aria-label'), '⏳ Waiting: 0 (hidden)');

  // Blocked chat with status=2 & notFullyIdle=true must NOT swap ⦿ -> ⏳ on its sidebar row
  assert.strictEqual(rowPermRunning.hasAttribute('data-jetski-bg-waiting'), false);
  assert.strictEqual(spanPermRunning.textContent, '[14:00] ⦿ Clean scratch');
  assert.strictEqual(rowPermRunning.querySelector('.jetski-row-bg-wait-indicator'), null);

  // Unblocked coordinator waiting for subagent report swaps ⦿ -> ⏳ with exact tooltip
  assert.strictEqual(rowCoord.getAttribute('data-jetski-bg-waiting'), 'true');
  assert.strictEqual(spanCoord.textContent, '[14:20] ⏳ Multi-agent review');
  const coordWaitBadge = rowCoord.querySelector('.jetski-row-bg-wait-indicator');
  assert(coordWaitBadge, 'Coordinator waiting on subagent must show ⏳ row indicator');
  assert.strictEqual(coordWaitBadge.getAttribute('title'), 'Waiting for subagent reports');

  // Verify Needs Me inbox rows: 5 blocked chats as 🔥 urgent + chat-battle-ready as unseen
  const inboxEl = sidebarWrap.querySelector('.jetski-needs-reaction-inbox');
  assert(inboxEl, 'Needs-Reaction inbox must mount');
  const urgentPill = inboxEl.querySelector('[data-testid="jetski-inbox-urgent-pill"]');
  assert.strictEqual(urgentPill.textContent, '· 5!');

  const getInboxRows = () =>
    inboxEl.querySelectorAll('[data-testid="jetski-inbox-row"]');
  const detailByCid = () => {
    const m = new Map();
    for (const r of getInboxRows()) {
      const cid = r.getAttribute('data-cascade-id');
      const det = r.querySelector('[data-testid="jetski-inbox-detail"]');
      m.set(cid, {
        tier: r.getAttribute('data-tier'),
        detail: det ? det.textContent : '',
        row: r,
      });
    }
    return m;
  };

  let details = detailByCid();
  assert.strictEqual(details.size, 6);
  assert.strictEqual(details.get('chat-battle-ready').tier, 'unseen');
  assert.strictEqual(details.get('chat-perm-running').tier, 'urgent');
  assert.strictEqual(
    details.get('chat-perm-running').detail,
    'Permission required for execute: rm -rf /tmp/scratch'
  );
  assert.strictEqual(
    details.get('chat-wait-ask').detail,
    'Awaiting answer: Which branch should I audit?'
  );
  assert.strictEqual(
    details.get('chat-elicitation').detail,
    'Input required: Enter API token'
  );
  assert.strictEqual(
    details.get('chat-sub-parent').detail,
    '[Security Auditor] Permission required for edit: /etc/hosts'
  );
  assert.strictEqual(
    details.get('chat-battle').detail,
    '[Battle arm 1] Permission required for git push'
  );

  // Switch inbox to 🤖 Working tab and verify coordinator subagent detail + numeric running icon
  const tabWorking = inboxEl.querySelector('[data-testid="jetski-inbox-tab-working"]');
  tabWorking.dispatchEvent('pointerdown', { button: 0 });
  flushRaf();
  let workingDetails = detailByCid();
  assert.strictEqual(workingDetails.size, 2);
  assert.strictEqual(workingDetails.get('chat-coord').detail, 'Waiting for subagent reports');
  assert.strictEqual(
    workingDetails.get('chat-coord').row.querySelector('.jetski-inbox-tier-icon').textContent,
    '⏳'
  );
  assert.strictEqual(workingDetails.get('chat-num-running').detail, '');
  assert.strictEqual(
    workingDetails.get('chat-num-running').row.querySelector('.jetski-inbox-tier-icon').textContent,
    '🤖'
  );

  // Phase B: Subagent reports via SYSTEM_MESSAGE (tail via GetCascadeTrajectorySteps),
  // coordinator still status=1 IDLE without final synthesis -> stays in Working!
  stepTailsByCid['chat-coord'] = [
    {
      type: 101,
      status: 3,
      systemMessage: { agentMessage: { sender: 'sub-worker-1' } },
    },
    { type: 23, status: 3 },
  ];
  trajectoriesByCid['chat-coord'].numTotalSteps = 5;
  reduxStore.dispatch({
    type: 'trajectorySummaries/applyStreamBatch',
    payload: {
      updates: [[
        'chat-coord',
        {
          ...storeState.trajectorySummaries.summaries['chat-coord'],
          stepCount: 5,
          lastModifiedTime: '2026-10-29T18:30:02Z',
        },
      ]],
      deletes: [],
    },
  });
  await flushAsync();
  workingDetails = detailByCid();
  assert.strictEqual(workingDetails.get('chat-coord').detail, 'Synthesizing subagent findings');
  assert.strictEqual(coordWaitBadge.getAttribute('title'), 'Synthesizing subagent findings');

  // Phase C: Coordinator emits intermediate tool call on page 1 and concluding DONE
  // PLANNER_RESPONSE + CHECKPOINT on page 2 -> transitions to 🚨 Needs Me and clears ⏳
  stepTailsByCid['chat-coord'] = [
    {
      type: 101,
      status: 3,
      systemMessage: { agentMessage: { sender: 'sub-worker-1' } },
    },
    {
      type: 15,
      status: 3,
      plannerResponse: {
        response: 'Reading file...',
        toolCalls: [{ name: 'read_file', argumentsJson: '{}' }],
      },
    },
    {
      type: 15,
      status: 3,
      plannerResponse: { response: 'All subagent checks passed.' },
    },
    { type: 23, status: 3 },
  ];
  trajectoriesByCid['chat-coord'].numTotalSteps = 7;
  reduxStore.dispatch({
    type: 'trajectorySummaries/applyStreamBatch',
    payload: {
      updates: [[
        'chat-coord',
        {
          ...storeState.trajectorySummaries.summaries['chat-coord'],
          stepCount: 7,
          lastModifiedTime: '2026-10-29T18:30:05Z',
        },
      ]],
      deletes: [],
    },
  });
  await flushAsync();

  assert.strictEqual(btnNeedsMe.getAttribute('aria-label'), '🚨 Needs Me: 7 (shown)');
  assert.strictEqual(btnWorking.getAttribute('aria-label'), '🤖 Working: 1 (shown)');
  assert.strictEqual(rowCoord.hasAttribute('data-jetski-bg-waiting'), false);
  assert.strictEqual(spanCoord.textContent, '[14:20] ⦿ Multi-agent review');
  assert.strictEqual(rowCoord.querySelector('.jetski-row-bg-wait-indicator'), null);

  // Switch back to 🚨 Needs Me tab; verify killing sub-traj-blocked via manage_subagents and
  // finishing battleModeInfo clears both already-viewed parents from Needs Me without poisoning
  const tabNeedsMe = inboxEl.querySelector('[data-testid="jetski-inbox-tab-needs-me"]');
  tabNeedsMe.dispatchEvent('pointerdown', { button: 0 });
  flushRaf();

  trajectoriesByCid['chat-sub-parent'].trajectory.steps.push(
    {
      type: 15,
      status: 3,
      plannerResponse: {
        toolCalls: [
          {
            name: 'manage_subagents',
            argumentsJson: JSON.stringify({
              Action: 'kill',
              ConversationIds: ['sub-traj-blocked'],
            }),
          },
        ],
      },
    },
    {
      type: 15,
      status: 3,
      plannerResponse: { response: 'Terminated stuck child.' },
    }
  );
  trajectoriesByCid['chat-sub-parent'].numTotalSteps = 4;

  reduxStore.dispatch({
    type: 'trajectorySummaries/applyStreamBatch',
    payload: {
      updates: [
        [
          'chat-sub-parent',
          {
            ...storeState.trajectorySummaries.summaries['chat-sub-parent'],
            stepCount: 4,
          },
        ],
        [
          'chat-battle',
          {
            ...storeState.trajectorySummaries.summaries['chat-battle'],
            battleModeInfo: {
              endType: 'BATTLE_END_TYPE_WINNER_SELECTED',
              winningConversationId: 'arm-1',
              childrenConversationIds: ['arm-1', 'arm-2'],
            },
          },
        ],
      ],
      deletes: [],
    },
  });
  await flushAsync();
  details = detailByCid();
  assert.strictEqual(details.has('chat-sub-parent'), false);
  assert.strictEqual(details.has('chat-battle'), false);
  assert.strictEqual(urgentPill.textContent, '· 3!');

  // Dismiss chat-perm-running while blocked at stepIndex 2 -> disappears from Needs Me
  const permSeenBtn = details
    .get('chat-perm-running')
    .row.querySelector('[data-testid="jetski-inbox-seen-btn"]');
  permSeenBtn.dispatchEvent('pointerdown', { button: 0 });
  flushRaf();
  details = detailByCid();
  assert.strictEqual(details.has('chat-perm-running'), false);
  assert.strictEqual(urgentPill.textContent, '· 2!');

  // New blocked step (stepIndex 5, npm publish) re-surfaces chat-perm-running in Needs Me!
  reduxStore.dispatch({
    type: 'trajectorySummaries/applyStreamBatch',
    payload: {
      updates: [[
        'chat-perm-running',
        {
          ...storeState.trajectorySummaries.summaries['chat-perm-running'],
          stepCount: 6,
          waitingSteps: [
            {
              stepIndex: 5,
              step: {
                status: 9,
                type: 21,
                requestedInteraction: {
                  interaction: {
                    case: 'permission',
                    value: {
                      resource: { action: 'execute', target: 'npm publish' },
                    },
                  },
                },
              },
            },
          ],
        },
      ]],
      deletes: [],
    },
  });
  await flushAsync();
  details = detailByCid();
  assert.strictEqual(details.has('chat-perm-running'), true);
  assert.strictEqual(details.get('chat-perm-running').tier, 'urgent');
  assert.strictEqual(
    details.get('chat-perm-running').detail,
    'Permission required for execute: npm publish'
  );
  assert.strictEqual(urgentPill.textContent, '· 3!');

  window.__jetskiClosedChatsFilterCleanup();
  assert.strictEqual(pendingTimers.size, 0, 'Zero dangling timers after cleanup');

  console.log('R22_ASSERTIONS_PASSED');
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
        f"R22 JS harness failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )
    self.assertIn("R22_ASSERTIONS_PASSED", res.stdout)

  def test_r76_ask_question_waiting_steps_and_payload_variants(self) -> None:
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
    this._textContent = '';
    this._innerHTML = '';
    this.style = {};
    this.attributes = new Map();
    this.children = [];
    this.parentElement = null;
    this.listeners = new Map();
  }
  get textContent() {
    if (this.children.length > 0) {
      return this._textContent + this.children.map((c) => c.textContent).join('');
    }
    return this._textContent;
  }
  set textContent(val) {
    this._textContent = String(val);
    for (const c of this.children) c.parentElement = null;
    this.children = [];
  }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(val) { this._innerHTML = String(val); }
  setAttribute(k, v) {
    this.attributes.set(k, String(v));
    if (k === 'id') this.id = String(v);
    if (k === 'class') this.className = String(v);
  }
  getAttribute(k) {
    if (k === 'id' && this.id) return this.id;
    return this.attributes.has(k) ? this.attributes.get(k) : null;
  }
  hasAttribute(k) { return this.attributes.has(k); }
  removeAttribute(k) { this.attributes.delete(k); }
  appendChild(child) {
    if (child.parentElement) child.remove();
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  insertBefore(child, ref) {
    if (child === ref) return child;
    if (child.parentElement) child.remove();
    child.parentElement = this;
    const idx = this.children.indexOf(ref);
    if (idx === -1) this.children.push(child);
    else this.children.splice(idx, 0, child);
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
    for (const fn of (this.listeners.get(type) || [])) {
      fn({ type, target: this, ...eventInit });
    }
  }
  getBoundingClientRect() {
    return { left: 24, top: 40, bottom: 62, right: 140, width: 116, height: 22 };
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
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
  if (selector === 'button[aria-label="Display Options"]') {
    return el.tagName === 'BUTTON' && el.getAttribute('aria-label') === 'Display Options';
  }
  const bucketMatch = /^button\[data-bucket="([^"]+)"\]$/.exec(selector);
  if (bucketMatch) {
    return el.tagName === 'BUTTON' && el.getAttribute('data-bucket') === bucketMatch[1];
  }
  if (selector === '[data-testid="conversation-row-sidebar"][data-cascade-id]') {
    return (
      el.getAttribute('data-testid') === 'conversation-row-sidebar' &&
      el.hasAttribute('data-cascade-id')
    );
  }
  const attrEqMatch = /^\[([-a-zA-Z0-9_]+)="([^"]+)"\]$/.exec(selector);
  if (attrEqMatch) return el.getAttribute(attrEqMatch[1]) === attrEqMatch[2];
  if (selector.startsWith('.')) {
    const classes = selector.split('.').filter(Boolean);
    const elClasses = el.className ? el.className.split(/\s+/) : [];
    return classes.every((cls) => elClasses.includes(cls));
  }
  return false;
}

const docEl = new FakeElement('html');
const headEl = new FakeElement('head');
const bodyEl = new FakeElement('body');
docEl.appendChild(headEl);
docEl.appendChild(bodyEl);

const sidebarWrap = new FakeElement('div');
const actionRow = new FakeElement('div');
const displayOptionsBtn = new FakeElement('button');
displayOptionsBtn.setAttribute('aria-label', 'Display Options');
actionRow.appendChild(displayOptionsBtn);
sidebarWrap.appendChild(actionRow);

const sidebarListEl = new FakeElement('div');
sidebarListEl.setAttribute('data-testid', 'conversation-list-sidebar');
sidebarWrap.appendChild(sidebarListEl);
bodyEl.appendChild(sidebarWrap);

const cids = [
  'chat-snake-ws',
  'chat-toolcall-meta',
  'chat-toolcall-pr',
  'chat-toolcall-string-array',
  'chat-generic-payload',
  'chat-direct-step-questions',
  'chat-oneof-direct',
];

for (const cid of cids) {
  const row = new FakeElement('div');
  row.setAttribute('data-testid', 'conversation-row-sidebar');
  row.setAttribute('data-cascade-id', cid);
  const titleSpan = new FakeElement('span');
  titleSpan.className = 'truncate';
  titleSpan.textContent = `[14:00] ⦿ ${cid}`;
  const hoverBar = new FakeElement('div');
  const pinBtn = new FakeElement('button');
  pinBtn.setAttribute('data-testid', 'conversation-pin-button');
  hoverBar.appendChild(pinBtn);
  row.appendChild(titleSpan);
  row.appendChild(hoverBar);
  sidebarListEl.appendChild(row);
}

const trajectoriesByCid = {
  'chat-snake-ws': {
    numTotalSteps: 1,
    trajectory: { steps: [{ type: 14, status: 3 }] },
  },
  'chat-toolcall-meta': {
    numTotalSteps: 2,
    trajectory: {
      steps: [
        { type: 14, status: 3 },
        {
          type: 132,
          status: 9,
          metadata: {
            toolCall: {
              name: 'ask_question',
              argumentsJson: JSON.stringify({
                questions: [{ question: 'Pick database shard' }],
              }),
            },
          },
        },
      ],
    },
  },
  'chat-toolcall-pr': {
    numTotalSteps: 2,
    trajectory: {
      steps: [
        { type: 14, status: 3 },
        {
          type: 15,
          status: 9,
          plannerResponse: {
            toolCalls: [{
              name: 'askQuestion',
              argumentsJson: JSON.stringify({ question: 'Proceed with wipe?' }),
            }],
          },
        },
      ],
    },
  },
  'chat-toolcall-string-array': {
    numTotalSteps: 2,
    trajectory: {
      steps: [
        { type: 14, status: 3 },
        {
          type: 132,
          status: 9,
          metadata: {
            toolCall: {
              name: 'ask_question',
              argumentsJson: JSON.stringify({ questions: ['Deploy now?'] }),
            },
          },
        },
      ],
    },
  },
  'chat-generic-payload': {
    numTotalSteps: 2,
    trajectory: {
      steps: [
        { type: 14, status: 3 },
        {
          type: 132,
          status: 9,
          generic: {
            result: {
              payload: {
                ask_question: {
                  questions: [{ question: 'Confirm migration?' }],
                },
              },
            },
          },
        },
      ],
    },
  },
  'chat-direct-step-questions': {
    numTotalSteps: 2,
    trajectory: {
      steps: [
        { type: 14, status: 3 },
        {
          type: 138,
          status: 9,
          questions: [{ question: 'Which branch to merge?' }],
        },
      ],
    },
  },
  'chat-oneof-direct': {
    numTotalSteps: 2,
    trajectory: {
      steps: [
        { type: 14, status: 3 },
        {
          type: 138,
          status: 9,
          requestedInteraction: {
            interaction: {
              ask_question: {
                questions: [{ question: 'Approve cherry-pick?' }],
              },
            },
          },
        },
      ],
    },
  },
};

const nowIso = new Date().toISOString();
const storeState = {
  trajectorySummaries: {
    summaries: {
      'chat-snake-ws': {
        summary: '[14:00] ⦿ chat-snake-ws',
        status: 1,
        stepCount: 1,
        lastModifiedTime: nowIso,
        annotations: { title: '[14:00] ⦿ chat-snake-ws', archived: false },
        waiting_steps: [
          {
            index: 0,
            status: 9,
            type: 138,
            requestedInteraction: {
              interaction: {
                case: 'askQuestion',
                value: { questions: [{ question: 'Roll out to canary?' }] },
              },
            },
          },
        ],
      },
      'chat-toolcall-meta': {
        summary: '[14:00] ⦿ chat-toolcall-meta',
        status: 1,
        stepCount: 2,
        lastModifiedTime: nowIso,
        annotations: { title: '[14:00] ⦿ chat-toolcall-meta', archived: false },
      },
      'chat-toolcall-pr': {
        summary: '[14:00] ⦿ chat-toolcall-pr',
        status: 1,
        stepCount: 2,
        lastModifiedTime: nowIso,
        annotations: { title: '[14:00] ⦿ chat-toolcall-pr', archived: false },
      },
      'chat-toolcall-string-array': {
        summary: '[14:00] ⦿ chat-toolcall-string-array',
        status: 1,
        stepCount: 2,
        lastModifiedTime: nowIso,
        annotations: { title: '[14:00] ⦿ chat-toolcall-string-array', archived: false },
      },
      'chat-generic-payload': {
        summary: '[14:00] ⦿ chat-generic-payload',
        status: 1,
        stepCount: 2,
        lastModifiedTime: nowIso,
        annotations: { title: '[14:00] ⦿ chat-generic-payload', archived: false },
      },
      'chat-direct-step-questions': {
        summary: '[14:00] ⦿ chat-direct-step-questions',
        status: 1,
        stepCount: 2,
        lastModifiedTime: nowIso,
        annotations: { title: '[14:00] ⦿ chat-direct-step-questions', archived: false },
      },
      'chat-oneof-direct': {
        summary: '[14:00] ⦿ chat-oneof-direct',
        status: 1,
        stepCount: 2,
        lastModifiedTime: nowIso,
        annotations: { title: '[14:00] ⦿ chat-oneof-direct', archived: false },
      },
    },
  },
  conversation: { cascadeId: 'chat-snake-ws' },
  sideBar: { isOpen: true },
};

const subscribers = [];
const reduxStore = {
  getState: () => storeState,
  dispatch(action) {
    for (const sub of subscribers) sub();
    return action;
  },
  subscribe(fn) {
    subscribers.push(fn);
    return () => {};
  },
};
displayOptionsBtn.__reactFiber$test = { memoizedProps: { store: reduxStore } };

const storage = new Map();
let rafCallbacks = [];

global.window = {
  innerWidth: 1280,
  location: { pathname: '/c/chat-snake-ws', search: '' },
  __APP_CONFIG__: { csrfToken: 'r76-csrf' },
  localStorage: {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
  },
  fetch: async (url, opts) => {
    const u = String(url);
    const body = opts?.body ? JSON.parse(opts.body) : {};
    if (u.includes('/GetCascadeTrajectory')) {
      const src = trajectoriesByCid[body.cascadeId];
      if (!src) return { ok: true, json: async () => ({ trajectory: { steps: [] } }) };
      return { ok: true, json: async () => JSON.parse(JSON.stringify(src)) };
    }
    if (u.includes('jetski-chains.json')) {
      return { ok: true, json: async () => ({ parentOf: {}, createdAt: {}, redirects: {} }) };
    }
    if (u.includes('jetski-roadmaps.json')) {
      return { ok: true, json: async () => ({ version: 1, projects: {} }) };
    }
    return { ok: false, status: 404 };
  },
};

global.document = {
  documentElement: docEl,
  head: headEl,
  body: bodyEl,
  getElementById: (id) => docEl.querySelector(`#${id}`),
  createElement: (tag) => new FakeElement(tag),
  querySelector: (sel) => docEl.querySelector(sel),
  querySelectorAll: (sel) => docEl.querySelectorAll(sel),
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent() {},
};
global.MutationObserver = class {
  observe() {}
  disconnect() {}
};
global.requestAnimationFrame = (cb) => {
  rafCallbacks.push(cb);
  return rafCallbacks.length;
};
let nextTimerId = 1;
const pendingTimers = new Map();
global.setTimeout = (fn, ms) => {
  const id = nextTimerId++;
  pendingTimers.set(id, { fn, due: Date.now() + (Number(ms) || 0) });
  return id;
};
global.clearTimeout = (id) => {
  pendingTimers.delete(id);
};

const flushRaf = () => {
  while (rafCallbacks.length > 0) rafCallbacks.shift()();
};
const flushAsync = async () => {
  for (let round = 0; round < 5; round++) {
    flushRaf();
    for (let i = 0; i < 8; i++) await Promise.resolve();
  }
  flushRaf();
};

eval(code);

(async () => {
  await flushAsync();

  const filterBar = actionRow.querySelector('.jetski-attention-filter-bar');
  const btnNeedsMe = filterBar.querySelector('button[data-bucket="needs-me"]');
  assert.strictEqual(btnNeedsMe.getAttribute('aria-label'), '🚨 Needs Me: 7 (shown)');

  const inboxEl = sidebarWrap.querySelector('.jetski-needs-reaction-inbox');
  assert(inboxEl, 'Needs-Reaction inbox must mount');
  const urgentPill = inboxEl.querySelector('[data-testid="jetski-inbox-urgent-pill"]');
  assert.strictEqual(urgentPill.textContent, '· 7!');

  const rows = inboxEl.querySelectorAll('[data-testid="jetski-inbox-row"]');
  const detailByCid = new Map();
  for (const r of rows) {
    const cid = r.getAttribute('data-cascade-id');
    const det = r.querySelector('[data-testid="jetski-inbox-detail"]');
    detailByCid.set(cid, {
      tier: r.getAttribute('data-tier'),
      detail: det ? det.textContent : '',
    });
  }

  assert.strictEqual(detailByCid.size, 7);
  assert.strictEqual(
    detailByCid.get('chat-snake-ws').detail,
    'Awaiting answer: Roll out to canary?'
  );
  assert.strictEqual(
    detailByCid.get('chat-toolcall-meta').detail,
    'Awaiting answer: Pick database shard'
  );
  assert.strictEqual(
    detailByCid.get('chat-toolcall-pr').detail,
    'Awaiting answer: Proceed with wipe?'
  );
  assert.strictEqual(
    detailByCid.get('chat-toolcall-string-array').detail,
    'Awaiting answer: Deploy now?'
  );
  assert.strictEqual(
    detailByCid.get('chat-generic-payload').detail,
    'Awaiting answer: Confirm migration?'
  );
  assert.strictEqual(
    detailByCid.get('chat-direct-step-questions').detail,
    'Awaiting answer: Which branch to merge?'
  );
  assert.strictEqual(
    detailByCid.get('chat-oneof-direct').detail,
    'Awaiting answer: Approve cherry-pick?'
  );

  for (const cid of cids) {
    assert.strictEqual(detailByCid.get(cid).tier, 'urgent', `Urgent tier expected for ${cid}`);
  }

  window.__jetskiClosedChatsFilterCleanup();
  assert.strictEqual(pendingTimers.size, 0, 'Zero dangling timers after cleanup');

  console.log('R76_ASSERTIONS_PASSED');
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
        f"R76 JS harness failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )
    self.assertIn("R76_ASSERTIONS_PASSED", res.stdout)


  def test_cross_project_open_and_sidebar_locate(self) -> None:
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
    this.style = {};
    this.attributes = new Map();
    this.children = [];
    this.parentElement = null;
    this.listeners = new Map();
    this._textContent = '';
    this._innerHTML = '';
  }
  get textContent() { return this._textContent; }
  set textContent(v) { this._textContent = String(v); }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(v) { this._innerHTML = String(v); }
  setAttribute(k, v) {
    this.attributes.set(k, String(v));
    if (k === 'id') this.id = String(v);
    if (k === 'class') this.className = String(v);
  }
  getAttribute(k) { return this.attributes.has(k) ? this.attributes.get(k) : null; }
  hasAttribute(k) { return this.attributes.has(k); }
  removeAttribute(k) { this.attributes.delete(k); }
  appendChild(c) { c.parentElement = this; this.children.push(c); return c; }
  insertBefore(c, ref) { c.parentElement = this; this.children.push(c); return c; }
  remove() {
    if (!this.parentElement) return;
    const i = this.parentElement.children.indexOf(this);
    if (i !== -1) this.parentElement.children.splice(i, 1);
    this.parentElement = null;
  }
  addEventListener(t, fn) {
    if (!this.listeners.has(t)) this.listeners.set(t, []);
    this.listeners.get(t).push(fn);
  }
  closest() { return null; }
  querySelector(s) { return this.querySelectorAll(s)[0] || null; }
  querySelectorAll(s) {
    if (typeof s === 'string' && s.includes(',')) {
      return s.split(',').flatMap((p) => this.querySelectorAll(p.trim()));
    }
    const out = [];
    const visit = (n) => {
      for (const c of n.children) {
        if (typeof s === 'string') {
          if (s.startsWith('#') && c.id === s.slice(1)) {
            out.push(c);
          } else if (
            s.includes('conversation-list-sidebar') &&
            c.getAttribute('data-testid') === 'conversation-list-sidebar'
          ) {
            out.push(c);
          } else if (
            s.includes('conversation-row-sidebar') &&
            c.getAttribute('data-testid') === 'conversation-row-sidebar'
          ) {
            out.push(c);
          }
        }
        visit(c);
      }
    };
    visit(this);
    return out;
  }
  scrollIntoView(opts) {
    this._scrolledOpts = opts;
  }
}

const docEl = new FakeElement('html');
const headEl = new FakeElement('head');
const bodyEl = new FakeElement('body');
docEl.appendChild(headEl);
docEl.appendChild(bodyEl);

const mockStore = {
  getState: () => ({
    multiConvoLayout: { activeConversations: [] },
    trajectorySummaries: {
      summaries: {
        'chat-cross-1': {
          cascadeId: 'chat-cross-1',
          trajectoryMetadata: { projectId: 'proj-other' },
        },
      },
    },
    projects: {
      projects: [
        { id: 'proj-other', archived: true },
      ],
    },
  }),
  dispatch: () => {},
  subscribe: () => () => {},
};

const toggledSections = [];
const vlistEl = new FakeElement('div');
vlistEl.setAttribute('data-testid', 'conversation-list-sidebar');

const vlistItems = [
  { type: 'header', id: 'header-proj-other', isCollapsed: true },
  { type: 'row', cascadeId: 'chat-cross-1' },
];

vlistEl['__reactFiber$test'] = {
  memoizedProps: {
    store: mockStore,
    items: vlistItems,
    onToggleCollapseSection: (pid) => toggledSections.push(pid),
  },
  memoizedState: {
    memoizedState: {
      scrollToIndex: (idx) => { vlistEl._scrolledToItem = idx; },
    },
  },
};
bodyEl.appendChild(vlistEl);

const chatRow = new FakeElement('div');
chatRow.setAttribute('data-testid', 'conversation-row-sidebar');
chatRow.setAttribute('data-cascade-id', 'chat-cross-1');
vlistEl.appendChild(chatRow);

const fetchCalls = [];
let navigatedUrl = null;
let navState = null;

global.requestAnimationFrame = (cb) => cb();
global.window = {
  __APP_CONFIG__: { csrfToken: 'test-csrf' },
  location: {
    pathname: '/battle',
    search: '',
    hash: '',
  },
  history: {
    state: { __TSR_index: 2 },
    pushState: (state, title, url) => {
      navState = state;
      navigatedUrl = url;
    },
    replaceState: (state, title, url) => {
      navState = state;
      navigatedUrl = url;
    },
  },
  document: null,
  fetch: async (url, opts) => {
    fetchCalls.push({ url: String(url), body: opts?.body ? JSON.parse(opts.body) : null });
    if (String(url).includes('jetski-chains.json')) {
      return { ok: true, json: async () => ({ parentOf: {}, createdAt: {}, redirects: {} }) };
    }
    return { ok: true, json: async () => ({}) };
  },
  localStorage: {
    getItem: () => null,
    setItem: () => {},
  },
  dispatchEvent: () => {},
  addEventListener: () => {},
  removeEventListener: () => {},
  requestAnimationFrame: (cb) => cb(),
  setTimeout: (cb) => { Promise.resolve().then(cb); return 1; },
  clearTimeout: () => {},
};
global.setTimeout = window.setTimeout;
global.clearTimeout = window.clearTimeout;
global.PopStateEvent = class {
  constructor(type, init) {
    this.type = type;
    this.state = init?.state;
  }
};

global.document = {
  documentElement: docEl,
  head: headEl,
  body: bodyEl,
  getElementById: (id) => docEl.querySelector(`#${id}`),
  createElement: (tag) => new FakeElement(tag),
  querySelector: (sel) => {
    if (typeof sel === 'string' && sel.includes('conversation-list-sidebar')) return vlistEl;
    if (typeof sel === 'string' && sel.includes('chat-cross-1')) return chatRow;
    return docEl.querySelector(sel);
  },
  querySelectorAll: (sel) => docEl.querySelectorAll(sel),
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent() {},
};
global.MutationObserver = class {
  observe() {}
  disconnect() {}
};

eval(code);

(async () => {
  assert(
    typeof window.__jetskiOpenConversation === 'function',
    '__jetskiOpenConversation must be exported'
  );
  assert(
    typeof window.__jetskiLocateInSidebar === 'function',
    '__jetskiLocateInSidebar must be exported'
  );

  await window.__jetskiLocateInSidebar('chat-cross-1');
  assert(toggledSections.includes('proj-other'), 'Must expand header-proj-other section');

  window.__jetskiOpenConversation('chat-cross-1');
  for (let i = 0; i < 6; i++) await Promise.resolve();
  assert.strictEqual(
    navigatedUrl,
    '/c/chat-cross-1?section=proj-other',
    'Must navigate to /c/chat-cross-1?section=proj-other'
  );

  const unarchiveCall = fetchCalls.find((c) => c.url.includes('/UpdateProject'));
  assert(unarchiveCall, 'Must call UpdateProject RPC to unarchive proj-other');
  assert.strictEqual(unarchiveCall.body?.project?.id, 'proj-other');
  assert.strictEqual(unarchiveCall.body?.project?.archived, false);

  window.__jetskiClosedChatsFilterCleanup?.();
  console.log('ALL_CROSS_PROJECT_TESTS_PASSED');
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
        f"Cross-project JS harness failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )
    self.assertIn("ALL_CROSS_PROJECT_TESTS_PASSED", res.stdout)

  def test_web_ui_model_autopilot_and_quota_failover(self) -> None:
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
    this.style = {};
    this.attributes = new Map();
    this.children = [];
    this.parentElement = null;
    this.listeners = new Map();
    this._textContent = '';
    this._innerHTML = '';
  }
  get textContent() { return this._textContent; }
  set textContent(v) { this._textContent = String(v); }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(v) { this._innerHTML = String(v); }
  setAttribute(k, v) {
    this.attributes.set(k, String(v));
    if (k === 'id') this.id = String(v);
    if (k === 'class') this.className = String(v);
  }
  getAttribute(k) { return this.attributes.has(k) ? this.attributes.get(k) : null; }
  hasAttribute(k) { return this.attributes.has(k); }
  removeAttribute(k) { this.attributes.delete(k); }
  appendChild(c) {
    if (c.parentElement) c.remove();
    c.parentElement = this;
    this.children.push(c);
    return c;
  }
  insertBefore(c, ref) {
    if (c === ref) return c;
    if (c.parentElement) c.remove();
    c.parentElement = this;
    const idx = this.children.indexOf(ref);
    if (idx === -1) this.children.push(c);
    else this.children.splice(idx, 0, c);
    return c;
  }
  remove() {
    if (!this.parentElement) return;
    const i = this.parentElement.children.indexOf(this);
    if (i !== -1) this.parentElement.children.splice(i, 1);
    this.parentElement = null;
  }
  addEventListener(t, fn) {
    if (!this.listeners.has(t)) this.listeners.set(t, []);
    this.listeners.get(t).push(fn);
  }
  closest(selector) {
    let cur = this;
    while (cur) {
      if (typeof selector === 'string' && selector.startsWith('#') && cur.id === selector.slice(1)) {
        return cur;
      }
      cur = cur.parentElement;
    }
    return null;
  }
  querySelector(s) { return this.querySelectorAll(s)[0] || null; }
  querySelectorAll(s) {
    if (typeof s === 'string' && s.includes(',')) {
      return s.split(',').flatMap((p) => this.querySelectorAll(p.trim()));
    }
    const out = [];
    const visit = (n) => {
      for (const c of n.children) {
        if (typeof s === 'string') {
          if (s.startsWith('#') && c.id === s.slice(1)) {
            out.push(c);
          } else if (
            s === 'button[aria-label="Display Options"]' &&
            c.tagName === 'BUTTON' &&
            c.getAttribute('aria-label') === 'Display Options'
          ) {
            out.push(c);
          }
        }
        visit(c);
      }
    };
    visit(this);
    return out;
  }
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

const protoMarker = { __isBufProto: true };
function protoObj(typeName, fields) {
  return Object.assign(Object.create(protoMarker), { $typeName: typeName, ...fields });
}

function makeNativeConfig(label, enumVal, modelId, remaining) {
  const quotaFields = {
    resetTime: protoObj('google.protobuf.Timestamp', {
      seconds: 1793447031n,
      nanos: 0,
    }),
  };
  if (remaining !== undefined) {
    quotaFields.remainingFraction = remaining;
  }
  return protoObj('exa.codeium_common_pb.ClientModelConfig', {
    label,
    modelOrAlias: protoObj('exa.codeium_common_pb.ModelOrAlias', {
      choice: { case: 'model', value: enumVal },
    }),
    creditMultiplier: 0,
    pricingType: 0,
    disabled: false,
    supportsImages: true,
    supportsLegacy: false,
    isPremium: false,
    betaWarningMessage: '',
    isBeta: false,
    provider: 0,
    isRecommended: true,
    allowedTiers: [1, 2],
    description: '',
    quotaInfo: protoObj('exa.codeium_common_pb.QuotaInfo', quotaFields),
    tagTitle: '',
    tagDescription: '',
    supportedMimeTypes: { 'image/png': true },
    supportsThoughtCirculation: false,
    modelUrl: '',
    modelId,
    contextWindowOptions: [],
  });
}

function makeServerUserStatus() {
  const nativeConfigs = [
    makeNativeConfig('Gemini Pro Next', 1260, 'auto', 0.989),
    makeNativeConfig('Claude Opus 5.5 (High)', 1370, 'opus-5.5-high', 0.56),
    makeNativeConfig('Claude Opus 5.5 (Medium)', 1369, 'opus-5.5-medium', 0.56),
    makeNativeConfig('Claude Fable 5.1 (High)', 1388, 'fable-5.1-dropdown', 0.80),
    makeNativeConfig('Gemini 4 Argon (High)', 1393, 'gemini-launch-candidate', undefined),
  ];
  return protoObj('exa.codeium_common_pb.UserStatus', {
    cascadeModelConfigData: protoObj('exa.codeium_common_pb.CascadeModelConfigData', {
      clientModelConfigs: nativeConfigs,
      clientModelSorts: [
        protoObj('exa.codeium_common_pb.ClientModelSort', {
          name: 'Recommended',
          groups: [
            protoObj('exa.codeium_common_pb.ClientModelGroup', {
              groupName: '',
              modelLabels: nativeConfigs.map((c) => c.label),
            }),
          ],
        }),
      ],
      battleModeModelConfigs: [],
      battleModeModelSorts: [],
    }),
  });
}

const availableModelsPayload = {
  response: {
    models: {
      'auto': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M260',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.989, resetTime: '2026-10-30T11:43:51Z' },
      },
      'opus-5.5-high': {
        displayName: 'Claude Opus 5.5 (High)',
        model: 'MODEL_PLACEHOLDER_M370',
        apiProvider: 'API_PROVIDER_ANTHROPIC_VERTEX',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.56, resetTime: '2026-10-30T12:00:00Z' },
      },
      'opus-5.5-medium': {
        displayName: 'Claude Opus 5.5 (Medium)',
        model: 'MODEL_PLACEHOLDER_M369',
        apiProvider: 'API_PROVIDER_ANTHROPIC_VERTEX',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.56, resetTime: '2026-10-30T12:00:00Z' },
      },
      'opus-5.5-high-bon-le': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M421',
        apiProvider: 'API_PROVIDER_ANTHROPIC_VERTEX',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.95, resetTime: '2026-10-30T12:30:00Z' },
      },
      'sonnet-5.5-high-bon-le': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M420',
        apiProvider: 'API_PROVIDER_ANTHROPIC_VERTEX',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.72, resetTime: '2026-10-30T12:30:00Z' },
      },
      'bon-experiment-arm-226': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M226',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.99 },
      },
      'gemini-2.5-flash': {
        displayName: 'Gemini 2.5 Flash',
        model: 'MODEL_GOOGLE_GEMINI_2_5_FLASH',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.998, resetTime: '2026-10-30T14:00:00Z' },
      },
      'gemini-3.8-flash-high': {
        displayName: 'Gemini 3.8 Flash (High)',
        model: 'MODEL_PLACEHOLDER_M320',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.50, resetTime: '2026-10-30T14:00:00Z' },
      },
      'gemini-launch-candidate': {
        displayName: 'Gemini 4 Argon (High)',
        model: 'MODEL_PLACEHOLDER_M393',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
        supportsImages: true,
        // Protobuf3 JSON omission: future resetTime with omitted remainingFraction means 0.0
        quotaInfo: { resetTime: '2026-10-30T15:00:00Z' },
      },
      'fable-5.1-dropdown': {
        displayName: 'Claude Fable 5.1 (High)',
        model: 'MODEL_PLACEHOLDER_M388',
        apiProvider: 'API_PROVIDER_ANTHROPIC_VERTEX',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.80, resetTime: '2026-10-30T12:00:00Z' },
      },
      'expired-model': {
        displayName: 'Expired Reset',
        model: 'MODEL_PLACEHOLDER_M111',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.05, resetTime: '2026-10-29T10:00:00Z' },
      },
      'unmetered-model': {
        displayName: 'Unmetered',
        model: 'MODEL_PLACEHOLDER_M112',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
        supportsImages: true,
        quotaInfo: {},
      },
    },
    tabModelIds: [],
    imageGenerationModelIds: [],
    audioTranscriptionModelIds: [],
    deprecatedModelIds: {},
  },
};

const storeState = {
  trajectorySummaries: {
    summaries: {
      'chat-cpp-1': {
        cascadeId: 'chat-cpp-1',
        summary: '[14:00] ⦿ SkParagraph line metrics',
        stepCount: 2,
        trajectoryMetadata: {
          projectId: '8acb67c4-2cb7-43ca-ba04-dc547b3c4788',
        },
        annotations: { title: '[14:00] ⦿ SkParagraph line metrics', archived: false },
      },
    },
  },
  modelSelection: {
    default: { model: 1260 }, // auto
    overrideVersionId: { kind: 1 },
    byConversation: {},
    trajectorySync: {},
  },
  userStatus: makeServerUserStatus(),
  projects: {
    projects: [
      { id: '8acb67c4-2cb7-43ca-ba04-dc547b3c4788', name: 'SkParagraph · ● 1' },
      { id: '3f5b7a12-8e5f-47d2-a2f4-d644447c86a8', name: 'skia-g3-roll-check' },
    ],
  },
  conversation: { cascadeId: '' },
};

const subscribers = [];
const reduxStore = {
  getState() { return storeState; },
  dispatch(action) {
    if (action?.type === 'userStatus/setUserStatus') {
      storeState.userStatus = action.payload;
    } else if (action?.type === 'modelSelection/modelPicked') {
      const { conversationId, model, pick } = action.payload || {};
      if (conversationId) {
        storeState.modelSelection.byConversation[conversationId] = {
          model,
          contextWindow: undefined,
        };
      } else {
        storeState.modelSelection.default = { model };
      }
      if (pick) {
        const key = conversationId || '';
        storeState.modelSelection.trajectorySync[key] = {
          pick: { atStep: pick.atStep, queuedMessageIds: [...(pick.queuedMessageIds || [])] },
        };
      }
    } else if (action?.type === 'modelSelection/trajectoryModelObserved') {
      const { conversationId, model } = action.payload || {};
      if (conversationId) {
        storeState.modelSelection.byConversation[conversationId] = {
          model,
          contextWindow: undefined,
        };
      }
    }
    for (const fn of subscribers) fn();
    return action;
  },
  subscribe(fn) {
    subscribers.push(fn);
    return () => {};
  },
};
displayOptionsBtn.__reactFiber$test = { memoizedProps: { store: reduxStore } };

const NOW_ISO = '2026-10-29T18:30:00Z';
let fakeNow = Date.parse(NOW_ISO);
Date.now = () => fakeNow;

let outgoingFetchCalls = [];
// Back localStorage with a persistent Map so F5 reload assertions verify pin persistence
const autopilotStorage = new Map();
global.window = {
  innerWidth: 1280,
  location: { pathname: '/', search: '' },
  __APP_CONFIG__: { csrfToken: 'csrf-autopilot-test' },
  localStorage: {
    getItem: (k) => (autopilotStorage.has(k) ? autopilotStorage.get(k) : null),
    setItem: (k, v) => { autopilotStorage.set(k, String(v)); },
    removeItem: (k) => { autopilotStorage.delete(k); },
  },
  fetch: async (url, opts) => {
    const u = String(url);
    if (u.includes('GetAvailableModels')) {
      return { ok: true, json: async () => availableModelsPayload };
    }
    if (u.includes('SendUserCascadeMessage') || u.includes('StartCascade')) {
      outgoingFetchCalls.push({ url: u, body: opts?.body ? JSON.parse(opts.body) : null });
      return { ok: true, json: async () => ({}) };
    }
    return { ok: false, status: 404 };
  },
};
global.document = {
  documentElement: docEl,
  head: headEl,
  body: bodyEl,
  getElementById(id) { return docEl.querySelector(`#${id}`); },
  createElement(t) { return new FakeElement(t); },
  querySelector(s) { return docEl.querySelector(s); },
  querySelectorAll(s) { return docEl.querySelectorAll(s); },
  addEventListener() {},
  removeEventListener() {},
};
global.MutationObserver = class { observe() {} disconnect() {} };
global.requestAnimationFrame = (cb) => { cb(); return 1; };
let pendingTimers = new Map();
let nextTimerId = 1;
global.setTimeout = (fn, ms) => {
  const id = nextTimerId++;
  pendingTimers.set(id, fn);
  return id;
};
global.clearTimeout = (id) => {
  pendingTimers.delete(id);
};

(async () => {
  eval(code);
  await new Promise((r) => setImmediate(r));

  const configs = storeState.userStatus.cascadeModelConfigData.clientModelConfigs;
  const byModelId = new Map(configs.map((c) => [c.modelId, c]));

  // 1. Protobuf3 omitted remainingFraction quota verification
  const argon = byModelId.get('gemini-launch-candidate');
  assert(argon, 'Argon must be present in configs');
  assert.strictEqual(
    argon.tagTitle,
    '0%',
    `Argon with omitted remainingFraction and future resetTime must show 0%, got: ${argon.tagTitle}`
  );
  assert.strictEqual(argon.quotaInfo.remainingFraction, 0);

  const expired = byModelId.get('expired-model');
  assert(expired, 'Expired model must be present');
  assert.strictEqual(
    expired.tagTitle,
    'Hidden · 100%',
    `Expired resetTime must yield 100%, got: ${expired.tagTitle}`
  );

  const unmetered = byModelId.get('unmetered-model');
  assert(unmetered, 'Unmetered model must be present');
  assert.strictEqual(unmetered.tagTitle, 'Hidden · 100%');

  // 2. Draft chat auto-selection for SkParagraph (?section=<SkParagraph UUID> -> cpp-core)
  global.window.location.search = '?section=8acb67c4-2cb7-43ca-ba04-dc547b3c4788';
  reduxStore.dispatch({ type: 'NOOP' });
  await new Promise((r) => setImmediate(r));

  // In cpp-core, claude-bon (Opus 5.5 BoN High at 95%) must be picked over 3p-wtu-daily (56%)
  assert.strictEqual(
    storeState.modelSelection.default.model,
    1421,
    `cpp-core draft must auto-select Opus 5.5 BoN High (1421), got: ${storeState.modelSelection.default.model}`
  );

  // 3. Draft chat in skia-g3-roll-check (?section=<skia-g3-roll-check UUID> -> triage)
  global.window.location.search = '?section=3f5b7a12-8e5f-47d2-a2f4-d644447c86a8';
  reduxStore.dispatch({ type: 'NOOP' });
  await new Promise((r) => setImmediate(r));

  // In triage, Gemini 2.5 Flash (312 at 99.8%) must be picked over Gemini 3.8 Flash (50%)
  assert.strictEqual(
    storeState.modelSelection.default.model,
    312,
    `triage draft must auto-select Gemini 2.5 Flash (312), got: ${storeState.modelSelection.default.model}`
  );

  // 4. Existing conversation mid-chat failover: trajectoryModelObserved with exhausted model
  global.window.location.search = '';
  storeState.conversation.cascadeId = 'chat-cpp-1';
  reduxStore.dispatch({
    type: 'modelSelection/trajectoryModelObserved',
    payload: {
      conversationId: 'chat-cpp-1',
      turnKey: 'turn-1',
      label: 'Gemini 4 Argon (High)',
      model: 1393, // 0% exhausted WTU model
      executionStartStep: 0,
      queuedMessageIds: [],
    },
  });
  await new Promise((r) => setImmediate(r));

  // Must fail over to cpp-core healthy Opus 5.5 BoN High (1421)
  assert.strictEqual(
    storeState.modelSelection.byConversation['chat-cpp-1'].model,
    1421,
    `Exhausted Argon model must fail over to healthy Opus 1421, got: ${storeState.modelSelection.byConversation['chat-cpp-1']?.model}`
  );

  // Floating toast must mount
  const toast = global.document.getElementById('jetski-model-autopilot-toast');
  assert(toast, 'Autopilot floating toast must mount on failover');
  assert.strictEqual(toast.getAttribute('data-to-model'), 'Claude Opus 5.5 BoN (High)');

  // 5. Existing conversation mid-chat failover: wasteful WTU model (Fable 5.1 -> Opus 5.5)
  reduxStore.dispatch({
    type: 'modelSelection/trajectoryModelObserved',
    payload: {
      conversationId: 'chat-cpp-1',
      turnKey: 'turn-2',
      label: 'Claude Fable 5.1 (High)',
      model: 1388, // wasteful WTU model
      executionStartStep: 2,
      queuedMessageIds: [],
    },
  });
  await new Promise((r) => setImmediate(r));

  assert.strictEqual(
    storeState.modelSelection.byConversation['chat-cpp-1'].model,
    1421,
    `Wasteful Fable 5.1 model must shield to Opus 1421, got: ${storeState.modelSelection.byConversation['chat-cpp-1']?.model}`
  );

  // 6. Manual user pin: user explicitly picks Gemini 4 Carbon-G (1226, 99% quota)
  reduxStore.dispatch({
    type: 'modelSelection/modelPicked',
    payload: {
      conversationId: 'chat-cpp-1',
      model: 1226,
    },
  });
  await new Promise((r) => setImmediate(r));

  assert.strictEqual(storeState.modelSelection.byConversation['chat-cpp-1'].model, 1226);
  // Subsequent sync must respect user pin
  reduxStore.dispatch({ type: 'NOOP' });
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(
    storeState.modelSelection.byConversation['chat-cpp-1'].model,
    1226,
    'Manual user pin must be preserved across syncs'
  );

  // If user picks 0% dead model (1393), emergency floor (< 5%) rescues it
  reduxStore.dispatch({
    type: 'modelSelection/modelPicked',
    payload: {
      conversationId: 'chat-cpp-1',
      model: 1393,
    },
  });
  await new Promise((r) => setImmediate(r));
  assert.notStrictEqual(
    storeState.modelSelection.byConversation['chat-cpp-1'].model,
    1393,
    '0% model pick must trigger emergency rescue'
  );

  // 7. ConnectRPC fetch gate: outgoing SendUserCascadeMessage with 0% planModel rewritten
  outgoingFetchCalls = [];
  await global.window.fetch(
    '/exa.language_server_pb.LanguageServerService/SendUserCascadeMessage',
    {
      method: 'POST',
      body: JSON.stringify({
        cascadeId: 'chat-cpp-1',
        cascadeConfig: {
          plannerConfig: {
            planModel: 'MODEL_PLACEHOLDER_M393', // Argon at 0%
          },
        },
      }),
    }
  );
  assert.strictEqual(outgoingFetchCalls.length, 1);
  const rewrittenBody = outgoingFetchCalls[0].body;
  assert.notStrictEqual(
    rewrittenBody.cascadeConfig.plannerConfig.planModel,
    'MODEL_PLACEHOLDER_M393',
    'Fetch gate must rewrite 0% planModel before sending'
  );

  // 8. Draft manual pick survives reload on the same project draft, does not block other
  // projects, and transfers to the seeded conversation so the next draft gets fresh autopilot
  storeState.conversation.cascadeId = '';
  global.window.location.search = '?section=8acb67c4-2cb7-43ca-ba04-dc547b3c4788';
  reduxStore.dispatch({
    type: 'modelSelection/modelPicked',
    payload: { model: 1226 },
  });
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(storeState.modelSelection.default.model, 1226);

  // Simulate full browser reload (fresh window RAM, persisted localStorage)
  delete global.window.__jetskiManualPickedModels;
  storeState.modelSelection.default = { model: 1018 };
  eval(code);
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(
    storeState.modelSelection.default.model,
    1226,
    'Manual draft pick must survive window reload on the same project draft'
  );

  // Switching to triage project draft must not be blocked by SkParagraph draft manual pick
  global.window.location.search = '?section=3f5b7a12-8e5f-47d2-a2f4-d644447c86a8';
  reduxStore.dispatch({ type: 'NOOP' });
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(
    storeState.modelSelection.default.model,
    312,
    'Draft manual pick in one project must not block autopilot in another project'
  );

  // Back on SkParagraph draft, seeding a new conversation transfers the pin and frees the draft
  global.window.location.search = '?section=8acb67c4-2cb7-43ca-ba04-dc547b3c4788';
  reduxStore.dispatch({
    type: 'modelSelection/conversationSeeded',
    payload: { conversationId: 'seeded-from-draft', model: 1226 },
  });
  await new Promise((r) => setImmediate(r));
  // Next fresh draft in SkParagraph now gets autopilot Opus 5.5 BoN High (1421)
  reduxStore.dispatch({ type: 'NOOP' });
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(
    storeState.modelSelection.default.model,
    1421,
    'After seeding a conversation, the next draft must auto-select the profile model (1421)'
  );

  // 9. Existing conversation manual pick (including WTU model) survives full window reload
  // and is not reverted by trajectoryModelObserved re-observing the previous turn on load
  storeState.conversation.cascadeId = 'chat-cpp-1';
  reduxStore.dispatch({
    type: 'modelSelection/modelPicked',
    payload: {
      conversationId: 'chat-cpp-1',
      model: 1388, // User explicitly pins Fable 5.1 (WTU model) at stepCount=4
    },
  });
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(storeState.modelSelection.byConversation['chat-cpp-1'].model, 1388);

  // Simulate F5 window reload: RAM Map and Redux byConversation are wiped, localStorage remains
  delete global.window.__jetskiManualPickedModels;
  storeState.modelSelection.byConversation = {};
  storeState.modelSelection.trajectorySync = {};
  eval(code);
  await new Promise((r) => setImmediate(r));

  // On reload, Jetski's trajectory observer fires for the existing turn (executionStartStep: 2 <= atStep: 4)
  reduxStore.dispatch({
    type: 'modelSelection/trajectoryModelObserved',
    payload: {
      conversationId: 'chat-cpp-1',
      turnKey: 'turn-reload',
      label: 'Claude Opus 5.5 BoN (High)',
      model: 1421,
      executionStartStep: 2,
      queuedMessageIds: [],
    },
  });
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(
    storeState.modelSelection.byConversation['chat-cpp-1'].model,
    1388,
    'Manual pick in existing chat must survive F5 reload and ignore older turn trajectoryModelObserved'
  );

  // 10. Cleanup removes toast and clears state
  window.__jetskiClosedChatsFilterCleanup();
  assert.strictEqual(global.document.getElementById('jetski-model-autopilot-toast'), null);

  console.log('ALL_AUTOPILOT_TESTS_PASSED');
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
        f"Model autopilot JS harness failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )
    self.assertIn("ALL_AUTOPILOT_TESTS_PASSED", res.stdout)


if __name__ == "__main__":
  unittest.main()
