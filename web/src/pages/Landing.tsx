import { Board } from "../components/Board";
import { Dial } from "../components/Dial";
import { TopBar } from "../components/TopBar";
import { UsernameForm } from "../components/UsernameForm";
import { ENGINE_LABELS, MOTIFS } from "../lib/motifs";

// A real find from a real history (a public Chess.com game), shown as a sample.
const SAMPLE = {
  fen: "7k/p5pp/b3Q3/3p4/3P4/8/P4nPP/6RK w - - 1 30",
  lastMove: "e4f2",
  caption: "Sample from a real history: DankSonPotato, playing Black, mates with 29…Nf2#, a smothered mate. 3-minute game, 13 May 2023.",
};

export function Landing() {
  return (
    <div className="page page--landing">
      <TopBar compact={false} />
      <main>
        <section className="hero" aria-labelledby="hero-title">
          <h1 id="hero-title" className="hero__title">Every game you’ve played, searched for the moves worth keeping.</h1>

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
                <figcaption>{SAMPLE.caption}</figcaption>
              </figure>
            </div>
          </div>
        </section>

        <section className="vocab" aria-labelledby="vocab-title">
          <div className="vocab__intro">
            <h2 id="vocab-title">What it looks for</h2>
            <p>
              Every move of every game is checked for these. The first list needs nothing but the rules of chess and
              is ready as soon as your games are in. The second comes from Stockfish, and every verdict names the engine
              and settings behind it.
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
            <ul className="vocab__list vocab__list--engine">
              {ENGINE_LABELS.map((m) => (
                <li key={m.type}>
                  <span className={`glyph glyph--${m.tone}`}>{m.glyph}</span>
                  <span><strong>{m.name}</strong> {m.definition}</span>
                </li>
              ))}
            </ul>
          </div>
        </section>
      </main>
      <footer className="footer">
        <p>Games are read from the public Chess.com and Lichess APIs. There’s nothing to sign up for.</p>
      </footer>
    </div>
  );
}
