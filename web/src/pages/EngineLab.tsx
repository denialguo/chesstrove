import { useState } from "react";
import { discoveries, expected, probeRequests, unpack, type Analysed, type Game, type GameResults, type InputGame, type Probe } from "../engine/archaeology";
import { ENGINE, UciEngine, blackToMove, type PositionResult } from "../engine/uci";

// The browser-engine benchmark (/lab/engine): speed at 1/2/4 workers x 10k/25k nodes on a fixed sample, and
// agreement with the native Stockfish index on the whole fixture (scripts/engine_fixture.py). Open it on any
// machine; `?auto=1` runs it straight away and leaves the report in window.__bench.

type FixtureGame = InputGame & { native: PositionResult[]; native_top_two: Record<string, Probe["results"]> };
interface Fixture { player: string; native: Record<string, unknown>; games: FixtureGame[] }

const SPEED_POSITIONS = 600;

async function analyseGames(games: Game[], nodes: number, workers: number, onDone?: (n: number) => void) {
  const t0 = performance.now();
  const engines = await Promise.all(Array.from({ length: workers }, () => UciEngine.start()));
  const startup = performance.now() - t0;
  const results = new Map<number, PositionResult[]>();
  let next = 0, positions = 0;
  const t1 = performance.now();
  await Promise.all(engines.map(async (e) => {
    while (next < games.length) {
      const g = games[next++];
      await e.newGame();
      const out: PositionResult[] = [];
      for (let p = 0; p <= g.moves.length; p++) out.push(await e.analyse({ fen: g.initial_fen, moves: g.moves.slice(0, p), nodes }));
      results.set(g.id, out);
      positions += out.length;
      onDone?.(positions);
    }
  }));
  const seconds = (performance.now() - t1) / 1000;
  return { engines, results, positions, seconds, startupMs: Math.round(startup) };
}

async function topTwo(engines: UciEngine[], games: Game[], results: Map<number, PositionResult[]>) {
  const tasks = games.flatMap((g) => probeRequests(g, results.get(g.id)!).filter((r) => r.kind === "top_two").map((r) => ({ g, r })));
  const probes = new Map<number, Record<string, Probe>>();
  let next = 0;
  await Promise.all(engines.map(async (e) => {
    while (next < tasks.length) {
      const { g, r } = tasks[next++];
      const base = results.get(g.id)![r.position];
      const depth = Math.min(base.mate !== null ? 20 : base.depth ?? 12, 30);
      await e.newGame();
      const out = await e.probe({ fen: g.initial_fen, moves: g.moves.slice(0, r.position), nodes: 1_000_000 * r.lines, depth, lines: r.lines });
      if (out) probes.set(g.id, { ...probes.get(g.id), [`${r.position}:top_two`]: { kind: "top_two", moves: [], results: out.lines, budget: {} } });
    }
  }));
  return { probes, count: tasks.length };
}

const pct = (a: number, b: number) => (b ? Math.round((1000 * a) / b) / 10 : null);

function bucket(r: PositionResult, stm: "w" | "b") {
  const e = expected("w", stm, r.cp, r.mate);
  return e < 0.35 ? "black" : e > 0.65 ? "white" : "balanced";
}

/** Agreement between two sets of (game, ply) keys. */
function agree(native: Set<string>, browser: Set<string>) {
  const both = [...native].filter((k) => browser.has(k)).length;
  return { native: native.size, browser: browser.size, both, recall: pct(both, native.size), precision: pct(both, browser.size) };
}

function candidateSets(analysed: Analysed) {
  const d = discoveries(analysed, 100_000);
  const key = (x: { game: { id: number }; ply: number }) => `${x.game.id}:${x.ply}`;
  return {
    throws_0_30: new Set(d.biggest_throw!.filter((x) => Number(x.score.value) >= 0.3).map(key)),
    comebacks_from_0_10: new Set(d.biggest_comeback!.filter((x) => Number(x.score.value) <= 0.1).map((x) => String(x.game.id))),
    only_winning_candidates: new Set(analysed.flatMap(({ g, r }) => probeRequests(g, r.positions).filter((p) => p.kind === "top_two").map((p) => `${g.id}:${p.position + 1}`))),
    only_winning_moves: new Set(d.only_winning_move!.map(key)),
    sacrifices: new Set(d.material_sacrifice!.map(key)),
    mate_runs: new Set(d.longest_mate_found!.map(key)),
  };
}

export async function runBench(fixture: Fixture, log: (s: string) => void) {
  const games = fixture.games.map((g) => unpack(g, fixture.player));
  const lag = { max: 0 };
  let last = performance.now();
  const timer = setInterval(() => { const now = performance.now(); lag.max = Math.max(lag.max, now - last - 50); last = now; }, 50);
  const report: Record<string, unknown> = {
    engine: ENGINE, native: fixture.native, cores: navigator.hardwareConcurrency, userAgent: navigator.userAgent,
    fixture: { games: games.length, positions: games.reduce((n, g) => n + g.moves.length + 1, 0) },
  };
  // speed: the same first ~600 positions each time
  const sample: Game[] = [];
  for (let n = 0; n < SPEED_POSITIONS && sample.length < games.length; n += games[sample.length].moves.length + 1) sample.push(games[sample.length]);
  const speed = [];
  for (const nodes of [10_000, 25_000]) for (const workers of [1, 2, 4]) {
    lag.max = 0;
    const r = await analyseGames(sample, nodes, workers);
    r.engines.forEach((e) => e.terminate());
    const row = { nodes, workers, positions: r.positions, seconds: Math.round(r.seconds * 10) / 10,
                  positions_per_second: Math.round(r.positions / r.seconds), worker_startup_ms: r.startupMs,
                  max_main_thread_lag_ms: Math.round(lag.max) };
    speed.push(row);
    log(JSON.stringify(row));
  }
  report.speed = speed;
  // agreement with native, on every fixture game
  const accuracy: Record<string, unknown> = {};
  const nativeAnalysed: Analysed = fixture.games.map((fg, i) => ({ g: games[i], r: {
    positions: fg.native, probes: Object.fromEntries(Object.entries(fg.native_top_two).map(([k, v]) => [k, { kind: "top_two", moves: [], results: v, budget: {} } as Probe])) } }));
  const nativeSets = candidateSets(nativeAnalysed);
  for (const nodes of [10_000, 25_000]) {
    const workers = Math.min(4, Math.max(1, navigator.hardwareConcurrency - 1));
    const r = await analyseGames(games, nodes, workers, (n) => { if (n % 1000 < 80) log(`${nodes} nodes: ${n} positions`); });
    const probes = await topTwo(r.engines, games, r.results);
    r.engines.forEach((e) => e.terminate());
    let n = 0, sameBest = 0, sameBucket = 0, nativeMates = 0, bothMate = 0, browserOnlyMate = 0;
    fixture.games.forEach((fg, i) => {
      const g = games[i], mine = r.results.get(g.id)!;
      fg.native.forEach((nat, p) => {
        if (nat.best === null) return; // terminal positions: the rules decide, nothing to compare
        const b = mine[p], stm = blackToMove(g.initial_fen, p) ? "b" : "w";
        n++;
        if (b.best === nat.best) sameBest++;
        if (bucket(b, stm) === bucket(nat, stm)) sameBucket++;
        if (nat.mate !== null) { nativeMates++; if (b.mate !== null && Math.sign(b.mate) === Math.sign(nat.mate)) bothMate++; }
        else if (b.mate !== null) browserOnlyMate++;
      });
    });
    const browserAnalysed: Analysed = games.map((g) => ({ g, r: { positions: r.results.get(g.id)!, probes: probes.probes.get(g.id) ?? {} } as GameResults }));
    const sets = candidateSets(browserAnalysed);
    accuracy[`${nodes}`] = {
      positions: n, seconds: Math.round(r.seconds), positions_per_second: Math.round(r.positions / r.seconds), workers,
      top_two_probes: probes.count,
      same_best_move: pct(sameBest, n), same_broad_evaluation: pct(sameBucket, n),
      mates: { native: nativeMates, found_same_side: pct(bothMate, nativeMates), browser_only: browserOnlyMate },
      discoveries: Object.fromEntries(Object.entries(nativeSets).map(([k, v]) => [k, agree(v, sets[k as keyof typeof sets])])),
    };
    log(JSON.stringify(accuracy[`${nodes}`]));
  }
  report.accuracy = accuracy;
  clearInterval(timer);
  return report;
}

export default function EngineLab() {
  const [lines, setLines] = useState<string[]>([]);
  const [report, setReport] = useState<unknown>(null);
  const [running, setRunning] = useState(false);
  const go = async () => {
    setRunning(true);
    const fixture: Fixture = await (await fetch("/lab/bench.json")).json();
    const r = await runBench(fixture, (s) => setLines((l) => [...l, s]));
    (window as unknown as { __bench: unknown }).__bench = r;
    setReport(r);
    setRunning(false);
  };
  if (new URLSearchParams(location.search).has("auto") && !running && !report && !lines.length) void go();
  return (
    <main className="page" style={{ padding: "2rem", maxWidth: 900, margin: "0 auto" }}>
      <h1>Browser engine benchmark</h1>
      <p>{ENGINE.name} ({ENGINE.build}, {ENGINE.flavor}). Speed on a fixed sample at 1/2/4 workers and 10k/25k nodes, then
        agreement with ChessTrove's native Stockfish index. Takes a few minutes and uses several CPU cores.</p>
      <button type="button" disabled={running} onClick={go}>{running ? "Running…" : "Run benchmark"}</button>
      <pre style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{lines.join("\n")}</pre>
      {report ? <pre style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{JSON.stringify(report, null, 2)}</pre> : null}
    </main>
  );
}
