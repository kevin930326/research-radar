// 加入、移出、復原一篇（Story 3.17；API 是 Story 3.16 的三個端點）：論文頁、找論文、我的文獻庫共用。
// - 成功：快取裡這一篇的「在不在我的文獻庫」跟著改（markInLibrary），回 "done"。
// - 移出時後端回 404（本來就不在文獻庫，例如另一個分頁已經移出）：結果一樣是不在（移出是冪等的），
//   快取照樣改成「不在」，但回 "alreadyGone"：這一次沒有真的移出，不能給「復原」（復原會拿到別次移出的封存，
//   ADR-0049 復原取最新一筆；審查 F3）。
// - 401：照登入過期處理（清快取、回登入頁），回 "failed"。清快取前後都再確認還是同一位（審查 G1）。
// - 其他失敗（加入／復原的 404、網路、伺服器錯誤）：回 "failed"，畫面回到按之前的樣子。
//   State Patterns 沒有「加入／移出失敗」的句子，這一輪不顯示新的文字（列給 Kevin 決定，3.17 決定紀錄）。
// - 送出之後才登出、登入過期或換了人（快取清過，世代號不同）：回應是上一位的，不改快取、回 "failed"（審查 F2）。
//   寫快取前還要等取消進行中的清單讀取，那段期間換人也一樣（審查 G1）。
// - 每次寫入都帶「這個分頁以為的成員」＝畫面上那份資料是用誰的身分讀的（資料的 readAs：讀取時帶的預期成員，後端比對過；
//   ADR-0055），不是「路由現在認定的人」：路由的 context 可能比畫面上的資料早換成下一位（審查 fix1 H1、H2）。
//   另一個分頁換了人時後端什麼都不寫、回 409：不重試、回 "failed"（按鈕不留在等待中），照「發現換人」處理——
//   清掉上一位的快取、整頁換成現在登入的人（followMemberChange）。讀取回 409 也走同一個流程（useFollowMemberChange）。
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate, useRouter } from "@tanstack/react-router";
import { createContext, useCallback } from "react";
import { flushSync } from "react-dom";

import type { LibrarySource, Member, PaperPage, ReadAs } from "../datasource/types";
import { librarySource, markInLibrary } from "./paper-queries";
import { clearMemberQueries, loadMember, meQuery, memberGeneration, noteMemberSwap, pendingMemberCheck } from "./session";

export type LibraryAction = "add" | "remove" | "restore";
export type LibraryChangeOutcome = "done" | "alreadyGone" | "failed";

/**
 * 送出一次加入、移出或復原，照結果改快取。不丟例外。
 * 世代號（換人就加 1）在三個地方比對（審查 F2、G1）：回應回來時、真的寫快取之前（中間要等取消進行中的清單讀取）、
 * 處理 401 之前與之後（清快取要等）。任何一次和送出時不同，表示中間登出、登入過期或換了人：回應是上一位的，
 * 不改快取、不清快取、不回登入頁，回 "failed"。
 * 401 而且還是同一位：清快取，再呼叫 `onSignedOut`（回登入頁）；清的期間又換了人（例如有人登入）就不呼叫。
 * `member.expectedMemberId`：這個分頁以為的成員，交給 `source` 放進請求標頭（ADR-0055）。後端說換了人（memberChanged）而且
 * 還是同一代：呼叫 `member.onMemberChanged`（不等它做完，按鈕馬上恢復）、回 "failed"；不重試、不改快取。
 */
export async function sendLibraryChange(
  queryClient: QueryClient,
  source: LibrarySource,
  paperId: string,
  action: LibraryAction,
  onSignedOut: () => Promise<void> | void = () => {},
  member: { expectedMemberId?: string; onMemberChanged?: () => Promise<void> | void } = {},
): Promise<LibraryChangeOutcome> {
  const generation = memberGeneration(queryClient);
  const stillCurrent = () => memberGeneration(queryClient) === generation;
  // 找論文切回視窗、正在確認登入的人：等結果，換了人（或沒有人）就不送，畫面上的是上一位的清單（PR #114 確認審查 D1）。
  // 沒有進行中的確認時不等（不多一個 await，其他時候的行為不變）。
  const check = pendingMemberCheck(queryClient);
  if (check !== undefined && (await check) === "changed") {
    return "failed";
  }
  let result;
  try {
    result = await source[action](paperId, member.expectedMemberId);
  } catch {
    return "failed";
  }
  if (!stillCurrent()) {
    return "failed";
  }
  if (result.kind === "memberChanged") {
    void member.onMemberChanged?.();
    return "failed";
  }
  if (result.kind === "signedOut") {
    await clearMemberQueries(queryClient);
    // clearMemberQueries 一開始把世代號加 1；清完之後還是這一代才表示中間沒有別人登入。
    if (memberGeneration(queryClient) === generation + 1) {
      await onSignedOut();
    }
    return "failed";
  }
  if (result.kind === "notFound") {
    if (action !== "remove") return "failed";
    return (await markInLibrary(queryClient, paperId, false, stillCurrent)) ? "alreadyGone" : "failed";
  }
  return (await markInLibrary(queryClient, paperId, action !== "remove", stillCurrent)) ? "done" : "failed";
}

/** 進行中的「整頁換成現在的人」（同時收到好幾個 409 時只做一次）。 */
const following = new WeakMap<QueryClient, Promise<void>>();

/**
 * 寫入時後端說換了人（409 member_changed）之後：照「發現換人」處理（和找論文切回視窗確認時一樣，PR #114）。
 * 1. 問一次「我是誰」（loadMember；換了人它會清掉上一位的查詢快取）。loadMember 沒有清（快取裡的已經是現在的人、或問不到）
 *    也照樣清：後端已經說了這一頁是上一位的。
 * 2. 沒有人：回登入頁並帶「登入已經過期」（`toLogin`）。
 * 3. 有人（或問不到）：找論文、我的文獻庫同步重新掛上（noteMemberSwap；上一位的列馬上離開畫面），再 `reload`
 *    （重跑路由的登入檢查與資料讀取：導覽列、論文頁換成現在的人；問不到就是全站共用的「載入失敗」）。
 * 回傳的 promise 不會 reject（`reload`／`toLogin` 失敗也收掉：畫面已經在換，沒有人能處理這個錯誤）；結束（成功或失敗）
 * 就拿掉進行中的那一次，下一個 409 會重新開始（審查 fix1 L3）。
 */
export function followMemberChange(
  queryClient: QueryClient,
  actions: { toLogin: () => Promise<void> | void; reload: () => Promise<void> | void },
): Promise<void> {
  const running = following.get(queryClient);
  if (running !== undefined) return running;
  const job = (async () => {
    const generation = memberGeneration(queryClient);
    let member: Member | null | undefined;
    try {
      ({ member } = await loadMember(queryClient));
    } catch {
      member = undefined;
    }
    if (memberGeneration(queryClient) === generation) {
      await clearMemberQueries(queryClient);
      if (member != null) queryClient.setQueryData(meQuery.queryKey, member);
    }
    if (member === null) {
      await actions.toLogin();
      return;
    }
    flushSync(() => noteMemberSwap(queryClient));
    await actions.reload();
  })()
    .catch(() => {})
    .finally(() => following.delete(queryClient));
  following.set(queryClient, job);
  return job;
}

/**
 * 找論文的列（PaperRowList 底下的 LibraryToggle）用的「這幾列是用誰的身分讀的」：找論文把清單資料的 `readAs` 放進來。
 * 沒有放（undefined）時列上的寫入不送（useLibraryChange）。
 */
export const RowsReadAs = createContext<ReadAs>(undefined);

/**
 * 回一個函式：讀取或寫入時後端說換了人（409 member_changed，ADR-0055）之後，照「發現換人」處理（followMemberChange）：
 * 沒有人就回登入頁並帶「登入已經過期」，有人就整頁換成現在的人。
 */
export function useFollowMemberChange(): () => Promise<void> {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const router = useRouter();
  const href = useLocation({ select: (location) => location.href });
  return useCallback(
    () =>
      followMemberChange(queryClient, {
        toLogin: async () => {
          await navigate({ to: "/login", search: { next: href, expired: true as const }, replace: true });
        },
        reload: () => router.invalidate(),
      }),
    [queryClient, navigate, router, href],
  );
}

/**
 * 回一個函式：對 `paperId` 做 `action`，回 "done"、"alreadyGone"（只有移出）或 "failed"（不丟例外）。401 時回登入頁。
 * `readAs`：畫面上那份資料是用誰的身分讀的（論文頁的 loader、清單的查詢結果記下的；ADR-0055）。請求帶它當預期成員，
 * 所以畫面上是上一位的資料時，按下去一定是 409、不會寫進現在登入的人的文獻庫；後端說換了人時整頁換成現在的人。
 * 沒有 `readAs`（資料不是帶預期成員讀的；成員版沒有這種畫面）就不送、回 "failed"：不知道這份資料是誰的，不寫。
 */
export function useLibraryChange(readAs: ReadAs): (paperId: string, action: LibraryAction) => Promise<LibraryChangeOutcome> {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const href = useLocation({ select: (location) => location.href });
  const follow = useFollowMemberChange();
  return useCallback(
    (paperId, action) => {
      if (readAs === undefined) return Promise.resolve("failed" as const);
      const toLogin = async () => {
        await navigate({ to: "/login", search: { next: href, expired: true as const }, replace: true });
      };
      return sendLibraryChange(queryClient, librarySource(), paperId, action, toLogin, {
        expectedMemberId: readAs,
        onMemberChanged: follow,
      });
    },
    [queryClient, navigate, href, readAs, follow],
  );
}

/**
 * 論文頁顯示「在不在我的文獻庫」：按過加入或移出、而且還是同一份論文頁資料時照按的結果，否則照 API 的
 * `in_my_library`（路由換了一篇或重讀了，就是新的一份；審查 F9）。
 */
export function libraryShown(page: PaperPage, changed: { page: PaperPage; inLibrary: boolean } | null): boolean {
  return changed !== null && changed.page === page ? changed.inLibrary : page.in_my_library;
}
