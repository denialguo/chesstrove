// The 'Best-move underpromotion' check: Stockfish (in this browser) scores every legal move at each of a
// player's underpromotions, both sides', and says whether the promotion was the single best move. It's the
// all_moves probe of the full analysis on its own: a few searches instead of every position, so it runs on
// one click. Each verdict is saved as soon as it's known.

import { Chess } from "chessops/chess";
import { parseFen } from "chessops/fen";
import { api, type Platform } from "../lib/api";
import { bestMoveVerdict, type BestMove } from "./archaeology";
import { BASELINE_NODES, CONFIG, workersFor } from "./runner";
import { playerKey, store } from "./store";
import { UciEngine } from "./uci";

export interface UpVerdict { key: string; mine: boolean; best: BestMove; rank: number | null; legal: number }

const PROBE = { nodes_per_line: 1_000_000, max_depth: 30, mate_depth: 20 }; // as the full analysis (runner.ts)
const prefix = (platform: string, user: string) => playerKey(`${CONFIG}|upcheck-v3`, platform, user);

function legalMoves(fen: string): number {
  const pos = Chess.fromSetup(parseFen(fen).unwrap()).unwrap();
  let count = 0;
  for (const [from, dests] of pos.allDests()) {
    const pawn = pos.board.get(from)?.role === "pawn";
    for (const to of dests) count += pawn && (to >> 3 === 0 || to >> 3 === 7) ? 4 : 1;
  }
  return count;
}

export const savedVerdicts = (platform: string, user: string) => store.items<UpVerdict>(prefix(platform, user));

/** The player's underpromotions (theirs and their opponents'). */
export async function underpromotions(platform: Platform, user: string) {
  const [mine, against] = await Promise.all([api.events(platform, user, "UNDERPROMOTION", 1000), api.eventsAgainst(platform, user, "UNDERPROMOTION", 1000)]);
  return [...mine.map((e) => ({ e, mine: true })), ...against.map((e) => ({ e, mine: false }))];
}

export async function checkUnderpromotions(platform: Platform, user: string, onVerdict: (v: UpVerdict, done: number, total: number) => void) {
  const done = new Set((await savedVerdicts(platform, user)).map((v) => v.key));
  const todo = (await underpromotions(platform, user)).filter(({ e }) => !done.has(`${e.game_id}:${e.ply}`));
  const games = new Map<number, Promise<Awaited<ReturnType<typeof api.game>>>>();
  const engines = await Promise.all(Array.from({ length: Math.min(workersFor("balanced"), Math.max(1, todo.length)) }, () => UciEngine.start()));
  let next = 0, finished = 0;
  try {
    await Promise.all(engines.map(async (engine) => {
      while (next < todo.length) {
        const { e, mine } = todo[next++];
        if (!games.has(e.game_id)) games.set(e.game_id, api.game(String(e.game_id)));
        const g = await games.get(e.game_id)!;
        const req = { fen: g.initial_fen, moves: g.moves.slice(0, e.ply - 1).map((m) => m.uci) };
        await engine.newGame();
        const base = await engine.analyse({ ...req, nodes: BASELINE_NODES });
        const legal = legalMoves(e.fen);
        const depth = Math.min(base.mate !== null ? PROBE.mate_depth : base.depth ?? 12, PROBE.max_depth);
        await engine.newGame();
        const out = await engine.probe({ ...req, nodes: PROBE.nodes_per_line * legal, depth, lines: legal });
        const color = e.color;
        const verdict = bestMoveVerdict(e.fen, e.uci, color, legal,
          out ? { kind: "all_moves", moves: [], results: out.lines, budget: {} } : undefined);
        const v: UpVerdict = { key: `${e.game_id}:${e.ply}`, mine, best: verdict.best, rank: verdict.rank, legal };
        await store.putItem(`${prefix(platform, user)}|${v.key}`, v);
        onVerdict(v, ++finished, todo.length);
      }
    }));
  } finally {
    engines.forEach((x) => x.terminate());
  }
}
