#!/usr/bin/env python3
"""Unit tests for the Auto dropdown option in jetski_closed_filter.js."""

import glob
import os
import shutil
import subprocess
import unittest

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(CONFIG_DIR, "scripts")


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


class TestAutopilotAutoDropdown(unittest.TestCase):

  def test_auto_dropdown_option_and_manual_pin_reset(self) -> None:
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
  querySelector(s) { return this.querySelectorAll(s)[0] || null; }
  querySelectorAll(s) {
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
      'opus-5.5-high-bon-le': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M421',
        apiProvider: 'API_PROVIDER_ANTHROPIC_VERTEX',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.95, resetTime: '2026-10-30T12:30:00Z' },
      },
      'bon-experiment-arm-226': {
        displayName: 'Gemini Pro Next',
        model: 'MODEL_PLACEHOLDER_M226',
        apiProvider: 'API_PROVIDER_GOOGLE_GEMINI',
        supportsImages: true,
        quotaInfo: { remainingFraction: 0.99 },
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
    default: { model: 1260 },
    overrideVersionId: { kind: 1 },
    byConversation: {},
    trajectorySync: {},
  },
  userStatus: makeServerUserStatus(),
  projects: {
    projects: [
      { id: '8acb67c4-2cb7-43ca-ba04-dc547b3c4788', name: 'SkParagraph · ● 1' },
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
  fetch: async (url) => {
    const u = String(url);
    if (u.includes('GetAvailableModels')) {
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
global.requestAnimationFrame = (cb) => { cb(); return 1; };
global.setTimeout = () => 1;
global.clearTimeout = () => {};

(async () => {
  eval(code);
  await new Promise((r) => setImmediate(r));

  const configs = storeState.userStatus.cascadeModelConfigData.clientModelConfigs;
  const labels =
    storeState.userStatus.cascadeModelConfigData.clientModelSorts[0].groups[0].modelLabels;

  // 1. Verify Auto presence in clientModelConfigs and Recommended sort
  const autoCfg = configs.find((c) => c && c.modelId === '__jetski_autopilot__');
  assert(autoCfg, 'Auto option must be injected into clientModelConfigs');
  assert.strictEqual(autoCfg.label, 'Auto');
  assert.strictEqual(autoCfg.tagTitle, 'Autopilot');
  assert.deepStrictEqual(autoCfg.modelOrAlias.choice, { case: 'model', value: 999999 });
  assert.strictEqual(labels[0], 'Auto', 'Auto must be the first option in the dropdown');
  assert.strictEqual(
    labels[1],
    'Claude Opus 5.5 BoN (High)',
    'Strongest model Opus 5.5 BoN High must immediately follow Auto'
  );

  // 2. User manually picks Claude Fable 5.1 (1388) in existing chat-cpp-1
  storeState.conversation.cascadeId = 'chat-cpp-1';
  reduxStore.dispatch({
    type: 'modelSelection/modelPicked',
    payload: {
      conversationId: 'chat-cpp-1',
      model: 1388,
    },
  });
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(storeState.modelSelection.byConversation['chat-cpp-1'].model, 1388);
  const rawPins = JSON.parse(autopilotStorage.get('jetski_manual_model_pins_v1') || '{}');
  assert.strictEqual(rawPins['chat-cpp-1']?.model, 1388, 'Manual pin must be saved in localStorage');

  // 3. User selects Auto (999999) in chat-cpp-1: pin is cleared and profile model restored
  reduxStore.dispatch({
    type: 'modelSelection/modelPicked',
    payload: {
      conversationId: 'chat-cpp-1',
      model: 999999,
    },
  });
  await new Promise((r) => setImmediate(r));

  const pinsAfterReset = JSON.parse(autopilotStorage.get('jetski_manual_model_pins_v1') || '{}');
  assert.strictEqual(
    pinsAfterReset['chat-cpp-1'],
    undefined,
    'Selecting Auto must delete manual pin from localStorage'
  );
  assert.strictEqual(
    storeState.modelSelection.byConversation['chat-cpp-1'].model,
    1421,
    `Selecting Auto must restore profile model Opus 5.5 BoN High (1421), got: ${
      storeState.modelSelection.byConversation['chat-cpp-1']?.model
    }`
  );
  assert.strictEqual(
    storeState.modelSelection.trajectorySync['chat-cpp-1']?.pick?.atStep,
    999999,
    'Synthetic pick at step 999999 must guard against older turn revert'
  );

  // Floating toast must show transition from Auto to profile model
  const toast = global.document.getElementById('jetski-model-autopilot-toast');
  assert(toast, 'Autopilot toast must mount when resetting via Auto');
  assert.strictEqual(toast.getAttribute('data-to-model'), 'Claude Opus 5.5 BoN (High)');

  // 4. Draft chat manual pin reset via Auto
  storeState.conversation.cascadeId = '';
  global.window.location.search = '?section=8acb67c4-2cb7-43ca-ba04-dc547b3c4788';
  reduxStore.dispatch({
    type: 'modelSelection/modelPicked',
    payload: { model: 1226 },
  });
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(storeState.modelSelection.default.model, 1226);
  const draftPins = JSON.parse(autopilotStorage.get('jetski_manual_model_pins_v1') || '{}');
  assert(draftPins['__draft__:8acb67c4-2cb7-43ca-ba04-dc547b3c4788']);

  // Selecting Auto on draft clears draft pin and restores Opus 5.5 BoN High (1421)
  reduxStore.dispatch({
    type: 'modelSelection/modelPicked',
    payload: { model: 999999 },
  });
  await new Promise((r) => setImmediate(r));

  const draftPinsAfter = JSON.parse(autopilotStorage.get('jetski_manual_model_pins_v1') || '{}');
  assert.strictEqual(draftPinsAfter['__draft__:8acb67c4-2cb7-43ca-ba04-dc547b3c4788'], undefined);
  assert.strictEqual(
    storeState.modelSelection.default.model,
    1421,
    'Selecting Auto on draft must restore profile model (1421)'
  );

  // 5. Cleanup removes Auto and restores native configs
  window.__jetskiClosedChatsFilterCleanup();
  const restoredConfigs = storeState.userStatus.cascadeModelConfigData.clientModelConfigs;
  assert.strictEqual(restoredConfigs.length, 4, 'Cleanup must restore original 4 native configs');
  assert(
    !restoredConfigs.some((c) => c && c.modelId === '__jetski_autopilot__'),
    'Auto must be stripped from configs on cleanup'
  );

  console.log('AUTO_DROPDOWN_TESTS_PASSED');
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
        f"Auto dropdown harness failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )
    self.assertIn("AUTO_DROPDOWN_TESTS_PASSED", res.stdout)


if __name__ == "__main__":
  unittest.main()
