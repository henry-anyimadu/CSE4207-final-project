"""Scenario configuration: YAML file + environment-variable overrides."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import GameMode

ARENA_REQUIRED_STATS = {"kills", "deaths"}
ARENA_OPTIONAL_STATS = {"assists", "damage", "headshots"}
ROCKET_REQUIRED_STATS = {"goals", "shots"}
ROCKET_OPTIONAL_STATS = {"assists", "saves", "demos"}

DEFAULT_STATS = {
    GameMode.ARENA: ["kills", "deaths", "assists"],
    GameMode.ROCKET: ["goals", "assists", "shots", "saves", "demos"],
}


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MatchGroup(_Cfg):
    """A group of identical matches. ``count`` matches run concurrently."""

    mode: GameMode
    count: int = Field(default=1, ge=1, le=500)
    match_id_start: int | None = Field(
        default=None, description="First match id in this group; default continues after the previous group"
    )
    actions_per_second: float = Field(
        default=5.0,
        ge=0,
        le=1000,
        description="Gameplay moments per second per match. One moment emits 1+ events (e.g. kill + death + assists).",
    )
    tick_hz: float = Field(default=0.0, ge=0, le=120, description="Telemetry snapshots per second (0 = off)")
    duration_s: int = Field(default=300, ge=0, description="Match length; 0 = endless")
    on_end: Literal["stop", "restart"] = Field(
        default="stop", description="restart = start a fresh match with a new id when one finishes"
    )
    start_offset_s: float = Field(default=0.0, ge=0, description="Delay before this group starts")
    stats: list[str] | None = Field(default=None, description="Per-player counters to generate")

    # arena-only
    players: int = Field(default=12, ge=2, le=100)
    teams: int = Field(default=0, ge=0, le=8, description="0 = free-for-all")
    score_limit: int | None = Field(default=None, ge=1, description="End when a player (or team) reaches N kills")

    # rocket-only
    team_size: int = Field(default=3, ge=1, le=4)
    max_overtime_s: int = Field(default=300, ge=0)

    @model_validator(mode="after")
    def _check(self) -> MatchGroup:
        if self.stats is None:
            self.stats = list(DEFAULT_STATS[self.mode])
        stats = set(self.stats)
        if self.mode == GameMode.ARENA:
            req, opt = ARENA_REQUIRED_STATS, ARENA_OPTIONAL_STATS
            if self.teams == 1:
                raise ValueError("arena teams must be 0 (free-for-all) or >= 2")
            if self.teams and self.players < self.teams:
                raise ValueError("arena needs at least one player per team")
        else:
            req, opt = ROCKET_REQUIRED_STATS, ROCKET_OPTIONAL_STATS
        unknown = stats - req - opt
        if unknown:
            raise ValueError(f"unknown stats for {self.mode.value}: {sorted(unknown)}; allowed: {sorted(req | opt)}")
        missing = req - stats
        if missing:
            raise ValueError(f"{self.mode.value} always produces {sorted(missing)}; add them to stats")
        # keep a stable, canonical order
        order = ["kills", "deaths", "goals", "assists", "shots", "saves", "demos", "damage", "headshots"]
        self.stats = [s for s in order if s in stats]
        return self

    @property
    def player_count(self) -> int:
        return self.team_size * 2 if self.mode == GameMode.ROCKET else self.players


class HttpConfig(_Cfg):
    origin_url: str = "http://origin:8000"
    events_path: str = "/matches/{match_id}/events"
    health_path: str = "/health"
    timeout_s: float = Field(default=2.0, gt=0)
    max_retries: int = Field(default=5, ge=0)
    backoff_base_s: float = Field(default=0.1, ge=0)
    backoff_max_s: float = Field(default=2.0, ge=0)
    concurrency_per_match: int = Field(default=4, ge=1, le=64)
    queue_size: int = Field(default=5000, ge=1)
    wait_for_origin_s: float = Field(default=120.0, ge=0, description="0 = don't wait for /health")


class SimConfig(_Cfg):
    seed: int = 4207
    sink: Literal["http", "stdout", "file", "null"] = "http"
    output_path: str | None = Field(default=None, description="Target for sink=file (JSON lines)")
    metrics_port: int = Field(default=9100, ge=0, description="0 = no metrics server")
    speed: float = Field(default=1.0, gt=0, description="Simulated seconds per wall-clock second")
    http: HttpConfig = Field(default_factory=HttpConfig)
    matches: list[MatchGroup] = Field(default_factory=lambda: [MatchGroup(mode=GameMode.ARENA)])

    @model_validator(mode="after")
    def _assign_ids(self) -> SimConfig:
        next_id = 101
        taken: set[int] = set()
        for g in self.matches:
            if g.match_id_start is None:
                g.match_id_start = next_id
            ids = set(range(g.match_id_start, g.match_id_start + g.count))
            clash = ids & taken
            if clash:
                raise ValueError(f"match ids overlap between groups: {sorted(clash)[:5]}")
            taken |= ids
            next_id = max(taken) + 1
        if self.sink == "file" and not self.output_path:
            raise ValueError("sink=file requires output_path")
        return self

    def match_ids(self) -> list[int]:
        return [g.match_id_start + i for g in self.matches for i in range(g.count)]


ENV_OVERRIDES = {
    "ORIGIN_URL": ("http", "origin_url", str),
    "GAMESIM_ORIGIN_URL": ("http", "origin_url", str),
    "GAMESIM_SEED": (None, "seed", int),
    "GAMESIM_SINK": (None, "sink", str),
    "GAMESIM_OUTPUT_PATH": (None, "output_path", str),
    "GAMESIM_METRICS_PORT": (None, "metrics_port", int),
    "GAMESIM_SPEED": (None, "speed", float),
    "GAMESIM_WAIT_FOR_ORIGIN_S": ("http", "wait_for_origin_s", float),
}
# These apply to every match group (handy for benchmark sweeps without editing YAML).
GROUP_ENV_OVERRIDES = {
    "GAMESIM_ACTIONS_PER_SECOND": ("actions_per_second", float),
    "GAMESIM_TICK_HZ": ("tick_hz", float),
    "GAMESIM_DURATION_S": ("duration_s", int),
    "GAMESIM_ON_END": ("on_end", str),
}


def load_config(path: str | Path | None = None, env: dict[str, str] | None = None) -> SimConfig:
    env = dict(os.environ if env is None else env)
    path = path or env.get("GAMESIM_CONFIG")
    raw: dict = {}
    if path:
        raw = yaml.safe_load(Path(path).read_text()) or {}
    raw.setdefault("http", {})
    for var, (section, key, cast) in ENV_OVERRIDES.items():
        if var in env and env[var] != "":
            (raw[section] if section else raw)[key] = cast(env[var])
    groups = raw.get("matches") or [{"mode": "arena"}]
    for var, (key, cast) in GROUP_ENV_OVERRIDES.items():
        if var in env and env[var] != "":
            for g in groups:
                g[key] = cast(env[var])
    raw["matches"] = groups
    return SimConfig.model_validate(raw)
