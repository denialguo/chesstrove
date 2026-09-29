import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: Vite serves the app and proxies /api to `chesstrove serve` (port 8000).
// Build: the static app lands inside the Python package, so one server hosts both.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
  build: { outDir: "../src/chesstrove/web", emptyOutDir: true },
});
