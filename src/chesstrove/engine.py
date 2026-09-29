"""Layer 2: Stockfish analysis of every position, persisted per engine config. See ARCHITECTURE.md.

Reproducibility rules: node/depth limits only (never time), Threads=1, hash cleared per game
(`ucinewgame`), positions searched in order 0..N with the game's move history. Under these rules a
config re-run on a game reproduces its results exactly.
"""

import contextlib
import hashlib
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import chess
import chess.engine
import psycopg

from chesstrove import db
from chesstrove.models import CanonicalGame
from chesstrove.reconstruction import start_board

PV_MAX = 12  # plies of principal variation kept per position; bounds storage
FETCH_BATCH = 50
INFO = chess.engine.INFO_BASIC | chess.engine.INFO_SCORE | chess.engine.INFO_PV


@dataclass(frozen=True, slots=True)
class EngineSettings:
    """Everything that affects results, except the engine's own name/version (read from the binary)."""

    limit_kind: Literal["nodes", "depth"] = "nodes"
    limit_value: int = 100_000
    multipv: int = 1
    threads: int = 1
    hash_mb: int = 64

    def limit(self) -> chess.engine.Limit:
        return chess.engine.Limit(**{self.limit_kind: self.limit_value})


@dataclass(frozen=True, slots=True)
class PositionResult:
    position: int
    score_cp: int | None  # White's point of view; exactly one of score_cp / mate is set
    mate: int | None  # moves to mate, White's POV; 0 = side to move is checkmated
    wdl: tuple[int, int, int] | None
    best_uci: str | None
    pv_uci: list[str] | None
    multipv: list[dict] | None
    depth: int | None
    seldepth: int | None
    nodes: int | None


def stockfish_path(explicit: str | None = None) -> str:
    path = explicit or os.environ.get("CHESSTROVE_STOCKFISH") or shutil.which("stockfish")
    if not path:
        raise FileNotFoundError("Stockfish not found: `brew install stockfish`, or pass --stockfish / set CHESSTROVE_STOCKFISH")
    return path


def open_stockfish(path: str, settings: EngineSettings) -> chess.engine.SimpleEngine:
    engine = chess.engine.SimpleEngine.popen_uci(path)
    engine.configure({"Threads": settings.threads, "Hash": settings.hash_mb, "UCI_ShowWDL": True})
    return engine


def analyze_game(engine: Any, settings: EngineSettings, game: CanonicalGame) -> list[PositionResult]:
    """Every position of the game, 0..N, in order. The board carries the move history, so the engine
    sees repetitions and the 50-move counter (python-chess sends `position <start> moves ...`)."""
    board = start_board(game)
    new_game = object()  # a fresh token makes python-chess send `ucinewgame`: hash cleared per game
    results = [_analyze_position(engine, settings, board, 0, new_game)]
    for position, uci in enumerate(game.moves_uci, start=1):
        board.push_uci(uci)
        results.append(_analyze_position(engine, settings, board, position, new_game))
    return results


def _analyze_position(engine: Any, settings: EngineSettings, board: chess.Board, position: int, game: object) -> PositionResult:
    if not any(board.legal_moves):  # checkmate or stalemate: the rules decide, there's nothing to search
        mated = board.is_check()
        return PositionResult(position, None if mated else 0, 0 if mated else None, None, None, None, None, None, None, None)
    infos = engine.analyse(board, settings.limit(), multipv=settings.multipv, game=game, info=INFO)
    top = infos[0]
    score = top["score"].white()
    wdl = top["wdl"].white() if "wdl" in top else None
    pv = [m.uci() for m in top.get("pv", [])[:PV_MAX]]
    lines = None
    if settings.multipv > 1:
        lines = [{"uci": i["pv"][0].uci(), "score_cp": i["score"].white().score(), "mate": i["score"].white().mate()}
                 for i in infos if i.get("pv")]
    return PositionResult(
        position, score.score(), score.mate(), (wdl.wins, wdl.draws, wdl.losses) if wdl else None,
        pv[0] if pv else None, pv or None, lines, top.get("depth"), top.get("seldepth"), top.get("nodes"),
    )


def run(
    conn: psycopg.Connection,
    settings: EngineSettings = EngineSettings(),
    stockfish: str | None = None,
    max_games: int | None = None,
    progress: Callable[[dict], None] | None = None,
    engine_factory: Callable[[], Any] | None = None,
) -> int:
    """Analyze every game that has no results under this config yet (newest first). Returns the run id.

    Each finished game is committed with its status row, so Ctrl-C or a crash loses at most the game in
    progress, and re-running resumes. A game the engine chokes on is logged, skipped for this run, and
    retried next run; the engine is restarted.
    """
    path = None
    if engine_factory is None:
        path = stockfish_path(stockfish)
        engine_factory = lambda: open_stockfish(path, settings)  # noqa: E731
    engine = engine_factory()
    try:
        config_id = db.ensure_engine_config(conn, engine.id["name"], settings)
        pending = db.count_pending_engine_games(conn, config_id)
        total = min(pending, max_games) if max_games is not None else pending
        run_id = db.start_engine_run(conn, config_id, total, path, _sha256(path) if path else None)
        done, positions, skip = 0, 0, []
        started = time.perf_counter()
        status = "failed"
        try:
            while done + len(skip) < total:
                rows = db.pending_engine_games(conn, config_id, skip, min(FETCH_BATCH, total - done - len(skip)))
                if not rows:
                    break
                for row in rows:
                    t = time.perf_counter()
                    try:
                        results = analyze_game(engine, settings, db.game_from_row(row))
                    except chess.engine.EngineError as e:  # includes the engine process dying
                        skip.append(row["id"])
                        db.record_engine_error(conn, run_id, {"game_id": row["id"], "error": f"{type(e).__name__}: {e}"})
                        with contextlib.suppress(Exception):
                            engine.quit()
                        engine = engine_factory()
                        continue
                    with conn.transaction():
                        db.insert_engine_positions(conn, config_id, row["id"], results)
                        db.mark_engine_game_done(conn, config_id, row["id"], run_id, len(results))
                        db.record_engine_progress(conn, run_id, 1, len(results), time.perf_counter() - t)
                    done += 1
                    positions += len(results)
                    if progress:
                        elapsed = time.perf_counter() - started
                        progress({"games_done": done, "games_total": total, "positions": positions,
                                  "elapsed": elapsed, "positions_per_sec": positions / elapsed if elapsed else 0})
            status = "completed"
        except KeyboardInterrupt:
            status = "cancelled"
            raise
        finally:
            db.finish_engine_run(conn, run_id, status)
    finally:
        with contextlib.suppress(Exception):  # after Ctrl-C the engine may already be gone
            engine.quit()
    return run_id


def _sha256(path: str) -> str:
    with open(os.path.realpath(path), "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()
