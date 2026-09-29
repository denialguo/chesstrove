"""All SQL lives here. Plain psycopg, no ORM: the queries are few and worth reading."""

import os
from collections.abc import Iterable
from dataclasses import fields
from datetime import date
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from chesstrove.models import CanonicalGame, MoveFacts

MAX_STORED_ERRORS = 1000

MOVE_COLUMNS = (
    "game_id, ply, color, san, uci, piece, captured, promotion, is_check, is_checkmate, is_castling, "
    "is_en_passant, fen_after, material_white, material_black, queens_after, legal_moves_before"
)


def connect(dsn: str | None = None) -> psycopg.Connection[dict[str, Any]]:
    """Autocommit connection; callers group work with `with conn.transaction():`.

    Uses $CHESSTROVE_DATABASE_URL when set, otherwise a zero-setup embedded Postgres whose data lives in
    ~/.chesstrove/pgdata (or $CHESSTROVE_HOME/pgdata) and persists between runs.
    """
    dsn = dsn or os.environ.get("CHESSTROVE_DATABASE_URL") or embedded_dsn()
    # UTC session so timestamps read back the same regardless of the server's local zone
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row, options="-c timezone=UTC")


@cache
def embedded_dsn() -> str:
    import pgserver  # starts (or reuses) a local server; ~1-2 s the first time per process

    data_dir = Path(os.environ.get("CHESSTROVE_HOME", Path.home() / ".chesstrove")) / "pgdata"
    data_dir.mkdir(parents=True, exist_ok=True)
    return pgserver.get_server(data_dir, cleanup_mode="stop").get_uri()


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(files("chesstrove").joinpath("schema.sql").read_text())


# --- users & accounts ----------------------------------------------------------------------------

def ensure_account(conn: psycopg.Connection, user: str, platform: str, username: str) -> int:
    """Get-or-create the user and the platform account. Returns the account id."""
    # DO UPDATE (a no-op) instead of DO NOTHING so RETURNING yields the existing row too
    user_id = conn.execute(
        "INSERT INTO users (username) VALUES (%s) ON CONFLICT (username) DO UPDATE SET username = EXCLUDED.username RETURNING id",
        (user,),
    ).fetchone()["id"]
    return conn.execute(
        """INSERT INTO chess_accounts (user_id, platform, username) VALUES (%s, %s, %s)
           ON CONFLICT (platform, username) DO UPDATE SET username = EXCLUDED.username RETURNING id""",
        (user_id, platform, username),
    ).fetchone()["id"]


# --- imports -------------------------------------------------------------------------------------

def start_import(conn: psycopg.Connection, source: str, source_ref: str, account_id: int | None = None) -> int:
    row = conn.execute(
        "INSERT INTO imports (source, source_ref, account_id) VALUES (%s, %s, %s) RETURNING id",
        (source, source_ref, account_id),
    ).fetchone()
    return row["id"]


def record_progress(
    conn: psycopg.Connection, import_id: int, seen: int = 0, imported: int = 0, duplicate: int = 0,
    failed: int = 0, errors: list[dict] | None = None, skipped: int = 0,
) -> None:
    conn.execute(
        """UPDATE imports SET
             games_seen = games_seen + %s, games_imported = games_imported + %s,
             games_duplicate = games_duplicate + %s, games_failed = games_failed + %s,
             games_skipped = games_skipped + %s,
             errors = CASE WHEN jsonb_array_length(errors) < %s THEN errors || %s ELSE errors END
           WHERE id = %s""",
        (seen, imported, duplicate, failed, skipped, MAX_STORED_ERRORS, Jsonb(errors or []), import_id),
    )


def set_resume_state(conn: psycopg.Connection, import_id: int, state: dict) -> None:
    conn.execute("UPDATE imports SET resume_state = %s WHERE id = %s", (Jsonb(state), import_id))


def chesscom_months_done(conn: psycopg.Connection, username: str) -> set[str]:
    """Union over every import of this account, so a new, still-empty import row can't hide past progress."""
    rows = conn.execute(
        """SELECT DISTINCT jsonb_array_elements_text(resume_state->'months_done') AS month
           FROM imports WHERE source = 'chesscom' AND source_ref = %s""",
        (username,),
    ).fetchall()
    return {r["month"] for r in rows}


def lichess_checkpoint(conn: psycopg.Connection, username: str) -> int:
    """Latest `since` (ms) recorded by any import of this account; 0 = from the beginning."""
    row = conn.execute(
        """SELECT coalesce(max((resume_state->>'since')::bigint), 0) AS since
           FROM imports WHERE source = 'lichess' AND source_ref = %s""",
        (username,),
    ).fetchone()
    return row["since"]


def set_import_account(conn: psycopg.Connection, import_id: int, account_id: int) -> None:
    conn.execute("UPDATE imports SET account_id = %s WHERE id = %s", (account_id, import_id))


def finish_import(conn: psycopg.Connection, import_id: int, status: str) -> None:
    conn.execute("UPDATE imports SET status = %s, finished_at = now() WHERE id = %s", (status, import_id))


def list_imports(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM imports ORDER BY id DESC").fetchall()


def get_import(conn: psycopg.Connection, import_id: int) -> dict | None:
    return conn.execute("SELECT * FROM imports WHERE id = %s", (import_id,)).fetchone()


# --- games & moves -------------------------------------------------------------------------------

def insert_game(conn: psycopg.Connection, g: CanonicalGame, import_id: int | None) -> int | None:
    """Insert a game; returns its id, or None if a game with the same source_key already exists."""
    row = conn.execute(
        """INSERT INTO games (source_key, source, external_id, import_id, played_at, white, black,
                              white_rating, black_rating, result, time_control, rated, eco, opening,
                              initial_fen, chess960, ply_count, pgn)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (source_key) DO NOTHING
           RETURNING id""",
        (g.source_key, g.source, g.external_id, import_id, g.played_at, g.white, g.black,
         g.white_rating, g.black_rating, g.result, g.time_control, g.rated, g.eco, g.opening,
         g.initial_fen, g.chess960, len(g.moves_uci), g.pgn),
    ).fetchone()
    return row["id"] if row else None


def insert_moves(conn: psycopg.Connection, game_id: int, facts: list[MoveFacts]) -> None:
    if not facts:
        return
    with conn.cursor().copy(f"COPY moves ({MOVE_COLUMNS}) FROM STDIN") as copy:
        for f in facts:
            copy.write_row((
                game_id, f.ply, f.color, f.san, f.uci, f.piece, f.captured, f.promotion, f.is_check,
                f.is_checkmate, f.is_castling, f.is_en_passant, f.fen_after, f.material_white,
                f.material_black, f.queens_after, f.legal_moves_before,
            ))


def list_games(conn: psycopg.Connection, player: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    return conn.execute(
        """SELECT id, source, played_at, white, black, white_rating, black_rating, result,
                  time_control, eco, ply_count
           FROM games
           WHERE %(player)s::text IS NULL OR lower(white) = lower(%(player)s) OR lower(black) = lower(%(player)s)
           ORDER BY played_at DESC NULLS LAST, id DESC
           LIMIT %(limit)s OFFSET %(offset)s""",
        {"player": player, "limit": limit, "offset": offset},
    ).fetchall()


def get_game(conn: psycopg.Connection, game_id: int) -> dict | None:
    game = conn.execute("SELECT * FROM games WHERE id = %s", (game_id,)).fetchone()
    if game:
        game["moves"] = conn.execute("SELECT * FROM moves WHERE game_id = %s ORDER BY ply", (game_id,)).fetchall()
    return game


# --- analysis & events ---------------------------------------------------------------------------

EVENT_COLUMNS = "game_id, ply, type, detector_id, detector_version, color, fen, metadata, analysis_run_id"


def start_analysis_run(conn: psycopg.Connection, detector_versions: dict[str, int]) -> int:
    row = conn.execute(
        "INSERT INTO analysis_runs (detector_versions) VALUES (%s) RETURNING id", (Jsonb(detector_versions),)
    ).fetchone()
    return row["id"]


def record_run_progress(conn: psycopg.Connection, run_id: int, games: int, events: int) -> None:
    conn.execute(
        "UPDATE analysis_runs SET games_processed = games_processed + %s, events_created = events_created + %s WHERE id = %s",
        (games, events, run_id),
    )


def finish_analysis_run(conn: psycopg.Connection, run_id: int, status: str) -> None:
    conn.execute("UPDATE analysis_runs SET status = %s, finished_at = now() WHERE id = %s", (status, run_id))


def set_run_detectors(conn: psycopg.Connection, run_id: int, detector_versions: dict[str, int]) -> None:
    conn.execute("UPDATE analysis_runs SET detector_versions = %s WHERE id = %s", (Jsonb(detector_versions), run_id))


def list_analysis_runs(conn: psycopg.Connection, limit: int = 50) -> list[dict]:
    return conn.execute("SELECT * FROM analysis_runs ORDER BY id DESC LIMIT %s", (limit,)).fetchall()


def get_analysis_run(conn: psycopg.Connection, run_id: int) -> dict | None:
    return conn.execute("SELECT * FROM analysis_runs WHERE id = %s", (run_id,)).fetchone()


def insert_events(conn: psycopg.Connection, game_id: int, run_id: int, events: list[tuple[Any, Any]]) -> None:
    """events: (detector, Event) pairs. Most games have none, so skip the COPY round trips then."""
    if not events:
        return
    with conn.cursor().copy(f"COPY events ({EVENT_COLUMNS}) FROM STDIN") as copy:
        for detector, e in events:
            copy.write_row((game_id, e.ply, e.type, detector.id, detector.version, e.color, e.fen,
                            Jsonb(e.metadata), run_id))


def delete_events(conn: psycopg.Connection, game_ids: list[int], detector_ids: list[str]) -> None:
    conn.execute("DELETE FROM events WHERE game_id = ANY(%s) AND detector_id = ANY(%s)", (game_ids, detector_ids))


def mark_analyzed(conn: psycopg.Connection, game_ids: list[int], versions: dict[str, int]) -> None:
    conn.execute(
        """INSERT INTO game_analysis (game_id, detector_versions)
           SELECT unnest(%s::bigint[]), %s
           ON CONFLICT (game_id) DO UPDATE
             SET detector_versions = game_analysis.detector_versions || EXCLUDED.detector_versions,
                 analyzed_at = now()""",
        (game_ids, Jsonb(versions)),
    )


def games_to_analyze(
    conn: psycopg.Connection, versions: dict[str, int], after_id: int, limit: int, stale_only: bool
) -> list[dict]:
    """Next page (keyset on id) of games plus their stored UCI mainline."""
    return conn.execute(
        """SELECT g.*, ARRAY(SELECT m.uci FROM moves m WHERE m.game_id = g.id ORDER BY m.ply) AS moves_uci
           FROM games g LEFT JOIN game_analysis a ON a.game_id = g.id
           WHERE g.id > %(after)s
             AND (NOT %(stale_only)s OR a.detector_versions IS NULL OR NOT a.detector_versions @> %(versions)s)
           ORDER BY g.id
           LIMIT %(limit)s""",
        {"after": after_id, "limit": limit, "stale_only": stale_only, "versions": Jsonb(versions)},
    ).fetchall()


def has_stale_games(conn: psycopg.Connection, detector_id: str, version: int) -> bool:
    return conn.execute(
        """SELECT EXISTS (SELECT 1 FROM games g LEFT JOIN game_analysis a ON a.game_id = g.id
                          WHERE a.detector_versions IS NULL OR NOT a.detector_versions @> %s) AS stale""",
        (Jsonb({detector_id: version}),),
    ).fetchone()["stale"]


def game_from_row(row: dict) -> CanonicalGame:
    return CanonicalGame(**{f.name: row[f.name] for f in fields(CanonicalGame) if f.name != "moves_uci"},
                         moves_uci=tuple(row["moves_uci"]))


def list_events(
    conn: psycopg.Connection, type: str | None = None, color: str | None = None, player: str | None = None,
    since: date | None = None, until: date | None = None, game_id: int | None = None,
    limit: int = 50, offset: int = 0,
) -> list[dict]:
    """Newest first. `player` means "moves played by this username" (matched to the event's color)."""
    return conn.execute(
        """SELECT e.id, e.game_id, e.ply, e.type, e.detector_version, e.color, e.fen, e.metadata,
                  g.played_at, g.white, g.black, g.result, g.time_control
           FROM events e JOIN games g ON g.id = e.game_id
           WHERE (%(type)s::text IS NULL OR e.type = %(type)s)
             AND (%(color)s::text IS NULL OR e.color = %(color)s)
             AND (%(player)s::text IS NULL OR lower(CASE e.color WHEN 'w' THEN g.white ELSE g.black END) = lower(%(player)s))
             AND (%(since)s::date IS NULL OR g.played_at >= %(since)s)
             AND (%(until)s::date IS NULL OR g.played_at < %(until)s::date + 1)
             AND (%(game_id)s::bigint IS NULL OR e.game_id = %(game_id)s)
           ORDER BY g.played_at DESC NULLS LAST, e.game_id DESC, e.ply
           LIMIT %(limit)s OFFSET %(offset)s""",
        {"type": type, "color": color, "player": player, "since": since, "until": until,
         "game_id": game_id, "limit": limit, "offset": offset},
    ).fetchall()


# --- engine analysis (Layer 2) --------------------------------------------------------------------

ENGINE_POSITION_COLUMNS = (
    "config_id, game_id, position, score_cp, mate, wdl, best_uci, pv_uci, multipv, depth, seldepth, nodes"
)
_PENDING = """FROM games g
              WHERE NOT EXISTS (SELECT 1 FROM engine_game_status s WHERE s.config_id = %(config)s AND s.game_id = g.id)"""


def ensure_engine_config(conn: psycopg.Connection, engine_name: str, s: Any) -> int:
    """Get-or-create the config row for (engine as it reports itself, settings)."""
    return conn.execute(
        """INSERT INTO engine_configs (engine_name, limit_kind, limit_value, multipv, threads, hash_mb)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (engine_name, limit_kind, limit_value, multipv, threads, hash_mb)
             DO UPDATE SET engine_name = EXCLUDED.engine_name
           RETURNING id""",
        (engine_name, s.limit_kind, s.limit_value, s.multipv, s.threads, s.hash_mb),
    ).fetchone()["id"]


def pending_engine_game_ids(conn: psycopg.Connection, config_id: int, limit: int | None) -> list[int]:
    """Games with no results under this config, newest first (recent games get engine insights first).
    Taken as a snapshot at the start of a run, so games still in flight can't be handed out twice."""
    rows = conn.execute(
        f"SELECT g.id {_PENDING} ORDER BY g.played_at DESC NULLS LAST, g.id DESC LIMIT %(limit)s",
        {"config": config_id, "limit": limit},
    ).fetchall()
    return [r["id"] for r in rows]


def games_with_moves(conn: psycopg.Connection, game_ids: list[int]) -> list[dict]:
    return conn.execute(
        """SELECT g.*, ARRAY(SELECT m.uci FROM moves m WHERE m.game_id = g.id ORDER BY m.ply) AS moves_uci
           FROM games g WHERE g.id = ANY(%s)""",
        (game_ids,),
    ).fetchall()


def start_engine_run(conn: psycopg.Connection, config_id: int, games_total: int, workers: int,
                     binary_path: str | None, binary_sha256: str | None) -> int:
    return conn.execute(
        """INSERT INTO engine_runs (config_id, games_total, workers, binary_path, binary_sha256)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (config_id, games_total, workers, binary_path, binary_sha256),
    ).fetchone()["id"]


def insert_engine_probes(conn: psycopg.Connection, config_id: int, game_id: int, probes: list[Any]) -> None:
    for p in probes:
        conn.execute(
            "INSERT INTO engine_move_probes (config_id, game_id, position, moves, results) VALUES (%s, %s, %s, %s, %s)",
            (config_id, game_id, p.position, list(p.moves), Jsonb(list(p.results))),
        )


def insert_engine_positions(conn: psycopg.Connection, config_id: int, game_id: int, results: list[Any]) -> None:
    with conn.cursor().copy(f"COPY engine_positions ({ENGINE_POSITION_COLUMNS}) FROM STDIN") as copy:
        for r in results:
            copy.write_row((config_id, game_id, r.position, r.score_cp, r.mate, list(r.wdl) if r.wdl else None,
                            r.best_uci, r.pv_uci, Jsonb(r.multipv) if r.multipv else None, r.depth, r.seldepth, r.nodes))


def mark_engine_game_done(conn: psycopg.Connection, config_id: int, game_id: int, run_id: int, positions: int) -> None:
    conn.execute(
        "INSERT INTO engine_game_status (config_id, game_id, run_id, positions) VALUES (%s, %s, %s, %s)",
        (config_id, game_id, run_id, positions),
    )


def record_engine_progress(conn: psycopg.Connection, run_id: int, games: int, positions: int, seconds: float) -> None:
    conn.execute(
        """UPDATE engine_runs SET games_done = games_done + %s, positions_done = positions_done + %s,
                                  engine_seconds = engine_seconds + %s WHERE id = %s""",
        (games, positions, seconds, run_id),
    )


def record_engine_error(conn: psycopg.Connection, run_id: int, error: dict) -> None:
    conn.execute(
        """UPDATE engine_runs SET games_failed = games_failed + 1,
             errors = CASE WHEN jsonb_array_length(errors) < %s THEN errors || %s ELSE errors END
           WHERE id = %s""",
        (MAX_STORED_ERRORS, Jsonb([error]), run_id),
    )


def finish_engine_run(conn: psycopg.Connection, run_id: int, status: str) -> None:
    conn.execute("UPDATE engine_runs SET status = %s, finished_at = now() WHERE id = %s", (status, run_id))


def list_engine_runs(conn: psycopg.Connection, limit: int = 50) -> list[dict]:
    return conn.execute(
        """SELECT r.*, c.engine_name, c.limit_kind, c.limit_value, c.multipv
           FROM engine_runs r JOIN engine_configs c ON c.id = r.config_id ORDER BY r.id DESC LIMIT %s""",
        (limit,),
    ).fetchall()


def status_summary(conn: psycopg.Connection, detector_versions: dict[str, int]) -> dict:
    """Both layers at a glance: how much is imported, and how far each analysis has got."""
    totals = conn.execute(
        """SELECT count(*) AS games, coalesce(sum(ply_count + 1), 0) AS positions,
                  count(*) FILTER (WHERE a.detector_versions @> %s) AS deterministic_done
           FROM games g LEFT JOIN game_analysis a ON a.game_id = g.id""",
        (Jsonb(detector_versions),),
    ).fetchone()
    engines = conn.execute(
        """SELECT c.id AS config_id, c.engine_name, c.limit_kind, c.limit_value, c.multipv,
                  count(s.game_id) AS games_done, coalesce(sum(s.positions), 0) AS positions_done
           FROM engine_configs c LEFT JOIN engine_game_status s ON s.config_id = c.id
           GROUP BY c.id ORDER BY c.id""",
    ).fetchall()
    return {**totals, "engine": engines}


def get_engine_config(conn: psycopg.Connection, config_id: int) -> dict | None:
    return conn.execute("SELECT * FROM engine_configs WHERE id = %s", (config_id,)).fetchone()


def default_engine_config(conn: psycopg.Connection) -> dict | None:
    """The config that covers the most games (ties: the newest)."""
    return conn.execute(
        """SELECT c.* FROM engine_configs c JOIN engine_game_status s ON s.config_id = c.id
           GROUP BY c.id ORDER BY count(*) DESC, c.id DESC LIMIT 1""",
    ).fetchone()


def engine_facts_for_moves(conn: psycopg.Connection, config_id: int, moves: list[tuple[int, int]]) -> dict:
    """For each (game_id, ply): the move, the engine's view of the positions before and after it, and any
    probe searched from before it. Keyed by (game_id, ply); moves without analysis are absent."""
    if not moves:
        return {}
    rows = conn.execute(
        """SELECT k.game_id, k.ply, m.uci, m.color,
                  b.score_cp AS before_cp, b.mate AS before_mate, b.best_uci, b.multipv,
                  a.score_cp AS after_cp, a.mate AS after_mate, p.results AS probe
           FROM unnest(%(games)s::bigint[], %(plies)s::int[]) AS k(game_id, ply)
           JOIN moves m ON m.game_id = k.game_id AND m.ply = k.ply
           JOIN engine_positions b ON b.config_id = %(config)s AND b.game_id = k.game_id AND b.position = k.ply - 1
           LEFT JOIN engine_positions a ON a.config_id = %(config)s AND a.game_id = k.game_id AND a.position = k.ply
           LEFT JOIN LATERAL (SELECT results FROM engine_move_probes p
                              WHERE p.config_id = %(config)s AND p.game_id = k.game_id AND p.position = k.ply - 1
                                AND m.uci = ANY(p.moves) LIMIT 1) p ON true""",
        {"games": [g for g, _ in moves], "plies": [p for _, p in moves], "config": config_id},
    ).fetchall()
    return {(r["game_id"], r["ply"]): r for r in rows}
