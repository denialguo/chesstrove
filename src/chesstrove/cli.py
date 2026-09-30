"""Minimal CLI. Database: $CHESSTROVE_DATABASE_URL if set, else a built-in Postgres in ~/.chesstrove."""

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import psycopg

from chesstrove import db, engine, insights, labels
from chesstrove.analysis import reanalyze
from chesstrove.detectors import DETECTORS
from chesstrove.ingest import import_chesscom, import_lichess, import_pgn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chesstrove")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create tables (idempotent)")
    p = sub.add_parser("import-pgn", help="import one or more PGN files")
    p.add_argument("files", nargs="+", type=Path)
    p = sub.add_parser("import-chesscom", help="import (or catch up) a Chess.com game history")
    p.add_argument("username")
    p.add_argument("--user", default="me", help="local ChessTrove user who owns the account")
    p = sub.add_parser("import-lichess", help="import (or catch up) a Lichess game history; set LICHESS_TOKEN to go faster")
    p.add_argument("username")
    p.add_argument("--user", default="me", help="local ChessTrove user who owns the account")
    sub.add_parser("imports", help="list imports and their status")
    p = sub.add_parser("games", help="list games")
    p.add_argument("--player")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    p = sub.add_parser("game", help="show one game with its moves and events")
    p.add_argument("id", type=int)
    p = sub.add_parser("events", help="search detected events")
    p.add_argument("--type", help="e.g. UNDERPROMOTION (see `detectors`)")
    p.add_argument("--color", choices=["w", "b"])
    p.add_argument("--player", help="only moves played by this username")
    p.add_argument("--since", type=date.fromisoformat, help="YYYY-MM-DD, inclusive")
    p.add_argument("--until", type=date.fromisoformat, help="YYYY-MM-DD, inclusive")
    p.add_argument("--game", type=int)
    p.add_argument("--engine", action="store_true", help="attach Stockfish analysis where the game has been analyzed")
    p.add_argument("--engine-config", type=int, help="which engine config (default: the one covering most games)")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    sub.add_parser("detectors", help="list detectors and their versions")
    p = sub.add_parser("serve", help="run the REST API (docs at /docs)")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 on a host (set CHESSTROVE_PUBLIC=1 there)")
    p = sub.add_parser("reanalyze", help="re-run detectors over stored games (only stale games unless --all)")
    p.add_argument("--detector", action="append", help="repeatable; default: all detectors")
    p.add_argument("--all", action="store_true", help="redo every game, not just stale ones")
    sub.add_parser("status", help="what's imported and how far each analysis layer has got")
    p = sub.add_parser("engine", help="Stockfish analysis of every position (Layer 2)")
    esub = p.add_subparsers(dest="engine_command", required=True)
    p = esub.add_parser("analyze", help="analyze every game without results for these settings; resumable, Ctrl-C safe")
    limit = p.add_mutually_exclusive_group()
    limit.add_argument("--nodes", type=int, help=f"nodes per position (default {engine.EngineSettings().limit_value:,})")
    limit.add_argument("--depth", type=int, help="fixed depth per position instead of nodes")
    p.add_argument("--multipv", type=int, default=1, help="lines per position (costs roughly proportionally)")
    p.add_argument("--hash", type=int, default=engine.EngineSettings().hash_mb, help="hash MB")
    p.add_argument("--max-games", type=int, help="stop after this many games (e.g. to sample or benchmark)")
    p.add_argument("--workers", type=int, default=engine.default_workers(),
                   help="parallel Stockfish processes, 1 thread each (default: CPUs - 1)")
    p.add_argument("--stockfish", help="path to the binary (default: $CHESSTROVE_STOCKFISH, then PATH)")
    esub.add_parser("runs", help="list engine runs")
    p = esub.add_parser("labels", help="BLUNDER / MISSED_WIN / ONLY_WINNING_MOVE from stored evaluations (instant)")
    p.add_argument("--type", choices=labels.LABELS, required=True)
    p.add_argument("--player", help="only moves played by this username")
    p.add_argument("--config", type=int, help="engine config (default: the one covering most games)")
    p.add_argument("--blunder", type=float, default=labels.Thresholds().blunder, help="expected-score drop")
    p.add_argument("--winning", type=float, default=labels.Thresholds().winning)
    p.add_argument("--not-winning", type=float, default=labels.Thresholds().not_winning)
    p.add_argument("--scale", choices=labels.SCALES, default="lichess",
                   help="lichess: human-calibrated win%% curve (default); stockfish: engine WDL (much steeper)")
    p.add_argument("--include-recaptures", action="store_true", help="ONLY_WINNING_MOVE: keep obvious recaptures")
    p.add_argument("--limit", type=int, default=20)
    p = esub.add_parser("verify-underpromotions", help="re-ask both underpromotion questions at a stronger setting")
    strength = p.add_mutually_exclusive_group()
    strength.add_argument("--depth", type=int, default=22, help="search depth for every line (default 22)")
    strength.add_argument("--nodes", type=int, help="recompute a full-history node config's own probes")
    p.add_argument("--refresh", action="store_true", help="recompute probes that already exist")
    p.add_argument("--workers", type=int, default=engine.default_workers())
    p.add_argument("--stockfish")
    p = esub.add_parser("verify-only-moves", help="two-line searches where ONLY_WINNING_MOVE can apply (resumable)")
    p.add_argument("--refresh", action="store_true", help="recompute probes that already exist")
    p.add_argument("--player", help="only this username's moves (fewer candidates)")
    p.add_argument("--config", type=int)
    p.add_argument("--winning", type=float, default=labels.Thresholds().winning)
    p.add_argument("--workers", type=int, default=engine.default_workers())
    p.add_argument("--stockfish")
    p = esub.add_parser("verify-unusual-moves",
                        help="two-line searches for the unusual-move discovery: engine-choice moves in undecided positions (resumable)")
    p.add_argument("--player", help="only this username's moves (recommended: the set is per player)")
    p.add_argument("--config", type=int)
    p.add_argument("--workers", type=int, default=engine.default_workers())
    p.add_argument("--stockfish")
    args = parser.parse_args(argv)

    try:
        conn = db.connect()
    except psycopg.OperationalError as e:
        hint = ("\n(it comes from $CHESSTROVE_DATABASE_URL; unset it to use the built-in database)"
                if os.environ.get("CHESSTROVE_DATABASE_URL") else "")
        print(f"can't connect to the database: {e}{hint}", file=sys.stderr)
        return 1
    with conn:
        db.init_schema(conn)
        match args.command:
            case "init-db":
                print("schema ready")
            case "import-pgn":
                for path in args.files:
                    import_id = import_pgn(conn, path.read_text(encoding="utf-8-sig", errors="replace"), str(path))
                    _print(db.get_import(conn, import_id))
            case "import-chesscom":
                _print(db.get_import(conn, import_chesscom(conn, args.username, args.user)))
            case "import-lichess":
                _print(db.get_import(conn, import_lichess(conn, args.username, args.user)))
            case "imports":
                _print(db.list_imports(conn))
            case "games":
                _print(db.list_games(conn, args.player, args.limit, args.offset))
            case "game":
                game = db.get_game(conn, args.id)
                if game is None:
                    print(f"no game {args.id}", file=sys.stderr)
                    return 1
                game["events"] = db.list_events(conn, game_id=args.id, limit=10_000)
                _print(game)
            case "events":
                found = db.list_events(conn, args.type, args.color, args.player, args.since, args.until,
                                       args.game, args.limit, args.offset)
                _print(insights.annotate(conn, found, args.engine_config) if args.engine or args.engine_config else found)
            case "detectors":
                _print([{"id": d.id, "version": d.version, "definition": (d.__doc__ or "").strip()} for d in DETECTORS])
            case "serve":
                import uvicorn

                uvicorn.run("chesstrove.api:app", host=args.host, port=args.port, proxy_headers=True)
            case "reanalyze":
                _print(db.get_analysis_run(conn, reanalyze(conn, args.detector, force=args.all)))
            case "status":
                _print_status(db.status_summary(conn, {d.id: d.version for d in DETECTORS}))
            case "engine":
                if args.engine_command == "runs":
                    _print(db.list_engine_runs(conn))
                    return 0
                if args.engine_command == "labels":
                    t = labels.Thresholds(args.blunder, args.winning, args.not_winning)
                    _print(labels.query(conn, args.type, t, args.config, args.player, args.limit, args.scale,
                                        args.include_recaptures))
                    return 0
                if args.engine_command == "verify-underpromotions":
                    try:
                        settings = (engine.EngineSettings(limit_value=args.nodes) if args.nodes
                                    else engine.EngineSettings(limit_kind="depth", limit_value=args.depth))
                        _print(engine.verify_underpromotions(conn, settings, args.stockfish, args.workers,
                                                             refresh=args.refresh))
                    except FileNotFoundError as e:
                        print(e, file=sys.stderr)
                        return 1
                    return 0
                if args.engine_command in ("verify-only-moves", "verify-unusual-moves"):
                    def show(p: dict) -> None:
                        print(f"\r{p['done']:,}/{p['total']:,} positions", end="", file=sys.stderr, flush=True)
                    try:
                        if args.engine_command == "verify-only-moves":
                            result = engine.verify_only_winning_moves(conn, args.config, args.winning, args.player,
                                                                      args.stockfish, args.workers, show,
                                                                      refresh=args.refresh)
                        else:
                            result = engine.verify_unusual_moves(conn, args.config, args.player, args.stockfish,
                                                                 args.workers, show)
                    except (ValueError, FileNotFoundError) as e:
                        print(e, file=sys.stderr)
                        return 1
                    print(file=sys.stderr)
                    _print(result)
                    return 0
                settings = engine.EngineSettings(
                    limit_kind="depth" if args.depth else "nodes",
                    limit_value=args.depth or args.nodes or engine.EngineSettings().limit_value,
                    multipv=args.multipv, hash_mb=args.hash,
                )
                try:
                    run_id = engine.run(conn, settings, args.stockfish, args.max_games, _engine_progress,
                                        workers=args.workers)
                except FileNotFoundError as e:
                    print(e, file=sys.stderr)
                    return 1
                except KeyboardInterrupt:
                    print("\ncancelled; finished games are saved, run the same command to resume", file=sys.stderr)
                    return 130
                print(file=sys.stderr)
                _print([r for r in db.list_engine_runs(conn, 1) if r["id"] == run_id][0])
    return 0


def _print(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _print_status(s: dict) -> None:
    games = s["games"] or 1
    print(f"{s['games']:,} games imported · {s['positions']:,} positions indexed")
    done = s["deterministic_done"]
    print(f"Deterministic analysis: {'complete' if done == s['games'] else f'{done / games:.0%} (run `chesstrove reanalyze`)'}")
    if not s["engine"]:
        print("Stockfish analysis: not started (`chesstrove engine analyze`)")
    for e in s["engine"]:
        label = f"{e['engine_name']}, {e['limit_kind']}={e['limit_value']:,}"
        if e["multipv"] > 1:
            label += f", multipv={e['multipv']}"
        share = e["positions_done"] / (s["positions"] or 1)
        print(f"Stockfish analysis [{label}]: {share:.0%} ({e['games_done']:,}/{s['games']:,} games)")


def _engine_progress(p: dict) -> None:
    left = p["games_total"] - p["games_done"]
    per_game = p["elapsed"] / p["games_done"]
    print(f"\r{p['games_done']:,}/{p['games_total']:,} games · {p['positions']:,} positions · "
          f"{p['positions_per_sec']:.1f} positions/s · ~{left * per_game / 60:.0f} min left at this pace   ",
          end="", file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
