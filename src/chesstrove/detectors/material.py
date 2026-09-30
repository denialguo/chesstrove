import chess

from chesstrove.detectors.base import Event, event
from chesstrove.models import MoveContext


class ThreePlusQueens:
    """Total queens on the board goes from under 3 to 3 or more on this ply. Fires again if the count
    drops below 3 and later comes back."""

    id = "THREE_PLUS_QUEENS"
    requires = "three_queens"
    tier = "fast"
    version = 1

    def detect(self, ctx: MoveContext) -> list[Event]:
        f = ctx.facts
        if not (f.queens_before < 3 <= f.queens_after):
            return []
        board = ctx.board_after
        return [event(ctx, self.id, total=f.queens_after,
                      white=chess.popcount(board.queens & board.occupied_co[chess.WHITE]),
                      black=chess.popcount(board.queens & board.occupied_co[chess.BLACK]))]
