"""Origin FastAPI service. Simulator writes here; future Redis proxy reads here.

The origin is intentionally stateless. All game state is stored in PostgreSQL,
so Kubernetes can restart/replace the origin pod without losing match data.
"""
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .database import Base, engine, get_db
from .models import GameEvent, GameMatch, MatchPlayerStats, Player, Team, utcnow
from .schemas import EventCreate, MatchCreate

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # MVP schema bootstrap. For production, replace create_all with Alembic migrations.
    for attempt in range(1, 31):
        try:
            Base.metadata.create_all(bind=engine)
            logger.info("Database schema ready")
            break
        except SQLAlchemyError:
            if attempt == 30:
                raise
            logger.warning("Database unavailable, retry %s/30", attempt)
            time.sleep(2)
    yield


app = FastAPI(
    title="Live Game Origin API",
    description="Persistent gameplay event ingestion and live match analytics (pre-cache).",
    version="0.1.0",
    lifespan=lifespan,
)
Instrumentator(excluded_handlers=["/health", "/ready", "/metrics"]).instrument(app).expose(
    app, endpoint="/metrics", include_in_schema=False
)


def get_match_or_404(db: Session, match_id: int, *, lock: bool = False) -> GameMatch:
    stmt = select(GameMatch).where(GameMatch.match_id == match_id)
    if lock:
        stmt = stmt.with_for_update()
    match = db.scalar(stmt)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")
    return match


def ensure_utc(value: datetime) -> datetime:
    # SQLite used by local unit tests can return naive datetimes.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def time_remaining(match: GameMatch) -> int:
    if match.status == "finished":
        return 0
    elapsed = int((datetime.now(timezone.utc) - ensure_utc(match.started_at)).total_seconds())
    return max(0, match.duration_seconds - elapsed)


def matches_existing_event(existing: GameEvent, match_id: int, payload: EventCreate) -> bool:
    return (
        existing.match_id == match_id
        and existing.player_id == payload.player_id
        and existing.event_type == payload.type
        and existing.target_player_id == payload.target_player_id
        and existing.points == payload.points
        and existing.boost == payload.boost
    )


def stats_rows(db: Session, match_id: int):
    return db.execute(
        select(MatchPlayerStats, Player)
        .join(Player, Player.player_id == MatchPlayerStats.player_id)
        .where(MatchPlayerStats.match_id == match_id)
        .order_by(MatchPlayerStats.score.desc(), MatchPlayerStats.kills.desc(), MatchPlayerStats.player_id)
    ).all()


def player_payload(stats: MatchPlayerStats, player: Player) -> dict:
    return {
        "player_id": player.player_id,
        "name": player.username,
        "team_id": stats.team_id,
        "kills": stats.kills,
        "deaths": stats.deaths,
        "assists": stats.assists,
        "score": stats.score,
        "boost": stats.boost,
    }


@app.get("/health", tags=["Operations"])
def health():
    return {"status": "ok"}


@app.get("/ready", tags=["Operations"])
def ready(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="PostgreSQL unavailable")
    return {"status": "ready"}


@app.post("/matches", status_code=201, tags=["Matches"])
def create_match(payload: MatchCreate, db: Session = Depends(get_db)):
    if db.get(GameMatch, payload.match_id) is not None:
        raise HTTPException(status_code=409, detail="Match ID already exists")

    now = utcnow()
    db.add(GameMatch(
        match_id=payload.match_id, status="live", duration_seconds=payload.duration_seconds,
        started_at=now, updated_at=now,
    ))
    try:
        # Flush parent first to satisfy FK constraints on teams and memberships.
        db.flush()
        for team in payload.teams:
            db.add(Team(match_id=payload.match_id, team_id=team.team_id, name=team.name))
        db.flush()
        for team in payload.teams:
            for member in team.players:
                existing = db.get(Player, member.player_id)
                if existing is None:
                    db.add(Player(player_id=member.player_id, username=member.username))
                elif existing.username != member.username:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Player {member.player_id} already exists with another username",
                    )
        db.flush()
        for team in payload.teams:
            for member in team.players:
                db.add(MatchPlayerStats(
                    match_id=payload.match_id, player_id=member.player_id,
                    team_id=team.team_id, kills=0, deaths=0, assists=0, score=0, boost=0,
                ))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Match/player constraint conflict")
    except Exception:
        db.rollback()
        raise
    return {"match_id": payload.match_id, "status": "live", "players": sum(len(t.players) for t in payload.teams)}


@app.post("/matches/{match_id}/events", status_code=201, tags=["Events"])
def ingest_event(
    match_id: int, payload: EventCreate, response: Response, db: Session = Depends(get_db)
):
    # Lock match row so two origin replicas cannot apply conflicting events concurrently.
    match = get_match_or_404(db, match_id, lock=True)
    existing_event = db.get(GameEvent, payload.event_id)
    if existing_event:
        if not matches_existing_event(existing_event, match_id, payload):
            raise HTTPException(status_code=409, detail="event_id already used for a different event")
        response.status_code = status.HTTP_200_OK
        return {"event_id": payload.event_id, "match_id": match_id, "duplicate": True}

    if match.status != "live" or time_remaining(match) == 0:
        raise HTTPException(status_code=409, detail="Match is finished or its time has expired")

    actor = db.get(MatchPlayerStats, (match_id, payload.player_id))
    if not actor:
        raise HTTPException(status_code=422, detail="Player is not registered in this match")
    target = None
    if payload.type == "kill":
        target = db.get(MatchPlayerStats, (match_id, payload.target_player_id))
        if not target:
            raise HTTPException(status_code=422, detail="Target player is not in this match")
        if actor.team_id == target.team_id:
            raise HTTPException(status_code=422, detail="Kill target must be on the opposing team")

    db.add(GameEvent(
        event_id=payload.event_id, match_id=match_id, event_type=payload.type,
        player_id=payload.player_id, target_player_id=payload.target_player_id,
        points=payload.points, boost=payload.boost, occurred_at=payload.timestamp,
        received_at=utcnow(),
    ))
    # SQL-side increments are atomic; scores are derived by summing players per team.
    where_actor = (MatchPlayerStats.match_id == match_id) & (MatchPlayerStats.player_id == payload.player_id)
    updates: dict = {}
    if payload.type == "kill":
        updates = {"kills": MatchPlayerStats.kills + 1, "score": MatchPlayerStats.score + payload.points}
        db.execute(update(MatchPlayerStats).where(
            MatchPlayerStats.match_id == match_id,
            MatchPlayerStats.player_id == payload.target_player_id,
        ).values(deaths=MatchPlayerStats.deaths + 1))
    elif payload.type == "death":
        updates = {"deaths": MatchPlayerStats.deaths + 1}
    elif payload.type == "assist":
        updates = {"assists": MatchPlayerStats.assists + 1, "score": MatchPlayerStats.score + payload.points}
    elif payload.type == "score":
        updates = {"score": MatchPlayerStats.score + payload.points}
    elif payload.type == "boost":
        updates = {"boost": payload.boost}

    db.execute(update(MatchPlayerStats).where(where_actor).values(**updates))
    match.updated_at = utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Event IDs are globally unique. A concurrent duplicate must not count twice.
        prior = db.get(GameEvent, payload.event_id)
        if prior and matches_existing_event(prior, match_id, payload):
            response.status_code = status.HTTP_200_OK
            return {"event_id": payload.event_id, "match_id": match_id, "duplicate": True}
        raise HTTPException(status_code=409, detail="event_id already in use")
    return {"event_id": payload.event_id, "match_id": match_id, "duplicate": False}


@app.get("/matches/{match_id}/leaderboard", tags=["Analytics"])
def leaderboard(match_id: int, db: Session = Depends(get_db)):
    match = get_match_or_404(db, match_id)
    return {
        "match_id": match_id,
        "status": match.status,
        "as_of": ensure_utc(match.updated_at),
        "players": [player_payload(s, p) for s, p in stats_rows(db, match_id)],
    }


@app.get("/matches/{match_id}/players/{player_id}", tags=["Analytics"])
def player_stats(match_id: int, player_id: int, db: Session = Depends(get_db)):
    get_match_or_404(db, match_id)
    record = db.execute(
        select(MatchPlayerStats, Player)
        .join(Player, Player.player_id == MatchPlayerStats.player_id)
        .where(MatchPlayerStats.match_id == match_id, MatchPlayerStats.player_id == player_id)
    ).first()
    if not record:
        raise HTTPException(status_code=404, detail="Player is not in this match")
    return {"match_id": match_id, **player_payload(*record)}


@app.get("/matches/{match_id}/stats", tags=["Analytics"])
def match_stats(match_id: int, db: Session = Depends(get_db)):
    match = get_match_or_404(db, match_id)
    rows = stats_rows(db, match_id)
    count_events = db.scalar(select(func.count(GameEvent.event_id)).where(GameEvent.match_id == match_id))
    return {
        "match_id": match_id,
        "status": match.status,
        "time_remaining": time_remaining(match),
        "as_of": ensure_utc(match.updated_at),
        "total_events": count_events,
        "total_kills": sum(s.kills for s, _ in rows),
        "total_deaths": sum(s.deaths for s, _ in rows),
        "top_player": player_payload(*rows[0]) if rows else None,
    }


@app.get("/matches/{match_id}/state", tags=["Analytics"])
def game_state(match_id: int, db: Session = Depends(get_db)):
    """Screenshot-friendly JSON containing gameID, timeRemaining, Team, Player."""
    match = get_match_or_404(db, match_id)
    rows = stats_rows(db, match_id)
    teams = db.scalars(select(Team).where(Team.match_id == match_id).order_by(Team.team_id)).all()
    team_list = []
    for team in teams:
        members = [stats for stats, _ in rows if stats.team_id == team.team_id]
        team_list.append({
            "teamID": team.team_id, "name": team.name,
            "playerIds": [s.player_id for s in members],
            "teamScore": sum(s.score for s in members),
        })
    return {
        "gameID": match_id,
        "timeRemaining": time_remaining(match),  # seconds
        "status": match.status,
        "as_of": ensure_utc(match.updated_at),
        "teams": team_list,
        "players": [
            {"playerID": p.player_id, "userName": p.username,
             "playerScore": s.score, "playerBoost": s.boost, "teamID": s.team_id}
            for s, p in rows
        ],
    }


@app.get("/matches/{match_id}/events/recent", tags=["Events"])
def recent_events(
    match_id: int, limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)
):
    get_match_or_404(db, match_id)
    events = db.scalars(
        select(GameEvent).where(GameEvent.match_id == match_id)
        .order_by(GameEvent.received_at.desc(), GameEvent.event_id.desc()).limit(limit)
    ).all()
    return {
        "match_id": match_id,
        "events": [
            {"event_id": e.event_id, "type": e.event_type,
             "timestamp": ensure_utc(e.occurred_at), "player_id": e.player_id,
             "target_player_id": e.target_player_id, "points": e.points,
             "boost": e.boost}
            for e in events
        ],
    }


@app.patch("/matches/{match_id}/finish", tags=["Matches"])
def finish_match(match_id: int, db: Session = Depends(get_db)):
    match = get_match_or_404(db, match_id, lock=True)
    if match.status != "finished":
        match.status = "finished"
        match.ended_at = utcnow()
        match.updated_at = utcnow()
        db.commit()
    return {"match_id": match_id, "status": "finished"}
