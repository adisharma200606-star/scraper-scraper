"""'What works here': a one-line summary per subreddit, built ONLY from the posts just scraped.

Nothing about any subreddit is hard-coded. For each subreddit we find the top quarter of the
scraped posts by engagement (upvotes + comments), then check how much more (or less) likely a
post with a simple trait (question title, text vs link post, title length, flair, posting
time...) is to land in that top quarter. "2.0x" = twice as likely as posts without it.

Read it as a hint, not a rule: it only reflects this scrape's posts (e.g. only posts matching
your keywords), and correlation isn't cause.
"""
import re
import time
from datetime import datetime
from statistics import median

MIN_POSTS = 10          # fewer scored posts than this and we don't judge
MIN_GROUP = 3           # a trait needs at least this many posts with it AND without it
STRONG = 1.5            # at least 1.5x more (or less) likely to be a top post to count
FRESH_SECONDS = 36 * 3600  # archive scores for newer posts aren't final yet

QUESTION_START = re.compile(
    r"^(how|what|why|when|where|which|who|is|are|does|do|did|can|could|should|would|will|"
    r"anyone|any|has|have|recommend)\b", re.I)
FIRST_PERSON = re.compile(r"\b(i|i'm|i've|my|me|we|we're|our|us)\b", re.I)


def _is_text_post(p):
    url = p.get("url") or ""
    return bool(p.get("selftext")) or "/comments/" in url or not url


TRAITS = [
    ("question titles", lambda p: (p.get("title") or "").strip().endswith("?")
        or bool(QUESTION_START.match((p.get("title") or "").strip()))),
    ("numbers in the title", lambda p: bool(re.search(r"\d", p.get("title") or ""))),
    ("first-person titles (I/my/we)", lambda p: bool(FIRST_PERSON.search(p.get("title") or ""))),
    ("short titles (<40 chars)", lambda p: len(p.get("title") or "") < 40),
    ("long titles (>90 chars)", lambda p: len(p.get("title") or "") > 90),
    ("text posts", _is_text_post),
    ("link/media posts", lambda p: not _is_text_post(p)),
    ("long write-ups (>1500 chars)", lambda p: len(p.get("selftext") or "") > 1500),
]


def _scored(posts, now):
    """Posts whose score we can trust."""
    out = []
    for p in posts:
        if p.get("score") is None:
            continue  # RSS has no scores
        age = now - (p.get("created_utc") or now)
        if p.get("source") in ("arctic_shift", "pullpush") and age < FRESH_SECONDS:
            continue  # archived at posting time; score not final yet
        out.append(p)
    return out


def _engagement(p):
    return (p.get("score") or 0) + (p.get("num_comments") or 0)


def _top_ids(posts):
    """The top ~quarter by engagement. Ties at a low threshold are left out, not let in."""
    values = sorted((_engagement(p) for p in posts), reverse=True)
    cutoff = values[max(0, len(values) // 4 - 1)]
    top = {p["id"] for p in posts if _engagement(p) >= cutoff}
    if len(top) > len(posts) * 0.4:  # too many ties: only count posts strictly above
        top = {p["id"] for p in posts if _engagement(p) > cutoff}
    return top


def _lift(posts, has, top):
    """How many times more likely posts WITH the trait are to be top posts."""
    with_ = [p for p in posts if has(p)]
    without = [p for p in posts if not has(p)]
    if len(with_) < MIN_GROUP or len(without) < MIN_GROUP:
        return None
    rate_with = (sum(p["id"] in top for p in with_) + 0.5) / (len(with_) + 1)
    rate_without = (sum(p["id"] in top for p in without) + 0.5) / (len(without) + 1)
    return rate_with / rate_without


def _best_time(posts, top):
    """The 3-hour window (your computer's local time) where posts most often became top posts."""
    hour_of = {p["id"]: datetime.fromtimestamp(p["created_utc"]).hour for p in posts}
    best, best_lift = None, None
    for start in range(24):
        window = {start, (start + 1) % 24, (start + 2) % 24}
        lift = _lift(posts, lambda p, w=window: hour_of[p["id"]] in w, top)
        if lift is not None and lift >= STRONG and (best_lift is None or lift > best_lift):
            best, best_lift = start, lift
    if best is None:
        return None
    return f"posting {best:02d}:00-{(best + 3) % 24:02d}:00 your time ({best_lift:.1f}x)"


def _flair_traits(posts):
    counts = {}
    for p in posts:
        if p.get("flair"):
            counts[p["flair"]] = counts.get(p["flair"], 0) + 1
    return [(f'"{f}" flair', lambda p, f=f: p.get("flair") == f)
            for f, n in counts.items() if n >= MIN_GROUP]


def summarize(posts, keyword_filtered=False, now=None):
    """Return one insight per subreddit (most-scraped subreddit first)."""
    now = now or time.time()
    by_sub = {}
    for p in posts:
        if p.get("subreddit"):
            by_sub.setdefault(p["subreddit"], []).append(p)

    results = []
    for sub, sub_posts in sorted(by_sub.items(), key=lambda kv: -len(kv[1])):
        scored = _scored(sub_posts, now)
        info = {"subreddit": sub, "posts": len(sub_posts), "scored": len(scored),
                "works": [], "doesnt": [], "best_time": None}
        if len(scored) < MIN_POSTS:
            info["line"] = (f"r/{sub}: not enough scored posts to judge ({len(scored)} of "
                            f"{len(sub_posts)}; need {MIN_POSTS}+ older than 36h). Widen the date range.")
            results.append(info)
            continue

        top = _top_ids(scored)
        if not top:
            info["line"] = f"r/{sub} ({len(scored)} posts): every post did about the same; nothing to compare"
            results.append(info)
            continue
        lifts = []
        for label, has in TRAITS + _flair_traits(scored):
            lift = _lift(scored, has, top)
            if lift is not None:
                lifts.append((label, lift))
        works = sorted([x for x in lifts if x[1] >= STRONG], key=lambda x: -x[1])[:3]
        doesnt = sorted([x for x in lifts if x[1] <= 1 / STRONG], key=lambda x: x[1])[:2]
        info["works"] = [f"{label} ({lift:.1f}x)" for label, lift in works]
        info["doesnt"] = [f"{label} ({lift:.1f}x)" for label, lift in doesnt]
        info["best_time"] = _best_time(scored, top)
        info["median_score"] = median(p["score"] for p in scored)
        info["median_comments"] = median(p.get("num_comments") or 0 for p in scored)

        parts = []
        good = info["works"] + ([info["best_time"]] if info["best_time"] else [])
        if good:
            parts.append("works: " + ", ".join(good))
        if info["doesnt"]:
            parts.append("doesn't: " + ", ".join(info["doesnt"]))
        if not parts:
            parts.append("no clear pattern; nothing stood out")
        parts.append(f"typical post: {info['median_score']:g} upvotes, {info['median_comments']:g} comments")
        line = f"r/{sub} ({len(scored)} posts): " + " | ".join(parts)
        if keyword_filtered:
            line += " [only posts matching your keywords]"
        info["line"] = line
        results.append(info)
    return results
