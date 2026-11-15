// Playwright（ADR-0010）：瀏覽器測試跑在帶 CSP 的建置結果上（vite preview 讀同一份安全標頭）。
// 本機只跑 chromium；CI 跑 chromium、firefox、webkit 三種引擎。
import { defineConfig, devices } from "@playwright/test";

import { DEMO_PORT, MEMBER_PORT } from "./e2e/sites.ts";

const isCI = Boolean(process.env.CI);

const browsers = isCI
  ? [
      { name: "chromium", use: { ...devices["Desktop Chrome"] } },
      { name: "firefox", use: { ...devices["Desktop Firefox"] } },
      { name: "webkit", use: { ...devices["Desktop Safari"] } },
    ]
  : [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }];

export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  forbidOnly: isCI,
  retries: 0,
  workers: isCI ? 2 : undefined,
  reporter: isCI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    trace: "retain-on-failure",
  },
  projects: browsers,
  webServer: [
    {
      command: `npm run build:member && VITE_BUILD_TARGET=member npx vite preview --port ${MEMBER_PORT} --strictPort`,
      url: `http://127.0.0.1:${MEMBER_PORT}/notice`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `npm run build:demo && VITE_BUILD_TARGET=demo npx vite preview --port ${DEMO_PORT} --strictPort`,
      url: `http://127.0.0.1:${DEMO_PORT}/`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
