import type { QueryClient } from "@tanstack/react-query";
import { Outlet, createRootRouteWithContext } from "@tanstack/react-router";
import type { ReactNode } from "react";

import { NotFound } from "../components/states/NotFound";
import { Offline } from "../components/states/Offline";

/** 路由共用的東西：資料快取（成員版的登入檢查用它記住「我是誰」）。 */
export interface RouterContext {
  queryClient: QueryClient;
}

/**
 * 沒有導覽列的畫面共用的外框（找不到這一頁、元件示範頁、示範站）：沒有網路時的訊息條，加上這一頁的內容。
 * 成員版登入後的頁面另有導覽列的外框（routes/member.tsx），登入頁自己排版（pages/LoginPage.tsx）。
 */
export function PlainMain({ children }: { children: ReactNode }) {
  return (
    <main className="min-h-screen px-gutter-mobile pt-16 pb-24 tablet:px-gutter-tablet wide:px-gutter-desktop">
      <Offline />
      {children}
    </main>
  );
}

function RootLayout() {
  return <Outlet />;
}

function RootNotFound() {
  return (
    <PlainMain>
      <NotFound />
    </PlainMain>
  );
}

// 兩種建置共用的最外層。找不到的網址一律由這裡顯示（router 設 notFoundMode: "root"），不經過登入檢查。
// 頁尾在之後的外框 Story 才接上（AR-67）。
export const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: RootLayout,
  notFoundComponent: RootNotFound,
});
