// 論文頁（Story 3.8）：整頁層級的狀態（載入中、錯誤、找不到或沒有權限、登入過期）、AI 文字的淨化、
// 「開啟全文」、兩欄與手機版的版面，以及 1440／1000／500 三種寬度的整頁截圖。
// 區塊層級的狀態（State Patterns 每一列）在 src/pages/PaperPage.test.tsx。資料是 fixtures/papers.json 的假論文。
import { fileURLToPath } from "node:url";

import { MEMBERS, gate, paperId, paperPage } from "./api-mock.ts";
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { SITES } from "./pages.ts";

const member = SITES[0]!;
const SCREENSHOT_DIR = fileURLToPath(new URL("./screenshots/", import.meta.url));
const url = (label: string) => `${member.baseURL}/papers/${paperId(label)}`;
const NOT_FOUND = TEXT["common:states.notFound.message"];
const LOAD_FAILED = TEXT["common:states.loadFailed.message"];
const LOAD_FAILED_TITLE = TEXT["common:states.loadFailed.title"];
const NAMES_FAILED = TEXT["paper:paper.names.loadFailed"];
const RETRY = TEXT["paper:paper.names.retry"];

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

// 整頁載入失敗、找不到這一頁：訊息就是 h1，分頁名稱是「載入失敗」／「找不到這一頁」（EXPERIENCE.md
// State Patterns「全站共用」；Codex 審查 3.8 中 8）。
test("a server error shows load failed as the page title", async ({ page }) => {
  await page.goto(url("server-error"));
  await expect(page.locator("h1")).toHaveCount(1);
  await expect(page.locator("main h1")).toHaveText(LOAD_FAILED);
  await expect(page).toHaveTitle(LOAD_FAILED_TITLE);
  await expect(page.locator("header")).toBeVisible();
});

// 顯示名稱沒讀到：論文照常顯示，只有分類那一行換成訊息條＋「重試」（State Patterns「錯誤」；Codex 審查 3.8 中 6）。
test("display names failing: the page shows, only the classification line has a message and retry", async ({
  page,
  apiMock,
}) => {
  const data = paperPage("normal");
  apiMock.displayNames = "failed";
  await page.goto(url("normal"));
  await expect(page.locator("h1")).toHaveText(data.paper.title);
  const failed = page.locator("[data-names='failed']");
  await expect(failed.getByText(NAMES_FAILED, { exact: true })).toBeVisible();
  await expect(page.locator("[data-block='classification']")).toHaveCount(0);
  await expect(page.locator("[data-block='zh_summary']")).toContainText(data.zh_summary.value!.research_question!);
  await expect(page.locator("[data-block='research_model'] [data-construct]").first()).toBeVisible();
  await expect(page.getByText(LOAD_FAILED, { exact: true })).toHaveCount(0);

  // 按「重試」：這次讀到了，分類那一行回來，訊息條消失；論文不重讀。
  apiMock.displayNames = "ok";
  const paperRequests = apiMock.requests.filter((request) => request.path.startsWith("/api/v1/papers/")).length;
  await failed.getByRole("button", { name: RETRY }).click();
  await expect(page.locator("[data-block='classification'] [data-tag]").first()).toBeVisible();
  await expect(page.locator("[data-names='failed']")).toHaveCount(0);
  expect(apiMock.requests.filter((request) => request.path.startsWith("/api/v1/papers/")).length).toBe(paperRequests);
});

// 兩個端點交錯回應（Codex 審查 3.8 中 3）：論文讀到、顯示名稱回 401，一樣是登入過期。
test("display names saying 401 while the paper loads goes back to the login page as expired", async ({
  page,
  apiMock,
}) => {
  apiMock.displayNames = "expired";
  await page.goto(url("normal"));
  await expect(page).toHaveURL(/\/login\?/);
  const target = new URL(page.url());
  expect(target.searchParams.get("expired")).toBe("true");
  expect(target.searchParams.get("next")).toBe(`/papers/${paperId("normal")}`);
});

test("retry getting 401 goes back to the login page as expired", async ({ page, apiMock }) => {
  apiMock.displayNames = "failed";
  await page.goto(url("normal"));
  await expect(page.getByText(NAMES_FAILED, { exact: true })).toBeVisible();
  apiMock.displayNames = "expired";
  await page.getByRole("button", { name: RETRY }).click();
  await expect(page).toHaveURL(/\/login\?/);
  const target = new URL(page.url());
  expect(target.searchParams.get("expired")).toBe("true");
  expect(target.searchParams.get("next")).toBe(`/papers/${paperId("normal")}`);
  await expect(page.locator("header")).toHaveCount(0);
});

test("not found or no permission: the shared not found under the navigation bar", async ({ page, apiMock }) => {
  await page.goto(url("not-found"));
  await expect(page.getByText(NOT_FOUND, { exact: true })).toBeVisible();
  await expect(page.locator("header")).toBeVisible();
  await expect(page.locator("h1")).toHaveCount(1);
  await expect(page.locator("main h1")).toHaveText(NOT_FOUND);
  await expect(page).toHaveTitle(NOT_FOUND);

  // ID 不是 UUID：不送請求，同一個畫面。
  apiMock.requests.length = 0;
  await page.goto(`${member.baseURL}/papers/not-a-uuid`);
  await expect(page.getByText(NOT_FOUND, { exact: true })).toBeVisible();
  expect(apiMock.requests.filter((request) => request.path.startsWith("/api/v1/papers/"))).toEqual([]);
});

// 同一個分頁換人登入（Codex 審查 3.8 中 4）：A 看過的論文頁，B 登入後回到同一篇（回應故意晚到），
// 從頭到尾都不會出現 A 才看得到的字（快取與路由記住的資料都已清掉）。不重新載入整頁，記憶體裡的東西還在。
test("A logs out and B logs in in the same tab: A's paper page never shows for B", async ({ page, apiMock }) => {
  const [first, second] = [MEMBERS[0]!, MEMBERS[1]!];
  const onlyA = "Paper title only member A can see";
  const forB = "Paper title member B sees";
  apiMock.paperByMember = {
    [first.member_id]: { ...paperPage("normal"), paper: { ...paperPage("normal").paper, title: onlyA } },
    [second.member_id]: { ...paperPage("normal"), paper: { ...paperPage("normal").paper, title: forB } },
  };
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(url("normal"));
  await expect(page.locator("h1")).toHaveText(onlyA);
  await page.evaluate(() => {
    (window as unknown as { __sameDocument?: boolean }).__sameDocument = true;
  });

  await page.getByRole("button", { name: TEXT["nav:nav.account.trigger"] }).click();
  await page.getByRole("menuitem", { name: TEXT["nav:nav.account.logout"] }).click();
  await expect(page).toHaveURL(`${member.baseURL}/login`);
  // 從這裡開始記下畫面上有沒有出現過 A 的字（每次畫面變動都檢查）。
  await page.evaluate((text) => {
    const seen: string[] = [];
    (window as unknown as { __seenA: string[] }).__seenA = seen;
    new MutationObserver(() => {
      if (document.body.textContent?.includes(text)) seen.push(location.pathname);
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
  }, onlyA);

  apiMock.loginList = [second];
  await page.getByRole("button", { name: TEXT["login:login.button.idle"] }).click();
  await expect(page).toHaveURL(`${member.baseURL}/notice`);
  // 回到同一篇（站內換頁，不重新載入整頁），論文的回應晚到。站內沒有連到論文頁的連結（3.14 才有），
  // 所以照瀏覽器「上一頁／下一頁」的做法：改網址後送 popstate，路由照網址換頁。
  const [paperGate, release] = gate();
  apiMock.paperGate = paperGate;
  await page.evaluate((path) => {
    window.history.pushState(window.history.state, "", path);
    window.dispatchEvent(new PopStateEvent("popstate", { state: window.history.state }));
  }, `/papers/${paperId("normal")}`);
  await expect(page).toHaveURL(url("normal"));
  await expect(page.locator("[data-state='loading']")).toBeVisible();
  await expect(page.getByText(onlyA)).toHaveCount(0);
  release();
  await expect(page.locator("h1")).toHaveText(forB);
  await expect(page.getByText(onlyA)).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __seenA: string[] }).__seenA)).toEqual([]);
  expect(await page.evaluate(() => (window as unknown as { __sameDocument?: boolean }).__sameDocument)).toBe(true);
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

// 手機版底部固定列不會蓋住鍵盤焦點（WCAG 2.4.11；Opus 畫面審查中 2）：用 Tab 走過每一個可以聚焦的元素，
// 焦點在固定列外面的，都要整個在固定列上面。390×844 與 720×450（1440 寬放大 200%）各走一次。
for (const size of [
  { width: 390, height: 844 },
  { width: 720, height: 450 },
]) {
  test(`phone width ${size.width}×${size.height}: the bottom bar never hides the keyboard focus`, async ({
    page,
    browserName,
  }) => {
    await page.setViewportSize(size);
    await page.goto(url("normal"));
    await expect(page.locator("h1")).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    const key = browserName === "webkit" ? "Alt+Tab" : "Tab";
    const hidden: string[] = [];
    let visited = 0;
    for (let presses = 0; presses < 60; presses += 1) {
      await page.keyboard.press(key);
      const focus = await page.evaluate(() => {
        const element = document.activeElement as HTMLElement | null;
        if (element === null || element === document.body) return null;
        const bar = document.querySelector<HTMLElement>("[data-full-text='bar']");
        if (bar === null || bar.contains(element)) return { inBar: true, covered: false, label: "" };
        const rect = element.getBoundingClientRect();
        const top = bar.getBoundingClientRect().top;
        return { inBar: false, covered: rect.bottom > top + 0.5, label: `${element.outerHTML.slice(0, 60)} ${rect.bottom}>${top}` };
      });
      if (focus === null) break;
      if (focus.inBar) break; // 走到固定列裡的「開啟全文」：前面的都看過了。
      visited += 1;
      if (focus.covered) hidden.push(focus.label);
    }
    expect(visited).toBeGreaterThan(3);
    expect(hidden).toEqual([]);
  });
}

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
