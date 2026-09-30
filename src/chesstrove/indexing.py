"""The deterministic indexer: replay a game, run the detectors, and produce exactly the rows the database stores
(the packed game_moves row and the event rows). No database, network, filesystem or environment: the server
importer runs it natively, and the public site runs this same file in the visitor's browser (Pyodide, in a Web
Worker: web/src/indexer), then uploads the rows. One definition, so the two can't drift.

Everything here must stay importable in Pyodide: the standard library and python-chess only.
"""

import json
from collections.abc import Sequence
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


def versions(detectors: Sequence[Detector] = DETECTORS) -> dict[str, int]:
    return {d.id: d.version for d in detectors}


def analyze(game: CanonicalGame, detectors: Sequence[Detector]) -> tuple[list[MoveFacts], list[tuple[Detector, Event]]]:
    """One replay; every detector sees every ply."""
    facts: list[MoveFacts] = []
    events: list[tuple[Detector, Event]] = []
    for ctx in replay(game):
        facts.append(ctx.facts)
        for detector in detectors:
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


def index_game(game: CanonicalGame, detectors: Sequence[Detector] = DETECTORS) -> dict[str, Any]:
    """Everything the database stores for one game: its columns, packed moves (None for a game with no moves)
    and events."""
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
    (JSON text), with the parse outcome counted the way the server importer counts it."""
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
