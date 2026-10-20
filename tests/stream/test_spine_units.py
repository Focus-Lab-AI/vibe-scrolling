"""Unit tests for the streaming spine (no browser)."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from src.models import Post
from src.stream.adapters.fake import FakeAdapter
from src.stream.event_log import read_events
from src.stream.hub import HubConfig, StreamHub
from src.stream.producer import DemandSignal
from tests.stream.conftest import KeywordCurator
from tests.stream.fake_site import make_comments, make_feed_page


def mk(pid: str, text: str = "", *, platform: str = "fake", is_ad: bool = False, media=None) -> Post:
    return Post(id=pid, platform=platform, text=text, author_handle="a", author_name="A",
                created_at="2026-10-20", is_ad=is_ad, media_urls=list(media or []))


async def wait_for(cond, timeout: float = 5.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(interval)
    raise AssertionError("condition not met in time")


def make_hub(tmp_path, curator=None, **cfg) -> StreamHub:
    cfg.setdefault("batch_window", 0.05)
    return StreamHub(None, {}, curator or KeywordCurator(), config=HubConfig(**cfg),
                     streams_dir=tmp_path / "streams", session_id="s1")


# --------------------------------------------------------------------- demand
def test_demand_watermark_hysteresis():
    d = DemandSignal(low=8, high=20)
    assert d.wanting
    assert d.update(19) is True
    assert d.update(20) is False          # hit high → stop
    assert d.update(12) is False          # between watermarks → still stopped
    assert d.update(8) is True            # back to low → resume
    assert d.update(15) is True           # between → keep going
    with pytest.raises(ValueError):
        DemandSignal(low=5, high=5)


# --------------------------------------------------------------------- prefilter
@pytest.mark.asyncio
async def test_prefilter_dedupe_ads_empty(tmp_path):
    hub = make_hub(tmp_path)
    await hub.start()
    sub = hub.subscribe(buffer_target=100)
    posts = [
        mk("1", "A perfectly normal post about the weather today"),
        mk("1", "A perfectly normal post about the weather today"),   # dup id
        mk("2", "  a perfectly NORMAL post about the weather   today "),  # dup text
        mk("3", "Buy now!!! limited offer for everyone", is_ad=True),
        mk("4", ""),                                                  # empty
        mk("5", "", media=["http://x/img.jpg"]),                       # media-only ok
        mk("6", "How I learned machine learning — a guide"),
    ]
    for p in posts:
        await hub.raw_queue.put(p)
    await wait_for(lambda: hub.counters["delivered"] >= 3)
    await asyncio.sleep(0.2)
    delivered = []
    while not sub.queue.empty():
        delivered.append(sub.queue.get_nowait())
    ids = [m["post"]["id"] for m in delivered if m["type"] == "post.curated"]
    assert sorted(ids) == ["1", "5", "6"]
    assert hub.counters["dup_id"] == 1 and hub.counters["dup_text"] == 1

    events = [e for e in read_events(hub.session_dir) if e["type"] == "post.curated"]
    by_id = {e["post"]["id"]: e for e in events}
    assert by_id["3"]["verdict"]["action"] == "drop" and by_id["3"]["verdict"]["stage"] == "prefilter"
    assert "ad" in by_id["3"]["verdict"]["flags"]
    assert by_id["4"]["verdict"]["action"] == "drop"
    assert by_id["2"]["verdict"]["action"] == "drop" and "duplicate" in by_id["2"]["verdict"]["flags"]
    # dropped events carry no post body
    assert "text" not in by_id["3"]["post"]
    assert by_id["1"]["post"]["text"].startswith("A perfectly")
    await hub.close()


# --------------------------------------------------------------------- batching
@pytest.mark.asyncio
async def test_batching_respects_max_batch_and_window(tmp_path):
    cur = KeywordCurator(max_batch=4)
    hub = make_hub(tmp_path, cur, batch_window=0.3)
    await hub.start()
    # A phone with a full buffer → not urgent → batches fill up to max/window.
    sub = hub.subscribe(buffer_target=100)
    hub.update_demand(sub, 50)
    for i in range(10):
        await hub.raw_queue.put(mk(f"p{i}", f"neutral chatter number {i} about nothing much"))
    await wait_for(lambda: sum(len(b) for b in cur.batches) == 10)
    assert all(len(b) <= 4 for b in cur.batches)
    assert len(cur.batches) <= 4  # mostly full batches, not 10 singletons
    await hub.close()


@pytest.mark.asyncio
async def test_batching_urgent_when_phone_low(tmp_path):
    cur = KeywordCurator(max_batch=8)
    hub = make_hub(tmp_path, cur, batch_window=5.0)
    await hub.start()
    hub.subscribe(buffer_target=100)  # depth 0 < low watermark → urgent
    t0 = time.monotonic()
    await hub.raw_queue.put(mk("x1", "neutral chatter that should ship right away"))
    await wait_for(lambda: hub.counters["delivered"] == 1, timeout=2.0)
    assert time.monotonic() - t0 < 1.0  # did not wait for the 5s window
    await hub.close()


@pytest.mark.asyncio
async def test_inflight_limit_and_fallback(tmp_path):
    cur = KeywordCurator(max_batch=1, delay=0.2)
    hub = make_hub(tmp_path, cur, max_inflight=2)
    await hub.start()
    hub.subscribe(buffer_target=100)
    for i in range(6):
        await hub.raw_queue.put(mk(f"q{i}", f"neutral chatter number {i} about nothing"))
    await wait_for(lambda: hub.counters["delivered"] == 6, timeout=5)
    assert cur.max_inflight_seen == 2
    await hub.close()

    failing = KeywordCurator(fail=True)
    hub2 = StreamHub(None, {}, failing, config=HubConfig(batch_window=0.01),
                     streams_dir=tmp_path / "s2", session_id="s2")
    await hub2.start()
    sub = hub2.subscribe(buffer_target=10)
    await hub2.raw_queue.put(mk("f1", "neutral chatter that the curator chokes on"))
    await wait_for(lambda: hub2.counters["delivered"] == 1)
    msg = sub.queue.get_nowait()
    assert msg["verdict"]["stage"] == "fallback" and msg["verdict"]["action"] == "keep"
    await hub2.close()


# --------------------------------------------------------------------- ready buffer / demand
@pytest.mark.asyncio
async def test_boost_jumps_ahead_and_buffer_target(tmp_path):
    hub = make_hub(tmp_path, KeywordCurator(max_batch=8), batch_window=0.2)
    await hub.start()
    # No subscriber yet: posts accumulate in the ready buffer.
    for i in range(3):
        await hub.raw_queue.put(mk(f"n{i}", f"neutral chatter number {i} about nothing"))
    await hub.raw_queue.put(mk("g1", "Lessons from my machine learning tutorial journey"))
    await wait_for(lambda: hub.ready_count() == 4)
    sub = hub.subscribe(buffer_target=2)
    first = [sub.queue.get_nowait(), sub.queue.get_nowait()]
    assert sub.queue.empty()  # buffer_target respected
    assert first[0]["post"]["id"] == "g1" and first[0]["verdict"]["action"] == "boost"
    assert hub.ready_count() == 2
    # demand level = phone depth (2) + ready (2)
    assert hub.demand.level == 4
    hub.update_demand(sub, 0)
    assert sub.queue.qsize() == 2 and hub.ready_count() == 0
    await hub.close()


@pytest.mark.asyncio
async def test_demand_level_drives_signal(tmp_path):
    hub = make_hub(tmp_path, low_watermark=3, high_watermark=6)
    await hub.start()
    sub = hub.subscribe(buffer_target=50)
    hub.update_demand(sub, 10)
    assert hub.demand.wanting is False
    hub.update_demand(sub, 5)
    assert hub.demand.wanting is False
    hub.update_demand(sub, 2)
    assert hub.demand.wanting is True
    await hub.close()


# --------------------------------------------------------------------- comments + filtered
class _Pool:
    async def get_context(self, platform):
        return object()


class _CommentsAdapter:
    platform = "fake"
    home_url = "http://fake/"

    async def fetch_comments(self, context, post, limit=50):
        return [{"id": c["id"], "text": c["text"], "author_handle": c["author"]["handle"]}
                for c in make_comments(post.id)]


@pytest.mark.asyncio
async def test_open_comments_maps_actions(tmp_path):
    hub = StreamHub(_Pool(), {"fake": _CommentsAdapter()}, KeywordCurator(),
                    config=HubConfig(batch_window=0.01), streams_dir=tmp_path, session_id="c")
    await hub.start(platforms=[])
    await hub.raw_queue.put(mk("p7", "My cat discovered the box"))
    await wait_for(lambda: hub.find_post("p7") is not None)
    thread = await hub.open_comments("p7", "r1")
    actions = {r["reply"]["id"]: r["action"] for r in thread.replies}
    assert thread.request_id == "r1" and len(actions) == 8
    assert actions["p7_c1"] == "hide"        # rage → drain → drop → hide
    assert actions["p7_c2"] == "collapse"    # engagement bait, kept → collapse
    assert actions["p7_c0"] == "show"
    with pytest.raises(KeyError):
        await hub.open_comments("nope")
    await hub.close()


@pytest.mark.asyncio
async def test_write_filtered_contract(tmp_path, curator_ctx):
    hub = StreamHub(None, {}, KeywordCurator(), ctx=curator_ctx, config=HubConfig(batch_window=0.01),
                    streams_dir=tmp_path / "streams", workspace_dir=tmp_path, session_id="wf")
    await hub.start()
    texts = ["neutral chatter about lunch plans today", "You idiots, total outrage here",
             "A machine learning guide for beginners", "My cat is sleeping on the keyboard"]
    for i, t in enumerate(texts):
        await hub.raw_queue.put(mk(f"w{i}", t))
    await hub.raw_queue.put(mk("ad", "Sponsored stuff for sale right now", is_ad=True))
    await wait_for(lambda: hub.event_log.count >= 5)
    path = hub.write_filtered()
    data = json.loads(path.read_text())
    meta = data["filter_metadata"]
    assert path.name == "posts.filtered.json"
    assert meta["source_posts"] == meta["kept_posts"] + meta["dropped_count"] == 5
    assert {d["id"] for d in meta["dropped"]} == {"w1", "ad"}
    assert set(meta["dropped"][0]) == {"id", "score", "category", "filter_reason", "platform"}
    scores = [p["score"] for p in data["posts"]]
    assert scores == sorted(scores, reverse=True)
    assert all({"score", "filter_reason", "category", "text", "id"} <= set(p) for p in data["posts"])
    assert meta["goals_snapshot"] == curator_ctx.goals_text
    assert meta["job"] == "streams/wf"
    await hub.close()


# --------------------------------------------------------------------- fake adapter
def test_fake_adapter_parse_body():
    ad = FakeAdapter("http://127.0.0.1:1")
    posts = ad.parse_body("http://127.0.0.1:1/api/feed?cursor=1",
                          {"posts": make_feed_page(1), "next_cursor": 2})
    assert len(posts) == 11 and posts[0].id == "p9"  # page repeats previous last post
    assert posts[1].is_ad and posts[1].id == "p10"
    assert all(p.platform == "fake" for p in posts)
    assert posts[1].url.endswith("/post/p10")
