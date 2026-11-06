#!/usr/bin/env python3
"""Contract unit tests for Workbench (work_store, ingestor, workctl, workbench_server)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKBENCH_DIR = os.path.join(CONFIG_DIR, "workbench")
if WORKBENCH_DIR not in sys.path:
  sys.path.insert(0, WORKBENCH_DIR)


class TestWorkbenchContracts(unittest.TestCase):

  def setUp(self) -> None:
    self.tmp_dir = tempfile.TemporaryDirectory()
    self.db_path = os.path.join(self.tmp_dir.name, "work_graph.db")
    self.state_path = os.path.join(self.tmp_dir.name, "watcher_state.json")
    self.cl_state_path = os.path.join(self.tmp_dir.name, "cl_watcher_state.json")
    self.info_path = os.path.join(self.tmp_dir.name, "jetski-workbench.json")

  def tearDown(self) -> None:
    self.tmp_dir.cleanup()

  def test_canonicalize_artifact_key_and_hub_detector(self) -> None:
    import work_store

    self.assertEqual(
        work_store.canonicalize_artifact_key("gerrit", "1414016"),
        "gerrit:skia:1414016",
    )
    self.assertEqual(
        work_store.canonicalize_artifact_key("gerrit:chromium", "8746922"),
        "gerrit:chromium:8746922",
    )
    self.assertEqual(
        work_store.canonicalize_artifact_key("github", "194335"),
        "github_pr:flutter/flutter:194335",
    )
    self.assertEqual(
        work_store.canonicalize_artifact_key("flutter_issue", "155062"),
        "github_issue:flutter/flutter:155062",
    )
    self.assertEqual(
        work_store.canonicalize_artifact_key("buganizer", "580955594"),
        "buganizer:580955594",
    )
    self.assertEqual(
        work_store.canonicalize_artifact_key("gmail_draft", "r2068112471352956917"),
        "gmail_draft:r2068112471352956917",
    )

    store = work_store.WorkStore(self.db_path)
    # Hub CL citing >3 bugs or carrying [SkoreText] must be forced to context_only
    hub_state = {
        "subject": "[SkoreText] Add CoreTextLineShaper",
        "bug_ids": ["101", "102", "103", "104", "105"],
    }
    self.assertTrue(
        work_store.is_hub_artifact("gerrit:skia:1382196", hub_state, linked_bug_count=5)
    )
    store.upsert_artifact(
        "gerrit:skia:1382196",
        kind="gerrit",
        title="[SkoreText] Add CoreTextLineShaper",
        url="https://skia-review.googlesource.com/c/skia/+/1382196",
        state=hub_state,
    )
    w1 = store.create_work("Fix trailing spaces in SkParagraph", project_id="skia")
    role = store.link_artifact(w1, "gerrit:skia:1382196", role="primary", source="auto")
    self.assertEqual(role, "context_only")

  def test_negative_links_prevent_auto_relink_and_allow_manual_override(self) -> None:
    import work_store

    store = work_store.WorkStore(self.db_path)
    w1 = store.create_work("Work A", project_id="skia")
    store.upsert_artifact(
        "buganizer:580955594",
        kind="buganizer",
        title="SkChromeRemoteGlyphCache crash",
        url="https://issues.chromium.org/issues/580955594",
        state={"status": "ASSIGNED"},
    )
    store.link_artifact(w1, "buganizer:580955594", role="primary", source="auto")
    self.assertIn("buganizer:580955594", store.get_work_artifact_keys(w1))

    # User detaches the artifact via [✂]; tombstone must block auto-relink
    store.unlink_artifact(w1, "buganizer:580955594")
    self.assertNotIn("buganizer:580955594", store.get_work_artifact_keys(w1))
    auto_res = store.link_artifact(w1, "buganizer:580955594", role="primary", source="auto")
    self.assertIsNone(auto_res)
    self.assertNotIn("buganizer:580955594", store.get_work_artifact_keys(w1))

    # Manual link overrides the negative tombstone
    manual_res = store.link_artifact(
        w1, "buganizer:580955594", role="primary", source="manual"
    )
    self.assertEqual(manual_res, "primary")
    self.assertIn("buganizer:580955594", store.get_work_artifact_keys(w1))

    metrics = store.compute_metrics(days=14)
    self.assertGreaterEqual(metrics["link_detached"], 1)

  def test_three_valued_obligation_evaluator_and_stalled_watchdog(self) -> None:
    import work_store

    now = 1762450000.0
    store = work_store.WorkStore(self.db_path)
    w1 = store.create_work("Flutter #50168 strut metrics", project_id="flutter", now=now - 8 * 86400)
    cl_key = "gerrit:skia:1414545"
    thread_key = "gmail_thread:19d6229b9c71ff05"
    store.upsert_artifact(
        cl_key,
        kind="gerrit",
        title="Fix strut halfLeading",
        url="https://skia-review.googlesource.com/c/skia/+/1414545",
        state={"status": "NEW", "rolls": {}},
        now=now,
    )
    store.upsert_artifact(
        thread_key,
        kind="gmail_thread",
        title="Re: Fixed bugs in Flutter (Renzo)",
        url="https://mail.google.com/mail/u/0/#inbox/19d6229b9c71ff05",
        state={"last_from_me": False, "last_message_ts": now - 3600},
        now=now,
    )
    store.link_artifact(w1, cl_key, role="primary", source="auto", now=now)
    store.link_artifact(w1, thread_key, role="channel", source="auto", now=now)

    ob_id = store.add_obligation(
        w1,
        description="Reply to Renzo once Gerrit 1414545 rolls into Flutter",
        trigger_expr="cl_merged(gerrit:skia:1414545) AND rolled_into(Flutter, gerrit:skia:1414545)",
        target_artifact_key=thread_key,
        done_matcher="reply_sent(gmail_thread:19d6229b9c71ff05)",
        now=now - 8 * 86400,
    )

    # 1. While CL is NEW, trigger evaluates to 'false', and >7d age raises stalled_alert
    store.evaluate_all_obligations(now=now)
    ob = store.get_obligation(ob_id)
    self.assertEqual(ob["last_eval"], "false")
    self.assertEqual(ob["status"], "open")
    self.assertTrue(ob["stalled_alert"])

    # 2. When CL is MERGED but Flutter roll status is missing/unknown -> 'unknown'
    store.upsert_artifact(
        cl_key,
        kind="gerrit",
        title="Fix strut halfLeading",
        url="https://skia-review.googlesource.com/c/skia/+/1414545",
        state={"status": "MERGED", "rolls": {}},
        now=now,
    )
    store.evaluate_all_obligations(now=now)
    ob = store.get_obligation(ob_id)
    self.assertEqual(ob["last_eval"], "unknown")
    self.assertEqual(ob["status"], "open")

    # 3. When Flutter roll lands -> 'true' and status becomes 'ready'
    store.upsert_artifact(
        cl_key,
        kind="gerrit",
        title="Fix strut halfLeading",
        url="https://skia-review.googlesource.com/c/skia/+/1414545",
        state={"status": "MERGED", "rolls": {"Flutter": {"rolled": True}}},
        now=now,
    )
    store.evaluate_all_obligations(now=now)
    ob = store.get_obligation(ob_id)
    self.assertEqual(ob["last_eval"], "true")
    self.assertEqual(ob["status"], "ready")

    # 4. When reply is sent on the target gmail_thread -> done_matcher marks obligation 'done'
    store.upsert_artifact(
        thread_key,
        kind="gmail_thread",
        title="Re: Fixed bugs in Flutter (Renzo)",
        url="https://mail.google.com/mail/u/0/#inbox/19d6229b9c71ff05",
        state={"last_from_me": True, "last_message_ts": now + 10},
        now=now + 10,
    )
    store.evaluate_all_obligations(now=now + 10)
    ob = store.get_obligation(ob_id)
    self.assertEqual(ob["status"], "done")

  def test_merged_cl_stays_visible_while_work_has_open_obligations(self) -> None:
    import work_store

    now = 1762450000.0
    store = work_store.WorkStore(self.db_path)
    w1 = store.create_work("David Bebawy #155062 & #84030 review", project_id="skia", now=now)
    cl_key = "gerrit:skia:1414016"
    draft_key = "gmail_draft:r2068112471352956917"
    store.upsert_artifact(
        cl_key,
        kind="gerrit",
        title="Fix getRectsForRange empty lines",
        url="https://skia-review.googlesource.com/c/skia/+/1414016",
        state={"status": "MERGED", "rolls": {"Flutter": {"rolled": False}}},
        now=now,
    )
    store.upsert_artifact(
        draft_key,
        kind="gmail_draft",
        title="Draft: Re: Fixed bugs in Flutter",
        url="https://mail.google.com/mail/u/0/#drafts/r2068112471352956917",
        state={"status": "DRAFT", "thread_id": "19d6229b9c71ff05"},
        now=now,
    )
    store.link_artifact(w1, cl_key, role="primary", source="auto", now=now)
    store.link_artifact(w1, draft_key, role="channel", source="auto", now=now)
    store.add_obligation(
        w1,
        description="Send draft to Renzo after Gerrit 1414016 rolls into Flutter",
        trigger_expr="rolled_into(Flutter, gerrit:skia:1414016)",
        target_artifact_key=draft_key,
        now=now,
    )
    store.evaluate_all_obligations(now=now)
    proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)
    works = [w for w in proj["works"] if w["work_id"] == w1]
    self.assertEqual(len(works), 1)
    card = works[0]
    # While obligation is open (not ready), draft is parked -> lane is 'waiting' (not 'done')
    self.assertEqual(card["lane"], "waiting")
    art_keys = [a["artifact_key"] for a in card["artifacts"]]
    self.assertIn(cl_key, art_keys)
    self.assertIn(draft_key, art_keys)

    # Once the roll into Flutter completes, obligation becomes 'ready' -> lane is 'needs-you'
    store.upsert_artifact(
        cl_key,
        kind="gerrit",
        title="Fix getRectsForRange empty lines",
        url="https://skia-review.googlesource.com/c/skia/+/1414016",
        state={"status": "MERGED", "rolls": {"Flutter": {"rolled": True}}},
        now=now + 60,
    )
    proj_ready = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 60)
    card_ready = [w for w in proj_ready["works"] if w["work_id"] == w1][0]
    self.assertEqual(card_ready["lane"], "needs-you")

  def test_read_vs_write_ingestion_and_single_thread_channel_rule(self) -> None:
    import ingestor
    import work_store

    now = 1762450000.0
    store = work_store.WorkStore(self.db_path)
    # 1. Read-only transcript: views bug and email thread -> 0 Works minted, stored in candidate_buffer
    read_transcript = os.path.join(self.tmp_dir.name, "read_conv.jsonl")
    with open(read_transcript, "w", encoding="utf-8") as f:
      f.write(
          json.dumps({
              "type": "USER_INPUT",
              "content": "Посмотри b/580955594 и тред 19d6229b9c71ff05",
          })
          + "\n"
      )
      f.write(
          json.dumps({
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": "python3 ~/.gemini/config/scripts/bug_actions.py render 580955594"
                  },
              }],
          })
          + "\n"
      )
    ingestor.ingest_transcript_file(
        store,
        conv_id="conv-readonly-1",
        root_cid="conv-readonly-1",
        transcript_path=read_transcript,
        cursors={},
        now=now,
    )
    self.assertEqual(len(store.list_works()), 0)
    self.assertGreaterEqual(len(store.list_candidates(now=now)), 1)

    # 2. Write transcript with 1 human email thread in context + Gerrit draft save -> mints Work & links thread as channel
    write_transcript = os.path.join(self.tmp_dir.name, "write_conv.jsonl")
    with open(write_transcript, "w", encoding="utf-8") as f:
      f.write(
          json.dumps({
              "type": "USER_INPUT",
              "content": "Ответь в Gerrit 1414016 и учти письмо от Дэвида thread:19d69ae1ef631823",
          })
          + "\n"
      )
      f.write(
          json.dumps({
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "python3 ~/.gemini/config/scripts/gerrit_draft.py save "
                          "--change 1414016 --message 'Done'"
                      )
                  },
              }],
          })
          + "\n"
      )
    ingestor.ingest_transcript_file(
        store,
        conv_id="conv-write-1",
        root_cid="conv-write-1",
        transcript_path=write_transcript,
        cursors={},
        now=now,
    )
    works = store.list_works()
    self.assertEqual(len(works), 1)
    w_keys = store.get_work_artifact_roles(works[0]["work_id"])
    self.assertEqual(w_keys.get("gerrit:skia:1414016"), "primary")
    self.assertEqual(w_keys.get("gmail_thread:19d69ae1ef631823"), "channel")
    self.assertEqual(w_keys.get("chat_chain:conv-write-1"), "channel")

  def test_virtual_ephemeral_projection_and_roadmap_writeback(self) -> None:
    import work_store

    now = 1762450000.0
    store = work_store.WorkStore(self.db_path)
    cl_watcher_state = {
        "items": {
            "gerrit:1420001": {
                "item_key": "gerrit:1420001",
                "kind": "gerrit",
                "target_id": "1420001",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "subject": "Standalone 1-CL review",
                "url": "https://skia-review.googlesource.com/c/skia/+/1420001",
                "entity_state": {
                    "category": "needs_me",
                    "action_required": True,
                    "reason": "Needs review",
                },
            }
        }
    }
    proj = store.build_dashboard_projection(cl_watcher_state=cl_watcher_state, now=now)
    # Must project standalone 1-CL review in-memory without writing to SQLite works table
    self.assertEqual(len(store.list_works()), 0)
    virt = [w for w in proj["works"] if w.get("is_virtual")]
    self.assertEqual(len(virt), 1)
    self.assertEqual(virt[0]["lane"], "needs-you")

    # Roadmap writeback on persisted Work completion
    written_back: list[tuple[str, str]] = []
    w_id = store.create_work(
        "Ship Workbench MVP",
        project_id="61f270de",
        roadmap_item_key="61f270de:R2",
        now=now,
    )
    store.complete_work(
        w_id,
        roadmap_cb=lambda proj_id, item_id: written_back.append((proj_id, item_id)),
        now=now,
    )
    self.assertEqual(written_back, [("61f270de", "R2")])

  def test_workctl_cli_and_http_server_endpoints(self) -> None:
    import work_store
    import workbench_server

    workctl_py = os.path.join(WORKBENCH_DIR, "workctl.py")
    env = os.environ.copy()
    env["WORKBENCH_DB_PATH"] = self.db_path

    res = subprocess.run(
        [
            sys.executable,
            workctl_py,
            "new",
            "TextClusters investigation",
            "--project",
            "skia",
            "--primary",
            "buganizer:578524618",
            "--channel",
            "gmail_thread:florin_tc",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    work_id = res.stdout.strip()
    self.assertTrue(work_id.startswith("work-"))

    subprocess.run(
        [
            sys.executable,
            workctl_py,
            "oblige",
            work_id,
            "Send summary doc to Florin",
            "--when",
            "always",
            "--target",
            "gmail_thread:florin_tc",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    list_res = subprocess.run(
        [sys.executable, workctl_py, "list", "--json"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    items = json.loads(list_res.stdout)
    self.assertEqual(len(items), 1)
    self.assertEqual(items[0]["work_id"], work_id)

    # Start HTTP server on an ephemeral port and verify / and /api/state
    store = work_store.WorkStore(self.db_path)
    srv = workbench_server.create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        cl_state_path=self.cl_state_path,
        info_path=self.info_path,
    )
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
      with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as resp:
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.read().decode("utf-8"))
      self.assertEqual(len(payload["works"]), 1)
      self.assertEqual(payload["works"][0]["lane"], "needs-you")

      with urllib.request.urlopen(f"http://127.0.0.1:{port}/?embed=1", timeout=5) as resp:
        html = resp.read().decode("utf-8")
      self.assertIn("Workbench", html)
      self.assertIn("TextClusters investigation", html)
      self.assertIn("Send summary doc to Florin", html)
      self.assertIn("wb-expand-all", html)
      self.assertIn("wb-collapse-all", html)
      self.assertIn("data-toggle-card", html)

      with urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico", timeout=5) as resp:
        self.assertEqual(resp.status, 204)
    finally:
      srv.shutdown()
      srv.server_close()

  def test_chronological_origin_resolution_and_light_theme(self) -> None:
    import work_store
    import workbench_server

    oct_15_ts = 1760538903.0  # 2026-10-15 10:35 ET (Julia -> Renzo)
    oct_23_reply_ts = 1761243038.0  # 2026-10-23 14:10 ET (Renzo -> Julia)
    oct_23_chat_ts = 1761247654.0  # 2026-10-23 15:27 ET (Chat R32 started)
    now = 1762450000.0

    store = work_store.WorkStore(self.db_path)
    # Work initially minted by downstream chat R32 before email thread is ingested
    wid = store.create_work(
        "Handoff State: R32 (`flutter#160251` — `letterSpacing` on zero-width clusters)",
        project_id="flutter",
        now=oct_23_chat_ts,
    )
    store.upsert_artifact(
        "chat_chain:a98931ef",
        kind="chat_chain",
        title="Handoff State: R32 (`flutter#160251` — `letterSpacing` on zero-width clusters)",
        url="/c/a98931ef",
        state={"cascade_id": "a98931ef", "origin_ts": oct_23_chat_ts},
        now=now,
    )
    store.link_artifact(wid, "chat_chain:a98931ef", role="channel", source="auto", now=now)
    store.upsert_artifact(
        "gerrit:skia:1404137",
        kind="gerrit",
        title="1404137: Do not apply letterSpacing to zero-width clusters",
        url="https://skia-review.googlesource.com/c/skia/+/1404137",
        state={"status": "MERGED", "origin_ts": oct_23_chat_ts + 5500},
        now=now,
    )
    store.link_artifact(wid, "gerrit:skia:1404137", role="primary", source="auto", now=now)

    # Later, Gmail ingestor attaches the Oct 15 correspondence thread + draft
    store.upsert_artifact(
        "gmail_thread:1a13c69d14fe1c3d",
        kind="gmail_thread",
        title="Fixed bugs in Flutter (Renzo Olivares)",
        url="https://mail.google.com/mail/u/0/#inbox/1a13c69d14fe1c3d",
        state={
            "thread_id": "1a13c69d14fe1c3d",
            "origin_ts": oct_15_ts,
            "root_subject": "Fixed bugs in Flutter",
            "started_by_me": True,
            "counterparty": "Renzo Olivares",
            "counterparty_reply_ts": oct_23_reply_ts,
            "message_count": 6,
        },
        now=now,
    )
    store.link_artifact(wid, "gmail_thread:1a13c69d14fe1c3d", role="channel", source="auto", now=now)
    store.upsert_artifact(
        "gmail_draft:r2068112471352956917",
        kind="gmail_draft",
        title="Draft: Re: Fixed bugs in Flutter (Renzo Olivares)",
        url="https://mail.google.com/mail/u/0/#drafts/r2068112471352956917",
        state={"status": "DRAFT", "thread_id": "1a13c69d14fe1c3d"},
        now=now,
    )
    store.link_artifact(
        wid, "gmail_draft:r2068112471352956917", role="channel", source="auto", now=now
    )

    proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)
    card = [w for w in proj["works"] if w["work_id"] == wid][0]
    self.assertEqual(card["title"], "Fixed bugs in Flutter — Renzo Olivares")
    self.assertIsNotNone(card["origin"])
    self.assertEqual(card["origin"]["artifact_key"], "gmail_thread:1a13c69d14fe1c3d")
    self.assertIn(
        "🌱 Origin: ✉ Fixed bugs in Flutter (Oct 15 · Julia → Renzo Olivares · reply Oct 23 · 6 msgs)",
        card["origin"]["label"],
    )

    html = workbench_server.render_workbench_html(proj, 1)
    self.assertIn("--bg: #fbfbfd;", html)
    self.assertIn("--surface: #ffffff;", html)
    self.assertIn("wb-origin", html)
    self.assertIn("Fixed bugs in Flutter — Renzo Olivares", html)

  def test_renzo_five_bugs_checklist_and_consolidated_draft_obligation(self) -> None:
    import base64
    from unittest import mock
    import ingestor
    import work_store
    import workbench_server

    now = 1762450000.0
    store = work_store.WorkStore(self.db_path)

    cl_state = {
        "items": {
            "gerrit:1404137@c1": {
                "item_key": "gerrit:1404137",
                "kind": "gerrit",
                "target_id": "1404137",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "patchset": 9,
                "is_wip": False,
                "merge_conflict": True,
                "cr_summary": "+1",
                "reviewer_status": "Ready to submit",
                "assigned_reviewers_count": 2,
                "subject": "[skparagraph] Do not letter-space default-ignorable clusters",
                "url": "https://skia-review.googlesource.com/c/skia/+/1404137",
                "linked_bugs": [{
                    "bug_key": "gh_issue:flutter/flutter#160251",
                    "short_id": "flutter#160251",
                    "title": "Zero-width character (\\u200b) does not have zero-width",
                    "status": "OPEN",
                    "url": "https://github.com/flutter/flutter/issues/160251",
                }],
                "entity_state": {
                    "status": "conflict",
                    "lane": "you",
                    "spine_short_state": {"short": "Conflict", "icon": "⚠️"},
                },
            },
            "gerrit:1414016@c2": {
                "item_key": "gerrit:1414016",
                "kind": "gerrit",
                "target_id": "1414016",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "patchset": 1,
                "is_wip": False,
                "merge_conflict": False,
                "cr_summary": "+1 (1/2)",
                "reviewer_status": "Needs +1 CR (1/2 approved)",
                "assigned_reviewers_count": 2,
                "subject": "[skparagraph] Measure the hit glyph with its own justification shift",
                "url": "https://skia-review.googlesource.com/c/skia/+/1414016",
                "stack_ikeys": ["gerrit:1414016", "gerrit:1414017"],
                "linked_bugs": [{
                    "bug_key": "gh_issue:flutter/flutter#155062",
                    "short_id": "flutter#155062",
                    "title": "TextPainter.getPositionForOffset doesn't return correct text position",
                    "status": "CLOSED",
                    "url": "https://github.com/flutter/flutter/issues/155062",
                }],
                "entity_state": {"status": "waiting", "lane": "waiting"},
            },
            "gerrit:1414017@c2": {
                "item_key": "gerrit:1414017",
                "kind": "gerrit",
                "target_id": "1414017",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "patchset": 1,
                "is_wip": False,
                "merge_conflict": False,
                "cr_summary": "+1 (1/2)",
                "reviewer_status": "Needs +1 CR (1/2 approved)",
                "assigned_reviewers_count": 2,
                "subject": "[skparagraph] Shift the trailing space of an RTL run when justifying",
                "url": "https://skia-review.googlesource.com/c/skia/+/1414017",
                "stack_parent_ikey": "gerrit:1414016",
                "stack_ikeys": ["gerrit:1414016", "gerrit:1414017"],
                "linked_bugs": [{
                    "bug_key": "gh_issue:flutter/flutter#84030",
                    "short_id": "flutter#84030",
                    "title": "",
                    "status": "OPEN",
                    "url": "https://github.com/flutter/flutter/issues/84030",
                }],
                "entity_state": {"status": "waiting", "lane": "waiting"},
            },
            "gerrit:1420016@c3": {
                "item_key": "gerrit:1420016",
                "kind": "gerrit",
                "target_id": "1420016",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "patchset": 5,
                "is_wip": True,
                "wip_label": "WIP",
                "merge_conflict": True,
                "cr_summary": "0",
                "reviewer_status": "No reviewers assigned",
                "assigned_reviewers_count": 0,
                "subject": "[skparagraph] Unify span background height across fallback runs",
                "url": "https://skia-review.googlesource.com/c/skia/+/1420016",
                "commit_message": "Part of https://github.com/flutter/flutter/issues/40936",
                "entity_state": {"status": "draft", "lane": "desk"},
            },
            "github:196137@c3": {
                "item_key": "github:196137",
                "kind": "github",
                "target_id": "196137",
                "status": "OPEN",
                "patchset": 1,
                "is_wip": True,
                "is_draft": True,
                "wip_label": "Draft",
                "merge_conflict": False,
                "cr_summary": "0 rev",
                "reviewer_status": "No reviewers assigned",
                "assigned_reviewers_count": 0,
                "subject": "Reuse matching PaintIDs in ParagraphBuilderSkia::CreatePaintID",
                "url": "https://github.com/flutter/flutter/pull/196137",
                "commit_message": "Fixes https://github.com/flutter/flutter/issues/40936",
                "entity_state": {"status": "draft", "lane": "desk"},
            },
            "gerrit:1417037@c4": {
                "item_key": "gerrit:1417037",
                "kind": "gerrit",
                "target_id": "1417037",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "patchset": 4,
                "is_wip": True,
                "wip_label": "WIP",
                "merge_conflict": False,
                "cr_summary": "0",
                "reviewer_status": "No reviewers assigned",
                "assigned_reviewers_count": 0,
                "subject": "[skparagraph] Split TrueType composite glyphs across foreground styles",
                "url": "https://skia-review.googlesource.com/c/skia/+/1417037",
                "linked_bugs": [{
                    "bug_key": "gh_issue:flutter/flutter#141080",
                    "short_id": "flutter#141080",
                    "title": "Cannot properly apply colour to individual characters in Text.rich",
                    "status": "OPEN",
                    "url": "https://github.com/flutter/flutter/issues/141080",
                }],
                "entity_state": {"status": "draft", "lane": "desk"},
            },
            "gerrit:1418478@c5": {
                "item_key": "gerrit:1418478",
                "kind": "gerrit",
                "target_id": "1418478",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "patchset": 6,
                "is_wip": True,
                "wip_label": "WIP",
                "merge_conflict": True,
                "cr_summary": "0",
                "reviewer_status": "No reviewers assigned",
                "assigned_reviewers_count": 0,
                "subject": "[skparagraph] Keep signed span advance and cursive ligatures across styles",
                "url": "https://skia-review.googlesource.com/c/skia/+/1418478",
                "commit_message": "Bug: https://github.com/flutter/flutter/issues/110875",
                "entity_state": {"status": "draft", "lane": "desk"},
            },
        },
        "bugs": {},
    }

    ingestor.ingest_cl_watcher_state(store, cl_state, now=now)

    draft_html = """<div dir="ltr"><p>Hi Renzo,</p>
    <ol>
      <li><b><a href="https://github.com/flutter/flutter/issues/160251">#160251</a> — <i>Zero-width character (\\u200b) does not have zero-width</i></b> (<a href="https://skia-review.googlesource.com/c/skia/+/1404137">CL 1404137</a>)</li>
      <li><b><a href="https://github.com/flutter/flutter/issues/155062">#155062</a> — <i>TextPainter.getPositionForOffset doesn't return correct text position</i></b> (<a href="https://skia-review.googlesource.com/c/skia/+/1414016">CL 1414016</a>, stacked with <a href="https://skia-review.googlesource.com/c/skia/+/1414017">CL 1414017</a>)</li>
      <li><b><a href="https://github.com/flutter/flutter/issues/40936">#40936</a> — <i>TextSpan backgroundColor with special Arabic character are not displayed correctly</i></b> (Fix in progress)</li>
      <li><b><a href="https://github.com/flutter/flutter/issues/141080">#141080</a> — <i>Cannot properly apply colour to individual characters in Text.rich</i></b> (<a href="https://skia-review.googlesource.com/c/skia/+/1417037">CL 1417037</a>)</li>
      <li><b><a href="https://github.com/flutter/flutter/issues/110875">#110875</a> — <i>Arabic text having duplicate letters when having different color in RichText</i></b> (Investigation in progress, comparing with <a href="https://skia-review.googlesource.com/c/skia/+/1417037">CL 1417037</a>)</li>
    </ol></div>"""
    b64_body = base64.urlsafe_b64encode(draft_html.encode("utf-8")).decode("ascii")

    def fake_run(cmd, *args, **kwargs):
      del args, kwargs
      res = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
      if "drafts" in cmd:
        res.stdout = json.dumps({"drafts": [{"id": "r2068112471352956917", "threadId": "1a13c69d14fe1c3d"}]})
      elif "get-draft" in cmd:
        res.stdout = json.dumps({
            "id": "r2068112471352956917",
            "message": {
                "threadId": "1a13c69d14fe1c3d",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Re: Fixed bugs in Flutter"},
                        {"name": "To", "value": "Renzo Olivares <roliv@google.com>"},
                    ],
                    "body": {"data": b64_body},
                },
            },
        })
      elif "get-thread" in cmd:
        res.stdout = json.dumps({
            "messages": [{
                "internalDate": "1760538903000",
                "snippet": "Hi Renzo, fixed bugs list",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Fixed bugs in Flutter"},
                        {"name": "From", "value": "Julia Lavrova <jlavrova@google.com>"},
                        {"name": "To", "value": "Renzo Olivares <roliv@google.com>"},
                    ]
                },
            }]
        })
      elif "search" in cmd:
        res.stdout = json.dumps({"messages": []})
      return res

    with mock.patch("os.path.isfile", return_value=True), mock.patch("subprocess.run", side_effect=fake_run):
      ingestor.ingest_gmail_drafts_and_threads(store, {}, now=now)

    proj = store.build_dashboard_projection(cl_watcher_state=cl_state, now=now)
    renzo_cards = [w for w in proj["works"] if "Fixed bugs in Flutter" in w["title"]]
    self.assertEqual(len(renzo_cards), 1)
    card = renzo_cards[0]

    # 1. Single consolidated obligation instead of 4 per-CL obligations
    open_obs = [o for o in card["obligations"] if o["status"] != "done"]
    self.assertEqual(len(open_obs), 1)
    self.assertIn("progress_summary", open_obs[0])

    # 2. All 5 bugs appear in card["checklist"] in draft order with their mapped CLs
    checklist = card.get("checklist") or []
    self.assertEqual(
        [item["short_id"] for item in checklist],
        ["flutter#160251", "flutter#155062", "flutter#40936", "flutter#141080", "flutter#110875"],
    )
    by_bug = {item["short_id"]: item for item in checklist}
    self.assertEqual([c["artifact_key"] for c in by_bug["flutter#160251"]["cls"]], ["gerrit:skia:1404137"])
    self.assertEqual(by_bug["flutter#160251"]["stage"], "conflict")
    self.assertFalse(by_bug["flutter#160251"]["draft_outdated"])

    self.assertEqual(
        [c["artifact_key"] for c in by_bug["flutter#155062"]["cls"]],
        ["gerrit:skia:1414016", "gerrit:skia:1414017"],
    )
    self.assertEqual(by_bug["flutter#155062"]["stage"], "review")
    self.assertFalse(by_bug["flutter#155062"]["draft_outdated"])

    self.assertCountEqual(
        [c["artifact_key"] for c in by_bug["flutter#40936"]["cls"]],
        ["gerrit:skia:1420016", "github_pr:flutter/flutter:196137"],
    )
    self.assertTrue(by_bug["flutter#40936"]["draft_outdated"])

    self.assertEqual([c["artifact_key"] for c in by_bug["flutter#141080"]["cls"]], ["gerrit:skia:1417037"])
    self.assertEqual(by_bug["flutter#141080"]["stage"], "wip")
    self.assertFalse(by_bug["flutter#141080"]["draft_outdated"])

    self.assertEqual([c["artifact_key"] for c in by_bug["flutter#110875"]["cls"]], ["gerrit:skia:1418478"])
    self.assertTrue(by_bug["flutter#110875"]["draft_outdated"])

    # 3. Lane is 'needs-you' due to Gerrit 1404137 conflict (not due to parked draft)
    self.assertEqual(card["lane"], "needs-you")
    self.assertTrue(any("1404137" in r for r in card["reasons"]))
    self.assertFalse(any("Unsent draft" in r for r in card["reasons"]))

    html = workbench_server.render_workbench_html(proj, 1)
    self.assertIn("wb-checklist", html)
    self.assertNotIn("eval:false", html)


if __name__ == "__main__":
  unittest.main()
