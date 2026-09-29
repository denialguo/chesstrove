import io
import urllib.error
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from chesstrove import api, ingest
from test_analysis import PGN
from test_chesscom import ARCHIVES, fake_api


@pytest.fixture
def client(dsn, conn, monkeypatch):
    monkeypatch.setenv("CHESSTROVE_DATABASE_URL", dsn)
    return TestClient(api.app)  # runs background tasks right after each response


def test_pgn_import_then_query(client):
    r = client.post("/imports/pgn?name=mine.pgn", content=PGN)
    assert r.status_code == 202
    imp = client.get(f"/imports/{r.json()['import_id']}").json()
    assert (imp["status"], imp["source_ref"], imp["games_imported"]) == ("completed", "mine.pgn", 2)

    games = client.get("/games", params={"player": "alice"}).json()
    assert len(games) == 2
    game = client.get(f"/games/{games[0]['id']}").json()
    assert game["moves"][0]["san"] == "a8=N+"
    assert [e["type"] for e in game["events"]] == ["UNDERPROMOTION"]

    events = client.get("/events", params={"type": "MISSED_MATE_IN_ONE", "player": "alice"}).json()
    assert [e["metadata"]["mating_moves"] for e in events] == [["Ra1#"]]
    assert client.get("/events", params={"color": "w", "since": "2024-01-01", "until": "2024-12-31"}).json()[0]["type"] == "UNDERPROMOTION"


def test_validation_and_not_found(client):
    assert client.get("/games/999").status_code == 404
    assert client.get("/imports/999").status_code == 404
    assert client.get("/events", params={"color": "white"}).status_code == 422
    assert client.get("/events", params={"since": "yesterday"}).status_code == 422
    assert client.get("/games", params={"limit": 100_000}).status_code == 422
    assert client.post("/imports/pgn", content="  ").status_code == 422
    assert client.post("/imports/chesscom", json={"username": "../etc"}).status_code == 422
    assert client.post("/analysis-runs", json={"detectors": ["NOPE"]}).status_code == 422


def test_chesscom_import(client, monkeypatch):
    now = datetime(2024, 3, 15, tzinfo=UTC)
    monkeypatch.setattr(api, "import_chesscom",
                        lambda c, username, user, import_id: ingest.import_chesscom(c, username, user, fake_api(), now, import_id))
    r = client.post("/imports/chesscom", json={"username": "Alice"})
    imp = client.get(f"/imports/{r.json()['import_id']}").json()
    assert (imp["status"], imp["source_ref"], imp["games_imported"]) == ("completed", "alice", 4)
    assert imp["account_id"] is not None

    # a second import through the API resumes from the first one's months
    api_calls = fake_api()
    monkeypatch.setattr(api, "import_chesscom",
                        lambda c, username, user, import_id: ingest.import_chesscom(c, username, user, api_calls, now, import_id))
    client.post("/imports/chesscom", json={"username": "alice"})
    assert len(api_calls.calls) == 2  # archive list + current month only


def test_failed_background_import_reports_why(client, monkeypatch):
    not_found = urllib.error.HTTPError(ARCHIVES, 404, "Not Found", {}, io.BytesIO())  # type: ignore[arg-type]

    def fetch(url):
        raise not_found

    monkeypatch.setattr(api, "import_chesscom",
                        lambda c, username, user, import_id: ingest.import_chesscom(c, username, user, fetch, None, import_id))
    import_id = client.post("/imports/chesscom", json={"username": "alice"}).json()["import_id"]
    imp = client.get(f"/imports/{import_id}").json()
    assert imp["status"] == "failed" and "404" in imp["errors"][-1]["error"]


def test_reanalyze(client):
    client.post("/imports/pgn", content=PGN)
    r = client.post("/analysis-runs", json={"detectors": ["UNDERPROMOTION"], "all": True})
    assert r.status_code == 202
    run = client.get(f"/analysis-runs/{r.json()['run_id']}").json()
    assert (run["status"], run["games_processed"], run["detector_versions"]) == ("completed", 2, {"UNDERPROMOTION": 1})
    nothing_stale = client.post("/analysis-runs").json()["run_id"]  # empty body = all detectors, stale only
    assert client.get(f"/analysis-runs/{nothing_stale}").json()["games_processed"] == 0
    assert len(client.get("/analysis-runs").json()) == 3  # import's run + two reanalyses


def test_detectors(client):
    ids = [d["id"] for d in client.get("/detectors").json()]
    assert "MISSED_MATE_IN_ONE" in ids and len(ids) == 8
