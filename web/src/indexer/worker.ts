/// <reference lib="webworker" />
// The indexing worker. First pass: fetches a Chess.com history newest month first, runs ChessTrove's own Python
// indexer (src/chesstrove/indexing.py, the same file the server runs) in Pyodide, and uploads the rows in batches.
// Deep pass: runs the DEEP detectors over games the server already has. Everything CPU-heavy happens here, off the
// page's thread and off the server, in short slices with rests between them (DUTY), so one core isn't pinned.

import { batches, fetchRetry, monthsToIndex, restFor, type FromWorker, type Start, type StartDeep } from "./protocol";

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.28.3/full/";
const CHESS_ZIP = "/py/python-chess-1.11.2.zip"; // uv.lock's pin, zipped by scripts/build_browser_chess.sh
// The browser core: indexing.py and what it imports (tests/test_browser_import.py checks that list stays closed).
const CORE = import.meta.glob(
  ["../../../src/chesstrove/{__init__,models,reconstruction,indexing}.py",
   "../../../src/chesstrove/importers/{__init__,pgn,chesscom}.py", "../../../src/chesstrove/detectors/*.py"],
  { query: "?raw", import: "default", eager: true },
) as Record<string, string>;

const post = (m: FromWorker) => postMessage(m);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const timed = async <T>(name: string, fn: () => Promise<T> | T): Promise<T> => {
  const t = performance.now();
  try { return await fn(); } finally { post({ type: "timing", name, ms: performance.now() - t }); }
};

class Fatal extends Error {}

interface Core { index: (archive: string) => string; deep: (games: string) => string; fast: Record<string, number>; deepVersions: Record<string, number> }

async function boot(): Promise<Core> {
  const { loadPyodide } = await import(/* @vite-ignore */ `${PYODIDE}pyodide.mjs`);
  const py = await loadPyodide({ indexURL: PYODIDE });
  const home = "/home/pyodide"; // on sys.path
  py.unpackArchive(await (await fetchRetry(CHESS_ZIP, undefined, "ChessTrove")).arrayBuffer(), "zip", { extractDir: home });
  for (const [path, text] of Object.entries(CORE)) {
    const file = `${home}/chesstrove/${path.split("/src/chesstrove/")[1]}`;
    py.FS.mkdirTree(file.slice(0, file.lastIndexOf("/")));
    py.FS.writeFile(file, text);
  }
  py.runPython("import json, chesstrove.indexing as ix");
  const ix = py.globals.get("ix");
  return { index: ix.index_chesscom_archive, deep: ix.deep_scan,
           fast: JSON.parse(py.runPython("json.dumps(ix.versions(ix.FAST))")),
           deepVersions: JSON.parse(py.runPython("json.dumps(ix.versions(ix.DEEP))")) };
}

/** One slice of Python work, then the rest that keeps the worker to its duty cycle. */
async function slice<T>(duty: number, fn: () => T): Promise<T> {
  const t = performance.now();
  const out = await timed("analyze", fn);
  const rest = restFor(performance.now() - t, duty);
  if (rest > 0) await timed("rest", () => sleep(rest));
  return out;
}

const refused = async (r: Response) => {
  const detail = (await r.json().catch(() => ({})))?.detail ?? `error ${r.status}`;
  return new Fatal(r.status === 409 ? String(detail) : `ChessTrove's server refused a batch (${detail}).`);
};
const sameVersions = (a: Record<string, number>, b: Record<string, number>) => JSON.stringify(a) === JSON.stringify(b);
const OUTDATED = "This page is older than ChessTrove's server. Reload the page to continue.";

type Chunk = { games: unknown[]; skipped: number; errors: unknown[] };

async function firstPass(s: Start) {
  const p = { type: "progress" as const, phase: "loading" as "loading" | "listing" | "indexing" | "done",
              archivesTotal: 0, archivesDone: 0, month: null as string | null, analyzed: 0, uploaded: 0, events: 0, uploading: false };
  const progress = () => post({ ...p });
  progress();
  const core = await timed("pyodide", boot);
  if (!sameVersions(core.fast, s.versions)) throw new Fatal(OUTDATED);

  p.phase = "listing"; progress();
  const base = `https://api.chess.com/pub/player/${encodeURIComponent(s.username)}/games`;
  const list = await timed("fetch", () => fetchRetry(`${base}/archives`, undefined, "Chess.com"));
  if (list.status === 404) throw new Fatal(`Chess.com has no player called “${s.username}”.`);
  const current = new Date().toISOString().slice(0, 7).replace("-", "/");
  const months = monthsToIndex(((await list.json()).archives ?? []) as string[], s.monthsDone, current);
  p.archivesTotal = months.length; p.phase = "indexing"; progress();

  const upload = async (month: string, c: Chunk, last: boolean) => {
    const body = JSON.stringify({ versions: s.versions, month, month_complete: last, games: c.games, skipped: c.skipped, errors: c.errors });
    p.uploading = true; progress();
    const r = await timed("upload", () => fetchRetry(`${s.apiBase}/api/indexing/${s.importId}/batches`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-Import-Token": s.token }, body }, "ChessTrove's server"));
    if (!r.ok) throw await refused(r);
    const ack = await r.json();
    p.uploaded += c.games.length; p.events += ack.events; p.uploading = false; progress();
    if (last) post({ type: "month", month });
  };

  // Analyse in small slices (short bursts, rests between), upload in batches; one upload in flight, overlapping the
  // next slices. Python blocks this thread while it runs, so the slices are also what lets uploads progress.
  let inflight: Promise<void> = Promise.resolve();
  for (const month of months) {
    p.month = month; progress();
    const res = await timed("fetch", () => fetchRetry(`${base}/${month}`, undefined, "Chess.com"));
    const archive = JSON.parse(await res.text()) as { games?: unknown[] };
    let pending: Chunk = { games: [], skipped: 0, errors: [] };
    for (const part of batches(archive.games ?? [], s.slice)) {
      const r: Chunk = JSON.parse(await slice(s.duty, () => core.index(JSON.stringify({ games: part.games }))));
      p.analyzed += r.games.length; progress();
      pending = { games: [...pending.games, ...r.games], skipped: pending.skipped + r.skipped, errors: [...pending.errors, ...r.errors] };
      if (pending.games.length >= s.batchSize || part.last) {
        await inflight;
        inflight = upload(month, pending, part.last);
        pending = { games: [], skipped: 0, errors: [] };
      }
    }
    p.archivesDone += 1; progress();
  }
  await inflight;
  const done = await fetchRetry(`${s.apiBase}/api/indexing/${s.importId}/finish`,
                                { method: "POST", headers: { "X-Import-Token": s.token } }, "ChessTrove's server");
  if (!done.ok) throw new Fatal("ChessTrove's server didn't accept the finished import.");
  p.phase = "done"; p.month = null; progress();
}

async function deepPass(s: StartDeep) {
  const p = { type: "progress" as const, phase: "loading" as "loading" | "deep" | "done", archivesTotal: 0, archivesDone: 0,
              month: null, analyzed: 0, uploaded: 0, events: 0, uploading: false };
  const progress = () => post({ ...p });
  progress();
  const core = await timed("pyodide", boot);
  if (!sameVersions(core.deepVersions, s.versions)) throw new Fatal(OUTDATED);
  p.phase = "deep"; progress();
  const auth = { "X-Import-Token": s.token };
  for (;;) {
    const r = await timed("fetch", () => fetchRetry(`${s.apiBase}/api/indexing/deep/${s.runId}/games`, { headers: auth }, "ChessTrove's server"));
    if (!r.ok) throw await refused(r);
    const games = (await r.json()) as unknown[];
    if (!games.length) break;
    let found: unknown[] = [];
    for (const part of batches(games, s.slice)) {
      found = [...found, ...JSON.parse(await slice(s.duty, () => core.deep(JSON.stringify(part.games))))];
      p.analyzed += part.games.length; progress();
    }
    p.uploading = true; progress();
    const ack = await timed("upload", () => fetchRetry(`${s.apiBase}/api/indexing/deep/${s.runId}/batches`, {
      method: "POST", headers: { "Content-Type": "application/json", ...auth }, body: JSON.stringify({ versions: s.versions, games: found }) },
      "ChessTrove's server"));
    if (!ack.ok) throw await refused(ack);
    p.uploaded += games.length; p.events += (await ack.json()).events; p.uploading = false; progress();
  }
  await fetchRetry(`${s.apiBase}/api/indexing/deep/${s.runId}/finish`, { method: "POST", headers: auth }, "ChessTrove's server");
  p.phase = "done"; progress();
}

self.onmessage = (e: MessageEvent<Start | StartDeep>) => {
  (e.data.type === "deep" ? deepPass(e.data) : firstPass(e.data))
    .catch((err) => post({ type: "error", message: err instanceof Error ? err.message : String(err), retry: !(err instanceof Fatal) }));
};
