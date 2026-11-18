"""Validation contracts used by the simulator and future caching proxy."""
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PlayerCreate(BaseModel):
    player_id: int = Field(gt=0)
    username: str = Field(min_length=1, max_length=80)


class TeamCreate(BaseModel):
    team_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=80)
    players: list[PlayerCreate] = Field(min_length=1, max_length=100)


class MatchCreate(BaseModel):
    match_id: int = Field(gt=0)
    duration_seconds: int = Field(default=600, gt=0, le=86400)
    teams: list[TeamCreate] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({t.team_id for t in self.teams}) != len(self.teams):
            raise ValueError("Team IDs must be unique within a match")
        player_ids = [p.player_id for t in self.teams for p in t.players]
        if len(set(player_ids)) != len(player_ids):
            raise ValueError("Each player can belong to only one team in a match")
        return self


class EventCreate(BaseModel):
    event_id: str = Field(min_length=1, max_length=120)
    type: Literal["kill", "death", "assist", "score", "boost"]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    player_id: int = Field(gt=0)
    target_player_id: int | None = Field(default=None, gt=0)
    points: int = Field(default=0, ge=0, le=10000)
    boost: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def validate_by_type(self):
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must include a timezone, e.g. Z")
        if self.type == "kill":
            if self.target_player_id is None or self.target_player_id == self.player_id:
                raise ValueError("kill requires a different target_player_id")
        elif self.target_player_id is not None:
            raise ValueError("target_player_id is only allowed for kill events")
        if self.type == "boost" and self.boost is None:
            raise ValueError("boost events require boost between 0 and 100")
        if self.type != "boost" and self.boost is not None:
            raise ValueError("boost is only allowed for boost events")
        return self
