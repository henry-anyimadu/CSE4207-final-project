"""Prometheus metrics exposed by the simulator (default :9100/metrics).

Useful Grafana queries:
  rate(gamesim_events_sent_total{outcome="ok"}[1m])            -> write load on the origin
  gamesim_match_latest_seq - on(match_id) <origin served version> -> leaderboard staleness in events
  histogram_quantile(0.95, rate(gamesim_send_latency_seconds_bucket[1m]))
  gamesim_schedule_lag_seconds > 0                              -> origin can't keep up with the write rate
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info, start_http_server

EVENTS_GENERATED = Counter("gamesim_events_generated_total", "Events produced by the game engines", ["mode", "type"])
EVENTS_SENT = Counter(
    "gamesim_events_sent_total", "Events delivered (or given up on) by the sink", ["mode", "type", "outcome"]
)
SEND_RETRIES = Counter("gamesim_send_retries_total", "HTTP retries to the origin", ["mode", "reason"])
HTTP_RESPONSES = Counter("gamesim_http_responses_total", "Origin responses by status code", ["code"])
SEND_LATENCY = Histogram(
    "gamesim_send_latency_seconds",
    "Origin POST latency per attempt",
    ["mode"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
QUEUE_DEPTH = Gauge("gamesim_queue_depth", "Events waiting to be sent", ["match_id"])
SCHEDULE_LAG = Gauge(
    "gamesim_schedule_lag_seconds", "How far the producer is behind its real-time schedule", ["match_id"]
)
LATEST_SEQ = Gauge("gamesim_match_latest_seq", "Highest seq generated for the match", ["match_id", "mode"])
ACKED_SEQ = Gauge("gamesim_match_acked_seq", "Highest seq acknowledged by the origin", ["match_id", "mode"])
ACTIVE_MATCHES = Gauge("gamesim_active_matches", "Matches currently running", ["mode"])
MATCHES_FINISHED = Counter("gamesim_matches_finished_total", "Matches that reached match_end", ["mode", "reason"])
INFO = Info("gamesim", "Simulator run configuration")


def serve(port: int) -> None:
    if port:
        start_http_server(port)


def forget_match(match_id: int, mode: str) -> None:
    """Drop per-match series once a match is over so restarts don't leak labels."""
    mid = str(match_id)
    for g in (QUEUE_DEPTH, SCHEDULE_LAG):
        try:
            g.remove(mid)
        except KeyError:
            pass
    for g in (LATEST_SEQ, ACKED_SEQ):
        try:
            g.remove(mid, mode)
        except KeyError:
            pass
