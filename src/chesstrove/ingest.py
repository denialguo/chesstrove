"""Import pipeline: source -> CanonicalGame -> one-pass replay -> games + moves rows."""

import itertools
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg

from chesstrove import db
from chesstrove.analysis import Run, analyze, tracked_run
from chesstrove.importers import chesscom
from chesstrove.importers.pgn import ParseFailure, read_pgn
from chesstrove.models import CanonicalGame

BATCH_SIZE = 500  # games per transaction


def import_pgn(conn: psycopg.Connection, text: str, source_ref: str, import_id: int | None = None) -> int:
    """Import every game in a PGN string. Returns the import id."""
    return run_import(conn, read_pgn(text), "pgn", source_ref, import_id)


def run_import(
    conn: psycopg.Connection, items: Iterable[CanonicalGame | ParseFailure], source: str, source_ref: str,
    import_id: int | None = None,
) -> int:
    """Resumable by construction: re-running the same input skips already-stored games
    (dedupe happens before replay), so an interrupted import just needs to be run again."""
    with tracked_import(conn, source, source_ref, import_id=import_id) as import_id, tracked_run(conn) as run:
        store_items(conn, import_id, run, items)
    return import_id


def import_chesscom(
    conn: psycopg.Connection,
    username: str,
    user: str = "me",
    fetch: Callable[[str], Any] = chesscom.fetch_json,
    now: datetime | None = None,
    import_id: int | None = None,
) -> int:
    """Import a Chess.com history, one monthly archive at a time.

    Past months are recorded in imports.resume_state once their games are committed and are never
    fetched again. The current month is always refetched (games are still being added to it).
    A month that fails to download, or has a game that raised while being stored, is retried next run.
    """
    username = username.lower()
    account_id = db.ensure_account(conn, user, "chesscom", username)
    months_done = db.chesscom_months_done(conn, username)
    current_month = (now or datetime.now(UTC)).strftime("%Y/%m")

    with tracked_import(conn, "chesscom", username, account_id, import_id) as import_id, tracked_run(conn) as run:
        db.set_resume_state(conn, import_id, {"months_done": sorted(months_done)})
        for url in chesscom.archive_urls(username, fetch):
            month = chesscom.month_of(url)
            if month in months_done:
                continue
            try:
                archive = fetch(url)
            except chesscom.FETCH_ERRORS as e:
                db.record_progress(conn, import_id, errors=[{"month": month, "error": f"download failed: {e}"}])
                continue
            crashed = store_items(conn, import_id, run, chesscom.games_in_archive(archive), {"month": month})
            if month < current_month and not crashed:
                months_done.add(month)
                db.set_resume_state(conn, import_id, {"months_done": sorted(months_done)})
    return import_id


@contextmanager
def tracked_import(
    conn: psycopg.Connection, source: str, source_ref: str, account_id: int | None = None, import_id: int | None = None
) -> Iterator[int]:
    """Create (or adopt a pre-created) imports row; mark it completed or failed when the block exits."""
    if import_id is None:
        import_id = db.start_import(conn, source, source_ref, account_id)
    elif account_id is not None:
        db.set_import_account(conn, import_id, account_id)
    try:
        yield import_id
    except BaseException as e:
        db.record_progress(conn, import_id, errors=[{"error": f"import aborted: {type(e).__name__}: {e}"}])
        db.finish_import(conn, import_id, "failed")
        raise
    db.finish_import(conn, import_id, "completed")


def store_items(
    conn: psycopg.Connection, import_id: int, run: Run, items: Iterable[CanonicalGame | ParseFailure],
    context: dict | None = None,
) -> int:
    """Store games in batches. One bad game is recorded (with `context`) and skipped; it never aborts the import.

    Returns how many games raised while being stored. Unlike parse failures (deterministic, e.g. a
    variant), those may succeed on retry, so callers shouldn't mark their input as finished.
    """
    crashed = 0
    for batch in itertools.batched(enumerate(items, start=1), BATCH_SIZE):
        imported = duplicate = events = 0
        stored_ids: list[int] = []
        errors: list[dict] = []
        with conn.transaction():
            for index, item in batch:
                if isinstance(item, ParseFailure):
                    errors.append({**(context or {}), "index": index, "error": item.error})
                    continue
                try:
                    with conn.transaction():  # savepoint: a failure discards only this game
                        stored = store_game(conn, item, import_id, run)
                    if stored is None:
                        duplicate += 1
                    else:
                        imported += 1
                        stored_ids.append(stored[0])
                        events += stored[1]
                except psycopg.OperationalError:
                    raise  # the connection is gone; per-game handling can't help
                except Exception as e:
                    crashed += 1
                    errors.append({**(context or {}), "index": index, "error": f"{type(e).__name__}: {e}"})
            db.mark_analyzed(conn, stored_ids, run.versions)
            db.record_progress(conn, import_id, len(batch), imported, duplicate, len(errors), errors)
            db.record_run_progress(conn, run.id, imported, events)
    return crashed


def store_game(conn: psycopg.Connection, game: CanonicalGame, import_id: int | None, run: Run) -> tuple[int, int] | None:
    """Persist one game, its moves and its events from a single replay.
    Returns (game id, event count), or None if the game was already stored.
    The caller records it in game_analysis (once per batch)."""
    game_id = db.insert_game(conn, game, import_id)
    if game_id is None:
        return None
    facts, events = analyze(game, run.detectors)
    db.insert_moves(conn, game_id, facts)
    db.insert_events(conn, game_id, run.id, events)
    return game_id, len(events)
