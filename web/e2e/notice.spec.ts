// (2) 使用須知（畫面 17）：只有第 3、4 段，文字和 EXPERIENCE.md 逐字相同，h1 只有一個，
// 第 4 段最後一行的「資料涵蓋範圍」是連結（Story 3.15）。
import { ABSENT, TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { SITES } from "./pages.ts";

const member = SITES[0]!;
const CAVEATS = ["rare", "supported", "evidenceMap", "trendLag"] as const;

test("member: / goes to the search page", async ({ page }) => {
  await page.goto(`${member.baseURL}/`);
  await expect(page).toHaveURL(`${member.baseURL}/search`);
  await expect(page.locator("h1")).toHaveText(TEXT["search:search.page.title"]);
});

test("member: notice shows only paragraphs 3 and 4, word for word", async ({ page }) => {
  await page.goto(`${member.baseURL}/notice`);

  const headings = page.locator("h1");
  await expect(headings).toHaveCount(1);
  await expect(headings).toHaveText(TEXT["notice:notice.page.title"]);

  const sections = page.locator("main section");
  await expect(sections).toHaveCount(2);
  await expect(sections.locator("h2")).toHaveText([
    TEXT["notice:notice.limits.heading"],
    TEXT["notice:notice.numbers.heading"],
  ]);
  await expect(sections.nth(0).locator("p")).toHaveText([
    TEXT["notice:notice.limits.noQuestions"],
    TEXT["notice:notice.limits.noPasteable"],
    TEXT["notice:notice.limits.screeningOnly"],
  ]);

  const numbers = sections.nth(1).locator("p");
  await expect(numbers).toHaveText([
    ...CAVEATS.flatMap((key) => [
      TEXT[`caveat:caveat.${key}.warning`],
      TEXT[`caveat:caveat.${key}.explanation`],
    ]),
    TEXT["notice:notice.numbers.coverage"],
  ]);

  // 第 4 段最後一行的「資料涵蓋範圍」是連結（Story 3.15），整行本身不在連結裡；這一頁只有這一個連結。
  const lastLine = numbers.last();
  await expect(lastLine.locator("a")).toHaveCount(1);
  await expect(lastLine.locator("a")).toHaveAttribute("href", "/coverage");
  expect(await lastLine.evaluate((element) => element.closest("a") === null)).toBe(true);
  await expect(page.locator("main a")).toHaveCount(1);

  const bodyText = (await page.locator("body").innerText()).replace(/\s+/g, "");
  for (const text of ABSENT) {
    expect(bodyText).not.toContain(text.replace(/\s+/g, ""));
  }
  // 導覽列在 3.5 接上（內容由 nav.spec.ts 檢查）；全站頁尾在 3.15 接上（內容由 footer.spec.ts 檢查）。
  await expect(page.locator("header")).toHaveCount(1);
  await expect(page.locator("footer")).toHaveCount(1);
});

test("member: page language is Traditional Chinese", async ({ page }) => {
  await page.goto(`${member.baseURL}/notice`);
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-Hant");
});
