"""Deterministic match engine base class and timeline.

An engine is pure game logic: given a seed it always produces the same events
in the same order. Wall-clock pacing lives in ``gamesim.runner``; that split is
what makes benchmark runs repeatable (same seed -> same event stream, whether
it is replayed in real time against the origin or dumped to a file).
"""

from __future__ import annotations

import hashlib
import math
import random
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from ..config import MatchGroup
from ..models import (
    STAT_FOR_EVENT,
    EventType,
    GameplayEvent,
    MatchEndPayload,
    MatchStartPayload,
    PlayerInfo,
    TeamInfo,
    make_event_id,
)
from .names import gamer_tags


class GameEngine(ABC):
    def __init__(self, match_id: int, spec: MatchGroup, seed: int, epoch: datetime | None = None):
        self.match_id = match_id
        self.spec = spec
        self.stats = set(spec.stats or [])
        # String seeds are hashed with SHA-512 -> stable across Python processes.
        self.rng = random.Random(f"gamesim:{seed}:{match_id}")
        self.epoch = epoch or datetime.now(UTC)
        fingerprint = f"{seed}:{match_id}:{spec.model_dump_json(exclude={'count', 'match_id_start', 'on_end'})}"
        self.run_id = hashlib.sha1(fingerprint.encode()).hexdigest()[:6]
        self.seq = 0
        self.game_time = 0.0
        self.finished = False
        self.overtime = False
        self.overtime_started_at: float | None = None
        self.teams: list[TeamInfo] = []
        self.players: list[PlayerInfo] = []
        self.team_scores: dict[str, int] = {}
        self._setup()

    # ---- hooks for subclasses -------------------------------------------------
    @abstractmethod
    def _setup(self) -> None:
        """Create teams/players and initial state."""

    @abstractmethod
    def on_action(self) -> list[GameplayEvent]:
        """One gameplay moment (kill, shot, ...)."""

    @abstractmethod
    def on_tick(self) -> list[GameplayEvent]:
        """One telemetry snapshot."""

    @abstractmethod
    def on_regulation_end(self) -> list[GameplayEvent]:
        """Called when the clock hits duration_s. Either finish or enter overtime."""

    # ---- helpers --------------------------------------------------------------
    @property
    def mode(self):
        return self.spec.mode

    def make_names(self, n: int) -> list[str]:
        return gamer_tags(self.rng, n)

    def emit(self, type: EventType, **fields) -> GameplayEvent:
        stat = STAT_FOR_EVENT.get(type)
        if stat is not None and stat not in self.stats:
            raise AssertionError(f"engine emitted disabled stat {stat}")
        if stat is None:
            fields.setdefault("value", 0)  # lifecycle / tick events don't count toward any stat
        ev = GameplayEvent(
            event_id=make_event_id(self.match_id, self.run_id, self.seq),
            match_id=self.match_id,
            run_id=self.run_id,
            seq=self.seq,
            type=type,
            timestamp=self.epoch + timedelta(seconds=self.game_time),
            game_time_s=round(self.game_time, 3),
            stat=stat,
            **fields,
        )
        self.seq += 1
        return ev

    def stat_enabled(self, stat: str) -> bool:
        return stat in self.stats

    def time_remaining(self) -> float | None:
        if not self.spec.duration_s:
            return None
        if self.overtime:
            return 0.0
        return max(0.0, round(self.spec.duration_s - self.game_time, 3))

    @abstractmethod
    def winners(self) -> tuple[str | None, str | None]:
        """(winner_team_id, winner_player_id); None where tied / not applicable."""

    def force_end(self) -> list[GameplayEvent]:
        """End now (overtime cap reached) and declare whoever is ahead."""
        team, player = self.winners()
        return self.finish("time_expired", winner_team_id=team, winner_player_id=player)

    def start(self) -> list[GameplayEvent]:
        payload = MatchStartPayload(
            run_id=self.run_id,
            mode=self.mode,
            duration_s=self.spec.duration_s,
            stats=list(self.spec.stats or []),
            teams=self.teams,
            players=self.players,
        )
        return [self.emit(EventType.MATCH_START, payload=payload)]

    def finish(self, reason: str, winner_team_id=None, winner_player_id=None) -> list[GameplayEvent]:
        self.finished = True
        payload = MatchEndPayload(
            reason=reason,
            elapsed_s=round(self.game_time, 3),
            team_scores=dict(self.team_scores),
            winner_team_id=winner_team_id,
            winner_player_id=winner_player_id,
        )
        return [self.emit(EventType.MATCH_END, payload=payload)]


def timeline(engine: GameEngine) -> Iterator[tuple[float, list[GameplayEvent]]]:
    """Yield ``(game_time_s, events)`` in order until the match ends.

    Actions and ticks are on fixed grids (k / rate), computed from integer
    counters so there is no floating-point drift over long runs.
    """
    spec = engine.spec
    aps, hz = spec.actions_per_second, spec.tick_hz
    yield 0.0, engine.start()

    a_i = t_i = 1
    regulation_done = False
    while not engine.finished:
        ta = a_i / aps if aps > 0 else math.inf
        tt = t_i / hz if hz > 0 else math.inf
        t = min(ta, tt)
        if t == math.inf:
            # nothing scheduled (both rates 0): only the clock can end the match
            if not spec.duration_s:
                return
            t = float(spec.duration_s)

        dur = spec.duration_s
        if dur and not regulation_done and t >= dur:
            regulation_done = True
            engine.game_time = float(dur)
            evs = engine.on_regulation_end()
            if engine.overtime and engine.overtime_started_at is None:
                engine.overtime_started_at = float(dur)
            yield float(dur), evs
            if engine.finished:
                return
            if ta == math.inf and tt == math.inf:
                # overtime but nothing can happen any more
                yield engine.game_time, engine.force_end()
                return
            continue

        ot_start = engine.overtime_started_at
        if engine.overtime and ot_start is not None and t - ot_start >= spec.max_overtime_s:
            engine.game_time = ot_start + spec.max_overtime_s
            yield engine.game_time, engine.force_end()
            return

        engine.game_time = t
        evs: list[GameplayEvent] = []
        if t == ta:
            evs.extend(engine.on_action())
            a_i += 1
        if t == tt and not engine.finished:
            evs.extend(engine.on_tick())
            t_i += 1
        elif t == tt:
            t_i += 1
        yield t, evs
