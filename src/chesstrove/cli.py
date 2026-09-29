"""Minimal CLI. Database from $CHESSTROVE_DATABASE_URL (default postgresql:///chesstrove)."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from chesstrove import db
from chesstrove.analysis import reanalyze
from chesstrove.detectors import DETECTORS
from chesstrove.ingest import import_chesscom, import_pgn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chesstrove")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create tables (idempotent)")
    p = sub.add_parser("import-pgn", help="import one or more PGN files")
    p.add_argument("files", nargs="+", type=Path)
    p = sub.add_parser("import-chesscom", help="import (or catch up) a Chess.com game history")
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
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    sub.add_parser("detectors", help="list detectors and their versions")
    p = sub.add_parser("serve", help="run the REST API (docs at /docs)")
    p.add_argument("--port", type=int, default=8000)
    p = sub.add_parser("reanalyze", help="re-run detectors over stored games (only stale games unless --all)")
    p.add_argument("--detector", action="append", help="repeatable; default: all detectors")
    p.add_argument("--all", action="store_true", help="redo every game, not just stale ones")
    args = parser.parse_args(argv)

    with db.connect() as conn:
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
                _print(db.list_events(conn, args.type, args.color, args.player, args.since, args.until,
                                      args.game, args.limit, args.offset))
            case "detectors":
                _print([{"id": d.id, "version": d.version, "definition": (d.__doc__ or "").strip()} for d in DETECTORS])
            case "serve":
                import uvicorn

                uvicorn.run("chesstrove.api:app", host="127.0.0.1", port=args.port)  # no auth: localhost only
            case "reanalyze":
                _print(db.get_analysis_run(conn, reanalyze(conn, args.detector, force=args.all)))
    return 0


def _print(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
