import { createRouter, type RouterHistory } from "@tanstack/react-router";

import { routeTree } from "#route-tree";

import { LoadFailed } from "./components/states/LoadFailed";
import { NotFound } from "./components/states/NotFound";
import { PageLoading } from "./components/states/PageLoading";

/** 載入超過 0.3 秒才出現骨架畫面（EXPERIENCE.md「骨架畫面」）。 */
export const PENDING_MS = 300;

export function createAppRouter(options: { history?: RouterHistory } = {}) {
  return createRouter({
    routeTree,
    history: options.history,
    defaultPendingComponent: PageLoading,
    defaultPendingMs: PENDING_MS,
    defaultErrorComponent: LoadFailed,
    defaultNotFoundComponent: NotFound,
    scrollRestoration: true,
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
