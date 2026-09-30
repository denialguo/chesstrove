"""Combining the layers at query time: attach engine facts to deterministic events.

Nothing here is stored. Deterministic metadata and engine analysis stay separate, side by side, and
engine answers always name the config that produced them.
"""

import math
from typing import Any

import chess
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


# "Best" means best by a margin a player would recognise: moves within this much expected score (lichess
# scale) of each other tie. Without it, a knight promotion at +5.63 "beat" queening at +5.51 (noise), and a
# mate in 4 beat a mate in 5 (a node-limited search's mate distance is only an upper bound anyway).
BEST_MARGIN = 0.10


def _expected(score: chess.engine.Score) -> float:
    """The mover's expected score (db._expected, lichess scale): a forced mate is 1 or 0 whatever its length."""
    if score.is_mate():
        return 1.0 if score.mate() >= 0 else 0.0  # MateGiven is mate 0: the mover delivered it
    return 1 / (1 + math.exp(-db.LICHESS_K * score.score()))


def _show(score: chess.engine.Score | None) -> dict | None:
    if score is None:
        return None
    return {"mate": score.mate()} if score.is_mate() else {"cp": score.score()}


def annotate(conn: psycopg.Connection, events: list[dict], config_id: int | None = None) -> list[dict]:
    """Adds `engine_analysis` to each event: None if its game hasn't been analyzed under the config
    (default: the config covering the most games). Probes stored under other (e.g. deeper) configs are
    attached separately as `deeper_verification`, each labelled with its own config."""
    config = db.get_engine_config(conn, config_id) if config_id else db.default_engine_config(conn)
    if config is None or not events:
        return [{**e, "engine_analysis": None} for e in events]
    keys = [(e["game_id"], e["ply"]) for e in events]
    rows = db.engine_facts_for_moves(conn, config["id"], keys)
    others = db.other_config_probes(conn, config["id"], [(g, p - 1) for g, p in keys])
    out = []
    for e in events:
        analysis = _analysis(config, rows.get((e["game_id"], e["ply"])))
        if analysis is not None:
            deeper = _deeper(others.get((e["game_id"], e["ply"] - 1), []), rows[(e["game_id"], e["ply"])])
            if deeper:
                analysis["deeper_verification"] = deeper
        out.append({**e, "engine_analysis": analysis})
    return out


def _config_label(config: dict) -> dict:
    return {"id": config["id"], "engine": config["engine_name"], config["limit_kind"]: config["limit_value"],
            "multipv": config["multipv"]}


def _analysis(config: dict, row: dict | None) -> dict | None:
    if row is None:
        return None
    flip = row["color"] == "b"
    played = row["uci"]
    before = _pov(row["before_cp"], row["before_mate"], flip)
    after = _pov(row["after_cp"], row["after_mate"], flip) if row["after_cp"] is not None or row["after_mate"] is not None else None
    lines = row["multipv"] or []

    out: dict[str, Any] = {
        "config": _config_label(config),
        "eval_before": _show(before),
        "eval_after": _show(after),
        "eval_loss_cp": before.score() - after.score() if after and not before.is_mate() and not after.is_mate() else None,
        "engine_choice": row["best_uci"],
        # The unrestricted search's first choice. NOT a claim that the move is the best move: that needs
        # every legal move scored (the all_moves probe, below).
        "matches_engine_choice": played == row["best_uci"],
        "rank_in_engine_lines": next((i + 1 for i, l in enumerate(lines) if l["uci"] == played), None),
    }
    out.update(_verdicts(played, flip, _board_before(row), row["vs_queen"], row["vs_queen_budget"],
                         row["all_moves"], row["all_moves_list"], row["all_moves_budget"]))
    return out


def _board_before(row: dict) -> chess.Board:
    return chess.Board(row["fen_before"] or chess.STARTING_FEN, chess960=row["chess960"])


def _transposition_keys(board: chess.Board, lines: list[dict]) -> dict[str, str]:
    """uci -> a key; moves with the same key are one choice, so any score gap between them is search noise.

    - Promotions on the same square whose best reply captures the new piece there (with any piece): whatever
      takes on that square, the resulting position is identical for =Q, =R, =B or =N, so the opponent faces
      the same set of outcomes and the values are equal. (E.g. exf1=R+ Kxf1 vs exf1=Q+ Rxf1.)
    - Otherwise: the exact position after the move and the engine's best reply (a real transposition).
    - Moves without a reply in their line (mate, or a line cut short) are only equal to themselves.
    """
    keys = {}
    for line in lines:
        pv = line.get("pv") or []
        keys[line["uci"]] = line["uci"]
        if len(pv) >= 2 and len(pv[0]) == 5 and pv[1][2:4] == pv[0][2:4]:
            keys[line["uci"]] = f"promotion {pv[0][:4]} captured"
            continue
        if len(pv) >= 2:
            b = board.copy(stack=False)
            try:
                b.push_uci(pv[0])
                b.push_uci(pv[1])
            except ValueError:
                continue
            keys[line["uci"]] = b.epd()  # position without move counters
    return keys


def _class_scores(scores: dict[str, chess.engine.Score], keys: dict[str, str]) -> dict[str, chess.engine.Score]:
    """Every move scores as the best member of its transposition class."""
    best: dict[str, chess.engine.Score] = {}
    for uci, score in scores.items():
        best[keys[uci]] = max(best.get(keys[uci], score), score)
    return {uci: best[keys[uci]] for uci in scores}


def _deeper(probes: list[dict], row: dict) -> list[dict]:
    """The same two questions answered under other configs (strongest first)."""
    by_config: dict[int, dict] = {}
    for p in probes:
        by_config.setdefault(p["config"]["id"], {"config": p["config"]})[p["kind"]] = p
    blocks = []
    for entry in by_config.values():
        q, a = entry.get("vs_queen"), entry.get("all_moves")
        verdict = _verdicts(row["uci"], row["color"] == "b", _board_before(row),
                            q and q["results"], q and q["budget"], a and a["results"], a and a["moves"], a and a["budget"])
        if verdict:
            blocks.append({"config": _config_label(entry["config"]), **verdict})
    return blocks


def _verdicts(played: str, flip: bool, board: chess.Board, vs_queen: list | None, vs_queen_budget: dict | None,
              all_moves: list | None, all_moves_list: list | None, all_moves_budget: dict | None) -> dict:
    out: dict[str, Any] = {}
    if vs_queen:  # B. underpromotion vs. queening on the same square, scored in one search
        scores = {r["uci"]: _pov(r["score_cp"], r["mate"], flip) for r in vs_queen}
        keys = _transposition_keys(board, vs_queen)
        queen = played[:4] + "q"
        if played in scores and queen in scores:
            transposes = keys[played] == keys[queen]
            gap = _expected(scores[played]) - _expected(scores[queen])
            # identical positions after the reply are equal, whatever noise the two scores carry; otherwise
            # a difference only counts past BEST_MARGIN
            verdict = "equal" if transposes or abs(gap) < BEST_MARGIN else "better" if gap > 0 else "worse"
            out["vs_queen"] = {"evaluation": _show(scores[played]), "queen_promotion_evaluation": _show(scores[queen]),
                               "transposes_with_queen": transposes, "verdict": verdict, "budget": vs_queen_budget}
            out["better_than_queen"] = verdict == "better"
    if all_moves is not None:  # A. the played move vs. every legal move, scored in one search
        out.update(_best_move_verdict(played, flip, board, all_moves, all_moves_list, all_moves_budget))
    return out


def _best_move_verdict(played: str, flip: bool, board: chess.Board, results: list, legal: list, budget: dict) -> dict:
    """is_best_move / tied_for_best_move / unique_best_move, only when every legal move got a score.
    Moves that transpose into the same position are one choice, so they tie rather than rank, and moves
    within BEST_MARGIN expected score of the top one tie with it (two forced mates always do).
    played_move_rank is the engine's raw order."""
    raw = {r["uci"]: _pov(r["score_cp"], r["mate"], flip) for r in results}
    complete = set(raw) == set(legal) and played in raw
    verdict: dict[str, Any] = {"all_moves": {"legal_moves": len(legal), "scored": len(raw), "budget": budget}}
    if not complete:  # never guess: an unscored legal move might be better
        return {**verdict, "is_best_move": None, "tied_for_best_move": None, "unique_best_move": None,
                "played_move_rank": None}
    keys = _transposition_keys(board, results)
    scores = _class_scores(raw, keys)
    top = max(scores.values())
    exp = {u: _expected(sc) for u, sc in scores.items()}
    best = sorted(u for u in scores if exp[u] >= max(exp.values()) - BEST_MARGIN)
    is_best = played in best
    verdict["all_moves"].update(evaluation=_show(raw[played]), best_moves=best, best_evaluation=_show(top),
                                transposes_with=sorted(u for u in raw if u != played and keys[u] == keys[played]))
    return {**verdict,
            "is_best_move": is_best,
            "tied_for_best_move": is_best and len(best) > 1,
            "unique_best_move": is_best and len(best) == 1,
            "played_move_rank": 1 + sum(1 for sc in scores.values() if sc > scores[played])}


def best_underpromotions(conn: psycopg.Connection, platform: str, username: str) -> dict | None:
    """The player-page row 'Best-move underpromotion': underpromotions (by the player and against them)
    that the native index judged the single best move among all legal moves (the all_moves probe). None
    when the index hasn't judged any of this player's underpromotions: the browser check takes over."""
    events = [*[{**e, "mine": True} for e in db.list_events(conn, "UNDERPROMOTION", player=username, platform=platform, limit=10_000)],
              *[{**e, "mine": False} for e in db.list_events(conn, "UNDERPROMOTION", against=username, platform=platform, limit=10_000)]]
    judged = [e for e in annotate(conn, events) if (e["engine_analysis"] or {}).get("unique_best_move") is not None]
    if not judged:
        return None
    best = [e for e in judged if e["engine_analysis"]["unique_best_move"]]
    return {"total": len(events), "judged": len(judged), "mine": sum(e["mine"] for e in best),
            "against": sum(not e["mine"] for e in best), "found": [f"{e['game_id']}:{e['ply']}" for e in best]}
