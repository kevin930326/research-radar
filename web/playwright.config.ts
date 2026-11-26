// Playwright（ADR-0010）：瀏覽器測試跑在帶 CSP 的建置結果上（vite preview 讀同一份安全標頭）。
// 元件示範頁只在開發環境，所以另開一個成員版的開發伺服器（沒有 CSP，不放進 e2e/pages.ts 的 SITES）。
// 本機只跑 chromium；CI 跑 chromium、firefox、webkit 三種引擎。
import { defineConfig, devices } from "@playwright/test";

import { DEMO_PORT, DEV_PORT, MEMBER_PORT } from "./e2e/sites.ts";

const isCI = Boolean(process.env.CI);

const browsers = isCI
  ? [
      { name: "chromium", use: { ...devices["Desktop Chrome"] } },
      // 【探測分支專用】PROBE_PREFS（JSON）給 firefox 的偏好設定。
      {
        name: "firefox",
        use: {
          ...devices["Desktop Firefox"],
          launchOptions: { firefoxUserPrefs: JSON.parse(process.env.PROBE_PREFS ?? "{}") as Record<string, boolean | number | string> },
        },
      },
      { name: "webkit", use: { ...devices["Desktop Safari"] } },
    ]
  : [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }];

export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  forbidOnly: isCI,
  // CI 上失敗時自動再試一次；本機不重試。原因：GitHub 的機器上 Firefox 跑 axe 掃描時偶爾整頁當掉
  //（frame.evaluate: Page crashed），每次當在不同頁面，不是程式的問題（ADR-0010 的 2026-11-17 更新）。
  // 重試才過的測試在報告裡會標成 flaky，看得到。
  retries: isCI ? 1 : 0,
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
    {
      command: `VITE_BUILD_TARGET=member npx vite --port ${DEV_PORT} --strictPort --host 127.0.0.1`,
      url: `http://127.0.0.1:${DEV_PORT}/dev/components`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
