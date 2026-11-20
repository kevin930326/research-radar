// 加入、移出、復原（Story 3.17）：回應回來之前換了人就不改快取（審查 F2）、移出時已經不在（404）不給復原
// （審查 F3）、論文頁換了一份資料就照新的顯示（審查 F9）、清單讀到一半時加入不會被讀完的舊資料蓋回去（審查 F10）。
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import type { LibraryChangeResult, LibraryItem, LibrarySource, PaperList, PaperPage } from "../datasource/types";
import { libraryShown, sendLibraryChange } from "./library-change";
import { markInLibrary } from "./paper-queries";
import { clearMemberQueries } from "./session";

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

  it("a 401 that comes back after the cache was cleared is not treated as this member's sign-out", async () => {
    const queryClient = new QueryClient();
    const [hold, release] = gate();
    const pending = sendLibraryChange(queryClient, source({ kind: "signedOut" }, hold), PAPER, "remove");
    await clearMemberQueries(queryClient);
    release();
    expect(await pending).toBe("failed");
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
