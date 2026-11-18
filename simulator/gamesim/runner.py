"""Real-time runner: paces each match's timeline against the wall clock and
hands events to the sink.

Per match there is one producer (the engine timeline) and one sender. The
sender keeps up to ``concurrency_per_match`` POSTs in flight, but treats
``match_start`` and ``match_end`` as barriers: everything before a barrier is
acknowledged before the barrier is sent, and the barrier is acknowledged
before anything after it. So the origin always sees a match created before
any of its events, and the match ends only after all of its events landed.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime

from . import metrics
from .config import MatchGroup, SimConfig
from .engines import create_engine, timeline
from .models import EventType, GameplayEvent
from .sinks import DUPLICATE, FAILED, OK, HttpSink, Sink, build_sink, register_match_mode

DROPPED = "dropped"  # still queued when shutdown was requested

log = logging.getLogger("gamesim.runner")

BARRIERS = {EventType.MATCH_START.value, EventType.MATCH_END.value}
FIXED_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class Stats:
    def __init__(self) -> None:
        self.sent = {OK: 0, DUPLICATE: 0, FAILED: 0, DROPPED: 0}
        self.generated = 0


class MatchSender:
    def __init__(
        self,
        match_id: int,
        mode: str,
        sink: Sink,
        concurrency: int,
        queue_size: int,
        stats: Stats,
        stop: asyncio.Event | None = None,
    ):
        self.match_id, self.mode, self.sink = match_id, mode, sink
        self.stop = stop or asyncio.Event()
        self.concurrency = concurrency
        self.queue: asyncio.Queue[GameplayEvent | None] = asyncio.Queue(maxsize=queue_size)
        self.stats = stats
        self.acked = -1
        self._mid = str(match_id)

    async def put(self, ev: GameplayEvent) -> None:
        await self.queue.put(ev)  # blocks when full -> backpressure on the producer
        metrics.QUEUE_DEPTH.labels(self._mid).set(self.queue.qsize())

    async def close(self) -> None:
        await self.queue.put(None)

    async def _send(self, ev: GameplayEvent, must_deliver: bool = False) -> None:
        outcome = await self.sink.send(ev, must_deliver=must_deliver)
        self.stats.sent[outcome] += 1
        metrics.EVENTS_SENT.labels(self.mode, ev.type, outcome).inc()
        if outcome != FAILED and ev.seq > self.acked:
            self.acked = ev.seq
            metrics.ACKED_SEQ.labels(self._mid, self.mode).set(ev.seq)

    async def run(self) -> None:
        inflight: set[asyncio.Task] = set()
        while True:
            ev = await self.queue.get()
            metrics.QUEUE_DEPTH.labels(self._mid).set(self.queue.qsize())
            if ev is None:
                break
            if self.stop.is_set():
                # shutting down: don't hammer the origin with a stale backlog
                self.stats.sent[DROPPED] += 1
                metrics.EVENTS_SENT.labels(self.mode, ev.type, DROPPED).inc()
                continue
            if ev.type in BARRIERS:
                if inflight:
                    await asyncio.gather(*inflight)
                    inflight.clear()
                await self._send(ev, must_deliver=True)
                continue
            while len(inflight) >= self.concurrency:
                done, _ = await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)
                inflight -= done
            inflight.add(asyncio.create_task(self._send(ev)))
        if inflight:
            await asyncio.gather(*inflight)


class IdAllocator:
    """Hands out fresh match ids for ``on_end: restart``."""

    def __init__(self, start: int):
        self._it = itertools.count(start)

    def next(self) -> int:
        return next(self._it)


async def _sleep_or_stop(stop: asyncio.Event, delay: float) -> bool:
    """Sleep ``delay`` seconds; return True if stop was requested."""
    if delay <= 0:
        return stop.is_set()
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
        return True
    except TimeoutError:
        return False


async def run_slot(
    group: MatchGroup, match_id: int, cfg: SimConfig, sink: Sink, ids: IdAllocator, stop: asyncio.Event, stats: Stats
) -> None:
    mode = group.mode.value
    loop = asyncio.get_running_loop()
    if await _sleep_or_stop(stop, group.start_offset_s / cfg.speed):
        return
    while not stop.is_set():
        register_match_mode(match_id, mode)
        engine = create_engine(match_id, group, cfg.seed, epoch=datetime.now(UTC))
        sender = MatchSender(match_id, mode, sink, cfg.http.concurrency_per_match, cfg.http.queue_size, stats, stop)
        sender_task = asyncio.create_task(sender.run())
        metrics.ACTIVE_MATCHES.labels(mode).inc()
        log.info(
            "match %d started (%s, %d players, %.1f actions/s, %.0f Hz ticks, %ss)",
            match_id,
            mode,
            group.player_count,
            group.actions_per_second,
            group.tick_hz,
            group.duration_s or "endless",
        )
        start = loop.time()
        mid = str(match_id)
        end_reason = None
        try:
            for t, evs in timeline(engine):
                if stop.is_set():
                    break
                lag = loop.time() - (start + t / cfg.speed)
                metrics.SCHEDULE_LAG.labels(mid).set(max(0.0, lag))
                if lag < 0 and await _sleep_or_stop(stop, -lag):
                    break
                for ev in evs:
                    stats.generated += 1
                    metrics.EVENTS_GENERATED.labels(mode, ev.type).inc()
                    metrics.LATEST_SEQ.labels(mid, mode).set(ev.seq)
                    if ev.type == EventType.MATCH_END.value:
                        end_reason = ev.payload.reason
                    await sender.put(ev)
        finally:
            await sender.close()
            await sender_task
            metrics.ACTIVE_MATCHES.labels(mode).dec()
        if end_reason:
            metrics.MATCHES_FINISHED.labels(mode, end_reason).inc()
            log.info("match %d finished (%s) after %.1fs game time", match_id, end_reason, engine.game_time)
        if group.on_end != "restart" or stop.is_set():
            return
        metrics.forget_match(match_id, mode)
        match_id = ids.next()


async def _report(stop: asyncio.Event, stats: Stats, every: float = 10.0) -> None:
    last, last_t = 0, time.monotonic()
    while not await _sleep_or_stop(stop, every):
        now = time.monotonic()
        total = stats.sent[OK] + stats.sent[DUPLICATE] + stats.sent[FAILED]
        log.info(
            "sent %.1f ev/s | ok=%d dup=%d failed=%d dropped=%d | generated=%d",
            (total - last) / (now - last_t),
            stats.sent[OK],
            stats.sent[DUPLICATE],
            stats.sent[FAILED],
            stats.sent[DROPPED],
            stats.generated,
        )
        last, last_t = total, now


async def run(
    cfg: SimConfig, stop: asyncio.Event | None = None, sink: Sink | None = None, max_wall_s: float | None = None
) -> Stats:
    """Run every configured match concurrently until they finish or ``stop`` is set."""
    stop = stop or asyncio.Event()
    sink = sink or build_sink(cfg, stop=stop)
    stats = Stats()
    await sink.start()
    try:
        if isinstance(sink, HttpSink):
            await sink.wait_for_origin()
        ids = IdAllocator(max(cfg.match_ids()) + 1)
        slots = [
            asyncio.create_task(run_slot(g, g.match_id_start + i, cfg, sink, ids, stop, stats))
            for g in cfg.matches
            for i in range(g.count)
        ]
        reporter = asyncio.create_task(_report(stop, stats))
        if max_wall_s:
            asyncio.get_running_loop().call_later(max_wall_s, stop.set)
        await asyncio.gather(*slots)
        stop.set()
        await reporter
    finally:
        await sink.close()
    log.info(
        "done: ok=%d dup=%d failed=%d dropped=%d generated=%d",
        stats.sent[OK],
        stats.sent[DUPLICATE],
        stats.sent[FAILED],
        stats.sent[DROPPED],
        stats.generated,
    )
    return stats


def generate(cfg: SimConfig, max_game_s: float | None = None, epoch: datetime = FIXED_EPOCH) -> Iterator[GameplayEvent]:
    """Offline mode: every match's full event stream, merged by game time, no sleeping.

    Fully deterministic (fixed epoch), so the output works as a fixture for
    origin tests. ``on_end: restart`` is ignored here; endless matches need
    ``max_game_s``.
    """
    streams = []
    for g in cfg.matches:
        for i in range(g.count):
            if not g.duration_s and not max_game_s:
                raise ValueError("endless match (duration_s: 0) needs --max-game-seconds in generate mode")
            engine = create_engine(g.match_id_start + i, g, cfg.seed, epoch=epoch)

            def stream(engine=engine):
                for t, evs in timeline(engine):
                    if max_game_s is not None and t > max_game_s:
                        return
                    yield from evs

            streams.append(stream())
    yield from heapq.merge(*streams, key=lambda e: (e.game_time_s, e.match_id, e.seq))
