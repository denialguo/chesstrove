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
    errors           jsonb NOT NULL DEFAULT '[]',   -- [{index, error}], capped
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz
);

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
CREATE TABLE IF NOT EXISTS moves (
    game_id           bigint NOT NULL REFERENCES games ON DELETE CASCADE,
    ply               int NOT NULL,
    color             char(1) NOT NULL CHECK (color IN ('w', 'b')),
    san               text NOT NULL,
    uci               text NOT NULL,
    piece             char(1) NOT NULL,
    captured          char(1),
    promotion         char(1),
    is_check          boolean NOT NULL,
    is_checkmate      boolean NOT NULL,
    is_castling       boolean NOT NULL,
    is_en_passant     boolean NOT NULL,
    fen_after         text NOT NULL,
    material_white    smallint NOT NULL,
    material_black    smallint NOT NULL,
    queens_after      smallint NOT NULL,
    legal_moves_before smallint NOT NULL,
    PRIMARY KEY (game_id, ply)
);

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

-- Which detector versions have seen each game, so re-runs only replay stale games.
-- A game is up to date for a set of detectors iff detector_versions @> '{"ID": version, ...}'.
CREATE TABLE IF NOT EXISTS game_analysis (
    game_id            bigint PRIMARY KEY REFERENCES games ON DELETE CASCADE,
    detector_versions  jsonb NOT NULL DEFAULT '{}',
    analyzed_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_detector ON events (detector_id, detector_version);
