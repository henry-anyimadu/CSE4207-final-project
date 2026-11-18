"""Run the real-time pipeline against an in-process fake origin (httpx.MockTransport)."""

import asyncio
import json
import random

import httpx
import pytest

from gamesim.config import HttpConfig, MatchGroup, SimConfig
from gamesim.models import GameplayEvent
from gamesim.reference import MatchState
from gamesim.runner import run
from gamesim.sinks import HttpSink


class FakeOrigin:
    """Applies events with the reference rules; can inject failures."""

    def __init__(self, fail_rate=0.0, latency=0.0, seed=0):
        self.matches: dict[int, MatchState] = {}
        self.order: dict[int, list[str]] = {}
        self.fail_rate, self.latency = fail_rate, latency
        self.rng = random.Random(seed)
        self.posts = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        if self.latency:
            await asyncio.sleep(self.rng.uniform(0, self.latency))
        self.posts += 1
        if self.rng.random() < self.fail_rate:
            return httpx.Response(503)
        mid = int(request.url.path.split("/")[2])
        ev = GameplayEvent.model_validate(json.loads(request.content))
        assert ev.match_id == mid
        st = self.matches.get(mid)
        if ev.type == "match_start":
            st = self.matches.setdefault(mid, MatchState(mid))
        elif st is None or st.status == "pending":
            return httpx.Response(404, json={"detail": "unknown match"})
        if st.status == "finished" and ev.type != "match_end":
            return httpx.Response(409, json={"detail": "match finished"})
        if not st.apply(ev):
            return httpx.Response(409, json={"detail": "duplicate"})
        self.order.setdefault(mid, []).append(ev.type)
        return httpx.Response(201, json={"accepted": ev.event_id})


def make_cfg(**http):
    return SimConfig(
        seed=7,
        metrics_port=0,
        speed=50.0,  # 50x real time so the test is quick
        http=HttpConfig(origin_url="http://origin", backoff_base_s=0.001, backoff_max_s=0.01, **http),
        matches=[
            MatchGroup(
                mode="arena",
                count=2,
                actions_per_second=5,
                tick_hz=2,
                duration_s=20,
                stats=["kills", "deaths", "assists", "damage"],
            ),
            MatchGroup(mode="rocket", count=1, actions_per_second=1, tick_hz=10, duration_s=20),
        ],
    )


async def _run(cfg, origin, **kw):
    stop = asyncio.Event()
    sink = HttpSink(cfg.http, transport=httpx.MockTransport(origin.handler), stop=stop)
    return await asyncio.wait_for(run(cfg, stop=stop, sink=sink, **kw), timeout=30)


async def test_end_to_end_ordering_and_consistency():
    origin = FakeOrigin(latency=0.002)
    cfg = make_cfg(concurrency_per_match=8)
    stats = await _run(cfg, origin)
    assert stats.sent["failed"] == 0
    assert set(origin.matches) == {101, 102, 103}
    for mid, st in origin.matches.items():
        order = origin.order[mid]
        assert order[0] == "match_start" and order[-1] == "match_end"
        assert order.count("match_start") == order.count("match_end") == 1
        assert st.status == "finished"
        assert st.version == len(order) - 1  # every seq landed exactly once


async def test_retries_make_delivery_exact_despite_503s():
    origin = FakeOrigin(fail_rate=0.3, seed=3)
    cfg = make_cfg(max_retries=20)
    stats = await _run(cfg, origin)
    assert stats.sent["failed"] == 0
    assert origin.posts > stats.generated  # retries happened
    # applied state equals a clean replay
    clean = FakeOrigin()
    await _run(make_cfg(), clean)
    for mid in clean.matches:
        assert origin.matches[mid].leaderboard() == clean.matches[mid].leaderboard()


async def test_restart_allocates_new_match_ids():
    origin = FakeOrigin()
    cfg = SimConfig(
        metrics_port=0,
        speed=100.0,
        http=HttpConfig(origin_url="http://origin"),
        matches=[MatchGroup(mode="arena", count=1, actions_per_second=5, duration_s=5, on_end="restart")],
    )
    await _run(cfg, origin, max_wall_s=0.35)  # ~35s of game time -> several 5s matches
    ids = sorted(origin.matches)
    assert ids[0] == 101 and len(ids) >= 3
    assert ids == list(range(101, 101 + len(ids)))


async def test_waits_for_origin_health():
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(503 if calls["n"] < 3 else 200)

    sink = HttpSink(HttpConfig(origin_url="http://o", wait_for_origin_s=5), transport=httpx.MockTransport(handler))
    await sink.start()
    await sink.wait_for_origin()
    await sink.close()
    assert calls["n"] == 3

    sink = HttpSink(
        HttpConfig(origin_url="http://o", wait_for_origin_s=0.3),
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    await sink.start()
    with pytest.raises(RuntimeError):
        await sink.wait_for_origin()
    await sink.close()


async def test_client_errors_are_not_retried():
    seen = []

    def handler(request):
        seen.append(1)
        return httpx.Response(422, json={"detail": "bad"})

    sink = HttpSink(HttpConfig(origin_url="http://o", max_retries=5), transport=httpx.MockTransport(handler))
    await sink.start()
    from gamesim.engines import create_engine

    ev = create_engine(1, MatchGroup(mode="arena"), 1).start()[0]
    assert await sink.send(ev, must_deliver=True) == "failed"
    assert len(seen) == 1
    await sink.close()
