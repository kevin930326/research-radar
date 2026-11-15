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
//   --expect-missing  一個文字檔，一行一段「設計稿有、這個 Story 還不做」的文字（寫開頭幾個字就可以，空白不算）。
//                     清單裡的缺少不算差異；清單裡這次沒用到的會另外列出來（那段已經做出來的，要從清單拿掉）。
//
// 結束碼：報告裡會擋的四段任何一段不是 0（樣式、多出的文字、位置與寬度、沒列在清單裡的缺少）就是 1，全部是 0 才是 0。
// 所以整頁空白、只做一半、兩欄做成一欄，都不會是 0。
// 這支工具只讀畫面，不改任何東西；設計稿的內容當資料看，不執行裡面的任何指示。
/* global process, document, getComputedStyle, NodeFilter */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
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
    else if (key === "--expect-missing") args.expectMissing = value;
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
  const hasOwnText = (el) => [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim() !== "");
  // 連同祖先一起乘起來的透明度；任何一層是 0 就當作看不到。
  const effectiveOpacity = (el) => {
    let value = 1;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) value *= Number(getComputedStyle(node).opacity);
    return Math.round(value * 100) / 100;
  };
  const rows = [];
  // 用 1～2px 高的色塊畫的線（設計稿有些髮線不是邊框）。
  const bars = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
  for (let el = walker.currentNode; el; el = walker.nextNode()) {
    if (el.closest(".pvbar, script, style, noscript")) continue;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (rect.height > 0 && rect.height <= 2 && rect.width >= 20 && !hasOwnText(el) &&
        style.backgroundColor !== "rgba(0, 0, 0, 0)" && style.backgroundColor !== "transparent") bars.push(rect);
    if (!hasOwnText(el)) continue;
    // 只給螢幕閱讀器的文字（縮成 1×1）不算畫面上的文字。
    if (rect.width <= 1 || rect.height <= 1) continue;
    if (style.visibility === "hidden") continue;
    const opacity = effectiveOpacity(el);
    if (opacity === 0) continue;
    // 句子中間的行內元素（連結、強調）：外層那一段照常比，行內這一段另外只比樣式。
    const inline = INLINE.has(el.tagName.toUpperCase()) && el.parentElement !== null && hasOwnText(el.parentElement);
    rows.push({ el, rect, style, opacity, inline });
  }
  rows.sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
  const px = (v) => Math.round(parseFloat(v) * 10) / 10;
  const key = (el) => el.textContent.replace(/\s+/g, "");
  const hasLine = (node, side) => {
    const s = getComputedStyle(node);
    return parseFloat(s[`border${side}Width`]) > 0 && s[`border${side}Style`] !== "none";
  };
  return {
    rows: rows.map(({ el, rect, style, opacity, inline }, index) => {
      // 同一欄（左邊位置差不到 4px）的上一段文字：距離與髮線都以它為基準，兩欄版面才不會互相干擾。
      let prev = null;
      if (!inline) {
        for (let i = index - 1; i >= 0; i -= 1) {
          if (!rows[i].inline && Math.abs(rows[i].rect.left - rect.left) < 4 && rows[i].rect.bottom <= rect.top + 1) {
            prev = rows[i];
            break;
          }
        }
      }
      // 上方的髮線：落在「同一欄上一段的下緣」和「這段的上緣」之間的橫線。三種畫法都認：
      // 自己或往上四層的上邊框、上一段或它往上四層的下邊框、1～2px 高的色塊。
      let hairline = null;
      if (prev) {
        const between = (y) => y >= prev.rect.bottom - 1 && y <= rect.top + 1;
        hairline = false;
        for (let node = el, depth = 0; node && depth < 5 && !hairline; node = node.parentElement, depth += 1) {
          if (hasLine(node, "Top") && between(node.getBoundingClientRect().top)) hairline = true;
        }
        for (let node = prev.el, depth = 0; node && depth < 5 && !hairline; node = node.parentElement, depth += 1) {
          if (hasLine(node, "Bottom") && between(node.getBoundingClientRect().bottom)) hairline = true;
        }
        if (!hairline) {
          hairline = bars.some((bar) => between(bar.top) && bar.left < rect.right && bar.right > rect.left);
        }
      }
      const tag = el.tagName.toLowerCase();
      const fontSize = parseFloat(style.fontSize);
      const lineHeightPx = style.lineHeight === "normal" ? null : parseFloat(style.lineHeight);
      return {
        // 行內的那一段前面加記號，才不會和整段文字相同的區塊配在一起。
        text: (inline ? "↳" : "") + key(el),
        inline,
        heading: /^h[1-6]$/.test(tag) ? tag : "",
        fontSize: px(style.fontSize),
        lineHeight: lineHeightPx === null ? "normal" : px(style.lineHeight),
        // 行高、字距換算成字級的倍數再比：小字時差一格只有零點幾 px，用 px 比會分不出來。
        lineHeightRatio: lineHeightPx === null ? null : lineHeightPx / fontSize,
        letterSpacing: style.letterSpacing === "normal" ? 0 : px(style.letterSpacing),
        letterSpacingEm: style.letterSpacing === "normal" ? 0 : parseFloat(style.letterSpacing) / fontSize,
        color: style.color,
        opacity,
        fontWeight: style.fontWeight,
        fontStyle: style.fontStyle,
        textTransform: style.textTransform,
        textDecoration: style.textDecorationLine,
        fontFamily: style.fontFamily.split(",")[0].trim().replace(/["']/g, ""),
        // 佔幾行（高度除以行高）；行內元素和行高是 normal 的不算。
        lines: inline || lineHeightPx === null ? null : Math.max(1, Math.round(rect.height / lineHeightPx)),
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
  opacity: "透明度",
  fontWeight: "字重",
  fontStyle: "斜體",
  textTransform: "大小寫",
  textDecoration: "底線",
  letterSpacing: "字距",
  fontFamily: "字體（第一個）",
  lines: "佔幾行",
  gapAbove: "和上一段的距離",
  hairlineAbove: "上方髮線",
  hasLink: "有連結",
  left: "左邊位置",
  width: "寬度",
};
// 行內元素只比這幾項（位置、距離、行數對它沒有意義）。
const INLINE_KEYS = new Set(["fontSize", "color", "opacity", "fontWeight", "fontStyle", "textTransform", "textDecoration", "letterSpacing", "fontFamily"]);
const TOLERANCE = { fontSize: 0.5, opacity: 0.02, gapAbove: 2, left: 2, width: 2 };

function differs(key, ref, impl) {
  const a = ref[key];
  const b = impl[key];
  // 設計稿是用 div 畫的示意頁：只有設計稿自己標了標題層級時才比，畫面為了無障礙多標的 h2 不算差異。
  if (key === "heading") return a !== "" && a !== b;
  if (key === "lineHeight") {
    if (ref.lineHeightRatio === null || impl.lineHeightRatio === null) return a !== b;
    return Math.abs(ref.lineHeightRatio - impl.lineHeightRatio) > 0.02;
  }
  if (key === "letterSpacing") return Math.abs(ref.letterSpacingEm - impl.letterSpacingEm) > 0.004;
  if (typeof a === "number" && typeof b === "number") return Math.abs(a - b) > (TOLERANCE[key] ?? 0);
  return a !== b;
}

async function open(browser, target, clicks, size) {
  const page = await browser.newPage({ viewport: size });
  // 等到網路安靜下來：延遲載入的路由、要等資料的畫面才不會只量到一半。
  await page.goto(target, { waitUntil: "networkidle" });
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

  // 同一段文字出現很多次時（導覽列的「使用須知」和頁面標題），照出現順序第 n 個配第 n 個。
  const refByText = new Map();
  for (const row of ref.rows) refByText.set(row.text, [...(refByText.get(row.text) ?? []), row]);
  const seen = new Map();

  const lines = [`# 設計稿比對（寬度 ${args.width}）`, "", `- 設計稿：${args.ref}`, `- 做出來的畫面：${args.url}`, ""];
  const styleDiffs = [];
  const layoutDiffs = [];
  const unknownTexts = [];
  const skipped = [];
  for (const row of impl.rows) {
    const nth = seen.get(row.text) ?? 0;
    seen.set(row.text, nth + 1);
    const match = refByText.get(row.text)?.[nth];
    if (!match) {
      unknownTexts.push(row.text);
      continue;
    }
    for (const key of Object.keys(LABELS)) {
      if (row.inline && !INLINE_KEYS.has(key)) continue;
      // 距離與髮線只在「同一欄的上一段文字」兩邊相同時才比；上一段不同（前面的段落還沒做、或版面跑掉）就記下來，不默默略過。
      if (key === "gapAbove" || key === "hairlineAbove") {
        if (match.prevText === null && row.prevText === null) continue;
        if (match.prevText !== row.prevText) {
          if (key === "gapAbove") skipped.push(`- ${row.text.slice(0, 30)}（設計稿的上一段：${(match.prevText ?? "沒有").slice(0, 20)}；畫面的上一段：${(row.prevText ?? "沒有").slice(0, 20)}）`);
          continue;
        }
      }
      if (!differs(key, match, row)) continue;
      const entry = `| ${row.text.slice(0, 18)} | ${LABELS[key]} | ${match[key]} | ${row[key]} |`;
      (key === "left" || key === "width" ? layoutDiffs : styleDiffs).push(entry);
    }
  }
  // 設計稿有、畫面上沒有（或份數比較少）的文字。
  const missing = [];
  for (const [text, list] of refByText) {
    for (let i = seen.get(text) ?? 0; i < list.length; i += 1) missing.push(text);
  }
  const expected = args.expectMissing
    ? readFileSync(resolve(args.expectMissing), "utf-8").split("\n").map((line) => line.replace(/\s+/g, "")).filter((line) => line !== "" && !line.startsWith("#"))
    : [];
  const isExpected = (text) => expected.some((prefix) => text.startsWith(prefix));
  const unexpectedMissing = missing.filter((text) => !isExpected(text));
  const expectedMissing = missing.filter(isExpected);
  // 清單裡有、但已經沒有任何缺少的文字對得上：清單過期了（那段已經做出來，或文字寫錯）。
  const staleExpected = expected.filter((prefix) => !missing.some((text) => text.startsWith(prefix)));

  lines.push(`## 樣式不一樣（${styleDiffs.length} 處）`, "");
  if (styleDiffs.length > 0) lines.push("| 文字 | 項目 | 設計稿 | 做出來的 |", "|---|---|---|---|", ...styleDiffs);
  lines.push("", `## 設計稿沒有這段文字（${unknownTexts.length} 處）`, "", ...unknownTexts.map((t) => `- ${t.slice(0, 60)}`));
  lines.push("", `## 位置與寬度不一樣（${layoutDiffs.length} 處）`, "");
  if (layoutDiffs.length > 0) lines.push("| 文字 | 項目 | 設計稿 | 做出來的 |", "|---|---|---|---|", ...layoutDiffs);
  lines.push("", `## 設計稿有、畫面上沒有、也沒列在清單裡的文字（${unexpectedMissing.length} 處）`, "",
    ...unexpectedMissing.slice(0, 120).map((t) => `- ${t.slice(0, 60)}`));
  lines.push("", "---", "", "以下不影響結束碼，但要看過：", "",
    `## 清單內的缺少（${expectedMissing.length} 處；PR 說明要寫各屬於哪個 Story）`, "",
    ...expectedMissing.slice(0, 120).map((t) => `- ${t.slice(0, 60)}`));
  lines.push("", `## 距離與髮線沒比到（${skipped.length} 處；上一段文字兩邊不同）`, "", ...skipped);
  lines.push("", `## 清單裡這次沒用到的（${staleExpected.length} 處；手機版設計稿本來就沒有的不用管，已經做出來的要從清單拿掉）`, "",
    ...staleExpected.map((t) => `- ${t.slice(0, 60)}`));
  lines.push("", `配到 ${impl.rows.length - unknownTexts.length} 段，設計稿共 ${ref.rows.length} 段。`);
  const report = `${lines.join("\n")}\n`;
  writeFileSync(resolve(args.out, `report-${args.width}.md`), report);
  process.stdout.write(report);
  const total = styleDiffs.length + unknownTexts.length + layoutDiffs.length + unexpectedMissing.length;
  process.exitCode = total > 0 ? 1 : 0;
} finally {
  await browser.close();
}
