"""Wire contract between the gameplay simulator and the origin API.

Every event is POSTed as JSON to ``{ORIGIN_URL}/matches/{match_id}/events``.

The origin does NOT need to understand game rules. Every event can be applied
with the same generic rule (see ``gamesim.reference.MatchState.apply``):

    if event_id already seen: ignore (idempotent retries)
    if type == "match_start": create match, players and teams from payload
                              (if the match id already exists, a NEW match_start
                              means a new simulator run: reset the match)
    if type == "match_end":   mark match finished, store payload as summary
    if type == "tick":        overwrite live telemetry (positions, boost, clock)
    if stat is not None:      stats[player_id][stat] += value
    score[player_id]         += points
    team_score[team_id]      += team_score_delta

``seq`` is a strictly increasing per-match counter (0 = match_start). The
origin can expose the highest applied ``seq`` as the leaderboard ``version``,
which lets k6 / Grafana measure how stale a cached response is.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class GameMode(str, Enum):
    ARENA = "arena"
    ROCKET = "rocket"


class EventType(str, Enum):
    # lifecycle
    MATCH_START = "match_start"
    MATCH_END = "match_end"
    # live telemetry snapshot (high frequency, not a stat)
    TICK = "tick"
    # arena stat events
    KILL = "kill"
    DEATH = "death"
    ASSIST = "assist"
    DAMAGE = "damage"
    HEADSHOT = "headshot"
    # rocket stat events
    GOAL = "goal"
    SHOT = "shot"
    SAVE = "save"
    DEMO = "demo"


# Which per-player counter each stat event increments.
STAT_FOR_EVENT: dict[EventType, str] = {
    EventType.KILL: "kills",
    EventType.DEATH: "deaths",
    EventType.ASSIST: "assists",
    EventType.DAMAGE: "damage",
    EventType.HEADSHOT: "headshots",
    EventType.GOAL: "goals",
    EventType.SHOT: "shots",
    EventType.SAVE: "saves",
    EventType.DEMO: "demos",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class Vec3(_Strict):
    x: float
    y: float
    z: float = 0.0


class TeamInfo(_Strict):
    team_id: str
    name: str


class PlayerInfo(_Strict):
    player_id: str
    display_name: str
    team_id: str | None = None


class MatchStartPayload(_Strict):
    kind: Literal["match_start"] = "match_start"
    run_id: str = Field(description="Hash of seed + match config; also embedded in every event_id")
    mode: GameMode
    duration_s: int = Field(description="Regulation length; 0 = endless match")
    stats: list[str] = Field(description="Per-player counters this match will produce")
    teams: list[TeamInfo] = Field(default_factory=list, description="Empty for free-for-all")
    players: list[PlayerInfo]


class PlayerState(_Strict):
    player_id: str
    pos: Vec3
    boost: int | None = Field(default=None, ge=0, le=100, description="Rocket mode only")
    health: int | None = Field(default=None, ge=0, le=100, description="Arena mode only")


class TickPayload(_Strict):
    kind: Literal["tick"] = "tick"
    time_remaining_s: float | None = Field(description="None for endless matches")
    overtime: bool = False
    team_scores: dict[str, int] = Field(default_factory=dict)
    ball: Vec3 | None = None
    players: list[PlayerState]


class MatchEndPayload(_Strict):
    kind: Literal["match_end"] = "match_end"
    reason: Literal["time_expired", "overtime_goal", "score_limit", "shutdown"]
    elapsed_s: float
    team_scores: dict[str, int] = Field(default_factory=dict)
    winner_team_id: str | None = None
    winner_player_id: str | None = None


Payload = Annotated[
    MatchStartPayload | TickPayload | MatchEndPayload,
    Field(discriminator="kind"),
]


class GameplayEvent(_Strict):
    """One POST body sent to ``/matches/{match_id}/events``."""

    event_id: str = Field(description="Globally unique, deterministic; use for dedup")
    match_id: int
    run_id: str = Field(description="Same for every event of one simulator run of this match")
    seq: int = Field(ge=0, description="Strictly increasing per match; 0 is match_start")
    type: EventType
    timestamp: datetime
    game_time_s: float = Field(description="Seconds since match start (simulated clock)")
    player_id: str | None = None
    target_player_id: str | None = None
    team_id: str | None = None
    points: int = 0
    stat: str | None = Field(default=None, description="Counter to increment, e.g. 'kills'")
    value: int = Field(default=1, description="Amount added to `stat`")
    team_score_delta: int = 0
    payload: Payload | None = None

    def to_json_dict(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True)


def make_event_id(match_id: int, run_id: str, seq: int) -> str:
    """e.g. ``match101-a1b2c3-event204``.

    ``run_id`` is a hash of (seed, match config): re-running the same scenario
    (or a restarted simulator pod) re-sends identical ids, so the origin's
    dedup makes retries/restarts harmless; a new seed or config gets new ids.
    """
    return f"match{match_id}-{run_id}-event{seq}"


def event_json_schema() -> dict:
    schema = GameplayEvent.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "GameplayEvent"
    return schema
