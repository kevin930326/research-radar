// 我的文獻庫（Story 3.17）：清單要幾篇就接著讀幾頁、多讀到的留在 pending、「再載入」從上一次接著讀；
// 加入、移出、復原的路徑與方法、論文 ID 有編碼、200／404／401 的結果，其他錯誤丟出去。
import { afterEach, describe, expect, it, vi } from "vitest";

import { datasource } from "./api";
import type { LibraryList } from "./types";

function item(id: number) {
  return { paper: { id: `lib-${id}`, title: `Fake paper ${id}`, in_my_library: true }, added_via: "manual", added_at: "2026-11-01T00:00:00Z" };
}

function page(ids: number[], next: string | null) {
  return { items: ids.map(item), next_cursor: next, total: 5 };
}

function respond(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const library = datasource.library!;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("library list paging", () => {
  it("reads pages until it has enough items and keeps the extra ones in pending", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(respond(200, page([1, 2], "c1")))
      .mockResolvedValueOnce(respond(200, page([3, 4], "c2")));
    vi.stubGlobal("fetch", fetchMock);
    const result = await library.list(3);
    const list = (result as { kind: "found"; list: LibraryList }).list;
    expect(list.items.map((entry) => entry.paper.id)).toEqual(["lib-1", "lib-2", "lib-3"]);
    expect(list.pending.map((entry) => entry.paper.id)).toEqual(["lib-4"]);
    expect(list.total).toBe(5);
    expect(String(fetchMock.mock.calls[0]![0])).toMatch(/\/api\/v1\/library$/);
    expect(String(fetchMock.mock.calls[1]![0])).toContain("cursor=c1");
  });

  it("continues from the previous list: pending first, then the next cursor", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(respond(200, page([5], null)));
    vi.stubGlobal("fetch", fetchMock);
    const previous = { ...page([1, 2, 3], "c2"), pending: [item(4)] } as unknown as LibraryList;
    const list = ((await library.list(5, previous)) as { kind: "found"; list: LibraryList }).list;
    expect(list.items.map((entry) => entry.paper.id)).toEqual(["lib-1", "lib-2", "lib-3", "lib-4", "lib-5"]);
    expect(list.next_cursor).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0]![0])).toContain("cursor=c2");
  });

  it("401 is signed out; other errors are thrown", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(respond(401, {})));
    expect(await library.list(50)).toEqual({ kind: "signedOut" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(respond(500, {})));
    await expect(library.list(50)).rejects.toThrow("HTTP 500");
  });
});

describe("library changes", () => {
  it.each([
    ["add", "PUT", "/api/v1/library/a%2Fb"],
    ["remove", "DELETE", "/api/v1/library/a%2Fb"],
    ["restore", "POST", "/api/v1/library/a%2Fb/restore"],
  ] as const)("%s sends %s %s and returns the item", async (action, method, path) => {
    const fetchMock = vi.fn().mockResolvedValueOnce(respond(200, item(1)));
    vi.stubGlobal("fetch", fetchMock);
    const result = await library[action]("a/b");
    expect(result).toEqual({ kind: "done", item: item(1) });
    const [url, init] = fetchMock.mock.calls[0]! as [string, RequestInit];
    expect(String(url)).toMatch(new RegExp(`${path.replace(/[/%]/g, "\\$&")}$`));
    expect(init.method).toBe(method);
  });

  it("404 is not found, 401 is signed out, other errors are thrown", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(respond(404, {})));
    expect(await library.add("x")).toEqual({ kind: "notFound" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(respond(401, {})));
    expect(await library.remove("x")).toEqual({ kind: "signedOut" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(respond(500, {})));
    await expect(library.restore("x")).rejects.toThrow("HTTP 500");
  });
});

// epic-3h PR 3（ADR-0055）：三種寫入都從同一個函式送出，給了預期成員就帶 X-Radar-Expected-Member；409 member_changed 是 memberChanged。
describe("library changes carry the expected member", () => {
  const MEMBER = "00000000-0000-4000-8000-00000000000a";

  it.each(["add", "remove", "restore"] as const)("%s sends the expected member header", async (action) => {
    const fetchMock = vi.fn().mockResolvedValueOnce(respond(200, item(1)));
    vi.stubGlobal("fetch", fetchMock);
    await library[action]("a/b", MEMBER);
    const [, init] = fetchMock.mock.calls[0]! as [string, RequestInit];
    expect(new Headers(init.headers).get("X-Radar-Expected-Member")).toBe(MEMBER);
  });

  it.each(["add", "remove", "restore"] as const)("%s without an expected member sends no such header", async (action) => {
    const fetchMock = vi.fn().mockResolvedValueOnce(respond(200, item(1)));
    vi.stubGlobal("fetch", fetchMock);
    await library[action]("a/b");
    const [, init] = fetchMock.mock.calls[0]! as [string, RequestInit];
    expect(new Headers(init.headers).has("X-Radar-Expected-Member")).toBe(false);
  });

  it("409 member_changed is memberChanged; any other 409 is thrown", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(respond(409, { type: "about:blank", title: "Conflict", status: 409, code: "member_changed" })));
    expect(await library.remove("x", MEMBER)).toEqual({ kind: "memberChanged" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(respond(409, { type: "about:blank", title: "Conflict", status: 409, code: "request_rejected" })));
    await expect(library.add("x", MEMBER)).rejects.toThrow("HTTP 409");
  });

  it("400 expected_member_invalid is thrown (the page never sends a malformed one)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(respond(400, { type: "about:blank", title: "Bad Request", status: 400, code: "expected_member_invalid" })));
    await expect(library.restore("x", "not-a-member")).rejects.toThrow("HTTP 400");
  });
});

// epic-3h2 PR 1 fix1（ADR-0055）：成員專屬的讀取（論文頁、找論文清單、文獻庫清單）也帶預期成員；每一頁都帶；
// 讀到的結果記下用誰讀的（readAs）；409 member_changed 是 memberChanged；沒給就不帶（readAs 也沒有）。
describe("member reads carry the expected member", () => {
  const MEMBER = "00000000-0000-4000-8000-00000000000a";
  const papers = datasource.papers!;
  const headerOf = (call: unknown[]) => new Headers((call[1] as RequestInit | undefined)?.headers).get("X-Radar-Expected-Member");
  const paperBody = { paper: { id: "p1" }, in_my_library: true };
  const listPage = (ids: string[], next: string | null) => ({ items: ids.map((id) => ({ id })), next_cursor: next, total: 3, snapshot: null });
  const conflict = { type: "about:blank", title: "Conflict", status: 409, code: "member_changed" };

  it("paper: the header goes out, the result says who it was read as; 409 member_changed is memberChanged", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(respond(200, paperBody));
    vi.stubGlobal("fetch", fetchMock);
    expect(await papers.paper("p1", MEMBER)).toEqual({ kind: "found", page: paperBody, readAs: MEMBER });
    expect(headerOf(fetchMock.mock.calls[0]!)).toBe(MEMBER);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(respond(409, conflict)));
    expect(await papers.paper("p1", MEMBER)).toEqual({ kind: "memberChanged" });
  });

  it("paper without an expected member: no header, no readAs", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(respond(200, paperBody));
    vi.stubGlobal("fetch", fetchMock);
    const result = await papers.paper("p1");
    expect(result).toStrictEqual({ kind: "found", page: paperBody });
    expect(headerOf(fetchMock.mock.calls[0]!)).toBeNull();
  });

  it("paper list: every page carries the header; the result says who it was read as; 409 on a later page is memberChanged", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(respond(200, listPage(["a", "b"], "c1")))
      .mockResolvedValueOnce(respond(200, listPage(["c"], null)));
    vi.stubGlobal("fetch", fetchMock);
    const result = await papers.list({}, 3, null, MEMBER);
    expect(result.kind === "found" && result.readAs).toBe(MEMBER);
    expect(fetchMock.mock.calls.map(headerOf)).toEqual([MEMBER, MEMBER]);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(respond(200, listPage(["a", "b"], "c1"))).mockResolvedValueOnce(respond(409, conflict)));
    expect(await papers.list({}, 3, null, MEMBER)).toEqual({ kind: "memberChanged" });
  });

  it("library list: every page carries the header; the result says who it was read as; 409 is memberChanged", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(respond(200, page([1, 2], "c1")))
      .mockResolvedValueOnce(respond(200, page([3], null)));
    vi.stubGlobal("fetch", fetchMock);
    const result = await library.list(3, null, MEMBER);
    expect(result.kind === "found" && result.readAs).toBe(MEMBER);
    expect(fetchMock.mock.calls.map(headerOf)).toEqual([MEMBER, MEMBER]);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(respond(409, conflict)));
    expect(await library.list(3, null, MEMBER)).toEqual({ kind: "memberChanged" });
  });

  it("lists without an expected member: no header, no readAs", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(respond(200, page([1], null))).mockResolvedValueOnce(respond(200, listPage(["a"], null)));
    vi.stubGlobal("fetch", fetchMock);
    const own = await library.list(3);
    const all = await papers.list({}, 3);
    expect("readAs" in own || "readAs" in all).toBe(false);
    expect(fetchMock.mock.calls.map(headerOf)).toEqual([null, null]);
  });
});
