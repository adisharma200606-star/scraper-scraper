"""The shared shape every source converts its data into, plus date helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

POST_FIELDS = [
    "id", "subreddit", "title", "author", "score", "num_comments", "created",
    "flair", "selftext", "url", "permalink", "over_18", "created_utc", "source",
]
COMMENT_FIELDS = [
    "id", "post_id", "parent_id", "subreddit", "author", "body", "score",
    "created", "permalink", "created_utc", "source",
]


@dataclass
class Query:
    """What to search for. Every field is optional."""
    subreddit: str | None = None
    query: str | None = None       # keyword(s)
    author: str | None = None
    since: int | None = None       # unix timestamp, inclusive
    until: int | None = None       # unix timestamp, exclusive
    max: int = 100
    listing: str = "new"           # only used by live sources: new / hot / top / rising

    def __post_init__(self):
        self.subreddit = _clean(self.subreddit, ("r/", "/r/"))
        self.author = _clean(self.author, ("u/", "/u/"))
        self.query = (self.query or "").strip() or None


def _clean(value, prefixes):
    value = (value or "").strip()
    for p in prefixes:
        if value.lower().startswith(p):
            value = value[len(p):]
    return value.strip("/") or None


# ---------- dates ----------

def parse_when(text, end_of_day=False):
    """Turn '2025-01-31', '7d', '12h', '2w' or a unix timestamp into a unix timestamp.

    end_of_day=True makes a plain date mean the END of that day, so
    '--until 2025-01-31' includes everything posted on the 31st.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    text = str(text).strip().lower()
    if not text:
        return None
    m = re.fullmatch(r"(\d+)\s*([hdw])", text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        return int((datetime.now(timezone.utc) - delta).timestamp())
    if text.isdigit() and len(text) >= 9:
        return int(text)
    try:
        d = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"Can't read date '{text}'. Use YYYY-MM-DD, or e.g. 7d / 12h / 2w.")
    if end_of_day:
        d += timedelta(days=1)
    return int(d.timestamp())


def iso(ts):
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def post_id_from(text):
    """Accept a post ID, 't3_' ID or full Reddit URL and return the bare ID."""
    text = (text or "").strip()
    m = re.search(r"/comments/([a-z0-9]+)", text, re.I)
    if m:
        return m.group(1).lower()
    if text.lower().startswith("t3_"):
        text = text[3:]
    return text.lower() or None


def full_link(permalink):
    if not permalink:
        return None
    if permalink.startswith("http"):
        return permalink
    return "https://www.reddit.com" + permalink


# ---------- converting Reddit-shaped data ----------
# Arctic Shift, PullPush and Reddit's own JSON all use Reddit's field names,
# so one converter covers all three.

def post_from_reddit(d, source):
    created = to_int(d.get("created_utc"))
    return {
        "id": d.get("id"),
        "subreddit": d.get("subreddit"),
        "title": d.get("title"),
        "author": d.get("author"),
        "score": to_int(d.get("score")),
        "num_comments": to_int(d.get("num_comments")),
        "created": iso(created),
        "flair": d.get("link_flair_text"),
        "selftext": d.get("selftext") or "",
        "url": d.get("url"),
        "permalink": full_link(d.get("permalink")),
        "over_18": bool(d.get("over_18")),
        "created_utc": created,
        "source": source,
    }


def comment_from_reddit(d, source):
    created = to_int(d.get("created_utc"))
    link_id = d.get("link_id") or ""
    post_id = link_id.split("_", 1)[-1] if link_id else None
    sub = d.get("subreddit")
    permalink = d.get("permalink")
    if not permalink and sub and post_id and d.get("id"):
        permalink = f"/r/{sub}/comments/{post_id}/_/{d['id']}/"
    return {
        "id": d.get("id"),
        "post_id": post_id,
        "parent_id": d.get("parent_id"),
        "subreddit": sub,
        "author": d.get("author"),
        "body": d.get("body") or "",
        "score": to_int(d.get("score")),
        "created": iso(created),
        "permalink": full_link(permalink),
        "created_utc": created,
        "source": source,
    }
