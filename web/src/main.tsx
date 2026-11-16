import "./i18n";
import "./styles.css";
// 英文與數字展示、小標籤用的字體：Fontsource 打包進建置結果，不連外部字體服務。
import "@fontsource/instrument-serif/400.css";
import "@fontsource/instrument-serif/400-italic.css";
import "@fontsource/dm-mono/400.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { createAppRouter } from "./router";

// 路由的登入檢查與畫面共用同一份資料快取。
const queryClient = new QueryClient();
const router = createAppRouter({ queryClient });

const rootElement = document.getElementById("root");
if (rootElement === null) {
  throw new Error("#root not found");
}

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
