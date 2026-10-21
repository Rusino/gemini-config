#!/usr/bin/env python3
"""
Fast, zero-dependency search tool for the veteran reviewer's code review corpus.
Searches data/reviews.jsonl and returns relevant historical review precedents.
"""

import argparse
import json
import math
import os
import re
import sys

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "reviews.jsonl"
)


def load_corpus(db_path):
    if not os.path.exists(db_path):
        sys.stderr.write(f"Error: Database file not found at {db_path}\n")
        sys.exit(1)
    
    records = []
    with open(db_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def score_record(rec, target_files, keywords, compiled_kws):
    score = 0.0
    rec_file = rec.get("file", "")
    rec_base = os.path.basename(rec_file).lower()
    rec_msg = rec.get("message", "")
    rec_subj = rec.get("subject", "")
    
    file_matched = False
    if target_files:
        for tf in target_files:
            tf_lower = tf.lower()
            tf_base = os.path.basename(tf_lower)
            if rec_file.lower() == tf_lower:
                score += 80.0
                file_matched = True
            elif rec_base == tf_base:
                score += 50.0
                file_matched = True
            elif tf_lower in rec_file.lower():
                score += 25.0
                file_matched = True
            elif rec_file == "/PATCHSET_LEVEL" and (tf_base in rec_msg.lower() or tf_base in rec_subj.lower()):
                score += 30.0
                file_matched = True

    kw_matched = False
    if compiled_kws:
        for kw_str, pattern in compiled_kws:
            matches_msg = len(pattern.findall(rec_msg))
            matches_subj = len(pattern.findall(rec_subj))
            matches_file = len(pattern.findall(rec_file))
            
            if matches_msg > 0 or matches_subj > 0 or matches_file > 0:
                kw_matched = True
                # Match density and TF
                score += (matches_msg * 10.0) + (matches_subj * 8.0) + (matches_file * 12.0)
                # Substantive length bonus (log of message length)
                msg_len = len(rec_msg)
                if msg_len > 20:
                    score += math.log(msg_len) * 3.0
                # Exact case match bonus
                if kw_str in rec_msg:
                    score += 5.0

    # If filters were provided, require at least one match
    if target_files and compiled_kws:
        if not (file_matched or kw_matched):
            return 0.0
        # Big boost if BOTH file and keyword matched
        if file_matched and kw_matched:
            score += 40.0
    elif target_files:
        if not file_matched:
            return 0.0
    elif compiled_kws:
        if not kw_matched:
            return 0.0

    # Boost reviews on others' CLs vs comments on own CLs
    if rec.get("is_review", False):
        score += 15.0

    return score


def search(records, files=None, keywords=None, subsystem="all", reviews_only=True, limit=15):
    target_files = [f.strip() for f in files] if files else []
    
    compiled_kws = []
    if keywords:
        for kw in keywords:
            kw_clean = kw.strip()
            if kw_clean:
                try:
                    p = re.compile(re.escape(kw_clean), re.IGNORECASE)
                except Exception:
                    p = re.compile(re.escape(kw_clean))
                compiled_kws.append((kw_clean, p))

    scored = []
    for r in records:
        # Subsystem filter
        if subsystem == "text" and not r.get("is_text_font", False):
            continue
        if subsystem == "general" and r.get("is_text_font", False):
            continue
        
        # Reviews-only filter
        if reviews_only and not r.get("is_review", False):
            continue

        s = score_record(r, target_files, keywords, compiled_kws)
        if s > 0.0:
            scored.append((s, r))

    scored.sort(key=lambda x: (x[0], len(x[1].get("message", ""))), reverse=True)
    return [item[1] for item in scored[:limit]]


def format_markdown(results):
    if not results:
        return "No matching veteran comments found."

    lines = []
    for r in results:
        cl = r.get("cl")
        date = r.get("date", "")
        file_path = r.get("file", "")
        line = r.get("line")
        loc_str = f"{file_path}:{line}" if line is not None else file_path
        url = f"https://skia-review.googlesource.com/c/skia/+/{cl}"
        review_tag = "Review" if r.get("is_review") else "Author Note"
        
        msg = r.get("message", "").strip()
        lines.append(f"### [{review_tag}] [CL {cl}]({url}) ({date}) — `{loc_str}`")
        if r.get("subject"):
            lines.append(f"*Subject: {r.get('subject')}*")
        lines.append("")
        # Format comment message as blockquote
        quoted = "\n".join(f"> {line}" for line in msg.split("\n"))
        lines.append(quoted)
        lines.append("")

    return "\n".join(lines).strip()


def main():
    parser = argparse.ArgumentParser(
        description="Search the veteran reviewer's code review corpus from Skia Gerrit."
    )
    parser.add_argument(
        "--files", "-f",
        nargs="+",
        help="Target files to match by exact path, basename, or directory prefix (e.g. SkFontHost_FreeType.cpp TextLine.cpp)."
    )
    parser.add_argument(
        "--keywords", "-k",
        nargs="+",
        help="Keywords or symbols to match (e.g. SkStream surrogate invert mutex)."
    )
    parser.add_argument(
        "--subsystem", "-s",
        choices=["all", "text", "general"],
        default="all",
        help="Subsystem filter: 'text' (fonts, shaping, typography, ports), 'general' (core C++, GPU, streams, math), or 'all' (default)."
    )
    parser.add_argument(
        "--include-self",
        action="store_true",
        help="Include comments the reviewer made on their own CLs (default searches reviews on others' CLs only)."
    )
    parser.add_argument(
        "--reviews-only",
        action="store_true",
        default=True,
        help="Search reviews on others' CLs only (default: True)."
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=15,
        help="Maximum number of comments to return (default: 15)."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON array instead of markdown."
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Path to reviews.jsonl (default: {DEFAULT_DB_PATH})."
    )

    args = parser.parse_args()

    reviews_only = not args.include_self

    records = load_corpus(args.db)
    results = search(
        records,
        files=args.files,
        keywords=args.keywords,
        subsystem=args.subsystem,
        reviews_only=reviews_only,
        limit=args.limit
    )

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print(format_markdown(results))


if __name__ == "__main__":
    main()
