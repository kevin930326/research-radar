// 找論文（Story 3.14）的篩選條件：網址參數怎麼讀、怎麼寫、怎麼換成 API 的參數（AD-21：篩選與目前顯示幾篇
// 都寫進網址，cursor 不進網址）。只處理資料，畫面上的字由 i18n 提供。
// 條件怎麼合由後端決定（ADR-0050 第 2 點、ADR-0011 的 2026-11-27 更新）：期刊、年份多值「或」；主題、方法多值預設「或」，
// 網址上有 topic_match=all／method_match=all 時「且」（Story 3.20）。前端把選的值與這兩個設定原樣送出去。
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
  /**
   * 主題、方法選了兩個以上時「同時符合」（Story 3.20）：只有 `all`；省略是「任一個」（預設，和後端一樣）。
   * 該欄位選的不到兩個時不留（tidyMatch），網址上才不會殘留。
   */
  topic_match?: "all";
  method_match?: "all";
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
  return tidyMatch({
    scope: scope === "tracked" ? undefined : scope,
    journal: journal.length > 0 ? journal.join(",") : undefined,
    from: hasYears ? from : undefined,
    to: hasYears ? to : undefined,
    topic: topic.length > 0 ? topic.join(",") : undefined,
    method: method.length > 0 ? method.join(",") : undefined,
    topic_match: search.topic_match === "all" ? "all" : undefined,
    method_match: search.method_match === "all" ? "all" : undefined,
    this_week: search.this_week === true || search.this_week === "true" ? true : undefined,
    shown: shown !== undefined && shown > 0 ? shown : undefined,
  });
}

/** 有主題、方法與它們的「同時符合」的條件（找論文與示範站論文清單都是這個形狀）。 */
interface MatchFields {
  topic?: string;
  method?: string;
  topic_match?: "all";
  method_match?: "all";
}

/**
 * 主題、方法選的不到兩個時拿掉它的「同時符合」（Story 3.20：只選一個時兩種一樣，網址上不留；取消勾選剩一個時回到任一個）。
 * 每一次換條件都經過這裡（路由的檢查、頁面的 update、選單的「清除」），網址上才不會殘留。
 */
export function tidyMatch<T extends MatchFields>(params: T): T {
  return {
    ...params,
    topic_match: selectedKeys(params.topic).length >= 2 ? params.topic_match : undefined,
    method_match: selectedKeys(params.method).length >= 2 ? params.method_match : undefined,
  };
}

/**
 * 網址列上有 `*_match`、但整理過的條件沒有（只有一個 key、不認得的拿掉後不到兩個、`ALL` 這種不認得的值）：
 * 打開頁面時要把網址列換掉（規則 3「不到兩個時網址上不留」；審查 B 的 F2）。`raw` 是路由檢查前的網址參數。
 */
export function hasStaleMatch(raw: Record<string, unknown>, effective: MatchFields): boolean {
  return (
    (raw.topic_match !== undefined && effective.topic_match === undefined) ||
    (raw.method_match !== undefined && effective.method_match === undefined)
  );
}

/** 主題或方法有沒有正在用「同時符合」（兩個以上、選了同時符合）。 */
export function hasAllMatch(params: MatchFields): boolean {
  const tidy = tidyMatch(params);
  return tidy.topic_match === "all" || tidy.method_match === "all";
}

/** 主題與方法都改回「任一個」（0 篇時的「改看符合任一個的 N 篇」）；其他條件不動。 */
export function anyMatch<T extends MatchFields>(params: T): T {
  return { ...params, topic_match: undefined, method_match: undefined };
}

/**
 * 只留顯示名稱裡有的期刊、主題、方法代號（主題另外認得「未分類」）。網址上不認得的代號（舊書籤、
 * 分類表改版前留下的主題）送出去後端回 422（ADR-0050 第 10 點），所以送出前拿掉（審查 S1-2）。
 */
export function knownOnly(params: SearchParams, names: DisplayNames): SearchParams {
  const keep = (value: string | undefined, known: Set<string>) =>
    joinKeys(selectedKeys(value).filter((key) => known.has(key)));
  const keys = (entries: readonly { key: string }[]) => new Set(entries.map((entry) => entry.key));
  // 拿掉不認得的之後不到兩個：「同時符合」也拿掉（tidyMatch）。
  return tidyMatch({
    ...params,
    journal: keep(params.journal, keys(names.journals)),
    topic: keep(params.topic, new Set([...keys(names.topics), UNCLASSIFIED_TOPIC])),
    method: keep(params.method, keys(names.methods)),
  });
}


/**
 * 照「拿掉不認得的代號之後」的個數整理「同時符合」（審查 B 的 F1）：網址上有分類表已經沒有的主題（舊書籤）時，
 * 網址上的個數比實際用到的多，只看網址會把不該留的 `*_match=all` 留下來。不認得的代號本身照舊留在網址上
 * （送 API 前由 knownOnly 拿掉）。顯示名稱還沒讀到時（null）只能照網址上的個數（tidyMatch）。
 */
export function tidyMatchKnown<T extends MatchFields>(params: T, names: DisplayNames | null): T {
  if (names === null) return tidyMatch(params);
  const known = knownOnly(
    { topic: params.topic, method: params.method, topic_match: params.topic_match, method_match: params.method_match },
    names,
  );
  return { ...params, topic_match: known.topic_match, method_match: known.method_match };
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

/**
 * 篩選條件（不含「目前顯示幾篇」）→ API 的參數。cursor 由資料來源層處理。
 * 「同時符合」只在那一欄有兩個以上時送 `*_match=all`；任一個不送（後端沒給就是 any，Story 3.20）。
 */
export function toApiParams(params: SearchParams): ListPapersParams {
  const api: ListPapersParams = {};
  const tidy = tidyMatch(params);
  const journals = selectedKeys(params.journal);
  const topics = selectedKeys(params.topic);
  if (journals.length > 0) api.journal = journals;
  if (params.from !== undefined && params.to !== undefined) api.year = yearRange(params.from, params.to);
  if (topics.length > 0) api.topic = topics;
  if (tidy.topic_match === "all") api.topic_match = "all";
  const methods = selectedKeys(params.method);
  if (methods.length > 0) api.method = methods;
  if (tidy.method_match === "all") api.method_match = "all";
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
