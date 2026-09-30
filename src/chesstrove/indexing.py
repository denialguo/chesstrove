"""The deterministic indexer: replay a game, run the detectors, and produce exactly the rows the database stores
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

# The game columns a batch carries (everything `games` stores except the id and import).
GAME_FIELDS = ("source_key", "source", "external_id", "played_at", "white", "black", "white_rating", "black_rating",
               "result", "time_control", "rated", "eco", "opening", "initial_fen", "chess960", "pgn")


# The first pass is everything a player page needs; the deep pass is what's expensive and not needed for the
# headline (MISSED_MATE_IN_ONE: a mistake, not a rare moment, and ~40% of all indexing time on its own).
FAST = tuple(d for d in DETECTORS if d.tier == "fast")
DEEP = tuple(d for d in DETECTORS if d.tier == "deep")

# When a detector can possibly fire. Each detector names (`requires`) the first condition its own detect() checks,
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
    """Events as the `events` table stores them (less the game id and analysis run)."""
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
