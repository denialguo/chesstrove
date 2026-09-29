import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRight } from "lucide-react";
import { PLATFORM_NAME, type Platform } from "../lib/api";

const VALID = /^[A-Za-z0-9_-]{2,50}$/;

/** Username + platform. `dial` sits on the landing clock face (its submit is the case's brass plunger,
 * linked by `formId`); `bar` lives in the top bar. */
export function UsernameForm({ variant, id = "username", formId }: { variant: "dial" | "bar"; id?: string; formId?: string }) {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [platform, setPlatform] = useState<Platform>("chesscom");
  const [error, setError] = useState<string | null>(null);

  function submit(e: FormEvent) {
    e.preventDefault();
    const name = username.trim();
    if (!VALID.test(name)) {
      setError(name ? "Usernames use letters, numbers, - and _ only." : "Enter a username.");
      return;
    }
    navigate(`/u/${platform}/${name.toLowerCase()}`);
  }

  return (
    <form id={formId} className={`uform uform--${variant}`} onSubmit={submit} noValidate>
      <label htmlFor={id} className="uform__label">{variant === "dial" ? "Player" : "Look up a player"}</label>
      <input
        id={id}
        className="uform__input"
        value={username}
        onChange={(e) => { setUsername(e.target.value); setError(null); }}
        placeholder="username"
        autoComplete="off"
        autoCapitalize="none"
        spellCheck={false}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-error` : undefined}
      />
      <div className="uform__platforms" role="radiogroup" aria-label="Site">
        {(Object.keys(PLATFORM_NAME) as Platform[]).map((p) => (
          <button key={p} type="button" role="radio" aria-checked={platform === p}
            className="uform__platform" onClick={() => setPlatform(p)}>
            {PLATFORM_NAME[p]}
          </button>
        ))}
      </div>
      {error && <p id={`${id}-error`} className="uform__error" role="alert">{error}</p>}
      {variant === "bar" && (
        <button type="submit" className="uform__go" aria-label="Open games"><ArrowRight size={18} strokeWidth={2.25} /></button>
      )}
    </form>
  );
}
