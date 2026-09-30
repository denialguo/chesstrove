import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { ArrowUpDown, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, ExternalLink } from "lucide-react";
import { Board } from "../components/Board";
import { Digits } from "../components/Digits";
import { Unreachable, Waiting } from "../components/Waiting";
import { TopBar } from "../components/TopBar";
import { api, ApiError, PLATFORM_NAME, sourceUrl, type EnginePosition, type GameDetail, type Platform } from "../lib/api";
import { formatDate, moveLabel, timeControl } from "../lib/format";
import { FORM_HELP, formBadge, formNote, motifInfo } from "../lib/motifs";

const START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

/** White's win chance (0..1) on Lichess's human-calibrated curve. */
function whiteChance(p: EnginePosition | undefined, whiteToMove: boolean): number | null {
  if (!p) return null;
  if (p.mate !== null) return p.mate === 0 ? (whiteToMove ? 0 : 1) : p.mate > 0 ? 1 : 0;
  return 1 / (1 + Math.exp(-0.00368208 * (p.score_cp ?? 0)));
}

export function Game() {
  const { gameId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const [game, setGame] = useState<GameDetail | null>(null);
  const [error, setError] = useState<"missing" | "down" | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [flipped, setFlipped] = useState(params.get("o") === "black");
  const movesRef = useRef<HTMLOListElement>(null);
  const ply = Math.max(0, Math.min(Number(params.get("ply") ?? 0) || 0, game?.moves.length ?? 0));

  useEffect(() => {
    setError(null);
    api.game(gameId).then(setGame).catch((e) => setError(e instanceof ApiError && e.status === 404 ? "missing" : "down"));
  }, [gameId, attempt]);

  const go = (next: number) => {
    if (!game) return;
    const clamped = Math.max(0, Math.min(next, game.moves.length));
    const p = new URLSearchParams(params);
    p.set("ply", String(clamped));
    setParams(p, { replace: true });
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      if (e.key === "ArrowLeft") go(ply - 1);
      else if (e.key === "ArrowRight") go(ply + 1);
      else if (e.key === "Home") go(0);
      else if (e.key === "End") go(game?.moves.length ?? 0);
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // keep the current move in view inside the scrolling move list (not the page)
  useEffect(() => {
    const list = movesRef.current;
    const row = list?.querySelector<HTMLElement>(".is-current")?.closest("li");
    // scroll by whole rows so no row is ever sliced at the top edge; the current row sits mid-list
    if (list && row) list.scrollTop = row.offsetTop - Math.floor(list.clientHeight / row.offsetHeight / 2) * row.offsetHeight;
  }, [ply, game]);

  const eventsByPly = useMemo(() => {
    const map = new Map<number, string[]>();
    game?.events.forEach((e) => map.set(e.ply, [...(map.get(e.ply) ?? []), e.type]));
    return map;
  }, [game]);

  if (error === "missing") return <Shell><div className="empty"><h1>No game with that number.</h1></div></Shell>;
  if (error) return <Shell><Unreachable onRetry={() => setAttempt((a) => a + 1)} /></Shell>;
  if (!game) return <Shell><Waiting>Setting up the board…</Waiting></Shell>;

  const move = ply > 0 ? game.moves[ply - 1] : null;
  const fen = move ? move.fen_after : game.initial_fen ?? START;
  const whiteToMove = fen.split(" ")[1] === "w";
  const chance = whiteChance(game.engine_positions?.find((p) => p.position === ply), whiteToMove);
  const platform = game.source_key.split(":")[0] as Platform;
  const url = sourceUrl(game);
  const orientation = flipped ? "black" : "white";
  const top = flipped ? { name: game.white, rating: game.white_rating } : { name: game.black, rating: game.black_rating };
  const bottom = flipped ? { name: game.black, rating: game.black_rating } : { name: game.white, rating: game.white_rating };
  const pairs = Array.from({ length: Math.ceil(game.moves.length / 2) }, (_, i) => game.moves.slice(i * 2, i * 2 + 2));
  const tc = timeControl(game.time_control);

  return (
    <Shell>
      <section className="game-head">
        <h1 className="game-head__title">{game.white} <span className="game-head__vs">vs</span> {game.black}</h1>
        <p className="game-head__meta">
          <span className="num">{game.result}</span> · {formatDate(game.played_at)}{tc && <> · {tc}</>}
          {game.opening && <> · {game.opening}</>}
          {url && platform in PLATFORM_NAME && (
            <> · <a href={url} className="textlink" target="_blank" rel="noreferrer">Open on {PLATFORM_NAME[platform]} <ExternalLink size={14} aria-hidden="true" /></a></>
          )}
        </p>
      </section>

      <div className="viewer">
        <div className="viewer__board">
          {chance !== null && (
            <div className={`evalbar ${flipped ? "evalbar--flipped" : ""}`} role="img"
              aria-label={`Stockfish: White's win chance ${Math.round(chance * 100)}%`}>
              <div className="evalbar__white" style={{ transform: `scaleY(${chance})` }} />
            </div>
          )}
          <div className="viewer__stack">
            <PlayerPlate {...top} toMove={whiteToMove === flipped} />
            <Board fen={fen} orientation={orientation} lastMove={move?.uci} check={move?.is_check} />
            <PlayerPlate {...bottom} toMove={whiteToMove === !flipped} />
          </div>
        </div>

        <div className="viewer__side">
          <div className="controls" role="toolbar" aria-label="Move through the game">
            <button type="button" onClick={() => go(0)} aria-label="Start" disabled={ply === 0}><ChevronsLeft size={18} /></button>
            <button type="button" onClick={() => go(ply - 1)} aria-label="Previous move" disabled={ply === 0}><ChevronLeft size={18} /></button>
            <button type="button" onClick={() => go(ply + 1)} aria-label="Next move" disabled={ply === game.moves.length}><ChevronRight size={18} /></button>
            <button type="button" onClick={() => go(game.moves.length)} aria-label="End" disabled={ply === game.moves.length}><ChevronsRight size={18} /></button>
            <button type="button" onClick={() => setFlipped(!flipped)} aria-label="Flip board" className="controls__flip"><ArrowUpDown size={18} /></button>
          </div>

          <ol className="moves" aria-label="Moves" ref={movesRef}>
            {pairs.map((pair, i) => (
              <li key={i} className="moves__pair">
                <span className="moves__no">{Math.ceil(pair[0].ply / 2)}</span>
                {pair.map((m) => (
                  <button key={m.ply} type="button" className={`moves__move ${m.ply === ply ? "is-current" : ""}`}
                    aria-current={m.ply === ply ? "step" : undefined} onClick={() => go(m.ply)}>
                    {m.san}
                    {[...new Set(eventsByPly.get(m.ply)?.map((t) => motifInfo(t)?.glyph))].map((g) => <span key={g} className="moves__glyph">{g}</span>)}
                  </button>
                ))}
              </li>
            ))}
          </ol>

          {game.events.length > 0 && (
            <div className="found">
              <h2>Patterns in this game</h2>
              <ul>
                {game.events.map((e) => (
                  <li key={e.id}>
                    <button type="button" onClick={() => go(e.ply)}>
                      <span className="glyph">{motifInfo(e.type)?.glyph ?? "•"}</span>
                      <span>{motifInfo(e.type)?.name ?? e.type}
                        {formNote(e.metadata) && formBadge(formNote(e.metadata)!.form) && <> <span className={`form-tag form-tag--${formNote(e.metadata)!.form}`}
                          title={[FORM_HELP[formNote(e.metadata)!.form], formNote(e.metadata)!.note].filter(Boolean).join(" ")}>{formBadge(formNote(e.metadata)!.form)}</span></>}
                      </span>
                      <span className="found__move">{moveLabel(e.ply, e.san, e.color)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </Shell>
  );
}

/** Each side's plate is one face of the clock: its hand runs while that side is to move. */
function PlayerPlate({ name, rating, toMove }: { name: string; rating: number | null; toMove: boolean }) {
  return (
    <div className={`plate ${toMove ? "plate--to-move" : ""}`}>
      <svg className="plate__dial" viewBox="0 0 32 32" aria-hidden="true">
        <circle cx="16" cy="16" r="15.5" className="plate__bezel" />
        <circle cx="16" cy="16" r="13.5" className="plate__enamel" />
        <g className="plate__hand"><line x1="16" y1="17.5" x2="16" y2="5" /></g>
        <circle cx="16" cy="16" r="1.6" className="plate__pin" />
      </svg>
      <span className="plate__name">{name}{toMove && <span className="visually-hidden"> (to move)</span>}</span>
      {rating && <Digits value={rating} places={1} className="plate__rating" />}
    </div>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="page page--game">
      <TopBar />
      <main>{children}</main>
    </div>
  );
}
