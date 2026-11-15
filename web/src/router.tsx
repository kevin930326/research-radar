import { QueryClient } from "@tanstack/react-query";
import { createRouter, type RouterHistory } from "@tanstack/react-router";

import { routeTree } from "#route-tree";

import { LoadFailed } from "./components/states/LoadFailed";
import { NotFound } from "./components/states/NotFound";
import { PageLoading } from "./components/states/PageLoading";

/** 載入超過 0.3 秒才出現骨架畫面（EXPERIENCE.md「骨架畫面」）。 */
export const PENDING_MS = 300;

export function createAppRouter(options: { history?: RouterHistory; queryClient?: QueryClient } = {}) {
  return createRouter({
    routeTree,
    history: options.history,
    context: { queryClient: options.queryClient ?? new QueryClient() },
    defaultPendingComponent: PageLoading,
    defaultPendingMs: PENDING_MS,
    defaultErrorComponent: LoadFailed,
    defaultNotFoundComponent: NotFound,
    // 找不到的網址一律由最外層顯示（routes/root.tsx），不會落進要登入的外框裡。
    notFoundMode: "root",
    scrollRestoration: true,
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
