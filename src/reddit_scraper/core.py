"""The brain: picks sources, falls back when one fails, removes duplicates.

Both the command line (cli.py) and the web page (web/server.py) call these functions.
"""
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace

from .http import Unsupported
from .models import Query, _clean
from .sources import SOURCES

# The order sources are tried in when source="auto".
POST_ORDER = ["arctic_shift", "pullpush", "reddit_json", "rss"]
COMMENT_SEARCH_ORDER = ["arctic_shift", "pullpush", "reddit_json", "rss"]
POST_COMMENTS_ORDER = ["arctic_shift", "reddit_json", "rss", "pullpush"]


# A source whose server just failed (after retries) is skipped for a short while,
# so fetching comments for 50 posts doesn't wait on a dead server 50 times.
DOWN_SECONDS = 120
_down_until = {}


@dataclass
class Result:
    items: list = field(default_factory=list)
    sources: list = field(default_factory=list)   # sources that returned something
    errors: list = field(default_factory=list)    # what went wrong along the way


def _quiet(*_):
    pass


def _in_range(item, q):
    ts = item.get("created_utc")
    if ts is None:
        return True
    if q.since is not None and ts < q.since:
        return False
    if q.until is not None and ts >= q.until:
        return False
    return True


def _lazy(make_iter, *args):
    """Delay starting a source so even an instant error is caught as a fallback."""
    yield from make_iter(*args)


def _run(make_iter, order, source, max_items, log, on_item, q=None, res=None, seen=None):
    """Try sources in order until we have enough results or run out of sources.

    A source that errors or finds nothing hands over to the next one. If a source
    fails halfway, the next one carries on from where it stopped instead of
    starting again.
    """
    if source != "auto" and source not in SOURCES:
        raise ValueError(f"Unknown source '{source}'. Choose from: auto, {', '.join(SOURCES)}")
    names = order if source == "auto" else [source]
    res = res if res is not None else Result()
    seen = seen if seen is not None else set()
    start = len(res.items)

    for i, name in enumerate(names):
        remaining = max_items - (len(res.items) - start)
        if remaining <= 0:
            break
        if source == "auto" and time.time() < _down_until.get(name, 0):
            log(f"  {name}: skipped (it failed a moment ago)")
            continue
        log(f"Trying {name}...")
        it = _lazy(make_iter, SOURCES[name], remaining, res.items[start:])
        got, failed = 0, False
        while True:
            try:
                item = next(it)
            except StopIteration:
                break
            except Unsupported as e:
                log(f"  {name}: skipped ({e})")
                failed = True
                break
            except Exception as e:  # any source problem -> fall back, never crash
                res.errors.append(f"{name}: {e}")
                log(f"  {name} failed: {e}")
                failed = True
                status = getattr(e, "status", None)
                if status is None or status >= 500:
                    _down_until[name] = time.time() + DOWN_SECONDS  # server down: rest it
                break
            if not item.get("id") or item["id"] in seen:
                continue
            if q is not None and not _in_range(item, q):
                continue
            seen.add(item["id"])
            res.items.append(item)
            got += 1
            on_item(item)
            if len(res.items) - start >= max_items:
                break
        if got:
            res.sources.append(name)
            log(f"  {name}: got {got}")
        elif not failed:
            log(f"  {name}: no results")
        if len(res.items) - start >= max_items or (got and not failed):
            break
        if i + 1 < len(names):
            log("  falling back to the next source")
    return res


def _resume_point(q, items):
    """When a newest-first source dies halfway, continue from its oldest result."""
    if not items or q.listing != "new":
        return q.until
    oldest = min((it["created_utc"] for it in items if it.get("created_utc")), default=None)
    return oldest + 1 if oldest is not None else q.until


def scrape_posts(q, source="auto", log=None, on_item=None):
    log = log or _quiet
    return _run(
        lambda mod, n, got: mod.posts(replace(q, max=n, until=_resume_point(q, got)), log),
        POST_ORDER, source, q.max, log, on_item or _quiet, q=q,
    )


def scrape_comments(q, source="auto", log=None, on_item=None):
    """Search comments by subreddit / keyword / author / date range."""
    log = log or _quiet
    return _run(
        lambda mod, n, got: mod.comments(replace(q, max=n, until=_resume_point(q, got)), log),
        COMMENT_SEARCH_ORDER, source, q.max, log, on_item or _quiet, q=q,
    )


def scrape_post_comments(post_ids, max_per_post=500, source="auto", log=None, on_item=None,
                         since=None, until=None):
    """Fetch the comments under each of the given posts.

    since/until (unix timestamps) optionally keep only comments made in that window.
    """
    log = log or _quiet
    window = Query(since=since, until=until) if (since or until) else None
    res, seen = Result(), set()
    for i, pid in enumerate(post_ids, 1):
        log(f"Comments for post {pid} ({i}/{len(post_ids)})")
        before = len(res.items)
        _run(
            lambda mod, n, got, pid=pid: mod.comments_for_post(pid, n, log),
            POST_COMMENTS_ORDER, source, max_per_post, log, on_item or _quiet,
            q=window, res=res, seen=seen,
        )
        log(f"  -> {len(res.items) - before} comments")
    res.sources = sorted(set(res.sources))
    return res


# ---------- searching several subreddits, or finding them for you ----------

def parse_subreddits(text):
    """'AI_Agents, r/SaaS startups' -> ['AI_Agents', 'SaaS', 'startups']"""
    out = []
    for part in re.split(r"[,\s]+", text or ""):
        name = _clean(part, ("r/", "/r/"))
        if name and name.lower() not in (o.lower() for o in out):
            out.append(name)
    return out


def find_subreddits(query, n=5, log=None, sample_posts=None):
    """Pick the n subreddits most worth searching for these keywords.

    Two signals, best first:
    1. subreddits where the topic actually came up recently (from Reddit's own search)
    2. communities Reddit's subreddit search says match the keywords
    """
    log = log or _quiet
    counts = Counter(p["subreddit"] for p in (sample_posts or []) if p.get("subreddit"))
    active = [name for name, c in counts.most_common() if c >= 2]
    try:
        matching = [s["name"] for s in SOURCES["rss"].find_subreddits(query, limit=n * 2, log=log)]
    except Exception as e:
        log(f"  Reddit's subreddit search failed: {e}")
        matching = []
    once = [name for name, c in counts.most_common() if c == 1]

    picked = []
    for name in active + matching + once:
        if name.lower() not in (p.lower() for p in picked):
            picked.append(name)
    return picked[:n]


def _sample_all_of_reddit(q, max_items, log, on_item=None):
    """Reddit's own search across every subreddit (via RSS). Returns what it found."""
    found = []
    try:
        for post in SOURCES["rss"].search_all(replace(q, max=max_items), log):
            found.append(post)
            if on_item:
                on_item(post)
    except Exception as e:
        log(f"  Reddit's all-of-Reddit search failed: {e}")
    return found


def _across_subreddits(single, q, source, log, on_item, find_n, on_subreddits, include_search):
    """Run a single-subreddit search over several subreddits (given, or found automatically)."""
    log = log or _quiet
    on_item = on_item or _quiet
    subs = parse_subreddits(q.subreddit)
    if len(subs) == 1:
        return single(replace(q, subreddit=subs[0]), source, log, on_item)
    if not subs and (not q.query or q.author):
        return single(q, source, log, on_item)  # no keywords to go on, or an author narrows it already

    combined, seen = Result(), set()

    def take(item):
        if item.get("id") and item["id"] not in seen:
            seen.add(item["id"])
            combined.items.append(item)
            on_item(item)

    if not subs:
        shown = q.query if '"' in q.query else f'"{q.query}"'
        log(f"No subreddit given, so finding the best ones for {shown}...")
        if include_search and source in ("auto", "rss"):
            log("Searching all of Reddit first...")
            sample = _sample_all_of_reddit(q, max(1, min(100, q.max // 2)), log, take)
            if sample:
                combined.sources.append("reddit_search")
                log(f"  Reddit search: got {len(sample)}")
        else:
            sample = _sample_all_of_reddit(q, 50, log)  # only used to rank subreddits
        subs = find_subreddits(q.query, find_n, log, sample)
        if not subs:
            log("Couldn't find subreddits automatically (finding them needs Reddit itself). "
                "Type one or more subreddits in and try again; that works through Arctic Shift.")
            combined.errors.append("no subreddits found")
            return combined
        log("Searching: " + ", ".join(f"r/{s}" for s in subs))
        if on_subreddits:
            on_subreddits(subs)

    budget = max(1, q.max - len(combined.items))
    per_sub = max(1, math.ceil(budget / len(subs)))
    for name in subs:
        log(f"--- r/{name} ---")
        res = single(replace(q, subreddit=name, max=per_sub), source, log, take)
        combined.sources += [s for s in res.sources if s not in combined.sources]
        combined.errors += [f"r/{name}: {e}" for e in res.errors]
    return combined


def search_posts(q, source="auto", log=None, on_item=None, find_n=5, on_subreddits=None):
    """Like scrape_posts, but the subreddit can be empty (found for you) or a list."""
    return _across_subreddits(scrape_posts, q, source, log, on_item, find_n, on_subreddits, True)


def search_comments(q, source="auto", log=None, on_item=None, find_n=5, on_subreddits=None):
    """Like scrape_comments, but the subreddit can be empty (found for you) or a list."""
    return _across_subreddits(scrape_comments, q, source, log, on_item, find_n, on_subreddits, False)


def posts_to_hydrate(posts, top_n):
    """Pick which posts to fetch comments for: the top N by comment count, or all."""
    ranked = sorted(posts, key=lambda p: p.get("num_comments") or 0, reverse=True)
    ranked = [p for p in ranked if p.get("num_comments") != 0]
    return [p["id"] for p in (ranked if top_n is None or top_n < 0 else ranked[:top_n])]


SORTS = {
    "new": (lambda x: x.get("created_utc") or 0, True),
    "old": (lambda x: x.get("created_utc") or 0, False),
    "top": (lambda x: x.get("score") if x.get("score") is not None else -10**9, True),
    "comments": (lambda x: x.get("num_comments") or 0, True),
}


def sort_items(items, how="new"):
    key, reverse = SORTS.get(how, SORTS["new"])
    return sorted(items, key=key, reverse=reverse)


def check_sources():
    """Quick health check: one tiny request to each source. Yields one result at a time."""
    for name, mod in SOURCES.items():
        t0 = time.time()
        try:
            mod.ping()
            ok, detail = True, "working"
        except Exception as e:
            ok, detail = False, str(e)
        yield {"source": name, "ok": ok, "detail": detail, "seconds": round(time.time() - t0, 1)}
