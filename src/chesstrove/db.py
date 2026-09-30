"""All SQL lives here. Plain psycopg, no ORM: the queries are few and worth reading."""

import os
from collections.abc import Iterable
from dataclasses import fields
from datetime import date
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any

import chess
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from chesstrove.detectors import DETECTORS
from chesstrove.detectors.named_mates import NAMED_MATES
from chesstrove.models import CanonicalGame, MoveFacts

MAX_STORED_ERRORS = 1000

GAME_MOVES_COLUMNS = ("game_id, first_color, uci, san, piece, captured, promotion, flags, material_white, "
                      "material_black, queens_after, legal_moves_before")


def connect(dsn: str | None = None) -> psycopg.Connection[dict[str, Any]]:
    """Autocommit connection; callers group work with `with conn.transaction():`.

    Uses $CHESSTROVE_DATABASE_URL when set, otherwise a zero-setup embedded Postgres whose data lives in
    ~/.chesstrove/pgdata (or $CHESSTROVE_HOME/pgdata) and persists between runs.
    """
    dsn = dsn or os.environ.get("CHESSTROVE_DATABASE_URL") or embedded_dsn()
    # UTC session so timestamps read back the same regardless of the server's local zone
    # prepare_threshold=None: hosted poolers (Supabase's Supavisor, PgBouncer) can't keep prepared statements
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row, options="-c timezone=UTC",
                           prepare_threshold=None)


@cache
def embedded_dsn() -> str:
    import pgserver  # starts (or reuses) a local server; ~1-2 s the first time per process

    data_dir = Path(os.environ.get("CHESSTROVE_HOME", Path.home() / ".chesstrove")) / "pgdata"
    data_dir.mkdir(parents=True, exist_ok=True)
    return pgserver.get_server(data_dir, cleanup_mode="stop").get_uri()


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(files("chesstrove").joinpath("schema.sql").read_text())
    if has_legacy_moves(conn):
        raise RuntimeError("this database still stores one row per move (the old `moves` table). Back it up, then "
                           "run `chesstrove compact-moves --check` and convert it (README, \"Compacting the moves table\").")


def has_legacy_moves(conn: psycopg.Connection) -> bool:
    return conn.execute("""SELECT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'moves' AND relkind = 'r'
                           AND relnamespace = current_schema()::regnamespace) AS x""").fetchone()["x"]


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


def set_profile(conn: psycopg.Connection, import_id: int, profile: dict) -> None:
    conn.execute("UPDATE imports SET games_expected = %s, player_rating = %s, rating_mode = %s WHERE id = %s",
                 (profile["games"], profile["rating"], profile["rating_mode"], import_id))


def latest_import(conn: psycopg.Connection, source: str, source_ref: str) -> dict | None:
    return conn.execute("SELECT * FROM imports WHERE source = %s AND source_ref = %s ORDER BY id DESC LIMIT 1",
                        (source, source_ref)).fetchone()


def fail_running_imports(conn: psycopg.Connection, reason: str) -> None:
    conn.execute("""UPDATE imports SET status = 'failed', finished_at = now(), errors = errors || %s
                    WHERE status = 'running'""", (Jsonb([{"error": reason}]),))


# --- games & moves -------------------------------------------------------------------------------

GAME_COLUMNS = ("source_key, source, external_id, import_id, played_at, white, black, white_rating, black_rating, "
                "result, time_control, rated, eco, opening, initial_fen, chess960, ply_count, pgn")


def _game_row(g: CanonicalGame, import_id: int | None) -> tuple:
    return (g.source_key, g.source, g.external_id, import_id, g.played_at, g.white, g.black,
            g.white_rating, g.black_rating, g.result, g.time_control, g.rated, g.eco, g.opening,
            g.initial_fen, g.chess960, len(g.moves_uci), g.pgn)


def insert_game(conn: psycopg.Connection, g: CanonicalGame, import_id: int | None) -> int | None:
    """Insert a game; returns its id, or None if a game with the same source_key already exists."""
    return insert_games(conn, [g], import_id).get(g.source_key)


def insert_games(conn: psycopg.Connection, games: list[CanonicalGame], import_id: int | None) -> dict[str, int]:
    """Insert games in one statement; returns {source_key: id} for the ones that weren't already stored."""
    if not games:
        return {}
    one = "(" + ", ".join(["%s"] * 18) + ")"
    rows = conn.execute(
        f"""INSERT INTO games ({GAME_COLUMNS}) VALUES {", ".join([one] * len(games))}
            ON CONFLICT (source_key) DO NOTHING RETURNING id, source_key""",
        [v for g in games for v in _game_row(g, import_id)],
    ).fetchall()
    return {r["source_key"]: r["id"] for r in rows}


def existing_game_keys(conn: psycopg.Connection, keys: list[str]) -> set[str]:
    rows = conn.execute("SELECT source_key FROM games WHERE source_key = ANY(%s)", (keys,)).fetchall()
    return {r["source_key"] for r in rows}


def insert_moves(conn: psycopg.Connection, games: list[tuple[int, list[MoveFacts]]]) -> None:
    """(game id, facts) pairs, all in one COPY, one packed row per game (schema.sql: game_moves)."""
    games = [(game_id, facts) for game_id, facts in games if facts]
    if not games:
        return
    with conn.cursor().copy(f"COPY game_moves ({GAME_MOVES_COLUMNS}) FROM STDIN") as copy:
        for game_id, facts in games:
            copy.write_row((
                game_id, facts[0].color, " ".join(f.uci for f in facts), " ".join(f.san for f in facts),
                "".join(f.piece for f in facts), "".join(f.captured or "." for f in facts),
                "".join(f.promotion or "." for f in facts),
                [f.is_check + 2 * f.is_checkmate + 4 * f.is_castling + 8 * f.is_en_passant for f in facts],
                [f.material_white for f in facts], [f.material_black for f in facts],
                [f.queens_after for f in facts], [f.legal_moves_before for f in facts],
            ))


def positions(conn: psycopg.Connection, game_ids: Iterable[int]) -> dict[int, list[str]]:
    """Each game's positions as FENs, replayed from its stored moves: [start, after ply 1, after ply 2, ...].
    Positions aren't stored (they'd be most of the database); a replay is ~0.3 ms a game."""
    rows = conn.execute(
        """SELECT g.id, g.initial_fen, g.chess960, gm.uci FROM games g
           LEFT JOIN game_moves gm ON gm.game_id = g.id WHERE g.id = ANY(%s)""", (list(set(game_ids)),)).fetchall()
    out = {}
    for r in rows:
        board = chess.Board(r["initial_fen"] or chess.STARTING_FEN, chess960=r["chess960"])
        fens = [board.fen()]
        for uci in (r["uci"] or "").split():
            board.push_uci(uci)
            fens.append(board.fen())
        out[r["id"]] = fens
    return out


def attach_fens(conn: psycopg.Connection, rows: list[dict], ply: str = "ply") -> list[dict]:
    """Sets fen_before and fen_after on rows that carry game_id and a ply (the move), replaying only their games."""
    games = positions(conn, (r["game_id"] for r in rows))
    for r in rows:
        fens = games.get(r["game_id"], [])
        r["fen_before"] = fens[r[ply] - 1] if 0 < r[ply] <= len(fens) else None
        r["fen_after"] = fens[r[ply]] if 0 <= r[ply] < len(fens) else None
    return rows


# A game's platform is the prefix of its dedupe key (chesscom:/lichess:), so it also covers games that
# arrived as a PGN export. Usernames are only unique per platform.
def list_games(conn: psycopg.Connection, player: str | None = None, limit: int = 50, offset: int = 0,
               platform: str | None = None) -> list[dict]:
    return conn.execute(
        """SELECT id, source, source_key, external_id, played_at, white, black, white_rating, black_rating,
                  result, time_control, eco, opening, ply_count
           FROM games g
           WHERE (%(player)s::text IS NULL OR lower(white) = lower(%(player)s) OR lower(black) = lower(%(player)s))
             AND (%(platform)s::text IS NULL OR split_part(g.source_key, ':', 1) = %(platform)s)
           ORDER BY played_at DESC NULLS LAST, id DESC
           LIMIT %(limit)s OFFSET %(offset)s""",
        {"player": player, "limit": limit, "offset": offset, "platform": platform},
    ).fetchall()


def get_game(conn: psycopg.Connection, game_id: int) -> dict | None:
    game = conn.execute("SELECT * FROM games WHERE id = %s", (game_id,)).fetchone()
    if game:
        game["moves"] = conn.execute("SELECT * FROM moves WHERE game_id = %s ORDER BY ply", (game_id,)).fetchall()
        fens = positions(conn, [game_id]).get(game_id, [])
        for m in game["moves"]:
            m["fen_after"] = fens[m["ply"]]
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


def insert_events(conn: psycopg.Connection, run_id: int, games: list[tuple[int, list[tuple[Any, Any]]]]) -> None:
    """(game id, [(detector, Event)]) pairs, all in one COPY. Most games have none, so skip it then."""
    if not any(events for _, events in games):
        return
    with conn.cursor().copy(f"COPY events ({EVENT_COLUMNS}) FROM STDIN") as copy:
        for game_id, events in games:
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
    limit: int = 50, offset: int = 0, platform: str | None = None, against: str | None = None,
) -> list[dict]:
    """Newest first. `player` = moves played by this username (matched to the event's color);
    `against` = moves played by this username's opponents."""
    rows = conn.execute(
        f"""SELECT e.id, e.game_id, e.ply, e.type, e.detector_version, e.color, e.fen, e.metadata,
                  g.played_at, g.white, g.black, g.result, g.time_control, g.source_key, g.external_id, g.chess960,
                  m.san, m.uci
           FROM events e JOIN games g ON g.id = e.game_id
           JOIN moves m ON m.game_id = e.game_id AND m.ply = e.ply
           WHERE (%(type)s::text IS NULL OR e.type = %(type)s)
             AND (%(color)s::text IS NULL OR e.color = %(color)s)
             AND (%(player)s::text IS NULL OR lower(CASE e.color WHEN 'w' THEN g.white ELSE g.black END) = lower(%(player)s))
             AND (%(against)s::text IS NULL OR lower(CASE e.color WHEN 'w' THEN g.black ELSE g.white END) = lower(%(against)s))
             AND (%(since)s::date IS NULL OR g.played_at >= %(since)s)
             AND (%(until)s::date IS NULL OR g.played_at < %(until)s::date + 1)
             AND (%(game_id)s::bigint IS NULL OR e.game_id = %(game_id)s)
             AND (%(platform)s::text IS NULL OR split_part(g.source_key, ':', 1) = %(platform)s)
           ORDER BY g.played_at DESC NULLS LAST, e.game_id DESC, e.ply
           LIMIT %(limit)s OFFSET %(offset)s""",
        {"type": type, "color": color, "player": player, "since": since, "until": until,
         "game_id": game_id, "limit": limit, "offset": offset, "platform": platform, "against": against},
    ).fetchall()
    for r in rows:  # the position after the move: the stored position before it, plus the move
        board = chess.Board(r["fen"], chess960=r["chess960"])
        board.push_uci(r["uci"])
        r["fen_after"] = board.fen()
    return rows


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
            """INSERT INTO engine_move_probes (config_id, game_id, position, kind, moves, results, budget)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (config_id, game_id, p.position, p.kind, list(p.moves), Jsonb(list(p.results)), Jsonb(p.budget)),
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
    engines = conn.execute(  # full-history configs only (probe-only verification configs have no runs)
        """SELECT c.id AS config_id, c.engine_name, c.limit_kind, c.limit_value, c.multipv,
                  count(s.game_id) AS games_done, coalesce(sum(s.positions), 0) AS positions_done
           FROM engine_configs c LEFT JOIN engine_game_status s ON s.config_id = c.id
           WHERE EXISTS (SELECT 1 FROM engine_runs r WHERE r.config_id = c.id)
           GROUP BY c.id ORDER BY c.id""",
    ).fetchall()
    return {**totals, "engine": engines}


def underpromotion_moves(conn: psycopg.Connection) -> list[tuple[int, int, str]]:
    rows = conn.execute(
        """SELECT e.game_id, e.ply, m.uci FROM events e JOIN moves m ON m.game_id = e.game_id AND m.ply = e.ply
           WHERE e.type = 'UNDERPROMOTION' ORDER BY e.game_id, e.ply""",
    ).fetchall()
    return [(r["game_id"], r["ply"], r["uci"]) for r in rows]


def position_depths(conn: psycopg.Connection, config_id: int, positions: list[tuple[int, int]],
                    mate_depth: int | None = None) -> dict:
    """(game_id, position) -> the depth the config's normal analysis reached there: a probe's target.
    `mate_depth`: where that analysis ended on a proven mate, the reported depth isn't an effort measure
    (once a mate is proven the tree collapses and Stockfish reports up to 245 almost for free), so the
    target is this instead."""
    if not positions:
        return {}
    rows = conn.execute(
        """SELECT p.game_id, p.position, p.depth, p.mate FROM unnest(%s::bigint[], %s::int[]) AS k(game_id, position)
           JOIN engine_positions p ON p.config_id = %s AND p.game_id = k.game_id AND p.position = k.position""",
        ([g for g, _ in positions], [p for _, p in positions], config_id),
    ).fetchall()
    return {(r["game_id"], r["position"]): (mate_depth if mate_depth and r["mate"] is not None else r["depth"])
            for r in rows if r["depth"]}


def delete_probes(conn: psycopg.Connection, config_id: int, kinds: tuple[str, ...], player: str | None = None) -> None:
    """`player`: only probes of positions where that username was to move (a scoped refresh never touches
    anyone else's)."""
    conn.execute(
        """DELETE FROM engine_move_probes p USING moves m, games g
           WHERE p.config_id = %(config)s AND p.kind = ANY(%(kinds)s)
             AND m.game_id = p.game_id AND m.ply = p.position + 1 AND g.id = p.game_id
             AND (%(player)s::text IS NULL
                  OR lower(CASE m.color WHEN 'w' THEN g.white ELSE g.black END) = lower(%(player)s))""",
        {"config": config_id, "kinds": list(kinds), "player": player})


def probe_keys(conn: psycopg.Connection, config_id: int) -> set[tuple[int, int, str]]:
    rows = conn.execute("SELECT game_id, position, kind FROM engine_move_probes WHERE config_id = %s", (config_id,))
    return {(r["game_id"], r["position"], r["kind"]) for r in rows}


def other_config_probes(conn: psycopg.Connection, base_config_id: int, positions: list[tuple[int, int]]) -> dict:
    """Probes under configs other than `base_config_id` for (game_id, position), with each config's row,
    strongest first. Keyed by (game_id, position) -> list of {config, kind, moves, results, budget}."""
    if not positions:
        return {}
    rows = conn.execute(
        """SELECT p.game_id, p.position, p.kind, p.moves, p.results, p.budget, row_to_json(c) AS config
           FROM unnest(%(games)s::bigint[], %(positions)s::int[]) AS k(game_id, position)
           JOIN engine_move_probes p ON p.game_id = k.game_id AND p.position = k.position
           JOIN engine_configs c ON c.id = p.config_id
           WHERE p.config_id <> %(base)s AND p.kind IN ('vs_queen', 'all_moves')
           ORDER BY c.limit_kind, c.limit_value DESC, c.id""",
        {"games": [g for g, _ in positions], "positions": [p for _, p in positions], "base": base_config_id},
    ).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault((r["game_id"], r["position"]), []).append(r)
    return out


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
        """SELECT k.game_id, k.ply, m.uci, m.color, g.chess960,
                  b.score_cp AS before_cp, b.mate AS before_mate, b.best_uci, b.multipv,
                  a.score_cp AS after_cp, a.mate AS after_mate,
                  q.results AS vs_queen, q.budget AS vs_queen_budget,
                  x.results AS all_moves, x.moves AS all_moves_list, x.budget AS all_moves_budget
           FROM unnest(%(games)s::bigint[], %(plies)s::int[]) AS k(game_id, ply)
           JOIN moves m ON m.game_id = k.game_id AND m.ply = k.ply
           JOIN games g ON g.id = k.game_id
           JOIN engine_positions b ON b.config_id = %(config)s AND b.game_id = k.game_id AND b.position = k.ply - 1
           LEFT JOIN engine_positions a ON a.config_id = %(config)s AND a.game_id = k.game_id AND a.position = k.ply
           LEFT JOIN engine_move_probes q ON q.config_id = %(config)s AND q.game_id = k.game_id
                AND q.position = k.ply - 1 AND q.kind = 'vs_queen' AND m.uci = ANY(q.moves)
           LEFT JOIN engine_move_probes x ON x.config_id = %(config)s AND x.game_id = k.game_id
                AND x.position = k.ply - 1 AND x.kind = 'all_moves'""",
        {"games": [g for g, _ in moves], "plies": [p for _, p in moves], "config": config_id},
    ).fetchall()
    return {(r["game_id"], r["ply"]): r for r in attach_fens(conn, rows)}


LICHESS_K = 0.00368208  # Lichess's win% curve, fitted to human games: win = 1 / (1 + exp(-k * cp))


def _expected(scale: str, color: str, cp: str, mate: str, wdl: str) -> str:
    """SQL: the mover's expected score (0..1) from stored White-POV values.

    stockfish: (W + D/2) / 1000 from Stockfish's WDL (calibrated to engine-strength play: steep)
    lichess:   Lichess's logistic curve on centipawns (calibrated to human games: gentler)
    A forced mate is 1 or 0 on both; mate = 0 means the side to move is mated (only ever after a move,
    so the mover delivered it: 1.0). Stalemate is stored as 0 cp (0.5 on the lichess scale).
    """
    mover_mates = f"CASE WHEN ({mate} > 0) = ({color} = 'w') THEN 1.0 ELSE 0.0 END"
    if scale == "stockfish":
        from_wdl = (f"CASE WHEN {color} = 'w' THEN ({wdl}[1] + {wdl}[2] / 2.0) / 1000 "
                    f"ELSE ({wdl}[3] + {wdl}[2] / 2.0) / 1000 END")
        return f"CASE WHEN {mate} = 0 THEN 1.0 WHEN {wdl} IS NOT NULL THEN {from_wdl} ELSE 0.5 END"
    if scale == "lichess":
        pov_cp = f"(CASE WHEN {color} = 'w' THEN {cp} ELSE -{cp} END)"
        return (f"CASE WHEN {mate} = 0 THEN 1.0 WHEN {mate} IS NOT NULL THEN {mover_mates} "
                f"ELSE 1 / (1 + exp(-{LICHESS_K} * {pov_cp})) END")
    raise ValueError(f"unknown scale {scale!r}")


def _line(scale: str, line: str, color: str) -> str:
    """_expected for one line of a probe's jsonb results."""
    wdl = f"(ARRAY[({line}->'wdl'->>0)::int, ({line}->'wdl'->>1)::int, ({line}->'wdl'->>2)::int])"
    return _expected(scale, color, f"({line}->>'score_cp')::int", f"({line}->>'mate')::int", wdl)


def engine_label_rows(conn: psycopg.Connection, label: str, config_id: int, blunder: float, winning: float,
                      not_winning: float, player: str | None, limit: int, scale: str = "lichess",
                      include_recaptures: bool = False, platform: str | None = None) -> list[dict]:
    first, second = _line(scale, "p.results->0", "mv.color"), _line(scale, "p.results->1", "mv.color")
    extra_join, extra_cols = "", ""
    # BLUNDER and MISSED_WIN are threshold views over the biggest_throw quantity (archaeology.py), with
    # its rule: a move that was the engine's own choice is never a mistake (the "drop" is the engine
    # seeing further one ply later; 18 of 9,480 BLUNDER rows on the real corpus were that artifact).
    not_engine_choice = "mv.uci IS DISTINCT FROM mv.engine_choice"
    if label == "BLUNDER":
        where = f"mv.before - mv.after >= %(blunder)s AND {not_engine_choice}"
        order = "mv.before - mv.after DESC, mv.cp_swing DESC"
    elif label == "MISSED_WIN":
        where = f"mv.before >= %(winning)s AND mv.after <= %(not_winning)s AND {not_engine_choice}"
        order = "mv.before - mv.after DESC, mv.cp_swing DESC"
    elif label == "ONLY_WINNING_MOVE":
        extra_join = """JOIN engine_move_probes p ON p.config_id = %(config)s AND p.game_id = mv.game_id
                          AND p.position = mv.ply - 1 AND p.kind = 'top_two'"""
        extra_cols = f""", p.results->1->>'uci' AS runner_up, round({first}, 3) AS best_line,
                          round({second}, 3) AS runner_up_line"""
        where = f"""jsonb_array_length(p.results) >= 2 AND mv.uci = p.results->0->>'uci'
                    AND {first} >= %(winning)s AND {second} <= %(not_winning)s
                    AND (%(include_recaptures)s OR NOT mv.is_recapture)"""
        # quiet moves first: a found quiet only-move is the rare, impressive kind; mates and captures after
        order = f"mv.is_capture OR mv.is_check, {first} - {second} DESC"
    else:
        raise ValueError(f"unknown label {label!r}")
    rows = conn.execute(
        f"""WITH mv AS (
              SELECT m.game_id, m.ply, m.color, m.san, m.uci, b.best_uci AS engine_choice,
                     m.captured IS NOT NULL AS is_capture, m.is_check,
                     -- takes back on the square the opponent just captured on: an "only move" nobody misses
                     (m.captured IS NOT NULL AND m.prev_captured IS NOT NULL
                      AND substr(m.uci, 3, 2) = substr(m.prev_uci, 3, 2)) AS is_recapture,
                     {_expected(scale, 'm.color', 'b.score_cp', 'b.mate', 'b.wdl')} AS before,
                     {_expected(scale, 'm.color', 'a.score_cp', 'a.mate', 'a.wdl')} AS after,
                     abs(coalesce(b.score_cp, 0) - coalesce(a.score_cp, 0)) AS cp_swing,
                     b.score_cp AS cp_before, b.mate AS mate_before, a.score_cp AS cp_after, a.mate AS mate_after
              FROM games g0
              CROSS JOIN LATERAL ({_moves_with_neighbours("g0.id")}) m
              JOIN engine_positions b ON b.config_id = %(config)s AND b.game_id = m.game_id AND b.position = m.ply - 1
              JOIN engine_positions a ON a.config_id = %(config)s AND a.game_id = m.game_id AND a.position = m.ply
              WHERE %(player)s::text IS NULL OR lower(g0.white) = lower(%(player)s) OR lower(g0.black) = lower(%(player)s)
            )
            SELECT %(label)s AS type, %(scale)s AS scale, mv.game_id, mv.ply, mv.color, mv.san, mv.uci,
                   mv.engine_choice, mv.is_capture, mv.is_check, mv.is_recapture,
                   round(mv.before, 3) AS expected_before, round(mv.after, 3) AS expected_after,
                   round(mv.before - mv.after, 3) AS expected_drop {extra_cols},
                   mv.cp_before, mv.mate_before, mv.cp_after, mv.mate_after,
                   g.initial_fen,
                   g.played_at, g.white, g.black, g.result, g.source, g.external_id
            FROM mv JOIN games g ON g.id = mv.game_id {extra_join}
            WHERE (%(player)s::text IS NULL
                   OR lower(CASE mv.color WHEN 'w' THEN g.white ELSE g.black END) = lower(%(player)s))
              AND (%(platform)s::text IS NULL OR split_part(g.source_key, ':', 1) = %(platform)s)
              AND {where}
            ORDER BY {order}, g.played_at DESC NULLS LAST, mv.game_id, mv.ply  -- exact ties are common (every mate is 1.0)
            LIMIT %(limit)s""",
        {"label": label, "scale": scale, "config": config_id, "blunder": blunder, "winning": winning,
         "not_winning": not_winning, "player": player, "limit": limit, "include_recaptures": include_recaptures,
         "platform": platform},
    ).fetchall()
    return attach_fens(conn, rows)


def only_winning_move_candidates(conn: psycopg.Connection, config_id: int, winning: float,
                                 player: str | None) -> list[tuple[int, int]]:
    """(game_id, position) where the mover was clearly winning on EITHER scale, played the engine's
    choice, had more than one legal move, and no two-line search exists yet: the only places
    ONLY_WINNING_MOVE can apply, whichever scale is used to read it later."""
    sf = _expected("stockfish", "m.color", "b.score_cp", "b.mate", "b.wdl")
    li = _expected("lichess", "m.color", "b.score_cp", "b.mate", "b.wdl")
    rows = conn.execute(
        f"""SELECT m.game_id, m.ply - 1 AS position
            FROM moves m
            JOIN games g ON g.id = m.game_id
            JOIN engine_positions b ON b.config_id = %(config)s AND b.game_id = m.game_id AND b.position = m.ply - 1
            WHERE m.uci = b.best_uci AND m.legal_moves_before > 1
              AND GREATEST({sf}, {li}) >= %(winning)s
              AND (%(player)s::text IS NULL
                   OR lower(CASE m.color WHEN 'w' THEN g.white ELSE g.black END) = lower(%(player)s))
              AND NOT EXISTS (SELECT 1 FROM engine_move_probes p WHERE p.config_id = %(config)s
                              AND p.game_id = m.game_id AND p.position = m.ply - 1 AND p.kind = 'top_two')
            ORDER BY m.game_id, m.ply""",
        {"config": config_id, "winning": winning, "player": player},
    ).fetchall()
    return [(r["game_id"], r["position"]) for r in rows]


# --- player pages (web) ----------------------------------------------------------------------------

# A rare moment: one move matching at least one of these, counted once however many labels it carries. Missed mates
# in one are mistakes, not rare moments; engine labels aren't detectors (and not every player has them).
RARE_MOMENT_TYPES = [d.id for d in DETECTORS if d.id != "MISSED_MATE_IN_ONE"]
# Named mates count in these forms only; variants are deliberately loose while they're under review.
RARE_MOMENT_NAMED_TYPES = [d.id for d in NAMED_MATES]
RARE_MOMENT_FORMS = ["textbook", "characteristic"]

def player_summary(conn: psycopg.Connection, platform: str, username: str) -> dict:
    """Everything a player page needs in one round of queries. Motif counts are split by who played the
    move: the player (`mine`) or their opponents (`against`)."""
    params = {"platform": platform, "user": username}
    mine = """split_part(g.source_key, ':', 1) = %(platform)s
              AND (lower(g.white) = lower(%(user)s) OR lower(g.black) = lower(%(user)s))"""
    totals = conn.execute(
        f"""WITH g AS (SELECT g.*, CASE WHEN lower(g.white) = lower(%(user)s) THEN 'w' ELSE 'b' END AS me
                       FROM games g WHERE {mine})
            SELECT count(*) AS games, coalesce(sum(ply_count + 1), 0) AS positions,
                   min(played_at) AS first_game, max(played_at) AS last_game,
                   count(*) FILTER (WHERE (me = 'w' AND result = '1-0') OR (me = 'b' AND result = '0-1')) AS wins,
                   count(*) FILTER (WHERE result = '1/2-1/2') AS draws,
                   count(*) FILTER (WHERE (me = 'w' AND result = '0-1') OR (me = 'b' AND result = '1-0')) AS losses,
                   (SELECT CASE WHEN me = 'w' THEN white_rating ELSE black_rating END
                    FROM g ORDER BY played_at DESC NULLS LAST LIMIT 1) AS rating,
                   (SELECT CASE WHEN me = 'w' THEN white ELSE black END
                    FROM g ORDER BY played_at DESC NULLS LAST LIMIT 1) AS display_name
            FROM g""",
        params,
    ).fetchone()
    motifs = conn.execute(
        f"""SELECT e.type,
                   count(*) FILTER (WHERE lower(CASE e.color WHEN 'w' THEN g.white ELSE g.black END) = lower(%(user)s)) AS mine,
                   count(*) FILTER (WHERE lower(CASE e.color WHEN 'w' THEN g.white ELSE g.black END) <> lower(%(user)s)) AS against,
                   -- named mates only: the player's own, by form (textbook / characteristic / variant)
                   jsonb_strip_nulls(jsonb_build_object(
                       'textbook', nullif(count(*) FILTER (WHERE e.metadata->>'form' = 'textbook' AND lower(CASE e.color WHEN 'w' THEN g.white ELSE g.black END) = lower(%(user)s)), 0),
                       'characteristic', nullif(count(*) FILTER (WHERE e.metadata->>'form' = 'characteristic' AND lower(CASE e.color WHEN 'w' THEN g.white ELSE g.black END) = lower(%(user)s)), 0),
                       'variant', nullif(count(*) FILTER (WHERE e.metadata->>'form' = 'variant' AND lower(CASE e.color WHEN 'w' THEN g.white ELSE g.black END) = lower(%(user)s)), 0)
                   )) AS forms
            FROM events e JOIN games g ON g.id = e.game_id WHERE {mine}
            GROUP BY e.type ORDER BY e.type""",
        params,
    ).fetchall()
    # the hero's totals: distinct qualifying moves per side, and the labels on them by type (for its examples)
    rare = conn.execute(
        f"""WITH x AS (
                SELECT e.game_id, e.ply, e.type,
                       lower(CASE e.color WHEN 'w' THEN g.white ELSE g.black END) = lower(%(user)s) AS mine
                FROM events e JOIN games g ON g.id = e.game_id
                WHERE {mine} AND e.type = ANY(%(types)s)
                  AND (e.type <> ALL(%(named_types)s) OR e.metadata->>'form' = ANY(%(forms)s)))
            SELECT (SELECT count(DISTINCT (game_id, ply)) FROM x WHERE mine) AS mine,
                   (SELECT count(DISTINCT (game_id, ply)) FROM x WHERE NOT mine) AS against,
                   coalesce((SELECT jsonb_agg(t ORDER BY t.type) FROM (
                       SELECT type, count(*) FILTER (WHERE mine) AS mine, count(*) FILTER (WHERE NOT mine) AS against
                       FROM x GROUP BY type) t), '[]') AS types""",
        {**params, "types": RARE_MOMENT_TYPES, "named_types": RARE_MOMENT_NAMED_TYPES,
         "forms": RARE_MOMENT_FORMS},
    ).fetchone()
    # the platform's own current rating beats the rating on whichever game happens to be newest so far
    current = conn.execute(
        """SELECT player_rating, rating_mode FROM imports WHERE source = %(platform)s AND source_ref = lower(%(user)s)
           AND player_rating IS NOT NULL ORDER BY id DESC LIMIT 1""", params).fetchone()
    if current:
        totals = {**totals, "rating": current["player_rating"], "rating_mode": current["rating_mode"]}
    config = default_engine_config(conn)
    engine = None
    if config:
        engine = conn.execute(
            f"""SELECT count(s.game_id) AS games_analyzed, coalesce(sum(s.positions), 0) AS positions_analyzed
                FROM games g LEFT JOIN engine_game_status s ON s.game_id = g.id AND s.config_id = %(config)s
                WHERE {mine}""",
            {**params, "config": config["id"]},
        ).fetchone() | {"config": {k: config[k] for k in ("id", "engine_name", "limit_kind", "limit_value")}}
    latest_import = conn.execute(
        "SELECT * FROM imports WHERE source = %s AND source_ref = %s ORDER BY id DESC LIMIT 1",
        (platform, username.lower()),
    ).fetchone()
    return {"platform": platform, "username": username.lower(), **totals, "motifs": motifs, "rare_moments": rare,
            "engine": engine, "latest_import": latest_import}


def engine_input(conn: psycopg.Connection, platform: str, username: str) -> list[dict]:
    """A player's games in the compact form the browser engine needs, newest first. Per game, the moves
    as space-joined strings (uci, san) and per-ply strings/arrays: captured and promotion letters ('.' for
    none), flags (bit 1 check, 2 checkmate, 4 castling), material after each ply, legal moves before each
    ply. `events`: [ply, type] of the game's deterministic events (the browser's priority queue)."""
    return conn.execute(
        """SELECT g.id, g.played_at, g.white, g.black, g.result, g.initial_fen, g.chess960, g.ply_count,
                  g.external_id, g.source_key,
                  substring(g.pgn from '\\[Termination "([^"]*)"\\]') AS termination,
                  gm.uci, gm.san, gm.captured, gm.promotion,
                  -- per ply: 1 check, 2 checkmate, 4 castling, one digit each (en passant isn't sent)
                  array_to_string(ARRAY(SELECT f & 7 FROM unnest(gm.flags) WITH ORDINALITY AS u(f, i) ORDER BY i), '') AS flags,
                  gm.material_white AS mw, gm.material_black AS mb, gm.legal_moves_before AS legal,
                  (SELECT coalesce(json_agg(json_build_array(e.ply, e.type) ORDER BY e.ply), '[]')
                   FROM events e WHERE e.game_id = g.id) AS events
           FROM games g JOIN game_moves gm ON gm.game_id = g.id
           WHERE split_part(g.source_key, ':', 1) = %(platform)s
             AND (lower(g.white) = lower(%(user)s) OR lower(g.black) = lower(%(user)s))
           ORDER BY g.played_at DESC NULLS LAST, g.id DESC""",
        {"platform": platform, "user": username},
    ).fetchall()


def unusual_move_candidates(conn: psycopg.Connection, config_id: int, player: str | None,
                            low: float = 0.10, high: float = 0.90) -> list[tuple[int, int]]:
    """(game_id, position) for the unusual-move discovery's two-line search: the mover played the
    engine's choice with more than one legal move, it isn't a recapture or a mate on the board, and the
    game was undecided (lichess expected score in [low, high); clearly winning positions are
    only_winning_move_candidates). No top_two probe yet."""
    li = _expected("lichess", "m.color", "b.score_cp", "b.mate", "b.wdl")
    rows = conn.execute(
        f"""SELECT m.game_id, m.ply - 1 AS position
            FROM games g
            CROSS JOIN LATERAL ({_moves_with_neighbours("g.id")}) m
            JOIN engine_positions b ON b.config_id = %(config)s AND b.game_id = m.game_id AND b.position = m.ply - 1
            WHERE m.uci = b.best_uci AND m.legal_moves_before > 1 AND NOT m.is_checkmate
              AND NOT (m.captured IS NOT NULL AND m.prev_captured IS NOT NULL AND substr(m.uci, 3, 2) = substr(m.prev_uci, 3, 2))
              AND {li} >= %(low)s AND {li} < %(high)s
              AND (%(player)s::text IS NULL
                   OR lower(CASE m.color WHEN 'w' THEN g.white ELSE g.black END) = lower(%(player)s))
              AND NOT EXISTS (SELECT 1 FROM engine_move_probes p WHERE p.config_id = %(config)s
                              AND p.game_id = m.game_id AND p.position = m.ply - 1 AND p.kind = 'top_two')
            ORDER BY m.game_id, m.ply""",
        {"config": config_id, "player": player, "low": low, "high": high},
    ).fetchall()
    return [(r["game_id"], r["position"]) for r in rows]


def game_engine_positions(conn: psycopg.Connection, game_id: int, config_id: int) -> list[dict]:
    return conn.execute(
        """SELECT position, score_cp, mate, wdl, best_uci, depth FROM engine_positions
           WHERE config_id = %s AND game_id = %s ORDER BY position""",
        (config_id, game_id),
    ).fetchall()


# --- engine archaeology: discoveries derived at query time (see archaeology.py) -----------------------

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def _pov_expected(scale: str, pov: str, stm: str, cp: str, mate: str, wdl: str) -> str:
    """SQL: `pov`'s expected score (0..1) at a stored position where `stm` is to move. Unlike _expected,
    which is for the mover right after its move, mate = 0 here depends on who is mated."""
    return f"CASE WHEN {mate} = 0 THEN CASE WHEN {stm} = {pov} THEN 0.0 ELSE 1.0 END ELSE {_expected(scale, pov, cp, mate, wdl)} END"


def _pov_ordinal(pov: str, stm: str, cp: str, mate: str) -> str:
    """SQL: a total order on evaluations from `pov`'s side, for ranking and tie-breaks where expected
    scores tie (every forced mate is 1.0 or 0.0): mate delivered > mate in 1 > mate in 2 > ... > any
    centipawn score > ... > mated in 2 > mated in 1 > mated."""
    return (f"CASE WHEN {mate} = 0 THEN CASE WHEN {stm} = {pov} THEN -100000 ELSE 100000 END "
            f"WHEN {mate} IS NOT NULL THEN CASE WHEN ({mate} > 0) = ({pov} = 'w') THEN 100000 - abs({mate}) "
            f"ELSE -100000 + abs({mate}) END "
            f"ELSE CASE WHEN {pov} = 'w' THEN {cp} ELSE -{cp} END END")


def _piece_value(letter: str) -> str:
    return f"CASE {letter} WHEN 'P' THEN 1 WHEN 'N' THEN 3 WHEN 'B' THEN 3 WHEN 'R' THEN 5 WHEN 'Q' THEN 9 ELSE 0 END"


def _other(color: str) -> str:
    return f"CASE WHEN {color} = 'w' THEN 'b' ELSE 'w' END"


def _player_games(alias: str = "g") -> str:
    """SQL condition: the game belongs to %(player)s on %(platform)s (either may be NULL = any)."""
    return (f"(%(platform)s::text IS NULL OR split_part({alias}.source_key, ':', 1) = %(platform)s) "
            f"AND (%(player)s::text IS NULL OR lower({alias}.white) = lower(%(player)s) OR lower({alias}.black) = lower(%(player)s))")


def _moves_with_neighbours(game_id: str) -> str:
    """SQL: one game's plies (the moves view), each with the previous ply's facts (prev_*) and the game's last
    ply (last_*)."""
    return f"""SELECT mm.*, lag(mm.captured) OVER w AS prev_captured, lag(mm.uci) OVER w AS prev_uci,
                      lag(mm.is_check) OVER w AS prev_is_check, lag(mm.material_white) OVER w AS prev_material_white,
                      lag(mm.material_black) OVER w AS prev_material_black,
                      last_value(mm.is_checkmate) OVER whole AS last_is_checkmate, last_value(mm.color) OVER whole AS last_color
               FROM moves mm WHERE mm.game_id = {game_id}
               WINDOW w AS (ORDER BY mm.ply),
                      whole AS (ORDER BY mm.ply ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)"""


def archaeology_moves_sql(scale: str) -> str:
    """One row per move of the player's games with the engine's view before and after it, all from the
    MOVER's side: evaluations (cp/mate, White's POV as stored, plus mover-POV expected score and ordinal),
    the engine's choice, material balance, and a material window over the next plies for sacrifices.
    Params: %(config)s, %(player)s (required), %(platform)s. Only the player's own moves come out;
    the windows still see every ply (the opponent's replies are what a sacrifice window measures)."""
    stm_after = _other("r.color")
    wb = "(m.material_white - m.material_black)"  # after this ply, White's side
    # Expected scores and ordinals are computed after the player filter: the windows below stop
    # Postgres pushing the filter down, and computing them for the opponent's half too cost ~1 s.
    return f"""
      SELECT r.*,
             {_expected(scale, 'r.color', 'r.cp_before', 'r.mate_before', 'r.wdl_before')} AS exp_before,
             {_pov_expected(scale, 'r.color', stm_after, 'r.cp_after', 'r.mate_after', 'r.wdl_after')} AS exp_after,
             {_pov_ordinal('r.color', 'r.color', 'r.cp_before', 'r.mate_before')} AS ord_before,
             {_pov_ordinal('r.color', stm_after, 'r.cp_after', 'r.mate_after')} AS ord_after,
             -- forced mate from the mover's side: > 0 mover mates in n, < 0 mover is mated in n
             CASE WHEN r.mate_before IS NULL THEN NULL WHEN r.color = 'w' THEN r.mate_before ELSE -r.mate_before END AS mover_mate_before,
             CASE WHEN r.mate_after IS NULL THEN NULL WHEN r.mate_after = 0 THEN 0
                  WHEN r.color = 'w' THEN r.mate_after ELSE -r.mate_after END AS mover_mate_after
      FROM (
        SELECT m.game_id, m.ply, m.color, m.san, m.uci, m.piece, m.captured, m.promotion, m.is_check,
               m.is_checkmate, m.is_castling, m.legal_moves_before,
               b.score_cp AS cp_before, b.mate AS mate_before, b.wdl AS wdl_before,
               a.score_cp AS cp_after, a.mate AS mate_after, a.wdl AS wdl_after,
               b.best_uci AS engine_choice, b.depth AS depth_before,
               (m.captured IS NOT NULL AND m.prev_captured IS NOT NULL
                AND substr(m.uci, 3, 2) = substr(m.prev_uci, 3, 2)) AS is_recapture,
               coalesce(m.prev_is_check, false) AS in_check_before,
               CASE m.color WHEN 'w' THEN m.prev_material_black ELSE m.prev_material_white END AS opp_material_before,
               -- material balance from the mover's side (P1 N3 B3 R5 Q9). Before the move = after it, less
               -- what it captured and what a promotion gained: exact at every ply, set-up starts included
               CASE m.color WHEN 'w' THEN 1 ELSE -1 END * {wb}
                 - {_piece_value('m.captured')} - CASE WHEN m.promotion IS NULL THEN 0 ELSE {_piece_value('m.promotion')} - 1 END
                 AS balance_before,
               CASE m.color WHEN 'w' THEN 1 ELSE -1 END * {wb} AS balance_after,
               -- the next five plies (reply, and two more moves each), White's side: a sacrifice must hold
               -- GREATEST/LEAST of five leads: a sliding max/min frame is recomputed per row (5 s)
               GREATEST({", ".join(f"lead({wb}, {k}) OVER plies" for k in range(1, 6))}) AS wb_max_next5,
               LEAST({", ".join(f"lead({wb}, {k}) OVER plies" for k in range(1, 6))}) AS wb_min_next5,
               -- to the end of the game; a reverse running window is linear (a frame ending at
               -- UNBOUNDED FOLLOWING is recomputed per row: quadratic, 4 s on a 3,700-game history)
               max({wb}) OVER rest AS wb_max_rest, min({wb}) OVER rest AS wb_min_rest,
               lead(m.captured) OVER plies AS reply_captured, lead(m.san) OVER plies AS reply_san,
               g.ply_count AS last_ply, m.last_is_checkmate AS game_ends_in_mate, m.last_color AS last_mover,
               g.played_at, g.white, g.black, g.result, g.source_key, g.external_id,
               split_part(g.source_key, ':', 1) AS platform,
               CASE m.color WHEN 'w' THEN g.white ELSE g.black END AS mover,
               CASE m.color WHEN 'w' THEN g.black ELSE g.white END AS opponent,
               g.termination
        FROM ({_player_games_narrow()}) g
        -- each game's moves unpacked once, with the previous ply and the game's last ply alongside (joining
        -- the moves view to itself would unpack the game again for every move)
        CROSS JOIN LATERAL ({_moves_with_neighbours("g.id")}) m
        JOIN engine_positions b ON b.config_id = %(config)s AND b.game_id = m.game_id AND b.position = m.ply - 1
        JOIN engine_positions a ON a.config_id = %(config)s AND a.game_id = m.game_id AND a.position = m.ply
        WINDOW plies AS (PARTITION BY m.game_id ORDER BY m.ply),
               rest AS (PARTITION BY m.game_id ORDER BY m.ply DESC ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
      ) r
      WHERE lower(r.mover) = lower(%(player)s)"""


def _player_games_narrow() -> str:
    """The player's games, only the columns archaeology shows (the raw PGN stays behind: dragging it
    through window sorts cost seconds), with how the game ended from the PGN's Termination tag."""
    return f"""SELECT g.id, g.played_at, g.white, g.black, g.result, g.source_key, g.external_id, g.initial_fen,
                      g.ply_count, substring(g.pgn from '\\[Termination "([^"]*)"\\]') AS termination
               FROM games g WHERE {_player_games()}
               -- a fence: evaluate the PGN regex once per game, not once per move after flattening
               OFFSET 0
               """


def archaeology_positions_sql(scale: str) -> str:
    """One row per stored position of the player's games, from the PLAYER's side (%(player)s is
    required): expected score, ordinal, and `trusted`. A position is untrusted when the engine
    contradicted itself one ply later: its own recommended move was played next and the evaluation
    moved by more than 0.30 expected score. A comeback 'from -8' must not rest on such a number."""
    pov = "CASE WHEN lower(g.white) = lower(%(player)s) THEN 'w' ELSE 'b' END"
    stm = ("CASE WHEN (mod(p.position, 2) = 0) = (split_part(coalesce(g.initial_fen, 'x w'), ' ', 2) = 'w') "
           "THEN 'w' ELSE 'b' END")
    return f"""
        SELECT *, NOT (next_uci IS NOT NULL AND next_uci = best_uci
                       AND abs(next_exp - exp) > 0.30) AS trusted
        FROM (
          SELECT p.game_id, p.position, p.score_cp, p.mate, p.best_uci, pov, stm,
                 {_pov_expected(scale, 'pov', 'stm', 'p.score_cp', 'p.mate', 'p.wdl')} AS exp,
                 {_pov_ordinal('pov', 'stm', 'p.score_cp', 'p.mate')} AS ord,
                 lead({_pov_expected(scale, 'pov', 'stm', 'p.score_cp', 'p.mate', 'p.wdl')})
                   OVER (PARTITION BY p.game_id ORDER BY p.position) AS next_exp,
                 nullif(split_part(nm.uci, ' ', p.position + 1), '') AS next_uci
          FROM (SELECT g.*, {pov} AS pov FROM games g WHERE {_player_games()} AND %(player)s::text IS NOT NULL) g
          JOIN engine_positions p ON p.config_id = %(config)s AND p.game_id = g.id
          CROSS JOIN LATERAL (SELECT {stm} AS stm) s
          LEFT JOIN game_moves nm ON nm.game_id = p.game_id
        ) x"""
