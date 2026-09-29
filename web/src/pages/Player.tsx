import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowRight, ChevronDown } from "lucide-react";
import { Board } from "../components/Board";
import { Dial } from "../components/Dial";
import { Digits } from "../components/Digits";
import { TopBar } from "../components/TopBar";
import { api, ApiError, PLATFORM_NAME, type EventRow, type LabelRow, type Platform, type PlayerSummary } from "../lib/api";
import { formatDate, formatMonth, moveLabel, n, pct, plural } from "../lib/format";
import { ENGINE_LABELS, MOTIFS, type LabelInfo, type MotifInfo } from "../lib/motifs";

const POLL_MS = 2000;

export function Player() {
  const { platform: rawPlatform = "", username = "" } = useParams();
  const platform = rawPlatform as Platform;
  const [summary, setSummary] = useState<PlayerSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const importRequested = useRef(false);

  const load = useCallback(async () => {
    try {
      const s = await api.player(platform, username);
      setSummary(s);
      const neverImported = s.games === 0 && !s.latest_import;
      if (neverImported && !importRequested.current) {
        importRequested.current = true;
        await api.startImport(platform, username);
        setSummary(await api.player(platform, username));
      }
    } catch (e) {
      setError(e instanceof ApiError && e.status === 422 ? "That isn't a valid username." : "ChessTrove's server didn't answer. Try again in a moment.");
    }
  }, [platform, username]);

  useEffect(() => {
    importRequested.current = false;
    setSummary(null);
    setError(null);
    load();
  }, [load]);

  const running = summary?.latest_import?.status === "running";
  useEffect(() => {
    if (!running) return;
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [running, load]);

  if (!(platform in PLATFORM_NAME)) {
    return <Shell><div className="empty"><h1>Unknown site.</h1><p>ChessTrove reads Chess.com and Lichess.</p></div></Shell>;
  }
  if (error) return <Shell><div className="empty"><h1>Couldn’t load this player.</h1><p>{error}</p></div></Shell>;
  if (!summary) return <Shell><div className="loading" aria-live="polite">Opening {username}…</div></Shell>;

  const failed = summary.games === 0 && summary.latest_import?.status === "failed";
  if (failed) {
    const notFound = summary.latest_import!.errors.some((e) => /404/.test(e.error));
    return (
      <Shell>
        <div className="empty">
          <h1>{notFound ? `No ${PLATFORM_NAME[platform]} player called “${username}”.` : "The import stopped."}</h1>
          <p>{notFound ? "Check the spelling, or try the other site." : `${PLATFORM_NAME[platform]} didn’t return this player’s games. Try again later.`}</p>
        </div>
      </Shell>
    );
  }

  const mineTotal = summary.motifs.reduce((sum, m) => sum + m.mine, 0);
  const againstTotal = summary.motifs.reduce((sum, m) => sum + m.against, 0);
  const name = summary.display_name ?? username;

  return (
    <Shell>
      <section className="player-head" aria-labelledby="player-name">
        <h1 id="player-name" className="player-head__name">{name}</h1>
        <p className="player-head__meta">
          {PLATFORM_NAME[platform]}
          {summary.rating ? <> · <span className="num">{summary.rating}</span></> : null}
          {summary.games > 0 && <> · {plural(summary.games, "game")} since {formatMonth(summary.first_game)}</>}
        </p>
      </section>

      <div className="case case--player">
        <div className="case__plungers" aria-hidden="true">
          <span className={`plunger ${running ? "plunger--down" : "plunger--up"} plunger--decor`} />
          <span className={`plunger ${running ? "plunger--up" : "plunger--down"} plunger--decor`} />
        </div>
        <div className="case__body">
          <Dial running={running}>
            <div className="face">
              {running ? (
                <>
                  <Digits value={summary.games} places={4} className="face__count" />
                  <span className="face__label">games in so far</span>
                </>
              ) : (
                <>
                  <Digits value={mineTotal} className="face__count" />
                  <span className="face__label">finds by {name}</span>
                </>
              )}
            </div>
          </Dial>
          <Dial>
            <div className="face">
              <Digits value={againstTotal} className="face__count" />
              <span className="face__label">finds against {name}</span>
            </div>
          </Dial>
        </div>
        <p className="case__plate" aria-live="polite">
          {running
            ? `Importing from ${PLATFORM_NAME[platform]}. Motifs appear as games arrive; you can leave and come back.`
            : <><span className="num">{n(summary.wins)}</span> wins · <span className="num">{n(summary.draws)}</span> draws · <span className="num">{n(summary.losses)}</span> losses</>}
        </p>
      </div>

      <section className="ledger" aria-labelledby="ledger-title">
        <div className="section-head">
          <h2 id="ledger-title">The collection</h2>
          <p>Every pattern below is counted across all {plural(summary.games, "game")}. Open one to see the positions.</p>
        </div>
        <div className="ledger__cols" aria-hidden="true">
          <span>By {name}</span><span /><span>Against</span>
        </div>
        <ul className="ledger__rows">
          {MOTIFS.map((m) => {
            const counts = summary.motifs.find((x) => x.type === m.type) ?? { mine: 0, against: 0 };
            return <MotifRow key={m.type} motif={m} mine={counts.mine} against={counts.against} platform={platform} username={username} name={name} />;
          })}
        </ul>
      </section>

      <EngineSection summary={summary} platform={platform} username={username} name={name} />
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="page page--player">
      <TopBar />
      <main>{children}</main>
    </div>
  );
}

function MotifRow({ motif, mine, against, platform, username, name }: {
  motif: MotifInfo; mine: number; against: number; platform: Platform; username: string; name: string;
}) {
  const [open, setOpen] = useState(false);
  const [side, setSide] = useState<"mine" | "against">(mine > 0 || against === 0 ? "mine" : "against");
  const empty = mine === 0 && against === 0;
  const panelId = `motif-${motif.type}`;
  return (
    <li className={`ledger__row ${empty ? "ledger__row--empty" : ""} ${open ? "ledger__row--open" : ""}`}>
      <button type="button" className="ledger__summary" disabled={empty} aria-expanded={open} aria-controls={panelId}
        onClick={() => setOpen(!open)}>
        <Digits value={mine} className="ledger__count" />
        <span className="ledger__motif">
          <span className="glyph">{motif.glyph}</span>
          <span className="ledger__text">
            <span className="ledger__name">{motif.name}</span>
            <span className="ledger__def">{empty ? "Not in these games yet." : motif.definition}</span>
          </span>
        </span>
        <Digits value={against} className="ledger__count ledger__count--against" />
        {!empty && <ChevronDown className="ledger__chev" size={18} aria-hidden="true" />}
      </button>
      {open && (
        <div id={panelId} className="ledger__panel">
          <div className="seg" role="tablist" aria-label="Whose moves">
            <button role="tab" aria-selected={side === "mine"} disabled={mine === 0} onClick={() => setSide("mine")}>By {name} ({n(mine)})</button>
            <button role="tab" aria-selected={side === "against"} disabled={against === 0} onClick={() => setSide("against")}>Against ({n(against)})</button>
          </div>
          <Specimens key={side} platform={platform} username={username} type={motif.type} side={side} />
        </div>
      )}
    </li>
  );
}

function Specimens({ platform, username, type, side }: { platform: Platform; username: string; type: string; side: "mine" | "against" }) {
  const [rows, setRows] = useState<EventRow[] | null>(null);
  useEffect(() => {
    (side === "mine" ? api.events(platform, username, type) : api.eventsAgainst(platform, username, type)).then(setRows).catch(() => setRows([]));
  }, [platform, username, type, side]);
  if (!rows) return <p className="loading">Loading positions…</p>;
  if (!rows.length) return <p className="muted">None on this side.</p>;
  return (
    <ul className="specimens">
      {rows.map((r) => {
        const playerIsWhite = r.white.toLowerCase() === username.toLowerCase();
        const opponent = playerIsWhite ? r.black : r.white;
        return (
          <li key={r.id}>
            <Link to={`/g/${r.game_id}?ply=${r.ply}${playerIsWhite ? "" : "&o=black"}`} className="specimen">
              <Board fen={r.fen_after} lastMove={r.uci} orientation={playerIsWhite ? "white" : "black"}
                check={r.san.includes("+") || r.san.includes("#")} label={`${moveLabel(r.ply, r.san, r.color)} against ${opponent}`} />
              <span className="specimen__move">{moveLabel(r.ply, r.san, r.color)}</span>
              <span className="specimen__meta">vs {opponent} · {formatDate(r.played_at)}</span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function EngineSection({ summary, platform, username, name }: { summary: PlayerSummary; platform: Platform; username: string; name: string }) {
  const e = summary.engine;
  const analyzed = e?.games_analyzed ?? 0;
  return (
    <section className="engine" aria-labelledby="engine-title">
      <div className="section-head">
        <h2 id="engine-title">Stockfish</h2>
        {analyzed > 0 ? (
          <p>
            {e!.config.engine_name} has analyzed {n(analyzed)} of {plural(summary.games, "game")} at {n(e!.config.limit_value)} nodes
            per position. Win chances use Lichess’s human-calibrated curve.
          </p>
        ) : (
          <p>Stockfish hasn’t analyzed these games yet. The collection above doesn’t need it.</p>
        )}
      </div>
      {analyzed > 0 && ENGINE_LABELS.map((l) => (
        <EngineList key={l.type} label={l} platform={platform} username={username} name={name} />
      ))}
    </section>
  );
}

function EngineList({ label, platform, username, name }: { label: LabelInfo; platform: Platform; username: string; name: string }) {
  const [rows, setRows] = useState<LabelRow[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => { api.labels(platform, username, label.type).then(setRows).catch(() => setRows([])); }, [platform, username, label.type]);
  return (
    <div className={`elist elist--${label.tone}`}>
      <div className="elist__head">
        <span className={`glyph glyph--${label.tone}`}>{label.glyph}</span>
        <div>
          <h3>{label.name}</h3>
          <p>{label.definition}</p>
        </div>
      </div>
      {!rows ? <p className="loading">Loading…</p> : rows.length === 0 ? (
        <p className="muted">None for {name}.</p>
      ) : (
        <ol className="elist__rows">
          {rows.map((r) => {
            const key = `${r.game_id}-${r.ply}`;
            const isWhite = r.color === "w";
            const opponent = isWhite ? r.black : r.white;
            const good = label.tone === "good";
            const beforeFen = r.fen_before ?? r.initial_fen ?? "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
            return (
              <li key={key} className={open === key ? "is-open" : undefined}>
                <button type="button" className="elist__row" aria-expanded={open === key} onClick={() => setOpen(open === key ? null : key)}>
                  <span className="elist__move">{moveLabel(r.ply, r.san, r.color)}</span>
                  <span className="elist__swing">
                    {good ? <>only win · next best <span className="num">{pct(Number(r.runner_up_line))}</span></>
                      : <><span className="num">{pct(Number(r.expected_before))}</span> → <span className="num">{pct(Number(r.expected_after))}</span></>}
                  </span>
                  <span className="elist__meta">vs {opponent} · {formatDate(r.played_at)}</span>
                  <ChevronDown className="elist__chev" size={16} aria-hidden="true" />
                </button>
                {open === key && (
                  <div className="elist__detail">
                    <Board
                      fen={good ? r.fen_after : beforeFen}
                      lastMove={good ? r.uci : null}
                      orientation={isWhite ? "white" : "black"}
                      arrows={good ? [] : [
                        { uci: r.uci, brush: "flag" },
                        ...(r.engine_choice && r.engine_choice !== r.uci ? [{ uci: r.engine_choice, brush: "brass" as const }] : []),
                      ]}
                      label={good ? `${r.san}, the only winning move` : `Played ${r.san}; Stockfish preferred ${r.engine_choice}`}
                    />
                    <div className="elist__explain">
                      {good ? (
                        <p>{name} played <strong>{r.san}</strong>. Win chance with it: <span className="num">{pct(Number(r.best_line))}</span>; with the next-best move, <span className="num">{pct(Number(r.runner_up_line))}</span>.</p>
                      ) : (
                        <p><span className="key key--flag" /> Played <strong>{r.san}</strong>: win chance <span className="num">{pct(Number(r.expected_before))}</span> → <span className="num">{pct(Number(r.expected_after))}</span>.
                          {r.engine_choice && r.engine_choice !== r.uci && <> <span className="key key--brass" /> Stockfish’s choice is the brass arrow.</>}</p>
                      )}
                      <Link to={`/g/${r.game_id}?ply=${r.ply}${isWhite ? "" : "&o=black"}`} className="textlink">Open the game at this move <ArrowRight size={14} aria-hidden="true" /></Link>
                    </div>
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
