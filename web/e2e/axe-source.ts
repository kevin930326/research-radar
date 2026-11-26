// 跑 axe 的測試共用（a11y.spec.ts、nav.spec.ts、a11y-floor.spec.ts）：firefox 用哪一份 axe 原始碼。
import type { Page } from "@playwright/test";
import axeCore from "axe-core";

// Firefox 159（Playwright 1.65 帶的版本）讀 ARIA 元素反射屬性（element.ariaLabelledByElements 這類）時，
// 內容行程會當掉（Bugzilla 2090055，Firefox 163 修好）；axe 4.14 處理 aria-labelledby 等參照時會讀它，
// CI 上的論文頁約四分之一的機率整頁當掉（frame.evaluate: Page crashed）。ADR-0010 的 2026-11-26 更新。
// 所以 firefox 改用「axe 原始碼＋把這 9 個參照類屬性的反射屬性名稱設成 false」：axe 改讀屬性字串、用 id 找元素，
// 這是它原本就有的路徑。（不用 null：axe.configure 合併設定時會把 null 變成空物件。）
// 規則、標籤、頁面都不變；我們的頁面只用屬性字串，不用 JS 設參照，所以結果一樣。
// axe-core 是 @axe-core/playwright 帶進來的同一份；升級它時要確認兩邊還是同一個版本。
// 升到 Firefox 163 以上的 Playwright 之後拿掉這段。
const IDREF_ATTRIBUTES = [
  "aria-actions",
  "aria-activedescendant",
  "aria-controls",
  "aria-describedby",
  "aria-details",
  "aria-errormessage",
  "aria-flowto",
  "aria-labelledby",
  "aria-owns",
];
const AXE_WITHOUT_ELEMENT_REFLECTION = `${axeCore.source}
;axe.configure({ standards: { ariaAttrs: ${JSON.stringify(Object.fromEntries(IDREF_ATTRIBUTES.map((name) => [name, { prop: false }])))} } });`;

/** 給 AxeBuilder 的 axeSource：firefox 用上面那一份，其他引擎回 undefined（用 @axe-core/playwright 內建的）。 */
export function axeSourceFor(page: Page): string | undefined {
  return page.context().browser()?.browserType().name() === "firefox" ? AXE_WITHOUT_ELEMENT_REFLECTION : undefined;
}
