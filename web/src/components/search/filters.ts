// 找論文的篩選：桌機的下拉選單與手機的滿版篩選共用的資料（選項清單、目前選了什麼的字）。
// 選了立刻套用（EXPERIENCE.md 畫面 12 第 2 點）：每一次改動都經 apply 寫進網址，查詢跟著網址走。
import type { TFunction } from "i18next";

import type { DisplayNames } from "../../generated/model";
import {
  AIS_CORE_JOURNALS,
  TRACKING_START_YEAR,
  joinKeys,
  presetRange,
  selectedKeys,
  yearPreset,
  type SearchParams,
} from "../../pages/search-filters";
import { formatYears } from "./parts";

/** 主題「未分類」的代號（後端的保留字，ADR-0050 第 1 點）。 */
export const UNCLASSIFIED = "unclassified";

export interface FilterState {
  params: SearchParams;
  /** 期刊與主題的顯示名稱；還沒讀到是 null（選單沒有選項）。 */
  names: DisplayNames | null;
  /** 今年（台北）：「今年」「近 N 年」與年份上限。 */
  currentYear: number;
  /** 套用新的條件（寫進網址；「目前顯示幾篇」回到一頁）。 */
  apply: (next: SearchParams) => void;
}

/** 勾一個或取消一個（期刊、主題的複選）。 */
export function toggleKey(value: string | undefined, key: string): string | undefined {
  const keys = selectedKeys(value);
  return joinKeys(keys.includes(key) ? keys.filter((item) => item !== key) : [...keys, key]);
}

/** 「AIS 核心 11 本」：只勾這 11 本（設計稿的快速選項是「換成這 11 本」，不是再加上）。顯示名稱裡沒有的不送。 */
export function aisCoreJournals(names: DisplayNames | null): string | undefined {
  const known = new Set((names?.journals ?? []).map((entry) => entry.key));
  return joinKeys(AIS_CORE_JOURNALS.filter((key) => known.has(key)));
}

/** 期刊的顯示名稱（全名）；沒讀到或不認得就用代號。 */
export function journalName(names: DisplayNames | null, key: string): string {
  return names?.journals.find((entry) => entry.key === key)?.name ?? key;
}

/** 主題的顯示名稱；「未分類」與不認得的由呼叫端處理。 */
export function topicName(names: DisplayNames | null, key: string, unclassified: string): string {
  if (key === UNCLASSIFIED) return unclassified;
  return names?.topics.find((entry) => entry.key === key)?.name ?? key;
}

/** 期刊選單或標籤上目前選了什麼：一本寫全名，多本寫「N 本」，沒選是 null。 */
export function journalValue(state: FilterState, t: TFunction<"search">): string | null {
  const keys = selectedKeys(state.params.journal);
  if (keys.length === 0) return null;
  if (keys.length === 1) return journalName(state.names, keys[0]!);
  return t("search.filter.journalCount", { count: keys.length });
}

/** 主題：一個寫名稱，多個寫「N 個」。 */
export function topicValue(state: FilterState, t: TFunction<"search">, unclassified: string): string | null {
  const keys = selectedKeys(state.params.topic);
  if (keys.length === 0) return null;
  if (keys.length === 1) return topicName(state.names, keys[0]!, unclassified);
  return t("search.filter.topicCount", { count: keys.length });
}

/** 年份：選的範圍（「2020–2026」，同一年寫一次）。 */
export function yearValue(state: FilterState): string | null {
  const { from, to } = state.params;
  return from === undefined || to === undefined ? null : formatYears(from, to);
}

/** 年份選單的四個快速選項與它們的範圍（「不限」是追蹤起始年到今年）。 */
export function yearOptions(currentYear: number): { preset: "none" | "thisYear" | "last3" | "last5"; from: number; to: number }[] {
  return [
    { preset: "none", from: TRACKING_START_YEAR, to: currentYear },
    ...(["thisYear", "last3", "last5"] as const).map((preset) => ({ preset, ...presetRange(preset, currentYear)! })),
  ];
}

export { yearPreset };
