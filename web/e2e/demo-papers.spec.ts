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
const NAMES = json<{ names: { journals: { key: string; name: string }[]; topics: { key: string; name: string }[] } }>(
  `${MANIFEST.export_id}/taxonomy.json`,
).names;
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

    // 每列都有「● 有完整內容」或「○ 只有書目」，前 50 篇的數目和範例檔一樣。
    const shownIds = await rows(page).evaluateAll((items) => items.map((item) => item.getAttribute("data-paper-row")));
    const full = shownIds.filter((id) => INDEX.has_full_content[INDEX.id.indexOf(id!)]).length;
    await expect(page.locator("[data-paper-row] [data-full-content='yes']")).toHaveCount(full);
    await expect(page.locator("[data-paper-row] [data-full-content='no']")).toHaveCount(PAGE_SIZE - full);
    await expect(page.locator("[data-paper-row] [data-full-content='yes']").first()).toHaveText(text("demo:demo.row.full"));
    await expect(page.locator("[data-paper-row] [data-full-content='no']").first()).toHaveText(text("demo:demo.row.biblioOnly"));

    // 依年份新到舊。
    const years = shownIds.map((id) => INDEX.year[INDEX.id.indexOf(id!)] ?? 0);
    expect(years).toEqual([...years].sort((a, b) => b - a));

    // 「再載入 50 篇」：接著顯示，目前顯示幾篇寫進網址；全部顯示後按鈕不見。
    await expect(page.locator("[data-shown]")).toHaveText(fill(text("search:search.results.shown"), { shown: PAGE_SIZE, total: TOTAL }));
    await page.locator("[data-load-more]").click();
    await expect(rows(page)).toHaveCount(TOTAL);
    await expect(page).toHaveURL(/[?&]shown=100(&|$)/);
    await expect(page.locator("[data-load-more]")).toHaveCount(0);
    // 重新整理後還是顯示同樣多篇。
    await page.reload();
    await expect(rows(page)).toHaveCount(TOTAL);
  });

  test("row titles are not links yet; open full text goes to the DOI; tags filter the list", async ({ page }) => {
    await open(page);
    await expect(rows(page)).toHaveCount(PAGE_SIZE);
    // 論文頁（Story 3.9）還沒做（AR-67）：標題不是連結。
    await expect(page.locator("[data-paper-row] h3 a")).toHaveCount(0);
    // 開啟全文的連結和分片裡後端寫的 doi_url 一樣、開新分頁。
    for (const paper of SHARD) {
      const { id, doi_url: doiUrl } = paper.biblio.biblio;
      const link = page.locator(`[data-paper-row='${id}'] [data-open-full-text]`);
      if ((await page.locator(`[data-paper-row='${id}']`).count()) === 0) continue;
      if (doiUrl === null) await expect(link).toHaveCount(0);
      else {
        await expect(link).toHaveAttribute("href", doiUrl);
        await expect(link).toHaveAttribute("target", "_blank");
      }
    }
    // 主題標籤：同一頁打開，只帶這一個主題。
    const tag = page.locator("[data-paper-row] [data-topic-link]").first();
    const name = (await tag.textContent())!.trim();
    const key = NAMES.topics.find((topic) => topic.name === name)!.key;
    await tag.click();
    await expect(page).toHaveURL(`${demo.baseURL}/papers?topic=${key}`);
    await expectCount(page, count((at) => INDEX.topics[at]?.includes(key) === true));
  });

  test("filters run in the browser and are written into the address", async ({ page }) => {
    await open(page);
    await expectCount(page, TOTAL);
    const pressed = (name: string) => page.getByRole("button", { name, exact: true });

    await pressed(text("demo:demo.filter.abstractOnly")).click();
    await expect(page).toHaveURL(/[?&]abstract=true/);
    await expectCount(page, count((at) => INDEX.has_abstract[at]!));
    await expect(pressed(text("demo:demo.filter.abstractOnly"))).toHaveAttribute("aria-pressed", "true");

    await pressed(text("demo:demo.filter.fullOnly")).click();
    await expect(page).toHaveURL(/[?&]full=true/);
    await expectCount(page, count((at) => INDEX.has_abstract[at]! && INDEX.has_full_content[at]!));

    // 期刊（多選是「或」）。
    await page.locator("[data-filter-chip]").filter({ hasText: text("search:search.filter.journal") }).click();
    const misq = NAMES.journals.find((journal) => journal.key === "misq")!.name;
    const io = NAMES.journals.find((journal) => journal.key === "io")!.name;
    await page.getByRole("checkbox", { name: misq }).check();
    await page.getByRole("checkbox", { name: io }).check();
    await page.keyboard.press("Escape");
    await expect(page).toHaveURL(/[?&]journal=misq(%2C|,)io/);
    const journalMatch = (at: number) => ["misq", "io"].includes(INDEX.journal[at] ?? "");
    await expectCount(page, count((at) => journalMatch(at) && INDEX.has_abstract[at]! && INDEX.has_full_content[at]!));

    // 標題裡的字（不分大小寫、字面比對）；也寫進網址。
    await page.locator("[data-title-query]").fill("PAPER 5");
    await expect(page).toHaveURL(/[?&]title=PAPER(\+|%20)5/);
    const titleMatch = (at: number) => TITLES[at]!.toLowerCase().includes("paper 5");
    await expectCount(page, count((at) => titleMatch(at) && journalMatch(at) && INDEX.has_abstract[at]! && INDEX.has_full_content[at]!));

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
