"""The four no-login sources. Each module exposes the same functions:

    posts(q, log)                         -> yields post dicts
    comments(q, log)                      -> yields comment dicts (search)
    comments_for_post(post_id, max, log)  -> yields comment dicts for one post

A source raises http.Unsupported when it can't do a given kind of search.
"""
from . import arctic_shift, pullpush, reddit_json, rss

SOURCES = {
    "arctic_shift": arctic_shift,
    "pullpush": pullpush,
    "reddit_json": reddit_json,
    "rss": rss,
}
