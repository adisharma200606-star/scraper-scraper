"""Arctic Shift: a free archive of Reddit going back to 2005. Best for date ranges.

API docs: https://github.com/ArthurHeitmann/arctic_shift/blob/master/api/README.md
Note: for posts younger than ~36 hours, score and comment counts may still be 0 or 1.
"""
from ..http import SourceError, get_json
from ..models import comment_from_reddit, keyword_matcher, post_from_reddit
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


def _search(endpoint, keyword_param, q, convert, text_of, log):
    """Keyword search, with a plan B.

    Plan A: ask Arctic Shift to do the keyword search (fast).
    Plan B: if that errors (it often times out with HTTP 422), page through
    the subreddit's posts ourselves and keep the ones that match the keywords.
    Slower, but it uses plain browsing, which keeps working.
    """
    params = _filters(q)
    if not q.query:
        for row in _walk(endpoint, params, q.max, log):
            yield convert(row, NAME)
        return

    got = 0
    try:
        # Not patient: if their keyword search is struggling, go straight to plan B.
        for row in _walk(endpoint, dict(params, **{keyword_param: q.query}), q.max, log, patient=False):
            got += 1
            yield convert(row, NAME)
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

    match = keyword_matcher(q.query)
    scanned = found = 0
    for row in _walk(endpoint, params, q.scan_max, log):
        scanned += 1
        if log and scanned % 1000 == 0:
            log(f"  checked {scanned}, {found} matched so far")
        if match(text_of(row)):
            found += 1
            yield convert(row, NAME)
            if found >= q.max:
                break
    if log:
        log(f"  checked {scanned}, {found} matched")
        if scanned >= q.scan_max:
            log(f"  stopped after checking {q.scan_max}. Narrow the dates or raise the scan limit for more")


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
