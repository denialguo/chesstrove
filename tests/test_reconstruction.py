import chess
import pytest

from chesstrove.importers.pgn import read_pgn
from chesstrove.models import CanonicalGame, MoveFacts
from chesstrove.reconstruction import replay


def facts_for(movetext: str, fen: str | None = None, headers: str = "") -> list[MoveFacts]:
    if fen:
        headers += f'[SetUp "1"]\n[FEN "{fen}"]\n'
    [game] = read_pgn(f'{headers}[Result "*"]\n\n{movetext} *\n')
    assert isinstance(game, CanonicalGame), game
    return [ctx.facts for ctx in replay(game)]


def test_scholars_mate_facts():
    f = facts_for("1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#")
    assert [m.ply for m in f] == list(range(1, 8))
    assert [m.color for m in f] == ["w", "b"] * 3 + ["w"]
    assert f[0].legal_moves_before == 20
    assert f[0].fen_before == chess.STARTING_FEN
    assert all(a.fen_after == b.fen_before for a, b in zip(f, f[1:]))
    mate = f[-1]
    assert (mate.san, mate.uci, mate.piece, mate.captured) == ("Qxf7#", "h5f7", "Q", "P")
    assert mate.is_capture and mate.is_check and mate.is_checkmate
    assert (mate.material_white, mate.material_black) == (39, 38)
    assert (mate.queens_before, mate.queens_after) == (2, 2)
    assert not f[0].is_capture and not f[0].is_check


def test_castling():
    f = facts_for("1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5 4. O-O")
    castle = f[-1]
    assert castle.is_castling and castle.piece == "K" and castle.captured is None
    assert (castle.from_square, castle.to_square) == ("e1", "g1")


def test_en_passant():
    f = facts_for("1. e4 a6 2. e5 d5 3. exd6")
    ep = f[-1]
    assert ep.is_en_passant and ep.captured == "P" and ep.to_square == "d6"
    assert ep.material_black == 38


def test_underpromotion_with_check():
    [p] = facts_for("1. a8=N+", fen="8/P1k5/8/8/8/8/8/4K3 w - - 0 1")
    assert p.promotion == "N" and p.piece == "P" and p.is_check and not p.is_checkmate
    assert p.san == "a8=N+"
    assert (p.queens_before, p.queens_after, p.material_white) == (0, 0, 3)


def test_promotion_capture_and_queen_count():
    [p] = facts_for("1. bxa8=Q+", fen="r3k3/1P6/8/8/8/8/8/3QK3 w - - 0 1")
    assert (p.promotion, p.captured, p.queens_before, p.queens_after) == ("Q", "R", 1, 2)
    assert p.is_check


def test_black_to_move_start():
    f = facts_for("1... e5 2. e4", fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1")
    assert [m.color for m in f] == ["b", "w"]


def test_chess960_castling_is_not_a_capture():
    # king b1, rook e1: 960 O-O is encoded as king-takes-rook (b1e1) and must not count as a capture
    [castle] = facts_for("1. O-O", fen="4k3/8/8/8/8/8/8/1K2R3 w E - 0 1", headers='[Variant "Chess960"]\n')
    assert castle.is_castling and castle.captured is None and castle.san == "O-O"
    assert castle.uci == "b1e1" and castle.fen_after.startswith("4k3/8/8/8/8/8/8/5RK1")


def test_board_before_is_a_snapshot():
    [game] = read_pgn('[Result "*"]\n\n1. e4 e5 *\n')
    contexts = []
    for ctx in replay(game):
        assert ctx.board_before.fen() == ctx.facts.fen_before
        assert ctx.board_after.fen() == ctx.facts.fen_after
        assert ctx.board_before.is_legal(ctx.move)
        contexts.append(ctx.board_before)
    assert contexts[0].fen() == chess.STARTING_FEN  # untouched by later pushes


def test_corrupt_stored_moves_raise():
    [game] = read_pgn('[Result "*"]\n\n1. e4 *\n')
    bad = CanonicalGame(**{s: getattr(game, s) for s in CanonicalGame.__slots__} | {"moves_uci": ("e2e4", "e2e4")})
    with pytest.raises(chess.IllegalMoveError):
        list(replay(bad))
