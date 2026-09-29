// Thin typed client for the FastAPI backend (/api/*).

export type Platform = "chesscom" | "lichess";
export const PLATFORM_NAME: Record<Platform, string> = { chesscom: "Chess.com", lichess: "Lichess" };

export interface Motif { type: string; mine: number; against: number }
export interface Import {
  id: number; status: "running" | "completed" | "failed"; games_seen: number; games_imported: number;
  games_duplicate: number; games_failed: number; games_skipped: number; errors: { error: string }[];
}
export interface PlayerSummary {
  platform: Platform; username: string; display_name: string | null; games: number; positions: number;
  first_game: string | null; last_game: string | null; wins: number; draws: number; losses: number;
  rating: number | null; motifs: Motif[];
  engine: { games_analyzed: number; positions_analyzed: number; config: { engine_name: string; limit_kind: string; limit_value: number } } | null;
  latest_import: Import | null;
}
export interface EventRow {
  id: number; game_id: number; ply: number; type: string; color: "w" | "b"; fen: string; fen_after: string;
  san: string; uci: string; metadata: Record<string, unknown>; played_at: string | null; white: string;
  black: string; result: string; time_control: string | null;
}
export interface LabelRow {
  type: string; game_id: number; ply: number; color: "w" | "b"; san: string; uci: string; engine_choice: string | null;
  expected_before: number; expected_after: number; expected_drop: number; runner_up?: string;
  best_line?: number; runner_up_line?: number; played_at: string | null; white: string; black: string;
  result: string; fen_before: string | null; fen_after: string; initial_fen: string | null;
  is_capture: boolean; is_check: boolean;
  cp_before: number | null; mate_before: number | null; cp_after: number | null; mate_after: number | null;
}
export interface Move { ply: number; color: "w" | "b"; san: string; uci: string; fen_after: string; is_check: boolean }
export interface EnginePosition { position: number; score_cp: number | null; mate: number | null; best_uci: string | null }
export interface GameDetail {
  id: number; source_key: string; external_id: string | null; played_at: string | null; white: string; black: string;
  white_rating: number | null; black_rating: number | null; result: string; time_control: string | null;
  eco: string | null; opening: string | null; initial_fen: string | null; pgn: string; moves: Move[];
  events: EventRow[]; engine_positions?: EnginePosition[];
}

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
  labels: (p: Platform, u: string, type: string, limit = 12) =>
    request<LabelRow[]>(`/engine-labels?${qs({ platform: p, player: u, type, limit })}`),
  game: (id: string) => request<GameDetail>(`/games/${id}?engine=true`),
};

export function sourceUrl(g: { source_key: string; external_id: string | null; pgn?: string }): string | null {
  const [platform] = g.source_key.split(":");
  if (platform === "lichess" && g.external_id) return `https://lichess.org/${g.external_id}`;
  const link = g.pgn?.match(/\[Link "([^"]+)"\]/)?.[1];
  if (link) return link;
  return platform === "chesscom" && g.external_id ? `https://www.chess.com/game/${g.external_id}` : null;
}
