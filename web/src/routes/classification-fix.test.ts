// 修正分類的送出（Story 3.11；規則同 library-change.ts，ADR-0055）：帶畫面那份資料的成員、409 照「發現換人」處理而且不改快取、
// 401 清快取回登入頁、422／404／網路錯誤回 null、成功時快取裡的論文頁換上新的分類區塊、歷史換成新的；
// 回應回來之前換了人就什麼都不寫。
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import type { ClassificationFixes, ClassificationFixResult, PaperSource } from "../datasource/types";
import { classificationFixesKey, sendClassificationFix } from "./classification-fix";
import { paperKey } from "./paper-queries";
import { clearMemberQueries } from "./session";

const PAPER = "00000000-0000-4000-8000-000000000001";
const MEMBER = "00000000-0000-4000-8000-00000000000a";
const BODY = { topic: ["e_commerce", "digital_platforms"], method: ["experiment"] };
const OLD = { state: "ready", value: { topic: null, method: null, fix_member_count: 0 }, provenance: null };
const FIXES = {
  classification: { state: "ready", value: { topic: null, method: null, fix_member_count: 1 }, provenance: null },
  history: [{ fixed_at: "2026-11-19T02:30:00Z", member_name: "Test Member A", topic: BODY.topic, method: BODY.method, reason: null }],
} as unknown as ClassificationFixes;

function source(answer: ClassificationFixResult | Error, hold?: Promise<void>) {
  const fixClassification = vi.fn(async () => {
    await hold;
    if (answer instanceof Error) throw answer;
    return answer;
  });
  return { source: { fixClassification } as unknown as PaperSource, fixClassification };
}

function cached(queryClient: QueryClient) {
  queryClient.setQueryData(paperKey(PAPER, MEMBER), { kind: "found", page: { paper: { id: PAPER }, classification: OLD }, readAs: MEMBER });
}

function classificationOf(queryClient: QueryClient) {
  return queryClient.getQueryData<{ page: { classification: unknown } }>(paperKey(PAPER, MEMBER))!.page.classification;
}

describe("sendClassificationFix", () => {
  it("sends the expected member and on success swaps the cached classification and history", async () => {
    const queryClient = new QueryClient();
    cached(queryClient);
    const { source: fake, fixClassification } = source({ kind: "done", fixes: FIXES });
    const onMemberChanged = vi.fn();
    const result = await sendClassificationFix(queryClient, fake, PAPER, BODY, () => {}, { expectedMemberId: MEMBER, onMemberChanged });
    expect(result).toBe(FIXES);
    expect(fixClassification).toHaveBeenCalledWith(PAPER, BODY, MEMBER);
    expect(classificationOf(queryClient)).toEqual(FIXES.classification);
    expect(queryClient.getQueryData(classificationFixesKey(PAPER, MEMBER))).toEqual({ kind: "found", fixes: FIXES, readAs: MEMBER });
    expect(onMemberChanged).not.toHaveBeenCalled();
  });

  it("409 member_changed: follows the member change, writes nothing to the cache", async () => {
    const queryClient = new QueryClient();
    cached(queryClient);
    const onMemberChanged = vi.fn();
    const onSignedOut = vi.fn();
    const result = await sendClassificationFix(queryClient, source({ kind: "memberChanged" }).source, PAPER, BODY, onSignedOut, {
      expectedMemberId: MEMBER,
      onMemberChanged,
    });
    expect(result).toBeNull();
    expect(onMemberChanged).toHaveBeenCalledTimes(1);
    expect(onSignedOut).not.toHaveBeenCalled();
    expect(classificationOf(queryClient)).toEqual(OLD);
  });

  it("401: clears the member's cache and goes to the login page", async () => {
    const queryClient = new QueryClient();
    cached(queryClient);
    const onSignedOut = vi.fn();
    const result = await sendClassificationFix(queryClient, source({ kind: "signedOut" }).source, PAPER, BODY, onSignedOut, {
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
      const result = await sendClassificationFix(queryClient, source(answer).source, PAPER, BODY, () => {}, {
        expectedMemberId: MEMBER,
        onMemberChanged: () => {},
      });
      expect(result).toBeNull();
      expect(classificationOf(queryClient)).toEqual(OLD);
    },
  );

  it("the answer comes back after someone else logged in: nothing is written", async () => {
    const queryClient = new QueryClient();
    let release = () => {};
    const hold = new Promise<void>((resolve) => (release = resolve));
    const pending = sendClassificationFix(queryClient, source({ kind: "done", fixes: FIXES }, hold).source, PAPER, BODY, () => {}, {
      expectedMemberId: MEMBER,
      onMemberChanged: () => {},
    });
    await clearMemberQueries(queryClient);
    cached(queryClient);
    release();
    expect(await pending).toBeNull();
    expect(classificationOf(queryClient)).toEqual(OLD);
    expect(queryClient.getQueryData(classificationFixesKey(PAPER, MEMBER))).toBeUndefined();
  });
});
