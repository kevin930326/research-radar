import { queryOptions, useQuery, useQueryClient } from "@tanstack/react-query";
import { Outlet, createRoute, lazyRouteComponent, redirect, useLocation, useNavigate } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import { SiteHeader } from "../components/SiteHeader";
import type { LibraryControl } from "../components/paper/LibraryBlock";
import { LoadFailed } from "../components/states/LoadFailed";
import { Offline } from "../components/states/Offline";
import { NotFound } from "../components/states/NotFound";
import { PageLoading } from "../components/states/PageLoading";
import type { PaperPage as PaperPageData } from "../datasource/types";
import { LibraryPage } from "../pages/LibraryPage";
import { LoginPage } from "../pages/LoginPage";
import { NoticePage } from "../pages/NoticePage";
import { PaperPage } from "../pages/PaperPage";
import { SearchPage } from "../pages/SearchPage";
import { isPaperId } from "../pages/paper-format";
import { validateSearchParams, type SearchParams } from "../pages/search-filters";
import { useLibraryChange } from "./library-change";
import { displayNamesQuery, paperSource } from "./paper-queries";
import { PlainMain, rootRoute } from "./root";
import { FIRST_PAGE, clearMemberQueries, meQuery, validateLoginSearch } from "./session";

// 成員版的路由表（Story 3.5）：
// - /login：登入頁（開發與試用期間是假登入頁），不用登入。
// - 其他成員版的頁面都在「要登入」的外框（app）底下：每次進頁面先問「我是誰」。
//   沒登入（401）就到登入頁，網址帶 next（登入後回到原本那一頁）；本來在站內（快取裡有上一次的成員）
//   才是「登入過期」，多帶 expired，登入頁顯示「登入已經過期，請再登入一次」。直接打開網址時快取是空的，不顯示。
//   「我是誰」問不到（網路、伺服器錯誤）是全站共用的「載入失敗」。
// - 登入後第一頁是找論文（Story 3.14 起；之前是使用須知，AR-67），所以 / 導到 /search。
// - /search：找論文（Story 3.14 成員版）。篩選與目前顯示幾篇都寫進網址（AD-21），路由驗證網址參數；
//   資料在頁面裡讀（篩選列先出來，論文列在讀的時候是骨架），任何一個 API 回 401 時照登入過期處理。
// - /papers/$paperId：論文頁（Story 3.8）。ID 不是 UUID 時不送請求；看不到或不存在（404）時在導覽列下方顯示
//   全站共用的「找不到這一頁」；論文或顯示名稱任一個 API 回 401 時照登入過期處理（清快取、回登入頁）。
//   論文讀到就顯示整頁；顯示名稱沒讀到只有分類那一行顯示訊息條與「重試」（Codex 審查 3.8 中 3、中 6）。
// - /library：我的文獻庫（Story 3.17）。「目前顯示幾篇」寫進網址（AD-21）；資料在頁面裡讀，任何一個 API 回 401
//   時照登入過期處理。
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
      {/* 標題離導覽列：900px 以上 72px、899px 以下 40px（設計稿）；頁尾歸 Story 3.15（Kevin 2026-11-16 決定）。 */}
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

const searchRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/search",
  validateSearch: validateSearchParams,
  component: function SearchRoute() {
    const params = searchRoute.useSearch();
    const navigate = searchRoute.useNavigate();
    const queryClient = useQueryClient();
    const href = useLocation({ select: (location) => location.href });
    return (
      <SearchPage
        params={params}
        // 篩選改了：換掉這一筆瀏覽紀錄（不是每勾一個就多一筆），捲動位置不動。下一組條件用函式從最新的網址算
        // （上一次換頁還在等「我是誰」時，畫面拿到的條件是舊的；審查 S1-4）。
        onUpdate={(change) =>
          void navigate({ search: (current: SearchParams) => change(current), replace: true, resetScroll: false })
        }
        onSignedOut={() =>
          void (async () => {
            await clearMemberQueries(queryClient);
            await navigate({ to: "/login", search: { next: href, expired: true as const }, replace: true });
          })()
        }
      />
    );
  },
});

/** 我的文獻庫的網址參數：只有「目前顯示幾篇」（按過「再載入」才有；正整數）。 */
export function validateLibrarySearch(search: Record<string, unknown>): { shown?: number } {
  const shown = search.shown;
  return typeof shown === "number" && Number.isInteger(shown) && shown > 0 ? { shown } : {};
}

const libraryRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/library",
  validateSearch: validateLibrarySearch,
  component: function LibraryRoute() {
    const { shown } = libraryRoute.useSearch();
    const navigate = libraryRoute.useNavigate();
    const queryClient = useQueryClient();
    const href = useLocation({ select: (location) => location.href });
    return (
      <LibraryPage
        shown={shown}
        onMore={(next) => void navigate({ search: { shown: next }, replace: true, resetScroll: false })}
        onSignedOut={() =>
          void (async () => {
            await clearMemberQueries(queryClient);
            await navigate({ to: "/login", search: { next: href, expired: true as const }, replace: true });
          })()
        }
      />
    );
  },
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

const paperQuery = (paperId: string) =>
  queryOptions({ queryKey: ["papers", paperId], queryFn: () => paperSource().paper(paperId), retry: false });

/** 論文頁＋顯示名稱：顯示名稱沒讀到時只有分類那一行顯示訊息條；按「重試」回 401 時照登入過期處理。 */
function PaperWithNames({ page }: { page: PaperPageData }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const href = useLocation({ select: (location) => location.href });
  const names = useQuery(displayNamesQuery);
  const signedOut = names.data?.kind === "signedOut";
  useEffect(() => {
    if (!signedOut) return;
    void (async () => {
      await clearMemberQueries(queryClient);
      await navigate({ to: "/login", search: { next: href, expired: true as const }, replace: true });
    })();
  }, [signedOut, queryClient, navigate, href]);
  const library = usePaperLibrary(page);
  if (signedOut) {
    return null;
  }
  return (
    <PaperPage
      page={page}
      names={names.data?.kind === "found" ? names.data.names : null}
      onRetryNames={() => void names.refetch({ cancelRefetch: false })}
      library={library}
    />
  );
}

/**
 * 論文頁的「我的文獻庫」（Story 3.17）：一打開照 API 的 `in_my_library` 顯示；按了之後送出，成功才換樣子
 * （快取裡這一篇也跟著改，見 useLibraryChange）。送出中再按不會再送一次。
 */
function usePaperLibrary(page: PaperPageData): LibraryControl {
  const change = useLibraryChange();
  const [inLibrary, setInLibrary] = useState(page.in_my_library);
  const [busy, setBusy] = useState(false);
  const [focusToken, setFocusToken] = useState(0);
  // 連點兩下時第二下還看不到 busy（同一次畫面），用 ref 擋。
  const sending = useRef(false);
  const run = (action: "add" | "remove") => {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    void change(page.paper.id, action).then((outcome) => {
      sending.current = false;
      setBusy(false);
      if (outcome === "done") {
        setInLibrary(action === "add");
        setFocusToken((token) => token + 1);
      }
    });
  };
  return { inLibrary, busy, focusToken, onAdd: () => run("add"), onRemove: () => run("remove") };
}

const paperRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/papers/$paperId",
  // 離開這一頁就丟掉路由記住的資料：登出再換人登入時，不會先顯示上一位看過的論文頁再重新讀。
  gcTime: 0,
  loader: async ({ params, context, location }) => {
    const { queryClient } = context;
    if (!isPaperId(params.paperId)) {
      return { kind: "notFound" as const };
    }
    // 兩個請求一起送。任一個回 401（登入過期）都回登入頁；論文其他錯誤是整頁「載入失敗」。
    // 顯示名稱的其他錯誤不擋整頁：結果留在查詢快取裡，畫面（PaperWithNames）讀它決定分類那一行怎麼顯示。
    const [paper, names] = await Promise.allSettled([
      queryClient.fetchQuery(paperQuery(params.paperId)),
      queryClient.fetchQuery(displayNamesQuery),
    ]);
    const paperSignedOut = paper.status === "fulfilled" && paper.value.kind === "signedOut";
    const namesSignedOut = names.status === "fulfilled" && names.value.kind === "signedOut";
    if (paperSignedOut || namesSignedOut) {
      await clearMemberQueries(queryClient);
      throw redirect({ to: "/login", search: { next: location.href, expired: true as const }, replace: true });
    }
    if (paper.status === "rejected") {
      throw paper.reason;
    }
    const result = paper.value;
    // signedOut 上面已經處理，剩下的只有 notFound。
    if (result.kind !== "found") {
      return { kind: "notFound" as const };
    }
    return { kind: "found" as const, page: result.page };
  },
  component: function PaperRoute() {
    const data = paperRoute.useLoaderData();
    if (data.kind === "notFound") {
      return <NotFound />;
    }
    return <PaperWithNames page={data.page} />;
  },
});

const appTree = appRoute.addChildren([indexRoute, noticeRoute, searchRoute, libraryRoute, paperRoute]);

export const routeTree = import.meta.env.DEV
  ? rootRoute.addChildren([loginRoute, appTree, ...devRoutes()])
  : rootRoute.addChildren([loginRoute, appTree]);
