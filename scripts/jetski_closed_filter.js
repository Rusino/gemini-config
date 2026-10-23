(() => {
  if (typeof window.__jetskiClosedChatsFilterCleanup === 'function') {
    try {
      window.__jetskiClosedChatsFilterCleanup();
    } catch {
      // Ignore cleanup errors from older versions
    }
  }
  window.__jetskiClosedChatsFilterInstalled = true;

  const STORAGE_ENABLED_KEY = 'jetski_closed_filter_enabled';
  const STORAGE_SCOPE_KEY = 'jetski_closed_filter_scope';
  const BUTTON_CLASS = 'jetski-closed-chats-filter-btn';
  const TITLEBAR_BTN_CLASS = 'jetski-titlebar-close-reopen-btn';
  const ROW_BTN_CLASS = 'jetski-row-close-reopen-btn';
  const STYLE_ID = 'jetski-closed-chats-filter-style';

  // Lifecycle titles follow "[HH:MM] <marker> <Topic>" where marker encodes chain state
  // The time prefix and the space after the marker are mandatory so a plain title opening
  // with a guillemet quote is never read as the finalized « marker
  const TITLE_MARKER_RE = /^\s*\[\d{1,2}:\d{2}\]\s*(‹✓›|«»|[✓⦿▸«»])(?:\s|$)/u;
  const TIME_PREFIX_EXTRACT_RE = /^\s*\[(\d{1,2}:\d{2})\]/;
  const CONT_WORD = '(?:\\u043f\\u0440\\u043e\\u0434\\u043e\\u043b\\u0436\\u0435\\u043d\\u0438\\u0435|continue)';
  const TITLE_CLEANUP_RE = new RegExp(
    `^(?:\\s*\\[\\d{1,2}:\\d{2}(?:\\s+${CONT_WORD})?\\]\\s*(?:‹✓›|«»|[▸✓⦿«»]|\\s*)*)|^(?:${CONT_WORD}(?:\\s+\\d{1,2}:\\d{2})?\\s*:\\s*)+`,
    'iu'
  );
  const TRAILING_CLEANUP_RE = new RegExp(
    `\\s*\\(${CONT_WORD}\\s+(?:\\d{4}-\\d{2}-\\d{2}\\s+)?\\d{1,2}:\\d{2}\\)\\s*$`,
    'iu'
  );
  // chat_lifecycle.py finalize rewrites every chat of a chain to «, ‹✓›, » (or «» for a
  // single-chat task), so each title alone tells whether its task is closed
  const FINALIZED_MARKERS = new Set(['«', '‹✓›', '»', '«»']);
  // Open chains read ▸ root, ✓ handed-off steps, ⦿ current chat
  const OPEN_CHAIN_ROOT_MARKER = '▸';
  const OPEN_CHAIN_STEP_MARKER = '✓';
  const OPEN_ACTIVE_MARKER = '⦿';
  const CLOSED_START_MARKER = '«';
  const CLOSED_STEP_MARKER = '‹✓›';
  const CLOSED_END_MARKER = '»';
  const CLOSED_SINGLE_MARKER = '«»';

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

  let filterEnabled = safeGetStorage(STORAGE_ENABLED_KEY) !== 'false';
  let filterScope =
    safeGetStorage(STORAGE_SCOPE_KEY) === 'active-only'
      ? 'active-only'
      : 'closed';

  // Tracks conversations whose hideFromConversationList flag we flipped in Redux
  // so we never unhide conversations that were natively hidden by their source
  const managedHiddenIds = new Map();
  // Tracks cascadeIds where auxPaneV2/setOverviewPanelOpen was dispatched so stores
  // without the auxPaneV2 reducer never re-dispatch in a loop
  const overviewCloseDispatchedIds = new Set();
  let hiddenCountsByProject = new Map();
  let totalClosedCount = 0;
  let breakdownStats = emptyBreakdown();
  let reduxStore = null;
  let isInternalDispatch = false;
  let rafScheduled = false;
  let activeHoverBtn = null;
  let chainIndexCache = null;
  let chainIndexFetchedAt = 0;

  function parseTitleMarker(title) {
    if (typeof title !== 'string') return null;
    const match = TITLE_MARKER_RE.exec(title);
    return match ? match[1] : null;
  }

  function isFinalizedTitle(title) {
    const marker = parseTitleMarker(title);
    return Boolean(marker && FINALIZED_MARKERS.has(marker));
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

  // 'closed' hides only finalized chains («, ‹✓›, », «») while keeping all steps (▸, ✓, ⦿)
  // of open chains visible; 'active-only' also hides open chain roots (▸) and steps (✓)
  function isHiddenMarker(marker, scope) {
    if (FINALIZED_MARKERS.has(marker)) return true;
    return (
      scope === 'active-only' &&
      (marker === OPEN_CHAIN_ROOT_MARKER || marker === OPEN_CHAIN_STEP_MARKER)
    );
  }

  function emptyBreakdown() {
    return { totalEligible: 0, active: 0, openRoots: 0, steps: 0, finalized: 0 };
  }

  function countMarker(stats, marker) {
    stats.totalEligible++;
    if (FINALIZED_MARKERS.has(marker)) stats.finalized++;
    else if (marker === OPEN_CHAIN_STEP_MARKER) stats.steps++;
    else if (marker === OPEN_CHAIN_ROOT_MARKER) stats.openRoots++;
    else stats.active++;
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

  function classifySummaries(summariesMap, scope) {
    const closedIds = new Set();
    const perProjectHidden = new Map();
    const statsSummary = emptyBreakdown();

    for (const [cascadeId, summary] of Object.entries(summariesMap || {})) {
      if (!isEligibleTopLevelSummary(cascadeId, summary)) continue;
      const marker = parseTitleMarker(summary.summary);
      countMarker(statsSummary, marker);
      if (!isHiddenMarker(marker, scope)) continue;
      closedIds.add(cascadeId);
      const projectId = summary.trajectoryMetadata?.projectId || '__standalone__';
      perProjectHidden.set(projectId, (perProjectHidden.get(projectId) || 0) + 1);
    }

    return { closedIds, perProjectHidden, statsSummary };
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

    // Intercept incoming stream batches so closed conversations stay hidden
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
        const { closedIds } = classifySummaries(currentSummaries, filterScope);
        const rewrittenUpdates = action.payload.updates.map(([id, incomingSummary]) => {
          if (!incomingSummary) return [id, incomingSummary];
          if (filterEnabled && closedIds.has(id)) {
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

    const { closedIds, perProjectHidden, statsSummary } = classifySummaries(
      summariesMap,
      filterScope
    );
    totalClosedCount = closedIds.size;
    hiddenCountsByProject = perProjectHidden;
    if (statsSummary) {
      breakdownStats = statsSummary;
    }

    const updates = [];

    if (filterEnabled) {
      for (const cascadeId of closedIds) {
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
        if (!closedIds.has(cascadeId)) {
          managedHiddenIds.delete(cascadeId);
          const summary = summariesMap[cascadeId];
          if (summary?.trajectoryMetadata?.sourceMetadata?.hideFromConversationList) {
            updates.push([cascadeId, cloneSummaryWithHideFlag(summary, false)]);
          }
        }
      }
    } else if (managedHiddenIds.size > 0) {
      for (const [cascadeId] of Array.from(managedHiddenIds.entries())) {
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
      .${BUTTON_CLASS} {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 4px;
        height: 22px;
        padding: 0 7px;
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
      .${BUTTON_CLASS} svg {
        width: 12px;
        height: 12px;
        flex-shrink: 0;
      }
      .${BUTTON_CLASS}[data-active="false"] {
        background: rgba(128, 134, 139, 0.10);
        border-color: rgba(128, 134, 139, 0.22);
        color: var(--muted-foreground, #9aa0a6);
        opacity: 0.9;
      }
      .${BUTTON_CLASS}[data-active="false"]:hover {
        background: var(--sidebar-secondary, rgba(128, 134, 139, 0.20));
        color: var(--foreground, #e8eaed);
        opacity: 1;
      }
      .${BUTTON_CLASS}[data-active="true"] {
        background: color-mix(in srgb, var(--primary, #8ab4f8) 16%, transparent);
        border-color: color-mix(in srgb, var(--primary, #8ab4f8) 34%, transparent);
        color: var(--primary, #8ab4f8);
        opacity: 1;
      }
      .${BUTTON_CLASS}[data-active="true"]:hover {
        background: color-mix(in srgb, var(--primary, #8ab4f8) 24%, transparent);
      }
      .${BUTTON_CLASS} .jetski-filter-badge {
        font-variant-numeric: tabular-nums;
        font-weight: 600;
      }
      .${TITLEBAR_BTN_CLASS} {
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
        transition: background-color 120ms ease, color 120ms ease, border-color 120ms ease;
      }
      .${TITLEBAR_BTN_CLASS} > * {
        pointer-events: none;
      }
      .${TITLEBAR_BTN_CLASS}:hover {
        background: var(--secondary, rgba(128, 134, 139, 0.22));
        color: var(--foreground, #e8eaed);
        border-color: rgba(128, 134, 139, 0.40);
      }
      .${TITLEBAR_BTN_CLASS}[data-finalized="true"] {
        background: color-mix(in srgb, var(--primary, #8ab4f8) 16%, transparent);
        border-color: color-mix(in srgb, var(--primary, #8ab4f8) 36%, transparent);
        color: var(--primary, #8ab4f8);
      }
      .${TITLEBAR_BTN_CLASS}[data-finalized="true"]:hover {
        background: color-mix(in srgb, var(--primary, #8ab4f8) 24%, transparent);
      }
      .${ROW_BTN_CLASS} {
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
      .${ROW_BTN_CLASS} > * {
        pointer-events: none;
      }
      .${ROW_BTN_CLASS}:hover {
        background: var(--sidebar-secondary, rgba(128, 134, 139, 0.22));
        color: var(--foreground, #e8eaed);
      }
      .${ROW_BTN_CLASS}[data-finalized="true"] {
        color: var(--primary, #8ab4f8);
      }
      .${ROW_BTN_CLASS}[data-finalized="true"]:hover {
        background: color-mix(in srgb, var(--primary, #8ab4f8) 20%, transparent);
      }
      #jetski-closed-chats-filter-bubble {
        position: fixed;
        z-index: 2147483647;
        width: 310px;
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
        gap: 6px;
        font-weight: 600;
        font-size: 12px;
        margin-bottom: 6px;
        padding-bottom: 6px;
        border-bottom: 1px solid rgba(128, 134, 139, 0.22);
      }
      #jetski-closed-chats-filter-bubble .jcfb-dot {
        width: 7px;
        height: 7px;
        border-radius: 999px;
        flex-shrink: 0;
      }
      #jetski-closed-chats-filter-bubble .jcfb-section-title {
        font-size: 10.5px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--muted-foreground, #9aa0a6);
        margin: 6px 0 3px 0;
      }
      #jetski-closed-chats-filter-bubble .jcfb-row {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        gap: 8px;
        padding: 1.5px 0;
      }
      #jetski-closed-chats-filter-bubble .jcfb-val {
        font-variant-numeric: tabular-nums;
        font-weight: 600;
        white-space: nowrap;
      }
      #jetski-closed-chats-filter-bubble .jcfb-total-row {
        margin-top: 3px;
        padding-top: 3px;
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
    if (filterEnabled) {
      if (document.documentElement.getAttribute('data-jetski-hide-closed') !== 'true') {
        document.documentElement.setAttribute('data-jetski-hide-closed', 'true');
      }
    } else if (document.documentElement.hasAttribute('data-jetski-hide-closed')) {
      document.documentElement.removeAttribute('data-jetski-hide-closed');
    }

    // Fallback DOM row tagging for rows rendered before Redux hook or outside standard slices
    const rowEls = document.querySelectorAll('div[data-sidebar-row-id]');
    const domStats = emptyBreakdown();
    let domClosedCount = 0;
    for (const rowEl of rowEls) {
      const linkEl = rowEl.querySelector('a[aria-label]');
      const spanEl = rowEl.querySelector('span.truncate');
      const title = linkEl?.getAttribute('aria-label') || spanEl?.textContent || '';
      const marker = parseTitleMarker(title);
      countMarker(domStats, marker);

      if (isHiddenMarker(marker, filterScope)) {
        domClosedCount++;
        if (rowEl.getAttribute('data-jetski-closed-row') !== 'true') {
          rowEl.setAttribute('data-jetski-closed-row', 'true');
        }
      } else if (rowEl.hasAttribute('data-jetski-closed-row')) {
        rowEl.removeAttribute('data-jetski-closed-row');
      }
    }

    if (!reduxActive) {
      totalClosedCount = domClosedCount;
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
      if (filterEnabled && hiddenInProject > 0) {
        const nextText = `No active chats (${hiddenInProject} hidden)`;
        if (span.textContent !== nextText) {
          span.textContent = nextText;
        }
      } else if (span.textContent !== origText) {
        span.textContent = origText;
      }
    }
  }

  const EYE_OFF_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
  const EYE_ON_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';

  function buildBubbleHtml() {
    const dotColor = filterEnabled
      ? filterScope === 'active-only'
        ? '#60a5fa'
        : '#34d399'
      : '#9ca3af';

    let headerTitle = '';
    if (filterEnabled && filterScope === 'closed') {
      headerTitle = 'Filter ON: closed chats hidden';
    } else if (filterEnabled && filterScope === 'active-only') {
      headerTitle = 'Filter ON: active chats only';
    } else {
      headerTitle = 'Filter OFF: showing all chats';
    }

    const totalActionLabel = filterEnabled
      ? 'Hidden by filter (count on button):'
      : 'Would be hidden (count on button):';

    const leftClickAction = filterEnabled
      ? 'Show all chats (turn filter OFF)'
      : 'Hide filtered chats (turn filter ON)';

    const mode1Mark = filterScope === 'closed' ? '●' : '○';
    const mode2Mark = filterScope === 'active-only' ? '●' : '○';

    return `
      <div class="jcfb-header">
        <span class="jcfb-dot" style="background:${dotColor}"></span>
        <span>${headerTitle}</span>
      </div>
      <div class="jcfb-section-title">Chat count breakdown (all projects)</div>
      <div class="jcfb-row">
        <span>Active (⦿, no marker):</span>
        <span class="jcfb-val">${breakdownStats.active}</span>
      </div>
      <div class="jcfb-row">
        <span>Open chain roots (▸):</span>
        <span class="jcfb-val">${breakdownStats.openRoots}</span>
      </div>
      <div class="jcfb-row">
        <span>Handed-off steps (✓):</span>
        <span class="jcfb-val">${breakdownStats.steps}</span>
      </div>
      <div class="jcfb-row">
        <span>Finalized chains («, ‹✓›, », «»):</span>
        <span class="jcfb-val">${breakdownStats.finalized}</span>
      </div>
      <div class="jcfb-row jcfb-total-row">
        <span>${totalActionLabel}</span>
        <span class="jcfb-val">${totalClosedCount} of ${breakdownStats.totalEligible}</span>
      </div>
      <div class="jcfb-controls">
        <div><kbd>Left-click</kbd> — ${leftClickAction}</div>
        <div><kbd>Right-click</kbd> / <kbd>Shift+click</kbd> — switch mode:</div>
        <div style="padding-left:6px;margin-top:2px;">
          ${mode1Mark} <b>Closed hidden</b> (hide finalized «, ‹✓›, », «»)<br/>
          ${mode2Mark} <b>Active only</b> (also hide open chain ▸ and ✓)
        </div>
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

  function showBubbleForButton(btn) {
    activeHoverBtn = btn;
    const bubble = ensureBubbleElement();
    if (!bubble || !btn) return;
    const nextBubbleHtml = buildBubbleHtml();
    if (bubble.innerHTML !== nextBubbleHtml) {
      bubble.innerHTML = nextBubbleHtml;
    }
    if (bubble.getAttribute('data-visible') !== 'true') {
      bubble.setAttribute('data-visible', 'true');
    }

    const rect = btn.getBoundingClientRect();
    const bubbleWidth = 310;
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
    const bubble = document.getElementById('jetski-closed-chats-filter-bubble');
    if (bubble && bubble.getAttribute('data-visible') !== 'false') {
      bubble.setAttribute('data-visible', 'false');
    }
  }

  function updateButtonUI(btn) {
    if (!btn) return;
    const activeStr = filterEnabled ? 'true' : 'false';
    if (btn.getAttribute('data-active') !== activeStr) {
      btn.setAttribute('data-active', activeStr);
    }
    if (btn.getAttribute('aria-pressed') !== activeStr) {
      btn.setAttribute('aria-pressed', activeStr);
    }
    if (btn.hasAttribute('title')) {
      btn.removeAttribute('title');
    }

    // Show active (⦿) count alongside hidden/closed count across all modes without widening the pill
    const activeCount = breakdownStats.active;
    const hiddenSuffix =
      filterScope === 'active-only'
        ? `${totalClosedCount} hidden`
        : `${totalClosedCount} closed`;
    const countsText = `${activeCount} active · ${hiddenSuffix}`;
    const labelText = filterEnabled ? countsText : `All (${countsText})`;

    if (btn.getAttribute('aria-label') !== labelText) {
      btn.setAttribute('aria-label', labelText);
    }

    const iconSvg = filterEnabled ? EYE_OFF_SVG : EYE_ON_SVG;
    const nextHtml =
      iconSvg + `<span class="jetski-filter-badge">${labelText}</span>`;
    if (btn.innerHTML !== nextHtml) {
      btn.innerHTML = nextHtml;
    }

    if (activeHoverBtn === btn) {
      showBubbleForButton(btn);
    }
  }

  function executePrimaryButtonAction(btn, isShift) {
    if (isShift) {
      toggleFilterScope();
      showBubbleForButton(btn);
      return;
    }
    filterEnabled = !filterEnabled;
    safeSetStorage(STORAGE_ENABLED_KEY, String(filterEnabled));
    runFullSync();
    showBubbleForButton(btn);
  }

  function ensureToggleButton() {
    const displayOptionsBtns = document.querySelectorAll(
      'button[aria-label="Display Options"]'
    );
    for (const displayOptionsBtn of displayOptionsBtns) {
      const actionRow = displayOptionsBtn.parentElement;
      if (!actionRow) continue;

      let btn = actionRow.querySelector(`.${BUTTON_CLASS}`);
      if (!btn) {
        btn = document.createElement('button');
        btn.className = BUTTON_CLASS;
        btn.id = BUTTON_CLASS;
        btn.type = 'button';

        let lastPressTs = 0;

        btn.addEventListener('mouseenter', () => {
          showBubbleForButton(btn);
        });
        btn.addEventListener('mouseleave', () => {
          hideBubble();
        });
        btn.addEventListener('focus', () => {
          showBubbleForButton(btn);
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
            executePrimaryButtonAction(btn, e.shiftKey);
          }
        });

        btn.addEventListener('mousedown', (e) => {
          e.stopPropagation();
          if (e.button === 0 && !e.ctrlKey && !e.metaKey) {
            e.preventDefault();
            if (Date.now() - lastPressTs > 300) {
              lastPressTs = Date.now();
              executePrimaryButtonAction(btn, e.shiftKey);
            }
          }
        });

        btn.addEventListener('click', (e) => {
          e.preventDefault();
          e.stopPropagation();
          if (Date.now() - lastPressTs > 300) {
            lastPressTs = Date.now();
            executePrimaryButtonAction(btn, e.shiftKey);
          }
        });

        btn.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          e.stopPropagation();
          toggleFilterScope();
          showBubbleForButton(btn);
        });

        actionRow.insertBefore(btn, displayOptionsBtn);
      }

      updateButtonUI(btn);
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

  // Injects or updates the Close/Reopen pill button before the ⋮ button in each conversation titlebar
  function ensureTitlebarCloseButtons() {
    const moreActionsBtns = document.querySelectorAll(
      '[data-testid="titlebar-more-actions"]'
    );
    for (const moreBtn of moreActionsBtns) {
      const container = moreBtn.parentElement;
      if (!container) continue;
      const cascadeId = findCascadeIdFromElement(moreBtn);
      if (!cascadeId) continue;

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

  // Injects or updates the compact Close/Reopen button inside each sidebar row's hover action bar
  function ensureSidebarRowCloseButtons() {
    const rowEls = document.querySelectorAll(
      '[data-testid="conversation-row-sidebar"][data-cascade-id], div[data-sidebar-row-id]'
    );
    for (const rowEl of rowEls) {
      const cascadeId =
        rowEl.getAttribute('data-cascade-id') ||
        rowEl.getAttribute('data-sidebar-row-id');
      if (!cascadeId) continue;

      const anchorBtn =
        rowEl.querySelector('[data-testid="conversation-pin-button"]') ||
        rowEl.querySelector('[data-testid="conversation-archive-button"]');
      if (!anchorBtn || !anchorBtn.parentElement) continue;
      const hoverBar = anchorBtn.parentElement;

      const linkEl = rowEl.querySelector('a[aria-label]');
      const spanEl = rowEl.querySelector('span.truncate');
      const fallbackDomTitle =
        linkEl?.getAttribute('aria-label') || spanEl?.textContent || '';

      let btn = hoverBar.querySelector(`.${ROW_BTN_CLASS}`);
      if (!btn) {
        btn = document.createElement('button');
        btn.className = ROW_BTN_CLASS;
        btn.type = 'button';
        btn.setAttribute('data-testid', 'jetski-row-close-reopen-btn');
        bindActionBtnHandlers(btn, () => {
          const cid = btn.getAttribute('data-cascade-id') || cascadeId;
          const curLink = rowEl.querySelector('a[aria-label]');
          const curSpan = rowEl.querySelector('span.truncate');
          const domTitle =
            curLink?.getAttribute('aria-label') || curSpan?.textContent || '';
          if (cid) {
            void toggleConversationLifecycle(cid, domTitle);
          }
        });
        hoverBar.insertBefore(btn, anchorBtn);
      }
      updateRowCloseButtonUI(btn, cascadeId, fallbackDomTitle);
    }
  }

  function toggleFilterScope() {
    filterScope = filterScope === 'closed' ? 'active-only' : 'closed';
    safeSetStorage(STORAGE_SCOPE_KEY, filterScope);
    if (!filterEnabled) {
      filterEnabled = true;
      safeSetStorage(STORAGE_ENABLED_KEY, 'true');
    }
    runFullSync();
  }

  function runFullSync() {
    ensureStylesInjected();
    const reduxActive = syncReduxFilter();
    syncDomRowsAndPlaceholders(reduxActive);
    ensureToggleButton();
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
      if (
        target.id === 'jetski-closed-chats-filter-bubble' ||
        target.id === STYLE_ID ||
        target.id === BUTTON_CLASS ||
        target.className === TITLEBAR_BTN_CLASS ||
        target.className === ROW_BTN_CLASS ||
        (typeof target.closest === 'function' &&
          (target.closest('#jetski-closed-chats-filter-bubble') ||
            target.closest(`.${BUTTON_CLASS}`) ||
            target.closest(`.${TITLEBAR_BTN_CLASS}`) ||
            target.closest(`.${ROW_BTN_CLASS}`)))
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
    for (const el of document.querySelectorAll(
      `.${BUTTON_CLASS}, .${TITLEBAR_BTN_CLASS}, .${ROW_BTN_CLASS}`
    )) {
      el.remove();
    }
    const bubble = document.getElementById('jetski-closed-chats-filter-bubble');
    if (bubble) {
      bubble.remove();
    }
  };

  runFullSync();
})();
