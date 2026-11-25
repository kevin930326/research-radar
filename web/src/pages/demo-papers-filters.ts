// 示範站論文清單（Story 3.14 示範站；EXPERIENCE.md 畫面 7）的篩選條件：網址參數怎麼讀、怎麼寫，
// 以及在瀏覽器裡怎麼篩（AD-20：讀 papers-index.json 與 titles.json，條件怎麼合照 AD-19／ADR-0050 第 2 點）。
// 和成員版找論文（search-filters.ts）規則不同的地方不硬湊：多了方法、「只看有摘要的」「只看有完整內容的」、
// 標題裡的字；沒有範圍。期刊、年份、主題、方法、本週新增、目前顯示幾篇的網址寫法和找論文相同，直接用它的檢查。
// 只處理資料，畫面上的字由 i18n 提供。
import type { DemoPaperRow } from "../datasource/types";
import { titleMatches } from "../datasource/title-match";
import type { DisplayNames } from "../generated/model";
import { currentTaipeiYear, knownOnly, selectedKeys, validateSearchParams, type SearchParams } from "./search-filters";

/** 標題裡的字最多幾個字（網址不會無限長；超過的截掉）。 */
export const MAX_TITLE_QUERY = 200;

/** 示範站論文清單的網址參數（路由驗證過的）。省略的欄位是「沒有這個條件」。 */
export interface DemoPapersParams {
  /** 期刊代號，逗號分隔（多選，「或」）。 */
  journal?: string;
  /** 年份範圍的起訖（含）；兩個都有才算數。 */
  from?: number;
  to?: number;
  /** 主題代號，逗號分隔（多選，「且」）。 */
  topic?: string;
  /** 方法代號，逗號分隔（多選，「且」）。 */
  method?: string;
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
    abstract: flag(search.abstract),
    full: flag(search.full),
    this_week: shared.this_week,
    title: title.trim() === "" ? undefined : title,
    shown: shared.shown,
  };
}

/** 給找論文共用的篩選選項用（期刊、年份、主題、方法的欄位名稱與寫法相同）。 */
export function toSearchParams(params: DemoPapersParams): SearchParams {
  return { journal: params.journal, from: params.from, to: params.to, topic: params.topic, method: params.method };
}

/** 共用的篩選選項改了期刊、年份、主題、方法：寫回示範站的條件（其他條件不動）。 */
export function fromSearchParams(params: DemoPapersParams, next: SearchParams): DemoPapersParams {
  return { ...params, journal: next.journal, from: next.from, to: next.to, topic: next.topic, method: next.method };
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

/** 一篇論文的分類代號裡有沒有全部這幾個（「且」）。 */
function hasAll(keys: readonly string[] | null, wanted: readonly string[]): boolean {
  if (wanted.length === 0) return true;
  if (keys === null) return false;
  return wanted.every((key) => keys.includes(key));
}

/**
 * 在瀏覽器裡篩（AD-20）：不同欄位之間「且」；期刊、年份多值「或」（年份是一段範圍）；主題、方法多值「且」
 * （AD-19、ADR-0050 第 2 點，和成員版同一套）；標題的字用資料來源層的比對規則。
 * 結果依年份排序（新到舊；同一年照匯出的列順序；沒有年份的放最後）。
 */
export function filterDemoRows(rows: readonly DemoPaperRow[], params: DemoPapersParams): DemoPaperRow[] {
  const journals = selectedKeys(params.journal);
  const topics = selectedKeys(params.topic);
  const methods = selectedKeys(params.method);
  const matched = rows.filter(
    (row) =>
      (journals.length === 0 || (row.journal !== null && journals.includes(row.journal))) &&
      (params.from === undefined ||
        params.to === undefined ||
        (row.year !== null && params.from <= row.year && row.year <= params.to)) &&
      hasAll(row.topics, topics) &&
      hasAll(row.methods, methods) &&
      (params.abstract !== true || row.hasAbstract) &&
      (params.full !== true || row.hasFullContent) &&
      (params.this_week !== true || row.isNewThisWeek) &&
      (params.title === undefined || titleMatches(row.title, params.title)),
  );
  return matched
    .map((row, order) => ({ row, order }))
    .sort((a, b) => (b.row.year ?? Number.NEGATIVE_INFINITY) - (a.row.year ?? Number.NEGATIVE_INFINITY) || a.order - b.order)
    .map(({ row }) => row);
}
