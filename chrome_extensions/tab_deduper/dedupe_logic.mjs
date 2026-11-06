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

// Unwrap single-hop Google redirect wrappers before parsing target resources
export function unwrapRedirectUrl(rawUrl) {
  if (!rawUrl || typeof rawUrl !== 'string') return rawUrl;
  try {
    const outer = new URL(rawUrl);
    const host = outer.hostname.toLowerCase();
    if (
      (host === 'www.google.com' || host === 'google.com') &&
      outer.pathname === '/url'
    ) {
      const target =
        outer.searchParams.get('q') || outer.searchParams.get('url');
      if (target) {
        const inner = new URL(target);
        if (inner.protocol === 'http:' || inner.protocol === 'https:') {
          return inner.href;
        }
      }
    }
  } catch {
    return rawUrl;
  }
  return rawUrl;
}

export function isAutoDedupeExcluded(rawUrl, extraExcludedHosts = []) {
  const unwrapped = unwrapRedirectUrl(rawUrl);
  if (!unwrapped || typeof unwrapped !== 'string') return true;
  let parsed;
  try {
    parsed = new URL(unwrapped);
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
  const unwrapped = unwrapRedirectUrl(rawUrl);
  if (!unwrapped || typeof unwrapped !== 'string') {
    return {excluded: true, exactKey: null, entityKey: null, gerritInfo: null};
  }
  let url;
  try {
    url = new URL(unwrapped);
  } catch {
    return {excluded: true, exactKey: null, entityKey: null, gerritInfo: null};
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return {excluded: true, exactKey: null, entityKey: null, gerritInfo: null};
  }

  const host = url.host.toLowerCase();
  const hostname = url.hostname.toLowerCase();
  const pathname = url.pathname;

  // Clean tracking query parameters
  const searchParams = new URLSearchParams(url.search);
  for (const p of TRACKING_QUERY_PARAMS) {
    searchParams.delete(p);
  }

  // Gerrit code review hosts keep subviews distinct on click, unify per CL in cleanup
  const isGerritHost =
    hostname.endsWith('-review.googlesource.com') ||
    hostname === 'gerrit.googlesource.com' ||
    hostname === 'gerrit.test';
  if (isGerritHost) {
    const commentMatch = pathname.match(
      /^(?:\/c\/.+?)?\/\+\/(\d+)\/comment\/([0-9a-fA-F_]+)\/?$/,
    );
    if (commentMatch) {
      const [, changeNum, commentId] = commentMatch;
      return {
        excluded: false,
        kind: 'gerrit',
        exactKey: `gerrit-comment:${host}:${changeNum}:${commentId}`,
        entityKey: `gerrit-entity:${host}:${changeNum}`,
        gerritInfo: null,
        viewRank: 1,
        patchsetNum: 0,
      };
    }

    const clMatch = pathname.match(
      /^(?:\/c\/.+?)?\/\+\/(\d+)(?:\/(\d+|\d+\.\.\d+)(?:\/(.+))?)?\/?$/,
    );
    if (clMatch) {
      const [, changeNum, patchset, filePath] = clMatch;
      const entityKey = `gerrit-entity:${host}:${changeNum}`;
      const checksTab = searchParams.get('tab') === 'checks';
      if (filePath) {
        return {
          excluded: false,
          kind: 'gerrit',
          exactKey: `gerrit-file:${host}:${changeNum}:${patchset || 'any'}:${filePath}`,
          entityKey,
          gerritInfo: null,
          viewRank: 1,
          patchsetNum: 0,
        };
      }
      const isRange = Boolean(patchset && patchset.includes('..'));
      const numericPs = !isRange && patchset ? Number(patchset) || 0 : 0;
      return {
        excluded: false,
        kind: 'gerrit',
        exactKey: `gerrit:${host}:${changeNum}:ps=${patchset || '*'}:checks=${
          checksTab ? '1' : '0'
        }`,
        entityKey,
        // Inter-patchset range diffs stay distinct from unversioned CL root on auto-click
        gerritInfo: isRange
          ? null
          : {
              host,
              changeNum,
              patchset: patchset || null,
              checksTab,
            },
        viewRank: !checksTab && !isRange ? 3 : 1,
        patchsetNum: !checksTab && !isRange ? numericPs : 0,
      };
    }
  }

  // Issue trackers unify mirrors, /u/<N>/ prefixes, and #comment deep links
  const isDedicatedIssueHost =
    hostname === 'issues.chromium.org' ||
    hostname === 'issues.skia.org' ||
    hostname === 'bugs.chromium.org' ||
    hostname === 'issuetracker.google.com' ||
    hostname.startsWith('b.') ||
    hostname.endsWith('.issues.test');
  const isAnyIssueHost = isDedicatedIssueHost || hostname.endsWith('.google.com');
  if (isAnyIssueHost) {
    const issueMatch =
      pathname.match(/^(?:\/u\/\d+)?\/issues\/(\d+)\/?$/) ||
      (isDedicatedIssueHost
        ? pathname.match(/^(?:\/u\/\d+)?\/(\d{6,})\/?$/)
        : null);
    if (issueMatch) {
      const [, issueId] = issueMatch;
      const key = `issue:${issueId}`;
      return {
        excluded: false,
        kind: 'issue',
        exactKey: key,
        entityKey: key,
        gerritInfo: null,
        viewRank: 2,
        patchsetNum: 0,
      };
    }
  }

  // Google Docs, Sheets, and Slides preserve published /d/e/ IDs, copy/export, tabs, and gids
  const isDocsHost = hostname === 'docs.google.com' || hostname === 'docs.test';
  if (isDocsHost) {
    const docsMatch = pathname.match(
      new RegExp(
        '^/(document|spreadsheets|presentation|drawings)(?:/u/\\d+)?/d/' +
          '(e/[a-zA-Z0-9_-]+|[a-zA-Z0-9_-]+)(?:/([^/?#]+))?',
      ),
    );
    if (docsMatch) {
      const [, docType, docId, docAction] = docsMatch;
      if (docAction !== 'copy' && docAction !== 'export') {
        const rawTab = searchParams.get('tab');
        const docTab = !rawTab || rawTab === 't.0' ? 't.0' : rawTab;
        let gid = 'none';
        if (docType === 'spreadsheets') {
          const hashGid = url.hash.match(/#gid=(\d+)/);
          gid = !hashGid || hashGid[1] === '0' ? 'gid=0' : `gid=${hashGid[1]}`;
        }
        const key = `gdoc:${docType}:${docId}:tab=${docTab}:${gid}`;
        return {
          excluded: false,
          kind: 'gdoc',
          exactKey: key,
          entityKey: key,
          gerritInfo: null,
          viewRank: 2,
          patchsetNum: 0,
        };
      }
    }
  }

  // GitHub strips comment hashes on click, preserves distinct commits/checks, collapses in cleanup
  const isGithubHost = hostname === 'github.com' || hostname === 'github.test';
  if (isGithubHost) {
    const ghMatch = pathname.match(
      new RegExp(
        '^/([^/]+)/([^/]+)/(pull|issues)/(\\d+)' +
          '(?:/(files|changes|commits|checks)(?:/([^/?#]+))?)?/?$',
      ),
    );
    if (ghMatch) {
      const [, owner, repo, kind, num, rawSubview, subDetail] = ghMatch;
      const subview =
        rawSubview === 'changes' ? 'files' : rawSubview || 'overview';
      const checkRun =
        subview === 'checks' ? searchParams.get('check_run_id') || '' : '';
      const repoSlug = `${owner.toLowerCase()}/${repo.toLowerCase()}`;
      return {
        excluded: false,
        kind: 'gh',
        exactKey: `gh:${repoSlug}:${kind}:${num}:${subview}:${subDetail || ''}:${checkRun}`,
        entityKey: `gh-entity:${repoSlug}:${kind}:${num}`,
        gerritInfo: null,
        viewRank: subview === 'overview' ? 2 : 1,
        patchsetNum: 0,
      };
    }
  }

  // General HTTP(S) strips in-page #anchors while preserving #/ client-side SPA routes
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
  const spaRouteHash =
    url.hash.startsWith('#/') || url.hash.startsWith('#!/') ? url.hash : '';
  const key = `url:${url.protocol}//${host}${cleanPath}${searchStr}${spaRouteHash}`;

  return {
    excluded: false,
    kind: 'url',
    exactKey: key,
    entityKey: key,
    gerritInfo: null,
    viewRank: 2,
    patchsetNum: 0,
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

  // Gerrit reviews allow CL root to focus an explicit patchset while keeping two patchsets open
  if (a.gerritInfo && b.gerritInfo) {
    if (
      a.gerritInfo.host !== b.gerritInfo.host ||
      a.gerritInfo.changeNum !== b.gerritInfo.changeNum ||
      a.gerritInfo.checksTab !== b.gerritInfo.checksTab
    ) {
      return false;
    }
    if (a.gerritInfo.patchset === null) return true;
    if (b.gerritInfo.patchset === null) return true;
    return a.gerritInfo.patchset === b.gerritInfo.patchset;
  }

  return a.exactKey !== null && a.exactKey === b.exactKey;
}

// Build a fragment-only update URL on survivor's exact origin+path+query to scroll without reload
export function buildSameDocumentHashUrl(existingUrl, incomingRawUrl) {
  if (!existingUrl || !incomingRawUrl) return null;
  try {
    const existing = new URL(existingUrl);
    const incoming = new URL(unwrapRedirectUrl(incomingRawUrl));
    if (
      !incoming.hash ||
      incoming.hash === '#' ||
      incoming.hash.startsWith('#/') ||
      incoming.hash.startsWith('#!/')
    ) {
      return null;
    }
    const existingHost = existing.hostname.toLowerCase();
    if (
      (existingHost === 'docs.google.com' || existingHost === 'docs.test') &&
      existing.pathname.startsWith('/spreadsheets/') &&
      incoming.hash.startsWith('#gid=')
    ) {
      return null;
    }

    const sameBase =
      existing.origin === incoming.origin &&
      existing.pathname === incoming.pathname &&
      existing.search === incoming.search;
    if (!sameBase && !urlsMatchForAutoDedupe(incoming.href, existingUrl)) {
      return null;
    }

    const target = `${existing.origin}${existing.pathname}${existing.search}${incoming.hash}`;
    return target === existing.href ? null : target;
  } catch {
    return null;
  }
}

export function shouldSkipIntentionalDuplicate({
  newTab,
  targetTab,
  prevActiveTabIdByWindow,
}) {
  if (!newTab || !targetTab) return false;
  // Redirects, anchors, or CL-root-to-patchset matches have distinct raw URLs
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

export function planManualCleanup(tabs, extraExcludedHosts = []) {
  if (!tabs || !Array.isArray(tabs)) {
    return {closable: [], groupsCount: 0};
  }

  const isProtected = (t) => Boolean(t.pinned || t.active || t.audible);
  const isGrouped = (t) => typeof t.groupId === 'number' && t.groupId !== -1;

  const descByTab = new Map();
  const groups = new Map();

  for (const tab of tabs) {
    if (!tab.url) continue;
    if (isAutoDedupeExcluded(tab.url, extraExcludedHosts)) continue;
    const desc = parseResourceDescriptor(tab.url);
    if (desc.excluded || !desc.entityKey) continue;
    descByTab.set(tab, desc);
    if (!groups.has(desc.entityKey)) {
      groups.set(desc.entityKey, []);
    }
    groups.get(desc.entityKey).push(tab);
  }

  const closable = [];
  let groupsCount = 0;

  for (const [key, tabList] of groups.entries()) {
    if (tabList.length < 2) continue;
    groupsCount++;

    // Rank survivor: protected -> main overview -> latest patchset -> grouped -> latest accessed
    const survivor = [...tabList].sort((a, b) => {
      if (isProtected(a) !== isProtected(b)) {
        return isProtected(a) ? -1 : 1;
      }
      if (Boolean(a.pinned) !== Boolean(b.pinned)) {
        return a.pinned ? -1 : 1;
      }
      if (Boolean(a.active) !== Boolean(b.active)) {
        return a.active ? -1 : 1;
      }
      const da = descByTab.get(a);
      const db = descByTab.get(b);
      if ((da?.viewRank || 0) !== (db?.viewRank || 0)) {
        return (db?.viewRank || 0) - (da?.viewRank || 0);
      }
      if ((da?.patchsetNum || 0) !== (db?.patchsetNum || 0)) {
        return (db?.patchsetNum || 0) - (da?.patchsetNum || 0);
      }
      if (isGrouped(a) !== isGrouped(b)) {
        return isGrouped(a) ? -1 : 1;
      }
      const accessDiff = (b.lastAccessed || 0) - (a.lastAccessed || 0);
      if (accessDiff !== 0) return accessDiff;
      return (a.id || 0) - (b.id || 0);
    })[0];

    for (const tab of tabList) {
      if (tab.id === survivor.id) continue;
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
          : isGrouped(survivor)
          ? 'grouped'
          : 'kept',
      });
    }
  }

  return {closable, groupsCount};
}
