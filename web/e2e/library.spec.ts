// 我的文獻庫（Story 3.17，成員版）：EXPERIENCE.md 畫面 13 與 State Patterns「13. 我的文獻庫」的每一種狀態
// （可以操作、很多篇、勾選、沒有全文連結、超過 10 篇、擋住新分頁、移出、空的、載入中、錯誤），
// 論文頁的「加入我的文獻庫」／「● 已加入」＋「移出」（含手機：State Patterns「手機：已經加入我的文獻庫」），
// 找論文每列的「加入我的文獻庫」／「● 已加入」，以及三種寬度的整頁截圖。
// 資料是 fixtures/paper-list.json 的假清單（自己編的）；文獻庫是替身的 libraryIds。
import { fileURLToPath } from "node:url";

import type { Page } from "@playwright/test";

import {
  EXPECTED_MEMBER_HEADER,
  LIST_FIXTURES,
  MEMBERS,
  filterListRows,
  gate,
  libraryOf,
  paperId,
  paperPage,
  type ApiMockState,
  type ApiRequest,
  type ListFixtureRow,
} from "./api-mock.ts";
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { SITES } from "./pages.ts";

const member = SITES[0]!;
const SCREENSHOT_DIR = fileURLToPath(new URL("./screenshots/", import.meta.url));
const DESKTOP = { width: 1280, height: 900 };
const PHONE = { width: 390, height: 844 };
/** 全站頁尾在 899px 以下的高度（Story 3.15；寫死，算法見 page-frame.spec.ts）。 */
const PHONE_FOOTER_HEIGHT = 101;
const ROWS = LIST_FIXTURES.rows;
const WITH_DOI = ROWS.filter((entry) => entry.row.doi_url !== null).map((entry) => entry.row.id);
const NO_DOI = ROWS.filter((entry) => entry.row.doi_url === null).map((entry) => entry.row.id);

function text(key: string): string {
  const value = TEXT[key];
  if (value === undefined) throw new Error(`no expected text ${key}`);
  return value;
}

function fill(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{\{(\w+)\}\}/g, (_, name: string) => String(values[name]));
}

function title(id: string): string {
  return ROWS.find((entry) => entry.row.id === id)!.row.title;
}

async function openLibrary(page: Page, viewport = DESKTOP, query = ""): Promise<void> {
  await page.setViewportSize(viewport);
  await page.goto(`${member.baseURL}/library${query}`);
  await expect(page.locator("h1")).toHaveText(text("library:library.page.title"));
}

const rows = (page: Page) => page.locator("[data-library-rows] > li");
const row = (page: Page, id: string) => page.locator(`[data-library-row="${id}"]`);
const checkbox = (page: Page, id: string) => page.getByRole("checkbox", { name: fill(text("library:library.list.select"), { title: title(id) }) });
const openButton = (page: Page) => page.locator("[data-library='open']:visible");

/** 收到的請求（等到有為止；畫面先換、請求可能還在路上）。 */
async function requestsOf(requests: ApiRequest[], method: string, prefix: string): Promise<ApiRequest[]> {
  await expect.poll(() => requests.filter((request) => request.method === method && request.path.startsWith(prefix)).length).toBeGreaterThan(0);
  return requests.filter((request) => request.method === method && request.path.startsWith(prefix));
}

/**
 * 換掉 window.open：記下開了哪些網址（不真的開分頁），回傳的分頁物件看得到 opener 有沒有被切斷。
 * `allowed` 是瀏覽器放行幾個分頁（之後的都擋住，回 null）；沒給是全部放行。
 */
async function recordTabs(page: Page, allowed: number | null = null): Promise<void> {
  await page.addInitScript((limit) => {
    const opened: { url: string; opener: unknown }[] = [];
    (window as unknown as { __opened: typeof opened }).__opened = opened;
    window.open = () => {
      if (limit !== null && opened.length >= limit) return null;
      const entry = { url: "", opener: "set" as unknown };
      opened.push(entry);
      return {
        set opener(value: unknown) {
          entry.opener = value;
        },
        location: {
          replace: (url: string) => {
            entry.url = url;
          },
        },
      } as unknown as Window;
    };
  }, allowed);
}

async function openedTabs(page: Page): Promise<{ url: string; opener: unknown }[]> {
  return page.evaluate(() => (window as unknown as { __opened: { url: string; opener: unknown }[] }).__opened);
}

test.describe("my library page", () => {
  test("lists my papers newest first with the count; no right rail, filters or AI lines (AR-67)", async ({ page, apiMock }) => {
    await openLibrary(page);
    await expect(rows(page)).toHaveCount(apiMock.libraryIds.length);
    await expect(page.locator("[data-library-count]")).toHaveText(fill(text("library:library.list.count"), { count: apiMock.libraryIds.length }));
    for (const [index, id] of apiMock.libraryIds.entries()) {
      const current = rows(page).nth(index);
      await expect(current).toHaveAttribute("data-library-row", id);
      await expect(current.locator("h3").getByRole("link")).toHaveAttribute("href", `/papers/${id}`);
      await expect(current.locator("[data-library='remove']:visible")).toHaveText(text("library:library.row.remove"));
    }
    // 期刊寫全名（D5）。
    const journal = LIST_FIXTURES.rows.find((entry) => entry.row.id === apiMock.libraryIds[0])!.row.journal!;
    expect(journal).toMatch(/^[a-z]+$/);
    await expect(rows(page).first()).not.toContainText(journal.toUpperCase());
    await expect(page.locator("aside")).toHaveCount(0);
    await expect(page.locator("[data-page='library'] [role='radio'], [data-page='library'] [aria-pressed]")).toHaveCount(0);
    await expect(page.locator("[data-ai-badge]")).toHaveCount(0);
    // 導覽列的「我的文獻庫」標成目前這一頁。
    await expect(page.locator("header").getByRole("link", { name: text("nav:nav.links.library") })).toHaveAttribute("aria-current", "page");
  });

  test("many papers: 50 at a time, load more says the real number left and disappears at the end", async ({ page, apiMock }) => {
    // 一頁 50 篇（client-config 的 library_page_size，和後端的 LIBRARY_PAGE_SIZE 一樣；審查 F12：寫死，不從替身讀）。
    // 測試資料：假清單 30 篇＋這裡編的 98 篇，一共 128 篇（超過 50 篇才有「再載入」）。
    const pageSize = 50;
    apiMock.clientConfig = { ...LIST_FIXTURES.clientConfig, library_page_size: pageSize };
    const extra: ListFixtureRow[] = Array.from({ length: 98 }, (_, index) => {
      const id = `38000000-0000-4000-8000-1${String(index).padStart(11, "0")}`;
      return {
        row: { ...ROWS[0]!.row, id, title: `Fake Library Paper ${index + 1}`, in_my_library: false },
        topics: [],
        this_week: false,
        lab: false,
        personal: false,
      };
    });
    apiMock.listRows = [...ROWS, ...extra];
    apiMock.libraryIds = apiMock.listRows.map((entry) => entry.row.id);
    const total = apiMock.libraryIds.length;
    expect(total).toBe(128);
    expect(total).toBeGreaterThan(pageSize);
    const shownIds = () => rows(page).evaluateAll((elements) => elements.map((element) => element.getAttribute("data-library-row")));
    const more = page.locator("[data-load-more]");

    await openLibrary(page);
    await expect(rows(page)).toHaveCount(50);
    expect(await shownIds()).toEqual(apiMock.libraryIds.slice(0, 50));
    await expect(more).toHaveText(fill(text("search:search.results.more"), { count: 50 }));
    await expect(page.locator("[data-shown]")).toHaveText(fill(text("search:search.results.shown"), { shown: 50, total: 128 }));

    await more.click();
    await expect(rows(page)).toHaveCount(100);
    expect(await shownIds()).toEqual(apiMock.libraryIds.slice(0, 100));
    await expect(page).toHaveURL(`${member.baseURL}/library?shown=100`);
    // 剩下不到一頁：寫實際的篇數。
    await expect(more).toHaveText(fill(text("search:search.results.more"), { count: 28 }));
    await expect(page.locator("[data-shown]")).toHaveText(fill(text("search:search.results.shown"), { shown: 100, total: 128 }));

    await more.click();
    await expect(rows(page)).toHaveCount(128);
    expect(await shownIds()).toEqual(apiMock.libraryIds);
    // 全部列出來以後不顯示。
    await expect(more).toHaveCount(0);
    await expect(page.locator("[data-shown]")).toHaveCount(0);
    await expect(page).toHaveURL(`${member.baseURL}/library?shown=150`);
    const cursors = (await requestsOf(apiMock.requests, "GET", "/api/v1/library")).map((request) => new URL(request.path, member.baseURL).searchParams.get("cursor"));
    expect(cursors).toEqual([null, "o50", "o100"]);
  });

  test("reloading at shown=250 (200 read at most on a fresh open): the address lines up and load more still works", async ({ page, apiMock }) => {
    // 審查 G8：直接打開網址時最多讀 200 篇（MAX_SHOWN）。shown＝200＋一頁（50）時，「再載入」算出來的又是 250，
    // 和網址一樣，按了不會有反應；所以讀完之後網址對齊成 200。測試資料：假清單 30 篇＋這裡編的 270 篇＝300 篇。
    const pageSize = 50;
    apiMock.clientConfig = { ...LIST_FIXTURES.clientConfig, library_page_size: pageSize };
    const extra: ListFixtureRow[] = Array.from({ length: 270 }, (_, index) => {
      const id = `39000000-0000-4000-8000-1${String(index).padStart(11, "0")}`;
      return {
        row: { ...ROWS[0]!.row, id, title: `Fake Library Paper ${index + 1}`, in_my_library: false },
        topics: [],
        this_week: false,
        lab: false,
        personal: false,
      };
    });
    apiMock.listRows = [...ROWS, ...extra];
    apiMock.libraryIds = apiMock.listRows.map((entry) => entry.row.id);
    expect(apiMock.libraryIds.length).toBe(300);
    const shownIds = () => rows(page).evaluateAll((elements) => elements.map((element) => element.getAttribute("data-library-row")));
    const more = page.locator("[data-load-more]");

    await openLibrary(page, DESKTOP, "?shown=250");
    await expect(rows(page)).toHaveCount(200);
    await expect(page).toHaveURL(`${member.baseURL}/library?shown=200`);
    expect(await shownIds()).toEqual(apiMock.libraryIds.slice(0, 200));
    await expect(page.locator("[data-shown]")).toHaveText(fill(text("search:search.results.shown"), { shown: 200, total: 300 }));
    await expect(more).toHaveText(fill(text("search:search.results.more"), { count: 50 }));

    await more.click();
    await expect(rows(page)).toHaveCount(250);
    expect(await shownIds()).toEqual(apiMock.libraryIds.slice(0, 250));
    await expect(page).toHaveURL(`${member.baseURL}/library?shown=250`);
    const cursors = (await requestsOf(apiMock.requests, "GET", "/api/v1/library")).map((request) => new URL(request.path, member.baseURL).searchParams.get("cursor"));
    // 對齊時不重讀：一開始 4 頁（200 篇），再載入讀第 5 頁。
    expect(cursors).toEqual([null, "o50", "o100", "o150", "o200"]);
  });

  test("selecting replaces the count with the action bar; cancel goes back", async ({ page, apiMock }) => {
    apiMock.libraryIds = ROWS.map((entry) => entry.row.id);
    await openLibrary(page);
    const [first, second] = apiMock.libraryIds;
    await checkbox(page, first!).check();
    await checkbox(page, second!).check();
    await expect(page.locator("[data-library-count]")).toBeHidden();
    const bar = page.locator("[data-library-actions]");
    await expect(bar).toBeVisible();
    await expect(openButton(page)).toHaveText(fill(text("library:library.actions.open"), { count: 2 }));
    await expect(openButton(page)).toBeEnabled();
    // 動作列黏在視窗上緣。
    await page.mouse.wheel(0, 2000);
    await expect.poll(async () => (await bar.boundingBox())?.y ?? -1).toBeLessThan(80);
    await bar.locator("[data-library='cancel']").click();
    await expect(bar).toHaveCount(0);
    await expect(page.locator("[data-library-count]")).toBeVisible();
    await expect(checkbox(page, first!)).not.toBeChecked();
  });

  test("open selected: one new tab per DOI link without opener, then 'opened N' for 2 seconds", async ({ page, apiMock }) => {
    await recordTabs(page);
    apiMock.libraryIds = WITH_DOI.slice(0, 3);
    await openLibrary(page);
    for (const id of apiMock.libraryIds) await checkbox(page, id).check();
    await openButton(page).click();
    await expect(openButton(page)).toHaveText(fill(text("library:library.actions.opened"), { count: 3 }));
    const tabs = await openedTabs(page);
    expect(tabs.map((tab) => tab.url)).toEqual(apiMock.libraryIds.map((id) => ROWS.find((entry) => entry.row.id === id)!.row.doi_url));
    expect(tabs.every((tab) => tab.opener === null)).toBe(true);
    await expect(openButton(page)).toHaveText(fill(text("library:library.actions.open"), { count: 3 }), { timeout: 4000 });
  });

  test("a real browser tab opens and has no opener", async ({ page, apiMock }) => {
    apiMock.libraryIds = WITH_DOI.slice(0, 1);
    await openLibrary(page);
    await checkbox(page, apiMock.libraryIds[0]!).check();
    const [popup] = await Promise.all([page.waitForEvent("popup"), openButton(page).click()]);
    expect(await popup.evaluate(() => window.opener)).toBeNull();
  });

  test("no DOI: skipped, one line under the action bar; 'opened N' counts only the opened ones", async ({ page, apiMock }) => {
    await recordTabs(page);
    apiMock.libraryIds = [WITH_DOI[0]!, NO_DOI[0]!, WITH_DOI[1]!];
    await openLibrary(page);
    for (const id of apiMock.libraryIds) await checkbox(page, id).check();
    await openButton(page).click();
    await expect(page.locator("[data-library-note='skipped']")).toHaveText(fill(text("library:library.notes.skipped"), { count: 1 }));
    await expect(openButton(page)).toHaveText(fill(text("library:library.actions.opened"), { count: 2 }));
    expect(await openedTabs(page)).toHaveLength(2);
    // 換勾選時那一行收起來。
    await checkbox(page, WITH_DOI[1]!).uncheck();
    await expect(page.locator("[data-library-note]")).toHaveCount(0);
  });

  test("more than 10: the button is disabled with 'at most 10 at a time'", async ({ page, apiMock }) => {
    apiMock.libraryIds = WITH_DOI.slice(0, 11);
    await openLibrary(page);
    for (const id of apiMock.libraryIds.slice(0, 10)) await checkbox(page, id).check();
    await expect(openButton(page)).toBeEnabled();
    await expect(page.getByText(text("library:library.actions.max"), { exact: true })).toHaveCount(0);
    await checkbox(page, apiMock.libraryIds[10]!).check();
    await expect(openButton(page)).toBeDisabled();
    await expect(page.getByText(text("library:library.actions.max"), { exact: true }).locator("visible=true")).toHaveCount(1);
  });

  test("the browser blocks new tabs: no 'opened N', the links are listed under the action bar", async ({ page, apiMock }) => {
    await recordTabs(page, 0);
    apiMock.libraryIds = WITH_DOI.slice(0, 2);
    await openLibrary(page);
    for (const id of apiMock.libraryIds) await checkbox(page, id).check();
    await openButton(page).click();
    const note = page.locator("[data-library-note='blocked']");
    await expect(note).toContainText(text("library:library.notes.blocked"));
    await expect(openButton(page)).toHaveText(fill(text("library:library.actions.open"), { count: 2 }));
    const links = note.getByRole("link");
    await expect(links).toHaveCount(2);
    for (const [index, id] of apiMock.libraryIds.entries()) {
      const link = links.nth(index);
      await expect(link).toHaveText(title(id));
      await expect(link).toHaveAttribute("href", ROWS.find((entry) => entry.row.id === id)!.row.doi_url!);
      await expect(link).toHaveAttribute("target", "_blank");
      await expect(link).toHaveAttribute("rel", "noopener noreferrer");
    }
    // 動作列下方的訊息讀得到（role=status）。
    await expect(page.locator("[data-library-notes]")).toHaveAttribute("role", "status");
  });

  test("the browser lets the first tab through and blocks the rest: only the ones not opened are listed", async ({ page, apiMock }) => {
    // 審查 F20：已經開了的那一篇不再列（照著點會重複開）。
    await recordTabs(page, 1);
    apiMock.libraryIds = WITH_DOI.slice(0, 3);
    await openLibrary(page);
    for (const id of apiMock.libraryIds) await checkbox(page, id).check();
    await openButton(page).click();
    const note = page.locator("[data-library-note='blocked']");
    await expect(note).toContainText(text("library:library.notes.blocked"));
    expect((await openedTabs(page)).map((tab) => tab.url)).toEqual([ROWS.find((entry) => entry.row.id === WITH_DOI[0])!.row.doi_url]);
    await expect(note.getByRole("link")).toHaveText(WITH_DOI.slice(1, 3).map((id) => title(id)));
  });

  test("phone: the action bar is fixed at the bottom; open lists the links in place without opening tabs", async ({ page, apiMock }) => {
    await recordTabs(page);
    // 篇數要多到可以捲動，固定在底部才看得出來。
    apiMock.libraryIds = [WITH_DOI[0]!, WITH_DOI[1]!, NO_DOI[0]!, ...WITH_DOI.slice(2, 12)];
    await openLibrary(page, PHONE);
    for (const id of apiMock.libraryIds.slice(0, 3)) await checkbox(page, id).check();
    const dock = page.locator("[data-library-dock]");
    const box = await dock.boundingBox();
    expect(box!.y + box!.height).toBeCloseTo(PHONE.height, 0);
    await expect(page.locator("[data-library-count]")).toBeVisible();
    await openButton(page).click();
    const note = page.locator("[data-library-note='mobile']");
    await expect(note).toContainText(text("library:library.notes.mobile"));
    await expect(note.getByRole("link")).toHaveCount(2);
    await expect(page.locator("[data-library-note='skipped']")).toHaveText(fill(text("library:library.notes.skipped"), { count: 1 }));
    expect(await openedTabs(page)).toHaveLength(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(PHONE.width);
  });

  test("remove: the row turns into 'removed' + undo in place, no confirm; undo brings it back; gone after leaving", async ({ page, apiMock }) => {
    const [first, second] = apiMock.libraryIds;
    const total = apiMock.libraryIds.length;
    await openLibrary(page);
    await checkbox(page, first!).check();
    await row(page, first!).locator("[data-library='remove']:visible").click();
    const gone = row(page, first!).locator("[data-library-gone]");
    await expect(gone).toContainText(text("library:library.row.removed"));
    await expect(gone).toContainText(title(first!));
    await expect(row(page, first!).locator("[data-library='restore']")).toBeFocused();
    await expect(page.locator("[data-library-count]")).toHaveText(fill(text("library:library.list.count"), { count: total - 1 }));
    // 移出的那一篇不再算在勾選裡。
    await expect(page.locator("[data-library-actions]")).toHaveCount(0);
    expect((await requestsOf(apiMock.requests, "DELETE", "/api/v1/library/")).map((request) => request.path)).toEqual([`/api/v1/library/${first}`]);

    await row(page, first!).locator("[data-library='restore']").click();
    await expect(row(page, first!).locator("h3")).toHaveText(title(first!));
    await expect(row(page, first!).locator("[data-library='remove']:visible")).toBeFocused();
    await expect(page.locator("[data-library-count]")).toHaveText(fill(text("library:library.list.count"), { count: total }));
    expect((await requestsOf(apiMock.requests, "POST", "/api/v1/library/")).map((request) => request.path)).toEqual([`/api/v1/library/${first}/restore`]);

    await row(page, second!).locator("[data-library='remove']:visible").click();
    await expect(row(page, second!).locator("[data-library-gone]")).toBeVisible();
    await page.locator("header").getByRole("link", { name: text("nav:nav.links.search") }).click();
    await page.locator("header").getByRole("link", { name: text("nav:nav.links.library") }).click();
    await expect(rows(page)).toHaveCount(total - 1);
    await expect(row(page, second!)).toHaveCount(0);
  });

  test("remove fails: the row stays as it was, no new sentence", async ({ page, apiMock }) => {
    apiMock.libraryChange = "failed";
    const [first] = apiMock.libraryIds;
    await openLibrary(page);
    const remove = row(page, first!).locator("[data-library='remove']:visible");
    // 審查 K4：清單列完才讀 before（webkit 曾在清單還沒載完時就讀到只有標題的 main）。
    await expect(rows(page)).toHaveCount(apiMock.libraryIds.length);
    const before = await page.locator("main").innerText();
    await remove.click();
    await requestsOf(apiMock.requests, "DELETE", "/api/v1/library/");
    await expect(remove).toBeEnabled();
    await expect(row(page, first!).locator("[data-library-gone]")).toHaveCount(0);
    expect(await page.locator("main").innerText()).toBe(before);
  });

  test("two clicks in the same frame on remove, then on undo: one request each, the count moves by one", async ({ page, apiMock }) => {
    // 審查 G7：兩下在同一個工作裡送出（第二下時畫面還沒換成「送出中」），只能送一次，篇數只減 1、只加 1。
    const [first] = apiMock.libraryIds;
    const total = apiMock.libraryIds.length;
    const count = page.locator("[data-library-count]");
    await openLibrary(page);
    await row(page, first!).locator("[data-library='remove']:visible").evaluate((element: HTMLElement) => {
      element.click();
      element.click();
    });
    await expect(row(page, first!).locator("[data-library='restore']")).toBeFocused();
    await expect(count).toHaveText(fill(text("library:library.list.count"), { count: total - 1 }));
    expect(apiMock.requests.filter((request) => request.method === "DELETE" && request.path.startsWith("/api/v1/library/")).map((request) => request.path)).toEqual([`/api/v1/library/${first}`]);

    await row(page, first!).locator("[data-library='restore']").evaluate((element: HTMLElement) => {
      element.click();
      element.click();
    });
    await expect(row(page, first!).locator("[data-library='remove']:visible")).toBeFocused();
    await expect(count).toHaveText(fill(text("library:library.list.count"), { count: total }));
    expect(apiMock.requests.filter((request) => request.method === "POST" && request.path.startsWith("/api/v1/library/")).map((request) => request.path)).toEqual([`/api/v1/library/${first}/restore`]);
  });

  test("remove when it is already gone (removed in another tab): 'removed' without undo", async ({ page, apiMock }) => {
    // 審查 F3：後端回 404（這一次沒有真的移出），不給「復原」：復原會拿到別次移出的封存（ADR-0049）。
    const [first, second] = apiMock.libraryIds;
    const total = apiMock.libraryIds.length;
    await openLibrary(page);
    await expect(rows(page)).toHaveCount(total);
    // 另一個分頁已經移出了（替身的文獻庫少了這一篇，封存裡沒有它）。
    apiMock.libraryIds = apiMock.libraryIds.filter((id) => id !== first);
    await row(page, first!).locator("[data-library='remove']:visible").click();
    const gone = row(page, first!).locator("[data-library-gone]");
    await expect(gone).toContainText(text("library:library.row.removed"));
    await expect(row(page, first!).locator("[data-library='restore']")).toHaveCount(0);
    await expect(row(page, first!).locator("[data-library='gone']")).toBeFocused();
    await expect(page.locator("[data-library-count]")).toHaveText(fill(text("library:library.list.count"), { count: total - 1 }));
    // 另一篇照常：真的移出了，有「復原」。
    await row(page, second!).locator("[data-library='remove']:visible").click();
    await expect(row(page, second!).locator("[data-library='restore']")).toBeFocused();
  });

  test("restore after leaving and coming back keeps the original place (the mock follows ADR-0049)", async ({ page, apiMock }) => {
    // 審查 F13：復原寫回原本的加入時間，回到原本的位置（不是排到第一個）。
    const ids = apiMock.libraryIds;
    await openLibrary(page);
    await row(page, ids[1]!).locator("[data-library='remove']:visible").click();
    await row(page, ids[1]!).locator("[data-library='restore']").click();
    await expect(row(page, ids[1]!).locator("[data-library='remove']:visible")).toBeFocused();
    await page.reload();
    await expect(rows(page)).toHaveCount(ids.length);
    expect(await rows(page).evaluateAll((elements) => elements.map((element) => element.getAttribute("data-library-row")))).toEqual(ids);
  });

  test("empty: the message and one line (AR-67 interim: no upload or import)", async ({ page, apiMock }) => {
    apiMock.libraryIds = [];
    await openLibrary(page);
    const empty = page.locator("[data-state='empty']");
    await expect(empty.getByRole("status")).toHaveText(text("library:library.empty.message"));
    await expect(empty).toContainText(text("library:library.empty.fromPaper"));
    await expect(empty.locator("p")).toHaveCount(2);
    await expect(page.locator("[data-page='library']").getByRole("button")).toHaveCount(0);
    // 不加連結（驗收條件；審查 F15）。
    await expect(empty.getByRole("link")).toHaveCount(0);
    await expect(page.locator("[data-page='library']").getByRole("link")).toHaveCount(0);
    await expect(page.locator("[data-library-count]")).toHaveCount(0);
  });

  test("loading: the row skeleton until the list arrives", async ({ page, apiMock }) => {
    const [libraryGate, release] = gate();
    apiMock.libraryGate = libraryGate;
    await openLibrary(page);
    await expect(page.getByRole("status", { name: text("library:library.list.loading") })).toBeVisible();
    await expect(rows(page)).toHaveCount(0);
    release();
    await expect(rows(page)).toHaveCount(apiMock.libraryIds.length);
    await expect(page.locator("[data-state='loading']")).toHaveCount(0);
  });

  test("error: the shared load-failed bar with reload", async ({ page, apiMock }) => {
    apiMock.library = "failed";
    await openLibrary(page);
    const alert = page.locator("[data-state='error']");
    await expect(alert).toHaveAttribute("role", "alert");
    await expect(alert).toContainText(text("library:library.error.message"));
    apiMock.library = "ok";
    await alert.getByRole("button", { name: text("common:states.loadFailed.action") }).click();
    await expect(rows(page)).toHaveCount(apiMock.libraryIds.length);
  });

  test("signed out: back to the login page, then back here", async ({ page, apiMock }) => {
    await openLibrary(page);
    await expect(rows(page)).toHaveCount(apiMock.libraryIds.length);
    apiMock.library = "expired";
    await page.locator("header").getByRole("link", { name: text("nav:nav.links.search") }).click();
    await page.locator("header").getByRole("link", { name: text("nav:nav.links.library") }).click();
    await expect(page).toHaveURL(/\/login\?/);
    expect(new URL(page.url()).searchParams.get("next")).toBe("/library");
    expect(new URL(page.url()).searchParams.get("expired")).toBe("true");
  });

  test("tap targets are at least 44px", async ({ page, apiMock }) => {
    await openLibrary(page);
    const [first] = apiMock.libraryIds;
    await checkbox(page, first!).check();
    for (const selector of ["[data-library='remove']:visible", "[data-library='open']:visible", "[data-library='cancel']:visible"]) {
      const box = await page.locator(selector).first().boundingBox();
      expect(box!.height, selector).toBeGreaterThanOrEqual(44);
    }
    const label = await row(page, first!).locator("label").boundingBox();
    expect(label!.height).toBeGreaterThanOrEqual(44);
    expect(label!.width).toBeGreaterThanOrEqual(44);
  });
});

test.describe("my library on the paper page", () => {
  const normal = () => paperId("normal");

  test("desktop: add, then 'already in my library' + remove; remove goes back to add", async ({ page, apiMock }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/papers/${normal()}`);
    const rail = page.locator("[data-library='rail']");
    await expect(rail.getByRole("heading")).toHaveText(text("paper:paper.library.heading"));
    await rail.getByRole("button", { name: text("paper:paper.library.add") }).click();
    await expect(rail).toContainText(text("paper:paper.library.added"));
    // 「●」：字前面一個不念出來的圓點（審查 F20）。
    await expect(rail.locator("span[aria-hidden='true']")).toHaveCount(1);
    const remove = rail.getByRole("button", { name: text("paper:paper.library.remove") });
    await expect(remove).toBeFocused();
    expect((await requestsOf(apiMock.requests, "PUT", "/api/v1/library/")).map((request) => request.path)).toEqual([`/api/v1/library/${normal()}`]);
    await remove.click();
    await expect(rail.getByRole("button", { name: text("paper:paper.library.add") })).toBeFocused();
    await requestsOf(apiMock.requests, "DELETE", "/api/v1/library/");
    expect(apiMock.libraryIds).not.toContain(normal());
  });

  test("opens as 'already in my library' when the API says so", async ({ page, apiMock }) => {
    apiMock.libraryIds = [normal()];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/papers/${normal()}`);
    const rail = page.locator("[data-library='rail']");
    await expect(rail).toContainText(text("paper:paper.library.added"));
    await expect(rail.getByRole("button", { name: text("paper:paper.library.add") })).toHaveCount(0);
  });

  test("phone: add sits in the bottom bar; once added the bar has only open full text and the block moves under the bibliography", async ({ page }) => {
    await page.setViewportSize({ width: 500, height: 900 });
    await page.goto(`${member.baseURL}/papers/${normal()}`);
    const bar = page.locator("[data-full-text='bar']");
    await expect(bar.getByRole("button")).toHaveCount(2);
    await expect(page.locator("[data-library='mobile']")).toHaveCount(0);
    await bar.getByRole("button", { name: text("paper:paper.library.add") }).click();
    await expect(bar.getByRole("button")).toHaveCount(1);
    await expect(bar.getByRole("button")).toContainText(text("paper:paper.fullText.open"));
    const block = page.locator("[data-library='mobile']");
    await expect(block).toContainText(text("paper:paper.library.added"));
    await expect(block.getByRole("button", { name: text("paper:paper.library.remove") })).toBeFocused();
    // 下面那一塊在書目與分類下面、取得全文的後面。
    const fullText = await page.locator("[data-full-text='mobile']").boundingBox();
    expect((await block.boundingBox())!.y).toBeGreaterThan(fullText!.y);
  });

  test("add fails: the button comes back, no new sentence", async ({ page, apiMock }) => {
    apiMock.libraryChange = "failed";
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/papers/${normal()}`);
    const add = page.locator("[data-library='rail']").getByRole("button", { name: text("paper:paper.library.add") });
    await add.click();
    await requestsOf(apiMock.requests, "PUT", "/api/v1/library/");
    await expect(add).toBeEnabled();
    await expect(page.locator("[data-library='rail']")).not.toContainText(text("paper:paper.library.added"));
  });

  test("add when signed out: back to the login page", async ({ page, apiMock }) => {
    apiMock.libraryChange = "expired";
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/papers/${normal()}`);
    await page.locator("[data-library='rail']").getByRole("button", { name: text("paper:paper.library.add") }).click();
    await expect(page).toHaveURL(/\/login\?/);
    expect(new URL(page.url()).searchParams.get("expired")).toBe("true");
  });
});

test.describe("my library on search rows", () => {
  test("add lights up on hover; once added the row says 'added' and is not a button; the scope 'my library' includes it", async ({ page, apiMock }) => {
    // 第二版（EXPERIENCE.md「Kevin 的決定」第 87 項）：「● 已加入」旁邊多一個「移出」按鈕，加入後焦點移到「移出」（設計稿 swapLib）。
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?topic=e_commerce`);
    const id = (await page.locator("[data-paper-row]").filter({ has: page.locator("[data-library='add']") }).first().getAttribute("data-paper-row"))!;
    const target = page.locator(`[data-paper-row="${id}"]`);
    const add = target.locator("[data-library='add']");
    const face = add.locator("span").first();
    const dim = await face.evaluate((element) => getComputedStyle(element).color);
    await target.hover();
    await expect.poll(() => face.evaluate((element) => getComputedStyle(element).color)).not.toBe(dim);
    await add.click();
    const added = target.locator("[data-library='added']");
    await expect(added).toHaveText(text("search:search.row.added"));
    await expect(target.locator("[data-library='remove']")).toBeFocused();
    await expect(target.getByRole("button")).toHaveCount(1);
    expect(apiMock.libraryIds).toContain(id);
    await page.getByRole("button", { name: text("search:search.scope.personal"), exact: true }).click();
    await expect(page.locator(`[data-paper-row="${id}"] [data-library='added']`)).toBeVisible();
  });

  test("add lights up when focus is in the row (keyboard)", async ({ page, browserName }) => {
    // 驗收條件「滑到或聚焦在一列」（審查 F14）：用鍵盤把焦點移到那一列的標題連結（滑鼠不在列上）。
    // 審查 K1：要真的用鍵盤走到。Safari 預設 Tab 不停在連結上，要用 Option+Tab（同 a11y-floor.spec.ts 的 walkFocus）。
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?topic=e_commerce`);
    const target = page.locator("[data-paper-row]").filter({ has: page.locator("[data-library='add']") }).first();
    const face = target.locator("[data-library='add'] span").first();
    const dim = await face.evaluate((element) => getComputedStyle(element).color);
    const titleLink = target.locator("h3").getByRole("link");
    const key = browserName === "webkit" ? "Alt+Tab" : "Tab";
    // 從頁面開頭往後走，最多按 MAX_TABS 下（導覽列、篩選列、前面幾列的連結與按鈕加起來遠少於這個數）。
    const MAX_TABS = 80;
    let reached = false;
    for (let presses = 0; presses < MAX_TABS && !reached; presses += 1) {
      await page.keyboard.press(key);
      reached = await titleLink.evaluate((element) => element === document.activeElement);
    }
    expect(reached).toBe(true);
    await expect(titleLink).toBeFocused();
    await expect.poll(() => face.evaluate((element) => getComputedStyle(element).color)).not.toBe(dim);
  });

  test("a list read still on its way when adding does not turn the row back into 'add'", async ({ page, apiMock }) => {
    // 審查 F10：「再載入」還在讀（伺服器那一頁是加入之前讀的）時按加入，讀完之後那一列仍是「● 已加入」。
    // 審查 G3：替身在接到請求時就把回應算好（加入之前的文獻庫），閘門只延遲送出；這裡也確認晚到的那一份真的是加入前算的。
    const query = "from=2016&to=2026";
    const total = filterListRows(ROWS, new URLSearchParams(`year=${Array.from({ length: 11 }, (_, index) => 2016 + index).join("&year=")}`))!.length;
    expect(total).toBeGreaterThan(LIST_FIXTURES.clientConfig.paper_list_page_size);
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${query}`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(LIST_FIXTURES.clientConfig.paper_list_page_size);
    const target = page.locator("[data-paper-row]").filter({ has: page.locator("[data-library='add']") }).first();
    const id = (await target.getAttribute("data-paper-row"))!;
    const before = apiMock.libraryIds;
    expect(before).not.toContain(id);
    const [listGate, release] = gate();
    apiMock.listGate = listGate;
    const late = page.waitForResponse((response) => response.url().includes("/api/v1/papers?") && response.url().includes("cursor="));
    await page.locator("[data-load-more]").click();
    await expect.poll(() => apiMock.requests.filter((request) => request.path.startsWith("/api/v1/papers?") && request.path.includes("cursor=")).length).toBeGreaterThan(0);
    await target.locator("[data-library='add']").click();
    await expect(page.locator(`[data-paper-row="${id}"] [data-library='added']`)).toBeVisible();
    // 加入在伺服器那邊已經完成，還在路上的那一份還沒送出。
    expect(apiMock.libraryIds).toContain(id);
    apiMock.listGate = null;
    release();
    const stale = (await (await late).json()) as { items: { id: string; in_my_library: boolean }[] };
    expect(stale.items.length).toBeGreaterThan(0);
    // 晚到的那一份照加入之前的文獻庫算（不是放行時的）。
    expect(stale.items.map((item) => item.in_my_library)).toEqual(stale.items.map((item) => before.includes(item.id)));
    await expect(page.locator("[data-paper-row]")).toHaveCount(total);
    await expect(page.locator(`[data-paper-row="${id}"] [data-library='added']`)).toBeVisible();
    await expect(page.locator(`[data-paper-row="${id}"] [data-library='add']`)).toHaveCount(0);
    expect(apiMock.libraryIds).toContain(id);
  });

  test("a first-page re-read still on its way when adding another row does not turn that row back into 'add'", async ({ page, apiMock }) => {
    // 審查 K3：晚到的那一份要包含被按的那一列、而且在那一份裡是「未加入」。
    // 做法：加入甲列後，畫面正在用的清單會重讀第一頁（markInLibrary 的 invalidate）；把這次重讀卡住，
    // 在它還沒回來時按乙列的加入。那一份是乙加入之前算的（乙是 false），乙也在這一頁上。
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?topic=e_commerce`);
    const shown = filterListRows(ROWS, new URLSearchParams("topic=e_commerce"))!;
    await expect(page.locator("[data-paper-row]")).toHaveCount(shown.length);
    const candidates = shown.map((entry) => entry.row.id).filter((id) => !apiMock.libraryIds.includes(id));
    expect(candidates.length).toBeGreaterThanOrEqual(2);
    const [first, second] = candidates as [string, string];
    const listReads = () => apiMock.requests.filter((request) => request.method === "GET" && request.path.startsWith("/api/v1/papers?")).length;
    const readsBefore = listReads();
    const [rereadGate, releaseReread] = gate();
    apiMock.listGate = rereadGate;
    const reread = page.waitForRequest((request) => request.method() === "GET" && new URL(request.url()).pathname === "/api/v1/papers");
    await page.locator(`[data-paper-row="${first}"] [data-library='add']`).click();
    await expect(page.locator(`[data-paper-row="${first}"] [data-library='added']`)).toBeVisible();
    await expect.poll(listReads).toBe(readsBefore + 1);
    // 之後的讀取（乙加入後的重讀）另外卡住，只放行上面那一份。
    const [laterGate, releaseLater] = gate();
    apiMock.listGate = laterGate;
    await page.locator(`[data-paper-row="${second}"] [data-library='add']`).click();
    await expect(page.locator(`[data-paper-row="${second}"] [data-library='added']`)).toBeVisible();
    expect(apiMock.libraryIds).toContain(second);
    releaseReread();
    const lateResponse = (await (await reread).response())!;
    const late = (await lateResponse.json()) as { items: { id: string; in_my_library: boolean }[] };
    await lateResponse.finished();
    // 晚到的那一份有乙、而且乙是「未加入」（甲已經是加入）。
    expect(late.items.find((item) => item.id === second)?.in_my_library).toBe(false);
    expect(late.items.find((item) => item.id === first)?.in_my_library).toBe(true);
    // 等瀏覽器把那一份交給畫面（兩個畫格），再看乙仍是「● 已加入」。
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(page.locator(`[data-paper-row="${second}"] [data-library='add']`)).toHaveCount(0);
    await expect(page.locator(`[data-paper-row="${second}"] [data-library='added']`)).toBeVisible();
    apiMock.listGate = null;
    releaseLater();
    await expect.poll(listReads).toBeGreaterThan(readsBefore + 1);
    await expect(page.locator(`[data-paper-row="${second}"] [data-library='added']`)).toBeVisible();
    await expect(page.locator(`[data-paper-row="${second}"] [data-library='add']`)).toHaveCount(0);
  });

  test("the mock answers with the member who sent the request, even if someone else signs in before it is sent", async ({ page, apiMock }) => {
    // 審查 K2：替身「接到請求就記下成員」要有測試。甲、乙對同一篇的收藏狀態不同；
    // 卡住甲的找論文請求，換成乙之後再放行，回應仍是甲的資料。
    const shown = filterListRows(ROWS, new URLSearchParams("topic=e_commerce"))!;
    const mine = shown.find((entry) => entry.personal)!.row.id;
    expect(apiMock.member).toEqual(MEMBERS[0]);
    const mineAll = apiMock.libraryIds;
    expect(mineAll).toContain(mine);
    expect(libraryOf(apiMock, MEMBERS[1]!.member_id).items.map((record) => record.id)).not.toContain(mine);
    const [listGate, release] = gate();
    apiMock.listGate = listGate;
    const sent = page.waitForRequest((request) => request.method() === "GET" && new URL(request.url()).pathname === "/api/v1/papers");
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?topic=e_commerce`);
    const request = await sent;
    await expect.poll(() => apiMock.requests.filter((entry) => entry.method === "GET" && entry.path.startsWith("/api/v1/papers?")).length).toBe(1);
    apiMock.member = MEMBERS[1]!;
    apiMock.listGate = null;
    release();
    const answer = (await (await request.response())!.json()) as { items: { id: string; in_my_library: boolean }[] };
    expect(answer.items.find((item) => item.id === mine)?.in_my_library).toBe(true);
    // 每一列都照甲的文獻庫算（乙的是空的）。
    expect(answer.items.map((item) => item.in_my_library)).toEqual(answer.items.map((item) => mineAll.includes(item.id)));
  });

  test("someone else signs in from another tab: the next page shows their library, not the cached one", async ({ page, apiMock }) => {
    // 審查 F1：同一個瀏覽器，另一個分頁換成乙登入（cookie 換了）。這個分頁換頁時「我是誰」換了人，快取裡甲的論文列不能再用。
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?topic=e_commerce`);
    const shown = filterListRows(ROWS, new URLSearchParams("topic=e_commerce"))!;
    await expect(page.locator("[data-paper-row]")).toHaveCount(shown.length);
    const mine = shown.find((entry) => entry.personal)!.row.id;
    await expect(page.locator(`[data-paper-row="${mine}"] [data-library='added']`)).toBeVisible();
    await page.locator("header").getByRole("link", { name: text("nav:nav.links.library") }).click();
    await expect(rows(page)).toHaveCount(apiMock.libraryIds.length);
    apiMock.member = MEMBERS[1]!;
    await page.goBack();
    await expect(page).toHaveURL(`${member.baseURL}/search?topic=e_commerce`);
    // 乙的文獻庫是空的：甲加過的那一篇在乙看來是「加入我的文獻庫」。
    await expect(page.locator(`[data-paper-row="${mine}"] [data-library='add']`)).toBeVisible();
    await expect(page.locator("[data-library='added']")).toHaveCount(0);
    await page.locator("header").getByRole("link", { name: text("nav:nav.links.library") }).click();
    await expect(page.locator("[data-state='empty']")).toBeVisible();
  });

  test("the library is empty and the scope is my library: the message and 'search all tracked journals'", async ({ page, apiMock }) => {
    apiMock.libraryIds = [];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    const empty = page.locator("[data-state='library-empty']");
    await expect(empty).toContainText(text("search:search.empty.library"));
    await empty.getByRole("link", { name: text("search:search.empty.toTracked") }).click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    await expect(page.getByRole("button", { name: text("search:search.scope.tracked") })).toHaveAttribute("aria-pressed", "true");
  });
});

// 第二版（2026-11-20；EXPERIENCE.md「Kevin 的決定」第 86～91 項，設計稿 search-no-box.html、library.html 第二版）。
// 預期都從假資料算或寫死，不從畫面讀。

/** 找論文的清單讀了幾次（GET /api/v1/papers?…）。 */
function listReads(requests: ApiRequest[]): number {
  return requests.filter((request) => request.method === "GET" && request.path.startsWith("/api/v1/papers?")).length;
}

/** 等瀏覽器把已經到的回應交給畫面（兩個畫格）。 */
async function twoFrames(page: Page): Promise<void> {
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
}

test.describe("search rows: remove (second version)", () => {
  // 主題「IT 專案管理」：假資料裡有已加入的（第一位測試成員的文獻庫）、沒加入的、沒有 DOI 的列。
  const QUERY = "topic=it_project_management";
  const shown = filterListRows(ROWS, new URLSearchParams(QUERY))!;
  const mine = shown.find((entry) => entry.personal)!.row.id;
  const searchRow = (page: Page, id: string) => page.locator(`[data-paper-row="${id}"]`);

  test("each row on the right: add, or 'added' + a dim remove, then open full text; no DOI has no link (decisions 87, 88)", async ({ page }) => {
    expect(shown.some((entry) => entry.personal)).toBe(true);
    expect(shown.some((entry) => !entry.personal && entry.row.doi_url !== null)).toBe(true);
    expect(shown.some((entry) => entry.row.doi_url === null)).toBe(true);
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${QUERY}`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(shown.length);
    for (const entry of shown) {
      const current = searchRow(page, entry.row.id);
      const buttons = current.getByRole("button");
      await expect(buttons).toHaveCount(1);
      if (entry.personal) {
        await expect(current.locator("[data-library='added']")).toHaveText(text("search:search.row.added"));
        await expect(buttons).toHaveText(text("search:search.row.remove"));
        await expect(buttons).toHaveAccessibleName(fill(text("search:search.row.removeLabel"), { title: entry.row.title }));
      } else {
        await expect(current.locator("[data-library='added']")).toHaveCount(0);
        await expect(buttons).toHaveText(text("search:search.row.add"));
        await expect(buttons).toHaveAccessibleName(fill(text("search:search.row.addLabel"), { title: entry.row.title }));
      }
      const fullText = current.locator("[data-open-full-text]");
      if (entry.row.doi_url === null) {
        await expect(fullText).toHaveCount(0);
      } else {
        await expect(fullText).toHaveAttribute("href", entry.row.doi_url);
        await expect(fullText).toHaveAttribute("target", "_blank");
        await expect(fullText).toHaveAttribute("rel", "noopener noreferrer");
      }
    }
    // 「移出」平常是第三層字色（#8E887B），滑到那一列變閱讀字色（#D5D0C5）。
    const remove = searchRow(page, mine).locator("[data-library='remove']");
    await expect(remove).toHaveCSS("color", "rgb(142, 136, 123)");
    await searchRow(page, mine).locator("h3").hover();
    await expect(remove).toHaveCSS("color", "rgb(213, 208, 197)");
  });

  test("remove turns the row back into add at once (focus on add, one DELETE); add again; open full text does not move", async ({ page, apiMock }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${QUERY}`);
    const current = searchRow(page, mine);
    const fullText = current.locator("[data-open-full-text]");
    const x = async () => (await fullText.boundingBox())!.x;
    const before = await x();
    await current.locator("[data-library='remove']").click();
    const add = current.locator("[data-library='add']");
    await expect(add).toBeFocused();
    await expect(current.locator("[data-library='added']")).toHaveCount(0);
    expect(apiMock.requests.filter((request) => request.method === "DELETE").map((request) => request.path)).toEqual([`/api/v1/library/${mine}`]);
    expect(apiMock.libraryIds).not.toContain(mine);
    // 不做「復原」。
    await expect(current.getByRole("button")).toHaveCount(1);
    expect(await x()).toBe(before);
    await add.click();
    await expect(current.locator("[data-library='remove']")).toBeFocused();
    expect(apiMock.libraryIds).toContain(mine);
    expect(await x()).toBe(before);
  });

  test("remove fails: the button stays as it was, no new sentence", async ({ page, apiMock }) => {
    apiMock.libraryChange = "failed";
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${QUERY}`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(shown.length);
    const before = await page.locator("main").innerText();
    const remove = searchRow(page, mine).locator("[data-library='remove']");
    await remove.click();
    await requestsOf(apiMock.requests, "DELETE", "/api/v1/library/");
    await expect(remove).toBeEnabled();
    await expect(searchRow(page, mine).locator("[data-library='added']")).toBeVisible();
    expect(await page.locator("main").innerText()).toBe(before);
  });

  test("remove fails after a keyboard press: focus comes back to the remove button", async ({ page, apiMock }) => {
    // 送出中按鈕停用、焦點會掉到 body；失敗後按鈕退回原樣，焦點也要回到「移出」。
    apiMock.libraryChange = "failed";
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${QUERY}`);
    const remove = searchRow(page, mine).locator("[data-library='remove']");
    await remove.focus();
    await page.keyboard.press("Enter");
    await requestsOf(apiMock.requests, "DELETE", "/api/v1/library/");
    await expect(remove).toBeEnabled();
    await expect(remove).toBeFocused();
  });

  test("remove when signed out: back to the login page, then back to this search", async ({ page, apiMock }) => {
    apiMock.libraryChange = "expired";
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${QUERY}`);
    await searchRow(page, mine).locator("[data-library='remove']").click();
    await expect(page).toHaveURL(/\/login\?/);
    const params = new URL(page.url()).searchParams;
    expect(params.get("expired")).toBe("true");
    expect(params.get("next")).toBe(`/search?${QUERY}`);
  });

  test("remove when it is already gone (removed in another tab, 404): the row turns into add", async ({ page, apiMock }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${QUERY}`);
    await expect(searchRow(page, mine).locator("[data-library='added']")).toBeVisible();
    apiMock.libraryIds = apiMock.libraryIds.filter((id) => id !== mine);
    await searchRow(page, mine).locator("[data-library='remove']").click();
    await expect(searchRow(page, mine).locator("[data-library='add']")).toBeFocused();
    await expect(searchRow(page, mine).locator("[data-library='added']")).toHaveCount(0);
  });

  test("scope 'my library': removed rows stay in place as add, the count does not change, no re-read; reload follows the server", async ({ page, apiMock }) => {
    // Kevin 2026-11-20 決定：範圍「我的文獻庫」時按移出，那一列留在原地變回「加入我的文獻庫」，篇數不變；重新整理後才不在清單裡。
    const ids = [...apiMock.libraryIds];
    const inSearch = filterListRows(ROWS, new URLSearchParams("scope=personal"), ids)!.map((entry) => entry.row.id);
    expect(inSearch.length).toBe(ids.length);
    expect(inSearch.length).toBeGreaterThanOrEqual(2);
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    const heading = page.locator("main").getByRole("heading", { level: 2 });
    await expect(page.locator("[data-paper-row]")).toHaveCount(inSearch.length);
    const found = fill(text("search:search.results.found"), { count: inSearch.length });
    await expect(heading).toHaveText(found);
    const readsBefore = listReads(apiMock.requests);
    for (const id of inSearch) {
      await searchRow(page, id).locator("[data-library='remove']").click();
      await expect(searchRow(page, id).locator("[data-library='add']")).toBeFocused();
    }
    expect(apiMock.libraryIds).toEqual([]);
    await twoFrames(page);
    expect(listReads(apiMock.requests)).toBe(readsBefore);
    await expect(page.locator("[data-paper-row]")).toHaveCount(inSearch.length);
    await expect(page.locator("[data-paper-row] [data-library='add']")).toHaveCount(inSearch.length);
    await expect(heading).toHaveText(found);
    // 重新整理：照伺服器（文獻庫已經空了）。
    await page.reload();
    await expect(page.locator("[data-state='library-empty']")).toContainText(text("search:search.empty.library"));
  });

  test("two clicks in the same frame on remove: one DELETE", async ({ page, apiMock }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${QUERY}`);
    await searchRow(page, mine).locator("[data-library='remove']").evaluate((element: HTMLElement) => {
      element.click();
      element.click();
    });
    await expect(searchRow(page, mine).locator("[data-library='add']")).toBeFocused();
    await twoFrames(page);
    expect(apiMock.requests.filter((request) => request.method === "DELETE").map((request) => request.path)).toEqual([`/api/v1/library/${mine}`]);
  });

  test("A logs out and B logs in in the same tab: rows A added show add for B, with no remove", async ({ page, apiMock }) => {
    // 起始狀態（本週新增前 3 篇）裡有甲加入過的；乙的文獻庫是空的。
    const start = filterListRows(ROWS, new URLSearchParams("this_week=true"))!.slice(0, 3);
    const aAdded = start.filter((entry) => entry.personal).map((entry) => entry.row.id);
    expect(aAdded.length).toBeGreaterThan(0);
    expect(libraryOf(apiMock, MEMBERS[1]!.member_id).items).toEqual([]);
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search`);
    await expect(page.locator("[data-paper-row] [data-library='remove']")).toHaveCount(aAdded.length);
    await page.getByRole("button", { name: text("nav:nav.account.trigger") }).click();
    await page.getByRole("menuitem", { name: text("nav:nav.account.logout") }).click();
    await expect(page).toHaveURL(`${member.baseURL}/login`);
    apiMock.loginList = [MEMBERS[1]!];
    await page.getByRole("button", { name: text("login:login.button.idle") }).click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(start.length);
    for (const id of aAdded) {
      await expect(searchRow(page, id).locator("[data-library='add']")).toBeVisible();
    }
    await expect(page.locator("[data-library='remove']")).toHaveCount(0);
    await expect(page.locator("[data-library='added']")).toHaveCount(0);
  });

  test("someone else signs in from another tab: rows A added show add, with no remove", async ({ page, apiMock }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${QUERY}`);
    await expect(searchRow(page, mine).locator("[data-library='remove']")).toBeVisible();
    await page.locator("header").getByRole("link", { name: text("nav:nav.links.library") }).click();
    await expect(rows(page)).toHaveCount(apiMock.libraryIds.length);
    // 另一個分頁換成乙登入（cookie 換了），這個分頁站內換頁。
    apiMock.member = MEMBERS[1]!;
    await page.goBack();
    await expect(page).toHaveURL(`${member.baseURL}/search?${QUERY}`);
    await expect(searchRow(page, mine).locator("[data-library='add']")).toBeVisible();
    await expect(page.locator("[data-library='remove']")).toHaveCount(0);
  });

  test("a remove still on its way when B takes over does not touch B's row (B also has the paper)", async ({ page, apiMock }) => {
    // 甲按了移出、回應還沒回來，另一個分頁換成乙（乙的文獻庫也有這篇）。回應是甲的：乙那一列仍是「● 已加入」。
    libraryOf(apiMock, MEMBERS[1]!.member_id).items = [{ id: mine, addedAt: "2026-11-01T00:00:00.000Z" }];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${QUERY}`);
    const [changeGate, releaseChange] = gate();
    apiMock.libraryChangeGate = changeGate;
    const deleted = page.waitForResponse((response) => response.request().method() === "DELETE");
    await searchRow(page, mine).locator("[data-library='remove']").click();
    await requestsOf(apiMock.requests, "DELETE", "/api/v1/library/");
    apiMock.member = MEMBERS[1]!;
    await page.locator("header").getByRole("link", { name: text("nav:nav.links.library") }).click();
    await expect(rows(page)).toHaveCount(1);
    await page.goBack();
    await expect(page).toHaveURL(`${member.baseURL}/search?${QUERY}`);
    await expect(searchRow(page, mine).locator("[data-library='added']")).toBeVisible();
    // 之後的重讀卡住（看得到快取有沒有被改，不是被重讀蓋掉）。
    const [listGate, releaseList] = gate();
    apiMock.listGate = listGate;
    apiMock.libraryChangeGate = null;
    releaseChange();
    await (await deleted).finished();
    await twoFrames(page);
    await expect(searchRow(page, mine).locator("[data-library='added']")).toBeVisible();
    await expect(searchRow(page, mine).locator("[data-library='add']")).toHaveCount(0);
    // 伺服器那邊移出的是甲的，乙的還在。
    expect(libraryOf(apiMock, MEMBERS[1]!.member_id).items.map((record) => record.id)).toEqual([mine]);
    apiMock.listGate = null;
    releaseList();
  });
});

test.describe("my library page (second version)", () => {
  test("each row: remove + open full text (new tab, no opener) without selecting; no DOI has only remove (decision 88)", async ({ page, apiMock }) => {
    apiMock.libraryIds = [WITH_DOI[0]!, NO_DOI[0]!];
    for (const viewport of [DESKTOP, PHONE]) {
      await openLibrary(page, viewport);
      await expect(rows(page)).toHaveCount(2);
      const withDoi = row(page, WITH_DOI[0]!);
      const link = withDoi.locator("[data-open-full-text]:visible");
      await expect(link).toHaveCount(1);
      await expect(link).toHaveText(new RegExp(`^${text("paper:paper.fullText.open")}`));
      await expect(link).toHaveAttribute("href", ROWS.find((entry) => entry.row.id === WITH_DOI[0])!.row.doi_url!);
      await expect(link).toHaveAttribute("target", "_blank");
      await expect(link).toHaveAttribute("rel", "noopener noreferrer");
      const remove = withDoi.locator("[data-library='remove']:visible");
      await expect(remove).toHaveText(text("library:library.row.remove"));
      await expect(remove).toHaveAccessibleName(fill(text("search:search.row.removeLabel"), { title: title(WITH_DOI[0]!) }));
      // 「移出」在左、「開啟全文」在右（手機在書目下面一行）。
      const removeBox = (await remove.boundingBox())!;
      const linkBox = (await link.boundingBox())!;
      expect(removeBox.x).toBeLessThan(linkBox.x);
      if (viewport === PHONE) {
        const meta = (await withDoi.locator("h3 + div").boundingBox())!;
        expect(linkBox.y).toBeGreaterThan(meta.y);
      }
      const noDoi = row(page, NO_DOI[0]!);
      await expect(noDoi.locator("[data-open-full-text]:visible")).toHaveCount(0);
      await expect(noDoi.locator("[data-library='remove']:visible")).toHaveCount(1);
      // 不用先勾：沒有選取列。
      await expect(page.locator("[data-library='open']")).toHaveCount(0);
    }
  });

  test("the checkbox border is dim until the row is hovered; checked fills it", async ({ page, apiMock }) => {
    const [first] = apiMock.libraryIds;
    await openLibrary(page);
    const box = row(page, first!).locator("label span[aria-hidden='true']").first();
    await expect(box).toHaveCSS("border-top-color", "rgb(142, 136, 123)");
    await row(page, first!).locator("h3").hover();
    await expect(box).toHaveCSS("border-top-color", "rgb(168, 162, 148)");
    await checkbox(page, first!).check();
    await expect(box).toHaveCSS("background-color", "rgb(239, 234, 224)");
  });

  test("selecting: 'N selected' + open full text + cancel replace the count; cancel collapses the notes (decision 89)", async ({ page, apiMock }) => {
    await recordTabs(page, 0);
    apiMock.libraryIds = WITH_DOI.slice(0, 2);
    await openLibrary(page);
    await expect(page.locator("[data-library-count]")).toBeVisible();
    await expect(page.locator("[data-library-actions]")).toHaveCount(0);
    for (const id of apiMock.libraryIds) await checkbox(page, id).check();
    const bar = page.locator("[data-library-actions]");
    await expect(bar.locator("[data-library-selected]")).toHaveText(fill(text("library:library.actions.selected"), { count: 2 }));
    await expect(bar.locator("[data-library='open']")).toHaveText(text("library:library.actions.open"));
    await expect(bar.locator("[data-library='cancel']")).toHaveText(text("library:library.actions.cancel"));
    // 順序：已選 N 篇 → 開啟全文 → 取消。
    const xs = [];
    for (const selector of ["[data-library-selected]", "[data-library='open']", "[data-library='cancel']"]) {
      xs.push((await bar.locator(selector).boundingBox())!.x);
    }
    expect([...xs].sort((a, b) => a - b)).toEqual(xs);
    await expect(page.locator("[data-library-count]")).toBeHidden();
    await bar.locator("[data-library='open']").click();
    await expect(page.locator("[data-library-note='blocked']")).toBeVisible();
    await bar.locator("[data-library='cancel']").click();
    await expect(page.locator("[data-library-note]")).toHaveCount(0);
    await expect(bar).toHaveCount(0);
    await expect(page.locator("[data-library-count]")).toBeVisible();
  });

  test("desktop: the notes sit under the selection bar and stick to the top with it (decision 91)", async ({ page, apiMock }) => {
    await recordTabs(page, 0);
    apiMock.libraryIds = [...WITH_DOI.slice(0, 12), NO_DOI[0]!];
    await openLibrary(page);
    for (const id of [WITH_DOI[0]!, WITH_DOI[1]!, NO_DOI[0]!]) await checkbox(page, id).check();
    await page.locator("[data-library-actions] [data-library='open']").click();
    const notes = page.locator("[data-library-notes]");
    await expect(notes.locator("[data-library-note='blocked']")).toBeVisible();
    await expect(notes.locator("[data-library-note='skipped']")).toHaveText(fill(text("library:library.notes.skipped"), { count: 1 }));
    // 和選取列在同一個黏住的區塊裡、在選取列下面。
    expect(await notes.evaluate((element) => getComputedStyle(element.parentElement!).position)).toBe("sticky");
    const barBox = (await page.locator("[data-library-actions]").boundingBox())!;
    expect((await notes.boundingBox())!.y).toBeGreaterThanOrEqual(barBox.y + barBox.height);
    await page.mouse.wheel(0, 3000);
    await expect.poll(async () => (await page.locator("[data-library-actions]").boundingBox())?.y ?? -1).toBeLessThan(80);
    const after = (await page.locator("[data-library-actions]").boundingBox())!;
    expect((await notes.boundingBox())!.y).toBeGreaterThanOrEqual(after.y + after.height);
    expect((await notes.boundingBox())!.y).toBeLessThan(900);
    // 連結清單最高 300px（min(40vh, 300px)），超過在裡面捲。
    const list = notes.locator("[data-library-note='blocked'] > div").nth(1);
    expect(await list.evaluate((element) => getComputedStyle(element).maxHeight)).toBe("300px");
    expect(await list.evaluate((element) => getComputedStyle(element).overflowY)).toBe("auto");
  });

  test("phone: the dock has 'N selected' left, cancel + open right; the notes open right above them (decision 90)", async ({ page, apiMock }) => {
    await recordTabs(page);
    apiMock.libraryIds = [WITH_DOI[0]!, NO_DOI[0]!, ...WITH_DOI.slice(1, 12)];
    await openLibrary(page, PHONE);
    for (const id of apiMock.libraryIds.slice(0, 3)) await checkbox(page, id).check();
    const dock = page.locator("[data-library-dock]");
    // 手機時寫篇數那一行照留著。
    await expect(page.locator("[data-library-count]")).toBeVisible();
    await expect(dock.locator("[data-library-selected]")).toHaveText(fill(text("library:library.actions.selected"), { count: 3 }));
    const selected = (await dock.locator("[data-library-selected]").boundingBox())!;
    const cancel = (await dock.locator("[data-library='cancel']").boundingBox())!;
    const open = (await dock.locator("[data-library='open']").boundingBox())!;
    expect(selected.x).toBeLessThan(cancel.x);
    expect(cancel.x).toBeLessThan(open.x);
    // 沒有內容時說明不佔位（審查 R11 後：仍留在無障礙樹裡，只是 1px、絕對定位）。
    await expect(dock.locator("[data-library-notes]")).toHaveText("");
    expect((await dock.locator("[data-library-notes]").boundingBox())!.height).toBeLessThanOrEqual(1);
    await dock.locator("[data-library='open']").click();
    const notes = dock.locator("[data-library-notes]");
    await expect(notes.locator("[data-library-note='mobile']")).toContainText(text("library:library.notes.mobile"));
    await expect(notes.locator("[data-library-note='skipped']")).toHaveText(fill(text("library:library.notes.skipped"), { count: 1 }));
    const notesBox = (await notes.boundingBox())!;
    const selectedAfter = (await dock.locator("[data-library-selected]").boundingBox())!;
    expect(notesBox.y + notesBox.height).toBeLessThanOrEqual(selectedAfter.y);
    const dockBox = (await dock.boundingBox())!;
    expect(dockBox.y + dockBox.height).toBeCloseTo(PHONE.height, 0);
    expect(await notes.locator("[data-library-note='mobile'] > div").nth(1).evaluate((element) => getComputedStyle(element).overflowY)).toBe("auto");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(PHONE.width);
  });

  test("tap targets on each row: remove and open full text are at least 44px high", async ({ page, apiMock }) => {
    apiMock.libraryIds = WITH_DOI.slice(0, 1);
    for (const viewport of [DESKTOP, PHONE]) {
      await openLibrary(page, viewport);
      for (const selector of ["[data-library='remove']:visible", "[data-open-full-text]:visible"]) {
        const box = (await rows(page).first().locator(selector).boundingBox())!;
        expect(box.height, selector).toBeGreaterThanOrEqual(44);
        expect(box.width, selector).toBeGreaterThanOrEqual(44);
      }
    }
  });
});

// PR #114 第 1 輪審查的修正（R1～R5、R8）。
test.describe("search rows: remove (review fixes)", () => {
  const QUERY = "topic=it_project_management";
  const shown = filterListRows(ROWS, new URLSearchParams(QUERY))!;
  const mine = shown.find((entry) => entry.personal)!.row.id;
  const notMine = shown.filter((entry) => !entry.personal).map((entry) => entry.row.id);
  const searchRow = (page: Page, id: string) => page.locator(`[data-paper-row="${id}"]`);
  const heading = (page: Page) => page.locator("main").getByRole("heading", { level: 2 });
  const found = (count: number) => fill(text("search:search.results.found"), { count });

  /** 這幾篇的加入／移出請求各自等放行才交給替身（每一篇一個閘門；回傳放行的函式）。 */
  async function holdChanges(page: Page, ids: string[]): Promise<Record<string, () => void>> {
    const holds: Record<string, Promise<void>> = {};
    const releases: Record<string, () => void> = {};
    for (const id of ids) [holds[id], releases[id]] = gate();
    await page.route("**/api/v1/library/*", async (route) => {
      const id = decodeURIComponent(new URL(route.request().url()).pathname.split("/").pop()!);
      await holds[id];
      await route.fallback();
    });
    return releases;
  }

  test("scope 'my library', 30 papers: load more, remove one, switch the scope and back: found 29 and the row is gone (R1)", async ({ page, apiMock }) => {
    apiMock.libraryIds = ROWS.map((entry) => entry.row.id);
    const all = filterListRows(ROWS, new URLSearchParams("scope=personal"), apiMock.libraryIds)!.map((entry) => entry.row.id);
    expect(all).toHaveLength(30);
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(heading(page)).toHaveText(found(30));
    await page.getByRole("button", { name: fill(text("search:search.results.more"), { count: 20 }) }).click();
    await expect(page.locator("[data-paper-row]")).toHaveCount(30);
    const removed = all[0]!;
    await searchRow(page, removed).locator("[data-library='remove']").click();
    await expect(searchRow(page, removed).locator("[data-library='add']")).toBeFocused();
    expect(apiMock.libraryIds).toHaveLength(29);
    await expect(heading(page)).toHaveText(found(30));
    await page.getByRole("button", { name: text("search:search.scope.tracked") }).click();
    await expect(page).not.toHaveURL(/scope=personal/);
    // 起始狀態（本週新增前 3 篇）讀完才換回去。
    await expect(page.locator("[data-paper-row]")).toHaveCount(3);
    await expect(page.locator("[data-state='loading']")).toHaveCount(0);
    // 換回來：等伺服器的這段期間是骨架，不先秀舊的那一份（移出的列還在、篇數 30）。
    const [listGate, releaseList] = gate();
    apiMock.listGate = listGate;
    await page.getByRole("button", { name: text("search:search.scope.personal"), exact: true }).click();
    await expect(page).toHaveURL(/scope=personal/);
    await expect(page.getByRole("button", { name: text("search:search.scope.personal"), exact: true })).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator("[data-state='loading']")).toBeVisible();
    await expect(page.locator("[data-paper-row]")).toHaveCount(0);
    apiMock.listGate = null;
    releaseList();
    await expect(heading(page)).toHaveText(found(29));
    await expect(page.locator("[data-paper-row]")).toHaveCount(20);
    await expect(page.locator("[data-shown]")).toHaveText(fill(text("search:search.results.shown"), { shown: 20, total: 29 }));
    await expect(searchRow(page, removed)).toHaveCount(0);
    await page.getByRole("button", { name: fill(text("search:search.results.more"), { count: 20 }) }).click();
    await expect(page.locator("[data-paper-row]")).toHaveCount(29);
    await expect(heading(page)).toHaveText(found(29));
    await expect(searchRow(page, removed)).toHaveCount(0);
  });

  test("scope 'my library' with nothing removed: back to the tab once the list is stale follows the server (another tab added one; then B took over) (R2)", async ({ page, apiMock }) => {
    await page.clock.install();
    const before = [...apiMock.libraryIds];
    const extra = ROWS.find((entry) => !before.includes(entry.row.id))!.row.id;
    const bOnly = ROWS.find((entry) => entry.row.id !== extra && !before.includes(entry.row.id))!.row.id;
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(before.length);
    // 另一個分頁：同一位加了一篇。
    apiMock.libraryIds = [extra, ...before];
    await page.clock.fastForward("06:00");
    await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
    await expect(page.locator("[data-paper-row]")).toHaveCount(before.length + 1);
    await expect(searchRow(page, extra)).toBeVisible();
    await expect(heading(page)).toHaveText(found(before.length + 1));
    // 另一個分頁：換成乙登入（乙只有自己的一篇）。
    libraryOf(apiMock, MEMBERS[1]!.member_id).items = [{ id: bOnly, addedAt: "2026-11-01T00:00:00.000Z" }];
    apiMock.member = MEMBERS[1]!;
    await page.clock.fastForward("06:00");
    await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
    await expect(page.locator("[data-paper-row]")).toHaveCount(1);
    await expect(searchRow(page, bOnly)).toBeVisible();
    for (const id of [extra, ...before]) await expect(searchRow(page, id)).toHaveCount(0);
  });

  test("scope 'my library' holding a removed row: back to the tab after it went stale does not read again, the row stays (R2)", async ({ page, apiMock }) => {
    await page.clock.install();
    const before = [...apiMock.libraryIds];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(before.length);
    await searchRow(page, before[0]!).locator("[data-library='remove']").click();
    await expect(searchRow(page, before[0]!).locator("[data-library='add']")).toBeFocused();
    const reads = listReads(apiMock.requests);
    await page.clock.fastForward("06:00");
    await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
    await twoFrames(page);
    expect(listReads(apiMock.requests)).toBe(reads);
    await expect(page.locator("[data-paper-row]")).toHaveCount(before.length);
    await expect(heading(page)).toHaveText(found(before.length));
  });

  test("scope 'my library', A and B each with their own paper: switching members never shows A's rows to B; a remove on A's screen after the cookie changed is refused with 409 and the page switches to B (R3)", async ({ page, apiMock }) => {
    const aOnly = ROWS[0]!.row.id;
    const bOnly = ROWS[1]!.row.id;
    apiMock.libraryIds = [aOnly];
    libraryOf(apiMock, MEMBERS[1]!.member_id).items = [{ id: bOnly, addedAt: "2026-11-01T00:00:00.000Z" }];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(1);
    await expect(searchRow(page, aOnly)).toBeVisible();
    // 另一個分頁換成乙（cookie 換了），這個分頁還是甲的畫面；這時按甲那一列的「移出」：請求帶的預期成員是甲，替身回 409（ADR-0055）。
    apiMock.member = MEMBERS[1]!;
    const write = page
      .waitForResponse((response) => response.request().method() === "DELETE" && new URL(response.url()).pathname.startsWith("/api/v1/library/"))
      .then(async (response) => ({ expected: (await response.request().allHeaders())[EXPECTED_MEMBER_HEADER], status: response.status(), body: (await response.json()) as { code?: string } }));
    await searchRow(page, aOnly).locator("[data-library='remove']").click();
    const [sent] = await requestsOf(apiMock.requests, "DELETE", "/api/v1/library/");
    expect(sent!.path).toBe(`/api/v1/library/${aOnly}`);
    const answer = await write;
    expect(answer.expected).toBe(MEMBERS[0]!.member_id);
    expect(answer.status).toBe(409);
    expect(answer.body.code).toBe("member_changed");
    // 甲、乙的文獻庫都沒被動到。
    expect(libraryOf(apiMock, MEMBERS[0]!.member_id).items.map((record) => record.id)).toEqual([aOnly]);
    expect(libraryOf(apiMock, MEMBERS[1]!.member_id).items.map((record) => record.id)).toEqual([bOnly]);
    // 畫面換成乙的清單：乙的那一列看得到，甲的那一列看不到。
    await expect(searchRow(page, bOnly)).toBeVisible();
    await expect(searchRow(page, aOnly)).toHaveCount(0);
    await expect(page.locator("[data-paper-row]")).toHaveCount(1);
    // 換人已在 409 那一步處理；站內換頁再回來仍是乙的清單（乙的清單已經讀過，清單請求還擋著時看到的就是它、沒有甲的列）。
    await page.locator("header").getByRole("link", { name: text("nav:nav.links.library") }).click();
    await expect(rows(page)).toHaveCount(1);
    await expect(row(page, bOnly)).toBeVisible();
    const [listGate, releaseList] = gate();
    apiMock.listGate = listGate;
    await page.goBack();
    await expect(page).toHaveURL(`${member.baseURL}/search?scope=personal`);
    await expect(searchRow(page, bOnly)).toBeVisible();
    await expect(searchRow(page, aOnly)).toHaveCount(0);
    await expect(page.locator("[data-paper-row]")).toHaveCount(1);
    apiMock.listGate = null;
    releaseList();
    await expect(searchRow(page, bOnly)).toBeVisible();
    await expect(page.locator("[data-paper-row]")).toHaveCount(1);
    // 同一個分頁：乙登出、甲登入，再回到範圍我的文獻庫：只有甲的。
    await page.getByRole("button", { name: text("nav:nav.account.trigger") }).click();
    await page.getByRole("menuitem", { name: text("nav:nav.account.logout") }).click();
    await expect(page).toHaveURL(`${member.baseURL}/login`);
    apiMock.loginList = [MEMBERS[0]!];
    await page.getByRole("button", { name: text("login:login.button.idle") }).click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    const [listGate2, releaseList2] = gate();
    apiMock.listGate = listGate2;
    await page.getByRole("button", { name: text("search:search.scope.personal"), exact: true }).click();
    await expect(page).toHaveURL(/scope=personal/);
    await expect(searchRow(page, bOnly)).toHaveCount(0);
    apiMock.listGate = null;
    releaseList2();
    await expect(searchRow(page, aOnly)).toBeVisible();
    await expect(page.locator("[data-paper-row]")).toHaveCount(1);
    await expect(searchRow(page, bOnly)).toHaveCount(0);
  });

  test("remove shows add at once while the request is on its way; success keeps it; failure brings 'added' + remove back (R4, decision 87)", async ({ page, apiMock }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${QUERY}`);
    const current = searchRow(page, mine);
    const add = current.locator("[data-library='add']");
    // 等待中 → 成功。
    let [changeGate, releaseChange] = gate();
    apiMock.libraryChangeGate = changeGate;
    await current.locator("[data-library='remove']").click();
    await requestsOf(apiMock.requests, "DELETE", "/api/v1/library/");
    // 回應還沒回來：已經是「加入我的文獻庫」（停用，防重複送出），沒有「● 已加入」「移出」。
    await expect(add).toBeVisible();
    await expect(add).toBeDisabled();
    await expect(current.locator("[data-library='added']")).toHaveCount(0);
    await expect(current.locator("[data-library='remove']")).toHaveCount(0);
    apiMock.libraryChangeGate = null;
    releaseChange();
    await expect(add).toBeEnabled();
    await expect(add).toBeFocused();
    expect(apiMock.libraryIds).not.toContain(mine);
    // 加回去，再移出一次：等待中 → 失敗。
    await add.click();
    await expect(current.locator("[data-library='remove']")).toBeFocused();
    [changeGate, releaseChange] = gate();
    apiMock.libraryChangeGate = changeGate;
    apiMock.libraryChange = "failed";
    const deletes = apiMock.requests.filter((request) => request.method === "DELETE").length;
    await current.locator("[data-library='remove']").click();
    await expect.poll(() => apiMock.requests.filter((request) => request.method === "DELETE").length).toBe(deletes + 1);
    await expect(add).toBeDisabled();
    await expect(current.locator("[data-library='added']")).toHaveCount(0);
    apiMock.libraryChangeGate = null;
    releaseChange();
    await expect(current.locator("[data-library='added']")).toBeVisible();
    await expect(current.locator("[data-library='remove']")).toBeFocused();
    await expect(add).toHaveCount(0);
    expect(apiMock.libraryIds).toContain(mine);
  });

  test("an answer coming back after the user moved on does not pull focus back to that row (R5)", async ({ page, apiMock }) => {
    const [first] = notMine;
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${QUERY}`);
    const release = await holdChanges(page, [first!]);
    await searchRow(page, first!).locator("[data-library='add']").focus();
    await page.keyboard.press("Enter");
    // 使用者已經移到別列。
    const elsewhere = searchRow(page, notMine[1]!).locator("h3 a");
    await elsewhere.focus();
    release[first!]!();
    await expect(searchRow(page, first!).locator("[data-library='remove']")).toBeVisible();
    await twoFrames(page);
    await expect(elsewhere).toBeFocused();
    expect(apiMock.libraryIds).toContain(first);
  });

  test("two rows pressed one after the other, answers in either order: focus ends on the row pressed last (R5)", async ({ page }) => {
    const [first, second] = notMine;
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${QUERY}`);
    const release = await holdChanges(page, [first!, second!]);
    await searchRow(page, first!).locator("[data-library='add']").focus();
    await page.keyboard.press("Enter");
    await searchRow(page, second!).locator("[data-library='add']").focus();
    await page.keyboard.press("Enter");
    // 先按的先回來：不搶焦點。
    release[first!]!();
    await expect(searchRow(page, first!).locator("[data-library='remove']")).toBeVisible();
    await twoFrames(page);
    await expect(searchRow(page, first!).locator("[data-library='remove']")).not.toBeFocused();
    release[second!]!();
    await expect(searchRow(page, second!).locator("[data-library='remove']")).toBeFocused();
  });

  test("two rows pressed one after the other, the later answers first: focus stays on it when the earlier answer comes back (R5)", async ({ page }) => {
    const [first, second] = notMine;
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${QUERY}`);
    const release = await holdChanges(page, [first!, second!]);
    await searchRow(page, first!).locator("[data-library='add']").focus();
    await page.keyboard.press("Enter");
    await searchRow(page, second!).locator("[data-library='add']").focus();
    await page.keyboard.press("Enter");
    release[second!]!();
    await expect(searchRow(page, second!).locator("[data-library='remove']")).toBeFocused();
    release[first!]!();
    await expect(searchRow(page, first!).locator("[data-library='remove']")).toBeVisible();
    await twoFrames(page);
    await expect(searchRow(page, second!).locator("[data-library='remove']")).toBeFocused();
  });
});

test.describe("search rows: 'no abstract' on a phone (R7)", () => {
  // 設計稿 search-no-box.html 390：「期刊 · 年份」<br>「○ 沒有摘要」：沒有摘要自己一行、前面沒有「·」；900px 以上接在同一行、前面「·」。
  const QUERY = "topic=it_project_management";
  const entry = filterListRows(ROWS, new URLSearchParams(QUERY))!.find((candidate) => !candidate.row.has_abstract)!;

  for (const [name, viewport, ownLine] of [
    ["phone", PHONE, true],
    ["desktop", DESKTOP, false],
  ] as const) {
    test(`${name}: 'no abstract' ${ownLine ? "on its own line with no '·'" : "after '·' on the same line"}`, async ({ page }) => {
      expect(entry.row.journal).not.toBeNull();
      expect(entry.row.publication_year).not.toBeNull();
      await page.setViewportSize(viewport);
      await page.goto(`${member.baseURL}/search?${QUERY}`);
      const meta = page.locator(`[data-paper-row="${entry.row.id}"] h3 + div`);
      const year = meta.getByText(String(entry.row.publication_year), { exact: true });
      const label = meta.getByText(text("paper:paper.biblio.noAbstract"), { exact: true });
      await expect(label).toBeVisible();
      const yearBox = (await year.boundingBox())!;
      const labelBox = (await label.boundingBox())!;
      const metaBox = (await meta.boundingBox())!;
      const separators = meta.locator("span:text-is('·'):visible");
      if (ownLine) {
        expect(labelBox.y).toBeGreaterThanOrEqual(yearBox.y + yearBox.height - 1);
        // 那一行從行首開始（前面只有 6px 的圓圈＋7px 間距）。
        expect(labelBox.x - metaBox.x).toBeLessThanOrEqual(14);
        await expect(separators).toHaveCount(1);
      } else {
        expect(Math.abs(labelBox.y - yearBox.y)).toBeLessThan(4);
        await expect(separators).toHaveCount(2);
      }
    });
  }
});

test.describe("my library page on a phone: the selection dock (R9, R11)", () => {
  test("a short library: the dock sits at the bottom of the window, not right under the list (R9)", async ({ page, apiMock }) => {
    apiMock.libraryIds = WITH_DOI.slice(0, 1);
    await openLibrary(page, PHONE);
    await checkbox(page, apiMock.libraryIds[0]!).check();
    const dock = page.locator("[data-library-dock]");
    await expect(dock).toBeVisible();
    const box = (await dock.boundingBox())!;
    // Story 3.15 起視窗最下面是全站頁尾（899px 以下高 101px，page-frame.spec.ts）：選取列貼在頁尾上面。
    expect(box.y + box.height).toBeCloseTo(PHONE.height - PHONE_FOOTER_HEIGHT, 0);
    // 清單在它上面，沒有被蓋住。
    const last = (await rows(page).last().boundingBox())!;
    expect(last.y + last.height).toBeLessThanOrEqual(box.y);
    expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThanOrEqual(PHONE.height);
  });

  test("the results and notes region is in the accessibility tree before anything is written, and takes no room (R11)", async ({ page, apiMock }) => {
    await recordTabs(page);
    apiMock.libraryIds = WITH_DOI.slice(0, 2);
    await openLibrary(page, PHONE);
    await checkbox(page, apiMock.libraryIds[0]!).check();
    const dock = page.locator("[data-library-dock]");
    // getByRole 不算 display:none 的元素：找得到就表示螢幕閱讀器看得到這個即時播報區。
    const region = dock.getByRole("status");
    await expect(region).toHaveCount(1);
    await expect(region).toHaveText("");
    const empty = (await region.boundingBox())!;
    expect(empty.height).toBeLessThanOrEqual(1);
    // 不佔一行：「已選 N 篇」就在選取列的第一行（上框線 1＋上內距 12）。
    const top = (await dock.boundingBox())!.y;
    const selected = (await dock.locator("[data-library-selected]").boundingBox())!;
    const open = (await dock.locator("[data-library='open']").boundingBox())!;
    expect(Math.min(selected.y, open.y) - top).toBeLessThanOrEqual(13);
    await openButton(page).click();
    await expect(region).toContainText(text("library:library.notes.mobile"));
  });
});

test.describe("my library page: the selection count after opening (R8)", () => {
  // 第 221、265 行的既有斷言改字串後不再比篇數（決定第 89 項）；這兩條把篇數補回來，不改既有測試。
  test("after opening N tabs and the 'opened N' flash, the bar still says N selected", async ({ page, apiMock }) => {
    await recordTabs(page);
    apiMock.libraryIds = WITH_DOI.slice(0, 3);
    await openLibrary(page);
    for (const id of apiMock.libraryIds) await checkbox(page, id).check();
    const bar = page.locator("[data-library-actions]");
    const selected = fill(text("library:library.actions.selected"), { count: apiMock.libraryIds.length });
    await expect(bar.locator("[data-library-selected]")).toHaveText(selected);
    await openButton(page).click();
    await expect(openButton(page)).toHaveText(fill(text("library:library.actions.opened"), { count: 3 }));
    await expect(bar.locator("[data-library-selected]")).toHaveText(selected);
    await expect(openButton(page)).toHaveText(text("library:library.actions.open"), { timeout: 4000 });
    await expect(bar.locator("[data-library-selected]")).toHaveText(selected);
    for (const id of apiMock.libraryIds) await expect(checkbox(page, id)).toBeChecked();
  });

  test("when the browser blocks the tabs, the bar still says N selected", async ({ page, apiMock }) => {
    await recordTabs(page, 0);
    apiMock.libraryIds = WITH_DOI.slice(0, 2);
    await openLibrary(page);
    for (const id of apiMock.libraryIds) await checkbox(page, id).check();
    await openButton(page).click();
    await expect(page.locator("[data-library-note='blocked']")).toBeVisible();
    await expect(page.locator("[data-library-actions] [data-library-selected]")).toHaveText(
      fill(text("library:library.actions.selected"), { count: apiMock.libraryIds.length }),
    );
    for (const id of apiMock.libraryIds) await expect(checkbox(page, id)).toBeChecked();
  });
});

for (const width of [1440, 1000, 500]) {
  test(`library screenshot at ${width}px`, async ({ page, browserName }) => {
    test.skip(browserName !== "chromium", "screenshots are taken in chromium only");
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${member.baseURL}/library`);
    await expect(rows(page).first()).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `${SCREENSHOT_DIR}library-${width}.png`, fullPage: true });
  });
}

// PR #114 第 2 輪修正（確認審查 C1；Gemini 參考審查第 1 條＝G1）。
test.describe("search rows: remove (review fixes, round 2)", () => {
  const searchRow = (page: Page, id: string) => page.locator(`[data-paper-row="${id}"]`);
  const heading = (page: Page) => page.locator("main").getByRole("heading", { level: 2 });
  const found = (count: number) => fill(text("search:search.results.found"), { count });
  const personalScope = (page: Page) => page.getByRole("button", { name: text("search:search.scope.personal"), exact: true });

  test("scope 'my library': remove one, go to my library page, back to search by the nav, switch to 'my library': the skeleton until the server answers, not the old list (C1)", async ({ page, apiMock }) => {
    apiMock.libraryIds = ROWS.map((entry) => entry.row.id);
    const all = filterListRows(ROWS, new URLSearchParams("scope=personal"), apiMock.libraryIds)!.map((entry) => entry.row.id);
    expect(all).toHaveLength(30);
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(heading(page)).toHaveText(found(30));
    await page.getByRole("button", { name: fill(text("search:search.results.more"), { count: 20 }) }).click();
    await expect(page.locator("[data-paper-row]")).toHaveCount(30);
    const removed = all[0]!;
    await searchRow(page, removed).locator("[data-library='remove']").click();
    await expect(searchRow(page, removed).locator("[data-library='add']")).toBeFocused();
    expect(apiMock.libraryIds).toHaveLength(29);
    // 到我的文獻庫，再用導覽列回找論文（條件是預設的起始狀態）。
    await page.locator("header").getByRole("link", { name: text("nav:nav.links.library") }).click();
    await expect(rows(page)).toHaveCount(20);
    await page.locator("header").getByRole("link", { name: text("nav:nav.links.search") }).click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(3);
    await expect(page.locator("[data-state='loading']")).toHaveCount(0);
    // 換到範圍我的文獻庫：伺服器回來前是骨架，不出現舊的那一份（找到 30 篇、移出的那一列）。
    const [listGate, releaseList] = gate();
    apiMock.listGate = listGate;
    await personalScope(page).click();
    await expect(page).toHaveURL(/scope=personal/);
    await expect(personalScope(page)).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator("[data-state='loading']")).toBeVisible();
    await expect(page.locator("[data-paper-row]")).toHaveCount(0);
    await expect(page.getByText(found(30))).toHaveCount(0);
    await expect(searchRow(page, removed)).toHaveCount(0);
    apiMock.listGate = null;
    releaseList();
    await expect(heading(page)).toHaveText(found(29));
    await expect(page.locator("[data-paper-row]")).toHaveCount(20);
    await expect(searchRow(page, removed)).toHaveCount(0);
  });

  test("scope 'my library' holding a removed row; B signs in from another tab; back to this tab: B's list, not A's, and A's rows cannot be added to B's library (G1)", async ({ page, apiMock }) => {
    const aRows = [ROWS[0]!.row.id, ROWS[2]!.row.id];
    const bOnly = ROWS[1]!.row.id;
    apiMock.libraryIds = [...aRows];
    libraryOf(apiMock, MEMBERS[1]!.member_id).items = [{ id: bOnly, addedAt: "2026-11-01T00:00:00.000Z" }];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(2);
    await searchRow(page, aRows[0]!).locator("[data-library='remove']").click();
    await expect(searchRow(page, aRows[0]!).locator("[data-library='add']")).toBeFocused();
    expect(libraryOf(apiMock, MEMBERS[0]!.member_id).items.map((record) => record.id)).toEqual([aRows[1]]);
    // 另一個分頁換成乙登入，再切回這個分頁。
    apiMock.member = MEMBERS[1]!;
    await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
    await expect(searchRow(page, bOnly)).toBeVisible();
    await expect(page.locator("[data-paper-row]")).toHaveCount(1);
    for (const id of aRows) await expect(searchRow(page, id)).toHaveCount(0);
    await expect(heading(page)).toHaveText(found(1));
    // 乙的文獻庫沒被加進甲移出的那一篇；沒有送出加入。
    expect(apiMock.requests.filter((request) => request.method === "PUT")).toHaveLength(0);
    expect(libraryOf(apiMock, MEMBERS[1]!.member_id).items.map((record) => record.id)).toEqual([bOnly]);
  });

  test("scope 'my library' holding a removed row; still the same member when back to this tab: the row stays, the list is not read again (G1)", async ({ page, apiMock }) => {
    const before = [...apiMock.libraryIds];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(before.length);
    await searchRow(page, before[0]!).locator("[data-library='remove']").click();
    await expect(searchRow(page, before[0]!).locator("[data-library='add']")).toBeFocused();
    const reads = listReads(apiMock.requests);
    const meAsked = apiMock.requests.filter((request) => request.path === "/api/v1/me").length;
    await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
    // 問過一次「我是誰」（同一位），清單不重讀，暫留列還在。
    await expect.poll(() => apiMock.requests.filter((request) => request.path === "/api/v1/me").length).toBe(meAsked + 1);
    await twoFrames(page);
    expect(listReads(apiMock.requests)).toBe(reads);
    await expect(page.locator("[data-paper-row]")).toHaveCount(before.length);
    await expect(searchRow(page, before[0]!).locator("[data-library='add']")).toBeEnabled();
    await expect(heading(page)).toHaveText(found(before.length));
  });

  test("scope 'my library' holding a removed row: the tab became visible while the window was not selected, then the window is clicked: 'who am I' is asked again (epic-3l C N2)", async ({ page, apiMock }) => {
    const before = [...apiMock.libraryIds];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(before.length);
    await searchRow(page, before[0]!).locator("[data-library='remove']").click();
    await expect(searchRow(page, before[0]!).locator("[data-library='add']")).toBeFocused();
    const meAsked = () => apiMock.requests.filter((request) => request.path === "/api/v1/me").length;
    const asked = meAsked();
    // 蓋住它的視窗移開了：分頁看得見，但視窗沒有被選取（document.hasFocus() 是 false）。
    await page.evaluate(() => {
      document.hasFocus = () => false;
    });
    const firstAnswer = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/me");
    await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
    await firstAnswer;
    await expect.poll(meAsked).toBe(asked + 1);
    // 那一次的確認收尾（同一位，什麼都不動）之後，才點回這個視窗。
    await twoFrames(page);
    await page.evaluate(() => {
      document.hasFocus = () => true;
      window.dispatchEvent(new Event("focus"));
    });
    // 修之前：focus 被當成同一次切回，不再問。
    await expect.poll(meAsked).toBe(asked + 2);
    await expect(searchRow(page, before[0]!).locator("[data-library='add']")).toBeEnabled();
  });
});

// PR #114 第 3 輪修正（確認審查 D1、D2）：有暫留列、切回視窗確認登入的人時，上一位的清單不能寫進新登入的人的文獻庫；
// 確認時已經沒有人要帶「登入過期」。
test.describe("search rows: remove (review fixes, round 3)", () => {
  const searchRow = (page: Page, id: string) => page.locator(`[data-paper-row="${id}"]`);
  const heading = (page: Page) => page.locator("main").getByRole("heading", { level: 2 });
  const found = (count: number) => fill(text("search:search.results.found"), { count });
  const writes = (requests: ApiRequest[]) => requests.filter((request) => request.method !== "GET" && request.path.startsWith("/api/v1/library/"));
  const meAsks = (requests: ApiRequest[]) => requests.filter((request) => request.path === "/api/v1/me").length;

  /** 甲：範圍我的文獻庫有 held（移出暫留）與 shared 兩列；乙的文獻庫有 shared（甲也有的那一篇）與 bOnly。 */
  async function holdAsA(page: Page, apiMock: ApiMockState) {
    const held = ROWS[0]!.row.id;
    const shared = ROWS[2]!.row.id;
    const bOnly = ROWS[1]!.row.id;
    apiMock.libraryIds = [held, shared];
    libraryOf(apiMock, MEMBERS[1]!.member_id).items = [
      { id: shared, addedAt: "2026-11-01T00:00:00.000Z" },
      { id: bOnly, addedAt: "2026-11-02T00:00:00.000Z" },
    ];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(2);
    await searchRow(page, held).locator("[data-library='remove']").click();
    await expect(searchRow(page, held).locator("[data-library='add']")).toBeFocused();
    expect(libraryOf(apiMock, MEMBERS[0]!.member_id).items.map((record) => record.id)).toEqual([shared]);
    const before = writes(apiMock.requests).length;
    return { held, shared, bOnly, before };
  }

  /** 甲的清單上還在的按鈕都按一下（不在、停用的不算；和滑鼠點一樣是 click）。 */
  async function pressOldButtons(page: Page, held: string, shared: string) {
    await page.evaluate(
      ([heldId, sharedId]) => {
        document.querySelector<HTMLButtonElement>(`[data-paper-row="${heldId}"] [data-library='add']`)?.click();
        document.querySelector<HTMLButtonElement>(`[data-paper-row="${sharedId}"] [data-library='remove']`)?.click();
      },
      [held, shared],
    );
  }

  test("B signs in from another tab; back to this tab; after /me answers B and before the page asks again: A's buttons send nothing, B's library is unchanged (D1)", async ({ page, apiMock }) => {
    const { held, shared, bOnly, before } = await holdAsA(page, apiMock);
    // 切回之後第 1 次「我是誰」照常回答（乙），之後的（重跑登入檢查）先卡住。
    const asked = meAsks(apiMock.requests);
    const [laterMe, releaseLaterMe] = gate();
    await page.route("**/api/v1/me", async (route) => {
      if (meAsks(apiMock.requests) > asked) await laterMe;
      await route.fallback();
    });
    apiMock.member = MEMBERS[1]!;
    const firstAnswer = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/me");
    await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
    await firstAnswer;
    await expect.poll(() => meAsks(apiMock.requests)).toBe(asked + 1);
    await pressOldButtons(page, held, shared);
    // 甲的清單已經不在畫面上（換成骨架或乙的清單；乙沒有暫留的那一篇），不等第 2 次「我是誰」。
    await expect(searchRow(page, held)).toHaveCount(0);
    releaseLaterMe();
    await expect(searchRow(page, bOnly)).toBeVisible();
    await expect(heading(page)).toHaveText(found(2));
    await twoFrames(page);
    expect(writes(apiMock.requests)).toHaveLength(before);
    expect(libraryOf(apiMock, MEMBERS[1]!.member_id).items.map((record) => record.id)).toEqual([shared, bOnly]);
  });

  test("B signs in from another tab; back to this tab; pressing A's buttons while /me is still on the way: nothing is sent, B's library is unchanged (D1)", async ({ page, apiMock }) => {
    const { held, shared, bOnly, before } = await holdAsA(page, apiMock);
    const asked = meAsks(apiMock.requests);
    const [meGate, releaseMe] = gate();
    apiMock.meGate = meGate;
    apiMock.member = MEMBERS[1]!;
    await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
    await expect.poll(() => meAsks(apiMock.requests)).toBe(asked + 1);
    // 「我是誰」還在路上：甲的按鈕看起來還能按，按了先等確認。
    await searchRow(page, held).locator("[data-library='add']").click();
    await searchRow(page, shared).locator("[data-library='remove']").click();
    await twoFrames(page);
    expect(writes(apiMock.requests)).toHaveLength(before);
    apiMock.meGate = null;
    releaseMe();
    await expect(searchRow(page, bOnly)).toBeVisible();
    await expect(heading(page)).toHaveText(found(2));
    await expect(searchRow(page, held)).toHaveCount(0);
    await twoFrames(page);
    expect(writes(apiMock.requests)).toHaveLength(before);
    expect(libraryOf(apiMock, MEMBERS[1]!.member_id).items.map((record) => record.id)).toEqual([shared, bOnly]);
  });

  test("same member, pressing 'add' on the held row while /me is still on the way: sent after the answer, the row is added back (D1)", async ({ page, apiMock }) => {
    const { held, shared, before } = await holdAsA(page, apiMock);
    const asked = meAsks(apiMock.requests);
    const [meGate, releaseMe] = gate();
    apiMock.meGate = meGate;
    await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
    await expect.poll(() => meAsks(apiMock.requests)).toBe(asked + 1);
    await searchRow(page, held).locator("[data-library='add']").click();
    await twoFrames(page);
    expect(writes(apiMock.requests)).toHaveLength(before);
    apiMock.meGate = null;
    releaseMe();
    await expect(searchRow(page, held).locator("[data-library='added']")).toBeVisible();
    expect(writes(apiMock.requests)).toHaveLength(before + 1);
    expect(libraryOf(apiMock, MEMBERS[0]!.member_id).items.map((record) => record.id).sort()).toEqual([held, shared].sort());
  });

  test("signed out from another tab; back to this tab: the login page says the sign-in expired (D2)", async ({ page, apiMock }) => {
    await holdAsA(page, apiMock);
    apiMock.member = null;
    await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
    await expect(page).toHaveURL(/\/login\?/);
    const url = new URL(page.url());
    expect(url.searchParams.get("next")).toBe("/search?scope=personal");
    expect(url.searchParams.get("expired")).toBe("true");
    await expect(page.locator("[data-login-note='expired']")).toBeVisible();
  });
});

// epic-3h PR 3（ADR-0055）：另一個分頁換成乙登入之後，甲的分頁在發現換人之前按的加入、移出、復原帶著「這個分頁以為的成員」（甲），
// 後端（替身）和現在登入的乙不同就回 409、什麼都不寫；畫面照「發現換人」換成乙的資料。epic-3g 審查的四個情境（Q8、找論文的加入、
// 論文頁的加入與移出、我的文獻庫的復原 P3-2）與「我是誰」問不到（L2）各一條；預期都從假資料算或寫死。
test.describe("writes from a tab that still shows the previous member (ADR-0055)", () => {
  const A = MEMBERS[0]!;
  const B = MEMBERS[1]!;
  const searchRow = (page: Page, id: string) => page.locator(`[data-paper-row="${id}"]`);
  const heading = (page: Page) => page.locator("main").getByRole("heading", { level: 2 });
  const found = (count: number) => fill(text("search:search.results.found"), { count });
  const idsOf = (state: ApiMockState, memberId: string) => libraryOf(state, memberId).items.map((record) => record.id);

  /** 下一個文獻庫寫入（method）送出時帶的標頭與替身的回應狀態。 */
  function nextWrite(page: Page, method: string) {
    return page
      .waitForResponse((response) => response.request().method() === method && new URL(response.url()).pathname.startsWith("/api/v1/library/"))
      .then(async (response) => ({ expected: (await response.request().allHeaders())[EXPECTED_MEMBER_HEADER], status: response.status() }));
  }

  test("Q8: no held rows, scope 'my library', A and B both have the paper; B signs in elsewhere and A presses remove at once: B's library is unchanged, the page becomes B's", async ({ page, apiMock: state }) => {
    const aOnly = ROWS[0]!.row.id;
    const bOnly = ROWS[1]!.row.id;
    const shared = ROWS[2]!.row.id;
    state.libraryIds = [aOnly, shared];
    libraryOf(state, B.member_id).items = [
      { id: shared, addedAt: "2026-11-01T00:00:00.000Z" },
      { id: bOnly, addedAt: "2026-11-02T00:00:00.000Z" },
    ];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(2);
    state.member = B;
    await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
    const write = nextWrite(page, "DELETE");
    await searchRow(page, shared).locator("[data-library='remove']").click();
    expect(await write).toEqual({ expected: A.member_id, status: 409 });
    await expect(searchRow(page, bOnly)).toBeVisible();
    await expect(searchRow(page, aOnly)).toHaveCount(0);
    await expect(heading(page)).toHaveText(found(2));
    await expect(searchRow(page, shared).locator("[data-library='added']")).toBeVisible();
    expect(idsOf(state, B.member_id)).toEqual([shared, bOnly]);
    expect(idsOf(state, A.member_id)).toEqual([aOnly, shared]);
    expect(libraryOf(state, B.member_id).removed).toEqual([]);
  });

  test("search: B signs in elsewhere and A presses add: nothing is added to B's library, the page becomes B's", async ({ page, apiMock: state }) => {
    const query = "topic=e_commerce";
    const shown = filterListRows(ROWS, new URLSearchParams(query))!;
    const notMine = shown.filter((entry) => !entry.personal).map((entry) => entry.row.id);
    const [target, bOnly] = [notMine[0]!, notMine[1]!];
    libraryOf(state, B.member_id).items = [{ id: bOnly, addedAt: "2026-11-01T00:00:00.000Z" }];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${query}`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(shown.length);
    await expect(searchRow(page, bOnly).locator("[data-library='add']")).toBeVisible();
    state.member = B;
    const write = nextWrite(page, "PUT");
    await searchRow(page, target).locator("[data-library='add']").click();
    expect(await write).toEqual({ expected: A.member_id, status: 409 });
    // 乙的資料：乙加過的那一篇是「● 已加入」，甲的那幾篇與按的那一篇是「加入我的文獻庫」，按鈕沒有停在送出中。
    await expect(searchRow(page, bOnly).locator("[data-library='added']")).toBeVisible();
    await expect(page.locator("[data-library='added']")).toHaveCount(1);
    await expect(searchRow(page, target).locator("[data-library='add']")).toBeEnabled();
    expect(idsOf(state, B.member_id)).toEqual([bOnly]);
  });

  for (const action of ["add", "remove"] as const) {
    test(`paper page: B signs in elsewhere and A presses ${action}: B's library is unchanged, the page becomes B's`, async ({ page, apiMock: state }) => {
      const normal = paperId("normal");
      const forB = "Paper title member B sees";
      const inLibrary = action === "remove";
      state.libraryIds = inLibrary ? [normal] : [];
      libraryOf(state, B.member_id).items = inLibrary ? [{ id: normal, addedAt: "2026-11-01T00:00:00.000Z" }] : [];
      await page.setViewportSize(DESKTOP);
      await page.goto(`${member.baseURL}/papers/${normal}`);
      const rail = page.locator("[data-library='rail']");
      const button = rail.getByRole("button", { name: text(`paper:paper.library.${action}`) });
      await expect(button).toBeVisible();
      state.member = B;
      state.paperByMember = { [B.member_id]: { ...paperPage("normal"), paper: { ...paperPage("normal").paper, title: forB }, in_my_library: inLibrary } };
      const write = nextWrite(page, action === "add" ? "PUT" : "DELETE");
      await button.click();
      expect(await write).toEqual({ expected: A.member_id, status: 409 });
      await expect(page.locator("h1")).toHaveText(forB);
      if (inLibrary) {
        await expect(rail).toContainText(text("paper:paper.library.added"));
        await expect(rail.getByRole("button", { name: text("paper:paper.library.remove") })).toBeEnabled();
      } else {
        await expect(rail.getByRole("button", { name: text("paper:paper.library.add") })).toBeEnabled();
      }
      expect(idsOf(state, B.member_id)).toEqual(inLibrary ? [normal] : []);
      expect(libraryOf(state, B.member_id).removed).toEqual([]);
    });
  }

  test("my library page: A removes a paper, B signs in elsewhere, A presses undo: B's library and B's removed record are unchanged, the page becomes B's (P3-2)", async ({ page, apiMock: state }) => {
    const aOnly = ROWS[0]!.row.id;
    const bOnly = ROWS[1]!.row.id;
    const shared = ROWS[2]!.row.id;
    state.libraryIds = [aOnly, shared];
    // 乙之前也移出過同一篇：沒有保護的話，甲畫面上的「復原」會用乙的身分把它加回乙的文獻庫。
    const bRemoved = { id: shared, addedAt: "2026-10-01T00:00:00.000Z" };
    libraryOf(state, B.member_id).items = [{ id: bOnly, addedAt: "2026-11-01T00:00:00.000Z" }];
    libraryOf(state, B.member_id).removed = [bRemoved];
    await openLibrary(page);
    await expect(rows(page)).toHaveCount(2);
    await row(page, shared).locator("[data-library='remove']:visible").click();
    await expect(row(page, shared).locator("[data-library='restore']")).toBeFocused();
    expect(idsOf(state, A.member_id)).toEqual([aOnly]);
    state.member = B;
    const write = nextWrite(page, "POST");
    await row(page, shared).locator("[data-library='restore']").click();
    expect(await write).toEqual({ expected: A.member_id, status: 409 });
    await expect(row(page, bOnly)).toBeVisible();
    await expect(rows(page)).toHaveCount(1);
    await expect(row(page, aOnly)).toHaveCount(0);
    expect(idsOf(state, B.member_id)).toEqual([bOnly]);
    expect(libraryOf(state, B.member_id).removed).toEqual([bRemoved]);
    expect(idsOf(state, A.member_id)).toEqual([aOnly]);
    expect(libraryOf(state, A.member_id).removed.map((record) => record.id)).toEqual([shared]);
  });

  test("L2: back to the tab with a held row, 'who am I' fails (network error), then A presses add: nothing goes into B's library, the page becomes B's", async ({ page, apiMock: state }) => {
    const held = ROWS[0]!.row.id;
    const bOnly = ROWS[1]!.row.id;
    const shared = ROWS[2]!.row.id;
    state.libraryIds = [held, shared];
    libraryOf(state, B.member_id).items = [
      { id: shared, addedAt: "2026-11-01T00:00:00.000Z" },
      { id: bOnly, addedAt: "2026-11-02T00:00:00.000Z" },
    ];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(2);
    await searchRow(page, held).locator("[data-library='remove']").click();
    await expect(searchRow(page, held).locator("[data-library='add']")).toBeFocused();
    // 切回時的「我是誰」網路錯（問不到：照常送出，L2）；之後的照常回答（乙）。
    let failNext = true;
    await page.route("**/api/v1/me", async (route) => {
      if (failNext) {
        failNext = false;
        await route.abort("internetdisconnected");
        return;
      }
      await route.fallback();
    });
    state.member = B;
    const failed = page.waitForEvent("requestfailed", (request) => new URL(request.url()).pathname === "/api/v1/me");
    await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
    await failed;
    await twoFrames(page);
    const write = nextWrite(page, "PUT");
    await searchRow(page, held).locator("[data-library='add']").click();
    expect(await write).toEqual({ expected: A.member_id, status: 409 });
    await expect(searchRow(page, bOnly)).toBeVisible();
    await expect(searchRow(page, held)).toHaveCount(0);
    await expect(heading(page)).toHaveText(found(2));
    expect(idsOf(state, B.member_id)).toEqual([shared, bOnly]);
  });
});

// epic-3h2 PR 1 fix1（ADR-0055）：寫入帶的預期成員＝畫面上那份資料是用誰的身分讀的，不是路由現在認定的人；成員專屬的讀取也帶
// 預期成員，後端（替身）和接到請求時登入的人不同就回 409。H1：同一頁換網址（「再載入」）時路由先認到乙、新的清單還擋著——
// 甲的列不留在畫面上；H2：論文頁 409 之後在背景重讀、還擋著時再按一次——帶的仍是甲、回 409；H3：「我是誰」認到甲之後讀取前
// cookie 換成乙——讀取回 409、整頁換成乙。每一條都斷言乙的文獻庫與封存沒變、畫面最後是乙；預期都從假資料算或寫死。
test.describe("the expected member is whoever the shown data was read as (ADR-0055, fix1)", () => {
  const A = MEMBERS[0]!;
  const B = MEMBERS[1]!;
  const searchRow = (page: Page, id: string) => page.locator(`[data-paper-row="${id}"]`);
  const idsOf = (state: ApiMockState, memberId: string) => libraryOf(state, memberId).items.map((record) => record.id);
  const ALL = ROWS.map((entry) => entry.row.id);

  /** 下一個 `method` 請求（路徑以 `prefix` 開頭）的預期成員標頭與替身回的狀態。 */
  function nextAnswer(page: Page, method: string, prefix: string) {
    return page
      .waitForResponse((response) => response.request().method() === method && `${new URL(response.url()).pathname}${new URL(response.url()).search}`.startsWith(prefix))
      .then(async (response) => ({ expected: (await response.request().allHeaders())[EXPECTED_MEMBER_HEADER], status: response.status() }));
  }

  /** 第一次收到符合 `pattern` 的請求時，先把登入的人換成乙再交給替身（「我是誰」回答之後、這個讀取送到之前換了 cookie）。 */
  async function switchToBOnFirst(page: Page, state: ApiMockState, pattern: string | RegExp): Promise<void> {
    let switched = false;
    await page.route(pattern, async (route) => {
      if (!switched && route.request().method() === "GET") {
        switched = true;
        state.member = B;
      }
      await route.fallback();
    });
  }

  test("H1 search: B signs in elsewhere, A presses load more and B's list is held: A's rows leave the page at once; B's library is unchanged, the page becomes B's", async ({ page, apiMock: state }) => {
    state.libraryIds = ALL;
    const shared = filterListRows(ROWS, new URLSearchParams("scope=personal"), ALL)![0]!.row.id;
    libraryOf(state, B.member_id).items = [{ id: shared, addedAt: "2026-11-01T00:00:00.000Z" }];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(20);
    await expect(searchRow(page, shared).locator("[data-library='remove']")).toBeVisible();
    state.member = B;
    const [listGate, releaseList] = gate();
    state.listGate = listGate;
    const meAsked = state.requests.filter((request) => request.path === "/api/v1/me").length;
    await page.getByRole("button", { name: fill(text("search:search.results.more"), { count: 20 }) }).click();
    await expect(page).toHaveURL(/shown=40/);
    // 路由已經問到乙（「我是誰」回答了）、乙的清單還擋著：甲的列（含甲、乙都有的那一篇的「移出」）不在畫面上。
    await expect.poll(() => state.requests.filter((request) => request.path === "/api/v1/me").length).toBeGreaterThan(meAsked);
    await expect.poll(() => listReads(state.requests)).toBeGreaterThan(1);
    await expect(page.locator("[data-state='loading']")).toBeVisible();
    await expect(page.locator("[data-paper-row]")).toHaveCount(0);
    expect(idsOf(state, B.member_id)).toEqual([shared]);
    state.listGate = null;
    releaseList();
    await expect(page.locator("[data-paper-row]")).toHaveCount(1);
    await expect(searchRow(page, shared).locator("[data-library='added']")).toBeVisible();
    expect(idsOf(state, B.member_id)).toEqual([shared]);
    expect(libraryOf(state, B.member_id).removed).toEqual([]);
    expect(idsOf(state, A.member_id)).toEqual(ALL);
    expect(requestsOfNow(state.requests, "DELETE", "/api/v1/library/")).toEqual([]);
  });

  test("H1 my library: A removes a paper, B signs in elsewhere, A presses load more and B's list is held: A's rows and undo leave the page at once; B's library and removed record are unchanged", async ({ page, apiMock: state }) => {
    state.libraryIds = ALL;
    const [first, shared] = [ALL[0]!, ALL[1]!];
    const bOnly = ALL[2]!;
    const bRemoved = { id: shared, addedAt: "2026-10-01T00:00:00.000Z" };
    libraryOf(state, B.member_id).items = [{ id: bOnly, addedAt: "2026-11-01T00:00:00.000Z" }];
    libraryOf(state, B.member_id).removed = [bRemoved];
    await openLibrary(page);
    await expect(rows(page)).toHaveCount(20);
    expect(await rows(page).first().getAttribute("data-library-row")).toBe(first);
    await row(page, shared).locator("[data-library='remove']:visible").click();
    await expect(row(page, shared).locator("[data-library='restore']")).toBeFocused();
    state.member = B;
    const [libraryGate, releaseLibrary] = gate();
    state.libraryGate = libraryGate;
    const reads = requestsOfNow(state.requests, "GET", "/api/v1/library").length;
    await page.locator("[data-load-more]").click();
    await expect(page).toHaveURL(/shown=40/);
    await expect.poll(() => requestsOfNow(state.requests, "GET", "/api/v1/library").length).toBeGreaterThan(reads);
    // 乙的清單還擋著：甲的列與「復原」都不在畫面上（沒有可以按的）。
    await expect(rows(page)).toHaveCount(0);
    await expect(page.locator("[data-library='restore']")).toHaveCount(0);
    state.libraryGate = null;
    releaseLibrary();
    await expect(row(page, bOnly)).toBeVisible();
    await expect(rows(page)).toHaveCount(1);
    expect(idsOf(state, B.member_id)).toEqual([bOnly]);
    expect(libraryOf(state, B.member_id).removed).toEqual([bRemoved]);
    expect(requestsOfNow(state.requests, "POST", "/api/v1/library/")).toEqual([]);
  });

  test("H2 paper page: after a 409 the page is read again in the background; pressing again before it arrives still goes out as A and is refused; B's library is unchanged", async ({ page, apiMock: state }) => {
    const normal = paperId("normal");
    const forB = "Paper title member B sees";
    state.libraryIds = [normal];
    libraryOf(state, B.member_id).items = [{ id: normal, addedAt: "2026-11-01T00:00:00.000Z" }];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/papers/${normal}`);
    const rail = page.locator("[data-library='rail']");
    const remove = rail.getByRole("button", { name: text("paper:paper.library.remove") });
    await expect(remove).toBeVisible();
    state.member = B;
    state.paperByMember = { [B.member_id]: { ...paperPage("normal"), paper: { ...paperPage("normal").paper, title: forB }, in_my_library: true } };
    const [paperGate, releasePaper] = gate();
    state.paperGate = paperGate;
    const reads = requestsOfNow(state.requests, "GET", `/api/v1/papers/${normal}`).length;
    const first = nextAnswer(page, "DELETE", "/api/v1/library/");
    await remove.click();
    expect(await first).toEqual({ expected: A.member_id, status: 409 });
    // 路由已經換成乙、論文頁在背景重讀（還擋著）：畫面仍是甲讀的那一份，再按一次也帶甲。
    await expect.poll(() => requestsOfNow(state.requests, "GET", `/api/v1/papers/${normal}`).length).toBeGreaterThan(reads);
    await expect(page.locator("h1")).not.toHaveText(forB);
    await expect(remove).toBeEnabled();
    const second = nextAnswer(page, "DELETE", "/api/v1/library/");
    await remove.click();
    expect(await second).toEqual({ expected: A.member_id, status: 409 });
    expect(idsOf(state, B.member_id)).toEqual([normal]);
    expect(libraryOf(state, B.member_id).removed).toEqual([]);
    state.paperGate = null;
    releasePaper();
    await expect(page.locator("h1")).toHaveText(forB);
    await expect(rail).toContainText(text("paper:paper.library.added"));
    expect(idsOf(state, B.member_id)).toEqual([normal]);
    expect(libraryOf(state, B.member_id).removed).toEqual([]);
    expect(idsOf(state, A.member_id)).toEqual([normal]);
  });

  test("H3 search: 'who am I' says A, then the cookie becomes B before the list is read: the list read goes out as A and is refused; the page becomes B's", async ({ page, apiMock: state }) => {
    const aOnly = ROWS[0]!.row.id;
    const bOnly = ROWS[1]!.row.id;
    state.libraryIds = [aOnly];
    libraryOf(state, B.member_id).items = [{ id: bOnly, addedAt: "2026-11-01T00:00:00.000Z" }];
    await switchToBOnFirst(page, state, /\/api\/v1\/papers\?/);
    const read = nextAnswer(page, "GET", "/api/v1/papers?");
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    expect(await read).toEqual({ expected: A.member_id, status: 409 });
    await expect(searchRow(page, bOnly)).toBeVisible();
    await expect(page.locator("[data-paper-row]")).toHaveCount(1);
    await expect(searchRow(page, aOnly)).toHaveCount(0);
    expect(idsOf(state, B.member_id)).toEqual([bOnly]);
    expect(idsOf(state, A.member_id)).toEqual([aOnly]);
  });

  test("H3 my library: 'who am I' says A, then the cookie becomes B before the list is read: refused; the page becomes B's", async ({ page, apiMock: state }) => {
    const aOnly = ROWS[0]!.row.id;
    const bOnly = ROWS[1]!.row.id;
    state.libraryIds = [aOnly];
    libraryOf(state, B.member_id).items = [{ id: bOnly, addedAt: "2026-11-01T00:00:00.000Z" }];
    await switchToBOnFirst(page, state, /\/api\/v1\/library(\?|$)/);
    const read = nextAnswer(page, "GET", "/api/v1/library");
    await openLibrary(page);
    expect(await read).toEqual({ expected: A.member_id, status: 409 });
    await expect(row(page, bOnly)).toBeVisible();
    await expect(rows(page)).toHaveCount(1);
    await expect(row(page, aOnly)).toHaveCount(0);
    expect(idsOf(state, B.member_id)).toEqual([bOnly]);
  });

  test("H3 paper page: 'who am I' says A, then the cookie becomes B before the paper is read: refused; the page becomes B's", async ({ page, apiMock: state }) => {
    const normal = paperId("normal");
    const forB = "Paper title member B sees";
    state.libraryIds = [normal];
    state.paperByMember = { [B.member_id]: { ...paperPage("normal"), paper: { ...paperPage("normal").paper, title: forB }, in_my_library: false } };
    await switchToBOnFirst(page, state, new RegExp(`/api/v1/papers/${normal}$`));
    const read = nextAnswer(page, "GET", `/api/v1/papers/${normal}`);
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/papers/${normal}`);
    expect(await read).toEqual({ expected: A.member_id, status: 409 });
    await expect(page.locator("h1")).toHaveText(forB);
    await expect(page.locator("[data-library='rail']").getByRole("button", { name: text("paper:paper.library.add") })).toBeEnabled();
    expect(idsOf(state, B.member_id)).toEqual([]);
  });

  test("M3: after a 409 the press is over (the button is back) while 'who am I' is still on its way; once it answers, the page becomes B's", async ({ page, apiMock: state }) => {
    const query = "topic=e_commerce";
    const shown = filterListRows(ROWS, new URLSearchParams(query))!;
    const notMine = shown.filter((entry) => !entry.personal).map((entry) => entry.row.id);
    const [target, bOnly] = [notMine[0]!, notMine[1]!];
    libraryOf(state, B.member_id).items = [{ id: bOnly, addedAt: "2026-11-01T00:00:00.000Z" }];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?${query}`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(shown.length);
    state.member = B;
    const [meGate, releaseMe] = gate();
    state.meGate = meGate;
    const meAsked = state.requests.filter((request) => request.path === "/api/v1/me").length;
    const write = nextAnswer(page, "PUT", "/api/v1/library/");
    await searchRow(page, target).locator("[data-library='add']").click();
    expect(await write).toEqual({ expected: A.member_id, status: 409 });
    // 「我是誰」已經送出、還沒回來：按的那一顆已經恢復（不停在送出中），畫面還是甲的。
    await expect.poll(() => state.requests.filter((request) => request.path === "/api/v1/me").length).toBeGreaterThan(meAsked);
    await expect(searchRow(page, target).locator("[data-library='add']")).toBeEnabled();
    await expect(searchRow(page, bOnly).locator("[data-library='add']")).toBeVisible();
    state.meGate = null;
    releaseMe();
    await expect(searchRow(page, bOnly).locator("[data-library='added']")).toBeVisible();
    await expect(page.locator("[data-library='added']")).toHaveCount(1);
    expect(idsOf(state, B.member_id)).toEqual([bOnly]);
  });
});

/** 到現在為止收到的請求（不等）。 */
function requestsOfNow(requests: ApiRequest[], method: string, prefix: string): ApiRequest[] {
  return requests.filter((request) => request.method === method && request.path.startsWith(prefix));
}

// epic-3h2 PR 1 fix2：換人流程只跑一輪、換人後的頁面一掛上就用新的人讀（N1）；「再載入」確認暫留列的補查回 409 時走換人流程（N2）。
test.describe("following a member change reads only as the new member (ADR-0055, fix2)", () => {
  const A = MEMBERS[0]!;
  const B = MEMBERS[1]!;
  const searchRow = (page: Page, id: string) => page.locator(`[data-paper-row="${id}"]`);
  const idsOf = (state: ApiMockState, memberId: string) => libraryOf(state, memberId).items.map((record) => record.id);
  const meAsks = (state: ApiMockState) => state.requests.filter((request) => request.path === "/api/v1/me").length;
  const ALL = ROWS.map((entry) => entry.row.id);

  /** 從現在起，路徑以 `prefix` 開頭的 GET 回應：帶的預期成員與狀態（依序）。 */
  function readsFrom(page: Page, prefix: string): { expected: string | undefined; status: number }[] {
    const seen: { expected: string | undefined; status: number }[] = [];
    page.on("response", (response) => {
      const url = new URL(response.url());
      if (response.request().method() !== "GET" || !`${url.pathname}${url.search}`.startsWith(prefix)) return;
      seen.push({ expected: response.request().headers()[EXPECTED_MEMBER_HEADER], status: response.status() });
    });
    return seen;
  }

  test("N1 search: A's load more is still on its way, B signs in elsewhere, A presses remove: one round of 'who am I', every list read after the 409 goes out as B, the page ends as B's", async ({ page, apiMock: state }) => {
    state.libraryIds = ALL;
    const shared = filterListRows(ROWS, new URLSearchParams("scope=personal"), ALL)![0]!.row.id;
    libraryOf(state, B.member_id).items = [{ id: shared, addedAt: "2026-11-01T00:00:00.000Z" }];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(20);
    // 甲按「再載入」，讀取擋著（甲的讀取還沒回來）；這時乙在別的分頁登入。
    const [listGate, releaseList] = gate();
    state.listGate = listGate;
    const reads = listReads(state.requests);
    await page.getByRole("button", { name: fill(text("search:search.results.more"), { count: 20 }) }).click();
    await expect.poll(() => listReads(state.requests)).toBeGreaterThan(reads);
    state.member = B;
    const meBefore = meAsks(state);
    const after = readsFrom(page, "/api/v1/papers?");
    const write = page
      .waitForResponse((response) => response.request().method() === "DELETE" && new URL(response.url()).pathname.startsWith("/api/v1/library/"))
      .then(async (response) => ({ expected: (await response.request().allHeaders())[EXPECTED_MEMBER_HEADER], status: response.status() }));
    await searchRow(page, shared).locator("[data-library='remove']").click();
    expect(await write).toEqual({ expected: A.member_id, status: 409 });
    // 換人流程問完「我是誰」、路由換成乙：乙的讀取送出（也擋著），畫面是骨架、不是甲的列。
    await expect.poll(() => meAsks(state)).toBe(meBefore + 2);
    await expect.poll(() => listReads(state.requests)).toBe(reads + 2);
    await expect(page.locator("[data-paper-row]")).toHaveCount(0);
    await expect(page.locator("[data-state='loading']")).toBeVisible();
    state.listGate = null;
    releaseList();
    await expect(searchRow(page, shared).locator("[data-library='added']")).toBeVisible();
    await expect(page.locator("[data-paper-row]")).toHaveCount(1);
    await expect(page.locator("[data-state='loading']")).toHaveCount(0);
    await twoFrames(page);
    await page.waitForTimeout(300);
    // 只跑一輪：換人流程問一次、路由重跑問一次。換人之後送出的清單讀取只有乙的一個（甲擋著的那一個放行後才回應，畫面不用）。
    expect(meAsks(state)).toBe(meBefore + 2);
    expect(listReads(state.requests)).toBe(reads + 2);
    expect(after.filter((read) => read.expected === B.member_id)).toEqual([{ expected: B.member_id, status: 200 }]);
    expect(after.filter((read) => read.expected !== B.member_id)).toEqual([{ expected: A.member_id, status: 200 }]);
    expect(idsOf(state, B.member_id)).toEqual([shared]);
    expect(libraryOf(state, B.member_id).removed).toEqual([]);
  });

  test("N1 my library: B signs in elsewhere and A presses remove: one round of 'who am I', every list read after the 409 goes out as B, the page ends as B's", async ({ page, apiMock: state }) => {
    const aOnly = ROWS[0]!.row.id;
    const bOnly = ROWS[1]!.row.id;
    state.libraryIds = [aOnly];
    libraryOf(state, B.member_id).items = [{ id: bOnly, addedAt: "2026-11-01T00:00:00.000Z" }];
    await openLibrary(page);
    await expect(rows(page)).toHaveCount(1);
    state.member = B;
    const meBefore = meAsks(state);
    const after = readsFrom(page, "/api/v1/library");
    const write = page
      .waitForResponse((response) => response.request().method() === "DELETE" && new URL(response.url()).pathname.startsWith("/api/v1/library/"))
      .then(async (response) => ({ expected: (await response.request().allHeaders())[EXPECTED_MEMBER_HEADER], status: response.status() }));
    await row(page, aOnly).locator("[data-library='remove']:visible").click();
    expect(await write).toEqual({ expected: A.member_id, status: 409 });
    await expect(row(page, bOnly)).toBeVisible();
    await expect(rows(page)).toHaveCount(1);
    await twoFrames(page);
    await page.waitForTimeout(300);
    expect(meAsks(state)).toBe(meBefore + 2);
    expect(after).toEqual([{ expected: B.member_id, status: 200 }]);
    expect(idsOf(state, B.member_id)).toEqual([bOnly]);
    expect(idsOf(state, A.member_id)).toEqual([aOnly]);
  });

  test("N2 search 'my library' holding a removed row: load more is read as A, then B signs in before the held row is checked: the check is refused, the page becomes B's", async ({ page, apiMock: state }) => {
    state.libraryIds = ALL;
    const all = filterListRows(ROWS, new URLSearchParams("scope=personal"), ALL)!.map((entry) => entry.row.id);
    const bOnly = all[25]!;
    libraryOf(state, B.member_id).items = [{ id: bOnly, addedAt: "2026-11-01T00:00:00.000Z" }];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(20);
    const held = all[0]!;
    await searchRow(page, held).locator("[data-library='remove']").click();
    await expect(searchRow(page, held).locator("[data-library='add']")).toBeFocused();
    // 主清單（甲）照常讀完；確認暫留列的那一篇送到之前，登入的人換成乙。
    let switched = false;
    await page.route(new RegExp(`/api/v1/papers/${held}$`), async (route) => {
      if (!switched) {
        switched = true;
        state.member = B;
      }
      await route.fallback();
    });
    const check = page
      .waitForResponse((response) => new URL(response.url()).pathname === `/api/v1/papers/${held}`)
      .then(async (response) => ({ expected: (await response.request().allHeaders())[EXPECTED_MEMBER_HEADER], status: response.status() }));
    await page.getByRole("button", { name: fill(text("search:search.results.more"), { count: 20 }) }).click();
    expect(await check).toEqual({ expected: A.member_id, status: 409 });
    await expect(searchRow(page, bOnly)).toBeVisible();
    await expect(page.locator("[data-paper-row]")).toHaveCount(1);
    await expect(searchRow(page, held)).toHaveCount(0);
    expect(idsOf(state, B.member_id)).toEqual([bOnly]);
    expect(libraryOf(state, B.member_id).removed).toEqual([]);
  });

  // epic-3h2 PR 1 fix3（N1 殘留）：第一輪收尾重讀又回 409 時要開始第二輪。重讀的結果和上一份一模一樣
  // （{ kind: "memberChanged" }），TanStack Query 的結構共享會沿用同一個物件；頁面用物件認「處理過沒」就會跳過、停在骨架。
  // 做法：第一輪的「我是誰」與重跑路由的「我是誰」都還說是甲（例如乙剛登入、那兩次回應比較早），所以頁面重新掛上後
  // 用甲讀 → 409（這時第一輪還沒結束：重跑路由的那一次擋著）；放行後第一輪結束、收尾重讀又是 409 → 第二輪問到乙。

  /** 接下來兩次「我是誰」回甲（第二次等 `hold` 放行才回）；之後照替身（目前登入的人）。回一個函式：問了幾次。 */
  async function meSaysAThenFollows(page: Page, hold: Promise<void>): Promise<() => number> {
    let asked = 0;
    await page.route(/\/api\/v1\/me$/, async (route) => {
      asked += 1;
      if (asked === 1) return route.fulfill({ json: A });
      if (asked === 2) {
        await hold;
        return route.fulfill({ json: A });
      }
      return route.fallback();
    });
    return () => asked;
  }

  test("fix3 N1 search: the read again at the end of the first round is refused again: a second round starts, the page ends as B's (not stuck on the skeleton)", async ({ page, apiMock: state }) => {
    state.libraryIds = ALL;
    const shared = filterListRows(ROWS, new URLSearchParams("scope=personal"), ALL)![0]!.row.id;
    libraryOf(state, B.member_id).items = [{ id: shared, addedAt: "2026-11-01T00:00:00.000Z" }];
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?scope=personal`);
    await expect(page.locator("[data-paper-row]")).toHaveCount(20);
    const [hold, release] = gate();
    const asked = await meSaysAThenFollows(page, hold);
    state.member = B;
    const after = readsFrom(page, "/api/v1/papers?");
    const refusedAsA = () => after.filter((read) => read.expected === A.member_id && read.status === 409).length;
    const write = page
      .waitForResponse((response) => response.request().method() === "DELETE" && new URL(response.url()).pathname.startsWith("/api/v1/library/"))
      .then(async (response) => ({ expected: (await response.request().allHeaders())[EXPECTED_MEMBER_HEADER], status: response.status() }));
    await searchRow(page, shared).locator("[data-library='remove']").click();
    expect(await write).toEqual({ expected: A.member_id, status: 409 });
    // 第一輪：「我是誰」說甲、頁面重新掛上用甲讀 → 409；重跑路由的「我是誰」擋著，這一輪還沒結束。
    await expect.poll(asked).toBe(2);
    await expect.poll(refusedAsA).toBeGreaterThan(0);
    await expect(page.locator("[data-paper-row]")).toHaveCount(0);
    const refusedInRound = refusedAsA();
    release();
    // 收尾重讀又是 409 → 第二輪：問「我是誰」是乙，整頁換成乙的清單。
    await expect(searchRow(page, shared).locator("[data-library='added']")).toBeVisible();
    await expect(page.locator("[data-paper-row]")).toHaveCount(1);
    await expect(page.locator("[data-state='loading']")).toHaveCount(0);
    await twoFrames(page);
    await page.waitForTimeout(300);
    // 第一輪兩次（換人流程一次、重跑路由一次），第二輪兩次。
    expect(asked()).toBe(4);
    expect(refusedAsA()).toBeGreaterThan(refusedInRound);
    expect(after.filter((read) => read.expected === B.member_id)).toEqual([{ expected: B.member_id, status: 200 }]);
    expect(after.filter((read) => read.expected !== B.member_id).every((read) => read.expected === A.member_id && read.status === 409)).toBe(true);
    expect(idsOf(state, B.member_id)).toEqual([shared]);
    expect(libraryOf(state, B.member_id).removed).toEqual([]);
  });

  test("fix3 N1 my library: the read again at the end of the first round is refused again: a second round starts, the page ends as B's (not stuck on the skeleton)", async ({ page, apiMock: state }) => {
    const aOnly = ROWS[0]!.row.id;
    const bOnly = ROWS[1]!.row.id;
    state.libraryIds = [aOnly];
    libraryOf(state, B.member_id).items = [{ id: bOnly, addedAt: "2026-11-01T00:00:00.000Z" }];
    await openLibrary(page);
    await expect(rows(page)).toHaveCount(1);
    const [hold, release] = gate();
    const asked = await meSaysAThenFollows(page, hold);
    state.member = B;
    const after = readsFrom(page, "/api/v1/library");
    const refusedAsA = () => after.filter((read) => read.expected === A.member_id && read.status === 409).length;
    const write = page
      .waitForResponse((response) => response.request().method() === "DELETE" && new URL(response.url()).pathname.startsWith("/api/v1/library/"))
      .then(async (response) => ({ expected: (await response.request().allHeaders())[EXPECTED_MEMBER_HEADER], status: response.status() }));
    await row(page, aOnly).locator("[data-library='remove']:visible").click();
    expect(await write).toEqual({ expected: A.member_id, status: 409 });
    await expect.poll(asked).toBe(2);
    await expect.poll(refusedAsA).toBeGreaterThan(0);
    await expect(rows(page)).toHaveCount(0);
    const refusedInRound = refusedAsA();
    release();
    await expect(row(page, bOnly)).toBeVisible();
    await expect(rows(page)).toHaveCount(1);
    await expect(page.locator("[data-state='loading']")).toHaveCount(0);
    await twoFrames(page);
    await page.waitForTimeout(300);
    expect(asked()).toBe(4);
    expect(refusedAsA()).toBeGreaterThan(refusedInRound);
    expect(after.filter((read) => read.expected === B.member_id)).toEqual([{ expected: B.member_id, status: 200 }]);
    expect(after.filter((read) => read.expected !== B.member_id).every((read) => read.expected === A.member_id && read.status === 409)).toBe(true);
    expect(idsOf(state, B.member_id)).toEqual([bOnly]);
    expect(idsOf(state, A.member_id)).toEqual([aOnly]);
  });
});

// 全站頁尾（Story 3.15）和手機底部選取列互不遮蓋：清單很短時選取列在頁尾上面；清單很長、捲到最底時選取列停在
// 頁尾上面（sticky 只在這一頁的範圍內移動），頁尾整個看得到；捲到中間時頁尾在視窗外，選取列貼在視窗底部。
test.describe("my library page on a phone: the dock and the site footer", () => {
  async function expectApart(page: Page): Promise<void> {
    const dock = (await page.locator("[data-library-dock]").boundingBox())!;
    const footer = (await page.locator("footer[data-site-footer]").boundingBox())!;
    expect(dock.y + dock.height, "dock bottom is above the footer top").toBeLessThanOrEqual(footer.y + 0.5);
    // 頁尾的連結點得到（最上面的元素是它自己，不是選取列）。
    const link = page.locator("footer[data-site-footer] a").first();
    const linkBox = (await link.boundingBox())!;
    const onTop = await page.evaluate(
      ([x, y]) => document.elementFromPoint(x!, y!)?.closest("footer") !== null,
      [linkBox.x + linkBox.width / 2, linkBox.y + linkBox.height / 2],
    );
    expect(onTop, "footer link is not covered").toBe(true);
  }

  test("a short library: the footer is below the dock", async ({ page, apiMock }) => {
    apiMock.libraryIds = WITH_DOI.slice(0, 1);
    await openLibrary(page, PHONE);
    await checkbox(page, apiMock.libraryIds[0]!).check();
    await expect(page.locator("[data-library-dock]")).toBeVisible();
    await expectApart(page);
  });

  test("a long library scrolled to the end: the dock stops above the footer", async ({ page, apiMock }) => {
    apiMock.libraryIds = [...WITH_DOI.slice(0, 12), NO_DOI[0]!];
    await openLibrary(page, PHONE);
    await checkbox(page, apiMock.libraryIds[0]!).check();
    // 捲到中間：選取列貼在視窗底部，頁尾在視窗外。
    const dock = page.locator("[data-library-dock]");
    const middle = (await dock.boundingBox())!;
    expect(middle.y + middle.height).toBeCloseTo(PHONE.height, 0);
    expect((await page.locator("footer[data-site-footer]").boundingBox())!.y).toBeGreaterThanOrEqual(PHONE.height);
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    // 頁面高度有小數（不到 1px 落在視窗外）。
    await expect(page.locator("footer[data-site-footer]")).toBeInViewport({ ratio: 0.99 });
    await expectApart(page);
  });
});
