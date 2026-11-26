// 修正分類（Story 3.11；EXPERIENCE.md 畫面 8 第 2 點；設計稿 paper.html 的「修正分類」六個狀態）：表單在分類那一行下面原地展開、
// 目前的分類先選好、沒改就不能送；送出中、沒有成功（選的與寫的都留著）、送出後（「◆ 已核對」＋人數、中文重點的標章改寫全文）；
// 人數按了展開歷史；寫入帶預期成員，另一個分頁換了人時什麼都不寫、整頁換成現在的人（ADR-0055）；小的可點的東西可點範圍至少 44px；
// 1440／1000／500 三種寬度、六個狀態的整頁截圖。資料是 fixtures/papers.json 的假論文（替身 e2e/api-mock.ts）。
import { fileURLToPath } from "node:url";

import type { Page } from "@playwright/test";

import { EXPECTED_MEMBER_HEADER, MEMBERS, PAPER_FIXTURES, gate, paperId, paperPage, type ApiMockState } from "./api-mock.ts";
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { SITES } from "./pages.ts";

const member = SITES[0]!;
const SCREENSHOT_DIR = fileURLToPath(new URL("./screenshots/", import.meta.url));
const DESKTOP = { width: 1280, height: 900 };
const url = (label: string) => `${member.baseURL}/papers/${paperId(label)}`;
const text = (key: string) => TEXT[key]!;
const fill = (template: string, values: Record<string, string | number>) =>
  template.replace(/\{\{(\w+)\}\}/g, (_, name: string) => String(values[name]));

// 假資料裡「意見不同」那一篇的兩筆歷史（新的在前；名字是設計稿的示意名字）。
const HISTORY = PAPER_FIXTURES.papers[paperId("reported")]!.classificationHistory!;
const REASON = HISTORY[0]!.reason!;

const area = (page: Page) => page.locator("main [data-classification-fix]");
const form = (page: Page) => area(page).locator("[data-fix-form]");
const pick = (page: Page, key: string) => form(page).locator(`[data-fix-pick="${key}"]`);
const send = (page: Page) => form(page).locator("[data-fix-send]");
const reason = (page: Page) => form(page).getByLabel(text("paper:paper.fix.reason"));

/** 下一個修正分類的送出：帶的預期成員、本文、替身回的狀態。 */
function nextFix(page: Page) {
  return page
    .waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname.endsWith("/classification-fixes"))
    .then(async (response) => ({
      expected: (await response.request().allHeaders())[EXPECTED_MEMBER_HEADER],
      body: response.request().postDataJSON() as unknown,
      status: response.status(),
    }));
}

async function openForm(page: Page, label = "normal") {
  await page.setViewportSize(DESKTOP);
  await page.goto(url(label));
  await area(page).locator("[data-fix-open]").click();
  await expect(form(page)).toBeVisible();
}

test("the form opens in place below the classification row with the current classification picked", async ({ page }) => {
  await openForm(page);
  const current = paperPage("normal").classification.value!;
  for (const key of [...current.topic!.keys, ...current.method!.keys]) {
    await expect(pick(page, key)).toHaveAttribute("aria-pressed", "true");
  }
  await expect(pick(page, "erp")).toHaveAttribute("aria-pressed", "false");
  // 表單開著時「修正分類」先不顯示；沒改不能送。
  await expect(area(page).locator("[data-fix-open]")).toHaveCount(0);
  await expect(send(page)).toBeDisabled();
  await expect(form(page)).toContainText(text("paper:paper.fix.hint"));
  // 一組一個都不選也不能送。
  for (const key of current.method!.keys) await pick(page, key).click();
  await pick(page, "erp").click();
  await expect(send(page)).toBeDisabled();
  await pick(page, "experiment").click();
  await expect(send(page)).toBeEnabled();
  // 取消：表單收起來，「修正分類」回來。
  await form(page).locator("[data-fix-cancel]").click();
  await expect(form(page)).toHaveCount(0);
  await expect(area(page).locator("[data-fix-open]")).toBeVisible();
});

test("'N characters left' only shows near the 100-character limit", async ({ page }) => {
  await openForm(page);
  await reason(page).fill("x".repeat(79));
  await expect(form(page).locator("[data-fix-remaining]")).toHaveCount(0);
  await reason(page).fill("x".repeat(80));
  await expect(form(page).locator("[data-fix-remaining]")).toHaveText(fill(text("paper:paper.fix.remaining"), { n: 20 }));
  // 輸入框最多 100 字。
  await reason(page).fill("x".repeat(120));
  await expect(reason(page)).toHaveValue("x".repeat(100));
  await expect(form(page).locator("[data-fix-remaining]")).toHaveText(fill(text("paper:paper.fix.remaining"), { n: 0 }));
});

test("sending: the button says sending, picks and reason are locked, cancel is hidden; done: verified and the count", async ({ page, apiMock: state }) => {
  const [hold, release] = gate();
  state.classificationFixGate = hold;
  await openForm(page);
  await pick(page, "erp").click();
  await reason(page).fill(`  ${REASON}  `);
  const write = nextFix(page);
  await send(page).click();
  await expect(send(page)).toHaveText(text("paper:paper.fix.sending"));
  await expect(send(page)).toBeDisabled();
  await expect(pick(page, "erp")).toBeDisabled();
  await expect(reason(page)).toHaveAttribute("readonly", "");
  await expect(form(page).locator("[data-fix-cancel]")).toHaveCount(0);
  release();
  expect(await write).toEqual({
    expected: MEMBERS[0]!.member_id,
    body: { topic: ["platforms", "trust", "erp"], method: ["experiment"], reason: REASON },
    status: 200,
  });
  await expect(form(page)).toHaveCount(0);
  const classification = page.locator("main [data-block='classification']");
  await expect(classification.locator("[data-ai-badge='verified']")).toHaveText(text("ai:ai.badge.verified"));
  await expect(classification.locator("[data-fix-count]")).toHaveText(fill(text("paper:paper.classification.fixed"), { members: 1 }));
  await expect(classification.locator("[data-tag]")).toContainText(["ERP"]);
  // 分類核對過：第一個未核對的標章（寫全文）移到中文重點。
  await expect(page.locator("main [data-block='zh_summary'] [data-ai-badge]")).toHaveText(text("ai:ai.badge.full"));
  await expect(area(page).locator("[data-fix-open]")).toBeVisible();
  // 展開歷史：剛才那一筆，名字是登入的那一位。
  await classification.locator("[data-fix-count]").click();
  await expect(area(page).locator("[data-fix-entry]")).toHaveCount(1);
  await expect(area(page).locator("[data-fix-entry]")).toContainText(MEMBERS[0]!.display_name);
  await expect(area(page).locator("[data-fix-entry]")).toContainText(REASON);
  // 重新整理之後照伺服器的資料（替身記住了修正）。
  await page.reload();
  await expect(page.locator("main [data-block='classification'] [data-fix-count]")).toHaveText(fill(text("paper:paper.classification.fixed"), { members: 1 }));
});

test("failed: the message shows above the buttons and everything picked and typed stays", async ({ page, apiMock: state }) => {
  state.classificationFix = "failed";
  await openForm(page);
  await pick(page, "erp").click();
  await reason(page).fill(REASON);
  await send(page).click();
  const alert = form(page).getByRole("alert");
  await expect(alert).toHaveText(text("paper:paper.fix.failed"));
  await expect(pick(page, "erp")).toHaveAttribute("aria-pressed", "true");
  await expect(reason(page)).toHaveValue(REASON);
  await expect(send(page)).toBeEnabled();
  await expect(send(page)).toHaveText(text("paper:paper.fix.send"));
  // 再試一次就成功。
  state.classificationFix = "ok";
  await send(page).click();
  await expect(form(page)).toHaveCount(0);
});

test("the count opens the history: newest first, the first of two marked current, reasons on their own line", async ({ page }) => {
  await page.setViewportSize(DESKTOP);
  await page.goto(url("reported"));
  const count = area(page).locator("[data-fix-count]");
  await expect(count).toHaveText(fill(text("paper:paper.classification.disagree"), { members: 2 }));
  await expect(count).toHaveAttribute("aria-expanded", "false");
  await count.click();
  await expect(count).toHaveAttribute("aria-expanded", "true");
  const entries = area(page).locator("[data-fix-entry]");
  await expect(entries).toHaveCount(2);
  await expect(entries.nth(0)).toContainText(HISTORY[0]!.fixed_at.slice(0, 10));
  await expect(entries.nth(0)).toContainText(HISTORY[0]!.member_name);
  await expect(entries.nth(0)).toContainText(text("paper:paper.fix.current"));
  await expect(entries.nth(0)).toContainText(REASON);
  await expect(entries.nth(1)).toContainText(HISTORY[1]!.member_name);
  await expect(entries.nth(1)).not.toContainText(text("paper:paper.fix.current"));
  await count.click();
  await expect(entries).toHaveCount(0);
});

test("a classification that failed has no fix entry", async ({ page }) => {
  await page.goto(url("all-failed"));
  await expect(page.locator("main [data-block='classification']")).toBeVisible();
  await expect(page.locator("main [data-classification-fix]")).toHaveCount(0);
  await expect(page.getByText(text("paper:paper.fix.open"), { exact: true })).toHaveCount(0);
});

test("small clickables are at least 44×44 while the row keeps the design's height", async ({ page }) => {
  // 看起來 28px（人數、「修正分類」）或 32px（表單的標籤）；按鈕本身至少 44×44，用負外距不撐開那一行（ADR-0037）。
  // 打開頁面時就看得到的那兩個 a11y-floor.spec.ts 也會量；表單裡的要打開才看得到，在這裡量。
  await page.setViewportSize(DESKTOP);
  await page.goto(url("reported"));
  const atLeast = async (selector: string) => {
    for (const box of await area(page).locator(selector).evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().toJSON() as DOMRect))) {
      expect(box.width, selector).toBeGreaterThanOrEqual(44);
      expect(box.height, selector).toBeGreaterThanOrEqual(44);
    }
  };
  await atLeast("[data-fix-count]");
  await atLeast("[data-fix-open]");
  await area(page).locator("[data-fix-open]").click();
  await atLeast("[data-fix-pick]");
  await atLeast("[data-fix-send]");
  await atLeast("[data-fix-cancel]");
  // 表單的標籤看起來 32px 高、兩排之間 12px（設計稿 button.pk 與 gap 12px 8px）。手機寬度才會換成好幾排（假資料只有 8 個主題）。
  await page.setViewportSize({ width: 390, height: 900 });
  const pills = await form(page).locator("[data-fix-group='topic'] [data-fix-pick] > span").evaluateAll((spans) =>
    spans.map((span) => span.getBoundingClientRect().toJSON() as DOMRect),
  );
  expect(pills.every((pill) => pill.height === 32)).toBe(true);
  const rows = [...new Set(pills.map((pill) => pill.top))].sort((a, b) => a - b);
  expect(rows.length).toBeGreaterThan(1);
  expect(rows[1]! - rows[0]!).toBe(32 + 12);
});

test("B signs in elsewhere and A sends a fix: nothing is written, the page becomes B's (ADR-0055)", async ({ page, apiMock: state }) => {
  const B = MEMBERS[1]!;
  const forB = "Paper title member B sees";
  await openForm(page);
  await pick(page, "erp").click();
  state.member = B;
  state.paperByMember = { [B.member_id]: { ...paperPage("normal"), paper: { ...paperPage("normal").paper, title: forB } } };
  const write = nextFix(page);
  await send(page).click();
  expect((await write).status).toBe(409);
  expect((await write).expected).toBe(MEMBERS[0]!.member_id);
  await expect(page.locator("h1")).toHaveText(forB);
  expect(state.fixHistories).toEqual({});
  expect(state.fixedBlocks).toEqual({});
});

// 設計稿的六個狀態（選分類、送出中、沒有成功、已核對、意見不同、看歷史）。
const STATES: Record<string, { label: string; prepare: (page: Page, state: ApiMockState) => Promise<void> }> = {
  form: { label: "normal", prepare: async (page) => void (await area(page).locator("[data-fix-open]").click()) },
  sending: {
    label: "normal",
    prepare: async (page, state) => {
      state.classificationFixGate = new Promise(() => {});
      await area(page).locator("[data-fix-open]").click();
      await pick(page, "erp").click();
      await send(page).click();
      await expect(send(page)).toHaveText(text("paper:paper.fix.sending"));
    },
  },
  failed: {
    label: "normal",
    prepare: async (page, state) => {
      state.classificationFix = "failed";
      await area(page).locator("[data-fix-open]").click();
      await pick(page, "erp").click();
      await reason(page).fill(REASON);
      await send(page).click();
      await expect(form(page).getByRole("alert")).toBeVisible();
    },
  },
  done: { label: "fixed", prepare: async () => {} },
  differ: { label: "reported", prepare: async () => {} },
  history: { label: "reported", prepare: async (page) => void (await area(page).locator("[data-fix-count]").click()) },
};

for (const [name, { label, prepare }] of Object.entries(STATES)) {
  for (const width of [1440, 1000, 500]) {
    test(`classification fix ${name} screenshot at ${width}px`, async ({ page, apiMock: state, browserName }) => {
      test.skip(browserName !== "chromium", "screenshots are taken in chromium only");
      await page.setViewportSize({ width, height: 900 });
      await page.goto(url(label));
      await expect(area(page)).toBeVisible();
      await prepare(page, state);
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: `${SCREENSHOT_DIR}paper-fix-${name}-${width}.png`, fullPage: true });
    });
  }
}
