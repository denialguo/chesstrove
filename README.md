# ChessTrove

ChessTrove indexes your entire Chess.com/Lichess history (or any PGN) so you can uncover rare motifs, unusual
positions, engine insights, and recurring patterns across every game you've played.

Today: a deterministic index (move facts + rare-motif detectors). Planned: a persistent, resumable Stockfish
index over every position. Design and roadmap: [ARCHITECTURE.md](ARCHITECTURE.md).

```sh
uv sync
# No database setup: data goes to a built-in Postgres in ~/.chesstrove.
# To use your own server instead: export CHESSTROVE_DATABASE_URL=postgresql://...
uv run chesstrove import-pgn my_games.pgn   # re-running is safe: duplicates are skipped
uv run chesstrove import-chesscom myname   # first run fetches everything; later runs only new months
uv run chesstrove import-lichess myname    # same idea; export LICHESS_TOKEN=... to stream faster
uv run chesstrove imports
uv run chesstrove games --player myname
uv run chesstrove game 1                    # moves + events

uv run chesstrove detectors                 # what each detector finds, and its version
uv run chesstrove events --type UNDERPROMOTION
uv run chesstrove events --type MISSED_MATE_IN_ONE --player myname --since 2024-01-01
uv run chesstrove reanalyze                 # after bumping/adding a detector: redoes only what's stale
uv run chesstrove serve                     # REST API on 127.0.0.1:8000, interactive docs at /docs

# Engine layer (needs Stockfish: brew install stockfish). Resumable; Ctrl-C keeps finished games.
uv run chesstrove engine analyze --nodes 25000 --max-games 100   # newest games first
uv run chesstrove status                    # games, positions, deterministic + Stockfish progress

uv run pytest                              # spins up an embedded Postgres; no setup needed
uv run scripts/benchmark.py --db
```

## Hosting it publicly

Three pieces, so the site loads instantly even while the free API server is asleep:

| Address | What | Host |
|---|---|---|
| `https://chesstrove.tech` | the React app, static files only | Vercel |
| `https://api.chesstrove.tech` | the FastAPI backend (`/api/*`) | Render |
| (private) | Postgres | Supabase |

The browser calls the API directly (never through Vercel), and only the API knows the database.

1. **Supabase.** Create a project. Under **Connect**, copy the **Session pooler** connection string (port
   5432; Render can't reach the direct IPv6 address). Put Render in the same region as the project
   (`render.yaml` says Virginia, Supabase's default `us-east-1`).
2. **Copy your local data up** (optional): `scripts/push_db.sh 'postgresql://...'`. This replaces anything
   ChessTrove already stored there. Without it, run `CHESSTROVE_DATABASE_URL=... uv run chesstrove init-db`
   once.
3. **Render (API).** Go to **New → Blueprint** and pick this repo (it reads `render.yaml`). Environment:
   - `CHESSTROVE_DATABASE_URL`: the Session pooler string (the blueprint asks for it; it's the only secret).
   - `CHESSTROVE_PUBLIC=1`: set by the blueprint.

   Then **Settings → Custom Domains → Add** `api.chesstrove.tech`. Render shows the CNAME target to use.
4. **Vercel (site).** **Add New → Project**, import this repo, and set **Root Directory** to `web`. The
   framework, build command and `dist` output come from `web/vercel.json`, which also sends every app route
   (`/u/...`, `/g/...`) to `index.html`. Environment variable, for Production:
   - `VITE_API_URL=https://api.chesstrove.tech`

   It's baked into the public JavaScript, so it must never hold a secret; the database URL never goes
   to Vercel. Then **Settings → Domains → Add** `chesstrove.tech` and `www.chesstrove.tech`, and choose to
   redirect `www` to the bare domain.
5. **DNS** (at the registrar, e.g. Namify): add exactly the records Vercel shows for `chesstrove.tech` and
   `www`, and the CNAME Render shows for `api`. Remove any parking records the registrar added for `@` or
   `www`. Both hosts issue HTTPS certificates once the records resolve.

The API only answers browsers on `https://chesstrove.tech`, `https://www.chesstrove.tech` and local Vite
(`http://localhost:5173`, `http://127.0.0.1:5173`); see `CORS_ORIGINS` in `api.py`. Vercel preview
deployments get their own URLs, so they can load but can't reach the API unless one is added there.

Locally nothing changes: `npx vite` in `web/` proxies `/api` to `chesstrove serve`, and `npm run build`
still writes the app into the Python package, so `chesstrove serve` hosts both on one port.

`CHESSTROVE_PUBLIC=1` (set by the blueprint) makes three changes:
- It turns off PGN upload, reanalysis and the import list.
- It returns an account's running or just-finished import instead of starting a duplicate.
- It limits each IP to 10 imports an hour and runs 2 Chess.com imports and 1 Lichess import at a time (`CHESSTROVE_CHESSCOM_SLOTS`, `CHESSTROVE_LICHESS_SLOTS`).

Stockfish doesn't run on the host. Players analysed locally (`chesstrove engine analyze`) before being
pushed up show that record book. Everyone else gets an offer to analyse their games in their own browser
(Stockfish 18 WASM, results kept in the browser); see ARCHITECTURE.md, "Browser engine". Benchmark any
machine at `/lab/engine`.

Free-tier limits:
- Render sleeps after 15 idle minutes. The site itself still loads at once from Vercel; the first player or
  game page after that says it's waking the server, which takes about a minute.
- Supabase pauses a project after a week without traffic, and its database is capped at 500 MB, roughly
  25k more games beyond the current data.
