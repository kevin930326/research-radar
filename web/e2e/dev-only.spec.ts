// 元件示範頁只在開發環境（Story 3.2）：成員版與示範站的建置結果裡沒有 /dev/components 路由、
// 示範頁的元件與示範頁自己的文字（標題、區塊名稱）；兩種 preview 打開 /dev/components 都是「找不到這一頁」。
// 範例字（「開啟全文」等）取自 EXPERIENCE.md，之後正式畫面也會用，所以不檢查。
// 檢查的是 webServer 剛建好的 dist／dist-demo。
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import dev from "../src/i18n/zh-TW/dev.json" with { type: "json" };
import { TEXT } from "./expected.ts";
import { expect, test } from "./fixtures.ts";
import { SITES } from "./pages.ts";

const WEB_ROOT = fileURLToPath(new URL("../", import.meta.url));
const EXPERIENCE = readFileSync(
  fileURLToPath(
    new URL("../../_bmad-output/planning-artifacts/ux-designs/ux-research-radar-2026-11-12/EXPERIENCE.md", import.meta.url),
  ),
  "utf-8",
);

function strings(value: unknown): string[] {
  return typeof value === "string" ? [value] : Object.values(value as Record<string, unknown>).flatMap(strings);
}

/** 建置結果的每個檔名與 js／css／html 的內容。 */
function output(outDir: string): { names: string[]; text: string } {
  const assets = `${WEB_ROOT}${outDir}/assets/`;
  const names = readdirSync(assets);
  const text = [
    readFileSync(`${WEB_ROOT}${outDir}/index.html`, "utf-8"),
    ...names.filter((name) => /\.(js|css)$/.test(name)).map((name) => readFileSync(`${assets}${name}`, "utf-8")),
  ].join("\n");
  return { names, text };
}

/** 字串在建置結果裡可能是原字，也可能被跳脫成 \uXXXX。 */
function escaped(value: string): string {
  return JSON.stringify(value).slice(1, -1).replace(/[^\x20-\x7e]/g, (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`);
}

test("production builds contain no component showcase", ({ browserName }) => {
  test.skip(browserName !== "chromium", "build output is checked once");
  // 只查示範頁特有的字：正式 i18n 或 EXPERIENCE.md 裡也有的（之後的畫面會用）不算。
  const production = Object.values(TEXT);
  const devTexts = strings(dev).filter(
    (value) => !EXPERIENCE.includes(value) && !production.some((text) => text.includes(value)),
  );
  expect(devTexts).toContain(dev.dev.page.title);
  for (const outDir of ["dist", "dist-demo"]) {
    const { names, text } = output(outDir);
    // 檔名去掉 -<雜湊> 再比，免得雜湊剛好含 dev。
    const bare = names.map((name) => name.replace(/-[A-Za-z0-9_-]{8}(?=\.[a-z0-9]+$)/, ""));
    expect(bare.filter((name) => /dev|showcase/i.test(name)), outDir).toEqual([]);
    for (const needle of ["/dev/components", "DevComponentsPage", "dev-showcase", "data-dev-section"]) {
      expect(text.includes(needle), `${outDir}: ${needle}`).toBe(false);
    }
    for (const value of devTexts) {
      expect(text.includes(value) || text.includes(escaped(value)), `${outDir}: ${value}`).toBe(false);
    }
  }
});

for (const site of SITES) {
  test(`${site.name} preview: /dev/components is not found`, async ({ page }) => {
    await page.goto(`${site.baseURL}/dev/components`);
    await expect(page.getByText(TEXT["common:states.notFound.message"], { exact: true })).toBeVisible();
    await expect(page.locator("[data-dev-section]")).toHaveCount(0);
  });
}
