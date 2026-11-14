// (7) 站內連結檢查（AR-67）：pages.ts 的每一頁（加上「找不到這一頁」）把連結與按鈕點一遍，
// 沒有一個落到「找不到這一頁」。之後每個 Story 都要通過。
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { MISSING_PATH, SITES } from "./pages.ts";

const NOT_FOUND = TEXT["common:states.notFound.message"];

for (const site of SITES) {
  for (const path of [...site.pages, MISSING_PATH]) {
    test(`${site.name} ${path}: no link or button leads to not found`, async ({ page }) => {
      const open = async () => {
        await page.goto(`${site.baseURL}${path}`);
        await expect(page.locator("main")).toBeVisible();
      };
      await open();
      const linkCount = await page.locator("main a[href]").count();
      const buttonCount = await page.locator("main button").count();

      for (let index = 0; index < linkCount; index += 1) {
        await open();
        const link = page.locator("main a[href]").nth(index);
        const href = (await link.getAttribute("href")) ?? "";
        // 相對網址要對目前這一頁解析，不是對網站根目錄。
        const target = new URL(href, page.url());
        if (target.origin !== site.baseURL) {
          // 站外連結開新分頁（UX「互動」）；站內連結檢查不跟過去。
          await expect(link).toHaveAttribute("target", "_blank");
          continue;
        }
        const before = page.url();
        await link.click();
        // 站內轉址（例如 / 導到 /notice）時落點和 href 不同，所以只等網址離開原本這一頁。
        if (target.href !== before) {
          await expect(page).not.toHaveURL(before);
        }
        await page.waitForLoadState();
        await expect(page.getByText(NOT_FOUND, { exact: true }), `${path} → ${href}`).toHaveCount(0);
      }

      for (let index = 0; index < buttonCount; index += 1) {
        await open();
        await page.locator("main button").nth(index).click();
        await page.waitForLoadState();
        await expect(page.getByText(NOT_FOUND, { exact: true }), `${path} button ${index}`).toHaveCount(0);
      }
    });
  }
}
