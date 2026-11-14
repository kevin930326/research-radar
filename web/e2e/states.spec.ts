// (5) 全站共用狀態：找不到這一頁（成員版有回首頁連結、示範站沒有）、沒有網路。
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { MISSING_PATH, SITES } from "./pages.ts";

const [member, demo] = [SITES[0]!, SITES[1]!];
const NOT_FOUND = TEXT["common:states.notFound.message"];

test("member: an unknown address shows not found with a link home", async ({ page }) => {
  await page.goto(`${member.baseURL}${MISSING_PATH}`);
  await expect(page.getByText(NOT_FOUND, { exact: true })).toBeVisible();

  const home = page.getByRole("link", { name: TEXT["common:states.notFound.home"] });
  await expect(home).toHaveAttribute("href", "/");
  await home.click();
  await expect(page).toHaveURL(`${member.baseURL}/notice`);
  await expect(page.getByText(NOT_FOUND, { exact: true })).toHaveCount(0);
});

test("demo: no notice page, and not found has no link home", async ({ page }) => {
  for (const path of ["/", "/notice", MISSING_PATH]) {
    await page.goto(`${demo.baseURL}${path}`);
    await expect(page.getByText(NOT_FOUND, { exact: true })).toBeVisible();
    await expect(page.locator("a")).toHaveCount(0);
    await expect(page.locator("h1")).toHaveCount(0);
  }
});

test("member: offline shows a message and keeps the loaded content", async ({ page, context }) => {
  const offline = page.getByText(TEXT["common:states.offline.message"], { exact: true });
  await page.goto(`${member.baseURL}/notice`);
  await expect(page.locator("h1")).toBeVisible();
  await expect(offline).toHaveCount(0);

  await context.setOffline(true);
  await expect(offline).toBeVisible();
  await expect(page.locator("h1")).toHaveText(TEXT["notice:notice.page.title"]);
  await expect(page.locator("main section")).toHaveCount(2);

  await context.setOffline(false);
  await expect(offline).toHaveCount(0);
});
