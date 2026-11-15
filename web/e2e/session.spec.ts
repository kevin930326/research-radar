// (8) 登入頁與登入狀態（Story 3.5、State Patterns「10. 登入」與全站共用的「登入過期」，AR-69）：
// 每一列狀態各一個測試。後端用 api-mock.ts 代替（fixtures.ts 自動裝上，預設已登入）。
import { MEMBERS, gate } from "./api-mock.ts";
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { MISSING_PATH, SITES } from "./pages.ts";

const member = SITES[0]!;
const LOGIN = TEXT["login:login.button.idle"];
const LOGGING_IN = TEXT["login:login.button.pending"];
const EXPIRED = TEXT["errors:not_authenticated"];
const NOTICE_TITLE = TEXT["notice:notice.page.title"];

test.describe("login page", () => {
  test.beforeEach(({ apiMock }) => {
    apiMock.member = null;
  });

  test("shows the brand, the subtitle and one button, with no nav bar", async ({ page }) => {
    await page.goto(`${member.baseURL}/login`);
    await expect(page.locator("h1")).toHaveCount(1);
    await expect(page.locator("h1")).toHaveText(
      `${TEXT["common:brand.name.research"]} ${TEXT["common:brand.name.radar"]}`,
    );
    await expect(page.getByText(TEXT["login:login.page.subtitle"], { exact: true })).toBeVisible();
    await expect(page.getByRole("button")).toHaveText([LOGIN]);
    await expect(page.locator("header, nav, footer")).toHaveCount(0);
    await expect(page.locator("[data-login-note]")).toHaveCount(0);
  });

  test("loading: the button says logging in and is disabled until the answer comes", async ({ page, apiMock }) => {
    const [loginGate, release] = gate();
    apiMock.loginGate = loginGate;
    await page.goto(`${member.baseURL}/login`);
    const button = page.getByRole("button");
    await button.click();
    await expect(button).toHaveText(LOGGING_IN);
    await expect(button).toHaveAttribute("aria-disabled", "true");
    await expect(button).toHaveAttribute("aria-busy", "true");
    // 停用中再按不會多送一次。
    await button.click({ force: true });
    release();
    await expect(page).toHaveURL(`${member.baseURL}/notice`);
    expect(apiMock.requests.filter((request) => request.method === "POST")).toHaveLength(1);
  });

  test("success goes to the notice page with the nav bar", async ({ page, apiMock }) => {
    await page.goto(`${member.baseURL}/login`);
    await page.getByRole("button", { name: LOGIN }).click();
    await expect(page).toHaveURL(`${member.baseURL}/notice`);
    await expect(page.locator("h1")).toHaveText(NOTICE_TITLE);
    await expect(page.locator("header")).toBeVisible();
    // 沒指定成員就用假登入清單的第一位；沒有 next。
    const post = apiMock.requests.find((request) => request.method === "POST");
    expect(post?.body).toEqual({ member_id: MEMBERS[0]!.member_id, next: null });
  });

  test("error: one line above the button, and the button can be pressed again", async ({ page, apiMock }) => {
    apiMock.login = "failed";
    await page.goto(`${member.baseURL}/login`);
    await page.getByRole("button", { name: LOGIN }).click();
    const note = page.getByRole("alert");
    await expect(note).toHaveText(TEXT["login:login.note.failed"]);
    await expect(note).toHaveAttribute("data-login-note", "failed");
    const button = page.getByRole("button", { name: LOGIN });
    await expect(button).not.toHaveAttribute("aria-disabled", "true");
    // 訊息行在按鈕上方。
    const noteBox = await note.boundingBox();
    const buttonBox = await button.boundingBox();
    expect(noteBox!.y + noteBox!.height).toBeLessThanOrEqual(buttonBox!.y);

    apiMock.login = "ok";
    await button.click();
    await expect(page).toHaveURL(`${member.baseURL}/notice`);
  });

  test("not on the list: an explanation line, and the button stays", async ({ page, apiMock }) => {
    apiMock.login = "denied";
    await page.goto(`${member.baseURL}/login`);
    await page.getByRole("button", { name: LOGIN }).click();
    const note = page.getByRole("status");
    await expect(note).toHaveText(TEXT["login:login.note.denied"]);
    await expect(note).toHaveAttribute("data-login-note", "denied");
    await expect(page.getByRole("button", { name: LOGIN })).toBeEnabled();
    await expect(page).toHaveURL(`${member.baseURL}/login`);
  });

  test("the member parameter picks the test member", async ({ page, apiMock }) => {
    const second = MEMBERS[1]!;
    await page.goto(`${member.baseURL}/login?member=${second.member_id}`);
    await page.getByRole("button", { name: LOGIN }).click();
    await expect(page).toHaveURL(`${member.baseURL}/notice`);
    const post = apiMock.requests.find((request) => request.method === "POST");
    expect(post?.body).toEqual({ member_id: second.member_id, next: null });
  });

  test("trial mode sends no member: the backend decides who logs in", async ({ page, apiMock }) => {
    apiMock.mode = "access_admin";
    await page.goto(`${member.baseURL}/login?member=${MEMBERS[1]!.member_id}`);
    await page.getByRole("button", { name: LOGIN }).click();
    await expect(page).toHaveURL(`${member.baseURL}/notice`);
    const post = apiMock.requests.find((request) => request.method === "POST");
    expect(post?.body).toEqual({ next: null });
  });
});

test.describe("signing in is required", () => {
  test("opening a page without signing in goes to login, then back to that page", async ({ page, apiMock }) => {
    apiMock.member = null;
    await page.goto(`${member.baseURL}/notice?from=test`);
    await expect(page).toHaveURL(`${member.baseURL}/login?next=${encodeURIComponent("/notice?from=test")}`);
    // 直接打開網址不是「登入過期」。
    await expect(page.getByText(EXPIRED, { exact: true })).toHaveCount(0);
    await expect(page.locator("[data-login-note]")).toHaveCount(0);

    await page.getByRole("button", { name: LOGIN }).click();
    await expect(page).toHaveURL(`${member.baseURL}/notice?from=test`);
    const post = apiMock.requests.find((request) => request.method === "POST");
    expect(post?.body).toEqual({ member_id: MEMBERS[0]!.member_id, next: "/notice?from=test" });
  });

  test("without a page to return to, login goes to the notice page", async ({ page, apiMock }) => {
    apiMock.member = null;
    await page.goto(`${member.baseURL}/`);
    await expect(page).toHaveURL(new RegExp(`/login\\?next=`));
    await page.getByRole("button", { name: LOGIN }).click();
    await expect(page).toHaveURL(`${member.baseURL}/notice`);
  });

  test("next pointing outside the site is ignored", async ({ page, apiMock }) => {
    apiMock.member = null;
    for (const next of ["//evil.example/x", "https://evil.example/", "/login", "/Login?x=1"]) {
      apiMock.requests.length = 0;
      await page.goto(`${member.baseURL}/login?next=${encodeURIComponent(next)}`);
      await page.getByRole("button", { name: LOGIN }).click();
      await expect(page).toHaveURL(`${member.baseURL}/notice`);
      // 前端自己就不送（替身也會擋，所以要看送出去的內容）。
      const post = apiMock.requests.find((request) => request.method === "POST");
      expect(post?.body, next).toEqual({ member_id: MEMBERS[0]!.member_id, next: null });
      apiMock.member = null;
    }
  });

  test("expired: back to login with one line, then back to the page after signing in", async ({ page, apiMock }) => {
    await page.goto(`${member.baseURL}/notice`);
    await expect(page.locator("h1")).toHaveText(NOTICE_TITLE);

    // 在站內時登入過期：下一次換頁（問「我是誰」得到 401）回到登入頁。
    apiMock.member = null;
    await page.getByRole("link", { name: /Research/ }).first().click();
    await expect(page).toHaveURL(/\/login\?/);
    const url = new URL(page.url());
    expect(url.searchParams.get("next")).toBe("/notice");
    expect(url.searchParams.get("expired")).toBe("true");
    const note = page.getByRole("status");
    await expect(note).toHaveText(EXPIRED);
    await expect(note).toHaveAttribute("data-login-note", "expired");
    await expect(page.locator("header")).toHaveCount(0);

    // 按下登入後訊息就消失（登入中…時已經沒有）。
    const [loginGate, release] = gate();
    apiMock.loginGate = loginGate;
    await page.getByRole("button", { name: LOGIN }).click();
    await expect(page.getByRole("button", { name: LOGGING_IN })).toBeVisible();
    await expect(page.locator("[data-login-note]")).toHaveCount(0);
    release();
    await expect(page).toHaveURL(`${member.baseURL}/notice`);
    await expect(page.locator("header")).toBeVisible();
  });

  test("an unknown address shows not found without asking to sign in", async ({ page, apiMock }) => {
    apiMock.member = null;
    await page.goto(`${member.baseURL}${MISSING_PATH}`);
    await expect(page.getByText(TEXT["common:states.notFound.message"], { exact: true })).toBeVisible();
    await expect(page).toHaveURL(`${member.baseURL}${MISSING_PATH}`);
    await expect(page.locator("header")).toHaveCount(0);
  });

  test("when who-am-i cannot be answered, the shared load-failed state shows", async ({ page, apiMock }) => {
    apiMock.meFails = true;
    await page.goto(`${member.baseURL}/notice`);
    await expect(page.getByText(TEXT["common:states.loadFailed.message"], { exact: true })).toBeVisible();
    await expect(page.locator("header")).toHaveCount(0);
  });
});
