// 回報中文重點錯誤的送出（Story 3.12；步驟同修正分類，paper-write.ts，ADR-0055）：帶畫面那份資料的成員、
// 409 照「發現換人」處理而且不改快取、401 清快取回登入頁、422／404／網路錯誤回 null、成功時快取裡的論文頁換上新的中文重點區塊；
// 回應回來之前換了人就什麼都不寫。
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import type { PaperSource, SummaryReportResult } from "../datasource/types";
import type { ZhSummaryBlock } from "../generated/model";
import { paperKey } from "./paper-queries";
import { clearMemberQueries } from "./session";
import { sendSummaryReport } from "./summary-report";

const PAPER = "00000000-0000-4000-8000-000000000001";
const MEMBER = "00000000-0000-4000-8000-00000000000a";
const BODY = { field: "findings", note: "fake note" };
const VALUE = { zh_summary_id: "s1", research_question: "q", method: null, theory: null, findings: "f" };
const OLD = { state: "ready", value: { ...VALUE, reported_fields: [] }, provenance: null } as unknown as ZhSummaryBlock;
const NEW = { state: "ready", value: { ...VALUE, reported_fields: ["findings"] }, provenance: null } as unknown as ZhSummaryBlock;

function source(answer: SummaryReportResult | Error, hold?: Promise<void>) {
  const reportSummary = vi.fn(async () => {
    await hold;
    if (answer instanceof Error) throw answer;
    return answer;
  });
  return { source: { reportSummary } as unknown as PaperSource, reportSummary };
}

function cached(queryClient: QueryClient) {
  queryClient.setQueryData(paperKey(PAPER, MEMBER), { kind: "found", page: { paper: { id: PAPER }, zh_summary: OLD }, readAs: MEMBER });
}

function summaryOf(queryClient: QueryClient) {
  return queryClient.getQueryData<{ page: { zh_summary: unknown } }>(paperKey(PAPER, MEMBER))!.page.zh_summary;
}

describe("sendSummaryReport", () => {
  it("sends the expected member and on success swaps the cached summary block", async () => {
    const queryClient = new QueryClient();
    cached(queryClient);
    const { source: fake, reportSummary } = source({ kind: "done", zhSummary: NEW });
    const onMemberChanged = vi.fn();
    const result = await sendSummaryReport(queryClient, fake, PAPER, BODY, () => {}, { expectedMemberId: MEMBER, onMemberChanged });
    expect(result).toBe(NEW);
    expect(reportSummary).toHaveBeenCalledWith(PAPER, BODY, MEMBER);
    expect(summaryOf(queryClient)).toEqual(NEW);
    expect(onMemberChanged).not.toHaveBeenCalled();
  });

  it("409 member_changed: follows the member change, writes nothing to the cache", async () => {
    const queryClient = new QueryClient();
    cached(queryClient);
    const onMemberChanged = vi.fn();
    const onSignedOut = vi.fn();
    const result = await sendSummaryReport(queryClient, source({ kind: "memberChanged" }).source, PAPER, BODY, onSignedOut, {
      expectedMemberId: MEMBER,
      onMemberChanged,
    });
    expect(result).toBeNull();
    expect(onMemberChanged).toHaveBeenCalledTimes(1);
    expect(onSignedOut).not.toHaveBeenCalled();
    expect(summaryOf(queryClient)).toEqual(OLD);
  });

  it("401: clears the member's cache and goes to the login page", async () => {
    const queryClient = new QueryClient();
    cached(queryClient);
    const onSignedOut = vi.fn();
    const result = await sendSummaryReport(queryClient, source({ kind: "signedOut" }).source, PAPER, BODY, onSignedOut, {
      expectedMemberId: MEMBER,
      onMemberChanged: () => {},
    });
    expect(result).toBeNull();
    expect(onSignedOut).toHaveBeenCalledTimes(1);
    expect(queryClient.getQueryData(paperKey(PAPER, MEMBER))).toBeUndefined();
  });

  it.each([{ kind: "rejected" } as const, { kind: "notFound" } as const, new Error("network")])(
    "%o: null, the cache stays as it was",
    async (answer) => {
      const queryClient = new QueryClient();
      cached(queryClient);
      const result = await sendSummaryReport(queryClient, source(answer).source, PAPER, BODY, () => {}, {
        expectedMemberId: MEMBER,
        onMemberChanged: () => {},
      });
      expect(result).toBeNull();
      expect(summaryOf(queryClient)).toEqual(OLD);
    },
  );

  it("the answer comes back after someone else logged in: nothing is written", async () => {
    const queryClient = new QueryClient();
    let release = () => {};
    const hold = new Promise<void>((resolve) => (release = resolve));
    const pending = sendSummaryReport(queryClient, source({ kind: "done", zhSummary: NEW }, hold).source, PAPER, BODY, () => {}, {
      expectedMemberId: MEMBER,
      onMemberChanged: () => {},
    });
    await clearMemberQueries(queryClient);
    cached(queryClient);
    release();
    expect(await pending).toBeNull();
    expect(summaryOf(queryClient)).toEqual(OLD);
  });
});
