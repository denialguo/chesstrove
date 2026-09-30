import { useEffect, useState, type ReactNode } from "react";

// A first request that's still out after a few seconds is almost always the API server waking up (the free
// host sleeps when it's quiet). Say so, once, instead of looking stuck.
export function Waiting({ children }: { children: ReactNode }) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setSlow(true), 4000);
    return () => clearTimeout(t);
  }, []);
  return (
    <div className="loading" aria-live="polite">
      <p>{children}</p>
      {slow && <p className="loading__slow">Waking ChessTrove’s server. This can take up to a minute; it keeps trying.</p>}
    </div>
  );
}

/** The API didn't answer at all (asleep past the timeout, down, or offline). The page itself still works. */
export function Unreachable({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="empty">
      <h1>ChessTrove’s server didn’t answer.</h1>
      <p>It may still be starting up. Give it a moment and try again.</p>
      <button type="button" className="engine-btn" onClick={onRetry}>Try again</button>
    </div>
  );
}
