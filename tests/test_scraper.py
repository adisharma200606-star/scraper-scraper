"""Offline tests: every network call is replaced with fake data, so these run anywhere.

Run with:  pip install -e ".[dev]"  then  pytest
"""
import json

import pytest

from reddit_scraper import core, export
from reddit_scraper.http import SourceError
from reddit_scraper.models import Query, parse_when, post_id_from
from reddit_scraper.sources import arctic_shift, pullpush, reddit_json, rss


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    import time
    from reddit_scraper import http
    monkeypatch.setattr(time, "sleep", lambda s: None)
    http._blocked_until.clear()
    core._down_until.clear()


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
    assert any("filtering locally" in m for m in logs)


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
