// 論文頁與找論文共用的查詢（Story 3.8、3.14）：資料都經 datasource（ADR-0010 第 1 點），快取規則寫在這裡。
import { queryOptions, type Query, type QueryClient, type QueryKey } from "@tanstack/react-query";

import { datasource } from "../datasource";
import type { LibraryList, LibrarySource, PaperList, PaperListFilters, PaperResult, PaperSource } from "../datasource/types";
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
    queryFn: async () => {
      let previous: PaperList | null = null;
      const size = (list: PaperList) => list.items.length + list.pending.length;
      for (const [queryKey, data] of queryClient.getQueriesData<{ kind: string; list?: PaperList }>({
        queryKey: ["paper-list", key],
      })) {
        // 自己這一份不算（重讀時要真的重讀）。
        if (queryKey[2] === limit || data?.kind !== "found" || data.list === undefined) continue;
        if (previous === null || size(data.list) > size(previous)) previous = data.list;
      }
      const result = await paperSource().list(filters, previous === null ? Math.min(limit, MAX_SHOWN) : limit, previous);
      // 範圍「我的文獻庫」接著讀時：之前移出、留在原地的那幾列（in_my_library 已是 false）還在清單裡，
      // 伺服器的 total 卻不算它們；把它們加回去，「找到 N 篇」「已顯示 n／N」才不會變小、再載入也不會提早消失。
      if (previous !== null && result.kind === "found" && isPersonalList(["paper-list", key])) {
        const carried = [...previous.items, ...previous.pending].filter((row) => !row.in_my_library).length;
        return { ...result, list: { ...result.list, total: result.list.total + carried } };
      }
      return result;
    },
    retry: false,
    staleTime: LIST_STALE_MS,
    gcTime: LIST_GC_MS,
    // 範圍是我的文獻庫的那幾份：移出後只標成過期（markInLibrary），切回視窗、重新連線時也不自動重讀，
    // 移出的那一列才會留在原地到重新整理或換條件（Kevin 2026-11-20 決定）。
    ...(isPersonalList(["paper-list", key]) ? { refetchOnWindowFocus: false, refetchOnReconnect: false } : {}),
  });
}

// --- 我的文獻庫（Story 3.17）-----------------------------------------------------------------------

/** 成員版才有的文獻庫讀寫函式；示範站沒有文獻庫。 */
export function librarySource(): LibrarySource {
  const source = datasource.library;
  if (source === null) {
    throw new Error("library source is only available in the member build");
  }
  return source;
}

/**
 * 我的文獻庫的清單（要 `limit` 篇）。規則同 `paperListQuery`：快取裡有篇數不同的一份就接著讀（「再載入」）。
 * 每次打開這一頁都重讀（`refetchOnMount: "always"`）；頁面開著的時候不自動重讀（切回視窗、重新連線都不讀）：
 * 移出的那一列要留在原地顯示「已移出」＋「復原」，離開頁面後才消失（State Patterns「13. 我的文獻庫」）。
 */
export function libraryListQuery(queryClient: QueryClient, limit: number) {
  return queryOptions({
    queryKey: ["library", limit],
    queryFn: () => {
      let previous: LibraryList | null = null;
      const size = (list: LibraryList) => list.items.length + list.pending.length;
      for (const [queryKey, data] of queryClient.getQueriesData<{ kind: string; list?: LibraryList }>({
        queryKey: ["library"],
      })) {
        if (queryKey[1] === limit || data?.kind !== "found" || data.list === undefined) continue;
        if (previous === null || size(data.list) > size(previous)) previous = data.list;
      }
      return librarySource().list(previous === null ? Math.min(limit, MAX_SHOWN) : limit, previous);
    },
    retry: false,
    staleTime: Infinity,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
}

/** 我的文獻庫的總篇數（`found`）或登入過期（`signedOut`）。 */
export type LibraryCountResult = { kind: "found"; total: number } | { kind: "signedOut" };

/**
 * 我的文獻庫一共幾篇（`GET /api/v1/library` 第一頁的 total；只讀一頁）。找論文範圍選「我的文獻庫」、沒有條件、
 * 0 篇時用它分辨「文獻庫是空的」與「文獻庫有論文、但都不在找論文的範圍裡」（F11，Kevin 2026-11-20）。
 * 鍵不放在 `["library", …]` 底下：那一組是文獻庫清單，`libraryListQuery` 會把同一組的每一份當成可以接著讀的上一份。
 * 每次用到都重讀（`staleTime` 0、`gcTime` 0）：別的分頁、我的文獻庫那一頁改過的篇數不會留著舊的。
 * 只要總篇數，但 API 沒有只回篇數的寫法，照樣讀第一頁（不改後端）。
 */
export const libraryCountQuery = queryOptions({
  queryKey: ["library-count"],
  queryFn: async (): Promise<LibraryCountResult> => {
    const result = await librarySource().list(0);
    return result.kind === "found" ? { kind: "found", total: result.list.total } : result;
  },
  retry: false,
  staleTime: 0,
  // 沒有畫面在用就丟掉：下次用到時不先拿舊的篇數顯示一下才換（審查）。
  gcTime: 0,
});

/**
 * 加入或移出之後，快取裡這一篇的「在不在我的文獻庫」跟著改：論文頁、找論文的清單（每一份）。
 * 找論文的清單正在讀的（「再載入」、重讀）先取消：那一份是改之前讀的（伺服器那一頁也可能在寫入生效前讀），
 * 讀完會把「已加入」蓋回去（審查 F10）。改完之後，畫面正在用的清單重讀一次，以伺服器為準；
 * 範圍是我的文獻庫的那幾份例外，只標成過期（見下面）。沒在用的標成過期，下次打開時重讀。文獻庫清單在下次打開時本來就會重讀；文獻庫篇數（libraryCountQuery）同樣處理。
 * `stillCurrent`：等取消的這段期間可能換了人（登出、登入過期、換人登入），寫快取前再問一次；回 false 就什麼都不寫、
 * 回 false（審查 G1）。回 true 表示寫了。
 */
export async function markInLibrary(
  queryClient: QueryClient,
  paperId: string,
  inLibrary: boolean,
  stillCurrent: () => boolean = () => true,
): Promise<boolean> {
  await queryClient.cancelQueries({ queryKey: ["paper-list"] });
  if (!stillCurrent()) return false;
  queryClient.setQueryData<PaperResult>(["papers", paperId], (data) =>
    data?.kind === "found" ? { ...data, page: { ...data.page, in_my_library: inLibrary } } : data,
  );
  const mark = (rows: PaperList["items"]) =>
    rows.map((row) => (row.id === paperId ? { ...row, in_my_library: inLibrary } : row));
  queryClient.setQueriesData<{ kind: string; list?: PaperList }>({ queryKey: ["paper-list"] }, (data) =>
    data?.kind === "found" && data.list !== undefined
      ? { ...data, list: { ...data.list, items: mark(data.list.items), pending: mark(data.list.pending) } }
      : data,
  );
  // 範圍是我的文獻庫、而且已經有資料的那幾份只標成過期、不馬上重讀：移出的那一列留在原地變回「加入我的文獻庫」、
  // 「找到 N 篇」不變（Kevin 2026-11-20 決定）；換條件、重新整理、之後再回來才照伺服器。沒有資料的（例如剛被取消的
  // 第一次讀取）照常重讀，不然畫面會一直是骨架。
  const keep = (query: Query) => isPersonalList(query.queryKey) && query.state.data !== undefined;
  void queryClient.invalidateQueries({ queryKey: ["paper-list"], refetchType: "none", predicate: keep });
  void queryClient.invalidateQueries({ queryKey: ["paper-list"], refetchType: "active", predicate: (query) => !keep(query) });
  void queryClient.invalidateQueries({ queryKey: ["library-count"], refetchType: "active" });
  return true;
}

/** 這一份找論文的清單是不是範圍「我的文獻庫」（鍵的第二段是 filtersKey）。 */
export function isPersonalList(queryKey: QueryKey): boolean {
  const key = queryKey[1];
  if (typeof key !== "string") return false;
  try {
    const entries = JSON.parse(key) as unknown;
    return Array.isArray(entries) && entries.some((entry) => Array.isArray(entry) && entry[0] === "scope" && entry[1] === "personal");
  } catch {
    return false;
  }
}
