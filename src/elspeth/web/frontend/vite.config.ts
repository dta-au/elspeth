/// <reference types="vitest" />
import { defineConfig, searchForWorkspaceRoot } from "vite";
import react from "@vitejs/plugin-react";
import fs from "fs";
import path from "path";

const backendPort = process.env.PLAYWRIGHT_BACKEND_PORT ?? "8451";
const frontendPort = Number(process.env.PLAYWRIGHT_FRONTEND_PORT ?? "5173");

export default defineConfig({
  plugins: [react()],
  build: {
    // Deploy-cache coherence (with the version beacon, f2d105691): keep the
    // previous generations' hashed assets so a stale open tab can still
    // lazy-load ITS chunks after a rebuild — the beacon banner announces the
    // new version; retained assets keep the tab functional until the user
    // refreshes. Unbounded growth is prevented by the postbuild prune
    // (scripts/prune-stale-assets.mjs): rebuilds rewrite every output with a
    // fresh mtime (verified empirically), so age-based pruning can never
    // touch the current generation.
    emptyOutDir: false,
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts", "./src/test/a11y/setup.ts"],
    // src/** for app unit tests, plus the e2e harness's pure logic (the
    // outcome classifier) which deserves fast unit coverage without Playwright.
    include: ["src/**/*.test.{ts,tsx}", "tests/e2e/harness/**/*.test.ts"],
    css: false,
  },
  server: {
    // Git worktrees symlink node_modules to the main checkout, whose REAL
    // path falls outside the worktree's workspace root — without the explicit
    // realpath entry the dev server 404s every file served from node_modules
    // by URL (the @fontsource woff2s), so worktree e2e runs render fallback
    // fonts and produce visual baselines the main checkout can't reproduce.
    // In the main checkout realpath(node_modules) is already inside the
    // workspace root, so this is a no-op there. Dev-server only; no effect
    // on builds.
    fs: {
      allow: [
        searchForWorkspaceRoot(process.cwd()),
        fs.realpathSync(path.resolve(__dirname, "node_modules")),
      ],
    },
    port: frontendPort,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${backendPort}`,
        changeOrigin: true,
      },
      "/ws": {
        target: `ws://127.0.0.1:${backendPort}`,
        ws: true,
      },
    },
  },
});
