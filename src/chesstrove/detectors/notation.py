import re

from chesstrove.detectors.base import Event, event, piece_name
from chesstrove.models import MoveContext

# Piece letter, then BOTH origin file and rank, then optional capture, then destination: "Qh4e1", "Nb1xd2+".
DOUBLE_DISAMBIGUATED = re.compile(r"^[NBRQK][a-h][1-8]x?[a-h][1-8]")


class DoubleDisambiguatedSan:
    """The SAN has to name both origin file and rank. Uses python-chess's minimal SAN, never the
    PGN's text, because some sites over-disambiguate. Pawns never qualify."""

    id = "DOUBLE_DISAMBIGUATED_SAN"
    requires = "piece_move"
    tier = "fast"
    version = 1

    def detect(self, ctx: MoveContext) -> list[Event]:
        if not DOUBLE_DISAMBIGUATED.match(ctx.san):
            return []
        return [event(ctx, self.id, san=ctx.san, piece=piece_name(ctx.facts.piece), from_square=ctx.facts.from_square)]
