// 回報中文重點錯誤（Story 3.12）的資料來源：路徑與方法、論文 ID 有編碼、本文照送、預期成員的標頭（ADR-0055）、
// 200／422／404／401／409 的結果，其他錯誤丟出去。資料全是假的。
import { afterEach, describe, expect, it, vi } from "vitest";

import { datasource } from "./api";

const MEMBER = "00000000-0000-4000-8000-00000000000a";
const ZH = { state: "ready", value: { zh_summary_id: "s1", research_question: "q", method: null, theory: null, findings: "f", reported_fields: ["findings"] }, provenance: null };
const BODY = { field: "findings", note: "fake note" };

function respond(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function problem(status: number, code: string) {
  return respond(status, { type: "about:blank", title: "x", status, code });
}

const papers = datasource.papers!;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("summary reports", () => {
  it("POST sends the body, the encoded id and the expected member; 200 gives the summary block", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(respond(200, { zh_summary: ZH }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await papers.reportSummary("a/b", BODY, MEMBER)).toEqual({ kind: "done", zhSummary: ZH });
    const [url, init] = fetchMock.mock.calls[0]! as [string, RequestInit];
    expect(url).toBe("/api/v1/papers/a%2Fb/summary-reports");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual(BODY);
    expect(new Headers(init.headers).get("X-Radar-Expected-Member")).toBe(MEMBER);
  });

  it.each([
    [problem(422, "request_invalid"), { kind: "rejected" }],
    [problem(404, "not_found"), { kind: "notFound" }],
    [problem(401, "not_authenticated"), { kind: "signedOut" }],
    [problem(409, "member_changed"), { kind: "memberChanged" }],
  ])("POST answer %# maps to a result", async (response, expected) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(response));
    expect(await papers.reportSummary("x", BODY, MEMBER)).toEqual(expected);
  });

  it("other errors are thrown", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(respond(500, {})));
    await expect(papers.reportSummary("x", BODY, MEMBER)).rejects.toThrow("HTTP 500");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(problem(409, "request_rejected")));
    await expect(papers.reportSummary("x", BODY, MEMBER)).rejects.toThrow("HTTP 409");
  });
});
