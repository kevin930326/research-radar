// (3) 量位置的版面檢查（EXPERIENCE.md Responsive「使用須知」）與 (4) 三種寬度的整頁截圖。
import { fileURLToPath } from "node:url";

import { expect, test } from "./fixtures.ts";
import { SITES } from "./pages.ts";

const member = SITES[0]!;
const SCREENSHOT_DIR = fileURLToPath(new URL("./screenshots/", import.meta.url));

interface Expected {
  gutter: number;
  /** 兩欄時左欄寬、欄距；上下排時是 null。 */
  columns: { left: number; gap: number } | null;
}

// 含斷點兩側：767／768（手機↔平板留白）、899／900（上下排↔兩欄）、1231／1232（窄兩欄↔寬兩欄）。
const LAYOUTS: Record<number, Expected> = {
  1440: { gutter: 96, columns: { left: 320, gap: 64 } },
  1232: { gutter: 96, columns: { left: 320, gap: 64 } },
  1231: { gutter: 48, columns: { left: 240, gap: 40 } },
  1000: { gutter: 48, columns: { left: 240, gap: 40 } },
  900: { gutter: 48, columns: { left: 240, gap: 40 } },
  899: { gutter: 48, columns: null },
  768: { gutter: 48, columns: null },
  767: { gutter: 20, columns: null },
  500: { gutter: 20, columns: null },
};
const RIGHT_MAX = 680;

for (const [widthText, expected] of Object.entries(LAYOUTS)) {
  const width = Number(widthText);
  test(`member notice layout at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${member.baseURL}/notice`);

    const title = await page.locator("h1").boundingBox();
    expect(title?.x).toBeCloseTo(expected.gutter, 0);

    const sections = page.locator("main section");
    await expect(sections).toHaveCount(2);
    for (let index = 0; index < 2; index += 1) {
      const heading = await sections.nth(index).locator("h2").boundingBox();
      const body = await sections.nth(index).locator(":scope > div").boundingBox();
      if (heading === null || body === null) {
        throw new Error("section is not visible");
      }
      expect(heading.x).toBeCloseTo(expected.gutter, 0);
      if (expected.columns === null) {
        // 上下排：段落標題在上、內文在下，左邊對齊。
        expect(body.x).toBeCloseTo(expected.gutter, 0);
        expect(body.y).toBeGreaterThanOrEqual(heading.y + heading.height - 1);
        expect(body.width).toBeCloseTo(width - expected.gutter * 2, 0);
      } else {
        const { left, gap } = expected.columns;
        const available = width - expected.gutter * 2 - left - gap;
        expect(heading.width).toBeCloseTo(left, 0);
        expect(body.x).toBeCloseTo(expected.gutter + left + gap, 0);
        expect(body.y).toBeCloseTo(heading.y, 0);
        expect(body.width).toBeCloseTo(Math.min(RIGHT_MAX, available), 0);
      }
    }

    // 不會出現左右捲動。
    const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
    expect(scrollWidth).toBeLessThanOrEqual(width);
  });
}

for (const width of [1440, 1000, 500]) {
  test(`member notice screenshot at ${width}px`, async ({ page, browserName }) => {
    test.skip(browserName !== "chromium", "screenshots are taken in chromium only");
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${member.baseURL}/notice`);
    await expect(page.locator("h1")).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `${SCREENSHOT_DIR}notice-${width}.png`, fullPage: true });
  });
}
