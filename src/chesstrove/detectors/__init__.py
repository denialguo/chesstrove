"""Detector registry. Adding a detector = write the class, add one line here, add tests."""

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
