# Gameplay simulator (`gamesim`)

Generates live, synthetic match data and **POSTs it straight to the origin API**
(`POST /matches/{match_id}/events`). Viewers and k6 read through the caching
proxy; the simulator never goes through the proxy, so cached reads never touch
game writes.

```
gamesim ──POST /matches/{id}/events──▶ origin API ──▶ PostgreSQL
   │
   └── :9100/metrics (Prometheus)
```

What it covers from the design doc and meeting notes:

| Requirement | How |
|---|---|
| 10–20 player live arena, kills/deaths/assists every second | `mode: arena`, `players: 2–100`, `actions_per_second` |
| Choose the number of players **and** which statistics are tracked | `players`, `stats: [kills, deaths, assists, damage, headshots]` |
| Multiple games at once | `count: N` per group, any mix of groups |
| Large, intensive games (Rocket League: position, boost, shots, saves, ball pos) | `mode: rocket`, 1v1–4v4, `tick_hz` telemetry (car pos + boost 0–100, ball pos, clock, score) |
| `gameID`, `timeRemaining`, teams with `teamScore`, players with `playerScore`/`playerBoost` | `match_id`, `tick.payload.time_remaining_s`, `team_scores`, player `score`, `boost` |
| Fixed event rate so cache-on/off runs are comparable | actions and ticks on a fixed time grid |
| Repeatable runs | fixed `seed` → byte-identical event stream |
| Retries must not double count | deterministic `event_id` for dedup; 409 = already have it |

## Quick start

```bash
cd simulator
pip install -e '.[dev]'

gamesim run -c scenarios/benchmark.yaml --origin-url http://localhost:8000
gamesim run -c scenarios/rocket.yaml --sink stdout --no-wait   # no origin needed
gamesim describe -c scenarios/multi-match.yaml                  # match ids + rosters (k6 targets)
gamesim generate -c scenarios/arena.yaml -o fixture.jsonl       # whole match instantly, deterministic
gamesim schema                                                  # JSON Schema of an event
pytest
```

Docker:

```bash
docker build -t gamesim simulator/
docker run --rm -e ORIGIN_URL=http://origin:8000 \
  -e GAMESIM_CONFIG=/app/scenarios/rocket.yaml -p 9100:9100 gamesim
```

Run **one** simulator replica per scenario. Extra replicas would send the same
event ids (harmless after dedup, but double the write load).

## Scenarios (`scenarios/`)

| File | What | Match ids | Approx. write load |
|---|---|---|---|
| `benchmark.yaml` | Baseline for k6 A/B runs: 1 endless 12-player arena, 5 actions/s | 101 | ~10 POST/s |
| `arena.yaml` | 16-player FFA, all five arena stats, 2 Hz position/health ticks, restarts every 5 min | 101 → 102 → … | ~20 POST/s |
| `multi-match.yaml` | 4 FFA + 2 team-deathmatch (score limit 50) at once | 101–106 | ~30 POST/s |
| `rocket.yaml` | 2 × 3v3 car soccer, 10 Hz telemetry, overtime on ties | 101–102 | ~21 POST/s |
| `stress.yaml` | 20 rocket @ 30 Hz + 10 big arenas | 101–130 | ~840 POST/s |

An *action* is one gameplay moment, and it can produce several events. For
example, one arena kill sends `damage` events, then `kill`, `death`, any
`assist`s and maybe a `headshot`. Ticks are one event each.

### Config reference

```yaml
seed: 4207                 # same seed + config => identical events
sink: http                 # http | stdout | file | null
output_path: null          # for sink: file (JSON lines)
metrics_port: 9100         # 0 disables
speed: 1.0                 # simulated seconds per wall second (10 = 10x fast-forward)
http:
  origin_url: http://origin:8000
  events_path: /matches/{match_id}/events
  health_path: /health     # waited on at startup (wait_for_origin_s, 0 = skip)
  timeout_s: 2
  max_retries: 5           # 5xx / 429 / timeouts; exponential backoff
  concurrency_per_match: 4 # POSTs in flight per match
  queue_size: 5000         # per match; producer blocks when full
  wait_for_origin_s: 120
matches:
  - mode: arena            # arena | rocket
    count: 1               # concurrent matches in this group
    match_id_start: 101    # default: continues after the previous group
    actions_per_second: 5
    tick_hz: 0             # telemetry snapshots/s, 0 = off
    duration_s: 300        # 0 = endless
    on_end: stop           # stop | restart (new match, next unused id)
    start_offset_s: 0
    stats: [kills, deaths, assists]   # arena: kills+deaths required; + assists, damage, headshots
    players: 12            # arena only
    teams: 0               # arena only; 0 = free-for-all, else >= 2
    score_limit: null      # arena only; end at N kills (player, or team if teams)
    team_size: 3           # rocket only (1-4 per side)
    max_overtime_s: 300    # rocket only
    # rocket stats: goals+shots required; + assists, saves, demos
```

Environment overrides (handy for k8s and benchmark sweeps): `GAMESIM_CONFIG`,
`ORIGIN_URL`, `GAMESIM_SEED`, `GAMESIM_SINK`, `GAMESIM_OUTPUT_PATH`,
`GAMESIM_METRICS_PORT`, `GAMESIM_SPEED`, `GAMESIM_WAIT_FOR_ORIGIN_S`, and,
applied to every match group, `GAMESIM_ACTIONS_PER_SECOND`, `GAMESIM_TICK_HZ`,
`GAMESIM_DURATION_S`, `GAMESIM_ON_END`.

## Contract with the origin API

Schema: [`contract/gameplay-event.schema.json`](contract/gameplay-event.schema.json).
One example of every event type: [`contract/examples/`](contract/examples/).
Executable reference implementation: [`gamesim/reference.py`](gamesim/reference.py)
(`MatchState.apply` + `leaderboard_response`).

```json
POST /matches/101/events
{
  "event_id": "match101-e00612-event5",
  "match_id": 101,
  "run_id": "e00612",
  "seq": 5,
  "type": "kill",
  "timestamp": "2026-11-08T22:00:00.400000Z",
  "game_time_s": 0.4,
  "player_id": "m101p03",
  "target_player_id": "m101p01",
  "points": 100,
  "stat": "kills",
  "value": 1,
  "team_score_delta": 0
}
```

### What the origin has to do

The origin needs **no game logic**. One generic rule applies every event:

```
if event_id already stored:         return 409            (retry / restarted simulator)
if type == "match_start":           upsert match; insert players + teams from payload
                                    (existing match id with a different run_id => reset that match)
elif event.run_id != match.run_id:  ignore                (straggler from an older run)
if type == "match_end":             status = finished; keep payload as the final summary
if type == "tick":                  overwrite the match's live state (positions, boost, clock); don't need to persist history
if stat is not null:                player_stats[match_id, player_id][stat] += value
player_score[match_id, player_id]  += points
team_score[match_id, team_id]      += team_score_delta
match.version = max(match.version, seq)
```

Responses the simulator expects: `2xx` = stored, `409` = duplicate (counted
as delivered), `429`/`5xx`/timeout = retried with backoff, other `4xx` =
logged and dropped (no retry). It waits for `GET /health` → 2xx before sending.

Guarantees the origin can rely on:

- **`match_start` arrives first.** It is acknowledged before any other event of
  the match is sent, and `match_end` is sent only after every earlier event
  is acknowledged. Events in between may arrive slightly out of order (up to
  `concurrency_per_match` in flight). That's fine because every stat update is
  an increment.
- **`event_id` = `match{match_id}-{run_id}-event{seq}`.** It is unique and
  deterministic: a restarted simulator re-sends the same ids, and a new seed
  or config gets a new `run_id`.
- **`player_id`s are globally unique** (`m101p07`), so the `players` table can
  key on `player_id` alone. `team_id` is `blue`/`orange` (rocket) or `t1..tN`
  (arena), unique per match.
- **Stat names come from a fixed set:** `kills deaths assists damage headshots goals
  shots saves demos`. `match_start.payload.stats` lists the stats that match
  will produce, so the leaderboard knows which columns to show.

### Freshness / staleness hook

`seq` increases by one for every event in a match. If the origin returns
`"version": <highest seq applied>` next to `as_of` in leaderboard/stats
responses, then:

- **k6** can compare the `version` a cached response carries against the
  latest one fetched straight from the origin.
- **Grafana** can plot `gamesim_match_latest_seq - <version served by proxy>`,
  which is staleness measured in events.

`reference.py` already returns `version`.

## Metrics (`:9100/metrics`)

| Metric | Labels | Meaning |
|---|---|---|
| `gamesim_events_generated_total` | mode, type | events produced |
| `gamesim_events_sent_total` | mode, type, outcome (`ok`/`duplicate`/`failed`/`dropped`) | delivery results |
| `gamesim_send_latency_seconds` | mode | origin POST latency (histogram) |
| `gamesim_send_retries_total` | mode, reason | retries by status/exception |
| `gamesim_http_responses_total` | code | origin status codes |
| `gamesim_match_latest_seq` / `gamesim_match_acked_seq` | match_id, mode | newest event generated / acknowledged |
| `gamesim_queue_depth` | match_id | backlog waiting to be sent (grows if origin is slow) |
| `gamesim_schedule_lag_seconds` | match_id | producer behind schedule (queue full) |
| `gamesim_active_matches` | mode | running matches |
| `gamesim_matches_finished_total` | mode, reason | completed matches |

## Layout

```
simulator/
  gamesim/
    models.py        event contract (pydantic) + JSON schema
    reference.py     how the origin should apply events (spec as code)
    config.py        YAML + env config
    engines/         arena.py, rocket.py, base.py (deterministic timeline), names.py
    runner.py        real-time pacing, per-match ordered sender, offline generate()
    sinks.py         HTTP (retries, dedup-safe), stdout, file, null
    metrics.py       Prometheus metrics
    cli.py           run / generate / describe / schema
  scenarios/         ready-made YAML scenarios
  contract/          JSON schema + example events (kept in sync by a test)
  tests/             engine invariants, determinism, end-to-end against a fake origin
```

On shutdown (SIGTERM/SIGINT) the simulator stops generating, finishes the
requests already in flight, drops whatever is still queued, and exits within
a couple of seconds. It does not send `match_end`. If it restarts with the
same config, it replays each match from `seq` 0 with the same event ids. The
origin returns 409 for everything it already has, and new data resumes once
the replay passes the point where the old run stopped. To start a match over
instead, change `seed`: the new `run_id` makes the origin reset that match.
