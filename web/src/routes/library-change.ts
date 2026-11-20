// 加入、移出、復原一篇（Story 3.17；API 是 Story 3.16 的三個端點）：論文頁、找論文、我的文獻庫共用。
// - 成功：快取裡這一篇的「在不在我的文獻庫」跟著改（markInLibrary），回 "done"。
// - 移出時後端回 404（本來就不在文獻庫，例如另一個分頁已經移出）：結果一樣是不在（移出是冪等的），
//   快取照樣改成「不在」，但回 "alreadyGone"：這一次沒有真的移出，不能給「復原」（復原會拿到別次移出的封存，
//   ADR-0049 復原取最新一筆；審查 F3）。
// - 401：照登入過期處理（清快取、回登入頁），回 "failed"。
// - 其他失敗（加入／復原的 404、網路、伺服器錯誤）：回 "failed"，畫面回到按之前的樣子。
//   State Patterns 沒有「加入／移出失敗」的句子，這一輪不顯示新的文字（列給 Kevin 決定，3.17 決定紀錄）。
// - 送出之後才登出、登入過期或換了人（快取清過，世代號不同）：回應是上一位的，不改快取、回 "failed"（審查 F2）。
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "@tanstack/react-router";
import { useCallback } from "react";

import type { LibrarySource, PaperPage } from "../datasource/types";
import { librarySource, markInLibrary } from "./paper-queries";
import { clearMemberQueries, memberGeneration } from "./session";

export type LibraryAction = "add" | "remove" | "restore";
export type LibraryChangeOutcome = "done" | "alreadyGone" | "failed";

/**
 * 送出一次加入、移出或復原，照結果改快取。不丟例外；401 回 "signedOut"（交給呼叫端回登入頁）。
 * 回應回來時快取已經清過（世代號和送出時不同）就什麼都不改、回 "failed"。
 */
export async function sendLibraryChange(
  queryClient: QueryClient,
  source: LibrarySource,
  paperId: string,
  action: LibraryAction,
): Promise<LibraryChangeOutcome | "signedOut"> {
  const generation = memberGeneration(queryClient);
  let result;
  try {
    result = await source[action](paperId);
  } catch {
    return "failed";
  }
  if (memberGeneration(queryClient) !== generation) {
    return "failed";
  }
  if (result.kind === "signedOut") {
    return "signedOut";
  }
  if (result.kind === "notFound") {
    if (action !== "remove") return "failed";
    await markInLibrary(queryClient, paperId, false);
    return "alreadyGone";
  }
  await markInLibrary(queryClient, paperId, action !== "remove");
  return "done";
}

/** 回一個函式：對 `paperId` 做 `action`，回 "done"、"alreadyGone"（只有移出）或 "failed"（不丟例外）。 */
export function useLibraryChange(): (paperId: string, action: LibraryAction) => Promise<LibraryChangeOutcome> {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const href = useLocation({ select: (location) => location.href });
  return useCallback(
    async (paperId, action) => {
      const outcome = await sendLibraryChange(queryClient, librarySource(), paperId, action);
      if (outcome === "signedOut") {
        await clearMemberQueries(queryClient);
        await navigate({ to: "/login", search: { next: href, expired: true as const }, replace: true });
        return "failed";
      }
      return outcome;
    },
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
