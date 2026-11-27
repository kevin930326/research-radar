// 找論文切回視窗時的確認（PR #114 收尾，epic-3l PR C）：確認中途丟例外時，等著的加入／移出不能永遠停在「送出中」（G5）；
// focus 與 visibilitychange 一起來時只確認一次（S4）；同一次切回只確認一次、離開再回來才是新的一次（X3）；確認不會 reject（X4）；
// 認出換人之後清快取失敗也當成換了人（X1）。不用畫面：直接呼叫切回視窗時跑的那個函式（memberCheckOnReturn）。
import { QueryClient, QueryObserver, onlineManager } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { datasource } from "../datasource";
import type { LibrarySource, PaperList } from "../datasource/types";
import { toApiParams } from "../pages/search-filters";
import { sendLibraryChange } from "./library-change";
import { memberCheckOnReturn } from "./member";
import { filtersKey, paperListKey } from "./paper-queries";
import { meQuery, memberGeneration, pendingMemberCheck } from "./session";

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
    const { confirm } = memberCheckOnReturn(queryClient, {
      swap: () => {
        throw new Error("render failed");
      },
      toLogin: () => {},
      reload: () => {},
    });
    const job = confirm("focus");
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
    const job = memberCheckOnReturn(queryClient, { swap: () => {}, toLogin, reload: () => {} }).confirm("focus");
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
    const { confirm } = memberCheckOnReturn(queryClient, { swap, toLogin: () => {}, reload });
    // 切回視窗時兩個事件連著來（同一次切回）。
    await Promise.all([confirm("visible"), confirm("focus")]);
    expect(me).toHaveBeenCalledTimes(1);
    expect(swap).toHaveBeenCalledTimes(1);
    expect(reload).toHaveBeenCalledTimes(1);
    stop();
  });

  it("S4: a later return to the window (left the window after the first check) checks again", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    const stop = holdRow(queryClient);
    const me = vi.spyOn(datasource.session!, "me").mockResolvedValue(A);
    const { confirm, leave } = memberCheckOnReturn(queryClient, { swap: () => {}, toLogin: () => {}, reload: () => {} });
    await confirm("visible");
    // 分頁藏起來（epic-3l C 審查 X3：沒有離開就不算新的一次切回）。
    leave();
    await confirm("visible");
    expect(me).toHaveBeenCalledTimes(2);
    stop();
  });

  it("X3: the second event of the same return comes late (the first check is already done): not checked again", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    const stop = holdRow(queryClient);
    const me = vi.spyOn(datasource.session!, "me").mockResolvedValue(A);
    const { confirm } = memberCheckOnReturn(queryClient, { swap: () => {}, toLogin: () => {}, reload: () => {} });
    await confirm("visible");
    // 修之前：第一次做完就放掉，晚到的第二個事件又問一次（2 次）。
    await confirm("focus");
    expect(me).toHaveBeenCalledTimes(1);
    stop();
  });

  it("X3: left and came back again while the first check is still waiting: a new check starts (writes wait for it)", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    const stop = holdRow(queryClient);
    let answer: (member: typeof A) => void = () => {};
    vi.spyOn(datasource.session!, "me").mockReturnValue(new Promise((done) => (answer = done)));
    const { confirm, leave } = memberCheckOnReturn(queryClient, { swap: () => {}, toLogin: () => {}, reload: () => {} });
    const first = confirm("visible");
    const firstCheck = pendingMemberCheck(queryClient);
    expect(firstCheck).toBeDefined();
    leave();
    const second = confirm("visible");
    // 修之前：上一次還在等就直接略過，記著的還是第一次。
    const secondCheck = pendingMemberCheck(queryClient);
    expect(secondCheck).not.toBe(firstCheck);
    // 同一次切回的 focus：不再確認。
    const third = confirm("focus");
    expect(pendingMemberCheck(queryClient)).toBe(secondCheck);
    // 切到別的程式再切回來（又一個 focus；視窗要先失焦才會再 focus）：新的一次。
    const fourth = confirm("focus");
    expect(pendingMemberCheck(queryClient)).not.toBe(secondCheck);
    answer(A);
    await Promise.all([first, second, third, fourth]);
    stop();
  });

  it("X1: 'who am I' says B and clearing A's cache throws: a waiting add gives up ('failed') and is not sent", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    const stop = holdRow(queryClient);
    vi.spyOn(datasource.session!, "me").mockResolvedValue(B);
    // loadMember 認出乙之後清甲的快取（clearMemberQueries）：取消進行中的查詢那一步失敗。
    vi.spyOn(queryClient, "cancelQueries").mockRejectedValue(new Error("cancel failed"));
    const job = memberCheckOnReturn(queryClient, { swap: () => {}, toLogin: () => {}, reload: () => {} }).confirm("focus");
    const source = addSource();
    const sent = sendLibraryChange(queryClient, source, PAPER, "add", () => {}, { expectedMemberId: A.member_id });
    await job;
    // 修之前：當成「問不到」，帶甲的 readAs 送出（後端會擋，但不該送）。
    expect(await sent).toBe("failed");
    expect(source.add).not.toHaveBeenCalled();
    stop();
  });

  it("X4: a check that throws does not reject (nothing left unhandled at the event), and the next return checks again", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    const stop = holdRow(queryClient);
    const me = vi.spyOn(datasource.session!, "me").mockResolvedValue(B);
    const { confirm, leave } = memberCheckOnReturn(queryClient, {
      swap: () => {
        throw new Error("render failed");
      },
      toLogin: () => {},
      reload: () => {},
    });
    // 修之前：reject（瀏覽器入口是 `void confirm()`，變成沒接住的錯誤）。
    await expect(confirm("focus")).resolves.toBeUndefined();
    leave();
    // 換了人之後快取的「我是誰」已經是乙；再放一列暫留列，下一次切回照樣確認。
    const stopAgain = holdRow(queryClient);
    await expect(confirm("visible")).resolves.toBeUndefined();
    expect(me).toHaveBeenCalledTimes(2);
    stopAgain();
    stop();
  });
  it("N1: left and came back while the first 'who am I' is still out: asked again (B), no add is sent, the page is re-mounted once", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    const stop = holdRow(queryClient);
    // 第一次的請求在另一個分頁換成乙之前就送出了（答案是甲、還沒回來）；之後再問就是乙。
    let answerFirst: (member: typeof A) => void = () => {};
    const me = vi
      .spyOn(datasource.session!, "me")
      .mockReturnValueOnce(new Promise((done) => (answerFirst = done)))
      .mockResolvedValueOnce(B);
    const swap = vi.fn();
    const { confirm, leave } = memberCheckOnReturn(queryClient, { swap, toLogin: () => {}, reload: () => {} });
    const source = addSource();
    const first = confirm("visible");
    const early = sendLibraryChange(queryClient, source, PAPER, "add", () => {}, { expectedMemberId: A.member_id });
    leave();
    const second = confirm("visible");
    const late = sendLibraryChange(queryClient, source, PAPER, "add", () => {}, { expectedMemberId: A.member_id });
    answerFirst(A);
    await Promise.all([first, second]);
    // 修之前：第二次併進第一次的請求（只問 1 次），拿到甲、判成同一位，兩個加入都帶甲送出。
    expect(me).toHaveBeenCalledTimes(2);
    expect(await early).toBe("failed");
    expect(await late).toBe("failed");
    expect(source.add).not.toHaveBeenCalled();
    expect(swap).toHaveBeenCalledTimes(1);
    expect(queryClient.getQueryData(meQuery.queryKey)).toEqual(B);
    stop();
  });

  it("N1: the first 'who am I' answers after the new check already found B: it does not overwrite B or re-mount again", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    const stop = holdRow(queryClient);
    let answerFirst: (member: typeof A) => void = () => {};
    const me = vi
      .spyOn(datasource.session!, "me")
      .mockReturnValueOnce(new Promise((done) => (answerFirst = done)))
      .mockResolvedValueOnce(B);
    const swap = vi.fn();
    const reload = vi.fn();
    const { confirm, leave } = memberCheckOnReturn(queryClient, { swap, toLogin: () => {}, reload });
    const first = confirm("visible");
    leave();
    // 修之前：第二次併進第一次還沒回來的請求，這一行一直等不到（測試逾時）。
    await confirm("visible");
    expect(swap).toHaveBeenCalledTimes(1);
    // 舊的請求這時才回來（甲）。
    answerFirst(A);
    await first;
    expect(queryClient.getQueryData(meQuery.queryKey)).toEqual(B);
    expect(swap).toHaveBeenCalledTimes(1);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(me).toHaveBeenCalledTimes(2);
    stop();
  });

  it("N2: visible while the window was not selected, then focus after that check is done: a new return, checked again", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    const stop = holdRow(queryClient);
    const me = vi.spyOn(datasource.session!, "me").mockResolvedValue(A);
    const { confirm } = memberCheckOnReturn(queryClient, { swap: () => {}, toLogin: () => {}, reload: () => {} });
    // 蓋住它的視窗移開了（看得見、但視窗沒被選取），過一陣子才點回來（focus）。
    await confirm("visible", false);
    // 修之前：當成同一次切回，不再確認（1 次）。
    await confirm("focus");
    expect(me).toHaveBeenCalledTimes(2);
    stop();
  });

  it("N2: visible while the window was not selected, focus comes while that check is still out: the same return, asked once", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(meQuery.queryKey, A);
    const stop = holdRow(queryClient);
    const me = vi.spyOn(datasource.session!, "me").mockResolvedValue(A);
    const { confirm } = memberCheckOnReturn(queryClient, { swap: () => {}, toLogin: () => {}, reload: () => {} });
    // 切分頁時 focus 可能比選取晚一點到：變回看得見那一刻還沒被選取，focus 緊接著來。
    await Promise.all([confirm("visible", false), confirm("focus")]);
    expect(me).toHaveBeenCalledTimes(1);
    stop();
  });
});

describe("the previous check is paused while offline (epic-3n C-5)", () => {
  // 讓出一次給瀏覽器的事件迴圈：真的 focus／visibilitychange、點擊都是在這種空檔進來的（不是等固定的時間）。
  const nextEvent = () => new Promise<void>((done) => setTimeout(done, 0));
  // 每條測試結束一定會做的收尾（斷言失敗也一樣）：停掉暫留列的訂閱、解除 QueryClient 的掛載、連線狀態還原成開始時的樣子。
  let cleanups: (() => void)[] = [];
  let wasOnline = true;
  beforeEach(() => {
    wasOnline = onlineManager.isOnline();
  });
  afterEach(() => {
    for (const cleanup of cleanups.reverse()) cleanup();
    cleanups = [];
    onlineManager.setOnline(wasOnline);
    vi.restoreAllMocks();
  });
  function mountedClient(): QueryClient {
    const queryClient = new QueryClient();
    queryClient.mount();
    queryClient.setQueryData(meQuery.queryKey, A);
    const stop = holdRow(queryClient);
    cleanups.push(() => {
      stop();
      void queryClient.cancelQueries();
      queryClient.unmount();
    });
    return queryClient;
  }

  it("C-5: another member is found after coming back online: an add clicked during the second return is not sent", async () => {
    const queryClient = mountedClient();
    vi.spyOn(datasource.session!, "me").mockResolvedValue(B);
    const swap = vi.fn();
    const { confirm, leave } = memberCheckOnReturn(queryClient, { swap, toLogin: () => {}, reload: () => {} });
    // 離線：第一次確認的「我是誰」被暫停（不是「還在路上」）。
    onlineManager.setOnline(false);
    const first = confirm("visible");
    await nextEvent();
    expect(queryClient.getQueryState(meQuery.queryKey)?.fetchStatus).toBe("paused");
    // 切走再切回：第二次確認等著第一次。
    leave();
    const second = confirm("visible");
    await nextEvent();
    const source = addSource();
    const sent = sendLibraryChange(queryClient, source, PAPER, "add", () => {}, { expectedMemberId: A.member_id });
    await nextEvent();
    onlineManager.setOnline(true);
    await Promise.all([first, second]);
    // 修之前：第二次再問一次，快取已經是乙，判成「同一個人」，這筆加入帶甲送出（add 被呼叫 1 次）。
    expect(await sent).toBe("failed");
    expect(source.add).not.toHaveBeenCalled();
    expect(swap).toHaveBeenCalledTimes(1);
    expect(memberGeneration(queryClient)).toBe(1);
  });

  it("C-5: the sign-in expired while offline: back to the sign-in page once, and the waiting add is not sent", async () => {
    const queryClient = mountedClient();
    const me = vi.spyOn(datasource.session!, "me").mockResolvedValue(null);
    const toLogin = vi.fn();
    const { confirm, leave } = memberCheckOnReturn(queryClient, { swap: () => {}, toLogin, reload: () => {} });
    onlineManager.setOnline(false);
    const first = confirm("visible");
    await nextEvent();
    leave();
    const second = confirm("visible");
    await nextEvent();
    const source = addSource();
    const sent = sendLibraryChange(queryClient, source, PAPER, "add", () => {}, { expectedMemberId: A.member_id });
    await nextEvent();
    onlineManager.setOnline(true);
    await Promise.all([first, second]);
    expect(await sent).toBe("failed");
    expect(source.add).not.toHaveBeenCalled();
    // 修之前：第二次再問一次（me 2 次），又回登入頁一次（toLogin 2 次）。
    expect(me).toHaveBeenCalledTimes(1);
    expect(toLogin).toHaveBeenCalledTimes(1);
  });

  it("the sign-in expired and clearing the cache failed once: the second return still goes to the sign-in page, the add is not sent", async () => {
    const queryClient = mountedClient();
    const me = vi.spyOn(datasource.session!, "me").mockResolvedValue(null);
    // 第一次清快取失敗（同 G5），之後正常。
    vi.spyOn(queryClient, "cancelQueries").mockRejectedValueOnce(new Error("cancel failed"));
    const toLogin = vi.fn();
    const { confirm, leave } = memberCheckOnReturn(queryClient, { swap: () => {}, toLogin, reload: () => {} });
    onlineManager.setOnline(false);
    const first = confirm("visible");
    await nextEvent();
    leave();
    const second = confirm("visible");
    await nextEvent();
    const source = addSource();
    const sent = sendLibraryChange(queryClient, source, PAPER, "add", () => {}, { expectedMemberId: A.member_id });
    await nextEvent();
    onlineManager.setOnline(true);
    await Promise.all([first, second]);
    expect(await sent).toBe("failed");
    expect(source.add).not.toHaveBeenCalled();
    // 沿用「換了人」但上一次沒走到回登入頁：這一次再問、回登入頁一次（只沿用不重做時是 0 次）。
    expect(me).toHaveBeenCalledTimes(2);
    expect(toLogin).toHaveBeenCalledTimes(1);
  });

  it("a third return while the first check's request is out again (the second is only waiting): the first check's add is not sent", async () => {
    const queryClient = mountedClient();
    let answerFirst: (member: typeof B) => void = () => {};
    vi.spyOn(datasource.session!, "me")
      .mockReturnValueOnce(new Promise((done) => (answerFirst = done)))
      .mockResolvedValue(B);
    const swap = vi.fn();
    const { confirm, leave } = memberCheckOnReturn(queryClient, { swap, toLogin: () => {}, reload: () => {} });
    onlineManager.setOnline(false);
    const first = confirm("visible");
    await nextEvent();
    // 第一次確認還沒有結果時按的加入：等的是第一次。
    const source = addSource();
    const sent = sendLibraryChange(queryClient, source, PAPER, "add", () => {}, { expectedMemberId: A.member_id });
    await nextEvent();
    leave();
    const second = confirm("visible");
    await nextEvent();
    // 恢復連線：第一次的請求重新送出、還在路上；這時又切走再切回。
    onlineManager.setOnline(true);
    await nextEvent();
    expect(queryClient.getQueryState(meQuery.queryKey)?.fetchStatus).toBe("fetching");
    leave();
    const third = confirm("visible");
    await nextEvent();
    answerFirst(B);
    await Promise.all([first, second, third]);
    // 修之前：第三次以為路上的請求是第二次的，取消它；第一次因此問不到（unknown），等著它的加入帶甲送出。
    expect(source.add).not.toHaveBeenCalled();
    expect(await sent).toBe("failed");
    expect(swap).toHaveBeenCalledTimes(1);
  });
});
