"""Lichess game export -> CanonicalGame.

https://lichess.org/api#tag/Games/operation/apiGamesUser
  GET /api/games/user/{name}?since=<ms>&sort=dateAsc&ongoing=true&pgnInJson=true  (NDJSON stream)

Anonymous exports stream ~20 games/s; set LICHESS_TOKEN (any personal token) for 30, or 60 for your own games.
Lichess allows one request at a time and asks clients to wait a full minute after a 429.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import replace
from typing import Any

from chesstrove.importers.pgn import ParseFailure, read_one
from chesstrove.models import CanonicalGame

EXPORT_URL = "https://lichess.org/api/games/user/{}"
USER_AGENT = "ChessTrove/0.1 (personal chess history indexer)"
SUPPORTED_VARIANTS = ("standard", "chess960", "fromPosition")  # fromPosition = normal chess from a FEN
ONGOING = ("created", "started")
RATE_LIMIT_WAIT = 60  # seconds, per Lichess API guidelines

OpenStream = Callable[[str], Iterable[dict]]


def export_url(username: str, since_ms: int) -> str:
    params = {"since": since_ms, "sort": "dateAsc", "ongoing": "true", "pgnInJson": "true",
              "opening": "true", "clocks": "false", "evals": "false"}
    return EXPORT_URL.format(urllib.parse.quote(username.lower())) + "?" + urllib.parse.urlencode(params)


def open_ndjson(url: str, attempts: int = 3) -> Iterator[dict]:
    """Stream one JSON object per line. Retries a 429 (after the minute Lichess asks for) before the
    stream starts; errors mid-stream propagate, and the caller resumes from its last checkpoint."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/x-ndjson"}
    if token := os.environ.get("LICHESS_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(attempts):
        try:
            response = urllib.request.urlopen(request, timeout=60)
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == attempts - 1:
                raise
            time.sleep(RATE_LIMIT_WAIT)
    with response:
        for line in response:
            if line.strip():
                yield json.loads(line)


def is_ongoing(game: dict) -> bool:
    return game.get("status") in ONGOING


def to_item(game: dict) -> CanonicalGame | ParseFailure:
    ref = f"https://lichess.org/{game.get('id', '?')}"
    if game.get("variant") not in SUPPORTED_VARIANTS:
        return ParseFailure(f"unsupported variant: {game.get('variant')}", ref, skipped=True)
    item = read_one(game.get("pgn"), "lichess", ref)
    if isinstance(item, CanonicalGame) and "rated" in game:
        item = replace(item, rated=game["rated"])
    return item


def stream_games(username: str, since_ms: int, open_stream: OpenStream = open_ndjson) -> Iterable[dict[str, Any]]:
    return open_stream(export_url(username, since_ms))
