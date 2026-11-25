import { useQuery } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { Link } from "@tanstack/react-router";
import { Fragment, useState, type CSSProperties, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { DataDateBar } from "../components/DataDateBar";
import { NewTabNote } from "../components/ExternalArrow";
import { PaperTitle } from "../components/PaperText";
import { ClassificationTagLink, UnclassifiedTag } from "../components/paper/PaperBiblio";
import { FilterMenu, type ClearTarget } from "../components/search/FilterMenus";
import { JournalOptions, MethodOptions, TopicOptions, YearOptions } from "../components/search/FilterLists";
import { focusFilters, journalName, topicName, type FilterState } from "../components/search/filters";
import { FilterSheet, type FilterSection } from "../components/search/MobileFilters";
import { OpenFullText } from "../components/search/PaperRows";
import { CrossIcon, FilterOption, NoticeIcon, formatYears } from "../components/search/parts";
import { datasource } from "../datasource";
import type { DemoPaperRow } from "../datasource/types";
import type { DisplayNames } from "../generated/model";
import {
  clearDemoConditions,
  demoFilterCount,
  filterDemoRows,
  fromSearchParams,
  hasDemoConditions,
  knownDemoOnly,
  toSearchParams,
  type DemoPapersParams,
} from "./demo-papers-filters";
import { externalUrl, formatCount } from "./paper-format";
import { ConditionTag, ErrorBar, More } from "./SearchPage";
import { TRACKING_START_YEAR, currentTaipeiYear, selectedKeys, type SearchParams } from "./search-filters";

// 示範站的論文清單（Story 3.14 示範站；EXPERIENCE.md 畫面 7、State Patterns「7. 論文清單」；
// 設計稿 docs/ux/reference/screens/papers-demo.html）。
// - 篩選在瀏覽器裡做（AD-20）：讀 preview/ 的索引與標題一次，之後每換一個條件都在本機篩，不再讀檔。
// - 標題寫明這是哪個數字的論文：沒有條件是「全部論文 · N 篇」，有條件是各條件 · N 篇（設計稿的順序：標題的字、
//   本週新增、期刊、主題、方法、年份、有摘要、有完整內容）。
// - 篩選列：用標題裡的字篩選（輸入就套用）、期刊、年份、主題、方法四個選單（和找論文共用的選單與選項）、
//   「只看有摘要的」「只看有完整內容的」；從連結帶進來的「本週新增」是可以移除的條件標籤。篩選都寫進網址（AD-21）。
// - 每列：標題、期刊 · 年份、AI 整理＋主題與方法的標籤、「● 有完整內容」或「○ 只有書目」、開啟全文（DOI）。
//   一次顯示 manifest 設定的篇數（50），最下面「再載入 50 篇」（「目前顯示幾篇」寫進網址）。
// - 載入中：8 列骨架，篩選列照樣可以改；錯誤：「沒有載入成功。重新整理後再試」＋「重新整理」；
//   篩到 0 篇：「沒有符合的論文」＋「清除篩選」。
// 和設計稿、EXPERIENCE.md 不同的地方（理由與要 Kevin 決定的，寫在決定紀錄 3.14-decisions.md）：
// - 標題不是連結：示範站的論文頁（Story 3.9）還沒做（AR-67）。
// - 期刊顯示全名（EXPERIENCE.md 決定第 92 項；匯出的分類表也只有全名，沒有縮寫）；選單裡的期刊同找論文只有全名。
// - 手機的論文列「開啟全文」靠右（決定第 94 項）。
// - 「AI 整理」是不能點的字（設計稿的樣子），不是論文頁那種可以點開說明的標章。

/** 網址上的條件（路由驗證過的）與換條件（寫回網址：傳「怎麼從目前的條件算出下一組」）。 */
interface DemoPapersPageProps {
  params: DemoPapersParams;
  onUpdate: (change: (current: DemoPapersParams) => DemoPapersParams) => void;
}

export const DEMO_PAPERS_QUERY_KEY = ["demo-papers"] as const;

function demoPaperSource() {
  const source = datasource.demoPapers;
  if (source === null) throw new Error("demoPapers is only available in the demo build");
  return source;
}

export function DemoPapersPage({ params: addressParams, onUpdate }: DemoPapersPageProps) {
  const { t } = useTranslation("demo");
  const { t: ts } = useTranslation("search");
  const { t: tc } = useTranslation("common");
  const { t: tp } = useTranslation("paper");
  const currentYear = currentTaipeiYear();
  // 先讀 manifest（資料截至哪一天馬上顯示），再讀它指到的三個檔（同一次匯出；換了匯出就是另一份快取）。
  const manifest = useQuery({
    queryKey: [...DEMO_PAPERS_QUERY_KEY, "manifest"],
    queryFn: () => demoPaperSource().manifest(),
    retry: false,
    staleTime: Infinity,
    retryOnMount: false,
  });
  const manifestData = manifest.data ?? null;
  const index = useQuery({
    queryKey: [...DEMO_PAPERS_QUERY_KEY, "index", manifestData?.exportId ?? null],
    queryFn: () => {
      if (manifestData === null) throw new Error("manifest not loaded");
      return demoPaperSource().index(manifestData);
    },
    enabled: manifestData !== null,
    retry: false,
    staleTime: Infinity,
    retryOnMount: false,
  });
  const data = index.data ?? null;
  const names = data?.names ?? null;
  // 網址上不認得的期刊、主題、方法代號拿掉（舊書籤；同找論文）。
  const params = names === null ? addressParams : knownDemoOnly(addressParams, names);
  const update = (change: (current: DemoPapersParams) => DemoPapersParams) =>
    onUpdate((current) => ({ ...change(current), shown: undefined }));
  const unclassified = tp("paper.classification.unclassified");

  // 共用的篩選選單與選項看的是找論文的條件形狀（期刊、年份、主題、方法的欄位相同），改了再寫回這一頁的條件。
  const filterState: FilterState = {
    params: toSearchParams(params),
    names,
    currentYear,
    update: (change: (current: SearchParams) => SearchParams) =>
      update((current) => fromSearchParams(current, change(toSearchParams(current)))),
  };
  const failed = (manifest.isError && manifestData === null) || (index.isError && data === null);
  const matched = data === null ? null : filterDemoRows(data.rows, params);

  const heading = headingParts(params, names, { t, thisWeek: ts("search.condition.thisWeek"), unclassified });
  const clear: ClearTarget = { to: "/papers", search: clearDemoConditions(params) };
  const removeThisWeek = () => {
    focusFilters();
    update((current) => ({ ...current, this_week: undefined }));
  };
  const conditionTags =
    params.this_week === true ? <ConditionTag kind="this_week" label={ts("search.condition.thisWeek")} onRemove={removeThisWeek} /> : null;
  const clearAll = hasDemoConditions(params) ? <ClearAllLink target={clear} className="-my-[6px] ml-2 text-s13 tracking-004" /> : null;

  let body: ReactNode;
  if (failed) {
    body = null;
  } else if (data === null || matched === null) {
    body = <DemoSkeletonRows label={ts("search.results.loading")} />;
  } else if (matched.length === 0) {
    body = <ZeroBar clear={hasDemoConditions(params) ? clear : null} />;
  } else {
    const pageSize = data.pageSize;
    const shown = Math.min(Math.max(params.shown ?? pageSize, pageSize), matched.length);
    const rows = matched.slice(0, shown);
    body = (
      <>
        <ul data-paper-rows className="mt-[28px] border-b border-hairline split:mt-[36px]">
          {rows.map((row) => (
            <DemoPaperRowItem key={row.id} row={row} names={data.names} />
          ))}
        </ul>
        {rows.length < matched.length ? (
          <More
            shown={rows.length}
            total={matched.length}
            pageSize={pageSize}
            loading={false}
            onMore={() => onUpdate((current) => ({ ...current, shown: rows.length + pageSize }))}
          />
        ) : null}
      </>
    );
  }

  return (
    <div data-page="demo-papers" className="text-s14 leading-normal tracking-0 text-text-primary">
      <DataDateBar snapshot={manifestData?.snapshot ?? null} />
      <h1 className="mt-[14px] font-title-zh text-page-title-zh text-balance text-text-primary split:mt-[18px]">
        {heading.map((part, at) => (
          <span key={at}>
            {at > 0 ? " " : null}
            <span className="whitespace-nowrap">
              {part}
              {at < heading.length - 1 || matched !== null ? <span className="pl-[0.3em] text-text-tertiary"> ·</span> : null}
            </span>
          </span>
        ))}
        {matched !== null ? (
          <>
            {" "}
            <span data-result-count className="whitespace-nowrap">
              {t("demo.papers.count", { count: formatCount(matched.length) })}
            </span>
          </>
        ) : null}
      </h1>
      {failed ? (
        <ErrorBar message={tc("states.loadFailed.message")} action={tc("states.loadFailed.action")} onRetry={() => void (manifest.isError ? manifest.refetch() : index.refetch())} />
      ) : (
        <>
          <TitleQuery
            value={params.title ?? ""}
            onChange={(title) => update((current) => ({ ...current, title: title.trim() === "" ? undefined : title }))}
          />
          <div className="mt-[20px] hidden flex-wrap items-center gap-2 split:flex">
            {conditionTags}
            <DesktopMenus state={filterState} params={params} unclassified={unclassified} />
            <span aria-hidden="true" className="mx-[6px] h-[18px] w-px bg-border" />
            <ToggleChip pressed={params.abstract === true} onToggle={() => update((current) => ({ ...current, abstract: current.abstract === true ? undefined : true }))}>
              {t("demo.filter.abstractOnly")}
            </ToggleChip>
            <ToggleChip pressed={params.full === true} onToggle={() => update((current) => ({ ...current, full: current.full === true ? undefined : true }))}>
              {t("demo.filter.fullOnly")}
            </ToggleChip>
            {clearAll}
          </div>
          <DemoMobileFilters state={filterState} params={params} update={update} total={matched?.length ?? null} clear={clear} unclassified={unclassified}>
            {conditionTags}
            {/* 手機也和桌機一樣：有任何條件（含帶進來的「本週新增」）就有「清除篩選」（設計稿「帶條件進來」的手機版）。 */}
            {clearAll}
          </DemoMobileFilters>
          {body}
        </>
      )}
      <NewTabNote />
    </div>
  );
}

/**
 * 標題那一行的各段（設計稿：每段後面接「 ·」，最後是篇數）。沒有條件是「全部論文」。
 * 一個欄位選了好幾個時用「、」接起來（設計稿只畫了一個的樣子，列給 Kevin）。
 * 顯示名稱還沒讀到時（載入中、錯誤）不寫期刊、主題、方法這幾段。
 */
function headingParts(
  params: DemoPapersParams,
  names: DisplayNames | null,
  { t, thisWeek, unclassified }: { t: TFunction<"demo">; thisWeek: string; unclassified: string },
): string[] {
  const parts: string[] = [];
  if (params.title !== undefined) parts.push(t("demo.papers.titleHas", { query: params.title.trim() }));
  if (params.this_week === true) parts.push(thisWeek);
  const join = (values: string[]) => values.join(t("demo.papers.separator"));
  if (names !== null) {
    const journals = selectedKeys(params.journal).map((key) => journalName(names, key));
    if (journals.length > 0) parts.push(join(journals));
    const topics = selectedKeys(params.topic).map((key) => topicName(names, key, unclassified));
    if (topics.length > 0) parts.push(join(topics));
    const methods = selectedKeys(params.method).map((key) => names.methods.find((entry) => entry.key === key)?.name ?? key);
    if (methods.length > 0) parts.push(join(methods));
  }
  if (params.from !== undefined && params.to !== undefined) parts.push(t("demo.papers.years", { years: formatYears(params.from, params.to) }));
  if (params.abstract === true) parts.push(t("demo.papers.withAbstract"));
  if (params.full === true) parts.push(t("demo.papers.withFull"));
  return parts.length === 0 ? [t("demo.papers.all")] : parts;
}

/**
 * 桌機的四個篩選選單（期刊、年份、主題、方法；和找論文同一個選單元件）。
 * 選單底部的「清除」只拿掉這一個選單的條件，其他條件（兩個「只看…的」、標題的字、本週新增）留著。
 */
function DesktopMenus({ state, params, unclassified }: { state: FilterState; params: DemoPapersParams; unclassified: string }) {
  const { t } = useTranslation("search");
  const { t: tp } = useTranslation("paper");
  const values = menuValues(state, unclassified, t);
  const clearOf = (field: "journal" | "topic" | "method"): ClearTarget => ({
    to: "/papers",
    search: { ...params, [field]: undefined, shown: undefined },
  });
  return (
    <>
      <FilterMenu label={t("search.filter.journal")} value={values.journal} width="wide" clear={clearOf("journal")}>
        <JournalOptions state={state} size="menu" />
      </FilterMenu>
      <FilterMenu label={t("search.filter.year")} value={values.year} width="narrow" clear={null}>
        <YearOptions state={state} size="menu" startYear={TRACKING_START_YEAR} />
      </FilterMenu>
      <FilterMenu label={t("search.filter.topic")} value={values.topic} width="narrow" clear={clearOf("topic")}>
        <TopicOptions state={state} size="menu" />
      </FilterMenu>
      <FilterMenu label={tp("paper.classification.method")} value={values.method} width="narrow" clear={clearOf("method")}>
        <MethodOptions state={state} size="menu" />
      </FilterMenu>
    </>
  );
}

/** 選單標籤（與手機每一段的標題右邊）寫的「選了什麼」：一個寫名稱，多個寫「N 本」「N 個」。 */
function menuValues(
  state: FilterState,
  unclassified: string,
  t: TFunction<"search">,
): Record<"journal" | "year" | "topic" | "method", string | null> {
  const one = (keys: string[], name: (key: string) => string, countKey: "search.filter.journalCount" | "search.filter.topicCount") =>
    keys.length === 0 ? null : keys.length === 1 ? name(keys[0]!) : t(countKey, { count: keys.length });
  const { params, names } = state;
  return {
    journal: one(selectedKeys(params.journal), (key) => journalName(names, key), "search.filter.journalCount"),
    year: params.from === undefined || params.to === undefined ? null : formatYears(params.from, params.to),
    topic: one(selectedKeys(params.topic), (key) => topicName(names, key, unclassified), "search.filter.topicCount"),
    method: one(
      selectedKeys(params.method),
      (key) => names?.methods.find((entry) => entry.key === key)?.name ?? key,
      "search.filter.topicCount",
    ),
  };
}

/**
 * 「只看有摘要的」「只看有完整內容的」（設計稿 .chip.f.tg）：和篩選標籤同一個樣子，按一下切換，選中是暖白底。
 * 看得到的 32px，按鈕本身 44px（ADR-0037），負外距抵掉。
 */
function ToggleChip({ pressed, onToggle, children }: { pressed: boolean; onToggle: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      data-toggle-chip
      onClick={onToggle}
      className="group -my-[6px] inline-flex h-tap-target cursor-pointer items-center bg-transparent p-[0px]"
    >
      <span
        className={`inline-flex h-8 items-center rounded-full border px-[14px] text-s13 leading-normal tracking-004 whitespace-nowrap transition-[border-color,color,background-color] duration-200 ${
          pressed ? "border-text-primary bg-text-primary text-on-button" : "border-border text-text-reading group-hover:border-text-primary"
        }`}
      >
        {children}
      </span>
    </button>
  );
}

/** 「清除篩選」：連到只留標題的字的網址（設計稿是連結）。按了之後它會不見，焦點先移到篩選。 */
function ClearAllLink({ target, className }: { target: ClearTarget; className: string }) {
  const { t } = useTranslation("search");
  return (
    <Link
      to={target.to}
      search={target.search}
      replace
      resetScroll={false}
      onClick={focusFilters}
      data-clear-all
      className={`inline-flex min-h-tap-target items-center text-accent-gold no-underline decoration-1 underline-offset-4 hover:underline ${className}`}
    >
      {t("search.filter.clearAll")}
    </Link>
  );
}

/**
 * 用標題裡的字篩選（設計稿 .qbox）：底線輸入框，桌機寬 360、手機滿寬；有字時右邊「×」清除文字。
 * 輸入就套用（寫進網址、換掉這一筆瀏覽紀錄）；網址上的字變了（例如上一頁）時輸入框跟著換。
 */
function TitleQuery({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const { t } = useTranslation("demo");
  const [text, setText] = useState(value);
  const [seen, setSeen] = useState(value);
  if (seen !== value) {
    setSeen(value);
    if (text.trim() !== value.trim()) setText(value);
  }
  const change = (next: string) => {
    setText(next);
    onChange(next);
  };
  return (
    <div className="mt-6 split:mt-[28px]">
      <div className="box-border flex h-[40px] w-full items-center gap-1 border-b border-border transition-[border-color] duration-250 focus-within:border-accent-gold split:w-[360px] split:max-w-full">
        <input
          type="text"
          data-title-query
          aria-label={t("demo.filter.title")}
          placeholder={t("demo.filter.title")}
          value={text}
          onChange={(event) => change(event.target.value)}
          // 輸入框本身 44px 高（ADR-0037 可點範圍），底線框是設計稿的 40px，上下各超出 2px。
          className="-my-[2px] h-tap-target min-w-[0px] flex-1 bg-transparent p-[0px] text-s16 tracking-003 text-text-primary placeholder:text-text-tertiary"
        />
        {text === "" ? null : (
          // 看得到的圓 28px，按鈕本身 44px（ADR-0037），負外距抵掉。
          <button
            type="button"
            aria-label={t("demo.filter.clearTitle")}
            onClick={(event) => {
              change("");
              event.currentTarget.parentElement?.querySelector("input")?.focus();
            }}
            className="group -my-[2px] -mr-2 -ml-2 inline-flex size-tap-target shrink-0 cursor-pointer items-center justify-center bg-transparent p-[0px] text-text-secondary"
          >
            <span className="inline-flex size-[28px] items-center justify-center rounded-full transition-[background-color,color] duration-200 group-hover:bg-menu-item-hover group-hover:text-text-primary">
              <CrossIcon size={9} />
            </span>
          </button>
        )}
      </div>
    </div>
  );
}

/** 手機的篩選：期刊、年份、主題、方法四段，下面是「只看有摘要的」「只看有完整內容的」兩個勾選（設計稿）。 */
function DemoMobileFilters({
  state,
  params,
  update,
  total,
  clear,
  unclassified,
  children,
}: {
  state: FilterState;
  params: DemoPapersParams;
  update: (change: (current: DemoPapersParams) => DemoPapersParams) => void;
  total: number | null;
  clear: ClearTarget;
  unclassified: string;
  children: ReactNode;
}) {
  const { t } = useTranslation("search");
  const { t: tp } = useTranslation("paper");
  const { t: td } = useTranslation("demo");
  const values = menuValues(state, unclassified, t);
  const sections: FilterSection[] = [
    { key: "journal", label: t("search.filter.journal"), value: values.journal, body: <JournalOptions state={state} size="sheet" /> },
    {
      key: "year",
      label: t("search.filter.year"),
      value: values.year,
      body: <YearOptions state={state} size="sheet" startYear={TRACKING_START_YEAR} />,
    },
    { key: "topic", label: t("search.filter.topic"), value: values.topic, body: <TopicOptions state={state} size="sheet" /> },
    { key: "method", label: tp("paper.classification.method"), value: values.method, body: <MethodOptions state={state} size="sheet" /> },
  ];
  const extra = (
    <div className="border-b border-hairline py-[6px]">
      <FilterOption
        type="checkbox"
        name="demo-abstract"
        size="sheet"
        labelSize="large"
        checked={params.abstract === true}
        onChange={() => update((current) => ({ ...current, abstract: current.abstract === true ? undefined : true }))}
        label={td("demo.filter.abstractOnly")}
      />
      <FilterOption
        type="checkbox"
        name="demo-full"
        size="sheet"
        labelSize="large"
        checked={params.full === true}
        onChange={() => update((current) => ({ ...current, full: current.full === true ? undefined : true }))}
        label={td("demo.filter.fullOnly")}
      />
    </div>
  );
  return (
    <FilterSheet
      count={demoFilterCount(params)}
      total={total}
      sections={sections}
      extra={extra}
      rowMarginClass="mt-[14px]"
      ClearLink={({ onClick }) => (
        <Link
          to={clear.to}
          search={clear.search}
          replace
          resetScroll={false}
          onClick={onClick}
          className="inline-flex min-h-tap-target items-center text-s14 tracking-004 text-accent-gold no-underline decoration-1 underline-offset-4 hover:underline"
        >
          {t("search.filter.clearAll")}
        </Link>
      )}
    >
      {children}
    </FilterSheet>
  );
}

/** 一列論文（設計稿 papers-demo.html 的 .prow）。 */
function DemoPaperRowItem({ row, names }: { row: DemoPaperRow; names: DisplayNames }) {
  const href = externalUrl(row.doiUrl);
  return (
    <li
      data-paper-row={row.id}
      className="group border-t border-hairline py-[22px] split:flex split:items-start split:justify-between split:gap-12 split:py-6"
    >
      <div className="min-w-[0px] split:max-w-reading-width split:flex-1">
        {/* 標題不是連結：示範站的論文頁（Story 3.9）還沒做（AR-67）。 */}
        <PaperTitle as="h3" size="row" wrap="pretty" className="max-split:text-s20">
          {row.title}
        </PaperTitle>
        <div className="mt-2 split:mt-[10px] split:flex split:flex-wrap split:items-center split:gap-x-[20px] split:gap-y-2">
          <RowMeta row={row} names={names} />
          <RowTags row={row} names={names} />
        </div>
      </div>
      <div className="mt-[10px] flex items-center justify-between gap-[20px] split:mt-[0px] split:w-[216px] split:shrink-0 split:pt-px">
        <FullContentMark full={row.hasFullContent} />
        {href === null ? null : <OpenFullText href={href} />}
      </div>
    </li>
  );
}

/** 期刊 · 年份（DM Mono 13px；期刊全名太長時在字之間換行，不截斷；決定第 92 項）。 */
function RowMeta({ row, names }: { row: DemoPaperRow; names: DisplayNames }) {
  const parts: ReactNode[] = [];
  if (row.journal !== null) parts.push(<span key="journal" className="text-text-reading">{journalName(names, row.journal)}</span>);
  if (row.year !== null) parts.push(<span key="year">{row.year}</span>);
  if (parts.length === 0) return null;
  // 899px 以下上 2px、下 1px：設計稿手機版這一行是放在一般文字行裡的 inline-flex，整行 20px 高、字往下 2px。
  return (
    <div className="font-mono text-s13 leading-normal tracking-004 text-balance text-text-secondary max-split:pt-[2px] max-split:pb-px">
      {parts.map((part, at) =>
        at === 0 ? (
          part
        ) : (
          // 「·」左右各 9px（設計稿）；它前面可以換行（期刊全名太長時）。「·」和年份之間沒有換行點（瀏覽器不會拆開，
          // 已在 Chromium 量過），所以不另外包一層：結構和設計稿一樣，比對工具才量得到列的上框線。
          <Fragment key={at}>
            <wbr />
            <span className="px-[9px] whitespace-nowrap text-text-tertiary">·</span>
            {part}
          </Fragment>
        ),
      )}
    </div>
  );
}

/**
 * 「AI 整理」＋主題與方法的標籤（設計稿：先主題、再方法；標籤連到論文清單並帶好這一個分類，「未分類」不能點）。
 * 分類沒有可以顯示的值（摘要來源不允許公開、還沒產生、沒有產生成功）時整段不顯示：設計稿沒畫這幾種，列給 Kevin。
 * 人工核對過（分類的來歷 human_verified）時是實心菱形＋「已核對」。
 */
function RowTags({ row, names }: { row: DemoPaperRow; names: DisplayNames }) {
  const { t } = useTranslation("ai");
  const topics = row.topics ?? [];
  const methods = row.methods ?? [];
  if (row.classificationState !== "ready" || topics.length + methods.length === 0) return null;
  const verified = row.classificationProvenance?.human_verified === true;
  const methodName = (key: string) => names.methods.find((entry) => entry.key === key)?.name ?? key;
  return (
    <div className="mt-[10px] flex flex-wrap items-center gap-x-[10px] gap-y-2 split:mt-[0px]">
      {row.classificationProvenance === null ? null : (
        <span className="inline-flex items-center gap-[7px] font-mono text-s12 leading-normal tracking-006 whitespace-nowrap text-text-secondary">
          <svg width="9" height="9" viewBox="0 0 9 9" fill="none" aria-hidden="true" className="shrink-0">
            <path d="M4.5 0.8L8.2 4.5 4.5 8.2 0.8 4.5z" stroke="currentColor" strokeWidth="0.9" fill={verified ? "currentColor" : "none"} />
          </svg>
          {verified ? t("ai.badge.verified") : t("ai.badge.short")}
        </span>
      )}
      {topics.map((key) =>
        key === "unclassified" ? (
          <UnclassifiedTag key={`topic-${key}`} size="row" />
        ) : (
          <ClassificationTagLink key={`topic-${key}`} field="topic" value={key} to="/papers" size="row">
            {topicName(names, key, key)}
          </ClassificationTagLink>
        ),
      )}
      {methods.map((key) => (
        <ClassificationTagLink key={`method-${key}`} field="method" value={key} to="/papers" size="row">
          {methodName(key)}
        </ClassificationTagLink>
      ))}
    </div>
  );
}

/** 「● 有完整內容」或「○ 只有書目」（設計稿：13px、34px 高；有完整內容是閱讀字色＋實心點，只有書目是第二層字色＋空心點）。 */
function FullContentMark({ full }: { full: boolean }) {
  const { t } = useTranslation("demo");
  return (
    <span
      data-full-content={full ? "yes" : "no"}
      className={`inline-flex h-[34px] min-w-[108px] items-center gap-2 text-s13 tracking-004 whitespace-nowrap max-split:min-w-[0px] ${
        full ? "text-text-reading" : "text-text-secondary"
      }`}
    >
      <span
        aria-hidden="true"
        className={full ? "size-[6px] shrink-0 rounded-full bg-text-primary" : "box-border size-[6px] shrink-0 rounded-full border border-text-secondary"}
      />
      {full ? t("demo.row.full") : t("demo.row.biblioOnly")}
    </span>
  );
}

/** 篩到 0 篇（State Patterns「7. 論文清單」）：「沒有符合的論文」＋「清除篩選」（只剩標題的字時沒有可以清除的）。 */
function ZeroBar({ clear }: { clear: ClearTarget | null }) {
  const { t } = useTranslation("search");
  return (
    <div role="status" data-state="zero" className="mt-[36px] flex max-w-reading-width items-start gap-[14px] split:mt-12">
      <span className="flex text-text-secondary">
        <NoticeIcon className="mt-[7px]" />
      </span>
      <div>
        <p className="text-s17 leading-170 text-text-primary">{t("search.empty.noMatch")}</p>
        {clear === null ? null : (
          <div className="mt-2">
            <ClearAllLink target={clear} className="-my-1 text-nav" />
          </div>
        )}
      </div>
    </div>
  );
}

// 載入中的骨架（State Patterns「7. 論文清單」：8 列論文列的骨架；設計稿「載入中」）：髮線色的長條、不閃動。
// 桌機：標題一條、期刊年份一條＋標籤一條（圓角）、右側兩條；手機：標題兩條、期刊年份、標籤、最後一條。
const SKELETON_ROWS: readonly { title: number; tags: number }[] = [
  { title: 520, tags: 300 },
  { title: 610, tags: 260 },
  { title: 440, tags: 320 },
  { title: 680, tags: 280 },
  { title: 560, tags: 300 },
  { title: 480, tags: 240 },
  { title: 600, tags: 310 },
  { title: 500, tags: 270 },
];

function Bar({ className, width }: { className: string; width?: readonly [number, number] }) {
  if (width === undefined) return <div className={`max-w-full bg-hairline ${className}`} />;
  const style = { "--bar-mobile": `${width[0]}px`, "--bar-desktop": `${width[1]}px` } as CSSProperties;
  return <div style={style} className={`w-(--bar-mobile) max-w-full bg-hairline split:w-(--bar-desktop) ${className}`} />;
}

function DemoSkeletonRows({ label }: { label: string }) {
  return (
    <div role="status" aria-label={label} data-state="loading" className="mt-[28px] border-y border-hairline split:mt-[36px]">
      {SKELETON_ROWS.map(({ title, tags }, at) => (
        <div
          key={at}
          aria-hidden="true"
          className={`py-[26px] split:flex split:items-start split:justify-between split:gap-12 split:py-[28px] ${at > 0 ? "border-t border-hairline" : ""}`}
        >
          <div className="min-w-[0px] split:max-w-reading-width split:flex-1">
            <Bar className="h-[15px] split:h-4" width={[title * 0.62, title]} />
            <Bar className="mt-[11px] h-[15px] split:hidden" width={[title * 0.4, title * 0.4]} />
            <div className="split:mt-4 split:flex split:items-center split:gap-[20px]">
              <Bar className="mt-4 h-[10px] w-[96px] split:mt-[0px]" />
              <Bar className="mt-4 h-[22px] rounded-full split:mt-[0px]" width={[tags * 0.8, tags]} />
            </div>
          </div>
          <div className="mt-6 split:mt-[0px] split:flex split:h-[34px] split:w-[216px] split:shrink-0 split:items-center split:justify-between split:gap-[20px]">
            <Bar className="h-[10px] w-[150px] split:w-[108px]" />
            <Bar className="hidden h-[10px] w-16 split:block" />
          </div>
        </div>
      ))}
    </div>
  );
}
