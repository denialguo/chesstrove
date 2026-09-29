import { Link } from "react-router-dom";
import { UsernameForm } from "./UsernameForm";

export function TopBar({ compact = true }: { compact?: boolean }) {
  return (
    <header className="topbar">
      <Link to="/" className="wordmark" aria-label="ChessTrove home">
        <svg viewBox="0 0 32 32" aria-hidden="true" className="wordmark__mark">
          <circle cx="16" cy="17" r="12" className="wordmark__dial" />
          <path d="M16 17V8.5" className="wordmark__hand" />
          <path d="M16 17l5.5 3.3" className="wordmark__hand wordmark__hand--short" />
          <rect x="12.5" y="1" width="7" height="3.5" rx="1.2" className="wordmark__plunger" />
        </svg>
        <span>ChessTrove</span>
      </Link>
      {compact && <UsernameForm variant="bar" />}
    </header>
  );
}
