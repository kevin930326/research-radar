// 加入、移出、復原（Story 3.17）：回應回來之前換了人就不改快取（審查 F2）、寫快取前與處理 401 時換了人也一樣（審查 G1）、移出時已經不在（404）不給復原
// （審查 F3）、論文頁換了一份資料就照新的顯示（審查 F9）、清單讀到一半時加入不會被讀完的舊資料蓋回去（審查 F10）。
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import type { LibraryChangeResult, LibraryItem, LibrarySource, PaperList, PaperPage } from "../datasource/types";
import { libraryShown, sendLibraryChange } from "./library-change";
import { markInLibrary } from "./paper-queries";
import { clearMemberQueries, memberGeneration } from "./session";

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
