// (8) 示範站的建置結果不含 API 程式：成員版帶著 /api/v1（資料來源 api 實作的基底常數），
// 示範站一個 /api/ 都沒有。檢查的是 webServer 剛建好的 dist／dist-demo。
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

const WEB_ROOT = fileURLToPath(new URL("../", import.meta.url));

function bundleText(outDir: string): string {
  const assets = `${WEB_ROOT}${outDir}/assets/`;
  return readdirSync(assets)
    .filter((name) => /\.(js|css)$/.test(name))
    .map((name) => readFileSync(`${assets}${name}`, "utf-8"))
    .join("\n");
}

function jsText(outDir: string): string {
  const assets = `${WEB_ROOT}${outDir}/assets/`;
  return readdirSync(assets)
    .filter((name) => name.endsWith(".js"))
    .map((name) => readFileSync(`${assets}${name}`, "utf-8"))
    .join("\n");
}

function cssText(outDir: string): string {
  const assets = `${WEB_ROOT}${outDir}/assets/`;
  return readdirSync(assets)
    .filter((name) => name.endsWith(".css"))
    .map((name) => readFileSync(`${assets}${name}`, "utf-8"))
    .join("\n");
}

test("assets are never inlined as data: URLs (assetsInlineLimit: 0)", ({ browserName }) => {
  test.skip(browserName !== "chromium", "build output is checked once");
  for (const outDir of ["dist", "dist-demo"]) {
    const css = cssText(outDir);
    expect(css, outDir).toContain("@font-face");
    expect(css.replace(/\s+/g, ""), outDir).not.toMatch(/url\(["']?data:/);
  }
});

test("demo build contains no API code", ({ browserName }) => {
  test.skip(browserName !== "chromium", "build output is checked once");
  expect(bundleText("dist")).toContain("/api/v1");
  const demo = bundleText("dist-demo");
  expect(demo).not.toContain("/api/v1");
  expect(demo).not.toContain("/api/");
});

// Story 3.14 示範站：示範站的建置結果沒有成員版才有的功能（登入、我的文獻庫、加入文獻庫的寫入、成員身分的標頭），
// 也沒有本機看畫面用的範例匯出（fixtures/demo-preview 只掛在伺服器上，不進 dist-demo）與狀態切換。
// 每個記號先確認成員版真的有，才證明「示範站沒有」不是因為記號寫錯。
test("demo build has no member features and ships no preview fixtures", ({ browserName }) => {
  test.skip(browserName !== "chromium", "build output is checked once");
  // 只看程式（.js）：樣式表是兩站共用的同一套。比對結果用 true／false，失敗時不印出整包程式。
  const member = jsText("dist");
  const demo = jsText("dist-demo");
  for (const marker of ["/api/v1", "X-Radar-Expected-Member", "data-library", "fake-login", "/library", "/login"]) {
    expect(member.includes(marker), `member has ${marker}`).toBe(true);
    expect(demo.includes(marker), `demo has ${marker}`).toBe(false);
  }
  // 示範站讀 preview/ 的程式在、成員版沒有；切換狀態的網址只在伺服器上。
  expect(demo.includes("/preview/"), "demo reads preview/").toBe(true);
  expect(member.includes("/preview/"), "member reads preview/").toBe(false);
  expect(demo.includes("__demo-preview"), "demo has the state switch").toBe(false);
  const files = readdirSync(`${WEB_ROOT}dist-demo`, { recursive: true }).map(String);
  expect(files.filter((name) => name.startsWith("preview") || name.endsWith(".json"))).toEqual([]);
});

// Story 3.9：示範站的論文頁和成員版共用版面與區塊，但成員版的論文頁才有的「我的文獻庫」、成員版的資料來源不進示範站的建置。
// 示範站論文頁的記號（data-demo-paper、只有書目的說明）只在示範站；成員版論文頁的我的文獻庫（data-library）只在成員版。
// 上面那條已經擋 /api/、data-library；這一條另外證明論文頁的程式真的在示範站的建置裡（不是因為頁面沒進建置才找不到）。
test("demo build has the demo paper page but not the member paper page's library", ({ browserName }) => {
  test.skip(browserName !== "chromium", "build output is checked once");
  const member = jsText("dist");
  const demo = jsText("dist-demo");
  for (const marker of ["data-demo-paper", "biblio-only", "/papers/$paperId"]) {
    expect(demo.includes(marker), `demo has ${marker}`).toBe(true);
  }
  for (const marker of ["data-demo-paper", "biblio-only"]) {
    expect(member.includes(marker), `member has ${marker}`).toBe(false);
  }
  // 共用的論文頁區塊在兩邊都有；成員版論文頁的我的文獻庫（輔助欄、手機底部的加入）只在成員版。
  for (const marker of ["paper-zh-summary", "paper-research-model"]) {
    expect(member.includes(marker), `member has ${marker}`).toBe(true);
    expect(demo.includes(marker), `demo has ${marker}`).toBe(true);
  }
  for (const marker of ["add-mobile", "paper-library"]) {
    expect(member.includes(marker), `member has ${marker}`).toBe(true);
    expect(demo.includes(marker), `demo has ${marker}`).toBe(false);
  }
});

// Story 3.11：修正分類（分類那一行的「修正分類」、人數按鈕、歷史、表單；修正歷史的端點）只在成員版。
// 介面文字在兩站共用的 i18n 裡，所以看的是元件的記號與端點路徑，不是字。
test("demo build has no classification fix", ({ browserName }) => {
  test.skip(browserName !== "chromium", "build output is checked once");
  const member = jsText("dist");
  const demo = jsText("dist-demo");
  for (const marker of ["data-classification-fix", "data-fix-form", "classification-fixes"]) {
    expect(member.includes(marker), `member has ${marker}`).toBe(true);
    expect(demo.includes(marker), `demo has ${marker}`).toBe(false);
  }
});
