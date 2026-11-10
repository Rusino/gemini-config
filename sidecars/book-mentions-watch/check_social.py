#!/usr/bin/env python3
"""Check social and Q&A sources for new mentions of "Start Doing Graph Theory".

Queries free public APIs without keys: Bluesky post search, Hacker News (Algolia) stories and
comments, Hypothes.is annotations on vertex.degree, and Stack Exchange excerpts. Skips items
whose key is already in state.json "seen_social". Prints JSON {"new": [{"source", "key", "url",
"date", "author", "text"}]} to stdout. With --mark, adds the reported keys to state.json (side
effect); without it, read-only. Network errors are reported in "errors", never fatal.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
QUERIES = ["vertex.degree", '"Start Doing Graph Theory"']
SE_SITES = ["math", "mathoverflow.net", "matheducators", "cs", "cstheory"]


def get(url):
    """GET url and return parsed JSON."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 book-mention-watch"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def bluesky(q):
    """Yield items from Bluesky post search."""
    d = get("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?limit=50&q=" + urllib.parse.quote(q))
    for p in d.get("posts", []):
        handle = p["author"]["handle"]
        url = "https://bsky.app/profile/%s/post/%s" % (handle, p["uri"].split("/")[-1])
        yield {"source": "bluesky", "key": url, "url": url, "author": handle,
               "date": p["record"].get("createdAt", "")[:10], "text": p["record"].get("text", "")[:300],
               "likes": p.get("likeCount", 0)}


def hn(q):
    """Yield stories and comments from Hacker News Algolia search."""
    d = get("https://hn.algolia.com/api/v1/search?hitsPerPage=100&query=" + urllib.parse.quote(q))
    for h in d.get("hits", []):
        url = "https://news.ycombinator.com/item?id=%s" % h["objectID"]
        yield {"source": "hn", "key": url, "url": url, "author": h.get("author", ""),
               "date": h.get("created_at", "")[:10],
               "text": (h.get("title") or h.get("comment_text") or "")[:300]}


def hypothesis():
    """Yield public Hypothes.is annotations on vertex.degree pages."""
    d = get("https://api.hypothes.is/api/search?limit=50&wildcard_uri="
            + urllib.parse.quote("https://vertex.degree/*"))
    for r in d.get("rows", []):
        url = r.get("links", {}).get("incontext") or r.get("uri", "")
        yield {"source": "hypothesis", "key": r["id"], "url": url, "author": r.get("user", ""),
               "date": r.get("created", "")[:10], "text": r.get("text", "")[:300]}


def stackexchange(site):
    """Yield Stack Exchange posts that contain the domain."""
    d = get("https://api.stackexchange.com/2.3/search/excerpts?order=desc&sort=creation&site=%s&q=%s"
            % (site, urllib.parse.quote("vertex.degree")))
    for i in d.get("items", []):
        pid = i.get("answer_id") or i.get("question_id")
        host = site if "." in site else site + ".stackexchange.com"
        url = "https://%s/a/%s" % (host, pid) if i.get("answer_id") else "https://%s/q/%s" % (host, pid)
        yield {"source": "stackexchange", "key": url, "url": url, "author": "",
               "date": "", "text": i.get("title", "")[:300]}


def main():
    state = json.load(open(STATE))
    seen = set(state.get("seen_social", []))
    calls = [lambda q=q: bluesky(q) for q in QUERIES] + [lambda q=q: hn(q) for q in QUERIES]
    calls += [hypothesis] + [lambda s=s: stackexchange(s) for s in SE_SITES]
    new, errors, keys = [], [], set()
    for call in calls:
        try:
            for item in call():
                if item["key"] not in seen and item["key"] not in keys:
                    keys.add(item["key"])
                    new.append(item)
        except Exception as e:  # one failing source must not hide the others
            errors.append(str(e))
    if "--mark" in sys.argv:
        state["seen_social"] = sorted(seen | keys)
        json.dump(state, open(STATE, "w"))
    print(json.dumps({"new": new, "errors": errors}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
