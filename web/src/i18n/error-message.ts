// 後端錯誤回應（RFC 9457）→ 畫面上的字的 i18n 鍵（Conventions「錯誤」、ADR-0047）。
// - 後端只回代碼（`code`），畫面上的字一律由 i18n 的 errors.<code> 提供；`title` 是給機器看的英文
//   HTTP 片語，`detail` 之類的欄位就算出現也不用：不把後端的英文或內部細節直接顯示給使用者。
// - errors.json 只放 EXPERIENCE.md 逐字有的句子；沒有專屬句子的代碼、看不懂的回應（不是物件、沒有
//   code、未知代碼、網路錯誤）一律落到全站共用的「沒有載入成功」。
import type { ProblemDetailsCode } from "../generated/model";
import errors from "./zh-TW/errors.json";

/** 沒有專屬句子時用的通用句（State Patterns「載入失敗」）。 */
export const FALLBACK_ERROR_KEY = "common:states.loadFailed.message";

type ErrorsKey = keyof typeof errors;

// errors.json 有專屬句子的代碼。型別寫成 ProblemDetailsCode（由 openapi.json 產生）：errors.json 多了
// 一個後端不會回的鍵，這一行的型別檢查就失敗。
const DEDICATED_CODES: readonly ProblemDetailsCode[] = Object.keys(errors) as ErrorsKey[];
const DEDICATED: ReadonlySet<string> = new Set(DEDICATED_CODES);

export type ErrorMessageKey = `errors:${ErrorsKey}` | typeof FALLBACK_ERROR_KEY;

/** 把錯誤回應的本文（已經 JSON.parse，或根本不是 JSON）轉成 i18n 鍵。 */
export function errorMessageKey(body: unknown): ErrorMessageKey {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return FALLBACK_ERROR_KEY;
  }
  const code: unknown = (body as Record<string, unknown>).code;
  if (typeof code === "string" && DEDICATED.has(code)) {
    return `errors:${code as ErrorsKey}`;
  }
  return FALLBACK_ERROR_KEY;
}
