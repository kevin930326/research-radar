// 全站頁尾（Story 3.15；EXPERIENCE.md「導覽」的頁尾，樣子照設計稿 coverage.html 的頁尾）：成員版每一頁（登入頁除外：
// 它自己排版、沒有導覽列）與示範站每一頁（示範站首頁除外，Epic 13 還沒做）都有；資料來源、資料涵蓋範圍的連結、
// 更新頻率，成員版多一個使用須知。頁尾在內容下面、整頁的最底下（內容很短時貼著視窗底部）。
// 外框的寬度與位置（寫死的預期值）在 page-frame.spec.ts。
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { SITES } from "./pages.ts";

const PHONE = { width: 390, height: 844 };
const DESKTOP = { width: 1280, height: 900 };

for (const site of SITES) {
  for (const path of site.pages.filter((entry) => entry !== "/login")) {
    for (const viewport of [DESKTOP, PHONE]) {
      test(`${site.name} ${path} at ${viewport.width}px has the site footer at the bottom of the page`, async ({ page }) => {
        await page.setViewportSize(viewport);
        await page.goto(`${site.baseURL}${path}`);
        await expect(page.locator("main h1")).toBeVisible();
        const footer = page.locator("footer[data-site-footer]");
        await expect(footer).toHaveCount(1);
        await expect(footer.locator("[data-footer-source]")).toHaveText(TEXT["common:footer.source.text"]!);
        await expect(footer.locator("[data-footer-frequency]")).toHaveText(TEXT["common:footer.update.frequency"]!);
        const links = footer.getByRole("link");
        if (site.name === "member") {
          await expect(links).toHaveText([TEXT["common:footer.links.coverage"]!, TEXT["common:footer.links.notice"]!]);
          await expect(links.nth(1)).toHaveAttribute("href", "/notice");
        } else {
          await expect(links).toHaveText([TEXT["common:footer.links.coverage"]!]);
        }
        await expect(links.first()).toHaveAttribute("href", "/coverage");
        // 在內容下面、整頁的最底下。
        const main = (await page.locator("main").boundingBox())!;
        const box = (await footer.boundingBox())!;
        expect(box.y).toBeGreaterThanOrEqual(main.y + main.height - 0.5);
        const pageHeight = await page.evaluate(() => document.documentElement.scrollHeight);
        // scrollHeight 是整數（無條件捨去小數），差在 1px 以內。
        expect(Math.abs(box.y + box.height - Math.max(pageHeight, viewport.height))).toBeLessThan(1);
      });
    }
  }
}

test("member: the login page has no site footer", async ({ page, apiMock }) => {
  apiMock.member = null;
  await page.goto(`${SITES[0]!.baseURL}/login`);
  await expect(page.locator("main")).toBeVisible();
  await expect(page.locator("footer")).toHaveCount(0);
});

test("footer links go to coverage and the notice", async ({ page }) => {
  await page.setViewportSize(DESKTOP);
  await page.goto(`${SITES[0]!.baseURL}/search`);
  const footer = page.locator("footer[data-site-footer]");
  await footer.getByRole("link", { name: TEXT["common:footer.links.notice"]! }).click();
  await expect(page.locator("main h1")).toHaveText(TEXT["notice:notice.page.title"]!);
  await page.locator("footer[data-site-footer]").getByRole("link", { name: TEXT["common:footer.links.coverage"]! }).click();
  await expect(page.locator("main h1")).toHaveText(TEXT["coverage:coverage.page.title"]!);
});
