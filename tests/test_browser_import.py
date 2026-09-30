"""Browser indexing: batches the browser computed (chesstrove.indexing) are checked and stored exactly as the
server importer would store the same games."""

import copy
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from chesstrove import api, browser_import, db, indexing
from chesstrove.ingest import import_chesscom

NOW = datetime.now(UTC)
PAST, CURRENT = "2024/01", NOW.strftime("%Y/%m")


def pgn(game_id: int, moves: str, fen: str | None = None, chess960: bool = False, white="alice", black="bob") -> dict:
    tags = [f'[White "{white}"]', f'[Black "{black}"]', '[Result "*"]', '[Date "2024.01.05"]',
            f'[Link "https://www.chess.com/game/live/{game_id}"]']
    if fen:
        tags += ['[SetUp "1"]', f'[FEN "{fen}"]']
    if chess960:
        tags.append('[Variant "Chess960"]')
    text = "\n".join(tags) + f"\n\n{moves} *\n"
    return {"url": f"https://www.chess.com/game/live/{game_id}", "pgn": text, "rules": "chess960" if chess960 else "chess",
            "rated": True, "eco": "https://www.chess.com/openings/Italian-Game"}


ARCHIVE = {"games": [
    pgn(1, "1. e4 e5 2. Nf3 Nc6"),
    pgn(2, "1. e4 d5 2. e5 f5 3. exf6 Nc6 4. fxg7 Bf5 5. gxh8=Q Qd7 6. Nf3 O-O-O 7. Bc4 e6 8. O-O Qg7 9. Qxg8"),
    pgn(3, "1. Nf7#", fen="6rk/6pp/8/6N1/8/8/8/6K1 w - - 0 1"),                    # smothered mate
    pgn(4, "1. a8=N", fen="8/P6k/8/8/8/8/8/K7 w - - 0 1", white="bob", black="alice"),  # underpromotion, opponent's
    pgn(5, "1. g3 g6 2. Qg2 Qg7 3. O-O O-O", fen="bnrbkrqn/pppppppp/8/8/8/8/PPPPPPPP/BNRBKRQN w KQkq - 0 1", chess960=True),
    {"url": "https://www.chess.com/game/live/6", "rules": "bughouse"},              # skipped, like the server does
]}


def indexed() -> dict:
    return json.loads(indexing.index_chesscom_archive(json.dumps(ARCHIVE)))


@pytest.fixture
def client(dsn, conn, monkeypatch):
    monkeypatch.setenv("CHESSTROVE_DATABASE_URL", dsn)
    monkeypatch.setattr(api, "profile", lambda source, username: {"games": 5, "rating": 1500, "rating_mode": "blitz"})
    return TestClient(api.app)


def session(client) -> tuple[int, dict]:
    r = client.post("/api/indexing/chesscom", json={"username": "Alice"}).json()
    assert r["mode"] == "index" and r["versions"] == indexing.versions(indexing.FAST)
    return r["import_id"], {"X-Import-Token": r["token"]}


def batch(games, month=PAST, complete=True, **extra) -> dict:
    return {"versions": indexing.versions(indexing.FAST), "month": month, "month_complete": complete, "games": games, **extra}


def snapshot(conn) -> dict:
    """Every stored fact, keyed by source key rather than database ids."""
    key = "(SELECT source_key FROM games g WHERE g.id = t.game_id)"
    return {
        "games": conn.execute(f"SELECT {', '.join(c for c in db.GAME_COLUMNS.split(', ') if c != 'import_id')} "
                              "FROM games ORDER BY source_key").fetchall(),
        "moves": conn.execute(f"SELECT {key} AS k, t.* FROM game_moves t ORDER BY k").fetchall(),
        "events": conn.execute(f"SELECT {key} AS k, ply, type, detector_id, detector_version, color, fen, metadata "
                               "FROM events t ORDER BY k, ply, type").fetchall(),
        "analysis": conn.execute(f"SELECT {key} AS k, detector_versions FROM game_analysis t ORDER BY k").fetchall(),
    }


def strip_ids(s: dict) -> dict:
    return {**s, "moves": [{k: v for k, v in r.items() if k != "game_id"} for r in s["moves"]]}


def test_browser_batches_store_exactly_what_the_server_importer_stores(client, conn):
    base = "https://api.chess.com/pub/player/alice/games"
    fetch = {f"{base}/archives": {"archives": [f"{base}/2024/01"]}, f"{base}/2024/01": ARCHIVE}.__getitem__
    import_chesscom(conn, "alice", fetch=fetch, now=NOW)
    server = strip_ids(snapshot(conn))
    assert len(server["games"]) == 5 and {e["type"] for e in server["events"]} >= {"SMOTHERED_MATE", "UNDERPROMOTION"}
    conn.execute("TRUNCATE games, imports, analysis_runs RESTART IDENTITY CASCADE")

    import_id, auth = session(client)
    out = indexed()
    r = client.post(f"/api/indexing/{import_id}/batches", headers=auth,
                    json=batch(out["games"], skipped=out["skipped"], errors=out["errors"]))
    assert r.status_code == 200, r.text
    fast = [e for e in server["events"] if e["detector_id"] != "MISSED_MATE_IN_ONE"]
    assert r.json() == {"stored": 5, "duplicate": 0, "events": len(fast)}
    assert client.post(f"/api/indexing/{import_id}/finish", headers=auth).status_code == 200
    imp = db.get_import(conn, import_id)
    assert (imp["status"], imp["games_imported"], imp["games_skipped"]) == ("completed", 5, 1)
    assert imp["resume_state"]["months_done"] == [PAST] and imp["games_expected"] == 5
    first = strip_ids(snapshot(conn))
    assert first["events"] == fast  # the first pass claims only what it ran:
    assert all("MISSED_MATE_IN_ONE" not in a["detector_versions"] for a in first["analysis"])
    summary = client.get("/api/players/chesscom/alice").json()  # the page doesn't care who indexed it
    assert summary["games"] == 5 and summary["rare_moments"]["mine"] >= 1 and summary["deep_pending"] == 5

    # the deep pass, later: then everything equals what the server importer stored
    deep = run_deep(client, "alice")
    assert deep["games"] == 5 and strip_ids(snapshot(conn)) == server
    assert client.get("/api/players/chesscom/alice").json()["deep_pending"] == 0
    assert client.post("/api/indexing/chesscom/deep", json={"username": "alice"}).json() == {"mode": "done", "pending": 0}


def run_deep(client, username: str) -> dict:
    """What the browser's deep pass does: take pending games, run indexing.deep_scan, send the events back."""
    r = client.post("/api/indexing/chesscom/deep", json={"username": username}).json()
    assert r["mode"] == "index" and r["versions"] == indexing.versions(indexing.DEEP)
    auth, run = {"X-Import-Token": r["token"]}, r["run_id"]
    total = {"games": 0, "events": 0}
    while games := client.get(f"/api/indexing/deep/{run}/games", headers=auth).json():
        found = json.loads(indexing.deep_scan(json.dumps(games)))
        ack = client.post(f"/api/indexing/deep/{run}/batches", headers=auth,
                          json={"versions": r["versions"], "games": found}).json()
        total = {k: total[k] + ack[k] for k in total}
    assert client.post(f"/api/indexing/deep/{run}/finish", headers=auth).status_code == 200
    return total


def test_repeated_and_overlapping_batches_are_harmless(client, conn):
    import_id, auth = session(client)
    games = indexed()["games"]
    post = lambda gs, **kw: client.post(f"/api/indexing/{import_id}/batches", headers=auth, json=batch(gs, **kw))
    assert post(games[:3], complete=False).json()["stored"] == 3
    assert post(games[:3], complete=False).json() == {"stored": 0, "duplicate": 3, "events": 0}  # a retried upload
    assert post(games[1:]).json()["stored"] == 2  # overlapping: the new ones only
    assert conn.execute("SELECT count(*) AS n FROM games").fetchone()["n"] == 5
    assert conn.execute("SELECT count(*) AS n FROM game_moves").fetchone()["n"] == 5


def test_months_resume_across_sessions_and_the_current_month_is_never_done(client, conn, monkeypatch):
    import_id, auth = session(client)
    games = indexed()["games"]
    client.post(f"/api/indexing/{import_id}/batches", headers=auth, json=batch(games[:2], month=PAST))
    client.post(f"/api/indexing/{import_id}/batches", headers=auth, json=batch(games[2:], month=CURRENT))
    assert db.get_import(conn, import_id)["resume_state"]["months_done"] == [PAST]
    # another tab while this one is active: it watches instead of indexing twice
    assert client.post("/api/indexing/chesscom", json={"username": "alice"}).json() == {"mode": "watch", "import_id": import_id}
    # the tab closes; later, a new session takes over and skips the finished month
    monkeypatch.setattr(browser_import, "IDLE", timedelta(0))
    r = client.post("/api/indexing/chesscom", json={"username": "alice"}).json()
    assert r["mode"] == "index" and r["import_id"] != import_id and r["months_done"] == [PAST]
    assert db.get_import(conn, import_id)["status"] == "failed"
    # the abandoned session can't write any more
    old = client.post(f"/api/indexing/{import_id}/batches", headers=auth, json=batch([], month=PAST))
    assert old.status_code == 409


@pytest.mark.parametrize("break_it, status", [
    (lambda g: g["moves"]["flags"].pop(), 422),                                   # arrays shorter than ply_count
    (lambda g: g["moves"].update(uci=g["moves"]["uci"] + " e2e4"), 422),          # one move too many
    (lambda g: g["moves"].update(piece="X" * g["ply_count"]), 422),
    (lambda g: g.update(ply_count=g["ply_count"] + 1), 422),
    (lambda g: g.update(white="mallory", black="eve"), 422),                      # not this player's game
    (lambda g: g.update(source_key="lichess:abc"), 422),
    (lambda g: g.update(extra=1), 422),
    (lambda g: g["events"].append({**g["events"][0], "ply": g["ply_count"] + 1}), 422),  # past the last ply
    (lambda g: g["events"][0].update(color="b" if g["events"][0]["color"] == "w" else "w"), 422),
    (lambda g: g["events"][0].update(detector_id="MADE_UP", type="MADE_UP"), 422),
    (lambda g: g["events"][0].update(detector_version=99), 422),
    (lambda g: g["events"][0].update(metadata={"x": "y" * 5000}), 422),
])
def test_malformed_games_are_refused_and_nothing_is_stored(client, conn, break_it, status):
    import_id, auth = session(client)
    games = copy.deepcopy(indexed()["games"])
    break_it(next(g for g in games if g["events"]))
    r = client.post(f"/api/indexing/{import_id}/batches", headers=auth, json=batch(games))
    assert r.status_code == status, r.text
    assert conn.execute("SELECT count(*) AS n FROM games").fetchone()["n"] == 0


def test_a_game_that_doesnt_match_its_pgn_is_caught(client, conn):
    """Well-formed but invented: a smothered mate the PGN doesn't contain. Event games are always sampled."""
    import_id, auth = session(client)
    games = copy.deepcopy(indexed()["games"])
    plain = games[0]
    plain["events"] = [{**next(g for g in games if g["events"])["events"][0], "ply": 1, "color": "w"}]
    r = client.post(f"/api/indexing/{import_id}/batches", headers=auth, json=batch(games))
    assert r.status_code == 422 and "PGN" in r.text
    assert conn.execute("SELECT count(*) AS n FROM games").fetchone()["n"] == 0


def test_sessions_versions_and_limits(client, conn, monkeypatch):
    import_id, auth = session(client)
    games = indexed()["games"]
    url = f"/api/indexing/{import_id}/batches"
    assert client.post(url, headers={"X-Import-Token": "guess"}, json=batch(games)).status_code == 403
    assert client.post(url, json=batch(games)).status_code == 422  # no token at all
    stale = {**batch(games), "versions": {**indexing.versions(indexing.FAST), "SMOTHERED_MATE": 0}}
    assert client.post(url, headers=auth, json=stale).status_code == 409
    monkeypatch.setattr(browser_import, "MAX_GAMES", 2)
    assert client.post(url, headers=auth, json=batch(games)).status_code == 422
    twice = batch([games[0], games[0]])
    assert client.post(url, headers=auth, json=twice).status_code == 422
    assert client.post(url, headers={**auth, "Content-Length": str(10**8)}, content=b"{}").status_code == 413
    assert conn.execute("SELECT count(*) AS n FROM games").fetchone()["n"] == 0


def test_the_browser_core_needs_no_server_code():
    """What the browser loads (indexing and its imports) must not pull in the database, web or network code."""
    code = ("import sys, json, chesstrove.indexing; "
            "print(json.dumps(sorted(m for m in sys.modules if m.startswith(('chesstrove', 'psycopg', 'fastapi', 'pgserver')))))")
    loaded = json.loads(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout)
    assert not [m for m in loaded if not m.startswith("chesstrove")]
    assert set(loaded) <= {"chesstrove", "chesstrove.indexing", "chesstrove.models", "chesstrove.reconstruction",
                           "chesstrove.importers", "chesstrove.importers.pgn", "chesstrove.importers.chesscom",
                           "chesstrove.detectors"} | {m for m in loaded if m.startswith("chesstrove.detectors.")}


def test_the_same_browser_resumes_its_own_session(client, conn):
    import_id, auth = session(client)
    games = indexed()["games"]
    client.post(f"/api/indexing/{import_id}/batches", headers=auth, json=batch(games[:2], month=PAST))
    # paused and resumed within a minute: anyone else would be told to watch; the token holder carries on
    r = client.post("/api/indexing/chesscom", json={"username": "alice", "import_id": import_id, "token": auth["X-Import-Token"]}).json()
    assert (r["mode"], r["import_id"], r["months_done"]) == ("index", import_id, [PAST])
    wrong = client.post("/api/indexing/chesscom", json={"username": "alice", "import_id": import_id, "token": "nope"}).json()
    assert wrong["mode"] == "watch"


def test_the_token_hash_is_never_shown(client):
    import_id, _ = session(client)
    for body in (client.get(f"/api/imports/{import_id}").json(), client.get("/api/players/chesscom/alice").json()["latest_import"]):
        assert body["resume_state"]["client"] == "browser" and "token_sha256" not in body["resume_state"]


WEB = __import__("pathlib").Path(__file__).parent.parent / "web"


def mate_positions() -> list[dict]:
    """Every named-mate position the detector tests use (textbook pictures and each form), as Chess.com games."""
    import chess
    from detectors.test_named_mates import CANON, FORMS
    cases = [(fen, uci) for fen, uci in CANON.values()] + [(fen, uci) for _, fen, uci, _ in FORMS]
    out = []
    for i, (fen, uci) in enumerate(cases, start=100):
        board = chess.Board(fen)
        san = board.san(chess.Move.from_uci(uci))
        mover = "1." if board.turn else "1..."
        out.append(pgn(i, f"{mover} {san}", fen=fen))
    return out


@pytest.mark.skipif(not (WEB / "node_modules/pyodide").exists(), reason="needs `npm install` in web/ (Pyodide)")
def test_the_browser_runtime_produces_exactly_the_native_output():
    """The browser's analyzer is this repo's Python, run by Pyodide: prove that holds on castling, Chess960, en
    passant, promotions and underpromotions, set-up positions, mates, and every named-mate picture and form."""
    archive = json.dumps({"games": ARCHIVE["games"] + mate_positions()})
    native = indexing.index_chesscom_archive(archive)
    browser = subprocess.run(["node", "checks/pyodide-core.mjs"], cwd=WEB, input=archive, capture_output=True,
                             text=True, check=True).stdout
    assert json.loads(browser) == json.loads(native)
    games = json.loads(native)["games"]
    forms = {e["metadata"]["form"] for g in games for e in g["events"] if "form" in e["metadata"]}
    assert len(games) == 56 and forms == {"textbook", "characteristic", "variant"}


def test_deep_pass_is_checked_idempotent_and_resumable(client, conn):
    import_id, auth = session(client)
    client.post(f"/api/indexing/{import_id}/batches", headers=auth, json=batch(indexed()["games"]))
    r = client.post("/api/indexing/chesscom/deep", json={"username": "alice"}).json()
    token, run = {"X-Import-Token": r["token"]}, r["run_id"]
    games = client.get(f"/api/indexing/deep/{run}/games", headers=token).json()
    found = json.loads(indexing.deep_scan(json.dumps(games)))
    url, v = f"/api/indexing/deep/{run}/batches", indexing.versions(indexing.DEEP)
    # another browser meanwhile: told to watch; this one (with its token) resumes the same scan
    assert client.post("/api/indexing/chesscom/deep", json={"username": "alice"}).json()["mode"] == "watch"
    again = client.post("/api/indexing/chesscom/deep", json={"username": "alice", "run_id": run, "token": r["token"]}).json()
    assert (again["mode"], again["run_id"]) == ("index", run)
    # refusals: wrong token, stale versions, a first-pass detector, an event past the game, someone else's game
    assert client.post(url, headers={"X-Import-Token": "x"}, json={"versions": v, "games": found}).status_code == 403
    assert client.post(url, headers=token, json={"versions": {"MISSED_MATE_IN_ONE": 0}, "games": found}).status_code == 409
    forged = copy.deepcopy(found)
    forged[0]["events"] = [{"detector_id": "SMOTHERED_MATE", "detector_version": 2, "ply": 1, "type": "SMOTHERED_MATE",
                            "color": "w", "fen": "x", "metadata": {}}]
    assert client.post(url, headers=token, json={"versions": v, "games": forged}).status_code == 422
    assert client.post(url, headers=token, json={"versions": v, "games": [{"source_key": "chesscom:999", "events": []}]}).status_code == 422
    # the same batch twice: events replaced, not doubled
    for _ in range(2):
        assert client.post(url, headers=token, json={"versions": v, "games": found}).status_code == 200
    n = conn.execute("SELECT count(*) AS n FROM events WHERE detector_id = 'MISSED_MATE_IN_ONE'").fetchone()["n"]
    assert n == sum(len(g["events"]) for g in found)
    assert client.get(f"/api/indexing/deep/{run}/games", headers=token).json() == []


def test_every_detector_declares_its_gate_and_pass():
    from chesstrove.detectors import DETECTORS
    assert all(d.requires in indexing.GATES and d.tier in ("fast", "deep") for d in DETECTORS)
    assert [d.id for d in indexing.DEEP] == ["MISSED_MATE_IN_ONE"] and len(indexing.FAST) + 1 == len(DETECTORS)


@pytest.mark.skipif(not (WEB / "node_modules/pyodide").exists(), reason="needs `npm install` in web/ (Pyodide)")
def test_the_browser_deep_pass_produces_exactly_the_native_output():
    games = [{"source_key": g["source_key"], "initial_fen": g["initial_fen"], "chess960": g["chess960"],
              "uci": g["moves"]["uci"] if g["moves"] else ""}
             for g in json.loads(indexing.index_chesscom_archive(json.dumps({"games": ARCHIVE["games"] + mate_positions()})))["games"]]
    native = indexing.deep_scan(json.dumps(games))
    browser = subprocess.run(["node", "checks/pyodide-core.mjs", "deep"], cwd=WEB, input=json.dumps(games),
                             capture_output=True, text=True, check=True).stdout
    assert json.loads(browser) == json.loads(native) and len(json.loads(native)) == 56


def test_a_server_restart_leaves_browser_imports_running(client, conn):
    """The indexing happens in the visitor's tab: a deploy or restart mustn't end it (it did, in the alpha)."""
    import_id, auth = session(client)
    server_import = db.start_import(conn, "chesscom", "someone_else")
    with TestClient(api.app):  # startup: what a restart runs
        pass
    assert db.get_import(conn, server_import)["status"] == "failed"
    assert db.get_import(conn, import_id)["status"] == "running"
    r = client.post(f"/api/indexing/{import_id}/batches", headers=auth, json=batch(indexed()["games"][:2]))
    assert r.status_code == 200 and r.json()["stored"] == 2
