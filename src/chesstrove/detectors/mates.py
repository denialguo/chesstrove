import chess

from chesstrove.detectors.base import Event, checker_squares, event
from chesstrove.models import MoveContext


class EnPassantCheckmate:
    """An en passant capture that mates, whether the pawn checks directly or uncovers a line."""

    id = "EN_PASSANT_CHECKMATE"
    version = 1

    def detect(self, ctx: MoveContext) -> list[Event]:
        if not (ctx.facts.is_en_passant and ctx.facts.is_checkmate):
            return []
        return [event(ctx, self.id, square=ctx.facts.to_square, checkers=checker_squares(ctx.board_after))]


class KingDeliveredMate:
    """The mating move is a king move. A king never checks by itself, so this is always a discovered
    mate or castling where the rook mates. Castling counts."""

    id = "KING_DELIVERED_MATE"
    version = 1

    def detect(self, ctx: MoveContext) -> list[Event]:
        if not (ctx.facts.piece == "K" and ctx.facts.is_checkmate):
            return []
        return [event(ctx, self.id, is_castling=ctx.facts.is_castling, checkers=checker_squares(ctx.board_after))]


class MissedMateInOne:
    """The mover had at least one mate in one and played something else.

    Only moves actually played are judged: a mate left on the board when the game ended by
    resignation or timeout isn't reported. Each missed ply is its own event.
    """

    id = "MISSED_MATE_IN_ONE"
    version = 1

    def detect(self, ctx: MoveContext) -> list[Event]:
        if ctx.facts.is_checkmate:
            return []
        mates = mating_moves(ctx.board_before)
        if not mates:
            return []
        return [event(ctx, self.id, mating_moves=mates, played=ctx.san)]


def mating_moves(board: chess.Board) -> list[str]:
    """SAN of every legal move that mates. Pushes and pops, leaving the board as it was."""
    king = board.king(not board.turn)
    if king is None:
        return []
    # A move can only check if it lands on a line/knight-jump to the king, or leaves a line to it
    # (discovery). Castling and en passant are rare; let gives_check handle them.
    rays, knight_jumps = chess.BB_RAYS[king], chess.BB_KNIGHT_ATTACKS[king]
    mates = []
    for move in board.legal_moves:
        could_check = (
            rays[move.from_square] or rays[move.to_square] or knight_jumps & chess.BB_SQUARES[move.to_square]
            or board.is_castling(move) or board.is_en_passant(move)
        )
        if not could_check:
            continue
        # ponytail: gives_check is push/pop (~3µs); ~10 candidates/ply but <1 checks. A bitboard check
        # test would halve this detector's cost (~57µs/ply now) if full-history analysis gets slow.
        if not board.gives_check(move):  # exact test; mate needs check
            continue
        board.push(move)
        mated = board.is_checkmate()
        board.pop()
        if mated:
            mates.append(board.san(move))
    return sorted(mates)
