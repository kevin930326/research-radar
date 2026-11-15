// e2e 用的固定文字與安全標頭：都從唯一來源讀，不在測試裡另寫一份。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import expected from "../src/i18n/expected/notice.zh-TW.json" with { type: "json" };

export const TEXT = expected.strings;
export const ABSENT = expected.absent;

const HEADERS_PATH = fileURLToPath(new URL("../../radar/entrypoints/api/security_headers.json", import.meta.url));

export const SECURITY_HEADERS = JSON.parse(readFileSync(HEADERS_PATH, "utf-8")) as Record<string, string>;

/** 設計稿有、EXPERIENCE.md 還沒有的文字（等 Kevin 決定後移到 strings）。 */
export const DESIGN_ONLY: Record<string, string> = Object.fromEntries(
  Object.entries(expected.designOnly).map(([key, value]) => [key, value.text]),
);
