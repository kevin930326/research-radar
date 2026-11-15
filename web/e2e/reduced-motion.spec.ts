// 減少動態（UX-DR6、Story 3.2）：元件示範頁上抽查幾個有過場的元素。沒設定時有過場；
// 模擬 prefers-reduced-motion: reduce 之後，同一批元素的過場與動畫都關掉（styles.css 的一條全站規則），
// 共用函式（use-reduced-motion.ts，示範頁把結果寫在 data-reduced-motion）也跟著變。
// 說明框的淡入（目前唯一的淡入）與一個測試注入的 @keyframes 動畫也要被關掉。
import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures.ts";
import { DEV_SITE } from "./pages.ts";

const SAMPLES = [
  "main button[data-variant='primary']:not([disabled])",
  "main button[data-variant='secondary']",
  "main button[data-variant='primary']:not([disabled]) span[aria-hidden='true']",
];

async function motion(page: Page, selector: string): Promise<{ duration: string; animation: string }> {
  return page.locator(selector).first().evaluate((element) => {
    const style = getComputedStyle(element);
    return { duration: style.transitionDuration, animation: style.animationName };
  });
}

test("reduced motion turns off transitions and animations site-wide", async ({ page }) => {
  await page.goto(`${DEV_SITE.baseURL}${DEV_SITE.pages[0]}`);
  await expect(page.locator("main h1")).toBeVisible();
  await expect(page.locator("[data-reduced-motion]")).toHaveAttribute("data-reduced-motion", "false");
  for (const selector of SAMPLES) {
    const { duration } = await motion(page, selector);
    expect(duration.split(",").some((value) => Number.parseFloat(value) > 0), selector).toBe(true);
  }

  await page.addStyleTag({
    content: "@keyframes rr-test-spin { to { rotate: 1turn; } } #rr-test-animated { animation: rr-test-spin 1s infinite; }",
  });
  await page.evaluate(() => {
    const element = document.createElement("div");
    element.id = "rr-test-animated";
    document.body.append(element);
  });
  expect((await motion(page, "#rr-test-animated")).animation).toBe("rr-test-spin");

  await page.emulateMedia({ reducedMotion: "reduce" });

  await expect(page.locator("[data-reduced-motion]")).toHaveAttribute("data-reduced-motion", "true");
  expect((await motion(page, "#rr-test-animated")).animation).toBe("none");
  await page.locator("[data-dev-section='ai-badges'] button").first().click();
  await expect(page.locator("[data-info-popover]")).toBeVisible();
  const popup = await motion(page, "[data-info-popover]");
  expect(popup.duration.split(",").every((value) => Number.parseFloat(value) === 0)).toBe(true);
  await page.keyboard.press("Escape");
  for (const selector of SAMPLES) {
    const { duration, animation } = await motion(page, selector);
    expect(duration.split(",").every((value) => Number.parseFloat(value) === 0), selector).toBe(true);
    expect(animation, selector).toBe("none");
  }

  await page.emulateMedia({ reducedMotion: "no-preference" });
  await expect(page.locator("[data-reduced-motion]")).toHaveAttribute("data-reduced-motion", "false");
});
