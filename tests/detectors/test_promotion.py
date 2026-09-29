from chesstrove.detectors.promotion import PromotionCheckmate, Underpromotion

KNIGHT_FORK_PROMO = "8/P1k5/8/8/8/8/8/4K3 w - - 0 1"  # a8=N+ checks the king on c7
BACK_RANK = "7k/P5pp/8/8/8/8/8/K7 w - - 0 1"  # a8=Q# and a8=R# mate along rank 8
DISCOVERY = "7k/6Pp/8/8/8/8/8/B5RK w - - 0 1"  # g7-g8 opens the a1-h8 diagonal


# --- UNDERPROMOTION ---

def test_underpromotion_to_knight_with_check(detect):
    [e] = detect(Underpromotion(), "1. a8=N+", KNIGHT_FORK_PROMO)
    assert (e.type, e.ply, e.color) == ("UNDERPROMOTION", 1, "w")
    assert e.fen == KNIGHT_FORK_PROMO
    assert e.metadata == {"promotion_piece": "knight", "square": "a8", "is_capture": False,
                          "gave_check": True, "gave_mate": False,
                          "queen_gives_check": False, "queen_gives_mate": False, "queen_stalemates": False}


def test_underpromotion_capture_to_rook(detect):
    [e] = detect(Underpromotion(), "1. bxa8=R", "r3k3/1P6/8/8/8/8/8/4K3 w - - 0 1")
    assert e.metadata["promotion_piece"] == "rook" and e.metadata["is_capture"]


def test_black_underpromotion_to_bishop(detect):
    [e] = detect(Underpromotion(), "1... a1=B", "4k3/8/8/8/8/8/p7/7K b - - 0 1")
    assert (e.color, e.metadata["square"], e.metadata["promotion_piece"]) == ("b", "a1", "bishop")


def test_queen_promotion_is_not_underpromotion(detect):
    assert detect(Underpromotion(), "1. a8=Q", KNIGHT_FORK_PROMO) == []


def test_ordinary_pawn_move_is_not_underpromotion(detect):
    assert detect(Underpromotion(), "1. e4 e5") == []


# --- PROMOTION_CHECKMATE ---

def test_queen_promotion_mate(detect):
    [e] = detect(PromotionCheckmate(), "1. a8=Q#", BACK_RANK)
    assert e.metadata == {"promotion_piece": "queen", "square": "a8", "promoted_piece_checks": True}


def test_underpromotion_mate_fires_both_detectors(detect):
    assert len(detect(PromotionCheckmate(), "1. a8=R#", BACK_RANK)) == 1
    [under] = detect(Underpromotion(), "1. a8=R#", BACK_RANK)
    assert under.metadata["gave_mate"]


def test_discovered_mate_by_promotion(detect):
    # The new knight doesn't check; the a1 bishop does, and the g1 rook guards g8.
    [e] = detect(PromotionCheckmate(), "1. g8=N#", DISCOVERY)
    assert e.metadata["promoted_piece_checks"] is False


def test_promotion_with_check_but_no_mate(detect):
    assert detect(PromotionCheckmate(), "1. a8=N+", KNIGHT_FORK_PROMO) == []


def test_promotion_without_check(detect):
    assert detect(PromotionCheckmate(), "1. a8=B", BACK_RANK) == []  # bishop on a8 doesn't reach h8


def test_mate_without_promotion(detect):
    assert detect(PromotionCheckmate(), "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#") == []
