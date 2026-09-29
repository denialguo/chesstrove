"""Layer 2: Stockfish analysis of every position, persisted per engine config. See ARCHITECTURE.md.

Reproducibility rules: node/depth limits only (never time), Threads=1, hash cleared per game
(`ucinewgame`), positions searched in order 0..N with the game's move history. Under these rules a
config re-run on a game reproduces its results exactly.
"""

import contextlib
import functools
import hashlib
import itertools
import multiprocessing
import os
import queue
import shutil
import signal
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import chess
import chess.engine
import psycopg

from chesstrove import db
from chesstrove.models import CanonicalGame
from chesstrove.reconstruction import start_board

PV_MAX = 12  # plies of principal variation kept per position; bounds storage
FETCH_BATCH = 50  # games loaded per query while feeding workers
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


@dataclass(frozen=True, slots=True)
class Probe:
    """A restricted search (UCI `searchmoves`) from `position`, scoring only `moves`, all in one search."""

    position: int
    moves: tuple[str, ...]
    results: tuple[dict, ...]  # ranked [{uci, score_cp, mate}], White's POV


@dataclass(frozen=True, slots=True)
class GameAnalysis:
    game_id: int
    positions: list[PositionResult]
    probes: list[Probe]
    seconds: float


@dataclass(frozen=True, slots=True)
class GameFailure:
    game_id: int
    error: str


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


def probe_requests(game: CanonicalGame, positions: Sequence[PositionResult]) -> list[tuple[int, tuple[str, ...]]]:
    """Restricted searches this game needs: each underpromotion vs. queening on the same square, plus the
    engine's own best move, so all of them are scored by one search and compare fairly."""
    requests = []
    for ply, uci in enumerate(game.moves_uci, start=1):
        if len(uci) == 5 and uci[4] in "nbr":
            best = positions[ply - 1].best_uci
            requests.append((ply - 1, tuple(dict.fromkeys([uci, uci[:4] + "q", *([best] if best else [])]))))
    return requests


def run_probe(engine: Any, settings: EngineSettings, game: CanonicalGame, position: int, moves: tuple[str, ...]) -> Probe:
    board = start_board(game)
    for uci in game.moves_uci[:position]:
        board.push_uci(uci)
    # A fresh game token clears the hash, so probes never influence (or depend on) the position results.
    infos = engine.analyse(board, settings.limit(), multipv=len(moves), game=object(), info=INFO,
                           root_moves=[board.parse_uci(m) for m in moves])
    results = tuple({"uci": i["pv"][0].uci(), "score_cp": i["score"].white().score(), "mate": i["score"].white().mate()}
                    for i in infos if i.get("pv"))
    return Probe(position, moves, results)


class Analyzer:
    """One engine and everything needed to turn a game into results. Lives in the parent (1 worker)
    or once per pool process. Restarts its engine after a failure."""

    def __init__(self, factory: Callable[[], Any], settings: EngineSettings):
        self.factory, self.settings = factory, settings
        self.engine = factory()

    def __call__(self, game_id: int, game: CanonicalGame) -> GameAnalysis | GameFailure:
        t = time.perf_counter()
        try:
            positions = analyze_game(self.engine, self.settings, game)
            probes = [run_probe(self.engine, self.settings, game, p, m) for p, m in probe_requests(game, positions)]
        except chess.engine.EngineError as e:  # includes the engine process dying
            self.close()
            self.engine = self.factory()
            return GameFailure(game_id, f"{type(e).__name__}: {e}")
        return GameAnalysis(game_id, positions, probes, time.perf_counter() - t)

    def close(self) -> None:
        with contextlib.suppress(Exception):  # after Ctrl-C the engine may already be gone
            self.engine.quit()


_worker: Analyzer | None = None  # one per pool process


def _init_worker(factory: Callable[[], Any], settings: EngineSettings) -> None:
    # The parent owns Ctrl-C (it terminates the pool). SIG_IGN is inherited by Stockfish too; it still exits
    # when its stdin closes, which happens when the pool kills this process.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    global _worker
    _worker = Analyzer(factory, settings)


def _work(task: tuple[int, CanonicalGame]) -> GameAnalysis | GameFailure:
    return _worker(*task)


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


def run(
    conn: psycopg.Connection,
    settings: EngineSettings = EngineSettings(),
    stockfish: str | None = None,
    max_games: int | None = None,
    progress: Callable[[dict], None] | None = None,
    engine_factory: Callable[[], Any] | None = None,
    workers: int = 1,
) -> int:
    """Analyze every game with no results under this config (newest first) on `workers` engines.

    Only this process writes to the database: each finished game is committed with its status row, so
    Ctrl-C or a crash loses at most the games in flight, and re-running resumes. A game the engine
    chokes on is logged, skipped for this run, and retried next run. Returns the run id.
    """
    path = None
    if engine_factory is None:
        path = stockfish_path(stockfish)
        engine_factory = functools.partial(open_stockfish, path, settings)  # picklable, for the pool
    identify = engine_factory()
    try:
        engine_name = identify.id["name"]
    finally:
        with contextlib.suppress(Exception):
            identify.quit()

    config_id = db.ensure_engine_config(conn, engine_name, settings)
    game_ids = db.pending_engine_game_ids(conn, config_id, max_games)  # snapshot: in-flight games can't be re-picked
    run_id = db.start_engine_run(conn, config_id, len(game_ids), workers, path, _sha256(path) if path else None)
    done = positions = 0
    started = time.perf_counter()
    status = "failed"
    try:
        for outcome in _outcomes(conn, game_ids, engine_factory, settings, workers):
            if isinstance(outcome, GameFailure):
                db.record_engine_error(conn, run_id, {"game_id": outcome.game_id, "error": outcome.error})
                continue
            with conn.transaction():
                db.insert_engine_positions(conn, config_id, outcome.game_id, outcome.positions)
                db.insert_engine_probes(conn, config_id, outcome.game_id, outcome.probes)
                db.mark_engine_game_done(conn, config_id, outcome.game_id, run_id, len(outcome.positions))
                db.record_engine_progress(conn, run_id, 1, len(outcome.positions), outcome.seconds)
            done += 1
            positions += len(outcome.positions)
            if progress:
                elapsed = time.perf_counter() - started
                progress({"games_done": done, "games_total": len(game_ids), "positions": positions,
                          "elapsed": elapsed, "positions_per_sec": positions / elapsed if elapsed else 0})
        status = "completed"
    except KeyboardInterrupt:
        status = "cancelled"
        raise
    finally:
        db.finish_engine_run(conn, run_id, status)
    return run_id


def _games(conn: psycopg.Connection, game_ids: list[int]) -> Iterator[tuple[int, CanonicalGame]]:
    for chunk in itertools.batched(game_ids, FETCH_BATCH):
        rows = {row["id"]: row for row in db.games_with_moves(conn, list(chunk))}
        for game_id in chunk:
            if game_id in rows:  # skip games deleted since the snapshot
                yield game_id, db.game_from_row(rows[game_id])


def _outcomes(conn: psycopg.Connection, game_ids: list[int], factory: Callable[[], Any], settings: EngineSettings,
              workers: int) -> Iterator[GameAnalysis | GameFailure]:
    games = _games(conn, game_ids)
    if workers == 1:
        analyzer = Analyzer(factory, settings)
        try:
            for game_id, game in games:
                yield analyzer(game_id, game)
        finally:
            analyzer.close()
        return

    finished: queue.Queue = queue.Queue()  # filled by the pool's result thread; only this thread touches the DB
    with multiprocessing.get_context("spawn").Pool(workers, _init_worker, (factory, settings)) as pool:  # exit = terminate
        in_flight = 0

        def submit() -> None:
            nonlocal in_flight
            if (task := next(games, None)) is not None:
                pool.apply_async(_work, (task,), callback=finished.put, error_callback=finished.put)
                in_flight += 1

        for _ in range(2 * workers):  # keep every worker busy without loading every game into memory
            submit()
        while in_flight:
            outcome = finished.get()
            in_flight -= 1
            if isinstance(outcome, BaseException):
                raise outcome  # a bug in the worker, not an engine hiccup: fail the run loudly
            yield outcome
            submit()


def _sha256(path: str) -> str:
    with open(os.path.realpath(path), "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()
