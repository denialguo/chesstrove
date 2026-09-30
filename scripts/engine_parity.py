"""Parity check: the browser's archaeology (web/src/engine/archaeology.ts) against the server's
(archaeology.py), on the same native engine results. Every one of the six record-book discoveries must
come out with the same games, plies and ranking values. Needs node (esbuild comes with web/'s npm install).

Usage: uv run python scripts/engine_parity.py [--player danksonpotato] [--platform chesscom] [--limit 8]
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from chesstrove import archaeology, db

ROOT = Path(__file__).resolve().parents[1]
TYPES = ("biggest_comeback", "biggest_throw", "only_winning_move", "material_sacrifice", "longest_mate_found", "underpromotion")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--player", default="danksonpotato")
    ap.add_argument("--platform", default="chesscom")
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()
    with db.connect() as c:
        config = db.default_engine_config(c)
        games = [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in g.items()}
                 for g in db.engine_input(c, args.platform, args.player) if not g["chess960"]]
        ids = [g["id"] for g in games]
        results: dict[int, dict] = {}
        for p in c.execute("""SELECT game_id, position, score_cp, mate, wdl, best_uci, depth FROM engine_positions
                              WHERE config_id = %s AND game_id = ANY(%s) ORDER BY game_id, position""", (config["id"], ids)):
            results.setdefault(p["game_id"], {"positions": [], "probes": {}})["positions"].append(
                {"cp": p["score_cp"], "mate": p["mate"], "wdl": p["wdl"], "best": p["best_uci"], "depth": p["depth"]})
        for p in c.execute("""SELECT game_id, position, kind, moves, results FROM engine_move_probes
                              WHERE config_id = %s AND game_id = ANY(%s)""", (config["id"], ids)):
            if p["game_id"] in results:
                results[p["game_id"]]["probes"][f"{p['position']}:{p['kind']}"] = {
                    "kind": p["kind"], "moves": p["moves"], "results": p["results"], "budget": {}}
        # the browser engine skips Chess960 games (for now), so leave them out of the server's lists too
        chess960 = {r["id"] for r in c.execute("SELECT id FROM games WHERE chess960")}
        server = {t: [r for r in archaeology.discoveries(c, t, args.player, args.platform, config["id"], args.limit + 50)["results"]
                      if r["game"]["id"] not in chess960][: args.limit] for t in TYPES}
    complete = {g["id"] for g in games if len(results.get(g["id"], {}).get("positions", [])) == g["ply_count"] + 1}
    payload = {"player": args.player, "limit": args.limit, "games": [g for g in games if g["id"] in complete],
               "results": {g: r for g, r in results.items() if g in complete}}
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "parity.mjs"
        subprocess.run([str(ROOT / "web/node_modules/.bin/esbuild"), str(ROOT / "scripts/engine_parity.mjs"), "--bundle",
                        "--platform=node", "--format=esm", "--log-level=warning", f"--outfile={bundle}"], check=True)
        out = subprocess.run(["node", str(bundle)], input=json.dumps(payload), capture_output=True, text=True, check=True)
    browser = json.loads(out.stdout)
    failures = 0
    for t in TYPES:
        key = lambda r: (r["game"]["id"], r["ply"], r["score"]["value"] if isinstance(r["score"]["value"], str)  # noqa: E731
                         else round(float(r["score"]["value"]), 3))
        s, b = [key(r) for r in server[t]], [key(r) for r in browser[t]]
        # same scores in the same order, and the same moves within each score (exact ties may swap:
        # Postgres computes exp() in numeric, the browser in doubles)
        ok = [x[2] for x in s] == [x[2] for x in b] and sorted(s, key=str) == sorted(b, key=str)
        failures += not ok
        print(f"{'ok  ' if ok else 'DIFF'} {t:20} server {len(s)} browser {len(b)}")
        if not ok:
            for x, y in zip(s + [None] * len(b), b + [None] * len(s)):
                if x != y:
                    print(f"       server {x}  browser {y}")
    print(f"{len(complete)} games compared")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
