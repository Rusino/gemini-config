// Background service worker for Safe Tab Deduper

import {
  buildSameDocumentHashUrl,
  isAutoDedupeExcluded,
  pickSurvivorTab,
  planManualCleanup,
  shouldSkipIntentionalDuplicate,
  urlsMatchForAutoDedupe,
} from './dedupe_logic.mjs';

const MAX_BIRTH_AGE_MS = 12000;
const POST_COMPLETE_REDIRECT_GRACE_MS = 1500;
const RECENT_LOG_LIMIT = 10;

// In-memory birth timestamps so only freshly opened tabs undergo auto-deduplication
const bornTabs = new Map();

// Tabs created explicitly via reopenUrl or user override that must not be closed
const bypassTabIds = new Set();
const pendingBypassUrls = new Map();

// Per-window active and prior tab IDs to detect native Duplicate tab commands
const prevActiveTabByWindow = new Map();

// Re-entrancy guard to prevent concurrent processing of the same tab
const inFlightTabIds = new Set();

// Fallback in-memory storage if chrome.storage.session is unavailable in older engines
let sessionSwitchesCache = [];

async function getStoredSettings() {
  const data = await chrome.storage.local.get({
    autoDedupe: true,
    pauseUntilMs: 0,
    excludedHosts: [],
  });
  return data;
}

async function getRecentSwitches() {
  if (chrome.storage?.session) {
    try {
      const res = await chrome.storage.session.get({recentAutoSwitches: []});
      return res.recentAutoSwitches || [];
    } catch {
      // Fall back to in-memory cache if session storage throws
    }
  }
  return sessionSwitchesCache;
}

async function recordAutoSwitch(entry) {
  const list = await getRecentSwitches();
  const updated = [entry, ...list.filter((x) => x.closedUrl !== entry.closedUrl)].slice(
    0,
    RECENT_LOG_LIMIT,
  );
  sessionSwitchesCache = updated;
  if (chrome.storage?.session) {
    try {
      await chrome.storage.session.set({recentAutoSwitches: updated});
    } catch {
      // Ignore session storage write errors
    }
  }
}

let badgeUpdateTimer = null;
function scheduleBadgeUpdate() {
  if (badgeUpdateTimer) clearTimeout(badgeUpdateTimer);
  badgeUpdateTimer = setTimeout(() => {
    updateBadge().catch(() => {});
  }, 150);
}

async function updateBadge() {
  const settings = await getStoredSettings();
  const now = Date.now();
  if (settings.pauseUntilMs && settings.pauseUntilMs > now) {
    await chrome.action.setBadgeText({text: '⏸'});
    await chrome.action.setBadgeBackgroundColor({color: '#5f6368'});
    return;
  }
  const allTabs = await chrome.tabs.query({});
  const {closable} = planManualCleanup(allTabs, settings.excludedHosts);
  const count = closable.length;
  await chrome.action.setBadgeText({text: count > 0 ? String(count) : ''});
  await chrome.action.setBadgeBackgroundColor({color: '#1a73e8'});
}

async function handleTabNavigation(tabId, url) {
  if (!url || typeof url !== 'string') return;
  if (bypassTabIds.has(tabId) || inFlightTabIds.has(tabId)) return;

  const now = Date.now();
  const birth = bornTabs.get(tabId);
  // Established tabs navigating normally must never be closed automatically
  if (!birth) return;
  if (now - birth.createdAt > MAX_BIRTH_AGE_MS) {
    bornTabs.delete(tabId);
    return;
  }
  if (
    birth.completedCount > 0 &&
    birth.firstCompleteAt !== null &&
    now - birth.firstCompleteAt > POST_COMPLETE_REDIRECT_GRACE_MS
  ) {
    bornTabs.delete(tabId);
    return;
  }

  const settings = await getStoredSettings();
  if (!settings.autoDedupe) return;
  if (settings.pauseUntilMs && settings.pauseUntilMs > now) return;
  if (isAutoDedupeExcluded(url, settings.excludedHosts)) return;

  inFlightTabIds.add(tabId);
  try {
    const currentTab = await chrome.tabs.get(tabId).catch(() => null);
    if (!currentTab) return;
    // Pinned or audible tabs must remain untouched while child tabs inside groups can dedupe
    if (currentTab.pinned || currentTab.audible) {
      return;
    }

    const allTabs = await chrome.tabs.query({});
    // Re-check bypass set after async queries in case reopenUrl registered tabId concurrently
    if (bypassTabIds.has(tabId)) return;
    const matches = allTabs.filter((t) => {
      if (t.id === tabId || inFlightTabIds.has(t.id)) return false;
      if (!t.url) return false;
      return urlsMatchForAutoDedupe(url, t.url, settings.excludedHosts);
    });

    if (!matches.length) return;
    const survivor = pickSurvivorTab(matches);
    if (!survivor) return;

    // Detect intentional user actions such as right-click Duplicate
    const initialUrl =
      birth.initialUrl && birth.initialUrl !== 'about:blank'
        ? birth.initialUrl
        : url;
    const isIntentional = shouldSkipIntentionalDuplicate({
      newTab: {...currentTab, url: initialUrl},
      targetTab: survivor,
      prevActiveTabIdByWindow: prevActiveTabByWindow,
    });
    if (isIntentional) {
      bornTabs.delete(tabId);
      return;
    }

    // Mark survivor established and scroll in-place without reload when only #hash differs
    bornTabs.delete(survivor.id);
    const hashUrl = buildSameDocumentHashUrl(survivor.url, url);
    await chrome.windows.update(survivor.windowId, {focused: true});
    await chrome.tabs.update(
      survivor.id,
      hashUrl ? {active: true, url: hashUrl} : {active: true},
    );

    // Close newly opened redundant tab only after focus successfully transferred
    await chrome.tabs.remove(tabId);
    bornTabs.delete(tabId);

    await recordAutoSwitch({
      ts: now,
      closedUrl: url,
      targetUrl: survivor.url,
      targetTabId: survivor.id,
      title: survivor.title || survivor.url,
    });
    scheduleBadgeUpdate();
  } catch (err) {
    console.warn('Safe Tab Deduper: auto-switch bypassed on error', err);
  } finally {
    inFlightTabIds.delete(tabId);
  }
}

// Lifecycle listeners
chrome.tabs.onCreated.addListener((tab) => {
  if (typeof tab.id === 'number') {
    const rawBirthUrl = tab.pendingUrl || tab.url || null;
    if (rawBirthUrl && (pendingBypassUrls.get(rawBirthUrl) || 0) > 0) {
      bypassTabIds.add(tab.id);
    }
    bornTabs.set(tab.id, {
      createdAt: Date.now(),
      firstCompleteAt: null,
      completedCount: 0,
      initialUrl:
        rawBirthUrl && rawBirthUrl !== 'about:blank' ? rawBirthUrl : null,
    });
    if (tab.url) {
      handleTabNavigation(tab.id, tab.url);
    }
  }
  scheduleBadgeUpdate();
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  const birth = bornTabs.get(tabId);
  if (birth) {
    const candidateUrl = changeInfo.url || tab.url;
    if (
      !birth.initialUrl &&
      candidateUrl &&
      candidateUrl !== 'about:blank'
    ) {
      birth.initialUrl = candidateUrl;
    }
    if (changeInfo.status === 'complete') {
      birth.completedCount++;
      if (birth.firstCompleteAt === null) {
        birth.firstCompleteAt = Date.now();
      }
    }
  }
  const effectiveUrl = changeInfo.url || tab.url;
  if (effectiveUrl) {
    handleTabNavigation(tabId, effectiveUrl);
  }
  scheduleBadgeUpdate();
});

chrome.tabs.onActivated.addListener((activeInfo) => {
  const existing = prevActiveTabByWindow.get(activeInfo.windowId);
  if (!existing) {
    prevActiveTabByWindow.set(activeInfo.windowId, {
      currentTabId: activeInfo.tabId,
      prevTabId: null,
    });
  } else if (existing.currentTabId !== activeInfo.tabId) {
    prevActiveTabByWindow.set(activeInfo.windowId, {
      currentTabId: activeInfo.tabId,
      prevTabId: existing.currentTabId,
    });
  }
  scheduleBadgeUpdate();
});

chrome.tabs.onRemoved.addListener((tabId) => {
  bornTabs.delete(tabId);
  bypassTabIds.delete(tabId);
  inFlightTabIds.delete(tabId);
  scheduleBadgeUpdate();
});

// Runtime messages from popup UI
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    if (!message || typeof message.action !== 'string') {
      sendResponse({error: 'Invalid message'});
      return;
    }
    if (message.action === 'getState') {
      const settings = await getStoredSettings();
      const allTabs = await chrome.tabs.query({});
      const duplicates = planManualCleanup(allTabs, settings.excludedHosts);
      const recentAutoSwitches = await getRecentSwitches();
      const version = chrome.runtime.getManifest?.()?.version || '1.2.0';
      sendResponse({
        settings,
        duplicates,
        recentAutoSwitches,
        totalTabs: allTabs.length,
        version,
      });
      return;
    }
    if (message.action === 'updateSettings') {
      const current = await getStoredSettings();
      const updated = {...current, ...message.settings};
      await chrome.storage.local.set(updated);
      scheduleBadgeUpdate();
      sendResponse({success: true, settings: updated});
      return;
    }
    if (message.action === 'closeSelectedDuplicates') {
      const tabIds = Array.isArray(message.tabIds) ? message.tabIds : [];
      let closedCount = 0;
      for (const id of tabIds) {
        try {
          const t = await chrome.tabs.get(id);
          // Protect tabs that became pinned or audible while popup was open
          if (t.pinned || t.audible) {
            continue;
          }
          await chrome.tabs.remove(id);
          closedCount++;
        } catch {
          // Tab may already have been closed
        }
      }
      scheduleBadgeUpdate();
      sendResponse({success: true, closedCount});
      return;
    }
    if (message.action === 'reopenUrl') {
      if (message.url) {
        const targetUrl = String(message.url);
        pendingBypassUrls.set(
          targetUrl,
          (pendingBypassUrls.get(targetUrl) || 0) + 1,
        );
        try {
          const created = await chrome.tabs.create({
            url: targetUrl,
            active: true,
          });
          if (created?.id) {
            bypassTabIds.add(created.id);
          }
        } finally {
          const rem = (pendingBypassUrls.get(targetUrl) || 1) - 1;
          if (rem <= 0) {
            pendingBypassUrls.delete(targetUrl);
          } else {
            pendingBypassUrls.set(targetUrl, rem);
          }
        }
      }
      sendResponse({success: true});
      return;
    }
    sendResponse({error: 'Unknown action'});
  })().catch((err) => {
    sendResponse({error: String(err)});
  });
  return true;
});

// Seed active tab state per window and update badge on extension startup
chrome.tabs
  .query({active: true})
  .then((tabs) => {
    for (const t of tabs) {
      if (
        typeof t.windowId === 'number' &&
        typeof t.id === 'number' &&
        !prevActiveTabByWindow.has(t.windowId)
      ) {
        prevActiveTabByWindow.set(t.windowId, {
          currentTabId: t.id,
          prevTabId: null,
        });
      }
    }
  })
  .catch(() => {});
scheduleBadgeUpdate();
