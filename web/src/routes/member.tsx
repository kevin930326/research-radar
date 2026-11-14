import { createRoute, redirect } from "@tanstack/react-router";

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

export const routeTree = rootRoute.addChildren([indexRoute, noticeRoute]);
