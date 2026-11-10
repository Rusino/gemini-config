#!/usr/bin/env python3
"""Command-line interface for Workbench work graph and obligation management."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

WORKBENCH_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKBENCH_DIR not in sys.path:
  sys.path.insert(0, WORKBENCH_DIR)

import ingestor  # pylint: disable=g-import-not-at-top
import work_store  # pylint: disable=g-import-not-at-top

ROADMAP_SCRIPT = os.path.expanduser("~/.gemini/config/hooks/roadmap.py")


def _default_url_for_key(akey: str) -> str:
  c_key = work_store.canonicalize_artifact_key(akey)
  return work_store._default_artifact_url(c_key)


def _roadmap_writeback(proj_id: str, item_id: str) -> None:
  if not os.path.isfile(ROADMAP_SCRIPT):
    return
  conv_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID") or proj_id
  subprocess.run(
      [sys.executable, ROADMAP_SCRIPT, "set", conv_id, item_id, "done"],
      capture_output=True,
      text=True,
      timeout=5,
      check=False,
  )


def main(argv: list[str] | None = None) -> int:
  """Parses Workbench CLI subcommands, mutates or queries WorkStore, and returns an exit code."""
  parser = argparse.ArgumentParser(description="Workbench CLI")
  parser.add_argument("--db", default="", help="Override SQLite DB path")
  sub = parser.add_subparsers(dest="cmd", required=True)

  p_new = sub.add_parser("new", help="Create a new Work")
  p_new.add_argument("title")
  p_new.add_argument("--project", default="")
  p_new.add_argument("--roadmap", default="")
  p_new.add_argument("--primary", action="append", default=[])
  p_new.add_argument("--channel", action="append", default=[])

  p_pin = sub.add_parser("pin", help="Pin current conversation to a Work")
  p_pin.add_argument("title")
  p_pin.add_argument("--conv", default="")
  p_pin.add_argument("--project", default="")

  p_link = sub.add_parser("link", help="Link an artifact to a Work")
  p_link.add_argument("work_id")
  p_link.add_argument("artifact_key")
  p_link.add_argument("--role", default="primary", choices=["primary", "channel", "context_only"])
  p_link.add_argument("--title", default="")
  p_link.add_argument("--url", default="")

  p_unlink = sub.add_parser("unlink", help="Detach an artifact and record tombstone")
  p_unlink.add_argument("work_id")
  p_unlink.add_argument("artifact_key")

  p_oblige = sub.add_parser("oblige", help="Add a reactive obligation to a Work")
  p_oblige.add_argument("work_id")
  p_oblige.add_argument("description")
  p_oblige.add_argument("--when", default="always")
  p_oblige.add_argument("--target", default="")
  p_oblige.add_argument("--done-matcher", default="")

  p_done = sub.add_parser("done", help="Mark a Work done")
  p_done.add_argument("work_id")

  p_merge = sub.add_parser("merge", help="Merge source Work into target Work")
  p_merge.add_argument("target_work_id")
  p_merge.add_argument("source_work_id")

  p_split = sub.add_parser("split", help="Split an artifact into a new Work")
  p_split.add_argument("work_id")
  p_split.add_argument("artifact_key")
  p_split.add_argument("--title", required=True)

  p_list = sub.add_parser("list", help="List Works")
  p_list.add_argument("--lane", default="")
  p_list.add_argument("--json", action="store_true")

  p_metrics = sub.add_parser("metrics", help="Show 14-day evaluation metrics")
  p_metrics.add_argument("--days", type=int, default=14)
  p_metrics.add_argument("--json", action="store_true")

  p_dismiss = sub.add_parser("dismiss", help="Dismiss a thread from Unanswered Human Threads")
  p_dismiss.add_argument("artifact_key")

  p_wait = sub.add_parser("wait", help="Mark a correspondence obligation answered & waiting")
  p_wait.add_argument("obligation_id")

  p_ambig = sub.add_parser("resolve-ambiguity", help="Resolve borderline obligation closure")
  p_ambig.add_argument("obligation_id")
  p_ambig.add_argument("decision", choices=["keep", "close"])
  p_ambig.add_argument("--event-key", default="")

  p_dset = sub.add_parser("draft-set", help="Save or update a live reply draft on a Work")
  p_dset.add_argument("work_id")
  body_grp = p_dset.add_mutually_exclusive_group()
  body_grp.add_argument("--body", default="")
  body_grp.add_argument("--body-file", default="")
  fmt_grp = p_dset.add_mutually_exclusive_group()
  fmt_grp.add_argument("--html", action="store_true")
  fmt_grp.add_argument("--text", action="store_true")
  p_dset.add_argument("--thread", default="")
  p_dset.add_argument("--to", default="")
  p_dset.add_argument("--cc", default="")
  p_dset.add_argument("--subject", default="")
  p_dset.add_argument("--when", default="")
  ag_grp = p_dset.add_mutually_exclusive_group()
  ag_grp.add_argument("--auto-gate", dest="auto_gate", action="store_true")
  ag_grp.add_argument("--no-auto-gate", dest="auto_gate", action="store_false")
  p_dset.set_defaults(auto_gate=None)

  p_dshow = sub.add_parser("draft-show", help="Show the active live reply draft on a Work")
  p_dshow.add_argument("work_id")
  p_dshow.add_argument("--json", action="store_true")

  p_dclear = sub.add_parser("draft-clear", help="Clear the live reply draft on a Work")
  p_dclear.add_argument("work_id")

  p_dmat = sub.add_parser("draft-materialize", help="Create a Gmail draft from Work's reply draft")
  p_dmat.add_argument("work_id")
  p_dmat.add_argument("--force-new", action="store_true")
  p_dmat.add_argument("--json", action="store_true")

  p_schat = sub.add_parser("start-chat", help="Start or reuse a Jetski chat for a Work card")
  p_schat.add_argument("work_id")
  p_schat.add_argument("--force-new", action="store_true")
  p_schat.add_argument("--cl-state", default=ingestor.DEFAULT_CL_WATCHER_STATE)
  p_schat.add_argument("--json", action="store_true")

  args = parser.parse_args(argv)
  store = work_store.WorkStore(args.db or None)

  if args.cmd == "new":
    wid = store.create_work(
        args.title,
        project_id=args.project,
        roadmap_item_key=args.roadmap,
        meta={"title_manual": True},
    )
    for pkey in args.primary:
      ck = work_store.canonicalize_artifact_key(pkey)
      store.upsert_artifact(ck, title=ck, url=_default_url_for_key(ck))
      store.link_artifact(wid, ck, role="primary", source="manual")
    for idx, chkey in enumerate(args.channel):
      ck = work_store.canonicalize_artifact_key(chkey)
      ex_art = store.get_artifact(ck)
      ex_st = (ex_art or {}).get("state") or {}
      extra_st = work_store.manual_correspondence_state(
          ck, ex_st, is_channel_slot=True
      )
      ch_title = args.title if (not args.primary and idx == 0 and not ex_art) else ck
      ch_role = "primary" if (not args.primary and idx == 0) else "channel"
      store.upsert_artifact(
          ck, title=ch_title, url=_default_url_for_key(ck), state=extra_st
      )
      store.link_artifact(wid, ck, role=ch_role, source="manual")
    store.reconcile_correspondence_obligations(wid)
    print(wid)
    return 0

  if args.cmd == "pin":
    cid = args.conv or os.environ.get("ANTIGRAVITY_CONVERSATION_ID") or "local"
    ch_key = f"chat_chain:{cid}"
    wid = store.find_work_by_artifact(ch_key)
    if not wid:
      wid = store.create_work(args.title, project_id=args.project)
    store.upsert_artifact(ch_key, kind="chat_chain", title=args.title, url=f"/c/{cid}")
    store.link_artifact(wid, ch_key, role="channel", source="manual")
    print(wid)
    return 0

  if args.cmd == "link":
    ck = work_store.canonicalize_artifact_key(args.artifact_key)
    ex_st = ((store.get_artifact(ck) or {}).get("state")) or {}
    extra_st = work_store.manual_correspondence_state(
        ck, ex_st, is_channel_slot=(args.role == "channel")
    )
    store.upsert_artifact(
        ck,
        title=args.title or ck,
        url=args.url or _default_url_for_key(ck),
        state=extra_st,
    )
    store.link_artifact(args.work_id, ck, role=args.role, source="manual")
    store.reconcile_correspondence_obligations(args.work_id)
    print(ck)
    return 0

  if args.cmd == "unlink":
    store.unlink_artifact(args.work_id, args.artifact_key)
    return 0

  if args.cmd == "oblige":
    ob_id = store.add_obligation(
        args.work_id,
        description=args.description,
        trigger_expr=args.when,
        target_artifact_key=args.target,
        done_matcher=args.done_matcher,
    )
    print(ob_id)
    return 0

  if args.cmd == "done":
    store.complete_work(args.work_id, roadmap_cb=_roadmap_writeback)
    return 0

  if args.cmd == "merge":
    store.merge_works(args.target_work_id, args.source_work_id)
    return 0

  if args.cmd == "split":
    new_wid = store.split_work(args.work_id, args.artifact_key, args.title)
    print(new_wid)
    return 0

  if args.cmd == "list":
    proj = store.build_dashboard_projection()
    works = proj["works"]
    if args.lane:
      works = [w for w in works if w["lane"] == args.lane]
    if args.json:
      print(json.dumps(works, indent=2, ensure_ascii=False))
    else:
      for w in works:
        print(f"[{w['lane']}] {w['work_id']}  {w['title']}  ({len(w['artifacts'])} artifacts)")
    return 0

  if args.cmd == "metrics":
    m = store.compute_metrics(days=args.days)
    if args.json:
      print(json.dumps(m, indent=2))
    else:
      for k, v in m.items():
        print(f"{k}: {v}")
    return 0

  if args.cmd == "dismiss":
    print(store.dismiss_tray_thread(args.artifact_key))
    return 0

  if args.cmd == "wait":
    store.mark_obligation_waiting(args.obligation_id)
    return 0

  if args.cmd == "resolve-ambiguity":
    store.resolve_obligation_ambiguity(
        args.obligation_id,
        decision=args.decision,
        event_key=args.event_key,
    )
    return 0

  # Persist evolving reply notes inside Workbench without mutating Gmail on intermediate steps
  if args.cmd == "draft-set":
    body_str = args.body
    if args.body_file:
      with open(args.body_file, "r", encoding="utf-8") as f:
        body_str = f.read()
    if args.html:
      fmt_val = "html"
    elif args.text:
      fmt_val = "text"
    else:
      has_html_tag = bool(
          re.search(
              r"</?(?:p|div|ul|ol|li|br|pre|a|b|strong|i|em|code|blockquote|h[1-6])\b",
              body_str,
              re.IGNORECASE,
          )
      )
      fmt_val = "html" if has_html_tag else "text"
    # Extract referenced CLs/bugs for checklist coverage without promoting them to primary links
    m_keys = ingestor.extract_draft_mentioned_keys(args.subject, body_str)
    saved = store.save_reply_draft(
        args.work_id,
        body_str,
        fmt=fmt_val,
        thread_key=args.thread,
        to=args.to,
        cc=args.cc,
        subject=args.subject,
        trigger_expr=args.when,
        auto_gate=args.auto_gate,
        mentioned_keys=m_keys,
    )
    print(json.dumps(saved, ensure_ascii=False))
    return 0

  if args.cmd == "draft-show":
    rd = store.get_reply_draft(args.work_id)
    if rd is None:
      print("{}" if args.json else "No active reply draft")
      return 0
    if args.json:
      print(json.dumps(rd, indent=2, ensure_ascii=False))
    else:
      print(rd.get("body") or "")
    return 0

  if args.cmd == "draft-clear":
    store.clear_reply_draft(args.work_id)
    return 0

  if args.cmd == "draft-materialize":
    res = ingestor.materialize_reply_draft(
        store, args.work_id, force_new=bool(args.force_new)
    )
    if args.json:
      print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
      print(f"{res['draft_key']}  {res['url']}")
    return 0

  if args.cmd == "start-chat":
    res = ingestor.start_chat_for_work(
        store,
        args.work_id,
        cl_state_path=args.cl_state,
        force_new=bool(args.force_new),
    )
    if args.json:
      print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
      print(f"{res['conversation_id']}  {res['chat_url']}")
    return 0

  return 0


if __name__ == "__main__":
  sys.exit(main())
