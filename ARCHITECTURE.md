# ChessTrove architecture

A deterministic indexer for one player's chess history. No engine, no LLM.

## Pipeline

```text
PGN file / Chess.com API / Lichess API
        │  importers/*.py        yield CanonicalGame | ParseFailure
        ▼
  CanonicalGame                  models.py (source-independent, has raw PGN + validated UCI mainline)
        │  ingest.run_import     dedupe on source_key BEFORE any replay; one savepoint per game
        ▼
  replay(game)                   reconstruction.py, one pass, yields MoveContext per ply
        │                        (board_before, move, board_after, san, MoveFacts)
        ├──► moves rows          COPY, per game
        └──► detectors/          every detector sees the same MoveContext stream (analysis.analyze)
                  ▼
               events rows + game_analysis (which detector versions saw this game)

  analysis.reanalyze             later: stored moves.uci -> replay -> only stale detectors -> replace events
```

A game is replayed **once** per pass. Detectors never replay games themselves.

## Layout

```text
src/chesstrove/
  models.py            CanonicalGame, MoveFacts, MoveContext
  reconstruction.py    replay(): the single place that walks a game's moves
  importers/pgn.py     PGN text -> CanonicalGame; to_canonical() is reused by API importers
  importers/chesscom.py  public API client (stdlib urllib, backoff on 429/5xx) + archive JSON -> CanonicalGame
  ingest.py            batching, dedupe, per-game failure isolation, import bookkeeping, per-month Chess.com resume
  analysis.py          analyze() (one replay, all detectors), tracked analysis runs, reanalyze()
  detectors/           base.py (Event, Detector, helpers), one module per family, registry in __init__.py
  db.py                all SQL (plain psycopg 3, no ORM)
  schema.sql           full schema, idempotent
  cli.py               chesstrove init-db | import-pgn | import-chesscom | imports | games | game |
                                  events | detectors | reanalyze | serve
  api.py               FastAPI over the same functions; long jobs return 202 + id and run in the background
scripts/benchmark.py
tests/                 real Postgres (embedded via pgserver, or $CHESSTROVE_TEST_DATABASE_URL)
```

Added in later milestones: `importers/lichess.py`.

The importer "interface" is a convention rather than an ABC: an importer is any iterable of
`CanonicalGame | ParseFailure`. Chess.com and Lichess both serve PGN, so they fetch, then call
`to_canonical(game, raw, source=...)`. `run_import()` doesn't care where items came from.

## Core types

- **`CanonicalGame`**: source, `source_key`, external id, played_at (UTC), players, ratings, result,
  time control, rated, ECO/opening, `initial_fen` (NULL = standard), `chess960`, `moves_uci`, raw `pgn`.
  - `source_key` is the global dedupe key: `chesscom:<id>` / `lichess:<id>` when the PGN has a platform URL
    (so a file export and a later API import collapse together), otherwise
    `sha256:` of the key headers plus the UCI mainline. Comments, clocks, variations and whitespace don't change it.
- **`MoveFacts`**: ply, color, SAN, UCI, piece, from/to, captured, check/mate/castling/en passant/promotion,
  FEN before/after, queens before/after, material per side, legal move count before. Computed once.
- **`MoveContext`**: `game, ply, board_before, move, board_after, san, facts`. `board_before` is a copy;
  `board_after` is the live board, valid only during that iteration. Detectors may push/pop but must restore.
- **Detector** ([detectors/base.py](src/chesstrove/detectors/base.py)):

  ```python
  @dataclass(frozen=True)
  class Event:
      type: str; ply: int; color: str; fen: str; metadata: dict

  class Detector(Protocol):
      id: str
      version: int
      def detect(self, ctx: MoveContext) -> list[Event]: ...

  DETECTORS: tuple[Detector, ...] = (...)   # adding one = new class + one line here + tests
  ```

  Detectors are stateless per ply. Anything "once per game" is expressed as a transition
  (e.g. queens went from <3 to >=3 on this ply), so no per-game state object is needed.

## Schema

See [schema.sql](src/chesstrove/schema.sql). Tables: `users`, `chess_accounts`, `imports`, `games`, `moves`,
`analysis_runs`, `events`, `game_analysis`.

- **Idempotency:** `games.source_key UNIQUE` + `INSERT … ON CONFLICT DO NOTHING RETURNING id`.
- **Resumability:** re-running an import skips stored games before replaying them. For Chess.com,
  `imports.resume_state = {"months_done": [...]}` is updated after each past month's games are committed.
  Each import copies the previous state forward, so the latest import for a username holds all of it. The current
  month is always refetched. A month that fails to download is logged in `errors` and retried on the next run.
- **Failure isolation:** a savepoint per game, a transaction per 500 games; failures go to `imports.errors`
  (capped at 1000) with their index in the input. A game that *raises* while being stored (as opposed to a
  deterministic parse failure) keeps its Chess.com month open, so it's retried next run.
- **Raw vs derived:** `games`/`moves` never reference `events`. `events(detector_id, detector_version)` +
  `analysis_runs.detector_versions` say what produced what.
- **Versioning / reprocessing:** `game_analysis.detector_versions` records, per game, which version of each
  detector has seen it. A game is stale for a detector iff `NOT detector_versions @> '{"ID": v}'`.
  `chesstrove reanalyze` (no args) finds detectors that are stale anywhere (bumped version or newly added),
  replays only the stale games from stored `moves.uci`, runs only those detectors, and replaces their events.
  One transaction per 500 games, so an interrupted run just resumes. `--detector X` limits it; `--all` forces
  every game. Nothing is downloaded or re-parsed.
- **Lookups:** by game (`moves` PK, `events` unique key leads with `game_id`), by event type
  (`events(type, color)`), by date and player (`games.played_at`, `lower(white|black)`).
- `moves` stores `fen_after` only. `fen_before` of ply *n* is `fen_after` of ply *n−1* or `games.initial_fen`.

## Detector definitions

Adopted as proposed. Each class's docstring is its definition (`chesstrove detectors` prints them).
Unless a note says otherwise, "mate" means `board_after.is_checkmate()`.

| Detector | Ambiguity | Definition |
|---|---|---|
| `UNDERPROMOTION` | Is a forced/irrelevant underpromotion interesting? | Any promotion to N, B or R. Metadata: piece, square, capture, gave_check, gave_mate. No "was it necessary" judgment (that needs an engine). |
| `PROMOTION_CHECKMATE` | Must the new piece give the check, or does a discovered mate by a promoting pawn count? Queen promotions? | Any promotion move that mates, any piece. Metadata `promoted_piece_checks: bool` separates direct from discovered. |
| `EN_PASSANT_CHECKMATE` | Discovered mate (the capturing pawn opens a line) vs. direct pawn mate | Any en passant capture that mates. Metadata `checkers` lists the checking squares. |
| `KING_DELIVERED_MATE` | A king can't give check itself, so this only happens via discovery or castling (the rook mates) | Moving piece is the king (castling included) and the move mates. Metadata `is_castling`, `checkers`. |
| `DOUBLE_CHECK` | Include double-check mates? Separate discovered-double type? | `len(board_after.checkers()) >= 2`. Every double check is also a discovered check, so one type is enough. Metadata `is_checkmate`, `checkers`. |
| `THREE_PLUS_QUEENS` | Total or per side? Once per game or every ply? | Total queens on board ≥ 3, emitted on the ply where the count goes from < 3 to ≥ 3. It emits again if the count drops below 3 and later comes back. Metadata: white/black queen counts. |
| `DOUBLE_DISAMBIGUATED_SAN` | Source SAN may be over-disambiguated by the exporting site | Uses the SAN computed by python-chess (minimal disambiguation), never the PGN text: the SAN names both origin file and rank, e.g. `Qh4e1`. Pawns never qualify. |
| `MISSED_MATE_IN_ONE` | Played move mates but a different mate existed? Mate available on the final position (resignation/timeout)? Repeated misses on consecutive turns? | Emit only if ≥1 legal mating move exists and the played move doesn't mate. The final position with no move played is not a "missed" move (possible later `MATE_AVAILABLE_AT_END`). Each missed ply is its own event. Metadata: sorted SAN list of mating moves and the move played. For speed, a bitboard pre-filter skips moves that can't possibly check, then `gives_check` is tested before the push-and-test for mate. Verified against brute force on 33k positions. |

Events store `color` (the side that moved). "Who" is resolved at query time: `events --player NAME` matches
the event's color against `games.white/black`.

## Milestones

1. **Done:** PGN ingestion, canonical game, one-pass replay, games + moves persisted, import status,
   dedupe, partial-failure handling, CLI, benchmark.
2. **Done: Chess.com import.** `GET /pub/player/{user}/games/archives`, then each month. Resume state is
   per month, the JSON `rated` flag and ECO URL fill gaps in the PGN, and `users`/`chess_accounts` are created
   on first import. Unsupported variants (Crazyhouse, King of the Hill, …) are recorded as failures.
3. **Done: detector framework plus the 8 detectors.** Detectors run inside `store_game` on the same replay.
   Also: `analysis_runs`, per-game version tracking, `reanalyze`, `events` search, and positive /
   near-miss / edge-case fixtures per detector in `tests/detectors/`.
4. **Done: REST API.** `POST /imports/pgn` (raw PGN body), `POST /imports/chesscom`, `GET /imports[/{id}]`,
   `GET /games[/{id}]`, `GET /events?type&color&player&since&until&game_id`, `GET /detectors`,
   `POST /analysis-runs`, `GET /analysis-runs[/{id}]`. Localhost only, no auth. A job whose server dies mid-run
   stays `running`; re-running the import is safe.
5. **Lichess import,** then profiling on a real 10k+ game history.

## Performance

Real history: Chess.com user `erik`, 16,162 games, 1.06M plies. Apple Silicon laptop; sustained runs
throttle, so treat these as ±15%.

```text
parse                        107,000 plies/s   10 s
replay + facts                20,000 plies/s   53 s   (FEN generation, legal-move count)
replay + facts + detectors     9,000 plies/s  118 s   (MISSED_MATE_IN_ONE is ~half of it)
full import (PGN -> DB)        4,700 plies/s  228 s   DB writes are ~10% of this; the rest is chess work
re-import (all duplicates)    44,000 plies/s   24 s   parse + ON CONFLICT, no replay
reanalyze --all                5,000 plies/s  214 s   from stored moves, no parsing
```

Events found: 1,105 MISSED_MATE_IN_ONE, 208 DOUBLE_CHECK, 194 THREE_PLUS_QUEENS, 113 UNDERPROMOTION,
45 PROMOTION_CHECKMATE, and 0 each for en passant mate, king-delivered mate and double-disambiguated SAN.

First import of a large history is a one-time ~4 min. Later imports only replay new games, and reanalyze
only replays stale games with stale detectors. If it ever matters, in order of payoff:
1. A bitboard exact check test in `mating_moves` (roughly halves MISSED_MATE_IN_ONE).
2. Drop `legal_moves_before`, or reuse the mate scan's move list for it.
3. `multiprocessing` over games (the pipeline is per-game and embarrassingly parallel).
