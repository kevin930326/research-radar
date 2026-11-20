// 加入、移出、復原一篇（Story 3.17；API 是 Story 3.16 的三個端點）：論文頁、找論文、我的文獻庫共用。
// - 成功：快取裡這一篇的「在不在我的文獻庫」跟著改（markInLibrary），回 "done"。
//   移出時後端回 404（本來就不在文獻庫）也算 "done"：結果一樣是不在（移出是冪等的）。
// - 401：照登入過期處理（清快取、回登入頁），回 "failed"。
// - 其他失敗（404、網路、伺服器錯誤）：回 "failed"，畫面回到按之前的樣子。
//   State Patterns 沒有「加入／移出失敗」的句子，這一輪不顯示新的文字（列給 Kevin 決定，3.17 決定紀錄）。
import { useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "@tanstack/react-router";
import { useCallback } from "react";

import { librarySource, markInLibrary } from "./paper-queries";
import { clearMemberQueries } from "./session";

export type LibraryAction = "add" | "remove" | "restore";
export type LibraryChangeOutcome = "done" | "failed";

/** 回一個函式：對 `paperId` 做 `action`，回 "done" 或 "failed"（不丟例外）。 */
export function useLibraryChange(): (paperId: string, action: LibraryAction) => Promise<LibraryChangeOutcome> {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const href = useLocation({ select: (location) => location.href });
  return useCallback(
    async (paperId, action) => {
      let result;
      try {
        result = await librarySource()[action](paperId);
      } catch {
        return "failed";
      }
      if (result.kind === "signedOut") {
        await clearMemberQueries(queryClient);
        await navigate({ to: "/login", search: { next: href, expired: true as const }, replace: true });
        return "failed";
      }
      if (result.kind === "notFound" && action !== "remove") {
        return "failed";
      }
      markInLibrary(queryClient, paperId, action !== "remove");
      return "done";
    },
    [queryClient, navigate, href],
  );
}
