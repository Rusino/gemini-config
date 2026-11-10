#!/usr/bin/env python3
"""Contract unit tests for Workbench (work_store, ingestor, workctl, workbench_server)."""

from __future__ import annotations

import base64
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
      self.assertIn("wb-toggle-collapse", html)
      self.assertNotIn("wb-expand-all", html)
      self.assertNotIn("wb-collapse-all", html)
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
    self.assertIn("wb-hdr-stack", html)
    self.assertIn("wb-origin-row", html)
    self.assertRegex(html, r"\.wb-pills\s*\{[^}]*flex-direction:\s*column")
    self.assertRegex(html, r"\.wb-chk-cls\s*\{[^}]*flex-direction:\s*column")
    self.assertNotIn("eval:false", html)

  def test_exclude_config_works_and_config_artifacts_from_cards(self) -> None:
    import ingestor
    import work_store

    now = 1762450000.0
    self.assertTrue(work_store.is_config_project("04508c43-54a0-4003-8051-7129cd997b88"))
    self.assertTrue(work_store.is_config_project("5a2a0f80"))
    self.assertTrue(work_store.is_config_project("Improving Experience"))
    self.assertTrue(work_store.is_config_project("Flight Deck"))
    self.assertTrue(work_store.is_config_project("My Work"))
    self.assertFalse(work_store.is_config_project("skia"))
    self.assertFalse(work_store.is_config_project("8acb67c4-2cb7-43ca-ba04-dc547b3c4788"))

    self.assertTrue(work_store.is_config_text("Задача R16 в проекте Improving Experience"))
    self.assertTrue(work_store.is_config_text("R112 Clickable links and Gerrit auto-title"))
    self.assertTrue(work_store.is_config_text("Flight Deck section collapse vs tabs (R62)"))
    self.assertTrue(
        work_store.is_config_text("Почини генерацию заголовков ревью-чатов в Flight Deck")
    )
    self.assertTrue(
        work_store.is_config_text(
            "Используй скилл ui-variants (/usr/local/google/home/jlavrova/.gemini/config)"
        )
    )
    self.assertFalse(
        work_store.is_config_text(
            "[FLIGHT-DECK AUDIT: Gerrit 1420017 PS2] Measure hit glyph"
        )
    )
    self.assertFalse(
        work_store.is_config_text("Flight-Deck Tier 2 Audit: Gerrit 1414017 PS1")
    )

    store = work_store.WorkStore(self.db_path)
    # 1. Existing product Work for Gerrit 1414016
    w_prod = store.create_work(
        "1414016: [skparagraph] Measure the hit glyph with its own justification shift",
        project_id="skia",
        now=now,
    )
    store.upsert_artifact(
        "gerrit:skia:1414016",
        kind="gerrit",
        title="1414016: [skparagraph] Measure the hit glyph with its own justification shift",
        url="https://skia-review.googlesource.com/c/skia/+/1414016",
        state={"status": "NEW"},
        now=now,
    )
    store.link_artifact(w_prod, "gerrit:skia:1414016", role="primary", source="auto", now=now)

    # 2. Config transcript in project 'Improving Experience' citing Gerrit 1414016 must be ignored
    cfg_transcript = os.path.join(self.tmp_dir.name, "cfg_conv.jsonl")
    with open(cfg_transcript, "w", encoding="utf-8") as f:
      f.write(
          json.dumps({
              "type": "USER_INPUT",
              "content": "В R112 проверим авто-заголовок на примере Gerrit 1414016",
          })
          + "\n"
      )
      f.write(
          json.dumps({
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "edit_file",
                  "args": {
                      "Path": "/usr/local/google/home/jlavrova/.gemini/config/hooks/stop_guard.py"
                  },
              }],
          })
          + "\n"
      )
    ingested_cfg = ingestor.ingest_transcript_file(
        store,
        conv_id="bb4e75ac-5396-4d7c-a095-19bd161047d3",
        root_cid="bb4e75ac-5396-4d7c-a095-19bd161047d3",
        transcript_path=cfg_transcript,
        cursors={},
        chat_title="R112 Clickable links and Gerrit auto-title",
        project_id="04508c43-54a0-4003-8051-7129cd997b88",
        now=now,
    )
    self.assertFalse(ingested_cfg)
    self.assertNotIn(
        "chat_chain:bb4e75ac-5396-4d7c-a095-19bd161047d3",
        store.get_work_artifact_keys(w_prod),
    )

    # 3. Auto-linking a config chat to a product Work is blocked at link_artifact gate
    store.upsert_artifact(
        "chat_chain:ea2b5a0f-4a39-4bd6-a6aa-3cc940b6aa3c",
        kind="chat_chain",
        title="Задача R16 в проекте Improving Experience",
        url="/c/ea2b5a0f-4a39-4bd6-a6aa-3cc940b6aa3c",
        now=now,
    )
    self.assertIsNone(
        store.link_artifact(
            w_prod,
            "chat_chain:ea2b5a0f-4a39-4bd6-a6aa-3cc940b6aa3c",
            role="channel",
            source="auto",
            now=now,
        )
    )

    # 4. Pre-existing legacy DB pollution cleanup via purge_config_artifacts_and_works
    w_cfg_only = store.create_work(
        "Flight Deck section collapse vs tabs (R62)",
        project_id="5a2a0f80-96c2-4334-a58d-7631310d4f72",
        now=now,
    )
    store.upsert_artifact(
        "chat_chain:5fa30fcf-89ad-4fc8-8fb4-1990acbc9041",
        kind="chat_chain",
        title="Flight Deck section collapse vs tabs (R62)",
        url="/c/5fa30fcf-89ad-4fc8-8fb4-1990acbc9041",
        now=now,
    )
    w_polluted = store.create_work(
        "R112 Clickable links and Gerrit auto-title",
        project_id="04508c43-54a0-4003-8051-7129cd997b88",
        now=now,
    )
    store.upsert_artifact(
        "github_pr:flutter/flutter:195122",
        kind="github_pr",
        title="195122: [web_paragraph] Avoid browser text metrics queries on re-layout",
        url="https://github.com/flutter/flutter/pull/195122",
        state={"status": "OPEN"},
        now=now,
    )
    store.upsert_artifact(
        "chat_chain:489a12df-4f26-4261-a4a9-171080ad3b1a",
        kind="chat_chain",
        title="Review PR #195122 web_paragraph metrics",
        url="/c/489a12df-4f26-4261-a4a9-171080ad3b1a",
        now=now,
    )
    store.link_artifact(
        w_polluted, "github_pr:flutter/flutter:195122", role="primary", source="auto", now=now
    )
    store.link_artifact(
        w_polluted,
        "chat_chain:489a12df-4f26-4261-a4a9-171080ad3b1a",
        role="channel",
        source="auto",
        now=now,
    )
    # Simulate pre-R6 legacy links already stored in work_links before the gate existed
    with store._connect() as conn:
      conn.execute(
          "INSERT INTO work_links (work_id, artifact_key, role, source, confidence, created_at) "
          "VALUES (?, ?, 'channel', 'auto', 1.0, ?), (?, ?, 'channel', 'auto', 1.0, ?)",
          (
              w_cfg_only,
              "chat_chain:5fa30fcf-89ad-4fc8-8fb4-1990acbc9041",
              now,
              w_polluted,
              "chat_chain:ea2b5a0f-4a39-4bd6-a6aa-3cc940b6aa3c",
              now,
          ),
      )

    purged = store.purge_config_artifacts_and_works(now=now)
    self.assertGreaterEqual(purged["works_archived"], 1)
    self.assertGreaterEqual(purged["links_removed"], 2)
    self.assertGreaterEqual(purged["works_repaired"], 1)

    proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)
    wids = {w["work_id"] for w in proj["works"]}
    self.assertNotIn(w_cfg_only, wids)
    self.assertIn(w_polluted, wids)
    repaired = [w for w in proj["works"] if w["work_id"] == w_polluted][0]
    self.assertEqual(repaired["project_id"], "")
    self.assertNotIn("R112", repaired["title"])
    self.assertIn("195122", repaired["title"])
    art_keys = [a["artifact_key"] for a in repaired["artifacts"]]
    self.assertNotIn("chat_chain:ea2b5a0f-4a39-4bd6-a6aa-3cc940b6aa3c", art_keys)
    self.assertIn("chat_chain:489a12df-4f26-4261-a4a9-171080ad3b1a", art_keys)

  def test_link_provenance_and_no_mid_chat_cooccurrence_bridging(self) -> None:
    import ingestor
    import work_store
    import workbench_server

    store = work_store.WorkStore(db_path=self.db_path)
    now = 1794010000.0
    cursors: dict[str, dict[str, float | int]] = {}

    # 1. Review chat b771ef3c on Gerrit 1408158 that uploads companion WIP CL 1406080 in step 1
    cid_review = "b771ef3c-b77f-4c91-a6ce-09209da6fd95"
    tpath_review = os.path.join(
        self.tmp_dir.name, cid_review, ".system_generated", "logs", "transcript.jsonl"
    )
    os.makedirs(os.path.dirname(tpath_review), exist_ok=True)
    with open(tpath_review, "w", encoding="utf-8") as f:
      f.write(
          json.dumps({
              "step_index": 0,
              "type": "USER_INPUT",
              "created_at": "2026-10-28T14:00:00Z",
              "content": (
                  "Код ревью @[skia | 1408158]"
                  "(https://skia-review.googlesource.com/c/skia/+/1408158)"
              ),
          })
          + "\n"
      )
      f.write(
          json.dumps({
              "step_index": 1,
              "type": "PLANNER_RESPONSE",
              "created_at": "2026-10-28T14:10:00Z",
              "content": (
                  "Я загрузила companion WIP CL: "
                  "https://skia-review.googlesource.com/c/skia/+/1406080 "
                  "(SkParagraph_BuilderResetKeepsBuiltText)"
              ),
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "python3 ~/.gemini/config/scripts/gerrit_draft.py "
                          "save --change 1408158 --message 'LGTM'"
                      )
                  },
              }],
          })
          + "\n"
      )

    ingestor.ingest_transcript_file(
        store,
        conv_id=cid_review,
        root_cid=cid_review,
        transcript_path=tpath_review,
        cursors=cursors,
        chat_title="Код ревью @[skia | 1408158:[skparagraph] Use std::string for the builder text]",
        now=now,
    )

    # 2. Planning chat cfdcb811 on flutter#90063 that mentions 1409696 in planner text
    # and asks about 1408158 in a later user turn (step 2 > 0)
    cid_plan = "cfdcb811-6aca-4ae3-ba88-cbadbb3162a7"
    tpath_plan = os.path.join(
        self.tmp_dir.name, cid_plan, ".system_generated", "logs", "transcript.jsonl"
    )
    os.makedirs(os.path.dirname(tpath_plan), exist_ok=True)
    with open(tpath_plan, "w", encoding="utf-8") as f:
      f.write(
          json.dumps({
              "step_index": 0,
              "type": "USER_INPUT",
              "created_at": "2026-10-30T10:00:00Z",
              "content": (
                  "Задача R42 из roadmap проекта SkParagraph: "
                  "составить ПОДРОБНЫЙ ПЛАН (flutter#90063)"
              ),
          })
          + "\n"
      )
      f.write(
          json.dumps({
              "step_index": 1,
              "type": "PLANNER_RESPONSE",
              "created_at": "2026-10-30T10:05:00Z",
              "content": (
                  "Для справки см. соседний CL "
                  "https://skia-review.googlesource.com/c/skia/+/1409696"
              ),
              "tool_calls": [{
                  "name": "write_file",
                  "args": {"Path": "/work/skia/modules/skparagraph/src/ParagraphImpl.cpp"},
              }],
          })
          + "\n"
      )
      f.write(
          json.dumps({
              "step_index": 2,
              "type": "USER_INPUT",
              "created_at": "2026-10-30T11:00:00Z",
              "content": (
                  "И что ты в результате сделала, если уже есть "
                  "[skia | 1408158](https://skia-review.googlesource.com/c/skia/+/1408158) "
                  "от YeungKC?"
              ),
          })
          + "\n"
      )

    ingestor.ingest_transcript_file(
        store,
        conv_id=cid_plan,
        root_cid=cid_plan,
        transcript_path=tpath_plan,
        cursors=cursors,
        chat_title="",
        now=now + 10,
    )

    # 3. cl_watcher_state brings in Gerrit 1407016 linked to flutter#90063 and Gerrit 1408158
    cl_state = {
        "items": {
            "gerrit:1408158": {
                "kind": "gerrit",
                "target_id": "1408158",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "subject": "[skparagraph] Use std::string for the builder text",
                "url": "https://skia-review.googlesource.com/c/skia/+/1408158",
                "conversation_id": cid_review,
            },
            "gerrit:1407016": {
                "kind": "gerrit",
                "target_id": "1407016",
                "host": "skia-review.googlesource.com",
                "status": "MERGED",
                "subject": "[skparagraph] Keep line breaking of long words linear",
                "url": "https://skia-review.googlesource.com/c/skia/+/1407016",
                "conversation_id": "9db15645-df20-4126-aec2-c753c45eefe1",
                "linked_bugs": [{
                    "bug_key": "gh_issue:flutter/flutter#90063",
                    "short_id": "flutter#90063",
                    "title": "Long line breaking O(N^2) hang",
                    "status": "OPEN",
                    "url": "https://github.com/flutter/flutter/issues/90063",
                }],
            },
        }
    }
    ingestor.ingest_cl_watcher_state(store, cl_state, now=now + 20)

    proj = store.build_dashboard_projection(cl_watcher_state=cl_state, now=now + 30)
    works_by_primary: dict[str, dict[str, Any]] = {}
    for w in proj["works"]:
      for a in w["artifacts"]:
        if a["role"] == "primary":
          works_by_primary[a["artifact_key"]] = w

    self.assertIn("gerrit:skia:1408158", works_by_primary)
    self.assertIn("github_issue:flutter/flutter:90063", works_by_primary)
    w_1408158 = works_by_primary["gerrit:skia:1408158"]
    w_90063 = works_by_primary["github_issue:flutter/flutter:90063"]

    # Crucial invariant: mid-chat mention of 1408158 at step 2 of cfdcb811 must NOT merge the two works
    self.assertNotEqual(w_1408158["work_id"], w_90063["work_id"])

    arts_1408158 = {a["artifact_key"]: a for a in w_1408158["artifacts"]}
    self.assertIn("gerrit:skia:1406080", arts_1408158)
    self.assertNotIn("gerrit:skia:1407016", arts_1408158)
    self.assertNotIn("github_issue:flutter/flutter:90063", arts_1408158)
    self.assertEqual(arts_1408158["gerrit:skia:1406080"]["link_reason"], "companion_wip")
    self.assertIn("1408158", arts_1408158["gerrit:skia:1406080"]["why_badge"])

    arts_90063 = {a["artifact_key"]: a for a in w_90063["artifacts"]}
    self.assertIn("gerrit:skia:1407016", arts_90063)
    self.assertEqual(arts_90063["gerrit:skia:1407016"]["role"], "primary")
    self.assertIn("90063", arts_90063["gerrit:skia:1407016"]["why_badge"])
    # Both 1408158 and 1409696 are stored as context_only chat mentions on w_90063
    self.assertEqual(arts_90063["gerrit:skia:1408158"]["role"], "context_only")
    self.assertEqual(arts_90063["gerrit:skia:1408158"]["link_reason"], "chat_mention")
    self.assertEqual(arts_90063["gerrit:skia:1409696"]["role"], "context_only")
    self.assertEqual(arts_90063["gerrit:skia:1409696"]["link_reason"], "chat_mention")

    # Verify HTML rendering includes .wb-why badges and collapsible <details class="wb-mentions">
    html_out = workbench_server.render_workbench_html(proj, ui_version=1)
    self.assertIn("wb-why", html_out)
    self.assertIn("companion WIP к 1408158", html_out)
    self.assertIn("wb-mentions", html_out)
    self.assertIn("📎 Упомянуто в чате (2)", html_out)

  def test_hybrid_correspondence_first_cards_and_full_body_ingestor(self) -> None:
    import base64
    from unittest import mock
    import ingestor
    import work_store
    import workbench_server

    now = 1762475000.0
    store = work_store.WorkStore(db_path=self.db_path)

    extracted = ingestor.extract_artifacts_from_text(
        "See http://cl/829409333 and CL 785449435 "
        "and gmail readonly get-thread 1a1acda5f4631fb4 --json"
    )
    ext_keys = [k for k, _, _ in extracted]
    self.assertIn("piper:829409333", ext_keys)
    self.assertIn("piper:785449435", ext_keys)
    self.assertIn("gmail_thread:1a1acda5f4631fb4", ext_keys)

    w_issue_only = store.create_work("github_issue:flutter/flutter:185368", now=now - 3600)
    store.upsert_artifact(
        "github_issue:flutter/flutter:185368",
        kind="github_issue",
        title="flutter#185368: Engine benchmarks are no longer running after monorepo",
        url="https://github.com/flutter/flutter/issues/185368",
        state={"status": "OPEN"},
        now=now - 3600,
    )
    store.link_artifact(
        w_issue_only,
        "github_issue:flutter/flutter:185368",
        role="primary",
        source="auto",
        now=now - 3600,
    )
    store.purge_config_artifacts_and_works(now=now - 3500)
    active_wids = {w["work_id"] for w in store.list_works()}
    self.assertIn(w_issue_only, active_wids)

    w_span = store.create_work(
        "1414543: [core] Make SkSpan ctor checks debug-only", now=now - 7200
    )
    store.upsert_artifact(
        "gerrit:skia:1414543",
        kind="gerrit",
        title="1414543: [core] Make SkSpan ctor checks debug-only",
        url="https://skia-review.googlesource.com/c/skia/+/1414543",
        state={"status": "MERGED", "origin_ts": now - 7200},
        now=now - 7200,
    )
    store.link_artifact(
        w_span, "gerrit:skia:1414543", role="primary", source="auto", now=now - 7200
    )

    w_nano = store.create_work(
        "1414546: [skparagraph] Revive SkParagraph nanobench", now=now - 7000
    )
    store.upsert_artifact(
        "gerrit:skia:1414546",
        kind="gerrit",
        title="1414546: [skparagraph] Revive SkParagraph nanobench",
        url="https://skia-review.googlesource.com/c/skia/+/1414546",
        state={"status": "MERGED", "origin_ts": now - 7000},
        now=now - 7000,
    )
    store.link_artifact(
        w_nano, "gerrit:skia:1414546", role="primary", source="auto", now=now - 7000
    )

    w_open1 = store.create_work("1414016: Measure hit glyph", now=now - 80000)
    store.upsert_artifact(
        "gerrit:skia:1414016",
        kind="gerrit",
        title="1414016: Measure hit glyph",
        url="https://skia-review.googlesource.com/c/skia/+/1414016",
        state={"status": "NEW", "origin_ts": now - 80000},
        now=now - 80000,
    )
    store.link_artifact(
        w_open1, "gerrit:skia:1414016", role="primary", source="auto", now=now - 80000
    )

    w_open2 = store.create_work("1420016: Unify span background height", now=now - 75000)
    store.upsert_artifact(
        "gerrit:skia:1420016",
        kind="gerrit",
        title="1420016: Unify span background height",
        url="https://skia-review.googlesource.com/c/skia/+/1420016",
        state={"status": "NEW", "origin_ts": now - 75000},
        now=now - 75000,
    )
    store.link_artifact(
        w_open2, "gerrit:skia:1420016", role="primary", source="auto", now=now - 75000
    )

    def _b64(s: str) -> str:
      return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")

    # Multipart HTML body where links exist only in the decoded HTML part, not the snippet
    skspan_msg1_html = (
        '<p>We filed <a href="https://github.com/flutter/flutter/issues/185368">'
        "flutter#185368</a> and landed "
        '<a href="https://skia-review.googlesource.com/c/skia/+/1414543">1414543</a>.</p>'
    )
    skspan_msg3_html = (
        '<p>Also revived nanobench in '
        '<a href="https://skia-review.googlesource.com/c/skia/+/1414546">1414546</a>.</p>'
        "<blockquote>On Nov 5 Aaron wrote: old quote with Gerrit 9999999</blockquote>"
    )
    gh_issue_msg_html = (
        "<p>@Rusino Actually one thing that is preventing running these is Firebase TestLab.</p>"
        '<p><a href="https://github.com/flutter/flutter/issues/185368#issuecomment-6694545581">'
        "View it on GitHub</a></p>"
    )
    digest_msg_html = (
        "<p>Summary of 4 bugs and 2 open CLs: "
        "flutter#160251, flutter#155062, flutter#40936, flutter#141080, "
        "Gerrit 1414016, Gerrit 1420016.</p>"
    )

    seen_cmds: list[list[str]] = []

    def fake_gmail_run(cmd, *args, **kwargs):
      del args, kwargs
      seen_cmds.append(list(cmd))
      # Guard against invalid --max-results flag
      for token in cmd:
        if token.startswith("--max-results"):
          return subprocess.CompletedProcess(
              cmd, 1, stdout="", stderr="unknown flag --max-results"
          )
      res = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
      if "drafts" in cmd:
        res.stdout = json.dumps({"drafts": []})
      elif "search" in cmd:
        q = " ".join(cmd)
        if "from:me" in q:
          res.stdout = json.dumps({
              "messages": [
                  {"id": "m1", "threadId": "1a1acda5f4631fb4"},
                  {"id": "m2", "threadId": "1a19f47b74ee2fb4"},
                  {"id": "m3", "threadId": "1a1aa99999999999"},
              ]
          })
        elif "notifications@github.com" in q:
          res.stdout = json.dumps({
              "messages": [
                  {"id": "m4", "threadId": "1a1adc4087cc656b"},
              ]
          })
        else:
          res.stdout = json.dumps({"messages": []})
      elif "get-thread" in cmd:
        tid = cmd[cmd.index("get-thread") + 1]
        if tid == "1a1acda5f4631fb4":
          res.stdout = json.dumps({
              "messages": [
                  {
                      "internalDate": str(int((now - 10000) * 1000)),
                      "snippet": "Short 160-char snippet with no URLs at all",
                      "payload": {
                          "mimeType": "multipart/alternative",
                          "headers": [
                              {
                                  "name": "Subject",
                                  "value": (
                                      "Engine benchmarks (txt_benchmarks) never reach the Perf "
                                      "dashboard: a 20% SkParagraph regression went unnoticed"
                                  ),
                              },
                              {"name": "From", "value": "Julia Lavrova <jlavrova@google.com>"},
                              {
                                  "name": "To",
                                  "value": (
                                      "Casey Hillers <codefu@google.com>, "
                                      "Jim Graham <jimgraham@google.com>"
                                  ),
                              },
                          ],
                          "parts": [
                              {"mimeType": "text/html", "body": {"data": _b64(skspan_msg1_html)}}
                          ],
                      },
                  },
                  {
                      "internalDate": str(int((now - 9500) * 1000)),
                      "snippet": "Thanks for flagging this",
                      "payload": {
                          "mimeType": "text/plain",
                          "headers": [
                              {
                                  "name": "Subject",
                                  "value": "Re: Engine benchmarks (txt_benchmarks)",
                              },
                              {"name": "From", "value": "Aaron Clarke <aaclarke@google.com>"},
                              {"name": "To", "value": "Julia Lavrova <jlavrova@google.com>"},
                          ],
                          "body": {"data": _b64("Thanks for flagging this")},
                      },
                  },
                  {
                      "internalDate": str(int((now - 9000) * 1000)),
                      "snippet": "Short snippet without 1414546",
                      "payload": {
                          "mimeType": "multipart/alternative",
                          "headers": [
                              {
                                  "name": "Subject",
                                  "value": "Re: Engine benchmarks (txt_benchmarks)",
                              },
                              {"name": "From", "value": "Julia Lavrova <jlavrova@google.com>"},
                              {"name": "To", "value": "Aaron Clarke <aaclarke@google.com>"},
                          ],
                          "parts": [
                              {"mimeType": "text/html", "body": {"data": _b64(skspan_msg3_html)}}
                          ],
                      },
                  },
              ]
          })
        elif tid == "1a1adc4087cc656b":
          res.stdout = json.dumps({
              "messages": [
                  {
                      "internalDate": str(int((now - 600) * 1000)),
                      "snippet": "Jim Graham commented on #185368",
                      "payload": {
                          "mimeType": "text/html",
                          "headers": [
                              {
                                  "name": "Subject",
                                  "value": (
                                      "Re: [flutter/flutter] Engine benchmarks are no longer "
                                      "running after monorepo (Issue #185368)"
                                  ),
                              },
                              {"name": "From", "value": "Jim Graham <notifications@github.com>"},
                              {
                                  "name": "To",
                                  "value": "flutter/flutter <flutter@noreply.github.com>",
                              },
                              {
                                  "name": "Cc",
                                  "value": (
                                      "Rusino <jlavrova@google.com>, "
                                      "Mention <mention@noreply.github.com>"
                                  ),
                              },
                          ],
                          "body": {"data": _b64(gh_issue_msg_html)},
                      },
                  }
              ]
          })
        elif tid == "1a19f47b74ee2fb4":
          res.stdout = json.dumps({
              "messages": [
                  {
                      "internalDate": str(int((now - 50000) * 1000)),
                      "snippet": "Hi Kaylee, here are my thoughts on ShapedText API",
                      "payload": {
                          "mimeType": "text/plain",
                          "headers": [
                              {"name": "Subject", "value": "Re: ShapedText"},
                              {"name": "From", "value": "Julia Lavrova <jlavrova@google.com>"},
                              {"name": "To", "value": "Kaylee Lubick <kjlubick@google.com>"},
                          ],
                          "body": {
                              "data": _b64("Hi Kaylee, here are my thoughts on ShapedText API.")
                          },
                      },
                  }
              ]
          })
        elif tid == "1a1aa99999999999":
          res.stdout = json.dumps({
              "messages": [
                  {
                      "internalDate": str(int((now - 1000) * 1000)),
                      "snippet": "Weekly status digest",
                      "payload": {
                          "mimeType": "text/html",
                          "headers": [
                              {"name": "Subject", "value": "Weekly status digest"},
                              {"name": "From", "value": "Julia Lavrova <jlavrova@google.com>"},
                              {"name": "To", "value": "Renzo Olivares <roliv@google.com>"},
                          ],
                          "body": {"data": _b64(digest_msg_html)},
                      },
                  }
              ]
          })
      return res

    w_state: dict[str, Any] = {"active_draft_ids": ["r999999999999999999"]}
    store.upsert_artifact(
        "gmail_draft:r999999999999999999",
        kind="gmail_draft",
        title="Draft: Old draft",
        state={"status": "DRAFT"},
        now=now - 100,
    )
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_gmail_run
    ):
      ingestor.ingest_gmail_drafts_and_threads(store, w_state, now=now)

    # Last disappearing draft must be marked SENT even when active_draft_ids is now empty
    gone_draft = store.get_artifact("gmail_draft:r999999999999999999")
    self.assertIsNotNone(gone_draft)
    self.assertEqual((gone_draft or {}).get("state", {}).get("status"), "SENT")

    proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)

    skspan_cards = [
        w
        for w in proj["works"]
        if any(a["artifact_key"] == "gerrit:skia:1414543" for a in w["artifacts"])
    ]
    self.assertEqual(len(skspan_cards), 1)
    sk_card = skspan_cards[0]
    self.assertIn("Engine benchmarks (txt_benchmarks)", sk_card["title"])
    sk_arts = {a["artifact_key"]: a for a in sk_card["artifacts"]}
    self.assertIn("gmail_thread:1a1acda5f4631fb4", sk_arts)
    self.assertIn("gmail_thread:1a1adc4087cc656b", sk_arts)
    self.assertIn("github_issue:flutter/flutter:185368", sk_arts)
    self.assertIn("gerrit:skia:1414543", sk_arts)
    self.assertIn("gerrit:skia:1414546", sk_arts)
    self.assertNotIn("gerrit:skia:9999999", sk_arts)
    self.assertEqual(sk_card["lane"], "needs-you")

    shaped_cards = [w for w in proj["works"] if "ShapedText" in w["title"]]
    self.assertEqual(len(shaped_cards), 1)
    self.assertEqual(
        shaped_cards[0]["artifacts"][0]["artifact_key"], "gmail_thread:1a19f47b74ee2fb4"
    )
    self.assertEqual(shaped_cards[0]["artifacts"][0]["why_badge"], "основа карточки")

    open1_card = [w for w in proj["works"] if w["work_id"] == w_open1][0]
    open2_card = [w for w in proj["works"] if w["work_id"] == w_open2][0]
    self.assertNotEqual(open1_card["work_id"], open2_card["work_id"])
    self.assertEqual(open1_card["title"], "1414016: Measure hit glyph")
    self.assertEqual(open2_card["title"], "1420016: Unify span background height")
    open1_arts = {a["artifact_key"]: a for a in open1_card["artifacts"]}
    self.assertIn("gmail_thread:1a1aa99999999999", open1_arts)
    self.assertEqual(open1_arts["gmail_thread:1a1aa99999999999"]["role"], "context_only")
    self.assertEqual(open1_arts["gmail_thread:1a1aa99999999999"]["why_badge"], "упомянуто в сводке")

    html_out = workbench_server.render_workbench_html(proj, ui_version=1)
    self.assertIn("wb-correspondence", html_out)
    self.assertIn("1414546", html_out)
    corr_pos = html_out.find("wb-correspondence")
    chk_pos = html_out.find("wb-checklist")
    self.assertGreater(corr_pos, 0)
    self.assertGreater(chk_pos, corr_pos)

  def test_unanswered_tray_structural_filter_and_dismiss(self) -> None:
    import base64
    from unittest import mock
    import ingestor
    import work_store
    import workbench_server

    store = work_store.WorkStore(self.db_path)
    now = 1762400000.0

    def _b64(s: str) -> str:
      return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")

    mass_to_9 = (
        ", ".join(f"eng{i}@google.com" for i in range(8))
        + ", Julia Lavrova <jlavrova@google.com>"
    )
    ok_cc_8 = ", ".join(f"col{i}@google.com" for i in range(6)) + ", jlavrova@google.com"
    threads_data = {
        "t_promo": {
            "messages": [{
                "internalDate": str(int((now - 1000) * 1000)),
                "labelIds": ["CATEGORY_PROMOTIONS", "IMPORTANT"],
                "snippet": "Early Black Friday Access: 10% Off Sitewide",
                "payload": {
                    "headers": [
                        {
                            "name": "Subject",
                            "value": "Early Black Friday Access: 10% Off Sitewide",
                        },
                        {
                            "name": "From",
                            "value": "Bokksu Boutique <hello@mail.bokksuboutique.com>",
                        },
                        {"name": "To", "value": "<jlavrova@google.com>"},
                        {
                            "name": "List-Unsubscribe",
                            "value": "<https://click.bokksuboutique.com/ocu/x>",
                        },
                        {"name": "Precedence", "value": "bulk"},
                    ],
                    "body": {"data": _b64("10% off")},
                },
            }]
        },
        "t_list_doc": {
            "messages": [{
                "internalDate": str(int((now - 1100) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_FORUMS", "IMPORTANT"],
                "snippet": "Document shared with chrome-eng",
                "payload": {
                    "headers": [
                        {
                            "name": "Subject",
                            "value": 'Document shared with you: "One-pager: Chrome Spoofer"',
                        },
                        {"name": "From", "value": "Terry Shi (via Google Docs) <tshi@google.com>"},
                        {"name": "To", "value": "chrome-eng@google.com"},
                        {"name": "List-Id", "value": "<chrome-eng.google.com>"},
                        {"name": "Precedence", "value": "list"},
                    ],
                    "body": {
                        "data": _b64(
                            '<a href="https://docs.google.com/document/d/'
                            '1abcDefGhiJklMnOpQrStUvWxYz0123456789/edit">Open doc</a>'
                        )
                    },
                },
            }]
        },
        "t_personal_doc_share": {
            "messages": [{
                "internalDate": str(int((now - 1120) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_PERSONAL", "IMPORTANT"],
                "snippet": "Terry Shi shared a document with you",
                "payload": {
                    "headers": [
                        {
                            "name": "Subject",
                            "value": 'Document shared with you: "Direct Doc Share Note"',
                        },
                        {"name": "From", "value": "Terry Shi (via Google Docs) <tshi@google.com>"},
                        {"name": "To", "value": "jlavrova@google.com"},
                    ],
                    "body": {
                        "data": _b64(
                            '<a href="https://docs.google.com/document/d/'
                            '1DirectShareDocIdWith44Characters0123456789/edit">Open</a>'
                        )
                    },
                },
            }]
        },
        "t_mass": {
            "messages": [{
                "internalDate": str(int((now - 1200) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_PERSONAL", "IMPORTANT"],
                "snippet": "Please add monthly updates in Taskflow",
                "payload": {
                    "headers": [
                        {
                            "name": "Subject",
                            "value": "October updates on H2 projects in Taskflow",
                        },
                        {"name": "From", "value": "Rakshit Sharma <sharaks@google.com>"},
                        {"name": "To", "value": mass_to_9},
                    ],
                    "body": {"data": _b64("Hi everyone, please update Taskflow.")},
                },
            }]
        },
        "t_crstatus": {
            "messages": [{
                "internalDate": str(int((now - 1300) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_UPDATES", "IMPORTANT"],
                "snippet": "Verify shipping feature",
                "payload": {
                    "headers": [
                        {
                            "name": "Subject",
                            "value": "[Action required] Verify shipping feature",
                        },
                        {
                            "name": "From",
                            "value": "Chromestatus <admin@cr-status.appspotmail.com>",
                        },
                        {"name": "To", "value": "jlavrova@google.com"},
                    ],
                    "body": {"data": _b64("Please verify your M159 feature.")},
                },
            }]
        },
        "t_notes": {
            "messages": [{
                "internalDate": str(int((now - 1400) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_PERSONAL", "IMPORTANT"],
                "snippet": "Meeting notes summary",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": 'Notes: "SkoreText/Flutter Sync"'},
                        {"name": "From", "value": "Gemini <gemini-notes@google.com>"},
                        {"name": "To", "value": "jlavrova@google.com"},
                    ],
                    "body": {"data": _b64("Summary of meeting notes.")},
                },
            }]
        },
        "t_calnote": {
            "messages": [{
                "internalDate": str(int((now - 1500) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_PERSONAL", "IMPORTANT"],
                "snippet": "Updated invitation with note",
                "payload": {
                    "headers": [
                        {
                            "name": "Subject",
                            "value": "Updated invitation with note: Mouad / Julia @ Wed 2:30pm",
                        },
                        {"name": "From", "value": "Mouad Debbar <mdebbar@google.com>"},
                        {"name": "To", "value": "jlavrova@google.com"},
                    ],
                    "body": {"data": _b64("Moving 30 mins later.")},
                },
            }]
        },
        "t_calcanceled": {
            "messages": [{
                "internalDate": str(int((now - 1510) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
                "snippet": "Canceled event with note",
                "payload": {
                    "headers": [
                        {
                            "name": "Subject",
                            "value": "Canceled event with note: Weekly Sync",
                        },
                        {"name": "From", "value": "Mouad Debbar <mdebbar@google.com>"},
                        {"name": "To", "value": "jlavrova@google.com"},
                    ],
                    "body": {"data": _b64("Rescheduling to next week.")},
                },
            }]
        },
        "t_no_category": {
            "messages": [{
                "internalDate": str(int((now - 1520) * 1000)),
                "labelIds": ["INBOX", "IMPORTANT"],
                "snippet": "Missing CATEGORY_PERSONAL label",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "System alert without category"},
                        {"name": "From", "value": "Team Bot <team@google.com>"},
                        {"name": "To", "value": "jlavrova@google.com"},
                    ],
                    "body": {"data": _b64("Uncategorized system alert.")},
                },
            }]
        },
        "t_mixed_cat": {
            "messages": [
                {
                    "internalDate": str(int((now - 1540) * 1000)),
                    "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
                    "snippet": "Initial personal ping",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Mixed category thread"},
                            {"name": "From", "value": "Alex <alex@google.com>"},
                            {"name": "To", "value": "jlavrova@google.com"},
                        ],
                        "body": {"data": _b64("Initial message")},
                    },
                },
                {
                    "internalDate": str(int((now - 1530) * 1000)),
                    "labelIds": ["INBOX", "CATEGORY_UPDATES"],
                    "snippet": "Follow-up update",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Re: Mixed category thread"},
                            {"name": "From", "value": "Tracker <updates@example.com>"},
                            {"name": "To", "value": "jlavrova@google.com"},
                        ],
                        "body": {"data": _b64("Automated update")},
                    },
                },
            ]
        },
        "t_organizers": {
            "messages": [{
                "internalDate": str(int((now - 1550) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
                "snippet": "Conference logistics",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "BlinkOn logistics"},
                        {"name": "From", "value": "BlinkOn <organizers@example.com>"},
                        {"name": "To", "value": "jlavrova@google.com"},
                    ],
                    "body": {"data": _b64("Welcome to BlinkOn.")},
                },
            }]
        },
        "t_not_to_me": {
            "messages": [{
                "internalDate": str(int((now - 1560) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
                "snippet": "Delivered via BCC/alias without me in To/Cc",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "FYI alias blast"},
                        {"name": "From", "value": "Peer <peer@google.com>"},
                        {"name": "To", "value": "skia-team-alias@google.com"},
                    ],
                    "body": {"data": _b64("Team announcement.")},
                },
            }]
        },
        "t_role_headers": {
            "messages": [{
                "internalDate": str(int((now - 1570) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
                "snippet": "Auto-submitted message",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Training reminder"},
                        {"name": "From", "value": "Ops <peopleops-help@google.com>"},
                        {"name": "To", "value": "jlavrova@google.com"},
                        {"name": "Auto-Submitted", "value": "auto-generated"},
                    ],
                    "body": {"data": _b64("Complete training.")},
                },
            }]
        },
        "t_archived": {
            "messages": [{
                "internalDate": str(int((now - 1600) * 1000)),
                "labelIds": ["CATEGORY_PERSONAL", "IMPORTANT"],
                "snippet": "Already archived personal note",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Old archived question"},
                        {"name": "From", "value": "Greg Daniel <egdaniel@google.com>"},
                        {"name": "To", "value": "jlavrova@google.com"},
                    ],
                    "body": {"data": _b64("Quick archived question")},
                },
            }]
        },
        "t_human_ok": {
            "messages": [{
                "internalDate": str(int((now - 500) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_PERSONAL", "IMPORTANT"],
                "snippet": "Can we align on TextClusters API tomorrow?",
                "payload": {
                    "headers": [
                        {
                            "name": "Subject",
                            "value": 'Re: "TextClusters" API" onmouseover="alert(1)',
                        },
                        {"name": "From", "value": "Florin Malita <fmalita@google.com>"},
                        {
                            "name": "To",
                            "value": (
                                "Mouad Debbar <mdebbar@google.com>, "
                                "Julia Lavrova <jlavrova@google.com>"
                            ),
                        },
                    ],
                    "body": {"data": _b64("Can we align on TextClusters API tomorrow?")},
                },
            }]
        },
        "t_human_cc8": {
            "messages": [{
                "internalDate": str(int((now - 550) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
                "snippet": "Fontations small-group thread at 8 recipients",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Fontations rollout question"},
                        {"name": "From", "value": "Dominik Röttsches <drott@chromium.org>"},
                        {"name": "To", "value": "bungeman@google.com"},
                        {"name": "Cc", "value": ok_cc_8},
                    ],
                    "body": {"data": _b64("Quick question about Fontations.")},
                },
            }]
        },
        "t_human_dismiss_me": {
            "messages": [{
                "internalDate": str(int((now - 600) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_PERSONAL", "IMPORTANT"],
                "snippet": "Coffee chat next week?",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Coffee chat next week?"},
                        {"name": "From", "value": "Heather Miller <hcm@google.com>"},
                        {"name": "To", "value": "Julia Lavrova <jlavrova@google.com>"},
                    ],
                    "body": {"data": _b64("Want to grab coffee next week?")},
                },
            }]
        },
    }

    # Pre-seed an old auto-leaked standalone un-replied promo Work (B6 regression check)
    leaked_wid = store.create_work("Early Black Friday Access: 10% Off Sitewide", now=now - 2000)
    store.upsert_artifact(
        "gmail_thread:t_promo",
        kind="gmail_thread",
        title="Early Black Friday Access",
        url="https://mail.google.com/mail/u/0/#all/t_promo",
        state={"sent_by_me": False},
        now=now - 2000,
    )
    store.link_artifact(
        leaked_wid,
        "gmail_thread:t_promo",
        role="primary",
        source="auto",
        link_reason="gmail_root",
        now=now - 2000,
    )

    inbound_queries: list[str] = []

    def fake_gmail_run(cmd, *args, **kwargs):
      del args, kwargs
      res = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
      if "drafts" in cmd:
        res.stdout = json.dumps({"drafts": []})
      elif "search" in cmd:
        q = " ".join(cmd)
        if "category:personal" in q or "newer_than:21d -from:noreply" in q:
          inbound_queries.append(q)
          res.stdout = json.dumps({
              "messages": [
                  {"id": f"m_{tid}", "threadId": tid} for tid in threads_data
              ]
          })
        else:
          res.stdout = json.dumps({"messages": []})
      elif "get-thread" in cmd:
        tid = cmd[cmd.index("get-thread") + 1]
        res.stdout = json.dumps(threads_data.get(tid, {"messages": []}))
      return res

    w_state: dict[str, Any] = {}
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_gmail_run
    ):
      tray = ingestor.ingest_gmail_drafts_and_threads(store, w_state, now=now)

    # 1. Inbound query enforces Inbox + Personal category
    self.assertTrue(inbound_queries)
    self.assertIn("in:inbox", inbound_queries[0])
    self.assertIn("category:personal", inbound_queries[0])

    # 2. Only the 3 genuine <=8-recipient INBOX/CATEGORY_PERSONAL human threads survive
    tray_tids = {t["thread_id"] for t in tray}
    self.assertEqual(tray_tids, {"t_human_ok", "t_human_cc8", "t_human_dismiss_me"})

    # 3. Drive shares & pre-existing ineligible standalone email Works must NOT stay in Works
    store.purge_config_artifacts_and_works(now=now)
    work_titles = [w["title"] for w in store.list_works()]
    self.assertFalse(
        any("Chrome Spoofer" in wt or "Direct Doc Share" in wt for wt in work_titles),
        f"Drive share thread leaked into Works: {work_titles}",
    )
    self.assertFalse(
        any("Black Friday" in wt for wt in work_titles),
        f"Pre-existing promo Work survived purge: {work_titles}",
    )

    # 4. workctl.py dismiss with a bare hex thread ID must normalize to gmail_thread:<tid>
    workctl_py = os.path.join(WORKBENCH_DIR, "workctl.py")
    cli_env = os.environ.copy()
    cli_env["WORKBENCH_DB_PATH"] = self.db_path
    subprocess.run(
        [sys.executable, workctl_py, "dismiss", "deadbeef00001111"],
        env=cli_env,
        capture_output=True,
        text=True,
        check=True,
    )
    self.assertIn("gmail_thread:deadbeef00001111", store.list_dismissed_tray_keys())

    # 5. Dismissing via API immediately filters projection and survives re-sync
    w_state["unanswered_tray"] = tray
    with open(self.state_path, "w", encoding="utf-8") as f:
      json.dump(w_state, f)
    with open(self.cl_state_path, "w", encoding="utf-8") as f:
      json.dump({"items": {}, "bugs": {}}, f)

    srv = workbench_server.create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        cl_state_path=self.cl_state_path,
        watcher_state_path=self.state_path,
        info_path=self.info_path,
    )
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
      req = urllib.request.Request(
          f"http://127.0.0.1:{port}/api/tray/dismiss",
          data=json.dumps({"artifact_key": "gmail_thread:t_human_dismiss_me"}).encode("utf-8"),
          headers={"Content-Type": "application/json"},
          method="POST",
      )
      with urllib.request.urlopen(req, timeout=5) as resp:
        self.assertEqual(resp.status, 200)
        after_dismiss = json.loads(resp.read().decode("utf-8"))

      self.assertEqual(
          {x["thread_id"] for x in after_dismiss["unanswered_tray"]},
          {"t_human_ok", "t_human_cc8"},
      )
      self.assertEqual(after_dismiss["counts"]["unanswered"], 2)

      # Re-sync must not re-introduce the dismissed thread
      with mock.patch("os.path.isfile", return_value=True), mock.patch(
          "subprocess.run", side_effect=fake_gmail_run
      ):
        tray_after = ingestor.ingest_gmail_drafts_and_threads(store, w_state, now=now + 60)
      self.assertEqual(
          {x["thread_id"] for x in tray_after},
          {"t_human_ok", "t_human_cc8"},
      )

      # Promoting t_human_ok hides it immediately, and completing it keeps it hidden (N2)
      req_prom = urllib.request.Request(
          f"http://127.0.0.1:{port}/api/work/create",
          data=json.dumps({
              "title": "TextClusters API alignment",
              "primaries": [],
              "channels": ["gmail_thread:t_human_ok"],
          }).encode("utf-8"),
          headers={"Content-Type": "application/json"},
          method="POST",
      )
      with urllib.request.urlopen(req_prom, timeout=5) as resp:
        after_promote = json.loads(resp.read().decode("utf-8"))
      self.assertEqual(
          [x["thread_id"] for x in after_promote["unanswered_tray"]],
          ["t_human_cc8"],
      )
      prom_wid = [
          w["work_id"] for w in after_promote["works"] if w["title"] == "TextClusters API alignment"
      ][0]
      req_done = urllib.request.Request(
          f"http://127.0.0.1:{port}/api/work/done",
          data=json.dumps({"work_id": prom_wid}).encode("utf-8"),
          headers={"Content-Type": "application/json"},
          method="POST",
      )
      with urllib.request.urlopen(req_done, timeout=5) as resp:
        after_done = json.loads(resp.read().decode("utf-8"))
      self.assertEqual(
          [x["thread_id"] for x in after_done["unanswered_tray"]],
          ["t_human_cc8"],
      )
      with mock.patch("os.path.isfile", return_value=True), mock.patch(
          "subprocess.run", side_effect=fake_gmail_run
      ):
        tray_after_done = ingestor.ingest_gmail_drafts_and_threads(store, w_state, now=now + 120)
      self.assertEqual(
          [x["thread_id"] for x in tray_after_done],
          ["t_human_cc8"],
      )

      html_out = workbench_server.render_workbench_html(after_dismiss, ui_version=1)
      self.assertIn("data-dismiss-thread", html_out)
      self.assertIn("data-promote-thread", html_out)
      self.assertIn('rel="noopener"', html_out)
      # JS esc() must escape double/single quotes to prevent attribute breakout in tray rows
      self.assertIn("&quot;", html_out)
    finally:
      srv.shutdown()
      srv.server_close()

  def test_collapse_first_toggle_search_auto_expand_and_alt_click(self) -> None:
    import re
    import workbench_server

    proj = {
        "synced_et": "Nov 07 17:15 ET",
        "counts": {"needs_you": 1, "waiting": 2, "obligations_open": 0, "unanswered": 0},
        "metrics": {"link_precision": 1.0},
        "unanswered_tray": [],
        "works": [
            {
                "work_id": "w_needs",
                "title": "Needs Attention Work",
                "lane": "needs-you",
                "is_multi_channel": False,
                "obligations": [],
                "artifacts": [],
                "checklist": [],
                "reasons": ["Review requested"],
                "updated_et": "17:00 ET",
            },
            {
                "work_id": "w_wait1",
                "title": "Wait Alpha Florin",
                "lane": "waiting",
                "is_multi_channel": False,
                "obligations": [],
                "artifacts": [],
                "checklist": [],
                "reasons": [],
                "updated_et": "16:00 ET",
            },
            {
                "work_id": "w_wait2",
                "title": "Wait Beta",
                "lane": "waiting",
                "is_multi_channel": False,
                "obligations": [],
                "artifacts": [],
                "checklist": [],
                "reasons": [],
                "updated_et": "15:00 ET",
            },
            {
                "work_id": "w_done",
                "title": "Done Gamma",
                "lane": "done",
                "is_multi_channel": False,
                "obligations": [],
                "artifacts": [],
                "checklist": [],
                "reasons": [],
                "updated_et": "14:00 ET",
            },
        ],
    }
    html_out = workbench_server.render_workbench_html(proj, ui_version=1)
    self.assertIn('id="wb-toggle-collapse"', html_out)
    self.assertNotIn("wb-expand-all", html_out)
    self.assertNotIn("wb-collapse-all", html_out)

    match = re.search(r"<script>\n(.*?)\n</script>", html_out, re.DOTALL)
    self.assertIsNotNone(match, "Inline script must exist in rendered Workbench HTML")
    script_js = match.group(1)

    node_harness = """
const assert = require('assert');
const storage = new Map();
storage.set('wb_collapsed_v1', JSON.stringify({ overrides: {}, bulk: true }));

function makeEl(id, extra = {}) {
  const attrs = Object.assign({}, extra.attrs || {});
  const listeners = {};
  return Object.assign({
    id,
    value: '',
    textContent: '',
    innerHTML: '',
    disabled: false,
    getAttribute(k) {
      return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null;
    },
    setAttribute(k, v) { attrs[k] = String(v); },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    dispatch(ev, evtObj) {
      for (const fn of (listeners[ev] || [])) fn(evtObj || {});
    },
  }, extra.props || {});
}

const els = {
  'wb-synced': makeEl('wb-synced'),
  'kpi-needs': makeEl('kpi-needs'),
  'kpi-wait': makeEl('kpi-wait'),
  'kpi-obs': makeEl('kpi-obs'),
  'kpi-tray': makeEl('kpi-tray'),
  'kpi-prec': makeEl('kpi-prec'),
  'wb-tray-container': makeEl('wb-tray-container'),
  'wb-cards-container': makeEl('wb-cards-container'),
  'wb-new-btn': makeEl('wb-new-btn'),
  'wb-cancel-new': makeEl('wb-cancel-new'),
  'wb-new-form': makeEl('wb-new-form', { props: { reset() {} } }),
  'wb-new-title': makeEl('wb-new-title'),
  'wb-new-primary': makeEl('wb-new-primary'),
  'wb-new-channel': makeEl('wb-new-channel'),
  'wb-new-oblige': makeEl('wb-new-oblige'),
  'wb-new-when': makeEl('wb-new-when'),
  'wb-sync-btn': makeEl('wb-sync-btn'),
  'wb-toggle-collapse': makeEl('wb-toggle-collapse', {
    attrs: { 'data-action': 'collapse' },
  }),
  'wb-search': makeEl('wb-search'),
};
const chips = ['all', 'needs-you', 'multi', 'obligations', 'done'].map((f, idx) =>
  makeEl('chip-' + f, {
    attrs: { 'data-filter': f, 'data-active': idx === 0 ? 'true' : 'false' },
  })
);
const bodyListeners = {};
global.localStorage = {
  getItem: (k) => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => { storage.set(k, String(v)); },
};
global.window = {
  addEventListener: () => {},
  getSelection: () => '',
  prompt: () => '',
};
global.setInterval = () => 0;
global.fetch = async () => ({ ok: false });
global.document = {
  getElementById: (id) => els[id] || makeEl(id),
  querySelectorAll: (sel) => (sel === '.wb-chip' ? chips : []),
  createElement: () => {
    let txt = '';
    return {
      set textContent(v) { txt = String(v || ''); },
      get innerHTML() {
        return txt.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      },
    };
  },
  body: {
    addEventListener(ev, fn) { (bodyListeners[ev] = bodyListeners[ev] || []).push(fn); },
    dispatch(ev, evtObj) { for (const fn of (bodyListeners[ev] || [])) fn(evtObj); },
  },
};

""" + script_js + """

function parseCards() {
  const re = new RegExp(
    '<article class="wb-card" data-work-id="([^"]+)" ' +
    'data-lane="([^"]+)" data-collapsed="(true|false)">',
    'g'
  );
  const out = {};
  let m;
  while ((m = re.exec(els['wb-cards-container'].innerHTML)) !== null) {
    out[m[1]] = { lane: m[2], collapsed: m[3] === 'true' };
  }
  return out;
}

function clickCardHeader(wid, altKey) {
  const cards = parseCards();
  assert(cards[wid], 'Expected visible card ' + wid);
  const hdrObj = {
    getAttribute: (k) => (k === 'data-toggle-card' ? wid : null),
    closest: (sel) => (sel === '.wb-card' ? {
      getAttribute: (k) => (k === 'data-collapsed' ? String(cards[wid].collapsed) : null),
    } : null),
  };
  document.body.dispatch('click', {
    altKey: Boolean(altKey),
    preventDefault: () => {},
    target: {
      closest: (sel) => {
        if (sel === 'button, a, input') return null;
        if (sel === '[data-toggle-card]') return hdrObj;
        return null;
      },
    },
  });
}

const toggleBtn = els['wb-toggle-collapse'];
const searchInput = els['wb-search'];

let cards = parseCards();
assert.deepStrictEqual(Object.keys(cards), ['w_needs', 'w_wait1', 'w_wait2']);
assert.strictEqual(cards.w_needs.collapsed, false);
assert.strictEqual(cards.w_wait1.collapsed, true);
assert.strictEqual(cards.w_wait2.collapsed, true);
assert.strictEqual(toggleBtn.textContent, '⇈');
assert.strictEqual(toggleBtn.getAttribute('data-action'), 'collapse');
assert.strictEqual(toggleBtn.getAttribute('data-expanded-count'), '1');
assert.strictEqual(toggleBtn.getAttribute('data-collapsed-count'), '2');
assert.strictEqual(toggleBtn.disabled, false);

toggleBtn.dispatch('click');
cards = parseCards();
assert.strictEqual(cards.w_needs.collapsed, true);
assert.strictEqual(cards.w_wait1.collapsed, true);
assert.strictEqual(cards.w_wait2.collapsed, true);
assert.strictEqual(toggleBtn.textContent, '⇊');
assert.strictEqual(toggleBtn.getAttribute('data-action'), 'expand');
assert.strictEqual(toggleBtn.getAttribute('data-collapsed-count'), '3');
const saved1 = JSON.parse(storage.get('wb_collapsed_v2'));
assert.deepStrictEqual(saved1, { overrides: { w_needs: true } });
assert.strictEqual('bulk' in saved1, false);

toggleBtn.dispatch('click');
cards = parseCards();
assert.strictEqual(cards.w_needs.collapsed, false);
assert.strictEqual(cards.w_wait1.collapsed, false);
assert.strictEqual(cards.w_wait2.collapsed, false);
assert.strictEqual(toggleBtn.textContent, '⇈');
assert.strictEqual(toggleBtn.getAttribute('data-expanded-count'), '3');
const saved2 = JSON.parse(storage.get('wb_collapsed_v2'));
assert.deepStrictEqual(saved2, { overrides: { w_wait1: false, w_wait2: false } });

chips[4].dispatch('click');
cards = parseCards();
assert.deepStrictEqual(Object.keys(cards), ['w_done']);
assert.strictEqual(cards.w_done.collapsed, true);
assert.strictEqual(toggleBtn.textContent, '⇊');
assert.strictEqual(toggleBtn.getAttribute('data-collapsed-count'), '1');

chips[0].dispatch('click');
toggleBtn.dispatch('click');
const savedBeforeSearch = JSON.parse(storage.get('wb_collapsed_v2'));
assert.deepStrictEqual(savedBeforeSearch, { overrides: { w_needs: true } });
clickCardHeader('w_needs', false);
assert.deepStrictEqual(JSON.parse(storage.get('wb_collapsed_v2')), { overrides: {} });
storage.delete('wb_collapsed_v2');

searchInput.value = 'Florin';
searchInput.dispatch('input', { target: searchInput });
cards = parseCards();
assert.deepStrictEqual(Object.keys(cards), ['w_wait1']);
assert.strictEqual(cards.w_wait1.collapsed, false);
assert.strictEqual(toggleBtn.textContent, '⇈');
assert.strictEqual(toggleBtn.getAttribute('data-expanded-count'), '1');
assert.strictEqual(storage.has('wb_collapsed_v2'), false);

toggleBtn.dispatch('click');
cards = parseCards();
assert.strictEqual(cards.w_wait1.collapsed, true);
assert.strictEqual(toggleBtn.textContent, '⇊');
assert.strictEqual(storage.has('wb_collapsed_v2'), false);

searchInput.value = 'no-such-item-xyz';
searchInput.dispatch('input', { target: searchInput });
cards = parseCards();
assert.deepStrictEqual(Object.keys(cards), []);
assert.strictEqual(toggleBtn.disabled, true);
assert.strictEqual(toggleBtn.getAttribute('data-expanded-count'), '0');
assert.strictEqual(toggleBtn.getAttribute('data-collapsed-count'), '0');

searchInput.value = '';
searchInput.dispatch('input', { target: searchInput });
cards = parseCards();
assert.deepStrictEqual(Object.keys(cards), ['w_needs', 'w_wait1', 'w_wait2']);
assert.strictEqual(cards.w_needs.collapsed, false);
assert.strictEqual(cards.w_wait1.collapsed, true);
assert.strictEqual(cards.w_wait2.collapsed, true);
assert.strictEqual(toggleBtn.disabled, false);

clickCardHeader('w_wait1', true);
cards = parseCards();
assert.strictEqual(cards.w_needs.collapsed, false);
assert.strictEqual(cards.w_wait1.collapsed, false);
assert.strictEqual(cards.w_wait2.collapsed, false);

clickCardHeader('w_needs', true);
cards = parseCards();
assert.strictEqual(cards.w_needs.collapsed, true);
assert.strictEqual(cards.w_wait1.collapsed, true);
assert.strictEqual(cards.w_wait2.collapsed, true);
"""
    res = subprocess.run(
        ["node", "-e", node_harness],
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(
        res.returncode,
        0,
        f"Node behavioral check failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )

  def test_companion_policy_foreign_conflict_and_config_product_pr_protection(self) -> None:
    import ingestor
    import work_store

    now = 1794170000.0
    store = work_store.WorkStore(self.db_path)

    # GitHub PR notification HTML puts data-url="/issues/<N>" on pull_request hovercards
    gh_pr_html = (
        'dbebawy left a comment <a href="https://github.com/flutter/flutter/pull/195366">'
        "(flutter/flutter#195366)</a> referencing "
        '<a data-url="https://github.com/flutter/flutter/issues/195122" '
        'data-hovercard-type="pull_request" '
        'href="https://github.com/flutter/flutter/pull/195122">#195122</a> '
        "and Fixes https://github.com/flutter/flutter/issues/193506."
    )
    ext = ingestor.extract_artifacts_from_text(gh_pr_html)
    ext_keys = [k for k, _, _ in ext]
    self.assertIn("github_pr:flutter/flutter:195366", ext_keys)
    self.assertIn("github_pr:flutter/flutter:195122", ext_keys)
    self.assertIn("github_issue:flutter/flutter:193506", ext_keys)
    self.assertNotIn("github_issue:flutter/flutter:195366", ext_keys)
    self.assertNotIn("github_issue:flutter/flutter:195122", ext_keys)

    # Chromium Gerrit companion_to without explicit host inherits the child's Gerrit repo
    self.assertEqual(
        work_store.canonicalize_companion_parent(
            "gerrit:chromium:8635030", {"companion_to": "gerrit:8595771"}
        ),
        "gerrit:chromium:8595771",
    )

    # Companion creation chat on PR #195366 publishing Draft PR #195377
    cid_comp = "2a40bfdd-b05c-4cbd-ad67-178961b3b72b"
    tpath_comp = os.path.join(
        self.tmp_dir.name, cid_comp, ".system_generated", "logs", "transcript.jsonl"
    )
    os.makedirs(os.path.dirname(tpath_comp), exist_ok=True)
    with open(tpath_comp, "w", encoding="utf-8") as f:
      f.write(
          json.dumps({
              "step_index": 0,
              "type": "USER_INPUT",
              "created_at": "2026-10-26T22:00:00Z",
              "content": (
                  "Оформи компаньон ПР на базе "
                  "https://github.com/flutter/flutter/pull/195366 как драфт"
              ),
          })
          + "\n"
      )
      f.write(
          json.dumps({
              "step_index": 1,
              "type": "PLANNER_RESPONSE",
              "created_at": "2026-10-26T22:20:00Z",
              "content": (
                  "Я создала companion Draft PR "
                  "https://github.com/flutter/flutter/pull/195377 "
                  "([web_paragraph] Companion fixes and tests for soft hyphens)"
              ),
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "gh pr create --draft --title "
                          "'[web_paragraph] Companion fixes and tests for soft hyphens (#195366)'"
                      )
                  },
              }],
          })
          + "\n"
      )

    cursors: dict[str, dict[str, float | int]] = {}
    ingestor.ingest_transcript_file(
        store,
        conv_id=cid_comp,
        root_cid=cid_comp,
        transcript_path=tpath_comp,
        cursors=cursors,
        chat_title="Companion Draft PR for PR #195366 (`[web_paragraph] Render soft hyphens`)",
        now=now,
    )

    # External PR #195366 stamped with My Work project_id and foreign merge_conflict=True;
    # own non-WIP CL 1398679 with merge_conflict=True; active open companion PR #195377
    my_work_pid = "61f270de-563d-45a4-b9eb-1c0bccf80cc9"
    cfg_cid = "3531f6cd-c086-4609-8e85-4afecac6fa61"
    cl_state: dict[str, Any] = {
        "items": {
            f"github:195366@{cfg_cid}": {
                "item_key": "github:195366",
                "kind": "github",
                "target_id": "195366",
                "status": "OPEN",
                "is_self": False,
                "role": "reviewer",
                "is_wip": False,
                "is_draft": False,
                "merge_conflict": True,
                "project_id": my_work_pid,
                "conversation_id": cfg_cid,
                "subject": "[web_paragraph] Render soft hyphens",
                "url": "https://github.com/flutter/flutter/pull/195366",
                "entity_state": {"status": "waiting", "lane": "waiting"},
            },
            "github:195377@comp": {
                "item_key": "github:195377",
                "kind": "github",
                "target_id": "195377",
                "status": "OPEN",
                "is_self": True,
                "role": "author",
                "is_wip": True,
                "is_draft": True,
                "merge_conflict": True,
                "cq_failed": True,
                "is_companion": True,
                "companion_to": "github:195366",
                "subject": "[web_paragraph] Companion fixes and tests for soft hyphens (#195366)",
                "url": "https://github.com/flutter/flutter/pull/195377",
                "entity_state": {
                    "status": "conflict",
                    "lane": "you",
                    "action_required": True,
                },
            },
            "gerrit:1398679@own": {
                "item_key": "gerrit:1398679",
                "kind": "gerrit",
                "target_id": "1398679",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "is_self": True,
                "role": "author",
                "is_wip": False,
                "merge_conflict": True,
                "subject": "[skparagraph] Fix baselineShift in extendedVisit",
                "url": "https://skia-review.googlesource.com/c/skia/+/1398679",
                "linked_bugs": [{
                    "bug_key": "b/400000001",
                    "short_id": "b/400000001",
                    "title": "baselineShift offset mismatch",
                    "status": "OPEN",
                }],
                "entity_state": {"status": "waiting", "lane": "desk"},
            },
        },
        "bugs": {},
    }
    ingestor.ingest_cl_watcher_state(store, cl_state, now=now + 10)
    store.purge_config_artifacts_and_works(now=now + 15)

    self.assertFalse(
        work_store.is_config_artifact(
            "github_pr:flutter/flutter:195366",
            title="195366: [web_paragraph] Render soft hyphens",
            state=cl_state["items"][f"github:195366@{cfg_cid}"],
            project_id=my_work_pid,
        )
    )

    proj = store.build_dashboard_projection(cl_watcher_state=cl_state, now=now + 20)
    cards_195366 = [
        w
        for w in proj["works"]
        if any(a["artifact_key"] == "github_pr:flutter/flutter:195366" for a in w["artifacts"])
    ]
    self.assertEqual(len(cards_195366), 1)
    card_soft = cards_195366[0]
    self.assertFalse(card_soft["is_virtual"])
    self.assertNotIn("Companion Draft PR", card_soft["title"])
    self.assertIn("Render soft hyphens", card_soft["title"])

    soft_arts = {a["artifact_key"]: a for a in card_soft["artifacts"]}
    self.assertIn("github_pr:flutter/flutter:195377", soft_arts)
    self.assertNotIn(f"chat_chain:{cfg_cid}", soft_arts)
    self.assertEqual(card_soft["lane"], "waiting")

    # Own non-WIP CL with merge_conflict still triggers needs-you
    own_card = [
        w
        for w in proj["works"]
        if any(a["artifact_key"] == "gerrit:skia:1398679" for a in w["artifacts"])
    ][0]
    self.assertEqual(own_card["lane"], "needs-you")
    self.assertTrue(any("Merge conflict" in r for r in own_card["reasons"]))

    # External reviewer PR needing review with merge_conflict keeps its review reason
    cl_state["items"][f"github:195366@{cfg_cid}"]["entity_state"] = {
        "category": "needs_me",
        "reason": "Review requested by dbebawy",
        "lane": "you",
    }
    ingestor.ingest_cl_watcher_state(store, cl_state, now=now + 25)
    proj_rev = store.build_dashboard_projection(cl_watcher_state=cl_state, now=now + 26)
    card_rev = [w for w in proj_rev["works"] if w["work_id"] == card_soft["work_id"]][0]
    self.assertEqual(card_rev["lane"], "needs-you")
    self.assertIn("Review requested by dbebawy", card_rev["reasons"])
    self.assertFalse(any("Merge conflict" in r for r in card_rev["reasons"]))

    # Live external human comment on the companion wakes the parent's Work card
    cl_state["items"][f"github:195366@{cfg_cid}"]["entity_state"] = {
        "status": "waiting",
        "lane": "waiting",
    }
    cl_state["items"]["github:195377@comp"]["open_external_threads"] = 1
    ingestor.ingest_cl_watcher_state(store, cl_state, now=now + 30)
    proj_comment = store.build_dashboard_projection(cl_watcher_state=cl_state, now=now + 31)
    card_comment = [w for w in proj_comment["works"] if w["work_id"] == card_soft["work_id"]][0]
    self.assertEqual(card_comment["lane"], "needs-you")
    self.assertTrue(any("195377" in r for r in card_comment["reasons"]))

    # Parent merges while companion stays OPEN -> orphaned companion cleanup alert
    cl_state["items"]["github:195377@comp"]["open_external_threads"] = 0
    cl_state["items"][f"github:195366@{cfg_cid}"]["status"] = "MERGED"
    ingestor.ingest_cl_watcher_state(store, cl_state, now=now + 40)
    proj_orphan = store.build_dashboard_projection(cl_watcher_state=cl_state, now=now + 41)
    card_orphan = [w for w in proj_orphan["works"] if w["work_id"] == card_soft["work_id"]][0]
    self.assertEqual(card_orphan["lane"], "needs-you")
    self.assertTrue(any("orphaned companion" in r.lower() for r in card_orphan["reasons"]))

    # Closing the companion transitions the Work cleanly to done (both MERGED and CLOSED parent)
    cl_state["items"]["github:195377@comp"]["status"] = "CLOSED"
    ingestor.ingest_cl_watcher_state(store, cl_state, now=now + 50)
    proj_done = store.build_dashboard_projection(cl_watcher_state=cl_state, now=now + 51)
    card_done = [w for w in proj_done["works"] if w["work_id"] == card_soft["work_id"]][0]
    self.assertEqual(card_done["lane"], "done")

    cl_state["items"][f"github:195366@{cfg_cid}"]["status"] = "CLOSED"
    ingestor.ingest_cl_watcher_state(store, cl_state, now=now + 55)
    proj_closed = store.build_dashboard_projection(cl_watcher_state=cl_state, now=now + 56)
    card_closed = [w for w in proj_closed["works"] if w["work_id"] == card_soft["work_id"]][0]
    self.assertEqual(card_closed["lane"], "done")

    # Unbound open companion in cl_watcher_state whose virtual parent is ABANDONED surfaces cleanup
    cl_state["items"]["gerrit:1499998@virt"] = {
        "item_key": "gerrit:1499998",
        "kind": "gerrit",
        "target_id": "1499998",
        "host": "skia-review.googlesource.com",
        "status": "ABANDONED",
        "is_self": False,
        "role": "reviewer",
        "subject": "[skparagraph] Abandoned external change",
        "url": "https://skia-review.googlesource.com/c/skia/+/1499998",
        "entity_state": {"status": "abandoned", "lane": "done"},
    }
    cl_state["items"]["gerrit:1499999@virt"] = {
        "item_key": "gerrit:1499999",
        "kind": "gerrit",
        "target_id": "1499999",
        "host": "skia-review.googlesource.com",
        "status": "NEW",
        "is_self": True,
        "role": "author",
        "is_wip": True,
        "is_companion": True,
        "companion_to": "gerrit:1499998",
        "subject": "[companion WIP] Contract tests for 1499998",
        "url": "https://skia-review.googlesource.com/c/skia/+/1499999",
        "entity_state": {"status": "draft", "lane": "desk"},
    }
    proj_virt_orphan = store.build_dashboard_projection(cl_watcher_state=cl_state, now=now + 60)
    virt_cards = [
        w for w in proj_virt_orphan["works"] if w["work_id"] == "virt-gerrit:skia:1499998"
    ]
    self.assertEqual(len(virt_cards), 1)
    self.assertEqual(virt_cards[0]["lane"], "needs-you")
    self.assertTrue(any("orphaned companion" in r.lower() for r in virt_cards[0]["reasons"]))
    self.assertFalse(
        any(w["work_id"] == "virt-gerrit:skia:1499999" for w in proj_virt_orphan["works"])
    )

    # Structural repair: companion-only primary promotes its parent CL to primary,
    # and companion chat titles on Works upgrade to the parent CL's real subject
    w_comp_only = store.create_work(
        "1408158: [skparagraph] Use std::string for the builder text", now=now + 65
    )
    store.upsert_artifact(
        "gerrit:skia:1408158",
        kind="gerrit",
        title="1408158: [skparagraph] Use std::string for the builder text",
        url="https://skia-review.googlesource.com/c/skia/+/1408158",
        state={"status": "NEW"},
        now=now + 65,
    )
    store.upsert_artifact(
        "gerrit:skia:1406080",
        kind="gerrit",
        title="1406080: [companion WIP]",
        url="https://skia-review.googlesource.com/c/skia/+/1406080",
        state={"is_companion": True, "companion_to": "gerrit:skia:1408158", "is_wip": True},
        now=now + 65,
    )
    store.link_artifact(
        w_comp_only,
        "gerrit:skia:1408158",
        role="context_only",
        source="auto",
        link_reason="chat_mention",
        now=now + 65,
    )
    store.link_artifact(
        w_comp_only,
        "gerrit:skia:1406080",
        role="primary",
        source="auto",
        link_reason="companion_wip",
        now=now + 65,
    )
    w_glyph = store.create_work(
        "GlyphRun companion CLs on crrev/c/8690001 PS19", now=now + 65
    )
    store.upsert_artifact(
        "gerrit:chromium:8690001",
        kind="gerrit",
        title="8690001: [canvas] Draw GlyphRuns with the direction captured by shapeText()",
        url="https://chromium-review.googlesource.com/c/chromium/src/+/8690001",
        state={
            "status": "MERGED",
            "subject": "[canvas] Draw GlyphRuns with the direction captured by shapeText()",
        },
        now=now + 65,
    )
    store.link_artifact(
        w_glyph,
        "gerrit:chromium:8690001",
        role="primary",
        source="auto",
        link_reason="chat_topic",
        now=now + 65,
    )
    # Duplicate companion-titled card sharing its parent PR with a real correspondence Work merges
    w_real_parent = store.create_work(
        "[flutter/flutter] Render soft hyphens (PR #198001) — David Bebawy", now=now + 65
    )
    w_dup_comp = store.create_work(
        "Companion Draft PR for PR #198001 (`Render soft hyphens`)", now=now + 65
    )
    store.upsert_artifact(
        "github_pr:flutter/flutter:198001",
        kind="github_pr",
        title="198001: Render soft hyphens",
        state={"status": "OPEN"},
        now=now + 65,
    )
    store.upsert_artifact(
        "github_pr:flutter/flutter:198002",
        kind="github_pr",
        title="198002: Companion fixes (#198001)",
        state={"is_companion": True, "companion_to": "github_pr:flutter/flutter:198001"},
        now=now + 65,
    )
    store.link_artifact(
        w_real_parent, "github_pr:flutter/flutter:198001", role="primary", now=now + 65
    )
    store.link_artifact(
        w_dup_comp, "github_pr:flutter/flutter:198001", role="primary", now=now + 65
    )
    store.link_artifact(
        w_dup_comp, "github_pr:flutter/flutter:198002", role="context_only", now=now + 65
    )
    store.purge_config_artifacts_and_works(now=now + 70)
    self.assertEqual(
        store.get_work_artifact_roles(w_comp_only).get("gerrit:skia:1408158"), "primary"
    )
    self.assertEqual(
        store.get_work_artifact_roles(w_real_parent).get("github_pr:flutter/flutter:198002"),
        "primary",
    )
    proj_repaired = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 71)
    self.assertFalse(any(w["work_id"] == w_dup_comp for w in proj_repaired["works"]))
    glyph_card = [w for w in proj_repaired["works"] if w["work_id"] == w_glyph][0]
    self.assertNotIn("companion CLs", glyph_card["title"])
    self.assertIn("Draw GlyphRuns with the direction captured by shapeText()", glyph_card["title"])
    self.assertEqual(glyph_card["lane"], "done")

    # B1: Real entity_state from entity_state.py uses lane="you", attention_reason on item,
    # and spine_short_state.short="Conflict" even on foreign PRs
    real_est_state: dict[str, Any] = {
        "items": {
            "github:196100@rev": {
                "item_key": "github:196100",
                "kind": "github",
                "target_id": "196100",
                "status": "OPEN",
                "is_self": False,
                "role": "reviewer",
                "merge_conflict": True,
                "in_my_attention": True,
                "attention_reason": "Review requested by contributor",
                "subject": "[web_paragraph] External review with conflict",
                "url": "https://github.com/flutter/flutter/pull/196100",
                "entity_state": {
                    "status": "attention",
                    "lane": "you",
                    "is_reviewer_change": True,
                    "needs_my_reply": True,
                    "spine_short_state": {
                        "short": "Conflict",
                        "icon": "⚠️",
                        "tone": "spine-warn",
                    },
                },
            }
        }
    }
    proj_b1_v = store.build_dashboard_projection(cl_watcher_state=real_est_state, now=now + 75)
    vc_b1 = [
        w for w in proj_b1_v["works"] if w["work_id"] == "virt-github_pr:flutter/flutter:196100"
    ]
    self.assertEqual(len(vc_b1), 1)
    self.assertEqual(vc_b1[0]["lane"], "needs-you")
    self.assertIn("Review requested by contributor", vc_b1[0]["reasons"])
    self.assertFalse(any("conflict" in r.lower() for r in vc_b1[0]["reasons"]))
    w_b1 = store.create_work("External review with conflict", now=now + 76)
    ingestor.ingest_cl_watcher_state(store, real_est_state, now=now + 76)
    store.link_artifact(w_b1, "github_pr:flutter/flutter:196100", role="primary", now=now + 76)
    proj_b1_p = store.build_dashboard_projection(cl_watcher_state=real_est_state, now=now + 77)
    pc_b1 = [w for w in proj_b1_p["works"] if w["work_id"] == w_b1][0]
    self.assertEqual(pc_b1["lane"], "needs-you")
    self.assertIn("Review requested by contributor", pc_b1["reasons"])
    self.assertFalse(any("conflict" in r.lower() for r in pc_b1["reasons"]))

    # B2 & B3: Markdown clickable companion links without trailing (lbl) are detected as
    # companions and never clobber an already-stored descriptive companion subject
    store.upsert_artifact(
        "gerrit:skia:1490010",
        kind="gerrit",
        title="1490010: [skparagraph] Companion contract test for 1490001",
        state={"is_companion": True, "companion_to": "gerrit:skia:1490001"},
        now=now + 80,
    )
    cid_md = "77777777-8888-4999-aaaa-bbbbbbbbbbbb"
    tpath_md = os.path.join(
        self.tmp_dir.name, cid_md, ".system_generated", "logs", "transcript.jsonl"
    )
    os.makedirs(os.path.dirname(tpath_md), exist_ok=True)
    with open(tpath_md, "w", encoding="utf-8") as f:
      f.write(
          json.dumps({
              "step_index": 0,
              "type": "USER_INPUT",
              "created_at": "2026-10-28T12:00:00Z",
              "content": "Review https://skia-review.googlesource.com/c/skia/+/1490001",
          })
          + "\n"
      )
      f.write(
          json.dumps({
              "step_index": 1,
              "type": "PLANNER_RESPONSE",
              "created_at": "2026-10-28T12:15:00Z",
              "content": (
                  "Uploaded companion WIP CL "
                  "[Skia CL 1490010 PS1](https://skia-review.googlesource.com/c/skia/+/1490010/1) "
                  "and Chromium companion WIP CL "
                  "[Chromium CL 8700010 PS1]"
                  "(https://chromium-review.googlesource.com/c/chromium/src/+/8700010/1)"
              ),
              "tool_calls": [{"name": "run_command", "args": {"CommandLine": "git cl upload"}}],
          })
          + "\n"
      )
    ingestor.ingest_transcript_file(store, cid_md, cid_md, tpath_md, {}, now=now + 81)
    art_sk_md = store.get_artifact("gerrit:skia:1490010") or {}
    art_cr_md = store.get_artifact("gerrit:chromium:8700010") or {}
    self.assertIn("Companion contract test", str(art_sk_md.get("title") or ""))
    self.assertTrue(json.loads(art_cr_md.get("state_json") or "{}").get("is_companion"))

    # B4: Unbound open companion whose closed parent aged out of cl_watcher_state (or was only
    # context_only on another Work) still surfaces orphaned cleanup + human comment on virt parent
    w_unrelated = store.create_work("Unrelated task", now=now + 85)
    store.upsert_artifact(
        "buganizer:400000999", kind="buganizer", title="b/400000999", now=now + 85
    )
    store.link_artifact(w_unrelated, "buganizer:400000999", role="primary", now=now + 85)
    store.link_artifact(
        w_unrelated, "github_pr:flutter/flutter:195000", role="context_only", now=now + 85
    )
    aged_parent_state: dict[str, Any] = {
        "items": {
            "github:195099@comp": {
                "item_key": "github:195099",
                "kind": "github",
                "target_id": "195099",
                "status": "OPEN",
                "is_self": True,
                "is_draft": True,
                "is_companion": True,
                "companion_to": "github:195000",
                "ready_to_abandon": True,
                "open_external_threads": 1,
                "subject": "[web_paragraph] Companion tests for #195000",
                "url": "https://github.com/flutter/flutter/pull/195099",
            }
        }
    }
    proj_b4 = store.build_dashboard_projection(cl_watcher_state=aged_parent_state, now=now + 86)
    p_b4 = [w for w in proj_b4["works"] if w["work_id"] == "virt-github_pr:flutter/flutter:195000"]
    self.assertEqual(len(p_b4), 1)
    self.assertEqual(p_b4[0]["lane"], "needs-you")
    self.assertTrue(any("comment on companion" in r.lower() for r in p_b4[0]["reasons"]))
    self.assertTrue(any("orphaned companion" in r.lower() for r in p_b4[0]["reasons"]))

    # B5: Inline PR bullet never stamps bug_title, single-quoted JSON hovercard masks /issues/<N>,
    # and Draft Work with 'flutter#N' stub swaps to p_key without bypassing negative_links
    self.assertEqual(
        ingestor.extract_bug_titles_from_html_or_text("<li>PR #197777 — Render soft hyphens</li>"),
        {},
    )
    hq_html = (
        '<a data-hydro-click=\'{"url":"https://github.com/flutter/flutter/issues/197778"}\' '
        'href="https://github.com/flutter/flutter/pull/197778">#197778</a>'
    )
    self.assertEqual(
        [k for k, _, _ in ingestor.extract_artifacts_from_text(hq_html)],
        ["github_pr:flutter/flutter:197778"],
    )
    w_draft_pr = store.create_work("Re: PR #197779 Soft hyphens", now=now + 90)
    store.upsert_artifact(
        "gmail_draft:d99", kind="gmail_draft", state={"status": "DRAFT"}, now=now + 90
    )
    store.link_artifact(w_draft_pr, "gmail_draft:d99", role="channel", now=now + 90)
    store.upsert_artifact(
        "github_issue:flutter/flutter:197779", title="flutter#197779", now=now + 90
    )
    store.link_artifact(
        w_draft_pr, "github_issue:flutter/flutter:197779", role="primary", now=now + 90
    )
    store.upsert_artifact(
        "github_pr:flutter/flutter:197779",
        title="197779: Render soft hyphens",
        state={"status": "OPEN"},
        now=now + 90,
    )
    store.purge_config_artifacts_and_works(now=now + 91)
    self.assertIn(w_draft_pr, {w["work_id"] for w in store.list_works()})
    self.assertEqual(
        store.get_work_artifact_roles(w_draft_pr).get("github_pr:flutter/flutter:197779"), "primary"
    )
    # Aged-out PR placeholder also beats an empty 'flutter#N' issue stub without bug_title
    store.upsert_artifact(
        "github_pr:flutter/flutter:197780", title="github_pr:flutter/flutter:197780", now=now + 92
    )
    store.upsert_artifact(
        "github_issue:flutter/flutter:197780", title="flutter#197780", now=now + 92
    )
    store.purge_config_artifacts_and_works(now=now + 93)
    self.assertIsNone(store.get_artifact("github_issue:flutter/flutter:197780"))
    self.assertIsNotNone(store.get_artifact("github_pr:flutter/flutter:197780"))

    # B6: Non-anchored 'Companion for/to' & singular 'companion fix' titles + title fallback
    self.assertTrue(work_store.is_companion_title("195377: [web_paragraph] Companion for #195366"))
    self.assertTrue(work_store.is_companion_title("[skparagraph] Companion fix to CL 1408158"))
    self.assertEqual(
        work_store.canonicalize_companion_parent(
            "github_pr:flutter/flutter:195377",
            state={"subject": "Soft hyphens tests"},
            title="195377: Companion for #195366",
        ),
        "github_pr:flutter/flutter:195366",
    )

  def test_r12_correspondence_end_to_end_ownership_and_interim_reply_safety(self) -> None:
    import base64
    from unittest import mock
    import ingestor
    import work_store
    import workbench_server

    now = time.time()
    store = work_store.WorkStore(self.db_path)

    def _b64(text: str) -> str:
      return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")

    inbound_state = {"reply_sent": False, "followup_inbound": False}

    def fake_gmail_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
      res = subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
      if "drafts" in cmd and "get-draft" not in cmd:
        res.stdout = "[]"
      elif "search" in cmd:
        q = cmd[cmd.index("search") + 1]
        if "from:me" in q:
          res.stdout = json.dumps([{"threadId": "1a19mouad0000001"}])
        elif "in:inbox" in q:
          res.stdout = json.dumps([{"threadId": "1a19coldinbound1"}])
      elif "get-thread" in cmd:
        tid = cmd[cmd.index("get-thread") + 1]
        if tid == "1a19coldinbound1":
          msgs = [{
              "id": "m_cold_1",
              "internalDate": str(int((now - 1800) * 1000)),
              "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
              "snippet": "Could you check TextPainter line metrics on empty lines?",
              "payload": {
                  "mimeType": "text/plain",
                  "headers": [
                      {"name": "Subject", "value": "TextPainter empty line metrics"},
                      {"name": "From", "value": "Marcel Garus <mgarus@google.com>"},
                      {"name": "To", "value": "Julia Lavrova <jlavrova@google.com>"},
                  ],
                  "body": {
                      "data": _b64("Hi Julia, could you check TextPainter empty line metrics?")
                  },
              },
          }]
          if inbound_state["reply_sent"]:
            msgs.append({
                "id": "m_cold_2",
                "internalDate": str(int((now + 120) * 1000)),
                "labelIds": ["SENT"],
                "snippet": "Looking into this now, I will send numbers tomorrow.",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "Subject", "value": "Re: TextPainter empty line metrics"},
                        {"name": "From", "value": "Julia Lavrova <jlavrova@google.com>"},
                        {"name": "To", "value": "Marcel Garus <mgarus@google.com>"},
                    ],
                    "body": {
                        "data": _b64("Looking into this now, I will send numbers tomorrow.")
                    },
                },
            })
          if inbound_state["followup_inbound"]:
            msgs.append({
                "id": "m_cold_3",
                "internalDate": str(int((now + 3600) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
                "snippet": "Here is a minimal repro case as well.",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "Subject", "value": "Re: TextPainter empty line metrics"},
                        {"name": "From", "value": "Marcel Garus <mgarus@google.com>"},
                        {"name": "To", "value": "Julia Lavrova <jlavrova@google.com>"},
                    ],
                    "body": {"data": _b64("Here is a minimal repro case as well.")},
                },
            })
          res.stdout = json.dumps({"messages": msgs})
        elif tid == "1a19mouad0000001":
          res.stdout = json.dumps({
              "messages": [{
                  "id": "m_mouad_1",
                  "internalDate": str(int((now - 600) * 1000)),
                  "labelIds": ["SENT"],
                  "snippet": "Hi Mouad, attached are the Gallery WebParagraph benchmark numbers.",
                  "payload": {
                      "mimeType": "text/plain",
                      "headers": [
                          {"name": "Subject", "value": "Gallery WebParagraph benchmark report"},
                          {"name": "From", "value": "Julia Lavrova <jlavrova@google.com>"},
                          {"name": "To", "value": "Mouad Debbar <mdebbar@google.com>"},
                      ],
                      "body": {
                          "data": _b64(
                              "Hi Mouad, attached are the Gallery WebParagraph benchmark numbers."
                          )
                      },
                  },
              }]
          })
      return res

    w_state: dict[str, object] = {}
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_gmail_run
    ):
      tray = ingestor.ingest_gmail_drafts_and_threads(store, w_state, now=now)

    self.assertEqual([t["thread_id"] for t in tray], ["1a19coldinbound1"])

    proj0 = store.build_dashboard_projection(
        cl_watcher_state={"items": {}}, unanswered_threads=tray, now=now
    )
    mouad_card = [w for w in proj0["works"] if "Gallery WebParagraph" in w["title"]][0]
    self.assertEqual(mouad_card["lane"], "waiting")
    mouad_obs = [o for o in mouad_card["obligations"] if o["status"] != "done"]
    self.assertEqual(len(mouad_obs), 1)
    self.assertEqual(mouad_obs[0]["status"], "open")
    self.assertGreaterEqual(proj0["counts"]["obligations_open"], 1)

    # After 7 days of silence without a reply, outbound obligation wakes into Needs You
    proj_7d = store.build_dashboard_projection(
        cl_watcher_state={"items": {}}, now=now + 7 * 86400 + 60
    )
    mouad_7d = [w for w in proj_7d["works"] if w["work_id"] == mouad_card["work_id"]][0]
    self.assertEqual(mouad_7d["lane"], "needs-you")
    self.assertEqual(
        [o for o in mouad_7d["obligations"] if o["status"] != "done"][0]["status"], "ready"
    )

    # Promoting cold inbound thread via + Work lands directly in Needs You with ready obligation
    w_state["unanswered_tray"] = tray
    with open(self.state_path, "w", encoding="utf-8") as f:
      json.dump(w_state, f)
    with open(self.cl_state_path, "w", encoding="utf-8") as f:
      json.dump({"items": {}, "bugs": {}}, f)

    srv = workbench_server.create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        cl_state_path=self.cl_state_path,
        watcher_state_path=self.state_path,
        info_path=self.info_path,
    )
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
      req_prom = urllib.request.Request(
          f"http://127.0.0.1:{port}/api/work/create",
          data=json.dumps({
              "title": "TextPainter empty line metrics",
              "primaries": [],
              "channels": ["gmail_thread:1a19coldinbound1"],
          }).encode("utf-8"),
          headers={"Content-Type": "application/json"},
          method="POST",
      )
      with urllib.request.urlopen(req_prom, timeout=5) as resp:
        after_prom = json.loads(resp.read().decode("utf-8"))
    finally:
      srv.shutdown()
      srv.server_close()

    marcel_card = [w for w in after_prom["works"] if "TextPainter empty line" in w["title"]][0]
    self.assertEqual(marcel_card["lane"], "needs-you")
    marcel_obs = [o for o in marcel_card["obligations"] if o["status"] != "done"]
    self.assertEqual(len(marcel_obs), 1)
    self.assertEqual(marcel_obs[0]["status"], "ready")

    # Interim reply (last_from_me=True) flips ready -> open (Waiting) without deleting obligation
    inbound_state["reply_sent"] = True
    w_state["gmail_threads"] = {}
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_gmail_run
    ):
      ingestor.ingest_gmail_drafts_and_threads(store, w_state, now=now + 180)
    proj_interim = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 180)
    marcel_interim = [w for w in proj_interim["works"] if w["work_id"] == marcel_card["work_id"]][0]
    self.assertEqual(marcel_interim["lane"], "waiting")
    interim_obs = [o for o in marcel_interim["obligations"] if o["status"] != "done"]
    self.assertEqual(len(interim_obs), 1)
    self.assertEqual(interim_obs[0]["status"], "open")

    # Subsequent counterparty reply wakes the same obligation right back into Needs You
    inbound_state["followup_inbound"] = True
    w_state["gmail_threads"] = {}
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_gmail_run
    ):
      ingestor.ingest_gmail_drafts_and_threads(store, w_state, now=now + 3700)
    proj_re = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 3700)
    marcel_re = [w for w in proj_re["works"] if w["work_id"] == marcel_card["work_id"]][0]
    self.assertEqual(marcel_re["lane"], "needs-you")
    self.assertEqual(
        [o for o in marcel_re["obligations"] if o["status"] != "done"][0]["status"], "ready"
    )

    # Specific roll-gated obligation: interim reply before trigger fires must not satisfy it,
    # and specific promise supersedes generic umbrella on that card
    cl_key = "gerrit:skia:1490001"
    store.upsert_artifact(
        cl_key,
        kind="gerrit",
        title="1490001: Fix empty line metrics",
        state={"status": "NEW", "rolls": {}},
        now=now + 3800,
    )
    store.link_artifact(marcel_card["work_id"], cl_key, role="primary", now=now + 3800)
    spec_ob = store.add_obligation(
        marcel_card["work_id"],
        description="Send final confirmation once 1490001 rolls into Flutter",
        trigger_expr=f"cl_merged({cl_key}) AND rolled_into(Flutter, {cl_key})",
        target_artifact_key="gmail_thread:1a19coldinbound1",
        done_matcher="reply_sent(gmail_thread:1a19coldinbound1)",
        now=now + 3800,
    )
    store.upsert_artifact(
        "gmail_thread:1a19coldinbound1",
        kind="gmail_thread",
        state={"last_from_me": True, "last_message_ts": now + 3900, "unanswered_human": False},
        now=now + 3900,
    )
    store.reconcile_correspondence_obligations(marcel_card["work_id"], now=now + 3900)
    proj_spec = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 3900)
    marcel_spec = [w for w in proj_spec["works"] if w["work_id"] == marcel_card["work_id"]][0]
    open_spec_obs = [o for o in marcel_spec["obligations"] if o["status"] != "done"]
    self.assertEqual([o["obligation_id"] for o in open_spec_obs], [spec_ob])
    self.assertEqual(open_spec_obs[0]["status"], "open")

    # When 1490001 rolls at now+5000, the earlier interim reply (now+3900) does not auto-close it
    store.upsert_artifact(
        cl_key,
        kind="gerrit",
        state={"status": "MERGED", "rolls": {"Flutter": {"rolled": True}}},
        now=now + 5000,
    )
    store.evaluate_all_obligations(now=now + 5000)
    self.assertEqual((store.get_obligation(spec_ob, now=now + 5000) or {})["status"], "ready")

    # Post-ready reply at now+5100 satisfies the roll-gated obligation
    store.upsert_artifact(
        "gmail_thread:1a19coldinbound1",
        kind="gmail_thread",
        state={"last_from_me": True, "last_message_ts": now + 5100},
        now=now + 5100,
    )
    store.evaluate_all_obligations(now=now + 5100)
    self.assertEqual((store.get_obligation(spec_ob, now=now + 5100) or {})["status"], "done")

  def test_r12_ambiguity_prompt_manual_wait_and_verbal_close(self) -> None:
    import work_store
    import workbench_server

    now = time.time()
    store = work_store.WorkStore(self.db_path)

    w_tyler = store.create_work("Open Skia PDF Bugs — Tyler Denniston", now=now)
    t_key = "gmail_thread:1a17f55bb4012bd2"
    store.upsert_artifact(
        t_key,
        kind="gmail_thread",
        title="Open Skia PDF Bugs (Tyler Denniston)",
        url="https://mail.google.com/mail/u/0/#all/1a17f55bb4012bd2",
        state={
            "thread_id": "1a17f55bb4012bd2",
            "root_subject": "Open Skia PDF Bugs",
            "counterparty": "Tyler Denniston",
            "started_by_me": True,
            "sent_by_me": True,
            "last_from_me": False,
            "last_message_ts": now - 300,
            "last_message_id": "msg_thanks_1",
            "last_snippet": "Got it, thank you Julia!",
            "unanswered_human": True,
        },
        now=now,
    )
    store.link_artifact(w_tyler, t_key, role="primary", source="auto", now=now)
    store.reconcile_correspondence_obligations(w_tyler, now=now)

    with open(self.state_path, "w", encoding="utf-8") as f:
      json.dump({}, f)
    with open(self.cl_state_path, "w", encoding="utf-8") as f:
      json.dump({"items": {}, "bugs": {}}, f)

    proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)
    card = [w for w in proj["works"] if w["work_id"] == w_tyler][0]
    open_obs = [o for o in card["obligations"] if o["status"] != "done"]
    self.assertEqual(len(open_obs), 1)
    ob_id = open_obs[0]["obligation_id"]
    self.assertTrue(bool(open_obs[0].get("ambiguity_prompt")))
    self.assertEqual(open_obs[0].get("ambiguity_event_key"), "msg_thanks_1")

    html = workbench_server.render_workbench_html(proj, ui_version=1)
    self.assertIn("data-ob-keep", html)
    self.assertIn("data-ob-close-topic", html)
    self.assertIn("data-ob-wait", html)

    srv = workbench_server.create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        cl_state_path=self.cl_state_path,
        watcher_state_path=self.state_path,
        info_path=self.info_path,
    )
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
      def _post(path: str, payload: dict[str, object]) -> dict[str, object]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
          self.assertEqual(resp.status, 200)
          return json.loads(resp.read().decode("utf-8"))

      # Keep obligation -> prompt disappears and does not re-appear for msg_thanks_1
      after_keep = _post(
          "/api/obligation/resolve_ambiguity",
          {"obligation_id": ob_id, "decision": "keep", "event_key": "msg_thanks_1"},
      )
      c_keep = [w for w in after_keep["works"] if w["work_id"] == w_tyler][0]
      ob_keep = [o for o in c_keep["obligations"] if o["status"] != "done"][0]
      self.assertFalse(bool(ob_keep.get("ambiguity_prompt")))

      # Clicking [Ответила -> жду] flips ready -> open (Waiting) with fresh 7d silence timer
      after_wait = _post("/api/obligation/wait", {"obligation_id": ob_id})
      c_wait = [w for w in after_wait["works"] if w["work_id"] == w_tyler][0]
      self.assertEqual(c_wait["lane"], "waiting")
      self.assertEqual(
          [o for o in c_wait["obligations"] if o["status"] != "done"][0]["status"], "open"
      )

      # Newer courtesy message triggers ambiguity prompt once for msg_thanks_2; closing completes
      t2 = time.time() + 100
      store.upsert_artifact(
          t_key,
          kind="gmail_thread",
          state={
              "last_from_me": False,
              "last_message_ts": t2,
              "last_message_id": "msg_thanks_2",
              "last_snippet": "Sounds good, thanks!",
              "unanswered_human": True,
          },
          now=t2,
      )
      store.reconcile_correspondence_obligations(w_tyler, now=t2)
      after_close = _post(
          "/api/obligation/resolve_ambiguity",
          {"obligation_id": ob_id, "decision": "close", "event_key": "msg_thanks_2"},
      )
      c_closed = [w for w in after_close["works"] if w["work_id"] == w_tyler][0]
      self.assertEqual(c_closed["lane"], "done")
    finally:
      srv.shutdown()
      srv.server_close()

  def test_r12_gdoc_comments_direct_shares_and_transcript_reconciliation(self) -> None:
    import base64
    from datetime import datetime, timezone
    from unittest import mock
    import ingestor
    import work_store
    import workbench_server

    now = 1762450000.0
    store = work_store.WorkStore(self.db_path)

    def _b64(text: str) -> str:
      return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")

    doc_active = "1v9KDb3WgPWQQGoMdS88qqTWF_l-rhFa2cjvxZHC4yh0"
    doc_shared_out = "1GA7uMBIOEzgiUAk3-OQD08JMRVkBy2cv-wwFNSatQ3o"
    doc_passive = "18aNyOgR4VUG0VzounQ_7IZyD_Uia5ynM5nXhJ5WxIh8"
    doc_org_blast = "1zzzzOrgWideBlastDocId0000000000000000000000"

    # Existing work holds bare gdoc placeholder + passive reference doc on another work
    w_font = store.create_work("Skia Font Manager optimization", project_id="skia", now=now - 7200)
    store.upsert_artifact(
        f"gdoc:{doc_active}",
        kind="gdoc",
        title=f"gdoc:{doc_active}",
        url=f"https://docs.google.com/document/d/{doc_active}/edit",
        now=now - 7200,
    )
    store.link_artifact(w_font, f"gdoc:{doc_active}", role="primary", now=now - 7200)

    w_passive = store.create_work("1419999: Passive reference doc CL", project_id="skia", now=now)
    store.upsert_artifact(
        "gerrit:skia:1419999",
        kind="gerrit",
        title="1419999: Passive reference doc CL",
        state={"status": "NEW"},
        now=now,
    )
    store.upsert_artifact(
        f"gdoc:{doc_passive}",
        kind="gdoc",
        title=f"gdoc:{doc_passive}",
        url=f"https://docs.google.com/document/d/{doc_passive}/edit",
        now=now,
    )
    store.link_artifact(w_passive, "gerrit:skia:1419999", role="primary", now=now)
    store.link_artifact(w_passive, f"gdoc:{doc_passive}", role="primary", now=now)

    comment_plain = (
        "Kenneth Koh (kennethkohmk@google.com) mentioned you in a comment in the\n"
        "following document\n"
        'Skia response to "Flutter & Skia Font Manager Optimization"\n'
        f"(https://docs.google.com/document/d/{doc_active}/edit"
        "?disco=doco.BMSJgpkZmEwW7S-9CIjiIg&usp=comment_email_document)\n\n"
        "Matt Boetger\nWe do not have a plan right now to move to Fontations.\n\n"
        "Change what Google sends you "
        "(https://docs.google.com/document/u/1/docos/notify?"
        f"id={doc_active}&title=Skia+response+to+%22Flutter+%26+Skia+Font+Manager+Optimization%22)"
    )

    def fake_gmail_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
      res = subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
      if "search" in cmd:
        q = cmd[cmd.index("search") + 1]
        if "comments-noreply@docs.google.com" in q:
          res.stdout = json.dumps([
              {"threadId": "1a181b7efb9b1211"},
              {"threadId": "1a17edabfd5cd6bc"},
              {"threadId": "1a1a3c43a8936d02"},
          ])
      elif "get-thread" in cmd:
        tid = cmd[cmd.index("get-thread") + 1]
        if tid == "1a181b7efb9b1211":
          res.stdout = json.dumps({
              "messages": [{
                  "id": "m_doc_comment_1",
                  "internalDate": str(int((now - 900) * 1000)),
                  "labelIds": ["INBOX", "CATEGORY_UPDATES"],
                  "snippet": "Kenneth Koh mentioned you in a comment",
                  "payload": {
                      "mimeType": "text/plain",
                      "headers": [
                          {
                              "name": "Subject",
                              "value": "Skia response to ... - Thank you for the concrete steps...",
                          },
                          {
                              "name": "From",
                              "value": (
                                  '"Matt Boetger (Google Docs)" '
                                  "<comments-noreply@docs.google.com>"
                              ),
                          },
                          {"name": "To", "value": "jlavrova@google.com"},
                      ],
                      "body": {"data": _b64(comment_plain)},
                  },
              }]
          })
        elif tid == "1a17edabfd5cd6bc":
          res.stdout = json.dumps({
              "messages": [{
                  "id": "m_doc_share_out",
                  "internalDate": str(int((now - 1200) * 1000)),
                  "labelIds": ["SENT"],
                  "snippet": "Julia Lavrova has invited you to comment on the following document",
                  "payload": {
                      "mimeType": "text/plain",
                      "headers": [
                          {
                              "name": "Subject",
                              "value": (
                                  'Document shared with you: "Eliminating ICU from Flutter on '
                                  'Apple Platforms"'
                              ),
                          },
                          {
                              "name": "From",
                              "value": (
                                  '"Julia Lavrova (via Google Docs)" '
                                  "<drive-shares-dm-noreply@google.com>"
                              ),
                          },
                          {"name": "To", "value": "Renzo Olivares <roliv@google.com>"},
                      ],
                      "body": {
                          "data": _b64(
                              "Julia Lavrova invited you to comment: "
                              f"https://docs.google.com/document/d/{doc_shared_out}/edit"
                          )
                      },
                  },
              }]
          })
        elif tid == "1a1a3c43a8936d02":
          res.stdout = json.dumps({
              "messages": [{
                  "id": "m_doc_blast",
                  "internalDate": str(int((now - 500) * 1000)),
                  "labelIds": ["INBOX", "CATEGORY_UPDATES"],
                  "snippet": "Terry Shi shared a document with chrome-eng@google.com",
                  "payload": {
                      "mimeType": "text/plain",
                      "headers": [
                          {
                              "name": "Subject",
                              "value": 'Document shared with you: "Chrome Spoofer Identification"',
                          },
                          {
                              "name": "From",
                              "value": (
                                  '"Terry Shi (via Google Docs)" '
                                  "<drive-shares-dm-noreply@google.com>"
                              ),
                          },
                          {"name": "To", "value": "chrome-eng@google.com"},
                      ],
                      "body": {
                          "data": _b64(
                              f"https://docs.google.com/document/d/{doc_org_blast}/edit"
                          )
                      },
                  },
              }]
          })
      return res

    w_state: dict[str, object] = {}
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_gmail_run
    ):
      ingestor.ingest_gmail_drafts_and_threads(store, w_state, now=now)

    # Read-only transcript upgrades passive doc's human title without minting a Work or obligation
    ro_iso = datetime.fromtimestamp(now + 10, tz=timezone.utc).isoformat()
    ro_transcript = os.path.join(self.tmp_dir.name, "ro_gdoc.jsonl")
    with open(ro_transcript, "w", encoding="utf-8") as f:
      f.write(
          json.dumps({
              "type": "USER_INPUT",
              "created_at": ro_iso,
              "content": f"Прочитай спек https://docs.google.com/document/d/{doc_passive}/edit",
          })
          + "\n"
      )
      f.write(
          json.dumps({
              "type": "PLANNER_RESPONSE",
              "created_at": ro_iso,
              "tool_calls": [{
                  "name": "call_mcp_tool",
                  "args": {
                      "ServerName": "google_drive_google_drive",
                      "ToolName": "read_file_content",
                      "Arguments": json.dumps({"fileId": doc_passive, "includeComments": True}),
                  },
              }],
          })
          + "\n"
      )
      f.write(
          json.dumps({
              "type": "GENERIC",
              "created_at": ro_iso,
              "content": json.dumps({
                  "id": doc_passive,
                  "title": "Paragraph Line Metrics Reference Spec",
              }),
          })
          + "\n"
      )
    ingestor.ingest_transcript_file(
        store,
        conv_id="conv-ro-gdoc-1",
        root_cid="conv-ro-gdoc-1",
        transcript_path=ro_transcript,
        cursors={},
        now=now + 10,
    )

    proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 20)
    self.assertFalse(
        any(
            a["artifact_key"] == f"gdoc:{doc_org_blast}"
            for w in proj["works"]
            for a in w["artifacts"]
        )
    )

    # Passive reference GDoc got its real human title, but has ZERO obligations
    passive_card = [w for w in proj["works"] if w["work_id"] == w_passive][0]
    passive_doc_art = [
        a for a in passive_card["artifacts"] if a["artifact_key"] == f"gdoc:{doc_passive}"
    ][0]
    self.assertEqual(passive_doc_art["title"], "Paragraph Line Metrics Reference Spec")
    self.assertEqual([o for o in passive_card["obligations"] if o["status"] != "done"], [])

    # Active GDoc comment thread upgraded doc title, attached #disco thread, and woke Needs You
    font_card = [w for w in proj["works"] if w["work_id"] == w_font][0]
    self.assertEqual(font_card["lane"], "needs-you")
    font_arts = {a["artifact_key"]: a for a in font_card["artifacts"]}
    self.assertEqual(
        font_arts[f"gdoc:{doc_active}"]["title"],
        'Skia response to "Flutter & Skia Font Manager Optimization"',
    )
    gt_key = f"gdoc_thread:{doc_active}:doco.BMSJgpkZmEwW7S-9CIjiIg"
    self.assertIn(gt_key, font_arts)
    self.assertIn("disco=doco.BMSJgpkZmEwW7S-9CIjiIg", font_arts[gt_key]["url"])
    self.assertEqual(
        [o for o in font_card["obligations"] if o["status"] != "done"][0]["status"], "ready"
    )

    # Outbound GDoc share by Julia created an open obligation in Waiting
    shared_cards = [
        w for w in proj["works"] if "Eliminating ICU from Flutter on Apple Platforms" in w["title"]
    ]
    self.assertEqual(len(shared_cards), 1)
    self.assertEqual(shared_cards[0]["lane"], "waiting")
    self.assertEqual(
        [o for o in shared_cards[0]["obligations"] if o["status"] != "done"][0]["status"], "open"
    )

    html = workbench_server.render_workbench_html(proj, ui_version=1)
    self.assertIn('data-kind="gdoc_thread"', html)

    # Replying via Jetski MCP add_comment_reply flips GDoc thread ready -> open (Waiting)
    reply_iso = datetime.fromtimestamp(now + 60, tz=timezone.utc).isoformat()
    reply_transcript = os.path.join(self.tmp_dir.name, "reply_gdoc.jsonl")
    with open(reply_transcript, "w", encoding="utf-8") as f:
      f.write(
          json.dumps({
              "type": "USER_INPUT",
              "created_at": reply_iso,
              "content": (
                  f"Ответь Мэтту в доке https://docs.google.com/document/d/{doc_active}/edit"
              ),
          })
          + "\n"
      )
      f.write(
          json.dumps({
              "type": "PLANNER_RESPONSE",
              "created_at": reply_iso,
              "tool_calls": [{
                  "name": "call_mcp_tool",
                  "args": {
                      "ServerName": "google_drive_google_drive",
                      "ToolName": "add_comment_reply",
                      "Arguments": json.dumps({
                          "fileId": doc_active,
                          "commentId": "BMSJgpkZmEwW7S-9CIjiIg",
                          "content": "Sounds good, parked until Fontations is revisited.",
                      }),
                  },
              }],
          })
          + "\n"
      )
    cursors: dict[str, dict[str, float | int]] = {}
    ingestor.ingest_transcript_file(
        store,
        conv_id="conv-reply-gdoc-1",
        root_cid="conv-reply-gdoc-1",
        transcript_path=reply_transcript,
        cursors=cursors,
        now=now + 60,
    )
    proj_after_reply = store.build_dashboard_projection(
        cl_watcher_state={"items": {}}, now=now + 60
    )
    font_after_reply = [w for w in proj_after_reply["works"] if w["work_id"] == w_font][0]
    self.assertEqual(font_after_reply["lane"], "waiting")
    self.assertEqual(
        [o for o in font_after_reply["obligations"] if o["status"] != "done"][0]["status"], "open"
    )

    # Resolving the comment thread via MCP resolve_comment_thread closes its obligation
    resolve_iso = datetime.fromtimestamp(now + 120, tz=timezone.utc).isoformat()
    with open(reply_transcript, "a", encoding="utf-8") as f:
      f.write(
          json.dumps({
              "type": "PLANNER_RESPONSE",
              "created_at": resolve_iso,
              "tool_calls": [{
                  "name": "call_mcp_tool",
                  "args": {
                      "ServerName": "google_drive_google_drive",
                      "ToolName": "resolve_comment_thread",
                      "Arguments": json.dumps({
                          "fileId": doc_active,
                          "commentId": "BMSJgpkZmEwW7S-9CIjiIg",
                      }),
                  },
              }],
          })
          + "\n"
      )
    ingestor.ingest_transcript_file(
        store,
        conv_id="conv-reply-gdoc-1",
        root_cid="conv-reply-gdoc-1",
        transcript_path=reply_transcript,
        cursors=cursors,
        now=now + 120,
    )
    proj_resolved = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 120)
    font_resolved = [w for w in proj_resolved["works"] if w["work_id"] == w_font][0]
    self.assertEqual([o for o in font_resolved["obligations"] if o["status"] != "done"], [])

  def test_r12_reconciliation_and_sync_invariants(self) -> None:
    import base64
    from datetime import datetime, timezone
    from unittest import mock
    import ingestor
    import work_store
    import workctl

    now = 1762500000.0
    store = work_store.WorkStore(self.db_path)

    def _b64(text: str) -> str:
      return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")

    # B5_S1: 1-message inbound opener ending with "Thanks, Alice" is NOT a thread closure
    self.assertEqual(
        work_store.detect_borderline_correspondence_closure(
            "gmail_thread",
            {
                "last_message_id": "m_open_1",
                "last_message_ts": now - 60,
                "last_from_me": False,
                "sent_by_me": False,
                "message_count": 1,
                "last_unquoted_text": (
                    "Hi Julia, SkParagraph crashes on RTL ellipsis in b/399999999. Thanks, Alice"
                ),
            },
        ),
        ("", ""),
    )

    # B1_S1 + B3_S1: idempotent ready_at on draft obligations; sent draft keeps 7d Waiting umbrella
    w_draft = store.create_work("1414545: Draft follow-up", project_id="skia", now=now)
    store.upsert_artifact(
        "gerrit:skia:1414545",
        kind="gerrit",
        title="1414545: Fix strut metrics",
        state={"status": "MERGED"},
        now=now,
    )
    store.upsert_artifact(
        "gmail_thread:1a19draftthread1",
        kind="gmail_thread",
        title="Strut metrics report",
        state={
            "thread_id": "1a19draftthread1",
            "started_by_me": True,
            "sent_by_me": True,
            "last_from_me": True,
            "last_message_ts": now - 600,
        },
        now=now,
    )
    store.upsert_artifact(
        "gmail_draft:r_draft_1",
        kind="gmail_draft",
        title="Draft: Strut metrics report",
        state={"status": "DRAFT", "thread_id": "1a19draftthread1"},
        now=now,
    )
    store.link_artifact(w_draft, "gerrit:skia:1414545", role="primary", now=now)
    store.link_artifact(w_draft, "gmail_thread:1a19draftthread1", role="channel", now=now)
    store.reconcile_correspondence_obligations(w_draft, now=now)
    store.link_artifact(w_draft, "gmail_draft:r_draft_1", role="channel", now=now)
    ob_draft = store.replace_draft_obligation(
        w_draft,
        "gmail_draft:r_draft_1",
        "Send draft",
        "cl_merged(gerrit:skia:1414545)",
        done_matcher="draft_sent(gmail_draft:r_draft_1)",
        now=now,
    )
    store.replace_draft_obligation(
        w_draft,
        "gmail_draft:r_draft_1",
        "Send draft",
        "cl_merged(gerrit:skia:1414545)",
        done_matcher="draft_sent(gmail_draft:r_draft_1)",
        now=now + 90,
    )
    ob_draft_row = store.get_obligation(ob_draft, now=now + 90) or {}
    self.assertEqual(json.loads(ob_draft_row.get("meta_json") or "{}").get("ready_at"), now)

    store.upsert_artifact("gmail_draft:r_draft_1", state={"status": "SENT"}, now=now + 200)
    store.upsert_artifact(
        "gmail_thread:1a19draftthread1",
        state={"sent_by_me": True, "last_from_me": True, "last_message_ts": now + 200},
        now=now + 200,
    )
    store.reconcile_correspondence_obligations(w_draft, now=now + 200)
    proj_sent = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 201)
    d_card = [w for w in proj_sent["works"] if w["work_id"] == w_draft][0]
    self.assertEqual(d_card["lane"], "waiting")
    self.assertEqual(
        [o for o in d_card["obligations"] if o["status"] != "done"][0]["status"], "open"
    )
    proj_sent_8d = store.build_dashboard_projection(
        cl_watcher_state={"items": {}}, now=now + 200 + 8 * 86400
    )
    d_card_8d = [w for w in proj_sent_8d["works"] if w["work_id"] == w_draft][0]
    self.assertEqual(d_card_8d["lane"], "needs-you")

    # B1_S1b + B2_S1 + B6_S1: unrelated CL obligation, unlink pruning, merge, clock skew
    w_mix = store.create_work("Mixed CL + GDoc channel", project_id="skia", now=now)
    store.upsert_artifact(
        "gerrit:skia:1415000", kind="gerrit", state={"status": "NEW"}, now=now
    )
    gt_mix = "gdoc_thread:1DocMix000000000000000000:doco.c1"
    store.upsert_artifact(
        gt_mix,
        kind="gdoc_thread",
        state={
            "doc_id": "1DocMix000000000000000000",
            "disco_id": "doco.c1",
            "last_from_me": False,
            "last_message_ts": now + 50,
            "unanswered_human": True,
            "is_active_correspondence": True,
        },
        now=now,
    )
    store.link_artifact(w_mix, "gerrit:skia:1415000", role="primary", now=now)
    store.link_artifact(w_mix, gt_mix, role="channel", now=now)
    store.reconcile_correspondence_obligations(w_mix, now=now)
    store.add_obligation(
        w_mix,
        description="Verify perf after 1415000 merges",
        trigger_expr="cl_merged(gerrit:skia:1415000)",
        target_artifact_key="gerrit:skia:1415000",
        now=now + 5,
    )
    def _work_obs(wid: str, ts: float) -> list[dict[str, object]]:
      p_cur = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=ts)
      return [w for w in p_cur["works"] if w["work_id"] == wid][0]["obligations"]

    mix_obs = [
        o
        for o in _work_obs(w_mix, now + 5)
        if o["kind"] == "correspondence" and o["status"] != "done"
    ]
    self.assertEqual(len(mix_obs), 1)
    self.assertEqual(mix_obs[0]["status"], "ready")

    # B6_S1: closing when last_message_ts (now+50) > cur_now (now+10) must not reopen on next sync
    store.mark_obligation_done(str(mix_obs[0]["obligation_id"]), now=now + 10)
    store.reconcile_correspondence_obligations(w_mix, now=now + 15)
    self.assertEqual(
        (store.get_obligation(str(mix_obs[0]["obligation_id"]), now=now + 15) or {})["status"],
        "done",
    )

    # B2_S1: unlink closes orphaned correspondence obligation; merge_works deduplicates umbrellas
    w_src = store.create_work("Source mail work", now=now)
    store.upsert_artifact(
        "gmail_thread:1a19mergethr0001",
        kind="gmail_thread",
        state={"last_from_me": False, "last_message_ts": now, "unanswered_human": True},
        now=now,
    )
    store.link_artifact(w_src, "gmail_thread:1a19mergethr0001", role="channel", now=now)
    store.reconcile_correspondence_obligations(w_src, now=now)
    store.merge_works(w_mix, w_src, now=now + 20)
    store.reconcile_correspondence_obligations(w_mix, now=now + 20)
    merged_corr = [
        o
        for o in _work_obs(w_mix, now + 20)
        if o["target_artifact_key"] == "gmail_thread:1a19mergethr0001" and o["status"] != "done"
    ]
    self.assertEqual(len(merged_corr), 1)
    store.unlink_artifact(w_mix, "gmail_thread:1a19mergethr0001", now=now + 25)
    after_unlink = [
        o
        for o in _work_obs(w_mix, now + 25)
        if o["target_artifact_key"] == "gmail_thread:1a19mergethr0001" and o["status"] != "done"
    ]
    self.assertEqual(after_unlink, [])

    # B7_S1 + B5_S2: workctl new --channel gdoc:<id> & run_sync_cycle(include_gmail=False)
    rc = workctl.main([
        "--db",
        self.db_path,
        "new",
        "Discuss Fontations rollout doc",
        "--channel",
        "gdoc:1ManualDocChannel000000000",
    ])
    self.assertEqual(rc, 0)
    empty_brain = os.path.join(self.tmp_dir.name, "empty_brain")
    os.makedirs(empty_brain, exist_ok=True)
    with open(self.cl_state_path, "w", encoding="utf-8") as f:
      json.dump({"items": {}, "bugs": {}}, f)
    with open(self.state_path, "w", encoding="utf-8") as f:
      json.dump({"last_gmail_sync": now}, f)
    proj_nogmail = ingestor.run_sync_cycle(
        store,
        cl_state_path=self.cl_state_path,
        watcher_state_path=self.state_path,
        brain_dir=empty_brain,
        include_gmail=False,
        now=now + 30,
    )
    manual_card = [
        w for w in proj_nogmail["works"] if "Discuss Fontations rollout doc" in w["title"]
    ][0]
    self.assertEqual(
        len([o for o in manual_card["obligations"] if o["status"] != "done"]), 1
    )

    # B1_S2 + B2_S2 + B3_S2 + B4_S1/S2 + B6_S2: transcript-before-gmail without "doco.",
    # direct MCP resolve surviving Gmail re-sync, reopening on new comment, >4 CL transcript,
    # notify?title= first param, courtesy reply on outbound share, and dismiss_tray_thread
    self.assertEqual(
        store.dismiss_tray_thread("gdoc_thread:1DismissDoc000000000000:doco.x1", now=now),
        "gdoc_thread:1DismissDoc000000000000:doco.x1",
    )
    self.assertIn(
        "gdoc_thread:1DismissDoc000000000000:doco.x1", store.list_dismissed_tray_keys()
    )

    doc_order = "1OrderDoc0000000000000000000000000000000000"
    doc_share2 = "1ShareCourtesy00000000000000000000000000000"
    w_order = store.create_work("Order test doc", project_id="skia", now=now)
    store.upsert_artifact(f"gdoc:{doc_order}", kind="gdoc", title=f"gdoc:{doc_order}", now=now)
    store.link_artifact(w_order, f"gdoc:{doc_order}", role="primary", now=now)
    # Also pre-link transport gmail_thread to verify B4_S1a superseding
    store.upsert_artifact("gmail_thread:1a19gdocorder001", kind="gmail_thread", now=now)
    store.link_artifact(w_order, "gmail_thread:1a19gdocorder001", role="channel", now=now)

    # Step 1: transcript (>4 non-hub CLs!) calls resolve_comment_thread WITHOUT "doco." BEFORE Gmail
    multi_cl_tpath = os.path.join(self.tmp_dir.name, "multi_cl_resolve.jsonl")
    ts_res_iso = datetime.fromtimestamp(now + 40, tz=timezone.utc).isoformat()
    with open(multi_cl_tpath, "w", encoding="utf-8") as f:
      f.write(
          json.dumps({
              "type": "PLANNER_RESPONSE",
              "created_at": ts_res_iso,
              "tool_calls": [
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "echo https://skia-review.googlesource.com/c/skia/+/1400001 "
                              "https://skia-review.googlesource.com/c/skia/+/1400002 "
                              "https://skia-review.googlesource.com/c/skia/+/1400003 "
                              "https://skia-review.googlesource.com/c/skia/+/1400004 "
                              "https://skia-review.googlesource.com/c/skia/+/1400005"
                          )
                      },
                  },
                  {
                      "name": "call_mcp_tool",
                      "args": {
                          "ServerName": "google_drive_google_drive",
                          "ToolName": "resolve_comment_thread",
                          "Arguments": json.dumps({
                              "fileId": doc_order,
                              "commentId": "C_ORDER_1",
                          }),
                      },
                  },
              ],
          })
          + "\n"
      )
    ingestor.ingest_transcript_file(
        store,
        conv_id="conv-multi-cl-1",
        root_cid="conv-multi-cl-1",
        transcript_path=multi_cl_tpath,
        cursors={},
        chat_title="Investigate comment across CLs",
        now=now + 40,
    )

    # Step 2: Gmail sync arrives with older comment email (now - 300) using ?disco=doco.C_ORDER_1
    # and notify?title=First+Param+Title, plus outbound share with courtesy reply
    gdoc_state = {"reopened": False}

    def fake_gmail_edge(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
      res = subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
      if "search" in cmd:
        q = cmd[cmd.index("search") + 1]
        if "comments-noreply@docs.google.com" in q:
          res.stdout = json.dumps([
              {"threadId": "1a19gdocorder001"},
              {"threadId": "1a19sharecourtesy"},
          ])
      elif "get-thread" in cmd:
        tid = cmd[cmd.index("get-thread") + 1]
        if tid == "1a19gdocorder001":
          html_body = (
              f'<a href="https://docs.google.com/document/d/{doc_order}/edit'
              '?disco=doco.C_ORDER_1">Open</a> '
              '<a href="https://docs.google.com/document/u/0/docos/notify'
              f'?title=First+Param+Title&id={doc_order}">Notify</a>'
          )
          msgs = [{
              "id": "m_ord_1",
              "internalDate": str(int((now - 300) * 1000)),
              "labelIds": ["INBOX", "CATEGORY_UPDATES"],
              "snippet": "Ben mentioned you in a comment",
              "payload": {
                  "mimeType": "text/html",
                  "headers": [
                      {"name": "Subject", "value": "Comment notification"},
                      {
                          "name": "From",
                          "value": '"Ben Wagner (Google Docs)" <comments-noreply@docs.google.com>',
                      },
                      {"name": "To", "value": "jlavrova@google.com"},
                  ],
                  "body": {"data": _b64(html_body)},
              },
          }]
          if gdoc_state["reopened"]:
            msgs.append({
                "id": "m_ord_2",
                "internalDate": str(int((now + 300) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_UPDATES"],
                "snippet": "Wait, what about CoreText?",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "Subject", "value": "Comment notification"},
                        {
                            "name": "From",
                            "value": (
                                '"Ben Wagner (Google Docs)" <comments-noreply@docs.google.com>'
                            ),
                        },
                        {"name": "To", "value": "jlavrova@google.com"},
                    ],
                    "body": {
                        "data": _b64(
                            f"https://docs.google.com/document/d/{doc_order}/edit"
                            "?disco=doco.C_ORDER_1\n\nWait, what about CoreText?"
                        )
                    },
                },
            })
          res.stdout = json.dumps({"messages": msgs})
        elif tid == "1a19sharecourtesy":
          res.stdout = json.dumps({
              "messages": [
                  {
                      "id": "m_sh_1",
                      "internalDate": str(int((now - 600) * 1000)),
                      "labelIds": ["SENT"],
                      "snippet": "Shared doc",
                      "payload": {
                          "mimeType": "text/plain",
                          "headers": [
                              {"name": "Subject", "value": 'Document shared with you: "Perf Spec"'},
                              {
                                  "name": "From",
                                  "value": (
                                      '"Julia Lavrova (via Google Docs)" '
                                      "<drive-shares-dm-noreply@google.com>"
                                  ),
                              },
                              {"name": "To", "value": "Renzo Olivares <roliv@google.com>"},
                          ],
                          "body": {
                              "data": _b64(
                                  f"https://docs.google.com/document/d/{doc_share2}/edit"
                              )
                          },
                      },
                  },
                  {
                      "id": "m_sh_2",
                      "internalDate": str(int((now - 100) * 1000)),
                      "labelIds": ["INBOX"],
                      "snippet": "Looks great, thanks Julia!",
                      "payload": {
                          "mimeType": "text/plain",
                          "headers": [
                              {
                                  "name": "Subject",
                                  "value": 'Re: Document shared with you: "Perf Spec"',
                              },
                              {"name": "From", "value": "Renzo Olivares <roliv@google.com>"},
                              {"name": "To", "value": "jlavrova@google.com"},
                          ],
                          "body": {"data": _b64("Looks great, thanks Julia!")},
                      },
                  },
              ]
          })
      return res

    ws_edge: dict[str, object] = {}
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_gmail_edge
    ):
      ingestor.ingest_gmail_drafts_and_threads(store, ws_edge, now=now + 60)

    proj_edge = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 60)
    ord_card = [w for w in proj_edge["works"] if w["work_id"] == w_order][0]
    ord_gthreads = [a for a in ord_card["artifacts"] if a["kind"] == "gdoc_thread"]
    self.assertEqual(len(ord_gthreads), 1)
    ord_docs = [a for a in ord_card["artifacts"] if a["kind"] == "gdoc"]
    self.assertEqual(ord_docs[0]["title"], "First Param Title")
    self.assertNotEqual(ord_card["lane"], "needs-you")
    self.assertEqual([o for o in ord_card["obligations"] if o["status"] != "done"], [])

    # Courtesy reply on outbound shared doc surfaces ambiguity prompt
    sh_card = [w for w in proj_edge["works"] if "Perf Spec" in w["title"]][0]
    sh_obs = [o for o in sh_card["obligations"] if o["status"] != "done"]
    self.assertTrue(bool(sh_obs[0].get("ambiguity_prompt")))

    # Completing sh_card must NOT re-mint a duplicate active Work on the next Gmail sync
    store.complete_work(sh_card["work_id"], now=now + 70)
    ws_edge["gmail_threads"] = {}
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_gmail_edge
    ):
      ingestor.ingest_gmail_drafts_and_threads(store, ws_edge, now=now + 80)
    proj_after_done = store.build_dashboard_projection(
        cl_watcher_state={"items": {}}, now=now + 80
    )
    self.assertEqual(
        [
            w
            for w in proj_after_done["works"]
            if "Perf Spec" in w["title"] and w["lane"] != "done"
        ],
        [],
    )

    # Step 3: Newer colleague comment at now+300 reopens the resolved #disco thread into Needs You
    gdoc_state["reopened"] = True
    ws_edge["gmail_threads"] = {}
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_gmail_edge
    ):
      ingestor.ingest_gmail_drafts_and_threads(store, ws_edge, now=now + 310)
    proj_reopen = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 310)
    ord_reopen = [w for w in proj_reopen["works"] if w["work_id"] == w_order][0]
    self.assertEqual(ord_reopen["lane"], "needs-you")
    self.assertEqual(
        [o for o in ord_reopen["obligations"] if o["status"] != "done"][0]["status"], "ready"
    )

  def test_r13_gmail_drafts_above_ten_and_cli_failure_never_false_sent(self) -> None:
    """Verifies >10 active Gmail drafts and failed CLI runs never falsely mark drafts SENT."""
    import base64
    from unittest import mock
    import ingestor
    import work_store

    now = 1762600000.0
    store = work_store.WorkStore(self.db_path)
    store.upsert_artifact(
        "gerrit:skia:1414016",
        kind="gerrit",
        title="1414016: Fix getRectsForRange empty lines",
        url="https://skia-review.googlesource.com/c/skia/+/1414016",
        state={"status": "MERGED", "rolls": {"Flutter": {"rolled": False}}},
        now=now,
    )
    b64_12 = base64.urlsafe_b64encode(
        b'<p>Hi Renzo, fixed in <a href="https://skia-review.googlesource.com/c/skia/+/1414016">'
        b"CL 1414016</a></p>"
    ).decode("ascii")
    b64_noise = base64.urlsafe_b64encode(b"<p>Scratch note</p>").decode("ascii")

    mode = {"cli_fail": False, "drop_12": False}
    seen_drafts_cmds: list[list[str]] = []

    def fake_gmail(
        cmd: list[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
      del args, kwargs
      res = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
      if "drafts" in cmd:
        seen_drafts_cmds.append(list(cmd))
        if mode["cli_fail"]:
          res.returncode = 1
          res.stderr = "transient LOAS timeout"
          return res
        ids = [f"r_d_{i:02d}" for i in range(1, 13)]
        if mode["drop_12"]:
          ids = [x for x in ids if x != "r_d_12"]
        res.stdout = json.dumps([
            {"id": did, "threadId": "1a19elevendraft" if did == "r_d_12" else f"t_{did}"}
            for did in ids
        ])
      elif "get-draft" in cmd:
        did = cmd[cmd.index("get-draft") + 1]
        is_target = did == "r_d_12"
        res.stdout = json.dumps({
            "id": did,
            "message": {
                "id": f"m_{did}",
                "threadId": "1a19elevendraft" if is_target else f"t_{did}",
                "internalDate": str(int((now - 120) * 1000)),
                "payload": {
                    "headers": [
                        {
                            "name": "Subject",
                            "value": "Re: Fixed bugs in Flutter" if is_target else f"Note {did}",
                        },
                        {"name": "To", "value": "Renzo Olivares <roliv@google.com>"},
                    ],
                    "body": {"data": b64_12 if is_target else b64_noise},
                },
            },
        })
      elif "get-thread" in cmd:
        res.stdout = json.dumps({
            "messages": [{
                "id": "m_orig_12",
                "internalDate": str(int((now - 600) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
                "snippet": "Any update on Flutter bugs?",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Fixed bugs in Flutter"},
                        {"name": "From", "value": "Renzo Olivares <roliv@google.com>"},
                        {"name": "To", "value": "Julia Lavrova <jlavrova@google.com>"},
                    ]
                },
            }]
        })
      elif "search" in cmd:
        res.stdout = json.dumps({"messages": []})
      return res

    ws: dict[str, object] = {}
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_gmail
    ):
      ingestor.ingest_gmail_drafts_and_threads(store, ws, now=now)
      # Second sync with all 12 drafts still active must keep the 12th draft in DRAFT status
      ingestor.ingest_gmail_drafts_and_threads(store, ws, now=now + 90)

    self.assertGreaterEqual(len(seen_drafts_cmds), 2)
    self.assertIn("--max", seen_drafts_cmds[0])
    d12 = store.get_artifact("gmail_draft:r_d_12") or {}
    self.assertEqual((d12.get("state") or {}).get("status"), "DRAFT")
    active_ids = ws.get("active_draft_ids")
    self.assertIsInstance(active_ids, list)
    assert isinstance(active_ids, list)
    self.assertEqual(len(active_ids), 12)

    # Transient CLI failure when listing drafts must never mark previously tracked drafts SENT
    mode["cli_fail"] = True
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_gmail
    ):
      ingestor.ingest_gmail_drafts_and_threads(store, ws, now=now + 180)
    self.assertEqual(
        ((store.get_artifact("gmail_draft:r_d_12") or {}).get("state") or {}).get("status"),
        "DRAFT",
    )

    # Clean list response omitting r_d_12 marks it SENT
    mode["cli_fail"] = False
    mode["drop_12"] = True
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_gmail
    ):
      ingestor.ingest_gmail_drafts_and_threads(store, ws, now=now + 270)
    self.assertEqual(
        ((store.get_artifact("gmail_draft:r_d_12") or {}).get("state") or {}).get("status"),
        "SENT",
    )

  def test_r13_parked_draft_wakes_card_on_newer_counterparty_reply(self) -> None:
    """Verifies parked Gmail and Workbench drafts wake into Needs You when a newer reply arrives."""
    import base64
    from unittest import mock
    import ingestor
    import work_store

    now = 1762610000.0
    store = work_store.WorkStore(self.db_path)
    store.upsert_artifact(
        "gerrit:skia:1421111",
        kind="gerrit",
        title="1421111: Fix cluster advance",
        url="https://skia-review.googlesource.com/c/skia/+/1421111",
        state={
            "status": "NEW",
            "is_mine": True,
            "assigned_reviewers_count": 1,
            "entity_state": {"lane": "waiting", "status": "waiting"},
        },
        now=now - 500,
    )
    b64_draft = base64.urlsafe_b64encode(
        b'<p>Uploaded <a href="https://skia-review.googlesource.com/c/skia/+/1421111">'
        b"CL 1421111</a></p>"
    ).decode("ascii")
    b64_msg1 = base64.urlsafe_b64encode(b"Could you check cluster advance?").decode("ascii")
    b64_msg2 = base64.urlsafe_b64encode(b"Wait, it also crashes on RTL!").decode("ascii")
    thread_stage = {"new_reply": False}

    def fake_gmail(
        cmd: list[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
      del args, kwargs
      res = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
      if "drafts" in cmd:
        res.stdout = json.dumps([{"id": "r_park_1", "threadId": "1a19parkwake1"}])
      elif "get-draft" in cmd:
        res.stdout = json.dumps({
            "id": "r_park_1",
            "message": {
                "id": "m_draft_park_1",
                "threadId": "1a19parkwake1",
                "internalDate": str(int((now - 300) * 1000)),
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Re: Fixed bugs in Flutter"},
                        {"name": "To", "value": "Renzo Olivares <roliv@google.com>"},
                    ],
                    "body": {"data": b64_draft},
                },
            },
        })
      elif "get-thread" in cmd:
        msgs = [
            {
                "id": "m_pw_0",
                "internalDate": str(int((now - 900) * 1000)),
                "labelIds": ["SENT"],
                "snippet": "Looking into it",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "Subject", "value": "Fixed bugs in Flutter"},
                        {"name": "From", "value": "Julia Lavrova <jlavrova@google.com>"},
                        {"name": "To", "value": "Renzo Olivares <roliv@google.com>"},
                    ],
                    "body": {"data": b64_msg1},
                },
            },
            {
                "id": "m_pw_1",
                "internalDate": str(int((now - 600) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
                "snippet": "Could you check cluster advance?",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "Subject", "value": "Re: Fixed bugs in Flutter"},
                        {"name": "From", "value": "Renzo Olivares <roliv@google.com>"},
                        {"name": "To", "value": "Julia Lavrova <jlavrova@google.com>"},
                    ],
                    "body": {"data": b64_msg1},
                },
            },
        ]
        if thread_stage["new_reply"]:
          msgs.append({
              "id": "m_pw_2",
              "internalDate": str(int((now + 150) * 1000)),
              "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
              "snippet": "Wait, it also crashes on RTL!",
              "payload": {
                  "mimeType": "text/plain",
                  "headers": [
                      {"name": "Subject", "value": "Re: Fixed bugs in Flutter"},
                      {"name": "From", "value": "Renzo Olivares <roliv@google.com>"},
                      {"name": "To", "value": "Julia Lavrova <jlavrova@google.com>"},
                  ],
                  "body": {"data": b64_msg2},
              },
          })
        res.stdout = json.dumps({"messages": msgs})
      elif "search" in cmd:
        q_idx = cmd.index("search") + 1
        q_str = cmd[q_idx] if q_idx < len(cmd) else ""
        if "in:inbox" in q_str or "from:me" in q_str:
          res.stdout = json.dumps({"messages": [{"id": "m_pw_1", "threadId": "1a19parkwake1"}]})
        else:
          res.stdout = json.dumps({"messages": []})
      return res

    ws: dict[str, object] = {}
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_gmail
    ):
      ingestor.ingest_gmail_drafts_and_threads(store, ws, now=now)
      # Re-sync 90s later without draft edits must not advance draft_updated_ts
      ws["gmail_threads"] = {}
      ingestor.ingest_gmail_drafts_and_threads(store, ws, now=now + 90)

    proj_wait = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 90)
    cards_wait = [w for w in proj_wait["works"] if "Fixed bugs in Flutter" in w["title"]]
    self.assertEqual(len(cards_wait), 1)
    self.assertEqual(cards_wait[0]["lane"], "waiting")

    # Counterparty replies at now+150 (newer than draft at now-300): card MUST wake into needs-you
    thread_stage["new_reply"] = True
    ws["gmail_threads"] = {}
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_gmail
    ):
      ingestor.ingest_gmail_drafts_and_threads(store, ws, now=now + 200)

    proj_wake = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 200)
    cards_wake = [w for w in proj_wake["works"] if w["work_id"] == cards_wait[0]["work_id"]]
    self.assertEqual(len(cards_wake), 1)
    self.assertEqual(cards_wake[0]["lane"], "needs-you")

    # Part B: Parked Workbench reply_draft also wakes on newer counterparty reply, then re-parks
    w_wb = store.create_work("Skia shaping question", project_id="skia", now=now)
    store.upsert_artifact(
        "gerrit:skia:1422222",
        kind="gerrit",
        title="1422222: Fix shaping cluster range",
        state={
            "status": "NEW",
            "is_mine": True,
            "assigned_reviewers_count": 1,
            "entity_state": {"lane": "waiting", "status": "waiting"},
        },
        now=now,
    )
    store.upsert_artifact(
        "gmail_thread:1a19parkwake2",
        kind="gmail_thread",
        title="Shaping cluster range (Florin)",
        state={
            "thread_id": "1a19parkwake2",
            "root_subject": "Shaping cluster range",
            "counterparty": "Florin Malita",
            "sent_by_me": True,
            "last_from_me": False,
            "last_message_ts": now - 400,
            "unanswered_human": True,
            "is_active_correspondence": True,
        },
        now=now,
    )
    store.link_artifact(w_wb, "gerrit:skia:1422222", role="primary", now=now)
    store.link_artifact(w_wb, "gmail_thread:1a19parkwake2", role="channel", now=now)
    store.save_reply_draft(
        w_wb,
        "<p>Hi Florin, uploaded CL 1422222 for review.</p>",
        auto_gate=True,
        now=now - 100,
    )
    proj_wb_wait = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)
    wb_card = [w for w in proj_wb_wait["works"] if w["work_id"] == w_wb][0]
    self.assertEqual(wb_card["lane"], "waiting")

    store.upsert_artifact(
        "gmail_thread:1a19parkwake2",
        kind="gmail_thread",
        state={
            "last_from_me": False,
            "last_message_ts": now + 120,
            "unanswered_human": True,
        },
        now=now + 120,
    )
    store.reconcile_correspondence_obligations(w_wb, now=now + 120)
    proj_wb_wake = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 120)
    wb_wake = [w for w in proj_wb_wake["works"] if w["work_id"] == w_wb][0]
    self.assertEqual(wb_wake["lane"], "needs-you")

    store.save_reply_draft(
        w_wb,
        "<p>Hi Florin, updated CL 1422222 and addressed your follow-up.</p>",
        auto_gate=True,
        now=now + 220,
    )
    proj_wb_repark = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 220)
    wb_repark = [w for w in proj_wb_repark["works"] if w["work_id"] == w_wb][0]
    self.assertEqual(wb_repark["lane"], "waiting")

  def test_r13_workbench_live_reply_draft_cli_http_and_materialize(self) -> None:
    """Verifies Workbench live reply drafts across auto-gating, checklist, CLI, HTTP, and Gmail."""
    import base64
    from unittest import mock
    import ingestor
    import work_store
    import workbench_server

    now = 1762620000.0
    store = work_store.WorkStore(self.db_path)
    wid = store.create_work("Fixed bugs in Flutter", project_id="flutter", now=now)
    t_key = "gmail_thread:1a19livedraft001"
    store.upsert_artifact(
        t_key,
        kind="gmail_thread",
        title="Fixed bugs in Flutter (Renzo Olivares)",
        url="https://mail.google.com/mail/u/0/#all/1a19livedraft001",
        state={
            "thread_id": "1a19livedraft001",
            "root_subject": "Fixed bugs in Flutter",
            "counterparty": "Renzo Olivares",
            "sent_by_me": True,
            "last_from_me": False,
            "last_message_ts": now - 300,
            "unanswered_human": True,
            "is_active_correspondence": True,
        },
        now=now,
    )
    store.upsert_artifact(
        "github_issue:flutter/flutter:160251",
        kind="github_issue",
        title="flutter#160251: Zero-width cluster letterSpacing",
        state={"status": "OPEN", "bug_title": "Zero-width cluster letterSpacing"},
        now=now,
    )
    store.upsert_artifact(
        "github_issue:flutter/flutter:155062",
        kind="github_issue",
        title="flutter#155062: getPositionForOffset justification",
        state={"status": "OPEN", "bug_title": "getPositionForOffset justification"},
        now=now,
    )
    store.link_artifact(wid, t_key, role="primary", source="auto", now=now)
    store.link_artifact(wid, "github_issue:flutter/flutter:160251", role="primary", now=now)
    store.link_artifact(wid, "github_issue:flutter/flutter:155062", role="primary", now=now)

    # Save milestone #1 via workctl CLI; comparison CL 1399999 in body must NOT become primary
    workctl_py = os.path.join(WORKBENCH_DIR, "workctl.py")
    body1_path = os.path.join(self.tmp_dir.name, "draft_m1.html")
    with open(body1_path, "w", encoding="utf-8") as f:
      f.write(
          '<p>Hi Renzo,</p><ul>'
          '<li><a href="https://github.com/flutter/flutter/issues/160251">#160251</a> '
          '(compared with <a href="https://skia-review.googlesource.com/c/skia/+/1399999">'
          "CL 1399999</a>)</li>"
          '<li><a href="https://github.com/flutter/flutter/issues/155062">#155062</a></li>'
          "</ul>"
      )
    subprocess.run(
        [
            sys.executable,
            workctl_py,
            "--db",
            self.db_path,
            "draft-set",
            wid,
            "--html",
            "--body-file",
            body1_path,
            "--auto-gate",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    self.assertNotIn("gerrit:skia:1399999", store.get_work_artifact_roles(wid))

    # Link Gerrit + PR + Piper primary changes; auto_gate refreshes and checklist flags outdated
    store.upsert_artifact(
        "gerrit:skia:1404137",
        kind="gerrit",
        title="1404137: Do not letter-space ignorable clusters",
        url="https://skia-review.googlesource.com/c/skia/+/1404137",
        state={
            "status": "NEW",
            "is_mine": True,
            "assigned_reviewers_count": 1,
            "entity_state": {"lane": "waiting", "status": "waiting"},
            "linked_bugs": [{"bug_key": "gh_issue:flutter/flutter#160251"}],
        },
        now=now + 20,
    )
    store.upsert_artifact(
        "github_pr:flutter/flutter:196137",
        kind="github_pr",
        title="PR #196137: Fix justification hit-test",
        url="https://github.com/flutter/flutter/pull/196137",
        state={
            "status": "OPEN",
            "is_mine": True,
            "assigned_reviewers_count": 1,
            "entity_state": {"lane": "waiting", "status": "waiting"},
            "commit_message": "Fixes https://github.com/flutter/flutter/issues/155062",
        },
        now=now + 20,
    )
    piper_ref = "cl" + "/1008075988"
    store.upsert_artifact(
        "piper:1008075988",
        kind="piper",
        title=f"{piper_ref}: Sync google3 golden",
        url=f"http://{piper_ref}",
        state={
            "status": "NEW",
            "is_mine": True,
            "assigned_reviewers_count": 1,
            "entity_state": {"lane": "waiting", "status": "waiting"},
        },
        now=now + 20,
    )
    store.link_artifact(wid, "gerrit:skia:1404137", role="primary", now=now + 20)
    store.link_artifact(wid, "github_pr:flutter/flutter:196137", role="primary", now=now + 20)
    store.link_artifact(wid, "piper:1008075988", role="primary", now=now + 20)
    store.reconcile_correspondence_obligations(wid, now=now + 25)

    proj_mid = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 25)
    card_mid = [w for w in proj_mid["works"] if w["work_id"] == wid][0]
    self.assertEqual(card_mid["lane"], "waiting")
    chk_mid = card_mid.get("checklist") or []
    self.assertEqual(len(chk_mid), 2)
    self.assertTrue(all(item["draft_outdated"] for item in chk_mid))

    # Update draft mentioning both fixes and merge into a target Work; draft + obligation survive
    clean_html = (
        '<p>Hi Renzo,</p><ul>'
        '<li><a href="https://github.com/flutter/flutter/issues/160251">#160251</a>: '
        '<a href="https://skia-review.googlesource.com/c/skia/+/1404137">Skia CL 1404137</a> '
        f'and <a href="http://{piper_ref}">{piper_ref}</a></li>'
        '<li><a href="https://github.com/flutter/flutter/issues/155062">#155062</a>: '
        '<a href="https://github.com/flutter/flutter/pull/196137">PR #196137</a></li>'
        "</ul>"
    )
    store.save_reply_draft(wid, clean_html, fmt="html", auto_gate=True, now=now + 40)
    wid_merged = store.create_work("Merged Flutter bugs card", project_id="flutter", now=now + 45)
    store.merge_works(wid_merged, wid, now=now + 50)
    store.reconcile_correspondence_obligations(wid_merged, now=now + 50)
    merged_rd = store.get_reply_draft(wid_merged)
    self.assertIsNotNone(merged_rd)
    assert merged_rd is not None
    self.assertIn("1404137", merged_rd["body"])

    proj_clean = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 55)
    card_clean = [w for w in proj_clean["works"] if w["work_id"] == wid_merged][0]
    chk_clean = card_clean.get("checklist") or []
    self.assertEqual(len(chk_clean), 2)
    self.assertFalse(any(item["draft_outdated"] for item in chk_clean))

    # Land all 3 changes (including Piper SUBMITTED) -> wb_reply_draft obligation becomes ready
    store.upsert_artifact(
        "gerrit:skia:1404137",
        state={"status": "MERGED", "rolls": {"Flutter": {"rolled": True}}},
        now=now + 100,
    )
    store.upsert_artifact(
        "github_pr:flutter/flutter:196137", state={"status": "MERGED"}, now=now + 100
    )
    store.upsert_artifact("piper:1008075988", state={"status": "SUBMITTED"}, now=now + 100)
    proj_ready = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 105)
    card_ready = [w for w in proj_ready["works"] if w["work_id"] == wid_merged][0]
    self.assertEqual(card_ready["lane"], "needs-you")

    # Redirect URLs in draft body must fail fast before calling Gmail CLI
    store.save_reply_draft(
        wid_merged,
        '<p>Bad <a href="https://www.google.com/url?q=https://example.com">link</a></p>',
        fmt="html",
        auto_gate=True,
        now=now + 110,
    )
    with self.assertRaises(ValueError):
      ingestor.materialize_reply_draft(store, wid_merged, now=now + 111)
    store.save_reply_draft(wid_merged, clean_html, fmt="html", auto_gate=True, now=now + 112)

    # Start HTTP server and verify /api/work/draft/{materialize,save,clear} + SSR/JS rendering
    create_calls: list[list[str]] = []
    b64_stored = base64.urlsafe_b64encode(clean_html.encode("utf-8")).decode("ascii")

    def fake_mat_gmail(
        cmd: list[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
      del args, kwargs
      res = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
      if "get-thread" in cmd:
        res.stdout = json.dumps({
            "messages": [{
                "id": "msg_latest_77",
                "threadId": "1a19livedraft001",
                "internalDate": str(int((now - 300) * 1000)),
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Fixed bugs in Flutter"},
                        {"name": "From", "value": "Renzo Olivares <roliv@google.com>"},
                        {
                            "name": "To",
                            "value": (
                                "Julia Lavrova <jlavrova@google.com>, "
                                "Extra Peer <peer@google.com>"
                            ),
                        },
                        {
                            "name": "Cc",
                            "value": (
                                "Flora <flora@google.com>, "
                                "Julia Lavrova <jlavrova@google.com>"
                            ),
                        },
                    ]
                },
            }]
        })
      elif "create-draft" in cmd:
        create_calls.append(list(cmd))
        res.stdout = "Draft created: ID=r_mat_9001\n"
      elif "get-draft" in cmd:
        res.stdout = json.dumps({
            "id": "r_mat_9001",
            "message": {
                "id": "m_mat_9001",
                "threadId": "1a19livedraft001",
                "payload": {"mimeType": "text/html", "body": {"data": b64_stored}},
            },
        })
      return res

    srv = workbench_server.create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        cl_state_path=self.cl_state_path,
        watcher_state_path=self.state_path,
        info_path=self.info_path,
    )
    port = srv.server_address[1]
    t_srv = threading.Thread(target=srv.serve_forever, daemon=True)
    t_srv.start()
    try:
      def _post(path: str, payload: dict[str, object]) -> dict[str, object]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
          return json.loads(resp.read().decode("utf-8"))

      with mock.patch("subprocess.run", side_effect=fake_mat_gmail):
        mat1 = _post("/api/work/draft/materialize", {"work_id": wid_merged})
        mat2 = _post("/api/work/draft/materialize", {"work_id": wid_merged})

      self.assertEqual(len(create_calls), 1)
      ccmd = create_calls[0]
      self.assertIn("--include-history", ccmd)
      self.assertIn("--html", ccmd)
      to_val = ccmd[ccmd.index("--to") + 1]
      cc_val = ccmd[ccmd.index("--cc") + 1]
      self.assertIn("roliv@google.com", to_val)
      self.assertIn("peer@google.com", to_val)
      self.assertNotIn("jlavrova@google.com", to_val)
      self.assertIn("flora@google.com", cc_val)
      self.assertNotIn("jlavrova@google.com", cc_val)
      self.assertEqual((mat1.get("materialized") or {}).get("draft_id"), "r_mat_9001")
      self.assertTrue(bool((mat2.get("materialized") or {}).get("reused")))

      with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
        html_page = resp.read().decode("utf-8")
      self.assertIn("wb-reply-draft", html_page)

      # Editing after materialization marks needs_rematerialize without mutating Gmail in place
      after_edit = _post(
          "/api/work/draft/save",
          {
              "work_id": wid_merged,
              "body": clean_html + "<p>P.S. Rolled today!</p>",
              "format": "html",
              "auto_gate": True,
          },
      )
      ed_card = [w for w in after_edit["works"] if w["work_id"] == wid_merged][0]
      self.assertTrue(bool((ed_card.get("reply_draft") or {}).get("needs_rematerialize")))

      # Simulate sending r_mat_9001 in Gmail -> clears reply_draft and hands off to 7d Waiting
      store.upsert_artifact("gmail_draft:r_mat_9001", state={"status": "SENT"}, now=now + 300)
      store.upsert_artifact(
          t_key,
          state={"sent_by_me": True, "last_from_me": True, "last_message_ts": now + 300},
          now=now + 300,
      )
      store.reconcile_correspondence_obligations(wid_merged, now=now + 300)
      proj_sent = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 301)
      sent_card = [w for w in proj_sent["works"] if w["work_id"] == wid_merged][0]
      self.assertIsNone(sent_card.get("reply_draft"))
      self.assertEqual(sent_card["lane"], "waiting")

      # Verify /api/work/draft/clear on a new draft
      _post(
          "/api/work/draft/save",
          {"work_id": wid_merged, "body": "Quick follow-up", "format": "text"},
      )
      cleared = _post("/api/work/draft/clear", {"work_id": wid_merged})
      clr_card = [w for w in cleared["works"] if w["work_id"] == wid_merged][0]
      self.assertIsNone(clr_card.get("reply_draft"))
    finally:
      srv.shutdown()
      srv.server_close()

  def test_r13_phase3_skeptic_confirmed_contracts(self) -> None:
    """Verifies Skeptic-confirmed R13 contracts across fast-send, discard, unlink/split, and UI."""
    import base64
    from unittest import mock
    import ingestor
    import work_store
    import workbench_server
    import workctl

    now = 1762630000.0
    store = work_store.WorkStore(self.db_path)

    # 1. Unified draft mention extraction: filters DUMMY_IDS & PR hovercards, keeps prose Skia CL
    hq_draft_body = (
        "Hi Renzo,\n\nSee Skia CL 1404137, CL 123456, b/123456789, "
        '<a data-hydro-click=\'{"url":"https://github.com/flutter/flutter/issues/197778"}\' '
        'href="https://github.com/flutter/flutter/pull/197778">PR #197778</a>, and '
        "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz123456/edit"
    )
    m_keys = ingestor.extract_draft_mentioned_keys("Re: Skia CL 1404138", hq_draft_body)
    self.assertIn("gerrit:skia:1404138", m_keys)
    self.assertIn("gerrit:skia:1404137", m_keys)
    self.assertIn("github_pr:flutter/flutter:197778", m_keys)
    self.assertIn("gdoc:1AbCdEfGhIjKlMnOpQrStUvWxYz123456", m_keys)
    self.assertNotIn("gerrit:skia:123456", m_keys)
    self.assertNotIn("buganizer:123456789", m_keys)
    self.assertNotIn("github_issue:flutter/flutter:197778", m_keys)

    # 2. Fast-send (<90s) vs discarded Gmail draft + empty-mailbox JSON + superseded force-new tab
    w_fast = store.create_work("Fast-sent materialized draft", now=now)
    store.upsert_artifact(
        "gmail_thread:1a19fast1",
        kind="gmail_thread",
        title="Fast send thread (Renzo)",
        state={
            "thread_id": "1a19fast1",
            "root_subject": "Fast send thread",
            "counterparty": "Renzo Olivares",
            "sent_by_me": True,
            "last_from_me": False,
            "last_message_ts": now - 200,
            "unanswered_human": True,
            "is_active_correspondence": True,
        },
        now=now,
    )
    store.link_artifact(w_fast, "gmail_thread:1a19fast1", role="primary", now=now)
    rd_default = store.save_reply_draft(
        w_fast, "<p>Done in Skia CL 1404137</p>", subject="Re: Skia CL 1404138", now=now
    )
    self.assertTrue(rd_default["auto_gate"])
    self.assertIn("gerrit:skia:1404138", rd_default["mentioned_keys"])
    store.record_materialized_reply_draft(
        w_fast, "r_fast_old", thread_id="1a19fast1", now=now + 1
    )
    store.record_materialized_reply_draft(
        w_fast, "r_fast_new", thread_id="1a19fast1", now=now + 2
    )
    self.assertEqual(
        ((store.get_artifact("gmail_draft:r_fast_old") or {}).get("state") or {}).get("status"),
        "DISCARDED",
    )

    w_disc = store.create_work("Discarded materialized draft", now=now)
    store.upsert_artifact(
        "gmail_thread:1a19disc1",
        kind="gmail_thread",
        title="Discard thread (Florin)",
        state={
            "thread_id": "1a19disc1",
            "root_subject": "Discard thread",
            "counterparty": "Florin Malita",
            "sent_by_me": True,
            "last_from_me": False,
            "last_message_ts": now - 200,
            "unanswered_human": True,
            "is_active_correspondence": True,
        },
        now=now,
    )
    store.link_artifact(w_disc, "gmail_thread:1a19disc1", role="primary", now=now)
    store.save_reply_draft(w_disc, "<p>Draft to discard</p>", now=now)
    store.record_materialized_reply_draft(
        w_disc, "r_disc_only", thread_id="1a19disc1", now=now + 3
    )

    b64_sent = base64.urlsafe_b64encode(b"Done in Skia CL 1404137").decode("ascii")
    b64_in = base64.urlsafe_b64encode(b"Any update?").decode("ascii")
    empty_mode = {"shape": "empty_obj"}

    def fake_fast_or_discard(
        cmd: list[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
      del args, kwargs
      res = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
      if "drafts" in cmd and "get-draft" not in cmd:
        if empty_mode["shape"] == "empty_obj":
          res.stdout = json.dumps({"resultSizeEstimate": 0})
        else:
          res.stdout = json.dumps({"drafts": None, "resultSizeEstimate": 0})
      elif "get-thread" in cmd:
        tid = cmd[cmd.index("get-thread") + 1]
        if tid == "1a19fast1":
          res.stdout = json.dumps({
              "messages": [{
                  "id": "m_fast_sent",
                  "internalDate": str(int((now + 10) * 1000)),
                  "labelIds": ["SENT"],
                  "payload": {
                      "mimeType": "text/plain",
                      "headers": [
                          {"name": "Subject", "value": "Re: Fast send thread"},
                          {"name": "From", "value": "Julia Lavrova <jlavrova@google.com>"},
                          {"name": "To", "value": "Renzo Olivares <roliv@google.com>"},
                      ],
                      "body": {"data": b64_sent},
                  },
              }]
          })
        else:
          res.stdout = json.dumps({
              "messages": [{
                  "id": "m_disc_in",
                  "internalDate": str(int((now - 200) * 1000)),
                  "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
                  "payload": {
                      "mimeType": "text/plain",
                      "headers": [
                          {"name": "Subject", "value": "Discard thread"},
                          {"name": "From", "value": "Florin Malita <fmalita@google.com>"},
                          {"name": "To", "value": "Julia Lavrova <jlavrova@google.com>"},
                      ],
                      "body": {"data": b64_in},
                  },
              }]
          })
      elif "search" in cmd:
        res.stdout = json.dumps({"messages": []})
      return res

    ws_empty: dict[str, object] = {}
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_fast_or_discard
    ):
      ingestor.ingest_gmail_drafts_and_threads(store, ws_empty, now=now + 20)
      empty_mode["shape"] = "null_drafts"
      ingestor.ingest_gmail_drafts_and_threads(store, ws_empty, now=now + 30)

    self.assertEqual(
        ((store.get_artifact("gmail_draft:r_fast_new") or {}).get("state") or {}).get("status"),
        "SENT",
    )
    self.assertIsNone(store.get_reply_draft(w_fast))
    self.assertEqual(
        ((store.get_artifact("gmail_draft:r_disc_only") or {}).get("state") or {}).get("status"),
        "DISCARDED",
    )
    surviving_rd = store.get_reply_draft(w_disc)
    self.assertIsNotNone(surviving_rd)
    assert surviving_rd is not None
    self.assertEqual(surviving_rd.get("materialized_draft_key"), "")

    # 3. mark_obligation_done on ob-wbdraft-* clears reply_draft without zombie resurrection
    wb_disc_oid = work_store._wb_draft_obligation_id(w_disc)
    store.record_materialized_reply_draft(
        w_disc, "r_disc_done", thread_id="1a19disc1", now=now + 35
    )
    store.mark_obligation_done(wb_disc_oid, now=now + 40)
    self.assertIsNone(store.get_reply_draft(w_disc))
    store.reconcile_correspondence_obligations(w_disc, now=now + 41)
    proj_done_ob = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 41)
    disc_card = [w for w in proj_done_ob["works"] if w["work_id"] == w_disc][0]
    self.assertFalse(any("Unsent" in r for r in disc_card["reasons"]))

    # 4. Unlinking sole primary CL retires auto_gate; unlinking thread clears thread_key; split_work
    w_unlk = store.create_work("Unlink & split draft contracts", now=now + 50)
    store.upsert_artifact(
        "gerrit:skia:1430001",
        kind="gerrit",
        title="1430001: Pending CL",
        state={
            "status": "NEW",
            "is_mine": True,
            "assigned_reviewers_count": 1,
            "entity_state": {"lane": "waiting", "status": "waiting"},
        },
        now=now + 50,
    )
    store.upsert_artifact(
        "gmail_thread:1a19unlk1",
        kind="gmail_thread",
        title="Unlink thread (Peer)",
        state={
            "thread_id": "1a19unlk1",
            "sent_by_me": True,
            "last_from_me": True,
            "last_message_ts": now + 45,
            "is_active_correspondence": True,
        },
        now=now + 50,
    )
    store.link_artifact(w_unlk, "gerrit:skia:1430001", role="primary", now=now + 50)
    store.link_artifact(w_unlk, "gmail_thread:1a19unlk1", role="channel", now=now + 50)
    store.save_reply_draft(w_unlk, "<p>Will reply after CL lands</p>", now=now + 51)
    new_split_wid = store.split_work(
        w_unlk, "gmail_thread:1a19unlk1", "Split thread card", now=now + 52
    )
    self.assertEqual((store.get_reply_draft(w_unlk) or {}).get("thread_key"), "")
    split_obs = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 53)
    new_split_card = [w for w in split_obs["works"] if w["work_id"] == new_split_wid][0]
    self.assertFalse(
        any(
            str(o["obligation_id"]).startswith("ob-wbdraft-")
            for o in new_split_card["obligations"]
        )
    )
    store.unlink_artifact(w_unlk, "gerrit:skia:1430001", now=now + 55)
    proj_unlk = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 56)
    unlk_card = [w for w in proj_unlk["works"] if w["work_id"] == w_unlk][0]
    self.assertEqual(unlk_card["lane"], "needs-you")
    self.assertTrue(any("Unsent reply draft" in r for r in unlk_card["reasons"]))

    # 5. Config-titled Work with active reply_draft survives purge; standalone To+Subject skips bot
    # gmail_thread and never silences unrelated gdoc_thread
    w_cfg = store.create_work("Workbench roadmap notes", project_id="config", now=now + 60)
    store.upsert_artifact(
        "gmail_thread:1a19botgerrit",
        kind="gmail_thread",
        title="Change 1430002 merged (gerrit)",
        state={"thread_id": "1a19botgerrit", "is_automated": True, "last_from_me": False},
        now=now + 60,
    )
    store.upsert_artifact(
        "gdoc_thread:doc777:disc1",
        kind="gdoc_thread",
        title="Design doc comment (Alice)",
        state={
            "doc_title": "Design doc",
            "counterparty": "Alice",
            "last_from_me": False,
            "last_message_ts": now + 58,
            "unanswered_human": True,
            "is_active_correspondence": True,
        },
        now=now + 60,
    )
    store.upsert_artifact(
        "gerrit:skia:1430002",
        kind="gerrit",
        title="1430002: Active CL",
        state={
            "status": "NEW",
            "is_mine": True,
            "assigned_reviewers_count": 1,
            "entity_state": {"lane": "waiting", "status": "waiting"},
        },
        now=now + 60,
    )
    store.link_artifact(w_cfg, "gerrit:skia:1430002", role="primary", now=now + 60)
    store.link_artifact(w_cfg, "gmail_thread:1a19botgerrit", role="channel", now=now + 60)
    store.link_artifact(w_cfg, "gdoc_thread:doc777:disc1", role="channel", now=now + 60)
    rd_standalone = store.save_reply_draft(
        w_cfg,
        "<p>Standalone email</p>",
        to="bob@google.com",
        subject="New standalone topic",
        now=now + 61,
    )
    self.assertEqual(rd_standalone["thread_key"], "")
    proj_gdoc_live = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 62)
    cfg_card = [w for w in proj_gdoc_live["works"] if w["work_id"] == w_cfg][0]
    self.assertEqual(cfg_card["lane"], "needs-you")

    w_cfg_only = store.create_work("Config-titled card with draft", project_id="config", now=now)
    store.save_reply_draft(
        w_cfg_only, "Standalone note", fmt="text", to="bob@google.com", subject="Hi", now=now + 63
    )
    store.purge_config_artifacts_and_works(now=now + 64)
    self.assertIsNotNone(store.get_reply_draft(w_cfg_only))

    # 6. >=25 unlinked scratch drafts skip get-draft on Sync 2+, and >100 surviving draft covers tid
    get_draft_calls: list[str] = []
    store.upsert_artifact(
        "gmail_draft:r_101_surv",
        kind="gmail_draft",
        state={
            "status": "DRAFT",
            "thread_id": "1a19over100",
            "draft_updated_ts": now + 70,
            "from_workbench_draft": True,
        },
        now=now + 70,
    )
    store.link_artifact(w_cfg, "gmail_draft:r_101_surv", role="channel", now=now + 70)

    def fake_100_drafts(
        cmd: list[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
      del args, kwargs
      res = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
      if "drafts" in cmd and "get-draft" not in cmd:
        res.stdout = json.dumps([
            {"id": f"r_scratch_{i:03d}", "threadId": f"t_scr_{i:03d}"} for i in range(100)
        ])
      elif "get-draft" in cmd:
        did = cmd[cmd.index("get-draft") + 1]
        get_draft_calls.append(did)
        res.stdout = json.dumps({
            "id": did,
            "message": {
                "id": f"m_{did}",
                "threadId": "1a19over100" if did == "r_101_surv" else f"t_{did}",
                "internalDate": str(int((now + 70) * 1000)),
                "payload": {"headers": [], "body": {"data": ""}},
            },
        })
      elif "search" in cmd:
        q_str = cmd[cmd.index("search") + 1]
        if "in:inbox" in q_str:
          res.stdout = json.dumps([{"id": "m_ov100", "threadId": "1a19over100"}])
        else:
          res.stdout = json.dumps([])
      elif "get-thread" in cmd:
        res.stdout = json.dumps({
            "messages": [{
                "id": "m_ov100",
                "internalDate": str(int((now + 50) * 1000)),
                "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "Subject", "value": "Over 100 draft coverage"},
                        {"name": "From", "value": "Peer <peer@google.com>"},
                        {"name": "To", "value": "Julia Lavrova <jlavrova@google.com>"},
                    ],
                    "body": {"data": b64_in},
                },
            }]
        })
      return res

    ws_100: dict[str, object] = {
        "active_draft_ids": [f"r_scratch_{i:03d}" for i in range(100)] + ["r_101_surv"]
    }
    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_100_drafts
    ):
      tray_100 = ingestor.ingest_gmail_drafts_and_threads(store, ws_100, now=now + 80)
    self.assertFalse(any(d == "r_scratch_030" for d in get_draft_calls))
    self.assertEqual([t["thread_id"] for t in tray_100], [])

    # 7. Plain-text URL escaping/punctuation + effective_upd idempotency after mat_at
    rendered_pt = ingestor._render_plain_text_draft_html(
        "See https://example.com/a?x=1&y=2, and "
        "(https://skia-review.googlesource.com/c/skia/+/1414016)."
    )
    self.assertIn(
        '<a href="https://example.com/a?x=1&amp;y=2">https://example.com/a?x=1&amp;y=2</a>,',
        rendered_pt,
    )
    self.assertNotIn("&amp;amp;", rendered_pt)
    self.assertIn(
        '(<a href="https://skia-review.googlesource.com/c/skia/+/1414016">'
        "https://skia-review.googlesource.com/c/skia/+/1414016</a>).",
        rendered_pt,
    )
    w_idemp = store.create_work("Idempotent save", now=now + 90)
    store.save_reply_draft(
        w_idemp, "Same body", fmt="text", to="a@google.com", subject="Subj", now=now + 90
    )
    store.record_materialized_reply_draft(w_idemp, "r_idemp_1", subject="Subj", now=now + 100)
    rd_same = store.save_reply_draft(
        w_idemp, "Same body", fmt="text", to="a@google.com", subject="Subj", now=now + 150
    )
    self.assertLessEqual(rd_same["updated_at"], 100.0 + now)
    # Metadata changes after materialization must advance updated_at
    rd_diff_to = store.save_reply_draft(
        w_idemp, "Same body", fmt="text", to="c@google.com", subject="Subj", now=now + 110
    )
    self.assertEqual(rd_diff_to["updated_at"], now + 110)

    # 8. Format detector, XSS/entity/C++ template preview parity, JS editor state, and workctl CLI
    self.assertEqual(
        workbench_server._detect_draft_format(
            'Hi Renzo,\n\nSee <a href="https://example.com">CL</a>'
        ),
        "html",
    )
    self.assertEqual(
        workbench_server._detect_draft_format("Fixed SkSpan<const Run> bounds"),
        "text",
    )
    prev_text = workbench_server._render_safe_draft_preview({
        "body": "Fixed SkSpan<const Run> in https://skia-review.googlesource.com/c/skia/+/1414016.",
        "format": "text",
    })
    self.assertIn("&lt;const Run&gt;", prev_text)
    self.assertIn('href="https://skia-review.googlesource.com/c/skia/+/1414016"', prev_text)
    prev_html = workbench_server._render_safe_draft_preview({
        "body": (
            '<p>R&amp;D <a href="https://example.com?a=1&amp;b=2">A &amp; B</a>'
            '<script>alert(1)</script><a href="javascript:alert(1)">bad</a></p>'
        ),
        "format": "html",
    })
    self.assertNotIn("<script>", prev_html)
    self.assertNotIn("javascript:", prev_html)
    self.assertNotIn("&amp;amp;", prev_html)
    self.assertIn("R&amp;D", prev_html)
    self.assertIn('href="https://example.com?a=1&amp;b=2"', prev_html)
    self.assertIn(">A &amp; B</a>", prev_html)

    full_ssr = workbench_server.render_workbench_html(
        store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now + 160), 1
    )
    self.assertIn("setSelectionRange", full_ssr)
    self.assertIn("const ok = await postJson", full_ssr)

    # workctl mutually_exclusive_group + draft-show --json + draft-clear
    with self.assertRaises(SystemExit):
      workctl.main(["--db", self.db_path, "draft-set", w_idemp, "--body", "x", "--html", "--text"])
    with self.assertRaises(SystemExit):
      workctl.main([
          "--db",
          self.db_path,
          "draft-set",
          w_idemp,
          "--body",
          "x",
          "--auto-gate",
          "--no-auto-gate",
      ])
    self.assertEqual(workctl.main(["--db", self.db_path, "draft-show", w_idemp, "--json"]), 0)
    with mock.patch(
        "ingestor.materialize_reply_draft",
        return_value={"draft_id": "r_cli", "draft_key": "gmail_draft:r_cli", "url": "https://x"},
    ):
      self.assertEqual(
          workctl.main(["--db", self.db_path, "draft-materialize", w_idemp, "--json"]), 0
      )
    self.assertEqual(workctl.main(["--db", self.db_path, "draft-clear", w_idemp]), 0)
    self.assertIsNone(store.get_reply_draft(w_idemp))

  # Verify domain activity/origin timestamps survive sync passes, lift virtual CLs on equal footing,
  # and drive Waiting chip filtering plus Triage/Activity/Age bidirectional sorting in the UI
  def test_r14_domain_timestamps_sync_idempotency_and_sorting_ui(self) -> None:
    import re
    import workbench_server
    import work_store

    # 2026-11-09 16:00:00 UTC
    now = 1794240000.0
    store = work_store.WorkStore(self.db_path)

    wid_idem = store.create_work(
        "SkParagraph strut cache", project_id="skia", work_id="work-idem", now=now - 3600
    )
    store.upsert_artifact(
        "gerrit:skia:1414001",
        kind="gerrit",
        title="1414001: strut cache",
        state={"created_ts": now - 86400, "activity_ts": now - 3600},
        now=now - 3600,
    )
    store.link_artifact(
        wid_idem, "gerrit:skia:1414001", role="primary", link_reason="chat_topic", now=now - 3600
    )
    with store._connect() as conn:
      upd_before = float(
          conn.execute(
              "SELECT updated_at FROM works WHERE work_id = ?", (wid_idem,)
          ).fetchone()["updated_at"]
      )
      ev_before = int(
          conn.execute(
              "SELECT COUNT(*) AS c FROM events WHERE work_id = ?", (wid_idem,)
          ).fetchone()["c"]
      )

    store.create_work(
        "SkParagraph strut cache", project_id="skia", work_id="work-idem", now=now
    )
    store.link_artifact(
        wid_idem, "gerrit:skia:1414001", role="primary", link_reason="chat_topic", now=now
    )
    with store._connect() as conn:
      upd_after = float(
          conn.execute(
              "SELECT updated_at FROM works WHERE work_id = ?", (wid_idem,)
          ).fetchone()["updated_at"]
      )
      ev_after = int(
          conn.execute(
              "SELECT COUNT(*) AS c FROM events WHERE work_id = ?", (wid_idem,)
          ).fetchone()["c"]
      )
    self.assertEqual(upd_after, upd_before)
    self.assertEqual(ev_after, ev_before)

    # C4: Piper local ET slash timestamps and non-finite float guards
    self.assertEqual(work_store._parse_domain_ts("2026/11/09 11:00:00"), now)
    self.assertEqual(work_store._parse_domain_ts(float("inf")), 0.0)
    self.assertEqual(work_store._parse_domain_ts("inf"), 0.0)
    self.assertEqual(work_store._parse_domain_ts(float("nan")), 0.0)
    self.assertEqual(work_store.format_et_timestamp(float("inf"), now=now), "")

    # C5: create_work on existing work_id preserves snooze_until, done status, and work_minted count
    wid_lifecycle = store.create_work("Lifecycle work", work_id="work-lifecycle", now=now - 7200)
    with store._connect() as conn:
      conn.execute(
          "UPDATE works SET snooze_until = ? WHERE work_id = ?",
          (now + 3600, wid_lifecycle),
      )
    store.create_work("Lifecycle work v2", work_id="work-lifecycle", meta={"k": 1}, now=now - 3600)
    store.complete_work(wid_lifecycle, now=now - 1800)
    store.create_work("Lifecycle work v2", work_id="work-lifecycle", meta={"k": 1}, now=now)
    with store._connect() as conn:
      lc_row = conn.execute(
          "SELECT status, snooze_until FROM works WHERE work_id = ?", (wid_lifecycle,)
      ).fetchone()
      minted_cnt = int(
          conn.execute(
              "SELECT COUNT(*) AS c FROM events WHERE work_id = ? AND event_type = 'work_minted'",
              (wid_lifecycle,),
          ).fetchone()["c"]
      )
    self.assertEqual(lc_row["status"], "done")
    self.assertEqual(float(lc_row["snooze_until"]), now + 3600)
    self.assertEqual(minted_cnt, 1)

    # C3: manual links survive auto syncs and competing auto reasons do not ping-pong updated_at
    wid_manual = store.create_work("Manual bug + CL", work_id="work-manual", now=now - 5000)
    store.upsert_artifact(
        "buganizer:580955594",
        kind="buganizer",
        state={"status": "ASSIGNED", "origin_ts": now - 90000, "modified_ts": now - 5000},
        now=now - 5000,
    )
    store.upsert_artifact(
        "gerrit:skia:1414002",
        kind="gerrit",
        state={
            "status": "NEW",
            "linked_bug_keys": ["buganizer:580955594"],
            "created_ts": now - 4000,
            "activity_ts": now - 4000,
        },
        now=now - 5000,
    )
    store.link_artifact(
        wid_manual, "buganizer:580955594", role="primary", source="manual", now=now - 5000
    )
    store.link_artifact(
        wid_manual,
        "gerrit:skia:1414002",
        role="primary",
        source="manual",
        link_reason="manual",
        link_detail="Pinned manually",
        now=now - 5000,
    )
    store.reconcile_work_bug_cls(wid_manual, now=now - 1000)
    store.link_artifact(
        wid_manual,
        "gerrit:skia:1414002",
        role="primary",
        source="auto",
        link_reason="bug_trailer",
        link_detail="В описании СЛ 1414002 указан баг b/580955594",
        now=now - 500,
    )
    with store._connect() as conn:
      man_link = conn.execute(
          "SELECT source, link_reason FROM work_links WHERE work_id = ? AND artifact_key = ?",
          (wid_manual, "gerrit:skia:1414002"),
      ).fetchone()
      man_upd = float(
          conn.execute(
              "SELECT updated_at FROM works WHERE work_id = ?", (wid_manual,)
          ).fetchone()["updated_at"]
      )
    self.assertEqual(man_link["source"], "manual")
    self.assertEqual(man_upd, now - 5000)

    store.link_artifact(
        wid_idem,
        "gerrit:skia:1414001",
        role="primary",
        source="auto",
        link_reason="bug_trailer",
        link_detail="В описании СЛ 1414001 указан баг b/1",
        now=now,
    )
    with store._connect() as conn:
      upd_competing = float(
          conn.execute(
              "SELECT updated_at FROM works WHERE work_id = ?", (wid_idem,)
          ).fetchone()["updated_at"]
      )
    self.assertEqual(upd_competing, upd_before)

    # C2: materializing a draft or auditing a stub CL today must not reset a 10d Work's origin_at
    wid_stalled = store.create_work(
        "Stalled upstream issue follow-up", work_id="work-stalled", now=now - 10 * 86400
    )
    store.upsert_artifact(
        "piper:1008075988",
        kind="piper",
        title="1008075988: Piper audit stub",
        state={"status": "PENDING", "audited_msg_date": "2026-11-09T15:59:00Z"},
        now=now,
    )
    store.link_artifact(wid_stalled, "piper:1008075988", role="primary", now=now - 10 * 86400)
    store.save_reply_draft(
        wid_stalled,
        "Checking in",
        to="roliv@google.com",
        subject="Status update",
        now=now,
    )
    store.record_materialized_reply_draft(wid_stalled, "r_mat_age", now=now)

    wid_reb = store.create_work("Rebased feature work", work_id="work-rebased", now=now)
    store.upsert_artifact(
        "github_issue:flutter/flutter:50168",
        kind="github_issue",
        title="flutter#50168 Old 2020 bug",
        state={"status": "OPEN", "origin_ts": 1580000000.0, "created_time": "2020-01-26T00:00:00Z"},
        now=now,
    )
    store.upsert_artifact(
        "gerrit:skia:1414100",
        kind="gerrit",
        title="1414100: Main fix",
        state={
            "status": "NEW",
            "is_self": True,
            "role": "author",
            "merge_conflict": True,
            "created_ts": now - 3 * 86400,
            "activity_ts": now - 6 * 3600,
            "audited_msg_date": "2026-11-09T15:59:00Z",
        },
        now=now,
    )
    store.upsert_artifact(
        "gerrit:skia:1414101",
        kind="gerrit",
        title="1414101: [WIP] Companion tests for 1414100",
        state={
            "status": "NEW",
            "is_self": True,
            "is_wip": True,
            "is_companion": True,
            "companion_to": "gerrit:skia:1414100",
            "created_ts": now - 5 * 3600,
            "activity_ts": now - 5 * 3600,
        },
        now=now,
    )
    store.upsert_artifact(
        "gerrit:skia:1414999",
        kind="gerrit",
        title="1414999: Passing mention",
        state={"status": "NEW", "created_ts": now - 60, "activity_ts": now - 60},
        now=now,
    )
    store.link_artifact(wid_reb, "github_issue:flutter/flutter:50168", role="primary", now=now)
    store.link_artifact(wid_reb, "gerrit:skia:1414100", role="primary", now=now)
    store.link_artifact(
        wid_reb, "gerrit:skia:1414101", role="primary", link_reason="companion_wip", now=now
    )
    store.link_artifact(
        wid_reb, "gerrit:skia:1414999", role="context_only", link_reason="chat_mention", now=now
    )

    wid_urgent = store.create_work("Ready obligation work", work_id="work-urgent", now=now)
    store.upsert_artifact(
        "gmail_thread:th_urgent",
        kind="gmail_thread",
        title="Question from Renzo",
        state={
            "origin_ts": now - 5 * 86400,
            "last_message_ts": now - 12 * 3600,
            "is_automated": False,
        },
        now=now,
    )
    store.link_artifact(wid_urgent, "gmail_thread:th_urgent", role="channel", now=now)
    store.add_obligation(
        wid_urgent,
        description="Reply to Renzo",
        trigger_expr="always",
        target_artifact_key="gmail_thread:th_urgent",
        now=now - 12 * 3600,
    )

    cl_state = {
        "items": {
            "gerrit:1414555": {
                "kind": "gerrit",
                "target_id": "1414555",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "is_self": False,
                "role": "reviewer",
                "in_my_attention": True,
                "subject": "Fresh incoming review CL",
                "created": "2026-11-08 16:00:00.000000000",
                "last_msg_date": "2026-11-09 15:55:00.000000000",
                "updated_at": "10:55:00 ET",
                "entity_state": {"lane": "you", "action_required": True, "reason": "Review needed"},
            },
            "gerrit:1420999": {
                "kind": "gerrit",
                "target_id": "1420999",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "is_self": False,
                "role": "reviewer",
                "in_my_attention": True,
                "subject": "Dateless virtual CL",
                "entity_state": {"lane": "you", "action_required": True, "reason": "Review needed"},
            },
        }
    }
    proj = store.build_dashboard_projection(cl_watcher_state=cl_state, now=now)
    by_id = {w["work_id"]: w for w in proj["works"]}

    stalled_card = by_id["work-stalled"]
    self.assertEqual(stalled_card["origin_at"], now - 10 * 86400)
    self.assertEqual(stalled_card["activity_at"], now)

    reb_card = by_id["work-rebased"]
    self.assertEqual(reb_card["origin_at"], now - 3 * 86400)
    self.assertEqual(reb_card["activity_at"], now - 5 * 3600)

    virt_card = by_id["virt-gerrit:skia:1414555"]
    self.assertEqual(virt_card["activity_at"], now - 300)
    self.assertEqual(virt_card["origin_at"], now - 86400)

    dateless_card = by_id["virt-gerrit:skia:1420999"]
    self.assertEqual(dateless_card["activity_at"], 0.0)
    self.assertEqual(dateless_card["origin_at"], 0.0)
    self.assertEqual(dateless_card["activity_et"], "")
    self.assertEqual(dateless_card["origin_et"], "")

    needs_ids = [w["work_id"] for w in proj["works"] if w["lane"] == "needs-you"]
    self.assertEqual(
        needs_ids,
        [
            "work-urgent",
            "virt-gerrit:skia:1414555",
            "work-rebased",
            "virt-gerrit:skia:1420999",
        ],
    )

    ui_proj = {
        "synced_et": "11:00 ET (just now)",
        "counts": {"needs_you": 2, "waiting": 2, "obligations_open": 1, "unanswered": 0},
        "metrics": {"link_precision": 1.0},
        "unanswered_tray": [],
        "works": [
            {
                "work_id": "w_urgent",
                "title": "Urgent Ready Work",
                "lane": "needs-you",
                "has_urgent_obligation": True,
                "activity_at": 1000,
                "activity_et": "Nov 5 at 10:00 ET (4d ago)",
                "origin_at": 500,
                "origin_et": "Nov 1 at 09:00 ET (8d ago)",
                "updated_at": 1000,
                "updated_et": "Nov 5 at 10:00 ET (4d ago)",
                "is_multi_channel": True,
                "obligations": [
                    {"status": "ready", "description": "Reply", "trigger_expr": "always"}
                ],
                "artifacts": [],
                "checklist": [],
                "reasons": ["Ready"],
            },
            {
                "work_id": "w_fresh_wait",
                "title": "Fresh Waiting CL",
                "lane": "waiting",
                "has_urgent_obligation": False,
                "activity_at": 5000,
                "activity_et": "10:58 ET (2m ago)",
                "origin_at": 4900,
                "origin_et": "10:55 ET (5m ago)",
                "updated_at": 5000,
                "updated_et": "10:58 ET (2m ago)",
                "is_multi_channel": False,
                "obligations": [],
                "artifacts": [],
                "checklist": [],
                "reasons": ["Waiting on reviewer"],
            },
            {
                "work_id": "w_old_needs",
                "title": "Long Open Rebased Work",
                "lane": "needs-you",
                "has_urgent_obligation": False,
                "activity_at": 4000,
                "activity_et": "10:30 ET (30m ago)",
                "origin_at": 100,
                "origin_et": "Oct 20 at 12:00 ET (20d ago)",
                "updated_at": 4000,
                "updated_et": "10:30 ET (30m ago)",
                "is_multi_channel": False,
                "obligations": [],
                "artifacts": [],
                "checklist": [],
                "reasons": ["Merge conflict"],
            },
            {
                "work_id": "w_zero_ts",
                "title": "Unknown Timestamp Waiting",
                "lane": "waiting",
                "has_urgent_obligation": False,
                "activity_at": 0,
                "activity_et": "",
                "origin_at": 0,
                "origin_et": "",
                "updated_at": 0,
                "updated_et": "",
                "is_multi_channel": False,
                "obligations": [],
                "artifacts": [],
                "checklist": [],
                "reasons": [],
            },
        ],
    }
    html_out = workbench_server.render_workbench_html(ui_proj, ui_version=2)
    self.assertIn('data-filter="waiting"', html_out)
    self.assertIn('id="wb-sort-mode"', html_out)
    self.assertIn('id="wb-sort-dir"', html_out)
    match = re.search(r"<script>\n(.*?)\n</script>", html_out, re.DOTALL)
    self.assertIsNotNone(match)
    script_js = match.group(1)

    node_sort_test = """
const assert = require('assert');
const storage = new Map();
function makeEl(id, extra = {}) {
  const attrs = Object.assign({}, extra.attrs || {});
  const listeners = {};
  return Object.assign({
    id,
    value: extra.value || '',
    textContent: extra.textContent || '',
    innerHTML: '',
    disabled: false,
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
    setAttribute(k, v) { attrs[k] = String(v); },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    dispatch(ev, evtObj) { for (const fn of (listeners[ev] || [])) fn(evtObj || {}); },
  }, extra.props || {});
}
const els = {
  'wb-synced': makeEl('wb-synced'),
  'kpi-needs': makeEl('kpi-needs'),
  'kpi-wait': makeEl('kpi-wait'),
  'kpi-obs': makeEl('kpi-obs'),
  'kpi-tray': makeEl('kpi-tray'),
  'kpi-prec': makeEl('kpi-prec'),
  'wb-tray-container': makeEl('wb-tray-container'),
  'wb-cards-container': makeEl('wb-cards-container'),
  'wb-new-btn': makeEl('wb-new-btn'),
  'wb-cancel-new': makeEl('wb-cancel-new'),
  'wb-new-form': makeEl('wb-new-form', { props: { reset() {} } }),
  'wb-sync-btn': makeEl('wb-sync-btn'),
  'wb-toggle-collapse': makeEl('wb-toggle-collapse', { attrs: { 'data-action': 'collapse' } }),
  'wb-search': makeEl('wb-search'),
  'wb-sort-mode': makeEl('wb-sort-mode', { value: 'triage' }),
  'wb-sort-dir': makeEl('wb-sort-dir', { attrs: { 'data-dir': 'desc' }, textContent: '↓ New' }),
};
const chips = ['all', 'needs-you', 'waiting', 'multi', 'obligations', 'done'].map((f, idx) =>
  makeEl('chip-' + f, { attrs: { 'data-filter': f, 'data-active': idx === 0 ? 'true' : 'false' } })
);
global.localStorage = {
  getItem: (k) => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => { storage.set(k, String(v)); },
};
global.window = { addEventListener: () => {}, getSelection: () => '', prompt: () => '' };
global.setInterval = () => 0;
global.fetch = async () => ({ ok: false });
global.document = {
  getElementById: (id) => els[id] || makeEl(id),
  querySelectorAll: (sel) => (sel === '.wb-chip' ? chips : []),
  createElement: () => {
    let txt = '';
    return {
      set textContent(v) { txt = String(v || ''); },
      get innerHTML() {
        return txt.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      },
    };
  },
  body: { addEventListener: () => {} },
};

""" + script_js + """

function visibleIds() {
  const re = /data-work-id="([^"]+)"/g;
  const out = [];
  let m;
  while ((m = re.exec(els['wb-cards-container'].innerHTML)) !== null) out.push(m[1]);
  return out;
}

assert.deepStrictEqual(visibleIds(), ['w_urgent', 'w_old_needs', 'w_fresh_wait', 'w_zero_ts']);

chips[2].dispatch('click');
assert.deepStrictEqual(visibleIds(), ['w_fresh_wait', 'w_zero_ts']);

chips[0].dispatch('click');
els['wb-sort-mode'].value = 'activity';
els['wb-sort-mode'].dispatch('change', { target: els['wb-sort-mode'] });
assert.strictEqual(els['wb-sort-dir'].getAttribute('data-dir'), 'desc');
assert.strictEqual(els['wb-sort-dir'].textContent, '↓ New');
assert.deepStrictEqual(visibleIds(), ['w_fresh_wait', 'w_old_needs', 'w_urgent', 'w_zero_ts']);

els['wb-sort-dir'].dispatch('click');
assert.strictEqual(els['wb-sort-dir'].getAttribute('data-dir'), 'asc');
assert.strictEqual(els['wb-sort-dir'].textContent, '↑ Old');
assert.deepStrictEqual(visibleIds(), ['w_urgent', 'w_old_needs', 'w_fresh_wait', 'w_zero_ts']);

els['wb-sort-mode'].value = 'age';
els['wb-sort-mode'].dispatch('change', { target: els['wb-sort-mode'] });
assert.strictEqual(els['wb-sort-dir'].getAttribute('data-dir'), 'asc');
assert.deepStrictEqual(visibleIds(), ['w_old_needs', 'w_urgent', 'w_fresh_wait', 'w_zero_ts']);
assert(els['wb-cards-container'].innerHTML.includes('Opened Oct 20 at 12:00 ET'));
assert.deepStrictEqual(JSON.parse(storage.get('wb_sort_v1')), { mode: 'age', dir: 'asc' });
"""
    res = subprocess.run(
        ["node", "-e", node_sort_test], capture_output=True, text=True, check=False
    )
    self.assertEqual(
        res.returncode,
        0,
        f"Node sort check failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )

  def test_r15_gh_issue_canonicalization_my_bugs_clean_review_and_multichannel(self) -> None:
    # Verify gh_issue canonicalization, fresh My Bugs projection, clean-review promotion,
    # correspondence inbound vs silence-ping reasons, and true multi-channel detection
    import ingestor
    import work_store

    now = 1_762_700_000.0
    store = work_store.WorkStore(self.db_path)

    # Seed a legacy bogus buganizer:<gh_id> row to verify sync/purge migration cleans it up
    store.upsert_artifact(
        "buganizer:196106",
        kind="buganizer",
        title="b/196106: Legacy mis-keyed GitHub issue",
        url="https://github.com/flutter/flutter/issues/196106",
        state={"assignee": "Rusino", "status": "OPEN"},
        now=now - 500,
    )
    # Seed a legacy non-flutter GitHub row whose url fell back to chromium and has an obligation
    store.upsert_artifact(
        "buganizer:43",
        kind="buganizer",
        title="b/43: Legacy non-flutter GitHub issue",
        url="https://issues.chromium.org/issues/43",
        state={
            "bug_key": "gh_issue:Rusino/paragraph#43",
            "source": "github",
            "title": "Paragraph wrap bug",
            "assignee": "Rusino",
            "status": "CLOSED",
        },
        now=now - 500,
    )
    store.upsert_artifact(
        "github_issue:Rusino/paragraph:43",
        kind="github_issue",
        title="Rusino/paragraph#43",
        url="",
        state={},
        now=now - 600,
    )
    w_legacy_mig = store.create_work("Legacy GH issue obligation migration", now=now - 500)
    store.link_artifact(w_legacy_mig, "buganizer:43", role="primary", source="manual", now=now)
    mig_ob_id = store.add_obligation(
        w_legacy_mig,
        "Close once paragraph#43 closes",
        trigger_expr="bug_closed(buganizer:43)",
        target_artifact_key="buganizer:43",
        now=now - 450,
    )

    self.assertEqual(
        work_store.canonicalize_artifact_key("gh_issue", "Rusino/paragraph#42"),
        "github_issue:Rusino/paragraph:42",
    )
    self.assertEqual(
        work_store.canonicalize_artifact_key("buganizer", "b/384910293"),
        "buganizer:384910293",
    )
    self.assertEqual(
        work_store.canonicalize_watcher_bug("b/384910293", {"title": "T"})[0],
        "buganizer:384910293",
    )
    self.assertEqual(
        work_store._default_artifact_url("github_issue:Rusino/paragraph:42"),
        "https://github.com/Rusino/paragraph/issues/42",
    )

    cl_state = {
        "bugs": {
            "gh_issue:flutter/flutter#196106": {
                "bug_key": "gh_issue:flutter/flutter#196106",
                "source": "github",
                "tracker_flavor": "github",
                "id": "196106",
                "url": "https://github.com/flutter/flutter/issues/196106",
                "title": "Arabic ligature cut across spans",
                "status": "OPEN",
                "assignee": "mdebbar, Rusino",
                "is_stale_backlog": False,
                "modified_ts": now - 120,
            },
            "gh_issue:flutter/flutter#193731": {
                "bug_key": "gh_issue:flutter/flutter#193731",
                "source": "github",
                "id": "193731",
                "url": "https://github.com/flutter/flutter/issues/193731",
                "title": "WebParagraph line metrics diverge",
                "status": "OPEN",
                "assignee": "Rusino",
                "lane": "assigned",
                "is_stale_backlog": False,
                "modified_ts": now - 200,
            },
            "gh_issue:Rusino/paragraph#42": {
                "bug_key": "gh_issue:Rusino/paragraph#42",
                "source": "github",
                "id": "42",
                "url": "https://github.com/Rusino/paragraph/issues/42",
                "title": "TS paragraph line height",
                "status": "OPEN",
                "assignee": "Rusino",
                "lane": "has_cl",
                "is_stale_backlog": False,
                "modified_ts": now - 150,
            },
            "buganizer:580955594": {
                "bug_key": "buganizer:580955594",
                "source": "buganizer",
                "id": "580955594",
                "title": "Crash in SkScalerContextProxy",
                "status": "ASSIGNED",
                "assignee": "jlavrova@google.com",
                "lane": "you",
                "needs_you": True,
                "why_needs_you": "Blunderbuss SLO ping",
                "is_stale_backlog": False,
                "modified_ts": now - 60,
            },
            "buganizer:40000001": {
                "bug_key": "buganizer:40000001",
                "source": "buganizer",
                "id": "40000001",
                "title": "Ancient stale backlog ticket",
                "status": "ASSIGNED",
                "assignee": "jlavrova@google.com",
                "lane": "assigned",
                "is_stale_backlog": True,
                "modified_ts": now - 86400 * 400,
            },
            "buganizer:520529526": {
                "bug_key": "buganizer:520529526",
                "source": "buganizer",
                "id": "520529526",
                "title": "Shared SkSpan static extent bug",
                "status": "ASSIGNED",
                "assignee": "fmalita@google.com",
                "lane": "you",
                "needs_you": True,
                "why_needs_you": "Your fix CL merged on shared bug — add Collaborator?",
                "is_stale_backlog": False,
                "modified_ts": now - 90,
            },
        },
        "items": {
            "github:195999": {
                "kind": "github_pr",
                "target_id": "flutter/flutter:195999",
                "subject": "Fix Arabic ligature shaping",
                "url": "https://github.com/flutter/flutter/pull/195999",
                "status": "OPEN",
                "role": "author",
                "is_self": True,
                "linked_bugs": [
                    {
                        "bug_key": "flutter#196106",
                        "title": "Arabic ligature cut across spans",
                        "status": "OPEN",
                    }
                ],
                "entity_state": {"lane": "waiting", "status": "waiting"},
                "updated_ts": now - 100,
            },
            "gerrit:1420111": {
                "kind": "gerrit",
                "host": "skia",
                "target_id": "1420111",
                "patchset": 2,
                "subject": "[SkoreText] Fix TS paragraph line height",
                "status": "NEW",
                "role": "author",
                "is_self": True,
                "linked_bugs": [
                    {
                        "bug_key": "Rusino/paragraph#42",
                        "title": "TS paragraph line height",
                        "status": "OPEN",
                    }
                ],
                "entity_state": {"lane": "waiting", "status": "waiting"},
                "updated_ts": now - 95,
            },
            "gerrit:7686597": {
                "kind": "gerrit",
                "host": "chromium",
                "target_id": "7686597",
                "patchset": 4,
                "subject": "skia: add opt-in Linux text correction",
                "status": "NEW",
                "role": "reviewer (ext)",
                "merge_conflict": False,
                "attention_reason": "WAITING_ON_CQ_DRY_RUN",
                "entity_state": {
                    "lane": "waiting",
                    "status": "waiting",
                    "reason": "Waiting on CQ dry run",
                    "is_reviewer_change": True,
                    "can_finalize_clean_review": True,
                    "is_armed_finalize": False,
                },
                "updated_ts": now - 80,
            },
            "gerrit:7686999": {
                "kind": "gerrit",
                "host": "chromium",
                "target_id": "7686999",
                "patchset": 1,
                "subject": "[Companion WIP] Probe for crrev/c/7686597",
                "status": "NEW",
                "is_companion": True,
                "companion_to": "gerrit:chromium:7686597",
                "updated_ts": now - 75,
            },
            "gerrit:1409697": {
                "kind": "gerrit",
                "host": "skia",
                "target_id": "1409697",
                "patchset": 6,
                "subject": "Conflicted external clean review",
                "status": "NEW",
                "role": "reviewer (ext)",
                "merge_conflict": True,
                "entity_state": {
                    "lane": "waiting",
                    "status": "waiting",
                    "is_reviewer_change": True,
                    "can_finalize_clean_review": True,
                    "is_armed_finalize": False,
                    "spine_short_state": {"short": "Conflict"},
                },
                "updated_ts": now - 70,
            },
            "gerrit:1409888": {
                "kind": "gerrit",
                "host": "skia",
                "target_id": "1409888",
                "patchset": 1,
                "subject": "Watched foreign CL on CC",
                "status": "NEW",
                "role": "cc",
                "is_self": False,
                "is_watching": True,
                "entity_state": {
                    "lane": "waiting",
                    "status": "watching",
                    "is_reviewer_change": False,
                },
                "updated_ts": now - 65,
            },
        },
    }

    ingestor.ingest_cl_watcher_state(store, cl_state, now=now)
    store.purge_config_artifacts_and_works(now=now)
    store.evaluate_all_obligations(now=now)

    self.assertIsNone(store.get_artifact("buganizer:196106"))
    gh_art = store.get_artifact("github_issue:flutter/flutter:196106")
    self.assertIsNotNone(gh_art)
    assert gh_art is not None
    self.assertIn("Rusino", str((gh_art.get("state") or {}).get("assignee") or ""))

    self.assertIsNone(store.get_artifact("buganizer:43"))
    gh43 = store.get_artifact("github_issue:Rusino/paragraph:43")
    self.assertIsNotNone(gh43)
    assert gh43 is not None
    self.assertEqual(gh43["url"], "https://github.com/Rusino/paragraph/issues/43")
    self.assertEqual(gh43["title"], "Rusino/paragraph#43: Paragraph wrap bug")

    # Standalone inbound email vs 7d silence-ping email should both stay Single item (not multi)
    w_inbound = store.create_work("Inbound email ask", now=now - 300)
    store.upsert_artifact(
        "gmail_thread:th_in",
        kind="gmail_thread",
        title="Font fallback question",
        state={
            "is_active_correspondence": True,
            "last_from_me": False,
            "unanswered_human": True,
            "last_message_ts": now - 300,
        },
        now=now,
    )
    store.link_artifact(w_inbound, "gmail_thread:th_in", role="primary", source="manual", now=now)
    store.reconcile_correspondence_obligations(w_inbound, now=now)

    # Outbound MCP comment reply >7d ago with stale last_from_me=False must wake as silence_ping
    w_silence = store.create_work("Outbound unanswered ping", now=now - 10 * 86400)
    store.upsert_artifact(
        "gmail_thread:th_sil",
        kind="gmail_thread",
        title="Roll blocker follow-up",
        state={
            "is_active_correspondence": True,
            "last_from_me": False,
            "unanswered_human": False,
            "last_message_ts": now - 9 * 86400,
            "mcp_reply_ts": now - 8 * 86400,
        },
        now=now,
    )
    store.link_artifact(w_silence, "gmail_thread:th_sil", role="primary", source="manual", now=now)
    store.reconcile_correspondence_obligations(w_silence, now=now)

    # Parked auto-gated reply draft on a conflicted own CL must not leak replies into needs_you_types
    w_parked_conflict = store.create_work("Conflicted own CL with parked reply", now=now - 500)
    store.upsert_artifact(
        "gerrit:skia:1418001",
        kind="gerrit",
        title="1418001: Fix glyph bounds",
        state={
            "status": "NEW",
            "is_self": True,
            "role": "author",
            "merge_conflict": True,
            "updated_ts": now - 50,
        },
        now=now,
    )
    store.upsert_artifact(
        "gmail_thread:th_parked",
        kind="gmail_thread",
        title="Reporter ping",
        state={
            "is_active_correspondence": True,
            "last_from_me": False,
            "unanswered_human": True,
            "last_message_ts": now - 400,
        },
        now=now,
    )
    store.link_artifact(
        w_parked_conflict, "gerrit:skia:1418001", role="primary", source="manual", now=now
    )
    store.link_artifact(
        w_parked_conflict, "gmail_thread:th_parked", role="channel", source="manual", now=now
    )
    store.save_reply_draft(
        w_parked_conflict,
        "Will reply once Gerrit 1418001 merges",
        fmt="text",
        thread_key="gmail_thread:th_parked",
        auto_gate=True,
        now=now - 100,
    )

    # Abandoned CL with stale cached entity_state must transition cleanly to done, not needs-you
    w_abandoned_cl = store.create_work("Abandoned CL with stale est", now=now - 400)
    store.upsert_artifact(
        "gerrit:skia:1399999",
        kind="gerrit",
        title="1399999: Old experiment",
        state={
            "status": "ABANDONED",
            "is_self": True,
            "role": "author",
            "merge_conflict": True,
            "entity_state": {"lane": "you", "action_required": True},
        },
        now=now,
    )
    store.link_artifact(
        w_abandoned_cl, "gerrit:skia:1399999", role="primary", source="manual", now=now
    )

    # Companion alert on an unhydrated stub parent must populate both work_types and needs_you_types
    w_comp_stub = store.create_work("Companion alert on stub parent", now=now - 350)
    store.upsert_artifact("gerrit:skia:888", kind="gerrit", title="888", state={}, now=now)
    store.upsert_artifact(
        "gerrit:skia:889",
        kind="gerrit",
        title="889: [Companion WIP] Probe for 888",
        state={
            "status": "NEW",
            "is_companion": True,
            "companion_to": "gerrit:skia:888",
            "open_external_threads": 1,
        },
        now=now,
    )
    store.link_artifact(w_comp_stub, "gerrit:skia:888", role="primary", source="manual", now=now)
    store.link_artifact(w_comp_stub, "gerrit:skia:889", role="primary", source="manual", now=now)

    # Shared foreign bug linked to a Work should surface why_needs_you but NOT count as my-bugs
    w_foreign_bug = store.create_work("Shared bug follow-up", now=now - 200)
    store.link_artifact(
        w_foreign_bug, "buganizer:520529526", role="primary", source="manual", now=now
    )

    proj = store.build_dashboard_projection(cl_watcher_state=cl_state, now=now)
    by_id = {c["work_id"]: c for c in proj["works"]}

    mig_obs = [o for o in by_id[w_legacy_mig]["obligations"] if o["obligation_id"] == mig_ob_id]
    self.assertEqual(len(mig_obs), 1)
    self.assertEqual(mig_obs[0]["trigger_expr"], "bug_closed(github_issue:Rusino/paragraph:43)")
    self.assertEqual(mig_obs[0]["status"], "ready")

    # Bound GitHub issue #196106 stays on its PR Work (not duplicated as virt-github_issue)
    self.assertNotIn("virt-github_issue:flutter/flutter:196106", by_id)
    pr_cards = [
        c for c in proj["works"]
        if any(a["artifact_key"] == "github_pr:flutter/flutter:195999" for a in c["artifacts"])
    ]
    self.assertEqual(len(pr_cards), 1)
    self.assertIn("my-bugs", pr_cards[0]["work_types"])
    self.assertIn("my-cls", pr_cards[0]["work_types"])
    self.assertTrue(pr_cards[0]["is_multi_channel"])

    # Unbound [SkoreText] hub CL retains its linked assigned bug in My Bugs without duplicating
    self.assertNotIn("virt-github_issue:Rusino/paragraph:42", by_id)
    hub_virt = by_id["virt-gerrit:skia:1420111"]
    self.assertIn("my-bugs", hub_virt["work_types"])
    self.assertIn("my-cls", hub_virt["work_types"])

    # Fresh unbound bugs (#193731 waiting, b/580955594 needs-you) project; stale backlog is skipped
    self.assertIn("virt-github_issue:flutter/flutter:193731", by_id)
    self.assertEqual(by_id["virt-github_issue:flutter/flutter:193731"]["lane"], "waiting")
    self.assertIn("my-bugs", by_id["virt-github_issue:flutter/flutter:193731"]["work_types"])
    self.assertIn("virt-buganizer:580955594", by_id)
    self.assertEqual(by_id["virt-buganizer:580955594"]["lane"], "needs-you")
    self.assertIn("Blunderbuss SLO ping", " ".join(by_id["virt-buganizer:580955594"]["reasons"]))
    self.assertNotIn("virt-buganizer:40000001", by_id)

    # Foreign bug card shows why_needs_you but never matches my-bugs
    fb_card = by_id[w_foreign_bug]
    self.assertEqual(fb_card["lane"], "needs-you")
    self.assertIn("add Collaborator", " ".join(fb_card["reasons"]))
    self.assertNotIn("my-bugs", fb_card["work_types"])

    # Clean non-conflicted review promotes to needs-you + reviews even with attention_reason set
    rev_card = by_id["virt-gerrit:chromium:7686597"]
    self.assertEqual(rev_card["lane"], "needs-you")
    self.assertIn("Ready to CR+1", " ".join(rev_card["reasons"]))
    self.assertIn("reviews", rev_card["needs_you_types"])
    self.assertFalse(rev_card["is_multi_channel"])

    # Watched/CC foreign CL must not pollute Reviews; abandoned CL must be done
    self.assertNotIn("reviews", by_id["virt-gerrit:skia:1409888"]["work_types"])
    self.assertEqual(by_id[w_abandoned_cl]["lane"], "done")

    # Parked draft on conflicted own CL keeps replies in work_types but out of needs_you_types
    pk_card = by_id[w_parked_conflict]
    self.assertEqual(pk_card["lane"], "needs-you")
    self.assertIn("replies", pk_card["work_types"])
    self.assertIn("my-cls", pk_card["needs_you_types"])
    self.assertNotIn("replies", pk_card["needs_you_types"])

    # Companion alert on stub parent maintains needs_you_types <= work_types
    cs_card = by_id[w_comp_stub]
    self.assertEqual(cs_card["lane"], "needs-you")
    self.assertIn("reviews", cs_card["needs_you_types"])
    self.assertIn("reviews", cs_card["work_types"])

    # Merge-conflicted external review stays in waiting rather than demanding CQ Dry Run
    conf_rev = by_id["virt-gerrit:skia:1409697"]
    self.assertEqual(conf_rev["lane"], "waiting")
    self.assertIn("reviews", conf_rev["work_types"])

    # Standalone correspondence Works are Single item and distinguish inbound vs 7d silence ping
    in_card = by_id[w_inbound]
    sil_card = by_id[w_silence]
    self.assertFalse(in_card["is_multi_channel"])
    self.assertFalse(sil_card["is_multi_channel"])
    self.assertIn("replies", in_card["needs_you_types"])
    self.assertIn("replies", sil_card["needs_you_types"])
    self.assertTrue(any("Reply needed" in r for r in in_card["reasons"]))
    self.assertTrue(any("silence ping" in r for r in sil_card["reasons"]))
    self.assertEqual(in_card["obligations"][0]["correspondence_mode"], "inbound_reply")
    self.assertEqual(sil_card["obligations"][0]["correspondence_mode"], "silence_ping")

  def test_r15_two_dimensional_lane_type_filter_chips_and_live_counts(self) -> None:
    # Verify 2D (Lane x Type) filter chips replace Multi-channel/Obligations and keep live (N)
    # counts strictly equal to the number of rendered cards across every combination and search
    import html as html_lib
    import json
    import re
    import workbench_server

    ui_proj = {
        "synced_et": "Nov 09 16:00 ET",
        "counts": {"needs_you": 3, "waiting": 2, "done": 1, "obligations_open": 2, "unanswered": 0},
        "metrics": {"link_precision": 1.0},
        "unanswered_tray": [],
        "works": [
            {
                "work_id": "w_mixed_reply",
                "title": "Email needing reply on LGTMd review",
                "lane": "needs-you",
                "work_types": ["replies", "reviews"],
                "needs_you_types": ["replies"],
                "is_multi_channel": True,
                "activity_at": 5000,
                "origin_at": 4000,
                "obligations": [{
                    "obligation_id": "ob_in_1",
                    "kind": "correspondence",
                    "status": "ready",
                    "correspondence_mode": "inbound_reply",
                    "description": "Reply to Thread alpha",
                    "trigger_expr": "correspondence_turn(gmail_thread:t1, 7)",
                }],
                "artifacts": [],
                "checklist": [],
                "reasons": ["📨 Reply needed: Thread alpha"],
            },
            {
                "work_id": "w_hot_review",
                "title": "External SkParagraph CL needing review",
                "lane": "needs-you",
                "work_types": ["reviews", "my-bugs"],
                "needs_you_types": ["reviews", "my-bugs"],
                "is_multi_channel": True,
                "activity_at": 4800,
                "origin_at": 3800,
                "obligations": [{
                    "obligation_id": "ob_sil_1",
                    "kind": "correspondence",
                    "status": "ready",
                    "correspondence_mode": "silence_ping",
                    "description": "Follow up on ping",
                    "trigger_expr": "correspondence_turn(gmail_thread:t2, 7)",
                }],
                "artifacts": [],
                "checklist": [],
                "reasons": ["🟢 Ready to CR+1: crrev/c/7686597 PS4"],
            },
            {
                "work_id": "w_hot_own_cl",
                "title": "Own CL with merge conflict alpha",
                "lane": "needs-you",
                "work_types": ["my-cls"],
                "needs_you_types": ["my-cls"],
                "is_multi_channel": False,
                "activity_at": 4600,
                "origin_at": 3600,
                "obligations": [],
                "artifacts": [],
                "checklist": [],
                "reasons": ["⚠️ Merge conflict: Gerrit 1418836"],
            },
            {
                "work_id": "w_wait_bug",
                "title": "Fresh assigned bug waiting for CL alpha",
                "lane": "waiting",
                "work_types": ["my-bugs"],
                "needs_you_types": [],
                "is_multi_channel": False,
                "activity_at": 4400,
                "origin_at": 3400,
                "obligations": [],
                "artifacts": [],
                "checklist": [],
                "reasons": ["Assigned bug (no CL yet)"],
            },
            {
                "work_id": "w_wait_rev",
                "title": "External PR waiting on author",
                "lane": "waiting",
                "work_types": ["reviews"],
                "needs_you_types": [],
                "is_multi_channel": False,
                "activity_at": 4200,
                "origin_at": 3200,
                "obligations": [],
                "artifacts": [],
                "checklist": [],
                "reasons": ["WAITING_ON_AUTHOR"],
            },
            {
                "work_id": "w_done_bug",
                "title": "Completed bug fix alpha",
                "lane": "done",
                "work_types": ["my-bugs", "my-cls"],
                "needs_you_types": [],
                "is_multi_channel": True,
                "activity_at": 3000,
                "origin_at": 2000,
                "obligations": [],
                "artifacts": [],
                "checklist": [],
                "reasons": ["All CLs merged and obligations satisfied"],
            },
        ],
    }

    html_out = workbench_server.render_workbench_html(ui_proj, ui_version=3)
    self.assertNotIn('data-filter="multi"', html_out)
    self.assertNotIn('data-filter="obligations"', html_out)

    # Parse actual SSR <button class="wb-chip"> elements and verify initial server counts
    parsed_lane_chips: list[dict[str, str]] = []
    parsed_type_chips: list[dict[str, str]] = []
    for btn_m in re.finditer(r'<button class="wb-chip"([^>]*)>', html_out):
      attr_map = {
          k: html_lib.unescape(v)
          for k, v in re.findall(r'([a-zA-Z0-9_-]+)="([^"]*)"', btn_m.group(1))
      }
      if "data-filter" in attr_map:
        parsed_lane_chips.append(attr_map)
      elif "data-type" in attr_map:
        parsed_type_chips.append(attr_map)
    self.assertEqual(
        {c["data-filter"]: int(c["data-count"]) for c in parsed_lane_chips},
        {"all": 5, "needs-you": 3, "waiting": 2, "done": 1},
    )
    self.assertEqual(
        {c["data-type"]: int(c["data-count"]) for c in parsed_type_chips},
        {"all": 5, "replies": 1, "reviews": 3, "my-bugs": 2, "my-cls": 1},
    )
    lane_specs_json = json.dumps(parsed_lane_chips)
    type_specs_json = json.dumps(parsed_type_chips)

    match = re.search(r"<script>\n(.*?)\n</script>", html_out, re.DOTALL)
    self.assertIsNotNone(match)
    assert match is not None
    script_js = match.group(1)

    node_2d_test = """
const assert = require('assert');
function makeEl(id, extra = {}) {
  const attrs = Object.assign({}, extra.attrs || {});
  const listeners = {};
  return Object.assign({
    id,
    value: extra.value || '',
    textContent: extra.textContent || '',
    innerHTML: extra.innerHTML || '',
    disabled: false,
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
    setAttribute(k, v) { attrs[k] = String(v); },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k); },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    dispatch(ev, evtObj) { for (const fn of (listeners[ev] || [])) fn(evtObj || {}); },
  }, extra.props || {});
}
const els = {
  'wb-synced': makeEl('wb-synced'),
  'kpi-needs': makeEl('kpi-needs'),
  'kpi-wait': makeEl('kpi-wait'),
  'kpi-obs': makeEl('kpi-obs'),
  'kpi-tray': makeEl('kpi-tray'),
  'kpi-prec': makeEl('kpi-prec'),
  'wb-tray-container': makeEl('wb-tray-container'),
  'wb-cards-container': makeEl('wb-cards-container'),
  'wb-new-btn': makeEl('wb-new-btn'),
  'wb-cancel-new': makeEl('wb-cancel-new'),
  'wb-new-form': makeEl('wb-new-form', { props: { reset() {} } }),
  'wb-sync-btn': makeEl('wb-sync-btn'),
  'wb-toggle-collapse': makeEl('wb-toggle-collapse', { attrs: { 'data-action': 'collapse' } }),
  'wb-search': makeEl('wb-search'),
  'wb-sort-mode': makeEl('wb-sort-mode', { value: 'triage' }),
  'wb-sort-dir': makeEl('wb-sort-dir', { attrs: { 'data-dir': 'desc' }, textContent: '↓ New' }),
};
const laneChips = """ + lane_specs_json + """.map((attrs) =>
  makeEl('lane-' + attrs['data-filter'], { attrs })
);
const typeChips = """ + type_specs_json + """.map((attrs) =>
  makeEl('type-' + attrs['data-type'], { attrs })
);
const allChips = laneChips.concat(typeChips);
global.localStorage = { getItem: () => null, setItem: () => {} };
global.window = { addEventListener: () => {}, getSelection: () => '', prompt: () => '' };
global.setInterval = () => 0;
global.fetch = async () => ({ ok: false });
global.document = {
  getElementById: (id) => els[id] || makeEl(id),
  querySelectorAll: (sel) => (sel === '.wb-chip' ? allChips : []),
  createElement: () => {
    let txt = '';
    return {
      set textContent(v) { txt = String(v || ''); },
      get innerHTML() {
        return txt.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      },
    };
  },
  body: { addEventListener: () => {} },
};

""" + script_js + """

function visibleIds() {
  const re = /data-work-id="([^"]+)"/g;
  const out = [];
  let m;
  while ((m = re.exec(els['wb-cards-container'].innerHTML)) !== null) out.push(m[1]);
  return out;
}

// Check correspondence obligation badges under Needs You and Done badge under Done
laneChips[1].dispatch('click');
typeChips[0].dispatch('click');
assert(els['wb-cards-container'].innerHTML.includes('📨 NEEDS REPLY'));
assert(els['wb-cards-container'].innerHTML.includes('⏰ 7d SILENCE PING'));

laneChips[3].dispatch('click');
assert(els['wb-cards-container'].innerHTML.includes('✅ Done'));
assert(!els['wb-cards-container'].innerHTML.includes('⏳ Waiting'));

// Check key semantic slices: Needs You + Reviews excludes w_mixed_reply (email-only urgency)
laneChips[1].dispatch('click');
typeChips[2].dispatch('click');
assert.deepStrictEqual(visibleIds(), ['w_hot_review']);

// Switching to All + Reviews includes all 3 active review cards
laneChips[0].dispatch('click');
assert.deepStrictEqual(visibleIds(), ['w_mixed_reply', 'w_hot_review', 'w_wait_rev']);

// Exhaustively verify every (query, laneChip, typeChip) pair matches its displayed data-count
for (const q of ['', 'alpha']) {
  els['wb-search'].value = q;
  els['wb-search'].dispatch('input', { target: els['wb-search'] });
  for (const lc of laneChips) {
    lc.dispatch('click');
    for (const tc of typeChips) {
      const advertisedTypeCnt = Number(tc.getAttribute('data-count'));
      tc.dispatch('click');
      const lVal = lc.getAttribute('data-filter');
      const tVal = tc.getAttribute('data-type');
      assert.strictEqual(
        visibleIds().length,
        advertisedTypeCnt,
        `Type count mismatch for q=${q} lane=${lVal} type=${tVal}`
      );
      assert(tc.innerHTML.includes(`(${advertisedTypeCnt})`));
    }
  }
  for (const tc of typeChips) {
    tc.dispatch('click');
    for (const lc of laneChips) {
      const advertisedLaneCnt = Number(lc.getAttribute('data-count'));
      lc.dispatch('click');
      const tVal = tc.getAttribute('data-type');
      const lVal = lc.getAttribute('data-filter');
      assert.strictEqual(
        visibleIds().length,
        advertisedLaneCnt,
        `Lane count mismatch for q=${q} type=${tVal} lane=${lVal}`
      );
      assert(lc.innerHTML.includes(`(${advertisedLaneCnt})`));
    }
  }
}
"""
    res = subprocess.run(
        ["node", "-e", node_2d_test], capture_output=True, text=True, check=False
    )
    self.assertEqual(
        res.returncode,
        0,
        f"Node 2D filter check failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )

  def test_github_issue_and_pr_correspondence_sync_and_trash(self) -> None:
    # Verify GitHub GraphQL correspondence reconciliation, trash gating, and body-bug demotion
    import base64
    from datetime import datetime, timezone
    from unittest import mock
    import ingestor
    import work_store

    now = 1794256720.0
    iso_old_ext = datetime.fromtimestamp(now - 7200, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    iso_self = datetime.fromtimestamp(now - 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    iso_closed = datetime.fromtimestamp(now - 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    iso_post_close = datetime.fromtimestamp(now - 600, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # Mid-sentence citations must not pollute bug_title while structured list items still parse
    prose_body = (
        "Thank you for filing. Another report is #109825: the attached text mixes CJK and "
        "Arabic with combining marks and looks similar to the old crash."
    )
    self.assertEqual(ingestor.extract_bug_titles_from_html_or_text(prose_body), {})
    bullet_html = (
        "<ul><li>flutter#109825 — Crash on CJK and Arabic with combining marks</li></ul>"
    )
    self.assertEqual(
        ingestor.extract_bug_titles_from_html_or_text(bullet_html),
        {"github_issue:flutter/flutter:109825": "Crash on CJK and Arabic with combining marks"},
    )
    self.assertEqual(
        work_store.extract_github_notif_target(
            "[flutter/flutter] [windows] Crash in skia::textlayout::TextLine::buildTextBlob  "
            "(Issue #116575)"
        ),
        "github_issue:flutter/flutter:116575",
    )
    self.assertEqual(
        work_store.extract_github_notif_target(
            "Re: [flutter/flutter] [web_paragraph] Render soft hyphens (PR #195366)"
        ),
        "github_pr:flutter/flutter:195366",
    )

    def _b64(text: str) -> str:
      return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")

    store = work_store.WorkStore(self.db_path)

    # Verify _fetch_gmail_thread_provenance detects bot auto-lock and preserves human turn
    lock_thread_payload = {
        "messages": [
            {
                "id": "m_db_1",
                "internalDate": str(int((now - 7200) * 1000)),
                "labelIds": ["IMPORTANT", "CATEGORY_UPDATES"],
                "snippet": "Same crash #109825: the attached text...",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {
                            "name": "Subject",
                            "value": (
                                "[flutter/flutter] [windows] Crash in "
                                "skia::textlayout::TextLine::buildTextBlob  (Issue #116575)"
                            ),
                        },
                        {"name": "From", "value": "David Bebawy <notifications@github.com>"},
                        {"name": "To", "value": "flutter/flutter <flutter@noreply.github.com>"},
                    ],
                    "body": {"data": _b64(prose_body)},
                },
            },
            {
                "id": "m_bot_lock",
                "internalDate": str(int((now - 600) * 1000)),
                "labelIds": ["IMPORTANT", "CATEGORY_PERSONAL"],
                "snippet": "This thread has been automatically locked",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {
                            "name": "Subject",
                            "value": (
                                "Re: [flutter/flutter] [windows] Crash in "
                                "skia::textlayout::TextLine::buildTextBlob  (Issue #116575)"
                            ),
                        },
                        {
                            "name": "From",
                            "value": "github-actions[bot] <notifications@github.com>",
                        },
                        {"name": "To", "value": "flutter/flutter <flutter@noreply.github.com>"},
                    ],
                    "body": {
                        "data": _b64(
                            "This thread has been automatically locked since there has not been "
                            "any recent activity after it was closed. Please open a new issue."
                        )
                    },
                },
            },
        ]
    }
    with mock.patch("subprocess.run") as mock_run:
      mock_run.return_value = subprocess.CompletedProcess(
          ["gmail"], 0, stdout=json.dumps(lock_thread_payload), stderr=""
      )
      prov_116575 = ingestor._fetch_gmail_thread_provenance("th_lock_116575", {}, now)
    self.assertEqual(prov_116575.get("schema_ver"), 6)
    self.assertEqual(
        prov_116575.get("gh_target_key"), "github_issue:flutter/flutter:116575"
    )
    self.assertTrue(bool(prov_116575.get("gh_locked")))
    self.assertTrue(bool(prov_116575.get("gh_thread_closed")))
    self.assertFalse(bool(prov_116575.get("last_from_me")))

    # Case 1: Closed/locked #116575 + collateral body #109825 -> done and #109825 context_only
    w_116575 = store.create_work("Issue #116575", now=now - 10000)
    store.upsert_artifact(
        "gerrit:skia:572293",
        kind="gerrit",
        title="572293: Fix CJK/Arabic buildTextBlob crash",
        state={"status": "MERGED"},
        now=now - 10000,
    )
    store.upsert_artifact(
        "github_issue:flutter/flutter:116575",
        kind="github_issue",
        title="flutter#116575: [windows] Crash in skia::textlayout::TextLine::buildTextBlob",
        state={},
        now=now - 10000,
    )
    store.upsert_artifact(
        "github_issue:flutter/flutter:109825",
        kind="github_issue",
        title="flutter#109825: the attached text mixes CJK and Arabic",
        state={"status": "OPEN", "bug_title": "the attached text mixes CJK and Arabic"},
        now=now - 10000,
    )
    store.upsert_artifact(
        "gmail_thread:th_116575",
        kind="gmail_thread",
        title=(
            "[flutter/flutter] [windows] Crash in skia::textlayout::TextLine::buildTextBlob "
            "(Issue #116575) (David Bebawy)"
        ),
        state={
            "thread_id": "th_116575",
            "root_subject": (
                "[flutter/flutter] [windows] Crash in "
                "skia::textlayout::TextLine::buildTextBlob  (Issue #116575)"
            ),
            "counterparty": "David Bebawy",
            "is_github_issue_thread": True,
            "last_from_me": False,
            "last_message_ts": now - 7200,
            "unanswered_human": True,
            "in_trash_or_spam": False,
        },
        now=now - 7200,
    )
    store.link_artifact(w_116575, "gerrit:skia:572293", role="primary", source="auto", now=now)
    store.link_artifact(
        w_116575,
        "github_issue:flutter/flutter:116575",
        role="primary",
        source="auto",
        link_reason="gmail_thread_ref",
        link_detail="Из переписки '[flutter/flutter] [windows] Crash in skia::textlay'",
        now=now,
    )
    store.link_artifact(
        w_116575,
        "github_issue:flutter/flutter:109825",
        role="primary",
        source="auto",
        link_reason="gmail_thread_ref",
        link_detail="Из переписки '[flutter/flutter] [windows] Crash in skia::textlay'",
        now=now,
    )
    store.link_artifact(
        w_116575,
        "gmail_thread:th_116575",
        role="primary",
        source="auto",
        link_reason="github_issue_thread",
        now=now,
    )

    # Case 2: Open #185368 where Rusino replied on GitHub after flar -> Waiting
    w_multi = store.create_work("txt_benchmarks", now=now - 10000)
    store.upsert_artifact(
        "github_issue:flutter/flutter:185368",
        kind="github_issue",
        title="flutter#185368: Engine benchmarks are no longer running after monorepo",
        state={"status": "OPEN"},
        now=now - 10000,
    )
    store.upsert_artifact(
        "gmail_thread:th_185368",
        kind="gmail_thread",
        title=(
            "[flutter/flutter] Engine benchmarks are no longer running after monorepo "
            "(Issue #185368)"
        ),
        state={
            "thread_id": "th_185368",
            "root_subject": (
                "[flutter/flutter] Engine benchmarks are no longer running after monorepo "
                "(Issue #185368)"
            ),
            "counterparty": "flar",
            "is_github_issue_thread": True,
            "last_from_me": False,
            "last_message_ts": now - 7200,
            "unanswered_human": True,
            "in_trash_or_spam": False,
        },
        now=now - 7200,
    )
    store.link_artifact(
        w_multi, "github_issue:flutter/flutter:185368", role="primary", source="auto", now=now
    )
    store.link_artifact(w_multi, "gmail_thread:th_185368", role="channel", source="auto", now=now)

    # Case 3: Unlocked CLOSED #199001 with post-close external comment after Rusino -> Needs You
    w_reopened = store.create_work("Post-close comment issue #199001", now=now - 10000)
    store.upsert_artifact(
        "github_issue:flutter/flutter:199001",
        kind="github_issue",
        title="flutter#199001: Post-close reply check",
        state={},
        now=now - 10000,
    )
    store.upsert_artifact(
        "gmail_thread:th_199001",
        kind="gmail_thread",
        title="[flutter/flutter] Post-close reply check (Issue #199001) (Alice)",
        state={
            "thread_id": "th_199001",
            "root_subject": "[flutter/flutter] Post-close reply check (Issue #199001)",
            "counterparty": "Alice",
            "is_github_issue_thread": True,
            "last_from_me": False,
            "last_message_ts": now - 600,
            "unanswered_human": True,
            "in_trash_or_spam": False,
        },
        now=now - 600,
    )
    store.link_artifact(
        w_reopened, "github_issue:flutter/flutter:199001", role="primary", source="auto", now=now
    )
    store.link_artifact(
        w_reopened, "gmail_thread:th_199001", role="primary", source="auto", now=now
    )

    # Case 4: PR thread reviews (skipping PENDING) + 30s delivery lag -> Waiting
    iso_pr_ext = datetime.fromtimestamp(now - 3610, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    w_pr = store.create_work("PR #195366 soft hyphens", now=now - 10000)
    store.upsert_artifact(
        "github_pr:flutter/flutter:195366",
        kind="github_pr",
        title="195366: [web_paragraph] Render soft hyphens",
        state={"status": "OPEN", "is_mine": False, "role": "reviewer"},
        now=now - 10000,
    )
    store.upsert_artifact(
        "gmail_thread:th_pr_195366",
        kind="gmail_thread",
        title="[flutter/flutter] [web_paragraph] Render soft hyphens (PR #195366)",
        state={
            "thread_id": "th_pr_195366",
            "root_subject": "[flutter/flutter] [web_paragraph] Render soft hyphens (PR #195366)",
            "gh_target_key": "github_pr:flutter/flutter:195366",
            "counterparty": "contributor1",
            "is_github_pr_thread": True,
            "last_from_me": False,
            "last_message_ts": now - 3590,
            "unanswered_human": True,
            "in_trash_or_spam": False,
        },
        now=now - 3590,
    )
    store.link_artifact(w_pr, "github_pr:flutter/flutter:195366", role="primary", now=now)
    store.link_artifact(w_pr, "gmail_thread:th_pr_195366", role="channel", now=now)

    store.reconcile_correspondence_obligations(now=now)
    graphql_resp = {
        "data": {
            "repository": {
                "n0": {
                    "__typename": "Issue",
                    "number": 116575,
                    "title": "[windows] Crash in skia::textlayout::TextLine::buildTextBlob",
                    "state": "CLOSED",
                    "locked": True,
                    "closedAt": iso_closed,
                    "updatedAt": iso_closed,
                    "url": "https://github.com/flutter/flutter/issues/116575",
                    "comments": {
                        "nodes": [
                            {
                                "createdAt": iso_old_ext,
                                "viewerDidAuthor": False,
                                "author": {"login": "DavidBebawy"},
                            },
                            {
                                "createdAt": iso_self,
                                "viewerDidAuthor": True,
                                "author": {"login": "Rusino"},
                            },
                        ]
                    },
                },
                "n1": {
                    "__typename": "Issue",
                    "number": 109825,
                    "title": "Real upstream title for 109825",
                    "state": "OPEN",
                    "locked": False,
                    "closedAt": None,
                    "updatedAt": iso_old_ext,
                    "url": "https://github.com/flutter/flutter/issues/109825",
                    "comments": {"nodes": []},
                },
                "n2": {
                    "__typename": "Issue",
                    "number": 185368,
                    "title": "Engine benchmarks are no longer running after monorepo",
                    "state": "OPEN",
                    "locked": False,
                    "closedAt": None,
                    "updatedAt": iso_self,
                    "url": "https://github.com/flutter/flutter/issues/185368",
                    "comments": {
                        "nodes": [
                            {
                                "createdAt": iso_old_ext,
                                "viewerDidAuthor": False,
                                "author": {"login": "flar"},
                            },
                            {
                                "createdAt": iso_self,
                                "viewerDidAuthor": True,
                                "author": {"login": "Rusino"},
                            },
                        ]
                    },
                },
                "n3": {
                    "__typename": "Issue",
                    "number": 199001,
                    "title": "Post-close reply check",
                    "state": "CLOSED",
                    "locked": False,
                    "closedAt": iso_closed,
                    "updatedAt": iso_post_close,
                    "url": "https://github.com/flutter/flutter/issues/199001",
                    "comments": {
                        "nodes": [
                            {
                                "createdAt": iso_self,
                                "viewerDidAuthor": True,
                                "author": {"login": "Rusino"},
                            },
                            {
                                "createdAt": iso_post_close,
                                "viewerDidAuthor": False,
                                "author": {"login": "alice"},
                            },
                        ]
                    },
                },
                "n4": {
                    "__typename": "PullRequest",
                    "number": 195366,
                    "title": "[web_paragraph] Render soft hyphens",
                    "state": "OPEN",
                    "locked": False,
                    "closedAt": None,
                    "mergedAt": None,
                    "updatedAt": iso_self,
                    "url": "https://github.com/flutter/flutter/pull/195366",
                    "comments": {
                        "nodes": [
                            {
                                "createdAt": iso_pr_ext,
                                "viewerDidAuthor": False,
                                "author": {"login": "contributor1"},
                            }
                        ]
                    },
                    "reviews": {
                        "nodes": [
                            {
                                "state": "APPROVED",
                                "submittedAt": iso_self,
                                "viewerDidAuthor": True,
                                "author": {"login": "Rusino"},
                            },
                            {
                                "state": "PENDING",
                                "submittedAt": None,
                                "viewerDidAuthor": False,
                                "author": {"login": "contributor1"},
                            },
                        ]
                    },
                },
            }
        }
    }
    w_state: dict[str, object] = {}
    with mock.patch("subprocess.run") as mock_run:
      mock_run.return_value = subprocess.CompletedProcess(
          ["gh"], 0, stdout=json.dumps(graphql_resp), stderr=""
      )
      ingestor.sync_github_artifacts(store, w_state, now=now, force=True)

    store.purge_config_artifacts_and_works(now=now)
    store.reconcile_correspondence_obligations(now=now)
    proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)
    cards = {c["work_id"]: c for c in proj["works"]}

    self.assertEqual(
        (store.get_artifact("github_issue:flutter/flutter:116575") or {}).get("state", {}).get(
            "status"
        ),
        "CLOSED",
    )
    self.assertEqual(
        (store.get_artifact("github_issue:flutter/flutter:109825") or {}).get("title"),
        "flutter#109825: Real upstream title for 109825",
    )
    self.assertEqual(
        store.get_work_artifact_roles(w_116575).get("github_issue:flutter/flutter:109825"),
        "context_only",
    )
    self.assertEqual(cards[w_116575]["lane"], "done")
    self.assertTrue(
        all(o["status"] == "done" for o in cards[w_116575]["obligations"])
    )

    self.assertEqual(cards[w_multi]["lane"], "waiting")
    self.assertEqual(
        [o["status"] for o in cards[w_multi]["obligations"]], ["open"]
    )
    self.assertFalse(
        bool((store.get_artifact("gmail_thread:th_185368") or {}).get("state", {}).get(
            "unanswered_human"
        ))
    )

    self.assertEqual(cards[w_reopened]["lane"], "needs-you")
    self.assertEqual(
        [o["status"] for o in cards[w_reopened]["obligations"]], ["ready"]
    )
    self.assertEqual(cards[w_pr]["lane"], "waiting")
    self.assertFalse(
        bool((store.get_artifact("gmail_thread:th_pr_195366") or {}).get("state", {}).get(
            "unanswered_human"
        ))
    )

    # Case 5: Trashed GitHub thread never mints a Work and refreshes tracked threads to done
    w_trashed = store.create_work("Trashed GH thread", now=now - 5000)
    store.upsert_artifact(
        "gmail_thread:th_trash_existing",
        kind="gmail_thread",
        title="[flutter/flutter] Old issue (Issue #177777) (Bob)",
        state={
            "thread_id": "th_trash_existing",
            "root_subject": "[flutter/flutter] Old issue (Issue #177777)",
            "counterparty": "Bob",
            "is_github_issue_thread": True,
            "last_from_me": False,
            "last_message_ts": now - 500,
            "unanswered_human": True,
            "in_trash_or_spam": False,
        },
        now=now - 500,
    )
    store.link_artifact(w_trashed, "gmail_thread:th_trash_existing", role="primary", now=now - 500)
    store.reconcile_correspondence_obligations(w_trashed, now=now - 400)

    trashed_existing_payload = {
        "messages": [{
            "id": "m_trash_existing",
            "internalDate": str(int((now - 500) * 1000)),
            "labelIds": ["TRASH", "CATEGORY_UPDATES"],
            "snippet": "Hello",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "[flutter/flutter] Old issue (Issue #177777)"},
                    {"name": "From", "value": "Bob <notifications@github.com>"},
                    {"name": "To", "value": "flutter/flutter <flutter@noreply.github.com>"},
                ],
                "body": {"data": _b64("Can you check flutter#177777?")},
            },
        }]
    }
    trashed_gh_payload = {
        "messages": [{
            "id": "m_trash_new",
            "internalDate": str(int((now - 100) * 1000)),
            "labelIds": ["TRASH", "CATEGORY_UPDATES"],
            "snippet": "Hello",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "[flutter/flutter] Trashed new (Issue #188888)"},
                    {"name": "From", "value": "Someone <notifications@github.com>"},
                    {"name": "To", "value": "flutter/flutter <flutter@noreply.github.com>"},
                ],
                "body": {"data": _b64("Can you check flutter#188888?")},
            },
        }]
    }

    def _fake_gmail_trash(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
      if "drafts" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
      if "search" in cmd:
        q = cmd[cmd.index("search") + 1]
        if "notifications@github.com" in q:
          return subprocess.CompletedProcess(
              cmd, 0, stdout=json.dumps([{"threadId": "th_trash_new"}]), stderr=""
          )
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
      if "get-thread" in cmd and "th_trash_new" in cmd:
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(trashed_gh_payload), stderr=""
        )
      if "get-thread" in cmd and "th_trash_existing" in cmd:
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(trashed_existing_payload), stderr=""
        )
      return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    works_before = len(store.list_works())
    with (
        mock.patch.object(ingestor.os.path, "isfile", return_value=True),
        mock.patch("subprocess.run", side_effect=_fake_gmail_trash),
    ):
      tray_after = ingestor.ingest_gmail_drafts_and_threads(store, {}, now=now)
    trashed_card = [
        c
        for c in store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)["works"]
        if c["work_id"] == w_trashed
    ][0]
    self.assertTrue(all(o["status"] == "done" for o in trashed_card["obligations"]))
    self.assertEqual(len(store.list_works()), works_before)
    self.assertEqual(tray_after, [])
    self.assertIsNone(store.get_artifact("github_issue:flutter/flutter:188888"))

  def test_github_sync_edge_contracts_b1_to_b6(self) -> None:
    # Verify B1-B6 GitHub sync invariants: turn preservation, PR provenance, push lag,
    # cross-issue close filter, CL-less closed issue completion, and generic owner/repo parsing
    import base64
    from datetime import datetime, timezone
    from unittest import mock
    import ingestor
    import work_store

    now = 1794300000.0
    iso_self = datetime.fromtimestamp(now - 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    iso_human = datetime.fromtimestamp(now - 600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _b64(text: str) -> str:
      return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")

    store = work_store.WorkStore(self.db_path)

    # B1: Post-self-reply GitHub human comment before Gmail delivery keeps last_from_me=False,
    # wakes parked auto-gated reply drafts, and classifies as Reply needed (never 7d silence ping)
    with self.subTest("B1"):
      w_b1 = store.create_work("B1 post-reply GitHub comment", now=now - 10000)
      store.upsert_artifact(
          "github_issue:flutter/flutter:198001",
          kind="github_issue",
          title="flutter#198001: B1 issue",
          state={"status": "OPEN"},
          now=now - 10000,
      )
      store.upsert_artifact(
          "gerrit:skia:1498001",
          kind="gerrit",
          title="1498001: Fix B1",
          state={"status": "NEW", "is_mine": True, "entity_state": {"lane": "waiting"}},
          now=now - 10000,
      )
      store.upsert_artifact(
          "gmail_thread:th_b1",
          kind="gmail_thread",
          title="[flutter/flutter] B1 issue (Issue #198001) (Alice)",
          state={
              "thread_id": "th_b1",
              "root_subject": "[flutter/flutter] B1 issue (Issue #198001)",
              "gh_target_key": "github_issue:flutter/flutter:198001",
              "counterparty": "Alice",
              "is_github_issue_thread": True,
              "sent_by_me": True,
              "last_from_me": True,
              "last_message_ts": now - 7200,
              "gh_self_reply_ts": now - 3600,
              "unanswered_human": False,
          },
          now=now - 3600,
      )
      store.link_artifact(w_b1, "github_issue:flutter/flutter:198001", role="primary", now=now)
      store.link_artifact(w_b1, "gerrit:skia:1498001", role="primary", now=now)
      store.link_artifact(w_b1, "gmail_thread:th_b1", role="channel", now=now)
      store.save_reply_draft(
          w_b1,
          "Merged CL 1498001 and fixed flutter#198001.",
          thread_key="gmail_thread:th_b1",
          auto_gate=True,
          now=now - 4000,
      )

      b1_gql = {
          "data": {
              "repository": {
                  "n0": {
                      "__typename": "Issue",
                      "number": 198001,
                      "title": "B1 issue",
                      "state": "OPEN",
                      "locked": False,
                      "closedAt": None,
                      "updatedAt": iso_human,
                      "url": "https://github.com/flutter/flutter/issues/198001",
                      "comments": {
                          "nodes": [
                              {
                                  "createdAt": iso_self,
                                  "viewerDidAuthor": True,
                                  "author": {"login": "Rusino"},
                              },
                              {
                                  "createdAt": iso_human,
                                  "viewerDidAuthor": False,
                                  "author": {"login": "alice"},
                              },
                          ]
                      },
                  }
              }
          }
      }
      with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            ["gh"], 0, stdout=json.dumps(b1_gql), stderr=""
        )
        ingestor.sync_github_artifacts(store, {}, now=now, force=True)

      # Stale Gmail re-sync before email delivery must not wipe newer gh_human_reply_ts
      store.upsert_artifact(
          "gmail_thread:th_b1",
          kind="gmail_thread",
          state={
              "last_from_me": True,
              "last_message_ts": now - 7200,
              "unanswered_human": False,
          },
          now=now,
      )
      store.reconcile_correspondence_obligations(w_b1, now=now)
      b1_th_st = (store.get_artifact("gmail_thread:th_b1") or {}).get("state") or {}
      self.assertFalse(bool(b1_th_st.get("last_from_me")))
      self.assertTrue(bool(b1_th_st.get("unanswered_human")))
      self.assertEqual(float(b1_th_st.get("gh_human_reply_ts") or 0.0), now - 600)
      b1_issue_art = store.get_artifact("github_issue:flutter/flutter:198001") or {}
      self.assertFalse(
          work_store._is_correspondence_silence_ping(
              b1_th_st, {"github_issue:flutter/flutter:198001": b1_issue_art}
          )
      )
      b1_proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)
      b1_card = [c for c in b1_proj["works"] if c["work_id"] == w_b1][0]
      self.assertEqual(b1_card["lane"], "needs-you")
      self.assertTrue(
          any("Reply needed" in r or "Unanswered" in r for r in b1_card["reasons"])
      )
      self.assertFalse(any("7d silence ping" in r for r in b1_card["reasons"]))

    # B2: PR threads stay protected in purge, lose origin tiebreak to real mail, and label right
    with self.subTest("B2"):
      w_b2_solo = store.create_work("Solo PR notif", now=now - 5000)
      store.upsert_artifact(
          "gmail_thread:th_b2_solo",
          kind="gmail_thread",
          title="[flutter/flutter] Solo PR (PR #198002) (Bob)",
          state={
              "thread_id": "th_b2_solo",
              "root_subject": "[flutter/flutter] Solo PR (PR #198002)",
              "is_github_pr_thread": True,
              "sent_by_me": False,
              "schema_ver": 6,
              "in_inbox": False,
              "has_personal_category": False,
              "me_addressed": False,
              "recipient_count": 0,
          },
          now=now - 5000,
      )
      store.link_artifact(
          w_b2_solo,
          "gmail_thread:th_b2_solo",
          role="primary",
          link_reason="github_pr_thread",
          now=now - 5000,
      )
      w_b2 = store.create_work("Combined human + PR notif", now=now - 5000)
      store.upsert_artifact(
          "gmail_thread:th_b2_pr",
          kind="gmail_thread",
          title="[flutter/flutter] PR thread (PR #198012) (Bob)",
          state={
              "thread_id": "th_b2_pr",
              "root_subject": "[flutter/flutter] PR thread (PR #198012)",
              "origin_ts": now - 4000,
              "is_github_pr_thread": True,
              "sent_by_me": False,
              "schema_ver": 6,
          },
          now=now - 4000,
      )
      store.upsert_artifact(
          "gmail_thread:th_b2_human",
          kind="gmail_thread",
          title="Real design review email (Bob)",
          state={
              "thread_id": "th_b2_human",
              "root_subject": "Real design review email",
              "origin_ts": now - 1000,
              "is_active_correspondence": True,
              "sent_by_me": True,
              "schema_ver": 6,
          },
          now=now - 1000,
      )
      store.link_artifact(
          w_b2,
          "gmail_thread:th_b2_pr",
          role="primary",
          link_reason="github_pr_thread",
          now=now - 4000,
      )
      store.link_artifact(
          w_b2,
          "gmail_thread:th_b2_human",
          role="primary",
          link_reason="gmail_root",
          now=now - 1000,
      )
      store.purge_config_artifacts_and_works(now=now)
      b2_proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)
      b2_active_wids = {c["work_id"] for c in b2_proj["works"]}
      self.assertIn(w_b2_solo, b2_active_wids)
      b2_card = [c for c in b2_proj["works"] if c["work_id"] == w_b2][0]
      _, origin_info = work_store.resolve_work_origin_and_title(
          {"work_id": w_b2, "title": b2_card["title"], "meta_json": "{}"},
          b2_card["artifacts"],
      )
      self.assertEqual(origin_info.get("artifact_key"), "gmail_thread:th_b2_human")
      pr_badge, _ = work_store.compute_artifact_provenance(
          {
              "artifact_key": "gmail_thread:th_b2_pr",
              "kind": "gmail_thread",
              "role": "primary",
              "link_reason": "github_pr_thread",
              "state": {"is_github_pr_thread": True},
          },
          [],
      )
      self.assertEqual(pr_badge, "дискуссия в PR")

    # B3: Post-review PR push newer than last_self_comment_at wakes card even after synced_at,
    # while 30s delivery lag relative to gh_human_ts stays covered
    with self.subTest("B3"):
      w_b3 = store.create_work("B3 PR push after review", now=now - 10000)
      store.upsert_artifact(
          "github_pr:flutter/flutter:198003",
          kind="github_pr",
          title="198003: B3 PR",
          state={
              "status": "OPEN",
              "is_mine": False,
              "role": "reviewer",
              "last_self_comment_at": now - 3600,
              "last_human_comment_at": 0.0,
              "synced_at": now - 20,
          },
          now=now - 20,
      )
      store.upsert_artifact(
          "gmail_thread:th_b3",
          kind="gmail_thread",
          title="[flutter/flutter] B3 PR (PR #198003) (Author)",
          state={
              "thread_id": "th_b3",
              "root_subject": "[flutter/flutter] B3 PR (PR #198003)",
              "gh_target_key": "github_pr:flutter/flutter:198003",
              "counterparty": "Author",
              "is_github_pr_thread": True,
              "last_from_me": False,
              "last_message_ts": now - 100,
              "gh_self_reply_ts": now - 3600,
              "unanswered_human": True,
          },
          now=now - 100,
      )
      store.link_artifact(w_b3, "github_pr:flutter/flutter:198003", role="primary", now=now)
      store.link_artifact(w_b3, "gmail_thread:th_b3", role="channel", now=now)
      store.reconcile_correspondence_obligations(w_b3, now=now)
      b3_proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)
      b3_card = [c for c in b3_proj["works"] if c["work_id"] == w_b3][0]
      self.assertEqual(b3_card["lane"], "needs-you")

      # Verify 30s delivery lag relative to gh_human_ts still stays Waiting
      w_b3_lag = store.create_work("B3 delivery lag stays waiting", now=now - 10000)
      store.upsert_artifact(
          "github_pr:flutter/flutter:198004",
          kind="github_pr",
          title="198004: B3 lag PR",
          state={
              "status": "OPEN",
              "is_mine": False,
              "role": "reviewer",
              "last_human_comment_at": now - 3610,
              "last_self_comment_at": now - 3600,
              "synced_at": now - 10,
          },
          now=now - 10,
      )
      store.upsert_artifact(
          "gmail_thread:th_b3_lag",
          kind="gmail_thread",
          title="[flutter/flutter] B3 lag PR (PR #198004) (Author)",
          state={
              "thread_id": "th_b3_lag",
              "root_subject": "[flutter/flutter] B3 lag PR (PR #198004)",
              "gh_target_key": "github_pr:flutter/flutter:198004",
              "counterparty": "Author",
              "is_github_pr_thread": True,
              "last_from_me": False,
              "last_message_ts": now - 3590,
              "gh_self_reply_ts": now - 3600,
              "unanswered_human": False,
          },
          now=now - 3590,
      )
      store.link_artifact(w_b3_lag, "github_pr:flutter/flutter:198004", role="primary", now=now)
      store.link_artifact(w_b3_lag, "gmail_thread:th_b3_lag", role="channel", now=now)
      store.reconcile_correspondence_obligations(w_b3_lag, now=now)
      b3_lag_proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)
      b3_lag_card = [c for c in b3_lag_proj["works"] if c["work_id"] == w_b3_lag][0]
      self.assertEqual(b3_lag_card["lane"], "waiting")

    # B4: Collateral close/merge of another issue number in body must not close target issue
    with self.subTest("B4"):
      b4_body = "We closed #109825 as not planned yesterday.\nStill open here."
      b4_payload = {
          "messages": [{
              "id": "m_b4_1",
              "internalDate": str(int((now - 500) * 1000)),
              "labelIds": ["INBOX", "CATEGORY_UPDATES"],
              "snippet": "We closed #109825 as not planned yesterday",
              "payload": {
                  "mimeType": "text/plain",
                  "headers": [
                      {
                          "name": "Subject",
                          "value": "[flutter/flutter] Open target (Issue #116576)",
                      },
                      {"name": "From", "value": "Alice <notifications@github.com>"},
                      {"name": "To", "value": "flutter/flutter <flutter@noreply.github.com>"},
                  ],
                  "body": {"data": _b64(b4_body)},
              },
          }]
      }
      with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            ["gmail"], 0, stdout=json.dumps(b4_payload), stderr=""
        )
        prov_b4 = ingestor._fetch_gmail_thread_provenance("th_b4_open", {}, now)
      self.assertFalse(bool(prov_b4.get("gh_thread_closed")))

    # B5: CL-less Work with already-closed/locked GitHub issue (0 obligations) completes to done
    with self.subTest("B5"):
      w_b5 = store.create_work("CL-less closed GH issue #116577", now=now - 10000)
      store.upsert_artifact(
          "github_issue:flutter/flutter:116577",
          kind="github_issue",
          title="flutter#116577: Closed issue with no CL",
          state={"status": "CLOSED", "locked": True, "closed_at": now - 3600, "synced_at": now},
          now=now,
      )
      store.upsert_artifact(
          "gmail_thread:th_b5",
          kind="gmail_thread",
          title="[flutter/flutter] Closed issue (Issue #116577) (Bob)",
          state={
              "thread_id": "th_b5",
              "root_subject": "[flutter/flutter] Closed issue (Issue #116577)",
              "gh_target_key": "github_issue:flutter/flutter:116577",
              "is_github_issue_thread": True,
              "gh_locked": True,
              "gh_thread_closed": True,
              "last_from_me": False,
              "unanswered_human": False,
          },
          now=now,
      )
      store.link_artifact(w_b5, "github_issue:flutter/flutter:116577", role="primary", now=now)
      store.link_artifact(w_b5, "gmail_thread:th_b5", role="primary", now=now)
      store.reconcile_correspondence_obligations(w_b5, now=now)
      b5_proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)
      b5_card = [c for c in b5_proj["works"] if c["work_id"] == w_b5][0]
      self.assertEqual(b5_card["obligations"], [])
      self.assertEqual(b5_card["lane"], "done")

      # Pure chat_chain Works without CLs or closed bugs stay waiting
      w_b5_chat = store.create_work("Pure chat stays waiting", now=now - 10000)
      store.upsert_artifact(
          "chat_chain:pure_b5",
          kind="chat_chain",
          title="Debugging chat",
          state={"bucket": "waiting"},
          now=now,
      )
      store.link_artifact(w_b5_chat, "chat_chain:pure_b5", role="primary", now=now)
      b5_chat_proj = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)
      b5_chat_card = [c for c in b5_chat_proj["works"] if c["work_id"] == w_b5_chat][0]
      self.assertEqual(b5_chat_card["lane"], "waiting")

    # B6: Generic <owner>/<repo> and <4-digit GH notifications never map to flutter/flutter
    with self.subTest("B6"):
      self.assertEqual(
          ingestor.extract_artifacts_from_text("[Rusino/paragraph] Fix wrap (PR #42)"),
          [(
              "github_pr:Rusino/paragraph:42",
              "github_pr",
              "https://github.com/Rusino/paragraph/pull/42",
          )],
      )
      self.assertEqual(
          ingestor.extract_artifacts_from_text("[Rusino/paragraph] Fix wrap (PR #1234)"),
          [(
              "github_pr:Rusino/paragraph:1234",
              "github_pr",
              "https://github.com/Rusino/paragraph/pull/1234",
          )],
      )
      self.assertEqual(
          ingestor.extract_artifacts_from_text("Re: PR #197779"),
          [(
              "github_pr:flutter/flutter:197779",
              "github_pr",
              "https://github.com/flutter/flutter/pull/197779",
          )],
      )
      b6_body = "Updated line wrapping in https://github.com/Rusino/paragraph/pull/42"
      b6_payload = {
          "messages": [{
              "id": "m_b6_1",
              "internalDate": str(int((now - 120) * 1000)),
              "labelIds": ["INBOX", "CATEGORY_UPDATES"],
              "snippet": "Please check PR #42",
              "payload": {
                  "mimeType": "text/plain",
                  "headers": [
                      {"name": "Subject", "value": "[Rusino/paragraph] Fix wrap (PR #42)"},
                      {"name": "From", "value": "Someone <notifications@github.com>"},
                      {"name": "To", "value": "Rusino/paragraph <paragraph@noreply.github.com>"},
                  ],
                  "body": {"data": _b64(b6_body)},
              },
          }]
      }

      def _fake_gmail_b6(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        if "drafts" in cmd:
          return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if "search" in cmd:
          q = cmd[cmd.index("search") + 1]
          if "notifications@github.com" in q:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps([{"threadId": "th_b6_para"}]), stderr=""
            )
          return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if "get-thread" in cmd and "th_b6_para" in cmd:
          return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(b6_payload), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

      with (
          mock.patch.object(ingestor.os.path, "isfile", return_value=True),
          mock.patch("subprocess.run", side_effect=_fake_gmail_b6),
      ):
        ingestor.ingest_gmail_drafts_and_threads(store, {}, now=now)
      b6_wid = store.find_work_by_artifact("github_pr:Rusino/paragraph:42", roles=("primary",))
      self.assertTrue(bool(b6_wid))
      self.assertIsNone(store.get_artifact("github_pr:flutter/flutter:42"))

  def test_github_iso_timestamp_parsing_across_correspondence_and_drafts(self) -> None:
    # Verify ISO-8601 strings from cl_watcher on GitHub artifacts never crash or skew turn order
    from datetime import datetime, timezone
    import work_store

    now = 1794300000.0

    def _iso(ts: float) -> str:
      return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    ts_old = now - 7200
    ts_self = now - 3600
    ts_closed = now - 1800
    ts_new_human = now - 600

    store = work_store.WorkStore(self.db_path)

    # Closed issue with ISO timestamps (cl_watcher format) must resolve to done without ValueError
    w_closed = store.create_work("ISO closed issue #198501", now=now - 10000)
    store.upsert_artifact(
        "github_issue:flutter/flutter:198501",
        kind="github_issue",
        title="flutter#198501: ISO closed issue",
        state={
            "status": "CLOSED",
            "locked": False,
            "closed_at": _iso(ts_closed),
            "last_human_comment_at": _iso(ts_old),
            "last_self_comment_at": _iso(ts_self),
            "synced_at": _iso(ts_closed + 60),
        },
        now=now,
    )
    store.upsert_artifact(
        "gmail_thread:th_iso_closed",
        kind="gmail_thread",
        title="[flutter/flutter] ISO closed (Issue #198501) (Alice)",
        state={
            "thread_id": "th_iso_closed",
            "root_subject": "[flutter/flutter] ISO closed (Issue #198501)",
            "gh_target_key": "github_issue:flutter/flutter:198501",
            "counterparty": "Alice",
            "is_github_issue_thread": True,
            "last_from_me": False,
            "last_message_ts": ts_old,
            "unanswered_human": True,
        },
        now=now,
    )
    store.link_artifact(w_closed, "github_issue:flutter/flutter:198501", role="primary", now=now)
    store.link_artifact(w_closed, "gmail_thread:th_iso_closed", role="channel", now=now)
    store.reconcile_correspondence_obligations(w_closed, now=now)
    # Also verify live cl_watcher_state['bugs'] ISO overlay during projection never crashes
    proj_closed = store.build_dashboard_projection(
        cl_watcher_state={
            "items": {},
            "bugs": {
                "gh_issue:flutter/flutter#198501": {
                    "bug_key": "gh_issue:flutter/flutter#198501",
                    "source": "github",
                    "id": "198501",
                    "tracker_flavor": "github",
                    "status": "CLOSED",
                    "last_human_comment_at": _iso(ts_old),
                    "last_self_comment_at": _iso(ts_self),
                }
            },
        },
        now=now,
    )
    card_closed = [c for c in proj_closed["works"] if c["work_id"] == w_closed][0]
    self.assertEqual(card_closed["lane"], "done")

    # Open issue with newer ISO human comment must wake done obligation, void stale draft & ping
    w_open = store.create_work("ISO open issue #198502", now=now - 10000)
    store.upsert_artifact(
        "github_issue:flutter/flutter:198502",
        kind="github_issue",
        title="flutter#198502: ISO open issue",
        state={
            "status": "OPEN",
            "last_human_comment_at": _iso(ts_old),
            "last_self_comment_at": _iso(ts_self),
        },
        now=now,
    )
    store.upsert_artifact(
        "gmail_thread:th_iso_open",
        kind="gmail_thread",
        title="[flutter/flutter] ISO open (Issue #198502) (Bob)",
        state={
            "thread_id": "th_iso_open",
            "root_subject": "[flutter/flutter] ISO open (Issue #198502)",
            "gh_target_key": "github_issue:flutter/flutter:198502",
            "counterparty": "Bob",
            "is_github_issue_thread": True,
            "last_from_me": True,
            "last_message_ts": ts_self,
            "unanswered_human": False,
        },
        now=now,
    )
    store.link_artifact(w_open, "github_issue:flutter/flutter:198502", role="primary", now=now)
    store.link_artifact(w_open, "gmail_thread:th_iso_open", role="channel", now=now)
    store.reconcile_correspondence_obligations(w_open, now=now - 1000)
    with store._connect() as conn:
      conn.execute(
          "UPDATE obligations SET status = 'done', satisfied_at = ? WHERE work_id = ?",
          (ts_self, w_open),
      )

    # New ISO human comment arrives after satisfied_at and after draft_updated_ts
    store.upsert_artifact(
        "github_issue:flutter/flutter:198502",
        kind="github_issue",
        state={"last_human_comment_at": _iso(ts_new_human)},
        now=now,
    )
    th_open_art = store.get_artifact("gmail_thread:th_iso_open") or {}
    th_open_st = json.loads(th_open_art["state_json"])
    arts_open = {
        "github_issue:flutter/flutter:198502": {
            "state": {"status": "OPEN", "last_human_comment_at": _iso(ts_new_human)}
        }
    }
    self.assertFalse(work_store._is_correspondence_silence_ping(th_open_st, arts_open))
    self.assertFalse(work_store._is_thread_covered_by_draft_ts(th_open_st, ts_self, arts_open))

    store.reconcile_correspondence_obligations(w_open, now=now)
    proj_open = store.build_dashboard_projection(cl_watcher_state={"items": {}}, now=now)
    card_open = [c for c in proj_open["works"] if c["work_id"] == w_open][0]
    self.assertEqual(card_open["lane"], "needs-you")
    self.assertTrue(any("Reply needed" in r for r in card_open["reasons"]))

    # Merged PR with ISO merged_at respects pre-merge vs post-merge ISO human comment timestamps
    pr_pre_arts = {
        "github_pr:flutter/flutter:198503": {
            "state": {
                "status": "MERGED",
                "merged_at": _iso(ts_closed),
                "last_human_comment_at": _iso(ts_old),
                "last_self_comment_at": _iso(ts_self),
            }
        }
    }
    pr_post_arts = {
        "github_pr:flutter/flutter:198503": {
            "state": {
                "status": "MERGED",
                "merged_at": _iso(ts_closed),
                "last_human_comment_at": _iso(ts_new_human),
                "last_self_comment_at": _iso(ts_self),
            }
        }
    }
    pr_th_st = {
        "is_github_pr_thread": True,
        "gh_target_key": "github_pr:flutter/flutter:198503",
        "last_message_ts": ts_old,
        "last_from_me": False,
    }
    self.assertTrue(work_store.is_github_thread_resolved(pr_th_st, pr_pre_arts))
    self.assertFalse(work_store.is_github_thread_resolved(pr_th_st, pr_post_arts))

  def test_absorb_obligation_covered_duplicate_cl_works(self) -> None:
    """Absorbs orphan auto-created CL Works only when a correspondence Work gates their CLs."""
    import base64
    from unittest import mock
    import ingestor
    import work_store

    store = work_store.WorkStore(self.db_path)
    now = 1762900000.0

    # Pre-existing correspondence Work mirrors live DB where Gmail thread arrived before CL links
    w_renzo = store.create_work("Fixed bugs in Flutter — Renzo Olivares", now=now - 500)
    store.upsert_artifact(
        "gmail_thread:1a13c69d14fe1c3d",
        kind="gmail_thread",
        title="Fixed bugs in Flutter (Renzo Olivares)",
        state={
            "thread_id": "1a13c69d14fe1c3d",
            "root_subject": "Fixed bugs in Flutter",
            "counterparty": "Renzo Olivares",
            "sent_by_me": True,
            "last_from_me": False,
            "last_message_ts": now - 400,
            "is_active_correspondence": True,
        },
        now=now - 500,
    )
    store.link_artifact(
        w_renzo, "gmail_thread:1a13c69d14fe1c3d", role="primary", link_reason="gmail_root", now=now
    )

    # Prerequisite stack parent without its own Bug: trailer (exercises upward stack coverage)
    w_stack = store.create_work(
        "[skparagraph] Keep visual cluster advance non-negative", now=now - 150
    )
    store.upsert_artifact(
        "gerrit:skia:1421196",
        kind="gerrit",
        title="1421196: [skparagraph] Keep visual cluster advance non-negative",
        state={"status": "NEW", "is_mine": True},
        now=now - 150,
    )
    store.link_artifact(
        w_stack, "gerrit:skia:1421196", role="primary", source="auto", now=now - 150
    )

    cl_state = {
        "items": {
            "gerrit:1421196@p1": {
                "item_key": "gerrit:1421196",
                "kind": "gerrit",
                "target_id": "1421196",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "patchset": 2,
                "is_mine": True,
                "subject": "[skparagraph] Keep visual cluster advance non-negative",
                "url": "https://skia-review.googlesource.com/c/skia/+/1421196",
                "commit_message": "Refactor cluster advance helper",
                "entity_state": {"status": "waiting", "lane": "waiting"},
            },
            "gerrit:1404137@c1": {
                "item_key": "gerrit:1404137",
                "kind": "gerrit",
                "target_id": "1404137",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "patchset": 11,
                "is_mine": True,
                "stack_parent_ikey": "gerrit:1421196",
                "subject": "[skparagraph] Do not letter-space default-ignorable clusters",
                "url": "https://skia-review.googlesource.com/c/skia/+/1404137",
                "commit_message": "Bug: https://github.com/flutter/flutter/issues/160251",
                "entity_state": {"status": "waiting", "lane": "waiting"},
            },
            "gerrit:1414017@c2": {
                "item_key": "gerrit:1414017",
                "kind": "gerrit",
                "target_id": "1414017",
                "host": "skia-review.googlesource.com",
                "status": "NEW",
                "patchset": 1,
                "is_mine": True,
                "subject": "[skparagraph] Shift the trailing space of an RTL run when justifying",
                "url": "https://skia-review.googlesource.com/c/skia/+/1414017",
                "commit_message": "Bug: https://github.com/flutter/flutter/issues/84030",
                "entity_state": {"status": "waiting", "lane": "waiting"},
            },
        },
        "bugs": {},
    }
    ingestor.ingest_cl_watcher_state(store, cl_state, now=now - 100)

    w_ignorable = store.find_work_by_artifact("gerrit:skia:1404137", roles=("primary",))
    w_rtl = store.find_work_by_artifact("gerrit:skia:1414017", roles=("primary",))
    self.assertEqual(w_ignorable, w_stack)
    self.assertIsNotNone(w_rtl)
    self.assertNotEqual(w_rtl, w_renzo)
    assert w_ignorable is not None and w_rtl is not None
    store.upsert_artifact(
        "chat_chain:4726af35-fce3-4cea-9fc1-743d0357e8ac",
        kind="chat_chain",
        title="Default-ignorable clusters",
        now=now - 90,
    )
    store.link_artifact(
        w_ignorable,
        "chat_chain:4726af35-fce3-4cea-9fc1-743d0357e8ac",
        role="channel",
        source="auto",
        link_reason="cl_watcher_chat",
        now=now - 90,
    )
    # Automated reviewlog mail and companion probes on S must not block duplicate absorption
    store.upsert_artifact(
        "gmail_thread:th_auto_reviewlog",
        kind="gmail_thread",
        title="Change in skia[main]: 1404137",
        state={"thread_id": "th_auto_reviewlog", "is_automated": True},
        now=now - 88,
    )
    store.link_artifact(
        w_ignorable,
        "gmail_thread:th_auto_reviewlog",
        role="channel",
        source="auto",
        link_reason="gmail_thread_ref",
        now=now - 88,
    )
    store.upsert_artifact(
        "gerrit:skia:1499999",
        kind="gerrit",
        title="1499999: [WIP] companion probe",
        state={"status": "ABANDONED", "is_companion": True, "companion_to": "gerrit:skia:1404137"},
        now=now - 86,
    )
    store.link_artifact(
        w_ignorable,
        "gerrit:skia:1499999",
        role="primary",
        source="auto",
        link_reason="companion_wip",
        now=now - 86,
    )

    # Manually titled Work sharing a gated CL must stay separate even after draft sync
    w_manual = store.create_work(
        "Pinned custom note", meta={"title_manual": True}, now=now - 80
    )
    store.link_artifact(w_manual, "gerrit:skia:1404137", role="primary", source="auto", now=now - 80)

    draft_html = (
        "<p>Hi Renzo,</p><ul>"
        '<li><a href="https://github.com/flutter/flutter/issues/160251">#160251</a> '
        '(<a href="https://skia-review.googlesource.com/c/skia/+/1404137">CL 1404137</a>)</li>'
        '<li><a href="https://github.com/flutter/flutter/issues/84030">#84030</a> '
        '(<a href="https://skia-review.googlesource.com/c/skia/+/1414017">CL 1414017</a>)</li>'
        "</ul>"
    )
    b64_body = base64.urlsafe_b64encode(draft_html.encode("utf-8")).decode("ascii")

    def fake_gmail(cmd, *args, **kwargs):
      del args, kwargs
      res = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
      if "drafts" in cmd:
        res.stdout = json.dumps(
            {"drafts": [{"id": "r_renzo_dup", "threadId": "1a13c69d14fe1c3d"}]}
        )
      elif "get-draft" in cmd:
        res.stdout = json.dumps({
            "id": "r_renzo_dup",
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
                "internalDate": str(int((now - 400) * 1000)),
                "snippet": "Fixed bugs in Flutter",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Fixed bugs in Flutter"},
                        {"name": "From", "value": "Renzo Olivares <roliv@google.com>"},
                        {"name": "To", "value": "Julia Lavrova <jlavrova@google.com>"},
                    ]
                },
            }]
        })
      else:
        res.stdout = json.dumps({"messages": []})
      return res

    with mock.patch("os.path.isfile", return_value=True), mock.patch(
        "subprocess.run", side_effect=fake_gmail
    ):
      ingestor.ingest_gmail_drafts_and_threads(store, {}, now=now)

    active_ids = {w["work_id"] for w in store.list_works()}
    self.assertIn(w_renzo, active_ids)
    self.assertNotIn(w_ignorable, active_ids)
    self.assertNotIn(w_rtl, active_ids)
    self.assertIn(w_manual, active_ids)
    renzo_roles = store.get_work_artifact_roles(w_renzo)
    self.assertEqual(renzo_roles.get("gerrit:skia:1421196"), "primary")
    self.assertEqual(
        renzo_roles.get("chat_chain:4726af35-fce3-4cea-9fc1-743d0357e8ac"), "channel"
    )

    # Splitting a CL out of the consolidated card must keep the split card alive across syncs
    w_split = store.split_work(w_renzo, "gerrit:skia:1414017", "Standalone RTL CL", now=now + 10)
    store.reconcile_correspondence_obligations(now=now + 15)
    self.assertIn(w_split, {w["work_id"] for w in store.list_works()})

    # Passive reference GDoc without active correspondence must never absorb standalone CL cards
    store.upsert_artifact(
        "gdoc:1PassiveSpecDocId1234567890",
        kind="gdoc",
        title="Design Spec",
        state={"doc_title": "Design Spec"},
        now=now + 16,
    )
    store.upsert_artifact(
        "gerrit:skia:1430999",
        kind="gerrit",
        title="1430999: Spec implementation",
        state={"status": "NEW", "is_mine": True},
        now=now + 16,
    )
    w_cl_spec = store.create_work("1430999: Spec implementation", now=now + 16)
    store.link_artifact(
        w_cl_spec, "gerrit:skia:1430999", role="primary", source="auto", now=now + 16
    )
    w_doc_only = store.create_work("Design Spec card", now=now + 16)
    store.link_artifact(
        w_doc_only, "gdoc:1PassiveSpecDocId1234567890", role="primary", now=now + 16
    )
    store.link_artifact(w_doc_only, "gerrit:skia:1430999", role="primary", now=now + 16)
    store.add_obligation(
        w_doc_only,
        "Verify spec after merge",
        trigger_expr="cl_merged(gerrit:skia:1430999)",
        now=now + 17,
    )
    self.assertIn(w_cl_spec, {w["work_id"] for w in store.list_works()})

    # Ungated correspondence never merges a CL card until an auto-gated reply draft gates that CL
    store.upsert_artifact(
        "gerrit:skia:1431001",
        kind="gerrit",
        title="1431001: Ungated until draft",
        state={"status": "NEW", "is_mine": True},
        now=now + 20,
    )
    w_dup_wb = store.create_work("1431001: Ungated until draft", now=now + 20)
    store.link_artifact(w_dup_wb, "gerrit:skia:1431001", role="primary", source="auto", now=now + 20)
    w_corr_wb = store.create_work("Flutter follow-up — Peer", now=now + 20)
    store.upsert_artifact(
        "gmail_thread:th_wb_gate",
        kind="gmail_thread",
        title="Flutter follow-up (Peer)",
        state={
            "thread_id": "th_wb_gate",
            "root_subject": "Flutter follow-up",
            "counterparty": "Peer",
            "sent_by_me": True,
            "last_from_me": True,
            "last_message_ts": now + 15,
            "is_active_correspondence": True,
        },
        now=now + 20,
    )
    store.link_artifact(w_corr_wb, "gmail_thread:th_wb_gate", role="channel", now=now + 20)
    store.link_artifact(w_corr_wb, "gerrit:skia:1431001", role="primary", now=now + 20)
    store.reconcile_correspondence_obligations(now=now + 21)
    self.assertIn(w_dup_wb, {w["work_id"] for w in store.list_works()})

    store.save_reply_draft(
        w_corr_wb, "<p>Will update once 1431001 lands</p>", auto_gate=True, now=now + 25
    )
    self.assertNotIn(w_dup_wb, {w["work_id"] for w in store.list_works()})

    # Scoped reconciliation on a newly minted duplicate source Work must still absorb it into T
    w_late_src = store.create_work("1431001: Late transcript card", now=now + 26)
    store.link_artifact(
        w_late_src, "gerrit:skia:1431001", role="primary", source="auto", now=now + 26
    )
    store.reconcile_correspondence_obligations(w_late_src, now=now + 27)
    self.assertNotIn(w_late_src, {w["work_id"] for w in store.list_works()})

    # bug_closed(...) obligation on correspondence Work must absorb auto CL Work fixing that bug
    store.upsert_artifact(
        "github_issue:flutter/flutter:99901",
        kind="github_issue",
        title="flutter#99901: Line break bug",
        state={"status": "OPEN"},
        now=now + 30,
    )
    store.upsert_artifact(
        "gerrit:skia:1432002",
        kind="gerrit",
        title="1432002: Fix line break bug",
        state={
            "status": "NEW",
            "is_mine": True,
            "commit_message": "Bug: https://github.com/flutter/flutter/issues/99901",
        },
        now=now + 30,
    )
    w_bug_corr = store.create_work("Bug 99901 thread — Peer", now=now + 30)
    store.link_artifact(w_bug_corr, "gmail_thread:th_wb_gate", role="channel", now=now + 30)
    store.link_artifact(
        w_bug_corr, "github_issue:flutter/flutter:99901", role="primary", now=now + 30
    )
    store.link_artifact(w_bug_corr, "gerrit:skia:1432002", role="primary", now=now + 30)
    w_dup_bug = store.create_work("1432002: Fix line break bug", now=now + 30)
    store.link_artifact(
        w_dup_bug, "gerrit:skia:1432002", role="primary", source="auto", now=now + 30
    )
    store.link_artifact(
        w_dup_bug, "github_issue:flutter/flutter:99901", role="primary", source="auto", now=now + 30
    )
    store.add_obligation(
        w_bug_corr,
        "Reply once #99901 closes",
        trigger_expr="bug_closed(github_issue:flutter/flutter:99901)",
        now=now + 31,
    )
    self.assertNotIn(w_dup_bug, {w["work_id"] for w in store.list_works()})

  # Verify approved self CLs surface Ready to submit in Needs You, chat_mention never downgrades
  # primary links, subject-scoped emails demote unrelated body CLs, and flutter#184815 splits out
  def test_r18_ready_to_submit_lane_chat_mention_guard_and_subject_email_isolation(self) -> None:
    scripts_dir = os.path.expanduser("~/.gemini/config/scripts")
    if scripts_dir not in sys.path:
      sys.path.insert(0, scripts_dir)
    from unittest import mock
    import entity_state as es
    import ingestor
    import work_store

    now = 1_794_331_000.0
    store = work_store.WorkStore(self.db_path)

    ready_item = {
        "item_key": "gerrit:1422517",
        "kind": "gerrit",
        "target_id": "1422517",
        "project_name": "Skia",
        "status": "NEW",
        "patchset": 2,
        "is_self": True,
        "is_wip": False,
        "phase": "WAITING_ON_OTHERS",
        "in_my_attention": False,
        "approved_count": 1,
        "missing_approvals": 0,
        "cq_status": "✓ Checks green",
        "open_external_threads": 0,
        "subject": 'Reland "[skparagraph] Cap letter spacing line shift"',
        "url": "https://skia-review.googlesource.com/c/skia/+/1422517",
    }
    ready_item["entity_state"] = es.compute_item_entity_state(ready_item, now_ts=now)

    reply_item = {
        "item_key": "gerrit:1409637",
        "kind": "gerrit",
        "target_id": "1409637",
        "project_name": "Skia",
        "status": "NEW",
        "patchset": 4,
        "is_self": True,
        "is_wip": False,
        "phase": "NEEDS_YOUR_ACTION",
        "in_my_attention": True,
        "approved_count": 1,
        "missing_approvals": 0,
        "cq_status": "✓ Checks green",
        "open_external_threads": 1,
        "attention_reason": "Added by <GERRIT_ACCOUNT_5283275> using the hovercard menu",
        "last_msg_author_name": "Miyoung Shin",
        "subject": "Handle bidi runs",
        "url": "https://skia-review.googlesource.com/c/skia/+/1409637",
    }
    reply_item["entity_state"] = es.compute_item_entity_state(reply_item, now_ts=now)

    proj_cl = store.build_dashboard_projection(
        cl_watcher_state={
            "items": {
                "gerrit:1422517": ready_item,
                "gerrit:1409637": reply_item,
            }
        },
        now=now,
    )
    by_virt_id = {w["work_id"]: w for w in proj_cl["works"]}
    card_1422517 = by_virt_id["virt-gerrit:skia:1422517"]
    self.assertEqual(card_1422517["lane"], "needs-you")
    self.assertIn("🚀 Ready to submit: Gerrit 1422517 PS2", card_1422517["reasons"])

    card_1409637 = by_virt_id["virt-gerrit:skia:1409637"]
    self.assertEqual(card_1409637["lane"], "needs-you")
    for r_txt in card_1409637["reasons"]:
      self.assertNotIn("<GERRIT_ACCOUNT_", r_txt)
      self.assertNotIn("Ready to submit", r_txt)

    # chat_mention must never downgrade an existing primary link to context_only
    w_guard = store.create_work("1418376: Cap letter spacing", now=now)
    store.upsert_artifact(
        "gerrit:skia:1418376",
        kind="gerrit",
        title="1418376: [skparagraph] Cap letter spacing line shift",
        state={
            "status": "MERGED",
            "is_mine": True,
            "commit_message": "Bug: https://github.com/flutter/flutter/issues/184815",
        },
        now=now,
    )
    store.link_artifact(
        w_guard,
        "gerrit:skia:1418376",
        role="primary",
        source="auto",
        link_reason="bug_trailer",
        now=now,
    )
    role_after_mention = store.link_artifact(
        w_guard,
        "gerrit:skia:1418376",
        role="context_only",
        source="auto",
        link_reason="chat_mention",
        now=now + 10,
    )
    self.assertEqual(role_after_mention, "primary")
    self.assertEqual(store.get_work_artifact_roles(w_guard)["gerrit:skia:1418376"], "primary")

    # Subject-scoped revert email ("Please revert Skia CL 1415856") must not hijack unrelated
    # self-authored body CL 1418376 even before 1418376 has a Work owner
    store2_path = self.db_path + ".subj_iso"
    store2 = work_store.WorkStore(store2_path)
    store2.upsert_artifact(
        "gerrit:skia:1415856",
        kind="gerrit",
        title="1415856: Swizzler SIMD tweak",
        state={"status": "MERGED", "is_mine": True, "commit_message": "Bug: b/400000111"},
        now=now,
    )
    store2.upsert_artifact(
        "gerrit:skia:1418376",
        kind="gerrit",
        title="1418376: [skparagraph] Cap letter spacing line shift",
        state={
            "status": "MERGED",
            "is_mine": True,
            "commit_message": "Bug: https://github.com/flutter/flutter/issues/184815",
        },
        now=now,
    )

    def _b64(s: str) -> str:
      return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")

    def fake_jorge_gmail(cmd, *args, **kwargs):
      del args, kwargs
      res = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
      if "drafts" in cmd:
        res.stdout = json.dumps({"drafts": []})
      elif "search" in cmd:
        q = " ".join(cmd)
        if "from:me" in q:
          res.stdout = json.dumps({"messages": [{"id": "mj1", "threadId": "1a1ae0e07e44a362"}]})
        else:
          res.stdout = json.dumps({"messages": []})
      elif "get-thread" in cmd:
        res.stdout = json.dumps({
            "messages": [
                {
                    "internalDate": str(int((now - 3600) * 1000)),
                    "snippet": "Please revert Skia CL 1415856",
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [
                            {
                                "name": "Subject",
                                "value": "Please revert Skia CL 1415856 (breaks ASAN/UBSan bots)",
                            },
                            {"name": "From", "value": "Jorge Betancourt <jmbetancourt@google.com>"},
                            {"name": "To", "value": "Julia Lavrova <jlavrova@google.com>"},
                        ],
                        "body": {
                            "data": _b64(
                                "Hi Julia, https://skia-review.googlesource.com/c/skia/+/1415856 "
                                "broke ASAN."
                            )
                        },
                    },
                },
                {
                    "internalDate": str(int((now - 1800) * 1000)),
                    "snippet": "Sorry, I was focused on 1418376",
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [
                            {
                                "name": "Subject",
                                "value": (
                                    "Re: Please revert Skia CL 1415856 (breaks ASAN/UBSan bots)"
                                ),
                            },
                            {"name": "From", "value": "Julia Lavrova <jlavrova@google.com>"},
                            {"name": "To", "value": "Jorge Betancourt <jmbetancourt@google.com>"},
                        ],
                        "body": {
                            "data": _b64(
                                "Sorry, I was landing "
                                "https://skia-review.googlesource.com/c/skia/+/1418376 first."
                            )
                        },
                    },
                },
            ]
        })
      return res

    with (
        mock.patch("os.path.isfile", return_value=True),
        mock.patch("subprocess.run", side_effect=fake_jorge_gmail),
    ):
      ingestor.ingest_gmail_drafts_and_threads(store2, {}, now=now)

    jorge_wid = store2.find_work_by_artifact("gmail_thread:1a1ae0e07e44a362", roles=("primary",))
    self.assertIsNotNone(jorge_wid)
    jorge_roles = store2.get_work_artifact_roles(jorge_wid or "")
    self.assertEqual(jorge_roles.get("gerrit:skia:1415856"), "primary")
    self.assertEqual(jorge_roles.get("gerrit:skia:1418376"), "context_only")

    # Polluted Jorge card splits flutter#184815 artifacts + Mouad PR thread into work-184815-r18
    store3 = work_store.WorkStore(self.db_path + ".split_184815")
    w_polluted = store3.create_work(
        "Please revert Skia CL 1415856 (breaks ASAN/UBSan bots on main) — Jorge Betancourt",
        work_id="work-189f43b331",
        now=now,
    )
    for ak, kind, title in [
        ("gmail_thread:1a1ae0e07e44a362", "gmail_thread", "Please revert Skia CL 1415856"),
        ("gmail_thread:1a1b3655788740e1", "gmail_thread", "[flutter/flutter] PR #196148"),
        ("gerrit:skia:1415856", "gerrit", "1415856: Swizzler"),
        ("gerrit:skia:1418376", "gerrit", "1418376: [skparagraph] Cap letter spacing line shift"),
        ("gerrit:skia:1420816", "gerrit", "1420816: Revert Cap letter spacing"),
        ("gerrit:skia:1422517", "gerrit", '1422517: Reland "[skparagraph] Cap letter spacing"'),
        ("github_issue:flutter/flutter:184815", "github_issue", "flutter#184815: letterSpacing"),
        ("github_pr:flutter/flutter:196148", "github_pr", "PR #196148: Accept capped line shift"),
        ("chat_chain:78507fdf-d92f-4b37-9daa-7f6078a1ead4", "chat_chain", "Chat 1418376"),
        ("github_pr:flutter/flutter:196295", "github_pr", "PR #196295"),
    ]:
      store3.upsert_artifact(ak, kind=kind, title=title, now=now)
    store3.link_artifact(
        w_polluted, "gmail_thread:1a1ae0e07e44a362", role="primary", link_reason="gmail_root"
    )
    store3.link_artifact(
        w_polluted, "gerrit:skia:1415856", role="primary", link_reason="gmail_thread_ref"
    )
    store3.link_artifact(
        w_polluted, "gerrit:skia:1418376", role="primary", link_reason="gmail_thread_ref"
    )
    store3.link_artifact(
        w_polluted, "gerrit:skia:1420816", role="primary", link_reason="bug_trailer"
    )
    store3.link_artifact(
        w_polluted, "gerrit:skia:1422517", role="primary", link_reason="bug_trailer"
    )
    store3.link_artifact(
        w_polluted, "github_issue:flutter/flutter:184815", role="primary", link_reason="bug_trailer"
    )
    store3.link_artifact(
        w_polluted, "github_pr:flutter/flutter:196148", role="primary", link_reason="bug_trailer"
    )
    store3.link_artifact(
        w_polluted,
        "gmail_thread:1a1b3655788740e1",
        role="channel",
        link_reason="github_pr_thread",
    )
    store3.link_artifact(
        w_polluted,
        "chat_chain:78507fdf-d92f-4b37-9daa-7f6078a1ead4",
        role="channel",
        link_reason="cl_watcher_chat",
    )
    store3.link_artifact(
        w_polluted,
        "github_pr:flutter/flutter:196295",
        role="context_only",
        link_reason="chat_mention",
        link_detail="Попутно упомянут в чате 3d1e2bf2 (шаг 391)",
    )
    ob_id = store3.add_obligation(
        w_polluted,
        "Close loop PR #196148",
        trigger_expr="correspondence_turn(gmail_thread:1a1b3655788740e1, 7)",
        target_artifact_key="gmail_thread:1a1b3655788740e1",
        now=now,
    )

    store3.purge_config_artifacts_and_works(now=now + 60)

    roles_jorge = store3.get_work_artifact_roles("work-189f43b331")
    roles_184815 = store3.get_work_artifact_roles("work-184815-r18")
    self.assertEqual(roles_jorge.get("gerrit:skia:1415856"), "primary")
    self.assertEqual(roles_jorge.get("gerrit:skia:1418376"), "context_only")
    self.assertNotIn("gerrit:skia:1422517", roles_jorge)
    self.assertNotIn("github_issue:flutter/flutter:184815", roles_jorge)
    self.assertNotIn("gmail_thread:1a1b3655788740e1", roles_jorge)
    self.assertNotIn("github_pr:flutter/flutter:196295", roles_jorge)

    self.assertEqual(roles_184815.get("github_issue:flutter/flutter:184815"), "primary")
    self.assertEqual(roles_184815.get("gerrit:skia:1418376"), "primary")
    self.assertEqual(roles_184815.get("gerrit:skia:1420816"), "primary")
    self.assertEqual(roles_184815.get("gerrit:skia:1422517"), "primary")
    self.assertEqual(roles_184815.get("github_pr:flutter/flutter:196148"), "primary")
    self.assertEqual(roles_184815.get("gmail_thread:1a1b3655788740e1"), "channel")
    self.assertEqual(
        roles_184815.get("chat_chain:78507fdf-d92f-4b37-9daa-7f6078a1ead4"), "channel"
    )
    self.assertEqual(roles_184815.get("github_pr:flutter/flutter:196295"), "context_only")
    ob_moved = store3.get_obligation(ob_id, now=now + 60)
    self.assertIsNotNone(ob_moved)
    self.assertEqual((ob_moved or {}).get("work_id"), "work-184815-r18")

    # Stale pre-merge entity_state on MERGED PR must not leak Ready to submit, while partial
    # entity_state={"needs_self_submit": True} on an OPEN self CL surfaces Ready to submit
    self.assertEqual(
        work_store._evaluate_cl_needs_you(
            "github_pr:flutter/flutter:196148",
            {
                "status": "MERGED",
                "is_mine": True,
                "entity_state": {
                    "input_sig": "OPEN|WAITING_ON_OTHERS|0",
                    "lane": "you",
                    "action_required": True,
                    "needs_self_submit": True,
                    "spine_short_state": {"short": "Ready to submit"},
                },
            },
        ),
        "",
    )
    self.assertEqual(
        work_store._evaluate_cl_needs_you(
            "gerrit:skia:1422517",
            {
                "status": "NEW",
                "is_mine": True,
                "patchset": 2,
                "entity_state": {"needs_self_submit": True},
            },
        ),
        "🚀 Ready to submit: Gerrit 1422517 PS2",
    )

    # Auto context_only with matching gmail_thread_ref or empty prev_reason never downgrades
    w_same_reason = store.create_work("1419757: Revert Swizzler", now=now)
    store.upsert_artifact("gerrit:skia:1419757", kind="gerrit", title="1419757", now=now)
    store.link_artifact(
        w_same_reason,
        "gerrit:skia:1419757",
        role="primary",
        source="auto",
        link_reason="gmail_thread_ref",
        now=now,
    )
    self.assertEqual(
        store.link_artifact(
            w_same_reason,
            "gerrit:skia:1419757",
            role="context_only",
            source="auto",
            link_reason="gmail_thread_ref",
            now=now + 5,
        ),
        "primary",
    )

    # "Chromium CL 8663938" extracts gerrit:chromium:8663938 without phantom gerrit:skia
    draft_keys = work_store._extract_draft_mentioned_keys("Please land Chromium CL 8663938 first")
    self.assertIn("gerrit:chromium:8663938", draft_keys)
    self.assertNotIn("gerrit:skia:8663938", draft_keys)

    # Bot last_msg_author_name must not impersonate human <GERRIT_ACCOUNT_N> reviewer
    sanitized_bot = work_store._sanitize_gerrit_account_placeholders(
        "<GERRIT_ACCOUNT_5283275> replied on the change",
        {"last_msg_author_name": "Skia Commit-Bot"},
    )
    self.assertEqual(sanitized_bot, "account #5283275 replied on the change")

    # Unhydrated stub body CL is still demoted on subject-scoped email, whereas stacked
    # parent CL referenced by subject child's stack_parent_ikey stays primary even with w_owner
    store4 = work_store.WorkStore(self.db_path + ".stub_and_stack")
    w_parent = store4.create_work("1422510: Base CL", now=now)
    store4.upsert_artifact(
        "gerrit:skia:1422510",
        kind="gerrit",
        title="1422510: Base CL",
        state={"status": "NEW", "is_mine": True},
        now=now,
    )
    store4.link_artifact(w_parent, "gerrit:skia:1422510", role="primary", now=now)
    store4.upsert_artifact(
        "gerrit:skia:1422518",
        kind="gerrit",
        title="1422518: Child CL",
        state={
            "status": "NEW",
            "is_mine": True,
            "stack_parent_ikey": "gerrit:1422510",
            "stack_ancestors": ["gerrit:1422510"],
        },
        now=now,
    )
    store4.upsert_artifact("gerrit:skia:1499999", kind="gerrit", title="1499999", state={}, now=now)

    def fake_stack_gmail(cmd, *args, **kwargs):
      del args, kwargs
      res = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
      if "drafts" in cmd:
        res.stdout = json.dumps({"drafts": []})
      elif "search" in cmd:
        msgs = [{"id": "ms1", "threadId": "th_stack"}] if "from:me" in " ".join(cmd) else []
        res.stdout = json.dumps({"messages": msgs})
      elif "get-thread" in cmd:
        res.stdout = json.dumps({
            "messages": [{
                "internalDate": str(int((now - 600) * 1000)),
                "snippet": "Child CL 1422518",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "Subject", "value": "Question on Skia CL 1422518"},
                        {"name": "From", "value": "Florin Malita <fmalita@google.com>"},
                        {"name": "To", "value": "Julia Lavrova <jlavrova@google.com>"},
                    ],
                    "body": {
                        "data": _b64(
                            "Depends on https://skia-review.googlesource.com/c/skia/+/1422510 ; "
                            "unrelated https://skia-review.googlesource.com/c/skia/+/1499999 ."
                        )
                    },
                },
            }]
        })
      return res

    with (
        mock.patch("os.path.isfile", return_value=True),
        mock.patch("subprocess.run", side_effect=fake_stack_gmail),
    ):
      ingestor.ingest_gmail_drafts_and_threads(store4, {}, now=now)
    th_wid = store4.find_work_by_artifact("gmail_thread:th_stack", roles=("primary", "channel"))
    self.assertEqual(th_wid, w_parent)
    th_roles = store4.get_work_artifact_roles(w_parent)
    self.assertEqual(th_roles.get("gerrit:skia:1422510"), "primary")
    self.assertEqual(th_roles.get("gerrit:skia:1422518"), "primary")
    self.assertEqual(th_roles.get("gerrit:skia:1499999"), "context_only")

    # Split preserves existing snooze_until on work-184815-r18 and moves 1422517 chat
    with store3._connect() as conn:
      conn.execute(
          "UPDATE works SET snooze_until = ? WHERE work_id = ?",
          (now + 3600, "work-184815-r18"),
      )
    store3.upsert_artifact(
        "chat_chain:99991848-1111-2222-3333-444455556666",
        kind="chat_chain",
        title="Gerrit 1422517 reland",
        now=now + 70,
    )
    store3.link_artifact(
        "work-189f43b331",
        "gerrit:skia:1418376",
        role="primary",
        source="manual",
        now=now + 70,
    )
    store3.link_artifact(
        "work-189f43b331",
        "chat_chain:99991848-1111-2222-3333-444455556666",
        role="channel",
        link_reason="cl_watcher_chat",
        link_detail="Рабочий чат по СЛ 1422517",
        now=now + 70,
    )
    store3.purge_config_artifacts_and_works(now=now + 80)
    w_split_after = {w["work_id"]: w for w in store3.list_works()}.get("work-184815-r18") or {}
    self.assertEqual(float(w_split_after.get("snooze_until") or 0.0), now + 3600)
    self.assertEqual(
        store3.get_work_artifact_roles("work-184815-r18").get(
            "chat_chain:99991848-1111-2222-3333-444455556666"
        ),
        "channel",
    )




if __name__ == "__main__":
  unittest.main()
