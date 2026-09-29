"""Benchmark Stockfish analysis on real games, in a throwaway database (never touches your data).

    uv run scripts/benchmark_engine.py games.pgn --games 100 --nodes 25000 100000
    uv run scripts/benchmark_engine.py games.pgn --games 300 --nodes 10000 --workers 1 2 4 8 10 13

Reports, per node limit: positions analyzed, positions/sec, games, wall-clock time, time spent inside
searches, worker count. Pick settings from these numbers; don't guess.
"""

import argparse
import tempfile
import time
from pathlib import Path

import pgserver

from chesstrove import db, engine
from chesstrove.importers.pgn import read_pgn
from chesstrove.ingest import run_import


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pgn", type=Path)
    ap.add_argument("--games", type=int, default=100)
    ap.add_argument("--nodes", type=int, nargs="+", default=[25_000, 100_000])
    ap.add_argument("--multipv", type=int, default=1)
    ap.add_argument("--workers", type=int, nargs="+", default=[1])
    args = ap.parse_args()

    text = args.pgn.read_text(encoding="utf-8-sig", errors="replace")
    games = [g for g in read_pgn(text) if hasattr(g, "moves_uci")][: args.games]
    with tempfile.TemporaryDirectory() as tmp:
        with db.connect(pgserver.get_server(tmp, cleanup_mode="stop").get_uri()) as conn:
            db.init_schema(conn)
            run_import(conn, games, "pgn", "benchmark sample")
            positions = conn.execute("SELECT sum(ply_count + 1) AS n FROM games").fetchone()["n"]
            print(f"sample: {len(games)} games, {positions:,} positions "
                  f"({positions / len(games):.0f} per game)\n")
            baseline = None
            for nodes, workers in [(n, w) for n in args.nodes for w in args.workers]:
                conn.execute("TRUNCATE engine_configs CASCADE")  # same work every time
                settings = engine.EngineSettings(limit_value=nodes, multipv=args.multipv)
                t = time.perf_counter()
                run_id = engine.run(conn, settings, workers=workers)
                wall = time.perf_counter() - t
                [run] = [r for r in db.list_engine_runs(conn) if r["id"] == run_id]
                rate = run["positions_done"] / wall
                baseline = baseline or rate
                print(f"nodes={nodes:>9,}  multipv={args.multipv}  workers={run['workers']:>2}  speedup={rate / baseline:4.1f}x  "
                      f"games={run['games_done']}  positions={run['positions_done']:,}  "
                      f"wall={wall:,.0f}s  in-search={run['engine_seconds']:,.0f}s  "
                      f"positions/s={run['positions_done'] / wall:,.1f}  "
                      f"ms/position={wall / run['positions_done'] * 1000:,.0f}", flush=True)
            size = conn.execute("SELECT pg_total_relation_size('engine_positions') AS b, count(*) AS n "
                                "FROM engine_positions").fetchone()
            print(f"\nstorage: {size['b'] / size['n']:,.0f} bytes per stored position (incl. index)")


if __name__ == "__main__":
    main()
