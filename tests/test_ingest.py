from chesstrove import db
from chesstrove.cli import main
from chesstrove.importers.pgn import read_pgn
from chesstrove.ingest import import_pgn, run_import
from chesstrove.models import CanonicalGame

GAME_A = """[Event "Live Chess"]
[White "alice"]
[Black "bob"]
[Result "1-0"]
[UTCDate "2024.03.10"]
[UTCTime "01:02:03"]
[Link "https://www.chess.com/game/live/1"]

1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0
"""
GAME_B = """[Event "Club"]
[White "bob"]
[Black "alice"]
[Result "0-1"]

1. f3 e5 2. g4 Qh4# 0-1
"""
BAD = '[Event "bad"]\n[Result "*"]\n\n1. e4 e5 2. Ke3 *\n'


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_import_stores_games_and_moves(conn):
    import_id = import_pgn(conn, GAME_A + "\n" + GAME_B, "two.pgn")
    imp = db.get_import(conn, import_id)
    assert (imp["status"], imp["games_seen"], imp["games_imported"], imp["games_failed"]) == ("completed", 2, 2, 0)
    assert count(conn, "games") == 2 and count(conn, "moves") == 7 + 4

    [latest, _] = db.list_games(conn)  # dated game first, undated last
    game = db.get_game(conn, latest["id"])
    assert game["source_key"] == "chesscom:1" and game["pgn"] == GAME_A.strip()
    assert [m["ply"] for m in game["moves"]] == list(range(1, 8))
    last = game["moves"][-1]
    assert (last["san"], last["is_checkmate"], last["captured"]) == ("Qxf7#", True, "P")
    assert [g["white"] for g in db.list_games(conn, player="ALICE")] == ["alice", "bob"]


def test_reimport_is_idempotent(conn):
    import_pgn(conn, GAME_A + "\n" + GAME_B, "first.pgn")
    second = db.get_import(conn, import_pgn(conn, GAME_A + "\n" + GAME_B, "again.pgn"))
    assert (second["games_imported"], second["games_duplicate"]) == (0, 2)
    assert count(conn, "games") == 2 and count(conn, "moves") == 11


def test_same_platform_game_with_different_formatting_dedupes(conn):
    import_pgn(conn, GAME_A, "a.pgn")
    reformatted = GAME_A.replace("1. e4 e5", "1. e4 {[%clk 0:03:00]} 1... e5").replace('[Event "Live Chess"]\n', "")
    imp = db.get_import(conn, import_pgn(conn, reformatted, "b.pgn"))
    assert imp["games_duplicate"] == 1 and count(conn, "games") == 1


def test_bad_game_does_not_abort_import(conn):
    imp = db.get_import(conn, import_pgn(conn, f"{GAME_A}\n{BAD}\n{GAME_B}", "mixed.pgn"))
    assert (imp["status"], imp["games_seen"], imp["games_imported"], imp["games_failed"]) == ("completed", 3, 2, 1)
    assert imp["errors"][0]["index"] == 2 and "Ke3" in imp["errors"][0]["error"]
    assert count(conn, "games") == 2


def test_failure_while_storing_rolls_back_only_that_game(conn):
    [good] = read_pgn(GAME_A)
    [other] = read_pgn(GAME_B)
    fields = {s: getattr(other, s) for s in CanonicalGame.__slots__}
    corrupt = CanonicalGame(**fields | {"moves_uci": ("f2f3", "f2f3")})  # fails mid-replay, after the games INSERT
    imp = db.get_import(conn, run_import(conn, [corrupt, good], "pgn", "test"))
    assert (imp["games_imported"], imp["games_failed"]) == (1, 1)
    assert "IllegalMoveError" in imp["errors"][0]["error"]
    assert count(conn, "games") == 1 and count(conn, "moves") == 7  # no orphan game row or partial moves
    # the corrupted game's key is free again, so a correct copy imports cleanly
    assert db.get_import(conn, run_import(conn, [other], "pgn", "retry"))["games_imported"] == 1


def test_batches_commit_progress(conn, monkeypatch):
    monkeypatch.setattr("chesstrove.ingest.BATCH_SIZE", 1)
    imp = db.get_import(conn, import_pgn(conn, f"{GAME_A}\n{BAD}\n{GAME_B}", "batched.pgn"))
    assert (imp["games_seen"], imp["games_imported"], imp["games_failed"]) == (3, 2, 1)


def test_cli_import_and_show(dsn, conn, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHESSTROVE_DATABASE_URL", dsn)
    pgn = tmp_path / "games.pgn"
    pgn.write_text(GAME_A)
    assert main(["import-pgn", str(pgn)]) == 0
    assert '"games_imported": 1' in capsys.readouterr().out
    assert main(["game", "1"]) == 0
    assert "Qxf7#" in capsys.readouterr().out
    assert main(["game", "999"]) == 1
