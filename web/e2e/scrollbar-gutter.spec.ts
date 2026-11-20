// 短頁長頁內容左緣相同（從 page-frame.spec.ts 拆出來：Playwright 不准在 describe 裡改啟動參數，只能整個檔案改）。
import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures.ts";
import { MISSING_PATH, SITES } from "./pages.ts";

const member = SITES[0]!;

/** main 的左緣加左邊留白，就是內容左緣。 */
async function contentX(page: Page) {
  const main = page.locator("main");
  await expect(main).toBeVisible();
  const rect = await main.boundingBox();
  if (rect === null) throw new Error("main has no box");
  const paddingLeft = await main.evaluate((node) => parseFloat(getComputedStyle(node).paddingLeft));
  return rect.x + paddingLeft;
}

// 一律保留捲軸的位置（Kevin 2026-11-21 決定，epic-3g P2-1）：很短的頁面（找不到這一頁）和很長的頁面（使用須知），
// 內容左緣一樣。Playwright 的無頭 chromium 預設帶 --hide-scrollbars，Mac 上的 chromium 還預設用浮在內容上的捲軸，
// 兩種都不佔寬度（左緣加不加這行都一樣）。所以這個檔案只在 chromium 拿掉 --hide-scrollbars、關掉浮動捲軸
//（--disable-features=OverlayScrollbar；本機 Mac 量過兩個都要才有 15px 寬的捲軸），讓長頁真的有佔寬度的捲軸。
// 視窗用 1920（外框置中，捲軸寬度會讓左緣移動；1280 時外框貼齊左邊，量不出差別）。
// firefox、webkit 照預設跑：本機 webkit 量過捲軸不佔寬度，firefox 沒量過；
// 在這兩種瀏覽器上只靠 <html> 的 scrollbar-gutter 是 stable 這條斷言擋「這行被拿掉或寫錯」。
test.use({
  launchOptions: [
    async ({ browserName }, use) => {
      await use(
        browserName === "chromium"
          ? { ignoreDefaultArgs: ["--hide-scrollbars"], args: ["--disable-features=OverlayScrollbar"] }
          : {},
      );
    },
    { scope: "worker" },
  ],
});

test("short and long pages keep the content's left edge (scrollbar-gutter: stable)", async ({ page, browserName }) => {
  await page.setViewportSize({ width: 1920, height: 900 });
  const edges: number[] = [];
  for (const [path, tall] of [
    [MISSING_PATH, false],
    ["/notice", true],
  ] as const) {
    await page.goto(`${member.baseURL}${path}`);
    await expect(page.locator("main h1")).toBeVisible();
    const sizes = await page.evaluate(() => ({
      scrollHeight: document.documentElement.scrollHeight,
      innerHeight: window.innerHeight,
      scrollbarWidth: window.innerWidth - document.documentElement.clientWidth,
      gutter: getComputedStyle(document.documentElement).scrollbarGutter,
    }));
    expect(sizes.scrollHeight > sizes.innerHeight, `${path} scrolls`).toBe(tall);
    expect(sizes.gutter, `${path} scrollbar-gutter`).toBe("stable");
    // chromium 長頁的捲軸一定要佔寬度：沒有的話這條測試量不出差別，先失敗讓人知道。
    if (browserName === "chromium" && tall) {
      expect(sizes.scrollbarWidth, `${path} scrollbar width`).toBeGreaterThan(0);
    }
    // 內容左緣：main 的左緣加左邊留白（兩頁的標題排法不同，不拿標題比）。
    edges.push(await contentX(page));
  }
  expect(edges[1]).toBeCloseTo(edges[0]!, 0);
});
