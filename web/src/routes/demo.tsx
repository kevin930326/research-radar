import { createRoute } from "@tanstack/react-router";

import { DemoHeader } from "../components/DemoHeader";
import { Offline } from "../components/states/Offline";
import { DemoPapersPage } from "../pages/DemoPapersPage";
import { validateDemoPapersParams, type DemoPapersParams } from "../pages/demo-papers-filters";
import { PAGE_FRAME_CLASS, rootRoute } from "./root";

// 示範站的路由表：使用須知是成員版的頁面，不放。
// - /papers：論文清單（Story 3.14 示範站）。篩選與目前顯示幾篇都寫進網址（AD-21），路由驗證網址參數。
// - 其他網址（含 /）都是「找不到這一頁」：示範站首頁在 Epic 13、論文頁在 Story 3.9（都還沒做）。
// 外框和成員版登入後的頁面一樣（ADR-0054 的 PAGE_FRAME_CLASS）：導覽列＋這一頁的內容；頁尾歸 Story 3.15。

const papersRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/papers",
  validateSearch: (search: Record<string, unknown>) => validateDemoPapersParams(search),
  component: function DemoPapersRoute() {
    const params = papersRoute.useSearch();
    const navigate = papersRoute.useNavigate();
    return (
      <div className={`${PAGE_FRAME_CLASS} flex min-h-screen flex-col`}>
        <DemoHeader />
        {/* 標題離導覽列：900px 以上 72px、899px 以下 40px（設計稿）。 */}
        <main className="flex flex-1 flex-col px-gutter-mobile pt-[40px] pb-24 tablet:px-gutter-tablet split:pt-[72px] wide:px-gutter-desktop">
          <Offline />
          <DemoPapersPage
            params={params}
            // 篩選改了：換掉這一筆瀏覽紀錄（不是每勾一個就多一筆），捲動位置不動。
            onUpdate={(change) =>
              void navigate({ search: (current: DemoPapersParams) => change(current), replace: true, resetScroll: false })
            }
          />
        </main>
      </div>
    );
  },
});

export const routeTree = rootRoute.addChildren([papersRoute]);
