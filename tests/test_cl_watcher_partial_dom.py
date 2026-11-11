#!/usr/bin/env python3
"""Contract tests for flicker-free partial DOM reconciliation in Flight Deck."""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


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
  constructor(tag) {
    super(1);
    this.tagName = String(tag || 'DIV').toUpperCase();
    this.childNodes = [];
    this._attrs = new Map();
    this.dataset = new Proxy({}, {
      get: (_, prop) => {
        const attr = 'data-' + String(prop).replace(/[A-Z]/g, m => '-' + m.toLowerCase());
        return this._attrs.has(attr) ? this._attrs.get(attr) : undefined;
      },
      set: (_, prop, val) => {
        const attr = 'data-' + String(prop).replace(/[A-Z]/g, m => '-' + m.toLowerCase());
        this.setAttribute(attr, String(val));
        return true;
      },
      deleteProperty: (_, prop) => {
        const attr = 'data-' + String(prop).replace(/[A-Z]/g, m => '-' + m.toLowerCase());
        this.removeAttribute(attr);
        return true;
      }
    });
    this._value = '';
    this._dirtyValue = false;
    this._checked = false;
    this._open = false;
    this._disabled = false;
    this.style = {};
    this.classList = {
      add: (c) => {
        const cur = new Set((this.getAttribute('class') || '').split(/\s+/).filter(Boolean));
        cur.add(c);
        this.setAttribute('class', [...cur].join(' '));
      },
      remove: (c) => {
        const cur = (this.getAttribute('class') || '').split(/\s+/).filter(x => x && x !== c);
        this.setAttribute('class', cur.join(' '));
      },
    };
  }
  get id() { return this._attrs.get('id') || ''; }
  set id(v) { this.setAttribute('id', v); }
  get type() { return this._attrs.get('type') || (this.tagName === 'TEXTAREA' ? 'textarea' : ''); }
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
    domStats.attrSets += 1;
    this._attrs.set(k, s);
    if (k === 'open') this._open = true;
    if (k === 'checked') this._checked = true;
    if (k === 'disabled') this._disabled = true;
    if (k === 'value' && !this._dirtyValue) this._value = s;
  }
  removeAttribute(name) {
    const k = String(name).toLowerCase();
    if (this._attrs.has(k)) {
      domStats.attrRemovals += 1;
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
    domStats.insertions += 1;
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
    domStats.insertions += 1;
    return child;
  }
  removeChild(child, silent = false) {
    const idx = this.childNodes.indexOf(child);
    if (idx >= 0) {
      this.childNodes.splice(idx, 1);
      child.parentNode = null;
      if (!silent) domStats.removals += 1;
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
    domStats.replacements += 1;
    return oldChild;
  }
  get textContent() {
    return this.childNodes.map(n => n.nodeType === 8 ? '' : n.textContent).join('');
  }
  set textContent(v) {
    const s = String(v ?? '');
    if (this.childNodes.length === 1 && this.childNodes[0].nodeType === 3) {
      this.childNodes[0].nodeValue = s;
      return;
    }
    for (const c of this.childNodes) c.parentNode = null;
    this.childNodes = s ? [new MText(s)] : [];
    if (this.childNodes[0]) this.childNodes[0].parentNode = this;
    domStats.textWrites += 1;
  }
  get innerHTML() {
    return this.childNodes.map(serializeNode).join('');
  }
  set innerHTML(html) {
    domStats.innerHTMLSets += 1;
    for (const c of this.childNodes) c.parentNode = null;
    this.childNodes = parseHtmlFragment(String(html ?? ''), this);
  }
  get outerHTML() {
    return serializeNode(this);
  }
  querySelectorAll(sel) {
    const out = [];
    const match = makeMatcher(sel);
    const walk = (el) => {
      for (const ch of el.children) {
        if (match(ch)) out.push(ch);
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

function makeMatcher(sel) {
  const s = sel.trim();
  if (s === '[data-confirming="1"]') {
    return el => el.getAttribute('data-confirming') === '1';
  }
  if (s === 'details[data-details-key]') {
    return el => el.tagName === 'DETAILS' && el.hasAttribute('data-details-key');
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
      const c = new MComment(val);
      stack[stack.length - 1].childNodes.push(c);
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
        const el = new MElement(tag);
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

function setupMiniDom() {
  const slots = new Map();
  const rootBody = new MElement('BODY');
  for (const id of [
    'view-switch', 'rollers', 'synced', 'refresh-btn', 'projects',
    'headline', 'subline', 'focus', 'processing', 'lanes', 'footer',
    'triage-modal-root', 'ping-modal-root'
  ]) {
    const el = new MElement('DIV');
    el.setAttribute('id', id);
    rootBody.appendChild(el);
    slots.set(id, el);
  }
  const docEl = new MElement('HTML');
  docEl.scrollTop = 0;
  docEl.appendChild(rootBody);

  global.window = {
    scrollY: 0,
    parent: null,
    location: {search: '', hash: '', reload: () => {}, assign: () => {}},
    scrollTo: (_x, y) => {
      domStats.scrollRestores += 1;
      global.window.scrollY = y;
    },
    addEventListener: () => {},
  };
  global.location = global.window.location;
  global.history = {replaceState: (_s, _t, h) => { global.location.hash = h; }};
  const memStore = new Map();
  global.localStorage = {
    getItem: k => memStore.has(k) ? memStore.get(k) : null,
    setItem: (k, v) => memStore.set(k, String(v)),
  };
  global.sessionStorage = global.localStorage;
  global.document = {
    documentElement: docEl,
    body: rootBody,
    activeElement: null,
    createElement: tag => new MElement(tag),
    getElementById: id => slots.get(id) || docEl.querySelector('#' + id),
    querySelectorAll: sel => docEl.querySelectorAll(sel),
    querySelector: sel => docEl.querySelector(sel),
    addEventListener: () => {},
  };
  resetDomStats();
  return {slots, docEl};
}
"""


def _run_dashboard_js(test_js: str) -> dict:
  script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
  script_no_boot = script_body.rsplit("\nboot();", 1)[0]
  full_js = (
      "const assert = require('assert');\n"
      + MINI_DOM_PRELUDE
      + "\nsetupMiniDom();\n"
      + script_no_boot
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


class FlightDeckPartialDomTest(unittest.TestCase):

  # Verifies that updated_at bumps on background syncs produce zero content DOM writes
  def test_updated_at_poll_skips_all_content_dom_mutations(self):
    res = _run_dashboard_js(r"""
      const makePayload = (syncTime, updAt) => ({
        ui_version: 101,
        last_sync: syncTime,
        rollers_summary: {},
        bugs: {},
        items: {
          'gerrit:1410001@c1': {
            composite_key: 'gerrit:1410001@c1',
            item_key: 'gerrit:1410001',
            kind: 'gerrit',
            target_id: '1410001',
            project_name: 'SkParagraph',
            subject: '[SkParagraph] Cluster advance',
            url: 'https://skia-review.googlesource.com/c/skia/+/1410001',
            patchset: 1,
            status: 'NEW',
            in_my_attention: true,
            chat_busy: true,
            conversation_id: 'c1',
            updated_at: updAt,
          }
        }
      });
      let payload = makePayload('12:00:00 ET', '2026-11-11 12:00:00 ET');
      global.fetch = async () => ({ok: true, status: 200, json: async () => payload});
      await fetchState();
      const firstCard = document.getElementById('processing').children[0];
      assert(firstCard, 'Expected busy item in #processing');

      resetDomStats();
      let renderCalls = 0;
      const origRender = render;
      render = (...args) => { renderCalls += 1; return origRender(...args); };
      payload = makePayload('12:02:00 ET', '2026-11-11 12:02:00 ET');
      await fetchState();

      assert.strictEqual(renderCalls, 0, 'updated_at bump alone must skip render()');
      assert.strictEqual(document.getElementById('synced').textContent, '12:02:00 ET');
      assert.strictEqual(document.getElementById('processing').children[0], firstCard);
      console.log(JSON.stringify({
        innerHTMLSets: domStats.innerHTMLSets,
        insertions: domStats.insertions,
        removals: domStats.removals,
        replacements: domStats.replacements,
        attrSets: domStats.attrSets,
      }));
    """)
    self.assertEqual(res["innerHTMLSets"], 0)
    self.assertEqual(res["insertions"], 0)
    self.assertEqual(res["removals"], 0)
    self.assertEqual(res["replacements"], 0)
    self.assertEqual(res["attrSets"], 0)

  # Verifies inserting or updating one focus card keeps sibling card nodes and .dot.moving intact
  def test_partial_card_update_and_top_insertion_preserve_sibling_nodes(self):
    res = _run_dashboard_js(r"""
      const cloneData = obj => JSON.parse(JSON.stringify(obj, stripTransientStatusFields));
      const baseData = {
        ui_version: 101,
        last_sync: '12:00 ET',
        jetski_box_url: 'https://jetski.example',
        rollers_summary: {
          Android: {num_failed: 1, badge: 'red', num_behind: 2, url: 'https://r/a'}
        },
        bugs: {},
        items: {
          'gerrit:1001@c1': {
            composite_key: 'gerrit:1001@c1',
            item_key: 'gerrit:1001',
            kind: 'gerrit',
            target_id: '1001',
            project_name: 'SkParagraph',
            subject: 'First card',
            url: 'https://skia-review.googlesource.com/c/skia/+/1001',
            patchset: 1,
            status: 'NEW',
            in_my_attention: true,
            last_msg_author: 'reviewer@google.com',
            last_msg_snippet: 'Looks close',
            last_msg_age: '5m ago',
            activity_ts: 200,
          },
          'gerrit:1002@c2': {
            composite_key: 'gerrit:1002@c2',
            item_key: 'gerrit:1002',
            kind: 'gerrit',
            target_id: '1002',
            project_name: 'SkParagraph',
            subject: 'Second card',
            url: 'https://skia-review.googlesource.com/c/skia/+/1002',
            patchset: 2,
            status: 'NEW',
            in_my_attention: true,
            last_msg_author: 'other@google.com',
            last_msg_snippet: 'Question on run',
            last_msg_age: '10m ago',
            activity_ts: 100,
          }
        }
      };
      cachedData = baseData;
      hasLiveStatus = true;
      pendingBootScrollY = 180;
      render();
      assert.strictEqual(
        domStats.scrollRestores, 1, 'Initial boot render must restore pendingBootScrollY once'
      );
      assert.strictEqual(global.window.scrollY, 180);

      const focusEl = document.getElementById('focus');
      assert.strictEqual(focusEl.children.length, 2);
      const card1Before = focusEl.children[0];
      const card2Before = focusEl.children[1];
      const rollerDotBefore = document.getElementById('rollers').querySelector('.moving');
      assert(rollerDotBefore, 'Expected .dot.moving in #rollers');

      // Mutate only card 1 age ("5m ago" -> "6m ago") and re-render
      const next1 = cloneData(baseData);
      next1.items['gerrit:1001@c1'].last_msg_age = '6m ago';
      cachedData = next1;
      resetDomStats();
      render();

      assert.strictEqual(focusEl.children[0], card1Before, 'Modified card must morph in place');
      assert.strictEqual(
        focusEl.children[1], card2Before, 'Unchanged sibling card must stay untouched'
      );
      assert(card1Before.textContent.includes('6m ago'));
      assert.strictEqual(
        document.getElementById('rollers').querySelector('.moving'), rollerDotBefore
      );
      const morphRemovals = domStats.removals;
      const morphReplacements = domStats.replacements;

      // Insert a brand-new card at the top (activity_ts: 300) pushing card1 and card2 down
      const next2 = cloneData(next1);
      next2.items['gerrit:1003@c3'] = {
        composite_key: 'gerrit:1003@c3',
        item_key: 'gerrit:1003',
        kind: 'gerrit',
        target_id: '1003',
        project_name: 'SkParagraph',
        subject: 'Urgent top card',
        url: 'https://skia-review.googlesource.com/c/skia/+/1003',
        patchset: 1,
        status: 'NEW',
        in_my_attention: true,
        activity_ts: 300,
      };
      global.window.scrollY = 240;
      cachedData = next2;
      resetDomStats();
      render();

      assert.strictEqual(focusEl.children.length, 3);
      assert(focusEl.children[0].textContent.includes('Urgent top card'));
      assert.strictEqual(
        focusEl.children[1], card1Before, 'Card 1 must shift down without recreate'
      );
      assert.strictEqual(
        focusEl.children[2], card2Before, 'Card 2 must shift down without recreate'
      );
      console.log(JSON.stringify({
        morphRemovals,
        morphReplacements,
        scrollRestoresOnIncremental: domStats.scrollRestores,
      }));
    """)
    self.assertEqual(res["morphRemovals"], 0)
    self.assertEqual(res["morphReplacements"], 0)
    self.assertEqual(res["scrollRestoresOnIncremental"], 0)

  # Verifies processing strip elapsed-minute ticks morph text without resetting <details> or .moving
  def test_processing_strip_minute_tick_preserves_details_and_moving_dot(self):
    res = _run_dashboard_js(r"""
      const t0 = Date.now() - 4 * 60000;
      const data1 = {
        ui_version: 101,
        last_sync: '12:00 ET',
        jetski_box_url: 'https://jetski.example',
        items: {
          'gerrit:2001@c9': {
            composite_key: 'gerrit:2001@c9',
            item_key: 'gerrit:2001',
            kind: 'gerrit',
            target_id: '2001',
            project_name: 'SkParagraph',
            subject: 'Busy review item',
            title: '[11:55] Gerrit 2001 audit',
            url: 'https://skia-review.googlesource.com/c/skia/+/2001',
            patchset: 1,
            status: 'NEW',
            in_my_attention: true,
            chat_busy: true,
            conversation_id: 'c9',
            chat_last_modified: new Date(t0).toISOString(),
          }
        },
        bugs: {},
      };
      cachedData = data1;
      hasLiveStatus = true;
      render();

      const procEl = document.getElementById('processing');
      const detailsBefore = procEl.children[0];
      assert.strictEqual(detailsBefore.tagName, 'DETAILS');
      const dotBefore = detailsBefore.querySelector('.moving');
      assert(dotBefore, 'Expected .dot.moving inside expanded processing card');

      // User expands the processing item
      detailsBefore.open = true;
      openDetailsKeys.add(detailsBefore.getAttribute('data-details-key'));

      // Next poll: 1 minute elapsed (4 min -> 5 min)
      const data2 = JSON.parse(JSON.stringify(data1, stripTransientStatusFields));
      data2.items['gerrit:2001@c9'].chat_last_modified = new Date(t0 - 60000).toISOString();
      cachedData = data2;
      resetDomStats();
      render();

      assert.strictEqual(
        procEl.children[0], detailsBefore, '<details> node must survive minute tick'
      );
      assert.strictEqual(detailsBefore.open, true, 'Expanded <details> state must stay open');
      assert.strictEqual(
        detailsBefore.querySelector('.moving'), dotBefore, '.dot.moving must not reset'
      );
      assert(detailsBefore.textContent.includes('(5 min)'));
      console.log(JSON.stringify({
        removals: domStats.removals,
        replacements: domStats.replacements,
      }));
    """)
    self.assertEqual(res["removals"], 0)
    self.assertEqual(res["replacements"], 0)

  # Verifies user edits inside security-merge <textarea> survive sibling and same-card updates
  def test_security_merge_textarea_preserves_user_draft_across_partial_morph(self):
    res = _run_dashboard_js(r"""
      const makeBugData = (soakDays) => ({
        ui_version: 101,
        last_sync: '12:00 ET',
        items: {},
        bugs: {
          'buganizer:5001': {
            bug_key: 'buganizer:5001',
            id: '5001',
            short_id: 'crbug/5001',
            title: '[Merge M132] Fix paragraph crash',
            project_name: 'Skia',
            priority: 'P1',
            status: 'ASSIGNED',
            lane: 'you',
            is_security_merge_request: true,
            parent_issue_id: '4999',
            merge_milestone: 132,
            url: 'https://issues.chromium.org/issues/5001',
          }
        },
        merge_request_groups: {
          '4999': {
            group_key: 'merge_group:4999',
            parent_id: '4999',
            parent_short_id: 'crbug/4999',
            parent_title: 'Fix paragraph crash',
            parent_url: 'https://issues.chromium.org/issues/4999',
            project_name: 'Skia',
            priority: 'P1',
            milestones_label: 'M132',
            hard_gates_ok: true,
            comment_draft: `Server default (${soakDays}d soak)`,
            requests: [{
              id: '5001',
              short_id: 'crbug/5001',
              milestone: 132,
              url: 'https://issues.chromium.org/issues/5001',
            }],
            gates: {canary_soak: {ok: true, soak_days: soakDays, canary_version: '134.0.1.0'}},
          }
        },
      });
      selectedView = 'bugs';
      cachedData = makeBugData(2);
      hasLiveStatus = true;
      render();

      const ta = document.getElementById('merge-draft-4999');
      assert(ta, 'Expected merge draft textarea');
      ta.value = 'Custom user-edited justification text';

      // Soak days advances 2d -> 3d on the SAME card while textarea is blurred
      cachedData = makeBugData(3);
      render();

      const taAfter = document.getElementById('merge-draft-4999');
      assert.strictEqual(taAfter, ta, 'Textarea node must not be recreated');
      assert.strictEqual(taAfter.value, 'Custom user-edited justification text');
      assert(document.getElementById('focus').textContent.includes('3d soak'));
      console.log(JSON.stringify({ok: true}));
    """)
    self.assertTrue(res["ok"])

  # Verifies stub DOMs without element trees fall back to innerHTML and preserve scrollY
  def test_stub_dom_without_element_tree_falls_back_to_inner_html(self):
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.rsplit("\nboot();", 1)[0] + r"""
const stubEls = {};
const scrollLog = [];
global.window = {
  scrollY: 360,
  scrollTo: (_x, y) => { scrollLog.push(y); global.window.scrollY = y; },
};
global.document = {
  documentElement: {scrollTop: 360},
  getElementById: id => {
    if (!stubEls[id]) stubEls[id] = {innerHTML: '', textContent: ''};
    return stubEls[id];
  },
  querySelectorAll: () => [],
  querySelector: () => null,
};
cachedData = {
  items: {
    'gerrit:3001@c1': {
      item_key: 'gerrit:3001',
      kind: 'gerrit',
      target_id: '3001',
      project_name: 'Skia',
      subject: 'Stub fallback check',
      url: 'https://skia-review.googlesource.com/c/skia/+/3001',
      patchset: 1,
      status: 'NEW',
      in_my_attention: true,
    }
  },
  bugs: {},
};
render();
console.log(JSON.stringify({
  hasSubject: stubEls['focus'].innerHTML.includes('Stub fallback check'),
  scrollLog,
}));
"""
    proc = subprocess.run(
        ["node", "-"],
        input=harness,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    out = json.loads(proc.stdout.strip())
    self.assertTrue(out["hasSubject"])
    self.assertEqual(out["scrollLog"], [360])

  # Verifies R94 edge contracts: armed confirm navigation, scroll priority, multi-chat proc, focus
  def test_reconciler_edge_invariants_confirm_scroll_proc_and_focus(self):
    res = _run_dashboard_js(r"""
      // B3 guard: non-tag '<' tokens in parseHtmlFragment terminate cleanly
      const stray = document.createElement('div');
      stray.innerHTML = '<span>ok</span><!x><?pi><2>';
      assert.strictEqual(stray.children.length, 1);

      // B2 guard: live scrollY beats stale pendingBootScrollY, and scrollY=0 persists '0'
      localStorage.setItem(SCROLL_Y_STORAGE_KEY, '80');
      pendingBootScrollY = 80;
      global.window.scrollY = 260;
      document.documentElement.scrollTop = 260;
      cachedData = {ui_version: 101, last_sync: '12:00 ET', items: {}, bugs: {}};
      hasLiveStatus = true;
      render();
      assert.strictEqual(global.window.scrollY, 260, 'Live scrollY must beat pendingBootScrollY');
      assert.strictEqual(sessionStorage.getItem(SCROLL_Y_STORAGE_KEY), '260');
      global.window.scrollY = 0;
      document.documentElement.scrollTop = 0;
      render();
      assert.strictEqual(sessionStorage.getItem(SCROLL_Y_STORAGE_KEY), '0');

      // B1 guard: project switch while a focus card has [data-confirming="1"] never drops siblings
      cachedData = {
        ui_version: 101,
        last_sync: '12:01 ET',
        items: {
          'gerrit:4001@c1': {
            composite_key: 'gerrit:4001@c1',
            item_key: 'gerrit:4001',
            kind: 'gerrit',
            target_id: '4001',
            project_name: 'SkParagraph',
            subject: 'First sibling card',
            url: 'https://skia-review.googlesource.com/c/skia/+/4001',
            patchset: 1,
            status: 'NEW',
            in_my_attention: true,
            activity_ts: 200,
          },
          'gerrit:4002@c2': {
            composite_key: 'gerrit:4002@c2',
            item_key: 'gerrit:4002',
            kind: 'gerrit',
            target_id: '4002',
            project_name: 'Skia',
            subject: 'Second armed card',
            url: 'https://skia-review.googlesource.com/c/skia/+/4002',
            patchset: 1,
            status: 'NEW',
            in_my_attention: true,
            activity_ts: 100,
            companions: [{
              item_key: 'gerrit:9402', label: 'CL 9402', status: 'NEW', ready_to_abandon: true
            }],
          },
        },
        bugs: {},
      };
      render();
      const focusEl = document.getElementById('focus');
      assert.strictEqual(focusEl.children.length, 2);
      const fakeBtn = focusEl.children[1].querySelector('button') || focusEl.children[1];
      await abandonCompanions(['gerrit:9402'], fakeBtn);
      assert.strictEqual(fakeBtn.dataset.confirming, '1');
      setProject('SkParagraph');
      assert.strictEqual(focusEl.children.length, 1);
      setProject('ALL');
      assert.strictEqual(focusEl.children.length, 2, 'Both cards must remain mounted in ALL');

      // B4 guard: two busy chats on the same CL keep distinct DOM nodes in #processing
      cachedData = {
        ui_version: 101,
        last_sync: '12:02 ET',
        items: {
          'gerrit:5001@c1': {
            composite_key: 'gerrit:5001@c1',
            item_key: 'gerrit:5001',
            kind: 'gerrit',
            target_id: '5001',
            project_name: 'Skia',
            subject: 'Shared CL',
            url: 'https://skia-review.googlesource.com/c/skia/+/5001',
            patchset: 1,
            status: 'NEW',
            in_my_attention: true,
            chat_busy: true,
            conversation_id: 'c1',
            activity_ts: 100,
          },
        },
        bugs: {},
      };
      render();
      const procEl = document.getElementById('processing');
      const detC1 = procEl.children[0];
      const dotC1 = detC1.querySelector('.moving');
      cachedData = JSON.parse(JSON.stringify(cachedData, stripTransientStatusFields));
      cachedData.items['gerrit:5001@c2'] = {
        composite_key: 'gerrit:5001@c2',
        item_key: 'gerrit:5001',
        kind: 'gerrit',
        target_id: '5001',
        project_name: 'Skia',
        subject: 'Shared CL',
        url: 'https://skia-review.googlesource.com/c/skia/+/5001',
        patchset: 1,
        status: 'NEW',
        in_my_attention: true,
        chat_busy: true,
        conversation_id: 'c2',
        activity_ts: 200,
      };
      render();
      assert.strictEqual(procEl.children.length, 2);
      assert.strictEqual(procEl.children[1], detC1, 'Existing busy chat node must shift intact');
      assert.strictEqual(detC1.querySelector('.moving'), dotC1);

      // B5 guard: focused unedited textarea still receives server default once blurred
      selectedView = 'bugs';
      const bugDraftPayload = (draftTxt) => ({
        ui_version: 101,
        last_sync: '12:03 ET',
        items: {},
        bugs: {
          'buganizer:6001': {
            bug_key: 'buganizer:6001',
            id: '6001',
            short_id: 'crbug/6001',
            title: '[Merge M133] Fix crash',
            project_name: 'Skia',
            priority: 'P1',
            status: 'ASSIGNED',
            lane: 'you',
            is_security_merge_request: true,
            parent_issue_id: '6000',
            merge_milestone: 133,
            url: 'https://issues.chromium.org/issues/6001',
          },
        },
        merge_request_groups: {
          '6000': {
            group_key: 'merge_group:6000',
            parent_id: '6000',
            parent_short_id: 'crbug/6000',
            parent_title: 'Fix crash',
            parent_url: 'https://issues.chromium.org/issues/6000',
            project_name: 'Skia',
            priority: 'P1',
            milestones_label: 'M133',
            hard_gates_ok: true,
            comment_draft: draftTxt,
            requests: [{
              id: '6001', short_id: 'crbug/6001', milestone: 133,
              url: 'https://issues.chromium.org/issues/6001',
            }],
            gates: {canary_soak: {ok: true, soak_days: 3, canary_version: '135.0.0.1'}},
          },
        },
      });
      cachedData = bugDraftPayload('Draft v1');
      render();
      const ta = document.getElementById('merge-draft-6000');
      document.activeElement = ta;
      cachedData = bugDraftPayload('Draft v2');
      render();
      assert.strictEqual(ta.value, 'Draft v1', 'Focused textarea must not change mid-focus');
      document.activeElement = null;
      render(true);
      assert.strictEqual(ta.value, 'Draft v2', 'Blurred unedited textarea must adopt Draft v2');
      console.log(JSON.stringify({edgeInvariantsOk: true}));
    """)
    self.assertTrue(res["edgeInvariantsOk"])


if __name__ == "__main__":
  unittest.main()
