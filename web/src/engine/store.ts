// This browser's engine index, in IndexedDB. Nothing here is sent to ChessTrove's server.
// Keys: `${config}|${platform}|${user}|${gameId}` for a game's results (written once per finished game or
// probe, so a closed tab loses at most the game in flight), `${config}|${platform}|${user}` for the state.

import type { GameResults } from "./archaeology";
import type { ProbeRequest } from "./archaeology";

export interface StoredGame extends GameResults { gameId: number; pending: ProbeRequest[] }
export interface RunState { optedIn: boolean; paused: boolean; speed: Speed; startedAt: string }
export type Speed = "balanced" | "fast" | "max";

const DB = "chesstrove-engine";
let opening: Promise<IDBDatabase> | null = null;

function open(): Promise<IDBDatabase> {
  opening ??= new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore("games");
      req.result.createObjectStore("state");
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => { opening = null; reject(req.error); };
  });
  return opening;
}

function tx<T>(store: string, mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return open().then((db) => new Promise<T>((resolve, reject) => {
    const r = fn(db.transaction(store, mode).objectStore(store));
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  }));
}

export const playerKey = (config: string, platform: string, user: string) => `${config}|${platform}|${user.toLowerCase()}`;

export const store = {
  games: (player: string) =>
    tx<StoredGame[]>("games", "readonly", (s) => s.getAll(IDBKeyRange.bound(`${player}|`, `${player}|￿`))),
  putGame: (player: string, g: StoredGame) => tx("games", "readwrite", (s) => s.put(g, `${player}|${g.gameId}`)),
  state: (player: string) => tx<RunState | undefined>("state", "readonly", (s) => s.get(player)),
  putState: (player: string, st: RunState) => tx("state", "readwrite", (s) => s.put(st, player)),
  clear: async (player: string) => {
    await tx("games", "readwrite", (s) => s.delete(IDBKeyRange.bound(`${player}|`, `${player}|￿`)));
    await tx("state", "readwrite", (s) => s.delete(player));
  },
};
