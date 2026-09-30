"""The indexer as it was before gating, lazy FENs and the faster missed-mate scan (September 2026), frozen as the
reference those optimisations must match exactly: replay() materialises a FEN on every ply, every detector sees
every ply, and MISSED_MATE_IN_ONE uses the original candidate filter. Only tests import this."""

from collections.abc import Iterator

import chess

from chesstrove.detectors import DETECTORS
from chesstrove.detectors.base import Event
from chesstrove.models import CanonicalGame, MoveContext, MoveFacts
from chesstrove.reconstruction import material, queen_count, start_board


def replay(game: CanonicalGame) -> Iterator[MoveContext]:
    """Replay the mainline once, yielding a context per ply.

    board_after is the live board and is only valid until the next iteration.
    """
    board = start_board(game)
    fen_before = board.fen()
    queens_before = queen_count(board)
    for ply, uci in enumerate(game.moves_uci, start=1):
        move = board.parse_uci(uci)  # raises IllegalMoveError on corrupt input

        # ponytail: one Board.copy per ply (~µs); switch detectors to push/pop on one board if the benchmark says so
        board_before = board.copy(stack=False)
        legal_moves_before = board.legal_moves.count()
        san = board.san(move)
        piece = board.piece_type_at(move.from_square)
        is_en_passant = board.is_en_passant(move)
        captured = chess.PAWN if is_en_passant else board.piece_type_at(move.to_square)
        is_castling = board.is_castling(move)
        if is_castling:  # castling is encoded as king-takes-own-rook in 960; never a capture
            captured = None
        color = "w" if board.turn == chess.WHITE else "b"

        board.push(move)

        fen_after = board.fen()
        queens_after = queen_count(board)
        facts = MoveFacts(
            ply=ply,
            color=color,
            san=san,
            uci=uci,
            piece=chess.piece_symbol(piece).upper(),
            from_square=chess.square_name(move.from_square),
            to_square=chess.square_name(move.to_square),
            captured=chess.piece_symbol(captured).upper() if captured else None,
            is_check=san[-1] in "+#",  # san() already did the check/mate test; don't regenerate moves
            is_checkmate=san[-1] == "#",
            is_castling=is_castling,
            is_en_passant=is_en_passant,
            promotion=chess.piece_symbol(move.promotion).upper() if move.promotion else None,
            fen_before=fen_before,
            fen_after=fen_after,
            queens_before=queens_before,
            queens_after=queens_after,
            material_white=material(board, chess.WHITE),
            material_black=material(board, chess.BLACK),
            legal_moves_before=legal_moves_before,
        )
        yield MoveContext(game, ply, board_before, move, board, san, facts)
        fen_before, queens_before = fen_after, queens_after


def reference_mating_moves(board: chess.Board) -> list[str]:
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


def analyze(game: CanonicalGame, detectors=DETECTORS) -> tuple[list[MoveFacts], list]:
    facts, events = [], []
    for ctx in replay(game):
        facts.append(ctx.facts)
        for d in detectors:
            if d.id == "MISSED_MATE_IN_ONE":
                if ctx.facts.is_checkmate:
                    continue
                mates = reference_mating_moves(ctx.board_before)
                if mates:
                    events.append((d, Event(d.id, ctx.ply, ctx.facts.color, ctx.facts.fen_before,
                                            {"mating_moves": mates, "played": ctx.san})))
                continue
            events.extend((d, e) for e in d.detect(ctx))
    return facts, events
