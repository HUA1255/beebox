import { defineConfig, devices } from "@playwright/test";

const port = Number(process.env.BEEBOX_CONSOLE_SMOKE_PORT ?? 17497);
const python = process.env.BEEBOX_PYTHON ?? "python";

// One real BeeBox process, started by scripts/run_console_smoke.py with the
// fake speech engine and fake video platform. No models, GPU or network.
export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `"${python}" scripts/run_console_smoke.py --port ${port}`,
    cwd: "..",
    url: `http://127.0.0.1:${port}/v1/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
