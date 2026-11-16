"""Offline tests: every network call is replaced with fake data, so these run anywhere.

Run with:  pip install -e ".[dev]"  then  pytest
"""
import json

import pytest

from reddit_scraper import core, export
from reddit_scraper.http import SourceError
from reddit_scraper.models import Query, parse_when, post_id_from
from reddit_scraper.sources import arctic_shift, pullpush, reddit_json, rss

# The real versions, kept before the autouse fixture below swaps them for offline fakes.
REAL_SUBREDDIT_INFO = arctic_shift.subreddit_info
REAL_SIMILAR_SUBREDDITS = arctic_shift.similar_subreddits
REAL_NEWEST_POST_TIME = arctic_shift.newest_post_time


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    import time
    from reddit_scraper import http
    monkeypatch.setattr(time, "sleep", lambda s: None)
    http._blocked_until.clear()
    core._down_until.clear()
    # The empty-result explainer makes its own requests. By default pretend the archive's
    # subreddit lookup can't be reached; tests that need it fake it.

    def unreachable(*a, **k):
        raise SourceError("not reachable in tests")
    monkeypatch.setattr(arctic_shift, "subreddit_info", unreachable)
    monkeypatch.setattr(arctic_shift, "similar_subreddits", unreachable)
    monkeypatch.setattr(arctic_shift, "newest_post_time", unreachable)


def fake_post(i, ts, sub="test"):
    return {"id": f"p{i}", "subreddit": sub, "title": f"Post {i}", "author": "alice",
            "score": i, "num_comments": i * 2, "created_utc": ts,
            "permalink": f"/r/{sub}/comments/p{i}/x/", "selftext": "body", "url": "u"}


def fake_comment(i, ts, post="p1"):
    return {"id": f"c{i}", "link_id": f"t3_{post}", "parent_id": f"t3_{post}", "subreddit": "test",
            "author": "bob", "body": f"comment {i}", "score": i, "created_utc": ts}


# ---------- helpers ----------

def test_parse_when():
    assert parse_when("2025-01-01") == 1735689600
    assert parse_when("2025-01-01", end_of_day=True) == 1735689600 + 86400
    assert parse_when("") is None
    assert parse_when("7d") < parse_when("1d")
    with pytest.raises(ValueError):
        parse_when("next tuesday")


def test_post_id_from():
    assert post_id_from("https://www.reddit.com/r/x/comments/1AbC23/some_title/") == "1abc23"
    assert post_id_from("t3_xyz") == "xyz"
    assert post_id_from("xyz") == "xyz"


def test_query_cleans_prefixes():
    q = Query(subreddit="r/SmallBusiness", author="u/someone", query="  ")
    assert (q.subreddit, q.author, q.query) == ("SmallBusiness", "someone", None)


# ---------- archive paging ----------

def test_arctic_pages_backwards_and_dedupes(monkeypatch):
    pages = [
        {"data": [fake_post(i, 1000 - i) for i in range(100)]},
        {"data": [fake_post(99, 901)] + [fake_post(i, 1000 - i) for i in range(100, 150)]},
    ]
    calls = []

    def fake_get_json(url, params=None, **kw):
        calls.append(dict(params))
        return pages[len(calls) - 1]

    monkeypatch.setattr("reddit_scraper.sources._paging.get_json", fake_get_json)
    got = list(arctic_shift.posts(Query(subreddit="test", query="hi", max=1000)))
    assert len(got) == 150
    assert len({p["id"] for p in got}) == 150
    assert calls[0]["query"] == "hi" and calls[0]["subreddit"] == "test"
    assert calls[1]["before"] == 902  # oldest of page 1 (901) + 1
    assert got[0]["permalink"].startswith("https://www.reddit.com/r/test/")


def test_pullpush_respects_max(monkeypatch):
    monkeypatch.setattr("reddit_scraper.sources._paging.get_json",
                        lambda url, params=None, **kw: {"data": [fake_post(i, 5000 - i) for i in range(100)]})
    assert len(list(pullpush.posts(Query(subreddit="test", max=30)))) == 30


# ---------- fallback ----------

def test_auto_falls_back_when_first_source_fails(monkeypatch):
    def broken(q, log=None):
        raise SourceError("HTTP 503")
        yield

    def working(q, log=None):
        for i in range(5):
            yield {"id": f"x{i}", "created_utc": 100 + i}

    monkeypatch.setattr(arctic_shift, "posts", broken)
    monkeypatch.setattr(pullpush, "posts", working)
    res = core.scrape_posts(Query(subreddit="test", max=10))
    assert len(res.items) == 5
    assert res.sources == ["pullpush"]
    assert "arctic_shift" in res.errors[0]


def test_partial_failure_resumes_from_oldest(monkeypatch):
    def dies_midway(q, log=None):
        yield {"id": "a", "created_utc": 500}
        yield {"id": "b", "created_utc": 400}
        raise SourceError("connection reset")

    seen_until = []

    def backup(q, log=None):
        seen_until.append(q.until)
        yield {"id": "b", "created_utc": 400}  # duplicate, should be dropped
        yield {"id": "c", "created_utc": 300}

    monkeypatch.setattr(arctic_shift, "posts", dies_midway)
    monkeypatch.setattr(pullpush, "posts", backup)
    res = core.scrape_posts(Query(subreddit="t", max=10))
    assert [p["id"] for p in res.items] == ["a", "b", "c"]
    assert seen_until == [401]


def test_date_range_is_enforced(monkeypatch):
    def rows(q, log=None):
        yield {"id": "old", "created_utc": 50}
        yield {"id": "ok", "created_utc": 150}
        yield {"id": "new", "created_utc": 250}

    monkeypatch.setattr(arctic_shift, "posts", rows)
    res = core.scrape_posts(Query(since=100, until=200, max=10), source="arctic_shift")
    assert [p["id"] for p in res.items] == ["ok"]


def test_every_source_failing_returns_errors_not_crash(monkeypatch):
    for mod in (arctic_shift, pullpush, reddit_json, rss):
        monkeypatch.setattr(mod, "posts", lambda q, log=None: (_ for _ in ()).throw(SourceError("down")))
    res = core.scrape_posts(Query(subreddit="t"))
    assert res.items == [] and len(res.errors) == 4


# ---------- reddit json + rss parsing ----------

def test_reddit_json_comment_tree(monkeypatch):
    tree = [
        {"data": {"children": [{"kind": "t3", "data": {"id": "p1", "subreddit": "test"}}]}},
        {"data": {"children": [
            {"kind": "t1", "data": dict(fake_comment(1, 10), replies={"data": {"children": [
                {"kind": "t1", "data": dict(fake_comment(2, 11), replies="")},
                {"kind": "more", "data": {"children": ["z1", "z2"]}},
            ]}})},
            {"kind": "t1", "data": dict(fake_comment(3, 12), replies="")},
        ]}},
    ]
    monkeypatch.setattr(reddit_json, "get_json", lambda url, params=None, **kw: tree)
    got = list(reddit_json.comments_for_post("p1", 100))
    assert [c["id"] for c in got] == ["c1", "c2", "c3"]
    assert all(c["post_id"] == "p1" for c in got)


def test_rss_parsing(monkeypatch):
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry><author><name>/u/carol</name></author><category term="test" label="r/test"/>
  <id>t3_abc12</id><link href="https://www.reddit.com/r/test/comments/abc12/hello/"/>
  <published>2025-01-02T03:04:05+00:00</published><title>Hello</title></entry>
</feed>"""

    class Resp:
        content = xml

    calls = []

    def fake_get(url, params=None, **kw):
        calls.append(url)
        return Resp()

    monkeypatch.setattr(rss, "get", fake_get)
    got = list(rss.posts(Query(subreddit="test", max=10)))
    assert len(got) == 1
    p = got[0]
    assert (p["id"], p["author"], p["subreddit"], p["title"]) == ("abc12", "carol", "test", "Hello")
    assert p["created"] == "2025-01-02 03:04"


# ---------- export ----------

def test_export_csv_and_json(tmp_path):
    posts = [dict(fake_post(1, 100), created="x", source="s")]
    comments = [dict(fake_comment(1, 101), post_id="p1")]
    files = export.save(posts, comments, out=tmp_path / "run.csv", fmt="both")
    names = sorted(f.name for f in files)
    assert names == ["run.csv", "run.json", "run_comments.csv"]
    data = json.loads((tmp_path / "run.json").read_text())
    assert data["posts"][0]["comments"][0]["id"] == "c1"
    assert (tmp_path / "run.csv").read_text(encoding="utf-8-sig").startswith("id,subreddit,title")


def test_cli_prints_json(monkeypatch, capsys):
    from reddit_scraper import cli
    monkeypatch.setattr(arctic_shift, "posts", lambda q, log=None: iter([dict(fake_post(1, 100))]))
    with pytest.raises(SystemExit) as e:
        cli.main(["posts", "-s", "test", "--source", "arctic_shift", "--out", "-"])
    assert e.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["posts"][0]["id"] == "p1"


# ---------- keyword matching ----------

def test_keyword_matcher():
    from reddit_scraper.models import keyword_matcher
    m = keyword_matcher("voice ai agent")
    assert m("Building a Voice AI Agents stack")
    assert not m("she said voice agent")          # 'ai' must be a word, not inside 'said'
    m = keyword_matcher('"refund policy" OR returns -spam')
    assert m("our refund policy") and m("Returns are hard")
    assert not m("returns spam") and not m("refund the policy")


# ---------- Arctic Shift keyword fallback ----------

def _arctic_fake(monkeypatch, fail_keyword=True):
    rows = [dict(fake_post(i, 1000 - i), title=t) for i, t in enumerate(
        ["Voice AI agents are here", "cooking tips", "my voice ai agent broke", "random"])]
    calls = []

    def fake_get_json(url, params=None, **kw):
        calls.append(dict(params))
        if fail_keyword and "query" in params:
            raise SourceError("HTTP 422 from arctic-shift: Timeout. Maybe slow down a bit", 422)
        return {"data": rows if "before" not in params else []}

    monkeypatch.setattr("reddit_scraper.sources._paging.get_json", fake_get_json)
    return calls


def test_arctic_keyword_timeout_falls_back_to_local_scan(monkeypatch):
    calls = _arctic_fake(monkeypatch)
    logs = []
    got = list(arctic_shift.posts(Query(subreddit="ai", query="voice ai agent", max=10), logs.append))
    assert [p["title"] for p in got] == ["Voice AI agents are here", "my voice ai agent broke"]
    assert "query" in calls[0] and "query" not in calls[1]
    assert any("one by one" in m for m in logs)


def test_arctic_keyword_without_subreddit_explains(monkeypatch):
    _arctic_fake(monkeypatch)
    with pytest.raises(SourceError, match="Add a subreddit"):
        list(arctic_shift.posts(Query(query="voice ai agent")))


# ---------- blocks and retries ----------

def test_reddit_json_block_is_remembered(monkeypatch):
    calls = []

    def blocked(url, params=None, **kw):
        calls.append(url)
        raise SourceError("HTTP 403 from www.reddit.com", 403)

    monkeypatch.setattr(reddit_json, "get_json", blocked)
    for _ in range(3):
        with pytest.raises(SourceError, match="blocking"):
            list(reddit_json.posts(Query(subreddit="ai")))
    assert len(calls) == 1  # only asked Reddit once, then skipped


def test_server_errors_like_526_are_retried(monkeypatch):
    from reddit_scraper import http

    class R:
        def __init__(self, code):
            self.status_code, self.headers = code, {}

    codes = iter([526, 503, 200])
    monkeypatch.setattr(http.requests, "get", lambda *a, **k: R(next(codes)))
    assert http.get("https://api.pullpush.io/x").status_code == 200


# ---------- RSS ----------

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">{}</feed>"""

ENTRY = """<entry><author><name>/u/{author}</name></author><category term="ai" label="r/ai"/>
<content type="html">{content}</content><id>{eid}</id><link href="{link}"/>
<published>2026-11-0{day}T10:00:00+00:00</published><title>{title}</title></entry>"""


def _rss_fake(monkeypatch, xml):
    class Resp:
        content = xml.encode()

    calls = []
    monkeypatch.setattr(rss, "get", lambda url, params=None, **kw: calls.append(url) or Resp())
    return calls


def test_rss_keyword_filters_locally_with_post_text(monkeypatch):
    body = "&lt;div class=&quot;md&quot;&gt;&lt;p&gt;We built a voice AI agent for clinics&lt;/p&gt;&lt;/div&gt; submitted by /u/x [link] [comments]"
    entries = [
        ENTRY.format(author="a", content=body, eid="t3_aaa", link="https://www.reddit.com/r/ai/comments/aaa/x/", day=3, title="Show and tell"),
        ENTRY.format(author="b", content="", eid="t3_bbb", link="https://www.reddit.com/r/ai/comments/bbb/y/", day=2, title="Unrelated"),
    ]
    calls = _rss_fake(monkeypatch, FEED.format("".join(entries)))
    got = list(rss.posts(Query(subreddit="ai", query="voice ai agent", max=10)))
    assert [p["id"] for p in got] == ["aaa"]
    assert got[0]["selftext"] == "We built a voice AI agent for clinics"
    assert calls[0].endswith("/r/ai/new/.rss")  # normal feed, not the broken search feed


def test_rss_comments_for_post(monkeypatch):
    entries = [
        ENTRY.format(author="op", content="post", eid="t3_aaa", link="https://www.reddit.com/r/ai/comments/aaa/x/", day=1, title="Post"),
        ENTRY.format(author="c1", content="&lt;p&gt;great point&lt;/p&gt;", eid="t1_c1", link="https://www.reddit.com/r/ai/comments/aaa/x/c1/", day=2, title="c1 on Post"),
    ]
    _rss_fake(monkeypatch, FEED.format("".join(entries)))
    got = list(rss.comments_for_post("aaa", 50))
    assert [(c["id"], c["post_id"], c["author"], c["body"]) for c in got] == [("c1", "aaa", "c1", "great point")]


def test_dead_server_is_rested_between_posts(monkeypatch):
    calls = []

    def dead(pid, n, log=None):
        calls.append(pid)
        raise SourceError("api.pullpush.io kept failing (HTTP 526)", 526)
        yield

    for mod in (arctic_shift, reddit_json, rss):
        monkeypatch.setattr(mod, "comments_for_post", lambda pid, n, log=None: iter([]))
    monkeypatch.setattr(pullpush, "comments_for_post", dead)
    core.scrape_post_comments(["a", "b", "c"])
    assert calls == ["a"]  # tried once, then skipped for the next posts


# ---------- subreddit finder ----------

SUB_FEED = FEED.format("".join(
    f'<entry><id>t5_{i}</id><link href="https://www.reddit.com/r/{name}/"/><title>{name} title</title>'
    f'<content type="html">about {name}</content></entry>'
    for i, name in enumerate(["VoiceAI", "AI_Agents", "artificial"])))


def test_rss_find_subreddits(monkeypatch):
    calls = _rss_fake(monkeypatch, SUB_FEED)
    got = rss.find_subreddits("voice ai agent", limit=2)
    assert [s["name"] for s in got] == ["VoiceAI", "AI_Agents"]
    assert calls[0].endswith("/subreddits/search.rss")


def test_find_subreddits_prefers_where_topic_is_active(monkeypatch):
    _rss_fake(monkeypatch, SUB_FEED)
    sample = [{"subreddit": "LocalLLaMA"}, {"subreddit": "LocalLLaMA"}, {"subreddit": "random"}]
    got = core.find_subreddits("voice ai agent", n=4, sample_posts=sample)
    assert got == ["LocalLLaMA", "VoiceAI", "AI_Agents", "artificial"]


def test_search_posts_with_no_subreddit_finds_and_searches_them(monkeypatch):
    found_from_search = [dict(fake_post(90, 5000, sub="LocalLLaMA"), source="rss"),
                         dict(fake_post(91, 5001, sub="LocalLLaMA"), source="rss")]
    monkeypatch.setattr(rss, "search_all", lambda q, log=None: iter(found_from_search))
    monkeypatch.setattr(rss, "find_subreddits", lambda query, limit=10, log=None: [{"name": "VoiceAI"}])
    searched = []

    def arctic(q, log=None):
        searched.append((q.subreddit, q.max))
        yield dict(fake_post(len(searched), 4000, sub=q.subreddit), id=f"{q.subreddit}-1")

    monkeypatch.setattr(arctic_shift, "posts", arctic)
    picked = []
    res = core.search_posts(Query(query="voice ai agent", max=10), find_n=2, on_subreddits=picked.append)
    assert picked == [["LocalLLaMA", "VoiceAI"]]
    assert searched == [("LocalLLaMA", 4), ("VoiceAI", 4)]   # 10 total, 2 used by Reddit search
    assert len(res.items) == 4 and "reddit_search" in res.sources


def test_search_posts_with_a_list_of_subreddits(monkeypatch):
    searched = []

    def arctic(q, log=None):
        searched.append(q.subreddit)
        yield dict(fake_post(1, 4000, sub=q.subreddit), id=q.subreddit)

    monkeypatch.setattr(arctic_shift, "posts", arctic)
    res = core.search_posts(Query(subreddit="AI_Agents, r/SaaS", query="cold email", max=10))
    assert searched == ["AI_Agents", "SaaS"] and len(res.items) == 2


def test_cli_subs_command(monkeypatch, capsys):
    from reddit_scraper import cli
    monkeypatch.setattr(rss, "search_all", lambda q, log=None: iter([{"id": "a", "subreddit": "AI_Agents"},
                                                                      {"id": "b", "subreddit": "AI_Agents"}]))
    monkeypatch.setattr(rss, "find_subreddits", lambda query, limit=10, log=None: [{"name": "VoiceAI"}])
    with pytest.raises(SystemExit) as e:
        cli.main(["subs", "voice ai agent"])
    assert e.value.code == 0
    assert capsys.readouterr().out.splitlines() == ["r/AI_Agents   (2 recent matching posts)", "r/VoiceAI"]


def test_arctic_slow_down_422_is_retried_when_browsing(monkeypatch):
    from reddit_scraper import http

    class R:
        def __init__(self, code, body):
            self.status_code, self.headers, self._body = code, {}, body
        def json(self):
            return self._body

    slow = R(422, {"error": "Timeout. Maybe slow down a bit"})
    ok = R(200, {"data": []})
    codes = iter([slow, ok])
    monkeypatch.setattr(http.requests, "get", lambda *a, **k: next(codes))
    assert http.get("https://arctic-shift.photon-reddit.com/api/x").status_code == 200
    codes = iter([slow, ok])
    with pytest.raises(SourceError):
        http.get("https://arctic-shift.photon-reddit.com/api/x", retry_slowdowns=False)


# ---------- what works here ----------

def _p(i, title, score, sub="test", **kw):
    return dict({"id": f"x{i}", "subreddit": sub, "title": title, "score": score, "num_comments": 1,
                 "created_utc": 1_700_000_000 + i * 3600, "source": "arctic_shift", "selftext": "", "url": ""}, **kw)


def test_insights_spot_what_wins():
    from reddit_scraper import insights
    posts = [_p(i, "How do you price this?", 100) for i in range(6)] + \
            [_p(10 + i, "Launching my new tool today", 2) for i in range(10)]
    [s] = insights.summarize(posts, now=1_800_000_000)
    assert s["works"][0].startswith("question titles")
    assert s["line"].startswith("r/test (16 posts): works: question titles")


def test_insights_skip_unscorable_posts():
    from reddit_scraper import insights
    now = 1_800_000_000
    fresh = [_p(i, "t", 5, created_utc=now - 3600) for i in range(20)]      # under 36h old
    rss_posts = [_p(50 + i, "t", None, source="rss") for i in range(20)]    # no scores
    [s] = insights.summarize(fresh + rss_posts, now=now)
    assert s["scored"] == 0 and "not enough scored posts" in s["line"]


# ---------- Reddit rate limits ----------

def test_reddit_requests_are_spaced_out(monkeypatch):
    from reddit_scraper import http
    import time as real_time
    clock = {"now": 1000.0}
    slept = []
    monkeypatch.setattr(real_time, "time", lambda: clock["now"])
    monkeypatch.setattr(real_time, "sleep", lambda s: (slept.append(s), clock.__setitem__("now", clock["now"] + s)))
    http._last_request_at.clear()
    http._pace("https://www.reddit.com/a")
    http._pace("https://www.reddit.com/b")
    http._pace("https://arctic-shift.photon-reddit.com/x")  # other sites aren't paced
    assert slept == [6.0]


def test_reddit_429_is_retried_only_once(monkeypatch):
    from reddit_scraper import http

    class R:
        status_code, headers = 429, {}
        def json(self):
            raise ValueError

    calls = []
    monkeypatch.setattr(http.requests, "get", lambda *a, **k: calls.append(1) or R())
    monkeypatch.setattr(http, "_pace", lambda url: None)
    with pytest.raises(SourceError):
        http.get("https://www.reddit.com/r/x/new/.rss")
    assert len(calls) == 2


def test_failed_reddit_search_does_not_switch_off_rss(monkeypatch):
    def refuse(url, params=None, **kw):
        raise SourceError("HTTP 403 from www.reddit.com", 403)

    monkeypatch.setattr(rss, "get", refuse)
    with pytest.raises(SourceError, match="refusing requests"):
        list(rss.search_all(Query(query="macbook pro")))
    from reddit_scraper import http
    assert not http.is_blocked("rss")


# ---------- security and robustness ----------

def test_csv_cells_cannot_run_as_formulas(tmp_path):
    posts = [dict(fake_post(1, 100), title='=HYPERLINK("http://evil","click")', selftext="@SUM(1)",
                  author="+bob", score=-5)]
    export.save(posts, [], out=tmp_path / "run.csv")
    text = (tmp_path / "run.csv").read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in text and "'@SUM" in text and "'+bob" in text
    assert ",-5," in text  # real numbers are left alone


def test_export_survives_broken_emoji(tmp_path):
    broken = json.loads('"half an emoji \\ud83d here"')  # Reddit text sometimes has these
    posts = [dict(fake_post(1, 100), title=broken)]
    files = export.save(posts, [], out=tmp_path / "run.csv", fmt="both")
    assert len(files) == 2
    assert "half an emoji ? here" in (tmp_path / "run.csv").read_text(encoding="utf-8-sig")


def test_names_and_post_ids_are_checked():
    assert post_id_from("hello world") is None
    assert post_id_from("../../x?y=1") is None
    with pytest.raises(ValueError, match="isn't a Reddit username"):
        Query(author="bad/name?x")
    with pytest.raises(ValueError, match="isn't a subreddit name"):
        core.parse_subreddits("python, ../../api")
    assert core.parse_subreddits("https://www.reddit.com/r/SaaS/, r/python") == ["SaaS", "python"]


def test_parse_when_far_past_is_a_clear_error():
    with pytest.raises(ValueError, match="too far back"):
        parse_when("99999999d")


def test_rss_refuses_xml_with_entities(monkeypatch):
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><feed>&a;</feed>'

    class Resp:
        content = bomb

    monkeypatch.setattr(rss, "get", lambda url, params=None, **kw: Resp())
    with pytest.raises(SourceError, match="normal Reddit feed"):
        list(rss.posts(Query(subreddit="test", max=10)))


def test_insights_cope_with_missing_dates():
    from reddit_scraper import insights
    posts = [dict(fake_post(i, 1700000000 + i * 3600), source="reddit_json") for i in range(20)]
    posts[0]["created_utc"] = None
    insights.summarize(posts, now=1800000000)  # used to crash


def test_cli_rejects_counts_below_one(capsys):
    from reddit_scraper import cli
    for args in (["posts", "-s", "x", "--max", "0"], ["posts", "-s", "x", "--comments", "-3"]):
        with pytest.raises(SystemExit) as e:
            cli.main(args)
        assert e.value.code == 2


@pytest.fixture
def web():
    """Start the real web server on a free local port; give back a request helper."""
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    from reddit_scraper.web import server

    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def request(method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        h = {"Host": f"127.0.0.1:{port}"}
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    request.port = port
    yield request
    srv.shutdown()
    srv.server_close()


def test_web_page_loads_and_cannot_be_framed(web):
    resp, data = web("GET", "/")
    assert resp.status == 200 and b"Reddit Scraper" in data
    assert resp.getheader("X-Frame-Options") == "DENY"
    assert web("GET", "/../../etc/passwd")[0].status == 404


def test_web_refuses_other_websites(web):
    job = json.dumps({"mode": "comments"})  # fails instantly, no network needed
    json_ct = {"Content-Type": "application/json"}
    assert web("GET", "/", headers={"Host": "evil.example"})[0].status == 403   # DNS rebinding
    assert web("POST", "/api/run", job, dict(json_ct, Origin="https://evil.example"))[0].status == 403
    assert web("POST", "/api/run", job, dict(json_ct, **{"Sec-Fetch-Site": "cross-site"}))[0].status == 403
    assert web("GET", "/api/check", headers={"Sec-Fetch-Site": "cross-site"})[0].status == 403
    assert web("POST", "/api/run", job, {"Content-Type": "text/plain"})[0].status == 415

    ok = dict(json_ct, Origin=f"http://localhost:{web.port}", **{"Sec-Fetch-Site": "same-origin"})
    resp, data = web("POST", "/api/run", job, ok)
    assert resp.status == 200
    assert json.loads(data.splitlines()[0])["type"] == "error"


def test_web_checks_the_request_body(web):
    json_ct = {"Content-Type": "application/json"}
    assert web("POST", "/api/run", "[1]", json_ct)[0].status == 400
    assert web("POST", "/api/run", "{oops", json_ct)[0].status == 400
    # Declared sizes that are too big, or negative (which used to hang the server).
    assert web("POST", "/api/run", "{}", dict(json_ct, **{"Content-Length": "200000"}))[0].status == 413
    assert web("POST", "/api/run", "{}", dict(json_ct, **{"Content-Length": "-1"}))[0].status == 413


# ---------- explaining an empty result ----------

def _subreddit_fakes(monkeypatch, info, similar=(), newest=None):
    monkeypatch.setattr(arctic_shift, "subreddit_info", lambda name: info)
    monkeypatch.setattr(arctic_shift, "similar_subreddits", lambda name: list(similar))
    monkeypatch.setattr(arctic_shift, "newest_post_time", lambda name: newest)


def test_explain_quiet_subreddit_and_suggest_busier_one(monkeypatch):
    _subreddit_fakes(monkeypatch, {"name": "voiceagent", "posts": 3},
                     [("voiceagents", 441), ("VoiceAgentsAI", 3), ("voiceagent", 3)], newest=1_752_000_000)
    hint, yours = core.explain_empty("voiceagent", Query(query="sarvam", since=1_762_000_000))
    assert yours and "hasn't had a new post since" in hint
    assert "r/voiceagents (441 posts)" in hint and "VoiceAgentsAI" not in hint


def test_explain_unknown_subreddit(monkeypatch):
    _subreddit_fakes(monkeypatch, None, [("startups", 282287)])
    hint, yours = core.explain_empty("startupz", Query())
    assert yours and "isn't in the archive" in hint and "r/startups" in hint


def test_explain_active_subreddit_without_matches(monkeypatch):
    _subreddit_fakes(monkeypatch, {"name": "VoiceAgents", "posts": 441}, newest=2_000_000_000)
    hint, yours = core.explain_empty("VoiceAgents", Query(query="sarvam", since=1_762_000_000))
    assert yours and "nothing in this date range mentions sarvam" in hint
    # if the archives couldn't search it, "nothing mentions it" might not be true, so say nothing
    assert core.explain_empty("VoiceAgents", Query(query="sarvam"), searched="none") == (None, False)


def test_explain_says_nothing_when_archive_unreachable():
    assert core.explain_empty("voiceagent", Query()) == (None, False)  # autouse fake: unreachable


def test_explain_still_suggests_when_activity_check_is_busy(monkeypatch):
    _subreddit_fakes(monkeypatch, {"name": "voiceagent", "posts": 3}, [("voiceagents", 441)])

    def busy(name):
        raise SourceError("HTTP 422")
    monkeypatch.setattr(arctic_shift, "newest_post_time", busy)
    hint, yours = core.explain_empty("voiceagent", Query(query="sarvam"))
    assert not yours and "Couldn't check" in hint and "r/voiceagents (441 posts)" in hint


def test_empty_search_reports_why(monkeypatch):
    _subreddit_fakes(monkeypatch, None)
    monkeypatch.setattr(arctic_shift, "posts", lambda q, log=None: iter(()))
    logs = []
    res = core.search_posts(Query(subreddit="r/nopeznope"), source="arctic_shift", log=logs.append)
    assert res.items == [] and "isn't in the archive" in res.hints[0]
    assert any(m.startswith("Why nothing was found") for m in logs)


def test_arctic_subreddit_lookups_parse_the_archive(monkeypatch):
    asked = []

    def fake_get_json(url, params=None, **kw):
        asked.append(dict(params))
        if url.endswith("subreddits/search") and "subreddit" in params:
            return {"data": [{"display_name": "voiceagent", "_meta": {"num_posts": 3}}]}
        if url.endswith("subreddits/search"):
            return {"data": [{"display_name": "voiceagent", "_meta": {"num_posts": 3}},
                             {"display_name": "voiceagents", "_meta": {"num_posts": 441}}]}
        return {"data": [{"created_utc": 1752000000.0}]}
    monkeypatch.setattr(arctic_shift, "get_json", fake_get_json)
    assert REAL_SUBREDDIT_INFO("voiceagent") == {"name": "voiceagent", "posts": 3}
    assert REAL_NEWEST_POST_TIME("voiceagent") == 1752000000
    assert REAL_SIMILAR_SUBREDDITS("voiceagents") == [("voiceagents", 441), ("voiceagent", 3)]
    assert asked[-1]["subreddit_prefix"] == "voiceagent"  # last letter dropped to catch both


# ---------- "your search" vs "scraper problem" ----------

def _sources(monkeypatch, **behaviour):
    """behaviour: source name -> list of posts to return, or an Exception to raise."""
    from reddit_scraper import sources as srcmod
    for name, mod in srcmod.SOURCES.items():
        b = behaviour.get(name, SourceError(f"HTTP 503 from {name}"))

        def posts(q, log=None, b=b):
            if isinstance(b, Exception):
                raise b
            yield from b
        monkeypatch.setattr(mod, "posts", posts)


def test_verdict_your_search_when_sources_worked_but_nothing_matched(monkeypatch):
    _sources(monkeypatch, arctic_shift=[])
    _subreddit_fakes(monkeypatch, {"name": "VoiceAgents", "posts": 441}, newest=2_000_000_000)
    res = core.search_posts(Query(subreddit="VoiceAgents", query="sarvam"))
    o = core.outcome(res.checks, len(res.items))
    assert o["verdict"] == "your_search" and "mentions sarvam" in o["details"][0]


def test_verdict_scraper_problem_when_every_source_failed(monkeypatch):
    _sources(monkeypatch)  # all fail; archive lookup unreachable too (autouse fake)
    res = core.search_posts(Query(subreddit="VoiceAgents", query="sarvam"))
    o = core.outcome(res.checks, len(res.items))
    assert o["verdict"] == "scraper" and "not your search" in o["headline"]
    assert any("HTTP 503" in d for d in o["details"])


def test_verdict_blames_a_typo_not_the_scraper(monkeypatch):
    # Arctic Shift answers a misspelled subreddit with an error, which looks like a failure.
    _sources(monkeypatch, arctic_shift=SourceError("HTTP 422: Timeout. Maybe slow down a bit", 422))
    _subreddit_fakes(monkeypatch, None, [("VoiceAgents", 441)])
    res = core.search_posts(Query(subreddit="VoiceAgentz", query="sarvam"))
    o = core.outcome(res.checks, len(res.items))
    assert o["verdict"] == "your_search" and "doesn't exist" in o["details"][0]


def test_verdict_only_newest_posts_checked_is_not_a_real_no(monkeypatch):
    _sources(monkeypatch, rss=[])  # archives and JSON fail, RSS answers with nothing
    _subreddit_fakes(monkeypatch, {"name": "VoiceAgents", "posts": 441}, newest=2_000_000_000)
    res = core.search_posts(Query(subreddit="VoiceAgents", query="sarvam"))
    o = core.outcome(res.checks, len(res.items))
    assert o["verdict"] == "scraper" and any("only Reddit's newest posts" in d for d in o["details"])


def test_verdict_picking_rss_yourself_is_a_search_choice(monkeypatch):
    _sources(monkeypatch, rss=[])
    _subreddit_fakes(monkeypatch, {"name": "VoiceAgents", "posts": 441}, newest=2_000_000_000)
    res = core.search_posts(Query(subreddit="VoiceAgents", query="sarvam"), source="rss")
    o = core.outcome(res.checks, len(res.items))
    assert o["verdict"] == "your_search" and "only reaches the newest posts" in o["details"][0]


def test_verdict_partial_when_one_subreddit_failed(monkeypatch):
    def posts(q, log=None):
        if q.subreddit == "good":
            yield post_from_reddit_like(1, "good")
        else:
            raise SourceError("HTTP 503")
    from reddit_scraper import sources as srcmod
    for mod in srcmod.SOURCES.values():
        monkeypatch.setattr(mod, "posts", posts)
    res = core.search_posts(Query(subreddit="good, bad", query="x"))
    o = core.outcome(res.checks, len(res.items))
    assert o["verdict"] == "partial" and "r/bad" in o["headline"]


def post_from_reddit_like(i, sub):
    from reddit_scraper.models import post_from_reddit
    return post_from_reddit(dict(fake_post(i, 1_700_000_000, sub=sub), title="x"), "arctic_shift")


def test_cli_exit_codes_tell_search_and_scraper_apart(monkeypatch, capsys, tmp_path):
    from reddit_scraper import cli
    _subreddit_fakes(monkeypatch, {"name": "VoiceAgents", "posts": 441}, newest=2_000_000_000)
    _sources(monkeypatch, arctic_shift=[])
    with pytest.raises(SystemExit) as e:
        cli.main(["posts", "-s", "VoiceAgents", "-q", "sarvam", "--out", str(tmp_path / "a.csv")])
    assert e.value.code == 1 and "YOUR SEARCH:" in capsys.readouterr().err
    _sources(monkeypatch)
    with pytest.raises(SystemExit) as e:
        cli.main(["posts", "-s", "VoiceAgents", "-q", "sarvam", "--out", str(tmp_path / "b.csv")])
    assert e.value.code == 3 and "SCRAPER PROBLEM:" in capsys.readouterr().err


def test_web_job_reports_the_verdict_and_input_mistakes(monkeypatch):
    from reddit_scraper.web import server
    _sources(monkeypatch)
    events = []
    server.run_job({"subreddit": "VoiceAgents", "query": "sarvam"}, events.append)
    assert events[-1]["type"] == "done" and events[-1]["outcome"]["verdict"] == "scraper"
    with pytest.raises(ValueError):  # the web handler turns this into {"type": "error", "yours": True}
        server.run_job({"subreddit": "not a/name"}, events.append)


def test_web_labels_input_mistakes_as_yours(web):
    ct = {"Content-Type": "application/json"}
    _, data = web("POST", "/api/run", json.dumps({"subreddit": "not a/name"}), ct)
    ev = json.loads(data.splitlines()[0])
    assert ev["type"] == "error" and ev["yours"] is True and "subreddit name" in ev["msg"]
