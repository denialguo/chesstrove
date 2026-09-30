// Runs this browser's engine over one player's games: a few Web Workers, a priority queue, results
// checkpointed to IndexedDB after every game and every probe. Only starts when the visitor asks.
//
// Order: (1) games with deterministic events (sparse, high-value), (2) the 100 most recent, (3) the rest,
// newest first. Probes (the expensive searches) run only on candidates a finished game's baseline turns
// up, and go ahead of further baseline games so discoveries appear early.

import { unpack, probeRequests, type Game, type InputGame, type GameResults, type ProbeRequest, type Analysed } from "./archaeology";
import { store, playerKey, type StoredGame, type Speed } from "./store";
import { ENGINE, UciEngine, type PositionResult } from "./uci";
import { apiUrl, wakeFetch } from "../lib/api";

/** Baseline search per position. Chosen by the benchmark (ARCHITECTURE.md, "Browser engine"). */
export const BASELINE_NODES = 25_000;
// Probes: the deepest iteration all lines finish, aiming at the position's own baseline depth (native:
// engine.run_probe), with a node ceiling per line sized for a browser. Part of the config identity.
const PROBE = { version: 1, nodes_per_line: 1_000_000, max_depth: 30, mate_depth: 20 };
const RECENT = 100;

/** Everything that changes results. Results under any other config are never reused. */
export const CONFIG = `${ENGINE.name}|${ENGINE.build}|${ENGINE.flavor}|nodes=${BASELINE_NODES}|multipv=1|hash=${ENGINE.hash_mb}`
  + `|probe=v${PROBE.version}:${PROBE.nodes_per_line}:${PROBE.max_depth}:${PROBE.mate_depth}`;

export interface Device { cores: number; memoryGb: number | null; constrained: boolean }

export function device(): Device {
  const cores = navigator.hardwareConcurrency || 2;
  const memoryGb = (navigator as Navigator & { deviceMemory?: number }).deviceMemory ?? null;
  const coarse = typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches;
  return { cores, memoryGb, constrained: cores <= 4 || (memoryGb !== null && memoryGb <= 4) || coarse };
}

export function workersFor(speed: Speed, d: Device = device()): number {
  if (d.constrained) return 1;
  const spare = Math.max(1, d.cores - 1);
  return speed === "max" ? spare : speed === "fast" ? Math.min(4, spare) : Math.min(2, spare);
}

export const supported = () => typeof WebAssembly === "object" && typeof Worker === "function" && typeof indexedDB === "object";

export interface Progress {
  games: number; gamesDone: number; positions: number; positionsDone: number;
  probesPending: number; rate: number | null; // positions per second, rolling
  running: boolean; error: string | null;
}

type Task = { kind: "game"; g: Game } | { kind: "probe"; g: Game; req: ProbeRequest };

export class Analysis {
  readonly key: string;
  private games: Game[] = [];
  private done = new Map<number, StoredGame>();
  private order: Game[] = [];
  private workers: UciEngine[] = [];
  private busy = new Set<number>(); // game ids in flight
  private running = false;
  private error: string | null = null;
  private samples: [number, number][] = []; // (time, positions done)
  private listeners = new Set<() => void>();

  constructor(readonly platform: string, readonly user: string) {
    this.key = playerKey(CONFIG, platform, user);
  }

  async load(): Promise<void> {
    const r = await wakeFetch(apiUrl(`/players/${this.platform}/${encodeURIComponent(this.user)}/engine-input`));
    if (!r.ok) throw new Error(`couldn't load the games (${r.status})`);
    const input: InputGame[] = await r.json();
    this.games = input.filter((g) => !g.chess960 && g.ply_count > 0).map((g) => unpack(g, this.user));
    for (const s of await store.games(this.key)) this.done.set(s.gameId, s);
    const ids = new Set<number>();
    const take = (gs: Game[]) => gs.forEach((g) => { if (!ids.has(g.id)) { ids.add(g.id); this.order.push(g); } });
    take(this.games.filter((g) => g.events.length > 0)); // input is newest first
    take(this.games.slice(0, RECENT));
    take(this.games);
    this.emit();
  }

  subscribe(fn: () => void) { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; }
  private emit() { this.listeners.forEach((fn) => fn()); }

  progress(): Progress {
    const positions = this.games.reduce((n, g) => n + g.ply_count + 1, 0);
    let positionsDone = 0, probesPending = 0;
    for (const s of this.done.values()) { positionsDone += s.positions.length; probesPending += s.pending.length; }
    const [t0, p0] = this.samples[0] ?? [0, 0];
    const [t1, p1] = this.samples[this.samples.length - 1] ?? [0, 0];
    return { games: this.games.length, gamesDone: this.done.size, positions, positionsDone, probesPending,
             rate: t1 - t0 > 3000 ? ((p1 - p0) * 1000) / (t1 - t0) : null, running: this.running, error: this.error };
  }

  /** The finished games, for the discoveries. */
  analysed(): Analysed {
    const byId = new Map(this.games.map((g) => [g.id, g]));
    return [...this.done.values()].flatMap((s) => (byId.has(s.gameId) ? [{ g: byId.get(s.gameId)!, r: s as GameResults }] : []));
  }

  complete() { const p = this.progress(); return p.gamesDone === p.games && p.probesPending === 0; }

  async start(workers: number): Promise<void> {
    if (this.running) return;
    this.running = true;
    this.error = null;
    this.samples = [];
    this.emit();
    try {
      this.workers = await Promise.all(Array.from({ length: workers }, () => UciEngine.start()));
    } catch (e) {
      this.fail(e);
      return;
    }
    await Promise.all(this.workers.map((w) => this.loop(w)));
    if (this.running) this.stop();
  }

  stop() {
    this.running = false;
    this.workers.forEach((w) => w.terminate()); // an unfinished game is simply redone next time
    this.workers = [];
    this.busy.clear();
    this.emit();
  }

  private fail(e: unknown) {
    this.error = e instanceof Error ? e.message : String(e);
    this.stop();
  }

  private next(): Task | null {
    for (const s of this.done.values()) {
      const req = s.pending.find((p) => !this.busy.has(s.gameId * 10_000 + p.position * 4 + ["top_two", "vs_queen", "all_moves"].indexOf(p.kind)));
      if (req) return { kind: "probe", g: this.games.find((g) => g.id === s.gameId)!, req };
    }
    const g = this.order.find((g) => !this.done.has(g.id) && !this.busy.has(g.id));
    return g ? { kind: "game", g } : null;
  }

  private async loop(engine: UciEngine) {
    while (this.running) {
      const task = this.next();
      if (!task) return;
      const tag = task.kind === "game" ? task.g.id : task.g.id * 10_000 + task.req.position * 4 + ["top_two", "vs_queen", "all_moves"].indexOf(task.req.kind);
      this.busy.add(tag);
      try {
        if (task.kind === "game") await this.baseline(engine, task.g);
        else await this.probe(engine, task.g, task.req);
      } catch (e) {
        if (this.running) this.fail(e);
        return;
      } finally {
        this.busy.delete(tag);
      }
    }
  }

  private async baseline(engine: UciEngine, g: Game) {
    await engine.newGame();
    const positions: PositionResult[] = [];
    for (let p = 0; p <= g.moves.length; p++) {
      if (!this.running) return;
      positions.push(await engine.analyse({ fen: g.initial_fen, moves: g.moves.slice(0, p), nodes: BASELINE_NODES }));
    }
    const s: StoredGame = { gameId: g.id, positions, probes: {}, pending: probeRequests(g, positions) };
    await store.putGame(this.key, s);
    this.done.set(g.id, s);
    const done = [...this.done.values()].reduce((n, x) => n + x.positions.length, 0);
    this.samples.push([performance.now(), done]);
    while (this.samples.length > 2 && this.samples[this.samples.length - 1][0] - this.samples[0][0] > 30_000) this.samples.shift();
    this.emit();
  }

  private async probe(engine: UciEngine, g: Game, req: ProbeRequest) {
    const s = this.done.get(g.id)!;
    const base = s.positions[req.position];
    const depth = Math.min(base?.mate !== null && base?.mate !== undefined ? PROBE.mate_depth : base?.depth ?? 12, PROBE.max_depth);
    await engine.newGame(); // probes never share a hash with anything else
    const out = await engine.probe({ fen: g.initial_fen, moves: g.moves.slice(0, req.position), nodes: PROBE.nodes_per_line * req.lines,
                                     depth, lines: req.lines, searchmoves: req.moves ?? undefined });
    if (!this.running) return;
    const next: StoredGame = { ...s, pending: s.pending.filter((p) => p !== req) };
    if (out) next.probes = { ...s.probes, [`${req.position}:${req.kind}`]: {
      kind: req.kind, moves: req.moves ?? out.lines.map((l) => l.uci), results: out.lines,
      budget: { depth, nodes_cap: PROBE.nodes_per_line * req.lines, completed_depth: out.completed } } };
    await store.putGame(this.key, next); // a probe with no complete iteration is dropped, not retried forever
    this.done.set(g.id, next);
    this.emit();
  }
}
