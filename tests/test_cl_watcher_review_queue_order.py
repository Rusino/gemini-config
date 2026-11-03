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


if __name__ == "__main__":
  unittest.main()
