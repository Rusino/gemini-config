#!/usr/bin/env python3
"""Contract tests for topological review queue ordering and parent readiness gates."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(TESTS_DIR, "..", "scripts")
TRIAGE_SCRIPTS_DIR = os.path.join(
    TESTS_DIR, "..", "skills", "review-triage", "scripts"
)
CANARY_SCRIPTS_DIR = os.path.join(
    TESTS_DIR, "..", "skills", "skia-canary-advisor", "scripts"
)

for d in (SCRIPTS_DIR, TRIAGE_SCRIPTS_DIR, CANARY_SCRIPTS_DIR):
  if d not in sys.path:
    sys.path.insert(0, d)

import canary_advisor  # pylint: disable=g-import-not-at-top
import cl_watcher as w  # pylint: disable=g-import-not-at-top
import run_queue_triage as rqt  # pylint: disable=g-import-not-at-top
import triage_store  # pylint: disable=g-import-not-at-top


class ReviewQueueOrderAndReadinessTest(unittest.TestCase):

  def setUp(self):
    super().setUp()
    w._RELATED_CLS_CACHE.clear()
    w._RELATED_STACK_ORDER_CACHE.clear()

  def test_gerrit_stack_ordering_and_fifo(self):
    host = "skia-review.googlesource.com"
    # Stack: 1412376 (root) -> 1412377 (child) -> 1412378 (grandchild)
    w._RELATED_STACK_ORDER_CACHE[f"{host}:1412376"] = [
        "1412376",
        "1412377",
        "1412378",
    ]
    w._RELATED_STACK_ORDER_CACHE[f"{host}:1412377"] = [
        "1412376",
        "1412377",
        "1412378",
    ]
    w._RELATED_STACK_ORDER_CACHE[f"{host}:1412378"] = [
        "1412376",
        "1412377",
        "1412378",
    ]

    # Present changes in reverse order alongside an older independent CL
    items = [
        {
            "item_key": "gerrit:1412378",
            "kind": "gerrit",
            "target_id": "1412378",
            "preferred_host": host,
        },
        {
            "item_key": "gerrit:1412377",
            "kind": "gerrit",
            "target_id": "1412377",
            "preferred_host": host,
        },
        {
            "item_key": "gerrit:1412376",
            "kind": "gerrit",
            "target_id": "1412376",
            "preferred_host": host,
        },
        {
            "item_key": "gerrit:1400000",
            "kind": "gerrit",
            "target_id": "1400000",
            "preferred_host": host,
        },
    ]
    cache = {
        "gerrit:1412378": {"created": "2026-10-31 12:00:00.000000000", "patchset": 1},
        "gerrit:1412377": {"created": "2026-10-31 11:30:00.000000000", "patchset": 1},
        "gerrit:1412376": {"created": "2026-10-31 11:00:00.000000000", "patchset": 1},
        "gerrit:1400000": {"created": "2026-10-31 09:00:00.000000000", "patchset": 1},
    }

    ordered = w.order_and_link_stack_changes(items, cache)
    tids = [d["target_id"] for d in ordered]

    # 1400000 is depth 0 and older; 1412376 is depth 0; 1412377 is depth 1; 1412378 is depth 2
    self.assertEqual(tids, ["1400000", "1412376", "1412377", "1412378"])

    d00 = next(d for d in ordered if d["target_id"] == "1400000")
    d76 = next(d for d in ordered if d["target_id"] == "1412376")
    d77 = next(d for d in ordered if d["target_id"] == "1412377")
    d78 = next(d for d in ordered if d["target_id"] == "1412378")

    self.assertEqual(d76["stack_depth"], 0)
    self.assertEqual(d76["stack_ancestors"], [])
    self.assertEqual(d76["stack_parent_ikey"], "")
    self.assertEqual(d76["stack_root_ikey"], "gerrit:1412376")
    self.assertEqual(d76["stack_size"], 3)
    self.assertEqual(d76["stack_index"], 1)
    self.assertEqual(
        d76["stack_ikeys"],
        ["gerrit:1412376", "gerrit:1412377", "gerrit:1412378"],
    )

    self.assertEqual(d77["stack_depth"], 1)
    self.assertEqual(d77["stack_ancestors"], ["gerrit:1412376"])
    self.assertEqual(d77["stack_parent_ikey"], "gerrit:1412376")
    self.assertEqual(d77["stack_root_ikey"], "gerrit:1412376")
    self.assertEqual(d77["stack_size"], 3)
    self.assertEqual(d77["stack_index"], 2)
    self.assertEqual(
        d77["stack_ikeys"],
        ["gerrit:1412376", "gerrit:1412377", "gerrit:1412378"],
    )

    self.assertEqual(d78["stack_depth"], 2)
    self.assertEqual(d78["stack_ancestors"], ["gerrit:1412376", "gerrit:1412377"])
    self.assertEqual(d78["stack_parent_ikey"], "gerrit:1412377")
    self.assertEqual(d78["stack_root_ikey"], "gerrit:1412376")
    self.assertEqual(d78["stack_size"], 3)
    self.assertEqual(d78["stack_index"], 3)
    self.assertEqual(
        d78["stack_ikeys"],
        ["gerrit:1412376", "gerrit:1412377", "gerrit:1412378"],
    )

    self.assertEqual(d00["stack_root_ikey"], "")
    self.assertEqual(d00["stack_size"], 1)
    self.assertEqual(d00["stack_index"], 0)
    self.assertEqual(d00["stack_ikeys"], [])

  def test_parent_readiness_barrier_blocks_descendants_until_triage_or_review(self):
    host = "skia-review.googlesource.com"
    w._RELATED_STACK_ORDER_CACHE[f"{host}:1412376"] = [
        "1412376",
        "1412377",
        "1412378",
    ]
    w._RELATED_STACK_ORDER_CACHE[f"{host}:1412377"] = [
        "1412376",
        "1412377",
        "1412378",
    ]
    w._RELATED_STACK_ORDER_CACHE[f"{host}:1412378"] = [
        "1412376",
        "1412377",
        "1412378",
    ]

    items = [
        {
            "item_key": "gerrit:1412376",
            "kind": "gerrit",
            "target_id": "1412376",
            "preferred_host": host,
            "role": "Reviewer",
        },
        {
            "item_key": "gerrit:1412377",
            "kind": "gerrit",
            "target_id": "1412377",
            "preferred_host": host,
            "role": "Reviewer",
        },
        {
            "item_key": "gerrit:1412378",
            "kind": "gerrit",
            "target_id": "1412378",
            "preferred_host": host,
            "role": "Reviewer",
        },
    ]
    cache = {
        "gerrit:1412376": {"status": "NEW", "patchset": 1, "in_my_attention": True},
        "gerrit:1412377": {"status": "NEW", "patchset": 1, "in_my_attention": True},
        "gerrit:1412378": {"status": "NEW", "patchset": 1, "in_my_attention": True},
    }

    w.order_and_link_stack_changes(items, cache)
    by_ikey = {d["item_key"]: d for d in items}

    # Root CL has no unready ancestors
    self.assertFalse(
        w._has_unready_open_stack_parent(
            "gerrit:1412376", by_ikey, cache, {}, {}
        )
    )

    # Descendants are blocked while root is untriaged
    with mock.patch.object(triage_store, "load_triage_result", return_value=None):
      self.assertTrue(
          w._has_unready_open_stack_parent(
              "gerrit:1412377", by_ikey, cache, {}, {}
          )
      )
      self.assertTrue(
          w._has_unready_open_stack_parent(
              "gerrit:1412378", by_ikey, cache, {}, {}
          )
      )

    # When root has a saved triage verdict for PS1, 1412377 is unblocked
    def fake_load_triage(kind, tid):
      if str(tid) == "1412376":
        return {"patchset": 1, "verdict": "Clean"}
      return None

    with mock.patch.object(
        triage_store, "load_triage_result", side_effect=fake_load_triage
    ):
      self.assertFalse(
          w._has_unready_open_stack_parent(
              "gerrit:1412377", by_ikey, cache, {}, {}
          )
      )
      # 1412378 is still blocked because 1412377 is not yet triaged
      self.assertTrue(
          w._has_unready_open_stack_parent(
              "gerrit:1412378", by_ikey, cache, {}, {}
          )
      )

    # When 1412376's chat is busy running review, 1412377 is blocked again
    new_items_busy = {
        "gerrit:1412376": {
            "item_key": "gerrit:1412376",
            "chat_busy": True,
            "status": "NEW",
            "patchset": 1,
        }
    }
    with mock.patch.object(
        triage_store, "load_triage_result", side_effect=fake_load_triage
    ):
      self.assertTrue(
          w._has_unready_open_stack_parent(
              "gerrit:1412377", by_ikey, cache, new_items_busy, {}
          )
      )

    # When 1412376 is MERGED, 1412377 is unblocked regardless of triage_store
    cache_merged = {
        "gerrit:1412376": {"status": "MERGED", "patchset": 1},
        "gerrit:1412377": {"status": "NEW", "patchset": 1},
        "gerrit:1412378": {"status": "NEW", "patchset": 1},
    }
    with mock.patch.object(triage_store, "load_triage_result", return_value=None):
      self.assertFalse(
          w._has_unready_open_stack_parent(
              "gerrit:1412377", by_ikey, cache_merged, {}, {}
          )
      )

  def test_github_stacked_prs_ordering_and_depth(self):
    items = [
        {
            "item_key": "github:303",
            "kind": "github",
            "target_id": "303",
            "head_ref": "branch-c",
            "base_ref": "branch-b",
            "created": "2026-10-31T12:00:00Z",
        },
        {
            "item_key": "github:301",
            "kind": "github",
            "target_id": "301",
            "head_ref": "branch-a",
            "base_ref": "master",
            "created": "2026-10-31T10:00:00Z",
        },
        {
            "item_key": "github:302",
            "kind": "github",
            "target_id": "302",
            "head_ref": "branch-b",
            "base_ref": "branch-a",
            "created": "2026-10-31T11:00:00Z",
        },
    ]
    cache = {d["item_key"]: d for d in items}

    ordered = w.order_and_link_stack_changes(items, cache)
    tids = [d["target_id"] for d in ordered]

    self.assertEqual(tids, ["301", "302", "303"])
    self.assertEqual(ordered[0]["stack_depth"], 0)
    self.assertEqual(ordered[0]["stack_root_ikey"], "github:301")
    self.assertEqual(ordered[0]["stack_size"], 3)
    self.assertEqual(ordered[0]["stack_index"], 1)
    self.assertEqual(
        ordered[0]["stack_ikeys"], ["github:301", "github:302", "github:303"]
    )

    self.assertEqual(ordered[1]["stack_depth"], 1)
    self.assertEqual(ordered[1]["stack_parent_ikey"], "github:301")
    self.assertEqual(ordered[1]["stack_root_ikey"], "github:301")
    self.assertEqual(ordered[1]["stack_size"], 3)
    self.assertEqual(ordered[1]["stack_index"], 2)

    self.assertEqual(ordered[2]["stack_depth"], 2)
    self.assertEqual(ordered[2]["stack_parent_ikey"], "github:302")
    self.assertEqual(
        ordered[2]["stack_ancestors"], ["github:301", "github:302"]
    )
    self.assertEqual(ordered[2]["stack_root_ikey"], "github:301")
    self.assertEqual(ordered[2]["stack_size"], 3)
    self.assertEqual(ordered[2]["stack_index"], 3)

  def test_run_queue_triage_topological_sort(self):
    dated_changes = [
        (
            1700000002,
            {
                "_number": 202,
                "_gerrit_host": "skia-review.googlesource.com",
                "current_revision": "sha2",
                "revisions": {
                    "sha2": {
                        "commit": {
                            "commit": "sha2",
                            "parents": [{"commit": "sha1"}],
                        }
                    }
                },
            },
        ),
        (
            1700000001,
            {
                "_number": 201,
                "_gerrit_host": "skia-review.googlesource.com",
                "current_revision": "sha1",
                "revisions": {
                    "sha1": {
                        "commit": {
                            "commit": "sha1",
                            "parents": [{"commit": "sha0"}],
                        }
                    }
                },
            },
        ),
    ]

    rqt.sort_gerrit_changes_topological_fifo(dated_changes)
    result_nums = [c[1]["_number"] for c in dated_changes]
    self.assertEqual(result_nums, [201, 202])

  def test_gerrit_reads_fallback_to_authenticated_endpoint_on_http_429(self):
    import urllib.error

    class _FakeHttpResp:

      def __init__(self, payload: str, status: int = 200):
        self._payload = payload
        self.status = status

      def read(self) -> bytes:
        return self._payload.encode("utf-8")

      def __enter__(self):
        return self

      def __exit__(self, exc_type, exc, tb):
        return False

    seen = []

    def fake_urlopen(req, timeout=8):
      seen.append((req.full_url, req.headers.get("Authorization")))
      if "/a/" not in req.full_url:
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {}, None
        )
      return _FakeHttpResp(
          ")]}'\n{\"/PATCHSET_LEVEL\": [{\"id\": \"c1\", \"patch_set\": 1,"
          " \"unresolved\": false, \"author\": {\"email\": \"jlavrova@google.com\"},"
          " \"message\": \"Looks good to me.\"}]}"
      )

    with mock.patch.object(
        canary_advisor, "_gerrit_luci_token", return_value="luci-tok-123"
    ):
      with mock.patch.object(
          w.urllib.request, "urlopen", side_effect=fake_urlopen
      ):
        comments = w.fetch_gerrit_comments(w.GERRIT_SKIA_HOST, "1414017")

    self.assertIn("/PATCHSET_LEVEL", comments)
    self.assertEqual(len(seen), 2)
    self.assertNotIn("/a/", seen[0][0])
    self.assertIn("/a/changes/1414017/comments", seen[1][0])
    self.assertEqual(seen[1][1], "Bearer luci-tok-123")

  def test_dashboard_variant_c_stack_grouping_and_blocker_banner(self):
    # Pass DASHBOARD_HTML via stdin to stay below Linux 128 KB MAX_ARG_STRLEN
    js_code = r"""
const assert = require('assert');
const html = require('fs').readFileSync(0, 'utf8');
const script = html.split('<script>')[1].split('</script>')[0].replace(/boot\(\);/g, '');
eval(script);

const testItems = [
  {
    item_key: 'gerrit:1412376',
    target_id: '1412376',
    kind: 'gerrit',
    project_name: 'SkParagraph',
    subject: '[SkParagraph] Root feature step 1',
    url: 'https://skia-review.googlesource.com/c/skia/+/1412376',
    patchset: 1,
    status: 'NEW',
    in_my_attention: true,
    stack_ancestors: [],
    stack_parent_ikey: '',
    stack_root_ikey: 'gerrit:1412376',
    stack_size: 3,
    stack_index: 1,
    stack_ikeys: ['gerrit:1412376', 'gerrit:1412377', 'gerrit:1412378'],
    triage: {verdict: 'NITS', verdict_label: '🔵 Minor nits', review_outcome: 'NITS'}
  },
  {
    item_key: 'gerrit:1412377',
    target_id: '1412377',
    kind: 'gerrit',
    project_name: 'SkParagraph',
    subject: '[SkParagraph] Child step 2',
    url: 'https://skia-review.googlesource.com/c/skia/+/1412377',
    patchset: 2,
    status: 'NEW',
    in_my_attention: false,
    self_approved: true,
    self_review_state: 'approved',
    conversation_id: 'conv-1412377',
    stack_ancestors: ['gerrit:1412376'],
    stack_parent_ikey: 'gerrit:1412376',
    stack_root_ikey: 'gerrit:1412376',
    stack_size: 3,
    stack_index: 2,
    stack_ikeys: ['gerrit:1412376', 'gerrit:1412377', 'gerrit:1412378']
  },
  {
    item_key: 'gerrit:1412378',
    target_id: '1412378',
    kind: 'gerrit',
    project_name: 'SkParagraph',
    subject: '[SkParagraph] Grandchild step 3',
    url: 'https://skia-review.googlesource.com/c/skia/+/1412378',
    patchset: 1,
    status: 'NEW',
    in_my_attention: true,
    stack_ancestors: ['gerrit:1412376', 'gerrit:1412377'],
    stack_parent_ikey: 'gerrit:1412377',
    stack_root_ikey: 'gerrit:1412376',
    stack_size: 3,
    stack_index: 3,
    stack_ikeys: ['gerrit:1412376', 'gerrit:1412377', 'gerrit:1412378']
  }
];

const mockData = {
  jetski_box_url: 'http://localhost:8089',
  items: {
    'gerrit:1412376': testItems[0],
    'gerrit:1412377': testItems[1],
    'gerrit:1412378': testItems[2]
  }
};

buildStackContext(testItems);

// 1. Verify badgesHtml contains chain position
assert(badgesHtml(testItems[0]).includes('⛓ 1/3'), 'Must contain ⛓ 1/3 badge');
assert(badgesHtml(testItems[1]).includes('⛓ 2/3'), 'Must contain ⛓ 2/3 badge');
assert(badgesHtml(testItems[2]).includes('⛓ 3/3'), 'Must contain ⛓ 3/3 badge');

// 2. Verify upstreamBlockersOf
const blockersRoot = upstreamBlockersOf(testItems[0], mockData);
assert.strictEqual(blockersRoot.length, 0, 'Root must have no upstream blockers');

const blockers77 = upstreamBlockersOf(testItems[1], mockData);
assert.strictEqual(blockers77.length, 1, 'Child must have 1 blocker (root with NITS)');
assert.strictEqual(blockers77[0].ancestor.target_id, '1412376');
assert(blockers77[0].reasons.includes('NITS'), 'Reason must include NITS');

const banner77 = stackBlockerBannerHtml(testItems[1], mockData);
assert(banner77.includes('Parent holds stack:'), 'Must render blocker banner on child');
assert(
    banner77.includes('[1/3] CL 1412376'),
    'Banner must reference parent with step: ' + banner77,
);
assert(banner77.includes('(NITS)'), 'Banner must reference parent reason: ' + banner77);

const bannerRoot = stackBlockerBannerHtml(testItems[0], mockData);
assert.strictEqual(bannerRoot, '', 'Root must not render blocker banner');

// 3. Verify renderFocusSection grouping and spine
const youItems = [testItems[0], testItems[2]];
const focusHtml = renderFocusSection(youItems, testItems, mockData);

assert(focusHtml.includes('class="stack-group"'), 'Must render stack-group container');
assert(focusHtml.includes('🔗 Stack (3 CLs)'), 'Must render Stack (3 CLs) header');
assert(focusHtml.includes('class="stack-spine"'), 'Must render stack-spine');
assert(focusHtml.includes('[1/3 🔵 NITS]'), 'Must render step 1 with NITS in spine');
assert(focusHtml.includes('[2/3 ✓ CR+1]'), 'Must render step 2 with CR+1 in spine');
assert(
    focusHtml.includes('details class="stack-step-collapsed"'),
    'Sibling in waiting must be collapsed',
);
assert(
    focusHtml.includes('href="https://skia-review.googlesource.com/c/skia/+/1412377"'),
    'Collapsed sibling must render direct CL link to Gerrit',
);
assert(
    focusHtml.includes('class="stack-step-chat"'),
    'Collapsed sibling must render stack-step-chat span',
);
assert(
    focusHtml.includes('conv-1412377'),
    'Collapsed sibling must render review chat link with conversation ID',
);
assert(
    focusHtml.includes('review chat'),
    'Collapsed sibling must render review chat button text',
);
assert(focusHtml.includes('CL 1412377'), 'Collapsed sibling must be 1412377');

// Verify that rowHtml renders CL label as a link to it.url
const row77 = rowHtml(testItems[1], mockData);
assert(
    row77.includes('<span class="cl"><a href="https://skia-review.googlesource.com/c/skia/+/1412377"'),
    'rowHtml must render CL identifier as a link to review page: ' + row77,
);

// 4. Verify lane rendering grouping
const laneItems = [testItems[0], testItems[2]];
const laneHtml = renderLaneItemsHtml(laneItems, mockData);
assert(laneHtml.includes('class="stack-lane-group"'), 'Must group multi-item stack in lane');
assert(laneHtml.includes('🔗 Stack (2 of 3 CLs)'), 'Must render stack lane header');

console.log('Variant C Dashboard JS tests passed');
process.exit(0);
"""
    cmd = ["node", "-e", js_code]
    res = subprocess.run(
        cmd, input=w.DASHBOARD_HTML, capture_output=True, text=True, check=False
    )
    self.assertEqual(
        res.returncode, 0, f"Node output: {res.stdout}\nError: {res.stderr}"
    )

  def test_export_flight_deck_info_ui_version(self):
    import json
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
      info_file = os.path.join(tmpdir, "jetski-flight-deck.json")
      with mock.patch.object(w, "FLIGHT_DECK_INFO_FILE", info_file):
        with mock.patch.object(w, "_INITIAL_SCRIPT_MTIME", 1730510000.5):
          with mock.patch.object(
              w, "flight_deck_pen_url", return_value="https://pen.goog"
          ):
            w.export_flight_deck_info(port=8089)
            with open(info_file, "r", encoding="utf-8") as f:
              data = json.load(f)
            self.assertEqual(data.get("ui_version"), 1730510000)
            self.assertIn("?v=1730510000", data.get("local_url", ""))
            self.assertIn("?v=1730510000", data.get("pen_url", ""))

  def test_flight_deck_pen_url_preserves_https_on_loopback_timeout(self):
    import json
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
      info_file = os.path.join(tmpdir, "jetski-flight-deck.json")
      with open(info_file, "w", encoding="utf-8") as f:
        json.dump(
            {"pen_url": "https://8089-pen.proxy.googlers.com?v=100"}, f
        )
      with mock.patch.dict(w._FLIGHT_DECK_PEN_URL, {}, clear=True):
        with mock.patch.object(w, "FLIGHT_DECK_INFO_FILE", info_file):
          with mock.patch.object(
              w,
              "get_pen_url",
              side_effect=[
                  "http://localhost:8089",
                  "https://8089-fresh.proxy.googlers.com",
              ],
          ):
            first = w.flight_deck_pen_url(8089)
            self.assertEqual(first, "https://8089-pen.proxy.googlers.com")
            self.assertNotIn(8089, w._FLIGHT_DECK_PEN_URL)
            second = w.flight_deck_pen_url(8089)
            self.assertEqual(second, "https://8089-fresh.proxy.googlers.com")
            self.assertEqual(
                w._FLIGHT_DECK_PEN_URL.get(8089),
                "https://8089-fresh.proxy.googlers.com",
            )

  def test_flight_deck_embedded_rendered_heartbeat_ping_and_refresh(self):
    # Pass DASHBOARD_HTML via stdin to stay below Linux 128 KB MAX_ARG_STRLEN
    js_code = r"""
const assert = require('assert');
const html = require('fs').readFileSync(0, 'utf8');
const script = html.split('<script>')[1].split('</script>')[0].replace(/boot\(\);/g, '');

const els = new Map();
function getEl(id) {
  if (!els.has(id)) {
    els.set(id, { id, textContent: id === 'synced' ? 'Loading…' : '', innerHTML: '' });
  }
  return els.get(id);
}
const parentMsgs = [];
const winListeners = [];
let reloadCalls = 0;
let statusCalls = 0;
let statusMode = 'fail_always';
let currentUiVer = 501;

global.document = {
  documentElement: { classList: { add() {} } },
  getElementById: getEl,
  addEventListener() {},
};
global.history = { replaceState() {} };
global.location = {
  search: '?embed=1',
  hash: '',
  reload() { reloadCalls++; },
};
global.window = {
  parent: {
    postMessage(msg) { parentMsgs.push(msg); },
  },
  addEventListener(type, fn) {
    if (type === 'message') winListeners.push(fn);
  },
};
global.fetch = async (url, opts) => {
  if (url === '/api/status') {
    statusCalls++;
    assert(opts && opts.signal, 'fetch(/api/status) must pass an AbortSignal');
    if (statusMode === 'fail_once') {
      statusMode = 'ok';
      return { ok: false, status: 502, json: async () => ({}) };
    }
    if (statusMode === 'fail_always') {
      throw new Error('ECONNREFUSED');
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({
        ui_version: currentUiVer,
        last_sync: '19:40 ET',
        items: {},
        bugs: {},
        rollers_summary: {},
      }),
    };
  }
  if (url === '/api/refresh') {
    statusMode = 'fail_once';
    currentUiVer = 502;
    return {
      ok: true,
      status: 200,
      json: async () => ({ ok: true, reloaded: true }),
    };
  }
  throw new Error('Unexpected fetch: ' + url);
};

const origSetInterval = global.setInterval;
const origSetTimeout = global.setTimeout;
global.setInterval = () => 0;

async function flushMicrotasks() {
  for (let i = 0; i < 8; i++) await Promise.resolve();
}

eval(script);
boot();

(async () => {
  await flushMicrotasks();
  // While initial fetch fails (stuck on Loading), boot() + ping must NOT claim rendered
  assert(winListeners.length >= 1, 'boot() must listen for parent ping messages');
  for (const fn of winListeners) {
    fn({ data: { type: 'jetski-flight-deck-ping' } });
  }
  await flushMicrotasks();
  assert.strictEqual(
    parentMsgs.filter((m) => m.type === 'jetski-flight-deck-rendered').length,
    0,
    'Unrendered iframe must not claim rendered on ping while fetch fails',
  );

  // Once /api/status recovers, a parent ping triggers fetchState() and posts rendered heartbeat
  statusMode = 'ok';
  for (const fn of winListeners) {
    fn({ data: { type: 'jetski-flight-deck-ping' } });
  }
  await flushMicrotasks();
  const renderedCls = parentMsgs.filter((m) => m.type === 'jetski-flight-deck-rendered');
  assert.strictEqual(renderedCls.length, 1, 'Recovered iframe must post rendered heartbeat');
  assert.strictEqual(renderedCls[0].uiVersion, 501);
  assert.strictEqual(renderedCls[0].synced, '19:40 ET');

  // Bugs view has its own branch in render() and must also post the rendered heartbeat
  setView('bugs');
  const renderedAfterBugs = parentMsgs.filter((m) => m.type === 'jetski-flight-deck-rendered');
  assert.strictEqual(renderedAfterBugs.length, 2, 'Bugs view render() must post heartbeat');

  // Parent liveness ping acks immediately when cachedData is present and refreshes status
  const callsBeforePing = statusCalls;
  for (const fn of winListeners) {
    fn({ data: { type: 'jetski-flight-deck-ping' } });
  }
  assert.strictEqual(
    parentMsgs.filter((m) => m.type === 'jetski-flight-deck-rendered').length,
    3,
    'Ping must synchronously ack rendered state when cachedData exists',
  );
  await flushMicrotasks();
  assert(statusCalls > callsBeforePing, 'Ping must trigger a background status refresh');

  // Manual refresh with reloaded=true waits until /api/status returns 200 before reloading
  statusMode = 'ok';
  global.setTimeout = (fn, ms) => (ms <= 200 ? (fn(), 0) : origSetTimeout(fn, ms));
  await triggerRefresh();
  global.setTimeout = origSetTimeout;
  assert.strictEqual(reloadCalls, 1, 'triggerRefresh must reload after backend comes back');

  global.setInterval = origSetInterval;
  console.log('EMBEDDED_HEARTBEAT_TESTS_PASSED');
  process.exit(0);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    cmd = ["node", "-e", js_code]
    res = subprocess.run(
        cmd, input=w.DASHBOARD_HTML, capture_output=True, text=True, check=False
    )
    self.assertEqual(
        res.returncode, 0, f"Node output: {res.stdout}\nError: {res.stderr}"
    )
    self.assertIn("EMBEDDED_HEARTBEAT_TESTS_PASSED", res.stdout)

  def test_save_watcher_state_guards_flight_deck_info_and_rollback_mtime(self):
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
      info_file = os.path.join(tmpdir, "jetski-flight-deck.json")
      prod_state_file = os.path.join(tmpdir, "prod_state.json")
      mock_state_file = os.path.join(tmpdir, "mock_state.json")
      with open(info_file, "w", encoding="utf-8") as f:
        json.dump({"ui_version": 100, "port": 8089}, f)

      with (
          mock.patch.object(w, "FLIGHT_DECK_INFO_FILE", info_file),
          mock.patch.object(w, "DEFAULT_STATE_FILE", prod_state_file, create=True),
          mock.patch.object(w, "_INITIAL_SCRIPT_MTIME", 200.0),
          mock.patch.object(
              w, "flight_deck_pen_url", return_value="https://pen.goog"
          ),
      ):
        # Redirected STATE_FILE in unit tests must not overwrite FLIGHT_DECK_INFO_FILE
        with (
            mock.patch.object(w, "STATE_FILE", mock_state_file),
            mock.patch.object(w, "_SERVING_PORT", 8089, create=True),
        ):
          w.save_watcher_state({"items": {}, "events": [], "last_sync": ""})
        with open(info_file, "r", encoding="utf-8") as f:
          self.assertEqual(json.load(f).get("ui_version"), 100)

        # Non-daemon CLI run (_SERVING_PORT == 0) must not clobber existing daemon ui_version
        with (
            mock.patch.object(w, "STATE_FILE", prod_state_file),
            mock.patch.object(w, "_SERVING_PORT", 0, create=True),
        ):
          w.save_watcher_state({"items": {}, "events": [], "last_sync": ""})
        with open(info_file, "r", encoding="utf-8") as f:
          self.assertEqual(json.load(f).get("ui_version"), 100)

        # Active --serve daemon (_SERVING_PORT == 8089) updates FLIGHT_DECK_INFO_FILE
        with (
            mock.patch.object(w, "STATE_FILE", prod_state_file),
            mock.patch.object(w, "_SERVING_PORT", 8089, create=True),
        ):
          w.save_watcher_state({"items": {}, "events": [], "last_sync": ""})
        with open(info_file, "r", encoding="utf-8") as f:
          self.assertEqual(json.load(f).get("ui_version"), 200)

      script_file = os.path.join(tmpdir, "sample_watcher.py")
      with open(script_file, "w", encoding="utf-8") as f:
        f.write("x = 1\n")
      base_mtime = os.path.getmtime(script_file)
      os.utime(script_file, (base_mtime - 10.0, base_mtime - 10.0))
      self.assertTrue(
          w.script_changed_on_disk(script_file, base_mtime),
          "Mtime rollback (git checkout) must also trigger daemon reload",
      )

  def test_dashboard_refresh_60s_noop_skip_and_ui_state_persistence(self):
    js_code = r"""
const assert = require('assert');
const html = require('fs').readFileSync(0, 'utf8');
const script = html.split('<script>')[1].split('</script>')[0].replace(/boot\(\);/g, '');

const storageMap = new Map();
global.localStorage = {
  getItem(k) { return storageMap.has(k) ? storageMap.get(k) : null; },
  setItem(k, v) { storageMap.set(String(k), String(v)); },
};

let lanesWrites = 0;
let focusWrites = 0;
const els = new Map();
function getEl(id) {
  if (!els.has(id)) {
    let inner = '';
    const obj = {
      id,
      textContent: id === 'synced' ? 'Loading…' : '',
      get innerHTML() { return inner; },
      set innerHTML(v) {
        inner = String(v);
        if (id === 'lanes') lanesWrites++;
        if (id === 'focus') focusWrites++;
      },
    };
    els.set(id, obj);
  }
  return els.get(id);
}

let detailNodes = [];
let confirmingNode = null;
const docListeners = new Map();
global.document = {
  documentElement: { scrollTop: 0, classList: { add() {} } },
  getElementById: getEl,
  addEventListener(type, fn) { docListeners.set(type, fn); },
  querySelector(sel) {
    if (sel === '[data-confirming="1"]') return confirmingNode;
    return null;
  },
  querySelectorAll(sel) {
    if (sel === 'details[data-details-key]') return detailNodes;
    return [];
  },
};

global.location = { search: '?embed=1', hash: '', reload() {} };
global.history = {
  replaceState(_s, _t, url) { global.location.hash = String(url || ''); },
};

const parentMsgs = [];
let scrollCalls = [];
global.window = {
  scrollY: 0,
  scrollTo(x, y) {
    scrollCalls.push([x, y]);
    global.window.scrollY = y;
    global.document.documentElement.scrollTop = y;
  },
  parent: { postMessage(msg) { parentMsgs.push(msg); } },
  addEventListener() {},
};

let statusPayload = {
  ui_version: 700,
  last_sync: '12:00 ET',
  rollers_summary: {},
  items: {
    'gerrit:100': {
      item_key: 'gerrit:100', kind: 'gerrit', target_id: '100',
      project_name: 'Skia', subject: 'Root CL', status: 'NEW',
      url: 'https://skia-review.googlesource.com/c/skia/+/100',
      in_my_attention: false, is_self: false, role: 'reviewer',
      stack_root_ikey: 'gerrit:100', stack_index: 1, stack_size: 2,
      stack_ikeys: ['gerrit:100', 'gerrit:101'], activity_ts: 10,
    },
    'gerrit:101': {
      item_key: 'gerrit:101', kind: 'gerrit', target_id: '101',
      project_name: 'Skia', subject: 'Child CL', status: 'NEW',
      url: 'https://skia-review.googlesource.com/c/skia/+/101',
      in_my_attention: true, is_self: false, role: 'reviewer',
      stack_root_ikey: 'gerrit:100', stack_parent_ikey: 'gerrit:100',
      stack_ancestors: ['gerrit:100'], stack_index: 2, stack_size: 2,
      stack_ikeys: ['gerrit:100', 'gerrit:101'], activity_ts: 20,
    },
    'gerrit:200': {
      item_key: 'gerrit:200', kind: 'gerrit', target_id: '200',
      project_name: 'Skia', subject: 'Busy CL', status: 'NEW',
      url: 'https://skia-review.googlesource.com/c/skia/+/200',
      in_my_attention: true, chat_busy: true, conversation_id: 'cid-200',
      is_self: false, role: 'reviewer', activity_ts: 30,
    },
  },
  bugs: {
    'buganizer:999': {
      bug_key: 'buganizer:999', short_id: 'b/999', issue_id: '999',
      project_name: 'Skia', title: 'Assigned bug with subtasks',
      url: 'https://b.corp.google.com/issues/999',
      lane: 'assigned', status: 'ASSIGNED', priority: 'P2',
      linked_cls: [
        {item_key: 'gerrit:1', label: 'CL 1', url: 'https://example.com/1', status: 'NEW'},
        {item_key: 'gerrit:2', label: 'CL 2', url: 'https://example.com/2', status: 'NEW'},
        {item_key: 'gerrit:3', label: 'CL 3', url: 'https://example.com/3', status: 'NEW'},
        {item_key: 'gerrit:4', label: 'CL 4', url: 'https://example.com/4', status: 'NEW'},
        {item_key: 'gerrit:5', label: 'CL 5', url: 'https://example.com/5', status: 'NEW'},
      ],
    },
  },
};

global.fetch = async (url) => {
  if (url === '/api/status') {
    return {
      ok: true,
      status: 200,
      json: async () => JSON.parse(JSON.stringify(statusPayload)),
    };
  }
  if (url === '/api/rebase') {
    return {
      ok: true,
      status: 200,
      json: async () => ({ ok: true }),
    };
  }
  throw new Error('Unexpected fetch: ' + url);
};

let recordedIntervals = [];
const origSetInterval = global.setInterval;
global.setInterval = (fn, ms) => { recordedIntervals.push(ms); return 1; };

async function flushMicrotasks() {
  for (let i = 0; i < 8; i++) await Promise.resolve();
}

const runTest = async () => {
  boot();
  await flushMicrotasks();
  assert.deepStrictEqual(recordedIntervals, [60000], 'Status poll interval must be 60000ms');
  assert(
    getEl('footer').textContent.includes('refreshes every 60 s'),
    'CL footer must advertise 60 s refresh interval: ' + getEl('footer').textContent,
  );
  assert(
    getEl('focus').innerHTML.includes('data-details-key="stack:gerrit:100"'),
    'Collapsed stack sibling must include data-details-key',
  );
  assert(
    getEl('processing').innerHTML.includes('data-details-key="proc:gerrit:200"'),
    'Processing strip item must include data-details-key',
  );

  // Persist selectedSection in localStorage and location.hash across reloads
  toggleSection('waiting');
  assert.strictEqual(storageMap.get('flight_deck_section_cls'), 'waiting');
  assert(global.location.hash.includes('section=waiting'), 'Hash must include section=waiting');

  setView('bugs');
  toggleSection('assigned');
  assert.strictEqual(storageMap.get('flight_deck_section_bugs'), 'assigned');
  assert(global.location.hash.includes('view=bugs'), 'Hash must include view=bugs');
  assert(global.location.hash.includes('section=assigned'), 'Hash must include section=assigned');
  assert(
    getEl('lanes').innerHTML.includes('data-section="assigned"'),
    'Assigned bug section must render open',
  );
  assert(
    !getEl('lanes').innerHTML.includes('data-section="assigned" hidden'),
    'Assigned bug section must not carry hidden attribute when selected',
  );
  assert(
    getEl('lanes').innerHTML.includes('data-details-key="subtasks:buganizer:999"'),
    'Bug subtask disclosure must include data-details-key',
  );

  // Simulate reload by resetting in-memory state and calling boot() with saved localStorage
  selectedSection = {cls: null, bugs: null};
  global.location.hash = '';
  boot();
  await flushMicrotasks();
  assert.strictEqual(selectedView, 'bugs');
  assert.strictEqual(selectedSection.cls, 'waiting');
  assert.strictEqual(selectedSection.bugs, 'assigned');
  assert(
    !getEl('lanes').innerHTML.includes('data-section="assigned" hidden'),
    'Reloaded boot() must restore open Assigned section from localStorage',
  );

  // Unchanged /api/status payload (only last_sync ticked) must skip render() but post heartbeat
  const writesBeforeNoop = lanesWrites;
  const msgsBeforeNoop = parentMsgs.length;
  statusPayload.last_sync = '12:01 ET';
  await fetchState();
  assert.strictEqual(lanesWrites, writesBeforeNoop, 'Unchanged payload must skip render()');
  assert.strictEqual(getEl('synced').textContent, '12:01 ET');
  assert.strictEqual(parentMsgs.length, msgsBeforeNoop + 1, 'Skipped render must still ack parent');

  // Explicit interactive action (e.g. rebaseItem) must force render() even if payload is unchanged
  const rebaseBtn = { disabled: false, textContent: 'Rebase', title: '' };
  const writesBeforeRebase = lanesWrites;
  await rebaseItem('gerrit:101', rebaseBtn);
  assert(
    lanesWrites > writesBeforeRebase,
    'Interactive action fetchState(true) must bypass no-op skip so mutated buttons repaint',
  );

  // Active [data-confirming="1"] button defers background render() even when payload changes
  confirmingNode = { dataset: { confirming: '1' } };
  statusPayload.bugs['buganizer:999'].title = 'Updated title while confirming';
  const writesBeforeConfirm = lanesWrites;
  await fetchState();
  assert.strictEqual(
    lanesWrites,
    writesBeforeConfirm,
    'Active confirmation button must defer background render()',
  );
  confirmingNode = null;

  // Real data change preserves open <details> keys and scrollY across render()
  detailNodes = [{
    open: true,
    getAttribute(k) { return k === 'data-details-key' ? 'subtasks:buganizer:999' : null; },
  }];
  global.window.scrollY = 420;
  global.document.documentElement.scrollTop = 420;
  scrollCalls = [];
  statusPayload.bugs['buganizer:999'].title = 'Updated title after confirm';
  await fetchState();
  assert(lanesWrites > writesBeforeConfirm, 'Changed payload after confirmation must render()');
  assert(
    /<details class="subtask-more" data-details-key="subtasks:buganizer:999" open>/.test(
      getEl('lanes').innerHTML,
    ),
    'Open <details> must retain open attribute across render()',
  );
  assert(
    scrollCalls.some((c) => c[1] === 420) && global.window.scrollY === 420,
    'render() must preserve scrollY=420 across DOM replacement',
  );

  global.setInterval = origSetInterval;
  console.log('REFRESH_AND_STATE_PERSISTENCE_PASSED');
  process.exit(0);
};

eval(
  script +
    '\n;(' +
    runTest.toString() +
    ')().catch((err) => { console.error(err); process.exit(1); });',
);
"""
    cmd = ["node", "-e", js_code]
    res = subprocess.run(
        cmd, input=w.DASHBOARD_HTML, capture_output=True, text=True, check=False
    )
    self.assertEqual(
        res.returncode, 0, f"Node output: {res.stdout}\nError: {res.stderr}"
    )
    self.assertIn("REFRESH_AND_STATE_PERSISTENCE_PASSED", res.stdout)

  def test_boot_preserves_rendered_dom_and_ui_state_while_loading(self):
    js_code = r"""
const assert = require('assert');
const html = require('fs').readFileSync(0, 'utf8');
const script = html.split('<script>')[1].split('</script>')[0].replace(/boot\(\);/g, '');

const localMap = new Map();
const sessionMap = new Map();
global.localStorage = {
  getItem(k) { return localMap.has(k) ? localMap.get(k) : null; },
  setItem(k, v) { localMap.set(String(k), String(v)); },
};
global.sessionStorage = {
  getItem(k) { return sessionMap.has(k) ? sessionMap.get(k) : null; },
  setItem(k, v) { sessionMap.set(String(k), String(v)); },
};

let lanesWrites = 0;
let focusWrites = 0;
const els = new Map();
function resetDomElements() {
  els.clear();
}
function getEl(id) {
  if (!els.has(id)) {
    let inner = '';
    const obj = {
      id,
      textContent: id === 'synced' ? 'Loading…' : '',
      get innerHTML() { return inner; },
      set innerHTML(v) {
        inner = String(v);
        if (id === 'lanes') lanesWrites++;
        if (id === 'focus') focusWrites++;
      },
    };
    els.set(id, obj);
  }
  return els.get(id);
}

let detailNodes = [];
const winListeners = [];
let reloadCalls = 0;
global.document = {
  documentElement: { scrollTop: 0, classList: { add() {} } },
  getElementById: getEl,
  addEventListener() {},
  querySelector() { return null; },
  querySelectorAll(sel) {
    if (sel === 'details[data-details-key]') return detailNodes;
    return [];
  },
};
global.location = {
  search: '?embed=1',
  hash: '',
  reload() { reloadCalls++; },
};
global.history = {
  replaceState(_s, _t, url) { global.location.hash = String(url || ''); },
};

const parentMsgs = [];
global.window = {
  scrollY: 0,
  scrollTo(_x, y) {
    global.window.scrollY = y;
    global.document.documentElement.scrollTop = y;
  },
  parent: { postMessage(msg) { parentMsgs.push(msg); } },
  addEventListener(type, fn) {
    if (type === 'message') winListeners.push(fn);
  },
};

const basePayload = {
  ui_version: 700,
  last_sync: '12:00 ET',
  rollers_summary: {},
  bug_sync_errors: [],
  merge_request_groups: {},
  items: {
    'gerrit:101': {
      item_key: 'gerrit:101', kind: 'gerrit', target_id: '101',
      project_name: 'Skia', subject: 'Persist Across Loading', status: 'NEW',
      url: 'https://skia-review.googlesource.com/c/skia/+/101',
      in_my_attention: true, is_self: false, role: 'reviewer', activity_ts: 20,
      commit_message: 'Huge unused commit body '.repeat(50),
      revision_ps_by_sha: {'deadbeef': 1},
      gmail: {thread_id: 't1'},
    },
    'gerrit:200': {
      item_key: 'gerrit:200', kind: 'gerrit', target_id: '200',
      project_name: 'Skia', subject: 'Waiting CL', status: 'NEW',
      url: 'https://skia-review.googlesource.com/c/skia/+/200',
      in_my_attention: false, is_self: true, role: 'owner', activity_ts: 15,
    },
    'gerrit:300': {
      item_key: 'gerrit:300', kind: 'gerrit', target_id: '300',
      project_name: 'Skia', subject: 'Busy CL', status: 'NEW',
      url: 'https://skia-review.googlesource.com/c/skia/+/300',
      in_my_attention: true, chat_busy: true, conversation_id: 'cid-300',
      is_self: false, role: 'reviewer', activity_ts: 30,
    },
  },
  bugs: {},
};

let pendingStatusHandlers = [];
let immediateStatus = true;
global.fetch = (url) => {
  if (url === '/api/status') {
    if (immediateStatus) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => JSON.parse(JSON.stringify(basePayload)),
      });
    }
    return new Promise((resolve, reject) => {
      pendingStatusHandlers.push({ resolve, reject });
    });
  }
  throw new Error('Unexpected fetch: ' + url);
};

const origSetInterval = global.setInterval;
const origSetTimeout = global.setTimeout;
global.setInterval = () => 1;
const scheduledTimeouts = [];
global.setTimeout = (fn, ms) => {
  scheduledTimeouts.push({ fn, ms });
  return scheduledTimeouts.length;
};

async function flushMicrotasks() {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

const runTest = async () => {
  // Initial live load populates DOM and caches compact snapshot + UI state in storage
  boot();
  await flushMicrotasks();
  toggleSection('waiting');
  detailNodes = [{
    open: true,
    getAttribute(k) { return k === 'data-details-key' ? 'proc:gerrit:300' : null; },
  }];
  global.window.scrollY = 360;
  global.document.documentElement.scrollTop = 360;
  render();

  // Simulate full page reload where /api/status is slow (in flight)
  immediateStatus = false;
  pendingStatusHandlers = [];
  scheduledTimeouts.length = 0;
  parentMsgs.length = 0;
  detailNodes = [];
  resetDomElements();
  global.window.scrollY = 0;
  global.document.documentElement.scrollTop = 0;
  global.location.hash = '';

  boot();

  // Synchronously during Loading…, cached cards, lanes, open details, and scroll must stay visible
  assert(
    getEl('synced').textContent.startsWith('Loading…'),
    'Header #synced must indicate Loading… before live fetch: ' + getEl('synced').textContent,
  );
  assert(
    getEl('focus').innerHTML.includes('Persist Across Loading'),
    'Focus cards must render immediately from cached snapshot while Loading… is shown',
  );
  assert(
    getEl('lanes').innerHTML.includes('Waiting CL') &&
      !getEl('lanes').innerHTML.includes('data-section="waiting" hidden'),
    'Open secondary section must stay visible while Loading… is shown',
  );
  assert(
    getEl('processing').innerHTML.includes('data-details-key="proc:gerrit:300" open'),
    'Expanded <details> key must survive full page reload from storage',
  );
  assert.strictEqual(
    global.window.scrollY,
    360,
    'Scroll position must restore synchronously after cached boot render',
  );
  assert.strictEqual(
    parentMsgs.filter((m) => m.type === 'jetski-flight-deck-rendered').length,
    0,
    'Cached warm render must NOT emit jetski-flight-deck-rendered before live /api/status',
  );
  for (const fn of winListeners) {
    fn({ data: { type: 'jetski-flight-deck-ping' } });
  }
  assert.strictEqual(
    parentMsgs.filter((m) => m.type === 'jetski-flight-deck-rendered').length,
    0,
    'Parent ping before live /api/status must NOT falsely ack liveness from storage cache',
  );

  // Transient error on initial fetch keeps cached DOM on screen and schedules fast 2500ms retry
  assert.strictEqual(pendingStatusHandlers.length, 1);
  pendingStatusHandlers.shift().reject(new Error('PEN 502'));
  await flushMicrotasks();
  assert.strictEqual(getEl('synced').textContent, 'Connection lost, retrying');
  assert(
    getEl('focus').innerHTML.includes('Persist Across Loading'),
    'Transient fetch error must NOT blank out cached focus cards',
  );
  const fastRetry = scheduledTimeouts.find((t) => t.ms === 2500);
  assert(fastRetry, 'Initial fetch failure with cached DOM must still schedule 2500ms fast retry');

  // Trigger fast retry and resolve with bumped ui_version=800 and unchanged data
  fastRetry.fn();
  assert.strictEqual(pendingStatusHandlers.length, 1);
  const writesBeforeLiveResolve = lanesWrites;
  const nextLive = JSON.parse(JSON.stringify(basePayload));
  nextLive.ui_version = 800;
  nextLive.last_sync = '12:02 ET';
  pendingStatusHandlers.shift().resolve({
    ok: true,
    status: 200,
    json: async () => nextLive,
  });
  await flushMicrotasks();

  assert.strictEqual(
    reloadCalls,
    0,
    'First live /api/status after reload must not loop-reload when ui_version differs from cache',
  );
  assert.strictEqual(
    lanesWrites,
    writesBeforeLiveResolve,
    'Unchanged live /api/status matching warm-boot DOM must skip redundant innerHTML rebuild',
  );
  assert.strictEqual(getEl('synced').textContent, '12:02 ET');
  const renderedMsgs = parentMsgs.filter((m) => m.type === 'jetski-flight-deck-rendered');
  assert.strictEqual(renderedMsgs.length, 1, 'Live /api/status resolution must ack parent once');
  assert.strictEqual(renderedMsgs[0].uiVersion, 800);

  // Verify statusRenderSignature tracks merge_request_groups and bug_sync_errors, skips blobs
  const sigBase = statusRenderSignature(basePayload);
  const withUnusedDiff = JSON.parse(JSON.stringify(basePayload));
  withUnusedDiff.items['gerrit:101'].commit_message = 'Changed body';
  withUnusedDiff.items['gerrit:101'].revision_ps_by_sha = {'cafebabe': 2};
  withUnusedDiff.items['gerrit:101'].gmail = {thread_id: 't2'};
  assert.strictEqual(
    statusRenderSignature(withUnusedDiff),
    sigBase,
    'Unused item blobs must not invalidate statusRenderSignature',
  );
  const withErrDiff = JSON.parse(JSON.stringify(basePayload));
  withErrDiff.bug_sync_errors = ['gh issue list failed'];
  assert.notStrictEqual(
    statusRenderSignature(withErrDiff),
    sigBase,
    'bug_sync_errors must be included in statusRenderSignature',
  );
  const withMergeGrpDiff = JSON.parse(JSON.stringify(basePayload));
  withMergeGrpDiff.merge_request_groups = {'500': {parent_id: '500'}};
  assert.notStrictEqual(
    statusRenderSignature(withMergeGrpDiff),
    sigBase,
    'merge_request_groups must be included in statusRenderSignature',
  );

  global.setInterval = origSetInterval;
  global.setTimeout = origSetTimeout;
  console.log('WARM_BOOT_LOADING_PRESERVED_PASSED');
  process.exit(0);
};

eval(
  script +
    '\n;(' +
    runTest.toString() +
    ')().catch((err) => { console.error(err); process.exit(1); });',
);
"""
    cmd = ["node", "-e", js_code]
    res = subprocess.run(
        cmd, input=w.DASHBOARD_HTML, capture_output=True, text=True, check=False
    )
    self.assertEqual(
        res.returncode, 0, f"Node output: {res.stdout}\nError: {res.stderr}"
    )
    self.assertIn("WARM_BOOT_LOADING_PRESERVED_PASSED", res.stdout)

  def test_serve_forever_accepts_before_pen_url_and_ignores_broken_pipe(self):
    import threading as _th

    block_export = unittest.mock.MagicMock()
    pen_gate = _th.Event()
    announce_done = _th.Event()
    serve_called = _th.Event()

    class FakeServer:

      def serve_forever(self):
        serve_called.set()

      def server_close(self):
        pass

    def slow_pen(_port: int) -> str:
      pen_gate.wait(timeout=2.0)
      return "https://8089-pen.corp.goog"

    with (
        unittest.mock.patch.object(
            w, "ThreadedHTTPServer", return_value=FakeServer()
        ),
        unittest.mock.patch.object(w, "export_flight_deck_info", block_export),
        unittest.mock.patch.object(w, "flight_deck_pen_url", side_effect=slow_pen),
        unittest.mock.patch(
            "builtins.print", side_effect=lambda *_a, **_kw: announce_done.set()
        ),
    ):
      t = _th.Thread(
          target=w.serve_forever, args=(18089, 60, 0), daemon=True
      )
      t.start()
      try:
        self.assertTrue(
            serve_called.wait(timeout=0.3),
            "server.serve_forever() must start before slow flight_deck_pen_url() finishes",
        )
      finally:
        pen_gate.set()
        announce_done.wait(timeout=2.0)
        t.join(timeout=2.0)

    # Aborted client sockets must not raise BrokenPipeError from _send_json or do_GET
    handler = w.FlightDeckHandler.__new__(w.FlightDeckHandler)
    handler.path = "/"
    handler.send_response = lambda *_a, **_kw: None
    handler.send_header = lambda *_a, **_kw: None
    handler.end_headers = lambda *_a, **_kw: None
    handler.wfile = unittest.mock.MagicMock()
    handler.wfile.write.side_effect = BrokenPipeError("client disconnected")
    handler._send_json({"ok": True})
    handler.do_GET()


if __name__ == "__main__":
  unittest.main()
