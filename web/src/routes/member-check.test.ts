// 找論文切回視窗時的確認（PR #114 收尾，epic-3l PR C）：確認中途丟例外時，等著的加入／移出不能永遠停在「送出中」（G5）；
// focus 與 visibilitychange 一起來時只確認一次（S4）。不用畫面：直接呼叫切回視窗時跑的那個函式（memberCheckOnReturn）。
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { datasource } from "../datasource";
import type { LibrarySource, PaperList } from "../datasource/types";
import { toApiParams } from "../pages/search-filters";
import { sendLibraryChange } from "./library-change";
import { memberCheckOnReturn } from "./member";
import { filtersKey, paperListKey } from "./paper-queries";
import { meQuery, pendingMemberCheck } from "./session";

const A = { member_id: "00000000-0000-4000-8000-00000000000a", display_name: "Test Member A" };
const B = { member_id: "00000000-0000-4000-8000-00000000000b", display_name: "Test Member B" };
const PAPER = "00000000-0000-4000-8000-000000000001";

/** 範圍「我的文獻庫」、有一列本頁移出暫留的清單，畫面正在用（有訂閱）：切回視窗才會確認。回傳停止訂閱的函式。 */
function holdRow(queryClient: QueryClient): () => void {
  const key = paperListKey(filtersKey(toApiParams({ scope: "personal" })), 20, A.member_id);
  const row = { id: PAPER, title: "Fake paper", in_my_library: false } as unknown as PaperList["items"][number];
  queryClient.setQueryData(key, { kind: "found", list: { items: [row], pending: [], next_cursor: null, total: 1 }, readAs: A.member_id });
  const observer = new QueryObserver(queryClient, { queryKey: key, queryFn: () => new Promise<never>(() => {}), staleTime: Infinity });
  return observer.subscribe(() => {});
}

function addSource(): LibrarySource & { add: ReturnType<typeof vi.fn> } {
  const add = vi.fn(async () => ({ kind: "done" as const, item: {} as never }));
  return { list: () => Promise.reject(new Error("not used")), add, remove: add, restore: add };
}

describe("checking the member when coming back to the window (#114 follow-up)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("G5: re-mounting the page throws after another member was found: a waiting add gives up ('failed') and is not sent", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    const stop = holdRow(queryClient);
    vi.spyOn(datasource.session!, "me").mockResolvedValue(B);
    const confirm = memberCheckOnReturn(queryClient, {
      swap: () => {
        throw new Error("render failed");
      },
      toLogin: () => {},
      reload: () => {},
    });
    const job = confirm();
    const source = addSource();
    const sent = sendLibraryChange(queryClient, source, PAPER, "add", () => {}, { expectedMemberId: A.member_id });
    await job.catch(() => {});
    // 修之前：確認永遠沒有結果，這一行一直等不到（測試逾時）。
    expect(await sent).toBe("failed");
    expect(source.add).not.toHaveBeenCalled();
    expect(pendingMemberCheck(queryClient)).toBeUndefined();
    stop();
  });

  it("G5: clearing the cache throws after 'who am I' says nobody: a waiting add gives up ('failed') and is not sent", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    const stop = holdRow(queryClient);
    vi.spyOn(datasource.session!, "me").mockResolvedValue(null);
    vi.spyOn(queryClient, "cancelQueries").mockRejectedValue(new Error("cancel failed"));
    const toLogin = vi.fn();
    const job = memberCheckOnReturn(queryClient, { swap: () => {}, toLogin, reload: () => {} })();
    const source = addSource();
    const sent = sendLibraryChange(queryClient, source, PAPER, "add", () => {}, { expectedMemberId: A.member_id });
    await job.catch(() => {});
    expect(await sent).toBe("failed");
    expect(source.add).not.toHaveBeenCalled();
    stop();
  });

  it("S4: focus and visibilitychange come together: 'who am I' is asked once, the page is re-mounted once", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    const stop = holdRow(queryClient);
    const me = vi.spyOn(datasource.session!, "me").mockResolvedValue(B);
    const swap = vi.fn();
    const reload = vi.fn();
    const confirm = memberCheckOnReturn(queryClient, { swap, toLogin: () => {}, reload });
    // 切回視窗時兩個事件連著來（同一次切回）。
    await Promise.all([confirm(), confirm()]);
    expect(me).toHaveBeenCalledTimes(1);
    expect(swap).toHaveBeenCalledTimes(1);
    expect(reload).toHaveBeenCalledTimes(1);
    stop();
  });

  it("S4: a later return to the window (after the first check is done) checks again", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    const stop = holdRow(queryClient);
    const me = vi.spyOn(datasource.session!, "me").mockResolvedValue(A);
    const confirm = memberCheckOnReturn(queryClient, { swap: () => {}, toLogin: () => {}, reload: () => {} });
    await confirm();
    await confirm();
    expect(me).toHaveBeenCalledTimes(2);
    stop();
  });
});
