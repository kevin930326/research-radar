// 修正分類（Story 3.11）的資料來源：路徑與方法、論文 ID 有編碼、本文照送、預期成員的標頭（ADR-0055）、
// 200／422／404／401／409 的結果，其他錯誤丟出去。資料全是假的。
import { afterEach, describe, expect, it, vi } from "vitest";

import { datasource } from "./api";

const MEMBER = "00000000-0000-4000-8000-00000000000a";
const FIXES = {
  classification: { state: "pending_batch", value: null, provenance: null },
  history: [{ fixed_at: "2026-11-19T02:30:00Z", member_name: "Test Member A", topic: ["e_commerce"], method: ["experiment"], reason: null }],
};
const BODY = { topic: ["e_commerce"], method: ["experiment"], reason: "cross-platform ratings" };

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

describe("classification fixes", () => {
  it("POST sends the body, the encoded id and the expected member", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(respond(200, FIXES));
    vi.stubGlobal("fetch", fetchMock);
    expect(await papers.fixClassification("a/b", BODY, MEMBER)).toEqual({ kind: "done", fixes: FIXES });
    const [url, init] = fetchMock.mock.calls[0]! as [string, RequestInit];
    expect(url).toBe("/api/v1/papers/a%2Fb/classification-fixes");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual(BODY);
    expect(new Headers(init.headers).get("X-Radar-Expected-Member")).toBe(MEMBER);
  });

  it("GET reads with the expected member and remembers who read it", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(respond(200, FIXES));
    vi.stubGlobal("fetch", fetchMock);
    expect(await papers.classificationFixes("a/b", MEMBER)).toEqual({ kind: "found", fixes: FIXES, readAs: MEMBER });
    const [url, init] = fetchMock.mock.calls[0]! as [string, RequestInit | undefined];
    expect(url).toBe("/api/v1/papers/a%2Fb/classification-fixes");
    expect(new Headers(init?.headers).get("X-Radar-Expected-Member")).toBe(MEMBER);
  });

  it.each([
    [problem(422, "request_invalid"), { kind: "rejected" }],
    [problem(404, "not_found"), { kind: "notFound" }],
    [problem(401, "not_authenticated"), { kind: "signedOut" }],
    [problem(409, "member_changed"), { kind: "memberChanged" }],
  ])("POST answer %# maps to a result", async (response, expected) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(response));
    expect(await papers.fixClassification("x", BODY, MEMBER)).toEqual(expected);
  });

  it.each([
    [problem(404, "not_found"), { kind: "notFound" }],
    [problem(401, "not_authenticated"), { kind: "signedOut" }],
    [problem(409, "member_changed"), { kind: "memberChanged" }],
  ])("GET answer %# maps to a result", async (response, expected) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(response));
    expect(await papers.classificationFixes("x", MEMBER)).toEqual(expected);
  });

  it("other errors are thrown", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(respond(500, {})));
    await expect(papers.fixClassification("x", BODY, MEMBER)).rejects.toThrow("HTTP 500");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(problem(409, "request_rejected")));
    await expect(papers.fixClassification("x", BODY, MEMBER)).rejects.toThrow("HTTP 409");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(respond(500, {})));
    await expect(papers.classificationFixes("x", MEMBER)).rejects.toThrow("HTTP 500");
  });
});
