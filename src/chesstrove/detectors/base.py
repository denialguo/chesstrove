"""The detector contract. A detector sees one MoveContext at a time and returns zero or more Events.

Detectors are stateless and deterministic. "Once per game" conditions are written as transitions
(before < threshold <= after) so no per-game state is needed.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

import chess

from chesstrove.models import MoveContext


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    ply: int
    color: str  # side that played the move
    fen: str  # position before the move
    metadata: dict[str, Any] = field(default_factory=dict)


class Detector(Protocol):
    id: str  # stable; stored on every event
    version: int  # bump when the definition changes; `chesstrove reanalyze` then redoes stale games

    def detect(self, ctx: MoveContext) -> list[Event]: ...


def event(ctx: MoveContext, type: str, **metadata: Any) -> Event:
    return Event(type, ctx.ply, ctx.facts.color, ctx.facts.fen_before, metadata)


def piece_name(letter: str) -> str:
    return chess.piece_name(chess.PIECE_SYMBOLS.index(letter.lower()))


def checker_squares(board: chess.Board) -> list[str]:
    return [chess.square_name(s) for s in board.checkers()]
