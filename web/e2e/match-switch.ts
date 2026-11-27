// 打開「任一個／同時符合」切換出現的狀態（Story 3.20）：a11y.spec.ts 與 a11y-floor.spec.ts 共用。
// 桌機點那一個篩選標籤打開下拉選單；手機點「篩選」打開滿版篩選、展開那一段。打開後等切換看得到、選單淡入結束。
import type { Locator, Page } from "@playwright/test";

import { TEXT } from "./expected.ts";
import { expect } from "./fixtures.ts";
import type { MatchSwitchState } from "./pages.ts";

const PHONE = { width: 390, height: 844 };
const DESKTOP = { width: 1280, height: 900 };

function label(field: MatchSwitchState["field"]): string {
  const value = TEXT[field === "topic" ? "search:search.filter.topic" : "paper:paper.classification.method"];
  if (value === undefined) throw new Error(`no expected text for ${field}`);
  return value;
}

/** 打開這個狀態，回傳切換那一組（看得到了）。 */
export async function openMatchSwitch(page: Page, baseURL: string, state: MatchSwitchState): Promise<Locator> {
  await page.setViewportSize(state.phone ? PHONE : DESKTOP);
  await page.goto(`${baseURL}${state.path}`);
  await expect(page.locator("main h1")).toBeVisible();
  await expect(page.locator("[data-state='loading']")).toHaveCount(0);
  if (state.phone) {
    await page.locator("[data-mobile-filter]").click();
    const sheet = page.getByRole("dialog");
    await sheet.locator(`[data-filter-section='${state.field}'] > button`).click();
    // 滑入動畫結束（Base UI 拿掉 data-starting-style）。
    await expect(sheet).not.toHaveAttribute("data-starting-style");
  } else {
    await page.locator("[data-filter-chip]").filter({ hasText: label(state.field) }).click();
  }
  const group = page.locator(`[data-match-switch='${state.field}']`);
  await expect(group).toBeVisible();
  // 選單淡入（0.2 秒）結束才檢查：半透明時 axe 會把文字的對比算低（同 a11y-floor.spec.ts 的說明框）。
  const popup = group.locator("xpath=ancestor::*[@data-open or @data-side][1]");
  if ((await popup.count()) > 0) await expect(popup).toHaveCSS("opacity", "1");
  // 切換自己的淡入（0.25 秒，設計稿 .mm 的 rrfade）也要結束：動畫跑完就從 getAnimations() 拿掉。
  await expect.poll(() => group.evaluate((element) => element.getAnimations().length)).toBe(0);
  return group;
}
