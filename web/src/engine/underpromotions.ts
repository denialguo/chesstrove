// The 'Best-move underpromotion' check: was the player's own underpromotion the single best move? Stockfish (in this
// browser, one worker) answers it from one search that scores the moves that matter side by side: the played move,
// the other promotions on the same move (queening is the usual rival: mates and ties hide there), and the engine's
// own two best moves. Same verdict rules as the full analysis (bestMoveVerdict: transpositions and near-equal
// scores tie). Scoring every legal move cost 4-5x the nodes for the same verdicts; it also gave an exact rank, which
// this no longer does. Measured on 19 underpromotions with a native full-analysis verdict: 17 agree (the all-moves
// probe: 16), never a false "unique best". Each verdict is saved as soon as it's known.

import { Chess } from "chessops/chess";
import { parseFen } from "chessops/fen";
import { makeUci } from "chessops/util";
import { api, type Platform } from "../lib/api";
import { bestMoveVerdict, type BestMove } from "./archaeology";
import { BASELINE_NODES, CONFIG } from "./runner";
import { playerKey, store } from "./store";
import { UciEngine } from "./uci";

export interface UpVerdict { key: string; mine: boolean; best: BestMove }

// nodes per candidate move; 100k-500k gave identical verdicts on the test set, so this is headroom, not precision
const PROBE = { nodes_per_line: 250_000, max_depth: 30, mate_depth: 20 };
const prefix = (platform: string, user: string) => playerKey(`${CONFIG}|upcheck-v4`, platform, user);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function legalUcis(fen: string): string[] {
  const pos = Chess.fromSetup(parseFen(fen).unwrap()).unwrap();
  const out: string[] = [];
  for (const [from, dests] of pos.allDests()) {
    const pawn = pos.board.get(from)?.role === "pawn";
    for (const to of dests) {
      if (pawn && (to >> 3 === 0 || to >> 3 === 7)) for (const p of ["queen", "rook", "bishop", "knight"] as const) out.push(makeUci({ from, to, promotion: p }));
      else out.push(makeUci({ from, to }));
    }
  }
  return out;
}

export const savedVerdicts = (platform: string, user: string) => store.items<UpVerdict>(prefix(platform, user));

/** The player's own underpromotions. Their opponents' are in the index too, but not Stockfish-checked by default. */
export const underpromotions = (platform: Platform, user: string) => api.events(platform, user, "UNDERPROMOTION", 1000);

export async function checkUnderpromotions(platform: Platform, user: string, onVerdict: (v: UpVerdict, done: number, total: number) => void,
                                           opts: { signal?: AbortSignal } = {}) {
  const done = new Set((await savedVerdicts(platform, user)).map((v) => v.key));
  const todo = (await underpromotions(platform, user)).filter((e) => !done.has(`${e.game_id}:${e.ply}`));
  if (!todo.length || opts.signal?.aborted) return;
  const engine = await UciEngine.start(); // one worker: a few seconds of search, not worth a second core's heat
  const stop = () => engine.terminate(); // leaving the page stops it; saved verdicts stay
  opts.signal?.addEventListener("abort", stop);
  let finished = 0;
  try {
    for (const e of todo) {
      if (opts.signal?.aborted) break;
      const started = performance.now();
      const g = await api.game(String(e.game_id));
      const req = { fen: g.initial_fen, moves: g.moves.slice(0, e.ply - 1).map((m) => m.uci) };
      const legal = legalUcis(e.fen);
      let best: BestMove = "unknown";
      if (legal.length === 1) best = "unique_best";
      else {
        await engine.newGame();
        const base = await engine.analyse({ ...req, nodes: BASELINE_NODES });
        const depth = Math.min(base.mate !== null ? PROBE.mate_depth : base.depth ?? 12, PROBE.max_depth);
        await engine.newGame();
        const top = await engine.probe({ ...req, nodes: PROBE.nodes_per_line * 2, depth, lines: 2 });
        const rivals = ["q", "r", "b", "n"].map((p) => e.uci.slice(0, 4) + p).filter((u) => legal.includes(u));
        const cand = [...new Set([e.uci, ...rivals, ...(top?.lines ?? []).map((l) => l.uci)])];
        await engine.newGame();
        const out = await engine.probe({ ...req, nodes: PROBE.nodes_per_line * cand.length, depth, lines: cand.length, searchmoves: cand });
        best = bestMoveVerdict(e.fen, e.uci, e.color, cand.length, out ? { kind: "all_moves", moves: [], results: out.lines, budget: {} } : undefined).best;
      }
      const v: UpVerdict = { key: `${e.game_id}:${e.ply}`, mine: true, best };
      await store.putItem(`${prefix(platform, user)}|${v.key}`, v);
      onVerdict(v, ++finished, todo.length);
      await sleep((performance.now() - started) * 0.5); // rest between positions: a third of the time idle
    }
  } catch (err) {
    if (!opts.signal?.aborted) throw err;
  } finally {
    opts.signal?.removeEventListener("abort", stop);
    stop();
  }
}
