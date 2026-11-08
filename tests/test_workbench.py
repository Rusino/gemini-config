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


if __name__ == "__main__":
  unittest.main()
