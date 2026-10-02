#!/usr/bin/env python3
"""Chat lifecycle helper for Antigravity and Jetski.

Manages conversation titles, status markers (Scheme Γ), and annotation synchronization.
Synchronizes both `~/.gemini/{jetski,antigravity}/annotations/<id>.pbtxt` (the UI source of truth)
and `conversation_summaries.db` (the SQLite metadata cache).
"""

from datetime import datetime
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = os.environ.get("JETSKI_TIMEZONE", "America/New_York")
APP_DATA_DIR_CANDIDATES = ("~/.gemini/jetski", "~/.gemini/antigravity")
SUMMARY_DB_NAME = "conversation_summaries.db"
ANNOTATIONS_DIR_NAME = "annotations"

# Lifecycle marker tokens
MARKER_START = "▸"       # In-progress branch root
MARKER_STEP = "✓"        # In-progress completed step
MARKER_ACTIVE = "⦿"      # In-progress current active focal point
MARKER_CLOSED_START = "«"  # Finalized branch root
MARKER_CLOSED_STEP = "‹✓›" # Finalized intermediate step
MARKER_CLOSED_END = "»"    # Finalized branch tail
MARKER_SINGLE_CLOSED = "«»" # Finalized single-chat task

# Regex matching any existing lifecycle prefix: [HH:MM] [marker] Topic
TITLE_CLEANUP_RE = re.compile(
    r"^(?:\[\d{2}:\d{2}(?:\s+(?:продолжение|continue))?\]\s*(?:[▸✓⦿«»]|‹✓›|«»|\s*)*)"
    r"|^(?:(?:продолжение|continue)(?:\s+\d{2}:\d{2})?\s*:\s*)+",
    re.IGNORECASE,
)
TRAILING_CLEANUP_RE = re.compile(
    r"\s*\((?:продолжение|continue)\s+(?:\d{4}-\d{2}-\d{2}\s+)?\d{2}:\d{2}\)\s*$",
    re.IGNORECASE,
)
TIME_PREFIX_RE = re.compile(r"^\[(\d{2}:\d{2})\]")


def get_current_time_str() -> str:
  try:
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    return datetime.now(tz).strftime("%H:%M")
  except Exception:
    return datetime.now().strftime("%H:%M")


def extract_time_prefix(title: str, fallback_time: str = None) -> str:
  if title:
    m = TIME_PREFIX_RE.match(title.strip())
    if m:
      return m.group(1)
  return fallback_time or get_current_time_str()


def clean_base_title(title: str) -> str:
  if not title:
    return ""
  s = TITLE_CLEANUP_RE.sub("", title).strip()
  s = TRAILING_CLEANUP_RE.sub("", s).strip()
  return s


def find_app_data_dirs() -> list[str]:
  res = []
  env_dir = os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
  if env_dir:
    exp = os.path.abspath(os.path.expanduser(env_dir))
    if os.path.isdir(exp):
      res.append(exp)
  for c in APP_DATA_DIR_CANDIDATES:
    exp = os.path.abspath(os.path.expanduser(c))
    if os.path.isdir(exp) and exp not in res:
      res.append(exp)
  return res


def get_ls_csrf_token(ls_address: str) -> str:
  """Gets the CSRF token from environment or extracts it from the running Hub server."""
  token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
  if token:
    return token
  try:
    url = f"http://{ls_address}/" if not ls_address.startswith(("http://", "https://")) else ls_address
    req = urllib.request.Request(url, headers={"User-Agent": "chat_lifecycle"})
    with urllib.request.urlopen(req, timeout=1.0) as resp:
      html = resp.read().decode("utf-8", errors="ignore")
      m = re.search(r'"csrfToken":\s*"([^"]+)"', html)
      if m:
        return m.group(1)
  except Exception:
    pass
  return ""


def update_conversation_title_rpc(conv_id: str, new_title: str) -> bool:
  """Notifies running Language Server daemon via Connect RPC to update annotations.

  This pushes updates immediately to active Web UI streaming subscribers (sidebar),
  avoiding stale in-memory cache until manual chat selection.
  """
  ls_address = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "localhost:5387")
  csrf_token = get_ls_csrf_token(ls_address)
  if not csrf_token:
    return False

  if not ls_address.startswith(("http://", "https://")):
    url = f"http://{ls_address}/exa.language_server_pb.LanguageServerService/UpdateConversationAnnotations"
  else:
    url = f"{ls_address}/exa.language_server_pb.LanguageServerService/UpdateConversationAnnotations"

  payload = json.dumps({
      "cascade_id": conv_id,
      "annotations": {"title": new_title},
      "merge_annotations": True,
  }).encode("utf-8")

  req = urllib.request.Request(
      url,
      data=payload,
      headers={
          "Content-Type": "application/json",
          "x-codeium-csrf-token": csrf_token,
      },
      method="POST",
  )
  try:
    with urllib.request.urlopen(req, timeout=2.0) as resp:
      return resp.status == 200
  except Exception:
    return False


# ---------------------------------------------------------------------------
# Language Server RPC (Connect/JSON over HTTP, stdlib only) and model selection
# ---------------------------------------------------------------------------
LS_SERVICE_PATH = "exa.language_server_pb.LanguageServerService"
# Per-machine preference used when the model cannot be inherited:
#   $HANDOFF_MODEL, else first non-comment line of <app_data_dir>/handoff_model
# (model id like `gemini-3.8-flash-high`, display name, or enum name).
HANDOFF_MODEL_FILE_NAME = "handoff_model"
AGENTAPI_TIERS = ("flash_lite", "flash", "pro")


def ls_rpc(method: str, body: dict, timeout: float = 20.0) -> dict:
  """Calls a Language Server RPC via Connect/JSON (same channel the IDE uses)."""
  ls_address = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "localhost:5387")
  csrf_token = get_ls_csrf_token(ls_address)
  if not csrf_token:
    raise RuntimeError("no CSRF token for the Language Server")
  base = ls_address if ls_address.startswith(("http://", "https://")) else f"http://{ls_address}"
  req = urllib.request.Request(
      f"{base}/{LS_SERVICE_PATH}/{method}",
      data=json.dumps(body).encode("utf-8"),
      headers={"Content-Type": "application/json", "x-codeium-csrf-token": csrf_token},
      method="POST",
  )
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
      raw = resp.read()
  except urllib.error.HTTPError as e:
    detail = e.read().decode("utf-8", errors="replace")[:300]
    raise RuntimeError(f"{method}: HTTP {e.code} {detail}") from e
  return json.loads(raw or b"{}")


def get_available_models() -> dict:
  """Returns the live model catalog: {"models": {id: details}, "tieredModelIds": {...}, ...}."""
  return ls_rpc("GetAvailableModels", {}).get("response") or {}


def get_last_used_model(conv_id: str) -> str:
  """Model enum of the conversation's last turn (what the IDE picker shows), or ""."""
  try:
    r = ls_rpc(
        "GetCascadeTrajectoryGeneratorMetadata",
        {"cascadeId": conv_id, "generatorMetadataOffset": 0, "includeMessages": False},
    )
    gm = r.get("generatorMetadata") or []
    if gm:
      enum = (gm[-1].get("plannerConfig") or {}).get("planModel") or ""
      if enum:
        return enum
  except Exception:
    pass
  try:  # created with a static config (agentapi / this script) but no turn yet
    meta = ls_rpc("GetConversationMetadata", {"conversationId": conv_id}).get("metadata") or {}
    static = (meta.get("staticConfig") or {}).get("cascadeConfig") or {}
    return (static.get("plannerConfig") or {}).get("planModel") or ""
  except Exception:
    return ""


def read_handoff_model_override() -> str:
  val = os.environ.get("HANDOFF_MODEL", "").strip()
  if val:
    return val
  for d in find_app_data_dirs():
    p = os.path.join(d, HANDOFF_MODEL_FILE_NAME)
    if os.path.isfile(p):
      try:
        with open(p, "r", encoding="utf-8") as f:
          for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
              return line
      except Exception:
        pass
  return ""


def lookup_model(models: dict, wanted: str) -> dict:
  """Resolves a model id / display name / enum name among enabled catalog entries."""
  w = (wanted or "").strip().lower()
  if not w:
    return {}
  for mid, det in models.items():
    if det.get("disabled"):
      continue
    enum = det.get("model", "")
    label = det.get("displayName", "")
    if w in (mid.lower(), enum.lower(), label.lower()):
      return {"enum": enum, "id": mid, "label": label or mid}
  return {}


def resolve_handoff_model(current_conv_id: str, tier: str = "pro", explicit: str = "") -> dict:
  """Chooses the plan model for the continuation chat.

  Order: explicit (--model) -> inherited from the current conversation's last turn ->
  $HANDOFF_MODEL / <app_data_dir>/handoff_model -> first model of the agentapi tier.
  Every candidate is validated against the live catalog: an enum that is not in it
  fails at the first turn ("unknown model key MODEL_PLACEHOLDER_*").
  """
  resp = get_available_models()
  models = resp.get("models") or {}
  tiers = resp.get("tieredModelIds") or {}

  def from_tier(t: str) -> dict:
    key = {"flash_lite": "flashLite", "flash": "flash", "pro": "pro"}.get(t, "pro")
    for mid in tiers.get(key) or []:
      hit = lookup_model(models, mid)
      if hit:
        return {**hit, "source": f"tier:{t}"}
    return {}

  if explicit:
    if explicit in AGENTAPI_TIERS:
      hit = from_tier(explicit)
    else:
      hit = lookup_model(models, explicit)
      hit = {**hit, "source": "explicit"} if hit else {}
    if not hit:
      raise RuntimeError(f"--model '{explicit}' is not available in GetAvailableModels")
    return hit

  inherited = get_last_used_model(current_conv_id) if current_conv_id else ""
  hit = lookup_model(models, inherited)
  if hit:
    return {**hit, "source": "inherited"}

  hit = lookup_model(models, read_handoff_model_override())
  if hit:
    return {**hit, "source": "override"}

  hit = from_tier(tier)
  if hit:
    return hit
  raise RuntimeError("no usable model found in GetAvailableModels")


def start_conversation_exact(model_enum: str, title: str, prompt: str, project_id: str) -> str:
  """Creates a visible conversation pinned to an exact plan model.

  Mirrors `agentapi new-conversation` (StartCascade -> title -> first message), which
  can only express Gemini tiers. No sourceMetadata is sent, so the chat is listed in
  the sidebar (the equivalent of `env -u ANTIGRAVITY_SOURCE_METADATA`).
  """
  body = {
      "source": "CORTEX_TRAJECTORY_SOURCE_AGENT_API",
      "trajectoryType": "CORTEX_TRAJECTORY_TYPE_CASCADE",
      "customAgentSpec": {
          "codingAgent": {"googleMode": True},
          "commandExecutionPolicy": "eager",
          "enforcedWorkspaceValidation": False,
          "cascadeConfig": {"plannerConfig": {"planModel": model_enum}},
      },
      "projectEnvConfig": {"projectId": project_id, "defaultProjectEnvironment": {}},
  }
  new_cid = ls_rpc("StartCascade", body).get("cascadeId", "")
  if not new_cid:
    raise RuntimeError("StartCascade returned no cascadeId")
  ls_rpc(
      "UpdateConversationAnnotations",
      {"cascadeIds": [new_cid], "annotations": {"title": title}, "mergeAnnotations": True},
  )
  ls_rpc(
      "SendUserCascadeMessage",
      {"cascadeId": new_cid, "items": [{"text": prompt}], "blocking": False},
  )
  return new_cid


def update_conversation_title(conv_id: str, new_title: str) -> bool:
  """Updates conversation title via Language Server RPC and persists to disk."""
  if not conv_id or not new_title:
    return False

  # 1. Update running Language Server via RPC (pushes live update to UI stream)
  rpc_ok = update_conversation_title_rpc(conv_id, new_title)

  # 2. Synchronize on-disk files & DB as fallback / durability guarantee
  disk_ok = False
  for app_dir in find_app_data_dirs():
    # 2a. Update annotations .pbtxt
    ann_dir = os.path.join(app_dir, ANNOTATIONS_DIR_NAME)
    ann_file = os.path.join(ann_dir, f"{conv_id}.pbtxt")
    try:
      os.makedirs(ann_dir, exist_ok=True)
      if os.path.isfile(ann_file):
        with open(ann_file, "r", encoding="utf-8") as f:
          content = f.read()
        if 'title:"' in content or "title: \"" in content:
          new_content = re.sub(r'title:\s*"[^"]*"', f'title:"{new_title}"', content)
        else:
          new_content = f'title:"{new_title}" {content}'.strip()
      else:
        new_content = f'title:"{new_title}"\n'
      with open(ann_file, "w", encoding="utf-8") as f:
        f.write(new_content)
      disk_ok = True
    except Exception:
      pass

    # 2b. Update conversation_summaries.db
    db_file = os.path.join(app_dir, SUMMARY_DB_NAME)
    if os.path.isfile(db_file):
      try:
        conn = sqlite3.connect(db_file, timeout=2.0)
        with conn:
          conn.execute(
              "UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?",
              (new_title, conv_id),
          )
        conn.close()
        disk_ok = True
      except Exception:
        pass

  return rpc_ok or disk_ok


def get_conversation_title(conv_id: str) -> str:
  for app_dir in find_app_data_dirs():
    ann_file = os.path.join(app_dir, ANNOTATIONS_DIR_NAME, f"{conv_id}.pbtxt")
    if os.path.isfile(ann_file):
      try:
        with open(ann_file, "r", encoding="utf-8") as f:
          m = re.search(r'title:\s*"([^"]*)"', f.read())
          if m:
            return m.group(1).strip()
      except Exception:
        pass
    db_file = os.path.join(app_dir, SUMMARY_DB_NAME)
    if os.path.isfile(db_file):
      try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=1.0)
        cur = conn.cursor()
        cur.execute("SELECT title FROM conversation_summaries WHERE conversation_id = ?", (conv_id,))
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
          return str(row[0]).strip()
      except Exception:
        pass
  return ""


def get_parent_conversation_id(conv_id: str) -> str:
  """Finds parent conversation ID from transcript.jsonl if continued from an earlier chat."""
  if not conv_id:
    return ""
  for app_dir in find_app_data_dirs():
    tr_path = os.path.join(app_dir, "brain", conv_id, ".system_generated", "logs", "transcript.jsonl")
    if os.path.isfile(tr_path):
      try:
        with open(tr_path, "r", encoding="utf-8") as f:
          for line in f:
            if "USER_INPUT" in line:
              m = re.search(r"conversation://([a-zA-Z0-9_\-]+)", line)
              if m:
                return m.group(1)
              break
      except Exception:
        pass
  return ""


def discover_conversation_chain(conv_id: str) -> list[str]:
  """Traces backwards through transcripts to the root, returning [root_id, ..., conv_id]."""
  chain = []
  curr = conv_id
  seen = set()
  while curr and curr not in seen:
    seen.add(curr)
    chain.append(curr)
    curr = get_parent_conversation_id(curr)
  return list(reversed(chain))


def expand_chain_with_ancestors(chain_ids: list[str]) -> list[str]:
  """Ensures chain includes any prior ancestors leading up to chain_ids[0]."""
  if not chain_ids:
    return []
  ancestors = discover_conversation_chain(chain_ids[0])
  full_chain = []
  seen = set()
  for cid in ancestors + chain_ids:
    if cid and cid not in seen:
      seen.add(cid)
      full_chain.append(cid)
  return full_chain


def advance_in_progress_chain(chain_ids: list[str] = None, current_conv_id: str = "") -> list[str]:
  """Updates conversation titles in an active branch to reflect Scheme Γ markers.

  - Root (first) chat: [HH:MM] ▸ Topic
  - Intermediate closed chats: [HH:MM] ✓ Topic
  - Current active chat: [HH:MM] ⦿ Topic
  - Single standalone chat: [HH:MM] ⦿ Topic
  """
  if not chain_ids:
    chain_ids = [current_conv_id] if current_conv_id else []
  chain_ids = expand_chain_with_ancestors(chain_ids)
  if not chain_ids:
    return []

  now_time = get_current_time_str()
  updated = []

  base_topic = ""
  for cid in reversed(chain_ids):
    t = clean_base_title(get_conversation_title(cid))
    if t and t != "Investigation":
      base_topic = t
      break
  if not base_topic:
    base_topic = "Investigation"

  if len(chain_ids) == 1:
    cid = chain_ids[0]
    cur_title = get_conversation_title(cid)
    base = clean_base_title(cur_title) or base_topic
    new_title = f"[{now_time}] {MARKER_ACTIVE} {base}"
    update_conversation_title(cid, new_title)
    updated.append(cid)
    return updated

  # 1. First chat (root of branch) -> ▸
  root_id = chain_ids[0]
  cur_root = get_conversation_title(root_id)
  t_root = extract_time_prefix(cur_root, now_time)
  base_root = clean_base_title(cur_root) or base_topic
  update_conversation_title(root_id, f"[{t_root}] {MARKER_START} {base_root}")
  updated.append(root_id)

  # 2. Intermediate closed steps -> ✓
  for mid_id in chain_ids[1:-1]:
    cur_mid = get_conversation_title(mid_id)
    t_mid = extract_time_prefix(cur_mid, now_time)
    base_mid = clean_base_title(cur_mid) or base_topic
    update_conversation_title(mid_id, f"[{t_mid}] {MARKER_STEP} {base_mid}")
    updated.append(mid_id)

  # 3. Last chat (current active step) -> ⦿
  active_id = chain_ids[-1]
  cur_active = get_conversation_title(active_id)
  base_active = clean_base_title(cur_active) or base_topic
  update_conversation_title(active_id, f"[{now_time}] {MARKER_ACTIVE} {base_active}")
  updated.append(active_id)

  return updated


def finalize_conversation_chain(current_conv_id: str, chain_ids: list[str] = None) -> list[str]:
  """Finalizes the whole chain or single conversation to closed lifecycle markers.

  Original [HH:MM] timestamps are preserved; only the marker changes.
  """
  if not chain_ids:
    chain_ids = [current_conv_id] if current_conv_id else []
  chain_ids = expand_chain_with_ancestors(chain_ids)

  now_time = get_current_time_str()
  updated = []

  def _finalize_one(cid: str, marker: str) -> None:
    cur_title = get_conversation_title(cid)
    t = extract_time_prefix(cur_title, now_time)
    base = clean_base_title(cur_title) or "Investigation"
    update_conversation_title(cid, f"[{t}] {marker} {base}")
    updated.append(cid)

  if len(chain_ids) <= 1:
    cid = chain_ids[0] if chain_ids else current_conv_id
    if cid:
      _finalize_one(cid, MARKER_SINGLE_CLOSED)
    return updated

  # Multiple conversations in chain: « ... ‹✓› ... »
  _finalize_one(chain_ids[0], MARKER_CLOSED_START)
  for mid_id in chain_ids[1:-1]:
    _finalize_one(mid_id, MARKER_CLOSED_STEP)
  _finalize_one(chain_ids[-1], MARKER_CLOSED_END)

  return updated


def is_finalized_title(title: str) -> bool:
  if not title:
    return False
  return (
      MARKER_SINGLE_CLOSED in title
      or MARKER_CLOSED_END in title
      or MARKER_CLOSED_STEP in title
      or MARKER_CLOSED_START in title
  )


def reopen_conversation(conv_id: str, force: bool = False) -> bool:
  """Reopens a finalized conversation back to active marker ⦿ (and restores ancestors)."""
  cur_title = get_conversation_title(conv_id)
  if not cur_title:
    return False
  if not force and not is_finalized_title(cur_title):
    return False
  updated = advance_in_progress_chain(current_conv_id=conv_id)
  return bool(updated)


def set_conversation_topic(conv_id: str, topic: str, marker: str = MARKER_ACTIVE) -> str:
  """Sets the topic for a conversation, formatting as `[HH:MM] <marker> <clean_topic>`."""
  clean = clean_base_title(topic)
  if not clean:
    clean = "Investigation"
  time_str = get_current_time_str()
  new_title = f"[{time_str}] {marker} {clean}" if marker else f"[{time_str}] {clean}"
  ok = update_conversation_title(conv_id, new_title)
  return new_title if ok else ""


def get_handoff_summary_path(conv_id: str, topic: str = "") -> str:
  """Returns the canonical summary artifact path for conv_id, creating the dir if needed."""
  if not conv_id:
    return ""
  if not topic:
    cur_title = get_conversation_title(conv_id)
    topic = clean_base_title(cur_title) or "Investigation"
  slug = re.sub(r"[^\w\-]+", "_", topic, flags=re.UNICODE).strip("_")[:40]
  filename = f"handoff_summary_{slug}_{conv_id[:8]}.md"

  app_dirs = find_app_data_dirs()
  for d in app_dirs:
    candidate = os.path.join(d, "brain", conv_id, filename)
    if os.path.isfile(candidate):
      return candidate
  # Fallback to first existing app data dir
  base_dir = app_dirs[0] if app_dirs else os.path.expanduser("~/.gemini/jetski")
  target = os.path.join(base_dir, "brain", conv_id, filename)
  os.makedirs(os.path.dirname(target), exist_ok=True)
  return target


def create_handoff(
    current_conv_id: str,
    summary_file: str = "",
    notes: str = "",
    next_step_prompt: str = "",
    new_topic: str = "",
    model: str = "pro",
    exact_model: str = "",
) -> dict:
  """Executes a clean handoff to a new conversation.

  1. Resolves/verifies summary artifact file. If notes are provided, writes/enriches summary.
  2. Builds title based on existing topic or new_topic.
  3. Launches the continuation directly via Language Server RPC with an exact plan model
     (explicit --model -> inherited from the current chat's last turn -> $HANDOFF_MODEL /
     <app_data_dir>/handoff_model -> agentapi tier `model`), preserving ANTIGRAVITY_PROJECT_ID
     and sending no sourceMetadata (chat stays visible). Falls back to
     `agentapi new-conversation --model=<tier>` if the RPC path is unavailable.
  4. Advances lifecycle chain markers (advances old chat to ✓ and new to ⦿).
  5. Verifies sourceMetadata: null.
  """
  if not current_conv_id:
    raise ValueError("current_conv_id must not be empty")

  # 1. Determine base topic and new continuation title
  if not new_topic:
    cur_title = get_conversation_title(current_conv_id)
    new_topic = clean_base_title(cur_title) or "Investigation"
  time_str = get_current_time_str()
  cont_title = f"[{time_str}] {MARKER_ACTIVE} {new_topic}"

  # 2. Resolve summary artifact file
  if not summary_file:
    summary_file = get_handoff_summary_path(current_conv_id, new_topic)

  # Check if summary file exists; if not or if notes provided, create/update it
  if not os.path.isfile(summary_file) or notes:
    os.makedirs(os.path.dirname(summary_file), exist_ok=True)
    body_notes = f"\n\n## Status and Notes\n{notes.strip()}\n" if notes else ""
    with open(summary_file, "w", encoding="utf-8") as f:
      f.write(
          f"# Handoff Summary: {new_topic}\n\n"
          f"Continuation of conversation://{current_conv_id}.{body_notes}\n"
          f"## Next Steps\n- {next_step_prompt.strip() if next_step_prompt else 'Continue investigation/tasks from previous conversation.'}\n"
      )

  # 3. Build continuation prompt
  prompt = (
      f"Continuing unfinished work from previous conversation (conversation://{current_conv_id}). "
      f"Read {summary_file} via view_file, review completed steps and discarded hypotheses, "
      f"and immediately continue executing from the next step recorded in the summary without asking "
      f"for confirmation (unless the summary explicitly states it is waiting for user input)."
  )
  if next_step_prompt:
    prompt = f"{prompt}\n\nNext immediate task: {next_step_prompt}"

  # 4. Determine project ID to preserve
  project_id = os.environ.get("ANTIGRAVITY_PROJECT_ID", "")
  if not project_id:
    for d in find_app_data_dirs():
      db_file = os.path.join(d, SUMMARY_DB_NAME)
      if os.path.isfile(db_file):
        try:
          conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=1.0)
          cur = conn.cursor()
          cur.execute("SELECT project_id FROM conversation_summaries WHERE conversation_id = ?", (current_conv_id,))
          row = cur.fetchone()
          conn.close()
          if row and row[0]:
            project_id = str(row[0]).strip()
            break
        except Exception:
          pass

  # 5. Launch the continuation: exact plan model via LS RPC, else agentapi tier.
  project_id_final = project_id if project_id and project_id != "outside-of-project" else "outside-of-project"
  model_info: dict = {}
  new_cid = ""
  try:
    model_info = resolve_handoff_model(current_conv_id, tier=model, explicit=exact_model)
    new_cid = start_conversation_exact(model_info["enum"], cont_title, prompt, project_id_final)
  except Exception as exc:
    model_info = {"source": f"agentapi tier:{model}", "fallback_reason": str(exc)[:300]}
    env = {k: v for k, v in os.environ.items() if k != "ANTIGRAVITY_SOURCE_METADATA"}
    env["ANTIGRAVITY_PROJECT_ID"] = project_id_final
    cmd = [
        "agentapi",
        "new-conversation",
        f"--model={model}",
        f"--title={cont_title}",
        prompt,
    ]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if res.returncode != 0:
      raise RuntimeError(f"agentapi new-conversation failed (code {res.returncode}): {res.stderr or res.stdout}")
    m = re.search(r'"conversationId":\s*"([^"]+)"', res.stdout)
    if not m:
      raise RuntimeError(f"Failed to parse conversationId from agentapi output: {res.stdout}")
    new_cid = m.group(1)

  # 6. Verify sourceMetadata (lives under metadata.sourceMetadata; null => visible in the sidebar)
  fetched, source_meta = get_conversation_source_metadata(new_cid)

  # 7. Advance lifecycle markers across the chain
  advance_in_progress_chain(current_conv_id=new_cid)

  return {
      "new_conversation_id": new_cid,
      "title": cont_title,
      "summary_file": summary_file,
      "model": model_info,
      "source_metadata": source_meta,
      "verified_visible": bool(fetched and source_meta is None),
  }


def get_conversation_source_metadata(conv_id: str) -> tuple[bool, object]:
  """Returns (fetched, metadata.sourceMetadata) via LS RPC, else via agentapi."""
  try:
    meta = ls_rpc("GetConversationMetadata", {"conversationId": conv_id}).get("metadata")
    if isinstance(meta, dict):
      return True, meta.get("sourceMetadata")
  except Exception:
    pass
  try:
    res = subprocess.run(["agentapi", "get-conversation-metadata", conv_id], capture_output=True, text=True, check=False)
    data = json.loads(res.stdout)
    meta = ((data.get("response") or {}).get("conversationMetadata") or {}).get("metadata")
    if isinstance(meta, dict):
      return True, meta.get("sourceMetadata")
  except Exception:
    pass
  return False, None


def get_chain_status(conv_id: str) -> dict:
  """Inspects and returns the full chain status for conv_id without guessing or querying SQL manually."""
  if not conv_id:
    return {"error": "conv_id is required"}

  chain = discover_conversation_chain(conv_id)
  items = []
  for cid in chain:
    raw_title = get_conversation_title(cid)
    items.append({
        "conversation_id": cid,
        "title": raw_title,
        "base_topic": clean_base_title(raw_title),
        "is_finalized": is_finalized_title(raw_title),
        "is_current": (cid == conv_id),
    })

  # Summary files live in the brain dir of the chat that *produced* the handoff,
  # i.e. the predecessor — so walk the whole chain, not just conv_id.
  summaries = []
  for cid in chain:
    for app_dir in find_app_data_dirs():
      brain_dir = os.path.join(app_dir, "brain", cid)
      if os.path.isdir(brain_dir):
        for f in sorted(os.listdir(brain_dir)):
          if "handoff_summary" in f and f.endswith(".md"):
            summaries.append(os.path.join(brain_dir, f))

  cur_title = get_conversation_title(conv_id)
  return {
      "current_conversation_id": conv_id,
      "current_title": cur_title,
      "base_topic": clean_base_title(cur_title),
      "is_finalized": is_finalized_title(cur_title),
      "chain_length": len(chain),
      "chain": items,
      "summary_files": summaries,
  }


USAGE = """\
chat_lifecycle.py — deterministic chat lifecycle CLI (titles, handoffs, chains).

Usage:
  python3 ~/.gemini/config/hooks/chat_lifecycle.py <command> [args]

Commands:
  status [id]                         Show the chain for <id>: titles, markers,
                                      is_finalized, summary file paths. Read-only.
  set-title <id> "<Topic>"            Rename the chat. Time prefix [HH:MM] and the
                                      marker are added automatically.
  summary-path <id>                   Print the canonical handoff summary path
                                      (brain/<id>/handoff_summary_<slug>_<id8>.md).
  handoff <id> [summary_file]         Create a visible continuation chat in the same
          [--notes "..."]             project, verify sourceMetadata is null and
          [--next "..."]              update chain markers. Writes/updates the
          [--model <m>]               summary if --notes is given. The continuation
                                      keeps the model of <id>'s last turn (see Notes);
                                      --model pins one explicitly.
  model [id]                          Read-only: show the model <id> last used and the
                                      model a handoff would pick now.
  finalize <id>                       Close the whole chain. USER-ONLY: run it
                                      only on the user's explicit word ("финал").
                                      Timestamps are preserved.
  reopen <id>                         Re-activate a finalized chain. USER-ONLY.

Internal (used by hooks; do not run manually):
  set <id> "<raw title>"              Write a title verbatim, no marker handling.
  advance <id> [<id>...]              Recompute in-progress markers for a chain.

Notes:
  * <id> may be any conversation of the chain for finalize/reopen/status.
  * When <id> is omitted, $CONVERSATION_ID is used (set inside the agent's
    run_command only).
  * Markers (▸ ✓ ⦿ « ‹✓› » «») are derived mechanically from the chain; never
    choose or edit them by hand.
  * Model of a continuation: --model <id|display name|enum|flash_lite|flash|pro>
    -> model of <id>'s last turn (inherit) -> $HANDOFF_MODEL or the first
    non-comment line of <app_data_dir>/handoff_model -> agentapi tier "pro".
    Candidates are validated against GetAvailableModels. `agentapi
    new-conversation` itself knows only Gemini tiers (pro = "Gemini 3.1 Pro
    (Low)" in Antigravity), which is why the launch goes through the LS RPC.
"""


def print_usage(stream=None) -> None:
  print(USAGE, file=stream or sys.stdout, end="")


KNOWN_COMMANDS = (
    "set", "set-title", "summary-path", "status", "model", "handoff", "reopen", "advance", "finalize",
)


if __name__ == "__main__":
  if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
    print_usage()
    sys.exit(0)
  if sys.argv[1] not in KNOWN_COMMANDS:
    print(f"chat_lifecycle.py: unknown command '{sys.argv[1]}'\n", file=sys.stderr)
    print_usage(sys.stderr)
    sys.exit(2)

  if len(sys.argv) > 2 and sys.argv[1] == "set":
    cid = sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else ""
    update_conversation_title(cid, title)
  elif len(sys.argv) > 2 and sys.argv[1] == "set-title":
    cid = sys.argv[2]
    topic = sys.argv[3] if len(sys.argv) > 3 else ""
    marker = sys.argv[4] if len(sys.argv) > 4 else MARKER_ACTIVE
    new_t = set_conversation_topic(cid, topic, marker)
    print(f"Updated title for {cid}: {new_t}")
  elif len(sys.argv) > 1 and sys.argv[1] == "summary-path":
    cid = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("CONVERSATION_ID", "")
    topic = sys.argv[3] if len(sys.argv) > 3 else ""
    p = get_handoff_summary_path(cid, topic)
    print(p)
  elif len(sys.argv) > 1 and sys.argv[1] == "status":
    cid = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("CONVERSATION_ID", "")
    info = get_chain_status(cid)
    print(json.dumps(info, ensure_ascii=False, indent=2))
  elif len(sys.argv) > 1 and sys.argv[1] == "model":
    # Read-only diagnostic: what <id> last used and what a handoff would pick now.
    cid = sys.argv[2] if len(sys.argv) > 2 else (
        os.environ.get("CONVERSATION_ID", "") or os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "")
    )
    out = {"conversation_id": cid, "last_used_enum": get_last_used_model(cid) if cid else ""}
    try:
      out["handoff_would_use"] = resolve_handoff_model(cid)
    except Exception as exc:
      out["handoff_would_use"] = {"error": str(exc)[:300], "fallback": "agentapi tier:pro"}
    out["override_setting"] = read_handoff_model_override()
    print(json.dumps(out, ensure_ascii=False, indent=2))
  elif len(sys.argv) > 1 and sys.argv[1] == "handoff":
    # Usage: chat_lifecycle.py handoff [cid] [summary_file] [--notes "..."] [--next "..."] [--model <m>]
    args = sys.argv[2:]
    cid = ""
    summary = ""
    notes = ""
    next_step = ""
    exact_model = ""
    i = 0
    while i < len(args):
      if args[i] == "--notes" and i + 1 < len(args):
        notes = args[i + 1]
        i += 2
      elif args[i] == "--next" and i + 1 < len(args):
        next_step = args[i + 1]
        i += 2
      elif args[i] == "--model" and i + 1 < len(args):
        exact_model = args[i + 1]
        i += 2
      elif args[i].startswith("--model="):
        exact_model = args[i].split("=", 1)[1]
        i += 1
      elif not cid:
        cid = args[i]
        i += 1
      elif not summary:
        summary = args[i]
        i += 1
      else:
        i += 1
    if not cid:
      cid = os.environ.get("CONVERSATION_ID", "")
    result = create_handoff(cid, summary_file=summary, notes=notes, next_step_prompt=next_step, exact_model=exact_model)
    print(json.dumps(result, ensure_ascii=False, indent=2))
  elif len(sys.argv) > 2 and sys.argv[1] == "reopen":
    cid = sys.argv[2]
    reopen_conversation(cid, force=True)
  elif len(sys.argv) > 1 and sys.argv[1] == "advance":
    cids = sys.argv[2:]
    if len(cids) == 1:
      advance_in_progress_chain(current_conv_id=cids[0])
    else:
      advance_in_progress_chain(chain_ids=cids)
  elif len(sys.argv) > 1 and sys.argv[1] == "finalize":
    cids = sys.argv[2:]
    if len(cids) == 1:
      finalize_conversation_chain(cids[0])
    else:
      finalize_conversation_chain(cids[0] if cids else "", cids)
