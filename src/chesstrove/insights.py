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
    line_scores = {l["uci"]: _pov(l["score_cp"], l["mate"], flip) for l in lines}

    is_best = played == row["best_uci"] or (played in line_scores and line_scores[played] == max(line_scores.values()))
    rank = next((i + 1 for i, l in enumerate(lines) if l["uci"] == played), 1 if played == row["best_uci"] else None)
    uniquely_best = None  # only knowable when MultiPV shows the runner-up
    if len(line_scores) >= 2:
        ranked = sorted(line_scores.values(), reverse=True)
        uniquely_best = played in line_scores and line_scores[played] == ranked[0] > ranked[1]

    out: dict[str, Any] = {
        "config": {"id": config["id"], "engine": config["engine_name"],
                   config["limit_kind"]: config["limit_value"], "multipv": config["multipv"]},
        "eval_before": _show(before),
        "eval_after": _show(after),
        "eval_loss_cp": before.score() - after.score() if after and not before.is_mate() and not after.is_mate() else None,
        "best_move": row["best_uci"],
        "is_best_move": is_best,
        "uniquely_best": uniquely_best,
        "played_move_rank": rank,
    }
    if row["probe"]:  # one search scoring the played move, queening, and the engine's pick: a fair comparison
        scores = {r["uci"]: _pov(r["score_cp"], r["mate"], flip) for r in row["probe"]}
        queen = played[:4] + "q"
        out["is_best_move"] = scores[played] >= max(scores.values())
        out["evaluation"] = _show(scores[played])
        if queen in scores:
            out["queen_promotion_evaluation"] = _show(scores[queen])
            out["better_than_queen"] = scores[played] > scores[queen]
    return out
