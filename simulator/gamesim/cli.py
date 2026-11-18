"""Command line entry point.

gamesim run       [--config FILE] [overrides]   stream events to the origin in real time
gamesim generate  [--config FILE] [--out FILE]  dump the deterministic event stream (no sleeping)
gamesim describe  [--config FILE]               print match ids / players (for k6 targets)
gamesim schema    [--out FILE]                  print the event JSON Schema
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys

from . import metrics
from .config import load_config
from .engines import create_engine
from .models import event_json_schema
from .runner import FIXED_EPOCH, generate, run


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", "-c", help="Scenario YAML (default: $GAMESIM_CONFIG or one arena match)")
    p.add_argument("--seed", type=int)


def _load(args):
    cfg = load_config(args.config)
    if args.seed is not None:
        cfg.seed = args.seed
    return cfg


def cmd_run(args) -> int:
    cfg = _load(args)
    if args.origin_url:
        cfg.http.origin_url = args.origin_url
    if args.sink:
        cfg.sink = args.sink
    if args.speed:
        cfg.speed = args.speed
    if args.no_wait:
        cfg.http.wait_for_origin_s = 0
    if cfg.sink == "file" and not cfg.output_path:
        raise SystemExit("sink=file needs output_path in the config or GAMESIM_OUTPUT_PATH")
    metrics.INFO.info(
        {
            "seed": str(cfg.seed),
            "sink": cfg.sink,
            "origin": cfg.http.origin_url,
            "matches": ",".join(map(str, cfg.match_ids())),
        }
    )
    metrics.serve(cfg.metrics_port)
    if cfg.metrics_port:
        logging.getLogger("gamesim").info("metrics on :%d/metrics", cfg.metrics_port)

    async def main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows
                pass
        try:
            stats = await run(cfg, stop=stop, max_wall_s=args.max_seconds)
        except RuntimeError as e:  # origin never became healthy
            logging.getLogger("gamesim").error("%s", e)
            return 1
        return 0 if stats.sent["failed"] == 0 or not args.strict else 2

    return asyncio.run(main())


def cmd_generate(args) -> int:
    cfg = _load(args)
    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    n = 0
    try:
        for ev in generate(cfg, max_game_s=args.max_game_seconds):
            out.write(json.dumps(ev.to_json_dict(), separators=(",", ":")) + "\n")
            n += 1
    finally:
        if args.out:
            out.close()
    print(f"wrote {n} events", file=sys.stderr)
    return 0


def cmd_describe(args) -> int:
    cfg = _load(args)
    matches = []
    for g in cfg.matches:
        for i in range(g.count):
            mid = g.match_id_start + i
            e = create_engine(mid, g, cfg.seed, epoch=FIXED_EPOCH)
            matches.append(
                {
                    "match_id": mid,
                    "mode": g.mode.value,
                    "duration_s": g.duration_s,
                    "actions_per_second": g.actions_per_second,
                    "tick_hz": g.tick_hz,
                    "on_end": g.on_end,
                    "stats": g.stats,
                    "teams": [t.team_id for t in e.teams],
                    "players": [p.model_dump(exclude_none=True) for p in e.players],
                }
            )
    print(json.dumps({"seed": cfg.seed, "origin_url": cfg.http.origin_url, "matches": matches}, indent=2))
    return 0


def cmd_schema(args) -> int:
    text = json.dumps(event_json_schema(), indent=2) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("GAMESIM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(prog="gamesim", description="Live gameplay simulator for the CSE 4207 caching project")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("run", help="stream events to the origin in real time (default)")
    _common(p)
    p.add_argument("--origin-url")
    p.add_argument("--sink", choices=["http", "stdout", "file", "null"])
    p.add_argument("--speed", type=float, help="simulated seconds per real second")
    p.add_argument("--max-seconds", type=float, help="stop after this many wall-clock seconds")
    p.add_argument("--no-wait", action="store_true", help="don't wait for origin /health")
    p.add_argument("--strict", action="store_true", help="exit 2 if any event failed to deliver")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("generate", help="write the deterministic event stream as JSON lines")
    _common(p)
    p.add_argument("--out", "-o")
    p.add_argument("--max-game-seconds", type=float)
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("describe", help="print match ids and rosters as JSON")
    _common(p)
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("schema", help="print the event JSON Schema")
    p.add_argument("--out", "-o")
    p.set_defaults(func=cmd_schema)

    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["run", *argv]
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
