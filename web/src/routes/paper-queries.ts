// 論文頁與找論文共用的查詢（Story 3.8、3.14）：資料都經 datasource（ADR-0010 第 1 點），快取規則寫在這裡。
import { queryOptions, type QueryClient } from "@tanstack/react-query";

import { datasource } from "../datasource";
import type { PaperList, PaperListFilters, PaperSource } from "../datasource/types";
import { MAX_SHOWN } from "../pages/search-filters";

/** 成員版才有的論文讀取函式；示範站的論文頁是 Story 3.9。 */
export function paperSource(): PaperSource {
  const source = datasource.papers;
  if (source === null) {
    throw new Error("paper source is only available in the member build");
  }
  return source;
}

// 顯示名稱是設定值，同一位成員登入期間不會變：讀到就留著（登出、換人時 clearMemberQueries 會清掉）。
// 路由先讀一次；畫面掛上時不再自動重讀，沒讀到也不自動重試（按「重試」才讀）。
export const displayNamesQuery = queryOptions({
  queryKey: ["display-names"],
  queryFn: () => paperSource().displayNames(),
  retry: false,
  staleTime: Infinity,
  retryOnMount: false,
});

// 設定值（一頁幾篇）同樣登入期間不會變。
export const clientConfigQuery = queryOptions({
  queryKey: ["client-config"],
  queryFn: () => paperSource().clientConfig(),
  retry: false,
  staleTime: Infinity,
  retryOnMount: false,
});

/** 清單的資料每週才更新一次：5 分鐘內回到同一組條件直接用快取。 */
const LIST_STALE_MS = 5 * 60 * 1000;
/**
 * 清單沒有畫面在用之後，快取留多久：從論文頁按上一頁回來時還在，停在原本的位置（AD-21；審查 S1-7）。
 * 預設的 5 分鐘太短，看一篇論文就可能超過。
 */
const LIST_GC_MS = 30 * 60 * 1000;

/** 篩選條件的鍵：排好順序的字串（物件當鍵時 TanStack 會做部分比對，不同條件可能被當成同一組）。 */
export function filtersKey(filters: PaperListFilters): string {
  return JSON.stringify(Object.entries(filters).sort(([a], [b]) => a.localeCompare(b)));
}

/**
 * 找論文的清單：同一組條件、要 `limit` 篇。快取裡同一組條件已經有別的篇數的清單（「再載入」之前的那一份、
 * 起始狀態的前 3 篇）時，從讀到最多的那一份接著讀，不重讀前面的（cursor 不進網址，AD-21）。
 * 沒有可以接著讀的（重新整理、直接打開網址）時最多讀 MAX_SHOWN 篇；之後照樣可以再載入（審查 S1-3）。
 */
export function paperListQuery(queryClient: QueryClient, filters: PaperListFilters, limit: number) {
  const key = filtersKey(filters);
  return queryOptions({
    queryKey: ["paper-list", key, limit],
    queryFn: () => {
      let previous: PaperList | null = null;
      const size = (list: PaperList) => list.items.length + list.pending.length;
      for (const [queryKey, data] of queryClient.getQueriesData<{ kind: string; list?: PaperList }>({
        queryKey: ["paper-list", key],
      })) {
        // 自己這一份不算（重讀時要真的重讀）。
        if (queryKey[2] === limit || data?.kind !== "found" || data.list === undefined) continue;
        if (previous === null || size(data.list) > size(previous)) previous = data.list;
      }
      return paperSource().list(filters, previous === null ? Math.min(limit, MAX_SHOWN) : limit, previous);
    },
    retry: false,
    staleTime: LIST_STALE_MS,
    gcTime: LIST_GC_MS,
  });
}
