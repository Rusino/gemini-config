#!/usr/bin/env python3
"""Contract tests for scripts/jetski_mac_injector.py"""

import base64
import hashlib
import json
import os
import plistlib
import socket
import struct
import sys
import tempfile
import threading
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
  sys.path.insert(0, SCRIPTS_DIR)

import jetski_mac_injector as jmi  # pylint: disable=g-import-not-at-top


def _ws_server_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
  first = (0x80 if fin else 0x00) | (opcode & 0x0F)
  n = len(payload)
  if n < 126:
    head = bytes([first, n])
  elif n < 65536:
    head = bytes([first, 126]) + struct.pack("!H", n)
  else:
    head = bytes([first, 127]) + struct.pack("!Q", n)
  return head + payload


def _ws_read_client_frame(conn: socket.socket, rx: bytearray):
  def read_exact(k: int) -> bytes:
    while len(rx) < k:
      chunk = conn.recv(4096)
      if not chunk:
        raise EOFError()
      rx.extend(chunk)
    out = bytes(rx[:k])
    del rx[:k]
    return out

  b0, b1 = read_exact(2)
  opcode = b0 & 0x0F
  masked = bool(b1 & 0x80)
  length = b1 & 0x7F
  if length == 126:
    length = struct.unpack("!H", read_exact(2))[0]
  elif length == 127:
    length = struct.unpack("!Q", read_exact(8))[0]
  mask = read_exact(4) if masked else b""
  payload = read_exact(length)
  if masked:
    payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
  return opcode, masked, payload


class TestJetskiMacInjector(unittest.TestCase):

  def test_is_hub_page_target_strict_isolation(self):
    self.assertTrue(
        jmi.is_hub_page_target(
            {"type": "page", "url": "https://127.0.0.1:62633/c/abc"}
        )
    )
    self.assertTrue(
        jmi.is_hub_page_target({"type": "page", "url": "https://localhost:5000"})
    )
    self.assertFalse(
        jmi.is_hub_page_target(
            {"type": "webview", "url": "https://127.0.0.1:62633/"}
        )
    )
    self.assertFalse(
        jmi.is_hub_page_target({"type": "page", "url": "http://127.0.0.1:62633/"})
    )
    self.assertFalse(
        jmi.is_hub_page_target({"type": "page", "url": "https://google.com/"})
    )
    self.assertFalse(
        jmi.is_hub_page_target({"type": "page", "url": "about:blank"})
    )

  def test_read_active_port_file_validation(self):
    with tempfile.TemporaryDirectory() as tmp:
      missing = os.path.join(tmp, "missing")
      self.assertIsNone(jmi.read_active_port_file(missing))
      p = os.path.join(tmp, "DevToolsActivePort")
      with open(p, "w", encoding="utf-8") as f:
        f.write("62631\n/devtools/browser/abc-123\n")
      self.assertEqual(
          jmi.read_active_port_file(p), (62631, "/devtools/browser/abc-123")
      )
      with open(p, "w", encoding="utf-8") as f:
        f.write("99999\n/devtools/browser/abc\n")
      self.assertIsNone(jmi.read_active_port_file(p))
      with open(p, "w", encoding="utf-8") as f:
        f.write("62631\n/wrong/path\n")
      self.assertIsNone(jmi.read_active_port_file(p))

  def test_sync_and_plist_generation(self):
    with tempfile.TemporaryDirectory() as tmp:
      src = os.path.join(tmp, "src.js")
      dst = os.path.join(tmp, "brain", "jetski-closed-filter.js")
      with open(src, "w", encoding="utf-8") as f:
        f.write("console.log(1);\n")
      v1 = jmi.sync_mac_artifact_js(src, dst)
      self.assertTrue(os.path.isfile(dst))
      with open(src, "w", encoding="utf-8") as f:
        f.write("console.log(12345);\n")
      v2 = jmi.sync_mac_artifact_js(src, dst)
      self.assertNotEqual(v1, v2)
      with open(dst, "r", encoding="utf-8") as f:
        self.assertEqual(f.read(), "console.log(12345);\n")
      plist_data = plistlib.loads(jmi.build_launch_agent_plist("/x/s.py", "/x/l"))
      self.assertEqual(plist_data["Label"], jmi.LAUNCH_AGENT_LABEL)
      self.assertEqual(
          plist_data["ProgramArguments"], ["/usr/bin/python3", "/x/s.py", "watch"]
      )

  def test_cdp_session_attach_ping_coalescing_and_hot_reload(self):
    recorded = []
    pongs = []
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def server_thread():
      conn, _ = srv.accept()
      conn.settimeout(5.0)
      try:
        req = bytearray()
        while b"\r\n\r\n" not in req:
          req.extend(conn.recv(4096))
        m_key = ""
        for ln in req.decode("latin1").split("\r\n"):
          if ln.lower().startswith("sec-websocket-key:"):
            m_key = ln.split(":", 1)[1].strip()
        accept = base64.b64encode(
            hashlib.sha1(
                (m_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        resp_hdr = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")
        # Coalesce a PING frame into the HTTP 101 packet to test rx buffer preservation
        conn.sendall(resp_hdr + _ws_server_frame(0x9, b"hi"))
        rx = bytearray()
        script_seq = 0
        for _ in range(64):
          try:
            op, masked, payload = _ws_read_client_frame(conn, rx)
          except EOFError:
            break
          if op == 0x8:
            break
          if op == 0xA:
            pongs.append((masked, payload))
            continue
          msg = json.loads(payload.decode("utf-8"))
          recorded.append(msg)
          method = msg["method"]
          mid = msg["id"]
          if method == "Target.setDiscoverTargets":
            # Interleave two targetCreated events BEFORE replying to setDiscoverTargets
            for ev in (
                {
                    "method": "Target.targetCreated",
                    "params": {
                        "targetInfo": {
                            "targetId": "webv-ignore",
                            "type": "webview",
                            "url": "https://127.0.0.1:62633/",
                        }
                    },
                },
                {
                    "method": "Target.targetCreated",
                    "params": {
                        "targetInfo": {
                            "targetId": "hub-page-1",
                            "type": "page",
                            "url": "https://127.0.0.1:62633/c/1",
                        }
                    },
                },
            ):
              conn.sendall(_ws_server_frame(0x1, json.dumps(ev).encode("utf-8")))
            conn.sendall(
                _ws_server_frame(
                    0x1, json.dumps({"id": mid, "result": {}}).encode("utf-8")
                )
            )
          elif method == "Target.attachToTarget":
            conn.sendall(
                _ws_server_frame(
                    0x1,
                    json.dumps(
                        {"id": mid, "result": {"sessionId": "sess-1"}}
                    ).encode("utf-8"),
                )
            )
          elif method == "Page.addScriptToEvaluateOnNewDocument":
            script_seq += 1
            conn.sendall(
                _ws_server_frame(
                    0x1,
                    json.dumps(
                        {"id": mid, "result": {"identifier": f"scr-{script_seq}"}}
                    ).encode("utf-8"),
                )
            )
          elif method == "Runtime.evaluate":
            conn.sendall(
                _ws_server_frame(
                    0x1,
                    json.dumps(
                        {"id": mid, "result": {"result": {"value": "injected"}}}
                    ).encode("utf-8"),
                )
            )
          else:
            conn.sendall(
                _ws_server_frame(
                    0x1, json.dumps({"id": mid, "result": {}}).encode("utf-8")
                )
            )
      finally:
        conn.close()
        srv.close()

    t = threading.Thread(target=server_thread, daemon=True)
    t.start()
    with tempfile.TemporaryDirectory() as tmp:
      src = os.path.join(tmp, "filter.js")
      dst = os.path.join(tmp, "dst.js")
      with open(src, "w", encoding="utf-8") as f:
        f.write("window.a=1;\n")
      ws = jmi.CdpWebSocket("127.0.0.1", port, "/devtools/browser/t", timeout=5.0)
      try:
        sess = jmi.BrowserInjectorSession(ws, js_src=src, js_dst=dst)
        sess.js_src = src
        sess.start()
        self.assertEqual(list(sess.attached.keys()), ["hub-page-1"])
        self.assertEqual(sess.attached["hub-page-1"]["script_id"], "scr-1")
        # Modify JS source and verify hot reload swaps new-document script + evals
        with open(src, "w", encoding="utf-8") as f:
          f.write("window.a=123456;\n")
        sess.maybe_hot_reload_js()
        self.assertEqual(sess.attached["hub-page-1"]["script_id"], "scr-2")
      finally:
        ws.close()
    t.join(timeout=5.0)

    self.assertEqual(pongs, [(True, b"hi")])
    methods = [m["method"] for m in recorded]
    self.assertEqual(
        methods,
        [
            "Target.setDiscoverTargets",
            "Target.attachToTarget",
            "Page.enable",
            "Page.addScriptToEvaluateOnNewDocument",
            "Runtime.evaluate",
            "Page.removeScriptToEvaluateOnNewDocument",
            "Page.addScriptToEvaluateOnNewDocument",
            "Runtime.evaluate",
        ],
    )


if __name__ == "__main__":
  unittest.main()
