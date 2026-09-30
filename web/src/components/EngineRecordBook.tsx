import { useEffect, useRef, useState } from "react";
import { Card, CARDS } from "./RecordBook";
import type { Discovery, DiscoveryType, Platform } from "../lib/api";
import { n, plural } from "../lib/format";
import { discoveries } from "../engine/archaeology";
import { Analysis, BASELINE_NODES, CONFIG, device, supported, workersFor, type Progress } from "../engine/runner";
import { playerKey, store, type RunState, type Speed } from "../engine/store";

// The record book for players ChessTrove hasn't analysed itself: Stockfish runs in this browser, only
// after the visitor asks, and its results stay in this browser (IndexedDB). The collection above never
// depends on any of this.

const SPEEDS: { id: Speed; label: string }[] = [
  { id: "balanced", label: "Balanced" }, { id: "fast", label: "Fast" }, { id: "max", label: "Max" },
];
const PER_WORKER = 40; // positions a second, a conservative guess for an average device at 25k nodes

function duration(seconds: number) {
  const m = seconds / 60;
  return m < 2 ? "a minute or two" : m < 60 ? `about ${m < 10 ? Math.round(m) : Math.round(m / 5) * 5} minutes`
    : `about ${Math.round(m / 30) / 2} hours`;
}

type Phase = "checking" | "unsupported" | "offer" | "active";

export function EngineRecordBook({ platform, username, positions }: { platform: Platform; username: string; positions: number }) {
  const [phase, setPhase] = useState<Phase>("checking");
  const [run, setRun] = useState<RunState | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [found, setFound] = useState<Partial<Record<DiscoveryType, Discovery[]>> | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const analysis = useRef<Analysis | null>(null);
  const key = playerKey(CONFIG, platform, username);
  const dev = device();

  const save = (st: RunState) => { setRun(st); void store.putState(key, st).catch(() => {}); };

  async function begin(st: RunState, autostart: boolean) {
    const a = new Analysis(platform, username);
    analysis.current = a;
    a.subscribe(() => setProgress(a.progress()));
    setPhase("active");
    try {
      await a.load();
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
      return;
    }
    setFound(discoveries(a.analysed()));
    if (autostart && !a.complete()) void a.start(workersFor(st.speed, dev));
  }

  useEffect(() => {
    if (!supported()) { setPhase("unsupported"); return; }
    let cancelled = false;
    store.state(key).then((st) => {
      if (cancelled) return;
      if (!st?.optedIn) { setPhase("offer"); return; }
      setRun(st);
      // resume on its own only where it's cheap: a capable device that wasn't paused
      void begin(st, !st.paused && !dev.constrained);
    }).catch(() => setPhase("offer"));
    return () => { cancelled = true; analysis.current?.stop(); analysis.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  // discoveries follow the results, a few seconds apart while the engine runs
  const running = progress?.running ?? false;
  useEffect(() => {
    if (!running) { if (analysis.current) setFound(discoveries(analysis.current.analysed())); return; }
    const t = setInterval(() => { if (analysis.current) setFound(discoveries(analysis.current.analysed())); }, 4000);
    return () => clearInterval(t);
  }, [running]);

  const start = () => {
    const st: RunState = { optedIn: true, paused: false, speed: run?.speed ?? "balanced", startedAt: run?.startedAt ?? new Date().toISOString() };
    save(st);
    if (analysis.current) void analysis.current.start(workersFor(st.speed, dev));
    else void begin(st, true);
  };
  const pause = () => { analysis.current?.stop(); if (run) save({ ...run, paused: true }); };
  const setSpeed = (speed: Speed) => {
    if (!run) return;
    save({ ...run, speed });
    const a = analysis.current;
    if (a && progress?.running) { a.stop(); void a.start(workersFor(speed, dev)); }
  };

  const head = (text: React.ReactNode) => (
    <div className="section-head">
      <h2 id="records-title">The record book</h2>
      {text}
    </div>
  );

  if (phase === "checking") return null;
  if (phase === "unsupported") {
    return (
      <section className="records" aria-labelledby="records-title">
        {head(<p>The record book needs Stockfish running in your browser, and this browser can’t run it (it needs WebAssembly and
          Web Workers). Everything above works without it.</p>)}
      </section>
    );
  }
  if (phase === "offer") {
    const workers = workersFor("balanced", dev);
    return (
      <section className="records engine-offer" aria-labelledby="records-title">
        {head(<p>Find your biggest comeback, your worst throw, the only winning moves you found, your sound sacrifices, your
          longest forced mates and how your underpromotions stack up. Stockfish reads every position of your games to find them.</p>)}
        <div className="engine-offer__go">
          <button type="button" className="plunger" onClick={start}>Analyze my games</button>
          <p>
            Runs on this device, not on ChessTrove’s server, and nothing is uploaded. It keeps {plural(workers, "processor core")} busy
            for {duration(positions / (workers * PER_WORKER))} ({n(positions)} positions). Progress is saved: pause, close the tab
            and pick it up later.
          </p>
        </div>
      </section>
    );
  }

  const p = progress;
  const complete = !!p && p.gamesDone === p.games && p.probesPending === 0;
  const eta = p?.rate && !complete ? duration((p.positions - p.positionsDone) / p.rate) : null;
  return (
    <section className="records" aria-labelledby="records-title">
      {head(
        <div className="engine-status" aria-live="polite">
          {loadError ? <p>Couldn’t load the games to analyse: {loadError}.</p>
            : !p ? <p className="loading">Loading your analysis…</p> : (
            <>
              <p className="engine-status__line">
                {complete ? <>Stockfish read all <span className="num">{n(p.positions)}</span> positions.</> : (
                  <>
                    {p.running ? "Analyzing with Stockfish" : "Paused"} · <span className="num">{n(p.positionsDone)}</span> / <span className="num">{n(p.positions)}</span> positions
                    {p.running && p.rate ? <> · <span className="num">{n(Math.round(p.rate))}</span> a second</> : null}
                    {p.running && eta ? <> · {eta} left</> : null}
                  </>
                )}
              </p>
              {p.error && <p className="engine-status__error">The engine stopped: {p.error}. The collection above isn’t affected.</p>}
              {!complete && (
                <div className="engine-status__controls">
                  {p.running
                    ? <button type="button" className="engine-btn" onClick={pause}>Pause</button>
                    : <button type="button" className="engine-btn engine-btn--go" onClick={start}>{p.positionsDone ? "Resume" : "Start"}</button>}
                  <div className="seg" role="radiogroup" aria-label="Speed">
                    {SPEEDS.map((s) => (
                      <button key={s.id} type="button" role="radio" aria-checked={run?.speed === s.id} aria-selected={run?.speed === s.id}
                        onClick={() => setSpeed(s.id)}
                        title={`${plural(workersFor(s.id, dev), "core")} on this device`}>{s.label}</button>
                    ))}
                  </div>
                </div>
              )}
              <p className="engine-status__note">
                Stockfish 18 runs in your browser ({n(BASELINE_NODES)} nodes a position) and the results stay on this device.
                {!complete && " Until every game is read, these are the best found so far."}
              </p>
            </>
          )}
        </div>,
      )}
      <div className="records__grid">
        {CARDS.map((c) => (
          <Card key={c.type} type={c.type} empty={complete ? c.empty : "Nothing yet in the games read so far."}
            title={complete ? c.title : `${c.title}, so far`} rows={found ? found[c.type] ?? [] : null} />
        ))}
      </div>
    </section>
  );
}
