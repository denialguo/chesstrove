import { Board } from "../components/Board";
import { Dial } from "../components/Dial";
import { TopBar } from "../components/TopBar";
import { UsernameForm } from "../components/UsernameForm";
import { ENGINE_LABELS, MOTIFS } from "../lib/motifs";

// A real find from a real history (a public Chess.com game), shown as a sample.
const SAMPLE = {
  fen: "7k/p5pp/b3Q3/3p4/3P4/8/P4nPP/6RK w - - 1 30",
  lastMove: "e4f2",
};

export function Landing() {
  return (
    <div className="page page--landing">
      <TopBar compact={false} />
      <main>
        <section className="hero" aria-labelledby="hero-title">
          <h1 id="hero-title" className="hero__title">Search your chess history.</h1>
          <p className="hero__intro">Smothered mates, underpromotions and missed wins, across your Chess.com or Lichess games.</p>

          <div className="case case--landing">
            <div className="case__plungers">
              <button type="submit" form="hero-form" className="plunger">Open my games</button>
              <span className="plunger plunger--down plunger--decor" aria-hidden="true" />
            </div>
            <div className="case__body">
              <Dial className="dial--form">
                <UsernameForm variant="dial" id="hero-username" formId="hero-form" />
              </Dial>
              <figure className="case__sample">
                <Dial className="dial--board">
                  <Board fen={SAMPLE.fen} lastMove={SAMPLE.lastMove} check label="Sample position: Black's knight on f2 mates the White king on h1, boxed in by its own rook and pawns." />
                </Dial>
                <figcaption>
                  <strong><span className="sample__move">29…Nf2#</span> · Smothered mate</strong>
                  <span>From DankSonPotato’s games, as Black.<br />3-minute game · 13 May 2023</span>
                </figcaption>
              </figure>
            </div>
          </div>
        </section>

        <section className="vocab" aria-labelledby="vocab-title">
          <div className="vocab__intro">
            <h2 id="vocab-title">Mates, promotions &amp; missed chances</h2>
            <p>
              Found from the moves alone, as your history imports. Named mating patterns too, from Anastasia’s to the Opera mate.
            </p>
          </div>
          <div className="vocab__cols">
            <ul className="vocab__list">
              {MOTIFS.map((m) => (
                <li key={m.type}>
                  <span className="glyph">{m.glyph}</span>
                  <span><strong>{m.name}</strong> {m.definition}</span>
                </li>
              ))}
            </ul>
            <div className="vocab__engine">
              <p><strong>With Stockfish</strong> · Optional analysis for winning moves, missed wins and blunders.</p>
              <ul className="vocab__list">
                {ENGINE_LABELS.map((m) => (
                  <li key={m.type}>
                    <span className={`glyph glyph--${m.tone}`}>{m.glyph}</span>
                    <span><strong>{m.name}</strong> {m.definition}</span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </section>
      </main>
      <footer className="footer">
        <p>Public games from Chess.com and Lichess. No account needed.</p>
      </footer>
    </div>
  );
}
