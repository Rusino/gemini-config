#!/usr/bin/env python3
"""Contract and live Chrome E2E tests for Safe Tab Deduper extension."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request
import websockets

CONFIG_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
EXT_DIR = os.path.join(CONFIG_ROOT, "chrome_extensions", "tab_deduper")
NODE_BIN = os.path.expanduser("~/.gemini/jetski/bin/node")
CHROME_BIN = os.path.expanduser(
    "~/.cache/puppeteer/chrome/linux-148.0.7778.97/chrome-linux64/chrome"
)


class DedupeLogicUnitTest(unittest.TestCase):

  def test_dedupe_logic_unit_matrix(self):
    test_script = """
    import assert from 'node:assert/strict';
    import {
      isAutoDedupeExcluded,
      parseResourceDescriptor,
      urlsMatchForAutoDedupe,
      buildSameDocumentHashUrl,
      shouldSkipIntentionalDuplicate,
      pickSurvivorTab,
      planManualCleanup,
    } from './chrome_extensions/tab_deduper/dedupe_logic.mjs';

    // Host and protocol exclusions
    assert.equal(isAutoDedupeExcluded('https://mail.google.com/mail/u/0/'), true);
    assert.equal(isAutoDedupeExcluded('https://calendar.google.com/calendar/r'), true);
    assert.equal(isAutoDedupeExcluded('https://meet.google.com/abc-defg-hij'), true);
    assert.equal(isAutoDedupeExcluded('http://localhost:8080/app'), true);
    assert.equal(isAutoDedupeExcluded('http://127.0.0.1:3000/app'), true);
    assert.equal(isAutoDedupeExcluded('https://custom.test/app', ['custom.test']), true);
    assert.equal(isAutoDedupeExcluded('https://github.com/flutter/flutter'), false);

    // Gerrit review URL matching
    const clRoot = 'https://skia-review.googlesource.com/c/skia/+/1414016';
    const clPs3 = 'https://skia-review.googlesource.com/c/skia/+/1414016/3';
    const clPs3Msg = 'https://skia-review.googlesource.com/c/skia/+/1414016/3#message-abc';
    const clPs1 = 'https://skia-review.googlesource.com/c/skia/+/1414016/1';
    const clRange = 'https://skia-review.googlesource.com/c/skia/+/1414016/1..3';
    const clChecks = 'https://skia-review.googlesource.com/c/skia/+/1414016/3?tab=checks';
    const clComment = 'https://skia-review.googlesource.com/c/skia/+/1414016/comment/abc_123';
    const clFile = 'https://skia-review.googlesource.com/c/skia/+/1414016/3/include/core/SkTypes.h';

    // Opening CL root matches open patchset tab
    assert.equal(urlsMatchForAutoDedupe(clRoot, clPs3), true);
    // Opening specific patchset matches root tab
    assert.equal(urlsMatchForAutoDedupe(clPs3, clRoot), true);
    // Identical patchsets and message anchors on the same patchset match
    assert.equal(urlsMatchForAutoDedupe(clPs3, clPs3), true);
    assert.equal(urlsMatchForAutoDedupe(clPs3Msg, clPs3), true);
    // Explicit patchset/range/checks/comment/file subviews stay distinct in auto-mode
    assert.equal(urlsMatchForAutoDedupe(clPs1, clPs3), false);
    assert.equal(urlsMatchForAutoDedupe(clRange, clPs3), false);
    assert.equal(urlsMatchForAutoDedupe(clChecks, clPs3), false);
    assert.equal(urlsMatchForAutoDedupe(clComment, clPs3), false);
    assert.equal(urlsMatchForAutoDedupe(clFile, clPs3), false);

    // Issue trackers unify mirrors, /u/<N>/ prefixes, short IDs, and #comment anchors
    const issueA = 'https://issues.chromium.org/issues/576664769';
    const issueB = 'https://issuetracker.google.com/issues/576664769?pli=1';
    const issueCorpU0 = 'https://bugs.custom.google.com/u/0/issues/576664769';
    const issueSkia = 'https://issues.skia.org/issues/576664769';
    const issueShort = 'https://issues.chromium.org/576664769';
    const issueComment2 = 'https://issues.chromium.org/issues/576664769#comment2';
    const issueComment5 = 'https://issues.chromium.org/issues/576664769#comment5';
    const redirectIssue =
      'https://www.google.com/url?q=https%3A%2F%2Fissues.chromium.org' +
      '%2Fissues%2F576664769%23comment5';

    assert.equal(urlsMatchForAutoDedupe(issueA, issueB), true);
    assert.equal(urlsMatchForAutoDedupe(issueA, issueCorpU0), true);
    assert.equal(urlsMatchForAutoDedupe(issueA, issueSkia), true);
    assert.equal(urlsMatchForAutoDedupe(issueA, issueShort), true);
    assert.equal(urlsMatchForAutoDedupe(issueA, issueComment2), true);
    assert.equal(urlsMatchForAutoDedupe(issueComment2, issueComment5), true);
    assert.equal(urlsMatchForAutoDedupe(redirectIssue, issueCorpU0), true);

    // Same-document hash scroll builder preserves survivor origin/path/query across mirrors
    assert.equal(
      buildSameDocumentHashUrl(issueCorpU0, issueComment5),
      'https://bugs.custom.google.com/u/0/issues/576664769#comment5',
    );
    assert.equal(buildSameDocumentHashUrl(clPs3, clComment), null);
    assert.equal(buildSameDocumentHashUrl(clPs3, clPs3Msg), clPs3Msg);
    assert.equal(
      buildSameDocumentHashUrl(
        clPs3,
        'https://skia-review.googlesource.com/c/skia/+/1414016#message-abc',
      ),
      clPs3Msg,
    );
    assert.equal(
      buildSameDocumentHashUrl(
        'https://other.test/docs/guide/?utm_source=chat',
        'https://other.test/docs/guide#sec2',
      ),
      'https://other.test/docs/guide/?utm_source=chat#sec2',
    );

    // Google Docs and Sheets sub-tabs, published /d/e/ docs, and /copy or /export actions
    const docA = 'https://docs.google.com/document/u/0/d/123456789/edit?pli=1&tab=t.0';
    const docHeading = 'https://docs.google.com/document/d/123456789/edit#heading=h.abc';
    const docTabX = 'https://docs.google.com/document/d/123456789/edit?tab=t.xyz';
    const docCopy = 'https://docs.google.com/document/d/123456789/copy';
    const docExport = 'https://docs.google.com/document/d/123456789/export?format=pdf';
    const pubDoc1 = 'https://docs.google.com/document/d/e/2PACX-1vFirstDoc/pub';
    const pubDoc2 = 'https://docs.google.com/document/d/e/2PACX-1vSecondDoc/pub';
    const sheetGid0 = 'https://docs.google.com/spreadsheets/d/abc/edit#gid=0';
    const sheetDefault = 'https://docs.google.com/spreadsheets/d/abc/edit';
    const sheetGid99 = 'https://docs.google.com/spreadsheets/d/abc/edit#gid=99';

    assert.equal(urlsMatchForAutoDedupe(docA, docHeading), true);
    assert.equal(urlsMatchForAutoDedupe(docA, docTabX), false);
    assert.equal(urlsMatchForAutoDedupe(docA, docCopy), false);
    assert.equal(urlsMatchForAutoDedupe(docA, docExport), false);
    assert.equal(urlsMatchForAutoDedupe(pubDoc1, pubDoc2), false);
    assert.equal(urlsMatchForAutoDedupe(pubDoc1, pubDoc1), true);
    assert.equal(urlsMatchForAutoDedupe(sheetGid0, sheetDefault), true);
    assert.equal(urlsMatchForAutoDedupe(sheetGid0, sheetGid99), false);

    // GitHub unifies comment hashes in auto-mode, keeps distinct commits/check runs open
    const prA = 'https://github.com/flutter/flutter/pull/194980';
    const prComment = 'https://github.com/flutter/flutter/pull/194980#issuecomment-42';
    const prFiles = 'https://github.com/flutter/flutter/pull/194980/files';
    const prCommit1 = 'https://github.com/flutter/flutter/pull/194980/commits/1111111';
    const prCommit2 = 'https://github.com/flutter/flutter/pull/194980/commits/2222222';
    const prCheck1 = 'https://github.com/flutter/flutter/pull/194980/checks?check_run_id=100';
    const prCheck2 = 'https://github.com/flutter/flutter/pull/194980/checks?check_run_id=200';
    assert.equal(urlsMatchForAutoDedupe(prA, prComment), true);
    assert.equal(urlsMatchForAutoDedupe(prA, prFiles), false);
    assert.equal(urlsMatchForAutoDedupe(prCommit1, prCommit2), false);
    assert.equal(urlsMatchForAutoDedupe(prCheck1, prCheck2), false);
    assert.equal(
      buildSameDocumentHashUrl(
        prCommit1,
        'https://github.com/flutter/flutter/pull/194980/commits/2222222#diff-abcR10',
      ),
      null,
    );

    // General web pages unify #section anchors while preserving #/ SPA routes
    assert.equal(
      urlsMatchForAutoDedupe('https://other.test/docs/guide', 'https://other.test/docs/guide#sec2'),
      true,
    );
    assert.equal(
      urlsMatchForAutoDedupe('https://app.test/#/inbox', 'https://app.test/#/sent'),
      false,
    );

    // Intentional duplicate guards (both scalar and {currentTabId, prevTabId} state)
    assert.equal(
      shouldSkipIntentionalDuplicate({
        newTab: {id: 43, openerTabId: 42, windowId: 1, index: 5, url: clPs3},
        targetTab: {id: 42, windowId: 1, index: 4, url: clPs3},
        prevActiveTabIdByWindow: new Map([[1, 42]]),
      }),
      true,
    );
    assert.equal(
      shouldSkipIntentionalDuplicate({
        newTab: {id: 43, openerTabId: null, windowId: 1, index: 5, url: clPs3},
        targetTab: {id: 42, windowId: 1, index: 4, url: clPs3},
        prevActiveTabIdByWindow: new Map([[1, {currentTabId: 43, prevTabId: 42}]]),
      }),
      true,
    );
    // Different raw URL (e.g. CL root vs patchset) is not a native Duplicate Tab action
    assert.equal(
      shouldSkipIntentionalDuplicate({
        newTab: {id: 43, openerTabId: null, windowId: 1, index: 5, url: clRoot},
        targetTab: {id: 42, windowId: 1, index: 4, url: clPs3},
        prevActiveTabIdByWindow: new Map([[1, {currentTabId: 43, prevTabId: 42}]]),
      }),
      false,
    );

    // Survivor selection and manual cleanup planning
    const survivor = pickSurvivorTab([
      {id: 10, pinned: false, lastAccessed: 1000},
      {id: 11, pinned: true, lastAccessed: 500},
      {id: 12, pinned: false, lastAccessed: 2000},
    ]);
    assert.equal(survivor.id, 11);

    // Manual cleanup collapses entity subviews (Gerrit subviews, PR files, Tab Groups)
    const plan = planManualCleanup([
      {id: 1, url: prA, title: 'PR 1', pinned: true},
      {id: 2, url: prFiles, title: 'PR Files', pinned: false},
      {id: 3, url: clPs3, title: 'CL PS3', active: false, lastAccessed: 100},
      {id: 4, url: clComment, title: 'CL Comment', active: false, lastAccessed: 900},
      {id: 5, url: clRoot, title: 'CL Root', active: false, lastAccessed: 800},
      {id: 6, url: clPs1, title: 'CL PS1', active: false, lastAccessed: 700},
      {id: 7, url: clChecks, title: 'CL Checks', active: false, lastAccessed: 950},
      {id: 8, url: clFile, title: 'CL File', active: false, lastAccessed: 960},
      {id: 9, url: issueCorpU0, title: 'Bug Grouped', groupId: 5, lastAccessed: 100},
      {id: 10, url: issueComment2, title: 'Bug Loose', groupId: -1, lastAccessed: 500},
      {id: 11, url: issueComment5, title: 'Bug Grouped Dup', groupId: 5, lastAccessed: 50},
      {id: 12, url: 'https://other.test/page', title: 'Other'},
      {id: 13, url: 'https://other.test/page#section2', title: 'Other Anchor'},
    ]);
    assert.equal(plan.groupsCount, 4);
    // Survivors: 1 (pinned PR), 3 (highest overview PS3), 9 (grouped bug), 12 (first page)
    assert.deepEqual(
      plan.closable.map((x) => x.tabId).sort((a, b) => a - b),
      [2, 4, 5, 6, 7, 8, 10, 11, 13],
    );

    console.log('ALL_DEDUPE_LOGIC_TESTS_PASSED');
    """
    proc = subprocess.run(
        [NODE_BIN, "--input-type=module", "-e", test_script],
        cwd=CONFIG_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(
        proc.returncode,
        0,
        f"Node unit matrix failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
    )
    self.assertIn("ALL_DEDUPE_LOGIC_TESTS_PASSED", proc.stdout)


class MockServerHandler(BaseHTTPRequestHandler):

  def log_message(self, format_str, *args):
    # Suppress test HTTP server logs
    pass

  def do_GET(self):
    if self.path.startswith("/c/skia/+/1414016"):
      html = f"""<!DOCTYPE html><html><body>
        <h1>Review Page: {self.path}</h1>
        <textarea id="draft">initial</textarea>
      </body></html>"""
      self.send_response(200)
      self.send_header("Content-Type", "text/html; charset=utf-8")
      self.end_headers()
      self.wfile.write(html.encode("utf-8"))
      return

    if self.path.startswith("/issues/576664769") or self.path.startswith(
        "/u/0/issues/576664769"
    ):
      html = """<!DOCTYPE html><html><body>
        <textarea id="bugdraft">initial</textarea>
        <div style="height: 2200px">spacer</div>
        <div id="comment9">Comment 9 body</div>
        <div style="height: 2200px">spacer2</div>
      </body></html>"""
      self.send_response(200)
      self.send_header("Content-Type", "text/html; charset=utf-8")
      self.end_headers()
      self.wfile.write(html.encode("utf-8"))
      return

    if self.path == "/go-cl":
      # 302 redirect to canonical CL root URL
      self.send_response(302)
      self.send_header("Location", "/c/skia/+/1414016")
      self.end_headers()
      return

    if self.path == "/dev":
      self.send_response(200)
      self.send_header("Content-Type", "text/plain")
      self.end_headers()
      self.wfile.write(b"local dev endpoint")
      return

    self.send_response(404)
    self.end_headers()


class LiveChromeExtensionE2ETest(unittest.TestCase):

  def setUp(self):
    self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockServerHandler)
    self.server_port = self.httpd.server_address[1]
    self.server_thread = threading.Thread(target=self.httpd.serve_forever)
    self.server_thread.daemon = True
    self.server_thread.start()

  def tearDown(self):
    self.httpd.shutdown()
    self.httpd.server_close()

  def test_live_chrome_extension_e2e(self):
    async def run_e2e():
      user_data = tempfile.mkdtemp()
      cmd = [
          CHROME_BIN,
          f"--user-data-dir={user_data}",
          "--headless=new",
          "--no-sandbox",
          "--disable-gpu",
          "--disable-popup-blocking",
          "--remote-debugging-port=0",
          f"--host-resolver-rules=MAP *.test 127.0.0.1",
          f"--load-extension={EXT_DIR}",
          f"--disable-extensions-except={EXT_DIR}",
          "about:blank",
      ]
      proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
      try:
        port_file = os.path.join(user_data, "DevToolsActivePort")
        for _ in range(50):
          if os.path.exists(port_file):
            break
          await asyncio.sleep(0.1)

        with open(port_file, "r", encoding="utf-8") as f:
          cdp_port = int(f.readline().strip())

        browser_info_url = f"http://127.0.0.1:{cdp_port}/json/version"
        with urllib.request.urlopen(browser_info_url) as resp:
          info = json.loads(resp.read().decode("utf-8"))
        ws_url = info["webSocketDebuggerUrl"]

        async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
          msg_id = 0

          async def send_cmd(method, params=None, session_id=None):
            nonlocal msg_id
            msg_id += 1
            req = {"id": msg_id, "method": method, "params": params or {}}
            if session_id:
              req["sessionId"] = session_id
            await ws.send(json.dumps(req))
            while True:
              raw = await ws.recv()
              msg = json.loads(raw)
              if msg.get("id") == req["id"]:
                if "error" in msg:
                  raise RuntimeError(f"CDP {method} error: {msg['error']}")
                return msg.get("result", {})

          await send_cmd("Target.setDiscoverTargets", {"discover": True})
          await asyncio.sleep(0.5)

          targets_resp = await send_cmd("Target.getTargets")
          targets = targets_resp.get("targetInfos", [])
          sw_target = next(
              (
                  t
                  for t in targets
                  if t.get("type") == "service_worker"
                  and "background.js" in t.get("url", "")
              ),
              None,
          )
          self.assertIsNotNone(
              sw_target,
              "Safe Tab Deduper background service worker not found in Chrome",
          )

          attach_sw = await send_cmd(
              "Target.attachToTarget",
              {"targetId": sw_target["targetId"], "flatten": True},
          )
          sw_sid = attach_sw["sessionId"]

          async def eval_sw(expression):
            res = await send_cmd(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                session_id=sw_sid,
            )
            return res.get("result", {}).get("value")

          # Open Tab A with patchset 2
          base_url = f"http://gerrit.test:{self.server_port}"
          cl_url_ps2 = f"{base_url}/c/skia/+/1414016/2"

          tab_a = await eval_sw(
              f'chrome.tabs.create({{url: "{cl_url_ps2}", active: true}})'
          )
          tab_a_id = tab_a["id"]
          await asyncio.sleep(0.8)

          # Find Tab A page target to type unsaved text into draft textarea
          targets_now = (await send_cmd("Target.getTargets")).get("targetInfos", [])
          page_a = next(
              t
              for t in targets_now
              if t.get("type") == "page" and "/c/skia/+/1414016/2" in t["url"]
          )
          attach_a = await send_cmd(
              "Target.attachToTarget",
              {"targetId": page_a["targetId"], "flatten": True},
          )
          session_a = attach_a["sessionId"]

          await send_cmd(
              "Runtime.evaluate",
              {
                  "expression": (
                      'document.getElementById("draft").value = "unsaved review draft";'
                  ),
                  "returnByValue": True,
              },
              session_id=session_a,
          )

          # Open duplicate Tab B with CL Root (which canonicalizes to open PS2)
          cl_url_root = f"{base_url}/c/skia/+/1414016"
          await eval_sw(
              f'chrome.tabs.create({{url: "{cl_url_root}", active: true}})'
          )
          await asyncio.sleep(1.2)

          # Check open tabs: duplicate CL root must be closed, leaving only Tab A!
          open_tabs = await eval_sw(
              "chrome.tabs.query({}).then(tabs => tabs.map(t => ({id: t.id, url: t.url})))"
          )
          cl_tabs = [t for t in open_tabs if "/c/skia/+/1414016" in t.get("url", "")]
          self.assertEqual(
              len(cl_tabs),
              1,
              f"Duplicate CL root was not closed: {cl_tabs}",
          )
          self.assertEqual(cl_tabs[0]["id"], tab_a_id)

          # Verify Tab A draft textarea was preserved without reloading!
          draft_check = await send_cmd(
              "Runtime.evaluate",
              {
                  "expression": 'document.getElementById("draft").value;',
                  "returnByValue": True,
              },
              session_id=session_a,
          )
          self.assertEqual(
              draft_check.get("result", {}).get("value"),
              "unsaved review draft",
              "Draft textarea was reset or tab was reloaded during deduplication",
          )

          # Open Tab C with 302 redirect to CL root -> must also be deduplicated!
          redirect_url = f"{base_url}/go-cl"
          await eval_sw(
              f'chrome.tabs.create({{url: "{redirect_url}", active: true}})'
          )
          await asyncio.sleep(1.4)

          open_tabs_after_redir = await eval_sw(
              "chrome.tabs.query({}).then(tabs => tabs.map(t => ({id: t.id, url: t.url})))"
          )
          cl_tabs_after_redir = [
              t for t in open_tabs_after_redir if "/c/skia/+/1414016" in t.get("url", "")
          ]
          self.assertEqual(
              len(cl_tabs_after_redir),
              1,
              f"Redirected duplicate tab was not closed: {cl_tabs_after_redir}",
          )

          # Open Tab D with DIFFERENT patchset (/1) -> MUST STAY OPEN side-by-side!
          cl_url_ps1 = f"{base_url}/c/skia/+/1414016/1"
          await eval_sw(
              f'chrome.tabs.create({{url: "{cl_url_ps1}", active: true}})'
          )
          await asyncio.sleep(1.0)

          open_tabs_ps1 = await eval_sw(
              "chrome.tabs.query({}).then(tabs => tabs.map(t => ({id: t.id, url: t.url})))"
          )
          cl_tabs_ps1 = [
              t for t in open_tabs_ps1 if "/c/skia/+/1414016" in t.get("url", "")
          ]
          self.assertEqual(
              len(cl_tabs_ps1),
              2,
              f"Different explicit patchset was incorrectly closed: {cl_tabs_ps1}",
          )

          # Open two local dev URLs -> loopback exclusion must keep both open!
          dev_url = f"http://127.0.0.1:{self.server_port}/dev"
          await eval_sw(
              f'chrome.tabs.create({{url: "{dev_url}", active: false}})'
          )
          await eval_sw(
              f'chrome.tabs.create({{url: "{dev_url}", active: false}})'
          )
          await asyncio.sleep(0.8)

          open_tabs_dev = await eval_sw(
              "chrome.tabs.query({}).then(tabs => tabs.map(t => ({id: t.id, url: t.url})))"
          )
          dev_tabs = [t for t in open_tabs_dev if t.get("url", "").endswith("/dev")]
          self.assertEqual(
              len(dev_tabs),
              2,
              f"Loopback dev URLs were incorrectly closed: {dev_tabs}",
          )

          # Native Duplicate Tab when Tab A is active -> MUST STAY OPEN!
          await eval_sw(f"chrome.tabs.update({tab_a_id}, {{active: true}})")
          await asyncio.sleep(0.2)
          dup_tab = await eval_sw(f"chrome.tabs.duplicate({tab_a_id})")
          dup_tab_id = dup_tab["id"]
          await asyncio.sleep(1.0)

          open_tabs_dup = await eval_sw(
              "chrome.tabs.query({}).then(tabs => tabs.map(t => ({id: t.id, url: t.url})))"
          )
          dup_present = any(t["id"] == dup_tab_id for t in open_tabs_dup)
          self.assertTrue(
              dup_present,
              f"Intentional native chrome.tabs.duplicate tab was closed: {open_tabs_dup}",
          )

          # Bug tab on /u/0/issues/... receives #comment9 link -> scrolls without reload
          bug_base = f"http://corp.issues.test:{self.server_port}"
          bug_u0 = f"{bug_base}/u/0/issues/576664769"
          bug_c9 = f"http://pub.issues.test:{self.server_port}/issues/576664769#comment9"
          bug_tab = await eval_sw(
              f'chrome.tabs.create({{url: "{bug_u0}", active: true}})'
          )
          bug_tab_id = bug_tab["id"]
          await asyncio.sleep(0.6)

          targets_bug = (await send_cmd("Target.getTargets")).get("targetInfos", [])
          page_bug = next(
              t
              for t in targets_bug
              if t.get("type") == "page" and "/u/0/issues/576664769" in t["url"]
          )
          session_bug = (
              await send_cmd(
                  "Target.attachToTarget",
                  {"targetId": page_bug["targetId"], "flatten": True},
              )
          )["sessionId"]
          await send_cmd(
              "Runtime.evaluate",
              {
                  "expression": (
                      'document.getElementById("bugdraft").value = "unsaved bug reply";'
                      "window.__bugMarker = 77;"
                  ),
                  "returnByValue": True,
              },
              session_id=session_bug,
          )

          # Create blank tab, add it to Tab Group first, then navigate to #comment9
          group_id = await eval_sw(
              f"chrome.tabs.group({{tabIds: [{bug_tab_id}]}})"
          )
          dup_in_group = await eval_sw(
              'chrome.tabs.create({url: "about:blank", active: true})'
          )
          dup_gid = dup_in_group["id"]
          assigned_gid = await eval_sw(
              f"chrome.tabs.group({{groupId: {group_id}, tabIds: [{dup_gid}]}})"
          )
          self.assertEqual(assigned_gid, group_id)
          await eval_sw(
              f'chrome.tabs.update({dup_gid}, {{url: "{bug_c9}", active: true}})'
          )
          await asyncio.sleep(1.0)

          tabs_after_grouped_nav = await eval_sw(
              "chrome.tabs.query({}).then(ts => ts.map(t => t.id))"
          )
          self.assertNotIn(dup_gid, tabs_after_grouped_nav)

          bug_state = (
              await send_cmd(
                  "Runtime.evaluate",
                  {
                      "expression": (
                          'JSON.stringify({draft: document.getElementById("bugdraft").value,'
                          " marker: window.__bugMarker, hash: location.hash,"
                          " scrollY: Math.round(window.scrollY)})"
                      ),
                      "returnByValue": True,
                  },
                  session_id=session_bug,
              )
          )["result"]["value"]
          parsed_bug_state = json.loads(bug_state)
          self.assertEqual(parsed_bug_state["draft"], "unsaved bug reply")
          self.assertEqual(parsed_bug_state["marker"], 77)
          self.assertEqual(parsed_bug_state["hash"], "#comment9")
          self.assertGreater(parsed_bug_state["scrollY"], 500)

          # Open Gerrit comment subview + grouped duplicate, then run popup manual cleanup
          cl_comment_url = f"{base_url}/c/skia/+/1414016/comment/abc_123"
          await eval_sw(
              f'chrome.tabs.create({{url: "{cl_comment_url}", active: false}})'
          )
          await eval_sw(
              f"chrome.tabs.group({{groupId: {group_id}, tabIds: [{dup_tab_id}]}})"
          )
          await asyncio.sleep(0.6)

          ext_id = sw_target["url"].split("/")[2]
          popup_url = f"chrome-extension://{ext_id}/popup.html"
          await eval_sw(f'chrome.tabs.create({{url: "{popup_url}", active: true}})')
          await asyncio.sleep(0.6)

          targets_popup = (await send_cmd("Target.getTargets")).get("targetInfos", [])
          page_popup = next(
              t for t in targets_popup if popup_url in t.get("url", "")
          )
          session_popup = (
              await send_cmd(
                  "Target.attachToTarget",
                  {"targetId": page_popup["targetId"], "flatten": True},
              )
          )["sessionId"]

          cleanup_res = (
              await send_cmd(
                  "Runtime.evaluate",
                  {
                      "expression": """(async () => {
                        const st = await chrome.runtime.sendMessage({action: 'getState'});
                        const ids = st.duplicates.closable.map(x => x.tabId);
                        const cl = await chrome.runtime.sendMessage({
                          action: 'closeSelectedDuplicates',
                          tabIds: ids,
                        });
                        return {closableCount: ids.length, closedCount: cl.closedCount};
                      })()""",
                      "awaitPromise": True,
                      "returnByValue": True,
                  },
                  session_id=session_popup,
              )
          )["result"]["value"]
          # Tab A (PS2), PS1, dup_tab (grouped PS2), and comment subview collapse to 1 CL tab
          self.assertEqual(cleanup_res["closableCount"], 3)
          self.assertEqual(cleanup_res["closedCount"], 3)

          remaining_cl = await eval_sw(
              "chrome.tabs.query({}).then("
              "ts => ts.filter(t => (t.url || '').includes('/c/skia/+/1414016')).length)"
          )
          self.assertEqual(remaining_cl, 1)

          # Verify recentAutoSwitches was recorded in session storage
          switches = await eval_sw(
              "chrome.storage.session.get().then(s => s.recentAutoSwitches || [])"
          )
          self.assertGreaterEqual(
              len(switches),
              1,
              "No recent auto switches recorded in session storage",
          )

      finally:
        proc.terminate()
        try:
          proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
          proc.kill()
        if proc.stderr:
          proc.stderr.close()
        shutil.rmtree(user_data, ignore_errors=True)

    asyncio.run(run_e2e())


if __name__ == "__main__":
  unittest.main(verbosity=2)
