"""Command line: `rscrape posts ...`, `rscrape comments ...`, `rscrape ui`, `rscrape check`.

Run `rscrape <command> --help` for every option.
"""
import argparse
import json
import sys

from . import core, export, insights
from .models import Query, parse_when, post_id_from

SOURCE_CHOICES = ["auto", "arctic_shift", "pullpush", "reddit_json", "rss"]

EXAMPLES = """examples:
  rscrape posts -s smallbusiness --since 2025-01-01 --until 2025-01-31 --max 500
  rscrape posts -s smallbusiness -q "refund policy" --since 30d --comments 20
  rscrape posts -q "voice ai agent" --since 30d       (no subreddit: finds them for you)
  rscrape posts -s "AI_Agents, SaaS, startups" -q "cold email"
  rscrape subs "voice ai agent"                     (just list matching subreddits)
  rscrape comments --post https://www.reddit.com/r/x/comments/abc123/title/
  rscrape comments -s ecommerce -q "shipping" --since 7d
  rscrape posts -s python --max 20 --out -      (print JSON instead of saving)
  rscrape ui                                    (open the web page)
  rscrape check                                 (see which sources are working)
"""


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def positive_int(text):
    """For counts like --max: 0 or a negative number would quietly do nothing useful."""
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' isn't a whole number")
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def add_filters(p):
    p.add_argument("-s", "--subreddit",
                   help="subreddit, or several separated by commas. Leave out to find them automatically")
    p.add_argument("-q", "--query", help='keywords; supports "exact phrase", OR, -exclude')
    p.add_argument("-a", "--author", help="only this Reddit username")
    p.add_argument("--since", help="start date: YYYY-MM-DD, or 7d / 12h / 2w ago")
    p.add_argument("--until", help="end date, inclusive: YYYY-MM-DD, or 1d etc")
    p.add_argument("--source", default="auto", choices=SOURCE_CHOICES,
                   help="where to scrape from (default: auto = try each until one works)")
    p.add_argument("--sort", default="new", choices=list(core.SORTS),
                   help="order of the saved results (default: new)")
    p.add_argument("--find", type=positive_int, default=5, metavar="N",
                   help="with no subreddit: how many subreddits to find and search (default 5)")
    p.add_argument("--scan-max", type=positive_int, default=10000,
                   help="if a keyword search has to filter locally, check at most this many items (default 10000)")
    p.add_argument("--include-removed", action="store_true",
                   help="keep posts/comments that moderators, AutoModerator or Reddit removed "
                        "(left out by default; the 'status' column says what happened)")
    p.add_argument("--out", help="file to save to (default: output/<time>_posts.csv). Use - to print JSON")
    p.add_argument("--format", default="csv", choices=["csv", "json", "both"])


def build_query(a, max_items):
    return Query(
        subreddit=a.subreddit, query=a.query, author=a.author,
        since=parse_when(a.since), until=parse_when(a.until, end_of_day=True),
        max=max_items, listing=getattr(a, "listing", "new"), scan_max=a.scan_max,
        include_removed=a.include_removed,
    )


def finish(posts, comments, a, sources, checks=()):
    posts = core.sort_items(posts, a.sort)
    comments = core.sort_items(comments, "top" if a.sort in ("top", "comments") else a.sort)
    summary = []
    if posts:
        try:
            summary = insights.summarize(posts, keyword_filtered=bool(getattr(a, "query", None)))
        except Exception as e:  # a hint isn't worth losing the results over: still save them
            log(f"(couldn't work out 'what works here': {e})")
    if a.out == "-":
        data = {"posts": export.nest(posts, comments)} if posts else {"comments": comments}
        if summary:
            data["what_works"] = [s["line"] for s in summary]
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        for f in export.save(posts, comments, out=a.out, fmt=a.format, insights=summary):
            log(f"saved {f.resolve()}")
    if summary:
        log("\nWhat works here (from this scrape only):")
        for s in summary:
            log("  " + s["line"])
    log(f"done: {len(posts)} posts, {len(comments)} comments"
        + (f" (from {', '.join(sources)})" if sources else ""))
    return report(core.outcome(list(checks), len(posts) + len(comments)))


# Exit codes, so scripts (and Claude Code) can tell the cases apart without reading the text.
EXIT_CODES = {"ok": 0, "partial": 0, "your_search": 1, "scraper": 3}
LABELS = {"ok": "", "partial": "PARTLY DONE: ", "your_search": "YOUR SEARCH: ", "scraper": "SCRAPER PROBLEM: "}


def report(result):
    """Say clearly whether an empty result is down to the search or to the scraper."""
    if result["verdict"] != "ok" or result["details"]:
        log("")
        log(LABELS[result["verdict"]] + result["headline"])
        for line in result["details"]:
            log("  - " + line)
    return EXIT_CODES[result["verdict"]]


def cmd_posts(a):
    q = build_query(a, a.max)
    res = core.search_posts(q, a.source, log=log, find_n=a.find)
    comments, sources, checks = [], list(res.sources), list(res.checks)
    if a.comments and res.items:
        top_n = None if a.comments == "all" else int(a.comments)
        ids = core.posts_to_hydrate(res.items, top_n)
        cres = core.scrape_post_comments(
            ids, a.comments_max, a.source, log=log,
            since=parse_when(a.comments_since), until=parse_when(a.comments_until, end_of_day=True),
            include_removed=a.include_removed,
        )
        comments = cres.items
        sources += [s for s in cres.sources if s not in sources]
        checks += cres.checks
    return finish(res.items, comments, a, sources, checks)


def cmd_comments(a):
    if a.post:
        ids = []
        for p in a.post:
            pid = post_id_from(p)
            if pid:
                ids.append(pid)
            else:
                log(f"skipping '{p}': not a Reddit post link or ID")
        if not ids:
            return 2
        res = core.scrape_post_comments(ids, a.max, a.source, log=log, include_removed=a.include_removed)
    else:
        if not (a.subreddit or a.author or a.query):
            log("give --post, or at least one of --subreddit / --author / --query")
            return 2
        res = core.search_comments(build_query(a, a.max), a.source, log=log, find_n=a.find)
    return finish([], res.items, a, res.sources, res.checks)


def cmd_subs(a):
    log(f'Finding subreddits for "{a.keywords}"...')
    sample = core._sample_all_of_reddit(Query(query=a.keywords), 50, log)
    names = core.find_subreddits(a.keywords, a.n, log, sample)
    if not names:
        log("none found (is Reddit's RSS reachable? try: rscrape check)")
        return 1
    counts = {}
    for p in sample:
        counts[p.get("subreddit", "").lower()] = counts.get(p.get("subreddit", "").lower(), 0) + 1
    for name in names:
        seen = counts.get(name.lower(), 0)
        print(f"r/{name}" + (f"   ({seen} recent matching posts)" if seen else ""))
    return 0


def cmd_check(a):
    bad = 0
    for r in core.check_sources():
        mark = "OK  " if r["ok"] else "FAIL"
        bad += not r["ok"]
        print(f"{mark} {r['source']:<13} {r['seconds']:>5}s  {r['detail']}", flush=True)
    return 1 if bad == 4 else 0


def cmd_ui(a):
    from .web.server import serve
    serve(port=a.port, open_browser=not a.no_browser)
    return 0


def _safe_output():
    """Make printing never crash on unusual characters.

    On Windows, output sent to a file or another program (like Claude Code) uses an old
    text encoding that can't hold emojis, and some Reddit text has half an emoji in it.
    UTF-8 with errors="replace" prints those as "?" instead of stopping with an error.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # not a normal text stream (rare); leave it as it is


def main(argv=None):
    _safe_output()
    p = argparse.ArgumentParser(
        prog="rscrape", description="Scrape Reddit posts and comments without logging in.",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("posts", help="scrape posts", epilog=EXAMPLES,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_filters(pp)
    pp.add_argument("--max", type=positive_int, default=100, help="max posts (default 100)")
    pp.add_argument("--listing", default="new", choices=["new", "hot", "top", "rising"],
                    help="which Reddit listing the live sources read (default new)")
    pp.add_argument("--comments", metavar="N|all",
                    help="also fetch comments for the top N posts (by comment count), or 'all'")
    pp.add_argument("--comments-max", type=positive_int, default=500, help="max comments per post (default 500)")
    pp.add_argument("--comments-since", help="only keep comments made on/after this date")
    pp.add_argument("--comments-until", help="only keep comments made on/before this date")
    pp.set_defaults(func=cmd_posts)

    pc = sub.add_parser("comments", help="scrape comments", epilog=EXAMPLES,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    pc.add_argument("--post", action="append", metavar="ID_OR_URL",
                    help="get comments under this post (repeat for several posts)")
    add_filters(pc)
    pc.add_argument("--max", type=positive_int, default=500, help="max comments (per post when using --post)")
    pc.set_defaults(func=cmd_comments)

    ps = sub.add_parser("subs", help="find subreddits for some keywords")
    ps.add_argument("keywords", help='e.g. "voice ai agent"')
    ps.add_argument("-n", type=positive_int, default=10, help="how many to show (default 10)")
    ps.set_defaults(func=cmd_subs)

    pu = sub.add_parser("ui", help="open the web page")
    pu.add_argument("--port", type=int, default=8765)
    pu.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    pu.set_defaults(func=cmd_ui)

    pk = sub.add_parser("check", help="test which sources are working right now")
    pk.set_defaults(func=cmd_check)

    a = p.parse_args(argv)
    if getattr(a, "comments", None) not in (None, "all"):
        try:
            ok = int(a.comments) >= 1  # a negative number used to mean "all posts" by accident
        except ValueError:
            ok = False
        if not ok:
            p.error("--comments must be a number (1 or more) or 'all'")
    try:
        sys.exit(a.func(a))
    except ValueError as e:  # something typed in the search itself (a bad name, date or number)
        p.error("check your input: " + str(e))
    except KeyboardInterrupt:
        log("stopped.")
        sys.exit(130)


if __name__ == "__main__":
    main()
