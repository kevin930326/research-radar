// (9) 導覽列與帳號選單（Story 3.5、畫面 18、State Patterns「18. 帳號選單」，AR-69）：
// 桌機的帳號選單（Base UI Menu）、手機的滿版選單（Base UI Dialog）、登出的三種結果，
// 導覽列上每個連結的落點（AR-67，和 links.spec.ts 同一套檢查；links.spec.ts 只點 main 裡的），
// 選單打開時的 axe，以及登入頁、帳號選單 1440／1000／500 的截圖（500 是手機的滿版選單）。
import { fileURLToPath } from "node:url";

import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { MEMBERS, gate } from "./api-mock.ts";
import { DESIGN_ONLY, TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { expectValidLanding, recordDocumentResponses } from "./landing.ts";
import { SITES } from "./pages.ts";

const member = SITES[0]!;
const SCREENSHOT_DIR = fileURLToPath(new URL("./screenshots/", import.meta.url));
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const NAME = MEMBERS[0]!.display_name;
const ACCOUNT = TEXT["nav:nav.account.trigger"];
const NOTICE = TEXT["nav:nav.account.notice"];
const LOGOUT = TEXT["nav:nav.account.logout"];
const LOGGING_OUT = TEXT["nav:nav.account.loggingOut"];
const LOGOUT_FAILED = TEXT["nav:nav.account.logoutFailed"];
const MENU = TEXT["nav:nav.menu.open"];
const CLOSE_MENU = DESIGN_ONLY["nav:nav.menu.close"]!;
const DESKTOP = { width: 1280, height: 900 };
const PHONE = { width: 390, height: 844 };

async function openNotice(page: Page, size: { width: number; height: number }): Promise<void> {
  await page.setViewportSize(size);
  await page.goto(`${member.baseURL}/notice`);
  await expect(page.locator("h1")).toHaveText(TEXT["notice:notice.page.title"]);
}

async function axeViolations(page: Page): Promise<string[]> {
  // 不檢查 Base UI 的焦點守衛（ADR-0037 第 5 點；Safari 上它還帶 role=button，axe 會誤報沒有名稱）。
  const results = await new AxeBuilder({ page }).withTags(TAGS).exclude("[data-base-ui-focus-guard]").analyze();
  return results.violations.map((violation) => `${violation.id}: ${violation.help}`);
}

test.describe("desktop nav bar", () => {
  test("has only the brand and the account button", async ({ page }) => {
    await openNotice(page, DESKTOP);
    const header = page.locator("header");
    await expect(header.getByRole("link")).toHaveCount(1);
    await expect(header.getByRole("link")).toHaveAttribute("href", "/notice");
    await expect(header.getByRole("button", { name: ACCOUNT })).toBeVisible();
    await expect(header.getByRole("button", { name: MENU })).toBeHidden();
  });

  test("the account menu lists the name, the notice page and logout", async ({ page }) => {
    await openNotice(page, DESKTOP);
    const trigger = page.getByRole("button", { name: ACCOUNT });
    await expect(trigger).toHaveAttribute("aria-expanded", "false");
    await trigger.click();
    await expect(trigger).toHaveAttribute("aria-expanded", "true");
    const menu = page.getByRole("menu");
    await expect(menu).toBeVisible();
    await expect(menu.getByText(NAME, { exact: true })).toBeVisible();
    await expect(menu.getByRole("menuitem")).toHaveText([NOTICE, LOGOUT]);
  });

  test("keyboard: Enter opens, arrows move, Esc closes and focus returns", async ({ page }) => {
    await openNotice(page, DESKTOP);
    const trigger = page.getByRole("button", { name: ACCOUNT });
    await trigger.focus();
    await page.keyboard.press("Enter");
    const items = page.getByRole("menu").getByRole("menuitem");
    await expect(items.first()).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await expect(items.nth(1)).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("menu")).toBeHidden();
    await expect(trigger).toBeFocused();
    await expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  test("logout loading: the item says logging out and is disabled", async ({ page, apiMock }) => {
    const [logoutGate, release] = gate();
    apiMock.logoutGate = logoutGate;
    await openNotice(page, DESKTOP);
    await page.getByRole("button", { name: ACCOUNT }).click();
    await page.getByRole("menuitem", { name: LOGOUT }).click();
    const item = page.getByRole("menuitem", { name: LOGGING_OUT });
    await expect(item).toBeVisible();
    await expect(item).toHaveAttribute("aria-disabled", "true");
    await expect(item).toHaveAttribute("aria-busy", "true");
    release();
    await expect(page).toHaveURL(`${member.baseURL}/login`);
  });

  test("logout failed: one line above logout, the menu stays open", async ({ page, apiMock }) => {
    apiMock.logout = "failed";
    await openNotice(page, DESKTOP);
    await page.getByRole("button", { name: ACCOUNT }).click();
    await page.getByRole("menuitem", { name: LOGOUT }).click();
    const menu = page.getByRole("menu");
    // 選單裡不能放 role=alert（axe：aria-required-children）：看得到的那一行 aria-hidden，
    // 報讀器從「登出」的說明與選單外的 alert 區知道。
    const error = menu.getByText(LOGOUT_FAILED, { exact: true });
    await expect(error).toBeVisible();
    await expect(page.locator("header").getByRole("alert")).toHaveText(LOGOUT_FAILED);
    const logout = menu.getByRole("menuitem", { name: LOGOUT });
    await expect(logout).toHaveAccessibleDescription(LOGOUT_FAILED);
    await expect(logout).not.toHaveAttribute("aria-disabled", "true");
    const errorBox = await error.boundingBox();
    const logoutBox = await logout.boundingBox();
    expect(errorBox!.y + errorBox!.height).toBeLessThanOrEqual(logoutBox!.y);
    expect(await axeViolations(page)).toEqual([]);

    // 關掉再打開不留上一次的錯誤。
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: ACCOUNT }).click();
    await expect(page.getByRole("menu").getByText(LOGOUT_FAILED, { exact: true })).toHaveCount(0);
  });

  test("closing the menu while logging out does not leave a stale error", async ({ page, apiMock }) => {
    const [logoutGate, release] = gate();
    apiMock.logoutGate = logoutGate;
    apiMock.logout = "failed";
    await openNotice(page, DESKTOP);
    await page.getByRole("button", { name: ACCOUNT }).click();
    await page.getByRole("menuitem", { name: LOGOUT }).click();
    await expect(page.getByRole("menuitem", { name: LOGGING_OUT })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("menu")).toBeHidden();
    const failed = page.waitForResponse((response) => response.url().endsWith("/auth/logout"));
    release();
    await failed;
    await page.getByRole("button", { name: ACCOUNT }).click();
    await expect(page.getByRole("menuitem", { name: LOGOUT })).toBeVisible();
    await expect(page.getByRole("menu").getByText(LOGOUT_FAILED, { exact: true })).toHaveCount(0);
  });

  test("the nav bar stays while a slow who-am-i check runs", async ({ page, apiMock }) => {
    await openNotice(page, DESKTOP);
    const [meGate, release] = gate();
    apiMock.meGate = meGate;
    await page.locator("header").getByRole("link").click();
    // 超過骨架畫面的 0.3 秒：外框（導覽列）不能換成載入中。
    await page.waitForTimeout(800);
    await expect(page.locator("header")).toBeVisible();
    release();
    await expect(page.locator("h1")).toHaveText(TEXT["notice:notice.page.title"]);
  });

  test("logout success goes to the login page; back there needs signing in again", async ({ page }) => {
    await openNotice(page, DESKTOP);
    await page.getByRole("button", { name: ACCOUNT }).click();
    await page.getByRole("menuitem", { name: LOGOUT }).click();
    await expect(page).toHaveURL(`${member.baseURL}/login`);
    await expect(page.getByRole("button", { name: TEXT["login:login.button.idle"] })).toBeVisible();
    // 登出不是「登入過期」。
    await expect(page.locator("[data-login-note]")).toHaveCount(0);

    await page.goto(`${member.baseURL}/notice`);
    await expect(page).toHaveURL(/\/login\?next=/);
  });

  test("no axe violations with the account menu open", async ({ page }) => {
    await openNotice(page, DESKTOP);
    await page.getByRole("button", { name: ACCOUNT }).click();
    await expect(page.getByRole("menu")).toBeVisible();
    expect(await axeViolations(page)).toEqual([]);
  });
});

test.describe("phone menu", () => {
  test("the menu button opens a full-screen menu; close and Esc shut it", async ({ page }) => {
    await openNotice(page, PHONE);
    await expect(page.getByRole("button", { name: ACCOUNT })).toBeHidden();
    const open = page.getByRole("button", { name: MENU });
    await open.click();
    const dialog = page.getByRole("dialog", { name: MENU });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText(NAME, { exact: true })).toBeVisible();
    await expect(dialog.getByRole("link", { name: NOTICE })).toBeVisible();
    await expect(dialog.getByRole("button", { name: LOGOUT })).toBeVisible();
    // 頁面本身藏起來（設計稿整頁換成選單）。
    await expect(page.locator("main")).toBeHidden();

    await dialog.getByRole("button", { name: CLOSE_MENU }).click();
    await expect(dialog).toBeHidden();
    await expect(page.locator("main")).toBeVisible();
    await expect(open).toBeFocused();

    await open.click();
    await expect(dialog).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(open).toBeFocused();
  });

  test("logout loading and failed in the phone menu", async ({ page, apiMock }) => {
    const [logoutGate, release] = gate();
    apiMock.logoutGate = logoutGate;
    apiMock.logout = "failed";
    await openNotice(page, PHONE);
    await page.getByRole("button", { name: MENU }).click();
    const dialog = page.getByRole("dialog", { name: MENU });
    await dialog.getByRole("button", { name: LOGOUT }).click();
    const pending = dialog.getByRole("button", { name: LOGGING_OUT });
    await expect(pending).toHaveAttribute("aria-disabled", "true");
    await expect(pending).toHaveAttribute("aria-busy", "true");
    release();
    await expect(dialog.getByRole("alert")).toHaveText(LOGOUT_FAILED);
    await expect(dialog.getByRole("button", { name: LOGOUT })).toBeVisible();
    expect(await axeViolations(page)).toEqual([]);

    apiMock.logout = "ok";
    apiMock.logoutGate = null;
    await dialog.getByRole("button", { name: LOGOUT }).click();
    await expect(page).toHaveURL(`${member.baseURL}/login`);
  });

  test("closing the menu through a link clears a failed logout", async ({ page, apiMock }) => {
    apiMock.logout = "failed";
    await openNotice(page, PHONE);
    await page.getByRole("button", { name: MENU }).click();
    const dialog = page.getByRole("dialog", { name: MENU });
    await dialog.getByRole("button", { name: LOGOUT }).click();
    await expect(dialog.getByRole("alert")).toBeVisible();
    await dialog.getByRole("link", { name: NOTICE }).click();
    await expect(dialog).toBeHidden();
    await page.getByRole("button", { name: MENU }).click();
    await expect(dialog.getByRole("button", { name: LOGOUT })).toBeVisible();
    await expect(dialog.getByRole("alert")).toHaveCount(0);
  });

  test("the menu closes when the window becomes desktop width", async ({ page }) => {
    await openNotice(page, PHONE);
    await page.getByRole("button", { name: MENU }).click();
    await expect(page.getByRole("dialog", { name: MENU })).toBeVisible();
    await page.setViewportSize(DESKTOP);
    await expect(page.getByRole("dialog", { name: MENU })).toHaveCount(0);
    await expect(page.locator("main")).toBeVisible();
  });

  test("no axe violations with the phone menu open", async ({ page }) => {
    await openNotice(page, PHONE);
    await page.getByRole("button", { name: MENU }).click();
    await expect(page.getByRole("dialog", { name: MENU })).toBeVisible();
    expect(await axeViolations(page)).toEqual([]);
  });
});

// 導覽列與選單裡的每個連結：落點必須是有效頁面（AR-67）。
test.describe("nav links land on real pages", () => {
  const cases: { label: string; size: typeof DESKTOP; open: (page: Page) => Promise<void>; links: (page: Page) => ReturnType<Page["locator"]> }[] = [
    { label: "desktop header", size: DESKTOP, open: async () => {}, links: (page) => page.locator("header a[href]") },
    {
      label: "desktop account menu",
      size: DESKTOP,
      open: async (page) => {
        await page.getByRole("button", { name: ACCOUNT }).click();
        await expect(page.getByRole("menu")).toBeVisible();
      },
      links: (page) => page.getByRole("menu").locator("a[href]"),
    },
    {
      label: "phone menu",
      size: PHONE,
      open: async (page) => {
        await page.getByRole("button", { name: MENU }).click();
        await expect(page.getByRole("dialog")).toBeVisible();
      },
      links: (page) => page.getByRole("dialog").locator("a[href]"),
    },
  ];
  for (const { label, size, open, links } of cases) {
    test(label, async ({ page }) => {
      await openNotice(page, size);
      await open(page);
      const count = await links(page).count();
      expect(count).toBeGreaterThan(0);
      for (let index = 0; index < count; index += 1) {
        await openNotice(page, size);
        await open(page);
        const responses = recordDocumentResponses(page);
        await links(page).nth(index).click();
        await page.waitForLoadState();
        await expectValidLanding(page, member, responses, `${label} link ${index}`);
        // 登入頁也在頁面清單裡，但已登入時點導覽列的連結不該落到登入頁。
        await expect(page).not.toHaveURL(/\/login/);
        // 換頁後選單關上。
        await expect(page.getByRole("menu")).toHaveCount(0);
        await expect(page.getByRole("dialog")).toHaveCount(0);
      }
    });
  }
});

for (const width of [1440, 1000, 500]) {
  test(`login and account menu screenshots at ${width}px`, async ({ page, browserName, apiMock }) => {
    test.skip(browserName !== "chromium", "screenshots are taken in chromium only");
    await page.setViewportSize({ width, height: 900 });

    apiMock.member = null;
    await page.goto(`${member.baseURL}/login`);
    await expect(page.getByRole("button")).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `${SCREENSHOT_DIR}login-${width}.png`, fullPage: true });

    apiMock.member = MEMBERS[0]!;
    await page.goto(`${member.baseURL}/notice`);
    await expect(page.locator("h1")).toBeVisible();
    if (width >= 900) {
      await page.getByRole("button", { name: ACCOUNT }).click();
      await expect(page.getByRole("menu")).toBeVisible();
    } else {
      await page.getByRole("button", { name: MENU }).click();
      await expect(page.getByRole("dialog")).toBeVisible();
    }
    await page.evaluate(() => document.fonts.ready);
    // 等淡入結束再拍。
    await page.waitForFunction(() => document.getAnimations().every((animation) => animation.playState !== "running"));
    await page.screenshot({ path: `${SCREENSHOT_DIR}account-menu-${width}.png` });
  });
}
