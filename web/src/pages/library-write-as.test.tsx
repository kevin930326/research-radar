// 找論文、我的文獻庫的加入／移出帶「畫面上那份清單是用誰的身分讀的」（清單資料的 readAs），不是這一頁認定的成員
// （路由的 context，頁面的 memberId）（ADR-0055；審查 fix2 L-a）。
// 平常兩者一樣（頁面的 key 含成員，換人就重新掛上），這裡故意做出「清單是甲讀的、頁面認定乙」：只有 readAs 那一層在擋時，
// 寫入仍帶甲；改回用頁面的成員（或路由的 context）就會帶乙，這組測試失敗。
// 用 react-dom/server 靜態渲染（ADR-0036 第 7 點，沒有 DOM）：渲染時記下列與頁面拿到的寫入函式，渲染完再呼叫它。
import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterContextProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import listFixtures from "../../e2e/fixtures/paper-list.json";
import fixtures from "../../e2e/fixtures/papers.json";
import { datasource } from "../datasource";
import type { LibraryItem, PaperList } from "../datasource/types";
import type { DisplayNames } from "../generated/model";
import { filtersKey, libraryListKey, paperListKey } from "../routes/paper-queries";
import { LibraryPage } from "./LibraryPage";
import { SearchPage } from "./SearchPage";
import { toApiParams } from "./search-filters";

type Change = (paperId: string, action: "add" | "remove" | "restore") => Promise<unknown>;

// 每一次 useLibraryChange 回的寫入函式（照渲染順序）。
const changes = vi.hoisted(() => [] as Change[]);
vi.mock("../routes/library-change", async (importOriginal) => {
  const original = await importOriginal<typeof import("../routes/library-change")>();
  return {
    ...original,
    useLibraryChange: (readAs: string | undefined) => {
      const change = original.useLibraryChange(readAs);
      changes.push(change);
      return change;
    },
  };
});

const A = "00000000-0000-4000-8000-00000000000a";
const B = "00000000-0000-4000-8000-00000000000b";
const NAMES = fixtures.displayNames as DisplayNames;
const CONFIG = listFixtures.clientConfig;
const ROW = listFixtures.rows[0]!.row as unknown as PaperList["items"][number];

const rootRoute = createRootRoute();
const router = createRouter({
  routeTree: rootRoute.addChildren([
    createRoute({ getParentRoute: () => rootRoute, path: "/search" }),
    createRoute({ getParentRoute: () => rootRoute, path: "/library" }),
    createRoute({ getParentRoute: () => rootRoute, path: "/papers/$paperId" }),
  ]),
  history: createMemoryHistory({ initialEntries: ["/search"] }),
});

/** 設定值、顯示名稱已經讀到的查詢快取。 */
function cacheWithSettings(): QueryClient {
  const queryClient = new QueryClient();
  queryClient.setQueryData(["client-config"], { kind: "found", config: CONFIG });
  queryClient.setQueryData(["display-names"], { kind: "found", names: NAMES });
  return queryClient;
}

function render(queryClient: QueryClient, page: React.ReactNode): string {
  return renderToStaticMarkup(
    <QueryClientProvider client={queryClient}>
      <RouterContextProvider router={router}>{page}</RouterContextProvider>
    </QueryClientProvider>,
  );
}

const DONE = { kind: "done", item: { paper: ROW, added_via: "manual", added_at: "2026-11-01T00:00:00Z" } } as const;

describe("rows write as whoever the shown list was read as, not the page's member (fix2 L-a)", () => {
  afterEach(() => {
    changes.length = 0;
    vi.restoreAllMocks();
  });

  it("search: the list was read as A, the page says B: add goes out as A", async () => {
    const queryClient = cacheWithSettings();
    const filters = toApiParams({ scope: "personal" });
    const list = { items: [ROW], pending: [], next_cursor: null, total: 1, snapshot: listFixtures.snapshot } as unknown as PaperList;
    queryClient.setQueryData(paperListKey(filtersKey(filters), CONFIG.paper_list_page_size, B), { kind: "found", list, readAs: A });
    const add = vi.spyOn(datasource.library!, "add").mockResolvedValue(DONE);
    const html = render(
      queryClient,
      <SearchPage params={{ scope: "personal" }} memberId={B} onUpdate={() => {}} onSignedOut={() => {}} onMemberChanged={() => {}} />,
    );
    expect(html).toContain(`data-paper-row="${ROW.id}"`);
    expect(changes.length).toBeGreaterThan(0);
    await changes.at(-1)!(ROW.id, "add");
    expect(add).toHaveBeenCalledWith(ROW.id, A);
  });

  it("my library: the list was read as A, the page says B: remove goes out as A", async () => {
    const queryClient = cacheWithSettings();
    const item = { paper: { ...ROW, in_my_library: true }, added_via: "manual", added_at: "2026-11-01T00:00:00Z" } as unknown as LibraryItem;
    const list = { items: [item], pending: [], next_cursor: null, total: 1 };
    queryClient.setQueryData(libraryListKey(CONFIG.library_page_size, B), { kind: "found", list, readAs: A });
    const remove = vi.spyOn(datasource.library!, "remove").mockResolvedValue(DONE);
    const html = render(
      queryClient,
      <LibraryPage shown={undefined} memberId={B} onMore={() => {}} onSignedOut={() => {}} onMemberChanged={() => {}} />,
    );
    expect(html).toContain(`data-library-row="${ROW.id}"`);
    expect(changes.length).toBeGreaterThan(0);
    await changes.at(-1)!(ROW.id, "remove");
    expect(remove).toHaveBeenCalledWith(ROW.id, A);
  });
});
