// 找論文（Story 3.14，成員版）：EXPERIENCE.md 畫面 12 與 State Patterns「12. 找論文」的每一種狀態、
// 篩選條件寫進網址、「再載入」從上一頁接著讀、自訂年份的檢查、手機的篩選，以及三種寬度的整頁截圖。
// 資料是 fixtures/paper-list.json 的假清單（自己編的），預期的篇數用替身同一套篩選規則算，不另寫一份數字。
import { fileURLToPath } from "node:url";

import type { Page } from "@playwright/test";

import { LIST_FIXTURES, PAPER_FIXTURES, filterListRows, gate, paperId } from "./api-mock.ts";
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { SITES } from "./pages.ts";

const member = SITES[0]!;
const SCREENSHOT_DIR = fileURLToPath(new URL("./screenshots/", import.meta.url));
const DESKTOP = { width: 1280, height: 900 };
const PHONE = { width: 390, height: 844 };
const PAGE_SIZE = LIST_FIXTURES.clientConfig.paper_list_page_size;
const AIS_CORE = ["misq", "isr", "jmis", "jais", "ejis", "isj", "jit", "jsis", "dss", "im", "io"];

/** 主題的顯示名稱（假資料的 display-names）。 */
const ECOMMERCE = PAPER_FIXTURES.displayNames.topics.find((topic) => topic.key === "e_commerce")!.name;

/** 固定文字（expected 的 JSON）；沒有這個鍵就是測試寫錯。 */
function text(key: string): string {
  const value = TEXT[key];
  if (value === undefined) throw new Error(`no expected text ${key}`);
  return value;
}

function fill(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{\{(\w+)\}\}/g, (_, name: string) => String(values[name]));
}

/** 替身依這組網址參數會回幾篇（同 api-mock.ts 的篩選）。 */
function expectedTotal(query: string): number {
  return filterListRows(LIST_FIXTURES.rows, new URLSearchParams(query))!.length;
}

async function openSearch(page: Page, query = "", viewport = DESKTOP): Promise<void> {
  await page.setViewportSize(viewport);
  await page.goto(`${member.baseURL}/search${query}`);
  await expect(page.locator("h1")).toHaveText(text("search:search.page.title"));
}

function listRequests(requests: { path: string }[]): URLSearchParams[] {
  return requests
    .filter((request) => request.path.startsWith("/api/v1/papers?") || request.path === "/api/v1/papers")
    .map((request) => new URL(request.path, member.baseURL).searchParams);
}

const rows = (page: Page) => page.locator("[data-paper-row]");
const chip = (page: Page, key: string) => page.locator("[data-filter-chip]").filter({ hasText: text(key) });

test.describe("search: states", () => {
  test("no conditions shows this week's first three papers and a link to all of them", async ({ page }) => {
    await openSearch(page);
    const start = page.locator("[data-state='start']");
    await expect(start.getByRole("heading", { level: 2 })).toHaveText(text("search:search.condition.thisWeek"));
    await expect(rows(page)).toHaveCount(3);
    const thisWeek = expectedTotal("this_week=true");
    const seeAll = start.getByRole("link", { name: fill(text("search:search.start.seeAll"), { count: thisWeek }) });
    await seeAll.click();
    await expect(page).toHaveURL(`${member.baseURL}/search?this_week=true`);
    // 帶進來的條件：一個可以拿掉的標籤，結果是全部本週新增。
    await expect(page.locator("[data-result-count]")).toContainText(String(thisWeek));
    await expect(rows(page)).toHaveCount(thisWeek);
    const tag = page.locator("[data-condition='this_week']:visible");
    await expect(tag).toContainText(text("search:search.condition.thisWeek"));
    await tag.getByRole("button", { name: text("search:search.filter.removeCondition") }).click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    await expect(page.locator("[data-state='start']")).toBeVisible();
  });

  test("results load more from the next page without reading the first page again", async ({ page, apiMock }) => {
    const query = "from=2016&to=2026";
    const total = expectedTotal(`year=${Array.from({ length: 11 }, (_, index) => 2016 + index).join("&year=")}`);
    expect(total).toBeGreaterThan(PAGE_SIZE);
    await openSearch(page, `?${query}`);
    await expect(page.locator("[data-result-count]")).toContainText(String(total));
    await expect(rows(page)).toHaveCount(PAGE_SIZE);
    await expect(page.locator("[data-shown]")).toHaveText(
      fill(text("search:search.results.shown"), { shown: PAGE_SIZE, total }),
    );
    apiMock.requests.length = 0;
    await page.getByRole("button", { name: fill(text("search:search.results.more"), { count: PAGE_SIZE }) }).click();
    await expect(rows(page)).toHaveCount(total);
    // 網址記下顯示幾篇（AD-21），cursor 不進網址。
    await expect(page).toHaveURL(new RegExp(`shown=${PAGE_SIZE * 2}`));
    expect(page.url()).not.toContain("cursor");
    const sent = listRequests(apiMock.requests);
    expect(sent).toHaveLength(1);
    expect(sent[0]!.get("cursor")).toBe(`o${PAGE_SIZE}`);
    // 全部都顯示了：沒有「再載入」。
    await expect(page.locator("[data-load-more]")).toHaveCount(0);
    // 重新整理：同樣顯示這麼多篇。
    await page.reload();
    await expect(rows(page)).toHaveCount(total);
  });

  test("no match says so and clearing goes back to the start", async ({ page }) => {
    await openSearch(page, "?journal=misq&from=2016&to=2016");
    const zero = page.locator("[data-state='zero']");
    await expect(zero).toContainText(text("search:search.empty.noMatch"));
    await expect(rows(page)).toHaveCount(0);
    await zero.getByRole("button", { name: text("search:search.filter.clearAll") }).click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    await expect(page.locator("[data-state='start']")).toBeVisible();
  });

  test("loading shows the skeleton until the list arrives", async ({ page, apiMock }) => {
    const [listGate, release] = gate();
    apiMock.listGate = listGate;
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?topic=e_commerce`);
    const loading = page.locator("[data-state='loading']");
    await expect(loading.getByRole("status")).toHaveAccessibleName(text("search:search.results.loading"));
    await expect(rows(page)).toHaveCount(0);
    release();
    await expect(loading).toHaveCount(0);
    await expect(rows(page)).toHaveCount(expectedTotal("topic=e_commerce"));
  });

  test("an error keeps the conditions and reloads with the button", async ({ page, apiMock }) => {
    apiMock.list = "failed";
    await openSearch(page, "?topic=e_commerce");
    const error = page.getByRole("alert").filter({ hasText: text("common:states.loadFailed.message") });
    await expect(error).toBeVisible();
    await expect(chip(page, "search:search.filter.topic")).toContainText("·");
    apiMock.list = "ok";
    await error.getByRole("button", { name: text("common:states.loadFailed.action") }).click();
    await expect(rows(page)).toHaveCount(expectedTotal("topic=e_commerce"));
    await expect(page).toHaveURL(`${member.baseURL}/search?topic=e_commerce`);
  });

  test("an expired login goes to the login page and comes back here", async ({ page, apiMock }) => {
    apiMock.list = "expired";
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?topic=e_commerce`);
    await expect(page).toHaveURL(/\/login\?/);
    const url = new URL(page.url());
    expect(url.searchParams.get("next")).toBe("/search?topic=e_commerce");
    expect(url.searchParams.get("expired")).toBe("true");
  });
});

test.describe("search: filters", () => {
  test("conditions in the address are sent to the API and shown on the chips", async ({ page, apiMock }) => {
    await openSearch(page, "?journal=isr&from=2020&to=2026&topic=e_commerce,unclassified&scope=lab");
    const sent = listRequests(apiMock.requests).at(-1)!;
    expect(sent.getAll("journal")).toEqual(["isr"]);
    expect(sent.getAll("year")).toEqual(["2020", "2021", "2022", "2023", "2024", "2025", "2026"]);
    expect(sent.getAll("topic")).toEqual(["e_commerce", "unclassified"]);
    expect(sent.get("scope")).toBe("lab");
    await expect(chip(page, "search:search.filter.journal")).toContainText("Information Systems Research");
    await expect(chip(page, "search:search.filter.year")).toContainText("2020–2026");
    await expect(chip(page, "search:search.filter.topic")).toContainText(fill(text("search:search.filter.topicCount"), { count: 2 }));
    await expect(page.getByRole("button", { name: text("search:search.scope.lab") })).toHaveAttribute("aria-pressed", "true");
  });

  test("picking a topic writes it to the address; clear removes everything but the scope", async ({ page }) => {
    await openSearch(page, "?scope=personal");
    await chip(page, "search:search.filter.topic").click();
    // 勾了立刻套用：勾選狀態跟著網址變，所以點了之後等它變成勾選（check() 會在網址還沒換時就檢查）。
    await page.getByRole("checkbox", { name: ECOMMERCE }).click();
    await expect(page.getByRole("checkbox", { name: ECOMMERCE })).toBeChecked();
    await expect(page).toHaveURL(`${member.baseURL}/search?scope=personal&topic=e_commerce`);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: text("search:search.filter.clearAll") }).first().click();
    await expect(page).toHaveURL(`${member.baseURL}/search?scope=personal`);
  });

  test("AIS core picks the eleven journals", async ({ page }) => {
    await openSearch(page, "?topic=e_commerce");
    await chip(page, "search:search.filter.journal").click();
    await page.getByRole("button", { name: text("search:search.filter.aisCore") }).click();
    await expect(page).toHaveURL((url) => url.searchParams.get("journal") === AIS_CORE.join(","));
    await expect(chip(page, "search:search.filter.journal")).toContainText(fill(text("search:search.filter.journalCount"), { count: 11 }));
  });

  test("a custom year range is checked and not applied when it is wrong", async ({ page }) => {
    await openSearch(page, "?topic=e_commerce");
    await chip(page, "search:search.filter.year").click();
    await page.getByRole("radio", { name: text("search:search.year.custom") }).check();
    const from = page.getByRole("textbox", { name: text("search:search.year.fromLabel") });
    const to = page.getByRole("textbox", { name: text("search:search.year.toLabel") });
    const cases: [string, string, string][] = [
      ["2010", "2020", fill(text("search:search.year.tooEarly"), { year: 2016 })],
      ["2020", "2099", text("search:search.year.tooLate").split("{{")[0]!],
      ["2022", "2019", text("search:search.year.reversed")],
      ["", "2020", text("search:search.year.missing")],
    ];
    // 離開輸入框也會檢查（設計稿）：第一次焦點在起始年（按「自訂」時移過去的），先填它；
    // 之後先填結束年、再填起始年。中途經過的組合都不合，網址不會變。
    for (const [index, [fromYear, toYear, message]] of cases.entries()) {
      const [first, firstYear, second, secondYear] =
        index === 0 ? [from, fromYear, to, toYear] : [to, toYear, from, fromYear];
      await first.fill(firstYear);
      await second.fill(secondYear);
      await second.press("Enter");
      await expect(page.getByRole("alert").filter({ hasText: message })).toBeVisible();
      await expect(page).toHaveURL(`${member.baseURL}/search?topic=e_commerce`);
    }
    await to.fill("2021");
    await from.fill("2018");
    await from.press("Enter");
    await expect(page).toHaveURL(`${member.baseURL}/search?topic=e_commerce&from=2018&to=2021`);
    await expect(page.getByRole("alert").filter({ hasText: /\S/ })).toHaveCount(0);
  });

  test("phone: the filter sheet applies right away and closes with the count", async ({ page }) => {
    await openSearch(page, "", PHONE);
    await page.locator("[data-mobile-filter]").click();
    const sheet = page.getByRole("dialog");
    await expect(sheet).toBeVisible();
    await sheet.locator("[data-filter-section='topic'] > button").click();
    await sheet.getByRole("checkbox", { name: ECOMMERCE }).click();
    await expect(sheet.getByRole("checkbox", { name: ECOMMERCE })).toBeChecked();
    await expect(page).toHaveURL(`${member.baseURL}/search?topic=e_commerce`);
    const total = expectedTotal("topic=e_commerce");
    await sheet.getByRole("button", { name: fill(text("search:search.mobile.apply"), { count: total }) }).click();
    await expect(sheet).toHaveCount(0);
    await expect(page.locator("[data-mobile-filter]")).toHaveText(fill(text("search:search.mobile.filterCount"), { count: 1 }));
    await expect(rows(page)).toHaveCount(total);
  });
});

test.describe("search: entry points", () => {
  test("the nav link and the brand go to search; the link is marked as the current page", async ({ page }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/notice`);
    const nav = page.locator("header").getByRole("link", { name: text("nav:nav.links.search") });
    await nav.click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    await expect(nav).toHaveAttribute("aria-current", "page");
    await page.goto(`${member.baseURL}/notice`);
    await page.locator("header").getByRole("link").first().click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
  });

  test("a topic tag on the paper page opens search with that topic", async ({ page, apiMock }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/papers/${paperId("normal")}`);
    const tag = page.locator("[data-topic-link]").first();
    await expect(tag).not.toHaveAttribute("target", "_blank");
    await tag.click();
    await expect(page).toHaveURL(`${member.baseURL}/search?topic=platforms`);
    await expect(page.locator("h1")).toHaveText(text("search:search.page.title"));
    expect(listRequests(apiMock.requests).at(-1)!.getAll("topic")).toEqual(["platforms"]);
  });
});

for (const width of [1440, 1000, 500]) {
  test(`search screenshot at ${width}px`, async ({ page, browserName }) => {
    test.skip(browserName !== "chromium", "screenshots are taken in chromium only");
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${member.baseURL}/search?topic=e_commerce`);
    await expect(rows(page).first()).toBeVisible();
    // 沒有橫向捲動。
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `${SCREENSHOT_DIR}search-${width}.png`, fullPage: true });
  });
}
