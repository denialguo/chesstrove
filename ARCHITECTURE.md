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

  engine.py            Layer 2: EngineSettings (config identity), analyze_game(), probes, Analyzer,
                       run(): worker pool (spawned processes, one Stockfish each, one game per task),
                       single DB writer, resumable, Ctrl-C safe, newest games first
  insights.py          query-time combination: engine_analysis attached to events (`events --engine`)
  labels.py            engine-derived labels from stored evaluations; thresholds are query parameters
scripts/benchmark_engine.py   Stockfish throughput and worker scaling on a real-game sample, in a throwaway DB

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
- **Failure isolation:** a savepoint per game, a transaction per 500 games. Unsupported variants are counted in
  `games_skipped` (intentional, not an error). Other failures go to `imports.errors`
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
| `UNDERPROMOTION` (v2) | Is a forced/irrelevant underpromotion interesting? What can be said about queening without an engine? | Any promotion to N, B or R. Metadata: piece, square, capture, gave_check, gave_mate, plus exact facts about queening on the same square instead: `queen_gives_check`, `queen_gives_mate`, `queen_stalemates`. These facts never claim which move was best; that needs the engine (see below). |
| `PROMOTION_CHECKMATE` | Must the new piece give the check, or does a discovered mate by a promoting pawn count? Queen promotions? | Any promotion move that mates, any piece. Metadata `promoted_piece_checks: bool` separates direct from discovered. |
| `EN_PASSANT_CHECKMATE` | Discovered mate (the capturing pawn opens a line) vs. direct pawn mate | Any en passant capture that mates. Metadata `checkers` lists the checking squares. |
| `KING_DELIVERED_MATE` | A king can't give check itself, so this only happens via discovery or castling (the rook mates) | Moving piece is the king (castling included) and the move mates. Metadata `is_castling`, `checkers`. |
| `DOUBLE_CHECK` | Include double-check mates? Separate discovered-double type? | `len(board_after.checkers()) >= 2`. Every double check is also a discovered check, so one type is enough. Metadata `is_checkmate`, `checkers`. |
| `THREE_PLUS_QUEENS` | Total or per side? Once per game or every ply? | Total queens on board ≥ 3, emitted on the ply where the count goes from < 3 to ≥ 3. It emits again if the count drops below 3 and later comes back. Metadata: white/black queen counts. |
| `DOUBLE_DISAMBIGUATED_SAN` | Source SAN may be over-disambiguated by the exporting site | Uses the SAN computed by python-chess (minimal disambiguation), never the PGN text: the SAN names both origin file and rank, e.g. `Qh4e1`. Pawns never qualify. |
| `MISSED_MATE_IN_ONE` | Played move mates but a different mate existed? Mate available on the final position (resignation/timeout)? Repeated misses on consecutive turns? | Emit only if ≥1 legal mating move exists and the played move doesn't mate. The final position with no move played is not a "missed" move (possible later `MATE_AVAILABLE_AT_END`). Each missed ply is its own event. Metadata: sorted SAN list of mating moves and the move played. For speed, a bitboard pre-filter skips moves that can't possibly check, then `gives_check` is tested before the push-and-test for mate. Verified against brute force on 33k positions. |

| `SMOTHERED_MATE` (v2) | Knight-only, or any mate where the king is boxed in by its own pieces? Does an empty flight square disqualify it? | Mate delivered by a knight (the knight is among `checkers`), and each square next to the mated king is either held by the king's own pieces or covered by the **mating knight itself**: no other piece helps trap the king. `pure` = every neighbouring square is the king's own piece (the textbook picture). v1 required `pure`; v2 follows the "the knight alone does the work" reading. Knight + discovered double-check mates still count if the knight checks. |
| `BACK_RANK_MATE` | Must the escape squares be blocked by own pieces, or is "attacked" enough? Queen or rook only? | Mated king stands on its own back rank; a rook or queen checks it along that rank; every square on the next rank adjacent to the king is occupied by the mated side's own pieces. Squares that are only attacked don't qualify. |

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
| engine name + version, exactly as the binary reports it over UCI (`id name`, e.g. `Stockfish 18`) | different versions evaluate differently. Taken from the binary, not the package manager: Homebrew's `stockfish 19` package reports itself as `Stockfish 18` |
| limit kind + value: `nodes` or `depth` | the main strength knob. **Time limits are not allowed**: they depend on machine load and aren't reproducible |
| MultiPV | changes which alternatives are recorded, and slightly changes search |
| Threads (default 1) | multi-threaded search is non-deterministic. Parallelism comes from workers, not threads |
| Hash (MB) | affects search results under node limits |

`UCI_ShowWDL` is always on and doesn't affect search. Chess960 mode is set per game automatically by
python-chess. Options that would change search are simply not exposed, so they can't vary unrecorded.

The engine binary's SHA-256 is recorded on each run as metadata but is not part of the identity. Different
builds of the same version (e.g. AVX2 vs. generic) search identically.

Determinism: Threads = 1, fixed node or depth limit, `ucinewgame` (hash cleared) at the start of each game,
positions searched in a fixed order (0 → N). Under those rules, re-running a config on a game reproduces its
results, and any difference is a bug. **Verified:** repeated 200k-node searches after `ucinewgame` gave
identical scores, best moves, node counts and WDL, and a test re-analyzes a game and asserts identical results.

### Schema

Built: `engine_configs`, `engine_runs`, `engine_game_status`, `engine_positions` (Phase 6) and
`engine_move_probes` (Phase 7). There is no `engine_events` table: engine labels are derived at query time
(see *Engine-derived labels*), so thresholds can change without rewriting anything.

```sql
engine_configs (id, engine_name,                  -- UCI id name, includes the version
                limit_kind, limit_value, multipv, threads, hash_mb,
                UNIQUE (engine_name, limit_kind, limit_value, multipv, threads, hash_mb))

engine_runs    (id, config_id, status,            -- running | completed | failed | cancelled
                workers, binary_path, binary_sha256,
                games_total, games_done, games_failed, positions_done,
                engine_seconds,                   -- wall time inside searches
                errors jsonb, started_at, finished_at)

engine_game_status (config_id, game_id, run_id, positions, completed_at,
                    PRIMARY KEY (config_id, game_id))   -- the unit of completion and resumption

engine_positions (config_id, game_id, position,   -- 0..N, position after `position` plies
                  score_cp int, mate int,         -- White's POV; exactly one is non-null;
                                                  -- mate = 0: side to move is checkmated (not searched)
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
  a new `config_id`. Old results stay until explicitly pruned (a `chesstrove engine prune --config …`
  command is planned).
- Storage: one `engine_positions` row per position per config. **Measured: 234 bytes per position** including
  the index (MultiPV 1, PV capped at 12 plies), so a 1M-position history is ~0.23 GB per config.

### Engine-derived labels (built)

Labels are **derived at query time** from stored evaluations ([labels.py](src/chesstrove/labels.py)).
Thresholds and scale are parameters, not stored state: `--blunder 0.25` or `--scale stockfish` relabels the
whole history in seconds and never re-runs Stockfish.

Every label uses **expected score** (0 to 1, the mover's point of view), on one of two scales:

| Scale | Formula | Calibrated to |
|---|---|---|
| `lichess` (default) | 1 / (1 + e^(−0.00368208 · cp)); forced mate 1 or 0 | human games |
| `stockfish` | (W + D/2) / 1000 from Stockfish's WDL | engine-strength play |

**Why the default is `lichess`, measured on a real 176,056-move history:**
- **Blunders:** the Stockfish scale labelled 8.9% of moves `BLUNDER`, the lichess scale 2.8%.
- **Missed wins:** 3.8% vs 0.31%.
- **Unrankable ties:** the Stockfish scale put 1,687 moves at the maximum drop, so they couldn't be ranked
  against each other.

Stockfish's WDL is right about engines, for which +1.5 is nearly always a win, but that's far too steep for
people. So ordinary human swings looked like blunders.

| Label | Definition (defaults) |
|---|---|
| `BLUNDER` | expected score before − after ≥ 0.30 |
| `MISSED_WIN` | before ≥ 0.90 (clearly winning, including forced mates) and after ≤ 0.60 |
| `ONLY_WINNING_MOVE` | best line ≥ 0.90, second-best line ≤ 0.60, the mover played the best line, and it isn't a recapture on the square the opponent just captured on (`--include-recaptures` to keep those) |

Rows carry `is_capture`, `is_check` and `is_recapture`. Ties are broken by centipawn swing (drops), or quiet moves
first (only-moves). Without the recapture rule, the first run found 3,774 "only winning moves" for one player,
and they were mostly obvious take-backs like `Kxh1`. With it (and the lichess scale), there were 293, 31 of them quiet.

`ONLY_WINNING_MOVE` needs the second-best line, which a MultiPV-1 history doesn't have. Rather than paying
MultiPV 2 on every position, it's two-stage: `chesstrove engine verify-only-moves` runs a `top_two` probe
(MultiPV 2, 2× the per-position budget, WDL per line) **only** where it can apply: the mover was clearly
winning on either scale, played the engine's choice, and had more than one legal move. It uses the config's
own settings, refuses a binary that reports a different engine, and is resumable.
Measured: 42,660 candidates (12% of positions) in 270 s on 13 workers.

CLI: `chesstrove engine labels --type BLUNDER|MISSED_WIN|ONLY_WINNING_MOVE [--player] [--scale]
[--blunder] [--winning] [--not-winning] [--include-recaptures]`. API: `GET /engine-labels?type=…`.

### Underpromotion: two separate questions

`UNDERPROMOTION` stays a Layer 1 event. The engine layer answers two different questions about it, each
with its own search (`engine_move_probes`, one row per kind), and never lets one stand in for the other:

- **A. Was the underpromotion the best move in the position?** (`kind = 'all_moves'`) One search from the
  position before the move with `searchmoves` = **every legal move** and MultiPV = their number, so every
  root move is scored in the same search iteration. Budget: config nodes × number of legal moves (each move
  gets about the normal per-position effort); depth configs search every line to the configured depth.
  - `is_best_move`: the played move scores at least as well as **every** legal move.
  - `tied_for_best_move`: best, and some other move scores exactly the same. For example, if `=Q#` and `=R#`
    both mate in 1, the rook underpromotion is tied, not unique.
  - `unique_best_move`: best, and strictly better than every other legal move.
  - `played_move_rank`: 1 + the number of moves scoring strictly better.
  - If the search didn't return a score for every legal move, all four are `null`. Never guess: an unscored
    move might be better.
- **B. Was underpromoting better than queening on the same square?** (`kind = 'vs_queen'`) One search
  over exactly {played underpromotion, queen promotion}, MultiPV 2, budget config nodes × 2. Gives
  `better_than_queen` (strictly better; equal is not better) plus both evaluations. It supports statements
  like "queening here would have thrown away the win".

Why A needs every legal move: an unrestricted search's first choice is not ground truth. In the
Saavedra-style test position, a 20k-node unrestricted search picked `Kd3` (+7.2), while scoring all 10
legal moves shows `g8=R` mates in 2 (and `Kc3` mates in 5). Comparing only against the engine's pick, or
against a few candidates, could both wrongly deny and wrongly grant "best". For ordinary moves (no probe),
events carry only `matches_engine_choice` (the played move is the unrestricted search's first choice) and
`rank_in_engine_lines` (within MultiPV lines, if any), and never `is_best_move`.

Probes run inside the same per-game task, after the game's positions, each with the hash cleared, so the
position results are identical whether or not probes ran.

**Deeper verification.** `chesstrove engine verify-underpromotions --nodes 1000000` re-asks both questions
under a stronger config. The results are stored under that config's own id, next to (never over) the
full-history verdict, and shown as `engine_analysis.deeper_verification`. On a real history (13 underpromotions
by one player, 25k → 1M nodes per move, 55 s total), 3 verdicts changed. One `c1=N+` went from rank 2 of
30 to **unique best**, a desperate defense in a lost position. A near-tie (`exf1=R+` +7.25 vs `=Q` +7.27) stayed
"unique best among all moves, not better than queening". Both searches are within noise of equal there, which
is exactly why both numbers are reported and not merged.

**Cost, measured on 19 real underpromotions** (median 30 legal moves, max 44): the all-moves search takes
0.17 s median (0.99 s max) at 25k nodes per move, 0.61 s median (3.95 s max) at 100k. That's 4.7 s and 17.5 s in total
for the whole history, and every search scored every legal move. The queen comparison adds under 1 s in total.
Exhaustive is affordable because underpromotions are rare.

The combined answer is produced at query time (`chesstrove events --engine`, `GET /events?engine=true`):

```json
{"type": "UNDERPROMOTION",
 "metadata": {"promotion_piece": "knight", "gave_check": true, "gave_mate": false,
              "queen_gives_check": false, "queen_gives_mate": false, "queen_stalemates": false},
 "engine_analysis": {"config": {"id": 1, "engine": "Stockfish 18", "nodes": 25000, "multipv": 1},
                     "eval_before": {"cp": 470}, "eval_after": {"cp": 455},
                     "engine_choice": "e7e8n", "matches_engine_choice": true,
                     "is_best_move": true, "tied_for_best_move": false, "unique_best_move": true,
                     "played_move_rank": 1,
                     "all_moves": {"legal_moves": 10, "scored": 10, "budget": {"nodes": 250000},
                                   "evaluation": {"cp": 486}, "best_moves": ["e7e8n"],
                                   "best_evaluation": {"cp": 486}},
                     "better_than_queen": true,
                     "vs_queen": {"evaluation": {"cp": 404}, "queen_promotion_evaluation": {"cp": 0},
                                  "budget": {"nodes": 50000}}}}
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
  benchmark shows it's worth the complexity. Correctness comes first; if added, `engine_runs` gets a
  cache-hit counter to measure it.

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
  CLI (built; `--workers` arrives with the Phase 7 pool):

  ```text
  chesstrove engine analyze [--nodes 100000 | --depth 18] [--multipv 3] [--hash 64]
                            [--max-games N] [--stockfish PATH]
  chesstrove engine runs
  chesstrove status          # games, positions, deterministic: complete, stockfish: 43% (per config)
  ```

  API: `GET /status`, `GET /engine-runs`. Starting runs stays in the CLI: they're long local batch jobs.

### Cost: measure, don't promise

Wall time ≈ positions × (nodes per position ÷ single-thread nodes/sec) ÷ workers, plus overhead. Every term
except the position count is machine- and setting-dependent, so no times are promised before measuring.
`scripts/benchmark_engine.py` will report, for 1,000 / 5,000 / 10,000-game samples of a real history:

```text
positions analyzed · positions/sec · games analyzed · wall-clock time · total CPU time
worker count · limit (nodes/depth) · MultiPV
```

It will sweep worker counts to find where throughput stops scaling (memory bandwidth, thermal throttling;
sustained Layer 1 runs on this laptop already throttle by ~15%).

**Phase 6 measurement (1 worker, Stockfish 18, MultiPV 1, 60 real games / 2,544 positions, Apple Silicon):**

```text
nodes/position   ms/position   positions/s   wall    time inside search
        10,000            10         103.3     25 s    24 s
        25,000            24          41.8     61 s    61 s
       100,000            99          10.1    253 s   253 s
```

Cost is linear in nodes (~1 µs per node) and pipeline overhead is negligible: wall time ≈ time inside
search.

**Phase 7 worker scaling (10k nodes, 300 real games / 16,271 positions, M4 Pro: 10 performance + 4
efficiency cores):**

```text
workers   speedup   positions/s   wall    time inside search (sum over workers)
      1      1.0x          97     168 s   167 s
      2      1.9x         180      90 s   179 s
      4      3.5x         336      48 s   190 s
      8      6.0x         584      28 s   214 s
     10      6.7x         652      25 s   239 s
     13      7.1x         688      24 s   293 s
```

Near-linear to ~8 workers. Beyond that each search slows (shared memory bandwidth, then the slower
efficiency cores), so the gains shrink. Default: CPUs − 1 (13 here). Heavy CPU use is acceptable by design, and
`--workers 10` keeps 94% of the throughput while leaving the machine responsive. Confirmed at scale: **1,000
games / 58,671 positions in 78 s (748 positions/s) at 13 workers**, 243 bytes per stored position.

Projection for a 250k-position history at 13 workers: ~6 min at 10k nodes, ~15 min at 25k, ~1 h at
100k (from the measured linear cost). These are projections, not promises.

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
5. **Done: rare-event detectors.** The 10 above, including `UNDERPROMOTION` v2's rule-based queen-alternative
   facts. Unsupported variants are counted in `imports.games_skipped`, not `games_failed`.
6. **Done: Stockfish analysis subsystem.** `engine_configs`/`engine_runs`/`engine_game_status`/
   `engine_positions`, config identity, a single worker, per-game analysis with move history, rule-scored
   terminal positions, resume, Ctrl-C cancellation, engine-crash recovery, `chesstrove status`,
   `GET /status`, and a benchmark (60 real games at three node limits; the 1k/5k/10k runs come with the pool).
7. **Full-history engine indexing.** *Done:* worker pool (`--workers`) with the worker-count benchmark,
   incremental analysis of new imports (they're simply pending under each config), `engine_move_probes`,
   underpromotion questions A and B, `events --engine`, and engine-derived labels (BLUNDER, MISSED_WIN,
   and two-stage ONLY_WINNING_MOVE), relabelable without re-running Stockfish. Measured on 1,000 real games;
   since cost is linear per position, 5k/10k runs add no information beyond wall time.
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
