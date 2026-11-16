// 共用的 test：每一頁都收集 CSP 違規事件（securitypolicyviolation）與主控台的 CSP 錯誤，
// 測試結束時必須是空的。事件一發生就經 exposeBinding 送回測試這邊，換頁也不會遺失。
// Playwright 的 init script 與 binding 不受頁面 CSP 限制，所以收得到。
// 另外每個測試自動裝上後端替身（api-mock.ts，Story 3.5）：預設已登入；要別的情境就改 apiMock 的欄位。
import { test as base, expect } from "@playwright/test";

import { createApiState, installApiMock, type ApiMockState } from "./api-mock.ts";

declare global {
  interface Window {
    __reportCspViolation?: (violation: string) => Promise<void>;
  }
}

export const test = base.extend<{ cspViolations: string[]; apiMock: ApiMockState }>({
  apiMock: [
    async ({ page }, use) => {
      const state = createApiState();
      await installApiMock(page, state);
      await use(state);
    },
    { auto: true },
  ],
  cspViolations: [
    async ({ page }, use) => {
      const violations: string[] = [];
      await page.exposeBinding("__reportCspViolation", ({ frame }, violation: string) => {
        violations.push(`${frame.url()} ${violation}`);
      });
      await page.addInitScript(() => {
        document.addEventListener("securitypolicyviolation", (event) => {
          void window.__reportCspViolation?.(`${event.violatedDirective} ${event.blockedURI}`);
        });
      });
      page.on("console", (message) => {
        if (/content.security.policy/i.test(message.text())) {
          violations.push(message.text());
        }
      });
      await use(violations);
      expect(violations).toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };
