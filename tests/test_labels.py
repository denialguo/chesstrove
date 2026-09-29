import chess
import chess.engine
import pytest

from chesstrove import db, engine, insights, labels
from chesstrove.engine import EngineSettings
from chesstrove.ingest import import_pgn

MOVES = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6"]
GAME = '[White "alice"]\n[Black "bob"]\n[Result "*"]\n\n1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 *\n'
TINY = EngineSettings(limit_value=1_000)

# Per position (k = after k plies), White's POV: (centipawns, WDL). The two scales agree on this story:
#   position 2 -> 3: White goes from clearly winning to losing (2. Nf3 is a BLUNDER and a MISSED_WIN);
#   position 3 -> 4: Black drops a quarter point (a blunder only at a .25 threshold).
SCRIPT = {
    0: (800, (950, 50, 0)),     # lichess 0.95, stockfish 0.975
    1: (800, (950, 50, 0)),
    2: (700, (900, 100, 0)),    # lichess 0.929, stockfish 0.95
    3: (-300, (100, 300, 600)),  # lichess 0.249, stockfish 0.25
    # 4..6: 0 cp, a dead draw
}
# Two-line searches: (uci, cp, WDL) for line 1 and line 2.
TOP_TWO = {
    0: [("e2e4", 800, (950, 50, 0)), ("d2d4", 0, (200, 600, 200))],      # only e4 wins
    2: [("g1f3", 700, (900, 100, 0)), ("b1c3", 650, (850, 150, 0))],     # two winning moves
}


class ScriptedEngine:
    def __init__(self, moves=MOVES, script=SCRIPT, top_two=TOP_TWO, name="FakeFish 1", choices=None):
        self.id = {"name": name}
        self.moves, self.script, self.top_two = moves, script, top_two
        self.choices = choices or {}  # position -> the engine's choice, where it isn't the move played
        self.top_two_calls = 0

    def analyse(self, board, limit, multipv, game, info, root_moves=None):
        position = len(board.move_stack)
        line = lambda uci, cp, wdl: {"score": chess.engine.PovScore(chess.engine.Cp(cp), chess.WHITE),  # noqa: E731
                                     "pv": [chess.Move.from_uci(uci)], "depth": 12,
                                     "wdl": chess.engine.PovWdl(chess.engine.Wdl(*wdl), chess.WHITE)}
        if multipv == 2 and root_moves is None:
            self.top_two_calls += 1
            return [line(*spec) for spec in self.top_two[position]]
        if root_moves:  # underpromotion probes: every root move scores 0
            return [line(m.uci(), 0, (0, 1000, 0)) for m in root_moves]
        choice = self.choices.get(position) or (
            self.moves[position] if position < len(self.moves) else next(iter(board.legal_moves)).uci())
        return [line(choice, *self.script.get(position, (0, (0, 1000, 0))))]

    def analysis(self, board, limit, multipv, game, info, root_moves=None):
        """Probes stream: one completed iteration at the requested depth."""
        infos = self.analyse(board, limit, multipv, game, info, root_moves)
        return _Search([{**i, "depth": limit.depth, "multipv": k} for k, i in enumerate(infos, 1)])

    def quit(self):
        pass


class _Search(list):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stop(self):
        pass


# The engine wanted something else where the script's mistakes happen: a move that WAS the engine's own
# choice is never a blunder (its "drop" would be the engine seeing further one ply later).
CHOICES = {2: "d2d4", 3: "d7d6"}


@pytest.fixture
def analyzed(conn):
    import_pgn(conn, GAME, "g.pgn")
    engine.run(conn, TINY, engine_factory=lambda: ScriptedEngine(choices=CHOICES))
    return conn


def plies(rows):
    return [(r["type"], r["ply"], r["color"], float(r["expected_before"]), float(r["expected_after"])) for r in rows]


@pytest.mark.parametrize("scale, before, after", [("lichess", 0.929, 0.249), ("stockfish", 0.95, 0.25)])
def test_blunder_and_missed_win_on_both_scales(analyzed, scale, before, after):
    assert plies(labels.query(analyzed, "BLUNDER", scale=scale)) == [("BLUNDER", 3, "w", before, after)]
    assert plies(labels.query(analyzed, "MISSED_WIN", scale=scale)) == [("MISSED_WIN", 3, "w", before, after)]
    [row] = labels.query(analyzed, "BLUNDER", scale=scale)
    assert (row["san"], row["engine_choice"], row["scale"]) == ("Nf3", "d2d4", scale)


def test_the_scales_disagree_where_it_matters(conn):
    # +150 cp: Stockfish's WDL calls it nearly won (0.95); humans convert it far less often (lichess 0.64).
    import_pgn(conn, GAME, "g.pgn")
    script = {0: (150, (900, 100, 0)), 1: (150, (900, 100, 0)), 2: (150, (900, 100, 0)), 3: (0, (0, 1000, 0))}
    engine.run(conn, TINY, engine_factory=lambda: ScriptedEngine(script=script, choices=CHOICES))
    assert [r["ply"] for r in labels.query(conn, "MISSED_WIN", scale="stockfish")] == [3]
    assert labels.query(conn, "MISSED_WIN", scale="lichess") == []


def test_player_filter(analyzed):
    assert len(labels.query(analyzed, "BLUNDER", player="ALICE")) == 1
    assert labels.query(analyzed, "BLUNDER", player="bob") == []


def test_thresholds_relabel_without_the_engine(analyzed):
    # Black's 2...Nc6 goes 0.751 -> 0.5: a 0.25 drop. Not a blunder at .30, a blunder at .25.
    looser = labels.query(analyzed, "BLUNDER", labels.Thresholds(blunder=0.25))
    assert [(r["ply"], r["color"]) for r in looser] == [(3, "w"), (4, "b")]
    assert labels.query(analyzed, "MISSED_WIN", labels.Thresholds(winning=0.99)) == []


def test_only_winning_move_two_stage(analyzed):
    assert labels.query(analyzed, "ONLY_WINNING_MOVE") == []  # nothing until stage 2 has run
    fake = ScriptedEngine(choices=CHOICES)
    result = engine.verify_only_winning_moves(analyzed, engine_factory=lambda: fake)
    # Candidates: the mover was winning (>= .90 on either scale) and played the engine's choice: position 0
    # only (2. Nf3 wasn't the engine's choice: that's what made it a blunder).
    assert (result["candidates"], result["probed"], fake.top_two_calls) == (1, 1, 1)
    for scale, best, runner_up in (("lichess", 0.95, 0.5), ("stockfish", 0.975, 0.5)):
        [row] = labels.query(analyzed, "ONLY_WINNING_MOVE", scale=scale)
        assert (row["ply"], row["san"], row["runner_up"], float(row["best_line"]), float(row["runner_up_line"])) == (
            1, "e4", "d2d4", best, runner_up)
    assert engine.verify_only_winning_moves(analyzed, engine_factory=lambda: ScriptedEngine(choices=CHOICES))["candidates"] == 0  # resumable


def test_recaptures_are_not_only_moves_unless_asked(conn):
    # 1. e4 d5 2. exd5 Qxd5: Black's Qxd5 takes back on d5, where White just captured.
    moves = ["e2e4", "d7d5", "e4d5", "d8d5"]
    import_pgn(conn, '[White "a"]\n[Black "b"]\n[Result "*"]\n\n1. e4 d5 2. exd5 Qxd5 *\n', "g.pgn")
    script = {3: (-800, (0, 50, 950)), 4: (-800, (0, 50, 950))}  # Black clearly winning before and after
    top_two = {3: [("d8d5", -800, (0, 50, 950)), ("g8f6", 0, (200, 600, 200))]}
    make = lambda: ScriptedEngine(moves=moves, script=script, top_two=top_two)  # noqa: E731
    engine.run(conn, TINY, engine_factory=make)
    engine.verify_only_winning_moves(conn, engine_factory=make)
    assert labels.query(conn, "ONLY_WINNING_MOVE") == []
    [row] = labels.query(conn, "ONLY_WINNING_MOVE", include_recaptures=True)
    assert (row["san"], row["is_recapture"], row["is_capture"]) == ("Qxd5", True, True)


def test_stage_two_refuses_a_different_engine(analyzed):
    with pytest.raises(ValueError, match="FakeFish 1"):
        engine.verify_only_winning_moves(analyzed, engine_factory=lambda: ScriptedEngine(name="OtherFish 2"))


def test_no_engine_analysis_means_no_labels(conn):
    import_pgn(conn, GAME, "g.pgn")
    assert labels.query(conn, "BLUNDER") == []


def test_deeper_underpromotion_verification_sits_next_to_the_base_verdict(conn):
    under = '[White "a"]\n[Black "b"]\n[Result "*"]\n[SetUp "1"]\n[FEN "8/P1k5/8/8/8/8/8/4K3 w - - 0 1"]\n\n1. a8=N+ *\n'
    import_pgn(conn, under, "g.pgn")
    engine.run(conn, TINY, engine_factory=lambda: ScriptedEngine(moves=["a7a8n"]))
    deep = EngineSettings(limit_kind="depth", limit_value=18)
    result = engine.verify_underpromotions(conn, deep, engine_factory=lambda: ScriptedEngine(moves=["a7a8n"]))
    assert (result["probes"], result["depth"]) == (2, 18)
    assert engine.verify_underpromotions(conn, deep, engine_factory=ScriptedEngine)["probes"] == 0  # resumable
    [e] = insights.annotate(conn, db.list_events(conn, type="UNDERPROMOTION"))
    a = e["engine_analysis"]
    assert a["config"]["nodes"] == 1_000  # the base verdict stays the full-history config's
    [deep] = a["deeper_verification"]
    assert deep["config"]["depth"] == 18 and deep["all_moves"]["budget"]["completed_depth"] == 18
    assert a["all_moves"]["budget"]["depth"] == 12  # base probes: the depth the normal analysis reached
    assert (deep["tied_for_best_move"], deep["better_than_queen"]) == (True, False)  # every move scored 0
    assert [c["limit_value"] for c in db.status_summary(conn, {})["engine"]] == [1_000]  # probe-only config hidden


def test_api(analyzed, dsn, monkeypatch):
    from fastapi.testclient import TestClient

    from chesstrove import api

    monkeypatch.setenv("CHESSTROVE_DATABASE_URL", dsn)
    client = TestClient(api.app)
    assert [r["ply"] for r in client.get("/api/engine-labels", params={"type": "BLUNDER"}).json()] == [3]
    assert len(client.get("/api/engine-labels", params={"type": "BLUNDER", "blunder": 0.25}).json()) == 2
    assert client.get("/api/engine-labels", params={"type": "BLUNDER", "scale": "stockfish"}).json()[0]["scale"] == "stockfish"
    assert client.get("/api/engine-labels", params={"type": "NOPE"}).status_code == 422


def test_scoped_refresh_only_touches_that_players_probes(analyzed):
    engine.verify_only_winning_moves(analyzed, engine_factory=ScriptedEngine)
    before = analyzed.execute("SELECT count(*) AS n FROM engine_move_probes WHERE kind = 'top_two'").fetchone()["n"]
    assert before > 0
    engine.verify_only_winning_moves(analyzed, player="nobody", refresh=True, engine_factory=ScriptedEngine)
    after = analyzed.execute("SELECT count(*) AS n FROM engine_move_probes WHERE kind = 'top_two'").fetchone()["n"]
    assert after == before  # someone else's refresh deleted nothing
