(() => {
  if (typeof window.__jetskiClosedChatsFilterCleanup === 'function') {
    try {
      window.__jetskiClosedChatsFilterCleanup();
    } catch {
      // Ignore cleanup errors from older versions
    }
  }
  window.__jetskiClosedChatsFilterInstalled = true;

  const STORAGE_BUCKETS_KEY = 'jetski_visible_buckets_v1';
  // Persists whether the top-of-sidebar Needs-Reaction inbox list is collapsed
  const STORAGE_INBOX_COLLAPSED_KEY = 'jetski_inbox_collapsed_v1';
  const BAR_CLASS = 'jetski-attention-filter-bar';
  // CSS class names for the collapsible Needs-Reaction inbox widget and its child controls
  const INBOX_CLASS = 'jetski-needs-reaction-inbox';
  const INBOX_HEADER_CLASS = 'jetski-inbox-header';
  const INBOX_TOGGLE_BTN_CLASS = 'jetski-inbox-toggle-btn';
  const INBOX_LIST_CLASS = 'jetski-inbox-list';
  const INBOX_ROW_CLASS = 'jetski-inbox-row';
  const INBOX_TAB_BTN_CLASS = 'jetski-inbox-tab-btn';
  const INBOX_SEEN_BTN_CLASS = 'jetski-inbox-seen-btn';
  const INBOX_ARCHIVE_BTN_CLASS = 'jetski-inbox-archive-btn';
  const BUTTON_CLASS = 'jetski-closed-chats-filter-btn';
  const TITLEBAR_BTN_CLASS = 'jetski-titlebar-archive-btn';
  // Live context window volume pill rendered before Search in each conversation titlebar
  const TITLEBAR_CONTEXT_BADGE_CLASS = 'jetski-titlebar-context-badge';
  const CONTEXT_SOFT_PCT = 60;
  const CONTEXT_TURN_START_PCT = 65;
  const CONTEXT_HARD_PCT = 80;
  const CONTEXT_MIN_FETCH_INTERVAL_MS = 1200;
  const CONTEXT_RUNNING_POLL_MS = 4000;
  const CONTEXT_CACHE_MAX_ENTRIES = 64;
  // Right-side ⏳ badge rendered on open ⦿ rows waiting on background tasks or subagents
  const ROW_BG_WAIT_INDICATOR_CLASS = 'jetski-row-bg-wait-indicator';
  // Compact open-task badge rendered inside sidebar project headers
  const PROJECT_ROADMAP_BADGE_CLASS = 'jetski-project-roadmap-badge';
  const STYLE_ID = 'jetski-closed-chats-filter-style';

  // Lifecycle titles follow "[HH:MM] <marker> <Topic>"; finished chats and handed-off chain
  // pieces are archived by chat_lifecycle.py, so visible chats carry only ⦿ or ⏳
  const TITLE_MARKER_RE = /^\s*\[\d{1,2}:\d{2}\]\s*([⏳⦿])(?:\s|$)/u;
  // Matches legacy pre-archive closed markers ("[HH:MM] «»|‹✓›|«|» <Topic>")
  const LEGACY_CLOSED_TITLE_RE = /^\s*\[\d{1,2}:\d{2}\]\s*(?:‹✓›|«»|[«»])(?:\s|$)/u;
  // Matches the leading "[HH:MM] ⦿" prefix so background-waiting rows can display ⏳ in DOM
  const ACTIVE_TO_WAITING_DISPLAY_RE = /^(\s*\[\d{1,2}:\d{2}\]\s*)⦿(\s|$)/u;
  const TIME_PREFIX_EXTRACT_RE = /^\s*\[(\d{1,2}:\d{2})\]/;
  const CONT_WORD = '(?:\\u043f\\u0440\\u043e\\u0434\\u043e\\u043b\\u0436\\u0435\\u043d\\u0438\\u0435|continue)';
  const TITLE_CLEANUP_RE = new RegExp(
    `^(?:\\s*\\[\\d{1,2}:\\d{2}(?:\\s+${CONT_WORD})?\\]\\s*(?:‹✓›|«»|⏳|[▸✓⦿«»]|\\s*)*)|^(?:${CONT_WORD}(?:\\s+\\d{1,2}:\\d{2})?\\s*:\\s*)+`,
    'iu'
  );
  const TRAILING_CLEANUP_RE = new RegExp(
    `\\s*\\(${CONT_WORD}\\s+(?:\\d{4}-\\d{2}-\\d{2}\\s+)?\\d{1,2}:\\d{2}\\)\\s*$`,
    'iu'
  );
  const OPEN_ACTIVE_MARKER = '⦿';
  const OPEN_WAITING_MARKER = '⏳';

  const BUCKET_ORDER = ['needs-me', 'working', 'waiting'];
  const SEARCH_BTN_CLASS = 'jetski-chat-search-btn';
  const LOCATE_BTN_CLASS = 'jetski-chat-locate-btn';
  const LOCATED_ROW_ATTR = 'data-jetski-located-row';
  const SEARCH_PANEL_ID = 'jetski-chat-search-panel';
  // Matryoshka chain star markers and read-only non-tail piece lock banners
  const CHAIN_STARS_BEFORE_TEST_ID = 'jetski-chain-stars-before';
  const CHAIN_STARS_AFTER_TEST_ID = 'jetski-chain-stars-after';
  const CHAIN_STARS_GROUP_CLASS = 'jetski-chain-stars-group';
  const CHAIN_STAR_BTN_CLASS = 'jetski-chain-star-btn';
  const ARCHIVE_BANNER_CLASS = 'jetski-archive-piece-banner';
  // Holds the read-only notice text so updating it does not wipe the jump button child node
  const ARCHIVE_LABEL_CLASS = 'jetski-archive-piece-label';
  const JUMP_TO_TAIL_BTN_CLASS = 'jetski-jump-to-tail-btn';
  const STAR_TOOLTIP_ID = 'jetski-chain-star-tooltip';
  const DEFAULT_VISIBLE_BUCKETS = ['needs-me', 'working'];

  const BUCKET_DEFS = {
    'needs-me': {
      key: 'needs-me',
      icon: '🚨',
      shortLabel: 'Needs Me',
      title: '🚨 Needs Me (⦿)',
    },
    working: {
      key: 'working',
      icon: '🤖',
      shortLabel: 'Working',
      title: '🤖 Agent Working',
    },
    waiting: {
      key: 'waiting',
      icon: '⏳',
      shortLabel: 'Waiting',
      title: '⏳ Waiting (Cron / External)',
    },
  };

  function safeGetStorage(key) {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function safeSetStorage(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      // Cross-origin iframe contexts with third-party storage blocked keep state in memory
    }
  }

  function loadVisibleBuckets() {
    const raw = safeGetStorage(STORAGE_BUCKETS_KEY);
    if (raw) {
      try {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) {
          const valid = parsed.filter((k) => BUCKET_ORDER.includes(k));
          // A set made only of retired buckets ('closed', 'steps') is not a choice of an empty
          // sidebar, so it falls back to the defaults; a saved [] stays empty
          if (valid.length > 0 || parsed.length === 0) return new Set(valid);
        }
      } catch {
        // Fall back to default visible buckets on malformed storage JSON
      }
    }
    return new Set(DEFAULT_VISIBLE_BUCKETS);
  }

  function saveVisibleBuckets(bucketsSet) {
    const arr = BUCKET_ORDER.filter((k) => bucketsSet.has(k));
    safeSetStorage(STORAGE_BUCKETS_KEY, JSON.stringify(arr));
  }

  let visibleBuckets = loadVisibleBuckets();
  // Hydrate initial inbox collapsed state so the header renders without layout shift
  let inboxCollapsed = safeGetStorage(STORAGE_INBOX_COLLAPSED_KEY) === 'true';
  const STORAGE_INBOX_TAB_KEY = 'jetski_inbox_tab_v1';
  const INBOX_TABS = ['needs-me', 'working', 'stale'];
  // Typographic tab labels without emoji prefixes fit on one header line in a narrow sidebar
  const INBOX_TAB_DEFS = [
    { key: 'needs-me', label: 'Needs Me' },
    { key: 'working', label: 'Working' },
    { key: 'stale', label: 'Stale' },
  ];
  const STALE_THRESHOLD_MS = 7 * 24 * 60 * 60 * 1000;
  const INBOX_EMPTY_TEXT = {
    'needs-me': '✓ All caught up: nothing needs you',
    'working': 'No agents are running',
    'stale': 'No stale chats',
  };
  // Always start on Needs Me; manualEmptyTabKey allows viewing an empty tab when clicked
  let activeInboxTab = 'needs-me';
  let manualEmptyTabKey = null;
  // Latches unseen needs-me chats so server view timestamps on pane mount do not dismiss them
  const latchedNeedsMeModMs = (window.__jetskiLatchedNeedsMeModMs ||= new Map());

  // Tracks conversations whose hideFromConversationList flag we flipped in Redux
  // so we never unhide conversations that were natively hidden by their source
  const managedHiddenIds = new Map();
  // Tracks cascadeIds where auxPaneV2/setOverviewPanelOpen was dispatched so stores
  // without the auxPaneV2 reducer never re-dispatch in a loop
  const overviewCloseDispatchedIds = new Set();
  let hiddenCountsByProject = new Map();
  let totalHiddenCount = 0;
  let breakdownStats = emptyBreakdown();
  let reduxStore = null;
  let isInternalDispatch = false;
  let rafScheduled = false;
  // Set by cleanup; the store keeps this instance's dispatch patch and subscriber after a
  // re-injection, so both must go inert instead of fighting the new instance
  let disposed = false;
  let activeHoverBtn = null;
  let activeHoverBucket = null;
  // Tracks which project roadmap badge owns the shared floating bubble tooltip
  let activeHoverRoadmapBadge = null;
  let activeHoverRoadmapProjectId = null;
  // Tracks which titlebar context badge owns the shared floating bubble tooltip
  let activeHoverContextBadge = null;
  let activeHoverContextCascadeId = null;
  // Per-conversation paged generatorMetadata cache so updates stay ~2 KB instead of growing
  const contextUsageCache = new Map();
  let chainIndexCache = null;
  let chainIndexFetchedAt = 0;
  // Auto-switch follows a fresh handoff redirect only for views the user did not open by hand,
  // and at most once per edge, so going back to an archived piece never bounces forward.
  // Both sets live on window: star buttons rendered by an earlier injection keep their DOM
  // identity and closures, and a re-injected instance must not repeat a done switch
  const AUTO_SWITCH_MAX_AGE_MS = 60000;
  const manualVisitedCids = (window.__jetskiManualVisitedCids ||= new Set());
  const observedArchivedViewIds = new Set();
  const autoSwitchedEdges = (window.__jetskiAutoSwitchedEdges ||= new Set());
  // Tracks the latest seen modification timestamp (ms) per cascadeId across re-injections
  const locallySeenModifiedMs = (window.__jetskiSeenModifiedMs ||= new Map());
  // Remembers explicitly dismissed blocked-step signatures so ✓ clears only that exact block
  const dismissedBlockedSigs = (window.__jetskiDismissedBlockedSigs ||= new Map());
  // Bounded trajectory evaluation caches keyed by summary tree signature with zero poll timers
  const TRAJ_STATUS_CACHE_MAX_ENTRIES = 64;
  const SINGLE_TRAJ_CACHE_MAX_ENTRIES = 96;
  const RECENT_TRAJ_CHECK_WINDOW_MS = 6 * 60 * 60 * 1000;
  const SILENT_SUBAGENT_GRACE_MS = 30 * 1000;
  const STALE_IDLE_TURN_MS = 15 * 60 * 1000;
  const trajectoryStatusCache = new Map();
  const singleTrajCache = new Map();
  // Accept both @bufbuild/protobuf v2 numeric enums in Redux and Connect-JSON strings
  const ACTIVE_RUN_STATUSES = new Set([
    'CASCADE_RUN_STATUS_RUNNING',
    'CASCADE_RUN_STATUS_BUSY',
    'CASCADE_RUN_STATUS_CANCELING',
    2,
    3,
    4,
  ]);
  const WAITING_STEP_STATUSES = new Set(['CORTEX_STEP_STATUS_WAITING', 9, 'WAITING']);
  const DONE_STEP_STATUSES = new Set(['CORTEX_STEP_STATUS_DONE', 3, 'DONE']);
  const TERMINAL_STEP_STATUSES = new Set([
    'CORTEX_STEP_STATUS_CANCELED',
    6,
    'CANCELED',
    'CORTEX_STEP_STATUS_ERROR',
    7,
    'ERROR',
    'CORTEX_STEP_STATUS_INTERRUPTED',
    12,
    'INTERRUPTED',
  ]);
  const ERROR_TERM_REASONS = new Set(['EXECUTOR_TERMINATION_REASON_ERROR', 1, 'ERROR']);
  const CANCELED_TERM_REASONS = new Set([
    'EXECUTOR_TERMINATION_REASON_USER_CANCELED',
    2,
    'USER_CANCELED',
  ]);
  const USER_INPUT_STEP_TYPES = new Set(['CORTEX_STEP_TYPE_USER_INPUT', 14]);
  const PLANNER_STEP_TYPES = new Set(['CORTEX_STEP_TYPE_PLANNER_RESPONSE', 15]);
  const SYSTEM_MSG_STEP_TYPES = new Set(['CORTEX_STEP_TYPE_SYSTEM_MESSAGE', 101]);
  const INVOKE_SUBAGENT_STEP_TYPES = new Set(['CORTEX_STEP_TYPE_INVOKE_SUBAGENT', 127]);
  const ASK_QUESTION_STEP_TYPES = new Set(['CORTEX_STEP_TYPE_ASK_QUESTION', 138]);
  const GENERIC_STEP_TYPES = new Set(['CORTEX_STEP_TYPE_GENERIC', 132]);
  // Post-response bookkeeping steps emitted after PLANNER_RESPONSE on finished turns
  const TRANSPARENT_TAIL_STEP_TYPES = new Set([
    'CORTEX_STEP_TYPE_FINISH',
    2,
    'CORTEX_STEP_TYPE_CHECKPOINT',
    23,
    'CORTEX_STEP_TYPE_SUGGESTED_RESPONSES',
    27,
    'CORTEX_STEP_TYPE_MEMORY',
    38,
    'CORTEX_STEP_TYPE_EPHEMERAL_MESSAGE',
    90,
    'CORTEX_STEP_TYPE_CONVERSATION_HISTORY',
    98,
    'CORTEX_STEP_TYPE_KNOWLEDGE_ARTIFACTS',
    99,
    'CORTEX_STEP_TYPE_KNOWLEDGE_GENERATION',
    102,
    'CORTEX_STEP_TYPE_DIRECTORY_RULES',
    139,
  ]);
  const ONGOING_BATTLE_END_TYPES = new Set([0, '', null, undefined, 'BATTLE_END_TYPE_UNSPECIFIED']);
  // Caches jetski-roadmaps.json across DOM sync cycles within ROADMAP_INDEX_TTL_MS
  const ROADMAP_INDEX_TTL_MS = 5000;
  let roadmapIndexCache = null;
  let roadmapIndexFetchedAt = 0;
  let roadmapIndexFetching = false;
  // Caches GetAvailableModels across UserStatus polls so hidden models stay unlocked
  const AVAILABLE_MODELS_TTL_MS = 60000;
  let availableModelsCache = null;
  let availableModelsFetchedAt = 0;
  let availableModelsFetching = null;
  let lastAppliedModelsFetchedAt = 0;

  // Numeric codeium_common_pb.Model enum values for non-placeholder model identifiers
  const NAMED_MODEL_ENUM_VALUES = {
    MODEL_GOOGLE_GEMINI_2_5_PRO: 246,
    MODEL_CLAUDE_4_SONNET: 281,
    MODEL_CLAUDE_4_SONNET_THINKING: 282,
    MODEL_CLAUDE_4_OPUS: 290,
    MODEL_CLAUDE_4_OPUS_THINKING: 291,
    MODEL_GOOGLE_GEMINI_2_5_FLASH: 312,
    MODEL_GOOGLE_GEMINI_2_5_FLASH_THINKING: 313,
    MODEL_GOOGLE_GEMINI_FOR_GOOGLE_2_5_PRO: 327,
    MODEL_GOOGLE_GEMINI_NEMOSREEF: 328,
    MODEL_GOOGLE_GEMINI_2_5_FLASH_THINKING_TOOLS: 329,
    MODEL_GOOGLE_GEMINI_2_5_FLASH_LITE: 330,
    MODEL_GOOGLE_GEMINI_2_5_PRO_EVAL: 331,
    MODEL_CLAUDE_4_5_SONNET: 333,
    MODEL_CLAUDE_4_5_SONNET_THINKING: 334,
    MODEL_CLAUDE_4_5_HAIKU: 340,
    MODEL_CLAUDE_4_5_HAIKU_THINKING: 341,
    MODEL_OPENAI_GPT_OSS_120B_MEDIUM: 342,
  };

  // Excludes artificial-latency battle arms and aliases that duplicate visible dropdown models
  const SKIPPED_HIDDEN_MODEL_IDS = new Set([
    'bon-experiment-arm-232',
    'bon-experiment-arm-233',
    'bon-experiment-arm-234',
    'bon-experiment-arm-235',
    'fable-5.1-dropdown',
    'gemini-4-carbon',
    'gemini-launch-candidate',
    'gemini-opt-out',
    'gemini-3.8-flash-tiered',
  ]);

  // Unmasks server-side "Gemini Pro Next" labels into unique dropdown labels for ModelSelector
  const CURATED_HIDDEN_MODELS = [
    ['glm-5.2-fp8-bon-arm', 'GLM 5.2', '3P'],
    ['kimi-k3-maas-bon-arm', 'Kimi K3', '3P'],
    ['sonnet-5.5-bon-le', 'Claude Sonnet 5.5 (Max)', 'BoN'],
    ['sonnet-5.5-high-bon-le', 'Claude Sonnet 5.5 (High)', 'BoN'],
    ['opus-5-bon-le', 'Claude Opus 5 (High)', 'BoN'],
    ['opus-4.8-max-bon-le', 'Claude Opus 4.8 (Max)', 'BoN'],
    ['opus-4.8-bon-le', 'Claude Opus 4.8 (High)', 'BoN'],
    ['opus-bon-le', 'Claude Opus 4.6', 'BoN'],
    ['opus-5.5-max-bon-le', 'Claude Opus 5.5 BoN (Max)', 'BoN'],
    ['opus-5.5-high-bon-le', 'Claude Opus 5.5 BoN (High)', 'BoN'],
    ['fable-5.1-max-bon-le', 'Claude Fable 5.1 BoN (Max)', 'BoN'],
    ['fable-5.1-bon-le', 'Claude Fable 5.1 BoN (High)', 'BoN'],
    ['bon-experiment-arm-226', 'Gemini 4 Carbon-G', 'Exp'],
    ['bon-experiment-arm-221', 'Gemini Perseus Fast', 'Exp'],
    ['bon-experiment-arm-222', 'Gemini CSWise Fast (High)', 'Exp'],
    ['bon-experiment-arm-223', 'Gemini CSWise Fast (Medium)', 'Exp'],
    ['bon-experiment-arm-227', 'Gemini Skimaki Fast', 'Exp'],
    ['bon-experiment-arm-229', 'Gemini Barium-B', 'Exp'],
    ['bon-experiment-arm-228', 'Gemini LE-01', 'Exp'],
    ['bon-experiment-arm-225', 'Gemini LE-02', 'Exp'],
    ['bon-experiment-arm-230', 'Gemini LE-05', 'Exp'],
    ['bon-experiment-arm-224', 'Gemini LE-06', 'Exp'],
    ['bon-experiment-arm-231', 'Gemini LE-11', 'Exp'],
    ['gemini-3.8-flash-router', 'Gemini 3.8 Flash Router', 'Exp'],
    ['snowball-subagent', 'Gemini Snowball Subagent', 'Exp'],
    ['gemini-3.1-flash-lite', 'Gemini 3.1 Flash Lite', 'Exp'],
    ['gemini-2.5-flash-thinking', 'Gemini 2.5 Flash (Thinking)', '2.5'],
    ['gemini-2.5-flash', 'Gemini 2.5 Flash', '2.5'],
    ['gemini-2.5-flash-lite', 'Gemini 2.5 Flash Lite', '2.5'],
    ['opus-5.5-medium', 'Claude Opus 5.5 (Medium)', 'WTU'],
    ['gemini-pro-coordinator', 'Gemini Pro Orchestrator', 'Pro'],
  ];
  const CURATED_HIDDEN_MODEL_MAP = new Map(
    CURATED_HIDDEN_MODELS.map(([id, label, tag]) => [id, { label, tag }])
  );

  // Two-threshold quota autopilot floors and floating notification toast ID (R71.2)
  const QUOTA_SOFT_FLOOR = 0.30;
  const QUOTA_HARD_FLOOR = 0.15;
  const EMERGENCY_PIN_FLOOR = 0.05;
  const AUTOPILOT_TOAST_ID = 'jetski-model-autopilot-toast';
  // Persist manual model pins across F5 reloads with a 30m TTL on unsent project drafts
  const MANUAL_MODEL_PINS_STORAGE_KEY = 'jetski_manual_model_pins_v1';
  const DRAFT_MANUAL_PIN_TTL_MS = 30 * 60 * 1000;
  const MANUAL_MODEL_PINS_MAX_ENTRIES = 200;
  let autopilotToastTimer = 0;
  // Remembers conversations and project drafts where the user manually picked a model
  const manualPickedModels = (window.__jetskiManualPickedModels ||= new Map());
  // Tracks the last project profile key auto-seeded for a draft or conversation
  const autoSeededTargets = new Map();

  const PROJECT_MODEL_PROFILES = {
    // triage profile
    '3f5b7a12-8e5f-47d2-a2f4-d644447c86a8': 'triage',
    'skia-g3-roll-check': 'triage',
    '52d8efe1-4a60-4325-b9f8-4c02156f8a25': 'triage',
    'debug_skia_roll_issue': 'triage',
    // cpp-core profile
    '1a021a3a-e0f3-4f85-ab21-42cfba6cf4d4': 'cpp-core',
    'chromium': 'cpp-core',
    '9571f9cb-06ce-4b6b-9a5d-9aa1ab55c52a': 'cpp-core',
    'lazy android': 'cpp-core',
    '42497f18-ff9d-4fd5-9e8b-e666147873c9': 'cpp-core',
    'textclusters api': 'cpp-core',
    '8acb67c4-2cb7-43ca-ba04-dc547b3c4788': 'cpp-core',
    'skparagraph': 'cpp-core',
    '9b93560e-8b7f-4b1c-9dfd-8290e4884541': 'cpp-core',
    'skia': 'cpp-core',
    '100deebd-72df-4dbe-a33a-aae6712f994e': 'cpp-core',
    'skoretext': 'cpp-core',
    'dc42bd7d-5c00-4416-9365-00cb2a59acaa': 'cpp-core',
    'skglyphid to 32 bits': 'cpp-core',
    '83acaa3f-d7d7-412d-9d07-d33229f9a45b': 'cpp-core',
    'typescript webparagraph': 'cpp-core',
    '2f999b18-d1cd-4eb5-9bdd-1c8fe328762e': 'cpp-core',
    'webparagraph': 'cpp-core',
    'bedf614c-197e-4ca3-b2d0-48b9def664c3': 'cpp-core',
    'text editor': 'cpp-core',
    // orchestrator profile
    '04508c43-54a0-4003-8051-7129cd997b88': 'orchestrator',
    'improving experience': 'orchestrator',
    '5a2a0f80-96c2-4334-a58d-7631310d4f72': 'orchestrator',
    'flight deck': 'orchestrator',
    '1c95149f-2787-4209-976d-350eb2a6532c': 'orchestrator',
    'code review organizer': 'orchestrator',
    'e914e1c4-006a-433c-8270-52eff8775667': 'orchestrator',
    'skia gardener': 'orchestrator',
    'a29f214d-9fb4-46fd-a8f6-7d60a7e2aaa3': 'orchestrator',
    'breaking chats': 'orchestrator',
  };

  const WEB_MODEL_PROFILES = {
    'cpp-core': [
      'opus-5.5-high-bon-le',
      'opus-5.5-medium',
      'opus-5.5-max-bon-le',
      'opus-5.5-high',
      'sonnet-5.5-high-bon-le',
      'sonnet-5.5-bon-le',
      'bon-experiment-arm-226',
      'gemini-pro-coordinator',
      'auto',
    ],
    'orchestrator': [
      'gemini-pro-coordinator',
      'bon-experiment-arm-226',
      'auto',
      'sonnet-5.5-high-bon-le',
      'sonnet-5.5-bon-le',
      'gemini-flash-next',
      'gemini-3.8-flash-high',
      'gemini-3.8-flash-medium',
      'gemini-2.5-flash',
      'gemini-3.1-pro-high',
    ],
    'triage': [
      'gemini-flash-next',
      'gemini-3.8-flash-high',
      'gemini-3.8-flash-medium',
      'gemini-2.5-flash',
      'gemini-3.5-flash-lite',
      'gemini-pro-coordinator',
      'auto',
    ],
  };

  function isActiveRunStatus(status) {
    return ACTIVE_RUN_STATUSES.has(status);
  }

  function parseTitleMarker(title) {
    if (typeof title !== 'string') return null;
    const match = TITLE_MARKER_RE.exec(title);
    return match ? match[1] : null;
  }

  function isWaitingTitle(title) {
    return parseTitleMarker(title) === OPEN_WAITING_MARKER;
  }

  // True when an open leaf ⦿ chat is unblocked and waiting on background work or subagent synthesis
  function isWaitingOnBackgroundWork(summary, curTitle, statusInfo) {
    if (!summary || parseTitleMarker(curTitle) !== OPEN_ACTIVE_MARKER) return false;
    // Blocked chats need immediate user input and must keep ⦿ rather than swapping to ⏳
    if (!statusInfo || statusInfo.isBlocked || isActiveRunStatus(summary.status)) return false;
    return statusInfo.bucket === 'working';
  }

  // Returns the underlying row title before any visual ⦿ -> ⏳ background-wait DOM substitution
  function getRawDomTitle(rowEl) {
    const linkEl = rowEl?.querySelector('a[aria-label]');
    const spanEl = rowEl?.querySelector('span.truncate');
    // Prefer the saved original title while a temporary ⏳ display swap is active on spanEl
    if (spanEl?.hasAttribute('data-jetski-orig-title')) {
      const curText = spanEl.textContent || '';
      // If React re-rendered spanEl with a non-⏳ title, use the fresh text directly
      if (curText && parseTitleMarker(curText) !== OPEN_WAITING_MARKER) {
        return curText;
      }
      return spanEl.getAttribute('data-jetski-orig-title') || curText;
    }
    return linkEl?.getAttribute('aria-label') || spanEl?.textContent || '';
  }

  function getCurrentTimeStr() {
    try {
      const fmt = new Intl.DateTimeFormat('en-US', {
        timeZone: 'America/New_York',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      });
      const parts = fmt.formatToParts(new Date());
      let hh = '00';
      let mm = '00';
      for (const p of parts) {
        if (p.type === 'hour') hh = p.value === '24' ? '00' : p.value.padStart(2, '0');
        if (p.type === 'minute') mm = p.value.padStart(2, '0');
      }
      return `${hh}:${mm}`;
    } catch {
      const d = new Date();
      return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    }
  }

  function extractTimePrefix(title, fallbackTime) {
    if (typeof title === 'string') {
      const m = TIME_PREFIX_EXTRACT_RE.exec(title);
      if (m) {
        const [h, min] = m[1].split(':');
        return `${h.padStart(2, '0')}:${min}`;
      }
    }
    return fallbackTime || getCurrentTimeStr();
  }

  function cleanBaseTitle(title) {
    if (typeof title !== 'string' || !title) return '';
    let s = title.replace(TITLE_CLEANUP_RE, '').trim();
    s = s.replace(TRAILING_CLEANUP_RE, '').trim();
    return s;
  }

  function getCsrfToken() {
    if (window.__APP_CONFIG__?.csrfToken) {
      return window.__APP_CONFIG__.csrfToken;
    }
    const scripts = document.querySelectorAll('script[src*="jetski-closed-filter.js"]');
    for (const s of scripts) {
      const src = s.getAttribute('src') || '';
      const m = /[?&]csrf=([^&]+)/.exec(src);
      if (m) {
        try {
          return decodeURIComponent(m[1]);
        } catch {
          return m[1];
        }
      }
    }
    return '';
  }

  async function fetchChainIndex() {
    const now = Date.now();
    if (chainIndexCache && now - chainIndexFetchedAt < 5000) {
      return chainIndexCache;
    }
    if (typeof window.fetch !== 'function') {
      return chainIndexCache || { parentOf: {}, createdAt: {}, redirects: {} };
    }
    try {
      const csrf = encodeURIComponent(getCsrfToken());
      const resp = await window.fetch(
        `/static/artifacts/jetski-chains.json?v=${now}&csrf=${csrf}`
      );
      if (resp && resp.ok) {
        const data = await resp.json();
        if (data && typeof data === 'object') {
          chainIndexCache = {
            parentOf: data.parentOf || {},
            createdAt: data.createdAt || {},
            redirects: data.redirects || {},
          };
          chainIndexFetchedAt = now;
          return chainIndexCache;
        }
      }
    } catch {
      // Ignore network errors and fall back to cached or single-chat resolution
    }
    return chainIndexCache || { parentOf: {}, createdAt: {}, redirects: {} };
  }

  async function fetchRoadmapIndex() {
    const now = Date.now();
    if (roadmapIndexCache && now - roadmapIndexFetchedAt < ROADMAP_INDEX_TTL_MS) {
      return roadmapIndexCache;
    }
    if (typeof window.fetch !== 'function') {
      return roadmapIndexCache || { projects: {} };
    }
    try {
      const csrf = encodeURIComponent(getCsrfToken());
      const resp = await window.fetch(
        `/static/artifacts/jetski-roadmaps.json?v=${now}&csrf=${csrf}`
      );
      if (resp && resp.ok) {
        const data = await resp.json();
        if (data && typeof data === 'object') {
          roadmapIndexCache = {
            projects: data.projects || {},
          };
          roadmapIndexFetchedAt = now;
          return roadmapIndexCache;
        }
      }
    } catch {
      // Ignore network errors and preserve cached roadmap summaries
    }
    return roadmapIndexCache || { projects: {} };
  }

  // Periodically refreshes jetski-roadmaps.json so sidebar project badges track CLI updates
  function ensureRoadmapIndexLoaded() {
    const now = Date.now();
    if (
      roadmapIndexFetching ||
      (roadmapIndexCache && now - roadmapIndexFetchedAt < ROADMAP_INDEX_TTL_MS)
    ) {
      return;
    }
    if (typeof window.fetch !== 'function') return;
    roadmapIndexFetching = true;
    const prevSig = JSON.stringify(roadmapIndexCache?.projects || {});
    fetchRoadmapIndex()
      .then((idx) => {
        roadmapIndexFetching = false;
        const nextSig = JSON.stringify(idx?.projects || {});
        if (nextSig !== prevSig) {
          scheduleSync();
        }
      })
      .catch(() => {
        roadmapIndexFetching = false;
      });
  }

  // Clones keep the protobuf prototypes, since Redux selectors read the summary as a message
  function cloneSummaryWithAnnotations(summary, annotationsPatch) {
    const summaryProto = Object.getPrototypeOf(summary);
    const nextSummary = Object.assign(Object.create(summaryProto), summary);
    const prevAnn = summary.annotations;
    const nextAnn = prevAnn
      ? Object.assign(Object.create(Object.getPrototypeOf(prevAnn)), prevAnn)
      : {};
    Object.assign(nextAnn, annotationsPatch);
    nextSummary.annotations = nextAnn;
    return nextSummary;
  }

  function getTitleForCascadeId(cascadeId, fallbackDomTitle) {
    const store = findReduxStore();
    const s = store?.getState()?.trajectorySummaries?.summaries?.[cascadeId];
    const fromStore = s?.annotations?.title || s?.summary || '';
    return fromStore || fallbackDomTitle || '';
  }

  function isArchivedCascadeId(cascadeId) {
    const store = findReduxStore();
    const s = store?.getState()?.trajectorySummaries?.summaries?.[cascadeId];
    return Boolean(s?.annotations?.archived);
  }

  async function postUpdateAnnotationsRpc(cascadeId, annotations) {
    if (typeof window.fetch !== 'function') return false;
    try {
      const resp = await window.fetch(
        '/exa.language_server_pb.LanguageServerService/UpdateConversationAnnotations',
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'x-codeium-csrf-token': getCsrfToken(),
          },
          body: JSON.stringify({
            cascadeIds: [cascadeId],
            annotations,
            mergeAnnotations: true,
          }),
        }
      );
      return Boolean(resp && resp.ok);
    } catch {
      return false;
    }
  }

  // Applies an optimistic Redux summary update before the async LS RPC completes
  function dispatchSummaryUpdate(cascadeId, nextSummary) {
    findReduxStore()?.dispatch({
      type: 'trajectorySummaries/applyStreamBatch',
      payload: { updates: [[cascadeId, nextSummary]], deletes: [] },
    });
  }

  // Flips annotations.archived of one chat; chat_lifecycle.py archives handed-off chain pieces
  // itself, so the UI never walks the chain or rewrites titles
  async function toggleConversationArchived(cascadeId) {
    if (!cascadeId) return;
    latchedNeedsMeModMs.delete(cascadeId);
    const archived = !isArchivedCascadeId(cascadeId);
    const s = findReduxStore()?.getState()?.trajectorySummaries?.summaries?.[cascadeId];
    if (s) {
      dispatchSummaryUpdate(cascadeId, cloneSummaryWithAnnotations(s, { archived }));
    }
    runFullSync();
    await postUpdateAnnotationsRpc(cascadeId, { archived });
  }

  function getStepOneofPayload(step, camel, snake) {
    if (!step || typeof step !== 'object') return null;
    const direct = step[camel] || step[snake];
    if (direct && typeof direct === 'object') return direct;
    const inner = step.step;
    if (inner && typeof inner === 'object') {
      if (
        (inner.case === camel || inner.case === snake) &&
        inner.value &&
        typeof inner.value === 'object'
      ) {
        return inner.value;
      }
      const directInner = inner[camel] || inner[snake];
      if (directInner && typeof directInner === 'object') return directInner;
    }
    return null;
  }

  // Extracts user-facing detail when a step is blocked on permission, question, or elicitation
  function parseStepInteraction(step, requireWaitingStatus = true) {
    if (!step || typeof step !== 'object') return null;
    if (requireWaitingStatus && !WAITING_STEP_STATUSES.has(step.status)) return null;

    const requested =
      step.requestedInteraction || step.requested_interaction || {};
    const oneof =
      requested && typeof requested.interaction === 'object'
        ? requested.interaction
        : null;
    const oneofCase = oneof?.case || '';
    const oneofVal =
      oneof && oneof.value && typeof oneof.value === 'object' ? oneof.value : null;

    let perm =
      requested?.permission && typeof requested.permission === 'object'
        ? requested.permission
        : null;
    if (!perm && oneofCase === 'permission') perm = oneofVal;

    let ask =
      (requested?.askQuestion && typeof requested.askQuestion === 'object'
        ? requested.askQuestion
        : null) ||
      (requested?.ask_question && typeof requested.ask_question === 'object'
        ? requested.ask_question
        : null);
    if (!ask && (oneofCase === 'askQuestion' || oneofCase === 'ask_question')) {
      ask = oneofVal;
    }
    if (!ask && oneof && typeof oneof === 'object') {
      ask =
        (oneof.askQuestion && typeof oneof.askQuestion === 'object'
          ? oneof.askQuestion
          : null) ||
        (oneof.ask_question && typeof oneof.ask_question === 'object'
          ? oneof.ask_question
          : null);
    }
    if (!ask && ASK_QUESTION_STEP_TYPES.has(step.type)) {
      ask = getStepOneofPayload(step, 'askQuestion', 'ask_question');
    }
    if (!ask) {
      const generic =
        getStepOneofPayload(step, 'generic', 'generic') ||
        (GENERIC_STEP_TYPES.has(step.type) ? step : null);
      if (generic) {
        const genRes = generic.result || generic;
        const genPayload =
          genRes.payload ||
          genRes.resultStruct ||
          genRes.result_struct ||
          genRes.resultValue ||
          genRes.result_value;
        const genReq =
          generic.requestedInteraction ||
          generic.requested_interaction ||
          genRes.requestedInteraction ||
          genRes.requested_interaction;
        if (genReq && typeof genReq === 'object') {
          const genInter = genReq.interaction;
          ask =
            genReq.askQuestion ||
            genReq.ask_question ||
            (genInter?.case === 'askQuestion' || genInter?.case === 'ask_question'
              ? genInter.value
              : null) ||
            genInter?.askQuestion ||
            genInter?.ask_question;
        }
        if (!ask && genPayload && typeof genPayload === 'object') {
          ask =
            genPayload.askQuestion ||
            genPayload.ask_question ||
            (Array.isArray(genPayload.questions) ? genPayload : null);
        }
        if (!ask) {
          ask =
            generic.askQuestion ||
            generic.ask_question ||
            (Array.isArray(generic.questions) ? generic : null);
        }
      }
    }
    if (
      !ask &&
      (Array.isArray(step.questions) || typeof step.question === 'string')
    ) {
      ask = step;
    }

    let elic =
      requested?.elicitation && typeof requested.elicitation === 'object'
        ? requested.elicitation
        : null;
    if (!elic && oneofCase === 'elicitation') elic = oneofVal;

    let detail = 'Waiting for user input';
    if (perm) {
      const res =
        perm.resource && typeof perm.resource === 'object' ? perm.resource : {};
      const action =
        res.action ||
        perm.actionDescription ||
        perm.action_description ||
        perm.toolName ||
        'command';
      const target = res.target || perm.reason || perm.command || '';
      detail = `Permission required for ${action}: ${target}`.replace(/[:\s]+$/, '');
    } else if (ask) {
      const questions = Array.isArray(ask.questions) ? ask.questions : [];
      let qText = '';
      if (questions.length > 0 && questions[0] && typeof questions[0] === 'object') {
        qText = String(questions[0].question || '');
      } else if (questions.length > 0 && typeof questions[0] === 'string') {
        qText = questions[0];
      } else if (ask.question) {
        qText = String(ask.question);
      }
      detail = `Awaiting answer: ${qText || 'Question'}`;
    } else if (elic) {
      const msg =
        elic.message || elic.url || elic.serverName || elic.server_name || '';
      detail = `Input required: ${msg}`.replace(/[:\s]+$/, '');
    }

    if (detail === 'Waiting for user input') {
      const meta = step.metadata && typeof step.metadata === 'object' ? step.metadata : {};
      const pr = getStepOneofPayload(step, 'plannerResponse', 'planner_response');
      const prCalls = pr?.toolCalls || pr?.tool_calls;
      const prCall = Array.isArray(prCalls) && prCalls.length > 0 ? prCalls[0] : null;
      const toolCall =
        meta.modifiedToolCall ||
        meta.modified_tool_call ||
        meta.toolCall ||
        meta.tool_call ||
        step.toolCall ||
        step.tool_call ||
        prCall ||
        {};
      const stepType = typeof step.type === 'string' ? step.type : '';
      const typeFallback = stepType.startsWith('CORTEX_STEP_TYPE_')
        ? stepType.slice('CORTEX_STEP_TYPE_'.length).toLowerCase()
        : '';
      const toolName = String(toolCall?.name || typeFallback || '');
      const argsRaw =
        toolCall?.argumentsJson || toolCall?.arguments_json || toolCall?.args;
      let argsObj = {};
      if (typeof argsRaw === 'string' && argsRaw) {
        try {
          const parsed = JSON.parse(argsRaw);
          if (parsed && typeof parsed === 'object') argsObj = parsed;
        } catch {
          // Ignore malformed tool argument JSON and fall back to step payload fields
        }
      } else if (argsRaw && typeof argsRaw === 'object') {
        argsObj = argsRaw;
      }
      if (toolName === 'ask_question' || toolName === 'askQuestion') {
        const qList = Array.isArray(argsObj.questions) ? argsObj.questions : [];
        let qText = '';
        if (qList.length > 0 && qList[0] && typeof qList[0] === 'object') {
          qText = String(qList[0].question || '');
        } else if (qList.length > 0 && typeof qList[0] === 'string') {
          qText = qList[0];
        } else if (argsObj.question) {
          qText = String(argsObj.question);
        }
        detail = `Awaiting answer: ${qText || 'Question'}`;
      } else {
        const runCmd = getStepOneofPayload(step, 'runCommand', 'run_command') || {};
        const toolSummary = argsObj.toolSummary || argsObj.tool_summary;
        const cmd =
          argsObj.CommandLine ||
          argsObj.command_line ||
          runCmd.commandLine ||
          runCmd.command_line;
        if (toolSummary) {
          detail = `Permission required for ${toolSummary}`;
        } else if (cmd) {
          detail = `Permission required for command: ${cmd}`;
        } else if (toolName && toolName !== 'unspecified' && toolName !== 'generic') {
          detail = `Permission required for ${toolName}`;
        }
      }
    }
    return detail;
  }

  // Extracts blocked interaction detail and earliest waiting stepIndex from summary.waitingSteps
  function extractSummaryWaitingInfo(summary) {
    const waitingSteps = summary?.waitingSteps || summary?.waiting_steps;
    if (!Array.isArray(waitingSteps) || waitingSteps.length === 0) return null;
    let bestEntry = null;
    let bestIdx = null;
    for (const entry of waitingSteps) {
      if (!entry || typeof entry !== 'object') continue;
      const rawIdx = entry.stepIndex ?? entry.step_index ?? entry.index ?? 0;
      const idx = Number.isFinite(Number(rawIdx)) ? Math.trunc(Number(rawIdx)) : 0;
      if (bestIdx === null || idx < bestIdx) {
        bestIdx = idx;
        bestEntry = entry;
      }
    }
    const stepObj =
      bestEntry?.step && typeof bestEntry.step === 'object'
        ? bestEntry.step
        : bestEntry;
    const parsed =
      stepObj && typeof stepObj === 'object'
        ? parseStepInteraction(stepObj, false)
        : null;
    const detail = parsed || 'Waiting for user input';
    return {
      detail,
      stepIndex: bestIdx ?? 0,
      generic: detail === 'Waiting for user input',
    };
  }

  function extractSubagentsAndKills(steps) {
    const subagents = new Map();
    const invokeIndices = new Map();
    const killedIds = new Set();
    let lastKillAllIdx = -1;

    for (let idx = 0; idx < steps.length; idx++) {
      const step = steps[idx];
      if (!step || typeof step !== 'object') continue;
      let inv = getStepOneofPayload(step, 'invokeSubagent', 'invoke_subagent');
      if (!inv && INVOKE_SUBAGENT_STEP_TYPES.has(step.type)) inv = step;
      if (inv) {
        const specs = Array.isArray(inv.subagents) ? inv.subagents : [];
        const results = Array.isArray(inv.results) ? inv.results : [];
        for (let rIdx = 0; rIdx < results.length; rIdx++) {
          const res = results[rIdx];
          const childCid = res?.conversationId || res?.conversation_id;
          if (!childCid || typeof childCid !== 'string') continue;
          const spec = specs[rIdx];
          const role = String(
            spec?.role || spec?.typeName || spec?.type_name || ''
          );
          subagents.set(childCid, role || 'Subagent');
          invokeIndices.set(childCid, idx);
          killedIds.delete(childCid);
        }
        const legacyCid = inv.conversationId || inv.conversation_id;
        if (typeof legacyCid === 'string' && legacyCid && !subagents.has(legacyCid)) {
          const legacyRole = String(inv.subagentName || inv.subagent_name || '');
          subagents.set(legacyCid, legacyRole || 'Subagent');
          invokeIndices.set(legacyCid, idx);
          killedIds.delete(legacyCid);
        }
      }

      const toolCalls = [];
      const pr = getStepOneofPayload(step, 'plannerResponse', 'planner_response');
      const prCalls = pr?.toolCalls || pr?.tool_calls;
      if (Array.isArray(prCalls)) toolCalls.push(...prCalls);
      const meta = step.metadata && typeof step.metadata === 'object' ? step.metadata : {};
      const metaTc = meta.toolCall || meta.tool_call || step.toolCall;
      if (metaTc && typeof metaTc === 'object') toolCalls.push(metaTc);

      for (const tc of toolCalls) {
        if (!tc || tc.name !== 'manage_subagents') continue;
        const argsRaw = tc.argumentsJson || tc.arguments_json || '{}';
        let args = null;
        try {
          args = typeof argsRaw === 'string' ? JSON.parse(argsRaw) : argsRaw;
        } catch {
          continue;
        }
        const action = String(args?.Action || args?.action || '').toLowerCase();
        if (action === 'kill_all') {
          lastKillAllIdx = idx;
          for (const k of subagents.keys()) killedIds.add(k);
        } else if (action === 'kill') {
          const cids = args?.ConversationIds || args?.conversation_ids;
          if (Array.isArray(cids)) {
            for (const kid of cids) {
              if (typeof kid === 'string' && kid) killedIds.add(kid);
            }
          }
        }
      }
    }
    return { subagents, invokeIndices, killedIds, lastKillAllIdx };
  }

  function plannerHasToolCalls(step) {
    const pr = getStepOneofPayload(step, 'plannerResponse', 'planner_response');
    const tcs = pr?.toolCalls || pr?.tool_calls;
    return Array.isArray(tcs) && tcs.length > 0;
  }

  function stepSpawnsSubagent(step) {
    if (!step || typeof step !== 'object') return false;
    if (INVOKE_SUBAGENT_STEP_TYPES.has(step.type)) return true;
    if (getStepOneofPayload(step, 'invokeSubagent', 'invoke_subagent')) return true;
    const pr = getStepOneofPayload(step, 'plannerResponse', 'planner_response');
    const tcs = pr?.toolCalls || pr?.tool_calls;
    if (Array.isArray(tcs)) {
      for (const tc of tcs) {
        if (tc && (tc.name === 'invoke_subagent' || tc.name === 'swarm')) return true;
      }
    }
    const meta = step.metadata && typeof step.metadata === 'object' ? step.metadata : {};
    const mtc = meta.toolCall || meta.tool_call || step.toolCall;
    return Boolean(mtc && (mtc.name === 'invoke_subagent' || mtc.name === 'swarm'));
  }

  // Evaluates trajectory steps for trailing WAITING blocks and coordinator subagent synthesis
  function evaluateTrajectoryStatus(
    trajPayload,
    extraActiveSubagents,
    silentSubagents,
    isWaitingChat
  ) {
    if (!trajPayload || typeof trajPayload !== 'object') {
      return { state: 'unknown', detail: '' };
    }
    const rawStatus = trajPayload.status ?? '';
    const traj =
      trajPayload.trajectory && typeof trajPayload.trajectory === 'object'
        ? trajPayload.trajectory
        : trajPayload;
    const meta = traj.metadata && typeof traj.metadata === 'object' ? traj.metadata : {};
    const steps = Array.isArray(traj.steps) ? traj.steps : [];
    const termReason =
      trajPayload.latestExecutorTerminationReason ??
      trajPayload.latest_executor_termination_reason ??
      traj.latestExecutorTerminationReason ??
      traj.latest_executor_termination_reason ??
      meta.latestExecutorTerminationReason ??
      meta.latest_executor_termination_reason ??
      '';

    // Check trailing WAITING steps before RUNNING or IDLE; stop only at USER_INPUT or a
    // concluding no-tool-call DONE PLANNER_RESPONSE so parallel DONE tools cannot mask WAITING
    for (let i = steps.length - 1; i >= 0; i--) {
      const step = steps[i];
      if (!step || typeof step !== 'object') continue;
      const waitingDetail = parseStepInteraction(step, true);
      if (waitingDetail) return { state: 'blocked', detail: waitingDetail };
      if (USER_INPUT_STEP_TYPES.has(step.type)) break;
      if (
        PLANNER_STEP_TYPES.has(step.type) &&
        DONE_STEP_STATUSES.has(step.status) &&
        !plannerHasToolCalls(step)
      ) {
        break;
      }
    }

    if (isActiveRunStatus(rawStatus)) {
      return { state: 'running', detail: 'Agent turn running' };
    }
    if (ERROR_TERM_REASONS.has(termReason)) {
      return { state: 'error', detail: 'Execution stopped due to error' };
    }
    if (CANCELED_TERM_REASONS.has(termReason)) {
      return { state: 'canceled', detail: 'Canceled by user' };
    }

    let lastUserInputIdx = -1;
    for (let i = 0; i < steps.length; i++) {
      if (steps[i] && USER_INPUT_STEP_TYPES.has(steps[i].type)) {
        lastUserInputIdx = i;
      }
    }

    const { subagents, invokeIndices, killedIds, lastKillAllIdx } =
      extractSubagentsAndKills(steps);
    const activeSubagents = new Map();
    for (const [cid, role] of subagents.entries()) {
      if (!killedIds.has(cid)) activeSubagents.set(cid, role);
    }
    const killAllInTurn = lastKillAllIdx > lastUserInputIdx;
    if (extraActiveSubagents && !killAllInTurn) {
      for (const [cid, role] of extraActiveSubagents.entries()) {
        if (!killedIds.has(cid) && !activeSubagents.has(cid)) {
          activeSubagents.set(cid, role || 'Subagent');
        }
      }
    }

    if (activeSubagents.size > 0) {
      const lastReportIdx = new Map();
      for (let i = 0; i < steps.length; i++) {
        const step = steps[i];
        if (!step || !SYSTEM_MSG_STEP_TYPES.has(step.type)) continue;
        const sysMsg =
          getStepOneofPayload(step, 'systemMessage', 'system_message') || {};
        const msgText = String(sysMsg.message || '');
        const agentMsg = sysMsg.agentMessage || sysMsg.agent_message || {};
        const sender = typeof agentMsg.sender === 'string' ? agentMsg.sender : '';
        for (const cid of activeSubagents.keys()) {
          if (
            sender === cid ||
            msgText.includes(`sender=${cid}`) ||
            msgText.includes(cid)
          ) {
            lastReportIdx.set(cid, i);
          }
        }
      }

      const silentIds = silentSubagents || new Set();
      let turnSpawned = false;
      for (let i = lastUserInputIdx + 1; i < steps.length; i++) {
        if (stepSpawnsSubagent(steps[i])) {
          turnSpawned = true;
          break;
        }
      }
      for (const cid of activeSubagents.keys()) {
        if (silentIds.has(cid)) continue;
        let invIdx = -1;
        if (invokeIndices.has(cid)) {
          invIdx = invokeIndices.get(cid);
        } else if (turnSpawned) {
          invIdx = lastUserInputIdx + 1;
        } else {
          continue;
        }
        if (invIdx > lastUserInputIdx) {
          const repIdx = lastReportIdx.has(cid) ? lastReportIdx.get(cid) : -1;
          if (repIdx <= invIdx) {
            return { state: 'running', detail: 'Waiting for subagent reports' };
          }
        }
      }

      let latestReport = -1;
      for (const repIdx of lastReportIdx.values()) {
        if (repIdx > latestReport) latestReport = repIdx;
      }
      if (latestReport > lastUserInputIdx) {
        let hasSynthesis = false;
        let terminatedAfterReport = false;
        for (let i = steps.length - 1; i > latestReport; i--) {
          const step = steps[i];
          if (!step || typeof step !== 'object') continue;
          if (TERMINAL_STEP_STATUSES.has(step.status)) {
            terminatedAfterReport = true;
            break;
          }
          if (
            PLANNER_STEP_TYPES.has(step.type) &&
            DONE_STEP_STATUSES.has(step.status) &&
            !plannerHasToolCalls(step)
          ) {
            hasSynthesis = true;
            break;
          }
        }
        if (!hasSynthesis && !terminatedAfterReport) {
          return { state: 'running', detail: 'Synthesizing subagent findings' };
        }
      }
    }

    for (let i = steps.length - 1; i > lastUserInputIdx; i--) {
      const step = steps[i];
      if (!step || typeof step !== 'object') continue;
      if (TRANSPARENT_TAIL_STEP_TYPES.has(step.type)) continue;
      if (TERMINAL_STEP_STATUSES.has(step.status)) {
        return { state: 'canceled', detail: 'Canceled' };
      }
      if (
        PLANNER_STEP_TYPES.has(step.type) &&
        DONE_STEP_STATUSES.has(step.status) &&
        !plannerHasToolCalls(step)
      ) {
        return { state: 'idle', detail: 'Complete' };
      }
      if (isWaitingChat) return { state: 'idle', detail: 'Scheduled' };
      return { state: 'running', detail: 'Agent turn in progress' };
    }
    return { state: 'idle', detail: 'Idle' };
  }

  // Maps each parent conversation ID to its non-killed child subagents and ongoing battle arms
  function buildDescendantIndex(summariesMap) {
    const childrenByParent = new Map();
    const seenEdges = new Set();
    if (!summariesMap || typeof summariesMap !== 'object') return childrenByParent;

    const addEdge = (parentCid, childCid, label, isBattleArm) => {
      const edgeKey = `${parentCid}->${childCid}`;
      let list = childrenByParent.get(parentCid);
      if (seenEdges.has(edgeKey)) {
        if (isBattleArm && list) {
          const prev = list.find((e) => e.cid === childCid);
          if (prev) prev.isBattleArm = true;
        }
        return;
      }
      seenEdges.add(edgeKey);
      if (!list) {
        list = [];
        childrenByParent.set(parentCid, list);
      }
      list.push({
        cid: childCid,
        label: label || 'Subagent',
        isBattleArm: Boolean(isBattleArm),
      });
    };

    for (const [childCid, childSummary] of Object.entries(summariesMap)) {
      if (!childSummary || childSummary.killed) continue;
      const parentCid = childSummary.trajectoryMetadata?.parentConversationId || '';
      if (!parentCid || typeof parentCid !== 'string') continue;
      const label = String(
        childSummary.annotations?.title || childSummary.summary || 'Subagent'
      ).trim();
      const isFork = Boolean(
        childSummary.isBattleModeFork ||
          childSummary.trajectoryMetadata?.isBattleModeFork
      );
      addEdge(parentCid, childCid, label || 'Subagent', isFork);
    }

    for (const [parentCid, parentSummary] of Object.entries(summariesMap)) {
      if (!parentSummary) continue;
      const battle =
        parentSummary.battleModeInfo || parentSummary.battle_mode_info;
      if (!battle || typeof battle !== 'object') continue;
      const endType = battle.endType ?? battle.end_type ?? 0;
      const winner =
        battle.winningConversationId || battle.winning_conversation_id;
      if (!ONGOING_BATTLE_END_TYPES.has(endType) || winner) continue;
      const armIds =
        battle.childrenConversationIds || battle.children_conversation_ids;
      const eliminated = Array.isArray(battle.eliminated) ? battle.eliminated : [];
      if (!Array.isArray(armIds)) continue;
      for (let idx = 0; idx < armIds.length; idx++) {
        const armCid = armIds[idx];
        if (!armCid || typeof armCid !== 'string') continue;
        if (eliminated[idx]) continue;
        if (summariesMap[armCid]?.killed) continue;
        addEdge(parentCid, armCid, `Battle arm ${idx + 1}`, true);
      }
    }
    return childrenByParent;
  }

  function buildSummaryStatusSig(cid, summary) {
    if (!summary) return `${cid}:none`;
    const ws = summary.waitingSteps || summary.waiting_steps;
    const wsLen = Array.isArray(ws) ? ws.length : 0;
    const rawTitle = summary.annotations?.title || summary.summary || '';
    const waitFlag = isWaitingTitle(rawTitle) ? 1 : 0;
    const term =
      summary.latestExecutorTerminationReason ??
      summary.latest_executor_termination_reason ??
      '';
    return (
      `${cid}:${summary.stepCount ?? ''}:${parseTimestampMs(summary.lastModifiedTime)}:` +
      `${summary.status ?? ''}:${summary.notFullyIdle ? 1 : 0}:` +
      `${summary.hasRunningSubagents ? 1 : 0}:${wsLen}:${term}:` +
      `${summary.killed ? 1 : 0}:${summary.interrupted ? 1 : 0}:${waitFlag}`
    );
  }

  function collectDescendantTreeIds(cascadeId, childIndex) {
    const tree = [];
    const seen = new Set([cascadeId]);
    const queue = (childIndex.get(cascadeId) || []).map((c) => c.cid);
    while (queue.length > 0) {
      const nxt = queue.shift();
      if (seen.has(nxt)) continue;
      seen.add(nxt);
      tree.push(nxt);
      for (const gc of childIndex.get(nxt) || []) {
        if (!seen.has(gc.cid)) queue.push(gc.cid);
      }
    }
    return tree;
  }

  function mergeSummaryIntoTraj(summary, trajPayload) {
    const merged = { ...(trajPayload || {}) };
    if (!merged.status && summary?.status !== undefined) {
      merged.status = summary.status;
    }
    const term =
      summary?.latestExecutorTerminationReason ??
      summary?.latest_executor_termination_reason;
    if (
      term !== undefined &&
      !merged.latestExecutorTerminationReason &&
      !merged.latest_executor_termination_reason
    ) {
      merged.latestExecutorTerminationReason = term;
    }
    return merged;
  }

  async function fetchSingleTrajectoryRpc(cascadeId, summary) {
    if (!cascadeId || typeof window.fetch !== 'function') return null;
    const sig = buildSummaryStatusSig(cascadeId, summary);
    const cached = singleTrajCache.get(cascadeId);
    if (cached && cached.sig === sig) {
      singleTrajCache.delete(cascadeId);
      singleTrajCache.set(cascadeId, cached);
      return cached.payload;
    }
    const headers = {
      'Content-Type': 'application/json',
      'x-codeium-csrf-token': getCsrfToken(),
    };
    try {
      const resp = await window.fetch(
        '/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory',
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            cascadeId,
            trajectoryVerbosity: 'CLIENT_TRAJECTORY_VERBOSITY_PROD_UI',
          }),
        }
      );
      if (!resp || !resp.ok) return null;
      const data = await resp.json();
      if (!data || typeof data !== 'object') return null;
      const steps = data.trajectory?.steps;
      const total = Number(data.numTotalSteps ?? data.num_total_steps ?? 0);
      // Fetch remaining pages when GetCascadeTrajectory truncates at GRPCMaxMessageSize
      if (Array.isArray(steps)) {
        for (let page = 0; page < 8 && steps.length > 0 && steps.length < total; page++) {
          try {
            const tailResp = await window.fetch(
              '/exa.language_server_pb.LanguageServerService/GetCascadeTrajectorySteps',
              {
                method: 'POST',
                headers,
                body: JSON.stringify({
                  cascadeId,
                  stepOffset: steps.length,
                  trajectoryVerbosity: 'CLIENT_TRAJECTORY_VERBOSITY_PROD_UI',
                }),
              }
            );
            if (!tailResp || !tailResp.ok) break;
            const tailData = await tailResp.json();
            if (!Array.isArray(tailData?.steps) || tailData.steps.length === 0) {
              break;
            }
            steps.push(...tailData.steps);
          } catch {
            // Proceed with fetched trajectory prefix when tail page fails
            break;
          }
        }
      }
      if (singleTrajCache.size >= SINGLE_TRAJ_CACHE_MAX_ENTRIES) {
        const oldestKey = singleTrajCache.keys().next().value;
        if (oldestKey !== undefined) singleTrajCache.delete(oldestKey);
      }
      singleTrajCache.set(cascadeId, { sig, payload: data });
      return data;
    } catch {
      return null;
    }
  }

  function getOrCreateTrajStatusEntry(cascadeId) {
    let entry = trajectoryStatusCache.get(cascadeId);
    if (entry) {
      trajectoryStatusCache.delete(cascadeId);
      trajectoryStatusCache.set(cascadeId, entry);
      return entry;
    }
    if (trajectoryStatusCache.size >= TRAJ_STATUS_CACHE_MAX_ENTRIES) {
      for (const [oldId, oldEntry] of trajectoryStatusCache.entries()) {
        if (!oldEntry.inFlight) {
          trajectoryStatusCache.delete(oldId);
          break;
        }
      }
    }
    entry = {
      treeSig: '',
      attemptedSig: '',
      inFlight: false,
      state: '',
      detail: '',
      blockedSig: '',
      fetchedAt: 0,
    };
    trajectoryStatusCache.set(cascadeId, entry);
    return entry;
  }

  function isSilentSubagentSummary(sSum, nowMs) {
    if (!sSum || Object.keys(sSum).length === 0) return false;
    const term =
      sSum.latestExecutorTerminationReason ??
      sSum.latest_executor_termination_reason;
    if (ERROR_TERM_REASONS.has(term) || CANCELED_TERM_REASONS.has(term)) {
      return true;
    }
    const modMs =
      parseTimestampMs(sSum.lastModifiedTime) ||
      parseTimestampMs(sSum.createdTime);
    return modMs > 0 && nowMs - modMs > SILENT_SUBAGENT_GRACE_MS;
  }

  function shouldInspectTrajectory(cascadeId, summary, childIndex) {
    if (!cascadeId || !summary || typeof window.fetch !== 'function') return false;
    if (
      isActiveRunStatus(summary.status) ||
      Boolean(summary.notFullyIdle) ||
      Boolean(summary.hasRunningSubagents)
    ) {
      return true;
    }
    if ((childIndex.get(cascadeId) || []).length > 0) return true;
    const prev = trajectoryStatusCache.get(cascadeId);
    if (prev && (prev.state === 'running' || prev.state === 'blocked')) return true;
    const modMs =
      parseTimestampMs(summary.lastModifiedTime) ||
      parseTimestampMs(summary.createdTime);
    if (modMs > 0 && Date.now() - modMs <= RECENT_TRAJ_CHECK_WINDOW_MS) {
      return true;
    }
    return false;
  }

  async function ensureTrajectoryStatusLoaded(
    cascadeId,
    summary,
    treeSig,
    summariesMap,
    childIndex
  ) {
    if (disposed || !shouldInspectTrajectory(cascadeId, summary, childIndex)) return;
    const entry = getOrCreateTrajStatusEntry(cascadeId);
    const now = Date.now();
    const graceStale =
      entry.treeSig === treeSig &&
      entry.detail === 'Waiting for subagent reports' &&
      now - entry.fetchedAt > SILENT_SUBAGENT_GRACE_MS;
    if (!graceStale && (entry.treeSig === treeSig || entry.attemptedSig === treeSig)) {
      return;
    }
    if (entry.inFlight) return;
    entry.inFlight = true;
    entry.attemptedSig = treeSig;

    try {
      const rootTraj = await fetchSingleTrajectoryRpc(cascadeId, summary);
      if (disposed || !rootTraj) return;

      const latestStoreMap =
        findReduxStore()?.getState()?.trajectorySummaries?.summaries ||
        summariesMap ||
        {};
      const curSummary = latestStoreMap[cascadeId] || summary;
      const curChildIndex = buildDescendantIndex(latestStoreMap);
      const curTree = collectDescendantTreeIds(cascadeId, curChildIndex);
      const curTreeSig = [
        buildSummaryStatusSig(cascadeId, curSummary),
        ...curTree.map((d) => buildSummaryStatusSig(d, latestStoreMap[d])),
      ].join('|');

      const rawTitle = curSummary?.annotations?.title || curSummary?.summary || '';
      const isWaitingChat = isWaitingTitle(rawTitle);
      const rootSteps = Array.isArray(rootTraj.trajectory?.steps)
        ? rootTraj.trajectory.steps
        : Array.isArray(rootTraj.steps)
          ? rootTraj.steps
          : [];
      const rootSubInfo = extractSubagentsAndKills(rootSteps);
      let rootLastUi = -1;
      for (let i = 0; i < rootSteps.length; i++) {
        if (rootSteps[i] && USER_INPUT_STEP_TYPES.has(rootSteps[i].type)) {
          rootLastUi = i;
        }
      }
      const rootKillAllActive = rootSubInfo.lastKillAllIdx > rootLastUi;

      let resolvedState = 'idle';
      let resolvedDetail = '';
      let resolvedBlockedSig = '';

      // Trailing WAITING on self trajectory takes precedence over RUNNING and IDLE
      const earlySelf = evaluateTrajectoryStatus(
        mergeSummaryIntoTraj(curSummary, rootTraj),
        null,
        null,
        isWaitingChat
      );
      const selfWaitEarly = extractSummaryWaitingInfo(curSummary);
      if (earlySelf.state === 'blocked' || selfWaitEarly) {
        const selfIdx = selfWaitEarly ? selfWaitEarly.stepIndex : rootSteps.length;
        const selfDetail =
          earlySelf.state === 'blocked' &&
          earlySelf.detail &&
          earlySelf.detail !== 'Waiting for user input'
            ? earlySelf.detail
            : (selfWaitEarly?.detail || earlySelf.detail || 'Waiting for user input');
        resolvedState = 'blocked';
        resolvedDetail = selfDetail;
        resolvedBlockedSig = `self:${selfIdx}:${selfDetail}`;
      } else {
        // BFS walk across non-killed descendants and ongoing battle arms
        const directActiveChildren = new Map();
        const silentChildren = new Set();
        let anyDescRunning = false;
        let blockedDescMsg = '';
        let blockedDescSig = '';
        const visited = new Set([cascadeId]);
        const bfsQueue = (curChildIndex.get(cascadeId) || []).map((c) => ({
          cid: c.cid,
          label: c.label,
          isBattleArm: Boolean(c.isBattleArm),
          isDirect: true,
          pKilled: rootSubInfo.killedIds,
          pKillAll: rootKillAllActive,
          pRoles: rootSubInfo.subagents,
        }));

        while (bfsQueue.length > 0) {
          const item = bfsQueue.shift();
          if (visited.has(item.cid)) continue;
          visited.add(item.cid);
          if (
            item.pKilled.has(item.cid) ||
            (item.pKillAll && !item.pRoles.has(item.cid))
          ) {
            continue;
          }
          const dSummary = latestStoreMap[item.cid] || {};
          if (dSummary.killed) continue;
          const role = item.pRoles.get(item.cid) || item.label || 'Subagent';
          const dWait = extractSummaryWaitingInfo(dSummary);
          if (dWait && !dWait.generic) {
            blockedDescMsg = `[${role}] ${dWait.detail}`;
            blockedDescSig = `desc:${item.cid}:${dWait.stepIndex}:${blockedDescMsg}`;
            break;
          }
          const dTraj = await fetchSingleTrajectoryRpc(item.cid, dSummary);
          if (disposed) return;
          const dSteps = Array.isArray(dTraj?.trajectory?.steps)
            ? dTraj.trajectory.steps
            : Array.isArray(dTraj?.steps)
              ? dTraj.steps
              : [];
          const dSubInfo = extractSubagentsAndKills(dSteps);
          let dLastUi = -1;
          for (let i = 0; i < dSteps.length; i++) {
            if (dSteps[i] && USER_INPUT_STEP_TYPES.has(dSteps[i].type)) {
              dLastUi = i;
            }
          }
          let dTrajState = '';
          if (dTraj) {
            const dEval = evaluateTrajectoryStatus(
              mergeSummaryIntoTraj(dSummary, dTraj),
              null,
              null,
              false
            );
            dTrajState = dEval.state;
            if (dEval.state === 'blocked') {
              const descIdx = dWait ? dWait.stepIndex : dSteps.length;
              blockedDescMsg = `[${role}] ${dEval.detail}`;
              blockedDescSig = `desc:${item.cid}:${descIdx}:${blockedDescMsg}`;
              break;
            }
          }
          if (dWait) {
            blockedDescMsg = `[${role}] ${dWait.detail}`;
            blockedDescSig = `desc:${item.cid}:${dWait.stepIndex}:${blockedDescMsg}`;
            break;
          }
          const dRunning =
            isActiveRunStatus(dSummary.status) ||
            Boolean(dSummary.notFullyIdle) ||
            Boolean(dSummary.hasRunningSubagents) ||
            dTrajState === 'running';
          if (dRunning) anyDescRunning = true;

          // Battle Mode arms never send SYSTEM_MESSAGE reports, so skip synthesis tracking
          const isBattleArm = Boolean(
            item.isBattleArm ||
              dSummary.isBattleModeFork ||
              dSummary.trajectoryMetadata?.isBattleModeFork
          );
          if (item.isDirect && !isBattleArm) {
            directActiveChildren.set(item.cid, role);
            if (!dRunning && isSilentSubagentSummary(dSummary, Date.now())) {
              silentChildren.add(item.cid);
            }
          }

          for (const gc of curChildIndex.get(item.cid) || []) {
            if (!visited.has(gc.cid)) {
              bfsQueue.push({
                cid: gc.cid,
                label: gc.label,
                isBattleArm: Boolean(gc.isBattleArm),
                isDirect: false,
                pKilled: dSubInfo.killedIds,
                pKillAll: dSubInfo.lastKillAllIdx > dLastUi,
                pRoles: dSubInfo.subagents,
              });
            }
          }
        }

        const evalNowMs = Date.now();
        for (const sCid of rootSubInfo.subagents.keys()) {
          if (silentChildren.has(sCid) || !latestStoreMap[sCid]) continue;
          const sSum = latestStoreMap[sCid];
          if (sSum.killed) {
            silentChildren.add(sCid);
            continue;
          }
          if (
            !isActiveRunStatus(sSum.status) &&
            !sSum.notFullyIdle &&
            isSilentSubagentSummary(sSum, evalNowMs)
          ) {
            silentChildren.add(sCid);
          }
        }

        if (blockedDescMsg) {
          resolvedState = 'blocked';
          resolvedDetail = blockedDescMsg;
          resolvedBlockedSig = blockedDescSig;
        } else if (
          isActiveRunStatus(curSummary?.status) ||
          (!isWaitingChat && Boolean(curSummary?.notFullyIdle)) ||
          anyDescRunning
        ) {
          resolvedState = 'running';
          resolvedDetail = 'Agent turn running';
        } else if (!curSummary?.interrupted) {
          const fullEval = evaluateTrajectoryStatus(
            mergeSummaryIntoTraj(curSummary, rootTraj),
            directActiveChildren,
            silentChildren,
            isWaitingChat
          );
          const lmtMs = parseTimestampMs(curSummary?.lastModifiedTime);
          const staleIdle =
            lmtMs > 0 && Date.now() - lmtMs > STALE_IDLE_TURN_MS;
          if (fullEval.state === 'running' && !staleIdle) {
            resolvedState = 'running';
            resolvedDetail = fullEval.detail;
          } else {
            resolvedState = 'idle';
            resolvedDetail = fullEval.detail;
          }
        }
      }

      const prevSig = `${entry.state}:${entry.detail}:${entry.blockedSig}`;
      entry.treeSig = curTreeSig;
      entry.attemptedSig = curTreeSig;
      entry.state = resolvedState;
      entry.detail = resolvedDetail;
      entry.blockedSig = resolvedBlockedSig;
      entry.fetchedAt = Date.now();
      if (`${resolvedState}:${resolvedDetail}:${resolvedBlockedSig}` !== prevSig) {
        scheduleSync();
      }
    } finally {
      entry.inFlight = false;
      if (!disposed) {
        const latestMap =
          findReduxStore()?.getState()?.trajectorySummaries?.summaries;
        const latestSummary = latestMap?.[cascadeId];
        if (latestMap && latestSummary && entry.treeSig) {
          const latestIdx = buildDescendantIndex(latestMap);
          const latestTree = collectDescendantTreeIds(cascadeId, latestIdx);
          const latestSig = [
            buildSummaryStatusSig(cascadeId, latestSummary),
            ...latestTree.map((d) => buildSummaryStatusSig(d, latestMap[d])),
          ].join('|');
          if (latestSig !== entry.attemptedSig) {
            void ensureTrajectoryStatusLoaded(
              cascadeId,
              latestSummary,
              latestSig,
              latestMap,
              latestIdx
            );
          }
        }
      }
    }
  }

  // Resolves unified bucket, blocked detail, and coordinator subagent synthesis state
  function resolveConversationStatus(
    cascadeId,
    summary,
    fallbackTitle,
    summariesMap,
    childIndex
  ) {
    const resolvedMap =
      summariesMap || findReduxStore()?.getState()?.trajectorySummaries?.summaries || {};
    let resolvedId = cascadeId || '';
    if (!resolvedId && summary && resolvedMap) {
      for (const [k, v] of Object.entries(resolvedMap)) {
        if (v === summary) {
          resolvedId = k;
          break;
        }
      }
    }
    const idxMap = childIndex || buildDescendantIndex(resolvedMap);
    const title =
      summary?.annotations?.title || summary?.summary || fallbackTitle || '';
    const marker = parseTitleMarker(title);
    const isWaitingChat = marker === OPEN_WAITING_MARKER;

    const treeIds = resolvedId ? collectDescendantTreeIds(resolvedId, idxMap) : [];
    const treeSig = resolvedId
      ? [
          buildSummaryStatusSig(resolvedId, summary),
          ...treeIds.map((d) => buildSummaryStatusSig(d, resolvedMap[d])),
        ].join('|')
      : '';
    if (resolvedId && summary) {
      void ensureTrajectoryStatusLoaded(
        resolvedId,
        summary,
        treeSig,
        resolvedMap,
        idxMap
      );
    }
    const cachedTraj = resolvedId ? trajectoryStatusCache.get(resolvedId) : null;
    const cachedMatches = Boolean(cachedTraj && cachedTraj.treeSig === treeSig);
    // Keep prior running/blocked trajectory state while an update RPC is in flight to avoid
    // 1-frame flicker into Needs Me (and premature latching) between step transitions
    const useCachedTraj = Boolean(
      cachedMatches ||
        (cachedTraj?.inFlight &&
          (cachedTraj.state === 'running' || cachedTraj.state === 'blocked'))
    );

    // Blocked check on self or active descendants takes precedence over running and waiting
    const selfWait = extractSummaryWaitingInfo(summary);
    if (selfWait) {
      const detail =
        selfWait.generic && cachedMatches && cachedTraj.state === 'blocked' && cachedTraj.detail
          ? cachedTraj.detail
          : selfWait.detail;
      return {
        bucket: 'needs-me',
        isBlocked: true,
        blockedDetail: detail,
        blockedSig: `self:${selfWait.stepIndex}:${detail}`,
        workingDetail: '',
      };
    }

    // Fall back to raw descendant summaries only before trajectory kill-filtering resolves
    let anyDescendantRunning = false;
    if (!cachedMatches) {
      for (const dCid of treeIds) {
        const dSummary = resolvedMap[dCid];
        if (!dSummary || dSummary.killed) continue;
        const dWait = extractSummaryWaitingInfo(dSummary);
        if (dWait) {
          const directEntry = (idxMap.get(resolvedId) || []).find((c) => c.cid === dCid);
          const role = String(
            directEntry?.label ||
              dSummary.annotations?.title ||
              dSummary.summary ||
              'Subagent'
          ).trim();
          const detail = `[${role || 'Subagent'}] ${dWait.detail}`;
          return {
            bucket: 'needs-me',
            isBlocked: true,
            blockedDetail: detail,
            blockedSig: `desc:${dCid}:${dWait.stepIndex}:${detail}`,
            workingDetail: '',
          };
        }
        if (
          isActiveRunStatus(dSummary.status) ||
          Boolean(dSummary.notFullyIdle) ||
          Boolean(dSummary.hasRunningSubagents)
        ) {
          anyDescendantRunning = true;
        }
      }
    }

    if (useCachedTraj && cachedTraj.state === 'blocked') {
      const detail = cachedTraj.detail || 'Waiting for user input';
      return {
        bucket: 'needs-me',
        isBlocked: true,
        blockedDetail: detail,
        blockedSig: cachedTraj.blockedSig || `traj:${summary?.stepCount ?? ''}:${detail}`,
        workingDetail: '',
      };
    }

    // Working check: active turn, non-⏳ background task, active child, or pending synthesis
    const selfRunning =
      isActiveRunStatus(summary?.status) ||
      (!isWaitingChat && Boolean(summary?.notFullyIdle)) ||
      Boolean(summary?.hasRunningSubagents);
    const lmtMs = parseTimestampMs(summary?.lastModifiedTime);
    const staleIdle = lmtMs > 0 && Date.now() - lmtMs > STALE_IDLE_TURN_MS;
    const trajRunning =
      useCachedTraj &&
      cachedTraj.state === 'running' &&
      (cachedTraj.detail === 'Agent turn running' || (!staleIdle && !summary?.interrupted));
    if (selfRunning || anyDescendantRunning || trajRunning) {
      return {
        bucket: 'working',
        isBlocked: false,
        blockedDetail: '',
        blockedSig: '',
        workingDetail: trajRunning ? cachedTraj.detail : '',
      };
    }

    // Cron or external waiting chat without blocked steps or active turns
    if (isWaitingChat) {
      return {
        bucket: 'waiting',
        isBlocked: false,
        blockedDetail: '',
        blockedSig: '',
        workingDetail: '',
      };
    }

    // Idle open chat awaiting user review or next prompt
    return {
      bucket: 'needs-me',
      isBlocked: false,
      blockedDetail: '',
      blockedSig: '',
      workingDetail: '',
    };
  }

  // Classifies a conversation into one of the 3 state buckets
  function classifyChatBucket(
    summary,
    fallbackTitle,
    cascadeId,
    summariesMap,
    childIndex
  ) {
    return resolveConversationStatus(
      cascadeId,
      summary,
      fallbackTitle,
      summariesMap,
      childIndex
    ).bucket;
  }

  function emptyBreakdown() {
    return {
      totalEligible: 0,
      'needs-me': 0,
      working: 0,
      waiting: 0,
    };
  }

  function countBucket(stats, bucket) {
    stats.totalEligible++;
    if (bucket in stats) {
      stats[bucket]++;
    }
  }

  function hasLegacyClosedMarker(title) {
    return typeof title === 'string' && LEGACY_CLOSED_TITLE_RE.test(title);
  }

  // Looks up a project metadata object across Redux project slices
  function findProjectEntry(projectId, store) {
    if (!projectId || projectId === '__standalone__') return null;
    const state = (store || findReduxStore())?.getState();
    if (!state) return null;
    const projs = state?.projects?.projects;
    if (Array.isArray(projs)) {
      const m = projs.find((p) => p && p.id === projectId);
      if (m) return m;
    } else if (projs && typeof projs === 'object' && projs[projectId]) {
      return projs[projectId];
    }
    const byId = state?.projects?.byId?.[projectId] || state?.project?.projects?.[projectId];
    if (byId && typeof byId === 'object') return byId;
    const sidebarProjects = state?.sidebarProjects?.projects;
    if (Array.isArray(sidebarProjects)) {
      const m = sidebarProjects.find((p) => p && p.id === projectId);
      if (m) return m;
    }
    return null;
  }

  // Returns true when the project is explicitly marked archived in Redux
  function isArchivedProject(projectId, store) {
    const entry = findProjectEntry(projectId, store);
    return Boolean(entry && entry.archived === true);
  }

  function isEligibleTopLevelSummary(cascadeId, summary) {
    if (!summary) return false;
    const meta = summary.trajectoryMetadata;
    if (meta?.parentConversationId || meta?.isBattleModeFork) return false;
    if (summary.annotations?.archived) return false;
    // Chats created by CLI automation or internal tools do not belong in user inboxes
    if (meta?.sourceMetadata?.tool) return false;
    if (meta?.sourceMetadata?.hideFromConversationList && !managedHiddenIds.has(cascadeId)) {
      return false;
    }
    const rawTitle = summary?.annotations?.title || summary?.summary || '';
    // Pre-archive closed chats and chats of archived projects are never actionable
    if (hasLegacyClosedMarker(rawTitle)) return false;
    if (isArchivedProject(meta?.projectId)) return false;
    return true;
  }

  function classifySummaries(summariesMap, allowedBuckets) {
    const hiddenIds = new Set();
    let chosenHiddenCount = 0;
    const perProjectHidden = new Map();
    const statsSummary = emptyBreakdown();
    const childIndex = buildDescendantIndex(summariesMap);

    for (const [cascadeId, summary] of Object.entries(summariesMap || {})) {
      if (!isEligibleTopLevelSummary(cascadeId, summary)) continue;
      const bucket = classifyChatBucket(
        summary,
        '',
        cascadeId,
        summariesMap,
        childIndex
      );
      countBucket(statsSummary, bucket);
      if (allowedBuckets.has(bucket)) continue;
      hiddenIds.add(cascadeId);
      chosenHiddenCount++;
      const projectId = summary.trajectoryMetadata?.projectId || '__standalone__';
      perProjectHidden.set(projectId, (perProjectHidden.get(projectId) || 0) + 1);
    }

    return { hiddenIds, chosenHiddenCount, perProjectHidden, statsSummary };
  }

  function cloneSummaryWithHideFlag(summary, hide) {
    const summaryProto = Object.getPrototypeOf(summary);
    const nextSummary = Object.assign(Object.create(summaryProto), summary);

    const prevMeta = summary.trajectoryMetadata;
    const nextMeta = prevMeta
      ? Object.assign(Object.create(Object.getPrototypeOf(prevMeta)), prevMeta)
      : { $typeName: 'exa.cortex_pb.CortexTrajectoryMetadata' };

    const prevSource = prevMeta?.sourceMetadata;
    const nextSource = prevSource
      ? Object.assign(Object.create(Object.getPrototypeOf(prevSource)), prevSource)
      : { $typeName: 'exa.cortex_pb.CortexTrajectorySourceMetadata' };

    nextSource.hideFromConversationList = Boolean(hide);
    nextMeta.sourceMetadata = nextSource;
    nextSummary.trajectoryMetadata = nextMeta;
    return nextSummary;
  }

  function findReduxStore() {
    if (reduxStore) return reduxStore;

    const candidates = [
      document.querySelector('[data-testid="conversation-list-sidebar"]'),
      document.querySelector('[data-sidebar-scroll-container="true"]'),
      document.querySelector('button[aria-label="Display Options"]'),
      document.querySelector('[data-cascade-id]'),
      document.getElementById('root'),
      document.body?.firstElementChild,
    ].filter(Boolean);

    for (const el of candidates) {
      const fiberKey = Object.keys(el).find(
        (k) => k.startsWith('__reactFiber$') || k.startsWith('__reactContainer$')
      );
      if (!fiberKey) continue;

      const queue = [el[fiberKey]];
      if (el[fiberKey]?.stateNode?.current) {
        queue.push(el[fiberKey].stateNode.current);
      }
      const visited = new Set();
      let steps = 0;
      while (queue.length > 0 && steps < 250) {
        const fiber = queue.shift();
        if (!fiber || visited.has(fiber)) continue;
        visited.add(fiber);
        steps++;

        const directStore =
          fiber.memoizedProps?.store ||
          fiber.memoizedProps?.value?.store ||
          fiber.memoizedState?.memoizedState?.store;
        if (
          directStore &&
          typeof directStore.getState === 'function' &&
          directStore.getState()?.trajectorySummaries
        ) {
          hookReduxStore(directStore);
          return directStore;
        }

        let ctx = fiber.dependencies?.firstContext;
        let ctxSteps = 0;
        while (ctx && ctxSteps < 50) {
          ctxSteps++;
          const ctxStore = ctx.memoizedValue?.store;
          if (
            ctxStore &&
            typeof ctxStore.getState === 'function' &&
            ctxStore.getState()?.trajectorySummaries
          ) {
            hookReduxStore(ctxStore);
            return ctxStore;
          }
          ctx = ctx.next;
        }

        if (fiber.return && !visited.has(fiber.return)) {
          queue.push(fiber.return);
        }
        if (steps < 40) {
          if (fiber.child && !visited.has(fiber.child)) {
            queue.push(fiber.child);
          }
          if (fiber.sibling && !visited.has(fiber.sibling)) {
            queue.push(fiber.sibling);
          }
        }
      }
    }
    return null;
  }

  // True when every chime trigger in a stream batch (top-level turn end or a waiting step)
  // belongs to an archived chat: an archived chain piece only finishes the turn that handed
  // it off, and hearing that would expose the chain split
  function batchOnlyAlertsArchivedPieces(updates, prevSummaries) {
    let silenced = 0;
    for (const [id, s] of updates) {
      if (!s) continue;
      const prev = prevSummaries[id];
      // The middleware announces turn ends only for top-level chats
      const endsTurn =
        Boolean(prev?.notFullyIdle) &&
        !s.notFullyIdle &&
        !s.trajectoryMetadata?.parentConversationId;
      const ws = s.waitingSteps || s.waiting_steps;
      const asks = Array.isArray(ws) && ws.length > 0;
      if (!endsTurn && !asks) continue;
      if (!(s.annotations?.archived || prev?.annotations?.archived)) return false;
      silenced++;
    }
    return silenced > 0;
  }

  // playAgentSound fetches the chime file before playing it and gives up on a non-ok
  // response, so failing that fetch drops one chime without touching its 2s quiet window.
  // One shared gate serves every injection; window.__jetskiAgentSoundMuted opens and closes it
  // Gates sound chime fetches and rewrites exhausted model payloads on ConnectRPC requests
  const AGENT_SOUND_URL_RE = /\.(?:mp3|wav|ogg)(?:[?#]|$)/i;
  function installAgentSoundGate() {
    const innerFetch = window.fetch;
    if (typeof innerFetch !== 'function') return;
    const baseFetch = innerFetch.__jetskiOrigFetch || innerFetch;
    const gatedFetch = function (input, init) {
      const url = typeof input === 'string' ? input : input?.url || String(input || '');
      if (window.__jetskiAgentSoundMuted > 0 && AGENT_SOUND_URL_RE.test(url)) {
        return Promise.resolve({ ok: false, status: 204 });
      }
      let nextInit = init;
      if (
        !disposed &&
        reduxStore &&
        typeof init?.body === 'string' &&
        (url.includes('/SendUserCascadeMessage') || url.includes('/StartCascade'))
      ) {
        try {
          const body = JSON.parse(init.body);
          const planModel = body?.cascadeConfig?.plannerConfig?.planModel;
          if (planModel !== undefined && planModel !== null) {
            const catalog = buildModelCatalogIndex(reduxStore);
            const entry =
              typeof planModel === 'number'
                ? catalog.byEnum.get(planModel)
                : catalog.byId.get(String(planModel).toLowerCase());
            // Resolve per-conversation or per-project draft manual pin state before failover
            const rawCid =
              body.cascadeId ||
              reduxStore.getState()?.conversation?.cascadeId ||
              '';
            const pinKey = resolveDraftPinKey(reduxStore, rawCid);
            const pin = getManualPinEntry(pinKey);
            const isPinned = Boolean(
              entry?.enumVal && pin && pin.model === entry.enumVal
            );
            let replacement = null;
            let reason = '';
            if (entry && entry.quota < (isPinned ? EMERGENCY_PIN_FLOOR : QUOTA_HARD_FLOOR)) {
              const profile =
                resolveProjectProfileForConversation(reduxStore, body.cascadeId) ||
                'orchestrator';
              replacement = resolveAutopilotModelForProfile(catalog, profile);
              reason = `${Math.round(entry.quota * 100)}% quota`;
            } else if (!isPinned) {
              const shielded = rerouteWastefulWtuModelInUi(catalog, planModel, QUOTA_SOFT_FLOOR);
              if (shielded) {
                replacement = shielded;
                reason = 'WTU shield';
              }
            }
            // Promote draft manual pin to the new cascadeId on StartCascade and free the draft slot
            if (url.includes('/StartCascade') && pinKey.startsWith('__draft__:')) {
              if (body.cascadeId && pin) {
                setManualPin(body.cascadeId, replacement?.enumVal || pin.model, 1);
              }
              deleteManualPin(pinKey);
              autoSeededTargets.delete(pinKey);
            }
            if (replacement && replacement.enumVal && replacement.enumVal !== entry?.enumVal) {
              const nextPlanModel =
                typeof planModel === 'number'
                  ? replacement.enumVal
                  : replacement.enumName ||
                    (replacement.enumVal >= 1000
                      ? `MODEL_PLACEHOLDER_M${replacement.enumVal - 1000}`
                      : planModel);
              body.cascadeConfig.plannerConfig.planModel = nextPlanModel;
              nextInit = { ...init, body: JSON.stringify(body) };
              showAutopilotToast(
                entry?.label || String(planModel),
                replacement.label,
                reason
              );
            }
          }
        } catch {
          // Keep untouched request options on malformed payload
        }
      }
      return baseFetch.call(window, input, nextInit);
    };
    gatedFetch.__jetskiSoundGate = true;
    gatedFetch.__jetskiOrigFetch = baseFetch;
    window.fetch = gatedFetch;
  }

  // Clones a Protobuf-ES v2 message while preserving its descriptor prototype
  function cloneWithProto(source, overrides) {
    const proto = source && typeof source === 'object' ? Object.getPrototypeOf(source) : null;
    const base = proto && proto !== Object.prototype ? Object.create(proto) : {};
    return Object.assign(base, source || {}, overrides || {});
  }

  // Maps a server Model enum name or numeric value to codeium_common_pb.Model
  function resolveModelEnumValue(modelEnum) {
    if (typeof modelEnum === 'number' && Number.isFinite(modelEnum) && modelEnum > 0) {
      return modelEnum;
    }
    if (typeof modelEnum !== 'string' || !modelEnum) return 0;
    const placeholderMatch = /^MODEL_PLACEHOLDER_M(\d+)$/.exec(modelEnum);
    // Placeholder enums M<N> map to 1000 + N in codeium_common.proto
    if (placeholderMatch) {
      return 1000 + Number(placeholderMatch[1]);
    }
    return NAMED_MODEL_ENUM_VALUES[modelEnum] || 0;
  }

  // Formats an unknown modelId into a readable fallback label when displayName is masked
  function humanizeModelId(modelId) {
    const cleaned = String(modelId || '')
      .replace(/-(?:bon-arm|bon-le)$/i, '')
      .replace(/[-_]+/g, ' ')
      .trim();
    if (!cleaned) return String(modelId || 'Hidden Model');
    return cleaned
      .split(/\s+/)
      .map((w) =>
        w.length <= 3 && /\d/.test(w) ? w.toUpperCase() : w[0].toUpperCase() + w.slice(1)
      )
      .join(' ');
  }

  // Extracts reset timestamp in milliseconds from a Protobuf Timestamp or ISO string
  function parseQuotaResetMs(resetTime) {
    if (typeof resetTime === 'string') {
      const ms = Date.parse(resetTime);
      return Number.isFinite(ms) ? ms : 0;
    }
    if (resetTime && typeof resetTime === 'object') {
      const rawSec = resetTime.seconds;
      if (
        typeof rawSec === 'bigint' ||
        typeof rawSec === 'number' ||
        typeof rawSec === 'string'
      ) {
        const sec = Number(rawSec);
        const nanos = Number(resetTime.nanos || 0);
        if (Number.isFinite(sec) && sec > 0) {
          return sec * 1000 + Math.floor((Number.isFinite(nanos) ? nanos : 0) / 1000000);
        }
      }
    }
    return 0;
  }

  // Formats remaining time until quota reset as a concise duration string
  function formatQuotaResetRemaining(resetMs) {
    if (!resetMs || resetMs <= 0) return '';
    const diffMs = resetMs - Date.now();
    if (diffMs <= 0) return '';
    const totalMins = Math.max(1, Math.ceil(diffMs / 60000));
    const hours = Math.floor(totalMins / 60);
    const mins = totalMins % 60;
    if (hours > 0 && mins > 0) return `resets in ${hours}h ${mins}m`;
    if (hours > 0) return `resets in ${hours}h`;
    return `resets in ${mins}m`;
  }

  // Computes effective remaining quota fraction [0.0, 1.0] handling Protobuf3 0.0 omission
  function getEffectiveQuotaFraction(quotaInfo, tagTitle, tagDescription, nowMs) {
    const rawTitle = typeof tagTitle === 'string' ? tagTitle.trim() : '';
    const rawDesc = typeof tagDescription === 'string' ? tagDescription.trim() : '';
    if (
      rawTitle === 'OneQuota' ||
      rawDesc.includes('OneQuota') ||
      !quotaInfo ||
      typeof quotaInfo !== 'object'
    ) {
      return 1.0;
    }
    const now = typeof nowMs === 'number' ? nowMs : Date.now();
    const resetMs = parseQuotaResetMs(quotaInfo.resetTime);
    if (resetMs > 0 && resetMs <= now) {
      return 1.0;
    }
    const rawFrac = quotaInfo.remainingFraction;
    if (rawFrac === undefined || rawFrac === null) {
      return resetMs > now ? 0.0 : 1.0;
    }
    const numFrac = Number(rawFrac);
    return Number.isFinite(numFrac) ? Math.max(0, Math.min(1, numFrac)) : 0.0;
  }

  // Formats live quota percentage pill and tooltip description for a model
  function formatModelQuotaTag(baseTagTitle, baseTagDescription, modelId, quotaInfo) {
    const rawTitle = typeof baseTagTitle === 'string' ? baseTagTitle.trim() : '';
    const rawDesc = typeof baseTagDescription === 'string' ? baseTagDescription.trim() : '';
    if (
      rawTitle === 'OneQuota' ||
      rawDesc.includes('OneQuota') ||
      !quotaInfo ||
      typeof quotaInfo !== 'object'
    ) {
      return { tagTitle: rawTitle, tagDescription: rawDesc };
    }
    const resetMs = parseQuotaResetMs(quotaInfo.resetTime);
    if (quotaInfo.remainingFraction === undefined && !resetMs) {
      return { tagTitle: rawTitle, tagDescription: rawDesc };
    }
    const frac = getEffectiveQuotaFraction(quotaInfo, rawTitle, rawDesc);
    const pct = Math.round(frac * 100);
    const tagTitle = rawTitle ? `${rawTitle} · ${pct}%` : `${pct}%`;
    const resetText = formatQuotaResetRemaining(resetMs);
    const descParts = [`${pct}% quota left`];
    if (resetText) descParts.push(resetText);
    const fallbackDesc = rawDesc || (typeof modelId === 'string' ? modelId.trim() : '');
    if (fallbackDesc) descParts.push(fallbackDesc);
    return { tagTitle, tagDescription: descParts.join(' · ') };
  }

  // Builds a Protobuf-ES QuotaInfo message preserving descriptor prototype
  function buildQuotaInfoObject(templateConfig, rawQuotaInfo) {
    if (!rawQuotaInfo || typeof rawQuotaInfo !== 'object') return undefined;
    const qProto =
      templateConfig?.quotaInfo && typeof templateConfig.quotaInfo === 'object'
        ? Object.getPrototypeOf(templateConfig.quotaInfo)
        : null;
    const quotaObj = qProto && qProto !== Object.prototype ? Object.create(qProto) : {};
    quotaObj.$typeName = 'exa.codeium_common_pb.QuotaInfo';

    const resetMs = parseQuotaResetMs(rawQuotaInfo.resetTime);
    const rawFrac = rawQuotaInfo.remainingFraction;
    const effectiveFrac =
      rawFrac !== undefined && rawFrac !== null
        ? Number(rawFrac)
        : resetMs > Date.now()
        ? 0
        : 1;
    quotaObj.remainingFraction = Number.isFinite(effectiveFrac)
      ? Math.max(0, Math.min(1, effectiveFrac))
      : 0;

    const rawReset = rawQuotaInfo.resetTime;
    if (typeof rawReset === 'string') {
      const ms = Date.parse(rawReset);
      if (!Number.isNaN(ms)) {
        const tsProto =
          templateConfig?.quotaInfo?.resetTime &&
          typeof templateConfig.quotaInfo.resetTime === 'object'
            ? Object.getPrototypeOf(templateConfig.quotaInfo.resetTime)
            : null;
        const tsObj = tsProto && tsProto !== Object.prototype ? Object.create(tsProto) : {};
        tsObj.$typeName = 'google.protobuf.Timestamp';
        tsObj.seconds = BigInt(Math.floor(ms / 1000));
        tsObj.nanos = (ms % 1000) * 1000000;
        quotaObj.resetTime = tsObj;
      }
    } else if (rawReset && typeof rawReset === 'object') {
      quotaObj.resetTime = rawReset;
    }
    return quotaObj;
  }

  // Indexes models from clientModelConfigs and availableModelsCache
  function buildModelCatalogIndex(store) {
    const byId = new Map();
    const byEnum = new Map();

    const addEntry = (entry) => {
      if (!entry) return;
      if (entry.modelId) byId.set(entry.modelId.toLowerCase(), entry);
      if (entry.enumVal) byEnum.set(entry.enumVal, entry);
      if (entry.enumName) byId.set(entry.enumName.toLowerCase(), entry);
    };

    const modelsMap = availableModelsCache?.models;
    if (modelsMap && typeof modelsMap === 'object') {
      for (const [mid, det] of Object.entries(modelsMap)) {
        if (!det || typeof det !== 'object') continue;
        const enumVal = resolveModelEnumValue(det.model);
        const curated = CURATED_HIDDEN_MODEL_MAP.get(mid);
        const label = curated?.label || det.displayName || humanizeModelId(mid);
        const quota = getEffectiveQuotaFraction(det.quotaInfo, curated?.tag || '', '');
        addEntry({
          modelId: mid,
          enumVal,
          enumName: det.model || '',
          label,
          quota,
        });
      }
    }

    const configs = store?.getState()?.userStatus?.cascadeModelConfigData?.clientModelConfigs;
    if (Array.isArray(configs)) {
      for (const cfg of configs) {
        if (!cfg) continue;
        const mid = cfg.modelId || '';
        const numVal = cfg.modelOrAlias?.choice?.value;
        const enumVal = typeof numVal === 'number' ? numVal : 0;
        const label = cfg.label || mid;
        const quota = getEffectiveQuotaFraction(cfg.quotaInfo, cfg.tagTitle, cfg.tagDescription);
        const prev = (mid && byId.get(mid.toLowerCase())) || (enumVal && byEnum.get(enumVal));
        const entry = {
          modelId: mid || prev?.modelId || '',
          enumVal: enumVal || prev?.enumVal || 0,
          enumName:
            prev?.enumName || (enumVal >= 1000 ? `MODEL_PLACEHOLDER_M${enumVal - 1000}` : ''),
          label,
          quota,
        };
        addEntry(entry);
      }
    }

    return { byId, byEnum };
  }

  // Reroutes wasteful WTU models to request-counted or Orchestrator pools
  function rerouteWastefulWtuModelInUi(catalog, modelKey, minFloor = QUOTA_SOFT_FLOOR) {
    if (!catalog || modelKey === undefined || modelKey === null) return null;
    const entry =
      typeof modelKey === 'number'
        ? catalog.byEnum.get(modelKey)
        : catalog.byId.get(String(modelKey).toLowerCase());
    const id = (entry?.modelId || String(modelKey)).toLowerCase();
    const enumStr = (entry?.enumName || String(modelKey)).toLowerCase();
    const numVal = entry?.enumVal || (typeof modelKey === 'number' ? modelKey : 0);

    // Non-BoN Fable 5.1 -> Opus 5.5
    const isFable =
      id === 'fable-5.1-dropdown' ||
      (id.startsWith('fable-5.1') && !id.includes('-bon-le')) ||
      numVal === 1388 ||
      numVal === 1386 ||
      numVal === 1387 ||
      enumStr === 'model_placeholder_m388' ||
      enumStr === 'model_placeholder_m386' ||
      enumStr === 'model_placeholder_m387';
    if (isFable) {
      for (const cand of [
        'opus-5.5-high-bon-le',
        'opus-5.5-max-bon-le',
        'opus-5.5-medium',
        'opus-5.5-high',
      ]) {
        const hit = catalog.byId.get(cand);
        if (hit && hit.quota >= minFloor) return hit;
      }
    }

    // Gemini 4 Argon WTU -> Gemini Pro Orchestrator / Carbon-G
    const isArgon =
      id.startsWith('gemini-launch-candidate') ||
      numVal === 1393 ||
      (numVal >= 1416 && numVal <= 1418) ||
      enumStr === 'model_placeholder_m393' ||
      enumStr === 'model_placeholder_m416' ||
      enumStr === 'model_placeholder_m417' ||
      enumStr === 'model_placeholder_m418';
    if (isArgon) {
      for (const cand of ['gemini-pro-coordinator', 'bon-experiment-arm-226', 'auto']) {
        const hit = catalog.byId.get(cand);
        if (hit && hit.quota >= minFloor) return hit;
      }
    }

    // Gemini 4 Carbon WTU -> Gemini 4 Carbon-G
    const isCarbon =
      (id.startsWith('gemini-4-carbon') && !id.includes('-g')) ||
      (numVal >= 1439 && numVal <= 1442) ||
      (enumStr >= 'model_placeholder_m439' && enumStr <= 'model_placeholder_m442');
    if (isCarbon) {
      for (const cand of ['bon-experiment-arm-226', 'gemini-pro-coordinator', 'auto']) {
        const hit = catalog.byId.get(cand);
        if (hit && hit.quota >= minFloor) return hit;
      }
    }

    return null;
  }

  // Resolves the best available healthy model for a project profile
  function resolveAutopilotModelForProfile(
    catalog,
    profileName,
    softFloor = QUOTA_SOFT_FLOOR,
    hardFloor = QUOTA_HARD_FLOOR
  ) {
    if (!catalog) return null;
    const p = String(profileName || 'orchestrator')
      .trim()
      .toLowerCase()
      .replace(/^profile:/, '');

    if (p === 'cpp-core') {
      const hitBon =
        catalog.byId.get('opus-5.5-high-bon-le') || catalog.byId.get('opus-5.5-max-bon-le');
      const hitWtu = catalog.byId.get('opus-5.5-medium') || catalog.byId.get('opus-5.5-high');
      const qBon = hitBon ? hitBon.quota : 0;
      const qWtu = hitWtu ? hitWtu.quota : 0;
      if ((hitBon && qBon >= softFloor) || (hitWtu && qWtu >= softFloor)) {
        if (hitWtu && qWtu > qBon + 0.05 && qWtu >= softFloor) return hitWtu;
        if (hitBon && qBon >= softFloor) return hitBon;
        if (hitWtu) return hitWtu;
      }
    }

    if (p === 'triage') {
      const hitFlash25 =
        catalog.byId.get('gemini-2.5-flash') || catalog.byId.get('gemini-3.5-flash-lite');
      const hitFlashNext =
        catalog.byId.get('gemini-flash-next') ||
        catalog.byId.get('gemini-3.8-flash-high') ||
        catalog.byId.get('gemini-3.8-flash-medium');
      const q25 = hitFlash25 ? hitFlash25.quota : 0;
      const qNext = hitFlashNext ? hitFlashNext.quota : 0;
      if ((hitFlash25 && q25 >= softFloor) || (hitFlashNext && qNext >= softFloor)) {
        if (hitFlash25 && q25 > qNext + 0.05 && q25 >= softFloor) return hitFlash25;
        if (hitFlashNext && qNext >= softFloor) return hitFlashNext;
        if (hitFlash25) return hitFlash25;
      }
    }

    const chain = WEB_MODEL_PROFILES[p] || WEB_MODEL_PROFILES.orchestrator;
    for (const mid of chain) {
      const hit = catalog.byId.get(mid);
      if (hit && hit.quota >= softFloor) return hit;
    }

    const candidates = [];
    for (const mid of chain) {
      const hit = catalog.byId.get(mid);
      if (hit) candidates.push(hit);
    }
    if (candidates.length > 0) {
      candidates.sort((a, b) => (b.quota || 0) - (a.quota || 0));
      if (candidates[0].quota >= hardFloor) return candidates[0];
      return candidates[0];
    }

    return catalog.byId.get('auto') || catalog.byEnum.values().next().value || null;
  }

  // Finds project profile ('cpp-core', 'triage', 'orchestrator') for a conversation or route
  function resolveProjectProfileForConversation(store, cascadeId) {
    const summariesMap = store?.getState()?.trajectorySummaries?.summaries;
    let projectId = cascadeId && summariesMap?.[cascadeId]?.trajectoryMetadata?.projectId;

    if (!projectId) {
      const search = window.location?.search || '';
      const m = /[?&]section=([a-zA-Z0-9_-]+)/.exec(search);
      if (m) projectId = m[1];
    }
    if (!projectId) {
      projectId = store?.getState()?.projects?.activeProjectId;
    }

    if (projectId) {
      const cleanId = String(projectId).trim().toLowerCase();
      if (PROJECT_MODEL_PROFILES[cleanId]) {
        return PROJECT_MODEL_PROFILES[cleanId];
      }
      let pName = '';
      const projs = store?.getState()?.projects?.projects;
      if (Array.isArray(projs)) {
        const match = projs.find((p) => p && p.id === projectId);
        pName = match?.name || '';
      } else if (projs && typeof projs === 'object') {
        pName = projs[projectId]?.name || '';
      }
      if (!pName && roadmapIndexCache?.projects?.[projectId]) {
        pName = roadmapIndexCache.projects[projectId].name || '';
      }
      if (pName) {
        const cleanName = cleanBaseTitle(pName).toLowerCase();
        if (PROJECT_MODEL_PROFILES[cleanName]) {
          return PROJECT_MODEL_PROFILES[cleanName];
        }
      }
    }
    return projectId ? 'orchestrator' : '';
  }

  // Computes a project-scoped pin key for drafts so manual draft picks do not leak across projects
  function resolveDraftPinKey(store, cascadeId) {
    if (cascadeId) return String(cascadeId);
    let projectId = '';
    const search = window.location?.search || '';
    const m = /[?&]section=([a-zA-Z0-9_-]+)/.exec(search);
    if (m) projectId = m[1];
    if (!projectId) {
      projectId = store?.getState()?.projects?.activeProjectId || '';
    }
    const cleanId = String(projectId || 'default').trim().toLowerCase() || 'default';
    return `__draft__:${cleanId}`;
  }

  // Hydrates persisted manual model pins from localStorage so F5 reloads keep user picks
  function loadManualModelPins() {
    try {
      const raw = window.localStorage?.getItem(MANUAL_MODEL_PINS_STORAGE_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object') return;
      const now = Date.now();
      for (const [k, v] of Object.entries(parsed)) {
        if (!k) continue;
        const model = typeof v === 'number' ? v : Number(v?.model);
        if (!Number.isFinite(model) || model <= 0) continue;
        const atStep = typeof v === 'object' && v ? Math.max(0, Number(v.atStep || 0)) : 0;
        const updatedAt =
          typeof v === 'object' && v && Number.isFinite(Number(v.updatedAt))
            ? Number(v.updatedAt)
            : now;
        if (k.startsWith('__draft__:') && now - updatedAt > DRAFT_MANUAL_PIN_TTL_MS) {
          continue;
        }
        manualPickedModels.set(k, { model, atStep, updatedAt });
      }
    } catch {
      // Ignore malformed localStorage state
    }
  }

  // Persists manual model pins to localStorage bounded to recent entries
  function saveManualModelPins() {
    try {
      if (typeof window.localStorage?.setItem !== 'function') return;
      const now = Date.now();
      const entries = [];
      for (const [k, v] of manualPickedModels.entries()) {
        if (!k || !v) continue;
        const model = typeof v === 'number' ? v : Number(v.model);
        if (!Number.isFinite(model) || model <= 0) continue;
        const atStep = typeof v === 'object' ? Math.max(0, Number(v.atStep || 0)) : 0;
        const updatedAt =
          typeof v === 'object' && Number.isFinite(Number(v.updatedAt))
            ? Number(v.updatedAt)
            : now;
        if (k.startsWith('__draft__:') && now - updatedAt > DRAFT_MANUAL_PIN_TTL_MS) {
          manualPickedModels.delete(k);
          continue;
        }
        entries.push([k, { model, atStep, updatedAt }]);
      }
      const trimmed = entries.slice(-MANUAL_MODEL_PINS_MAX_ENTRIES);
      const out = {};
      for (const [k, obj] of trimmed) out[k] = obj;
      window.localStorage.setItem(MANUAL_MODEL_PINS_STORAGE_KEY, JSON.stringify(out));
    } catch {
      // Ignore storage quota errors
    }
  }

  function getManualPinEntry(key) {
    if (!key) return null;
    const raw = manualPickedModels.get(key);
    if (!raw) return null;
    const now = Date.now();
    const entry =
      typeof raw === 'number'
        ? { model: raw, atStep: 0, updatedAt: now }
        : {
            model: Number(raw.model),
            atStep: Math.max(0, Number(raw.atStep || 0)),
            updatedAt: Number.isFinite(Number(raw.updatedAt)) ? Number(raw.updatedAt) : now,
          };
    if (String(key).startsWith('__draft__:') && now - entry.updatedAt > DRAFT_MANUAL_PIN_TTL_MS) {
      manualPickedModels.delete(key);
      saveManualModelPins();
      return null;
    }
    return entry;
  }

  function setManualPin(key, modelEnum, atStep = 0) {
    if (!key || !modelEnum) return;
    manualPickedModels.set(key, {
      model: Number(modelEnum),
      atStep: Math.max(0, Number(atStep || 0)),
      updatedAt: Date.now(),
    });
    saveManualModelPins();
  }

  function deleteManualPin(key) {
    if (!key) return;
    if (manualPickedModels.delete(key)) {
      saveManualModelPins();
    }
  }

  loadManualModelPins();

  // Shows a transient floating notification toast when autopilot fails over a model
  function showAutopilotToast(oldLabel, newLabel, reason) {
    if (disposed || typeof document?.createElement !== 'function') return;
    let toast = document.getElementById(AUTOPILOT_TOAST_ID);
    if (!toast) {
      toast = document.createElement('div');
      toast.id = AUTOPILOT_TOAST_ID;
      toast.setAttribute('data-testid', 'jetski-model-autopilot-toast');
      toast.className = 'jetski-autopilot-toast';
      document.body?.appendChild(toast);
    }
    setAttrIfChanged(toast, 'data-from-model', String(oldLabel || ''));
    setAttrIfChanged(toast, 'data-to-model', String(newLabel || ''));
    const reasonText = reason ? ` (${reason})` : '';
    toast.textContent = `⚡ Autopilot: switched model from ${oldLabel} to ${newLabel}${reasonText}`;
    if (autopilotToastTimer) {
      clearTimeout(autopilotToastTimer);
      autopilotToastTimer = 0;
    }
    autopilotToastTimer = setTimeout(() => {
      autopilotToastTimer = 0;
      toast?.remove();
    }, 4500);
  }

  // Synchronizes project profile defaults on drafts and fails over exhausted models in Redux
  function syncModelAutopilotInStore(store) {
    const state = store?.getState();
    if (!state?.modelSelection) return;
    const catalog = buildModelCatalogIndex(store);
    if (catalog.byEnum.size === 0) return;

    let cascadeId =
      state.conversation?.cascadeId ||
      state.conversation?.convoState?.cascadeId ||
      '';
    if (!cascadeId) {
      const path = window.location?.pathname || '';
      const m = /\/(?:c|cascade|conversation)\/([a-zA-Z0-9_-]+)/.exec(path);
      if (m && m[1]) cascadeId = m[1];
    }

    const summaries = state.trajectorySummaries?.summaries || {};
    const summary = cascadeId ? summaries[cascadeId] : null;
    const stepCount = Number(summary?.stepCount ?? 0);
    const isDraft = !cascadeId || stepCount === 0;
    const profile = resolveProjectProfileForConversation(store, cascadeId);

    if (isDraft) {
      // Restore or honor project-scoped manual draft picks before applying profile defaults
      const targetKey = resolveDraftPinKey(store, cascadeId);
      const curModel = cascadeId
        ? state.modelSelection.byConversation?.[cascadeId]?.model
        : state.modelSelection.default?.model;
      const draftPin = getManualPinEntry(targetKey);
      if (draftPin && draftPin.model) {
        const pinEntry = catalog.byEnum.get(draftPin.model);
        // Keep manual picks while GetAvailableModels is still hydrating (!pinEntry) or quota >= 5%
        if (!pinEntry || pinEntry.quota >= EMERGENCY_PIN_FLOOR) {
          if (curModel !== draftPin.model) {
            isInternalDispatch = true;
            try {
              store.dispatch({
                type: 'modelSelection/modelPicked',
                payload: {
                  conversationId: cascadeId || undefined,
                  model: draftPin.model,
                  ...(cascadeId ? { pick: { atStep: 999999, queuedMessageIds: [] } } : {}),
                },
              });
            } finally {
              isInternalDispatch = false;
            }
          }
          return;
        }
        deleteManualPin(targetKey);
      }
      if (!profile) return;
      const best = resolveAutopilotModelForProfile(catalog, profile);
      if (!best || !best.enumVal) return;
      const seedSig = `${profile}:${best.enumVal}`;
      if (curModel === best.enumVal && autoSeededTargets.get(targetKey) === seedSig) {
        return;
      }
      autoSeededTargets.set(targetKey, seedSig);
      isInternalDispatch = true;
      try {
        store.dispatch({
          type: 'modelSelection/modelPicked',
          payload: {
            conversationId: cascadeId || undefined,
            model: best.enumVal,
            ...(cascadeId ? { pick: { atStep: 999999, queuedMessageIds: [] } } : {}),
          },
        });
      } finally {
        isInternalDispatch = false;
      }
      return;
    }

    // Look up persisted manual pin for existing conversation before evaluating quota or WTU shield
    const pin = getManualPinEntry(cascadeId);
    let curModel =
      state.modelSelection.byConversation?.[cascadeId]?.model ??
      state.modelSelection.default?.model;
    // Restore persisted manual pick after F5 reload when no newer turn has executed since the pick
    if (pin && pin.model && stepCount <= pin.atStep && curModel !== pin.model) {
      const pinEntry = catalog.byEnum.get(pin.model);
      if (!pinEntry || pinEntry.quota >= EMERGENCY_PIN_FLOOR) {
        isInternalDispatch = true;
        try {
          store.dispatch({
            type: 'modelSelection/modelPicked',
            payload: {
              conversationId: cascadeId,
              model: pin.model,
              pick: { atStep: 999999, queuedMessageIds: [] },
            },
          });
        } finally {
          isInternalDispatch = false;
        }
        curModel = pin.model;
      }
    }
    if (!curModel) return;
    const curEntry = catalog.byEnum.get(curModel);
    // Pinned conversations bypass WTU shield and use the 5% emergency floor instead of 15%
    const isPinned = Boolean(pin && pin.model === curModel);

    let replacement = null;
    let reason = '';
    if (curEntry && curEntry.quota < (isPinned ? EMERGENCY_PIN_FLOOR : QUOTA_HARD_FLOOR)) {
      replacement = resolveAutopilotModelForProfile(
        catalog,
        profile || 'orchestrator'
      );
      reason = `${Math.round(curEntry.quota * 100)}% quota`;
    } else if (!isPinned) {
      const shielded = rerouteWastefulWtuModelInUi(catalog, curModel, QUOTA_SOFT_FLOOR);
      if (shielded) {
        replacement = shielded;
        reason = 'WTU shield';
      }
    }

    if (replacement && replacement.enumVal && replacement.enumVal !== curModel) {
      isInternalDispatch = true;
      try {
        store.dispatch({
          type: 'modelSelection/modelPicked',
          payload: {
            conversationId: cascadeId,
            model: replacement.enumVal,
            pick: { atStep: 999999, queuedMessageIds: [] },
          },
        });
      } finally {
        isInternalDispatch = false;
      }
      showAutopilotToast(
        curEntry?.label || String(curModel),
        replacement.label,
        reason
      );
    }
  }

  // Strips synthetic unlocked models and restores native tags on cleanup or re-injection
  function stripUnlockedModels(userStatus) {
    const data = userStatus?.cascadeModelConfigData;
    if (!data || !Array.isArray(data.clientModelConfigs)) return userStatus;
    const hasUnlocked = data.clientModelConfigs.some(
      (c) => c && c.__jetskiUnlockedModel === true
    );
    const hasPatchedNative = data.clientModelConfigs.some((c) => c && c.__jetskiOrigTag);
    if (!hasUnlocked && !hasPatchedNative) return userStatus;

    const unlockedConfigs = data.clientModelConfigs.filter(
      (c) => c && c.__jetskiUnlockedModel === true
    );
    const unlockedLabels = new Set(unlockedConfigs.map((c) => c.label));
    const cleanConfigs = data.clientModelConfigs
      .filter((c) => !c || c.__jetskiUnlockedModel !== true)
      .map((c) => {
        if (!c || !c.__jetskiOrigTag) return c;
        const orig = c.__jetskiOrigTag;
        const restored = cloneWithProto(c, {
          tagTitle: orig.tagTitle,
          tagDescription: orig.tagDescription,
          quotaInfo: orig.quotaInfo,
        });
        delete restored.__jetskiOrigTag;
        return restored;
      });

    const cleanSorts =
      hasUnlocked && Array.isArray(data.clientModelSorts)
        ? data.clientModelSorts.map((sort) => {
            if (!sort || !Array.isArray(sort.groups)) return sort;
            return cloneWithProto(sort, {
              groups: sort.groups.map((group) => {
                if (!group || !Array.isArray(group.modelLabels)) return group;
                return cloneWithProto(group, {
                  modelLabels: group.modelLabels.filter((lbl) => !unlockedLabels.has(lbl)),
                });
              }),
            });
          })
        : data.clientModelSorts;

    return cloneWithProto(userStatus, {
      cascadeModelConfigData: cloneWithProto(data, {
        clientModelConfigs: cleanConfigs,
        clientModelSorts: cleanSorts,
      }),
    });
  }

  // Fetches GetAvailableModels once per TTL window to discover hidden executable models
  async function fetchAvailableModels() {
    const now = Date.now();
    if (availableModelsCache && now - availableModelsFetchedAt < AVAILABLE_MODELS_TTL_MS) {
      return availableModelsCache;
    }
    if (availableModelsFetching) return availableModelsFetching;
    if (typeof window.fetch !== 'function') return availableModelsCache;

    availableModelsFetching = (async () => {
      try {
        const resp = await window.fetch(
          '/exa.language_server_pb.LanguageServerService/GetAvailableModels',
          {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'x-codeium-csrf-token': getCsrfToken(),
            },
            body: '{}',
          }
        );
        if (resp && resp.ok) {
          const raw = await resp.json();
          // GetAvailableModels wraps its payload inside a top-level response object
          const respObj = raw?.response && typeof raw.response === 'object' ? raw.response : raw;
          if (respObj?.models && typeof respObj.models === 'object') {
            availableModelsCache = respObj;
            availableModelsFetchedAt = Date.now();
          }
        }
      } catch {
        // Ignore transient ConnectRPC failures and keep previous cache
      } finally {
        availableModelsFetching = null;
      }
      return availableModelsCache;
    })();
    return availableModelsFetching;
  }

  // Appends hidden executable models from GetAvailableModels and enriches quota tags
  function enrichUserStatusWithHiddenModels(userStatus, availablePayload) {
    const baseStatus = stripUnlockedModels(userStatus);
    const data = baseStatus?.cascadeModelConfigData;
    if (!data || !Array.isArray(data.clientModelConfigs) || data.clientModelConfigs.length === 0) {
      return baseStatus;
    }
    const modelsMap =
      availablePayload?.models && typeof availablePayload.models === 'object'
        ? availablePayload.models
        : null;

    const templateConfig = data.clientModelConfigs[0];

    let nativeModified = false;
    const enrichedNativeConfigs = data.clientModelConfigs.map((cfg) => {
      if (!cfg) return cfg;
      // Prefer fresher GetUserStatus server quota over cached GetAvailableModels
      const serverQuota =
        cfg.quotaInfo && typeof cfg.quotaInfo === 'object' ? cfg.quotaInfo : null;
      let quotaObj = serverQuota;
      if (
        serverQuota &&
        (serverQuota.remainingFraction === undefined || serverQuota.remainingFraction === null) &&
        parseQuotaResetMs(serverQuota.resetTime) > 0
      ) {
        quotaObj = buildQuotaInfoObject(templateConfig, serverQuota);
      } else if (!serverQuota) {
        quotaObj = buildQuotaInfoObject(templateConfig, modelsMap?.[cfg.modelId]?.quotaInfo);
      }
      const { tagTitle, tagDescription } = formatModelQuotaTag(
        cfg.tagTitle,
        cfg.tagDescription,
        cfg.modelId,
        quotaObj
      );
      // Skip cloning when model tags and quota metadata are already up to date
      if (
        tagTitle === (cfg.tagTitle || '') &&
        tagDescription === (cfg.tagDescription || '') &&
        quotaObj === cfg.quotaInfo
      ) {
        return cfg;
      }
      nativeModified = true;
      const patched = cloneWithProto(cfg, {
        tagTitle,
        tagDescription,
        ...(quotaObj !== cfg.quotaInfo ? { quotaInfo: quotaObj } : {}),
      });
      // Stash original tags non-enumerably so cleanup restores clean server state
      Object.defineProperty(patched, '__jetskiOrigTag', {
        value: {
          tagTitle: cfg.tagTitle || '',
          tagDescription: cfg.tagDescription || '',
          quotaInfo: cfg.quotaInfo,
        },
        enumerable: false,
      });
      return patched;
    });

    // When GetAvailableModels is still loading or failed, return native quota updates
    if (!modelsMap) {
      if (!nativeModified) return baseStatus;
      return cloneWithProto(baseStatus, {
        cascadeModelConfigData: cloneWithProto(data, {
          clientModelConfigs: enrichedNativeConfigs,
        }),
      });
    }

    // Exclude FIM tab autocomplete, image generation, audio transcription, and deprecated arms
    const nonChatIds = new Set([
      ...(Array.isArray(availablePayload.tabModelIds) ? availablePayload.tabModelIds : []),
      ...(Array.isArray(availablePayload.imageGenerationModelIds)
        ? availablePayload.imageGenerationModelIds
        : []),
      ...(Array.isArray(availablePayload.audioTranscriptionModelIds)
        ? availablePayload.audioTranscriptionModelIds
        : []),
      ...Object.keys(availablePayload.deprecatedModelIds || {}),
    ]);

    const existingModelIds = new Set();
    const existingEnums = new Set();
    const usedLabels = new Set();
    for (const cfg of enrichedNativeConfigs) {
      if (!cfg) continue;
      if (cfg.modelId) existingModelIds.add(cfg.modelId);
      const numVal = cfg.modelOrAlias?.choice?.value;
      if (typeof numVal === 'number') existingEnums.add(numVal);
      if (cfg.label) usedLabels.add(cfg.label);
    }

    // Order curated hidden models first, followed by any newly added server models
    const candidateIds = [];
    for (const [id] of CURATED_HIDDEN_MODELS) {
      if (Object.prototype.hasOwnProperty.call(modelsMap, id)) {
        candidateIds.push(id);
      }
    }
    for (const id of Object.keys(modelsMap)) {
      if (!CURATED_HIDDEN_MODEL_MAP.has(id)) {
        candidateIds.push(id);
      }
    }

    const unlockedConfigs = [];
    const unlockedLabels = [];

    for (const modelId of candidateIds) {
      if (
        !modelId ||
        existingModelIds.has(modelId) ||
        SKIPPED_HIDDEN_MODEL_IDS.has(modelId) ||
        nonChatIds.has(modelId) ||
        /^(?:tab_|chat_)/i.test(modelId) ||
        /image/i.test(modelId) ||
        modelId.startsWith('models/')
      ) {
        continue;
      }
      const entry = modelsMap[modelId];
      // Skip internal routing placeholders that are not interactive chat models
      if (!entry || typeof entry !== 'object' || entry.apiProvider === 'API_PROVIDER_INTERNAL') {
        continue;
      }
      const enumVal = resolveModelEnumValue(entry.model);
      // Skip unmapped enum strings so Redux never dispatches an invalid Model enum
      if (!enumVal || existingEnums.has(enumVal)) continue;

      const curated = CURATED_HIDDEN_MODEL_MAP.get(modelId);
      let label = '';
      let tagTitle = 'Hidden';
      if (curated) {
        label = curated.label;
        tagTitle = curated.tag;
      } else if (
        typeof entry.displayName === 'string' &&
        entry.displayName.trim() &&
        entry.displayName !== 'None' &&
        !usedLabels.has(entry.displayName.trim())
      ) {
        label = entry.displayName.trim();
      } else {
        label = humanizeModelId(modelId);
      }
      if (usedLabels.has(label)) {
        label = `${label} (${modelId})`;
      }
      if (usedLabels.has(label)) continue;

      const modelOrAlias = cloneWithProto(templateConfig.modelOrAlias, {
        $typeName: 'exa.codeium_common_pb.ModelOrAlias',
        choice: { case: 'model', value: enumVal },
      });

      const quotaObj = buildQuotaInfoObject(templateConfig, entry.quotaInfo);
      const tagInfo = formatModelQuotaTag(tagTitle, modelId, modelId, quotaObj);

      const unlockedCfg = cloneWithProto(templateConfig, {
        $typeName: 'exa.codeium_common_pb.ClientModelConfig',
        label,
        modelOrAlias,
        disabled: false,
        supportsImages: Boolean(entry.supportsImages),
        isRecommended: true,
        quotaInfo: quotaObj,
        tagTitle: tagInfo.tagTitle,
        tagDescription: tagInfo.tagDescription,
        supportedMimeTypes:
          entry.supportedMimeTypes && typeof entry.supportedMimeTypes === 'object'
            ? { ...entry.supportedMimeTypes }
            : {},
        modelId,
      });
      // Non-enumerable marker keeps Protobuf-ES reflection clean while allowing strip on cleanup
      Object.defineProperty(unlockedCfg, '__jetskiUnlockedModel', {
        value: true,
        enumerable: false,
      });

      existingModelIds.add(modelId);
      existingEnums.add(enumVal);
      usedLabels.add(label);
      unlockedConfigs.push(unlockedCfg);
      unlockedLabels.push(label);
    }

    if (unlockedConfigs.length === 0) {
      if (!nativeModified) return baseStatus;
      return cloneWithProto(baseStatus, {
        cascadeModelConfigData: cloneWithProto(data, {
          clientModelConfigs: enrichedNativeConfigs,
        }),
      });
    }

    // useModelGroups reads groups[0] of the Recommended sort to populate the dropdown
    const nextSorts = Array.isArray(data.clientModelSorts)
      ? data.clientModelSorts.map((sort, idx) => {
          const isRec =
            typeof sort?.name === 'string'
              ? sort.name.toLowerCase() === 'recommended'
              : idx === 0;
          if (!isRec || !Array.isArray(sort?.groups) || sort.groups.length === 0) return sort;
          const firstGroup = sort.groups[0];
          const baseLabels = Array.isArray(firstGroup?.modelLabels) ? firstGroup.modelLabels : [];
          return cloneWithProto(sort, {
            groups: [
              cloneWithProto(firstGroup, {
                modelLabels: [...baseLabels, ...unlockedLabels],
              }),
              ...sort.groups.slice(1),
            ],
          });
        })
      : data.clientModelSorts;

    return cloneWithProto(baseStatus, {
      cascadeModelConfigData: cloneWithProto(data, {
        clientModelConfigs: [...enrichedNativeConfigs, ...unlockedConfigs],
        clientModelSorts: nextSorts,
      }),
    });
  }

  // Ensures the hydrated Redux userStatus slice includes unlocked models from GetAvailableModels
  async function syncHiddenModelsInStore(store) {
    const curStatus = store?.getState()?.userStatus;
    const curConfigs = curStatus?.cascadeModelConfigData?.clientModelConfigs;
    if (!Array.isArray(curConfigs) || curConfigs.length === 0) return;
    const cacheFresh = Boolean(
      availableModelsCache && Date.now() - availableModelsFetchedAt < AVAILABLE_MODELS_TTL_MS
    );
    const hasUnlocked = curConfigs.some((c) => c && c.__jetskiUnlockedModel === true);
    if (cacheFresh && hasUnlocked && lastAppliedModelsFetchedAt === availableModelsFetchedAt) {
      syncModelAutopilotInStore(store);
      return;
    }

    const payload = cacheFresh ? availableModelsCache : await fetchAvailableModels();
    if (disposed || !payload) return;

    const latestStatus = store.getState()?.userStatus;
    const latestConfigs = latestStatus?.cascadeModelConfigData?.clientModelConfigs;
    if (!Array.isArray(latestConfigs) || latestConfigs.length === 0) return;
    const latestHasUnlocked = latestConfigs.some((c) => c && c.__jetskiUnlockedModel === true);
    if (latestHasUnlocked && lastAppliedModelsFetchedAt === availableModelsFetchedAt) {
      syncModelAutopilotInStore(store);
      return;
    }

    const enriched = enrichUserStatusWithHiddenModels(latestStatus, payload);
    if (enriched === latestStatus) {
      syncModelAutopilotInStore(store);
      return;
    }
    lastAppliedModelsFetchedAt = availableModelsFetchedAt;

    isInternalDispatch = true;
    try {
      store.dispatch({
        type: 'userStatus/setUserStatus',
        payload: enriched,
      });
    } finally {
      isInternalDispatch = false;
    }
    syncModelAutopilotInStore(store);
  }

  function hookReduxStore(store) {
    if (reduxStore === store) return;
    reduxStore = store;
    installAgentSoundGate();

    // Intercept incoming stream batches so filtered conversations stay hidden
    // without flickering into the sidebar index on server stream ticks
    const origDispatch = store.dispatch;
    store.dispatch = function patchedDispatch(action) {
      if (disposed) return origDispatch.call(this, action);
      // Enrich periodic UserStatus polls synchronously so unlocked models never flicker out
      if (
        !isInternalDispatch &&
        action?.type === 'userStatus/setUserStatus' &&
        action.payload &&
        availableModelsCache
      ) {
        lastAppliedModelsFetchedAt = availableModelsFetchedAt;
        if (Date.now() - availableModelsFetchedAt >= AVAILABLE_MODELS_TTL_MS) {
          void syncHiddenModelsInStore(store);
        }
        return origDispatch.call(this, {
          ...action,
          payload: enrichUserStatusWithHiddenModels(action.payload, availableModelsCache),
        });
      }
      // Track manual model picks and rescue emergency exhausted picks (< 5% quota)
      if (
        !isInternalDispatch &&
        action?.type === 'modelSelection/modelPicked' &&
        action.payload
      ) {
        // Record stepCount alongside the manual pick so older trajectory turns cannot revert it on F5
        const rawCid = action.payload.conversationId || '';
        const pinKey = resolveDraftPinKey(store, rawCid);
        const stepCount = rawCid
          ? Number(store.getState()?.trajectorySummaries?.summaries?.[rawCid]?.stepCount ?? 0)
          : 0;
        const pickedModel = action.payload.model;
        const catalog = buildModelCatalogIndex(store);
        const entry = catalog.byEnum.get(pickedModel);
        if (entry && entry.quota < EMERGENCY_PIN_FLOOR) {
          const profile =
            resolveProjectProfileForConversation(store, action.payload.conversationId) ||
            'orchestrator';
          const rescue = resolveAutopilotModelForProfile(catalog, profile);
          if (rescue && rescue.enumVal && rescue.enumVal !== pickedModel) {
            // Persist the rescued model as the active pin for this conversation or draft
            setManualPin(pinKey, rescue.enumVal, stepCount);
            showAutopilotToast(
              entry.label,
              rescue.label,
              `${Math.round(entry.quota * 100)}% exhausted`
            );
            return origDispatch.call(this, {
              ...action,
              payload: { ...action.payload, model: rescue.enumVal },
            });
          }
        }
        if (pickedModel) {
          setManualPin(pinKey, pickedModel, stepCount);
        }
      }
      // Transfer a project draft manual pick to the newly seeded conversation and free the draft slot
      if (
        !isInternalDispatch &&
        action?.type === 'modelSelection/conversationSeeded' &&
        action.payload?.conversationId &&
        !action.payload.sourceConversationId
      ) {
        const draftKey = resolveDraftPinKey(store, '');
        const draftPin = getManualPinEntry(draftKey);
        if (draftPin && draftPin.model) {
          setManualPin(action.payload.conversationId, draftPin.model, 1);
          deleteManualPin(draftKey);
        }
        // Clear cached draft seed signature so the next new draft evaluates fresh autopilot state
        autoSeededTargets.delete(draftKey);
      }
      // Fail over exhausted trajectory models and shield wasteful WTU runs
      if (
        !isInternalDispatch &&
        action?.type === 'modelSelection/trajectoryModelObserved' &&
        action.payload
      ) {
        const cid = action.payload.conversationId;
        const obsModel = action.payload.model;
        const catalog = buildModelCatalogIndex(store);
        const stepCount = cid
          ? Number(store.getState()?.trajectorySummaries?.summaries?.[cid]?.stepCount ?? 0)
          : 0;
        const execStep = Number(action.payload.executionStartStep ?? stepCount);
        const pin = cid ? getManualPinEntry(cid) : null;
        // Preserve manual pick across F5 reload when trajectoryModelObserved replays an older turn
        if (pin && pin.model && (stepCount <= pin.atStep || execStep <= pin.atStep)) {
          const pinEntry = catalog.byEnum.get(pin.model);
          // Keep user's manual pick while catalog hydrates or as long as its quota is >= 5%
          if (!pinEntry || pinEntry.quota >= EMERGENCY_PIN_FLOOR) {
            if (obsModel !== pin.model && cid) {
              isInternalDispatch = true;
              try {
                store.dispatch({
                  type: 'modelSelection/modelPicked',
                  payload: {
                    conversationId: cid,
                    model: pin.model,
                    pick: { atStep: 999999, queuedMessageIds: [] },
                  },
                });
              } finally {
                isInternalDispatch = false;
              }
            }
            return origDispatch.call(this, {
              ...action,
              payload: {
                ...action.payload,
                model: pin.model,
                label: pinEntry?.label || action.payload.label,
              },
            });
          }
        } else if (pin && cid && stepCount > pin.atStep && execStep > pin.atStep) {
          // Advance pin step when a newer turn runs on the pinned model, or clear if switched
          if (obsModel === pin.model) {
            setManualPin(cid, pin.model, stepCount);
          } else {
            deleteManualPin(cid);
          }
        }
        const obsEntry = catalog.byEnum.get(obsModel);
        // Re-read active pin after step progression so WTU shield and 15% floor respect user pins
        const activePin = cid ? getManualPinEntry(cid) : null;
        const isPinned = Boolean(activePin && activePin.model === obsModel);
        let replacement = null;
        let reason = '';
        if (obsEntry && obsEntry.quota < (isPinned ? EMERGENCY_PIN_FLOOR : QUOTA_HARD_FLOOR)) {
          const profile =
            resolveProjectProfileForConversation(store, cid) || 'orchestrator';
          replacement = resolveAutopilotModelForProfile(catalog, profile);
          reason = `${Math.round(obsEntry.quota * 100)}% quota`;
        } else if (!isPinned) {
          const shielded = rerouteWastefulWtuModelInUi(catalog, obsModel, QUOTA_SOFT_FLOOR);
          if (shielded) {
            replacement = shielded;
            reason = 'WTU shield';
          }
        }
        if (replacement && replacement.enumVal && replacement.enumVal !== obsModel) {
          showAutopilotToast(
            obsEntry?.label || action.payload.label || String(obsModel),
            replacement.label,
            reason
          );
          if (cid) {
            isInternalDispatch = true;
            try {
              store.dispatch({
                type: 'modelSelection/modelPicked',
                payload: {
                  conversationId: cid,
                  model: replacement.enumVal,
                  pick: { atStep: 999999, queuedMessageIds: [] },
                },
              });
            } finally {
              isInternalDispatch = false;
            }
          }
          return origDispatch.call(this, {
            ...action,
            payload: {
              ...action.payload,
              model: replacement.enumVal,
              label: replacement.label,
            },
          });
        }
      }
      // Rewrite any open request so keyboard shortcuts or UI triggers cannot open the panel
      if (
        action?.type === 'auxPaneV2/setOverviewPanelOpen' &&
        action.payload?.open !== false
      ) {
        return origDispatch.call(this, {
          ...action,
          payload: {
            ...action.payload,
            open: false,
          },
        });
      }
      // Sanitize hydrated session storage so persisted open flags never restore the panel
      if (
        action?.type === 'auxPaneV2/restorePersistedPanes' &&
        action.payload?.overviewPanelOpen &&
        typeof action.payload.overviewPanelOpen === 'object'
      ) {
        const closedMap = {};
        // Force every persisted conversation entry to false before the reducer merges it
        for (const k of Object.keys(action.payload.overviewPanelOpen)) {
          closedMap[k] = false;
        }
        return origDispatch.call(this, {
          ...action,
          payload: {
            ...action.payload,
            overviewPanelOpen: closedMap,
          },
        });
      }
      if (
        !isInternalDispatch &&
        action?.type === 'trajectorySummaries/applyStreamBatch' &&
        Array.isArray(action.payload?.updates)
      ) {
        const prevSummaries = store.getState()?.trajectorySummaries?.summaries || {};
        const currentSummaries = { ...prevSummaries };
        for (const [id, incomingSummary] of action.payload.updates) {
          if (incomingSummary) {
            currentSummaries[id] = incomingSummary;
          }
        }
        const { hiddenIds } = classifySummaries(currentSummaries, visibleBuckets);
        const rewrittenUpdates = action.payload.updates.map(([id, incomingSummary]) => {
          if (!incomingSummary) return [id, incomingSummary];
          if (hiddenIds.has(id)) {
            if (!incomingSummary.trajectoryMetadata?.sourceMetadata?.hideFromConversationList) {
              managedHiddenIds.set(id, false);
              return [id, cloneSummaryWithHideFlag(incomingSummary, true)];
            }
          } else if (managedHiddenIds.has(id)) {
            managedHiddenIds.delete(id);
            if (incomingSummary.trajectoryMetadata?.sourceMetadata?.hideFromConversationList) {
              return [id, cloneSummaryWithHideFlag(incomingSummary, false)];
            }
          }
          return [id, incomingSummary];
        });
        // The notification middleware runs inside this dispatch and starts the chime fetch
        // synchronously, so the gate only needs to stay closed for the dispatch itself
        const mute = batchOnlyAlertsArchivedPieces(action.payload.updates, prevSummaries);
        if (mute) window.__jetskiAgentSoundMuted = (window.__jetskiAgentSoundMuted || 0) + 1;
        let result;
        try {
          result = origDispatch.call(this, {
            ...action,
            payload: {
              ...action.payload,
              updates: rewrittenUpdates,
            },
          });
        } finally {
          if (mute) window.__jetskiAgentSoundMuted--;
        }
        scheduleSync();
        return result;
      }
      return origDispatch.call(this, action);
    };

    store.subscribe(() => {
      if (!isInternalDispatch) {
        scheduleSync();
      }
    });
    // Populate unlocked hidden models as soon as the Redux store is attached
    void syncHiddenModelsInStore(store);
  }

  // Forces the floating Overview Panel closed in Redux for all active conversations
  function syncOverviewPanelClosed(store) {
    const state = store?.getState();
    // Skip stores that have not initialized the auxPaneV2 slice
    if (!state?.auxPaneV2) return;

    const openMap = state.auxPaneV2.overviewPanelOpen || {};
    const targetIds = new Set();

    // Queue any conversation that already has an explicit open flag in Redux
    for (const [cid, isOpen] of Object.entries(openMap)) {
      // Unset or true entries default to open in useOverviewPanelOpen
      if (isOpen !== false) {
        targetIds.add(cid);
      }
    }

    const activeCandidates = [
      state.conversation?.cascadeId,
      state.conversation?.convoState?.cascadeId,
      state.multiConvoLayout?.focusedPaneId,
    ];
    const path = window.location?.pathname || '';
    const m = /\/(?:c|cascade|conversation)\/([a-zA-Z0-9_-]+)/.exec(path);
    // Include the routed conversation ID before Redux conversation state settles
    if (m && m[1]) {
      activeCandidates.push(m[1]);
    }
    // Include split-pane conversations discovered from mounted host or titlebar nodes
    for (const el of document.querySelectorAll(
      '[data-testid="overview-panel-host"], [data-testid="titlebar-more-actions"]'
    )) {
      const cid = findCascadeIdFromElement(el);
      // Only add resolved non-empty conversation IDs
      if (cid) activeCandidates.push(cid);
    }

    // useOverviewPanelOpen defaults missing map keys to true, so explicitly write false
    for (const cid of activeCandidates) {
      // Skip conversations already recorded as false in overviewPanelOpen
      if (typeof cid === 'string' && cid.length > 0 && openMap[cid] !== false) {
        // Avoid infinite dispatch loops on mock stores without an auxPaneV2 reducer
        if (!overviewCloseDispatchedIds.has(cid) || openMap[cid] === true) {
          targetIds.add(cid);
        }
      }
    }

    // Nothing to dispatch when all active conversations are already marked closed
    if (targetIds.size === 0) return;

    isInternalDispatch = true;
    try {
      // Dispatch explicit false per conversation so OverviewPanelHost unmounts the panel
      for (const cid of targetIds) {
        overviewCloseDispatchedIds.add(cid);
        store.dispatch({
          type: 'auxPaneV2/setOverviewPanelOpen',
          payload: { cascadeId: cid, open: false },
        });
      }
    } finally {
      isInternalDispatch = false;
    }
  }

  function syncReduxFilter() {
    const store = findReduxStore();
    if (!store) return false;

    // Keep the floating Overview Panel closed whenever Redux state syncs
    syncOverviewPanelClosed(store);
    // Unlock hidden executable models in Redux userStatus if hydrated after store attach
    void syncHiddenModelsInStore(store);
    // Synchronize model autopilot defaults and quota failovers
    syncModelAutopilotInStore(store);

    const summariesMap = store.getState()?.trajectorySummaries?.summaries;
    if (!summariesMap) return false;

    const { hiddenIds, chosenHiddenCount, perProjectHidden, statsSummary } =
      classifySummaries(summariesMap, visibleBuckets);
    totalHiddenCount = chosenHiddenCount;
    hiddenCountsByProject = perProjectHidden;
    if (statsSummary) {
      breakdownStats = statsSummary;
    }

    const updates = [];

    for (const cascadeId of hiddenIds) {
      const summary = summariesMap[cascadeId];
      if (!summary) continue;
      const alreadyHidden =
        Boolean(summary.trajectoryMetadata?.sourceMetadata?.hideFromConversationList);
      if (!alreadyHidden) {
        managedHiddenIds.set(cascadeId, false);
        updates.push([cascadeId, cloneSummaryWithHideFlag(summary, true)]);
      }
    }
    for (const [cascadeId] of Array.from(managedHiddenIds.entries())) {
      if (!hiddenIds.has(cascadeId)) {
        managedHiddenIds.delete(cascadeId);
        const summary = summariesMap[cascadeId];
        if (summary?.trajectoryMetadata?.sourceMetadata?.hideFromConversationList) {
          updates.push([cascadeId, cloneSummaryWithHideFlag(summary, false)]);
        }
      }
    }

    if (updates.length > 0) {
      isInternalDispatch = true;
      try {
        store.dispatch({
          type: 'trajectorySummaries/applyStreamBatch',
          payload: { updates, deletes: [] },
        });
      } finally {
        isInternalDispatch = false;
      }
    }

    return true;
  }

  function ensureStylesInjected() {
    let style = document.getElementById(STYLE_ID);
    if (!style) {
      style = document.createElement('style');
      style.id = STYLE_ID;
      (document.head || document.documentElement).appendChild(style);
    }
    const nextCss = `
      #${SEARCH_PANEL_ID} {
        position: fixed; top: 10vh; left: 50%; transform: translateX(-50%);
        width: min(720px, 90vw); max-height: 75vh; z-index: 2147483000;
        display: flex; flex-direction: column; gap: 8px; padding: 12px;
        background: var(--background, #1e1e1e); color: var(--foreground, #ddd);
        border: 1px solid var(--border, #555); border-radius: 8px;
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45); font-size: 13px;
      }
      #${SEARCH_PANEL_ID} input {
        width: 100%; padding: 6px 8px; font-size: 14px; color: inherit;
        background: transparent; border: 1px solid var(--border, #555); border-radius: 4px;
      }
      #${SEARCH_PANEL_ID} .jcs-results { overflow-y: auto; }
      #${SEARCH_PANEL_ID} .jcs-hit { padding: 6px 4px; cursor: pointer; border-radius: 4px; }
      #${SEARCH_PANEL_ID} .jcs-hit:hover { background: rgba(127, 127, 127, 0.18); }
      #${SEARCH_PANEL_ID} .jcs-meta { opacity: 0.65; font-size: 11px; }
      #${SEARCH_PANEL_ID} mark { background: rgba(255, 200, 0, 0.35); color: inherit; }
      // Style all titlebar action buttons as uniform compact icons without pill borders
      .${SEARCH_BTN_CLASS},
      .${LOCATE_BTN_CLASS},
      .${TITLEBAR_BTN_CLASS} {
        background: transparent;
        border: none;
        cursor: pointer;
        padding: 0 4px;
        opacity: 0.8;
        font-size: 14px;
        line-height: 1;
      }
      .${SEARCH_BTN_CLASS}:hover,
      .${LOCATE_BTN_CLASS}:hover,
      .${TITLEBAR_BTN_CLASS}:hover {
        opacity: 1;
      }
      /* Compact context volume pill aligned with context_guard 60% / 65% / 80% thresholds */
      .${TITLEBAR_CONTEXT_BADGE_CLASS} {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        height: 20px;
        padding: 0 7px;
        margin-right: 4px;
        border-radius: 999px;
        font-size: 11px;
        font-weight: 600;
        line-height: 1;
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
        cursor: pointer;
        user-select: none;
        border: 1px solid rgba(128, 134, 139, 0.32);
        background: rgba(128, 134, 139, 0.12);
        color: var(--muted-foreground, #b8bdc4);
        transition: background-color 120ms ease, border-color 120ms ease, color 120ms ease;
      }
      .${TITLEBAR_CONTEXT_BADGE_CLASS}:hover {
        background: rgba(128, 134, 139, 0.22);
        color: var(--foreground, #e8eaed);
      }
      .${TITLEBAR_CONTEXT_BADGE_CLASS}[data-level="soft"] {
        border-color: rgba(251, 191, 36, 0.45);
        background: rgba(251, 191, 36, 0.13);
        color: #fde047;
      }
      .${TITLEBAR_CONTEXT_BADGE_CLASS}[data-level="soft"]:hover {
        background: rgba(251, 191, 36, 0.22);
      }
      .${TITLEBAR_CONTEXT_BADGE_CLASS}[data-level="turn-start"] {
        border-color: rgba(251, 146, 60, 0.48);
        background: rgba(251, 146, 60, 0.15);
        color: #fdba74;
      }
      .${TITLEBAR_CONTEXT_BADGE_CLASS}[data-level="turn-start"]:hover {
        background: rgba(251, 146, 60, 0.24);
      }
      .${TITLEBAR_CONTEXT_BADGE_CLASS}[data-level="hard"] {
        border-color: rgba(248, 113, 113, 0.5);
        background: rgba(248, 113, 113, 0.16);
        color: #fca5a5;
      }
      .${TITLEBAR_CONTEXT_BADGE_CLASS}[data-level="hard"]:hover {
        background: rgba(248, 113, 113, 0.26);
      }
      @keyframes jetski-locate-pulse {
        0% {
          box-shadow: 0 0 0 2px #8ab4f8, 0 0 12px rgba(138, 180, 248, 0.55);
          background-color: rgba(138, 180, 248, 0.22);
        }
        70% {
          box-shadow: 0 0 0 2px rgba(138, 180, 248, 0.75), 0 0 6px rgba(138, 180, 248, 0.25);
          background-color: rgba(138, 180, 248, 0.14);
        }
        100% {
          box-shadow: 0 0 0 0 rgba(138, 180, 248, 0);
          background-color: transparent;
        }
      }
      [data-testid="conversation-row-sidebar"][data-jetski-located-row="true"],
      div[data-sidebar-row-id][data-jetski-located-row="true"] {
        border-radius: 8px;
        animation: jetski-locate-pulse 2.2s ease-out forwards;
      }
      [data-sidebar-row-id][data-jetski-closed-row="true"]:not([data-sidebar-section-id="pinned"]) {
        height: 0px !important;
        min-height: 0px !important;
        max-height: 0px !important;
        padding-top: 0px !important;
        padding-bottom: 0px !important;
        margin-top: 0px !important;
        margin-bottom: 0px !important;
        border: none !important;
        overflow: hidden !important;
        pointer-events: none !important;
        visibility: hidden !important;
      }
      /* Hide the floating Overview Panel and zero its chat width reserve on first paint */
      [data-testid="overview-panel-host-sibling"],
      [data-testid="overview-panel-host-gutter"],
      [data-testid="overview-panel-host-overlay"],
      [data-testid="overview-panel-overlay-backdrop"],
      [data-testid="overview-panel-dropdown-popover"],
      [data-testid="battle-overview-dropdown"],
      [data-testid="toggle-overview-panel"] {
        display: none !important;
      }
      [data-testid="overview-panel-host"] {
        --overview-panel-reserve: 0px !important;
        --overview-panel-gutter-reserve: 0px !important;
      }
      .${BAR_CLASS} {
        display: inline-flex;
        align-items: center;
        gap: 2px;
        padding: 0;
        width: auto;
        height: 20px;
        box-sizing: border-box;
        flex-wrap: nowrap;
      }
      .${BUTTON_CLASS} {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 20px;
        min-width: 20px !important;
        height: 20px;
        min-height: 20px !important;
        padding: 0 !important;
        border-radius: 4px;
        border: none !important;
        background: transparent;
        cursor: pointer;
        user-select: none;
        transition: opacity 120ms ease, background-color 120ms ease;
        flex: 0 0 auto;
        box-sizing: border-box;
      }
      .${BUTTON_CLASS} > * {
        pointer-events: none;
      }
      .${BUTTON_CLASS}[data-active="false"] {
        background: transparent;
        border: none;
        opacity: 0.35;
        filter: grayscale(0.4);
      }
      .${BUTTON_CLASS}[data-active="false"]:hover {
        background: var(--sidebar-secondary, rgba(128, 134, 139, 0.18));
        opacity: 0.8;
      }
      .${BUTTON_CLASS}[data-active="true"] {
        background: transparent;
        border: none;
        opacity: 1;
        filter: none;
      }
      .${BUTTON_CLASS}[data-active="true"]:hover {
        background: var(--sidebar-secondary, rgba(128, 134, 139, 0.18));
        opacity: 1;
      }
      .${BUTTON_CLASS} .jetski-bucket-icon {
        font-size: 11px;
        line-height: 1;
        display: inline-flex;
        align-items: center;
        justify-content: center;
      }

      /* Style the right-side ⏳ indicator and hide false unread dots while waiting on tasks */
      .${ROW_BG_WAIT_INDICATOR_CLASS} {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: 12.5px;
        line-height: 1;
        flex-shrink: 0;
        user-select: none;
      }
      [data-jetski-bg-waiting="true"] [data-testid="status-unread-dot"],
      [data-jetski-bg-waiting="true"] .status-unread-dot {
        display: none !important;
      }
      .${PROJECT_ROADMAP_BADGE_CLASS} {
        display: inline-flex;
        align-items: center;
        gap: 3px;
        padding: 1px 5px;
        border-radius: 4px;
        border: 1px solid rgba(128, 134, 139, 0.28);
        background: rgba(128, 134, 139, 0.12);
        color: var(--muted-foreground, #b8bdc4);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        font-size: 10.5px;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
        line-height: 1.25;
        flex-shrink: 0;
        white-space: nowrap;
        user-select: none;
      }
      .${PROJECT_ROADMAP_BADGE_CLASS}[data-has-doing="true"] {
        border-color: rgba(138, 180, 248, 0.42);
        background: rgba(138, 180, 248, 0.14);
        color: #8ab4f8;
      }
      #jetski-closed-chats-filter-bubble .jcfb-roadmap-list {
        display: flex;
        flex-direction: column;
        gap: 3px;
        max-height: 240px;
        overflow-y: auto;
        margin-top: 2px;
      }
      #jetski-closed-chats-filter-bubble .jcfb-roadmap-item {
        display: flex;
        align-items: flex-start;
        gap: 5px;
        padding: 2px 4px;
        border-radius: 4px;
        font-size: 11px;
        line-height: 1.35;
      }
      #jetski-closed-chats-filter-bubble .jcfb-roadmap-item[data-status="doing"] {
        background: rgba(138, 180, 248, 0.12);
        color: #d2e3fc;
      }
      #jetski-closed-chats-filter-bubble .jcfb-roadmap-glyph {
        flex-shrink: 0;
        font-weight: 600;
      }
      #jetski-closed-chats-filter-bubble .jcfb-roadmap-id {
        flex-shrink: 0;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
      }
      #jetski-closed-chats-filter-bubble .jcfb-roadmap-text {
        flex: 1 1 auto;
        min-width: 0;
        word-break: break-word;
      }
      #jetski-closed-chats-filter-bubble {
        position: fixed;
        z-index: 2147483647;
        width: max-content;
        max-width: 320px;
        padding: 4px 8px;
        border-radius: 6px;
        background: var(--glass, var(--card, var(--secondary, #2d2d2d)));
        color: var(--secondary-foreground, var(--foreground, #cccccc));
        border: 1px solid var(--glass-border, var(--border, rgba(128, 128, 128, 0.2)));
        box-shadow: inset 0 1px 0 var(--glass-highlight, transparent),
          var(--glass-shadow, 0 4px 6px -1px rgba(0, 0, 0, 0.05));
        -webkit-backdrop-filter: blur(20px) saturate(1.6);
        backdrop-filter: blur(20px) saturate(1.6);
        font-family: var(
          --font-sans,
          -apple-system,
          BlinkMacSystemFont,
          "Segoe UI",
          Roboto,
          sans-serif
        );
        font-size: 12px;
        line-height: 1.4;
        user-select: none;
        pointer-events: none;
        opacity: 0;
        transform: translateY(-2px);
        transition: opacity 110ms ease, transform 110ms ease;
      }
      #jetski-closed-chats-filter-bubble[data-mode="roadmap"] {
        width: 320px;
        padding: 10px 12px;
        border-radius: 8px;
        font-size: 11.5px;
        line-height: 1.45;
      }
      /* Context breakdown card width inside the shared floating glass tooltip */
      #jetski-closed-chats-filter-bubble[data-mode="context"] {
        width: 312px;
        padding: 10px 12px;
        border-radius: 8px;
        font-size: 11.5px;
        line-height: 1.45;
      }
      #jetski-closed-chats-filter-bubble .jcfb-ctx-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        padding: 1.5px 0;
        font-variant-numeric: tabular-nums;
      }
      #jetski-closed-chats-filter-bubble .jcfb-ctx-val {
        color: var(--muted-foreground, #b8bdc4);
        text-align: right;
        word-break: break-word;
      }
      #jetski-closed-chats-filter-bubble .jcfb-ctx-footer {
        margin-top: 6px;
        padding-top: 6px;
        border-top: 1px solid rgba(128, 134, 139, 0.22);
        font-size: 10.5px;
        color: var(--muted-foreground, #9aa0a6);
      }
      #jetski-closed-chats-filter-bubble[data-visible="true"] {
        opacity: 1;
        transform: translateY(0);
      }
      #jetski-closed-chats-filter-bubble .jcfb-pill {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        white-space: nowrap;
        font-weight: 500;
      }
      #jetski-closed-chats-filter-bubble .jcfb-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 6px;
        font-weight: 600;
        font-size: 12px;
        margin-bottom: 4px;
      }
      #jetski-closed-chats-filter-bubble .jcfb-header-left {
        display: flex;
        align-items: center;
        gap: 6px;
      }
      #jetski-closed-chats-filter-bubble .jcfb-dot {
        width: 7px;
        height: 7px;
        border-radius: 999px;
        flex-shrink: 0;
      }
      #jetski-closed-chats-filter-bubble .jcfb-desc {
        font-size: 11px;
        color: var(--muted-foreground, #b8bdc4);
        padding-bottom: 7px;
        margin-bottom: 6px;
        border-bottom: 1px solid rgba(128, 134, 139, 0.22);
      }
      #jetski-closed-chats-filter-bubble .jcfb-section-title {
        font-size: 10.5px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--muted-foreground, #9aa0a6);
        margin: 4px 0 3px 0;
      }
      .${CHAIN_STARS_GROUP_CLASS} {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 2px;
        pointer-events: auto;
        /* Remain visible when narrow-pane responsive rules apply invisible to nav */
        visibility: visible;
        z-index: 15;
      }
      .jetski-chain-stars-before::after,
      .jetski-chain-stars-after::before {
        content: "";
        display: block;
        width: 1.5px;
        height: 6px;
        background: rgba(156, 163, 175, 0.4);
        margin: 2px 0;
        border-radius: 1px;
      }
      .${CHAIN_STAR_BTN_CLASS} {
        background: none;
        border: none;
        padding: 1px 3px;
        cursor: pointer;
        border-radius: 4px;
        line-height: 1;
        transition: transform 0.15s, color 0.15s;
        font-size: 13px;
        color: rgba(156, 163, 175, 0.7);
        outline: none;
      }
      .${CHAIN_STAR_BTN_CLASS}[data-active="true"] {
        color: #f59e0b;
        font-size: 14px;
        filter: drop-shadow(0 0 3px rgba(245, 158, 11, 0.6));
      }
      .${CHAIN_STAR_BTN_CLASS}:hover {
        transform: scale(1.25);
        color: #fbbf24;
      }
      #${STAR_TOOLTIP_ID} {
        position: fixed;
        z-index: 2147483647;
        width: 288px;
        padding: 9px 12px;
        border-radius: 8px;
        background: var(--glass, var(--card, var(--secondary, #2d2d2d)));
        color: var(--secondary-foreground, var(--foreground, #cccccc));
        border: 1px solid var(--glass-border, var(--border, rgba(128, 128, 128, 0.2)));
        box-shadow: inset 0 1px 0 var(--glass-highlight, transparent),
          var(--glass-shadow, 0 4px 6px -1px rgba(0, 0, 0, 0.05));
        -webkit-backdrop-filter: blur(20px) saturate(1.6);
        backdrop-filter: blur(20px) saturate(1.6);
        font-family: var(
          --font-sans,
          -apple-system,
          BlinkMacSystemFont,
          "Segoe UI",
          Roboto,
          sans-serif
        );
        font-size: 11.5px;
        line-height: 1.45;
        pointer-events: none;
        opacity: 0;
        transform: translateY(-2px);
        transition: opacity 110ms ease, transform 110ms ease;
      }
      #${STAR_TOOLTIP_ID}[data-visible="true"] {
        opacity: 1;
        transform: translateY(0);
      }
      .${ARCHIVE_BANNER_CLASS} {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 9px 14px;
        border-radius: 12px;
        background: rgba(128, 134, 139, 0.12);
        border: 1px solid rgba(128, 134, 139, 0.28);
        font-size: 12px;
        width: 100%;
        box-sizing: border-box;
        margin-bottom: 4px;
      }
      .${JUMP_TO_TAIL_BTN_CLASS} {
        cursor: pointer;
        padding: 4px 11px;
        border-radius: 6px;
        background: #8ab4f8;
        color: #1e1e1e;
        font-weight: 600;
        font-size: 11.5px;
        border: none;
        white-space: nowrap;
        transition: background 0.15s;
      }
      .${JUMP_TO_TAIL_BTN_CLASS}:hover {
        background: #aecbfa;
      }
      #${AUTOPILOT_TOAST_ID} {
        position: fixed;
        bottom: 24px;
        right: 24px;
        z-index: 2147483646;
        padding: 8px 14px;
        border-radius: 8px;
        background: var(--background, #1e1e1e);
        color: var(--foreground, #e8eaed);
        border: 1px solid rgba(138, 180, 248, 0.45);
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
        font-family: var(
          --font-sans,
          -apple-system,
          BlinkMacSystemFont,
          "Segoe UI",
          Roboto,
          sans-serif
        );
        font-size: 12px;
        font-weight: 500;
        pointer-events: none;
      }
    ` +
      // Style the collapsible Needs-Reaction inbox widget above the sidebar project list
      `
      /* Hide redundant new-conversation button to pull history and inbox up by ~50px */
      [aria-label="Sidebar"] [data-testid="new-conversation-button"] {
        display: none !important;
      }
      [aria-label="Sidebar"] > div:first-child {
        margin-bottom: 4px !important;
      }
      [aria-label="Sidebar"] > div.mt-3.h-px {
        margin-top: 6px !important;
        background-color: rgba(128, 128, 128, 0.14) !important;
      }
      [aria-label="Sidebar"] div.overflow-y-auto:has(.${INBOX_CLASS}) {
        gap: 6px !important;
        padding-top: 6px !important;
        padding-bottom: 0 !important;
      }
      /* Make inbox parent a flex column so conversation-list-sidebar never overflows bottom */
      div:has(> .${INBOX_CLASS}) {
        display: flex !important;
        flex-direction: column !important;
      }
      div:has(> .${INBOX_CLASS}) > [data-testid="conversation-list-sidebar"],
      div:has(> .${INBOX_CLASS}) > [data-sidebar-scroll-container="true"] {
        flex: 1 1 0% !important;
        min-height: 0 !important;
        height: auto !important;
      }
      /* Borderless hairline section replaces the outdated rounded card box */
      .${INBOX_CLASS} {
        display: flex;
        flex-direction: column;
        margin: 2px 8px 6px 8px;
        padding: 3px 0 2px 0;
        border-radius: 0;
        background: transparent;
        border: none;
        border-top: 1px solid rgba(128, 128, 128, 0.16);
        border-bottom: 1px solid rgba(128, 128, 128, 0.16);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        font-size: 11.5px;
        line-height: 1.3;
        overflow: hidden;
        flex-shrink: 0;
        user-select: none;
      }
      .${INBOX_CLASS}[data-urgent="true"] {
        border-top-color: rgba(128, 128, 128, 0.22);
      }
      .jetski-inbox-empty-row {
        padding: 4px 8px;
        color: var(--muted-foreground, #9aa0a6);
        font-size: 11px;
      }
      .${INBOX_HEADER_CLASS} {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 4px;
        padding: 0 4px 2px 6px;
        min-height: 22px;
        background: transparent;
        border-bottom: 1px solid rgba(128, 128, 128, 0.10);
        font-size: 11px;
        color: var(--foreground, #e8eaed);
      }
      .${INBOX_CLASS}[data-collapsed="true"] .${INBOX_HEADER_CLASS} {
        border-bottom-color: transparent;
        padding-bottom: 0;
      }
      .${INBOX_HEADER_CLASS} .jetski-inbox-header-left {
        display: flex;
        align-items: center;
        gap: 10px;
        min-width: 0;
        flex-wrap: nowrap;
        overflow: hidden;
      }
      .${INBOX_TAB_BTN_CLASS} {
        position: relative;
        display: inline-flex;
        align-items: baseline;
        gap: 3.5px;
        padding: 2px 0 4px 0;
        margin-bottom: -3px;
        border-radius: 0;
        border: none;
        border-bottom: 1.5px solid transparent;
        background: transparent;
        color: rgba(154, 160, 166, 0.72);
        font-family: inherit;
        font-size: 11px;
        font-weight: 500;
        letter-spacing: -0.005em;
        white-space: nowrap;
        cursor: pointer;
        user-select: none;
        transition: color 100ms ease, border-color 100ms ease;
      }
      .${INBOX_TAB_BTN_CLASS}:hover {
        color: var(--foreground, #e8eaed);
      }
      .${INBOX_TAB_BTN_CLASS}[data-active="true"] {
        color: var(--foreground, #e8eaed);
        font-weight: 600;
        border-bottom-color: rgba(232, 234, 237, 0.85);
      }
      .${INBOX_CLASS}[data-urgent="true"] .jetski-inbox-tab-needs-me[data-active="true"] {
        border-bottom-color: #f87171;
      }
      .${INBOX_TAB_BTN_CLASS} .jetski-inbox-count-badge {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 0;
        border-radius: 0;
        background: transparent;
        font-size: 10px;
        font-weight: 500;
        font-variant-numeric: tabular-nums;
        line-height: 1.3;
        opacity: 0.62;
      }
      .${INBOX_TAB_BTN_CLASS}[data-active="true"] .jetski-inbox-count-badge {
        opacity: 0.88;
      }
      .${INBOX_TAB_BTN_CLASS} .jetski-inbox-urgent-pill {
        display: inline-flex;
        align-items: center;
        gap: 1px;
        padding: 0;
        border-radius: 0;
        background: transparent;
        color: #f87171;
        font-size: 10px;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
        line-height: 1.3;
      }
      .${INBOX_TOGGLE_BTN_CLASS} {
        background: transparent;
        border: none;
        padding: 0 2px;
        cursor: pointer;
        color: var(--muted-foreground, #9aa0a6);
        opacity: 0.55;
        font-size: 10px;
        line-height: 1;
        flex-shrink: 0;
      }
      .${INBOX_LIST_CLASS} {
        display: flex;
        flex-direction: column;
        max-height: 108px;
        overflow-y: auto;
        padding: 2px 0 0 0;
      }
      .${INBOX_CLASS}[data-collapsed="true"] .${INBOX_LIST_CLASS} {
        display: none !important;
      }
      .${INBOX_ROW_CLASS} {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 5px;
        padding: 2.5px 4px 2.5px 6px;
        cursor: pointer;
        min-height: 22px;
        border-radius: 0;
        box-sizing: border-box;
        transition: background-color 100ms ease;
      }
      .${INBOX_ROW_CLASS}:hover {
        background: rgba(128, 134, 139, 0.14);
      }
      .${INBOX_ROW_CLASS}[data-tier="urgent"] {
        box-shadow: inset 2px 0 0 #f87171;
        background: rgba(248, 113, 113, 0.04);
      }
      .${INBOX_ROW_CLASS}[data-tier="urgent"]:hover {
        background: rgba(248, 113, 113, 0.10);
      }
      .${INBOX_ROW_CLASS} .jetski-inbox-row-main {
        display: flex;
        align-items: center;
        gap: 5px;
        flex: 1 1 auto;
        min-width: 0;
      }
      .${INBOX_ROW_CLASS} .jetski-inbox-tier-icon {
        flex-shrink: 0;
        font-size: 10px;
        line-height: 1;
      }
      .${INBOX_ROW_CLASS}[data-tab="needs-me"] .jetski-inbox-tier-icon {
        display: none;
      }
      .${INBOX_ROW_CLASS} .jetski-inbox-project {
        flex: 0 0 auto;
        max-width: 72px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        padding: 0;
        border-radius: 0;
        background: transparent;
        color: rgba(154, 160, 166, 0.72);
        font-size: 10px;
        font-weight: 500;
      }
      .${INBOX_ROW_CLASS} .jetski-inbox-project::after {
        content: " ·";
        opacity: 0.55;
      }
      .${INBOX_ROW_CLASS} .jetski-inbox-text-col {
        display: flex;
        flex-direction: column;
        flex: 1 1 auto;
        min-width: 0;
        gap: 0.5px;
      }
      .${INBOX_ROW_CLASS} .jetski-inbox-title {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        color: var(--foreground, #e8eaed);
        font-size: 11.5px;
      }
      .${INBOX_ROW_CLASS}[data-tier="urgent"] .jetski-inbox-title {
        font-weight: 500;
      }
      .${INBOX_ROW_CLASS} .jetski-inbox-detail {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        font-size: 10px;
        line-height: 1.2;
        color: #f87171;
        opacity: 0.9;
      }
      .${INBOX_ROW_CLASS} .jetski-inbox-time {
        flex-shrink: 0;
        color: rgba(154, 160, 166, 0.65);
        font-size: 10px;
        font-variant-numeric: tabular-nums;
      }
      .${INBOX_ROW_CLASS} .jetski-inbox-actions {
        display: inline-flex;
        align-items: center;
        gap: 1px;
        flex-shrink: 0;
      }
      .${INBOX_SEEN_BTN_CLASS},
      .${INBOX_ARCHIVE_BTN_CLASS} {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 18px;
        height: 18px;
        padding: 0;
        border: none;
        border-radius: 3px;
        background: transparent;
        color: var(--muted-foreground, #9aa0a6);
        font-size: 10.5px;
        line-height: 1;
        cursor: pointer;
        opacity: 0.55;
      }
      .${INBOX_ARCHIVE_BTN_CLASS} {
        filter: grayscale(1);
        font-size: 10px;
      }
      .${INBOX_SEEN_BTN_CLASS}:hover,
      .${INBOX_ARCHIVE_BTN_CLASS}:hover {
        opacity: 1;
        background: rgba(128, 134, 139, 0.20);
        color: var(--foreground, #e8eaed);
      }
    `;
    if (style.textContent !== nextCss) {
      style.textContent = nextCss;
    }
  }

  function syncDomRowsAndPlaceholders(reduxActive) {
    const anyHidden = visibleBuckets.size < BUCKET_ORDER.length;

    const store = findReduxStore();
    const summariesMap = store?.getState()?.trajectorySummaries?.summaries || {};
    const childIndex = buildDescendantIndex(summariesMap);

    // Fallback DOM row tagging for rows rendered before Redux hook or outside standard slices
    const rowEls = document.querySelectorAll('div[data-sidebar-row-id]');
    const domStats = emptyBreakdown();
    let domHiddenCount = 0;
    for (const rowEl of rowEls) {
      const rowId = rowEl.getAttribute('data-sidebar-row-id') || '';
      // Read the raw title so a temporary ⏳ display swap never alters bucket classification
      const title = getRawDomTitle(rowEl);
      const bucket = classifyChatBucket(
        summariesMap[rowId],
        title,
        rowId,
        summariesMap,
        childIndex
      );
      countBucket(domStats, bucket);

      if (!visibleBuckets.has(bucket)) {
        domHiddenCount++;
        if (rowEl.getAttribute('data-jetski-closed-row') !== 'true') {
          rowEl.setAttribute('data-jetski-closed-row', 'true');
        }
      } else if (rowEl.hasAttribute('data-jetski-closed-row')) {
        rowEl.removeAttribute('data-jetski-closed-row');
      }
    }

    if (!reduxActive) {
      totalHiddenCount = domHiddenCount;
      breakdownStats = domStats;
    }

    // Clarify empty project placeholders when all chats in an expanded project were filtered out
    const placeholderContainers = document.querySelectorAll(
      'div[data-sidebar-section-drop-id]:not([data-sidebar-row-id])'
    );
    for (const container of placeholderContainers) {
      const projectId = container.getAttribute('data-sidebar-section-drop-id');
      if (!projectId) continue;
      const span = container.querySelector('span');
      if (!span) continue;
      const text = span.textContent || '';
      if (
        text !== 'No conversations yet' &&
        !span.hasAttribute('data-jetski-orig-placeholder')
      ) {
        continue;
      }
      if (!span.hasAttribute('data-jetski-orig-placeholder')) {
        span.setAttribute('data-jetski-orig-placeholder', text);
      }
      const origText = span.getAttribute('data-jetski-orig-placeholder') || 'No conversations yet';
      const hiddenInProject = hiddenCountsByProject.get(projectId) || 0;
      if (anyHidden && hiddenInProject > 0) {
        const nextText = `No matching chats (${hiddenInProject} hidden)`;
        if (span.textContent !== nextText) {
          span.textContent = nextText;
        }
      } else if (span.textContent !== origText) {
        span.textContent = origText;
      }
    }
  }

  function buildBubbleHtml(hoveredBucketKey) {
    const bucketKey = BUCKET_DEFS[hoveredBucketKey] ? hoveredBucketKey : 'needs-me';
    const def = BUCKET_DEFS[bucketKey];
    const isShown = visibleBuckets.has(bucketKey);
    const dotColor = isShown ? '#34d399' : '#9ca3af';
    const stateWord = isShown ? 'shown' : 'hidden';
    const bucketCount = breakdownStats[bucketKey] || 0;

    return (
      `<div class="jcfb-pill">` +
      `<span class="jcfb-dot" style="background:${dotColor}"></span>` +
      `<span>${def.title} · ${bucketCount} ${stateWord}</span>` +
      `</div>`
    );
  }

  function ensureBubbleElement() {
    let bubble = document.getElementById('jetski-closed-chats-filter-bubble');
    if (!bubble && document.body) {
      bubble = document.createElement('div');
      bubble.id = 'jetski-closed-chats-filter-bubble';
      bubble.className = 'compact-tooltip surface-glass';
      bubble.setAttribute('data-visible', 'false');
      document.body.appendChild(bubble);
    }
    return bubble;
  }

  // Positions a compact pill bubble centered above btn, flipping below only if clipped at top
  function positionCompactBubble(bubble, btn) {
    const rect = btn.getBoundingClientRect();
    const bubbleWidth = bubble.offsetWidth || 180;
    const bubbleHeight = bubble.offsetHeight || 26;
    const btnWidth = rect.width || Math.max(0, (rect.right || 0) - (rect.left || 0)) || 20;
    let left = rect.left + btnWidth / 2 - bubbleWidth / 2;
    // Clamp horizontal position so right-aligned titlebar buttons never overflow the viewport
    if (left + bubbleWidth + 8 > window.innerWidth) {
      left = Math.max(8, window.innerWidth - bubbleWidth - 8);
    }
    left = Math.max(8, left);
    let top = rect.top - bubbleHeight - 6;
    if (top < 6) {
      top = rect.bottom + 6;
    }
    bubble.style.left = `${Math.round(left)}px`;
    bubble.style.top = `${Math.round(top)}px`;
  }

  // Clears active context badge ownership when another tooltip takes over or hides
  function clearContextHover() {
    activeHoverContextBadge = null;
    activeHoverContextCascadeId = null;
  }

  function showBubbleForButton(btn, bucketKey) {
    activeHoverBtn = btn;
    activeHoverBucket = bucketKey || btn?.getAttribute('data-bucket') || 'needs-me';
    // Clear any active roadmap badge hover state since the shared bubble now renders bucket stats
    activeHoverRoadmapBadge = null;
    activeHoverRoadmapProjectId = null;
    clearContextHover();
    const bubble = ensureBubbleElement();
    if (!bubble || !btn) return;
    if (bubble.getAttribute('data-mode') !== 'filter') {
      bubble.setAttribute('data-mode', 'filter');
    }
    const nextBubbleHtml = buildBubbleHtml(activeHoverBucket);
    if (bubble.innerHTML !== nextBubbleHtml) {
      bubble.innerHTML = nextBubbleHtml;
    }
    if (bubble.getAttribute('data-visible') !== 'true') {
      bubble.setAttribute('data-visible', 'true');
    }
    positionCompactBubble(bubble, btn);
  }

  function hideBubble() {
    activeHoverBtn = null;
    activeHoverBucket = null;
    // Reset both bucket and roadmap hover owners when hiding the shared bubble
    activeHoverRoadmapBadge = null;
    activeHoverRoadmapProjectId = null;
    clearContextHover();
    const bubble = document.getElementById('jetski-closed-chats-filter-bubble');
    if (bubble && bubble.getAttribute('data-visible') !== 'false') {
      bubble.setAttribute('data-visible', 'false');
    }
  }

  // Displays the shared surface-glass pill tooltip anchored under a titlebar icon button
  function showActionPillBubble(btn, labelText) {
    if (!btn || !labelText) return;
    activeHoverBtn = null;
    activeHoverBucket = null;
    activeHoverRoadmapBadge = null;
    activeHoverRoadmapProjectId = null;
    clearContextHover();
    const bubble = ensureBubbleElement();
    if (!bubble) return;
    // Reuse the compact 1-line filter pill layout for icon button tooltips
    if (bubble.getAttribute('data-mode') !== 'filter') {
      bubble.setAttribute('data-mode', 'filter');
    }
    const nextBubbleHtml = `<div class="jcfb-pill"><span>${escapeHtml(labelText)}</span></div>`;
    if (bubble.innerHTML !== nextBubbleHtml) {
      bubble.innerHTML = nextBubbleHtml;
    }
    if (bubble.getAttribute('data-visible') !== 'true') {
      bubble.setAttribute('data-visible', 'true');
    }
    positionCompactBubble(bubble, btn);
  }

  // Wires hover, focus, and press listeners on an icon button to show/hide the pill bubble
  function bindHoverBubble(btn, getLabel) {
    const show = () => {
      const label = typeof getLabel === 'function' ? getLabel() : getLabel;
      showActionPillBubble(btn, label);
    };
    btn.addEventListener('mouseenter', show);
    btn.addEventListener('mouseleave', hideBubble);
    btn.addEventListener('focus', show);
    btn.addEventListener('blur', hideBubble);
    btn.addEventListener('pointerdown', hideBubble);
  }

  function escapeHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // Formats compact open counts (▸doing ☐todo) and tooltip summary; returns null when 0 open
  function formatProjectRoadmapInfo(summary) {
    if (!summary || typeof summary !== 'object') return null;
    const doing = Math.max(0, Number(summary.doing) || 0);
    const todo = Math.max(0, Number(summary.todo) || 0);
    const done = Math.max(0, Number(summary.done) || 0);
    const dropped = Math.max(0, Number(summary.dropped) || 0);
    const open = doing + todo;
    if (open <= 0) return null;
    const total = Math.max(open, Number(summary.total) || done + open);
    const parts = [];
    if (doing > 0) parts.push(`▸${doing}`);
    if (todo > 0) parts.push(`☐${todo}`);
    const badgeText = parts.join(' ');
    const breakdownParts = [`${done} done`, `${doing} doing`, `${todo} todo`];
    if (dropped > 0) {
      breakdownParts.push(`${dropped} dropped`);
    }
    const breakdownText = breakdownParts.join(' · ');
    const headerLine = `${open} open of ${total} (${breakdownText})`;
    const openItems = Array.isArray(summary.openItems) ? summary.openItems : [];
    const itemLines = openItems.map((it) => {
      const depth = Math.max(0, Number(it?.depth) || 0);
      const indent = '  '.repeat(depth);
      const glyph = it?.status === 'doing' ? '▸' : '☐';
      const ownerSuffix = it?.status === 'doing' && it?.owner ? ` [${it.owner}]` : '';
      return `${indent}${glyph} ${it?.id || ''} ${it?.text || ''}${ownerSuffix}`;
    });
    const tooltipText =
      itemLines.length > 0 ? `${headerLine}\n${itemLines.join('\n')}` : headerLine;
    return {
      doing,
      todo,
      done,
      dropped,
      open,
      total,
      badgeText,
      breakdownText,
      headerLine,
      tooltipText,
      ariaLabel: `Roadmap: ${headerLine}`,
      openItems,
    };
  }

  function buildRoadmapBubbleHtml(info) {
    const dotColor = info.doing > 0 ? '#8ab4f8' : '#9ca3af';
    const itemsHtml = info.openItems
      .map((it) => {
        const status = it?.status === 'doing' ? 'doing' : 'todo';
        const glyph = status === 'doing' ? '▸' : '☐';
        const depth = Math.max(0, Number(it?.depth) || 0);
        const padLeft = 4 + depth * 12;
        const ownerSuffix =
          status === 'doing' && it?.owner ? ` [${escapeHtml(it.owner)}]` : '';
        return (
          `<div class="jcfb-roadmap-item" data-status="${status}" ` +
          `style="padding-left:${padLeft}px">` +
          `<span class="jcfb-roadmap-glyph">${glyph}</span>` +
          `<span class="jcfb-roadmap-id">${escapeHtml(it?.id || '')}</span>` +
          `<span class="jcfb-roadmap-text">${escapeHtml(it?.text || '')}${ownerSuffix}</span>` +
          `</div>`
        );
      })
      .join('');
    const listBlock = itemsHtml
      ? `<div class="jcfb-section-title">Open roadmap items</div>` +
        `<div class="jcfb-roadmap-list">${itemsHtml}</div>`
      : '';
    return (
      `<div class="jcfb-header">` +
      `<span class="jcfb-header-left">` +
      `<span class="jcfb-dot" style="background:${dotColor}"></span>` +
      `<span>Project Roadmap</span>` +
      `</span>` +
      `<span>[${info.open} open of ${info.total}]</span>` +
      `</div>` +
      `<div class="jcfb-desc">${escapeHtml(info.breakdownText)}</div>` +
      listBlock
    );
  }

  function showRoadmapBubbleForBadge(badge, projectId) {
    const summary = roadmapIndexCache?.projects?.[projectId];
    const info = formatProjectRoadmapInfo(summary);
    if (!badge || !info) {
      hideBubble();
      return;
    }
    activeHoverBtn = null;
    activeHoverBucket = null;
    activeHoverRoadmapBadge = badge;
    activeHoverRoadmapProjectId = projectId;
    // Release any hovered context pill before reusing the shared tooltip for roadmap items
    clearContextHover();
    const bubble = ensureBubbleElement();
    if (!bubble) return;
    if (bubble.getAttribute('data-mode') !== 'roadmap') {
      bubble.setAttribute('data-mode', 'roadmap');
    }
    const nextHtml = buildRoadmapBubbleHtml(info);
    if (bubble.innerHTML !== nextHtml) {
      bubble.innerHTML = nextHtml;
    }
    if (bubble.getAttribute('data-visible') !== 'true') {
      bubble.setAttribute('data-visible', 'true');
    }

    const rect = badge.getBoundingClientRect();
    const bubbleWidth = 320;
    let left = rect.left;
    if (left + bubbleWidth + 8 > window.innerWidth) {
      left = Math.max(8, window.innerWidth - bubbleWidth - 8);
    }
    left = Math.max(8, left);
    const top = rect.bottom + 6;
    bubble.style.left = `${Math.round(left)}px`;
    bubble.style.top = `${Math.round(top)}px`;
  }

  // Renders or removes the compact open-roadmap badge inside each sidebar project header
  function ensureProjectRoadmapBadges() {
    const sectionEls = document.querySelectorAll(
      'div[data-sidebar-section-drop-id]:not([data-sidebar-row-id])'
    );
    const projectsMap = roadmapIndexCache?.projects || {};
    for (const sectionEl of sectionEls) {
      const projectId = sectionEl.getAttribute('data-sidebar-section-drop-id') || '';
      if (!projectId) continue;
      const headerBtn = sectionEl.querySelector(
        'button[data-testid="project-header"], button[data-project-card]'
      );
      if (!headerBtn) continue;
      const info = formatProjectRoadmapInfo(projectsMap[projectId]);
      let badge = headerBtn.querySelector(`.${PROJECT_ROADMAP_BADGE_CLASS}`);
      if (!info) {
        if (badge) {
          if (activeHoverRoadmapBadge === badge) {
            hideBubble();
          }
          badge.remove();
        }
        continue;
      }
      const titleSpan = headerBtn.querySelector('span.truncate');
      const host = titleSpan?.parentElement || headerBtn;
      if (!badge) {
        badge = document.createElement('span');
        badge.className = PROJECT_ROADMAP_BADGE_CLASS;
        badge.setAttribute('data-testid', 'project-roadmap-badge');
        badge.addEventListener('mouseenter', () => {
          const pid = badge.getAttribute('data-project-id') || projectId;
          showRoadmapBubbleForBadge(badge, pid);
        });
        badge.addEventListener('mouseleave', () => {
          hideBubble();
        });
        badge.addEventListener('focus', () => {
          const pid = badge.getAttribute('data-project-id') || projectId;
          showRoadmapBubbleForBadge(badge, pid);
        });
        badge.addEventListener('blur', () => {
          hideBubble();
        });
        host.appendChild(badge);
      }
      if (badge.getAttribute('data-project-id') !== projectId) {
        badge.setAttribute('data-project-id', projectId);
      }
      const hasDoingStr = info.doing > 0 ? 'true' : 'false';
      if (badge.getAttribute('data-has-doing') !== hasDoingStr) {
        badge.setAttribute('data-has-doing', hasDoingStr);
      }
      if (badge.getAttribute('data-doing') !== String(info.doing)) {
        badge.setAttribute('data-doing', String(info.doing));
      }
      if (badge.getAttribute('data-todo') !== String(info.todo)) {
        badge.setAttribute('data-todo', String(info.todo));
      }
      if (badge.getAttribute('data-open') !== String(info.open)) {
        badge.setAttribute('data-open', String(info.open));
      }
      if (badge.getAttribute('data-total') !== String(info.total)) {
        badge.setAttribute('data-total', String(info.total));
      }
      if (badge.textContent !== info.badgeText) {
        badge.textContent = info.badgeText;
      }
      if (badge.getAttribute('title') !== info.tooltipText) {
        badge.setAttribute('title', info.tooltipText);
      }
      if (badge.getAttribute('aria-label') !== info.ariaLabel) {
        badge.setAttribute('aria-label', info.ariaLabel);
      }
      if (activeHoverRoadmapBadge === badge && activeHoverRoadmapProjectId === projectId) {
        showRoadmapBubbleForBadge(badge, projectId);
      }
    }
  }

  function updateBucketButtonUI(btn, bucketKey) {
    if (!btn || !BUCKET_DEFS[bucketKey]) return;
    const def = BUCKET_DEFS[bucketKey];
    const isShown = visibleBuckets.has(bucketKey);
    const activeStr = isShown ? 'true' : 'false';
    if (btn.getAttribute('data-active') !== activeStr) {
      btn.setAttribute('data-active', activeStr);
    }
    if (btn.getAttribute('aria-pressed') !== activeStr) {
      btn.setAttribute('aria-pressed', activeStr);
    }
    if (btn.hasAttribute('title')) {
      btn.removeAttribute('title');
    }

    const count = breakdownStats[bucketKey] || 0;
    const stateWord = isShown ? 'shown' : 'hidden';
    const labelText = `${def.icon} ${def.shortLabel}: ${count} (${stateWord})`;
    if (btn.getAttribute('aria-label') !== labelText) {
      btn.setAttribute('aria-label', labelText);
    }

    const nextHtml = `<span class="jetski-bucket-icon">${def.icon}</span>`;
    if (btn.innerHTML !== nextHtml) {
      btn.innerHTML = nextHtml;
    }

    if (activeHoverBtn === btn) {
      showBubbleForButton(btn, bucketKey);
    }
  }

  function toggleBucketVisibility(bucketKey, soloMode) {
    if (!BUCKET_DEFS[bucketKey]) return;
    if (soloMode) {
      if (visibleBuckets.size === 1 && visibleBuckets.has(bucketKey)) {
        visibleBuckets = new Set(BUCKET_ORDER);
      } else {
        visibleBuckets = new Set([bucketKey]);
      }
    } else if (visibleBuckets.has(bucketKey)) {
      visibleBuckets.delete(bucketKey);
    } else {
      visibleBuckets.add(bucketKey);
    }
    saveVisibleBuckets(visibleBuckets);
    runFullSync();
  }

  function createBucketButton(bucketKey) {
    const btn = document.createElement('button');
    btn.className = BUTTON_CLASS;
    btn.type = 'button';
    btn.setAttribute('data-bucket', bucketKey);

    let lastPressTs = 0;

    btn.addEventListener('mouseenter', () => {
      showBubbleForButton(btn, bucketKey);
    });
    btn.addEventListener('mouseleave', () => {
      hideBubble();
    });
    btn.addEventListener('focus', () => {
      showBubbleForButton(btn, bucketKey);
    });
    btn.addEventListener('blur', () => {
      hideBubble();
    });

    // Handle left-button press immediately on pointerdown/mousedown so virtualizer
    // re-renders or parent header handlers between mousedown and mouseup never swallow clicks
    btn.addEventListener('pointerdown', (e) => {
      e.stopPropagation();
      if (e.button === 0 && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        lastPressTs = Date.now();
        toggleBucketVisibility(bucketKey, Boolean(e.shiftKey));
        showBubbleForButton(btn, bucketKey);
      }
    });

    btn.addEventListener('mousedown', (e) => {
      e.stopPropagation();
      if (e.button === 0 && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        if (Date.now() - lastPressTs > 300) {
          lastPressTs = Date.now();
          toggleBucketVisibility(bucketKey, Boolean(e.shiftKey));
          showBubbleForButton(btn, bucketKey);
        }
      }
    });

    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (Date.now() - lastPressTs > 300) {
        lastPressTs = Date.now();
        toggleBucketVisibility(bucketKey, Boolean(e.shiftKey));
        showBubbleForButton(btn, bucketKey);
      }
    });

    btn.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      e.stopPropagation();
      toggleBucketVisibility(bucketKey, true);
      showBubbleForButton(btn, bucketKey);
    });

    return btn;
  }


  // Ordered pieces of cascadeId's handoff chain from jetski-chains.json; past cascadeId the
  // chain follows the newest child, the same branch chat_lifecycle.py treats as the tail
  async function chainOf(cascadeId) {
    const { parentOf, createdAt } = await fetchChainIndex();
    const childrenOf = {};
    for (const [child, parent] of Object.entries(parentOf)) {
      (childrenOf[parent] = childrenOf[parent] || []).push(child);
    }
    const chain = [cascadeId];
    const seen = new Set(chain);
    for (let cur = parentOf[cascadeId]; cur && !seen.has(cur); cur = parentOf[cur]) {
      seen.add(cur);
      chain.unshift(cur);
    }
    for (let cur = cascadeId; childrenOf[cur]; ) {
      const next = childrenOf[cur]
        .filter((c) => !seen.has(c))
        .sort((x, y) => String(createdAt[x] || '').localeCompare(String(createdAt[y] || '')))
        .pop();
      if (!next) break;
      seen.add(next);
      chain.push(next);
      cur = next;
    }
    return chain;
  }

  // Only the user's messages and the agent's replies are searched; thinking and tool output
  // would bury them
  function stepSearchText(step) {
    if (step?.type === 'CORTEX_STEP_TYPE_USER_INPUT') {
      return { who: 'user', text: step.userInput?.userResponse || '' };
    }
    if (step?.type === 'CORTEX_STEP_TYPE_PLANNER_RESPONSE') {
      const pr = step.plannerResponse || {};
      return { who: 'agent', text: pr.modifiedResponse || pr.response || '' };
    }
    return null;
  }

  async function fetchChainSteps(cascadeId) {
    const resp = await window.fetch(
      '/exa.language_server_pb.LanguageServerService/GetCascadeTrajectorySteps',
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-codeium-csrf-token': getCsrfToken(),
        },
        body: JSON.stringify({ cascadeId }),
      }
    );
    if (!resp.ok) throw new Error(`HTTP ${resp.status} ${await resp.text()}`);
    return (await resp.json())?.steps || [];
  }

  // Paragraph around the match, so most hits read without opening the piece
  function matchParagraph(text, at, len) {
    const brk = text.lastIndexOf('\n\n', at);
    const start = brk < 0 ? 0 : brk + 2;
    const endIdx = text.indexOf('\n\n', at + len);
    let para = text.slice(start, endIdx < 0 ? text.length : endIdx);
    let offset = at - start;
    if (para.length > 600) {
      const from = Math.max(0, offset - 250);
      para = (from > 0 ? '…' : '') + para.slice(from, from + 500) + '…';
      offset = offset - from + (from > 0 ? 1 : 0);
    }
    return (
      escapeHtml(para.slice(0, offset)) + '<mark>' + escapeHtml(para.slice(offset, offset + len)) +
      '</mark>' + escapeHtml(para.slice(offset + len))
    );
  }

  // Native Find in Conversation keys its state by cascadeId; it finds and scrolls to the term
  // once the routed conversation view mounts
  function showNativeFind(cascadeId, term) {
    const store = findReduxStore();
    if (!store) return;
    store.dispatch({
      type: 'findInPane/setIsFindBarVisible',
      payload: { tabId: cascadeId, isFindBarVisible: true },
    });
    store.dispatch({
      type: 'findInPane/setSearchTerm',
      payload: { tabId: cascadeId, searchTerm: term },
    });
  }

  // Restores an archived project when navigating to one of its conversations
  async function unarchiveProjectIfArchived(projectId, store) {
    if (!projectId || projectId === '__standalone__') return false;
    const state = store?.getState();
    const sidebarProjects = state?.sidebarProjects?.projects;
    if (Array.isArray(sidebarProjects) && sidebarProjects.some((p) => p?.id === projectId)) {
      return false;
    }
    const projectsMap = state?.projects?.projects || state?.projects?.byId;
    if (projectsMap && typeof projectsMap === 'object') {
      const p = projectsMap[projectId];
      if (p && p.archived === false) return false;
    }
    if (typeof window.fetch !== 'function') return false;
    try {
      const resp = await window.fetch(
        '/exa.language_server_pb.LanguageServerService/UpdateProject',
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'x-codeium-csrf-token': getCsrfToken(),
          },
          body: JSON.stringify({
            project: {
              id: projectId,
              archived: false,
            },
          }),
        }
      );
      return Boolean(resp && resp.ok);
    } catch {
      return false;
    }
  }

  // Routes inside the SPA by swapping the conversation ID in the current chat URL; updates both
  // browser history and Redux multiConvoLayout/conversation slices for split-view stability.
  // options.preserveFocus replaces the pane without taking focus from another split pane
  function openConversation(cascadeId, term, sourceCid, options) {
    const preserveFocus = Boolean(options?.preserveFocus);
    const path = window.location?.pathname || '';
    const m = /^(.*\/(?:c|cascade|conversation)\/)([^/?#]+)(.*)$/.exec(path);
    const routePrefix = m ? m[1] : '/c/';
    const routeSuffix = m ? m[3] : '';
    // Extract active conversation IDs from URL pathname, handling split-view '+' separators
    let rawIds = [];
    if (m) {
      let decoded = m[2];
      try {
        decoded = decodeURIComponent(m[2]);
      } catch {}
      rawIds = decoded.split('+').filter(Boolean);
    }
    const searchStr = window.location?.search || '';
    const searchParams = searchStr ? new URLSearchParams(searchStr) : new URLSearchParams();
    const focusedParam = searchParams.get('focused') || '';

    const store = findReduxStore();
    const layoutState = store?.getState()?.multiConvoLayout;
    const convoCascadeId = store?.getState()?.conversation?.cascadeId || '';
    // Target the clicked pane or active conversation so adjacent split panes remain unchanged
    const replaceTarget = sourceCid || focusedParam || convoCascadeId;

    let targetIdx = -1;
    if (replaceTarget) {
      targetIdx = rawIds.indexOf(replaceTarget);
    }
    if (targetIdx === -1 && rawIds.length > 0) {
      targetIdx = 0;
    }

    const replacedList = rawIds.length > 0 ? [...rawIds] : [cascadeId];
    if (targetIdx >= 0 && targetIdx < replacedList.length) {
      replacedList[targetIdx] = cascadeId;
    }

    const nextIds = [];
    const seen = new Set();
    for (const id of replacedList) {
      if (id === '_new' || !seen.has(id)) {
        if (id !== '_new') seen.add(id);
        nextIds.push(id);
      }
    }

    // Preserve non-empty search query params while keeping focused ID aligned with target;
    // preserveFocus keeps the focus on another split pane that the user is working in
    const nextPath = routePrefix + nextIds.join('+') + routeSuffix;
    const keepsFocus = preserveFocus && focusedParam && focusedParam !== replaceTarget;
    if (!keepsFocus && (searchParams.has('focused') || nextIds.length > 1)) {
      searchParams.set('focused', cascadeId);
    }

    // Align active sidebar project section and unarchive project if archived
    const summariesMap = store?.getState()?.trajectorySummaries?.summaries || {};
    const targetProjectId =
      summariesMap[cascadeId]?.trajectoryMetadata?.projectId ||
      store?.getState()?.trajectorySummaries?.externalSummaries?.[cascadeId]?.trajectoryMetadata
        ?.projectId;
    const isNonConvoRoute = !m;
    const shouldSyncSection =
      Boolean(targetProjectId && targetProjectId !== '__standalone__') &&
      (isNonConvoRoute || searchParams.has('section'));
    if (shouldSyncSection) {
      searchParams.set('section', targetProjectId);
    }
    if (targetProjectId && targetProjectId !== '__standalone__') {
      void unarchiveProjectIfArchived(targetProjectId, store);
    }

    const qs = searchParams.toString();
    const nextUrl = nextPath + (qs ? `?${qs}` : '');

    // Increment TanStack Router __TSR_index to preserve history navigation order
    const prevState =
      window.history?.state && typeof window.history.state === 'object'
        ? window.history.state
        : {};
    const nextState =
      typeof prevState.__TSR_index === 'number'
        ? { ...prevState, __TSR_index: prevState.__TSR_index + 1 }
        : { ...prevState };
    window.history.pushState(nextState, '', nextUrl);
    window.dispatchEvent(new PopStateEvent('popstate'));

    // Reconcile Redux layout and active conversation slices to prevent router ping-pong
    if (store) {
      let takeFocus = !preserveFocus || rawIds.length <= 1 || convoCascadeId === replaceTarget;
      if (layoutState?.rootNode) {
        function findPane(node, matches) {
          if (!node) return null;
          if (node.type === 'pane') return matches(node) ? node : null;
          if (Array.isArray(node.children)) {
            for (const child of node.children) {
              const found = findPane(child, matches);
              if (found) return found;
            }
          }
          return null;
        }
        // The pane showing replaceTarget wins over the focused pane, or a background
        // auto-switch would replace whatever chat the user is focused on
        const targetPane =
          (replaceTarget &&
            findPane(
              layoutState.rootNode,
              (n) => n.cascadeId === replaceTarget || n.id === replaceTarget
            )) ||
          findPane(layoutState.rootNode, (n) => n.id === layoutState.focusedPaneId) ||
          findPane(layoutState.rootNode, () => true);
        if (targetPane) {
          takeFocus = !preserveFocus || targetPane.id === layoutState.focusedPaneId;
          store.dispatch({
            type: 'multiConvoLayout/resetPane',
            payload: { paneId: targetPane.id, cascadeId },
          });
          if (takeFocus) {
            store.dispatch({
              type: 'multiConvoLayout/setFocusedPane',
              payload: targetPane.id,
            });
          }
        }
      }
      if (takeFocus) {
        store.dispatch({
          type: 'conversation/setCascadeId',
          payload: { cascadeId },
        });
      }
      // Synchronize button states and conversation piece locks immediately upon routing
      scheduleSync();
    }

    // Reveal and scroll to conversation row in sidebar
    void locateConversationInSidebar(cascadeId);

    // Find computes matches from the mounted view's text, so wait for the routed view to mount
    if (term) {
      setTimeout(() => showNativeFind(cascadeId, term), 600);
    }
  }

  function closeSearchPanel() {
    document.getElementById(SEARCH_PANEL_ID)?.remove();
  }

  let searchSeq = 0;
  // Searches every piece of the chain; steps are fetched once per opened panel
  async function runChatSearch(query, resultsEl, chain, stepCache) {
    const seq = ++searchSeq;
    const needle = query.trim().toLowerCase();
    if (!needle) {
      resultsEl.innerHTML = '';
      return;
    }
    const groups = [];
    try {
      for (let i = chain.length - 1; i >= 0; i--) {
        const cid = chain[i];
        // Cache the promise so searches fired while a fetch is pending share it
        if (!stepCache.has(cid)) {
          stepCache.set(
            cid,
            fetchChainSteps(cid).catch((err) => {
              stepCache.delete(cid);
              throw err;
            })
          );
        }
        const hits = [];
        for (const step of await stepCache.get(cid)) {
          const st = stepSearchText(step);
          if (!st?.text) continue;
          const at = st.text.toLowerCase().indexOf(needle);
          if (at >= 0) hits.push({ who: st.who, html: matchParagraph(st.text, at, needle.length) });
        }
        if (hits.length) groups.push({ cid, piece: i + 1, hits: hits.reverse() });
      }
    } catch (err) {
      if (seq === searchSeq) {
        resultsEl.innerHTML = `<div class="jcs-meta">Search failed: ${escapeHtml(err)}</div>`;
      }
      return;
    }
    // A slower earlier search must not overwrite results of a newer query
    if (seq !== searchSeq) return;
    if (groups.length === 0) {
      resultsEl.innerHTML = '<div class="jcs-meta">No matches</div>';
      return;
    }
    resultsEl.innerHTML = groups
      .map(
        (g) =>
          `<div class="jcs-meta">piece ${g.piece}/${chain.length} · ${g.hits.length} hit(s)</div>` +
          g.hits
            .map(
              (h) =>
                `<div class="jcs-hit" data-cascade-id="${escapeHtml(g.cid)}">` +
                `<div class="jcs-meta">${h.who}</div><div>${h.html}</div></div>`
            )
            .join('')
      )
      .join('');
  }

  // Search window over one chat and all its archived handoff pieces; the chain index is
  // awaited so a cold first open does not silently search only the current piece
  async function openSearchPanel(cascadeId) {
    closeSearchPanel();
    const chain = await chainOf(cascadeId);
    const stepCache = new Map();
    const panel = document.createElement('div');
    panel.id = SEARCH_PANEL_ID;
    const input = document.createElement('input');
    input.type = 'search';
    input.setAttribute(
      'placeholder',
      chain.length > 1 ? `Search this chat and its ${chain.length - 1} earlier piece(s)` :
        'Search this chat'
    );
    const resultsEl = document.createElement('div');
    resultsEl.className = 'jcs-results';
    panel.appendChild(input);
    panel.appendChild(resultsEl);
    document.body.appendChild(panel);
    let timer = 0;
    input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => runChatSearch(input.value, resultsEl, chain, stepCache), 250);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeSearchPanel();
    });
    resultsEl.addEventListener('click', (e) => {
      const hit = e.target.closest('.jcs-hit');
      if (!hit) return;
      const term = input.value.trim();
      const hitCid = hit.getAttribute('data-cascade-id');
      closeSearchPanel();
      manualVisitedCids.add(hitCid);
      openConversation(hitCid, term, cascadeId);
    });
    input.focus();
  }

  // The test harness fake DOM cannot type into the panel, so it drives search directly
  window.__jetskiChatSearch = { runChatSearch, chainOf };

  let locateHighlightTimer = 0;

  function nextFrame() {
    return new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
  }

  // Extracts VirtualizedConversationList props and virtualizer from the sidebar Fiber tree
  function findSidebarListController() {
    const listEl =
      document.querySelector('[data-testid="conversation-list-sidebar"]') ||
      document.querySelector('[data-sidebar-scroll-container="true"]');
    if (!listEl) return null;

    const fiberKey = Object.keys(listEl).find(
      (k) => k.startsWith('__reactFiber$') || k.startsWith('__reactContainer$')
    );
    if (!fiberKey) return { listEl, props: null, virtualizer: null };

    let fiber = listEl[fiberKey];
    let steps = 0;
    let vlistFiber = null;
    while (fiber && steps < 40) {
      steps++;
      if (Array.isArray(fiber.memoizedProps?.items)) {
        vlistFiber = fiber;
        break;
      }
      fiber = fiber.return;
    }

    let virtualizer = null;
    if (vlistFiber) {
      let hook = vlistFiber.memoizedState;
      let hSteps = 0;
      while (hook && hSteps < 40) {
        hSteps++;
        const cand = hook.memoizedState?.current || hook.memoizedState;
        if (cand && typeof cand.scrollToIndex === 'function') {
          virtualizer = cand;
          break;
        }
        hook = hook.next;
      }
    }

    return {
      listEl,
      props: vlistFiber?.memoizedProps || null,
      virtualizer,
    };
  }

  // Reveals, scrolls to, and pulses a conversation row in the left sidebar
  async function locateConversationInSidebar(cascadeId) {
    if (!cascadeId) return;

    // Archived pieces are hidden from the sidebar, so resolve to the live chain tail
    let targetId = cascadeId;
    try {
      const chain = await chainOf(cascadeId);
      if (Array.isArray(chain) && chain.length > 0) {
        targetId = chain[chain.length - 1];
      }
    } catch {
      targetId = cascadeId;
    }

    const store = findReduxStore();

    // Open the left sidebar before querying list DOM or Fiber nodes
    if (store?.getState()?.sideBar?.isOpen === false) {
      store.dispatch({ type: 'sideBar/setIsOpenInternal', payload: true });
      await nextFrame();
      await nextFrame();
    }

    // Unhide the conversation's attention bucket if currently filtered out
    const summariesMap = store?.getState()?.trajectorySummaries?.summaries || {};
    const summary = summariesMap[targetId];
    const rawTitle = summary?.annotations?.title || summary?.summary || '';
    const bucket = classifyChatBucket(summary, rawTitle, targetId, summariesMap);
    if (!visibleBuckets.has(bucket)) {
      visibleBuckets.add(bucket);
      saveVisibleBuckets(visibleBuckets);
      runFullSync();
      await nextFrame();
    }

    let ctrl = findSidebarListController();
    if (!ctrl) {
      await nextFrame();
      ctrl = findSidebarListController();
    }

    const projectId = summary?.trajectoryMetadata?.projectId;

    if (ctrl?.props) {
      let needsRerender = false;

      // Expand collapsed section headers and project folder so the row enters flat items
      if (Array.isArray(ctrl.props.items)) {
        for (const item of ctrl.props.items) {
          if (item.type === 'section-header' && item.isCollapsed) {
            ctrl.props.onToggleSectionHeader?.(item.collapseId || item.id);
            needsRerender = true;
          }
        }
        if (projectId) {
          for (const item of ctrl.props.items) {
            const isMatch =
              item.type === 'header' &&
              (item.id === projectId ||
                item.id === 'header-' + projectId ||
                (typeof item.id === 'string' && item.id.replace(/^header-/, '') === projectId));
            if (isMatch && item.isCollapsed) {
              ctrl.props.onToggleCollapseSection?.(projectId);
              needsRerender = true;
            }
          }
        }
      }

      if (needsRerender) {
        await nextFrame();
        ctrl = findSidebarListController() || ctrl;
      }

      // Expand pagination when the project row is truncated behind "Show N more"
      if (projectId && Array.isArray(ctrl.props?.items)) {
        const hasProjectRow = ctrl.props.items.some(
          (it) =>
            it.type === 'row' &&
            it.cascadeId === targetId &&
            it.groupId !== 'pinned'
        );
        if (!hasProjectRow) {
          const hasShowMore = ctrl.props.items.some(
            (it) => it.type === 'show-more' && it.groupId === projectId
          );
          if (hasShowMore) {
            ctrl.props.onTogglePagination?.(projectId);
            await nextFrame();
            ctrl = findSidebarListController() || ctrl;
          }
        }
      }
    }

    // Prefer the project folder row over the pinned quick-link when scrolling
    if (ctrl?.props && Array.isArray(ctrl.props.items)) {
      let targetIdx = -1;
      for (let i = 0; i < ctrl.props.items.length; i++) {
        const it = ctrl.props.items[i];
        if (it.type === 'row' && it.cascadeId === targetId) {
          if (targetIdx === -1 || (it.groupId && it.groupId !== 'pinned')) {
            targetIdx = i;
            if (it.groupId && it.groupId !== 'pinned') {
              break;
            }
          }
        }
      }
      if (targetIdx >= 0 && ctrl.virtualizer?.scrollToIndex) {
        ctrl.virtualizer.scrollToIndex(targetIdx, { align: 'center' });
        await nextFrame();
        await nextFrame();
      }
    }

    let highlightEl =
      document.querySelector(
        `[data-testid="conversation-row-sidebar"][data-cascade-id="${targetId}"]`
      ) ||
      document.querySelector(`div[data-sidebar-row-id="${targetId}"]`);

    if (!highlightEl) {
      const candidates = document.querySelectorAll('[data-cascade-id], [data-sidebar-row-id]');
      for (const el of candidates) {
        if (
          el.getAttribute('data-cascade-id') === targetId ||
          el.getAttribute('data-sidebar-row-id') === targetId
        ) {
          highlightEl = el;
          break;
        }
      }
    }

    if (highlightEl) {
      if (typeof highlightEl.scrollIntoView === 'function') {
        highlightEl.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
      if (locateHighlightTimer) {
        clearTimeout(locateHighlightTimer);
        locateHighlightTimer = 0;
      }
      for (const prev of document.querySelectorAll(`[${LOCATED_ROW_ATTR}="true"]`)) {
        prev.removeAttribute(LOCATED_ROW_ATTR);
      }
      highlightEl.setAttribute(LOCATED_ROW_ATTR, 'true');
      locateHighlightTimer = setTimeout(() => {
        highlightEl.removeAttribute(LOCATED_ROW_ATTR);
        locateHighlightTimer = 0;
      }, 2300);
      if (locateHighlightTimer && typeof locateHighlightTimer.unref === 'function') {
        locateHighlightTimer.unref();
      }
    }
  }

  window.__jetskiOpenConversation = openConversation;
  window.__jetskiLocateInSidebar = locateConversationInSidebar;

  // Matryoshka chain star tooltip showing lazy piece turn counts and question previews
  let chainTooltipEl = null;
  let activeTooltipBtn = null;
  let tooltipFetchSeq = 0;

  function ensureChainStarTooltip() {
    if (chainTooltipEl && document.body?.contains?.(chainTooltipEl)) return chainTooltipEl;
    chainTooltipEl = document.createElement('div');
    chainTooltipEl.id = STAR_TOOLTIP_ID;
    chainTooltipEl.className = 'compact-tooltip surface-glass';
    chainTooltipEl.setAttribute('data-visible', 'false');
    document.body?.appendChild?.(chainTooltipEl);
    return chainTooltipEl;
  }

  function hideChainStarTooltip() {
    activeTooltipBtn = null;
    if (chainTooltipEl) {
      chainTooltipEl.setAttribute('data-visible', 'false');
      chainTooltipEl.innerHTML = '';
      chainTooltipEl.textContent = '';
    }
  }

  async function showChainStarTooltip(btn, pieceCid, pieceIdx, totalPieces, isCurrent, isTail) {
    activeTooltipBtn = btn;
    const tooltip = ensureChainStarTooltip();
    if (!tooltip) return;
    const rect = typeof btn.getBoundingClientRect === 'function'
      ? btn.getBoundingClientRect()
      : { top: 40, right: 28, height: 16 };
    const top = Math.max(10, (rect.top || 40) + ((rect.height || 16) / 2) - 30);
    const left = (rect.right || 28) + 12;

    tooltip.style.top = `${top}px`;
    tooltip.style.left = `${left}px`;

    const store = findReduxStore();
    const summaryObj = store?.getState()?.trajectorySummaries?.summaries?.[pieceCid] || {};
    const title = summaryObj.summary || pieceCid.slice(0, 12);
    const statusLabel = isCurrent
      ? '<span style="color:#fbbf24;font-weight:600;">Current piece</span>'
      : (isTail
          ? '<span style="color:#8ab4f8;font-weight:600;">Active tail</span>'
          : '<span style="color:#9aa0a6;">Chain archive</span>');

    tooltip.innerHTML = `
      <div style="font-weight:600;font-size:12px;margin-bottom:2px;display:flex;align-items:center;gap:6px;">
        <span>Piece #${pieceIdx + 1} of ${totalPieces}</span>
        <span style="font-size:10px;opacity:0.8;">•</span>
        <span style="font-size:11px;">${statusLabel}</span>
      </div>
      <div style="font-size:11px;color:var(--muted-foreground,#9aa0a6);margin-bottom:6px;word-break:break-word;">
        ${escapeHtml(title)}
      </div>
      <div class="jetski-chain-steps-preview" style="font-size:10.5px;color:#dadce0;border-top:1px solid rgba(128,134,139,0.2);padding-top:4px;">
        Loading messages…
      </div>
    `;
    tooltip.setAttribute('data-visible', 'true');

    const curSeq = ++tooltipFetchSeq;
    try {
      const steps = await fetchChainSteps(pieceCid);
      if (activeTooltipBtn !== btn || curSeq !== tooltipFetchSeq) return;
      const userQuestions = [];
      for (const s of steps) {
        if (s.type === 'CORTEX_STEP_TYPE_USER_INPUT' && s.userInput?.userResponse) {
          let text = s.userInput.userResponse.trim();
          text = text.replace(/^(?:continuing\s+conversation:[^\n]+|\[handoff[^\n]+)\n+/i, '').trim();
          if (text) userQuestions.push(text);
        }
      }
      const previewEl = tooltip.querySelector?.('.jetski-chain-steps-preview');
      if (!previewEl) return;
      if (userQuestions.length === 0) {
        previewEl.innerHTML = '<span style="opacity:0.6;">0 user prompts</span>';
      } else if (userQuestions.length === 1) {
        previewEl.innerHTML = `
          <div style="font-weight:600;margin-bottom:2px;">1 user prompt:</div>
          <div style="opacity:0.9;font-style:italic;">"${escapeHtml(userQuestions[0].slice(0, 120))}${userQuestions[0].length > 120 ? '…' : ''}"</div>
        `;
      } else {
        const first = userQuestions[0].slice(0, 90) + (userQuestions[0].length > 90 ? '…' : '');
        const last = userQuestions[userQuestions.length - 1].slice(0, 90) +
          (userQuestions[userQuestions.length - 1].length > 90 ? '…' : '');
        previewEl.innerHTML = `
          <div style="font-weight:600;margin-bottom:2px;">User prompts: ${userQuestions.length}</div>
          <div style="margin-bottom:2px;"><span style="opacity:0.6;">First:</span> "${escapeHtml(first)}"</div>
          <div><span style="opacity:0.6;">Last:</span> "${escapeHtml(last)}"</div>
        `;
      }
    } catch {
      if (activeTooltipBtn !== btn || curSeq !== tooltipFetchSeq) return;
      const previewEl = tooltip.querySelector?.('.jetski-chain-steps-preview');
      if (previewEl) {
        previewEl.innerHTML = '<span style="opacity:0.5;">Steps unavailable</span>';
      }
    }
  }

  // Intercepts pointerdown/mousedown/click with a 300ms debounce so buttons navigate reliably
  function bindActionBtnHandlers(btn, onTrigger) {
    let lastPressTs = 0;
    const triggerOnce = (e) => {
      e.preventDefault();
      e.stopPropagation();
      // Debounce rapid multi-event dispatches across pointerdown, mousedown, and click
      if (Date.now() - lastPressTs > 300) {
        lastPressTs = Date.now();
        onTrigger();
      }
    };
    // Trigger on pointerdown before potential DOM replacement between mousedown and mouseup
    btn.addEventListener('pointerdown', (e) => {
      e.stopPropagation();
      if (e.button === 0 && !e.ctrlKey && !e.metaKey) {
        triggerOnce(e);
      }
    });
    btn.addEventListener('mousedown', (e) => {
      e.stopPropagation();
      if (e.button === 0 && !e.ctrlKey && !e.metaKey) {
        triggerOnce(e);
      }
    });
    btn.addEventListener('click', (e) => {
      if (!e.ctrlKey && !e.metaKey) {
        triggerOnce(e);
      }
    });
  }

  function createStarButton(cid, idx, chain, isCurrent, isTail, sourceCid) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = CHAIN_STAR_BTN_CLASS;
    btn.setAttribute('data-testid', 'jetski-chain-star-btn');
    btn.setAttribute('data-piece-id', cid);
    btn.setAttribute('data-piece-index', String(idx));
    btn.setAttribute('data-active', isCurrent ? 'true' : 'false');
    btn.setAttribute(
      'aria-label',
      `Chain piece ${idx + 1} of ${chain.length}${isCurrent ? ' (current)' : ''}`
    );
    btn.textContent = isCurrent ? '★' : '✦';

    // Route to target chain piece while binding source pane ID for split-view replacement
    bindActionBtnHandlers(btn, () => {
      hideChainStarTooltip();
      if (!isCurrent) {
        manualVisitedCids.add(cid);
        openConversation(cid, undefined, sourceCid || chain[idx]);
      }
    });
    btn.addEventListener('mouseenter', () => {
      void showChainStarTooltip(btn, cid, idx, chain.length, isCurrent, isTail);
    });
    btn.addEventListener('mouseleave', () => {
      hideChainStarTooltip();
    });
    return btn;
  }

  function syncConversationPieceLock(convoViewEl, cascadeId, chain) {
    const activeIdx = chain.indexOf(cascadeId);
    if (activeIdx === -1) {
      // Purge any dangling read-only banners when conversation is not indexed in chain
      for (const b of convoViewEl.querySelectorAll(`.${ARCHIVE_BANNER_CLASS}`)) {
        b.remove();
      }
      return;
    }
    const isTail = activeIdx === chain.length - 1;

    const inputBox = convoViewEl.querySelector('[data-testid="agent-input-box"]');
    // Archived chats mount nativeArchivedBanner instead of inputBox, so root footer container there
    const nativeArchivedBanner = convoViewEl.querySelector?.(
      '.min-h-\\[80px\\].rounded-2xl.bg-muted, [class*="min-h-"][class*="rounded-2xl"]'
    );
    const footerInner =
      inputBox?.parentElement ||
      convoViewEl.querySelector?.('[data-testid="input-box-bottom-toolbar"]')?.parentElement ||
      nativeArchivedBanner?.parentElement ||
      convoViewEl;

    if (!isTail) {
      convoViewEl.setAttribute('data-jetski-non-tail-locked', 'true');
      if (inputBox) {
        inputBox.style.display = 'none';
        const ceList = inputBox.querySelectorAll('[contenteditable]');
        for (const ce of ceList) {
          ce.setAttribute('contenteditable', 'false');
          if (typeof ce.blur === 'function') ce.blur();
        }
        const sendBtns = inputBox.querySelectorAll('button[data-testid="send-button"]');
        for (const btn of sendBtns) {
          btn.setAttribute('disabled', 'true');
        }
      }

      if (nativeArchivedBanner) {
        nativeArchivedBanner.style.display = 'none';
      }

      let banner = footerInner.querySelector(`.${ARCHIVE_BANNER_CLASS}`);
      if (!banner) {
        banner = document.createElement('div');
        banner.className = ARCHIVE_BANNER_CLASS;
        banner.setAttribute('data-testid', ARCHIVE_BANNER_CLASS);
        footerInner.appendChild(banner);
      }

      // Preserve DOM identity of jumpBtn by storing read-only label in a dedicated child span
      let labelSpan = banner.querySelector(`.${ARCHIVE_LABEL_CLASS}`);
      if (!labelSpan) {
        labelSpan = document.createElement('span');
        labelSpan.className = ARCHIVE_LABEL_CLASS;
        banner.insertBefore(labelSpan, banner.firstChild);
      }
      const nextLabel = `🔒 Archived piece #${activeIdx + 1} of ${chain.length} (read-only) `;
      if (labelSpan.textContent !== nextLabel) {
        labelSpan.textContent = nextLabel;
      }

      // Bind click handler once at creation time to prevent duplicate listener accumulation
      let jumpBtn = banner.querySelector(`.${JUMP_TO_TAIL_BTN_CLASS}`);
      if (!jumpBtn) {
        jumpBtn = document.createElement('button');
        jumpBtn.type = 'button';
        jumpBtn.className = JUMP_TO_TAIL_BTN_CLASS;
        jumpBtn.setAttribute('data-testid', JUMP_TO_TAIL_BTN_CLASS);
        bindActionBtnHandlers(jumpBtn, () => {
          const tid = jumpBtn.getAttribute('data-tail-id');
          const sid = jumpBtn.getAttribute('data-source-id');
          // Optimistically discard banner to avoid visual flash while route transition resolves
          banner.remove();
          if (tid) {
            openConversation(tid, undefined, sid);
          }
        });
        banner.appendChild(jumpBtn);
      }
      const tailId = chain[chain.length - 1];
      const nextBtnText = `Jump to active #${chain.length} →`;
      // Guard text and data attribute writes to avoid redundant childList/attribute mutations
      if (jumpBtn.textContent !== nextBtnText) {
        jumpBtn.textContent = nextBtnText;
      }
      if (jumpBtn.getAttribute('data-tail-id') !== tailId) {
        jumpBtn.setAttribute('data-tail-id', tailId);
      }
      if (jumpBtn.getAttribute('data-source-id') !== cascadeId) {
        jumpBtn.setAttribute('data-source-id', cascadeId);
      }
    } else {
      convoViewEl.removeAttribute('data-jetski-non-tail-locked');
      if (inputBox) {
        inputBox.style.display = '';
        const ceList = inputBox.querySelectorAll('[contenteditable]');
        for (const ce of ceList) {
          ce.setAttribute('contenteditable', 'true');
        }
        const sendBtns = inputBox.querySelectorAll('button[data-testid="send-button"]');
        for (const btn of sendBtns) {
          btn.removeAttribute('disabled');
        }
      }
      // Restore native banner visibility when switching this view back to the active tail
      if (nativeArchivedBanner) {
        nativeArchivedBanner.style.display = '';
      }
      // Clear all banners to guarantee complete cleanup across nested or cloned DOM nodes
      for (const b of convoViewEl.querySelectorAll(`.${ARCHIVE_BANNER_CLASS}`)) {
        b.remove();
      }
    }
  }

  let chainNavSyncSeq = 0;

  // Moves a view off a chat that was just archived by handoff/next-task onto its successor.
  // Only fresh redirects count: an old archived piece opened later must stay where it is
  function maybeAutoSwitchToSuccessor(cid, redirects) {
    if (manualVisitedCids.has(cid) || !isArchivedCascadeId(cid)) return;
    const r = redirects?.[cid];
    const target = r?.to;
    if (!target || target === cid) return;
    const at = Date.parse(r.at || '') || 0;
    if (Date.now() - at >= AUTO_SWITCH_MAX_AGE_MS) return;
    const edge = `${cid}->${target}`;
    if (autoSwitchedEdges.has(edge)) return;
    autoSwitchedEdges.add(edge);
    openConversation(target, undefined, cid, { preserveFocus: true });
  }

  async function syncConversationChainNavigators() {
    const curSeq = ++chainNavSyncSeq;
    const convoViews = document.querySelectorAll(
      '[data-testid="conversation-view"][data-cascade-id]'
    );
    for (const convoViewEl of convoViews) {
      const cid = convoViewEl.getAttribute('data-cascade-id');
      if (!cid) continue;

      // A view whose chat just turned archived drops the 5s cache, so the redirect written
      // right before the archive is read now rather than after the window ends
      if (isArchivedCascadeId(cid) && !observedArchivedViewIds.has(cid)) {
        observedArchivedViewIds.add(cid);
        chainIndexFetchedAt = 0;
      }

      let chain;
      try {
        chain = await chainOf(cid);
      } catch {
        chain = [cid];
      }

      // Drop stale async resolution if a newer pass started or view navigated to another chat
      if (curSeq !== chainNavSyncSeq) return;
      if (convoViewEl.getAttribute('data-cascade-id') !== cid) continue;

      maybeAutoSwitchToSuccessor(cid, chainIndexCache?.redirects);

      syncConversationPieceLock(convoViewEl, cid, chain);

      if (chain.length <= 1) {
        convoViewEl.querySelector(`[data-testid="${CHAIN_STARS_BEFORE_TEST_ID}"]`)?.remove();
        convoViewEl.querySelector(`[data-testid="${CHAIN_STARS_AFTER_TEST_ID}"]`)?.remove();
        convoViewEl.querySelector('.jetski-chain-stars-fallback')?.remove();
        convoViewEl.removeAttribute('data-jetski-chain-sig');
        continue;
      }

      const activeIdx = chain.indexOf(cid);
      if (activeIdx === -1) continue;

      // Key rendered star buttons by chain topology and active piece to preserve DOM identity
      const chainSig = `${chain.join(',')}@${cid}`;
      const nav = convoViewEl.querySelector('[data-testid="conversation-navigator"]');
      if (nav) {
        convoViewEl.querySelector('.jetski-chain-stars-fallback')?.remove();
        let beforeGroup = nav.querySelector(`[data-testid="${CHAIN_STARS_BEFORE_TEST_ID}"]`);
        let afterGroup = nav.querySelector(`[data-testid="${CHAIN_STARS_AFTER_TEST_ID}"]`);
        const needsAfter = activeIdx < chain.length - 1;
        const currentSig = nav.getAttribute('data-jetski-chain-sig');

        // Skip re-rendering star buttons when chain structure has not changed
        if (
          currentSig === chainSig &&
          beforeGroup &&
          (needsAfter ? afterGroup : !afterGroup)
        ) {
          continue;
        }

        nav.setAttribute('data-jetski-chain-sig', chainSig);
        const inner = nav.querySelector('.relative.flex.min-h-0.flex-col') || nav.children[0];

        if (!beforeGroup) {
          beforeGroup = document.createElement('div');
          beforeGroup.className = `${CHAIN_STARS_GROUP_CLASS} jetski-chain-stars-before`;
          beforeGroup.setAttribute('data-testid', CHAIN_STARS_BEFORE_TEST_ID);
          if (inner) {
            nav.insertBefore(beforeGroup, inner);
          } else {
            nav.appendChild(beforeGroup);
          }
        }
        while (beforeGroup.children?.length > 0) {
          beforeGroup.children[0].remove();
        }
        // Render leading and active chain pieces into the before-group container
        for (let i = 0; i <= activeIdx; i++) {
          const pieceCid = chain[i];
          const isCur = i === activeIdx;
          const isTl = i === chain.length - 1;
          beforeGroup.appendChild(createStarButton(pieceCid, i, chain, isCur, isTl, cid));
        }

        if (needsAfter) {
          if (!afterGroup) {
            afterGroup = document.createElement('div');
            afterGroup.className = `${CHAIN_STARS_GROUP_CLASS} jetski-chain-stars-after`;
            afterGroup.setAttribute('data-testid', CHAIN_STARS_AFTER_TEST_ID);
            nav.appendChild(afterGroup);
          }
          while (afterGroup.children?.length > 0) {
            afterGroup.children[0].remove();
          }
          // Render descendant chain pieces into the after-group container
          for (let i = activeIdx + 1; i < chain.length; i++) {
            const pieceCid = chain[i];
            const isCur = false;
            const isTl = i === chain.length - 1;
            afterGroup.appendChild(createStarButton(pieceCid, i, chain, isCur, isTl, cid));
          }
        } else if (afterGroup) {
          afterGroup.remove();
        }
      } else {
        convoViewEl.querySelector(`[data-testid="${CHAIN_STARS_BEFORE_TEST_ID}"]`)?.remove();
        convoViewEl.querySelector(`[data-testid="${CHAIN_STARS_AFTER_TEST_ID}"]`)?.remove();
        const slot =
          convoViewEl.querySelector('.pointer-events-none.absolute.inset-y-0.left-0.z-10.w-7') ||
          convoViewEl;
        let fallback = slot.querySelector('.jetski-chain-stars-fallback');
        // Preserve fallback DOM buttons when chain signature is unchanged
        if (fallback && fallback.getAttribute('data-jetski-chain-sig') === chainSig) {
          continue;
        }
        if (!fallback) {
          fallback = document.createElement('div');
          fallback.className = `${CHAIN_STARS_GROUP_CLASS} jetski-chain-stars-fallback`;
          // Center fallback stars vertically inside the 28px rail slot when nav is unmounted
          fallback.style.position = 'absolute';
          fallback.style.left = '0';
          fallback.style.width = '28px';
          fallback.style.top = '50%';
          fallback.style.transform = 'translateY(-50%)';
          slot.appendChild(fallback);
        }
        fallback.setAttribute('data-jetski-chain-sig', chainSig);
        while (fallback.children?.length > 0) {
          fallback.children[0].remove();
        }
        for (let i = 0; i < chain.length; i++) {
          const pieceCid = chain[i];
          const isCur = i === activeIdx;
          const isTl = i === chain.length - 1;
          fallback.appendChild(createStarButton(pieceCid, i, chain, isCur, isTl, cid));
        }
      }
    }
  }

  if (typeof document.addEventListener === 'function') {
    document.addEventListener(
      'keydown',
      (e) => {
        const view = e.target?.closest?.('[data-testid="conversation-view"]');
        if (view?.getAttribute('data-jetski-non-tail-locked') === 'true') {
          if (e.key === 'Enter') {
            e.preventDefault();
            e.stopPropagation();
          }
        }
      },
      true
    );
  }

  window.__jetskiChainNav = { syncConversationChainNavigators };

  // Injects the 3-button filter bar inline inside actionRow before Display Options
  function ensureFilterBar() {
    const displayOptionsBtns = document.querySelectorAll(
      'button[aria-label="Display Options"]'
    );
    for (const displayOptionsBtn of displayOptionsBtns) {
      const actionRow = displayOptionsBtn.parentElement;
      if (!actionRow) continue;

      let bar = actionRow.querySelector(`.${BAR_CLASS}`);
      if (!bar) {
        bar = document.createElement('div');
        bar.className = BAR_CLASS;
        bar.setAttribute('data-testid', 'jetski-attention-filter-bar');

        for (const bucketKey of BUCKET_ORDER) {
          bar.appendChild(createBucketButton(bucketKey));
        }

        actionRow.insertBefore(bar, displayOptionsBtn);
      }

      for (const bucketKey of BUCKET_ORDER) {
        const btn = bar.querySelector(`button[data-bucket="${bucketKey}"]`);
        updateBucketButtonUI(btn, bucketKey);
      }
    }
  }

  // Strips live activity suffixes (" · ⟳ 1 ⚠ 2") appended by sidebar project cards
  const PROJECT_ACTIVITY_SUFFIX_RE =
    /(?:\s*·\s*(?:[⟳⚠●]\s*\d+)(?:\s+(?:[⟳⚠●]\s*\d+))*)+$/u;

  // Resolves a human-readable project title from Redux slices or sidebar project headers
  function resolveProjectName(projectId, store) {
    if (!projectId || projectId === '__standalone__') return 'Standalone';
    const state = store?.getState();
    // The Workspaces sidebar keeps names in the sidebarProjects slice as an array of {id, name}
    const sidebarProjects = state?.sidebarProjects?.projects;
    const fromSidebar = Array.isArray(sidebarProjects)
      ? sidebarProjects.find((p) => p?.id === projectId)?.name
      : '';
    const fromStore =
      fromSidebar ||
      state?.projects?.projects?.[projectId]?.name ||
      state?.project?.projects?.[projectId]?.name ||
      state?.projects?.byId?.[projectId]?.name ||
      '';
    if (typeof fromStore === 'string' && fromStore.trim()) {
      return fromStore.replace(PROJECT_ACTIVITY_SUFFIX_RE, '').trim();
    }
    const sectionEls = document.querySelectorAll(
      'div[data-sidebar-section-drop-id]:not([data-sidebar-row-id])'
    );
    for (const sec of sectionEls) {
      if (sec.getAttribute('data-sidebar-section-drop-id') !== projectId) continue;
      const headerBtn = sec.querySelector(
        'button[data-testid="project-header"], button[data-project-card]'
      );
      const span = headerBtn?.querySelector('span.truncate');
      const domName = span?.textContent?.trim() || '';
      if (domName) {
        return domName.replace(PROJECT_ACTIVITY_SUFFIX_RE, '').trim();
      }
    }
    return projectId;
  }

  // Dismisses a conversation from Needs Me inbox and clears its latch
  function markConversationSeen(cascadeId) {
    if (!cascadeId) return;
    latchedNeedsMeModMs.delete(cascadeId);
    const summariesMap =
      findReduxStore()?.getState()?.trajectorySummaries?.summaries || {};
    const s = summariesMap[cascadeId];
    if (s) {
      const statusInfo = resolveConversationStatus(cascadeId, s, '', summariesMap);
      if (statusInfo.isBlocked && statusInfo.blockedSig) {
        dismissedBlockedSigs.set(cascadeId, statusInfo.blockedSig);
      }
    }
    const modMs =
      parseTimestampMs(s?.lastModifiedTime) ||
      parseTimestampMs(s?.createdTime) ||
      Date.now();
    const srvViewMs = parseTimestampMs(s?.annotations?.lastUserViewTime);
    locallySeenModifiedMs.set(
      cascadeId,
      Math.max(modMs, srvViewMs + 1, Date.now(), 1)
    );
    ensureNeedsReactionInbox();
  }

  // Dismisses active conversation when the user clicks or focuses the prompt input box
  function handleInputBoxSeen(event) {
    const target = event?.target;
    if (!target || typeof target.closest !== 'function') return;
    const inputBox = target.closest('[data-testid="agent-input-box"]');
    if (!inputBox) return;
    const cid = findCascadeIdFromElement(inputBox);
    if (!cid) return;
    if (latchedNeedsMeModMs.has(cid)) {
      markConversationSeen(cid);
    } else {
      const summary = findReduxStore()?.getState()?.trajectorySummaries?.summaries?.[cid];
      if (isUnseenNeedsReaction(cid, summary)) {
        markConversationSeen(cid);
      }
    }
  }

  function bindInputBoxSeenListeners() {
    if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
      document.addEventListener('pointerdown', handleInputBoxSeen, true);
      document.addEventListener('focusin', handleInputBoxSeen, true);
    }
  }

  function unbindInputBoxSeenListeners() {
    if (typeof document !== 'undefined' && typeof document.removeEventListener === 'function') {
      document.removeEventListener('pointerdown', handleInputBoxSeen, true);
      document.removeEventListener('focusin', handleInputBoxSeen, true);
    }
  }

  // Converts a protobuf Timestamp ({seconds, nanos}), Date, numeric ms, or ISO string to epoch ms
  function parseTimestampMs(ts) {
    if (!ts) return 0;
    if (typeof ts === 'number') return Number.isFinite(ts) ? ts : 0;
    if (typeof ts === 'string') return Date.parse(ts) || 0;
    if (ts instanceof Date) return ts.getTime() || 0;
    if (typeof ts === 'object' && ts.seconds !== undefined && ts.seconds !== null) {
      const sec = Number(ts.seconds);
      const nanos = Number(ts.nanos || 0);
      if (Number.isFinite(sec)) {
        return sec * 1000 + (Number.isFinite(nanos) ? Math.floor(nanos / 1e6) : 0);
      }
    }
    return 0;
  }

  // Returns true when a needs-me chat has activity or a blocked step not yet dismissed by the user
  function isUnseenNeedsReaction(cascadeId, summary, statusInfo) {
    if (!summary) return false;
    const resolvedStatus =
      statusInfo || resolveConversationStatus(cascadeId, summary, '');
    const lastMod =
      parseTimestampMs(summary.lastModifiedTime) ||
      parseTimestampMs(summary.createdTime);

    // Live blocked steps (self or subagent) stay visible until resolved or explicitly dismissed
    if (resolvedStatus.isBlocked) {
      if (
        resolvedStatus.blockedSig &&
        dismissedBlockedSigs.get(cascadeId) === resolvedStatus.blockedSig
      ) {
        return false;
      }
      return true;
    }
    dismissedBlockedSigs.delete(cascadeId);

    // Once latched in Needs Me, mounting in pane (which updates serverLastView) does not dismiss
    if (latchedNeedsMeModMs.has(cascadeId)) {
      if (lastMod > (latchedNeedsMeModMs.get(cascadeId) || 0)) {
        latchedNeedsMeModMs.set(cascadeId, lastMod);
      }
      return true;
    }

    const localLastView = locallySeenModifiedMs.get(cascadeId) || 0;
    const serverLastView = parseTimestampMs(summary.annotations?.lastUserViewTime);
    const effectiveLastView = Math.max(serverLastView, localLastView);
    if (summary.annotations?.markedAsUnread && localLastView <= serverLastView) {
      latchedNeedsMeModMs.set(cascadeId, lastMod || Date.now());
      return true;
    }
    if (lastMod > 0) {
      if (effectiveLastView === 0 || lastMod > effectiveLastView) {
        latchedNeedsMeModMs.set(cascadeId, lastMod);
        return true;
      }
      return false;
    }
    return false;
  }

  // Classifies an unseen inbox chat into 'urgent' (blocked on self/subagent step) vs 'unseen'
  function classifyInboxTier(summary, statusInfo) {
    const ws = summary?.waitingSteps || summary?.waiting_steps;
    // Chats blocked on tool approval or user questions rank above unread finished turns
    if (
      statusInfo?.isBlocked ||
      (Array.isArray(ws) && ws.length > 0)
    ) {
      return 'urgent';
    }
    return 'unseen';
  }

  // Builds a single Needs-Reaction inbox row with click navigation and quick-action buttons
  function createInboxRowElement(item) {
    const row = document.createElement('div');
    row.className = INBOX_ROW_CLASS;
    row.setAttribute('data-testid', 'jetski-inbox-row');
    row.setAttribute('data-cascade-id', item.cascadeId);
    row.setAttribute('data-tab', item.tabKey);
    if (item.tier) {
      row.setAttribute('data-tier', item.tier);
    }
    if (item.detailText) {
      row.setAttribute('data-detail', item.detailText);
      row.setAttribute('title', `${item.baseTitle} — ${item.detailText}`);
    }

    const main = document.createElement('div');
    main.className = 'jetski-inbox-row-main';

    const icon = document.createElement('span');
    icon.className = 'jetski-inbox-tier-icon';
    if (item.tabKey === 'working') {
      icon.textContent = item.workingIcon || '🤖';
    } else if (item.tabKey === 'stale') {
      icon.textContent = '🧹';
    } else {
      icon.textContent = item.tier === 'urgent' ? '🔥' : '⦿';
    }

    const proj = document.createElement('span');
    proj.className = 'jetski-inbox-project';
    proj.textContent = item.projectName;

    const textCol = document.createElement('div');
    textCol.className = 'jetski-inbox-text-col';

    const title = document.createElement('span');
    title.className = 'jetski-inbox-title';
    title.textContent = item.baseTitle;
    textCol.appendChild(title);

    if (item.detailText) {
      const detail = document.createElement('span');
      detail.className = 'jetski-inbox-detail';
      detail.setAttribute('data-testid', 'jetski-inbox-detail');
      detail.textContent = item.detailText;
      textCol.appendChild(detail);
    }

    main.appendChild(icon);
    main.appendChild(proj);
    main.appendChild(textCol);

    if (item.timePrefix) {
      const time = document.createElement('span');
      time.className = 'jetski-inbox-time';
      time.textContent = item.timePrefix;
      main.appendChild(time);
    }

    const actions = document.createElement('div');
    actions.className = 'jetski-inbox-actions';

    if (item.tabKey === 'needs-me') {
      const seenBtn = document.createElement('button');
      seenBtn.type = 'button';
      seenBtn.className = INBOX_SEEN_BTN_CLASS;
      seenBtn.setAttribute('data-testid', 'jetski-inbox-seen-btn');
      seenBtn.setAttribute('aria-label', 'Mark as seen');
      seenBtn.textContent = '✓';
      bindActionBtnHandlers(seenBtn, () => {
        const cid = row.getAttribute('data-cascade-id');
        if (cid) markConversationSeen(cid);
      });
      actions.appendChild(seenBtn);

      const archiveBtn = document.createElement('button');
      archiveBtn.type = 'button';
      archiveBtn.className = INBOX_ARCHIVE_BTN_CLASS;
      archiveBtn.setAttribute('data-testid', 'jetski-inbox-archive-btn');
      archiveBtn.setAttribute('aria-label', 'Archive chat');
      archiveBtn.textContent = '🗄';
      bindActionBtnHandlers(archiveBtn, () => {
        const cid = row.getAttribute('data-cascade-id');
        if (cid) void toggleConversationArchived(cid);
      });
      actions.appendChild(archiveBtn);
    } else if (item.tabKey === 'stale') {
      const archiveBtn = document.createElement('button');
      archiveBtn.type = 'button';
      archiveBtn.className = INBOX_ARCHIVE_BTN_CLASS;
      archiveBtn.setAttribute('data-testid', 'jetski-inbox-archive-btn');
      archiveBtn.setAttribute('aria-label', 'Archive chat');
      archiveBtn.textContent = '🗄';
      bindActionBtnHandlers(archiveBtn, () => {
        const cid = row.getAttribute('data-cascade-id');
        if (cid) void toggleConversationArchived(cid);
      });
      actions.appendChild(archiveBtn);
    }

    row.appendChild(main);
    row.appendChild(actions);

    // Clicking the row opens the chat without dismissing it from the inbox
    bindActionBtnHandlers(row, () => {
      const cid = row.getAttribute('data-cascade-id');
      if (cid) {
        manualVisitedCids.add(cid);
        openConversation(cid);
      }
    });

    return row;
  }

  // Creates the Needs-Reaction inbox container and header once per sidebar mount
  function createInboxShell() {
    const el = document.createElement('div');
    el.className = INBOX_CLASS;
    el.setAttribute('data-testid', INBOX_CLASS);

    const headerEl = document.createElement('div');
    headerEl.className = INBOX_HEADER_CLASS;
    headerEl.setAttribute('data-testid', INBOX_HEADER_CLASS);

    const headerLeft = document.createElement('div');
    headerLeft.className = 'jetski-inbox-header-left';

    for (const def of INBOX_TAB_DEFS) {
      const tabBtn = document.createElement('button');
      tabBtn.type = 'button';
      tabBtn.className = `${INBOX_TAB_BTN_CLASS} jetski-inbox-tab-${def.key}`;
      tabBtn.setAttribute('data-testid', `jetski-inbox-tab-${def.key}`);
      tabBtn.setAttribute('data-tab', def.key);
      tabBtn.setAttribute('data-active', activeInboxTab === def.key ? 'true' : 'false');

      const labelSpan = document.createElement('span');
      labelSpan.className = 'jetski-inbox-tab-label';
      labelSpan.textContent = def.label;
      tabBtn.appendChild(labelSpan);

      const countBadge = document.createElement('span');
      countBadge.className = 'jetski-inbox-count-badge';
      countBadge.setAttribute('data-testid', `jetski-inbox-count-badge-${def.key}`);
      countBadge.textContent = '0';
      tabBtn.appendChild(countBadge);

      if (def.key === 'needs-me') {
        const urgentPill = document.createElement('span');
        urgentPill.className = 'jetski-inbox-urgent-pill';
        urgentPill.setAttribute('data-testid', 'jetski-inbox-urgent-pill');
        urgentPill.style.display = 'none';
        tabBtn.appendChild(urgentPill);
      }

      bindActionBtnHandlers(tabBtn, () => {
        activeInboxTab = def.key;
        manualEmptyTabKey = def.key;
        safeSetStorage(STORAGE_INBOX_TAB_KEY, activeInboxTab);
        if (inboxCollapsed) {
          inboxCollapsed = false;
          safeSetStorage(STORAGE_INBOX_COLLAPSED_KEY, 'false');
        }
        ensureNeedsReactionInbox();
      });

      headerLeft.appendChild(tabBtn);
    }

    const toggleBtn = document.createElement('button');
    toggleBtn.type = 'button';
    toggleBtn.className = INBOX_TOGGLE_BTN_CLASS;
    toggleBtn.setAttribute('data-testid', INBOX_TOGGLE_BTN_CLASS);

    headerEl.appendChild(headerLeft);
    headerEl.appendChild(toggleBtn);

    bindActionBtnHandlers(headerEl, () => {
      inboxCollapsed = !inboxCollapsed;
      safeSetStorage(STORAGE_INBOX_COLLAPSED_KEY, inboxCollapsed ? 'true' : 'false');
      ensureNeedsReactionInbox();
    });

    const listContainer = document.createElement('div');
    listContainer.className = INBOX_LIST_CLASS;
    listContainer.setAttribute('data-testid', INBOX_LIST_CLASS);

    el.appendChild(headerEl);
    el.appendChild(listContainer);
    return el;
  }

  // Mounts and updates the pinned collapsible Needs-Reaction inbox above the sidebar project list
  function ensureNeedsReactionInbox() {
    const listEl =
      document.querySelector('[data-testid="conversation-list-sidebar"]') ||
      document.querySelector('[data-sidebar-scroll-container="true"]');
    const displayOptionsBtn = document.querySelector('button[aria-label="Display Options"]');
    const actionRow = displayOptionsBtn?.parentElement;

    const mountParent = listEl?.parentElement || actionRow?.parentElement;
    if (!mountParent) return;

    let inboxEl = document.querySelector(`.${INBOX_CLASS}`);
    if (!inboxEl) {
      inboxEl = createInboxShell();
    }

    // Place inbox immediately before listEl when mounted, or right after actionRow
    if (listEl && listEl.parentElement) {
      const parentChildren = listEl.parentElement.children || [];
      const idxInbox = Array.prototype.indexOf.call(parentChildren, inboxEl);
      const idxList = Array.prototype.indexOf.call(parentChildren, listEl);
      if (inboxEl.parentElement !== listEl.parentElement || idxInbox !== idxList - 1) {
        listEl.parentElement.insertBefore(inboxEl, listEl);
      }
    } else if (
      actionRow &&
      actionRow.parentElement &&
      inboxEl.parentElement !== actionRow.parentElement
    ) {
      const children = actionRow.parentElement.children || [];
      const actionIdx = Array.prototype.indexOf.call(children, actionRow);
      const nextSibling =
        actionIdx >= 0 && actionIdx + 1 < children.length ? children[actionIdx + 1] : null;
      actionRow.parentElement.insertBefore(inboxEl, nextSibling);
    }

    const store = findReduxStore();
    const summariesMap = store?.getState()?.trajectorySummaries?.summaries || {};
    const childIndex = buildDescendantIndex(summariesMap);
    const now = Date.now();

    const itemsNeedsMe = [];
    const itemsWorking = [];
    const itemsStale = [];
    let urgentCount = 0;

    for (const [cascadeId, summary] of Object.entries(summariesMap)) {
      if (!isEligibleTopLevelSummary(cascadeId, summary)) continue;
      const rawTitle = summary?.annotations?.title || summary?.summary || '';
      const baseTitle = cleanBaseTitle(rawTitle) || rawTitle || cascadeId.slice(0, 8);
      const timeMatch = TIME_PREFIX_EXTRACT_RE.exec(rawTitle);
      const timePrefix = timeMatch ? timeMatch[1] : '';
      const projectId = summary?.trajectoryMetadata?.projectId || '__standalone__';
      const projectName = resolveProjectName(projectId, store);
      const modTs =
        parseTimestampMs(summary?.lastModifiedTime) ||
        parseTimestampMs(summary?.createdTime) ||
        now;
      const ageMs = Math.max(0, now - modTs);
      const statusInfo = resolveConversationStatus(
        cascadeId,
        summary,
        rawTitle,
        summariesMap,
        childIndex
      );
      const bucket = statusInfo.bucket;

      if (bucket === 'working') {
        const isRunning = isActiveRunStatus(summary?.status);
        const workingIcon = isRunning ? '🤖' : '⏳';
        const subagentDetail =
          statusInfo.workingDetail && statusInfo.workingDetail !== 'Agent turn running'
            ? statusInfo.workingDetail
            : '';
        itemsWorking.push({
          cascadeId,
          tabKey: 'working',
          baseTitle,
          timePrefix,
          projectId,
          projectName,
          workingIcon,
          detailText: subagentDetail,
          modTs,
        });
      } else if (bucket === 'needs-me') {
        // Never demote an actively blocked chat to Stale even if its last turn predates 7 days
        if (!statusInfo.isBlocked && ageMs >= STALE_THRESHOLD_MS) {
          const days = Math.floor(ageMs / (24 * 60 * 60 * 1000));
          itemsStale.push({
            cascadeId,
            tabKey: 'stale',
            baseTitle,
            timePrefix: `${days}d`,
            projectId,
            projectName,
            modTs,
          });
        } else if (isUnseenNeedsReaction(cascadeId, summary, statusInfo)) {
          const tier = classifyInboxTier(summary, statusInfo);
          if (tier === 'urgent') urgentCount++;
          itemsNeedsMe.push({
            cascadeId,
            tabKey: 'needs-me',
            baseTitle,
            timePrefix,
            projectId,
            projectName,
            tier,
            detailText: statusInfo.isBlocked ? statusInfo.blockedDetail : '',
            modTs,
          });
        }
      }
    }

    // Sort Tab 1 (Needs Me): urgent first, then newest modified first
    itemsNeedsMe.sort((a, b) => {
      if (a.tier !== b.tier) return a.tier === 'urgent' ? -1 : 1;
      if (b.modTs !== a.modTs) return b.modTs - a.modTs;
      if (b.timePrefix !== a.timePrefix) return b.timePrefix.localeCompare(a.timePrefix);
      return a.cascadeId.localeCompare(b.cascadeId);
    });

    // Sort Tab 2 (Working): newest modified first
    itemsWorking.sort((a, b) => {
      if (b.modTs !== a.modTs) return b.modTs - a.modTs;
      if (b.timePrefix !== a.timePrefix) return b.timePrefix.localeCompare(a.timePrefix);
      return a.cascadeId.localeCompare(b.cascadeId);
    });

    // Sort Tab 3 (Stale): oldest modified first
    itemsStale.sort((a, b) => {
      if (a.modTs !== b.modTs) return a.modTs - b.modTs;
      return a.cascadeId.localeCompare(b.cascadeId);
    });

    const itemsByTab = {
      'needs-me': itemsNeedsMe,
      'working': itemsWorking,
      'stale': itemsStale,
    };

    const totalCount = itemsNeedsMe.length + itemsWorking.length + itemsStale.length;
    const isEmpty = totalCount === 0;
    const emptyStr = isEmpty ? 'true' : 'false';
    if (inboxEl.getAttribute('data-empty') !== emptyStr) {
      inboxEl.setAttribute('data-empty', emptyStr);
    }
    // Stays visible when empty so the user sees an explicit all-clear instead of a vanished widget
    inboxEl.style.display = '';

    // Keep Needs Me sticky even at 0 items; return from Working/Stale to Needs Me once emptied
    if ((itemsByTab[activeInboxTab]?.length || 0) > 0) {
      manualEmptyTabKey = null;
    } else if (activeInboxTab !== 'needs-me' && manualEmptyTabKey !== activeInboxTab) {
      activeInboxTab = 'needs-me';
      safeSetStorage(STORAGE_INBOX_TAB_KEY, activeInboxTab);
    }

    const currentTabItems = itemsByTab[activeInboxTab] || [];

    if (inboxEl.getAttribute('data-active-tab') !== activeInboxTab) {
      inboxEl.setAttribute('data-active-tab', activeInboxTab);
    }
    const urgentStr = urgentCount > 0 ? 'true' : 'false';
    if (inboxEl.getAttribute('data-urgent') !== urgentStr) {
      inboxEl.setAttribute('data-urgent', urgentStr);
    }
    if (inboxEl.getAttribute('data-count') !== String(currentTabItems.length)) {
      inboxEl.setAttribute('data-count', String(currentTabItems.length));
    }
    if (inboxEl.getAttribute('data-urgent-count') !== String(urgentCount)) {
      inboxEl.setAttribute('data-urgent-count', String(urgentCount));
    }

    const collapsedStr = inboxCollapsed ? 'true' : 'false';
    if (inboxEl.getAttribute('data-collapsed') !== collapsedStr) {
      inboxEl.setAttribute('data-collapsed', collapsedStr);
    }

    // Sync tab buttons (active state, count badges, urgent pill)
    for (const def of INBOX_TAB_DEFS) {
      const tabBtn = inboxEl.querySelector(`.jetski-inbox-tab-${def.key}`);
      if (tabBtn) {
        const isActive = activeInboxTab === def.key;
        if (tabBtn.getAttribute('data-active') !== (isActive ? 'true' : 'false')) {
          tabBtn.setAttribute('data-active', isActive ? 'true' : 'false');
        }
        const countBadge = tabBtn.querySelector('.jetski-inbox-count-badge');
        const count = itemsByTab[def.key]?.length || 0;
        if (countBadge && countBadge.textContent !== String(count)) {
          countBadge.textContent = String(count);
        }
        if (def.key === 'needs-me') {
          const urgentPill = tabBtn.querySelector('.jetski-inbox-urgent-pill');
          if (urgentPill) {
            urgentPill.style.display = urgentCount > 0 ? '' : 'none';
            const nextUrgentText = urgentCount > 0 ? `· ${urgentCount}!` : '';
            if (urgentPill.textContent !== nextUrgentText) {
              urgentPill.textContent = nextUrgentText;
            }
          }
        }
      }
    }

    const toggleBtn = inboxEl.querySelector(`.${INBOX_TOGGLE_BTN_CLASS}`);
    if (toggleBtn) {
      const nextGlyph = inboxCollapsed ? '▸' : '▾';
      if (toggleBtn.textContent !== nextGlyph) {
        toggleBtn.textContent = nextGlyph;
      }
      const nextLabel = inboxCollapsed
        ? 'Expand Needs Reaction inbox'
        : 'Collapse Needs Reaction inbox';
      if (toggleBtn.getAttribute('aria-label') !== nextLabel) {
        toggleBtn.setAttribute('aria-label', nextLabel);
      }
    }

    // Re-render row nodes only when the ordered inbox signature changes
    const sig = `${activeInboxTab}:` + currentTabItems
      .map(
        (it) =>
          `${it.cascadeId}:${it.tier || ''}:${it.workingIcon || ''}:` +
          `${it.projectName}:${it.baseTitle}:${it.timePrefix}:${it.detailText || ''}`
      )
      .join('|');
    if (inboxEl.getAttribute('data-jetski-inbox-sig') !== sig) {
      inboxEl.setAttribute('data-jetski-inbox-sig', sig);
      const listContainer = inboxEl.querySelector(`.${INBOX_LIST_CLASS}`);
      if (listContainer) {
        while (listContainer.children?.length > 0) {
          listContainer.children[0].remove();
        }
        for (const item of currentTabItems) {
          listContainer.appendChild(createInboxRowElement(item));
        }
        // Render the per-tab all-clear message whenever the active tab has 0 items
        if (currentTabItems.length === 0) {
          const emptyRow = document.createElement('div');
          emptyRow.className = 'jetski-inbox-empty-row';
          emptyRow.textContent = INBOX_EMPTY_TEXT[activeInboxTab] || INBOX_EMPTY_TEXT['needs-me'];
          listContainer.appendChild(emptyRow);
        }
      }
    }
  }

  // Resolves the conversation ID owning a DOM element via data attributes, React Fiber, or URL
  function findCascadeIdFromElement(el) {
    let cur = el;
    let depth = 0;
    // Walk up the DOM and inspect React Fiber props so split-view panes get their own cascadeId
    while (cur && depth < 12) {
      depth++;
      if (typeof cur.getAttribute === 'function') {
        const attrId =
          cur.getAttribute('data-cascade-id') || cur.getAttribute('data-sidebar-row-id');
        if (attrId) return attrId;
      }
      const fiberKey = Object.keys(cur).find(
        (k) => k.startsWith('__reactFiber$') || k.startsWith('__reactContainer$')
      );
      if (fiberKey) {
        let fiber = cur[fiberKey];
        let fSteps = 0;
        while (fiber && fSteps < 40) {
          fSteps++;
          const cid =
            fiber.memoizedProps?.cascadeId ||
            fiber.memoizedProps?.conversationId;
          if (typeof cid === 'string' && cid.length > 0) {
            return cid;
          }
          fiber = fiber.return;
        }
      }
      cur = cur.parentElement;
    }
    const store = findReduxStore();
    const state = store?.getState();
    const stateCid =
      state?.conversation?.cascadeId ||
      state?.multiConvoLayout?.focusedPaneId ||
      '';
    if (typeof stateCid === 'string' && stateCid) {
      return stateCid;
    }
    const path = window.location?.pathname || '';
    const m = /\/(?:c|cascade|conversation)\/([a-zA-Z0-9_-]+)/.exec(path);
    return m ? m[1] : '';
  }

  // Syncs the titlebar archive button label and icon with the chat annotations
  function updateTitlebarArchiveButtonUI(btn, cascadeId) {
    if (!btn || !cascadeId) return;
    btn.setAttribute('data-cascade-id', cascadeId);
    const archived = isArchivedCascadeId(cascadeId);
    const archStr = archived ? 'true' : 'false';
    if (btn.getAttribute('data-archived') !== archStr) {
      btn.setAttribute('data-archived', archStr);
    }
    const label = archived ? 'Unarchive chat' : 'Archive chat';
    if (btn.getAttribute('aria-label') !== label) {
      btn.setAttribute('aria-label', label);
    }
    // Strip native tooltip so it does not overlap the custom surface-glass hover bubble
    if (btn.hasAttribute('title')) {
      btn.removeAttribute('title');
    }
    // Render a pure icon without text spans so the titlebar stays compact
    const nextText = archived ? '⦿' : '🗄';
    if (btn.textContent !== nextText) {
      btn.textContent = nextText;
    }
  }

  // Formats token counts for the compact titlebar pill without rounding across unit boundaries
  function formatCompactTokens(tokens) {
    const n = Math.max(0, Math.round(Number(tokens) || 0));
    if (n < 1000) return String(n);
    if (n < 9950) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k`;
    if (n < 999500) return `${Math.round(n / 1000)}k`;
    return `${(n / 1000000).toFixed(1).replace(/\.0$/, '')}M`;
  }

  function formatExactInt(n) {
    return String(Math.max(0, Math.round(Number(n) || 0))).replace(
      /\B(?=(\d{3})+(?!\d))/g,
      ','
    );
  }

  // Prevents upward rounding across 60% / 65% / 80% / 100% before pct actually reaches them
  function formatDisplayPct(pct) {
    let p = Math.round(pct);
    if (pct < CONTEXT_SOFT_PCT && p >= CONTEXT_SOFT_PCT) p = CONTEXT_SOFT_PCT - 1;
    else if (pct < CONTEXT_TURN_START_PCT && p >= CONTEXT_TURN_START_PCT) {
      p = CONTEXT_TURN_START_PCT - 1;
    } else if (pct < CONTEXT_HARD_PCT && p >= CONTEXT_HARD_PCT) p = CONTEXT_HARD_PCT - 1;
    else if (pct < 100 && p >= 100) p = 99;
    return Math.max(0, p);
  }

  // Parses router arm, backend checkpoint, and underlying model family from responseModel
  function parseBackendDetails(routeId, backendRaw) {
    const rawR = String(routeId || '').trim();
    const rawB = String(backendRaw || '').trim();
    if (!rawR && !rawB) return { familyShort: '', backendDisplay: '' };

    let familyShort = '';
    const full = `${rawR} ${rawB}`.toLowerCase();
    if (full.includes('barium-b')) familyShort = 'Barium-B';
    else if (full.includes('skimaki')) familyShort = 'Skimaki';
    else if (full.includes('carbon-g')) familyShort = 'Carbon-G';
    else if (full.includes('perseus')) familyShort = 'Perseus';
    else if (full.includes('cswise')) familyShort = 'CSWise';
    else if (full.includes('snowball')) familyShort = 'Snowball';
    else if (full.includes('argon')) familyShort = 'Argon';

    let ckpt = '';
    if (rawB) {
      const gMatch = /Gemini-\d[A-Za-z0-9._-]+/i.exec(rawB);
      if (gMatch) {
        ckpt = gMatch[0]
          .replace(/-EEVEE$/i, '')
          .replace(/-(?:Barium-B|Topo-Skimaki|Skimaki|Carbon-G)$/i, '');
      } else {
        const soupMatch = /soup_[A-Za-z0-9_]+/i.exec(rawB);
        if (soupMatch) {
          ckpt = soupMatch[0];
        } else if (!rawB.includes('/')) {
          ckpt = rawB.replace(/@default$/i, '');
        } else {
          ckpt = rawB.split('/')[0].replace(/-(?:jetski(?:-launch-prep)?|agy-fast)$/i, '');
        }
      }
    }

    const parts = [];
    if (rawR) parts.push(rawR);
    if (familyShort && ckpt) {
      parts.push(`${familyShort} (${ckpt})`);
    } else if (familyShort || ckpt) {
      parts.push(familyShort || ckpt);
    }
    const backendDisplay = parts.join(' · ');
    return { familyShort, backendDisplay };
  }

  // Shortens display name and appends router family when not already in the label
  function formatShortModelBadge(displayName, familyShort) {
    if (!displayName) return '';
    let b = String(displayName).trim();
    if (b.startsWith('Gemini Pro Orchestrator')) {
      b = 'Orchestrator';
    } else if (b.startsWith('Gemini 3.8 Flash (router)') || b === 'Gemini 3.8 Flash Router') {
      b = 'Flash 3.8';
    } else if (b.startsWith('Claude ')) {
      b = b.slice(7);
    } else if (b.startsWith('Gemini ')) {
      b = b.slice(7);
    }
    b = b.replace(/\(Medium\)/i, '(Med)');
    if (
      familyShort &&
      !b.toLowerCase().includes(familyShort.toLowerCase().replace(/-g$/, ''))
    ) {
      b = `${b} (${familyShort})`;
    }
    return b;
  }

  // Resolves actual model information from generatorMetadata chatModel
  function parseActualModelInfo(cm, fallbackResponseModel) {
    const rawEnum = String(cm?.model || '').trim();
    const rawDisplay = String(cm?.modelDisplayName || '').trim();
    const rawResponseModel = String(cm?.responseModel || fallbackResponseModel || '').trim();
    if (!rawEnum && !rawDisplay && !rawResponseModel) return null;

    const pipeIdx = rawResponseModel.indexOf('|');
    const routeId = (pipeIdx >= 0 ? rawResponseModel.slice(0, pipeIdx) : rawResponseModel).trim();
    const backendRaw = (pipeIdx >= 0 ? rawResponseModel.slice(pipeIdx + 1) : '').trim();

    const { familyShort, backendDisplay } = parseBackendDetails(routeId, backendRaw);

    const mMatch = /^MODEL_PLACEHOLDER_M(\d+)$/.exec(rawEnum);
    const enumShort = mMatch ? `M${mMatch[1]}` : rawEnum;

    let baseRouteId = routeId;
    if (baseRouteId.endsWith('-router-plan')) {
      baseRouteId = baseRouteId.slice(0, -12);
    } else if (baseRouteId.endsWith('-router-exec')) {
      baseRouteId = baseRouteId.slice(0, -12);
    }

    const curated =
      CURATED_HIDDEN_MODEL_MAP.get(routeId) ||
      CURATED_HIDDEN_MODEL_MAP.get(baseRouteId);

    const catalog = buildModelCatalogIndex(findReduxStore());
    const enumVal = resolveModelEnumValue(rawEnum);
    const catalogEntry =
      (routeId && catalog.byId.get(routeId.toLowerCase())) ||
      (baseRouteId && catalog.byId.get(baseRouteId.toLowerCase())) ||
      (enumVal && catalog.byEnum.get(enumVal)) ||
      (rawEnum && catalog.byId.get(rawEnum.toLowerCase()));

    let displayName = curated?.label || '';
    if (!displayName && catalogEntry?.label && catalogEntry.label !== 'Gemini Pro Next') {
      displayName = catalogEntry.label;
    }
    if (!displayName && rawDisplay && rawDisplay !== 'Gemini Pro Next' && rawDisplay !== 'None') {
      displayName = rawDisplay;
    }
    if (!displayName && catalogEntry?.label) {
      displayName = catalogEntry.label;
    }
    if (!displayName && routeId) {
      displayName = humanizeModelId(routeId);
    }
    if (!displayName) {
      displayName = rawDisplay || rawEnum || 'Unknown Model';
    }

    const shortBadge = formatShortModelBadge(displayName, familyShort);
    return {
      rawEnum,
      enumShort,
      displayName,
      shortBadge,
      responseModel: rawResponseModel,
      routeId,
      familyShort,
      backendDisplay,
    };
  }

  // Returns selected model label for next turn from Redux modelSelection
  function resolveNextTurnModelLabel(cascadeId) {
    const store = findReduxStore();
    const state = store?.getState();
    const selection = state?.modelSelection;
    if (!selection) return '';
    const rawVal =
      (cascadeId && selection.byConversation?.[cascadeId]?.model) ??
      selection.default?.model;
    const enumVal = resolveModelEnumValue(rawVal);
    if (!enumVal) return '';
    const catalog = buildModelCatalogIndex(store);
    const entry = catalog.byEnum.get(enumVal);
    let label = entry?.label;
    if (!label && enumVal >= 1000) {
      const armId = `bon-experiment-arm-${enumVal - 1000}`;
      const curated = CURATED_HIDDEN_MODEL_MAP.get(armId);
      if (curated) label = curated.label;
    }
    if (!label) {
      label = humanizeModelId(String(rawVal));
    }
    const pinKey = resolveDraftPinKey(store, cascadeId);
    const pin = getManualPinEntry(pinKey);
    if (pin && pin.model === enumVal) {
      label = `${label} · Pinned`;
    }
    return label;
  }

  // Extracts the latest valid contextWindowMetadata entry, matching context_guard.py semantics
  function parseLatestContextMetadata(gm) {
    if (!Array.isArray(gm)) return null;
    let latestModelCm = null;
    let latestModelResponseModel = '';
    for (let j = gm.length - 1; j >= 0; j--) {
      const cm = gm[j]?.chatModel;
      if (cm && (cm.model || cm.modelDisplayName || cm.responseModel)) {
        if (!latestModelCm) latestModelCm = cm;
        if (!latestModelResponseModel && cm.responseModel) {
          if (!latestModelCm.model || cm.model === latestModelCm.model) {
            latestModelResponseModel = cm.responseModel;
          }
        }
        if (latestModelCm && latestModelResponseModel) break;
      }
    }

    for (let i = gm.length - 1; i >= 0; i--) {
      const csm = gm[i]?.chatModel?.chatStartMetadata;
      const cwm = csm?.contextWindowMetadata;
      const maxTokens = Math.round(Number(cwm?.maxContextTokens) || 0);
      if (
        maxTokens <= 0 ||
        (cwm?.estimatedTokensUsed == null && cwm?.tokenBreakdown?.totalTokens == null)
      ) {
        continue;
      }
      const usedTokens = Math.max(
        0,
        Math.round(
          Number(cwm.estimatedTokensUsed ?? cwm.tokenBreakdown?.totalTokens ?? 0) || 0
        )
      );
      const freeTokens = Math.max(0, maxTokens - usedTokens);
      const pct = (100 * usedTokens) / maxTokens;
      // Proto3 JSON sends -1 before compaction and omits 0 on the first checkpoint
      const ckpt = Number(csm?.checkpointIndex ?? 0);
      const compacted = Number.isFinite(ckpt) && ckpt >= 0;
      let level = 'ok';
      if (compacted || pct >= CONTEXT_HARD_PCT) level = 'hard';
      else if (pct >= CONTEXT_TURN_START_PCT) level = 'turn-start';
      else if (pct >= CONTEXT_SOFT_PCT) level = 'soft';

      const rawGroups = Array.isArray(cwm?.tokenBreakdown?.groups)
        ? cwm.tokenBreakdown.groups
        : [];
      const groups = rawGroups
        .filter((g) => g && Number.isFinite(Number(g.numTokens)))
        .map((g) => ({
          name: String(g.name || g.type || 'Other'),
          tokens: Math.max(0, Math.round(Number(g.numTokens) || 0)),
        }));

      const usedStr = formatCompactTokens(usedTokens);
      const maxStr = formatCompactTokens(maxTokens);
      const dispPct = formatDisplayPct(pct);
      const prefix = compacted ? '⟳ ' : '';

      const modelInfo = parseActualModelInfo(
        latestModelCm || gm[i]?.chatModel,
        latestModelResponseModel
      );
      const modelTag = modelInfo?.shortBadge ? `${modelInfo.shortBadge} · ` : '';

      return {
        matchedIndex: i,
        usedTokens,
        maxTokens,
        freeTokens,
        pct,
        ckpt: Number.isFinite(ckpt) ? ckpt : -1,
        compacted,
        level,
        groups,
        modelInfo,
        badgeText: `${prefix}${modelTag}${usedStr} / ${maxStr} · ${dispPct}%`,
      };
    }
    return null;
  }

  function getContextSummarySig(cascadeId) {
    const s = findReduxStore()?.getState()?.trajectorySummaries?.summaries?.[cascadeId];
    if (!s) return '__none__';
    return [
      s.status || '',
      parseTimestampMs(s.lastModifiedTime),
      s.stepCount ?? '',
      Boolean(s.notFullyIdle),
    ].join(':');
  }

  function clearContextEntryTimers(entry) {
    if (entry.throttleTimerId) {
      clearTimeout(entry.throttleTimerId);
      entry.throttleTimerId = 0;
    }
    if (entry.pollTimerId) {
      clearTimeout(entry.pollTimerId);
      entry.pollTimerId = 0;
    }
  }

  function getOrCreateContextCacheEntry(cascadeId) {
    let entry = contextUsageCache.get(cascadeId);
    if (entry) {
      contextUsageCache.delete(cascadeId);
      contextUsageCache.set(cascadeId, entry);
      return entry;
    }
    if (contextUsageCache.size >= CONTEXT_CACHE_MAX_ENTRIES) {
      for (const [oldId, oldEntry] of contextUsageCache.entries()) {
        if (!oldEntry.inFlight && !oldEntry.throttleTimerId && !oldEntry.pollTimerId) {
          contextUsageCache.delete(oldId);
          break;
        }
      }
    }
    entry = {
      lastValidIndex: 0,
      summarySig: '',
      fetchedAt: 0,
      inFlight: false,
      pendingRefresh: false,
      pendingForce: false,
      throttleTimerId: 0,
      pollTimerId: 0,
      info: null,
    };
    contextUsageCache.set(cascadeId, entry);
    return entry;
  }

  async function fetchContextUsageOnce(cascadeId, offset) {
    if (typeof window.fetch !== 'function') return { ok: false, gm: [] };
    try {
      const resp = await window.fetch(
        '/exa.language_server_pb.LanguageServerService/GetCascadeTrajectoryGeneratorMetadata',
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'x-codeium-csrf-token': getCsrfToken(),
          },
          body: JSON.stringify({
            cascadeId,
            generatorMetadataOffset: offset,
            includeMessages: false,
          }),
        }
      );
      if (!resp || !resp.ok) return { ok: false, gm: [] };
      const data = await resp.json();
      return {
        ok: true,
        gm: Array.isArray(data?.generatorMetadata) ? data.generatorMetadata : [],
      };
    } catch {
      return { ok: false, gm: [] };
    }
  }

  // True only when cascadeId is actively running and mounted in an open pane titlebar
  function isOpenRunningCascade(cascadeId) {
    const summary = findReduxStore()?.getState()?.trajectorySummaries?.summaries?.[cascadeId];
    if (!isActiveRunStatus(summary?.status)) return false;
    for (const b of document.querySelectorAll(`.${TITLEBAR_CONTEXT_BADGE_CLASS}`)) {
      if (b.getAttribute('data-cascade-id') === cascadeId) return true;
    }
    return false;
  }

  async function triggerContextUsageFetch(cascadeId) {
    if (disposed || !cascadeId) return;
    const entry = getOrCreateContextCacheEntry(cascadeId);
    if (entry.inFlight) return;
    entry.inFlight = true;
    entry.pendingRefresh = false;
    entry.pendingForce = false;
    const reqSig = getContextSummarySig(cascadeId);
    entry.fetchedAt = Date.now();
    let resOk = false;
    try {
      const initialOffset = entry.lastValidIndex > 0 ? entry.lastValidIndex : 0;
      let usedOffset = initialOffset;
      let res = await fetchContextUsageOnce(cascadeId, initialOffset);
      let parsed = res.ok ? parseLatestContextMetadata(res.gm) : null;
      // Retry from 0 when trajectory rewound or cached anchor lost contextWindowMetadata
      if (res.ok && initialOffset > 0 && (res.gm.length === 0 || !parsed)) {
        usedOffset = 0;
        res = await fetchContextUsageOnce(cascadeId, 0);
        parsed = res.ok ? parseLatestContextMetadata(res.gm) : null;
      }
      resOk = res.ok;
      if (!disposed && res.ok) {
        entry.summarySig = reqSig;
        entry.lastValidIndex = parsed ? usedOffset + parsed.matchedIndex : 0;
        const prevSig = entry.info
          ? `${entry.info.usedTokens}:${entry.info.maxTokens}:${entry.info.ckpt}:${entry.info.badgeText}`
          : '';
        entry.info = parsed;
        const nextSig = parsed
          ? `${parsed.usedTokens}:${parsed.maxTokens}:${parsed.ckpt}:${parsed.badgeText}`
          : '';
        if (prevSig !== nextSig) {
          scheduleSync();
        }
      }
    } finally {
      entry.inFlight = false;
      const latestSig = getContextSummarySig(cascadeId);
      if (!disposed && (entry.pendingRefresh || (resOk && latestSig !== reqSig))) {
        const force = entry.pendingForce;
        entry.pendingRefresh = false;
        entry.pendingForce = false;
        ensureContextUsageLoaded(cascadeId, force);
      } else if (
        !disposed &&
        !entry.throttleTimerId &&
        !entry.pollTimerId &&
        isOpenRunningCascade(cascadeId)
      ) {
        // Re-check open running turns during silent model thinking; stops immediately once idle
        entry.pollTimerId = setTimeout(() => {
          entry.pollTimerId = 0;
          if (!disposed && isOpenRunningCascade(cascadeId)) {
            ensureContextUsageLoaded(cascadeId, false);
          }
        }, CONTEXT_RUNNING_POLL_MS);
      }
    }
  }

  // Coalesces context metadata fetches per conversation with zero background polling when idle
  function ensureContextUsageLoaded(cascadeId, forceRefresh = false) {
    if (disposed || !cascadeId) return;
    const entry = getOrCreateContextCacheEntry(cascadeId);
    const summary = findReduxStore()?.getState()?.trajectorySummaries?.summaries?.[cascadeId];
    const summarySig = getContextSummarySig(cascadeId);
    const now = Date.now();
    const isRunningDue =
      isActiveRunStatus(summary?.status) &&
      now - entry.fetchedAt >= CONTEXT_RUNNING_POLL_MS;
    const needsFetch =
      forceRefresh ||
      entry.fetchedAt === 0 ||
      summarySig !== entry.summarySig ||
      isRunningDue;
    if (!needsFetch) return;

    if (entry.inFlight) {
      entry.pendingRefresh = true;
      if (forceRefresh) entry.pendingForce = true;
      return;
    }

    const elapsed = now - entry.fetchedAt;
    if (!forceRefresh && entry.fetchedAt > 0 && elapsed < CONTEXT_MIN_FETCH_INTERVAL_MS) {
      // Pre-empt any conditional 4s running poll so fast idle completions still fire
      if (entry.pollTimerId) {
        clearTimeout(entry.pollTimerId);
        entry.pollTimerId = 0;
      }
      if (!entry.throttleTimerId) {
        const waitMs = Math.max(20, CONTEXT_MIN_FETCH_INTERVAL_MS - elapsed);
        entry.throttleTimerId = setTimeout(() => {
          entry.throttleTimerId = 0;
          if (!disposed) {
            void triggerContextUsageFetch(cascadeId);
          }
        }, waitMs);
      }
      return;
    }

    clearContextEntryTimers(entry);
    void triggerContextUsageFetch(cascadeId);
  }

  function buildContextBubbleHtml(info, cascadeId) {
    const dotColors = {
      ok: '#8ab4f8',
      soft: '#fbbf24',
      'turn-start': '#fb923c',
      hard: '#f87171',
    };
    const dotColor = dotColors[info.level] || '#8ab4f8';
    const compactTag = info.compacted ? ` · Compacted #${info.ckpt + 1}` : '';
    const rowsHtml = info.groups
      .map((g) => {
        const grpPct = info.maxTokens > 0 ? ((100 * g.tokens) / info.maxTokens).toFixed(1) : '0.0';
        return (
          `<div class="jcfb-ctx-row">` +
          `<span>${escapeHtml(g.name)}</span>` +
          `<span class="jcfb-ctx-val">${formatExactInt(g.tokens)} (${grpPct}%)</span>` +
          `</div>`
        );
      })
      .join('');
    const breakdownBlock = rowsHtml
      ? `<div class="jcfb-section-title">Token breakdown</div>${rowsHtml}`
      : '';

    const modelInfo = info.modelInfo;
    const nextTurnModel = resolveNextTurnModelLabel(cascadeId);
    let modelRows = '';
    if (modelInfo) {
      const enumSuffix =
        modelInfo.enumShort && modelInfo.enumShort !== modelInfo.displayName
          ? ` (${escapeHtml(modelInfo.enumShort)})`
          : '';
      modelRows +=
        `<div class="jcfb-ctx-row">` +
        `<span>Last turn</span>` +
        `<span class="jcfb-ctx-val">${escapeHtml(modelInfo.displayName)}${enumSuffix}</span>` +
        `</div>`;
      if (modelInfo.backendDisplay) {
        modelRows +=
          `<div class="jcfb-ctx-row">` +
          `<span>Backend</span>` +
          `<span class="jcfb-ctx-val">${escapeHtml(modelInfo.backendDisplay)}</span>` +
          `</div>`;
      }
    }
    if (nextTurnModel) {
      modelRows +=
        `<div class="jcfb-ctx-row">` +
        `<span>Next turn</span>` +
        `<span class="jcfb-ctx-val">${escapeHtml(nextTurnModel)}</span>` +
        `</div>`;
    }
    const modelBlock = modelRows
      ? `<div class="jcfb-section-title">Model</div>${modelRows}`
      : '';

    let statusNote = 'Status: Within budget (< 60%)';
    if (info.compacted) statusNote = 'Status: Compacted — handoff required';
    else if (info.level === 'hard') statusNote = 'Status: Hard limit (>= 80%) — handoff now';
    else if (info.level === 'turn-start') {
      statusNote = 'Status: Turn-start limit (>= 65%) — handoff on next turn';
    } else if (info.level === 'soft') {
      statusNote = 'Status: Soft warning (>= 60%) — handoff at micro-boundary';
    }
    return (
      `<div class="jcfb-header">` +
      `<span class="jcfb-header-left">` +
      `<span class="jcfb-dot" style="background:${dotColor}"></span>` +
      `<span>Context Window</span>` +
      `</span>` +
      `<span>${info.pct.toFixed(1)}%${escapeHtml(compactTag)}</span>` +
      `</div>` +
      `<div class="jcfb-desc">` +
      `${formatExactInt(info.usedTokens)} / ${formatExactInt(info.maxTokens)} tokens · ` +
      `${formatExactInt(info.freeTokens)} free` +
      `</div>` +
      modelBlock +
      breakdownBlock +
      `<div class="jcfb-ctx-footer">` +
      `<div>${escapeHtml(statusNote)}</div>` +
      `<div>Guards: Soft 60% · Turn-start 65% · Hard 80%</div>` +
      `</div>`
    );
  }

  function setAttrIfChanged(el, attr, val) {
    if (el.getAttribute(attr) !== val) el.setAttribute(attr, val);
  }

  function showContextBubbleForBadge(badge, cascadeId) {
    const info = contextUsageCache.get(cascadeId)?.info;
    if (!badge || !info) {
      hideBubble();
      return;
    }
    activeHoverBtn = null;
    activeHoverBucket = null;
    activeHoverRoadmapBadge = null;
    activeHoverRoadmapProjectId = null;
    activeHoverContextBadge = badge;
    activeHoverContextCascadeId = cascadeId;
    const bubble = ensureBubbleElement();
    if (!bubble) return;
    setAttrIfChanged(bubble, 'data-mode', 'context');
    const nextHtml = buildContextBubbleHtml(info, cascadeId);
    if (bubble.innerHTML !== nextHtml) {
      bubble.innerHTML = nextHtml;
    }
    setAttrIfChanged(bubble, 'data-visible', 'true');
    const rect = badge.getBoundingClientRect();
    const bubbleWidth = 312;
    let left = Math.max(8, Math.min(rect.right - bubbleWidth, window.innerWidth - bubbleWidth - 8));
    bubble.style.left = `${Math.round(left)}px`;
    bubble.style.top = `${Math.round(rect.bottom + 6)}px`;
  }

  function updateTitlebarContextBadgeUI(badge, cascadeId) {
    if (!badge || !cascadeId) return;
    setAttrIfChanged(badge, 'data-cascade-id', cascadeId);
    ensureContextUsageLoaded(cascadeId, false);
    const info = contextUsageCache.get(cascadeId)?.info || null;
    if (!info) {
      setAttrIfChanged(badge, 'data-empty', 'true');
      if (badge.style.display !== 'none') badge.style.display = 'none';
      if (activeHoverContextBadge === badge) hideBubble();
      return;
    }
    setAttrIfChanged(badge, 'data-empty', 'false');
    if (badge.style.display !== '') badge.style.display = '';
    setAttrIfChanged(badge, 'data-level', info.level);
    setAttrIfChanged(badge, 'data-compacted', info.compacted ? 'true' : 'false');
    setAttrIfChanged(badge, 'data-actual-model', info.modelInfo?.shortBadge || '');
    if (badge.textContent !== info.badgeText) badge.textContent = info.badgeText;
    setAttrIfChanged(badge, 'aria-label', `Context usage: ${info.badgeText}`);
    if (activeHoverContextBadge === badge) {
      showContextBubbleForBadge(badge, cascadeId);
    }
  }

  // Injects or updates the Context badge, Search, Locate, and Archive buttons before ⋮
  function ensureTitlebarButtons() {
    const moreActionsBtns = document.querySelectorAll(
      '[data-testid="titlebar-more-actions"]'
    );
    for (const moreBtn of moreActionsBtns) {
      const container = moreBtn.parentElement;
      if (!container) continue;
      const cascadeId = findCascadeIdFromElement(moreBtn);
      if (!cascadeId) continue;

      // Mount context pill leftmost before Search so archived pieces also show token usage
      let ctxBadge = container.querySelector(`.${TITLEBAR_CONTEXT_BADGE_CLASS}`);
      if (!ctxBadge) {
        ctxBadge = document.createElement('span');
        ctxBadge.className = TITLEBAR_CONTEXT_BADGE_CLASS;
        ctxBadge.setAttribute('data-testid', 'jetski-titlebar-context-badge');
        ctxBadge.setAttribute('role', 'button');
        ctxBadge.setAttribute('tabIndex', '0');
        const resolveCid = () =>
          ctxBadge.getAttribute('data-cascade-id') || findCascadeIdFromElement(moreBtn);
        const onEnter = () => {
          const cid = resolveCid();
          if (cid) showContextBubbleForBadge(ctxBadge, cid);
        };
        ctxBadge.addEventListener('mouseenter', onEnter);
        ctxBadge.addEventListener('mouseleave', hideBubble);
        ctxBadge.addEventListener('focus', onEnter);
        ctxBadge.addEventListener('blur', hideBubble);
        bindActionBtnHandlers(ctxBadge, () => {
          const cid = resolveCid();
          if (cid) ensureContextUsageLoaded(cid, true);
        });
        const firstInjected = container.querySelector(`.${SEARCH_BTN_CLASS}`) || moreBtn;
        container.insertBefore(ctxBadge, firstInjected);
      }
      updateTitlebarContextBadgeUI(ctxBadge, cascadeId);

      // Chain search lives in the titlebar because it needs the open chat as its scope
      if (!container.querySelector(`.${SEARCH_BTN_CLASS}`)) {
        const searchBtn = document.createElement('button');
        searchBtn.className = SEARCH_BTN_CLASS;
        searchBtn.type = 'button';
        searchBtn.textContent = '🔍';
        searchBtn.setAttribute('aria-label', 'Search this chat and its hidden pieces');
        bindHoverBubble(searchBtn, '🔍 Search this chat and its hidden pieces');
        bindActionBtnHandlers(searchBtn, () => {
          const cid = findCascadeIdFromElement(moreBtn);
          if (cid) openSearchPanel(cid);
        });
        container.insertBefore(searchBtn, moreBtn);
      }

      // Reveals the active conversation or live chain tail in the left sidebar
      let locateBtn = container.querySelector(`.${LOCATE_BTN_CLASS}`);
      if (!locateBtn) {
        locateBtn = document.createElement('button');
        locateBtn.className = LOCATE_BTN_CLASS;
        locateBtn.type = 'button';
        locateBtn.setAttribute('data-testid', 'jetski-chat-locate-btn');
        locateBtn.textContent = '🎯';
        locateBtn.setAttribute('aria-label', 'Locate this chat in sidebar');
        locateBtn.setAttribute('data-cascade-id', cascadeId);
        bindHoverBubble(locateBtn, '🎯 Locate this chat in sidebar');
        bindActionBtnHandlers(locateBtn, () => {
          const cid =
            locateBtn.getAttribute('data-cascade-id') || findCascadeIdFromElement(moreBtn);
          if (cid) {
            void locateConversationInSidebar(cid);
          }
        });
        container.insertBefore(locateBtn, moreBtn);
      } else if (locateBtn.getAttribute('data-cascade-id') !== cascadeId) {
        locateBtn.setAttribute('data-cascade-id', cascadeId);
      }

      let btn = container.querySelector(`.${TITLEBAR_BTN_CLASS}`);
      if (!btn) {
        btn = document.createElement('button');
        btn.className = TITLEBAR_BTN_CLASS;
        btn.type = 'button';
        btn.setAttribute('data-testid', 'jetski-titlebar-archive-btn');
        bindHoverBubble(btn, () => `${btn.textContent} ${btn.getAttribute('aria-label')}`);
        bindActionBtnHandlers(btn, () => {
          const cid =
            btn.getAttribute('data-cascade-id') || findCascadeIdFromElement(moreBtn);
          if (cid) {
            void toggleConversationArchived(cid);
          }
        });
        container.insertBefore(btn, moreBtn);
      }
      updateTitlebarArchiveButtonUI(btn, cascadeId);
    }
  }

  // Visually swaps ⦿ -> ⏳ in the row title and renders a right-side ⏳ badge while an open
  // leaf chat is idle waiting on a background task or subagent, without touching .pbtxt
  function syncRowBackgroundWaitUI(
    rowEl,
    cascadeId,
    summary,
    rawDomTitle,
    summariesMap,
    childIndex
  ) {
    // Skip detached or unidentified row nodes
    if (!rowEl || !cascadeId) return;
    const curTitle =
      summary?.annotations?.title || summary?.summary || rawDomTitle || '';
    const statusInfo = summary
      ? resolveConversationStatus(
          cascadeId,
          summary,
          curTitle,
          summariesMap,
          childIndex
        )
      : null;
    const waitingBg = isWaitingOnBackgroundWork(summary, curTitle, statusInfo);
    const spanEl = rowEl.querySelector('span.truncate');

    // Apply visual ⏳ swap and right-side indicator while background work is active
    if (waitingBg) {
      // Mark the row so CSS hides false unread blue dots while subagents run
      if (rowEl.getAttribute('data-jetski-bg-waiting') !== 'true') {
        rowEl.setAttribute('data-jetski-bg-waiting', 'true');
      }
      // Replace the leading ⦿ marker in the DOM title span while saving the original title
      if (spanEl && ACTIVE_TO_WAITING_DISPLAY_RE.test(curTitle)) {
        // Record the raw ⦿ title so bucket classification and restoration stay exact
        if (spanEl.getAttribute('data-jetski-orig-title') !== curTitle) {
          spanEl.setAttribute('data-jetski-orig-title', curTitle);
        }
        const displayTitle = curTitle.replace(
          ACTIVE_TO_WAITING_DISPLAY_RE,
          `$1${OPEN_WAITING_MARKER}$2`
        );
        // Guard textContent write to avoid redundant childList mutations
        if (spanEl.textContent !== displayTitle) {
          spanEl.textContent = displayTitle;
        }
      }
      const waitLabel =
        statusInfo?.workingDetail && statusInfo.workingDetail !== 'Agent turn running'
          ? statusInfo.workingDetail
          : 'Waiting on background task or subagent';
      let badge = rowEl.querySelector(`.${ROW_BG_WAIT_INDICATOR_CLASS}`);
      // Mount the right-side ⏳ badge once inside the row status cluster
      if (!badge) {
        badge = document.createElement('span');
        badge.className = ROW_BG_WAIT_INDICATOR_CLASS;
        badge.setAttribute('data-testid', 'jetski-row-bg-wait-indicator');
        badge.setAttribute('title', waitLabel);
        badge.setAttribute('aria-label', waitLabel);
        badge.textContent = OPEN_WAITING_MARKER;
        const anchorBtn =
          rowEl.querySelector('[data-testid="conversation-pin-button"]') ||
          rowEl.querySelector('[data-testid="conversation-archive-button"]');
        const hoverBar = anchorBtn?.parentElement;
        const statusWrapper = hoverBar?.parentElement;
        const statusContainer =
          statusWrapper && statusWrapper !== rowEl
            ? Array.from(statusWrapper.children || []).find((c) => c !== hoverBar) ||
              statusWrapper
            : rowEl;
        statusContainer.appendChild(badge);
      } else {
        setAttrIfChanged(badge, 'title', waitLabel);
        setAttrIfChanged(badge, 'aria-label', waitLabel);
      }
    } else {
      // Clear the background-wait attribute once the chat is no longer waiting
      if (rowEl.hasAttribute('data-jetski-bg-waiting')) {
        rowEl.removeAttribute('data-jetski-bg-waiting');
      }
      // Restore the original ⦿ title text when leaving background-wait state
      if (spanEl && spanEl.hasAttribute('data-jetski-orig-title')) {
        const origTitle = spanEl.getAttribute('data-jetski-orig-title') || '';
        spanEl.removeAttribute('data-jetski-orig-title');
        const targetTitle = curTitle || origTitle;
        // Only write textContent if it still differs from the restored title
        if (targetTitle && spanEl.textContent !== targetTitle) {
          spanEl.textContent = targetTitle;
        }
      }
      const badge = rowEl.querySelector(`.${ROW_BG_WAIT_INDICATOR_CLASS}`);
      // Remove the right-side ⏳ badge when background work finishes
      if (badge) {
        badge.remove();
      }
    }
  }

  // Injects or updates row action indicators; archiving stays with the native row button
  function ensureSidebarRowButtons() {
    // Read Redux summaries so each row can sync its live background-wait state
    const store = findReduxStore();
    const summariesMap = store?.getState()?.trajectorySummaries?.summaries || {};
    const childIndex = buildDescendantIndex(summariesMap);
    const rowEls = document.querySelectorAll(
      '[data-testid="conversation-row-sidebar"][data-cascade-id], div[data-sidebar-row-id]'
    );
    for (const rowEl of rowEls) {
      // Skip outer virtualizer wrappers that already contain an inner conversation-row-sidebar
      if (
        rowEl.getAttribute('data-testid') !== 'conversation-row-sidebar' &&
        rowEl.querySelector('[data-testid="conversation-row-sidebar"][data-cascade-id]')
      ) {
        continue;
      }
      const cascadeId =
        rowEl.getAttribute('data-cascade-id') ||
        rowEl.getAttribute('data-sidebar-row-id');
      if (!cascadeId) continue;

      // Sync the live ⏳ indicator before checking hover action buttons
      const fallbackDomTitle = getRawDomTitle(rowEl);
      syncRowBackgroundWaitUI(
        rowEl,
        cascadeId,
        summariesMap[cascadeId],
        fallbackDomTitle,
        summariesMap,
        childIndex
      );
    }
  }

  function runFullSync() {
    // Refresh the cached roadmap index and update project header badges on every sync pass
    ensureRoadmapIndexLoaded();
    ensureStylesInjected();
    const reduxActive = syncReduxFilter();
    syncDomRowsAndPlaceholders(reduxActive);
    ensureFilterBar();
    // Sync the Needs-Reaction inbox widget above the sidebar project list
    ensureNeedsReactionInbox();
    // Render or remove compact open-roadmap badges after project card DOM rows are synced
    ensureProjectRoadmapBadges();
    ensureTitlebarButtons();
    ensureSidebarRowButtons();
    // Synchronize chain stars and read-only piece locks across open conversation views
    void syncConversationChainNavigators();
  }

  function scheduleSync() {
    if (rafScheduled || disposed) return;
    rafScheduled = true;
    requestAnimationFrame(() => {
      rafScheduled = false;
      runFullSync();
    });
  }

  const observer = new MutationObserver((mutations) => {
    const hasExternalMutation = mutations.some((m) => {
      const target = m.target;
      if (!target || typeof target !== 'object') return true;
      // Ignore self-triggered mutations on injected filter controls and badges
      if (
        target.id === 'jetski-closed-chats-filter-bubble' ||
        target.id === STYLE_ID ||
        target.id === STAR_TOOLTIP_ID ||
        target.id === AUTOPILOT_TOAST_ID ||
        target.className === BAR_CLASS ||
        // Ignore mutations on the Needs-Reaction inbox container itself
        target.className === INBOX_CLASS ||
        target.className === BUTTON_CLASS ||
        target.className === TITLEBAR_BTN_CLASS ||
        // Ignore async token-count updates on the context pill to avoid feedback sync loops
        target.className === TITLEBAR_CONTEXT_BADGE_CLASS ||
        target.className === ROW_BG_WAIT_INDICATOR_CLASS ||
        target.className === PROJECT_ROADMAP_BADGE_CLASS ||
        target.className === SEARCH_BTN_CLASS ||
        target.className === LOCATE_BTN_CLASS ||
        target.className === CHAIN_STARS_GROUP_CLASS ||
        target.className === CHAIN_STAR_BTN_CLASS ||
        target.className === ARCHIVE_BANNER_CLASS ||
        (typeof target.closest === 'function' &&
          (target.closest('#jetski-closed-chats-filter-bubble') ||
            target.closest(`#${STAR_TOOLTIP_ID}`) ||
            target.closest(`.${BAR_CLASS}`) ||
            // Skip subtree mutations inside the Needs-Reaction inbox widget
            target.closest(`.${INBOX_CLASS}`) ||
            target.closest(`.${BUTTON_CLASS}`) ||
            target.closest(`.${TITLEBAR_BTN_CLASS}`) ||
            // Skip text node mutations inside the context volume pill
            target.closest(`.${TITLEBAR_CONTEXT_BADGE_CLASS}`) ||
            target.closest(`.${ROW_BG_WAIT_INDICATOR_CLASS}`) ||
            target.closest(`.${PROJECT_ROADMAP_BADGE_CLASS}`) ||
            target.closest(`.${LOCATE_BTN_CLASS}`) ||
            target.closest(`.${CHAIN_STARS_GROUP_CLASS}`) ||
            target.closest(`.${ARCHIVE_BANNER_CLASS}`) ||
            target.closest(`#${AUTOPILOT_TOAST_ID}`) ||
            target.closest(`#${SEARCH_PANEL_ID}`)))
      ) {
        return false;
      }
      return true;
    });
    if (hasExternalMutation) {
      scheduleSync();
    }
  });

  if (document.documentElement) {
    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
    });
  }

  window.__jetskiClosedChatsFilterCleanup = () => {
    unbindInputBoxSeenListeners();
    observer.disconnect();
    disposed = true;
    // Restore the un-enriched server UserStatus before tearing down the instance
    const curUserStatus = reduxStore?.getState()?.userStatus;
    const cleanUserStatus = stripUnlockedModels(curUserStatus);
    if (cleanUserStatus && cleanUserStatus !== curUserStatus) {
      reduxStore.dispatch({
        type: 'userStatus/setUserStatus',
        payload: cleanUserStatus,
      });
    }
    // A re-injected instance starts with empty managedHiddenIds and would read our flags as
    // native hiding, which drops those chats from every bucket, so hand them back unhidden
    const summaries = reduxStore?.getState()?.trajectorySummaries?.summaries || {};
    const unhide = [...managedHiddenIds.keys()]
      .filter((id) => summaries[id])
      .map((id) => [id, cloneSummaryWithHideFlag(summaries[id], false)]);
    managedHiddenIds.clear();
    if (unhide.length > 0) {
      reduxStore.dispatch({
        type: 'trajectorySummaries/applyStreamBatch',
        payload: { updates: unhide, deletes: [] },
      });
    }
    // Cancel pending context throttle and running-poll timers so stale closures cannot fire
    for (const entry of contextUsageCache.values()) {
      clearContextEntryTimers(entry);
    }
    contextUsageCache.clear();
    trajectoryStatusCache.clear();
    singleTrajCache.clear();
    if (autopilotToastTimer) {
      clearTimeout(autopilotToastTimer);
      autopilotToastTimer = 0;
    }
    manualPickedModels.clear();
    autoSeededTargets.clear();
    // Remove all injected filter buttons, search panels, and chain stars on reload
    const cleanupSelector = [
      `.${BAR_CLASS}`,
      // Remove the Needs-Reaction inbox widget on script reload
      `.${INBOX_CLASS}`,
      `.${BUTTON_CLASS}`,
      `.${TITLEBAR_BTN_CLASS}`,
      // Remove titlebar context pills so the fresh instance mounts its own closures
      `.${TITLEBAR_CONTEXT_BADGE_CLASS}`,
      `.${ROW_BG_WAIT_INDICATOR_CLASS}`,
      `.${PROJECT_ROADMAP_BADGE_CLASS}`,
      `.${SEARCH_BTN_CLASS}`,
      `.${LOCATE_BTN_CLASS}`,
      `#${SEARCH_PANEL_ID}`,
      `.${CHAIN_STARS_GROUP_CLASS}`,
      `.${ARCHIVE_BANNER_CLASS}`,
      `#${STAR_TOOLTIP_ID}`,
      `#${AUTOPILOT_TOAST_ID}`,
    ].join(', ');
    for (const el of document.querySelectorAll(cleanupSelector)) {
      el.remove();
    }
    if (locateHighlightTimer) {
      clearTimeout(locateHighlightTimer);
      locateHighlightTimer = 0;
    }
    for (const prev of document.querySelectorAll(`[${LOCATED_ROW_ATTR}="true"]`)) {
      prev.removeAttribute(LOCATED_ROW_ATTR);
    }
    // Restore any row titles whose ⦿ marker was visually swapped to ⏳
    for (const spanEl of document.querySelectorAll('span.truncate')) {
      if (spanEl.hasAttribute('data-jetski-orig-title')) {
        const origTitle = spanEl.getAttribute('data-jetski-orig-title') || '';
        spanEl.removeAttribute('data-jetski-orig-title');
        if (origTitle && spanEl.textContent !== origTitle) {
          spanEl.textContent = origTitle;
        }
      }
    }
    const bubble = document.getElementById('jetski-closed-chats-filter-bubble');
    if (bubble) {
      bubble.remove();
    }
    const toast = document.getElementById(AUTOPILOT_TOAST_ID);
    if (toast) {
      toast.remove();
    }
  };

  bindInputBoxSeenListeners();
  runFullSync();
})();
