import { Link } from "react-router-dom";
import { TopBar } from "../components/TopBar";

export function NotFound() {
  return (
    <div className="page">
      <TopBar />
      <main className="empty">
        <h1>No page here.</h1>
        <p>Look up a player above, or <Link to="/">go to the start</Link>.</p>
      </main>
    </div>
  );
}
