import re
from collections import Counter
from datetime import UTC, datetime

import pytest

from gamesim.config import MatchGroup, SimConfig, load_config
from gamesim.engines import create_engine, timeline
from gamesim.models import GameplayEvent
from gamesim.reference import MatchState
from gamesim.runner import generate

EPOCH = datetime(2026, 11, 8, 22, 0, tzinfo=UTC)


def play(spec: MatchGroup, match_id=101, seed=4207):
    eng = create_engine(match_id, spec, seed, epoch=EPOCH)
    events = [ev for _, evs in timeline(eng) for ev in evs]
    state = MatchState(match_id)
    for ev in events:
        state.apply(ev)
    return events, state


def test_same_seed_same_stream():
    spec = MatchGroup(mode="rocket", tick_hz=10, actions_per_second=1, duration_s=60)
    a, _ = play(spec)
    b, _ = play(spec)
    assert [e.to_json_dict() for e in a] == [e.to_json_dict() for e in b]
    c, _ = play(spec, seed=1)
    assert [e.to_json_dict() for e in a] != [e.to_json_dict() for e in c]


def test_lifecycle_and_seq():
    events, state = play(MatchGroup(mode="arena", duration_s=30))
    assert events[0].type == "match_start" and events[0].seq == 0
    assert events[-1].type == "match_end"
    assert [e.seq for e in events] == list(range(len(events)))
    assert len({e.event_id for e in events}) == len(events)
    assert all(e.match_id == 101 for e in events)
    assert state.status == "finished" and state.version == events[-1].seq
    times = [e.game_time_s for e in events]
    assert times == sorted(times) and times[-1] == 30.0


def test_events_roundtrip_through_json():
    events, _ = play(MatchGroup(mode="rocket", tick_hz=5, duration_s=20))
    for ev in events:
        assert GameplayEvent.model_validate(ev.to_json_dict()) == ev


def test_doc_example_shape():
    """The POST body matches the shape in the design doc."""
    events, _ = play(MatchGroup(mode="arena", duration_s=10))
    kill = next(e for e in events if e.type == "kill").to_json_dict()
    for key in ("event_id", "type", "timestamp", "player_id", "target_player_id", "points"):
        assert key in kill
    assert kill["points"] == 100
    assert re.fullmatch(r"match101-[0-9a-f]{6}-event\d+", kill["event_id"])


def test_fixed_action_rate():
    events, _ = play(MatchGroup(mode="arena", actions_per_second=5, duration_s=60))
    kills_or_skirmish_moments = {e.game_time_s for e in events if e.type not in ("match_start", "match_end")}
    # one moment every 0.2s, skirmishes with damage disabled emit nothing
    assert len(kills_or_skirmish_moments) <= 5 * 60
    assert len(kills_or_skirmish_moments) > 5 * 60 * 0.6


@pytest.mark.parametrize("teams", [0, 2, 3])
def test_arena_invariants(teams):
    stats = ["kills", "deaths", "assists", "damage", "headshots"]
    events, st = play(MatchGroup(mode="arena", players=12, teams=teams, stats=stats, duration_s=120))
    rows = st.players.values()
    total = lambda s: sum(r.stats.get(s, 0) for r in rows)  # noqa: E731
    assert total("kills") == total("deaths") > 0
    assert total("headshots") <= total("kills")
    assert total("damage") >= 100 * total("kills")  # every kill needs 100 damage
    for r in rows:
        assert r.score == 100 * r.stats["kills"] + 50 * r.stats["assists"] + 25 * r.stats["headshots"]
    for e in events:
        if e.type == "kill":
            assert e.player_id != e.target_player_id
            if teams:
                assert st.players[e.player_id].team_id != st.players[e.target_player_id].team_id
    if teams:
        assert sum(st.team_scores.values()) == total("kills")
        assert len(st.team_scores) == teams


def test_arena_stat_selection():
    events, st = play(MatchGroup(mode="arena", stats=["kills", "deaths"], duration_s=30))
    assert {e.type for e in events} == {"match_start", "kill", "death", "match_end"}
    assert set(st.leaderboard()[0]) == {"rank", "player_id", "name", "score", "kills", "deaths"}


def test_arena_score_limit():
    events, st = play(MatchGroup(mode="arena", teams=2, players=10, score_limit=20, duration_s=600))
    end = events[-1]
    assert end.payload.reason == "score_limit"
    assert max(st.team_scores.values()) == 20
    assert end.payload.winner_team_id == max(st.team_scores, key=st.team_scores.get)


def test_rocket_invariants():
    events, st = play(MatchGroup(mode="rocket", tick_hz=10, actions_per_second=1, duration_s=300))
    c = Counter(e.type for e in events)
    assert c["shot"] >= c["goal"] and c["shot"] >= c["save"]
    assert sum(st.team_scores.values()) == c["goal"]
    end = events[-1].payload
    assert end.team_scores == dict(st.team_scores)
    if end.reason == "time_expired" and end.winner_team_id:
        assert end.team_scores[end.winner_team_id] == max(end.team_scores.values())
    # a goal is always immediately preceded by that player's shot
    for i, e in enumerate(events):
        if e.type == "goal":
            assert events[i - 1].type == "shot" and events[i - 1].player_id == e.player_id
        if e.type in ("demo", "save"):
            assert st.players[e.player_id].team_id != st.players[e.target_player_id].team_id


def test_rocket_ticks():
    events, st = play(MatchGroup(mode="rocket", team_size=2, tick_hz=10, actions_per_second=0.5, duration_s=30))
    ticks = [e for e in events if e.type == "tick"]
    assert len(ticks) in (299, 300)  # last grid point coincides with full time
    for t in ticks:
        p = t.payload
        assert len(p.players) == 4 and p.ball is not None
        assert all(0 <= s.boost <= 100 for s in p.players)
        assert all(abs(s.pos.x) <= 4096 and abs(s.pos.y) <= 5120 for s in p.players)
        assert t.stat is None and t.points == 0
    assert ticks[0].payload.time_remaining_s == pytest.approx(29.9)
    assert st.live is not None and st.live["kind"] == "tick"


def test_rocket_overtime_ends_on_goal_or_cap():
    # scan seeds until a regulation tie happens, then check overtime behaviour
    for seed in range(200):
        events, _ = play(
            MatchGroup(mode="rocket", actions_per_second=0.2, duration_s=60, max_overtime_s=120), seed=seed
        )
        end = events[-1].payload
        if end.elapsed_s > 60:
            assert end.reason in ("overtime_goal", "time_expired")
            if end.reason == "overtime_goal":
                b, o = end.team_scores["blue"], end.team_scores["orange"]
                assert abs(b - o) == 1 and end.winner_team_id == ("blue" if b > o else "orange")
            assert end.elapsed_s <= 180
            return
    pytest.skip("no tied match in 200 seeds")


def test_endless_match_runs_until_capped():
    cfg = SimConfig(matches=[MatchGroup(mode="arena", duration_s=0)])
    events = list(generate(cfg, max_game_s=10))
    assert events[0].type == "match_start"
    assert all(e.type != "match_end" for e in events)
    assert events[-1].game_time_s <= 10


def test_generate_merges_matches_by_time():
    cfg = SimConfig(
        matches=[
            MatchGroup(mode="arena", count=2, duration_s=20),
            MatchGroup(mode="rocket", count=1, tick_hz=5, duration_s=20),
        ]
    )
    events = list(generate(cfg))
    assert {e.match_id for e in events} == {101, 102, 103}
    assert [e.game_time_s for e in events] == sorted(e.game_time_s for e in events)


def test_player_ids_unique_across_matches():
    cfg = SimConfig(matches=[MatchGroup(mode="arena", count=3, duration_s=5)])
    starts = [e for e in generate(cfg) if e.type == "match_start"]
    ids = [p.player_id for s in starts for p in s.payload.players]
    assert len(ids) == len(set(ids)) == 36


def test_config_validation(tmp_path):
    with pytest.raises(ValueError):
        MatchGroup(mode="arena", stats=["kills", "goals"])
    with pytest.raises(ValueError):
        MatchGroup(mode="arena", stats=["kills"])  # deaths required
    with pytest.raises(ValueError):
        SimConfig(
            matches=[MatchGroup(mode="arena", match_id_start=1, count=3), MatchGroup(mode="arena", match_id_start=2)]
        )
    f = tmp_path / "c.yaml"
    f.write_text("matches:\n  - mode: rocket\n    count: 2\n  - mode: arena\n")
    cfg = load_config(f, env={"ORIGIN_URL": "http://x:1", "GAMESIM_ACTIONS_PER_SECOND": "9"})
    assert cfg.match_ids() == [101, 102, 103]
    assert cfg.http.origin_url == "http://x:1"
    assert all(g.actions_per_second == 9 for g in cfg.matches)


def test_reference_dedups():
    events, st = play(MatchGroup(mode="arena", duration_s=10))
    before = st.leaderboard_response()
    for ev in events:
        assert st.apply(ev) is False
    assert st.leaderboard_response() == before


def test_rerun_with_new_seed_replaces_match_same_seed_is_idempotent():
    spec = MatchGroup(mode="arena", duration_s=20)
    run_a, st = play(spec, seed=1)
    snapshot = st.leaderboard_response()
    # simulator pod restarts with the same config: every event is a duplicate
    assert not any(st.apply(e) for e in run_a)
    assert st.leaderboard_response() == snapshot
    # new seed reusing match 101: ids differ, match_start resets, new results win
    run_b, fresh = play(spec, seed=2)
    assert {e.event_id for e in run_a}.isdisjoint(e.event_id for e in run_b)
    for e in run_b:
        assert st.apply(e)
    assert st.leaderboard() == fresh.leaderboard()
    # late stragglers from run A are ignored
    assert st.apply(run_a[5].model_copy(update={"event_id": "late-retry"})) is False
    assert st.leaderboard() == fresh.leaderboard()


def test_committed_schema_is_current():
    import json
    from pathlib import Path

    from gamesim.models import event_json_schema

    path = Path(__file__).parent.parent / "contract" / "gameplay-event.schema.json"
    assert json.loads(path.read_text()) == event_json_schema(), "run: python -m gamesim schema -o " + str(path)


def test_non_stat_events_have_zero_value():
    events, _ = play(MatchGroup(mode="rocket", tick_hz=5, duration_s=5))
    for e in events:
        assert (e.stat is None) == (e.value == 0)
