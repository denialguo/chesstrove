import io
import urllib.error
import urllib.parse

import pytest

from chesstrove import db, ingest
from chesstrove.importers import lichess
from chesstrove.importers.pgn import ParseFailure
from chesstrove.ingest import import_lichess, import_pgn
from chesstrove.models import CanonicalGame


def lichess_game(n: int, created: int, status: str = "mate", variant: str = "standard", rated: bool = True) -> dict:
    game_id = f"game{n:04d}"
    pgn = (f'[Event "Rated Blitz game"]\n[Site "https://lichess.org/{game_id}"]\n[White "alice"]\n'
           f'[Black "bob"]\n[Result "*"]\n[Variant "Standard"]\n\n1. e4 e5 2. Nf3 Nc6 *\n')
    return {"id": game_id, "createdAt": created, "status": status, "variant": variant, "rated": rated, "pgn": pgn}


class FakeLichess:
    """Honors `since` and returns games oldest first, like the real export with sort=dateAsc."""

    def __init__(self, games: list[dict], fail_after: int | None = None):
        self.games = games
        self.fail_after = fail_after
        self.urls: list[str] = []

    def since_of(self, url: str) -> int:
        return int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["since"][0])

    def __call__(self, url: str):
        self.urls.append(url)
        since = self.since_of(url)
        for i, g in enumerate(sorted((g for g in self.games if g["createdAt"] >= since), key=lambda g: g["createdAt"])):
            if self.fail_after is not None and i == self.fail_after:
                raise ConnectionResetError("stream dropped")
            yield g


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def checkpoint(conn) -> int:
    return db.lichess_checkpoint(conn, "alice")


# --- parsing -------------------------------------------------------------------------------------

def test_to_item():
    game = lichess.to_item(lichess_game(1, 1000, rated=False))
    assert isinstance(game, CanonicalGame)
    assert (game.source, game.source_key, game.rated) == ("lichess", "lichess:game0001", False)
    atomic = lichess.to_item(lichess_game(2, 1000, variant="atomic"))
    assert isinstance(atomic, ParseFailure) and "atomic" in atomic.error and "game0002" in atomic.pgn
    assert isinstance(lichess.to_item({**lichess_game(3, 1000), "pgn": None}), ParseFailure)


def test_from_position_is_normal_chess():
    g = lichess_game(4, 1000, variant="fromPosition")
    g["pgn"] = g["pgn"].replace('[Variant "Standard"]', '[Variant "From Position"]\n[SetUp "1"]\n'
                                '[FEN "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1"]').replace("1. e4 e5 2. Nf3 Nc6", "1. e4")
    item = lichess.to_item(g)
    assert isinstance(item, CanonicalGame) and item.initial_fen == "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1"


def test_export_url():
    query = urllib.parse.parse_qs(urllib.parse.urlparse(lichess.export_url("Alice", 123)).query)
    assert {k: v[0] for k, v in query.items()}.items() >= {
        "since": "123", "sort": "dateAsc", "ongoing": "true", "pgnInJson": "true"}.items()
    assert "/api/games/user/alice?" in lichess.export_url("Alice", 0)


# --- import & resume -----------------------------------------------------------------------------

def test_import_and_checkpoint(conn):
    api = FakeLichess([lichess_game(1, 1000), lichess_game(2, 2000), lichess_game(3, 3000, variant="horde")])
    imp = db.get_import(conn, import_lichess(conn, "Alice", open_stream=api))
    assert (imp["status"], imp["source_ref"], imp["games_imported"], imp["games_failed"]) == ("completed", "alice", 2, 1)
    assert imp["resume_state"] == {"since": 3001}  # a deterministic parse failure doesn't hold the checkpoint back
    assert conn.execute("SELECT platform FROM chess_accounts").fetchone()["platform"] == "lichess"
    assert api.since_of(api.urls[0]) == 0


def test_second_run_starts_at_checkpoint(conn):
    games = [lichess_game(1, 1000), lichess_game(2, 2000)]
    import_lichess(conn, "alice", open_stream=FakeLichess(games))
    api = FakeLichess([*games, lichess_game(3, 5000)])
    imp = db.get_import(conn, import_lichess(conn, "alice", open_stream=api))
    assert api.since_of(api.urls[0]) == 2001
    assert (imp["games_imported"], imp["games_duplicate"]) == (1, 0)
    assert checkpoint(conn) == 5001


def test_ongoing_game_holds_the_checkpoint(conn):
    live = lichess_game(2, 2000, status="started")
    imp = db.get_import(conn, import_lichess(conn, "alice", open_stream=FakeLichess(
        [lichess_game(1, 1000), live, lichess_game(3, 3000)])))
    assert imp["games_imported"] == 2  # the ongoing game isn't stored
    assert checkpoint(conn) == 1001  # stops before the live game, so the next run asks for it again

    finished = {**live, "status": "resign"}
    imp = db.get_import(conn, import_lichess(conn, "alice", open_stream=FakeLichess(
        [lichess_game(1, 1000), finished, lichess_game(3, 3000)])))
    assert (imp["games_imported"], imp["games_duplicate"]) == (1, 1)
    assert checkpoint(conn) == 3001 and count(conn, "games") == 3


def test_dropped_stream_keeps_committed_progress(conn, monkeypatch):
    monkeypatch.setattr(ingest, "BATCH_SIZE", 1)
    games = [lichess_game(i, i * 1000) for i in range(1, 5)]
    with pytest.raises(ConnectionResetError):
        import_lichess(conn, "alice", open_stream=FakeLichess(games, fail_after=2))
    [imp] = db.list_imports(conn)
    assert imp["status"] == "failed" and "stream dropped" in imp["errors"][-1]["error"]
    assert count(conn, "games") == 2 and checkpoint(conn) == 2001

    api = FakeLichess(games)
    imp = db.get_import(conn, import_lichess(conn, "alice", open_stream=api))
    assert api.since_of(api.urls[0]) == 2001 and imp["games_imported"] == 2 and count(conn, "games") == 4


def test_game_that_raises_is_retried(conn, monkeypatch):
    monkeypatch.setattr(ingest, "BATCH_SIZE", 1)
    real_store_game = ingest.store_game

    def flaky(conn, game, import_id, run):
        if game.source_key == "lichess:game0002":
            raise RuntimeError("transient")
        return real_store_game(conn, game, import_id, run)

    games = [lichess_game(1, 1000), lichess_game(2, 2000), lichess_game(3, 3000)]
    monkeypatch.setattr(ingest, "store_game", flaky)
    imp = db.get_import(conn, import_lichess(conn, "alice", open_stream=FakeLichess(games)))
    assert (imp["status"], imp["games_imported"], imp["games_failed"]) == ("completed", 2, 1)
    assert checkpoint(conn) == 1001  # held before the failed game

    monkeypatch.setattr(ingest, "store_game", real_store_game)
    imp = db.get_import(conn, import_lichess(conn, "alice", open_stream=FakeLichess(games)))
    assert (imp["games_imported"], imp["games_duplicate"]) == (1, 1) and checkpoint(conn) == 3001


def test_unknown_user_fails_the_import(conn):
    def not_found(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO())  # type: ignore[arg-type]

    with pytest.raises(urllib.error.HTTPError):
        import_lichess(conn, "alice", open_stream=not_found)
    [imp] = db.list_imports(conn)
    assert imp["status"] == "failed" and "404" in imp["errors"][-1]["error"]


def test_pgn_export_and_api_import_dedupe(conn):
    import_pgn(conn, lichess_game(1, 1000)["pgn"], "lichess-export.pgn")
    imp = db.get_import(conn, import_lichess(conn, "alice", open_stream=FakeLichess([lichess_game(1, 1000)])))
    assert imp["games_duplicate"] == 1 and count(conn, "games") == 1


# --- HTTP ----------------------------------------------------------------------------------------

def test_open_ndjson_waits_out_rate_limit_and_sends_token(monkeypatch):
    responses = [urllib.error.HTTPError("u", 429, "Too Many", {}, io.BytesIO()),  # type: ignore[arg-type]
                 io.BytesIO(b'{"id": "a"}\n\n{"id": "b"}\n')]
    sleeps = []

    def urlopen(request, timeout):
        assert request.get_header("Authorization") == "Bearer secret"
        assert request.get_header("Accept") == "application/x-ndjson"
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setenv("LICHESS_TOKEN", "secret")
    monkeypatch.setattr(lichess.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(lichess.time, "sleep", sleeps.append)
    assert list(lichess.open_ndjson("https://x.test/")) == [{"id": "a"}, {"id": "b"}]
    assert sleeps == [60]
