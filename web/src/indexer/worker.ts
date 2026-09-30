/// <reference lib="webworker" />
// The indexing worker: fetches a Chess.com history newest month first, runs ChessTrove's own Python indexer
// (src/chesstrove/indexing.py, the same file the server runs) in Pyodide, and uploads each month's rows to
// the API in batches. Everything CPU-heavy happens here, off the page's thread and off the server.

import { batches, fetchRetry, monthsToIndex, type FromWorker, type Start } from "./protocol";

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.28.3/full/";
const CHESS_ZIP = "/py/python-chess-1.11.2.zip"; // uv.lock's pin, zipped by scripts/build_browser_chess.sh
// The browser core: indexing.py and what it imports (tests/test_browser_import.py checks that list stays closed).
const CORE = import.meta.glob(
  ["../../../src/chesstrove/{__init__,models,reconstruction,indexing}.py",
   "../../../src/chesstrove/importers/{__init__,pgn,chesscom}.py", "../../../src/chesstrove/detectors/*.py"],
  { query: "?raw", import: "default", eager: true },
) as Record<string, string>;

const post = (m: FromWorker) => postMessage(m);
const timed = async <T>(name: string, fn: () => Promise<T> | T): Promise<T> => {
  const t = performance.now();
  try { return await fn(); } finally { post({ type: "timing", name, ms: performance.now() - t }); }
};

class Fatal extends Error {}

async function boot(): Promise<{ index: (archive: string) => string; versions: Record<string, number> }> {
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
  return { index: py.globals.get("ix").index_chesscom_archive, versions: JSON.parse(py.runPython("json.dumps(ix.versions())")) };
}

async function run(s: Start) {
  const p = { type: "progress" as const, phase: "loading" as "loading" | "listing" | "indexing" | "done", archivesTotal: 0,
              archivesDone: 0, month: null as string | null, analyzed: 0, uploaded: 0, events: 0, uploading: false };
  const progress = () => post({ ...p });
  progress();
  const core = await timed("pyodide", boot);
  if (JSON.stringify(core.versions) !== JSON.stringify(s.versions)) {
    throw new Fatal("This page is older than ChessTrove's server. Reload the page to continue.");
  }

  p.phase = "listing"; progress();
  const base = `https://api.chess.com/pub/player/${encodeURIComponent(s.username)}/games`;
  const list = await timed("fetch", () => fetchRetry(`${base}/archives`, undefined, "Chess.com"));
  if (list.status === 404) throw new Fatal(`Chess.com has no player called “${s.username}”.`);
  const current = new Date().toISOString().slice(0, 7).replace("-", "/");
  const months = monthsToIndex(((await list.json()).archives ?? []) as string[], s.monthsDone, current);
  p.archivesTotal = months.length; p.phase = "indexing"; progress();

  // Python blocks this thread while it analyses, so work in batch-sized chunks: while chunk n+1 is analysed,
  // chunk n's upload is already on the wire (a whole month at once kept uploads waiting behind analysis).
  const upload = async (month: string, result: { games: unknown[]; skipped: number; errors: unknown[] }, last: boolean) => {
    const body = JSON.stringify({ versions: s.versions, month, month_complete: last, games: result.games,
                                  skipped: result.skipped, errors: result.errors });
    p.uploading = true; progress();
    const r = await timed("upload", () => fetchRetry(`${s.apiBase}/api/indexing/${s.importId}/batches`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-Import-Token": s.token }, body }, "ChessTrove's server"));
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({})))?.detail ?? `error ${r.status}`;
      throw new Fatal(r.status === 409 ? String(detail) : `ChessTrove's server refused a batch (${detail}).`);
    }
    const ack = await r.json();
    p.uploaded += result.games.length; p.events += ack.events; p.uploading = false; progress();
    if (last) post({ type: "month", month });
  };

  let inflight: Promise<void> = Promise.resolve();
  for (const month of months) {
    p.month = month; progress();
    const res = await timed("fetch", () => fetchRetry(`${base}/${month}`, undefined, "Chess.com"));
    const archive = JSON.parse(await res.text()) as { games?: unknown[] };
    for (const chunk of batches(archive.games ?? [], s.batchSize)) {
      const result = JSON.parse(await timed("analyze", () => core.index(JSON.stringify({ games: chunk.games }))));
      p.analyzed += result.games.length; progress();
      await inflight; // one upload in flight: order kept, memory bounded
      inflight = upload(month, result, chunk.last);
    }
    p.archivesDone += 1; progress();
  }
  await inflight;
  const done = await fetchRetry(`${s.apiBase}/api/indexing/${s.importId}/finish`,
                                { method: "POST", headers: { "X-Import-Token": s.token } }, "ChessTrove's server");
  if (!done.ok) throw new Fatal("ChessTrove's server didn't accept the finished import.");
  p.phase = "done"; p.month = null; progress();
}

self.onmessage = (e: MessageEvent<Start>) => {
  run(e.data).catch((err) => post({ type: "error", message: err instanceof Error ? err.message : String(err), retry: !(err instanceof Fatal) }));
};
