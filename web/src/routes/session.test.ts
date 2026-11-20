// 登入後要回去的路徑（next）只收站內路徑：規則和後端的 safe_next_path 一樣，另外不收登入頁自己。
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { clearMemberQueries, loadMember, memberGeneration, safeNextPath, validateLoginSearch } from "./session";

describe("safeNextPath", () => {
  it.each(["/", "/notice", "/notice?from=a&b=1", "/notice#top", `/${"a".repeat(2047)}`])("keeps %s", (value) => {
    expect(safeNextPath(value)).toBe(value);
  });

  it.each([
    ["empty", ""],
    ["not a path", "notice"],
    ["absolute URL", "https://evil.example/"],
    ["protocol-relative", "//evil.example/x"],
    ["backslash", "/\\evil.example"],
    ["space", "/notice page"],
    ["control character", "/notice\n"],
    ["non-ASCII", "/caf\u00e9"],
    ["too long", `/${"a".repeat(2048)}`],
    ["login page", "/login"],
    ["login page with query", "/login?next=/notice"],
    ["login sub-path", "/login/x"],
    ["login page in capitals", "/LOGIN"],
    ["login page percent-encoded", "/%6Cogin"],
    ["broken percent-encoding", "/%E0%A4%A"],
  ])("rejects %s", (_label, value) => {
    expect(safeNextPath(value)).toBeUndefined();
  });

  it("rejects anything that is not a string", () => {
    for (const value of [undefined, null, 1, true, ["/notice"], { path: "/notice" }]) {
      expect(safeNextPath(value)).toBeUndefined();
    }
  });

  it("keeps paths that only start like the login page", () => {
    expect(safeNextPath("/loginhelp")).toBe("/loginhelp");
  });
});

describe("validateLoginSearch", () => {
  it("keeps only valid fields", () => {
    expect(validateLoginSearch({ next: "/notice", expired: true, member: "m-1", other: "x" })).toEqual({
      next: "/notice",
      expired: true,
      member: "m-1",
    });
  });

  it("always writes all three fields, so raw values from the address cannot slip through", () => {
    // 路由把網址上原本的參數和這裡的結果合在一起，少寫一個欄位就會留下沒檢查過的值。
    const result = validateLoginSearch({ next: "//evil.example", expired: 1, member: 5 });
    expect(Object.keys(result).sort()).toEqual(["expired", "member", "next"]);
    expect(Object.values(result)).toEqual([undefined, undefined, undefined]);
  });

  it("drops an unsafe next, a non-true expired and an empty member", () => {
    expect(validateLoginSearch({ next: "//evil.example", expired: "true", member: "" })).toEqual({});
  });
});

// 登出、登入過期、登入成功時（Codex 審查第 1 條）：不只「我是誰」，所有查詢快取都清掉。
describe("clearMemberQueries", () => {
  it("removes every cached query, not only who-am-i", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(["session", "me"], { member_id: "a", display_name: "A" });
    queryClient.setQueryData(["papers", "list"], ["paper of A"]);
    queryClient.setQueryData(["library"], { owner: "a" });
    await clearMemberQueries(queryClient);
    expect(queryClient.getQueryCache().getAll()).toEqual([]);
  });

  it("cancels a query still in flight, so its answer does not come back into the cache", async () => {
    const queryClient = new QueryClient();
    let answer: (value: string) => void = () => {};
    const pending = queryClient
      .fetchQuery({ queryKey: ["library"], queryFn: () => new Promise<string>((resolve) => (answer = resolve)) })
      .catch(() => "cancelled");
    await clearMemberQueries(queryClient);
    answer("data of A");
    await expect(pending).resolves.toBe("cancelled");
    expect(queryClient.getQueryData(["library"])).toBeUndefined();
    expect(queryClient.getQueryCache().getAll()).toEqual([]);
  });
});

// 每次清快取，世代號加 1（審查 F2：還在路上的加入、移出、復原回來時看得出換過人）。
describe("memberGeneration", () => {
  it("goes up by one each time the cache is cleared, separately for each query client", async () => {
    const queryClient = new QueryClient();
    const other = new QueryClient();
    expect(memberGeneration(queryClient)).toBe(0);
    await clearMemberQueries(queryClient);
    await clearMemberQueries(queryClient);
    expect(memberGeneration(queryClient)).toBe(2);
    expect(memberGeneration(other)).toBe(0);
  });
});

// 另一個分頁換人登入後，這個分頁下一次換頁時（審查 F1）：「我是誰」換了人就先清掉上一位的快取。
describe("loadMember", () => {
  const A = { member_id: "00000000-0000-4000-8000-00000000000a", display_name: "Test Member A" };
  const B = { member_id: "00000000-0000-4000-8000-00000000000b", display_name: "Test Member B" };

  function answerMe(status: number, body: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }))),
    );
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function cacheOf(member: typeof A) {
    const queryClient = new QueryClient();
    queryClient.setQueryData(["session", "me"], member);
    queryClient.setQueryData(["paper-list", "[]", 20], { kind: "found", owner: member.member_id });
    queryClient.setQueryData(["papers", "p-1"], { kind: "found", owner: member.member_id });
    return queryClient;
  }

  it("another member answers: everything cached for the previous member is cleared, only the new member stays", async () => {
    const queryClient = cacheOf(A);
    answerMe(200, B);
    const result = await loadMember(queryClient);
    expect(result).toEqual({ member: B, wasSignedIn: true });
    expect(queryClient.getQueryCache().getAll().map((query) => query.queryKey)).toEqual([["session", "me"]]);
    expect(queryClient.getQueryData(["session", "me"])).toEqual(B);
    expect(memberGeneration(queryClient)).toBe(1);
  });

  it("the same member answers: the cache stays", async () => {
    const queryClient = cacheOf(A);
    answerMe(200, A);
    expect(await loadMember(queryClient)).toEqual({ member: A, wasSignedIn: true });
    expect(queryClient.getQueryData(["paper-list", "[]", 20])).toEqual({ kind: "found", owner: A.member_id });
    expect(queryClient.getQueryData(["papers", "p-1"])).toEqual({ kind: "found", owner: A.member_id });
    expect(memberGeneration(queryClient)).toBe(0);
  });

  it("nobody was cached (opening an address directly): nothing to clear, not signed in before", async () => {
    const queryClient = new QueryClient();
    answerMe(200, B);
    expect(await loadMember(queryClient)).toEqual({ member: B, wasSignedIn: false });
    expect(memberGeneration(queryClient)).toBe(0);
  });

  it("signed out (401): returns no member and leaves clearing to the caller", async () => {
    const queryClient = cacheOf(A);
    answerMe(401, { type: "about:blank", title: "Unauthorized", status: 401, code: "not_authenticated" });
    expect(await loadMember(queryClient)).toEqual({ member: null, wasSignedIn: true });
    expect(memberGeneration(queryClient)).toBe(0);
  });
});
