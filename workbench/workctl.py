#!/usr/bin/env python3
"""Command-line interface for Workbench work graph and obligation management."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

WORKBENCH_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKBENCH_DIR not in sys.path:
  sys.path.insert(0, WORKBENCH_DIR)

import work_store  # pylint: disable=g-import-not-at-top

ROADMAP_SCRIPT = os.path.expanduser("~/.gemini/config/hooks/roadmap.py")


def _default_url_for_key(akey: str) -> str:
  c_key = work_store.canonicalize_artifact_key(akey)
  parts = c_key.split(":")
  kind = parts[0]
  tid = parts[-1]
  if kind == "gerrit":
    if len(parts) >= 3 and parts[1] == "chromium":
      return f"https://chromium-review.googlesource.com/c/chromium/src/+/{tid}"
    return f"https://skia-review.googlesource.com/c/skia/+/{tid}"
  if kind == "github_pr":
    return f"https://github.com/flutter/flutter/pull/{tid}"
  if kind == "github_issue":
    return f"https://github.com/flutter/flutter/issues/{tid}"
  if kind == "buganizer":
    return f"https://issues.chromium.org/issues/{tid}"
  if kind == "gmail_thread":
    return f"https://mail.google.com/mail/u/0/#inbox/{tid}"
  if kind == "gmail_draft":
    return f"https://mail.google.com/mail/u/0/#drafts/{tid}"
  if kind == "gdoc":
    return f"https://docs.google.com/document/d/{tid}/edit"
  if kind == "chat_chain":
    return f"/c/{tid}"
  return ""


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
    for chkey in args.channel:
      ck = work_store.canonicalize_artifact_key(chkey)
      store.upsert_artifact(ck, title=ck, url=_default_url_for_key(ck))
      store.link_artifact(wid, ck, role="channel", source="manual")
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
    store.upsert_artifact(
        ck, title=args.title or ck, url=args.url or _default_url_for_key(ck)
    )
    store.link_artifact(args.work_id, ck, role=args.role, source="manual")
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

  return 0


if __name__ == "__main__":
  sys.exit(main())
