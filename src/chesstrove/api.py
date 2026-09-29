"""Minimal REST API over the same functions the CLI uses. Run: `chesstrove serve` (localhost only, no auth).

Long jobs (imports, reanalysis) return 202 with the new row's id and run in the background;
poll GET /imports/{id} or GET /analysis-runs/{id}.
"""

import logging
from collections.abc import Iterator
from datetime import date
from typing import Annotated, Literal

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from chesstrove import db, detectors
from chesstrove.analysis import reanalyze
from chesstrove.ingest import import_chesscom, import_pgn

log = logging.getLogger(__name__)
app = FastAPI(title="ChessTrove", description="Search every motif in your chess history.")

Limit = Annotated[int, Query(ge=1, le=1000)]
Offset = Annotated[int, Query(ge=0)]


def conn() -> Iterator:
    # ponytail: a connection per request (~ms on localhost); add psycopg_pool if this ever serves real traffic
    with db.connect() as c:
        yield c


Conn = Annotated[object, Depends(conn)]


def _found(row: dict | None, what: str) -> dict:
    if row is None:
        raise HTTPException(404, f"{what} not found")
    return row


# --- imports ---------------------------------------------------------------------------------------

class ChesscomImport(BaseModel):
    username: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")
    user: str = Field("me", min_length=1, max_length=100)


@app.post("/imports/pgn", status_code=202)
async def create_pgn_import(request: Request, background: BackgroundTasks, c: Conn,
                            name: Annotated[str, Query(max_length=200)] = "upload.pgn") -> dict:
    """Body: raw PGN text (one or many games)."""
    text = (await request.body()).decode("utf-8-sig", errors="replace")
    if not text.strip():
        raise HTTPException(422, "empty PGN body")
    import_id = db.start_import(c, "pgn", name)  # one quick INSERT; fine to block the loop for it
    background.add_task(_in_new_connection, import_pgn, text, name, import_id=import_id)
    return {"import_id": import_id, "status": "running"}


@app.post("/imports/chesscom", status_code=202)
def create_chesscom_import(body: ChesscomImport, background: BackgroundTasks, c: Conn) -> dict:
    username = body.username.lower()
    import_id = db.start_import(c, "chesscom", username)
    background.add_task(_in_new_connection, import_chesscom, username, body.user, import_id=import_id)
    return {"import_id": import_id, "status": "running"}


@app.get("/imports")
def list_imports(c: Conn) -> list[dict]:
    return db.list_imports(c)


@app.get("/imports/{import_id}")
def get_import(import_id: int, c: Conn) -> dict:
    return _found(db.get_import(c, import_id), "import")


# --- games -----------------------------------------------------------------------------------------

@app.get("/games")
def list_games(c: Conn, player: str | None = None, limit: Limit = 50, offset: Offset = 0) -> list[dict]:
    return db.list_games(c, player, limit, offset)


@app.get("/games/{game_id}")
def get_game(game_id: int, c: Conn) -> dict:
    game = _found(db.get_game(c, game_id), "game")
    game["events"] = db.list_events(c, game_id=game_id, limit=10_000)
    return game


# --- events & detectors ----------------------------------------------------------------------------

@app.get("/events")
def list_events(
    c: Conn,
    type: str | None = None,
    color: Literal["w", "b"] | None = None,
    player: str | None = None,
    since: date | None = None,
    until: date | None = None,
    game_id: int | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[dict]:
    """`player` = moves played by that username (matched to the event's color). Dates are inclusive."""
    return db.list_events(c, type, color, player, since, until, game_id, limit, offset)


@app.get("/detectors")
def list_detectors() -> list[dict]:
    return [{"id": d.id, "version": d.version, "definition": (d.__doc__ or "").strip()} for d in detectors.DETECTORS]


class ReanalyzeRequest(BaseModel):
    detectors: list[str] | None = None
    all: bool = False  # redo every game, not just stale ones


@app.post("/analysis-runs", status_code=202)
def create_analysis_run(background: BackgroundTasks, c: Conn, body: Annotated[ReanalyzeRequest, Body()] = ReanalyzeRequest()) -> dict:
    try:
        chosen = detectors.select(body.detectors)  # reject unknown ids now, not in the background
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    run_id = db.start_analysis_run(c, {d.id: d.version for d in chosen})
    background.add_task(_in_new_connection, reanalyze, body.detectors, force=body.all, run_id=run_id)
    return {"run_id": run_id, "status": "running"}


@app.get("/analysis-runs")
def list_analysis_runs(c: Conn, limit: Limit = 50) -> list[dict]:
    return db.list_analysis_runs(c, limit)


@app.get("/analysis-runs/{run_id}")
def get_analysis_run(run_id: int, c: Conn) -> dict:
    return _found(db.get_analysis_run(c, run_id), "analysis run")


def _in_new_connection(job, *args, **kwargs) -> None:
    """Background jobs outlive the request, so they get their own connection. A failure is recorded on
    the job's row (status=failed, plus the reason) by the job itself, so here it's only logged."""
    try:
        with db.connect() as c:
            job(c, *args, **kwargs)
    except Exception:
        log.exception("background job %s failed", job.__name__)
