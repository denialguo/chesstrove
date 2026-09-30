var E="",S=`"""Detector registry. Adding a detector = write the class, add one line here, add tests."""

from collections.abc import Sequence

from chesstrove.detectors.base import Detector, Event
from chesstrove.detectors.checks import DoubleCheck
from chesstrove.detectors.material import ThreePlusQueens
from chesstrove.detectors.mates import EnPassantCheckmate, KingDeliveredMate, MissedMateInOne
from chesstrove.detectors.notation import DoubleDisambiguatedSan
from chesstrove.detectors.named_mates import NAMED_MATES
from chesstrove.detectors.patterns import BackRankMate, SmotheredMate
from chesstrove.detectors.promotion import PromotionCheckmate, Underpromotion

DETECTORS: tuple[Detector, ...] = (
    Underpromotion(),
    PromotionCheckmate(),
    EnPassantCheckmate(),
    KingDeliveredMate(),
    DoubleCheck(),
    ThreePlusQueens(),
    DoubleDisambiguatedSan(),
    MissedMateInOne(),
    SmotheredMate(),
    BackRankMate(),
    *NAMED_MATES,
)


def select(ids: Sequence[str] | None) -> tuple[Detector, ...]:
    """The named detectors, or all of them. Unknown ids are an error, not a silent no-op."""
    if not ids:
        return DETECTORS  # looked up at call time, so tests can patch the registry
    by_id = {d.id: d for d in DETECTORS}
    if unknown := set(ids) - by_id.keys():
        raise ValueError(f"unknown detector(s): {', '.join(sorted(unknown))}; known: {', '.join(by_id)}")
    return tuple(by_id[i] for i in dict.fromkeys(ids))


__all__ = ["DETECTORS", "Detector", "Event", "select"]
`,C=`"""The detector contract. A detector sees one MoveContext at a time and returns zero or more Events.

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
    version: int  # bump when the definition changes; \`chesstrove reanalyze\` then redoes stale games
    # the first condition detect() checks, as a key of indexing.GATES: the indexer skips the detector on plies where
    # it can't hold ("any" = every ply). Must be implied by detect()'s own logic, never a new rule.
    requires: str
    tier: str  # "fast": the first pass; "deep": a slower, optional second pass (indexing.FAST / DEEP)

    def detect(self, ctx: MoveContext) -> list[Event]: ...


def event(ctx: MoveContext, type: str, **metadata: Any) -> Event:
    fen = ctx.facts.fen_before if ctx.facts.fen_before is not None else ctx.board_before.fen()
    return Event(type, ctx.ply, ctx.facts.color, fen, metadata)


def piece_name(letter: str) -> str:
    return chess.piece_name(chess.PIECE_SYMBOLS.index(letter.lower()))


def checker_squares(board: chess.Board) -> list[str]:
    return [chess.square_name(s) for s in board.checkers()]
`,M=`from chesstrove.detectors.base import Event, checker_squares, event
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
`,A=`import chess

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
`,R=`import chess

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
    expensive step (push, is_checkmate, pop). \`legal\`: the moves replay already generated, if any.
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
                if (piece == chess.QUEEN or (piece == chess.BISHOP) == diagonal) \\
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
`,P=`"""Named mating patterns (Epaulette, Anastasia's, Boden's, ...): family, then form.

These names have no single rigid definition across chess sources, so each pattern answers three
separate questions about the final position:

  family          does the pattern's defining mechanism make the mate? This is what emits the event, and
                  false positives here are worse than misses. A family member that fails the next test
                  is a variant: a classical relationship is weakened, substituted or helped out.
  characteristic  do the pattern's defining pieces do their defining jobs, unaided? Geometry, king
                  location or orientation may still differ from the familiar diagram.
  textbook        does it also look like the diagram people learn under that name?

Each event carries \`form\` ("textbook", "characteristic" or "variant"), the concrete \`traits\` the form was
derived from, and \`short_of\`: the traits that kept it out of the next tier up. There is no score. Not
every pattern has all three tiers: where a looser family would lose the name's meaning, the family test
is already characteristic and there is no variant; where no stereotype stands out, there is no textbook tier.
(Before September 2026 the middle tier was stored as "canonical"; schema.sql renames those rows.)

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
        """The attacker's piece type on \`square\`, if the attacker has a piece there."""
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
        """Attacking pieces outside \`defining\` that alone cover some free square (the checker's own
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
          characteristic: Iterable[str] = (), textbook: Iterable[str] | None = ()) -> Match:
    """Family membership is settled by the caller. \`characteristic\` and \`textbook\` name the boolean traits each
    tier needs on top of the one below; \`textbook=None\` means the pattern has no textbook tier. Every
    match records \`no_extra_helpers\`, and a tier may ask for it."""
    defining = set(defining) | {m.checker}
    helpers = m.helpers(defining)
    traits = {**traits, "no_extra_helpers": not helpers}
    short = [t for t in characteristic if not traits[t]]
    if not short and textbook is not None:
        short = [t for t in textbook if not traits[t]]
        form = "characteristic" if short else "textbook"
    else:
        form = "variant" if short else "characteristic"
    return {"form": form, "traits": traits, "short_of": short,
            "defining": sorted(map(m.label, defining)), "helpers": sorted(map(m.label, helpers)),
            "own_blockers": sorted(map(chess.square_name, m.own))}


# --- the patterns: each takes the mate's anatomy and returns a Match, or None outside the family ---------

def epaulette(m: Mate) -> Match | None:
    """Family: a queen checks head-on (orthogonally, from two or more squares away) and both squares
    beside the king, across the line of check, hold its own pieces (any pieces: the broad usage).
    Variants are deliberately loose for now (a mid-board king with pawn shoulders, sealed from behind by
    another attacker, still counts) so they can be reviewed before being narrowed. Characteristic: those shoulder pieces are both rooks and the queen needs no help. Textbook: also the king
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
    }, characteristic=["both_shoulders_are_rooks", "no_extra_helpers"], textbook=["king_on_edge", "queen_two_squares_away"])


def swallows_tail(m: Mate) -> Match | None:
    """Family: a queen mates from an orthogonally adjacent square; the two diagonal squares behind the
    king, which the queen can't reach, hold its own pieces (the queen covers everything else, so no
    helper is ever needed beyond her guard); \`rear\` records which pieces make the tail. Textbook: those
    two are the only pieces of its own around the king. More own blockers make it characteristic."""
    if m.piece != Q or not m.orthogonal() or m.dist != 1:
        return None
    (dx, dy), (px, py) = m.d, _perp(m.d)
    rear = [_off(m.king, -dx + px, -dy + py), _off(m.king, -dx - px, -dy - py)]
    if not all(s in m.own for s in rear):
        return None
    return grade(m, m.defenders(m.checker, P, N, B, R, Q, K), {
        "rear": sorted(chess.piece_name(m.board.piece_type_at(s)) for s in rear),
        "only_tail_pieces_block": m.own == set(rear),
    }, textbook=["only_tail_pieces_block"])


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
    those squares needs the knight (nothing else covers it). Characteristic: one knight takes both
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
    }, characteristic=["knight_controls_both_characteristic_flights"],
        textbook=["king_on_side_file", "king_next_to_corner", "no_extra_helpers"])


def arabian(m: Mate) -> Match | None:
    """Family: a rook mates from an adjacent square, guarded by a knight that also takes away a flight
    square the rook doesn't cover, and the knight isn't itself guarded by a pawn (that's a Hook mate).
    Characteristic: rook and knight cover everything between them (the king's own pieces may block the
    rest; the king needn't be cornered). Textbook: also the king is in the
    corner. Variant: other attackers have to close squares too. Deliberately loose for now (it keeps
    mid-board kings and second heavy pieces) so the variants can be reviewed before being narrowed."""
    if m.piece != R or m.dist != 1:
        return None
    rest = m.rest()
    knights = [n for n in m.defenders(m.checker, N) if any(n in m.cover[s] for s in rest)]
    if not knights or any(m.defenders(n, P) for n in m.defenders(m.checker, N)):
        return None  # none, or the guard is pawn-backed: that chain is a Hook mate
    return grade(m, knights, {"king_in_corner": m.in_corner()},
                 characteristic=["no_extra_helpers"], textbook=["king_in_corner"])


def boden(m: Mate) -> Match | None:
    """Family (and characteristic): a bishop mates; a second bishop, on the other colour, covers flight
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
    the bishop's place doesn't count: that's an ordinary queen-and-rook mate. Characteristic: rook and bishop
    need no other attacker. Textbook: also the king is on its own back rank."""
    if m.piece != R or m.dist != 1 or not m.along_edge():
        return None
    bishops = [g for g in m.defenders(m.checker, B) if any(g in m.cover[s] for s in m.free if s != m.checker)]
    if not bishops:
        return None
    return grade(m, bishops, {"king_on_home_rank": m.home_rank()},
                 characteristic=["no_extra_helpers"], textbook=["king_on_home_rank"])


def anderssen(m: Mate) -> Match | None:
    """Family (and characteristic): a rook or queen mates from the corner next to the king, guarded
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
    """Family (and characteristic): a queen mates from the square directly in front of an edge king (Qg7#
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
    the corner's other edge. Characteristic: queen and support need no other attacker (the king's own pieces
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
    }, characteristic=["no_extra_helpers"], textbook=["support_is_pawn", "king_on_home_rank"])


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
    """Family (and characteristic): against a cornered king, a rook or queen mates along an edge; bishops
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
    """Family: a rook mates from an adjacent square, guarded by a knight that is itself guarded by a pawn
    (the rook-knight-pawn chain). A rook-and-knight mate with that chain is a Hook, not an Arabian.
    Characteristic: the chain needs no other attacker. Textbook: also the king's own pieces close some of its
    squares. Variant: other attackers help close the net."""
    if m.piece != R or m.dist != 1:
        return None
    chains = [{m.checker, k, p} for k in m.defenders(m.checker, N) for p in m.defenders(k, P)]
    if not chains:
        return None
    chain = min(chains, key=lambda c: len(m.helpers(c)))
    return grade(m, chain, {"own_blockers_close_squares": bool(m.own)},
                 characteristic=["no_extra_helpers"], textbook=["own_blockers_close_squares"])


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
    """Family (and characteristic): two bishops and a knight do all the work. The checker is one of them, every
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
    """Family (and characteristic): a bishop mates from beside the king, guarded by a rook or queen along a
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
    some other square doesn't make it Pillsbury's. Characteristic: rook, bishop and anything guarding the rook
    need no other attacker. Textbook: also the king is on its own back rank next to the corner and the
    rook checks from a distance (a rook lifted next to the king, or a king up the side file, is
    characteristic)."""
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
    }, characteristic=["no_extra_helpers"], textbook=["bishop_covers_corner", "king_on_home_rank", "rook_from_distance"])


def ladder(m: Mate) -> Match | None:
    """Family (and textbook): two heavy pieces, one mating along the king's edge, the other on the next
    line in covering every square of it. Rooks or queens alike; \`both_rooks\` records which."""
    v = m.along_edge()
    if m.piece not in (R, Q) or not v:
        return None
    px, py = _perp(v)
    inner = [s for i in (-1, 0, 1) if (s := _off(m.king, v[0] + i * px, v[1] + i * py)) is not None]
    line = chess.SquareSet(chess.BB_RANKS[chess.square_rank(inner[0])] if v[1] else chess.BB_FILES[chess.square_file(inner[0])])
    for h in line:
        if h != m.checker and m.kind(h) in (R, Q) and all(s in m.free and h in m.cover[s] for s in inner):
            return grade(m, [h], {"both_rooks": m.piece == R and m.kind(h) == R})
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
    requires = "checkmate"  # anatomy() is None otherwise
    tier = "fast"
    version = 3  # 2: family/form split, events carry form and traits. 3: Hook takes the pawn-backed chain from Arabian; every ladder is textbook; swallow's tail textbook needs only the two tail blockers

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
`,B=`import re

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
`,D=`"""Named mating patterns, defined only by the final position's geometry."""

import chess

from chesstrove.detectors.base import Event, checker_squares, event
from chesstrove.models import MoveContext


def _mated_king(ctx: MoveContext) -> tuple[chess.Board, chess.Color, chess.Square] | None:
    board = ctx.board_after
    if not ctx.facts.is_checkmate:
        return None
    mated = board.turn
    return board, mated, board.king(mated)


class SmotheredMate:
    """A knight gives mate, and each square next to the mated king is either held by the king's own
    pieces or covered by the mating knight itself: the knight alone does the work, no other piece helps
    trap the king. \`pure\` = every neighbouring square is the king's own piece (the textbook picture).
    The knight may be part of a double check."""

    id = "SMOTHERED_MATE"
    requires = "checkmate"
    tier = "fast"
    version = 2  # v2: squares covered by the mating knight itself count (v1 required all to be own pieces)

    def detect(self, ctx: MoveContext) -> list[Event]:
        found = _mated_king(ctx)
        if not found:
            return []
        board, mated, king = found
        mating_knights = board.checkers() & board.knights
        if not mating_knights:
            return []
        knight_cover = 0
        for square in mating_knights:
            knight_cover |= chess.BB_KNIGHT_ATTACKS[square]
        open_squares = chess.BB_KING_ATTACKS[king] & ~board.occupied_co[mated]
        if open_squares & ~knight_cover:
            return []  # some escape square is taken away by another piece, not the knight
        return [event(ctx, self.id, king_square=chess.square_name(king), checkers=checker_squares(board),
                      pure=not open_squares)]


class BackRankMate:
    """The mated king is on its own back rank, a rook or queen checks it along that rank, and every
    square next to the king on the following rank is occupied by the king's own pieces. Escape squares
    that are merely attacked don't count."""

    id = "BACK_RANK_MATE"
    requires = "checkmate"
    tier = "fast"
    version = 1

    def detect(self, ctx: MoveContext) -> list[Event]:
        found = _mated_king(ctx)
        if not found:
            return []
        board, mated, king = found
        back_rank = 0 if mated == chess.WHITE else 7
        if chess.square_rank(king) != back_rank:
            return []
        rank_checkers = [s for s in board.checkers()
                         if chess.square_rank(s) == back_rank and board.piece_type_at(s) in (chess.ROOK, chess.QUEEN)]
        if not rank_checkers:
            return []
        next_rank = chess.BB_RANKS[1 if mated == chess.WHITE else 6]
        if chess.BB_KING_ATTACKS[king] & next_rank & ~board.occupied_co[mated]:
            return []
        return [event(ctx, self.id, king_square=chess.square_name(king),
                      checker=chess.square_name(rank_checkers[0]),
                      checker_piece=chess.piece_name(board.piece_type_at(rank_checkers[0])))]
`,O=`import chess

from chesstrove.detectors.base import Event, event, piece_name
from chesstrove.models import MoveContext


class Underpromotion:
    """Any promotion to a knight, bishop or rook. Also records exact facts about queening on the same
    square instead (check / mate / stalemate). Those facts never claim which move was best; that
    needs the engine layer."""

    id = "UNDERPROMOTION"
    requires = "underpromotion"
    tier = "fast"
    version = 2  # v2: queen_gives_check, queen_gives_mate, queen_stalemates

    def detect(self, ctx: MoveContext) -> list[Event]:
        f = ctx.facts
        if f.promotion not in ("N", "B", "R"):
            return []
        board = ctx.board_before
        board.push(chess.Move(ctx.move.from_square, ctx.move.to_square, chess.QUEEN))
        queen = {"queen_gives_check": board.is_check(), "queen_gives_mate": board.is_checkmate(),
                 "queen_stalemates": board.is_stalemate()}
        board.pop()
        return [event(ctx, self.id, promotion_piece=piece_name(f.promotion), square=f.to_square,
                      is_capture=f.is_capture, gave_check=f.is_check, gave_mate=f.is_checkmate, **queen)]


class PromotionCheckmate:
    """A promotion (to any piece) that mates, including discovered mates where the new piece doesn't check."""

    id = "PROMOTION_CHECKMATE"
    requires = "checkmate"
    tier = "fast"
    version = 1

    def detect(self, ctx: MoveContext) -> list[Event]:
        f = ctx.facts
        if not (f.promotion and f.is_checkmate):
            return []
        promoted_piece_checks = ctx.move.to_square in ctx.board_after.checkers()
        return [event(ctx, self.id, promotion_piece=piece_name(f.promotion), square=f.to_square,
                      promoted_piece_checks=promoted_piece_checks)]
`,I="",F=`"""Chess.com public API -> CanonicalGame.

https://www.chess.com/news/view/published-data-api
  /pub/player/{user}/games/archives  -> {"archives": [".../games/2024/01", ...]}
  each archive                       -> {"games": [{"pgn", "rules", "rated", "eco", ...}]}
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

from chesstrove.importers.pgn import ParseFailure, read_one
from chesstrove.models import CanonicalGame

ARCHIVES_URL = "https://api.chess.com/pub/player/{}/games/archives"
STATS_URL = "https://api.chess.com/pub/player/{}/stats"
USER_AGENT = "ChessTrove/0.1 (personal chess history indexer)"  # Chess.com asks clients to identify themselves
SUPPORTED_RULES = ("chess", "chess960", "oddschess")  # odds chess = normal rules from a SetUp FEN
RETRY_STATUSES = (429, 500, 502, 503, 504)
FETCH_ERRORS = (urllib.error.URLError, TimeoutError, json.JSONDecodeError)


def profile(username: str, fetch: Callable[[str], Any] | None = None) -> dict:
    """From the player's stats: games played (wins + losses + draws over every chess mode; Chess.com counts
    rated games only, so the archives usually hold a few more) and the current rating in the mode they've
    played most."""
    stats = (fetch or fetch_json)(STATS_URL.format(urllib.parse.quote(username.lower())))
    modes = {key: v for key, v in stats.items() if key.startswith("chess") and isinstance(v, dict) and "record" in v}
    played = {key: sum(v["record"].get(k, 0) for k in ("win", "loss", "draw")) for key, v in modes.items()}
    top = max(played, key=played.get, default=None)
    rating = (modes[top].get("last") or {}).get("rating") if top else None
    mode = top and top.removeprefix("chess_").replace("chess960_", "960 ")
    return {"games": sum(played.values()), "rating": rating, "rating_mode": mode if rating else None}


def fetch_json(url: str, attempts: int = 4) -> Any:
    """GET JSON with backoff on rate limits and server errors. Requests are sequential on purpose:
    Chess.com rate-limits parallel requests, and one user's history is at most a few hundred months."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUSES or attempt == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
        time.sleep(2**attempt)


def archive_urls(username: str, fetch: Callable[[str], Any] = fetch_json) -> list[str]:
    return fetch(ARCHIVES_URL.format(urllib.parse.quote(username.lower())))["archives"]


def month_of(archive_url: str) -> str:
    """'https://api.chess.com/pub/player/x/games/2024/01' -> '2024/01' (sorts chronologically)."""
    year, month = archive_url.rstrip("/").split("/")[-2:]
    return f"{year}/{month}"


def games_in_archive(archive: dict) -> Iterator[CanonicalGame | ParseFailure]:
    for g in archive.get("games", []):
        ref = g.get("url", "")
        if g.get("rules") not in SUPPORTED_RULES:  # includes bughouse, which has no PGN at all
            yield ParseFailure(f"unsupported variant: {g.get('rules')}", ref, skipped=True)
            continue
        item = read_one(g.get("pgn"), "chesscom", ref)
        if isinstance(item, CanonicalGame):
            # The JSON knows things the PGN doesn't.
            item = replace(
                item,
                rated=g.get("rated", item.rated),
                opening=item.opening or _opening_from_eco_url(g.get("eco")),
            )
        yield item


def _opening_from_eco_url(url: str | None) -> str | None:
    # ".../openings/Pirc-Defense-Classical-Variation-4...Bg7" -> "Pirc Defense Classical Variation 4...Bg7"
    if not url or "/openings/" not in url:
        return None
    return urllib.parse.unquote(url.split("/openings/", 1)[1]).replace("-", " ") or None
`,G=`"""PGN text -> CanonicalGame.

Every importer yields CanonicalGame | ParseFailure. The Chess.com and Lichess APIs both return PGN,
so their importers fetch, then reuse to_canonical() with their own source/metadata.
"""

import hashlib
import io
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import chess
import chess.pgn

from chesstrove.models import CanonicalGame, Source

# Games that carry a platform URL get a platform key, so a file export and an API import dedupe together.
PLATFORM_URLS = [
    ("chesscom", re.compile(r"chess\\.com/game/(?:live|daily)/(\\d+)")),
    ("lichess", re.compile(r"lichess\\.org/([A-Za-z0-9]{8})\\b")),
]


@dataclass(frozen=True, slots=True)
class ParseFailure:
    error: str
    pgn: str
    skipped: bool = False  # intentionally not imported (unsupported variant), as opposed to broken


class UnsupportedVariant(ValueError):
    pass


def read_pgn(text: str, source: Source = "pgn") -> Iterator[CanonicalGame | ParseFailure]:
    """Yield one item per game in a (possibly multi-game) PGN string. Never raises on bad games."""
    # ponytail: whole file in memory (~100MB for 50k games); stream from disk if that ever hurts
    stream = io.StringIO(text)
    while True:
        start = stream.tell()
        game = chess.pgn.read_game(stream)
        if game is None:
            return
        raw = text[start : stream.tell()].strip()
        try:
            yield to_canonical(game, raw, source)
        except ValueError as e:
            yield ParseFailure(str(e), raw, skipped=isinstance(e, UnsupportedVariant))


def read_one(text: str | None, source: Source, ref: str) -> CanonicalGame | ParseFailure:
    """Exactly one game from an API response's PGN field; anything else is a ParseFailure naming \`ref\`."""
    if not text:
        return ParseFailure("no PGN in API response", ref)
    items = list(read_pgn(text, source))
    if len(items) != 1:
        return ParseFailure(f"expected one game in API PGN, got {len(items)}", ref)
    return items[0]


def to_canonical(game: chess.pgn.Game, raw: str, source: Source) -> CanonicalGame:
    if game.errors:
        raise ValueError(f"invalid PGN: {game.errors[0]}")
    board = game.board()
    if board.uci_variant != "chess":
        raise UnsupportedVariant(f"unsupported variant: {board.uci_variant}")

    h = game.headers
    moves = tuple(m.uci() for m in game.mainline_moves())
    if not moves and not raw.startswith("["):
        raise ValueError("no tags and no moves: not a PGN game")
    initial_fen = None if board.fen() == chess.STARTING_FEN else board.fen()
    platform_id = _platform_id(h)

    if platform_id:
        source_key, external_id = f"{platform_id[0]}:{platform_id[1]}", platform_id[1]
    else:
        # ponytail: identity = key headers + moves; two byte-identical casual games on one day collide
        identity = "|".join(
            [*(h.get(k, "") for k in ("Event", "Site", "Round", "Date", "UTCDate", "UTCTime", "White", "Black", "Result")),
             initial_fen or "", " ".join(moves)]
        )
        source_key, external_id = "sha256:" + hashlib.sha256(identity.encode()).hexdigest(), None

    return CanonicalGame(
        source=source,
        source_key=source_key,
        external_id=external_id,
        played_at=_played_at(h),
        white=_known(h.get("White")),
        black=_known(h.get("Black")),
        white_rating=_int(h.get("WhiteElo")),
        black_rating=_int(h.get("BlackElo")),
        result=h.get("Result", "*"),
        time_control=_known(h.get("TimeControl")),
        rated=_rated(h.get("Event", "")),
        eco=_known(h.get("ECO")),
        opening=_known(h.get("Opening")),
        initial_fen=initial_fen,
        moves_uci=moves,
        pgn=raw,
        chess960=board.chess960,
    )


def _platform_id(h: chess.pgn.Headers) -> tuple[str, str] | None:
    for header in ("Link", "Site"):
        for platform, pattern in PLATFORM_URLS:
            if m := pattern.search(h.get(header, "")):
                return platform, m.group(1)
    return None


def _known(value: str | None) -> str | None:
    return None if value in (None, "", "?", "-") else value


def _int(value: str | None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _rated(event: str) -> bool | None:
    # Lichess: "Rated Blitz game" / "Casual Blitz game". Chess.com PGNs don't say; its API does.
    if event.startswith("Rated"):
        return True
    if event.startswith("Casual"):
        return False
    return None


def _played_at(h: chess.pgn.Headers) -> datetime | None:
    # Only UTCTime is trustworthy as UTC; a bare Date is stored as midnight UTC.
    attempts = [(f"{h.get('UTCDate')} {h.get('UTCTime')}", "%Y.%m.%d %H:%M:%S"),
                (h.get("UTCDate") or h.get("Date") or "", "%Y.%m.%d")]
    for value, fmt in attempts:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None  # "????.??.??" or partial dates
`,U=`"""The deterministic indexer: replay a game, run the detectors, and produce exactly the rows the database stores
(the packed game_moves row and the event rows). No database, network, filesystem or environment: the server
importer runs it natively, and the public site runs this same file in the visitor's browser (Pyodide, in a Web
Worker: web/src/indexer), then uploads the rows. One definition, so the two can't drift.

Everything here must stay importable in Pyodide: the standard library and python-chess only.
"""

import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from chesstrove.detectors import DETECTORS, Detector, Event
from chesstrove.importers.chesscom import games_in_archive
from chesstrove.importers.pgn import ParseFailure
from chesstrove.models import CanonicalGame, MoveFacts
from chesstrove.reconstruction import replay

# The game columns a batch carries (everything \`games\` stores except the id and import).
GAME_FIELDS = ("source_key", "source", "external_id", "played_at", "white", "black", "white_rating", "black_rating",
               "result", "time_control", "rated", "eco", "opening", "initial_fen", "chess960", "pgn")


# The first pass is everything a player page needs; the deep pass is what's expensive and not needed for the
# headline (MISSED_MATE_IN_ONE: a mistake, not a rare moment, and ~40% of all indexing time on its own).
FAST = tuple(d for d in DETECTORS if d.tier == "fast")
DEEP = tuple(d for d in DETECTORS if d.tier == "deep")

# When a detector can possibly fire. Each detector names (\`requires\`) the first condition its own detect() checks,
# so skipping it on other plies changes nothing: tests/test_indexing.py compares against every detector on every
# ply. A mate on the board is ~1 ply in 100, so most detectors run on almost none.
GATES: dict[str, Callable[[MoveFacts], bool]] = {
    "any": lambda f: True,
    "checkmate": lambda f: f.is_checkmate,
    "not_checkmate": lambda f: not f.is_checkmate,
    "check": lambda f: f.is_check,
    "underpromotion": lambda f: f.promotion in ("N", "B", "R"),
    "three_queens": lambda f: f.queens_after >= 3,
    "piece_move": lambda f: f.piece != "P",  # SAN like Qh4e1 starts with a piece letter
}


def versions(detectors: Sequence[Detector] = DETECTORS) -> dict[str, int]:
    return {d.id: d.version for d in detectors}


def analyze(game: CanonicalGame, detectors: Sequence[Detector]) -> tuple[list[MoveFacts], list[tuple[Detector, Event]]]:
    """One replay; each detector sees the plies its precondition allows (GATES). No FEN strings are built unless an
    event needs its position."""
    gated = [(GATES[getattr(d, "requires", "any")], d) for d in detectors]  # undeclared: every ply
    facts: list[MoveFacts] = []
    events: list[tuple[Detector, Event]] = []
    for ctx in replay(game, fens=False):
        f = ctx.facts
        facts.append(f)
        for gate, detector in gated:
            if gate(f):
                events.extend((detector, e) for e in detector.detect(ctx))
    return facts, events


def pack_moves(facts: list[MoveFacts]) -> dict[str, Any]:
    """A game's plies as its game_moves row (schema.sql): strings and small-int arrays, ply i at position i-1."""
    return {
        "first_color": facts[0].color,
        "uci": " ".join(f.uci for f in facts),
        "san": " ".join(f.san for f in facts),
        "piece": "".join(f.piece for f in facts),
        "captured": "".join(f.captured or "." for f in facts),
        "promotion": "".join(f.promotion or "." for f in facts),
        "flags": [f.is_check + 2 * f.is_checkmate + 4 * f.is_castling + 8 * f.is_en_passant for f in facts],
        "material_white": [f.material_white for f in facts],
        "material_black": [f.material_black for f in facts],
        "queens_after": [f.queens_after for f in facts],
        "legal_moves_before": [f.legal_moves_before for f in facts],
    }


def event_rows(events: list[tuple[Detector, Event]]) -> list[dict[str, Any]]:
    """Events as the \`events\` table stores them (less the game id and analysis run)."""
    return [{"detector_id": d.id, "detector_version": d.version, "ply": e.ply, "type": e.type, "color": e.color,
             "fen": e.fen, "metadata": e.metadata} for d, e in events]


def index_game(game: CanonicalGame, detectors: Sequence[Detector] = FAST) -> dict[str, Any]:
    """Everything the database stores for one game: its columns, packed moves (None for a game with no moves)
    and events. Default: the first pass (FAST), which is what the browser indexes."""
    facts, events = analyze(game, detectors)
    row = {f: getattr(game, f) for f in GAME_FIELDS}
    row["played_at"] = game.played_at.isoformat() if game.played_at else None
    return {**row, "ply_count": len(facts), "moves": pack_moves(facts) if facts else None, "events": event_rows(events)}


def game_from_row(row: dict[str, Any]) -> CanonicalGame:
    """The inverse of index_game's columns (moves come from the packed uci)."""
    fields = {f: row[f] for f in GAME_FIELDS}
    fields["played_at"] = datetime.fromisoformat(row["played_at"]) if row["played_at"] else None
    uci = tuple(row["moves"]["uci"].split()) if row.get("moves") else ()
    return CanonicalGame(**fields, moves_uci=uci)


def index_chesscom_archive(archive_json: str) -> str:
    """The browser worker's entry point: one Chess.com monthly archive (JSON text) in, the indexed games out
    (JSON text, first pass), with the parse outcome counted the way the server importer counts it."""
    games, skipped, errors = [], 0, []
    for index, item in enumerate(games_in_archive(json.loads(archive_json)), start=1):
        if isinstance(item, ParseFailure):
            if item.skipped:
                skipped += 1
            else:
                errors.append({"index": index, "error": item.error})
            continue
        try:
            games.append(index_game(item))
        except Exception as e:  # the server importer records these and moves on; so does the browser
            errors.append({"index": index, "error": f"{type(e).__name__}: {e}"})
    return json.dumps({"games": games, "skipped": skipped, "errors": errors})


def deep_scan(games_json: str) -> str:
    """The browser worker's deep pass over games already stored: [{source_key, initial_fen, chess960, uci}] in,
    [{source_key, events}] out, for the DEEP detectors only."""
    out = []
    for g in json.loads(games_json):
        game = CanonicalGame(source="chesscom", source_key=g["source_key"], external_id=None, played_at=None, white=None,
                             black=None, white_rating=None, black_rating=None, result="*", time_control=None, rated=None,
                             eco=None, opening=None, initial_fen=g["initial_fen"], moves_uci=tuple(g["uci"].split()),
                             pgn="", chess960=g["chess960"])
        out.append({"source_key": g["source_key"], "events": event_rows(analyze(game, DEEP)[1])})
    return json.dumps(out)
`,K=`"""Core, source-independent data types."""

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
`,j=`"""One-pass replay of a game's mainline into MoveContexts."""

from collections.abc import Iterator

import chess

from chesstrove.models import CanonicalGame, MoveContext, MoveFacts

PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}


def material(board: chess.Board, color: chess.Color) -> int:
    return sum(v * chess.popcount(board.pieces_mask(pt, color)) for pt, v in PIECE_VALUES.items())


def queen_count(board: chess.Board) -> int:
    return chess.popcount(board.queens)


def start_board(game: CanonicalGame) -> chess.Board:
    return chess.Board(game.initial_fen or chess.STARTING_FEN, chess960=game.chess960)


def replay(game: CanonicalGame, fens: bool = True) -> Iterator[MoveContext]:
    """Replay the mainline once, yielding a context per ply.

    board_after is the live board and is only valid until the next iteration. fens=False skips the two FEN strings
    per ply (a tenth of indexing time) and leaves facts.fen_before/fen_after None: an event takes its position from
    board_before instead, the same string (tests/test_reconstruction.py).
    """
    board = start_board(game)
    fen_before = board.fen() if fens else None
    queens_before = queen_count(board)
    for ply, uci in enumerate(game.moves_uci, start=1):
        move = board.parse_uci(uci)  # raises IllegalMoveError on corrupt input

        # ponytail: one Board.copy per ply (~µs); switch detectors to push/pop on one board if the benchmark says so
        board_before = board.copy(stack=False)
        legal = list(board.legal_moves)  # counted and stored; MISSED_MATE_IN_ONE reuses the list
        san = board.san(move)
        piece = board.piece_type_at(move.from_square)
        is_en_passant = board.is_en_passant(move)
        captured = chess.PAWN if is_en_passant else board.piece_type_at(move.to_square)
        is_castling = board.is_castling(move)
        if is_castling:  # castling is encoded as king-takes-own-rook in 960; never a capture
            captured = None
        color = "w" if board.turn == chess.WHITE else "b"

        board.push(move)

        fen_after = board.fen() if fens else None
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
            legal_moves_before=len(legal),
        )
        yield MoveContext(game, ply, board_before, move, board, san, facts, legal)
        fen_before, queens_before = fen_after, queens_after
`;const Q=(n,e)=>e>=1?0:n*(1-e)/e;function L(n,e,t){const r=new Set(e.filter(s=>s<t));return n.map(s=>s.split("/games/")[1]).filter(s=>s&&!r.has(s)).sort().reverse()}function w(n,e){const t=[];for(let r=0;r<n.length;r+=e)t.push({games:n.slice(r,r+e),last:r+e>=n.length});return t.length?t:[{games:[],last:!0}]}async function h(n,e,t,r=fetch,s=o=>new Promise(a=>setTimeout(a,o))){let o=2e3;for(let a=0;;a++){try{const i=await r(n,e);if(i.ok||i.status<500&&i.status!==429&&i.status!==408)return i}catch{}if(a===7)throw new Error(`${t} didn't answer`);await s(o),o=Math.min(o*2,3e4)}}const y="https://cdn.jsdelivr.net/pyodide/v0.28.3/full/",H="/py/python-chess-1.11.2.zip",V=Object.assign({"../../../src/chesstrove/__init__.py":E,"../../../src/chesstrove/detectors/__init__.py":S,"../../../src/chesstrove/detectors/base.py":C,"../../../src/chesstrove/detectors/checks.py":M,"../../../src/chesstrove/detectors/material.py":A,"../../../src/chesstrove/detectors/mates.py":R,"../../../src/chesstrove/detectors/named_mates.py":P,"../../../src/chesstrove/detectors/notation.py":B,"../../../src/chesstrove/detectors/patterns.py":D,"../../../src/chesstrove/detectors/promotion.py":O,"../../../src/chesstrove/importers/__init__.py":I,"../../../src/chesstrove/importers/chesscom.py":F,"../../../src/chesstrove/importers/pgn.py":G,"../../../src/chesstrove/indexing.py":U,"../../../src/chesstrove/models.py":K,"../../../src/chesstrove/reconstruction.py":j}),k=n=>postMessage(n),z=n=>new Promise(e=>setTimeout(e,n)),d=async(n,e)=>{const t=performance.now();try{return await e()}finally{k({type:"timing",name:n,ms:performance.now()-t})}};class _ extends Error{}async function x(){const{loadPyodide:n}=await import(`${y}pyodide.mjs`),e=await n({indexURL:y}),t="/home/pyodide";e.unpackArchive(await(await h(H,void 0,"ChessTrove")).arrayBuffer(),"zip",{extractDir:t});for(const[s,o]of Object.entries(V)){const a=`${t}/chesstrove/${s.split("/src/chesstrove/")[1]}`;e.FS.mkdirTree(a.slice(0,a.lastIndexOf("/"))),e.FS.writeFile(a,o)}e.runPython("import json, chesstrove.indexing as ix");const r=e.globals.get("ix");return{index:r.index_chesscom_archive,deep:r.deep_scan,fast:JSON.parse(e.runPython("json.dumps(ix.versions(ix.FAST))")),deepVersions:JSON.parse(e.runPython("json.dumps(ix.versions(ix.DEEP))"))}}async function N(n,e){const t=performance.now(),r=await d("analyze",e),s=Q(performance.now()-t,n);return s>0&&await d("rest",()=>z(s)),r}const b=async n=>{var t;const e=((t=await n.json().catch(()=>({})))==null?void 0:t.detail)??`error ${n.status}`;return new _(n.status===409?String(e):`ChessTrove's server refused a batch (${e}).`)},q=(n,e)=>JSON.stringify(n)===JSON.stringify(e),T="This page is older than ChessTrove's server. Reload the page to continue.";async function $(n){const e={type:"progress",phase:"loading",archivesTotal:0,archivesDone:0,month:null,analyzed:0,uploaded:0,events:0,uploading:!1},t=()=>k({...e});t();const r=await d("pyodide",x);if(!q(r.fast,n.versions))throw new _(T);e.phase="listing",t();const s=`https://api.chess.com/pub/player/${encodeURIComponent(n.username)}/games`,o=await d("fetch",()=>h(`${s}/archives`,void 0,"Chess.com"));if(o.status===404)throw new _(`Chess.com has no player called “${n.username}”.`);const a=new Date().toISOString().slice(0,7).replace("-","/"),i=L((await o.json()).archives??[],n.monthsDone,a);e.archivesTotal=i.length,e.phase="indexing",t();const g=async(f,p,v)=>{const c=JSON.stringify({versions:n.versions,month:f,month_complete:v,games:p.games,skipped:p.skipped,errors:p.errors});e.uploading=!0,t();const l=await d("upload",()=>h(`${n.apiBase}/api/indexing/${n.importId}/batches`,{method:"POST",headers:{"Content-Type":"application/json","X-Import-Token":n.token},body:c},"ChessTrove's server"));if(!l.ok)throw await b(l);const u=await l.json();e.uploaded+=p.games.length,e.events+=u.events,e.uploading=!1,t(),v&&k({type:"month",month:f})};let m=Promise.resolve();for(const f of i){e.month=f,t();const p=await d("fetch",()=>h(`${s}/${f}`,void 0,"Chess.com")),v=JSON.parse(await p.text());let c={games:[],skipped:0,errors:[]};for(const l of w(v.games??[],n.slice)){const u=JSON.parse(await N(n.duty,()=>r.index(JSON.stringify({games:l.games}))));e.analyzed+=u.games.length,t(),c={games:[...c.games,...u.games],skipped:c.skipped+u.skipped,errors:[...c.errors,...u.errors]},(c.games.length>=n.batchSize||l.last)&&(await m,m=g(f,c,l.last),c={games:[],skipped:0,errors:[]})}e.archivesDone+=1,t()}if(await m,!(await h(`${n.apiBase}/api/indexing/${n.importId}/finish`,{method:"POST",headers:{"X-Import-Token":n.token}},"ChessTrove's server")).ok)throw new _("ChessTrove's server didn't accept the finished import.");e.phase="done",e.month=null,t()}async function W(n){const e={type:"progress",phase:"loading",archivesTotal:0,archivesDone:0,month:null,analyzed:0,uploaded:0,events:0,uploading:!1},t=()=>k({...e});t();const r=await d("pyodide",x);if(!q(r.deepVersions,n.versions))throw new _(T);e.phase="deep",t();const s={"X-Import-Token":n.token};for(;;){const o=await d("fetch",()=>h(`${n.apiBase}/api/indexing/deep/${n.runId}/games`,{headers:s},"ChessTrove's server"));if(!o.ok)throw await b(o);const a=await o.json();if(!a.length)break;let i=[];for(const m of w(a,n.slice))i=[...i,...JSON.parse(await N(n.duty,()=>r.deep(JSON.stringify(m.games))))],e.analyzed+=m.games.length,t();e.uploading=!0,t();const g=await d("upload",()=>h(`${n.apiBase}/api/indexing/deep/${n.runId}/batches`,{method:"POST",headers:{"Content-Type":"application/json",...s},body:JSON.stringify({versions:n.versions,games:i})},"ChessTrove's server"));if(!g.ok)throw await b(g);e.uploaded+=a.length,e.events+=(await g.json()).events,e.uploading=!1,t()}await h(`${n.apiBase}/api/indexing/deep/${n.runId}/finish`,{method:"POST",headers:s},"ChessTrove's server"),e.phase="done",t()}self.onmessage=n=>{(n.data.type==="deep"?W(n.data):$(n.data)).catch(e=>k({type:"error",message:e instanceof Error?e.message:String(e),retry:!(e instanceof _)}))};
