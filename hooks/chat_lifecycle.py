#!/usr/bin/env python3
"""Chat lifecycle helper for Antigravity and Jetski.

Manages conversation titles, status markers (Scheme Γ), and annotation synchronization.
Synchronizes both `~/.gemini/{jetski,antigravity}/annotations/<id>.pbtxt` (the UI source of truth)
and `conversation_summaries.db` (the SQLite metadata cache).
"""

from __future__ import annotations

from datetime import datetime
import glob
import json
import os
import re
import shutil
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
CHAIN_INDEX_FILE_NAME = "jetski-chains.json"
DEFAULT_PROJECT_RETENTION_FILE = os.path.expanduser(
    "~/.gemini/config/projects_retention.json"
)
DEFAULT_AUTO_ARCHIVE_RETENTION: dict[str, int] = {
    "skia-g3-roll-check": 1,
}
DEFAULT_PROJECT_MODELS_FILE = os.path.expanduser(
    "~/.gemini/config/projects_models.json"
)
DEFAULT_PROJECT_MODELS: dict[str, str] = {
    "skia-g3-roll-check": "flash",
}

# Lifecycle marker tokens: only visible chats carry one. Finished chats and handed-off
# pieces are archived instead of re-marked
MARKER_ACTIVE = "⦿"      # Open chat that needs the user
MARKER_WAITING = "⏳"    # Open chat waiting on cron or an external event
# Pre-archive "closed" markers still found on old titles; audit --fix archives such chats
LEGACY_CLOSED_MARKERS = ("«", "‹✓›", "»", "«»")

# Matches the leading lifecycle marker token in "[HH:MM] <marker> <Topic>"
TITLE_MARKER_RE = re.compile(
    r"^\s*\[\d{1,2}:\d{2}\]\s*(‹✓›|«»|⏳|[✓⦿▸«»])(?:\s|$)"
)
# Regex matching any existing lifecycle prefix: [HH:MM] [marker] Topic
TITLE_CLEANUP_RE = re.compile(
    r"^(?:\[\d{2}:\d{2}(?:\s+(?:продолжение|continue))?\]\s*(?:[▸✓⦿⏳«»]|‹✓›|«»|\s*)*)"
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


# Dual-inherits ValueError and RuntimeError so both CLI/API and handoff callers catch it
class InvalidTopicError(ValueError, RuntimeError):
  """Raised when a chat topic is empty, single-word, bare entity ID, or overly generic"""


# Filler nouns/verbs/adjectives that cannot stand alone without a concrete domain subject
GENERIC_TOPIC_WORDS = frozenset({
    "investigation",
    "investigate",
    "investigating",
    "debugging",
    "debug",
    "bug",
    "bugs",
    "issue",
    "issues",
    "task",
    "tasks",
    "work",
    "working",
    "cl",
    "pr",
    "chat",
    "chats",
    "conversation",
    "conversations",
    "continuation",
    "continue",
    "continuing",
    "unfinished",
    "general",
    "misc",
    "miscellaneous",
    "question",
    "questions",
    "help",
    "fix",
    "fixes",
    "fixing",
    "update",
    "updates",
    "updating",
    "test",
    "tests",
    "testing",
    "review",
    "reviews",
    "reviewing",
    "analysis",
    "analyze",
    "analyzing",
    "check",
    "checks",
    "checking",
    "todo",
    "todos",
    "followup",
    "follow-up",
    "new",
    "current",
    "project",
    "projects",
    "code",
    "change",
    "changes",
    "refactor",
    "refactoring",
    "untitled",
    "handoff",
    "summary",
    "state",
    "исследование",
    "исследования",
    "исследованию",
    "исследованием",
    "исследовании",
    "отладка",
    "отладки",
    "отладке",
    "отладку",
    "отладкой",
    "вопрос",
    "вопроса",
    "вопросу",
    "вопросом",
    "вопросе",
    "вопросы",
    "вопросов",
    "задача",
    "задачи",
    "задач",
    "задаче",
    "задачу",
    "задачей",
    "продолжение",
    "проверка",
    "проверки",
    "проверок",
    "проверке",
    "проверку",
    "проверкой",
    "правки",
    "правка",
    "правок",
    "правкам",
    "правками",
    "работа",
    "работы",
    "работе",
    "работу",
    "работой",
    "чат",
    "чаты",
    "чата",
    "чату",
    "чатом",
    "чате",
    "чатов",
    "сл",
    "пр",
    "баг",
    "бага",
    "багу",
    "багом",
    "баге",
    "баги",
    "багов",
    "багам",
    "багами",
    "багах",
    "ошибка",
    "ошибки",
    "ошибок",
    "ошибке",
    "ошибку",
    "ошибкой",
    "тест",
    "теста",
    "тесту",
    "тестом",
    "тесте",
    "тесты",
    "тестов",
    "тестирование",
    "ревью",
    "обновление",
    "обновления",
    "изменения",
    "изменение",
    "исправление",
    "исправления",
    "фикс",
    "фикса",
    "фиксу",
    "фиксом",
    "фиксе",
    "фиксы",
    "фиксов",
    "анализ",
    "анализа",
    "анализу",
    "анализом",
    "анализе",
    "код",
    "кода",
    "коду",
    "кодом",
    "коде",
    "рефакторинг",
    "общий",
    "общая",
    "общее",
    "общие",
    "разное",
    "текущий",
    "текущая",
    "текущее",
    "новый",
    "новая",
    "новое",
    "новые",
})

# Prepositions, conjunctions, and articles ignored when counting meaningful topic words
TOPIC_STOP_WORDS = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "from", "by",
    "about", "into", "onto", "upon", "out", "up", "down", "over", "under",
    "and", "or", "but", "nor", "so", "yet", "as", "if", "than", "that", "this",
    "these", "those", "it", "its", "is", "are", "was", "were", "be", "been",
    "being", "do", "does", "did", "have", "has", "had", "can", "could", "will",
    "would", "shall", "should", "may", "might", "must", "some", "any", "all",
    "more", "most", "other", "another", "such", "no", "not", "only", "just",
    "very", "too", "also",
    "и", "в", "во", "на", "с", "со", "к", "ко", "по", "из", "от", "до", "для",
    "о", "об", "обо", "за", "над", "под", "при", "про", "без", "через", "между",
    "а", "но", "да", "или", "либо", "что", "как", "так", "же", "ли", "бы",
    "не", "ни", "это", "этот", "эта", "эти", "тот", "та", "те", "весь", "вся",
    "все", "всё", "мой", "моя", "мои", "наш", "наша", "наши",
})

# Conversational prompt verbs skipped when extracting subject words from user input
PROMPT_FILLER_WORDS = frozenset({
    "please", "look", "see", "can", "you", "lets", "let", "need", "want",
    "tell", "show", "make", "run", "start", "take", "give", "find", "why",
    "where", "how", "what", "when", "who", "which",
    "посмотри", "глянь", "пожалуйста", "плз", "давай", "сделай", "проверь",
    "нужно", "надо", "покажи", "скажи", "запусти", "разбери", "оцени",
    "проведи", "почему", "зачем", "когда", "где", "какой", "какие", "там",
    "тут", "здесь", "сейчас", "происходит",
})

# Collapses bare entity references into one token so "PR #123" or "cl/123" counts as 1 word
_ENTITY_UNIT_RE = re.compile(
    r"\b(?:crrev(?:\.com)?/c/\d+|cl/\d+|b/\d+|crbug(?:\.com)?/\d+)\b"
    r"|\b(?:PR|Issue|CL|СЛ|сл|bug|баг)\s*(?:#|:|\s)\s*\d+\b"
    r"|\b(?:commit|коммит|sha|rev|revision)\s*(?:#|:|\s)\s*[0-9a-f]{7,40}\b",
    re.IGNORECASE,
)
_TOPIC_WORD_RE = re.compile(
    r"[A-Za-zА-Яа-яЁё0-9_]+(?:[-.][A-Za-zА-Яа-яЁё0-9_]+)*"
)


# Returns (all_tokens, content_tokens, non_generic_tokens) after collapsing entity IDs
def _analyze_topic_tokens(topic: str) -> tuple[list[str], list[str], list[str]]:
  clean = clean_base_title(topic)
  if not clean:
    return [], [], []
  normalized = re.sub(r"\bfollow-up\b", "followup", clean, flags=re.IGNORECASE)
  normalized = _ENTITY_UNIT_RE.sub("ENTITY_ID_TOKEN", normalized)
  tokens = _TOPIC_WORD_RE.findall(normalized)
  content = [t for t in tokens if t.lower() not in TOPIC_STOP_WORDS]
  non_generic = [
      t
      for t in content
      if t != "ENTITY_ID_TOKEN" and t.lower() not in GENERIC_TOPIC_WORDS
  ]
  return tokens, content, non_generic


def is_valid_chat_topic(topic: str) -> bool:
  """True if topic has >= 2 meaningful words and is not purely generic or a bare entity ID"""
  tokens, content, non_generic = _analyze_topic_tokens(topic)
  if len(tokens) < 2 or len(content) < 2:
    return False
  return bool(non_generic)


def validate_chat_topic(topic: str) -> str:
  """Returns cleaned topic if valid, or raises InvalidTopicError with actionable guidance"""
  clean = clean_base_title(topic)
  tokens, content, non_generic = _analyze_topic_tokens(clean)
  if not clean or len(tokens) < 2 or len(content) < 2:
    raise InvalidTopicError(
        f"Invalid chat title topic {clean!r}: single-word titles and bare entity IDs are"
        " forbidden. Pass a specific 2-6 word topic (< 50 chars) naming the concrete"
        ' subsystem/feature/bug/PR/CL and action (e.g. "PR #194980 multi-span BiDi",'
        ' "FontCollection FaceCache thread safety").'
    )
  if not non_generic:
    raise InvalidTopicError(
        f"Invalid chat title topic {clean!r}: overly generic title composed only of"
        " filler words. Pass a specific 2-6 word topic (< 50 chars) naming the concrete"
        ' subsystem/feature/bug/PR/CL and action (e.g. "PR #194980 multi-span BiDi",'
        ' "FontCollection FaceCache thread safety").'
    )
  return clean


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


_csrf_cache: dict[str, str] = {}
# Cache the discovered desktop language_server endpoint across hook invocations
LS_ENDPOINT_CACHE_FILE = "/tmp/jetski_ls_endpoint.json"


def _pid_is_alive(pid: int) -> bool:
  if pid <= 0:
    return False
  try:
    os.kill(pid, 0)
    return True
  except OSError:
    return False


def discover_local_ls_endpoint() -> tuple[str, str]:
  """Discovers the local desktop language_server HTTPS address and CSRF token"""
  # Avoid probing the live desktop daemon during hermetic unit test runs
  if "unittest" in sys.modules and not os.environ.get("JETSKI_TEST_ALLOW_LS_DISCOVERY"):
    return "", ""
  try:
    if os.path.isfile(LS_ENDPOINT_CACHE_FILE):
      with open(LS_ENDPOINT_CACHE_FILE, "r", encoding="utf-8") as f:
        cached = json.load(f)
      if (
          isinstance(cached, dict)
          and _pid_is_alive(int(cached.get("pid") or 0))
          and cached.get("ls_address")
          and cached.get("csrf_token")
      ):
        return str(cached["ls_address"]), str(cached["csrf_token"])
  except Exception:
    pass

  try:
    ps_out = subprocess.check_output(
        ["ps", "-axo", "pid=,command="], text=True, timeout=1.0
    )
    candidates: list[tuple[int, int, str]] = []
    for line in ps_out.splitlines():
      if "language_server" not in line or "--csrf_token" not in line:
        continue
      m_pid = re.match(r"^\s*(\d+)\s+", line)
      m_tok = re.search(r"--csrf_token\s+([0-9a-fA-F\-]+)", line)
      if not (m_pid and m_tok):
        continue
      pid = int(m_pid.group(1))
      # Prefer the standalone Antigravity hub daemon over background IDE extensions
      prio = 0 if "--app_data_dir antigravity" in line else 1
      candidates.append((prio, pid, m_tok.group(1)))

    candidates.sort()
    for _, pid, tok in candidates:
      lsof_out = subprocess.check_output(
          ["lsof", "-Pan", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
          text=True,
          timeout=1.0,
      )
      m_port = re.search(r"127\.0\.0\.1:(\d+)\s+\(LISTEN\)", lsof_out)
      if m_port:
        addr = f"https://127.0.0.1:{m_port.group(1)}"
        try:
          with open(LS_ENDPOINT_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "ls_address": addr, "csrf_token": tok}, f)
        except OSError:
          pass
        return addr, tok
  except Exception:
    pass
  return "", ""


def get_ls_endpoint() -> tuple[str, str]:
  """Resolves (ls_address, csrf_token) from env, web hub, or local desktop process"""
  env_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "")
  env_tok = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
  if env_addr and env_tok:
    return env_addr, env_tok
  ls_address = env_addr or "localhost:5387"
  tok = get_ls_csrf_token(ls_address)
  if tok:
    return ls_address, tok
  if not env_addr:
    disc_addr, disc_tok = discover_local_ls_endpoint()
    if disc_addr and disc_tok:
      return disc_addr, disc_tok
  return ls_address, ""


def _urlopen_ls(req: urllib.request.Request, timeout: float):
  # Block unmocked HTTP calls to the live Language Server during unit tests
  if (
      "unittest" in sys.modules
      and not os.environ.get("JETSKI_TEST_ALLOW_LS_DISCOVERY")
      and getattr(urllib.request.urlopen, "__module__", "") == "urllib.request"
  ):
    raise urllib.error.URLError("Blocked live LS request during unittest")
  # Local desktop language_server uses an ephemeral self-signed localhost cert
  if req.full_url.startswith(("https://127.0.0.1", "https://localhost")):
    import ssl
    return urllib.request.urlopen(
        req, timeout=timeout, context=ssl._create_unverified_context()
    )
  return urllib.request.urlopen(req, timeout=timeout)


def get_ls_csrf_token(ls_address: str) -> str:
  """Gets the CSRF token from environment or extracts it from the running Hub server."""
  token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
  if token:
    return token
  # Scraping the Hub page costs up to 1 s and hooks issue several RPCs within
  # a 5 s budget, so a found token is reused for the life of the process
  if ls_address in _csrf_cache:
    return _csrf_cache[ls_address]
  try:
    url = f"http://{ls_address}/" if not ls_address.startswith(("http://", "https://")) else ls_address
    req = urllib.request.Request(url, headers={"User-Agent": "chat_lifecycle"})
    with _urlopen_ls(req, timeout=1.0) as resp:
      html = resp.read().decode("utf-8", errors="ignore")
      m = re.search(r'"csrfToken":\s*"([^"]+)"', html)
      if m:
        _csrf_cache[ls_address] = m.group(1)
        return m.group(1)
  except Exception:
    pass
  return ""


DEFAULT_PROJECTS_DIR = os.path.expanduser("~/.gemini/config/projects")
BADGE_RE = re.compile(
    r"(?:\s*·\s*(?:[⟳⚠●]\s*\d+)(?:\s+(?:[⟳⚠●]\s*\d+))*)+$"
)
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
HANDOFF_PARENT_RE = re.compile(
    r"(?:continu(?:ing|e)\b|продолж(?:аем|и)\b)"
    r"(?:(?!(?:split out of|separated from|coordinated from))[^\"\n]){0,200}?"
    r"(?:conversation://|Conversation ID:\s*|/brain/)([a-zA-Z0-9_\-]+)",
    re.IGNORECASE,
)


def strip_activity_badge(name: str) -> str:
  if not name:
    return ""
  return BADGE_RE.sub("", name).rstrip()


def resolve_project_id(
    project_arg: str, projects_dir: str = ""
) -> str:
  """Resolves a project name or UUID against ~/.gemini/config/projects/*.json.

  Matches case-insensitively and ignores live activity badges (e.g. ' · ⟳ 1').
  """
  raw = (project_arg or "").strip()
  if not raw:
    raise ValueError("Project name or ID must not be empty")
  if raw.lower() in ("outside-of-project", "unassigned", "none"):
    return "outside-of-project"

  clean_target = strip_activity_badge(raw).lower()
  pdir = projects_dir or os.path.expanduser(
      os.environ.get("ANTIGRAVITY_PROJECTS_DIR", DEFAULT_PROJECTS_DIR)
  )
  available_names: list[str] = []
  if os.path.isdir(pdir):
    for pfile in sorted(glob.glob(os.path.join(pdir, "*.json"))):
      try:
        with open(pfile, "r", encoding="utf-8") as f:
          pdata = json.load(f)
        if not isinstance(pdata, dict):
          continue
        pid = str(pdata.get("id") or "").strip()
        pname = strip_activity_badge(str(pdata.get("name") or "").strip())
        if pname:
          available_names.append(f"{pname} ({pid})")
        if pid and pid.lower() == raw.lower():
          return pid
        if pid and pname and pname.lower() == clean_target:
          return pid
      except (OSError, ValueError):
        continue

  # Allow raw UUIDs (or any identifier when projects_dir does not exist, e.g. in unit tests)
  if UUID_RE.match(raw) or not os.path.isdir(pdir):
    return raw

  avail_str = ", ".join(available_names) if available_names else "none found"
  raise ValueError(
      f"Unknown project '{project_arg}'. Available projects: {avail_str}"
  )


def update_conversation_title_rpc(conv_id: str, new_title: str) -> bool:
  """Notifies running Language Server daemon via Connect RPC to update annotations."""
  try:
    ls_rpc(
        "UpdateConversationAnnotations",
        {
            "cascadeIds": [conv_id],
            "annotations": {"title": new_title},
            "mergeAnnotations": True,
        },
        timeout=2.0,
    )
    return True
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
  ls_address, csrf_token = get_ls_endpoint()
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
    with _urlopen_ls(req, timeout=timeout) as resp:
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


def load_project_models() -> dict[str, str]:
  """Returns mapping of project identifier or name to preferred model string."""
  rules = dict(DEFAULT_PROJECT_MODELS)
  if os.path.isfile(DEFAULT_PROJECT_MODELS_FILE):
    try:
      with open(DEFAULT_PROJECT_MODELS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
      if isinstance(data, dict):
        for k, v in data.items():
          if isinstance(v, str) and v.strip():
            rules[k.strip()] = v.strip()
    except Exception:
      pass
  return rules


def save_project_models(rules: dict[str, str]) -> None:
  """Saves mapping of project identifier or name to preferred model string."""
  os.makedirs(os.path.dirname(DEFAULT_PROJECT_MODELS_FILE), exist_ok=True)
  with open(DEFAULT_PROJECT_MODELS_FILE, "w", encoding="utf-8") as f:
    json.dump(rules, f, ensure_ascii=False, indent=2)


PENDING_MOVES_FILENAME = ".pending_project_moves.json"


def _record_pending_move(app_data_dirs: list[str], cid: str, project_id: str) -> None:
  """Records a pending move in .pending_project_moves.json across app_data_dirs."""
  if not cid or not project_id:
    return
  for app_dir in app_data_dirs:
    if not app_dir or not os.path.isdir(app_dir):
      continue
    pfile = os.path.join(app_dir, PENDING_MOVES_FILENAME)
    pending: dict[str, str] = {}
    if os.path.isfile(pfile):
      try:
        with open(pfile, "r", encoding="utf-8") as f:
          data = json.load(f)
        if isinstance(data, dict):
          pending.update({str(k): str(v) for k, v in data.items() if k and v})
      except Exception:
        pass
    pending[cid] = project_id
    tmp_path = f"{pfile}.{os.getpid()}.tmp"
    try:
      with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(pending, f, indent=2)
      os.replace(tmp_path, pfile)
    except Exception:
      pass


def get_conversation_project_id(conv_id: str) -> str:
  """Finds project id for conversation using multi-tier resolution or environment."""
  if not conv_id:
    return os.environ.get("ANTIGRAVITY_PROJECT_ID", "").strip()
  app_dirs = find_app_data_dirs()
  for d in app_dirs:
    pfile = os.path.join(d, PENDING_MOVES_FILENAME)
    if os.path.isfile(pfile):
      try:
        with open(pfile, "r", encoding="utf-8") as f:
          data = json.load(f)
        if isinstance(data, dict) and conv_id in data:
          pid = str(data[conv_id]).strip()
          if pid:
            return pid
      except Exception:
        pass
    db_file = os.path.join(d, SUMMARY_DB_NAME)
    if os.path.isfile(db_file):
      try:
        from context_guard import _query_summary_db

        row = _query_summary_db(db_file, conv_id)
        if row and row[1]:
          return str(row[1]).strip()
      except Exception:
        pass

  try:
    from context_guard import get_conversation_db_info

    _, pid = get_conversation_db_info(conv_id)
    if pid:
      return pid
  except Exception:
    pass
  return os.environ.get("ANTIGRAVITY_PROJECT_ID", "").strip()


def get_project_model_preference(project_id_or_name: str, projects_dir: str = "") -> str:
  """Looks up preferred model for a project by ID or clean name.

  Checks projects_models.json (and built-in defaults) first, then falls back to
  inspecting the project's JSON definition file in ~/.gemini/config/projects/
  for an inline model setting or to resolve project name <-> ID
  """
  raw = (project_id_or_name or "").strip()
  if not raw or raw in ("outside-of-project", "unassigned", "none"):
    return ""

  rules = load_project_models()
  if raw in rules:
    return rules[raw]

  clean_raw = strip_activity_badge(raw).lower()
  for k, v in rules.items():
    if strip_activity_badge(k).lower() == clean_raw:
      return v

  pdir = projects_dir or os.path.expanduser(
      os.environ.get("ANTIGRAVITY_PROJECTS_DIR", DEFAULT_PROJECTS_DIR)
  )
  if os.path.isdir(pdir):
    for pfile in sorted(glob.glob(os.path.join(pdir, "*.json"))):
      try:
        with open(pfile, "r", encoding="utf-8") as f:
          pdata = json.load(f)
        if not isinstance(pdata, dict):
          continue
        pid = str(pdata.get("id") or "").strip()
        pname = strip_activity_badge(str(pdata.get("name") or "").strip())
        is_match = (
            (pid and pid.lower() == raw.lower())
            or (pname and pname.lower() == clean_raw)
        )
        if is_match:
          inline = (
              pdata.get("defaultModel")
              or pdata.get("model")
              or (pdata.get("settings") or {}).get("defaultModel")
              or (pdata.get("settings") or {}).get("model")
              or ""
          )
          if inline and isinstance(inline, str):
            return inline.strip()

          if pid in rules:
            return rules[pid]
          if pname:
            for k, v in rules.items():
              if strip_activity_badge(k).lower() == pname.lower():
                return v
      except (OSError, ValueError):
        continue
  return ""


def resolve_handoff_model(
    current_conv_id: str,
    tier: str = "pro",
    explicit: str = "",
    project_id: str = "",
) -> dict:
  """Chooses the plan model for the continuation chat.

  Order: explicit (--model) -> project default (if set) ->
  inherited from the current conversation's last turn ->
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

  pid = project_id or (get_conversation_project_id(current_conv_id) if current_conv_id else "")
  if pid and pid != "outside-of-project":
    proj_pref = get_project_model_preference(pid)
    if proj_pref:
      if proj_pref in AGENTAPI_TIERS:
        hit = from_tier(proj_pref)
      else:
        hit = lookup_model(models, proj_pref)
      if hit:
        return {**hit, "source": f"project:{pid}"}

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


# Catalog entries without a display name are internal (e.g. `snowball-subagent` for
# MODEL_PLACEHOLDER_M136) and must never reach a public commit trailer
INTERNAL_MODEL_RE = re.compile(r"MODEL_PLACEHOLDER_|subagent|placeholder", re.IGNORECASE)


def get_conversation_model_enum(conv_id: str) -> str:
  """Model enum that generated the conversation's latest step, or ""."""
  try:
    r = ls_rpc(
        "GetCascadeTrajectoryGeneratorMetadata",
        {"cascadeId": conv_id, "generatorMetadataOffset": 0, "includeMessages": False},
    )
    for gm in reversed(r.get("generatorMetadata") or []):
      enum = (gm.get("chatModel") or {}).get("model") or ""
      if enum:
        return enum
  except Exception:
    pass
  return get_last_used_model(conv_id)


def get_model_trailer(conv_id: str = "") -> str:
  """Returns git trailer e.g. 'Assisted-by: Gemini 3.8 Flash (High)' or fallback 'Assisted-by: AI'.

  Names only the model that actually ran the conversation, never handoff guesses such as
  the project default, and only by its public catalog display name.
  """
  try:
    enum = get_conversation_model_enum(conv_id) if conv_id else ""
    models = (get_available_models().get("models") or {}) if enum else {}
    for mid, det in models.items():
      if det.get("model") != enum:
        continue
      label = (det.get("displayName") or "").strip()
      if label and label != mid and not INTERNAL_MODEL_RE.search(label):
        return f"Assisted-by: {label}"
  except Exception:
    pass
  return "Assisted-by: AI"


class BeforeSendError(RuntimeError):
  """before_send failed; the chat exists (`cascade_id`) but got no message."""

  def __init__(self, cascade_id: str, cause: Exception):
    super().__init__(f"{cause} (empty chat {cascade_id} was not started)")
    self.cascade_id = cascade_id


def start_conversation_exact(
    model_enum: str, title: str, prompt: str, project_id: str, before_send=None
) -> str:
  """Creates a visible conversation pinned to an exact plan model.

  Mirrors `agentapi new-conversation` (StartCascade -> title -> first message), which
  can only express Gemini tiers. No sourceMetadata is sent, so the chat is listed in
  the sidebar (the equivalent of `env -u ANTIGRAVITY_SOURCE_METADATA`).
  before_send(cascade_id) runs before the first message; if it raises, the
  message is not sent and BeforeSendError is raised.
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
  if project_id and project_id != "outside-of-project":
    _record_pending_move(find_app_data_dirs(), new_cid, project_id)
  ls_rpc(
      "UpdateConversationAnnotations",
      {"cascadeIds": [new_cid], "annotations": {"title": title}, "mergeAnnotations": True},
  )
  if before_send:
    try:
      before_send(new_cid)
    except Exception as exc:
      raise BeforeSendError(new_cid, exc) from exc
  ls_rpc(
      "SendUserCascadeMessage",
      {"cascadeId": new_cid, "items": [{"text": prompt}], "blocking": False},
  )
  return new_cid


_PBTXT_TITLE_RE = re.compile(r'title:\s*"((?:\\.|[^"\\])*)"')


def _escape_pbtxt_str(val: str) -> str:
  # prototext requires escaped backslashes and quotes, and rejects literal newlines
  clean = " ".join(val.splitlines())
  return clean.replace("\\", "\\\\").replace('"', '\\"')


def _unescape_pbtxt_str(raw: str) -> str:
  return re.sub(r"\\(.)", r"\1", raw)


def update_conversation_title(conv_id: str, new_title: str) -> bool:
  """Updates conversation title via Language Server RPC and persists to disk."""
  new_title = " ".join((new_title or "").splitlines()).strip()
  if not conv_id or not new_title:
    return False

  # 1. Update running Language Server via RPC (pushes live update to UI stream)
  rpc_ok = update_conversation_title_rpc(conv_id, new_title)

  # 2. Synchronize on-disk files & DB as fallback / durability guarantee
  escaped_title = _escape_pbtxt_str(new_title)
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
        if _PBTXT_TITLE_RE.search(content):
          # Use a lambda replacement so re.sub never expands \n escapes in titles
          new_content = _PBTXT_TITLE_RE.sub(
              lambda _: f'title:"{escaped_title}"', content
          )
        else:
          new_content = f'title:"{escaped_title}" {content}'.strip()
      else:
        new_content = f'title:"{escaped_title}"\n'
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

  # If the initial RPC failed because an on-disk .pbtxt was malformed, retry after fixing disk
  if not rpc_ok and disk_ok:
    rpc_ok = update_conversation_title_rpc(conv_id, new_title)

  return rpc_ok or disk_ok


def get_conversation_title(conv_id: str) -> str:
  for app_dir in find_app_data_dirs():
    ann_file = os.path.join(app_dir, ANNOTATIONS_DIR_NAME, f"{conv_id}.pbtxt")
    if os.path.isfile(ann_file):
      try:
        with open(ann_file, "r", encoding="utf-8") as f:
          m = _PBTXT_TITLE_RE.search(f.read())
          if m:
            return _unescape_pbtxt_str(m.group(1)).strip()
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
          first_line = f.readline()
          if not first_line:
            continue
          try:
            obj = json.loads(first_line)
          except ValueError:
            obj = None
          if isinstance(obj, dict):
            if obj.get("type") != "USER_INPUT" or obj.get("step_index", 0) != 0:
              continue
            content = str(obj.get("content") or "")
            m = HANDOFF_PARENT_RE.search(content)
            if m:
              return m.group(1)
          else:
            if "USER_INPUT" in first_line:
              m = HANDOFF_PARENT_RE.search(first_line)
              if m:
                return m.group(1)
      except Exception:
        pass
  return ""


def _scan_transcript_graph() -> tuple[dict[str, str], dict[str, list[str]], dict[str, str]]:
  """Scans brain/*/transcript.jsonl to build (parent_of, children_of, created_at_of)."""
  parent_of: dict[str, str] = {}
  children_of: dict[str, list[str]] = {}
  created_at_of: dict[str, str] = {}
  for app_dir in find_app_data_dirs():
    brain_dir = os.path.join(app_dir, "brain")
    if not os.path.isdir(brain_dir):
      continue
    for cid in sorted(os.listdir(brain_dir)):
      if cid in created_at_of and cid in parent_of:
        continue
      tr_path = os.path.join(
          brain_dir, cid, ".system_generated", "logs", "transcript.jsonl"
      )
      if not os.path.isfile(tr_path):
        continue
      try:
        with open(tr_path, "r", encoding="utf-8") as f:
          first_line = f.readline()
        if not first_line:
          continue
        obj = json.loads(first_line)
        if not isinstance(obj, dict):
          continue
        if obj.get("type") != "USER_INPUT" or obj.get("step_index", 0) != 0:
          continue
        ts = str(obj.get("created_at") or "")
        if ts and cid not in created_at_of:
          created_at_of[cid] = ts
        content = str(obj.get("content") or "")
        m = HANDOFF_PARENT_RE.search(content)
        if m and cid not in parent_of:
          pid = m.group(1)
          parent_of[cid] = pid
          children_of.setdefault(pid, []).append(cid)
      except Exception:
        continue
  return parent_of, children_of, created_at_of


def _order_chain_from_root(
    root_id: str,
    children_of: dict[str, list[str]],
    created_at_of: dict[str, str],
) -> list[str]:
  """Orders a tree rooted at root_id into a linear chain with the active/latest branch last."""
  memo_max_ts: dict[str, str] = {}
  memo_has_children: dict[str, bool] = {}

  def _Children(nid: str, visited: set[str]) -> list[str]:
    return [ch for ch in children_of.get(nid, []) if ch not in visited]

  def _SubtreeInfo(nid: str, visited: set[str]) -> tuple[str, bool]:
    if nid in memo_max_ts:
      return memo_max_ts[nid], memo_has_children[nid]
    ch_list = _Children(nid, visited | {nid})
    max_ts = created_at_of.get(nid, "")
    for ch in ch_list:
      ch_ts, _ = _SubtreeInfo(ch, visited | {nid})
      if ch_ts > max_ts:
        max_ts = ch_ts
    memo_max_ts[nid] = max_ts
    memo_has_children[nid] = bool(ch_list)
    return max_ts, bool(ch_list)

  _SubtreeInfo(root_id, set())

  ordered: list[str] = []
  seen: set[str] = set()

  def _Dfs(nid: str) -> None:
    if nid in seen:
      return
    seen.add(nid)
    ordered.append(nid)
    ch_list = _Children(nid, seen)
    ch_list.sort(
        key=lambda c: (
            memo_max_ts.get(c, ""),
            memo_has_children.get(c, False),
            created_at_of.get(c, ""),
            c,
        )
    )
    for ch in ch_list:
      _Dfs(ch)

  _Dfs(root_id)
  return ordered


def link_visible_chains(
    visible_ids: set[str], raw_parent: dict[str, str]
) -> tuple[dict[str, str], dict[str, list[str]]]:
  """Links each visible chat to its nearest visible handoff ancestor.

  A chain is defined by handoff links alone: each step may carry its own
  topic, so titles never decide membership. Hidden intermediates (archived,
  tool-created) are skipped over, not treated as chain ends.
  """
  vis_parent: dict[str, str] = {}
  vis_children: dict[str, list[str]] = {}
  for cid in sorted(visible_ids):
    curr = raw_parent.get(cid)
    seen_anc = {cid}
    while curr and curr not in seen_anc:
      seen_anc.add(curr)
      if curr in visible_ids:
        vis_parent[cid] = curr
        vis_children.setdefault(curr, []).append(cid)
        break
      curr = raw_parent.get(curr)
  return vis_parent, vis_children


# Guard state records each launch before the continuation's transcript exists, so redirects
# are visible to the browser even when the new chat has not written its first step yet
def _scan_guard_redirects() -> dict[str, dict]:
  redirects: dict[str, dict] = {}
  for app_dir in find_app_data_dirs():
    brain_dir = os.path.join(app_dir, "brain")
    if not os.path.isdir(brain_dir):
      continue
    for cid in sorted(os.listdir(brain_dir)):
      if cid in redirects:
        continue
      guard_path = os.path.join(brain_dir, cid, "scratch", ".context_guard_state.json")
      if not os.path.isfile(guard_path):
        continue
      try:
        with open(guard_path, "r", encoding="utf-8") as f:
          data = json.load(f)
      except Exception:
        continue
      if not isinstance(data, dict):
        continue
      to_cid = data.get("handed_off_to")
      if not to_cid or not isinstance(to_cid, str):
        continue
      redirects[cid] = {
          "to": to_cid,
          "at": str(data.get("handed_off_at") or ""),
          "kind": str(data.get("handoff_kind") or "handoff"),
      }
  return redirects


def export_chain_index() -> dict:
  """Exports handoff parent links, creation timestamps and redirects to jetski-chains.json."""
  parent_of, _, created_at_of = _scan_transcript_graph()
  redirects = _scan_guard_redirects()
  for cid, r in redirects.items():
    to_cid, at_ts = r["to"], r["at"]
    # next-task starts an independent root chat, so it is never a chain link
    if r["kind"] != "next-task" and to_cid not in parent_of:
      parent_of[to_cid] = cid
    if at_ts and to_cid not in created_at_of:
      created_at_of[to_cid] = at_ts
  payload = {"parentOf": parent_of, "createdAt": created_at_of, "redirects": redirects}
  raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
  # Mirror the chain index into every active brain directory so /static/artifacts/ can serve it
  for app_dir in find_app_data_dirs():
    brain_dir = os.path.join(app_dir, "brain")
    if not os.path.isdir(brain_dir):
      try:
        os.makedirs(brain_dir, exist_ok=True)
      except OSError:
        continue
    dst = os.path.join(brain_dir, CHAIN_INDEX_FILE_NAME)
    tmp = dst + ".tmp"
    # Write to a temp file and atomically replace so browser fetches never observe partial JSON
    try:
      with open(tmp, "w", encoding="utf-8") as f:
        f.write(raw)
      os.replace(tmp, dst)
    except OSError:
      pass
  return payload


# SearchConversations returns at most this many chats per query
SEARCH_RPC_CAP = 100


# Ancestors of cid, cid, then the newest-child path below it: the same pieces the sidebar
# search panel (chainOf in jetski_closed_filter.js) treats as one chat. Sibling branches from
# a forced second handoff are other chats
def _graph_chain(cid: str, graph: tuple) -> list[str]:
  parent_of, children_of, created_at_of = graph
  chain, seen = [cid], {cid}
  while parent_of.get(chain[0]) and parent_of[chain[0]] not in seen:
    chain.insert(0, parent_of[chain[0]])
    seen.add(chain[0])
  cur = cid
  while True:
    kids = [c for c in children_of.get(cur, []) if c not in seen]
    if not kids:
      return chain
    cur = max(kids, key=lambda c: created_at_of.get(c, ""))
    seen.add(cur)
    chain.append(cur)


# Searches every chat, archived ones included, through the LS SearchConversations RPC (one
# best hit per chat) and tags each hit with its piece number in its handoff chain
def search_conversations(query: str) -> dict:
  results = ls_rpc("SearchConversations", {"query": query}).get("results") or []
  graph = _scan_transcript_graph()
  hits = []
  for r in results:
    cid = r.get("cascadeId") or ""
    chain = _graph_chain(cid, graph)
    hits.append({
        **r,
        "piece": chain.index(cid) + 1 if cid in chain else 1,
        "chain_length": len(chain),
        "chain_tail": chain[-1],
    })
  return {"hits": hits, "capped": len(results) >= SEARCH_RPC_CAP}


# Finds every user message and agent reply that contains query (case-insensitive) across all
# pieces of conv_id's chain, newest first. SearchConversations cannot do this: it keeps one
# hit per chat
def search_chain(query: str, conv_id: str) -> list[dict]:
  needle = query.strip().lower()
  chain = _graph_chain(conv_id, _scan_transcript_graph())
  hits = []
  for piece in range(len(chain), 0, -1):
    cid = chain[piece - 1]
    steps = ls_rpc("GetCascadeTrajectorySteps", {"cascadeId": cid}, timeout=60).get("steps") or []
    for idx in range(len(steps) - 1, -1, -1):
      step = steps[idx]
      if step.get("type") == "CORTEX_STEP_TYPE_USER_INPUT":
        who, text = "user", (step.get("userInput") or {}).get("userResponse") or ""
      elif step.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
        pr = step.get("plannerResponse") or {}
        who, text = "agent", pr.get("modifiedResponse") or pr.get("response") or ""
      else:
        continue
      at = text.lower().find(needle)
      if at < 0:
        continue
      hits.append({
          "cascadeId": cid, "piece": piece, "chain_length": len(chain), "step": idx,
          "who": who, "snippet": text[max(0, at - 80):at + len(needle) + 120].replace("\n", " "),
      })
  return hits


def discover_conversation_chain(
    conv_id: str,
    include_descendants: bool = True,
) -> list[str]:
  """Traces through transcripts to return the full chain [root_id, ..., leaf_id]."""
  if not conv_id:
    return []
  chain = []
  curr = conv_id
  seen = set()
  while curr and curr not in seen:
    seen.add(curr)
    chain.append(curr)
    curr = get_parent_conversation_id(curr)
  ancestors = list(reversed(chain))
  if not include_descendants or not ancestors:
    return ancestors

  _, children_of, created_at_of = _scan_transcript_graph()
  if not children_of:
    return ancestors

  root_id = ancestors[0]
  ordered = _order_chain_from_root(root_id, children_of, created_at_of)
  # Ensure all ancestors are preserved in case any lacked a transcript on disk
  full_chain: list[str] = []
  seen_out: set[str] = set()
  for cid in ancestors + ordered:
    if cid and cid not in seen_out:
      seen_out.add(cid)
      full_chain.append(cid)
  return full_chain


def expand_chain_with_ancestors(chain_ids: list[str]) -> list[str]:
  """Ensures chain includes any prior ancestors (and descendants if a single ID is passed)."""
  if not chain_ids:
    return []
  include_desc = len(chain_ids) == 1
  discovered = discover_conversation_chain(
      chain_ids[0], include_descendants=include_desc
  )
  full_chain = []
  seen = set()
  for cid in discovered + chain_ids:
    if cid and cid not in seen:
      seen.add(cid)
      full_chain.append(cid)
  return full_chain


# Archives every piece of conv_id's chain: ancestors and the newest-child path, the same
# pieces chain search treats as one chat. Sibling branches from a forced second handoff are
# other chats and stay as they are
def archive_chain(conv_id: str) -> list[str]:
  res = archive_conversations(_graph_chain(conv_id, _scan_transcript_graph()))
  export_chain_index()
  return res["archived_ids"]


# Parses the leading lifecycle marker token from "[HH:MM] <marker> <Topic>"
def parse_title_marker(title: str) -> str:
  if not title:
    return ""
  m = TITLE_MARKER_RE.match(title)
  return m.group(1) if m else ""


# Reads the archived flag the LS mirrors into annotations/<id>.pbtxt
def is_archived(conv_id: str) -> bool:
  for app_dir in find_app_data_dirs():
    ann_file = os.path.join(app_dir, ANNOTATIONS_DIR_NAME, f"{conv_id}.pbtxt")
    try:
      with open(ann_file, "r", encoding="utf-8") as f:
        if re.search(r"\barchived\s*:\s*true\b", f.read()):
          return True
    except OSError:
      continue
  return False


# Checks whether an open leaf chat title carries the explicit leading ⏳ waiting marker
def is_waiting_title(title: str) -> bool:
  return parse_title_marker(title or "") == MARKER_WAITING


_USER_REQ_TAG_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)
_CLEAN_PREFIX_RE = re.compile(
    r"^(?:Chain\s+State|Handoff\s+Summary|Summary|State|Goal|Immediate\s+Next\s+Step"
    r"|Next\s+immediate\s+task|Next\s+task)\s*:\s*",
    re.IGNORECASE,
)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL_OR_FILE_RE = re.compile(r"(?:https?://|file://)\S+")


def _clean_candidate_topic(text: str, max_len: int = 38) -> str:
  """Sanitizes raw prompt or state lines into a concise candidate chat topic."""
  if not text:
    return ""
  m_tag = _USER_REQ_TAG_RE.search(text)
  raw = m_tag.group(1) if m_tag else text
  raw = _MD_LINK_RE.sub(r"\1", raw)
  raw = _URL_OR_FILE_RE.sub(" ", raw)
  raw = re.sub(r"[`#*_-]", " ", raw)
  first_line = raw.strip().splitlines()[0] if raw.strip() else ""
  first_line = _CLEAN_PREFIX_RE.sub("", first_line).strip()
  if not first_line:
    return ""
  # If the first clause before dash, colon or semicolon is already a valid topic, prefer it
  parts = re.split(r"\s+[—–-]\s+|[:;\n(]", first_line, maxsplit=1)
  if len(parts) > 1 and is_valid_chat_topic(parts[0].strip()):
    first_line = parts[0].strip()
  tokens = [
      t
      for t in _TOPIC_WORD_RE.findall(first_line)
      if t.lower() not in TOPIC_STOP_WORDS
  ]
  if not tokens:
    return ""
  # Truncate at word boundary to <= 6 words and <= max_len characters
  words = first_line.split()[:6]
  res = ""
  for w in words:
    cand = f"{res} {w}".strip() if res else w
    if len(cand) > max_len:
      break
    res = cand
  return res.rstrip()


def _extract_prompt_subject_words(text: str, max_words: int = 4) -> str:
  """Extracts non-generic domain tokens from a prompt following an entity identifier."""
  if not text:
    return ""
  m_tag = _USER_REQ_TAG_RE.search(text)
  raw = m_tag.group(1) if m_tag else text
  raw = _PASTED_LIFECYCLE_TITLE_RE.sub(" ", raw)
  raw = _URL_OR_FILE_RE.sub(" ", raw)
  raw = _ENTITY_GERRIT_MENTION_RE.sub(" ", raw)
  raw = _ENTITY_CRREV_MENTION_RE.sub(" ", raw)
  raw = _ENTITY_UNIT_RE.sub(" ", raw)
  tokens = _TOPIC_WORD_RE.findall(raw)
  subject_tokens: list[str] = []
  for t in tokens:
    low = t.lower()
    if (
        low in TOPIC_STOP_WORDS
        or low in PROMPT_FILLER_WORDS
        or low in GENERIC_TOPIC_WORDS
        or re.match(r"^\d+$", t)
        or UUID_RE.match(t)
    ):
      continue
    subject_tokens.append(t)
    if len(subject_tokens) >= max_words:
      break
  res = " ".join(subject_tokens)
  return res[:32].rstrip()


def _first_user_prompt_text(conv_id: str) -> str:
  if not conv_id:
    return ""
  for app_dir in find_app_data_dirs():
    tr_path = os.path.join(
        app_dir,
        "brain",
        conv_id,
        ".system_generated",
        "logs",
        "transcript.jsonl",
    )
    if os.path.isfile(tr_path):
      try:
        with open(tr_path, "r", encoding="utf-8") as f:
          for line in f:
            if '"USER_INPUT"' not in line:
              continue
            try:
              obj = json.loads(line)
            except Exception:
              continue
            if obj.get("type") == "USER_INPUT":
              content = str(obj.get("content") or "")
              if (
                  HANDOFF_PARENT_RE.search(content)
                  or "Continuing unfinished work from previous conversation"
                  in content
              ):
                return ""
              m_tag = _USER_REQ_TAG_RE.search(content)
              return (m_tag.group(1) if m_tag else content).strip()
      except Exception:
        pass
  return ""


def derive_fallback_topic(
    conv_id: str,
    state_file: str = "",
    notes: str = "",
    next_step: str = "",
    prompt: str = "",
) -> str:
  """Derives a concrete 2-6 word topic (< 50 chars) from chain, state file, roadmap, or prompt."""
  # Priority 1: Non-generic title already present on conv_id or its ancestor chain
  if conv_id:
    cur_t = clean_base_title(get_conversation_title(conv_id))
    if is_valid_chat_topic(cur_t):
      return cur_t
    chain = discover_conversation_chain(conv_id, include_descendants=False)
    for cid in reversed(chain):
      cand_t = clean_base_title(get_conversation_title(cid))
      if is_valid_chat_topic(cand_t):
        return cand_t

  # Priority 2: Chain state file or handoff summary heading / Goal
  candidate_files: list[str] = []
  if state_file and os.path.isfile(state_file):
    candidate_files.append(state_file)
  pid = get_conversation_project_id(conv_id) if conv_id else ""
  in_project = bool(pid) and pid != "outside-of-project"
  if in_project:
    try:
      import roadmap

      sf = roadmap.state_path(pid, conv_id)
      if os.path.isfile(sf) and sf not in candidate_files:
        candidate_files.append(sf)
    except Exception:
      pass
  if conv_id:
    for app_dir in find_app_data_dirs():
      brain_dir = os.path.join(app_dir, "brain", conv_id)
      if os.path.isdir(brain_dir):
        for fn in sorted(os.listdir(brain_dir)):
          if "handoff_summary" in fn and fn.endswith(".md"):
            candidate_files.append(os.path.join(brain_dir, fn))

  for fpath in candidate_files:
    try:
      with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
      for line in lines[:15]:
        line_s = line.strip()
        if line_s.startswith("# ") or line_s.startswith("## Goal"):
          cleaned = line_s.lstrip("#").strip()
          if cleaned.lower().startswith("goal"):
            cleaned = re.sub(
                r"^goal\s*:\s*", "", cleaned, flags=re.IGNORECASE
            ).strip()
          cand = _clean_candidate_topic(cleaned)
          if is_valid_chat_topic(cand):
            return cand
    except Exception:
      continue

  # Priority 3: Active doing roadmap item owned by or linked to conv_id
  if in_project:
    try:
      import roadmap

      doc = roadmap.load(pid)
      for it in doc.get("items", []):
        if it.get("status") == "doing" and (
            it.get("owner") == conv_id or it.get("link") == conv_id
        ):
          cand = f"{it['id']} {_clean_candidate_topic(it.get('text', ''), 32)}".strip()
          if is_valid_chat_topic(cand):
            return cand
    except Exception:
      pass

  # Priority 4: Concise topic from next_step, notes, prompt, or first user prompt
  first_prompt = _first_user_prompt_text(conv_id) if conv_id else ""
  for candidate_text in (next_step, notes, prompt, first_prompt):
    if not candidate_text:
      continue
    ent = extract_entity_topic(candidate_text)
    subj = _extract_prompt_subject_words(candidate_text)
    if ent and subj:
      combined = f"{ent} {subj}"[:42].rstrip()
      if is_valid_chat_topic(combined):
        return combined
    cand = _clean_candidate_topic(candidate_text)
    if is_valid_chat_topic(cand):
      return cand

  return ""


# Switches or toggles an open leaf chat between ⦿ (Needs Me) and ⏳ (Waiting External/Cron)
def set_conversation_wait_state(
    conv_id: str, waiting: bool | None = None
) -> str:
  if not conv_id:
    return ""
  cur_title = get_conversation_title(conv_id)
  # Archived chats are finished or handed off; only visible chats switch between ⦿ and ⏳
  if is_archived(conv_id):
    return cur_title
  # Toggle when waiting is None so UI and CLI callers can flip state in one call
  target_waiting = (not is_waiting_title(cur_title)) if waiting is None else bool(waiting)
  target_marker = MARKER_WAITING if target_waiting else MARKER_ACTIVE
  ts = extract_time_prefix(cur_title, get_current_time_str())
  raw_base = clean_base_title(cur_title)
  base = (
      raw_base
      if (raw_base and raw_base.lower() not in GENERIC_TOPIC_WORDS)
      else (derive_fallback_topic(conv_id) or raw_base or f"Chat {conv_id[:8]}")
  )
  new_title = f"[{ts}] {target_marker} {base}"
  if cur_title != new_title:
    update_conversation_title(conv_id, new_title)
  return new_title


# Brings a finished chat back into the sidebar as an open ⦿ chat (⏳ is kept)
def reopen_conversation(conv_id: str) -> bool:
  archive_conversations([conv_id], archived=False)
  cur_title = get_conversation_title(conv_id)
  if not cur_title:
    return False
  if parse_title_marker(cur_title) not in (MARKER_ACTIVE, MARKER_WAITING):
    ts = extract_time_prefix(cur_title, get_current_time_str())
    raw_base = clean_base_title(cur_title)
    base = (
        raw_base
        if (raw_base and raw_base.lower() not in GENERIC_TOPIC_WORDS)
        else (derive_fallback_topic(conv_id) or raw_base or f"Chat {conv_id[:8]}")
    )
    update_conversation_title(conv_id, f"[{ts}] {MARKER_ACTIVE} {base}")
  return True


def set_conversation_topic(
    conv_id: str, topic: str, marker: str = "", validate: bool = True
) -> str:
  """Sets the topic for a conversation, formatting as `[HH:MM] <marker> <clean_topic>`."""
  if validate:
    clean = validate_chat_topic(topic)
  else:
    clean = clean_base_title(topic)
    if not clean:
      raise InvalidTopicError("Topic must not be empty")
  # Keep ⏳ when the caller does not pick a marker; renaming must not wake a cron chat
  if not marker:
    cur_marker = parse_title_marker(get_conversation_title(conv_id))
    marker = MARKER_WAITING if cur_marker == MARKER_WAITING else MARKER_ACTIVE
  time_str = get_current_time_str()
  new_title = f"[{time_str}] {marker} {clean}" if marker else f"[{time_str}] {clean}"
  ok = update_conversation_title(conv_id, new_title)
  return new_title if ok else ""


# Regexes to extract entity identifiers from prompts for automatic chat titling:
_ENTITY_GERRIT_URL_RE = re.compile(
    r"https?://([a-zA-Z0-9_.-]*-review)\.(?:googlesource\.com|git\.corp\.google\.com)/\S*?(\d{5,8})\b"
)
_ENTITY_GITHUB_PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/(\d+)")
_ENTITY_GITHUB_ISSUE_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/issues/(\d+)")
_ENTITY_BUG_URL_RE = re.compile(
    r"https?://(?:issues\.chromium\.org|b\.corp\.google\.com)/issues/(\d+)"
)
_ENTITY_COMMIT_URL_RE = re.compile(
    r"https?://(?:github\.com/[^/\s]+/[^/\s]+/commit|"
    r"[a-zA-Z0-9_.-]+\.googlesource\.com/[^/\s]+/\+)/([0-9a-f]{7,40})"
)
# Jetski renders a mentioned Gerrit change as "@[<repo> | <number>:<subject>]"; a "/" in the
# label would mean an owner/repo GitHub mention, whose number is not a CL
_ENTITY_GERRIT_MENTION_RE = re.compile(r"@\[[^|/\]]+\|\s*(\d{5,8}):")
_ENTITY_CRREV_MENTION_RE = re.compile(
    r"@\[(?:crrev(?:\.com)?/c/|chromium-review[^|/\]]*/c/|chromium/src/\+/)?(\d{5,8})[:\]|]"
)
_ENTITY_CRREV_SHORT_RE = re.compile(r"(?:^|[\s(])crrev(?:\.com)?/c/(\d{5,8})\b", re.IGNORECASE)
_ENTITY_CL_SHORT_RE = re.compile(r"(?:^|[\s(])cl/(\d+)\b", re.IGNORECASE)
_ENTITY_BUG_SHORT_RE = re.compile(r"(?:^|[\s(])b/(\d+)\b", re.IGNORECASE)
_ENTITY_CRBUG_SHORT_RE = re.compile(r"(?:^|[\s(])crbug(?:\.com)?/(\d+)\b", re.IGNORECASE)
_ENTITY_PR_TEXT_RE = re.compile(r"\bPR\s*[:#\s]*(\d+)\b", re.IGNORECASE)
_ENTITY_CL_TEXT_RE = re.compile(r"(?:\bCL|(?:\b|\s)(?:СЛ|сл))\s*[:#\s]*(\d{5,8})\b")
_ENTITY_BUG_TEXT_RE = re.compile(r"(?:\b(?:bug|баг))\s*[:#\s]*(\d{6,10})\b", re.IGNORECASE)
_ENTITY_COMMIT_TEXT_RE = re.compile(
    r"(?:\b(?:commit|коммит|sha|rev|revision))\s*[:#\s]*([0-9a-f]{7,40})\b", re.IGNORECASE
)
# Matches quoted or pasted lifecycle chat titles like "[18:51] ⦿ cl/1405639 ..." so they
# do not hijack new chat titles with foreign CL/PR numbers
_PASTED_LIFECYCLE_TITLE_RE = re.compile(
    r"[`\"'«“‘]?\s*\[\d{1,2}:\d{2}\]\s*(?:‹✓›|«»|⏳|[✓⦿▸«»])"
    r"(?:[^`\"'»“”’\n\]]{0,60}?)(?:[`\"'»”’]|(?=[`\"'»“”’\],;\.!?\n\(\)]|\s+[\u0400-\u04ff]|"
    r"\s+(?:and|with|vs\.?|versus|or|but|is|was|are|has|have|had|can|could|will|would|please|"
    r"why|where|how|now)\b|\s*—|\s*-{2,}|\s*$))",
    re.IGNORECASE,
)


def extract_entity_topic(text: str) -> str | None:
  """Extracts a canonical entity topic (e.g. 'cl/12345', 'PR #123') from prompt text."""
  if not text:
    return None
  # Ignore quoted lifecycle chat titles so examples never hijack current topic
  text = _PASTED_LIFECYCLE_TITLE_RE.sub(" ", text)

  m = _ENTITY_CRREV_SHORT_RE.search(text) or _ENTITY_CRREV_MENTION_RE.search(text)
  if m:
    return f"crrev/c/{m.group(1)}"

  m = _ENTITY_GERRIT_URL_RE.search(text)
  if m:
    host = m.group(1).lower()
    cl_num = m.group(2)
    if "chromium" in host or "crrev" in host:
      return f"crrev/c/{cl_num}"
    return f"cl/{cl_num}"

  m = _ENTITY_GITHUB_PR_URL_RE.search(text)
  if m:
    return f"PR #{m.group(1)}"
  m = _ENTITY_GITHUB_ISSUE_URL_RE.search(text)
  if m:
    return f"Issue #{m.group(1)}"
  m = _ENTITY_BUG_URL_RE.search(text)
  if m:
    return f"b/{m.group(1)}"
  m = _ENTITY_COMMIT_URL_RE.search(text)
  if m:
    return f"commit {m.group(1)[:8]}"
  m = _ENTITY_GERRIT_MENTION_RE.search(text)
  if m:
    return f"cl/{m.group(1)}"
  m = _ENTITY_CL_SHORT_RE.search(text)
  if m:
    return f"cl/{m.group(1)}"
  m = _ENTITY_BUG_SHORT_RE.search(text)
  if m:
    return f"b/{m.group(1)}"
  m = _ENTITY_CRBUG_SHORT_RE.search(text)
  if m:
    return f"crbug/{m.group(1)}"
  m = _ENTITY_PR_TEXT_RE.search(text)
  if m:
    return f"PR #{m.group(1)}"
  m = _ENTITY_CL_TEXT_RE.search(text)
  if m:
    return f"cl/{m.group(1)}"
  m = _ENTITY_BUG_TEXT_RE.search(text)
  if m:
    return f"b/{m.group(1)}"
  m = _ENTITY_COMMIT_TEXT_RE.search(text)
  if m:
    return f"commit {m.group(1)[:8]}"
  return None


def auto_title_conversation(conv_id: str, prompt: str) -> str:
  """Titles an untitled conversation if prompt mentions a PR, CL, bug, or commit."""
  if not conv_id or not prompt:
    return ""
  cur_title = get_conversation_title(conv_id)
  cur_base = clean_base_title(cur_title)
  # Respect existing valid title: never overwrite a chat that already
  # has a specific multi-word topic
  if cur_title and TIME_PREFIX_RE.match(cur_title.strip()) and is_valid_chat_topic(cur_base):
    return ""
  entity = extract_entity_topic(prompt)
  if not entity:
    return ""
  subj = _extract_prompt_subject_words(prompt, max_words=3)
  topic = f"{entity} {subj}"[:42].rstrip() if subj else entity
  if cur_base == topic and TIME_PREFIX_RE.match((cur_title or "").strip()):
    return ""
  # Use validate=False so single-word/bare-entity auto-titles set the base topic without crashing
  return set_conversation_topic(conv_id, topic, validate=False)


def get_handoff_summary_path(conv_id: str, topic: str = "") -> str:
  """Returns the canonical summary artifact path for conv_id, creating the dir if needed."""
  if not conv_id:
    return ""
  if not topic:
    cur_title = get_conversation_title(conv_id)
    topic = clean_base_title(cur_title) or derive_fallback_topic(conv_id) or conv_id[:8]
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


def guard_state_path(conv_id: str) -> str:
  """The context_guard state file of conv_id (artifact_dir/scratch/.context_guard_state.json)."""
  app_dirs = find_app_data_dirs()
  base_dir = app_dirs[0] if app_dirs else os.path.expanduser("~/.gemini/jetski")
  for d in app_dirs:
    if os.path.isdir(os.path.join(d, "brain", conv_id)):
      base_dir = d
      break
  return os.path.join(base_dir, "brain", conv_id, "scratch", ".context_guard_state.json")


def load_guard_state(conv_id: str) -> dict:
  from context_guard import load_state

  return load_state(guard_state_path(conv_id))


def record_handoff(conv_id: str, new_cid: str, kind: str = "handoff") -> None:
  """Marks conv_id as handed off to new_cid in its guard state.

  The CLI itself is the durable record of the launch: the hooks only see the
  tail of a long tool output, so the JSON with the id may never reach them
  """
  from context_guard import save_state

  state = load_guard_state(conv_id)
  state["handed_off_to"] = new_cid
  state["handoff_completed"] = True
  state["pending_handoff_launch"] = False
  state["handed_off_at"] = datetime.now(ZoneInfo("UTC")).isoformat()
  # export_chain_index links only "handoff" redirects into the chain
  state["handoff_kind"] = kind
  save_state(guard_state_path(conv_id), state)


# Only JetboxSubscribeToSummaries carries backgroundTaskCount / hasRunningSubagents; the
# GetAllCascadeTrajectories fallback lacks them and never sets hasActivity, so None (state
# unknown) is returned then, and callers must not treat it as "nothing running"
def _fetch_live_summaries() -> dict | None:
  try:
    import project_activity_badge as pab  # pylint: disable=g-import-not-at-top

    summaries = pab.fetch_trajectories() or {}
  except Exception:
    return None
  if not any(isinstance(s, dict) and "hasActivity" in s for s in summaries.values()):
    return None
  return summaries


# Live work that would report back into a chat after it is handed off and hidden.
# The LS counts neither the caller's own turn nor foreground commands, so a chat can
# check itself from inside its turn; notFullyIdle / RUNNING would always be true there
def list_live_dependents(
    conv_ids: list[str], summaries: dict | None = None, include_running: bool = False
) -> dict[str, list[str]] | None:
  if summaries is None:
    summaries = _fetch_live_summaries()
  if summaries is None:
    return None
  live: dict[str, list[str]] = {}
  for cid in conv_ids:
    s = summaries.get(cid) or {}
    reasons = []
    tasks = int(s.get("backgroundTaskCount") or 0)
    if tasks:
      reasons.append(f"{tasks} background task(s)")
    if s.get("hasRunningSubagents"):
      reasons.append("running subagent(s)")
    # Checking another chat (sweep, audit): its own running turn is live work too
    if include_running and s.get("status") == "CASCADE_RUN_STATUS_RUNNING":
      reasons.append("running turn")
    if reasons:
      live[cid] = reasons
  return live


# Must run before anything slow: a CLI call that outlives WaitMsBeforeAsync becomes a
# background task of the caller and would count itself
def _refuse_live_dependents(command: str, conv_id: str) -> None:
  live = list_live_dependents([conv_id])
  if live is None:
    raise RuntimeError(
        f"cannot tell whether {conv_id} still has running subagents or background tasks"
        f" (LS Jetbox summaries unavailable); rerun `{command}` with --ignore-live only after"
        f" checking that nothing is running"
    )
  if live:
    raise RuntimeError(
        f"{conv_id} still has {', '.join(live[conv_id])}; their reports would land in a"
        f" hidden chat. Wait for them (end the turn, they notify), kill them, or rerun"
        f" `{command}` with --ignore-live after recording them in the state file"
    )


def create_handoff(
    current_conv_id: str,
    summary_file: str = "",
    notes: str = "",
    next_step_prompt: str = "",
    new_topic: str = "",
    model: str = "pro",
    exact_model: str = "",
    force: bool = False,
    ignore_live: bool = False,
) -> dict:
  """Executes a clean handoff to a new conversation.

  1. Builds title based on existing topic or new_topic.
  2. Resolves the summary. In a project it is the chain state file (seeded from
     summary_file if only that exists, never from a template; fails before
     launching if neither exists). Outside a project it is the handoff_summary
     artifact, written/enriched from notes if needed.
  3. Launches the continuation directly via Language Server RPC with an exact plan model
     (explicit --model -> inherited from the current chat's last turn -> $HANDOFF_MODEL /
     <app_data_dir>/handoff_model -> agentapi tier `model`), preserving ANTIGRAVITY_PROJECT_ID
     and sending no sourceMetadata (chat stays visible). Falls back to
     `agentapi new-conversation --model=<tier>` if the RPC path is unavailable.
     In a project the state file and owned `doing` items move to the new chat
     before its first message (RPC path) or right after launch (agentapi path).
  4. Advances lifecycle chain markers (advances old chat to ✓ and new to ⦿).
  5. Verifies sourceMetadata: null.

  A chat that already recorded a continuation refuses a second one unless
  force is set, because the old chat may re-run handoff after it lost the
  first result to tool-output truncation.
  """
  if not current_conv_id:
    raise ValueError("current_conv_id must not be empty")

  prior = load_guard_state(current_conv_id).get("handed_off_to", "")
  if prior and not force:
    raise RuntimeError(
        f"{current_conv_id} was already handed off to {prior}; no second continuation was"
        " started. If this turn launched it, end the turn now; pass --force only when the"
        " user explicitly asked for another continuation"
    )
  # The caller is hidden once its continuation exists, so live work would report into nothing
  if not ignore_live:
    _refuse_live_dependents("handoff", current_conv_id)

  # 1. Determine project ID to preserve
  project_id = get_conversation_project_id(current_conv_id)
  in_project = bool(project_id) and project_id != "outside-of-project"

  ignored_summary = ""
  if in_project:
    import roadmap

    # The chain state file is the summary; it is never synthesized from a
    # template, because a stub would hide that the agent recorded nothing
    state_file = roadmap.state_path(project_id, current_conv_id)
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    if summary_file and os.path.abspath(summary_file) != state_file:
      if os.path.isfile(state_file):
        ignored_summary = summary_file
      elif os.path.isfile(summary_file):
        # The guard may have resolved the chat as outside-project from a
        # stale DB row and asked for a handoff_summary; carry it over
        shutil.copyfile(summary_file, state_file)
      else:
        raise RuntimeError(f"summary file {summary_file} does not exist")
    if notes:
      block = f"\n## Handoff notes\n{notes.strip()}\n"
      existing = ""
      if os.path.isfile(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
          existing = f.read()
      # A failed launch is retried with the same arguments
      if block not in existing:
        with open(state_file, "a", encoding="utf-8") as f:
          f.write(block)
    if not os.path.isfile(state_file):
      raise RuntimeError(
          f"chain state file {state_file} does not exist: write it first (goal, current"
          " state with evidence, failed hypotheses, running tasks, exact next step) or pass --notes"
      )
    summary_file = state_file

  # 2. Determine base topic and new continuation title
  if new_topic:
    new_topic = validate_chat_topic(new_topic)
  else:
    cur_title = get_conversation_title(current_conv_id)
    cur_base = clean_base_title(cur_title)
    if is_valid_chat_topic(cur_base):
      new_topic = cur_base
    else:
      derived = derive_fallback_topic(
          current_conv_id,
          state_file=summary_file if in_project else "",
          notes=notes,
          next_step=next_step_prompt,
      )
      new_topic = validate_chat_topic(derived or cur_base)

  time_str = get_current_time_str()
  cont_title = f"[{time_str}] {MARKER_ACTIVE} {new_topic}"

  if in_project:
    prompt = (
        f"Continuing unfinished work from previous conversation (conversation://{current_conv_id}). "
        f"First run `python3 ~/.gemini/config/hooks/roadmap.py show`, then read your chain state file "
        f"{os.path.dirname(state_file)}/<this conversation's id>.md via view_file (`python3 "
        f"~/.gemini/config/hooks/roadmap.py state-path $ANTIGRAVITY_CONVERSATION_ID` prints the path; "
        f"if the file does not exist yet, run `python3 ~/.gemini/config/hooks/roadmap.py state-move "
        f"{current_conv_id} $ANTIGRAVITY_CONVERSATION_ID` first). "
        f"Verify the recorded state before building on it (e.g. reproduce the failing test, check git status), "
        f"then continue from the recorded next step without asking for confirmation (unless the state file "
        f"says it is waiting for user input). Keep the state file and roadmap statuses current as you work."
    )
  else:
    # 3. Resolve summary artifact file. `summary-path <id>` named it after the
    # old chat's own topic, so a new topic must not redirect the lookup
    if not summary_file:
      summary_file = get_handoff_summary_path(current_conv_id)

    # Check if summary file exists; if not or if notes provided, create/update it
    if not os.path.isfile(summary_file) or notes:
      os.makedirs(os.path.dirname(summary_file), exist_ok=True)
      body_notes = f"\n\n## Status and Notes\n{notes.strip()}\n" if notes else ""
      with open(summary_file, "w", encoding="utf-8") as f:
        f.write(
            f"# Handoff Summary: {new_topic}\n\n"
            f"Continuation of conversation://{current_conv_id}.{body_notes}\n"
            f"## Epic Roadmap\n- [ ] {next_step_prompt.strip() if next_step_prompt else 'Continue investigation/tasks from previous conversation.'}\n\n"
            f"## Next Steps\n- {next_step_prompt.strip() if next_step_prompt else 'Continue investigation/tasks from previous conversation.'}\n"
        )

    # 4. Build continuation prompt
    prompt = (
        f"Continuing unfinished work from previous conversation (conversation://{current_conv_id}). "
        f"Read {summary_file} via view_file, review completed steps, remaining Epic Roadmap backlog, and discarded hypotheses, "
        f"and immediately continue executing from the next step recorded in the summary without asking "
        f"for confirmation (unless the summary explicitly states it is waiting for user input)."
    )
  if next_step_prompt:
    prompt = f"{prompt}\n\nNext immediate task: {next_step_prompt}"

  # 5. Launch the continuation: exact plan model via LS RPC, else agentapi tier.
  project_id_final = project_id if project_id and project_id != "outside-of-project" else "outside-of-project"
  model_info: dict = {}
  new_cid = ""
  moved_to = ""

  def move_state(cid: str) -> None:
    nonlocal moved_to
    roadmap.state_move(project_id, moved_to or current_conv_id, cid)
    moved_to = cid

  try:
    model_info = resolve_handoff_model(
        current_conv_id,
        tier=model,
        explicit=exact_model,
        project_id=project_id_final,
    )
    new_cid = start_conversation_exact(
        model_info["enum"], cont_title, prompt, project_id_final,
        before_send=move_state if in_project else None,
    )
  except BeforeSendError as exc:
    try:
      archive_conversations([exc.cascade_id])
    except Exception:
      pass
    raise RuntimeError(f"state file handover failed, no continuation was started: {exc}") from exc
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

  extra: dict = {}
  # Recorded before anything that can still fail: the launch already happened
  # and the hooks must learn it from this file, not from the tool output
  try:
    record_handoff(current_conv_id, new_cid)
  except Exception as exc:
    extra["guard_state_error"] = str(exc)[:300]
  if in_project:
    # agentapi sends the first message itself, so here the move can only
    # follow; the prompt tells the continuation to run it if it gets there first
    if moved_to != new_cid:
      try:
        move_state(new_cid)
      except Exception as exc:
        extra["state_move_error"] = (
            f"{exc}; the continuation is told to run `roadmap.py state-move` itself"
        )
    summary_file = roadmap.state_path(project_id, new_cid)
    if ignored_summary:
      extra["ignored_summary_file"] = ignored_summary

  # 6. Verify sourceMetadata (lives under metadata.sourceMetadata; null => visible in the sidebar)
  fetched, source_meta = get_conversation_source_metadata(new_cid)

  # 7. Once the continuation is verified visible, the handed-off chat leaves the sidebar. It
  # stays reachable via conversation:// and chain search; the live-work guard above ensures
  # nothing reports into it later. An unverified continuation keeps the caller visible, or
  # both chats could vanish. The chain index is written first: the archive reaches the browser
  # via LS and triggers the auto-switch, which reads the redirect from jetski-chains.json
  export_chain_index()
  update_conversation_title(new_cid, cont_title)
  if fetched and source_meta is None:
    archive_conversations([current_conv_id])

  return {
      "new_conversation_id": new_cid,
      "title": cont_title,
      "summary_file": summary_file,
      "model": model_info,
      "source_metadata": source_meta,
      "verified_visible": bool(fetched and source_meta is None),
      **extra,
  }


ROADMAP_ITEM_ID_RE = re.compile(r"^R[1-9]\d*(?:\.[1-9]\d*)*$", re.IGNORECASE)


# Finalizes open sibling chats in project_id that completed roadmap items and own no open items
def sweep_completed_project_chats(
    project_id: str,
    exclude_cids: set[str] | None = None,
    finalized_cids: set[str] | None = None,
) -> list[list[str]]:
  if not project_id or project_id == "outside-of-project":
    return []
  try:
    import roadmap

    doc = roadmap.load(project_id)
  except Exception:
    return []

  busy_cids: set[str] = set()
  # Protect the active baton holder unless the caller is explicitly finalizing it
  baton = doc.get("baton")
  baton_holder = (baton.get("conv") or "").strip() if isinstance(baton, dict) else ""
  if baton_holder and baton_holder not in (finalized_cids or ()):
    busy_cids.add(baton_holder)

  closed_candidates: list[str] = []
  for it in doc.get("items", []):
    st = it.get("status", "")
    if st in ("todo", "doing"):
      for field in ("owner", "link"):
        val = (it.get(field) or "").strip()
        if val:
          busy_cids.add(val)
    elif st in ("done", "dropped"):
      for field in ("link", "updated_by"):
        val = (it.get(field) or "").strip()
        if val and val not in closed_candidates:
          closed_candidates.append(val)

  visited: set[str] = set(exclude_cids or ()) | set(finalized_cids or ())
  actually_finalized: set[str] = set(finalized_cids or ())
  swept_chains: list[list[str]] = []
  live_summaries: dict | None = None
  live_fetched = False
  for cid in closed_candidates:
    if cid in visited:
      continue
    chain = discover_conversation_chain(cid) or [cid]
    if any(c in visited for c in chain):
      visited.update(chain)
      continue
    visited.update(chain)
    # Never sweep a chain that still owns or is linked to an open todo/doing item
    if any(c in busy_cids for c in chain):
      continue
    leaf_id = chain[-1]
    leaf_pid = get_conversation_project_id(leaf_id)
    if leaf_pid and leaf_pid != project_id:
      continue
    leaf_title = get_conversation_title(leaf_id)
    if not leaf_title or is_archived(leaf_id) or is_waiting_title(leaf_title):
      continue
    # A finalized chat drops out of sight, so live work must not report into it; with live
    # state unknown nothing is finalized
    if not live_fetched:
      live_summaries, live_fetched = _fetch_live_summaries(), True
    if live_summaries is None or list_live_dependents(
        [leaf_id], live_summaries, include_running=True
    ):
      continue
    finalized = archive_chain(leaf_id)
    swept_chains.append(finalized)
    actually_finalized.update(finalized)
    for c in chain:
      try:
        sf = roadmap.state_path(project_id, c)
        if os.path.isfile(sf):
          os.unlink(sf)
      except Exception:
        pass

  if actually_finalized:
    try:
      roadmap.clear_baton(project_id, actually_finalized)
    except Exception:
      pass
  return swept_chains


# Finalizes current_conv_id's chain and opens a fresh root chat for the next roadmap item
def create_next_task(
    current_conv_id: str,
    item_id: str = "",
    new_topic: str = "",
    notes: str = "",
    next_step_prompt: str = "",
    model: str = "pro",
    exact_model: str = "",
    force: bool = False,
    ignore_live: bool = False,
) -> dict:
  if not current_conv_id:
    raise ValueError("current_conv_id must not be empty")

  prior = load_guard_state(current_conv_id).get("handed_off_to", "")
  if prior and not force:
    raise RuntimeError(
        f"{current_conv_id} was already handed off to {prior}; no second chat was"
        " started. If this turn launched it, end the turn now; pass --force only when"
        " the user explicitly asked for another chat"
    )
  # next-task finalizes the caller, which hides it the same way a handoff does
  if not ignore_live:
    _refuse_live_dependents("next-task", current_conv_id)

  project_id = get_conversation_project_id(current_conv_id)
  if not project_id or project_id == "outside-of-project":
    raise RuntimeError(
        f"next-task requires an in-project conversation with a roadmap ({current_conv_id})"
    )

  import roadmap

  doc = roadmap.load(project_id)
  norm_id = item_id.strip().upper() if item_id else ""

  # Refuse to abandon an in-flight doing item owned by current_conv_id without closing it first
  owned_doing = [
      it["id"]
      for it in doc.get("items", [])
      if it.get("status") == "doing"
      and it.get("owner") == current_conv_id
      and it.get("id") != norm_id
  ]
  if owned_doing and not force:
    raise RuntimeError(
        f"conversation {current_conv_id[:8]} still owns 'doing' item(s)"
        f" {', '.join(owned_doing)}: mark them done/todo via `roadmap.py set`"
        " before starting the next task"
    )

  # Auto-select the first top-level todo item (or any todo item) when item_id is omitted
  if not norm_id:
    top_todos = [
        it
        for it in doc.get("items", [])
        if not it.get("parent") and it.get("status") == "todo"
    ]
    any_todos = [it for it in doc.get("items", []) if it.get("status") == "todo"]
    chosen = (top_todos or any_todos or [None])[0]
    if not chosen:
      raise RuntimeError("no 'todo' items left in the project roadmap")
    norm_id = chosen["id"]
    target_item = chosen
  else:
    target_item = roadmap._find(doc, norm_id)

  st = target_item.get("status", "")
  if st in ("done", "dropped"):
    raise RuntimeError(f"roadmap item {norm_id} is already {st}")
  owner = target_item.get("owner") or ""
  if st == "doing" and owner and owner != current_conv_id and not force:
    raise RuntimeError(
        f"roadmap item {norm_id} is already 'doing' in conversation {owner[:8]}"
    )

  item_text = target_item["text"]
  if new_topic:
    clean_topic = validate_chat_topic(new_topic)
  else:
    entity = extract_entity_topic(item_text)
    if entity:
      clean_topic = f"{norm_id} {entity}"
    else:
      cand_topic = _clean_candidate_topic(item_text, 34)
      clean_topic = f"{norm_id} {cand_topic}"[:38].rstrip()
  if not is_valid_chat_topic(clean_topic):
    clean_topic = f"{norm_id} Task"
  time_str = get_current_time_str()
  cont_title = f"[{time_str}] {MARKER_ACTIVE} {clean_topic}"

  old_state_file = roadmap.state_path(project_id, current_conv_id)
  state_dir = os.path.dirname(old_state_file)
  # Use 'split out of conversation://...' so HANDOFF_PARENT_RE keeps the new chat as a root
  prompt = (
      f"Starting roadmap task {norm_id} ({item_text}), split out of"
      f" conversation://{current_conv_id}. First run `python3"
      " ~/.gemini/config/hooks/roadmap.py show`, then read your chain state file"
      f" {state_dir}/<this conversation's id>.md via view_file (`python3"
      " ~/.gemini/config/hooks/roadmap.py state-path $ANTIGRAVITY_CONVERSATION_ID`"
      f" prints the path). Begin working on {norm_id} immediately without asking"
      " for confirmation. Keep the state file and roadmap statuses current as you"
      " work."
  )
  if next_step_prompt:
    prompt = f"{prompt}\n\nNext immediate task: {next_step_prompt}"

  seeded_for = ""

  def seed_task_state(cid: str) -> None:
    nonlocal seeded_for
    roadmap.set_status(project_id, cid, norm_id, "doing")
    roadmap.link_item(project_id, cid, norm_id, cid)
    new_sf = roadmap.state_path(project_id, cid)
    os.makedirs(os.path.dirname(new_sf), exist_ok=True)
    notes_block = f"\n## Context Notes\n{notes.strip()}\n" if notes.strip() else ""
    next_line = (
        next_step_prompt.strip()
        if next_step_prompt.strip()
        else f"Investigate and implement {norm_id}: {item_text}"
    )
    with open(new_sf, "w", encoding="utf-8") as f:
      f.write(
          f"# {norm_id}: {item_text}\n\n"
          f"Split out of conversation://{current_conv_id}.\n\n"
          f"## Goal\n- {norm_id}: {item_text}\n"
          f"{notes_block}\n"
          f"## Immediate Next Step\n- {next_line}\n"
      )
    if os.path.isfile(old_state_file) and os.path.abspath(old_state_file) != os.path.abspath(
        new_sf
    ):
      try:
        os.unlink(old_state_file)
      except OSError:
        pass
    seeded_for = cid

  model_info: dict = {}
  new_cid = ""
  try:
    model_info = resolve_handoff_model(
        current_conv_id,
        tier=model,
        explicit=exact_model,
        project_id=project_id,
    )
    new_cid = start_conversation_exact(
        model_info["enum"],
        cont_title,
        prompt,
        project_id,
        before_send=seed_task_state,
    )
  except BeforeSendError as exc:
    try:
      archive_conversations([exc.cascade_id])
    except Exception:
      pass
    raise RuntimeError(
        f"next-task state setup failed, no conversation was started: {exc}"
    ) from exc
  except Exception as exc:
    model_info = {
        "source": f"agentapi tier:{model}",
        "fallback_reason": str(exc)[:300],
    }
    env = {
        k: v
        for k, v in os.environ.items()
        if k != "ANTIGRAVITY_SOURCE_METADATA"
    }
    env["ANTIGRAVITY_PROJECT_ID"] = project_id
    cmd = [
        "agentapi",
        "new-conversation",
        f"--model={model}",
        f"--title={cont_title}",
        prompt,
    ]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if res.returncode != 0:
      raise RuntimeError(
          f"agentapi new-conversation failed (code {res.returncode}): {res.stderr or res.stdout}"
      )
    m = re.search(r'"conversationId":\s*"([^"]+)"', res.stdout)
    if not m:
      raise RuntimeError(
          f"Failed to parse conversationId from agentapi output: {res.stdout}"
      )
    new_cid = m.group(1)

  extra: dict = {}
  try:
    record_handoff(current_conv_id, new_cid, kind="next-task")
  except Exception as exc:
    extra["guard_state_error"] = str(exc)[:300]

  if seeded_for != new_cid:
    try:
      seed_task_state(new_cid)
    except Exception as exc:
      extra["state_seed_error"] = str(exc)[:300]

  new_state_file = roadmap.state_path(project_id, new_cid)

  fetched, source_meta = get_conversation_source_metadata(new_cid)

  # Archive the finished task's chain (only once new_cid is verified visible, same as
  # handoff), sweep completed sibling chats, and record new_cid as an active root chat.
  # The chain index goes first so the browser auto-switch finds the redirect on archive
  export_chain_index()
  finalized_chain = archive_chain(current_conv_id) if fetched and source_meta is None else []
  swept_sibling_chains = sweep_completed_project_chats(
      project_id,
      exclude_cids={new_cid},
      finalized_cids=set(finalized_chain),
  )
  update_conversation_title(new_cid, cont_title)

  return {
      "new_conversation_id": new_cid,
      "item_id": norm_id,
      "title": cont_title,
      "summary_file": new_state_file,
      "finalized_chain": finalized_chain,
      "swept_sibling_chains": swept_sibling_chains,
      "model": model_info,
      "source_metadata": source_meta,
      "verified_visible": bool(fetched and source_meta is None),
      **extra,
  }


# The roadmap lives in the sidebar badge and `roadmap.py show`; echoing it here only made
# the old chat paste it into its final message
def print_handoff_result(result: dict) -> None:
  out = dict(result)
  out.pop("roadmap", None)
  print(json.dumps(out, ensure_ascii=False, indent=2))


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
        "is_archived": is_archived(cid),
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
      "is_archived": is_archived(conv_id),
      "chain_length": len(chain),
      "chain": items,
      "summary_files": summaries,
  }


def archive_conversations(
    conv_ids: list[str], include_chain: bool = False, archived: bool = True
) -> dict:
  """Sets the archived flag via Language Server RPC and in annotations/<id>.pbtxt."""
  target_ids: list[str] = []
  seen: set[str] = set()
  for cid in conv_ids:
    if not cid:
      continue
    expanded = (
        discover_conversation_chain(cid, include_descendants=True)
        if include_chain
        else [cid]
    )
    for item in expanded:
      if item and item not in seen:
        seen.add(item)
        target_ids.append(item)

  if not target_ids:
    return {"archived_ids": [], "count": 0, "rpc_ok": False, "disk_ok": False}

  rpc_ok = False
  try:
    ls_rpc(
        "UpdateConversationAnnotations",
        {
            "cascadeIds": target_ids,
            "annotations": {"archived": archived},
            "mergeAnnotations": True,
        },
    )
    rpc_ok = True
  except Exception:
    pass

  disk_ok = False
  flag = f"archived:{'true' if archived else 'false'}"
  for app_dir in find_app_data_dirs():
    ann_dir = os.path.join(app_dir, ANNOTATIONS_DIR_NAME)
    try:
      os.makedirs(ann_dir, exist_ok=True)
    except OSError:
      continue
    for cid in target_ids:
      ann_file = os.path.join(ann_dir, f"{cid}.pbtxt")
      try:
        if os.path.isfile(ann_file):
          with open(ann_file, "r", encoding="utf-8") as f:
            content = f.read()
          if re.search(r"\barchived\s*:\s*(?:true|false)", content):
            new_content = re.sub(r"\barchived\s*:\s*(?:true|false)", flag, content)
          else:
            new_content = f"{content.rstrip()}  {flag}\n"
        else:
          new_content = f"{flag}\n"
        with open(ann_file, "w", encoding="utf-8") as f:
          f.write(new_content)
        disk_ok = True
      except OSError:
        pass

  return {
      "archived_ids": target_ids,
      "count": len(target_ids),
      "rpc_ok": rpc_ok,
      "disk_ok": disk_ok,
  }


def load_retention_rules() -> dict[str, int]:
  """Returns a mapping of project identifier to keep count."""
  rules = dict(DEFAULT_AUTO_ARCHIVE_RETENTION)
  if os.path.isfile(DEFAULT_PROJECT_RETENTION_FILE):
    try:
      with open(DEFAULT_PROJECT_RETENTION_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
      if isinstance(data, dict):
        for k, v in data.items():
          if isinstance(v, int):
            rules[k] = v
    except Exception:
      pass
  return rules


def archive_project_conversations(
    project_filter: str,
    keep: int = 1,
    projects_dir: str = "",
) -> dict:
  """Archives older active conversations in a project, keeping the `keep` newest."""
  target_project_id = resolve_project_id(
      project_filter, projects_dir=projects_dir
  )
  summaries = _fetch_ls_summaries_for_audit()
  if summaries is None:
    return {
        "project_id": target_project_id,
        "archived_ids": [],
        "count": 0,
        "kept_ids": [],
        "error": "LS unreachable",
    }

  archived_on_disk: set[str] = set()
  for app_dir in find_app_data_dirs():
    ann_dir = os.path.join(app_dir, ANNOTATIONS_DIR_NAME)
    if os.path.isdir(ann_dir):
      for fn in sorted(os.listdir(ann_dir)):
        if fn.endswith(".pbtxt"):
          cid = fn[:-6]
          try:
            with open(os.path.join(ann_dir, fn), "r", encoding="utf-8") as f:
              txt = f.read()
            if re.search(r"\barchived\s*:\s*true\b", txt):
              archived_on_disk.add(cid)
          except OSError:
            pass

  # Collect active top-level conversations belonging to this project
  active_chats: list[tuple[str, str]] = []
  for cid, s in summaries.items():
    if not isinstance(s, dict):
      continue
    ann = s.get("annotations") or {}
    if ann.get("archived") or cid in archived_on_disk:
      continue
    meta = s.get("trajectoryMetadata") or {}
    if meta.get("parentConversationId") or meta.get("isBattleModeFork"):
      continue
    if (meta.get("sourceMetadata") or {}).get("tool"):
      continue
    pid = str(meta.get("projectId") or "").strip()
    if pid == target_project_id:
      created_at = str(meta.get("createdAt") or "")
      active_chats.append((created_at, cid))

  active_chats.sort(key=lambda x: x[0])
  if keep > 0:
    to_keep = [cid for _, cid in active_chats[-keep:]]
    to_archive = [cid for _, cid in active_chats[:-keep]]
  else:
    to_keep = []
    to_archive = [cid for _, cid in active_chats]

  if not to_archive:
    return {
        "project_id": target_project_id,
        "archived_ids": [],
        "count": 0,
        "kept_ids": to_keep,
        "rpc_ok": True,
        "disk_ok": True,
    }

  res = archive_conversations(to_archive, include_chain=False)
  res["project_id"] = target_project_id
  res["kept_ids"] = to_keep
  return res


def auto_archive_projects(target_cid: str = "") -> dict:
  """Archives older conversations for projects configured with a retention policy."""
  rules = load_retention_rules()
  results: dict[str, dict] = {}
  target_project_id = get_conversation_project_id(target_cid) if target_cid else ""

  # Map project names/UUIDs to canonical project IDs
  resolved_rules: dict[str, tuple[str, int]] = {}
  for proj_key, keep in rules.items():
    try:
      pid = resolve_project_id(proj_key)
      resolved_rules[pid] = (proj_key, keep)
    except Exception:
      pass

  # If triggered by a specific chat whose project is unassigned or not under retention, exit early
  if target_cid and (not target_project_id or target_project_id not in resolved_rules):
    return results

  for pid, (proj_key, keep) in resolved_rules.items():
    if target_project_id and pid != target_project_id:
      continue
    try:
      results[proj_key] = archive_project_conversations(pid, keep=keep)
    except Exception as e:
      results[proj_key] = {"error": str(e)}

  return results


def _fetch_ls_summaries_for_audit() -> dict | None:
  """Fetches trajectory summaries from the Language Server if reachable."""
  try:
    import project_activity_badge as pab  # pylint: disable=g-import-not-at-top
    res = pab.fetch_trajectories()
    if res is not None:
      return res
  except Exception:
    pass
  try:
    resp = ls_rpc("GetAllCascadeTrajectories", {"excludeSubtrajectories": True})
    if isinstance(resp, dict) and "trajectorySummaries" in resp:
      return resp.get("trajectorySummaries") or {}
  except Exception:
    pass
  return None


def _iso_to_hhmm(iso_str: str) -> str:
  if not iso_str:
    return ""
  try:
    clean = iso_str.strip()
    if clean.endswith("Z"):
      clean = clean[:-1] + "+00:00"
    # Trim nanoseconds to microseconds if present
    clean = re.sub(r"\.(\d{6})\d+", r".\1", clean)
    dt = datetime.fromisoformat(clean)
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    return dt.astimezone(tz).strftime("%H:%M")
  except Exception:
    return ""


# Formats an ISO timestamp as 'Oct 27 14:05 ET' in the user's zone
def _iso_to_et_stamp(iso_str: str) -> str:
  try:
    dt = datetime.fromisoformat(re.sub(r"(\.\d{6})\d*", r"\1", iso_str).replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(DEFAULT_TIMEZONE)).strftime("%b %d %H:%M ET")
  except Exception:
    return ""


def audit_conversations(
    fix: bool = False,
    project_filter: str = "",
    projects_dir: str = "",
) -> dict:
  """Scans top-level conversations and chains for marker/format errors and storage desync.

  Checks synchronization across:
  1. Language Server RPC (live in-memory store / sidebar stream)
  2. ~/.gemini/{jetski,antigravity}/annotations/<id>.pbtxt
  3. conversation_summaries.db
  And verifies Scheme Γ lifecycle markers across all visible handoff chains.
  """
  target_project_id = ""
  if project_filter:
    target_project_id = resolve_project_id(
        project_filter, projects_dir=projects_dir
    )

  ls_summaries = _fetch_ls_summaries_for_audit()
  ls_online = ls_summaries is not None

  pbtxt_titles: dict[str, str] = {}
  archived_on_disk: set[str] = set()
  db_titles: dict[str, str] = {}
  db_projects: dict[str, str] = {}

  for app_dir in find_app_data_dirs():
    ann_dir = os.path.join(app_dir, ANNOTATIONS_DIR_NAME)
    if os.path.isdir(ann_dir):
      for fn in sorted(os.listdir(ann_dir)):
        if not fn.endswith(".pbtxt"):
          continue
        cid = fn[:-6]
        try:
          with open(os.path.join(ann_dir, fn), "r", encoding="utf-8") as f:
            txt = f.read()
          if re.search(r"\barchived\s*:\s*true\b", txt):
            archived_on_disk.add(cid)
          m = _PBTXT_TITLE_RE.search(txt)
          if m and cid not in pbtxt_titles:
            pbtxt_titles[cid] = _unescape_pbtxt_str(m.group(1)).strip()
        except OSError:
          pass

    db_file = os.path.join(app_dir, SUMMARY_DB_NAME)
    if os.path.isfile(db_file):
      try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=2.0)
        cur = conn.cursor()
        try:
          cur.execute(
              "SELECT conversation_id, title, project_id FROM conversation_summaries"
          )
          for cid, title, pid in cur.fetchall():
            if cid not in db_titles and title:
              db_titles[cid] = str(title).strip()
            if cid not in db_projects and pid:
              db_projects[cid] = str(pid).strip()
        except sqlite3.OperationalError:
          cur.execute("SELECT conversation_id, title FROM conversation_summaries")
          for cid, title in cur.fetchall():
            if cid not in db_titles and title:
              db_titles[cid] = str(title).strip()
        conn.close()
      except Exception:
        pass

  raw_parent, _, created_at_of = _scan_transcript_graph()

  # Determine visible top-level conversations
  visible_ids: set[str] = set()
  ls_titles: dict[str, str] = {}
  ls_projects: dict[str, str] = {}
  if ls_online and ls_summaries is not None:
    for cid, s in ls_summaries.items():
      if not isinstance(s, dict):
        continue
      ann = s.get("annotations") or {}
      if ann.get("archived") or cid in archived_on_disk:
        continue
      meta = s.get("trajectoryMetadata") or {}
      if meta.get("parentConversationId") or meta.get("isBattleModeFork"):
        continue
      if (meta.get("sourceMetadata") or {}).get("tool"):
        continue
      visible_ids.add(cid)
      ls_t = str(ann.get("title") or s.get("summary") or "").strip()
      ls_titles[cid] = ls_t
      pid = str(meta.get("projectId") or "").strip()
      if pid:
        ls_projects[cid] = pid
      cat = str(meta.get("createdAt") or "").strip()
      if cat:
        created_at_of[cid] = cat
  else:
    for cid in set(pbtxt_titles.keys()) | set(db_titles.keys()):
      if cid not in archived_on_disk:
        visible_ids.add(cid)

  def _BestTitle(cid: str) -> str:
    return (
        pbtxt_titles.get(cid)
        or db_titles.get(cid)
        or ls_titles.get(cid)
        or get_conversation_title(cid)
        or ""
    )

  def _ProjectOf(cid: str) -> str:
    pid = ls_projects.get(cid) or db_projects.get(cid) or "outside-of-project"
    return pid if pid else "outside-of-project"

  vis_parent, vis_children = link_visible_chains(visible_ids, raw_parent)

  roots = sorted(c for c in visible_ids if c not in vis_parent)
  discrepancies: list[dict] = []
  fixed_count = 0
  audited_chains = 0
  audited_chats = 0

  # A visible chat reads "[HH:MM] ⦿|⏳ <topic>" with exactly one marker
  valid_title_re = re.compile(
      r"^\[\d{2}:\d{2}\] (?:⦿|⏳) (?!(?:[▸✓⦿⏳«»]|‹✓›|«»)(?:\s|$))\S"
  )

  live_summaries: dict | None = None
  live_fetched = False
  for root_id in roots:
    chain = _order_chain_from_root(root_id, vis_children, created_at_of)
    if target_project_id:
      if not any(_ProjectOf(c) == target_project_id for c in chain):
        continue

    audited_chains += 1
    audited_chats += len(chain)

    # Determine chain base topic (for fallback if a node has empty title)
    chain_topic = ""
    for cid in reversed(chain):
      t = clean_base_title(_BestTitle(cid))
      if is_valid_chat_topic(t):
        chain_topic = t
        break
    if not chain_topic and chain:
      chain_topic = derive_fallback_topic(chain[-1])

    for idx, cid in enumerate(chain):
      cur_title = _BestTitle(cid)
      marker = parse_title_marker(cur_title)
      # Handed-off pieces and chats closed under the pre-archive «…» scheme belong in the
      # archive, not in the sidebar
      # A piece is handed off only when a visible chat continues it: in a forked tree the
      # older branch's leaf is still an open chat
      if cid in vis_children or marker in LEGACY_CLOSED_MARKERS:
        is_fixed = False
        # Same rule as handoff: live work must not report into a chat that gets hidden,
        # and unknown live state archives nothing
        if fix and not live_fetched:
          live_summaries, live_fetched = _fetch_live_summaries(), True
        if fix and live_summaries is not None and not list_live_dependents(
            [cid], live_summaries, include_running=True
        ):
          res = archive_conversations([cid])
          is_fixed = bool(res["rpc_ok"] or res["disk_ok"])
          fixed_count += int(is_fixed)
        discrepancies.append({
            "conversation_id": cid,
            "project_id": _ProjectOf(cid),
            "chain_position": f"{idx + 1}/{len(chain)}",
            "issues": ["should_be_archived"],
            "title": cur_title,
            "fixed": is_fixed,
        })
        continue

      open_leaf_marker = MARKER_WAITING if marker == MARKER_WAITING else MARKER_ACTIVE
      fallback_hhmm = _iso_to_hhmm(created_at_of.get(cid, "")) or get_current_time_str()
      ts = extract_time_prefix(cur_title, fallback_hhmm)
      raw_base = clean_base_title(cur_title)
      base = (
          raw_base
          if is_valid_chat_topic(raw_base)
          else (chain_topic or derive_fallback_topic(cid) or raw_base or f"Chat {cid[:8]}")
      )
      expected_title = f"[{ts}] {open_leaf_marker} {base}"

      pb_t = pbtxt_titles.get(cid, "")
      db_t = db_titles.get(cid, "")
      ls_t = ls_titles.get(cid, "") if ls_online else ""

      issues: list[str] = []
      if cur_title != expected_title or not valid_title_re.match(cur_title):
        issues.append("marker_mismatch")

      if ls_online and ls_t != expected_title and not valid_title_re.match(ls_t):
        if "marker_mismatch" not in issues:
          issues.append("ls_marker_mismatch")

      if not is_valid_chat_topic(raw_base):
        issues.append("generic_or_single_word_title")

      present_stores = [t for t in (pb_t, db_t) if t]
      if ls_online:
        present_stores.append(ls_t)
      if (
          len(set(present_stores)) > 1
          or (pb_t and pb_t != expected_title)
          or (db_t and db_t != expected_title)
          or (ls_online and ls_t != expected_title)
      ):
        if (pb_t != db_t) or (ls_online and ls_t != pb_t):
          issues.append("storage_desync")
        elif not issues:
          issues.append("storage_desync")

      if issues:
        is_fixed = False
        if fix:
          is_fixed = update_conversation_title(cid, expected_title)
          if is_fixed:
            fixed_count += 1
        discrepancies.append({
            "conversation_id": cid,
            "project_id": _ProjectOf(cid),
            "chain_position": f"{idx + 1}/{len(chain)}",
            "issues": issues,
            "ls_title": ls_t if ls_online else None,
            "pbtxt_title": pb_t or None,
            "db_title": db_t or None,
            "expected_title": expected_title,
            "fixed": is_fixed,
        })

  export_chain_index()
  return {
      "ls_online": ls_online,
      "project_filter": target_project_id or None,
      "total_visible_chats": audited_chats,
      "total_chains": audited_chains,
      "issues_count": len(discrepancies),
      "fixed_count": fixed_count,
      "discrepancies": discrepancies,
  }


USAGE = """\
chat_lifecycle.py — deterministic chat lifecycle CLI (titles, handoffs, chains).

Usage:
  python3 ~/.gemini/config/hooks/chat_lifecycle.py <command> [args]

Commands:
  status [id]                         Show the chain for <id>: titles, markers,
                                      is_archived, summary file paths. Read-only.
  set-title <id> "<Topic>"            Rename the chat. Time prefix [HH:MM] and the
                                      marker are added automatically.
  auto-title <id> "<prompt>"          Detect PR/CL/bug/commit in prompt and set title
                                      if the chat is currently untitled.
  summary-path <id>                   Print the canonical handoff summary path
                                      (brain/<id>/handoff_summary_<slug>_<id8>.md).
  handoff <id> [summary_file]         Create a visible continuation chat in the same
          [--notes "..."]             project, verify sourceMetadata is null and
          [--next "..."]              archive <id>. In a project the summary
          [--topic "<Topic>"]         is the chain state file (roadmap.py state-path);
          [--model <m>] [--force]     it moves to the new chat and the roadmap is
                                      printed before the JSON. Outside projects it
                                      writes/updates the summary if --notes is given.
                                      --topic titles the continuation with the next
                                      item's topic; without it the topic is inherited.
                                      The continuation keeps the model of <id>'s last
                                      turn (see Notes); --model pins one explicitly.
                                      A chat that already handed off refuses a second
                                      continuation unless --force is passed. It also
                                      refuses while <id> has running subagents or
                                      background tasks (incl. timers): their reports
                                      would land in a hidden chat. --ignore-live skips it.
  next-task [<id>] [<R_ID>]           Archive <id>'s chain and launch a
          [--topic "<Topic>"]         fresh root chat (⦿) in the same project for roadmap
          [--notes "..."]             item <R_ID> (or the first todo item if omitted).
          [--next "..."]              Claims <R_ID> as doing, links <R_ID> -> <new_id>,
          [--model <m>] [--force]     seeds state/<new_id>.md, and removes state/<id>.md.
          [--ignore-live]             Same live subagent/task guard as handoff.
  start [--project <p>] [--title <Topic>] [--model <m>] "<prompt>"
                                      Launch an independent root chat (⦿) pinned to
                                      a project atomically via before_send hook.
  search "<query>"                    Search all chats, archived handoff pieces included
                                      (LS SearchConversations: one best hit per chat, at
                                      most 100 chats, symbols like ✓ never match). Each
                                      hit shows its piece number in its chain.
  search "<query>" --chain <id>       Every user message and agent reply containing the
                                      query across all pieces of <id>'s chain, newest
                                      first (thinking and tool output are skipped).
  model [id] [--trailer]              Read-only: show the model <id> last used and the
                                      model a handoff would pick now (or output
                                      git `Assisted-by: <Model>` trailer with --trailer).
  project-model [<p>] [<m>]           View or configure preferred model for project <p>
                                      (e.g. `skia-g3-roll-check` -> `flash`). With no
                                      arguments, lists all configured project models.
  audit [--fix] [--project <p>]       Scan visible top-level chats and handoff chains
                                      for storage desync (LS RPC vs .pbtxt vs SQLite)
                                      and broken/double markers; visible handed-off
                                      pieces and old «…»-closed chats are reported as
                                      should_be_archived. --fix retitles / archives.
  archive [<id...>] [--chain]         Archive the specified conversation(s) (or their
          [--project <p>] [--keep <N>] entire handoff chain with --chain) via LS RPC
                                      and annotations/<id>.pbtxt. With --project,
                                      archives older active chats in project <p>,
                                      keeping the newest <N> (default: 1).
  auto-archive [<id>] [--hook]        Prune older chats across projects configured with
                                      a retention policy (default: keep 1 newest for
                                      skia-g3-roll-check). Use --hook for lifecycle hooks.
  finalize <id>                       Archive the whole chain. USER-ONLY: run it
                                      only on the user's explicit word ("финал").
  reopen <id>                         Unarchive <id> as an open ⦿ chat. USER-ONLY.
  wait [<id>] [on|off|toggle]         Set or toggle an open chat's idle attention marker
                                      between ⏳ (Waiting Cron/External) and ⦿ (Needs Me).

Internal (used by hooks; do not run manually):
  set <id> "<raw title>"              Write a title verbatim, no marker handling.

Notes:
  * <id> may be any conversation of the chain for finalize/reopen/status/archive --chain.
  * When <id> is omitted, $CONVERSATION_ID is used (set inside the agent's
    run_command only).
  * Only visible chats carry a marker: ⦿ (needs the user) or ⏳ (waiting). Handed-off
    pieces and finished chats are archived. Never choose or edit markers by hand. A
    chain is the set of chats joined by handoff links (who continues whom); topics may
    differ from step to step.
  * Model of a continuation: --model <id|display name|enum|flash_lite|flash|pro>
    -> project default (projects_models.json / DEFAULT_PROJECT_MODELS / project.json)
    -> model of <id>'s last turn (inherit) -> $HANDOFF_MODEL or the first
    non-comment line of <app_data_dir>/handoff_model -> agentapi tier "pro".
    Candidates are validated against GetAvailableModels. `agentapi
    new-conversation` itself knows only Gemini tiers (pro = "Gemini 3.1 Pro
    (Low)" in Antigravity), which is why the launch goes through the LS RPC.
"""


def print_usage(stream=None) -> None:
  print(USAGE, file=stream or sys.stdout, end="")


KNOWN_COMMANDS = (
    "set",
    "set-title",
    "auto-title",
    "summary-path",
    "status",
    "model",
    "project-model",
    "handoff",
    "next-task",
    "start",
    "reopen",
    "wait",
    "finalize",
    "audit",
    "archive",
    "auto-archive",
    "search",
)


def _cli_fail(command: str, message: str) -> None:
  print(f"chat_lifecycle.py {command}: {message}", file=sys.stderr)
  sys.exit(2)


def _require_conversation_id(command: str, cid: str) -> str:
  """Rejects a missing, flag-like or non-UUID conversation id before any
  roadmap/LS access, so a stray option is never treated as a chat id."""
  if not cid:
    _cli_fail(command, "conversation id is required (or set $CONVERSATION_ID)")
  if cid.startswith("-"):
    _cli_fail(command, f"unknown option '{cid}'")
  if not UUID_RE.match(cid):
    _cli_fail(command, f"invalid conversation id '{cid}' (expected a UUID)")
  return cid


def _env_conversation_id() -> str:
  return os.environ.get("CONVERSATION_ID", "") or os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "")


def parse_handoff_args(args: list[str]) -> dict:
  """Parses `handoff [cid] [summary_file] [--notes ..] [--next ..] [--topic ..] [--model ..]
  [--force] [--ignore-live]`.
  """
  opts = {
      "cid": "", "summary": "", "notes": "", "next_step": "", "new_topic": "", "exact_model": "",
      "force": False, "ignore_live": False,
  }
  valued = {"--notes": "notes", "--next": "next_step", "--topic": "new_topic", "--model": "exact_model"}
  i = 0
  while i < len(args):
    a = args[i]
    key, eq, inline = a.partition("=")
    if a == "--force":
      opts["force"] = True
      i += 1
    elif a == "--ignore-live":
      opts["ignore_live"] = True
      i += 1
    elif eq and key in valued:
      opts[valued[key]] = inline
      i += 1
    elif a in valued and i + 1 < len(args):
      opts[valued[a]] = args[i + 1]
      i += 2
    elif a in valued:
      _cli_fail("handoff", f"option '{a}' requires a value")
    elif a.startswith("-"):
      _cli_fail("handoff", f"unknown option '{a}'")
    elif not opts["cid"]:
      opts["cid"] = a
      i += 1
    elif not opts["summary"]:
      opts["summary"] = a
      i += 1
    else:
      _cli_fail("handoff", f"unexpected argument '{a}'")
  return opts


# Disambiguates optional positional UUID conv_id and R<N> roadmap item_id in either order
def parse_next_task_args(args: list[str]) -> dict:
  """Parses `next-task [cid] [item_id] [--topic ..] [--notes ..] [--next ..] [--model ..]
  [--force] [--ignore-live]`.
  """
  opts = {
      "cid": "",
      "item_id": "",
      "notes": "",
      "next_step": "",
      "new_topic": "",
      "exact_model": "",
      "force": False,
      "ignore_live": False,
  }
  valued = {
      "--notes": "notes",
      "--next": "next_step",
      "--topic": "new_topic",
      "--model": "exact_model",
  }
  i = 0
  # Accept --flag=val, --flag val, R<N> item IDs, and UUID conversation IDs in any order
  while i < len(args):
    a = args[i]
    key, eq, inline = a.partition("=")
    if a == "--force":
      opts["force"] = True
      i += 1
    elif a == "--ignore-live":
      opts["ignore_live"] = True
      i += 1
    elif eq and key in valued:
      opts[valued[key]] = inline
      i += 1
    elif a in valued and i + 1 < len(args):
      opts[valued[a]] = args[i + 1]
      i += 2
    elif a in valued:
      _cli_fail("next-task", f"option '{a}' requires a value")
    elif a.startswith("-"):
      _cli_fail("next-task", f"unknown option '{a}'")
    # Match R<N> tokens before UUIDs so `next-task R2` works without an explicit chat ID
    elif not opts["item_id"] and ROADMAP_ITEM_ID_RE.match(a):
      opts["item_id"] = a.upper()
      i += 1
    elif not opts["cid"]:
      opts["cid"] = a
      i += 1
    else:
      _cli_fail("next-task", f"unexpected argument '{a}'")
  return opts


def parse_start_args(args: list[str]) -> dict:
  """Parses `start [--project <p>] [--title <Topic>] [--model <m>] "<prompt>"`."""
  opts = {
      "project": "",
      "title": "",
      "model": "",
      "prompt": "",
  }
  valued = {
      "--project": "project",
      "-p": "project",
      "--title": "title",
      "-t": "title",
      "--model": "model",
      "-m": "model",
  }
  i = 0
  pos_args: list[str] = []
  while i < len(args):
    a = args[i]
    key, eq, inline = a.partition("=")
    if eq and key in valued:
      opts[valued[key]] = inline
      i += 1
    elif a in valued and i + 1 < len(args):
      opts[valued[a]] = args[i + 1]
      i += 2
    elif a in valued:
      _cli_fail("start", f"option '{a}' requires a value")
    elif a.startswith("-"):
      _cli_fail("start", f"unknown option '{a}'")
    else:
      pos_args.append(a)
      i += 1
  if not pos_args:
    _cli_fail("start", "missing required <prompt> argument")
  opts["prompt"] = " ".join(pos_args)
  return opts


def cmd_start(opts: dict) -> int:
  prompt = opts.get("prompt", "").strip()
  if not prompt:
    _cli_fail("start", "missing required prompt text")
    return 1

  # 1. Project
  project_arg = opts.get("project", "").strip()
  if project_arg:
    try:
      project_id = resolve_project_id(project_arg)
    except Exception as exc:
      _cli_fail("start", str(exc))
      return 1
  else:
    project_id = (
        os.environ.get("ANTIGRAVITY_PROJECT_ID", "").strip()
        or "outside-of-project"
    )

  # 2. Topic / Title
  explicit_topic = opts.get("title", "").strip()
  if explicit_topic:
    try:
      clean_topic = validate_chat_topic(explicit_topic)
    except InvalidTopicError as exc:
      _cli_fail("start", str(exc))
      return 1
  else:
    entity = extract_entity_topic(prompt)
    subj = _extract_prompt_subject_words(prompt, max_words=3)
    if entity and subj:
      clean_topic = f"{entity} {subj}"[:42].rstrip()
    elif entity:
      clean_topic = entity
    elif subj:
      clean_topic = subj
    else:
      clean_topic = "New Chat"

  time_str = get_current_time_str()
  title = f"[{time_str}] ⦿ {clean_topic}"

  # 3. Model
  model_arg = opts.get("model", "").strip()
  try:
    model_info = resolve_handoff_model(
        "", tier="pro", explicit=model_arg, project_id=project_id
    )
    model_enum = model_info["enum"]
  except Exception as exc:
    _cli_fail("start", str(exc))
    return 1

  # 4. Start
  try:
    new_cid = start_conversation_exact(
        model_enum=model_enum,
        title=title,
        prompt=prompt,
        project_id=project_id,
    )
  except Exception as exc:
    _cli_fail("start", f"failed to start conversation: {exc}")
    return 1

  print(
      json.dumps(
          {
              "conversationId": new_cid,
              "title": title,
              "projectId": project_id,
              "model": model_enum,
          },
          indent=2,
      )
  )
  return 0


def main(argv: list[str] | None = None) -> int:
  if argv is None:
    argv = sys.argv[1:]
  if not argv or argv[0] in ("-h", "--help", "help"):
    print_usage()
    return 0
  if argv[0] not in KNOWN_COMMANDS:
    print(f"chat_lifecycle.py: unknown command '{argv[0]}'\n", file=sys.stderr)
    print_usage(sys.stderr)
    return 2
  if any(a in ("-h", "--help") for a in argv[1:]):
    print_usage()
    return 0

  cmd = argv[0]
  args = argv[1:]
  if cmd == "set":
    cid = _require_conversation_id("set", args[0] if args else "")
    title = args[1] if len(args) > 1 else ""
    update_conversation_title(cid, title)
  elif cmd == "set-title":
    cid = _require_conversation_id("set-title", args[0] if args else "")
    topic = args[1] if len(args) > 1 else ""
    marker = args[2] if len(args) > 2 else MARKER_ACTIVE
    try:
      new_t = set_conversation_topic(cid, topic, marker)
    except InvalidTopicError as exc:
      _cli_fail("set-title", str(exc))
    print(f"Updated title for {cid}: {new_t}")
  elif cmd == "auto-title":
    cid = _require_conversation_id("auto-title", args[0] if args else "")
    prompt = args[1] if len(args) > 1 else ""
    new_t = auto_title_conversation(cid, prompt)
    if new_t:
      print(f"Updated title for {cid}: {new_t}")
  elif cmd == "summary-path":
    cid = _require_conversation_id(
        "summary-path", args[0] if args else _env_conversation_id())
    topic = args[1] if len(args) > 1 else ""
    p = get_handoff_summary_path(cid, topic)
    print(p)
  elif cmd == "status":
    cid = _require_conversation_id(
        "status", args[0] if args else _env_conversation_id())
    info = get_chain_status(cid)
    print(json.dumps(info, ensure_ascii=False, indent=2))
  elif cmd == "model":
    # Read-only diagnostic: what <id> last used and what a handoff would pick now
    trailer_mode = "--trailer" in args
    rest = [a for a in args if a != "--trailer"]
    cid = _require_conversation_id(
        "model", rest[0] if rest else _env_conversation_id())
    pid = get_conversation_project_id(cid) if cid else ""
    pref = get_project_model_preference(pid) if pid else ""
    out = {
        "conversation_id": cid,
        "project_id": pid or "outside-of-project",
        "project_model_preference": pref or "(none, using inherited/global default)",
        "last_used_enum": get_last_used_model(cid) if cid else "",
    }
    try:
      out["handoff_would_use"] = resolve_handoff_model(cid, project_id=pid)
    except Exception as exc:
      out["handoff_would_use"] = {"error": str(exc)[:300], "fallback": "agentapi tier:pro"}
    out["override_setting"] = read_handoff_model_override()
    if trailer_mode:
      print(get_model_trailer(cid))
    else:
      print(json.dumps(out, ensure_ascii=False, indent=2))
  elif cmd == "project-model":
    if not args:
      print(json.dumps(load_project_models(), ensure_ascii=False, indent=2))
    elif len(args) == 1:
      target = args[0]
      resolved_pid = resolve_project_id(target)
      pref = get_project_model_preference(target)
      print(json.dumps({
          "project_query": target,
          "resolved_project_id": resolved_pid,
          "preferred_model": pref or "(none, using inherited/global default)",
      }, ensure_ascii=False, indent=2))
    else:
      target, new_model = args[0], args[1]
      resolved_pid = resolve_project_id(target)
      try:
        resp = get_available_models()
        models = resp.get("models") or {}
        if new_model not in AGENTAPI_TIERS and not lookup_model(models, new_model):
          print(
              f"[WARN] Model '{new_model}' not found in live GetAvailableModels catalog",
              file=sys.stderr,
          )
      except Exception:
        pass
      rules = load_project_models()
      rules[target] = new_model
      save_project_models(rules)
      print(json.dumps({
          "project": target,
          "resolved_project_id": resolved_pid,
          "preferred_model": new_model,
          "saved_to": DEFAULT_PROJECT_MODELS_FILE,
      }, ensure_ascii=False, indent=2))
  elif cmd == "handoff":
    opts = parse_handoff_args(args)
    cid = _require_conversation_id("handoff", opts["cid"] or _env_conversation_id())
    try:
      result = create_handoff(
          cid,
          summary_file=opts["summary"],
          notes=opts["notes"],
          next_step_prompt=opts["next_step"],
          new_topic=opts["new_topic"],
          exact_model=opts["exact_model"],
          force=opts["force"],
          ignore_live=opts["ignore_live"],
      )
    except InvalidTopicError as exc:
      _cli_fail("handoff", str(exc))
    print_handoff_result(result)
  # Transition to a new root chat for the next roadmap item while closing the current chain
  elif cmd == "next-task":
    opts = parse_next_task_args(args)
    cid = _require_conversation_id("next-task", opts["cid"] or _env_conversation_id())
    try:
      result = create_next_task(
          cid,
          item_id=opts["item_id"],
          new_topic=opts["new_topic"],
          notes=opts["notes"],
          next_step_prompt=opts["next_step"],
          exact_model=opts["exact_model"],
          force=opts["force"],
          ignore_live=opts["ignore_live"],
      )
    except InvalidTopicError as exc:
      _cli_fail("next-task", str(exc))
    print_handoff_result(result)
  elif cmd == "start":
    opts = parse_start_args(args)
    return cmd_start(opts)
  elif cmd == "audit":
    do_fix = False
    proj_filter = ""
    i = 0
    while i < len(args):
      if args[i] == "--fix":
        do_fix = True
        i += 1
      elif args[i] in ("--project", "--project-id") and i + 1 < len(args):
        proj_filter = args[i + 1]
        i += 2
      elif args[i].startswith("--project=") or args[i].startswith("--project-id="):
        proj_filter = args[i].split("=", 1)[1]
        i += 1
      else:
        i += 1
    report = audit_conversations(fix=do_fix, project_filter=proj_filter)
    print(json.dumps(report, ensure_ascii=False, indent=2))
  elif cmd == "archive":
    inc_chain = False
    proj_filter = ""
    keep_count = 1
    cids_to_archive: list[str] = []
    i = 0
    while i < len(args):
      arg = args[i]
      if arg == "--chain":
        inc_chain = True
        i += 1
      elif arg in ("--project", "--project-id") and i + 1 < len(args):
        proj_filter = args[i + 1]
        i += 2
      elif arg.startswith("--project=") or arg.startswith("--project-id="):
        proj_filter = arg.split("=", 1)[1]
        i += 1
      elif arg in ("--keep", "-k") and i + 1 < len(args):
        keep_count = int(args[i + 1])
        i += 2
      elif arg.startswith("--keep="):
        keep_count = int(arg.split("=", 1)[1])
        i += 1
      else:
        cids_to_archive.append(_require_conversation_id("archive", arg))
        i += 1

    if proj_filter:
      res = archive_project_conversations(proj_filter, keep=keep_count)
      print(json.dumps(res, ensure_ascii=False, indent=2))
    elif cids_to_archive:
      res = archive_conversations(cids_to_archive, include_chain=inc_chain)
      print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
      _cli_fail(
          "archive",
          "either --project <p> or at least one conversation ID is required",
      )
  elif cmd == "search":
    query, chain_of = "", ""
    i = 0
    while i < len(args):
      a = args[i]
      if a == "--chain" and i + 1 < len(args):
        chain_of = _require_conversation_id("search", args[i + 1])
        i += 2
      elif a.startswith("-"):
        _cli_fail("search", f"unknown option '{a}'")
      elif not query:
        query = a
        i += 1
      else:
        _cli_fail("search", f"unexpected argument '{a}'")
    # A blank needle would match every message
    if not query.strip():
      _cli_fail("search", "a query is required")
    try:
      chain_hits = search_chain(query, chain_of) if chain_of else []
      res = {} if chain_of else search_conversations(query)
    except RuntimeError as exc:
      _cli_fail("search", str(exc))
    if chain_of:
      for h in chain_hits:
        print(
            f"[piece {h['piece']}/{h['chain_length']}, {h['who']} step {h['step']}]"
            f" {h['snippet']}\n  conversation://{h['cascadeId']}"
        )
      if not chain_hits:
        print("No matches")
    else:
      for h in res["hits"]:
        when = _iso_to_et_stamp(h.get("lastModifiedTime", ""))
        who = "user" if h.get("matchSource") == "MATCH_SOURCE_USER" else "agent"
        print(
            f"{h.get('title', '')}  [piece {h['piece']}/{h['chain_length']}, {when},"
            f" {who} step {h.get('matchedStepIndex', 0)}]\n"
            f"  {h.get('snippet', '')}\n  conversation://{h.get('cascadeId', '')}"
        )
      if res["capped"]:
        print(f"(LS returned {SEARCH_RPC_CAP} chats, the cap: refine the query)")
      if not res["hits"]:
        print("No matches")
  elif cmd == "auto-archive":
    is_hook = "--hook" in args
    target_cid = ""
    if is_hook:
      try:
        if not sys.stdin.isatty():
          payload = json.loads(sys.stdin.read() or "{}")
          target_cid = str(payload.get("conversationId") or "").strip()
      except Exception:
        pass
    else:
      for a in args:
        if a != "--hook" and not a.startswith("-"):
          target_cid = _require_conversation_id("auto-archive", a)
          break
    res = auto_archive_projects(target_cid=target_cid)
    if is_hook:
      print("{}")
    else:
      print(json.dumps(res, ensure_ascii=False, indent=2))
  elif cmd == "reopen":
    cid = _require_conversation_id("reopen", args[0] if args else "")
    reopen_conversation(cid)
  elif cmd == "wait":
    # Allow `wait [on|off|toggle]` using $CONVERSATION_ID or `wait <id> [on|off|toggle]`
    mode_tokens = {
        "on", "true", "1", "waiting", "wait",
        "off", "false", "0", "needs-me", "active", "toggle",
    }
    if args and args[0].lower() in mode_tokens:
      cid = _require_conversation_id("wait", _env_conversation_id())
      mode_str = args[0].lower()
    else:
      cid = _require_conversation_id(
          "wait", args[0] if args else _env_conversation_id()
      )
      mode_str = args[1].lower() if len(args) > 1 else "on"
    if mode_str in ("on", "true", "1", "waiting", "wait"):
      waiting_arg: bool | None = True
    elif mode_str in ("off", "false", "0", "needs-me", "active"):
      waiting_arg = False
    elif mode_str == "toggle":
      waiting_arg = None
    else:
      _cli_fail("wait", f"unknown mode '{mode_str}' (expected on, off, or toggle)")
      return 2
    new_t = set_conversation_wait_state(cid, waiting=waiting_arg)
    print(f"Updated title for {cid}: {new_t}")
  elif cmd == "finalize":
    cids = [_require_conversation_id("finalize", a) for a in args]
    if not cids:
      _cli_fail("finalize", "at least one conversation ID is required")
    closed: list[str] = []
    for cid in cids:
      closed += archive_chain(cid)
    # Also sweep completed sibling worker chats in the same project so one finalize closes the batch
    pid = get_conversation_project_id(cids[0])
    if pid and pid != "outside-of-project":
      sweep_completed_project_chats(pid, finalized_cids=set(closed))
  return 0


if __name__ == "__main__":
  sys.exit(main())

