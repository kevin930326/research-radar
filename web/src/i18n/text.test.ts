// 畫面文字和 EXPERIENCE.md 逐字相同（AR-69）：
// 1. i18n 的每一條都在摘出來的清單裡、值完全相同，清單裡的每一條也都在 i18n 裡（不多不少）。
// 2. 清單的每一條都逐字出現在 EXPERIENCE.md（清單不會和文件走樣）。
// 3. 使用須知第 1、2 段的字不在 i18n 裡（AR-67：Epic 5、6 才出現）。
// 4. EXPERIENCE.md 沒有、只有設計稿（或 PRD 名詞表）才有的字放在 `otherSources`，各自寫出來源檔，
//    逐字出現在那份檔裡（Story 3.8；設計稿和 EXPERIENCE.md 不一樣時以設計稿為準，列給 Kevin）。
// 有佔位符（{{x}}）的句子：佔位符對到文件裡的一段不含空白的字（例如「被引用 N 次」的 N、「#12」的 12）。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import expected from "./expected/notice.zh-TW.json";
import { LANGUAGE, resources } from "./index";

const REPO_ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const experience = readFileSync(`${REPO_ROOT}${expected.source}`, "utf-8");

function flatten(namespace: string, value: unknown, prefix = ""): [string, string][] {
  if (typeof value === "string") {
    return [[`${namespace}:${prefix}`, value]];
  }
  return Object.entries(value as Record<string, unknown>).flatMap(([key, child]) =>
    flatten(namespace, child, prefix === "" ? key : `${prefix}.${key}`),
  );
}

/** 文件裡有沒有這一句；佔位符（{{x}}）對到一段不含空白的字。 */
function appearsIn(document: string, text: string): boolean {
  if (!/\{\{\w+\}\}/.test(text)) {
    return document.includes(text);
  }
  const pattern = text
    .split(/\{\{\w+\}\}/)
    .map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join("\\S+?");
  return new RegExp(pattern).test(document);
}

const otherSources: Record<string, { text: string; source: string }> = expected.otherSources;
const otherTexts = Object.fromEntries(Object.entries(otherSources).map(([key, entry]) => [key, entry.text]));

const actual = Object.fromEntries(
  Object.entries(resources[LANGUAGE]).flatMap(([namespace, tree]) => flatten(namespace, tree)),
);

describe("UI text matches EXPERIENCE.md", () => {
  it("i18n values equal the extracted list, entry by entry", () => {
    expect(actual).toEqual({ ...expected.strings, ...otherTexts });
  });

  it.each(Object.entries(expected.strings))("%s appears verbatim in EXPERIENCE.md", (_key, text) => {
    expect(appearsIn(experience, text), text).toBe(true);
  });

  it.each(Object.entries(otherSources))("%s appears verbatim in its source file", (key, { text, source }) => {
    expect(Object.keys(expected.strings)).not.toContain(key);
    expect(appearsIn(readFileSync(`${REPO_ROOT}${source}`, "utf-8"), text), `${text} in ${source}`).toBe(true);
  });

  it("placeholders only stand for a run of non-space text", () => {
    expect(appearsIn("cited 8 times · 8 per year", "cited {{citations}} times · {{perYear}} per year")).toBe(true);
    expect(appearsIn("cited 8 times", "cited {{citations}} times · {{perYear}} per year")).toBe(false);
    expect(appearsIn("as of 2026-11-12 · #12", "· #{{number}}")).toBe(true);
    expect(appearsIn("as of ", "as of {{date}}")).toBe(false);
  });

  it.each(expected.absent)("paragraphs 1 and 2 are not shown: %s", (text) => {
    expect(experience).toContain(text);
    for (const value of Object.values(actual)) {
      expect(value).not.toContain(text);
    }
  });

  it("keys follow page.block.item", () => {
    for (const key of Object.keys(actual)) {
      // 錯誤的鍵照 Conventions「錯誤」固定是 errors.<code>（後端的代碼），不是三段（Story 3.4）。
      if (key.startsWith("errors:")) {
        continue;
      }
      expect(key.split(":")[1]?.split(".")).toHaveLength(3);
    }
  });

  it("error keys are errors.<code> with a snake_case backend code", () => {
    for (const key of Object.keys(actual).filter((name) => name.startsWith("errors:"))) {
      expect(key).toMatch(/^errors:[a-z][a-z0-9_]*$/);
    }
  });
});
