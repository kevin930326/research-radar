// 登入後要回去的路徑（next）只收站內路徑：規則和後端的 safe_next_path 一樣，另外不收登入頁自己。
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import { clearMemberQueries, safeNextPath, validateLoginSearch } from "./session";

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
