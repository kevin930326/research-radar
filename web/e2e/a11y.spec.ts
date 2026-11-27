// (6) axe（WCAG 2.1 A／AA）掃兩種建置的每個頁面，加上「找不到這一頁」；
// 再掃「任一個／同時符合」切換出現的狀態（Story 3.20：選單或手機滿版篩選打開，pages.ts 的 MATCH_SWITCH_STATES）。
import AxeBuilder from "@axe-core/playwright";

import { axeSourceFor } from "./axe-source.ts";
import { expect, test } from "./fixtures.ts";
import { openMatchSwitch } from "./match-switch.ts";
import { MATCH_SWITCH_STATES, MISSING_PATH, SITES } from "./pages.ts";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

for (const site of SITES) {
  for (const path of [...site.pages, MISSING_PATH]) {
    test(`${site.name} ${path}: no axe violations`, async ({ page }) => {
      await page.goto(`${site.baseURL}${path}`);
      await expect(page.locator("main")).toBeVisible();
      const results = await new AxeBuilder({ page, axeSource: axeSourceFor(page) }).withTags(TAGS).analyze();
      expect(results.violations.map((violation) => `${violation.id}: ${violation.help}`)).toEqual([]);
    });
  }
}

for (const state of MATCH_SWITCH_STATES) {
  const site = SITES.find((candidate) => candidate.name === state.site)!;
  test(`${site.name} ${state.path} (${state.phone ? "phone sheet" : "desktop menu"} with the any/all switch): no axe violations`, async ({ page }) => {
    await openMatchSwitch(page, site.baseURL, state);
    // 不檢查 Base UI 的焦點守衛（同 a11y-floor.spec.ts 的說明框：axe 看不到它把焦點轉走，會誤判成 aria-hidden-focus）。
    const results = await new AxeBuilder({ page, axeSource: axeSourceFor(page) }).withTags(TAGS).exclude("[data-base-ui-focus-guard]").analyze();
    expect(results.violations.map((violation) => `${violation.id}: ${violation.help} ${violation.nodes.map((node) => node.html).join(" | ")}`)).toEqual([]);
  });
}
