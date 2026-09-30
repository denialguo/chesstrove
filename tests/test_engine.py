import shutil
import time

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


ROOT_SCORES: dict[str, int | tuple[str, int]] = {}  # uci -> White-POV centipawns, or ("mate", n); tests set it


def _root_score(uci: str) -> chess.engine.Score:
    v = ROOT_SCORES.get(uci, 0)
    return chess.engine.Mate(v[1]) if isinstance(v, tuple) else chess.engine.Cp(v)
ROOT_REPLIES: dict[str, str] = {}  # uci -> the reply in that move's line (reveals transpositions)


class FakeEngine:
    """Records what it was asked. Answers +0.10 for its first legal move. In restricted searches every
    root move gets a line, scored from ROOT_SCORES (default 0), so tests control rankings and ties."""

    id = {"name": "FakeFish 1"}

    def __init__(self, fail_on_plies: int | None = None, drop_probe_lines: int = 0):
        self.calls: list[tuple[int, str, object]] = []
        self.probes: list[tuple[int, list[str], object]] = []
        self.fail_on_plies = fail_on_plies
        self.drop_probe_lines = drop_probe_lines  # simulate a search that didn't score every root move
        self.quit_called = False

    def analyse(self, board, limit, multipv, game, info, root_moves=None):
        if self.fail_on_plies is not None and len(board.move_stack) == self.fail_on_plies:
            raise chess.engine.EngineTerminatedError("engine died")
        if root_moves:
            assert multipv == len(root_moves)  # every root move must get its own line
            self.probes.append((len(board.move_stack), [m.uci() for m in root_moves], limit))
            lines = sorted(root_moves, key=lambda m: _root_score(m.uci()), reverse=True)
            lines = lines[: len(lines) - self.drop_probe_lines]
            return [{"score": chess.engine.PovScore(_root_score(m.uci()), chess.WHITE),
                     "pv": [m] + ([chess.Move.from_uci(ROOT_REPLIES[m.uci()])] if m.uci() in ROOT_REPLIES else [])}
                    for m in lines]
        self.calls.append((len(board.move_stack), board.root().fen(), game))
        move = next(iter(board.legal_moves))
        return [{"score": chess.engine.PovScore(chess.engine.Cp(10), chess.WHITE), "pv": [move],
                 "depth": 5, "seldepth": 7, "nodes": limit.nodes,
                 "wdl": chess.engine.PovWdl(chess.engine.Wdl(100, 850, 50), chess.WHITE)}]

    def analysis(self, board, limit, multipv, game, info, root_moves=None):
        """Streams what analyse() would answer as one completed iteration at the requested depth."""
        infos = self.analyse(board, limit, multipv, game, info, root_moves)
        return FakeSearch([{**i, "depth": limit.depth, "multipv": k} for k, i in enumerate(infos, 1)])

    def quit(self):
        self.quit_called = True


class FakeSearch:
    def __init__(self, infos):
        self.infos, self.stopped = infos, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self.infos)

    def stop(self):
        self.stopped = True


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


def underpromotion_analysis(conn, monkeypatch, scores, pgn=WHITE_UNDER + "\n" + BLACK_UNDER, replies=None, **fake):
    from chesstrove import insights

    monkeypatch.setattr(__import__(__name__), "ROOT_SCORES", scores)
    monkeypatch.setattr(__import__(__name__), "ROOT_REPLIES", replies or {})
    import_pgn(conn, pgn, "g.pgn")
    engine.run(conn, TINY, engine_factory=lambda: FakeEngine(**fake))
    return {e["color"]: e["engine_analysis"] for e in insights.annotate(conn, db.list_events(conn, type="UNDERPROMOTION"))}


def test_probes_cover_the_queen_question_and_every_legal_move(conn, monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(__import__(__name__), "ROOT_SCORES", {})
    import_pgn(conn, WHITE_UNDER, "g.pgn")
    engine.run(conn, TINY, engine_factory=lambda: fake)
    probes = conn.execute("SELECT kind, moves, budget FROM engine_move_probes ORDER BY kind").fetchall()
    legal = {m.uci() for m in chess.Board("8/P1k5/8/8/8/8/8/4K3 w - - 0 1").legal_moves}
    assert [p["kind"] for p in probes] == ["all_moves", "vs_queen"]
    assert set(probes[0]["moves"]) == legal and len(legal) == 9  # 4 promotions + 5 king moves
    assert probes[1]["moves"] == ["a7a8n", "a7a8q"]
    # depth-limited at the depth the normal analysis reached (the fake reports 5), so every line finishes
    # the same iteration and the scores compare
    cap = engine.PROBE_NODES_PER_LINE
    assert (probes[0]["budget"], probes[1]["budget"]) == ({"depth": 5, "nodes_cap": 9 * cap, "completed_depth": 5},
                                                          {"depth": 5, "nodes_cap": 2 * cap, "completed_depth": 5})
    # the depth AND the node ceiling reach the engine
    assert sorted((p[2].depth, p[2].nodes) for p in fake.probes) == [(5, 2 * cap), (5, 9 * cap)]


def test_unique_best_and_better_than_queen(conn, monkeypatch):
    a = underpromotion_analysis(conn, monkeypatch, {"a7a8n": 300, "a7a8q": 20})["w"]
    assert (a["is_best_move"], a["unique_best_move"], a["tied_for_best_move"], a["played_move_rank"]) == (True, True, False, 1)
    assert (a["better_than_queen"], a["vs_queen"]["evaluation"], a["vs_queen"]["queen_promotion_evaluation"]) == (
        True, {"cp": 300}, {"cp": 20})
    assert a["all_moves"]["best_moves"] == ["a7a8n"] and a["all_moves"]["scored"] == a["all_moves"]["legal_moves"] == 9


def test_a_better_move_outside_the_queen_comparison_means_not_best(conn, monkeypatch):
    # Beats queening, but a quiet king move (never in the vs_queen search) scores higher: not the best move.
    a = underpromotion_analysis(conn, monkeypatch, {"a7a8n": 300, "a7a8q": 20, "e1d2": 600})["w"]
    assert (a["better_than_queen"], a["is_best_move"], a["unique_best_move"], a["played_move_rank"]) == (True, False, False, 2)
    assert a["all_moves"]["best_moves"] == ["e1d2"]


def test_best_needs_a_real_margin(conn, monkeypatch):
    # 12 centipawns at +5.5 is noise, and two forced mates are both a win whatever their length: ties.
    a = underpromotion_analysis(conn, monkeypatch, {"a7a8n": 563, "a7a8q": 551})["w"]
    assert (a["is_best_move"], a["tied_for_best_move"], a["unique_best_move"], a["vs_queen"]["verdict"]) == (True, True, False, "equal")
    m = underpromotion_analysis(conn, monkeypatch, {"a7a8n": ("mate", 4), "a7a8q": ("mate", 5)})["w"]
    assert (m["unique_best_move"], m["tied_for_best_move"], m["better_than_queen"]) == (False, True, False)


def test_tied_for_best(conn, monkeypatch):
    a = underpromotion_analysis(conn, monkeypatch, {"a7a8n": 50, "a7a8q": 50})["w"]
    assert (a["is_best_move"], a["tied_for_best_move"], a["unique_best_move"], a["better_than_queen"]) == (
        True, True, False, False)  # equal to queening is not *better* than queening


def test_black_scores_are_flipped(conn, monkeypatch):
    # White-POV +50 for Black's bishop promotion is the worst outcome for Black.
    b = underpromotion_analysis(conn, monkeypatch, {"a2a1b": 300, "a2a1q": -20})["b"]
    assert (b["is_best_move"], b["better_than_queen"], b["vs_queen"]["evaluation"]) == (False, False, {"cp": -300})


def test_incomplete_all_moves_search_claims_nothing(conn, monkeypatch):
    # A search that never scores every legal move fails the probe: nothing is stored, nothing is claimed,
    # and the game's own evaluations are kept.
    a = underpromotion_analysis(conn, monkeypatch, {"a7a8n": 50}, drop_probe_lines=1)["w"]
    assert "is_best_move" not in a and "all_moves" not in a
    assert conn.execute("SELECT count(*) AS n FROM engine_move_probes WHERE kind = 'all_moves'").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM engine_game_status").fetchone()["n"] == 2


def test_ordinary_moves_never_claim_best(conn):
    from chesstrove import insights

    import_pgn(conn, SCHOLARS, "g.pgn")
    engine.run(conn, TINY, engine_factory=FakeEngine)
    game_id = conn.execute("SELECT id FROM games").fetchone()["id"]
    [e] = insights.annotate(conn, [{"game_id": game_id, "ply": 7, "type": "ANY"}])  # 4. Qxf7#
    a = e["engine_analysis"]
    assert "is_best_move" not in a and "unique_best_move" not in a  # only the all-moves probe may claim these
    assert (a["engine_choice"], a["matches_engine_choice"], a["eval_after"]) == ("h5h7", False, {"mate": 0})


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
    assert (a["is_best_move"], a["unique_best_move"], a["better_than_queen"]) == (True, True, True)
    assert a["vs_queen"]["evaluation"]["cp"] > 200
    assert abs(a["vs_queen"]["queen_promotion_evaluation"]["cp"]) < 100  # queening lets Black hold


@needs_stockfish
def test_stockfish_saavedra_rook_underpromotion_is_best_and_beats_queening(conn):
    # A cheap unrestricted search prefers Kd3 here; scoring every legal move finds g8=R mates in 2, where
    # g8=Q stalemates. Without Black's rook, quiet king moves still force mate too, so the rook is tied for
    # best rather than the only winning move: two forced mates are both a win.
    from chesstrove import insights

    saavedra = '[White "a"]\n[Black "b"]\n[Result "*"]\n[SetUp "1"]\n[FEN "8/6P1/8/8/8/8/2K5/k7 w - - 0 1"]\n\n1. g8=R *\n'
    import_pgn(conn, saavedra, "g.pgn")
    engine.run(conn, EngineSettings(limit_value=20_000))
    [e] = insights.annotate(conn, db.list_events(conn, type="UNDERPROMOTION"))
    a = e["engine_analysis"]
    assert (a["is_best_move"], a["better_than_queen"]) == (True, True)
    assert a["all_moves"]["evaluation"] == {"mate": 2} and a["vs_queen"]["queen_promotion_evaluation"] == {"cp": 0}


# b8=R+ and b8=Q+ both get taken by ...Rxb8: the same position either way.
DOOMED_PROMOTION = '[White "alice"]\n[Black "bob"]\n[Result "*"]\n[SetUp "1"]\n[FEN "r3k3/1P6/8/8/8/8/8/4K3 w - - 0 1"]\n\n1. b8=R+ *\n'


def test_promotions_that_transpose_are_equal_whatever_the_noise(conn, monkeypatch):
    # The rook line scores 0.30 higher, but after ...Rxb8 both lines reach the identical position.
    a = underpromotion_analysis(conn, monkeypatch, {"b7b8r": 300, "b7b8q": 20}, pgn=DOOMED_PROMOTION,
                                replies={"b7b8r": "a8b8", "b7b8q": "a8b8"})["w"]
    assert (a["better_than_queen"], a["vs_queen"]["transposes_with_queen"]) == (False, True)
    assert (a["is_best_move"], a["tied_for_best_move"], a["unique_best_move"]) == (True, True, False)
    assert a["all_moves"]["transposes_with"] == ["b7b8q"] and a["all_moves"]["best_moves"] == ["b7b8q", "b7b8r"]


# King c7 and rook a8 both guard b8: the new piece can be taken two different ways.
TWO_CAPTURERS = '[White "alice"]\n[Black "bob"]\n[Result "*"]\n[SetUp "1"]\n[FEN "r7/1Pk5/8/8/8/8/8/4K3 w - - 0 1"]\n\n1. b8=R *\n'


def test_promoted_piece_taken_by_different_pieces_is_still_equal(conn, monkeypatch):
    # =R is met by ...Rxb8 and =Q+ by ...Kxb8: different positions, but either capture is available against
    # either promotion, and each gives the same position whatever piece stood on b8. Equal.
    a = underpromotion_analysis(conn, monkeypatch, {"b7b8r": 50, "b7b8q": 20}, pgn=TWO_CAPTURERS,
                                replies={"b7b8r": "a8b8", "b7b8q": "c7b8"})["w"]
    assert (a["vs_queen"]["transposes_with_queen"], a["better_than_queen"], a["unique_best_move"]) == (True, False, False)


def test_promotion_not_captured_in_one_line_is_not_equal(conn, monkeypatch):
    a = underpromotion_analysis(conn, monkeypatch, {"b7b8r": 300, "b7b8q": 20}, pgn=TWO_CAPTURERS,
                                replies={"b7b8r": "a8b8", "b7b8q": "c7d7"})["w"]
    assert (a["vs_queen"]["transposes_with_queen"], a["better_than_queen"]) == (False, True)


def test_different_replies_do_not_transpose(conn, monkeypatch):
    a = underpromotion_analysis(conn, monkeypatch, {"b7b8r": 300, "b7b8q": 20}, pgn=DOOMED_PROMOTION,
                                replies={"b7b8r": "a8b8", "b7b8q": "e8e7"})["w"]
    assert (a["better_than_queen"], a["vs_queen"]["transposes_with_queen"], a["unique_best_move"]) == (True, False, True)


@needs_stockfish
def test_stockfish_doomed_promotion_transposes_with_queening(conn):
    from chesstrove import insights

    import_pgn(conn, DOOMED_PROMOTION, "g.pgn")
    engine.run(conn, EngineSettings(limit_value=20_000))
    [e] = insights.annotate(conn, db.list_events(conn, type="UNDERPROMOTION"))
    a = e["engine_analysis"]
    assert a["vs_queen"]["transposes_with_queen"] and a["better_than_queen"] is False
    assert "b7b8q" in a["all_moves"]["transposes_with"]


def test_probe_depth_is_capped():
    fake = FakeEngine()
    [game] = read_pgn(WHITE_UNDER)
    probe = engine.run_probe(fake, TINY, game, 0, "vs_queen", ("a7a8n", "a7a8q"), depth=245)  # a proven mate's depth
    assert probe.budget["depth"] == engine.PROBE_MAX_DEPTH and fake.probes[0][2].depth == engine.PROBE_MAX_DEPTH


# --- the depth-22 probe stall (ARCHITECTURE.md): bounded, whole-iteration probes ---------------------

STALL_FEN = "8/7P/8/4k1p1/8/2r1P1P1/5PK1/8 w - - 0 53"  # game 3658, before h8=B+: 14 legal moves


def _info(uci, cp, depth, multipv, bound=None):
    i = {"score": chess.engine.PovScore(chess.engine.Cp(cp), chess.WHITE), "pv": [chess.Move.from_uci(uci)],
         "depth": depth, "multipv": multipv}
    return {**i, bound: True} if bound else i


class ScriptedEngine:
    """Streams a fixed list of infos, like a search stopped by its node ceiling mid-iteration."""

    def __init__(self, infos, block=False):
        self.infos, self.block, self.limits = infos, block, []

    def analysis(self, board, limit, multipv, game, info, root_moves=None):
        self.limits.append(limit)
        engine = self

        class Search(FakeSearch):
            def __iter__(self):
                yield from self.infos
                while engine.block and not self.stopped:  # a stuck engine: nothing until stop()
                    time.sleep(0.01)

        return Search(self.infos)


def test_a_search_cut_mid_iteration_keeps_the_last_whole_iteration():
    [game] = read_pgn(WHITE_UNDER)
    infos = [_info("a7a8n", 40, 7, 1), _info("a7a8q", 20, 7, 2),  # iteration 7: both lines
             _info("a7a8q", 90, 8, 1, "lowerbound"),                # iteration 8: a fail-high doesn't count
             _info("a7a8q", 60, 8, 1)]                              # ...and only one line finished
    fake = ScriptedEngine(infos)
    probe = engine.run_probe(fake, TINY, game, 0, "vs_queen", ("a7a8n", "a7a8q"), depth=9, nodes_per_line=1000)
    assert [(r["uci"], r["score_cp"], r["depth"]) for r in probe.results] == [("a7a8n", 40, 7), ("a7a8q", 20, 7)]
    assert probe.budget == {"depth": 9, "nodes_cap": 2000, "completed_depth": 7}
    assert (fake.limits[0].depth, fake.limits[0].nodes) == (9, 2000)


def test_no_whole_iteration_is_a_failure_not_a_guess():
    [game] = read_pgn(WHITE_UNDER)
    with pytest.raises(engine.ProbeError):
        engine.run_probe(ScriptedEngine([_info("a7a8n", 40, 7, 1)]), TINY, game, 0, "vs_queen", ("a7a8n", "a7a8q"), depth=9)


def test_watchdog_fails_a_stuck_engine_and_stores_nothing():
    [game] = read_pgn(WHITE_UNDER)
    infos = [_info("a7a8n", 40, 7, 1), _info("a7a8q", 20, 7, 2)]  # a whole iteration, then silence
    with pytest.raises(engine.ProbeError, match="watchdog"):
        engine.run_probe(ScriptedEngine(infos, block=True), TINY, game, 0, "vs_queen", ("a7a8n", "a7a8q"),
                         depth=9, watchdog=0.2)


@needs_stockfish
def test_stockfish_stall_position_is_bounded_and_reproducible():
    # The real position that hung a depth-22 all_moves probe for hours: every move alone takes ~1s at
    # depth 22, but the 14-line search explodes once the top line becomes a mate. A (small, for the
    # test) node ceiling must end it deterministically, at a whole iteration, every line scored.
    [game] = read_pgn(f'[SetUp "1"]\n[FEN "{STALL_FEN}"]\n[Result "*"]\n\n53. h8=B+ *\n')
    settings = EngineSettings("depth", 22)
    runs = []
    for _ in range(2):
        sf = engine.open_stockfish(engine.stockfish_path(), settings)
        try:
            runs.append(engine.run_probe(sf, settings, game, 0, "all_moves", nodes_per_line=200_000))
        finally:
            sf.quit()
    first, second = runs
    assert len(first.results) == 14 and first.budget["depth"] == 22
    assert first.budget["nodes_cap"] == 14 * 200_000 and first.budget["completed_depth"] < 22
    assert {r["depth"] for r in first.results} == {first.budget["completed_depth"]}  # one iteration, all lines
    assert first.results == second.results and first.budget == second.budget  # node ceilings reproduce
