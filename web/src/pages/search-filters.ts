// 找論文（Story 3.14）的篩選條件：網址參數怎麼讀、怎麼寫、怎麼換成 API 的參數（AD-21：篩選與目前顯示幾篇
// 都寫進網址，cursor 不進網址）。只處理資料，畫面上的字由 i18n 提供。
// 條件怎麼合（期刊、年份「或」，主題「且」）由後端決定（ADR-0050 第 2 點），前端只把選的值原樣送出去。
import type { DisplayNames, ListPapersParams, PaperListScope } from "../generated/model";

/**
 * 追蹤範圍的起始年（年份輸入的下限、「不限」右邊的年份範圍）。
 * 後端設定 config/journals.yaml 的 start_year 是同一個數；之後改起始年要兩邊一起改（ADR-0050 的 2026-11-20 更新）。
 */
export const TRACKING_START_YEAR = 2016;

/**
 * 期刊選單的快速選項「AIS 核心 11 本」：AIS Senior Scholars' Basket（Kevin 2026-11-20 確認的 11 本）。
 * 值是追蹤期刊設定（config/journals.yaml）的期刊代號，和 display-names 的 key 相同。
 */
export const AIS_CORE_JOURNALS: readonly string[] = [
  "misq",
  "isr",
  "jmis",
  "jais",
  "ejis",
  "isj",
  "jit",
  "jsis",
  "dss",
  "im",
  "io",
];

/** 「還沒有任何條件」時列幾篇本週新增（設計稿 search-no-box.html「還沒篩選」是 3 篇）。 */
export const START_ROWS = 3;

/**
 * 快取裡沒有可以接著讀的清單時（重新整理、直接打開網址），一次最多讀回幾篇：網址的「目前顯示幾篇」
 * 被改成很大的數也不會連打幾百次 API。只限制這一次，之後照樣可以「再載入」（審查 S1-3）。
 */
export const MAX_SHOWN = 200;

/** 主題「未分類」的代號（後端的保留字，ADR-0050 第 1 點）。 */
export const UNCLASSIFIED_TOPIC = "unclassified";

const SCOPES: readonly PaperListScope[] = ["tracked", "lab", "personal"];
/** 期刊、主題代號：英數字、底線、連字號（後端的代號都是這種寫法；其他字元不放進網址也不送出去）。 */
const KEY = /^[a-z0-9_-]{1,64}$/;
/** 網址上一個欄位最多幾個值（後端是 50，ADR-0050 第 10 點）。 */
const MAX_VALUES = 50;

/** 找論文的網址參數（路由驗證過的）。省略的欄位是「沒有這個條件」。 */
export interface SearchParams {
  /** 範圍：省略是所有追蹤期刊。 */
  scope?: Exclude<PaperListScope, "tracked">;
  /** 期刊代號，逗號分隔（多選）。 */
  journal?: string;
  /** 年份範圍的起訖（含）；兩個都有才算數。 */
  from?: number;
  to?: number;
  /** 主題代號，逗號分隔（多選）。 */
  topic?: string;
  /**
   * 方法代號，逗號分隔：只從論文頁的方法標籤帶進來（找論文沒有方法的篩選選單），
   * 顯示成可以移除的條件標籤（EXPERIENCE.md 畫面 8 第 2 點）。
   */
  method?: string;
  /** 從「看全部 N 篇」帶進來的條件「本週新增」。 */
  this_week?: true;
  /** 目前顯示幾篇（按過「再載入」才有；省略是一頁）。 */
  shown?: number;
}

function keyList(value: unknown): string[] {
  if (typeof value !== "string") return [];
  const keys = value
    .split(",")
    .map((key) => key.trim())
    .filter((key) => KEY.test(key));
  return [...new Set(keys)].slice(0, MAX_VALUES);
}

function yearValue(value: unknown): number | undefined {
  const year = typeof value === "number" ? value : typeof value === "string" ? Number(value) : Number.NaN;
  return Number.isInteger(year) && year >= 1 && year <= 9999 ? year : undefined;
}

/**
 * 路由的 validateSearch：只留看得懂的值，其他丟掉（每個欄位都寫出來，網址上沒檢查過的值才不會留下）。
 * 年份範圍要在追蹤起始年到今年（台北）之間、起不晚於訖，不合就整組不套用：後端對這種年份一定回 422
 * （ADR-0050 第 10 點），送出去只會變成「沒有載入成功」（審查 S1-1）。
 */
export function validateSearchParams(search: Record<string, unknown>, currentYear: number = currentTaipeiYear()): SearchParams {
  const scope = SCOPES.includes(search.scope as PaperListScope) ? (search.scope as PaperListScope) : "tracked";
  const journal = keyList(search.journal);
  const topic = keyList(search.topic);
  const method = keyList(search.method);
  const from = yearValue(search.from);
  const to = yearValue(search.to);
  const hasYears =
    from !== undefined && to !== undefined && TRACKING_START_YEAR <= from && from <= to && to <= currentYear;
  const shown = typeof search.shown === "number" && Number.isInteger(search.shown) ? search.shown : undefined;
  return {
    scope: scope === "tracked" ? undefined : scope,
    journal: journal.length > 0 ? journal.join(",") : undefined,
    from: hasYears ? from : undefined,
    to: hasYears ? to : undefined,
    topic: topic.length > 0 ? topic.join(",") : undefined,
    method: method.length > 0 ? method.join(",") : undefined,
    this_week: search.this_week === true || search.this_week === "true" ? true : undefined,
    shown: shown !== undefined && shown > 0 ? shown : undefined,
  };
}

/**
 * 只留顯示名稱裡有的期刊、主題、方法代號（主題另外認得「未分類」）。網址上不認得的代號（舊書籤、
 * 分類表改版前留下的主題）送出去後端回 422（ADR-0050 第 10 點），所以送出前拿掉（審查 S1-2）。
 */
export function knownOnly(params: SearchParams, names: DisplayNames): SearchParams {
  const keep = (value: string | undefined, known: Set<string>) =>
    joinKeys(selectedKeys(value).filter((key) => known.has(key)));
  const keys = (entries: readonly { key: string }[]) => new Set(entries.map((entry) => entry.key));
  return {
    ...params,
    journal: keep(params.journal, keys(names.journals)),
    topic: keep(params.topic, new Set([...keys(names.topics), UNCLASSIFIED_TOPIC])),
    method: keep(params.method, keys(names.methods)),
  };
}


/** 選了哪些期刊、主題（網址上的逗號分隔字串拆開）。 */
export function selectedKeys(value: string | undefined): string[] {
  return value === undefined ? [] : value.split(",");
}

/** 把選的代號寫回網址：沒有選就省略。 */
export function joinKeys(keys: readonly string[]): string | undefined {
  return keys.length === 0 ? undefined : keys.join(",");
}

/** 有沒有期刊、年份、主題、方法、「本週新增」任何一個條件（範圍不算條件）。 */
export function hasConditions(params: SearchParams): boolean {
  return (
    params.journal !== undefined ||
    params.from !== undefined ||
    params.topic !== undefined ||
    params.method !== undefined ||
    params.this_week === true
  );
}

/** 「還沒有任何條件」（Epic 3 版的「空的：還沒搜尋」）：所有追蹤期刊、沒有任何條件，列本週新增。 */
export function isStart(params: SearchParams): boolean {
  return params.scope === undefined && !hasConditions(params);
}

/** 有幾個篩選選單有選（手機「篩選 · N」）：期刊、年份、主題各算一項。 */
export function appliedFilterCount(params: SearchParams): number {
  return [params.journal, params.from, params.topic].filter((value) => value !== undefined).length;
}

/** 拿掉期刊、年份、主題與帶進來的條件（方法、本週新增；「清除篩選」）；範圍留著。 */
export function clearConditions(params: SearchParams): SearchParams {
  return { scope: params.scope };
}

/** 從…到…的每一年（API 的年份是多值「或」）。 */
export function yearRange(from: number, to: number): number[] {
  return Array.from({ length: to - from + 1 }, (_, index) => from + index);
}

/** 篩選條件（不含「目前顯示幾篇」）→ API 的參數。cursor 由資料來源層處理。 */
export function toApiParams(params: SearchParams): ListPapersParams {
  const api: ListPapersParams = {};
  const journals = selectedKeys(params.journal);
  const topics = selectedKeys(params.topic);
  if (journals.length > 0) api.journal = journals;
  if (params.from !== undefined && params.to !== undefined) api.year = yearRange(params.from, params.to);
  if (topics.length > 0) api.topic = topics;
  const methods = selectedKeys(params.method);
  if (methods.length > 0) api.method = methods;
  if (params.this_week === true) api.this_week = true;
  if (params.scope !== undefined) api.scope = params.scope;
  return api;
}

/** 年份選單的選項：不限、今年、近 3 年、近 5 年、自訂（設計稿 search-no-box.html 的年份選單）。 */
export type YearPreset = "none" | "thisYear" | "last3" | "last5" | "custom";

/** 一個年份選項代表的範圍；「不限」與「自訂」是 null。 */
export function presetRange(preset: YearPreset, currentYear: number): { from: number; to: number } | null {
  switch (preset) {
    case "thisYear":
      return { from: currentYear, to: currentYear };
    case "last3":
      return { from: currentYear - 2, to: currentYear };
    case "last5":
      return { from: currentYear - 4, to: currentYear };
    default:
      return null;
  }
}

/** 網址上的年份範圍是哪一個選項（剛好等於某個快速選項就是它，其他都是自訂）。 */
export function yearPreset(params: SearchParams, currentYear: number): YearPreset {
  if (params.from === undefined || params.to === undefined) return "none";
  for (const preset of ["thisYear", "last3", "last5"] as const) {
    const range = presetRange(preset, currentYear);
    if (range !== null && range.from === params.from && range.to === params.to) return preset;
  }
  return "custom";
}

/** 自訂年份輸入不合的原因（設計稿的四種）；合的話是 null。 */
export type YearProblem = "missing" | "tooEarly" | "tooLate" | "reversed";

/** 檢查自訂的起訖年（字串是輸入框裡的字）。合的話回範圍。 */
export function checkYears(
  fromText: string,
  toText: string,
  currentYear: number,
): { ok: true; from: number; to: number } | { ok: false; problem: YearProblem } {
  const parse = (text: string) => (/^\d{1,4}$/.test(text.trim()) ? Number(text.trim()) : Number.NaN);
  const from = parse(fromText);
  const to = parse(toText);
  if (!(from > 0) || !(to > 0)) return { ok: false, problem: "missing" };
  if (from < TRACKING_START_YEAR) return { ok: false, problem: "tooEarly" };
  if (to > currentYear) return { ok: false, problem: "tooLate" };
  if (from > to) return { ok: false, problem: "reversed" };
  return { ok: true, from, to };
}

/** 今年（台北的日曆年）：「今年」「近 N 年」與年份上限都用它。 */
export function currentTaipeiYear(now: Date = new Date()): number {
  const year = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Taipei", year: "numeric" }).format(now);
  return Number(year);
}
