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
 * 整頁外框（ADR-0054）：寬 100%、最寬 1280px（含左右留白，內容 1088px）、左右置中。
 * 只定義在這裡；成員版的導覽列＋內容（routes/member.tsx 的 AppLayout）與下面的 PlainMain 都用它。
 * 視窗 1280px 以下沒有作用。滿版的東西（背景、手機的滿版選單與篩選）是 fixed 或畫在 body 上，不受限。
 */
export const PAGE_FRAME_CLASS = "mx-auto w-full max-w-page-max-width";

/**
 * 沒有導覽列的畫面共用的外框（找不到這一頁、元件示範頁、示範站）：沒有網路時的訊息條，加上這一頁的內容。
 * 成員版登入後的頁面另有導覽列的外框（routes/member.tsx），登入頁自己排版（pages/LoginPage.tsx）。
 */
export function PlainMain({ children }: { children: ReactNode }) {
  return (
    <main
      className={`${PAGE_FRAME_CLASS} min-h-screen px-gutter-mobile pt-16 pb-24 tablet:px-gutter-tablet wide:px-gutter-desktop`}
    >
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
// 全站頁尾（Story 3.15）跟著有導覽列的外框（成員版 AppLayout、示範站 demoLayout）；這裡沒有導覽列（找不到的網址、
// 還沒確認身分時的載入中與載入失敗、登入頁），也不放頁尾。
export const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: RootLayout,
  notFoundComponent: RootNotFound,
});
