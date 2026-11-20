// 成員版的資料來源：呼叫同網域的後端 API（呼叫函式由 orval 從 openapi.json 產生，Story 3.4）。
import {
  addLibraryItem,
  clientConfig as getClientConfig,
  displayNames as getDisplayNames,
  fakeLogin,
  fakeLoginOptions,
  listLibrary,
  listPapers,
  logout,
  me as getMe,
  paper as getPaper,
  removeLibraryItem,
  restoreLibraryItem,
} from "../../generated/api";
import {
  FakeLoginMode,
  type FakeLoginBody,
  type LibraryItemResponse,
  type LibraryPageResponse,
  type PaperListResponse,
  type PaperRow,
} from "../../generated/model";
import type {
  ClientConfigResult,
  DataSource,
  DisplayNamesResult,
  LibraryChangeResult,
  LibraryList,
  LibraryListResult,
  LibrarySource,
  LoginOutcome,
  Member,
  MemberSession,
  PaperList,
  PaperListFilters,
  PaperListResult,
  PaperResult,
  PaperSource,
} from "../types";

/** 後端 API 的基底路徑（Conventions「API」）。 */
export const API_BASE = "/api/v1";

const session: MemberSession = {
  async me(): Promise<Member | null> {
    const response = await getMe();
    if (response.status === 200) {
      return response.data;
    }
    // 沒登入與登入過期都是 401 not_authenticated（3.3 分不出兩者，由呼叫端依「本來在站內」判斷）。
    if (response.status === 401) {
      return null;
    }
    throw new Error(`GET ${API_BASE}/me: HTTP ${response.status}`);
  },

  async logIn({ memberId, next }): Promise<LoginOutcome> {
    try {
      // 假登入的模式：開發環境（pick_member）要帶一位成員，試用環境（access_admin）由後端決定只登入管理者。
      const options = await fakeLoginOptions();
      if (options.status !== 200) {
        return { kind: "failed" };
      }
      const body: FakeLoginBody = { next: next ?? null };
      if (options.data.mode === FakeLoginMode.pick_member) {
        body.member_id = memberId ?? options.data.members[0]?.member_id ?? null;
      }
      const response = await fakeLogin(body);
      if (response.status === 200) {
        return { kind: "signedIn", next: response.data.next };
      }
      if (response.status === 403 && response.data.code === "login_rejected") {
        return { kind: "denied" };
      }
      return { kind: "failed" };
    } catch {
      // 網路錯誤、回應不是 JSON：都當作沒有登入成功。
      return { kind: "failed" };
    }
  },

  async logOut(): Promise<void> {
    const response = await logout();
    if (response.status !== 204) {
      throw new Error(`POST ${API_BASE}/auth/logout: HTTP ${response.status}`);
    }
  },
};

const papers: PaperSource = {
  async paper(paperId: string): Promise<PaperResult> {
    // 路徑參數一定要編碼：呼叫端已確認是 UUID，這裡再保險一次，不讓 ../ 之類的字跑出這個路徑。
    const response = await getPaper(encodeURIComponent(paperId));
    if (response.status === 200) {
      return { kind: "found", page: response.data };
    }
    if (response.status === 404) {
      return { kind: "notFound" };
    }
    if (response.status === 401) {
      return { kind: "signedOut" };
    }
    throw new Error(`GET ${API_BASE}/papers/{id}: HTTP ${response.status}`);
  },

  async displayNames(): Promise<DisplayNamesResult> {
    const response = await getDisplayNames();
    if (response.status === 200) {
      return { kind: "found", names: response.data };
    }
    // 和論文的 401 一樣：登入過期由路由清快取、回登入頁（Codex 審查 3.8 中 3）。
    if (response.status === 401) {
      return { kind: "signedOut" };
    }
    throw new Error(`GET ${API_BASE}/display-names: HTTP ${response.status}`);
  },

  async clientConfig(): Promise<ClientConfigResult> {
    const response = await getClientConfig();
    if (response.status === 200) {
      return { kind: "found", config: response.data };
    }
    if (response.status === 401) {
      return { kind: "signedOut" };
    }
    throw new Error(`GET ${API_BASE}/client-config: HTTP ${response.status}`);
  },

  async list(filters: PaperListFilters, limit: number, previous?: PaperList | null): Promise<PaperListResult> {
    // 「再載入」：從上一次的清單接著讀（先用它多讀到的 pending，再照 next_cursor 讀下一頁）。
    let rows: PaperRow[] = previous ? [...previous.items, ...previous.pending] : [];
    let last: Omit<PaperListResponse, "items"> | null = previous
      ? { next_cursor: previous.next_cursor, total: previous.total, snapshot: previous.snapshot }
      : null;
    while (last === null || (rows.length < limit && last.next_cursor !== null)) {
      const cursor: string | undefined = last?.next_cursor ?? undefined;
      const response = await listPapers(cursor === undefined ? filters : { ...filters, cursor });
      if (response.status === 401) {
        return { kind: "signedOut" };
      }
      if (response.status !== 200) {
        // 422（條件不合法，ADR-0047）與其他錯誤：畫面顯示「錯誤」。
        throw new Error(`GET ${API_BASE}/papers: HTTP ${response.status}`);
      }
      const page = response.data;
      rows = [...rows, ...page.items];
      last = {
        // 空的一頁卻還有下一頁（後端不會這樣回）：當作沒有下一頁，不會一直打下去。
        next_cursor: page.items.length === 0 ? null : page.next_cursor,
        total: page.total,
        snapshot: page.snapshot,
      };
    }
    // 只放要求的篇數（型別的約定），多讀到的留在 pending，下一次接著用。
    return { kind: "found", list: { ...last, items: rows.slice(0, limit), pending: rows.slice(limit) } };
  },
};

/** 加入、移出、復原共用：200 回那一筆、404 notFound、401 signedOut，其他丟例外。 */
async function change(
  name: string,
  call: () => Promise<{ status: number; data: unknown }>,
): Promise<LibraryChangeResult> {
  const response = await call();
  if (response.status === 200) {
    return { kind: "done", item: response.data as LibraryItemResponse };
  }
  if (response.status === 404) {
    return { kind: "notFound" };
  }
  if (response.status === 401) {
    return { kind: "signedOut" };
  }
  throw new Error(`${name}: HTTP ${response.status}`);
}

const library: LibrarySource = {
  async list(limit: number, previous?: LibraryList | null): Promise<LibraryListResult> {
    // 同找論文的 list：先用上一次多讀到的 pending，不夠才照 next_cursor 讀下一頁。
    let rows: LibraryItemResponse[] = previous ? [...previous.items, ...previous.pending] : [];
    let last: Omit<LibraryPageResponse, "items"> | null = previous
      ? { next_cursor: previous.next_cursor, total: previous.total }
      : null;
    while (last === null || (rows.length < limit && last.next_cursor !== null)) {
      const cursor: string | undefined = last?.next_cursor ?? undefined;
      const response = await listLibrary(cursor === undefined ? undefined : { cursor });
      if (response.status === 401) {
        return { kind: "signedOut" };
      }
      if (response.status !== 200) {
        throw new Error(`GET ${API_BASE}/library: HTTP ${response.status}`);
      }
      const page = response.data;
      rows = [...rows, ...page.items];
      last = { next_cursor: page.items.length === 0 ? null : page.next_cursor, total: page.total };
    }
    return { kind: "found", list: { ...last, items: rows.slice(0, limit), pending: rows.slice(limit) } };
  },

  // 路徑參數一定要編碼（同論文頁）。
  add: (paperId) => change(`PUT ${API_BASE}/library/{id}`, () => addLibraryItem(encodeURIComponent(paperId))),
  remove: (paperId) => change(`DELETE ${API_BASE}/library/{id}`, () => removeLibraryItem(encodeURIComponent(paperId))),
  restore: (paperId) =>
    change(`POST ${API_BASE}/library/{id}/restore`, () => restoreLibraryItem(encodeURIComponent(paperId))),
};

const impl = {
  siteKind: () => "member" as const,
  session,
  papers,
  library,
  // e2e 用它確認示範站的建置結果不含 API 程式（成員版的建置結果帶著 /api/v1）。
  apiBase: API_BASE,
};

export const datasource: DataSource = impl;
