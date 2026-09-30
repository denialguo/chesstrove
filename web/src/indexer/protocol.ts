// Messages between the page and the indexing worker (worker.ts).

export interface Start {
  type: "start"; username: string; apiBase: string; importId: number; token: string;
  monthsDone: string[]; versions: Record<string, number>; batchSize: number;
}
export interface Progress {
  type: "progress";
  phase: "loading" | "listing" | "indexing" | "done";
  archivesTotal: number; archivesDone: number; month: string | null;
  analyzed: number; uploaded: number; events: number; uploading: boolean;
}
export type FromWorker =
  | Progress
  | { type: "month"; month: string }            // a month the server has acknowledged in full
  | { type: "error"; message: string; retry: boolean }
  | { type: "timing"; name: string; ms: number }; // for benchmarks: pyodide, fetch, analyze, upload

/** Chess.com archive months, newest first; months already stored are skipped, except the current one, which is
 *  still being played. */
export function monthsToIndex(archiveUrls: string[], done: string[], current: string): string[] {
  const skip = new Set(done.filter((m) => m < current));
  return archiveUrls.map((u) => u.split("/games/")[1]).filter((m) => m && !skip.has(m)).sort().reverse();
}

/** A month's games in upload order: batches of `size`; the last completes the month (an empty month is one
 *  empty batch, so it's still recorded as done). */
export function batches<T>(games: T[], size: number): { games: T[]; last: boolean }[] {
  const out = [];
  for (let i = 0; i < games.length; i += size) out.push({ games: games.slice(i, i + size), last: i + size >= games.length });
  return out.length ? out : [{ games: [], last: true }];
}

/** fetch with retries for what passes: rate limits, server errors, a sleeping or redeploying server, a dropped
 *  connection (backing off to 30 s, about 3 minutes in all). Anything else (4xx) is returned for the caller. */
export async function fetchRetry(url: string, init: RequestInit | undefined, what: string,
                                 get: typeof fetch = fetch, sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))): Promise<Response> {
  let wait = 2000;
  for (let attempt = 0; ; attempt++) {
    try {
      const r = await get(url, init);
      if (r.ok || (r.status < 500 && r.status !== 429 && r.status !== 408)) return r;
    } catch { /* network: retry */ }
    if (attempt === 7) throw new Error(`${what} didn't answer`);
    await sleep(wait);
    wait = Math.min(wait * 2, 30_000);
  }
}
