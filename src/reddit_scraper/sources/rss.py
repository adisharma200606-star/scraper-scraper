"""Reddit RSS feeds (add .rss to a subreddit or post URL).

The least-blocked part of Reddit. It gives recent posts with their text, and
comment threads, but no scores or comment counts. Keyword searches read the
subreddit's normal feed and filter locally (Reddit's search feed is unreliable).
"""
import html
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime
from urllib.parse import urlencode

from ..http import SourceError, Unsupported, get, is_blocked, mark_blocked
from ..models import full_link, iso, keyword_matcher

NAME = "rss"
BASE = "https://www.reddit.com"
NS = {"a": "http://www.w3.org/2005/Atom"}
PAGE_SLEEP = 3.0
MAX_PAGES = 10  # Reddit feeds stop around 1000 items anyway


def _parse_time(text):
    try:
        return int(datetime.fromisoformat((text or "").replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _html_to_text(raw):
    """Turn the feed's HTML content into plain text and drop Reddit's footer."""
    raw = re.sub(r"(?is)<br\s*/?>|</p>|</li>", "\n", raw or "")
    text = html.unescape(re.sub(r"(?s)<[^>]+>", "", raw))
    text = re.sub(r"\s*submitted by\s+/u/\S+.*$", "", text, flags=re.S)  # post footer
    text = re.sub(r"\[link\]\s*\[comments\]\s*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _entries(path, params, log, retries=3):
    if is_blocked(NAME):
        raise SourceError("Reddit is blocking RSS for this network; skipping it for now", 403)
    try:
        resp = get(BASE + path, params, log=log, retries=retries)
    except SourceError as e:
        if e.status in (401, 403):
            mark_blocked(NAME)
        raise
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        raise SourceError("RSS feed wasn't valid XML (probably a block page)")
    return root.findall("a:entry", NS)


def _common(entry):
    link_el = entry.find("a:link", NS)
    link = link_el.get("href") if link_el is not None else ""
    cat = entry.find("a:category", NS)
    author = (entry.findtext("a:author/a:name", "", NS) or "").removeprefix("/u/") or None
    created = _parse_time(entry.findtext("a:published", "", NS) or entry.findtext("a:updated", "", NS))
    return {
        "entry_id": entry.findtext("a:id", "", NS),
        "link": link,
        "subreddit": cat.get("term") if cat is not None else None,
        "author": author,
        "created": created,
        "text": _html_to_text(entry.findtext("a:content", "", NS)),
    }


def _to_post(entry, fallback_sub):
    c = _common(entry)
    if c["entry_id"].startswith("t1_"):
        return None  # a comment, not a post
    m = re.search(r"/comments/([a-z0-9]+)", c["link"])
    post_id = m.group(1) if m else c["entry_id"].replace("t3_", "")
    if not post_id:
        return None
    return {
        "id": post_id, "subreddit": c["subreddit"] or fallback_sub,
        "title": entry.findtext("a:title", "", NS), "author": c["author"],
        "score": None, "num_comments": None, "created": iso(c["created"]), "flair": None,
        "selftext": c["text"], "url": c["link"], "permalink": full_link(c["link"]),
        "over_18": False, "created_utc": c["created"], "source": NAME,
    }


def _to_comment(entry, post_id, fallback_sub):
    c = _common(entry)
    if not c["entry_id"].startswith("t1_"):
        return None
    return {
        "id": c["entry_id"][3:], "post_id": post_id, "parent_id": None,
        "subreddit": c["subreddit"] or fallback_sub, "author": c["author"], "body": c["text"],
        "score": None, "created": iso(c["created"]), "permalink": full_link(c["link"]),
        "created_utc": c["created"], "source": NAME,
    }


def _walk(path, convert, q, log):
    """Read a feed page by page, newest first, keeping items that pass the filters."""
    match = keyword_matcher(q.query) if q.query else None
    seen, after, count = set(), None, 0
    for _ in range(MAX_PAGES):
        params = {"limit": 100}
        if after:
            params["after"] = after
        items = []
        for entry in _entries(path, params, log):
            item = convert(entry)
            if item and item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
        if not items:
            return
        for item in items:
            ts = item.get("created_utc")
            if q.since is not None and ts is not None and ts < q.since:
                return  # gone past the start date
            if q.until is not None and ts is not None and ts >= q.until:
                continue
            if q.author and (item.get("author") or "").lower() != q.author.lower():
                continue
            if match and not match(f"{item.get('title') or ''}\n{item.get('selftext') or item.get('body') or ''}"):
                continue
            yield item
            count += 1
            if count >= q.max:
                return
        after = ("t1_" if "body" in items[-1] else "t3_") + items[-1]["id"]
        time.sleep(PAGE_SLEEP)


def posts(q, log=None):
    if q.subreddit:
        listing = "new" if q.query or q.since else (q.listing if q.listing in ("new", "hot", "top", "rising") else "new")
        path = f"/r/{q.subreddit}/{listing}/.rss"
    elif q.author:
        path = f"/user/{q.author}/submitted/.rss"
    elif q.query:
        raise Unsupported("RSS can't keyword-search all of Reddit; add a subreddit")
    else:
        path = "/r/all/new/.rss"
    yield from _walk(path, lambda e: _to_post(e, q.subreddit), q, log)


def comments(q, log=None):
    if q.subreddit:
        path = f"/r/{q.subreddit}/comments/.rss"
    elif q.author:
        path = f"/user/{q.author}/comments/.rss"
    else:
        raise Unsupported("RSS comment search needs a subreddit or author")
    yield from _walk(path, lambda e: _to_comment(e, _post_id_of(e), q.subreddit), q, log)


def _post_id_of(entry):
    link_el = entry.find("a:link", NS)
    m = re.search(r"/comments/([a-z0-9]+)", link_el.get("href") if link_el is not None else "")
    return m.group(1) if m else None


def comments_for_post(post_id, max_items, log=None):
    """A post's comment feed. Holds up to ~100 of the newest comments."""
    count = 0
    for entry in _entries(f"/comments/{post_id}/.rss", {"limit": 100}, log):
        c = _to_comment(entry, post_id, None)
        if c:
            yield c
            count += 1
            if count >= max_items:
                return


def ping():
    _entries("/r/AskReddit/new/.rss", {"limit": 1}, None, retries=0)


# ---------- finding subreddits / searching all of Reddit ----------
# Used when you don't know which subreddit to look in.

def find_subreddits(query, limit=10, log=None):
    """Ask Reddit which communities match the keywords. Returns [{name, title, about}], best first."""
    found = []
    for entry in _entries("/subreddits/search.rss", {"q": query, "limit": max(limit, 25)}, log):
        link_el = entry.find("a:link", NS)
        m = re.search(r"/r/([A-Za-z0-9_]+)", link_el.get("href") if link_el is not None else "")
        if not m:
            continue
        found.append({
            "name": m.group(1),
            "title": entry.findtext("a:title", "", NS),
            "about": _html_to_text(entry.findtext("a:content", "", NS))[:300],
        })
        if len(found) >= limit:
            break
    return found


def search_all(q, log=None):
    """Reddit's own search across all subreddits, newest first. Reddit does the matching."""
    if not q.query:
        raise Unsupported("searching all of Reddit needs keywords")
    search = q.query + (f" author:{q.author}" if q.author else "")
    path = "/search.rss?" + urlencode({"q": search, "sort": "new", "type": "link"})
    yield from _walk(path, lambda e: _to_post(e, None), replace(q, query=None), log)
