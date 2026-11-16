// e2e 用的固定文字與安全標頭：都從唯一來源讀，不在測試裡另寫一份。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import expected from "../src/i18n/expected/notice.zh-TW.json" with { type: "json" };

// EXPERIENCE.md 的字，加上只有設計稿（或 PRD 名詞表）才有的字（Story 3.8 的 otherSources）。
const OTHER_TEXT: Record<string, string> = Object.fromEntries(
  Object.entries(expected.otherSources).map(([key, entry]) => [key, entry.text]),
);
export const TEXT: typeof expected.strings & Record<string, string> = { ...expected.strings, ...OTHER_TEXT };
export const ABSENT = expected.absent;

const HEADERS_PATH = fileURLToPath(new URL("../../radar/entrypoints/api/security_headers.json", import.meta.url));

export const SECURITY_HEADERS = JSON.parse(readFileSync(HEADERS_PATH, "utf-8")) as Record<string, string>;
