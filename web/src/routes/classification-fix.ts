// 修正分類（Story 3.11；API 是 `GET`／`POST /api/v1/papers/{id}/classification-fixes`）：成員版論文頁用。
// 寫入的規則照我的文獻庫的加入／移出（ADR-0055），步驟和回報錯誤共用（paper-write.ts：409、401、422、404、換人）：
// - 沒有成功時回 null，畫面顯示「沒有送出成功，再試一次」。
// - 成功：回修正後的分類區塊與歷史；快取裡這一篇的論文頁換上新的分類區塊、歷史的查詢換成新的（不用再讀一次）。
//   回應回來時世代號不同（送出之後換了人）就不改快取、回 null。
// - 找論文的清單有分類標籤：標成過期，下次打開時重讀（不馬上重讀）。
import { queryOptions, useQueryClient, type QueryClient, type QueryKey } from "@tanstack/react-query";
import { useCallback } from "react";

import type {
  ClassificationFixBody,
  ClassificationFixes,
  ClassificationFixesResult,
  PaperResult,
  PaperSource,
  ReadAs,
} from "../datasource/types";
import { paperSource } from "./paper-queries";
import { sendPaperWrite, usePaperWrite, type PaperWrite, type PaperWriteMember } from "./paper-write";

/** 修正歷史的查詢鍵：最後一段是讀的成員（同 paperKey；ADR-0055）。 */
export function classificationFixesKey(paperId: string, memberId?: string): QueryKey {
  return ["classification-fixes", paperId, ...(memberId === undefined ? [] : [memberId])];
}

/** 修正歷史：按「N 位成員修正過」才讀；讀過就留著（送出修正時直接換成新的）。 */
export function classificationFixesQuery(paperId: string, memberId?: string) {
  return queryOptions({
    queryKey: classificationFixesKey(paperId, memberId),
    queryFn: (): Promise<ClassificationFixesResult> => paperSource().classificationFixes(paperId, memberId),
    retry: false,
    staleTime: Infinity,
  });
}

/**
 * 送出一次修正分類，照結果改快取。不丟例外；成功回修正後的分類區塊與歷史，其他（見 paper-write.ts）回 null。
 */
export async function sendClassificationFix(
  queryClient: QueryClient,
  source: PaperSource,
  paperId: string,
  body: ClassificationFixBody,
  onSignedOut: () => Promise<void> | void,
  member: PaperWriteMember,
): Promise<ClassificationFixes | null> {
  return sendPaperWrite(queryClient, classificationFixWrite(queryClient, source, paperId, body), onSignedOut, member);
}

/** 一次修正：送出，成功之後改快取（見檔頭），回修正後的分類區塊與歷史。 */
function classificationFixWrite(
  queryClient: QueryClient,
  source: PaperSource,
  paperId: string,
  body: ClassificationFixBody,
): PaperWrite<{ fixes: ClassificationFixes }, ClassificationFixes> {
  return {
    write: (expected) => source.fixClassification(paperId, body, expected),
    apply: ({ fixes }, readAs) => {
      queryClient.setQueriesData<PaperResult>({ queryKey: ["papers", paperId] }, (data) =>
        data?.kind === "found" ? { ...data, page: { ...data.page, classification: fixes.classification } } : data,
      );
      const history: ClassificationFixesResult = { kind: "found", fixes, readAs };
      queryClient.setQueryData(classificationFixesKey(paperId, readAs), history);
      void queryClient.invalidateQueries({ queryKey: ["paper-list"], refetchType: "none" });
      return fixes;
    },
  };
}

/**
 * 回一個函式：對 `paperId` 送出修正，成功回新的分類區塊與歷史，否則回 null。`readAs` 見 useLibraryChange：
 * 沒有 `readAs` 就不送、回 null（不知道畫面上的資料是誰的，不寫）。
 */
export function useSendClassificationFix(
  readAs: ReadAs,
): (paperId: string, body: ClassificationFixBody) => Promise<ClassificationFixes | null> {
  const queryClient = useQueryClient();
  const write = usePaperWrite(readAs);
  return useCallback(
    (paperId, body) => write(classificationFixWrite(queryClient, paperSource(), paperId, body)),
    [queryClient, write],
  );
}
