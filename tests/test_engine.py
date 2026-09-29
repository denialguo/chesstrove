import shutil

import chess
import chess.engine
import pytest

from chesstrove import db, engine
from chesstrove.engine import EngineSettings, analyze_game
from chesstrove.importers.pgn import read_pgn
from chesstrove.ingest import import_pgn

SCHOLARS = '[White "a"]\n[Black "b"]\n[Result "1-0"]\n[UTCDate "2024.01.02"]\n[UTCTime "10:00:00"]\n\n' \
           '1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0\n'
SHORT = '[White "c"]\n[Black "d"]\n[Result "*"]\n[UTCDate "2024.01.01"]\n[UTCTime "10:00:00"]\n\n1. d4 d5 *\n'
STALEMATE = '[SetUp "1"]\n[FEN "k7/8/1Q6/8/8/8/8/K7 w - - 0 1"]\n[Result "1/2-1/2"]\n\n1. Kb2 1/2-1/2\n'  # Kb2: black is stalemated
TINY = EngineSettings(limit_value=2_000)
needs_stockfish = pytest.mark.skipif(shutil.which("stockfish") is None, reason="Stockfish not installed")


class FakeEngine:
    """Records what it was asked. Answers +0.10 for its first legal move; in restricted searches, scores
    root moves in the order given (+0.30, +0.20, ...), so tests control the ranking."""

    id = {"name": "FakeFish 1"}

    def __init__(self, fail_on_plies: int | None = None):
        self.calls: list[tuple[int, str, object]] = []
        self.probes: list[tuple[int, list[str]]] = []
        self.fail_on_plies = fail_on_plies
        self.quit_called = False

    def analyse(self, board, limit, multipv, game, info, root_moves=None):
        if self.fail_on_plies is not None and len(board.move_stack) == self.fail_on_plies:
            raise chess.engine.EngineTerminatedError("engine died")
        if root_moves:
            self.probes.append((len(board.move_stack), [m.uci() for m in root_moves]))
            return [{"score": chess.engine.PovScore(chess.engine.Cp(30 - 10 * i), chess.WHITE), "pv": [m]}
                    for i, m in enumerate(root_moves)]
        self.calls.append((len(board.move_stack), board.root().fen(), game))
        move = next(iter(board.legal_moves))
        return [{"score": chess.engine.PovScore(chess.engine.Cp(10), chess.WHITE), "pv": [move],
                 "depth": 5, "seldepth": 7, "nodes": limit.nodes,
                 "wdl": chess.engine.PovWdl(chess.engine.Wdl(100, 850, 50), chess.WHITE)}]

    def quit(self):
        self.quit_called = True


def one(pgn: str):
    [game] = read_pgn(pgn)
    return game


# --- analyze_game (fake engine: our side of the contract) ------------------------------------------

def test_every_position_with_history_and_one_game_token():
    fake = FakeEngine()
    results = analyze_game(fake, TINY, one(SCHOLARS))
    assert [r.position for r in results] == list(range(8))  # 7 plies -> positions 0..7
    assert [c[0] for c in fake.calls] == list(range(7))  # position 7 is mate: not searched
    assert {c[1] for c in fake.calls} == {chess.STARTING_FEN}  # sent as start + moves, never a bare FEN
    assert len({id(c[2]) for c in fake.calls}) == 1  # one game token per game -> one ucinewgame
    assert (results[0].score_cp, results[0].wdl, results[0].nodes) == (10, (100, 850, 50), 2_000)


def test_terminal_positions_are_scored_by_the_rules():
    mate = analyze_game(FakeEngine(), TINY, one(SCHOLARS))[-1]
    assert (mate.score_cp, mate.mate, mate.best_uci) == (None, 0, None)
    stale = analyze_game(FakeEngine(), TINY, one(STALEMATE))[-1]
    assert (stale.score_cp, stale.mate, stale.best_uci) == (0, None, None)


def test_a_new_hash_for_every_game():
    fake = FakeEngine()
    analyze_game(fake, TINY, one(SHORT))
    analyze_game(fake, TINY, one(SHORT))
    assert len({id(c[2]) for c in fake.calls}) == 2


# --- run: persistence, resume, failure, cancellation (fake engine) ---------------------------------

def run_fake(conn, fake=None, **kwargs):
    fake = fake or FakeEngine()
    return engine.run(conn, TINY, engine_factory=lambda: fake, **kwargs), fake


def test_run_persists_every_position_newest_game_first(conn):
    import_pgn(conn, SCHOLARS + "\n" + SHORT, "g.pgn")
    run_id, fake = run_fake(conn)
    [run] = db.list_engine_runs(conn)
    assert (run["id"], run["status"], run["games_done"], run["positions_done"], run["engine_name"]) == (
        run_id, "completed", 2, 8 + 3, "FakeFish 1")
    assert fake.quit_called
    assert fake.calls[0][0] == 0 and len(fake.calls) == 7 + 3  # scholars (newer) first: 7 searches (mate not searched), then 3
    rows = conn.execute("SELECT position, score_cp, mate, best_uci, pv_uci, wdl FROM engine_positions "
                        "ORDER BY game_id, position").fetchall()
    assert len(rows) == 11 and rows[0]["wdl"] == [100, 850, 50] and rows[0]["pv_uci"] == [rows[0]["best_uci"]]


def test_resume_and_no_repeat_work(conn):
    import_pgn(conn, SCHOLARS + "\n" + SHORT, "g.pgn")
    run_fake(conn, max_games=1)
    assert db.status_summary(conn, {})["engine"][0]["games_done"] == 1
    _, fake = run_fake(conn)
    assert len(fake.calls) == 3  # only the remaining game (3 positions)
    _, fake = run_fake(conn)
    assert fake.calls == []  # nothing left under this config


def test_different_settings_are_a_different_config(conn):
    import_pgn(conn, SHORT, "g.pgn")
    run_fake(conn)
    engine.run(conn, EngineSettings(limit_value=3_000), engine_factory=FakeEngine)
    configs = conn.execute("SELECT limit_value FROM engine_configs ORDER BY id").fetchall()
    assert [c["limit_value"] for c in configs] == [2_000, 3_000]
    assert conn.execute("SELECT count(*) AS n FROM engine_game_status").fetchone()["n"] == 2


def test_engine_crash_skips_the_game_and_restarts(conn):
    import_pgn(conn, SCHOLARS + "\n" + SHORT, "g.pgn")
    engines = [FakeEngine(), FakeEngine(fail_on_plies=3), FakeEngine()]  # identify; dies in scholar's; restart
    run_id = engine.run(conn, TINY, engine_factory=lambda: engines.pop(0))
    [run] = db.list_engine_runs(conn)
    assert (run["status"], run["games_done"], run["games_failed"]) == ("completed", 1, 1)
    assert "engine died" in run["errors"][0]["error"]
    assert conn.execute("SELECT count(*) AS n FROM engine_positions").fetchone()["n"] == 3  # no partial game
    assert run_id and engine.run(conn, TINY, engine_factory=FakeEngine) and \
        db.status_summary(conn, {})["engine"][0]["games_done"] == 2  # retried and finished next run


def test_ctrl_c_keeps_finished_games(conn):
    import_pgn(conn, SCHOLARS + "\n" + SHORT, "g.pgn")

    def interrupt_on_second_game(p):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_fake(conn, progress=interrupt_on_second_game)
    [run] = db.list_engine_runs(conn)
    assert (run["status"], run["games_done"]) == ("cancelled", 1)
    assert db.status_summary(conn, {})["engine"][0]["games_done"] == 1


def test_status_summary(conn):
    import_pgn(conn, SCHOLARS + "\n" + SHORT, "g.pgn")
    run_fake(conn, max_games=1)
    s = db.status_summary(conn, {"UNDERPROMOTION": 2})
    assert (s["games"], s["positions"], s["deterministic_done"]) == (2, 11, 2)
    assert (s["engine"][0]["games_done"], s["engine"][0]["positions_done"]) == (1, 8)


# --- real Stockfish ---------------------------------------------------------------------------------

@needs_stockfish
def test_stockfish_finds_the_mate_and_is_reproducible():
    game = one(SCHOLARS)
    sf = engine.open_stockfish(engine.stockfish_path(), TINY)
    try:
        first = analyze_game(sf, TINY, game)
        second = analyze_game(sf, TINY, game)
    finally:
        sf.quit()
    assert first == second  # same config, same game -> identical results
    before_mate = first[6]  # White to play Qxf7#
    assert (before_mate.mate, before_mate.best_uci) == (1, "h5f7")
    assert first[-1].mate == 0


@needs_stockfish
def test_scores_are_from_whites_point_of_view():
    # Black to move, but White is a queen up: the score must still be positive.
    sf = engine.open_stockfish(engine.stockfish_path(), TINY)
    try:
        [r] = analyze_game(sf, TINY, one('[SetUp "1"]\n[FEN "4k3/8/8/8/8/8/8/3QK3 b - - 0 1"]\n[Result "*"]\n\n*\n'))
    finally:
        sf.quit()  # an engine left running keeps its thread (and pytest) alive
    assert (r.score_cp or 0) > 500 or (r.mate or 0) > 0


@needs_stockfish
def test_multipv_lines(conn):
    import_pgn(conn, SHORT, "g.pgn")
    engine.run(conn, EngineSettings(limit_value=2_000, multipv=3))
    row = conn.execute("SELECT multipv FROM engine_positions WHERE position = 0").fetchone()
    assert len(row["multipv"]) == 3 and all({"uci", "score_cp", "mate"} <= line.keys() for line in row["multipv"])


# --- worker pool ----------------------------------------------------------------------------------

def stored_positions(conn):
    return [tuple(r.values()) for r in conn.execute(
        "SELECT g.source_key, position, score_cp, mate, best_uci, pv_uci, wdl FROM engine_positions e "
        "JOIN games g ON g.id = e.game_id ORDER BY g.source_key, position").fetchall()]


def test_pool_gives_the_same_results_as_one_worker(conn):
    import_pgn(conn, SCHOLARS + "\n" + SHORT + "\n" + STALEMATE, "g.pgn")
    engine.run(conn, TINY, engine_factory=FakeEngine, workers=1)
    single = stored_positions(conn)
    conn.execute("TRUNCATE engine_configs CASCADE")
    run_id = engine.run(conn, TINY, engine_factory=FakeEngine, workers=2)
    assert stored_positions(conn) == single
    [run] = db.list_engine_runs(conn)
    assert (run["id"], run["workers"], run["status"], run["games_done"]) == (run_id, 2, "completed", 3)


def test_pool_survives_an_engine_crash(conn):
    import functools

    import_pgn(conn, SCHOLARS + "\n" + SHORT, "g.pgn")
    engine.run(conn, TINY, engine_factory=functools.partial(FakeEngine, fail_on_plies=3), workers=2)
    [run] = db.list_engine_runs(conn)
    assert (run["status"], run["games_done"], run["games_failed"]) == ("completed", 1, 1)


def test_pool_ctrl_c_keeps_finished_games(conn):
    import_pgn(conn, SCHOLARS + "\n" + SHORT + "\n" + STALEMATE, "g.pgn")

    def interrupt(p):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        engine.run(conn, TINY, engine_factory=FakeEngine, workers=2, progress=interrupt)
    [run] = db.list_engine_runs(conn)
    assert (run["status"], run["games_done"]) == ("cancelled", 1)
    engine.run(conn, TINY, engine_factory=FakeEngine, workers=2)  # resumes the other two
    assert db.status_summary(conn, {})["engine"][0]["games_done"] == 3


# --- underpromotion probes + insights ---------------------------------------------------------------

WHITE_UNDER = '[White "alice"]\n[Black "bob"]\n[Result "*"]\n[SetUp "1"]\n[FEN "8/P1k5/8/8/8/8/8/4K3 w - - 0 1"]\n\n1. a8=N+ *\n'
BLACK_UNDER = '[White "bob"]\n[Black "alice"]\n[Result "*"]\n[SetUp "1"]\n[FEN "4k3/8/8/8/8/8/p7/7K b - - 0 1"]\n\n1... a1=B *\n'


def test_probe_scores_underpromotion_queen_and_best_in_one_search(conn):
    from chesstrove import insights

    import_pgn(conn, WHITE_UNDER + "\n" + BLACK_UNDER, "g.pgn")
    engine.run(conn, TINY, engine_factory=FakeEngine)
    probes = conn.execute("SELECT position, moves FROM engine_move_probes ORDER BY game_id").fetchall()
    assert [p["moves"][:2] for p in probes] == [["a7a8n", "a7a8q"], ["a2a1b", "a2a1q"]]  # played first, then queen

    events = insights.annotate(conn, db.list_events(conn, type="UNDERPROMOTION"))
    by_color = {e["color"]: e["engine_analysis"] for e in events}
    # The fake scores root moves +30, +20, ... from White's POV, in the order given (played, queen, best).
    white, black = by_color["w"], by_color["b"]
    assert (white["is_best_move"], white["better_than_queen"], white["evaluation"], white["queen_promotion_evaluation"]) == (
        True, True, {"cp": 30}, {"cp": 20})
    # For Black the same numbers flip: the played move is now the worst of the three.
    assert (black["is_best_move"], black["better_than_queen"], black["evaluation"]) == (False, False, {"cp": -30})
    assert white["config"]["engine"] == "FakeFish 1" and white["best_move"] is not None


def test_unanalyzed_events_say_so(conn):
    from chesstrove import insights

    import_pgn(conn, WHITE_UNDER, "g.pgn")
    assert [e["engine_analysis"] for e in insights.annotate(conn, db.list_events(conn))] == [None]


@needs_stockfish
def test_stockfish_knight_fork_underpromotion_beats_queening(conn):
    from chesstrove import insights

    fork = '[White "a"]\n[Black "b"]\n[Result "*"]\n[SetUp "1"]\n[FEN "8/2q1P1k1/8/8/8/8/7P/6K1 w - - 0 1"]\n\n1. e8=N+ *\n'
    import_pgn(conn, fork, "g.pgn")
    engine.run(conn, EngineSettings(limit_value=20_000))
    [e] = insights.annotate(conn, db.list_events(conn, type="UNDERPROMOTION"))
    a = e["engine_analysis"]
    assert a["is_best_move"] and a["better_than_queen"] and a["evaluation"]["cp"] > 200
    assert abs(a["queen_promotion_evaluation"]["cp"]) < 100  # queening lets Black hold
