"""Engine-derived labels (BLUNDER, MISSED_WIN, ONLY_WINNING_MOVE), computed at query time from stored
evaluations. Thresholds are parameters, not stored: changing .30 to .25 relabels everything instantly
and never re-runs Stockfish.

Expected score = (W + D/2) / 1000 from Stockfish's own WDL model, from the mover's point of view.
Delivering mate counts as 1.0, stalemate 0.5. Scale-robust in a way centipawns aren't: +3 -> +6 barely
moves it, 0 -> +3 moves it a lot.
"""

from dataclasses import dataclass
from typing import Literal

import psycopg

from chesstrove import db

Label = Literal["BLUNDER", "MISSED_WIN", "ONLY_WINNING_MOVE"]
LABELS: tuple[Label, ...] = ("BLUNDER", "MISSED_WIN", "ONLY_WINNING_MOVE")


@dataclass(frozen=True, slots=True)
class Thresholds:
    blunder: float = 0.30  # expected-score drop
    winning: float = 0.90  # "clearly winning"
    not_winning: float = 0.60  # "no longer winning"


def query(
    conn: psycopg.Connection,
    label: Label,
    t: Thresholds = Thresholds(),
    config_id: int | None = None,
    player: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Labelled moves, most dramatic first (largest expected-score drop; for ONLY_WINNING_MOVE, the
    biggest gap between the only winning move and the runner-up)."""
    config = db.get_engine_config(conn, config_id) if config_id else db.default_engine_config(conn)
    if config is None:
        return []
    return db.engine_label_rows(conn, label, config["id"], t.blunder, t.winning, t.not_winning, player, limit)
