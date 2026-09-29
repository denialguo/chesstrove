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


def found(fen: str, uci: str) -> dict[str, dict]:
    """Every pattern the mating move carries, with its metadata."""
    board = chess.Board(fen)
    san = board.san(chess.Move.from_uci(uci))
    [game] = read_pgn(f'[SetUp "1"]\n[FEN "{fen}"]\n[Result "*"]\n\n{"1." if board.turn else "1..."} {san} *\n')
    ctxs = list(replay(game))
    assert ctxs[-1].facts.is_checkmate
    return {e.type: e.metadata for ctx in ctxs for d in ALL for e in d.detect(ctx)}


def names(fen: str, uci: str) -> set[str]:
    return set(found(fen, uci))


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


NO_TEXTBOOK_TIER = {"CORRIDOR_MATE", "BLACKBURNE_MATE"}


@pytest.mark.parametrize("name", [n for n in CANON if n.endswith("_MATE") and n not in ("BACK_RANK_MATE", "SMOTHERED_MATE")])
def test_textbook_picture_is_graded_textbook(name):
    meta = found(*CANON[name])[name]
    assert meta["form"] == ("canonical" if name in NO_TEXTBOOK_TIER else "textbook"), meta
    assert meta["short_of"] == []


# (pattern, position before, mating move, expected form or None for "not this pattern"). Each is checked
# in all four orientations.
FORMS = [
    # Epaulette: family keeps any shoulder pieces; rooks + unaided queen is canonical; the edge picture is textbook
    ("EPAULETTE_MATE", "3bnb2/3rkr2/8/8/8/8/8/Q5K1 w - - 0 1", "a1e5", "canonical"),  # rook shoulders, away from the back rank
    ("EPAULETTE_MATE", "3bnb2/3pkp2/8/8/8/8/8/Q5K1 w - - 0 1", "a1e5", "variant"),  # pawn shoulders: the broader family
    ("EPAULETTE_MATE", "3rkr2/8/2P3P1/8/8/8/8/1Q4K1 w - - 0 1", "b1e4", "variant"),  # rook shoulders, but pawns cover d7/f7
    ("EPAULETTE_MATE", "R7/3rkr2/8/8/8/8/8/Q5K1 w - - 0 1", "a1e5", "variant"),  # rooks; a rook cuts off the rank behind
    ("EPAULETTE_MATE", "R7/3pkp2/8/8/8/8/8/Q5K1 w - - 0 1", "a1e5", "variant"),  # pawn shoulders sealed from behind: loose, kept for review
    ("EPAULETTE_MATE", "3rk3/8/8/8/1B6/8/Q7/7K w - - 0 1", "a2e6", None),  # one shoulder only
    # Anastasia: one knight on both characteristic flights is canonical; the Kh7 picture is textbook
    ("ANASTASIA_MATE", "6k1/6p1/8/6N1/8/8/8/R5K1 w - - 0 1", "a1a8", "canonical"),  # rotated onto the back rank
    ("ANASTASIA_MATE", "6k1/6pp/8/4N3/8/8/8/R5K1 w - - 0 1", "a1a8", "variant"),  # a back-rank mate a knight finishes
    ("ANASTASIA_MATE", "6k1/6pp/8/4N3/2B5/8/8/R5K1 w - - 0 1", "a1a8", None),  # the bishop covers f7 too: knight incidental
    # Arabian
    ("ARABIAN_MATE", "4kr2/8/2N5/8/8/8/8/4R1K1 w - - 0 1", "e1e7", "canonical"),  # off the corner, mechanism intact
    ("ARABIAN_MATE", "2k5/8/N3P3/8/8/8/8/1R4K1 w - - 0 1", "b1b8", "variant"),  # a pawn closes d7
    ("ARABIAN_MATE", "2k5/8/N7/8/8/8/8/1R1R2K1 w - - 0 1", "b1b8", "variant"),  # a second rook helps: loose, kept for review
    ("ARABIAN_MATE", "5N1k/8/8/8/2B5/8/8/6KR w - - 0 1", "h1h7", None),  # the knight only guards the rook
    # Réti
    ("RETI_MATE", "1nb5/1pk5/8/3Q2B1/8/8/8/6K1 w - - 0 1", "g5d8", "canonical"),  # three blockers, queen guard
    ("RETI_MATE", "1nb5/1pk5/2p5/P7/5B2/8/8/3QR1K1 w - - 0 1", "f4e5", None),  # the bishop mates from a distance
    # Pillsbury
    ("PILLSBURY_MATE", "6B1/8/7k/5P1p/8/8/8/6RK w - - 0 1", "g1g6", "canonical"),  # lifted rook, king up the side file
    ("PILLSBURY_MATE", "6k1/5p1p/4N3/8/8/3R4/1B6/6K1 w - - 0 1", "d3g3", "variant"),  # a knight covers f8
    ("PILLSBURY_MATE", "5rkr/5p1p/8/8/8/3R4/1B6/6K1 w - - 0 1", "d3g3", None),  # corner blocked: the bishop does nothing characteristic
    # Lolli, Damiano
    ("LOLLI_MATE", "8/8/8/7k/7P/8/6Q1/2K5 w - - 0 1", "g2g5", "canonical"),  # king in the middle of an edge
    ("LOLLI_MATE", "6k1/8/8/8/8/8/1B6/6QK w - - 0 1", "g1g7", None),  # a bishop, not a pawn, guards the queen
    ("DAMIANO_MATE", "5rk1/5p2/8/8/8/3B4/8/6KQ w - - 0 1", "h1h7", "canonical"),  # bishop support
    ("DAMIANO_MATE", "6k1/3N1p2/6P1/8/8/8/8/6KQ w - - 0 1", "h1h7", "variant"),  # a knight covers f8
    ("DAMIANO_MATE", "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4", "h5f7", None),  # Scholar's mate
    # others with a canonical or variant tier
    ("OPERA_MATE", "4kb2/8/7N/6B1/8/8/8/3R2K1 w - - 0 1", "d1d8", "variant"),  # a knight covers f7
    ("GRECO_MATE", "7k/7p/7B/8/8/8/8/R5K1 w - - 0 1", "a1a8", "canonical"),  # along the back rank
    ("LADDER_MATE", "4k3/R7/8/8/8/8/8/1Q4K1 w - - 0 1", "b1b8", "canonical"),  # queen and rook
]


@pytest.mark.parametrize("name,fen,uci,form", FORMS)
@pytest.mark.parametrize("flip_files,swap_colours", [(False, False), (True, False), (False, True), (True, True)])
def test_form(name, fen, uci, form, flip_files, swap_colours):
    meta = found(*transformed(fen, uci, flip_files, swap_colours)).get(name)
    assert (meta and meta["form"]) == form, meta


def test_variant_explains_itself():
    meta = found("3bnb2/3pkp2/8/8/8/8/8/Q5K1 w - - 0 1", "a1e5")["EPAULETTE_MATE"]
    assert meta["short_of"] == ["both_shoulders_are_rooks"] and meta["traits"]["shoulders"] == ["pawn", "pawn"]
    meta = found("2k5/8/N3P3/8/8/8/8/1R4K1 w - - 0 1", "b1b8")["ARABIAN_MATE"]
    assert meta["short_of"] == ["no_extra_helpers"] and meta["helpers"] == ["e6"] and meta["defining"] == ["Na6", "Rb8"]


def test_pieces_merely_present_are_not_a_pattern():
    # A plain back-rank mate with two bishops and a knight idling on the board: not Blackburne's,
    # not Boden's, not Réti's.
    assert names("6k1/5ppp/8/8/8/8/1B3N2/R1B3K1 w - - 0 1", "a1a8") == {"BACK_RANK_MATE"}


def test_opera_needs_the_bishop_to_take_a_flight_square():
    # Rd8# guarded by Ba5 from the other diagonal: the bishop guards the rook but takes nothing away.
    assert names("4kn2/4pp2/8/B7/8/8/8/3R2K1 w - - 0 1", "d1d8") == set()


def test_non_mate_names_nothing():
    assert all(d.detect(ctx) == [] for ctx in replay(next(iter(read_pgn('[Result "*"]\n\n1. e4 *\n')))) for d in NAMED_MATES)
