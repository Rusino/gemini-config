#!/usr/bin/env python3
"""Workbench R30 contracts: user-owned works vs auto-linker draft groups."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
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
import workctl  # pylint: disable=g-import-not-at-top

NOW = 1_760_000_000.0
CL_KEY = "gerrit:skia:1414016"


def _raw_meta(store: work_store.WorkStore, wid: str) -> dict:
  """Returns the parsed works.meta_json for wid straight from SQLite."""
  with store._connect() as conn:  # pylint: disable=protected-access
    row = conn.execute("SELECT meta_json FROM works WHERE work_id = ?", (wid,)).fetchone()
  return json.loads(row[0] or "{}")


def _card(store: work_store.WorkStore, wid: str) -> dict:
  """Returns the projected dashboard card for wid."""
  proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=NOW)
  return next(w for w in proj["works"] if w["work_id"] == wid)


class OwnedWorksStoreTest(unittest.TestCase):
  """Verifies owner field storage, accept/merge rules, migration, and link protection."""

  def setUp(self) -> None:
    self._tmp = tempfile.TemporaryDirectory()
    self.db_path = os.path.join(self._tmp.name, "workbench.db")
    self.store = work_store.WorkStore(self.db_path)
    self.store.upsert_artifact(CL_KEY, kind="gerrit", title="Fix rects", now=NOW)

  def tearDown(self) -> None:
    self._tmp.cleanup()

  # Missing or unparsable owner reads as draft so legacy auto groups never pose as her works
  def test_work_owner_helper_defaults_to_draft(self) -> None:
    self.assertEqual(work_store.work_owner({}), "draft")
    self.assertEqual(work_store.work_owner("{not json"), "draft")
    self.assertEqual(work_store.work_owner({"owner": "bogus"}), "draft")
    self.assertEqual(work_store.work_owner({"owner": "user"}), "user")
    self.assertEqual(work_store.work_owner('{"owner": "user"}'), "user")

  # create_work defaults to draft; owner="user" sticks and later auto upserts never downgrade it
  def test_create_work_owner_default_and_no_downgrade(self) -> None:
    w_auto = self.store.create_work("Auto group", now=NOW)
    self.assertEqual(_raw_meta(self.store, w_auto).get("owner"), "draft")
    w_user = self.store.create_work("Mine", work_id="work-mine", owner="user", now=NOW)
    self.assertEqual(_raw_meta(self.store, w_user).get("owner"), "user")
    self.store.create_work("Mine renamed", project_id="p", work_id="work-mine", now=NOW + 1)
    self.assertEqual(_raw_meta(self.store, w_user).get("owner"), "user")
    # An explicit user re-create upgrades an existing draft in place
    self.store.create_work("Auto group", work_id=w_auto, owner="user", now=NOW + 2)
    self.assertEqual(_raw_meta(self.store, w_auto).get("owner"), "user")

  # Accept flips draft -> user; merge of a draft and a user work yields a user work either way
  def test_set_owner_and_merge_rule(self) -> None:
    w_draft = self.store.create_work("Draft", now=NOW)
    self.store.set_work_owner(w_draft, "user")
    self.assertEqual(_raw_meta(self.store, w_draft).get("owner"), "user")

    t_draft = self.store.create_work("T draft", work_id="work-t1", now=NOW)
    s_user = self.store.create_work("S user", work_id="work-s1", owner="user", now=NOW)
    self.store.merge_works(t_draft, s_user, source="auto", now=NOW)
    self.assertEqual(_raw_meta(self.store, t_draft).get("owner"), "user")

    t_user = self.store.create_work("T user", work_id="work-t2", owner="user", now=NOW)
    s_draft = self.store.create_work("S draft", work_id="work-s2", now=NOW)
    self.store.merge_works(t_user, s_draft, now=NOW)
    self.assertEqual(_raw_meta(self.store, t_user).get("owner"), "user")

    t_d = self.store.create_work("T d", work_id="work-t3", now=NOW)
    s_d = self.store.create_work("S d", work_id="work-s3", now=NOW)
    self.store.merge_works(t_d, s_d, now=NOW)
    self.assertEqual(_raw_meta(self.store, t_d).get("owner"), "draft")

  # Schema init stamps owner=draft on legacy rows once, keeps user, and is idempotent
  def test_migration_marks_legacy_works_draft_and_keeps_user(self) -> None:
    with sqlite3.connect(self.db_path) as conn:
      conn.executemany(
          "INSERT INTO works (work_id, title, created_at, updated_at, meta_json) "
          "VALUES (?, ?, ?, ?, ?)",
          [
              ("work-legacy", "Legacy", NOW, NOW, '{"title_manual": true}'),
              ("work-empty", "Empty", NOW, NOW, ""),
              ("work-user", "User", NOW, NOW, '{"owner": "user"}'),
          ],
      )
    store2 = work_store.WorkStore(self.db_path)
    self.assertEqual(
        _raw_meta(store2, "work-legacy"), {"title_manual": True, "owner": "draft"}
    )
    self.assertEqual(_raw_meta(store2, "work-empty"), {"owner": "draft"})
    self.assertEqual(_raw_meta(store2, "work-user"), {"owner": "user"})
    work_store.WorkStore(self.db_path)
    self.assertEqual(_raw_meta(store2, "work-user"), {"owner": "user"})

  # Auto-linker cannot grab a primary that a user work owns; the draft gets context_only
  def test_auto_primary_on_draft_demoted_when_user_work_owns_it(self) -> None:
    w_user = self.store.create_work("Mine", owner="user", now=NOW)
    self.store.link_artifact(w_user, CL_KEY, role="primary", source="auto", now=NOW)
    w_draft = self.store.create_work("Auto group", work_id="work-auto", now=NOW)
    role = self.store.link_artifact(w_draft, CL_KEY, role="primary", source="auto", now=NOW)
    self.assertEqual(role, "context_only")
    self.assertEqual(self.store.get_work_artifact_roles(w_draft)[CL_KEY], "context_only")
    self.assertEqual(self.store.get_work_artifact_roles(w_user)[CL_KEY], "primary")

  # Manual links keep precedence; draft vs draft competition is unchanged
  def test_manual_link_and_draft_vs_draft_unchanged(self) -> None:
    w_user = self.store.create_work("Mine", owner="user", now=NOW)
    self.store.link_artifact(w_user, CL_KEY, role="primary", source="auto", now=NOW)
    w_manual = self.store.create_work("Manual draft", work_id="work-m", now=NOW)
    role_m = self.store.link_artifact(w_manual, CL_KEY, role="primary", source="manual", now=NOW)
    self.assertEqual(role_m, "primary")

    other = "gerrit:skia:1414017"
    self.store.upsert_artifact(other, kind="gerrit", title="Other", now=NOW)
    d1 = self.store.create_work("D1", work_id="work-d1", now=NOW)
    d2 = self.store.create_work("D2", work_id="work-d2", now=NOW)
    self.store.link_artifact(d1, other, role="primary", source="auto", now=NOW)
    self.assertEqual(
        self.store.link_artifact(d2, other, role="primary", source="auto", now=NOW), "primary"
    )

  # Lane classification ignores owner: a draft with a ready obligation still lands in Needs You
  def test_needs_you_independent_of_owner(self) -> None:
    w_draft = self.store.create_work("Draft", work_id="work-dn", now=NOW)
    w_user = self.store.create_work("User", work_id="work-un", owner="user", now=NOW)
    for wid in (w_draft, w_user):
      self.store.add_obligation(wid, description="Reply to Ben", trigger_expr="always", now=NOW)
    c_draft = _card(self.store, w_draft)
    c_user = _card(self.store, w_user)
    self.assertEqual(c_draft["lane"], "needs-you")
    self.assertEqual(c_user["lane"], "needs-you")
    self.assertEqual(c_draft["owner"], "draft")
    self.assertEqual(c_user["owner"], "user")
    slim = workbench_server.slim_ui_projection(
        {"works": [c_draft, c_user], "counts": {}, "metrics": {}}
    )
    self.assertEqual([w["owner"] for w in slim["works"]], ["draft", "user"])

  # split_work inherits the source owner: a piece of her Work stays hers, a draft stays draft
  def test_split_work_inherits_source_owner(self) -> None:
    w_user = self.store.create_work("Mine", owner="user", now=NOW)
    self.store.link_artifact(w_user, CL_KEY, role="primary", source="manual", now=NOW)
    new_u = self.store.split_work(w_user, CL_KEY, "Split mine", now=NOW)
    self.assertEqual(_raw_meta(self.store, new_u).get("owner"), "user")
    w_draft = self.store.create_work("Auto", work_id="work-auto-split", now=NOW)
    self.store.link_artifact(w_draft, CL_KEY, role="primary", source="manual", now=NOW)
    new_d = self.store.split_work(w_draft, CL_KEY, "Split auto", now=NOW + 1)
    self.assertEqual(_raw_meta(self.store, new_d).get("owner"), "draft")

  # + Chat on a virtual card materializes a Work she asked for, so it is owner=user
  def test_materialize_virtual_card_creates_user_work(self) -> None:
    card = {"work_id": f"virt-{CL_KEY}", "title": "Virtual CL", "artifacts": []}
    ingestor._materialize_virtual_card(  # pylint: disable=protected-access
        self.store, card, "proj", NOW
    )
    with self.store._connect() as conn:  # pylint: disable=protected-access
      row = conn.execute("SELECT work_id FROM works WHERE title = 'Virtual CL'").fetchone()
    self.assertEqual(_raw_meta(self.store, row[0]).get("owner"), "user")


class OwnedWorksEntryPointsTest(unittest.TestCase):
  """Verifies user-initiated create paths mark owner=user and /api/work/accept flips drafts."""

  def setUp(self) -> None:
    self._tmp = tempfile.TemporaryDirectory()
    self.db_path = os.path.join(self._tmp.name, "workbench.db")
    self.store = work_store.WorkStore(self.db_path)

  def tearDown(self) -> None:
    self._tmp.cleanup()

  # workctl new and pin are explicit user commands, so the minted works are owner=user
  def test_workctl_new_and_pin_create_user_works(self) -> None:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
      workctl.main(["--db", self.db_path, "new", "CLI work"])
      workctl.main(["--db", self.db_path, "pin", "Pinned", "--conv", "abc"])
    wids = buf.getvalue().split()
    self.assertEqual(len(wids), 2)
    for wid in wids:
      self.assertEqual(_raw_meta(self.store, wid).get("owner"), "user")

  # Pinning a chat the ingestor already grouped into a draft makes that work hers
  def test_workctl_pin_onto_existing_draft_promotes_it(self) -> None:
    w_draft = self.store.create_work("Auto chat", work_id="work-auto-chat", now=NOW)
    self.store.link_artifact(w_draft, "chat_chain:xyz", role="channel", now=NOW)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
      workctl.main(["--db", self.db_path, "pin", "Pinned", "--conv", "xyz"])
    self.assertEqual(buf.getvalue().split(), [w_draft])
    self.assertEqual(_raw_meta(self.store, w_draft).get("owner"), "user")

  # /api/work/create mints owner=user and /api/work/accept turns a draft into her work
  def test_http_create_and_accept(self) -> None:
    w_draft = self.store.create_work("Auto", work_id="work-auto", now=NOW)
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
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()

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
      created = _post("/api/work/create", {"title": "Mine via UI"})
      mine = next(w for w in created["works"] if w["title"] == "Mine via UI")
      self.assertEqual(mine["owner"], "user")
      accepted = _post("/api/work/accept", {"work_id": w_draft})
      acc = next(w for w in accepted["works"] if w["work_id"] == w_draft)
      self.assertEqual(acc["owner"], "user")
    finally:
      srv.shutdown()
      srv.server_close()


def _ui_work(wid: str, lane: str, owner: str | None) -> dict:
  """Builds a minimal persisted card dict for the client renderer."""
  w = {
      "work_id": wid,
      "title": f"Title {wid}",
      "lane": lane,
      "status": "active",
      "reasons": [],
      "artifacts": [],
      "obligations": [],
      "checklist": [],
      "is_virtual": False,
      "work_types": [],
      "needs_you_types": [],
      "activity_at": NOW,
      "origin_at": NOW,
  }
  if owner is not None:
    w["owner"] = owner
  return w


class OwnedWorksUiTest(unittest.TestCase):
  """Verifies the Мои работы / Черновики split, Принять button, and persisted fold state."""

  # Draft cards outside Needs You fold under Черновики; Needs-You drafts stay in the main group
  def test_js_groups_drafts_and_accept_button(self) -> None:
    proj = card_ui._make_projection(  # pylint: disable=protected-access
        _ui_work("work-u", "waiting", "user"),
        _ui_work("work-dn", "needs-you", "draft"),
        _ui_work("work-dw", "waiting", "draft"),
    )
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
  const iMine = html.indexOf('Мои работы');
  const iDrafts = html.indexOf('Черновики');
  assert(iMine >= 0 && iDrafts > iMine, 'Мои работы must render above Черновики');
  const draftsPart = html.slice(iDrafts);
  assert(draftsPart.includes('data-work-id="work-dw"'), 'waiting draft goes to Черновики');
  assert(!draftsPart.includes('data-work-id="work-dn"'), 'Needs-You draft stays in main group');
  assert(!draftsPart.includes('data-work-id="work-u"'), 'user work stays in main group');
  assert(/wb-drafts-body"[^>]*hidden/.test(html), 'Черновики collapsed by default');
  assert(html.includes('data-work-accept="work-dw"'), 'draft card gets Принять');
  assert(html.includes('data-work-accept="work-dn"'), 'Needs-You draft card gets Принять');
  assert(!html.includes('data-work-accept="work-u"'), 'user card has no Принять');

  const toggleBtn = {
    hasAttribute: (k) => k === 'data-drafts-toggle',
    getAttribute: () => null,
  };
  await document.body.dispatch('click', {
    altKey: false,
    preventDefault: () => {},
    target: { closest: (sel) => (sel.includes('button') ? toggleBtn : null) },
  });
  assert(!/wb-drafts-body"[^>]*hidden/.test(els['wb-cards-container'].innerHTML));
  assert.strictEqual(JSON.parse(storage.get('wb_drafts_v1')).open, true);

  const acceptBtn = {
    hasAttribute: (k) => k === 'data-work-accept',
    getAttribute: (k) => (k === 'data-work-accept' ? 'work-dw' : null),
  };
  await document.body.dispatch('click', {
    altKey: false,
    preventDefault: () => {},
    target: { closest: (sel) => (sel.includes('button') ? acceptBtn : null) },
  });
  const last = posted[posted.length - 1];
  assert.strictEqual(last.url, '/api/work/accept');
  assert.strictEqual(last.body.work_id, 'work-dw');
})().catch((err) => { console.error(err); process.exit(1); });
"""
    )
    proc = subprocess.run(["node", "-e", node_code], capture_output=True, text=True, check=False)
    self.assertEqual(proc.returncode, 0, msg=f"Node failed:\n{proc.stderr}\n{proc.stdout}")

  # Activity and Age stay flat chronological lists; only Triage groups by owner
  def test_js_activity_and_age_sorts_do_not_group(self) -> None:
    proj = card_ui._make_projection(  # pylint: disable=protected-access
        _ui_work("work-u", "waiting", "user"),
        _ui_work("work-dw", "waiting", "draft"),
    )
    script_js = card_ui._extract_script(  # pylint: disable=protected-access
        workbench_server.render_workbench_html(proj, ui_version=1)
    )
    for mode in ("activity", "age"):
      node_code = (
          card_ui._node_harness_prefix()  # pylint: disable=protected-access
          + f"\nstorage.set('wb_sort_v1', JSON.stringify({{mode: '{mode}', dir: 'desc'}}));\n"
          + f"global.__latestState = {json.dumps(proj)};\n"
          + script_js
          + r"""
const html = els['wb-cards-container'].innerHTML;
assert(!html.includes('Черновики') && !html.includes('Мои работы'), 'flat list expected');
assert(html.includes('data-work-id="work-dw"') && html.includes('data-work-accept="work-dw"'));
"""
      )
      proc = subprocess.run(["node", "-e", node_code], capture_output=True, text=True, check=False)
      self.assertEqual(proc.returncode, 0, msg=f"{mode}: {proc.stderr}\n{proc.stdout}")


if __name__ == "__main__":
  unittest.main()
