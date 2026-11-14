// 證明 eslint.config.js 的專案規則真的擋得住：對 lint-fixtures/ 的故意違反檔跑 ESLint，
// 每一種違規都要回報錯誤；照規則寫的檔案不能被誤擋。
import { fileURLToPath } from "node:url";

import { ESLint } from "eslint";
import { describe, expect, it } from "vitest";

const WEB_ROOT = fileURLToPath(new URL("..", import.meta.url));

async function ruleIds(fixture: string): Promise<string[]> {
  // ignore: false：lint-fixtures/ 在正式設定裡是忽略的，這裡要刻意檢查它。
  const eslint = new ESLint({ cwd: WEB_ROOT, ignore: false });
  const path = fixture.startsWith("e2e/") ? `lint-fixtures/${fixture}` : `lint-fixtures/src/${fixture}`;
  // 確認檔案真的落在設定的範圍裡（沒對到任何 files 的話 ESLint 不會檢查，測試就失去意義）。
  expect(await eslint.calculateConfigForFile(path)).toBeDefined();
  const [result] = await eslint.lintFiles([path]);
  if (result === undefined) {
    throw new Error(`no lint result for ${fixture}`);
  }
  expect(result.fatalErrorCount).toBe(0);
  return result.messages.filter((message) => message.severity === 2).map((message) => message.ruleId ?? "");
}

describe("project lint rules", () => {
  it("blocks fetch in a page", async () => {
    expect(await ruleIds("pages/FetchPage.tsx")).toContain("no-restricted-globals");
  });

  it("blocks window.fetch in a page", async () => {
    expect(await ruleIds("pages/WindowFetchPage.tsx")).toContain("no-restricted-properties");
  });

  it("blocks importing datasource/api from a page", async () => {
    expect(await ruleIds("pages/ImportApiPage.tsx")).toContain("no-restricted-imports");
  });

  it("blocks importing datasource/static from a component", async () => {
    expect(await ruleIds("components/ImportStatic.tsx")).toContain("no-restricted-imports");
  });

  it("blocks hard-coded Chinese JSX text", async () => {
    const ids = await ruleIds("components/HardcodedText.tsx");
    expect(ids).toContain("i18next/no-literal-string");
    expect(ids).toContain("no-restricted-syntax");
  });

  it("blocks hard-coded Chinese in an attribute", async () => {
    expect(await ruleIds("pages/HardcodedAttribute.tsx")).toContain("no-restricted-syntax");
  });

  it("blocks globalThis.fetch in a component", async () => {
    expect(await ruleIds("components/GlobalThisFetch.tsx")).toContain("no-restricted-properties");
  });

  it("blocks fetch in a route loader", async () => {
    expect(await ruleIds("routes/loader-fetch.ts")).toContain("no-restricted-globals");
  });

  it("blocks importing #datasource-impl from a page", async () => {
    expect(await ruleIds("pages/ImportImplPage.tsx")).toContain("no-restricted-imports");
  });

  it("blocks Chinese in a template literal", async () => {
    expect(await ruleIds("components/TemplateLiteral.tsx")).toContain("no-restricted-syntax");
  });

  it("blocks Chinese in an e2e file", async () => {
    expect(await ruleIds("e2e/hardcoded.spec.ts")).toContain("no-restricted-syntax");
  });

  it("lets the datasource layer itself fetch", async () => {
    expect(await ruleIds("datasource/api/allowed-fetch.ts")).toEqual([]);
  });

  it("allows the datasource entry and types", async () => {
    expect(await ruleIds("pages/Allowed.tsx")).toEqual([]);
  });
});
