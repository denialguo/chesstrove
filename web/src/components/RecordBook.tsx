import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";
import { Board } from "./Board";
import { api, type Discovery, type DiscoveryType, type Platform } from "../lib/api";
import { evalShort, evalWords, formatDate, moveLabel, plural } from "../lib/format";

// The engine records: one entry per discovery type, the top find with its board and evidence,
// the next few as rows. Every sentence is built from the result's own evidence (archaeology.py).

interface Shown {
  value: string; // the entry's headline value
  sentence: string;
  fen: string;
  lastMove?: string | null;
  arrows?: { uci: string; brush: "brass" | "flag" }[];
  bad?: boolean; // a moment that went wrong: the value goes flag red
}

export const CARDS: { type: DiscoveryType; title: string; empty: string }[] = [
  { type: "biggest_comeback", title: "Biggest comeback", empty: "No won game came back from a losing position yet." },
  { type: "biggest_throw", title: "Biggest throw", empty: "Nothing thrown away yet." },
  { type: "only_winning_move", title: "Only winning moves found", empty: "No only-moves found yet." },
  { type: "material_sacrifice", title: "Sound sacrifices", empty: "No queen, rook or exchange sacrifice that Stockfish backed, yet." },
  { type: "longest_mate_found", title: "Longest mate carried out", empty: "No forced mate of two moves or more carried out yet." },
  { type: "underpromotion", title: "Underpromotions, judged", empty: "No underpromotions in these games yet." },
];

const side = (c: "w" | "b") => (c === "w" ? "white" : "black") as "white" | "black";
const label = (d: Discovery) => (d.move ? moveLabel(d.ply, d.move.san, d.move.by ?? d.color) : "");

function howWon(termination: string | null | undefined) {
  const t = (termination ?? "").toLowerCase();
  return t.includes("checkmate") ? "by checkmate" : t.includes("resign") ? "by resignation"
    : t.includes("time") ? "on time" : t.includes("abandon") ? "when they abandoned" : "";
}

const BEST: Record<string, (d: Discovery) => string> = {
  unique_best: (d) => `the single best of ${d.legal_moves} legal moves`,
  tied_best: (d) => `tied for the best move (with ${d.best_moves!.filter((m) => m.uci !== d.move!.uci).map((m) => m.san).join(", ")})`,
  not_best: (d) => `number ${d.played_move_rank} of ${d.legal_moves} legal moves`,
  unknown: () => "not yet checked against every legal move",
};
const QUEEN: Record<string, string> = {
  better: "better than queening", equal: "as good as queening", worse: "worse than queening", unknown: "",
};

function present(type: DiscoveryType, d: Discovery): Shown {
  switch (type) {
    case "biggest_comeback":
      return {
        value: evalShort(d.eval), fen: d.fen!, lastMove: d.move?.uci,
        sentence: `After ${label(d)} you were ${evalWords(d.eval)} against ${d.opponent}, and won${howWon(d.game.termination) ? ` ${howWon(d.game.termination)}` : ""}.`,
      };
    case "biggest_throw":
      return {
        value: `${evalShort(d.eval_before)} → ${evalShort(d.eval_after)}`, bad: true, fen: d.fen_before!,
        arrows: [{ uci: d.move!.uci, brush: "flag" }, ...(d.engine_choice?.uci ? [{ uci: d.engine_choice.uci, brush: "brass" as const }] : [])],
        sentence: `${label(d)} turned ${evalWords(d.eval_before)} into ${evalWords(d.eval_after)}. Stockfish wanted ${d.engine_choice?.san}.`,
      };
    case "only_winning_move": {
      const r = d.comparison!.runner_up;
      return {
        value: label(d), fen: d.fen_after!, lastMove: d.move!.uci,
        sentence: `The one move that kept the win (${evalShort(d.comparison!.best_line.eval)}). The next best, ${r.san}, was ${evalWords(r.eval)}.`,
      };
    }
    case "material_sacrifice": {
      const s = d.sacrifice!;
      return {
        value: label(d), fen: d.fen_after!, lastMove: d.move!.uci,
        sentence: `A ${s.kind} sacrifice: ${s.reply} took it and you stayed ${s.deficit} points down${s.ends_in_mate ? " until you mated" : ""}. It was Stockfish's first choice (${evalShort(d.eval_before)}).`,
      };
    }
    case "longest_mate_found":
      return {
        value: `${d.score.value} moves`, fen: d.fen_before!, lastMove: null,
        arrows: [{ uci: d.move!.uci, brush: "brass" }],
        sentence: `From ${label(d)} you kept a forced mate on the board for ${plural(d.run!.moves, "move")} and delivered it. Stockfish: mate in ${d.run!.engine_mate_in_at_start} at the start.`,
      };
    case "underpromotion":
      return {
        value: d.move!.san, fen: d.fen_after!, lastMove: d.move!.uci,
        sentence: `${label(d)} was ${BEST[d.best_move ?? "unknown"](d)}${QUEEN[d.vs_queen ?? "unknown"] ? `, and ${QUEEN[d.vs_queen!]}` : ""}.`,
      };
    default:
      return { value: label(d), fen: d.fen_after ?? d.fen!, sentence: "" };
  }
}

/** What a runner-up row shows next to its move: the evidence, not the move again. */
function rowValue(type: DiscoveryType, d: Discovery) {
  switch (type) {
    case "only_winning_move": return `next best ${evalShort(d.comparison!.runner_up.eval)}`;
    case "material_sacrifice": return `${d.sacrifice!.kind}${d.sacrifice!.ends_in_mate ? ", then mate" : ""}`;
    case "underpromotion":
      return [{ unique_best: "best move", tied_best: "tied best", not_best: `rank ${d.played_move_rank}`, unknown: "" }[d.best_move ?? "unknown"],
              d.vs_queen && d.vs_queen !== "unknown" ? `${d.vs_queen} than =Q`.replace("equal than", "equal to") : ""]
        .filter(Boolean).join(" · ");
    default: return present(type, d).value;
  }
}

const gameLink = (d: Discovery) => `/g/${d.game.id}?ply=${d.ply}${d.color === "b" ? "&o=black" : ""}`;

/** The record book from ChessTrove's own (native) engine index: players it analysed server-side. */
export function RecordBook({ platform, username, games, engine }: {
  platform: Platform; username: string; games: number; engine: string;
}) {
  return (
    <section className="records" aria-labelledby="records-title">
      <div className="section-head">
        <h2 id="records-title">Engine records</h2>
        <p>{engine} · {plural(games, "game")}. Open a position to review the move.</p>
      </div>
      <div className="records__grid">
        {CARDS.map((c) => <ServerCard key={c.type} {...c} platform={platform} username={username} />)}
      </div>
    </section>
  );
}

function ServerCard({ type, title, empty, platform, username }: {
  type: DiscoveryType; title: string; empty: string; platform: Platform; username: string;
}) {
  const [rows, setRows] = useState<Discovery[] | null>(null);
  useEffect(() => { api.discoveries(platform, username, type).then((r) => setRows(r.results)).catch(() => setRows([])); },
    [platform, username, type]);
  return <Card type={type} title={title} empty={empty} rows={rows} />;
}

/** One discovery type: the top find with its board and evidence, the next few as rows. */
export function Card({ type, title, empty, rows }: { type: DiscoveryType; title: string; empty: string; rows: Discovery[] | null }) {
  const [top, ...rest] = rows ?? [];
  const shown = top && present(type, top);
  return (
    <article className="record" aria-labelledby={`record-${type}`}>
      <header className="record__head">
        <h3 id={`record-${type}`}>{title}</h3>
        {shown && <span className={`record__value ${shown.bad ? "record__value--bad" : ""} ${["only_winning_move", "material_sacrifice", "underpromotion"].includes(type) ? "record__value--move" : ""}`}>{shown.value}</span>}
      </header>
      {!rows ? <p className="loading">Reading…</p> : !top ? <p className="muted">{empty}</p> : (
        <>
          <div className="record__lead">
            <Link to={gameLink(top)} className="record__board" aria-label={`Open the game at ${label(top)}`}>
              <Board fen={shown!.fen} lastMove={shown!.lastMove} arrows={shown!.arrows} orientation={side(top.color)}
                check={top.move?.san.includes("+") || top.move?.san.includes("#")} label={shown!.sentence} />
            </Link>
            <div className="record__text">
              <p>{shown!.sentence}</p>
              <p className="record__meta">vs {top.opponent ?? (top.color === "w" ? top.game.black : top.game.white)} · {formatDate(top.game.played_at)}</p>
              <Link to={gameLink(top)} className="textlink">Review position <ArrowRight size={14} aria-hidden="true" /></Link>
            </div>
          </div>
          {rest.length > 0 && (
            <ol className="record__rest">
              {rest.map((d) => (
                <li key={`${d.game.id}-${d.ply}`}>
                  <Link to={gameLink(d)} className="record__row">
                    <span className="record__move">{label(d) || formatDate(d.game.played_at)}</span>
                    <span className="num">{rowValue(type, d)}</span>
                    <span className="record__meta">vs {d.opponent ?? (d.color === "w" ? d.game.black : d.game.white)} · {formatDate(d.game.played_at)}</span>
                  </Link>
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </article>
  );
}
