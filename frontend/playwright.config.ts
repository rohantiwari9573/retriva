import { defineConfig, devices } from "@playwright/test";

/**
 * E2E config for the LOCAL Docker stack only (http://localhost:3000/8000).
 * Never points at the AWS deployment - that instance has no LM Studio, and
 * more importantly these tests create/delete real accounts/data, which has
 * no business touching the public demo. See docs/e2e-testing.md for the
 * "local vs AWS" distinction this project is explicit about everywhere else.
 *
 * Does NOT start its own dev server - `docker compose up` is assumed
 * already running (same precondition as every other local-stack workflow
 * in this repo). Failing fast with a clear connection error if it isn't is
 * preferable to this config silently spinning up a second, differently
 * configured instance.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  retries: 0,
  workers: 1,
  // Document processing genuinely takes ~75-80s to reach FAILED when LM
  // Studio is unreachable (3 retries with backoff - see
  // backend/app/workers/tasks/document_processing.py and
  // DOCUMENT_PROCESSING_MAX_RETRIES), confirmed by direct observation
  // (polled and logged the real timing before picking this number - not
  // guessed). The default 30s per-test timeout is far too short for that
  // one step regardless of a longer per-assertion timeout, since the
  // overall test timeout wins either way.
  timeout: 150_000,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
