// Indexing a history on this device: owns the worker (worker.ts), talks to the API to start or carry on, and
// keeps the session and progress in IndexedDB so a reload or a closed tab can continue instead of starting over.

import { api, apiUrl, type IndexingSession } from "../lib/api";
import type { FromWorker, Progress, Start } from "./protocol";

const BATCH = 100; // games per upload: ~0.5 MB, one short transaction server-side

export interface Saved { importId: number; token: string; versions: string; analyzed: number; uploaded: number; events: number; months: string[] }
export interface State extends Omit<Progress, "type"> {
  status: "idle" | "running" | "paused" | "watching" | "done" | "error";
  error: string | null; retry: boolean; saved: Saved | null;
}

/** Whether this browser can index at all (a module worker with WebAssembly). */
export const supported = () => typeof Worker !== "undefined" && typeof WebAssembly === "object" && typeof indexedDB !== "undefined";

// --- IndexedDB: one record per player; the analyzer versions are in it, so older results never pass for current
const DB = "chesstrove-indexing";
let opening: Promise<IDBDatabase> | null = null;
function db(): Promise<IDBDatabase> {
  opening ??= new Promise((resolve, reject) => {
    const r = indexedDB.open(DB, 1);
    r.onupgradeneeded = () => r.result.createObjectStore("sessions");
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => { opening = null; reject(r.error); };
  });
  return opening;
}
async function idb<T>(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest): Promise<T | undefined> {
  try {
    const d = await db();
    return await new Promise((resolve, reject) => {
      const r = fn(d.transaction("sessions", mode).objectStore("sessions"));
      r.onsuccess = () => resolve(r.result as T);
      r.onerror = () => reject(r.error);
    });
  } catch { return undefined; } // private windows and blocked storage: indexing still works, it just can't resume
}
const key = (user: string) => `chesscom|${user.toLowerCase()}`;
export const loadSaved = (user: string) => idb<Saved>("readonly", (s) => s.get(key(user)));
const save = (user: string, v: Saved) => idb("readwrite", (s) => s.put(v, key(user)));
const forget = (user: string) => idb("readwrite", (s) => s.delete(key(user)));

export class Indexer {
  state: State = { status: "idle", phase: "loading", archivesTotal: 0, archivesDone: 0, month: null, analyzed: 0, uploaded: 0,
                   events: 0, uploading: false, error: null, retry: false, saved: null };
  private worker: Worker | null = null;
  private listeners = new Set<() => void>();

  constructor(readonly username: string) {}

  subscribe(fn: () => void) { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; }
  private set(patch: Partial<State>) { this.state = { ...this.state, ...patch }; this.listeners.forEach((fn) => fn()); }

  async init(): Promise<Saved | null> {
    const saved = (await loadSaved(this.username)) ?? null;
    this.set({ saved });
    return saved;
  }

  /** Start, or carry on with this browser's earlier session. Returns the server's answer. */
  async start(): Promise<IndexingSession["mode"]> {
    this.set({ status: "running", error: null, phase: "loading" });
    const saved = this.state.saved;
    let session: IndexingSession;
    try {
      session = await api.startIndexing(this.username, saved ? { import_id: saved.importId, token: saved.token } : undefined);
    } catch (e) {
      this.set({ status: "error", error: e instanceof Error && /429/.test(e.message)
        ? "Too many imports from your connection in the last hour. Try again later." : "ChessTrove's server didn't answer.", retry: true });
      return "watch";
    }
    if (session.mode !== "index") {
      this.set({ status: session.mode === "done" ? "done" : "watching" });
      return session.mode;
    }
    const versions = JSON.stringify(session.versions);
    const same = saved && saved.importId === session.import_id && saved.versions === versions;
    const base: Saved = same ? saved! : { importId: session.import_id, token: session.token, versions, analyzed: 0, uploaded: 0, events: 0, months: [] };
    await save(this.username, base);
    this.set({ saved: base, analyzed: base.analyzed, uploaded: base.uploaded, events: base.events });

    const w = new Worker(new URL("./worker.ts", import.meta.url), { type: "module" });
    this.worker = w;
    let counted = { analyzed: 0, uploaded: 0, events: 0 }; // this worker's own counts, added to what was saved
    w.onmessage = async (e: MessageEvent<FromWorker>) => {
      const m = e.data;
      if (m.type === "progress") {
        counted = { analyzed: m.analyzed, uploaded: m.uploaded, events: m.events };
        const { type: _, ...rest } = m;
        this.set({ ...rest, analyzed: base.analyzed + m.analyzed, uploaded: base.uploaded + m.uploaded, events: base.events + m.events,
                   status: m.phase === "done" ? "done" : "running" });
        if (m.phase === "done") { this.stopWorker(); await forget(this.username); }
      } else if (m.type === "month") { // acknowledged by the server: only now does it count as done here
        const next = { ...base, months: [...base.months, m.month], analyzed: base.analyzed + counted.analyzed,
                       uploaded: base.uploaded + counted.uploaded, events: base.events + counted.events };
        await save(this.username, next);
        this.set({ saved: next });
      } else if (m.type === "timing") { // visible to benchmarks as performance measures (ct-index:pyodide, …)
        const end = performance.now();
        performance.measure(`ct-index:${m.name}`, { start: end - m.ms, end });
      } else {
        this.stopWorker();
        this.set({ status: "error", error: m.message, retry: m.retry, uploading: false });
      }
    };
    w.onerror = (e) => { this.stopWorker(); this.set({ status: "error", error: e.message || "The indexer stopped.", retry: true }); };
    const start: Start = { type: "start", username: this.username, apiBase: apiUrl("").replace(/\/api$/, ""), importId: session.import_id,
                           token: session.token, monthsDone: session.months_done, versions: session.versions, batchSize: BATCH };
    w.postMessage(start);
    return "index";
  }

  /** Stop now; the server keeps what it acknowledged, and continue() picks up from the next month. */
  pause() { this.stopWorker(); this.set({ status: "paused", uploading: false }); }

  private stopWorker() { this.worker?.terminate(); this.worker = null; }
  dispose() { this.stopWorker(); this.listeners.clear(); }
}
