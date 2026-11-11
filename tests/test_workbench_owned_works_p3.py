#!/usr/bin/env python3
"""Tests for R30 phase 3: adopt / split / close hints on Workbench cards and their dismissals."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.request

WORKBENCH_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workbench"
)
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (WORKBENCH_DIR, TESTS_DIR):
  if _p not in sys.path:
    sys.path.insert(0, _p)

import test_workbench_card_ui as card_ui  # pylint: disable=g-import-not-at-top
import work_store  # pylint: disable=g-import-not-at-top
import workbench_server  # pylint: disable=g-import-not-at-top

NOW = 1_760_000_000.0
DAY = 86400.0
BUG_A = "buganizer:111111111"
BUG_B = "buganizer:222222222"
CL_A = "gerrit:skia:1000001"
CL_B = "gerrit:skia:1000002"
MAIL = "gmail_thread:abc"


def _q(store: work_store.WorkStore, sql: str, args: tuple = ()) -> list[tuple]:
  """Runs a read query straight on SQLite and returns plain tuples."""
  with store._connect() as conn:  # pylint: disable=protected-access
    return [tuple(r) for r in conn.execute(sql, args).fetchall()]


def _cards(store: work_store.WorkStore, now: float) -> dict[str, dict]:
  """Returns {work_id: card} from the projection at now."""
  proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)
  return {w["work_id"]: w for w in proj["works"]}


def _kinds(card: dict) -> list[str]:
  return [h["kind"] for h in card.get("hints") or []]


def _cl_state(own: bool, status: str = "NEW", bug: str = "", ts: float = NOW) -> dict:
  st = {"status": status, "subject": "x", "activity_ts": ts, "is_self": own}
  if not own:
    st["owner"] = "someone@google.com"
  if bug:
    st["linked_bug_keys"] = [bug]
  return st


class HintStoreTest(unittest.TestCase):
  """Verifies when each hint kind appears, split_cluster moves, and dismissal fingerprints."""

  def setUp(self) -> None:
    self._tmp = tempfile.TemporaryDirectory()
    self.store = work_store.WorkStore(os.path.join(self._tmp.name, "workbench.db"))

  def tearDown(self) -> None:
    self._tmp.cleanup()

  def _art(self, key: str, kind: str, title: str, state: dict) -> None:
    self.store.upsert_artifact(key, kind=kind, title=title, state=state, now=NOW)

  def _bug(self, key: str, status: str = "ASSIGNED", title: str = "Bug") -> None:
    self._art(key, "buganizer", title, {"status": status, "bug_title": title, "updated_ts": NOW})

  def _live_draft(self, own_cl: bool = True) -> str:
    self._art(CL_A, "gerrit", "Fix", _cl_state(own_cl, bug=BUG_A))
    self._bug(BUG_A)
    self._art(MAIL, "gmail_thread", "Thread", {"last_message_ts": NOW, "last_from_me": False})
    wid = self.store.create_work("Auto", work_id="work-d", now=NOW)
    for k in (CL_A, BUG_A, MAIL):
      self.store.link_artifact(wid, k, role="primary", link_reason="bug_trailer", now=NOW)
    return wid

  # A live draft (3+ members, own CL, recent) offers adopt, and only adopt
  def test_adopt_hint_on_live_draft(self) -> None:
    wid = self._live_draft()
    card = _cards(self.store, NOW + DAY)[wid]
    self.assertEqual(_kinds(card), ["adopt"])
    self.assertEqual(card["hints"][0]["label"], "Сделать своей?")

  # Adopt needs her signal (own CL or her recent reply) and activity within 7 days
  def test_adopt_needs_signal_and_recency(self) -> None:
    wid = self._live_draft(own_cl=False)
    self.assertEqual(_kinds(_cards(self.store, NOW + DAY)[wid]), [])
    self._art(MAIL, "gmail_thread", "Thread", {"last_message_ts": NOW, "last_from_me": True})
    self.assertEqual(_kinds(_cards(self.store, NOW + DAY)[wid]), ["adopt"])
    self.assertEqual(_kinds(_cards(self.store, NOW + 8 * DAY)[wid]), [])
    self.store.unlink_artifact(wid, BUG_A, now=NOW)
    self.assertEqual(_kinds(_cards(self.store, NOW + DAY)[wid]), [])

  # Her own works never get adopt; drafts never get split or close
  def test_owner_gates_hint_kinds(self) -> None:
    wid = self._live_draft()
    self.store.set_work_owner(wid, "user", now=NOW)
    self.assertNotIn("adopt", _kinds(_cards(self.store, NOW + DAY)[wid]))
    split_wid = self._two_bug_user_work()
    self.store.set_work_owner(split_wid, "draft", now=NOW)
    self.assertNotIn("split", _kinds(_cards(self.store, NOW + DAY)[split_wid]))
    done_wid = self._finished_user_work()
    self.store.set_work_owner(done_wid, "draft", now=NOW)
    self.assertNotIn("close", _kinds(_cards(self.store, NOW + 8 * DAY)[done_wid]))

  def _two_bug_user_work(self) -> str:
    self._bug(BUG_A, title="Caret jumps on RTL")
    self._bug(BUG_B, title="Ellipsis overflows")
    self._art(CL_A, "gerrit", "Fix A", _cl_state(True, bug=BUG_A))
    self._art(CL_B, "gerrit", "Fix B", _cl_state(True, bug=BUG_B))
    self._art(MAIL, "gmail_thread", "Thread", {"last_message_ts": NOW})
    wid = self.store.create_work("Mine", work_id="work-u", owner="user", now=NOW)
    for k in (BUG_A, CL_A, BUG_B, CL_B, MAIL):
      self.store.link_artifact(wid, k, role="primary", link_reason="bug_trailer", now=NOW)
    return wid

  # Two bug clusters of 2+ members each offer split with short bug ids as cluster labels
  def test_split_hint_lists_bug_clusters(self) -> None:
    wid = self._two_bug_user_work()
    card = _cards(self.store, NOW + DAY)[wid]
    self.assertEqual(_kinds(card), ["split"])
    hint = card["hints"][0]
    self.assertTrue(hint["label"].startswith("Разделить?"))
    self.assertEqual([c["label"] for c in hint["clusters"]], ["b/111111111", "b/222222222"])
    self.assertEqual([c["bug_key"] for c in hint["clusters"]], [BUG_A, BUG_B])
    self.assertIn("b/111111111", hint["detail"])

  # A single bug cluster does not split, but more than 15 members does
  def test_split_hint_on_oversized_work(self) -> None:
    self._bug(BUG_A)
    self._art(CL_A, "gerrit", "Fix A", _cl_state(True, bug=BUG_A))
    wid = self.store.create_work("Mine", work_id="work-big", owner="user", now=NOW)
    for k in (BUG_A, CL_A):
      self.store.link_artifact(wid, k, role="primary", link_reason="bug_trailer", now=NOW)
    for i in range(13):
      key = f"gmail_thread:t{i}"
      self._art(key, "gmail_thread", f"T{i}", {"last_message_ts": NOW})
      self.store.link_artifact(wid, key, role="primary", link_reason="gmail_root", now=NOW)
    self.assertEqual(_kinds(_cards(self.store, NOW + DAY)[wid]), [])
    self._art("gmail_thread:t13", "gmail_thread", "T13", {"last_message_ts": NOW})
    self.store.link_artifact(
        wid, "gmail_thread:t13", role="primary", link_reason="gmail_root", now=NOW
    )
    card = _cards(self.store, NOW + DAY)[wid]
    self.assertEqual(_kinds(card), ["split"])
    self.assertEqual([c["bug_key"] for c in card["hints"][0]["clusters"]], [BUG_A])

  # split_cluster moves the bug and its CLs into a new user work titled from the bug
  def test_split_cluster_moves_members(self) -> None:
    wid = self._two_bug_user_work()
    new_wid = self.store.split_work_cluster(wid, BUG_A, now=NOW + 1)
    self.assertTrue(new_wid)
    self.assertEqual(sorted(self.store.get_work_artifact_keys(new_wid)), sorted([BUG_A, CL_A]))
    self.assertEqual(
        sorted(self.store.get_work_artifact_keys(wid)), sorted([BUG_B, CL_B, MAIL])
    )
    row = _q(self.store, "SELECT title, meta_json FROM works WHERE work_id = ?", (new_wid,))[0]
    self.assertEqual(row[0], "Caret jumps on RTL")
    self.assertEqual(work_store.work_owner(row[1]), "user")
    self.assertEqual(self.store.split_work_cluster(wid, "buganizer:999", now=NOW + 2), "")

  def _snapshot(self, wid: str) -> tuple:
    """Returns (works, source links, negative_links, obligations) rows for unchanged checks."""
    return (
        _q(self.store, "SELECT work_id FROM works ORDER BY work_id"),
        _q(self.store, "SELECT * FROM work_links WHERE work_id = ? ORDER BY artifact_key", (wid,)),
        _q(self.store, "SELECT * FROM negative_links ORDER BY work_id, artifact_key"),
        _q(self.store, "SELECT * FROM obligations ORDER BY obligation_id"),
    )

  def _user_work_with_bug_mail(self) -> str:
    wid = self._two_bug_user_work()
    mail2 = "gmail_thread:m2"
    self._art(mail2, "gmail_thread", "Re", {"last_message_ts": NOW, "last_from_me": False})
    self.store.link_artifact(
        wid, mail2, role="primary", link_reason="bug_ref", link_detail="b/111111111", now=NOW
    )
    self.store.reconcile_correspondence_obligations(wid, now=NOW)
    return wid

  # A moved thread keeps its "Reply needed": the obligation follows with its status intact
  def test_split_cluster_carries_ready_obligation(self) -> None:
    wid = self._user_work_with_bug_mail()
    sql = "SELECT work_id, status FROM obligations WHERE target_artifact_key = ?"
    self.assertEqual(_q(self.store, sql, ("gmail_thread:m2",)), [(wid, "ready")])
    new_wid = self.store.split_work_cluster(wid, BUG_A, now=NOW + 1)
    self.assertIn("gmail_thread:m2", self.store.get_work_artifact_keys(new_wid))
    self.assertEqual(_q(self.store, sql, ("gmail_thread:m2",)), [(new_wid, "ready")])

  # Split is a user-work action; a draft group refuses it and stays as it was
  def test_split_cluster_refuses_draft(self) -> None:
    wid = self._two_bug_user_work()
    self.store.set_work_owner(wid, "draft", now=NOW)
    before = self._snapshot(wid)
    self.assertEqual(self.store.split_work_cluster(wid, BUG_A, now=NOW + 1), "")
    self.assertEqual(self._snapshot(wid), before)

  # A cluster holding every member would leave an empty work behind, so it is refused
  def test_split_cluster_refuses_whole_work(self) -> None:
    self._bug(BUG_A)
    self._art(CL_A, "gerrit", "Fix A", _cl_state(True, bug=BUG_A))
    wid = self.store.create_work("Mine", work_id="work-one", owner="user", now=NOW)
    for k in (BUG_A, CL_A):
      self.store.link_artifact(wid, k, role="primary", link_reason="bug_trailer", now=NOW)
    before = self._snapshot(wid)
    self.assertEqual(self.store.split_work_cluster(wid, BUG_A, now=NOW + 1), "")
    self.assertEqual(self._snapshot(wid), before)

  # b/12345678 is a digit prefix of b/123456789; a mail naming the longer id joins that bug
  def test_bug_clusters_digit_boundary(self) -> None:
    short, long_ = "buganizer:12345678", "buganizer:123456789"
    mail = {
        "artifact_key": "gmail_thread:x", "kind": "gmail_thread", "state": {},
        "link_detail": "re b/123456789",
    }
    members = [{"artifact_key": short}, {"artifact_key": long_}, mail]
    self.assertEqual(
        dict(work_store.bug_clusters(members)),
        {short: [short], long_: [long_, "gmail_thread:x"]},
    )

  # Any failure while moving members rolls back: nothing orphaned and no empty new work
  def test_split_cluster_failure_is_atomic(self) -> None:
    wid = self._user_work_with_bug_mail()
    before = self._snapshot(wid)
    orig = self.store._log_event  # pylint: disable=protected-access

    def _boom(conn, event_type, *args, **kwargs):
      if event_type == "work_split":
        raise RuntimeError("boom")
      return orig(conn, event_type, *args, **kwargs)

    with mock.patch.object(self.store, "_log_event", side_effect=_boom):
      with self.assertRaises(RuntimeError):
        self.store.split_work_cluster(wid, BUG_A, now=NOW + 1)
    self.assertEqual(self._snapshot(wid), before)

  def _finished_user_work(self) -> str:
    self._bug(BUG_A, status="FIXED")
    self._art(CL_A, "gerrit", "Fix A", _cl_state(True, status="MERGED", bug=BUG_A))
    wid = self.store.create_work("Mine", work_id="work-f", owner="user", now=NOW)
    for k in (BUG_A, CL_A):
      self.store.link_artifact(wid, k, role="primary", link_reason="bug_trailer", now=NOW)
    return wid

  # Closed bugs, landed CLs, no open obligations and a quiet week offer close
  def test_close_hint(self) -> None:
    wid = self._finished_user_work()
    self.assertEqual(_kinds(_cards(self.store, NOW + 8 * DAY)[wid]), ["close"])
    self.assertEqual(_kinds(_cards(self.store, NOW + DAY)[wid]), [])

  # An open bug or an open obligation keeps the work from close
  def test_close_blocked_by_open_bug_or_obligation(self) -> None:
    wid = self._finished_user_work()
    self._bug(BUG_A, status="ASSIGNED")
    self.assertNotIn("close", _kinds(_cards(self.store, NOW + 8 * DAY)[wid]))
    self._bug(BUG_A, status="FIXED")
    self.store.add_obligation(wid, "Follow up", now=NOW)
    self.assertNotIn("close", _kinds(_cards(self.store, NOW + 8 * DAY)[wid]))

  # ✕ hides one kind until membership changes, and does not bump updated_at
  def test_dismiss_until_membership_changes(self) -> None:
    wid = self._live_draft()
    before = _q(self.store, "SELECT updated_at FROM works WHERE work_id = ?", (wid,))
    self.store.dismiss_work_hint(wid, "adopt", now=NOW + 5)
    self.assertEqual(
        _q(self.store, "SELECT updated_at FROM works WHERE work_id = ?", (wid,)), before
    )
    self.assertEqual(_kinds(_cards(self.store, NOW + DAY)[wid]), [])
    self._art(CL_B, "gerrit", "Other", _cl_state(True))
    self.store.link_artifact(wid, CL_B, role="primary", link_reason="bug_trailer", now=NOW)
    self.assertEqual(_kinds(_cards(self.store, NOW + DAY)[wid]), ["adopt"])

  # Hints are advisory: every card keeps the same lane with hint computation disabled
  def test_hints_do_not_change_lanes(self) -> None:
    self._live_draft()
    self._two_bug_user_work()
    with_hints = {k: c["lane"] for k, c in _cards(self.store, NOW + DAY).items()}
    with mock.patch.object(work_store, "compute_work_hints", return_value=[]):
      without = {k: c["lane"] for k, c in _cards(self.store, NOW + DAY).items()}
    self.assertEqual(with_hints, without)


class HintHttpAndUiTest(unittest.TestCase):
  """Verifies the hint endpoints and the Подсказка line on cards."""

  def setUp(self) -> None:
    self._tmp = tempfile.TemporaryDirectory()
    self.store = work_store.WorkStore(os.path.join(self._tmp.name, "workbench.db"))

  def tearDown(self) -> None:
    self._tmp.cleanup()

  # POST split_cluster creates the new work and POST hint/dismiss stores the fingerprint
  def test_http_split_cluster_and_dismiss(self) -> None:
    for k, title in ((BUG_A, "Bug A"), (BUG_B, "Bug B")):
      self.store.upsert_artifact(k, kind="buganizer", title=title, state={"status": "NEW"})
    self.store.upsert_artifact(CL_A, kind="gerrit", state=_cl_state(True, bug=BUG_A))
    wid = self.store.create_work("Mine", work_id="work-u", owner="user", now=NOW)
    for k in (BUG_A, CL_A, BUG_B):
      self.store.link_artifact(wid, k, role="primary", link_reason="bug_trailer", now=NOW)
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
      _post("/api/work/hint/dismiss", {"work_id": wid, "kind": "close"})
      raw = _q(self.store, "SELECT meta_json FROM works WHERE work_id = ?", (wid,))[0][0]
      meta = json.loads(raw)
      self.assertIn("close", meta["hint_dismissed"])
      proj = _post("/api/work/split_cluster", {"work_id": wid, "bug_key": BUG_A})
      titles = {w["title"] for w in proj["works"]}
      self.assertIn("Bug A", titles)
      self.assertEqual(self.store.get_work_artifact_keys(wid), [BUG_B])
    finally:
      srv.shutdown()
      srv.server_close()

  # The hint line escapes values and its buttons post accept / split_cluster / done / dismiss
  def test_js_renders_hints(self) -> None:
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
        "hints": [
            {"kind": "split", "label": "Разделить?", "detail": "<b>evil</b>",
             "clusters": [{"bug_key": BUG_A, "label": "b/111111111"}]},
            {"kind": "close", "label": "Закрыть?", "detail": ""},
            {"kind": "adopt", "label": "Сделать своей?", "detail": ""},
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
  assert(html.includes('Подсказка'), 'hint line');
  assert(html.includes('Разделить?: b/111111111'), 'split label with clusters');
  assert(html.includes('Закрыть?') && html.includes('Сделать своей?'), 'close and adopt');
  assert(!html.includes('<b>evil</b>'), 'detail must be escaped');
  assert(html.includes('data-hint-dismiss="split"'), 'dismiss button');
  assert(html.includes('data-hint-bug="buganizer:111111111"'), 'split cluster button');
  const mk = (attrs) => ({
    hasAttribute: (k) => k in attrs,
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
  });
  const cases = [
    [{'data-hint-action': 'adopt', 'data-hint-work': 'work-u'}, '/api/work/accept', {}],
    [{'data-hint-action': 'close', 'data-hint-work': 'work-u'}, '/api/work/done', {}],
    [{'data-hint-action': 'split', 'data-hint-work': 'work-u',
      'data-hint-bug': 'buganizer:111111111'}, '/api/work/split_cluster',
     {bug_key: 'buganizer:111111111'}],
    [{'data-hint-dismiss': 'split', 'data-hint-work': 'work-u'}, '/api/work/hint/dismiss',
     {kind: 'split'}],
  ];
  for (const [attrs, url, extra] of cases) {
    const btn = mk(attrs);
    await document.body.dispatch('click', {
      altKey: false,
      preventDefault: () => {},
      target: { closest: (sel) => (sel.includes('button') ? btn : null) },
    });
    const last = posted[posted.length - 1];
    assert.strictEqual(last.url, url);
    assert.strictEqual(last.body.work_id, 'work-u');
    for (const [k, v] of Object.entries(extra)) assert.strictEqual(last.body[k], v);
  }
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    proc = subprocess.run(["node", "-e", node_code], capture_output=True, text=True, check=False)
    self.assertEqual(proc.returncode, 0, msg=f"Node failed:\n{proc.stderr}\n{proc.stdout}")


if __name__ == "__main__":
  unittest.main()
