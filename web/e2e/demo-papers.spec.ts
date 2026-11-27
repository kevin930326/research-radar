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

    // 標題關鍵字（不分大小寫、字面比對）；也寫進網址。
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
    // 四個選單、「有摘要」「有完整內容」、標題關鍵字都在篩選列上。
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

  test("Esc on one menu and opening the next right away: the new menu stays open with the focus in it", async ({ page }) => {
    // 上一個選單的關閉動畫（0.2 秒）結束時才還焦點；那時已經開了下一個，焦點不能被拉回上一個標籤，
    // 不然剛開的選單會關掉、或焦點停在錯的標籤上（epic-3j 查到的，FilterMenus.tsx 的 finalFocus）。
    await open(page);
    await expectCount(page, TOTAL);
    const chip = (label: string) => page.locator("[data-filter-chip]").filter({ hasText: label });
    const firstTopic = page.getByRole("checkbox", { name: NAMES.topics[0]!.name, exact: true });
    const firstMethod = page.getByRole("checkbox", { name: NAMES.methods[0]!.name, exact: true });
    await chip(text("search:search.filter.topic")).click();
    await firstTopic.check();
    await expect(page).toHaveURL(new RegExp(`[?&]topic=${NAMES.topics[0]!.key}(&|$)`));
    await page.keyboard.press("Escape");
    await chip(text("paper:paper.classification.method")).click();
    // 等上一個選單拆掉（還焦點就在這一刻），剛開的選單還在、焦點在它裡面。
    await expect(firstTopic).toHaveCount(0);
    const methodMenu = page.locator("[role='dialog']").filter({ has: firstMethod });
    await expect(methodMenu).toBeVisible();
    await expect(methodMenu).not.toHaveAttribute("data-ending-style");
    await expect.poll(() => methodMenu.evaluate((menu) => menu.contains(document.activeElement))).toBe(true);
    await expect(page.locator("[role='dialog']")).toHaveCount(1);
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

// Story 3.20：主題、方法多選的「任一個／同時符合」（設計稿 filter-match-mode.html 的示範站）。預期的篇數直接從範例檔數：
// 「或」＝有任一個 key、「且」＝每個 key 都有，不同欄位之間「且」（和成員版的後端同一個定義）。
test.describe("demo paper list: any or all (Story 3.20)", () => {
  const has = (field: "topics" | "methods", at: number, keys: string[], mode: "any" | "all") => {
    const own = INDEX[field][at];
    if (own === null || own === undefined) return false;
    return mode === "all" ? keys.every((key) => own.includes(key)) : keys.some((key) => own.includes(key));
  };
  // ERP 和知識管理與協作沒有同一篇：任一個有論文、同時符合 0 篇。方法：問卷／SEM 和實驗也是。
  const TOPICS = ["erp", "knowledge_collaboration"];
  const METHODS = ["survey_sem", "experiment"];
  const topicNames = TOPICS.map((key) => name(NAMES.topics, key));
  const methodNames = METHODS.map((key) => name(NAMES.methods, key));
  const chip = (page: Page, label: string) => page.locator("[data-filter-chip]").filter({ hasText: label });
  const switchOf = (scope: Page | ReturnType<Page["getByRole"]>, field: "topic" | "method") => scope.locator(`[data-match-switch='${field}']`);
  const modeButton = (scope: Page | ReturnType<Page["getByRole"]>, field: "topic" | "method", mode: "any" | "all") =>
    switchOf(scope, field).getByRole("button", { name: text(mode === "any" ? "search:search.filter.matchAny" : "search:search.filter.matchAll") });
  /** 選項（整列是 label，點在 label 上；輸入框上面蓋著選項的字，同找論文的 e2e）。 */
  const option = (scope: Page | ReturnType<Page["getByRole"]>, label: string) =>
    scope.getByRole("checkbox", { name: label, exact: true }).locator("xpath=ancestor::label[1]");
  const joined = (names: string[], mode: "any" | "all") => names.join(text(mode === "any" ? "demo:demo.papers.anyJoin" : "demo:demo.papers.allJoin"));

  test("the sample export has papers for any and none for all", () => {
    expect(count((at) => has("topics", at, TOPICS, "any"))).toBeGreaterThan(0);
    expect(count((at) => has("topics", at, TOPICS, "all"))).toBe(0);
    expect(count((at) => has("methods", at, METHODS, "any"))).toBeGreaterThan(0);
    expect(count((at) => has("methods", at, METHODS, "all"))).toBe(0);
  });

  test("desktop: topics are any by default, the switch makes them all, the title joins them with the any or all word, and the zero link goes back", async ({ page }) => {
    const anyCount = count((at) => has("topics", at, TOPICS, "any"));
    await open(page, `?topic=${TOPICS[0]}`);
    await chip(page, text("search:search.filter.topic")).click();
    await expect(page.locator("[data-match-switch]")).toHaveCount(0);
    await option(page, topicNames[1]!).click();
    await expect(page.getByRole("checkbox", { name: topicNames[1]!, exact: true })).toBeChecked();
    await expect(modeButton(page, "topic", "any")).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator("h1")).toHaveText(heading([joined(topicNames, "any")], anyCount));
    await expect(chip(page, text("search:search.filter.topic"))).toContainText(fill(text("search:search.filter.topicCount"), { count: 2 }));
    // 「或」是第二層字色（設計稿 .cj）。
    const word = page.locator("h1 span", { hasText: text("demo:demo.papers.anyJoin").trim() }).last();
    await expect(word).toHaveCSS("color", "rgb(168, 162, 148)");
    await expect(rows(page)).toHaveCount(Math.min(anyCount, PAGE_SIZE));

    await modeButton(page, "topic", "all").click();
    await expect(page).toHaveURL(/[?&]topic_match=all(&|$)/);
    await expect(modeButton(page, "topic", "all")).toBeFocused();
    await expect(chip(page, text("search:search.filter.topic"))).toContainText(fill(text("search:search.filter.matchAllCount"), { count: 2 }));
    await expect(page.locator("h1")).toHaveText(heading([joined(topicNames, "all")], 0));
    await page.keyboard.press("Escape");

    const zero = page.locator("[data-state='zero']");
    await expect(zero).toBeVisible();
    const link = zero.getByRole("link", { name: fill(text("search:search.empty.anyInstead"), { count: anyCount }) });
    expect(await zero.getByRole("link").allTextContents()).toEqual([
      fill(text("search:search.empty.anyInstead"), { count: anyCount }),
      text("search:search.filter.clearAll"),
    ]);
    expect((await link.boundingBox())!.height).toBeGreaterThanOrEqual(44);

    // 重新整理還是同時符合。
    await page.reload();
    await expect(page.locator("h1")).toHaveText(heading([joined(topicNames, "all")], 0));
    await link.click();
    await expect(page).not.toHaveURL(/topic_match/);
    await expectCount(page, anyCount);
    await expect(page.locator("h1")).toBeFocused();
  });

  test("desktop: methods have their own switch, independent of the topic one; journals keep the list separator", async ({ page }) => {
    const methodAny = (at: number) => has("methods", at, METHODS, "any");
    await open(page, `?method=${METHODS.join(",")}`);
    await expectCount(page, count(methodAny));
    await expect(page.locator("h1")).toHaveText(heading([joined(methodNames, "any")], count(methodAny)));
    await chip(page, text("paper:paper.classification.method")).click();
    await expect(switchOf(page, "topic")).toHaveCount(0);
    await modeButton(page, "method", "all").click();
    await expect(page).toHaveURL(/[?&]method_match=all(&|$)/);
    await expect(page).not.toHaveURL(/topic_match/);
    await expect(page.locator("h1")).toHaveText(heading([joined(methodNames, "all")], 0));
    await expect(chip(page, text("paper:paper.classification.method"))).toContainText(fill(text("search:search.filter.matchAllCount"), { count: 2 }));
    // 取消一個方法：切換消失、網址上不留 method_match。
    await option(page, methodNames[1]!).click();
    await expect(page).not.toHaveURL(/method_match/);
    await expect(page.locator("[data-match-switch]")).toHaveCount(0);
    await page.keyboard.press("Escape");

    // 主題同時符合（兩個都有的那一組）、方法任一個：各算各的，欄位之間「且」。
    const both = ["digital_platforms", "e_commerce"];
    const expected = count((at) => has("topics", at, both, "all") && has("methods", at, METHODS, "any"));
    await open(page, `?topic=${both.join(",")}&topic_match=all&method=${METHODS.join(",")}`);
    await expectCount(page, expected);
    // 兩本期刊：照舊用頓號。
    const journals = ["misq", "io"];
    const inJournals = (at: number) => journals.includes(INDEX.journal[at] ?? "");
    await open(page, `?journal=${journals.join(",")}`);
    await expect(page.locator("h1")).toHaveText(
      heading([journals.map((key) => name(NAMES.journals, key)).join(text("demo:demo.papers.separator"))], count(inJournals)),
    );
  });

  test("a leftover all in the address is replaced on open and the list is any", async ({ page }) => {
    // 規則 3：不到兩個（一個 key、不認得的拿掉後剩一個）或沒有 key 時，網址列上不留 *_match（審查 B 的 F2）。
    await open(page, "?topic=erp&topic_match=all");
    await expect(page).toHaveURL(`${demo.baseURL}/papers?topic=erp`);
    await open(page, `?topic=${TOPICS[0]},gone&topic_match=all`);
    await expect(page).toHaveURL(new RegExp(`/papers\\?topic=${TOPICS[0]}(%2C|,)gone$`));
    await expectCount(page, count((at) => has("topics", at, [TOPICS[0]!], "any")));
    await open(page, "?method_match=all");
    await expect(page).toHaveURL(`${demo.baseURL}/papers`);
  });

  test("a topic the taxonomy no longer has does not keep all alive: dropping to one known topic goes back to any", async ({ page }) => {
    // 舊書籤：兩個認得的＋一個分類表已經沒有的，同時符合（審查 B 的 F1）。
    await open(page, `?topic=${TOPICS.join(",")},gone&topic_match=all`);
    await expect(chip(page, text("search:search.filter.topic"))).toContainText(fill(text("search:search.filter.matchAllCount"), { count: 2 }));
    await chip(page, text("search:search.filter.topic")).click();
    await option(page, topicNames[1]!).click();
    await expect(page).not.toHaveURL(/topic_match/);
    await expect(page).toHaveURL(/[?&]topic=erp(%2C|,)gone(&|$)/);
    await expect(page.locator("[data-match-switch]")).toHaveCount(0);
    await option(page, topicNames[1]!).click();
    await expect(modeButton(page, "topic", "any")).toHaveAttribute("aria-pressed", "true");
    await expect(page).not.toHaveURL(/topic_match/);
    await expectCount(page, count((at) => has("topics", at, TOPICS, "any")));
  });

  test("the switch fades in (0.25s, design .mm) unless reduced motion is on; with one topic the menu's bottom row is the plain one", async ({ page }) => {
    await open(page, `?topic=${TOPICS[0]}`);
    await chip(page, text("search:search.filter.topic")).click();
    // 只選一個：底部列照 papers-demo／search-no-box（上下 10px、只有「清除」；審查 B 的 F8）。
    const clear = page.getByRole("link", { name: text("search:search.filter.clear"), exact: true });
    await expect(clear.locator("xpath=..")).toHaveCSS("padding-top", "10px");
    await option(page, topicNames[1]!).click();
    const group = switchOf(page, "topic");
    await expect(group).toHaveCSS("animation-name", "match-fade");
    await expect(group).toHaveCSS("animation-duration", "0.25s");
    // 選了兩個：底部列換成 filter-match-mode 的 .mfoot（上下 6px）。
    await expect(clear.locator("xpath=..")).toHaveCSS("padding-top", "6px");
    await page.emulateMedia({ reducedMotion: "reduce" });
    await expect(group).toHaveCSS("animation-name", "none");
  });

  test("zero for all and zero for any too: no link", async ({ page }) => {
    // 範例匯出沒有 2016–2017 年的論文：任一個也是 0 篇。
    expect(count((at) => (INDEX.year[at] ?? 0) <= 2017 && has("topics", at, TOPICS, "any"))).toBe(0);
    await open(page, `?from=2016&to=2017&topic=${TOPICS.join(",")}&topic_match=all`);
    await expect(page.locator("[data-state='zero']")).toBeVisible();
    await expect(page.locator("[data-any-match]")).toHaveCount(0);
  });

  test("phone: the method switch sits above the apply button only while the method section is open", async ({ page }) => {
    const methodAny = count((at) => has("methods", at, METHODS, "any"));
    await open(page, `?method=${METHODS[0]}`, PHONE);
    await page.locator("[data-mobile-filter]").click();
    const sheet = page.getByRole("dialog");
    const section = sheet.locator("[data-filter-section='method'] > button");
    await section.click();
    await expect(sheet.locator("[data-match-switch]")).toHaveCount(0);
    await option(sheet, methodNames[1]!).click();
    await expect(sheet.getByRole("checkbox", { name: methodNames[1]!, exact: true })).toBeChecked();
    const apply = sheet.getByRole("button", { name: fill(text("search:search.mobile.apply"), { count: methodAny }) });
    await expect(apply).toBeVisible();
    await expect(section).toContainText(fill(text("search:search.filter.topicCount"), { count: 2 }));
    const [switchBox, applyBox] = await Promise.all([switchOf(sheet, "method").boundingBox(), apply.boundingBox()]);
    expect(switchBox!.y + switchBox!.height).toBeLessThanOrEqual(applyBox!.y);
    for (const mode of ["any", "all"] as const) {
      const box = await modeButton(sheet, "method", mode).boundingBox();
      expect(box!.width).toBeGreaterThanOrEqual(44);
      expect(box!.height).toBeGreaterThanOrEqual(44);
    }
    // 鍵盤也可以按。
    await modeButton(sheet, "method", "all").focus();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/[?&]method_match=all(&|$)/);
    await expect(modeButton(sheet, "method", "all")).toBeFocused();
    await expect(section).toContainText(fill(text("search:search.filter.matchAllCount"), { count: 2 }));
    await expect(sheet.getByRole("button", { name: fill(text("search:search.mobile.apply"), { count: 0 }) })).toBeVisible();
    await sheet.locator("[data-filter-section='topic'] > button").click();
    await expect(sheet.locator("[data-match-switch]")).toHaveCount(0);
    await sheet.getByRole("button", { name: fill(text("search:search.mobile.apply"), { count: 0 }) }).click();
    const link = page.locator("[data-state='zero']").getByRole("link", { name: fill(text("search:search.empty.anyInstead"), { count: methodAny }) });
    await link.click();
    await expect(page).not.toHaveURL(/method_match/);
    await expectCount(page, methodAny);
    await expect(page.locator("h1")).toBeFocused();
  });

  test("phone: the any link is pressed while the filter sheet is still sliding away: the focus stays on the heading", async ({ page }) => {
    // epic-3j 6-2／epic-3l CI 除錯（A-ci-debug.md）：上一條在 firefox、webkit 時好時壞，因為按「看 0 篇」之後滿版篩選要滑 0.25 秒
    // 才拆掉，連結按得太快時，拆掉那一刻焦點被拉回「篩選」按鈕、蓋掉標題（MobileFilters.tsx 的 finalFocus）。
    // 這裡固定做出「還在滑的時候就按了連結」：同一段腳本裡按「看 0 篇」再按連結，不靠機器快慢。
    const methodAny = count((at) => has("methods", at, METHODS, "any"));
    await open(page, `?method=${METHODS.join(",")}&method_match=all`, PHONE);
    await expect(page.locator("[data-state='zero']")).toBeVisible();
    await page.locator("[data-mobile-filter]").click();
    const sheet = page.getByRole("dialog");
    const apply = sheet.getByRole("button", { name: fill(text("search:search.mobile.apply"), { count: 0 }) });
    await expect(apply).toBeVisible();
    const label = fill(text("search:search.mobile.apply"), { count: 0 });
    const pressed = await page.evaluate((name) => {
      [...document.querySelectorAll<HTMLButtonElement>("[role=dialog] button")].find((button) => button.textContent?.trim() === name)!.click();
      document.querySelector<HTMLElement>("[data-state='zero'] [data-any-match]")!.click();
      // 滿版篩選還在（還沒拆掉、還沒還焦點），焦點已經在標題上。
      return document.querySelector("[role=dialog]") !== null && document.activeElement?.matches("h1[data-page-heading]") === true;
    }, label);
    expect(pressed).toBe(true);
    // 連隱藏的一起算：滿版篩選真的拆掉了才看焦點（epic-3l C 審查 X8）。
    await expect(page.locator("[role=dialog]")).toHaveCount(0);
    await expect(page).not.toHaveURL(/method_match/);
    await expectCount(page, methodAny);
    await expect(page.locator("h1")).toBeFocused();
  });
});

// 篩選選單早就有的畫面問題（epic-3k 量到、epic-3l PR E 修；設計稿 filter-match-mode.html 示範站桌機與手機、papers-demo.html）。
test.describe("demo paper list: filter menu layout", () => {
  /** 打開的選單（Base UI Popover 的 popup）與它的標籤看得到的那一塊（按鈕本身比它上下各多 6px）。 */
  async function openTopicMenu(page: Page) {
    await open(page, "?topic=erp");
    const chip = page.locator("[data-filter-chip]").filter({ hasText: text("search:search.filter.topic") });
    await chip.click();
    const menu = page.getByRole("dialog");
    // 整列是 label，點在 label 上（輸入框上面蓋著選項的字；同上面的年份）。
    await menu.locator("label").filter({ has: page.getByRole("checkbox", { name: name(NAMES.topics, "knowledge_collaboration"), exact: true }) }).click();
    await expect(page).toHaveURL(/[?&]topic=erp(%2C|,)knowledge_collaboration/);
    // 選單淡入的動畫走完才量位置。
    await page.evaluate(() => Promise.all(document.getAnimations().map((animation) => animation.finished.catch(() => undefined))));
    return { menu, chip: chip.locator(":scope > span") };
  }

  test("desktop: the topic menu opens below the chip, 490px wide in two columns, not over the heading", async ({ page }) => {
    const { menu, chip } = await openTopicMenu(page);
    const menuBox = (await menu.boundingBox())!;
    const chipBox = (await chip.boundingBox())!;
    const headingBox = (await page.locator("h1").boundingBox())!;
    // 從篩選鈕下方展開（設計稿 .menu.fl top: 40px，標籤 32px 高），左邊和標籤對齊；不蓋到標題。
    expect(menuBox.y).toBeGreaterThanOrEqual(chipBox.y + chipBox.height);
    expect(Math.round(menuBox.y - chipBox.y)).toBe(40);
    expect(Math.round(menuBox.x)).toBe(Math.round(chipBox.x));
    expect(menuBox.y).toBeGreaterThan(headingBox.y + headingBox.height);
    expect(Math.round(menuBox.width)).toBe(490);
    // 19 個主題排成兩欄；「未分類」在兩欄下面。
    const unclassified = text("paper:paper.classification.unclassified");
    const topicBoxes = await Promise.all(
      NAMES.topics.map(({ name: topicName }) => menu.getByRole("checkbox", { name: topicName, exact: true }).locator("xpath=ancestor::label[1]").boundingBox()),
    );
    const columns = new Set(topicBoxes.map((box) => Math.round(box!.x)));
    expect(columns.size).toBe(2);
    const lowest = Math.max(...topicBoxes.map((box) => box!.y + box!.height));
    const other = (await menu.getByRole("checkbox", { name: unclassified, exact: true }).locator("xpath=ancestor::label[1]").boundingBox())!;
    expect(other.y).toBeGreaterThanOrEqual(lowest);
  });

  test("desktop: the menu background is opaque (no text shows through)", async ({ page }) => {
    const { menu } = await openTopicMenu(page);
    const alpha = await menu.evaluate((element) => {
      const color = getComputedStyle(element).backgroundColor;
      const parts = color.match(/[\d.]+/g)!.map(Number);
      return parts.length === 4 ? parts[3] : 1;
    });
    expect(alpha).toBe(1);
  });

  test("phone: scrolled to the bottom, the sheet's title and close button stay on screen; unclassified has a hairline above it", async ({ page }) => {
    await open(page, "?topic=erp", PHONE);
    await page.getByRole("button", { name: fill(text("search:search.mobile.filterCount"), { count: 1 }), exact: true }).click();
    const sheet = page.getByRole("dialog");
    await sheet.getByRole("button", { name: text("search:search.filter.topic") }).click();
    const unclassified = sheet.getByRole("checkbox", { name: text("paper:paper.classification.unclassified"), exact: true });
    await expect(unclassified).toBeAttached();
    // 「未分類」上面一條髮線（設計稿 optsOf：手機與桌機同一段）。
    const hairline = await unclassified.locator("xpath=ancestor::label[1]/..").evaluate((element) => getComputedStyle(element).borderTopWidth);
    expect(hairline).toBe("1px");
    await page.evaluate(() => Promise.all(document.getAnimations().map((animation) => animation.finished.catch(() => undefined))));
    const scrolled = await sheet.evaluate((element) => {
      element.scrollTop = element.scrollHeight;
      return element.scrollTop;
    });
    // 真的捲動了，這條測試才有測到。
    expect(scrolled).toBeGreaterThan(0);
    for (const target of [sheet.getByRole("heading", { name: text("search:search.mobile.filter") }), sheet.getByRole("button", { name: text("search:search.mobile.close") })]) {
      const box = (await target.boundingBox())!;
      expect(box.y).toBeGreaterThanOrEqual(0);
      expect(box.y + box.height).toBeLessThanOrEqual(PHONE.height);
    }
  });

  test("desktop: a menu too tall for the window still opens below its chip, not beside it", async ({ page }) => {
    // 期刊選單 490px 一欄、14 本，在 1280×900 放不下；沒有「一律在下方」的設定時會翻到旁邊、蓋住別的標籤（epic-3l PR E 審查 Y2）。
    await open(page);
    const chip = page.locator("[data-filter-chip]").filter({ hasText: text("search:search.filter.journal") });
    await chip.click();
    const menu = page.getByRole("dialog");
    await expect(menu).toBeVisible();
    await page.evaluate(() => Promise.all(document.getAnimations().map((animation) => animation.finished.catch(() => undefined))));
    const menuBox = (await menu.boundingBox())!;
    const chipBox = (await chip.locator(":scope > span").boundingBox())!;
    // 放在標籤下方 40px 時真的放不下，這條測試才有測到（和選單實際放在哪裡無關）。
    expect(chipBox.y + 40 + menuBox.height).toBeGreaterThan(DESKTOP.height);
    expect(Math.round(menuBox.y - chipBox.y)).toBe(40);
    expect(Math.round(menuBox.x)).toBe(Math.round(chipBox.x));
  });

  test("phone: moving through the sheet with Tab, the focused control is never hidden under the fixed top or bottom bar", async ({ page, browserName }) => {
    // webkit 的 Tab 預設只走表單元件，Alt+Tab 才走全部（同 a11y-floor.spec.ts）。
    const forward = browserName === "webkit" ? "Alt+Tab" : "Tab";
    const backward = browserName === "webkit" ? "Alt+Shift+Tab" : "Shift+Tab";
    await open(page, "?topic=erp", PHONE);
    await page.getByRole("button", { name: fill(text("search:search.mobile.filterCount"), { count: 1 }), exact: true }).click();
    const sheet = page.getByRole("dialog");
    await sheet.getByRole("button", { name: text("search:search.filter.topic") }).click();
    await expect(sheet.getByRole("checkbox", { name: text("paper:paper.classification.unclassified"), exact: true })).toBeAttached();
    await page.evaluate(() => Promise.all(document.getAnimations().map((animation) => animation.finished.catch(() => undefined))));
    const first = sheet.getByRole("button", { name: text("search:search.filter.journal") });
    const last = sheet.getByRole("button", { name: fill(text("search:search.mobile.apply"), { count: count((at) => INDEX.topics[at]?.includes("erp") === true) }) });
    /** 焦點所在的元件被固定的頂部或底部那一列蓋住多少（頂部、底部裡面的元件不算）；回傳 null 是沒被蓋住。 */
    const covered = () =>
      page.evaluate(() => {
        const active = document.activeElement as HTMLElement;
        const popup = active.closest<HTMLElement>("[role=dialog]")!;
        const bars = [...popup.querySelectorAll<HTMLElement>("*")].filter((element) => getComputedStyle(element).position === "sticky");
        const head = bars[0]!;
        const foot = bars[bars.length - 1]!;
        if (head.contains(active) || foot.contains(active)) return null;
        const box = active.getBoundingClientRect();
        const top = head.getBoundingClientRect().bottom;
        const bottom = foot.getBoundingClientRect().top;
        if (box.top >= top - 0.5 && box.bottom <= bottom + 0.5) return null;
        const label = (active.closest("label") ?? active).textContent?.trim() || active.getAttribute("aria-label");
        return `${label} ${Math.round(box.top)}-${Math.round(box.bottom)} (visible ${Math.round(top)}-${Math.round(bottom)}, scrollTop ${popup.scrollTop})`;
      });
    const hidden: string[] = [];
    let scrolled = 0;
    await first.focus();
    for (const [key, end] of [
      [forward, last],
      [backward, first],
    ] as const) {
      for (let step = 0; step < 100; step += 1) {
        await page.keyboard.press(key);
        const where = await covered();
        if (where !== null) hidden.push(`${key}: ${where}`);
        scrolled = Math.max(scrolled, await sheet.evaluate((element) => element.scrollTop));
        if (await end.evaluate((element) => element === document.activeElement)) break;
      }
      await expect(end).toBeFocused();
    }
    // 真的捲動了，這條測試才有測到。
    expect(scrolled).toBeGreaterThan(0);
    expect(hidden).toEqual([]);
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
