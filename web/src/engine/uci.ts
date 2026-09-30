// One Stockfish (WASM, single-threaded) in a Web Worker, spoken to over UCI. Mirrors the native engine
// layer (src/chesstrove/engine.py): every search sends the game's whole move history (`position ...
// moves ...`) so repetitions and the 50-move counter count, `ucinewgame` clears the hash per game, and
// scores are stored from White's point of view.

export const ENGINE = {
  name: "Stockfish 18",
  build: "stockfish.js 18.0.8 lite-single",
  flavor: "wasm",
  url: "/engine/stockfish-18-lite-single.js",
  hash_mb: 16,
} as const;

/** A position's baseline result, White's POV (same shape as the native engine_positions row). */
export interface PositionResult {
  cp: number | null; // exactly one of cp / mate is set
  mate: number | null; // 0 = side to move is checkmated
  wdl: [number, number, number] | null;
  best: string | null;
  depth: number | null;
}

/** One line of a probe, White's POV. */
export interface ProbeLine { uci: string; score_cp: number | null; mate: number | null; wdl: number[] | null; depth: number; pv: string[] }

export interface SearchRequest {
  fen: string | null; // null = standard start
  moves: string[]; // the game so far
  nodes: number;
  depth?: number;
  multipv?: number;
  searchmoves?: string[];
}

interface Info {
  depth: number; multipv: number; cp: number | null; mate: number | null; wdl: number[] | null;
  bound: boolean; pv: string[]; nodes: number;
}

function parseInfo(line: string): Info | null {
  const t = line.split(" ");
  if (t[0] !== "info" || !t.includes("score")) return null;
  const at = (k: string) => { const i = t.indexOf(k); return i < 0 ? null : t[i + 1]; };
  const s = t.indexOf("score");
  const pvAt = t.indexOf("pv");
  const wdlAt = t.indexOf("wdl");
  return {
    depth: Number(at("depth") ?? 0),
    multipv: Number(at("multipv") ?? 1),
    cp: t[s + 1] === "cp" ? Number(t[s + 2]) : null,
    mate: t[s + 1] === "mate" ? Number(t[s + 2]) : null,
    wdl: wdlAt < 0 ? null : [Number(t[wdlAt + 1]), Number(t[wdlAt + 2]), Number(t[wdlAt + 3])],
    bound: t.includes("lowerbound") || t.includes("upperbound"),
    pv: pvAt < 0 ? [] : t.slice(pvAt + 1),
    nodes: Number(at("nodes") ?? 0),
  };
}

/** Is Black to move after `moves` from `fen`? */
export function blackToMove(fen: string | null, plies: number): boolean {
  const startBlack = (fen ?? "").split(" ")[1] === "b";
  return startBlack !== (plies % 2 === 1);
}

function white(info: Info, black: boolean) {
  const flip = (v: number | null) => (v === null ? null : black ? -v : v);
  return {
    cp: flip(info.cp), mate: info.mate === 0 ? 0 : flip(info.mate),
    wdl: info.wdl ? (black ? [info.wdl[2], info.wdl[1], info.wdl[0]] : info.wdl) : null,
  };
}

export class UciEngine {
  private worker: Worker;
  private waiters: { match: (line: string) => boolean; resolve: (line: string) => void }[] = [];
  private onLine: ((line: string) => void) | null = null;
  private dead: Error | null = null;

  private constructor(url: string) {
    this.worker = new Worker(url);
    this.worker.onmessage = (e: MessageEvent) => {
      const line = String(e.data);
      this.onLine?.(line);
      const i = this.waiters.findIndex((w) => w.match(line));
      if (i >= 0) this.waiters.splice(i, 1)[0].resolve(line);
    };
    this.worker.onerror = (e) => { this.dead = new Error(e.message || "engine worker failed"); this.failAll(); };
  }

  static async start(url: string = ENGINE.url): Promise<UciEngine> {
    const engine = new UciEngine(url);
    engine.send("uci");
    await engine.wait((l) => l === "uciok", 30_000);
    engine.send("setoption name UCI_ShowWDL value true");
    engine.send(`setoption name Hash value ${ENGINE.hash_mb}`);
    engine.send("setoption name Threads value 1");
    await engine.ready();
    return engine;
  }

  send(cmd: string) { this.worker.postMessage(cmd); }

  private failAll() {
    for (const w of this.waiters.splice(0)) w.resolve("__dead__");
  }

  private wait(match: (line: string) => boolean, timeoutMs = 0): Promise<string> {
    if (this.dead) return Promise.reject(this.dead);
    return new Promise((resolve, reject) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const waiter = {
        match,
        resolve: (line: string) => {
          clearTimeout(timer);
          if (line === "__dead__") reject(this.dead ?? new Error("engine stopped")); else resolve(line);
        },
      };
      this.waiters.push(waiter);
      if (timeoutMs) timer = setTimeout(() => {
        this.waiters = this.waiters.filter((w) => w !== waiter);
        reject(new Error("engine did not answer"));
      }, timeoutMs);
    });
  }

  async ready() { this.send("isready"); await this.wait((l) => l === "readyok", 30_000); }

  async newGame() { this.send("ucinewgame"); await this.ready(); }

  private position(req: SearchRequest) {
    this.send(`position ${req.fen ? `fen ${req.fen}` : "startpos"}${req.moves.length ? ` moves ${req.moves.join(" ")}` : ""}`);
  }

  /** The baseline search: node-limited, one line. The final report of the search, like the native layer's. */
  async analyse(req: SearchRequest): Promise<PositionResult> {
    const black = blackToMove(req.fen, req.moves.length);
    let last: Info | null = null;
    this.onLine = (line) => { const i = parseInfo(line); if (i && i.multipv === 1 && i.pv.length) last = i; else if (i && !last) last = i; };
    this.position(req);
    this.send(`go nodes ${req.nodes}`);
    const done = await this.wait((l) => l.startsWith("bestmove"));
    this.onLine = null;
    const best = done.split(" ")[1];
    const info = last as Info | null;
    if (!info || best === "(none)") {
      // no legal moves: the rules decide. Stockfish reports mate 0 (mated) or cp 0 (stalemate).
      const mated = info?.mate === 0;
      return { cp: mated ? null : 0, mate: mated ? 0 : null, wdl: null, best: null, depth: null };
    }
    const w = white(info, black);
    return { cp: w.mate === null ? w.cp : null, mate: w.mate, wdl: w.wdl as PositionResult["wdl"], best, depth: info.depth || null };
  }

  /** A multi-line probe (engine.py run_probe): every line must come from the same completed iteration,
   * so the result is the deepest depth at which all `lines` reported an exact score. */
  async probe(req: SearchRequest & { lines: number; depth: number }): Promise<{ lines: ProbeLine[]; completed: number } | null> {
    const black = blackToMove(req.fen, req.moves.length);
    const byDepth = new Map<number, Map<number, Info>>();
    this.onLine = (line) => {
      const i = parseInfo(line);
      if (!i || !i.pv.length || !i.depth || i.bound) return;
      if (!byDepth.has(i.depth)) byDepth.set(i.depth, new Map());
      byDepth.get(i.depth)!.set(i.multipv, i);
    };
    this.send(`setoption name MultiPV value ${req.lines}`);
    this.position(req);
    this.send(`go depth ${req.depth} nodes ${req.nodes}${req.searchmoves ? ` searchmoves ${req.searchmoves.join(" ")}` : ""}`);
    await this.wait((l) => l.startsWith("bestmove"));
    this.onLine = null;
    this.send("setoption name MultiPV value 1");
    const complete = [...byDepth.entries()].filter(([, got]) => got.size === req.lines).map(([d]) => d);
    if (!complete.length) return null;
    const depth = Math.max(...complete);
    const lines = [...byDepth.get(depth)!.entries()].sort(([a], [b]) => a - b).map(([, i]) => {
      const w = white(i, black);
      return { uci: i.pv[0], score_cp: w.mate === null ? w.cp : null, mate: w.mate, wdl: w.wdl, depth: i.depth, pv: i.pv.slice(0, 4) };
    });
    return { lines, completed: depth };
  }

  terminate() { this.worker.terminate(); this.dead = new Error("engine stopped"); this.failAll(); }
}
