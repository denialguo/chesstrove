"""Engine archaeology: discoveries across a player's whole history, derived at query time from stored
engine results (engine_positions, engine_move_probes) plus games and moves. Nothing here is stored and
nothing re-runs Stockfish; thresholds are parameters. See ARCHITECTURE.md, "Engine archaeology".

Every result carries the same evidence: the game (with how it ended), the ply, the move, the positions
before and after, the engine's evaluations from the player's side (mate distances kept), the engine's
choice, the engine config, and the quantity it was ranked by. Engine facts (evaluations, best lines) and
ChessTrove's definitions (what counts as a sacrifice, a comeback, an unusual move) are kept apart: each
type documents which of its conditions are which.

Evaluations are shown from the mover's (or, for game-level types, the player's) point of view:
{"cp": 250} or {"mate": 3} (mates in 3), {"mate": -2} (is mated in 2), {"mate": 0} (mate delivered).
"""

from dataclasses import asdict, dataclass
from typing import Any, Literal

import chess
import psycopg

from chesstrove import db, insights

Type = Literal["biggest_throw", "biggest_comeback", "lost_advantage", "only_winning_move", "underpromotion",
               "material_sacrifice", "missed_forced_mate", "longest_mate_found", "only_move_keeping_mate",
               "unusual_move"]
TYPES: tuple[Type, ...] = Type.__args__  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class Params:
    """Every threshold a discovery uses. None of them are stored with results: change one and re-query."""

    scale: Literal["lichess", "stockfish"] = "lichess"  # expected-score scale, see labels.py
    winning: float = 0.90  # "clearly winning" (only_winning_move)
    not_winning: float = 0.60  # the runner-up no longer wins (only_winning_move)
    sacrifice_engine_choice: bool = True  # sound = the engine's own first choice ("and still the best move")
    sacrifice_tolerance: float = 0.05  # if not: a sound sacrifice loses at most this much expected score
    sacrifice_floor: float = 0.50  # ...and leaves the sacrificer at least this (no desperados)
    unusual_min_gap: float = 0.30  # every alternative at least this much worse (unusual_move)
    # unusual_move ranking: gap x (1 + sum of weight x feature); see `unusualness`
    weight_quiet: float = 0.5
    weight_retreat: float = 0.5
    weight_sacrifice: float = 1.0
    weight_underpromotion: float = 1.0


def discoveries(conn: psycopg.Connection, type: Type, player: str, platform: str | None = None,
                config_id: int | None = None, limit: int = 20, params: Params = Params()) -> dict:
    """The ranked discoveries of one type for one player (username, on `platform` if given)."""
    if type not in TYPES:
        raise ValueError(f"unknown discovery type {type!r}; known: {', '.join(TYPES)}")
    config = db.get_engine_config(conn, config_id) if config_id else db.default_engine_config(conn)
    if config is None:
        return {"type": type, "config": None, "params": asdict(params), "results": []}
    q = {"config": config["id"], "player": player, "platform": platform, "limit": limit}
    results = _TYPES[type](conn, q, params, config)
    return {"type": type, "config": _config(config), "params": asdict(params), "results": results}


# --- evidence ------------------------------------------------------------------------------------------

def _config(config: dict) -> dict:
    return {"id": config["id"], "engine": config["engine_name"], config["limit_kind"]: config["limit_value"],
            "multipv": config["multipv"], "threads": config["threads"], "hash_mb": config["hash_mb"]}


def _eval(cp: int | None, mate: int | None, pov: str) -> dict | None:
    """Stored White-POV value -> {"cp"} / {"mate"} from `pov`'s side. mate 0 stays 0 (mate on the board)."""
    if cp is None and mate is None:
        return None
    if mate is not None:
        return {"mate": 0 if mate == 0 else (mate if pov == "w" else -mate)}
    return {"cp": cp if pov == "w" else -cp}


def _san(fen: str, uci: str | None) -> str | None:
    if not uci:
        return None
    try:
        board = chess.Board(fen)
        return board.san(chess.Move.from_uci(uci))
    except (ValueError, AssertionError):
        return None


def _game(r: dict) -> dict:
    return {"id": r["game_id"], "played_at": r["played_at"], "white": r["white"], "black": r["black"],
            "result": r["result"], "termination": r["termination"], "platform": r["platform"],
            "source_key": r["source_key"], "external_id": r["external_id"]}


def _move_evidence(r: dict, config: dict, score_name: str, score: Any) -> dict:
    return {
        "game": _game(r),
        "ply": r["ply"], "color": r["color"], "player": r["mover"], "opponent": r["opponent"],
        "move": {"san": r["san"], "uci": r["uci"]},
        "fen_before": r["fen_before"], "fen_after": r["fen_after"],
        "eval_before": _eval(r["cp_before"], r["mate_before"], r["color"]),
        "eval_after": _eval(r["cp_after"], r["mate_after"], r["color"]),
        "expected_before": round(float(r["exp_before"]), 3), "expected_after": round(float(r["exp_after"]), 3),
        "engine_choice": {"uci": r["engine_choice"], "san": _san(r["fen_before"], r["engine_choice"])},
        "move_class": _move_class(r),
        "engine": _config(config),
        "score": {"name": score_name, "value": score},
    }


def _move_class(r: dict) -> dict:
    return {"capture": r["captured"] is not None, "check": r["is_check"], "promotion": r["promotion"],
            "castling": r["is_castling"], "recapture": r["is_recapture"],
            "quiet": r["captured"] is None and not r["is_check"] and r["promotion"] is None,
            "retreat": _retreat(r["uci"], r["color"]), "sacrifice": _sacrifice(r)}


def _retreat(uci: str, color: str) -> bool:
    """The piece moved back toward its own side (a deterministic mark of a non-obvious move)."""
    rank_from, rank_to = int(uci[1]), int(uci[3])
    return rank_to < rank_from if color == "w" else rank_to > rank_from


def _moves(conn: psycopg.Connection, q: dict, p: Params, where: str, order: str, extra_join: str = "",
           extra_cols: str = "", limit: bool = True) -> list[dict]:
    sql = (f"SELECT mv.* {extra_cols} FROM ({db.archaeology_moves_sql(p.scale)}) mv {extra_join} "
           f"WHERE lower(mv.mover) = lower(%(player)s) AND {where} ORDER BY {order}"
           + (" LIMIT %(limit)s" if limit else ""))
    return conn.execute(sql, {**q, **asdict(p)}).fetchall()


STABLE = "mv.played_at DESC NULLS LAST, mv.game_id, mv.ply"  # final tie-break: newest first, then position


# --- 3. BIGGEST_THROW ----------------------------------------------------------------------------------

def biggest_throw(conn, q, p, config):
    """The largest drops in the player's expected score caused by one of their own moves: ranked by
    expected_before - expected_after (the BLUNDER and MISSED_WIN labels are thresholds over the same
    quantity). Ties (every forced mate is 1.0 or 0.0) break by how certain the win was (mate in 2 before
    beats +9) and how certain the loss after.
    ChessTrove rule: a move that WAS the engine's own choice is never a throw. Its "drop" is the engine
    seeing further one ply later (18 of 9,480 BLUNDER rows on the real corpus were this artifact)."""
    rows = _moves(conn, q, p, "mv.uci IS DISTINCT FROM mv.engine_choice AND mv.exp_before > mv.exp_after",
                  f"mv.exp_before - mv.exp_after DESC, mv.ord_before DESC, mv.ord_after, {STABLE}")
    return [_move_evidence(r, config, "expected_score_drop", round(float(r["exp_before"] - r["exp_after"]), 3))
            for r in rows]


# --- 4. BIGGEST_COMEBACK / LOST_ADVANTAGE (game level) -----------------------------------------------

def biggest_comeback(conn, q, p, config):
    """Games the player won, ranked by the worst position they reached (lowest expected score from the
    player's side; ties by ordinal: being mated in 1 is worse than in 5, is worse than -9).
    ChessTrove rule: only trusted positions count (the engine didn't contradict itself one ply later).
    Each result says how the game ended, because 'won on time from mate-in-2 against' is a different
    story from a mate, and whether the player later had a forced mate of their own."""
    return _game_extreme(conn, q, p, config, won=True)


def lost_advantage(conn, q, p, config):
    """The inverse: games the player lost, ranked by the best position they reached."""
    return _game_extreme(conn, q, p, config, won=False)


def _game_extreme(conn, q, p, config, won: bool):
    outcome = ("(g.result = '1-0' AND pos.pov = 'w') OR (g.result = '0-1' AND pos.pov = 'b')" if won else
               "(g.result = '0-1' AND pos.pov = 'w') OR (g.result = '1-0' AND pos.pov = 'b')")
    direction = "ASC" if won else "DESC"
    rows = conn.execute(
        f"""WITH pos AS ({db.archaeology_positions_sql(p.scale)}),
            pick AS (
              SELECT DISTINCT ON (pos.game_id) pos.*
              FROM pos JOIN games g ON g.id = pos.game_id
              WHERE pos.trusted AND ({outcome})
              ORDER BY pos.game_id, pos.exp {direction}, pos.ord {direction}, pos.position
            )
            SELECT pick.*, g.played_at, g.white, g.black, g.result, g.source_key, g.external_id, g.ply_count,
                   split_part(g.source_key, ':', 1) AS platform,
                   substring(g.pgn from '\\[Termination "([^"]*)"\\]') AS termination,
                   m.san AS into_san, m.uci AS into_uci, m.color AS into_color,
                   coalesce(m.fen_after, g.initial_fen, '{db.START_FEN}') AS fen,
                   (SELECT min(later.position) FROM pos later WHERE later.game_id = pick.game_id
                      AND later.position > pick.position AND later.trusted AND later.ord > 90000) AS later_forced_mate_at
            FROM pick JOIN games g ON g.id = pick.game_id
            LEFT JOIN moves m ON m.game_id = pick.game_id AND m.ply = pick.position
            -- among equally extreme positions, games decided on the board (mate, resignation) before
            -- time and abandonment: surviving mate-in-1 on the clock is a smaller story
            ORDER BY pick.exp {direction}, pick.ord {direction},
                     (substring(g.pgn from '\\[Termination "([^"]*)"\\]') ~* 'time|abandon') ,
                     g.played_at DESC NULLS LAST, pick.game_id
            LIMIT %(limit)s""",
        {**q, **asdict(p)},
    ).fetchall()
    out = []
    for r in rows:
        player = r["white"] if r["pov"] == "w" else r["black"]
        out.append({
            "game": _game(r),
            "ply": r["position"], "color": r["pov"], "player": player,
            "opponent": r["black"] if r["pov"] == "w" else r["white"],
            # the move that led into the position (by either side), and the position itself
            "move": {"san": r["into_san"], "uci": r["into_uci"], "by": r["into_color"]} if r["into_san"] else None,
            "fen": r["fen"],
            "eval": _eval(r["score_cp"], r["mate"], r["pov"]),
            "expected": round(float(r["exp"]), 3),
            "engine_choice": {"uci": r["best_uci"], "san": _san(r["fen"], r["best_uci"])},
            "later_forced_mate_at": r["later_forced_mate_at"],
            "engine": _config(config),
            "score": {"name": "worst_expected_score" if won else "best_expected_score", "value": round(float(r["exp"]), 3)},
        })
    return out


# --- 2. ONLY_WINNING_MOVE ------------------------------------------------------------------------------

def _top_two_join(p: Params) -> tuple[str, str]:
    first, second = db._line(p.scale, "tt.results->0", "mv.color"), db._line(p.scale, "tt.results->1", "mv.color")
    join = """JOIN engine_move_probes tt ON tt.config_id = %(config)s AND tt.game_id = mv.game_id
              AND tt.position = mv.ply - 1 AND tt.kind = 'top_two' AND jsonb_array_length(tt.results) >= 2
              AND tt.results->0->>'uci' = mv.uci"""
    cols = f""", {first} AS line1_exp, {second} AS line2_exp, tt.results AS tt_results, tt.budget AS tt_budget"""
    return join, cols


def _with_runner_up(ev: dict, r: dict) -> dict:
    line1, line2 = r["tt_results"][0], r["tt_results"][1]
    ev["comparison"] = {
        "best_line": {"uci": line1["uci"], "eval": _eval(line1["score_cp"], line1["mate"], r["color"]),
                      "expected": round(float(r["line1_exp"]), 3)},
        "runner_up": {"uci": line2["uci"], "san": _san(r["fen_before"], line2["uci"]),
                      "eval": _eval(line2["score_cp"], line2["mate"], r["color"]),
                      "expected": round(float(r["line2_exp"]), 3)},
        "search": r["tt_budget"],  # the two lines come from one search (top_two probe)
    }
    return ev


def only_winning_move(conn, q, p, config):
    """Before the move, the engine's two-line search (top_two probe: the two best moves scored in one
    search) had exactly one move keeping a clearly winning position (best >= `winning`, runner-up <=
    `not_winning`: a margin, not a hair), and the player played it. Engine fact: the two lines.
    ChessTrove rules: recaptures on the square just captured on don't count (nobody misses those), and
    delivering mate doesn't count (mate in one is the rule-based layer's). Ranked quiet moves first (a
    capture that parries a mate threat is an only move nobody misses; a quiet one is the rare find),
    then by the gap between the two lines."""
    join, cols = _top_two_join(p)
    first, second = db._line(p.scale, "tt.results->0", "mv.color"), db._line(p.scale, "tt.results->1", "mv.color")
    rows = _moves(conn, q, p,
                  f"{first} >= %(winning)s AND {second} <= %(not_winning)s AND NOT mv.is_recapture AND NOT mv.is_checkmate",
                  f"(mv.captured IS NULL AND NOT mv.is_check) DESC, {_gap_sql(p)} DESC, {STABLE}",
                  join, cols)
    return [_with_runner_up(_move_evidence(r, config, "gap_to_runner_up", round(float(r["line1_exp"] - r["line2_exp"]), 3)), r)
            for r in rows]


def _gap_sql(p: Params) -> str:
    return f"({db._line(p.scale, 'tt.results->0', 'mv.color')} - {db._line(p.scale, 'tt.results->1', 'mv.color')})"


# --- 1. BEST_MOVE_UNDERPROMOTION ----------------------------------------------------------------------

def underpromotion(conn, q, p, config):
    """Every underpromotion the player played, with both engine questions answered separately (see
    insights.py): was it the best move among ALL legal moves (unique / tied / not best / unknown), and
    how did it compare with queening on the same square (better / equal / worse / unknown). Equal
    includes promotions that transpose (the piece is captured either way). Ranked: unique best, tied
    best, then the rest; within those, better than queening first."""
    events = db.list_events(conn, type="UNDERPROMOTION", player=q["player"], platform=q["platform"], limit=1000)
    annotated = insights.annotate(conn, events, config["id"])
    out = []
    for e in annotated:
        a = e["engine_analysis"]
        if a is None:
            continue
        best = ("unknown" if a.get("is_best_move") is None else "unique_best" if a["unique_best_move"]
                else "tied_best" if a["tied_for_best_move"] else "not_best")
        vq = a.get("vs_queen")
        vs_queen = "unknown" if vq is None else vq["verdict"]
        fen_before = _fen_before(conn, e["game_id"], e["ply"])
        am = a.get("all_moves") or {}
        out.append({
            "game": {"id": e["game_id"], "played_at": e["played_at"], "white": e["white"], "black": e["black"],
                     "result": e["result"], "source_key": e["source_key"], "external_id": e["external_id"],
                     "platform": e["source_key"].split(":")[0]},
            "ply": e["ply"], "color": e["color"],
            "player": e["white"] if e["color"] == "w" else e["black"],
            "move": {"san": e["san"], "uci": e["uci"]}, "fen_before": fen_before, "fen_after": e["fen_after"],
            "best_move": best, "vs_queen": vs_queen,
            "played_move_rank": a.get("played_move_rank"), "legal_moves": am.get("legal_moves"),
            "best_moves": [{"uci": u, "san": _san(fen_before, u)} for u in am.get("best_moves", [])],
            "evaluation": am.get("evaluation") or (vq or {}).get("evaluation"),
            "best_evaluation": am.get("best_evaluation"),
            "queen_promotion_evaluation": (vq or {}).get("queen_promotion_evaluation"),
            "eval_before": a["eval_before"], "eval_after": a["eval_after"],
            "searches": {"all_moves": am.get("budget"), "vs_queen": (vq or {}).get("budget")},
            "deeper_verification": a.get("deeper_verification", []),
            "engine": _config(config),
            "score": {"name": "verdict", "value": f"{best}, {vs_queen} than queening" if vs_queen != "equal"
                      else f"{best}, equal to queening"},
        })
    rank = {"unique_best": 0, "tied_best": 1, "not_best": 2, "unknown": 3}
    vrank = {"better": 0, "equal": 1, "worse": 2, "unknown": 3}
    out.sort(key=lambda r: (rank[r["best_move"]], vrank[r["vs_queen"]], -(r["game"]["played_at"].timestamp() if r["game"]["played_at"] else 0)))
    return out[: q["limit"]]


def _fen_before(conn, game_id: int, ply: int) -> str:
    row = conn.execute(
        """SELECT coalesce(prev.fen_after, g.initial_fen, %s) AS fen FROM games g
           LEFT JOIN moves prev ON prev.game_id = g.id AND prev.ply = %s WHERE g.id = %s""",
        (db.START_FEN, ply - 1, game_id)).fetchone()
    return row["fen"]


# --- 6. SOUND MATERIAL SACRIFICE ----------------------------------------------------------------------

SACRIFICE_DEFICIT = {"queen": 5, "rook": 3, "exchange": 2}  # net material the player is down, at least


def _sacrifice(r: dict) -> dict | None:
    """ChessTrove's definition, deterministic (no engine): the opponent's reply captures the player's
    queen or rook, and over the reply and the next four plies the player never gets back to within the
    threshold of where they stood before the move:
      queen sacrifice     reply takes the queen, net deficit >= 5 throughout (a queen for at most a minor)
      rook sacrifice      reply takes a rook,   net deficit >= 3 throughout (a rook for at most two pawns)
      exchange sacrifice  reply takes a rook,   net deficit >= 2 throughout (a rook for a minor piece)
    The whole window must exist unless the game ends in the player's mate within it: a game resigned or
    lost on time right after the capture proves nothing (a real case: ...Qxd2, resigned, when Bxd2
    simply traded queens). A trade (queen taken, queen taken back within two moves) never qualifies;
    neither does a move made
    in check (a king forced out of a fork doesn't sacrifice the queen), nor a piece offered and declined,
    nor a sacrifice that was the move before the one that lost the piece (false negatives, by design). `never_recovered`: the deficit holds to the end
    of the game; `ends_in_mate`: ...and the player then delivers mate."""
    captured, before = r.get("reply_captured"), r.get("balance_before")
    if captured not in ("Q", "R") or before is None or r.get("wb_max_next5") is None:
        return None
    if r.get("in_check_before"):  # no free choice: a forked queen lost to a check isn't a sacrifice
        return None
    mates = bool(r["game_ends_in_mate"] and r["last_mover"] == r["color"])
    if r["last_ply"] - r["ply"] < 5 and not mates:  # the game stopped inside the window: it proves nothing
        return None  # (real case: ...Qxd2 answered by resignation, though Bxd2 simply traded queens)
    white = r["color"] == "w"
    best_next = r["wb_max_next5"] if white else -r["wb_min_next5"]  # the best the player gets back, 5 plies
    deficit = before - best_next
    kind = ("queen" if captured == "Q" and deficit >= SACRIFICE_DEFICIT["queen"] else
            "rook" if captured == "R" and deficit >= SACRIFICE_DEFICIT["rook"] else
            "exchange" if captured == "R" and deficit >= SACRIFICE_DEFICIT["exchange"] else None)
    if kind is None:
        return None
    best_rest = r["wb_max_rest"] if white else -r["wb_min_rest"]
    never = before - best_rest >= SACRIFICE_DEFICIT[kind]
    return {"kind": kind, "piece": "queen" if captured == "Q" else "rook", "reply": r["reply_san"],
            "material_before": before, "material_after_window": best_next, "deficit": deficit,
            "never_recovered": never,
            "ends_in_mate": never and mates}


def material_sacrifice(conn, q, p, config):
    """Sacrifices (definition: _sacrifice) that the engine considers sound: by default the sacrificing
    move was the engine's own first choice ("gave up the queen and still had the best move"); with
    `sacrifice_engine_choice` off, the player's expected score after the move is within
    `sacrifice_tolerance` of before. Either way it's at least `sacrifice_floor` afterwards (a desperado
    in a lost position isn't a sound sacrifice). Why the engine-choice default: in a crushing position
    everything keeps the win, so tolerance alone let through queens thrown away at +22 with mate in 1
    on the board. Engine facts: the evaluations and the engine's choice; the rest is ChessTrove's
    definition. Ranked: sacrifices leading to mate, then queen > rook > exchange, the deficit, the
    position after."""
    sound = ("mv.uci = mv.engine_choice" if p.sacrifice_engine_choice
             else "mv.exp_after >= mv.exp_before - %(sacrifice_tolerance)s")
    rows = _moves(conn, q, p,
                  f"mv.reply_captured IN ('Q', 'R') AND mv.balance_before IS NOT NULL AND NOT mv.in_check_before "
                  f"AND {sound} AND mv.exp_after >= %(sacrifice_floor)s",
                  STABLE, limit=False)
    found = []
    for r in rows:
        sac = _sacrifice(r)
        if sac:
            found.append((r, sac))
    order = {"queen": 0, "rook": 1, "exchange": 2}
    found.sort(key=lambda x: (not x[1]["ends_in_mate"], order[x[1]["kind"]], -x[1]["deficit"], -float(x[0]["exp_after"])))
    return [{**_move_evidence(r, config, "sacrifice", sac["kind"]), "sacrifice": sac} for r, sac in found[: q["limit"]]]


# --- 7. FORCED MATES ------------------------------------------------------------------------------------

def missed_forced_mate(conn, q, p, config):
    """The player had a forced mate in 2 or more (engine: mate score before the move) and their move
    left no forced mate for them (engine: the position after). Shortest first: a missed mate in 2 is
    the sharpest miss. Mate in 1 is the rule-based MISSED_MATE_IN_ONE's. ChessTrove rule: the engine's
    own choice is never 'missing' the mate (the lost mate would be the engine re-evaluating)."""
    rows = _moves(conn, q, p,
                  "mv.mover_mate_before >= 2 AND (mv.mover_mate_after IS NULL OR mv.mover_mate_after < 0) "
                  "AND mv.uci IS DISTINCT FROM mv.engine_choice",
                  f"mv.mover_mate_before, mv.ord_after, {STABLE}")
    return [_move_evidence(r, config, "missed_mate_in", r["mover_mate_before"]) for r in rows]


def longest_mate_found(conn, q, p, config):
    """Games the player won by delivering mate after carrying a forced mate for a while: an unbroken run
    of the player's moves, each made with a forced mate on the board and keeping it (engine: a mate
    score for the player before and after every one), ending in the mate. Ranked by mate_length = min(moves the player took, the engine's mate
    distance at the start), conservative on both sides: at a fixed node budget a mate distance is an
    upper bound (a shallow search can find a long mate before a short one: one real run showed mate in
    13, then mate in 5 a move later), and a player who took 10 moves over a mate in 3 carried a mate in
    3. At least 2 moves. ChessTrove rule: a run that begins against a bare king is technique (K+Q v K),
    not a find."""
    rows = conn.execute(
        f"""WITH mv AS ({db.archaeology_moves_sql(p.scale)}),
            pm AS (SELECT * FROM mv WHERE lower(mv.mover) = lower(%(player)s) AND mv.game_ends_in_mate
                   AND mv.last_mover = mv.color),
            -- a move breaks the run unless it was made with a forced mate AND kept it (or mated)
            brk AS (SELECT game_id, max(ply) FILTER (WHERE mover_mate_before IS NULL OR mover_mate_before <= 0
                                                     OR mover_mate_after IS NULL OR mover_mate_after < 0) AS last_break
                    FROM pm GROUP BY game_id),
            run AS (SELECT DISTINCT ON (pm.game_id) pm.*,
                           count(*) OVER (PARTITION BY pm.game_id) AS run_moves
                    FROM pm JOIN brk USING (game_id) WHERE pm.ply > coalesce(brk.last_break, 0)
                    ORDER BY pm.game_id, pm.ply)
            SELECT *, LEAST(mv.run_moves, mv.mover_mate_before) AS mate_length FROM run mv
            WHERE mv.mover_mate_before >= 2 AND mv.run_moves >= 2
              AND coalesce(mv.opp_material_before, 1) > 0  -- mating a bare king is technique, not a find
            ORDER BY LEAST(mv.run_moves, mv.mover_mate_before) DESC, mv.mover_mate_before DESC, {STABLE}
            LIMIT %(limit)s""",
        {**q, **asdict(p)},
    ).fetchall()
    out = []
    for r in rows:
        ev = _move_evidence(r, config, "mate_length", r["mate_length"])
        ev["run"] = {"moves": r["run_moves"], "engine_mate_in_at_start": r["mover_mate_before"],
                     "mating_ply": r["last_ply"]}
        out.append(ev)
    return out


def only_move_keeping_mate(conn, q, p, config):
    """The two-line search had a forced mate (in 2 or more) for exactly one move, and the runner-up
    doesn't mate and isn't clearly winning either (< `winning`). The player played the mating line.
    ChessTrove rule: without the second condition this was mostly 'the only mate' in positions a
    runner-up at +8 to +12 won anyway. Ranked by the mate's length."""
    join, cols = _top_two_join(p)
    mate1 = "(CASE WHEN mv.color = 'w' THEN 1 ELSE -1 END * (tt.results->0->>'mate')::int)"
    mate2 = "(CASE WHEN mv.color = 'w' THEN 1 ELSE -1 END * (tt.results->1->>'mate')::int)"
    second = db._line(p.scale, "tt.results->1", "mv.color")
    rows = _moves(conn, q, p, f"{mate1} >= 2 AND ({mate2} IS NULL OR {mate2} <= 0) AND {second} < %(winning)s",
                  f"{mate1} DESC, {_gap_sql(p)} DESC, {STABLE}", join, cols + f", {mate1} AS line1_mate")
    return [_with_runner_up(_move_evidence(r, config, "mate_in", r["line1_mate"]), r) for r in rows]


# --- 5. UNUSUAL ENGINE-APPROVED MOVES ------------------------------------------------------------------

def unusualness(features: dict, gap: float, p: Params) -> float:
    """A ranking aid, not a verdict: gap x (1 + w_quiet*quiet + w_retreat*retreat + w_sacrifice*sacrifice
    + w_underpromotion*underpromotion). `gap` is how much worse (expected score) the best alternative
    was in the same search; the features are deterministic marks of a non-obvious move. Weights are
    Params; with all weights 0 this is the gap alone."""
    bonus = (p.weight_quiet * features["quiet"] + p.weight_retreat * features["retreat"]
             + p.weight_sacrifice * bool(features["sacrifice"])
             + p.weight_underpromotion * (features["promotion"] in ("N", "B", "R")))
    return round(gap * (1 + bonus), 3)


def unusual_move(conn, q, p, config):
    """Moves where the engine's two-line search strongly preferred what the player played: the player's
    move is the best line and every alternative is at least `unusual_min_gap` worse (expected score).
    Excludes recaptures, mates on the board, and moves whose runner-up gets mated: those parry a threat
    (on the real history the top of the list was king moves whose only competitor walked into mate in 1),
    they don't surprise. Each result lists its features (quiet, retreat, capture,
    check, sacrifice, underpromotion, whether it kept a win or a forced mate) and is ranked by
    `unusualness`. Coverage: only positions with a top_two probe (clearly winning ones, and the ones
    `engine verify-unusual-moves` searched), which each result's `comparison.search` shows."""
    join, cols = _top_two_join(p)
    mate2 = "(CASE WHEN mv.color = 'w' THEN 1 ELSE -1 END * (tt.results->1->>'mate')::int)"
    rows = _moves(conn, q, p, f"{_gap_sql(p)} >= %(unusual_min_gap)s AND NOT mv.is_recapture AND NOT mv.is_checkmate "
                              f"AND NOT coalesce({mate2} < 0, false)",
                  STABLE, join, cols, limit=False)
    scored = []
    for r in rows:
        gap = round(float(r["line1_exp"] - r["line2_exp"]), 3)
        ev = _with_runner_up(_move_evidence(r, config, "unusualness", 0.0), r)
        ev["score"]["value"] = unusualness(ev["move_class"], gap, p)
        ev["gap"] = gap
        scored.append(ev)
    scored.sort(key=lambda e: (-e["score"]["value"], -e["gap"]))
    return scored[: q["limit"]]


_TYPES = {"biggest_throw": biggest_throw, "biggest_comeback": biggest_comeback, "lost_advantage": lost_advantage,
          "only_winning_move": only_winning_move, "underpromotion": underpromotion,
          "material_sacrifice": material_sacrifice, "missed_forced_mate": missed_forced_mate,
          "longest_mate_found": longest_mate_found, "only_move_keeping_mate": only_move_keeping_mate,
          "unusual_move": unusual_move}
