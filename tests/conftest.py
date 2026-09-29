import os
import tempfile

import pytest

from chesstrove import db

TABLES = "engine_move_probes, engine_positions, engine_game_status, engine_runs, engine_configs, game_analysis, events, analysis_runs, moves, games, imports, chess_accounts, users"


@pytest.fixture(scope="session")
def dsn():
    """Real Postgres: $CHESSTROVE_TEST_DATABASE_URL, else a throwaway embedded server (pgserver)."""
    if url := os.environ.get("CHESSTROVE_TEST_DATABASE_URL"):
        yield url
        return
    import pgserver

    with tempfile.TemporaryDirectory() as data_dir:
        server = pgserver.get_server(data_dir, cleanup_mode="stop")
        yield server.get_uri()
        server.cleanup()


@pytest.fixture
def conn(dsn):
    with db.connect(dsn) as c:
        db.init_schema(c)
        c.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        yield c
