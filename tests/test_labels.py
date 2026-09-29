import chess
import chess.engine
import pytest

from chesstrove import db, engine, labels
from chesstrove.engine import EngineSettings
from chesstrove.ingest import import_pgn

MOVES = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6"]
GAME = '[White "alice"]\n[Black "bob"]\n[Result "*"]\n\n1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 *\n'
TINY = EngineSettings(limit_value=1_000)

# White-POV WDL per position (position k = after k plies). Expected score for White = (W + D/2) / 1000.
WDL = {
    0: (950, 50, 0),    # White 0.975: clearly winning
    1: (950, 50, 0),
    2: (900, 100, 0),   # White 0.95 before 2. Nf3 ...
    3: (100, 300, 600),  # ... 0.25 after it: a 0.70 drop -> BLUNDER and MISSED_WIN
    # 4..6 default to a dead draw (0.5)
}
# Two-line searches: (uci, White-POV WDL) for line 1 and line 2.
TOP_TWO = {
    0: [("e2e4", (950, 50, 0)), ("d2d4", (200, 600, 200))],   # only e4 wins -> ONLY_WINNING_MOVE
    2: [("g1f3", (900, 100, 0)), ("b1c3", (850, 150, 0))],    # two winning moves -> not "only"
}


class ScriptedEngine:
    id = {"name": "FakeFish 1"}

    def __init__(self, name: str = "FakeFish 1"):
        self.id = {"name": name}
        self.top_two_calls = 0

    def analyse(self, board, limit, multipv, game, info, root_moves=None):
        position = len(board.move_stack)
        pov = lambda wdl: chess.engine.PovWdl(chess.engine.Wdl(*wdl), chess.WHITE)  # noqa: E731
        if multipv == 2 and root_moves is None:
            self.top_two_calls += 1
            return [{"score": chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE), "pv": [chess.Move.from_uci(u)],
                     "wdl": pov(w)} for u, w in TOP_TWO[position]]
        choice = chess.Move.from_uci(MOVES[position]) if position < len(MOVES) else next(iter(board.legal_moves))
        return [{"score": chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE), "pv": [choice],
                 "wdl": pov(WDL.get(position, (0, 1000, 0)))}]

    def quit(self):
        pass


@pytest.fixture
def analyzed(conn):
    import_pgn(conn, GAME, "g.pgn")
    engine.run(conn, TINY, engine_factory=ScriptedEngine)
    return conn


def plies(rows):
    return [(r["type"], r["ply"], r["color"], float(r["expected_before"]), float(r["expected_after"])) for r in rows]


def test_blunder_and_missed_win(analyzed):
    assert plies(labels.query(analyzed, "BLUNDER")) == [("BLUNDER", 3, "w", 0.95, 0.25)]
    assert plies(labels.query(analyzed, "MISSED_WIN")) == [("MISSED_WIN", 3, "w", 0.95, 0.25)]
    [row] = labels.query(analyzed, "BLUNDER")
    assert (row["san"], row["engine_choice"], float(row["expected_drop"])) == ("Nf3", "g1f3", 0.7)


def test_player_filter(analyzed):
    assert len(labels.query(analyzed, "BLUNDER", player="ALICE")) == 1
    assert labels.query(analyzed, "BLUNDER", player="bob") == []


def test_thresholds_relabel_without_the_engine(analyzed):
    # Black's 2...Nc6 goes 0.75 -> 0.50: a 0.25 drop. Not a blunder at .30, a blunder at .25.
    looser = labels.query(analyzed, "BLUNDER", labels.Thresholds(blunder=0.25))
    assert [(r["ply"], r["color"]) for r in looser] == [(3, "w"), (4, "b")]
    assert labels.query(analyzed, "MISSED_WIN", labels.Thresholds(winning=0.99)) == []


def test_only_winning_move_two_stage(analyzed):
    assert labels.query(analyzed, "ONLY_WINNING_MOVE") == []  # nothing until stage 2 has run
    fake = ScriptedEngine()
    result = engine.verify_only_winning_moves(analyzed, engine_factory=lambda: fake)
    # Candidates: the mover was winning (>= .90) and played the engine's choice -> positions 0 and 2 only.
    assert (result["candidates"], result["probed"], fake.top_two_calls) == (2, 2, 2)
    [row] = labels.query(analyzed, "ONLY_WINNING_MOVE")
    assert (row["ply"], row["san"], row["runner_up"], float(row["best_line"]), float(row["runner_up_line"])) == (
        1, "e4", "d2d4", 0.975, 0.5)
    again = engine.verify_only_winning_moves(analyzed, engine_factory=ScriptedEngine)
    assert again["candidates"] == 0  # resumable: done positions are skipped


def test_stage_two_refuses_a_different_engine(analyzed):
    with pytest.raises(ValueError, match="FakeFish 1"):
        engine.verify_only_winning_moves(analyzed, engine_factory=lambda: ScriptedEngine("OtherFish 2"))


def test_no_engine_analysis_means_no_labels(conn):
    import_pgn(conn, GAME, "g.pgn")
    assert labels.query(conn, "BLUNDER") == []


def test_api(analyzed, dsn, monkeypatch):
    from fastapi.testclient import TestClient

    from chesstrove import api

    monkeypatch.setenv("CHESSTROVE_DATABASE_URL", dsn)
    client = TestClient(api.app)
    assert [r["ply"] for r in client.get("/engine-labels", params={"type": "BLUNDER"}).json()] == [3]
    assert len(client.get("/engine-labels", params={"type": "BLUNDER", "blunder": 0.25}).json()) == 2
    assert client.get("/engine-labels", params={"type": "NOPE"}).status_code == 422
