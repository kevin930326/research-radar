// 加入、移出、復原（Story 3.17）：回應回來之前換了人就不改快取（審查 F2）、寫快取前與處理 401 時換了人也一樣（審查 G1）、移出時已經不在（404）不給復原
// （審查 F3）、論文頁換了一份資料就照新的顯示（審查 F9）、清單讀到一半時加入不會被讀完的舊資料蓋回去（審查 F10）。
import { focusManager, QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { LibraryChangeResult, LibraryItem, LibrarySource, PaperList, PaperListFilters, PaperPage } from "../datasource/types";
import { followMemberChange, libraryShown, sendLibraryChange } from "./library-change";
import { datasource } from "../datasource";
import { toApiParams } from "../pages/search-filters";
import { filtersKey, isPersonalList, markInLibrary, paperListQuery } from "./paper-queries";
import { clearMemberQueries, meQuery, memberGeneration } from "./session";

const PAPER = "00000000-0000-4000-8000-000000000001";

function row(id: string, inLibrary: boolean) {
  return { id, title: `Fake paper ${id}`, in_my_library: inLibrary } as unknown as PaperList["items"][number];
}

function list(inLibrary: boolean): { kind: "found"; list: PaperList } {
  return { kind: "found", list: { items: [row(PAPER, inLibrary)], pending: [], next_cursor: null, total: 1 } as unknown as PaperList };
}

const LIST_KEY = ["paper-list", "[]", 20];
const ITEM = { paper: row(PAPER, true), added_via: "manual", added_at: "2026-11-01T00:00:00Z" } as unknown as LibraryItem;

/** 一個文獻庫來源：每個動作回 `answer`；給了 `hold` 就等它放行才回。 */
function source(answer: LibraryChangeResult, hold?: Promise<void>): LibrarySource {
  const reply = async () => {
    await hold;
    return answer;
  };
  return { list: () => Promise.reject(new Error("not used")), add: reply, remove: reply, restore: reply };
}

function gate(): [Promise<void>, () => void] {
  let release = () => {};
  const promise = new Promise<void>((resolve) => (release = resolve));
  return [promise, release];
}

describe("sendLibraryChange", () => {
  it("done: the paper is marked in every cached list", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(LIST_KEY, list(false));
    expect(await sendLibraryChange(queryClient, source({ kind: "done", item: ITEM }), PAPER, "add")).toBe("done");
    expect(queryClient.getQueryData<ReturnType<typeof list>>(LIST_KEY)!.list.items[0]!.in_my_library).toBe(true);
  });

  it("the answer comes back after the cache was cleared (logged out, someone else logged in): nothing is written", async () => {
    const queryClient = new QueryClient();
    const [hold, release] = gate();
    const pending = sendLibraryChange(queryClient, source({ kind: "done", item: ITEM }, hold), PAPER, "add");
    // 甲的加入還在路上：甲登出、乙登入（清快取），乙讀到同一篇（不在乙的文獻庫）。
    await clearMemberQueries(queryClient);
    queryClient.setQueryData(LIST_KEY, list(false));
    release();
    expect(await pending).toBe("failed");
    expect(queryClient.getQueryData<ReturnType<typeof list>>(LIST_KEY)!.list.items[0]!.in_my_library).toBe(false);
  });

  it("remove: the answer comes back after the cache was cleared (logged out, someone else logged in): nothing is written", async () => {
    // 2026-11-20 第二版：找論文也能移出（決定第 87 項）。甲的移出還在路上：甲登出、乙登入（清快取），乙的文獻庫也有這篇。
    const queryClient = new QueryClient();
    const [hold, release] = gate();
    const pending = sendLibraryChange(queryClient, source({ kind: "done", item: ITEM }, hold), PAPER, "remove");
    await clearMemberQueries(queryClient);
    queryClient.setQueryData(LIST_KEY, list(true));
    release();
    expect(await pending).toBe("failed");
    expect(queryClient.getQueryData<ReturnType<typeof list>>(LIST_KEY)!.list.items[0]!.in_my_library).toBe(true);
  });

  it("remove answered 404 after the cache was cleared: nothing is written", async () => {
    const queryClient = new QueryClient();
    const [hold, release] = gate();
    const pending = sendLibraryChange(queryClient, source({ kind: "notFound" }, hold), PAPER, "remove");
    await clearMemberQueries(queryClient);
    queryClient.setQueryData(LIST_KEY, list(true));
    release();
    expect(await pending).toBe("failed");
    expect(queryClient.getQueryData<ReturnType<typeof list>>(LIST_KEY)!.list.items[0]!.in_my_library).toBe(true);
  });

  it("a 401 that comes back after the cache was cleared is not treated as this member's sign-out", async () => {
    const queryClient = new QueryClient();
    const [hold, release] = gate();
    const toLogin = vi.fn();
    const pending = sendLibraryChange(queryClient, source({ kind: "signedOut" }, hold), PAPER, "remove", toLogin);
    await clearMemberQueries(queryClient);
    release();
    expect(await pending).toBe("failed");
    expect(toLogin).not.toHaveBeenCalled();
  });

  it("a 401 for this member: the cache is cleared, then back to the login page", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(LIST_KEY, list(true));
    const toLogin = vi.fn(() => {
      // 回登入頁時快取已經清掉。
      expect(queryClient.getQueryData(LIST_KEY)).toBeUndefined();
    });
    expect(await sendLibraryChange(queryClient, source({ kind: "signedOut" }), PAPER, "remove", toLogin)).toBe("failed");
    expect(toLogin).toHaveBeenCalledTimes(1);
    expect(memberGeneration(queryClient)).toBe(1);
  });

  it("someone else signs in while the 401 is clearing the cache: no trip to the login page", async () => {
    // 審查 G1：清快取要等（取消進行中的查詢）；這段期間乙登入成功（登入成功也會清一次），不能把乙踢回登入頁。
    const queryClient = new QueryClient();
    const cancel = queryClient.cancelQueries.bind(queryClient);
    vi.spyOn(queryClient, "cancelQueries").mockImplementationOnce(async (...args) => {
      await cancel(...args);
      await clearMemberQueries(queryClient);
    });
    const toLogin = vi.fn();
    expect(await sendLibraryChange(queryClient, source({ kind: "signedOut" }), PAPER, "remove", toLogin)).toBe("failed");
    expect(toLogin).not.toHaveBeenCalled();
    expect(memberGeneration(queryClient)).toBe(2);
  });

  it.each(["add", "remove"] as const)(
    "%s: someone else signs in while waiting to write (cancelling list reads): nothing is written",
    async (action) => {
      // 審查 G1：回應回來時還是甲；寫快取前要先取消進行中的清單讀取，這段期間甲登出、乙登入，乙讀到同一篇。
      const queryClient = new QueryClient();
      const before = action === "add" ? false : true;
      queryClient.setQueryData(LIST_KEY, list(before));
      const cancel = queryClient.cancelQueries.bind(queryClient);
      vi.spyOn(queryClient, "cancelQueries").mockImplementationOnce(async (...args) => {
        await cancel(...args);
        await clearMemberQueries(queryClient);
        queryClient.setQueryData(LIST_KEY, list(before));
      });
      expect(await sendLibraryChange(queryClient, source({ kind: "done", item: ITEM }), PAPER, action)).toBe("failed");
      expect(queryClient.getQueryData<ReturnType<typeof list>>(LIST_KEY)!.list.items[0]!.in_my_library).toBe(before);
    },
  );

  it("remove answered 404, someone else signs in while waiting to write: nothing is written", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(LIST_KEY, list(true));
    const cancel = queryClient.cancelQueries.bind(queryClient);
    vi.spyOn(queryClient, "cancelQueries").mockImplementationOnce(async (...args) => {
      await cancel(...args);
      await clearMemberQueries(queryClient);
      queryClient.setQueryData(LIST_KEY, list(true));
    });
    expect(await sendLibraryChange(queryClient, source({ kind: "notFound" }), PAPER, "remove")).toBe("failed");
    expect(queryClient.getQueryData<ReturnType<typeof list>>(LIST_KEY)!.list.items[0]!.in_my_library).toBe(true);
  });

  it("removing what is already gone (404): not in the library, but no undo", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(LIST_KEY, list(true));
    expect(await sendLibraryChange(queryClient, source({ kind: "notFound" }), PAPER, "remove")).toBe("alreadyGone");
    expect(queryClient.getQueryData<ReturnType<typeof list>>(LIST_KEY)!.list.items[0]!.in_my_library).toBe(false);
  });

  it.each(["add", "restore"] as const)("%s answered 404 fails and changes nothing", async (action) => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(LIST_KEY, list(false));
    expect(await sendLibraryChange(queryClient, source({ kind: "notFound" }), PAPER, action)).toBe("failed");
    expect(queryClient.getQueryData<ReturnType<typeof list>>(LIST_KEY)!.list.items[0]!.in_my_library).toBe(false);
  });

  it("removed: done, the paper is marked not in the library", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(LIST_KEY, list(true));
    expect(await sendLibraryChange(queryClient, source({ kind: "done", item: ITEM }), PAPER, "remove")).toBe("done");
    expect(queryClient.getQueryData<ReturnType<typeof list>>(LIST_KEY)!.list.items[0]!.in_my_library).toBe(false);
  });

  it("a network error fails", async () => {
    const queryClient = new QueryClient();
    const failing: LibrarySource = { ...source({ kind: "notFound" }), add: () => Promise.reject(new Error("offline")) };
    expect(await sendLibraryChange(queryClient, failing, PAPER, "add")).toBe("failed");
  });
});

describe("markInLibrary", () => {
  it("a list read that started before the add does not put 'add' back when it finishes", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(LIST_KEY, list(false));
    const [hold, release] = gate();
    // 「再載入」或重讀：送出時伺服器還沒加入，讀回來是「不在」。
    const reading = queryClient
      .fetchQuery({
        queryKey: LIST_KEY,
        queryFn: async () => {
          await hold;
          return list(false);
        },
      })
      .catch(() => "cancelled");
    await markInLibrary(queryClient, PAPER, true);
    release();
    await reading;
    expect(queryClient.getQueryData<ReturnType<typeof list>>(LIST_KEY)!.list.items[0]!.in_my_library).toBe(true);
  });
});

describe("markInLibrary: lists with the scope 'my library'", () => {
  // 範圍「我的文獻庫」時按移出（Kevin 2026-11-20 決定）：那一列留在原地變回「加入」、篇數不變，所以那一份清單只標過期、不馬上重讀。
  const PERSONAL_KEY = ["paper-list", JSON.stringify([["scope", "personal"]]), 20];
  const TRACKED_KEY = ["paper-list", JSON.stringify([["topic", ["e_commerce"]]]), 20];

  /** 有一個畫面在用這一份（掛上觀察者）；回傳讀了幾次的計數與取消掛上的函式。 */
  function watch(queryClient: QueryClient, queryKey: unknown[], answer: () => ReturnType<typeof list>) {
    const queryFn = vi.fn(async () => answer());
    const observer = new QueryObserver(queryClient, { queryKey, queryFn, staleTime: Infinity, retry: false });
    const stop = observer.subscribe(() => {});
    return { queryFn, stop };
  }

  it("an active 'my library' list with data is marked stale but not read again; the row stays, now not in the library", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(PERSONAL_KEY, list(true));
    const { queryFn, stop } = watch(queryClient, PERSONAL_KEY, () => ({ ...list(false), list: { ...list(false).list, items: [], total: 0 } }));
    expect(await markInLibrary(queryClient, PAPER, false)).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(queryFn).not.toHaveBeenCalled();
    const data = queryClient.getQueryData<ReturnType<typeof list>>(PERSONAL_KEY)!;
    expect(data.list.items.map((item) => [item.id, item.in_my_library])).toEqual([[PAPER, false]]);
    expect(data.list.total).toBe(1);
    expect(queryClient.getQueryState(PERSONAL_KEY)!.isInvalidated).toBe(true);
    stop();
  });

  it("an active list with another scope is read again", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(TRACKED_KEY, list(true));
    const { queryFn, stop } = watch(queryClient, TRACKED_KEY, () => list(false));
    await markInLibrary(queryClient, PAPER, false);
    await vi.waitFor(() => expect(queryFn).toHaveBeenCalledTimes(1));
    stop();
  });

  it("an active 'my library' list without data (its first read was cancelled) is read again", async () => {
    const queryClient = new QueryClient();
    const [hold, release] = gate();
    let calls = 0;
    const queryFn = vi.fn(async () => {
      calls += 1;
      if (calls === 1) await hold;
      return list(false);
    });
    const observer = new QueryObserver(queryClient, { queryKey: PERSONAL_KEY, queryFn, retry: false });
    const stop = observer.subscribe(() => {});
    await vi.waitFor(() => expect(queryFn).toHaveBeenCalledTimes(1));
    await markInLibrary(queryClient, PAPER, false);
    release();
    await vi.waitFor(() => expect(queryFn).toHaveBeenCalledTimes(2));
    stop();
  });
});

describe("paperListQuery: lists with the scope 'my library'", () => {
  // 移出後只標過期的那一份，切回視窗或重新連線時也不能自動重讀（不然那一列會在換條件、重新整理之前消失）。
  // 沒有暫留列的照預設：過期了就重讀（審查 R2）。
  it("does not read again on window focus or reconnect only while rows removed here are held; other scopes keep the defaults", () => {
    const queryClient = new QueryClient();
    const personal = paperListQuery(queryClient, { scope: "personal" } as PaperListFilters, 20);
    const held = { queryKey: personal.queryKey, state: { data: { kind: "found", list: { ...list(false).list } } } };
    const none = { queryKey: personal.queryKey, state: { data: list(true) } };
    const focus = personal.refetchOnWindowFocus as (query: unknown) => boolean;
    const reconnect = personal.refetchOnReconnect as (query: unknown) => boolean;
    expect(focus(held)).toBe(false);
    expect(reconnect(held)).toBe(false);
    expect(focus(none)).toBe(true);
    expect(reconnect(none)).toBe(true);
    const tracked = paperListQuery(queryClient, { topic: ["e_commerce"] } as unknown as PaperListFilters, 20);
    expect(tracked.refetchOnWindowFocus).toBeUndefined();
    expect(tracked.refetchOnReconnect).toBeUndefined();
  });

  it("recognises the key built from the search page's own filters", () => {
    expect(isPersonalList(["paper-list", filtersKey(toApiParams({ scope: "personal" }))])).toBe(true);
    expect(isPersonalList(["paper-list", filtersKey(toApiParams({ scope: "personal", topic: "e_commerce" }))])).toBe(true);
    expect(isPersonalList(["paper-list", filtersKey(toApiParams({}))])).toBe(false);
  });

  it("load more after a remove: the total counts the removed rows still listed (server total + rows carried over that are out)", async () => {
    const queryClient = new QueryClient();
    const filters = toApiParams({ scope: "personal" });
    // 第一頁 3 篇（第 2 篇移出了、留在原地），pending 1 篇也移出了；伺服器現在說一共 5 篇。
    const previous = {
      items: [row("a", true), row("b", false), row("c", true)],
      pending: [row("d", false)],
      next_cursor: "c1",
      total: 7,
    } as unknown as PaperList;
    queryClient.setQueryData(["paper-list", filtersKey(filters), 3], { kind: "found", list: previous });
    const fresh = { items: [...previous.items, previous.pending[0]!, row("e", true)], pending: [], next_cursor: null, total: 5 } as unknown as PaperList;
    const list = vi.spyOn(datasource.papers!, "list").mockResolvedValue({ kind: "found", list: fresh });
    try {
      const options = paperListQuery(queryClient, filters, 5);
      const result = (await (options.queryFn as () => Promise<{ kind: "found"; list: PaperList }>)()).list;
      expect(list).toHaveBeenCalledWith(filters, 5, previous);
      expect(result.total).toBe(5 + 2);
      expect(result.items).toEqual(fresh.items);
    } finally {
      list.mockRestore();
    }
  });

  it("load more in another scope keeps the server total", async () => {
    const queryClient = new QueryClient();
    const filters = toApiParams({});
    const previous = { items: [row("a", false)], pending: [], next_cursor: "c1", total: 9 } as unknown as PaperList;
    queryClient.setQueryData(["paper-list", filtersKey(filters), 1], { kind: "found", list: previous });
    const fresh = { items: [row("a", false), row("b", false)], pending: [], next_cursor: null, total: 4 } as unknown as PaperList;
    const list = vi.spyOn(datasource.papers!, "list").mockResolvedValue({ kind: "found", list: fresh });
    try {
      const result = (await (paperListQuery(queryClient, filters, 2).queryFn as () => Promise<{ kind: "found"; list: PaperList }>)()).list;
      expect(result.total).toBe(4);
    } finally {
      list.mockRestore();
    }
  });
});

describe("libraryShown", () => {
  const page = (inLibrary: boolean) => ({ paper: { id: PAPER }, in_my_library: inLibrary }) as unknown as PaperPage;

  it("follows the API until something is pressed", () => {
    expect(libraryShown(page(true), null)).toBe(true);
    expect(libraryShown(page(false), null)).toBe(false);
  });

  it("after pressing, shows the result for the same page data", () => {
    const current = page(false);
    expect(libraryShown(current, { page: current, inLibrary: true })).toBe(true);
  });

  it("new page data (another paper, or read again) shows the API value, not the old press", () => {
    const before = page(false);
    expect(libraryShown(page(false), { page: before, inLibrary: true })).toBe(false);
    expect(libraryShown(page(true), { page: before, inLibrary: false })).toBe(true);
  });
});

// PR #114 審查 R1、R2：走真的接續讀取（datasource 的 list 照 next_cursor 打 GET /api/v1/papers；fetch 換成假的伺服器），
// 不把讀取函式整個換掉。
describe("paperListQuery 'my library': the real continuation path against a fake server", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    focusManager.setFocused(undefined);
  });

  const filters = toApiParams({ scope: "personal" });

  /**
   * 假的伺服器：`library` 是現在在文獻庫裡的論文（依序）；一頁 `pageSize` 篇，cursor 是「接在哪一篇後面」（像後端的 keyset，
   * 移出前面的不會讓後面漏掉）。`after` 可以改寫某個 cursor 之後那一頁（例如別的分頁加回去、排在後面）。
   * GET /api/v1/papers/{id} 回 in_my_library。
   */
  function server(pageSize: number) {
    const state = { library: [] as string[], after: {} as Record<string, string[]>, paperFails: false };
    const calls: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://test");
      calls.push(`${url.pathname}${url.search}`);
      const reply = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
      const one = /^\/api\/v1\/papers\/(.+)$/.exec(url.pathname);
      if (one !== null) {
        if (state.paperFails) return new Response("{}", { status: 500, headers: { "content-type": "application/json" } });
        const id = decodeURIComponent(one[1]!);
        return reply({ paper: { id }, in_my_library: state.library.includes(id) });
      }
      const cursor = url.searchParams.get("cursor");
      const start = cursor === null ? 0 : state.library.indexOf(cursor) + 1;
      const ids = cursor !== null && state.after[cursor] !== undefined ? state.after[cursor]! : state.library.slice(start, start + pageSize);
      const last = ids.at(-1);
      const next = last !== undefined && state.library.indexOf(last) < state.library.length - 1 ? last : null;
      return reply({ items: ids.map((id) => row(id, true)), next_cursor: next, total: state.library.length, snapshot: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    return { state, calls };
  }

  type Found = { kind: "found"; list: PaperList };
  const read = async (queryClient: QueryClient, limit: number) =>
    ((await queryClient.fetchQuery({ ...paperListQuery(queryClient, filters, limit), staleTime: 0 })) as Found).list;
  const ids = (list: PaperList) => list.items.map((item) => item.id);

  it("load more, remove, switch the scope and back: read again from the server (not from the longer list), the removed row is gone", async () => {
    const queryClient = new QueryClient();
    const { state, calls } = server(2);
    state.library = ["a", "b", "c", "d"];
    await read(queryClient, 2);
    expect(ids(await read(queryClient, 4))).toEqual(["a", "b", "c", "d"]);
    // 移出 a（伺服器剩 3 篇），兩份都留著 a（in_my_library false）。
    state.library = ["b", "c", "d"];
    await markInLibrary(queryClient, "a", false);
    calls.length = 0;
    // 換回原條件：要 2 篇的那一份重讀。
    const back = await read(queryClient, 2);
    expect(ids(back)).toEqual(["b", "c"]);
    expect(back.total).toBe(3);
    expect(calls).toEqual(["/api/v1/papers?scope=personal"]);
  });

  it("load more after a remove: the removed row stays, the total counts it once (server total + held rows)", async () => {
    const queryClient = new QueryClient();
    const { state, calls } = server(2);
    state.library = ["a", "b", "c", "d"];
    await read(queryClient, 2);
    state.library = ["b", "c", "d"];
    await markInLibrary(queryClient, "a", false);
    calls.length = 0;
    const more = await read(queryClient, 4);
    expect(more.items.map((item) => [item.id, item.in_my_library])).toEqual([["a", false], ["b", true], ["c", true], ["d", true]]);
    expect(more.total).toBe(4);
    expect(calls).toEqual(["/api/v1/papers?scope=personal&cursor=b", "/api/v1/papers/a"]);
  });

  it("load more after a remove, but another tab added it back: counted once, the row shows 'added' again, no duplicate row", async () => {
    const queryClient = new QueryClient();
    const { state } = server(2);
    state.library = ["a", "b", "c", "d"];
    await read(queryClient, 2);
    state.library = ["b", "c", "d"];
    await markInLibrary(queryClient, "a", false);
    // 另一個分頁把 a 加回去（伺服器又是 4 篇），而且排在後面：b 之後那一頁是 c、d、a。
    state.library = ["b", "c", "d", "a"];
    state.after = { b: ["c", "d", "a"] };
    const more = await read(queryClient, 5);
    expect(more.items.map((item) => [item.id, item.in_my_library])).toEqual([["a", true], ["b", true], ["c", true], ["d", true]]);
    expect(more.pending).toEqual([]);
    expect(more.total).toBe(4);
  });

  it("load more after a remove, another tab added it back, and asking the server about it fails: the total matches the rows shown (C3)", async () => {
    const queryClient = new QueryClient();
    const { state, calls } = server(2);
    state.library = ["a", "b", "c", "d"];
    await read(queryClient, 2);
    state.library = ["b", "c", "d"];
    await markInLibrary(queryClient, "a", false);
    state.library = ["b", "c", "d", "a"];
    state.after = { b: ["c", "d", "a"] };
    state.paperFails = true;
    calls.length = 0;
    const more = await read(queryClient, 5);
    expect(calls).toEqual(["/api/v1/papers?scope=personal&cursor=b", "/api/v1/papers/a"]);
    // 新的一頁裡又有 a：伺服器已經算它，照畫面只留一列、改回「已加入」，篇數不多算。
    expect(more.items.map((item) => [item.id, item.in_my_library])).toEqual([["a", true], ["b", true], ["c", true], ["d", true]]);
    expect(more.pending).toEqual([]);
    expect(more.next_cursor).toBeNull();
    expect(more.total).toBe(4);
  });

  it("load more after a remove and asking the server fails, the row is not in the new page: still held and counted once (C3)", async () => {
    const queryClient = new QueryClient();
    const { state } = server(2);
    state.library = ["a", "b", "c", "d"];
    await read(queryClient, 2);
    state.library = ["b", "c", "d"];
    await markInLibrary(queryClient, "a", false);
    state.paperFails = true;
    const more = await read(queryClient, 4);
    expect(more.items.map((item) => [item.id, item.in_my_library])).toEqual([["a", false], ["b", true], ["c", true], ["d", true]]);
    expect(more.total).toBe(4);
  });

  it("nothing new to read (the list was already read to the end): the total is not counted twice", async () => {
    const queryClient = new QueryClient();
    const { state, calls } = server(2);
    state.library = ["a", "b"];
    await read(queryClient, 2);
    state.library = ["b"];
    await markInLibrary(queryClient, "a", false);
    calls.length = 0;
    const more = await read(queryClient, 4);
    expect(ids(more)).toEqual(["a", "b"]);
    expect(more.total).toBe(2);
    expect(calls).toEqual([]);
  });

  it("window focus: a stale 'my library' list with nothing held is read again (another tab added a paper, or someone else signed in)", async () => {
    const queryClient = new QueryClient();
    // mount：讓切回視窗、重新連線的事件傳到查詢（App 裡 QueryClientProvider 會做）。
    queryClient.mount();
    const { state, calls } = server(20);
    state.library = ["a", "b"];
    const observer = new QueryObserver(queryClient, { ...paperListQuery(queryClient, filters, 20), staleTime: 0 });
    const stop = observer.subscribe(() => {});
    await vi.waitFor(() => expect(observer.getCurrentResult().data).toBeDefined());
    expect(calls).toHaveLength(1);
    // 另一個分頁：同一位加了 c；或換了人，伺服器回的是另一位的文獻庫。
    state.library = ["x"];
    focusManager.setFocused(false);
    focusManager.setFocused(true);
    await vi.waitFor(() => expect(ids((observer.getCurrentResult().data as Found).list)).toEqual(["x"]));
    expect(calls).toHaveLength(2);
    stop();
    queryClient.unmount();
  });

  it("window focus: a 'my library' list holding a row removed here is not read again", async () => {
    const queryClient = new QueryClient();
    // mount：讓切回視窗、重新連線的事件傳到查詢（App 裡 QueryClientProvider 會做）。
    queryClient.mount();
    const { state, calls } = server(20);
    state.library = ["a", "b"];
    const observer = new QueryObserver(queryClient, { ...paperListQuery(queryClient, filters, 20), staleTime: 0 });
    const stop = observer.subscribe(() => {});
    await vi.waitFor(() => expect(observer.getCurrentResult().data).toBeDefined());
    expect(calls).toHaveLength(1);
    state.library = ["b"];
    await markInLibrary(queryClient, "a", false);
    focusManager.setFocused(false);
    focusManager.setFocused(true);
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(calls).toHaveLength(1);
    expect((observer.getCurrentResult().data as Found).list.items.map((item) => [item.id, item.in_my_library])).toEqual([["a", false], ["b", true]]);
    stop();
    queryClient.unmount();
  });

  it("adding in a 'my library' list with nothing held left: the active list is read again", async () => {
    const queryClient = new QueryClient();
    // mount：讓切回視窗、重新連線的事件傳到查詢（App 裡 QueryClientProvider 會做）。
    queryClient.mount();
    const { state, calls } = server(20);
    state.library = ["a", "b"];
    const observer = new QueryObserver(queryClient, paperListQuery(queryClient, filters, 20));
    const stop = observer.subscribe(() => {});
    await vi.waitFor(() => expect(observer.getCurrentResult().data).toBeDefined());
    expect(calls).toHaveLength(1);
    state.library = ["b"];
    await markInLibrary(queryClient, "a", false);
    // 同一列又按了加入：沒有暫留列了，照一般規則重讀。
    state.library = ["a", "b"];
    await markInLibrary(queryClient, "a", true);
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    stop();
    queryClient.unmount();
  });
});

// epic-3h PR 3（ADR-0055）：寫入帶「這個分頁以為的成員」；後端說換了人（memberChanged）時不改快取、不重試、回 "failed"，
// 交給「整頁換成現在的人」（followMemberChange）。
describe("writes carry the member this tab expects (ADR-0055)", () => {
  const A = { member_id: "00000000-0000-4000-8000-00000000000a", display_name: "Test Member A" };
  const B = { member_id: "00000000-0000-4000-8000-00000000000b", display_name: "Test Member B" };

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each(["add", "remove", "restore"] as const)("%s: the expected member goes to the source", async (action) => {
    const queryClient = new QueryClient();
    const call = vi.fn<LibrarySource["add"]>(async () => ({ kind: "done", item: ITEM }));
    const recording: LibrarySource = { list: () => Promise.reject(new Error("not used")), add: call, remove: call, restore: call };
    await sendLibraryChange(queryClient, recording, PAPER, action, () => {}, { expectedMemberId: A.member_id });
    expect(call.mock.calls).toEqual([[PAPER, A.member_id]]);
  });

  it("memberChanged: the cache is not touched, no retry, 'failed', and the page follows the change once", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(LIST_KEY, list(true));
    const call = vi.fn(async (): Promise<LibraryChangeResult> => ({ kind: "memberChanged" }));
    const recording: LibrarySource = { list: () => Promise.reject(new Error("not used")), add: call, remove: call, restore: call };
    const onMemberChanged = vi.fn();
    const toLogin = vi.fn();
    const outcome = await sendLibraryChange(queryClient, recording, PAPER, "remove", toLogin, { expectedMemberId: A.member_id, onMemberChanged });
    expect(outcome).toBe("failed");
    expect(call).toHaveBeenCalledTimes(1);
    expect(onMemberChanged).toHaveBeenCalledTimes(1);
    expect(toLogin).not.toHaveBeenCalled();
    expect(queryClient.getQueryData<ReturnType<typeof list>>(LIST_KEY)!.list.items[0]!.in_my_library).toBe(true);
  });

  it("memberChanged that comes back after the cache was already cleared: nothing more to do", async () => {
    const queryClient = new QueryClient();
    const [hold, release] = gate();
    const onMemberChanged = vi.fn();
    const pending = sendLibraryChange(queryClient, source({ kind: "memberChanged" }, hold), PAPER, "add", () => {}, { onMemberChanged });
    await clearMemberQueries(queryClient);
    release();
    expect(await pending).toBe("failed");
    expect(onMemberChanged).not.toHaveBeenCalled();
  });

  it("followMemberChange: B answers 'who am I': A's cache is cleared, the page is swapped, then reloaded", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    queryClient.setQueryData(LIST_KEY, list(true));
    vi.spyOn(datasource.session!, "me").mockResolvedValue(B);
    const reload = vi.fn(() => {
      expect(queryClient.getQueryData(LIST_KEY)).toBeUndefined();
    });
    const toLogin = vi.fn();
    await followMemberChange(queryClient, { toLogin, reload });
    expect(reload).toHaveBeenCalledTimes(1);
    expect(toLogin).not.toHaveBeenCalled();
    expect(queryClient.getQueryData(meQuery.queryKey)).toEqual(B);
    expect(memberGeneration(queryClient)).toBe(1);
  });

  it("followMemberChange: 'who am I' already says B in the cache: still cleared (the backend said this page is A's)", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, B);
    queryClient.setQueryData(LIST_KEY, list(true));
    vi.spyOn(datasource.session!, "me").mockResolvedValue(B);
    await followMemberChange(queryClient, { toLogin: () => {}, reload: () => {} });
    expect(queryClient.getQueryData(LIST_KEY)).toBeUndefined();
    expect(queryClient.getQueryData(meQuery.queryKey)).toEqual(B);
    expect(memberGeneration(queryClient)).toBe(1);
  });

  it("followMemberChange: nobody is signed in: cleared, then back to the login page (no reload)", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    queryClient.setQueryData(LIST_KEY, list(true));
    vi.spyOn(datasource.session!, "me").mockResolvedValue(null);
    const toLogin = vi.fn();
    const reload = vi.fn();
    await followMemberChange(queryClient, { toLogin, reload });
    expect(toLogin).toHaveBeenCalledTimes(1);
    expect(reload).not.toHaveBeenCalled();
    expect(queryClient.getQueryData(LIST_KEY)).toBeUndefined();
  });

  it("followMemberChange: 'who am I' fails: still cleared, then reloaded (the page shows load-failed or the new member)", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    queryClient.setQueryData(LIST_KEY, list(true));
    vi.spyOn(datasource.session!, "me").mockRejectedValue(new Error("network"));
    const reload = vi.fn();
    await followMemberChange(queryClient, { toLogin: () => {}, reload });
    expect(reload).toHaveBeenCalledTimes(1);
    expect(queryClient.getQueryData(LIST_KEY)).toBeUndefined();
    expect(memberGeneration(queryClient)).toBe(1);
  });

  it("followMemberChange: several 409s at once follow the change only once", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    const me = vi.spyOn(datasource.session!, "me").mockResolvedValue(B);
    const reload = vi.fn();
    await Promise.all([1, 2, 3].map(() => followMemberChange(queryClient, { toLogin: () => {}, reload })));
    expect(me).toHaveBeenCalledTimes(1);
    expect(reload).toHaveBeenCalledTimes(1);
  });
});
