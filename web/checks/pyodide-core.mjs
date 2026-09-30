// Run ChessTrove's browser core the way the indexing worker does (Pyodide + the zipped python-chess + the files
// worker.ts bundles), but in Node, offline. Reads a Chess.com archive (JSON) on stdin, writes
// indexing.index_chesscom_archive's output to stdout (or, with `deep`, reads deep_scan's input and writes its output). tests/test_browser_import.py compares it with native Python.
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { loadPyodide } from "pyodide";

const web = join(dirname(fileURLToPath(import.meta.url)), "..");
const worker = readFileSync(join(web, "src/indexer/worker.ts"), "utf8");
// the same files the worker bundles: its import.meta.glob patterns, expanded here ({a,b} and *.py)
const patterns = [...worker.slice(worker.indexOf("import.meta.glob(")).split("{ query")[0].matchAll(/"(\.\.\/\.\.\/\.\.\/src\/chesstrove\/[^"]+)"/g)].map((m) => m[1]);
const src = join(web, "src/indexer");
const files = patterns.flatMap((p) => {
  const m = p.match(/^(.*?)\{([^}]*)\}(.*)$/);
  const expanded = m ? m[2].split(",").map((x) => m[1] + x + m[3]) : [p];
  return expanded.flatMap((f) => f.endsWith("/*.py")
    ? readdirSync(join(src, f.slice(0, -5))).filter((n) => n.endsWith(".py")).map((n) => f.slice(0, -4) + n) : [f]);
});

const py = await loadPyodide();
const home = "/home/pyodide";
py.unpackArchive(new Uint8Array(readFileSync(join(web, "public/py/python-chess-1.11.2.zip"))), "zip", { extractDir: home });
for (const f of files) {
  const dest = `${home}/chesstrove/${f.split("/src/chesstrove/")[1]}`;
  py.FS.mkdirTree(dest.slice(0, dest.lastIndexOf("/")));
  py.FS.writeFile(dest, readFileSync(join(src, f), "utf8"));
}
py.runPython("import chesstrove.indexing as ix");
const chunks = [];
for await (const c of process.stdin) chunks.push(c); // readFileSync(0) fails on a non-blocking pipe
const input = Buffer.concat(chunks).toString("utf8");
const ix = py.globals.get("ix"); // `deep` argument: the deep pass (indexing.deep_scan) instead of the first
process.stdout.write(process.argv[2] === "deep" ? ix.deep_scan(input) : ix.index_chesscom_archive(input));
