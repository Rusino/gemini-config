// Popup controller for Safe Tab Deduper user interface

document.addEventListener('DOMContentLoaded', () => {
  const autoDedupeToggle = document.getElementById('autoDedupeToggle');
  const pauseBtn = document.getElementById('pauseBtn');
  const dupList = document.getElementById('dupList');
  const dupCountLabel = document.getElementById('dupCountLabel');
  const versionLabel = document.getElementById('versionLabel');
  const closeSelectedBtn = document.getElementById('closeSelectedBtn');
  const recentList = document.getElementById('recentList');

  let currentDuplicates = [];
  let selectedTabIds = new Set();
  let pauseUntilMs = 0;

  function formatCompactUrl(raw) {
    try {
      const u = new URL(raw);
      return u.host + u.pathname + (u.search ? u.search.slice(0, 30) : '');
    } catch {
      return raw || '';
    }
  }

  function updatePauseButton() {
    const now = Date.now();
    if (pauseUntilMs && pauseUntilMs > now) {
      const remainingSec = Math.ceil((pauseUntilMs - now) / 1000);
      pauseBtn.textContent = `Resume (${remainingSec}s)`;
    } else {
      pauseBtn.textContent = 'Pause 60s';
    }
  }

  function renderDuplicates(closable, totalTabs) {
    currentDuplicates = closable || [];
    selectedTabIds = new Set(currentDuplicates.map((item) => item.tabId));

    const tabsSuffix =
      typeof totalTabs === 'number' ? ` (${totalTabs} tabs)` : '';
    dupCountLabel.textContent = `${currentDuplicates.length} found${tabsSuffix}`;
    if (!currentDuplicates.length) {
      dupList.innerHTML = '<div class="empty-state">No duplicates open</div>';
      closeSelectedBtn.disabled = true;
      closeSelectedBtn.textContent = 'Close selected (0)';
      return;
    }

    dupList.innerHTML = '';
    for (const item of currentDuplicates) {
      const row = document.createElement('div');
      row.className = 'tab-row';

      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = selectedTabIds.has(item.tabId);
      cb.addEventListener('change', () => {
        if (cb.checked) {
          selectedTabIds.add(item.tabId);
        } else {
          selectedTabIds.delete(item.tabId);
        }
        updateCloseButton();
      });

      const info = document.createElement('div');
      info.className = 'tab-info';

      const name = document.createElement('div');
      name.className = 'tab-name';
      name.textContent = item.title || item.url;

      const urlSpan = document.createElement('div');
      urlSpan.className = 'tab-url';
      urlSpan.textContent = formatCompactUrl(item.url);

      info.appendChild(name);
      info.appendChild(urlSpan);

      const badge = document.createElement('span');
      badge.className = 'badge';
      badge.textContent = `kept: ${item.keptReason || 'kept'}`;

      row.appendChild(cb);
      row.appendChild(info);
      row.appendChild(badge);
      dupList.appendChild(row);
    }
    updateCloseButton();
  }

  function updateCloseButton() {
    const count = selectedTabIds.size;
    closeSelectedBtn.textContent = `Close selected (${count})`;
    closeSelectedBtn.disabled = count === 0;
  }

  function renderRecent(recentSwitches) {
    if (!recentSwitches || !recentSwitches.length) {
      recentList.innerHTML =
        '<div class="empty-state" style="padding: 8px 0;">No tabs switched recently</div>';
      return;
    }
    recentList.innerHTML = '';
    for (const entry of recentSwitches) {
      const row = document.createElement('div');
      row.className = 'recent-row';

      const label = document.createElement('span');
      label.className = 'recent-url';
      label.title = entry.closedUrl;
      label.textContent = formatCompactUrl(entry.closedUrl);

      const btn = document.createElement('button');
      btn.className = 'reopen-btn';
      btn.textContent = 'Reopen';
      btn.addEventListener('click', () => {
        chrome.runtime.sendMessage({
          action: 'reopenUrl',
          url: entry.closedUrl,
        });
      });

      row.appendChild(label);
      row.appendChild(btn);
      recentList.appendChild(row);
    }
  }

  function refreshState() {
    chrome.runtime.sendMessage({action: 'getState'}, (response) => {
      if (!response || response.error) return;
      if (versionLabel && response.version) {
        versionLabel.textContent =
          'v' + String(response.version).replace(/\.0$/, '');
      }
      autoDedupeToggle.checked = Boolean(response.settings?.autoDedupe);
      pauseUntilMs = Number(response.settings?.pauseUntilMs || 0);
      updatePauseButton();
      renderDuplicates(response.duplicates?.closable || [], response.totalTabs);
      renderRecent(response.recentAutoSwitches || []);
    });
  }

  autoDedupeToggle.addEventListener('change', () => {
    chrome.runtime.sendMessage({
      action: 'updateSettings',
      settings: {autoDedupe: autoDedupeToggle.checked},
    });
  });

  pauseBtn.addEventListener('click', () => {
    const now = Date.now();
    const newPause = pauseUntilMs && pauseUntilMs > now ? 0 : now + 60000;
    pauseUntilMs = newPause;
    updatePauseButton();
    chrome.runtime.sendMessage({
      action: 'updateSettings',
      settings: {pauseUntilMs: newPause},
    });
  });

  closeSelectedBtn.addEventListener('click', () => {
    const tabIds = [...selectedTabIds];
    if (!tabIds.length) return;
    closeSelectedBtn.disabled = true;
    closeSelectedBtn.textContent = 'Closing...';
    chrome.runtime.sendMessage(
      {action: 'closeSelectedDuplicates', tabIds},
      () => {
        refreshState();
      },
    );
  });

  refreshState();
  setInterval(updatePauseButton, 1000);
});
