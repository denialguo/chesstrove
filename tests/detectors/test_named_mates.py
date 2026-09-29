import chess
import pytest

from chesstrove.detectors.named_mates import NAMED_MATES
from chesstrove.detectors.patterns import BackRankMate, SmotheredMate
from chesstrove.importers.pgn import read_pgn
from chesstrove.reconstruction import replay

ALL = (*NAMED_MATES, BackRankMate(), SmotheredMate())

# One textbook picture per pattern: (position before, mating move). Each must carry exactly its own name.
CANON = {
    "EPAULETTE_MATE": ("3rkr2/8/8/8/8/8/Q7/7K w - - 0 1", "a2e6"),
    "SWALLOWS_TAIL_MATE": ("3r1r2/4k3/8/3P4/8/7Q/8/7K w - - 0 1", "h3e6"),
    "DOVETAIL_MATE": ("4b3/4kp2/8/2P5/8/8/8/3QK3 w - - 0 1", "d1d6"),
    "ANASTASIA_MATE": ("8/4N1pk/8/R7/8/8/8/6K1 w - - 0 1", "a5h5"),
    "ARABIAN_MATE": ("7k/1R6/5N2/8/8/8/8/6K1 w - - 0 1", "b7h7"),
    "BODEN_MATE": ("2kr4/3n4/8/8/5B2/8/4B3/6K1 w - - 0 1", "e2a6"),
    "OPERA_MATE": ("4kb2/5p2/4q3/6B1/8/8/8/3R2K1 w - - 0 1", "d1d8"),
    "ANDERSSEN_MATE": ("6k1/6P1/5K2/8/8/8/8/7R w - - 0 1", "h1h8"),
    "LOLLI_MATE": ("6k1/8/5P1Q/8/8/8/8/7K w - - 0 1", "h6g7"),
    "DAMIANO_MATE": ("5rk1/5p2/6P1/8/8/8/8/6KQ w - - 0 1", "h1h7"),
    "MORPHY_MATE": ("7k/7p/8/8/7B/8/8/6RK w - - 0 1", "h4f6"),
    "GRECO_MATE": ("7k/6p1/8/R7/2B5/8/8/6K1 w - - 0 1", "a5h5"),
    "HOOK_MATE": ("3rkr2/R7/8/3N4/2P5/8/8/6K1 w - - 0 1", "a7e7"),
    "CORRIDOR_MATE": ("K7/8/6p1/6pk/6p1/8/8/R7 w - - 0 1", "a1h1"),
    "BLACKBURNE_MATE": ("5rk1/5p2/8/6N1/8/3B4/1B6/6K1 w - - 0 1", "d3h7"),
    "RETI_MATE": ("1nb5/1pk5/2p5/6B1/8/8/8/3R2K1 w - - 0 1", "g5d8"),
    "PILLSBURY_MATE": ("5rk1/5p1p/8/8/8/3R4/1B6/6K1 w - - 0 1", "d3g3"),
    "LADDER_MATE": ("4k3/R7/8/8/8/8/8/1R4K1 w - - 0 1", "b1b8"),
    "BOX_MATE": ("4k3/8/4K3/8/8/8/8/R7 w - - 0 1", "a1a8"),
    "BACK_RANK_MATE": ("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1", "a1a8"),  # a back-rank mate is not a corridor mate
    "SMOTHERED_MATE": ("6rk/6pp/8/6N1/8/8/8/6K1 w - - 0 1", "g5f7"),
}


def names(fen: str, uci: str) -> set[str]:
    board = chess.Board(fen)
    san = board.san(chess.Move.from_uci(uci))
    [game] = read_pgn(f'[SetUp "1"]\n[FEN "{fen}"]\n[Result "*"]\n\n{"1." if board.turn else "1..."} {san} *\n')
    ctxs = list(replay(game))
    assert ctxs[-1].facts.is_checkmate
    return {e.type for ctx in ctxs for d in ALL for e in d.detect(ctx)}


def transformed(fen: str, uci: str, flip_files: bool, swap_colours: bool) -> tuple[str, str]:
    board, move = chess.Board(fen), chess.Move.from_uci(uci)
    sq = lambda s: s ^ 7 if flip_files else s  # noqa: E731
    if flip_files:
        board = board.transform(chess.flip_horizontal)
    if swap_colours:
        board = board.mirror()
        sq_ = sq
        sq = lambda s: chess.square_mirror(sq_(s))  # noqa: E731
    return board.fen(), chess.Move(sq(move.from_square), sq(move.to_square)).uci()


@pytest.mark.parametrize("name", CANON)
@pytest.mark.parametrize("flip_files,swap_colours", [(False, False), (True, False), (False, True), (True, True)])
def test_textbook_picture_carries_exactly_its_own_name(name, flip_files, swap_colours):
    assert names(*transformed(*CANON[name], flip_files, swap_colours)) == {name}


def test_pieces_merely_present_are_not_a_pattern():
    # A plain back-rank mate with two bishops and a knight idling on the board: not Blackburne's,
    # not Boden's, not Réti's.
    assert names("6k1/5ppp/8/8/8/8/1B3N2/R1B3K1 w - - 0 1", "a1a8") == {"BACK_RANK_MATE"}


def test_opera_needs_the_bishop_to_take_a_flight_square():
    # Rd8# guarded by Ba5 from the other diagonal: the bishop guards the rook but takes nothing away.
    assert names("4kn2/4pp2/8/B7/8/8/8/3R2K1 w - - 0 1", "d1d8") == set()


def test_non_mate_names_nothing():
    assert all(d.detect(ctx) == [] for ctx in replay(next(iter(read_pgn('[Result "*"]\n\n1. e4 *\n')))) for d in NAMED_MATES)
