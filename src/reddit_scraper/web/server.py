"""A tiny local web server for the scraper page. Uses only Python's standard library.

It only listens on your own computer (127.0.0.1); nobody else on the network can open it.
Results stream to the page line by line as they arrive.
"""
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import core, insights
from ..models import Query, parse_when, post_id_from

PAGE = Path(__file__).with_name("index.html")
ALLOWED_HOSTS = ("127.0.0.1", "localhost")
MAX_BODY = 100_000  # the form is a few hundred bytes; anything huge isn't from our page

# Sent with the page so other websites can't show it inside a hidden frame and trick clicks.
PAGE_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class ClientGone(BaseException):
    """The browser stopped listening (Stop button or tab closed). Not a source error."""


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def run_job(opts, emit):
    """Run one scrape described by the form on the page."""
    log = lambda m: emit({"type": "log", "msg": m})
    source = opts.get("source") or "auto"
    q = Query(
        subreddit=opts.get("subreddit"), query=opts.get("query"), author=opts.get("author"),
        since=parse_when(opts.get("since")), until=parse_when(opts.get("until"), end_of_day=True),
        max=max(1, _int(opts.get("max"), 100)), listing=opts.get("listing") or "new",
    )
    sources, errors, checks = set(), [], []
    n_posts = n_comments = 0
    all_posts = []
    find_n = min(20, max(1, _int(opts.get("find_n"), 5)))
    on_subs = lambda subs: emit({"type": "subreddits", "items": subs})

    def on_post(p):
        nonlocal n_posts
        n_posts += 1
        all_posts.append(p)
        emit({"type": "post", "item": p})

    def on_comment(c):
        nonlocal n_comments
        n_comments += 1
        emit({"type": "comment", "item": c})

    if opts.get("mode") == "comments":
        post_ids = [post_id_from(x) for x in (opts.get("posts") or "").split()]
        post_ids = [p for p in post_ids if p]
        if post_ids:
            res = core.scrape_post_comments(
                post_ids, q.max, source, log=log, on_item=on_comment, since=q.since, until=q.until)
        elif q.subreddit or q.author or q.query:
            res = core.search_comments(q, source, log=log, on_item=on_comment,
                                       find_n=find_n, on_subreddits=on_subs)
        else:
            raise ValueError("Enter post links, or at least a subreddit, author or keyword.")
        sources.update(res.sources)
        errors += res.errors
        checks += res.checks
    else:
        res = core.search_posts(q, source, log=log, on_item=on_post,
                                find_n=find_n, on_subreddits=on_subs)
        sources.update(res.sources)
        errors += res.errors
        checks += res.checks
        hydrate = opts.get("hydrate") or "none"
        if hydrate != "none" and res.items:
            top_n = None if hydrate == "all" else max(1, _int(opts.get("hydrate_n"), 10))
            ids = core.posts_to_hydrate(res.items, top_n)
            cres = core.scrape_post_comments(
                ids, max(1, _int(opts.get("comments_max"), 500)), source, log=log, on_item=on_comment,
                since=parse_when(opts.get("c_since")),
                until=parse_when(opts.get("c_until"), end_of_day=True),
            )
            sources.update(cres.sources)
            errors += cres.errors
            checks += cres.checks

    if all_posts:
        try:
            summary = insights.summarize(all_posts, keyword_filtered=bool(q.query))
            emit({"type": "insights", "items": [s["line"] for s in summary]})
        except Exception as e:  # a hint isn't worth losing the results over
            log(f"(couldn't work out 'what works here': {e})")
    emit({"type": "done", "posts": n_posts, "comments": n_comments,
          "sources": sorted(sources), "errors": errors,
          "outcome": core.outcome(checks, n_posts + n_comments)})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep the terminal quiet

    def _host_ok(self):
        # Blocks "DNS rebinding": a website whose name secretly points at 127.0.0.1.
        # The browser would still send that website's name here, so we refuse it.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host in ALLOWED_HOSTS:
            return True
        self.send_error(403)
        return False

    def _same_site_ok(self):
        """Only our own page may use the /api/ endpoints.

        Any website you have open could otherwise make your browser send requests here
        (to start scrapes, which uses up Reddit's rate limit). Browsers say where a request
        comes from in the Origin and Sec-Fetch-Site headers, so we check those.
        Tools like curl send neither, and that's fine: they already run on your computer.
        """
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            self.send_error(403, "Requests from other websites aren't allowed")
            return False
        origin = self.headers.get("Origin")
        if origin is not None:
            port = self.server.server_address[1]
            if origin not in [f"http://{h}:{port}" for h in ALLOWED_HOSTS]:
                self.send_error(403, "Requests from other websites aren't allowed")
                return False
        return True

    def _send(self, code, body, ctype, headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8", PAGE_HEADERS)
        elif self.path == "/api/check":
            if self._same_site_ok():
                self._send(200, json.dumps(list(core.check_sources())), "application/json")
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._host_ok():
            return
        if self.path != "/api/run":
            return self.send_error(404)
        if not self._same_site_ok():
            return
        # Our page always sends JSON. Requiring it also means a browser has to ask permission
        # before another website could send it, and this server never gives that permission.
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self.send_error(415, "Send the form as JSON")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.send_error(400)
        if not 0 <= length <= MAX_BODY:
            return self.send_error(413)
        try:
            opts = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send_error(400)
        if not isinstance(opts, dict):
            return self.send_error(400)

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(obj):
            try:
                # ensure_ascii (the default) writes emojis as \u escapes, which the page reads
                # back fine, and copes with broken emoji halves that some Reddit text contains.
                self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
                self.wfile.flush()
            except OSError:
                raise ClientGone()

        try:
            run_job(opts, emit)
        except ClientGone:
            return
        except ValueError as e:  # something typed in the form (a bad name, date or number)
            try:
                emit({"type": "error", "msg": str(e), "yours": True})
            except ClientGone:
                pass
        except Exception as e:  # anything else is a fault in the scraper, not the search
            try:
                emit({"type": "error", "msg": str(e), "yours": False})
            except ClientGone:
                pass


def serve(port=8765, open_browser=True):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"Scraper page running at {url}  (press Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
