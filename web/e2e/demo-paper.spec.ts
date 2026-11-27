// 示範站的論文頁（Story 3.9；EXPERIENCE.md 畫面 8「示範站的論文頁有三種」、State Patterns「8. 論文頁」與「全站共用」）：
// 讀 preview/ 的範例匯出（fixtures/demo-preview，自己編的假論文）。三種示範站版本的字、沒有成員的功能、
// 「沒有產生成功」只寫這一句、分類標籤與「看有完整內容的論文」連到論文清單、找不到與載入失敗、
// 1440／1000／500 三種寬度的整頁截圖。預期的值從範例檔讀，不用畫面的程式。
// 論文清單的標題連到論文頁：既有的兩條測試（a11y-floor 的 Tab 上限、links 的落點清單）擋住，這個 PR 先不做（列給 Kevin）。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import type { Page, Route } from "@playwright/test";

import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { MISSING_PATH, SITES, demoPaperId } from "./pages.ts";

const demo = SITES[1]!;
const SCREENSHOT_DIR = fileURLToPath(new URL("./screenshots/", import.meta.url));
const FIXTURE = fileURLToPath(new URL("./fixtures/demo-preview/", import.meta.url));
const DESKTOP = { width: 1280, height: 900 };

function json<T>(path: string): T {
  return JSON.parse(readFileSync(`${FIXTURE}${path}`, "utf-8")) as T;
}

interface RawPaper {
  biblio: {
    biblio: { id: string; title: string; doi_url: string | null };
    classification: { value: { topic: { keys: string[] } | null } | null } | null;
    has_full_content: boolean;
    demo_eligible: boolean;
    has_abstract: boolean;
  };
}
const MANIFEST = json<{ export_id: string }>("manifest.json");
const SHARD = json<{ papers: RawPaper[] }>(`${MANIFEST.export_id}/papers/000.json`).papers;
const INDEX = json<{ has_full_content: boolean[] }>(`${MANIFEST.export_id}/papers-index.json`);

function text(key: string): string {
  const value = TEXT[key];
  if (value === undefined) throw new Error(`no expected text ${key}`);
  return value;
}

function raw(number: number): RawPaper {
  const id = demoPaperId(number);
  return SHARD.find((paper) => paper.biblio.biblio.id === id)!;
}

async function openPaper(page: Page, number: number, viewport = DESKTOP): Promise<RawPaper> {
  await page.setViewportSize(viewport);
  await page.goto(`${demo.baseURL}/papers/${demoPaperId(number)}`);
  const paper = raw(number);
  await expect(page.locator("main h1")).toHaveText(paper.biblio.biblio.title);
  return paper;
}

/**
 * 成員版論文頁設計稿上有、之後的 Story 才做的字（修正分類、回報錯誤、我的 PDF、找相似論文、引用、頁尾……；
 * design-compare 的 --expect-missing 清單 paper.txt，同一份）。示範站現在一個都不會有。清單裡的字去掉了空白。
 */
const LATER_OR_MEMBER_ONLY = readFileSync(fileURLToPath(new URL("../../docs/ux/reference/expect-missing/paper.txt", import.meta.url)), "utf-8")
  .split("\n")
  .map((line) => line.trim())
  .filter((line) => line !== "" && !line.startsWith("#"));

/** 成員才有的功能（State Patterns「全站共用」的「示範站的後端功能」：不存在，不顯示入口）。 */
async function expectNoMemberFeatures(page: Page): Promise<void> {
  for (const key of ["paper:paper.library.heading", "paper:paper.library.add", "paper:paper.summary.reported", "paper:paper.state.failed"]) {
    await expect(page.getByText(text(key)), key).toHaveCount(0);
  }
  expect(LATER_OR_MEMBER_ONLY.length).toBeGreaterThan(5);
  // 畫面上每一段字（文字節點，去掉空白）；清單一行一段，行尾「…」只比開頭（和比對工具相同的比法）。
  const paragraphs = await page.evaluate(() => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const found: string[] = [];
    for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
      const value = (node.textContent ?? "").replace(/\s+/g, "");
      if (value !== "") found.push(value);
    }
    return found;
  });
  expect(paragraphs.length).toBeGreaterThan(5);
  for (const line of LATER_OR_MEMBER_ONLY) {
    const hit = paragraphs.filter((value) => (line.endsWith("…") ? value.startsWith(line.slice(0, -1)) : value === line));
    expect(hit, line).toEqual([]);
  }
  await expect(page.locator("[data-library]")).toHaveCount(0);
  await expect(page.locator("a[href*='login']")).toHaveCount(0);
}

test.describe("demo paper page", () => {
  test("full content: summary, research model and abstract; no member features; no API requests", async ({ page }) => {
    const requests: string[] = [];
    page.on("request", (request) => requests.push(new URL(request.url()).pathname));
    await openPaper(page, 1);
    await expect(page.locator("[data-demo-paper]")).toHaveAttribute("data-demo-paper", "full");
    await expect(page.locator("[data-data-date]")).toBeVisible();
    await expect(page.locator("#paper-zh-summary")).toHaveText(text("paper:paper.summary.heading"));
    await expect(page.locator("#paper-research-model")).toHaveText(text("paper:paper.model.heading"));
    await expect(page.locator("#paper-abstract")).toHaveText(text("paper:paper.abstract.heading"));
    await expect(page.getByText(text("paper:paper.biblio.hasAbstract"), { exact: true })).toBeVisible();
    await expect(page.getByText(text("ai:ai.badge.full"))).toBeVisible();
    await expect(page.locator("aside [data-full-text='open']")).toBeVisible();
    await expectNoMemberFeatures(page);
    // 示範站沒有後端：只讀 preview/ 的靜態檔，沒有任何 /api/ 請求。
    expect(requests.filter((path) => path.startsWith("/api/"))).toEqual([]);
    expect(requests.filter((path) => path.startsWith("/preview/")).length).toBeGreaterThan(0);
  });

  test("eligible without an abstract: the no-abstract block and the research model line", async ({ page }) => {
    const paper = await openPaper(page, 2);
    expect(paper.biblio).toMatchObject({ demo_eligible: true, has_abstract: false });
    await expect(page.locator("[data-demo-paper]")).toHaveAttribute("data-demo-paper", "noAbstract");
    await expect(page.locator("[data-block='no-abstract']")).toContainText(text("paper:paper.noAbstract.message"));
    await expect(page.locator("[data-block='research_model']")).toContainText(text("paper:paper.model.noAbstract"));
    await expect(page.getByText(text("demo:demo.paper.biblioOnly"))).toHaveCount(0);
    await expectNoMemberFeatures(page);
  });

  test("bibliography only: the licence notice, the publisher link and the link to papers with full content", async ({ page }) => {
    const paper = await openPaper(page, 3);
    expect(paper.biblio.demo_eligible).toBe(false);
    const notice = page.locator("[data-block='biblio-only']");
    await expect(notice).toContainText(text("demo:demo.paper.biblioOnly"));
    const publisher = notice.getByRole("link", { name: text("paper:paper.noAbstract.publisher") });
    await expect(publisher).toHaveAttribute("href", paper.biblio.biblio.doi_url!);
    await expect(publisher).toHaveAttribute("target", "_blank");
    for (const id of ["paper-zh-summary", "paper-research-model", "paper-abstract", "paper-no-abstract"]) {
      await expect(page.locator(`#${id}`)).toHaveCount(0);
    }
    await expect(page.getByText(text("paper:paper.biblio.hasAbstract"), { exact: true })).toHaveCount(0);
    await expect(page.locator("[data-block='classification']")).toBeVisible();
    await expectNoMemberFeatures(page);

    // 「看有完整內容的論文」：論文清單、勾好「有完整內容」。
    await notice.getByRole("link", { name: text("demo:demo.paper.fullPapers") }).click();
    await expect(page).toHaveURL(`${demo.baseURL}/papers?full=true`);
    const full = INDEX.has_full_content.filter(Boolean).length;
    await expect(page.locator("[data-result-count]")).toHaveText(text("demo:demo.papers.count").replace("{{count}}", String(full)));
  });

  test("not generated successfully: only the short demo sentence", async ({ page }) => {
    await openPaper(page, 12);
    await expect(page.locator("[data-block='zh_summary']")).toHaveAttribute("data-block-state", "failed");
    await expect(page.locator("[data-block='zh_summary']")).toContainText(text("demo:demo.paper.failed"));
    await expect(page.getByText(text("paper:paper.state.failed"))).toHaveCount(0);
  });

  test("classification tags go to the paper list with that one condition", async ({ page }) => {
    const paper = await openPaper(page, 1);
    const key = paper.biblio.classification!.value!.topic!.keys[0]!;
    await page.locator("[data-block='classification'] [data-topic-link]").first().click();
    await expect(page).toHaveURL(`${demo.baseURL}/papers?topic=${key}`);
    await expect(page.locator("[data-paper-row]").first()).toBeVisible();
  });

  test("an id that is not in this export, or not an id, is not found under the demo header", async ({ page }) => {
    for (const path of ["/papers/00000000-0000-4000-8000-000000000000", "/papers/not-a-paper"]) {
      await page.goto(`${demo.baseURL}${path}`);
      await expect(page.locator("main h1")).toHaveText(text("common:states.notFound.message"));
      // 「找不到這一頁」就是頁面標題（h1），分頁名稱也是它（State Patterns「全站共用」）。
      await expect(page.locator("h1")).toHaveCount(1);
      await expect(page).toHaveTitle(text("common:states.notFound.message"));
      await expect(page.locator("header [data-brand]")).toBeVisible();
    }
    // 其他網址仍然是找不到這一頁。
    await page.goto(`${demo.baseURL}${MISSING_PATH}`);
    await expect(page.locator("h1")).toHaveText(text("common:states.notFound.message"));
  });

  test("a shard that fails to load shows load failed under the demo header", async ({ page }) => {
    await page.route("**/preview/*/papers/*.json", (route: Route) => route.fulfill({ status: 500, body: "" }));
    await page.goto(`${demo.baseURL}/papers/${demoPaperId(1)}`);
    await expect(page.locator("main h1")).toHaveText(text("common:states.loadFailed.message"));
    await expect(page.locator("header [data-brand]")).toBeVisible();
  });
});

for (const width of [1440, 1000, 500]) {
  for (const [label, number] of [
    ["full", 1],
    ["no-abstract", 2],
    ["biblio-only", 3],
    ["failed", 12],
  ] as const) {
    test(`demo paper ${label} screenshot at ${width}px`, async ({ page, browserName }) => {
      test.skip(browserName !== "chromium", "screenshots are taken in chromium only");
      await openPaper(page, number, { width, height: 900 });
      await page.evaluate(() => document.fonts.ready);
      // 900px 以上兩欄（輔助欄在右）；以下一欄、底部固定列。沒有橫向捲動。
      await expect(page.locator("aside")).toBeVisible({ visible: width >= 900 });
      await expect(page.locator("[data-full-text='bar']")).toBeVisible({ visible: width < 900 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await page.screenshot({ path: `${SCREENSHOT_DIR}demo-paper-${label}-${width}.png`, fullPage: true });
    });
  }
}

// ---- 引用匯出（Story 3.10）：和成員版同一份實作（AR-24），示範站也在瀏覽器裡產生、不呼叫任何後端 ----
// 預期值是照 tests/golden/exports/README.md 的規則、對範例的 Fake Paper 1、3 手寫的。範例的作者「Test Author 1」
// 照規則最後一個字是姓，所以姓是「1」、名是「Test Author」。

const FAKE_1_APA = "1, T. A., & Author, S. (2025). Fake Paper 1: full content. MIS Quarterly, 1(1), 1–9. https://doi.org/10.9999/demo.1";
const FAKE_3_BIBTEX = [
  "@article{32024fake,",
  "  author = {3, Test Author and Author, Second},",
  "  title = {Fake Paper 3: biblio only not open},",
  "  journal = {MIS Quarterly},",
  "  year = {2024},",
  "  volume = {1},",
  "  number = {1},",
  "  pages = {1--9},",
  "  doi = {10.9999/demo.3}",
  "}",
];

test("demo citation: copy shows copied for 2 seconds; no API or other backend requests", async ({ page }) => {
  await page.addInitScript(() => {
    const written: string[] = [];
    (window as unknown as { __clipboard: string[] }).__clipboard = written;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: (value: string) => (written.push(value), Promise.resolve()) },
    });
  });
  const requests: string[] = [];
  await openPaper(page, 1);
  page.on("request", (request) => requests.push(new URL(request.url()).pathname));
  const button = page.locator("[data-cite='rail'] [data-cite='apa']");
  await expect(button).toHaveText(text("paper:paper.cite.copyApa"));
  await button.click();
  await expect(button).toHaveText(text("paper:paper.cite.copied"));
  await expect(button).toHaveAttribute("aria-disabled", "true");
  expect(await page.evaluate(() => (window as unknown as { __clipboard: string[] }).__clipboard)).toEqual([FAKE_1_APA]);
  await expect(button).toHaveText(text("paper:paper.cite.copyApa"), { timeout: 3000 });
  expect(requests).toEqual([]);
});

test("demo citation: bibliography only papers download BibTeX in the browser", async ({ page }) => {
  const requests: string[] = [];
  await openPaper(page, 3, { width: 500, height: 900 });
  page.on("request", (request) => requests.push(request.url()));
  const button = page.locator("[data-cite='mobile'] [data-cite='bibtex']");
  const [download] = await Promise.all([page.waitForEvent("download"), button.click()]);
  expect(download.suggestedFilename()).toBe("32024fake.bib");
  expect(readFileSync((await download.path())!, "utf-8")).toBe(`${FAKE_3_BIBTEX.join("\n")}\n`);
  await expect(button).toHaveText(text("paper:paper.cite.downloadBibtex"));
  expect(requests.filter((url) => !url.startsWith("blob:"))).toEqual([]);
});

// 第 1 輪審查修正（PR #133）：示範站的「下載 RIS」。預期值照 tests/golden/exports/README.md 的規則、對 Fake Paper 1 手寫。
const FAKE_1_RIS = [
  "TY  - JOUR",
  "AU  - 1, Test Author",
  "AU  - Author, Second",
  "TI  - Fake Paper 1: full content",
  "T2  - MIS Quarterly",
  "PY  - 2025",
  "VL  - 1",
  "IS  - 1",
  "SP  - 1",
  "EP  - 9",
  "DO  - 10.9999/demo.1",
  "ER  - ",
];

test("demo citation: download RIS makes the file in the browser and the button keeps its text", async ({ page }) => {
  const requests: string[] = [];
  await openPaper(page, 1);
  page.on("request", (request) => requests.push(request.url()));
  const button = page.locator("[data-cite='rail'] [data-cite='ris']");
  const [download] = await Promise.all([page.waitForEvent("download"), button.click()]);
  expect(download.suggestedFilename()).toBe("12025fake.ris");
  expect(readFileSync((await download.path())!, "utf-8")).toBe(`${FAKE_1_RIS.join("\r\n")}\r\n`);
  await expect(button).toHaveText(text("paper:paper.cite.downloadRis"));
  expect(requests.filter((url) => !url.startsWith("blob:"))).toEqual([]);
});

// 全站頁尾（Story 3.15）和手機版底部固定列互不遮蓋（示範站也用同一個 FullTextBar；成員版那一條在 paper.spec.ts）：
// 捲到中間時固定列貼在視窗底部、頁尾在視窗外；捲到最底時固定列停在頁尾上面，頁尾的連結與固定列的按鈕都點得到。
test("demo phone: the bottom bar and the site footer never cover each other", async ({ page }) => {
  await openPaper(page, 1, { width: 390, height: 844 });
  const bar = page.locator("[data-full-text='bar']");
  const footer = page.locator("footer[data-site-footer]");
  const top = (await bar.boundingBox())!;
  expect(top.y + top.height).toBeCloseTo(844, 0);
  expect((await footer.boundingBox())!.y).toBeGreaterThanOrEqual(844);

  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await expect(footer).toBeInViewport({ ratio: 0.99 });
  const end = (await bar.boundingBox())!;
  const footerBox = (await footer.boundingBox())!;
  expect(end.y + end.height, "bar bottom is above the footer top").toBeLessThanOrEqual(footerBox.y + 0.5);
  const link = (await footer.locator("a").first().boundingBox())!;
  const onTop = await page.evaluate(
    ([x, y]) => document.elementFromPoint(x!, y!)?.closest("footer") !== null,
    [link.x + link.width / 2, link.y + link.height / 2],
  );
  expect(onTop, "footer link is not covered").toBe(true);
  const button = (await page.locator("[data-full-text='open-mobile']").boundingBox())!;
  const buttonOnTop = await page.evaluate(
    ([x, y]) => document.elementFromPoint(x!, y!)?.closest("[data-full-text='bar']") !== null,
    [button.x + button.width / 2, button.y + button.height / 2],
  );
  expect(buttonOnTop, "bar button is not covered").toBe(true);
});
