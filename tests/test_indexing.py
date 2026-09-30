"""The optimised indexer (detector gating, no FENs unless an event needs one, the tighter missed-mate scan, FAST and
DEEP passes) must produce exactly what the original did: tests/reference_indexer.py is that original, frozen. The
full 3,689-game check against a real history lives in the benchmark notes (ARCHITECTURE.md); this runs the fixtures:
castling, en passant, promotions, set-up positions, Chess960, and every named-mate picture and form."""

import json

import chess
import pytest

import reference_indexer as reference
from chesstrove import indexing
from chesstrove.detectors.mates import mating_moves
from chesstrove.importers.chesscom import games_in_archive
from chesstrove.importers.pgn import read_pgn
from test_analysis import PGN
from test_browser_import import ARCHIVE, mate_positions

GAMES = [g for g in games_in_archive({"games": ARCHIVE["games"] + mate_positions()}) if hasattr(g, "moves_uci")] \
        + [g for g in read_pgn(PGN) if hasattr(g, "moves_uci")]


def rows(events) -> list:
    return [(d.id, d.version, e.type, e.ply, e.color, e.fen, json.dumps(e.metadata, sort_keys=True)) for d, e in events]


@pytest.mark.parametrize("game", GAMES, ids=lambda g: g.source_key)
def test_gated_passes_equal_the_reference(game):
    ref_facts, ref_events = reference.analyze(game)
    facts, events = indexing.analyze(game, indexing.DETECTORS)
    assert rows(events) == rows(ref_events)
    if ref_facts:
        assert indexing.pack_moves(facts) == indexing.pack_moves(ref_facts)
    fast, deep = indexing.analyze(game, indexing.FAST)[1], indexing.analyze(game, indexing.DEEP)[1]
    assert rows(fast) == [r for r in rows(ref_events) if r[0] != "MISSED_MATE_IN_ONE"]
    assert rows(deep) == [r for r in rows(ref_events) if r[0] == "MISSED_MATE_IN_ONE"]


# positions where a mate in one hides behind each way a move can check
MATE_IN_ONE = [
    "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1",                 # back-rank rook
    "6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1",             # rook along the rank
    "7k/6pp/8/6N1/8/8/8/6RK w - - 0 1",                  # knight (smothered-style)
    "k7/8/1K6/8/8/8/8/7R w - - 0 1",                     # plain rook
    "5b1r/p3pppp/2Q2nq1/1N6/k7/7P/PP1B1PP1/n4RK1 w - - 5 18",  # Na3#: discovered (from a real game)
    "6k1/5Rpp/8/2pB4/1pP3P1/1b4b1/1B5p/4rR1K w - - 1 31",   # Rf8#: double check (real)
    "2k4r/P1p1bp1p/2P1p1pB/1P2P1P1/8/5N2/n2r3P/R5K1 w - - 1 32",  # a8=Q#: promotion (real)
    "7k/5Kpp/8/8/8/8/8/B7 w - - 0 1",                    # the king uncovers the bishop (discovered)
    "k7/2P5/1K6/8/8/8/8/8 w - - 0 1",                    # promotion mates (c8=Q# and c8=R#)
    "4k3/8/4K3/8/8/8/8/3Q4 w - - 0 1",                   # queen
    "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",              # castling in the move list
    "8/8/8/2k5/3Pp3/8/8/4K2B b - d3 0 1",                # en passant in the move list
    "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3",
]


@pytest.mark.parametrize("fen", MATE_IN_ONE)
def test_missed_mate_scan_equals_the_reference(fen):
    board = chess.Board(fen)
    for side in (board, board.mirror()):
        assert mating_moves(side) == reference.reference_mating_moves(side)
        assert mating_moves(side, list(side.legal_moves)) == reference.reference_mating_moves(side)
