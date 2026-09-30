"""Import pipeline: source -> CanonicalGame -> one-pass replay -> games + moves rows."""

import itertools
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg

from chesstrove import db
from chesstrove.analysis import Run, analyze, tracked_run
from chesstrove.importers import chesscom, lichess
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


def import_lichess(
    conn: psycopg.Connection,
    username: str,
    user: str = "me",
    open_stream: lichess.OpenStream = lichess.open_ndjson,
    import_id: int | None = None,
) -> int:
    """Import a Lichess history from one oldest-first stream, checkpointing after every committed batch.

    The checkpoint (imports.resume_state.since, ms) is the next run's `since`. It advances past each stored
    game, but stops before the first game that's still in progress (so that game is fetched again once
    it's finished) or after a batch in which a game raised (so it's retried).
    """
    username = username.lower()
    account_id = db.ensure_account(conn, user, "lichess", username)
    since = checkpoint = db.lichess_checkpoint(conn, username)
    frozen = False  # once set, the checkpoint stays put for the rest of this run

    with tracked_import(conn, "lichess", username, account_id, import_id) as import_id, tracked_run(conn) as run:
        db.set_resume_state(conn, import_id, {"since": checkpoint})
        for chunk in itertools.batched(lichess.stream_games(username, since, open_stream), BATCH_SIZE):
            items = []
            chunk_checkpoint = checkpoint
            for game in chunk:
                if lichess.is_ongoing(game):
                    if not frozen:
                        chunk_checkpoint = min(chunk_checkpoint, game["createdAt"])  # games arrive oldest first
                    frozen = True
                    continue
                items.append(lichess.to_item(game))
                if not frozen:
                    chunk_checkpoint = game["createdAt"] + 1
            crashed = store_items(conn, import_id, run, items)
            if crashed:
                frozen = True
            else:
                checkpoint = chunk_checkpoint
            db.set_resume_state(conn, import_id, {"since": checkpoint})
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
        skipped = 0
        errors: list[dict] = []
        games: list[tuple[int, CanonicalGame]] = []
        for index, item in batch:
            if isinstance(item, ParseFailure):
                if item.skipped:
                    skipped += 1
                else:
                    errors.append({**(context or {}), "index": index, "error": item.error})
            else:
                games.append((index, item))
        try:
            with conn.transaction():
                stored_ids, events, duplicate, failed = store_batch(conn, games, import_id, run, context)
                _finish_batch(conn, import_id, run, len(batch), stored_ids, events, duplicate, skipped, errors + failed)
        except psycopg.OperationalError:
            raise  # the connection is gone; per-game handling can't help
        except Exception:
            # the database refused something in the batch: redo it one game at a time to find it
            with conn.transaction():
                stored_ids, events, duplicate, failed = store_one_by_one(conn, games, import_id, run, context)
                _finish_batch(conn, import_id, run, len(batch), stored_ids, events, duplicate, skipped, errors + failed)
        crashed += len(failed)
    return crashed


def _finish_batch(conn, import_id, run, seen, stored_ids, events, duplicate, skipped, errors) -> None:
    db.mark_analyzed(conn, stored_ids, run.versions)
    db.record_progress(conn, import_id, seen, len(stored_ids), duplicate, len(errors), errors, skipped)
    db.record_run_progress(conn, run.id, len(stored_ids), events)


def store_batch(
    conn: psycopg.Connection, games: list[tuple[int, CanonicalGame]], import_id: int | None, run: Run,
    context: dict | None = None,
) -> tuple[list[int], int, int, list[dict]]:
    """Dedupe, replay and write a batch in a handful of statements, whatever its size: the hosted database is
    tens of milliseconds away, and a few statements per game made a 3,700-game import take half an hour.
    A game that fails to replay is recorded and left out. Returns (stored ids, events, duplicates, errors)."""
    known = db.existing_game_keys(conn, [g.source_key for _, g in games])
    new: dict[str, tuple[CanonicalGame, list, list]] = {}
    duplicate, errors = 0, []
    for index, game in games:
        if game.source_key in known or game.source_key in new:
            duplicate += 1
            continue
        try:
            new[game.source_key] = (game, *replay_game(game, run))
        except Exception as e:
            errors.append({**(context or {}), "index": index, "error": f"{type(e).__name__}: {e}"})
    ids = db.insert_games(conn, [g for g, _, _ in new.values()], import_id)  # a key stored meanwhile is left out
    duplicate += len(new) - len(ids)
    db.insert_moves(conn, [(ids[k], facts) for k, (_, facts, _) in new.items() if k in ids])
    db.insert_events(conn, run.id, [(ids[k], events) for k, (_, _, events) in new.items() if k in ids])
    return list(ids.values()), sum(len(new[k][2]) for k in ids), duplicate, errors


def store_one_by_one(
    conn: psycopg.Connection, games: list[tuple[int, CanonicalGame]], import_id: int | None, run: Run,
    context: dict | None = None,
) -> tuple[list[int], int, int, list[dict]]:
    """The slow path, a savepoint per game, for a batch the database refused as a whole."""
    stored_ids, events, duplicate, errors = [], 0, 0, []
    for index, game in games:
        try:
            with conn.transaction():
                ids, n, dup, failed = store_batch(conn, [(index, game)], import_id, run, context)
        except psycopg.OperationalError:
            raise
        except Exception as e:
            ids, n, dup, failed = [], 0, 0, [{**(context or {}), "index": index, "error": f"{type(e).__name__}: {e}"}]
        stored_ids += ids
        events += n
        duplicate += dup
        errors += failed
    return stored_ids, events, duplicate, errors


def replay_game(game: CanonicalGame, run: Run) -> tuple[list, list]:
    """One replay: the move facts to store and the detector events. Tests patch this to make a game fail."""
    return analyze(game, run.detectors)
