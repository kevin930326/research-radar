// 站內連結檢查的「落點」判斷（AR-67）：links.spec.ts 與它的反例共用同一套檢查。
// 只看畫面上沒有「找不到這一頁」不夠：真的 HTTP 404（例如 /api/ 底下回的 404 JSON）畫面上也沒有那句話。
import type { Page, Response } from "@playwright/test";

import { TEXT } from "./expected.ts";
import { expect } from "./fixtures.ts";
import type { Site } from "./pages.ts";

const NOT_FOUND = TEXT["common:states.notFound.message"];

/** 記下這一頁主框架的每一個文件導航回應（換頁、重新載入、轉址的每一段）；呼叫端可清空重記。 */
export function recordDocumentResponses(page: Page): Response[] {
  const responses: Response[] = [];
  page.on("response", (response) => {
    if (response.request().isNavigationRequest() && response.frame() === page.mainFrame()) {
      responses.push(response);
    }
  });
  return responses;
}

/**
 * 文件導航的回應都要成功：2xx；3xx 只可能是轉址的中間一段或 304（瀏覽器快取裡的 2xx 仍有效）。
 * 沒有文件導航（站內換頁）時清單是空的，直接通過。
 */
export function expectDocumentResponsesOk(responses: readonly Response[], label: string): void {
  for (const response of responses) {
    const status = response.status();
    expect(status >= 200 && status < 400, `${label}: HTTP ${status} ${response.url()}`).toBe(true);
  }
  const last = responses.at(-1);
  if (last !== undefined) {
    const status = last.status();
    expect(status < 300 || status === 304, `${label}: HTTP ${status} ${last.url()}`).toBe(true);
  }
}

/** 落點必須是這個站真的有的頁面：HTTP 成功、網址在頁面清單裡、畫面有 h1、沒有「找不到這一頁」。 */
export async function expectValidLanding(
  page: Page,
  site: Site,
  responses: readonly Response[],
  label: string,
): Promise<void> {
  expectDocumentResponsesOk(responses, label);
  // 站內轉址（例如 / 導到 /search）會先經過不在清單裡的網址，所以等網址落定，不是只看一次。
  await expect(page, `${label}: not a known page`).toHaveURL(
    (url) => url.origin === site.baseURL && site.pages.includes(url.pathname),
  );
  await expect(page.locator("main h1"), `${label}: no h1`).toBeVisible();
  await expect(page.getByText(NOT_FOUND, { exact: true }), `${label}: not found`).toHaveCount(0);
}
