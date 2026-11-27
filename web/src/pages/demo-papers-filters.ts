// 示範站論文清單（Story 3.14 示範站；EXPERIENCE.md 畫面 7）的篩選條件：網址參數怎麼讀、怎麼寫，
// 以及在瀏覽器裡怎麼篩（AD-20：讀 papers-index.json 與 titles.json，條件怎麼合照 AD-19／ADR-0050 第 2 點）。
// 和成員版找論文（search-filters.ts）規則不同的地方不硬湊：多了方法、「只看有摘要的」「只看有完整內容的」、
// 標題裡的字；沒有範圍。期刊、年份、主題、方法、本週新增、目前顯示幾篇的網址寫法和找論文相同，直接用它的檢查。
// 只處理資料，畫面上的字由 i18n 提供。
import type { DemoPaperRow } from "../datasource/types";
import { normalizeForTitleMatch, titleMatcher } from "../datasource/title-match";
import type { DisplayNames } from "../generated/model";
import { anyMatch, currentTaipeiYear, knownOnly, selectedKeys, tidyMatch, validateSearchParams, type SearchParams } from "./search-filters";

/** 標題裡的字最多幾個字（網址不會無限長；超過的截掉）。 */
export const MAX_TITLE_QUERY = 200;

/** 示範站論文清單的網址參數（路由驗證過的）。省略的欄位是「沒有這個條件」。 */
export interface DemoPapersParams {
  /** 期刊代號，逗號分隔（多選，「或」）。 */
  journal?: string;
  /** 年份範圍的起訖（含）；兩個都有才算數。 */
  from?: number;
  to?: number;
  /** 主題代號，逗號分隔（多選；預設「或」，topic_match=all 時「且」）。 */
  topic?: string;
  /** 方法代號，逗號分隔（多選；預設「或」，method_match=all 時「且」）。 */
  method?: string;
  /** 主題、方法選了兩個以上時「同時符合」（Story 3.20；寫法同找論文，省略是任一個）。 */
  topic_match?: "all";
  method_match?: "all";
  /** 只看有摘要的。 */
  abstract?: true;
  /** 只看有完整內容的。 */
  full?: true;
  /** 從連結帶進來的條件「本週新增」（可以移除的標籤）。 */
  this_week?: true;
  /** 用標題裡的字篩選（字面比對）。 */
  title?: string;
  /** 目前顯示幾篇（按過「再載入」才有；省略是一頁）。 */
  shown?: number;
}

function flag(value: unknown): true | undefined {
  return value === true || value === "true" ? true : undefined;
}

/**
 * 路由的 validateSearch：只留看得懂的值（每個欄位都寫出來，網址上沒檢查過的值不會留下）。
 * 期刊、年份、主題、方法、本週新增、目前顯示幾篇用找論文的同一個檢查（年份要在追蹤起始年到今年之間、起不晚於訖）。
 */
export function validateDemoPapersParams(search: Record<string, unknown>, currentYear: number = currentTaipeiYear()): DemoPapersParams {
  const shared = validateSearchParams(search, currentYear);
  const rawTitle = typeof search.title === "string" ? search.title : typeof search.title === "number" ? String(search.title) : "";
  const title = rawTitle.slice(0, MAX_TITLE_QUERY);
  return {
    journal: shared.journal,
    from: shared.from,
    to: shared.to,
    topic: shared.topic,
    method: shared.method,
    topic_match: shared.topic_match,
    method_match: shared.method_match,
    abstract: flag(search.abstract),
    full: flag(search.full),
    this_week: shared.this_week,
    title: title.trim() === "" ? undefined : title,
    shown: shared.shown,
  };
}

/** 給找論文共用的篩選選項用（期刊、年份、主題、方法與它們的「同時符合」，欄位名稱與寫法相同）。 */
export function toSearchParams(params: DemoPapersParams): SearchParams {
  return {
    journal: params.journal,
    from: params.from,
    to: params.to,
    topic: params.topic,
    method: params.method,
    topic_match: params.topic_match,
    method_match: params.method_match,
  };
}

/** 共用的篩選選項改了期刊、年份、主題、方法：寫回示範站的條件（其他條件不動；不到兩個的「同時符合」拿掉）。 */
export function fromSearchParams(params: DemoPapersParams, next: SearchParams): DemoPapersParams {
  return tidyMatch({
    ...params,
    journal: next.journal,
    from: next.from,
    to: next.to,
    topic: next.topic,
    method: next.method,
    topic_match: next.topic_match,
    method_match: next.method_match,
  });
}

/** 拿掉顯示名稱裡沒有的期刊、主題、方法代號（主題另外認得「未分類」；同找論文）。 */
export function knownDemoOnly(params: DemoPapersParams, names: DisplayNames): DemoPapersParams {
  return fromSearchParams(params, knownOnly(toSearchParams(params), names));
}

/**
 * 有沒有可以「清除篩選」的條件：期刊、年份、主題、方法、兩個「只看…的」、本週新增。
 * 標題裡的字不算（它有自己的「清除文字」；設計稿只有標題的字時沒有「清除篩選」）。
 */
export function hasDemoConditions(params: DemoPapersParams): boolean {
  return (
    params.journal !== undefined ||
    params.from !== undefined ||
    params.topic !== undefined ||
    params.method !== undefined ||
    params.abstract === true ||
    params.full === true ||
    params.this_week === true
  );
}

/** 手機「篩選 · N」：滿版篩選裡有選的項目（期刊、年份、主題、方法、兩個「只看…的」各算一項；設計稿）。 */
export function demoFilterCount(params: DemoPapersParams): number {
  return [params.journal, params.from, params.topic, params.method, params.abstract, params.full].filter((value) => value !== undefined).length;
}

/** 「清除篩選」：拿掉標題的字以外的條件（目前顯示幾篇也回到一頁）。 */
export function clearDemoConditions(params: DemoPapersParams): DemoPapersParams {
  return { title: params.title };
}

/** 有沒有任何條件（含標題的字）：沒有就是「全部論文」。 */
export function isAllPapers(params: DemoPapersParams): boolean {
  return !hasDemoConditions(params) && params.title === undefined;
}

/**
 * 一篇論文的分類代號合不合：`all` 是全部這幾個都有（「且」），`any` 是有任一個（「或」）。
 * 沒有分類（null）的論文只在沒有這個條件時留下（同後端：沒有有效分類的論文不會出現在分類的片段裡）。
 */
function hasKeys(keys: readonly string[] | null, wanted: readonly string[], mode: "any" | "all"): boolean {
  if (wanted.length === 0) return true;
  if (keys === null) return false;
  return mode === "all" ? wanted.every((key) => keys.includes(key)) : wanted.some((key) => keys.includes(key));
}

// 每一篇正規化過的標題（第一次比對時算、之後重用）：標題框每打一個字都要全部重篩，不必每次都對每一篇重做 NFKC
// 與轉小寫（審查 3.14 P5）。列物件是讀檔時建好、之後不變的，所以用它當 key。
const normalizedTitles = new WeakMap<DemoPaperRow, string>();

function normalizedTitle(row: DemoPaperRow): string {
  let value = normalizedTitles.get(row);
  if (value === undefined) {
    value = normalizeForTitleMatch(row.title);
    normalizedTitles.set(row, value);
  }
  return value;
}

/** 依年份新到舊、同一年依 id 由大到小、沒有年份的放最後（成員版 ADR-0050 第 5 點：publication_year DESC NULLS LAST, id DESC）。 */
function byYearThenId(a: DemoPaperRow, b: DemoPaperRow): number {
  if (a.year !== b.year) {
    if (a.year === null) return 1;
    if (b.year === null) return -1;
    return b.year - a.year;
  }
  // 讀檔時 UUID 不分大小寫；轉小寫再比，順序才和 Postgres uuid 一樣。
  const left = a.id.toLowerCase();
  const right = b.id.toLowerCase();
  return left < right ? 1 : left > right ? -1 : 0;
}

/**
 * 在瀏覽器裡篩（AD-20）：不同欄位之間「且」；期刊、年份多值「或」（年份是一段範圍）；主題、方法多值預設「或」、
 * `*_match=all` 時「且」（ADR-0011 的 2026-11-27 更新、Story 3.20，和成員版的後端同一套）；
 * 標題的字用資料來源層的比對規則（title-match.ts）。
 * 結果的順序和成員版相同：年份新到舊、同一年 id 由大到小、沒有年份的放最後。
 */
export function filterDemoRows(rows: readonly DemoPaperRow[], params: DemoPapersParams): DemoPaperRow[] {
  const journals = selectedKeys(params.journal);
  const topics = selectedKeys(params.topic);
  const methods = selectedKeys(params.method);
  const tidy = tidyMatch(params);
  const topicMode = tidy.topic_match === "all" ? "all" : "any";
  const methodMode = tidy.method_match === "all" ? "all" : "any";
  const matchTitle = params.title === undefined ? null : titleMatcher(params.title);
  return rows
    .filter(
      (row) =>
        (journals.length === 0 || (row.journal !== null && journals.includes(row.journal))) &&
        (params.from === undefined ||
          params.to === undefined ||
          (row.year !== null && params.from <= row.year && row.year <= params.to)) &&
        hasKeys(row.topics, topics, topicMode) &&
        hasKeys(row.methods, methods, methodMode) &&
        (params.abstract !== true || row.hasAbstract) &&
        (params.full !== true || row.hasFullContent) &&
        (params.this_week !== true || row.isNewThisWeek) &&
        (matchTitle === null || matchTitle(normalizedTitle(row))),
    )
    .sort(byYearThenId);
}

/** 主題與方法都改回「任一個」、其他條件不變時的篇數（同時符合 0 篇時的「改看符合任一個的 N 篇」；在瀏覽器裡算）。 */
export function anyMatchCount(rows: readonly DemoPaperRow[], params: DemoPapersParams): number {
  return filterDemoRows(rows, anyMatch(params)).length;
}
