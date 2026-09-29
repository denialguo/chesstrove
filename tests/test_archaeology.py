"""Engine archaeology on real games with scripted engine results: every evaluation, engine choice and
two-line search below is written by the test, so each definition's edges can be pinned exactly."""

import itertools

import pytest

from chesstrove import archaeology, db
from chesstrove.archaeology import Params
from chesstrove.engine import EngineSettings, PositionResult, Probe
from chesstrove.ingest import import_pgn

ME = "me"
_games = itertools.count()


def game(conn, movetext, *, white=ME, black="them", result="*", fen=None, evals=None, best=None, top_two=None,
         probes=()):
    """Imports one game, then stores scripted analysis for every position (0..N):
    evals   {position: cp or ("mate", n)}, White's POV; default 0 cp (terminal mates: mate 0 automatically)
    best    {position: uci}; default: the move actually played from that position
    top_two {position: [(uci, cp or ("mate", n)), (uci, ...)]}: a two-line search, best line first."""
    setup = f'[SetUp "1"]\n[FEN "{fen}"]\n' if fen else ""
    n = next(_games)  # identical PGNs are deduplicated on import: make each test game its own
    import_pgn(conn, f'[Event "t{n}"]\n[White "{white}"]\n[Black "{black}"]\n[Result "{result}"]\n{setup}\n'
                     f'{movetext} {result}\n', "g.pgn")
    game_id = conn.execute("SELECT max(id) AS id FROM games").fetchone()["id"]
    moves = conn.execute("SELECT ply, uci, is_checkmate FROM moves WHERE game_id = %s ORDER BY ply", (game_id,)).fetchall()
    config_id = db.ensure_engine_config(conn, "FakeFish 1", EngineSettings("nodes", 1000))
    evals, best, top_two = evals or {}, best or {}, top_two or {}
    results = []
    for position in range(len(moves) + 1):
        value = evals.get(position, 0)
        if position == len(moves) and moves and moves[-1]["is_checkmate"]:
            value = ("mate", 0)
        cp, mate = (None, value[1]) if isinstance(value, tuple) else (value, None)
        played = moves[position]["uci"] if position < len(moves) else None
        results.append(PositionResult(position, cp, mate, None, best.get(position, played), None, None, 12, 12, 1000))
    db.insert_engine_positions(conn, config_id, game_id, results)
    for position, lines in top_two.items():
        rows = [{"uci": u, "score_cp": None if isinstance(v, tuple) else v, "mate": v[1] if isinstance(v, tuple) else None,
                 "wdl": None, "depth": 12, "pv": [u]} for u, v in lines]
        db.insert_engine_probes(conn, config_id, game_id, [Probe(position, "top_two", tuple(u for u, _ in lines), tuple(rows),
                                                                 {"depth": 12, "nodes_cap": 40_000_000, "completed_depth": 12})])
    for p in probes:
        db.insert_engine_probes(conn, config_id, game_id, [p])
    db.mark_engine_game_done(conn, config_id, game_id, None, len(results))
    return game_id


def found(conn, type, params=Params(), player=ME):
    return archaeology.discoveries(conn, type, player, limit=50, params=params)["results"]


def keys(results):
    return [(r["game"]["id"], r["ply"]) for r in results]


RUY = "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6"  # plies 1..8; White = me


# --- BIGGEST_THROW -------------------------------------------------------------------------------------

def test_throw_ranks_by_expected_score_drop_and_carries_evidence(conn):
    g = game(conn, RUY, evals={2: 300, 3: -300}, best={2: "d2d4"})  # 2.Nf3 from +3 to -3; engine wanted d4
    [r] = found(conn, "biggest_throw")
    assert (r["game"]["id"], r["ply"], r["move"]["san"]) == (g, 3, "Nf3")
    assert (r["eval_before"], r["eval_after"], r["engine_choice"]["san"]) == ({"cp": 300}, {"cp": -300}, "d4")
    assert r["score"]["name"] == "expected_score_drop" and r["score"]["value"] == pytest.approx(0.5, abs=0.01)
    assert r["engine"]["engine"] == "FakeFish 1" and r["fen_before"] and r["fen_after"]


def test_the_engines_own_choice_is_never_a_throw(conn):
    game(conn, RUY, evals={2: 300, 3: -300})  # Nf3 WAS the engine's choice: the engine re-evaluated
    assert found(conn, "biggest_throw") == []


def test_opponents_moves_are_not_the_players_throws(conn):
    # Stored scores are White's: -300 -> +300 is Black (me) going from +3 to -3 with 2...Nc6.
    game(conn, RUY, evals={3: -300, 4: 300}, best={3: "d7d5"}, white="them", black=ME)
    game(conn, RUY, evals={3: -300, 4: 300}, best={3: "d7d5"})  # the same drop, but White (me) didn't play it
    assert [r["color"] for r in found(conn, "biggest_throw")] == ["b"]


def test_mate_throws_tie_break_on_certainty(conn):
    # Both drops are 1.0 (a forced mate is 1.0 or 0.0); the sharper one ranks first: mate in 1 thrown
    # into being mated in 1 beats mate in 3 thrown into being mated in 5.
    slow = game(conn, RUY, evals={4: ("mate", 3), 5: ("mate", -5)}, best={4: "d2d4"})
    sharp = game(conn, RUY, evals={4: ("mate", 1), 5: ("mate", -1)}, best={4: "d2d4"})
    rows = found(conn, "biggest_throw")
    assert keys(rows) == [(sharp, 5), (slow, 5)]
    assert rows[0]["eval_before"] == {"mate": 1} and rows[0]["eval_after"] == {"mate": -1}


# --- BIGGEST_COMEBACK / LOST_ADVANTAGE -----------------------------------------------------------------

def test_comeback_is_the_worst_trusted_position_in_a_won_game(conn):
    g = game(conn, RUY, result="1-0", evals={4: ("mate", -2), 6: -400}, best={4: "d2d4"})
    game(conn, RUY, result="0-1", evals={4: ("mate", -2)}, best={4: "d2d4"})  # lost: not a comeback
    [r] = found(conn, "biggest_comeback")
    assert (r["game"]["id"], r["ply"], r["eval"], r["expected"]) == (g, 4, {"mate": -2}, 0.0)
    assert r["move"]["san"] == "Nc6" and r["move"]["by"] == "b"  # what led into it


def test_a_position_the_engine_contradicted_one_ply_later_is_not_trusted(conn):
    # Position 4 says mated in 2, but the engine's own choice (the default: the move played) was
    # played next and the evaluation jumped to +3: that -M2 is not a real low point. -4.00 is.
    g = game(conn, RUY, result="1-0", evals={4: ("mate", -2), 5: 300, 6: -400, 7: -400, 8: -400})
    [r] = found(conn, "biggest_comeback")
    assert (r["game"]["id"], r["ply"], r["eval"]) == (g, 6, {"cp": -400})


def test_lost_advantage_is_the_best_position_in_a_lost_game(conn):
    g = game(conn, RUY, result="0-1", evals={4: ("mate", 3)}, best={4: "d2d4"})
    [r] = found(conn, "lost_advantage")
    assert (r["game"]["id"], r["ply"], r["eval"], r["score"]["name"]) == (g, 4, {"mate": 3}, "best_expected_score")


def test_comeback_from_the_black_side_uses_blacks_point_of_view(conn):
    g = game(conn, RUY, white="them", black=ME, result="0-1", evals={5: ("mate", 2)}, best={5: "d7d6"})
    [r] = found(conn, "biggest_comeback")  # White (them) had mate in 2: Black (me) was being mated in 2
    assert (r["game"]["id"], r["ply"], r["eval"], r["color"]) == (g, 5, {"mate": -2}, "b")


# --- ONLY_WINNING_MOVE / ONLY_MOVE_KEEPING_MATE --------------------------------------------------------

def test_only_winning_move_needs_a_winning_best_line_and_a_losing_runner_up(conn):
    g = game(conn, RUY, top_two={2: [("g1f3", 800), ("d2d4", -100)],   # only move: in
                                 4: [("f1b5", 800), ("d2d4", 700)],    # the runner-up also wins: out
                                 6: [("b5a4", 500), ("d2d4", -100)]})  # best line not clearly winning: out
    [r] = found(conn, "only_winning_move")
    assert (r["game"]["id"], r["ply"]) == (g, 3)
    assert r["comparison"]["runner_up"] == {"uci": "d2d4", "san": "d4", "eval": {"cp": -100}, "expected": 0.409}
    assert r["comparison"]["best_line"]["eval"] == {"cp": 800} and r["move_class"]["quiet"]


def test_only_winning_move_thresholds_are_parameters(conn):
    game(conn, RUY, top_two={4: [("f1b5", 800), ("d2d4", 700)]})
    assert found(conn, "only_winning_move") == []
    assert len(found(conn, "only_winning_move", Params(not_winning=0.95))) == 1  # no re-analysis needed


def test_recaptures_and_mates_on_the_board_are_not_only_moves(conn):
    game(conn, "1. e4 d5 2. exd5 Qxd5", white="them", black=ME, top_two={3: [("d8d5", ("mate", -9)), ("g8f6", 600)]})
    game(conn, "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#", result="1-0", top_two={6: [("h5f7", ("mate", 1)), ("d2d3", -50)]})
    assert found(conn, "only_winning_move") == []


def test_only_move_keeping_mate(conn):
    g = game(conn, RUY, top_two={2: [("g1f3", ("mate", 4)), ("d2d4", 200)],       # only mate, and only win: in
                                 4: [("f1b5", ("mate", 4)), ("d2d4", ("mate", 6))],  # another mate exists: out
                                 6: [("b5a4", ("mate", 4)), ("d2d4", 900)]})         # the runner-up wins anyway: out
    [r] = found(conn, "only_move_keeping_mate")
    assert (r["game"]["id"], r["ply"], r["score"]) == (g, 3, {"name": "mate_in", "value": 4})


# --- UNUSUAL MOVES ---------------------------------------------------------------------------------------

def test_unusual_move_is_gap_times_documented_bonuses(conn):
    game(conn, RUY, top_two={6: [("b5a4", 300), ("d2d4", -300)]})  # 4.Ba4: quiet AND a retreat
    [r] = found(conn, "unusual_move")
    assert r["gap"] == pytest.approx(0.5, abs=0.01)
    assert r["move_class"]["quiet"] and r["move_class"]["retreat"]
    assert r["score"]["value"] == pytest.approx(r["gap"] * (1 + 0.5 + 0.5), abs=0.002)
    flat = Params(weight_quiet=0, weight_retreat=0, weight_sacrifice=0, weight_underpromotion=0)
    assert found(conn, "unusual_move", flat)[0]["score"]["value"] == r["gap"]


def test_parrying_a_mate_threat_is_not_unusual(conn):
    game(conn, RUY, top_two={6: [("b5a4", 300), ("d2d4", ("mate", -1))]})  # the alternative walks into mate
    assert found(conn, "unusual_move") == []


def test_equal_or_transposing_alternatives_are_not_unusual(conn):
    game(conn, RUY, top_two={6: [("b5a4", 300), ("b5c6", 300)]})  # two moves, same score: nothing to see
    assert found(conn, "unusual_move") == []


# --- FORCED MATES ------------------------------------------------------------------------------------------

def test_missed_forced_mate_starts_at_mate_in_two(conn):
    g = game(conn, RUY, evals={2: ("mate", 2), 3: 150, 4: ("mate", 1), 5: 100}, best={2: "d1h5", 4: "d1h5"})
    [r] = found(conn, "missed_forced_mate")  # the missed mate in 1 is MISSED_MATE_IN_ONE's, not this
    assert (r["game"]["id"], r["ply"], r["score"]["value"], r["eval_after"]) == (g, 3, 2, {"cp": 150})


def test_keeping_the_mate_by_another_route_is_not_missing_it(conn):
    game(conn, RUY, evals={2: ("mate", 2), 3: ("mate", 3)}, best={2: "d1h5"})
    assert found(conn, "missed_forced_mate") == []


SCHOLAR = "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#"


def test_longest_mate_found_counts_an_unbroken_run_that_ends_in_mate(conn):
    g = game(conn, SCHOLAR, result="1-0", evals={4: ("mate", 2), 5: ("mate", 2), 6: ("mate", 1)})
    [r] = found(conn, "longest_mate_found")
    assert (r["game"]["id"], r["ply"], r["score"]["value"]) == (g, 5, 2)
    assert r["run"] == {"moves": 2, "engine_mate_in_at_start": 2, "mating_ply": 7}


def test_a_move_that_lets_the_mate_go_breaks_the_run(conn):
    # 3.Bc4 is played with mate in 2 on the board but the position after isn't a mate any more: the
    # run is only 4.Qxf7# itself, a mate in 1.
    game(conn, SCHOLAR, result="1-0", evals={4: ("mate", 2), 5: 200, 6: ("mate", 1)})
    assert found(conn, "longest_mate_found") == []


def test_mate_length_is_conservative_on_both_sides(conn):
    # The engine says mate in 7 at the start, but the player needed only 2 moves: mate_length 2.
    game(conn, SCHOLAR, result="1-0", evals={4: ("mate", 7), 5: ("mate", 2), 6: ("mate", 1)})
    [r] = found(conn, "longest_mate_found")
    assert r["score"]["value"] == 2 and r["run"]["engine_mate_in_at_start"] == 7


# --- SOUND MATERIAL SACRIFICE ------------------------------------------------------------------------------

PHILIDOR = "5r1k/6pp/7N/8/8/8/Q5PP/6K1 w - - 0 1"  # 1.Qg8+ Rxg8 2.Nf7#: the queen sacrifice


def test_queen_sacrifice_leading_to_mate(conn):
    g = game(conn, "1. Qg8+ Rxg8 2. Nf7#", fen=PHILIDOR, result="1-0",
             evals={0: ("mate", 2), 1: ("mate", 2), 2: ("mate", 1)})
    [r] = found(conn, "material_sacrifice")
    assert (r["game"]["id"], r["ply"], r["move"]["san"]) == (g, 1, "Qg8+")
    assert r["sacrifice"] == {"kind": "queen", "piece": "queen", "reply": "Rxg8", "material_before": 7,
                              "material_after_window": -2, "deficit": 9, "never_recovered": True, "ends_in_mate": True}


def test_a_sacrifice_that_isnt_the_engines_choice_needs_the_tolerance_mode(conn):
    game(conn, "1. Qg8+ Rxg8 2. Nf7#", fen=PHILIDOR, result="1-0",
         evals={0: ("mate", 2), 1: ("mate", 2), 2: ("mate", 1)}, best={0: "a2a7"})
    assert found(conn, "material_sacrifice") == []
    assert len(found(conn, "material_sacrifice", Params(sacrifice_engine_choice=False))) == 1


def test_a_trade_is_not_a_sacrifice(conn):
    # 1.Re8+ Rxe8 2.Rxe8#: a rook is taken, but taken back at once. Never down material.
    game(conn, "1. Re8+ Rxe8 2. Rxe8#", fen="r5k1/5ppp/8/8/8/8/4RPPP/4R1K1 w - - 0 1", result="1-0",
         evals={0: ("mate", 2), 1: ("mate", 1), 2: ("mate", 1)})
    assert found(conn, "material_sacrifice") == []


def test_a_queen_lost_to_a_fork_while_in_check_is_not_a_sacrifice(conn):
    # 1...Ne2+ forks king and queen; 2.Kb1 is forced and ...Nxc3+ wins the queen. No choice, no sacrifice.
    game(conn, "1... Ne2+ 2. Kb1 Nxc3+", fen="6k1/8/8/8/3n4/2Q5/8/2K5 b - - 0 1", evals={1: 500, 2: 500, 3: 500})
    assert found(conn, "material_sacrifice") == []
    assert found(conn, "material_sacrifice", Params(sacrifice_engine_choice=False, sacrifice_tolerance=1)) == []


def test_a_desperado_in_a_lost_position_is_not_sound(conn):
    game(conn, "1. Qg8+ Rxg8 2. Nf7#", fen=PHILIDOR, result="1-0", evals={0: -500, 1: -500, 2: -500})
    assert found(conn, "material_sacrifice") == []


# --- UNDERPROMOTION -----------------------------------------------------------------------------------------

def test_underpromotion_keeps_best_move_and_queen_comparison_apart(conn):
    fen = "8/P1k5/8/8/8/8/8/4K3 w - - 0 1"
    legal = ["a7a8q", "a7a8r", "a7a8b", "a7a8n", "e1d1", "e1d2", "e1e2", "e1f1", "e1f2"]
    line = lambda u, cp: {"uci": u, "score_cp": cp, "mate": None, "wdl": None, "depth": 12, "pv": [u]}  # noqa: E731
    all_moves = Probe(0, "all_moves", tuple(legal), tuple(line(u, 90 if u == "a7a8n" else 0) for u in legal), {"depth": 12})
    vs_queen = Probe(0, "vs_queen", ("a7a8n", "a7a8q"), (line("a7a8n", 90), line("a7a8q", 0)), {"depth": 12})
    g = game(conn, "1. a8=N+", fen=fen, probes=[all_moves, vs_queen])
    [r] = found(conn, "underpromotion")
    assert (r["game"]["id"], r["best_move"], r["vs_queen"], r["played_move_rank"]) == (g, "unique_best", "better", 1)
    assert r["best_moves"] == [{"uci": "a7a8n", "san": "a8=N+"}]
    assert r["queen_promotion_evaluation"] == {"cp": 0} and r["evaluation"] == {"cp": 90}


def test_unknown_type_is_an_error(conn):
    with pytest.raises(ValueError):
        archaeology.discoveries(conn, "brilliancy", ME)


# --- API --------------------------------------------------------------------------------------------------

def test_api_returns_evidence_and_takes_thresholds(dsn, conn, monkeypatch):
    from fastapi.testclient import TestClient

    from chesstrove import api

    monkeypatch.setenv("CHESSTROVE_DATABASE_URL", dsn)
    g = game(conn, RUY, top_two={4: [("f1b5", 800), ("d2d4", 700)]}, evals={2: 300, 3: -300}, best={2: "d2d4"})
    client = TestClient(api.app)
    body = client.get("/api/engine-discoveries", params={"type": "biggest_throw", "player": ME}).json()
    assert body["config"]["engine"] == "FakeFish 1" and body["params"]["scale"] == "lichess"
    [r] = body["results"]
    assert (r["game"]["id"], r["ply"], r["eval_before"], r["eval_after"]) == (g, 3, {"cp": 300}, {"cp": -300})
    q = {"type": "only_winning_move", "player": ME}
    assert client.get("/api/engine-discoveries", params=q).json()["results"] == []
    assert len(client.get("/api/engine-discoveries", params={**q, "not_winning": 0.95}).json()["results"]) == 1
    assert client.get("/api/engine-discoveries", params={**q, "type": "brilliancy"}).status_code == 422


def test_a_game_that_stops_inside_the_window_proves_no_sacrifice(conn):
    # White's queen is taken and White resigns at once: maybe a sacrifice, maybe a trade never finished.
    game(conn, "1. Qg8+ Rxg8", fen=PHILIDOR, result="0-1", evals={0: ("mate", 2), 1: ("mate", 2), 2: ("mate", 1)})
    assert found(conn, "material_sacrifice") == []


def test_probes_of_proven_mates_target_a_fixed_depth(conn):
    # A proven mate reports a huge depth almost for free; a probe inheriting it would search the
    # runner-up line to the node ceiling every time. Mates get PROBE_MATE_DEPTH, others their own depth.
    from chesstrove import engine

    g = game(conn, RUY, evals={2: ("mate", 3)})
    conn.execute("UPDATE engine_positions SET depth = 245 WHERE game_id = %s AND position = 2", (g,))
    depths = db.position_depths(conn, 1, [(g, 2), (g, 4)], engine.PROBE_MATE_DEPTH)
    assert depths == {(g, 2): engine.PROBE_MATE_DEPTH, (g, 4): 12}
