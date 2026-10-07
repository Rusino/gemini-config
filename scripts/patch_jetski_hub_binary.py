#!/usr/bin/env python3
"""Patches the extracted jetski-hub-server binary so index.html loads jetski-closed-filter.js."""

import glob
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
from typing import List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PRIMARY_JS_SRC = os.path.join(SCRIPT_DIR, "jetski_closed_filter.js")
FALLBACK_JS_SRC = os.path.expanduser(
    "~/.gemini/jetski/scratch/jetski_sidebar_filter/content.js"
)
ARTIFACT_JS_DST = os.path.expanduser(
    "~/.gemini/jetski/brain/jetski-closed-filter.js"
)
SAR_LAUNCHER = "/google/bin/releases/jetski-devs/jetski-hub-server/server"

SHIM_PREFIX = (
    b"<script>\n(function() {\n  'use strict';\n  if (window.nativeStorage)"
    b" return;"
)
SCRIPT_OPEN_TAG = b"<script>"
PATCH_MARKER = b"/*JETSKI_CLOSED_FILTER*/"

REPLACEMENT_BODY = (
    b"<script>/*JETSKI_CLOSED_FILTER*/(()=>{"
    b"if(!window.nativeStorage){window.nativeStorage={"
    b"getItems:async()=>{const o={};for(let i=0;i<localStorage.length;i++){"
    b'const k=localStorage.key(i);if(k&&k.startsWith("ag:"))o[k.slice(3)]=localStorage.getItem(k)}return o},'
    b"updateItems:async c=>{for(const[k,v]of Object.entries(c))"
    b'v==null?localStorage.removeItem("ag:"+k):localStorage.setItem("ag:"+k,v)}}}'
    b'const l=()=>{const s=document.createElement("script");'
    b's.src="/static/artifacts/jetski-closed-filter.js?v="+Date.now()+"&csrf="+encodeURIComponent(window.__APP_CONFIG__?.csrfToken||"");'
    b"document.head.appendChild(s)};"
    b'document.readyState==="loading"?document.addEventListener("DOMContentLoaded",l):l()'
    b"})();"
)
REPLACEMENT_SUFFIX = b"</script>"


def resolve_source_js() -> str:
  if os.path.isfile(PRIMARY_JS_SRC):
    return PRIMARY_JS_SRC
  if os.path.isfile(FALLBACK_JS_SRC):
    return FALLBACK_JS_SRC
  raise FileNotFoundError(
      f"Missing filter script at {PRIMARY_JS_SRC} and {FALLBACK_JS_SRC}"
  )


def sync_artifact_js(
    src_path: Optional[str] = None, dst_path: Optional[str] = None
) -> None:
  src = src_path or resolve_source_js()
  dst = dst_path or ARTIFACT_JS_DST
  os.makedirs(os.path.dirname(dst), exist_ok=True)
  shutil.copyfile(src, dst)
  print(f"[OK] Synced {src} -> {dst}")


def extract_sar_and_find_binaries() -> List[str]:
  binaries: List[str] = []
  if os.path.isfile(SAR_LAUNCHER):
    env = dict(os.environ)
    env["SAR_EXTRACT_ONLY"] = "1"
    res = subprocess.run(
        [SAR_LAUNCHER],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    out = (res.stdout or "") + "\n" + (res.stderr or "")
    for m in re.finditer(r"(/tmp/sar\.server\.[^\s]+)", out):
      shardir = m.group(1).rstrip(".")
      candidate = os.path.join(
          shardir,
          "server_impl.runfiles/google3/third_party/jetski/cmd/hub/server/jetski-hub-server",
      )
      if os.path.isfile(candidate) and candidate not in binaries:
        binaries.append(candidate)

  for candidate in sorted(
      glob.glob(
          "/tmp/sar.server.*/server_impl.runfiles/google3/third_party/jetski/cmd/hub/server/jetski-hub-server"
      )
  ):
    if candidate not in binaries:
      binaries.append(candidate)
  return binaries


def build_padded_replacement(shim_len: int) -> bytes:
  raw_len = len(REPLACEMENT_BODY) + len(REPLACEMENT_SUFFIX)
  if raw_len > shim_len:
    raise ValueError(
        f"Replacement script ({raw_len} bytes) exceeds shim_len ({shim_len}"
        " bytes)"
    )
  padded = REPLACEMENT_BODY + (b" " * (shim_len - raw_len)) + REPLACEMENT_SUFFIX
  assert len(padded) == shim_len
  return padded


def write_patched_binary(bin_path: str, data: bytearray) -> None:
  parent_dir = os.path.dirname(bin_path)
  orig_parent_mode = stat.S_IMODE(os.stat(parent_dir).st_mode)
  os.chmod(parent_dir, orig_parent_mode | stat.S_IWUSR)

  tmp_path = bin_path + ".patched.tmp"
  with open(tmp_path, "wb") as f:
    f.write(data)
  os.chmod(tmp_path, 0o755)
  os.replace(tmp_path, bin_path)


def patch_binary(bin_path: str) -> bool:
  with open(bin_path, "rb") as f:
    data = bytearray(f.read())

  marker_pos = data.find(PATCH_MARKER)
  if marker_pos != -1:
    shim_start = marker_pos - len(SCRIPT_OPEN_TAG)
    shim_end_tag = data.find(REPLACEMENT_SUFFIX, marker_pos)
    if shim_start < 0 or shim_end_tag == -1:
      print(f"[WARN] Malformed patched shim in {bin_path}", file=sys.stderr)
      return False
    shim_end = shim_end_tag + len(REPLACEMENT_SUFFIX)
    shim_len = shim_end - shim_start
    padded_replacement = build_padded_replacement(shim_len)
    if bytes(data[shim_start:shim_end]) == padded_replacement:
      print(f"[OK] Binary already up to date: {bin_path}")
      return True
    data[shim_start:shim_end] = padded_replacement
    write_patched_binary(bin_path, data)
    print(f"[OK] Updated existing shim in {bin_path}")
    return True

  shim_start = data.find(SHIM_PREFIX)
  if shim_start == -1:
    print(f"[WARN] browserStorageShim not found in {bin_path}", file=sys.stderr)
    return False

  shim_end_tag = data.find(REPLACEMENT_SUFFIX, shim_start)
  if shim_end_tag == -1:
    print(
        f"[WARN] Closing </script> for browserStorageShim not found in"
        f" {bin_path}",
        file=sys.stderr,
    )
    return False

  shim_end = shim_end_tag + len(REPLACEMENT_SUFFIX)
  shim_len = shim_end - shim_start
  padded_replacement = build_padded_replacement(shim_len)

  # Locate `lea browserStorageShim(%rip), %rdi; mov $shim_len, %esi` in serveIndexWithConfig
  mov_esi = b"\xbe" + struct.pack("<I", shim_len)
  pos = 0
  lea_matches: List[int] = []
  while True:
    idx = data.find(mov_esi, pos)
    if idx == -1:
      break
    lea_pos = idx - 7
    if lea_pos >= 8 and data[lea_pos : lea_pos + 3] == b"\x48\x8d\x3d":
      disp32 = struct.unpack("<i", data[lea_pos + 3 : lea_pos + 7])[0]
      if idx + disp32 == shim_start:
        lea_matches.append(lea_pos)
    pos = idx + 1

  if len(lea_matches) != 1:
    print(
        f"[WARN] Expected 1 LEA reference to browserStorageShim in {bin_path},"
        f" found {len(lea_matches)}",
        file=sys.stderr,
    )
    return False

  lea_pos = lea_matches[0]
  # Preceding instructions: `test %cl, %cl` (84 c9), `je rel8` (74 1d), `mov %rbx, %rcx` (48 89 d9)
  je_pos: Optional[int] = None
  for offset in range(2, 10):
    cand = lea_pos - offset
    if data[cand] == 0x74 and data[cand - 2 : cand] == b"\x84\xc9":
      je_pos = cand
      break

  if je_pos is None:
    print(
        f"[WARN] Could not locate conditional jump before {hex(lea_pos)} in"
        f" {bin_path}",
        file=sys.stderr,
    )
    return False

  # Patch conditional jump `je` -> `nop; nop` so shim is always injected
  data[je_pos : je_pos + 2] = b"\x90\x90"
  data[shim_start:shim_end] = padded_replacement

  write_patched_binary(bin_path, data)
  print(
      f"[OK] Patched {bin_path} (je@{hex(je_pos)} -> NOP, shim@{hex(shim_start)}"
      f" [{shim_len} bytes])"
  )
  return True


def main() -> int:
  sync_artifact_js()
  binaries = extract_sar_and_find_binaries()
  if not binaries:
    print("[ERROR] No jetski-hub-server binaries found", file=sys.stderr)
    return 1

  any_ok = False
  for b in binaries:
    if patch_binary(b):
      any_ok = True
  return 0 if any_ok else 1


if __name__ == "__main__":
  sys.exit(main())
