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
    """A knight gives mate while every on-board square next to the mated king is occupied by that
    king's own pieces. The knight may be part of a double check."""

    id = "SMOTHERED_MATE"
    version = 1

    def detect(self, ctx: MoveContext) -> list[Event]:
        found = _mated_king(ctx)
        if not found:
            return []
        board, mated, king = found
        if not board.checkers() & board.knights:
            return []
        if chess.BB_KING_ATTACKS[king] & ~board.occupied_co[mated]:
            return []  # some neighbouring square is empty or holds an enemy piece
        return [event(ctx, self.id, king_square=chess.square_name(king), checkers=checker_squares(board))]


class BackRankMate:
    """The mated king is on its own back rank, a rook or queen checks it along that rank, and every
    square next to the king on the following rank is occupied by the king's own pieces. Escape squares
    that are merely attacked don't count."""

    id = "BACK_RANK_MATE"
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
