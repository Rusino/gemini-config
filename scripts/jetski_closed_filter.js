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
  const BAR_CLASS = 'jetski-attention-filter-bar';
  const BUTTON_CLASS = 'jetski-closed-chats-filter-btn';
  const TITLEBAR_BTN_CLASS = 'jetski-titlebar-close-reopen-btn';
  const TITLEBAR_WAIT_BTN_CLASS = 'jetski-titlebar-wait-btn';
  const ROW_BTN_CLASS = 'jetski-row-close-reopen-btn';
  const ROW_WAIT_BTN_CLASS = 'jetski-row-wait-btn';
  // Right-side ⏳ badge rendered on open ⦿ rows waiting on background tasks or subagents
  const ROW_BG_WAIT_INDICATOR_CLASS = 'jetski-row-bg-wait-indicator';
  // Compact open-task badge rendered inside sidebar project headers
  const PROJECT_ROADMAP_BADGE_CLASS = 'jetski-project-roadmap-badge';
  const STYLE_ID = 'jetski-closed-chats-filter-style';

  // Lifecycle titles follow "[HH:MM] <marker> <Topic>" where marker encodes chain/wait state
  // The time prefix and the space after the marker are mandatory so a plain title opening
  // with a guillemet quote is never read as the finalized « marker
  const TITLE_MARKER_RE = /^\s*\[\d{1,2}:\d{2}\]\s*(‹✓›|«»|⏳|[✓⦿▸«»])(?:\s|$)/u;
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
  // chat_lifecycle.py finalize rewrites every chat of a chain to «, ‹✓›, » (or «» for a
  // single-chat task), so each title alone tells whether its task is closed
  const FINALIZED_MARKERS = new Set(['«', '‹✓›', '»', '«»']);
  // Open chains read ▸ root, ✓ handed-off steps, and ⦿ (Needs Me) or ⏳ (Waiting) leaf
  const OPEN_CHAIN_ROOT_MARKER = '▸';
  const OPEN_CHAIN_STEP_MARKER = '✓';
  const OPEN_ACTIVE_MARKER = '⦿';
  const OPEN_WAITING_MARKER = '⏳';
  const CLOSED_START_MARKER = '«';
  const CLOSED_STEP_MARKER = '‹✓›';
  const CLOSED_END_MARKER = '»';
  const CLOSED_SINGLE_MARKER = '«»';

  const BUCKET_ORDER = ['needs-me', 'working', 'waiting', 'steps', 'closed'];
  const DEFAULT_VISIBLE_BUCKETS = ['needs-me', 'working'];

  const BUCKET_DEFS = {
    'needs-me': {
      key: 'needs-me',
      icon: '🚨',
      shortLabel: 'Needs Me',
      title: '🚨 Needs Me (⦿)',
      description:
        'Open chats stopped and waiting for your reply, decision, or sign-off.',
    },
    working: {
      key: 'working',
      icon: '🤖',
      shortLabel: 'Working',
      title: '🤖 Agent Working',
      description:
        'Open chats where the agent or a background command/subagent is actively running right now.',
    },
    waiting: {
      key: 'waiting',
      icon: '⏳',
      shortLabel: 'Waiting',
      title: '⏳ Waiting (Cron / External)',
      description:
        'Background cron/timer chats or chats waiting on external events (Gerrit, CQ) that do not need your action right now.',
    },
    steps: {
      key: 'steps',
      icon: '▸✓',
      shortLabel: 'Chain Steps',
      title: '▸/✓ Handed-off Chain Steps',
      description:
        'Earlier handed-off parts (▸ root, ✓ intermediate step) of open multi-chat chains.',
    },
    closed: {
      key: 'closed',
      icon: '«»',
      shortLabel: 'Closed',
      title: '«» Closed Chats',
      description:
        'Completed and finalized single-chat tasks («») or handoff chains («, ‹✓›, »).',
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
          return new Set(valid);
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
  let activeHoverBtn = null;
  let activeHoverBucket = null;
  // Tracks which project roadmap badge owns the shared floating bubble tooltip
  let activeHoverRoadmapBadge = null;
  let activeHoverRoadmapProjectId = null;
  let chainIndexCache = null;
  let chainIndexFetchedAt = 0;
  let chainIndexFetching = false;
  let chainPredecessorIds = new Set();
  // Caches jetski-roadmaps.json across DOM sync cycles within ROADMAP_INDEX_TTL_MS
  const ROADMAP_INDEX_TTL_MS = 5000;
  let roadmapIndexCache = null;
  let roadmapIndexFetchedAt = 0;
  let roadmapIndexFetching = false;

  function parseTitleMarker(title) {
    if (typeof title !== 'string') return null;
    const match = TITLE_MARKER_RE.exec(title);
    return match ? match[1] : null;
  }

  function isFinalizedTitle(title) {
    const marker = parseTitleMarker(title);
    return Boolean(marker && FINALIZED_MARKERS.has(marker));
  }

  function isWaitingTitle(title) {
    return parseTitleMarker(title) === OPEN_WAITING_MARKER;
  }

  function isChainPredecessorTitle(title) {
    const marker = parseTitleMarker(title);
    return marker === OPEN_CHAIN_ROOT_MARKER || marker === OPEN_CHAIN_STEP_MARKER;
  }

  // Returns IDs that have at least one visible continuation child in jetski-chains.json
  function computeChainPredecessorIds(summariesMap, chainIndex) {
    const preds = new Set();
    const parentOf = chainIndex?.parentOf;
    if (!parentOf || typeof parentOf !== 'object') return preds;
    for (const [childId, parentId] of Object.entries(parentOf)) {
      if (!parentId || !childId) continue;
      if (!summariesMap || (childId in summariesMap && parentId in summariesMap)) {
        preds.add(parentId);
      }
    }
    return preds;
  }

  function isChainPredecessor(cascadeId, title) {
    return (
      isChainPredecessorTitle(title) ||
      Boolean(cascadeId && chainPredecessorIds.has(cascadeId))
    );
  }

  // True when an open leaf ⦿ chat has finished its LLM turn and is waiting on a subagent or task
  function isWaitingOnBackgroundWork(summary, fallbackTitle, cascadeId) {
    // Summaries without Redux state cannot report background task activity
    if (!summary) return false;
    const title =
      summary?.annotations?.title || summary?.summary || fallbackTitle || '';
    // Only open leaf chats with ⦿ qualify; cron ⏳ chats already have a permanent marker
    if (parseTitleMarker(title) !== OPEN_ACTIVE_MARKER) return false;
    // Handed-off chain predecessors never show active background wait state
    if (isChainPredecessor(cascadeId, title)) return false;
    return (
      summary.status !== 'CASCADE_RUN_STATUS_RUNNING' &&
      Boolean(summary.notFullyIdle)
    );
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
      return chainIndexCache || { parentOf: {}, createdAt: {} };
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
          };
          chainIndexFetchedAt = now;
          return chainIndexCache;
        }
      }
    } catch {
      // Ignore network errors and fall back to cached or single-chat resolution
    }
    return chainIndexCache || { parentOf: {}, createdAt: {} };
  }

  // Periodically refreshes jetski-chains.json in the background so chain steps filter by graph too
  function ensureChainIndexLoaded() {
    const now = Date.now();
    if (chainIndexFetching || (chainIndexCache && now - chainIndexFetchedAt < 15000)) {
      return;
    }
    if (typeof window.fetch !== 'function') return;
    chainIndexFetching = true;
    const prevCount = Object.keys(chainIndexCache?.parentOf || {}).length;
    fetchChainIndex()
      .then((idx) => {
        chainIndexFetching = false;
        const nextCount = Object.keys(idx?.parentOf || {}).length;
        if (nextCount !== prevCount) {
          scheduleSync();
        }
      })
      .catch(() => {
        chainIndexFetching = false;
      });
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

  function resolveChainIds(cascadeId, chainIndex, summariesMap) {
    if (!cascadeId) return [];
    const parentOf = chainIndex?.parentOf || {};
    const createdAt = { ...(chainIndex?.createdAt || {}) };
    if (summariesMap) {
      for (const [cid, s] of Object.entries(summariesMap)) {
        const ts = s?.trajectoryMetadata?.createdAt;
        if (ts && !createdAt[cid]) {
          createdAt[cid] = String(ts);
        }
      }
    }

    const childrenOf = {};
    for (const [child, parent] of Object.entries(parentOf)) {
      if (!parent) continue;
      if (!childrenOf[parent]) childrenOf[parent] = [];
      childrenOf[parent].push(child);
    }

    const ancReversed = [];
    const seenAnc = new Set();
    let cur = cascadeId;
    while (cur && !seenAnc.has(cur)) {
      seenAnc.add(cur);
      ancReversed.push(cur);
      cur = parentOf[cur];
    }
    const ancestors = ancReversed.reverse();
    const rootId = ancestors[0] || cascadeId;
    if (!childrenOf[rootId] && ancestors.length <= 1) {
      return [cascadeId];
    }

    const memoMaxTs = {};
    const memoHasChildren = {};
    function getSubtreeInfo(nid, visited) {
      if (nid in memoMaxTs) {
        return [memoMaxTs[nid], memoHasChildren[nid]];
      }
      const nextVisited = new Set(visited);
      nextVisited.add(nid);
      const chList = (childrenOf[nid] || []).filter((ch) => !nextVisited.has(ch));
      let maxTs = createdAt[nid] || '';
      for (const ch of chList) {
        const [chTs] = getSubtreeInfo(ch, nextVisited);
        if (chTs > maxTs) maxTs = chTs;
      }
      memoMaxTs[nid] = maxTs;
      memoHasChildren[nid] = chList.length > 0;
      return [maxTs, chList.length > 0];
    }
    getSubtreeInfo(rootId, new Set());

    const ordered = [];
    const seen = new Set();
    function dfs(nid) {
      if (seen.has(nid)) return;
      seen.add(nid);
      ordered.push(nid);
      const chList = (childrenOf[nid] || []).filter((ch) => !seen.has(ch));
      chList.sort((a, b) => {
        const maxA = memoMaxTs[a] || '';
        const maxB = memoMaxTs[b] || '';
        if (maxA !== maxB) return maxA < maxB ? -1 : 1;
        const hasA = memoHasChildren[a] ? 1 : 0;
        const hasB = memoHasChildren[b] ? 1 : 0;
        if (hasA !== hasB) return hasA - hasB;
        const tsA = createdAt[a] || '';
        const tsB = createdAt[b] || '';
        if (tsA !== tsB) return tsA < tsB ? -1 : 1;
        return a < b ? -1 : a > b ? 1 : 0;
      });
      for (const ch of chList) dfs(ch);
    }
    dfs(rootId);

    const fullChain = [];
    const seenOut = new Set();
    for (const cid of [...ancestors, ...ordered]) {
      if (cid && !seenOut.has(cid)) {
        seenOut.add(cid);
        fullChain.push(cid);
      }
    }
    return fullChain.length > 0 ? fullChain : [cascadeId];
  }

  function cloneSummaryWithTitle(summary, newTitle) {
    if (!summary) return summary;
    const summaryProto = Object.getPrototypeOf(summary);
    const nextSummary = Object.assign(Object.create(summaryProto), summary);
    nextSummary.summary = newTitle;
    const prevAnn = summary.annotations;
    const nextAnn = prevAnn
      ? Object.assign(Object.create(Object.getPrototypeOf(prevAnn)), prevAnn)
      : {};
    nextAnn.title = newTitle;
    nextSummary.annotations = nextAnn;
    return nextSummary;
  }

  function getTitleForCascadeId(cascadeId, fallbackDomTitle) {
    const store = findReduxStore();
    const s = store?.getState()?.trajectorySummaries?.summaries?.[cascadeId];
    const fromStore = s?.annotations?.title || s?.summary || '';
    return fromStore || fallbackDomTitle || '';
  }

  async function postUpdateTitleRpc(cascadeId, newTitle) {
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
            annotations: { title: newTitle },
            mergeAnnotations: true,
          }),
        }
      );
      return Boolean(resp && resp.ok);
    } catch {
      return false;
    }
  }

  // Swaps an open leaf conversation between ⦿ (Needs Me) and ⏳ (Waiting External/Cron)
  async function toggleConversationWaiting(cascadeId, fallbackDomTitle) {
    if (!cascadeId) return;
    const store = findReduxStore();
    const curTitle = getTitleForCascadeId(cascadeId, fallbackDomTitle);
    // Finalized («, ‹✓›, », «») and handed-off (▸, ✓) steps cannot toggle wait state
    if (isFinalizedTitle(curTitle) || isChainPredecessor(cascadeId, curTitle)) return;

    const nowTime = getCurrentTimeStr();
    const ts = extractTimePrefix(curTitle, nowTime);
    const base = cleanBaseTitle(curTitle) || 'Investigation';
    const nextMarker = isWaitingTitle(curTitle)
      ? OPEN_ACTIVE_MARKER
      : OPEN_WAITING_MARKER;
    const newTitle = `[${ts}] ${nextMarker} ${base}`;

    const s = store?.getState()?.trajectorySummaries?.summaries?.[cascadeId];
    // Apply optimistic Redux title update immediately before the async LS RPC completes
    if (store && s) {
      store.dispatch({
        type: 'trajectorySummaries/applyStreamBatch',
        payload: {
          updates: [[cascadeId, cloneSummaryWithTitle(s, newTitle)]],
          deletes: [],
        },
      });
    }

    runFullSync();
    await postUpdateTitleRpc(cascadeId, newTitle);
  }

  async function toggleConversationLifecycle(cascadeId, fallbackDomTitle) {
    if (!cascadeId) return;
    const store = findReduxStore();
    const summariesMap = store?.getState()?.trajectorySummaries?.summaries || {};
    const curTitle = getTitleForCascadeId(cascadeId, fallbackDomTitle);
    const closing = !isFinalizedTitle(curTitle);
    const nowTime = getCurrentTimeStr();

    const chainIndex = await fetchChainIndex();
    const chainIds = resolveChainIds(cascadeId, chainIndex, summariesMap);

    let chainTopic = 'Investigation';
    for (let i = chainIds.length - 1; i >= 0; i--) {
      const t = cleanBaseTitle(
        getTitleForCascadeId(chainIds[i], chainIds[i] === cascadeId ? fallbackDomTitle : '')
      );
      if (t && t !== 'Investigation') {
        chainTopic = t;
        break;
      }
    }

    const titleUpdates = [];
    for (let idx = 0; idx < chainIds.length; idx++) {
      const cid = chainIds[idx];
      const existingTitle = getTitleForCascadeId(
        cid,
        cid === cascadeId ? fallbackDomTitle : ''
      );
      let marker = OPEN_ACTIVE_MARKER;
      if (chainIds.length === 1) {
        marker = closing ? CLOSED_SINGLE_MARKER : OPEN_ACTIVE_MARKER;
      } else if (idx === 0) {
        marker = closing ? CLOSED_START_MARKER : OPEN_CHAIN_ROOT_MARKER;
      } else if (idx === chainIds.length - 1) {
        marker = closing ? CLOSED_END_MARKER : OPEN_ACTIVE_MARKER;
      } else {
        marker = closing ? CLOSED_STEP_MARKER : OPEN_CHAIN_STEP_MARKER;
      }

      const ts =
        chainIds.length === 1 && !closing
          ? nowTime
          : extractTimePrefix(existingTitle, nowTime);
      const base = cleanBaseTitle(existingTitle) || chainTopic;
      const newTitle = `[${ts}] ${marker} ${base}`;
      titleUpdates.push([cid, newTitle]);
    }

    if (store && store.getState()?.trajectorySummaries?.summaries) {
      const currentSummaries = store.getState().trajectorySummaries.summaries;
      const batchUpdates = [];
      for (const [cid, newTitle] of titleUpdates) {
        const s = currentSummaries[cid];
        if (s) {
          batchUpdates.push([cid, cloneSummaryWithTitle(s, newTitle)]);
        }
      }
      if (batchUpdates.length > 0) {
        store.dispatch({
          type: 'trajectorySummaries/applyStreamBatch',
          payload: { updates: batchUpdates, deletes: [] },
        });
      }
    }

    runFullSync();

    await Promise.all(
      titleUpdates.map(([cid, newTitle]) => postUpdateTitleRpc(cid, newTitle))
    );
  }

  // Classifies a conversation into one of the 5 mutually exclusive state buckets
  function classifyChatBucket(summary, fallbackTitle, cascadeId) {
    const title =
      summary?.annotations?.title || summary?.summary || fallbackTitle || '';
    const marker = parseTitleMarker(title);
    if (marker && FINALIZED_MARKERS.has(marker)) {
      return 'closed';
    }
    if (isChainPredecessor(cascadeId, title)) {
      return 'steps';
    }
    // Cron chats with marker ⏳ keep notFullyIdle=true between ticks, so only
    // CASCADE_RUN_STATUS_RUNNING counts as actively working for ⏳ chats
    const isRunning =
      summary?.status === 'CASCADE_RUN_STATUS_RUNNING' ||
      (marker !== OPEN_WAITING_MARKER && Boolean(summary?.notFullyIdle));
    if (isRunning) {
      return 'working';
    }
    if (marker === OPEN_WAITING_MARKER) {
      return 'waiting';
    }
    return 'needs-me';
  }

  function emptyBreakdown() {
    return {
      totalEligible: 0,
      'needs-me': 0,
      working: 0,
      waiting: 0,
      steps: 0,
      closed: 0,
    };
  }

  function countBucket(stats, bucket) {
    stats.totalEligible++;
    if (bucket in stats) {
      stats[bucket]++;
    }
  }

  function isEligibleTopLevelSummary(cascadeId, summary) {
    if (!summary) return false;
    const meta = summary.trajectoryMetadata;
    if (meta?.parentConversationId || meta?.isBattleModeFork) return false;
    if (summary.annotations?.archived) return false;
    if (meta?.sourceMetadata?.hideFromConversationList && !managedHiddenIds.has(cascadeId)) {
      return false;
    }
    return true;
  }

  function classifySummaries(summariesMap, allowedBuckets) {
    chainPredecessorIds = computeChainPredecessorIds(summariesMap, chainIndexCache);
    const hiddenIds = new Set();
    const perProjectHidden = new Map();
    const statsSummary = emptyBreakdown();

    for (const [cascadeId, summary] of Object.entries(summariesMap || {})) {
      if (!isEligibleTopLevelSummary(cascadeId, summary)) continue;
      const bucket = classifyChatBucket(summary, '', cascadeId);
      countBucket(statsSummary, bucket);
      if (allowedBuckets.has(bucket)) continue;
      hiddenIds.add(cascadeId);
      const projectId = summary.trajectoryMetadata?.projectId || '__standalone__';
      perProjectHidden.set(projectId, (perProjectHidden.get(projectId) || 0) + 1);
    }

    return { hiddenIds, perProjectHidden, statsSummary };
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

  function hookReduxStore(store) {
    if (reduxStore === store) return;
    reduxStore = store;

    // Intercept incoming stream batches so filtered conversations stay hidden
    // without flickering into the sidebar index on server stream ticks
    const origDispatch = store.dispatch;
    store.dispatch = function patchedDispatch(action) {
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
        const currentSummaries = {
          ...(store.getState()?.trajectorySummaries?.summaries || {}),
        };
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
        const result = origDispatch.call(this, {
          ...action,
          payload: {
            ...action.payload,
            updates: rewrittenUpdates,
          },
        });
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

    const summariesMap = store.getState()?.trajectorySummaries?.summaries;
    if (!summariesMap) return false;

    const { hiddenIds, perProjectHidden, statsSummary } = classifySummaries(
      summariesMap,
      visibleBuckets
    );
    totalHiddenCount = hiddenIds.size;
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
      html[data-jetski-hide-closed="true"] div[data-sidebar-row-id][data-jetski-closed-row="true"]:not([data-sidebar-section-id="pinned"]) {
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
        display: flex;
        align-items: center;
        gap: 4px;
        padding: 4px 10px 6px 10px;
        width: 100%;
        box-sizing: border-box;
        flex-wrap: nowrap;
      }
      .${BUTTON_CLASS} {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        flex: 1 1 0;
        min-width: 0;
        gap: 3px;
        height: 22px;
        padding: 0 5px;
        border-radius: 6px;
        border: 1px solid transparent;
        font-family: inherit;
        font-size: 11px;
        font-weight: 500;
        line-height: 1;
        white-space: nowrap;
        cursor: pointer;
        user-select: none;
        transition: background-color 120ms ease, color 120ms ease, border-color 120ms ease, opacity 120ms ease;
      }
      .${BUTTON_CLASS} > * {
        pointer-events: none;
      }
      .${BUTTON_CLASS}[data-active="false"] {
        background: rgba(128, 134, 139, 0.08);
        border-color: rgba(128, 134, 139, 0.20);
        color: var(--muted-foreground, #9aa0a6);
        opacity: 0.68;
      }
      .${BUTTON_CLASS}[data-active="false"]:hover {
        background: var(--sidebar-secondary, rgba(128, 134, 139, 0.18));
        color: var(--foreground, #e8eaed);
        opacity: 0.95;
      }
      .${BUTTON_CLASS}[data-active="true"] {
        background: color-mix(in srgb, var(--primary, #8ab4f8) 18%, transparent);
        border-color: color-mix(in srgb, var(--primary, #8ab4f8) 38%, transparent);
        color: var(--primary, #8ab4f8);
        opacity: 1;
      }
      .${BUTTON_CLASS}[data-active="true"]:hover {
        background: color-mix(in srgb, var(--primary, #8ab4f8) 26%, transparent);
      }
      .${BUTTON_CLASS} .jetski-bucket-icon {
        font-size: 11px;
        line-height: 1;
      }
      .${BUTTON_CLASS} .jetski-filter-badge {
        font-variant-numeric: tabular-nums;
        font-weight: 600;
      }
      .${TITLEBAR_BTN_CLASS},
      .${TITLEBAR_WAIT_BTN_CLASS} {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 4px;
        height: 24px;
        padding: 0 8px;
        border-radius: 6px;
        border: 1px solid rgba(128, 134, 139, 0.25);
        background: rgba(128, 134, 139, 0.10);
        color: var(--secondary-foreground, #c4c7c5);
        font-family: inherit;
        font-size: 11.5px;
        font-weight: 500;
        line-height: 1;
        white-space: nowrap;
        cursor: pointer;
        user-select: none;
        flex-shrink: 0;
        margin-right: 4px;
        transition: background-color 120ms ease, color 120ms ease, border-color 120ms ease;
      }
      .${TITLEBAR_BTN_CLASS} > *,
      .${TITLEBAR_WAIT_BTN_CLASS} > * {
        pointer-events: none;
      }
      .${TITLEBAR_BTN_CLASS}:hover,
      .${TITLEBAR_WAIT_BTN_CLASS}:hover {
        background: var(--secondary, rgba(128, 134, 139, 0.22));
        color: var(--foreground, #e8eaed);
        border-color: rgba(128, 134, 139, 0.40);
      }
      .${TITLEBAR_BTN_CLASS}[data-finalized="true"],
      .${TITLEBAR_WAIT_BTN_CLASS}[data-waiting="true"] {
        background: color-mix(in srgb, var(--primary, #8ab4f8) 16%, transparent);
        border-color: color-mix(in srgb, var(--primary, #8ab4f8) 36%, transparent);
        color: var(--primary, #8ab4f8);
      }
      .${ROW_BTN_CLASS},
      .${ROW_WAIT_BTN_CLASS} {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 24px;
        height: 24px;
        padding: 0;
        border-radius: 6px;
        border: none;
        background: transparent;
        color: var(--muted-foreground, #9aa0a6);
        font-family: inherit;
        font-size: 11px;
        font-weight: 600;
        line-height: 1;
        cursor: pointer;
        user-select: none;
        flex-shrink: 0;
        transition: background-color 120ms ease, color 120ms ease;
      }
      .${ROW_BTN_CLASS} > *,
      .${ROW_WAIT_BTN_CLASS} > * {
        pointer-events: none;
      }
      .${ROW_BTN_CLASS}:hover,
      .${ROW_WAIT_BTN_CLASS}:hover {
        background: var(--sidebar-secondary, rgba(128, 134, 139, 0.22));
        color: var(--foreground, #e8eaed);
      }
      .${ROW_BTN_CLASS}[data-finalized="true"],
      .${ROW_WAIT_BTN_CLASS}[data-waiting="true"] {
        color: var(--primary, #8ab4f8);
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
        width: 320px;
        padding: 10px 12px;
        border-radius: 8px;
        background: var(--popover, #1e1f22);
        color: var(--popover-foreground, #e8eaed);
        border: 1px solid var(--border, rgba(255, 255, 255, 0.14));
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        font-size: 11.5px;
        line-height: 1.45;
        pointer-events: none;
        opacity: 0;
        transform: translateY(-2px);
        transition: opacity 110ms ease, transform 110ms ease;
      }
      #jetski-closed-chats-filter-bubble[data-visible="true"] {
        opacity: 1;
        transform: translateY(0);
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
      #jetski-closed-chats-filter-bubble .jcfb-row {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        gap: 8px;
        padding: 2px 4px;
        border-radius: 4px;
      }
      #jetski-closed-chats-filter-bubble .jcfb-row[data-hovered="true"] {
        background: rgba(138, 180, 248, 0.14);
        font-weight: 600;
      }
      #jetski-closed-chats-filter-bubble .jcfb-val {
        font-variant-numeric: tabular-nums;
        font-weight: 600;
        white-space: nowrap;
      }
      #jetski-closed-chats-filter-bubble .jcfb-total-row {
        margin-top: 3px;
        padding-top: 4px;
        border-top: 1px dashed rgba(128, 134, 139, 0.25);
        font-weight: 600;
      }
      #jetski-closed-chats-filter-bubble .jcfb-controls {
        margin-top: 7px;
        padding-top: 6px;
        border-top: 1px solid rgba(128, 134, 139, 0.22);
        font-size: 11px;
        color: var(--muted-foreground, #b0b5bc);
      }
      #jetski-closed-chats-filter-bubble .jcfb-controls div + div {
        margin-top: 3px;
      }
      #jetski-closed-chats-filter-bubble kbd {
        display: inline-block;
        padding: 0 4px;
        border-radius: 4px;
        background: rgba(128, 134, 139, 0.2);
        color: var(--popover-foreground, #e8eaed);
        font-family: inherit;
        font-size: 10.5px;
        font-weight: 600;
      }
    `;
    if (style.textContent !== nextCss) {
      style.textContent = nextCss;
    }
  }

  function syncDomRowsAndPlaceholders(reduxActive) {
    const anyHidden = visibleBuckets.size < BUCKET_ORDER.length;
    if (anyHidden) {
      if (document.documentElement.getAttribute('data-jetski-hide-closed') !== 'true') {
        document.documentElement.setAttribute('data-jetski-hide-closed', 'true');
      }
    } else if (document.documentElement.hasAttribute('data-jetski-hide-closed')) {
      document.documentElement.removeAttribute('data-jetski-hide-closed');
    }

    const store = findReduxStore();
    const summariesMap = store?.getState()?.trajectorySummaries?.summaries || {};

    // Fallback DOM row tagging for rows rendered before Redux hook or outside standard slices
    const rowEls = document.querySelectorAll('div[data-sidebar-row-id]');
    const domStats = emptyBreakdown();
    let domHiddenCount = 0;
    for (const rowEl of rowEls) {
      const rowId = rowEl.getAttribute('data-sidebar-row-id') || '';
      // Read the raw title so a temporary ⏳ display swap never alters bucket classification
      const title = getRawDomTitle(rowEl);
      const bucket = classifyChatBucket(summariesMap[rowId], title, rowId);
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
    const stateTag = isShown ? 'SHOWN' : 'HIDDEN';
    const bucketCount = breakdownStats[bucketKey] || 0;
    const clickVerb = isShown ? 'hide' : 'show';

    const totalVisible = Math.max(0, breakdownStats.totalEligible - totalHiddenCount);

    const rowsHtml = BUCKET_ORDER.map((k) => {
      const bDef = BUCKET_DEFS[k];
      const bShown = visibleBuckets.has(k);
      const bCount = breakdownStats[k] || 0;
      const bStatus = bShown ? 'shown' : 'hidden';
      const hoveredAttr = k === bucketKey ? 'true' : 'false';
      return `
        <div class="jcfb-row" data-hovered="${hoveredAttr}">
          <span>${bDef.title}:</span>
          <span class="jcfb-val">${bCount} (${bStatus})</span>
        </div>
      `;
    }).join('');

    return `
      <div class="jcfb-header">
        <span class="jcfb-header-left">
          <span class="jcfb-dot" style="background:${dotColor}"></span>
          <span>${def.title}</span>
        </span>
        <span>[${stateTag} · ${bucketCount}]</span>
      </div>
      <div class="jcfb-desc">
        ${def.description}<br/>
        <b>Click</b> to ${clickVerb} ${def.shortLabel} chats.
      </div>
      <div class="jcfb-section-title">All 5 chat states (independent filters)</div>
      ${rowsHtml}
      <div class="jcfb-row jcfb-total-row">
        <span>Showing in sidebar:</span>
        <span class="jcfb-val">${totalVisible} of ${breakdownStats.totalEligible} (${totalHiddenCount} hidden)</span>
      </div>
      <div class="jcfb-controls">
        <div><kbd>Left-click</kbd> — toggle this state ON / OFF (combine filters)</div>
        <div><kbd>Right-click</kbd> / <kbd>Shift+click</kbd> — show ONLY this state (or all 5 if solo)</div>
      </div>
    `;
  }

  function ensureBubbleElement() {
    let bubble = document.getElementById('jetski-closed-chats-filter-bubble');
    if (!bubble && document.body) {
      bubble = document.createElement('div');
      bubble.id = 'jetski-closed-chats-filter-bubble';
      bubble.setAttribute('data-visible', 'false');
      document.body.appendChild(bubble);
    }
    return bubble;
  }

  function showBubbleForButton(btn, bucketKey) {
    activeHoverBtn = btn;
    activeHoverBucket = bucketKey || btn?.getAttribute('data-bucket') || 'needs-me';
    // Clear any active roadmap badge hover state since the shared bubble now renders bucket stats
    activeHoverRoadmapBadge = null;
    activeHoverRoadmapProjectId = null;
    const bubble = ensureBubbleElement();
    if (!bubble || !btn) return;
    const nextBubbleHtml = buildBubbleHtml(activeHoverBucket);
    if (bubble.innerHTML !== nextBubbleHtml) {
      bubble.innerHTML = nextBubbleHtml;
    }
    if (bubble.getAttribute('data-visible') !== 'true') {
      bubble.setAttribute('data-visible', 'true');
    }

    const rect = btn.getBoundingClientRect();
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

  function hideBubble() {
    activeHoverBtn = null;
    activeHoverBucket = null;
    // Reset both bucket and roadmap hover owners when hiding the shared bubble
    activeHoverRoadmapBadge = null;
    activeHoverRoadmapProjectId = null;
    const bubble = document.getElementById('jetski-closed-chats-filter-bubble');
    if (bubble && bubble.getAttribute('data-visible') !== 'false') {
      bubble.setAttribute('data-visible', 'false');
    }
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
    const bubble = ensureBubbleElement();
    if (!bubble) return;
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

    const nextHtml =
      `<span class="jetski-bucket-icon">${def.icon}</span>` +
      `<span class="jetski-filter-badge">${count}</span>`;
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

  // Injects the 5-button filter bar directly below the sidebar header row
  function ensureFilterBar() {
    const displayOptionsBtns = document.querySelectorAll(
      'button[aria-label="Display Options"]'
    );
    for (const displayOptionsBtn of displayOptionsBtns) {
      const actionRow = displayOptionsBtn.parentElement;
      if (!actionRow) continue;
      const headerHost = actionRow.parentElement || actionRow;

      let bar = headerHost.querySelector(`.${BAR_CLASS}`);
      if (!bar) {
        bar = document.createElement('div');
        bar.className = BAR_CLASS;
        bar.setAttribute('data-testid', 'jetski-attention-filter-bar');

        for (const bucketKey of BUCKET_ORDER) {
          bar.appendChild(createBucketButton(bucketKey));
        }

        // Insert right after actionRow when headerHost is actionRow's parent
        if (headerHost !== actionRow) {
          const idx = Array.isArray(headerHost.children)
            ? headerHost.children.indexOf(actionRow)
            : -1;
          const nextSib =
            actionRow.nextSibling ||
            (idx >= 0 && idx + 1 < headerHost.children.length
              ? headerHost.children[idx + 1]
              : null);
          if (nextSib) {
            headerHost.insertBefore(bar, nextSib);
          } else {
            headerHost.appendChild(bar);
          }
        } else {
          headerHost.insertBefore(bar, displayOptionsBtn);
        }
      }

      for (const bucketKey of BUCKET_ORDER) {
        const btn = bar.querySelector(`button[data-bucket="${bucketKey}"]`);
        updateBucketButtonUI(btn, bucketKey);
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

  // Intercepts pointerdown/mousedown/click with a 300ms debounce so parent row links do not navigate
  function bindActionBtnHandlers(btn, onTrigger) {
    let lastPressTs = 0;
    const triggerOnce = (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (Date.now() - lastPressTs > 300) {
        lastPressTs = Date.now();
        onTrigger();
      }
    };
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

  // Syncs the titlebar wait-toggle button for open leaf chats (⦿ <-> ⏳)
  function updateTitlebarWaitButtonUI(btn, cascadeId) {
    if (!btn || !cascadeId) return;
    btn.setAttribute('data-cascade-id', cascadeId);
    const curTitle = getTitleForCascadeId(cascadeId, '');
    // Hide the Wait toggle on closed chats and handed-off chain predecessors
    const isLeafOpen =
      !isFinalizedTitle(curTitle) && !isChainPredecessor(cascadeId, curTitle);
    btn.style.display = isLeafOpen ? '' : 'none';
    const waiting = isWaitingTitle(curTitle);
    const waitStr = waiting ? 'true' : 'false';
    if (btn.getAttribute('data-waiting') !== waitStr) {
      btn.setAttribute('data-waiting', waitStr);
    }
    const label = waiting
      ? 'Move chat to 🚨 Needs Me (⦿)'
      : 'Move chat to ⏳ Waiting (Cron / External)';
    // Sync DOM attributes and innerHTML only on change to avoid re-triggering MutationObserver
    if (btn.getAttribute('aria-label') !== label) {
      btn.setAttribute('aria-label', label);
    }
    if (btn.getAttribute('title') !== label) {
      btn.setAttribute('title', label);
    }
    const nextHtml = waiting
      ? '<span>🚨</span><span>Needs Me</span>'
      : '<span>⏳</span><span>Wait</span>';
    if (btn.innerHTML !== nextHtml) {
      btn.innerHTML = nextHtml;
    }
  }

  // Syncs the titlebar pill button label and finalized attribute with the current chat title
  function updateTitlebarCloseButtonUI(btn, cascadeId) {
    if (!btn || !cascadeId) return;
    btn.setAttribute('data-cascade-id', cascadeId);
    const curTitle = getTitleForCascadeId(cascadeId, '');
    const finalized = isFinalizedTitle(curTitle);
    const finStr = finalized ? 'true' : 'false';
    if (btn.getAttribute('data-finalized') !== finStr) {
      btn.setAttribute('data-finalized', finStr);
    }
    const label = finalized ? 'Reopen chat' : 'Close chat';
    if (btn.getAttribute('aria-label') !== label) {
      btn.setAttribute('aria-label', label);
    }
    if (btn.getAttribute('title') !== label) {
      btn.setAttribute('title', label);
    }
    const nextHtml = finalized
      ? '<span>⦿</span><span>Reopen</span>'
      : '<span>«»</span><span>Close</span>';
    if (btn.innerHTML !== nextHtml) {
      btn.innerHTML = nextHtml;
    }
  }

  // Injects or updates the Wait and Close/Reopen pill buttons before ⋮ in each conversation titlebar
  function ensureTitlebarCloseButtons() {
    const moreActionsBtns = document.querySelectorAll(
      '[data-testid="titlebar-more-actions"]'
    );
    for (const moreBtn of moreActionsBtns) {
      const container = moreBtn.parentElement;
      if (!container) continue;
      const cascadeId = findCascadeIdFromElement(moreBtn);
      if (!cascadeId) continue;

      let waitBtn = container.querySelector(`.${TITLEBAR_WAIT_BTN_CLASS}`);
      if (!waitBtn) {
        waitBtn = document.createElement('button');
        waitBtn.className = TITLEBAR_WAIT_BTN_CLASS;
        waitBtn.type = 'button';
        waitBtn.setAttribute('data-testid', 'jetski-titlebar-wait-btn');
        bindActionBtnHandlers(waitBtn, () => {
          const cid =
            waitBtn.getAttribute('data-cascade-id') || findCascadeIdFromElement(moreBtn);
          if (cid) {
            void toggleConversationWaiting(cid, '');
          }
        });
        container.insertBefore(waitBtn, moreBtn);
      }
      updateTitlebarWaitButtonUI(waitBtn, cascadeId);

      let btn = container.querySelector(`.${TITLEBAR_BTN_CLASS}`);
      if (!btn) {
        btn = document.createElement('button');
        btn.className = TITLEBAR_BTN_CLASS;
        btn.type = 'button';
        btn.setAttribute('data-testid', 'jetski-titlebar-close-reopen-btn');
        bindActionBtnHandlers(btn, () => {
          const cid =
            btn.getAttribute('data-cascade-id') || findCascadeIdFromElement(moreBtn);
          if (cid) {
            void toggleConversationLifecycle(cid, '');
          }
        });
        container.insertBefore(btn, moreBtn);
      }
      updateTitlebarCloseButtonUI(btn, cascadeId);
    }
  }

  // Syncs the compact sidebar row wait-toggle button for open leaf chats (⦿ <-> ⏳)
  function updateRowWaitButtonUI(btn, cascadeId, fallbackDomTitle) {
    if (!btn || !cascadeId) return;
    btn.setAttribute('data-cascade-id', cascadeId);
    const curTitle = getTitleForCascadeId(cascadeId, fallbackDomTitle);
    // Only open leaf chats can be moved between Needs Me and Waiting
    const isLeafOpen =
      !isFinalizedTitle(curTitle) && !isChainPredecessor(cascadeId, curTitle);
    btn.style.display = isLeafOpen ? '' : 'none';
    const waiting = isWaitingTitle(curTitle);
    const waitStr = waiting ? 'true' : 'false';
    if (btn.getAttribute('data-waiting') !== waitStr) {
      btn.setAttribute('data-waiting', waitStr);
    }
    const label = waiting
      ? 'Move chat to 🚨 Needs Me (⦿)'
      : 'Move chat to ⏳ Waiting (Cron / External)';
    // Guard DOM mutations so the sidebar observer does not loop on unchanged buttons
    if (btn.getAttribute('aria-label') !== label) {
      btn.setAttribute('aria-label', label);
    }
    if (btn.getAttribute('title') !== label) {
      btn.setAttribute('title', label);
    }
    const nextHtml = waiting ? '<span>🚨</span>' : '<span>⏳</span>';
    if (btn.innerHTML !== nextHtml) {
      btn.innerHTML = nextHtml;
    }
  }

  // Syncs the compact sidebar row action button icon and tooltip with the row's current title
  function updateRowCloseButtonUI(btn, cascadeId, fallbackDomTitle) {
    if (!btn || !cascadeId) return;
    btn.setAttribute('data-cascade-id', cascadeId);
    const curTitle = getTitleForCascadeId(cascadeId, fallbackDomTitle);
    const finalized = isFinalizedTitle(curTitle);
    const finStr = finalized ? 'true' : 'false';
    if (btn.getAttribute('data-finalized') !== finStr) {
      btn.setAttribute('data-finalized', finStr);
    }
    const label = finalized ? 'Reopen chat' : 'Close chat';
    if (btn.getAttribute('aria-label') !== label) {
      btn.setAttribute('aria-label', label);
    }
    if (btn.getAttribute('title') !== label) {
      btn.setAttribute('title', label);
    }
    const nextHtml = finalized ? '<span>⦿</span>' : '<span>«»</span>';
    if (btn.innerHTML !== nextHtml) {
      btn.innerHTML = nextHtml;
    }
  }

  // Visually swaps ⦿ -> ⏳ in the row title and renders a right-side ⏳ badge while an open
  // leaf chat is idle waiting on a background task or subagent, without touching .pbtxt
  function syncRowBackgroundWaitUI(rowEl, cascadeId, summary, rawDomTitle) {
    // Skip detached or unidentified row nodes
    if (!rowEl || !cascadeId) return;
    const curTitle =
      summary?.annotations?.title || summary?.summary || rawDomTitle || '';
    const waitingBg = isWaitingOnBackgroundWork(summary, curTitle, cascadeId);
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
      let badge = rowEl.querySelector(`.${ROW_BG_WAIT_INDICATOR_CLASS}`);
      // Mount the right-side ⏳ badge once inside the row status cluster
      if (!badge) {
        badge = document.createElement('span');
        badge.className = ROW_BG_WAIT_INDICATOR_CLASS;
        badge.setAttribute('data-testid', 'jetski-row-bg-wait-indicator');
        badge.setAttribute('title', 'Waiting on background task or subagent');
        badge.setAttribute('aria-label', 'Waiting on background task or subagent');
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

  // Injects or updates the compact Wait and Close/Reopen buttons inside each sidebar row hover bar
  function ensureSidebarRowCloseButtons() {
    // Read Redux summaries so each row can sync its live background-wait state
    const store = findReduxStore();
    const summariesMap = store?.getState()?.trajectorySummaries?.summaries || {};
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
        fallbackDomTitle
      );

      const anchorBtn =
        rowEl.querySelector('[data-testid="conversation-pin-button"]') ||
        rowEl.querySelector('[data-testid="conversation-archive-button"]');
      if (!anchorBtn || !anchorBtn.parentElement) continue;
      const hoverBar = anchorBtn.parentElement;

      let waitBtn = hoverBar.querySelector(`.${ROW_WAIT_BTN_CLASS}`);
      if (!waitBtn) {
        waitBtn = document.createElement('button');
        waitBtn.className = ROW_WAIT_BTN_CLASS;
        waitBtn.type = 'button';
        waitBtn.setAttribute('data-testid', 'jetski-row-wait-btn');
        bindActionBtnHandlers(waitBtn, () => {
          const cid = waitBtn.getAttribute('data-cascade-id') || cascadeId;
          // Read the raw title so clicking Wait uses the real marker rather than the DOM swap
          const domTitle = getRawDomTitle(rowEl);
          if (cid) {
            void toggleConversationWaiting(cid, domTitle);
          }
        });
        hoverBar.insertBefore(waitBtn, anchorBtn);
      }
      updateRowWaitButtonUI(waitBtn, cascadeId, fallbackDomTitle);

      let btn = hoverBar.querySelector(`.${ROW_BTN_CLASS}`);
      if (!btn) {
        btn = document.createElement('button');
        btn.className = ROW_BTN_CLASS;
        btn.type = 'button';
        btn.setAttribute('data-testid', 'jetski-row-close-reopen-btn');
        bindActionBtnHandlers(btn, () => {
          const cid = btn.getAttribute('data-cascade-id') || cascadeId;
          // Read the raw title so closing/reopening preserves the underlying topic text
          const domTitle = getRawDomTitle(rowEl);
          if (cid) {
            void toggleConversationLifecycle(cid, domTitle);
          }
        });
        hoverBar.insertBefore(btn, anchorBtn);
      }
      updateRowCloseButtonUI(btn, cascadeId, fallbackDomTitle);
    }
  }

  function runFullSync() {
    ensureChainIndexLoaded();
    // Refresh the cached roadmap index and update project header badges on every sync pass
    ensureRoadmapIndexLoaded();
    ensureStylesInjected();
    const reduxActive = syncReduxFilter();
    syncDomRowsAndPlaceholders(reduxActive);
    ensureFilterBar();
    // Render or remove compact open-roadmap badges after project card DOM rows are synced
    ensureProjectRoadmapBadges();
    ensureTitlebarCloseButtons();
    ensureSidebarRowCloseButtons();
  }

  function scheduleSync() {
    if (rafScheduled) return;
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
        target.className === BAR_CLASS ||
        target.className === BUTTON_CLASS ||
        target.className === TITLEBAR_BTN_CLASS ||
        target.className === TITLEBAR_WAIT_BTN_CLASS ||
        target.className === ROW_BTN_CLASS ||
        target.className === ROW_WAIT_BTN_CLASS ||
        target.className === ROW_BG_WAIT_INDICATOR_CLASS ||
        target.className === PROJECT_ROADMAP_BADGE_CLASS ||
        (typeof target.closest === 'function' &&
          (target.closest('#jetski-closed-chats-filter-bubble') ||
            target.closest(`.${BAR_CLASS}`) ||
            target.closest(`.${BUTTON_CLASS}`) ||
            target.closest(`.${TITLEBAR_BTN_CLASS}`) ||
            target.closest(`.${TITLEBAR_WAIT_BTN_CLASS}`) ||
            target.closest(`.${ROW_BTN_CLASS}`) ||
            target.closest(`.${ROW_WAIT_BTN_CLASS}`) ||
            target.closest(`.${ROW_BG_WAIT_INDICATOR_CLASS}`) ||
            target.closest(`.${PROJECT_ROADMAP_BADGE_CLASS}`)))
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
    observer.disconnect();
    // Remove all injected filter buttons and background-wait badges on reload
    const cleanupSelector = [
      `.${BAR_CLASS}`,
      `.${BUTTON_CLASS}`,
      `.${TITLEBAR_BTN_CLASS}`,
      `.${TITLEBAR_WAIT_BTN_CLASS}`,
      `.${ROW_BTN_CLASS}`,
      `.${ROW_WAIT_BTN_CLASS}`,
      `.${ROW_BG_WAIT_INDICATOR_CLASS}`,
      `.${PROJECT_ROADMAP_BADGE_CLASS}`,
    ].join(', ');
    for (const el of document.querySelectorAll(cleanupSelector)) {
      el.remove();
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
  };

  runFullSync();
})();
