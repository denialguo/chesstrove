"""Core, source-independent data types."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import chess

Source = Literal["pgn", "chesscom", "lichess"]


@dataclass(frozen=True, slots=True)
class CanonicalGame:
    """One game as every importer must produce it, before it has a database id."""

    source: Source  # which importer brought it in
    # Global dedupe key: "chesscom:<id>", "lichess:<id>", or "sha256:<hash of normalized movetext+headers>".
    # Same game imported from a PGN export and from the API collapses to one row.
    source_key: str
    external_id: str | None
    played_at: datetime | None
    white: str | None
    black: str | None
    white_rating: int | None
    black_rating: int | None
    result: str
    time_control: str | None
    rated: bool | None
    eco: str | None
    opening: str | None
    initial_fen: str | None  # None means the standard starting position
    moves_uci: tuple[str, ...]  # mainline, already validated by the parser
    pgn: str  # raw text exactly as received
    chess960: bool = False


@dataclass(frozen=True, slots=True)
class MoveFacts:
    """Deterministic facts about one ply, computed once and shared by storage and detectors."""

    ply: int  # 1-based
    color: Literal["w", "b"]
    san: str
    uci: str
    piece: str  # uppercase piece letter: P N B R Q K
    from_square: str
    to_square: str
    captured: str | None  # uppercase piece letter, or None
    is_check: bool
    is_checkmate: bool
    is_castling: bool
    is_en_passant: bool
    promotion: str | None  # uppercase piece letter, or None
    fen_before: str | None  # None when replayed with fens=False (indexing: see MoveContext.board_before.fen())
    fen_after: str | None
    queens_before: int
    queens_after: int
    material_white: int  # after the move, P=1 N=3 B=3 R=5 Q=9
    material_black: int
    legal_moves_before: int

    @property
    def is_capture(self) -> bool:
        return self.captured is not None


@dataclass(frozen=True, slots=True)
class MoveContext:
    """What every detector receives. Detectors may push/pop on the boards but must restore them."""

    game: CanonicalGame
    ply: int
    board_before: chess.Board
    move: chess.Move
    board_after: chess.Board
    san: str
    facts: MoveFacts
    legal_before: list[chess.Move] | None = None  # the mover's legal moves, already generated to count them
