"""The player page's rare-moment totals: distinct qualifying moves per side, not detector hits."""

import pytest

from chesstrove import db
from chesstrove.ingest import import_pgn
from psycopg.types.json import Jsonb
from test_analysis import PGN


@pytest.fixture
def summary(conn):
    import_pgn(conn, PGN, "mine.pgn")
    conn.execute("DELETE FROM events")  # only the labels below
    a, b = (r["id"] for r in conn.execute(
        "SELECT id FROM games ORDER BY CASE WHEN lower(white) = 'alice' THEN 0 ELSE 1 END").fetchall())

    def label(game, ply, color, type, form=None):
        conn.execute(
            "INSERT INTO events (game_id, ply, type, detector_id, detector_version, color, fen, metadata) "
            "VALUES (%s, %s, %s, %s, 1, %s, '', %s)",
            (game, ply, type, type, color, Jsonb({"form": form} if form else {})))

    # game a: alice is White
    for t in ("UNDERPROMOTION", "PROMOTION_CHECKMATE", "SMOTHERED_MATE"):
        label(a, 1, "w", t)                            # one move, three labels
    label(a, 3, "w", "BACK_RANK_MATE")
    label(a, 3, "w", "OPERA_MATE", "textbook")         # back-rank + named mate on one move
    label(a, 5, "w", "DOUBLE_CHECK")                   # a third moment in the same game
    label(a, 7, "w", "MISSED_MATE_IN_ONE")             # a mistake, not a rare moment
    label(a, 9, "w", "BLUNDER")                        # engine labels never count
    label(a, 9, "w", "ONLY_WINNING_MOVE")
    label(a, 11, "w", "ARABIAN_MATE", "characteristic")  # a named mate on its own
    label(a, 13, "w", "EPAULETTE_MATE", "variant")     # variants are left out
    label(a, 15, "w", "BODEN_MATE")                    # named mates without a grade are left out
    # game b: alice is Black; bob's move is against her
    label(b, 2, "w", "LADDER_MATE", "textbook")
    label(b, 2, "w", "BACK_RANK_MATE")
    label(b, 4, "b", "DOUBLE_CHECK")
    return db.player_summary(conn, "sha256", "alice")  # PGN games without a site id are keyed by hash


def test_distinct_moves_per_side(summary):
    assert (summary["rare_moments"]["mine"], summary["rare_moments"]["against"]) == (5, 1)


def test_three_labels_on_one_move_add_one(conn, summary):
    before = summary["rare_moments"]["mine"]
    game = conn.execute("SELECT game_id FROM events WHERE ply = 5").fetchone()["game_id"]
    conn.execute("INSERT INTO events (game_id, ply, type, detector_id, detector_version, color, fen) VALUES "
                 "(%(g)s, 15, 'DOUBLE_CHECK', 'DOUBLE_CHECK', 1, 'w', ''), (%(g)s, 15, 'KING_DELIVERED_MATE', 'KING_DELIVERED_MATE', 1, 'w', ''),"
                 "(%(g)s, 15, 'BOX_MATE', 'BOX_MATE', 1, 'w', '')", {"g": game})
    after = db.player_summary(conn, "sha256", "alice")  # PGN games without a site id are keyed by hash
    assert after["rare_moments"]["mine"] == before + 1


def test_labels_by_type_leave_out_mistakes_engine_and_variants(summary):
    types = {t["type"]: (t["mine"], t["against"]) for t in summary["rare_moments"]["types"]}
    assert types == {"UNDERPROMOTION": (1, 0), "PROMOTION_CHECKMATE": (1, 0), "SMOTHERED_MATE": (1, 0),
                     "BACK_RANK_MATE": (1, 1), "OPERA_MATE": (1, 0), "DOUBLE_CHECK": (2, 0),
                     "ARABIAN_MATE": (1, 0), "LADDER_MATE": (0, 1)}


def test_collection_counts_unchanged(summary):
    motifs = {m["type"]: (m["mine"], m["against"]) for m in summary["motifs"]}
    assert motifs["MISSED_MATE_IN_ONE"] == (1, 0)
    assert motifs["EPAULETTE_MATE"] == (1, 0)
    assert motifs["BACK_RANK_MATE"] == (1, 1)


def test_forms_breakdown_uses_the_new_names(summary):
    forms = {m["type"]: m["forms"] for m in summary["motifs"]}
    assert forms["OPERA_MATE"] == {"textbook": 1}
    assert forms["ARABIAN_MATE"] == {"characteristic": 1}
    assert forms["EPAULETTE_MATE"] == {"variant": 1}


def test_old_canonical_rows_are_migrated_and_still_count(conn, summary):
    before = summary["rare_moments"]["mine"]
    conn.execute("UPDATE events SET metadata = '{\"form\": \"canonical\"}' WHERE type = 'ARABIAN_MATE'")
    conn.execute("UPDATE events SET metadata = '{\"form\": \"variant\"}' WHERE type = 'OPERA_MATE'")
    db.init_schema(conn)
    forms = {r["type"]: r["form"] for r in conn.execute(
        "SELECT type, metadata->>'form' AS form FROM events WHERE metadata ? 'form'").fetchall()}
    assert forms == {"ARABIAN_MATE": "characteristic", "OPERA_MATE": "variant",
                     "EPAULETTE_MATE": "variant", "LADDER_MATE": "textbook"}
    # the Opera move also carries BACK_RANK_MATE, so it stays a rare moment; the Arabian one is unchanged
    assert db.player_summary(conn, "sha256", "alice")["rare_moments"]["mine"] == before
