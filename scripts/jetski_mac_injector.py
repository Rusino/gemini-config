#!/usr/bin/env python3
"""Injects jetski_closed_filter.js into macOS Antigravity Hub via native CDP"""

import argparse
import base64
import collections
import hashlib
import json
import os
import plistlib
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from typing import Any, Deque, Dict, List, Optional, Tuple
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PRIMARY_JS_SRC = os.path.join(SCRIPT_DIR, "jetski_closed_filter.js")
MAC_ARTIFACT_JS_DST = os.path.expanduser(
    "~/.gemini/antigravity/brain/jetski-closed-filter.js"
)
DEFAULT_ACTIVE_PORT_FILE = os.path.expanduser(
    "~/Library/Application Support/Antigravity/DevToolsActivePort"
)
LAUNCH_AGENT_LABEL = "com.google.antigravity.jetski-filter"
DEFAULT_PLIST_PATH = os.path.expanduser(
    f"~/Library/LaunchAgents/{LAUNCH_AGENT_LABEL}.plist"
)
DEFAULT_LOG_PATH = os.path.expanduser(
    "~/.gemini/config/logs/jetski_mac_injector.log"
)

HUB_URL_RE = re.compile(r"^https://(?:127\.0\.0\.1|localhost):\d+(?:/|$)")

MAX_HEADER_BYTES = 65536
MAX_WS_MSG_BYTES = 16 * 1024 * 1024
MAX_WS_FRAMES_PER_MSG = 1024
MAX_DRAIN_ITERATIONS = 4096
IDLE_POLL_SEC = 4.0
DISCONNECTED_POLL_SEC = 3.0


class CdpClosedError(OSError):
  """Raised when the CDP TCP or WebSocket stream closes"""


def is_hub_page_target(target_info: Dict[str, Any]) -> bool:
  """Matches only top-level local Hub windows, never guest webviews or DevTools"""
  if not isinstance(target_info, dict):
    return False
  if target_info.get("type") != "page":
    return False
  url = str(target_info.get("url") or "")
  return bool(HUB_URL_RE.match(url))


def read_active_port_file(path: str) -> Optional[Tuple[int, str]]:
  """Parses (port, browser_ws_path) from Chromium's DevToolsActivePort file"""
  try:
    with open(path, "r", encoding="utf-8") as f:
      lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
  except OSError:
    return None
  if len(lines) < 2:
    return None
  try:
    port = int(lines[0])
  except ValueError:
    return None
  ws_path = lines[1]
  if port <= 0 or port > 65535 or not ws_path.startswith("/devtools/"):
    return None
  return port, ws_path


def compute_file_version(path: str) -> str:
  st = os.stat(path)
  return f"{st.st_mtime_ns}-{st.st_size}"


def sync_mac_artifact_js(
    src_path: str = PRIMARY_JS_SRC,
    dst_path: str = MAC_ARTIFACT_JS_DST,
    export_indexes: bool = False,
) -> str:
  """Atomically mirrors jetski_closed_filter.js into ~/.gemini/antigravity/brain"""
  if not os.path.isfile(src_path):
    raise FileNotFoundError(f"Missing filter script at {src_path}")
  src_ver = compute_file_version(src_path)
  os.makedirs(os.path.dirname(dst_path), exist_ok=True)
  needs_copy = True
  if os.path.isfile(dst_path):
    try:
      src_st = os.stat(src_path)
      dst_st = os.stat(dst_path)
      if src_st.st_size == dst_st.st_size and dst_st.st_mtime_ns >= src_st.st_mtime_ns:
        needs_copy = False
    except OSError:
      needs_copy = True
  if needs_copy:
    tmp_path = f"{dst_path}.tmp.{os.getpid()}"
    shutil.copy2(src_path, tmp_path)
    os.replace(tmp_path, dst_path)
  if export_indexes:
    hooks_dir = os.path.join(os.path.dirname(SCRIPT_DIR), "hooks")
    if hooks_dir not in sys.path:
      sys.path.insert(0, hooks_dir)
    try:
      import chat_lifecycle  # pylint: disable=g-import-not-at-top
      import roadmap  # pylint: disable=g-import-not-at-top

      chat_lifecycle.export_chain_index()
      roadmap.export_roadmap_index()
    except Exception:
      pass
  return src_ver


def build_bootstrap_script(version_tag: str) -> str:
  """Builds an idempotent loader snippet safe for both new-document and live eval"""
  ver_json = json.dumps(version_tag)
  return (
      "(()=>{"
      "if(window!==window.top)return 'skip:iframe';"
      "if(!/^https:\\/\\/(127\\.0\\.0\\.1|localhost):\\d+$/.test(window.location.origin))"
      "return 'skip:origin';"
      f"const VER={ver_json};"
      "if(window.__jetskiClosedChatsFilterInstalled&&"
      "window.__jetskiClosedChatsFilterMacVer===VER)return 'already-current';"
      "if(window.__jetskiClosedChatsFilterLoadingVer===VER)return 'loading';"
      "const inject=()=>{"
      "const csrf=window.__APP_CONFIG__?.csrfToken||'';"
      "if(!csrf)return 'skip:no-app-config';"
      "if(window.__jetskiClosedChatsFilterInstalled&&"
      "window.__jetskiClosedChatsFilterMacVer===VER)return 'already-current';"
      "window.__jetskiClosedChatsFilterLoadingVer=VER;"
      "document.querySelectorAll('script[data-jetski-mac-loader=\"1\"]')"
      ".forEach(n=>n.remove());"
      "const s=document.createElement('script');"
      "s.dataset.jetskiMacLoader='1';"
      "s.src='/static/artifacts/jetski-closed-filter.js?v='+encodeURIComponent(VER)"
      "+'&csrf='+encodeURIComponent(csrf);"
      "s.onload=()=>{window.__jetskiClosedChatsFilterMacVer=VER;"
      "window.__jetskiClosedChatsFilterLoadingVer=null;};"
      "s.onerror=()=>{window.__jetskiClosedChatsFilterLoadingVer=null;};"
      "(document.head||document.documentElement).appendChild(s);"
      "return 'injected';"
      "};"
      # Defer until DOMContentLoaded when running via addScriptToEvaluateOnNewDocument
      # before <head> inline script populates window.__APP_CONFIG__.csrfToken
      "if(document.readyState==='loading'||!window.__APP_CONFIG__?.csrfToken){"
      "if(document.readyState==='loading'){"
      "document.addEventListener('DOMContentLoaded',()=>{inject();},{once:true});"
      "return 'deferred';"
      "}"
      "return 'skip:no-app-config';"
      "}"
      "return inject();"
      "})();"
  )


def build_eject_script() -> str:
  return (
      "(()=>{"
      "const had=Boolean(window.__jetskiClosedChatsFilterInstalled);"
      "if(typeof window.__jetskiClosedChatsFilterCleanup==='function'){"
      "window.__jetskiClosedChatsFilterCleanup();"
      "}"
      "window.__jetskiClosedChatsFilterInstalled=false;"
      "window.__jetskiClosedChatsFilterMacVer=null;"
      "window.__jetskiClosedChatsFilterLoadingVer=null;"
      "document.querySelectorAll('script[data-jetski-mac-loader=\"1\"]')"
      ".forEach(n=>n.remove());"
      "document.getElementById('jetski-closed-chats-filter-style')?.remove();"
      "return had?'ejected':'not-installed';"
      "})();"
  )


def build_status_script() -> str:
  return (
      "JSON.stringify({"
      "installed:Boolean(window.__jetskiClosedChatsFilterInstalled),"
      "ver:window.__jetskiClosedChatsFilterMacVer||null,"
      "hasBar:Boolean(document.querySelector('.jetski-attention-filter-bar')),"
      "hasInbox:Boolean(document.querySelector('.jetski-needs-reaction-inbox')),"
      "titlebarBtns:document.querySelectorAll('.jetski-titlebar-archive-btn').length,"
      "contextBadges:document.querySelectorAll('.jetski-titlebar-context-badge').length"
      "})"
  )


class CdpWebSocket:
  """Minimal stdlib RFC 6455 loopback client for Chrome DevTools Protocol"""

  def __init__(self, host: str, port: int, path: str, timeout: float = 10.0):
    self._sock = socket.create_connection((host, port), timeout=timeout)
    self._rx_buf = bytearray()
    self._next_id = 1
    self.pending_events: Deque[Dict[str, Any]] = collections.deque()
    try:
      self._handshake(host, port, path)
    except Exception:
      self.close()
      raise

  def fileno(self) -> int:
    return self._sock.fileno()

  def has_buffered_bytes(self) -> bool:
    return bool(self._rx_buf)

  def _handshake(self, host: str, port: int, path: str) -> None:
    raw_key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {raw_key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    self._sock.sendall(req.encode("ascii"))
    header_buf = bytearray()
    while b"\r\n\r\n" not in header_buf:
      if len(header_buf) > MAX_HEADER_BYTES:
        raise CdpClosedError("WebSocket handshake header exceeded limit")
      chunk = self._sock.recv(4096)
      if not chunk:
        raise CdpClosedError("EOF during WebSocket handshake")
      header_buf.extend(chunk)
    sep = header_buf.index(b"\r\n\r\n")
    head_bytes = bytes(header_buf[:sep])
    # Preserve any server frames coalesced into the TCP segment after headers
    self._rx_buf.extend(header_buf[sep + 4 :])
    status_line = head_bytes.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
    if " 101 " not in status_line:
      raise CdpClosedError(f"WebSocket upgrade rejected: {status_line}")
    expected_accept = base64.b64encode(
        hashlib.sha1(
            (raw_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()
    ).decode("ascii")
    if expected_accept.lower() not in head_bytes.decode("latin1").lower():
      raise CdpClosedError("Invalid Sec-WebSocket-Accept response")

  def _read_exact(self, n: int) -> bytes:
    while len(self._rx_buf) < n:
      chunk = self._sock.recv(max(4096, n - len(self._rx_buf)))
      if not chunk:
        raise CdpClosedError("EOF on CDP WebSocket")
      self._rx_buf.extend(chunk)
    out = bytes(self._rx_buf[:n])
    del self._rx_buf[:n]
    return out

  def _send_frame(self, opcode: int, payload: bytes) -> None:
    mask_key = os.urandom(4)
    header = bytearray([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
      header.append(0x80 | n)
    elif n < 65536:
      header.append(0x80 | 126)
      header.extend(struct.pack("!H", n))
    else:
      header.append(0x80 | 127)
      header.extend(struct.pack("!Q", n))
    header.extend(mask_key)
    masked = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))
    self._sock.sendall(bytes(header) + masked)

  def _recv_frame(self) -> Tuple[int, bool, bytes]:
    b0, b1 = self._read_exact(2)
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
      length = struct.unpack("!H", self._read_exact(2))[0]
    elif length == 127:
      length = struct.unpack("!Q", self._read_exact(8))[0]
    if length > MAX_WS_MSG_BYTES:
      raise CdpClosedError(f"Oversized WebSocket frame: {length}")
    mask_key = self._read_exact(4) if masked else b""
    payload = self._read_exact(length)
    if masked:
      payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))
    return opcode, fin, payload

  def recv_json(self) -> Dict[str, Any]:
    msg_opcode: Optional[int] = None
    chunks: List[bytes] = []
    total = 0
    for _ in range(MAX_WS_FRAMES_PER_MSG):
      opcode, fin, payload = self._recv_frame()
      if opcode == 0x8:
        raise CdpClosedError("Peer closed CDP WebSocket")
      if opcode == 0x9:
        self._send_frame(0xA, payload[:125])
        continue
      if opcode == 0xA:
        continue
      if opcode != 0x0:
        msg_opcode = opcode
        chunks = [payload]
        total = len(payload)
      else:
        chunks.append(payload)
        total += len(payload)
      if total > MAX_WS_MSG_BYTES:
        raise CdpClosedError("Oversized reassembled WebSocket message")
      if fin and msg_opcode == 0x1:
        raw = b"".join(chunks).decode("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    raise CdpClosedError("Exceeded MAX_WS_FRAMES_PER_MSG without FIN")

  def call(
      self,
      method: str,
      params: Optional[Dict[str, Any]] = None,
      session_id: Optional[str] = None,
  ) -> Dict[str, Any]:
    req_id = self._next_id
    self._next_id += 1
    msg: Dict[str, Any] = {"id": req_id, "method": method, "params": params or {}}
    if session_id:
      msg["sessionId"] = session_id
    self._send_frame(0x1, json.dumps(msg).encode("utf-8"))
    for _ in range(MAX_DRAIN_ITERATIONS):
      incoming = self.recv_json()
      if incoming.get("id") == req_id:
        if "error" in incoming:
          raise RuntimeError(f"CDP {method} failed: {incoming['error']}")
        res = incoming.get("result")
        return res if isinstance(res, dict) else {}
      # Queue interleaved CDP events so callers handle them without reentrancy
      if "method" in incoming:
        self.pending_events.append(incoming)
    raise CdpClosedError(f"No CDP response received for {method}")

  def close(self) -> None:
    try:
      self._send_frame(0x8, b"")
    except OSError:
      pass
    try:
      self._sock.close()
    except OSError:
      pass


def connect_ws_url(ws_url: str, timeout: float = 10.0) -> CdpWebSocket:
  parsed = urllib.parse.urlparse(ws_url)
  host = parsed.hostname or "127.0.0.1"
  port = parsed.port or 80
  path = parsed.path or "/"
  if parsed.query:
    path = f"{path}?{parsed.query}"
  return CdpWebSocket(host, port, path, timeout=timeout)


def list_http_targets(port: int, timeout: float = 3.0) -> List[Dict[str, Any]]:
  url = f"http://127.0.0.1:{port}/json/list"
  req = urllib.request.Request(url, method="GET")
  with urllib.request.urlopen(req, timeout=timeout) as resp:
    data = json.loads(resp.read().decode("utf-8"))
  return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []


def eval_on_hub_pages(
    port: int, expression: str
) -> List[Tuple[Dict[str, Any], Any]]:
  results: List[Tuple[Dict[str, Any], Any]] = []
  for target in list_http_targets(port):
    if not is_hub_page_target(target):
      continue
    ws_url = str(target.get("webSocketDebuggerUrl") or "")
    if not ws_url:
      continue
    ws = connect_ws_url(ws_url, timeout=5.0)
    try:
      res = ws.call(
          "Runtime.evaluate",
          {"expression": expression, "returnByValue": True, "awaitPromise": False},
      )
      val = res.get("result", {}).get("value")
      results.append((target, val))
    finally:
      ws.close()
  return results


class BrowserInjectorSession:
  """Maintains flattened CDP sessions on Hub pages for instant reload & hot-update"""

  def __init__(
      self,
      ws: CdpWebSocket,
      js_src: str = PRIMARY_JS_SRC,
      js_dst: str = MAC_ARTIFACT_JS_DST,
  ):
    self.ws = ws
    self.js_src = js_src
    self.js_dst = js_dst
    self.current_ver = sync_mac_artifact_js(js_src, js_dst, export_indexes=True)
    self.bootstrap_js = build_bootstrap_script(self.current_ver)
    # Maps targetId -> {"session_id": str, "script_id": str}
    self.attached: Dict[str, Dict[str, str]] = {}
    self.session_to_target: Dict[str, str] = {}

  def start(self) -> None:
    self.ws.call("Target.setDiscoverTargets", {"discover": True})
    self.drain_pending_events()

  def _attach_or_refresh_target(self, target_info: Dict[str, Any]) -> None:
    target_id = str(target_info.get("targetId") or "")
    if not target_id or not is_hub_page_target(target_info):
      return
    if target_id in self.attached:
      return
    try:
      res = self.ws.call(
          "Target.attachToTarget", {"targetId": target_id, "flatten": True}
      )
      sid = str(res.get("sessionId") or "")
      if not sid:
        return
      self.ws.call("Page.enable", {}, session_id=sid)
      add_res = self.ws.call(
          "Page.addScriptToEvaluateOnNewDocument",
          {"source": self.bootstrap_js},
          session_id=sid,
      )
      eval_res = self.ws.call(
          "Runtime.evaluate",
          {"expression": self.bootstrap_js, "returnByValue": True},
          session_id=sid,
      )
      script_id = str(add_res.get("identifier") or "")
      self.attached[target_id] = {"session_id": sid, "script_id": script_id}
      self.session_to_target[sid] = target_id
      status = eval_res.get("result", {}).get("value")
      print(
          f"[attach] target={target_id[:12]} ver={self.current_ver} res={status}",
          flush=True,
      )
    except (RuntimeError, OSError) as exc:
      print(f"[warn] attach failed for {target_id[:12]}: {exc}", flush=True)

  def _forget_target(self, target_id: str) -> None:
    entry = self.attached.pop(target_id, None)
    if entry:
      self.session_to_target.pop(entry["session_id"], None)

  def handle_event(self, ev: Dict[str, Any]) -> None:
    method = ev.get("method")
    params = ev.get("params") if isinstance(ev.get("params"), dict) else {}
    if method in ("Target.targetCreated", "Target.targetInfoChanged"):
      info = params.get("targetInfo")
      if isinstance(info, dict):
        self._attach_or_refresh_target(info)
    elif method == "Target.targetDestroyed":
      self._forget_target(str(params.get("targetId") or ""))
    elif method == "Target.detachedFromTarget":
      sid = str(params.get("sessionId") or "")
      tid = str(params.get("targetId") or self.session_to_target.get(sid) or "")
      if tid:
        self._forget_target(tid)
    elif method == "Page.loadEventFired":
      sid = str(ev.get("sessionId") or "")
      if sid and sid in self.session_to_target:
        try:
          self.ws.call(
              "Runtime.evaluate",
              {"expression": self.bootstrap_js, "returnByValue": True},
              session_id=sid,
          )
        except (RuntimeError, OSError):
          pass

  def drain_pending_events(self) -> None:
    for _ in range(MAX_DRAIN_ITERATIONS):
      if not self.ws.pending_events:
        return
      self.handle_event(self.ws.pending_events.popleft())

  def maybe_hot_reload_js(self) -> None:
    try:
      next_ver = compute_file_version(self.js_src)
    except OSError:
      return
    if next_ver == self.current_ver and os.path.isfile(self.js_dst):
      return
    self.current_ver = sync_mac_artifact_js(self.js_src, self.js_dst)
    self.bootstrap_js = build_bootstrap_script(self.current_ver)
    for tid, entry in list(self.attached.items()):
      sid = entry["session_id"]
      old_script_id = entry.get("script_id")
      try:
        if old_script_id:
          self.ws.call(
              "Page.removeScriptToEvaluateOnNewDocument",
              {"identifier": old_script_id},
              session_id=sid,
          )
        add_res = self.ws.call(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": self.bootstrap_js},
            session_id=sid,
        )
        entry["script_id"] = str(add_res.get("identifier") or "")
        eval_res = self.ws.call(
            "Runtime.evaluate",
            {"expression": self.bootstrap_js, "returnByValue": True},
            session_id=sid,
        )
        status = eval_res.get("result", {}).get("value")
        print(
            f"[reload] target={tid[:12]} ver={self.current_ver} res={status}",
            flush=True,
        )
      except (RuntimeError, OSError) as exc:
        print(f"[warn] reload failed for {tid[:12]}: {exc}", flush=True)
        self._forget_target(tid)
    self.drain_pending_events()


def run_watch_loop(
    active_port_file: str,
    js_src: str = PRIMARY_JS_SRC,
    js_dst: str = MAC_ARTIFACT_JS_DST,
) -> int:
  stop = False

  def _on_signal(_sig: int, _frame: Any) -> None:
    nonlocal stop
    stop = True

  signal.signal(signal.SIGTERM, _on_signal)
  signal.signal(signal.SIGINT, _on_signal)

  print(f"[start] Watching {active_port_file}", flush=True)
  while not stop:
    endpoint = read_active_port_file(active_port_file)
    if not endpoint:
      time.sleep(DISCONNECTED_POLL_SEC)
      continue
    port, ws_path = endpoint
    ws: Optional[CdpWebSocket] = None
    try:
      ws = CdpWebSocket("127.0.0.1", port, ws_path, timeout=10.0)
      print(f"[connect] Attached to Antigravity CDP 127.0.0.1:{port}", flush=True)
      session = BrowserInjectorSession(ws, js_src=js_src, js_dst=js_dst)
      session.start()
      while not stop:
        if ws.has_buffered_bytes():
          ready = True
        else:
          rlist, _, _ = select.select([ws], [], [], IDLE_POLL_SEC)
          ready = bool(rlist)
        if ready:
          ev = ws.recv_json()
          if "method" in ev:
            session.handle_event(ev)
          session.drain_pending_events()
        session.maybe_hot_reload_js()
    except (CdpClosedError, OSError, RuntimeError, ValueError) as exc:
      if not stop:
        print(f"[wait] CDP idle/disconnected ({exc}); retrying", flush=True)
        time.sleep(DISCONNECTED_POLL_SEC)
    finally:
      if ws is not None:
        ws.close()
  print("[stop] Exiting watcher", flush=True)
  return 0


def build_launch_agent_plist(
    script_path: str, log_path: str = DEFAULT_LOG_PATH
) -> bytes:
  payload = {
      "Label": LAUNCH_AGENT_LABEL,
      "ProgramArguments": ["/usr/bin/python3", script_path, "watch"],
      "RunAtLoad": True,
      "KeepAlive": True,
      "ProcessType": "Background",
      "ThrottleInterval": 5,
      "StandardOutPath": log_path,
      "StandardErrorPath": log_path,
      "EnvironmentVariables": {
          "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
          "PYTHONUNBUFFERED": "1",
      },
  }
  return plistlib.dumps(payload)


def _launchctl_domain() -> str:
  return f"gui/{os.getuid()}"


def cmd_install(args: argparse.Namespace) -> int:
  sync_mac_artifact_js(args.js_src, args.js_dst, export_indexes=True)
  os.makedirs(os.path.dirname(args.plist_path), exist_ok=True)
  os.makedirs(os.path.dirname(args.log_path), exist_ok=True)
  plist_bytes = build_launch_agent_plist(
      os.path.abspath(__file__), log_path=args.log_path
  )
  with open(args.plist_path, "wb") as f:
    f.write(plist_bytes)
  domain = _launchctl_domain()
  subprocess.run(
      ["launchctl", "bootout", domain, args.plist_path],
      capture_output=True,
      check=False,
  )
  subprocess.run(
      ["launchctl", "bootstrap", domain, args.plist_path],
      check=True,
  )
  subprocess.run(
      ["launchctl", "kickstart", "-k", f"{domain}/{LAUNCH_AGENT_LABEL}"],
      check=False,
  )
  print(f"[OK] Installed and started {LAUNCH_AGENT_LABEL} ({args.plist_path})")
  return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
  domain = _launchctl_domain()
  subprocess.run(
      ["launchctl", "bootout", domain, args.plist_path],
      capture_output=True,
      check=False,
  )
  if os.path.exists(args.plist_path):
    os.remove(args.plist_path)
    print(f"[OK] Removed {args.plist_path}")
  endpoint = read_active_port_file(args.active_port_file)
  if endpoint:
    try:
      for target, val in eval_on_hub_pages(endpoint[0], build_eject_script()):
        print(f"[OK] Window {target.get('id', '')[:12]}: {val}")
    except OSError:
      pass
  if os.path.exists(args.js_dst):
    os.remove(args.js_dst)
    print(f"[OK] Removed {args.js_dst}")
  print("[OK] Rollback complete")
  return 0


def cmd_inject_once(args: argparse.Namespace) -> int:
  ver = sync_mac_artifact_js(args.js_src, args.js_dst, export_indexes=True)
  endpoint = read_active_port_file(args.active_port_file)
  if not endpoint:
    print(f"[ERROR] Active port file unreadable: {args.active_port_file}", file=sys.stderr)
    return 1
  results = eval_on_hub_pages(endpoint[0], build_bootstrap_script(ver))
  if not results:
    print("[WARN] No open Antigravity Hub page targets found")
    return 1
  for target, val in results:
    print(f"[OK] {target.get('id', '')[:12]} ({target.get('url')}): {val}")
  return 0


def cmd_eject(args: argparse.Namespace) -> int:
  endpoint = read_active_port_file(args.active_port_file)
  if not endpoint:
    print("[OK] Antigravity is not running")
    return 0
  for target, val in eval_on_hub_pages(endpoint[0], build_eject_script()):
    print(f"[OK] {target.get('id', '')[:12]} ({target.get('url')}): {val}")
  return 0


def cmd_status(args: argparse.Namespace) -> int:
  domain_target = f"{_launchctl_domain()}/{LAUNCH_AGENT_LABEL}"
  proc = subprocess.run(
      ["launchctl", "print", domain_target],
      capture_output=True,
      text=True,
      check=False,
  )
  agent_running = proc.returncode == 0 and "state = running" in proc.stdout
  print(f"LaunchAgent plist: {args.plist_path} (exists={os.path.exists(args.plist_path)})")
  print(f"LaunchAgent running: {agent_running}")
  endpoint = read_active_port_file(args.active_port_file)
  if not endpoint:
    print("Antigravity CDP: not detected")
    return 0
  print(f"Antigravity CDP: 127.0.0.1:{endpoint[0]}")
  try:
    results = eval_on_hub_pages(endpoint[0], build_status_script())
  except OSError as exc:
    print(f"Antigravity CDP unreachable: {exc}")
    return 0
  for target, val in results:
    print(f"  Window {target.get('id', '')[:12]}: {val}")
  return 0


def main(argv: Optional[List[str]] = None) -> int:
  if sys.platform != "darwin" and os.environ.get("JETSKI_ALLOW_NON_DARWIN") != "1":
    print("[SKIP] jetski_mac_injector.py only runs on macOS", file=sys.stderr)
    return 0
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--active-port-file", default=DEFAULT_ACTIVE_PORT_FILE)
  parser.add_argument("--js-src", default=PRIMARY_JS_SRC)
  parser.add_argument("--js-dst", default=MAC_ARTIFACT_JS_DST)
  parser.add_argument("--plist-path", default=DEFAULT_PLIST_PATH)
  parser.add_argument("--log-path", default=DEFAULT_LOG_PATH)
  sub = parser.add_subparsers(dest="cmd", required=True)
  sub.add_parser("watch")
  sub.add_parser("install")
  sub.add_parser("uninstall")
  sub.add_parser("inject-once")
  sub.add_parser("eject")
  sub.add_parser("status")
  args = parser.parse_args(argv)
  dispatch = {
      "watch": lambda a: run_watch_loop(a.active_port_file, a.js_src, a.js_dst),
      "install": cmd_install,
      "uninstall": cmd_uninstall,
      "inject-once": cmd_inject_once,
      "eject": cmd_eject,
      "status": cmd_status,
  }
  return dispatch[args.cmd](args)


if __name__ == "__main__":
  sys.exit(main())
