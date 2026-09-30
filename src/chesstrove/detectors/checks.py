from chesstrove.detectors.base import Event, checker_squares, event
from chesstrove.models import MoveContext


class DoubleCheck:
    """Two or more pieces give check after the move. Every double check is also a discovered check,
    so one type covers "discovered double check" too."""

    id = "DOUBLE_CHECK"
    requires = "check"
    tier = "fast"
    version = 1

    def detect(self, ctx: MoveContext) -> list[Event]:
        if not ctx.facts.is_check:
            return []
        checkers = checker_squares(ctx.board_after)
        if len(checkers) < 2:
            return []
        return [event(ctx, self.id, checkers=checkers, is_checkmate=ctx.facts.is_checkmate)]
