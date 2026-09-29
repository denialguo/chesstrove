"""One-pass replay of a game's mainline into MoveContexts."""

from collections.abc import Iterator

import chess

from chesstrove.models import CanonicalGame, MoveContext, MoveFacts

PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}


def material(board: chess.Board, color: chess.Color) -> int:
    return sum(v * chess.popcount(board.pieces_mask(pt, color)) for pt, v in PIECE_VALUES.items())


def queen_count(board: chess.Board) -> int:
    return chess.popcount(board.queens)


def start_board(game: CanonicalGame) -> chess.Board:
    return chess.Board(game.initial_fen or chess.STARTING_FEN, chess960=game.chess960)


def replay(game: CanonicalGame) -> Iterator[MoveContext]:
    """Replay the mainline once, yielding a context per ply.

    board_after is the live board and is only valid until the next iteration.
    """
    board = start_board(game)
    fen_before = board.fen()
    queens_before = queen_count(board)
    for ply, uci in enumerate(game.moves_uci, start=1):
        move = board.parse_uci(uci)  # raises IllegalMoveError on corrupt input

        # ponytail: one Board.copy per ply (~µs); switch detectors to push/pop on one board if the benchmark says so
        board_before = board.copy(stack=False)
        legal_moves_before = board.legal_moves.count()
        san = board.san(move)
        piece = board.piece_type_at(move.from_square)
        is_en_passant = board.is_en_passant(move)
        captured = chess.PAWN if is_en_passant else board.piece_type_at(move.to_square)
        is_castling = board.is_castling(move)
        if is_castling:  # castling is encoded as king-takes-own-rook in 960; never a capture
            captured = None
        color = "w" if board.turn == chess.WHITE else "b"

        board.push(move)

        fen_after = board.fen()
        queens_after = queen_count(board)
        facts = MoveFacts(
            ply=ply,
            color=color,
            san=san,
            uci=uci,
            piece=chess.piece_symbol(piece).upper(),
            from_square=chess.square_name(move.from_square),
            to_square=chess.square_name(move.to_square),
            captured=chess.piece_symbol(captured).upper() if captured else None,
            is_check=san[-1] in "+#",  # san() already did the check/mate test; don't regenerate moves
            is_checkmate=san[-1] == "#",
            is_castling=is_castling,
            is_en_passant=is_en_passant,
            promotion=chess.piece_symbol(move.promotion).upper() if move.promotion else None,
            fen_before=fen_before,
            fen_after=fen_after,
            queens_before=queens_before,
            queens_after=queens_after,
            material_white=material(board, chess.WHITE),
            material_black=material(board, chess.BLACK),
            legal_moves_before=legal_moves_before,
        )
        yield MoveContext(game, ply, board_before, move, board, san, facts)
        fen_before, queens_before = fen_after, queens_after
