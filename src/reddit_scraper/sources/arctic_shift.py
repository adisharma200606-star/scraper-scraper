"""Arctic Shift: a free archive of Reddit going back to 2005. Best for date ranges.

API docs: https://github.com/ArthurHeitmann/arctic_shift/blob/master/api/README.md
Note: for posts younger than ~36 hours, score and comment counts may still be 0 or 1.
"""
from ..models import comment_from_reddit, post_from_reddit
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


def _walk(endpoint, params, max_items, log):
    return walk_backwards(
        f"{BASE}/{endpoint}", params, max_items=max_items,
        page_size=100, size_param="limit", sleep=0.5, log=log,
    )


def posts(q, log=None):
    params = _filters(q)
    if q.query:
        params["query"] = q.query  # searches title + body text
    for row in _walk("posts/search", params, q.max, log):
        yield post_from_reddit(row, NAME)


def comments(q, log=None):
    params = _filters(q)
    if q.query:
        params["body"] = q.query  # needs a subreddit or author alongside it
    for row in _walk("comments/search", params, q.max, log):
        yield comment_from_reddit(row, NAME)


def comments_for_post(post_id, max_items, log=None):
    for row in _walk("comments/search", {"sort": "desc", "link_id": post_id}, max_items, log):
        yield comment_from_reddit(row, NAME)


def ping():
    from ..http import get_json
    get_json(f"{BASE}/posts/search", {"subreddit": "AskReddit", "limit": 1}, retries=0, timeout=15)
