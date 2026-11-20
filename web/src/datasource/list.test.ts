// 成員版的論文清單（Story 3.14、AD-21）：要幾篇就接著讀幾頁、只放要求的篇數（多讀到的留在 pending）、
// 「再載入」從上一次接著讀（先用 pending）、401 是登出、其他錯誤丟出去。
import { afterEach, describe, expect, it, vi } from "vitest";

import { datasource } from "./api";
import type { PaperList } from "./types";

function row(id: number) {
  return { id: `rr-${id}`, title: `Fake paper ${id}` };
}

function page(ids: number[], next: string | null) {
  return { items: ids.map(row), next_cursor: next, total: 45, snapshot: { id: 1 } };
}

function respond(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const papers = datasource.papers!;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("paper list paging", () => {
  it("reads pages until it has enough rows, sending the cursor and filters; extra rows wait in pending", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(respond(200, page([1, 2], "c1")))
      .mockResolvedValueOnce(respond(200, page([3, 4], "c2")));
    vi.stubGlobal("fetch", fetchMock);
    const result = await papers.list({ topic: ["erp"] }, 3);
    expect(result.kind).toBe("found");
    const list = (result as { kind: "found"; list: PaperList }).list;
    // 審查 H-M2：items 不超過要求的篇數。
    expect(list.items.map((item) => item.id)).toEqual(["rr-1", "rr-2", "rr-3"]);
    expect(list.pending.map((item) => item.id)).toEqual(["rr-4"]);
    expect(list.next_cursor).toBe("c2");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[0]![0])).not.toContain("cursor");
    expect(String(fetchMock.mock.calls[1]![0])).toContain("cursor=c1");
    expect(String(fetchMock.mock.calls[1]![0])).toContain("topic=erp");
  });

  it("continues from the previous list instead of reading it again", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(respond(200, page([3, 4], null)));
    vi.stubGlobal("fetch", fetchMock);
    const previous = { ...page([1, 2], "c1"), pending: [] } as unknown as PaperList;
    const result = await papers.list({}, 4, previous);
    const list = (result as { kind: "found"; list: PaperList }).list;
    expect(list.items).toHaveLength(4);
    expect(list.next_cursor).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0]![0])).toContain("cursor=c1");
  });

  it("uses the pending rows first and reads the next page only when they run out", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(respond(200, page([6, 7], null)));
    vi.stubGlobal("fetch", fetchMock);
    // 起始狀態只要 3 篇，同一頁多讀到的 4、5 在 pending。
    const previous = { ...page([1, 2, 3], "c1"), pending: page([4, 5], null).items } as unknown as PaperList;
    const four = await papers.list({}, 4, previous);
    expect((four as { kind: "found"; list: PaperList }).list.items.map((item) => item.id)).toEqual(["rr-1", "rr-2", "rr-3", "rr-4"]);
    expect((four as { kind: "found"; list: PaperList }).list.pending.map((item) => item.id)).toEqual(["rr-5"]);
    expect(fetchMock).not.toHaveBeenCalled();
    const seven = await papers.list({}, 7, (four as { kind: "found"; list: PaperList }).list);
    expect((seven as { kind: "found"; list: PaperList }).list.items.map((item) => item.id)).toEqual([
      "rr-1",
      "rr-2",
      "rr-3",
      "rr-4",
      "rr-5",
      "rr-6",
      "rr-7",
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0]![0])).toContain("cursor=c1");
  });

  it("stops on an empty page even when the server says there is more", async () => {
    const fetchMock = vi.fn().mockResolvedValue(respond(200, page([], "again")));
    vi.stubGlobal("fetch", fetchMock);
    const result = await papers.list({}, 20);
    expect((result as { kind: "found"; list: PaperList }).list.next_cursor).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("is signed out on 401 and throws on other errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(respond(401, {})));
    expect(await papers.list({}, 20)).toEqual({ kind: "signedOut" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(respond(422, { error: { code: "request_invalid" } })));
    await expect(papers.list({}, 20)).rejects.toThrow("422");
  });
});
