"""PGN text -> CanonicalGame.

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
    ("chesscom", re.compile(r"chess\.com/game/(?:live|daily)/(\d+)")),
    ("lichess", re.compile(r"lichess\.org/([A-Za-z0-9]{8})\b")),
]


@dataclass(frozen=True, slots=True)
class ParseFailure:
    error: str
    pgn: str


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
            yield ParseFailure(str(e), raw)


def to_canonical(game: chess.pgn.Game, raw: str, source: Source) -> CanonicalGame:
    if game.errors:
        raise ValueError(f"invalid PGN: {game.errors[0]}")
    board = game.board()
    if board.uci_variant != "chess":
        raise ValueError(f"unsupported variant: {board.uci_variant}")

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
