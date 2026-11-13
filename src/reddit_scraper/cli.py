"""Command line: `rscrape posts ...`, `rscrape comments ...`, `rscrape ui`, `rscrape check`.

Run `rscrape <command> --help` for every option.
"""
import argparse
import json
import sys

from . import core, export
from .models import Query, parse_when, post_id_from

SOURCE_CHOICES = ["auto", "arctic_shift", "pullpush", "reddit_json", "rss"]

EXAMPLES = """examples:
  rscrape posts -s smallbusiness --since 2025-01-01 --until 2025-01-31 --max 500
  rscrape posts -s smallbusiness -q "refund policy" --since 30d --comments 20
  rscrape posts -q "best crm" --sort top --out results/crm.csv
  rscrape comments --post https://www.reddit.com/r/x/comments/abc123/title/
  rscrape comments -s ecommerce -q "shipping" --since 7d
  rscrape posts -s python --max 20 --out -      (print JSON instead of saving)
  rscrape ui                                    (open the web page)
  rscrape check                                 (see which sources are working)
"""


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def add_filters(p):
    p.add_argument("-s", "--subreddit", help="subreddit name, e.g. smallbusiness")
    p.add_argument("-q", "--query", help='keywords; supports "exact phrase", OR, -exclude')
    p.add_argument("-a", "--author", help="only this Reddit username")
    p.add_argument("--since", help="start date: YYYY-MM-DD, or 7d / 12h / 2w ago")
    p.add_argument("--until", help="end date, inclusive: YYYY-MM-DD, or 1d etc")
    p.add_argument("--source", default="auto", choices=SOURCE_CHOICES,
                   help="where to scrape from (default: auto = try each until one works)")
    p.add_argument("--sort", default="new", choices=list(core.SORTS),
                   help="order of the saved results (default: new)")
    p.add_argument("--out", help="file to save to (default: output/<time>_posts.csv). Use - to print JSON")
    p.add_argument("--format", default="csv", choices=["csv", "json", "both"])


def build_query(a, max_items):
    return Query(
        subreddit=a.subreddit, query=a.query, author=a.author,
        since=parse_when(a.since), until=parse_when(a.until, end_of_day=True),
        max=max_items, listing=getattr(a, "listing", "new"),
    )


def finish(posts, comments, a, sources, errors):
    posts = core.sort_items(posts, a.sort)
    comments = core.sort_items(comments, "top" if a.sort in ("top", "comments") else a.sort)
    if a.out == "-":
        data = {"posts": export.nest(posts, comments)} if posts else {"comments": comments}
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        for f in export.save(posts, comments, out=a.out, fmt=a.format):
            log(f"saved {f}")
    log(f"done: {len(posts)} posts, {len(comments)} comments"
        + (f" (from {', '.join(sources)})" if sources else ""))
    if not posts and not comments:
        log("nothing found." + (" problems: " + "; ".join(errors) if errors else ""))
        return 1
    return 0


def cmd_posts(a):
    q = build_query(a, a.max)
    res = core.scrape_posts(q, a.source, log=log)
    comments, sources, errors = [], list(res.sources), list(res.errors)
    if a.comments and res.items:
        top_n = None if a.comments == "all" else int(a.comments)
        ids = core.posts_to_hydrate(res.items, top_n)
        cres = core.scrape_post_comments(
            ids, a.comments_max, a.source, log=log,
            since=parse_when(a.comments_since), until=parse_when(a.comments_until, end_of_day=True),
        )
        comments = cres.items
        sources += [s for s in cres.sources if s not in sources]
        errors += cres.errors
    return finish(res.items, comments, a, sources, errors)


def cmd_comments(a):
    if a.post:
        ids = [post_id_from(p) for p in a.post]
        res = core.scrape_post_comments([i for i in ids if i], a.max, a.source, log=log)
    else:
        if not (a.subreddit or a.author or a.query):
            log("give --post, or at least one of --subreddit / --author / --query")
            return 2
        res = core.scrape_comments(build_query(a, a.max), a.source, log=log)
    return finish([], res.items, a, res.sources, res.errors)


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


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="rscrape", description="Scrape Reddit posts and comments without logging in.",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("posts", help="scrape posts", epilog=EXAMPLES,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_filters(pp)
    pp.add_argument("--max", type=int, default=100, help="max posts (default 100)")
    pp.add_argument("--listing", default="new", choices=["new", "hot", "top", "rising"],
                    help="which Reddit listing the live sources read (default new)")
    pp.add_argument("--comments", metavar="N|all",
                    help="also fetch comments for the top N posts (by comment count), or 'all'")
    pp.add_argument("--comments-max", type=int, default=500, help="max comments per post (default 500)")
    pp.add_argument("--comments-since", help="only keep comments made on/after this date")
    pp.add_argument("--comments-until", help="only keep comments made on/before this date")
    pp.set_defaults(func=cmd_posts)

    pc = sub.add_parser("comments", help="scrape comments", epilog=EXAMPLES,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    pc.add_argument("--post", action="append", metavar="ID_OR_URL",
                    help="get comments under this post (repeat for several posts)")
    add_filters(pc)
    pc.add_argument("--max", type=int, default=500, help="max comments (per post when using --post)")
    pc.set_defaults(func=cmd_comments)

    pu = sub.add_parser("ui", help="open the web page")
    pu.add_argument("--port", type=int, default=8765)
    pu.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    pu.set_defaults(func=cmd_ui)

    pk = sub.add_parser("check", help="test which sources are working right now")
    pk.set_defaults(func=cmd_check)

    a = p.parse_args(argv)
    if getattr(a, "comments", None) not in (None, "all"):
        try:
            int(a.comments)
        except ValueError:
            p.error("--comments must be a number or 'all'")
    try:
        sys.exit(a.func(a))
    except ValueError as e:
        p.error(str(e))
    except KeyboardInterrupt:
        log("stopped.")
        sys.exit(130)


if __name__ == "__main__":
    main()
