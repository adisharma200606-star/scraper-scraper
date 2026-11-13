"""Reddit's own public JSON pages (add .json to almost any Reddit URL).

Gives live, up-to-date scores and full comment threads, but:
- only reaches the latest ~1000 items of any listing (no deep history)
- rate-limits hard (~10 requests/minute)
- Reddit blocks it from many networks (mobile data, VPNs, offices, cloud servers).
  When that happens we skip it for 10 minutes instead of retrying.
"""
import time

from ..http import SourceError, Unsupported, get_json, is_blocked, mark_blocked
from ..models import comment_from_reddit, post_from_reddit

NAME = "reddit_json"
BASE = "https://www.reddit.com"
PAGE_SLEEP = 4.0
BLOCKED_MSG = ("Reddit is blocking its JSON pages for this network (common on mobile data, "
               "VPNs and offices); skipping it for now")


def _get(path, params, log, retries=3):
    if is_blocked(NAME):
        raise SourceError(BLOCKED_MSG, 403)
    try:
        return get_json(BASE + path, dict(params, raw_json=1), log=log, retries=retries)
    except SourceError as e:
        if e.status in (401, 403):
            mark_blocked(NAME)
            raise SourceError(BLOCKED_MSG, e.status)
        raise


def _listing(path, params, max_items, log, *, chronological, since=None):
    """Walk a Reddit listing page by page using its 'after' cursor."""
    after = None
    count = 0
    while count < max_items:
        p = dict(params, limit=100)
        if after:
            p["after"] = after
        data = (_get(path, p, log) or {}).get("data") or {}
        children = data.get("children") or []
        if not children:
            return
        for child in children:
            if child.get("kind") not in ("t1", "t3"):
                continue
            d = child.get("data") or {}
            created = int(float(d.get("created_utc") or 0))
            if chronological and since is not None and created and created < since:
                return  # newest-first listing has gone past the start date
            yield d
            count += 1
            if count >= max_items:
                return
        after = data.get("after")
        if not after:
            return
        time.sleep(PAGE_SLEEP)


def posts(q, log=None):
    if q.query:
        search = q.query + (f" author:{q.author}" if q.author else "")
        path = f"/r/{q.subreddit}/search.json" if q.subreddit else "/search.json"
        sort = "top" if q.listing == "top" else "new"
        params = {"q": search, "sort": sort, "t": "all"}
        if q.subreddit:
            params["restrict_sr"] = 1
        chronological = sort == "new"
    elif q.author and not q.subreddit:
        path, params, chronological = f"/user/{q.author}/submitted.json", {"sort": "new"}, True
    else:
        listing = q.listing if q.listing in ("new", "hot", "top", "rising") else "new"
        path = f"/r/{q.subreddit or 'all'}/{listing}.json"
        params = {"t": "all"} if listing == "top" else {}
        chronological = listing == "new"

    for d in _listing(path, params, q.max, log, chronological=chronological, since=q.since):
        if q.author and (d.get("author") or "").lower() != q.author.lower():
            continue
        yield post_from_reddit(d, NAME)


def comments(q, log=None):
    if q.query:
        raise Unsupported("Reddit's public pages can't keyword-search comments")
    if q.subreddit:
        path = f"/r/{q.subreddit}/comments.json"
    elif q.author:
        path = f"/user/{q.author}/comments.json"
    else:
        raise Unsupported("needs a subreddit or author")
    for d in _listing(path, {"sort": "new"}, q.max, log, chronological=True, since=q.since):
        if q.author and (d.get("author") or "").lower() != q.author.lower():
            continue
        yield comment_from_reddit(d, NAME)


def comments_for_post(post_id, max_items, log=None):
    data = _get(f"/comments/{post_id}.json", {"limit": 500, "depth": 50, "sort": "top"}, log)
    if not isinstance(data, list) or len(data) < 2:
        return
    post = ((data[0].get("data") or {}).get("children") or [{}])[0].get("data") or {}
    subreddit = post.get("subreddit")
    count = 0
    hidden = 0
    stack = list(reversed((data[1].get("data") or {}).get("children") or []))
    while stack and count < max_items:
        child = stack.pop()
        if child.get("kind") == "more":
            hidden += len((child.get("data") or {}).get("children") or [])
            continue
        d = child.get("data") or {}
        d.setdefault("subreddit", subreddit)
        d.setdefault("link_id", f"t3_{post_id}")
        yield comment_from_reddit(d, NAME)
        count += 1
        replies = d.get("replies")
        if isinstance(replies, dict):
            stack.extend(reversed((replies.get("data") or {}).get("children") or []))
    if hidden and log:
        log(f"  reddit_json: {hidden} replies were collapsed behind 'load more' and skipped")


def ping():
    _get("/r/AskReddit/new.json", {"limit": 1}, None, retries=0)
