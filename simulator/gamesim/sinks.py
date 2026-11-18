"""Where events go: the origin API over HTTP, or stdout / a file / nowhere for dev."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from abc import ABC, abstractmethod
from typing import IO

import httpx

from . import metrics
from .config import HttpConfig, SimConfig
from .models import GameplayEvent

log = logging.getLogger("gamesim.sink")

OK, DUPLICATE, FAILED = "ok", "duplicate", "failed"
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class Sink(ABC):
    async def start(self) -> None:  # noqa: B027
        pass

    @abstractmethod
    async def send(self, ev: GameplayEvent, *, must_deliver: bool = False) -> str:
        """Deliver one event. Returns OK / DUPLICATE / FAILED."""

    async def close(self) -> None:  # noqa: B027
        pass


class NullSink(Sink):
    async def send(self, ev, *, must_deliver=False):
        return OK


class StreamSink(Sink):
    """JSON lines to stdout or a file."""

    def __init__(self, stream: IO[str], owns: bool = False):
        self.stream, self.owns = stream, owns

    async def send(self, ev, *, must_deliver=False):
        self.stream.write(json.dumps(ev.to_json_dict(), separators=(",", ":")) + "\n")
        return OK

    async def close(self):
        self.stream.flush()
        if self.owns:
            self.stream.close()


class HttpSink(Sink):
    """POST /matches/{match_id}/events with retries.

    Retrying is safe because every event has a deterministic ``event_id`` the
    origin dedups on. A 409 from the origin is treated as "already have it".
    """

    def __init__(
        self, cfg: HttpConfig, transport: httpx.AsyncBaseTransport | None = None, stop: asyncio.Event | None = None
    ):
        self.cfg = cfg
        self.stop = stop or asyncio.Event()
        self._transport = transport
        self.client: httpx.AsyncClient | None = None
        self._logged_4xx: set[int] = set()

    async def start(self) -> None:
        limits = httpx.Limits(max_connections=256, max_keepalive_connections=128)
        self.client = httpx.AsyncClient(
            base_url=self.cfg.origin_url.rstrip("/"),
            timeout=self.cfg.timeout_s,
            limits=limits,
            transport=self._transport,
            headers={"User-Agent": "gamesim/1.0"},
        )

    async def wait_for_origin(self) -> None:
        if self.cfg.wait_for_origin_s <= 0:
            return
        deadline = time.monotonic() + self.cfg.wait_for_origin_s
        attempt = 0
        while True:
            try:
                r = await self.client.get(self.cfg.health_path)
                if r.status_code < 300:
                    log.info("origin healthy at %s", self.cfg.origin_url)
                    return
                reason = f"HTTP {r.status_code}"
            except httpx.HTTPError as e:
                reason = type(e).__name__
            attempt += 1
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"origin {self.cfg.origin_url} not healthy after {self.cfg.wait_for_origin_s:.0f}s ({reason})"
                )
            if attempt == 1 or attempt % 10 == 0:
                log.info("waiting for origin %s (%s)", self.cfg.origin_url, reason)
            await asyncio.sleep(min(2.0, 0.25 * attempt))

    def _backoff(self, attempt: int) -> float:
        return min(self.cfg.backoff_max_s, self.cfg.backoff_base_s * (2**attempt))

    async def send(self, ev: GameplayEvent, *, must_deliver: bool = False) -> str:
        assert self.client is not None, "call start() first"
        mode = _mode_of(ev)
        path = self.cfg.events_path.format(match_id=ev.match_id)
        body = ev.to_json_dict()
        attempt = 0
        while True:
            t0 = time.perf_counter()
            try:
                r = await self.client.post(path, json=body)
                metrics.SEND_LATENCY.labels(mode).observe(time.perf_counter() - t0)
                metrics.HTTP_RESPONSES.labels(str(r.status_code)).inc()
                if r.status_code < 300:
                    return OK
                if r.status_code == 409:
                    return DUPLICATE
                if r.status_code not in RETRYABLE_STATUS:
                    if r.status_code not in self._logged_4xx:
                        self._logged_4xx.add(r.status_code)
                        log.error("origin rejected %s with %s: %s", ev.event_id, r.status_code, r.text[:300])
                    return FAILED  # a malformed/rejected event won't succeed on retry
                reason = str(r.status_code)
            except httpx.HTTPError as e:
                reason = type(e).__name__
            attempt += 1
            if (not must_deliver and attempt > self.cfg.max_retries) or self.stop.is_set():
                return FAILED
            metrics.SEND_RETRIES.labels(mode, reason).inc()
            if must_deliver and attempt % 10 == 0:
                log.warning("still retrying %s (%s, attempt %d)", ev.event_id, reason, attempt)
            await asyncio.sleep(self._backoff(attempt))

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()


_MODE_BY_MATCH: dict[int, str] = {}


def register_match_mode(match_id: int, mode: str) -> None:
    _MODE_BY_MATCH[match_id] = mode


def _mode_of(ev: GameplayEvent) -> str:
    return _MODE_BY_MATCH.get(ev.match_id, "unknown")


def build_sink(
    cfg: SimConfig, stop: asyncio.Event | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> Sink:
    if cfg.sink == "http":
        return HttpSink(cfg.http, transport=transport, stop=stop)
    if cfg.sink == "stdout":
        return StreamSink(sys.stdout)
    if cfg.sink == "file":
        return StreamSink(open(cfg.output_path, "a", encoding="utf-8"), owns=True)
    return NullSink()
