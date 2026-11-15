// (1) 文件回應的安全標頭等於 security_headers.json；字體從同源載入、沒有 CSP 違規（fixtures.ts 自動檢查）。
import { MISSING_PATH, SITES } from "./pages.ts";
import { SECURITY_HEADERS } from "./expected.ts";
import { expect, test } from "./fixtures.ts";

for (const site of SITES) {
  for (const path of ["/", ...site.pages, MISSING_PATH]) {
    test(`${site.name} ${path}: document carries the site-wide security headers`, async ({ page }) => {
      const response = await page.goto(`${site.baseURL}${path}`);
      expect(response).not.toBeNull();
      const headers = response?.headers() ?? {};
      for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
        expect(headers[name.toLowerCase()], name).toBe(value);
      }
      await expect(page.locator("main")).toBeVisible();
    });
  }
}

test("member: fonts are bundled and load from the same origin", async ({ page }) => {
  const site = SITES[0]!;
  const requests: string[] = [];
  page.on("request", (request) => requests.push(request.url()));
  const fontResponses: { url: string; status: number }[] = [];
  page.on("response", (response) => {
    if (/\.woff2?$/.test(new URL(response.url()).pathname)) {
      fontResponses.push({ url: response.url(), status: response.status() });
    }
  });

  await page.goto(`${site.baseURL}/notice`);
  await expect(page.locator("h1")).toBeVisible();
  // 這一頁還沒用到這兩套字體，主動要瀏覽器載入，確認 CSP（font-src 'self'）下載得到。
  const loaded = await page.evaluate(async () => {
    const faces = await Promise.all([
      document.fonts.load('16px "Instrument Serif"'),
      document.fonts.load('16px "DM Mono"'),
    ]);
    return faces.map((list) => list.length);
  });

  expect(loaded.every((count) => count > 0)).toBe(true);
  expect(fontResponses.length).toBeGreaterThan(0);
  for (const font of fontResponses) {
    expect(font.url.startsWith(`${site.baseURL}/assets/`)).toBe(true);
    expect(font.status).toBe(200);
  }
  for (const url of requests) {
    expect(new URL(url).origin).toBe(site.baseURL);
  }
});
