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
    const clPs1 = 'https://skia-review.googlesource.com/c/skia/+/1414016/1';
    const clChecks = 'https://skia-review.googlesource.com/c/skia/+/1414016/3?tab=checks';
    const clComment = 'https://skia-review.googlesource.com/c/skia/+/1414016/comment/abc_123';
    const clFile = 'https://skia-review.googlesource.com/c/skia/+/1414016/3/include/core/SkTypes.h';

    // Opening CL root matches open patchset tab
    assert.equal(urlsMatchForAutoDedupe(clRoot, clPs3), true);
    // Opening specific patchset matches root tab
    assert.equal(urlsMatchForAutoDedupe(clPs3, clRoot), true);
    // Opening identical patchsets match
    assert.equal(urlsMatchForAutoDedupe(clPs3, clPs3), true);
    // Opening DIFFERENT explicit patchsets must NOT match (preserves comparison)
    assert.equal(urlsMatchForAutoDedupe(clPs1, clPs3), false);
    // Checks view must not be swallowed by diff overview
    assert.equal(urlsMatchForAutoDedupe(clChecks, clPs3), false);
    // Comment link must not be swallowed by CL overview
    assert.equal(urlsMatchForAutoDedupe(clComment, clPs3), false);
    // Specific file diff must not be swallowed by CL overview
    assert.equal(urlsMatchForAutoDedupe(clFile, clPs3), false);

    // Issue trackers across mirrors
    const issueA = 'https://issues.chromium.org/issues/576664769';
    const issueB = 'https://issuetracker.google.com/issues/576664769?pli=1';
    const issueC = 'https://bugs.custom.google.com/issues/576664769';
    const issueComment2 = 'https://issues.chromium.org/issues/576664769#comment2';
    const issueComment5 = 'https://issues.chromium.org/issues/576664769#comment5';

    assert.equal(urlsMatchForAutoDedupe(issueA, issueB), true);
    assert.equal(urlsMatchForAutoDedupe(issueA, issueC), true);
    // Explicit comment hash deep links must stay distinct
    assert.equal(urlsMatchForAutoDedupe(issueComment2, issueComment5), false);

    // Google Docs and Sheets sub-tabs
    const docA = 'https://docs.google.com/document/u/0/d/123456789/edit?pli=1&tab=t.0';
    const docB = 'https://docs.google.com/document/u/1/d/123456789/edit';
    const docTabX = 'https://docs.google.com/document/d/123456789/edit?tab=t.xyz';
    const sheetGid0 = 'https://docs.google.com/spreadsheets/d/abc/edit#gid=0';
    const sheetDefault = 'https://docs.google.com/spreadsheets/d/abc/edit';
    const sheetGid99 = 'https://docs.google.com/spreadsheets/d/abc/edit#gid=99';

    assert.equal(urlsMatchForAutoDedupe(docA, docB), true);
    assert.equal(urlsMatchForAutoDedupe(docA, docTabX), false);
    assert.equal(urlsMatchForAutoDedupe(sheetGid0, sheetDefault), true);
    assert.equal(urlsMatchForAutoDedupe(sheetGid0, sheetGid99), false);

    // GitHub PRs and issues
    const prA = 'https://github.com/flutter/flutter/pull/194980';
    const prB = 'https://github.com/flutter/flutter/pull/194980/';
    const prFiles = 'https://github.com/flutter/flutter/pull/194980/files';
    assert.equal(urlsMatchForAutoDedupe(prA, prB), true);
    assert.equal(urlsMatchForAutoDedupe(prA, prFiles), false);

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

    const plan = planManualCleanup([
      {id: 1, url: prA, title: 'PR 1', pinned: true},
      {id: 2, url: prB, title: 'PR 2', pinned: false},
      {id: 3, url: clPs3, title: 'CL 3', active: true},
      {id: 4, url: clPs3, title: 'CL 4', active: false},
      {id: 5, url: clRoot, title: 'CL Root', active: false},
      {id: 6, url: clPs1, title: 'CL PS1', active: false},
      {id: 7, url: 'https://other.test/page', title: 'Other'},
    ]);
    assert.equal(plan.groupsCount, 2);
    // Pinned (1), active (3), and PS1 (6) stay open; PR dup (2), PS3 dup (4), and Root (5) close
    assert.deepEqual(plan.closable.map(x => x.tabId).sort((a, b) => a - b), [2, 4, 5]);

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
