// 找論文（Story 3.14，成員版）：EXPERIENCE.md 畫面 12 與 State Patterns「12. 找論文」的每一種狀態、
// 篩選條件寫進網址、「再載入」從上一頁接著讀、自訂年份的檢查、手機的篩選，以及三種寬度的整頁截圖。
// 資料是 fixtures/paper-list.json 的假清單（自己編的），預期的篇數用替身同一套篩選規則算，不另寫一份數字。
import { fileURLToPath } from "node:url";

import type { Page } from "@playwright/test";

import { LIST_FIXTURES, PAPER_FIXTURES, filterListRows, gate, paperId, type ListFixtureRow } from "./api-mock.ts";
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
/** 起始狀態列幾篇本週新增（設計稿 3 篇）。 */
const START_ROWS = 3;

/** 替身最後收到的清單請求：畫面先出來、請求可能還沒到，所以等到有為止（審查 A-1：不穩定的原因）。 */
async function lastListRequest(requests: { path: string }[]): Promise<URLSearchParams> {
  await expect.poll(() => listRequests(requests).length).toBeGreaterThan(0);
  return listRequests(requests).at(-1)!;
}

/** 選項（整列是 label，點在 label 上；真正的輸入框比整列往內縮，上面蓋著選項的字）。 */
const option = (page: Page, role: "radio" | "checkbox", name: string) =>
  page.locator("label").filter({ has: page.getByRole(role, { name }) });

/** 今年（台北時間；年份選項與網址驗證用同一個時區）。 */
function taipeiYear(): string {
  return new Intl.DateTimeFormat("en", { timeZone: "Asia/Taipei", year: "numeric" }).format(new Date());
}

/** 「找到 N 篇」整句。 */
function foundText(total: number): string {
  return fill(text("search:search.results.found"), { count: total });
}

/** 目前焦點在哪裡（tag 與 data 屬性），清除之後不能掉到 body（審查 S2-3）。 */
async function focused(page: Page): Promise<string> {
  return page.evaluate(() => {
    const element = document.activeElement;
    if (element === null || element === document.body) return "body";
    const marks = ["data-filter-chip", "data-mobile-filter"].filter((name) => element.hasAttribute(name));
    return [element.tagName.toLowerCase(), ...marks].join(" ");
  });
}

test.describe("search: states", () => {
  test("no conditions shows this week's first three papers and a link to all of them", async ({ page }) => {
    // 假資料的本週新增多於 3 篇，才測得出「只列前 3 篇」（審查 A-4）。
    const thisWeek = expectedTotal("this_week=true");
    expect(thisWeek).toBe(5);
    await openSearch(page);
    const start = page.locator("[data-state='start']");
    await expect(start.getByRole("heading", { level: 2 })).toHaveText(text("search:search.condition.thisWeek"));
    await expect(rows(page)).toHaveCount(START_ROWS);
    const seeAll = start.getByRole("link", { name: fill(text("search:search.start.seeAll"), { count: thisWeek }) });
    await seeAll.click();
    await expect(page).toHaveURL(`${member.baseURL}/search?this_week=true`);
    // 帶進來的條件：一個可以拿掉的標籤，結果是全部本週新增。
    await expect(page.locator("[data-result-count]")).toHaveText(foundText(thisWeek));
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
    await expect(page.locator("[data-result-count]")).toHaveText(foundText(total));
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

  test("library count fails: the shared error bar, and reload reads the count again", async ({ page, apiMock }) => {
    // F11：範圍我的文獻庫、沒條件、0 篇時多讀的文獻庫篇數失敗，和清單失敗一樣是錯誤條＋「重新整理」（審查）。
    apiMock.listRows = LIST_FIXTURES.rows.filter((entry) => !entry.personal);
    apiMock.library = "failed";
    await openSearch(page, "?scope=personal");
    const alert = page.locator("[data-state='error']");
    await expect(alert).toContainText(text("common:states.loadFailed.message"));
    apiMock.library = "ok";
    await alert.getByRole("button", { name: text("common:states.loadFailed.action") }).click();
    await expect(page.locator("[data-state='zero']")).toContainText(text("search:search.empty.noMatch"));
  });

  test("library count says the login expired: back to the login page", async ({ page, apiMock }) => {
    apiMock.listRows = LIST_FIXTURES.rows.filter((entry) => !entry.personal);
    apiMock.library = "expired";
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(page).toHaveURL(/\/login\?/);
    const url = new URL(page.url());
    expect(url.searchParams.get("next")).toBe("/search?scope=personal");
    expect(url.searchParams.get("expired")).toBe("true");
  });

  test("reloading at shown=220 (200 read at most on a fresh open): the address lines up and load more still works", async ({ page, apiMock }) => {
    // 同我的文獻庫的審查 G8：直接打開網址時最多讀 200 篇（MAX_SHOWN）。shown＝200＋一頁（20）時，「再載入」算出來的
    // 又是 220，和網址一樣，按了不會有反應；所以讀完之後網址對齊成 200。測試資料：假清單 30 篇＋這裡編的 270 篇，
    // 條件是「本週新增」（編的都是本週新增）。
    const extra: ListFixtureRow[] = Array.from({ length: 270 }, (_, index) => ({
      row: {
        ...LIST_FIXTURES.rows[0]!.row,
        id: `38000000-0000-4000-8000-1${String(index).padStart(11, "0")}`,
        title: `Fake Search Paper ${index + 1}`,
      },
      topics: [],
      this_week: true,
      lab: false,
      personal: false,
    }));
    apiMock.listRows = [...LIST_FIXTURES.rows, ...extra];
    const expected = filterListRows(apiMock.listRows, new URLSearchParams("this_week=true"))!.map((entry) => entry.row.id);
    expect(expected.length).toBeGreaterThan(200 + PAGE_SIZE);
    const shownIds = () => rows(page).evaluateAll((elements) => elements.map((element) => element.getAttribute("data-paper-row")));
    const more = page.locator("[data-load-more]");

    await openSearch(page, `?this_week=true&shown=${200 + PAGE_SIZE}`);
    await expect(rows(page)).toHaveCount(200);
    await expect(page).toHaveURL(/[?&]shown=200(&|$)/);
    expect(await shownIds()).toEqual(expected.slice(0, 200));
    await expect(page.locator("[data-shown]")).toHaveText(
      fill(text("search:search.results.shown"), { shown: 200, total: expected.length }),
    );
    await expect(more).toHaveText(fill(text("search:search.results.more"), { count: PAGE_SIZE }));

    await more.click();
    await expect(rows(page)).toHaveCount(200 + PAGE_SIZE);
    expect(await shownIds()).toEqual(expected.slice(0, 200 + PAGE_SIZE));
    await expect(page).toHaveURL(new RegExp(`[?&]shown=${200 + PAGE_SIZE}(&|$)`));
    // 對齊時不重讀：一開始 10 頁（200 篇），再載入只讀第 11 頁。
    const cursors = listRequests(apiMock.requests).map((params) => params.get("cursor"));
    expect(cursors).toEqual([null, ...Array.from({ length: 10 }, (_, index) => `o${(index + 1) * PAGE_SIZE}`)]);
  });

  test("no match says so and clearing goes back to the start", async ({ page }) => {
    await openSearch(page, "?journal=misq&from=2016&to=2016");
    const zero = page.locator("[data-state='zero']");
    await expect(zero).toContainText(text("search:search.empty.noMatch"));
    await expect(rows(page)).toHaveCount(0);
    await zero.getByRole("link", { name: text("search:search.filter.clearAll") }).click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    await expect(page.locator("[data-state='start']")).toBeVisible();
    // 按下去的連結不見了，焦點在第一個篩選標籤（審查 S2-3）。
    expect(await focused(page)).toBe("button data-filter-chip");
  });

  test("an empty library without conditions has nothing to clear", async ({ page, apiMock }) => {
    // 審查 S1-8：沒有條件時「清除篩選」按了沒反應，所以不放。Story 3.17：文獻庫是空的時，訊息條是
    // 「你的文獻庫還沒有論文」＋「改找所有追蹤期刊」（State Patterns「12. 找論文」；「上傳或匯入」是 Epic 6）。
    // F11（Kevin 2026-11-20 准改準備方式）：文獻庫真的是空的（替身的文獻庫設成空），清單的假資料保持完整。
    apiMock.libraryIds = [];
    await openSearch(page, "?scope=personal");
    const empty = page.locator("[data-state='library-empty']");
    await expect(empty).toContainText(text("search:search.empty.library"));
    await expect(empty.getByRole("link")).toHaveCount(1);
    await expect(empty.getByRole("link")).toHaveText(text("search:search.empty.toTracked"));
    await expect(page.locator("[data-state='zero']")).toHaveCount(0);
    await expect(page.locator("[data-clear-all]")).toHaveCount(0);
  });

  test("a library whose papers are all outside search: no match, not the empty-library message, nothing to clear", async ({ page, apiMock }) => {
    // F11：文獻庫有論文（替身的文獻庫還是一開始的那幾篇），但那幾篇都不在找論文的清單裡（例如被排除），
    // 範圍選我的文獻庫、沒有條件時是 0 篇。這時文獻庫不是空的，照一般的「沒有符合的論文」。
    apiMock.listRows = LIST_FIXTURES.rows.filter((entry) => !entry.personal);
    const inLibrary = LIST_FIXTURES.rows.filter((entry) => entry.personal).length;
    expect(inLibrary).toBeGreaterThan(0);
    expect(apiMock.libraryIds).toHaveLength(inLibrary);
    await openSearch(page, "?scope=personal");
    const zero = page.locator("[data-state='zero']");
    await expect(zero).toContainText(text("search:search.empty.noMatch"));
    await expect(zero.getByRole("link")).toHaveCount(0);
    await expect(page.locator("[data-state='library-empty']")).toHaveCount(0);
    await expect(page.locator("[data-clear-all]")).toHaveCount(0);
    await expect(rows(page)).toHaveCount(0);
  });

  test("an empty library (the list itself complete): the library message, no zero bar, nothing to clear", async ({ page, apiMock }) => {
    // 審查 F6：文獻庫真的是空的（替身的文獻庫沒有論文），清單的假資料一列都不刪。
    apiMock.libraryIds = [];
    expect(apiMock.listRows).toEqual(LIST_FIXTURES.rows);
    await openSearch(page, "?scope=personal");
    const empty = page.locator("[data-state='library-empty']");
    await expect(empty).toContainText(text("search:search.empty.library"));
    await expect(empty.getByRole("link")).toHaveCount(1);
    await expect(empty.getByRole("link")).toHaveText(text("search:search.empty.toTracked"));
    await expect(page.locator("[data-state='zero']")).toHaveCount(0);
    await expect(page.locator("[data-clear-all]")).toHaveCount(0);
    await expect(rows(page)).toHaveCount(0);
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
    // 條件留著：主題標籤寫著選的主題（審查 A-L7）。
    await expect(chip(page, "search:search.filter.topic")).toHaveText(`${text("search:search.filter.topic")}·${ECOMMERCE}`);
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
    const sent = await lastListRequest(apiMock.requests);
    expect(sent.getAll("journal")).toEqual(["isr"]);
    expect(sent.getAll("year")).toEqual(["2020", "2021", "2022", "2023", "2024", "2025", "2026"]);
    expect(sent.getAll("topic")).toEqual(["e_commerce", "unclassified"]);
    expect(sent.get("scope")).toBe("lab");
    await expect(chip(page, "search:search.filter.journal")).toContainText("Information Systems Research");
    // 報讀名稱也有選了什麼，不只「期刊」（審查 S2-2）。
    await expect(chip(page, "search:search.filter.journal")).toHaveAccessibleName(/Information Systems Research/);
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
    await page.getByRole("link", { name: text("search:search.filter.clearAll") }).first().click();
    await expect(page).toHaveURL(`${member.baseURL}/search?scope=personal`);
    expect(await focused(page)).toBe("button data-filter-chip");
  });

  test("clear in a menu keeps the focus inside the menu", async ({ page }) => {
    await openSearch(page, "?topic=e_commerce");
    await chip(page, "search:search.filter.topic").click();
    await page.getByRole("link", { name: text("search:search.filter.clear"), exact: true }).click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    // 「清除」不見了，焦點在選單的第一個選項（審查 S2-3）。
    expect(await focused(page)).toBe("input");
    await expect(page.getByRole("checkbox", { name: ECOMMERCE })).not.toBeChecked();
  });

  test("two quick picks while the address is still changing both stay", async ({ page, apiMock }) => {
    // 審查 S1-4：每次換條件都要等「我是誰」，等的時候再勾一個，第一個不能被蓋掉。
    await openSearch(page, "?topic=e_commerce");
    await chip(page, "search:search.filter.journal").click();
    const [meGate, release] = gate();
    apiMock.meGate = meGate;
    await page.getByRole("checkbox", { name: "MIS Quarterly" }).click();
    await page.getByRole("checkbox", { name: "Information Systems Research" }).click();
    release();
    await expect(page).toHaveURL((url) => url.searchParams.get("journal") === "misq,isr");
    await expect(page.getByRole("checkbox", { name: "MIS Quarterly" })).toBeChecked();
    await expect(page.getByRole("checkbox", { name: "Information Systems Research" })).toBeChecked();
  });

  test("unknown keys in the address are left out instead of failing", async ({ page, apiMock }) => {
    // 審查 S1-2：後端對不認得的代號回 422；送出前拿掉，不會變成「沒有載入成功」。
    await openSearch(page, "?topic=not_a_topic&journal=misq,old_journal&method=gone");
    await expect(rows(page).first()).toBeVisible();
    await expect(page.getByRole("alert").filter({ hasText: text("common:states.loadFailed.message") })).toHaveCount(0);
    // 每一個清單請求都要對（確認審查 N3）：只看最後一個，先送過一次 422 再送對的也會通過。
    await lastListRequest(apiMock.requests);
    const sent = listRequests(apiMock.requests);
    expect(sent.map((query) => query.getAll("topic"))).toEqual(sent.map(() => []));
    expect(sent.map((query) => query.getAll("journal"))).toEqual(sent.map(() => ["misq"]));
    expect(sent.map((query) => query.getAll("method"))).toEqual(sent.map(() => []));
  });

  test("years outside the tracking range in the address are not sent", async ({ page, apiMock }) => {
    // 審查 S1-1：2016 以前、今年以後的年份後端回 422，整組不套用。
    await openSearch(page, "?topic=e_commerce&from=2010&to=2020");
    await expect(rows(page).first()).toBeVisible();
    await expect(page.getByRole("alert").filter({ hasText: text("common:states.loadFailed.message") })).toHaveCount(0);
    // 每一個清單請求都不帶年份（確認審查 N3）。
    await lastListRequest(apiMock.requests);
    const sent = listRequests(apiMock.requests);
    expect(sent.map((query) => query.getAll("year"))).toEqual(sent.map(() => []));
    expect(sent.map((query) => query.getAll("topic"))).toEqual(sent.map(() => ["e_commerce"]));
  });

  test("a method brought in from the paper page is a removable condition", async ({ page, apiMock }) => {
    // 審查 S2-1、EXPERIENCE.md 畫面 8 第 2 點：找論文沒有方法的篩選，方法是一個可以移除的條件標籤。
    const methodName = PAPER_FIXTURES.displayNames.methods.find((entry) => entry.key === "experiment")!.name;
    await openSearch(page, "?method=experiment");
    expect((await lastListRequest(apiMock.requests)).getAll("method")).toEqual(["experiment"]);
    const tag = page.locator("[data-condition='method']:visible");
    await expect(tag).toHaveText(
      fill(text("search:search.condition.named"), { label: text("paper:paper.classification.method"), value: methodName }),
    );
    await expect(rows(page)).toHaveCount(expectedTotal("method=experiment"));
    await tag.getByRole("button", { name: text("search:search.filter.removeCondition") }).click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    await expect(page.locator("[data-state='start']")).toBeVisible();
    expect(await focused(page)).toBe("button data-filter-chip");
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
    // 離開輸入框也會檢查（設計稿），焦點在兩個年份框之間移動時不檢查：每一組都填完才按 Enter。
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

  test("custom years: the boxes start from the applied range and leaving them unchanged applies nothing", async ({ page }) => {
    // 審查 H-M1：選「近 3 年」後按「自訂」，框裡是近 3 年；沒改字就離開，網址不變。
    await openSearch(page, "?topic=e_commerce");
    await chip(page, "search:search.filter.year").click();
    await option(page, "radio", text("search:search.year.last3")).click();
    await expect(page).toHaveURL((url) => url.searchParams.has("from"));
    const applied = page.url();
    const from = new URL(applied).searchParams.get("from")!;
    const to = new URL(applied).searchParams.get("to")!;
    await option(page, "radio", text("search:search.year.custom")).click();
    const fromBox = page.getByRole("textbox", { name: text("search:search.year.fromLabel") });
    const toBox = page.getByRole("textbox", { name: text("search:search.year.toLabel") });
    await expect(fromBox).toHaveValue(from);
    await expect(toBox).toHaveValue(to);
    await expect(fromBox).toBeFocused();
    await page.keyboard.press("Tab");
    await page.keyboard.press("Tab");
    await expect(page.getByRole("alert").filter({ hasText: /\S/ })).toHaveCount(0);
    expect(page.url()).toBe(applied);
  });

  test("custom years: moving between the two boxes does not check yet", async ({ page }) => {
    // 審查 S2-10：填完起始年按 Tab 到結束年時不檢查（原本會先報「起始年不能比結束年晚」）。
    await openSearch(page, "?topic=e_commerce&from=2018&to=2019");
    await chip(page, "search:search.filter.year").click();
    await expect(page.getByRole("radio", { name: text("search:search.year.custom") })).toBeChecked();
    const fromBox = page.getByRole("textbox", { name: text("search:search.year.fromLabel") });
    await expect(fromBox).toHaveValue("2018");
    await fromBox.fill("2021");
    await page.keyboard.press("Tab");
    await expect(page.getByRole("textbox", { name: text("search:search.year.toLabel") })).toBeFocused();
    await expect(page.getByRole("alert").filter({ hasText: /\S/ })).toHaveCount(0);
    await page.getByRole("textbox", { name: text("search:search.year.toLabel") }).fill("2023");
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(`${member.baseURL}/search?topic=e_commerce&from=2021&to=2023`);
  });

  test("custom years: the arrow keys leave the focus on the radio", async ({ page }) => {
    // 審查 S2-11：方向鍵選到「自訂」時焦點留在單選鈕，還能繼續用方向鍵。
    await openSearch(page, "?topic=e_commerce&from=2016&to=2016");
    await chip(page, "search:search.filter.year").click();
    const custom = page.getByRole("radio", { name: text("search:search.year.custom") });
    await expect(custom).toBeChecked();
    await custom.focus();
    await page.keyboard.press("ArrowUp");
    // 等「近 5 年」套用完（標籤上的年份換好），再用方向鍵回到「自訂」。
    await expect(chip(page, "search:search.filter.year")).not.toContainText("2016");
    await page.keyboard.press("ArrowDown");
    await expect(custom).toBeChecked();
    await expect(custom).toBeFocused();
  });

  test("phone: the filter sheet applies right away and closes with the count", async ({ page }) => {
    await openSearch(page, "", PHONE);
    await page.locator("[data-mobile-filter]").click();
    const sheet = page.getByRole("dialog");
    await expect(sheet).toBeVisible();
    // 打開時第一段「期刊」展開（設計稿），一次展開一段。
    await expect(sheet.locator("[data-filter-section='journal'] > button")).toHaveAttribute("aria-expanded", "true");
    await expect(sheet.locator("[data-filter-section='topic'] > button")).toHaveAttribute("aria-expanded", "false");
    await sheet.locator("[data-filter-section='topic'] > button").click();
    await sheet.getByRole("checkbox", { name: ECOMMERCE }).click();
    await expect(sheet.getByRole("checkbox", { name: ECOMMERCE })).toBeChecked();
    await expect(sheet.locator("[data-filter-section='journal'] > button")).toHaveAttribute("aria-expanded", "false");
    await expect(page).toHaveURL(`${member.baseURL}/search?topic=e_commerce`);
    const total = expectedTotal("topic=e_commerce");
    await sheet.getByRole("button", { name: fill(text("search:search.mobile.apply"), { count: total }) }).click();
    await expect(sheet).toHaveCount(0);
    await expect(page.locator("[data-mobile-filter]")).toHaveText(fill(text("search:search.mobile.filterCount"), { count: 1 }));
    await expect(rows(page)).toHaveCount(total);
  });

  test("phone: clearing in the sheet keeps the focus inside it", async ({ page }) => {
    await openSearch(page, "?topic=e_commerce", PHONE);
    await page.locator("[data-mobile-filter]").click();
    const sheet = page.getByRole("dialog");
    await sheet.getByRole("link", { name: text("search:search.filter.clearAll") }).click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    // 「清除篩選」不見了，焦點在第一段的標題（審查 S2-3）。
    await expect(sheet.locator("[data-filter-section='journal'] > button")).toBeFocused();
  });

  test("phone: clearing after a custom year range shows no limit and the default boxes again", async ({ page }) => {
    // 確認審查 N1：手機篩選開著時年份那一段不會重建；網址的年份拿掉了，單選鈕與兩個框也要跟著回到預設。
    const thisYear = taipeiYear();
    await openSearch(page, "", PHONE);
    await page.locator("[data-mobile-filter]").click();
    const sheet = page.getByRole("dialog");
    await sheet.locator("[data-filter-section='year'] > button").click();
    const custom = sheet.getByRole("radio", { name: text("search:search.year.custom") });
    const none = sheet.getByRole("radio", { name: text("search:search.year.none") });
    const fromBox = sheet.getByRole("textbox", { name: text("search:search.year.fromLabel") });
    const toBox = sheet.getByRole("textbox", { name: text("search:search.year.toLabel") });
    const clearAll = sheet.getByRole("link", { name: text("search:search.filter.clearAll") });

    // 情況一：自訂 2018–2019 套用後清除。
    await option(page, "radio", text("search:search.year.custom")).click();
    await fromBox.fill("2018");
    await toBox.fill("2019");
    await toBox.press("Enter");
    await expect(page).toHaveURL(`${member.baseURL}/search?from=2018&to=2019`);
    await expect(custom).toBeChecked();
    await clearAll.click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    await expect(none).toBeChecked();
    await expect(custom).not.toBeChecked();
    await expect(fromBox).toHaveValue("2016");
    await expect(toBox).toHaveValue(thisYear);

    // 情況二：套用近 3 年，按「自訂」不輸入就清除。
    await option(page, "radio", text("search:search.year.last3")).click();
    await expect(page).toHaveURL((url) => url.searchParams.has("from"));
    await option(page, "radio", text("search:search.year.custom")).click();
    await expect(custom).toBeChecked();
    await clearAll.click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    await expect(none).toBeChecked();
    await expect(custom).not.toBeChecked();
    await expect(fromBox).toHaveValue("2016");
    await expect(toBox).toHaveValue(thisYear);
  });

  test("a desktop menu closed by narrowing to phone width hands the focus to the phone filter button", async ({ page }) => {
    // 審查 S2-4、確認審查 N4：桌機的標籤在手機寬度藏起來，焦點不能留在看不到的地方或掉到 body。
    await openSearch(page, "?topic=e_commerce");
    await chip(page, "search:search.filter.topic").click();
    await expect(page.getByRole("checkbox", { name: ECOMMERCE })).toBeVisible();
    await page.setViewportSize(PHONE);
    await expect(page.getByRole("checkbox", { name: ECOMMERCE })).toHaveCount(0);
    await expect(page.locator("[data-mobile-filter]")).toBeFocused();
  });

  test("desktop: clearing after a custom year range shows no limit and the default boxes again", async ({ page }) => {
    // 確認審查 N1 的桌機版：年份選單裡自訂後，按頁面上的「清除篩選」，再打開年份選單是「不限」與預設年份。
    const thisYear = taipeiYear();
    await openSearch(page, "?topic=e_commerce");
    const yearChip = chip(page, "search:search.filter.year");
    const custom = page.getByRole("radio", { name: text("search:search.year.custom") });
    const none = page.getByRole("radio", { name: text("search:search.year.none") });
    const fromBox = page.getByRole("textbox", { name: text("search:search.year.fromLabel") });
    const toBox = page.getByRole("textbox", { name: text("search:search.year.toLabel") });
    const clearAll = page.locator("[data-clear-all]:visible");

    // 情況一：自訂 2018–2019 套用後清除。
    await yearChip.click();
    await option(page, "radio", text("search:search.year.custom")).click();
    await fromBox.fill("2018");
    await toBox.fill("2019");
    await toBox.press("Enter");
    await expect(page).toHaveURL(`${member.baseURL}/search?topic=e_commerce&from=2018&to=2019`);
    await page.keyboard.press("Escape");
    await clearAll.click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    await yearChip.click();
    await expect(none).toBeChecked();
    await expect(custom).not.toBeChecked();
    await expect(fromBox).toHaveValue("2016");
    await expect(toBox).toHaveValue(thisYear);

    // 情況二：套用近 3 年，按「自訂」不輸入，關掉選單後清除。
    await option(page, "radio", text("search:search.year.last3")).click();
    await expect(page).toHaveURL((url) => url.searchParams.has("from"));
    await option(page, "radio", text("search:search.year.custom")).click();
    await expect(custom).toBeChecked();
    await page.keyboard.press("Escape");
    await clearAll.click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    await yearChip.click();
    await expect(none).toBeChecked();
    await expect(custom).not.toBeChecked();
    await expect(fromBox).toHaveValue("2016");
    await expect(toBox).toHaveValue(thisYear);
  });
});

test.describe("search: page shape", () => {
  test("there is no search box, no example queries and no title-only sentence", async ({ page }) => {
    // 驗收條件第一段第一條（epics.md 3.14、AR-67 的「找論文（3）」那一列）：Epic 3 的找論文沒有搜尋框、例句、
    // 「沒有摘要的論文只比對標題…」那一句與「依相關度排序」（審查 A-2）。字不寫在測試裡（AD-21），改驗結構：
    // 沒有任何輸入框；「找到 N 篇」那一行只有這一句（EXPERIENCE 那一句接在它後面）；右邊是「依年份排序」。
    await openSearch(page, "?topic=e_commerce");
    const total = expectedTotal("topic=e_commerce");
    await expect(rows(page)).toHaveCount(total);
    await expect(page.getByRole("searchbox")).toHaveCount(0);
    await expect(page.getByRole("textbox")).toHaveCount(0);
    await expect(page.locator("main form")).toHaveCount(0);
    await expect(page.locator("main").getByRole("heading", { level: 2 })).toHaveText(foundText(total));
    await expect(page.locator("main").getByText(text("search:search.results.byYear"), { exact: true })).toBeVisible();
    // 起始狀態沒有例句：只有標題、「看全部 N 篇」與論文列的連結；按鈕只有論文列的「加入我的文獻庫」（Story 3.17）。
    await openSearch(page);
    const start = page.locator("[data-state='start']");
    await expect(rows(page)).toHaveCount(START_ROWS);
    // 按鈕數從假資料算（E1，Kevin 2026-11-20）：起始的 3 篇裡不在我的文獻庫的那幾篇，各一個「加入我的文獻庫」。
    const startNotInLibrary = filterListRows(LIST_FIXTURES.rows, new URLSearchParams("this_week=true"))!
      .slice(0, START_ROWS)
      .filter((entry) => !entry.personal).length;
    expect(startNotInLibrary).toBe(1);
    await expect(start.getByRole("button")).toHaveCount(startNotInLibrary);
    await expect(start.getByRole("button")).toHaveText(Array(startNotInLibrary).fill(text("search:search.row.add")));
    const rowLinks = await start.locator("[data-paper-row]").getByRole("link").count();
    await expect(start.getByRole("link")).toHaveCount(rowLinks + 1);
    await expect(page.getByRole("textbox")).toHaveCount(0);
  });

  test("each row: the title opens the paper page, the right side has only the full-text link in a new tab", async ({ page }) => {
    // 驗收條件第一段（審查 H-M4）：標題連到論文頁；右側的連結只有「開啟全文」。Story 3.17：右側多了
    // 「加入我的文獻庫」按鈕（已加入的是「● 已加入」，不能按）。
    await openSearch(page, "?topic=e_commerce");
    await expect(rows(page)).toHaveCount(expectedTotal("topic=e_commerce"));
    // 已加入與否從假資料判斷（E1，Kevin 2026-11-20），不從畫面讀；兩種列都至少要有一列。
    const personal = new Set(LIST_FIXTURES.rows.filter((entry) => entry.personal).map((entry) => entry.row.id));
    const expectedIds = filterListRows(LIST_FIXTURES.rows, new URLSearchParams("topic=e_commerce"))!.map((entry) => entry.row.id);
    expect(expectedIds.filter((id) => personal.has(id)).length).toBeGreaterThan(0);
    expect(expectedIds.filter((id) => !personal.has(id)).length).toBeGreaterThan(0);
    // 畫面上的列和預期的完全一樣（順序照 API），兩種列才真的都在畫面上（審查 F5）。
    expect(await rows(page).evaluateAll((elements) => elements.map((element) => element.getAttribute("data-paper-row")))).toEqual(expectedIds);
    const all = await rows(page).all();
    for (const row of all) {
      const id = await row.getAttribute("data-paper-row");
      expect(expectedIds).toContain(id);
      const added = row.locator("[data-library='added']");
      if (personal.has(id!)) {
        await expect(added).toHaveCount(1);
        await expect(added).toHaveText(text("search:search.row.added"));
        // 「●」：字前面一個不念出來的圓點（審查 F20）。
        await expect(added.locator("span[aria-hidden='true']")).toHaveCount(1);
        await expect(row.getByRole("button")).toHaveCount(0);
      } else {
        await expect(added).toHaveCount(0);
        await expect(row.getByRole("button")).toHaveCount(1);
        await expect(row.getByRole("button")).toHaveText(text("search:search.row.add"));
      }
      const links = row.getByRole("link");
      const titleLink = row.locator("h3").getByRole("link");
      await expect(titleLink).toHaveAttribute("href", `/papers/${id}`);
      await expect(titleLink).not.toHaveAttribute("target", /./);
      const fullText = row.locator("[data-open-full-text]");
      const hasDoi = (await fullText.count()) === 1;
      await expect(links).toHaveCount(hasDoi ? 2 : 1);
      if (hasDoi) {
        await expect(fullText).toHaveAttribute("target", "_blank");
        await expect(fullText).toHaveAttribute("rel", "noopener noreferrer");
        await expect(fullText).toHaveText(new RegExp(`^${text("paper:paper.fullText.open")}`));
      }
    }
    const first = rows(page).first();
    const firstId = await first.getAttribute("data-paper-row");
    await first.locator("h3").getByRole("link").click();
    await expect(page).toHaveURL(`${member.baseURL}/papers/${firstId}`);
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
    expect((await lastListRequest(apiMock.requests)).getAll("topic")).toEqual(["platforms"]);
  });

  test("a method tag on the paper page opens search with that method as a condition", async ({ page, apiMock }) => {
    // 審查 S2-1：方法標籤是站內連結，帶進去是可以移除的條件標籤。
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/papers/${paperId("normal")}`);
    const tag = page.locator("[data-method-link]").first();
    await expect(tag).not.toHaveAttribute("target", "_blank");
    await tag.click();
    await expect(page).toHaveURL(`${member.baseURL}/search?method=experiment`);
    await expect(page.locator("h1")).toHaveText(text("search:search.page.title"));
    await expect(page.locator("[data-condition='method']:visible")).toHaveCount(1);
    expect((await lastListRequest(apiMock.requests)).getAll("method")).toEqual(["experiment"]);
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
