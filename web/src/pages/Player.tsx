import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ChevronDown } from "lucide-react";
import { Board } from "../components/Board";
import { Dial } from "../components/Dial";
import { Digits } from "../components/Digits";
import { RecordBook } from "../components/RecordBook";
import { TopBar } from "../components/TopBar";
import { api, type Motif, ApiError, PLATFORM_NAME, type EventRow, type Platform, type PlayerSummary } from "../lib/api";
import { formatDate, formatMonth, moveLabel, n, plural } from "../lib/format";
import { FORM_NAME, MOTIFS, NAMED_MATES, formNote, type MateForm, type MotifInfo } from "../lib/motifs";

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

  // the dials count the collection only: one mate can carry several pattern names
  const core = summary.motifs.filter((m) => MOTIFS.some((x) => x.type === m.type));
  const mineTotal = core.reduce((sum, m) => sum + m.mine, 0);
  const againstTotal = core.reduce((sum, m) => sum + m.against, 0);
  const namedFound = NAMED_MATES.flatMap((m) => {
    const counts = summary.motifs.find((x) => x.type === m.type);
    return counts ? [{ m, counts }] : [];
  });
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
          <p>Counted across all {plural(summary.games, "game")}. Open one to see the positions.</p>
        </div>
        <div className="ledger__cols" aria-hidden="true">
          <span>By {name}</span><span /><span>Against</span>
        </div>
        <ul className="ledger__rows">
          {MOTIFS.map((m) => ({ m, counts: summary.motifs.find((x) => x.type === m.type) ?? { mine: 0, against: 0 } }))
            // found patterns first; the empty ones wait at the bottom
            .sort((a, b) => Number(b.counts.mine + b.counts.against > 0) - Number(a.counts.mine + a.counts.against > 0))
            .map(({ m, counts }) => {
            return <MotifRow key={m.type} motif={m} mine={counts.mine} against={counts.against} platform={platform} username={username} name={name} />;
          })}
        </ul>
      </section>

      {namedFound.length > 0 && (
        <section className="ledger ledger--named" aria-labelledby="named-title">
          <div className="section-head">
            <h3 id="named-title">Named mates</h3>
            <p>By their geometry, with how closely each matches the classic diagram. {plural(NAMED_MATES.length - namedFound.length, "other")} haven’t turned up yet.</p>
          </div>
          <ul className="ledger__rows">
            {namedFound.map(({ m, counts }) => (
              <MotifRow key={m.type} motif={m} mine={counts.mine} against={counts.against} forms={counts.forms} platform={platform} username={username} name={name} />
            ))}
          </ul>
        </section>
      )}

      {summary.engine?.games_analyzed ? (
        <RecordBook platform={platform} username={username} name={name} games={summary.games}
          engine={`${summary.engine.config.engine_name} (${n(summary.engine.config.limit_value)} ${summary.engine.config.limit_kind} a position)`} />
      ) : (
        <section className="records"><div className="section-head"><h2>The record book</h2>
          <p>Stockfish hasn’t analyzed these games yet. The collection above doesn’t need it.</p></div></section>
      )}
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

function MotifRow({ motif, mine, against, forms, platform, username, name }: {
  motif: MotifInfo; mine: number; against: number; forms?: Motif["forms"]; platform: Platform; username: string; name: string;
}) {
  const breakdown = forms && (["textbook", "canonical", "variant"] as MateForm[])
    .filter((f) => forms[f]).map((f) => `${forms[f]} ${FORM_NAME[f].toLowerCase()}`).join(" · ");
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
            {breakdown && <span className="ledger__forms">{breakdown}</span>}
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
        const form = formNote(r.metadata);
        return (
          <li key={r.id}>
            <Link to={`/g/${r.game_id}?ply=${r.ply}${playerIsWhite ? "" : "&o=black"}`} className="specimen">
              <Board fen={r.fen_after} lastMove={r.uci} orientation={playerIsWhite ? "white" : "black"}
                check={r.san.includes("+") || r.san.includes("#")} label={`${moveLabel(r.ply, r.san, r.color)} against ${opponent}`} />
              <span className="specimen__move">{moveLabel(r.ply, r.san, r.color)}</span>
              <span className="specimen__meta">vs {opponent} · {formatDate(r.played_at)}</span>
              {form && (
                <span className="specimen__form">
                  <span className={`form-tag form-tag--${form.form}`}>{FORM_NAME[form.form]}</span> {form.note}
                </span>
              )}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
