"""Named mating patterns (Epaulette, Anastasia's, Boden's, ...): family, then form.

These names have no single rigid definition across chess sources, so each pattern answers three
separate questions about the final position:

  family     does the pattern's defining mechanism make the mate? This is what emits the event, and
             false positives here are worse than misses.
  canonical  do the pattern's characteristic pieces do their characteristic jobs, unaided?
  textbook   does it also look like the diagram people learn under that name?

Each event carries `form` ("textbook", "canonical" or "variant"), the concrete `traits` the form was
derived from, and `short_of`: the traits that kept it out of the next tier up. There is no score. Not
every pattern has all three tiers: where a looser family would lose the name's meaning, the family test
is already canonical and there is no variant; where no stereotype stands out, there is no textbook tier.

Every piece a pattern names has to do its job in the final position (give the check, guard the checker,
cover a flight square), not merely be on the board. Patterns hold mirrored left to right and for either
colour, and one mate may carry several names. Only single-check mates qualify.

Vocabulary (all computed on the mating position):
  king      the mated king; its neighbours are its flight squares
  own       neighbours held by the mated side's own pieces (these never count against a form: the
            distinction is about extra attacking help, not the defender's own blockers)
  free      every other neighbour; since it's mate, each is covered by the attacker
  cover[s]  the attacker's pieces controlling neighbour s, with the king lifted off the board (so a
            slider checking the king also covers the square behind it); for the checker's own square
            that is its defenders
  inward    unit step from an edge square toward the centre (a corner has two)
  defining  the pieces a pattern names (the checker, and the pieces doing its characteristic jobs)
  helpers   other attacking pieces the mate needs: they alone cover some free square
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import chess

from chesstrove.detectors.base import Event, event
from chesstrove.models import MoveContext

Vec = tuple[int, int]
B, N, R, Q, P, K = chess.BISHOP, chess.KNIGHT, chess.ROOK, chess.QUEEN, chess.PAWN, chess.KING
CORNERS = (chess.A1, chess.H1, chess.A8, chess.H8)


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


def _diagonal(a: chess.Square, b: chess.Square) -> bool:
    return chess.square_file(a) != chess.square_file(b) and chess.square_rank(a) != chess.square_rank(b)


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

    def helpers(self, defining: Iterable[chess.Square]) -> set[chess.Square]:
        """Attacking pieces outside `defining` that alone cover some free square (the checker's own
        square included: its defenders). ponytail: pins aren't counted as help; a helper that only pins
        a would-be capturer of the checker is missed."""
        defining = set(defining)
        return {a for s in self.free if not set(self.cover[s]) & defining for a in self.cover[s]}

    # locations, for the textbook tier
    def on_edge(self) -> bool:
        return bool(_inward(self.king))

    def in_corner(self) -> bool:
        return self.king in CORNERS

    def next_to_corner(self) -> bool:
        """On an edge, one step from a corner (g8, h7, b1, ...)."""
        return any(chess.square_distance(self.king, c) == 1 for c in CORNERS) and self.on_edge()

    def near_corner(self) -> bool:
        """On an edge, within two steps of a corner (the castled king's f-h and a-c files)."""
        return self.on_edge() and min(chess.square_distance(self.king, c) for c in CORNERS) <= 2

    def home_rank(self) -> bool:
        return chess.square_rank(self.king) == (0 if self.mated == chess.WHITE else 7)

    def label(self, square: chess.Square) -> str:
        """Qh7, Nf6, g6 (a pawn)."""
        p = self.board.piece_at(square)
        return ("" if p.piece_type == P else p.symbol().upper()) + chess.square_name(square)


def anatomy_of(board: chess.Board) -> Mate | None:
    if not board.is_checkmate():
        return None
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


def anatomy(ctx: MoveContext) -> Mate | None:
    return anatomy_of(ctx.board_after) if ctx.facts.is_checkmate else None


Match = dict[str, Any]


def grade(m: Mate, defining: Iterable[chess.Square], traits: dict[str, Any],
          canonical: Iterable[str] = (), textbook: Iterable[str] | None = ()) -> Match:
    """Family membership is settled by the caller. `canonical` and `textbook` name the boolean traits each
    tier needs on top of the one below; `textbook=None` means the pattern has no textbook tier. Every
    match records `no_extra_helpers`, and a tier may ask for it."""
    defining = set(defining) | {m.checker}
    helpers = m.helpers(defining)
    traits = {**traits, "no_extra_helpers": not helpers}
    short = [t for t in canonical if not traits[t]]
    if not short and textbook is not None:
        short = [t for t in textbook if not traits[t]]
        form = "canonical" if short else "textbook"
    else:
        form = "variant" if short else "canonical"
    return {"form": form, "traits": traits, "short_of": short,
            "defining": sorted(map(m.label, defining)), "helpers": sorted(map(m.label, helpers)),
            "own_blockers": sorted(map(chess.square_name, m.own))}


# --- the patterns: each takes the mate's anatomy and returns a Match, or None outside the family ---------

def epaulette(m: Mate) -> Match | None:
    """Family: a queen checks head-on (orthogonally, from two or more squares away) and both squares
    beside the king, across the line of check, hold its own pieces (any pieces: the broad usage).
    Variants are deliberately loose for now (a mid-board king with pawn shoulders, sealed from behind by
    another attacker, still counts) so they can be reviewed before being narrowed. Canonical: those shoulder pieces are both rooks and the queen needs no help. Textbook: also the king
    has its back to the edge and the queen stands two squares in front."""
    if m.piece != Q or not m.orthogonal() or m.dist < 2:
        return None
    px, py = _perp(m.d)
    sides = [_off(m.king, px, py), _off(m.king, -px, -py)]
    if not all(s in m.own for s in sides):
        return None
    kinds = [m.board.piece_type_at(s) for s in sides]
    return grade(m, [], {
        "shoulders": sorted(map(chess.piece_name, kinds)),
        "both_shoulders_are_rooks": kinds == [R, R],
        "king_on_edge": m.d in _inward(m.king),
        "queen_two_squares_away": m.dist == 2,
    }, canonical=["both_shoulders_are_rooks", "no_extra_helpers"], textbook=["king_on_edge", "queen_two_squares_away"])


def swallows_tail(m: Mate) -> Match | None:
    """Family: a queen mates from an orthogonally adjacent square; the two diagonal squares behind the
    king, which the queen can't reach, hold its own pieces (the queen covers everything else, so no
    helper is ever needed beyond her guard). The family is the classical picture, so every match is
    textbook; `rear` records which pieces make the tail."""
    if m.piece != Q or not m.orthogonal() or m.dist != 1:
        return None
    (dx, dy), (px, py) = m.d, _perp(m.d)
    rear = [_off(m.king, -dx + px, -dy + py), _off(m.king, -dx - px, -dy - py)]
    if not all(s in m.own for s in rear):
        return None
    return grade(m, m.defenders(m.checker, P, N, B, R, Q, K),
                 {"rear": sorted(chess.piece_name(m.board.piece_type_at(s)) for s in rear)})


def dovetail(m: Mate) -> Match | None:
    """Family: a guarded queen mates from a diagonally adjacent square; the two squares beside the king
    on the far side from the queen (the ones it can't reach) hold the king's own pieces. The queen then
    covers everything else herself, and the geometry needs all eight squares, so every match is the
    classical picture: textbook."""
    if m.piece != Q or m.orthogonal() or m.dist != 1:
        return None
    dx, dy = m.d
    if _off(m.king, -dx, 0) not in m.own or _off(m.king, 0, -dy) not in m.own:
        return None
    return grade(m, m.defenders(m.checker, P, N, B, R, Q, K), {})


def anastasia(m: Mate) -> Match | None:
    """Family: a rook or queen mates along the edge the king stands on; the square straight in from the
    king holds its own piece; knights cover every flight square the checker doesn't, and at least one of
    those squares needs the knight (nothing else covers it). Canonical: one knight takes both
    characteristic flights, the two squares diagonally inward from the king (g8 and g6 against Kh7).
    Textbook: also the Kh7 picture (the king on a side file, one step from the corner), with no other
    attacker needed. A knight covering only one of the two, the other shut by the king's own piece or
    the checker, is a variant: often a back-rank mate that a knight happens to finish."""
    v = m.along_edge()
    if m.piece not in (R, Q) or not v or _off(m.king, *v) not in m.own:
        return None
    rest = m.rest()
    knights = {a for s in rest for a in m.cover[s] if m.kind(a) == N}
    if not rest or not all(set(m.cover[s]) & knights for s in rest):
        return None
    if not any(all(m.kind(a) == N for a in m.cover[s]) for s in rest):
        return None  # every square the knight covers was covered anyway: the knight is incidental
    px, py = _perp(v)
    flights = [s for s in (_off(m.king, v[0] + px, v[1] + py), _off(m.king, v[0] - px, v[1] - py)) if s is not None]
    return grade(m, knights, {
        "line_checker": chess.piece_name(m.piece),
        "inward_own_blocker": True,
        "knight_controls_both_characteristic_flights": len(flights) == 2 and any(
            all(s in m.free and k in m.cover[s] for s in flights) for k in knights),
        "king_on_side_file": v[0] != 0,
        "king_next_to_corner": m.next_to_corner(),
    }, canonical=["knight_controls_both_characteristic_flights"],
        textbook=["king_on_side_file", "king_next_to_corner", "no_extra_helpers"])


def arabian(m: Mate) -> Match | None:
    """Family: a rook mates from an adjacent square, guarded by a knight that also takes away a flight
    square the rook doesn't cover. Canonical: rook and knight cover everything between them (the king's
    own pieces may block the rest; the king needn't be cornered). Textbook: also the king is in the
    corner. Variant: other attackers have to close squares too. Deliberately loose for now (it keeps
    mid-board kings and second heavy pieces) so the variants can be reviewed before being narrowed."""
    if m.piece != R or m.dist != 1:
        return None
    rest = m.rest()
    knights = [n for n in m.defenders(m.checker, N) if any(n in m.cover[s] for s in rest)]
    if not knights:
        return None
    return grade(m, knights, {"king_in_corner": m.in_corner()},
                 canonical=["no_extra_helpers"], textbook=["king_in_corner"])


def boden(m: Mate) -> Match | None:
    """Family (and canonical): a bishop mates; a second bishop, on the other colour, covers flight
    squares the first can't; the two bishops between them cover every flight square, and the king's
    own pieces hem it in. Textbook: the king is on its own back rank (the castled-long picture)."""
    if m.piece != B or not m.own:
        return None
    if not all(any(m.kind(a) == B for a in m.cover[s]) for s in m.free):
        return None
    light = lambda s: bool(chess.BB_SQUARES[s] & chess.BB_LIGHT_SQUARES)  # noqa: E731
    other = {a for s in m.rest() for a in m.cover[s] if m.kind(a) == B and light(a) != light(m.checker)}
    if not other:
        return None
    return grade(m, other, {"king_on_home_rank": m.home_rank()}, textbook=["king_on_home_rank"])


def opera(m: Mate) -> Match | None:
    """Family: a rook mates from an adjacent square on the king's edge, guarded by a bishop that also
    takes away another flight square (Morphy's Opera game: Rd8#, Bg5 guards d8 and covers e7). A queen in
    the bishop's place doesn't count: that's an ordinary queen-and-rook mate. Canonical: rook and bishop
    need no other attacker. Textbook: also the king is on its own back rank."""
    if m.piece != R or m.dist != 1 or not m.along_edge():
        return None
    bishops = [g for g in m.defenders(m.checker, B) if any(g in m.cover[s] for s in m.free if s != m.checker)]
    if not bishops:
        return None
    return grade(m, bishops, {"king_on_home_rank": m.home_rank()},
                 canonical=["no_extra_helpers"], textbook=["king_on_home_rank"])


def anderssen(m: Mate) -> Match | None:
    """Family (and canonical): a rook or queen mates from the corner next to the king, guarded
    diagonally by a pawn that also covers another of the king's flight squares; the pawn usually needs
    support of its own (typically the king), which is part of the picture, not extra help. Textbook: a
    rook gives the mate."""
    if m.piece not in (R, Q) or m.dist != 1 or len(_inward(m.checker)) != 2:
        return None
    pawns = [p for p in m.defenders(m.checker, P) if any(p in m.cover[s] for s in m.free if s != m.checker)]
    if not pawns:
        return None
    return grade(m, pawns, {"checker_is_rook": m.piece == R}, textbook=["checker_is_rook"])


def lolli(m: Mate) -> Match | None:
    """Family (and canonical): a queen mates from the square directly in front of an edge king (Qg7#
    against Kg8), guarded by a pawn. From there the queen covers every flight square herself, so no
    helper is ever involved. Textbook: the castled-king picture, the king on its own back rank near a
    corner. Whether the king actually castled isn't checked."""
    if m.piece != Q or m.dist != 1 or m.d not in _inward(m.king):
        return None
    pawns = m.defenders(m.checker, P)
    if not pawns:
        return None
    return grade(m, pawns, {
        "king_on_home_rank": m.home_rank(),
        "king_near_corner": m.near_corner(),
        "pawn_controls_flight": any(p in m.cover[s] for p in pawns for s in m.free if s != m.checker),
    }, textbook=["king_on_home_rank", "king_near_corner"])


def damiano(m: Mate) -> Match | None:
    """Family: a queen mates from the edge square diagonally in front of a king one step from a corner
    (Qh7# against Kg8, Qg8# against Kh7), guarded by a pawn or, as a common substitute, a bishop.
    Scholar's-mate shapes (Qxf7# against Ke8) and Qb2# against Kc1 don't count: the queen must stand on
    the corner's other edge. Canonical: queen and support need no other attacker (the king's own pieces
    may close the rest). Textbook: also the support is a pawn and the king is on its own back rank."""
    if m.piece != Q or m.dist != 1 or m.orthogonal() or not m.next_to_corner() or m.in_corner():
        return None
    if not _inward(m.checker) or not any(m.d[0] * v[0] + m.d[1] * v[1] == 1 for v in _inward(m.king)):
        return None
    support = m.defenders(m.checker, P) or m.defenders(m.checker, B)
    if not support:
        return None
    return grade(m, support, {
        "support_piece": chess.piece_name(m.kind(support[0])),
        "support_is_pawn": m.kind(support[0]) == P,
        "king_on_home_rank": m.home_rank(),
    }, canonical=["no_extra_helpers"], textbook=["support_is_pawn", "king_on_home_rank"])


def morphy(m: Mate) -> Match | None:
    """Family (and textbook): against a cornered king, a bishop mates along the long diagonal; one of the
    two edge squares beside the king holds its own piece, and a rook covers the other."""
    if m.piece != B or not m.in_corner():
        return None
    a, b = (_off(m.king, *v) for v in _inward(m.king))
    for x, y in ((a, b), (b, a)):
        rooks = [r for r in m.cover[y] if m.kind(r) == R]
        if x in m.own and y in m.free and rooks:
            return grade(m, rooks, {})
    return None


def greco(m: Mate) -> Match | None:
    """Family (and canonical): against a cornered king, a rook or queen mates along an edge; bishops
    cover every flight square the checker doesn't, and a piece of the king's own closes another.
    Textbook: the mate comes down the side file (Qh5# or Rh-file against Kh8), not along the back rank."""
    v = m.along_edge()
    if m.piece not in (R, Q) or not m.in_corner() or not v or not m.own:
        return None
    rest = m.rest()
    if not rest or not all(any(m.kind(a) == B for a in m.cover[s]) for s in rest):
        return None
    bishops = {a for s in rest for a in m.cover[s] if m.kind(a) == B}
    return grade(m, bishops, {"checked_down_side_file": v[0] != 0}, textbook=["checked_down_side_file"])


def hook(m: Mate) -> Match | None:
    """Family (and textbook): a rook mates from an adjacent square, guarded by a knight that is guarded
    by a pawn; the king's own pieces close its remaining squares, and nothing outside that chain is
    needed."""
    if m.piece != R or m.dist != 1 or not m.own:
        return None
    for knight in m.defenders(m.checker, N):
        for pawn in m.defenders(knight, P):
            chain = {m.checker, knight, pawn}
            if all(m.cover[s] & chess.SquareSet(chain) for s in m.free if s != m.checker):
                return grade(m, chain, {})
    return None


def corridor(m: Mate) -> Match | None:
    """ChessTrove's own generalised back-rank mate, so it has no textbook tier: a rook or queen mates
    along any edge other than the king's own back rank, and the king's own pieces fill every square of
    the next line in."""
    v = m.along_edge()
    if m.piece not in (R, Q) or not v or v == ((0, 1) if m.mated == chess.WHITE else (0, -1)):
        return None
    px, py = _perp(v)
    inner = [s for i in (-1, 0, 1) if (s := _off(m.king, v[0] + i * px, v[1] + i * py)) is not None]
    return grade(m, [], {}, textbook=None) if all(s in m.own for s in inner) else None


def blackburne(m: Mate) -> Match | None:
    """Family (and canonical): two bishops and a knight do all the work. The checker is one of them, every
    flight square is covered by a bishop or knight, and both bishops and a knight each give check, guard
    the checker or cover a flight square. Sources draw it in several arrangements, so there is no
    textbook tier."""
    if m.piece not in (B, N) or not m.own:
        return None
    minor = (B, N)
    if not all(any(m.kind(a) in minor for a in m.cover[s]) for s in m.free):
        return None
    working = {m.checker} | {a for s in m.free for a in m.cover[s] if m.kind(a) in minor}
    working |= set(m.defenders(m.checker, *minor))
    kinds = [m.kind(a) for a in working]
    if kinds.count(B) != 2 or kinds.count(N) < 1:
        return None
    return grade(m, working, {}, textbook=None)


def reti(m: Mate) -> Match | None:
    """Family (and canonical): a bishop mates from beside the king, guarded by a rook or queen along a
    file or rank; the bishop also takes away a flight square of its own, the two pieces between them
    cover every free square, and at least three of the king's own pieces wall it in. Deliberately narrow:
    there is no variant tier. Textbook: four or more own pieces around the king (Réti-Tartakower, 1910)
    and a rook as the guard."""
    if m.piece != B or m.dist != 1 or len(m.own) < 3:
        return None
    guards = [a for a in m.defenders(m.checker, R, Q) if not _diagonal(a, m.checker)]
    walls = sum(m.checker in m.cover[s] for s in m.free if s != m.checker)
    for g in guards:
        if walls and all(s == m.checker or m.checker in m.cover[s] or g in m.cover[s] for s in m.free):
            return grade(m, [g], {
                "own_blocker_count": len(m.own),
                "four_own_blockers": len(m.own) >= 4,
                "bishop_flights": walls,
                "supporter": chess.piece_name(m.kind(g)),
                "supporter_is_rook": m.kind(g) == R,
            }, textbook=["four_own_blockers", "supporter_is_rook"])
    return None


def pillsbury(m: Mate) -> Match | None:
    """Family: a rook mates a king on an edge within two steps of a corner, coming straight in toward the
    edge (never along it), and a bishop takes away the edge square beside the king on the corner side
    (the corner itself when the king stands next to it: Rg-file against Kg8, Bb2 covering h8). A bishop covering
    some other square doesn't make it Pillsbury's. Canonical: rook, bishop and anything guarding the rook
    need no other attacker. Textbook: also the king is on its own back rank next to the corner and the
    rook checks from a distance (a rook lifted next to the king, or a king up the side file, is
    canonical)."""
    if m.piece != R or m.d not in _inward(m.king) or m.along_edge() or not m.near_corner():
        return None
    px, py = _perp(m.d)
    beside = [s for s in (_off(m.king, px, py), _off(m.king, -px, -py)) if s is not None]
    toward = min(beside, key=lambda s: min(chess.square_distance(s, c) for c in CORNERS))
    bishops = [a for a in m.cover[toward] if m.kind(a) == B] if toward in m.free else []
    if not bishops:
        return None
    guards = m.defenders(m.checker, P, N, B, R, Q, K) if m.dist == 1 else []
    return grade(m, [*bishops, *guards], {
        "bishop_covers_corner": toward in CORNERS,
        "king_on_home_rank": m.home_rank(),
        "rook_from_distance": m.dist >= 2,
    }, canonical=["no_extra_helpers"], textbook=["bishop_covers_corner", "king_on_home_rank", "rook_from_distance"])


def ladder(m: Mate) -> Match | None:
    """Family (and canonical): two heavy pieces, one mating along the king's edge, the other on the next
    line in covering every square of it. Textbook: both are rooks."""
    v = m.along_edge()
    if m.piece not in (R, Q) or not v:
        return None
    px, py = _perp(v)
    inner = [s for i in (-1, 0, 1) if (s := _off(m.king, v[0] + i * px, v[1] + i * py)) is not None]
    line = chess.SquareSet(chess.BB_RANKS[chess.square_rank(inner[0])] if v[1] else chess.BB_FILES[chess.square_file(inner[0])])
    for h in line:
        if h != m.checker and m.kind(h) in (R, Q) and all(s in m.free and h in m.cover[s] for s in inner):
            return grade(m, [h], {"both_rooks": m.piece == R and m.kind(h) == R}, textbook=["both_rooks"])
    return None


def box(m: Mate) -> Match | None:
    """Family (and textbook): the basic king-and-rook mate. The rook mates along the king's edge, the
    attacking king covers every square the rook doesn't, and no other attacking piece plays a part."""
    if m.piece != R or not m.along_edge():
        return None
    rest = m.rest()
    if not rest or not all(any(m.kind(a) == K for a in m.cover[s]) for s in rest):
        return None
    helpers = {a for s in m.cover for a in m.cover[s]} | {s for s in m.cover if m.kind(s)}
    if not all(m.kind(a) == K or a == m.checker for a in helpers):
        return None
    return grade(m, [a for a in helpers if m.kind(a) == K], {})


class NamedMate:
    version = 2  # 2: family/form split; events carry form, traits and the pieces involved

    def __init__(self, id: str, test: Callable[[Mate], Match | None]):
        self.id, self.test = id, test
        self.__doc__ = test.__doc__  # the definition

    def detect(self, ctx: MoveContext) -> list[Event]:
        m = anatomy(ctx)
        found = m and self.test(m)
        if not found:
            return []
        return [event(ctx, self.id, king_square=chess.square_name(m.king), checker=chess.square_name(m.checker),
                      checker_piece=chess.piece_name(m.piece), **found)]


NAMED_MATES = tuple(NamedMate(f"{name.upper()}_MATE", test) for name, test in (
    ("epaulette", epaulette), ("swallows_tail", swallows_tail), ("dovetail", dovetail),
    ("anastasia", anastasia), ("arabian", arabian), ("boden", boden), ("opera", opera),
    ("anderssen", anderssen), ("lolli", lolli), ("damiano", damiano), ("morphy", morphy),
    ("greco", greco), ("hook", hook), ("corridor", corridor), ("blackburne", blackburne),
    ("reti", reti), ("pillsbury", pillsbury), ("ladder", ladder), ("box", box),
))
