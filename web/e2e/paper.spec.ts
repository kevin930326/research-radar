// 論文頁（Story 3.8）：整頁層級的狀態（載入中、錯誤、找不到或沒有權限、登入過期）、AI 文字的淨化、
// 「開啟全文」、兩欄與手機版的版面，以及 1440／1000／500 三種寬度的整頁截圖。
// 區塊層級的狀態（State Patterns 每一列）在 src/pages/PaperPage.test.tsx。資料是 fixtures/papers.json 的假論文。
import { fileURLToPath } from "node:url";

import { gate, paperId, paperPage } from "./api-mock.ts";
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { SITES } from "./pages.ts";

const member = SITES[0]!;
const SCREENSHOT_DIR = fileURLToPath(new URL("./screenshots/", import.meta.url));
const url = (label: string) => `${member.baseURL}/papers/${paperId(label)}`;
const NOT_FOUND = TEXT["common:states.notFound.message"];
const LOAD_FAILED = TEXT["common:states.loadFailed.message"];

test("shows the paper with its blocks in order", async ({ page }) => {
  const data = paperPage("normal");
  await page.goto(url("normal"));
  await expect(page.locator("h1")).toHaveText(data.paper.title);
  await expect(page.locator("header")).toBeVisible();
  const blocks = page.locator("main [data-block]");
  expect(await blocks.evaluateAll((elements) => elements.map((element) => element.getAttribute("data-block")))).toEqual([
    "biblio",
    "classification",
    "zh_summary",
    "research_model",
    "abstract",
  ]);
  await expect(page.locator("[data-data-date]")).toContainText("2026-11-12");
});

test("AI text with tags, links and scripts is not clickable or executable", async ({ page }) => {
  const dialogs: string[] = [];
  page.on("dialog", (dialog) => {
    dialogs.push(dialog.message());
    void dialog.dismiss();
  });
  await page.goto(url("xss"));
  await expect(page.locator("h1")).toBeVisible();
  const aiText = page.locator("[data-ai-text]");
  expect(await aiText.count()).toBeGreaterThan(5);
  await expect(aiText.locator("a, img, script, iframe, style, svg, [onclick], [onerror], [href], [src]")).toHaveCount(0);
  await expect(page.locator("[data-summary-field='method'] [data-ai-text]")).not.toContainText("javascript");
  // 頁面上沒有任何一段被執行（攻擊字串都想設 window.__xss）。
  expect(await page.evaluate(() => (window as unknown as { __xss?: number }).__xss)).toBeUndefined();
  expect(dialogs).toEqual([]);
});

test("open full text opens the DOI link in a new tab", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(url("normal"));
  const doi = paperPage("normal").full_text_links.doi_url!;
  const request = page.context().waitForEvent("request", (candidate) => candidate.url() === doi);
  const popup = page.context().waitForEvent("page");
  await page.locator("[data-full-text='open']").click();
  await request;
  await popup;
  // 原本這一頁不動。
  await expect(page).toHaveURL(url("normal"));
});

test("both open full text buttons open the DOI without the opener or referrer", async ({ page }) => {
  // window.open 換成記錄參數（不真的開新分頁），看兩顆按鈕各自帶了什麼。
  await page.addInitScript(() => {
    const calls: unknown[][] = [];
    (window as unknown as { __openCalls: unknown[][] }).__openCalls = calls;
    window.open = (...args: unknown[]) => {
      calls.push(args);
      return null;
    };
  });
  const doi = paperPage("normal").full_text_links.doi_url!;
  const calls = () => page.evaluate(() => (window as unknown as { __openCalls: unknown[][] }).__openCalls);

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(url("normal"));
  await page.locator("[data-full-text='open']").click();
  expect(await calls()).toEqual([[doi, "_blank", "noopener,noreferrer"]]);

  await page.setViewportSize({ width: 500, height: 900 });
  await page.goto(url("normal"));
  await page.locator("[data-full-text='open-mobile']").click();
  expect(await calls()).toEqual([[doi, "_blank", "noopener,noreferrer"]]);
  await expect(page).toHaveURL(url("normal"));
});

test("loading shows the skeleton under the navigation bar", async ({ page, apiMock }) => {
  const [paperGate, release] = gate();
  apiMock.paperGate = paperGate;
  await page.goto(url("normal"));
  await expect(page.locator("[data-state='loading']")).toBeVisible();
  await expect(page.locator("header")).toBeVisible();
  release();
  await expect(page.locator("h1")).toHaveText(paperPage("normal").paper.title);
  await expect(page.locator("[data-state='loading']")).toHaveCount(0);
});

test("a server error shows load failed", async ({ page }) => {
  await page.goto(url("server-error"));
  await expect(page.getByText(LOAD_FAILED, { exact: true })).toBeVisible();
  await expect(page.locator("h1")).toHaveCount(0);
});

test("display names failing also shows load failed", async ({ page, apiMock }) => {
  apiMock.displayNamesFail = true;
  await page.goto(url("normal"));
  await expect(page.getByText(LOAD_FAILED, { exact: true })).toBeVisible();
});

test("not found or no permission: the shared not found under the navigation bar", async ({ page, apiMock }) => {
  await page.goto(url("not-found"));
  await expect(page.getByText(NOT_FOUND, { exact: true })).toBeVisible();
  await expect(page.locator("header")).toBeVisible();

  // ID 不是 UUID：不送請求，同一個畫面。
  apiMock.requests.length = 0;
  await page.goto(`${member.baseURL}/papers/not-a-uuid`);
  await expect(page.getByText(NOT_FOUND, { exact: true })).toBeVisible();
  expect(apiMock.requests.filter((request) => request.path.startsWith("/api/v1/papers/"))).toEqual([]);
});

test("the paper API saying 401 goes back to the login page as expired", async ({ page }) => {
  await page.goto(url("expired"));
  await expect(page).toHaveURL(/\/login\?/);
  const target = new URL(page.url());
  expect(target.searchParams.get("expired")).toBe("true");
  expect(target.searchParams.get("next")).toBe(`/papers/${paperId("expired")}`);
});

for (const width of [1440, 1000]) {
  test(`two columns at ${width}px: the rail is on the right and stays while scrolling`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(url("normal"));
    const main = await page.locator("[data-block='biblio']").boundingBox();
    const rail = page.locator("aside");
    const box = await rail.boundingBox();
    expect(main).not.toBeNull();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThan(main!.x + main!.width);
    expect(main!.width).toBeLessThanOrEqual(760);
    // 輔助欄內容寬 248、左邊內距 32（設計稿）。
    expect(box!.width).toBeCloseTo(248 + 32 + 1, 0);
    await expect(page.locator("[data-full-text='mobile']")).toBeHidden();
    await expect(page.locator("[data-full-text='bar']")).toBeHidden();
    await page.mouse.wheel(0, 1200);
    await expect.poll(async () => (await rail.boundingBox())?.y).toBeCloseTo(32, 0);
    const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
    expect(scrollWidth).toBeLessThanOrEqual(width);
  });
}

test("phone width: full text under the classification, open full text in a bar at the bottom", async ({ page }) => {
  await page.setViewportSize({ width: 500, height: 900 });
  await page.goto(url("normal"));
  await expect(page.locator("aside")).toBeHidden();
  await expect(page.locator("[data-full-text='mobile']")).toBeVisible();
  const bar = await page.locator("[data-full-text='bar']").boundingBox();
  expect(bar!.y + bar!.height).toBeCloseTo(900, 0);
  expect(bar!.width).toBeCloseTo(500, 0);
  const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(scrollWidth).toBeLessThanOrEqual(500);

  // 沒有 DOI：沒有底部固定列，一行「這篇沒有 DOI，沒有全文連結」。
  await page.goto(url("no-doi"));
  await expect(page.locator("[data-full-text='bar']")).toHaveCount(0);
  await expect(page.locator("[data-full-text='mobile']")).toContainText(TEXT["paper:paper.fullText.noDoi"]);
});

test("the AI badge popover shows the full wording, model and date", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(url("normal"));
  await page.locator("[data-block='zh_summary'] button:has([data-ai-badge])").click();
  const popup = page.locator("[data-info-popover]");
  await expect(popup).toContainText(TEXT["ai:ai.badge.full"]);
  await expect(popup.locator("[data-provenance='date']")).toHaveText("2026-11-10");
});

// 設計稿的四個狀態：正常、沒有摘要、只有中文重點沒成功、三個都沒成功。
for (const label of ["normal", "no-abstract", "zh-failed", "all-failed"]) {
  for (const width of [1440, 1000, 500]) {
    test(`paper ${label} screenshot at ${width}px`, async ({ page, browserName }) => {
      test.skip(browserName !== "chromium", "screenshots are taken in chromium only");
      await page.setViewportSize({ width, height: 900 });
      await page.goto(url(label));
      await expect(page.locator("h1")).toBeVisible();
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: `${SCREENSHOT_DIR}paper-${label}-${width}.png`, fullPage: true });
    });
  }
}
