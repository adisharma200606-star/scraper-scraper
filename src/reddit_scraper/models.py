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
    scan_max: int = 10000          # when a keyword search has to filter locally, check at most this many

    def __post_init__(self):
        self.subreddit = _clean(self.subreddit, ("r/", "/r/"))
        self.author = _clean(self.author, ("u/", "/u/"))
        self.query = (self.query or "").strip() or None
        if self.author and not USERNAME.fullmatch(self.author):
            raise ValueError(f"'{self.author}' isn't a Reddit username "
                             "(only letters, numbers, - and _).")


def _clean(value, prefixes):
    value = (value or "").strip()
    for p in prefixes:
        if value.lower().startswith(p):
            value = value[len(p):]
    return value.strip("/") or None


# What Reddit allows in names. Names go into web addresses, so anything else (spaces, / ? # ..)
# would quietly ask Reddit for the wrong page; we stop with a clear message instead.
SUBREDDIT_NAME = re.compile(r"[A-Za-z0-9_]{1,50}")
USERNAME = re.compile(r"[A-Za-z0-9_-]{1,50}")
POST_ID = re.compile(r"[a-z0-9]{1,15}")


def check_subreddit(name):
    if not SUBREDDIT_NAME.fullmatch(name or ""):
        raise ValueError(f"'{name}' isn't a subreddit name (only letters, numbers and _). "
                         "Separate several with commas.")
    return name


# ---------- keywords ----------

def keyword_matcher(query):
    """Build a function that checks text against a search like:  voice "ai agent" OR bot -crypto

    - words and "exact phrases" must all appear (any order, any case)
    - OR between two terms means either one is fine
    - -word means the text must NOT contain it
    - a trailing s/es is allowed, so "agent" also matches "agents"
    """
    groups, excludes, pending_or = [], [], False
    for token in re.findall(r'-?"[^"]+"|\S+', query or ""):
        if token.upper() == "OR":
            pending_or = True
            continue
        negative = token.startswith("-") and len(token) > 1
        term = (token[1:] if negative else token).strip('"').lower().strip()
        if not term:
            continue
        pattern = re.compile(r"(?<!\w)" + re.escape(term) + r"(?:s|es)?(?!\w)")
        if negative:
            excludes.append(pattern)
        elif pending_or and groups:
            groups[-1].append(pattern)
        else:
            groups.append([pattern])
        pending_or = False

    def matches(text):
        text = (text or "").lower()
        return (all(any(p.search(text) for p in group) for group in groups)
                and not any(p.search(text) for p in excludes))

    return matches


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
        try:
            delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
            return int((datetime.now(timezone.utc) - delta).timestamp())
        except OverflowError:  # e.g. 99999999d: further back than any calendar goes
            raise ValueError(f"'{text}' is too far back. Reddit started in 2005.")
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
    """Accept a post ID, 't3_' ID or full Reddit URL and return the bare ID.

    Returns None if it doesn't look like a post at all.
    """
    text = (text or "").strip()
    m = re.search(r"/comments/([a-z0-9]+)", text, re.I)
    if m:
        return m.group(1).lower()
    if text.lower().startswith("t3_"):
        text = text[3:]
    text = text.lower()
    return text if POST_ID.fullmatch(text) else None


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
