// The split deployment: API URLs from VITE_API_URL, and Vercel serving index.html for app routes.
// Run: npm run check
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { joinApi } from "../src/lib/api";

assert.equal(joinApi("", "/players/chesscom/alice"), "/api/players/chesscom/alice"); // local: same origin
assert.equal(joinApi("https://api.chesstrove.tech", "/games/1?engine=true"), "https://api.chesstrove.tech/api/games/1?engine=true");
assert.equal(joinApi("https://api.chesstrove.tech/", "/status"), "https://api.chesstrove.tech/api/status");

const vercel = JSON.parse(readFileSync("vercel.json", "utf8"));
assert.equal(vercel.outputDirectory, "dist");
assert.deepEqual(vercel.rewrites, [{ source: "/(.*)", destination: "/index.html" }]); // /u/..., /g/... load the app
assert.ok(!JSON.stringify(vercel).includes("/api"), "the API is called directly, never proxied through Vercel");
console.log("deploy check ok");
