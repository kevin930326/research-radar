// 選單、滿版篩選因為視窗跨過寬度而關上時，焦點已經移到別處就不能被拉走（epic-3l C 審查 X7；同 epic-3j 6-2 的一般關閉）。
// nav.spec.ts（帳號選單、手機選單）與 search.spec.ts（手機滿版篩選）共用。
import type { Locator, Page } from "@playwright/test";

import { expect } from "./fixtures.ts";

/**
 * 把視窗改成 `size`，讓打開著的選單跨寬度關上；在選單「已經關上、還沒拆掉、還沒交焦點」的那一刻（`trigger` 的
 * aria-expanded 不再是 true 時——Base UI 的 Dialog 關上後拿掉這個屬性，Menu 改成 false——同一段腳本裡）把焦點移到
 * 頁面上（選單外）第一個符合 `selector` 的元素。
 * 不是固定等幾秒：用 MutationObserver 接在關上的那一次畫面更新後面。斷言那一刻選單還在、焦點真的移過去了，
 * 這條測試才有測到「交焦點時焦點已經在別處」。
 */
export async function crossWidthAndFocus(page: Page, trigger: Locator, size: { width: number; height: number }, selector: string): Promise<void> {
  await trigger.evaluate((button, target) => {
    const state = window as unknown as { __movedOnClose?: boolean };
    delete state.__movedOnClose;
    const observer = new MutationObserver(() => {
      if (button.getAttribute("aria-expanded") === "true") return;
      observer.disconnect();
      const outside = [...document.querySelectorAll<HTMLElement>(target)].find((element) => element.closest("[role=dialog], [role=menu]") === null)!;
      outside.focus();
      state.__movedOnClose = document.querySelector("[role=dialog], [role=menu]") !== null && document.activeElement === outside;
    });
    observer.observe(button, { attributes: true, attributeFilter: ["aria-expanded"] });
  }, selector);
  await page.setViewportSize(size);
  await expect.poll(() => page.evaluate(() => (window as unknown as { __movedOnClose?: boolean }).__movedOnClose)).toBe(true);
}
