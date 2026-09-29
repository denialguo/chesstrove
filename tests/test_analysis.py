from datetime import date

import pytest

from chesstrove import db, detectors
from chesstrove.analysis import reanalyze
from chesstrove.cli import main
from chesstrove.detectors.base import event
from chesstrove.detectors.promotion import Underpromotion
from chesstrove.ingest import import_pgn

# 2024 game: White (alice) underpromotes with check. 2023 game: Black (alice) misses Ra1# and later mates.
UNDERPROMO_GAME = """[White "alice"]
[Black "bob"]
[Result "*"]
[UTCDate "2024.05.01"]
[UTCTime "10:00:00"]
[SetUp "1"]
[FEN "8/P1k5/8/8/8/8/8/4K3 w - - 0 1"]

1. a8=N+ *
"""
MISSED_MATE_GAME = """[White "bob"]
[Black "alice"]
[Result "0-1"]
[UTCDate "2023.01.01"]
[UTCTime "10:00:00"]
[SetUp "1"]
[FEN "r5k1/8/8/8/8/8/5PPP/6K1 b - - 0 1"]

1... Kf8 2. Kf1 Ra1# 0-1
"""
PGN = UNDERPROMO_GAME + "\n" + MISSED_MATE_GAME


def events(conn, **filters) -> list[tuple[str, int, str]]:
    return [(e["type"], e["ply"], e["color"]) for e in db.list_events(conn, **filters)]


def test_import_detects_events_in_the_same_pass(conn):
    import_pgn(conn, PGN, "games.pgn")
    assert events(conn) == [("UNDERPROMOTION", 1, "w"), ("MISSED_MATE_IN_ONE", 1, "b")]
    [run] = conn.execute("SELECT * FROM analysis_runs").fetchall()
    assert run["status"] == "completed" and (run["games_processed"], run["events_created"]) == (2, 2)
    assert run["detector_versions"] == {d.id: d.version for d in detectors.DETECTORS}
    row = conn.execute("SELECT * FROM events WHERE type = 'MISSED_MATE_IN_ONE'").fetchone()
    assert row["metadata"] == {"mating_moves": ["Ra1#"], "played": "Kf8"}
    assert (row["detector_id"], row["detector_version"], row["analysis_run_id"]) == ("MISSED_MATE_IN_ONE", 1, run["id"])


def test_event_filters(conn):
    import_pgn(conn, PGN, "games.pgn")
    assert events(conn, type="UNDERPROMOTION") == [("UNDERPROMOTION", 1, "w")]
    assert events(conn, color="b") == [("MISSED_MATE_IN_ONE", 1, "b")]
    assert len(events(conn, player="ALICE")) == 2  # alice played both moves, once as each color
    assert events(conn, player="bob") == []
    assert events(conn, since=date(2024, 1, 1)) == [("UNDERPROMOTION", 1, "w")]
    assert events(conn, until=date(2023, 1, 1)) == [("MISSED_MATE_IN_ONE", 1, "b")]  # inclusive end date
    assert events(conn, type="UNDERPROMOTION", color="b") == []


def test_reanalyze_skips_up_to_date_games(conn):
    import_pgn(conn, PGN, "games.pgn")
    run = db.get_analysis_run(conn, reanalyze(conn))
    assert (run["status"], run["games_processed"], run["detector_versions"]) == ("completed", 0, {})


def test_version_bump_makes_games_stale(conn, monkeypatch):
    import_pgn(conn, PGN, "games.pgn")
    missed_before = conn.execute("SELECT id FROM events WHERE type = 'MISSED_MATE_IN_ONE'").fetchone()["id"]
    bumped = Underpromotion.version + 1
    monkeypatch.setattr(Underpromotion, "version", bumped)

    run = db.get_analysis_run(conn, reanalyze(conn))  # no args: finds what's stale by itself
    assert run["detector_versions"] == {"UNDERPROMOTION": bumped}  # only the changed detector runs
    assert run["games_processed"] == 2  # both games had the old UNDERPROMOTION version recorded
    assert conn.execute("SELECT id FROM events WHERE type = 'MISSED_MATE_IN_ONE'").fetchone()["id"] == missed_before
    rows = conn.execute("SELECT type, detector_version FROM events ORDER BY type").fetchall()
    assert [(r["type"], r["detector_version"]) for r in rows] == [("MISSED_MATE_IN_ONE", 1), ("UNDERPROMOTION", bumped)]
    assert db.get_analysis_run(conn, reanalyze(conn))["games_processed"] == 0  # now up to date


def test_reanalyze_single_detector_leaves_others_alone(conn, monkeypatch):
    import_pgn(conn, PGN, "games.pgn")
    missed_before = conn.execute("SELECT id FROM events WHERE type = 'MISSED_MATE_IN_ONE'").fetchone()["id"]
    monkeypatch.setattr(Underpromotion, "version", Underpromotion.version + 1)
    run = db.get_analysis_run(conn, reanalyze(conn, ["UNDERPROMOTION"]))
    assert run["detector_versions"] == {"UNDERPROMOTION": Underpromotion.version}
    assert conn.execute("SELECT id FROM events WHERE type = 'MISSED_MATE_IN_ONE'").fetchone()["id"] == missed_before


def test_new_detector_is_backfilled(conn, monkeypatch):
    import_pgn(conn, PGN, "games.pgn")

    class EveryKingMove:
        id, version = "KING_MOVE", 1

        def detect(self, ctx):
            return [event(ctx, self.id)] if ctx.facts.piece == "K" else []

    monkeypatch.setattr(detectors, "DETECTORS", (*detectors.DETECTORS, EveryKingMove()))
    run = db.get_analysis_run(conn, reanalyze(conn))
    assert (run["games_processed"], run["detector_versions"]) == (2, {"KING_MOVE": 1})
    assert events(conn, type="KING_MOVE") == [("KING_MOVE", 1, "b"), ("KING_MOVE", 2, "w")]
    assert len(events(conn)) == 4  # nothing duplicated


def test_force_redoes_everything(conn):
    import_pgn(conn, PGN, "games.pgn")
    assert db.get_analysis_run(conn, reanalyze(conn, force=True))["games_processed"] == 2
    assert len(events(conn)) == 2


def test_unknown_detector_id(conn):
    with pytest.raises(ValueError, match="NOPE"):
        reanalyze(conn, ["NOPE"])


def test_broken_detector_during_import_fails_only_that_game(conn, monkeypatch):
    class Boom:
        id, version = "BOOM", 1

        def detect(self, ctx):
            if ctx.facts.promotion:
                raise RuntimeError("bug")
            return []

    monkeypatch.setattr(detectors, "DETECTORS", (*detectors.DETECTORS, Boom()))
    imp = db.get_import(conn, import_pgn(conn, PGN, "games.pgn"))
    assert (imp["games_imported"], imp["games_failed"]) == (1, 1)
    assert "RuntimeError: bug" in imp["errors"][0]["error"]
    assert conn.execute("SELECT count(*) AS n FROM games").fetchone()["n"] == 1  # nothing half-stored


def test_broken_detector_during_reanalyze_fails_the_run(conn, monkeypatch):
    import_pgn(conn, PGN, "games.pgn")

    class Boom:
        id, version = "BOOM", 1

        def detect(self, ctx):
            raise RuntimeError("bug")

    monkeypatch.setattr(detectors, "DETECTORS", (*detectors.DETECTORS, Boom()))
    with pytest.raises(RuntimeError):
        reanalyze(conn)
    assert conn.execute("SELECT status FROM analysis_runs ORDER BY id DESC").fetchone()["status"] == "failed"
    assert len(events(conn)) == 2  # the batch rolled back; old events intact


def test_registry_ids_are_unique_and_versioned():
    ids = [d.id for d in detectors.DETECTORS]
    assert len(ids) == len(set(ids)) == 10
    assert all(isinstance(d.version, int) and d.version >= 1 for d in detectors.DETECTORS)
    assert all(d.__doc__ for d in detectors.DETECTORS)  # every definition is written down


def test_cli_events_and_reanalyze(dsn, conn, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHESSTROVE_DATABASE_URL", dsn)
    (tmp_path / "g.pgn").write_text(PGN)
    main(["import-pgn", str(tmp_path / "g.pgn")])
    capsys.readouterr()
    assert main(["events", "--type", "MISSED_MATE_IN_ONE", "--player", "alice"]) == 0
    assert '"Ra1#"' in capsys.readouterr().out
    assert main(["reanalyze", "--detector", "UNDERPROMOTION", "--all"]) == 0
    assert '"games_processed": 2' in capsys.readouterr().out
    assert main(["game", "1"]) == 0
    assert '"UNDERPROMOTION"' in capsys.readouterr().out
