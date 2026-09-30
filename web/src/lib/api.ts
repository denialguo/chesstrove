// Thin typed client for the FastAPI backend (/api/*).

/** `base` + /api + path. An empty base means the page's own origin (local dev: Vite proxies /api). */
export const joinApi = (base: string, path: string) => `${base.replace(/\/+$/, "")}/api${path}`;
// Production builds set VITE_API_URL=https://api.chesstrove.tech; it's public config, never a secret.
const API_BASE: string = import.meta.env.VITE_API_URL ?? "";
export const apiUrl = (path: string) => joinApi(API_BASE, path);

export type Platform = "chesscom" | "lichess";
export const PLATFORM_NAME: Record<Platform, string> = { chesscom: "Chess.com", lichess: "Lichess" };

export interface Motif { type: string; mine: number; against: number; forms?: Partial<Record<"textbook" | "characteristic" | "variant", number>> }
export interface Import {
  id: number; status: "running" | "completed" | "failed"; finished_at: string | null; games_expected: number | null; games_seen: number; games_imported: number;
  games_duplicate: number; games_failed: number; games_skipped: number; errors: { error: string }[];
  /** Browser indexing sessions: client "browser" and when the last batch arrived. */
  resume_state?: { client?: string; last_seen?: string; months_done?: string[] } | null;
}
export interface PlayerSummary {
  platform: Platform; username: string; display_name: string | null; games: number; positions: number;
  first_game: string | null; last_game: string | null; wins: number; draws: number; losses: number;
  rating: number | null; rating_mode?: string | null; motifs: Motif[];
  /** Distinct moves matching at least one rare pattern (named mates in textbook or characteristic form; variants are left out), per side; `types` counts
   *  the labels on those moves, so they overlap. */
  rare_moments: { mine: number; against: number; types: { type: string; mine: number; against: number }[] };
  best_underpromotions?: { total: number; judged: number; mine: number; against: number; found: string[] } | null;
  engine: { games_analyzed: number; positions_analyzed: number; config: { engine_name: string; limit_kind: string; limit_value: number } } | null;
  latest_import: Import | null;
  /** Stored games the deep pass (missed mates in one) hasn't seen yet. */
  deep_pending?: number;
}
export interface EventRow {
  id: number; game_id: number; ply: number; type: string; color: "w" | "b"; fen: string; fen_after: string;
  san: string; uci: string; metadata: Record<string, unknown>; played_at: string | null; white: string;
  black: string; result: string; time_control: string | null;
}
export interface Move { ply: number; color: "w" | "b"; san: string; uci: string; fen_after: string; is_check: boolean }
export interface EnginePosition { position: number; score_cp: number | null; mate: number | null; best_uci: string | null }
export interface GameDetail {
  id: number; source_key: string; external_id: string | null; played_at: string | null; white: string; black: string;
  white_rating: number | null; black_rating: number | null; result: string; time_control: string | null;
  eco: string | null; opening: string | null; initial_fen: string | null; pgn: string; moves: Move[];
  events: EventRow[]; engine_positions?: EnginePosition[];
}

/** An evaluation from the player's side: {cp: 250} / {mate: 3} mates in 3 / {mate: -2} mated in 2 / {mate: 0} mate on the board. */
export type Eval = { cp: number } | { mate: number };
export type DiscoveryType = "biggest_throw" | "biggest_comeback" | "lost_advantage" | "only_winning_move" | "underpromotion"
  | "material_sacrifice" | "missed_forced_mate" | "longest_mate_found" | "only_move_keeping_mate" | "unusual_move";
export interface Discovery {
  game: { id: number; played_at: string | null; white: string; black: string; result: string; termination?: string | null;
          platform: string; source_key: string; external_id: string | null };
  ply: number; color: "w" | "b"; player: string; opponent?: string;
  move: { san: string; uci: string; by?: "w" | "b" } | null;
  fen?: string; fen_before?: string; fen_after?: string;
  eval?: Eval; eval_before?: Eval; eval_after?: Eval; expected?: number;
  engine_choice?: { uci: string | null; san: string | null };
  comparison?: { best_line: { uci: string; eval: Eval }; runner_up: { uci: string; san: string | null; eval: Eval; expected: number } };
  move_class?: { quiet: boolean; capture: boolean; check: boolean; retreat: boolean; promotion: string | null;
                 sacrifice: { kind: string } | null };
  sacrifice?: { kind: "queen" | "rook" | "exchange"; reply: string; deficit: number; ends_in_mate: boolean; never_recovered: boolean };
  run?: { moves: number; engine_mate_in_at_start: number; mating_ply: number };
  best_move?: "unique_best" | "tied_best" | "not_best" | "unknown";
  vs_queen?: "better" | "equal" | "worse" | "unknown";
  played_move_rank?: number | null; legal_moves?: number | null;
  best_moves?: { uci: string; san: string | null }[];
  queen_promotion_evaluation?: Eval | null; evaluation?: Eval | null;
  engine?: { id: number; engine: string; nodes?: number; depth?: number }; // server results only
  score: { name: string; value: number | string };
}
export interface Discoveries { type: DiscoveryType; config: Discovery["engine"] | null; results: Discovery[] }

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

/** fetch that waits out a sleeping API server. Render's free instance takes up to a minute to wake, and meanwhile
 *  can drop the connection or answer 502/503/504: those are retried with backoff for about two minutes, while the page
 *  says it's waking the server. Anything else (a 404, 422, 429, or a 500 from a real bug) comes straight back. */
export async function wakeFetch(url: string, init?: RequestInit, get: typeof fetch = fetch,
                                sleep = (ms: number) => new Promise((r) => setTimeout(r, ms)), budgetMs = 120_000): Promise<Response> {
  let wait = 1000, spent = 0;
  for (;;) {
    try {
      const r = await get(url, init);
      if (![502, 503, 504].includes(r.status)) return r;
      if (spent >= budgetMs) return r;
    } catch (e) {
      if (spent >= budgetMs) throw e;
    }
    await sleep(wait);
    spent += wait;
    wait = Math.min(wait * 2, 15_000);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await wakeFetch(apiUrl(path), init);
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

const qs = (params: Record<string, string | number | boolean | undefined>) =>
  new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined).map(([k, v]) => [k, String(v)])).toString();

export type IndexingSession =
  | { mode: "index"; import_id: number; token: string; months_done: string[]; versions: Record<string, number>; max_games: number }
  | { mode: "watch" | "done"; import_id: number };

export type DeepSession =
  | { mode: "index"; run_id: number; token: string; pending: number; versions: Record<string, number> }
  | { mode: "watch"; run_id: number; pending: number } | { mode: "done"; pending: 0 };

export const api = {
  /** Start (or carry on with) the deep pass over a player's stored games. */
  startDeep: (username: string, resume?: { run_id: number; token: string }) =>
    request<DeepSession>("/indexing/chesscom/deep", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, ...resume }),
    }),
  /** Start (or carry on with) indexing a Chess.com history in this browser (web/src/indexer). */
  startIndexing: (username: string, resume?: { import_id: number; token: string }) =>
    request<IndexingSession>("/indexing/chesscom", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, ...resume }),
    }),
  player: (p: Platform, u: string) => request<PlayerSummary>(`/players/${p}/${encodeURIComponent(u)}`),
  startImport: (p: Platform, u: string) =>
    request<{ import_id: number }>(`/imports/${p}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: u }),
    }),
  events: (p: Platform, u: string, type: string, limit = 24) =>
    request<EventRow[]>(`/events?${qs({ platform: p, player: u, type, limit })}`),
  eventsAgainst: (p: Platform, u: string, type: string, limit = 24) =>
    request<EventRow[]>(`/events?${qs({ platform: p, against: u, type, limit })}`),
  game: (id: string) => request<GameDetail>(`/games/${id}?engine=true`),
  discoveries: (p: Platform, u: string, type: DiscoveryType, limit = 6) =>
    request<Discoveries>(`/engine-discoveries?${qs({ platform: p, player: u, type, limit })}`),
};

export function sourceUrl(g: { source_key: string; external_id: string | null; pgn?: string }): string | null {
  const [platform] = g.source_key.split(":");
  if (platform === "lichess" && g.external_id) return `https://lichess.org/${g.external_id}`;
  const link = g.pgn?.match(/\[Link "([^"]+)"\]/)?.[1];
  if (link) return link;
  return platform === "chesscom" && g.external_id ? `https://www.chess.com/game/${g.external_id}` : null;
}
