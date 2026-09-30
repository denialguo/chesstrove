// Engine archaeology in the browser: the record book's six discoveries derived from this browser's own
// engine results. A port of src/chesstrove/archaeology.py and db.archaeology_moves_sql /
// archaeology_positions_sql (lichess scale, default Params); keep the two in step. Checked against the
// server on the native corpus by scripts/engine_parity (same inputs must give the same discoveries).

import { Chess } from "chessops/chess";
import { makeFen, parseFen } from "chessops/fen";
import { makeSan } from "chessops/san";
import { parseUci } from "chessops/util";
import type { Discovery, DiscoveryType, Eval } from "../lib/api";
import type { PositionResult, ProbeLine } from "./uci";

export const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

/** One game as GET /api/players/{platform}/{username}/engine-input sends it. */
export interface InputGame {
  id: number; played_at: string | null; white: string; black: string; result: string; initial_fen: string | null;
  chess960: boolean; ply_count: number; external_id: string | null; source_key: string; termination: string | null;
  uci: string; san: string; captured: string; promotion: string; flags: string; mw: number[]; mb: number[];
  legal: number[]; events: [number, string][];
}

/** A game, unpacked: index i = ply i + 1. */
export interface Game extends InputGame {
  moves: string[]; sans: string[]; color: "w" | "b"; // the player's colour
}

export function unpack(g: InputGame, player: string): Game {
  return { ...g, moves: g.uci ? g.uci.split(" ") : [], sans: g.san ? g.san.split(" ") : [],
           color: g.white.toLowerCase() === player.toLowerCase() ? "w" : "b" };
}

export interface Probe { kind: "top_two" | "vs_queen" | "all_moves"; moves: string[]; results: ProbeLine[]; budget: Record<string, number> }

/** Everything this browser has computed for a game. */
export interface GameResults { positions: PositionResult[]; probes: Record<string, Probe> } // probes keyed `${position}:${kind}`

export const PARAMS = { winning: 0.9, not_winning: 0.6, sacrifice_floor: 0.5 } as const;
const K = 0.00368208; // lichess win% curve (db.LICHESS_K)

type Color = "w" | "b";
const other = (c: Color): Color => (c === "w" ? "b" : "w");
const moverOf = (g: Game, ply: number): Color => {
  const startBlack = (g.initial_fen ?? "").split(" ")[1] === "b";
  return ((ply % 2 === 1) !== startBlack) ? "w" : "b";
};

/** `pov`'s expected score (0..1) with `stm` to move, from White-POV values (db._pov_expected, lichess). */
export function expected(pov: Color, stm: Color, cp: number | null, mate: number | null): number {
  if (mate === 0) return stm === pov ? 0 : 1;
  if (mate !== null) return (mate > 0) === (pov === "w") ? 1 : 0;
  const povCp = pov === "w" ? cp! : -cp!;
  return 1 / (1 + Math.exp(-K * povCp));
}

/** The Stockfish-WDL scale (db._expected "stockfish"), only for probe candidate selection. */
function expectedSf(pov: Color, r: PositionResult): number {
  if (r.mate === 0) return 1;
  if (!r.wdl) return 0.5;
  return pov === "w" ? (r.wdl[0] + r.wdl[1] / 2) / 1000 : (r.wdl[2] + r.wdl[1] / 2) / 1000;
}

/** A total order on evaluations from `pov`'s side (db._pov_ordinal). */
export function ordinal(pov: Color, stm: Color, cp: number | null, mate: number | null): number {
  if (mate === 0) return stm === pov ? -100000 : 100000;
  if (mate !== null) return (mate > 0) === (pov === "w") ? 100000 - Math.abs(mate) : -100000 + Math.abs(mate);
  return pov === "w" ? cp! : -cp!;
}

function showEval(cp: number | null, mate: number | null, pov: Color): Eval | null {
  if (cp === null && mate === null) return null;
  if (mate !== null) return { mate: mate === 0 ? 0 : pov === "w" ? mate : -mate };
  return { cp: pov === "w" ? cp! : -cp! };
}

const VALUE: Record<string, number> = { P: 1, N: 3, B: 3, R: 5, Q: 9 };
const val = (letter: string | null) => (letter ? VALUE[letter] ?? 0 : 0);
const at = (s: string, i: number) => (s[i] && s[i] !== "." ? s[i] : null);

// --- boards, only for what's shown ---------------------------------------------------------------------

/** FEN after `plies` moves of the game (0 = the start). */
export function fenAt(g: Game, plies: number): string {
  const pos = Chess.fromSetup(parseFen(g.initial_fen ?? START_FEN).unwrap()).unwrap();
  for (const u of g.moves.slice(0, plies)) pos.play(parseUci(u)!);
  return makeFen(pos.toSetup());
}

export function sanAt(fen: string, uci: string | null): string | null {
  if (!uci) return null;
  try {
    const pos = Chess.fromSetup(parseFen(fen).unwrap()).unwrap();
    return makeSan(pos, parseUci(uci)!);
  } catch { return null; }
}

/** The position after `ucis` from `fen`, without move counters (insights._transposition_keys). */
function epdAfter(fen: string, ucis: string[]): string | null {
  try {
    const pos = Chess.fromSetup(parseFen(fen).unwrap()).unwrap();
    for (const u of ucis) {
      const m = parseUci(u);
      if (!m || !pos.isLegal(m)) return null;
      pos.play(m);
    }
    return makeFen(pos.toSetup()).split(" ").slice(0, 4).join(" ");
  } catch { return null; }
}

// --- the move rows (db.archaeology_moves_sql) -----------------------------------------------------------

interface MoveRow {
  g: Game; ply: number; color: Color; uci: string; san: string; captured: string | null; promotion: string | null;
  isCheck: boolean; isCheckmate: boolean; engineChoice: string | null;
  cpBefore: number | null; mateBefore: number | null; cpAfter: number | null; mateAfter: number | null;
  expBefore: number; expAfter: number; ordBefore: number; ordAfter: number;
  moverMateBefore: number | null; moverMateAfter: number | null;
  isRecapture: boolean; inCheckBefore: boolean; oppMaterialBefore: number | null;
  balanceBefore: number; wbMaxNext5: number | null; wbMinNext5: number | null; wbMaxRest: number | null; wbMinRest: number | null;
  replyCaptured: string | null; replySan: string | null; lastPly: number; endsInMate: boolean; lastMover: Color;
}

function moveRows(g: Game, r: GameResults): MoveRow[] {
  const out: MoveRow[] = [];
  const n = g.moves.length;
  const wb = (i: number) => g.mw[i] - g.mb[i]; // after ply i + 1
  for (let ply = 1; ply <= n; ply++) {
    const color = moverOf(g, ply);
    if (color !== g.color) continue;
    const b = r.positions[ply - 1], a = r.positions[ply];
    if (!b || !a) continue;
    const i = ply - 1; // index into per-ply arrays
    const sign = color === "w" ? 1 : -1;
    const captured = at(g.captured, i), promotion = at(g.promotion, i);
    const next = [1, 2, 3, 4, 5].map((k) => i + k).filter((j) => j < n).map(wb);
    const rest = Array.from({ length: n - i - 1 }, (_, k) => wb(i + 1 + k));
    const flags = Number(g.flags[i]);
    out.push({
      g, ply, color, uci: g.moves[i], san: g.sans[i], captured, promotion,
      isCheck: (flags & 1) > 0, isCheckmate: (flags & 2) > 0, engineChoice: b.best,
      cpBefore: b.cp, mateBefore: b.mate, cpAfter: a.cp, mateAfter: a.mate,
      expBefore: b.mate === 0 ? 1 : expected(color, color, b.cp, b.mate),
      expAfter: expected(color, other(color), a.cp, a.mate),
      ordBefore: ordinal(color, color, b.cp, b.mate), ordAfter: ordinal(color, other(color), a.cp, a.mate),
      moverMateBefore: b.mate === null ? null : sign * b.mate,
      moverMateAfter: a.mate === null ? null : a.mate === 0 ? 0 : sign * a.mate,
      isRecapture: captured !== null && i > 0 && at(g.captured, i - 1) !== null && g.moves[i].slice(2, 4) === g.moves[i - 1].slice(2, 4),
      inCheckBefore: i > 0 && (Number(g.flags[i - 1]) & 1) > 0,
      oppMaterialBefore: i > 0 ? (color === "w" ? g.mb[i - 1] : g.mw[i - 1]) : null,
      balanceBefore: sign * wb(i) - val(captured) - (promotion ? val(promotion) - 1 : 0),
      wbMaxNext5: next.length ? Math.max(...next) : null, wbMinNext5: next.length ? Math.min(...next) : null,
      wbMaxRest: rest.length ? Math.max(...rest) : null, wbMinRest: rest.length ? Math.min(...rest) : null,
      replyCaptured: at(g.captured, i + 1), replySan: g.sans[i + 1] ?? null,
      lastPly: g.ply_count, endsInMate: (Number(g.flags[n - 1]) & 2) > 0, lastMover: moverOf(g, n),
    });
  }
  return out;
}

// newest first, then game, then ply (archaeology.STABLE)
const stable = (x: MoveRow, y: MoveRow) =>
  (Date.parse(y.g.played_at ?? "") || 0) - (Date.parse(x.g.played_at ?? "") || 0) || x.g.id - y.g.id || x.ply - y.ply;

function evidence(m: MoveRow, scoreName: string, score: unknown): Discovery {
  const fenBefore = fenAt(m.g, m.ply - 1);
  return {
    game: gameOf(m.g), ply: m.ply, color: m.color, player: m.color === "w" ? m.g.white : m.g.black,
    opponent: m.color === "w" ? m.g.black : m.g.white,
    move: { san: m.san, uci: m.uci }, fen_before: fenBefore, fen_after: fenAt(m.g, m.ply),
    eval_before: showEval(m.cpBefore, m.mateBefore, m.color), eval_after: showEval(m.cpAfter, m.mateAfter, m.color),
    expected_before: round(m.expBefore), expected_after: round(m.expAfter),
    engine_choice: { uci: m.engineChoice, san: sanAt(fenBefore, m.engineChoice) },
    score: { name: scoreName, value: score as number },
  } as Discovery;
}

const round = (x: number) => Math.round(x * 1000) / 1000;
const gameOf = (g: Game) => ({ id: g.id, played_at: g.played_at, white: g.white, black: g.black, result: g.result,
  termination: g.termination, platform: g.source_key.split(":")[0], source_key: g.source_key, external_id: g.external_id });

// --- the six discoveries --------------------------------------------------------------------------------

export type Analysed = { g: Game; r: GameResults }[];

function biggestThrow(rows: MoveRow[], limit: number): Discovery[] {
  return rows.filter((m) => m.uci !== m.engineChoice && m.expBefore > m.expAfter)
    .sort((x, y) => (y.expBefore - y.expAfter) - (x.expBefore - x.expAfter) || y.ordBefore - x.ordBefore || x.ordAfter - y.ordAfter || stable(x, y))
    .slice(0, limit).map((m) => evidence(m, "expected_score_drop", round(m.expBefore - m.expAfter)));
}

function biggestComeback(games: Analysed, limit: number): Discovery[] {
  const picks: { g: Game; position: number; exp: number; ord: number; r: PositionResult; laterMate: number | null }[] = [];
  for (const { g, r } of games) {
    const won = (g.result === "1-0" && g.color === "w") || (g.result === "0-1" && g.color === "b");
    if (!won) continue;
    const rows = r.positions.map((p, position) => {
      const stm: Color = moverOf(g, position + 1);
      return { position, p, stm, exp: expected(g.color, stm, p.cp, p.mate), ord: ordinal(g.color, stm, p.cp, p.mate) };
    });
    const trusted = rows.map((x, i) => {
      const nx = rows[i + 1];
      const nextUci = g.moves[x.position] ?? null; // the move played from this position
      return !(nx && nextUci !== null && nextUci === x.p.best && Math.abs(nx.exp - x.exp) > 0.3);
    });
    let best: (typeof rows)[number] | null = null;
    rows.forEach((x, i) => {
      if (trusted[i] && (!best || x.exp < best.exp || (x.exp === best.exp && x.ord < best.ord))) best = x;
    });
    if (!best) continue;
    const b = best as (typeof rows)[number];
    const later = rows.findIndex((x, i) => x.position > b.position && trusted[i] && x.ord > 90000);
    picks.push({ g, position: b.position, exp: b.exp, ord: b.ord, r: b.p, laterMate: later < 0 ? null : rows[later].position });
  }
  const clock = (g: Game) => (/time|abandon/i.test(g.termination ?? "") ? 1 : 0);
  picks.sort((x, y) => x.exp - y.exp || x.ord - y.ord || clock(x.g) - clock(y.g)
    || (Date.parse(y.g.played_at ?? "") || 0) - (Date.parse(x.g.played_at ?? "") || 0) || x.g.id - y.g.id);
  return picks.slice(0, limit).map(({ g, position, exp, r, laterMate }) => {
    const fen = fenAt(g, position);
    return {
      game: gameOf(g), ply: position, color: g.color, player: g.color === "w" ? g.white : g.black,
      opponent: g.color === "w" ? g.black : g.white,
      move: position > 0 ? { san: g.sans[position - 1], uci: g.moves[position - 1], by: moverOf(g, position) } : null,
      fen, eval: showEval(r.cp, r.mate, g.color), expected: round(exp),
      engine_choice: { uci: r.best, san: sanAt(fen, r.best) }, later_forced_mate_at: laterMate,
      score: { name: "worst_expected_score", value: round(exp) },
    } as unknown as Discovery;
  });
}

function lineExp(l: ProbeLine, color: Color) { return l.mate === 0 ? 1 : expected(color, color, l.score_cp, l.mate); }

function onlyWinningMove(rows: MoveRow[], results: Map<number, GameResults>, limit: number): Discovery[] {
  const found: { m: MoveRow; p: Probe; e1: number; e2: number }[] = [];
  for (const m of rows) {
    const p = results.get(m.g.id)?.probes[`${m.ply - 1}:top_two`];
    if (!p || p.results.length < 2 || p.results[0].uci !== m.uci) continue;
    const e1 = lineExp(p.results[0], m.color), e2 = lineExp(p.results[1], m.color);
    if (e1 >= PARAMS.winning && e2 <= PARAMS.not_winning && !m.isRecapture && !m.isCheckmate) found.push({ m, p, e1, e2 });
  }
  const quiet = (m: MoveRow) => (m.captured === null && !m.isCheck ? 1 : 0);
  found.sort((x, y) => quiet(y.m) - quiet(x.m) || (y.e1 - y.e2) - (x.e1 - x.e2) || stable(x.m, y.m));
  return found.slice(0, limit).map(({ m, p, e1, e2 }) => {
    const ev = evidence(m, "gap_to_runner_up", round(e1 - e2));
    const [l1, l2] = p.results;
    return { ...ev, comparison: {
      best_line: { uci: l1.uci, eval: showEval(l1.score_cp, l1.mate, m.color), expected: round(e1) },
      runner_up: { uci: l2.uci, san: sanAt(ev.fen_before!, l2.uci), eval: showEval(l2.score_cp, l2.mate, m.color), expected: round(e2) },
      search: p.budget,
    } } as Discovery;
  });
}

const DEFICIT = { queen: 5, rook: 3, exchange: 2 } as const;

function sacrificeOf(m: MoveRow) {
  const cap = m.replyCaptured;
  if ((cap !== "Q" && cap !== "R") || m.wbMaxNext5 === null || m.inCheckBefore) return null;
  const mates = m.endsInMate && m.lastMover === m.color;
  if (m.lastPly - m.ply < 5 && !mates) return null;
  const white = m.color === "w";
  const bestNext = white ? m.wbMaxNext5 : -m.wbMinNext5!;
  const deficit = m.balanceBefore - bestNext;
  const kind = cap === "Q" && deficit >= DEFICIT.queen ? "queen" : cap === "R" && deficit >= DEFICIT.rook ? "rook"
    : cap === "R" && deficit >= DEFICIT.exchange ? "exchange" : null;
  if (!kind) return null;
  const bestRest = white ? m.wbMaxRest : m.wbMinRest === null ? null : -m.wbMinRest;
  const never = bestRest !== null && m.balanceBefore - bestRest >= DEFICIT[kind];
  return { kind, piece: cap === "Q" ? "queen" : "rook", reply: m.replySan, material_before: m.balanceBefore,
           material_after_window: bestNext, deficit, never_recovered: never, ends_in_mate: never && mates };
}

function materialSacrifice(rows: MoveRow[], limit: number): Discovery[] {
  const found = rows.filter((m) => m.uci === m.engineChoice && m.expAfter >= PARAMS.sacrifice_floor)
    .sort(stable).flatMap((m) => { const s = sacrificeOf(m); return s ? [{ m, s }] : []; });
  const order = { queen: 0, rook: 1, exchange: 2 } as const;
  found.sort((x, y) => Number(x.s.ends_in_mate === false) - Number(y.s.ends_in_mate === false)
    || order[x.s.kind as keyof typeof order] - order[y.s.kind as keyof typeof order] || y.s.deficit - x.s.deficit || y.m.expAfter - x.m.expAfter);
  return found.slice(0, limit).map(({ m, s }) => ({ ...evidence(m, "sacrifice", s.kind), sacrifice: s }) as unknown as Discovery);
}

function longestMateFound(rows: MoveRow[], limit: number): Discovery[] {
  const byGame = new Map<number, MoveRow[]>();
  for (const m of rows) if (m.endsInMate && m.lastMover === m.color) {
    if (!byGame.has(m.g.id)) byGame.set(m.g.id, []);
    byGame.get(m.g.id)!.push(m);
  }
  const runs: { m: MoveRow; runMoves: number; length: number }[] = [];
  for (const pm of byGame.values()) {
    const breaks = pm.filter((m) => m.moverMateBefore === null || m.moverMateBefore <= 0 || m.moverMateAfter === null || m.moverMateAfter < 0);
    const lastBreak = breaks.length ? Math.max(...breaks.map((m) => m.ply)) : 0;
    const run = pm.filter((m) => m.ply > lastBreak).sort((x, y) => x.ply - y.ply);
    const first = run[0];
    if (!first || first.moverMateBefore === null || first.moverMateBefore < 2 || run.length < 2 || (first.oppMaterialBefore ?? 1) <= 0) continue;
    runs.push({ m: first, runMoves: run.length, length: Math.min(run.length, first.moverMateBefore) });
  }
  runs.sort((x, y) => y.length - x.length || y.m.moverMateBefore! - x.m.moverMateBefore! || stable(x.m, y.m));
  return runs.slice(0, limit).map(({ m, runMoves, length }) => ({
    ...evidence(m, "mate_length", length),
    run: { moves: runMoves, engine_mate_in_at_start: m.moverMateBefore, mating_ply: m.lastPly },
  }) as unknown as Discovery);
}

/** Probe scores from the mover's side, comparable across lines (chess.engine.Score order). */
const lineOrd = (l: ProbeLine, color: Color) => ordinal(color, color, l.score_cp, l.mate);

/** uci -> a key; moves with the same key are one choice (insights._transposition_keys). */
function transpositionKeys(fenBefore: string, lines: ProbeLine[]): Record<string, string> {
  return Object.fromEntries(lines.map((l) => {
    const pv = l.pv;
    if (pv.length >= 2 && pv[0].length === 5 && pv[1].slice(2, 4) === pv[0].slice(2, 4)) return [l.uci, `promotion ${pv[0].slice(0, 4)} captured`];
    return [l.uci, (pv.length >= 2 && epdAfter(fenBefore, pv.slice(0, 2))) || l.uci];
  }));
}

export type BestMove = "unique_best" | "tied_best" | "not_best" | "unknown";

/** Moves within this much expected score of each other tie (insights.BEST_MARGIN): +5.6 vs +5.5 is noise,
 * and two forced mates are both a win whatever their length. */
export const BEST_MARGIN = 0.1;

/** Was `played` the best of all `legal` moves, from an all_moves probe (insights._best_move_verdict)?
 * Only when every legal move got a score; moves that transpose into the same position tie, and so do
 * moves within BEST_MARGIN of the top. The rank is the engine's raw order. */
export function bestMoveVerdict(fenBefore: string, played: string, color: Color, legal: number, am: Probe | undefined):
    { best: BestMove; rank: number | null; bestMoves: string[] } {
  if (!am || am.results.length !== legal || !am.results.some((l) => l.uci === played)) return { best: "unknown", rank: null, bestMoves: [] };
  const k = transpositionKeys(fenBefore, am.results);
  const raw = Object.fromEntries(am.results.map((l) => [l.uci, lineOrd(l, color)]));
  const cls: Record<string, number> = {};
  for (const [u, sc] of Object.entries(raw)) cls[k[u]] = Math.max(cls[k[u]] ?? -Infinity, sc);
  const scores = Object.fromEntries(Object.keys(raw).map((u) => [u, cls[k[u]]]));
  const expOf: Record<string, number> = {};
  for (const l of am.results) expOf[k[l.uci]] = Math.max(expOf[k[l.uci]] ?? 0, lineExp(l, color));
  const topExp = Math.max(...Object.values(expOf));
  const bestMoves = Object.keys(scores).filter((u) => expOf[k[u]] >= topExp - BEST_MARGIN).sort();
  return { best: bestMoves.includes(played) ? (bestMoves.length === 1 ? "unique_best" : "tied_best") : "not_best",
           rank: 1 + Object.values(scores).filter((sc) => sc > scores[played]).length, bestMoves };
}

function underpromotion(games: Analysed, limit: number): Discovery[] {
  const out: (Discovery & { _rank: number; _vrank: number; _t: number })[] = [];
  for (const { g, r } of games) {
    for (const [ply, type] of g.events) {
      if (type !== "UNDERPROMOTION" || moverOf(g, ply) !== g.color) continue;
      const b = r.positions[ply - 1], a = r.positions[ply];
      if (!b || !a) continue;
      const played = g.moves[ply - 1];
      const fenBefore = fenAt(g, ply - 1);
      const keys = (lines: ProbeLine[]) => transpositionKeys(fenBefore, lines);
      let vsQueen = "unknown";
      const vq = r.probes[`${ply - 1}:vs_queen`];
      if (vq) {
        const e = Object.fromEntries(vq.results.map((l) => [l.uci, lineExp(l, g.color)]));
        const queen = played.slice(0, 4) + "q";
        if (played in e && queen in e) {
          const k = keys(vq.results);
          const gap = e[played] - e[queen];
          vsQueen = k[played] === k[queen] || Math.abs(gap) < BEST_MARGIN ? "equal" : gap > 0 ? "better" : "worse";
        }
      }
      const legal = g.legal[ply - 1];
      const { best, rank, bestMoves } = bestMoveVerdict(fenBefore, played, g.color, legal, r.probes[`${ply - 1}:all_moves`]);
      out.push({
        game: gameOf(g), ply, color: g.color, player: g.color === "w" ? g.white : g.black,
        opponent: g.color === "w" ? g.black : g.white,
        move: { san: g.sans[ply - 1], uci: played }, fen_before: fenBefore, fen_after: fenAt(g, ply),
        best_move: best, vs_queen: vsQueen, played_move_rank: rank, legal_moves: legal,
        best_moves: bestMoves.map((u) => ({ uci: u, san: sanAt(fenBefore, u) })),
        eval_before: showEval(b.cp, b.mate, g.color), eval_after: showEval(a.cp, a.mate, g.color),
        score: { name: "verdict", value: vsQueen === "equal" ? `${best}, equal to queening` : `${best}, ${vsQueen} than queening` },
        _rank: { unique_best: 0, tied_best: 1, not_best: 2, unknown: 3 }[best]!,
        _vrank: { better: 0, equal: 1, worse: 2, unknown: 3 }[vsQueen]!, _t: Date.parse(g.played_at ?? "") || 0,
      } as unknown as Discovery & { _rank: number; _vrank: number; _t: number });
    }
  }
  out.sort((x, y) => x._rank - y._rank || x._vrank - y._vrank || y._t - x._t);
  return out.slice(0, limit);
}

/** The six record-book discoveries over the games analysed so far. */
export function discoveries(games: Analysed, limit = 6): Partial<Record<DiscoveryType, Discovery[]>> {
  const results = new Map(games.map(({ g, r }) => [g.id, r]));
  const rows = games.flatMap(({ g, r }) => moveRows(g, r));
  return {
    biggest_comeback: biggestComeback(games, limit),
    biggest_throw: biggestThrow(rows, limit),
    only_winning_move: onlyWinningMove(rows, results, limit),
    material_sacrifice: materialSacrifice(rows, limit),
    longest_mate_found: longestMateFound(rows, limit),
    underpromotion: underpromotion(games, limit),
  };
}

// --- which probes a game needs, once its baseline is in (engine.py probe_requests + db candidates) ----------

export interface ProbeRequest { position: number; kind: Probe["kind"]; moves: string[] | null; lines: number }

export function probeRequests(g: Game, positions: PositionResult[]): ProbeRequest[] {
  const out: ProbeRequest[] = [];
  g.moves.forEach((uci, i) => {
    const ply = i + 1;
    if (moverOf(g, ply) !== g.color) return;
    const b = positions[i];
    // ONLY_WINNING_MOVE candidates: played the engine's choice, more than one legal move, clearly winning
    // on either scale (db.only_winning_move_candidates)
    if (b && uci === b.best && g.legal[i] > 1
        && Math.max(expectedSf(g.color, b), b.mate === 0 ? 1 : expected(g.color, g.color, b.cp, b.mate)) >= PARAMS.winning)
      out.push({ position: i, kind: "top_two", moves: null, lines: Math.min(2, g.legal[i]) });
    // underpromotions: both questions, kept separate
    if (uci.length === 5 && "nbr".includes(uci[4])) {
      out.push({ position: i, kind: "vs_queen", moves: [uci, uci.slice(0, 4) + "q"], lines: 2 });
      out.push({ position: i, kind: "all_moves", moves: null, lines: g.legal[i] });
    }
  });
  return out;
}
