// 成員版的登入狀態（Story 3.5、ADR-0046）：路由的登入檢查、登入頁、帳號選單共用。
import { queryOptions, type QueryClient } from "@tanstack/react-query";
import { useCallback, useSyncExternalStore } from "react";

import { datasource } from "../datasource";
import type { Member, MemberSession } from "../datasource/types";

/** 成員版才有的登入函式；示範站的建置沒有成員版的頁面，走到這裡就是程式寫錯。 */
export function memberSession(): MemberSession {
  const session = datasource.session;
  if (session === null) {
    throw new Error("member session is only available in the member build");
  }
  return session;
}

/**
 * 「我是誰」：每次進成員版的頁面都重問一次（staleTime 0），登入過期才看得出來；不重試（401 不是暫時的錯）。
 * 快取裡有沒有上一次的成員，用來分辨「登入過期」（本來在站內）和「還沒登入」（直接打開網址）。
 */
export const meQuery = queryOptions({
  queryKey: ["session", "me"],
  queryFn: () => memberSession().me(),
  staleTime: 0,
  // 沒有元件訂閱這筆資料（外框讀的是路由的 context），預設 5 分鐘就會被清掉；
  // 那樣在同一頁停久了才過期的人會被當成「還沒登入」，所以一直留著（登出、401、登入成功時才清，見 clearMemberQueries）。
  gcTime: Infinity,
  retry: false,
});

/** 每個查詢快取「清過幾次」：每次 clearMemberQueries 加 1。 */
const generations = new WeakMap<QueryClient, number>();

/**
 * 換人或沒有人的時候（登出成功、發現登入過期、登入成功）：取消進行中的查詢、清掉所有查詢快取。
 * 成員版的查詢都是某一位成員的資料，下一位登入的人不能看到上一位的（只清「我是誰」不夠）。
 * 先取消再清：還在路上的回應不會在清掉之後又寫回快取。
 */
export async function clearMemberQueries(queryClient: QueryClient): Promise<void> {
  // 世代號先加 1：還在路上的加入、移出、復原（不是查詢，cancelQueries 擋不到）回來時看得出已經換過人（審查 F2）。
  generations.set(queryClient, memberGeneration(queryClient) + 1);
  await queryClient.cancelQueries();
  queryClient.removeQueries();
}

/**
 * 這個查詢快取目前是第幾代（每次 clearMemberQueries 加 1）。送出寫入前記下來、回來時比對：
 * 不一樣表示中間登出、登入過期或換了人，回應是上一位的，不能寫進現在的快取。
 */
export function memberGeneration(queryClient: QueryClient): number {
  return generations.get(queryClient) ?? 0;
}

/** 切回視窗時確認登入的人的結果：還是同一位、換了人或沒有人、問不到。 */
export type MemberCheckVerdict = "same" | "changed" | "unknown";

/** 進行中的「確認登入的人」（找論文有暫留列、切回視窗時；PR #114 確認審查 D1）。 */
const memberChecks = new WeakMap<QueryClient, Promise<MemberCheckVerdict>>();

/** 記下一次進行中的確認；有結果之後拿掉（之後又開始新的一次就留新的那一次）。 */
export function trackMemberCheck(queryClient: QueryClient, verdict: Promise<MemberCheckVerdict>): void {
  memberChecks.set(queryClient, verdict);
  void verdict.then(() => {
    if (memberChecks.get(queryClient) === verdict) memberChecks.delete(queryClient);
  });
}

/** 進行中的確認（沒有就是 undefined）：加入、移出送出前要等它，結果是換了人就不送（D1）。 */
export function pendingMemberCheck(queryClient: QueryClient): Promise<MemberCheckVerdict> | undefined {
  return memberChecks.get(queryClient);
}

/**
 * 寫入時後端說「這個分頁以為的人和現在登入的人不同」（409 member_changed，ADR-0055）之後，這個分頁整頁換成現在的人
 * 幾次：找論文、我的文獻庫拿它當 key，加 1 就重新掛上（快取清掉之後，畫面上的查詢不會自己重讀）。
 */
const memberSwaps = new WeakMap<QueryClient, { count: number; listeners: Set<() => void> }>();

function swapsOf(queryClient: QueryClient): { count: number; listeners: Set<() => void> } {
  let swaps = memberSwaps.get(queryClient);
  if (swaps === undefined) {
    swaps = { count: 0, listeners: new Set() };
    memberSwaps.set(queryClient, swaps);
  }
  return swaps;
}

/** 記一次「整頁換成現在的人」：用 useMemberSwaps 的頁面重新掛上（在 flushSync 裡呼叫就同步重畫）。 */
export function noteMemberSwap(queryClient: QueryClient): void {
  const swaps = swapsOf(queryClient);
  swaps.count += 1;
  for (const listener of swaps.listeners) listener();
}

/** 這個分頁整頁換成現在的人幾次（給頁面當 key）。 */
export function useMemberSwaps(queryClient: QueryClient): number {
  const subscribe = useCallback(
    (listener: () => void) => {
      const swaps = swapsOf(queryClient);
      swaps.listeners.add(listener);
      return () => {
        swaps.listeners.delete(listener);
      };
    },
    [queryClient],
  );
  const read = useCallback(() => swapsOf(queryClient).count, [queryClient]);
  return useSyncExternalStore(subscribe, read, read);
}

/**
 * 每次進成員版的頁面先問「我是誰」。快取裡原本是另一位成員時（例如在另一個分頁登出、換人登入，cookie 換了），
 * 先清掉所有查詢快取再繼續：快取裡的論文列、文獻庫都是上一位的（審查 F1）。`wasSignedIn` 是問之前快取裡有沒有成員。
 */
export async function loadMember(queryClient: QueryClient): Promise<{ member: Member | null; wasSignedIn: boolean }> {
  const previous = queryClient.getQueryData(meQuery.queryKey);
  const member = await queryClient.fetchQuery(meQuery);
  if (member !== null && previous != null && previous.member_id !== member.member_id) {
    await clearMemberQueries(queryClient);
    queryClient.setQueryData(meQuery.queryKey, member);
  }
  return { member, wasSignedIn: previous != null };
}

/** 登入後的第一頁：找論文（Story 3.14 起；之前是使用須知，AR-67；系統首頁做好後改成系統首頁）。 */
export const FIRST_PAGE = "/search";

const NEXT_MAX_LENGTH = 2048;
const LOGIN_PATH = "/login";

/**
 * 登入後要回去的路徑：只收站內路徑（和後端 `safe_next_path` 同一套規則：`/` 開頭、不是 `//`、沒有反斜線、
 * 只有可印出的 ASCII、不超過 2048 字），另外不收登入頁自己（免得登入後又回到登入頁）。不合格回 undefined。
 */
export function safeNextPath(value: unknown): string | undefined {
  if (typeof value !== "string" || value === "" || value.length > NEXT_MAX_LENGTH) {
    return undefined;
  }
  if (!value.startsWith("/") || value.startsWith("//") || value.includes("\\")) {
    return undefined;
  }
  if (![...value].every((char) => char >= "\x21" && char <= "\x7e")) {
    return undefined;
  }
  // 路由比對不分大小寫、會解開 %xx，所以這裡也一樣（/Login、/%6Cogin 也是登入頁）。
  let pathname: string;
  try {
    pathname = decodeURIComponent(new URL(value, "https://site.invalid").pathname).toLowerCase();
  } catch {
    return undefined;
  }
  if (pathname === LOGIN_PATH || pathname.startsWith(`${LOGIN_PATH}/`)) {
    return undefined;
  }
  return value;
}

/** 登入頁的網址參數。 */
export interface LoginSearch {
  /** 登入後要回去的站內路徑。 */
  next?: string;
  /** 本來在站內、登入過期才被送回來（顯示「登入已經過期，請再登入一次」）。 */
  expired?: true;
  /** 開發環境用：要用哪一位測試成員登入（成員 ID）；沒給就用假登入清單的第一位。 */
  member?: string;
}

export function validateLoginSearch(search: Record<string, unknown>): LoginSearch {
  const next = safeNextPath(search.next);
  const member = typeof search.member === "string" && search.member !== "" ? search.member : undefined;
  // 三個欄位一定要寫出來（不合格就是 undefined）：路由會把網址上原本的參數和這裡的結果合在一起，
  // 少寫的欄位會留下網址上沒檢查過的值（例如站外的 next）。
  return {
    next,
    expired: search.expired === true ? true : undefined,
    member,
  };
}
