"""Write the browser-engine benchmark fixture (web/public/lab/bench.json) from the local native index.

The fixture is a fixed sample of one player's games in the engine-input shape, with the native Stockfish
results for every position (and the native two-line searches), so /lab/engine can measure the browser
engine's speed and its agreement with native analysis on any machine. Games: every game behind one of the
player's native record-book discoveries, plus a seeded random sample.

Usage: uv run python scripts/engine_fixture.py [--player danksonpotato] [--platform chesscom] [--random 60]
"""

import argparse
import json
import random
from pathlib import Path

from chesstrove import archaeology, db

OUT = Path(__file__).resolve().parents[1] / "web" / "public" / "lab" / "bench.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--player", default="danksonpotato")
    ap.add_argument("--platform", default="chesscom")
    ap.add_argument("--random", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    with db.connect() as c:
        config = db.default_engine_config(c)
        wanted: set[int] = set()
        for t in ("biggest_throw", "biggest_comeback", "only_winning_move", "material_sacrifice", "longest_mate_found", "underpromotion"):
            found = archaeology.discoveries(c, t, args.player, args.platform, config["id"], limit=8)["results"]
            wanted |= {r["game"]["id"] for r in found}
        games = [g for g in db.engine_input(c, args.platform, args.player) if not g["chess960"]]
        rng = random.Random(args.seed)
        wanted |= {g["id"] for g in rng.sample([g for g in games if g["id"] not in wanted], args.random)}
        chosen = [g for g in games if g["id"] in wanted]
        ids = [g["id"] for g in chosen]
        positions = c.execute(
            """SELECT game_id, position, score_cp, mate, wdl, best_uci, depth FROM engine_positions
               WHERE config_id = %s AND game_id = ANY(%s) ORDER BY game_id, position""", (config["id"], ids)).fetchall()
        probes = c.execute(
            """SELECT game_id, position, kind, results FROM engine_move_probes
               WHERE config_id = %s AND game_id = ANY(%s) AND kind = 'top_two'""", (config["id"], ids)).fetchall()
    native: dict[int, list] = {}
    for p in positions:
        native.setdefault(p["game_id"], []).append(
            {"cp": p["score_cp"], "mate": p["mate"], "wdl": p["wdl"], "best": p["best_uci"], "depth": p["depth"]})
    tops: dict[int, dict] = {}
    for p in probes:
        tops.setdefault(p["game_id"], {})[f"{p['position']}:top_two"] = p["results"]
    fixture = {
        "player": args.player, "platform": args.platform,
        "native": {"engine": config["engine_name"], config["limit_kind"]: config["limit_value"], "hash_mb": config["hash_mb"]},
        "games": [{**{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in g.items()},
                   "native": native.get(g["id"], []), "native_top_two": tops.get(g["id"], {})}
                  for g in chosen if len(native.get(g["id"], [])) == g["ply_count"] + 1],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixture, separators=(",", ":")))
    print(f"{len(fixture['games'])} games, {sum(g['ply_count'] + 1 for g in fixture['games']):,} positions -> {OUT} "
          f"({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
