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

### Named mates: family and form (`detectors/named_mates.py`, v3)

Chess sources don't agree on rigid geometry for these names. Lichess, Wikipedia and the older books draw
different diagrams, and common usage stretches some names (Epaulette with non-rook shoulders, Damiano with a
bishop instead of a pawn). ChessTrove doesn't pretend otherwise. It keeps a recognised broad **family**
searchable, and records on every event how far the position is from the classical picture:

| Form | Meaning |
|---|---|
| `textbook` | Looks like the diagram people learn under that name. |
| `canonical` | The pattern's characteristic pieces do their characteristic jobs, unaided. Location or orientation may differ. |
| `variant` | Still that family, but other attackers help close the net, or the defining pieces differ. |

The family test decides whether an event is emitted at all, and a false positive there is worse than a miss.
The form is derived from concrete, stored traits, never from a score.

Everything is computed on the mating position; only single-check mates qualify. The **anatomy** of a mate
sorts the king's neighbours into two groups:
- `own`: squares held by the king's own pieces.
- `free`: every other neighbour, each with the attacking pieces that cover it (`cover`). Cover is computed
  with the king lifted off the board, and for the checker's own square it is the checker's defenders.

Each pattern names its **defining** pieces: the checker plus the pieces doing its jobs. **Helpers** are any
other attacking pieces that alone cover some free square. Own blockers never count against a form: the
distinction is about extra *attacking* help. (Known gap: a helper whose only job is to pin a would-be capturer
isn't counted.)

Event metadata, besides `king_square`, `checker` and `checker_piece`:
- `form`.
- `traits`: the booleans and descriptors the form came from, including `no_extra_helpers`.
- `short_of`: the traits that kept the event out of the next tier up. The UI turns these into "differs from
  the classic picture" phrases (`web/src/lib/motifs.ts`).
- `defining` and `helpers`, as piece labels like `Qh7`, `Na6`, `e6`.
- `own_blockers`, as squares.

Not every pattern has three tiers. Where loosening the family would empty the name of meaning, the family test
is already canonical and there is no variant tier. Where sources show no single stereotype, there is no
textbook tier. One mate may carry several names (`BACK_RANK_MATE` + `OPERA_MATE` is fine). Every pattern holds
under left-right mirroring and colour swap.

In the table, a dash means the tier doesn't exist for that pattern.

| Pattern | Family (emits the event) | Canonical | Textbook | Variant |
|---|---|---|---|---|
| Epaulette | Queen checks orthogonally from ≥ 2 squares away; both squares beside the king, across the line of check, hold the king's own pieces, of any kind. | Both shoulders are rooks and there are no helpers. | Also: the king has its back to the edge and the queen is 2 squares in front. | Non-rook shoulders, or helpers (e.g. a rook cutting off the line behind a mid-board king). Kept deliberately loose for review. |
| Swallow's tail | A guarded queen, orthogonally adjacent; both diagonal squares behind the king are own. The queen covers the rest by construction. | Every match (`rear` records the tail pieces). | The two tail pieces are the king's only own blockers. | – |
| Dovetail | A guarded queen, diagonally adjacent; the two squares on the far side from her are own. The geometry forces an off-edge king. | – | Every match. | – |
| Anastasia | A rook or queen mates along the king's edge; the square straight in from the king is own; knights cover every square the checker doesn't, and at least one of those squares needs the knight (otherwise the knight is incidental and it's not Anastasia). | One knight covers both characteristic flights (the two squares diagonally inward: g8 and g6 against Kh7). | Also: the Kh7 picture (side file, next to the corner), with no helpers. | The knight covers only one flight; the other is own-blocked or covered by the checker. Often a back-rank mate that a knight finishes. |
| Arabian | A rook, adjacent, guarded by a knight that also covers a square the rook doesn't, and that knight isn't guarded by a pawn (the chain makes it a Hook, never both). | Rook and knight need no helpers; the king needn't be cornered. | Also: the king is in the corner. | Helpers close squares too. Loose for review: mid-board kings and second heavy pieces included. |
| Boden | A bishop mates; a bishop on the other colour covers squares the first can't; bishops cover every free square; own pieces hem the king in. | Every match. | The king is on its own back rank. | – |
| Opera | A rook, adjacent along the king's edge, guarded by a bishop that also covers another flight square. A queen in the bishop's place doesn't count. | No helpers. | Also: the king is on its own back rank. | Helpers. |
| Anderssen | A rook or queen mates from the corner next to the king, guarded diagonally by a pawn that also covers a flight square. The pawn's own support (often the king) is part of the picture. | Every match. | A rook mates. | – |
| Lolli | A queen, adjacent directly in front of an edge king, guarded by a pawn. She covers every flight square herself, so helpers can't occur. Castling history isn't checked. | Every match. | The king is on its own back rank, within two squares of a corner. | – |
| Damiano | A queen mates from the edge square diagonally in front of a king one step from a corner (Qh7# vs Kg8, Qg8# vs Kh7), guarded by a pawn or bishop. Scholar's mate (Qxf7# vs Ke8) and Qb2# vs Kc1 are out. | No helpers; bishop support is fine. | Also: pawn support and the king on its own back rank. | Helpers. |
| Morphy | Cornered king; a bishop mates along the long diagonal; one edge square beside the king is own, and a rook covers the other. | – | Every match. | – |
| Greco | Cornered king; a rook or queen mates along an edge; bishops cover every square the checker doesn't; at least one own blocker. | Every match. | The mate comes down the side file, not along the back rank. | – |
| Hook | Rook, adjacent, guarded by a knight guarded by a pawn. It takes these mates from Arabian. | The chain needs no helpers. | Also: some own blockers close squares. | Helpers. |
| Corridor | ChessTrove's own generalised back-rank mate: along any edge except the king's back rank, own pieces fill the next line in. | Every match. | – (not a traditional name). | – |
| Blackburne | Two bishops and a knight do all the work (check, guard, cover). | Every match. | – (drawn in several arrangements). | – |
| Réti | A bishop mates from beside the king, guarded orthogonally by a rook or queen. The bishop also covers a flight square; the two cover every free square; ≥ 3 own blockers. Deliberately narrow. | Every match. | ≥ 4 own blockers and a rook guard (Réti–Tartakower, 1910). | – (omitted: a looser Réti means nothing). |
| Pillsbury | A rook mates coming straight in toward an edge king within two squares of a corner (never along the edge); a bishop covers the edge square beside the king on the corner side. A bishop covering some other square doesn't qualify. | No helpers (the rook's guard counts as defining when the rook is adjacent). | Also: the bishop covers the corner itself, the king is on its home rank, and the rook checks from a distance. | Helpers. |
| Ladder | Two heavy pieces: one mates along the edge, the other covers every square of the next line in. | – | Every match, rooks or queens alike (`both_rooks` records which). | – |
| Box | King and rook only: the rook mates along the edge, and the attacking king covers the rest. | – | Every match. | – |

Deliberate ChessTrove interpretations:
- **Pillsbury.** It extends to a king two squares from the corner, with the bishop covering the square toward
  the corner. This keeps game 2276 (Kh6, a lifted Rg6 guarded by the f5 pawn, Bg8 covering h7) as canonical,
  not textbook.
- **Dovetail.** Its family already is the classical geometry, so every match is textbook.

**Calibration on the real history** (all 5,558 games, both sides' mates; v1 → v3):

| Pattern | v1 | v2 family | Textbook | Canonical | Variant |
|---|---|---|---|---|---|
| Anastasia | 14 | 13 | 0 | 3 | 10 |
| Anderssen | 1 | 1 | 1 | 0 | 0 |
| Arabian | 7 | 11 | 4 | 2 | 5 |
| Box | 18 | 18 | 18 | 0 | 0 |
| Damiano | 20 | 42 | 3 | 34 | 5 |
| Dovetail | 25 | 25 | 25 | 0 | 0 |
| Epaulette | 2 | 6 | 1 | 0 | 5 |
| Greco | 3 | 3 | 2 | 1 | 0 |
| Hook | 7 | 10 | 7 | 0 | 3 |
| Ladder | 119 | 119 | 119 | 0 | 0 |
| Lolli | 39 | 39 | 25 | 14 | 0 |
| Opera | 16 | 16 | 7 | 5 | 4 |
| Pillsbury | 3 | 2 | 1 | 1 | 0 |
| Réti | 3 | 0 | 0 | 0 | 0 |
| Swallow's tail | 16 | 16 | 8 | 8 | 0 |

What the inspection found:
- **Réti.** All three v1 matches were coincidences: the bishop mated from two squares away, or the position
  was a Boden-like queen mate.
- **Pillsbury.** Game 5311 dropped out: its bishop covered the far-side square of a king in the middle of
  the edge.
- **Damiano.** It grew because bishop support is now allowed (Qh7# with Bd3, Qxh2# with Bd6). A first
  attempt also admitted Scholar's mate, which is why the family pins the queen to the corner's other edge.
- **Anastasia.** Ten of the 13 are back-rank mates that a knight finishes, so they are variants.
- **Epaulette and Arabian variants.** These are intentionally loose: mid-board kings with pawn shoulders,
  and rook-and-knight mates with up to four helpers. They stay until someone decides to narrow them.

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

### Engine archaeology (built)

The engine index answers questions about a whole career, not one game: *what's the biggest win I ever
threw away, the worst position I came back from, the sacrifices I got right?* ([archaeology.py](src/chesstrove/archaeology.py);
`GET /api/engine-discoveries?type=…&player=…`; the player page's "record book").

**Principles**
- **Derived, never stored.** Every discovery is a query over `engine_positions`, `engine_move_probes`, `games`
  and `moves`. The raw engine results are the durable truth. Every threshold is a `Params` field and an
  API query parameter, so changing one re-ranks instantly and nothing re-runs Stockfish. The only new
  engine work is a sparse probe set (below).
- **Evidence travels with every result:**
  - the game (players, date, result, and how it ended: the PGN `Termination` tag), the ply and the move;
  - the positions before and after;
  - evaluations from the player's side, with mate distances kept: `{"cp": 250}`, `{"mate": 3}` (mates in
    3), `{"mate": -2}` (is mated in 2), `{"mate": 0}` (mate on the board);
  - expected scores, the engine's choice (UCI and SAN), and the engine config;
  - `score {name, value}`, the quantity the result was ranked by.
- **Engine facts and ChessTrove definitions stay apart:**
  - Engine facts are evaluations, best lines and mate distances, each naming its config.
  - Everything else is ChessTrove's definition: what counts as a sacrifice, a comeback, an unusual move.
  - The table below marks which is which, and each function's docstring states its rule.
- **Mate-aware ordering.** Expected score maps every forced mate to 1.0 or 0.0, so ties are broken by an
  ordinal: mate delivered > mate in 1 > mate in 2 > … > any centipawn score > … > mated in 2 > mated in 1.
- **No "brilliant."** Nothing claims objective brilliance. The unusual-move ranking is a documented,
  configurable formula over visible features, and the product says "unusual engine-approved moves".

**Shared rules**, each found on the real history:
- **The engine's own choice is never a mistake.** If the played move was the engine's first choice, a
  later drop is the engine seeing further one ply later, not the player's error. This applies to
  `biggest_throw` and `missed_forced_mate`, and now to the `BLUNDER` and `MISSED_WIN` labels: 18 of 9,480
  BLUNDER rows were this artifact.
- **Trusted positions.** A position is untrusted when the engine contradicted itself one ply later: its own
  recommended move was played next and the expected score moved by more than 0.30. Untrusted positions
  never become a comeback's low point or a lost advantage's high point.

| Type | Engine facts used | ChessTrove definition | Ranked by |
|---|---|---|---|
| `biggest_throw` | evaluations before/after the player's move; engine choice | not the engine's choice | expected-score drop; ties: ordinal before ↓, after ↑ |
| `biggest_comeback` | every position's evaluation (player's side) | games the player won; trusted positions only | worst expected score, ordinal; then games decided on the board before time/abandonment |
| `lost_advantage` | same | games the player lost | best expected score, ordinal |
| `only_winning_move` | `top_two` probe: best line ≥ `winning` (0.90), runner-up ≤ `not_winning` (0.60), the player played the best line | not a recapture on the square just captured on; not a mate on the board (that's rule-based) | quiet moves first (a capture that parries a mate threat is an only move nobody misses), then the gap |
| `underpromotion` | `all_moves` and `vs_queen` probes (see *Underpromotion*) | the two questions kept apart: best move = unique / tied / not / unknown; vs queening = better / equal (incl. transposing) / worse / unknown | unique best, tied, rest; better than queening first |
| `material_sacrifice` | evaluations before/after; engine choice | see *Sacrifice* below | leads to mate, queen > rook > exchange, deficit, position after |
| `missed_forced_mate` | mate ≥ 2 before; no mate for the player after | not the engine's choice (mate in 1 is the rule-based `MISSED_MATE_IN_ONE`) | shortest mate first |
| `longest_mate_found` | a mate score before and after every move of the run | the run ends in the player's mate; not started against a bare king | `min(moves taken, engine mate distance at start)` |
| `only_move_keeping_mate` | `top_two`: best line is mate ≥ 2, runner-up doesn't mate | the runner-up isn't clearly winning either (< `winning`); without it, this was mostly "the only mate" where a runner-up at +8 to +12 won anyway | mate length, then gap |
| `unusual_move` | `top_two`: the player played the best line, every alternative ≥ `unusual_min_gap` (0.30) worse | not a recapture or a mate on the board; not a parry: the runner-up doesn't get mated (the real top of the list was king moves whose only competitor walked into mate in 1) | `unusualness` (below) |

**Sacrifice.** Deterministic candidates first, then the engine. The opponent's reply captures the player's
queen or rook, and over the reply and the next four plies the player never gets back to within the
threshold of where they stood before the move:
- **queen:** reply takes the queen, net deficit ≥ 5 throughout (a queen for at most a minor piece);
- **rook:** reply takes a rook, net deficit ≥ 3;
- **exchange:** reply takes a rook, net deficit ≥ 2.

Material before the move is derived exactly: the balance after the move, less what it captured and what
a promotion gained. It works at every ply, including set-up starts.

What is excluded:
- **Trades.** A queen taken and taken back within two moves never qualifies.
- **Moves made in check.** A king forced out of a fork doesn't sacrifice the queen.
- **Games that stop inside the window,** unless they end in the player's mate there. On the real history,
  ...Qxd2 was answered by resignation when Bxd2 simply traded queens.

"Sound":
- **Default:** the sacrificing move was the engine's own first choice ("gave up the queen and still had
  the best move").
- **With `sacrifice_engine_choice=false`:** the expected score after is within `sacrifice_tolerance`
  (0.05) of before.
- **Either way:** at least `sacrifice_floor` (0.50) afterwards, so no desperados.
- **Why engine choice is the default:** on the real history, tolerance alone passed queens thrown away at
  +22 in material with mate in one on the board. In a crushing position everything "keeps the win".

Subtypes:
- `never_recovered`: the deficit lasts to the end of the game.
- `ends_in_mate`: never recovered, and the player then mates.

What it misses, by design:
- a sacrifice declined;
- a piece taken several moves later;
- a sacrifice made one move before the capture (as with Ng5 allowing the ...Ne2+ fork).

**Longest mate.** A run is the player's moves up to their mating move, each made with a forced mate on
the board and keeping it. Its length is `min(moves the player took, engine mate distance at the start)`,
which is conservative on both sides:
- **The engine side:** at a fixed node budget a mate distance is an upper bound. A shallow search can find
  a long mate before a short one; one real run showed mate in 13, then mate in 5 one move later.
- **The player side:** a player who took 10 moves over a mate in 3 carried a mate in 3.

Mating a bare king is technique (K+Q v K), not a find.

**Unusual moves.** `unusualness = gap × (1 + w_quiet·quiet + w_retreat·retreat + w_sacrifice·sacrifice +
w_underpromotion·underpromotion)`.
- `gap` is how much worse the best alternative was, in expected score, within one two-line search.
- The features are deterministic marks of a non-obvious move: quiet (no capture, check or promotion), a
  retreat toward the player's own side, a sacrifice (definition above), an underpromotion.
- Default weights are 0.5, 0.5, 1, 1, all parameters. With every weight at 0 the ranking is the gap alone.

**Probe coverage.** Two-line searches (`top_two`) exist only on sparse candidate sets:
- **Clearly winning positions** where the player played the engine's choice (`engine verify-only-moves`).
- **Undecided positions** (expected score 0.10–0.90) where the player's move was the engine's choice, it
  wasn't a recapture or a mate, and there were several legal moves (`engine verify-unusual-moves
  --player …`): about 27k moves for this user's three accounts.

The unusual-move and only-move lists cover only those positions, and every result's `comparison.search`
shows the probe budget.

**Performance, measured on 3,689 games / 230k plies:** about 1.5 s per query, pre-optimization numbers in
parentheses. The shared per-move view runs windows over every ply of the player's games. Three fixes:
- **An optimization fence (`OFFSET 0`) on the games subquery:** once the planner flattened it, the
  `Termination` regex ran over the full PGN of every *move*, not every game (6.7 s → 2.1 s).
- **Expected scores computed after the player filter:** the windows stop Postgres pushing the filter down.
- **Five `lead()`s under GREATEST/LEAST for the material window,** and a reverse running window for "the
  rest of the game". A frame ending at `UNBOUNDED FOLLOWING`, or a sliding max, is recomputed per row.

### Underpromotion: two separate questions

`UNDERPROMOTION` stays a Layer 1 event. The engine layer answers two different questions about it, each
with its own search (`engine_move_probes`, one row per kind), and never lets one stand in for the other:

- **A. Was the underpromotion the best move in the position?** (`kind = 'all_moves'`) One search from the
  position before the move with `searchmoves` = **every legal move** and MultiPV = their number, so every
  root move is scored in the same search iteration. Budget: see *Probe budget* below.
  - `is_best_move`: the played move scores at least as well as **every** legal move.
  - `tied_for_best_move`: best, and some other move scores exactly the same. For example, if `=Q#` and `=R#`
    both mate in 1, the rook underpromotion is tied, not unique.
  - `unique_best_move`: best, and strictly better than every other legal move.
  - `played_move_rank`: 1 + the number of moves scoring strictly better.
  - If the search didn't return a score for every legal move, all four are `null`. Never guess: an unscored
    move might be better.
- **B. Was underpromoting better than queening on the same square?** (`kind = 'vs_queen'`) One search
  over exactly {played underpromotion, queen promotion}, MultiPV 2, same budget rule. Gives
  `better_than_queen` (strictly better; equal is not better) plus both evaluations. It supports statements
  like "queening here would have thrown away the win".

Why A needs every legal move: an unrestricted search's first choice is not ground truth. In the
Saavedra-style test position, a 20k-node unrestricted search picked `Kd3` (+7.2), while scoring all 10
legal moves shows `g8=R` mates in 2 (and `Kc3` mates in 5). Comparing only against the engine's pick, or
against a few candidates, could both wrongly deny and wrongly grant "best". For ordinary moves (no probe),
events carry only `matches_engine_choice` (the played move is the unrestricted search's first choice) and
`rank_in_engine_lines` (within MultiPV lines, if any), and never `is_best_move`.

Probes run inside the same per-game task, after the game's positions, each with the hash cleared, so the
position results are identical whether or not probes ran. A probe that fails skips only itself: the
game's evaluations are kept, and `verify-underpromotions` retries the missing probe.

**Probe budget.** Every line of a probe must come from the same completed search iteration, otherwise
their scores aren't comparable. So a probe targets a depth (the position's own analysis depth for node
configs, or the config's depth, capped at 30) **and** carries a node ceiling of 20M × lines. The ceiling is
deterministic under Threads = 1 with a cleared hash. The result is the deepest iteration in which every line
reported an exact score (fail-high/low bounds don't count). The budget records all three, e.g.
`{"depth": 22, "nodes_cap": 280000000, "completed_depth": 21}`, and `completed_depth < depth` says the
ceiling cut it short.

Where the position's own analysis ended on a proven mate, its reported depth (up to 245) is not an
effort measure. Once a mate is proven the tree collapses and Stockfish reports huge depths almost for
free. So two-line probes of mate positions target `PROBE_MATE_DEPTH` = 20, deeper than 99% of non-mate
positions reach at 25k nodes.

Measured: inheriting the cap of 30 sent the runner-up line of ~17.5k mate positions to the node ceiling,
about 10 s each. The whole probe pass ran at 4 probes/s instead of 75. If no iteration covers every line, the probe fails and nothing is stored. A
wall-clock watchdog (15 min) exists only for a stuck engine: when it fires, the probe fails and is retried
next run. It is never a search limit, so no stored result depends on time. `verify-*` commands return every
failed probe with its game, position and kind.

**The depth-22 probe stall (found 2026-09).**
- **What happened:** the depth-22 `all_moves` probe for game 3658, position 104 (White to move, before
  `h8=B+`) ran for over two hours and never finished. The position is
  `8/7P/8/4k1p1/8/2r1P1P1/5PK1/8 w - - 0 53`, with 14 legal moves.
- **Not the cause:** a missing depth limit. `go depth 22` reached Stockfish, and every completed search
  reported exactly the requested depth.
- **Measured, MultiPV 14:**

  | Depth | Time | Nodes | Top line |
  |---|---|---|---|
  | 16 | 1.0 s | 2.8M | |
  | 18 | 2.5 s | 8.1M | |
  | 20 | 6.7 s | 25M | becomes a mate (#+19) |
  | 21 | 29.5 s | 146M | |

  At depth 22, lines 1–9 finished by 37 s (180M nodes). Line 10 then went on for hours; a streamed rerun
  reported nothing more in 5.5 further minutes.
- **Each move searched alone at depth 22** (h8=N, Kf1, Kg1, e4, Kh1, Kf3) took 0.4–1.9 s (1–5M nodes).
- **The cause:** interaction between the lines of one MultiPV search. All 14 root moves share one search
  and one hash table. Once the top line is a long forced mate in a rook-and-pawn endgame, huge decisive
  scores leak into the searches of the losing moves. Their scores become unstable: g4 is −5.5 alone but
  −53.48 inside the 14-line search, and scores in the thousands (e.g. −21.65) appear where the moves alone
  score about −5. Each further iteration costs several times the last: ×2.7 from depth 20 to 21, then
  unbounded at 22.
- **What that means:** a depth limit bounds the number of iterations, not the work inside one iteration.
- **The fix is the node ceiling above, not a lower depth.** The probe now finishes in 52 s at a completed
  depth of 21 with all 14 lines, recorded as such.
- **Regression tests** (`tests/test_engine.py`): the real position with a small ceiling is bounded, keeps
  one whole iteration, and is reproducible run to run. A scripted stream checks that a search cut
  mid-iteration keeps the previous whole iteration and that fail-high bounds are ignored, that a search
  with no whole iteration fails rather than guesses, and that the watchdog fails a stuck engine.
- **Still open:** the unstable scores deep in a 14-line search are also a correctness caveat. They matter
  for the rank of a bad move and for how far behind it is, not for which move is best. Verdicts that
  depend on a line far down the list should be read with that in mind.

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
                     "all_moves": {"legal_moves": 10, "scored": 10, "budget": {"depth": 11, "nodes_cap": 200000000, "completed_depth": 11},
                                   "evaluation": {"cp": 486}, "best_moves": ["e7e8n"],
                                   "best_evaluation": {"cp": 486}},
                     "better_than_queen": true,
                     "vs_queen": {"evaluation": {"cp": 404}, "queen_promotion_evaluation": {"cp": 0},
                                  "budget": {"depth": 11, "nodes_cap": 40000000, "completed_depth": 11}}}}
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

## Browser engine (built, opt-in)

The native engine index (above) is for ChessTrove's own corpus. For public players, the server does
imports and the deterministic analysis, and Stockfish runs in the visitor's browser. It only runs when the
visitor asks: a player page shows the whole collection and named mates without any engine, and the record
book starts as an offer ("Analyze my games").

**Engine.** stockfish.js 18.0.8, the "lite single-threaded" WASM build (`web/public/engine/`, 7.3 MB, GPLv3).
- It is the same Stockfish version as the native index, but a smaller network and a different build, so it
  is its own engine configuration, never mixed with native results.
- It is single-threaded, so it needs no SharedArrayBuffer, COOP/COEP or pthreads. It scales by running several
  independent Web Workers instead.
- Each search sends the game's full move history (`position ... moves ...`), so repetitions and the 50-move
  counter count, exactly as natively; positions are never deduplicated across games. `ucinewgame` runs per
  game, the hash is 16 MB, and scores are stored from White's point of view (`web/src/engine/uci.ts`).

**What runs, in what order** (`runner.ts`):
1. Games with deterministic events.
2. The 100 most recent games.
3. Everything else, newest first.

The baseline is one node-limited line per position. Probes are the expensive searches, and they run only on
candidates a finished game's baseline turns up, ahead of further baseline games:
- **top_two** where the player played the engine's choice in a clearly winning position (the native
  ONLY_WINNING_MOVE candidate rule);
- **vs_queen** and **all_moves** on the player's own underpromotions.

A probe keeps the deepest iteration every line finished, like `engine.run_probe`. It aims at the position's
baseline depth (20 when the baseline found a mate), capped at 30, with a ceiling of 1M nodes per line.

**Where results live.** Results stay in the visitor's IndexedDB and are never uploaded (option A: the server
stores games, events and imports; the browser owns evaluations and discoveries).
- Each finished game or probe is written at once, so a closed tab loses at most the game in flight. The
  probes still to run are stored too.
- Every key starts with the config identity: engine, build, flavour, nodes, MultiPV, hash and the probe
  settings. Change any of them and old results are simply not read.
- Worker count only affects speed (each worker is an independent single-threaded search), so it is not part
  of the identity.
- A later **server-verified** record book fits this model: re-search only the few record candidates
  natively and store them under a native config. That would be the `client_computed` vs `server_verified`
  distinction; it is not built.

**Discoveries** (`archaeology.ts`) are the six record-book types, ported from `archaeology.py` and the SQL
behind it.
- `scripts/engine_parity.py` feeds the native results through the port and compares it with the server. It
  matches on all 5,511 non-Chess960 games of the three local accounts: the same games, plies, order and
  values, with exact ties allowed to swap (Postgres computes `exp()` in numeric, the browser in doubles).
- Chess960 games are skipped for now.

**Workers.** Balanced 2, Fast min(4, cores − 1), Max cores − 1. A constrained device gets one worker whatever
the setting: ≤ 4 cores, ≤ 4 GB `deviceMemory`, or a coarse pointer.

**Resuming.** Once a visitor has opted in on a browser, it resumes by itself on the next visit, unless they
paused it or the device is constrained; then it waits for Resume.

**Failures.** A browser without WebAssembly, Workers or IndexedDB gets a note, and an engine failure shows
an error in the record book. The collection is unaffected either way.

**Benchmark** (`/lab/engine`, fixture from `scripts/engine_fixture.py`: 106 of danksonpotato's games,
8,357 positions, with the native results). Open it on any machine; `?auto=1` leaves the report in
`window.__bench`. Measured in Chrome on the development machine (Apple Silicon, 14 cores).

Speed, in positions a second:

| Nodes | 1 worker | 2 workers | 4 workers |
|---|---|---|---|
| 10k | 191 | 348 | 513 (671 on the full fixture) |
| 25k | 81 | 138 | 215 (280 on the full fixture) |

Worker startup takes about 110 ms each (the WASM is cached after the first), and the main thread never
stalled more than 2 ms.

Agreement with native Stockfish 18 at 25k nodes, on all 8,304 non-terminal fixture positions:

| | 10k browser | 25k browser |
|---|---|---|
| Same best move | 64.1% | 67.6% |
| Same broad evaluation (White better / balanced / Black better) | 96.6% | 96.6% |
| Native forced mates found, same side | 77.2% | 87.6% |
| Throws ≥ 0.30, recall / precision | 82.5 / 95.4 | 86.5 / 95.6 |
| Comebacks from ≤ 0.10, recall / precision | 90.0 / 96.4 | 93.3 / 96.6 |
| Only winning moves (with probes), recall / precision | 80 / 100 | 80 / 100 |
| Sound sacrifices, recall / precision | 62.5 / 100 | 62.5 / 100 |
| Forced-mate runs, recall / precision | 56 / 64 | 56 / 58 |

**Default: 25k nodes, 2 workers (Balanced).**
- Precision is the same at 10k and 25k, so the browser rarely claims what native analysis doesn't. 25k finds
  more: 88% of forced mates against 77%, and more throws and comebacks.
- At 2 workers it still leaves most of the machine free. At about 75 positions a game, and roughly 110–138
  positions a second measured on this machine at Balanced, 100 games take about a minute, 1,000 games 9–11
  minutes and 5,000 games 45–57 minutes. A constrained device on one worker takes 2–3 times as long; its
  exact speed is unmeasured.
- Forced-mate runs agree least. A mate's length depends on how deep the search looks, so a different network
  finds different runs. That is why browser results carry their own label and never pass for native ones.

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
