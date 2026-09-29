from chesstrove.detectors.patterns import BackRankMate, SmotheredMate
from chesstrove.detectors.promotion import Underpromotion

PHILIDOR = "6rk/6pp/8/6N1/8/8/8/6K1 w - - 0 1"  # Nf7#: h8 king walled in by its own rook and pawns
KNIGHT_MATE_OPEN_G8 = "7k/4N1pp/8/6N1/8/8/8/6K1 w - - 0 1"  # Nf7# but g8 is empty (covered by Ne7)
BLACK_SMOTHERS = "6k1/8/8/8/4n3/8/6PP/6RK b - - 0 1"  # ...Nf2#
BACK_RANK = "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"
BACK_RANK_QUEEN = "6k1/5ppp/8/8/8/8/8/3Q2K1 w - - 0 1"
BLACK_BACK_RANK = "r5k1/8/8/8/8/8/5PPP/6K1 b - - 0 1"
ESCAPE_ATTACKED_NOT_BLOCKED = "6k1/5p1p/8/8/8/8/1B6/R5K1 w - - 0 1"  # Ra8# but g7 is empty (covered by Bb2)
QUEEN_WOULD_STALEMATE = "8/6P1/8/8/8/8/2K5/k7 w - - 0 1"  # g8=Q covers a2: stalemate; g8=R doesn't


# --- SMOTHERED_MATE ---

def test_philidor_smothered_mate(detect):
    [e] = detect(SmotheredMate(), "1. Nf7#", PHILIDOR)
    assert (e.color, e.metadata) == ("w", {"king_square": "h8", "checkers": ["f7"], "pure": True})


def test_black_smothers_white(detect):
    [e] = detect(SmotheredMate(), "1... Nf2#", BLACK_SMOTHERS)
    assert (e.color, e.metadata["king_square"]) == ("b", "h1")


def test_escape_square_covered_by_the_mating_knight_counts(detect):
    # Nd6# checks e8 and also covers f7, the only square not held by Black's own pieces.
    [e] = detect(SmotheredMate(), "1. Nd6#", "3qkb2/3pr3/8/1N6/8/8/8/K7 w - - 0 1")
    assert (e.metadata["king_square"], e.metadata["pure"]) == ("e8", False)


def test_escape_square_covered_by_another_piece_is_not_smothered(detect):
    # Nf7# mates, but g8 is taken away by the other knight on e7, not by the mating knight.
    assert detect(SmotheredMate(), "1. Nf7#", KNIGHT_MATE_OPEN_G8) == []


def test_rook_mate_is_not_smothered(detect):
    assert detect(SmotheredMate(), "1. Ra8#", BACK_RANK) == []


def test_knight_check_without_mate(detect):
    # Same geometry minus the g8 rook: Nf7+ is only check (Kg8 escapes).
    assert detect(SmotheredMate(), "1. Nf7+", "7k/6pp/8/6N1/8/8/8/6K1 w - - 0 1") == []


# --- BACK_RANK_MATE ---

def test_rook_back_rank_mate(detect):
    [e] = detect(BackRankMate(), "1. Ra8#", BACK_RANK)
    assert e.metadata == {"king_square": "g8", "checker": "a8", "checker_piece": "rook"}


def test_queen_back_rank_mate(detect):
    [e] = detect(BackRankMate(), "1. Qd8#", BACK_RANK_QUEEN)
    assert e.metadata["checker_piece"] == "queen"


def test_black_mates_on_whites_back_rank(detect):
    [e] = detect(BackRankMate(), "1... Ra1#", BLACK_BACK_RANK)
    assert (e.color, e.metadata["king_square"]) == ("b", "g1")


def test_escape_square_merely_attacked_does_not_count(detect):
    assert detect(BackRankMate(), "1. Ra8#", ESCAPE_ATTACKED_NOT_BLOCKED) == []


def test_back_rank_check_without_mate(detect):
    assert detect(BackRankMate(), "1. Ra8+", "6k1/5pp1/8/8/8/8/8/R5K1 w - - 0 1") == []


def test_knight_mate_on_back_rank_is_not_back_rank_mate(detect):
    assert detect(BackRankMate(), "1. Nf7#", PHILIDOR) == []


# --- UNDERPROMOTION v2: exact facts about queening instead ---

def test_queening_would_have_stalemated(detect):
    [e] = detect(Underpromotion(), "1. g8=R", QUEEN_WOULD_STALEMATE)
    assert (e.metadata["queen_stalemates"], e.metadata["queen_gives_check"], e.metadata["gave_check"]) == (True, False, False)


def test_queening_would_also_have_mated(detect):
    [e] = detect(Underpromotion(), "1. a8=R#", "7k/P5pp/8/8/8/8/8/K7 w - - 0 1")
    assert (e.metadata["gave_mate"], e.metadata["queen_gives_mate"], e.metadata["queen_stalemates"]) == (True, True, False)


def test_queen_alternative_leaves_board_untouched():
    # The detector pushes the queen move on board_before; later detectors on the same ply need it restored.
    from chesstrove.importers.pgn import read_pgn
    from chesstrove.reconstruction import replay

    [game] = read_pgn(f'[SetUp "1"]\n[FEN "{QUEEN_WOULD_STALEMATE}"]\n[Result "*"]\n\n1. g8=R *\n')
    [ctx] = replay(game)
    Underpromotion().detect(ctx)
    assert ctx.board_before.fen() == QUEEN_WOULD_STALEMATE and not ctx.board_before.move_stack
