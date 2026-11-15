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
