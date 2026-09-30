"""Moves are stored one packed row per game (game_moves); the `moves` view and replayed positions stand in for
the old one-row-per-ply table with a FEN on every row."""

import pytest

from chesstrove import compact, db
from chesstrove.reconstruction import replay
from chesstrove.importers.pgn import read_pgn
from chesstrove.ingest import import_pgn

# castling both ways, en passant, a capture-promotion with check, and plain moves
SHARP = """[Event "t"]
[Site "?"]
[White "alice"]
[Black "bob"]
[Result "*"]

1. e4 d5 2. e5 f5 3. exf6 Nc6 4. fxg7 Bf5 5. gxh8=Q Qd7 6. Nf3 O-O-O 7. Bc4 e6 8. O-O Qg7 9. Qxg8 *
"""
# Chess960: castling is written king-takes-own-rook in UCI, so a replay must know the variant
FRC = """[Event "t"]
[Site "?"]
[White "alice"]
[Black "bob"]
[Result "*"]
[Variant "Chess960"]
[FEN "bnrbkrqn/pppppppp/8/8/8/8/PPPPPPPP/BNRBKRQN w KQkq - 0 1"]
[SetUp "1"]

1. g3 g6 2. Qg2 Qg7 3. O-O O-O *
"""

LEGACY = """CREATE TABLE moves (
    game_id bigint NOT NULL REFERENCES games ON DELETE CASCADE, ply int NOT NULL, color char(1) NOT NULL,
    san text NOT NULL, uci text NOT NULL, piece char(1) NOT NULL, captured char(1), promotion char(1),
    is_check boolean NOT NULL, is_checkmate boolean NOT NULL, is_castling boolean NOT NULL,
    is_en_passant boolean NOT NULL, fen_after text NOT NULL, material_white smallint NOT NULL,
    material_black smallint NOT NULL, queens_after smallint NOT NULL, legal_moves_before smallint NOT NULL,
    PRIMARY KEY (game_id, ply))"""
COLUMNS = ["game_id", "ply", "color", "san", "uci", "piece", "captured", "promotion", "is_check", "is_checkmate",
           "is_castling", "is_en_passant", "material_white", "material_black", "queens_after", "legal_moves_before"]


def expected_rows(conn) -> list[dict]:
    """What the old table held for every stored game, from a fresh replay."""
    out = []
    for g in conn.execute("SELECT id, pgn FROM games ORDER BY id").fetchall():
        [game] = read_pgn(g["pgn"])
        facts = [ctx.facts for ctx in replay(game)]  # with FENs: what the old table stored
        out += [{"game_id": g["id"], **{c: getattr(f, c) for c in COLUMNS[1:]}, "fen_after": f.fen_after} for f in facts]
    return out


@pytest.fixture
def games(conn):
    import_pgn(conn, SHARP + "\n" + FRC, "t.pgn")
    assert conn.execute("SELECT count(*) AS n FROM games").fetchone()["n"] == 2
    return expected_rows(conn)


def test_view_matches_the_old_rows_and_positions_replay_exactly(conn, games):
    view = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM moves ORDER BY game_id, ply").fetchall()
    assert view == [{c: r[c] for c in COLUMNS} for r in games]
    for r in games:  # the FEN every row used to store
        assert db.get_game(conn, r["game_id"])["moves"][r["ply"] - 1]["fen_after"] == r["fen_after"]
    flags = {r["uci"]: r for r in games}
    assert flags["e5f6"]["is_en_passant"] and flags["g7h8q"]["promotion"] == "Q" and flags["e8c8"]["is_castling"]
    assert flags["e1f1"]["is_castling"] and flags["e1f1"]["san"] == "O-O"  # Chess960: king takes own rook


def make_legacy(conn, games) -> None:
    """Put the database back in the old layout: one row per ply, a FEN on each."""
    conn.execute("DROP VIEW moves")
    conn.execute("TRUNCATE game_moves")
    conn.execute(LEGACY)
    with conn.cursor().copy(f"COPY moves ({', '.join(COLUMNS)}, fen_after) FROM STDIN") as copy:
        for r in games:
            copy.write_row([r[c] for c in COLUMNS] + [r["fen_after"]])


def assert_compact(conn, games) -> None:
    assert compact.state(conn) == "compact" and not db.has_legacy_moves(conn)
    view = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM moves ORDER BY game_id, ply").fetchall()
    assert view == [{c: r[c] for c in COLUMNS} for r in games]
    assert compact.problems(conn) == []
    db.init_schema(conn)  # the server starts again


@pytest.mark.parametrize("way", ["copy", "rebuild"])
def test_converting_an_old_database_keeps_every_ply(conn, games, way):
    make_legacy(conn, games)
    with pytest.raises(RuntimeError, match="compact-moves"):  # the server won't start on the old layout
        db.init_schema(conn)
    report = compact.check(conn)
    assert (report["state"], report["old_rows"], report["plies"]) == ("legacy", len(games), len(games))
    assert compact.state(conn) == "legacy"  # the check changed nothing

    getattr(compact, way)(conn, batch=1, log=lambda _: None)
    assert_compact(conn, games)
    getattr(compact, way)(conn, log=lambda _: None)  # a second run changes nothing
    assert_compact(conn, games)


def test_an_interrupted_copy_resumes(conn, games):
    make_legacy(conn, games)
    compact._schema(conn)
    first = games[0]["game_id"]
    conn.execute("""INSERT INTO game_moves SELECT %s, 'w', 'e2e4', 'e4', 'P', '.', '.', '{0}', '{39}', '{39}', '{1}', '{20}'""",
                 (first,))  # a wrong, partial row from an earlier run: the check must catch it
    assert compact.state(conn) == "converting"
    with pytest.raises(RuntimeError, match="didn't pack completely"):
        compact.copy(conn, log=lambda _: None)
    assert db.has_legacy_moves(conn)  # nothing dropped
    conn.execute("DELETE FROM game_moves WHERE game_id = %s", (first,))
    compact.copy(conn, log=lambda _: None)
    assert_compact(conn, games)


def test_an_ambiguous_layout_is_refused(conn, games):
    conn.execute("DROP VIEW moves")
    conn.execute("ALTER TABLE game_moves RENAME TO game_moves_elsewhere")
    assert compact.state(conn) == "ambiguous"
    for way in (compact.copy, compact.rebuild):
        with pytest.raises(RuntimeError, match="ambiguous"):
            way(conn, log=lambda _: None)
    conn.execute("ALTER TABLE game_moves_elsewhere RENAME TO game_moves")
