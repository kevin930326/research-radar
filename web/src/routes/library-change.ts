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
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "@tanstack/react-router";
import { useCallback } from "react";

import type { LibrarySource, PaperPage } from "../datasource/types";
import { librarySource, markInLibrary } from "./paper-queries";
import { clearMemberQueries, memberGeneration, pendingMemberCheck } from "./session";

export type LibraryAction = "add" | "remove" | "restore";
export type LibraryChangeOutcome = "done" | "alreadyGone" | "failed";

/**
 * 送出一次加入、移出或復原，照結果改快取。不丟例外。
 * 世代號（換人就加 1）在三個地方比對（審查 F2、G1）：回應回來時、真的寫快取之前（中間要等取消進行中的清單讀取）、
 * 處理 401 之前與之後（清快取要等）。任何一次和送出時不同，表示中間登出、登入過期或換了人：回應是上一位的，
 * 不改快取、不清快取、不回登入頁，回 "failed"。
 * 401 而且還是同一位：清快取，再呼叫 `onSignedOut`（回登入頁）；清的期間又換了人（例如有人登入）就不呼叫。
 */
export async function sendLibraryChange(
  queryClient: QueryClient,
  source: LibrarySource,
  paperId: string,
  action: LibraryAction,
  onSignedOut: () => Promise<void> | void = () => {},
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
    result = await source[action](paperId);
  } catch {
    return "failed";
  }
  if (!stillCurrent()) {
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

/** 回一個函式：對 `paperId` 做 `action`，回 "done"、"alreadyGone"（只有移出）或 "failed"（不丟例外）。401 時回登入頁。 */
export function useLibraryChange(): (paperId: string, action: LibraryAction) => Promise<LibraryChangeOutcome> {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const href = useLocation({ select: (location) => location.href });
  return useCallback(
    (paperId, action) =>
      sendLibraryChange(queryClient, librarySource(), paperId, action, async () => {
        await navigate({ to: "/login", search: { next: href, expired: true as const }, replace: true });
      }),
    [queryClient, navigate, href],
  );
}

/**
 * 論文頁顯示「在不在我的文獻庫」：按過加入或移出、而且還是同一份論文頁資料時照按的結果，否則照 API 的
 * `in_my_library`（路由換了一篇或重讀了，就是新的一份；審查 F9）。
 */
export function libraryShown(page: PaperPage, changed: { page: PaperPage; inLibrary: boolean } | null): boolean {
  return changed !== null && changed.page === page ? changed.inLibrary : page.in_my_library;
}
