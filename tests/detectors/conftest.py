import pytest

from chesstrove.detectors import Detector, Event
from chesstrove.importers.pgn import read_pgn
from chesstrove.models import CanonicalGame
from chesstrove.reconstruction import replay


@pytest.fixture
def detect():
    """detect(detector, movetext, fen=None) -> every event the detector emits over the game."""

    def run(detector: Detector, movetext: str, fen: str | None = None) -> list[Event]:
        headers = f'[SetUp "1"]\n[FEN "{fen}"]\n' if fen else ""
        [game] = read_pgn(f'{headers}[Result "*"]\n\n{movetext} *\n')
        assert isinstance(game, CanonicalGame), game
        return [e for ctx in replay(game) for e in detector.detect(ctx)]

    return run
