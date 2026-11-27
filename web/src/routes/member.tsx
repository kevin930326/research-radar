import { queryOptions, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { Link, Outlet, createRoute, lazyRouteComponent, redirect, useLocation, useNavigate, useRouter } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";

import { SiteFooter } from "../components/SiteFooter";
import { SiteHeader } from "../components/SiteHeader";
import type { ClassificationFixControl } from "../components/paper/ClassificationFix";
import type { SummaryReportControl } from "../components/paper/SummaryReport";
import type { LibraryControl } from "../components/paper/LibraryBlock";
import { LoadFailed } from "../components/states/LoadFailed";
import { Offline } from "../components/states/Offline";
import { NotFound } from "../components/states/NotFound";
import { PageLoading } from "../components/states/PageLoading";
import type { PaperPage as PaperPageData } from "../datasource/types";
import { CoveragePage, NUMBER_LINK_CLASS } from "../pages/CoveragePage";
import { LibraryPage } from "../pages/LibraryPage";
import { LoginPage } from "../pages/LoginPage";
import { NoticePage } from "../pages/NoticePage";
import { PaperPage } from "../pages/PaperPage";
import { SearchPage } from "../pages/SearchPage";
import { isPaperId } from "../pages/paper-format";
import { validateSearchParams, type SearchParams } from "../pages/search-filters";
import type { ReadAs } from "../datasource/types";
import { classificationFixesQuery, useSendClassificationFix } from "./classification-fix";
import { useSendSummaryReport } from "./summary-report";
import { libraryShown, useFollowMemberChange, useLibraryChange } from "./library-change";
import { displayNamesQuery, hasHeldRows, paperKey, paperSource } from "./paper-queries";
import { PAGE_FRAME_CLASS, PlainMain, rootRoute } from "./root";
import {
  FIRST_PAGE,
  clearMemberQueries,
  loadMember,
  meQuery,
  memberGeneration,
  trackMemberCheck,
  useCachedMemberId,
  useMemberSwaps,
  validateLoginSearch,
  type MemberCheckVerdict,
} from "./session";

// 成員版的路由表（Story 3.5）：
// - /login：登入頁（開發與試用期間是假登入頁），不用登入。
// - 其他成員版的頁面都在「要登入」的外框（app）底下：每次進頁面先問「我是誰」。
//   沒登入（401）就到登入頁，網址帶 next（登入後回到原本那一頁）；本來在站內（快取裡有上一次的成員）
//   才是「登入過期」，多帶 expired，登入頁顯示「登入過期，請重新登入」。直接打開網址時快取是空的，不顯示。
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
    <div className={`${PAGE_FRAME_CLASS} flex min-h-screen flex-col ${mobileMenuOpen ? "max-split:hidden" : ""}`}>
      <SiteHeader member={member} mobileMenuOpen={mobileMenuOpen} onMobileMenuChange={setMobileMenuOpen} />
      {/* 標題離導覽列：900px 以上 72px、899px 以下 40px（設計稿）。下內距是內容到頁尾的距離：899px 以下 64px、900px 以上 96px。 */}
      {/* main 也是直排的 flex：頁面可以撐滿剩下的高度（我的文獻庫的底部選取列在清單很短時也貼在頁尾上面；審查 R9）。 */}
      <main className="flex flex-1 flex-col px-gutter-mobile pt-[40px] pb-[64px] tablet:px-gutter-tablet split:pt-[72px] split:pb-24 wide:px-gutter-desktop">
        <Offline />
        <Outlet />
      </main>
      {/* 全站頁尾（Story 3.15）：和導覽列、內容同一個外框（ADR-0054）。 */}
      <SiteFooter site="member" />
    </div>
  );
}

const appRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "app",
  beforeLoad: async ({ context, location }) => {
    const { queryClient } = context;
    // 換了人（另一個分頁換人登入）時 loadMember 先清掉上一位的查詢快取（審查 F1）。
    const { member, wasSignedIn } = await loadMember(queryClient);
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

// 資料涵蓋範圍（Story 3.15）：計數方塊、顯示名稱（期刊的全名）與「本週新增」的篇數（找論文清單的總篇數）。
// 三個都讀到才顯示；任一個讀不到是「沒有載入成功」（按「重新整理」重讀沒讀到的）；任一個回 401 照登入過期處理。
// 計數方塊每週才更新一次：5 分鐘內回來直接用快取（和找論文清單一樣）。
const COVERAGE_STALE_MS = 5 * 60 * 1000;
const cubeQuery = queryOptions({
  queryKey: ["coverage", "cube"],
  queryFn: () => paperSource().cube(),
  retry: false,
  staleTime: COVERAGE_STALE_MS,
});
const thisWeekQuery = queryOptions({
  queryKey: ["coverage", "this-week"],
  queryFn: () => paperSource().list({ this_week: true }, 1),
  retry: false,
  staleTime: COVERAGE_STALE_MS,
});

const coverageRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/coverage",
  component: function CoverageRoute() {
    const queryClient = useQueryClient();
    const navigate = useNavigate();
    const href = useLocation({ select: (location) => location.href });
    const cube = useQuery(cubeQuery);
    const names = useQuery(displayNamesQuery);
    const thisWeek = useQuery(thisWeekQuery);
    const signedOut = cube.data?.kind === "signedOut" || names.data?.kind === "signedOut" || thisWeek.data?.kind === "signedOut";
    useEffect(() => {
      if (!signedOut) return;
      void (async () => {
        await clearMemberQueries(queryClient);
        await navigate({ to: "/login", search: { next: href, expired: true as const }, replace: true });
      })();
    }, [signedOut, queryClient, navigate, href]);
    if (signedOut) return null;
    const cubeData = cube.data?.kind === "found" ? cube.data.cube : null;
    const namesData = names.data?.kind === "found" ? names.data.names : null;
    // 「本週新增」用找論文清單的總篇數；清單的列不用（列裡有成員專屬的欄位，這裡只要篇數）。
    const listData = thisWeek.data?.kind === "found" ? thisWeek.data.list : null;
    const failed = cube.isError || names.isError || thisWeek.isError || thisWeek.data?.kind === "memberChanged";
    // 快照還沒發佈（snapshot 是 null）不會發生（State Patterns「9.」：空的不會發生）；真的遇到時當成沒有載入成功。
    const noSnapshot = cubeData !== null && cubeData.snapshot === null;
    const snapshot = cubeData?.snapshot ?? listData?.snapshot ?? null;
    const ready = cubeData !== null && cubeData.snapshot !== null && namesData !== null && listData !== null;
    return (
      <CoveragePage
        site="member"
        state={failed || noSnapshot ? "error" : ready ? "ready" : "loading"}
        snapshot={snapshot}
        data={
          ready && cubeData.snapshot !== null
            ? { rows: cubeData.rows, params: cubeData.params, names: namesData, snapshot: cubeData.snapshot, third: listData.total }
            : null
        }
        links={{
          third: (children) => (
            <Link to="/search" search={{ this_week: true }} className={NUMBER_LINK_CLASS}>
              {children}
            </Link>
          ),
        }}
        onRetry={() => {
          for (const query of [cube, names, thisWeek]) {
            if (query.isError || query.data?.kind === "memberChanged") void query.refetch({ cancelRefetch: false });
          }
          if (noSnapshot) void cube.refetch({ cancelRefetch: false });
        }}
      />
    );
  },
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
    const memberChanged = useMemberCheckWhileHolding();
    // 寫入時後端說換了人（409，ADR-0055）的次數：和切回視窗確認到換人一樣，整頁重新掛上。
    const swaps = useMemberSwaps(queryClient);
    const { member } = searchRoute.useRouteContext();
    const follow = useFollowMemberChange();
    const paused = useMemberSwapPending(queryClient, member.member_id);
    return (
      <SearchPage
        // 換了人：整頁重新掛上（快取已經清掉，清單照新的人重讀；每一列按過的狀態也不留）。路由的成員換了（換網址時
        // 「我是誰」認到別人）也一樣：不留上一位的 placeholder 與列的狀態（審查 fix1 H1）。
        key={`${member.member_id}-${memberChanged}-${swaps}`}
        memberId={member.member_id}
        paused={paused}
        onMemberChanged={follow}
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

/**
 * 換人進行中：快取裡「我是誰」已經是另一位（loadMember、換人流程問到的），路由的 context 還是上一位（路由還沒重跑完）。
 * 這段期間找論文、我的文獻庫不讀成員專屬的資料（清單是骨架）：用 context 的成員讀一定是 409，還會再觸發一輪換人流程
 * （審查 fix2 N1）。路由換成新的人之後頁面因 key 重新掛上，一掛上就用新的人讀。快取裡沒有「我是誰」（登出、清快取的途中）不算。
 */
function useMemberSwapPending(queryClient: QueryClient, memberId: string): boolean {
  const cached = useCachedMemberId(queryClient);
  return cached !== undefined && cached !== memberId;
}

/**
 * 範圍「我的文獻庫」有本頁移出暫留的列時，清單切回視窗不重讀（paperListQuery），所以另一個分頁換人登入時這一頁看不出來，
 * 暫留列的「加入我的文獻庫」會用新登入的人的身分加進他的文獻庫（Gemini 參考審查第 1 條；第 2 輪 G1）。
 * 切回這個分頁時，有暫留列就先問一次「我是誰」（loadMember，和每次進頁面一樣）：
 * - 還是同一位：不動，暫留列留著。
 * - 換了人（loadMember 清了快取）：找論文立刻重新掛上（同步重畫，上一位的清單馬上離開畫面、換成骨架，不等導覽列重問），
 *   再重跑路由的登入檢查（導覽列換成新的人）。
 * - 沒有人：和清單回 401 一樣清快取、回登入頁並帶 expired（登入過期；確認審查 D2）。
 * - 問不到（網路、伺服器錯誤）：不動。
 * 確認進行中這一頁的加入／移出先等結果，換了人或沒有人就不送（trackMemberCheck、sendLibraryChange；確認審查 D1）。
 */
function useMemberCheckWhileHolding(): number {
  const queryClient = useQueryClient();
  const router = useRouter();
  // 這個分頁看到換人幾次：給找論文當 key，換人就重新掛上（快取清掉之後，畫面上的查詢不會自己重讀）。
  const [changes, setChanges] = useState(0);
  useEffect(() => {
    const { confirm, leave } = memberCheckOnReturn(queryClient, {
      swap: () => flushSync(() => setChanges((count) => count + 1)),
      toLogin: async () => {
        await router.navigate({ to: "/login", search: { next: router.state.location.href, expired: true as const }, replace: true });
      },
      reload: () => router.invalidate(),
    });
    // confirm 不會 reject（見 memberCheckOnReturn）。
    const onFocus = () => void confirm("focus");
    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        leave();
        return;
      }
      // 視窗沒有被選取時變得看得見（例如蓋住它的視窗移開了）：之後點回來的 focus 要能算成新的一次（epic-3l C 確認 N2）。
      void confirm("visible", document.hasFocus());
    };
    // 和 TanStack Query 判斷「切回視窗」一樣聽 window 的 visibilitychange（document 發的會冒泡上來）。
    window.addEventListener("focus", onFocus);
    window.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("visibilitychange", onVisibility);
    };
  }, [queryClient, router]);
  return changes;
}

/**
 * 切回視窗時的確認（useMemberCheckWhileHolding 在 focus、分頁變回看得見時呼叫 `confirm`，分頁藏起來時呼叫 `leave`；
 * 拆出來是為了不靠畫面也能測）。
 * `swap`：換了人時同步重新掛上找論文；`toLogin`：沒有人時回登入頁；`reload`：重跑路由的登入檢查。
 * `confirm` 回一個 promise，確認（含收尾）做完才結束，**不會 reject**（中途的例外收在這裡：事件入口沒有人能處理，
 * 留著會變成沒接住的錯誤；epic-3l C 審查 X4）；畫面不等它。
 * - 同一次切回只確認一次（#114 收尾 S4）：分頁切回來時 focus 與「變回看得見」兩個事件都會來，不管一起來、或第二個晚到
 *   （第一次已經做完），都不再確認。怎麼認「新的一次切回」（epic-3l C 審查 X3）：分頁藏起來（`leave`）之後；或同一種事件又來了
 *   （視窗要先失焦才會再 focus、分頁要先藏起來才會再變回看得見，所以同一種事件第二次來表示中間離開過）；或「變回看得見」
 *   那一刻視窗沒有被選取（`focused` 是 false，入口傳 document.hasFocus()），那一次的確認做完之後才來的 focus（epic-3l C 確認 N2：
 *   視窗沒被選取時變得看得見，之後才點回來；做完之前就來的 focus 當成同一次，因為切分頁時 focus 可能比選取晚一點到）。
 *   不聽視窗失焦事件（blur）：建置檢查（tailwind-guard）把 "blur" 這個字當成不產生 CSS 的類別。
 * - 新的一次就算上一次還沒做完也照常確認，而且**重新問一次**「我是誰」（epic-3l C 確認 N1）：上一次的請求還在路上時，
 *   TanStack 會把新的問題併進那個請求（那是在別的分頁換人之前送出的，答案可能是舊的），所以先取消它（取消後它的回應不寫進快取，
 *   也不會蓋掉新的答案）再問。上一次的確認改成跟著新的一次的結果（等著它的加入／移出也一樣），不自己收尾、不當成「問不到」。
 *   上一次已經問到答案（正在清快取或重新掛上）就先等它做完，再問一次（不打斷它清到一半）。
 * - 確認途中丟例外（清快取、重新掛上失敗）：一律當成「換了人」收尾，等著的加入／移出不送、按鈕回到按之前的樣子
 *   （#114 收尾 G5：本來確認永遠沒有結果，按鈕一直停在送出中）。當成「換了人」是因為走到那裡時已經知道不是同一位，
 *   或是沒有人；不能當成同一位送出。
 * - 問「我是誰」那一步（loadMember）丟例外：世代號（換人就加 1）變了就是「換了人」——loadMember 認出別人、開始清上一位的
 *   快取時世代號先加 1，之後清快取才失敗；或是問的期間別的流程已經清過。沒變才是「問不到」（epic-3l C 審查 X1）。
 * 「我是誰」一直沒有回來（不是例外）時確認也一直沒有結果，按鈕會停在送出中：要修只能加計時器，這一輪不做（決定紀錄 C）；
 * 離開再切回來的那一次會重新問，問到了，上一次等著的也跟著有結果（N1）。
 */
export function memberCheckOnReturn(
  queryClient: QueryClient,
  actions: { swap: () => void; toLogin: () => Promise<void> | void; reload: () => Promise<void> | void },
): { confirm: (event: "focus" | "visible", focused?: boolean) => Promise<void>; leave: () => void } {
  // 這一次切回已經來過的事件（新的一次切回時清掉）。
  let seen = new Set<"focus" | "visible">();
  // 這一次切回是「變回看得見、視窗沒有被選取」開始的：那一次的確認做完了沒（N2）。
  let unfocusedStart: { done: boolean } | undefined;
  // 最近一次的確認：還沒有結果時，新的一次接手（`next`：新的一次的結果）。
  type Check = { verdict: Promise<MemberCheckVerdict>; settled: boolean; asked: boolean; next?: Promise<MemberCheckVerdict> };
  let latest: Check | undefined;
  const check = async () => {
    let resolve: (verdict: MemberCheckVerdict) => void = () => {};
    const mine: Check = { verdict: new Promise<MemberCheckVerdict>((done) => (resolve = done)), settled: false, asked: false };
    trackMemberCheck(queryClient, mine.verdict);
    const previous = latest;
    latest = mine;
    const settle = (verdict: MemberCheckVerdict) => {
      if (mine.settled) return;
      mine.settled = true;
      resolve(verdict);
    };
    try {
      if (previous !== undefined && !previous.settled) {
        if (!previous.asked && queryClient.getQueryState(meQuery.queryKey)?.fetchStatus === "fetching") {
          // 上一次的「我是誰」還在路上：它跟著這一次的結果；取消那個請求（取消後回應不寫進快取），下面重新問（N1）。
          previous.next = mine.verdict;
          await queryClient.cancelQueries({ queryKey: meQuery.queryKey, exact: true });
        } else {
          // 上一次已經問到答案、正在清快取或重新掛上：等它做完再問（不打斷它清到一半）。
          await previous.verdict;
        }
      }
      const generation = memberGeneration(queryClient);
      let member;
      let failed = false;
      try {
        ({ member } = await loadMember(queryClient));
      } catch {
        failed = true;
      }
      mine.asked = true;
      // 問的期間又切回來、新的一次接手了：這個答案（或例外）可能是舊的，不照它做（N1）。
      if (mine.next !== undefined) {
        settle(await mine.next);
        return;
      }
      if (failed) {
        settle(memberGeneration(queryClient) === generation ? "unknown" : "changed");
        return;
      }
      if (member !== null && memberGeneration(queryClient) === generation) {
        settle("same");
        return;
      }
      if (member === null) {
        // 本來在站內（有暫留列）才會走到這裡，所以是登入過期（和 SearchRoute 的 onSignedOut 一樣）。
        await clearMemberQueries(queryClient);
        settle("changed");
        await actions.toLogin();
        return;
      }
      // 換了人：先同步重畫（重新掛上），再讓等著的加入／移出知道結果，最後才重問導覽列的人。
      actions.swap();
      settle("changed");
      await actions.reload();
    } finally {
      // 上面已經有結果時這一行沒有作用。
      settle("changed");
    }
  };
  return {
    confirm: async (event, focused = true) => {
      const afterUnfocusedStart = event === "focus" && unfocusedStart?.done === true;
      const sameReturn = seen.size > 0 && !seen.has(event) && !afterUnfocusedStart;
      if (!sameReturn) {
        seen = new Set();
        unfocusedStart = undefined;
      }
      seen.add(event);
      if (sameReturn) return;
      const job = { done: false };
      if (event === "visible" && !focused) unfocusedStart = job;
      try {
        if (queryClient.getQueryCache().findAll({ queryKey: ["paper-list"], type: "active", predicate: hasHeldRows }).length === 0) return;
        await check();
      } catch {
        // 等著的加入／移出已經在 finally 收到結果；這裡只是不讓錯誤留在事件入口（見上面）。
      } finally {
        job.done = true;
      }
    },
    leave: () => {
      seen = new Set();
      unfocusedStart = undefined;
    },
  };
}

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
    // 寫入時後端說換了人（409，ADR-0055）：整頁重新掛上，清單照現在的人重讀（移出留在原地的列也不留）。
    // 路由的成員換了（換網址時「我是誰」認到別人）也一樣：不留上一位的 placeholder、「已移出」與「復原」（審查 fix1 H1）。
    const swaps = useMemberSwaps(queryClient);
    const { member } = libraryRoute.useRouteContext();
    const follow = useFollowMemberChange();
    const paused = useMemberSwapPending(queryClient, member.member_id);
    return (
      <LibraryPage
        key={`${member.member_id}-${swaps}`}
        memberId={member.member_id}
        paused={paused}
        onMemberChanged={follow}
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

// 讀取帶這一頁認定的成員當預期成員（ADR-0055）；讀到的結果記下是用誰讀的（readAs）。鍵最後一段是這一位（審查 fix2 N1）。
const paperQuery = (paperId: string, memberId: string) =>
  queryOptions({ queryKey: paperKey(paperId, memberId), queryFn: () => paperSource().paper(paperId, memberId), retry: false });

/**
 * 論文頁＋顯示名稱：顯示名稱沒讀到時只有分類那一行顯示訊息條；按「重試」回 401 時照登入過期處理。
 * `readAs`：這份論文頁資料是用誰的身分讀的（loader 記下的），加入／移出帶它（ADR-0055）。
 */
function PaperWithNames({ page, readAs }: { page: PaperPageData; readAs: ReadAs }) {
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
  const library = usePaperLibrary(page, readAs);
  const fix = useClassificationFix(page, readAs);
  const report = useSummaryReport(page, readAs);
  if (signedOut) {
    return null;
  }
  return (
    <PaperPage
      page={{ ...fix.page, zh_summary: report.zhSummary }}
      classificationFix={fix.control}
      summaryReport={report.control}
      names={names.data?.kind === "found" ? names.data.names : null}
      onRetryNames={() => void names.refetch({ cancelRefetch: false })}
      library={library}
    />
  );
}

/**
 * 論文頁的「我的文獻庫」（Story 3.17）：照 API 的 `in_my_library` 顯示；按了之後送出，成功才換樣子
 * （快取裡這一篇也跟著改，見 useLibraryChange）。送出中再按不會再送一次。
 * 按過之後的結果只對「按的時候那一份論文頁資料」有效：路由換了一篇論文或重讀了（`page` 換成新的一份），
 * 就照新的 `in_my_library` 顯示，不沿用舊的狀態（審查 F9）。移出時已經不在（404）也算移出了。
 * 寫入帶 `readAs`（loader 讀這一頁時的成員），不是路由 context 現在的成員：409 之後路由先換成下一位、論文頁在背景重讀，
 * 重讀回來之前畫面還是上一位的資料，這時再按一次也要帶上一位（後端回 409，不會寫進下一位的文獻庫；審查 fix1 H2）。
 */
function usePaperLibrary(page: PaperPageData, readAs: ReadAs): LibraryControl {
  // 論文頁不套用「舊 409 不重跑」（#114 收尾 R3-3）：背景重讀失敗、畫面停在上一位的資料時，按下去照常跑換人流程。
  const change = useLibraryChange(readAs, { skipOld409: false });
  const [changed, setChanged] = useState<{ page: PaperPageData; inLibrary: boolean } | null>(null);
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
      if (outcome !== "failed") {
        setChanged({ page, inLibrary: action === "add" });
        setFocusToken((token) => token + 1);
      }
    });
  };
  return {
    inLibrary: libraryShown(page, changed),
    busy,
    focusToken,
    onAdd: () => run("add"),
    onRemove: () => run("remove"),
  };
}

/**
 * 論文頁的修正分類（Story 3.11）：送出帶 `readAs`（同 usePaperLibrary，ADR-0055）。成功之後這一份論文頁資料的分類區塊
 * 換成 API 回的（「◆ 已核對」＋人數，中文重點的標章因此改寫全文）；同 usePaperLibrary，只對按的時候那一份資料有效。
 * 歷史按「N 位成員修正過」才讀；讀到 401 照登入過期處理，409 照「發現換人」處理，其他沒讀到就不顯示（沒有設計稿，列給 Kevin）。
 */
function useClassificationFix(page: PaperPageData, readAs: ReadAs): { page: PaperPageData; control: ClassificationFixControl } {
  const sendFix = useSendClassificationFix(readAs);
  const follow = useFollowMemberChange();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const href = useLocation({ select: (location) => location.href });
  const [changed, setChanged] = useState<{ page: PaperPageData; classification: PaperPageData["classification"] } | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const history = useQuery({ ...classificationFixesQuery(page.paper.id, readAs), enabled: historyOpen && readAs !== undefined });
  const kind = history.data?.kind;
  useEffect(() => {
    if (kind === "memberChanged") {
      void follow();
    } else if (kind === "signedOut") {
      void (async () => {
        await clearMemberQueries(queryClient);
        await navigate({ to: "/login", search: { next: href, expired: true as const }, replace: true });
      })();
    }
  }, [kind, follow, queryClient, navigate, href]);
  const shown = changed !== null && changed.page === page ? { ...page, classification: changed.classification } : page;
  return {
    page: shown,
    control: {
      send: async (body) => {
        const fixes = await sendFix(page.paper.id, body);
        if (fixes === null) return false;
        setChanged({ page, classification: fixes.classification });
        return true;
      },
      historyOpen,
      onToggleHistory: () => setHistoryOpen((open) => !open),
      history: history.data?.kind === "found" ? history.data.fixes.history : null,
    },
  };
}

/**
 * 論文頁的回報錯誤（Story 3.12）：送出帶 `readAs`（同 useClassificationFix，ADR-0055）與畫面上那一筆中文重點的
 * `zh_summary_id`（ADR-0064）。成功之後這一份論文頁資料的中文重點區塊換成 API 回的（那一欄多了「有成員回報可能有誤」）；
 * 同 usePaperLibrary，只對按的時候那一份資料有效。
 */
function useSummaryReport(page: PaperPageData, readAs: ReadAs): { zhSummary: PaperPageData["zh_summary"]; control: SummaryReportControl } {
  const sendReport = useSendSummaryReport(readAs);
  const [changed, setChanged] = useState<{ page: PaperPageData; zhSummary: PaperPageData["zh_summary"] } | null>(null);
  const shown = changed !== null && changed.page === page ? changed.zhSummary : page.zh_summary;
  return {
    zhSummary: shown,
    control: {
      send: async (body) => {
        // 表單只在 ready 時出現；保險起見，畫面上沒有那一筆就不送。
        const seen = shown.state === "ready" ? shown.value?.zh_summary_id : undefined;
        if (seen === undefined) return false;
        const zhSummary = await sendReport(page.paper.id, { ...body, zh_summary_id: seen });
        if (zhSummary === null) return false;
        setChanged({ page, zhSummary });
        return true;
      },
    },
  };
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
      queryClient.fetchQuery(paperQuery(params.paperId, context.member.member_id)),
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
    // 帶的預期成員和現在登入的人不同（409，ADR-0055）：畫面照「發現換人」處理（FollowMemberChange）。
    if (result.kind === "memberChanged") {
      return { kind: "memberChanged" as const };
    }
    // signedOut 上面已經處理，剩下的只有 notFound。
    if (result.kind !== "found") {
      return { kind: "notFound" as const };
    }
    // 記下這份資料是用誰的身分讀的（結果的 readAs；不是 context：快取裡同一篇可能是別次讀的）。
    return { kind: "found" as const, page: result.page, readAs: result.readAs };
  },
  component: function PaperRoute() {
    const data = paperRoute.useLoaderData();
    if (data.kind === "notFound") {
      return <NotFound />;
    }
    if (data.kind === "memberChanged") {
      return <FollowMemberChange result={data} />;
    }
    // 換一篇論文、或換成另一位讀的資料時整塊重新掛上（送出中、焦點等元件內的狀態不帶過去；審查 F9、fix1 H2）。
    return <PaperWithNames key={`${data.readAs ?? ""}-${data.page.paper.id}`} page={data.page} readAs={data.readAs} />;
  },
});

/**
 * 論文頁的讀取回 409 member_changed（ADR-0055）：照「發現換人」處理（和寫入的 409 同一個流程），等待期間是載入中。
 * 每拿到一次新的結果（`result` 換了一份）就處理一次。
 */
function FollowMemberChange({ result }: { result: object }) {
  const follow = useFollowMemberChange();
  const handled = useRef<object | null>(null);
  useEffect(() => {
    if (handled.current === result) return;
    handled.current = result;
    void follow();
  }, [result, follow]);
  return <PageLoading />;
}

const appTree = appRoute.addChildren([indexRoute, noticeRoute, coverageRoute, searchRoute, libraryRoute, paperRoute]);

export const routeTree = import.meta.env.DEV
  ? rootRoute.addChildren([loginRoute, appTree, ...devRoutes()])
  : rootRoute.addChildren([loginRoute, appTree]);
