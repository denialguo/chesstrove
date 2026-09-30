# ChessTrove

ChessTrove indexes your entire Chess.com/Lichess history (or any PGN) so you can uncover rare motifs, unusual
positions, engine insights, and recurring patterns across every game you've played.

Today: a deterministic index (move facts + rare-motif detectors). Planned: a persistent, resumable Stockfish
index over every position. Design and roadmap: [ARCHITECTURE.md](ARCHITECTURE.md).

```sh
uv sync
# No database setup: data goes to a built-in Postgres in ~/.chesstrove.
# To use your own server instead: export CHESSTROVE_DATABASE_URL=postgresql://...
uv run chesstrove import-pgn my_games.pgn   # re-running is safe: duplicates are skipped
uv run chesstrove import-chesscom myname   # first run fetches everything; later runs only new months
uv run chesstrove import-lichess myname    # same idea; export LICHESS_TOKEN=... to stream faster
uv run chesstrove imports
uv run chesstrove games --player myname
uv run chesstrove game 1                    # moves + events

uv run chesstrove detectors                 # what each detector finds, and its version
uv run chesstrove events --type UNDERPROMOTION
uv run chesstrove events --type MISSED_MATE_IN_ONE --player myname --since 2024-01-01
uv run chesstrove reanalyze                 # after bumping/adding a detector: redoes only what's stale
uv run chesstrove serve                     # REST API on 127.0.0.1:8000, interactive docs at /docs

# Engine layer (needs Stockfish: brew install stockfish). Resumable; Ctrl-C keeps finished games.
uv run chesstrove engine analyze --nodes 25000 --max-games 100   # newest games first
uv run chesstrove status                    # games, positions, deterministic + Stockfish progress

uv run pytest                              # spins up an embedded Postgres; no setup needed
uv run scripts/benchmark.py --db
```

## Hosting it publicly

The site runs on Render's free web service, with Supabase's free Postgres as the database:

1. **Supabase.** Create a project. Under **Connect**, copy the **Session pooler** connection string (port
   5432; Render can't reach the direct IPv6 address).
2. **Copy your local data up** (optional): `scripts/push_db.sh 'postgresql://...'`. This replaces anything
   ChessTrove already stored there. Without it, run `CHESSTROVE_DATABASE_URL=... uv run chesstrove init-db`
   once.
3. **Render.** Go to **New → Blueprint**, pick this repo (it reads `render.yaml`), and paste the connection
   string as `CHESSTROVE_DATABASE_URL`.

`CHESSTROVE_PUBLIC=1` (set by the blueprint) makes three changes:
- It turns off PGN upload, reanalysis and the import list.
- It returns an account's running or just-finished import instead of starting a duplicate.
- It limits each IP to 10 imports an hour and runs 2 Chess.com imports and 1 Lichess import at a time (`CHESSTROVE_CHESSCOM_SLOTS`, `CHESSTROVE_LICHESS_SLOTS`).

Stockfish doesn't run on the host. Players analysed locally (`chesstrove engine analyze`) before being
pushed up show that record book. Everyone else gets an offer to analyse their games in their own browser
(Stockfish 18 WASM, results kept in the browser); see ARCHITECTURE.md, "Browser engine". Benchmark any
machine at `/lab/engine`.

Free-tier limits:
- Render sleeps after 15 idle minutes, so the first visit after that takes about a minute to wake it.
- Supabase pauses a project after a week without traffic, and its database is capped at 500 MB, roughly
  25k more games beyond the current data.
