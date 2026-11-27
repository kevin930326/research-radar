// 寫入回 409（換了人）時「舊 409 不重跑」（審查 fix3 G2a）在頁面上的樣子（#114 收尾，epic-3l PR C）：
// - R3-4：我的文獻庫（找論文同一個 useLibraryChange）真的拿得到路由認定的成員：畫面上的清單是甲讀的、快取的「我是誰」與
//   路由都已經是乙時，按下去的 409 不再跑一次換人流程（不再問「我是誰」）。拿不到路由的成員時這條測試會失敗。
// - R3-3：論文頁不套用「舊 409 不重跑」（D3 選項 A）：同樣的情況，論文頁按下去（加入、修正分類、回報錯誤）照樣跑換人流程。
// - X5（epic-3l C 審查）：論文頁送出之後別的流程換過人（世代號變了），回來的 409 member_changed 也照樣跑換人流程。
// - X6（epic-3l C 審查）：論文頁的兩種 409 從 API 的回應一路到頁面拿到的送出函式：回報錯誤的 summary_changed 只重讀這一篇、
//   不跑換人流程；member_changed 跑換人流程；請求帶的預期成員是畫面資料的 readAs。表單留著選的與寫的（送出回 null，
//   SummaryReport.tsx 照「沒有成功」留著）由 e2e summary-report.spec.ts 的 summary_changed 那一條守。
// 用真的成員版路由表（createAppRouter）載入頁面，再用 react-dom/server 靜態渲染（ADR-0036 第 7 點，沒有 DOM）：
// 渲染時記下頁面拿到的寫入函式，渲染完再呼叫它。
import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import listFixtures from "../../e2e/fixtures/paper-list.json";
import fixtures from "../../e2e/fixtures/papers.json";
import { datasource } from "../datasource";
import type { LibraryItem, PaperList, PaperPage } from "../datasource/types";
import type { DisplayNames, PaperPageResponse } from "../generated/model";
import { createAppRouter } from "../router";
import { libraryListKey } from "../routes/paper-queries";
import { clearMemberQueries, memberGeneration } from "../routes/session";

type Change = (paperId: string, action: "add" | "remove" | "restore") => Promise<unknown>;
type Write = (request: { write: () => Promise<{ kind: "memberChanged" }>; apply: () => unknown }) => Promise<unknown>;

// 每一次 useLibraryChange、usePaperWrite 回的寫入函式（照渲染順序）。
const changes = vi.hoisted(() => [] as Change[]);
const writes = vi.hoisted(() => [] as Write[]);
// 論文頁的回報錯誤、修正分類的送出函式（useSendSummaryReport、useSendClassificationFix；X6 用真的 API 資料來源送）。
type Send = (paperId: string, body: Record<string, string | string[]>) => Promise<unknown>;
const reports = vi.hoisted(() => [] as Send[]);
const fixes = vi.hoisted(() => [] as Send[]);
vi.mock("../routes/summary-report", async (importOriginal) => {
  const original = await importOriginal<typeof import("../routes/summary-report")>();
  return {
    ...original,
    useSendSummaryReport: (...args: Parameters<typeof original.useSendSummaryReport>) => {
      const send = original.useSendSummaryReport(...args);
      reports.push(send as unknown as Send);
      return send;
    },
  };
});
vi.mock("../routes/classification-fix", async (importOriginal) => {
  const original = await importOriginal<typeof import("../routes/classification-fix")>();
  return {
    ...original,
    useSendClassificationFix: (...args: Parameters<typeof original.useSendClassificationFix>) => {
      const send = original.useSendClassificationFix(...args);
      fixes.push(send as unknown as Send);
      return send;
    },
  };
});
vi.mock("../routes/library-change", async (importOriginal) => {
  const original = await importOriginal<typeof import("../routes/library-change")>();
  return {
    ...original,
    useLibraryChange: (...args: Parameters<typeof original.useLibraryChange>) => {
      const change = original.useLibraryChange(...args);
      changes.push(change);
      return change;
    },
  };
});
vi.mock("../routes/paper-write", async (importOriginal) => {
  const original = await importOriginal<typeof import("../routes/paper-write")>();
  return {
    ...original,
    usePaperWrite: (...args: Parameters<typeof original.usePaperWrite>) => {
      const write = original.usePaperWrite(...args);
      writes.push(write as unknown as Write);
      return write;
    },
  };
});

const A = { member_id: "00000000-0000-4000-8000-00000000000a", display_name: "Test Member A" };
const B = { member_id: "00000000-0000-4000-8000-00000000000b", display_name: "Test Member B" };
const NAMES = fixtures.displayNames as DisplayNames;
const CONFIG = listFixtures.clientConfig;
const ROW = listFixtures.rows[0]!.row as unknown as PaperList["items"][number];
const PAPER = Object.values(fixtures.papers as unknown as Record<string, { page: PaperPageResponse }>)[0]!.page as unknown as PaperPage;

/** 設定值、顯示名稱已經讀到；「我是誰」問到的是乙。 */
function cacheAsB(): QueryClient {
  const queryClient = new QueryClient();
  queryClient.setQueryData(["client-config"], { kind: "found", config: CONFIG });
  queryClient.setQueryData(["display-names"], { kind: "found", names: NAMES });
  return queryClient;
}

/** 用乙的身分載入 `path`（路由的 context 是乙），靜態渲染一次。回傳渲染出來的 HTML。 */
async function renderAsB(queryClient: QueryClient, path: string): Promise<string> {
  vi.spyOn(datasource.session!, "me").mockResolvedValue(B);
  const router = createAppRouter({ history: createMemoryHistory({ initialEntries: [path] }), queryClient });
  await router.load();
  return renderToStaticMarkup(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("an old 409 after the member change was followed, on the pages (#114 follow-up)", () => {
  afterEach(() => {
    changes.length = 0;
    writes.length = 0;
    reports.length = 0;
    fixes.length = 0;
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("R3-4 my library: the list was read as A, 'who am I' and the route say B: the 409 does not start another round", async () => {
    const queryClient = cacheAsB();
    const item = { paper: { ...ROW, in_my_library: true }, added_via: "manual", added_at: "2026-11-01T00:00:00Z" } as unknown as LibraryItem;
    queryClient.setQueryData(libraryListKey(CONFIG.library_page_size, B.member_id), {
      kind: "found",
      list: { items: [item], pending: [], next_cursor: null, total: 1 },
      readAs: A.member_id,
    });
    const html = await renderAsB(queryClient, "/library");
    expect(html).toContain(`data-library-row="${ROW.id}"`);
    const me = vi.mocked(datasource.session!.me);
    const asked = me.mock.calls.length;
    const remove = vi.spyOn(datasource.library!, "remove").mockResolvedValue({ kind: "memberChanged" });
    expect(changes.length).toBeGreaterThan(0);
    expect(await changes.at(-1)!(ROW.id, "remove")).toBe("failed");
    expect(remove).toHaveBeenCalledWith(ROW.id, A.member_id);
    // 換人流程第一步是問「我是誰」：沒有多問就是沒有再跑一輪。
    expect(me.mock.calls.length).toBe(asked);
  });

  it("R3-3 paper page: the page was read as A, 'who am I' and the route say B: adding still starts the member change round", async () => {
    const queryClient = cacheAsB();
    vi.spyOn(datasource.papers!, "paper").mockResolvedValue({ kind: "found", page: PAPER, readAs: A.member_id });
    const html = await renderAsB(queryClient, `/papers/${PAPER.paper.id}`);
    expect(html).toContain(PAPER.paper.title);
    const me = vi.mocked(datasource.session!.me);
    const asked = me.mock.calls.length;
    const add = vi.spyOn(datasource.library!, "add").mockResolvedValue({ kind: "memberChanged" });
    expect(changes.length).toBeGreaterThan(0);
    expect(await changes.at(-1)!(PAPER.paper.id, "add")).toBe("failed");
    expect(add).toHaveBeenCalledWith(PAPER.paper.id, A.member_id);
    // 換人流程問「我是誰」，接著重跑路由的登入檢查又問一次：至少多問一次。
    await vi.waitFor(() => expect(me.mock.calls.length).toBeGreaterThan(asked));
  });

  // 修正分類、回報錯誤（照渲染順序，論文頁每次渲染各拿一個 usePaperWrite）。各用一個新的快取，換人流程互不影響。
  for (const [index, name] of [
    [0, "the classification fix"],
    [1, "the summary report"],
  ] as const) {
    it(`R3-3 paper page: ${name} also starts the member change round`, async () => {
      const queryClient = cacheAsB();
      vi.spyOn(datasource.papers!, "paper").mockResolvedValue({ kind: "found", page: PAPER, readAs: A.member_id });
      await renderAsB(queryClient, `/papers/${PAPER.paper.id}`);
      const me = vi.mocked(datasource.session!.me);
      expect(writes.length).toBeGreaterThanOrEqual(2);
      const write = writes.slice(-2)[index]!;
      const asked = me.mock.calls.length;
      expect(await write({ write: async () => ({ kind: "memberChanged" }), apply: () => "applied" })).toBeNull();
      await vi.waitFor(() => expect(me.mock.calls.length).toBeGreaterThan(asked));
    });
  }
  it("X5 paper page: someone else's round changed the member while the add was out: its 409 still starts the member change round", async () => {
    const queryClient = cacheAsB();
    vi.spyOn(datasource.papers!, "paper").mockResolvedValue({ kind: "found", page: PAPER, readAs: A.member_id });
    await renderAsB(queryClient, `/papers/${PAPER.paper.id}`);
    const me = vi.mocked(datasource.session!.me);
    const asked = me.mock.calls.length;
    // 送出之後、回應回來之前，別的流程清過快取（世代號加 1）。
    vi.spyOn(datasource.library!, "add").mockImplementation(async () => {
      await clearMemberQueries(queryClient);
      return { kind: "memberChanged" };
    });
    expect(await changes.at(-1)!(PAPER.paper.id, "add")).toBe("failed");
    // 修之前：世代號不同就直接結束，不問「我是誰」。
    await vi.waitFor(() => expect(me.mock.calls.length).toBeGreaterThan(asked));
  });

  it("X5 paper page: the same for the summary report", async () => {
    const queryClient = cacheAsB();
    vi.spyOn(datasource.papers!, "paper").mockResolvedValue({ kind: "found", page: PAPER, readAs: A.member_id });
    await renderAsB(queryClient, `/papers/${PAPER.paper.id}`);
    const me = vi.mocked(datasource.session!.me);
    const asked = me.mock.calls.length;
    const write = writes.slice(-2)[1]!;
    const answer = async () => {
      await clearMemberQueries(queryClient);
      return { kind: "memberChanged" as const };
    };
    expect(await write({ write: answer, apply: () => "applied" })).toBeNull();
    await vi.waitFor(() => expect(me.mock.calls.length).toBeGreaterThan(asked));
  });

  /** 論文頁的寫入都回 `status`（problem+json 的 `code`）；其他請求（例如顯示名稱）不回。回傳 fetch 的替身。 */
  function answerWrites(status: number, code: string) {
    const fetchMock = vi.fn(async (url: string) => {
      if (!/\/(summary-reports|classification-fixes)$/.test(url)) throw new Error(`not used: ${url}`);
      return new Response(JSON.stringify({ type: "about:blank", title: "x", status, code }), {
        status,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  function expectedHeader(fetchMock: ReturnType<typeof answerWrites>): string | null {
    const [, init] = fetchMock.mock.calls.at(-1)! as unknown as [string, RequestInit];
    return new Headers(init.headers).get("X-Radar-Expected-Member");
  }

  const ZH_SUMMARY_ID = (PAPER.zh_summary as { value: { zh_summary_id: string } }).value.zh_summary_id;
  const REPORT = { field: "findings", note: "fake note", zh_summary_id: ZH_SUMMARY_ID };

  it("X6 summary report answered 409 summary_changed: nothing sent back, only this paper is read again, no member change round", async () => {
    const queryClient = cacheAsB();
    const paper = vi.spyOn(datasource.papers!, "paper").mockResolvedValue({ kind: "found", page: PAPER, readAs: B.member_id });
    await renderAsB(queryClient, `/papers/${PAPER.paper.id}`);
    const read = paper.mock.calls.length;
    const generation = memberGeneration(queryClient);
    const fetchMock = answerWrites(409, "summary_changed");
    expect(await reports.at(-1)!(PAPER.paper.id, REPORT)).toBeNull();
    expect(expectedHeader(fetchMock)).toBe(B.member_id);
    // 這一篇重讀（路由的 loader 再讀一次論文頁）。
    await vi.waitFor(() => expect(paper.mock.calls.length).toBeGreaterThan(read));
    expect(paper.mock.calls.at(-1)![0]).toBe(PAPER.paper.id);
    // 沒有跑換人流程：快取沒有被清（世代號不變）。
    expect(memberGeneration(queryClient)).toBe(generation);
  });

  it("X6 summary report answered 409 member_changed: sent as the member the page was read as, the member change round starts", async () => {
    const queryClient = cacheAsB();
    vi.spyOn(datasource.papers!, "paper").mockResolvedValue({ kind: "found", page: PAPER, readAs: A.member_id });
    await renderAsB(queryClient, `/papers/${PAPER.paper.id}`);
    const generation = memberGeneration(queryClient);
    const fetchMock = answerWrites(409, "member_changed");
    expect(await reports.at(-1)!(PAPER.paper.id, REPORT)).toBeNull();
    expect(expectedHeader(fetchMock)).toBe(A.member_id);
    // 換人流程清掉上一位的快取（世代號加 1）。
    await vi.waitFor(() => expect(memberGeneration(queryClient)).toBeGreaterThan(generation));
  });

  it("X6 classification fix answered 409 member_changed: sent as the member the page was read as, the member change round starts", async () => {
    const queryClient = cacheAsB();
    vi.spyOn(datasource.papers!, "paper").mockResolvedValue({ kind: "found", page: PAPER, readAs: A.member_id });
    await renderAsB(queryClient, `/papers/${PAPER.paper.id}`);
    const generation = memberGeneration(queryClient);
    const fetchMock = answerWrites(409, "member_changed");
    expect(await fixes.at(-1)!(PAPER.paper.id, { topic: ["fake_topic"], method: ["fake_method"], reason: "fake reason" })).toBeNull();
    expect(expectedHeader(fetchMock)).toBe(A.member_id);
    await vi.waitFor(() => expect(memberGeneration(queryClient)).toBeGreaterThan(generation));
  });
});
