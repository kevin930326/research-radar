// 整頁外框（ADR-0054）：導覽列、內容兩塊寬度 100%、最寬 1280px（含左右留白各 96px，內容 1088px）、左右置中。
// 視窗 1920 時兩塊都是 x 320、寬 1280，內容從 416 開始、寬 1088；視窗 1280 時和改之前一樣（x 0、內容從 96 開始）。
// 預期值都是寫死的；1440 與 1280 以下各斷點另由 layout.spec.ts 量使用須知。頁尾還沒做（Story 3.15）。
import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures.ts";
import { MISSING_PATH, PAPER_PAGES, SITES } from "./pages.ts";

const member = SITES[0]!;
const demo = SITES[1]!;

interface Frame {
  /** 導覽列與內容區（main）的左緣與寬度。 */
  x: number;
  width: number;
  /** 去掉左右留白之後，內容的左緣與寬度。 */
  contentX: number;
  contentWidth: number;
}

const FRAMES: Record<number, Frame> = {
  1920: { x: 320, width: 1280, contentX: 416, contentWidth: 1088 },
  1280: { x: 0, width: 1280, contentX: 96, contentWidth: 1088 },
};

/** 有導覽列的成員版頁面（論文頁取版面最滿的那一篇）。 */
const WITH_NAV = ["/search", "/library", "/notice", PAPER_PAGES[0]!];

async function box(page: Page, selector: string) {
  const element = page.locator(selector);
  await expect(element).toBeVisible();
  const rect = await element.boundingBox();
  if (rect === null) throw new Error(`${selector} has no box`);
  const padding = await element.evaluate((node) => {
    const style = getComputedStyle(node);
    return { left: parseFloat(style.paddingLeft), right: parseFloat(style.paddingRight) };
  });
  return { ...rect, contentX: rect.x + padding.left, contentWidth: rect.width - padding.left - padding.right };
}

function expectFrame(actual: Awaited<ReturnType<typeof box>>, expected: Frame, label: string) {
  expect(actual.x, `${label} x`).toBeCloseTo(expected.x, 0);
  expect(actual.width, `${label} width`).toBeCloseTo(expected.width, 0);
  expect(actual.contentX, `${label} content x`).toBeCloseTo(expected.contentX, 0);
  expect(actual.contentWidth, `${label} content width`).toBeCloseTo(expected.contentWidth, 0);
}

for (const [widthText, expected] of Object.entries(FRAMES)) {
  const width = Number(widthText);

  for (const path of WITH_NAV) {
    test(`page frame at ${width}px: nav bar and content of ${path}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(`${member.baseURL}${path}`);
      await expect(page.locator("main h1")).toBeVisible();
      expectFrame(await box(page, "header"), expected, "header");
      expectFrame(await box(page, "main"), expected, "main");
      const title = await page.locator("main h1").boundingBox();
      if (title === null) throw new Error(`${path}: main h1 has no box`);
      expect(title.x, `${path} h1 x`).toBeCloseTo(expected.contentX, 0);
      // 有輔助欄的論文頁：輔助欄的右緣也在內容右緣（不再貼著視窗右緣，ADR-0054 背景）。
      if (path === PAPER_PAGES[0]) {
        const rail = await page.locator("aside").boundingBox();
        if (rail === null) throw new Error(`${path}: aside has no box`);
        expect(rail.x + rail.width, `${path} rail right edge`).toBeCloseTo(expected.contentX + expected.contentWidth, 0);
      }
      const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
      expect(scrollWidth).toBeLessThanOrEqual(width);
    });
  }

  for (const site of [member, demo]) {
    test(`page frame at ${width}px: ${site.name} not-found page`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(`${site.baseURL}${MISSING_PATH}`);
      await expect(page.locator("main h1")).toBeVisible();
      expectFrame(await box(page, "main"), expected, "main");
    });
  }

  // 登入頁自己排版：360px 的一欄在視窗正中間，外框上限不影響它（改之前也是置中）。
  test(`page frame at ${width}px: login column stays centered`, async ({ page, apiMock }) => {
    apiMock.member = null;
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${member.baseURL}/login`);
    const column = page.locator("main > div").first();
    await expect(column).toBeVisible();
    const rect = await column.boundingBox();
    if (rect === null) throw new Error("login column has no box");
    expect(rect.width, "login column width").toBeCloseTo(360, 0);
    expect(rect.x, "login column x").toBeCloseTo((width - 360) / 2, 0);
  });
}

// 短頁長頁左緣相同（scrollbar-gutter: stable）的測試在 scrollbar-gutter.spec.ts：要改瀏覽器啟動參數，只能放在自己的檔案。
