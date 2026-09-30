// The indexing worker's pure parts: which months, in what order, in what batches, and retrying uploads.
// Run: npm run check
import assert from "node:assert/strict";
import { batches, DUTY, fetchRetry, monthsToIndex, restFor } from "../src/indexer/protocol";
import { speed } from "../src/indexer/indexer";

const url = (m: string) => `https://api.chess.com/pub/player/alice/games/${m}`;
const archives = ["2023/11", "2024/01", "2024/02", "2024/03"].map(url);
// newest first; finished months skipped; the current month redone even if marked done (games still arrive)
assert.deepEqual(monthsToIndex(archives, ["2024/01", "2024/03"], "2024/03"), ["2024/03", "2024/02", "2023/11"]);
assert.deepEqual(monthsToIndex(archives, [], "2024/03"), ["2024/03", "2024/02", "2024/01", "2023/11"]);

// batches: the last one completes the month; an empty month still gets one batch so it's recorded
assert.deepEqual(batches([1, 2, 3, 4, 5], 2), [{ games: [1, 2], last: false }, { games: [3, 4], last: false }, { games: [5], last: true }]);
assert.deepEqual(batches([], 100), [{ games: [], last: true }]);

// uploads: a sleeping server (502/503) and a dropped connection are retried; a refusal (4xx) comes straight back
const replies = (...rs: (number | "drop")[]) => {
  let i = 0;
  return (async () => { const r = rs[i++]; if (r === "drop") throw new TypeError("network"); return new Response(null, { status: r }); }) as typeof fetch;
};
const noSleep = async () => {};
assert.equal((await fetchRetry("u", undefined, "x", replies(502, "drop", 503, 200), noSleep)).status, 200);
assert.equal((await fetchRetry("u", undefined, "x", replies(422, 200), noSleep)).status, 422);
assert.equal((await fetchRetry("u", undefined, "x", replies(429, 200), noSleep)).status, 200);
await assert.rejects(fetchRetry("u", undefined, "ChessTrove", replies(...Array(8).fill(503)), noSleep), /didn't answer/);
// pacing: balanced by default (nothing saved), and a slice's rest keeps the worker to its duty cycle
assert.equal(speed(), "balanced");
assert.equal(DUTY.balanced, 0.6);
assert.equal(Math.round(restFor(120, DUTY.balanced)), 80); // 120 ms of work, 80 ms of rest: 60% busy
assert.equal(restFor(120, DUTY.fast), 0);
assert.ok(DUTY.deep < DUTY.balanced && DUTY.gentle < DUTY.balanced);
console.log("indexer check ok");
