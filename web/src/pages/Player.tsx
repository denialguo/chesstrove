import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowRight, ChevronDown } from "lucide-react";
import { Board } from "../components/Board";
import { Dial } from "../components/Dial";
import { Digits } from "../components/Digits";
import { RecordBook } from "../components/RecordBook";
import { EngineRecordBook } from "../components/EngineRecordBook";
import { TopBar } from "../components/TopBar";
import { api, type Motif, ApiError, PLATFORM_NAME, type EventRow, type Platform, type PlayerSummary } from "../lib/api";
import { formatDate, formatMonth, moveLabel, n, plural, roughDuration } from "../lib/format";
import { BEST_UNDERPROMOTION, COUNTED, FORM_NAME, MOTIFS, NAMED_MATES, formNote, type MateForm, type MotifInfo } from "../lib/motifs";
import { supported } from "../engine/runner";
import { checkUnderpromotions, savedVerdicts, type UpVerdict } from "../engine/underpromotions";

const POLL_MS = 2000;

export function Player() {
  const { platform: rawPlatform = "", username = "" } = useParams();
  const platform = rawPlatform as Platform;
  const [summary, setSummary] = useState<PlayerSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const importRequested = useRef(false);
  const samples = useRef<[number, number][]>([]); // (time, games stored) while an import runs: the rate behind the estimate

  const load = useCallback(async () => {
    try {
      const s = await api.player(platform, username);
      if (s.latest_import?.status === "running") {
        samples.current = [...samples.current, [Date.now(), s.games] as [number, number]].filter(([t]) => t > Date.now() - 60_000);
      } else samples.current = [];
      setSummary(s);
      // fetch new games when there's no import yet, the last one failed, or it finished over a day ago; the
      // server returns the running (or just-finished) import instead of starting a duplicate
      const last = s.latest_import;
      const due = !last || last.status === "failed"
        || (last.status === "completed" && Date.now() - Date.parse(last.finished_at ?? "") > 86_400_000);
      if (due && !importRequested.current) {
        importRequested.current = true;
        try {
          await api.startImport(platform, username);
        } catch (e) {
          if (s.games === 0) throw e; // with games stored, a refused catch-up still leaves a page worth showing
          return;
        }
        setSummary(await api.player(platform, username));
      }
    } catch (e) {
      setError(e instanceof ApiError && e.status === 422 ? "That isn't a valid username."
        : e instanceof ApiError && e.status === 429 ? "Too many imports from your connection in the last hour. Try again later."
        : "ChessTrove's server didn't answer. Try again in a moment.");
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

  // rare moments: distinct moves per side matching a rare pattern (db.RARE_MOMENT_TYPES), however many labels
  // they carry. Not missed mates in one (mistakes), not engine finds (not every player has them).
  const rare = summary.rare_moments;
  const namedFound = NAMED_MATES.flatMap((m) => {
    const counts = summary.motifs.find((x) => x.type === m.type);
    return counts ? [{ m, counts }] : [];
  });
  const name = summary.display_name ?? username;
  // what the platform says they've played (Chess.com counts rated games only, hence "about")
  const expected = summary.latest_import?.games_expected ?? null;
  // time left: from the last minute's rate, once there's 15 s of it and games are actually arriving
  const [first, last] = [samples.current[0], samples.current[samples.current.length - 1]];
  const rate = first && last && last[0] - first[0] >= 15_000 ? ((last[1] - first[1]) * 1000) / (last[0] - first[0]) : 0;
  const importLeft = running && expected && rate > 0 && expected > summary.games ? roughDuration((expected - summary.games) / rate) : null;

  return (
    <Shell>
      <section className="player-head" aria-labelledby="player-name">
        <h1 id="player-name" className="player-head__name">{name}</h1>
        <p className="player-head__meta">
          {PLATFORM_NAME[platform]}
          {summary.rating ? <> · <span className="num">{summary.rating}</span>{summary.rating_mode ? ` ${summary.rating_mode}` : ""}</> : null}
          {summary.games > 0 && <> · {plural(summary.games, "game")} since {formatMonth(summary.first_game)}</>}
        </p>
      </section>

      <div className="case case--player">
        <div className="case__plungers" aria-hidden="true">
          <span className={`plunger ${running ? "plunger--down" : "plunger--up"} plunger--decor`} />
          <span className={`plunger ${running ? "plunger--up" : "plunger--down"} plunger--decor`} />
        </div>
        <div className="case__body">
          {running ? (
            <Tally value={expected ?? summary.games} running
              unit={expected ? `games on ${PLATFORM_NAME[platform]}` : "games in so far"}
              who="Importing" examples={null} />
          ) : (
            <Tally value={rare.mine} unit={rare.mine === 1 ? "Rare moment" : "Rare moments"}
              who={`by ${name}`} examples={examples(rare.types, "mine")} />
          )}
          <Tally value={rare.against} unit={rare.against === 1 ? "Rare moment" : "Rare moments"}
            who="by their opponents" examples={examples(rare.types, "against")} />
        </div>
        <div className="case__plate" aria-live="polite">
          <p className="case__explain">
            Unusual moves found without an engine. Each move counts once.
          </p>
          <p>
            {running
              ? `Importing from ${PLATFORM_NAME[platform]}${expected ? `: ${n(summary.games)} of ${platform === "chesscom" ? "about " : ""}${n(expected)} read so far` : ""}${importLeft ? `, ${importLeft} left` : ""}. Motifs appear as games arrive; you can leave and come back.`
              : <><span className="num">{n(summary.wins)}</span> wins · <span className="num">{n(summary.draws)}</span> draws · <span className="num">{n(summary.losses)}</span> losses</>}
          </p>
        </div>
      </div>

      <section className="ledger" aria-labelledby="ledger-title">
        <div className="section-head ledger__heading">
          <h2 id="ledger-title">Found in these games</h2>
          <p>Open a pattern to see the moves.</p>
        </div>
        <div className="ledger__cols" aria-hidden="true">
          <span>By {name}</span><span /><span>Against</span>
        </div>
        <ul className="ledger__rows">
          {MOTIFS.map((m) => ({ m, counts: summary.motifs.find((x) => x.type === m.type) ?? { mine: 0, against: 0 } }))
            // found patterns first; the empty ones wait at the bottom
            .sort((a, b) => Number(b.counts.mine + b.counts.against > 0) - Number(a.counts.mine + a.counts.against > 0))
            .flatMap(({ m, counts }) => [
              <MotifRow key={m.type} motif={m} mine={counts.mine} against={counts.against} platform={platform} username={username} name={name} />,
              ...(m.type === "UNDERPROMOTION" && counts.mine + counts.against > 0
                ? [<BestUnderpromotionRow key="BEST_UNDERPROMOTION" platform={platform} username={username} name={name}
                     mineTotal={counts.mine} againstTotal={counts.against}
                     server={new URLSearchParams(location.search).get("engine") === "browser" ? null : summary.best_underpromotions ?? null} />]
                : []),
            ])}
        </ul>
      </section>

      {namedFound.length > 0 && (
        <section className="ledger ledger--named" aria-labelledby="named-title">
          <div className="section-head">
            <h2 id="named-title">Mating patterns</h2>
            <p>By {name} on the left, opponents on the right. Open a mate for its positions and forms.</p>
          </div>
          <ul className="ledger__rows">
            {namedFound.map(({ m, counts }) => (
              <MotifRow key={m.type} motif={m} mine={counts.mine} against={counts.against} forms={counts.forms} platform={platform} username={username} name={name} />
            ))}
          </ul>
        </section>
      )}

      {summary.engine?.games_analyzed && new URLSearchParams(location.search).get("engine") !== "browser" ? (
        <RecordBook platform={platform} username={username} games={summary.games}
          engine={`${summary.engine.config.engine_name} (${n(summary.engine.config.limit_value)} ${summary.engine.config.limit_kind} a position)`} />
      ) : running ? (
        <section className="records"><div className="section-head"><h2>Engine records</h2>
          <p>Optional Stockfish analysis is available once these games finish importing.</p></div></section>
      ) : summary.games > 0 ? (
        <EngineRecordBook platform={platform} username={username} positions={summary.positions} />
      ) : null}
    </Shell>
  );
}

/** One side of the clock: a small dial with the count, and what it's made of beside it. */
function Tally({ value, unit, who, examples, running }: {
  value: number; unit: string; who: string; examples: React.ReactNode; running?: boolean;
}) {
  return (
    <div className="tally">
      <Dial running={running} className="dial--tally">
        <div className="face"><Digits value={value} className="face__count" /></div>
      </Dial>
      <div className="tally__text">
        <p className="tally__unit">{unit}</p>
        <p className="tally__who">{who}</p>
        {examples && <p className="tally__eg">{examples}</p>}
      </div>
    </div>
  );
}

/** "2 smothered mates · 10 underpromotions · 54 ladder mates": two of the rarer kinds, then the most common one,
 *  so the number is never mostly something the examples leave out. */
function examples(types: PlayerSummary["rare_moments"]["types"], side: "mine" | "against") {
  const have = COUNTED.map(([type, one, many]) => ({ count: types.find((t) => t.type === type)?.[side] ?? 0, one, many }))
    .filter((x) => x.count > 0);
  if (!have.length) return null;
  const biggest = have.reduce((a, b) => (b.count > a.count ? b : a));
  return [...have.filter((x) => x !== biggest).slice(0, 2), biggest].map((x, i, all) => <span key={x.one}><span className="nowrap">{plural(x.count, x.one, x.many)}{i < all.length - 1 && " ·"}</span> </span>);
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
            <span className="ledger__def">{empty ? "Not in these games yet." : motif.definition}</span>
          </span>
        </span>
        <Digits value={against} className="ledger__count ledger__count--against" />
        {!empty && <ChevronDown className="ledger__chev" size={18} aria-hidden="true" />}
      </button>
      {open && (
        <div id={panelId} className="ledger__panel">
          {breakdown && <p className="ledger__forms">By {name}: {breakdown}.</p>}
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

/** Underpromotions that were the single best move. Needs Stockfish: the server's verdicts where ChessTrove
 * analysed the player, otherwise a one-click check in this browser (engine/underpromotions.ts). */
function BestUnderpromotionRow({ platform, username, name, mineTotal, againstTotal, server }: {
  platform: Platform; username: string; name: string; mineTotal: number; againstTotal: number;
  server: PlayerSummary["best_underpromotions"] | null;
}) {
  const total = mineTotal + againstTotal;
  const [saved, setSaved] = useState<UpVerdict[] | null>(null);
  const [checking, setChecking] = useState<{ done: number; total: number } | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [side, setSide] = useState<"mine" | "against">("mine");
  const canRun = supported();
  useEffect(() => {
    if (server || !canRun) return;
    savedVerdicts(platform, username).then(setSaved).catch(() => setSaved([]));
  }, [platform, username, server, canRun]);

  const verdicts = server ? null : saved ?? [];
  const found = useMemo(() => new Set(server ? server.found : (verdicts ?? []).filter((v) => v.best === "unique_best").map((v) => v.key)),
    [server, verdicts]);
  const mine = server ? server.mine : (verdicts ?? []).filter((v) => v.best === "unique_best" && v.mine).length;
  const against = server ? server.against : (verdicts ?? []).filter((v) => v.best === "unique_best" && !v.mine).length;
  const judged = server ? server.judged : (verdicts ?? []).length;
  const locked = !server && judged === 0;
  const remaining = server ? 0 : total - judged;

  const leaving = useRef(new AbortController());
  useEffect(() => () => leaving.current.abort(), []);
  const check = async (workers?: number) => {
    setFailed(null);
    setChecking({ done: 0, total: remaining });
    try {
      await checkUnderpromotions(platform, username, (v, done, all) => {
        setSaved((s) => [...(s ?? []), v]);
        setChecking({ done, total: all });
      }, { workers, signal: leaving.current.signal });
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    }
    if (!leaving.current.signal.aborted) setChecking(null);
  };

  const empty = !locked && mine + against === 0;
  const panelId = "motif-BEST_UNDERPROMOTION";
  // the same numbers as the Underpromotion row above: theirs on the left, against them on the right
  const both = `${n(mineTotal)} by ${name}${againstTotal ? ` and ${n(againstTotal)} against` : ""}`;
  const note = !canRun && !server ? "Needs a browser that can run Stockfish."
    : checking ? `Stockfish is checking the underpromotions on this device: ${checking.done} of ${checking.total}.`
    : locked ? `Stockfish can check which of the underpromotions above (${both}) was the single best move, on this device, in ${roughDuration((total * 3) / 2)}.`
    : empty ? `None of the ${plural(judged, "underpromotion")} Stockfish checked was the single best move.`
    : BEST_UNDERPROMOTION.definition;
  return (
    <li className={`ledger__row ledger__row--derived ${empty ? "ledger__row--empty" : ""} ${locked ? "ledger__row--locked" : ""} ${open ? "ledger__row--open" : ""}`}>
      <div className="ledger__summary-wrap">
        <button type="button" className="ledger__summary" disabled={empty || locked} aria-expanded={open} aria-controls={panelId}
          onClick={() => setOpen(!open)}>
          {locked ? <span className="ledger__count ledger__lock" aria-label="not checked yet">?</span> : <Digits value={mine} className="ledger__count" />}
          <span className="ledger__motif">
            <span className="glyph">{BEST_UNDERPROMOTION.glyph}</span>
            <span className="ledger__text">
              <span className="ledger__name">{BEST_UNDERPROMOTION.name}</span>
              <span className="ledger__def">{failed ? `The check stopped: ${failed}.` : note}</span>
            </span>
          </span>
          {locked ? <span className="ledger__count ledger__count--against ledger__lock" aria-hidden="true">?</span>
            : <Digits value={against} className="ledger__count ledger__count--against" />}
          {!empty && !locked && <ChevronDown className="ledger__chev" size={18} aria-hidden="true" />}
        </button>
        {canRun && !server && (locked || remaining > 0) && (
          <button type="button" className="textlink ledger__unlock" disabled={!!checking} onClick={() => void check()}>
            {checking ? "Checking…" : locked ? "Check with Stockfish" : `Check the other ${remaining}`}
            {!checking && <ArrowRight size={14} aria-hidden="true" />}
          </button>
        )}
      </div>
      {open && (
        <div id={panelId} className="ledger__panel">
          <div className="seg" role="tablist" aria-label="Whose moves">
            <button role="tab" aria-selected={side === "mine"} disabled={mine === 0} onClick={() => setSide("mine")}>By {name} ({n(mine)})</button>
            <button role="tab" aria-selected={side === "against"} disabled={against === 0} onClick={() => setSide("against")}>Against ({n(against)})</button>
          </div>
          <Specimens key={side} platform={platform} username={username} type="UNDERPROMOTION" side={side} only={found} />
        </div>
      )}
    </li>
  );
}

function Specimens({ platform, username, type, side, only }: {
  platform: Platform; username: string; type: string; side: "mine" | "against"; only?: Set<string>; // only these "game:ply"
}) {
  const [rows, setRows] = useState<EventRow[] | null>(null);
  useEffect(() => {
    const limit = only ? 1000 : 24;
    (side === "mine" ? api.events(platform, username, type, limit) : api.eventsAgainst(platform, username, type, limit))
      .then((r) => setRows(only ? r.filter((e) => only.has(`${e.game_id}:${e.ply}`)) : r)).catch(() => setRows([]));
  }, [platform, username, type, side, only]);
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
                  <span className="form-tag">{FORM_NAME[form.form]} form.</span> {form.note}
                </span>
              )}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
