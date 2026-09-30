import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import "@fontsource-variable/source-serif-4/opsz.css";
import "@fontsource-variable/source-sans-3";
import "@fontsource-variable/source-code-pro";
import "chessground/assets/chessground.base.css";
import "chessground/assets/chessground.cburnett.css";
import "./styles.css";
import { Landing } from "./pages/Landing";
import { Player } from "./pages/Player";
import { Game } from "./pages/Game";
import { NotFound } from "./pages/NotFound";

const EngineLab = lazy(() => import("./pages/EngineLab"));

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/u/:platform/:username" element={<Player />} />
        <Route path="/g/:gameId" element={<Game />} />
        <Route path="/lab/engine" element={<Suspense fallback={null}><EngineLab /></Suspense>} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
