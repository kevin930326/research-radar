// 論文頁成員版的小表單送出（修正分類 Story 3.11、回報錯誤 Story 3.12）共用的步驟。規則照我的文獻庫的加入／移出
// （library-change.ts，ADR-0055）：
// - 一律帶「這個分頁以為的成員」＝畫面上那份論文頁資料是用誰的身分讀的（`readAs`）。後端說換了人（409）時什麼都沒寫：
//   不重試、回 null（表單照「沒有送出成功」留著選的與寫的），照「發現換人」處理（followMemberChange）。
// - 401：清快取、回登入頁，回 null。清快取前後都比對世代號（換人就加 1），中間換了人就不回登入頁。
// - 422（後端不收）、404（看不到）、網路或伺服器錯誤：回 null，畫面顯示「沒有送出成功」。
// - 成功：`apply` 改快取（在比對過世代號之後、同一個步驟裡），回它的結果。回應回來時世代號不同（送出之後換了人）
//   就回 null，不改快取。
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate, useRouter } from "@tanstack/react-router";
import { useCallback } from "react";

import type { Member, ReadAs } from "../datasource/types";
import { alreadyFollowed, useFollowMemberChange } from "./library-change";
import { clearMemberQueries, memberGeneration, pendingMemberCheck } from "./session";

/** 寫入的結果（datasource 的 ClassificationFixResult、SummaryReportResult 都是這個形狀）。 */
export type PaperWriteResult<T> =
  | ({ kind: "done" } & T)
  | { kind: "rejected" }
  | { kind: "notFound" }
  | { kind: "signedOut" }
  | { kind: "memberChanged" };

/** 預期成員與換人時要做的事（同 sendLibraryChange；這裡一定有預期成員）。 */
export interface PaperWriteMember {
  expectedMemberId: string;
  routeMemberId?: () => string | undefined;
  onMemberChanged: () => Promise<void> | void;
}

/** 一次寫入：`write` 帶預期成員呼叫 datasource；`apply` 在成功時改快取並回畫面要的結果。 */
export interface PaperWrite<T, R> {
  write: (expectedMemberId: string) => Promise<PaperWriteResult<T>>;
  apply: (done: T, expectedMemberId: string) => R;
}

/** 送出一次寫入，照檔頭的規則處理結果。不丟例外；成功回 `apply` 的結果，其他回 null。 */
export async function sendPaperWrite<T, R>(
  queryClient: QueryClient,
  { write, apply }: PaperWrite<T, R>,
  onSignedOut: () => Promise<void> | void,
  member: PaperWriteMember,
): Promise<R | null> {
  const generation = memberGeneration(queryClient);
  const stillCurrent = () => memberGeneration(queryClient) === generation;
  const check = pendingMemberCheck(queryClient);
  if (check !== undefined && (await check) === "changed") {
    return null;
  }
  let result;
  try {
    result = await write(member.expectedMemberId);
  } catch {
    return null;
  }
  if (!stillCurrent()) {
    return null;
  }
  switch (result.kind) {
    case "memberChanged":
      // 換人流程已經做完之後才回來的舊回應不再跑一次（同 sendLibraryChange）。
      if (!alreadyFollowed(queryClient, member.expectedMemberId, member.routeMemberId?.())) void member.onMemberChanged();
      return null;
    case "signedOut":
      await clearMemberQueries(queryClient);
      // clearMemberQueries 一開始把世代號加 1；清完之後還是這一代才表示中間沒有別人登入。
      if (memberGeneration(queryClient) === generation + 1) {
        await onSignedOut();
      }
      return null;
    case "rejected":
    case "notFound":
      return null;
    case "done":
      return apply(result, member.expectedMemberId);
  }
}

/**
 * 回一個函式：用 `readAs` 當預期成員送出一次寫入，401 時回登入頁、409 時照「發現換人」處理。`readAs` 見 useLibraryChange：
 * 沒有 `readAs` 就不送、回 null（不知道畫面上的資料是誰的，不寫）。
 */
export function usePaperWrite(readAs: ReadAs): <T, R>(request: PaperWrite<T, R>) => Promise<R | null> {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const router = useRouter();
  const href = useLocation({ select: (location) => location.href });
  const follow = useFollowMemberChange();
  return useCallback(
    <T, R>(request: PaperWrite<T, R>): Promise<R | null> => {
      if (readAs === undefined) return Promise.resolve(null);
      const toLogin = async () => {
        await navigate({ to: "/login", search: { next: href, expired: true as const }, replace: true });
      };
      return sendPaperWrite(queryClient, request, toLogin, {
        expectedMemberId: readAs,
        routeMemberId: () => (router.state.matches.at(-1)?.context as { member?: Member } | undefined)?.member?.member_id,
        onMemberChanged: follow,
      });
    },
    [queryClient, navigate, router, href, readAs, follow],
  );
}
