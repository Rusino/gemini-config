// Pure URL canonicalization and deduplication logic for Safe Tab Deduper

// Loopback and internal dev host patterns excluded from auto-deduplication
const LOOPBACK_HOST_REGEX =
  /^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|.*\.localhost)$/i;

// Multi-pane web applications where multiple tabs are often intentional
const DEFAULT_EXCLUDED_HOSTS = new Set([
  'mail.google.com',
  'calendar.google.com',
  'meet.google.com',
]);

// Standard tracking query parameters stripped before canonical comparison
const TRACKING_QUERY_PARAMS = new Set([
  'utm_source',
  'utm_medium',
  'utm_campaign',
  'utm_term',
  'utm_content',
  'pli',
  'usp',
]);

export function isAutoDedupeExcluded(rawUrl, extraExcludedHosts = []) {
  if (!rawUrl || typeof rawUrl !== 'string') return true;
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return true;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return true;
  const host = parsed.hostname.toLowerCase();
  if (LOOPBACK_HOST_REGEX.test(host)) return true;
  if (DEFAULT_EXCLUDED_HOSTS.has(host)) return true;
  for (const extra of extraExcludedHosts) {
    if (extra && host === String(extra).toLowerCase()) return true;
  }
  return false;
}

export function parseResourceDescriptor(rawUrl) {
  if (!rawUrl || typeof rawUrl !== 'string') {
    return {excluded: true, exactKey: null, gerritInfo: null};
  }
  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    return {excluded: true, exactKey: null, gerritInfo: null};
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return {excluded: true, exactKey: null, gerritInfo: null};
  }

  const host = url.host.toLowerCase();
  const hostname = url.hostname.toLowerCase();
  const pathname = url.pathname;

  // Clean tracking query parameters
  const searchParams = new URLSearchParams(url.search);
  for (const p of TRACKING_QUERY_PARAMS) {
    searchParams.delete(p);
  }

  // Rule A: Gerrit code review
  const isGerritHost =
    hostname.endsWith('-review.googlesource.com') ||
    hostname === 'gerrit.googlesource.com' ||
    hostname === 'gerrit.test';
  if (isGerritHost) {
    // Comment link check: /c/<project>/+/<changeNum>/comment/<commentId>
    const commentMatch = pathname.match(
      /^\/c\/(.+?)\/\+\/(\d+)\/comment\/([0-9a-fA-F_]+)\/?$/,
    );
    if (commentMatch) {
      const [, project, changeNum, commentId] = commentMatch;
      return {
        excluded: false,
        exactKey: `gerrit-comment:${host}:${project}:${changeNum}:${commentId}`,
        gerritInfo: null,
      };
    }

    // CL overview or patchset or file diff view
    const clMatch = pathname.match(
      /^\/c\/(.+?)\/\+\/(\d+)(?:\/(\d+)(?:\/(.+))?)?\/?$/,
    );
    if (clMatch) {
      const [, project, changeNum, patchset, filePath] = clMatch;
      const checksTab = searchParams.get('tab') === 'checks';
      if (filePath) {
        return {
          excluded: false,
          exactKey: `gerrit-file:${host}:${project}:${changeNum}:${patchset || 'any'}:${filePath}`,
          gerritInfo: null,
        };
      }
      return {
        excluded: false,
        exactKey: `gerrit:${host}:${project}:${changeNum}:ps=${patchset || '*'}:checks=${
          checksTab ? '1' : '0'
        }`,
        gerritInfo: {
          host,
          project,
          changeNum,
          patchset: patchset || null,
          filePath: null,
          checksTab,
          commentId: null,
        },
      };
    }
  }

  // Rule B: Issue Trackers
  const isIssueHost =
    hostname === 'issues.chromium.org' ||
    hostname === 'issuetracker.google.com' ||
    hostname.endsWith('.google.com') ||
    hostname.endsWith('.issues.test');
  if (isIssueHost) {
    const issueMatch = pathname.match(/^\/issues\/(\d+)\/?$/);
    if (issueMatch) {
      const [, issueId] = issueMatch;
      const commentHash = url.hash.startsWith('#comment') ? url.hash : '';
      return {
        excluded: false,
        exactKey: `issue:${issueId}${commentHash ? ':' + commentHash : ''}`,
        gerritInfo: null,
      };
    }
  }

  // Rule C: Google Docs, Sheets, Slides
  const isDocsHost = hostname === 'docs.google.com' || hostname === 'docs.test';
  if (isDocsHost) {
    const docsMatch = pathname.match(
      /^\/(document|spreadsheets|presentation|drawings)(?:\/u\/\d+)?\/d\/([a-zA-Z0-9_-]+)(?:\/|$)/,
    );
    if (docsMatch) {
      const [, docType, docId] = docsMatch;
      const rawTab = searchParams.get('tab');
      const docTab = !rawTab || rawTab === 't.0' ? 't.0' : rawTab;
      let gid = 'none';
      if (docType === 'spreadsheets') {
        const hashGid = url.hash.match(/#gid=(\d+)/);
        gid = !hashGid || hashGid[1] === '0' ? 'gid=0' : `gid=${hashGid[1]}`;
      }
      return {
        excluded: false,
        exactKey: `gdoc:${docType}:${docId}:tab=${docTab}:${gid}`,
        gerritInfo: null,
      };
    }
  }

  // Rule D: GitHub pull requests and issues
  const isGithubHost = hostname === 'github.com' || hostname === 'github.test';
  if (isGithubHost) {
    const ghMatch = pathname.match(
      /^\/([^/]+)\/([^/]+)\/(pull|issues)\/(\d+)(?:\/(files|commits|checks))?\/?$/,
    );
    if (ghMatch) {
      const [, owner, repo, kind, num, subview] = ghMatch;
      return {
        excluded: false,
        exactKey: `gh:${owner.toLowerCase()}/${repo.toLowerCase()}:${kind}:${num}:${
          subview || 'overview'
        }${url.hash || ''}`,
        gerritInfo: null,
      };
    }
  }

  // Rule E: General HTTP(S) canonical representation
  const sortedParams = [...searchParams.entries()].sort(([a], [b]) =>
    a.localeCompare(b),
  );
  const searchStr = sortedParams.length
    ? '?' + new URLSearchParams(sortedParams).toString()
    : '';
  const cleanPath =
    pathname.length > 1 && pathname.endsWith('/')
      ? pathname.slice(0, -1)
      : pathname;
  const hashStr = url.hash && url.hash !== '#' ? url.hash : '';

  return {
    excluded: false,
    exactKey: `url:${url.protocol}//${host}${cleanPath}${searchStr}${hashStr}`,
    gerritInfo: null,
  };
}

export function urlsMatchForAutoDedupe(
  newUrl,
  existingUrl,
  extraExcludedHosts = [],
) {
  if (
    isAutoDedupeExcluded(newUrl, extraExcludedHosts) ||
    isAutoDedupeExcluded(existingUrl, extraExcludedHosts)
  ) {
    return false;
  }

  const a = parseResourceDescriptor(newUrl);
  const b = parseResourceDescriptor(existingUrl);
  if (a.excluded || b.excluded) return false;

  // Gerrit reviews: allow CL root to focus an explicit patchset, but keep different patchsets open
  if (a.gerritInfo && b.gerritInfo) {
    if (
      a.gerritInfo.host !== b.gerritInfo.host ||
      a.gerritInfo.project !== b.gerritInfo.project ||
      a.gerritInfo.changeNum !== b.gerritInfo.changeNum ||
      a.gerritInfo.checksTab !== b.gerritInfo.checksTab
    ) {
      return false;
    }
    // Opening root (/+/123) matches any open patchset (/+/123/2)
    if (a.gerritInfo.patchset === null) return true;
    // Opening specific patchset (/+/123/2) matches root or exact patchset, but not another patchset
    if (b.gerritInfo.patchset === null) return true;
    return a.gerritInfo.patchset === b.gerritInfo.patchset;
  }

  return a.exactKey !== null && a.exactKey === b.exactKey;
}

export function shouldSkipIntentionalDuplicate({
  newTab,
  targetTab,
  prevActiveTabIdByWindow,
}) {
  if (!newTab || !targetTab) return false;
  // Redirects or CL-root-to-patchset canonical matches have distinct raw URLs
  const sameRawUrl =
    !newTab.url || !targetTab.url || newTab.url === targetTab.url;
  if (!sameRawUrl) return false;

  // Browser recorded direct parentage from the same exact URL
  if (newTab.openerTabId && newTab.openerTabId === targetTab.id) {
    return true;
  }
  // Native Duplicate Tab creates sibling next to active source tab without openerTabId
  const windowState = prevActiveTabIdByWindow?.get?.(newTab.windowId);
  const prevActiveId =
    windowState && typeof windowState === 'object'
      ? windowState.currentTabId !== newTab.id
        ? windowState.currentTabId
        : windowState.prevTabId
      : windowState;
  if (
    newTab.windowId === targetTab.windowId &&
    prevActiveId === targetTab.id &&
    newTab.index === targetTab.index + 1
  ) {
    return true;
  }
  return false;
}

export function pickSurvivorTab(candidates) {
  if (!candidates || !candidates.length) return null;
  return [...candidates].sort((a, b) => {
    if (Boolean(a.pinned) !== Boolean(b.pinned)) {
      return a.pinned ? -1 : 1;
    }
    const accessDiff = (b.lastAccessed || 0) - (a.lastAccessed || 0);
    if (accessDiff !== 0) return accessDiff;
    return (a.id || 0) - (b.id || 0);
  })[0];
}

function getGerritBaseGroupKey(gerritInfo) {
  return `gerrit-base:${gerritInfo.host}:${gerritInfo.project}:${
    gerritInfo.changeNum
  }:checks=${gerritInfo.checksTab ? '1' : '0'}`;
}

export function planManualCleanup(tabs, extraExcludedHosts = []) {
  if (!tabs || !Array.isArray(tabs)) {
    return {closable: [], groupsCount: 0};
  }

  const isProtected = (t) =>
    Boolean(
      t.pinned ||
        t.active ||
        t.audible ||
        (typeof t.groupId === 'number' && t.groupId !== -1),
    );

  const descByTab = new Map();
  const explicitGerritByBase = new Map();

  for (const tab of tabs) {
    if (!tab.url) continue;
    if (isAutoDedupeExcluded(tab.url, extraExcludedHosts)) continue;
    const desc = parseResourceDescriptor(tab.url);
    if (desc.excluded || !desc.exactKey) continue;
    descByTab.set(tab, desc);

    // Track highest explicit patchset per CL so unversioned root tabs merge into it
    if (desc.gerritInfo && desc.gerritInfo.patchset !== null) {
      const baseKey = getGerritBaseGroupKey(desc.gerritInfo);
      const psNum = Number(desc.gerritInfo.patchset) || 0;
      const existing = explicitGerritByBase.get(baseKey);
      if (!existing || psNum > existing.psNum) {
        explicitGerritByBase.set(baseKey, {
          exactKey: desc.exactKey,
          psNum,
        });
      }
    }
  }

  // Group tabs by canonical key, merging Gerrit CL root tabs into explicit patchsets
  const groups = new Map();
  for (const [tab, desc] of descByTab.entries()) {
    let key = desc.exactKey;
    if (desc.gerritInfo && desc.gerritInfo.patchset === null) {
      const baseKey = getGerritBaseGroupKey(desc.gerritInfo);
      const explicitTarget = explicitGerritByBase.get(baseKey);
      if (explicitTarget) {
        key = explicitTarget.exactKey;
      }
    }
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(tab);
  }

  const closable = [];
  let groupsCount = 0;

  for (const [key, tabList] of groups.entries()) {
    if (tabList.length < 2) continue;
    groupsCount++;

    // Pick survivor tab: protected first, then explicit patchset over CL root, then latest accessed
    let survivor = tabList.find(isProtected);
    if (!survivor) {
      const explicitPatchsetTabs = tabList.filter((t) => {
        const info = descByTab.get(t)?.gerritInfo;
        return info && info.patchset !== null;
      });
      survivor = pickSurvivorTab(
        explicitPatchsetTabs.length ? explicitPatchsetTabs : tabList,
      );
    }

    for (const tab of tabList) {
      if (tab.id === survivor.id) continue;
      // Protected tabs never get closed in batch cleanup
      if (isProtected(tab)) continue;

      closable.push({
        tabId: tab.id,
        windowId: tab.windowId,
        title: tab.title || tab.url,
        url: tab.url,
        canonicalKey: key,
        keptTabId: survivor.id,
        keptTitle: survivor.title || survivor.url,
        keptReason: survivor.pinned
          ? 'pinned'
          : survivor.active
          ? 'active'
          : survivor.audible
          ? 'audible'
          : 'kept',
      });
    }
  }

  return {closable, groupsCount};
}
