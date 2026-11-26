// 無障礙底線（UX-DR7、Story 3.2）：對 A11Y_SITES 的每一頁檢查
// (1) Tab 走過每個可以聚焦的元素，每一個都是 1px 實線金色外框、外擴 4px；
// (2) 看得到的按鈕與連結至少 44×44；
// (3) 720×450（1440 寬放大 200%）沒有橫向捲動、文字元素互不重疊；
// (4) 第一層標題剛好一個；
// (5) 論文標題與摘要元素自己有 lang，且和內容的語言一致（英文是 en，中文是 zh 開頭）；
// (6) 開發站另跑 axe（正式建置由 3.1 的 a11y.spec.ts 跑），每個說明框點開後也跑一次。
// 頁面清單和站內連結檢查同一份（pages.ts），新頁面加進來就自動被檢查。
// 「找不到這一頁」（MISSING_PATH）也跑 (1)～(4)：Story 3.8 起「找不到這一頁」這句就是 h1（EXPERIENCE.md
// State Patterns「全站共用」）；(5)、(6) 只跑真的頁面。
import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures.ts";
import { A11Y_SITES, MISSING_PATH } from "./pages.ts";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
/** accent-gold（#e3b466）的計算值。 */
const GOLD = "rgb(227, 180, 102)";
const TAP_TARGET = 44;
/** 次像素的誤差。 */
const EPSILON = 0.5;
/** Tab 最多按幾次（防止無限迴圈；頁面上的可聚焦元素不會超過這個數）。 */
const MAX_TABS = 200;

/** 可以操作的元素（Tab 走訪與 44×44 用同一組）。 */
const INTERACTIVE = "a[href], button, [role=button], summary, input, select, textarea";

/** 可以用 Tab 走到的元素：看得到、沒停用、tabindex 不是負的。每個標上編號，回傳數量。 */
async function markTabbables(page: Page): Promise<number> {
  return page.evaluate((selector) => {
    const elements = [...document.querySelectorAll<HTMLElement>(`${selector}, [tabindex]`)].filter((element) => {
      if (element.matches(":disabled") || element.tabIndex < 0) return false;
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && getComputedStyle(element).visibility !== "hidden";
    });
    elements.forEach((element, index) => element.setAttribute("data-tabbable", String(index)));
    return elements.length;
  }, INTERACTIVE);
}

interface FocusWalk {
  /** 這一頁應該走得到的元素數。 */
  expectedCount: number;
  /** 實際走到的元素（data-tabbable 編號）。 */
  visited: string[];
  /** 外框不對的元素與原因；空的才算通過。 */
  problems: string[];
}

/** 一個聚焦中的元素的外框是否符合規定（:focus-visible、1px 實線金色、外擴 4px）；符合回 null。 */
function ringProblem(focused: { id: string; focusVisible: boolean; outline: string; offset: string }): string | null {
  const expected = `solid 1px ${GOLD}`;
  if (!focused.focusVisible || focused.outline !== expected || focused.offset !== "4px") {
    return `${focused.id}: focus-visible=${focused.focusVisible} outline=${focused.outline} offset=${focused.offset}`;
  }
  return null;
}

/** 用鍵盤 Tab 走完一圈，記下每個走到的元素與外框問題。 */
async function walkFocus(page: Page, browserName: string): Promise<FocusWalk> {
  const expectedCount = await markTabbables(page);
  // Safari 預設 Tab 不會走到連結，要用 Option+Tab。
  const key = browserName === "webkit" ? "Alt+Tab" : "Tab";
  const visited: string[] = [];
  const problems: string[] = [];
  for (let presses = 0; presses < MAX_TABS; presses += 1) {
    await page.keyboard.press(key);
    const focused = await page.evaluate(() => {
      const element = document.activeElement as HTMLElement | null;
      if (element === null || element === document.body) return null;
      const style = getComputedStyle(element);
      return {
        id: element.getAttribute("data-tabbable") ?? element.outerHTML.slice(0, 80),
        focusVisible: element.matches(":focus-visible"),
        outline: `${style.outlineStyle} ${style.outlineWidth} ${style.outlineColor}`,
        offset: style.outlineOffset,
      };
    });
    if (focused === null) {
      // 焦點離開頁面（到網址列）：走完一圈，或這一頁沒有可以聚焦的元素。
      if (visited.length > 0 || expectedCount === 0) break;
      continue;
    }
    if (visited.includes(focused.id)) {
      break; // 繞回第一個元素：走完一圈。
    }
    visited.push(focused.id);
    const problem = ringProblem(focused);
    if (problem !== null) problems.push(problem);
  }
  return { expectedCount, visited, problems };
}

/**
 * 每個論文標題與摘要（[data-paper-text]）的 lang 是否和內容的語言一致；回傳不一致的（空的才算通過）。
 * lang 要寫在元素自己身上（不能靠 <html lang> 繼承）。內容有中日韓文字的是中文論文，lang 要是 zh 開頭；
 * 其他是英文（追蹤的期刊都是英文期刊），lang 要剛好是 en。
 */
async function paperLangProblems(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const cjk = /[\u3400-\u9fff\uf900-\ufaff]/;
    return [...document.querySelectorAll<HTMLElement>("[data-paper-text]")].flatMap((element) => {
      const text = (element.textContent ?? "").trim();
      const lang = element.getAttribute("lang") ?? "";
      const ok = cjk.test(text) ? /^zh(?:-|$)/i.test(lang) : lang === "en";
      return ok ? [] : [`${text.slice(0, 30)}: lang="${lang}"`];
    });
  });
}

/**
 * 這一頁的內容出現了：一般頁面等 h1，「找不到這一頁」等訊息條；再等載入中的骨架（[data-state='loading']）都換掉。
 * h1 比資料先出來：只等 h1 時，焦點走訪會在骨架還在的時候先標號，資料列晚幾十毫秒才補上，
 * 走到沒有編號的元素就對不起來；44×44 與 200% 放大也會量到比較少的元素（epic-3j 查到的）。
 */
async function waitForPage(page: Page, missing: boolean): Promise<void> {
  await expect(page.locator(missing ? "main [role='status']" : "main h1").first()).toBeVisible();
  await expect(page.locator("[data-state='loading']")).toHaveCount(0);
}

for (const site of A11Y_SITES) {
  // 「找不到這一頁」也跑焦點外框、44×44、200% 放大、h1；lang、axe 只跑真的頁面。
  const targets = [
    ...site.pages.map((path) => ({ path, missing: false })),
    { path: MISSING_PATH, missing: true },
  ];
  for (const { path, missing } of targets) {
    const url = `${site.baseURL}${path}`;

    test(`${site.name} ${path}: every focusable element shows the gold focus ring`, async ({ page, browserName }) => {
      await page.goto(url);
      await waitForPage(page, missing);
      const walk = await walkFocus(page, browserName);
      expect(walk.problems).toEqual([]);
      expect([...walk.visited].sort()).toEqual(
        Array.from({ length: walk.expectedCount }, (_, index) => String(index)).sort(),
      );
    });

    test(`${site.name} ${path}: visible buttons and links are at least 44×44`, async ({ page }) => {
      await page.goto(url);
      await waitForPage(page, missing);
      const small = await page.evaluate(
        ({ selector, min }) => {
          return [...document.querySelectorAll<HTMLElement>(selector)]
            .filter((element) => getComputedStyle(element).visibility !== "hidden")
            .map((element) => ({ element, rect: element.getBoundingClientRect() }))
            .filter(({ rect }) => rect.width > 0 && rect.height > 0)
            .filter(({ rect }) => rect.width < min || rect.height < min)
            .map(({ element, rect }) => `${element.outerHTML.slice(0, 80)} ${rect.width}×${rect.height}`);
        },
        { selector: INTERACTIVE, min: TAP_TARGET - EPSILON },
      );
      expect(small).toEqual([]);
    });

    test(`${site.name} ${path}: 200% zoom (720×450) has no horizontal scroll and no overlapping text`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: 720, height: 450 });
      await page.goto(url);
      await waitForPage(page, missing);
      await page.evaluate(() => document.fonts.ready);
      const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
      expect(scrollWidth).toBeLessThanOrEqual(720);

      const overlaps = await page.evaluate((epsilon) => {
        // 只放寬一個地方：論文頁 899px 以下底部的固定列（「開啟全文」，Story 3.8）照設計稿疊在捲過去的內容上面，
        // 列裡的字和列外的字疊在一起不算版面重疊；列裡的字彼此照樣比。其他 sticky／fixed 的元素不放寬。
        // 固定列有沒有蓋住鍵盤焦點，由 paper.spec.ts 的焦點測試看（Opus 畫面審查低 11、中 2）。
        const pinned = (element: HTMLElement) => element.closest("[data-full-text='bar']") !== null;
        // 直接含有文字的元素（不含隱藏的）。
        const elements = [...document.querySelectorAll<HTMLElement>("body *")].filter((element) => {
          const hasText = [...element.childNodes].some(
            (node) => node.nodeType === Node.TEXT_NODE && (node.textContent ?? "").trim() !== "",
          );
          if (!hasText) return false;
          const rect = element.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0 && getComputedStyle(element).visibility !== "hidden";
        });
        const found: string[] = [];
        for (let i = 0; i < elements.length; i += 1) {
          for (let j = i + 1; j < elements.length; j += 1) {
            const a = elements[i]!;
            const b = elements[j]!;
            if (a.contains(b) || b.contains(a)) continue;
            if (pinned(a) !== pinned(b)) continue;
            const ra = a.getBoundingClientRect();
            const rb = b.getBoundingClientRect();
            const width = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
            const height = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
            if (width > epsilon && height > epsilon) {
              found.push(`${a.textContent?.slice(0, 30)} × ${b.textContent?.slice(0, 30)}`);
            }
          }
        }
        return found;
      }, 1);
      expect(overlaps).toEqual([]);
    });

    test(`${site.name} ${path}: exactly one h1`, async ({ page }) => {
      await page.goto(url);
      await expect(page.locator("main")).toBeVisible();
      await expect(page.locator("h1")).toHaveCount(1);
    });

    if (missing) continue;

    test(`${site.name} ${path}: paper titles and abstracts carry the lang of their text`, async ({ page }) => {
      await page.goto(url);
      await expect(page.locator("main h1")).toBeVisible();
      expect(await paperLangProblems(page)).toEqual([]);
    });

    if (site.name === "dev") {
      test(`${site.name} ${path}: no axe violations`, async ({ page }) => {
        await page.goto(url);
        await expect(page.locator("main h1")).toBeVisible();
        const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
        expect(results.violations.map((violation) => `${violation.id}: ${violation.help}`)).toEqual([]);
      });

      test(`${site.name} ${path}: no axe violations with each popover open`, async ({ page }) => {
        await page.goto(url);
        await expect(page.locator("main h1")).toBeVisible();
        const triggers = page.locator("[data-info-popover-trigger]");
        const popup = page.locator("[data-info-popover]");
        const count = await triggers.count();
        expect(count).toBeGreaterThan(0);
        for (let index = 0; index < count; index += 1) {
          await triggers.nth(index).click();
          await expect(popup).toBeVisible();
          // 等淡入（0.2 秒）結束：Base UI 拿掉 data-starting-style、透明度到 1 才檢查（半透明時 axe 會把文字的對比算低）。
          await expect(popup).not.toHaveAttribute("data-starting-style");
          await expect(popup).toHaveCSS("opacity", "1");
          // 不檢查 Base UI 的焦點守衛：它是 tabindex=0＋aria-hidden 的 span（data-base-ui-focus-guard），
          // 焦點一進來就由 React 代收的 focus 事件轉走，axe 看不到這段處理，會誤判成 aria-hidden-focus。
          // 之前能過是因為 axe 把 role=dialog 當成模態而判成「無法判定」，不是真的沒問題。
          const results = await new AxeBuilder({ page }).withTags(TAGS).exclude("[data-base-ui-focus-guard]").analyze();
          expect(
            results.violations.map(
              (violation) =>
                `#${index} ${violation.id}: ${violation.help} ${violation.nodes.map((node) => node.html).join(" | ")}`,
            ),
          ).toEqual([]);
          await page.keyboard.press("Escape");
          await expect(popup).toHaveCount(0);
        }
      });
    }
  }
}

// 反例：證明上面的 Tab 走訪（同一個 walkFocus）真的抓得到沒有金色外框的元素，不是什麼都放過。
test("the focus check notices an element without the gold ring", async ({ page, browserName }) => {
  const site = A11Y_SITES.find((candidate) => candidate.name === "dev")!;
  await page.goto(`${site.baseURL}${site.pages[0]}`);
  await waitForPage(page, false);
  await page.addStyleTag({ content: "button:focus-visible { outline-color: red !important; }" });
  const walk = await walkFocus(page, browserName);
  expect(walk.visited.length).toBe(walk.expectedCount);
  expect(walk.problems.length).toBeGreaterThan(0);
  expect(walk.problems.every((problem) => problem.includes("rgb(255, 0, 0)"))).toBe(true);
});

// 反例：證明論文語言檢查（同一個 paperLangProblems）抓得到沒有 lang、空的 lang、和內容不一致的 lang，
// 而且中文論文標成 zh-Hant 不會被誤報。
test("the paper lang check notices a missing or mismatched lang", async ({ page }) => {
  const site = A11Y_SITES.find((candidate) => candidate.name === "dev")!;
  await page.goto(`${site.baseURL}${site.pages[0]}`);
  await waitForPage(page, false);
  expect(await paperLangProblems(page)).toEqual([]);
  await page.evaluate(() => {
    // 中文字用字碼產生（程式裡不寫死中文，AD-21）：「資訊系統」「資訊系統研究」。
    const zh = String.fromCodePoint(0x8cc7, 0x8a0a, 0x7cfb, 0x7d71);
    const add = (lang: string | null, text: string) => {
      const element = document.createElement("p");
      element.setAttribute("data-paper-text", "title");
      if (lang !== null) element.setAttribute("lang", lang);
      element.textContent = text;
      document.querySelector("main")!.append(element);
    };
    add(null, "Missing lang");
    add("", "Empty lang");
    add("en", zh);
    add("zh-Hant", "Wrong lang");
    add("zh-Hant", `${zh}${String.fromCodePoint(0x7814, 0x7a76)}`);
  });
  const problems = await paperLangProblems(page);
  expect(problems).toHaveLength(4);
  expect(problems.some((problem) => problem.startsWith("Missing lang"))).toBe(true);
  expect(problems.some((problem) => problem.startsWith("Empty lang"))).toBe(true);
  expect(problems.some((problem) => problem.startsWith("Wrong lang"))).toBe(true);
});
