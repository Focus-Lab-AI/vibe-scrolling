"""Pure-parse tests for the real-platform streaming adapters (no network)."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.stream.adapters.instagram import InstagramAdapter, parse_comments
from src.stream.adapters.linkedin import LinkedInAdapter, dom_items_to_posts
from src.stream.adapters.registry import ADAPTERS, HOME_URLS, get_adapter
from src.stream.adapters.threads import ThreadsAdapter
from src.stream.adapters.x import XAdapter, parse_tweet_detail
from src.stream.adapters.youtube import YouTubeAdapter
from src.stream.contracts import PlatformAdapter

FIX = Path(__file__).parent / "fixtures"
REPLY_KEYS = {"id", "text", "author_handle", "author_name", "created_at", "likes"}


def load(rel: str):
    p = FIX / rel
    return json.loads(p.read_text()) if p.suffix == ".json" else p.read_text()


def resp(url: str, status: int = 200):
    return SimpleNamespace(url=url, status=status)


def by_id(posts):
    return {p.id: p for p in posts}


# ------------------------------------------------------------------ protocol

ALL = [XAdapter, ThreadsAdapter, InstagramAdapter, YouTubeAdapter, LinkedInAdapter]


@pytest.mark.parametrize("cls", ALL)
def test_protocol_conformance(cls):
    a = cls()
    assert isinstance(a, PlatformAdapter)
    assert a.platform and a.home_url.startswith("https://")


def test_registry():
    assert set(ADAPTERS) == {"x", "threads", "instagram", "youtube", "linkedin"}
    for name, a in ADAPTERS.items():
        assert a.platform == name
        assert isinstance(a, PlatformAdapter)
        assert HOME_URLS[name] == a.home_url
    assert get_adapter("twitter") is ADAPTERS["x"]
    assert get_adapter("X") is ADAPTERS["x"]
    with pytest.raises(KeyError):
        get_adapter("myspace")


def test_spine_build_adapter_discovers_classes():
    from src.stream.adapters import build_adapter

    for name in ("x", "threads", "instagram", "youtube", "linkedin"):
        a = build_adapter(name)
        assert a is not None and a.platform == name


# -------------------------------------------------------------------- purity

PURITY_CASES = [
    (XAdapter, "x/home_timeline.json", "https://x.com/i/api/graphql/abc/HomeTimeline"),
    (ThreadsAdapter, "threads/feed.json", "https://www.threads.com/graphql/query"),
    (InstagramAdapter, "instagram/feed_graphql.json", "https://www.instagram.com/graphql/query"),
    (YouTubeAdapter, "youtube/browse_continuation.json", "https://www.youtube.com/youtubei/v1/browse"),
    (LinkedInAdapter, "linkedin/voyager_feed.json", "https://www.linkedin.com/voyager/api/graphql?q=feed"),
]


@pytest.mark.parametrize("cls,fixture,url", PURITY_CASES)
def test_parse_body_is_pure(cls, fixture, url):
    a = cls()
    body = load(fixture)
    snapshot = copy.deepcopy(body)
    state_before = copy.deepcopy(vars(a._parser))
    first = [asdict(p) for p in a.parse_body(url, body)]
    second = [asdict(p) for p in a.parse_body(url, body)]
    assert first and first == second
    assert body == snapshot, "parse_body must not mutate the body"
    assert vars(a._parser) == state_before, "no accumulation in the wrapped interceptor"
    assert all(p["platform"] == a.platform for p in first)
    assert len({p["id"] for p in first}) == len(first)


@pytest.mark.parametrize("cls", ALL)
def test_parse_body_tolerates_garbage(cls):
    a = cls()
    for body in ({}, {"data": None}, {"data": {"x": 1}}, [], None, {"included": "nope"}):
        assert a.parse_body("https://example.com", body) == []


# ------------------------------------------------------------------------- X


def test_x_matches():
    a = XAdapter()
    assert a.matches(resp("https://x.com/i/api/graphql/Q1/HomeTimeline?variables=%7B%7D"))
    assert a.matches(resp("https://x.com/i/api/graphql/Q2/HomeLatestTimeline"))
    assert not a.matches(resp("https://x.com/i/api/graphql/Q3/TweetDetail"))
    assert not a.matches(resp("https://x.com/home"))
    assert not a.matches(resp("https://abs.twimg.com/responsive-web/client-web/main.js"))


def test_x_parse_body():
    posts = by_id(XAdapter().parse_body("u", load("x/home_timeline.json")))
    assert set(posts) == {"1001", "1002", "1003", "1004", "1005", "1006", "1007"}

    p = posts["1001"]
    assert (p.author_handle, p.author_name, p.likes, p.reposts, p.replies, p.quotes) == (
        "alice", "Alice A", 12, 3, 4, 1,
    )
    assert p.url == "https://x.com/alice/status/1001"
    assert p.media_urls == ["https://pbs.twimg.com/media/a.jpg"]
    assert p.video_urls == ["https://video.twimg.com/high.mp4"]
    assert not p.is_ad

    assert posts["1002"].is_ad and posts["1002"].author_handle == "brand"

    rt = posts["1003"]
    assert rt.is_repost and rt.original_author == "bob"
    assert rt.author_handle == "carol" and rt.text == "original text" and rt.likes == 99
    assert rt.url == "https://x.com/carol/status/900"

    assert posts["1004"].text.startswith("This is the full long-form note tweet")
    assert posts["1005"].text == "Thread start" and posts["1006"].text == "Thread reply"
    assert posts["1007"].quoted_post["id"] == "800"
    assert posts["1007"].quoted_post["author_handle"] == "gina"


def test_x_parse_tweet_detail():
    replies = parse_tweet_detail(load("x/tweet_detail.json"), "1001")
    assert [r["id"] for r in replies] == ["2001", "2002", "2003"]
    for r in replies:
        assert REPLY_KEYS <= set(r)
    assert replies[0]["author_handle"] == "rhea" and replies[0]["likes"] == 7
    assert replies[1]["author_handle"] == "sam"
    assert replies[1]["text"].endswith("is complete here."), "note_tweet must win over full_text"
    assert parse_tweet_detail(load("x/tweet_detail.json"), "1001", limit=1) == replies[:1]
    assert parse_tweet_detail({}, "1001") == []


# ------------------------------------------------------------------- Threads


def test_threads_matches():
    a = ThreadsAdapter()
    assert a.matches(resp("https://www.threads.com/graphql/query"))
    assert a.matches(resp("https://www.threads.net/graphql/query"))
    assert not a.matches(resp("https://www.threads.com/api/graphql"))
    assert not a.matches(resp("https://www.instagram.com/graphql/query"))


def test_threads_parse_body():
    a = ThreadsAdapter()
    posts = by_id(a.parse_body("u", load("threads/feed.json")))
    assert set(posts) == {"3101", "3102", "3103", "3104"}
    p = posts["3101"]
    assert (p.author_handle, p.author_name, p.text, p.likes, p.replies, p.reposts, p.quotes) == (
        "hana", "Hana H", "Morning run thoughts", 21, 5, 2, 1,
    )
    assert p.media_urls == ["https://cdn.threads/big.jpg"]
    assert p.video_urls == ["https://cdn.threads/v.mp4"]
    assert p.url.endswith("/@hana/post/AbC1")
    assert not p.is_ad
    assert posts["3102"].is_ad  # paid partnership (legacy signal)
    assert posts["3103"].is_ad  # ad_id (adapter signal)
    rp = posts["3104"]
    assert rp.is_repost and rp.original_author == "ivan"
    assert rp.quoted_post["author_handle"] == "jules" and rp.quoted_post["text"] == "The original"


def test_threads_nested_feed_data_and_non_feed_query():
    a = ThreadsAdapter()
    nested = {"data": {"data": load("threads/feed.json")["data"]}}
    assert len(a.parse_body("u", nested)) == 4
    assert a.parse_body("u", {"data": {"viewer": {"id": 1}}}) == []


# ----------------------------------------------------------------- Instagram


def test_instagram_matches():
    a = InstagramAdapter()
    assert a.matches(resp("https://www.instagram.com/graphql/query"))
    assert not a.matches(resp("https://www.instagram.com/api/v1/feed/reels_tray/"))
    assert not a.matches(resp("https://www.threads.com/graphql/query"))


def test_instagram_parse_body():
    posts = by_id(InstagramAdapter().parse_body("u", load("instagram/feed_graphql.json")))
    assert set(posts) == {"4101", "4102", "4103"}  # 4104 has no code -> skipped
    p = posts["4101"]
    assert (p.author_handle, p.text, p.likes, p.replies) == ("kai", "Sunset", 120, 8)
    assert p.media_urls == ["https://cdn.ig/l.jpg"] and p.url == "https://www.instagram.com/p/IGa/"
    assert not p.is_ad
    assert posts["4102"].is_ad and posts["4102"].video_urls == ["https://cdn.ig/ad.mp4"]
    c = posts["4103"]
    assert c.author_handle == "lea" and c.media_urls == ["https://cdn.ig/c1.jpg", "https://cdn.ig/c2.jpg"]


def test_instagram_parse_body_other_query():
    assert InstagramAdapter().parse_body("u", {"data": {"xdt_viewer": {}}}) == []


def test_instagram_parse_html():
    a = InstagramAdapter()
    html = load("instagram/home.html")
    posts = by_id(a.parse_html(html))
    assert set(posts) == {"4201", "4202"}
    assert posts["4201"].text == "From the HTML" and not posts["4201"].is_ad
    assert posts["4202"].is_ad
    assert [asdict(p) for p in a.parse_html(html)] == [asdict(p) for p in a.parse_html(html)]


def test_instagram_parse_comments():
    html = load("instagram/post_comments.html")
    out = parse_comments(html)
    assert [c["id"] for c in out] == ["5001", "5002"]
    assert out[0]["author_handle"] == "nina" and out[0]["likes"] == 4
    assert out[1]["author_name"] == "omar"
    assert all(REPLY_KEYS <= set(c) for c in out)
    assert parse_comments(html, limit=1) == out[:1]


# ------------------------------------------------------------------- YouTube


def test_youtube_matches():
    a = YouTubeAdapter()
    assert a.matches(resp("https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"))
    assert not a.matches(resp("https://www.youtube.com/youtubei/v1/browse", status=403))
    assert not a.matches(resp("https://www.youtube.com/youtubei/v1/next"))
    assert not a.matches(resp("https://www.youtube.com/youtubei/v1/log_event"))


def test_youtube_parse_body():
    posts = by_id(YouTubeAdapter().parse_body("u", load("youtube/browse_continuation.json")))
    assert set(posts) == {"vid00000001", "short000001", "short000002", "ad:0:1760868000:0:42"}
    v = posts["vid00000001"]
    assert (v.text, v.author_handle, v.created_at) == ("How to focus deeply", "Deep Work Channel", "3 days ago")
    assert v.media_urls == ["https://i.ytimg.com/vi/vid00000001/hq.jpg"]
    assert v.platform_data["views"] == "1.2M views" and v.platform_data["duration"] == "12:34"
    assert not v.is_ad
    s = posts["short000001"]
    assert s.platform_data["type"] == "short" and s.text == "Tiny habit"
    ad = posts["ad:0:1760868000:0:42"]
    assert ad.is_ad and ad.text == "Try our productivity app"


def test_youtube_reload_command_and_initial_data():
    a = YouTubeAdapter()
    items = load("youtube/browse_continuation.json")["onResponseReceivedActions"][0][
        "appendContinuationItemsAction"
    ]["continuationItems"]
    body = {"onResponseReceivedCommands": [{"reloadContinuationItemsCommand": {"continuationItems": items}}]}
    assert len(a.parse_body("u", body)) == 4

    posts = a.parse_initial_data(load("youtube/yt_initial_data.json"))
    assert [p.id for p in posts if not p.is_ad] == ["vidInit0001"]
    ads = [p for p in posts if p.is_ad]
    assert len(ads) == 1 and ads[0].text == "Ad title" and ads[0].id.startswith("ad:")
    # hashed ad id is stable across calls
    assert [p.id for p in a.parse_initial_data(load("youtube/yt_initial_data.json"))] == [p.id for p in posts]
    assert a.parse_initial_data({}) == []


# ------------------------------------------------------------------ LinkedIn


def test_linkedin_matches():
    a = LinkedInAdapter()
    assert a.matches(resp("https://www.linkedin.com/voyager/api/graphql?queryId=voyagerFeedDashMainFeed.abc"))
    assert a.matches(resp("https://www.linkedin.com/voyager/api/feed/updatesV2?count=10"))
    assert not a.matches(resp("https://www.linkedin.com/voyager/api/feed/updatesV2", status=500))
    assert not a.matches(resp("https://www.linkedin.com/voyager/api/me"))
    assert not a.matches(resp("https://www.linkedin.com/feed/"))


def test_linkedin_parse_body():
    posts = by_id(LinkedInAdapter().parse_body("u", load("linkedin/voyager_feed.json")))
    assert set(posts) == {
        "urn:li:activity:7001",
        "urn:li:activity:7002",
        "urn:li:activity:7003",
        "urn:li:activity:6999",
    }
    p = posts["urn:li:activity:7001"]
    assert (p.author_handle, p.author_name, p.text) == ("priya-p", "Priya P", "Shipped a thing today.")
    assert (p.likes, p.replies, p.reposts) == (42, 6, 2)
    assert p.media_urls == ["https://media.licdn.com/img/large.jpg"]
    assert p.created_at == "2h"
    assert p.platform_data["author_headline"] == "Engineer at Somewhere"
    assert not p.is_ad
    ad = posts["urn:li:activity:7002"]
    assert ad.is_ad and ad.author_handle == "acme"
    assert ad.video_urls == ["https://dms.licdn.com/720.mp4"]
    assert ad.media_urls == ["https://media.licdn.com/thumb/t.jpg"]
    rs = posts["urn:li:activity:7003"]
    assert rs.is_repost and rs.quoted_post["text"] == "Original article post"


def test_linkedin_dom_items():
    items = [
        {"id": "urn:li:activity:1", "activity_id": "1", "text": "hi", "author_name": "Z",
         "url": "https://www.linkedin.com/feed/update/urn:li:activity:1/"},
        {"id": "urn:li:activity:1", "text": "dup"},
        {"id": ""},
    ]
    posts = dom_items_to_posts(items)
    assert [p.id for p in posts] == ["urn:li:activity:1"]
    assert posts[0].platform_data["source"] == "dom_fallback"
