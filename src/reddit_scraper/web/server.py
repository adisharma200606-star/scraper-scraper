"""A tiny local web server for the scraper page. Uses only Python's standard library.

It only listens on your own computer (127.0.0.1); nobody else on the network can open it.
Results stream to the page line by line as they arrive.
"""
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import core
from ..models import Query, parse_when, post_id_from

PAGE = Path(__file__).with_name("index.html")
ALLOWED_HOSTS = ("127.0.0.1", "localhost")


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
    sources, errors = set(), []
    n_posts = n_comments = 0
    find_n = min(20, max(1, _int(opts.get("find_n"), 5)))
    on_subs = lambda subs: emit({"type": "subreddits", "items": subs})

    def on_post(p):
        nonlocal n_posts
        n_posts += 1
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
    else:
        res = core.search_posts(q, source, log=log, on_item=on_post,
                                find_n=find_n, on_subreddits=on_subs)
        sources.update(res.sources)
        errors += res.errors
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

    emit({"type": "done", "posts": n_posts, "comments": n_comments,
          "sources": sorted(sources), "errors": errors})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep the terminal quiet

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host in ALLOWED_HOSTS:
            return True
        self.send_error(403)
        return False

    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/check":
            self._send(200, json.dumps(list(core.check_sources())), "application/json")
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._host_ok():
            return
        if self.path != "/api/run":
            return self.send_error(404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            opts = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send_error(400)

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(obj):
            try:
                self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
            except OSError:
                raise ClientGone()

        try:
            run_job(opts, emit)
        except ClientGone:
            return
        except Exception as e:
            try:
                emit({"type": "error", "msg": str(e)})
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
