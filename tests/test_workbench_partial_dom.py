#!/usr/bin/env python3
"""Contract tests for flicker-free partial DOM reconciliation in Workbench."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
  sys.path.insert(0, REPO_ROOT)

from workbench import workbench_server  # pylint: disable=g-import-not-at-top


MINI_DOM_PRELUDE = r"""
const VOID_TAGS = new Set([
  'AREA', 'BASE', 'BR', 'COL', 'EMBED', 'HR', 'IMG',
  'INPUT', 'LINK', 'META', 'SOURCE', 'TRACK', 'WBR',
]);

function unescHtml(s) {
  return String(s ?? '')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');
}

function escAttr(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escText(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

const domStats = {
  innerHTMLSets: 0,
  insertions: 0,
  removals: 0,
  replacements: 0,
  attrSets: 0,
  attrRemovals: 0,
  textWrites: 0,
  scrollRestores: 0,
};

function resetDomStats() {
  for (const k of Object.keys(domStats)) domStats[k] = 0;
}

class MNode {
  constructor(nodeType) {
    this.nodeType = nodeType;
    this.parentNode = null;
  }
  get nextSibling() {
    if (!this.parentNode) return null;
    const idx = this.parentNode.childNodes.indexOf(this);
    return idx >= 0 && idx + 1 < this.parentNode.childNodes.length
      ? this.parentNode.childNodes[idx + 1] : null;
  }
}

class MText extends MNode {
  constructor(val) {
    super(3);
    this._val = String(val ?? '');
  }
  get nodeValue() { return this._val; }
  set nodeValue(v) {
    domStats.textWrites += 1;
    this._val = String(v ?? '');
  }
  get textContent() { return this._val; }
  set textContent(v) { this.nodeValue = v; }
}

class MComment extends MNode {
  constructor(val) {
    super(8);
    this._val = String(val ?? '');
  }
  get nodeValue() { return this._val; }
  set nodeValue(v) {
    domStats.textWrites += 1;
    this._val = String(v ?? '');
  }
}

class MElement extends MNode {
  constructor(tag, trackStats = true) {
    super(1);
    this.tagName = String(tag || 'DIV').toUpperCase();
    this._trackStats = trackStats;
    this.childNodes = [];
    this._attrs = new Map();
    this._listeners = {};
    this._value = '';
    this._dirtyValue = false;
    this._checked = false;
    this._open = false;
    this._disabled = false;
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.focusCalls = 0;
    this.style = {};
  }
  get id() { return this._attrs.get('id') || ''; }
  set id(v) { this.setAttribute('id', v); }
  get type() {
    return this._attrs.get('type') || (this.tagName === 'TEXTAREA' ? 'textarea' : '');
  }
  get open() { return this._open; }
  set open(v) {
    this._open = Boolean(v);
    if (this._open) this._attrs.set('open', '');
    else this._attrs.delete('open');
  }
  get checked() { return this._checked; }
  set checked(v) { this._checked = Boolean(v); }
  get disabled() { return this._disabled; }
  set disabled(v) { this._disabled = Boolean(v); }
  get value() {
    if (this.tagName === 'TEXTAREA' && !this._dirtyValue) {
      return this.textContent;
    }
    return this._value;
  }
  set value(v) {
    this._dirtyValue = true;
    this._value = String(v ?? '');
  }
  focus() {
    this.focusCalls += 1;
    global.document.activeElement = this;
  }
  setSelectionRange(s, e) {
    this.selectionStart = s;
    this.selectionEnd = e;
  }
  reset() {}
  addEventListener(ev, fn) {
    (this._listeners[ev] = this._listeners[ev] || []).push(fn);
  }
  async dispatch(ev, payload) {
    for (const fn of (this._listeners[ev] || [])) {
      await fn(payload || {});
    }
  }
  get attributes() {
    const arr = [];
    for (const [name, value] of this._attrs.entries()) arr.push({name, value});
    return arr;
  }
  hasAttribute(name) { return this._attrs.has(String(name).toLowerCase()); }
  getAttribute(name) {
    const k = String(name).toLowerCase();
    return this._attrs.has(k) ? this._attrs.get(k) : null;
  }
  setAttribute(name, val) {
    const k = String(name).toLowerCase();
    const s = String(val ?? '');
    if (this._trackStats) domStats.attrSets += 1;
    this._attrs.set(k, s);
    if (k === 'open') this._open = true;
    if (k === 'checked') this._checked = true;
    if (k === 'disabled') this._disabled = true;
    if (k === 'value' && !this._dirtyValue) this._value = s;
  }
  removeAttribute(name) {
    const k = String(name).toLowerCase();
    if (this._attrs.has(k)) {
      if (this._trackStats) domStats.attrRemovals += 1;
      this._attrs.delete(k);
    }
    if (k === 'open') this._open = false;
    if (k === 'disabled') this._disabled = false;
  }
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child, true);
    child.parentNode = this;
    this.childNodes.push(child);
    if (this._trackStats) domStats.insertions += 1;
    return child;
  }
  insertBefore(child, refNode) {
    if (!refNode) return this.appendChild(child);
    if (child === refNode) return child;
    if (child.parentNode) child.parentNode.removeChild(child, true);
    const idx = this.childNodes.indexOf(refNode);
    if (idx < 0) return this.appendChild(child);
    child.parentNode = this;
    this.childNodes.splice(idx, 0, child);
    if (this._trackStats) domStats.insertions += 1;
    return child;
  }
  removeChild(child, silent = false) {
    const idx = this.childNodes.indexOf(child);
    if (idx >= 0) {
      this.childNodes.splice(idx, 1);
      child.parentNode = null;
      if (!silent && this._trackStats) domStats.removals += 1;
    }
    return child;
  }
  replaceChild(newChild, oldChild) {
    const idx = this.childNodes.indexOf(oldChild);
    if (idx < 0) return oldChild;
    if (newChild.parentNode) newChild.parentNode.removeChild(newChild, true);
    oldChild.parentNode = null;
    newChild.parentNode = this;
    this.childNodes[idx] = newChild;
    if (this._trackStats) domStats.replacements += 1;
    return oldChild;
  }
  get textContent() {
    return this.childNodes.map(n => n.nodeType === 8 ? '' : n.textContent).join('');
  }
  set textContent(v) {
    const s = String(v ?? '');
    if (this.childNodes.length === 1 && this.childNodes[0].nodeType === 3) {
      if (this._trackStats) domStats.textWrites += 1;
      this.childNodes[0]._val = s;
      return;
    }
    for (const c of this.childNodes) c.parentNode = null;
    this.childNodes = s ? [new MText(s)] : [];
    if (this.childNodes[0]) this.childNodes[0].parentNode = this;
    if (this._trackStats) domStats.textWrites += 1;
  }
  get innerHTML() {
    return this.childNodes.map(serializeNode).join('');
  }
  set innerHTML(html) {
    if (this.tagName === 'TEXTAREA') {
      const dec = unescHtml(String(html ?? ''));
      this._value = dec;
      this._dirtyValue = true;
      return;
    }
    if (this._trackStats) domStats.innerHTMLSets += 1;
    for (const c of this.childNodes) c.parentNode = null;
    this.childNodes = parseHtmlFragment(String(html ?? ''), this);
  }
  get outerHTML() {
    return serializeNode(this);
  }
  matches(sel) {
    return sel.split(',').some(part => makeSingleMatcher(part.trim())(this));
  }
  closest(sel) {
    let cur = this;
    while (cur && cur.nodeType === 1) {
      if (cur.matches(sel)) return cur;
      cur = cur.parentNode;
    }
    return null;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (el) => {
      for (const ch of el.children) {
        if (ch.matches(sel)) out.push(ch);
        walk(ch);
      }
    };
    walk(this);
    return out;
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
}

function makeSingleMatcher(s) {
  if (!s) return () => false;
  const attrEq = s.match(/^([A-Za-z0-9_-]+)?\[([A-Za-z0-9_-]+)="([^"]*)"\]$/);
  if (attrEq) {
    const [, tag, attr, val] = attrEq;
    return el => (!tag || el.tagName === tag.toUpperCase()) && el.getAttribute(attr) === val;
  }
  const attrHas = s.match(/^([A-Za-z0-9_-]+)?\[([A-Za-z0-9_-]+)\]$/);
  if (attrHas) {
    const [, tag, attr] = attrHas;
    return el => (!tag || el.tagName === tag.toUpperCase()) && el.hasAttribute(attr);
  }
  if (s.startsWith('.')) {
    const cls = s.slice(1);
    return el => (el.getAttribute('class') || '').split(/\s+/).includes(cls);
  }
  if (s.startsWith('#')) {
    const id = s.slice(1);
    return el => el.id === id;
  }
  return el => el.tagName === s.toUpperCase();
}

function serializeNode(n) {
  if (n.nodeType === 3) return escText(n.nodeValue);
  if (n.nodeType === 8) return `<!--${n.nodeValue}-->`;
  const tag = n.tagName.toLowerCase();
  let attrs = '';
  for (const {name, value} of n.attributes) {
    attrs += value === '' ? ` ${name}` : ` ${name}="${escAttr(value)}"`;
  }
  if (VOID_TAGS.has(n.tagName)) return `<${tag}${attrs}>`;
  return `<${tag}${attrs}>${n.innerHTML}</${tag}>`;
}

function parseAttrs(raw, el) {
  const re = /([A-Za-z0-9_:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
  let m;
  while ((m = re.exec(raw)) !== null) {
    const k = m[1].toLowerCase();
    const v = m[2] !== undefined ? unescHtml(m[2])
      : (m[3] !== undefined ? unescHtml(m[3]) : (m[4] !== undefined ? unescHtml(m[4]) : ''));
    el._attrs.set(k, v);
    if (k === 'open') el._open = true;
    if (k === 'checked') el._checked = true;
    if (k === 'disabled') el._disabled = true;
    if (k === 'value') el._value = v;
  }
}

function parseHtmlFragment(html, rootParent) {
  const root = {childNodes: []};
  const stack = [root];
  let i = 0;
  while (i < html.length) {
    if (html.startsWith('<!--', i)) {
      const end = html.indexOf('-->', i + 4);
      const val = end >= 0 ? html.slice(i + 4, end) : html.slice(i + 4);
      stack[stack.length - 1].childNodes.push(new MComment(val));
      i = end >= 0 ? end + 3 : html.length;
      continue;
    }
    if (html[i] === '<') {
      if (html[i + 1] === '/') {
        const end = html.indexOf('>', i + 2);
        const rawClose = end >= 0 ? html.slice(i + 2, end) : html.slice(i + 2);
        const closeTag = rawClose.trim().toUpperCase();
        for (let s = stack.length - 1; s >= 1; s--) {
          if (stack[s].tagName === closeTag) {
            stack.length = s;
            break;
          }
        }
        i = end >= 0 ? end + 1 : html.length;
        continue;
      }
      const tagMatch = html.slice(i + 1).match(/^([A-Za-z][A-Za-z0-9-]*)/);
      if (tagMatch) {
        const tag = tagMatch[1];
        let j = i + 1 + tag.length;
        let quote = '';
        while (j < html.length) {
          const ch = html[j];
          if (quote) {
            if (ch === quote) quote = '';
          } else if (ch === '"' || ch === "'") {
            quote = ch;
          } else if (ch === '>') {
            break;
          }
          j += 1;
        }
        const sliceAttr = html.slice(i + 1 + tag.length, j);
        const rawAttrs = sliceAttr.replace(/\/\s*$/, '');
        const selfClose = /\/\s*$/.test(sliceAttr) || VOID_TAGS.has(tag.toUpperCase());
        const el = new MElement(tag, true);
        parseAttrs(rawAttrs, el);
        const curParent = stack[stack.length - 1];
        el.parentNode = curParent === root ? rootParent : curParent;
        curParent.childNodes.push(el);
        i = j + 1;
        if (el.tagName === 'TEXTAREA') {
          const closeIdx = html.toLowerCase().indexOf('</textarea>', i);
          const rawText = closeIdx >= 0 ? html.slice(i, closeIdx) : html.slice(i);
          if (rawText) {
            const tn = new MText(unescHtml(rawText));
            tn.parentNode = el;
            el.childNodes.push(tn);
          }
          el._value = unescHtml(rawText);
          i = closeIdx >= 0 ? closeIdx + '</textarea>'.length : html.length;
        } else if (!selfClose) {
          stack.push(el);
        }
        continue;
      }
    }
    const nextLt = html.indexOf('<', i + 1);
    const rawTxt = nextLt >= 0 ? html.slice(i, nextLt) : html.slice(i);
    if (rawTxt) {
      const tn = new MText(unescHtml(rawTxt));
      const curParent = stack[stack.length - 1];
      tn.parentNode = curParent === root ? rootParent : curParent;
      curParent.childNodes.push(tn);
    }
    i = nextLt >= 0 ? nextLt : html.length;
  }
  for (const n of root.childNodes) n.parentNode = rootParent;
  return root.childNodes;
}

function mountWorkbenchBody(bodyHtml) {
  const docEl = new MElement('HTML', true);
  const bodyEl = new MElement('BODY', true);
  bodyEl.childNodes = parseHtmlFragment(bodyHtml, bodyEl);
  docEl.appendChild(bodyEl);

  const parentMsgs = [];
  let pollFn = null;
  const memStore = new Map();

  global.localStorage = {
    getItem: k => (memStore.has(k) ? memStore.get(k) : null),
    setItem: (k, v) => memStore.set(k, String(v)),
  };
  global.window = {
    scrollY: 0,
    parent: { postMessage: msg => parentMsgs.push(msg) },
    location: { reload: () => {}, assign: () => {} },
    scrollTo: (_x, y) => {
      domStats.scrollRestores += 1;
      global.window.scrollY = y;
    },
    addEventListener: () => {},
    getSelection: () => '',
    prompt: () => '',
    alert: () => {},
    open: () => ({}),
  };
  global.setInterval = (fn) => {
    pollFn = fn;
    return 1;
  };
  global.document = {
    documentElement: docEl,
    body: bodyEl,
    activeElement: null,
    // Off-screen scratch elements created by esc()/patchSlotHtml should not inflate live DOM stats
    createElement: tag => new MElement(tag, false),
    getElementById: id => docEl.querySelector('#' + id),
    querySelectorAll: sel => docEl.querySelectorAll(sel),
    querySelector: sel => docEl.querySelector(sel),
    addEventListener: () => {},
  };
  resetDomStats();
  return {
    docEl,
    bodyEl,
    parentMsgs,
    getPollFn: () => pollFn,
  };
}

async function clickElement(el, extra = {}) {
  await global.document.body.dispatch('click', Object.assign({
    altKey: false,
    preventDefault: () => {},
    target: el,
  }, extra));
}
"""


def _make_work(
    wid: str,
    lane: str = "needs-you",
    owner: str = "user",
    title: str = "Cluster advance fix",
    activity_at: float = 1762800200.0,
    activity_et: str = "Nov 11 at 15:00 ET (5m ago)",
    ps: int = 1,
    mentions: int = 0,
) -> dict:
  arts = [
      {
          "artifact_key": f"buganizer:500{wid[-1]}",
          "kind": "buganizer",
          "role": "primary",
          "title": f"b/500{wid[-1]}: {title}",
          "url": f"https://b.corp.google.com/issues/500{wid[-1]}",
          "state": {"status": "ASSIGNED", "counterparty": "Ben Wagner"},
      },
      {
          "artifact_key": f"gerrit:skia:14100{wid[-1]}",
          "kind": "gerrit",
          "role": "primary",
          "title": f"Skia 14100{wid[-1]} PS{ps}: {title}",
          "url": f"https://skia-review.googlesource.com/c/skia/+/14100{wid[-1]}",
          "state": {
              "status": "NEW",
              "subject": title,
              "patchset": ps,
              "assigned_reviewers_count": 1,
              "rolls": {"Flutter": False},
          },
      },
  ]
  for idx in range(mentions):
    arts.append({
        "artifact_key": f"gerrit:skia:990{idx}",
        "kind": "gerrit",
        "role": "context_only",
        "title": f"Skia 990{idx}: Mentioned helper",
        "url": f"https://skia-review.googlesource.com/c/skia/+/990{idx}",
        "why_badge": "mentioned in chat",
        "state": {"status": "MERGED"},
    })
  return {
      "work_id": wid,
      "title": title,
      "lane": lane,
      "owner": owner,
      "is_virtual": False,
      "work_types": ["my-bugs", "my-cls"],
      "needs_you_types": ["my-bugs"] if lane == "needs-you" else [],
      "reasons": [f"Needs review on PS{ps}"],
      "updated_at": activity_at,
      "updated_et": activity_et,
      "activity_at": activity_at,
      "activity_et": activity_et,
      "origin_at": activity_at - 3600.0,
      "origin_et": "Nov 11 at 14:00 ET",
      "obligations": [],
      "artifacts": arts,
      "hints": [],
      "suggestions": [],
      "search_text": f"{wid} {title} ps{ps}".lower(),
  }


def _make_projection(
    works: list[dict],
    synced_et: str = "15:05 ET (just now)",
    tray: list[dict] | None = None,
) -> dict:
  tray_items = list(tray or [])
  return {
      "ui_version": 7,
      "synced_et": synced_et,
      "counts": {
          "needs_you": sum(1 for w in works if w.get("lane") == "needs-you"),
          "waiting": sum(1 for w in works if w.get("lane") == "waiting"),
          "obligations_open": 0,
          "unanswered": len(tray_items),
      },
      "metrics": {"link_precision": 0.98},
      "unanswered_tray": tray_items,
      "works": works,
  }


def _run_workbench_js(initial_proj: dict, test_js: str) -> dict:
  html = workbench_server.render_workbench_html(
      initial_proj, ui_version=7, _already_slim=True
  )
  body_inner = html.split("<body>", 1)[1].split("<script>", 1)[0]
  script_body = html.split("<script>", 1)[1].split("</script>", 1)[0]
  if len(script_body.encode("utf-8")) >= 128 * 1024:
    raise AssertionError("Workbench inline <script> exceeded Linux 128 KB MAX_ARG_STRLEN")
  full_js = (
      "const assert = require('assert');\n"
      + MINI_DOM_PRELUDE
      + f"\nconst env = mountWorkbenchBody({json.dumps(body_inner)});\n"
      + "const ssrCardsBeforeBoot = "
      + "Array.from(document.getElementById('wb-cards-container').children);\n"
      + script_body
      + "\n(async () => {\n"
      + test_js
      + "\n})().catch(err => { console.error(err); process.exit(1); });\n"
  )
  proc = subprocess.run(
      ["node", "-"],
      input=full_js,
      capture_output=True,
      text=True,
      timeout=15,
      check=False,
  )
  if proc.returncode != 0:
    raise AssertionError(
        f"Node exited with {proc.returncode}:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
  lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
  return json.loads(lines[-1]) if lines else {}


class WorkbenchPartialDomTest(unittest.TestCase):

  # Verifies SSR hydration morphs cards in place and quiet polls perform zero content DOM writes
  def test_quiet_poll_skips_all_card_and_tray_dom_mutations(self):
    proj = _make_projection([
        _make_work("work-1", lane="needs-you", title="First card"),
        _make_work("work-2", lane="waiting", title="Second card", activity_at=1762800100.0),
    ])
    next_proj = copy.deepcopy(proj)
    next_proj["synced_et"] = "15:06 ET (just now)"
    next_proj["works"][0]["updated_et"] = "Nov 11 at 15:00 ET (6m ago)"
    next_proj["works"][0]["activity_et"] = "Nov 11 at 15:00 ET (6m ago)"
    next_proj["works"][0]["search_text"] = "work-1 first card ps1 reindexed"
    next_proj["works"][1]["updated_et"] = "Nov 11 at 15:00 ET (2h 15m ago)"
    next_proj["works"][1]["activity_et"] = "Nov 11 at 15:00 ET (2h 15m ago)"
    res = _run_workbench_js(
        proj,
        f"""
      const nextPayload = {json.dumps(next_proj)};
      const container = document.getElementById('wb-cards-container');
      assert.strictEqual(ssrCardsBeforeBoot.length, 2);
      assert.strictEqual(
        container.children[0],
        ssrCardsBeforeBoot[0],
        'Initial client hydration must morph SSR card 1 in place instead of innerHTML teardown'
      );
      assert.strictEqual(
        container.children[1],
        ssrCardsBeforeBoot[1],
        'Initial client hydration must morph SSR card 2 in place instead of innerHTML teardown'
      );
      const bootInnerSets = domStats.innerHTMLSets;

      global.fetch = async () => ({{ok: true, json: async () => nextPayload}});
      env.parentMsgs.length = 0;
      resetDomStats();
      await env.getPollFn()();

      assert.strictEqual(
        document.getElementById('wb-synced').textContent, 'Synced 15:06 ET (just now)'
      );
      assert(env.parentMsgs.some(m => m && m.type === 'jetski-workbench-rendered'));
      console.log(JSON.stringify({{
        bootInnerSets,
        innerHTMLSets: domStats.innerHTMLSets,
        insertions: domStats.insertions,
        removals: domStats.removals,
        replacements: domStats.replacements,
        attrSets: domStats.attrSets,
        textWrites: domStats.textWrites,
      }}));
    """,
    )
    self.assertEqual(res["bootInnerSets"], 0)
    self.assertEqual(res["innerHTMLSets"], 0)
    self.assertEqual(res["insertions"], 0)
    self.assertEqual(res["removals"], 0)
    self.assertEqual(res["replacements"], 0)
    self.assertEqual(res["attrSets"], 0)
    self.assertEqual(res["textWrites"], 1)

  # Verifies single-card updates and top insertions morph in place in grouped Triage and flat modes
  def test_flat_and_grouped_triage_updates_preserve_sibling_cards_and_scroll(self):
    w1 = _make_work("work-1", lane="needs-you", owner="user", title="Main urgent card", ps=1)
    w2 = _make_work(
        "work-2",
        lane="waiting",
        owner="user",
        title="Main waiting card",
        activity_at=1762800100.0,
        ps=1,
    )
    wd = _make_work(
        "work-d",
        lane="waiting",
        owner="draft",
        title="Auto folded draft",
        activity_at=1762800050.0,
        ps=1,
    )
    proj1 = _make_projection([w1, w2, wd])
    w1_ps2 = _make_work("work-1", lane="needs-you", owner="user", title="Main urgent card", ps=2)
    proj2 = _make_projection([w1_ps2, w2, wd], synced_et="15:07 ET")
    w_top = _make_work(
        "work-0",
        lane="needs-you",
        owner="user",
        title="Brand new top card",
        activity_at=1762800900.0,
        ps=1,
    )
    proj3 = _make_projection([w_top, w1_ps2, w2, wd], synced_et="15:08 ET")
    res = _run_workbench_js(
        proj1,
        f"""
      const p2 = {json.dumps(proj2)};
      const p3 = {json.dumps(proj3)};
      const container = document.getElementById('wb-cards-container');
      const card1Before = container.querySelector('[data-work-id="work-1"]');
      const card2Before = container.querySelector('[data-work-id="work-2"]');
      const unownedHdrBefore = container.querySelector('[data-unit-key="wb-sec:unowned-hdr"]');
      const cardDraftBefore = container.querySelector('[data-work-id="work-d"]');
      const dot1Before = card1Before.querySelector('.re-dot');
      assert(card1Before && card2Before && unownedHdrBefore && cardDraftBefore && dot1Before);

      // 1. Update only work-1 (PS1 -> PS2) on background poll
      global.window.scrollY = 220;
      global.fetch = async () => ({{ok: true, json: async () => p2}});
      resetDomStats();
      await env.getPollFn()();

      assert.strictEqual(
        container.querySelector('[data-work-id="work-1"]'),
        card1Before,
        'Updated card must morph in place without recreating <article>'
      );
      assert.strictEqual(
        card1Before.querySelector('.re-dot'),
        dot1Before,
        'Status dot inside morphed card must survive in place'
      );
      assert(card1Before.textContent.includes('PS2'));
      assert.strictEqual(
        container.querySelector('[data-work-id="work-2"]'),
        card2Before,
        'Unchanged sibling card must stay untouched'
      );
      assert.strictEqual(
        container.querySelector('[data-unit-key="wb-sec:unowned-hdr"]'),
        unownedHdrBefore,
        'Open unowned stream header must stay mounted'
      );
      assert.strictEqual(
        container.querySelector('[data-work-id="work-d"]'),
        cardDraftBefore,
        'Open unowned card must stay untouched'
      );
      const singleCardInnerSets = domStats.innerHTMLSets;
      const singleCardScrollRestores = domStats.scrollRestores;

      // 2. Insert brand-new urgent card at the top: existing cards shift down without recreate
      global.fetch = async () => ({{ok: true, json: async () => p3}});
      resetDomStats();
      await env.getPollFn()();

      const mainCards = Array.from(container.children).filter(el => el.tagName === 'ARTICLE');
      assert.strictEqual(mainCards.length, 4);
      assert.strictEqual(mainCards[0].getAttribute('data-work-id'), 'work-0');
      assert.strictEqual(mainCards[1], card1Before, 'Card 1 must shift down intact');
      assert.strictEqual(mainCards[2], card2Before, 'Card 2 must shift down intact');
      assert.strictEqual(mainCards[3], cardDraftBefore, 'Unowned card must shift down intact');
      const topInsertRemovals = domStats.removals;
      const topInsertScrollRestores = domStats.scrollRestores;

      // 3. Switch to flat Activity mode: all cards survive without innerHTML wipe
      const sortSel = document.getElementById('wb-sort-mode');
      sortSel.value = 'activity';
      resetDomStats();
      await sortSel.dispatch('change', {{target: sortSel}});
      assert.strictEqual(container.querySelector('[data-work-id="work-1"]'), card1Before);
      assert.strictEqual(container.querySelector('[data-work-id="work-2"]'), card2Before);
      assert.strictEqual(container.querySelector('[data-work-id="work-d"]'), cardDraftBefore);
      console.log(JSON.stringify({{
        singleCardInnerSets,
        singleCardScrollRestores,
        topInsertRemovals,
        topInsertScrollRestores,
        flatSwitchInnerSets: domStats.innerHTMLSets,
      }}));
    """,
    )
    self.assertEqual(res["singleCardInnerSets"], 0)
    self.assertEqual(res["singleCardScrollRestores"], 0)
    self.assertEqual(res["topInsertRemovals"], 0)
    self.assertEqual(res["topInsertScrollRestores"], 0)
    self.assertEqual(res["flatSwitchInnerSets"], 0)

  # Verifies expanded <details class="wb-mentions"> and unanswered tray rows survive partial updates
  def test_mentions_details_and_tray_reconcile_without_collapsing_state(self):
    tray1 = [
        {
            "artifact_key": "gmail_thread:t1",
            "sender": "Alice",
            "subject": "Thread one",
            "url": "https://mail.google.com/mail/u/0/#inbox/t1",
        },
        {
            "artifact_key": "gmail_thread:t2",
            "sender": "Bob",
            "subject": "Thread two",
            "url": "https://mail.google.com/mail/u/0/#inbox/t2",
        },
    ]
    tray2 = [
        {
            "artifact_key": "gmail_thread:t2",
            "sender": "Bob",
            "subject": "Thread two",
            "url": "https://mail.google.com/mail/u/0/#inbox/t2",
        },
        {
            "artifact_key": "gmail_thread:t3",
            "sender": "Carol",
            "subject": "Thread three",
            "url": "https://mail.google.com/mail/u/0/#inbox/t3",
        },
    ]
    proj1 = _make_projection([_make_work("work-1", mentions=1)], tray=tray1)
    proj2 = _make_projection(
        [_make_work("work-1", mentions=2)], synced_et="15:09 ET", tray=tray2
    )
    res = _run_workbench_js(
        proj1,
        f"""
      const p2 = {json.dumps(proj2)};
      const card = document.getElementById('wb-cards-container').children[0];
      const detailsBefore = card.querySelector('.wb-mentions');
      assert(detailsBefore && detailsBefore.tagName === 'DETAILS');
      detailsBefore.open = true;

      const trayEl = document.getElementById('wb-tray-container');
      const trayRowsBefore = trayEl.querySelectorAll('.wb-tray-row');
      assert.strictEqual(trayRowsBefore.length, 2);
      const bobRowBefore = trayRowsBefore[1];

      global.fetch = async () => ({{ok: true, json: async () => p2}});
      resetDomStats();
      await env.getPollFn()();

      const detailsAfter = card.querySelector('.wb-mentions');
      assert.strictEqual(detailsAfter, detailsBefore, '<details> node must survive mention add');
      assert.strictEqual(detailsAfter.open, true, 'Expanded <details> must remain open');
      assert(detailsAfter.textContent.includes('2 chat mentions'));

      const trayRowsAfter = trayEl.querySelectorAll('.wb-tray-row');
      assert.strictEqual(trayRowsAfter.length, 2);
      assert.strictEqual(
        trayRowsAfter[0], bobRowBefore, 'Surviving tray row must keep DOM identity'
      );
      assert(trayRowsAfter[1].textContent.includes('Carol'));
      console.log(JSON.stringify({{innerHTMLSets: domStats.innerHTMLSets}}));
    """,
    )
    self.assertEqual(res["innerHTMLSets"], 0)

  # Verifies live reply-draft textarea focus/edits and in-flight + Chat button survive poll morphs
  def test_live_reply_draft_editor_and_inflight_start_chat_survive_background_poll(self):
    w_draft = _make_work("work-1", lane="needs-you", title="Draft host card", ps=1)
    w_draft["reply_draft"] = {
        "body": "Initial draft note for 141001",
        "format": "text",
        "auto_gate": True,
        "updated_et": "15:00 ET",
    }
    w_chat = _make_work(
        "work-2", lane="needs-you", title="Chat launch card", activity_at=1762800150.0, ps=1
    )
    proj1 = _make_projection([w_draft, w_chat])

    w_draft_next = json.loads(json.dumps(w_draft))
    w_draft_next["artifacts"].append({
        "artifact_key": "gerrit:skia:99999",
        "kind": "gerrit",
        "role": "primary",
        "title": "Skia 99999 PS1: Follow-up CL",
        "url": "https://skia-review.googlesource.com/c/skia/+/99999",
        "in_draft": False,
        "state": {
            "status": "NEW",
            "subject": "Follow-up CL",
            "patchset": 1,
            "assigned_reviewers_count": 1,
        },
    })
    w_chat_next = _make_work(
        "work-2",
        lane="needs-you",
        title="Chat launch card",
        activity_at=1762800150.0,
        ps=2,
        mentions=1,
    )
    proj2 = _make_projection([w_draft_next, w_chat_next], synced_et="15:10 ET")

    res = _run_workbench_js(
        proj1,
        f"""
      const p2 = {json.dumps(proj2)};
      const container = document.getElementById('wb-cards-container');
      const card1 = container.querySelector('[data-work-id="work-1"]');
      const card2 = container.querySelector('[data-work-id="work-2"]');

      // 1. Open inline reply-draft editor on work-1 and simulate live typing + selection
      const editBtn = card1.querySelector('[data-edit-draft="work-1"]');
      await clickElement(editBtn);
      const taBefore = document.getElementById('wb-draft-ta-work-1');
      const agBefore = document.getElementById('wb-draft-ag-work-1');
      assert(taBefore && agBefore);
      taBefore.focus();
      taBefore.value = 'Unsaved live typing';
      taBefore.setSelectionRange(8, 12);
      agBefore.checked = false;
      const focusCallsBeforePoll = taBefore.focusCalls;

      // 2. Click + Chat on work-2 and hold /api/work/start_chat in flight while pollState fires
      let resolveStartChat = null;
      const startBtn = card2.querySelector('[data-start-chat="work-2"]');
      assert(startBtn);
      global.fetch = (url) => {{
        if (url === '/api/work/start_chat') {{
          return new Promise(resolve => {{
            resolveStartChat = () => resolve({{ok: true, json: async () => p2}});
          }});
        }}
        return Promise.resolve({{ok: true, json: async () => p2}});
      }};
      const startClickPromise = clickElement(startBtn);
      await Promise.resolve();
      assert.strictEqual(startBtn.disabled, true);
      assert.strictEqual(startBtn.textContent, 'Starting…');

      // 3. Run background pollState() while textarea is focused AND + Chat is in flight
      resetDomStats();
      await env.getPollFn()();

      const taAfter = document.getElementById('wb-draft-ta-work-1');
      const agAfter = document.getElementById('wb-draft-ag-work-1');
      assert.strictEqual(taAfter, taBefore, 'Focused <textarea> node must stay mounted in place');
      assert.strictEqual(taAfter.value, 'Unsaved live typing');
      assert.strictEqual(document.activeElement, taBefore);
      assert.strictEqual(taAfter.selectionStart, 8);
      assert.strictEqual(taAfter.selectionEnd, 12);
      assert.strictEqual(
        taAfter.focusCalls,
        focusCallsBeforePoll,
        'In-place morphed textarea must not lose and re-trigger focus()'
      );
      assert.strictEqual(agAfter, agBefore, 'Auto-gate checkbox node must stay mounted in place');
      assert.strictEqual(agAfter.checked, false);
      assert(
        card1.textContent.includes('Not in draft yet: Skia 99999'),
        'Sync strip must insert cleanly above actions without recreating textarea or checkbox'
      );

      assert.strictEqual(
        card2.querySelector('[data-start-chat="work-2"]'),
        startBtn,
        'In-flight + Chat button node must stay mounted'
      );
      assert.strictEqual(startBtn.disabled, true, 'In-flight + Chat must remain disabled on poll');
      assert.strictEqual(startBtn.textContent, 'Starting…');
      assert(card2.textContent.includes('PS2'), 'Other fields on work-2 must still morph');

      // 4. Finish /api/work/start_chat and verify button restores cleanly
      resolveStartChat();
      await startClickPromise;
      assert.strictEqual(startBtn.disabled, false);
      assert.strictEqual(startBtn.textContent, '+ Chat');
      console.log(JSON.stringify({{innerHTMLSets: domStats.innerHTMLSets}}));
    """,
    )
    self.assertEqual(res["innerHTMLSets"], 0)

  # Verifies stub Node DOMs without element trees fall back to innerHTML and scrollTo restore
  def test_stub_dom_fallback_preserves_innerhtml_and_scroll_restore(self):
    proj1 = _make_projection([_make_work("work-1", title="Initial stub title", ps=1)])
    proj2 = _make_projection(
        [_make_work("work-1", title="Updated stub title", ps=2)], synced_et="15:11 ET"
    )
    html = workbench_server.render_workbench_html(proj1, ui_version=7, _already_slim=True)
    script_body = html.split("<script>", 1)[1].split("</script>", 1)[0]
    node_code = (
        r"""
const assert = require('assert');
const stubEls = {};
const scrollLog = [];
let pollFn = null;
let innerWrites = 0;
function makeStub(id) {
  let htmlVal = '';
  return {
    id,
    value: id === 'wb-sort-mode' ? 'triage' : '',
    textContent: '',
    disabled: false,
    get innerHTML() { return htmlVal; },
    set innerHTML(v) {
      htmlVal = String(v ?? '');
      if (id === 'wb-cards-container') innerWrites += 1;
    },
    getAttribute: () => null,
    setAttribute: () => {},
    hasAttribute: () => false,
    addEventListener: () => {},
    reset: () => {},
  };
}
global.localStorage = { getItem: () => null, setItem: () => {} };
global.window = {
  scrollY: 275,
  parent: null,
  location: { reload: () => {} },
  scrollTo: (x, y) => { scrollLog.push([x, y]); global.window.scrollY = y; },
  addEventListener: () => {},
};
global.setInterval = (fn) => { pollFn = fn; return 1; };
global.fetch = async () => ({ ok: true, json: async () => global.__nextProj });
global.document = {
  activeElement: null,
  body: { addEventListener: () => {} },
  getElementById: id => (stubEls[id] || (stubEls[id] = makeStub(id))),
  querySelectorAll: () => [],
  createElement: () => {
    let t = '';
    return {
      set textContent(v) { t = String(v ?? ''); },
      get innerHTML() {
        return t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      },
    };
  },
};
"""
        + f"\nglobal.__nextProj = {json.dumps(proj2)};\n"
        + script_body
        + r"""
(async () => {
  const beforePollWrites = innerWrites;
  await pollFn();
  assert.strictEqual(innerWrites, beforePollWrites + 1);
  assert(stubEls['wb-cards-container'].innerHTML.includes('Updated stub title'));
  assert.deepStrictEqual(scrollLog[scrollLog.length - 1], [0, 275]);
  console.log(JSON.stringify({ok: true}));
})().catch(err => { console.error(err); process.exit(1); });
"""
    )
    proc = subprocess.run(
        ["node", "-"],
        input=node_code,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    self.assertTrue(json.loads(proc.stdout.strip())["ok"])


if __name__ == "__main__":
  unittest.main()
