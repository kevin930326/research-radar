// 修正分類（Story 3.11；API 是 `GET`／`POST /api/v1/papers/{id}/classification-fixes`）：成員版論文頁用。
// 寫入的規則照我的文獻庫的加入／移出（library-change.ts，ADR-0055）：
// - 一律帶「這個分頁以為的成員」＝畫面上那份論文頁資料是用誰的身分讀的（`readAs`）。後端說換了人（409）時什麼都沒寫：
//   不重試、回 null（表單照「沒有送出成功」留著選的與寫的），照「發現換人」處理（followMemberChange）。
// - 401：清快取、回登入頁，回 null。清快取前後都比對世代號（換人就加 1），中間換了人就不回登入頁。
// - 422（後端不收）、404（看不到）、網路或伺服器錯誤：回 null，畫面顯示「沒有送出成功。你選的還在，再試一次」。
// - 成功：回修正後的分類區塊與歷史；快取裡這一篇的論文頁換上新的分類區塊、歷史的查詢換成新的（不用再讀一次）。
//   回應回來時世代號不同（送出之後換了人）就不改快取、回 null。
// - 找論文的清單有分類標籤：標成過期，下次打開時重讀（不馬上重讀）。
import { queryOptions, useQueryClient, type QueryClient, type QueryKey } from "@tanstack/react-query";
import { useLocation, useNavigate, useRouter } from "@tanstack/react-router";
import { useCallback } from "react";

import type {
  ClassificationFixBody,
  ClassificationFixes,
  ClassificationFixesResult,
  Member,
  PaperResult,
  PaperSource,
  ReadAs,
} from "../datasource/types";
import { alreadyFollowed, useFollowMemberChange } from "./library-change";
import { paperSource } from "./paper-queries";
import { clearMemberQueries, memberGeneration, pendingMemberCheck } from "./session";

/** 修正歷史的查詢鍵：最後一段是讀的成員（同 paperKey；ADR-0055）。 */
export function classificationFixesKey(paperId: string, memberId?: string): QueryKey {
  return ["classification-fixes", paperId, ...(memberId === undefined ? [] : [memberId])];
}

/** 修正歷史：按「有 N 位成員修正過」才讀；讀過就留著（送出修正時直接換成新的）。 */
export function classificationFixesQuery(paperId: string, memberId?: string) {
  return queryOptions({
    queryKey: classificationFixesKey(paperId, memberId),
    queryFn: (): Promise<ClassificationFixesResult> => paperSource().classificationFixes(paperId, memberId),
    retry: false,
    staleTime: Infinity,
  });
}

/**
 * 送出一次修正分類，照結果改快取。不丟例外；成功回修正後的分類區塊與歷史，其他（見檔頭）回 null。
 * `member` 的欄位同 sendLibraryChange（這裡一定有預期成員）。
 */
export async function sendClassificationFix(
  queryClient: QueryClient,
  source: PaperSource,
  paperId: string,
  body: ClassificationFixBody,
  onSignedOut: () => Promise<void> | void,
  member: { expectedMemberId: string; routeMemberId?: () => string | undefined; onMemberChanged: () => Promise<void> | void },
): Promise<ClassificationFixes | null> {
  const generation = memberGeneration(queryClient);
  const stillCurrent = () => memberGeneration(queryClient) === generation;
  const check = pendingMemberCheck(queryClient);
  if (check !== undefined && (await check) === "changed") {
    return null;
  }
  let result;
  try {
    result = await source.fixClassification(paperId, body, member.expectedMemberId);
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
      break;
  }
  const { fixes } = result;
  queryClient.setQueriesData<PaperResult>({ queryKey: ["papers", paperId] }, (data) =>
    data?.kind === "found" ? { ...data, page: { ...data.page, classification: fixes.classification } } : data,
  );
  const history: ClassificationFixesResult = { kind: "found", fixes, readAs: member.expectedMemberId };
  queryClient.setQueryData(classificationFixesKey(paperId, member.expectedMemberId), history);
  void queryClient.invalidateQueries({ queryKey: ["paper-list"], refetchType: "none" });
  return fixes;
}

/**
 * 回一個函式：對 `paperId` 送出修正，成功回新的分類區塊與歷史，否則回 null。`readAs` 見 useLibraryChange：
 * 沒有 `readAs` 就不送、回 null（不知道畫面上的資料是誰的，不寫）。
 */
export function useSendClassificationFix(
  readAs: ReadAs,
): (paperId: string, body: ClassificationFixBody) => Promise<ClassificationFixes | null> {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const router = useRouter();
  const href = useLocation({ select: (location) => location.href });
  const follow = useFollowMemberChange();
  return useCallback(
    (paperId, body) => {
      if (readAs === undefined) return Promise.resolve(null);
      const toLogin = async () => {
        await navigate({ to: "/login", search: { next: href, expired: true as const }, replace: true });
      };
      return sendClassificationFix(queryClient, paperSource(), paperId, body, toLogin, {
        expectedMemberId: readAs,
        routeMemberId: () => (router.state.matches.at(-1)?.context as { member?: Member } | undefined)?.member?.member_id,
        onMemberChanged: follow,
      });
    },
    [queryClient, navigate, router, href, readAs, follow],
  );
}
