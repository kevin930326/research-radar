import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useRef, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { DataDateBar } from "../components/DataDateBar";
import { NewTabNote } from "../components/ExternalArrow";
import type { FilterState } from "../components/search/filters";
import { FilterMenus } from "../components/search/FilterMenus";
import { MobileFilters } from "../components/search/MobileFilters";
import { PaperRowList, SkeletonRows } from "../components/search/PaperRows";
import { CrossIcon, NoticeIcon } from "../components/search/parts";
import type { PaperList } from "../datasource/types";
import type { SnapshotRef } from "../generated/model";
import { clientConfigQuery, displayNamesQuery, filtersKey, paperListQuery } from "../routes/paper-queries";
import { formatCount, splitTemplate } from "./paper-format";
import {
  MAX_SHOWN,
  START_ROWS,
  appliedFilterCount,
  clearConditions,
  currentTaipeiYear,
  hasConditions,
  isStart,
  toApiParams,
  type SearchParams,
} from "./search-filters";

// 找論文（Story 3.14 成員版；EXPERIENCE.md 畫面 12 的 Epic 3 版、State Patterns「12. 找論文」；
// 設計稿 docs/ux/reference/screens/search-no-box.html）。
// 照 AR-67：範圍切換與期刊、年份、主題三個篩選選單；沒有搜尋框、例句、「沒有摘要的論文只比對標題…」那一句，
// 結果依年份排序（後端的排序，ADR-0050 第 5 點）。
// - 沒有任何條件（所有追蹤期刊、沒有篩選）：列本週新增的論文（前 3 篇，設計稿）與「看全部 N 篇」（帶「本週新增」條件）。
// - 有條件：「找到 N 篇」、論文列一次一頁（client-config 的 paper_list_page_size），「已顯示 n／N」與「再載入 20 篇」。
//   N 都是 API 的 total，前端不自己算；條件怎麼合由後端決定。
// - 篩選寫進網址、選了立刻套用（AD-21）；「再載入」只把網址的「目前顯示幾篇」加一頁，資料來源層接著讀下一頁。
// - 載入中：6 列骨架，篩選列照樣可以改；錯誤：訊息條＋「重新整理」（條件留著）；篩到 0 篇：「沒有符合的論文」＋「清除篩選」。
// - 任何一個 API 回 401：照登入過期處理（由路由清快取、回登入頁）。
// 頁面最外層把字級、行高、字距設成設計稿 .pp 的樣子（14px、瀏覽器預設行高、沒有字距、最亮的字色），區塊只寫不一樣的地方。
// 頁尾在 Story 3.15（Kevin 2026-11-16 決定），導覽列其他連結的目的地還沒做（AR-67）。

interface SearchPageProps {
  /** 網址上的條件（路由驗證過的）。 */
  params: SearchParams;
  /** 換條件（寫回網址）。 */
  onChange: (next: SearchParams) => void;
  /** 任何一個 API 回 401。 */
  onSignedOut: () => void;
}

/** 「找到 {{count}} 篇」這種句子：數字換成另一種樣式（DM Mono、最亮的字色）。 */
function WithNumber({ template, value, numberClass }: { template: string; value: string; numberClass: string }) {
  return (
    <>
      {splitTemplate(template).map((part, index) =>
        part.slot === null ? (
          part.text
        ) : (
          <span key={index} className={numberClass}>
            {value}
          </span>
        ),
      )}
    </>
  );
}

/** 快取裡最近一次拿到的快照（換條件、載入中時資料日期列不跟著消失）。 */
function latestSnapshot(queryClient: ReturnType<typeof useQueryClient>): SnapshotRef | null {
  let latest: { at: number; snapshot: SnapshotRef | null } | null = null;
  for (const query of queryClient.getQueryCache().findAll({ queryKey: ["paper-list"] })) {
    const data = query.state.data as { kind?: string; list?: PaperList } | undefined;
    if (data?.kind !== "found" || data.list === undefined) continue;
    if (latest === null || query.state.dataUpdatedAt > latest.at) {
      latest = { at: query.state.dataUpdatedAt, snapshot: data.list.snapshot };
    }
  }
  return latest?.snapshot ?? null;
}

export function SearchPage({ params, onChange, onSignedOut }: SearchPageProps) {
  const { t } = useTranslation("search");
  const { t: tc } = useTranslation("common");
  const queryClient = useQueryClient();
  const currentYear = currentTaipeiYear();
  const config = useQuery(clientConfigQuery);
  const names = useQuery(displayNamesQuery);
  const pageSize = config.data?.kind === "found" ? config.data.config.paper_list_page_size : null;
  const start = isStart(params);
  const filters = toApiParams(start ? { this_week: true } : params);
  const limit =
    pageSize === null ? null : start ? START_ROWS : Math.min(Math.max(params.shown ?? pageSize, pageSize), MAX_SHOWN);
  const key = filtersKey(filters);
  const list = useQuery({
    ...paperListQuery(queryClient, filters, limit ?? 0),
    enabled: limit !== null,
    // 「再載入」（同一組條件、多要一頁）時，新的一頁來之前先留著已經顯示的論文；換條件時不留（顯示骨架）。
    placeholderData: (previous, previousQuery) => (previousQuery?.queryKey[1] === key ? previous : undefined),
  });

  const signedOut = [config.data, names.data, list.data].some((data) => data?.kind === "signedOut");
  // 只通知一次：換到登入頁的途中這一頁還會再畫幾次（onSignedOut 每次都是新的函式），
  // 再通知一次會用登入頁自己的網址當 next，回不到這一頁。
  const notified = useRef(false);
  useEffect(() => {
    if (!signedOut || notified.current) return;
    notified.current = true;
    onSignedOut();
  }, [signedOut, onSignedOut]);

  const state: FilterState = {
    params,
    names: names.data?.kind === "found" ? names.data.names : null,
    currentYear,
    apply: (next) => onChange({ ...next, shown: undefined }),
  };
  const found = list.data?.kind === "found" ? list.data.list : null;
  const failed = config.isError || names.isError || (list.isError && !list.isPlaceholderData);
  const snapshot = found?.snapshot ?? latestSnapshot(queryClient);

  if (signedOut) {
    return null;
  }

  const thisWeekTag = params.this_week === true ? <ConditionTag onRemove={() => state.apply({ ...params, this_week: undefined })} /> : null;
  const clearAll = hasConditions(params) ? (
    <button
      type="button"
      onClick={() => state.apply(clearConditions(params))}
      className="-my-[6px] ml-2 inline-flex min-h-tap-target cursor-pointer items-center bg-transparent p-[0px] text-s13 tracking-004 text-accent-gold decoration-1 underline-offset-4 hover:underline"
    >
      {t("search.filter.clearAll")}
    </button>
  ) : null;

  let body: ReactNode;
  if (failed) {
    body = (
      <ErrorBar
        message={tc("states.loadFailed.message")}
        action={tc("states.loadFailed.action")}
        onRetry={() => {
          for (const query of [config, names, list]) {
            if (query.isError) void query.refetch();
          }
        }}
      />
    );
  } else if (found === null || limit === null) {
    body = <SkeletonRows label={t("search.results.loading")} />;
  } else if (start) {
    body = <StartBlock list={found} names={state.names} />;
  } else if (found.total === 0) {
    body = <ZeroBar onClear={() => state.apply(clearConditions(params))} />;
  } else {
    const rows = found.items.slice(0, limit);
    const loadingMore = list.isPlaceholderData && list.isFetching;
    body = (
      <>
        <CountLine total={found.total} thisWeek={params.this_week === true} />
        <PaperRowList rows={rows} names={state.names} />
        {/* 還有沒顯示的（後端說還有下一頁，或已經讀到的比顯示的多）才放「再載入」；total 比較大但沒有下一頁時不放，免得按了沒反應。 */}
        {rows.length < found.total && (found.items.length > rows.length || found.next_cursor !== null) && pageSize !== null ? (
          <More
            shown={rows.length}
            total={found.total}
            pageSize={pageSize}
            loading={loadingMore}
            onMore={() => onChange({ ...params, shown: Math.min(limit + pageSize, MAX_SHOWN) })}
            atLimit={limit >= MAX_SHOWN}
          />
        ) : null}
      </>
    );
  }

  return (
    <div data-page="search" className="text-s14 leading-normal tracking-0 text-text-primary">
      <DataDateBar snapshot={snapshot} />
      <h1 className="mt-[14px] font-title-zh text-page-title-zh text-text-primary split:mt-[18px]">{t("search.page.title")}</h1>
      <div className="mt-6 split:mt-8 split:flex split:flex-wrap split:items-center split:gap-x-[28px] split:gap-y-3">
        <ScopeSwitch params={params} onChange={(scope) => state.apply({ ...params, scope })} />
        <span aria-hidden="true" className="hidden h-[18px] w-px bg-border split:block" />
        <div className="hidden flex-wrap items-center gap-2 split:flex">
          {thisWeekTag}
          <FilterMenus state={state} />
          {clearAll}
        </div>
      </div>
      <MobileFilters state={state} total={found?.total ?? null}>
        {thisWeekTag}
        {/* 手機的「清除篩選」只在篩選選單有選時出現（設計稿 .mclr2）；只有帶進來的條件時，用標籤上的 × 拿掉。 */}
        {appliedFilterCount(params) > 0 ? clearAll : null}
      </MobileFilters>
      {body}
      <NewTabNote />
    </div>
  );
}

/** 範圍：所有追蹤期刊／實驗室文獻庫／我的文獻庫，選中的有金色底線（文字切換，不是篩選選單）。 */
function ScopeSwitch({ params, onChange }: { params: SearchParams; onChange: (scope: SearchParams["scope"]) => void }) {
  const { t } = useTranslation("search");
  const current = params.scope ?? "tracked";
  return (
    <div className="flex flex-wrap gap-x-6">
      {(["tracked", "lab", "personal"] as const).map((scope) => {
        const on = scope === current;
        return (
          // 看得到的高度 32px（設計稿），按鈕本身 44px（ADR-0037），負外距抵掉。
          <button
            key={scope}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(scope === "tracked" ? undefined : scope)}
            className={`-my-[6px] inline-flex h-tap-target cursor-pointer items-center bg-transparent p-[0px] text-nav transition-[color] duration-200 hover:text-text-primary ${
              on ? "text-text-primary" : "text-text-secondary"
            }`}
          >
            <span className={`border-b py-[5px] transition-[border-color] duration-200 ${on ? "border-accent-gold" : "border-transparent"}`}>
              {t(`search.scope.${scope}`)}
            </span>
          </button>
        );
      })}
    </div>
  );
}

/** 從連結帶進來的條件（「本週新增」）：暖白底的標籤，右邊「×」移除。 */
function ConditionTag({ onRemove }: { onRemove: () => void }) {
  const { t } = useTranslation("search");
  return (
    <span
      data-condition="this_week"
      className="inline-flex h-8 items-center rounded-full border border-text-primary bg-text-primary pr-1 pl-[14px] text-s13 leading-normal tracking-004 whitespace-nowrap text-on-button"
    >
      {t("search.condition.thisWeek")}
      {/* 看得到的圓 26px，按鈕 44px（ADR-0037），負外距抵掉。 */}
      <button
        type="button"
        aria-label={t("search.filter.removeCondition")}
        onClick={onRemove}
        className="-my-[9px] -mr-[9px] -ml-[5px] inline-flex size-tap-target cursor-pointer items-center justify-center rounded-full bg-transparent p-[0px] text-on-button"
      >
        <CrossIcon size={9} />
      </button>
    </span>
  );
}

/** 「找到 N 篇」（從「看全部」進來時前面接「本週新增 ·」），右邊「依年份排序」（手機不顯示，設計稿）。 */
function CountLine({ total, thisWeek }: { total: number; thisWeek: boolean }) {
  const { t } = useTranslation("search");
  return (
    <div className="mt-8 flex items-baseline justify-between gap-6 text-s14 leading-170 text-text-secondary split:mt-[44px]">
      <span>
        {thisWeek ? (
          <span>
            <span className="text-text-primary">{t("search.condition.thisWeek")}</span>
            <span className="px-[10px] text-text-tertiary">·</span>
          </span>
        ) : null}
        <span data-result-count className="text-s16 text-text-reading">
          <WithNumber
            template={t("search.results.found")}
            value={formatCount(total)}
            numberClass="font-mono tracking-002 text-text-primary"
          />
        </span>
      </span>
      <span className="hidden shrink-0 split:inline">
        <span className="font-mono text-s13 tracking-006 text-text-secondary">{t("search.results.byYear")}</span>
      </span>
    </div>
  );
}

/** 「再載入 20 篇」與「已顯示 n／N」（手機：按鈕滿寬，數字在下面置中）。 */
function More({
  shown,
  total,
  pageSize,
  loading,
  atLimit,
  onMore,
}: {
  shown: number;
  total: number;
  pageSize: number;
  loading: boolean;
  atLimit: boolean;
  onMore: () => void;
}) {
  const { t } = useTranslation("search");
  return (
    <div className="mt-[28px] split:mt-8 split:flex split:items-center split:gap-6">
      {atLimit ? null : (
        <button
          type="button"
          data-load-more
          disabled={loading}
          aria-busy={loading || undefined}
          onClick={onMore}
          className="inline-flex h-tap-target w-full cursor-pointer items-center justify-center gap-[10px] rounded-full border border-border bg-transparent px-6 text-button text-s14 tracking-006 text-text-primary transition-[border-color] duration-250 enabled:hover:border-text-primary disabled:cursor-default split:w-auto"
        >
          {loading ? t("search.results.loadingMore") : t("search.results.more", { count: pageSize })}
        </button>
      )}
      <div className="mt-3 text-center split:mt-[0px] split:text-left">
        <span data-shown className="font-mono text-s13 tracking-006 text-text-secondary">
          {t("search.results.shown", { shown: formatCount(shown), total: formatCount(total) })}
        </span>
      </div>
    </div>
  );
}

/** 還沒有任何條件：「本週新增」＋「看全部 N 篇」＋前幾篇。本週沒有新論文時只有標題（設計稿沒畫，列進決定紀錄）。 */
function StartBlock({ list, names }: { list: PaperList; names: FilterState["names"] }) {
  const { t } = useTranslation("search");
  const rows = list.items.slice(0, START_ROWS);
  return (
    <section aria-labelledby="search-this-week" data-state="start">
      <div className="mt-[44px] flex flex-wrap items-baseline justify-between gap-4 split:mt-16">
        <h2 id="search-this-week" className="font-title-zh text-section-title-zh text-text-primary max-split:text-s21">
          {t("search.condition.thisWeek")}
        </h2>
        {list.total > 0 ? (
          <Link
            to="/search"
            search={{ this_week: true }}
            resetScroll={false}
            className="-my-2 inline-flex min-h-tap-target items-center text-nav text-accent-gold no-underline decoration-1 underline-offset-4 hover:underline"
          >
            {t("search.start.seeAll", { count: formatCount(list.total) })}
          </Link>
        ) : null}
      </div>
      {rows.length > 0 ? <PaperRowList rows={rows} names={names} /> : null}
    </section>
  );
}

/** 篩到 0 篇（State Patterns「12. 找論文」）：「沒有符合的論文」＋「清除篩選」。 */
function ZeroBar({ onClear }: { onClear: () => void }) {
  const { t } = useTranslation("search");
  return (
    <div role="status" data-state="zero" className="mt-[36px] flex max-w-reading-width items-start gap-[14px] split:mt-12">
      <span className="flex text-text-secondary">
        <NoticeIcon className="mt-[7px]" />
      </span>
      <div>
        <p className="text-s17 leading-170 text-text-primary">{t("search.empty.noMatch")}</p>
        <div className="mt-2">
          <button
            type="button"
            onClick={onClear}
            className="-my-1 inline-flex min-h-tap-target cursor-pointer items-center bg-transparent p-[0px] text-nav text-accent-gold decoration-1 underline-offset-4 hover:underline"
          >
            {t("search.filter.clearAll")}
          </button>
        </div>
      </div>
    </div>
  );
}

/** 錯誤（State Patterns「12. 找論文」）：「沒有載入成功。重新整理後再試」＋「重新整理」（重讀，條件留著）。 */
function ErrorBar({ message, action, onRetry }: { message: string; action: string; onRetry: () => void }) {
  return (
    <div role="alert" data-state="error" className="mt-[36px] flex max-w-reading-width flex-wrap items-center gap-x-[14px] gap-y-[10px] split:mt-12">
      <span className="flex text-danger">
        <NoticeIcon />
      </span>
      <p className="text-s17 leading-170 text-text-primary">{message}</p>
      <div className="ml-[10px]">
        {/* 看得到的按鈕 36px 高（設計稿），按鈕本身 44px（ADR-0037），負外距抵掉。 */}
        <button type="button" onClick={onRetry} className="group -my-1 inline-flex h-tap-target cursor-pointer items-center bg-transparent p-[0px]">
          <span className="inline-flex h-[36px] items-center rounded-full border border-border px-[20px] text-s14 tracking-006 text-text-primary transition-[border-color] duration-250 group-hover:border-text-primary">
            {action}
          </span>
        </button>
      </div>
    </div>
  );
}

