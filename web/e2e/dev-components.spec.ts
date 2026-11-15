// 元件示範頁（/dev/components，Story 3.2）：六種元件都在、AI 標章的說明框點開有完整說法／模型／台北日期、
// Esc 關閉；1440／1000／500 量位置；三張截圖（chromium，先點開一個說明框）。
import { fileURLToPath } from "node:url";

import dev from "../src/i18n/zh-TW/dev.json" with { type: "json" };
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { DEV_SITE } from "./pages.ts";

const URL_ = `${DEV_SITE.baseURL}${DEV_SITE.pages[0]}`;
const SCREENSHOT_DIR = fileURLToPath(new URL("./screenshots/", import.meta.url));
const SECTIONS = dev.dev.sections;
const EXAMPLES = dev.dev.examples;
// dev-showcase.ts 的範例來歷：UTC 2026-11-11T17:30:00Z 是台北的 2026-11-12。
const MODEL = "claude-haiku-5-5";
const TAIPEI_DATE = "2026-11-12";
const GUTTERS: Record<number, number> = { 1440: 96, 1000: 48, 500: 20 };

test("lists the six kinds of components", async ({ page }) => {
  await page.goto(URL_);
  await expect(page.locator("h1")).toHaveText(dev.dev.page.title);
  await expect(page.locator("main section h2")).toHaveText([
    SECTIONS.colors,
    SECTIONS.buttons,
    SECTIONS.links,
    SECTIONS.aiBadges,
    SECTIONS.statusTags,
    SECTIONS.focus,
  ]);
  await expect(page.locator("[data-color-token]")).toHaveCount(19);
  await expect(page.locator("[data-type-token]")).toHaveCount(21);

  const buttons = page.locator("[data-dev-section='buttons'] button");
  await expect(buttons).toHaveText([EXAMPLES.primary, EXAMPLES.secondary, EXAMPLES.pending].map((text) => new RegExp(text)));
  await expect(buttons.nth(2)).toBeDisabled();

  const links = page.locator("[data-dev-section='links'] a");
  await expect(links.nth(0)).toHaveAttribute("href", "/notice");
  await expect(links.nth(0)).not.toHaveAttribute("target", /./);
  await expect(links.nth(1)).toHaveAttribute("target", "_blank");
  await expect(links.nth(1)).toHaveAttribute("rel", "noopener noreferrer");
  await expect(links.nth(1)).toHaveText(/↗$/);

  await expect(page.locator("[data-dev-section='ai-badges'] [data-ai-badge]")).toHaveText([
    `◇${TEXT["ai:ai.badge.full"]}`,
    `◇${TEXT["ai:ai.badge.short"]}`,
    `◆${TEXT["ai:ai.badge.verified"]}`,
  ]);

  const tags = page.locator("[data-dev-section='status-tags'] [data-status-tag]");
  await expect(tags).toHaveText([EXAMPLES.outOfScope, EXAMPLES.private]);
  // 有說明的可點（包在按鈕裡），純標示的不可點。
  await expect(page.locator("[data-dev-section='status-tags'] button")).toHaveCount(1);

  await expect(page.locator("[data-focus-sample]")).toHaveCSS("outline-style", "solid");
});

test("the AI badge popover shows the full wording, model and Taipei date, and Esc closes it", async ({ page }) => {
  await page.goto(URL_);
  const badges = page.locator("[data-dev-section='ai-badges'] button");

  await badges.nth(1).click();
  const popup = page.locator("[data-info-popover]");
  await expect(popup).toBeVisible();
  await expect(popup).toContainText(TEXT["ai:ai.badge.full"]);
  await expect(popup.locator("[data-provenance='model']")).toHaveText(MODEL);
  await expect(popup.locator("[data-provenance='date']")).toHaveText(TAIPEI_DATE);
  await page.keyboard.press("Escape");
  await expect(popup).toHaveCount(0);

  await badges.nth(2).click();
  await expect(popup).toContainText(TEXT["ai:ai.badge.verifiedFull"]);
  await page.keyboard.press("Escape");
  await expect(popup).toHaveCount(0);
});

test("the popover also opens on hover and from the keyboard", async ({ page }) => {
  await page.goto(URL_);
  const trigger = page.locator("[data-dev-section='ai-badges'] button").first();
  const popup = page.locator("[data-info-popover]");

  // 滑過出現、移開消失。
  await trigger.hover();
  await expect(popup).toBeVisible();
  await page.mouse.move(0, 0);
  await expect(popup).toHaveCount(0);

  // 鍵盤聚焦後按 Enter 出現、Esc 消失。
  await trigger.focus();
  await page.keyboard.press("Enter");
  await expect(popup).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(popup).toHaveCount(0);
});

test("a status tag with an explanation opens a popover", async ({ page }) => {
  await page.goto(URL_);
  await page.locator("[data-dev-section='status-tags'] button").click();
  await expect(page.locator("[data-info-popover]")).toHaveText(EXAMPLES.outOfScopeInfo);
  await page.keyboard.press("Escape");
  await expect(page.locator("[data-info-popover]")).toHaveCount(0);
});

for (const [widthText, gutter] of Object.entries(GUTTERS)) {
  const width = Number(widthText);

  test(`layout at ${width}px: page gutters and no horizontal scroll`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(URL_);
    const title = await page.locator("h1").boundingBox();
    expect(title?.x).toBeCloseTo(gutter, 0);
    for (const box of await Promise.all(
      (await page.locator("main section").all()).map((section) => section.boundingBox()),
    )) {
      expect(box?.x).toBeCloseTo(gutter, 0);
      expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(width - gutter + 0.5);
    }
    const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
    expect(scrollWidth).toBeLessThanOrEqual(width);
  });

  test(`screenshot at ${width}px`, async ({ page, browserName }) => {
    test.skip(browserName !== "chromium", "screenshots are taken in chromium only");
    await page.setViewportSize({ width, height: 900 });
    await page.goto(URL_);
    await expect(page.locator("h1")).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    await page.locator("[data-dev-section='ai-badges'] button").first().click();
    await expect(page.locator("[data-info-popover]")).toBeVisible();
    // 等淡入（0.2 秒）結束再截。
    await expect(page.locator("[data-info-popover]")).toHaveCSS("opacity", "1");
    await page.screenshot({ path: `${SCREENSHOT_DIR}dev-components-${width}.png`, fullPage: true });
  });
}
