// Benchmark the browser core in Node's Pyodide: node checks/bench-core.mjs <src/chesstrove dir> <archives.json> <fast|deep>
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { loadPyodide } from "pyodide";
const [src, archivesPath, mode] = process.argv.slice(2);
const files = ["__init__.py", "models.py", "reconstruction.py", "indexing.py", "importers/__init__.py", "importers/pgn.py", "importers/chesscom.py",
  ...readdirSync(join(src, "detectors")).filter((f) => f.endsWith(".py")).map((f) => `detectors/${f}`)];
const py = await loadPyodide();
py.unpackArchive(new Uint8Array(readFileSync("public/py/python-chess-1.11.2.zip")), "zip", { extractDir: "/home/pyodide" });
for (const f of files) { const d = `/home/pyodide/chesstrove/${f}`; py.FS.mkdirTree(d.slice(0, d.lastIndexOf("/"))); py.FS.writeFile(d, readFileSync(join(src, f), "utf8")); }
py.runPython("import json, chesstrove.indexing as ix");
const ix = py.globals.get("ix");
const archives = JSON.parse(readFileSync(archivesPath, "utf8"));
let t = performance.now(), games = 0, events = 0; const stored = [];
for (const a of Object.values(archives)) {
  const r = JSON.parse(ix.index_chesscom_archive(JSON.stringify(a)));
  games += r.games.length; events += r.games.reduce((n, g) => n + g.events.length, 0);
  if (mode === "deep") stored.push(...r.games.map((g) => ({ source_key: g.source_key, initial_fen: g.initial_fen, chess960: g.chess960, uci: g.moves ? g.moves.uci : "" })));
}
const first = performance.now() - t;
let deep = null;
if (mode === "deep") { t = performance.now(); const d = JSON.parse(ix.deep_scan(JSON.stringify(stored))); deep = { ms: Math.round(performance.now() - t), events: d.reduce((n, g) => n + g.events.length, 0) }; }
console.log(JSON.stringify({ src: src.includes("oldsrc") ? "committed" : "new", games, first_pass_ms: Math.round(first), ms_per_game: +(first / games).toFixed(2), first_pass_events: events, deep }));
