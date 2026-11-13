"""Reddit RSS feeds (add .rss to a subreddit URL).

The least-blocked option, so it's the last-resort fallback. But it only gives
recent posts, and has no scores, comment counts or post text.
"""
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime

from ..http import SourceError, Unsupported, get
from ..models import full_link, iso

NAME = "rss"
BASE = "https://www.reddit.com"
NS = {"a": "http://www.w3.org/2005/Atom"}


def _parse_time(text):
    try:
        return int(datetime.fromisoformat((text or "").replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _entries(path, params, log, retries=4):
    resp = get(BASE + path, params, log=log, retries=retries)
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        raise SourceError("RSS feed wasn't valid XML (probably a block page)")
    return root.findall("a:entry", NS)


def _to_post(entry, fallback_sub):
    entry_id = entry.findtext("a:id", "", NS)
    link_el = entry.find("a:link", NS)
    link = link_el.get("href") if link_el is not None else ""
    m = re.search(r"/comments/([a-z0-9]+)", link)
    post_id = m.group(1) if m else entry_id.replace("t3_", "")
    if not post_id or entry_id.startswith("t1_"):
        return None
    cat = entry.find("a:category", NS)
    sub = cat.get("term") if cat is not None else fallback_sub
    author = (entry.findtext("a:author/a:name", "", NS) or "").removeprefix("/u/") or None
    created = _parse_time(entry.findtext("a:published", "", NS) or entry.findtext("a:updated", "", NS))
    return {
        "id": post_id, "subreddit": sub, "title": entry.findtext("a:title", "", NS),
        "author": author, "score": None, "num_comments": None, "created": iso(created),
        "flair": None, "selftext": "", "url": link, "permalink": full_link(link),
        "over_18": False, "created_utc": created, "source": NAME,
    }


def posts(q, log=None):
    if q.query:
        path = f"/r/{q.subreddit}/search.rss" if q.subreddit else "/search.rss"
        params = {"q": q.query + (f" author:{q.author}" if q.author else ""), "sort": "new"}
        if q.subreddit:
            params["restrict_sr"] = 1
    elif q.author and not q.subreddit:
        path, params = f"/user/{q.author}/submitted.rss", {}
    else:
        listing = q.listing if q.listing in ("new", "hot", "top", "rising") else "new"
        path, params = f"/r/{q.subreddit or 'all'}/{listing}/.rss", {}

    seen, after, count = set(), None, 0
    while count < q.max:
        p = dict(params, limit=100)
        if after:
            p["after"] = after
        fresh = []
        for entry in _entries(path, p, log):
            post = _to_post(entry, q.subreddit)
            if post and post["id"] not in seen:
                fresh.append(post)
        if not fresh:
            return
        for post in fresh:
            seen.add(post["id"])
            if q.author and (post["author"] or "").lower() != q.author.lower():
                continue
            yield post
            count += 1
            if count >= q.max:
                return
        after = "t3_" + fresh[-1]["id"]
        time.sleep(3.0)


def comments(q, log=None):
    raise Unsupported("RSS feeds don't support comment search")


def comments_for_post(post_id, max_items, log=None):
    raise Unsupported("RSS feeds don't include comment threads")


def ping():
    _entries("/r/AskReddit/new/.rss", {"limit": 1}, None, retries=0)
