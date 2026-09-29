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

uv run pytest                              # spins up an embedded Postgres; no setup needed
uv run scripts/benchmark.py --db
```
