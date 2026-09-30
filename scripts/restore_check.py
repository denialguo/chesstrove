"""Prove a backup restores: load a pg_dump file into a throwaway local Postgres, print row counts and the moves
layout, then delete it. Usage: uv run scripts/restore_check.py backup.dump
Needs a pg_restore at least as new as the dump's pg_dump (brew install libpq). Restoring a Postgres 17 dump into
this Postgres 16 prints two harmless errors (transaction_timeout, schema "public" already exists)."""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pgserver

from chesstrove import compact, db

PG_RESTORE = shutil.which("pg_restore") or "/opt/homebrew/opt/libpq/bin/pg_restore"
data = Path(tempfile.mkdtemp(prefix="chesstrove-restore-"))
server = pgserver.get_server(data, cleanup_mode="delete")
try:
    r = subprocess.run([PG_RESTORE, "--no-owner", "--no-privileges", "-d", server.get_uri(), sys.argv[1]],
                       capture_output=True, text=True)
    print("pg_restore:", [line for line in r.stderr.splitlines() if "error" in line.lower()] or "no errors")
    with db.connect(server.get_uri()) as c:
        tables = [t["relname"] for t in c.execute("SELECT relname FROM pg_stat_user_tables ORDER BY relname").fetchall()]
        print({t: c.execute(f'SELECT count(*) AS n FROM "{t}"').fetchone()["n"] for t in tables})
        report = compact.check(c)
        print(json.dumps({k: report[k] for k in ("state", "games", "plies", "mb")}, indent=1))
finally:
    server.cleanup()
