-- ChessTrove schema. Idempotent: safe to run on every startup.
-- Raw data (games, moves) never depends on derived data (analysis_runs, events),
-- so detectors can be re-run over stored games without re-importing.

CREATE TABLE IF NOT EXISTS users (
    id          bigserial PRIMARY KEY,
    username    text NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chess_accounts (
    id          bigserial PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users ON DELETE CASCADE,
    platform    text NOT NULL CHECK (platform IN ('chesscom', 'lichess')),
    username    text NOT NULL,
    UNIQUE (platform, username)
);

CREATE TABLE IF NOT EXISTS imports (
    id               bigserial PRIMARY KEY,
    account_id       bigint REFERENCES chess_accounts ON DELETE SET NULL,  -- NULL for file imports
    source           text NOT NULL CHECK (source IN ('pgn', 'chesscom', 'lichess')),
    source_ref       text NOT NULL,           -- file name or platform username
    status           text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed')),
    resume_state     jsonb NOT NULL DEFAULT '{}',  -- e.g. Chess.com archive months already fetched
    games_seen       int NOT NULL DEFAULT 0,
    games_imported   int NOT NULL DEFAULT 0,
    games_duplicate  int NOT NULL DEFAULT 0,
    games_failed     int NOT NULL DEFAULT 0,
    games_skipped    int NOT NULL DEFAULT 0,       -- unsupported variants; intentional, not errors
    errors           jsonb NOT NULL DEFAULT '[]',   -- [{index, error}], capped
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz
);

-- Migrations for databases created before a column existed (CREATE TABLE IF NOT EXISTS won't add it).
ALTER TABLE imports ADD COLUMN IF NOT EXISTS games_skipped int NOT NULL DEFAULT 0;
-- what the platform says the player has played, fetched when an account import starts (NULL: unknown)
ALTER TABLE imports ADD COLUMN IF NOT EXISTS games_expected int;
-- the platform's current rating in the player's most-played time control, fetched with games_expected
ALTER TABLE imports ADD COLUMN IF NOT EXISTS player_rating int;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS rating_mode text;

CREATE TABLE IF NOT EXISTS games (
    id            bigserial PRIMARY KEY,
    source_key    text NOT NULL UNIQUE,     -- dedupe key; see CanonicalGame.source_key
    source        text NOT NULL CHECK (source IN ('pgn', 'chesscom', 'lichess')),
    external_id   text,
    import_id     bigint REFERENCES imports ON DELETE SET NULL,
    played_at     timestamptz,
    white         text,
    black         text,
    white_rating  int,
    black_rating  int,
    result        text NOT NULL,
    time_control  text,
    rated         boolean,
    eco           text,
    opening       text,
    initial_fen   text,                     -- NULL = standard start
    chess960      boolean NOT NULL DEFAULT false,
    ply_count     int NOT NULL,
    pgn           text NOT NULL,            -- raw, as received
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS games_played_at ON games (played_at);
CREATE INDEX IF NOT EXISTS games_white ON games (lower(white));
CREATE INDEX IF NOT EXISTS games_black ON games (lower(black));

-- One row per mainline ply. fen_before of ply n is fen_after of ply n-1 (or games.initial_fen).
-- A game's moves, one row per game: the move-by-move facts packed ply by ply (string position i / array
-- element i+1 = ply i+1). Positions (FENs) aren't stored; they're replayed from uci when needed
-- (db.positions, ~0.3 ms a game). One row per ply with a FEN each cost ~13 KB a game; this is ~2 KB.
CREATE TABLE IF NOT EXISTS game_moves (
    game_id            bigint PRIMARY KEY REFERENCES games ON DELETE CASCADE,
    first_color        char(1) NOT NULL CHECK (first_color IN ('w', 'b')),  -- who played ply 1
    uci                text NOT NULL,        -- space-separated
    san                text NOT NULL,        -- space-separated
    piece              text NOT NULL,        -- one letter per ply: P N B R Q K
    captured           text NOT NULL,        -- one letter per ply, '.' for none
    promotion          text NOT NULL,        -- one letter per ply, '.' for none
    flags              smallint[] NOT NULL,  -- 1 check, 2 checkmate, 4 castling, 8 en passant
    material_white     smallint[] NOT NULL,  -- after the ply, P=1 N=3 B=3 R=5 Q=9
    material_black     smallint[] NOT NULL,
    queens_after       smallint[] NOT NULL,
    legal_moves_before smallint[] NOT NULL
);

-- The old one-row-per-ply shape, unpacked on the fly, so queries written against it keep working. Transitional:
-- hot paths should read game_moves directly (see engine_input). Created once the old `moves` table is gone
-- (`chesstrove compact-moves` converts it).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'moves' AND relkind = 'r'
                 AND relnamespace = current_schema()::regnamespace) THEN
    CREATE OR REPLACE VIEW moves AS
      SELECT gm.game_id, m.ply::int AS ply,
             (CASE WHEN (m.ply % 2 = 1) = (gm.first_color = 'w') THEN 'w' ELSE 'b' END)::char(1) AS color,
             m.san, m.uci, m.piece::char(1) AS piece,
             nullif(m.captured, '.')::char(1) AS captured, nullif(m.promotion, '.')::char(1) AS promotion,
             (m.flags & 1) <> 0 AS is_check, (m.flags & 2) <> 0 AS is_checkmate,
             (m.flags & 4) <> 0 AS is_castling, (m.flags & 8) <> 0 AS is_en_passant,
             m.material_white, m.material_black, m.queens_after, m.legal_moves_before
      FROM game_moves gm
      CROSS JOIN LATERAL unnest(string_to_array(gm.uci, ' '), string_to_array(gm.san, ' '),
                                string_to_array(gm.piece, NULL), string_to_array(gm.captured, NULL),
                                string_to_array(gm.promotion, NULL), gm.flags, gm.material_white,
                                gm.material_black, gm.queens_after, gm.legal_moves_before)
        WITH ORDINALITY AS m(uci, san, piece, captured, promotion, flags, material_white, material_black,
                             queens_after, legal_moves_before, ply);
  END IF;
END $$;

-- Derived data: everything below can be deleted and rebuilt with `chesstrove reanalyze --all`.
CREATE TABLE IF NOT EXISTS analysis_runs (
    id                 bigserial PRIMARY KEY,
    detector_versions  jsonb NOT NULL,      -- {"UNDERPROMOTION": 1, ...}
    status             text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed')),
    games_processed    int NOT NULL DEFAULT 0,
    events_created     int NOT NULL DEFAULT 0,
    started_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz
);
-- a browser's deep-pass session (browser_import.start_deep): {client, platform, username, token_sha256, last_seen}
ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS session jsonb;

CREATE TABLE IF NOT EXISTS events (
    id                bigserial PRIMARY KEY,
    game_id           bigint NOT NULL REFERENCES games ON DELETE CASCADE,
    ply               int NOT NULL,
    type              text NOT NULL,
    detector_id       text NOT NULL,
    detector_version  int NOT NULL,
    color             char(1) NOT NULL CHECK (color IN ('w', 'b')),
    fen               text NOT NULL,            -- position before the move
    metadata          jsonb NOT NULL DEFAULT '{}',
    analysis_run_id   bigint REFERENCES analysis_runs ON DELETE SET NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (game_id, ply, type, detector_id)    -- re-running a detector replaces, never duplicates
);
CREATE INDEX IF NOT EXISTS events_type ON events (type, color);
-- Named-mate forms were stored as textbook / canonical / variant; the middle tier is now "characteristic".
-- The detector itself didn't change, so this relabels in place instead of replaying every game.
UPDATE events SET metadata = jsonb_set(metadata, '{form}', '"characteristic"') WHERE metadata->>'form' = 'canonical';

-- Which detector versions have seen each game, so re-runs only replay stale games.
-- A game is up to date for a set of detectors iff detector_versions @> '{"ID": version, ...}'.
CREATE TABLE IF NOT EXISTS game_analysis (
    game_id            bigint PRIMARY KEY REFERENCES games ON DELETE CASCADE,
    detector_versions  jsonb NOT NULL DEFAULT '{}',
    analyzed_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_detector ON events (detector_id, detector_version);

-- ================================================================================================
-- Layer 2: engine-analysis index. Reads games/moves, never writes them. See ARCHITECTURE.md.
-- ================================================================================================

-- The identity of a reproducible analysis setting. Results are only comparable within one config.
CREATE TABLE IF NOT EXISTS engine_configs (
    id              bigserial PRIMARY KEY,
    engine_name     text NOT NULL,            -- as the binary reports it over UCI, e.g. 'Stockfish 18'
    limit_kind      text NOT NULL CHECK (limit_kind IN ('nodes', 'depth')),  -- never time: not reproducible
    limit_value     int NOT NULL CHECK (limit_value > 0),
    multipv         int NOT NULL DEFAULT 1 CHECK (multipv >= 1),
    threads         int NOT NULL DEFAULT 1 CHECK (threads >= 1),
    hash_mb         int NOT NULL,
    UNIQUE (engine_name, limit_kind, limit_value, multipv, threads, hash_mb)
);

CREATE TABLE IF NOT EXISTS engine_runs (
    id               bigserial PRIMARY KEY,
    config_id        bigint NOT NULL REFERENCES engine_configs ON DELETE CASCADE,
    status           text NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    workers          int NOT NULL DEFAULT 1,
    binary_path      text,
    binary_sha256    text,                    -- metadata only; not part of the config identity
    games_total      int NOT NULL DEFAULT 0,  -- pending when the run started
    games_done       int NOT NULL DEFAULT 0,
    games_failed     int NOT NULL DEFAULT 0,
    positions_done   int NOT NULL DEFAULT 0,
    engine_seconds   double precision NOT NULL DEFAULT 0,  -- wall time spent inside searches
    errors           jsonb NOT NULL DEFAULT '[]',
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz
);

-- One row per (config, game) once every position of the game is stored: the unit of completion/resume.
CREATE TABLE IF NOT EXISTS engine_game_status (
    config_id     bigint NOT NULL REFERENCES engine_configs ON DELETE CASCADE,
    game_id       bigint NOT NULL REFERENCES games ON DELETE CASCADE,
    run_id        bigint REFERENCES engine_runs ON DELETE SET NULL,
    positions     int NOT NULL,
    completed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (config_id, game_id)
);

-- Position k = the position after k plies (0 = start). Scores are from White's point of view.
-- Exactly one of score_cp / mate is set. mate = 0 means the side to move is checkmated (no search run).
CREATE TABLE IF NOT EXISTS engine_positions (
    config_id   bigint NOT NULL REFERENCES engine_configs ON DELETE CASCADE,
    game_id     bigint NOT NULL REFERENCES games ON DELETE CASCADE,
    position    int NOT NULL,
    score_cp    int,
    mate        int,
    wdl         smallint[],                   -- {win, draw, loss} per mille, White's POV
    best_uci    text,                         -- NULL in terminal positions
    pv_uci      text[],                       -- capped
    multipv     jsonb,                        -- [{uci, score_cp, mate}] when multipv > 1
    depth       int,
    seldepth    int,
    nodes       bigint,
    PRIMARY KEY (config_id, game_id, position),
    CHECK ((score_cp IS NULL) <> (mate IS NULL))
);

-- One search that scores every move in `moves` (searchmoves + MultiPV = all of them), so the scores
-- compare directly. kind = 'vs_queen' (underpromotion vs. queening) or 'all_moves' (every legal move:
-- the only basis for saying a move was the best one).
CREATE TABLE IF NOT EXISTS engine_move_probes (
    config_id   bigint NOT NULL REFERENCES engine_configs ON DELETE CASCADE,
    game_id     bigint NOT NULL REFERENCES games ON DELETE CASCADE,
    position    int NOT NULL,                 -- searched from here (the position before the move)
    kind        text NOT NULL,
    moves       text[] NOT NULL,              -- the only root moves considered
    results     jsonb NOT NULL,               -- ranked [{uci, score_cp, mate}], White's POV
    budget      jsonb NOT NULL,               -- {"nodes": total} or {"depth": d}, as searched
    PRIMARY KEY (config_id, game_id, position, moves)
);
ALTER TABLE engine_move_probes ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'vs_queen';
ALTER TABLE engine_move_probes ADD COLUMN IF NOT EXISTS budget jsonb NOT NULL DEFAULT '{}';
