"""The web app: a REST API under /api (the same functions the CLI uses) and the site itself at /.
Run: `chesstrove serve` (localhost). With CHESSTROVE_PUBLIC=1 (the hosted site) the admin endpoints are
off and account imports are deduplicated, rate-limited per IP and run a few at a time.

Long jobs (imports, reanalysis) return 202 with the new row's id and run in the background;
poll GET /api/imports/{id} or GET /api/analysis-runs/{id}.
"""

import logging
import os
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from importlib.resources import files

from fastapi import APIRouter, BackgroundTasks, Body, Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field

from chesstrove import archaeology, db, detectors, insights, labels
from chesstrove.analysis import reanalyze
from chesstrove.ingest import import_chesscom, import_lichess, import_pgn

log = logging.getLogger(__name__)
PUBLIC = os.environ.get("CHESSTROVE_PUBLIC") == "1"
IMPORT_SLOTS = threading.BoundedSemaphore(int(os.environ.get("CHESSTROVE_IMPORT_SLOTS", "2")))  # the rest queue
IMPORTS_PER_IP_HOUR = 10
FRESH = timedelta(minutes=10)  # a finished import this recent is returned instead of starting another


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if PUBLIC:  # imports run in this process; any still "running" were cut off by a restart
        with db.connect() as c:
            db.fail_running_imports(c, "interrupted by a server restart; visit the page again to resume")
    yield


app = FastAPI(title="ChessTrove", description="Search every motif in your chess history.",
              docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)
api = APIRouter(prefix="/api")


def local_only() -> None:
    if PUBLIC:
        raise HTTPException(403, "not available on the public site")


LocalOnly = [Depends(local_only)]

Limit = Annotated[int, Query(ge=1, le=1000)]
Offset = Annotated[int, Query(ge=0)]
Platform = Literal["chesscom", "lichess"]
Username = Annotated[str, Path(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")]


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

class AccountImport(BaseModel):
    username: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")
    user: str = Field("me", min_length=1, max_length=100)


@api.post("/imports/pgn", status_code=202, dependencies=LocalOnly)
async def create_pgn_import(request: Request, background: BackgroundTasks, c: Conn,
                            name: Annotated[str, Query(max_length=200)] = "upload.pgn") -> dict:
    """Body: raw PGN text (one or many games)."""
    text = (await request.body()).decode("utf-8-sig", errors="replace")
    if not text.strip():
        raise HTTPException(422, "empty PGN body")
    import_id = db.start_import(c, "pgn", name)  # one quick INSERT; fine to block the loop for it
    background.add_task(_in_new_connection, import_pgn, text, name, import_id=import_id)
    return {"import_id": import_id, "status": "running"}


@api.post("/imports/chesscom", status_code=202)
def create_chesscom_import(body: AccountImport, request: Request, background: BackgroundTasks, c: Conn) -> dict:
    return _start_account_import(c, request, background, "chesscom", import_chesscom, body)


@api.post("/imports/lichess", status_code=202)
def create_lichess_import(body: AccountImport, request: Request, background: BackgroundTasks, c: Conn) -> dict:
    return _start_account_import(c, request, background, "lichess", import_lichess, body)


_recent_imports: dict[str, deque[float]] = defaultdict(deque)
_recent_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")  # the host's proxy puts the visitor first
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "?")


def _allow(ip: str) -> bool:
    """ponytail: in-memory sliding window, per process; fine for one instance, move to the DB if it scales out."""
    now = time.monotonic()
    with _recent_lock:
        q = _recent_imports[ip]
        while q and q[0] < now - 3600:
            q.popleft()
        if len(q) >= IMPORTS_PER_IP_HOUR:
            return False
        q.append(now)
        return True


def _start_account_import(c, request: Request, background: BackgroundTasks, source: str, job, body: AccountImport) -> dict:
    username = body.username.lower()
    latest = db.latest_import(c, source, username)
    if latest and (latest["status"] == "running" or latest["status"] == "completed"
                   and latest["finished_at"] > datetime.now(UTC) - FRESH):
        return {"import_id": latest["id"], "status": latest["status"]}  # one import per account at a time
    if PUBLIC and not _allow(_client_ip(request)):
        raise HTTPException(429, "too many imports from here; try again in an hour")
    import_id = db.start_import(c, source, username)
    background.add_task(_queued, job, username, "me" if PUBLIC else body.user, import_id=import_id)
    return {"import_id": import_id, "status": "running"}


def _queued(job, *args, **kwargs) -> None:
    with IMPORT_SLOTS:  # waits while the others finish; the row already says "running"
        _in_new_connection(job, *args, **kwargs)


@api.get("/imports", dependencies=LocalOnly)
def list_imports(c: Conn) -> list[dict]:
    return db.list_imports(c)


@api.get("/imports/{import_id}")
def get_import(import_id: int, c: Conn) -> dict:
    return _found(db.get_import(c, import_id), "import")


# --- games -----------------------------------------------------------------------------------------

@api.get("/players/{platform}/{username}")
def player(platform: Platform, username: Username, c: Conn) -> dict:
    """A player page's data: record, rating, motif counts (theirs vs. against them), engine coverage, and
    the latest import. `games: 0` with no import means "not imported yet"."""
    return db.player_summary(c, platform, username)


@api.get("/games")
def list_games(c: Conn, player: str | None = None, platform: Platform | None = None,
               limit: Limit = 50, offset: Offset = 0) -> list[dict]:
    return db.list_games(c, player, limit, offset, platform)


@api.get("/games/{game_id}")
def get_game(game_id: int, c: Conn, engine: bool = False) -> dict:
    """With `engine=true`: `engine_positions`, the evaluation of every position (default config)."""
    game = _found(db.get_game(c, game_id), "game")
    game["events"] = db.list_events(c, game_id=game_id, limit=10_000)
    if engine and (config := db.default_engine_config(c)):
        game["engine_positions"] = db.game_engine_positions(c, game_id, config["id"])
    return game


# --- events & detectors ----------------------------------------------------------------------------

@api.get("/events")
def list_events(
    c: Conn,
    type: str | None = None,
    color: Literal["w", "b"] | None = None,
    player: str | None = None,
    since: date | None = None,
    until: date | None = None,
    game_id: int | None = None,
    platform: Platform | None = None,
    against: str | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
    engine: bool = False,
    engine_config: int | None = None,
) -> list[dict]:
    """`player` = moves played by that username (matched to the event's color); `against` = moves played
    by that username's opponents. Dates are inclusive.
    `engine=true` attaches Stockfish analysis (default config: the one covering the most games)."""
    found = db.list_events(c, type, color, player, since, until, game_id, limit, offset, platform, against)
    return insights.annotate(c, found, engine_config) if engine or engine_config else found


@api.get("/detectors")
def list_detectors() -> list[dict]:
    return [{"id": d.id, "version": d.version, "definition": (d.__doc__ or "").strip()} for d in detectors.DETECTORS]


class ReanalyzeRequest(BaseModel):
    detectors: list[str] | None = None
    all: bool = False  # redo every game, not just stale ones


@api.post("/analysis-runs", status_code=202, dependencies=LocalOnly)
def create_analysis_run(background: BackgroundTasks, c: Conn, body: Annotated[ReanalyzeRequest, Body()] = ReanalyzeRequest()) -> dict:
    try:
        chosen = detectors.select(body.detectors)  # reject unknown ids now, not in the background
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    run_id = db.start_analysis_run(c, {d.id: d.version for d in chosen})
    background.add_task(_in_new_connection, reanalyze, body.detectors, force=body.all, run_id=run_id)
    return {"run_id": run_id, "status": "running"}


@api.get("/analysis-runs")
def list_analysis_runs(c: Conn, limit: Limit = 50) -> list[dict]:
    return db.list_analysis_runs(c, limit)


@api.get("/analysis-runs/{run_id}")
def get_analysis_run(run_id: int, c: Conn) -> dict:
    return _found(db.get_analysis_run(c, run_id), "analysis run")


# --- status & engine (Layer 2) ---------------------------------------------------------------------

@api.get("/status")
def status(c: Conn) -> dict:
    """Games and positions indexed, deterministic coverage, and progress per engine config."""
    return db.status_summary(c, {d.id: d.version for d in detectors.DETECTORS})


@api.get("/engine-labels")
def engine_labels(
    c: Conn,
    type: Literal["BLUNDER", "MISSED_WIN", "ONLY_WINNING_MOVE"],
    player: str | None = None,
    platform: Platform | None = None,
    config: int | None = None,
    blunder: Annotated[float, Query(gt=0, le=1)] = labels.Thresholds().blunder,
    winning: Annotated[float, Query(gt=0, le=1)] = labels.Thresholds().winning,
    not_winning: Annotated[float, Query(ge=0, lt=1)] = labels.Thresholds().not_winning,
    scale: Literal["lichess", "stockfish"] = "lichess",
    include_recaptures: bool = False,
    limit: Limit = 50,
) -> list[dict]:
    """Derived from stored evaluations at query time: change a threshold or scale and everything relabels
    instantly."""
    return labels.query(c, type, labels.Thresholds(blunder, winning, not_winning), config, player, limit,
                        scale, include_recaptures, platform)


Share = Annotated[float, Query(ge=0, le=1)]
_P = archaeology.Params()


@api.get("/engine-discoveries")
def engine_discoveries(
    c: Conn,
    type: archaeology.Type,
    player: Annotated[str, Query(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")],
    platform: Platform | None = None,
    config: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    scale: Literal["lichess", "stockfish"] = _P.scale,
    winning: Share = _P.winning,
    not_winning: Share = _P.not_winning,
    sacrifice_engine_choice: bool = _P.sacrifice_engine_choice,
    sacrifice_tolerance: Share = _P.sacrifice_tolerance,
    sacrifice_floor: Share = _P.sacrifice_floor,
    unusual_min_gap: Share = _P.unusual_min_gap,
    weight_quiet: Annotated[float, Query(ge=0, le=10)] = _P.weight_quiet,
    weight_retreat: Annotated[float, Query(ge=0, le=10)] = _P.weight_retreat,
    weight_sacrifice: Annotated[float, Query(ge=0, le=10)] = _P.weight_sacrifice,
    weight_underpromotion: Annotated[float, Query(ge=0, le=10)] = _P.weight_underpromotion,
) -> dict:
    """Engine archaeology: one player's rarest engine-backed moments of one kind, ranked, each with the
    evidence behind it (game, ply, positions, evaluations from the player's side, the engine's choice,
    the engine config, and the ranked quantity). Derived from stored results at query time: every
    threshold is a parameter, nothing re-runs Stockfish. Types and exact definitions: archaeology.py."""
    params = archaeology.Params(
        scale=scale, winning=winning, not_winning=not_winning, sacrifice_engine_choice=sacrifice_engine_choice,
        sacrifice_tolerance=sacrifice_tolerance, sacrifice_floor=sacrifice_floor, unusual_min_gap=unusual_min_gap,
        weight_quiet=weight_quiet, weight_retreat=weight_retreat, weight_sacrifice=weight_sacrifice,
        weight_underpromotion=weight_underpromotion)
    return archaeology.discoveries(c, type, player, platform, config, limit, params)


@api.get("/engine-runs")
def list_engine_runs(c: Conn, limit: Limit = 50) -> list[dict]:
    """Engine runs are started from the CLI (`chesstrove engine analyze`): they're long local batch jobs."""
    return db.list_engine_runs(c, limit)


def _in_new_connection(job, *args, **kwargs) -> None:
    """Background jobs outlive the request, so they get their own connection. A failure is recorded on
    the job's row (status=failed, plus the reason) by the job itself, so here it's only logged."""
    try:
        with db.connect() as c:
            job(c, *args, **kwargs)
    except Exception:
        log.exception("background job %s failed", job.__name__)


class SiteFiles(StaticFiles):
    """The built React app (web/ -> src/chesstrove/web). Unknown paths get index.html, so client-side
    routes like /u/chesscom/name load the app; /api/* never falls through to it."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as e:  # what StaticFiles raises (FastAPI's is a subclass)
            if e.status_code != 404 or path.startswith("api/"):
                raise
            return await super().get_response("index.html", scope)


app.include_router(api)
app.mount("/", SiteFiles(directory=str(files("chesstrove") / "web"), html=True), name="web")  # last: /api wins
