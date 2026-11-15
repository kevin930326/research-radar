// 畫面文字和 EXPERIENCE.md 逐字相同（AR-69）：
// 1. i18n 的每一條都在摘出來的清單裡、值完全相同，清單裡的每一條也都在 i18n 裡（不多不少）。
// 2. 清單的每一條都逐字出現在 EXPERIENCE.md（清單不會和文件走樣）。
// 3. 使用須知第 1、2 段的字不在 i18n 裡（AR-67：Epic 5、6 才出現）。
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

const actual = Object.fromEntries(
  Object.entries(resources[LANGUAGE]).flatMap(([namespace, tree]) => flatten(namespace, tree)),
);

describe("UI text matches EXPERIENCE.md", () => {
  it("i18n values equal the extracted list, entry by entry", () => {
    expect(actual).toEqual(expected.strings);
  });

  it.each(Object.entries(expected.strings))("%s appears verbatim in EXPERIENCE.md", (_key, text) => {
    expect(experience).toContain(text);
  });

  it.each(expected.absent)("paragraphs 1 and 2 are not shown: %s", (text) => {
    expect(experience).toContain(text);
    for (const value of Object.values(actual)) {
      expect(value).not.toContain(text);
    }
  });

  it("keys follow page.block.item", () => {
    for (const key of Object.keys(actual)) {
      expect(key.split(":")[1]?.split(".")).toHaveLength(3);
    }
  });
});
