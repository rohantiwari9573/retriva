import path from "node:path";
import { defineConfig } from "vitest/config";

// Deliberately minimal: this project has no browser-DOM test setup
// (no jsdom, no React Testing Library) - see docs/frontend.md's "Testing"
// section for exactly what this does and doesn't cover. Pure-logic unit
// tests only, run under Node.
export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
