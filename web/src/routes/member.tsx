import { createRoute, lazyRouteComponent, redirect } from "@tanstack/react-router";

import { NoticePage } from "../pages/NoticePage";
import { rootRoute } from "./root";

// 成員版的路由表。3.14 之前登入後第一頁是使用須知，所以 / 先導到 /notice。
const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: () => {
    throw redirect({ to: "/notice", replace: true });
  },
});

const noticeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/notice",
  component: NoticePage,
});

// 元件示範頁只在開發環境（Story 3.2）：正式建置時 import.meta.env.DEV 是 false，這一段連同動態 import
// 整個被拿掉，建置結果沒有這個路由、元件與示範頁的文字（e2e/dev-only.spec.ts 檢查）。
function devRoutes() {
  return [
    createRoute({
      getParentRoute: () => rootRoute,
      path: "/dev/components",
      component: lazyRouteComponent(() => import("../pages/DevComponentsPage"), "DevComponentsPage"),
    }),
  ] as const;
}

export const routeTree = import.meta.env.DEV
  ? rootRoute.addChildren([indexRoute, noticeRoute, ...devRoutes()])
  : rootRoute.addChildren([indexRoute, noticeRoute]);
