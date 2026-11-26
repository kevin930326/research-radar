// (7) 站內連結檢查（AR-67）：pages.ts 的每一頁（加上「找不到這一頁」）把連結與按鈕點一遍，
// 每個落點都必須是有效頁面（landing.ts：HTTP 成功、在頁面清單裡、有 h1、沒有「找不到這一頁」）。
// 之後每個 Story 都要通過。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { errors, type Page } from "@playwright/test";

import { LIST_FIXTURES, createApiState } from "./api-mock.ts";
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { expectDocumentResponsesOk, expectValidLanding, recordDocumentResponses } from "./landing.ts";
import { MISSING_PATH, SITES } from "./pages.ts";

const NOT_FOUND = TEXT["common:states.notFound.message"];
const member = SITES[0]!;
// 按鈕點完等換頁的上限；不換頁的按鈕（例如切換選項）每個都會等滿這段時間。
const BUTTON_NAVIGATION_WAIT_MS = 1_000;

/**
 * 有清單的頁面載入完之後一定有的列數（從假資料算；N1，Kevin 2026-11-20）：找論文的起始狀態列本週新增的前 3 篇，
 * 我的文獻庫列替身文獻庫一開始的那幾篇（假清單裡 personal 的列，一頁放得下）。每一列的標題都是連結。
 */
const LIST_ROWS: Record<string, { selector: string; count: number }> = {
  "/search": { selector: "[data-paper-row]", count: Math.min(3, LIST_FIXTURES.rows.filter((entry) => entry.this_week).length) },
  "/library": {
    selector: "[data-library-row]",
    count: Math.min(LIST_FIXTURES.rows.filter((entry) => entry.personal).length, LIST_FIXTURES.clientConfig.library_page_size),
  },
};

/**
 * 示範站論文清單（Story 3.14）一次列 50 篇、每列有好幾個連結，每點一個都要重開一次頁面，30 秒點不完（固定逾時，
 * 顧問 2026-11-26 查出、Kevin 准許）：論文列只點前 3 列裡的連結與按鈕，列以外的連結與按鈕照舊全部點。
 * 列數從範例匯出算（manifest 的篇數與一次顯示的篇數取小的），先確認清單真的列出來了才往下數。
 */
const DEMO_FIXTURE = fileURLToPath(new URL("./fixtures/demo-preview/manifest.json", import.meta.url));
const DEMO_MANIFEST = JSON.parse(readFileSync(DEMO_FIXTURE, "utf-8")) as {
  summary: { paper_count: number };
  client_config: { paper_list_page_size: number };
};
const DEMO_ROWS_CLICKED = 3;
const DEMO_LIST_ROWS: Record<string, { selector: string; count: number }> = {
  "/papers": {
    selector: "[data-paper-row]",
    count: Math.min(DEMO_MANIFEST.summary.paper_count, DEMO_MANIFEST.client_config.paper_list_page_size),
  },
};

/** 這一頁要點的元素的位置（第幾個）：在第 DEMO_ROWS_CLICKED 列以後的論文列裡的跳過，其他全留。 */
async function indexesToClick(
  page: Page,
  selector: string,
  rowSelector: string | undefined,
): Promise<number[]> {
  const total = await page.locator(selector).count();
  if (rowSelector === undefined) {
    return Array.from({ length: total }, (_, index) => index);
  }
  const skip = await page.locator(selector).evaluateAll(
    (elements, [rows, keep]) => {
      const all = Array.from(document.querySelectorAll(rows as string));
      return elements.map((element) => {
        const row = element.closest(rows as string);
        return row !== null && all.indexOf(row) >= (keep as number);
      });
    },
    [rowSelector, DEMO_ROWS_CLICKED] as const,
  );
  return skip.flatMap((skipped, index) => (skipped ? [] : [index]));
}

for (const site of SITES) {
  for (const path of [...site.pages, MISSING_PATH]) {
    test(`${site.name} ${path}: no link or button leads to not found`, async ({ page, apiMock }) => {
      const responses = recordDocumentResponses(page);
      const open = async () => {
        // 每次重開這一頁前把替身的文獻庫重設回一開始的樣子（E1，Kevin 2026-11-20）：上一輪點過的「加入我的文獻庫」
        // 「移出」（Story 3.17）會改替身裡的文獻庫，不重設的話重開後按鈕數就變了。
        const fresh = createApiState();
        apiMock.libraryIds = fresh.libraryIds;
        apiMock.libraryRemoved = fresh.libraryRemoved;
        responses.length = 0;
        await page.goto(`${site.baseURL}${path}`);
        // 頁面殼本身也要回 2xx（「找不到這一頁」也是頁面殼，由瀏覽器裡的路由顯示）。
        expectDocumentResponsesOk(responses, `open ${path}`);
        await expect(page.locator("main")).toBeVisible();
        // 等頁面載入完（載入骨架都消失；N1）：清單還在骨架時數連結與按鈕，會漏掉清單裡的那些。
        await expect(page.locator("[data-state='loading']")).toHaveCount(0);
        responses.length = 0;
      };
      await open();
      const listRows = site.name === "member" ? LIST_ROWS[path] : undefined;
      if (listRows !== undefined) {
        // 清單真的列出來了（列數等於從假資料算的），才往下數。
        expect(listRows.count).toBeGreaterThan(0);
        await expect(page.locator(`main ${listRows.selector}`)).toHaveCount(listRows.count);
      }
      const demoRows = site.name === "demo" ? DEMO_LIST_ROWS[path] : undefined;
      if (demoRows !== undefined) {
        // 示範站清單也一樣：真的列出來了（列數從範例匯出算）才往下數。
        expect(demoRows.count).toBeGreaterThan(DEMO_ROWS_CLICKED);
        await expect(page.locator(`main ${demoRows.selector}`)).toHaveCount(demoRows.count);
      }
      const linkCount = await page.locator("main a[href]").count();
      const linkIndexes = await indexesToClick(page, "main a[href]", demoRows && `main ${demoRows.selector}`);
      // 看得到的按鈕才點：同一個按鈕在另一種寬度才出現的（例如論文頁手機版底部的「開啟全文」，Story 3.8）
      // 用 CSS 藏起來，點不到；它在那一種寬度的行為由各自的測試看。
      const buttons = page.locator("main button:visible");
      const buttonIndexes = await indexesToClick(page, "main button:visible", demoRows && `main ${demoRows.selector}`);
      if (listRows !== undefined) {
        // 有清單的頁面一定有東西可點（N1：什麼都沒點就結束要失敗）；每一列的標題都是連結。
        expect(linkCount).toBeGreaterThanOrEqual(listRows.count);
      }

      for (const index of linkIndexes) {
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

      for (const index of buttonIndexes) {
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
