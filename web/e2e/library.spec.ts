// 我的文獻庫（Story 3.17，成員版）：EXPERIENCE.md 畫面 13 與 State Patterns「13. 我的文獻庫」的每一種狀態
// （可以操作、很多篇、勾選、沒有全文連結、超過 10 篇、擋住新分頁、移出、空的、載入中、錯誤），
// 論文頁的「加入我的文獻庫」／「● 已在我的文獻庫」＋「移出」（含手機：State Patterns「手機：已經加入我的文獻庫」），
// 找論文每列的「加入我的文獻庫」／「● 已加入」，以及三種寬度的整頁截圖。
// 資料是 fixtures/paper-list.json 的假清單（自己編的）；文獻庫是替身的 libraryIds。
import { fileURLToPath } from "node:url";

import type { Page } from "@playwright/test";

import { LIST_FIXTURES, MEMBERS, filterListRows, gate, paperId, type ApiRequest, type ListFixtureRow } from "./api-mock.ts";
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { SITES } from "./pages.ts";

const member = SITES[0]!;
const SCREENSHOT_DIR = fileURLToPath(new URL("./screenshots/", import.meta.url));
const DESKTOP = { width: 1280, height: 900 };
const PHONE = { width: 390, height: 844 };
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
    await expect(added).toBeFocused();
    await expect(target.getByRole("button")).toHaveCount(0);
    expect(apiMock.libraryIds).toContain(id);
    await page.getByRole("button", { name: text("search:search.scope.personal"), exact: true }).click();
    await expect(page.locator(`[data-paper-row="${id}"] [data-library='added']`)).toBeVisible();
  });

  test("add lights up when focus is in the row (keyboard)", async ({ page }) => {
    // 驗收條件「滑到或聚焦在一列」（審查 F14）：焦點放到那一列的標題連結（滑鼠不在列上）。
    // 直接 focus()：Safari 預設按 Tab 不停在連結上（審查 G4），用 Tab 走過去在 webkit 走不到。
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search?topic=e_commerce`);
    const target = page.locator("[data-paper-row]").filter({ has: page.locator("[data-library='add']") }).first();
    const face = target.locator("[data-library='add'] span").first();
    const dim = await face.evaluate((element) => getComputedStyle(element).color);
    const titleLink = target.locator("h3").getByRole("link");
    await titleLink.focus();
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
