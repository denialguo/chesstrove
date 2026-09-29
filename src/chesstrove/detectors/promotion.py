from chesstrove.detectors.base import Event, event, piece_name
from chesstrove.models import MoveContext


class Underpromotion:
    """Any promotion to a knight, bishop or rook. No judgment on whether it was necessary."""

    id = "UNDERPROMOTION"
    version = 1

    def detect(self, ctx: MoveContext) -> list[Event]:
        f = ctx.facts
        if f.promotion not in ("N", "B", "R"):
            return []
        return [event(ctx, self.id, promotion_piece=piece_name(f.promotion), square=f.to_square,
                      is_capture=f.is_capture, gave_check=f.is_check, gave_mate=f.is_checkmate)]


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
