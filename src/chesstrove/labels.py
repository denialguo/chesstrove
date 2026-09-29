"""Engine-derived labels (BLUNDER, MISSED_WIN, ONLY_WINNING_MOVE), computed at query time from stored
evaluations. Thresholds are parameters, not stored: changing .30 to .25 relabels everything instantly
and never re-runs Stockfish.

Expected score (0..1, the mover's point of view) comes on two scales:
  lichess (default)  Lichess's win% curve on centipawns, fitted to human games.
  stockfish          (W + D/2) / 1000 from Stockfish's WDL model, calibrated to engine-strength play. So
                     steep that ordinary human swings look like blunders: on a real 176k-move history it
                     labelled 8.9% of moves BLUNDER vs 2.8% on the lichess scale, and 1,687 moves tied at
                     the maximum drop.
Both are scale-robust in a way raw centipawns aren't: +3 -> +6 barely moves them, 0 -> +3 a lot.
"""

from dataclasses import dataclass
from typing import Literal

import psycopg

from chesstrove import db

Label = Literal["BLUNDER", "MISSED_WIN", "ONLY_WINNING_MOVE"]
LABELS: tuple[Label, ...] = ("BLUNDER", "MISSED_WIN", "ONLY_WINNING_MOVE")
Scale = Literal["lichess", "stockfish"]
SCALES: tuple[Scale, ...] = ("lichess", "stockfish")


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
    scale: Scale = "lichess",
    include_recaptures: bool = False,
    platform: str | None = None,
) -> list[dict]:
    """Labelled moves, most dramatic first (largest expected-score drop; for ONLY_WINNING_MOVE, the
    biggest gap between the only winning move and the runner-up, quiet moves first among equals).
    ONLY_WINNING_MOVE skips recaptures on the square the opponent just captured on unless asked."""
    config = db.get_engine_config(conn, config_id) if config_id else db.default_engine_config(conn)
    if config is None:
        return []
    return db.engine_label_rows(conn, label, config["id"], t.blunder, t.winning, t.not_winning, player, limit,
                                scale, include_recaptures, platform)
