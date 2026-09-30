import chess

from chesstrove.detectors.base import Event, checker_squares, event
from chesstrove.models import MoveContext


class EnPassantCheckmate:
    """An en passant capture that mates, whether the pawn checks directly or uncovers a line."""

    id = "EN_PASSANT_CHECKMATE"
    requires = "checkmate"
    tier = "fast"
    version = 1

    def detect(self, ctx: MoveContext) -> list[Event]:
        if not (ctx.facts.is_en_passant and ctx.facts.is_checkmate):
            return []
        return [event(ctx, self.id, square=ctx.facts.to_square, checkers=checker_squares(ctx.board_after))]


class KingDeliveredMate:
    """The mating move is a king move. A king never checks by itself, so this is always a discovered
    mate or castling where the rook mates. Castling counts."""

    id = "KING_DELIVERED_MATE"
    requires = "checkmate"
    tier = "fast"
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
    requires = "not_checkmate"
    tier = "deep"
    version = 1

    def detect(self, ctx: MoveContext) -> list[Event]:
        if ctx.facts.is_checkmate:
            return []
        mates = mating_moves(ctx.board_before, ctx.legal_before)
        if not mates:
            return []
        return [event(ctx, self.id, mating_moves=mates, played=ctx.san)]


def mating_moves(board: chess.Board, legal: list[chess.Move] | None = None) -> list[str]:
    """SAN of every legal move that mates, sorted. Pushes and pops, leaving the board as it was.

    Only a checking move can mate, and a move checks only if the moved (or promoted) piece attacks the king from its
    new square, or it uncovers one of the mover's sliders (it was the only piece between that slider and the king),
    or it's castling or en passant (rare: always tried). That is decided with bitboards, exactly, before the one
    expensive step (push, is_checkmate, pop). `legal`: the moves replay already generated, if any.
    """
    king = board.king(not board.turn)
    if king is None:
        return []
    us = board.turn
    ours, occupied = board.occupied_co[us], board.occupied
    # the mover's own pieces that alone stand between one of the mover's sliders and the king
    lines = ((chess.BB_RANK_ATTACKS[king][0] | chess.BB_FILE_ATTACKS[king][0]) & (board.rooks | board.queens)
             | chess.BB_DIAG_ATTACKS[king][0] & (board.bishops | board.queens))
    uncovers = 0
    for sniper in chess.scan_reversed(lines & ours):
        between = chess.between(king, sniper) & occupied
        if between and between & (between - 1) == 0:  # exactly one piece in the way
            uncovers |= between
    uncovers &= ours
    king_file, king_rank = chess.square_file(king), chess.square_rank(king)
    pawn_squares = chess.BB_PAWN_ATTACKS[not us][king]  # where one of our pawns would attack the king
    mates = []
    for move in (legal if legal is not None else board.legal_moves):
        frm, to = move.from_square, move.to_square
        checks = bool(chess.BB_SQUARES[frm] & uncovers) or board.is_castling(move) or board.is_en_passant(move)
        if not checks:
            piece = move.promotion or board.piece_type_at(frm)
            if piece == chess.KNIGHT:
                checks = bool(chess.BB_KNIGHT_ATTACKS[king] & chess.BB_SQUARES[to])
            elif piece == chess.PAWN:
                checks = bool(pawn_squares & chess.BB_SQUARES[to])
            elif piece != chess.KING and chess.BB_RAYS[to][king]:
                diagonal = abs(chess.square_file(to) - king_file) == abs(chess.square_rank(to) - king_rank)
                if (piece == chess.QUEEN or (piece == chess.BISHOP) == diagonal) \
                        and not chess.between(to, king) & (occupied & ~chess.BB_SQUARES[frm]):
                    checks = True
        if not checks:
            continue
        board.push(move)
        mated = board.is_checkmate()
        board.pop()
        if mated:
            mates.append(board.san(move))
    return sorted(mates)
