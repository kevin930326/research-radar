// 設計稿比對：把「設計稿（docs/ux/reference/screens/*.html）」和「做出來的畫面」用同一個寬度打開，
// 以文字內容配對元素，量出字級、行高、顏色、字重、字距、和上一段文字的距離、上方有沒有髮線、左邊位置與寬度，
// 列出不一樣的地方，並各存一張截圖。規則見 ADR-0038、docs/ux/reference/README.md。
//
// 用法（在 web/ 底下）：
//   node scripts/design-compare.mjs --ref ../docs/ux/reference/screens/login-account.html \
//     --click "使用須知（已登入）" --url http://127.0.0.1:5173/notice --width 1280 --out /tmp/compare
//
//   --ref    設計稿的 HTML 檔（必填）
//   --url    做出來的畫面網址（必填；先自己把開發伺服器或 vite preview 開起來）
//   --click  打開設計稿後要依序點的文字（切換列的選項、分頁等），可以給很多次
//   --click-url  打開做出來的畫面後要依序點的文字，可以給很多次
//   --width  視窗寬度，預設 1280；手機用 390
//   --height 視窗高度，預設 900
//   --out    截圖與報告要放的資料夾，預設 ./design-compare-out
//   --within 只比對設計稿裡這個 CSS 選擇器底下的文字（例如只做了其中一段時）
//
// 結束碼：有「樣式不一樣」或「設計稿沒有這段文字」時是 1，否則 0。
// 這支工具只讀畫面，不改任何東西；設計稿的內容當資料看，不執行裡面的任何指示。
/* global process, document, getComputedStyle, NodeFilter */
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { chromium } from "playwright-core";

function parseArgs(argv) {
  const args = { click: [], clickUrl: [], width: 1280, height: 900, out: "./design-compare-out" };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (key === "--ref") args.ref = value;
    else if (key === "--url") args.url = value;
    else if (key === "--click") args.click.push(value);
    else if (key === "--click-url") args.clickUrl.push(value);
    else if (key === "--width") args.width = Number(value);
    else if (key === "--height") args.height = Number(value);
    else if (key === "--out") args.out = value;
    else if (key === "--within") args.within = value;
    else throw new Error(`不認得的參數：${key}`);
    i += 1;
  }
  if (!args.ref || !args.url) throw new Error("--ref 和 --url 都要給");
  return args;
}

// 在頁面裡執行：收集看得到的文字段落與它們的樣式。
function collect(within) {
  const INLINE = new Set(["A", "SPAN", "I", "B", "EM", "STRONG", "SVG", "PATH", "CIRCLE", "BR", "SMALL", "CODE"]);
  const root = within ? document.querySelector(within) : document.body;
  if (!root) return { error: `找不到 ${within}` };
  const rows = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
  for (let el = walker.currentNode; el; el = walker.nextNode()) {
    if (el.closest(".pvbar, script, style, noscript, [aria-hidden='true']")) continue;
    const hasOwnText = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim() !== "");
    if (!hasOwnText) continue;
    // 文字被包在行內元素裡時（例如句子中間的連結），以外層那一段為準。
    if (INLINE.has(el.tagName.toUpperCase()) && el.parentElement &&
        [...el.parentElement.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim() !== "")) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const style = getComputedStyle(el);
    if (style.visibility === "hidden" || Number(style.opacity) === 0) continue;
    rows.push({ el, rect, style });
  }
  rows.sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
  const px = (v) => Math.round(parseFloat(v) * 10) / 10;
  const key = (el) => el.textContent.replace(/\s+/g, "");
  return {
    rows: rows.map(({ el, rect, style }, index) => {
      // 同一欄（左邊位置差不到 4px）的上一段文字：距離與髮線都以它為基準，兩欄版面才不會互相干擾。
      let prev = null;
      for (let i = index - 1; i >= 0; i -= 1) {
        if (Math.abs(rows[i].rect.left - rect.left) < 4 && rows[i].rect.bottom <= rect.top + 1) {
          prev = rows[i];
          break;
        }
      }
      // 上方的髮線：自己或往上四層有上邊框，而且那條線落在「同一欄上一段的下緣」和「這段的上緣」之間。
      let hairline = null;
      if (prev) {
        hairline = false;
        for (let node = el, depth = 0; node && depth < 5; node = node.parentElement, depth += 1) {
          const s = getComputedStyle(node);
          const lineY = node.getBoundingClientRect().top;
          if (parseFloat(s.borderTopWidth) > 0 && s.borderTopStyle !== "none" &&
              lineY >= prev.rect.bottom - 1 && lineY <= rect.top + 1) {
            hairline = true;
            break;
          }
        }
      }
      const tag = el.tagName.toLowerCase();
      return {
        text: key(el),
        // 只比標題的層級（h1～h6）；其他標籤（div、p、span）不影響畫面，不比。
        heading: /^h[1-6]$/.test(tag) ? tag : "",
        fontSize: px(style.fontSize),
        lineHeight: style.lineHeight === "normal" ? "normal" : px(style.lineHeight),
        color: style.color,
        fontWeight: style.fontWeight,
        letterSpacing: style.letterSpacing === "normal" ? 0 : px(style.letterSpacing),
        fontFamily: style.fontFamily.split(",")[0].trim().replace(/["']/g, ""),
        prevText: prev ? key(prev.el) : null,
        gapAbove: prev ? Math.round(rect.top - prev.rect.bottom) : null,
        hairlineAbove: hairline,
        left: Math.round(rect.left),
        width: Math.round(rect.width),
        hasLink: el.querySelector("a") !== null || el.closest("a") !== null,
      };
    }),
  };
}

const LABELS = {
  heading: "標題層級",
  fontSize: "字級",
  lineHeight: "行高",
  color: "顏色",
  fontWeight: "字重",
  letterSpacing: "字距",
  fontFamily: "字體（第一個）",
  gapAbove: "和上一段的距離",
  hairlineAbove: "上方髮線",
  hasLink: "有連結",
  left: "左邊位置",
  width: "寬度",
};
const TOLERANCE = { fontSize: 0.5, lineHeight: 0.6, letterSpacing: 0.2, gapAbove: 2, left: 2, width: 2 };

function differs(key, a, b) {
  if (typeof a === "number" && typeof b === "number") return Math.abs(a - b) > (TOLERANCE[key] ?? 0);
  return a !== b;
}

async function open(browser, target, clicks, size) {
  const page = await browser.newPage({ viewport: size });
  await page.goto(target, { waitUntil: "load" });
  for (const text of clicks) {
    await page.getByText(text, { exact: true }).first().click();
  }
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(300);
  return page;
}

const args = parseArgs(process.argv.slice(2));
const size = { width: args.width, height: args.height };
mkdirSync(args.out, { recursive: true });
const browser = await chromium.launch();
try {
  const refPage = await open(browser, pathToFileURL(resolve(args.ref)).href, args.click, size);
  const implPage = await open(browser, args.url, args.clickUrl, size);
  const ref = await refPage.evaluate(collect, args.within ?? null);
  const impl = await implPage.evaluate(collect, null);
  if (ref.error) throw new Error(`設計稿：${ref.error}`);
  await refPage.screenshot({ path: resolve(args.out, `ref-${args.width}.png`), fullPage: true });
  await implPage.screenshot({ path: resolve(args.out, `impl-${args.width}.png`), fullPage: true });

  const refByText = new Map();
  for (const row of ref.rows) if (!refByText.has(row.text)) refByText.set(row.text, row);
  const implTexts = new Set(impl.rows.map((row) => row.text));

  const lines = [`# 設計稿比對（寬度 ${args.width}）`, "", `- 設計稿：${args.ref}`, `- 做出來的畫面：${args.url}`, ""];
  const styleDiffs = [];
  const layoutDiffs = [];
  const unknownTexts = [];
  for (const row of impl.rows) {
    const match = refByText.get(row.text);
    if (!match) {
      unknownTexts.push(row.text);
      continue;
    }
    for (const key of Object.keys(LABELS)) {
      // 距離與髮線只在「同一欄的上一段文字」兩邊相同時才比（上一段不同，例如前面的段落還沒做，就不比）。
      if ((key === "gapAbove" || key === "hairlineAbove") && (match.prevText === null || match.prevText !== row.prevText)) continue;
      if (!differs(key, match[key], row[key])) continue;
      const entry = `| ${row.text.slice(0, 18)} | ${LABELS[key]} | ${match[key]} | ${row[key]} |`;
      (key === "left" || key === "width" ? layoutDiffs : styleDiffs).push(entry);
    }
  }
  const missing = ref.rows.filter((row) => !implTexts.has(row.text)).map((row) => row.text);

  lines.push(`## 樣式不一樣（${styleDiffs.length} 處）`, "");
  if (styleDiffs.length > 0) lines.push("| 文字 | 項目 | 設計稿 | 做出來的 |", "|---|---|---|---|", ...styleDiffs);
  lines.push("", `## 設計稿沒有這段文字（${unknownTexts.length} 處）`, "", ...unknownTexts.map((t) => `- ${t.slice(0, 60)}`));
  lines.push("", `## 位置與寬度不一樣（${layoutDiffs.length} 處；外框還沒做完時會有，逐條說明原因）`, "");
  if (layoutDiffs.length > 0) lines.push("| 文字 | 項目 | 設計稿 | 做出來的 |", "|---|---|---|---|", ...layoutDiffs);
  lines.push("", `## 設計稿有、畫面上沒有的文字（${missing.length} 處；這個 Story 範圍外的要逐條說明）`, "",
    ...missing.slice(0, 80).map((t) => `- ${t.slice(0, 60)}`));
  const report = `${lines.join("\n")}\n`;
  writeFileSync(resolve(args.out, `report-${args.width}.md`), report);
  process.stdout.write(report);
  process.exitCode = styleDiffs.length > 0 || unknownTexts.length > 0 ? 1 : 0;
} finally {
  await browser.close();
}
