// 使用須知的段落標題間距（Story 3.2 把 3.1 的 `mb-4 split:mb-0` 換成 `max-split:mb-4`）：
// 上下排（899 以下）時段落標題和內文之間是 16px；兩欄（900 以上）時兩欄的頂端對齊。
// layout.spec.ts（3.1）只檢查內文在標題下方，這裡量實際的距離。
import { expect, test } from "./fixtures.ts";
import { SITES } from "./pages.ts";

const member = SITES[0]!;
const STACKED_GAP = 16;

for (const width of [899, 768, 500, 1000, 1440]) {
  test(`member notice heading spacing at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${member.baseURL}/notice`);
    const sections = page.locator("main section");
    await expect(sections).toHaveCount(2);
    for (let index = 0; index < 2; index += 1) {
      const heading = await sections.nth(index).locator("h2").boundingBox();
      const body = await sections.nth(index).locator(":scope > div").boundingBox();
      if (heading === null || body === null) {
        throw new Error("section is not visible");
      }
      if (width < 900) {
        expect(Math.abs(body.y - (heading.y + heading.height) - STACKED_GAP)).toBeLessThanOrEqual(1);
      } else {
        expect(Math.abs(body.y - heading.y)).toBeLessThanOrEqual(1);
      }
    }
  });
}
