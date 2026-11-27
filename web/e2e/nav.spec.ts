// (9) 導覽列與帳號選單（Story 3.5、畫面 18、State Patterns「18. 帳號選單」，AR-69）：
// 桌機的帳號選單（Base UI Menu）、手機的滿版選單（Base UI Dialog）、登出的三種結果，
// 導覽列上每個連結的落點（AR-67，和 links.spec.ts 同一套檢查；links.spec.ts 只點 main 裡的），
// 選單打開時的 axe，以及登入頁、帳號選單 1440／1000／500 的截圖（500 是手機的滿版選單）。
import { fileURLToPath } from "node:url";

import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { MEMBERS, gate } from "./api-mock.ts";
import { axeSourceFor } from "./axe-source.ts";
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { expectValidLanding, recordDocumentResponses } from "./landing.ts";
import { SITES } from "./pages.ts";

const member = SITES[0]!;
const SCREENSHOT_DIR = fileURLToPath(new URL("./screenshots/", import.meta.url));
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const NAME = MEMBERS[0]!.display_name;
const LOGIN = TEXT["login:login.button.idle"];
const EXPIRED = TEXT["errors:not_authenticated"];
const ACCOUNT = TEXT["nav:nav.account.trigger"];
const NOTICE = TEXT["nav:nav.account.notice"];
const LOGOUT = TEXT["nav:nav.account.logout"];
const LOGGING_OUT = TEXT["nav:nav.account.loggingOut"];
const LOGOUT_FAILED = TEXT["nav:nav.account.logoutFailed"];
const MENU = TEXT["nav:nav.menu.open"];
const CLOSE_MENU = TEXT["nav:nav.menu.close"];
const DESKTOP = { width: 1280, height: 900 };
const PHONE = { width: 390, height: 844 };

async function openNotice(page: Page, size: { width: number; height: number }): Promise<void> {
  await page.setViewportSize(size);
  await page.goto(`${member.baseURL}/notice`);
  await expect(page.locator("h1")).toHaveText(TEXT["notice:notice.page.title"]);
}

/** 可以點的元素（和 a11y-floor.spec.ts 同一組，另加選單項目）。 */
const INTERACTIVE = "a[href], button, [role=button], [role=menuitem], summary, input, select, textarea";

/** 看得到、但小於 44×44 的可點元素（ADR-0037 第 5 點）；Base UI 的焦點守衛不算（看不到、不能點）。 */
async function smallTapTargets(page: Page): Promise<string[]> {
  return page.evaluate((selector) => {
    return [...document.querySelectorAll<HTMLElement>(selector)]
      .filter((element) => !element.hasAttribute("data-base-ui-focus-guard"))
      .filter((element) => getComputedStyle(element).visibility !== "hidden")
      .map((element) => ({ element, rect: element.getBoundingClientRect() }))
      .filter(({ rect }) => rect.width > 0 && rect.height > 0)
      .filter(({ rect }) => rect.width < 43.5 || rect.height < 43.5)
      .map(({ element, rect }) => `${element.outerHTML.slice(0, 80)} ${rect.width}×${rect.height}`);
  }, INTERACTIVE);
}

/** 換頁時不重新載入整頁的記號：記憶體裡的快取還在，才測得到「有沒有清掉」。 */
async function markDocument(page: Page): Promise<void> {
  await page.evaluate(() => {
    (window as unknown as { __sameDocument?: boolean }).__sameDocument = true;
  });
}

async function isSameDocument(page: Page): Promise<boolean> {
  return page.evaluate(() => (window as unknown as { __sameDocument?: boolean }).__sameDocument === true);
}

/**
 * 按 Esc 關掉選單，選單還沒拆掉、還沒還焦點的時候把焦點移到頁面上（選單外）第一個符合 `selector` 的元素：模擬關的同時
 * 點了或聚焦在別的東西上。選單拆掉時不能把焦點拉回打開它的按鈕（epic-3j 6-2，同 FilterMenus.tsx 的 finalFocus）。
 * 等一個 microtask 再移：手機的滿版選單打開時頁面藏起來，Esc 之後頁面那一次重畫完才看得到、才能聚焦（選單這時還在）。
 */
async function escapeAndFocus(page: Page, selector: string): Promise<void> {
  const moved = await page.evaluate(async (target) => {
    (document.activeElement ?? document.body).dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    await Promise.resolve();
    const outside = [...document.querySelectorAll<HTMLElement>(target)].find((element) => element.closest("[role=dialog], [role=menu]") === null)!;
    outside.focus();
    // 選單還在、焦點真的移過去了，這條測試才有測到「拆掉時焦點已經在別處」。
    return document.querySelector("[role=dialog], [role=menu]") !== null && document.activeElement === outside;
  }, selector);
  expect(moved).toBe(true);
}

async function axeViolations(page: Page): Promise<string[]> {
  // 不檢查 Base UI 的焦點守衛（ADR-0037 第 5 點；Safari 上它還帶 role=button，axe 會誤報沒有名稱）。
  const results = await new AxeBuilder({ page, axeSource: axeSourceFor(page) }).withTags(TAGS).exclude("[data-base-ui-focus-guard]").analyze();
  return results.violations.map((violation) => `${violation.id}: ${violation.help}`);
}

test.describe("desktop nav bar", () => {
  test("has only the brand and the account button", async ({ page }) => {
    await openNotice(page, DESKTOP);
    const header = page.locator("header");
    // 品牌、找論文、我的文獻庫（Story 3.17；AR-67：其他連結還沒做）。
    await expect(header.getByRole("link")).toHaveCount(3);
    await expect(header.getByRole("link").first()).toHaveAttribute("href", "/search");
    await expect(header.getByRole("link").nth(2)).toHaveAttribute("href", "/library");
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

  test("menu items get the hover background on hover and on the arrow keys", async ({ page }) => {
    await openNotice(page, DESKTOP);
    // 設計代幣 menu-item-hover 的實際顏色（Kevin 2026-11-16 決定；設計稿 .amenu .mi:hover）。
    const hover = await page.evaluate(() => {
      const probe = document.createElement("div");
      probe.style.backgroundColor = "var(--color-menu-item-hover)";
      document.body.append(probe);
      const color = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return color;
    });
    expect(hover).not.toBe("rgba(0, 0, 0, 0)");
    const items = page.getByRole("menu").getByRole("menuitem");
    const background = (index: number) =>
      items.nth(index).evaluate((element) => getComputedStyle(element).backgroundColor);

    // 鍵盤：Enter 打開時第一項是目前的那一項，方向鍵往下，底色跟著走，上一項不留。
    const trigger = page.getByRole("button", { name: ACCOUNT });
    await trigger.focus();
    await page.keyboard.press("Enter");
    await expect.poll(() => background(0)).toBe(hover);
    await expect.poll(() => background(1)).toBe("rgba(0, 0, 0, 0)");
    await page.keyboard.press("ArrowDown");
    await expect.poll(() => background(1)).toBe(hover);
    await expect.poll(() => background(0)).toBe("rgba(0, 0, 0, 0)");
    await page.keyboard.press("Escape");

    // 滑鼠：滑過哪一項，哪一項有底色。
    await trigger.click();
    await items.last().hover();
    await expect.poll(() => background(1)).toBe(hover);
    await expect.poll(() => background(0)).toBe("rgba(0, 0, 0, 0)");
  });

  test("focus moved elsewhere while the account menu closes: it stays there, not pulled back to the account button", async ({ page }) => {
    // epic-3j 6-2：finalFocus 給函式時 Base UI 不再判斷「焦點已經移到別處」，AccountMenu.tsx 自己判斷。
    await openNotice(page, DESKTOP);
    await page.getByRole("button", { name: ACCOUNT }).click();
    await expect(page.getByRole("menu")).toBeVisible();
    await escapeAndFocus(page, "header a");
    await expect(page.getByRole("menu")).toHaveCount(0);
    await expect(page.locator("header a").first()).toBeFocused();
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

  test("logout loading: the disabled item still gets the hover background", async ({ page, apiMock }) => {
    // 設計稿的停用項目滑過也有底色；字色維持第三層文字色，不跟著變亮。Base UI 1.9 滑過停用的項目也會設
    // data-highlighted，所以 hover: 與 data-[highlighted]: 兩個底色類別任一個在就會過，兩個都拿掉才失敗。
    const [logoutGate, release] = gate();
    apiMock.logoutGate = logoutGate;
    await openNotice(page, DESKTOP);
    const tokenColor = (property: "backgroundColor" | "color", variable: string) =>
      page.evaluate(
        ([name, value]) => {
          const probe = document.createElement("div");
          probe.style[name] = `var(${value})`;
          document.body.append(probe);
          const color = getComputedStyle(probe)[name];
          probe.remove();
          return color;
        },
        [property, variable] as const,
      );
    const hover = await tokenColor("backgroundColor", "--color-menu-item-hover");
    const tertiary = await tokenColor("color", "--color-text-tertiary");
    expect(hover).not.toBe("rgba(0, 0, 0, 0)");
    await page.getByRole("button", { name: ACCOUNT }).click();
    await page.getByRole("menuitem", { name: LOGOUT }).click();
    const item = page.getByRole("menuitem", { name: LOGGING_OUT });
    await expect(item).toHaveAttribute("aria-disabled", "true");
    const style = () =>
      item.evaluate((element) => {
        const computed = getComputedStyle(element);
        return { background: computed.backgroundColor, color: computed.color };
      });

    // 先把滑鼠移到別的項目上，停用的那一項沒有底色；再滑回來就有。
    await page.getByRole("menuitem", { name: NOTICE }).hover();
    await expect.poll(async () => (await style()).background).toBe("rgba(0, 0, 0, 0)");
    await item.hover();
    await expect.poll(async () => (await style()).background).toBe(hover);
    expect((await style()).color).toBe(tertiary);
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
    await page.locator("header").getByRole("link").first().click();
    // 超過骨架畫面的 0.3 秒：外框（導覽列）不能換成載入中。
    await page.waitForTimeout(800);
    await expect(page.locator("header")).toBeVisible();
    release();
    await expect(page.locator("h1")).toHaveText(TEXT["search:search.page.title"]);
  });

  test("logout success goes to the login page; the back button there needs signing in again", async ({ page }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(`${member.baseURL}/notice?from=start`);
    await expect(page.locator("h1")).toHaveText(TEXT["notice:notice.page.title"]);
    await markDocument(page);
    // 先在站內走一步（多一筆瀏覽紀錄），再登出。
    await page.getByRole("button", { name: ACCOUNT }).click();
    await page.getByRole("menuitem", { name: NOTICE }).click();
    await expect(page).toHaveURL(`${member.baseURL}/notice`);
    await page.getByRole("button", { name: ACCOUNT }).click();
    await page.getByRole("menuitem", { name: LOGOUT }).click();
    await expect(page).toHaveURL(`${member.baseURL}/login`);
    await expect(page.getByRole("button", { name: LOGIN })).toBeVisible();
    // 登出不是「登入過期」。
    await expect(page.locator("[data-login-note]")).toHaveCount(0);

    // 按返回鍵（不重新載入整頁，記憶體裡的快取還在）：回到上一頁要先登入，看不到上一位的名稱。
    await page.goBack();
    await expect(page).toHaveURL(/\/login\?/);
    const url = new URL(page.url());
    expect(url.searchParams.get("next")).toBe("/notice?from=start");
    expect(url.searchParams.get("expired")).toBeNull();
    await expect(page.getByRole("button", { name: LOGIN })).toBeVisible();
    await expect(page.locator("header")).toHaveCount(0);
    await expect(page.getByText(NAME)).toHaveCount(0);
    await expect(page.locator("[data-login-note]")).toHaveCount(0);
    await expect(page.getByText(EXPIRED, { exact: true })).toHaveCount(0);
    expect(await isSameDocument(page)).toBe(true);
  });

  test("A logs out and B logs in in the same tab: nothing of A is left", async ({ page, apiMock }) => {
    const [first, second] = [MEMBERS[0]!, MEMBERS[1]!];
    await openNotice(page, DESKTOP);
    await markDocument(page);
    await page.getByRole("button", { name: ACCOUNT }).click();
    await expect(page.getByRole("menu").getByText(first.display_name, { exact: true })).toBeVisible();
    await page.getByRole("menuitem", { name: LOGOUT }).click();
    await expect(page).toHaveURL(`${member.baseURL}/login`);

    // 下一位登入（假登入清單的第一位換成 B）：同一個分頁、沒有重新載入。
    apiMock.loginList = [second];
    apiMock.requests.length = 0;
    await page.getByRole("button", { name: LOGIN }).click();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    const post = apiMock.requests.find((request) => request.method === "POST");
    expect(post?.body).toEqual({ member_id: second.member_id, next: null });
    await page.getByRole("button", { name: ACCOUNT }).click();
    await expect(page.getByRole("menu").getByText(second.display_name, { exact: true })).toBeVisible();
    await expect(page.getByText(first.display_name)).toHaveCount(0);
    // 「我是誰」重新問過（不是用上一位留在快取裡的）。
    expect(apiMock.requests.filter((request) => request.path === "/api/v1/me").length).toBeGreaterThan(0);
    expect(await isSameDocument(page)).toBe(true);
  });

  test("the account menu closes when the window becomes phone width; focus moves to the menu button", async ({
    page,
  }) => {
    await openNotice(page, DESKTOP);
    const trigger = page.getByRole("button", { name: ACCOUNT });
    await trigger.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("menu").getByRole("menuitem").first()).toBeFocused();

    await page.setViewportSize(PHONE);
    await expect(page.getByRole("menu")).toHaveCount(0);
    await expect(page.getByRole("button", { name: ACCOUNT, includeHidden: true })).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByRole("button", { name: MENU })).toBeFocused();

    // 縮回桌機寬度也不會再出現。
    await page.setViewportSize(DESKTOP);
    await expect(trigger).toBeVisible();
    await expect(page.getByRole("menu")).toHaveCount(0);
    await expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  test("a width that only flashes by does not close the account menu", async ({ page, browserName }) => {
    // 只有 Chromium 拍整頁截圖時會把視窗一閃縮成 1×1；WebKit 拍整頁截圖會塞一段行內樣式，被 CSP 擋下來（測試框架的副作用）。
    test.skip(browserName !== "chromium", "only Chromium shrinks the window for a full-page screenshot");
    await openNotice(page, DESKTOP);
    await page.getByRole("button", { name: ACCOUNT }).click();
    await expect(page.getByRole("menu")).toBeVisible();
    // Chromium 拍整頁截圖時視窗會先縮成 1×1 再變回來（設計稿比對就是這樣拍的）：選單要留著。
    await page.screenshot({ fullPage: true });
    await page.waitForTimeout(400);
    await expect(page.getByRole("menu")).toBeVisible();
    await expect(page.getByRole("button", { name: ACCOUNT })).toHaveAttribute("aria-expanded", "true");
  });

  test("with the account menu open, every clickable item is at least 44×44", async ({ page, apiMock }) => {
    await openNotice(page, DESKTOP);
    await page.getByRole("button", { name: ACCOUNT }).click();
    await expect(page.getByRole("menu").getByRole("menuitem")).toHaveCount(2);
    expect(await smallTapTargets(page)).toEqual([]);

    // 登出失敗、選單裡多一行時也一樣。
    apiMock.logout = "failed";
    await page.getByRole("menuitem", { name: LOGOUT }).click();
    await expect(page.getByRole("menu").getByText(LOGOUT_FAILED, { exact: true })).toBeVisible();
    expect(await smallTapTargets(page)).toEqual([]);
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

  test("focus moved elsewhere while the phone menu closes: it stays there, not pulled back to the menu button", async ({ page }) => {
    // epic-3j 6-2：同帳號選單，MobileMenu.tsx 自己判斷焦點有沒有移到別處。
    await openNotice(page, PHONE);
    await page.getByRole("button", { name: MENU }).click();
    const dialog = page.getByRole("dialog", { name: MENU });
    await expect(dialog).toBeVisible();
    await escapeAndFocus(page, "header a");
    await expect(dialog).toHaveCount(0);
    await expect(page.locator("header a").first()).toBeFocused();
  });

  test("the phone menu has search; it goes there and is marked as the current page", async ({ page }) => {
    // Story 3.14（AR-67）：手機選單的「找論文」（審查 A-3；新加的測試，既有的沒有改）。
    await openNotice(page, PHONE);
    await page.getByRole("button", { name: MENU }).click();
    const dialog = page.getByRole("dialog", { name: MENU });
    const search = dialog.getByRole("link", { name: TEXT["nav:nav.links.search"] });
    await expect(search).toBeVisible();
    await expect(search).toHaveAttribute("href", "/search");
    await search.click();
    await expect(dialog).toBeHidden();
    await expect(page).toHaveURL(`${member.baseURL}/search`);
    await expect(page.locator("h1")).toHaveText(TEXT["search:search.page.title"]);
    await page.getByRole("button", { name: MENU }).click();
    await expect(dialog.getByRole("link", { name: TEXT["nav:nav.links.search"] })).toHaveAttribute("aria-current", "page");
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
    await expect(dialog.locator("[aria-hidden=true]", { hasText: LOGOUT_FAILED })).toBeVisible();
    await dialog.getByRole("link", { name: NOTICE }).click();
    await expect(dialog).toBeHidden();
    await page.getByRole("button", { name: MENU }).click();
    await expect(dialog.getByRole("button", { name: LOGOUT })).toBeVisible();
    // alert 區一直都在（和桌機一樣），清掉的是裡面的字與看得到的那一行。
    await expect(dialog.locator("[aria-hidden=true]", { hasText: LOGOUT_FAILED })).toHaveCount(0);
    await expect(dialog.getByRole("alert")).toHaveText("");
    await expect(dialog.getByRole("button", { name: LOGOUT })).not.toHaveAttribute("aria-describedby");
  });

  test("phone menu labels: one name for the dialog, no expanded state on close, logout failure read like desktop", async ({
    page,
    apiMock,
  }) => {
    apiMock.logout = "failed";
    await openNotice(page, PHONE);
    await page.getByRole("button", { name: MENU }).click();
    const dialog = page.getByRole("dialog", { name: MENU });
    await expect(dialog).toBeVisible();
    // 對話框叫「選單」，裡面的 nav 不再同名（報讀器不念兩次）。
    await expect(dialog.getByRole("navigation")).toHaveCount(1);
    await expect(dialog.getByRole("navigation")).not.toHaveAttribute("aria-label");
    await expect(page.getByRole("navigation", { name: MENU })).toHaveCount(0);
    // 「關閉選單」不是展開類的按鈕。
    await expect(dialog.getByRole("button", { name: CLOSE_MENU })).not.toHaveAttribute("aria-expanded");

    // 登出失敗：看得到的那一行 aria-hidden，「登出」的說明是那句話，對話框裡一直都在的 alert 區念出來。
    const alert = dialog.getByRole("alert");
    await expect(alert).toHaveText("");
    await dialog.getByRole("button", { name: LOGOUT }).click();
    await expect(alert).toHaveText(LOGOUT_FAILED);
    const line = dialog.locator("[aria-hidden=true]", { hasText: LOGOUT_FAILED });
    await expect(line).toBeVisible();
    await expect(dialog.getByRole("button", { name: LOGOUT })).toHaveAccessibleDescription(LOGOUT_FAILED);
    expect(await axeViolations(page)).toEqual([]);
  });

  test("with the phone menu open, every clickable item is at least 44×44", async ({ page, apiMock }) => {
    await openNotice(page, PHONE);
    await page.getByRole("button", { name: MENU }).click();
    const dialog = page.getByRole("dialog", { name: MENU });
    await expect(dialog.getByRole("button", { name: LOGOUT })).toBeVisible();
    expect(await smallTapTargets(page)).toEqual([]);

    apiMock.logout = "failed";
    await dialog.getByRole("button", { name: LOGOUT }).click();
    await expect(dialog.locator("[aria-hidden=true]", { hasText: LOGOUT_FAILED })).toBeVisible();
    expect(await smallTapTargets(page)).toEqual([]);
  });

  test("the phone menu closes at desktop width; focus moves to the account button and it does not come back", async ({
    page,
  }) => {
    await openNotice(page, PHONE);
    await page.getByRole("button", { name: MENU }).click();
    const dialog = page.getByRole("dialog", { name: MENU });
    await expect(dialog).toBeVisible();
    await page.keyboard.press("Tab");
    await page.setViewportSize(DESKTOP);
    await expect(dialog).toHaveCount(0);
    await expect(page.getByRole("button", { name: ACCOUNT })).toBeFocused();

    await page.setViewportSize(PHONE);
    await expect(page.getByRole("button", { name: MENU })).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.locator("main")).toBeVisible();
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
