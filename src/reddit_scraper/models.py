"""The shared shape every source converts its data into, plus date helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

POST_FIELDS = [
    "id", "subreddit", "title", "author", "score", "num_comments", "created",
    "flair", "selftext", "url", "permalink", "over_18", "created_utc", "source", "status",
]
COMMENT_FIELDS = [
    "id", "post_id", "parent_id", "subreddit", "author", "body", "score",
    "created", "permalink", "created_utc", "source", "status",
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
    include_removed: bool = False  # keep posts/comments that moderators or Reddit removed

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

# ---------- removed and deleted posts ----------
# Arctic Shift saves a post seconds after it's posted, then checks it again a few days later
# and records whether it was removed. Many posts are: 78 of 100 sampled in one big tech
# subreddit (mostly AutoModerator). Removed posts are left out by default, so nobody learns
# from posts the community didn't keep.

REMOVAL_LABELS = {
    "moderator": "removed by moderators",
    "automod_filtered": "removed by AutoModerator",
    "reddit": "removed by Reddit",
    "anti_evil_ops": "removed by Reddit",
    "community_ops": "removed by Reddit",
    "content_takedown": "removed by Reddit (legal takedown)",
    "copyright_takedown": "removed by Reddit (copyright)",
    "deleted": "deleted by author",
    "author": "deleted by author",
}
LIVE = "live"
NOT_RECHECKED = "not rechecked yet"  # archived before moderators could act; status unknown


def visibility(d, source, text_field):
    """Whether a raw post/comment is still up, removed, or deleted by its author."""
    meta = d.get("_meta") if isinstance(d.get("_meta"), dict) else {}
    category = d.get("removed_by_category") or meta.get("removal_type")
    if category:
        return REMOVAL_LABELS.get(category, f"removed ({category})")
    text = d.get(text_field)
    if text == "[removed]":
        return "removed"
    if text == "[deleted]" or meta.get("was_deleted_later"):
        return "deleted by author"
    # Archives store a snapshot: until Arctic Shift's second check, nobody knows yet.
    # PullPush never rechecks. Reddit's own pages and feeds show the current state.
    if source == "pullpush" or (source == "arctic_shift" and not meta.get("retrieved_2nd_on")):
        return NOT_RECHECKED
    return LIVE


def is_removed(item):
    """True for posts/comments taken down by moderators, AutoModerator or Reddit.
    (Deleted by the author is not counted as removed.)"""
    return str(item.get("status") or "").startswith("removed")


def left_out_message(counts):
    """'left out 57 removed (45 removed by AutoModerator, 12 removed by moderators)'"""
    detail = ", ".join(f"{n} {why}" for why, n in counts.most_common())
    return (f"  left out {sum(counts.values())} removed ({detail}). "
            "To keep them: --include-removed, or 'Removed posts: keep' on the web page")


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
        "status": visibility(d, source, "selftext"),
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
        "status": visibility(d, source, "body"),
    }
