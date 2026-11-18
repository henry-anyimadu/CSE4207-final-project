"""Relational storage: players, matches, teams, per-match stats, and event log."""
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Player(Base):
    __tablename__ = "players"

    player_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), nullable=False)


class GameMatch(Base):
    __tablename__ = "matches"

    match_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="live")
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Team(Base):
    __tablename__ = "teams"

    match_id: Mapped[int] = mapped_column(ForeignKey("matches.match_id"), primary_key=True)
    team_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)


class MatchPlayerStats(Base):
    __tablename__ = "match_player_stats"

    match_id: Mapped[int] = mapped_column(ForeignKey("matches.match_id"), primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"), primary_key=True)
    team_id: Mapped[int] = mapped_column(Integer, nullable=False)
    kills: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deaths: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assists: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    boost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        ForeignKeyConstraint(["match_id", "team_id"], ["teams.match_id", "teams.team_id"]),
        Index("ix_match_player_stats_rank", "match_id", "score"),
    )


class GameEvent(Base):
    __tablename__ = "game_events"

    event_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.match_id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"), nullable=False)
    target_player_id: Mapped[int | None] = mapped_column(ForeignKey("players.player_id"))
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    boost: Mapped[int | None] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_game_events_match_received", "match_id", "received_at"),)
