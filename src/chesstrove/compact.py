"""Converting a database from one row per ply (the old `moves` table, a FEN on every row) to one packed row per
game (game_moves). Two ways, both resumable and both checked game by game before anything counts as done:

  copy     pack the old rows into game_moves, then drop the old table. Needs room for both at once
           (the new table is ~15% of the old) plus the write-ahead log for it.
  rebuild  drop the old table first, then replay every game's stored PGN into game_moves. Needs no extra room,
           but the old rows are gone before the new ones exist: back up first. Every stored game replays from
           its PGN to exactly the rows the import stored (checked on 74,598 real games).

`check` changes nothing and says which state the database is in and what each way needs.
"""

from importlib.resources import files
from typing import Any, Callable

import psycopg

from chesstrove import db
from chesstrove.analysis import analyze
from chesstrove.importers.pgn import ParseFailure, read_pgn

PACKED_BYTES_PER_PLY = 22  # measured: 131.6 MB of game_moves for 6,239,220 plies (heap, TOAST and index)
BATCH = 2000


def _exists(conn, name: str) -> str | None:
    """'r' (table), 'v' (view) or None."""
    row = conn.execute("""SELECT relkind FROM pg_class WHERE relname = %s
                          AND relnamespace = current_schema()::regnamespace""", (name,)).fetchone()
    return row["relkind"] if row else None


def _size(conn, name: str) -> int:
    return conn.execute("SELECT coalesce(pg_total_relation_size(to_regclass(%s)), 0) AS b", (name,)).fetchone()["b"]


def state(conn) -> str:
    """legacy (old table, nothing packed), converting (old table and some packed rows), compact (the view over
    game_moves), or ambiguous (anything else: refuse to touch it)."""
    moves, packed = _exists(conn, "moves"), _exists(conn, "game_moves")
    if moves == "r":
        if packed is None:
            return "legacy"
        has = conn.execute("SELECT EXISTS (SELECT 1 FROM game_moves) AS x").fetchone()["x"]
        return "converting" if has else "legacy"
    if moves == "v" and packed == "r":
        return "compact"
    if moves is None and packed == "r":
        return "compact"  # view not created yet; the next schema run creates it
    return "ambiguous"


def problems(conn, limit: int = 10) -> list[dict]:
    """Games whose packed moves are missing or the wrong length (compact layout only)."""
    return conn.execute(
        """SELECT g.id, g.ply_count, cardinality(gm.flags) AS packed FROM games g
           LEFT JOIN game_moves gm ON gm.game_id = g.id
           WHERE g.ply_count > 0 AND (gm.game_id IS NULL OR cardinality(gm.flags) <> g.ply_count)
           ORDER BY g.id LIMIT %s""", (limit,)).fetchall()


def check(conn) -> dict[str, Any]:
    """Read-only report: state, counts, sizes, and the space each way needs."""
    s = state(conn)
    games = conn.execute("SELECT count(*) AS n, coalesce(sum(ply_count), 0) AS plies FROM games").fetchone()
    out: dict[str, Any] = {"state": s, "games": games["n"], "plies": int(games["plies"])}
    packed_exists = _exists(conn, "game_moves") == "r"
    if packed_exists:
        p = conn.execute("SELECT count(*) AS n, coalesce(sum(cardinality(flags)), 0) AS plies FROM game_moves").fetchone()
        out["packed_games"], out["packed_plies"] = p["n"], int(p["plies"])
    if s in ("legacy", "converting"):
        out["old_rows"] = conn.execute("SELECT count(*) AS n FROM moves").fetchone()["n"]
    sizes = {t: _size(conn, t) for t in ("moves", "game_moves", "games", "game_analysis", "events", "engine_positions",
                                          "engine_move_probes", "engine_game_status", "engine_runs", "engine_configs")}
    mb = lambda b: round(b / 2**20, 1)
    out["mb"] = {"database": mb(conn.execute("SELECT pg_database_size(current_database()) AS b").fetchone()["b"]),
                 **{t: mb(b) for t, b in sizes.items() if b}}
    engine = sum(b for t, b in sizes.items() if t.startswith("engine_"))
    target = out["plies"] * PACKED_BYTES_PER_PLY
    db_now = out["mb"]["database"] * 2**20
    old = sizes["moves"] if _exists(conn, "moves") == "r" else 0
    todo = target - (sizes["game_moves"] if packed_exists else 0)
    out["estimate_mb"] = {
        "packed_moves_when_done": mb(target),
        # copy: old and new side by side, plus WAL for the new rows (not in the database size; counts on disk)
        "copy_peak_database": mb(db_now + max(todo, 0)), "copy_peak_wal_up_to": mb(2 * max(todo, 0)),
        # rebuild: the old table is dropped first
        "rebuild_peak_database": mb(db_now - old + max(target - (sizes["game_moves"] if packed_exists else 0), 0)),
        "final_database": mb(db_now - old + max(todo, 0)),
        "final_without_engine_data": mb(db_now - old + max(todo, 0) - engine),
    }
    if s == "compact":
        out["problems"] = problems(conn)
    return out


def _schema(conn) -> None:
    conn.execute(files("chesstrove").joinpath("schema.sql").read_text())


def copy(conn, batch: int = BATCH, log: Callable[[str], None] = print) -> None:
    """Pack the old table's rows into game_moves batch by batch (each commits; converted games are skipped), check
    every game, then drop the old table."""
    s = state(conn)
    if s == "compact":
        log("already compact")
        return
    if s not in ("legacy", "converting"):
        raise RuntimeError(f"refusing: the moves layout is {s}; run --check")
    _schema(conn)  # creates game_moves; no view while the old table exists
    top = conn.execute("SELECT coalesce(max(game_id), 0) AS top FROM moves").fetchone()["top"]
    for lo in range(0, top, batch):
        conn.execute(
            """INSERT INTO game_moves (game_id, first_color, uci, san, piece, captured, promotion, flags,
                                       material_white, material_black, queens_after, legal_moves_before)
               SELECT game_id, (array_agg(color ORDER BY ply))[1],
                      string_agg(uci, ' ' ORDER BY ply), string_agg(san, ' ' ORDER BY ply),
                      string_agg(piece::text, '' ORDER BY ply), string_agg(coalesce(captured::text, '.'), '' ORDER BY ply),
                      string_agg(coalesce(promotion::text, '.'), '' ORDER BY ply),
                      array_agg((is_check::int + 2 * is_checkmate::int + 4 * is_castling::int + 8 * is_en_passant::int)::smallint ORDER BY ply),
                      array_agg(material_white ORDER BY ply), array_agg(material_black ORDER BY ply),
                      array_agg(queens_after ORDER BY ply), array_agg(legal_moves_before ORDER BY ply)
               FROM moves WHERE game_id > %s AND game_id <= %s GROUP BY game_id
               ON CONFLICT (game_id) DO NOTHING""", (lo, lo + batch))
        log(f"packed games up to id {min(lo + batch, top):,} of {top:,}")
    # every game the old table has, with exactly as many plies
    short = conn.execute(
        """SELECT count(*) AS n FROM (SELECT game_id, count(*) AS plies FROM moves GROUP BY game_id) o
           LEFT JOIN game_moves gm USING (game_id)
           WHERE gm.game_id IS NULL OR cardinality(gm.flags) <> o.plies""").fetchone()["n"]
    if short:
        raise RuntimeError(f"{short} games didn't pack completely; the old table is untouched. Rerun to retry.")
    conn.execute("DROP TABLE moves")
    _finish(conn, log)


def rebuild(conn, batch: int = BATCH, log: Callable[[str], None] = print) -> None:
    """Drop the old table (if any), then replay every game without packed moves from its stored PGN. Rerun to
    resume. Destructive: back up first."""
    s = state(conn)
    if s == "ambiguous":
        raise RuntimeError("refusing: the moves layout is ambiguous; run --check")
    if _exists(conn, "moves") == "r":
        conn.execute("DROP TABLE moves")
        log("dropped the old moves table")
    _schema(conn)  # game_moves and the view
    after, done = 0, 0
    while rows := conn.execute(
            """SELECT g.id, g.pgn FROM games g WHERE g.id > %s AND g.ply_count > 0
                 AND NOT EXISTS (SELECT 1 FROM game_moves gm WHERE gm.game_id = g.id)
               ORDER BY g.id LIMIT %s""", (after, batch)).fetchall():
        packed = []
        for r in rows:
            items = list(read_pgn(r["pgn"]))
            if len(items) != 1 or isinstance(items[0], ParseFailure):
                log(f"game {r['id']}: its PGN doesn't parse; left without moves")
                continue
            packed.append((r["id"], analyze(items[0], ())[0]))
        with conn.transaction():
            db.insert_moves(conn, packed)
        done += len(packed)
        after = rows[-1]["id"]
        log(f"replayed {done:,} games")
    _finish(conn, log)


def _finish(conn, log) -> None:
    _schema(conn)  # creates the view now that the old table is gone
    bad = problems(conn)
    if bad:
        raise RuntimeError(f"games with missing or short moves, e.g. {bad[:3]}; rerun `compact-moves --rebuild` to fill them")
    log("done: every game's moves are packed")
