// The split deployment: API URLs from VITE_API_URL, and Vercel serving index.html for app routes.
// Run: npm run check
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { joinApi, wakeFetch } from "../src/lib/api";

assert.equal(joinApi("", "/players/chesscom/alice"), "/api/players/chesscom/alice"); // local: same origin
assert.equal(joinApi("https://api.chesstrove.tech", "/games/1?engine=true"), "https://api.chesstrove.tech/api/games/1?engine=true");
assert.equal(joinApi("https://api.chesstrove.tech/", "/status"), "https://api.chesstrove.tech/api/status");

const vercel = JSON.parse(readFileSync("vercel.json", "utf8"));
assert.equal(vercel.outputDirectory, "dist");
assert.deepEqual(vercel.rewrites, [{ source: "/(.*)", destination: "/index.html" }]); // /u/..., /g/... load the app
assert.ok(!JSON.stringify(vercel).includes("/api"), "the API is called directly, never proxied through Vercel");
// a waking server: dropped connections and 502/503/504 are waited out; real answers come straight back
const replies = (...rs: (number | "drop")[]) => {
  let i = 0;
  return (async () => { const r = rs[i++]; if (r === "drop") throw new TypeError("network"); return new Response(null, { status: r }); }) as typeof fetch;
};
const noSleep = async () => {};
assert.equal((await wakeFetch("u", undefined, replies("drop", 502, 503, "drop", 504, 200), noSleep)).status, 200);
for (const status of [404, 422, 429, 500]) assert.equal((await wakeFetch("u", undefined, replies(status, 200), noSleep)).status, status);
assert.equal((await wakeFetch("u", undefined, replies(...Array(20).fill(503)), noSleep, 5000)).status, 503); // gives up eventually
console.log("deploy check ok");
