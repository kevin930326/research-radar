import { Outlet, createRoute, lazyRouteComponent, redirect } from "@tanstack/react-router";
import { useState } from "react";

import { SiteHeader } from "../components/SiteHeader";
import { LoadFailed } from "../components/states/LoadFailed";
import { Offline } from "../components/states/Offline";
import { PageLoading } from "../components/states/PageLoading";
import { LoginPage } from "../pages/LoginPage";
import { NoticePage } from "../pages/NoticePage";
import { PlainMain, rootRoute } from "./root";
import { FIRST_PAGE, clearMemberQueries, meQuery, validateLoginSearch } from "./session";

// 成員版的路由表（Story 3.5）：
// - /login：登入頁（開發與試用期間是假登入頁），不用登入。
// - 其他成員版的頁面都在「要登入」的外框（app）底下：每次進頁面先問「我是誰」。
//   沒登入（401）就到登入頁，網址帶 next（登入後回到原本那一頁）；本來在站內（快取裡有上一次的成員）
//   才是「登入過期」，多帶 expired，登入頁顯示「登入已經過期，請再登入一次」。直接打開網址時快取是空的，不顯示。
//   「我是誰」問不到（網路、伺服器錯誤）是全站共用的「載入失敗」。
// - 3.14 之前登入後第一頁是使用須知，所以 / 先導到 /notice。
// - 找不到的網址由最外層顯示（routes/root.tsx），不用登入。

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  validateSearch: validateLoginSearch,
  component: function LoginRoute() {
    return <LoginPage search={loginRoute.useSearch()} />;
  },
});

/** 登入後的頁面共用的外框：導覽列＋這一頁的內容。手機的滿版選單打開時，頁面本身藏起來（設計稿整頁換成選單）。 */
function AppLayout() {
  const { member } = appRoute.useRouteContext();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  return (
    <div className={`flex min-h-screen flex-col ${mobileMenuOpen ? "max-split:hidden" : ""}`}>
      <SiteHeader member={member} mobileMenuOpen={mobileMenuOpen} onMobileMenuChange={setMobileMenuOpen} />
      {/* 標題離導覽列：900px 以上 72px、899px 以下 40px（設計稿）；頁尾在之後的外框 Story 才接上。 */}
      <main className="flex-1 px-gutter-mobile pt-[40px] pb-24 tablet:px-gutter-tablet split:pt-[72px] wide:px-gutter-desktop">
        <Offline />
        <Outlet />
      </main>
    </div>
  );
}

const appRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "app",
  beforeLoad: async ({ context, location }) => {
    const { queryClient } = context;
    const wasSignedIn = queryClient.getQueryData(meQuery.queryKey) != null;
    const member = await queryClient.fetchQuery(meQuery);
    if (member === null) {
      // 登入過期（或還沒登入）：清掉所有查詢快取，再登入的可能是另一個人。
      await clearMemberQueries(queryClient);
      throw redirect({
        to: "/login",
        search: { next: location.href, ...(wasSignedIn ? { expired: true as const } : {}) },
        replace: true,
      });
    }
    return { member };
  },
  component: AppLayout,
  // 還不知道是誰（沒有導覽列）時的載入中與載入失敗：用沒有導覽列的外框。
  pendingComponent: function AppPending() {
    return (
      <PlainMain>
        <PageLoading />
      </PlainMain>
    );
  },
  errorComponent: function AppLoadFailed() {
    return (
      <PlainMain>
        <LoadFailed />
      </PlainMain>
    );
  },
});

const indexRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/",
  beforeLoad: () => {
    throw redirect({ to: FIRST_PAGE, replace: true });
  },
});

const noticeRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/notice",
  component: NoticePage,
});

// 元件示範頁只在開發環境（Story 3.2）：正式建置時 import.meta.env.DEV 是 false，這一段連同動態 import
// 整個被拿掉，建置結果沒有這個路由、元件與示範頁的文字（e2e/dev-only.spec.ts 檢查）。不用登入。
function devRoutes() {
  const devLayout = createRoute({
    getParentRoute: () => rootRoute,
    id: "dev",
    component: function DevLayout() {
      return (
        <PlainMain>
          <Outlet />
        </PlainMain>
      );
    },
  });
  return [
    devLayout.addChildren([
      createRoute({
        getParentRoute: () => devLayout,
        path: "/dev/components",
        component: lazyRouteComponent(() => import("../pages/DevComponentsPage"), "DevComponentsPage"),
      }),
    ]),
  ] as const;
}

const appTree = appRoute.addChildren([indexRoute, noticeRoute]);

export const routeTree = import.meta.env.DEV
  ? rootRoute.addChildren([loginRoute, appTree, ...devRoutes()])
  : rootRoute.addChildren([loginRoute, appTree]);
