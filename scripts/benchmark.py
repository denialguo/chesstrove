"""Benchmark the pipeline.

    uv run scripts/benchmark.py                   # 2,000 random games, parse + replay only
    uv run scripts/benchmark.py my_games.pgn      # your own history
    uv run scripts/benchmark.py --db              # also import into Postgres ($CHESSTROVE_DATABASE_URL or embedded)
"""

import argparse
import os
import random
import tempfile
import time
from pathlib import Path

import chess
import chess.pgn

from chesstrove import db
from chesstrove.importers.pgn import ParseFailure, read_pgn
from chesstrove.ingest import import_pgn
from chesstrove.analysis import analyze, reanalyze
from chesstrove.detectors import DETECTORS
from chesstrove.reconstruction import replay


def random_pgn(n_games: int, seed: int = 0) -> str:
    rng = random.Random(seed)
    games = []
    for i in range(n_games):
        board = chess.Board()
        while not board.is_game_over() and board.ply() < rng.randint(20, 160):
            board.push(rng.choice(list(board.legal_moves)))
        game = chess.pgn.Game.from_board(board)
        game.headers.update(Event="bench", Round=str(i), White="w", Black="b")
        games.append(str(game))
    return "\n\n".join(games) + "\n"


def report(label: str, games: int, plies: int, seconds: float, events: int = 0) -> None:
    print(f"{label:<26} games={games:,} plies={plies:,} events={events:,} time={seconds:.2f}s "
          f"games/s={games / seconds:,.0f} plies/s={plies / seconds:,.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pgn", nargs="?", type=Path)
    ap.add_argument("--games", type=int, default=2000)
    ap.add_argument("--db", action="store_true")
    args = ap.parse_args()

    text = args.pgn.read_text(encoding="utf-8-sig", errors="replace") if args.pgn else random_pgn(args.games)

    t = time.perf_counter()
    games = [g for g in read_pgn(text) if not isinstance(g, ParseFailure)]
    plies = sum(len(g.moves_uci) for g in games)
    report("parse", len(games), plies, time.perf_counter() - t)

    t = time.perf_counter()
    for g in games:
        for _ in replay(g):
            pass
    report("replay + facts", len(games), plies, time.perf_counter() - t)

    t = time.perf_counter()
    events = sum(len(analyze(g, DETECTORS)[1]) for g in games)
    report("replay + facts + detectors", len(games), plies, time.perf_counter() - t, events)

    if args.db:
        with tempfile.TemporaryDirectory() as tmp:
            dsn = None
            if not os.environ.get("CHESSTROVE_DATABASE_URL"):
                import pgserver
                dsn = pgserver.get_server(tmp, cleanup_mode="stop").get_uri()
            with db.connect(dsn) as conn:
                db.init_schema(conn)
                t = time.perf_counter()
                import_pgn(conn, text, "benchmark")
                report("full import (to DB)", len(games), plies, time.perf_counter() - t, events)
                t = time.perf_counter()
                import_pgn(conn, text, "benchmark-again")
                report("re-import (all dupes)", len(games), plies, time.perf_counter() - t)
                t = time.perf_counter()
                run = db.get_analysis_run(conn, reanalyze(conn, force=True))
                report("reanalyze --all", run["games_processed"], plies, time.perf_counter() - t, run["events_created"])


if __name__ == "__main__":
    main()
