import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: Vite serves the app and proxies /api to `chesstrove serve` (port 8000).
// Build: the static app lands inside the Python package, so `chesstrove serve` can host both locally.
// Vercel builds the same app into dist/ (web/vercel.json) and sets VITE_API_URL to the API's own domain.
export default defineConfig({
  plugins: [react()],
  // fs.allow: the indexing worker bundles ChessTrove's Python core straight from ../src/chesstrove
  server: { proxy: { "/api": "http://127.0.0.1:8000" }, fs: { allow: [".."] } },
  worker: { format: "es" },
  build: { outDir: "../src/chesstrove/web", emptyOutDir: true },
});
