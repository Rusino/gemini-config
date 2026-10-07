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
  const STYLE_ID = 'jetski-closed-chats-filter-style';

  // Lifecycle titles follow "[HH:MM] <marker> <Topic>" where marker encodes chain state
  // The time prefix and the space after the marker are mandatory so a plain title opening
  // with a guillemet quote is never read as the finalized « marker
  const TITLE_MARKER_RE = /^\s*\[\d{1,2}:\d{2}\]\s*(‹✓›|«»|[✓⦿▸«»])(?:\s|$)/u;
  // chat_lifecycle.py finalize rewrites every chat of a chain to «, ‹✓›, » (or «» for a
  // single-chat task), so each title alone tells whether its task is closed
  const FINALIZED_MARKERS = new Set(['«', '‹✓›', '»', '«»']);
  // Open chains read ▸ root, ✓ handed-off steps, ⦿ current chat
  const OPEN_CHAIN_ROOT_MARKER = '▸';
  const OPEN_CHAIN_STEP_MARKER = '✓';

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
  let hiddenCountsByProject = new Map();
  let totalClosedCount = 0;
  let breakdownStats = emptyBreakdown();
  let reduxStore = null;
  let isInternalDispatch = false;
  let rafScheduled = false;
  let activeHoverBtn = null;

  function parseTitleMarker(title) {
    if (typeof title !== 'string') return null;
    const match = TITLE_MARKER_RE.exec(title);
    return match ? match[1] : null;
  }

  // A ✓ step is already handed off, so it goes with closed chats; the ▸ root stays as the
  // entry point of a running task until 'active-only' leaves only ⦿ and unmarked chats
  function isHiddenMarker(marker, scope) {
    if (FINALIZED_MARKERS.has(marker) || marker === OPEN_CHAIN_STEP_MARKER) return true;
    return scope === 'active-only' && marker === OPEN_CHAIN_ROOT_MARKER;
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

  function syncReduxFilter() {
    const store = findReduxStore();
    if (!store) return false;

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
          ${mode1Mark} <b>Closed hidden</b> (hide ✓ and finalized «, ‹✓›, », «»)<br/>
          ${mode2Mark} <b>Active only</b> (also hide open chain roots ▸)
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

    let labelText = '';
    if (filterEnabled) {
      labelText =
        filterScope === 'active-only'
          ? `Active only · ${totalClosedCount} hidden`
          : `Closed hidden: ${totalClosedCount}`;
    } else {
      labelText =
        filterScope === 'active-only'
          ? `All chats (${totalClosedCount} non-active)`
          : `All chats (${totalClosedCount} closed)`;
    }

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
        (typeof target.closest === 'function' &&
          (target.closest('#jetski-closed-chats-filter-bubble') ||
            target.closest(`.${BUTTON_CLASS}`)))
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
    for (const el of document.querySelectorAll(`.${BUTTON_CLASS}`)) {
      el.remove();
    }
    const bubble = document.getElementById('jetski-closed-chats-filter-bubble');
    if (bubble) {
      bubble.remove();
    }
  };

  runFullSync();
})();
