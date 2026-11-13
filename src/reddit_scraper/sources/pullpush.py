"""PullPush: another free Reddit archive. Backup when Arctic Shift is down.

It is slower and often returns server errors, which the retry logic handles.
Rough limits: ~15 requests/minute, 1000/hour.
"""
from ..models import comment_from_reddit, post_from_reddit
from ._paging import walk_backwards

NAME = "pullpush"
BASE = "https://api.pullpush.io/reddit/search"


def _filters(q):
    p = {"sort": "desc", "sort_type": "created_utc"}
    if q.subreddit:
        p["subreddit"] = q.subreddit
    if q.author:
        p["author"] = q.author
    if q.query:
        p["q"] = q.query
    if q.since is not None:
        p["after"] = q.since
    if q.until is not None:
        p["before"] = q.until
    return p


def _walk(endpoint, params, max_items, log):
    return walk_backwards(
        f"{BASE}/{endpoint}/", params, max_items=max_items,
        page_size=100, size_param="size", sleep=3.0, log=log,
    )


def posts(q, log=None):
    for row in _walk("submission", _filters(q), q.max, log):
        yield post_from_reddit(row, NAME)


def comments(q, log=None):
    for row in _walk("comment", _filters(q), q.max, log):
        yield comment_from_reddit(row, NAME)


def comments_for_post(post_id, max_items, log=None):
    params = {"sort": "desc", "sort_type": "created_utc", "link_id": post_id}
    for row in _walk("comment", params, max_items, log):
        yield comment_from_reddit(row, NAME)


def ping():
    from ..http import get_json
    get_json(f"{BASE}/submission/", {"subreddit": "AskReddit", "size": 1}, retries=0, timeout=15)
