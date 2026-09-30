// Thin typed client for the FastAPI backend (/api/*).

export type Platform = "chesscom" | "lichess";
export const PLATFORM_NAME: Record<Platform, string> = { chesscom: "Chess.com", lichess: "Lichess" };

export interface Motif { type: string; mine: number; against: number; forms?: Partial<Record<"textbook" | "canonical" | "variant", number>> }
export interface Import {
  id: number; status: "running" | "completed" | "failed"; finished_at: string | null; games_expected: number | null; games_seen: number; games_imported: number;
  games_duplicate: number; games_failed: number; games_skipped: number; errors: { error: string }[];
}
export interface PlayerSummary {
  platform: Platform; username: string; display_name: string | null; games: number; positions: number;
  first_game: string | null; last_game: string | null; wins: number; draws: number; losses: number;
  rating: number | null; rating_mode?: string | null; motifs: Motif[];
  best_underpromotions?: { total: number; judged: number; mine: number; against: number; found: string[] } | null;
  engine: { games_analyzed: number; positions_analyzed: number; config: { engine_name: string; limit_kind: string; limit_value: number } } | null;
  latest_import: Import | null;
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, init);
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

const qs = (params: Record<string, string | number | boolean | undefined>) =>
  new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined).map(([k, v]) => [k, String(v)])).toString();

export const api = {
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
