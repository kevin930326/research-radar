import { queryOptions, useQuery } from "@tanstack/react-query";
import { Link, Outlet, createRoute } from "@tanstack/react-router";
import type { ReactNode } from "react";

import { DemoHeader } from "../components/DemoHeader";
import { SiteFooter } from "../components/SiteFooter";
import { NotFound } from "../components/states/NotFound";
import { Offline } from "../components/states/Offline";
import { datasource } from "../datasource";
import type { DemoManifest, DemoPaperIndex } from "../datasource/types";
import { CoveragePage, NUMBER_LINK_CLASS } from "../pages/CoveragePage";
import { DEMO_PAPERS_QUERY_KEY, DemoPapersPage } from "../pages/DemoPapersPage";
import { DemoPaperPage } from "../pages/DemoPaperPage";
import { validateDemoPapersParams, type DemoPapersParams } from "../pages/demo-papers-filters";
import { isPaperId } from "../pages/paper-format";
import { PAGE_FRAME_CLASS, rootRoute } from "./root";

// 示範站的路由表：使用須知是成員版的頁面，不放。
// - /papers：論文清單（Story 3.14 示範站）。篩選與目前顯示幾篇都寫進網址（AD-21），路由驗證網址參數。
// - /papers/$paperId：論文頁（Story 3.9）。ID 不是 UUID、或這次匯出的索引裡沒有這一篇：導覽列下方顯示
//   全站共用的「找不到這一頁」。manifest、索引、分片任一個讀不到或形狀不對：整頁「載入失敗」（導覽列照常）。
//   manifest 與索引和論文清單用同一份快取（從清單點進來不再讀一次）。
// - /coverage：資料涵蓋範圍（Story 3.15）。
// - 其他網址（含 /）都是「找不到這一頁」：示範站首頁在 Epic 13（還沒做）。
// 外框和成員版登入後的頁面一樣（ADR-0054 的 PAGE_FRAME_CLASS）：導覽列＋這一頁的內容＋全站頁尾（Story 3.15）。
// 兩頁共用外框（demoLayout）：載入中、載入失敗也畫在導覽列下面（和成員版的論文頁一樣）。

const demoLayout = createRoute({
  getParentRoute: () => rootRoute,
  id: "demo",
  component: function DemoLayout() {
    return (
      <div className={`${PAGE_FRAME_CLASS} flex min-h-screen flex-col`}>
        <DemoHeader />
        {/* 標題離導覽列：900px 以上 72px、899px 以下 40px（設計稿）。下內距是內容到頁尾的距離：899px 以下 64px、900px 以上 96px。 */}
        <main className="flex flex-1 flex-col px-gutter-mobile pt-[40px] pb-[64px] tablet:px-gutter-tablet split:pt-[72px] split:pb-24 wide:px-gutter-desktop">
          <Offline />
          <Outlet />
        </main>
        {/* 全站頁尾（Story 3.15）：示範站首頁（Epic 13）以外的每一頁都有，和導覽列、內容同一個外框（ADR-0054）。 */}
        <SiteFooter site="demo" />
      </div>
    );
  },
});

const papersRoute = createRoute({
  getParentRoute: () => demoLayout,
  path: "/papers",
  validateSearch: (search: Record<string, unknown>) => validateDemoPapersParams(search),
  component: function DemoPapersRoute() {
    const params = papersRoute.useSearch();
    const navigate = papersRoute.useNavigate();
    return (
      <DemoPapersPage
        params={params}
        // 篩選改了：換掉這一筆瀏覽紀錄（不是每勾一個就多一筆），捲動位置不動。
        onUpdate={(change) =>
          void navigate({ search: (current: DemoPapersParams) => change(current), replace: true, resetScroll: false })
        }
      />
    );
  },
});

function demoSource() {
  const source = datasource.demoPapers;
  if (source === null) throw new Error("demoPapers is only available in the demo build");
  return source;
}

// 和論文清單（DemoPapersPage）同一組快取鍵與設定：匯出不會在頁面開著的時候變，讀過就不再讀。
const CACHED = { retry: false, staleTime: Infinity } as const;
const manifestQuery = queryOptions({
  queryKey: [...DEMO_PAPERS_QUERY_KEY, "manifest"],
  queryFn: () => demoSource().manifest(),
  ...CACHED,
});
const indexQuery = (manifest: DemoManifest) =>
  queryOptions({
    queryKey: [...DEMO_PAPERS_QUERY_KEY, "index", manifest.exportId],
    queryFn: () => demoSource().index(manifest),
    ...CACHED,
  });
const paperQuery = (manifest: DemoManifest, index: DemoPaperIndex, paperId: string) =>
  queryOptions({
    queryKey: [...DEMO_PAPERS_QUERY_KEY, "paper", manifest.exportId, paperId.toLowerCase()],
    queryFn: () => demoSource().paper(manifest, index, paperId),
    ...CACHED,
  });

const paperRoute = createRoute({
  getParentRoute: () => demoLayout,
  path: "/papers/$paperId",
  loader: async ({ params, context }) => {
    if (!isPaperId(params.paperId)) {
      return { kind: "notFound" as const };
    }
    const { queryClient } = context;
    const manifest = await queryClient.fetchQuery(manifestQuery);
    const index = await queryClient.fetchQuery(indexQuery(manifest));
    const paper = await queryClient.fetchQuery(paperQuery(manifest, index, params.paperId));
    if (paper === null) {
      return { kind: "notFound" as const };
    }
    return { kind: "found" as const, paper, names: index.names };
  },
  component: function DemoPaperRoute() {
    const data = paperRoute.useLoaderData();
    if (data.kind === "notFound") {
      return <NotFound />;
    }
    return <DemoPaperPage key={data.paper.page.paper.id} paper={data.paper} names={data.names} />;
  },
});

// 資料涵蓋範圍（Story 3.15）：manifest（和論文清單同一份快取）→ 這次匯出的 cube.json 與 taxonomy.json。
// 第三個讀數是 manifest 的「有完整內容」篇數。三個讀數的數字連到論文清單並帶入條件（數字是 0 時不是連結）。
const coverageQuery = (manifest: DemoManifest) =>
  queryOptions({
    queryKey: [...DEMO_PAPERS_QUERY_KEY, "coverage", manifest.exportId],
    queryFn: () => demoSource().coverage(manifest),
    ...CACHED,
  });

const coverageRoute = createRoute({
  getParentRoute: () => demoLayout,
  path: "/coverage",
  component: function DemoCoverageRoute() {
    const manifest = useQuery(manifestQuery);
    const coverage = useQuery({ ...coverageQuery(manifest.data ?? EMPTY_MANIFEST), enabled: manifest.data !== undefined });
    const failed = manifest.isError || coverage.isError;
    const data =
      manifest.data !== undefined && coverage.data !== undefined
        ? {
            rows: coverage.data.cube.rows,
            params: coverage.data.cube.params,
            names: coverage.data.names,
            snapshot: manifest.data.snapshot,
            third: manifest.data.summary.fullContentCount,
          }
        : null;
    const link = (search: DemoPapersParams) => (children: ReactNode) => (
      <Link to="/papers" search={search} className={NUMBER_LINK_CLASS}>
        {children}
      </Link>
    );
    return (
      <CoveragePage
        site="demo"
        state={failed ? "error" : data !== null ? "ready" : "loading"}
        snapshot={manifest.data?.snapshot ?? null}
        data={data}
        links={{ total: link({}), withAbstract: link({ abstract: true }), third: link({ full: true }) }}
        onRetry={() => void (manifest.isError ? manifest.refetch() : coverage.refetch())}
      />
    );
  },
});

// 還沒讀到 manifest 時查詢鍵用的佔位（enabled 是 false，不會真的讀）。
const EMPTY_MANIFEST: DemoManifest = {
  snapshot: { number: 1, as_of: "1970-01-01T00:00:00Z" },
  exportId: "",
  pageSize: 1,
  files: [],
  summary: { paperCount: 0, withAbstractCount: 0, fullContentCount: 0 },
};

export const routeTree = rootRoute.addChildren([demoLayout.addChildren([papersRoute, paperRoute, coverageRoute])]);
