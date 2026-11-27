import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "@tanstack/react-router";
import { useEffect, useRef, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { DataDateBar } from "../components/DataDateBar";
import { NewTabNote } from "../components/ExternalArrow";
import { focusFilters, type FilterState } from "../components/search/filters";
import { FilterMenus } from "../components/search/FilterMenus";
import { MobileFilters } from "../components/search/MobileFilters";
import { PaperRowList, SkeletonRows } from "../components/search/PaperRows";
import { AnyMatchLink, ConditionTag, ErrorBar, More } from "../components/search/ListParts";
import { NoticeIcon } from "../components/search/parts";
import type { PaperList } from "../datasource/types";
import type { SnapshotRef } from "../generated/model";
import { RowsReadAs } from "../routes/library-change";
import { clientConfigQuery, displayNamesQuery, filtersKey, hasHeldRows, libraryCountQuery, paperListKey, paperListQuery } from "../routes/paper-queries";
import { formatCount, splitTemplate } from "./paper-format";
import {
  START_ROWS,
  anyMatch,
  appliedFilterCount,
  clearConditions,
  currentTaipeiYear,
  hasAllMatch,
  hasConditions,
  isStart,
  joinKeys,
  hasStaleMatch,
  knownOnly,
  selectedKeys,
  tidyMatchKnown,
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
// - 從論文頁的方法標籤帶進來的方法、「看全部」帶進來的本週新增：可以移除的條件標籤（EXPERIENCE.md 畫面 8 第 2 點、畫面 12 第 6 點）。
// - 網址上不認得的期刊、主題、方法代號不送出（後端會回 422）：等顯示名稱讀到、拿掉不認得的再查清單。
// - 載入中：6 列骨架，篩選列照樣可以改；錯誤：訊息條＋「重新整理」（條件留著）；篩到 0 篇：「沒有符合的論文」＋「清除篩選」。
// - 任何一個 API 回 401：照登入過期處理（由路由清快取、回登入頁）。
// 頁面最外層把字級、行高、字距設成設計稿 .pp 的樣子（14px、瀏覽器預設行高、沒有字距、最亮的字色），區塊只寫不一樣的地方。
// 頁尾在 Story 3.15（Kevin 2026-11-16 決定），導覽列其他連結的目的地還沒做（AR-67）。

interface SearchPageProps {
  /** 網址上的條件（路由驗證過的）。 */
  params: SearchParams;
  /** 換條件（寫回網址）：傳「怎麼從目前網址上的條件算出下一組」。 */
  onUpdate: (change: (current: SearchParams) => SearchParams) => void;
  /** 任何一個 API 回 401。 */
  onSignedOut: () => void;
  /** 這一頁認定的成員（路由的 context）：成員專屬的讀取都帶它當預期成員（ADR-0055）。 */
  memberId: string;
  /** 成員專屬的讀取回 409 member_changed（另一個分頁換了人；ADR-0055）。 */
  onMemberChanged: () => void | Promise<void>;
  /** 換人進行中（路由還沒換成新的人）：成員專屬的資料先不讀，清單是骨架（審查 fix2 N1）。 */
  paused?: boolean;
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

export function SearchPage({ params: addressParams, onUpdate, onSignedOut, memberId, onMemberChanged, paused = false }: SearchPageProps) {
  const { t } = useTranslation("search");
  const { t: tc } = useTranslation("common");
  const { t: tp } = useTranslation("paper");
  const queryClient = useQueryClient();
  const currentYear = currentTaipeiYear();
  const config = useQuery(clientConfigQuery);
  const names = useQuery(displayNamesQuery);
  const pageSize = config.data?.kind === "found" ? config.data.config.paper_list_page_size : null;
  const namesData = names.data?.kind === "found" ? names.data.names : null;
  // 網址上的條件拿掉不認得的代號（顯示名稱還沒讀到時是 null：先不查清單）。
  // 網址上沒有期刊、主題、方法代號時不用等顯示名稱，和顯示名稱同時查（確認審查 N9）。
  const hasKeys = [addressParams.journal, addressParams.topic, addressParams.method].some((value) => value !== undefined);
  const known = namesData !== null ? knownOnly(addressParams, namesData) : hasKeys ? null : addressParams;
  const params = known ?? addressParams;
  const start = isStart(params);
  const filters = toApiParams(start ? { this_week: true } : params);
  const limit = pageSize === null ? null : start ? START_ROWS : Math.max(params.shown ?? pageSize, pageSize);
  const key = filtersKey(filters);
  const list = useQuery({
    ...paperListQuery(queryClient, filters, limit ?? 0, memberId),
    enabled: limit !== null && known !== null && !paused,
    // 「再載入」（同一組條件、多要一頁）時，新的一頁來之前先留著已經顯示的論文；換條件時不留（顯示骨架）。
    placeholderData: (previous, previousQuery) => (previousQuery?.queryKey[1] === key ? previous : undefined),
  });

  // 換了條件：別組條件（範圍我的文獻庫）裡有本頁移出暫留列的那幾份丟掉。換回來時從伺服器重讀，不先把舊的那一份（移出的列還在、
  // 篇數是移出前的）秀出來再換掉（PR #114 審查 R1；Kevin 2026-11-20 決定「換條件後才照伺服器」）。從論文頁按上一頁回來不算換條件，留著。
  // 頁面一掛上也做一次（不只在頁面上換條件時）：到別頁再回來時條件可能已經不同（例如導覽列的「找論文」是預設條件），
  // 上一組條件的暫留清單還在快取裡，之後換回去會先閃出舊的那一份（確認審查 C1）。
  useEffect(() => {
    queryClient.removeQueries({ queryKey: ["paper-list"], predicate: (query) => query.queryKey[1] !== key && hasHeldRows(query) });
  }, [key, queryClient]);

  const found = list.data?.kind === "found" ? list.data.list : null;
  // 範圍是我的文獻庫、沒有條件、0 篇：再讀一次文獻庫的篇數，真的是 0 才說「還沒有論文」；文獻庫有論文、
  // 但都不在找論文的範圍裡（例如被排除）時照一般的「沒有符合的論文」（F11，Kevin 2026-11-20）。
  const personalZero = !start && found !== null && found.total === 0 && params.scope === "personal" && !hasConditions(params);
  const libraryCount = useQuery({ ...libraryCountQuery(memberId), enabled: personalZero && !paused });
  // 同時符合查到 0 篇：主題與方法都改回任一個、其他條件不變時有幾篇（「改看符合任一個的 N 篇」；Story 3.20）。
  // 向 API 再查一次、只用 total（讀一頁；同一組條件之前讀過就接著用那一份，不再打 API）。查失敗、登入過期、換了人
  // 都只是不顯示這個連結，不另外報錯（清單本身的查詢照常處理那幾種情況）。
  const allZero = !start && !personalZero && found !== null && found.total === 0 && hasAllMatch(params);
  const anyList = useQuery({
    ...paperListQuery(queryClient, toApiParams(anyMatch(params)), 1, memberId),
    enabled: allZero && known !== null && !paused,
  });
  const anyCount = allZero && anyList.data?.kind === "found" ? anyList.data.list.total : null;
  // N 查完了（拿到或失敗）：e2e 用 data-any-match-checked 等它，才確認得了「沒有連結」不是還沒查完（審查 B 的 F5）。
  const anyChecked = allZero && anyList.status !== "pending";
  const queries = personalZero ? [config, names, list, libraryCount] : [config, names, list];

  // 直接打開（重新整理）網址時最多讀 MAX_SHOWN 篇（paperListQuery）：網址的 shown 比讀到的多、後面還有的時候，
  // 把 shown 對齊成讀到的篇數。不對齊的話 shown 剛好是 MAX_SHOWN＋一頁時，「再載入」算出來的篇數就是現在的 shown，
  // 網址不變、什麼都不會發生（同我的文獻庫的審查 G8）。對齊後的清單從讀到的那一份接著用，不重讀；對齊之後把原本那一份
  // （只讀到 MAX_SHOWN 篇、卻記在較大的篇數底下）丟掉，之後再載入到那個篇數時才會從對齊後的那一份接著讀。
  // 注意：這兩個 effect 要寫在 useQuery(list) 後面，新的查詢才會先從舊的那一份接著讀、之後才刪舊的（同 LibraryPage）。
  const alignedFrom = useRef<{ key: string; limit: number } | null>(null);
  useEffect(() => {
    const from = alignedFrom.current;
    if (from === null || (from.key === key && from.limit === limit)) return;
    queryClient.removeQueries({ queryKey: paperListKey(from.key, from.limit, memberId), exact: true });
    alignedFrom.current = null;
  }, [key, limit, queryClient, memberId]);
  const capped =
    !start &&
    found !== null &&
    !list.isPlaceholderData &&
    limit !== null &&
    found.items.length < limit &&
    (found.pending.length > 0 || found.next_cursor !== null)
      ? found.items.length
      : null;
  useEffect(() => {
    if (capped === null || limit === null) return;
    alignedFrom.current = { key, limit };
    // 只在網址還是這一份的篇數時改：這段期間換了條件（shown 已清掉）就不動。
    onUpdate((current) => (current.shown !== undefined && current.shown > capped ? { ...current, shown: capped } : current));
  }, [capped, key, limit, onUpdate]);

  const signedOut = [config.data, names.data, list.data, libraryCount.data].some((data) => data?.kind === "signedOut");
  // 只通知一次：換到登入頁的途中這一頁還會再畫幾次（onSignedOut 每次都是新的函式），
  // 再通知一次會用登入頁自己的網址當 next，回不到這一頁。
  const notified = useRef(false);
  useEffect(() => {
    if (!signedOut || notified.current) return;
    notified.current = true;
    onSignedOut();
  }, [signedOut, onSignedOut]);
  // 讀取時後端說換了人（409，ADR-0055）：照「發現換人」處理（清快取、整頁換成現在的人）；這段期間清單是骨架。
  // 每一份 409 的結果只處理一次；換人流程收尾時重讀又拿到新的一份 409（又換了人）就再處理一次（審查 fix2 N1）。
  // 「哪一份」用查詢與這次結果的時間（dataUpdatedAt）認，不用結果物件：TanStack Query 的結構共享在內容相同時沿用
  // 同一個物件，重讀又是 { kind: "memberChanged" } 時物件不變，會被當成處理過而停在骨架（審查 fix3 N1）。
  const memberChanged = (
    [
      ["list", list],
      ["count", libraryCount],
    ] as const
  )
    .filter(([, query]) => query.data?.kind === "memberChanged")
    .map(([name, query]) => `${name}:${query.dataUpdatedAt}`)
    .join(" ");
  const followed = useRef(new Set<string>());
  useEffect(() => {
    const fresh = memberChanged.split(" ").filter((id) => id !== "" && !followed.current.has(id));
    if (fresh.length === 0) return;
    for (const id of fresh) followed.current.add(id);
    void onMemberChanged();
  }, [memberChanged, onMemberChanged]);

  // 每一次換條件都拿掉不到兩個的「同時符合」（取消勾選剩一個、移除帶進來的方法時；Story 3.20）。「不到兩個」數的是
  // 拿掉不認得的代號之後的：換之前先整理一次（網址上殘留的不帶進下一組），換完再整理一次（審查 B 的 F1）。
  const update = (change: (current: SearchParams) => SearchParams) =>
    onUpdate((current) => tidyMatchKnown({ ...change(tidyMatchKnown(current, namesData)), shown: undefined }, namesData));
  const state: FilterState = { params, names: namesData, currentYear, update };

  // 打開時網址列上的 *_match 用不到（只有一個 key、不認得的拿掉後不到兩個、看不懂的值）：換掉這一筆網址，
  // 不多一筆上一頁（onUpdate 是 replace；規則 3「不到兩個時網址上不留」，審查 B 的 F2）。
  const rawMatch = useLocation({
    select: (location) => {
      const search = location.search as Record<string, unknown>;
      return { topic_match: search.topic_match, method_match: search.method_match };
    },
  });
  const staleMatch = known !== null && hasStaleMatch(rawMatch, params);
  useEffect(() => {
    if (!staleMatch) return;
    onUpdate((current) => tidyMatchKnown(current, namesData));
  }, [staleMatch, onUpdate, namesData]);
  // 已經有資料時，背景重讀失敗不把整個畫面換成錯誤（審查 S1-5）。
  const failed = queries.some((query) => query.isError && query.data === undefined);
  const snapshot = found?.snapshot ?? latestSnapshot(queryClient);

  if (signedOut) {
    return null;
  }

  // 帶進來的條件標籤：本週新增、方法（一個方法一個標籤）。按 × 之後標籤不見，焦點移到篩選（審查 S2-3）。
  const removeCondition = (change: (current: SearchParams) => SearchParams) => () => {
    focusFilters();
    state.update(change);
  };
  const conditionTags = (
    <>
      {params.this_week === true ? (
        <ConditionTag
          kind="this_week"
          label={t("search.condition.thisWeek")}
          onRemove={removeCondition((current) => ({ ...current, this_week: undefined }))}
        />
      ) : null}
      {/* 顯示名稱讀到前不畫方法標籤：不先出現代號，不認得的代號也不會閃一下（確認審查 N2）。 */}
      {(namesData === null ? [] : selectedKeys(params.method)).map((method) => (
        <ConditionTag
          key={method}
          kind="method"
          label={t("search.condition.named", {
            label: tp("paper.classification.method"),
            value: namesData?.methods.find((entry) => entry.key === method)?.name ?? method,
          })}
          onRemove={removeCondition((current) => ({
            ...current,
            method: joinKeys(selectedKeys(current.method).filter((key) => key !== method)),
          }))}
        />
      ))}
    </>
  );
  // 「清除篩選」是連到「只留範圍」的網址的連結（設計稿）；按了之後它會不見，焦點先移到篩選。
  const clearAll = hasConditions(params) ? (
    <ClearLink params={params} className="-my-[6px] ml-2 text-s13 tracking-004" />
  ) : null;

  let body: ReactNode;
  if (failed) {
    body = (
      <ErrorBar
        message={tc("states.loadFailed.message")}
        action={tc("states.loadFailed.action")}
        onRetry={() => {
          for (const query of queries) {
            if (query.isError) void query.refetch();
          }
        }}
      />
    );
  } else if (found === null || limit === null) {
    body = <SkeletonRows label={t("search.results.loading")} />;
  } else if (start) {
    body = <StartBlock list={found} names={state.names} />;
  } else if (personalZero) {
    // 範圍是我的文獻庫、沒有條件、0 篇：文獻庫真的是空的才是「還沒有論文」（Story 3.17；State Patterns
    // 「12. 找論文」）；文獻庫有論文時是「沒有符合的論文」，沒有條件可以清除（F11）。篇數還沒讀到時照樣是骨架。
    const total = libraryCount.data?.kind === "found" ? libraryCount.data.total : null;
    body =
      total === null ? (
        <SkeletonRows label={t("search.results.loading")} />
      ) : total === 0 ? (
        <LibraryEmptyBar />
      ) : (
        <ZeroBar clear={null} />
      );
  } else if (found.total === 0) {
    // 範圍裡本來就沒有論文、也沒有條件時，沒有東西可以清除。
    body = <ZeroBar clear={hasConditions(params) ? params : null} anyCount={anyCount} anyChecked={anyChecked} />;
  } else {
    const rows = found.items.slice(0, limit);
    // 離線時查詢是「暫停」不是「讀取中」，一樣算在等（審查 S1-13）。
    const loadingMore = list.isPlaceholderData && list.fetchStatus !== "idle";
    body = (
      <>
        <CountLine total={found.total} thisWeek={params.this_week === true} />
        <PaperRowList rows={rows} names={state.names} />
        {/* 還有沒顯示的（後端說還有下一頁，或已經讀到的比顯示的多）才放「再載入」；total 比較大但沒有下一頁時不放，免得按了沒反應。 */}
        {rows.length < found.total &&
        (found.items.length > rows.length || found.pending.length > 0 || found.next_cursor !== null) &&
        pageSize !== null ? (
          <More
            shown={rows.length}
            total={found.total}
            pageSize={pageSize}
            loading={loadingMore}
            onMore={() => onUpdate((current) => ({ ...current, shown: rows.length + pageSize }))}
          />
        ) : null}
      </>
    );
  }

  return (
    <div data-page="search" className="text-s14 leading-normal tracking-0 text-text-primary">
      <DataDateBar snapshot={snapshot} />
      {/* tabIndex：按「改看符合任一個的 N 篇」之後焦點移到這裡（focusHeading）。 */}
      <h1 data-page-heading tabIndex={-1} className="mt-[14px] font-title-zh text-page-title-zh text-text-primary split:mt-[18px]">
        {t("search.page.title")}
      </h1>
      <div className="mt-6 split:mt-8 split:flex split:flex-wrap split:items-center split:gap-x-[28px] split:gap-y-3">
        <ScopeSwitch params={params} onChange={(scope) => state.update((current) => ({ ...current, scope }))} />
        <span aria-hidden="true" className="hidden h-[18px] w-px bg-border split:block" />
        <div className="hidden flex-wrap items-center gap-2 split:flex">
          {conditionTags}
          <FilterMenus state={state} />
          {clearAll}
        </div>
      </div>
      <MobileFilters state={state} total={found?.total ?? null}>
        {conditionTags}
        {/* 手機的「清除篩選」只在篩選選單有選時出現（設計稿 .mclr2）；只有帶進來的條件時，用標籤上的 × 拿掉。 */}
        {appliedFilterCount(params) > 0 ? clearAll : null}
      </MobileFilters>
      {/* 列上的加入／移出帶「這份清單是用誰的身分讀的」（ADR-0055）。 */}
      <RowsReadAs.Provider value={list.data?.kind === "found" ? list.data.readAs : undefined}>{body}</RowsReadAs.Provider>
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

/**
 * 「清除篩選」：連到「只留範圍」的網址（設計稿是連結）。按了之後它會不見，焦點先移到篩選（審查 S2-3）。
 * 看得到的是 13px 的金色字，連結本身 44px 高（ADR-0037），負外距由呼叫端給。
 */
function ClearLink({ params, className }: { params: SearchParams; className: string }) {
  const { t } = useTranslation("search");
  return (
    <Link
      to="/search"
      search={clearConditions(params)}
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
 * 「找到 N 篇」（從「看全部」進來時前面接「本週新增 ·」），右邊「依年份排序」（手機不顯示，設計稿）。
 * 左邊這一段是 h2（樣子不變）：論文標題是 h3，h1 下面不跳一層（審查 S2-15）。
 */
function CountLine({ total, thisWeek }: { total: number; thisWeek: boolean }) {
  const { t } = useTranslation("search");
  return (
    <div className="mt-8 flex items-baseline justify-between gap-6 text-s14 leading-170 text-text-secondary split:mt-[44px]">
      <h2>
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
      </h2>
      <span className="hidden shrink-0 split:inline">
        <span className="font-mono text-s13 tracking-006 text-text-secondary">{t("search.results.byYear")}</span>
      </span>
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

/**
 * 篩到 0 篇（State Patterns「12. 找論文」）：「沒有符合的論文」＋「清除篩選」（沒有條件時不放）。
 * 同時符合查到 0 篇、改回任一個有論文時，「清除篩選」前面多「改看符合任一個的 N 篇」（`anyCount`；Story 3.20）。
 */
function ZeroBar({
  clear,
  anyCount = null,
  anyChecked = false,
}: {
  clear: SearchParams | null;
  anyCount?: number | null;
  /** 「改看符合任一個」的篇數查完了（拿到或失敗）。 */
  anyChecked?: boolean;
}) {
  const { t } = useTranslation("search");
  return (
    <div role="status" data-state="zero" data-any-match-checked={anyChecked ? "" : undefined} className="mt-[36px] flex max-w-reading-width items-start gap-[14px] split:mt-12">
      <span className="flex text-text-secondary">
        <NoticeIcon className="mt-[7px]" />
      </span>
      <div>
        <p className="text-s17 leading-170 text-text-primary">{t("search.empty.noMatch")}</p>
        {clear === null ? null : (
          <div className="mt-2 flex flex-wrap gap-x-[28px]">
            {anyCount !== null && anyCount > 0 ? (
              <AnyMatchLink count={anyCount} target={{ to: "/search", search: { ...anyMatch(clear), shown: undefined } }} />
            ) : null}
            <ClearLink params={clear} className="-my-1 text-nav" />
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * 範圍選我的文獻庫、但文獻庫是空的（State Patterns「12. 找論文」；樣子參考設計稿 search.html「文獻庫是空的」那一條）：
 * 「還沒有論文」＋「改找所有追蹤期刊」。「上傳或匯入」是 Epic 6（AR-67），不放。
 */
function LibraryEmptyBar() {
  const { t } = useTranslation("search");
  return (
    <div role="status" data-state="library-empty" className="mt-[36px] flex max-w-reading-width items-start gap-[14px] split:mt-12">
      <span className="flex text-text-secondary">
        <NoticeIcon className="mt-[7px]" />
      </span>
      <div>
        <p className="text-s17 leading-170 text-text-primary">{t("search.empty.library")}</p>
        <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-3">
          {/* 看得到的 36px（設計稿），連結本身 44px（ADR-0037），負外距抵掉。 */}
          <Link
            to="/search"
            search={{}}
            replace
            resetScroll={false}
            data-to-tracked
            className="-my-1 inline-flex min-h-tap-target items-center text-s14 tracking-004 text-accent-gold no-underline decoration-1 underline-offset-4 hover:underline"
          >
            {t("search.empty.toTracked")}
          </Link>
        </div>
      </div>
    </div>
  );
}
