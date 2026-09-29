# ChessTrove architecture

> ChessTrove indexes a player's entire Chess.com/Lichess history so they can uncover rare motifs, unusual
> positions, engine insights, and recurring patterns across every game they've played.

The core is **large-scale personal chess-history indexing**. It is not a game reviewer or an AI coach.
The system:

```text
import a huge personal chess corpus → reconstruct it correctly → persist it
  → derive deterministic events → optionally engine-analyze every position → make all of it queryable
```

## Two analysis layers

| | Layer 1: deterministic index | Layer 2: engine-analysis index |
|---|---|---|
| What | Move facts + rule-based motif events | Stockfish evaluation of every position |
| Tools | `python-chess` only | Stockfish via `python-chess`'s UCI driver |
| Cost | ~minutes for a 16k-game history | hours; run once, persisted, reused |
| When | Inline with import; complete as soon as import is | Separate long-running batch, resumable |
| Changes when | a detector's version changes | engine version or search settings change |

**The layers are decoupled.** Importing never waits for Stockfish, and everything in Layer 1 is usable
while Layer 2 is 0%, 43% or 100% done. Layer 2 reads games and moves; it never writes to Layer 1 tables.
Queries that need engine data (Phase 8) simply cover the positions analyzed so far.

```text
games imported ──► deterministic index (complete immediately) ──► ChessTrove usable

meanwhile, independently:
  games lacking results for engine config C ──► N Stockfish workers ──► engine index ──► more queries light up

  8,421 games imported · 653,812 positions indexed
  Deterministic analysis: complete · Stockfish analysis: 43%
```

## Layer 1 pipeline (built)

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
  importers/pgn.py     PGN text -> CanonicalGame; read_one()/to_canonical() are reused by API importers
  importers/chesscom.py  public API client (stdlib urllib, backoff on 429/5xx) + archive JSON -> CanonicalGame
  importers/lichess.py   NDJSON export stream (oldest first, ongoing included) -> CanonicalGame
  ingest.py            batching, dedupe, per-game failure isolation, import bookkeeping,
                       per-month Chess.com resume, timestamp-checkpoint Lichess resume
  analysis.py          analyze() (one replay, all detectors), tracked analysis runs, reanalyze()
  detectors/           base.py (Event, Detector, helpers), one module per family, registry in __init__.py
  db.py                all SQL (plain psycopg 3, no ORM); built-in Postgres when no URL is configured
  schema.sql           full schema, idempotent
  cli.py               chesstrove init-db | import-pgn | import-chesscom | import-lichess | imports | games | game |
                                  events | detectors | reanalyze | serve
  api.py               FastAPI over the same functions; long jobs return 202 + id and run in the background
scripts/benchmark.py
tests/                 real Postgres (embedded via pgserver, or $CHESSTROVE_TEST_DATABASE_URL)

planned (Layer 2):
  engine/config.py     EngineConfig: identity of a reproducible analysis setting
  engine/worker.py     one Stockfish process per worker; analyzes one whole game per task
  engine/runner.py     work selection, process pool, single DB writer, progress, cancellation
  engine/detectors/    engine-backed classifications computed from stored evaluations (no Stockfish)
scripts/benchmark_engine.py
```

The importer "interface" is a convention rather than an ABC: an importer is any iterable of
`CanonicalGame | ParseFailure`. Chess.com and Lichess both serve PGN, so they fetch, then call
`read_one(pgn, source, ref)`. `run_import()` doesn't care where items came from.

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

## Schema: Layer 1 (built)

See [schema.sql](src/chesstrove/schema.sql). Tables: `users`, `chess_accounts`, `imports`, `games`, `moves`,
`analysis_runs`, `events`, `game_analysis`.

- **Idempotency:** `games.source_key UNIQUE` + `INSERT … ON CONFLICT DO NOTHING RETURNING id`.
- **Resumability:** re-running an import skips stored games before replaying them. For Chess.com,
  `imports.resume_state = {"months_done": [...]}` is updated after each past month's games are committed.
  The current month is always refetched. A month that fails to download is logged in `errors` and retried on
  the next run. For Lichess, `resume_state = {"since": <ms>}` is saved after each committed batch of the
  oldest-first stream and becomes the next run's `since`. Ongoing games are requested too, only so the
  checkpoint can stop before the first one: a game in progress during one import is fetched again (finished)
  by the next. Both sources read their resume state as a union/max over all past imports of the account, so a
  fresh import row never hides progress.
- **Failure isolation:** a savepoint per game, a transaction per 500 games; failures go to `imports.errors`
  (capped at 1000) with their index in the input. A game that *raises* while being stored (as opposed to a
  deterministic parse failure) holds the source's resume point, so it's retried next run.
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

## Layer 1 detector definitions

Each class's docstring is its definition (`chesstrove detectors` prints them).
Unless a note says otherwise, "mate" means `board_after.is_checkmate()`.

| Detector | Ambiguity | Definition |
|---|---|---|
| `UNDERPROMOTION` | Is a forced/irrelevant underpromotion interesting? | Any promotion to N, B or R. Metadata: piece, square, capture, gave_check, gave_mate. No "was it best" judgment; that needs the engine (see below). |
| `PROMOTION_CHECKMATE` | Must the new piece give the check, or does a discovered mate by a promoting pawn count? Queen promotions? | Any promotion move that mates, any piece. Metadata `promoted_piece_checks: bool` separates direct from discovered. |
| `EN_PASSANT_CHECKMATE` | Discovered mate (the capturing pawn opens a line) vs. direct pawn mate | Any en passant capture that mates. Metadata `checkers` lists the checking squares. |
| `KING_DELIVERED_MATE` | A king can't give check itself, so this only happens via discovery or castling (the rook mates) | Moving piece is the king (castling included) and the move mates. Metadata `is_castling`, `checkers`. |
| `DOUBLE_CHECK` | Include double-check mates? Separate discovered-double type? | `len(board_after.checkers()) >= 2`. Every double check is also a discovered check, so one type is enough. Metadata `is_checkmate`, `checkers`. |
| `THREE_PLUS_QUEENS` | Total or per side? Once per game or every ply? | Total queens on board ≥ 3, emitted on the ply where the count goes from < 3 to ≥ 3. It emits again if the count drops below 3 and later comes back. Metadata: white/black queen counts. |
| `DOUBLE_DISAMBIGUATED_SAN` | Source SAN may be over-disambiguated by the exporting site | Uses the SAN computed by python-chess (minimal disambiguation), never the PGN text: the SAN names both origin file and rank, e.g. `Qh4e1`. Pawns never qualify. |
| `MISSED_MATE_IN_ONE` | Played move mates but a different mate existed? Mate available on the final position (resignation/timeout)? Repeated misses on consecutive turns? | Emit only if ≥1 legal mating move exists and the played move doesn't mate. The final position with no move played is not a "missed" move (possible later `MATE_AVAILABLE_AT_END`). Each missed ply is its own event. Metadata: sorted SAN list of mating moves and the move played. For speed, a bitboard pre-filter skips moves that can't possibly check, then `gives_check` is tested before the push-and-test for mate. Verified against brute force on 33k positions. |

Planned Layer 1 additions (definitions to confirm):

| Detector | Ambiguity | Proposed definition |
|---|---|---|
| `SMOTHERED_MATE` | Knight-only, or any mate where the king is boxed in by its own pieces? | Mate delivered by a knight (the knight is among `checkers`) and every on-board square adjacent to the mated king is occupied by the mated side's own pieces. Knight + discovered double-check mates still count if the knight checks. |
| `BACK_RANK_MATE` | Must the escape squares be blocked by own pieces, or is "attacked" enough? Queen or rook only? | Mated king stands on its own back rank; the checker is a rook or queen on that same rank; every square on the next rank adjacent to the king is occupied by the mated side's own pieces. Squares that are only attacked don't qualify. |
| `UNDERPROMOTION` v2 | What exact facts about the queen alternative are safe to state without an engine? | Adds rule-based facts about queening on the same square instead: `queen_gives_check`, `queen_gives_mate`, `queen_stalemates` (the opponent would have no legal moves and not be in check). These are exact facts. They are **never** used to claim which move was best. |

Events store `color` (the side that moved). "Who" is resolved at query time: `events --player NAME` matches
the event's color against `games.white/black`.

## Layer 2: engine-analysis index (planned)

### What gets analyzed

For each game, every position from the start (position 0) through the final one (position N, after the last
move). Position *k* is the position after *k* plies, so move *k* goes from position *k−1* to position *k*. That
gives each move a before-and-after evaluation from the same engine config.

- **Move history is part of the input.** Stockfish receives `position <start> moves …`, not a bare FEN, so
  threefold repetition and the 50-move rule are visible to the search. A bare FEN can report +3 in a
  position that is a forced draw by repetition. This is why results are keyed by game position rather than
  by FEN (see *Identity and caching*).
- **Terminal positions are not sent to the engine.** Checkmate and stalemate are scored by the rules
  (mate 0, or 0 cp). The engine would have no move to search anyway.

### Identity and reproducibility

An `engine_configs` row is the identity of an analysis setting. Two results are comparable only if they
share a config. Identity fields:

| Field | Why it's in the identity |
|---|---|
| engine name + version (from the UCI `id name`, e.g. `Stockfish 17.1`) | different versions evaluate differently |
| limit kind + value: `nodes` or `depth` | the main strength knob. **Time limits are not allowed**: they depend on machine load and aren't reproducible |
| MultiPV | changes which alternatives are recorded, and slightly changes search |
| Threads (default 1) | multi-threaded search is non-deterministic. Parallelism comes from workers, not threads |
| Hash (MB) | affects search results under node limits |
| other UCI options that change search (e.g. `UCI_ShowWDL` doesn't; `Contempt`-style options would) | only options that affect results |

The engine binary's SHA-256 is recorded on each run as metadata but is not part of the identity. Different
builds of the same version (e.g. AVX2 vs. generic) search identically.

Determinism: Threads = 1, fixed node or depth limit, `ucinewgame` (hash cleared) at the start of each game,
positions searched in a fixed order (0 → N). Under those rules, re-running a config on a game reproduces its
results, and any difference is a bug.

### Schema

```sql
engine_configs (id, engine_name, engine_version, limit_kind, limit_value, multipv, threads, hash_mb,
                options jsonb, UNIQUE (engine_name, engine_version, limit_kind, limit_value, multipv,
                threads, hash_mb, options))

engine_runs    (id, config_id, scope,            -- 'all' | 'new' | explicit game ids
                status, workers, binary_sha256,
                games_total, games_done, positions_done, cache_hits,
                cpu_seconds, started_at, finished_at, error)

engine_game_status (config_id, game_id, run_id, positions, completed_at,
                    PRIMARY KEY (config_id, game_id))   -- the unit of completion and resumption

engine_positions (config_id, game_id, position,   -- 0..N, position after `position` plies
                  score_cp int, mate int,         -- White's point of view; exactly one is non-null
                  wdl smallint[3],                -- win/draw/loss per mille, White's POV (optional)
                  best_uci text, pv_uci text[],   -- PV capped (e.g. 12 plies) to bound storage
                  multipv jsonb,                  -- [{uci, score_cp, mate}] when MultiPV > 1
                  depth int, seldepth int, nodes bigint,
                  PRIMARY KEY (config_id, game_id, position))

engine_move_probes (config_id, game_id, position, moves text[],  -- restricted search (UCI searchmoves)
                    results jsonb,                               -- [{uci, score_cp, mate, pv}] ranked
                    PRIMARY KEY (config_id, game_id, position, moves))
```

- Scores are stored from **White's point of view**, so "games where I was +5 and lost" is one comparison
  plus a color flip. Per-move quality is derived, not stored. A view joins `moves` (ply *k*) with
  `engine_positions` *k−1* and *k* to give each move its evaluation before and after (mover's point of view),
  its evaluation loss, `is_best` and its MultiPV rank. It becomes a materialized view only if queries need it.
- **Nothing in `moves` or `events` is ever rewritten by engine analysis.** A new config adds rows under
  a new `config_id`. Old results stay until explicitly pruned (`chesstrove engine prune --config …`).
- Storage: roughly one `engine_positions` row per ply per config, ~150–250 bytes with a capped PV. A
  1M-ply history is ~0.2 GB per config. That's fine for Postgres; to be confirmed by benchmark.

### Engine-backed events

Classifications like BLUNDER, MISSED_WIN or ONLY_WINNING_MOVE are **engine detectors**. They're
versioned functions of stored evaluations and never run Stockfish. Their output goes to
`engine_events (config_id, detector_id, detector_version, game_id, ply, type, color, metadata)`, separate
from `events`. Changing a threshold means bumping the engine detector's version and recomputing from stored
evaluations in seconds. A new Stockfish version or depth means a new config, which is analyzed and then
classified. Deterministic `events` never carry engine data.

### Underpromotion: two separate questions

`UNDERPROMOTION` stays a Layer 1 event. The engine layer answers two different questions about it, and
stores them separately:

- **A. Was the underpromotion the best move in the position?** This compares the played move against
  *all* legal moves, from `engine_positions[position k−1]`. The comparison is by score, not by move string:
  `is_best` = the played move's score equals the best score (same centipawns, or same mate distance).
  `uniquely_best` = it's best and no other move scores the same. Example: if `=Q#` and `=R#` both mate, the
  rook underpromotion is tied for best, not uniquely best. The played move's rank comes from MultiPV when it's
  in the top *k*. Otherwise only "not in top *k*" is known.
- **B. Was underpromoting better than queening on the same square?** A restricted search from the same
  position (`engine_move_probes`, `searchmoves = {played underpromotion, queen promotion}`, MultiPV 2) scores
  both moves in one search under the same config, so the comparison is fair. This gives `better_than_queen`
  and `queen_promotion_eval`, and supports statements like "queening here would have thrown away the win".

Probes are queued automatically for every `UNDERPROMOTION` event once its game has been analyzed under a
config. The combined answer is produced at query time:

```json
{"type": "UNDERPROMOTION",
 "metadata": {"promotion_piece": "knight", "gave_check": true, "gave_mate": false,
              "queen_gives_check": false, "queen_gives_mate": false, "queen_stalemates": false},
 "engine_analysis": {"config": {"engine": "Stockfish 17.1", "nodes": 250000},
                     "is_best_move": true, "uniquely_best": true, "played_move_rank": 1,
                     "best_move": "e7e8n", "evaluation": 520,
                     "queen_promotion_evaluation": 15, "better_than_queen": true}}
```

Rule-based facts (`queen_gives_mate` and so on) and engine facts sit side by side, and never substitute for
each other.

### Identity and caching

- **Exact reuse:** a game analyzed under config *C* is never analyzed again under *C*
  (`engine_game_status`). Newly imported games are the only new work.
- **Transposition cache (later, optional):** the same position often recurs across games, especially in the
  opening. Reusing a result is only valid when the position's history can't affect the search: the
  halfmove clock is 0 (the last move was a capture or pawn move), so no earlier position can repeat. Opening
  positions mostly fail this test, so the win is smaller than it looks. This cache is added only if a
  benchmark shows it's worth the complexity. Correctness comes first, and the cache-hit counter on
  `engine_runs` exists to measure it.

### Processing model

```text
runner (parent process)
  ├── selects games with no engine_game_status row for config C (newest first by default,
  │   so recent games get insights first; keyset-paginated)
  ├── multiprocessing pool: worker 1 … worker N, each owning one Stockfish process (Threads=1)
  │     task = one whole game → list of position results (+ probes)
  └── single DB writer: each finished game's rows + its engine_game_status row in one transaction
```

- **Unit of work = one game.** It keeps positions in order (needed for determinism) and makes a game
  the atomic unit of completion.
- **Resumable and cancellable:** on Ctrl-C or a crash, finished games are already committed, and at most
  N in-flight games are lost. Re-running the same command resumes, with no special resume state needed.
- **Incremental:** new imports create games with no status row, so the next `engine analyze` picks them up.
  Optionally, the import can queue them automatically.
- **Settings change** (new Stockfish, deeper search): a new config, so every game is new work under it.
  Existing results are untouched.
- **Progress:** `games_done / games_total` and positions per second on `engine_runs`, updated per game,
  surfaced in `chesstrove status` / `GET /status` (the "Stockfish analysis: 43%" line).
- **Single machine only.** No queue service or distributed workers. Worker count defaults to physical cores
  minus one, then gets tuned by benchmark.
- **Configuration:** Stockfish path from `--stockfish`, `$CHESSTROVE_STOCKFISH`, or `stockfish` on `PATH`.
  CLI sketch:

  ```text
  chesstrove engine analyze [--nodes 250000 | --depth 18] [--multipv 3] [--workers N]
                            [--games all|new] [--stockfish PATH]
  chesstrove engine status
  chesstrove engine prune --config ID
  chesstrove status          # games, positions, deterministic: complete, stockfish: 43% (per config)
  ```

### Cost: measure, don't promise

Wall time ≈ positions × (nodes per position ÷ single-thread nodes/sec) ÷ workers, plus overhead. Every term
except the position count is machine- and setting-dependent, so no times are promised before measuring.
`scripts/benchmark_engine.py` will report, for 1,000 / 5,000 / 10,000-game samples of a real history:

```text
positions analyzed · positions/sec · games analyzed · wall-clock time · total CPU time
worker count · limit (nodes/depth) · MultiPV · cache hits
```

It will sweep worker counts to find where throughput stops scaling (memory bandwidth, thermal throttling;
sustained Layer 1 runs on this laptop already throttle by ~15%).

## Reprocessing matrix

| Change | What reruns | What doesn't |
|---|---|---|
| New or bumped Layer 1 detector | that detector, over stale games, from stored moves (`reanalyze`) | no download, no parsing, no engine |
| New Stockfish version or deeper search | Layer 2 under the new config (`engine analyze`) | Layer 1, events, older configs' results |
| Engine detector threshold changed | engine detector over stored evaluations | Stockfish |
| New games imported | Layer 1 inline for those games; Layer 2 picks them up on its next run | everything already indexed |

## Phases

1. **Done: PGN / Chess.com import.** Dedupe, resumable per-month Chess.com import, partial-failure
   handling. Also Lichess (checkpointed stream, ongoing-aware) and a REST API.
2. **Done: canonical games + one-pass reconstruction.**
3. **Done: persistent moves / positions / facts.**
4. **Done: deterministic detector framework**, with per-game version tracking and `reanalyze`.
5. **Done (first set): rare-event detectors.** The 8 above. Next in this phase: `UNDERPROMOTION` v2
   rule-based queen-alternative facts, `SMOTHERED_MATE`, `BACK_RANK_MATE`, and a `games_skipped` counter so
   unsupported variants stop showing up as failures.
6. **Stockfish analysis subsystem.** `engine_configs`/`engine_runs`/`engine_game_status`/`engine_positions`,
   config identity, a single worker, per-game analysis with move history, resume, cancellation, status,
   and a benchmark on 1,000 real games.
7. **Full-history engine indexing.** Worker pool plus the worker-count benchmark (1k / 5k / 10k games),
   incremental analysis of new imports, `engine_move_probes`, underpromotion questions A and B, and
   engine detectors (missed wins, blunders, only-winning-moves).
8. **Queries combining both layers**, as views and `GET` endpoints. For example: underpromotions that were
   best moves, queen promotions that caused stalemate, games with 3 queens that were lost, king-delivered mates
   after an engine mistake, only-winning moves found, biggest blunders, games where I was +5 and lost.

## Out of scope for now

LLM explanations, natural-language queries, agents, embeddings, coaching, social features, and an elaborate
frontend. These can all consume the indexed data later.

## Layer 1 performance (measured)

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
3. `multiprocessing` over games (the pipeline is per-game and embarrassingly parallel), the same pool
   design as Layer 2.
