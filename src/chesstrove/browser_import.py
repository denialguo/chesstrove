"""Server side of browser indexing: the visitor's browser runs chesstrove.indexing (Pyodide, web/src/indexer) and
uploads the rows in batches; this module checks them and stores them exactly as the server importer would.

The browser is an untrusted client, so every batch is checked before anything is written:
  - the session: a running browser import, its secret token, and the detector versions this server runs;
  - every game: the platform, a source key of the right shape, the indexed player on one side, required columns
    present and bounded, packed move arrays exactly `ply_count` long with values in range;
  - every event: a detector this server knows at its current version, a ply inside the game, the color of the
    side that played that ply, bounded metadata;
  - every game carrying a rare-moment event (anything but a missed mate in one: ~11% of real games) is replayed
    here from its own PGN, plus one other game at random per batch, and the batch is refused unless the columns,
    moves and events match exactly. So every rare moment a page shows has been derived on this server.

What that leaves (a deliberate trade for not replaying every game on a 0.1-CPU server): an unsampled game could
carry moves or missed-mate events that don't match its PGN, or leave events out; and the PGN itself is whatever
the client sent, so an invented game consistent with its own invented PGN is only limited by the checks above
(the right player, a Chess.com id, sane values). Every stored game keeps its raw PGN, so all of it can be
re-derived and checked later with the same code.
"""

import hashlib
import hmac
import json
import random
import re
import secrets
from datetime import UTC, datetime, timedelta
from collections.abc import Callable
from typing import Any

import psycopg

from chesstrove import db, indexing
from chesstrove.importers.pgn import ParseFailure, read_one

MAX_GAMES = 150  # per batch; ~400 KB of JSON
UNCHECKED_TYPES = {"MISSED_MATE_IN_ONE"}  # a mistake, not a rare moment: sampled like games without events
IDLE = timedelta(minutes=3)  # a browser import with no batch for this long is abandoned (tab closed, paused)
FRESH = timedelta(minutes=10)

VERSIONS = indexing.versions(indexing.FAST)  # what a first-pass batch was analysed with
DETECTOR_IDS = set(VERSIONS)
DEEP_VERSIONS = indexing.versions(indexing.DEEP)
UCI = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?$")
MONTH = re.compile(r"^\d{4}/(0[1-9]|1[0-2])$")
RESULTS = {"1-0", "0-1", "1/2-1/2", "*"}
PACKED_TEXT = {"piece": "PNBRQK", "captured": "PNBRQ.", "promotion": "NBRQ."}
PACKED_ARRAYS = {"flags": (0, 15), "material_white": (0, 200), "material_black": (0, 200), "queens_after": (0, 20),
                 "legal_moves_before": (0, 300)}


class Rejected(ValueError):
    """The batch breaks a rule; nothing from it was stored. `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- sessions --------------------------------------------------------------------------------------------

def start(conn: psycopg.Connection, username: str, allow: Callable[[], bool] = lambda: True,
          resume: tuple[int, str] | None = None) -> dict[str, Any]:
    """Start or adopt indexing for a Chess.com account. `mode`: `index` (this browser indexes: token, months already
    stored, detector versions), `watch` (another browser or the server is on it), or `done` (finished moments ago).
    `resume` = (import id, token) from this browser's earlier session: its holder carries on with the same import."""
    username = username.lower()
    now = datetime.now(UTC)
    latest = db.latest_import(conn, "chesscom", username)
    if latest and latest["status"] == "running":
        state = latest["resume_state"] or {}
        if resume and resume[0] == latest["id"] and state.get("client") == "browser" \
                and hmac.compare_digest(state.get("token_sha256", ""), _hash(resume[1])):
            db.touch_browser_import(conn, latest["id"])
            return {"mode": "index", "import_id": latest["id"], "token": resume[1],
                    "months_done": sorted(db.chesscom_months_done(conn, username)), "versions": VERSIONS, "max_games": MAX_GAMES}
        last_seen = datetime.fromisoformat(state["last_seen"]) if state.get("last_seen") else latest["started_at"]
        if state.get("client") != "browser" or now - last_seen < IDLE:
            return {"mode": "watch", "import_id": latest["id"]}
        db.record_progress(conn, latest["id"], errors=[{"error": "paused: the indexing tab stopped sending"}])
        db.finish_import(conn, latest["id"], "failed")
        db.finish_analysis_run(conn, state["run_id"], "failed")
    elif latest and latest["status"] == "completed" and latest["finished_at"] > now - FRESH:
        return {"mode": "done", "import_id": latest["id"]}
    if not allow():
        raise Rejected("too many imports from here; try again in an hour", 429)
    account_id = db.ensure_account(conn, "me", "chesscom", username)
    months_done = sorted(db.chesscom_months_done(conn, username))
    import_id = db.start_import(conn, "chesscom", username, account_id)
    run_id = db.start_analysis_run(conn, VERSIONS)
    token = secrets.token_urlsafe(24)
    db.set_resume_state(conn, import_id, {"client": "browser", "months_done": months_done, "run_id": run_id,
                                          "token_sha256": _hash(token), "last_seen": now.isoformat()})
    return {"mode": "index", "import_id": import_id, "token": token, "months_done": months_done,
            "versions": VERSIONS, "max_games": MAX_GAMES}


def _session(conn: psycopg.Connection, import_id: int, token: str) -> dict:
    imp = db.get_import(conn, import_id)
    state = (imp or {}).get("resume_state") or {}
    if not imp or state.get("client") != "browser" or not hmac.compare_digest(state.get("token_sha256", ""), _hash(token)):
        raise Rejected("not your import", 403)
    if imp["status"] != "running":
        raise Rejected("this import has ended; start again", 409)
    return imp


def finish(conn: psycopg.Connection, import_id: int, token: str) -> None:
    imp = _session(conn, import_id, token)
    db.finish_import(conn, import_id, "completed")
    db.finish_analysis_run(conn, imp["resume_state"]["run_id"], "completed")


# --- validation ------------------------------------------------------------------------------------------

def _text(g: dict, key: str, limit: int, optional: bool = True) -> None:
    v = g.get(key)
    if v is None and optional:
        return
    if not isinstance(v, str) or len(v) > limit:
        raise Rejected(f"{key}: expected text up to {limit} characters")


def _int(v: Any, lo: int, hi: int, what: str, optional: bool = False) -> None:
    if v is None and optional:
        return
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise Rejected(f"{what}: expected a whole number from {lo} to {hi}")


def validate_game(g: Any, username: str) -> None:
    """Raises Rejected unless `g` is shaped exactly like indexing.index_game's output for this player's game."""
    if not isinstance(g, dict) or set(g) != {*indexing.GAME_FIELDS, "ply_count", "moves", "events"}:
        raise Rejected("a game must have exactly the indexed fields")
    if g["source"] != "chesscom" or not isinstance(g["external_id"], str) or not g["external_id"].isdigit() \
            or len(g["external_id"]) > 20 or g["source_key"] != f"chesscom:{g['external_id']}":
        raise Rejected("source: expected a Chess.com game (chesscom:<id>)")
    for key in ("white", "black"):
        _text(g, key, 50, optional=False)
    if username not in (g["white"].lower(), g["black"].lower()):
        raise Rejected(f"{g['source_key']}: {username} didn't play in it")
    if g["result"] not in RESULTS:
        raise Rejected("result: expected 1-0, 0-1, 1/2-1/2 or *")
    for key, limit in (("time_control", 30), ("eco", 10), ("opening", 300), ("initial_fen", 100)):
        _text(g, key, limit)
    _text(g, "pgn", 200_000, optional=False)
    _int(g["white_rating"], 0, 5000, "white_rating", optional=True)
    _int(g["black_rating"], 0, 5000, "black_rating", optional=True)
    for key in ("rated", "chess960"):
        if not (isinstance(g[key], bool) or g[key] is None and key == "rated"):
            raise Rejected(f"{key}: expected true or false")
    if g["played_at"] is not None:
        try:
            datetime.fromisoformat(g["played_at"])
        except (TypeError, ValueError):
            raise Rejected("played_at: expected an ISO date and time") from None
    n = g["ply_count"]
    _int(n, 0, 2000, "ply_count")
    _validate_moves(g["moves"], n)
    first = g["moves"]["first_color"] if g["moves"] else "w"
    if not isinstance(g["events"], list) or len(g["events"]) > 10 * max(n, 1):
        raise Rejected("events: expected a list")
    for e in g["events"]:
        _validate_event(e, n, first)


def _validate_moves(m: Any, n: int) -> None:
    if n == 0:
        if m is not None:
            raise Rejected("moves: a game without moves has none")
        return
    if not isinstance(m, dict) or set(m) != set(db.GAME_MOVES_COLUMNS.split(", ")[1:]):
        raise Rejected("moves: expected the packed columns")
    if m["first_color"] not in ("w", "b"):
        raise Rejected("moves.first_color: expected w or b")
    for key in ("uci", "san"):
        if not isinstance(m[key], str) or len(m[key].split(" ")) != n:
            raise Rejected(f"moves.{key}: expected ply_count moves")
    if not all(UCI.match(u) for u in m["uci"].split(" ")) or not all(0 < len(s) <= 10 for s in m["san"].split(" ")):
        raise Rejected("moves: malformed move")
    for key, allowed in PACKED_TEXT.items():
        if not isinstance(m[key], str) or len(m[key]) != n or not set(m[key]) <= set(allowed):
            raise Rejected(f"moves.{key}: expected ply_count of {allowed}")
    for key, (lo, hi) in PACKED_ARRAYS.items():
        if not isinstance(m[key], list) or len(m[key]) != n:
            raise Rejected(f"moves.{key}: expected ply_count values")
        for v in m[key]:
            _int(v, lo, hi, f"moves.{key}")


def _validate_event(e: Any, n: int, first: str, versions: dict[str, int] = VERSIONS) -> None:
    if not isinstance(e, dict) or set(e) != {"detector_id", "detector_version", "ply", "type", "color", "fen", "metadata"}:
        raise Rejected("event: expected the event columns")
    if e["detector_id"] not in versions or e["detector_version"] != versions[e["detector_id"]] \
            or e["type"] != e["detector_id"]:
        raise Rejected(f"event: unknown detector or version ({e['detector_id']} v{e['detector_version']})")
    _int(e["ply"], 1, n, "event.ply")
    mover = first if e["ply"] % 2 == 1 else ("b" if first == "w" else "w")
    if e["color"] != mover:
        raise Rejected("event.color: not the side that played that ply")
    if not isinstance(e["fen"], str) or len(e["fen"]) > 100:
        raise Rejected("event.fen: expected a position")
    if not isinstance(e["metadata"], dict) or len(json.dumps(e["metadata"])) > 4000:
        raise Rejected("event.metadata: expected a small object")


def verify(g: dict) -> bool:
    """Re-derive one game from its PGN the way the server importer does and compare everything derived."""
    item = read_one(g["pgn"], "chesscom", "")
    if isinstance(item, ParseFailure):
        return False
    mine = indexing.index_game(item)
    # rated and the opening name come from Chess.com's JSON, not the PGN
    return all(mine[k] == g[k] for k in mine if k not in ("rated", "opening"))


# --- batches ---------------------------------------------------------------------------------------------

def store(conn: psycopg.Connection, import_id: int, token: str, body: Any) -> dict[str, int]:
    """Check a batch (see the module docstring) and store it in one transaction. Idempotent: games already stored
    (by anyone) count as duplicates. `month`, once `month_complete`, is recorded as done unless it's the current
    month (still being played), so a later session skips it."""
    imp = _session(conn, import_id, token)
    if not isinstance(body, dict) or set(body) - {"versions", "month", "month_complete", "games", "skipped", "errors"}:
        raise Rejected("expected a batch")
    if body.get("versions") != VERSIONS:
        raise Rejected("this page's analyzer is out of date; reload the page", 409)
    games, month = body.get("games"), body.get("month")
    if not isinstance(games, list) or len(games) > MAX_GAMES:
        raise Rejected(f"games: expected up to {MAX_GAMES}")
    if month is not None and (not isinstance(month, str) or not MONTH.match(month)):
        raise Rejected("month: expected YYYY/MM")
    _int(body.get("skipped", 0), 0, 100_000, "skipped")
    errors = body.get("errors", [])
    if not isinstance(errors, list) or len(errors) > 1000 or len(json.dumps(errors)) > 50_000:
        raise Rejected("errors: expected a short list")
    username = imp["source_ref"]
    keys = set()
    for g in games:
        validate_game(g, username)
        if g["source_key"] in keys:
            raise Rejected(f"{g['source_key']} appears twice in the batch")
        keys.add(g["source_key"])
    rare = [g for g in games if any(e["type"] not in UNCHECKED_TYPES for e in g["events"])]
    rest = [g for g in games if g not in rare]
    for g in rare + random.sample(rest, min(1, len(rest))):
        if not verify(g):
            raise Rejected(f"{g['source_key']} doesn't match its own PGN")

    state = imp["resume_state"]
    with conn.transaction():
        known = db.existing_game_keys(conn, list(keys))
        new = [g for g in games if g["source_key"] not in known]
        ids = db.insert_games(conn, [indexing.game_from_row(g) for g in new], import_id)
        stored = [g for g in new if g["source_key"] in ids]
        db.insert_moves(conn, [(ids[g["source_key"]], g["moves"]) for g in stored])
        db.insert_events(conn, state["run_id"], [(ids[g["source_key"]], g["events"]) for g in stored])
        db.mark_analyzed(conn, list(ids.values()), VERSIONS)
        events = sum(len(g["events"]) for g in stored)
        db.record_progress(conn, import_id, len(games) + body.get("skipped", 0) + len(errors), len(stored),
                           len(games) - len(stored), len(errors), [{"month": month, **e} for e in errors if isinstance(e, dict)],
                           body.get("skipped", 0))
        db.record_run_progress(conn, state["run_id"], len(stored), events)
        done = month if body.get("month_complete") and month and month < datetime.now(UTC).strftime("%Y/%m") else None
        db.touch_browser_import(conn, import_id, done)
    return {"stored": len(stored), "duplicate": len(games) - len(stored), "events": events}


# --- the deep pass: DEEP detectors over games already stored (browser: indexing.deep_scan) ---------------------------

DEEP_GAMES = 100  # per request, both ways


def start_deep(conn: psycopg.Connection, username: str, resume: tuple[int, str] | None = None) -> dict[str, Any]:
    """A deep-pass session for a Chess.com player's stored games: `index` (token, games pending), `watch` (another
    browser is on it), or `done` (nothing pending). Tracked on its own analysis_runs row, so the player's import
    stays finished while this runs."""
    username = username.lower()
    pending = db.deep_pending(conn, "chesscom", username, DEEP_VERSIONS)
    if not pending:
        return {"mode": "done", "pending": 0}
    now = datetime.now(UTC)
    active = db.running_deep_session(conn, "chesscom", username)
    if active:
        s = active["session"]
        if resume and resume[0] == active["id"] and hmac.compare_digest(s["token_sha256"], _hash(resume[1])):
            return {"mode": "index", "run_id": active["id"], "token": resume[1], "pending": pending, "versions": DEEP_VERSIONS}
        if now - datetime.fromisoformat(s["last_seen"]) < IDLE:
            return {"mode": "watch", "run_id": active["id"], "pending": pending}
        db.finish_analysis_run(conn, active["id"], "failed")
    token = secrets.token_urlsafe(24)
    run_id = db.start_analysis_run(conn, DEEP_VERSIONS)
    db.set_run_session(conn, run_id, {"client": "browser", "platform": "chesscom", "username": username,
                                      "token_sha256": _hash(token), "last_seen": now.isoformat()})
    return {"mode": "index", "run_id": run_id, "token": token, "pending": pending, "versions": DEEP_VERSIONS}


def _deep_session(conn: psycopg.Connection, run_id: int, token: str) -> dict:
    run = db.get_analysis_run(conn, run_id)
    s = (run or {}).get("session") or {}
    if not run or not hmac.compare_digest(s.get("token_sha256", ""), _hash(token)):
        raise Rejected("not your scan", 403)
    if run["status"] != "running":
        raise Rejected("this scan has ended; start again", 409)
    return run


def deep_games(conn: psycopg.Connection, run_id: int, token: str) -> list[dict]:
    """The next games still needing the deep pass, newest first: what indexing.deep_scan takes."""
    s = _deep_session(conn, run_id, token)["session"]
    return db.deep_pending_games(conn, s["platform"], s["username"], DEEP_VERSIONS, DEEP_GAMES)


def store_deep(conn: psycopg.Connection, run_id: int, token: str, body: Any) -> dict[str, int]:
    """Deep-pass results for stored games. Same rules as first-pass batches: known detectors at this server's
    versions, plies inside the game, the mover's color, bounded metadata; one game per batch is replayed here and
    must match. Rerunning a game replaces its deep events instead of adding to them."""
    run = _deep_session(conn, run_id, token)
    s = run["session"]
    if not isinstance(body, dict) or body.get("versions") != DEEP_VERSIONS:
        raise Rejected("this page's analyzer is out of date; reload the page", 409)
    games = body.get("games")
    if not isinstance(games, list) or len(games) > DEEP_GAMES:
        raise Rejected(f"games: expected up to {DEEP_GAMES}")
    keys = [g.get("source_key") if isinstance(g, dict) else None for g in games]
    if len(set(keys)) != len(keys) or not all(isinstance(k, str) for k in keys):
        raise Rejected("games: expected distinct source keys")
    stored = {r["source_key"]: r for r in db.deep_game_rows(conn, keys)}
    for g in games:
        row = stored.get(g["source_key"])
        if row is None or s["username"] not in (row["white"].lower(), row["black"].lower()) \
                or not row["source_key"].startswith(s["platform"] + ":"):
            raise Rejected(f"{g['source_key']}: not one of this player's stored games")
        if set(g) != {"source_key", "events"} or not isinstance(g["events"], list) or len(g["events"]) > 10 * max(row["ply_count"], 1):
            raise Rejected("a game must have exactly source_key and events")
        for e in g["events"]:
            _validate_event(e, row["ply_count"], row["first_color"] or "w", DEEP_VERSIONS)
    if games:
        g = random.choice(games)
        row = stored[g["source_key"]]
        mine = json.loads(indexing.deep_scan(json.dumps([{k: row[k] for k in ("source_key", "initial_fen", "chess960", "uci")}])))
        if mine[0]["events"] != g["events"]:
            raise Rejected(f"{g['source_key']}: deep events don't match its moves")
    ids = [stored[g["source_key"]]["id"] for g in games]
    with conn.transaction():
        db.delete_events(conn, ids, list(DEEP_VERSIONS))
        db.insert_events(conn, run_id, [(stored[g["source_key"]]["id"], g["events"]) for g in games])
        db.mark_analyzed(conn, ids, DEEP_VERSIONS)
        events = sum(len(g["events"]) for g in games)
        db.record_run_progress(conn, run_id, len(games), events)
        db.touch_run_session(conn, run_id)
    return {"games": len(games), "events": events}


def finish_deep(conn: psycopg.Connection, run_id: int, token: str) -> None:
    _deep_session(conn, run_id, token)
    db.finish_analysis_run(conn, run_id, "completed")
