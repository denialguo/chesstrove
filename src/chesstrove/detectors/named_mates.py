"""Named mating patterns (Epaulette, Anastasia's, Boden's, ...), by strict canonical geometry.

Every piece a pattern names has to do its characteristic job in the final position (give the check,
guard the checker, cover a flight square), not merely be on the board. Patterns hold mirrored left to right
and for either colour, and one mate may carry several names. Only single-check mates qualify: every
textbook picture here has one checker.

Vocabulary (all computed on the mating position):
  king      the mated king; its neighbours are its flight squares
  own       neighbours held by the mated side's own pieces
  free      every other neighbour; since it's mate, each is covered by the attacker
  cover[s]  the attacker's pieces controlling neighbour s, with the king lifted off the board (so a
            slider checking the king also covers the square behind it); for the checker's own square
            that is its defenders
  inward    unit step from an edge square toward the centre (a corner has two)
"""

from collections.abc import Callable
from dataclasses import dataclass

import chess

from chesstrove.detectors.base import Event, event
from chesstrove.models import MoveContext

Vec = tuple[int, int]


def _off(square: chess.Square, dx: int, dy: int) -> chess.Square | None:
    f, r = chess.square_file(square) + dx, chess.square_rank(square) + dy
    return chess.square(f, r) if 0 <= f < 8 and 0 <= r < 8 else None


def _sign(v: int) -> int:
    return (v > 0) - (v < 0)


def _inward(square: chess.Square) -> list[Vec]:
    f, r = chess.square_file(square), chess.square_rank(square)
    return [v for v, edge in (((1, 0), f == 0), ((-1, 0), f == 7), ((0, 1), r == 0), ((0, -1), r == 7)) if edge]


def _perp(v: Vec) -> Vec:
    return v[1], v[0]


@dataclass(frozen=True)
class Mate:
    board: chess.Board
    mated: chess.Color
    king: chess.Square
    checker: chess.Square
    piece: chess.PieceType  # the checker's type
    d: Vec  # unit step from the king toward the checker
    dist: int  # king-move distance to the checker
    own: frozenset[chess.Square]
    free: frozenset[chess.Square]
    cover: dict[chess.Square, chess.SquareSet]  # every neighbour, own ones included

    def kind(self, square: chess.Square) -> chess.PieceType | None:
        """The attacker's piece type on `square`, if the attacker has a piece there."""
        p = self.board.piece_at(square)
        return p.piece_type if p and p.color != self.mated else None

    def attackers(self, square: chess.Square) -> chess.SquareSet:
        lifted = self.board.copy(stack=False)
        lifted.remove_piece_at(self.king)
        return lifted.attackers(not self.mated, square)

    def defenders(self, square: chess.Square, *types: chess.PieceType) -> list[chess.Square]:
        return [a for a in self.attackers(square) if self.kind(a) in types]

    def orthogonal(self) -> bool:
        return 0 in self.d

    def along_edge(self) -> Vec | None:
        """The inward vector of the edge the king sits on and is checked along, if any."""
        return next((v for v in _inward(self.king) if self.orthogonal() and self.d[0] * v[0] + self.d[1] * v[1] == 0), None)

    def rest(self) -> list[chess.Square]:
        """Free squares the checker itself doesn't cover: the ones the helpers must take away. (The
        checker's own square doesn't count; mate already means it's guarded.)"""
        return [s for s in self.free if s != self.checker and self.checker not in self.cover[s]]


def anatomy(ctx: MoveContext) -> Mate | None:
    if not ctx.facts.is_checkmate:
        return None
    board = ctx.board_after
    mated = board.turn
    king = board.king(mated)
    checkers = board.checkers()
    if len(checkers) != 1:
        return None
    checker = checkers.pop()
    dx = chess.square_file(checker) - chess.square_file(king)
    dy = chess.square_rank(checker) - chess.square_rank(king)
    lifted = board.copy(stack=False)
    lifted.remove_piece_at(king)
    neighbours = chess.SquareSet(chess.BB_KING_ATTACKS[king])
    own = frozenset(s for s in neighbours if board.color_at(s) == mated)
    return Mate(board, mated, king, checker, board.piece_type_at(checker), (_sign(dx), _sign(dy)),
                max(abs(dx), abs(dy)), own, frozenset(neighbours) - own,
                {s: lifted.attackers(not mated, s) for s in neighbours})


# --- the patterns: each takes the mate's anatomy and says whether the picture is there -------------

def epaulette(m: Mate) -> bool:
    """A queen checks head-on (orthogonally, from at least two squares away); both squares beside the
    king, across the line of check, hold its own pieces; the queen alone covers everything else."""
    if m.piece != chess.QUEEN or not m.orthogonal() or m.dist < 2:
        return False
    px, py = _perp(m.d)
    sides = [_off(m.king, px, py), _off(m.king, -px, -py)]
    return all(s in m.own for s in sides) and all(m.checker in m.cover[s] for s in m.free)


def swallows_tail(m: Mate) -> bool:
    """A queen mates from an orthogonally adjacent square; the two diagonal squares behind the king,
    which the queen can't reach, hold its own pieces."""
    if m.piece != chess.QUEEN or not m.orthogonal() or m.dist != 1:
        return False
    (dx, dy), (px, py) = m.d, _perp(m.d)
    rear = [_off(m.king, -dx + px, -dy + py), _off(m.king, -dx - px, -dy - py)]
    return all(s in m.own for s in rear)


def dovetail(m: Mate) -> bool:
    """A queen mates from a diagonally adjacent square; the two squares beside the king on the far
    side from the queen (the ones it can't reach) hold the king's own pieces."""
    if m.piece != chess.QUEEN or m.orthogonal() or m.dist != 1:
        return False
    dx, dy = m.d
    return _off(m.king, -dx, 0) in m.own and _off(m.king, 0, -dy) in m.own


def anastasia(m: Mate) -> bool:
    """A rook or queen mates along the edge the king stands on; the square straight in from the king
    holds its own piece; knights cover every flight square the checker doesn't."""
    v = m.along_edge()
    if m.piece not in (chess.ROOK, chess.QUEEN) or not v or _off(m.king, *v) not in m.own:
        return False
    rest = m.rest()
    return bool(rest) and all(any(m.kind(a) == chess.KNIGHT for a in m.cover[s]) for s in rest)


def arabian(m: Mate) -> bool:
    """A rook mates from an adjacent square, guarded by a knight that also covers every flight square
    the rook doesn't."""
    if m.piece != chess.ROOK or m.dist != 1:
        return False
    rest = m.rest()
    return bool(rest) and any(all(n in m.cover[s] for s in rest) for n in m.defenders(m.checker, chess.KNIGHT))


def boden(m: Mate) -> bool:
    """A bishop mates; a second bishop, on the other colour, covers flight squares the first can't; the
    two bishops between them cover every flight square, and the king's own pieces hem it in."""
    if m.piece != chess.BISHOP or not m.own:
        return False
    if not all(any(m.kind(a) == chess.BISHOP for a in m.cover[s]) for s in m.free):
        return False
    other = [a for s in m.rest() for a in m.cover[s]
             if m.kind(a) == chess.BISHOP and (chess.BB_SQUARES[a] & chess.BB_LIGHT_SQUARES) != (chess.BB_SQUARES[m.checker] & chess.BB_LIGHT_SQUARES)]
    return bool(other)


def _diagonal_guards(m: Mate, square: chess.Square) -> list[chess.Square]:
    """Bishops, or queens along a diagonal, defending `square`."""
    return [a for a in m.defenders(square, chess.BISHOP, chess.QUEEN)
            if m.kind(a) == chess.BISHOP or chess.square_file(a) != chess.square_file(square)
            and chess.square_rank(a) != chess.square_rank(square)]


def opera(m: Mate) -> bool:
    """A rook mates from an adjacent square on the king's edge, guarded by a bishop that also takes
    away another flight square (Morphy's Opera game: Rd8#, Bg5 guards d8 and covers e7). A queen in
    the bishop's place doesn't count: that's an ordinary queen-and-rook mate."""
    if m.piece != chess.ROOK or m.dist != 1 or not m.along_edge():
        return False
    return any(any(g in m.cover[s] for s in m.free if s != m.checker) for g in m.defenders(m.checker, chess.BISHOP))


def anderssen(m: Mate) -> bool:
    """A rook or queen mates from the corner next to the king, guarded diagonally by a pawn that also
    covers another of the king's flight squares."""
    if m.piece not in (chess.ROOK, chess.QUEEN) or m.dist != 1 or len(_inward(m.checker)) != 2:
        return False
    return any(any(p in m.cover[s] for s in m.free if s != m.checker) for p in m.defenders(m.checker, chess.PAWN))


def lolli(m: Mate) -> bool:
    """A queen mates from the square directly in front of an edge king (Qg7# against g8), guarded by
    a pawn."""
    return (m.piece == chess.QUEEN and m.dist == 1 and m.d in _inward(m.king)
            and bool(m.defenders(m.checker, chess.PAWN)))


def damiano(m: Mate) -> bool:
    """A queen mates from a square diagonally in front of an edge king (Qh7# against g8), guarded by a
    pawn."""
    if m.piece != chess.QUEEN or m.dist != 1 or m.orthogonal():
        return False
    ahead = any(m.d[0] * v[0] + m.d[1] * v[1] == 1 and _off(m.king, *v) is not None for v in _inward(m.king))
    return ahead and len(_inward(m.king)) == 1 and bool(m.defenders(m.checker, chess.PAWN))


def morphy(m: Mate) -> bool:
    """Against a cornered king, a bishop mates along the long diagonal; one of the two edge squares
    beside the king holds its own piece, and a rook covers the other."""
    if m.piece != chess.BISHOP or len(_inward(m.king)) != 2:
        return False
    a, b = (_off(m.king, *v) for v in _inward(m.king))
    return any(x in m.own and y in m.free and any(m.kind(r) == chess.ROOK for r in m.cover[y])
               for x, y in ((a, b), (b, a)))


def greco(m: Mate) -> bool:
    """Against a cornered king, a rook or queen mates along an edge; bishops cover every flight square
    the checker doesn't, and a piece of the king's own closes another."""
    if m.piece not in (chess.ROOK, chess.QUEEN) or len(_inward(m.king)) != 2 or not m.along_edge() or not m.own:
        return False
    rest = m.rest()
    return bool(rest) and all(any(m.kind(a) == chess.BISHOP for a in m.cover[s]) for s in rest)


def hook(m: Mate) -> bool:
    """A rook mates from an adjacent square, guarded by a knight that is guarded by a pawn; the king's
    own pieces close its remaining squares, and nothing outside that chain is needed."""
    if m.piece != chess.ROOK or m.dist != 1 or not m.own:
        return False
    for knight in m.defenders(m.checker, chess.KNIGHT):
        for pawn in m.defenders(knight, chess.PAWN):
            chain = {m.checker, knight, pawn}
            if all(m.cover[s] & chess.SquareSet(chain) for s in m.free if s != m.checker):
                return True
    return False


def corridor(m: Mate) -> bool:
    """ChessTrove's generalised back-rank mate: a rook or queen mates along any edge other than the
    king's own back rank, and the king's own pieces fill every square of the next line in."""
    v = m.along_edge()
    if m.piece not in (chess.ROOK, chess.QUEEN) or not v or v == ((0, 1) if m.mated == chess.WHITE else (0, -1)):
        return False
    px, py = _perp(v)
    inner = [s for i in (-1, 0, 1) if (s := _off(m.king, v[0] + i * px, v[1] + i * py)) is not None]
    return all(s in m.own for s in inner)


def blackburne(m: Mate) -> bool:
    """Two bishops and a knight do all the work: the checker is one of them, every flight square is
    covered by a bishop or knight, and both bishops and a knight each give check, guard the checker
    or cover a flight square."""
    if m.piece not in (chess.BISHOP, chess.KNIGHT) or not m.own:
        return False
    minor = (chess.BISHOP, chess.KNIGHT)
    if not all(any(m.kind(a) in minor for a in m.cover[s]) for s in m.free):
        return False
    working = {m.checker} | {a for s in m.free for a in m.cover[s] if m.kind(a) in minor}
    working |= set(m.defenders(m.checker, *minor))
    kinds = [m.kind(a) for a in working]
    return kinds.count(chess.BISHOP) == 2 and kinds.count(chess.KNIGHT) >= 1


def reti(m: Mate) -> bool:
    """A bishop mates, guarded by a rook or queen along a file or rank; between them they cover every
    flight square the king's own pieces don't block."""
    if m.piece != chess.BISHOP or not m.own:
        return False
    heavy = [a for a in m.defenders(m.checker, chess.ROOK, chess.QUEEN)
             if chess.square_file(a) == chess.square_file(m.checker) or chess.square_rank(a) == chess.square_rank(m.checker)]
    return any(all(s == m.checker or m.checker in m.cover[s] or h in m.cover[s] for s in m.free) for h in heavy)


def pillsbury(m: Mate) -> bool:
    """A rook mates coming straight in toward an edge king (never along an edge); bishops, or queens on
    a diagonal, cover every flight square the rook doesn't, and the king's own pieces close the rest.
    Unlike the Opera mate, nothing needs to guard the rook."""
    if m.piece != chess.ROOK or m.d not in _inward(m.king) or m.along_edge() or not m.own:
        return False
    rest = m.rest()
    return bool(rest) and all(set(_diagonal_guards(m, s)) & set(m.cover[s]) for s in rest)


def ladder(m: Mate) -> bool:
    """Two heavy pieces: one mates along the king's edge, the other stands on the next line in and
    covers every square of it."""
    v = m.along_edge()
    if m.piece not in (chess.ROOK, chess.QUEEN) or not v:
        return False
    px, py = _perp(v)
    inner = [s for i in (-1, 0, 1) if (s := _off(m.king, v[0] + i * px, v[1] + i * py)) is not None]
    line = chess.SquareSet(chess.BB_RANKS[chess.square_rank(inner[0])] if v[1] else chess.BB_FILES[chess.square_file(inner[0])])
    return any(all(s in m.free and h in m.cover[s] for s in inner)
               for h in line if h != m.checker and m.kind(h) in (chess.ROOK, chess.QUEEN))


def box(m: Mate) -> bool:
    """The basic king-and-rook mate: the rook mates along the king's edge, the attacking king covers
    every square the rook doesn't, and no other attacking piece plays a part."""
    if m.piece != chess.ROOK or not m.along_edge():
        return False
    rest = m.rest()
    if not rest or not all(any(m.kind(a) == chess.KING for a in m.cover[s]) for s in rest):
        return False
    helpers = {a for s in m.cover for a in m.cover[s]} | {s for s in m.cover if m.kind(s)}
    return all(m.kind(a) == chess.KING or a == m.checker for a in helpers)


class NamedMate:
    version = 1

    def __init__(self, id: str, test: Callable[[Mate], bool]):
        self.id, self.test = id, test
        self.__doc__ = test.__doc__  # the definition

    def detect(self, ctx: MoveContext) -> list[Event]:
        m = anatomy(ctx)
        if not m or not self.test(m):
            return []
        return [event(ctx, self.id, king_square=chess.square_name(m.king), checker=chess.square_name(m.checker),
                      checker_piece=chess.piece_name(m.piece))]


NAMED_MATES = tuple(NamedMate(f"{name.upper()}_MATE", test) for name, test in (
    ("epaulette", epaulette), ("swallows_tail", swallows_tail), ("dovetail", dovetail),
    ("anastasia", anastasia), ("arabian", arabian), ("boden", boden), ("opera", opera),
    ("anderssen", anderssen), ("lolli", lolli), ("damiano", damiano), ("morphy", morphy),
    ("greco", greco), ("hook", hook), ("corridor", corridor), ("blackburne", blackburne),
    ("reti", reti), ("pillsbury", pillsbury), ("ladder", ladder), ("box", box),
))
