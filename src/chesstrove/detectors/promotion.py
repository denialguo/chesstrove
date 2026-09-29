import chess

from chesstrove.detectors.base import Event, event, piece_name
from chesstrove.models import MoveContext


class Underpromotion:
    """Any promotion to a knight, bishop or rook. Also records exact facts about queening on the same
    square instead (check / mate / stalemate). Those facts never claim which move was best; that
    needs the engine layer."""

    id = "UNDERPROMOTION"
    version = 2  # v2: queen_gives_check, queen_gives_mate, queen_stalemates

    def detect(self, ctx: MoveContext) -> list[Event]:
        f = ctx.facts
        if f.promotion not in ("N", "B", "R"):
            return []
        board = ctx.board_before
        board.push(chess.Move(ctx.move.from_square, ctx.move.to_square, chess.QUEEN))
        queen = {"queen_gives_check": board.is_check(), "queen_gives_mate": board.is_checkmate(),
                 "queen_stalemates": board.is_stalemate()}
        board.pop()
        return [event(ctx, self.id, promotion_piece=piece_name(f.promotion), square=f.to_square,
                      is_capture=f.is_capture, gave_check=f.is_check, gave_mate=f.is_checkmate, **queen)]


class PromotionCheckmate:
    """A promotion (to any piece) that mates, including discovered mates where the new piece doesn't check."""

    id = "PROMOTION_CHECKMATE"
    version = 1

    def detect(self, ctx: MoveContext) -> list[Event]:
        f = ctx.facts
        if not (f.promotion and f.is_checkmate):
            return []
        promoted_piece_checks = ctx.move.to_square in ctx.board_after.checkers()
        return [event(ctx, self.id, promotion_piece=piece_name(f.promotion), square=f.to_square,
                      promoted_piece_checks=promoted_piece_checks)]
