"""The brain: picks sources, falls back when one fails, removes duplicates.

Both the command line (cli.py) and the web page (web/server.py) call these functions.
"""
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from .http import Unsupported
from .models import Query, _clean, check_subreddit
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
    hints: list = field(default_factory=list)     # plain-English reasons a subreddit found nothing
    completed: list = field(default_factory=list) # sources that finished searching without failing
    checks: list = field(default_factory=list)    # one per search target, used by outcome()


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
        if not failed:
            res.completed.append(name)  # it searched everything it was asked to, even if it found 0
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
    # Any source that finishes returns a post's whole thread, so "completed" means fully searched.
    res.checks.append({
        "label": f"comments for {len(post_ids)} post{'s' if len(post_ids) != 1 else ''}",
        "searched": "full" if res.completed else "none", "found": len(res.items),
        "hint": None, "yours": False, "errors": list(res.errors),
    })
    return res


# ---------- searching several subreddits, or finding them for you ----------

def parse_subreddits(text):
    """'AI_Agents, r/SaaS startups' -> ['AI_Agents', 'SaaS', 'startups']

    A pasted link like https://www.reddit.com/r/SaaS/ works too. Anything that isn't a
    valid subreddit name stops with a clear message (see models.check_subreddit).
    """
    out = []
    for part in re.split(r"[,\s]+", text or ""):
        link = re.search(r"reddit\.com/r/([^/?#]+)", part, re.I)
        name = link.group(1) if link else _clean(part, ("r/", "/r/"))
        if name:
            check_subreddit(name)
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
        one = replace(q, subreddit=subs[0])
        res = single(one, source, log, on_item)
        _record_check(res, one, source, res, log)
        return res
    if not subs and (not q.query or q.author):
        res = single(q, source, log, on_item)  # no keywords to go on, or an author narrows it already
        _record_check(res, q, source, res, log)
        return res

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
            combined.checks.append({
                "label": "finding subreddits", "searched": "none", "found": len(combined.items),
                "hint": "Finding subreddits automatically needs Reddit itself, which isn't answering "
                        "on this network right now. Type one or more subreddits in instead.",
                "yours": False, "errors": [],
            })
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
        _record_check(res, replace(q, subreddit=name), source, combined, log)
    return combined


# ---------- explaining an empty result ----------

def _day(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return f"{d.day} {d:%b %Y}"


# Archives cover any date range. Reddit's live pages (JSON, RSS) only reach the newest posts.
ARCHIVES = ("arctic_shift", "pullpush")
LIVE_ONLY = ("reddit_json", "rss")


def _coverage(res):
    """How thoroughly a search was really done: "full", "recent" (newest posts only) or "none"."""
    if any(name in ARCHIVES for name in res.completed):
        return "full"
    return "recent" if res.completed else "none"


def explain_empty(name, q, searched="full"):
    """Say in plain English why searching r/<name> found nothing.

    Returns (text, yours): yours=True when the cause is in the search itself (a misspelled or
    quiet subreddit, keywords nothing mentions), not a scraper problem. (None, False) if we can't tell.

    Asks the Arctic Shift archive what it knows about the subreddit (a couple of quick
    requests, never to reddit.com) and suggests similarly named, busier subreddits.
    This matters because Arctic Shift answers a misspelled subreddit with the same
    "slow down" error as when it's busy, which would otherwise look like a scraper failure.
    """
    archive = SOURCES["arctic_shift"]
    try:
        info = archive.subreddit_info(name)
        similar = archive.similar_subreddits(name)
    except Exception:
        return None, False  # archive unreachable: the error messages already say what failed
    own_posts = info["posts"] if info else 0
    busier = [(n, c) for n, c in similar if n.lower() != name.lower() and c > max(own_posts, 10)][:3]
    tip = (" Did you mean " + " or ".join(f"r/{n} ({c:,} posts)" for n, c in busier) + "?") if busier else ""

    if info is None:
        return f"r/{name} isn't in the archive, so it probably doesn't exist. Check the spelling.{tip}", True
    try:
        newest = archive.newest_post_time(name)
    except Exception:  # the archive is busy right now; still pass on any suggestion
        return (f"Couldn't check how active r/{name} is right now.{tip}" if tip else None), False
    if newest is None:
        return f"r/{name} has no posts at all.{tip}", True
    if q.since is not None and newest < q.since:
        return (f"r/{name} hasn't had a new post since {_day(newest)}, before your start date, "
                f"so it's quiet or inactive ({info['posts']:,} posts ever). Try a longer date range.{tip}"), True
    if q.query and searched == "full":
        return (f"r/{name} is active, but nothing in this date range mentions {q.query}. "
                f"Try a longer date range, broader keywords or another subreddit.{tip}"), True
    return None, False


def _record_check(res, q, source, into, log):
    """Note how one search target went, so outcome() can say whose fault an empty result is."""
    searched = _coverage(res)
    hint, yours = None, False
    if not res.items and q.subreddit:
        hint, yours = explain_empty(q.subreddit, q, searched)
    if searched == "recent" and source in LIVE_ONLY and not res.items:
        # The user picked a live-only source on purpose: that's a search choice, not a failure.
        hint = hint or (f"You picked {source}, which only reaches the newest posts. "
                        "Use Auto or Arctic Shift to search further back.")
        yours = True
    if hint:
        into.hints.append(hint)
        log("Why nothing was found: " + hint)
    into.checks.append({
        "label": f"r/{q.subreddit}" if q.subreddit else (f"u/{q.author}" if q.author else "your search"),
        "searched": searched, "found": len(res.items), "hint": hint, "yours": yours,
        "errors": list(res.errors),
    })


def outcome(checks, found):
    """Sort a finished run into one plain-English verdict.

    "ok"          found results, everything was searched
    "partial"     found results, but some part couldn't be searched (there may be more)
    "your_search" nothing found, and the scraper worked: change the keywords, subreddit or dates
    "scraper"     nothing found because sources failed: not your search, try again later
    Returns {"verdict", "headline", "details": [...]}.
    """
    broken = [c for c in checks if c["searched"] != "full" and not c["yours"]]
    details = [c["hint"] for c in checks if c["hint"]]
    reasons = []
    for c in broken:
        if c["searched"] == "recent":
            reasons.append(f"{c['label']}: the archives failed, so only Reddit's newest posts were checked")
        for e in c["errors"][:3]:
            reasons.append(f"{c['label']}: {e}")
    fix = ("What to do: wait a few minutes and run it again, or click Check sources. "
           "If it keeps failing, try another network (offices and VPNs often block Reddit).")
    labels = ", ".join(c["label"] for c in broken)

    if found and not broken:
        return {"verdict": "ok", "headline": f"Done: found {found}.", "details": details}
    if found:
        return {"verdict": "partial",
                "headline": f"Found {found}, but the scraper couldn't fully search {labels}. There may be more.",
                "details": details + reasons + [fix]}
    if broken:
        return {"verdict": "scraper",
                "headline": "Scraper problem, not your search: the sources failed, so this isn't a real \"no results\".",
                "details": details + reasons + [fix]}
    return {"verdict": "your_search",
            "headline": "Nothing found, and the scraper worked fine: it's the search itself (see why below).",
            "details": details or ["Try broader or fewer keywords, a longer date range, or another subreddit."]}


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
