"""Running detectors: during import (same replay as the moves) and re-running over stored games."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg

from chesstrove import db
from chesstrove.detectors import Detector, select
from chesstrove.indexing import analyze, event_rows  # noqa: F401 (analyze is also imported from here)

BATCH_SIZE = 500  # games per transaction


@dataclass(frozen=True, slots=True)
class Run:
    id: int
    detectors: Sequence[Detector]

    @property
    def versions(self) -> dict[str, int]:
        return {d.id: d.version for d in self.detectors}


@contextmanager
def tracked_run(
    conn: psycopg.Connection, detectors: Sequence[Detector] | None = None, run_id: int | None = None
) -> Iterator[Run]:
    """An analysis_runs row recording exactly which detector versions produced its events. Default: all.
    A pre-created row (run_id) is adopted and its detector list set to what actually runs."""
    detectors = select(None) if detectors is None else detectors
    versions = {d.id: d.version for d in detectors}
    if run_id is None:
        run_id = db.start_analysis_run(conn, versions)
    else:
        db.set_run_detectors(conn, run_id, versions)
    run = Run(run_id, detectors)
    try:
        yield run
    except BaseException:
        db.finish_analysis_run(conn, run.id, "failed")
        raise
    db.finish_analysis_run(conn, run.id, "completed")


def reanalyze(
    conn: psycopg.Connection, detector_ids: Sequence[str] | None = None, force: bool = False, run_id: int | None = None
) -> int:
    """Re-run detectors over stored games from their stored moves. No download, no PGN parsing.

    By default only games whose recorded version of any selected detector differs from the current one
    (or that never saw it) are replayed, and with no ids given only the detectors that are stale somewhere
    run, so bumping a cheap detector doesn't re-run the expensive ones. `force` redoes every game.
    The selected detectors' events for each game are replaced atomically. Interrupted runs resume
    naturally: finished batches are no longer stale.
    """
    chosen = select(detector_ids)
    if not detector_ids and not force:
        chosen = tuple(d for d in chosen if db.has_stale_games(conn, d.id, d.version))
    with tracked_run(conn, chosen, run_id) as run:
        if not chosen:
            return run.id
        after = 0
        while rows := db.games_to_analyze(conn, run.versions, after, BATCH_SIZE, stale_only=not force):
            game_ids = [row["id"] for row in rows]
            with conn.transaction():
                db.delete_events(conn, game_ids, list(run.versions))
                found = [(row["id"], event_rows(analyze(db.game_from_row(row), run.detectors)[1])) for row in rows]
                db.insert_events(conn, run.id, found)
                events_created = sum(len(events) for _, events in found)
                db.mark_analyzed(conn, game_ids, run.versions)
                db.record_run_progress(conn, run.id, len(rows), events_created)
            after = game_ids[-1]
    return run.id
