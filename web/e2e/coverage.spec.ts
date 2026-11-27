// 資料涵蓋範圍（Story 3.15；EXPERIENCE.md 畫面 9、State Patterns「9. 資料涵蓋範圍」）：成員版讀 /api/v1/cube
// （api-mock.ts，假資料 fixtures/cube.json），示範站讀範例匯出的 cube.json（fixtures/demo-preview）。
// 正常、載入中（表格的骨架）、錯誤（全站共用的「沒有載入成功」，重新整理後恢復）、沒有依據（那一年沒有論文的格子是
// 「—」，不是 0%）；「空的」不會發生（State Patterns「9.」），沒有測試。入口：資料日期列、使用須知第 4 段、頁尾。
// 預期的篇數與比例寫死（數字是假資料編的，算法見每一行旁邊）；設計稿表格裡的數字是編的，不拿來當答案。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import type { Page, Route } from "@playwright/test";

import { gate } from "./api-mock.ts";
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { SITES } from "./pages.ts";

const member = SITES[0]!;
const demo = SITES[1]!;
const SCREENSHOT_DIR = fileURLToPath(new URL("./screenshots/", import.meta.url));
const DESKTOP = { width: 1280, height: 900 };
const PHONE = { width: 390, height: 844 };

function text(key: string): string {
  const value = TEXT[key];
  if (value === undefined) throw new Error(`no expected text ${key}`);
  return value;
}

function fill(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{\{(\w+)\}\}/g, (_, name: string) => String(values[name]));
}

interface CubeFile {
  snapshot: { number: number };
  rows: { journal: string; year: number; dim: string; n: number }[];
}
const MEMBER_CUBE = JSON.parse(readFileSync(fileURLToPath(new URL("./fixtures/cube.json", import.meta.url)), "utf-8")) as CubeFile;
const DEMO_FIXTURE = fileURLToPath(new URL("./fixtures/demo-preview/", import.meta.url));
const DEMO_EXPORT = (JSON.parse(readFileSync(`${DEMO_FIXTURE}manifest.json`, "utf-8")) as { export_id: string }).export_id;
const DEMO_CUBE = JSON.parse(readFileSync(`${DEMO_FIXTURE}${DEMO_EXPORT}/cube.json`, "utf-8")) as CubeFile;

const readings = (page: Page) => page.locator("[data-reading] [data-reading-number]");
const cell = (page: Page, journal: string, year: number) => page.locator(`tr[data-journal='${journal}'] td[data-year='${year}']`);

async function open(page: Page, base: string, viewport = DESKTOP): Promise<void> {
  await page.setViewportSize(viewport);
  await page.goto(`${base}/coverage`);
}

/** 名詞說明的詞與解釋；「快照編號」的例子是這一次的快照編號（`snapshot`；還不知道快照時是設計稿的 12）。 */
async function expectTerms(page: Page, snapshot = 12): Promise<void> {
  await expect(page.locator("[data-coverage-terms] dt")).toHaveText(
    ["outside", "snapshot"].map((key) => text(`coverage:coverage.terms.${key}`)),
  );
  await expect(page.locator("[data-coverage-terms] dd")).toHaveText([
    text("coverage:coverage.terms.outsideText"),
    fill(text("coverage:coverage.terms.snapshotText"), { number: snapshot }),
  ]);
}

/** 說明與 Elsevier 的兩句（設計稿的字；期刊縮寫和表格同一份對照表）。 */
async function expectIntroAndElsevier(page: Page): Promise<void> {
  await expect(page.locator("main h1 + p")).toHaveText(text("coverage:coverage.intro.text"));
  await expect(page.locator("[data-coverage-elsevier] p")).toHaveText([
    fill(text("coverage:coverage.elsevier.journals"), { journals: text("coverage:coverage.elsevier.list") }),
    text("coverage:coverage.elsevier.effect"),
  ]);
}

/** Tab 用哪個鍵（Safari 預設 Tab 不走連結，和 a11y-floor 一樣用 Option+Tab）。 */
function tabKey(browserName: string): string {
  return browserName === "webkit" ? "Alt+Tab" : "Tab";
}

// 焦點外框（styles.css 的 :focus-visible，ADR-0037）：金色 1px、往外 4px。
const OUTLINE_GOLD = [227, 180, 102] as const;
const MARGIN = 8;

/**
 * 用鍵盤把焦點放到一格（Shift+Tab 再 Tab，讓 :focus-visible 成立），截那一格外面 8px 的圖，回傳四邊外框的
 * 金色像素比例（0～1）。外框被固定的期刊欄蓋住或被捲動框切掉，那一邊就幾乎是 0。
 */
async function outlineCoverage(page: Page, target: ReturnType<typeof cell>, browserName: string): Promise<Record<"top" | "right" | "bottom" | "left", number>> {
  await target.focus();
  await page.keyboard.press(`Shift+${tabKey(browserName)}`);
  await page.keyboard.press(tabKey(browserName));
  await expect(target).toBeFocused();
  const box = (await target.boundingBox())!;
  const clip = { x: Math.floor(box.x) - MARGIN, y: Math.floor(box.y) - MARGIN, width: Math.ceil(box.width) + 2 * MARGIN, height: Math.ceil(box.height) + 2 * MARGIN };
  // 用 CSS 像素截圖：高解析螢幕上預設會截成 2 倍大，和下面用的 CSS 座標對不上。
  const shot = await page.screenshot({ clip, scale: "css" });
  const offset = { x: box.x - clip.x, y: box.y - clip.y };
  // 在沒有任何內容安全政策的空白頁解 PNG（不加套件）。
  const probe = await page.context().newPage();
  try {
    return await probe.evaluate(
      async ({ data, size, offset, gold }) => {
        const image = new Image();
        image.src = `data:image/png;base64,${data}`;
        await image.decode();
        const canvas = document.createElement("canvas");
        canvas.width = image.width;
        canvas.height = image.height;
        const context = canvas.getContext("2d")!;
        context.drawImage(image, 0, 0);
        const pixels = context.getImageData(0, 0, image.width, image.height).data;
        const isGold = (x: number, y: number) => {
          const at = (y * image.width + x) * 4;
          return Math.abs(pixels[at]! - gold[0]) + Math.abs(pixels[at + 1]! - gold[1]) + Math.abs(pixels[at + 2]! - gold[2]) < 90;
        };
        // 外框落在格子邊往外 4～5px；每一邊在 3～6px 的帶子裡找，沿著那一邊逐點看有沒有金色。
        const left = offset.x, top = offset.y, right = offset.x + size.width, bottom = offset.y + size.height;
        const band = (outside: (depth: number) => [number, number], length: number, along: (step: number) => [number, number]) => {
          let hit = 0;
          for (let step = 0; step < length; step += 1) {
            const found = [3, 4, 5, 6].some((depth) => {
              const [dx, dy] = outside(depth);
              const [ax, ay] = along(step);
              return isGold(Math.round(ax + dx), Math.round(ay + dy));
            });
            if (found) hit += 1;
          }
          return hit / length;
        };
        const width = Math.floor(right - left), height = Math.floor(bottom - top);
        return {
          top: band((depth) => [0, top - depth], width, (step) => [left + step, 0]),
          bottom: band((depth) => [0, bottom + depth - 1], width, (step) => [left + step, 0]),
          left: band((depth) => [left - depth, 0], height, (step) => [0, top + step]),
          right: band((depth) => [right + depth - 1, 0], height, (step) => [0, top + step]),
        };
      },
      { data: shot.toString("base64"), size: { width: box.width, height: box.height }, offset, gold: OUTLINE_GOLD },
    );
  } finally {
    await probe.close();
  }
}

test.describe("member coverage page", () => {
  test("shows the intro, three readings, the table, the Elsevier note and the terms", async ({ page }) => {
    await open(page, member.baseURL);
    await expect(page.locator("main h1")).toHaveText(text("coverage:coverage.page.title"));
    await expect(page.locator("[data-page='coverage']")).toHaveAttribute("data-state", "ready");
    await expect(page.locator("[data-reading] dt")).toHaveText([
      text("coverage:coverage.readings.total"),
      text("coverage:coverage.readings.withAbstract"),
      text("coverage:coverage.readings.thisWeek"),
    ]);
    // fixtures/cube.json：total 合計 6,175、with_abstract 合計 4,685（4685/6175 = 75.87%）；本週新增是 paper-list.json
    // 裡 this_week 的篇數。
    const thisWeek = (JSON.parse(readFileSync(fileURLToPath(new URL("./fixtures/paper-list.json", import.meta.url)), "utf-8")) as {
      rows: { this_week: boolean }[];
    }).rows.filter((entry) => entry.this_week).length;
    await expect(readings(page)).toHaveText(["6,175", "4,685", String(thisWeek)]);
    await expect(page.locator("[data-reading-percent]")).toHaveText("76%");
    // 成員版只有本週新增是連結（設計稿）：到找論文並帶入「本週新增」。
    await expect(page.locator("[data-reading] a")).toHaveCount(1);
    await expect(page.locator("[data-reading='third'] a")).toHaveAttribute("href", "/search?this_week=true");
    // 年份欄：追蹤起始年 2016 到快照那一年 2026（11 欄），最後一欄加註「未滿一年」。
    await expect(page.locator("thead th[scope='col']")).toHaveCount(12);
    await expect(page.locator("[data-partial-year]")).toHaveCount(1);
    await expect(page.locator("thead th").last()).toHaveText(`${text("coverage:coverage.table.partialYear")}2026`);
    await expectIntroAndElsevier(page);
    // 期刊縮寫照設計稿：I&M、I&O（和 Elsevier 那段一樣），不是代號轉大寫。
    await expect(page.locator("tr[data-journal='im'] abbr")).toHaveText("I&M");
    await expect(page.locator("tr[data-journal='io'] abbr")).toHaveText("I&O");
    await expectTerms(page, MEMBER_CUBE.snapshot.number);
  });

  test("no grounds: a year without papers is a dash, not 0%, and never gold", async ({ page }) => {
    await open(page, member.baseURL);
    // fixtures/cube.json 沒有 io 2026 的列。
    await expect(cell(page, "io", 2026)).toHaveText("—");
    await expect(cell(page, "io", 2026)).toHaveAttribute("data-state", "empty");
    // 有論文的格子：misq 2024 是 48/52（92.3% → 92%）；Elsevier 的 dss 2016 是 8/34（23.5% → 24%，未滿 50% 金色）。
    await expect(cell(page, "misq", 2024)).toHaveText("92%");
    await expect(cell(page, "misq", 2024)).toHaveAttribute("data-state", "ok");
    await expect(cell(page, "dss", 2016)).toHaveText("24%");
    await expect(cell(page, "dss", 2016)).toHaveAttribute("data-state", "low");
    // 樣子：100% 以上門檻是實心點、未滿是金色空心點、空格沒有點（EXPERIENCE.md 畫面 9）。
    await expect(cell(page, "misq", 2024).locator("span[aria-hidden]")).toHaveClass(/\bbg-text-reading\b/);
    await expect(cell(page, "misq", 2024)).toHaveClass(/\btext-text-reading\b/);
    await expect(cell(page, "dss", 2016).locator("span[aria-hidden]")).toHaveClass(/\bborder-accent-gold\b/);
    await expect(cell(page, "dss", 2016).locator("span[aria-hidden]")).not.toHaveClass(/\bbg-text-reading\b/);
    await expect(cell(page, "dss", 2016)).toHaveClass(/\btext-accent-gold\b/);
    await expect(cell(page, "io", 2026)).toHaveClass(/\btext-text-tertiary\b/);
    await expect(cell(page, "io", 2026).locator(".rounded-full")).toHaveCount(0);
    // 指到一格，右上顯示篇數；指到空格顯示「這一年沒有論文」。
    await cell(page, "misq", 2024).hover();
    await expect(page.locator("[data-coverage-pick]")).toHaveText(
      `${fill(text("coverage:coverage.pick.cell"), { journal: "MISQ", year: 2024 })}${fill(text("coverage:coverage.pick.counts"), { total: 52, withAbstract: 48 })}`,
    );
    await cell(page, "io", 2026).hover();
    await expect(page.locator("[data-coverage-pick]")).toContainText(text("coverage:coverage.legend.empty"));
    // 指到只換畫面上的字，報讀器的區塊不唸（還是空的）。
    await expect(page.locator("[data-coverage-announce]")).toHaveText("");
  });

  test("keyboard: Tab reaches a cell, which shows its counts and is announced", async ({ page, browserName }) => {
    await open(page, member.baseURL);
    // 表格前最後一個可以聚焦的是「本週新增」的數字連結；下一個是第一格。成員版的第一列是假期刊 JES
    // （fixtures/papers.json 的顯示名稱排第一、沒有論文），所以第一格是空格。
    await page.locator("[data-reading='third'] a").focus();
    await page.keyboard.press(tabKey(browserName));
    await expect(cell(page, "jes", 2016)).toBeFocused();
    await expect(page.locator("[data-coverage-pick]")).toContainText(text("coverage:coverage.legend.empty"));
    // 一列 11 格（2016～2026）：再走 11 格到 misq 2016（fixtures/cube.json：53 篇裡 52 篇）。
    for (let step = 0; step < 11; step += 1) await page.keyboard.press(tabKey(browserName));
    await expect(cell(page, "misq", 2016)).toBeFocused();
    const where = fill(text("coverage:coverage.pick.cell"), { journal: "MISQ", year: 2016 });
    const counts = fill(text("coverage:coverage.pick.counts"), { total: 53, withAbstract: 52 });
    await expect(page.locator("[data-coverage-pick]")).toHaveText(`${where}${counts}`);
    await expect(page.locator("[data-coverage-announce]")).toHaveText(`${where} ${counts}`);
    await expect(page.locator("[data-coverage-announce]")).toHaveAttribute("role", "status");
  });

  for (const viewport of [DESKTOP, PHONE]) {
    test(`keyboard at ${viewport.width}px: the focus outline of a cell shows on all four sides`, async ({ page, browserName }) => {
      // WebKit 截圖會塞一段行內樣式，被 CSP 擋下來（測試框架的副作用，和 nav.spec 拍整頁截圖那條同一個原因）；
      // 用截圖量外框只在 Chromium、Firefox 量。WebKit 的焦點外框由 a11y-floor 的走訪檢查。
      test.skip(browserName === "webkit", "WebKit screenshots inject an inline style that the CSP blocks");
      await open(page, member.baseURL, viewport);
      await expect(page.locator("table")).toBeVisible();
      const lastJournal = (await page.locator("tbody tr").last().getAttribute("data-journal"))!;
      // 第一欄（緊貼固定的期刊欄）、中間一格、最後一列最後一欄（貼著捲動框的右邊與下邊）；手機最後再從捲到最右邊
      // 的狀態回到第二欄（捲回來時不停在固定欄底下）。
      const targets = [cell(page, "misq", 2016), cell(page, "misq", 2021), cell(page, lastJournal, 2026), cell(page, "misq", 2017)];
      for (const target of targets) {
        const sides = await outlineCoverage(page, target, browserName);
        for (const side of ["top", "right", "bottom", "left"] as const) {
          expect(sides[side], `${await target.getAttribute("data-year")} ${side}`).toBeGreaterThan(0.9);
        }
      }
    });
  }

  test("broken counts (more abstracts than papers) show load failed instead of crashing", async ({ page, apiMock }) => {
    const rows = apiMock.cubeBody.rows.map((row) =>
      row.journal === "misq" && row.year === 2024 && row.dim === "with_abstract" ? { ...row, n: 999 } : row,
    );
    apiMock.cubeBody = { ...apiMock.cubeBody, rows };
    await open(page, member.baseURL);
    await expect(page.locator("[data-page='coverage']")).toHaveAttribute("data-state", "error");
    await expect(page.locator("[role='alert'][data-state='error']")).toContainText(text("common:states.loadFailed.message"));
    await expect(page.locator("table")).toHaveCount(0);
  });

  test("loading shows the skeleton of the readings and the table", async ({ page, apiMock }) => {
    const [wait, release] = gate();
    apiMock.cubeGate = wait;
    await open(page, member.baseURL);
    await expect(page.locator("[data-page='coverage']")).toHaveAttribute("data-state", "loading");
    await expect(page.locator("[data-coverage-skeleton]")).toBeVisible();
    await expect(readings(page)).toHaveCount(0);
    await expect(page.locator("[data-coverage-legend]")).toHaveCount(0);
    await expect(page.locator("[data-coverage-elsevier]")).toHaveCount(0);
    await expectTerms(page);
    release();
    await expect(page.locator("[data-page='coverage']")).toHaveAttribute("data-state", "ready");
    await expect(page.locator("[data-coverage-skeleton]")).toHaveCount(0);
  });

  test("an error shows load failed; trying again recovers", async ({ page, apiMock }) => {
    apiMock.cube = "failed";
    await open(page, member.baseURL);
    const error = page.locator("[role='alert'][data-state='error']");
    await expect(error).toContainText(text("common:states.loadFailed.message"));
    await expect(page.locator("table")).toHaveCount(0);
    await expect(page.locator("[data-readings]")).toHaveCount(0);
    await expectTerms(page);
    apiMock.cube = "ok";
    await error.getByRole("button", { name: text("common:states.loadFailed.action") }).click();
    await expect(page.locator("[data-page='coverage']")).toHaveAttribute("data-state", "ready");
    await expect(readings(page).first()).toHaveText("6,175");
  });

  test("no published snapshot yet (cannot happen) is shown as load failed", async ({ page, apiMock }) => {
    apiMock.cubeBody = { ...apiMock.cubeBody, rows: [], snapshot: null };
    await open(page, member.baseURL);
    await expect(page.locator("[data-page='coverage']")).toHaveAttribute("data-state", "error");
    await expect(page.locator("table")).toHaveCount(0);
  });

  test("an expired session goes to the login page", async ({ page, apiMock }) => {
    apiMock.cube = "expired";
    await open(page, member.baseURL);
    await expect(page).toHaveURL(/\/login/);
  });

  test("entry points: the data date on another page, and paragraph 4 of the notice", async ({ page }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/search`);
    await page.locator("[data-data-date-link]").click();
    await expect(page).toHaveURL(`${member.baseURL}/coverage`);
    // 在這一頁自己，日期不是連結。
    await expect(page.locator("[data-data-date]")).toBeVisible();
    await expect(page.locator("[data-data-date-link]")).toHaveCount(0);

    await page.goto(`${member.baseURL}/notice`);
    await page.locator("[data-coverage-link]").click();
    await expect(page).toHaveURL(`${member.baseURL}/coverage`);
    await expect(page.locator("main h1")).toHaveText(text("coverage:coverage.page.title"));
  });

  test("phone: the table scrolls sideways at 866px with the journal column fixed; the page does not", async ({ page }) => {
    await open(page, member.baseURL, PHONE);
    await expect(page.locator("table")).toBeVisible();
    expect((await page.locator("table").boundingBox())!.width).toBeCloseTo(866, 0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(PHONE.width);
    const scroller = page.locator("[data-coverage-scroll]");
    const before = (await page.locator("tr[data-journal='misq'] th").boundingBox())!.x;
    await scroller.evaluate((element) => element.scrollBy(300, 0));
    expect((await page.locator("tr[data-journal='misq'] th").boundingBox())!.x).toBeCloseTo(before, 0);
    // 手機是點一格。
    await cell(page, "misq", 2024).click();
    await expect(page.locator("[data-coverage-pick]")).toContainText(fill(text("coverage:coverage.pick.cell"), { journal: "MISQ", year: 2024 }));
  });
});

test.describe("demo coverage page", () => {
  test("shows the three readings from the export, all linked to the paper list", async ({ page }) => {
    await open(page, demo.baseURL);
    await expect(page.locator("[data-page='coverage']")).toHaveAttribute("data-state", "ready");
    await expect(page.locator("[data-reading] dt")).toHaveText([
      text("coverage:coverage.readings.total"),
      text("coverage:coverage.readings.withAbstract"),
      text("coverage:coverage.readings.fullContent"),
    ]);
    // 範例匯出（manifest.json 的 summary）：60 篇、51 篇有摘要（85%）、34 篇有完整內容。
    await expect(readings(page)).toHaveText(["60", "51", "34"]);
    await expect(page.locator("[data-reading-percent]")).toHaveText("85%");
    await expectIntroAndElsevier(page);
    await expectTerms(page, DEMO_CUBE.snapshot.number);
    await expect(page.locator("[data-reading] a")).toHaveCount(3);
    const hrefs = await page.locator("[data-reading] a").evaluateAll((links) => links.map((link) => link.getAttribute("href")));
    expect(hrefs).toEqual(["/papers", "/papers?abstract=true", "/papers?full=true"]);
    await page.locator("[data-reading='third'] a").click();
    await expect(page).toHaveURL(`${demo.baseURL}/papers?full=true`);
  });

  test("no grounds: journals and years without papers are dashes", async ({ page }) => {
    await open(page, demo.baseURL);
    const has = new Set(DEMO_CUBE.rows.map((row) => `${row.journal} ${row.year}`));
    // 範例只有 io、isr、misq、tem 有論文：jmis 每一年都是空格；2016 年每一本都是空格。
    expect(has.has("jmis 2024")).toBe(false);
    await expect(page.locator("tr[data-journal='jmis'] td")).toHaveText(Array.from({ length: 11 }, () => "—"));
    await expect(page.locator("td[data-year='2016'][data-state='empty']")).toHaveCount(14);
    await expect(page.locator("td[data-state='ok'], td[data-state='low']")).toHaveCount(has.size);
    await expect(page.locator("[data-partial-year]")).toHaveCount(1);
  });

  test("keyboard: Tab from the last reading reaches the first cell", async ({ page, browserName }) => {
    await open(page, demo.baseURL);
    await page.locator("[data-reading='third'] a").focus();
    await page.keyboard.press(tabKey(browserName));
    await expect(cell(page, "misq", 2016)).toBeFocused();
    // 範例沒有 2016 年的論文：選取區顯示「這一年沒有論文」。
    await expect(page.locator("[data-coverage-pick]")).toContainText(text("coverage:coverage.legend.empty"));
    await expect(page.locator("[data-coverage-announce]")).toContainText(text("coverage:coverage.legend.empty"));
  });

  test("loading shows the skeleton until cube.json arrives", async ({ page }) => {
    const [wait, release] = gate();
    await page.route("**/preview/*/cube.json", async (route: Route) => {
      await wait;
      await route.continue();
    });
    await open(page, demo.baseURL);
    await expect(page.locator("[data-page='coverage']")).toHaveAttribute("data-state", "loading");
    await expect(page.locator("[data-coverage-skeleton]")).toBeVisible();
    // 日期列照常（manifest 已經讀到）。
    await expect(page.locator("[data-data-date]")).toBeVisible();
    release();
    await expect(page.locator("[data-page='coverage']")).toHaveAttribute("data-state", "ready");
  });

  test("an error shows load failed; trying again recovers", async ({ page }) => {
    let fail = true;
    await page.route("**/preview/*/cube.json", async (route: Route) => {
      if (fail) await route.fulfill({ status: 500, body: "" });
      else await route.continue();
    });
    await open(page, demo.baseURL);
    const error = page.locator("[role='alert'][data-state='error']");
    await expect(error).toContainText(text("common:states.loadFailed.message"));
    // 日期列的快照（manifest 已經讀到）就是範例的快照。
    await expectTerms(page, DEMO_CUBE.snapshot.number);
    fail = false;
    await error.getByRole("button", { name: text("common:states.loadFailed.action") }).click();
    await expect(readings(page)).toHaveText(["60", "51", "34"]);
  });

  test("a cube.json of another export is a load failure", async ({ page }) => {
    await page.route("**/preview/*/cube.json", async (route: Route) => {
      const response = await route.fetch();
      const body = (await response.json()) as Record<string, unknown>;
      await route.fulfill({ response, json: { ...body, export_id: "9-9" } });
    });
    await open(page, demo.baseURL);
    await expect(page.locator("[role='alert'][data-state='error']")).toContainText(text("common:states.loadFailed.message"));
  });

  test("entry point: the data date on the paper list", async ({ page }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(`${demo.baseURL}/papers`);
    await page.locator("[data-data-date-link]").click();
    await expect(page).toHaveURL(`${demo.baseURL}/coverage`);
    await expect(page.locator("main h1")).toHaveText(text("coverage:coverage.page.title"));
  });
});

for (const [site, base] of [
  ["member", member.baseURL],
  ["demo", demo.baseURL],
] as const) {
  for (const width of [1440, 1000, 500]) {
    test(`${site} coverage screenshot at ${width}px`, async ({ page, browserName }) => {
      test.skip(browserName !== "chromium", "screenshots are taken in chromium only");
      await open(page, base, { width, height: 900 });
      await expect(page.locator("table")).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: `${SCREENSHOT_DIR}coverage-${site}-${width}.png`, fullPage: true });
    });
  }
}
