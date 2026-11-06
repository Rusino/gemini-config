# Safe Tab Deduper (Chrome Extension)

A lightweight, zero-dependency Manifest V3 unpacked Chrome extension that prevents duplicate
tabs by focusing open tabs without reloading, preserving form inputs, scroll position, and drafts.

## Features

- **Auto-focus existing tabs**: When clicking a link that is already open in another tab,
  the extension brings the existing tab to the foreground and removes the newly opened clone.
- **Zero DOM reload / draft preservation**: The existing tab is never reloaded (`url` is only
  passed to `chrome.tabs.update` for same-document `#anchor` / `#comment` fragment scrolls on
  the survivor's exact `origin + pathname + search`), so unsaved text, scroll position, and
  DevTools stay intact.
- **Smart Gerrit matching**: Opening CL overview (`+/1414016`) switches to any open patchset
  of that CL (`+/1414016/3`), while keeping explicit patchset comparisons, `?tab=checks`,
  per-file diffs, and inline comment links open on click; manual cleanup collapses all subviews
  of the same CL into one main overview tab.
- **Smart Docs & Sheets matching**: Strips user index (`u/0`), Google redirect wrappers, and
  heading hashes, while keeping different document tabs (`?tab=t.<id>`) and sheet tabs
  (`#gid=<id>`) distinct.
- **Smart Issue Tracker & GitHub matching**: Unifies issue mirrors, `u/0` prefixes, and
  `#comment` / `#issuecomment` anchors (scrolling the open tab to the anchor without reload),
  and collapses GitHub PR subviews (`pull/<N>/files`, `commits`, `checks`) during manual cleanup.
- **Protected tabs**: Pinned tabs and audible (playing audio) tabs are never closed; manual
  cleanup also deduplicates redundant copies inside Chrome Tab Groups (preferring grouped
  survivors over loose copies).
- **Safe duplicate detection**: Native `Right-click -> Duplicate` and intentional same-page
  clones are detected and kept open.
- **Popup dashboard**:
  - One-click toggle for automatic deduplication.
  - 60-second pause button.
  - Manual preview of open duplicate tabs with selective closure.
  - List of recent auto-switches with a 1-click `Reopen` button.

## Installation (Unpacked)

1. Open `chrome://extensions` in Google Chrome.
2. Enable **Developer mode** using the toggle switch in the top-right corner.
3. Click **Load unpacked** and select this directory:
   `~/.gemini/config/chrome_extensions/tab_deduper`
4. The extension icon will appear in the Chrome toolbar.

## Permissions

The extension requests only two minimal Chrome permissions:
- `tabs`: to read open tab URLs and titles, activate existing tabs, and close redundant tabs.
- `storage`: to save user preferences (auto-dedupe on/off, pause timer).

No `host_permissions` or `content_scripts` are requested. The extension has zero access
to web page content, cookies, or network traffic.
