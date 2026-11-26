// 回報錯誤（Story 3.12；EXPERIENCE.md 畫面 8 第 3 點；設計稿 paper.html 的「回報錯誤」四個狀態）：表單在中文重點標題下面原地展開、
// 「哪一欄」只能選一個（radio）、選了欄位也寫了說明才能送；送出中、沒有成功（選的與寫的都留著）、送出後那一欄內容下面多一行
// 金色空心三角＋「有成員回報此欄可能有誤」；說明最多 100 個 code point、沒有「剩 N 字」；鍵盤打開與收起表單時焦點的去處、
// 「哪一欄」的方向鍵；寫入帶預期成員，另一個分頁換了人時什麼都不寫、整頁換成現在的人（ADR-0055）；登入過期回登入頁；
// 中文重點沒有產生成功時沒有「回報錯誤」；可點範圍至少 44px；示範站沒有「回報錯誤」與回報的標記；
// 1440／1000／500 三種寬度、四個狀態的整頁截圖（只在 chromium；截圖存檔供人工看，自動比對由 design-compare 負責 1280／390）。
// 資料是 fixtures/papers.json 的假論文（替身 e2e/api-mock.ts）。
import { fileURLToPath } from "node:url";

import type { Page } from "@playwright/test";

import { EXPECTED_MEMBER_HEADER, MEMBERS, gate, paperId, paperPage, type ApiMockState } from "./api-mock.ts";
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { DEMO_PAPER_PAGES, SITES } from "./pages.ts";

const member = SITES[0]!;
const demo = SITES[1]!;
const SCREENSHOT_DIR = fileURLToPath(new URL("./screenshots/", import.meta.url));
const DESKTOP = { width: 1280, height: 900 };
const url = (label: string) => `${member.baseURL}/papers/${paperId(label)}`;
const text = (key: string) => TEXT[key]!;
// 自己編的說明（介面文字以外的中文不寫在程式裡，用英文）。
const NOTE = "The summary never says the drop is clear";

const area = (page: Page) => page.locator("main [data-summary-report]");
const open = (page: Page) => area(page).locator("[data-report-open]");
const form = (page: Page) => area(page).locator("[data-report-form]");
const pick = (page: Page, key: string) => form(page).locator(`[data-report-pick="${key}"]`);
const send = (page: Page) => form(page).locator("[data-report-send]");
const note = (page: Page) => form(page).getByLabel(text("paper:paper.report.note"));
const mark = (page: Page, field: string) => page.locator(`main [data-summary-field="${field}"] [data-note="reported"]`);

/** 下一個回報的送出：帶的預期成員、本文、替身回的狀態。 */
function nextReport(page: Page) {
  return page
    .waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname.endsWith("/summary-reports"))
    .then(async (response) => ({
      expected: (await response.request().allHeaders())[EXPECTED_MEMBER_HEADER],
      body: response.request().postDataJSON() as unknown,
      status: response.status(),
    }));
}

async function openForm(page: Page, label = "normal") {
  await page.setViewportSize(DESKTOP);
  await page.goto(url(label));
  await open(page).click();
  await expect(form(page)).toBeVisible();
}

test("'report' sits at the right of the summary heading row and opens the form in place below it", async ({ page }) => {
  await page.setViewportSize(DESKTOP);
  await page.goto(url("normal"));
  const heading = page.locator("#paper-zh-summary");
  await expect(open(page)).toHaveText(text("paper:paper.report.open"));
  const headingBox = (await heading.boundingBox())!;
  const openBox = (await open(page).boundingBox())!;
  // 同一列、在右邊（設計稿外層 justify-content: space-between）。
  expect(openBox.x).toBeGreaterThan(headingBox.x + headingBox.width);
  expect(openBox.y).toBeLessThan(headingBox.y + headingBox.height);
  await open(page).click();
  const formBox = (await form(page).boundingBox())!;
  const firstField = (await page.locator("main [data-summary-field]").first().boundingBox())!;
  // 原地展開：標題列下面、四欄上面。
  expect(formBox.y).toBeGreaterThanOrEqual(headingBox.y + headingBox.height);
  expect(formBox.y + formBox.height).toBeLessThanOrEqual(firstField.y);
  // 表單開著時「回報錯誤」先不顯示；四欄一個都沒選。
  await expect(open(page)).toHaveCount(0);
  const picks = form(page).locator("[data-report-pick]");
  await expect(picks).toHaveText([
    text("paper:paper.summary.researchQuestion"),
    text("paper:paper.summary.method"),
    text("paper:paper.summary.theory"),
    text("paper:paper.summary.findings"),
  ]);
  await expect(form(page).getByRole("radiogroup", { name: text("paper:paper.report.field") })).toBeVisible();
  for (const key of ["research_question", "method", "theory", "findings"]) {
    await expect(pick(page, key)).toHaveAttribute("aria-checked", "false");
  }
  // 取消：表單收起來，「回報錯誤」回來。
  await form(page).locator("[data-report-cancel]").click();
  await expect(form(page)).toHaveCount(0);
  await expect(open(page)).toBeVisible();
});

test("only one field can be picked; sending needs a field and a note that is not just spaces", async ({ page }) => {
  await openForm(page);
  await expect(send(page)).toBeDisabled();
  await pick(page, "method").click();
  await expect(send(page)).toBeDisabled();
  await pick(page, "findings").click();
  await expect(pick(page, "findings")).toHaveAttribute("aria-checked", "true");
  await expect(pick(page, "method")).toHaveAttribute("aria-checked", "false");
  await note(page).fill("   ");
  await expect(send(page)).toBeDisabled();
  await note(page).fill(NOTE);
  await expect(send(page)).toBeEnabled();
  // 只寫說明、沒選欄位也不能送（換一次表單重來）。
  await form(page).locator("[data-report-cancel]").click();
  await open(page).click();
  await note(page).fill(NOTE);
  await expect(send(page)).toBeDisabled();
});

test("the note holds at most 100 characters counted as code points, with no 'N left' hint", async ({ page }) => {
  // 「剩 N 字」的門檻與樣子沒有設計稿，先不做（列給 Kevin）；字數算 code point，和後端一樣（表情符號算一個字）。
  await openForm(page);
  await note(page).fill("x".repeat(120));
  await expect(note(page)).toHaveValue("x".repeat(100));
  await note(page).fill("😀".repeat(120));
  await expect(note(page)).toHaveValue("😀".repeat(100));
  await expect(form(page).getByText(/剩\s*\d+\s*字/)).toHaveCount(0);
});

test("keyboard: opening focuses the first field, arrows move and pick, cancelling and sending bring focus back to 'report'", async ({ page }) => {
  await page.setViewportSize(DESKTOP);
  await page.goto(url("normal"));
  await open(page).focus();
  await page.keyboard.press("Enter");
  await expect(form(page)).toBeVisible();
  await expect(pick(page, "research_question")).toBeFocused();
  // 方向鍵：移到下一欄並選它；往左從第一欄繞到最後一欄。
  await page.keyboard.press("ArrowRight");
  await expect(pick(page, "method")).toBeFocused();
  await expect(pick(page, "method")).toHaveAttribute("aria-checked", "true");
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("ArrowLeft");
  await expect(pick(page, "findings")).toBeFocused();
  await expect(pick(page, "findings")).toHaveAttribute("aria-checked", "true");
  // 整組只有一顆在 Tab 順序裡：Tab 走到「說明」。
  await page.keyboard.press("Tab");
  await expect(note(page)).toBeFocused();
  // 取消：焦點回到重新出現的「回報錯誤」。
  await form(page).locator("[data-report-cancel]").focus();
  await page.keyboard.press("Enter");
  await expect(form(page)).toHaveCount(0);
  await expect(open(page)).toBeFocused();
  // 送出成功：一樣回到「回報錯誤」。
  await page.keyboard.press("Enter");
  await expect(pick(page, "research_question")).toBeFocused();
  await page.keyboard.press("Space");
  await expect(pick(page, "research_question")).toHaveAttribute("aria-checked", "true");
  await page.keyboard.press("Tab");
  await page.keyboard.type(NOTE);
  // WebKit 預設不用 Tab 走到按鈕（Safari 的設定），同修正分類的鍵盤測試直接把焦點放到「送出回報」再按 Enter。
  await send(page).focus();
  await page.keyboard.press("Enter");
  await expect(form(page)).toHaveCount(0);
  await expect(open(page)).toBeFocused();
  await expect(mark(page, "research_question")).toBeVisible();
});

test("sending: the button says sending, fields and note are locked, cancel is hidden; done: the gold mark under that field", async ({ page, apiMock: state }) => {
  const [hold, release] = gate();
  state.summaryReportGate = hold;
  await openForm(page);
  await expect(mark(page, "findings")).toHaveCount(0);
  await pick(page, "findings").click();
  await note(page).fill(`  ${NOTE}  `);
  const write = nextReport(page);
  await send(page).click();
  await expect(send(page)).toHaveText(text("paper:paper.report.sending"));
  await expect(send(page)).toBeDisabled();
  await expect(pick(page, "findings")).toBeDisabled();
  await expect(note(page)).toHaveAttribute("readonly", "");
  await expect(form(page).locator("[data-report-cancel]")).toHaveCount(0);
  release();
  expect(await write).toEqual({ expected: MEMBERS[0]!.member_id, body: { field: "findings", note: NOTE }, status: 200 });
  await expect(form(page)).toHaveCount(0);
  await expect(open(page)).toBeVisible();
  // 那一欄內容下面一行：金色空心三角＋「有成員回報此欄可能有誤」；內容不變、其他欄沒有。
  await expect(mark(page, "findings")).toHaveText(text("paper:paper.summary.reported"));
  await expect(mark(page, "findings").locator("svg")).toHaveCSS("color", "rgb(227, 180, 102)");
  await expect(page.locator("main [data-summary-field] [data-note='reported']")).toHaveCount(1);
  await expect(page.locator("main [data-summary-field='findings']")).toContainText(paperPage("normal").zh_summary.value!.findings!);
  // 重新整理之後照伺服器的資料（替身記住了回報）。
  await page.reload();
  await expect(mark(page, "findings")).toBeVisible();
});

test("failed: the message shows above the buttons and the field and note stay", async ({ page, apiMock: state }) => {
  state.summaryReport = "failed";
  await openForm(page);
  await pick(page, "findings").click();
  await note(page).fill(NOTE);
  await send(page).click();
  const alert = form(page).getByRole("alert");
  await expect(alert).toHaveText(text("paper:paper.report.failed"));
  await expect(pick(page, "findings")).toHaveAttribute("aria-checked", "true");
  await expect(note(page)).toHaveValue(NOTE);
  await expect(send(page)).toBeEnabled();
  await expect(send(page)).toHaveText(text("paper:paper.report.send"));
  // 失敗訊息在按鈕上方。
  expect((await alert.boundingBox())!.y).toBeLessThan((await send(page).boundingBox())!.y);
  // 再試一次就成功。
  state.summaryReport = "ok";
  await send(page).click();
  await expect(form(page)).toHaveCount(0);
  await expect(mark(page, "findings")).toBeVisible();
});

test("a field someone already reported shows the mark on load, and 'report' is still there", async ({ page }) => {
  await page.setViewportSize(DESKTOP);
  await page.goto(url("reported"));
  const reported = paperPage("reported").zh_summary.value!.reported_fields;
  expect(reported.length).toBeGreaterThan(0);
  for (const field of reported) await expect(mark(page, field)).toHaveText(text("paper:paper.summary.reported"));
  await expect(page.locator("main [data-summary-field] [data-note='reported']")).toHaveCount(reported.length);
  await expect(open(page)).toBeVisible();
});

test.describe("no report entry when the summary is not ready", () => {
  for (const label of ["zh-failed", "all-failed", "pending", "empty"]) {
    test(`${label}: no 'report'`, async ({ page }) => {
      expect(paperPage(label).zh_summary.state).not.toBe("ready");
      await page.goto(url(label));
      await expect(page.locator("main [data-block='zh_summary']")).toBeVisible();
      await expect(page.locator("main [data-summary-report]")).toHaveCount(0);
      await expect(page.getByText(text("paper:paper.report.open"), { exact: true })).toHaveCount(0);
    });
  }
});

test("clickables are at least 44×44; field tags look 32px high", async ({ page }) => {
  // 「回報錯誤」看起來 28px、表單的標籤看起來 32px；按鈕本身至少 44×44，用負外距不撐開那一行。
  await page.setViewportSize(DESKTOP);
  await page.goto(url("normal"));
  const atLeast = async (selector: string) => {
    const boxes = await area(page).locator(selector).evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().toJSON() as DOMRect));
    // 選擇器找不到東西時迴圈會直接跳過：先斷言至少有一個。
    expect(boxes.length, selector).toBeGreaterThan(0);
    for (const box of boxes) {
      expect(box.width, selector).toBeGreaterThanOrEqual(44);
      expect(box.height, selector).toBeGreaterThanOrEqual(44);
    }
  };
  await atLeast("[data-report-open]");
  await open(page).click();
  await atLeast("[data-report-pick]");
  await atLeast("[data-report-send]");
  await atLeast("[data-report-cancel]");
  const pills = await form(page).locator("[data-report-pick] > span").evaluateAll((spans) => spans.map((span) => span.getBoundingClientRect().height));
  expect(pills.length).toBe(4);
  expect(pills.every((height) => height === 32)).toBe(true);
});

test("B signs in elsewhere and A sends a report: nothing is written, the page becomes B's (ADR-0055)", async ({ page, apiMock: state }) => {
  const B = MEMBERS[1]!;
  const forB = "Paper title member B sees";
  await openForm(page);
  await pick(page, "findings").click();
  await note(page).fill(NOTE);
  state.member = B;
  state.paperByMember = { [B.member_id]: { ...paperPage("normal"), paper: { ...paperPage("normal").paper, title: forB } } };
  const write = nextReport(page);
  await send(page).click();
  const sent = await write;
  expect(sent.status).toBe(409);
  expect(sent.expected).toBe(MEMBERS[0]!.member_id);
  await expect(page.locator("h1")).toHaveText(forB);
  expect(state.reportedBlocks).toEqual({});
});

test("sending after signing out: back to the login page, nothing is written", async ({ page, apiMock: state }) => {
  await openForm(page);
  await pick(page, "findings").click();
  await note(page).fill(NOTE);
  state.summaryReport = "expired";
  const write = nextReport(page);
  await send(page).click();
  expect((await write).status).toBe(401);
  await expect(page).toHaveURL(/\/login\?/);
  expect(state.reportedBlocks).toEqual({});
});

test("the demo site's paper pages have no 'report' and no reported mark", async ({ page }) => {
  // expect-missing/paper.txt 拿掉了「回報錯誤」（成員版現在有），示範站的既有測試拿那份清單當防線，所以這裡另外明確斷言
  // （既有的 demo-paper.spec.ts 不動）。
  expect(DEMO_PAPER_PAGES.length).toBeGreaterThan(0);
  for (const path of DEMO_PAPER_PAGES) {
    await page.goto(`${demo.baseURL}${path}`);
    await expect(page.locator("main h1")).toBeVisible();
    await expect(page.getByText(text("paper:paper.report.open"), { exact: true }), path).toHaveCount(0);
    await expect(page.getByText(text("paper:paper.summary.reported"), { exact: true }), path).toHaveCount(0);
    await expect(page.locator("[data-summary-report], [data-report-open], [data-note='reported']"), path).toHaveCount(0);
  }
});

// 設計稿的四個狀態（填表、送出中、沒有成功、已回報）。
const STATES: Record<string, { label: string; prepare: (page: Page, state: ApiMockState) => Promise<void> }> = {
  form: { label: "normal", prepare: async (page) => void (await open(page).click()) },
  sending: {
    label: "normal",
    prepare: async (page, state) => {
      state.summaryReportGate = new Promise(() => {});
      await open(page).click();
      await pick(page, "findings").click();
      await note(page).fill(NOTE);
      await send(page).click();
      await expect(send(page)).toHaveText(text("paper:paper.report.sending"));
    },
  },
  failed: {
    label: "normal",
    prepare: async (page, state) => {
      state.summaryReport = "failed";
      await open(page).click();
      await pick(page, "findings").click();
      await note(page).fill(NOTE);
      await send(page).click();
      await expect(form(page).getByRole("alert")).toBeVisible();
    },
  },
  done: {
    label: "normal",
    prepare: async (page) => {
      await open(page).click();
      await pick(page, "findings").click();
      await note(page).fill(NOTE);
      await send(page).click();
      await expect(mark(page, "findings")).toBeVisible();
    },
  },
};

for (const [name, { label, prepare }] of Object.entries(STATES)) {
  for (const width of [1440, 1000, 500]) {
    test(`summary report ${name} screenshot at ${width}px`, async ({ page, apiMock: state, browserName }) => {
      test.skip(browserName !== "chromium", "screenshots are taken in chromium only (WebKit's screenshot style is blocked by the CSP; see nav.spec.ts)");
      await page.setViewportSize({ width, height: 900 });
      await page.goto(url(label));
      await expect(area(page)).toBeVisible();
      await prepare(page, state);
      await page.evaluate(() => document.fonts.ready);
      // 截圖只存檔供人工看；這裡另外斷言版面沒有壞：沒有橫向捲動，中文重點那一塊（含表單）不超出畫面。
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      const box = await area(page).boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(width);
      await page.screenshot({ path: `${SCREENSHOT_DIR}paper-report-${name}-${width}.png`, fullPage: true });
    });
  }
}
