"""Reference implementation of how the origin should apply simulator events.

This is the executable spec of the contract in ``gamesim.models``. The origin
team can port ``MatchState.apply`` to SQL (or call it directly); the tests and
the dev mock origin use it to check the simulator's output is self-consistent.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .models import EventType, GameplayEvent


@dataclass
class PlayerRow:
    player_id: str
    display_name: str
    team_id: str | None
    score: int = 0
    stats: dict[str, int] = field(default_factory=lambda: defaultdict(int))


@dataclass
class MatchState:
    match_id: int
    status: str = "pending"
    mode: str | None = None
    stat_names: list[str] = field(default_factory=list)
    players: dict[str, PlayerRow] = field(default_factory=dict)
    team_names: dict[str, str] = field(default_factory=dict)
    team_scores: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    version: int = -1  # highest seq applied
    as_of: str | None = None
    live: dict[str, Any] | None = None  # latest tick payload
    summary: dict[str, Any] | None = None
    seen: set[str] = field(default_factory=set)
    run_id: str | None = None

    def _reset(self) -> None:
        self.players.clear()
        self.team_names.clear()
        self.team_scores.clear()
        self.version, self.live, self.summary = -1, None, None

    def apply(self, ev: GameplayEvent) -> bool:
        """Apply one event. Returns False if it was a duplicate."""
        if ev.event_id in self.seen:
            return False
        self.seen.add(ev.event_id)
        if ev.type != EventType.MATCH_START and ev.run_id != self.run_id:
            return False  # straggler from an older run (or event before match_start)

        if ev.type == EventType.MATCH_START:
            p = ev.payload
            if self.run_id is not None and p.run_id != self.run_id:
                self._reset()  # a new simulator run reusing this match id
            self.run_id = p.run_id
            self.status = "live"
            self.mode = p.mode
            self.stat_names = list(p.stats)
            self.team_names = {t.team_id: t.name for t in p.teams}
            for t in p.teams:
                self.team_scores.setdefault(t.team_id, 0)
            for pl in p.players:
                self.players[pl.player_id] = PlayerRow(pl.player_id, pl.display_name, pl.team_id)
        elif ev.type == EventType.MATCH_END:
            self.status = "finished"
            self.summary = ev.payload.model_dump(mode="json")
        elif ev.type == EventType.TICK:
            self.live = ev.payload.model_dump(mode="json")

        if ev.player_id is not None and ev.player_id in self.players:
            row = self.players[ev.player_id]
            if ev.stat is not None:
                row.stats[ev.stat] += ev.value
            row.score += ev.points
        if ev.team_id is not None and ev.team_score_delta:
            self.team_scores[ev.team_id] += ev.team_score_delta

        self.version = max(self.version, ev.seq)
        self.as_of = ev.timestamp.isoformat() if hasattr(ev.timestamp, "isoformat") else str(ev.timestamp)
        return True

    def leaderboard(self) -> list[dict[str, Any]]:
        rows = sorted(
            self.players.values(),
            key=lambda r: (-r.score, -r.stats.get("kills", r.stats.get("goals", 0)), r.player_id),
        )
        out = []
        for rank, r in enumerate(rows, start=1):
            entry = {
                "rank": rank,
                "player_id": r.player_id,
                "name": r.display_name,
                "score": r.score,
            }
            if r.team_id:
                entry["team_id"] = r.team_id
            for s in self.stat_names:
                entry[s] = r.stats.get(s, 0)
            out.append(entry)
        return out

    def leaderboard_response(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "status": self.status,
            "as_of": self.as_of,
            "version": self.version,
            "team_scores": dict(self.team_scores),
            "players": self.leaderboard(),
        }
