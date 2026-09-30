// Node side of scripts/engine_parity.py: reads {player, games, results} as JSON on stdin, runs the browser's
// archaeology (web/src/engine/archaeology.ts, bundled by esbuild) and prints its discoveries as JSON.
import { discoveries, unpack } from "../web/src/engine/archaeology.ts";

let text = "";
process.stdin.on("data", (d) => (text += d));
process.stdin.on("end", () => {
  const { player, games, results, limit } = JSON.parse(text);
  const analysed = games.map((g) => ({ g: unpack(g, player), r: results[g.id] })).filter((x) => x.r);
  process.stdout.write(JSON.stringify(discoveries(analysed, limit)));
});
