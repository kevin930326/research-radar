// 示範站論文清單（Story 3.14 示範站；EXPERIENCE.md 畫面 7、State Patterns「7. 論文清單」）：讀 preview/ 的範例匯出
// （fixtures/demo-preview，自己編的 60 篇假論文），篩選在瀏覽器裡做、條件寫進網址、一次 50 篇＋「再載入」、
// 每列「● 有完整內容」或「○ 只有書目」；沒有結果、載入中、錯誤三種狀態（用 page.route 擋 preview/ 的檔）；
// 三種寬度的整頁截圖。預期的篇數直接從範例檔數，不用畫面的篩選程式。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import type { Page, Route } from "@playwright/test";

import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { SITES } from "./pages.ts";

const demo = SITES[1]!;
const SCREENSHOT_DIR = fileURLToPath(new URL("./screenshots/", import.meta.url));
const FIXTURE = fileURLToPath(new URL("./fixtures/demo-preview/", import.meta.url));
const DESKTOP = { width: 1280, height: 900 };
const PHONE = { width: 390, height: 844 };

function json<T>(path: string): T {
  return JSON.parse(readFileSync(`${FIXTURE}${path}`, "utf-8")) as T;
}

interface Index {
  id: string[];
  journal: (string | null)[];
  year: (number | null)[];
  topics: (string[] | null)[];
  methods: (string[] | null)[];
  has_abstract: boolean[];
  has_full_content: boolean[];
  is_new_this_week: boolean[];
}
const MANIFEST = json<{ export_id: string; client_config: { paper_list_page_size: number } }>("manifest.json");
const INDEX = json<Index>(`${MANIFEST.export_id}/papers-index.json`);
const TITLES = json<{ titles: string[] }>(`${MANIFEST.export_id}/titles.json`).titles;
type Named = { key: string; name: string }[];
const NAMES = json<{ names: { journals: Named; topics: Named; methods: Named } }>(`${MANIFEST.export_id}/taxonomy.json`).names;
const SHARD = json<{ papers: { biblio: { biblio: { id: string; doi_url: string | null } } }[] }>(`${MANIFEST.export_id}/papers/000.json`).papers;
const PAGE_SIZE = MANIFEST.client_config.paper_list_page_size;
const TOTAL = TITLES.length;

/** 範例檔裡符合的篇數（每一篇的第 i 欄）。 */
function count(match: (at: number) => boolean): number {
  return TITLES.filter((_, at) => match(at)).length;
}

function text(key: string): string {
  const value = TEXT[key];
  if (value === undefined) throw new Error(`no expected text ${key}`);
  return value;
}

function fill(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{\{(\w+)\}\}/g, (_, name: string) => String(values[name]));
}

/** 標題整行（設計稿：各段之間「 · 」，最後是篇數）；沒有條件時只有「全部論文」那一段。 */
function heading(parts: string[], total: number): string {
  return [...parts, fill(text("demo:demo.papers.count"), { count: total })].join(" · ");
}

const name = (list: Named, key: string) => list.find((entry) => entry.key === key)!.name;

const rows = (page: Page) => page.locator("[data-paper-row]");
const resultCount = (page: Page) => page.locator("[data-result-count]");

async function open(page: Page, query = "", viewport = DESKTOP): Promise<void> {
  await page.setViewportSize(viewport);
  await page.goto(`${demo.baseURL}/papers${query}`);
}

async function expectCount(page: Page, total: number): Promise<void> {
  await expect(resultCount(page)).toHaveText(fill(text("demo:demo.papers.count"), { count: total }));
  await expect(rows(page)).toHaveCount(Math.min(total, PAGE_SIZE));
}

test.describe("demo paper list", () => {
  test("shows every paper, 50 at a time, with the full-content mark on each row", async ({ page }) => {
    await open(page);
    await expect(page.locator("h1")).toContainText(text("demo:demo.papers.all"));
    await expectCount(page, TOTAL);
    await expect(page.locator("[data-data-date]")).toBeVisible();

    await expect(page.locator("h1")).toHaveText(heading([text("demo:demo.papers.all")], TOTAL));

    // 每列都有「● 有完整內容」或「○ 只有書目」：逐列和範例檔的 has_full_content 比（兩列對調也抓得到）。
    const shownIds = await rows(page).evaluateAll((items) => items.map((item) => item.getAttribute("data-paper-row")!));
    expect(shownIds).toHaveLength(PAGE_SIZE);
    const full = shownIds.filter((id) => INDEX.has_full_content[INDEX.id.indexOf(id)]);
    expect(full.length).toBeGreaterThan(0);
    expect(full.length).toBeLessThan(PAGE_SIZE);
    for (const id of shownIds) {
      const hasFull = INDEX.has_full_content[INDEX.id.indexOf(id)]!;
      const mark = page.locator(`[data-paper-row='${id}'] [data-full-content]`);
      await expect(mark).toHaveAttribute("data-full-content", hasFull ? "yes" : "no");
      await expect(mark).toHaveText(text(hasFull ? "demo:demo.row.full" : "demo:demo.row.biblioOnly"));
    }
    // 圓點：有完整內容是實心（有底色、沒有框），只有書目是空心（透明底、1px 框）。
    const dot = async (id: string) =>
      page.locator(`[data-paper-row='${id}'] [data-full-content] > span[aria-hidden='true']`).evaluate((element) => {
        const style = getComputedStyle(element);
        return { background: style.backgroundColor, border: style.borderTopWidth };
      });
    const solid = await dot(full[0]!);
    expect(solid.background).not.toBe("rgba(0, 0, 0, 0)");
    expect(solid.border).toBe("0px");
    const hollow = await dot(shownIds.find((id) => !full.includes(id))!);
    expect(hollow.background).toBe("rgba(0, 0, 0, 0)");
    expect(hollow.border).toBe("1px");

    // 依年份新到舊。
    const years = shownIds.map((id) => INDEX.year[INDEX.id.indexOf(id!)] ?? 0);
    expect(years).toEqual([...years].sort((a, b) => b - a));

    // 「再載入 50 篇」：接著顯示，目前顯示幾篇寫進網址；全部顯示後按鈕不見。
    await expect(page.locator("[data-shown]")).toHaveText(fill(text("search:search.results.shown"), { shown: PAGE_SIZE, total: TOTAL }));
    await expect(page.locator("[data-load-more]")).toHaveText(fill(text("search:search.results.more"), { count: PAGE_SIZE }));
    // 再載入不再發請求（接著顯示已讀進來的資料，AD-20）。只看 fetch／XHR。
    const later: string[] = [];
    page.on("request", (request) => {
      if (["fetch", "xhr"].includes(request.resourceType())) later.push(request.url());
    });
    await page.locator("[data-load-more]").click();
    await expect(rows(page)).toHaveCount(TOTAL);
    await expect(page).toHaveURL(/[?&]shown=100(&|$)/);
    await expect(page.locator("[data-load-more]")).toHaveCount(0);
    expect(later).toEqual([]);
    // 重新整理後還是顯示同樣多篇。
    await page.reload();
    await expect(rows(page)).toHaveCount(TOTAL);
  });

  test("row titles are not links yet; open full text goes to the DOI; tags filter the list", async ({ page }) => {
    await open(page);
    await expect(rows(page)).toHaveCount(PAGE_SIZE);
    // 論文頁（Story 3.9）還沒做（AR-67）：標題不是連結。
    await expect(page.locator("[data-paper-row] h3 a")).toHaveCount(0);
    // 開啟全文的連結和分片裡後端寫的 doi_url 一樣、開新分頁。只比畫面上有的列；先確認比到的篇數（不能空跑）。
    const onPage = new Set(await rows(page).evaluateAll((items) => items.map((item) => item.getAttribute("data-paper-row")!)));
    const checked = SHARD.filter((paper) => onPage.has(paper.biblio.biblio.id));
    expect(checked.length).toBeGreaterThan(0);
    expect(checked.some((paper) => paper.biblio.biblio.doi_url !== null)).toBe(true);
    for (const paper of checked) {
      const { id, doi_url: doiUrl } = paper.biblio.biblio;
      const link = page.locator(`[data-paper-row='${id}'] [data-open-full-text]`);
      if (doiUrl === null) await expect(link).toHaveCount(0);
      else {
        await expect(link).toHaveAttribute("href", doiUrl);
        await expect(link).toHaveAttribute("target", "_blank");
      }
    }
    // 主題標籤：同一頁打開，只帶這一個主題。
    const tag = page.locator("[data-paper-row] [data-topic-link]").first();
    const tagName = (await tag.textContent())!.trim();
    const key = NAMES.topics.find((topic) => topic.name === tagName)!.key;
    await tag.click();
    await expect(page).toHaveURL(`${demo.baseURL}/papers?topic=${key}`);
    await expectCount(page, count((at) => INDEX.topics[at]?.includes(key) === true));
  });

  test("filters run in the browser and are written into the address", async ({ page }) => {
    await open(page);
    await expectCount(page, TOTAL);
    // 讀完檔之後，換條件、再載入都不再發請求（篩選在瀏覽器裡做，AD-20）。字型之類的資源不算，只看 fetch／XHR。
    const later: string[] = [];
    page.on("request", (request) => {
      if (["fetch", "xhr"].includes(request.resourceType())) later.push(request.url());
    });
    const pressed = (label: string) => page.getByRole("button", { name: label, exact: true });
    const withAbstract = text("demo:demo.papers.withAbstract");
    const withFull = text("demo:demo.papers.withFull");

    await pressed(text("demo:demo.filter.abstractOnly")).click();
    await expect(page).toHaveURL(/[?&]abstract=true/);
    await expectCount(page, count((at) => INDEX.has_abstract[at]!));
    await expect(page.locator("h1")).toHaveText(heading([withAbstract], count((at) => INDEX.has_abstract[at]!)));
    await expect(pressed(text("demo:demo.filter.abstractOnly"))).toHaveAttribute("aria-pressed", "true");

    await pressed(text("demo:demo.filter.fullOnly")).click();
    await expect(page).toHaveURL(/[?&]full=true/);
    const both = (at: number) => INDEX.has_abstract[at]! && INDEX.has_full_content[at]!;
    await expectCount(page, count(both));
    await expect(page.locator("h1")).toHaveText(heading([withAbstract, withFull], count(both)));

    // 期刊（多選是「或」）。
    await page.locator("[data-filter-chip]").filter({ hasText: text("search:search.filter.journal") }).click();
    const misq = name(NAMES.journals, "misq");
    const io = name(NAMES.journals, "io");
    await page.getByRole("checkbox", { name: misq }).check();
    await page.getByRole("checkbox", { name: io }).check();
    await page.keyboard.press("Escape");
    await expect(page).toHaveURL(/[?&]journal=misq(%2C|,)io/);
    const journalMatch = (at: number) => ["misq", "io"].includes(INDEX.journal[at] ?? "");
    await expectCount(page, count((at) => journalMatch(at) && both(at)));
    // 一個欄位選了好幾個：用分隔的標點接起來（自訂的寫法，列給 Kevin）。
    await expect(page.locator("h1")).toHaveText(heading([[misq, io].join(text("demo:demo.papers.separator")), withAbstract, withFull], count((at) => journalMatch(at) && both(at))));

    // 標題裡的字（不分大小寫、字面比對）；也寫進網址。
    await page.locator("[data-title-query]").fill("PAPER 5");
    await expect(page).toHaveURL(/[?&]title=PAPER(\+|%20)5/);
    const titleMatch = (at: number) => TITLES[at]!.toLowerCase().includes("paper 5");
    const titled = count((at) => titleMatch(at) && journalMatch(at) && both(at));
    expect(titled).toBeGreaterThan(0);
    await expectCount(page, titled);
    await expect(page.locator("h1")).toHaveText(
      heading([fill(text("demo:demo.papers.titleHas"), { query: "PAPER 5" }), [misq, io].join(text("demo:demo.papers.separator")), withAbstract, withFull], titled),
    );
    expect(later).toEqual([]);

    // 網址帶著條件重新打開，畫面一樣。
    const address = page.url();
    await page.goto(address);
    await expect(page.locator("[data-title-query]")).toHaveValue("PAPER 5");
    await expect(pressed(text("demo:demo.filter.fullOnly"))).toHaveAttribute("aria-pressed", "true");

    // 「清除篩選」只留標題的字。
    await page.locator("[data-clear-all]:visible").click();
    await expect(page).toHaveURL(`${demo.baseURL}/papers?title=PAPER+5`);
    await expectCount(page, count(titleMatch));
  });

  test("the filter row has the journal, year, topic and method menus, and each one filters", async ({ page }) => {
    await open(page);
    await expectCount(page, TOTAL);
    const later: string[] = [];
    page.on("request", (request) => {
      if (["fetch", "xhr"].includes(request.resourceType())) later.push(request.url());
    });
    const chip = (label: string) => page.locator("[data-filter-chip]").filter({ hasText: label });
    // 四個選單、兩個「只看…的」、標題的字都在篩選列上。
    for (const label of [
      text("search:search.filter.journal"),
      text("search:search.filter.year"),
      text("search:search.filter.topic"),
      text("paper:paper.classification.method"),
    ]) {
      await expect(chip(label)).toBeVisible();
    }
    await expect(page.locator("[data-toggle-chip]")).toHaveCount(2);
    await expect(page.locator("[data-title-query]")).toBeVisible();

    // 主題：範例裡最多篇的那一個。
    const topicCounts = NAMES.topics.map(({ key }) => [key, count((at) => INDEX.topics[at]?.includes(key) === true)] as const);
    const [topic] = topicCounts.reduce((best, entry) => (entry[1] > best[1] ? entry : best));
    const topicMatch = (at: number) => INDEX.topics[at]?.includes(topic) === true;
    await chip(text("search:search.filter.topic")).click();
    await page.getByRole("checkbox", { name: name(NAMES.topics, topic), exact: true }).check();
    await page.keyboard.press("Escape");
    await expect(page).toHaveURL(new RegExp(`[?&]topic=${topic}(&|$)`));
    await expectCount(page, count(topicMatch));
    await expect(page.locator("h1")).toHaveText(heading([name(NAMES.topics, topic)], count(topicMatch)));

    // 方法：這個主題裡最多篇的那一個。
    const methodCounts = NAMES.methods.map(
      ({ key }) => [key, count((at) => topicMatch(at) && INDEX.methods[at]?.includes(key) === true)] as const,
    );
    const [method] = methodCounts.reduce((best, entry) => (entry[1] > best[1] ? entry : best));
    const methodMatch = (at: number) => topicMatch(at) && INDEX.methods[at]?.includes(method) === true;
    expect(count(methodMatch)).toBeGreaterThan(0);
    await chip(text("paper:paper.classification.method")).click();
    await page.getByRole("checkbox", { name: name(NAMES.methods, method), exact: true }).check();
    await page.keyboard.press("Escape");
    await expect(page).toHaveURL(new RegExp(`[?&]method=${method}(&|$)`));
    await expectCount(page, count(methodMatch));
    await expect(page.locator("h1")).toHaveText(heading([name(NAMES.topics, topic), name(NAMES.methods, method)], count(methodMatch)));

    // 年份：「近 3 年」；起訖年從網址讀、再和今年核對，篇數照範例檔算。
    await chip(text("search:search.filter.year")).click();
    // 整列是 label，點在 label 上（輸入框上面蓋著選項的字；同找論文的 e2e）。
    await page.locator("label").filter({ has: page.getByRole("radio", { name: text("search:search.year.last3") }) }).click();
    await page.keyboard.press("Escape");
    await expect(page).toHaveURL(/[?&]from=\d{4}&to=\d{4}/);
    const address = new URL(page.url()).searchParams;
    const from = Number(address.get("from"));
    const to = Number(address.get("to"));
    // 「近 3 年」獨立核對：到今年（台北時間）、共 3 個年份。
    const thisYear = Number(new Intl.DateTimeFormat("en", { timeZone: "Asia/Taipei", year: "numeric" }).format(new Date()));
    expect(to).toBe(thisYear);
    expect(to - from).toBe(2);
    const yearMatch = (at: number) => methodMatch(at) && INDEX.year[at] !== null && from <= INDEX.year[at]! && INDEX.year[at]! <= to;
    await expectCount(page, count(yearMatch));
    const years = fill(text("demo:demo.papers.years"), { years: from === to ? String(from) : `${from}–${to}` });
    if (count(yearMatch) === 0) await expect(page.locator("[data-state='zero']")).toBeVisible();
    await expect(page.locator("h1")).toHaveText(heading([name(NAMES.topics, topic), name(NAMES.methods, method), years], count(yearMatch)));
    expect(later).toEqual([]);
  });

  test("the brand and the nav link go to the paper list", async ({ page }) => {
    await open(page, "?abstract=true");
    await expect(rows(page).first()).toBeVisible();
    const papers = page.locator("header nav a").filter({ hasText: text("demo:demo.header.papers") });
    await expect(papers).toHaveAttribute("href", "/papers");
    // 帶著篩選條件時也標成目前這一頁。
    await expect(papers).toHaveAttribute("data-status", "active");
    await expect(page.locator("header [data-brand]")).toHaveAttribute("href", "/papers");
    await page.locator("header [data-brand]").click();
    await expect(page).toHaveURL(`${demo.baseURL}/papers`);
    await expectCount(page, TOTAL);
    await open(page, "?abstract=true");
    await papers.click();
    await expect(page).toHaveURL(`${demo.baseURL}/papers`);
    await expectCount(page, TOTAL);
  });

  test("this week comes in as a removable condition", async ({ page }) => {
    await open(page, "?this_week=true");
    await expect(page.locator("h1")).toContainText(text("search:search.condition.thisWeek"));
    await expectCount(page, count((at) => INDEX.is_new_this_week[at]!));
    await page.locator("[data-condition='this_week']:visible button").click();
    await expect(page).toHaveURL(`${demo.baseURL}/papers`);
    await expectCount(page, TOTAL);
  });

  test("the phone filter sheet holds the same conditions", async ({ page }) => {
    await open(page, "", PHONE);
    await expectCount(page, TOTAL);
    await page.getByRole("button", { name: text("search:search.mobile.filter"), exact: true }).click();
    await page.getByRole("checkbox", { name: text("demo:demo.filter.abstractOnly") }).check();
    await expect(page).toHaveURL(/[?&]abstract=true/);
    await page.getByRole("button", { name: fill(text("search:search.mobile.apply"), { count: count((at) => INDEX.has_abstract[at]!) }) }).click();
    await expectCount(page, count((at) => INDEX.has_abstract[at]!));
  });

  test("phone: the sheet's clear filters link is not rebuilt when the conditions change", async ({ page }) => {
    // 審查 3.14 P2：「清除篩選」以前每次重繪都拆掉重建，停在上面的焦點會掉。
    await open(page, "?abstract=true", PHONE);
    await expectCount(page, count((at) => INDEX.has_abstract[at]!));
    await page.locator("[data-mobile-filter]").click();
    const sheet = page.getByRole("dialog");
    const clearAll = sheet.locator("[data-sheet-clear]");
    await expect(clearAll).toHaveText(text("search:search.filter.clearAll"));
    const before = await clearAll.elementHandle();
    await sheet.getByRole("checkbox", { name: text("demo:demo.filter.fullOnly") }).check();
    await expect(page).toHaveURL(/[?&]full=true/);
    const both = count((at) => INDEX.has_abstract[at]! && INDEX.has_full_content[at]!);
    await expect(sheet.getByRole("button", { name: fill(text("search:search.mobile.apply"), { count: both }) })).toBeVisible();
    expect(await before!.evaluate((element) => element.isConnected)).toBe(true);
    await clearAll.focus();
    await sheet.getByRole("checkbox", { name: text("demo:demo.filter.fullOnly") }).evaluate((element) => (element as HTMLInputElement).click());
    await expect(page).not.toHaveURL(/[?&]full=true/);
    await expect(clearAll).toBeFocused();
  });

  test("no match shows the zero state with clear filters", async ({ page }) => {
    // tem 只有一篇、沒有「實驗」：篩到 0 篇。
    expect(count((at) => INDEX.journal[at] === "tem" && INDEX.methods[at]?.includes("experiment") === true)).toBe(0);
    await open(page, "?journal=tem&method=experiment");
    await expect(page.locator("[data-state='zero']")).toContainText(text("search:search.empty.noMatch"));
    await expect(resultCount(page)).toHaveText(fill(text("demo:demo.papers.count"), { count: 0 }));
    await expect(rows(page)).toHaveCount(0);
    await page.locator("[data-state='zero'] [data-clear-all]").click();
    await expect(page).toHaveURL(`${demo.baseURL}/papers`);
    await expectCount(page, TOTAL);
  });

  test("only title words with no match has no clear filters link", async ({ page }) => {
    await open(page, "?title=zzzz-no-such-title");
    await expect(page.locator("[data-state='zero']")).toBeVisible();
    await expect(page.locator("[data-clear-all]")).toHaveCount(0);
  });

  test("loading shows the skeleton and the data date from the manifest", async ({ page }) => {
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    await page.route("**/preview/*/papers-index.json", async (route: Route) => {
      await held;
      await route.continue();
    });
    await open(page);
    await expect(page.locator("[data-state='loading']")).toBeVisible();
    await expect(page.locator("[data-state='loading']")).toHaveAttribute("aria-label", text("search:search.results.loading"));
    // State Patterns「7. 論文清單」：8 列論文列的骨架。
    await expect(page.locator("[data-state='loading'] > div")).toHaveCount(8);
    await expect(page.locator("[data-data-date]")).toBeVisible();
    await expect(page.locator("h1")).toHaveText(text("demo:demo.papers.all"));
    await expect(rows(page)).toHaveCount(0);
    release();
    await expectCount(page, TOTAL);
    await expect(page.locator("[data-state='loading']")).toHaveCount(0);
  });

  test("a file that fails shows load failed; refresh tries again", async ({ page }) => {
    let fail = true;
    await page.route("**/preview/*/titles.json", async (route: Route) => {
      if (fail) await route.fulfill({ status: 500, body: "" });
      else await route.continue();
    });
    await open(page);
    const error = page.locator("[data-state='error']");
    await expect(error).toContainText(text("common:states.loadFailed.message"));
    await expect(page.locator("[data-title-query]")).toHaveCount(0);
    await expect(page.locator("[data-filter-chip]")).toHaveCount(0);
    await expect(rows(page)).toHaveCount(0);
    fail = false;
    await error.getByRole("button", { name: text("common:states.loadFailed.action") }).click();
    await expectCount(page, TOTAL);
  });

  test("a manifest that cannot be read, or a file of another export, also fails", async ({ page }) => {
    await page.route("**/preview/manifest.json", (route: Route) => route.fulfill({ status: 404, body: "" }));
    await open(page);
    await expect(page.locator("[data-state='error']")).toContainText(text("common:states.loadFailed.message"));
    await expect(page.locator("[data-data-date]")).toHaveCount(0);

    await page.unroute("**/preview/manifest.json");
    await page.route("**/preview/*/taxonomy.json", async (route: Route) => {
      const response = await route.fetch();
      const body = (await response.json()) as Record<string, unknown>;
      await route.fulfill({ response, json: { ...body, export_id: "9-9" } });
    });
    await page.reload();
    await expect(page.locator("[data-state='error']")).toContainText(text("common:states.loadFailed.message"));
  });
});

for (const width of [1440, 1000, 500]) {
  test(`demo paper list screenshot at ${width}px`, async ({ page, browserName }) => {
    test.skip(browserName !== "chromium", "screenshots are taken in chromium only");
    await open(page, "?abstract=true", { width, height: 900 });
    await expect(rows(page).first()).toBeVisible();
    // 沒有橫向捲動。
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `${SCREENSHOT_DIR}demo-papers-${width}.png`, fullPage: true });
  });
}
