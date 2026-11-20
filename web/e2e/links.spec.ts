// (7) 站內連結檢查（AR-67）：pages.ts 的每一頁（加上「找不到這一頁」）把連結與按鈕點一遍，
// 每個落點都必須是有效頁面（landing.ts：HTTP 成功、在頁面清單裡、有 h1、沒有「找不到這一頁」）。
// 之後每個 Story 都要通過。
import { errors } from "@playwright/test";

import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { expectDocumentResponsesOk, expectValidLanding, recordDocumentResponses } from "./landing.ts";
import { MISSING_PATH, SITES } from "./pages.ts";

const NOT_FOUND = TEXT["common:states.notFound.message"];
const member = SITES[0]!;
// 按鈕點完等換頁的上限；不換頁的按鈕（例如切換選項）每個都會等滿這段時間。
const BUTTON_NAVIGATION_WAIT_MS = 1_000;

for (const site of SITES) {
  for (const path of [...site.pages, MISSING_PATH]) {
    test(`${site.name} ${path}: no link or button leads to not found`, async ({ page }) => {
      const responses = recordDocumentResponses(page);
      const open = async () => {
        responses.length = 0;
        await page.goto(`${site.baseURL}${path}`);
        // 頁面殼本身也要回 2xx（「找不到這一頁」也是頁面殼，由瀏覽器裡的路由顯示）。
        expectDocumentResponsesOk(responses, `open ${path}`);
        await expect(page.locator("main")).toBeVisible();
        responses.length = 0;
      };
      await open();
      const linkCount = await page.locator("main a[href]").count();
      // 看得到的按鈕才點：同一個按鈕在另一種寬度才出現的（例如論文頁手機版底部的「開啟全文」，Story 3.8）
      // 用 CSS 藏起來，點不到；它在那一種寬度的行為由各自的測試看。
      // 「加入我的文獻庫」「移出」（Story 3.17，data-library）不點：按了會改替身裡的文獻庫，重開這一頁後按鈕數變了；
      // 它們不換頁，行為由 library.spec.ts 看。
      const buttons = page.locator("main button:visible:not([data-library])");
      const buttonCount = await buttons.count();

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
        // 站內轉址（例如 / 導到 /search）時落點和 href 不同，所以只等網址離開原本這一頁。
        if (target.href !== before) {
          await expect(page).not.toHaveURL(before);
        }
        await page.waitForLoadState();
        await expectValidLanding(page, site, responses, `${path} → ${href}`);
      }

      for (let index = 0; index < buttonCount; index += 1) {
        await open();
        const before = page.url();
        await buttons.nth(index).click();
        // 換頁可能是非同步的（點完才發請求、才導航）：先短暫等網址離開原頁，等不到就當作沒換頁。
        try {
          await page.waitForURL((url) => url.toString() !== before, { timeout: BUTTON_NAVIGATION_WAIT_MS });
        } catch (error) {
          if (!(error instanceof errors.TimeoutError)) {
            throw error;
          }
        }
        await page.waitForLoadState();
        const label = `${path} button ${index}`;
        expectDocumentResponsesOk(responses, label);
        await expect(page.getByText(NOT_FOUND, { exact: true }), label).toHaveCount(0);
        // 按鈕換了頁，落點也要是有效頁面；沒換頁（例如切換選項）就只看上面兩項。
        if (page.url() !== before) {
          await expectValidLanding(page, site, responses, label);
        }
      }
    });
  }
}

// 反例：證明上面的落點檢查真的抓得到「HTTP 404」與「不在頁面清單裡」，不是什麼都放過。
test("the landing check fails on a 404 JSON document", async ({ page }) => {
  const url = `${member.baseURL}/api/v1/not-found`;
  // vite preview 沒有 api，/api/ 也會回頁面殼；這裡照 api 的真實行為回 404 JSON。
  await page.route(url, (route) =>
    route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "Not Found" }) }),
  );
  const responses = recordDocumentResponses(page);

  await page.goto(url);

  await expect(page.locator("body")).toContainText("Not Found");
  await expect(expectValidLanding(page, member, responses, "404 JSON")).rejects.toThrow(/HTTP 404/);
});

test("the landing check fails on an address outside the page list", async ({ page }) => {
  const responses = recordDocumentResponses(page);

  await page.goto(`${member.baseURL}${MISSING_PATH}`);

  // HTTP 是 200（頁面殼），擋下它的是頁面清單。
  expectDocumentResponsesOk(responses, MISSING_PATH);
  await expect(expectValidLanding(page, member, responses, MISSING_PATH)).rejects.toThrow(/not a known page/);
});
