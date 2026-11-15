import { Outlet, createRootRoute } from "@tanstack/react-router";

import { NotFound } from "../components/states/NotFound";
import { Offline } from "../components/states/Offline";

// 兩種建置共用的最外層：沒有網路時的訊息條，加上這一頁的內容。
// 導覽列、頁尾、帳號選單在 3.5、3.15 才接上（AR-67）。
function RootLayout() {
  return (
    <main className="min-h-screen px-gutter-mobile pt-16 pb-24 tablet:px-gutter-tablet wide:px-gutter-desktop">
      <Offline />
      <Outlet />
    </main>
  );
}

export const rootRoute = createRootRoute({
  component: RootLayout,
  notFoundComponent: NotFound,
});
