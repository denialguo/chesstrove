from datetime import UTC, datetime

from chesstrove.importers.pgn import ParseFailure, read_pgn
from chesstrove.models import CanonicalGame

CHESSCOM = """[Event "Live Chess"]
[Site "Chess.com"]
[Date "2024.03.09"]
[White "alice"]
[Black "bob"]
[Result "1-0"]
[WhiteElo "1500"]
[BlackElo "1480"]
[TimeControl "180+2"]
[ECO "C20"]
[UTCDate "2024.03.10"]
[UTCTime "01:02:03"]
[Link "https://www.chess.com/game/live/104857600"]

1. e4 {[%clk 0:03:01]} e5 {[%clk 0:03:00]} 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0
"""

LICHESS = """[Event "Rated Blitz game"]
[Site "https://lichess.org/AbCdEfGh"]
[White "carol"]
[Black "dave"]
[Result "0-1"]
[WhiteElo "?"]
[Opening "Bird Opening"]

1. f4 e5 0-1
"""

PLAIN = """[Event "Club"]
[White "x"]
[Black "y"]
[Result "*"]
[Date "????.??.??"]

1. d4 d5 *
"""


def only(text: str) -> CanonicalGame:
    [item] = list(read_pgn(text))
    assert isinstance(item, CanonicalGame), item
    return item


def test_chesscom_metadata():
    g = only(CHESSCOM)
    assert g.source == "pgn"
    assert g.source_key == "chesscom:104857600"
    assert g.external_id == "104857600"
    assert g.played_at == datetime(2024, 3, 10, 1, 2, 3, tzinfo=UTC)  # UTC headers win over local Date
    assert (g.white, g.black, g.white_rating, g.black_rating) == ("alice", "bob", 1500, 1480)
    assert (g.result, g.time_control, g.eco, g.rated) == ("1-0", "180+2", "C20", None)
    assert g.moves_uci == ("e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7")
    assert g.initial_fen is None
    assert g.pgn == CHESSCOM.strip()


def test_lichess_metadata():
    g = only(LICHESS)
    assert g.source_key == "lichess:AbCdEfGh"
    assert g.rated is True
    assert g.white_rating is None  # "?"
    assert g.opening == "Bird Opening"
    assert g.played_at is None


def test_plain_pgn_hash_key_ignores_comments_and_formatting():
    a = only(PLAIN)
    b = only(PLAIN.replace("1. d4 d5 *", "1.d4 {a comment} 1... d5 (1... Nf6) *"))
    assert a.source_key.startswith("sha256:")
    assert a.source_key == b.source_key
    assert a.source_key != only(PLAIN.replace("d5", "Nf6")).source_key


def test_multi_game_file_isolates_bad_game():
    bad = '[Event "bad"]\n[Result "*"]\n\n1. e4 e5 2. Ke3 *\n'  # illegal king move
    items = list(read_pgn(f"{CHESSCOM}\n{bad}\n{LICHESS}"))
    assert [type(i).__name__ for i in items] == ["CanonicalGame", "ParseFailure", "CanonicalGame"]
    assert "Ke3" in items[1].error
    assert items[1].pgn == bad.strip()
    assert items[2].pgn == LICHESS.strip()


def test_garbage_and_empty_input():
    assert list(read_pgn("")) == []
    [item] = read_pgn("this is not a pgn at all")
    assert isinstance(item, ParseFailure)
    [aborted] = read_pgn('[Event "aborted"]\n[Result "*"]\n\n*\n')  # tags but no moves is still a game
    assert isinstance(aborted, CanonicalGame) and aborted.moves_uci == ()


def test_invalid_fen_header():
    [item] = read_pgn('[SetUp "1"]\n[FEN "not a fen"]\n\n1. e4 *\n')
    assert isinstance(item, ParseFailure) and not item.skipped  # broken, not merely unsupported


def test_custom_start_position():
    fen = "4k3/P7/8/8/8/8/8/4K3 w - - 0 1"
    g = only(f'[SetUp "1"]\n[FEN "{fen}"]\n[Result "*"]\n\n1. a8=N *\n')
    assert g.initial_fen == fen
    assert g.moves_uci == ("a7a8n",)


def test_unsupported_variant_is_rejected():
    [item] = read_pgn('[Variant "Crazyhouse"]\n[Result "*"]\n\n1. e4 *\n')
    assert isinstance(item, ParseFailure) and "crazyhouse" in item.error and item.skipped


def test_chess960_is_supported():
    fen = "bqnbrkrn/pppppppp/8/8/8/8/PPPPPPPP/BQNBRKRN w GEge - 0 1"
    g = only(f'[Variant "Chess960"]\n[SetUp "1"]\n[FEN "{fen}"]\n[Result "*"]\n\n1. e4 e5 *\n')
    assert g.chess960 and g.initial_fen == "bqnbrkrn/pppppppp/8/8/8/8/PPPPPPPP/BQNBRKRN w KQkq - 0 1"  # normalized
