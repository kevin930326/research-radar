// 回報中文重點錯誤（Story 3.12；API 是 `POST /api/v1/papers/{id}/summary-reports`）：成員版論文頁用。
// 寫入的規則和修正分類一樣（paper-write.ts：預期成員、409、401、422、404、換人；ADR-0055）：
// - 沒有成功時回 null，畫面顯示「沒有送出成功。你寫的還在，再試一次」。
// - 成功：回 API 給的中文重點區塊（那一欄多了回報的標記）；快取裡這一篇的論文頁換上新的中文重點區塊。
//   回應回來時世代號不同（送出之後換了人）就不改快取、回 null。找論文的清單沒有回報的標記，不用動。
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import type { PaperResult, PaperSource, ReadAs, SummaryReportBody } from "../datasource/types";
import type { ZhSummaryBlock } from "../generated/model";
import { paperSource } from "./paper-queries";
import { sendPaperWrite, usePaperWrite, type PaperWrite, type PaperWriteMember } from "./paper-write";

/** 一次回報：送出，成功之後快取裡的論文頁換上新的中文重點區塊。 */
function summaryReportWrite(
  queryClient: QueryClient,
  source: PaperSource,
  paperId: string,
  body: SummaryReportBody,
): PaperWrite<{ zhSummary: ZhSummaryBlock }, ZhSummaryBlock> {
  return {
    write: (expected) => source.reportSummary(paperId, body, expected),
    apply: ({ zhSummary }) => {
      queryClient.setQueriesData<PaperResult>({ queryKey: ["papers", paperId] }, (data) =>
        data?.kind === "found" ? { ...data, page: { ...data.page, zh_summary: zhSummary } } : data,
      );
      return zhSummary;
    },
  };
}

/** 送出一次回報，照結果改快取。不丟例外；成功回新的中文重點區塊，其他（見 paper-write.ts）回 null。 */
export function sendSummaryReport(
  queryClient: QueryClient,
  source: PaperSource,
  paperId: string,
  body: SummaryReportBody,
  onSignedOut: () => Promise<void> | void,
  member: PaperWriteMember,
): Promise<ZhSummaryBlock | null> {
  return sendPaperWrite(queryClient, summaryReportWrite(queryClient, source, paperId, body), onSignedOut, member);
}

/**
 * 回一個函式：對 `paperId` 送出回報，成功回新的中文重點區塊，否則回 null。沒有 `readAs` 就不送、回 null
 * （同 useSendClassificationFix）。
 */
export function useSendSummaryReport(readAs: ReadAs): (paperId: string, body: SummaryReportBody) => Promise<ZhSummaryBlock | null> {
  const queryClient = useQueryClient();
  const write = usePaperWrite(readAs);
  return useCallback((paperId, body) => write(summaryReportWrite(queryClient, paperSource(), paperId, body)), [queryClient, write]);
}
