import io
import urllib.error
from datetime import UTC, datetime

import pytest

from chesstrove import db
from chesstrove.importers import chesscom
from chesstrove.importers.pgn import ParseFailure
from chesstrove.ingest import import_chesscom, import_pgn
from chesstrove.models import CanonicalGame

BASE = "https://api.chess.com/pub/player/alice/games"
ARCHIVES = f"{BASE}/archives"
NOW = datetime(2024, 3, 15, tzinfo=UTC)  # 2024/03 is the "current" month


def api_game(game_id: int, moves: str = "1. e4 e5 2. Nf3 Nc6 *", rules: str = "chess", rated: bool = True) -> dict:
    pgn = (f'[Event "Live Chess"]\n[White "alice"]\n[Black "bob"]\n[Result "*"]\n'
           f'[Link "https://www.chess.com/game/live/{game_id}"]\n\n{moves}\n')
    return {"url": f"https://www.chess.com/game/live/{game_id}", "pgn": pgn, "rules": rules, "rated": rated,
            "eco": "https://www.chess.com/openings/Italian-Game"}


class FakeApi:
    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str):
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def fake_api() -> FakeApi:
    return FakeApi({
        ARCHIVES: {"archives": [f"{BASE}/2024/01", f"{BASE}/2024/02", f"{BASE}/2024/03"]},
        f"{BASE}/2024/01": {"games": [api_game(1), api_game(2)]},
        f"{BASE}/2024/02": {"games": [api_game(3), api_game(4, rules="crazyhouse")]},
        f"{BASE}/2024/03": {"games": [api_game(5)]},
    })


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_games_in_archive_maps_api_fields():
    items = list(chesscom.games_in_archive({"games": [api_game(7, rated=False), api_game(8, rules="bughouse"),
                                                      {"url": "x", "rules": "chess"}]}))
    game, variant, missing = items
    assert isinstance(game, CanonicalGame)
    assert (game.source, game.source_key, game.rated, game.opening) == ("chesscom", "chesscom:7", False, "Italian Game")
    assert isinstance(variant, ParseFailure) and "bughouse" in variant.error
    assert isinstance(missing, ParseFailure) and "no PGN" in missing.error


def test_odds_chess_is_normal_chess_from_a_position():
    odds = api_game(9, rules="oddschess")
    odds["pgn"] = odds["pgn"].replace('[Link', '[SetUp "1"]\n[FEN "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNB1KBNR w KQkq - 0 1"]\n[Link')
    [game] = chesscom.games_in_archive({"games": [odds]})
    assert isinstance(game, CanonicalGame) and game.initial_fen.startswith("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNB1KBNR")


def test_month_of():
    assert chesscom.month_of(f"{BASE}/2023/11") == "2023/11"


def test_import_creates_account_and_stores_games(conn):
    imp = db.get_import(conn, import_chesscom(conn, "Alice", fetch=fake_api(), now=NOW))
    assert (imp["status"], imp["source_ref"]) == ("completed", "alice")
    assert (imp["games_seen"], imp["games_imported"], imp["games_failed"], imp["games_skipped"]) == (5, 4, 0, 1)
    assert imp["errors"] == []  # an unsupported variant is skipped on purpose, not an error
    assert imp["resume_state"] == {"months_done": ["2024/01", "2024/02"]}  # current month never marked done
    account = conn.execute("SELECT * FROM chess_accounts").fetchone()
    assert (account["platform"], account["username"], account["id"]) == ("chesscom", "alice", imp["account_id"])
    assert {g["source"] for g in db.list_games(conn)} == {"chesscom"}
    assert count(conn, "moves") == 16


def test_second_run_only_fetches_current_month(conn):
    import_chesscom(conn, "alice", fetch=fake_api(), now=NOW)
    api = fake_api()
    api.responses[f"{BASE}/2024/03"] = {"games": [api_game(5), api_game(6)]}  # a new game this month
    imp = db.get_import(conn, import_chesscom(conn, "alice", fetch=api, now=NOW))
    assert api.calls == [ARCHIVES, f"{BASE}/2024/03"]
    assert (imp["games_imported"], imp["games_duplicate"]) == (1, 1)
    assert imp["resume_state"] == {"months_done": ["2024/01", "2024/02"]}
    assert count(conn, "games") == 5


def test_month_rolls_over(conn):
    import_chesscom(conn, "alice", fetch=fake_api(), now=NOW)
    imp = db.get_import(conn, import_chesscom(conn, "alice", fetch=fake_api(), now=datetime(2024, 4, 2, tzinfo=UTC)))
    assert imp["resume_state"] == {"months_done": ["2024/01", "2024/02", "2024/03"]}


def test_failed_month_is_retried_next_run(conn):
    api = fake_api()
    api.responses[f"{BASE}/2024/01"] = urllib.error.URLError("connection reset")
    imp = db.get_import(conn, import_chesscom(conn, "alice", fetch=api, now=NOW))
    assert imp["status"] == "completed" and imp["games_imported"] == 2
    assert imp["errors"][0]["month"] == "2024/01" and "connection reset" in imp["errors"][0]["error"]
    assert imp["resume_state"] == {"months_done": ["2024/02"]}

    api = fake_api()
    imp = db.get_import(conn, import_chesscom(conn, "alice", fetch=api, now=NOW))
    assert api.calls == [ARCHIVES, f"{BASE}/2024/01", f"{BASE}/2024/03"]
    assert imp["games_imported"] == 2 and count(conn, "games") == 4


def test_unknown_user_fails_the_import(conn):
    not_found = urllib.error.HTTPError(ARCHIVES, 404, "Not Found", {}, io.BytesIO())  # type: ignore[arg-type]
    with pytest.raises(urllib.error.HTTPError):
        import_chesscom(conn, "alice", fetch=FakeApi({ARCHIVES: not_found}), now=NOW)
    [imp] = db.list_imports(conn)
    assert imp["status"] == "failed"


def test_pgn_export_and_api_import_dedupe(conn):
    import_pgn(conn, api_game(1)["pgn"], "export.pgn")
    imp = db.get_import(conn, import_chesscom(conn, "alice", fetch=fake_api(), now=NOW))
    assert imp["games_duplicate"] == 1 and count(conn, "games") == 4


def test_fetch_json_retries_rate_limit(monkeypatch):
    responses = [urllib.error.HTTPError("u", 429, "Too Many", {}, io.BytesIO()), io.BytesIO(b'{"ok": 1}')]  # type: ignore[arg-type]

    def urlopen(request, timeout):
        assert request.get_header("User-agent") == chesscom.USER_AGENT
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(chesscom.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(chesscom.time, "sleep", lambda s: None)
    assert chesscom.fetch_json("https://x.test/") == {"ok": 1}


def test_fetch_json_does_not_retry_404(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append(1)
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO())  # type: ignore[arg-type]

    monkeypatch.setattr(chesscom.urllib.request, "urlopen", urlopen)
    with pytest.raises(urllib.error.HTTPError):
        chesscom.fetch_json("https://x.test/")
    assert len(calls) == 1
