#!/bin/sh
# Copy the local ChessTrove database (embedded Postgres) into a hosted one, schema and data.
# Usage: scripts/push_db.sh 'postgresql://...'   (Supabase: the Session pooler string, port 5432)
# Replaces anything ChessTrove already stored there.
set -eu
target="$1"
bin="$(uv run python -c 'import pgserver, pathlib; print(pathlib.Path(pgserver.__file__).parent / "pginstall/bin")')"
local="$(env -u CHESSTROVE_DATABASE_URL uv run python -c 'from chesstrove import db; print(db.embedded_dsn())')"
"$bin/pg_dump" --no-owner --no-privileges --clean --if-exists --schema=public "$local" | "$bin/psql" -q -v ON_ERROR_STOP=1 "$target"
echo "copied; row check:"
"$bin/psql" -At "$target" -c "select (select count(*) from games) || ' games, ' || (select count(*) from events) || ' events'"
