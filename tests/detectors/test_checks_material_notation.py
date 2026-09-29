from chesstrove.detectors.checks import DoubleCheck
from chesstrove.detectors.material import ThreePlusQueens
from chesstrove.detectors.notation import DoubleDisambiguatedSan

# Ne4 blocks the e1 rook's file; Nf6 checks from f6 and uncovers the rook.
DOUBLE_CHECK = "4k3/8/8/8/4N3/8/8/4R1K1 w - - 0 1"
DOUBLE_CHECK_MATE = "3qkb2/3p1p2/8/8/4N3/8/8/4R1K1 w - - 0 1"
THREE_QUEENS_TO_BE = "4k3/P7/8/8/8/8/8/Q2QK3 w - - 0 1"
# Queens h4, e4, h1: h4->e1 shares a file with h1 and a rank with e4, so SAN must be Qh4e1.
THREE_QUEEN_GEOMETRY = "8/8/1k6/8/4Q2Q/8/8/K6Q w - - 0 1"


# --- DOUBLE_CHECK ---

def test_discovered_double_check(detect):
    [e] = detect(DoubleCheck(), "1. Nf6+", DOUBLE_CHECK)
    assert e.metadata == {"checkers": ["e1", "f6"], "is_checkmate": False}


def test_double_check_mate(detect):
    [e] = detect(DoubleCheck(), "1. Nf6#", DOUBLE_CHECK_MATE)
    assert e.metadata["is_checkmate"]


def test_single_discovered_check(detect):
    assert detect(DoubleCheck(), "1. Nc3+", DOUBLE_CHECK) == []  # only the rook checks


def test_direct_single_check(detect):
    assert detect(DoubleCheck(), "1. Nd6+", DOUBLE_CHECK.replace("4R1K1", "6K1")) == []


# --- THREE_PLUS_QUEENS ---

def test_third_queen_appears(detect):
    [e] = detect(ThreePlusQueens(), "1. a8=Q+", THREE_QUEENS_TO_BE)
    assert e.metadata == {"total": 3, "white": 3, "black": 0}


def test_fires_on_transition_only(detect):
    assert detect(ThreePlusQueens(), "1. a8=Q+ Kf7 2. Q8a7+", THREE_QUEENS_TO_BE) == detect(
        ThreePlusQueens(), "1. a8=Q+", THREE_QUEENS_TO_BE)  # later plies with 3 queens add nothing


def test_capture_promotion_that_keeps_count_at_three(detect):
    # 3 queens before (b8, a1, h1); axb8=Q captures one and makes one: still 3, no transition.
    assert detect(ThreePlusQueens(), "1. axb8=Q+", "1q2k3/P7/8/8/8/8/8/Q3K2Q w - - 0 1") == []


def test_underpromotion_does_not_add_a_queen(detect):
    assert detect(ThreePlusQueens(), "1. a8=N", THREE_QUEENS_TO_BE) == []


def test_fires_again_after_dropping_below_three(detect):
    # White promotes (2 -> 3), Black's queen takes it (3 -> 2), White promotes again (2 -> 3).
    events = detect(ThreePlusQueens(), "1. a8=Q Qxa8 2. b8=Q", "2q1k3/PP6/8/8/8/8/8/Q3K3 w - - 0 1")
    assert [e.ply for e in events] == [1, 3]


# --- DOUBLE_DISAMBIGUATED_SAN ---

def test_double_disambiguation(detect):
    [e] = detect(DoubleDisambiguatedSan(), "1. Qh4e1", THREE_QUEEN_GEOMETRY)
    assert e.metadata == {"san": "Qh4e1", "piece": "queen", "from_square": "h4"}


def test_file_only_disambiguation(detect):
    assert detect(DoubleDisambiguatedSan(), "1. Rae1", "4k3/8/8/8/8/8/8/R4RK1 w - - 0 1") == []


def test_pawn_capture_is_not_disambiguation(detect):
    assert detect(DoubleDisambiguatedSan(), "1. e4 d5 2. exd5") == []


def test_over_disambiguated_pgn_text_is_ignored(detect):
    # Only one queen can reach e1, so the minimal SAN is Qe1 even though the PGN says Qh4e1.
    assert detect(DoubleDisambiguatedSan(), "1. Qh4e1", "8/8/1k6/8/7Q/8/8/K7 w - - 0 1") == []
