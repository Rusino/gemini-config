#!/usr/bin/env python3
"""Workbench R30 phase 2 contracts: text mentions become suggestions on her works."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKBENCH_DIR = os.path.join(CONFIG_DIR, "workbench")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (WORKBENCH_DIR, TESTS_DIR):
  if _p not in sys.path:
    sys.path.insert(0, _p)

import ingestor  # pylint: disable=g-import-not-at-top
import test_workbench_card_ui as card_ui  # pylint: disable=g-import-not-at-top
import work_store  # pylint: disable=g-import-not-at-top
import workbench_server  # pylint: disable=g-import-not-at-top

NOW = 1_760_000_000.0
CL_KEY = "gerrit:skia:1414016"
BUG_KEY = "buganizer:389111535"
DOC_KEY = "gdoc:abc123"

# Every auto link_reason seen in the live DB, minus "manual" and the legacy empty reason
LIVE_AUTO_REASONS = (
    "bug_trailer", "chat_mention", "cl_watcher_chat", "gmail_thread_ref", "chat_topic",
    "gmail_root", "cl_watcher", "gmail_draft_ref", "gdoc_comment", "stack_child",
    "companion_wip", "gmail_digest_ref", "github_issue_thread", "github_pr_thread",
    "gdoc_share",
)


def _q(store: work_store.WorkStore, sql: str, args: tuple = ()) -> list[tuple]:
  """Runs a read query straight on SQLite and returns plain tuples."""
  with store._connect() as conn:  # pylint: disable=protected-access
    return [tuple(r) for r in conn.execute(sql, args).fetchall()]


def _links(store: work_store.WorkStore, wid: str) -> dict[str, tuple]:
  """Returns {artifact_key: (role, source, link_reason)} for wid."""
  rows = _q(
      store,
      "SELECT artifact_key, role, source, link_reason FROM work_links WHERE work_id = ?",
      (wid,),
  )
  return {r[0]: (r[1], r[2], r[3]) for r in rows}


def _suggestions(store: work_store.WorkStore, wid: str) -> dict[str, tuple]:
  """Returns {artifact_key: (link_reason, status)} from link_suggestions for wid."""
  rows = _q(
      store,
      "SELECT artifact_key, link_reason, status FROM link_suggestions WHERE work_id = ?",
      (wid,),
  )
  return {r[0]: (r[1], r[2]) for r in rows}


def _card(store: work_store.WorkStore, wid: str) -> dict:
  """Returns the projected dashboard card for wid."""
  proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=NOW)
  return next(w for w in proj["works"] if w["work_id"] == wid)


class SuggestionStoreTest(unittest.TestCase):
  """Verifies reason classes, suggestion routing, accept/reject, and merge/absorb guards."""

  def setUp(self) -> None:
    self._tmp = tempfile.TemporaryDirectory()
    self.db_path = os.path.join(self._tmp.name, "workbench.db")
    self.store = work_store.WorkStore(self.db_path)
    self.store.upsert_artifact(CL_KEY, kind="gerrit", title="Fix rects", now=NOW)
    self.store.upsert_artifact(BUG_KEY, kind="buganizer", title="b/389111535", now=NOW)
    self.store.upsert_artifact(DOC_KEY, kind="gdoc", title="Design doc", now=NOW)
    self.w_user = self.store.create_work("Mine", work_id="work-mine", owner="user", now=NOW)

  def tearDown(self) -> None:
    self._tmp.cleanup()

  # Each live auto reason is either structural or a mention, never both, so routing is total
  def test_reason_classes_cover_live_reasons(self) -> None:
    structural = set(work_store.STRUCTURAL_LINK_REASONS)
    mention = set(work_store.MENTION_LINK_REASONS)
    self.assertFalse(structural & mention)
    for reason in LIVE_AUTO_REASONS:
      self.assertIn(reason, structural | mention, reason)
    self.assertIn("gmail_thread_ref", mention)
    self.assertIn("bug_trailer", structural)

  # A suggestion goes stale once the same artifact links structurally: the card must not offer
  # it, and a late ✓ / ✕ must not rewrite or tombstone the live structural edge
  def test_stale_suggestion_hidden_and_inert(self) -> None:
    self.store.link_artifact(self.w_user, CL_KEY, role="primary", link_reason="chat_mention")
    self.store.link_artifact(self.w_user, CL_KEY, role="primary", link_reason="cl_watcher")
    self.assertEqual(_card(self.store, self.w_user)["suggestions"], [])
    self.assertEqual(_card(self.store, self.w_user)["suggestions_total"], 0)
    self.store.accept_link_suggestion(self.w_user, CL_KEY, now=NOW + 3)
    self.store.reject_link_suggestion(self.w_user, CL_KEY, now=NOW + 4)
    self.assertEqual(_links(self.store, self.w_user)[CL_KEY], ("primary", "auto", "cl_watcher"))
    self.assertEqual(
        _q(self.store, "SELECT COUNT(*) FROM negative_links WHERE work_id = ?", (self.w_user,)),
        [(0,)],
    )

  # ✓ on a row she already rejected (stale render or direct POST) must not undo the reject
  def test_accept_after_reject_is_ignored(self) -> None:
    self.store.link_artifact(self.w_user, CL_KEY, role="primary", link_reason="gmail_thread_ref")
    self.store.reject_link_suggestion(self.w_user, CL_KEY, now=NOW + 2)
    self.store.accept_link_suggestion(self.w_user, CL_KEY, now=NOW + 3)
    self.assertNotIn(CL_KEY, _links(self.store, self.w_user))
    self.assertEqual(_suggestions(self.store, self.w_user)[CL_KEY][1], "rejected")

  # Merging a work into her work carries its open suggestions instead of dropping them
  def test_merge_moves_open_suggestions_to_target(self) -> None:
    w_other = self.store.create_work("Other mine", work_id="work-other", owner="user", now=NOW)
    self.store.link_artifact(w_other, DOC_KEY, role="primary", link_reason="gmail_thread_ref")
    self.store.merge_works(self.w_user, w_other, now=NOW + 5)
    self.assertEqual(
        _suggestions(self.store, self.w_user), {DOC_KEY: ("gmail_thread_ref", "open")}
    )

  # A bot reviewlog mail about a CL already in the work is its notification channel, not a
  # text mention, so it must keep auto-linking into her works
  def test_reviewlog_channel_is_structural(self) -> None:
    self.assertIn("gmail_reviewlog", work_store.STRUCTURAL_LINK_REASONS)
    self.assertIn('link_reason="gmail_reviewlog"', inspect.getsource(ingestor))
    self.store.link_artifact(
        self.w_user, "gmail_thread:th_bot", role="channel", link_reason="gmail_reviewlog"
    )
    self.assertEqual(_links(self.store, self.w_user)["gmail_thread:th_bot"][0], "channel")

  # A new mention edge into her work is parked as an open suggestion, not a work_links row
  def test_mention_into_user_work_becomes_suggestion(self) -> None:
    role = self.store.link_artifact(
        self.w_user, CL_KEY, role="primary", link_reason="gmail_thread_ref", now=NOW + 1
    )
    self.assertIsNone(role)
    self.assertNotIn(CL_KEY, _links(self.store, self.w_user))
    self.assertEqual(_suggestions(self.store, self.w_user), {CL_KEY: ("gmail_thread_ref", "open")})

  # Re-suggesting is a no-op: no updated_at bump, no new events, created_at kept
  def test_resuggest_is_noop(self) -> None:
    self.store.link_artifact(
        self.w_user, CL_KEY, role="primary", link_reason="gmail_draft_ref", now=NOW + 1
    )
    before_upd = _q(self.store, "SELECT updated_at FROM works WHERE work_id = ?", (self.w_user,))
    before_ev = _q(self.store, "SELECT COUNT(*) FROM events")
    before_sug = _q(self.store, "SELECT * FROM link_suggestions")
    self.store.link_artifact(
        self.w_user, CL_KEY, role="context_only", link_reason="chat_mention", now=NOW + 50
    )
    self.assertEqual(
        _q(self.store, "SELECT updated_at FROM works WHERE work_id = ?", (self.w_user,)),
        before_upd,
    )
    self.assertEqual(_q(self.store, "SELECT COUNT(*) FROM events"), before_ev)
    self.assertEqual(_q(self.store, "SELECT * FROM link_suggestions"), before_sug)

  # Structural reasons keep auto-linking into her work exactly as before
  def test_structural_reason_still_links_user_work(self) -> None:
    self.store.link_artifact(self.w_user, BUG_KEY, role="primary", link_reason="bug_trailer")
    self.assertEqual(_links(self.store, self.w_user)[BUG_KEY][0], "primary")
    self.assertEqual(_suggestions(self.store, self.w_user), {})

  # Drafts keep today's behavior: mentions link directly and nothing is suggested
  def test_draft_work_mentions_link_as_today(self) -> None:
    w_draft = self.store.create_work("Auto", now=NOW)
    self.store.link_artifact(w_draft, CL_KEY, role="primary", link_reason="gmail_thread_ref")
    self.assertEqual(_links(self.store, w_draft)[CL_KEY], ("primary", "auto", "gmail_thread_ref"))
    self.assertEqual(_suggestions(self.store, w_draft), {})

  # An edge that already exists stays put on a later mention pass (no churn, R24)
  def test_existing_edge_untouched_by_mention(self) -> None:
    self.store.link_artifact(self.w_user, CL_KEY, role="primary", link_reason="cl_watcher")
    self.store.link_artifact(
        self.w_user, CL_KEY, role="context_only", link_reason="chat_mention", now=NOW + 5
    )
    self.assertEqual(_links(self.store, self.w_user)[CL_KEY], ("primary", "auto", "cl_watcher"))
    self.assertEqual(_suggestions(self.store, self.w_user), {})

  # A tombstoned artifact is never offered again on that work
  def test_negative_link_never_suggested(self) -> None:
    self.store.link_artifact(self.w_user, CL_KEY, role="primary", source="manual")
    self.store.unlink_artifact(self.w_user, CL_KEY, now=NOW + 1)
    self.store.link_artifact(self.w_user, CL_KEY, role="primary", link_reason="gmail_thread_ref")
    self.assertEqual(_suggestions(self.store, self.w_user), {})

  # Accept links manually with the reason's natural role; reject tombstones and stays quiet
  def test_accept_and_reject_suggestions(self) -> None:
    self.store.link_artifact(self.w_user, CL_KEY, role="primary", link_reason="gmail_draft_ref")
    self.store.link_artifact(self.w_user, DOC_KEY, role="primary", link_reason="chat_mention")
    self.store.link_artifact(self.w_user, BUG_KEY, role="primary", link_reason="gmail_digest_ref")
    self.store.accept_link_suggestion(self.w_user, CL_KEY, now=NOW + 2)
    self.store.accept_link_suggestion(self.w_user, DOC_KEY, now=NOW + 2)
    self.store.reject_link_suggestion(self.w_user, BUG_KEY, now=NOW + 2)
    links = _links(self.store, self.w_user)
    self.assertEqual(links[CL_KEY][:2], ("primary", "manual"))
    self.assertEqual(links[DOC_KEY][:2], ("context_only", "manual"))
    self.assertNotIn(BUG_KEY, links)
    sug = _suggestions(self.store, self.w_user)
    self.assertEqual(sug[CL_KEY][1], "accepted")
    self.assertEqual(sug[DOC_KEY][1], "accepted")
    self.assertEqual(sug[BUG_KEY][1], "rejected")
    self.assertEqual(
        _q(
            self.store,
            "SELECT COUNT(*) FROM negative_links WHERE work_id = ? AND artifact_key = ?",
            (self.w_user, BUG_KEY),
        ),
        [(1,)],
    )
    self.store.link_artifact(self.w_user, BUG_KEY, role="primary", link_reason="gmail_thread_ref")
    self.assertEqual(_suggestions(self.store, self.w_user)[BUG_KEY][1], "rejected")
    self.assertNotIn(BUG_KEY, _links(self.store, self.w_user))

  # Her card lists open suggestions with title/url (capped at 10); drafts carry none
  def test_card_payload_suggestions(self) -> None:
    self.store.link_artifact(self.w_user, CL_KEY, role="primary", source="manual")
    self.store.link_artifact(self.w_user, DOC_KEY, role="primary", link_reason="gmail_thread_ref")
    for i in range(12):
      key = f"gerrit:skia:90000{i:02d}"
      self.store.upsert_artifact(key, kind="gerrit", title=f"CL {i}", now=NOW)
      self.store.link_artifact(self.w_user, key, role="context_only", link_reason="chat_mention")
    self.store.reject_link_suggestion(self.w_user, "gerrit:skia:9000000")
    card = _card(self.store, self.w_user)
    sugs = card["suggestions"]
    self.assertEqual(len(sugs), 10)
    # The header counts every open suggestion, not just the capped list
    self.assertEqual(card["suggestions_total"], 12)
    self.assertNotIn("gerrit:skia:9000000", [s["artifact_key"] for s in sugs])
    doc = next(s for s in sugs if s["artifact_key"] == DOC_KEY)
    self.assertEqual(doc["title"], "Design doc")
    self.assertEqual(doc["link_reason"], "gmail_thread_ref")
    self.assertIn("url", doc)
    self.assertEqual(set(doc), {"artifact_key", "title", "url", "link_reason"})
    w_draft = self.store.create_work("Auto", now=NOW)
    self.store.link_artifact(w_draft, BUG_KEY, role="primary", link_reason="bug_trailer")
    self.assertEqual(_card(self.store, w_draft).get("suggestions", []), [])

  # merge_works into a draft must not hand it a primary that another of her works holds
  def test_merge_into_draft_demotes_primary_held_by_user_work(self) -> None:
    self.store.link_artifact(self.w_user, CL_KEY, role="primary", source="manual")
    w_src = self.store.create_work("Src draft", now=NOW)
    w_dst = self.store.create_work("Dst draft", now=NOW)
    # Src got its primary before her work existed in this shape; write it directly
    with self.store._connect() as conn:  # pylint: disable=protected-access
      conn.execute(
          "INSERT INTO work_links (work_id, artifact_key, role, source, created_at, link_reason) "
          "VALUES (?, ?, 'primary', 'auto', ?, 'gmail_thread_ref')",
          (w_src, CL_KEY, NOW),
      )
    self.store.link_artifact(w_src, BUG_KEY, role="primary", link_reason="bug_trailer")
    self.store.merge_works(w_dst, w_src, source="auto", now=NOW + 3)
    links = _links(self.store, w_dst)
    self.assertEqual(links[CL_KEY][0], "context_only")
    self.assertEqual(links[BUG_KEY][0], "primary")

  # Auto duplicate absorption may skip her work but never fold it into a draft
  def test_absorption_never_absorbs_user_work(self) -> None:
    w_dup = self.store.create_work("Dup draft", now=NOW)
    self.store.link_artifact(w_dup, CL_KEY, role="primary", link_reason="cl_watcher")
    w_mine = self.store.create_work("Mine dup", work_id="work-mine2", owner="user", now=NOW)
    self.store.link_artifact(w_mine, BUG_KEY, role="primary", link_reason="bug_trailer")
    with self.store._connect() as conn:  # pylint: disable=protected-access
      conn.execute(
          "INSERT INTO work_links (work_id, artifact_key, role, source, created_at, link_reason) "
          "VALUES (?, ?, 'primary', 'auto', ?, 'cl_watcher')",
          (w_mine, CL_KEY, NOW),
      )
      conn.execute("DELETE FROM work_links WHERE work_id = ? AND artifact_key = ?",
                   (w_mine, BUG_KEY))
    snap = self.store._load_duplicate_merge_snapshot()  # pylint: disable=protected-access
    rows = {str(r["work_id"]): r for r in snap.work_rows}
    keys = {CL_KEY}
    absorbable = work_store._is_absorbable_duplicate_work  # pylint: disable=protected-access
    self.assertTrue(absorbable(rows[w_dup], keys, keys, keys, set(), snap))
    self.assertFalse(absorbable(rows[w_mine], keys, keys, keys, set(), snap))


class SuggestionHttpAndUiTest(unittest.TestCase):
  """Verifies the suggestion endpoints and the Предложено row on her cards."""

  def setUp(self) -> None:
    self._tmp = tempfile.TemporaryDirectory()
    self.store = work_store.WorkStore(os.path.join(self._tmp.name, "workbench.db"))
    self.store.upsert_artifact(CL_KEY, kind="gerrit", title="Fix rects", now=NOW)
    self.store.upsert_artifact(BUG_KEY, kind="buganizer", title="b/389111535", now=NOW)
    self.w_user = self.store.create_work("Mine", work_id="work-mine", owner="user", now=NOW)

  def tearDown(self) -> None:
    self._tmp.cleanup()

  # POST accept/reject flip suggestion status and return a fresh projection
  def test_http_accept_and_reject(self) -> None:
    self.store.link_artifact(self.w_user, CL_KEY, role="primary", link_reason="gmail_thread_ref")
    self.store.link_artifact(self.w_user, BUG_KEY, role="primary", link_reason="chat_mention")
    cl_state = os.path.join(self._tmp.name, "cl.json")
    with open(cl_state, "w", encoding="utf-8") as f:
      json.dump({"items": {}}, f)
    srv = workbench_server.create_server(
        host="127.0.0.1",
        port=0,
        store=self.store,
        cl_state_path=cl_state,
        watcher_state_path=os.path.join(self._tmp.name, "watcher.json"),
        info_path=os.path.join(self._tmp.name, "info.json"),
    )
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def _post(path: str, payload: dict) -> dict:
      req = urllib.request.Request(
          f"http://127.0.0.1:{port}{path}",
          data=json.dumps(payload).encode("utf-8"),
          headers={"Content-Type": "application/json"},
          method="POST",
      )
      with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

    try:
      _post("/api/work/suggestion/accept", {"work_id": self.w_user, "artifact_key": CL_KEY})
      proj = _post(
          "/api/work/suggestion/reject", {"work_id": self.w_user, "artifact_key": BUG_KEY}
      )
      card = next(w for w in proj["works"] if w["work_id"] == self.w_user)
      self.assertEqual(card["suggestions"], [])
      self.assertEqual(_links(self.store, self.w_user)[CL_KEY][:2], ("primary", "manual"))
      self.assertEqual(_suggestions(self.store, self.w_user)[BUG_KEY][1], "rejected")
    finally:
      srv.shutdown()
      srv.server_close()

  # The card shows Предложено (N) with escaped links and ✓ / ✕ buttons that post the right API
  def test_js_renders_suggestions_row(self) -> None:
    w = {
        "work_id": "work-u",
        "title": "Mine",
        "lane": "waiting",
        "status": "active",
        "owner": "user",
        "reasons": [],
        "artifacts": [],
        "obligations": [],
        "checklist": [],
        "is_virtual": False,
        "work_types": [],
        "needs_you_types": [],
        "activity_at": NOW,
        "origin_at": NOW,
        "suggestions": [
            {"artifact_key": CL_KEY, "title": "<b>evil</b>", "url": "https://x/1",
             "link_reason": "gmail_thread_ref"},
            {"artifact_key": BUG_KEY, "title": "Bug", "url": "https://x/2",
             "link_reason": "chat_mention"},
        ],
    }
    proj = card_ui._make_projection(w)  # pylint: disable=protected-access
    script_js = card_ui._extract_script(  # pylint: disable=protected-access
        workbench_server.render_workbench_html(proj, ui_version=1)
    )
    node_code = (
        card_ui._node_harness_prefix()  # pylint: disable=protected-access
        + f"\nglobal.__latestState = {json.dumps(proj)};\n"
        + script_js
        + r"""
(async () => {
  const html = els['wb-cards-container'].innerHTML;
  assert(html.includes('Предложено (2)'), 'suggestions header with count');
  assert(!html.includes('<b>evil</b>'), 'suggestion title must be escaped');
  assert(html.includes('&lt;b&gt;evil&lt;/b&gt;'), 'escaped title rendered');
  assert(html.includes('href="https://x/1"'), 'suggestion is a link');
  assert(html.includes('data-suggestion-accept="gerrit:skia:1414016"'), 'accept button');
  assert(html.includes('data-suggestion-reject="buganizer:389111535"'), 'reject button');
  const mk = (attr, key) => ({
    hasAttribute: (k) => k === attr || k === 'data-suggestion-work',
    getAttribute: (k) => (k === attr ? key : (k === 'data-suggestion-work' ? 'work-u' : null)),
  });
  for (const [attr, key, url] of [
    ['data-suggestion-accept', 'gerrit:skia:1414016', '/api/work/suggestion/accept'],
    ['data-suggestion-reject', 'buganizer:389111535', '/api/work/suggestion/reject'],
  ]) {
    const btn = mk(attr, key);
    await document.body.dispatch('click', {
      altKey: false,
      preventDefault: () => {},
      target: { closest: (sel) => (sel.includes('button') ? btn : null) },
    });
    const last = posted[posted.length - 1];
    assert.strictEqual(last.url, url);
    assert.strictEqual(last.body.work_id, 'work-u');
    assert.strictEqual(last.body.artifact_key, key);
  }
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    proc = subprocess.run(["node", "-e", node_code], capture_output=True, text=True, check=False)
    self.assertEqual(proc.returncode, 0, msg=f"Node failed:\n{proc.stderr}\n{proc.stdout}")


if __name__ == "__main__":
  unittest.main()
