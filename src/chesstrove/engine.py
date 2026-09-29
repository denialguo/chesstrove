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
import threading
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
PROBE_PV_MAX = 4  # per probe line: enough to see the reply (e.g. the capture of a promoted piece)
PROBE_MAX_DEPTH = 30  # a proven mate reports depth 245; searching every legal move that deep never ends
# A depth limit bounds iterations, not work: one MultiPV iteration can explode (see ARCHITECTURE.md, "The
# depth-22 probe stall"). So probes also carry a node ceiling, deterministic under Threads=1 and a cleared
# hash, and keep the deepest iteration in which every line got an exact score.
PROBE_NODES_PER_LINE = 20_000_000
# Where the position's own analysis ended on a proven mate, its reported depth (up to 245) says nothing
# about effort, so probes there target this depth: deeper than 99% of non-mate positions reach at 25k
# nodes. (Inheriting 30 from the cap sent every two-line probe of a mate to the node ceiling: ~10 s each.)
PROBE_MATE_DEPTH = 20
# A stuck engine, not a slow search: the probe fails (nothing stored) and is retried next run. Never a
# search limit, so results never depend on wall time.
PROBE_WATCHDOG_SECONDS = 900.0
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
    """One search from `position` that scores every move in `moves` (UCI searchmoves + MultiPV = all of
    them), so their scores are directly comparable. Kinds:
      vs_queen   {played underpromotion, queening on the same square}
      all_moves  every legal move: the only basis for claiming a move is the best one
      top_two    the two best lines (MultiPV 2): is there exactly one winning move?
    """

    position: int
    kind: Literal["vs_queen", "all_moves", "top_two"]
    moves: tuple[str, ...]
    results: tuple[dict, ...]  # ranked [{uci, score_cp, mate}], White's POV; one per move when complete
    budget: dict  # {"nodes": total} or {"depth": d}, as actually searched


@dataclass(frozen=True, slots=True)
class GameAnalysis:
    game_id: int
    positions: list[PositionResult]
    probes: list[Probe]
    seconds: float


@dataclass(frozen=True, slots=True)
class ProbeResult:
    game_id: int
    probe: Probe
    seconds: float


@dataclass(frozen=True, slots=True)
class GameFailure:
    game_id: int
    error: str
    task: tuple = ()  # for probes: (position, kind); what to retry


class ProbeError(chess.engine.EngineError):
    """A probe that produced no usable result (watchdog fired, or no iteration completed for every line)."""


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


def probe_requests(game: CanonicalGame) -> list[tuple[int, str, tuple[str, ...] | None]]:
    """Probes this game needs, as (position, kind, moves; None = every legal move). Each underpromotion
    gets both questions, kept separate: was it better than queening, and was it the best move at all."""
    requests = []
    for ply, uci in enumerate(game.moves_uci, start=1):
        if len(uci) == 5 and uci[4] in "nbr":
            requests.append((ply - 1, "vs_queen", (uci, uci[:4] + "q")))
            requests.append((ply - 1, "all_moves", None))
    return requests


def run_probe(engine: Any, settings: EngineSettings, game: CanonicalGame, position: int,
              kind: Literal["vs_queen", "all_moves", "top_two"], moves: tuple[str, ...] | None = None,
              depth: int | None = None, nodes_per_line: int = PROBE_NODES_PER_LINE,
              watchdog: float = PROBE_WATCHDOG_SECONDS) -> Probe:
    """A multi-line search whose lines are compared with each other, so every line must come from the same
    completed iteration: a search cut mid-iteration leaves some lines a depth deeper than others, and their
    scores not comparable (seen on a real game: a 270 cp disagreement). The target is a depth (`depth`: the
    position's own analysis depth for node configs; depth configs use theirs); a node ceiling of
    `nodes_per_line` x lines bounds the work, and the result is the deepest iteration every line finished.
    The budget records both, and `completed_depth` < `depth` says the ceiling cut it short."""
    if settings.limit_kind == "depth":
        depth = settings.limit_value
    if not depth:
        raise ValueError("probes need a depth: pass the position's analysis depth, or use a depth config")
    depth = min(depth, PROBE_MAX_DEPTH)
    board = start_board(game)
    for uci in game.moves_uci[:position]:
        board.push_uci(uci)
    if kind == "top_two":
        root, lines = None, min(2, board.legal_moves.count())
    else:
        root = [board.parse_uci(m) for m in moves] if moves else list(board.legal_moves)
        lines = len(root)
    node_cap = nodes_per_line * lines
    infos, completed = _complete_iteration(engine, board, depth, node_cap, lines, root, watchdog)
    results = tuple({"uci": i["pv"][0].uci(), "score_cp": i["score"].white().score(), "mate": i["score"].white().mate(),
                     "wdl": list(i["wdl"].white()) if "wdl" in i else None, "depth": i.get("depth"),
                     "pv": [m.uci() for m in i["pv"][:PROBE_PV_MAX]]}  # the reply reveals transpositions
                    for i in infos)
    budget = {"depth": depth, "nodes_cap": node_cap, "completed_depth": completed}
    moves_searched = tuple(m.uci() for m in root) if root else tuple(r["uci"] for r in results)
    return Probe(position, kind, moves_searched, results, budget)


def _complete_iteration(engine: Any, board: chess.Board, depth: int, node_cap: int, lines: int,
                        root: list[chess.Move] | None, watchdog: float) -> tuple[list[dict], int]:
    """Streams one search and returns the lines of the deepest iteration in which all `lines` reported an
    exact score (fail-high/low bounds don't count), ranked, with that depth. Stockfish reports every line
    at the end of each iteration, so a search stopped by the node ceiling still leaves one whole iteration.
    The watchdog is only for a stuck engine: if it fires the probe fails, whatever the engine said."""
    by_depth: dict[int, dict[int, dict]] = {}
    stuck = threading.Event()
    # A fresh game token clears the hash, so probes never influence (or depend on) the position results.
    with engine.analysis(board, chess.engine.Limit(depth=depth, nodes=node_cap), multipv=lines, game=object(),
                         info=INFO, root_moves=root) as search:
        timer = threading.Timer(watchdog, lambda: (stuck.set(), search.stop()))
        timer.daemon = True
        timer.start()
        try:
            for info in search:
                if (info.get("pv") and "score" in info and info.get("depth")
                        and not info.get("lowerbound") and not info.get("upperbound")):
                    by_depth.setdefault(info["depth"], {})[info.get("multipv", 1)] = info
        finally:
            timer.cancel()
    if stuck.is_set():
        raise ProbeError(f"watchdog: no answer after {watchdog:.0f}s (engine stuck?); nothing stored")
    complete = [d for d, got in by_depth.items() if len(got) == lines]
    if not complete:
        raise ProbeError(f"no iteration scored all {lines} lines within {node_cap:,} nodes")
    best = max(complete)
    return [by_depth[best][k] for k in sorted(by_depth[best])], best  # MultiPV order = Stockfish's ranking


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
            probes = []
            for p, kind, moves in probe_requests(game):
                try:
                    probes.append(run_probe(self.engine, self.settings, game, p, kind, moves, depth=positions[p].depth))
                except ProbeError:  # the game's evaluations still count; verify-underpromotions retries the probe
                    self.close()
                    self.engine = self.factory()
        except chess.engine.EngineError as e:  # includes the engine process dying
            self.close()
            self.engine = self.factory()
            return GameFailure(game_id, f"{type(e).__name__}: {e}")
        return GameAnalysis(game_id, positions, probes, time.perf_counter() - t)

    def probe(self, game_id: int, game: CanonicalGame, position: int, kind: str,
              moves: tuple[str, ...] | None = None, depth: int | None = None) -> ProbeResult | GameFailure:
        t = time.perf_counter()
        try:
            probe = run_probe(self.engine, self.settings, game, position, kind, moves, depth)
        except chess.engine.EngineError as e:
            self.close()
            self.engine = self.factory()
            return GameFailure(game_id, f"{type(e).__name__}: {e}", (position, kind))
        return ProbeResult(game_id, probe, time.perf_counter() - t)

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


def _work(task: tuple) -> GameAnalysis | ProbeResult | GameFailure:
    return _dispatch(_worker, task)


def _dispatch(analyzer: Analyzer, task: tuple) -> GameAnalysis | ProbeResult | GameFailure:
    """Tasks are ("game", game_id, game) or ("probe", game_id, game, position, kind, moves, depth)."""
    kind, *args = task
    return analyzer(*args) if kind == "game" else analyzer.probe(*args)


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
        tasks = (("game", game_id, game) for game_id, game in _games(conn, game_ids))
        for outcome in _parallel(tasks, engine_factory, settings, workers):
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


def _parallel(tasks: Iterator[tuple], factory: Callable[[], Any], settings: EngineSettings,
              workers: int) -> Iterator[GameAnalysis | ProbeResult | GameFailure]:
    """Run tasks on `workers` engines; outcomes arrive in completion order. The caller does all DB writes."""
    if workers == 1:
        analyzer = Analyzer(factory, settings)
        try:
            for task in tasks:
                yield _dispatch(analyzer, task)
        finally:
            analyzer.close()
        return

    finished: queue.Queue = queue.Queue()  # filled by the pool's result thread; only this thread touches the DB
    with multiprocessing.get_context("spawn").Pool(workers, _init_worker, (factory, settings)) as pool:  # exit = terminate
        in_flight = 0

        def submit() -> None:
            nonlocal in_flight
            if (task := next(tasks, None)) is not None:
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


def verify_only_winning_moves(
    conn: psycopg.Connection,
    config_id: int | None = None,
    winning: float = 0.90,
    player: str | None = None,
    stockfish: str | None = None,
    workers: int = 1,
    progress: Callable[[dict], None] | None = None,
    engine_factory: Callable[[], Any] | None = None,
    refresh: bool = False,
) -> dict:
    """Stage 2 of ONLY_WINNING_MOVE: a two-line search (top_two probe) only where it can matter: the mover
    was clearly winning and played the engine's choice. Uses the config's own settings, and refuses a
    binary that reports a different engine, so results stay attributable. Resumable: finished probes are
    skipped next time."""
    config, settings, engine_factory = _probe_setup(conn, config_id, stockfish, engine_factory)
    if refresh:
        db.delete_probes(conn, config["id"], ("top_two",), player)
    candidates = db.only_winning_move_candidates(conn, config["id"], winning, player)
    return _top_two(conn, config, settings, engine_factory, candidates, workers, progress)


def verify_unusual_moves(
    conn: psycopg.Connection,
    config_id: int | None = None,
    player: str | None = None,
    stockfish: str | None = None,
    workers: int = 1,
    progress: Callable[[dict], None] | None = None,
    engine_factory: Callable[[], Any] | None = None,
) -> dict:
    """The same two-line search for the unusual-move discovery, on a sparse set: the player's moves that
    matched the engine's choice in undecided positions (db.unusual_move_candidates). Resumable."""
    config, settings, engine_factory = _probe_setup(conn, config_id, stockfish, engine_factory)
    candidates = db.unusual_move_candidates(conn, config["id"], player)
    return _top_two(conn, config, settings, engine_factory, candidates, workers, progress)


def _probe_setup(conn, config_id, stockfish, engine_factory):
    """The config's own settings, and an engine factory whose binary reports the config's engine."""
    config = db.get_engine_config(conn, config_id) if config_id else db.default_engine_config(conn)
    if config is None:
        raise ValueError("no engine analysis yet: run `chesstrove engine analyze` first")
    settings = EngineSettings(config["limit_kind"], config["limit_value"], config["multipv"], config["threads"], config["hash_mb"])
    if engine_factory is None:
        engine_factory = functools.partial(open_stockfish, stockfish_path(stockfish), settings)
    identify = engine_factory()
    try:
        if identify.id["name"] != config["engine_name"]:
            raise ValueError(f"config {config['id']} was analyzed with {config['engine_name']!r}, "
                             f"this binary is {identify.id['name']!r}")
    finally:
        with contextlib.suppress(Exception):
            identify.quit()
    return config, settings, engine_factory


def _top_two(conn, config, settings, engine_factory, candidates, workers, progress) -> dict:
    depths = db.position_depths(conn, config["id"], candidates, PROBE_MATE_DEPTH)
    by_game: dict[int, list[int]] = {}
    for game_id, position in candidates:
        by_game.setdefault(game_id, []).append(position)
    tasks = (("probe", game_id, game, position, "top_two", None, depths.get((game_id, position)))
             for game_id, game in _games(conn, list(by_game)) for position in by_game[game_id])
    done = 0
    failures: list[dict] = []  # retried next run: finished probes are already stored
    started = time.perf_counter()
    for outcome in _parallel(tasks, engine_factory, settings, workers):
        if isinstance(outcome, GameFailure):
            failures.append({"game_id": outcome.game_id, "position": outcome.task[0] if outcome.task else None,
                             "kind": outcome.task[1] if outcome.task else None, "error": outcome.error})
            continue
        db.insert_engine_probes(conn, config["id"], outcome.game_id, [outcome.probe])
        done += 1
        if progress:
            elapsed = time.perf_counter() - started
            progress({"done": done, "total": len(candidates), "elapsed": elapsed})
    return {"config_id": config["id"], "candidates": len(candidates), "probed": done, "failed": failures,
            "seconds": round(time.perf_counter() - started, 1)}


def verify_underpromotions(
    conn: psycopg.Connection,
    settings: EngineSettings,
    stockfish: str | None = None,
    workers: int = 1,
    engine_factory: Callable[[], Any] | None = None,
    refresh: bool = False,
) -> dict:
    """Ask both underpromotion questions (vs_queen, all_moves) under `settings`. A depth config (e.g. depth
    22) is stored under its own id and shown as `deeper_verification`; the full-history (node) config's own
    settings recompute its probes at each position's analysis depth. Cheap because underpromotions are rare.
    Resumable; `refresh` redoes existing probes."""
    if engine_factory is None:
        engine_factory = functools.partial(open_stockfish, stockfish_path(stockfish), settings)
    identify = engine_factory()
    try:
        engine_name = identify.id["name"]
    finally:
        with contextlib.suppress(Exception):
            identify.quit()
    config_id = db.ensure_engine_config(conn, engine_name, settings)
    if refresh:
        db.delete_probes(conn, config_id, ("vs_queen", "all_moves"))
    have = db.probe_keys(conn, config_id)
    underpromotions = db.underpromotion_moves(conn)
    depths = ({} if settings.limit_kind == "depth"
              else db.position_depths(conn, config_id, [(g, ply - 1) for g, ply, _ in underpromotions]))
    wanted: dict[int, list[tuple[int, str, tuple[str, ...] | None]]] = {}
    for game_id, ply, uci in underpromotions:
        if settings.limit_kind == "nodes" and (game_id, ply - 1) not in depths:
            continue  # a node config can only probe positions it has analyzed (it needs their depth)
        for kind, moves in (("vs_queen", (uci, uci[:4] + "q")), ("all_moves", None)):
            if (game_id, ply - 1, kind) not in have:
                wanted.setdefault(game_id, []).append((ply - 1, kind, moves))
    tasks = (("probe", game_id, game, position, kind, moves, depths.get((game_id, position)))
             for game_id, game in _games(conn, list(wanted)) for position, kind, moves in wanted[game_id])
    done = 0
    failures: list[dict] = []  # retried next run: finished probes are already stored
    started = time.perf_counter()
    for outcome in _parallel(tasks, engine_factory, settings, workers):
        if isinstance(outcome, GameFailure):
            failures.append({"game_id": outcome.game_id, "position": outcome.task[0] if outcome.task else None,
                             "kind": outcome.task[1] if outcome.task else None, "error": outcome.error})
            continue
        db.insert_engine_probes(conn, config_id, outcome.game_id, [outcome.probe])
        done += 1
    return {"config_id": config_id, "engine": engine_name, settings.limit_kind: settings.limit_value,
            "probes": done, "failed": failures, "seconds": round(time.perf_counter() - started, 1)}


def _sha256(path: str) -> str:
    with open(os.path.realpath(path), "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()
