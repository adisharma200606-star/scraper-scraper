"""Arctic Shift: a free archive of Reddit going back to 2005. Best for date ranges.

API docs: https://github.com/ArthurHeitmann/arctic_shift/blob/master/api/README.md
Note: for posts younger than ~36 hours, score and comment counts may still be 0 or 1.
"""
from collections import Counter

from ..http import SourceError, get_json
from ..models import (comment_from_reddit, is_removed, keyword_matcher, left_out_message,
                      post_from_reddit)
from ._paging import walk_backwards

NAME = "arctic_shift"
BASE = "https://arctic-shift.photon-reddit.com/api"


def _filters(q):
    p = {"sort": "desc"}
    if q.subreddit:
        p["subreddit"] = q.subreddit
    if q.author:
        p["author"] = q.author
    if q.since is not None:
        p["after"] = q.since
    if q.until is not None:
        p["before"] = q.until
    return p


def _walk(endpoint, params, max_items, log, patient=True):
    return walk_backwards(
        f"{BASE}/{endpoint}", params, max_items=max_items,
        page_size=100, size_param="limit", sleep=1.0, log=log, patient=patient,
    )


def _live_only(items, q, left_out):
    """Pass items on, leaving out removed ones unless asked to keep them (counted in left_out)."""
    for item in items:
        if not q.include_removed and is_removed(item):
            left_out[item["status"]] += 1
            continue
        yield item


def _say_left_out(left_out, log):
    if log and left_out:
        log(left_out_message(left_out))


def _first(items, n, on_done):
    """Yield up to n items, calling on_done() once at the end.

    on_done runs BEFORE the last item is handed over: whoever reads this usually stops
    asking as soon as it has enough, so anything after the last yield would never run.
    """
    got = 0
    for item in items:
        got += 1
        if got >= n:
            on_done()
            yield item
            return
        yield item
    on_done()


def _scan(endpoint, q, match, convert, text_of, log):
    """Page through everything in q's date range ourselves and keep what matches the keywords."""
    counts = {"scanned": 0, "found": 0}
    left_out = Counter()

    def matching():
        for row in _walk(endpoint, _filters(q), q.scan_max, log):
            counts["scanned"] += 1
            if log and counts["scanned"] % 1000 == 0:
                log(f"  checked {counts['scanned']}, {counts['found']} matched so far")
            if match(text_of(row)):
                yield convert(row, NAME)

    def counted(items):
        for item in items:
            counts["found"] += 1
            yield item

    def report():
        if log:
            log(f"  checked {counts['scanned']}, {counts['found']} matched")
            if counts["scanned"] >= q.scan_max:
                log(f"  stopped after checking {q.scan_max}. Narrow the dates or raise the scan limit for more")
        _say_left_out(left_out, log)

    yield from _first(counted(_live_only(matching(), q, left_out)), q.max, report)


def _search(endpoint, keyword_param, q, convert, text_of, log):
    """Keyword search, with a plan B.

    Plan A: ask Arctic Shift to do the keyword search (fast).
    Plan B: if that errors (it often times out with HTTP 422), page through
    the subreddit's posts ourselves and keep the ones that match the keywords.
    Slower, but it uses plain browsing, which keeps working.

    Removed posts are skipped, and we keep reading until we have q.max that are still up
    (reading at most q.scan_max), so a heavily moderated subreddit still gives enough results.
    """
    params = _filters(q)
    cap = q.max if q.include_removed else max(q.max, q.scan_max)
    left_out = Counter()
    say = lambda: _say_left_out(left_out, log)  # noqa: E731
    if not q.query:
        rows = _walk(endpoint, params, cap, log)
        yield from _first(_live_only((convert(r, NAME) for r in rows), q, left_out), q.max, say)
        return

    got = 0
    try:
        # Not patient: if their keyword search is struggling, go straight to plan B.
        rows = _walk(endpoint, dict(params, **{keyword_param: q.query}), cap, log, patient=False)
        for item in _first(_live_only((convert(r, NAME) for r in rows), q, left_out), q.max, say):
            got += 1
            yield item
        return
    except SourceError as e:
        if got:
            raise  # partway through: let the fallback logic resume from here
        if not (q.subreddit or q.author):
            raise SourceError(
                f"keyword search failed ({e}). Add a subreddit or author so it can "
                "scan and filter instead", e.status)
        if log:
            log(f"  {NAME}'s keyword search is busy ({e}), so checking posts one by one instead "
                "(slower, but works)")
    yield from _scan(endpoint, q, keyword_matcher(q.query), convert, text_of, log)


def posts(q, log=None):
    # 'query' searches title + body text
    yield from _search(
        "posts/search", "query", q, post_from_reddit,
        lambda r: f"{r.get('title') or ''}\n{r.get('selftext') or ''}", log,
    )


def comments(q, log=None):
    # 'body' needs a subreddit or author alongside it
    yield from _search("comments/search", "body", q, comment_from_reddit, lambda r: r.get("body"), log)


def comments_for_post(post_id, max_items, log=None):
    for row in _walk("comments/search", {"sort": "desc", "link_id": post_id}, max_items, log):
        yield comment_from_reddit(row, NAME)


def ping():
    get_json(f"{BASE}/posts/search", {"subreddit": "AskReddit", "limit": 1}, retries=0, timeout=15)


# ---------- explaining an empty result ----------
# These only feed a helpful message, so they ask once and give up quickly instead of
# waiting out Arctic Shift's "slow down" answers.
QUICK = {"retries": 1, "timeout": 20, "retry_slowdowns": False}


def subreddit_info(name):
    """What the archive knows about a subreddit: {"name", "posts"}, or None if it has never heard of it."""
    rows = (get_json(f"{BASE}/subreddits/search", {"subreddit": name}, **QUICK) or {}).get("data") or []
    if not rows:
        return None
    return {"name": rows[0].get("display_name") or name,
            "posts": (rows[0].get("_meta") or {}).get("num_posts") or 0}


def newest_post_time(name):
    """Time of the subreddit's newest archived post, or None if it has none."""
    rows = (get_json(f"{BASE}/posts/search", {"subreddit": name, "limit": 1, "sort": "desc"}, **QUICK)
            or {}).get("data") or []
    return int(float(rows[0]["created_utc"])) if rows else None


def similar_subreddits(name, limit=10):
    """Subreddits whose names start like this one, busiest first: [(name, number of posts)].

    The archive's subreddit search only matches the start of names, so we drop the last letter:
    that catches plurals ("voiceagent" / "voiceagents") and a mistyped last letter ("voiceagentz").
    """
    prefix = name[:-1] if len(name) > 3 else name
    rows = (get_json(f"{BASE}/subreddits/search", {"subreddit_prefix": prefix, "limit": limit}, **QUICK)
            or {}).get("data") or []
    found = [(r["display_name"], (r.get("_meta") or {}).get("num_posts") or 0)
             for r in rows if r.get("display_name")]
    return sorted(found, key=lambda x: -x[1])
