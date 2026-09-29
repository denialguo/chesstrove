# ChessTrove

Indexes your whole chess history (PGN files and Chess.com now; Lichess next) into Postgres so rare motifs can be
searched deterministically. Design: [ARCHITECTURE.md](ARCHITECTURE.md).

```sh
uv sync
export CHESSTROVE_DATABASE_URL=postgresql:///chesstrove   # any Postgres 14+
uv run chesstrove init-db
uv run chesstrove import-pgn my_games.pgn   # re-running is safe: duplicates are skipped
uv run chesstrove import-chesscom myname   # first run fetches everything; later runs only new months
uv run chesstrove imports
uv run chesstrove games --player myname
uv run chesstrove game 1                    # moves + events

uv run chesstrove detectors                 # what each detector finds, and its version
uv run chesstrove events --type UNDERPROMOTION
uv run chesstrove events --type MISSED_MATE_IN_ONE --player myname --since 2024-01-01
uv run chesstrove reanalyze                 # after bumping/adding a detector: redoes only what's stale

uv run pytest                              # spins up an embedded Postgres; no setup needed
uv run scripts/benchmark.py --db
```
