from chesstrove.detectors.mates import EnPassantCheckmate, KingDeliveredMate, MissedMateInOne

# Black king h5 boxed in: g4/h4 by the f3/g3 pawns, g6 by Ne7, h6 by Bf8. ...d5 exd6 e.p. opens rank 5 for Ra5.
EP_MATE = "5B2/3pN3/8/R3P2k/8/5PP1/8/6K1 b - - 0 1"
EP_CHECK_ONLY = "8/3pN3/8/R3P2k/8/5PP1/8/6K1 b - - 0 1"  # no Bf8: Kh6 escapes
CASTLE_MATE = "2rkr3/2p1p3/8/8/8/8/8/R3K3 w Q - 0 1"  # O-O-O puts the rook on d1; own pieces box the king in
CASTLE_CHECK = "3kr3/2p1p3/8/8/8/8/8/R3K3 w Q - 0 1"  # no c8 rook: Kc8 escapes
KING_DISCOVERY = "R2K3k/6pp/8/8/8/8/8/8 w - - 0 1"  # the king steps off rank 8, Ra8 mates
BACK_RANK = "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"  # Ra8# available
TWO_MATES = "6k1/5ppp/8/8/8/8/8/RR4K1 w - - 0 1"  # Ra8# and Rb8#


# --- EN_PASSANT_CHECKMATE ---

def test_en_passant_discovered_mate(detect):
    [e] = detect(EnPassantCheckmate(), "1... d5 2. exd6#", EP_MATE)
    assert (e.ply, e.color) == (2, "w")
    assert e.metadata == {"square": "d6", "checkers": ["a5"]}


def test_en_passant_check_without_mate(detect):
    assert detect(EnPassantCheckmate(), "1... d5 2. exd6+", EP_CHECK_ONLY) == []


def test_ordinary_capture_mate_is_not_en_passant(detect):
    assert detect(EnPassantCheckmate(), "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#") == []


def test_en_passant_without_check(detect):
    assert detect(EnPassantCheckmate(), "1. e4 a6 2. e5 d5 3. exd6") == []


# --- KING_DELIVERED_MATE ---

def test_castling_mate(detect):
    [e] = detect(KingDeliveredMate(), "1. O-O-O#", CASTLE_MATE)
    assert e.metadata == {"is_castling": True, "checkers": ["d1"]}


def test_discovered_mate_by_king_move(detect):
    [e] = detect(KingDeliveredMate(), "1. Kd7#", KING_DISCOVERY)
    assert e.metadata == {"is_castling": False, "checkers": ["a8"]}


def test_castling_check_without_mate(detect):
    assert detect(KingDeliveredMate(), "1. O-O-O+", CASTLE_CHECK) == []


def test_mate_by_another_piece(detect):
    assert detect(KingDeliveredMate(), "1. Ra8#", BACK_RANK) == []


# --- MISSED_MATE_IN_ONE ---

def test_missed_back_rank_mate(detect):
    [e] = detect(MissedMateInOne(), "1. Kf1", BACK_RANK)
    assert (e.ply, e.color, e.fen) == (1, "w", BACK_RANK)
    assert e.metadata == {"mating_moves": ["Ra8#"], "played": "Kf1"}


def test_all_mating_alternatives_listed(detect):
    [e] = detect(MissedMateInOne(), "1. Kf1", TWO_MATES)
    assert e.metadata["mating_moves"] == ["Ra8#", "Rb8#"]


def test_playing_a_different_mate_is_not_a_miss(detect):
    assert detect(MissedMateInOne(), "1. Rb8#", TWO_MATES) == []


def test_playing_the_mate(detect):
    assert detect(MissedMateInOne(), "1. Ra8#", BACK_RANK) == []


def test_no_mate_available(detect):
    assert detect(MissedMateInOne(), "1. e4 e5 2. Nf3 Nc6") == []


def test_check_that_is_not_mate_is_not_counted(detect):
    # Ra8+ here is only check (h7 is free), so nothing was missed.
    assert detect(MissedMateInOne(), "1. Kf1", "6k1/5pp1/8/8/8/8/8/R5K1 w - - 0 1") == []


def test_black_misses_mate(detect):
    [e] = detect(MissedMateInOne(), "1... Kf8", "r5k1/8/8/8/8/8/5PPP/6K1 b - - 0 1")
    assert e.color == "b" and e.metadata["mating_moves"] == ["Ra1#"]


def test_mate_by_en_passant_and_castling_are_found(detect):
    [ep] = detect(MissedMateInOne(), "1... d5 2. Kg2", EP_MATE)
    assert ep.metadata["mating_moves"] == ["exd6#"]
    [castle] = detect(MissedMateInOne(), "1. Kd2", CASTLE_MATE)
    assert castle.metadata["mating_moves"] == ["O-O-O#", "Rd1#"]  # the rook can also mate on its own


def test_board_is_restored_after_scan(detect):
    from chesstrove.detectors.mates import mating_moves
    import chess

    board = chess.Board(TWO_MATES)
    mating_moves(board)
    assert board.fen() == TWO_MATES and not board.move_stack
