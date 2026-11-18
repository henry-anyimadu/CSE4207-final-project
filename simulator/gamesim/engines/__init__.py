from __future__ import annotations

from datetime import datetime

from ..config import MatchGroup
from ..models import GameMode
from .arena import ArenaEngine
from .base import GameEngine, timeline
from .rocket import RocketEngine

ENGINES: dict[GameMode, type[GameEngine]] = {
    GameMode.ARENA: ArenaEngine,
    GameMode.ROCKET: RocketEngine,
}


def create_engine(match_id: int, spec: MatchGroup, seed: int, epoch: datetime | None = None) -> GameEngine:
    return ENGINES[GameMode(spec.mode)](match_id, spec, seed, epoch)


__all__ = ["ENGINES", "GameEngine", "ArenaEngine", "RocketEngine", "create_engine", "timeline"]
