"""Combining the layers at query time: attach engine facts to deterministic events.

Nothing here is stored. Deterministic metadata and engine analysis stay separate, side by side, and
engine answers always name the config that produced them.
"""

from typing import Any

import chess.engine
import psycopg

from chesstrove import db


def _pov(cp: int | None, mate: int | None, flip: bool) -> chess.engine.Score:
    """Stored White-POV value -> the mover's point of view."""
    if mate is not None:
        if mate == 0:  # stored only for a mated side to move, i.e. after the mover delivered mate
            return chess.engine.MateGiven
        return chess.engine.Mate(-mate if flip else mate)
    return chess.engine.Cp(-cp if flip else cp)


def _show(score: chess.engine.Score | None) -> dict | None:
    if score is None:
        return None
    return {"mate": score.mate()} if score.is_mate() else {"cp": score.score()}


def annotate(conn: psycopg.Connection, events: list[dict], config_id: int | None = None) -> list[dict]:
    """Adds `engine_analysis` to each event: None if its game hasn't been analyzed under the config
    (default: the config covering the most games)."""
    config = db.get_engine_config(conn, config_id) if config_id else db.default_engine_config(conn)
    if config is None or not events:
        return [{**e, "engine_analysis": None} for e in events]
    rows = db.engine_facts_for_moves(conn, config["id"], [(e["game_id"], e["ply"]) for e in events])
    return [{**e, "engine_analysis": _analysis(config, rows.get((e["game_id"], e["ply"])))} for e in events]


def _analysis(config: dict, row: dict | None) -> dict | None:
    if row is None:
        return None
    flip = row["color"] == "b"
    played = row["uci"]
    before = _pov(row["before_cp"], row["before_mate"], flip)
    after = _pov(row["after_cp"], row["after_mate"], flip) if row["after_cp"] is not None or row["after_mate"] is not None else None
    lines = row["multipv"] or []

    out: dict[str, Any] = {
        "config": {"id": config["id"], "engine": config["engine_name"],
                   config["limit_kind"]: config["limit_value"], "multipv": config["multipv"]},
        "eval_before": _show(before),
        "eval_after": _show(after),
        "eval_loss_cp": before.score() - after.score() if after and not before.is_mate() and not after.is_mate() else None,
        "engine_choice": row["best_uci"],
        # The unrestricted search's first choice. NOT a claim that the move is the best move: that needs
        # every legal move scored (the all_moves probe, below).
        "matches_engine_choice": played == row["best_uci"],
        "rank_in_engine_lines": next((i + 1 for i, l in enumerate(lines) if l["uci"] == played), None),
    }
    if row["vs_queen"]:  # B. underpromotion vs. queening on the same square, scored in one search
        scores = {r["uci"]: _pov(r["score_cp"], r["mate"], flip) for r in row["vs_queen"]}
        queen = played[:4] + "q"
        if played in scores and queen in scores:
            out["vs_queen"] = {"evaluation": _show(scores[played]), "queen_promotion_evaluation": _show(scores[queen]),
                               "budget": row["vs_queen_budget"]}
            out["better_than_queen"] = scores[played] > scores[queen]
    if row["all_moves"] is not None:  # A. the played move vs. every legal move, scored in one search
        out.update(_best_move_verdict(row, played, flip))
    return out


def _best_move_verdict(row: dict, played: str, flip: bool) -> dict:
    """is_best_move / tied_for_best_move / unique_best_move, only when every legal move got a score."""
    scores = {r["uci"]: _pov(r["score_cp"], r["mate"], flip) for r in row["all_moves"]}
    complete = set(scores) == set(row["all_moves_list"]) and played in scores
    verdict: dict[str, Any] = {"all_moves": {"legal_moves": len(row["all_moves_list"]), "scored": len(scores),
                                             "budget": row["all_moves_budget"]}}
    if not complete:  # never guess: an unscored legal move might be better
        return {**verdict, "is_best_move": None, "tied_for_best_move": None, "unique_best_move": None,
                "played_move_rank": None}
    top = max(scores.values())
    best = sorted(u for u, sc in scores.items() if sc == top)
    is_best = scores[played] == top
    verdict["all_moves"].update(evaluation=_show(scores[played]), best_moves=best, best_evaluation=_show(top))
    return {**verdict,
            "is_best_move": is_best,
            "tied_for_best_move": is_best and len(best) > 1,
            "unique_best_move": is_best and len(best) == 1,
            "played_move_rank": 1 + sum(1 for sc in scores.values() if sc > scores[played])}
