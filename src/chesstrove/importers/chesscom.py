"""Chess.com public API -> CanonicalGame.

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


def expected_games(username: str, fetch: Callable[[str], Any] | None = None) -> int:
    """Games the player has played, from their stats: wins + losses + draws over every chess mode. Chess.com
    counts rated games only, so the archives usually hold a few more."""
    stats = (fetch or fetch_json)(STATS_URL.format(urllib.parse.quote(username.lower())))
    return sum(sum(v["record"].get(k, 0) for k in ("win", "loss", "draw"))
               for key, v in stats.items() if key.startswith("chess") and isinstance(v, dict) and "record" in v)


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
