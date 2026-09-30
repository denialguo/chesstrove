"""Named mating patterns, defined only by the final position's geometry."""

import chess

from chesstrove.detectors.base import Event, checker_squares, event
from chesstrove.models import MoveContext


def _mated_king(ctx: MoveContext) -> tuple[chess.Board, chess.Color, chess.Square] | None:
    board = ctx.board_after
    if not ctx.facts.is_checkmate:
        return None
    mated = board.turn
    return board, mated, board.king(mated)


class SmotheredMate:
    """A knight gives mate, and each square next to the mated king is either held by the king's own
    pieces or covered by the mating knight itself: the knight alone does the work, no other piece helps
    trap the king. `pure` = every neighbouring square is the king's own piece (the textbook picture).
    The knight may be part of a double check."""

    id = "SMOTHERED_MATE"
    requires = "checkmate"
    tier = "fast"
    version = 2  # v2: squares covered by the mating knight itself count (v1 required all to be own pieces)

    def detect(self, ctx: MoveContext) -> list[Event]:
        found = _mated_king(ctx)
        if not found:
            return []
        board, mated, king = found
        mating_knights = board.checkers() & board.knights
        if not mating_knights:
            return []
        knight_cover = 0
        for square in mating_knights:
            knight_cover |= chess.BB_KNIGHT_ATTACKS[square]
        open_squares = chess.BB_KING_ATTACKS[king] & ~board.occupied_co[mated]
        if open_squares & ~knight_cover:
            return []  # some escape square is taken away by another piece, not the knight
        return [event(ctx, self.id, king_square=chess.square_name(king), checkers=checker_squares(board),
                      pure=not open_squares)]


class BackRankMate:
    """The mated king is on its own back rank, a rook or queen checks it along that rank, and every
    square next to the king on the following rank is occupied by the king's own pieces. Escape squares
    that are merely attacked don't count."""

    id = "BACK_RANK_MATE"
    requires = "checkmate"
    tier = "fast"
    version = 1

    def detect(self, ctx: MoveContext) -> list[Event]:
        found = _mated_king(ctx)
        if not found:
            return []
        board, mated, king = found
        back_rank = 0 if mated == chess.WHITE else 7
        if chess.square_rank(king) != back_rank:
            return []
        rank_checkers = [s for s in board.checkers()
                         if chess.square_rank(s) == back_rank and board.piece_type_at(s) in (chess.ROOK, chess.QUEEN)]
        if not rank_checkers:
            return []
        next_rank = chess.BB_RANKS[1 if mated == chess.WHITE else 6]
        if chess.BB_KING_ATTACKS[king] & next_rank & ~board.occupied_co[mated]:
            return []
        return [event(ctx, self.id, king_square=chess.square_name(king),
                      checker=chess.square_name(rank_checkers[0]),
                      checker_piece=chess.piece_name(board.piece_type_at(rank_checkers[0])))]
