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
